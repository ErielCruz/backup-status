# Backup Status Dashboard

Web dashboard at <https://backup.erielcruz.com> for the home server backup system.

## What It Answers

- Are backup and sync runs healthy right now?
- Which unit last ran, when did it run, and when is it scheduled next?
- What are the latest useful logs when something is failing or running?
- Do raw mirrors match by file count and byte size?
- Do restic snapshot repos have recent snapshots?
- How full are the local source drives?

## Current Backup Model

The app matches the current backup design:

- System/container backups are encrypted restic snapshots in SSD, 4TB, B2, and Hetzner repos.
- Secrets are encrypted restic snapshots in SSD, 4TB, B2, and Hetzner repos.
- Raw mirrors compare local source data against SSD, 4TB, B2, and Hetzner where applicable.
- `Long_Term_Backup` is the exception: source is 4TB and backup is Hetzner only.

Restic repos are not compared by raw file size because restic is deduplicated and encrypted. The dashboard checks snapshot availability and latest snapshot time instead.

## Data Sources

- Live systemd user units through `/run/user/1000/bus`
- Journal logs for the backup/sync services
- Audit state from `/state/audit-latest.json`
- Restic snapshot metadata from the active repos
- Rclone `size --json` for raw mirror count and size checks
- `df -h` for Samsung SSD, 4TB, and Pictures source capacity

The first page render does not wait for every remote provider. It loads immediately from live systemd state and the last audit, then refreshes restic and rclone parity checks in the background. Slow providers show `LOADING`, `UNKNOWN`, or the provider error instead of blocking the dashboard.

Loading the site does not start `backup-audit.service`. The dashboard only reads `/state/audit-latest.json`; scheduled/chained backup services run the audit.

Some services are chained instead of timer-driven. For those rows, `Next Run` shows the upstream trigger, for example `after secrets`, instead of a blank timer value. If systemd does not expose a timestamp for a completed oneshot service, the dashboard derives `Last Run` from the latest journal `Finished`/`Failed` line.

Hetzner Pictures can be slow to list with `rclone size`. If the live size check times out, the dashboard falls back to the last audit value and marks that row as an audit-backed value instead of a confirmed live check.

## Deployment

The running container is defined outside this repo:

```bash
/home/eriel/Documents/backup_docker/stacks/backup-status/compose.yaml
```

Required mounts:

- `/home/eriel/Pictures:/pictures:ro`
- `/home/eriel/Samsung_750:/samsung:ro`
- `/home/eriel/4TB:/4tb:ro`
- `/home/eriel/Documents/home_server_ops/home_server_backup/logs:/logs:ro`
- `/home/eriel/Documents/home_server_ops/home_server_backup/state:/state:ro`
- `/home/eriel/.config/rclone:/root/.config/rclone:ro`
- `/run/user/1000:/run/user/1000:ro`
- `/var/log/journal:/var/log/journal:ro`

Rebuild and restart:

```bash
cd /home/eriel/Documents/backup_docker/stacks/backup-status
docker compose up -d --build
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard page |
| `GET /health` | Health check |
| `GET /api/refresh` | HTMX dashboard refresh |
| `GET /api/logs/<unit>` | Recent journal lines for an allowed backup/sync unit |
| `POST /api/trigger/<unit>` | Start an allowed backup/sync unit |
| `GET /api/clear-cache` | Clear in-memory dashboard cache |
