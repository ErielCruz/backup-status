#!/usr/bin/env python3
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


def env(name, default):
    return os.environ.get(name) or default


SAMSUNG = env("SAMSUNG_MOUNT", "/samsung")
TB = env("TB_MOUNT", "/4tb")
PICTURES = env("PICTURES_MOUNT", "/pictures")
LOG_DIR = env("LOG_DIR", "/logs")
STATE_DIR = env("STATE_DIR", "/state")
AUDIT_STATE = Path(STATE_DIR) / "audit-latest.json"
RESTIC_PASSWORD_FILE = env("RESTIC_PASSWORD_FILE", "/run/secrets/restic_password")
STATUS_FILE = Path(LOG_DIR) / "status.json"
HISTORY_FILE = Path(LOG_DIR) / "failure_history.json"

USER_UID = env("BACKUP_USER_UID", "1000")
SYSTEMD_ENV = {
    "XDG_RUNTIME_DIR": f"/run/user/{USER_UID}",
    "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{USER_UID}/bus",
}

PIPELINES = {
    "backups": {
        "label": "System, Containers, Secrets",
        "description": "Encrypted restic snapshots to SSD, 4TB, B2, Hetzner",
        "units": ["backup-system-state", "backup-critical-secrets"],
    },
    "syncs": {
        "label": "Raw Mirrors",
        "description": "Backup_SSD data, Pictures, Photos, Videos to local and remote mirrors",
        "units": [
            "sync-local-backup-ssd",
            "sync-local-pictures-ssd",
            "sync-local-pictures",
            "sync-remote-b2-backup-ssd",
            "sync-remote-b2-pictures",
            "sync-remote-hetzner-backup-ssd",
            "sync-remote-hetzner-pictures",
            "backup-audit",
        ],
    },
    "long_term": {
        "label": "Long-Term Archive",
        "description": "4TB source mirrored only to Hetzner",
        "units": ["sync-remote-hetzner-long-term"],
    },
}

STANDALONE_UNITS = ["backup-restore-test", "log-rotate", "pull-dockge-compose", "backup-status-collector"]

CHAINED_AFTER = {
    "backup-critical-secrets": "after system backup",
    "sync-local-backup-ssd": "after secrets",
    "sync-local-pictures-ssd": "after Backup_SSD local mirror",
    "sync-local-pictures": "after Pictures SSD mirror",
    "sync-remote-b2-backup-ssd": "after local Pictures mirror",
    "sync-remote-b2-pictures": "after B2 Backup_SSD mirror",
    "sync-remote-hetzner-backup-ssd": "after B2 Pictures mirror",
    "sync-remote-hetzner-pictures": "after Hetzner Backup_SSD mirror",
    "backup-audit": "after Hetzner Pictures mirror",
    "sync-remote-hetzner-long-term": "after audit",
}

RESTIC_REPOS = {
    "system_ssd": {
        "label": "System SSD",
        "repo": f"{SAMSUNG}/Backup_SSD/HomeServerBackups/restic-backups",
        "kind": "system",
    },
    "system_4tb": {
        "label": "System 4TB",
        "repo": f"{TB}/Backup_SSD/HomeServerBackups/restic-backups",
        "kind": "system",
    },
    "system_b2": {
        "label": "System B2",
        "repo": "rclone:backup-ssd-ecc:backup-ssd-ecc/HomeServerBackups/restic-backups",
        "kind": "system",
    },
    "system_hetzner": {
        "label": "System Hetzner",
        "repo": "rclone:hetzner-4tb:restic-backups",
        "kind": "system",
    },
    "secrets_ssd": {
        "label": "Secrets SSD",
        "repo": f"{SAMSUNG}/Backup_SSD/HomeServerBackups/restic-secrets",
        "kind": "secrets",
    },
    "secrets_4tb": {
        "label": "Secrets 4TB",
        "repo": f"{TB}/Backup_SSD/HomeServerBackups/restic-secrets",
        "kind": "secrets",
    },
    "secrets_b2": {
        "label": "Secrets B2",
        "repo": "rclone:backup-ssd-ecc:backup-ssd-ecc/HomeServerBackups/restic-secrets",
        "kind": "secrets",
    },
    "secrets_hetzner": {
        "label": "Secrets Hetzner",
        "repo": "rclone:hetzner-4tb:restic-secrets",
        "kind": "secrets",
    },
}

BACKUP_SSD_EXCLUDES = [
    "Photos/**",
    "Videos/**",
    "HomeServerBackups/restic-backups/**",
    "HomeServerBackups/restic-secrets/**",
    "HomeServerBackups/runs/**",
    "HomeServerBackups/latest",
    "HomeServerBackups/*/latest",
]

MIRROR_GROUPS = [
    {
        "id": "backup_ssd_archive",
        "label": "Backup_SSD Archive Data",
        "note": "Raw archive mirror, excluding Photos, Videos, restic repos, and old run metadata",
        "items": [
            ("SSD source", f"{SAMSUNG}/Backup_SSD", BACKUP_SSD_EXCLUDES),
            ("4TB", f"{TB}/Backup_SSD", BACKUP_SSD_EXCLUDES),
            ("B2", "backup-ssd-ecc:backup-ssd-ecc", BACKUP_SSD_EXCLUDES),
            ("Hetzner", "hetzner-4tb:Backup_SSD", BACKUP_SSD_EXCLUDES),
        ],
    },
    {
        "id": "live_pictures",
        "label": "Live Pictures",
        "note": "Immich and Photoprism live library",
        "items": [
            ("Linux source", PICTURES, ["lost+found/**", "Immich/lost+found/**"]),
            ("SSD", f"{SAMSUNG}/Pictures", ["lost+found/**", "Immich/lost+found/**"]),
            ("4TB", f"{TB}/Pictures", ["lost+found/**", "Immich/lost+found/**"]),
            ("B2", "pictures-ecc:pictures-ecc/Pictures", []),
            ("Hetzner", "hetzner-4tb:Pictures/Pictures", []),
        ],
    },
    {
        "id": "photo_archive",
        "label": "SSD Photo Archive",
        "note": "Photo archive whose source is the Samsung SSD",
        "items": [
            ("SSD source", f"{SAMSUNG}/Backup_SSD/Photos", []),
            ("4TB", f"{TB}/Backup_SSD/Photos", []),
            ("B2", "pictures-ecc:pictures-ecc/Photos", []),
            ("Hetzner", "hetzner-4tb:Pictures/Photos", []),
        ],
    },
    {
        "id": "video_archive",
        "label": "SSD Video Archive",
        "note": "Video archive whose source is the Samsung SSD",
        "items": [
            ("SSD source", f"{SAMSUNG}/Backup_SSD/Videos", []),
            ("4TB", f"{TB}/Backup_SSD/Videos", []),
            ("B2", "pictures-ecc:pictures-ecc/Videos", []),
            ("Hetzner", "hetzner-4tb:Pictures/Videos", []),
        ],
    },
    {
        "id": "long_term",
        "label": "Long-Term Archive",
        "note": "Special case: source is 4TB and backup is Hetzner only",
        "items": [
            ("4TB source", f"{TB}/Long_Term_Backup", []),
            ("Hetzner", "hetzner-4tb:Long_Term_Backup", []),
        ],
    },
]

_cache = {}
_cache_lock = threading.Lock()
_refreshing = set()


def cached(key):
    now_ts = time.time()
    with _cache_lock:
        item = _cache.get(key)
        if item and item["expires"] > now_ts:
            return item["data"]
    return None


def set_cache(key, data, ttl=300):
    with _cache_lock:
        _cache[key] = {"data": data, "expires": time.time() + ttl}


def refresh_in_background(key, target):
    with _cache_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def worker():
        try:
            target()
        finally:
            with _cache_lock:
                _refreshing.discard(key)

    threading.Thread(target=worker, daemon=True).start()


def run_args(args, timeout=30, env_vars=None):
    try:
        run_env = os.environ.copy()
        run_env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + run_env.get("PATH", "")
        if env_vars:
            run_env.update(env_vars)
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=run_env)
        text = (result.stdout or result.stderr or "").strip()
        return text, result.returncode == 0
    except subprocess.TimeoutExpired:
        return "Command timed out", False
    except Exception as exc:
        return str(exc), False


def fmt_bytes(value):
    if value is None:
        return "-"
    value = int(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def fmt_date_short(iso):
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso))
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(iso)[:16]


def fmt_time_ago(iso):
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        seconds = int(diff.total_seconds())
        if seconds < -60:
            return "in " + fmt_duration(abs(seconds))
        if seconds < 0:
            return "soon"
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"
    except Exception:
        return str(iso)[:16]


def fmt_duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


app.jinja_env.filters["fmt_bytes"] = fmt_bytes
app.jinja_env.filters["fmt_date_short"] = fmt_date_short
app.jinja_env.filters["fmt_time_ago"] = fmt_time_ago
app.jinja_env.filters["fmt_duration"] = fmt_duration


def parse_systemd_timestamp(value):
    if not value or value in ("0", "n/a", "[not set]"):
        return None
    value = value.strip()
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(int(value) / 1e6, tz=timezone.utc).isoformat()
    except Exception:
        return value


def parse_journal_timestamp(line):
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z))", line)
    if not match:
        return None
    try:
        value = match.group(1).replace("Z", "+00:00")
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def load_json_file(path, default):
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def get_unit_logs(name, lines=20):
    args = [
        "journalctl",
        "--no-pager",
        "--since",
        "7 days ago",
        "--grep",
        f"{name}.service",
        "-n",
        str(lines),
        "--output=short-iso",
    ]
    out, ok = run_args(args, timeout=10, env_vars=SYSTEMD_ENV)
    if ok and out and "-- No entries --" not in out:
        return out.splitlines()

    args = [
        "journalctl",
        f"_UID={USER_UID}",
        f"_SYSTEMD_USER_UNIT={name}.service",
        "--no-pager",
        "-n",
        str(lines),
        "--output=short-iso",
    ]
    out, ok = run_args(args, timeout=10, env_vars=SYSTEMD_ENV)
    if not ok or not out:
        return []
    return [line for line in out.splitlines() if f"{name}.service" in line or f"{name}." in line][-lines:]


def last_run_from_logs(lines):
    finished = []
    started = []
    for line in lines:
        if "Finished " in line or "Failed to start " in line or "Main process exited" in line:
            ts = parse_journal_timestamp(line)
            if ts:
                finished.append(ts)
        elif "Starting " in line:
            ts = parse_journal_timestamp(line)
            if ts:
                started.append(ts)
    if finished:
        return max(finished)
    if started:
        return max(started)
    return None


def get_unit_info(name, fallback=None):
    props = [
        "Result",
        "ExecMainStatus",
        "ActiveState",
        "SubState",
        "InactiveExitTimestamp",
        "ActiveEnterTimestamp",
    ]
    args = ["systemctl", "--user", "show", f"{name}.service"]
    for prop in props:
        args.extend(["-p", prop])
    args.append("--value")
    out, ok = run_args(args, timeout=8, env_vars=SYSTEMD_ENV)

    if ok and out:
        values = out.splitlines()
        result = values[0] if len(values) > 0 else ""
        exit_code = values[1] if len(values) > 1 else ""
        active_state = values[2] if len(values) > 2 else ""
        sub_state = values[3] if len(values) > 3 else ""
        inactive_ts = values[4] if len(values) > 4 else ""
        active_ts = values[5] if len(values) > 5 else ""

        next_out, _ = run_args(
            ["systemctl", "--user", "show", f"{name}.timer", "-p", "NextElapseUSecRealtime", "--value"],
            timeout=8,
            env_vars=SYSTEMD_ENV,
        )
        last_trigger, _ = run_args(
            ["systemctl", "--user", "show", f"{name}.timer", "-p", "LastTriggerUSec", "--value"],
            timeout=8,
            env_vars=SYSTEMD_ENV,
        )
        timer_state, _ = run_args(
            ["systemctl", "--user", "show", f"{name}.timer", "-p", "ActiveState", "--value"],
            timeout=8,
            env_vars=SYSTEMD_ENV,
        )
        logs = get_unit_logs(name, 15)
        last_run = (
            parse_systemd_timestamp(last_trigger)
            or parse_systemd_timestamp(inactive_ts)
            or parse_systemd_timestamp(active_ts)
            or last_run_from_logs(logs)
            or (fallback or {}).get("last_run")
        )
        next_run = parse_systemd_timestamp(next_out)
        if active_state in ("active", "activating"):
            last_result = "running"
        elif result:
            last_result = result
        elif fallback:
            last_result = fallback.get("last_result", "unknown")
        else:
            last_result = "never-run" if not last_run else "unknown"
        return {
            "name": name,
            "next_run": next_run,
            "next_label": None if next_run else CHAINED_AFTER.get(name),
            "last_run": last_run,
            "last_result": last_result,
            "exit_code": exit_code,
            "active_state": active_state,
            "sub_state": sub_state,
            "timer_active": timer_state == "active",
            "last_lines": logs,
            "source": "live",
        }

    if fallback:
        copy = dict(fallback)
        copy["source"] = "collector"
        copy.setdefault("next_label", CHAINED_AFTER.get(name))
        logs = get_unit_logs(name, 15)
        if logs:
            copy["last_lines"] = logs
            copy["last_run"] = copy.get("last_run") or last_run_from_logs(logs)
            copy["source"] = "journal"
        return copy
    logs = get_unit_logs(name, 15)
    if logs:
        return {
            "name": name,
            "next_run": None,
            "next_label": CHAINED_AFTER.get(name),
            "last_run": last_run_from_logs(logs),
            "last_result": "unknown",
            "exit_code": "",
            "active_state": "unknown",
            "sub_state": "unknown",
            "timer_active": False,
            "last_lines": logs,
            "source": "journal",
        }
    return {
        "name": name,
        "next_run": None,
        "next_label": CHAINED_AFTER.get(name),
        "last_run": None,
        "last_result": "unknown",
        "exit_code": "",
        "active_state": "unknown",
        "sub_state": "unknown",
        "timer_active": False,
        "last_lines": [],
        "source": "missing",
    }


def load_status():
    collector = load_json_file(STATUS_FILE, {"pipelines": {}, "standalone": [], "updated": None})
    status = {"updated": collector.get("updated"), "pipelines": {}, "standalone": []}

    for pid, definition in PIPELINES.items():
        fallback_pipeline = collector.get("pipelines", {}).get(pid, {})
        fallback_units = {unit.get("name"): unit for unit in fallback_pipeline.get("units", [])}
        units = [get_unit_info(name, fallback_units.get(name)) for name in definition["units"]]
        failures = [u for u in units if u.get("last_result") in ("failure", "failed")]
        running = [u for u in units if u.get("last_result") == "running"]
        parent = units[0] if units else {}
        next_unit = next((u for u in units if u.get("next_run")), parent)
        last_unit = next((u for u in units if u.get("last_run")), parent)
        status["pipelines"][pid] = {
            "label": definition["label"],
            "description": definition["description"],
            "last_run": last_unit.get("last_run"),
            "last_result": "failed" if failures else ("running" if running else parent.get("last_result", "unknown")),
            "next_run": next_unit.get("next_run"),
            "next_label": None if next_unit.get("next_run") else parent.get("next_label"),
            "units": units,
        }

    fallback_standalone = {unit.get("name"): unit for unit in collector.get("standalone", [])}
    status["standalone"] = [get_unit_info(name, fallback_standalone.get(name)) for name in STANDALONE_UNITS]
    return status


def load_failure_history():
    return load_json_file(HISTORY_FILE, {})


def get_audit_state():
    data = cached("audit_state")
    if data is not None:
        return data
    result = {
        "errors": 0,
        "warnings": 0,
        "error_list": [],
        "warning_list": [],
        "audit_time": None,
        "snapshots": {},
        "b2_latest": "",
        "hetzner_latest": "",
        "verify_results": [],
    }
    result.update(load_json_file(AUDIT_STATE, {}))
    set_cache("audit_state", result, ttl=120)
    return result


def rclone_size(target, excludes=None, timeout=90):
    args = ["rclone", "size", "--json"]
    for pattern in excludes or []:
        args.extend(["--exclude", pattern])
    args.append(target)
    out, ok = run_args(args, timeout=timeout)
    if not ok or not out:
        return {"count": None, "bytes": None, "status": "error", "error": out[:240]}
    try:
        parsed = json.loads(out)
        return {
            "count": parsed.get("count", 0),
            "bytes": parsed.get("bytes", 0),
            "status": "ok",
            "error": "",
        }
    except Exception as exc:
        return {"count": None, "bytes": None, "status": "error", "error": str(exc)}


def compare_to_source(items):
    source = next((item for item in items if item["count"] is not None and item["bytes"] is not None), None)
    if not source:
        return "unknown"
    mismatched = []
    unknown = []
    for item in items:
        if item["status"] not in ("ok", "stale"):
            unknown.append(item)
            continue
        item["count_delta"] = item["count"] - source["count"]
        item["bytes_delta"] = item["bytes"] - source["bytes"]
        if item["count_delta"] != 0 or item["bytes_delta"] != 0:
            mismatched.append(item)
    if mismatched:
        return "mismatch"
    if unknown:
        return "unknown"
    return "ok"


def build_mirror_placeholders():
    audit = get_audit_state()
    audit_by_label = {item.get("label"): item for item in audit.get("verify_results", [])}
    groups = []
    for group in MIRROR_GROUPS:
        items = []
        for label, target, _excludes in group["items"]:
            info = {"label": label, "target": target, "count": None, "bytes": None, "status": "loading"}
            if group["id"] == "live_pictures":
                if label in ("Linux source", "B2"):
                    source = audit_by_label.get("B2:Pictures")
                    if source:
                        info["count"] = source.get("remote_count" if label == "B2" else "local_count")
                        info["bytes"] = source.get("remote_size" if label == "B2" else "local_size")
                        info["status"] = "ok"
                if label == "Hetzner":
                    source = audit_by_label.get("Hetzner:Pictures")
                    if source:
                        info["count"] = source.get("remote_count")
                        info["bytes"] = source.get("remote_size")
                        info["status"] = "ok"
            items.append(info)
        groups.append({
            "id": group["id"],
            "label": group["label"],
            "note": group["note"],
            "status": "loading",
            "items": items,
        })
    return groups


def audit_size_fallback(group_id, label):
    audit = get_audit_state()
    audit_by_label = {item.get("label"): item for item in audit.get("verify_results", [])}
    if group_id != "live_pictures":
        return None
    if label in ("Linux source", "B2"):
        source = audit_by_label.get("B2:Pictures")
        if not source:
            return None
        return {
            "count": source.get("remote_count" if label == "B2" else "local_count"),
            "bytes": source.get("remote_size" if label == "B2" else "local_size"),
        }
    if label == "Hetzner":
        source = audit_by_label.get("Hetzner:Pictures")
        if not source:
            return None
        return {"count": source.get("remote_count"), "bytes": source.get("remote_size")}
    return None


def compute_mirror_checks():
    groups = []
    for group in MIRROR_GROUPS:
        items = []
        with ThreadPoolExecutor(max_workers=min(6, len(group["items"]))) as executor:
            futures = {
                executor.submit(rclone_size, target, excludes, 60): (label, target)
                for label, target, excludes in group["items"]
            }
            for future in as_completed(futures):
                label, target = futures[future]
                try:
                    info = future.result()
                except Exception as exc:
                    info = {"count": None, "bytes": None, "status": "error", "error": str(exc)}
                if info.get("status") == "error":
                    fallback = audit_size_fallback(group["id"], label)
                    if fallback and fallback.get("count") is not None and fallback.get("bytes") is not None:
                        info.update(fallback)
                        info["status"] = "stale"
                        info["error"] = "Live size check timed out; showing last audit value."
                info.update({"label": label, "target": target})
                items.append(info)
        order = {label: index for index, (label, _target, _excludes) in enumerate(group["items"])}
        items.sort(key=lambda item: order.get(item["label"], 999))
        groups.append({
            "id": group["id"],
            "label": group["label"],
            "note": group["note"],
            "status": compare_to_source(items),
            "items": items,
        })
    set_cache("mirror_checks", groups, ttl=1800)


def get_mirror_checks():
    data = cached("mirror_checks")
    if data is not None:
        return data
    refresh_in_background("mirror_checks", compute_mirror_checks)
    groups = build_mirror_placeholders()
    set_cache("mirror_checks_placeholder", groups, ttl=45)
    return groups


def build_restic_placeholders():
    audit = get_audit_state()
    count_map = {
        "system_b2": audit.get("snapshots", {}).get("b2"),
        "system_hetzner": audit.get("snapshots", {}).get("hetzner"),
        "secrets_b2": audit.get("snapshots", {}).get("secrets"),
        "secrets_hetzner": audit.get("snapshots", {}).get("secrets"),
    }
    latest_map = {
        "system_b2": audit.get("b2_latest"),
        "system_hetzner": audit.get("hetzner_latest"),
    }
    repos = []
    for key, definition in RESTIC_REPOS.items():
        repos.append({
            "key": key,
            "label": definition["label"],
            "kind": definition["kind"],
            "count": count_map.get(key),
            "latest": latest_map.get(key),
            "snapshots": [],
            "status": "loading",
            "error": "",
        })
    return repos


def compute_restic_info():
    run_env = {
        "RESTIC_PASSWORD_FILE": RESTIC_PASSWORD_FILE,
    }

    def inspect_repo(key, definition):
        cmd = ["restic", "-r", definition["repo"], "snapshots", "--json", "--latest", "7"]
        if not definition["repo"].startswith("rclone:"):
            cmd.insert(1, "--no-lock")
        out, ok = run_args(cmd, timeout=30, env_vars=run_env)
        item = {
            "key": key,
            "label": definition["label"],
            "kind": definition["kind"],
            "count": None,
            "latest": None,
            "snapshots": [],
            "status": "error",
            "error": "",
        }
        if ok and out:
            try:
                snapshots = json.loads(out)
                snapshots.sort(key=lambda snap: snap.get("time", ""), reverse=True)
                item.update({
                    "count": len(snapshots),
                    "latest": snapshots[0]["time"][:19] if snapshots else None,
                    "snapshots": [{"time": snap.get("time", "")[:19], "paths": snap.get("paths", [])} for snap in snapshots[:3]],
                    "status": "ok",
                })
            except Exception as exc:
                item["error"] = str(exc)
        else:
            item["error"] = out[:240]
        return item

    repos = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(inspect_repo, key, definition): key
            for key, definition in RESTIC_REPOS.items()
        }
        for future in as_completed(futures):
            repos.append(future.result())
    order = {key: index for index, key in enumerate(RESTIC_REPOS.keys())}
    repos.sort(key=lambda item: order.get(item["key"], 999))
    set_cache("restic_info", repos, ttl=600)


def get_restic_info():
    data = cached("restic_info")
    if data is not None:
        return data
    refresh_in_background("restic_info", compute_restic_info)
    return build_restic_placeholders()
    return repos


def get_disk():
    data = cached("disk")
    if data is not None:
        return data
    labels = {SAMSUNG: "Samsung SSD", TB: "4TB Drive", PICTURES: "Pictures Source"}
    paths = [SAMSUNG, TB, PICTURES]
    out, ok = run_args(["df", "-h", *paths], timeout=10)
    if not ok:
        out, _ = run_args(["df", "-h", "/"], timeout=10)
    seen = set()
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        mount = parts[5]
        if mount in seen:
            continue
        seen.add(mount)
        rows.append({
            "label": labels.get(mount, mount),
            "mount": mount,
            "size": parts[1],
            "used": parts[2],
            "avail": parts[3],
            "use_pct": parts[4],
        })
    set_cache("disk", rows, ttl=120)
    return rows


def get_recent_logs(status):
    important = []
    units = []
    for pipeline in status.get("pipelines", {}).values():
        units.extend(pipeline.get("units", []))
    units.extend(status.get("standalone", []))

    priority = [u for u in units if u.get("last_result") in ("failure", "failed", "running")]
    if not priority:
        priority = [u for u in units if u.get("name") in ("backup-system-state", "backup-audit", "sync-remote-hetzner-pictures")]
    for unit in priority[:5]:
        lines = unit.get("last_lines", [])[-6:]
        if lines:
            important.append({"unit": unit["name"], "lines": lines})
    return important


def get_overall_health(status, audit, mirrors, restic):
    issues = []
    warnings = []

    for pid, pipeline in status.get("pipelines", {}).items():
        failed_units = [u["name"] for u in pipeline.get("units", []) if u.get("last_result") in ("failure", "failed")]
        if failed_units:
            issues.append(f"{pipeline['label']}: failed unit(s) {', '.join(failed_units)}")

    if audit.get("errors", 0) > 0:
        issues.extend(audit.get("error_list") or [f"{audit.get('errors')} audit errors"])
    if audit.get("warnings", 0) > 0:
        warnings.extend(audit.get("warning_list") or [f"{audit.get('warnings')} audit warnings"])

    for group in mirrors:
        if group["status"] == "mismatch":
            issues.append(f"{group['label']}: file count or size mismatch")
        elif group["status"] == "unknown":
            warnings.append(f"{group['label']}: size check unavailable")

    bad_repos = [repo["label"] for repo in restic if repo["status"] == "error"]
    if bad_repos:
        warnings.append("Restic snapshot check failed for " + ", ".join(bad_repos))

    stale = False
    updated = status.get("updated")
    if updated:
        try:
            dt = datetime.fromisoformat(updated)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            stale = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > 3600
        except Exception:
            stale = True
    if stale:
        warnings.append("The collector file is stale; live systemd data is being used.")

    health = "fail" if issues else ("warn" if warnings else "ok")
    return {"status": health, "issues": issues, "warnings": warnings}


def build_context():
    status = load_status()
    audit = get_audit_state()
    mirrors = get_mirror_checks()
    restic = get_restic_info()
    return {
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": status,
        "disk": get_disk(),
        "health": get_overall_health(status, audit, mirrors, restic),
        "restic": restic,
        "mirrors": mirrors,
        "audit": audit,
        "history": load_failure_history(),
        "recent_logs": get_recent_logs(status),
    }


@app.route("/")
def index():
    return render_template("index.html", **build_context())


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/refresh")
def api_refresh():
    return render_template("dashboard.html", **build_context())


@app.route("/api/logs/<unit_name>")
def api_logs(unit_name):
    allowed = {unit for pipeline in PIPELINES.values() for unit in pipeline["units"]}
    allowed.update(STANDALONE_UNITS)
    if unit_name not in allowed:
        return jsonify({"error": "unknown unit"}), 404
    try:
        lines = max(1, min(int(request.args.get("lines", 50)), 200))
    except ValueError:
        lines = 50
    return jsonify({"unit": unit_name, "lines": get_unit_logs(unit_name, lines)})


@app.route("/api/trigger/<unit_name>", methods=["POST"])
def api_trigger(unit_name):
    allowed = {unit for pipeline in PIPELINES.values() for unit in pipeline["units"]}
    allowed.update(STANDALONE_UNITS)
    if unit_name not in allowed:
        return jsonify({"error": "unknown unit"}), 404
    out, ok = run_args(["systemctl", "--user", "start", f"{unit_name}.service"], timeout=10, env_vars=SYSTEMD_ENV)
    if ok:
        return jsonify({"status": "started", "unit": unit_name})
    return jsonify({"error": out}), 500


@app.route("/api/clear-cache")
def clear_cache():
    with _cache_lock:
        _cache.clear()
    return jsonify({"status": "cache cleared"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
