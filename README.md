# Backup Status Dashboard

Web dashboard at https://backup.erielcruz.com showing backup and sync job health.

## What it shows

- **Jobs table** — which backup/sync jobs ran, their status (pass/fail), last run, next scheduled run
- **Disk usage** — Samsung SSD and 4TB drive capacity with color-coded usage bars
- **Snapshot count** — encrypted restic snapshots stored in Backblaze B2

## How it works

A host-side collector runs every 2 minutes via systemd timer, queries systemd for job status, and writes JSON to a shared volume. The Flask app reads this and renders the dashboard. HTMX polls every 60 seconds for live updates.

## Backup strategy (simple version)

Your data is protected by two separate systems:

| System | Tool | What | Encrypted? |
|---|---|---|---|
| **File sync** | rclone | Photos, Videos, documents | No — raw copy |
| **Snapshots** | restic | System state, DB dumps, secrets | Yes — passphrase |

- **Photos are safe even if the passphrase is lost.** They live in raw folders synced by rclone to local disk, B2, and Hetzner.
- **The passphrase only protects restic data** (container configs, database dumps, `.secret` files).
- Both Immich and Photoprism databases are backed up twice: once raw via rclone, once encrypted via restic.

## Running locally

```bash
cd /home/eriel/Documents/repos/backup-status
docker compose -f /home/eriel/Documents/backup_docker/stacks/backup-status/compose.yaml up -d
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard page |
| `GET /health` | Health check |
| `GET /api/refresh` | HTMX partial update |
