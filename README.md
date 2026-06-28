# backup-status

Simple self-hosted web dashboard for backup monitoring.

## Features

- `GET /` renders backup status dashboard (auto-refresh each 60s)
- `GET /health` returns app health
- `GET /api/timers` shows systemd timer status
- `GET /api/snapshots` shows restic snapshots
- `GET /api/stats` shows restic repository stats
- `GET /api/disk` shows disk usage on backup drives
- `GET /api/logs` shows recent backup logs
- `GET /api/failed` shows failed systemd units

## Environment

- `BACKUP_STATUS_HOST` default `0.0.0.0`
- `BACKUP_STATUS_PORT` default `5000`
- `RESTIC_REPOSITORY` restic repository URL (e.g. `b2:backup-ssd-ecc:restic-backups`)
- `RESTIC_PASSWORD_FILE` path to restic password file
- `SAMSUNG_MOUNT` mount path for Samsung SSD stats (default `/samsung`)
- `TB_MOUNT` mount path for 4TB drive stats (default `/4tb`)

## Local Run

```bash
docker build -t backup-status .
docker run --rm -p 5000:5000 \
  -v /var/run/systemd:/var/run/systemd:ro \
  -v /home/eriel/Samsung_750:/samsung:ro \
  -v /home/eriel/4TB:/4tb:ro \
  -v /home/eriel/Documents/home_server_ops/home_server_backup/config/backup.passphrase:/run/secrets/restic_password:ro \
  -e RESTIC_REPOSITORY=b2:backup-ssd-ecc:restic-backups \
  -e RESTIC_PASSWORD_FILE=/run/secrets/restic_password \
  backup-status
```