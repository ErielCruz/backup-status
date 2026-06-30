#!/usr/bin/env python3
from flask import Flask, render_template, jsonify
from pathlib import Path
import json
import os
import subprocess
import time

app = Flask(__name__)


def env(name, default):
    return os.environ.get(name) or default


RESTIC_REPO = env("RESTIC_REPOSITORY", "b2:backup-ssd-ecc:restic-backups")
RESTIC_PASSWORD_FILE = env("RESTIC_PASSWORD_FILE", "/run/secrets/restic_password")
SAMSUNG_MOUNT = env("SAMSUNG_MOUNT", "/samsung")
TB_MOUNT = env("TB_MOUNT", "/4tb")
LOG_DIR = env("LOG_DIR", "/logs")


def run_cmd(cmd, timeout=15):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.returncode == 0
    except subprocess.TimeoutExpired:
        return "Command timed out", False
    except Exception as exc:
        return str(exc), False


def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def get_systemd_timers():
    output, success = run_cmd(
        "systemctl list-timers --all --no-pager 2>/dev/null | grep -E 'backup|sync|pull' "
        "|| systemctl --user list-timers --all --no-pager 2>/dev/null | grep -E 'backup|sync|pull' "
        "|| cat /logs/timer-status.txt 2>/dev/null "
        "|| echo 'Run backup-status.sh on mini: systemctl --user list-timers --all | grep backup|sync'"
    )
    return output, success


def get_restic_snapshots():
    if not Path(RESTIC_PASSWORD_FILE).exists():
        return {"snapshots": [], "error": f"Password file not found"}
    output, success = run_cmd(
        f"restic -r {RESTIC_REPO} --password-file {RESTIC_PASSWORD_FILE} snapshots --compact --json 2>/dev/null"
    )
    if success and output.strip():
        try:
            return {"snapshots": json.loads(output), "success": True}
        except json.JSONDecodeError:
            return {"snapshots": [], "error": "Failed to parse snapshot JSON"}
    return {"snapshots": [], "error": output[-400:] if output else "No snapshots found"}


def get_restic_stats():
    if not Path(RESTIC_PASSWORD_FILE).exists():
        return "Password file not found"
    output, _ = run_cmd(
        f"restic -r {RESTIC_REPO} --password-file {RESTIC_PASSWORD_FILE} stats --mode raw-data 2>/dev/null"
    )
    return output


def get_disk_usage():
    output, _ = run_cmd(
        f"df -h {SAMSUNG_MOUNT} {TB_MOUNT} 2>/dev/null || df -h /"
    )
    return output


def get_recent_logs():
    log_dir = Path(LOG_DIR)
    if not log_dir.is_dir():
        return "Log directory not available"
    lines = []
    for f in sorted(log_dir.glob("*.log"), reverse=True)[:5]:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            if content:
                lines.append(f"=== {f.name} ===\n")
                lines.extend(content.splitlines()[-30:])
                lines.append("")
        except Exception:
            pass
    return "\n".join(lines[-80:]) if lines else "No log content"


def get_failed_units():
    output, _ = run_cmd(
        "systemctl --user --failed --no-pager 2>/dev/null | grep -E 'backup|sync|pull' "
        "|| echo 'No failed units'"
    )
    return output


@app.route("/")
def index():
    return render_template(
        "index.html",
        now=ts(),
        timers=get_systemd_timers()[0],
        snapshots=get_restic_snapshots(),
        stats=get_restic_stats(),
        disk=get_disk_usage(),
        failed=get_failed_units(),
        logs=get_recent_logs(),
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/timers")
def api_timers():
    output, _ = get_systemd_timers()
    return render_template("sections/timers.html", output=output, now=ts())


@app.route("/api/snapshots")
def api_snapshots():
    return render_template("sections/snapshots.html", snapshots=get_restic_snapshots(), now=ts())


@app.route("/api/stats")
def api_stats():
    return render_template("sections/stats.html", output=get_restic_stats(), now=ts())


@app.route("/api/disk")
def api_disk():
    return render_template("sections/disk.html", output=get_disk_usage(), now=ts())


@app.route("/api/logs")
def api_logs():
    return render_template("sections/logs.html", output=get_recent_logs(), now=ts())


@app.route("/api/failed")
def api_failed():
    return render_template("sections/failed.html", output=get_failed_units(), now=ts())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
