# Railway Enablement — Agent Bus, Supervisor & Consent (July 2026)

The agentic features (event bus, proactive supervisor, governance) are fully
built and already running locally — on Railway they are OFF because the env
flags were never set there. This is the exact, safe order to enable them.
All steps are Railway-dashboard env changes + one redeploy; no code changes.

## Prerequisite (do first)
Rotate credentials per `docs/security_rotation_checklist.md` — especially the
Railway DB password and `ADMIN_API_TOKEN` — BEFORE turning on autonomous
features in production.

## Step 1 — deploy the current backend + SQL
1. Push the backend to git → Railway auto-builds (`ddgs`, `trafilatura` install
   from requirements.txt).
2. Apply `sql/consent_casl.sql` to the Railway DB **manually** (psql against
   RAILWAY_DB_URL — never deploy_sp.ps1). Also confirm the agent-bus/supervisor
   tables exist there (`sql/agent_bus_pilot.sql`, `sql/supervisor.sql`,
   `sql/blackboard.sql`, `sql/security_hardening.sql` if not already applied).

## Step 2 — set these Railway env vars (safe posture: drafts only, no sends)
```
AGENT_BUS_ENABLED=1        # event bus consumer + nightly emitters
SUPERVISOR_ENABLED=1       # KPI-breach detectors, 4× weekday tick
GOV_ENABLED=1              # confidence-gating of write actions
AGENT_BUS_AUTOSEND=0       # keep OFF on first deploy — dunning stays draft+log
                           # (LOCAL is already =1; chain proven end-to-end 2026-07-02:
                           #  handler → A2A → governance ACT → SMTP send + audit)
SUPERVISOR_AUTOACT=0       # keep OFF — supervisor alerts only
UNSUBSCRIBE_SECRET=<new long random value — different from local>
APP_URL=https://orbitcrm-production.up.railway.app   # unsubscribe links resolve here
```
The CASL sender identity (company name + mailing address) is managed in the
**Company Profile card on executives-mgmt.html** (stored in `company_profile`,
`sql/company_profile.sql`). Set it there after deploying; the
`BUSINESS_MAILING_ADDRESS` env var is only a fallback for a fresh DB.
```
```

## Step 3 — verify (with X-Admin-Token header)
- `GET /agent-bus/status`  → enabled true, consumer running
- `GET /supervisor/status` → enabled true
- `GET /consent/status?email=test@example.com` → `{"suppressed": false}` (public)
- Watch a day of `invoice.dunning_drafted` events: drafts logged as completed
  activities, no emails sent.

## Step 4 — later, governed autosend (the real differentiator)
Only after: (a) credentials rotated, (b) consent live in prod, (c) a week of
clean draft-only operation:
```
AGENT_BUS_AUTOSEND=1
```
Sends are then triple-gated: recipient must be verified & deliverable
(`_is_real_email`), not on the suppression list, and the write passes the
governance confidence gate (medium-confidence actions queue for approval at
`GET /governance/queue`). `SUPERVISOR_AUTOACT=1` is the final flip, same rules.
