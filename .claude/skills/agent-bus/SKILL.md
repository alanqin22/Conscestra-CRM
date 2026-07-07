---
name: agent-bus
description: Playbook + roadmap for Conscestra CRM's event-driven agent-cooperation bus (the 5-phase plan to make all AI agents communicate/cooperate automatically). Use when working on agent_bus.py, adding a new event→agent handler, wiring emitters into the scheduler, or deciding the next phase. Tracks current status (Phase 1 built, off by default).
---

# Conscestra CRM — Agent Cooperation Bus (Phases 1–5)

How to make all the AI agents communicate and cooperate **actively, automatically,
and safely** — by activating the latent DB event bus and growing it phase by phase.
This skill is the durable plan + status + the recipe for adding cooperations.

## Where we are (update this section as phases land)

| Phase | Goal | Status |
|---|---|---|
| **1 — Activate the event bus** | A consumer that reacts to events and dispatches agents to act | **✅ Built & LIVE on Railway** (verified 2026-07-07: daily dunning drafts + outreach + supervisor alerts + blackboard notes in prod). Railway SQL 100% current; **deployed build is stale** (predates the 4 newest handlers + catch-all) → 235 pending rows accruing. Fix: redeploy `master` + `AGENT_BUS_CATCHALL=1` + `POST /agent-bus/drain`. See `docs/agent_bus_rollout.md` STATUS section |
| **1b — Orchestrator catch-all** | Settle the ~92% of event types with no bespoke handler | ✅ **Built 2026-07-02** — `handle_default` in agent_bus.py, gated `AGENT_BUS_CATCHALL` (=1 locally): claim drops the type filter; unhandled events settle as **reacted** (meaningful moments → blackboard signal on entity + owning account), **observed** (CRUD echoes → `recent_activity` last-touch upsert), or **acked** (lineage types — loop safety). Deterministic, no LLM per event; marks in `error_context`. Backlog: 11,203 undeliverable rows purged (`sql/purge_event_queue_backlog.sql` — run on Railway too). Boot cutoff still applies: catch-all handles NEW events, not pre-boot backlog. |
| **2 — A2A protocol** | Typed envelope + capability registry; route by capability | ✅ **Built** — `app/core/a2a.py`: envelope + registry (**21 caps**: 7 structured SP, 11 `<agent>.query` NL-passthroughs, 1 composite, write), `dispatch()`, **structured (non-NL) SP contract**, **peer handoff** (composite `delegate()`+compose, `hops`, correlation), **negotiation** (unknown intent → `suggestions`), `/a2a/*`, orchestrator `capabilities`/`route:`. **Single-agent delegation now A2A-routed** (`<agent>.query`, `routedVia`, `_call_agent` fallback). Write caps dry-run unless `confirm`. Polish left: confidence-gating/approval queue → Phase 5 |
| **3 — Proactive autonomy** | Standing supervisor tick; emergent cross-agent plays | 🟢 **Built** — `app/core/supervisor.py`: scheduled tick (every 3h, 9/12/15/18 ET Mon–Fri) reads `sp_orchestrator('executive')`, 4 detectors (ar_spike, slipped_deals, unbilled_orders, unworked_leads) → emits idempotent `supervisor.alert` events (fan out to Notifications+Orchestrator) + a briefing; `SUPERVISOR_AUTOACT=1` also kicks owning-agent loops (AR→dunning, leads→hot-lead). Gated on `SUPERVISOR_ENABLED`. `/supervisor/status` + `/supervisor/run-once`. SQL: `sql/supervisor.sql`. Next: richer detectors, per-breach A2A context |
| **4 — Shared blackboard memory** | Entity-keyed context agents post/read | 🟢 **Built** — `app/core/blackboard.py` + `agent_blackboard` table (`sql/blackboard.sql`): `post`/`read`/`context`/`clear`, upsert per (entity,author,topic), TTL. Agents coordinate via context: overdue handler respects a Sales `dunning_hold` (skips) + posts `ar_risk`; lead handler posts `hot_lead`. A2A `account.context` cap + `/blackboard/*` endpoints. Next: more posters/readers, surface in 360s |
| **5 — Governance** | Confidence-gate, approve/reverse, audit, rate-limit | 🟢 **Built** — `app/core/governance.py` + `action_approvals` table: `decide(confidence)` → act/propose/skip; A2A **write** dispatch gated by confidence (GOV_ENABLED) → high executes, medium queues for approval, low skips; `approve()` re-dispatches with `govern_bypass`; `reject()`; `/governance/{status,queue,approve,reject}`. Plus the earlier opt-in/autosend gates, idempotency, retry/backoff, audit. Next: gate supervisor AUTOACT, reversibility |
| **6 — Sequences (cadences)** | Multi-step TIMED playbooks — act, wait days, re-check, act again | 🟢 **Built 2026-07-07** — `app/core/sequences.py` + `agent_sequences` table (`sql/agent_sequences.sql`): durable per-instance state (playbook, step_no, next_step_at); 30-min emitter `fn_emit_sequence_step_events()` → `sequence.step_due` bus events → handler re-validates, **exits early** (lead engaged→completed, converted→completed, disqualified/deleted→cancelled), acts, advances. Pilot playbook `lead_followup` (4 steps: intro email draft +2h → reminder +3d → offer meeting +4d → nurture +7d); auto-started by `handle_lead_scored`. One active run per (playbook, entity) — partial unique index. Gated `SEQUENCES_ENABLED` (default 0). `/sequences/{status,list,start,cancel,run-once}`. E2E: `scratch/test_sequences.py` (20/20). Steps create internal drafts/tasks only — NO SMTP (sending stays Email-agent + AUTOSEND + governance). Next: dunning-escalation playbook, governance-gate step actions, surface active cadences in ai_summary |
| **7 — Customer intelligence** | Persistent "living profile" per customer — churn/preferences, not on-demand 360s | 🟢 **Built 2026-07-07** — `app/core/intelligence.py` + `account_intelligence` table (`sql/account_intelligence.sql`): nightly (22:35 ET, gated `INTEL_ENABLED`) deterministic scorer — churn risk = 0.55·lateness(vs OWN median order gap) + 0.25·engagement + 0.10·AR + 0.10·lost-deal, bands ≥0.7 high / ≥0.4 medium; plus RFM/LTV, preferred channel (modal, 12m, `system` excluded), expected next purchase. Explainable: components+weights persisted in `signals` jsonb. Consumers: accounts **ai_summary** fact sheet reads the profile row (best-effort) + high-band accounts get a `churn_risk` blackboard note (author `intelligence`, 48h TTL, self-expiring); **supervisor** `detect_churn_risk` (threshold `SUPERVISOR_CHURN_MIN`=5, tolerates missing table). `/intelligence/{status,at-risk,account/{id},run-once}`. E2E: `scratch/test_intelligence.py` (13/13 local AND Railway) + `test_intelligence_high.py` (5/5, forced high-band; NB order-insert triggers log activities at NOW — backdate them in synthetic tests). Railway table applied + 112 customers scored 2026-07-07 (all low — seeded data is uniformly fresh). **Marketing agent (same day):** `app/core/marketing.py` + `marketing_campaigns`/`marketing_sends` (`sql/marketing_campaigns.sql`): campaign = SEGMENT over accounts×account_intelligence (churn_band/min_ltv/industry/preferred_channel) + `{{first_name}}`-style template; recipients = segment accounts' contacts; launch enforces per-recipient CASL gates FOR REAL even in draft mode (suppression check; live sends additionally `_is_real_email` + `send_email(commercial=True)` footer); real email needs BOTH `AGENT_BUS_AUTOSEND=1` AND `?confirm=true`; UNIQUE(campaign,email) = no double-send; `/marketing/{campaigns,segment-preview,…/launch,…/results}`; results = sends by status + inbound replies + orders since launch. **Governance→executive routing (same day):** `sql/governance_routing.sql` adds amount/assigned_executive_id/assigned_to to action_approvals; `governance.route_approval()` (called by every `propose()`, best-effort) extracts the $ amount from params, picks the decision-maker by ROLE AFFINITY (invoice/payment→CFO, deal/discount/campaign→CRO, order/stock→COO, default CEO) then SMALLEST SUFFICIENT `approval_authority_limit` (NULL=unlimited; nobody sufficient→highest authority); emits `approval.routed` audit event + in_app notification (when employee_uuid) + email gated `GOV_ROUTE_EMAIL`; pending queue + CEO briefing ("Approvals Awaiting Your Decision") + per-role briefings (filtered to that exec) surface assignments. Tests: `test_marketing.py` 11/11, `test_gov_routing.py` 12/12. **v2 generate+optimize (2026-07-07):** `draft_campaign_content(segment, goal)` — LLM-drafted subject/subject_b/body (direct `json.loads` on a `\{.*\}` match — `parse_ai_json` does NOT parse plain fenced JSON) with deterministic template fallback; **A/B subjects** (`sql/marketing_ab.sql`: campaigns.subject_b + sends.variant) — 50/50 by list position, per-variant reply attribution + `leading` verdict in results; **agent-initiated campaigns within policy:** supervisor churn breach (`auto="churn_campaign"` under SUPERVISOR_AUTOACT) → `governance.propose('campaign.winback', conf 0.6)` (routes to CRO; idempotent: one pending proposal / one Win-back per 7d) → approval re-dispatches A2A `campaign.winback` (24th capability, `sp=marketing.winback_campaign_sp`) → create+launch, draft-only without AUTOSEND. NB: A2A `sp` handlers run BEFORE the governance gate in dispatch() — gating for winback happens at the supervisor propose step, not in dispatch. **Lead qualification (same day):** `app/core/qualification.py` — `win_probability(score)` = Laplace-smoothed conversion rate of settled leads in the score band; `recommend_rep(industry)` ranks active owners by industry accounts → open-lead load → total; `qualify()` card at `/qualification/lead/{id}`; `handle_lead_scored` embeds win% + rep in the hot_lead blackboard note. **Enrichment providers:** `LEADS_ENRICH_PROVIDER` = apollo | pdl (real adapters, need LEADS_ENRICH_API_KEY) | **web** (keyless — web_tools search, gap-fill-only so low confidence is safe) | generic (legacy URL). Test: `scratch/test_qual_marketing_v2.py` 15/15. GOTCHA: `config.py` does `load_dotenv(override=True)` — .env BEATS process env vars; tests must patch module attrs (e.g. `agent_bus.AUTOSEND=False`), not setenv. **churn_save play (7×6, built same day):** scorer starts a `churn_save` cadence when an account ENTERS high band (transition-triggered via prior-band map — persistently-high never replays; `start_sequences=` param): 1 support context-check (+1h: `churn_context` note + owner task) → 2 marketing win-back DRAFT on preferred channel (+2d, `winback_offer`) → 3 exec escalation (+5d, `churn_escalated`); exits early on new order (`won_back`), inbound touch (`re-engaged`), or band back to low (`risk_subsided`). Sequences engine generalized to account entities (`_LOADERS`, `_account_exit_reason_sync`, generic `_insert_activity_sync`). E2E: `scratch/test_churn_save.py` (16/16). **v2 learning loop (same day):** `sql/intelligence_v2.sql` adds soft attributes — `preferred_hour` (modal ET hour of INBOUND touches, 12m; cadences align multi-day steps to it via `sequences._aligned_step_at`, short waits untouched), `interests` (top-3 ordered categories via order_items→products→**category** table — table is `category`, singular), `sentiment_score/label` (90d avg from email_sentiment joined on contact email) — plus `account_intelligence_history` (1 row/account/day, written by every scoring pass). `intelligence.calibrate(horizon,window)` scores month-old predictions vs actual ordering (churned = no order in the 30d after the snapshot) → per-band churn rates + plain verdicts (raise HIGH_BAND / bands INVERTED); never auto-mutates weights. `app/core/learning.py` `agent_performance(days)` = cadence outcome mix + success rates (`_SUCCESS` map), campaign sends/replies/orders, calibration — `GET /learning/performance` + CEO briefing **"Agent Performance — Last 30 Days"** section (`_perf_lines`, best-effort). ai_summary shows interests / sentiment / engagement hour. E2E `scratch/test_learning.py` 14/14; Railway migrated, 112 profiles enriched, snapshots started 2026-07-07 (calibration meaningful ~30d later) |

**Recommended order:** (1) ship Phase 1 to prod cheaply → (2) Phase 3 supervisor
tick (the differentiator) → then Phases 2 & 4 (heavier architecture investments).

## The architecture (what already exists in the DB — reuse, don't rebuild)

```
emit_event(type, entity, id, payload)            event_types  = catalog (emit_event validates)
        │ INSERT                                  event_subscriptions = agent→event_type map
        ▼                                         (service accounts 0…01–0…12)
     events ──(AFTER INSERT: trg_fn_events_after_insert)──┐
        ├─▶ event_queue   (status, attempts, locked_by/at, next_attempt_at)  ← lockable retry queue
        └─▶ notifications (channel='agent_inbox', one row per subscribing agent)
```

Phase 1's consumer (`app/core/agent_bus.py`) is the piece that was missing: it
claims `event_queue` rows, routes by `event_type` to a Python handler, and
completes/retries. Handlers embody an agent acting, and may hand off to a peer by
emitting a follow-up event (proto-A2A).

## Key files

- `app/core/agent_bus.py` — consumer daemon, `HANDLERS` registry, both pilot
  handlers, `GET /agent-bus/status`, `POST /agent-bus/run-once`.
- `sql/agent_bus_pilot.sql` — event-type catalog rows, agent subscriptions,
  `fn_emit_overdue_invoice_events()`, `fn_emit_hot_lead_events()`.
- `app/main.py` — lifespan start/stop + nightly emitter jobs (22:25 / 22:30 ET).
- `docs/agent_bus_phase1.md` — full design + run/verify guide.
- `app/core/sequences.py` + `sql/agent_sequences.sql` — Phase 6 cadence engine
  (PLAYBOOKS registry, step handler, `/sequences/*`, 30-min emitter in main.py).
- `scratch/test_agent_bus_pilot.py`, `scratch/test_agent_bus_lead.py`,
  `scratch/test_sequences.py` — E2E proofs.

## Config (env) — safe by default

| Var | Default | Meaning |
|---|---|---|
| `AGENT_BUS_ENABLED` | `0` | master switch (daemon + nightly emitters both self-gate on it) |
| `AGENT_BUS_AUTOSEND` | `0` | `1` = real outbound (e.g. SMTP via Email agent); else draft+log |
| `AGENT_BUS_POLL_SECS` | `30` | consumer tick interval |
| `AGENT_BUS_BATCH` | `10` | max events per tick |
| `AGENT_BUS_MAX_ATTEMPTS` | `5` | retries before `status='failed'` |
| `AGENT_BUS_BACKFILL_MINUTES` | `0` | >0 = also process recent pre-boot events |
| `SUPERVISOR_ENABLED` | `0` | Phase 3 supervisor tick on/off |
| `SUPERVISOR_AUTOACT` | `0` | `1` = supervisor also kicks owning-agent loops on breach |
| `GOV_ENABLED` | `0` | Phase 5 confidence-gating of write actions on/off |
| `GOV_ACT_MIN` / `GOV_PROPOSE_MIN` | `0.8` / `0.5` | act / propose confidence thresholds |
| `NOTIF_TRIAGE_ENABLED` | `0` | Notification-triage sweep on/off |
| `NOTIF_TRIAGE_APPLY` | `0` | `1` = actually resolve/digest; else dry-run (report only) |
| `NOTIF_TRIAGE_CAP` | `5000` | max rows touched per pass per run |
| `SEQUENCES_ENABLED` | `0` | Phase 6 multi-step cadences on/off (`start()` no-ops + emitter skipped when 0) |
| `INTEL_ENABLED` | `0` | Phase 7 nightly customer-intelligence scoring on/off (`/intelligence/run-once` works regardless) |
| `SUPERVISOR_CHURN_MIN` | `5` | high-churn-band customers before the supervisor alerts |
| `GOV_ROUTE_EMAIL` | `0` | 1 = email the executive an approval was routed to (assignment + audit event always happen) |

## Live cooperations (handlers)

0. **Inbound bridge (2026-07-07):** every non-noreply inbound email →
   `app/agents/email/inbound_bridge.py` (called from `auto_reply.process_inbound_email`,
   INCLUDING rate-limited repeats) matches sender→contact(account)/lead, inserts a
   completed `direction='inbound'` activity (THE engagement signal: cadence
   engaged/re-engaged exits, campaign reply attribution, profile engagement
   recency all key off it — before this, inbound only hit audit_log and NONE of
   those fired in prod), and emits `email.received`
   (`sql/email_received_event.sql`, queue_enabled). Handler #7
   `handle_email_received`: complaint → blackboard `complaint` note (author
   `support`, read by churn_save context check) + urgent owner task (idempotent
   24h/sender); other intents acked. Dedupe: 1 touch per (entity,sender,subject)/1h.
   E2E: `scratch/test_inbound_bridge.py` (15/15 incl. inbound reply ending a live
   churn_save as re-engaged).
1. `invoice.overdue` → **Accounting** drafts tiered dunning + logs activity →
   emits `invoice.dunning_drafted` → **Email** inbox (real send only if AUTOSEND).
2. `lead.scored` (≥70) → **Activity** auto-schedules outreach call →
   emits `lead.outreach_scheduled` → **Notifications** inbox.
3. `activity.overdue_flagged` → **Activity** agent SURFACES *material* overdue work
   (linked to an open opportunity, a call/meeting, or `activity_score>15`) to the
   owner via an in-app nudge (`metadata.kind='activity_nudge'`) + posts an
   `overdue_activity` blackboard note. Deliberately the COMPLEMENT of the nightly
   `sp_activities_auto_sweep` (which snoozes only low-value tasks, score≤15) so
   important slipped work is never silently hidden. Idempotent per-activity (48h);
   does NOT mutate the activity. Trigger emits the source event only when
   `due_at<now()`, so a future reschedule emits harmless `activity.updated` (no
   loop). Triage Pass C auto-resolves the nudge once the activity is completed /
   brought current — closing the loop.

4. `lead.created` → **Leads** agent ENRICHES via an external data source — the
   first OUTWARD function call (IBM "agents use external tools"). `app/core/enrichment.py`
   `enrich_company()` is a deterministic stub by default; set `LEADS_ENRICH_API_URL`
   (+`LEADS_ENRICH_API_KEY`) and adapt `_call_api()` to a real provider
   (Clearbit/Apollo/PDL). The handler **gap-fills the lead's own fields**
   (`industry`/`website`/`employee_band`/`revenue_band` + `city`/`province` from
   hq — never overwrites; `enrichment.apply_to_lead()`, best-effort so the bus
   survives if `sql/leads_enrichment_columns.sql` isn't applied) AND posts a
   firmographics note to the blackboard (`lead/enrichment`, also the idempotency
   marker). Wired as A2A capability **`leads.enrich`** (params: `lead_id` or
   `company`/`email`/`domain`; `apply=true` writes the lead) — dispatchable
   (`route: leads.enrich lead_id=… apply=true`) and shown in `/a2a/capabilities`.
   Migration: `sql/leads_enrichment_columns.sql`.

### Draining the historical backlog (boot-cutoff bypass)

The consumer's boot cutoff means historical pending events aren't processed. Two
additive tools on the bus router handle that:

- **`drain_backlog(max_total, since_days)`** / `POST /agent-bus/drain` — capped,
  restartable drain of handler-type pending events (handlers re-validate +
  idempotency-guard, so stale events are safely skipped; `FOR UPDATE SKIP LOCKED`
  makes it concurrency-safe with the live loop). Re-run to continue.
- **`rollup_overdue_activities(apply)`** / `POST /agent-bus/rollup-overdue` — for
  the overdue-activity bulk, a per-OWNER rollup: ONE "N overdue items" summary per
  owner (`metadata.kind='overdue_rollup'`, grouped by subject with counts) instead
  of hundreds of per-activity nudges. Absorbs any per-activity nudges already
  raised and SETTLES the pending `activity.overdue_flagged` queue rows (+ their
  agent_inbox copies). Idempotent: one active rollup per owner (refreshes, never
  stacks). The per-activity handler stays for go-forward (low daily volume). Tests:
  `scratch/test_activity_overdue_handler.py` (6 checks, real activity, self-cleaning).

## Notification triage (alert-backlog control) — `app/core/notification_triage.py`

The structural fix for ever-growing **UNREAD ALERTS** (events fan out to ~12
agent inboxes + in_app but nothing resolved them → ≈7.8k unread). A scheduled
sweep (daily 21:55 ET, gated on `NOTIF_TRIAGE_ENABLED`, **dry-run unless
`NOTIF_TRIAGE_APPLY=1`**) — the Notifications/Orchestrator agent reading
non-critical alerts and taking positive action, replacing the manual
`sql/mark_old_notifications_read_8k.sql`. Three passes:

- **A — agent-inbox receipts:** mark read where the event_queue row is no longer
  pending OR no Python handler subscribes (agents' own mail; will never be
  actioned). Leaves the real bus worklist (pending + registered handler).
- **B — informational digest:** roll in-app FYI (`*.created`/`*.updated`,
  `account.updated`, `invoice_created`, `payment_created`, `invoice_paid`, …) into
  ONE digest notification per recipient per day (counts in `metadata.breakdown`,
  `kind='digest'`), then mark originals read. Digest anchors to one absorbed
  `event_uuid` (the column is NOT NULL) — no migration. Idempotent: re-runs fold
  into the same day's digest and never re-absorb it.
- **C — stale-actionable cleanup:** re-validate in-app ACTIONABLE alerts
  (`invoice.overdue`, `lead.scored`) against live entity state; resolve the ones
  that are paid / converted / disqualified; also resolve `activity_nudge` alerts
  once their activity is completed/closed or brought current; leave still-actionable
  + CRITICAL (`supervisor.alert`) unread. `classify()` +
  `ACTIONABLE_TYPES`/`CRITICAL_TYPES`; default tier = informational.

Endpoints `/notif-triage/{status,run-once}` (run-once defaults to dry-run;
`?apply=true` to resolve). Dry-run on the live backlog: **7,794 → ~460** (A 4,793,
B 2,454 across 9 users, C 93). E2E: `scratch/test_notif_triage.py` (8 checks incl.
idempotency, all on synthetic rows in a rolled-back txn — non-destructive).
**Next actionable cooperation:** an `activity.overdue_flagged` handler so the 2,587
overdue-activity flags get acted on (reschedule/nudge) instead of only digested.

## RECIPE — add a new cooperation (the whole point: purely additive)

No change to the consumer core. For `someevent → AgentX (+ handoff to AgentY)`:

1. **Register event type(s)** in `event_types` (emit_event validates against it).
   Add to `sql/agent_bus_pilot.sql` `INSERT … ON CONFLICT DO UPDATE is_active`.
2. **Subscribe agents** in `event_subscriptions` (service accounts `0…0N`,
   channel `agent_inbox`) — the source event for AgentX, the handoff event for AgentY.
3. **Write the handler** `async def handle_<event>(event)->dict` in `agent_bus.py`:
   load fresh context (don't trust payload), **re-check it's still actionable**,
   **idempotency guard** (skip if already actioned in a window), act (DB writes via
   `asyncio.to_thread`), emit a lineage-chained follow-up event for the handoff,
   return a result dict. Register `HANDLERS["<event>"] = handle_<event>`.
4. **(Optional) emitter** `fn_emit_<thing>_events(p_max)` for scheduled/manual
   firing — idempotent (one event / entity / 20h), capped. Wire a nightly job in
   `app/main.py` (self-gated on `agent_bus.ENABLED`).
5. **Test** an E2E script in `scratch/` mirroring the existing ones (autocommit
   helper, reset-at-start scoped to `source_system='agent_bus'`, assert
   claimed/handoff/idempotency). Clean up artifacts after.

## Gotchas / invariants

- **Double-enqueue: FIXED.** `emit_event()` + the events trigger used to create
  2 `event_queue` rows per event; `sql/fix_event_queue_double_enqueue.sql` added
  `UNIQUE(event_uuid)` + `ON CONFLICT DO NOTHING` on both enqueue paths → one row
  per event (batch counts are now intuitive). The consumer still keys by
  `event_uuid` defensively. Apply that migration on Railway too.
- **Root noise gate (2026-07-02):** `sql/event_queue_gate.sql` — `event_types.queue_enabled` gates §1A of `trg_fn_events_after_insert`; emit_event's redundant enqueue REMOVED (trigger is the only enqueue path now). Only the 14 consumable types (6 bespoke handlers + 8 catch-all REACT types) enter event_queue; everything else stays audit-only in `events`. Notification fan-out (§1B) unchanged. KEEP queue_enabled IN SYNC with HANDLERS + _REACTIONS; types emitted by direct-INSERT triggers must ALSO have an event_types catalog row or the gate drops them. Apply on Railway too.
- **Boot cutoff** protects against replaying historical backlog — only events at/
  after daemon start are processed. There are ~19k legacy pending rows; only
  handler-registered event types are ever touched (e.g. dotted `invoice.overdue`,
  NOT legacy underscore `invoice_overdue`).
- **Handlers must be idempotent** (re-check actionability + a recency guard) —
  events can be redelivered and emitters can re-fire.
- **emit_event() normalizes payloads to a canonical envelope** `{before, after,
  diff, context, meta}` — any OTHER top-level key is silently DROPPED. Business
  data must go under `'context'` (`jsonb_build_object('context', …)` in SQL,
  `{"context": {...}}` in Python) and be read back via `payload->'context'->>…`.
  This is also why handlers must load fresh entity state, never trust payload.
- **DB is psycopg2 (sync):** wrap DB calls in `asyncio.to_thread`; agent calls
  (`_call_agent`) are async. Local DSN: `postgresql://postgres:aria@localhost:5434/crmdb`.
- **Never run `deploy_sp.ps1` against Railway** — local target only; user deploys
  Railway SQL manually.
- **Overdue events self-resolve on settlement.** `trgfn_invoice_after` §4 closes
  out pending `invoice_overdue` / `invoice.overdue` queue rows + notifications when
  an invoice goes `paid`/`cancelled` (they previously lingered forever — ~96% of
  the old backlog were already-paid invoices). One-time backlog cleanup:
  `sql/resolve_stale_overdue_events.sql`. So a long `invoice_overdue` pending
  backlog is now a red flag, not normal.

## Phase roadmap detail

- **Phase 1 (done):** consumer + 2 handlers + nightly emitters. Remaining polish:
  LISTEN/NOTIFY for sub-second latency (currently 30s poll); turn ON in prod.
- **Phase 3 (built):** `app/core/supervisor.py` — scheduled tick reads
  `sp_orchestrator('executive')`, detectors (ar_spike / slipped_deals /
  unbilled_orders / unworked_leads) → idempotent `supervisor.alert` events +
  briefing; `SUPERVISOR_AUTOACT` kicks owning-agent loops. Gated, audited,
  `/supervisor/*` endpoints, `sql/supervisor.sql`. **Add a detector:** write
  `detect_x(pack)->Optional[signal]` and append to `DETECTORS`. Next: richer
  detectors, per-breach A2A drill-down context.
- **Phase 2 (started):** `app/core/a2a.py` — typed A2A envelope (`A2ARequest`/
  `A2AResult`: intent, entity_ref, params, correlation_id, confidence) + a
  capability `CAPABILITIES` registry (intent → owning agent, seeded from each
  agent's VALID_MODES). `dispatch(req)` resolves the capability and invokes the
  owning agent in-process; read vs write declared per capability; `dry_run`
  resolves without side effects. Discovery: `GET /a2a/capabilities`; ad-hoc
  call: `POST /a2a/dispatch`. Test: `scratch/test_a2a.py` (PASS). **Structured
  input contract:** a `Capability.sp` handler (`params → data` via the agent's
  sql_builder + SP, no NL/AI/HTTP) is used by default; `prose=True` forces the
  NL path. **Orchestrator capability routing:** `capabilities` lists the manifest;
  `route: <intent> [k=v ...]` dispatches by capability (write = dry-run unless
  `confirm`). **Peer handoff:** composite caps (`compose=`) `delegate()` to peers
  and compose, with a `hops` audit trail. **Negotiation:** unknown intent →
  `suggestions`. **Single-agent delegation is A2A-routed** via `<agent>.query`
  passthroughs (`A2AResult.raw` preserves the full response; `_call_agent`
  fallback). **Add a capability:** append `Capability(intent, agent, endpoint,
  kind, render, desc, sp=optional, compose=optional)` to `CAPABILITIES`.
- **Phase 4 (built):** `app/core/blackboard.py` + `agent_blackboard` table —
  entity-keyed observations agents `post()`/`read()`; upsert per
  (entity,author,topic), TTL via `expires_at`. Demo: a Sales `dunning_hold` note
  makes the overdue handler skip; Accounting posts `ar_risk`; A2A
  `account.context` reads it. **Add a poster/reader:** call `blackboard.post(...)`
  in a handler, `blackboard.read(entity_type, id, topic)` before acting.
- **Phase 5 (built):** `app/core/governance.py` + `action_approvals` table —
  `decide(confidence)` act/propose/skip; A2A **write** dispatch gated when
  `GOV_ENABLED` (high executes, medium → approval queue, low → skip); `approve()`
  re-dispatches with `govern_bypass`, `reject()` declines; `/governance/*`
  endpoints. **Tune:** `GOV_ACT_MIN`/`GOV_PROPOSE_MIN`. Next: gate supervisor
  AUTOACT through the queue, reversibility/undo, stale-approval expiry.

## PRODUCTION ROLLOUT

Full Railway runbook (ordered SQL list, staged env-flag sequence A→E, monitoring
queries, rollback): **`docs/agent_bus_rollout.md`**. The Phase-1-only checklist
below is a subset.

## SHIP PHASE 1 — rollout checklist (recommendation #1)

1. Apply `sql/agent_bus_pilot.sql` on the **Railway** DB (user runs it; never
   `deploy_sp.ps1`). Confirms event types, subscriptions, both `fn_emit_*`.
2. Set Railway env: `AGENT_BUS_ENABLED=1`, leave `AGENT_BUS_AUTOSEND=0`
   (draft-only) for the first week.
3. Deploy the backend (Railway) carrying `app/core/agent_bus.py` + `app/main.py`.
4. Smoke test: `GET /agent-bus/status` (enabled/running), then
   `POST /agent-bus/run-once`; or `SELECT fn_emit_overdue_invoice_events(5);`.
5. Monitor (daily): pending vs completed queue, drafts created, failures —
   ```sql
   SELECT e.event_type, q.status, count(*)
   FROM event_queue q JOIN events e ON e.event_uuid=q.event_uuid
   WHERE e.event_type IN ('invoice.overdue','lead.scored') AND e.source_system='agent_bus'
   GROUP BY 1,2 ORDER BY 1,2;
   ```
   plus `activities` with subject `Payment reminder%` / `Hot lead outreach%`.
6. After a clean week, flip `AGENT_BUS_AUTOSEND=1` to let Email actually send.
