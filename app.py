#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import subprocess
import time


def env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


HOST = env("BACKUP_STATUS_HOST", "0.0.0.0")
PORT = int(env("BACKUP_STATUS_PORT", "5000"))
RESTIC_REPO = env("RESTIC_REPOSITORY", "b2:backup-ssd-ecc:restic-backups")
RESTIC_PASSWORD_FILE = env("RESTIC_PASSWORD_FILE", "/run/secrets/restic_password")
SAMSUNG_MOUNT = env("SAMSUNG_MOUNT", "/samsung")
TB_MOUNT = env("TB_MOUNT", "/4tb")


def run_cmd(cmd: str, timeout: int = 15) -> tuple[str, bool]:
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.returncode == 0
    except subprocess.TimeoutExpired:
        return "Command timed out", False
    except Exception as exc:
        return str(exc), False


def get_systemd_timers() -> dict:
    output, success = run_cmd(
        "systemctl list-timers --all --no-pager 2>/dev/null | grep -E 'backup|sync|pull' || echo 'systemctl not available'"
    )
    return {"output": output, "success": success}


def get_restic_snapshots() -> dict:
    if not Path(RESTIC_PASSWORD_FILE).exists():
        return {"snapshots": [], "error": f"Password file not found: {RESTIC_PASSWORD_FILE}"}
    output, success = run_cmd(
        f"restic -r {RESTIC_REPO} --password-file {RESTIC_PASSWORD_FILE} snapshots --compact --json 2>/dev/null"
    )
    if success and output.strip():
        try:
            return {"snapshots": json.loads(output), "success": True}
        except json.JSONDecodeError:
            return {"snapshots": [], "error": "Failed to parse snapshots", "raw": output[:500]}
    return {"snapshots": [], "error": output[-500:] if output else "No snapshots or restic error"}


def get_restic_stats() -> dict:
    if not Path(RESTIC_PASSWORD_FILE).exists():
        return {"output": "Password file not found", "success": False}
    output, success = run_cmd(
        f"restic -r {RESTIC_REPO} --password-file {RESTIC_PASSWORD_FILE} stats --mode raw-data 2>/dev/null"
    )
    return {"output": output, "success": success}


def get_disk_usage() -> dict:
    output, success = run_cmd(f"df -h {SAMSUNG_MOUNT} {TB_MOUNT} 2>/dev/null || df -h /")
    return {"output": output, "success": success}


def get_recent_logs() -> dict:
    output, success = run_cmd(
        "journalctl -u 'backup-*' -u 'sync-*' --since '7 days ago' --no-pager 2>/dev/null | tail -80 || echo 'journalctl not available'"
    )
    return {"output": output, "success": success}


def get_failed_units() -> dict:
    output, success = run_cmd(
        "systemctl --failed --no-pager 2>/dev/null | grep -E 'backup|sync|pull' || echo ''"
    )
    return {"output": output, "success": success}


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Backup Status</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #111827;
      color: #f9fafb;
    }}
    body {{
      margin: 0;
      padding: 20px;
      background: #111827;
    }}
    .container {{
      max-width: 1100px;
      margin: 0 auto;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 1.8rem;
      font-weight: 650;
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .pulse {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: #22c55e;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.4; }}
    }}
    h2 {{
      margin: 30px 0 10px;
      font-size: 1.2rem;
      font-weight: 600;
      color: #d1d5db;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }}
    @media (max-width: 800px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
    .card {{
      padding: 16px;
      border: 1px solid #374151;
      border-radius: 8px;
      background: #1f2937;
    }}
    pre {{
      margin: 0;
      padding: 12px;
      background: #111827;
      border: 1px solid #374151;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 0.82rem;
      line-height: 1.5;
      max-height: 360px;
      overflow-y: auto;
    }}
    .success {{ color: #22c55e; font-weight: 600; }}
    .error {{ color: #ef4444; font-weight: 600; }}
    .snapshot {{
      display: flex;
      justify-content: space-between;
      padding: 8px 12px;
      margin: 4px 0;
      background: #111827;
      border: 1px solid #374151;
      border-radius: 4px;
      font-size: 0.85rem;
    }}
    .snapshot-time {{ color: #9ca3af; }}
    .snapshot-tags {{
      display: inline-block;
      padding: 2px 8px;
      background: #1e3a5f;
      border-radius: 999px;
      font-size: 0.75rem;
    }}
    button {{
      padding: 8px 16px;
      border: 0;
      border-radius: 6px;
      background: #3b82f6;
      color: white;
      font-size: 0.9rem;
      cursor: pointer;
    }}
    button:hover {{ background: #2563eb; }}
    .updated {{ color: #6b7280; font-size: 0.8rem; margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="container">
    <h1><span class="pulse"></span> Backup Status Dashboard</h1>
    <button onclick="refresh()">Refresh</button>
    <span class="updated" id="updated"></span>

    <h2>Systemd Timers</h2>
    <div class="card"><pre id="timers">Loading...</pre></div>

    <div class="grid">
      <div>
        <h2>Restic Snapshots</h2>
        <div class="card"><div id="snapshots">Loading...</div></div>
      </div>
      <div>
        <h2>Restic Stats</h2>
        <div class="card"><pre id="stats">Loading...</pre></div>
      </div>
    </div>

    <div class="grid">
      <div>
        <h2>Disk Usage</h2>
        <div class="card"><pre id="disk">Loading...</pre></div>
      </div>
      <div>
        <h2>Failed Units</h2>
        <div class="card"><pre id="failed">Loading...</pre></div>
      </div>
    </div>

    <h2>Recent Logs (last 7 days)</h2>
    <div class="card"><pre id="logs">Loading...</pre></div>
  </div>

  <script>
    async function fetchJson(url) {{
      const r = await fetch(url);
      return r.json();
    }}
    function esc(s) {{
      return String(s).replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}})[c]));
    }}
    async function refresh() {{
      try {{
        const [timers, snapshots, stats, disk, logs, failed] = await Promise.all([
          fetchJson('/api/timers'),
          fetchJson('/api/snapshots'),
          fetchJson('/api/stats'),
          fetchJson('/api/disk'),
          fetchJson('/api/logs'),
          fetchJson('/api/failed')
        ]);
        document.getElementById('timers').textContent = timers.output || 'No timers found';
        document.getElementById('disk').textContent = disk.output;
        document.getElementById('logs').textContent = logs.output || 'No logs';
        document.getElementById('failed').textContent = failed.output || 'No failed units';
        document.getElementById('stats').textContent = stats.output || 'No stats available';
        const snapDiv = document.getElementById('snapshots');
        if (snapshots.snapshots && snapshots.snapshots.length > 0) {{
          const list = snapshots.snapshots.slice(-15).reverse();
          snapDiv.innerHTML = list.map(s => {{
            const tags = (s.tags||[]).map(t => `<span class="snapshot-tags">${{esc(t)}}</span>`).join(' ');
            return `<div class="snapshot"><span><strong>${{esc(s.short_id)}}</strong> ${{tags}}</span><span class="snapshot-time">${{esc(s.time)}}</span></div>`;
          }}).join('');
        }} else {{
          snapDiv.innerHTML = '<div class="error">' + esc(snapshots.error || 'No snapshots found') + '</div>';
        }}
        document.getElementById('updated').textContent = 'Updated: ' + new Date().toLocaleString();
      }} catch (error) {{
        console.error('Failed to refresh:', error);
      }}
    }}
    refresh();
    setInterval(refresh, 60000);
  </script>
</body>
</html>
"""


class BackupStatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_html(PAGE)
        elif self.path == "/health":
            self._send_json({"status": "ok"})
        elif self.path == "/api/timers":
            self._send_json(get_systemd_timers())
        elif self.path == "/api/snapshots":
            self._send_json(get_restic_snapshots())
        elif self.path == "/api/stats":
            self._send_json(get_restic_stats())
        elif self.path == "/api/disk":
            self._send_json(get_disk_usage())
        elif self.path == "/api/logs":
            self._send_json(get_recent_logs())
        elif self.path == "/api/failed":
            self._send_json(get_failed_units())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args), flush=True)

    def _send_html(self, body: str):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), BackupStatusHandler)
    print(f"Serving backup status on {HOST}:{PORT}", flush=True)
    server.serve_forever()