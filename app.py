#!/usr/bin/env python3
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template

app = Flask(__name__)


def env(n, d):
    return os.environ.get(n) or d


RESTIC_REPO = env("RESTIC_REPOSITORY", "b2:backup-ssd-ecc:restic-backups")
RESTIC_PW = env("RESTIC_PASSWORD_FILE", "/run/secrets/restic_password")
SAMSUNG = env("SAMSUNG_MOUNT", "/samsung")
TB = env("TB_MOUNT", "/4tb")
LOG_DIR = env("LOG_DIR", "/logs")
STATUS_FILE = Path(LOG_DIR) / "status.json"


def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode == 0
    except subprocess.TimeoutExpired:
        return "Command timed out", False
    except Exception as e:
        return str(e), False


def load_status():
    if STATUS_FILE.is_file():
        try:
            return json.loads(STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"jobs": [], "updated": None}


def get_snapshots():
    if not Path(RESTIC_PW).exists():
        return {"count": 0, "latest": None, "error": "Password file not found"}
    out, ok = run(
        f"restic -r {RESTIC_REPO} --password-file {RESTIC_PW} snapshots --compact --json 2>/dev/null"
    )
    if ok and out:
        try:
            snaps = json.loads(out)
            latest = None
            if snaps:
                latest = max(s.get("time", "") for s in snaps if s.get("time"))
            return {"count": len(snaps), "latest": latest}
        except (json.JSONDecodeError, ValueError):
            pass
    return {"count": 0, "latest": None, "error": "No snapshot data"}


def get_disk():
    labels = {SAMSUNG: "Samsung SSD", TB: "4TB Drive"}
    out, _ = run(f"df -h {SAMSUNG} {TB} 2>/dev/null || df -h /")
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


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@app.route("/")
def index():
    status = load_status()
    return render_template(
        "index.html",
        now=now(),
        jobs=status.get("jobs", []),
        disk=get_disk(),
        snapshots=get_snapshots(),
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/refresh")
def api_refresh():
    status = load_status()
    return render_template(
        "dashboard.html",
        now=now(),
        jobs=status.get("jobs", []),
        disk=get_disk(),
        snapshots=get_snapshots(),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
