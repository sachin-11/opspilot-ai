# Runbook: backup and restore

Scope: the PostgreSQL system of record and the files that go with it. Commands are PowerShell, run from the repository root.

## What holds what

| Data | Where | In `backup.ps1`? | How it is protected |
| --- | --- | --- | --- |
| Incidents, investigations, users, memberships, audit log, knowledge index (pgvector), LangGraph checkpoints | PostgreSQL | **Yes** | `scripts/backup.ps1` |
| Uploaded knowledge documents | `knowledge_data` volume | No | Volume archive (below) |
| Queue jobs, rate-limit counters, WebSocket tickets, Pub/Sub | Redis | No | Not a system of record; safe to lose. In-flight investigations are recovered by the worker at startup. |
| Webhook secrets (encrypted) | PostgreSQL | Yes, but **unreadable without `OBS_SECRET_ENCRYPTION_KEY`** | Keep the key in your secrets manager |
| Audit-log integrity | PostgreSQL | Yes, but **unverifiable without `AUTH_AUDIT_KEY`** | Keep the key in your secrets manager |
| Configuration and secrets (`.env`) | Host | No | Secrets manager. Never put them in a backup. |

A restored database is only usable together with the same `OBS_SECRET_ENCRYPTION_KEY` and `AUTH_AUDIT_KEY`, the GitHub App credentials, and the OIDC settings it was running with. Back those up through your secrets manager, separately from the data.

## Targets (set your own, then measure them)

- **RPO** (data you can lose) equals the interval between backups. A nightly backup means up to 24 hours. For a lower RPO run `backup.ps1` more often, or use point-in-time recovery on managed PostgreSQL (recommended for production; it makes the script a second line of defence).
- **RTO** (time to recover) is the restore drill duration plus starting the services. Time a drill and record the number here.

## Taking a backup

```powershell
scripts\backup.ps1
```

This writes `backups\opspilot-<timestamp>.dump` and a `.sha256` file. It lists the archive before finishing, so an unreadable backup fails now rather than during an incident. Then:

1. **Copy it off the machine** (object storage, another host). A backup on the same disk is not a backup.
2. **Encrypt it at rest.** It contains customer data and the audit log. Use storage-level encryption or encrypt before upload.
3. **Schedule it** (Task Scheduler on Windows, cron elsewhere) and keep a retention policy for the backups themselves, for example 7 daily and 4 weekly.

Uploaded knowledge documents (volume name is `<compose project>_knowledge_data`, shown by `docker volume ls`):

```powershell
docker run --rm -v opspilot-ai_knowledge_data:/data:ro -v "${PWD}/backups:/out" alpine tar czf "/out/knowledge-$(Get-Date -Format yyyyMMdd-HHmmss).tgz" -C /data .
```

## Restore drill (do this regularly, at least monthly)

```powershell
scripts\restore.ps1 -File backups\opspilot-<timestamp>.dump -Verify
```

It checks the checksum, restores into a temporary database, confirms an `alembic_version` row exists, prints row counts for key tables, and drops the temporary database. The live database is never touched. A backup that has never been restored is a hope, not a backup.

## Recovering from a disaster

1. **Decide the point to restore to** and fetch that backup plus the knowledge archive from your off-machine copy.
2. **Stop everything that writes**: `docker compose stop api worker raw-consumer normalized-consumer web`.
3. **Inspect first (optional but cheap).** `scripts\restore.ps1 -File <dump>` restores into a new `<database>_restore_<timestamp>` database next to the live one so you can look before committing.
4. **Replace the live database:**

   ```powershell
   scripts\restore.ps1 -File backups\opspilot-<timestamp>.dump -ReplaceLive -ConfirmDatabaseName <database>
   ```

   It refuses without the exact database name, and refuses while any session is still connected.
5. **Restore the knowledge files** into the volume (`tar xzf` into `/data`, mirroring the backup command), then bring the app up on the **same version** that wrote the backup, or run `docker compose exec api alembic upgrade head` if the app is newer.
6. **Start the services** and check: `/health/ready` is 200, sign in, open a recent incident, run `python -m opspilot_api.auth.audit_verify` (it needs `AUTH_AUDIT_KEY`), and confirm the pipeline accepts a signed webhook.
7. **Write down** what was lost (everything after the backup time) and tell the affected users.

## Limits to know about

- `pg_dump` is a logical, point-in-time snapshot of one database; it does not replace continuous archiving.
- A restore creates the data again but not its **secrets**; see the table above.
- `-ReplaceLive` drops the database. It is meant for recovery, after the safe steps above, and never for routine use.
