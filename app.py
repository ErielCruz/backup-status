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
MIRROR_CACHE_FILE = Path(STATE_DIR) / "mirror-checks-latest.json"
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

MIRROR_TRIGGER_UNITS = [
    "sync-local-backup-ssd",
    "sync-local-pictures",
    "sync-remote-b2-backup-ssd",
    "sync-remote-b2-pictures",
    "sync-remote-hetzner-backup-ssd",
    "sync-remote-hetzner-pictures",
    "backup-audit",
    "sync-remote-hetzner-long-term",
]

CHAINED_AFTER = {
    "backup-critical-secrets": "after system backup",
    "sync-local-backup-ssd": "after secrets",
    "sync-local-pictures": "after Backup_SSD local mirror",
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

LIVE_PICTURES_EXCLUDES = [
    "lost+found/**",
    "Immich/lost+found/**",
    "Immich/encoded-video/**",
    "Immich/thumbs/**",
    "Immich/.Trash-1000/**",
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
            ("Linux source", PICTURES, LIVE_PICTURES_EXCLUDES),
            ("SSD", f"{SAMSUNG}/Pictures", LIVE_PICTURES_EXCLUDES),
            ("4TB", f"{TB}/Pictures", LIVE_PICTURES_EXCLUDES),
            ("B2", "pictures-ecc:pictures-ecc/Pictures", LIVE_PICTURES_EXCLUDES),
            ("Hetzner", "hetzner-4tb:Pictures/Pictures", LIVE_PICTURES_EXCLUDES),
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


def format_log_time(iso):
    dt = iso_to_utc(iso)
    if not dt:
        return "-"
    return dt.strftime("%H:%M:%S")


def classify_log_level(message):
    lowered = message.lower()

    if re.search(r"\berrors?\s*:\s*0\b", lowered) or "no errors were found" in lowered:
        return "ok"
    if re.search(r"\bwarnings?\s*:\s*0\b", lowered):
        return "ok"
    if "audit result: passed" in lowered:
        return "ok"

    if re.search(r"\berrors?\s*:\s*[1-9]\d*\b", lowered):
        return "error"
    if re.search(r"\bwarnings?\s*:\s*[1-9]\d*\b", lowered):
        return "warn"

    if re.search(r"\b(failed|failure|error|denied|corrupt)\b", lowered):
        return "error"
    if re.search(r"\b(warn|warning|timeout)\b", lowered) or "timed out" in lowered:
        return "warn"

    if "finished " in lowered or "completed " in lowered or " ok" in lowered or "success" in lowered:
        return "ok"
    return "info"


def parse_log_line(line):
    entry = {"time": "", "level": "info", "message": line}
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z))\s+\S+\s+([^:]+):\s*(.*)$", line)
    if match:
        entry["time"] = format_log_time(match.group(1))
        source = match.group(2).strip()
        message = match.group(3).strip()
        message = re.sub(r"^(Starting|Finished)\s+.+?\.service\s+-\s+", r"\1 ", message)
        message = re.sub(r"^Starting\s+", "Started ", message)
        message = re.sub(r"^Finished\s+", "Completed ", message)
        message = re.sub(r"^(Started|Completed)\s+Collect\s+", r"\1 ", message)
        entry["message"] = message
        if "backup-status-collector" not in source:
            entry["source"] = source
    entry["level"] = classify_log_level(entry["message"])
    return entry


def parse_log_lines(lines):
    return [parse_log_line(line) for line in lines]


def load_json_file(path, default):
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def write_json_file(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp_path.replace(path)
        return True
    except Exception:
        return False


def iso_to_utc(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def max_iso(values):
    dates = [iso_to_utc(value) for value in values if value]
    dates = [date for date in dates if date]
    if not dates:
        return None
    return max(dates).isoformat()


def is_future_iso(iso, grace_seconds=60):
    dt = iso_to_utc(iso)
    if not dt:
        return False
    return (dt - datetime.now(timezone.utc)).total_seconds() > grace_seconds


def get_unit_logs(name, lines=20):
    collector_lines = get_collector_unit_logs(name)
    if collector_lines:
        return sort_log_lines(collector_lines)[:lines]

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
    if ok and out and "-- No entries --" not in out:
        return latest_invocation_lines(sort_log_lines(out.splitlines()))[:lines]

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
    if not ok or not out or "-- No entries --" in out:
        return []
    return latest_invocation_lines(sort_log_lines(out.splitlines()))[:lines]


def latest_invocation_lines(lines):
    latest = []
    for line in lines:
        latest.append(line)
        if "Starting " in line:
            break
    return latest


def get_collector_unit_logs(name):
    status = load_json_file(STATUS_FILE, {})
    for pipeline in (status.get("pipelines") or {}).values():
        for unit in pipeline.get("units", []):
            if unit.get("name") == name:
                return unit.get("last_lines") or []
    for unit in status.get("standalone", []):
        if unit.get("name") == name:
            return unit.get("last_lines") or []
    return []


def sort_log_lines(lines):
    def key(item):
        index, line = item
        ts = iso_to_utc(parse_journal_timestamp(line))
        return (ts or datetime.min.replace(tzinfo=timezone.utc), -index)

    return [line for _index, line in sorted(enumerate(lines), key=key, reverse=True)]


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


def result_from_logs(lines):
    for line in lines:
        if "Failed to start " in line or "Main process exited" in line:
            return "failure"
        if "Finished " in line:
            return "success"
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
        logs = get_unit_logs(name, 80)
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
        logs = get_unit_logs(name, 80)
        if logs:
            log_last_run = last_run_from_logs(logs)
            copy["last_lines"] = logs
            if not copy.get("last_run") or is_future_iso(copy.get("last_run")):
                copy["last_run"] = log_last_run
            if copy.get("last_result") in (None, "", "unknown", "never-run"):
                copy["last_result"] = result_from_logs(logs) or copy.get("last_result", "unknown")
            copy["source"] = "journal"
        return copy
    logs = get_unit_logs(name, 80)
    if logs:
        return {
            "name": name,
            "next_run": None,
            "next_label": CHAINED_AFTER.get(name),
            "last_run": last_run_from_logs(logs),
            "last_result": result_from_logs(logs) or "unknown",
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


def format_audit_verifications(audit):
    rows = []
    for item in audit.get("verify_results", []):
        label = item.get("label", "")
        row = {
            "label": label,
            "status": item.get("status", "unknown"),
            "kind": "Raw mirror",
            "local": "-",
            "remote": "-",
        }
        if item.get("snapshot_count") is not None:
            latest = item.get("latest_snapshot")
            if not latest and label.startswith("B2:"):
                latest = audit.get("b2_latest")
            if not latest and label.startswith("Hetzner:"):
                latest = audit.get("hetzner_latest")
            remote = f"{item.get('snapshot_count')} snapshots"
            if latest:
                remote += f", latest {fmt_date_short(latest)}"
            row.update({
                "kind": "Restic repository",
                "local": "encrypted snapshot source",
                "remote": remote,
            })
        else:
            local_count = item.get("local_count")
            remote_count = item.get("remote_count")
            local_size = item.get("local_size")
            remote_size = item.get("remote_size")
            row["local"] = (
                f"{local_count} files, {fmt_bytes(local_size)}"
                if local_count is not None and local_size is not None
                else item.get("reason", "-")
            )
            row["remote"] = (
                f"{remote_count} files, {fmt_bytes(remote_size)}"
                if remote_count is not None and remote_size is not None
                else item.get("reason", "-")
            )
        rows.append(row)
    return rows


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


def mirror_timeout(group_id, label, target):
    if group_id == "live_pictures" and label == "Hetzner":
        return 300
    if str(target).startswith("hetzner-4tb:Pictures/"):
        return 180
    if str(target).startswith("hetzner-4tb:"):
        return 120
    return 60


def source_changed_after_sync(group_id, items, trigger_ts):
    """Return the newest source-file change timestamp after the last sync.

    A live library can legitimately grow after the chained mirror run. That is
    a pending replication window, not evidence that an already-completed sync
    failed. We only apply this distinction to the live Pictures source.
    """
    if group_id != "live_pictures" or not trigger_ts:
        return None
    source = next((item for item in items if item.get("label") == "Linux source"), None)
    if not source or source.get("status") not in ("ok", "stale"):
        return None

    trigger = iso_to_utc(trigger_ts)
    if not trigger:
        return None

    excluded = {
        "lost+found",
        "Immich/lost+found",
        "Immich/encoded-video",
        "Immich/thumbs",
        "Immich/.Trash-1000",
    }
    newest = None
    for root, dirs, files in os.walk(PICTURES, onerror=lambda _error: None):
        relative_root = os.path.relpath(root, PICTURES)
        if relative_root == ".":
            relative_root = ""
        dirs[:] = [
            directory
            for directory in dirs
            if os.path.join(relative_root, directory) not in excluded
        ]
        for filename in files:
            relative_path = os.path.join(relative_root, filename)
            if any(relative_path == path or relative_path.startswith(path + os.sep) for path in excluded):
                continue
            try:
                stat_result = os.stat(os.path.join(root, filename), follow_symlinks=False)
                # Immich can move a completed upload into its library while
                # preserving the original media mtime.  ctime changes for that
                # move, so use the newest of the two to identify files that
                # appeared after rclone had already scanned the source tree.
                modified = datetime.fromtimestamp(
                    max(stat_result.st_mtime, stat_result.st_ctime),
                    timezone.utc,
                )
            except OSError:
                continue
            if newest is None or modified > newest:
                newest = modified

    if newest and newest > trigger:
        return newest.isoformat()
    return None


def compare_to_source(items):
    # Never compare remote copies against a stale/incomplete local source.
    # A disconnected or not-yet-mounted local source can otherwise make the
    # remotes look like they contain thousands of unexpected files.
    source_labels = {"Linux source", "SSD source", "4TB source"}
    unavailable_sources = [
        item for item in items
        if item.get("label") in source_labels
        and item.get("status") not in ("ok", "stale")
    ]
    if unavailable_sources:
        return "unknown"

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
    groups = []
    for group in MIRROR_GROUPS:
        items = []
        for label, target, _excludes in group["items"]:
            info = {"label": label, "target": target, "count": None, "bytes": None, "status": "loading"}
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
    # The audit's Pictures result is combined:
    # ~/Pictures + Backup_SSD/Photos + Backup_SSD/Videos.
    # It must not be used as a fallback for the Live Pictures-only row.
    return None


def mirror_trigger_timestamp(status, audit):
    runs = [audit.get("audit_time")]
    for pipeline in status.get("pipelines", {}).values():
        for unit in pipeline.get("units", []):
            if unit.get("name") in MIRROR_TRIGGER_UNITS:
                runs.append(unit.get("last_run"))
    return max_iso(runs)


def load_mirror_cache():
    data = cached("mirror_checks_record")
    if data is not None:
        return data
    record = load_json_file(MIRROR_CACHE_FILE, {})
    if record.get("groups"):
        set_cache("mirror_checks_record", record, ttl=3600)
    return record


def save_mirror_cache(record):
    set_cache("mirror_checks_record", record, ttl=3600)
    write_json_file(MIRROR_CACHE_FILE, record)


def compute_mirror_checks(trigger_ts=None):
    groups = []
    for group in MIRROR_GROUPS:
        items = []
        with ThreadPoolExecutor(max_workers=min(6, len(group["items"]))) as executor:
            futures = {
                executor.submit(rclone_size, target, excludes, mirror_timeout(group["id"], label, target)): (label, target)
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
                    elif "timed out" in info.get("error", "").lower():
                        info["status"] = "timeout"
                        info["error"] = "Live size count timed out. The sync service can still be healthy; this only means the dashboard could not finish enumerating this location."
                info.update({"label": label, "target": target})
                items.append(info)
        order = {label: index for index, (label, _target, _excludes) in enumerate(group["items"])}
        items.sort(key=lambda item: order.get(item["label"], 999))
        status = compare_to_source(items)
        pending_since = source_changed_after_sync(group["id"], items, trigger_ts)
        if status == "mismatch" and pending_since:
            status = "pending"
        groups.append({
            "id": group["id"],
            "label": group["label"],
            "note": group["note"],
            "status": status,
            "pending_since": pending_since,
            "items": items,
        })
    record = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "trigger_ts": trigger_ts,
        "groups": groups,
    }
    save_mirror_cache(record)


def get_mirror_checks(status, audit):
    trigger_ts = mirror_trigger_timestamp(status, audit)
    record = load_mirror_cache()
    if record.get("groups") and record.get("trigger_ts") == trigger_ts:
        return record["groups"]

    if record.get("groups"):
        refresh_in_background("mirror_checks", lambda: compute_mirror_checks(trigger_ts))
        groups = record["groups"]
        for group in groups:
            group["cache_state"] = "refreshing"
        return groups

    refresh_in_background("mirror_checks", lambda: compute_mirror_checks(trigger_ts))
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
        # Dashboard reads must stay lock-free so monitoring never blocks backups.
        cmd = ["restic", "--no-lock", "-r", definition["repo"], "snapshots", "--json", "--latest", "7"]
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
        elif group["status"] == "pending":
            warnings.append(f"{group['label']}: new source files are waiting for the next sync")
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


def is_mirror_refreshing(mirrors):
    for group in mirrors:
        if group.get("status") == "loading" or group.get("cache_state") == "refreshing":
            return True
        if any(item.get("status") == "loading" for item in group.get("items", [])):
            return True
    return False


def is_restic_refreshing(restic):
    return any(repo.get("status") == "loading" for repo in restic)


def build_mirror_context(status=None, audit=None):
    status = status or load_status()
    audit = audit or get_audit_state()
    mirrors = get_mirror_checks(status, audit)
    mirror_record = load_mirror_cache()
    return {
        "mirrors": mirrors,
        "mirror_refreshing": is_mirror_refreshing(mirrors),
        "mirror_meta": {
            "checked_at": mirror_record.get("checked_at"),
            "trigger_ts": mirror_record.get("trigger_ts"),
            "current_trigger_ts": mirror_trigger_timestamp(status, audit),
        },
    }


def build_restic_context():
    restic = get_restic_info()
    return {
        "restic": restic,
        "restic_refreshing": is_restic_refreshing(restic),
    }


def build_context():
    status = load_status()
    audit = get_audit_state()
    mirror_context = build_mirror_context(status, audit)
    restic_context = build_restic_context()
    return {
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": status,
        "disk": get_disk(),
        "health": get_overall_health(status, audit, mirror_context["mirrors"], restic_context["restic"]),
        **mirror_context,
        **restic_context,
        "audit": audit,
        "audit_verify_rows": format_audit_verifications(audit),
        "history": load_failure_history(),
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


@app.route("/api/mirrors")
def api_mirrors():
    return render_template("_mirror_parity.html", **build_mirror_context())


@app.route("/api/restic")
def api_restic():
    return render_template("_restic_snapshots.html", **build_restic_context())


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
    raw_lines = get_unit_logs(unit_name, lines)
    return jsonify({"unit": unit_name, "lines": raw_lines, "entries": parse_log_lines(raw_lines)})


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
    try:
        MIRROR_CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return jsonify({"status": "cache cleared"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
