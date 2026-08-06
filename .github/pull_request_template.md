## What changed and why

<!-- One paragraph. What breaks if this is wrong? -->

## Before merging

Run these against **Railway**, not just locally. Every one has caught something
real in this repository.

```powershell
python -m scripts.postdeploy_verify --target railway --app-url https://orbitcrm-production.up.railway.app/health
python -m scripts.migrate --check --target railway
python -m app.core.dsar --coverage
```

- [ ] `postdeploy_verify` exits 0 — secrets, DB invariants, red team, DSAR
      coverage, runtime-DDL audit, schema drift.
      *Without `--app-url` the red team judges the admin connection this check
      uses rather than the app's role, and reports an expected breach.*
- [ ] `migrate --check --target railway` is clean.
      *Applying SQL in pgAdmin updates the database and not the ledger. If you
      applied by hand, run `migrate --reapply-changed --target railway` —
      the files are idempotent, so it re-runs and records the checksum.*
- [ ] `dsar --coverage` exits 0.
      *A new table with a `contact_id`/`account_id`/`email` column makes every
      Art. 15 export silently narrower until it is declared in the manifest.*
- [ ] **Railway variables reviewed** if this PR adds a `release_guard` check.
      *A new BLOCKING check with no variable set will refuse to start the
      deployed app. The guard is doing its job; the deploy still fails.*

## Migrations

- [ ] No new migration, **or** it is added to `REQUIRED_MIGRATIONS` in
      `app/core/deploy_state.py` and applied with
      `python -m scripts.migrate --target railway`
- [ ] No **edits to an already-applied migration**. In production an applied
      migration is immutable — a change is a new file.

## After Railway redeploys

```powershell
(Invoke-RestMethod https://orbitcrm-production.up.railway.app/health) |
    Select-Object status, @{n='ha';e={$_.ha.role}}, @{n='lock';e={$_.ha.lock_held}},
                  @{n='db';e={$_.database.connected_as}}
```

- [ ] `status` healthy, `db` = **`crm_app`** (not `postgres` — that would mean
      the privilege separation is off)
- [ ] `ha.role` = `leader` on exactly one process, `lock_held` = `true`
- [ ] `scheduler.last_tick` moves within a few hours
      *A fresh deploy resets it to null; that is expected, not a fault.*

---

<sub>This checklist is markdown for GitHub, not a shell script. The fenced
commands are the runnable part.</sub>
