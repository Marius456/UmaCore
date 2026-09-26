# Ubuntu PostgreSQL operations

## Verified deployment — 2026-09-25

Migration completed on `158.180.28.120`. All 14 public tables were preserved,
including 3 clubs, 115 members, and 2,028 quota-history rows. Source and restored
row fingerprints, constraints, extensions, and sequence values matched.
An authenticated application-role read/write check passed with the write rolled
back. The `/list_clubs` handler was exercised against the live database with
Discord transport mocked; actual Discord login and command synchronization were
confirmed from the running bot's logs.

A backup was restored and compared in full with the stopped production database.
Database-container recreation preserved every checked value, and a replacement
bot container reached Discord readiness. The daily backup timer is enabled and
its first service-run backup succeeded. The original Neon bot container is stopped
with restart policy `no`. No PostgreSQL host port is published.

The deployment-script changes are in the local working tree; use that updated
script for future deployments and commit/push it before relying on a fresh checkout.

The production bot uses a separately managed PostgreSQL 17 container,
`umacore-postgres`, on Docker network `umacore-db`. No database port is published.
The named volume `umacore-postgres-data` survives bot deployments and database
container replacement. Never remove this volume during cleanup.

The dashboard is excluded from this migration. A dashboard using Neon will not
see new bot data after cutover. Coordinate any other database writers before
exporting. Quiet session checks cannot detect an external client that connects
only occasionally.

## Installation and migration

Copy `scripts/postgres-admin.py` to `/home/ubuntu/umacore-postgres-admin.py`.
Run commands as `ubuntu`, who must have Docker access:

```bash
python3 ~/umacore-postgres-admin.py prepare
python3 ~/umacore-postgres-admin.py cutover
```

Preparation refuses existing database resources, checks the source major
version, creates credentials and persistent storage, and compares a trial
restore against the source. Cutover stops the bot, rejects other source
sessions, exports again, compares full-row hashes/counts, constraints, extensions,
and sequence state, then changes `~/UmaCore/.env` and replaces the bot container
using its existing image. These commands are deliberately not blindly rerunnable.
Inspect failures and existing resources before retrying. No empty-database fallback
is provided if Neon cannot be reached.

Protected state, credentials, source exports, validation manifests, and diagnostic
errors live in `~/.local/share/umacore-postgres/` (directory 0700, files 0600).
Never commit or paste their contents. `last-error.log` can contain sensitive data.
The application role owns its database but has no superuser, role creation, or
database creation privilege. PostgreSQL uses 128 MB shared buffers, 4 MB work memory,
30 connections, and 32 MB maintenance work memory.

Use the updated `scripts/deploy.ps1` / `deploy-remote.sh` for future deployments:
they attach new bot containers to the database network and leave storage alone.
Deployment and admin operations share a lock. A previous deployment script without
network support must not be used after migration.

## Backups and restore drills

Install the provided service and timer under `/etc/systemd/system/`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now umacore-postgres-backup.timer
sudo systemctl start umacore-postgres-backup.service
systemctl list-timers umacore-postgres-backup.timer
journalctl -u umacore-postgres-backup.service
python3 ~/umacore-postgres-admin.py verify-backup
```

Backups run daily around 03:15 UTC and after missed runs. Compressed custom-format
archives retain seven days; expiration happens only after a new archive passes
its table-of-contents check. The original migration exports remain retained.
`verify-backup` restores the latest archive into `umacore_verify`, records its
manifest, and drops that temporary database on success. A failed restore leaves
it available for investigation and refuses to overwrite it on retry.

To test volume persistence during maintenance:

```bash
docker stop para-bot-container
python3 ~/umacore-postgres-admin.py recreate
docker start para-bot-container
```

For disaster recovery, stop the bot and preserve the existing volume first.
Restore a selected archive into a **new** database owned by `umacore`:

```bash
docker exec umacore-postgres createdb -U postgres -O umacore umacore_recovered
docker exec -i umacore-postgres pg_restore -U postgres -d umacore_recovered \
  --role=umacore --no-owner --no-acl --exit-on-error --single-transaction \
  < ~/.local/share/umacore-postgres/backup-TIMESTAMP.dump
```

Validate recovered rows, constraints, and sequences before changing only the
database name in the protected `DATABASE_URL` and recreating the bot via the
updated deploy script. Merely restarting an existing container does not reload
its environment. Keep the previous database until recovery is accepted.

Backups are server-local: loss of the server can lose both database and backups.
Check timer failures in the journal and disk capacity regularly.

## Rollback

Before cutover, failures restart the original bot. After cutover starts, there
is deliberately no automatic source rollback: bot startup can already write data.
`para-bot-container-neon-retired` retains the old environment, is stopped, and has
automatic restart disabled. `original.env` preserves the old connection settings.

Only if no new writes occurred and Neon is accessible, stop the new bot, retain
its container under another name, restore `original.env` to `~/UmaCore/.env` with
mode 0600, rename the retired bot back to `para-bot-container`, restore its restart
policy, and start it. After any new writes, keep the bot stopped and reconcile or
export the local database back to the recovery target before switching. Never
run both bot instances together.

## Acceptance checks

Check PostgreSQL health, authenticated application-role reads and a rolled-back
write, bot logs for database initialization and Discord readiness, and a Discord
read command. The HTTP health endpoint alone does not prove database availability.
Run a backup restore drill and the volume persistence check, then verify a bot
container replacement keeps connecting successfully.
