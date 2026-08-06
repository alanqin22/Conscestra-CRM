# Runbook — Credential rotation

**Last verified: 2026-08-05.** Originally written after the 2026-06-10
compromise as a to-do list. Most of it is now done, so it has been rewritten as
a runbook — a list of pending work that is actually complete is worse than no
list, because the next reader either repeats finished work or stops trusting the
document.

---

## Current state

```powershell
python -m app.core.secret_health      # exit 0 = all guarded secrets present, strong, distinct
```

As of 2026-08-05 this reports **ok, no weak values, no shared values**. It prints
fingerprints only, never values, so its output is safe to paste into a ticket.

| Secret | Rotated | Note |
|---|---|---|
| `RAILWAY_DB_PASS` / `RAILWAY_DB_URL` | **2026-08-03** | was exposed in an assistant transcript |
| `RAILWAY_ADMIN_API_TOKEN` | **2026-08-03** | same exposure |
| `MEMORY_SIGNING_KEY` | **2026-08-03** | had been a dev placeholder in production |
| `ADMIN_API_TOKEN` | pre-existing, 48 chars | verified strong |
| `GOV_LINK_SECRET` | pre-existing, 64 chars | verified strong |
| `UNSUBSCRIBE_SECRET` | **status unconfirmed** | rotating invalidates old unsubscribe links; suppressed addresses stay suppressed |
| `OPENAI_API_KEY`, `AZURE_SPEECH_KEY`, `EMAIL_PASSWORD`, `CPANEL_TOKEN` | not rotated since 2026-06-10 | see below |

**`/orchestrator-health` does not exist.** The old version of this document told
you to check it after rotating. Use `/health` and `secret_health`.

## Loose credential files — checked 2026-08-05

All four still exist in the working tree:

```
authentication.txt                              2578 B
docs/raillway password and url.txt               226 B
docs/railway upload.txt                          856 B
45b23d9a1f8e4c7b8d2a6c9e3f1b4d8a.txt              33 B
```

They are gitignored and were never committed. Each was tested against every
current secret value: **none contains a live credential** — they hold
pre-rotation values that no longer authenticate anything.

That makes them low risk rather than no risk. They are still plaintext
credential files inside a working tree that gets copied, zipped and backed up.
Delete them once their contents are in a password manager.

## Rotating a secret

For each one, in this order:

1. Rotate at the provider (below).
2. Update `d:\a\crm_agent\.env`.
3. Update the Railway service variable of the same name.
4. Redeploy Railway; restart the local server.
5. Verify: `python -m app.core.secret_health` then
   `python -m scripts.postdeploy_verify --target railway --app-url https://<app>/health`

**Do not skip step 3.** A secret rotated locally and not on Railway fails only
in production, and usually only on the code path that uses it — which may be
days later.

| Secret | Where |
|---|---|
| Railway PostgreSQL | Railway → Postgres service → Settings → reset credentials. Railway auto-injects `DATABASE_URL`; confirm it reflects the new password. Update saved pgAdmin connections too |
| `CPANEL_TOKEN` | cPanel → Security → Manage API Tokens → revoke, create new |
| `OPENAI_API_KEY` | platform.openai.com → API keys → revoke + recreate |
| `AZURE_SPEECH_KEY` | Azure Portal → Speech resource → Keys → Regenerate Key 1 |
| `EMAIL_PASSWORD` | cPanel → Email Accounts → change password (SMTP + IMAP poller) |
| `ADMIN_API_TOKEN`, `GOV_LINK_SECRET`, `UNSUBSCRIBE_SECRET` | generate locally: `-join ((48..57)+(65..90)+(97..122) \| Get-Random -Count 48 \| % {[char]$_})` |

`MEMORY_SIGNING_KEY` rotates differently — it is a **keyring**, not a single
value. Move the current key into `MEMORY_SIGNING_KEYS_OLD` as `id:secret` (comma
separated, verify-only) and set the new key with a new `MEMORY_SIGNING_KEY_ID`.
Replacing it outright invalidates every existing signature.

## Also rotate when

- Anyone leaves, or a shared machine is decommissioned.
- A secret appears in a transcript, screenshot, log or support ticket — this is
  how the 2026-08-03 rotation was triggered.
- After restoring a database backup, **verify** rather than rotate: `--no-owner
  --no-acl` strips grants, so confirm `crm_app` still exists with the right
  privileges. See [runbook_restore.md](runbook_restore.md) §4.

## What rotation does not protect against

The database password is no longer the only thing between an attacker and the
data — the app connects as `crm_app`, which owns nothing and cannot CREATE, so
holding the app's credentials does not grant the ability to disable
database-layer controls. Check with `/health`'s `database.connected_as`: if it
reports `postgres`, the separation is off and rotation is not the problem.

## Gaps

- **No KMS.** Secrets live in `.env` and Railway variables. There is no central
  store, no automatic expiry, no access log for who read what.
- **Signing is symmetric.** `MEMORY_SIGNING_KEY` is HMAC — anyone who can verify
  a signature can forge one. A third party cannot audit the trail without also
  gaining the ability to write it.
- **No rotation schedule.** Rotation is event-driven. Four provider keys have
  not been rotated since the 2026-06-10 compromise.
