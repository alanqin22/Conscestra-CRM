# Agent Cooperation — Railway Rollout Runbook

One place to take the 5-phase agent-cooperation stack live on Railway. Everything
is **off by default**; this is a deliberate, gated, one-flag-at-a-time sequence.
Per the project convention, **SQL is applied to Railway manually** (never
`deploy_sp.ps1`); the Python backend ships via the normal Railway deploy of
`master`.

Pre-flight (already verified): Railway has the base event bus
(`events`, `event_queue`, `event_subscriptions`, `event_types`, `emit_event`,
`trg_fn_events_after_insert`), the 12 agent service accounts, `fn_score_lead`,
`leads.score`, and `accounting_invoice_pipeline`. No base bus SQL needed.

---

## 🚀 CUTOVER 2026-07-10 — the complete go-live sequence (do these in order)

Everything from both improvement rounds is on `master` and locally proven.
This list converts it into a running 24/7 system. Steps marked 🧑 are yours
(console/dashboard actions); the rest is copy-paste.

1. 🧑 **Rotate the Twilio auth token** (Console → Account → API keys & tokens →
   request secondary token → promote). It was exposed during setup. Update
   `TWILIO_AUTH_TOKEN` in local `.env` — and use the NEW value in step 3.
2. **Deploy the backend**: Railway deploys `master` (latest commits carry the
   objectives/critic/tuning/playbooks/knowledge/scoring/telephony/SDR stack +
   durable SDR sessions). Inert without flags — safe to deploy first.
3. 🧑 **Railway env vars** — add alongside the existing flags:
   ```
   OBJECTIVES_ENABLED=1          TUNING_PROPOSALS_ENABLED=1
   KB_DRAFT_ENABLED=1            SCORING_TRAIN_ENABLED=1
   SDR_CHAT_ENABLED=1            SDR_VOICE_ENABLED=1
   TWILIO_ACCOUNT_SID=ACxxxx     TWILIO_AUTH_TOKEN=<rotated>
   TWILIO_FROM_NUMBER=+16593997878
   SMS_AUTOSEND=1                AGENT_BUS_CATCHALL=1
   FORWARDED_ALLOW_IPS=*         # uvicorn behind Railway's proxy: real
                                 # client scheme/IP for Twilio signatures
                                 # and SDR rate limiting
   ```
   (OBJECTIVES_AUTOACT / GOV_ACT_MIN tuning etc. stay at their staged pace.)
4. **Apply SQL steps 8–15** in one paste:
   `psql "$RAILWAY_DB_URL" -f sql/railway_cutover_2026_07.sql`
5. **Drain the stale-build backlog** (the pre-existing item): the deploy in
   step 2 carries the catch-all; run `POST /agent-bus/drain` (admin) and watch
   pending settle.
6. 🧑 **Repoint the Twilio number** (Console → Active numbers → +1 659 399 7878):
   - Voice "A call comes in" → `https://<railway-app>/sdr/voice/inbound` (POST)
   - Messaging "A message comes in" → `https://<railway-app>/telephony/sms/inbound` (POST)
   (These currently point at the temporary trycloudflare tunnel.)
7. 🧑 **Publish the HTML** you deploy yourself: `index.html` (advanced-round
   narrative) and `store-home.html` (SDR chat widget) to agentorc.ca.
8. **Smoke test** (5 minutes): `GET /health` → `GET /sdr/status`,
   `/telephony/status`, `/objectives/list`, `/tuning/status`,
   `/knowledge/list`, `/scoring/status`, `/a2a/capabilities` (34) → then call
   and text the number, and send one web-chat message from the store page.
9. **Watch for a week**: `/governance/queue` (critic-annotated approvals →
   your inbox), supervisor briefings, `/learning/performance`. The nightly/
   weekly jobs begin producing on their own schedule — calibration-driven
   tuning proposals become meaningful ~2026-08-06.

---

## ✅ STATUS — verified 2026-07-07 (read-only DB probe + local smoke)

**Done already:**

- **Step 2 SQL: 100% applied on Railway.** All 14 checks pass — double-enqueue
  fix (`UNIQUE(event_uuid)`, 0 dup rows), `event_types.queue_enabled` gate,
  pilot event types + `fn_emit_*`, invoice self-resolve trigger,
  `activity.overdue_flagged` / `order.status_changed` / `supervisor.alert`
  catalog rows, `agent_blackboard`, `action_approvals`, `email_suppression`
  (CASL), leads enrichment columns. Probe script:
  `scratchpad railway_readiness_probe.py` (SELECT-only).
- **Stage A/B are live and working in prod:** last 14 days show 350 dunning
  drafts + 49 hot-lead outreach activities, 33 `supervisor.alert` events (latest
  same-day), and live blackboard notes (`ar_risk` 18, `hot_lead` 7).
- **Local stack proof:** `scratch/smoke_all_phases.py` → **12/12 PASS**
  (Phases 1–5; 23 A2A capabilities). Script now sends the admin bearer
  (`ADMIN_API_TOKEN`/`SMOKE_TOKEN`) since the security hardening.

**Remaining gap — the deployed build is STALE (predates the 4 newest handlers
+ catch-all):** 235 pending `event_queue` rows never attempted:

| type | rows | pattern |
|---|---|---|
| `opportunity.stage_changed` | 88 | **accruing now** (~20/day) — needs catch-all |
| `invoice_paid` | 73 | stopped 2026-06-24 — stale, handler post-dates deploy |
| `order.status_changed` | 73 | stopped 2026-06-24 — same |
| `lead.created` | 1 | same |

**Next actions (in order):**

1. **Redeploy `master`** on Railway (carries all 6 bespoke handlers, catch-all,
   notif-triage/retention, pipeline hygiene, CEO briefing — all inert without flags).
2. Set **`AGENT_BUS_CATCHALL=1`** so `opportunity.stage_changed` settles instead
   of accruing.
3. **`POST /agent-bus/drain`** (admin token) to settle the 235-row backlog —
   boot-cutoff bypass; capped + idempotent; re-run until drained.
4. Confirm flag stages below: A/B appear ON; verify C (`GOV_ENABLED`) is set
   **before** ever flipping D/E (`action_approvals` is empty — consistent with
   C not yet on, or no medium-confidence writes yet).
5. Anomaly to check post-deploy: event_queue holds **zero completed rows** even
   though drafts are created daily — either `EVENT_RETENTION_DAYS` is aggressive
   on Railway or a manual purge swept settled rows. `GET /agent-bus/status` +
   the Step 5 queue query should show completions after the redeploy; if not,
   investigate before Stage D.

---

## Step 1 — Deploy the backend

Railway deploys `master` (commits `9ee1297 → e1a5a91`). With no env flags set the
code is **inert** — consumers don't run, gates no-op. Safe to deploy anytime.

## Step 2 — Apply SQL on Railway (in this order, all idempotent)

```bash
psql "$RAILWAY_DB_URL" -f sql/fix_event_queue_double_enqueue.sql   # 1 dedupe + UNIQUE(event_uuid) + ON CONFLICT
psql "$RAILWAY_DB_URL" -f sql/agent_bus_pilot.sql                  # 2 event types, subscriptions, fn_emit_*
psql "$RAILWAY_DB_URL" -f tri_fn/trgfn_invoice_after.sql           # 3 overdue events self-resolve on payment
psql "$RAILWAY_DB_URL" -f sql/resolve_stale_overdue_events.sql     # 4 clear Railway's stale invoice_overdue backlog
psql "$RAILWAY_DB_URL" -f sql/supervisor.sql                       # 5 supervisor.alert type + subscriptions
psql "$RAILWAY_DB_URL" -f sql/blackboard.sql                       # 6 agent_blackboard table
psql "$RAILWAY_DB_URL" -f sql/governance.sql                       # 7 action_approvals table
psql "$RAILWAY_DB_URL" -f sql/business_objectives.sql              # 8 Phase 8 goal-oriented supervisor
                                                                   #   (AFTER account_intelligence.sql — churn seed reads it)
psql "$RAILWAY_DB_URL" -f sql/governance_critic.sql                # 9 critic columns on action_approvals
psql "$RAILWAY_DB_URL" -f sql/agent_tuning.sql                     # 10 governed model parameters (learning write-side)
psql "$RAILWAY_DB_URL" -f sql/agent_playbooks.sql                  # 11 playbooks-as-data (cadences ship as rows)
psql "$RAILWAY_DB_URL" -f sql/knowledge_base.sql                   # 12 knowledge base (service knowledge loop)
psql "$RAILWAY_DB_URL" -f sql/lead_scoring_model.sql               # 13 predictive lead-scoring model store
psql "$RAILWAY_DB_URL" -f sql/telephony.sql                        # 14 sms.received event type + subscriptions
psql "$RAILWAY_DB_URL" -f sql/sdr_sessions.sql                     # 15 durable SDR sessions + rate limiting
psql "$RAILWAY_DB_URL" -f sql/llm_usage.sql                        # 16 LLM usage metering (fuel gauge)
```

**One-paste alternative for steps 8–16:** `sql/railway_cutover_2026_07.sql`
(the nine migrations concatenated in order; idempotent, verified re-runnable).

LLM metering is on by default (kill `LLM_METER_ENABLED=0`); budgets are OFF
until you set `LLM_DAILY_TOKEN_BUDGET` (per-caller default) or
`LLM_BUDGET_<CALLER>` (e.g. `LLM_BUDGET_SDR=50000`) — over-budget callers
degrade to their deterministic fallbacks, never crash. `LLM_MODEL_LITE`
routes SDR/auto-reply/SMS wording to a cheaper model. Monitor at
`GET /llm/usage`; the CEO briefing carries the daily spend line.

Each ends with a verify `SELECT`. After this, **A2A (Phase 2) and the blackboard
(Phase 4) are fully live** (no flags) — `/a2a/capabilities`, `/a2a/dispatch`,
`/blackboard/*` work, and agents will write blackboard notes once the bus runs.

## Step 3 — Flip env flags, ONE stage at a time

Set in Railway env; watch each stage for a few days before the next. **Never turn
on an `AUTOSEND`/`AUTOACT` before `GOV_ENABLED`.**

| Stage | Set | Effect | Watch for |
|---|---|---|---|
| **A** | `AGENT_BUS_ENABLED=1` (`AGENT_BUS_AUTOSEND=0`) | Consumer runs; nightly emitters fire; Accounting **drafts** dunning + Activity schedules outreach. **No emails sent.** | dunning/outreach activities created; queue draining; no errors |
| **B** | `SUPERVISOR_ENABLED=1` (`SUPERVISOR_AUTOACT=0`) | Supervisor tick (9/12/15/18 ET) **alerts** on KPI breaches. No agent loops kicked. | `supervisor.alert` events + briefings look right |
| **C** | `GOV_ENABLED=1` | Write-action confidence-gating active (high→act, medium→queue, low→skip). | `/governance/queue` for proposed actions |
| **D** | `AGENT_BUS_AUTOSEND=1` | Dunning emails actually send — now governed by confidence. | sent vs. queued; approvals; customer replies |
| **E** | `SUPERVISOR_AUTOACT=1` | Supervisor also kicks owning-agent loops on breach (governed). | auto-actions vs. proposals |

Tunables (optional): `AGENT_BUS_OVERDUE_MAX` (nightly dunning cap, code constant,
default 25), `GOV_ACT_MIN`/`GOV_PROPOSE_MIN` (0.8/0.5), `SUPERVISOR_*_MIN`
thresholds.

Later additions, same one-at-a-time discipline (each has its own APPLY/dry-run
gate): `AGENT_BUS_CATCHALL=1` (settle unhandled event types — required to stop
`opportunity.stage_changed` accrual), `NOTIF_TRIAGE_ENABLED=1` →
`NOTIF_TRIAGE_APPLY=1` (alert-backlog sweep + retention),
`PIPELINE_HYGIENE_ENABLED=1` → `PIPELINE_HYGIENE_APPLY=1` (stale-deal cleanup),
`CEO_BRIEFING_ENABLED=1` + `CEO_BRIEFING_EMAIL` (daily 08:00 ET brief),
`OBJECTIVES_ENABLED=1` → later `OBJECTIVES_AUTOACT=1` (Phase 8 goal-oriented
supervisor: alerts on at-risk objectives first; AUTOACT lets off-track
objectives run their governed play — turn on only after GOV_ENABLED),
`TUNING_PROPOSALS_ENABLED=1` (weekly calibration→tuning proposals — queues
governed `tuning.adjust` approvals only, parameters change on approval;
meaningful once account_intelligence_history snapshots are ≥30 days old),
`KB_DRAFT_ENABLED=1` (nightly 23:00 ET knowledge mining: resolved support
threads → LLM-drafted `kb.publish` proposals — publishes only on approval;
KB retrieval into the auto-reply is on by default, kill `KB_RAG_ENABLED=0`),
`SCORING_TRAIN_ENABLED=1` (weekly Mon 23:30 ET predictive lead-scoring
training — trains a candidate on settled leads and PROPOSES activation via
`scoring.activate`; refuses honestly until ≥30 settled with ≥5 of each
outcome; a model can never activate itself),
`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/`TWILIO_FROM_NUMBER` +
`SMS_AUTOSEND=1` (telephony channel: outbound SMS/agent voice calls really
send — otherwise everything drafts as owner tasks; trial accounts only reach
verified numbers). To receive inbound SMS on Railway, point the Twilio
number's messaging webhook at `POST https://<railway-app>/telephony/sms/inbound`
(signature-validated; replies use the KB-grounded auto-reply,
`SMS_AUTOREPLY=0` for plain acks).

## Step 4 — Smoke test on Railway

```
GET  /agent-bus/status          # enabled + running, handlers loaded
GET  /a2a/capabilities          # 27 capabilities (incl. objectives.report)
GET  /supervisor/status         # detectors + thresholds
POST /supervisor/run-once       # breaches + briefing (+ 🎯 objectives section)
GET  /governance/status         # gate thresholds
GET  /governance/queue          # pending approvals (each with its critic critique)
POST /governance/critique       # backfill critic onto pending approvals without one
GET  /objectives/list           # objectives vs targets (value/expected/status/trend)
POST /objectives/run-once       # forced objectives pass
GET  /tuning/status             # governed params: value/default/bounds/last change
POST /tuning/propose            # forced calibration→proposal pass
GET  /sequences/playbooks       # all playbooks resolved (code + db, registries)
PUT  /sequences/playbooks/{x}   # add/override a cadence as data (validated)
GET  /context/{type}/{id}       # context-hydration pack + rendered block
GET  /knowledge/list            # KB articles (uses counter = which knowledge earns its keep)
POST /knowledge/draft-pass      # forced mining pass (resolved threads → proposals)
GET  /booking/availability      # free business-hour slots for an owner
POST /booking/book              # book a meeting (activity + signed .ics invite link)
POST /planner/plan              # bounded goal→plan (execute=true runs reads, queues writes)
GET  /scoring/status            # active model + latest candidate + metrics
POST /scoring/train             # forced train→propose pass
GET  /telephony/status          # Twilio config + autosend/autoreply posture
POST /telephony/sms/send        # admin SMS (agents go through A2A sms.send)
GET  /sdr/status                # SDR chat/voice gates + active sessions
POST /sdr/chat                  # PUBLIC prospect chat (gated + rate-limited)
```

Autonomous SDR (no SQL): `SDR_CHAT_ENABLED=1` turns on the public web chat
(per-IP rate limit `SDR_RATE_LIMIT`, deterministic capture state machine,
LLM wording grounded in the KB, leads created source=sdr_chat, meetings
booked via the booking engine); `SDR_VOICE_ENABLED=1` turns on conversational
voice — point the Twilio number's VOICE webhook at
`POST https://<railway-app>/sdr/voice/inbound` (signature-verified;
turn-based <Gather input="speech"> loop; media-streams is the future
upgrade). Both default OFF; the widget lives in store-home.html.

The bounded planner needs NO SQL and NO flag: plans are validated against the
capability registry (≤6 steps, ≤2 writes), reads execute deterministically,
and every write becomes a critic-reviewed governance approval — the plan's
goal + correlation id ride on the approvals for end-to-end audit.

PII minimization needs NO SQL and NO flag (on by default; kill
`PII_MASK_ENABLED=0`): customer-authored text and CRM context blocks are
masked (emails → `j***@domain`, phones/cards → last-4) before any LLM
prompt — auto-reply, knowledge mining, context injection. Operational agent
commands and user-typed chat are deliberately NOT masked (the agent needs
the field to act; sends stay gated by AUTOSEND/consent).

Meeting booking needs NO SQL and NO new flag: the booked meeting is an
internal activity; the invite email obeys the existing AUTOSEND + verified-
address gates (otherwise the owner gets a send-manually task with the signed
link). Cadence auto-booking kill switch: `BOOKING_AUTOBOOK=0`.

Context hydration ("born with context") needs NO SQL and NO flag — it is on by
default (read-only; `CONTEXT_HYDRATION_ENABLED=0` kills it instantly). It
auto-prepends the entity's compact 360 block to A2A NL dispatches and
personalizes the inbound auto-reply for known senders.
(`scratch/smoke_all_phases.py` is the local equivalent — point it at the Railway
URL for a full 1→5 check.)

## Step 5 — Daily monitoring (SQL)

```sql
-- Bus queue health (should drain; failed should stay ~0)
SELECT e.event_type, q.status, count(*)
FROM event_queue q JOIN events e ON e.event_uuid=q.event_uuid
WHERE e.source_system IN ('agent_bus','supervisor','crm')
GROUP BY 1,2 ORDER BY 1,2;

-- What the agents did
SELECT count(*) FILTER (WHERE subject ILIKE 'Payment reminder%') reminders,
       count(*) FILTER (WHERE subject ILIKE 'Hot lead outreach%') outreach
FROM activities WHERE created_at > now()-interval '1 day';

-- Supervisor alerts (last 24h)
SELECT payload->'context'->>'rule' rule, count(*)
FROM events WHERE event_type='supervisor.alert' AND created_at>now()-interval '1 day'
GROUP BY 1;

-- Shared context being written
SELECT topic, author_agent, count(*) FROM agent_blackboard
WHERE expires_at IS NULL OR expires_at>now() GROUP BY 1,2 ORDER BY 3 DESC;

-- Approval queue (anything stuck pending?)
SELECT action_type, count(*) FILTER (WHERE status='pending') pending,
       count(*) FILTER (WHERE status='executed') executed,
       count(*) FILTER (WHERE status='rejected') rejected
FROM action_approvals GROUP BY 1;

-- Invariants: no duplicate queue rows; overdue backlog stays small
SELECT count(*)-count(DISTINCT event_uuid) AS dup_queue_rows FROM event_queue;
SELECT count(*) FROM event_queue q JOIN events e ON e.event_uuid=q.event_uuid
WHERE q.status='pending' AND e.event_type='invoice_overdue';
```

## Rollback

- **Instant & safe:** set the stage's flag back to `0` — the code goes inert
  immediately (consumers stop, gates no-op). No redeploy needed.
- **Code:** redeploy the previous `master` commit.
- **SQL:** the schema is additive (tables/functions/constraint). The two one-way
  data passes — the `fix_event_queue_double_enqueue` dedupe and the
  `resolve_stale_overdue_events` backfill — only removed duplicate/stale rows and
  are safe to leave in place.

## Reference

Per-phase detail: `docs/agent_bus_phase{1,2,3,4,5}.md`. Architecture + recipes:
the `agent-bus` skill. Full local smoke: `scratch/smoke_all_phases.py`.
