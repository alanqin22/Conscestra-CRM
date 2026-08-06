# Runbook — Restore the production database

**Scope:** recovering `railway` (production PostgreSQL 18) from a dump.
**Owner:** Alan Y. Qin. **Last exercised:** 2026-08-05, successfully.

---

## 0. Facts you can rely on

These are measured, not estimated. Re-measure if the database grows materially.

| | Value | How it was obtained |
|---|---|---|
| **RPO** | 24 h, or the length of the last machine-off gap | daily task at 23:00 with catch-up enabled |
| **Restore into an EMPTY database** | **3.5 s** | nightly drill, many runs |
| **Restore OVER a populated database** | **6.8 s** | rehearsal 2026-08-05 — ~2× the empty case, and this is the case an incident has |
| **Re-grant after restore** | **0.9 s** | mandatory every time, see §4a |
| **Database recovery subtotal** | **7.7 s** | plus provisioning, app restart, verification |
| **Restore INTO Railway over the network** | **65.2 s** | rehearsed 2026-08-05 into a scratch database on the production instance — **18× the local figure** |
| Volume headroom during that restore | peak 235 MB of 1024 MB | measured; production untouched, 131 accounts intact after |
| Dump size / duration | 19.2 MB, ~208 s | 2026-08-05 run |
| Dumps retained | 14 (`BACKUP_KEEP`) | `backups\railway-*.dump` |

> **Use the 65 s figure, not the 3.5 s one.** The local numbers measure a dump
> being read off a local disk into a container on the same machine. A real
> restore pushes 18 MB across the internet into Railway, and that costs **65
> seconds, not 3.5** — the drill was measuring the fastest possible case and
> reporting it as the recovery time.
>
> Even 65 s is not "time to service". Add app restart, verification, and the
> mandatory re-grant in §4a — a step this runbook previously recorded as
> conditional when it is in fact required on every restore. Budget **5 minutes**
> for a clean recovery and treat anything faster as luck.

## 1. Prerequisites

- **Docker Desktop running.** The tooling runs `pgvector/pgvector:pg18` because
  the schema uses the `vector` type; a plain `postgres:18` image restores 223 of
  224 tables and reports success. Version-matched too: `pg_dump` refuses a
  server newer than itself.
- `RAILWAY_DB_URL` in `.env` (the `postgres` superuser, not `crm_app` — a
  restore recreates objects the app role may not own).
- Working directory `D:\a\crm_agent`.

## 2. Choose a dump

```powershell
Get-ChildItem D:\a\crm_agent\backups\railway-*.dump |
    Sort-Object LastWriteTime -Descending |
    Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}, LastWriteTime
```

Filenames are `railway-YYYYMMDD-HHMMSS.dump` in **local time**. Cross-check
against `backups\backup.log`, which records what each run verified — a dump
whose log entry does not end in `verified: every checked table matches
production` was never confirmed good.

## 3. Verify the dump before trusting it (non-destructive, ~4 min)

Never restore a dump you have not just proved restorable.

```powershell
cd D:\a\crm_agent
python -m scripts.backup_railway
```

This takes a *fresh* dump, restores it into a throwaway container, and compares
row counts for **all 200 tables** against production. To validate an *older*
dump instead, restore it by hand:

```powershell
docker run -d --name pgrestore -e POSTGRES_PASSWORD=drill -p 55432:5432 pgvector/pgvector:pg18
# wait for two successful TCP connections a second apart, NOT pg_isready --
# initdb runs a temporary server that answers and then restarts.
docker exec pgrestore createdb -U postgres check_db          # check the exit code
docker cp D:\a\crm_agent\backups\railway-XXXX.dump pgrestore:/tmp/d.dump
docker exec pgrestore pg_restore -U postgres -d check_db --no-owner --no-acl -j 4 /tmp/d.dump
docker exec pgrestore psql -U postgres -d check_db -c "\
  SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace \
   WHERE n.nspname='public' AND c.relkind='r';"
```

Expect **145** public tables. Verified totals as of 2026-08-05, Railway
PostgreSQL 18.4:

| Schema | Tables | |
|---|---|---|
| `public` | 145 | the application |
| `n8n_legacy` | 59 | retired 2026-08-05, kept read-only pending deletion |
| `auth` / `storage` / `realtime` | 31 | platform schemas |
| **total** | **235** | |

A restore that produces 145 in `public` and loses `n8n_legacy` is acceptable.
One that produces fewer than 145 is not — find the missing table before going
any further.

`pg_restore` exits non-zero for *warnings* as well as errors — missing roles and
extensions it cannot create are normal. **Do not read exit code 0 as success or
non-zero as failure.** Count the tables; that is the signal.

```powershell
docker rm -f pgrestore
```

## 4. Restore into production — UNEXERCISED

> Destructive and untested. Everything above this line has been run many times;
> nothing below it has been run once. Read it as a plan to follow carefully,
> not a script to trust.

**Before touching production:**

1. Take a dump of the current broken state. A corrupted database is still
   evidence, and you will want it if the restore makes things worse.
2. Stop the application so nothing writes during the restore — in Railway,
   scale the app service to 0 replicas.
3. Decide explicitly what you are giving up: everything written since the dump.
   Check `backups\backup.log` for the dump's timestamp and state that window
   out loud before proceeding.

```powershell
# 1. Preserve the current state first.
$env:BACKUP_KEEP=0; python -m scripts.backup_railway

# 2. Restore. --clean --if-exists drops each object before recreating it.
docker run --rm -v D:\a\crm_agent\backups:/b pgvector/pgvector:pg18 `
  pg_restore --no-owner --no-acl --clean --if-exists -j 4 `
  -d "$env:RAILWAY_DB_URL" /b/railway-XXXX.dump
```

**After the restore, before restarting the app:**

```powershell
python -m scripts.postdeploy_verify --target railway --app-url https://<app>/health
python -m scripts.verify_invariants
python -m app.core.dsar --coverage
```

### 4a. MANDATORY — re-grant before restarting the app

**Rehearsed 2026-08-05. This is not a check, it is a required step.**

An earlier version of this runbook said "if the grants are gone, re-apply
`sql/app_role.sql`". The rehearsal showed there is no *if*. `--no-owner
--no-acl` strips privileges from the archive, and `--clean` drops the tables the
existing grants were attached to. Measured, restoring over a populated database:

| | before restore | after restore |
|---|---|---|
| `crm_app` role exists | yes | yes (roles are cluster-level, they survive) |
| `SELECT` on `accounts` | **true** | **false** |
| `CREATE` on `public` | false | false |

So the role survives and every one of its privileges does not. **The application
is down at this point, on every restore, without exception.** The dangerous part
is not the outage — it is that the obvious fix under pressure is to point the
app at `postgres` and move on, which silently deletes the entire privilege
separation while appearing to resolve the incident.

Run this **before** scaling the app back up (measured: 0.9s):

```sql
GRANT USAGE ON SCHEMA public TO crm_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO crm_app;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO crm_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO crm_app;
REVOKE CREATE ON SCHEMA public FROM crm_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO crm_app;
```

(This is `sql/app_role.sql` minus the role creation, which is unnecessary — the
role is still there.) Then verify, and do not accept the privilege bits alone as
proof — issue a real query as the role:

```sql
SELECT has_table_privilege('crm_app','public.accounts','SELECT');  -- true
SELECT has_schema_privilege('crm_app','public','CREATE');          -- FALSE
SET ROLE crm_app; SELECT count(*) FROM accounts; RESET ROLE;       -- must work
```

`ALTER DEFAULT PRIVILEGES` matters as much as the grants: without it, the next
migration creates tables `crm_app` cannot read, and that failure appears days
later with no connection to the restore.

### 4b. Then bring the app back

Scale the app back up and confirm `/health` returns 200 with
`database.connected_as` = `crm_app`. **If it reports `postgres`, the separation
is off** — that is a security incident, not a successful recovery.

```powershell
python -m scripts.postdeploy_verify --target railway --app-url https://<app>/health
python -m scripts.verify_invariants
python -m app.core.dsar --coverage
python -m scripts.verify_runtime_ddl
```

## 5. What has actually gone wrong here

Every one of these was found by running the drill, not by reviewing it.

| Symptom | Cause | Now handled by |
|---|---|---|
| RTO printed for a restore that never happened | `createdb`/`pg_restore` exit codes unchecked; `pg_isready` answered by initdb's temporary server | return codes checked; two TCP probes a second apart |
| 223 of 224 tables restored, check passed | ten-table sample; `items` uses `vector`, absent from plain `postgres:18` | all tables compared; pgvector image pinned |
| Count mismatch with nothing wrong | counts read *after* a 208 s dump while indexing continued | counts taken **before** the dump, matching `pg_dump`'s snapshot |

## 6. If the restore fails

- **`pg_restore: error: could not execute query` on extensions** — usually
  benign. Confirm with the table count, not the exit code.
- **`type "vector" does not exist`** — wrong image. Use `pgvector/pgvector:pg18`.
- **`server version mismatch`** — `pg_dump` will not dump a newer server. The
  image major version must be ≥ Railway's (`SHOW server_version;`).
- **Restore completes, app still down** — the database is probably fine. Check
  `/health` for `connected_as` and the scheduler heartbeat before restoring
  again; a rolling-deploy leader race once looked exactly like a data problem
  and cost ten days.

## 7. Gaps in this runbook

Stated rather than left to be discovered mid-incident:

- **§4 has still never been executed against the LIVE database.** What is now
  rehearsed (2026-08-05):
  - `--clean --if-exists` over a populated PG18 database, same image and flags
    (`python -m scripts.rehearse_restore`)
  - the mandatory re-grant, verified by a real query as `crm_app`
  - a full restore **into Railway over the network**, into a scratch database
    on the production instance — 65.2 s, 200 tables, volume peak 235 MB of
    1024 MB, production untouched

  What remains unproven is the irreversible part: dropping and recreating the
  objects the live application is using. That cannot be rehearsed without doing
  it. Everything leading up to it now has measured evidence.

- **Volume headroom.** 1 GB volume, ~250 MB used. Free space is not observable
  from SQL — read it from the Railway dashboard (click `postgres-volume` →
  Metrics) before any restore that writes to that volume. This project has
  already had a volume fill and crash-loop recovery.

- **Point-in-time recovery is a PLAN UPGRADE, not a platform limitation.**
  An earlier version of this runbook and of `scripts/backup_railway.py` stated
  that Railway's managed Postgres cannot do PITR, reasoning from `archive_mode`
  being `context=postmaster`. That reasoning was about the wrong thing: Railway
  offers Backups and PITR **on the Pro plan** (Postgres service → Backups tab).
  Closing the 24-hour RPO is therefore a billing decision, not an engineering
  project. Correcting this here because the original claim would have sent
  someone to re-platform to solve a problem a plan change solves.
- **No point-in-time recovery.** Daily dumps only; anything between them is
  lost. WAL archiving would close this and is not configured.
- **Single copy.** `backups\` lives on one machine. A drive failure takes the
  dumps and the working tree together. Copying the folder to external or cloud
  storage is the cheapest remaining DR improvement and needs no code.
- **No automated restore alarm.** A daily run that stops running is currently
  noticed by a human reading the log.
