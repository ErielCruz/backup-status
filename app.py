#!/usr/bin/env python3
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template

app = Flask(__name__)


def env(n, d):
    return os.environ.get(n) or d


SAMSUNG = env("SAMSUNG_MOUNT", "/samsung")
TB = env("TB_MOUNT", "/4tb")
LOG_DIR = env("LOG_DIR", "/logs")
AUDIT_STATE = Path("/state/audit-latest.json")
RESTIC_PASSWORD_FILE = "/run/secrets/restic_password"

STATUS_FILE = Path(LOG_DIR) / "status.json"

_cache = {}


def cached(key, ttl=300):
    now = time.time()
    if key in _cache and _cache[key]["expires"] > now:
        return _cache[key]["data"]
    return None


def set_cache(key, data, ttl=300):
    _cache[key] = {"data": data, "expires": time.time() + ttl}


def run(cmd, timeout=30, env_vars=None):
    try:
        run_env = os.environ.copy()
        if env_vars:
            run_env.update(env_vars)
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=run_env)
        return r.stdout.strip(), r.returncode == 0
    except subprocess.TimeoutExpired:
        return "Command timed out", False
    except Exception as e:
        return str(e), False


def fmt_bytes(b):
    if b is None or b == 0:
        return "0 B"
    b = int(b)
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b/1024:.1f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b/(1024*1024):.1f} MB"
    elif b < 1024 * 1024 * 1024 * 1024:
        return f"{b/(1024*1024*1024):.1f} GB"
    else:
        return f"{b/(1024*1024*1024*1024):.1f} TB"


def fmt_duration(sec):
    if sec is None or sec == 0:
        return "0s"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    elif sec < 3600:
        m = sec // 60
        s = sec % 60
        return f"{m}m{s}s"
    else:
        h = sec // 3600
        m = (sec % 3600) // 60
        return f"{h}h{m}m"


def fmt_date_short(iso):
    if not iso:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(iso)[:16]


def fmt_time_ago(iso):
    if not iso:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = now - dt
        secs = int(diff.total_seconds())
        if secs < 0:
            return "soon"
        elif secs < 60:
            return f"{secs}s ago"
        elif secs < 3600:
            return f"{secs // 60}m ago"
        elif secs < 86400:
            return f"{secs // 3600}h ago"
        else:
            return f"{secs // 86400}d ago"
    except Exception:
        return str(iso)[:16]


app.jinja_env.filters["fmt_bytes"] = fmt_bytes
app.jinja_env.filters["fmt_duration"] = fmt_duration
app.jinja_env.filters["fmt_date_short"] = fmt_date_short
app.jinja_env.filters["fmt_time_ago"] = fmt_time_ago


def load_status():
    if STATUS_FILE.is_file():
        try:
            return json.loads(STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"pipelines": {}, "standalone": [], "updated": None}


def get_audit_state():
    data = cached("audit_state")
    if data is not None:
        return data

    result = {
        "errors": 0,
        "warnings": 0,
        "error_list": [],
        "warning_list": [],
        "last_audit": None,
        "snapshots": {},
        "b2_latest": "",
        "hetzner_latest": "",
        "verify_results": [],
    }

    if AUDIT_STATE.exists():
        try:
            audit = json.loads(AUDIT_STATE.read_text())
            result.update(audit)
        except Exception:
            pass

    set_cache("audit_state", result, ttl=300)
    return result


def get_remote_sizes():
    data = cached("remote_sizes")
    if data is not None:
        return data

    run_env = {
        "PATH": os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""),
    }

    result = {
        "system_state": {"local": {}, "mirror": {}, "b2": {}, "hetzner": {}},
        "pictures": {"local": {}, "mirror": {}, "b2": {}, "hetzner": {}},
        "long_term": {"source": {}, "hetzner": {}},
    }

    local_runs = Path(SAMSUNG) / "Backup_SSD" / "HomeServerBackups" / "runs"
    if local_runs.exists():
        out, _ = run(f"du -sb '{local_runs}' 2>/dev/null", timeout=10, env_vars=run_env)
        try:
            result["system_state"]["local"]["bytes"] = int(out.split()[0])
        except Exception:
            pass
        out, _ = run(f"find '{local_runs}' -type f 2>/dev/null | wc -l", timeout=10, env_vars=run_env)
        try:
            result["system_state"]["local"]["count"] = int(out.strip())
        except Exception:
            pass

    mirror_runs = Path(TB) / "Backup_SSD" / "HomeServerBackups" / "runs"
    if mirror_runs.exists():
        out, _ = run(f"du -sb '{mirror_runs}' 2>/dev/null", timeout=10, env_vars=run_env)
        try:
            result["system_state"]["mirror"]["bytes"] = int(out.split()[0])
        except Exception:
            pass

    local_pictures = Path(os.path.expanduser("~")) / "Pictures"
    if local_pictures.exists():
        out, _ = run(f"du -sb '{local_pictures}' 2>/dev/null", timeout=30, env_vars=run_env)
        try:
            result["pictures"]["local"]["bytes"] = int(out.split()[0])
        except Exception:
            pass
        out, _ = run(f"find '{local_pictures}' -type f 2>/dev/null | wc -l", timeout=30, env_vars=run_env)
        try:
            result["pictures"]["local"]["count"] = int(out.strip())
        except Exception:
            pass

    samsung_photos = Path(SAMSUNG) / "Backup_SSD" / "Photos"
    samsung_videos = Path(SAMSUNG) / "Backup_SSD" / "Videos"
    for p in [samsung_photos, samsung_videos]:
        if p.exists():
            out, _ = run(f"du -sb '{p}' 2>/dev/null", timeout=10, env_vars=run_env)
            try:
                result["pictures"]["local"]["bytes"] = result["pictures"]["local"].get("bytes", 0) + int(out.split()[0])
            except Exception:
                pass

    mirror_pictures = Path(TB) / "Pictures"
    if mirror_pictures.exists():
        out, _ = run(f"du -sb '{mirror_pictures}' 2>/dev/null", timeout=30, env_vars=run_env)
        try:
            result["pictures"]["mirror"]["bytes"] = int(out.split()[0])
        except Exception:
            pass

    remotes = {
        ("system_state", "b2"): "backup-ssd-ecc:backup-ssd-ecc/HomeServerBackups",
        ("system_state", "hetzner"): "hetzner-4tb:Backup_SSD/HomeServerBackups",
        ("pictures", "b2"): "pictures-ecc:pictures-ecc",
        ("pictures", "hetzner"): "hetzner-4tb:Pictures",
        ("long_term", "hetzner"): "hetzner-4tb:Long_Term_Backup",
    }

    for (pipeline, dest), remote in remotes.items():
        try:
            out, ok = run(f"rclone size --json '{remote}' 2>/dev/null", timeout=60, env_vars=run_env)
            if ok and out:
                info = json.loads(out)
                result[pipeline][dest] = {"count": info.get("count", 0), "bytes": info.get("bytes", 0)}
            else:
                result[pipeline][dest] = {"count": 0, "bytes": 0}
        except Exception:
            result[pipeline][dest] = {"count": 0, "bytes": 0}

    set_cache("remote_sizes", result, ttl=600)
    return result


def get_restic_info():
    data = cached("restic_info")
    if data is not None:
        return data

    run_env = os.environ.copy()
    run_env.update({
        "RESTIC_PASSWORD_FILE": RESTIC_PASSWORD_FILE,
        "PATH": os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""),
    })

    repos = {
        "b2": "b2:backup-ssd-ecc:restic-backups",
        "hetzner": "rclone:hetzner-4tb:restic-backups",
        "secrets": "b2:backup-ssd-ecc:restic-secrets",
    }

    result = {}
    for name, repo in repos.items():
        try:
            cmd = f"restic -r '{repo}' snapshots --json 2>/dev/null"
            out, ok = run(cmd, timeout=15, env_vars=run_env)
            if ok and out:
                snapshots = json.loads(out)
                result[name] = {
                    "count": len(snapshots),
                    "latest": snapshots[0]["time"][:19] if snapshots else None,
                }
        except Exception:
            pass

    set_cache("restic_info", result, ttl=600)
    return result


def get_disk():
    labels = {SAMSUNG: "Samsung SSD", TB: "4TB Drive"}
    out, _ = run(f"df -h {shlex.quote(SAMSUNG)} {shlex.quote(TB)} 2>/dev/null || df -h /")
    lines = []
    for line in out.split("\n")[1:]:
        parts = line.split()
        if len(parts) >= 6:
            mount = parts[5]
            lines.append({
                "label": labels.get(mount, mount),
                "size": parts[1],
                "used": parts[2],
                "avail": parts[3],
                "use_pct": parts[4],
            })
    return lines


def get_overall_health():
    status = load_status()
    pipelines = status.get("pipelines", {})
    audit = get_audit_state()

    failures = []
    for pid, pdata in pipelines.items():
        if pdata.get("last_result") != "success":
            failures.append(pdata.get("label", pid))

    if failures or audit.get("errors", 0) > 0:
        health = "fail"
    elif audit.get("warnings", 0) > 0:
        health = "warn"
    else:
        health = "ok"

    return {
        "status": health,
        "pipeline_failures": failures,
        "audit_errors": audit.get("errors", 0),
        "audit_warnings": audit.get("warnings", 0),
    }


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@app.route("/")
def index():
    return render_template(
        "index.html",
        now=now(),
        status=load_status(),
        disk=get_disk(),
        health=get_overall_health(),
        restic=get_restic_info(),
        remotes=get_remote_sizes(),
        audit=get_audit_state(),
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/refresh")
def api_refresh():
    return render_template(
        "dashboard.html",
        now=now(),
        status=load_status(),
        disk=get_disk(),
        health=get_overall_health(),
        restic=get_restic_info(),
        remotes=get_remote_sizes(),
        audit=get_audit_state(),
    )


@app.route("/api/clear-cache")
def clear_cache():
    _cache.clear()
    return jsonify({"status": "cache cleared"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
