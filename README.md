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

In `Last Audit Verification`, restic rows show snapshot evidence such as snapshot count and latest snapshot time. Raw mirror rows show local and remote file counts/sizes.

Logs are parsed from journal lines into readable time, level, and message fields. The log API returns both raw `lines` and parsed `entries`.

## Healthchecks Role

Healthchecks is deployed at <https://healthchecks.erielcruz.com> as the standard
heartbeat monitor for scheduled jobs. It answers a simpler operational question:
did each backup, sync, audit, or maintenance job check in on time and finish?

The Backup Status dashboard should stay focused on backup-specific evidence that
Healthchecks does not replace:

- local drive capacity and remaining space
- restic snapshot presence, counts, and latest snapshot times
- raw mirror parity by file count and byte size
- recent relevant logs from the backup and sync units
- topology details such as which data is expected on SSD, 4TB, B2, and Hetzner

Possible future simplification: after every important systemd job pings
Healthchecks on start, success, and failure, this app can stop trying to be the
primary schedule monitor. It can instead link to Healthchecks for heartbeat
status and keep only the backup evidence checks that are unique to this setup.
That would reduce custom code around timers, last/next run display, and generic
service health while preserving the checks needed to prove backups are complete.

## Data Sources

- Live systemd user units through `/run/user/1000/bus`
- Journal logs for the backup/sync services
- Audit state from `/state/audit-latest.json`
- Restic snapshot metadata from the active repos
- Rclone `size --json` for raw mirror count and size checks
- `df -h` for Samsung SSD, 4TB, and Pictures source capacity

The first page render does not wait for every remote provider. It loads immediately from live systemd state and the last audit, then refreshes restic and rclone parity checks in the background. Slow providers show `LOADING`, `UNKNOWN`, or the provider error instead of blocking the dashboard.

Mirror parity checks are persisted in `/state/mirror-checks-latest.json`. The web app reuses that saved result when no relevant sync or audit service has completed since the check. A full `rclone size` comparison only starts when the saved result is missing or a newer sync/audit run exists. This keeps page loads cheap and avoids re-counting remote files just because the dashboard was opened.

Loading the site does not start `backup-audit.service`. The dashboard only reads `/state/audit-latest.json`; scheduled/chained backup services run the audit.

Some services are chained instead of timer-driven. For those rows, `Next Run` shows the upstream trigger, for example `after secrets`, instead of a blank timer value. If systemd does not expose a timestamp for a completed oneshot service, the dashboard derives `Last Run` from the latest journal `Finished`/`Failed` line.

Hetzner Pictures can be slow to list with `rclone size`. The sync service can succeed while the dashboard's live count check times out, because counting has to enumerate the remote tree for display. Normal mirror checks use short timeouts, but Hetzner Live Pictures gets a longer 300-second window before it is marked unavailable. The audit's `Pictures` result is combined (`~/Pictures` plus SSD `Photos` and `Videos`), so it is not used as a fallback for the narrower Live Pictures row. A Live Pictures timeout is shown as an unavailable live check, not as a file mismatch.

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
- `/home/eriel/Documents/home_server_ops/home_server_backup/state:/state`
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
| `GET /api/clear-cache` | Clear in-memory dashboard cache and saved mirror parity cache |
