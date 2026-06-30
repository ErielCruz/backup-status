# Backup Status Dashboard

Web dashboard at https://backup.erielcruz.com showing backup and sync job health.

## What it shows

- **Health bar** — at-a-glance summary: job failures, backup runs count, total data stored, verification pass rate
- **Jobs table** — 11 backup/sync jobs with status (pass/fail), last run, next scheduled run
- **Disk usage** — Samsung SSD and 4TB drive capacity with color-coded usage bars (blue <60%, amber 60-80%, red >80%)
- **Run inventory** — all backup runs on disk with file count, total size, duration, and verification status (pass/fail/unknown)
- **Verification history** — runs that have verify.log showing checksum and decrypt smoke test results

## How it works

The Flask + HTMX app scans the backup root filesystem (`/samsung/Backup_SSD/HomeServerBackups/runs/`) for backup runs, reads `backup-report.json` and `verify.log` files, and computes aggregated stats. Job status comes from a JSON file written by a systemd collector. HTMX polls every 60 seconds for live updates.

Results are cached in-memory with configurable TTL to avoid excessive filesystem reads.

## Running locally

```bash
cd /home/eriel/Documents/repos/backup-status
docker compose -f /home/eriel/Documents/backup_docker/stacks/backup-status/compose.yaml up -d --build
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard page |
| `GET /health` | Health check `{"status":"ok"}` |
| `GET /api/refresh` | HTMX partial update (auto-polled every 60s) |
| `GET /api/clear-cache` | Clear in-memory cache |
