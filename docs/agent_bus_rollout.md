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
```

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
`CEO_BRIEFING_ENABLED=1` + `CEO_BRIEFING_EMAIL` (daily 08:00 ET brief).

## Step 4 — Smoke test on Railway

```
GET  /agent-bus/status          # enabled + running, handlers loaded
GET  /a2a/capabilities          # 23 capabilities
GET  /supervisor/status         # detectors + thresholds
POST /supervisor/run-once       # breaches + briefing
GET  /governance/status         # gate thresholds
GET  /governance/queue          # pending approvals
```
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
