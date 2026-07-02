# Credential Rotation Checklist (post 2026-06-10 compromise)

Status audit (2026-07-02): `.env` is gitignored and was **never committed** —
git history is clean. Loose credential text files in the repo root are now
gitignored too. The credentials below are still the pre-compromise values and
must be rotated manually (only you have console access).

Work through this list top-to-bottom. After each rotation, update the value in
**both** places: local `d:\a\crm_agent\.env` AND the Railway service env vars,
then restart the local server / redeploy Railway.

## 1. Railway PostgreSQL password  🔴 highest priority
- Railway dashboard → Postgres service → Settings → reset credentials
  (or `railway variables` / recreate the service user).
- Update: `.env` → `RAILWAY_DB_PASS`, `RAILWAY_DB_URL`; Railway backend service
  auto-injects `DATABASE_URL` (verify it reflects the new password).
- Also update any saved connection in pgAdmin/psql scripts.

## 2. cPanel API token (agentorc.ca)
- cPanel → Security → Manage API Tokens → revoke the old token, create new.
- Update: `.env` → `CPANEL_TOKEN`. Used by `scripts/upload_*.py` + `deploy_html.ps1`.

## 3. OpenAI API key
- platform.openai.com → API keys → revoke + recreate.
- Update: `.env` → `OPENAI_API_KEY` and Railway env var of the same name.

## 4. Azure Speech key
- Azure Portal → the Speech resource → Keys → Regenerate Key 1.
- Update: `.env` → `AZURE_SPEECH_KEY` and Railway.

## 5. Email account password (info@agentorc.ca)
- cPanel → Email Accounts → change password.
- Update: `.env` → `EMAIL_PASSWORD` (SMTP + IMAP poller) and Railway.

## 6. ADMIN_API_TOKEN
- Generate a fresh value (PowerShell):
  `-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 48 | % {[char]$_})`
- Update: `.env` and Railway. Anything that calls the admin endpoints
  (scratch test scripts, monitoring) must use the new value.

## 7. NEW — UNSUBSCRIBE_SECRET (CASL consent feature)
- Not a rotation — a new secret introduced by `app/core/consent.py`. Generate a
  long random string the same way and set it in `.env` + Railway. Unsubscribe
  links are HMAC-signed with it; changing it later invalidates old links
  (already-suppressed addresses stay suppressed).

## After rotating everything
- [ ] Restart local server; run one agent query to confirm OpenAI works.
- [ ] Redeploy Railway; check `/orchestrator-health`.
- [ ] Delete the loose credential files from the repo root once their contents
      are stored in a password manager: `authentication.txt`,
      `docs/raillway password and url.txt`, `docs/railway upload.txt`,
      `45b23d9a1f8e4c7b8d2a6c9e3f1b4d8a.txt` (all now gitignored as a safety net).
