# Agentorc / Conscestra — Full Architecture Audit vs. the Autonomous CRM Vision

_Audit date: 2026-07-18 · Scope: all 14 pillars of the "AI Operating System for
Customer Relationships" vision plus the 2026 Autonomous Enterprise Architecture
(AEA) enhancements (semantic layer, agent registry/lifecycle, observability,
policy-as-code, event-driven fabric). Companion to `conductor_gap_audit.md`
(2026-07-17), which covered the conductor + workflow-engine pillars in depth._

---

## TL;DR

The architecture implements roughly **90% of the 14-pillar vision**, and in
several places (governed writes, planner bounds, identity fail-closed posture)
it is *more* conservative and better-engineered than the reference describes.

Since the 2026-07-17 conductor audit, its gaps 1–3 are **closed in code**:

- the planner is on the conversational path (`plan:`/`goal:` handle,
  `app/agents/orchestrator/router.py:412`),
- the supervisor→planner bridge exists (`supervisor.py` Phase 3.5,
  `SUPERVISOR_PLANNER=1` locally as of 2026-07-18),
- automatic plan routing is built and **live locally** (`INTENT_PLAN_ROUTING=1`).

The remaining genuine gaps cluster in five areas — and the single biggest gap
is not architectural at all: **the deployed Railway build is stale and the
cutover hasn't happened**, so most of this orchestra only plays on localhost.

---

## Pillar-by-pillar scorecard

| # | Vision pillar | Status | Grounding |
|---|---|---|---|
| 1 | Orchestrator / conductor | ✅ ~95% | intent router + symphonies + bounded planner + supervisor bridge; `RouteDecision.kind` partially unifies the per-turn "conductor decision" |
| 2 | Unified communication layer | 🟠 ~70% | identity ✓ (`identity.py`), Conversation Object ✓ (`conversations.py`), but capture only wired for SMS + email; WhatsApp/Slack/Teams transports built yet credential-gated; **voice & webchat not captured into conversations** |
| 3 | Voice agent | ✅ ~90% | media streams + VAD + Azure STT/TTS (`voice_stream.py`), OTP tiers, payments; no emotion/tone analysis |
| 4 | SMS agent | ✅ | autosend live, KB-grounded composer, verification flows |
| 5 | Email agent | ✅ | drafts, quotes, CASL consent/suppression (`consent.py`), nurture sequences |
| 6 | Internet intelligence | ✅ | `web_tools.py` (ddgs → Tavily), `web.consult` A2A, gap-miner web fallback |
| 7 | Knowledge & retrieval | 🟠 ~60% | KB + ingestion (`kb_ingest.py`) + growth loop ✓, but **Postgres FTS only — no semantic / hybrid / graph search** (`knowledge.py:15`) |
| 8 | Specialized agents | ✅ ~85% | 15+ agents; no dedicated Inventory / Procurement / Logistics / Project agents (orders/products/store cover parts) |
| 9 | Security & governance | ✅ | RBAC (`auth_dep.py`), `write_guard.py` at the `execute_sp` choke point, critic-reviewed approvals, audit trails |
| 10 | Safe DB access | ✅ | no direct SQL from LLMs; everything through governed SPs |
| 11 | Multi-layered memory | 🟠 ~75% | customer ✓ (`customer_memory.py`) / org (KB) ✓ / strategic (`objectives.py`) ✓; **short-term memory is an in-process dict** (`memory.py`) — lost on restart, single-worker only |
| 12 | Autonomous workflow engine | ✅ code / 🟡 flags | planner + bus + sequences done; `SUPERVISOR_AUTOACT=0`, `OBJECTIVES_AUTOACT=0` still held (deliberately) |
| 13 | Executive intelligence | ✅ ~85% | CEO briefing (`ceo_briefing.py`), per-exec profiles (`executive_intelligence.py`), anomaly alerts; **no scenario simulation / what-if** |
| 14 | Continuous learning | ✅ | learning loop (`learning.py`), evals, tuning proposals, KB gap miner, data-quality agent, LLM metering (`llm_meter.py`) |

**Flag state (local `.env`, 2026-07-18):** bus/supervisor/governance/objectives
all ON; `SUPERVISOR_PLANNER=1`, `INTENT_PLAN_ROUTING=1`; only
`SUPERVISOR_AUTOACT=0` and `OBJECTIVES_AUTOACT=0` held back.

---

## Gaps and enhancements, ranked by leverage

### 1. 🔴 Semantic retrieval layer (the clearest AEA gap) — **DONE 2026-07-18**

All retrieval was deterministic Postgres full-text — precise but
vocabulary-brittle: a customer asking about "returns" won't match an article
titled "refund policy" unless they share words. Implemented as **hybrid
search** in `app/core/semantic.py` + `knowledge.rag_block`:

- `kb_embeddings` (sql/kb_semantic.sql) caches one OpenAI
  `text-embedding-3-small` vector per ACTIVE article, keyed by a content hash
  (re-embeds only on edits, retired articles drop out). Vectors are jsonb and
  cosine runs **in-process** — at KB scale (24 articles, ~30 KB each) this
  beats pgvector: zero extensions, near-zero volume cost on the tight Railway
  DB. Lazy refresh (≤ every SEM_REFRESH_SECS), embed calls metered into
  `llm_usage` (caller `kb_semantic`).
- `rag_block` fuses the FTS term-precision list with the semantic list by
  reciprocal-rank fusion — agreement outranks either signal alone; the gap
  miner still logs unanswerable questions.
- Degrades completely: flag off / no key / table missing / API error → pure
  FTS, byte-identical to before. `SEMANTIC_ENABLED=0` in code, `=1` in local
  `.env`. Admin: `/kb/semantic-status|-reindex|-search`.

Verified: "my package still has not shown up" (zero shared keywords) grounds
to the order-status article @0.36; "monthly installments" → payment methods
@0.44; keyword queries unchanged; disabled path identical to pre-semantic.
**Railway: apply sql/kb_semantic.sql after the volume resize, then set
SEMANTIC_ENABLED=1 there.**

### 2. 🔴 Conversation capture is half-wired (Unified Comms Phase 2)

`channel_adapters.py` captures only inbound SMS and inbound email into the
Conversation Object. Voice calls (which already produce transcripts) and
webchat (the highest-volume channel) don't thread in — so "start on voice,
continue via email with zero repetition" only works in one direction.
Wiring `capture_*` calls into the voice path and the chat path is small,
additive, and uses contracts that already exist. **← IMPLEMENTED + verified
same day; see the implementation appendix below.**

### 3. 🟠 Durable short-term memory — **DONE 2026-07-18**

Session memory was an in-process deque keyed by session id (`memory.py`) —
gone on restart, broken under multi-worker uvicorn. Now write-through:
`agent_session_memory` (sql/session_memory.sql) stores each session's trimmed
window as one jsonb row; the deque stays the fast path, and a cache miss
(restart / other worker) rehydrates from the DB. Best-effort everywhere — a
missing table degrades to the old in-process behavior; a memory failure never
breaks a chat turn. Idle rows self-prune after `SESSION_MEMORY_TTL_DAYS`
(default 7) — no scheduled job. Kill switch `SESSION_MEMORY_DB=0`.

Verified: a 2-turn session survived a simulated restart (store cleared,
history rehydrated identically); `clear_session` removes the DB row.
**Railway: apply sql/session_memory.sql (tiny fixed-size table — no volume
concern).**

### 4. 🟠 Agent registry & policy-as-code as data, not code — **DONE 2026-07-18**

Implemented as two small data layers over the existing engines
(sql/registry_policies_trace.sql):

- **Capability registry**: `capability_registry` overrides AVAILABILITY per
  A2A intent at runtime — the in-code registry stays the source of what
  exists. `dispatch` refuses a disabled capability with a clean structured
  error (which is itself traced). No row / no table / DB failure = enabled:
  the gate can degrade but never lock the platform out. Endpoints:
  `GET /a2a/registry` (manifest merged with state), `POST
  /a2a/registry/{intent}`. 30s cache.
- **Governance policies**: `governance_policies` rows override guardrail
  numbers live — whitelisted keys only (`gov.act_min`, `gov.propose_min`,
  `planner.max_steps`, `planner.max_writes`), bounds-checked, and the
  act/propose band is enforced (act_min can't drop below propose_min).
  `governance.decide()` and the planner's caps read the live values; no row =
  env/code default, so the guardrails are tunable but never absent. Endpoints:
  `GET/PUT/DELETE /governance/policies[/{key}]`.

Verified: act_min override 0.9 flipped decide(0.85) from act→propose and
delete restored it; out-of-bounds, band-violating and unknown keys refused;
planner.max_steps=2 rejected a 3-step plan; a disabled capability refused
dispatch and re-enabled cleanly. **Railway: apply
sql/registry_policies_trace.sql.**

### 5. 🟡 Observability: metered but not traced — **DONE 2026-07-18**

The correlation ids always existed (A2A envelopes, bus events, planner-tagged
approvals, sequences); now they stitch back together:

- `a2a_dispatches` (same migration) records every real dispatch — intent,
  agents, kind, ok/error, latency — best-effort, self-pruning (30 days).
- The a2a propose path tags each queued approval with `_correlation_id`
  (hidden from the approval UI), and `governance._execute` **reuses** the
  originating play's correlation id when an approved action re-dispatches —
  so proposal AND approved execution land in one trace.
- `app/core/trace.py`: `GET /trace/{correlation_id}` unions a2a_dispatches +
  events + action_approvals + agent_sequences, time-ordered; each source is
  queried independently and best-effort, so a partially-migrated DB still
  traces. `GET /trace-recent` lists entry points.

Verified: one correlation id returned a 5-step trace across `a2a`, `event`
and `approval` sources — including the *refused* disabled-capability dispatch,
which is exactly what an audit trail should capture.

### 6. 🟡 Scenario simulation for the executive layer — **DONE 2026-07-18**

`app/core/simulator.py` + a `simulate:` / `what if …` handle in the
orchestrator chat (and A2A read capability `crm.simulate`, admin endpoint
`GET /simulate?q=`):

- **parse** — lite-tier LLM maps the scenario onto ONE registered metric
  (`objectives.METRICS`) + a % change or set-to value; deterministic
  keyword/percent fallback; unmappable scenarios are refused with the metric
  catalog (the simulator never invents a metric).
- **ground + project** — starts from the metric's LIVE value and re-judges
  every active objective on that metric with `objectives.evaluate()` —
  status before → after, % of gap to target closed — plus one bounded
  deterministic ripple (overdue count ↔ overdue AR, proportional).
- **read-only by construction** — the module has no write path.

Verified: "cut overdue invoices by 30%" → 12 → 8 against the live 90-day
objective (on_track, 60% of gap closed) with the AR ripple ($1,957 → ~$1,370);
fallback parse, unmappable refusal, A2A dispatch and the chat handle (ASGI,
`mode=simulate`) all pass.

### 7. 🟡 Stage the held-back flags — **STAGED 2026-07-18 (propose mode)**

Both flags flipped locally behind governance-propose mode, exactly per the
staging design: eval gate ran first (10/10 green), then
`SUPERVISOR_AUTOACT=1` + `OBJECTIVES_AUTOACT=1` with
`SUPERVISOR_AUTOACT_CONF=0.75` — below GOV_ACT_MIN (0.8), so **every
auto-action queues for human approval; nothing executes unprompted**.
Verified on a live tick: ar_spike's dunning proposed + deduped, the
planner-first bridge suppressed legacy autoact where it queued writes, zero
direct emitter events. Objectives share the same gate (both objectives
currently on-track, so no play fired). **Graduation**: after an observation
window of clean proposals, delete `SUPERVISOR_AUTOACT_CONF` (restores 0.85 ≥
act_min → real acting) — or tune live via
`PUT /governance/policies/gov.act_min`. Railway staging steps are in
`docs/agent_bus_rollout.md` → CUTOVER ADDENDUM 2026-07-18.

### 8. ⚫ The real gap: production

Everything above is refinement; the material gap between vision and reality is
operational — stale Railway build (2026-07-07), Twilio webhooks on an
ephemeral tunnel, the pending token rotation, and the event_queue backlog.
Until the cutover, the production system is an older, smaller orchestra.
**2026-07-18: the runbook is fully refreshed** — `docs/agent_bus_rollout.md`
→ "CUTOVER ADDENDUM 2026-07-18" lists the 7 post-bundle migrations (with the
kb_semantic volume-resize prerequisite), the Railway env flags mirroring the
locally proven state, the AUTOACT graduation path, and the new smoke checks.
Execution remains the operator's (per the deploy rules).

---

## Re-audit round (same day) — "five pillars" additions

A second reference pass (zero data entry / revenue protection / visual
governance framing) surfaced two more items, both **DONE 2026-07-18**, no
migration needed (sentiment was already being captured per interaction by
`customer_memory._distill_llm` — only the read side was missing):

- **Cross-channel sentiment + CSAT**: negative conversations post a
  `negative_sentiment` blackboard signal (14-day TTL) that the AI 360 summary
  reads automatically; the CEO briefing gained a "Customer Health &
  Operations" section (7-day all-channel sentiment, 30-day CSAT proxy = share
  of non-negative interactions, inventory risk) with delta tracking; and a
  `sentiment_drop` supervisor detector (≥5 scored conversations, ≥40%
  negative) feeds the planner bridge with a save-play goal.
- **Inventory risk**: `detect_low_stock` — active products at/below the stock
  floor (5) or short of open-order demand (order_items × pending/processing
  orders) — trips `inventory_risk`, appears in the briefing, and maps to a
  replenishment-review planner goal. All four thresholds are env-tunable.

Follow-up round (**DONE 2026-07-18**) closed the last three:

- **Visual no-code governance** — governance-mgmt.html gained four cards:
  🎚 Guardrails (live policy sliders with Set/Reset against
  /governance/policies), 🧩 Capability registry (per-intent enable/disable
  toggles; disabling prompts for a note shown in refusals), 🧵 Recent plays
  (correlation-trace timeline viewer), 🔮 What-if simulator. Local HTML —
  the operator publishes it.
- **Email attachments → KB** — `_parse_email` now collects document
  attachments (pdf/txt/md/csv/html, ≤3, ≤8 MB, executables filtered);
  a KNOWN sender's documents flow through `kb_ingest` into governed article
  proposals (cap 3/doc/pass, idempotent by content hash, background).
  Strangers' files never reach the LLM. Kill switch `KB_ATTACH_INGEST=0`.
- **Inbound calendar** — `POST /calendar/import` turns pasted .ics VEVENTs
  into meeting activities, deduped by event UID; an attendee email matching
  a CRM contact relates the meeting to that contact + account.

## Guardrail round (2026-07-19) — the four-layer safety model, complete

Against the "4 essential guardrail layers" reference (HITL thresholds /
deterministic brand boundaries / toxic triage / granular RBAC), all four are
now implemented — and the work surfaced and closed a real pre-existing hole:

- **Layer 1 — HITL amount floor**: new `gov.hitl_amount` policy (default
  $1,000, live-editable in the Guardrails UI): a governed write whose params
  carry an amount at/above it ALWAYS pauses for approval, even at act-level
  confidence — confidence measures how sure the agent is, not how much is at
  stake. Verified: a 0.95-confidence $5,000 action queued; $50 flowed.
- **Layer 2 — deterministic brand boundary**: `brand.max_discount_pct`
  (default 15%) clamps every agent-built quote — requests above it are cut
  to the cap and flagged, never sent. Replaces a hardcoded 50% clamp.
- **Layer 3 — outbound guard** (`app/core/outbound_guard.py`): deterministic
  triage at the universal send choke points (send_email, send_sms) + the SDR
  reply composer — toxic language, shouting, legally binding promises,
  internal-marker leaks, unresolved placeholders, card numbers, and
  over-cap discount promises in prose. Zero LLM calls, microsecond cost;
  blocked sends degrade the platform way (skip/draft/scripted fallback).
  `GET /outbound-guard/status|test`. Kill switch `OUTBOUND_GUARD_ENABLED=0`.
- **Layer 4 — agent RBAC**: `capability_registry.allowed_callers`
  (sql/guardrails_acl.sql) restricts who may dispatch a capability — e.g.
  accounting capabilities walled off from customer-facing agents. Approved
  executions pass (the human approval is the authority).
- **Closed hole**: the six STRUCTURED write capabilities (sms.send,
  quote.generate, contact.update_profile, scoring.activate,
  data.normalize_phones, data.merge_contacts) previously bypassed the
  Phase-5 confidence gate entirely — the gate now sits before both dispatch
  paths. Eval harness 12/12 green after the change.

(The reference's input-layer guardrails already existed: write_guard at the
SP choke point, PII masking, SDR injection eval, customer_scope fail-closed.)

## What the vision asks for that is already *exceeded*

- **Guided determinism** — the planner's "every step must be a registered
  capability, reads execute / writes propose" is a stricter contract than the
  reference's "approval gates where required".
- **Event-driven fabric** — `agent_bus.py` + blackboard coordination +
  idempotency guards on every handler.
- **Hybrid orchestration patterns** — supervisor (central) + bus fan-out
  (peer) + planner (dynamic composition) coexist, which is the "adaptive
  network" end-state most vendors only describe.
- **Learning loop** — gap mining, tuning proposals, data-quality agent, and
  behavior evals form a governed improvement cycle with humans approving every
  change.

---

## Appendix — Item 2 implementation (conversation capture) — DONE 2026-07-18

Every customer conversation, regardless of channel, now threads into the ONE
cross-channel Conversation Object (`conversations.ingest`), best-effort and
flag-gated exactly like the SMS/email adapters.

- **Support voice line**: `voice_support._close_call` captures the whole call
  as one inbound `voice` message keyed by the caller's number
  (`capture_voice`, new in `channel_adapters.py`), with tier/reason metadata.
  `_close_call` is now idempotent (`_closed` guard moved inside it), which also
  fixed a latent double-log on the media-stream path where a goodbye and the
  carrier's stop event both closed the call.
- **SDR line (webchat + voice, both transports)**: `sdr.converse` gained an
  optional `handle` and captures each turn in both directions via
  `_capture_turn` — voice threads by caller number (passed from
  `/sdr/voice/turn` and `voice_stream._brain_turn`), webchat threads by the
  visitor's typed email once captured, else the browser session.
- Contract identical to existing adapters: BEST-EFFORT (never break the
  channel), FLAG-GATED (`CONV_CAPTURE_ENABLED`), ADDITIVE.

Verified end-to-end against the local DB: webchat 2 turns → 4 threaded
messages; an SDR voice turn → 2 messages keyed by phone; a support-call close
captures exactly once even when closed twice; and an SDR voice call + a
support call from the same number landed in **one** conversation.

Known limit (deferred): a webchat thread splits when the typed email first
appears (anonymous session key → email key) — the general anon→resolved
conversation merge is the fix, tracked in the Unified Comms backlog.
_(Closed 2026-07-19 — see the re-audit section below.)_

---

## Re-audit 2026-07-19 — updated "Ideal AI-Driven Autonomous CRM" reference

_A refreshed reference description (14 pillars restated + AEA framing:
semantic/context layer, agent registry & lifecycle, guided determinism,
event-driven fabric, hybrid multi-agent patterns, standard protocols) was
audited against the codebase. Claims from the 2026-07-18 audit were re-verified
in code (`semantic.py` hybrid search, `transports.py` adapters,
`voice_stream.py` barge-in, the 4 guardrail layers) rather than trusted._

**TL;DR: ~92% of the updated vision is implemented and locally verified.**
The updated description validates the direction rather than exposing new
architectural work; the distance to "ideal" remains operational (the Railway
cutover), not architectural.

### Scorecard movement since 2026-07-18

| # | Pillar | 07-18 | 07-19 | What moved |
|---|---|---|---|---|
| 2 | Unified comms | 🟠 70% | ✅ 85% | voice + webchat capture wired; all channels thread into ONE Conversation Object |
| 7 | Knowledge & retrieval | 🟠 60% | ✅ 85% | hybrid semantic+FTS with RRF fusion (`semantic.py`) |
| 9 | Security & governance | ✅ | ✅ **exceeds** | 4-layer guardrail model complete; structured-write governance bypass closed |
| 11 | Multi-layer memory | 🟠 75% | ✅ 95% | durable session memory (DB write-through) |
| 13 | Executive intelligence | ✅ 85% | ✅ 95% | what-if simulator (`simulator.py`) closes the scenario-simulation ask |

All other pillars unchanged (see the 07-18 scorecard above).

### True deltas the *updated* description introduces

1. **Channel breadth** — WhatsApp/Slack/Teams transports exist but are
   credential-gated (no live tenant); WeChat, MMS, social channels, and a
   self-service portal do not exist. Webchat/voice/SMS/email/API-MCP fully live.
2. **Multimodal voice understanding** — tone/emotion/urgency from the *audio*
   itself. Sentiment is inferred from transcripts only; nothing reads prosody.
   Barge-in/interruptibility is done.
3. **Graph search** — retrieval is hybrid (semantic + keyword) but has no
   knowledge-graph/ontology layer and no SharePoint/Drive/Notion connectors
   (`kb_ingest` covers PDF/doc/URL/email attachments).
4. **Standard inter-agent protocols** — internal A2A envelopes + MCP server
   exist; the external Google-A2A / agent-mesh protocols are not implemented.
   Low urgency until there is an external agent to talk to.

Named-agent gaps (Procurement, Logistics, Project, Document, dedicated
Forecasting/Collections) are **packaging, not capability** — dunning, inventory
risk, churn scoring, and document ingestion all exist inside the
supervisor/planner/KB layers. Split into dedicated agents only if routing
quality demands it.

### Ranked recommendations (2026-07-19)

1. ⚫ **Execute the Railway cutover** (operator task; runbook in
   `agent_bus_rollout.md` ADDENDUM). Dwarfs everything else.
2. 🟡 **Graduate AUTOACT** after a clean observation window of proposals
   (delete `SUPERVISOR_AUTOACT_CONF`).
3. 🟡 **Anon→resolved conversation merge** — the one known Unified Comms
   defect. **← DONE same day, see below.**
4. 🟢 **Voice urgency/emotion proxy** — cheap prosody proxy (interruption
   rate, speech rate) feeding the existing sentiment blackboard signal.
   **← DONE same day, see below.**
5. 🟢 WeChat/MMS/portal — only on demand; the `transports.py` adapter pattern
   makes each a small additive job.
6. ⚪ Defer: knowledge graph, external A2A protocol, dedicated ops agents —
   revisit at larger KB/agent scale.

### Appendix — items 3 & 4 implementation (2026-07-19)

**Anon→resolved conversation merge** (`conversations.py`,
`channel_adapters.py` — no migration; `status='merged'` needs no schema
change):

- `InboundMessage.prior_handle` — an adapter that KNOWS the sender previously
  threaded anonymously under a different handle passes it; `ingest` then folds
  that open anon conversation into the current one (`_merge_anon`): messages
  move (keeping their original handle for audit), counts and `created_at`
  roll up, the emptied shell becomes `status='merged'` (excluded from the
  partial open indexes). Only `party_id IS NULL` shells are eligible — a
  resolved person's thread is never absorbed.
- `capture_webchat` supplies `prior_handle=session:<sid>` once the visitor's
  email appears — the exact split scenario from the 07-18 known limit.
- Verified live: 2 anon webchat turns + an email turn → ONE conversation with
  all 3 messages, shell marked merged with 0 messages left; the follow-up turn
  threads in with no re-merge; `channel_selector._learned_preference`
  unaffected (merged shells have no party_id).

**Voice urgency proxy** (`voice_stream.py` — media-stream transport only,
where the raw audio is available; no migration):

- Deterministic prosody stand-ins, zero audio-ML: per-call barge-in count
  (the caller talking over the assistant) and sustained speech rate
  (transcribed words ÷ PCM speech duration). Thresholds env-tunable:
  `VOICE_URGENCY_BARGE_MIN` (2), `VOICE_URGENCY_WPM` (185),
  `VOICE_URGENCY_MIN_WORDS` (12, gates the wpm check so one short
  exclamation can't trip it).
- At call end an urgent call posts a `voice_urgency` blackboard signal
  (severity medium, 7-day TTL) on the RESOLVED caller — the same channel as
  `negative_sentiment`, so the AI 360 summary and supervisor detectors pick
  it up with zero extra wiring. Anonymous callers are skipped (no entity).
  `urgent_calls` counter in `/voice-stream/status`.
- Verified: threshold matrix (barge/wpm/calm/short-blip) + a live
  `_post_urgency` against a resolved contact landed on `agent_blackboard`
  and was cleaned up.

Pillar 3 (voice) moves ~90% → ~95%: true tone-of-voice ML remains the only
delta, and the reference's *behavioral* urgency ask is now covered.
