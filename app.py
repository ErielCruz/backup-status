#!/usr/bin/env python3
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template
import subprocess

app = Flask(__name__)


def env(n, d):
    return os.environ.get(n) or d


SAMSUNG = env("SAMSUNG_MOUNT", "/samsung")
TB = env("TB_MOUNT", "/4tb")
LOG_DIR = env("LOG_DIR", "/logs")
AUDIT_STATE = Path("/state/audit-latest.json")
RESTIC_PASSWORD_FILE = "/run/secrets/restic_password"
RESTIC_ENV_FILE = "/home/eriel/Documents/home_server_ops/home_server_backup/config/restic.env"

STATUS_FILE = Path(LOG_DIR) / "status.json"
RUNS_DIR = Path(SAMSUNG) / "Backup_SSD" / "HomeServerBackups" / "runs"

_cache = {}

def cached(key, ttl=300):
    now = time.time()
    if key in _cache and _cache[key]["expires"] > now:
        return _cache[key]["data"]
    return None

def set_cache(key, data, ttl=300):
    _cache[key] = {"data": data, "expires": time.time() + ttl}


def run(cmd, timeout=30, env=None):
    try:
        import subprocess
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
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
    else:
        return f"{b/(1024*1024*1024):.1f} GB"


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
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return iso[:16]


app.jinja_env.filters["fmt_bytes"] = fmt_bytes
app.jinja_env.filters["fmt_duration"] = fmt_duration
app.jinja_env.filters["fmt_date_short"] = fmt_date_short


def load_status():
    if STATUS_FILE.is_file():
        try:
            return json.loads(STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"jobs": [], "updated": None}


SAMSUNG = env("SAMSUNG_MOUNT", "/samsung")
TB = env("TB_MOUNT", "/4tb")
LOG_DIR = env("LOG_DIR", "/logs")
AUDIT_STATE = Path("/state/audit-latest.json")
RESTIC_PASSWORD_FILE = "/run/secrets/restic_password"
RESTIC_ENV_FILE = "/home/eriel/Documents/home_server_ops/home_server_backup/config/restic.env"

def get_restic_info():
    data = cached("restic_info")
    if data is not None:
        return data

    result = {"b2": None, "hetzner": None, "secrets": None, "error": None}

    env = os.environ.copy()
    env.update({
        "RESTIC_PASSWORD_FILE": RESTIC_PASSWORD_FILE,
        "PATH": os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""),
    })

    repos = {
        "b2": "b2:backup-ssd-ecc:restic-backups",
        "hetzner": "rclone:hetzner-4tb:restic-backups",
        "secrets": "b2:backup-ssd-ecc:restic-secrets"
    }

    for name, repo in repos.items():
        try:
            cmd = f"restic -r '{repo}' snapshots --json 2>/dev/null"
            out, ok = run(cmd, timeout=15, env=env)
            if ok and out:
                snapshots = json.loads(out)
                result[name] = {
                    "count": len(snapshots),
                    "latest": snapshots[0]["time"][:19] if snapshots else None
                }
        except Exception as e:
            result["error"] = str(e)

    set_cache("restic_info", result, ttl=600)
    return result


def get_remote_sizes():
    data = cached("remote_sizes")
    if data is not None:
        return data

    env = os.environ.copy()
    env.update({
        "PATH": os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""),
    })

    result = {}
    
    local_runs = Path(SAMSUNG) / "Backup_SSD" / "HomeServerBackups" / "runs"
    if local_runs.exists():
        out, _ = run(f"du -sb '{local_runs}' 2>/dev/null", timeout=10, env=env)
        try:
            result["local_runs_size"] = int(out.split()[0])
        except Exception:
            result["local_runs_size"] = 0
    else:
        result["local_runs_size"] = 0
    
    remotes = {
        "b2_runs": "backup-ssd-ecc:backup-ssd-ecc/HomeServerBackups/runs",
        "b2_pictures": "pictures-ecc:pictures-ecc",
        "hetzner_runs": "hetzner-4tb:Backup_SSD/HomeServerBackups/runs",
    }
    
    for name, remote in remotes.items():
        try:
            out, ok = run(f"rclone size --json '{remote}' 2>/dev/null", timeout=30, env=env)
            if ok and out:
                info = json.loads(out)
                result[name] = {"count": info.get("count", 0), "bytes": info.get("bytes", 0)}
            else:
                result[name] = {"count": 0, "bytes": 0}
        except Exception:
            result[name] = {"count": 0, "bytes": 0}
    
    set_cache("remote_sizes", result, ttl=600)
    return result


def get_audit_summary():
    data = cached("audit_summary")
    if data is not None:
        return data
    
    result = {"errors": 0, "warnings": 0, "last_audit": None, "passed": True}
    
    if AUDIT_STATE.exists():
        try:
            audit = json.loads(AUDIT_STATE.read_text())
            result["errors"] = audit.get("errors", 0)
            result["warnings"] = audit.get("warnings", 0)
            result["last_audit"] = audit.get("audit_time", "")
            result["snapshots"] = audit.get("snapshots", {})
            result["b2_latest"] = audit.get("b2_latest", "")
            result["hetzner_latest"] = audit.get("hetzner_latest", "")
            result["passed"] = result["errors"] == 0
        except Exception:
            pass
    
    set_cache("audit_summary", result, ttl=300)
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


def get_run_inventory():
    data = cached("run_inventory")
    if data is not None:
        return data

    runs = []
    if RUNS_DIR.is_dir():
        for run_dir in sorted(RUNS_DIR.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            run_id = run_dir.name
            run_info = {"run_id": run_id, "files": [], "total_size": 0, "has_report": False}

            for f in sorted(run_dir.iterdir()):
                if f.is_file():
                    size = f.stat().st_size
                    run_info["files"].append({"name": f.name, "size": size})
                    run_info["total_size"] += size
                    if f.name == "backup-report.json":
                        try:
                            report = json.loads(f.read_text())
                            run_info["has_report"] = True
                            run_info["duration"] = report.get("duration_seconds", 0)
                            run_info["started"] = report.get("started_at", "")
                            run_info["status"] = report.get("status", "unknown")
                            run_info["archive_size"] = report.get("archive_size_bytes", 0)
                            run_info["verify_log"] = report.get("verify_log")
                        except (json.JSONDecodeError, OSError):
                            pass

                if f.name == "verify.log":
                    try:
                        vtext = f.read_text()
                        run_info["verify_pass"] = "FAIL" not in vtext.upper() and "ERROR" not in vtext.upper()
                    except Exception:
                        run_info["verify_pass"] = None

            runs.append(run_info)

    set_cache("run_inventory", runs, ttl=600)
    return runs


def get_backup_stats():
    runs = get_run_inventory()
    total_size = sum(r["total_size"] for r in runs)
    with_reports = [r for r in runs if r.get("has_report")]
    verified = [r for r in runs if r.get("verify_pass") is not None]
    passed = [r for r in verified if r.get("verify_pass")]

    return {
        "runs_total": len(runs),
        "total_size": total_size,
        "runs_with_reports": len(with_reports),
        "runs_with_verification": len(verified),
        "verified_passed": len(passed),
    }


def get_verification_status():
    runs = get_run_inventory()
    recent = []
    for r in runs[:20]:
        verify = "unknown"
        if r.get("verify_pass") is True:
            verify = "pass"
        elif r.get("verify_pass") is False:
            verify = "fail"

        recent.append({
            "run_id": r["run_id"][:16],
            "started": r.get("started", ""),
            "size": r.get("archive_size", r.get("total_size", 0)),
            "duration": r.get("duration", 0),
            "verify": verify,
            "total_files": len(r.get("files", [])),
        })

    verified = [r for r in recent if r["verify"] != "unknown"]
    rate = round(len([r for r in verified if r["verify"] == "pass"]) / len(verified) * 100) if verified else 0

    return {"rate": rate, "recent": recent}


def get_overall_health():
    status = load_status()
    jobs = status.get("jobs", [])
    failures = [j for j in jobs if j.get("last_result") != "success"]
    verif = get_verification_status()
    audit = get_audit_summary()

    if failures or (audit.get("errors", 0) > 0):
        health = "fail"
    elif verif.get("rate", 100) < 100 and verif.get("recent"):
        health = "warn"
    elif audit.get("warnings", 0) > 0:
        health = "warn"
    else:
        health = "ok"

    return {
        "status": health,
        "job_failures": len(failures),
        "job_total": len(jobs),
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
        jobs=load_status().get("jobs", []),
        disk=get_disk(),
        runs=get_run_inventory(),
        stats=get_backup_stats(),
        verification=get_verification_status(),
        health=get_overall_health(),
        restic=get_restic_info(),
        remotes=get_remote_sizes(),
        audit=get_audit_summary(),
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/refresh")
def api_refresh():
    return render_template(
        "dashboard.html",
        now=now(),
        jobs=load_status().get("jobs", []),
        disk=get_disk(),
        runs=get_run_inventory(),
        stats=get_backup_stats(),
        verification=get_verification_status(),
        health=get_overall_health(),
        restic=get_restic_info(),
        remotes=get_remote_sizes(),
        audit=get_audit_summary(),
    )


@app.route("/api/clear-cache")
def clear_cache():
    _cache.clear()
    return jsonify({"status": "cache cleared"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
