# Conscestra CRM — World-Class Architecture Assessment

**Independent, adversarial review. 2026-09-05.** Reconstructed from the repository
(commit `69ff80f` on `feat/owner-eligibility-and-employee-grants`, 18 commits behind
`origin/master`), the local database (PostgreSQL 17.9), the production database
(Railway, PostgreSQL 18.6), the running production application
(`/health` commit `cfaef1776d54` = `origin/master` HEAD, deployed 2026-09-05 14:21 UTC),
CI configuration, and the governance repository pinned at `2a5f750`.

Every claim carries one of these labels:

| Label | Meaning |
|---|---|
| **FACT** | Measured on the live system or read directly from code in this review |
| **INFERENCE** | Concluded from facts; could be wrong |
| **RISK** | A consequence that has not happened yet, or has happened and could recur |
| **RECOMMENDATION** | What to change |
| **DECISION REQUIRED** | A choice only the owner can make |
| **HUMAN ACTION REQUIRED** | Something a person must do; the system cannot |

Verification status uses: VERIFIED · PARTIALLY VERIFIED · INFERRED · UNVERIFIED · CONTRADICTED BY EVIDENCE.

Nothing in this document was taken from `README.md`, `skills.md`, the marketing site,
or the two prior architecture reviews without being re-measured. Where a prior review
was wrong, §16 says so.

---

## 1. Executive verdict

**Conscestra is an AI-enabled CRM with an unusually complete governance
*mechanism* layer, operating as a low-traffic single-tenant demo. It is not yet a
governed AI-agent CRM in the operational sense, because the governance is built
but not exercised, the accountability chain cannot name a human owner for most
business records, and the platform's own failure alerts are raised but not
closed.**

The engineering is materially stronger than a typical AI-CRM codebase. The
deterministic write boundary is real and was confirmed fail-closed against
production in this review. The self-documenting, self-correcting engineering
culture (every control's comment records the failure that motivated it) is a
genuine asset that most enterprise platforms lack. But the gap between
"the mechanism exists" and "the mechanism governs" is the whole finding.

Three facts define the current state:

1. **FACT.** On Railway, 59 of 66 governance proposals (89%) expired unactioned.
   Two human approval decisions have ever been made in production. `governance_policies`
   has zero rows on both databases, `allowed_callers` is NULL on 45 of 45 capabilities,
   and `agent_capability_grants` has zero rows. Two of the advertised four guardrail
   layers are unconfigured everywhere.
2. **FACT.** On Railway, 39 of 51 rows in `owners` are customer contacts and only 7
   link to an employee. `orders.owner_id` is NULL on 97.5% of orders. The question
   "which human is accountable for this business outcome" has no answer for most
   records, and the read-side eligibility contract that would answer it (E2) is
   built but enforced nowhere on the write path.
3. **FACT.** On Railway, 18 orders shipped since 1 September have no shipped
   notification. Their 18 `order.shipped` events sat unclaimed for 85 hours.
   `/platform/health` reports `state: critical`, `drain_rate: 0`. The `bus_stalled`
   detector fired exactly once (2026-09-02 13:00 UTC) and then went silent, because
   it only inspects events created within the last 24 hours. Nobody acted.

**Is it fundamentally sound?** Yes. **Does it need a rewrite?** No. **Is it
world-class?** Not yet, and §19 defines what would make it so.

---

## 2. Architecture reconstruction

### 2.1 System map (VERIFIED from code and running system)

```
CHANNELS
  web chat (agentorc.ca static HTML → Railway API) · store widget · embedded widget SDK
  voice (Telnyx TeXML + media streams) · SMS · IMAP email · Slack/Teams/WhatsApp (credential-gated)
  customer portal · MCP stdio server (proxies HTTP) · admin HTML consoles
        │
        ▼
EDGE  app/main.py (2,522 ln) — FastAPI, 36 APScheduler jobs, CORS, PNA middleware
  auth_dep: posture (open | public-read | locked) · require_admin · require_session
  write_guard ContextVars: role · readonly_channel · customer_scope
  release_guard.enforce() at startup — refuses unsafe deployed config
        │
        ▼
ORCHESTRATION  app/agents/orchestrator/router.py (798 ln)
  symphonies (fixed fan-out) · intent_router (LLM + keyword fallback, cached)
  structured_cutover (contacts/accounts/leads: resolved intent is authoritative)
  planner (opt-in; ≤6 steps, ≤2 writes; writes → proposals)
  supervisor (14 detectors on a schedule; autoact → governance.decide)
        │
        ▼
CAPABILITY MESH  app/core/a2a.py (1,724 ln) — 45 capabilities: 29 read, 16 write
  gate order: registry enabled → allowed_callers → principal (writes) → params_schema
              → governance confidence → HITL amount floor → structured SP | prose agent
  audit: a2a_dispatches (outcome ∈ accepted/rejected/failed/unknown, principal)
        │
        ▼
DOMAIN AGENTS  12 LangGraph graphs (pre_router → [LLM] → sql_builder → db → formatter)
  + email, voice, store, SDR, custom (data-defined, tool-less)
        │
        ▼
DETERMINISTIC EXECUTION
  execute_sp() — forbidden-procedure list, WRITE_MODES courtesy check,
                 PostgreSQL READ-ONLY transaction for non-writer roles/channels,
                 customer_scope fail-closed
  ~26 stored procedures (governance/sp/), 65 triggers, 61 registered event types
  get_connection() — ~204 direct DML sites across 75 modules (declared in write_call_sites.py)
        │
        ▼
DATA  PostgreSQL 18 (Railway) · 159 tables · 88 FKs · 0 RLS policies · pgvector 0.8.6
  events → event_queue (SKIP LOCKED consumer, watermark) → handlers → notifications
  action_approvals · a2a_dispatches · audit_log (append-only trigger) · record_field_history
        │
        ▼
EXTERNAL  OpenAI gpt-5-mini (primary) · Gemini flash-lite (alt, free tier, policy-gated)
  SMTP · Telnyx · Azure Speech · DuckDuckGo/Tavily
```

### 2.2 Verification of the architectural claims

| Claim | Status | Evidence |
|---|---|---|
| Single typed chokepoint for agent writes | **CONTRADICTED** (and the code says so) | `write_call_sites.py`: three write boundaries; ~204 direct DML sites. The enumeration is the control. |
| Non-writer roles cannot mutate via NL | **VERIFIED in production** | Anonymous `structuredIntent:{mode:"archive"}` to Railway `/contact-chat` → HTTP 401 |
| LLM never emits SQL | **VERIFIED** | `semantic_query.py` compiles from a field allow-list; agents' `sql_builder` build SP calls from validated modes |
| Every write carries a principal | **VERIFIED since 2026-08-26** | Railway `a2a_dispatches`: all write rows after 08-26 carry `principal`; 60 earlier rows are pre-migration |
| Four guardrail layers | **PARTIALLY VERIFIED** | HITL floor + discount clamp + outbound guard: code-verified. `allowed_callers` (layer 4): NULL on 45/45 rows in both DBs → inert |
| Approvals are human-governed | **CONTRADICTED by operation** | 89% expire; 2 human decisions ever on Railway; 176 of 285 local decisions by `policy:web_order_cancel` |
| Correlation tracing end-to-end | **VERIFIED** | `/trace-recent` on Railway returns stitched a2a + event steps; `memory_retrievals` recorded per correlation id |
| Privilege separation in production | **VERIFIED** | `/health.database.connected_as = crm_app`, `rolsuper = false` |
| CI gate cannot be skipped | **VERIFIED** | `gate-report` job with `if: always()` converts skips to failures; pin file couples governance commit |
| Migrations current on both DBs | **VERIFIED with caveat** | `migrate --check` = "schema is current" on both; Railway has one extra catch-up migration with unverifiable checksum; `/deploy/migrations` on Railway reports `ok:false` for the same ledger (documented semantic split, still live) |
| HA leader election | **VERIFIED, single-node** | Railway `/health.ha.role = leader`, 2 workers; no automatic failover |
| Backups verified by restore | **VERIFIED by artefact** | `backups/` 17 files, runbook with measured RTO; RPO 24h, PITR not enabled |
| Multi-tenant | **UNVERIFIED / DEFERRED** | `MULTI_TENANT_ENABLED=0`, 1 tenant row, 0 RLS |
| Production carries enterprise load | **CONTRADICTED** | Railway: 0 active sessions, 4 admin sessions ever, 60 LLM calls in 7 days, 47 conversations in 30 days |

### 2.3 Data-flow, agent-tool-database relationship (VERIFIED)

Every agent reaches the database one of three ways:

1. **Via `execute_sp`** with an SP call whose `p_mode` was produced by a
   pre-router, an LLM JSON parse validated by `sql_builder`, or an authoritative
   `structuredIntent`. This path is role-gated, channel-gated, customer-scope-gated,
   and backstopped by a database read-only transaction.
2. **Via `a2a.dispatch`** for a registered capability, which either calls a
   structured SP function (16/16 writes are structured) or routes prose to the
   owning agent (which lands on path 1).
3. **Via `get_connection()` direct DML** in 75 modules — governance execution,
   the bus, schedulers, notifications, memory, staff email. Each public-reachable
   site is declared with its protecting control.

The LLM's output is never a SQL string on any path. This is the strongest
property in the system and it held under probing.

---

## 3. AI-agent architecture assessment

### 3.1 Are they agents or prompt wrappers?

**INFERENCE, well-supported.** The 12 domain "agents" are deterministic
pre-router → SP pipelines with an LLM as the intent parser of last resort. They
are *not* autonomous agents; they are governed capability endpoints with a
natural-language front. That is the right design for a CRM write path, and the
codebase says so plainly ("the LLM supplies judgment, deterministic rails supply
safety"). Calling them agents is marketing vocabulary; calling the *mesh* a
governed capability layer is accurate.

The genuinely agentic components are: the **supervisor** (sense → decide →
propose/act on a schedule), the **planner** (goal → bounded plan of registered
capabilities), the **bus handlers** (event → action), the **SDR/custom agents**
(conversational, tool-less), and the **critic** (independent deterministic
review). These are architecturally meaningful because each has a bounded
authority and an audit row per action.

### 3.2 Agent responsibility table (VERIFIED from code and registry)

| Agent | Purpose | Inputs | Outputs | Tools | Authority | Data access | Human owner | Audit evidence | Failure mode |
|---|---|---|---|---|---|---|---|---|---|
| 12 domain agents (accounts…notifications) | NL/structured CRUD on one entity | chatInput, session | SP result, formatted | one SP each | write iff role ∈ WRITE_ROLES | CRM-wide (SPs are unscoped) | session user (or none) | `audit_log` (entity-level), a2a row if dispatched | LLM parse fail → error; DB refuses under RO txn |
| Orchestrator | route, fan out, plan | message | agent output(s) | in-process ASGI | none of its own | via agents | session user | a2a rows per hop | keyword fallback; never 500 on classifier |
| Supervisor | detect breaches on schedule | KPI pack | `supervisor.alert` events; proposals | governance.propose, bus emit | AUTOACT via `governance.decide(0.75)` | KPI SP | **none** (alerts have no owner) | events table | detector exception swallowed at DEBUG |
| Planner | goal → plan | goal, capability manifest | read results + write proposals | a2a.dispatch | writes never execute | reads only | proposal approver | approvals tagged with plan cid | invalid plan rejected |
| Bus handlers (11 types) | act on events | event row | side effects (email drafts, tasks) | a2a, direct DML | `Principal.service('agent-bus')` | unscoped | **none** | event_queue status, a2a rows | retry ×5 then `failed`; orphaned before cutoff = silent |
| Critic | second opinion on proposals | proposal | critique jsonb | SQL reads | advise only | reads | approver | `action_approvals.critique` | best-effort, never blocks |
| Custom agents (2 on Railway) | grounded Q&A | message | text | none | none (U4 grants: 0) | KB tier only | studio author | conversations | outbound guard; escalation record |
| SDR | qualify + book | chat/voice | lead, meeting | booking.py | state machine decides | lead upsert | none | conversations, leads | scripted fallback |
| Email auto-reply | reply to inbound | IMAP mail | outbound draft/send | KB retrieval | AUTOSEND gated by `is_deployed()` | KB + context pack | none | `audit_log auto_reply_sent` (16/30d) | skip + gap log |

**Gaps this table exposes:**

- **Human owner column is mostly empty.** Only session-initiated writes and
  approvals have a person behind them. Supervisor alerts, bus actions and
  auto-replies have a *service principal* (good) but no accountable human (bad).
- **Data access is CRM-wide for every staff-facing agent.** Row scoping exists
  only for the verified-customer channel. `access.py` states this is deferred
  because no rep role exists. That is true (all 4 Railway sessions are `admin`),
  and it means the authorization model has one real tier: admin.
- **Overlap:** `supervisor.emit_dunning` and the bus's `invoice.overdue` handler
  both produce dunning; the code documents the dormant-second-composer risk
  and consolidated once already.

### 3.3 The "Governed AI" test, per capability class

| Question | Read capabilities | Write capabilities (16) | Outbound (email/SMS) |
|---|---|---|---|
| Identity | from_agent + principal (optional) | principal **required** | principal required + sender allowlist |
| Prohibited actions | registry disable | registry disable, `FORBIDDEN_PROCEDURES` | outbound guard patterns |
| Authorization | role/channel at SQL chokepoint | + governance confidence + HITL floor | + AUTOSEND × `is_deployed()` |
| Evidence | a2a row (if dispatched) | a2a row + approval row | `order_notifications` / `staff_email_ledger` with provider outcome |
| Reconstructable | yes via `/trace/{cid}` | yes | yes (state ≠ "sent"; "accepted" is the honest ceiling) |
| Reversible | n/a | **9 of 16** have undo handlers | no (documented) |
| Same request twice | idempotent | **approval double-execute possible** (§11) | idempotent by UNIQUE key (orders) |
| Hallucinated ID | `validate_params` shape only; SP raises | same | same |
| Unauthorized attempt | recorded `rejected` | recorded `rejected` | blocked + logged |

**Ungoverned AI capabilities found:**

1. **Supervisor and objectives AUTOACT are live in production** (`/supervisor/status`:
   `autoact: true`) with a hard-coded confidence of 0.75 that routes to *propose*
   only because `act_min` is 0.8. **RISK:** editing one policy row (`gov.act_min` ≤ 0.75)
   silently converts every supervisor auto-action from proposal to execution.
   There is no second gate. DECISION REQUIRED (§20).
2. **Reads have no confidence gate and no principal requirement.** A read is
   never consequential to the database, but it is consequential to *privacy*: an
   anonymous caller on Railway retrieved 50 email addresses and 23 phone numbers
   in one request (§8). Reads are governed by posture, not by governance.
3. **`structuredIntent` is accepted from the request body** as an authoritative
   resolved intent. It cannot escalate privilege (verified: 401 for anonymous),
   but it lets any writer-role client bypass the intent resolver and its
   operation-preservation guarantees. INFERENCE: harmless today; a design smell
   that will matter when non-admin roles exist.

---

## 4. Orchestrator assessment

**Verdict: a routing and dispatch layer with governance hooks, not a
governance layer.** PARTIALLY VERIFIED.

| Property | Status | Evidence |
|---|---|---|
| Routes correctly | PARTIALLY | LLM intent routing with keyword fallback; disagreements logged, not measured in this review |
| Preserves context | VERIFIED | correlation id + principal + role + tenant travel by ContextVar through in-process ASGI hops |
| Enforces policy | INDIRECT | policy lives in a2a/governance/write_guard; the orchestrator itself enforces nothing |
| Prevents privilege escalation | VERIFIED | principal is frozen, stamped at the edge, never built from prose |
| Handles conflicting agent outputs | **UNVERIFIED / ABSENT** | symphonies concatenate; no reconciliation step exists |
| Prevents infinite loops | VERIFIED | planner caps; bus retry cap; `MAX_CALLS_PER_TURN=3` |
| Prevents duplicate execution | PARTIALLY | bus: SKIP LOCKED + stale reclaim; approvals: **no** (§11) |
| Records decisions | VERIFIED | a2a rows, `intent_router` cache and disagreement log |
| Deterministic replay | **ABSENT** | LLM route decisions are cached, not versioned; a replay re-asks the model |
| Distinguishes planning from execution | VERIFIED | planner drafts; reads execute; writes propose |
| Failure recovery | PARTIALLY | job_ledger replays missed cron; bus watermark resumes; **events before cutoff are orphaned by design** |

---

## 5. Governance assessment

**Mechanism: strong. Operation: absent.** The distinction matters because a
governance system that no one operates converges on one of two outcomes: the
queue is ignored (what is happening now — 89% expiry) or thresholds get lowered
to make the queue go away (what `verification_policy.py` explicitly warns
against).

**FACT (Railway `action_approvals`):**

| status | count |
|---|---|
| expired | 59 |
| executed | 5 |
| pending | 2 |

Decided by: `system` 59 (expiry), `policy:voice_order_cancel` 3, `email-link` 2.

**FACT (both DBs):** `governance_policies` = 0 rows. `capability_registry.allowed_callers`
= NULL on 45/45. `agent_capability_grants` = 0. `customer_memories` verified = 0 of 377
on Railway (every memory awaits human verification).

**INFERENCE:** the queue is the wrong shape for its consumer. The proposals that
expire are `kb.publish` (23), `supervisor.emit_hot_leads` (20),
`supervisor.emit_dunning` (12) — recurring, low-stakes, generated nightly by
machines for a single human who does not open the queue. The system produces
governance work faster than one person can consume it, so the TTL does the
deciding. Expiry-by-TTL is a *policy decision recorded as `system`*, which is
honest, but it means the effective policy for most AI proposals is "do nothing".

**Prompt governance.** Domain prompts live in code (`app/agents/*/prompt.py`)
and are versioned by git and CI import checks. Custom-agent instructions live
in data behind the U2 draft → evaluate → publish gate, append-only versions.
This is adequate. What is missing is a *prompt-change → eval-delta* record:
`evals.py` writes results to a supervisor alert, not to a table, so there is no
history to compare a prompt version against.

**Tool/action governance.** Capability registry is closed-by-default and
synced at startup; `params_schema` is declared on 12 of 45 capabilities
(7 of 16 writes per the P1 memo). The unschema'd writes rely on the SP to
reject malformed input — acceptable, but it means an LLM-supplied stray key
travels into the domain layer for 9 write capabilities.

---

## 6. Accountability assessment

Trace: **Human intent → AI reasoning → agent decision → tool invocation →
authorization → database mutation → business outcome → audit evidence.**

Where accountability disappears (VERIFIED):

1. **At ownership.** `owners` is a mixed population of customer contacts and
   employees sharing a primary key space with `contacts`. On Railway: 51 owners,
   39 are contacts, 7 link to an employee. The work-ownership module found that
   routing on `leads.owner_id` would have "delivered internal staff worklists to
   CUSTOMERS". The E2 eligibility predicate exists and is tested, and enforces
   nothing on any write path today (`grant()` enforces nothing — its own doc).
2. **At the supervisor.** A `supervisor.alert` has a `owner_agent` and a
   `recommended_action`, never a human. 109 alerts in 30 days on Railway; none has
   an assignee, a deadline or a closure state. Escalations (customer promises) have
   all three; internal alerts have none. The asymmetry is the defect.
3. **At the bus.** `Principal.service('agent-bus')` answers "which unattended
   thing did it". It cannot answer "who is responsible when it does not" — the
   18 missing shipped notifications have no owner.
4. **At "AI decided" vs "employee did".** The system does *not* silently
   convert one into the other — `Principal.kind ∈ {user, service, customer,
   policy, token}` was introduced specifically to stop that, and
   `principal_for_decider` tells the category truthfully. This is a real strength.
   But `audit_log` (the entity-level trail with 14k rows) carries no actor
   column at all; attribution lives only in a2a rows and approval rows.

**Field-level history.** `record_field_history` has 5 rows on Railway, last
written 2026-08-13. Only `app/core/cases.py::_mutate` writes it. Every SP-based
update to accounts, contacts, opportunities, orders and leads records an
`audit_log` row with a payload but no before/after per field and no actor.
"Who changed the close date on this opportunity, from what, when" cannot be
answered for most entities. INFERENCE: this is the single largest gap between
the audit *claims* and the audit *record*.

---

## 7. Data integrity assessment

**FACT (both DBs, this review):** 0 orphans on the seven core relationships
checked (opportunity→account, contact→account, order→account, order→contact,
order_item→order, invoice→order, payment→invoice). `dup_contacts_email` = 0.

**FACT:** 159 tables, 88 foreign keys, **112 tables (70%) carry no foreign key
at all**, 0 row-level-security policies, `deleted_at` on only 6 tables.

**FACT (ownership NULLs, Railway):**

| table | NULL owner | total | % |
|---|---|---|---|
| orders | 2,257 | 2,315 | 97.5 |
| activities | 2,216 | 13,298 | 16.7 |
| opportunities | 148 | 1,760 | 8.4 |
| accounts | 7 | 131 | 5.3 |
| leads | 0 | 100 | 0 |

**FACT:** 1,661 closed opportunities on Railway; `opportunity_stage_history`
has 16 rows. Decision history for the pipeline does not exist; win-rate
analytics are computed from current state only (the Metric Registry work
correctly kills three-way drift on the *definition*, but the *data* has no
transition record).

**FACT:** `schema_migrations` differs: local 41 rows, Railway 42 (one
`railway_catchup_20260805.sql` applied only there, plus two blank checksums).
Both report "schema is current". Tables, views and triggers are identical
between the two; the only function drift is pgcrypto/uuid-ossp wrappers (harmless
extension-version difference).

**Architectural root cause (INFERENCE):** integrity is enforced by SPs and
triggers rather than by declared constraints. That is why the core spine is
clean (SPs are careful) while 70% of tables are unconstrained (they were added
by migrations that create tables without FKs, because a wrong FK in a
`CREATE TABLE IF NOT EXISTS` migration is hard to fix). The remedy is not "add
FKs everywhere"; it is a declared-constraint policy for new tables plus
`verify_invariants` coverage of the ones that matter (owner, entity links,
approval → dispatch).

---

## 8. Security assessment

**Posture (FACT):** production runs `API_SECURITY_MODE=public-read`. Anonymous
callers can read every CRM entity through the chat endpoints; writes require a
session with a writer role. Admin/command endpoints require `ADMIN_API_TOKEN`
(verified: `/metrics`, `/governance/queue` → 403 anonymously).

**FACT (probe, this review):** anonymous `POST /contact-chat "list contacts"`
on Railway returned a body containing 50 email addresses and 23 phone numbers.

**FACT (memory + `corpus_provenance.py`):** the seed-email migration destroyed
the real-vs-synthetic distinction; 129 of 129 Railway contact emails are on the
owned catch-all domain, but whether the *people* are real cannot be reconstructed.

**RISK — P0.** Public-read is an intentional marketing decision for a demo
corpus. The moment one real customer record exists, this posture is a personal
data breach by design, and the platform's own `release_guard._check_public_read_corpus`
knows it. No enterprise buyer will accept a platform whose default production
posture is anonymous read of all contacts. DECISION REQUIRED (§20).

**Strengths (VERIFIED):**

- Database-enforced read-only transaction for non-writer roles; WRITE_MODES is
  explicitly demoted to a courtesy check. Probed on production: 401.
- `FORBIDDEN_PROCEDURES` blocks two legacy write paths unconditionally, before
  every role exemption.
- Privilege separation: app connects as `crm_app` (NOLOGIN locally, LOGIN on Railway,
  non-superuser). `crm_app` lacks DELETE on 14 tables (append-only sets).
- `release_guard` refuses to start a deployed environment with an unsafe posture,
  a misspelled mode, an undeclared sender, or an undeclared direct-write module.
- Secret health with fingerprinting; startup consensus fingerprint (unused: `/deploy/consensus` → 0 attestations).
- Prompt injection: direct probe refused; custom agents and SDR are tool-less;
  customer-authored memory is fenced as UNTRUSTED and the outbound guard blocks
  echoes of the fence.
- Red team: 10 executed attacks with revert; "not run" is a failing outcome.
- Auth: bcrypt, per-IP and per-identifier rate limits (in-process, per worker).

**Weaknesses:**

- **RISK — P1.** The developer `.env` holds the production superuser DSN in
  plaintext, and a laptop with that `.env` was a live production email sender on
  2026-09-04 (documented in `autosend_allowed`). Fixed for email/SMS/voice via
  `is_deployed()`; the credential exposure remains.
- No RLS; no rep tier; `access.py` scoping is a seam. One real role.
- Session tokens accepted as `X-Session-Token` header too; `allow_headers=["*"]`.
- `postdeploy_verify` describes itself as read-only in `--help` and as mutating
  in its module header. One of these is false. RISK: someone runs it against
  production believing the first.
- CORS origins are logged, not audited here (UNVERIFIED).

---

## 9. Reliability assessment

**FACT (Railway `/health`):** 2 workers, leader elected, pool 16/process,
36 scheduled jobs, `job_ledger` catch-up replayed missed fires (`ceo_briefing`,
`staff_email_digest`, `supervisor_tick` caught up after restarts).

**FACT:** `scheduled_job_runs` shows every job `ok` for 7 days on Railway.

**FACT:** the bus consumer's `/agent-bus/status` answered from a follower worker
reports `running: false`. The endpoint reports the *answering process*, not the
cluster. INFERENCE: any operator reading it gets a coin-flip answer on a 2-worker
deployment.

**FACT:** 39 events orphaned (created before the consumer's cutoff, never
claimed) for 85 hours; drain rate 0 in the last hour while 178 other events
completed the same day. The design ("no mass replay", resume at watermark,
24h max catch-up) is correct for restarts and wrong for the case where the
consumer was alive but the events were created in a window it never covered.

**Failure containment:** every module degrades ("best-effort everywhere",
"a missing table degrades to a logged warning"). This is the platform's
dominant reliability philosophy, and it has a cost: **degradation is silent by
design in ~40 places** (`logger.debug` on swallowed exceptions in supervisor,
trace, critic, grounding). `trace._rows` was fixed to record source failures;
the same fix has not propagated.

**Concurrency:** 214 `conn.commit()` sites across 73 modules. Transaction
boundaries are per-function, not per-business-action. `governance.approve()`
spans three transactions (set approved → dispatch → set executed/failed) with no
compensating action if the process dies between the second and third; the row
stays `approved` forever and is neither pending nor executed.

**Idempotency:** strong where it was designed in (`order_notifications` UNIQUE,
escalations partial unique, bus claim), absent where it was not (approvals,
supervisor proposals dedupe by 12h window only).

---

## 10. Retrieval / RAG assessment

Pipeline (VERIFIED): query → FTS (Postgres) + semantic (OpenAI embeddings,
in-process cosine; pgvector column written but read path still numpy) →
reciprocal-rank fusion → audience gate (fail-closed) → context block →
model → outbound guard.

**FACT:** 198 approved KB articles on Railway (187 public, 11 internal).
`content_embeddings` 12,998 rows. `memory_retrievals` records every retrieval
with correlation id (12,241 rows locally; 4 on Railway — production barely
exercises the memory path).

**FACT (from `kb_coverage_audit` memory, not re-run here):** golden set of 163
questions; coverage 8% → 83%; **7 of 10 out-of-scope questions still receive
confident grounding** — the floor never fires. `knowledge.py` documents why: the
0.33 similarity floor sits below where real answers live, and raising it discards
genuine traffic.

**INFERENCE:** the system does *not* reliably know when it lacks evidence. The
audit instrument exists (four verdicts, FALSE_COVERAGE kept separate — the right
design) but its output is not a gate and not persisted. `eval_suite.run_gate()`
tests retrieval *accuracy on in-scope questions* (top-2 ≥ 0.85), which is the
easy half; abstention on out-of-scope questions is measured by a different tool
and gates nothing.

**Determinism:** FTS is deterministic; embeddings are cached by content hash;
RRF is deterministic. Ranking stability under corpus growth is unmeasured
(`content_index` documents that a recency window once hid 69% of the corpus —
caught by the author, not by a test).

---

## 11. Red-team findings (this review)

| Scenario | Method | Result | Fail mode |
|---|---|---|---|
| Anonymous NL write on production | `structuredIntent` archive | 401 | **closed** |
| Anonymous read of PII on production | "list contacts" | 50 emails returned | **open by policy** |
| Prompt injection to reveal prompt / delete | direct instruction | refused, no DB call | **closed** |
| Invalid mode via structured intent | `mode: delete` | rejected by sql_builder validation | closed |
| Approval double-execute | code read: `approve()` = read, check, unconditional `UPDATE … SET status` | two concurrent approves of the same pending row both pass the check | **open** (INFERRED; not executed against live data) |
| Duplicate bus delivery | code read: SKIP LOCKED + 5-min stale reclaim; handlers idempotency-guard | closed for order notifications; **handler > 5 min runs twice** for non-idempotent handlers | partially open |
| Worker crash mid-approval | code read | row stranded in `approved` | **silent** |
| Events created before consumer cutoff | production | 39 orphaned 85 h; alert fired once then went silent | **silent after 24 h** |
| Policy edit lowers `gov.act_min` | code read | supervisor AUTOACT flips from propose to execute with no second gate | **open** |
| Deployment during execution | Railway restart 03:30 UTC ×2 (documented) | job_ledger replays cron; in-flight bus rows reclaimed after 5 min | closed |
| Provider failure | `llm_router` | one attempt per provider; deterministic fallback; free-tier alt refused customer data | closed |
| Migration failure | `migrate.py` per-file checksum, `--check` | detects missing/changed; **cannot detect applied-and-unrecorded** (documented) | partially open |
| Hallucinated entity id | a2a → SP | SP raises; recorded `failed` (server error) rather than `rejected` | mislabelled |
| Self-approval | `red_team.attack_self_approval` | blocked (per script) | closed |
| Data exfiltration via custom agent | reach_invariant | external agents public tier only; verified live once | closed |

---

## 12. Production-vs-repository drift

| Invariant | Expected | Observed | Evidence | Gap | Risk | Remediation |
|---|---|---|---|---|---|---|
| Deployed commit = master | yes | `cfaef177` = `origin/master` HEAD | `/health.commit` | none; local `master` is 18 behind | low | pull |
| Migrations current | both | both "current"; Railway +1 catch-up, 2 blank checksums; `/deploy/migrations ok:false` | migrate --check, endpoint | semantic split documented, unresolved | medium | implement §1 of migration_integrity_spec |
| Event bus drains | yes | 39 orphaned 85 h, drain 0 | `/platform/health` | consumer cutoff semantics | **high** | §18 stage 2 |
| Guardrail layer 4 configured | yes | `allowed_callers` NULL 45/45 | DB | never seeded | medium | seed from `a2a_from_agents` evidence |
| Policy-as-data | rows | 0 rows both | DB | unused | low | fine; document |
| Capability grants (U4) | in use | 0 rows both | DB | feature unexercised | low | fine; do not claim |
| Field history | all entities | 5 rows Railway | DB | cases only | **high** | §18 stage 3 |
| Consensus attestation | replicas attest | 0 attestations | `/deploy/consensus` | never wired | low | wire or delete |
| Status endpoints cluster-aware | yes | per-worker | `/agent-bus/status` | 2-worker deployment lies | medium | read state from DB, not process |
| Supervisor alerts owned | yes | none owned | events | no assignee/closure | **high** | §18 stage 3 |

---

## 13. Competitive architecture comparison

Not a feature checklist. Architectural capability, judged on evidence.

| Capability | Salesforce / Agentforce | Dynamics 365 | ServiceNow | Conscestra (measured) |
|---|---|---|---|---|
| Authorization independent of the model | Yes (platform permissions) | Yes | Yes | **Yes — enforced in PostgreSQL, verified in prod** |
| Row-level security / sharing model | Mature, complex | Mature | Mature | **Absent** (one role, no RLS) |
| Field-level audit | Field History Tracking (limited fields) | Audit log per attribute | Audit | **Cases only** |
| Governed AI writes (propose → approve → execute) | Agentforce actions with approvals (add-on flows) | Copilot with confirmation | Now Assist with approvals | **Native, typed, critic-reviewed, correlated, partly undoable** |
| Independent critic on every proposal | No | No | No | **Yes, deterministic** |
| Correlation trace across AI → action → DB | Partial (Event Monitoring, paid) | Partial | Partial | **Yes, one id, one endpoint** |
| Outcome honesty ("accepted" not "sent") | No (Sent status common) | No | No | **Yes, by CHECK constraint** |
| Startup refusal on unsafe config | No | No | No | **Yes** |
| Multi-tenancy | Core | Core | Core | **Deferred** |
| Extensibility (metadata-driven objects) | Core | Core | Core | Custom fields + custom agents as data; **no custom objects** |
| Workflow engine | Flow (mature) | Power Automate | Flow Designer | Bus + sequences + playbooks-as-data; **no visual designer, no compensating transactions** |
| Observability (traces, metrics, alerts) | Platform | Platform | Platform | Self-observability endpoint; **no OTel export, alerts unowned** |
| Testing at scale | Testing Center | — | ATF | eval_suite (deterministic paraphrases); **retrieval abstention not gated** |
| Data residency / compliance posture | SOC2, region choice | SOC2 | SOC2 | Trust page, DSAR manifest-checked, **no third-party attestation** |
| Scale | global | global | global | **1 node, 2 workers, demo traffic** |

**Where Conscestra is genuinely differentiated (VERIFIED):**

1. The **write boundary**: LLM never emits SQL, role enforced by a database
   read-only transaction, principal required, outcome classified honestly.
2. **Governed-action primitives as first-class objects**: proposal, critique,
   correlation, undo, expiry, and a truthful principal category on the decider.
3. **Provider-acceptance honesty** on outbound mail, enforced by schema.
4. **Self-verifying operations**: release guard, red team that executes,
   backup that restores, CI gate that cannot be skipped, attestation of schema changes.
5. **Engineering culture encoded in code**: every control documents the failure that
   motivated it. This is rarer than any feature.

**Where Conscestra is behind (VERIFIED):**

1. Authorization depth: no rep/manager tiers, no row scoping, no RLS.
2. Audit depth: no field-level history outside cases; no actor on `audit_log`.
3. Operational governance: the queue is not worked; alerts are not owned.
4. Reliability under load: unproven; single node; in-process singletons.
5. Extensibility: no custom objects, no workflow designer, no compensation.
6. Observability export: no OTel/Prometheus; health is a JSON endpoint.
7. Third-party assurance: none.

---

## 14. Architectural moat analysis

**Can "governed AI orchestration of business operations" be a durable
architectural category?** INFERENCE: yes, but the moat is narrower than the
marketing states and sits in a different place.

The moat is *not* "AI agents in a CRM" — every incumbent has that. It is *not*
orchestration — LangGraph and its peers commoditise that. The defensible
primitive is the **governed action record**: a single object that carries
intent, principal category, confidence, critique, authorization decision,
correlation, execution outcome (honestly classified), reversibility, and expiry
— and a database that refuses to let any component skip it.

Conscestra has most of that object today. What would make it a moat:

1. **Make it the only way anything consequential happens.** Today it is one of
   three write boundaries. The other two are declared, not governed.
2. **Make it human-owned by construction.** A governed action with no eligible
   human owner should be *unroutable*, not expired. E2 is the predicate; it must
   become a write-time invariant on proposals and alerts.
3. **Make it replayable.** Persist the model input hash, the retrieval set (already
   done), the route decision and its version, so an action can be re-derived.
4. **Make it exportable.** A signed, per-action audit bundle an auditor can read
   without the platform is what enterprise buyers pay for.

If those four hold, the category claim is credible and hard to copy quickly,
because incumbents cannot retrofit "refuses to act without an accountable owner"
onto sharing models built for humans.

---

## 15. Complete defect register

Severity: P0 existential · P1 critical · P2 significant · P3 important · P4 enhancement.

| ID | Sev | Finding | Root cause | Evidence | Business risk | Architectural impact | Fix |
|---|---|---|---|---|---|---|---|
| D-01 | **P0** | Production serves all contact PII to anonymous callers | `public-read` posture chosen for marketing; corpus provenance unrecoverable | probe: 50 emails/23 phones; `corpus_provenance.py` | breach on first real record; no enterprise buyer accepts | trust model inverted (read ungoverned) | DECISION: locked posture + demo tenant with synthetic-only corpus |
| D-02 | **P0** | Business records have no accountable human owner | `owners` = mixed contacts/employees; no eligibility on write; orders.owner_id write-path regression | 39/51 owners are contacts; 97.5% orders unowned; 109 alerts unowned | "AI did it" has no human counterpart; routing to customers | accountability chain broken at the root | E2 predicate becomes a write-time invariant on proposals, alerts, and ownership columns; separate `staff` identity space |
| D-03 | P1 | Governance queue is not operated; 89% expire | machine-generated recurring proposals to a single human; no delegation, no batching, no default policy | Railway approvals table | governance is nominal; expiry becomes the policy | trust claim unsupported by operation | policy tiers: auto-approve classes with sampled review (reuse `verification_policy` tiers); route by executive profile; SLA on approvals |
| D-04 | P1 | 18 shipped orders without notification; bus orphaned 85 h; alert silent after 24 h | cutoff semantics + detector window bounded to 24 h + no alert ownership | `/platform/health`, DB | customer promises missed silently | event fabric cannot be trusted as a workflow substrate | orphan = failure state with owner; detector unbounded on age; alert object with assignee/deadline/closure |
| D-05 | P1 | Field-level history absent outside cases; `audit_log` has no actor | history written only by `cases._mutate`; SPs write entity-level `audit_log` | 5 rows Railway | cannot reconstruct who changed what | auditability claim overstated | one `_mutate`-style boundary per entity, or a trigger-based `record_field_history` with actor from `SET LOCAL app.principal` |
| D-06 | P1 | Approval execution is not atomic or race-safe | read-check-then-unconditional UPDATE across 3 transactions | `governance.approve/_set` | duplicate execution of non-idempotent actions; stranded `approved` rows | write-path idempotency incomplete | `UPDATE … WHERE status='pending' RETURNING`; execution claim row; stranded-row sweeper |
| D-07 | P1 | Supervisor AUTOACT live behind a single tunable threshold | `gov.act_min` policy row can silently convert propose → act | code | unreviewed execution of dunning/hot-lead plays | single point of policy failure | AUTOACT executions require a per-action-type explicit `auto_execute=true` policy row, never a numeric threshold alone |
| D-08 | P1 | Production superuser DSN on developer laptop; laptop was a live sender | `.env` convenience | `.env`, `autosend_allowed` docstring | credential compromise; duplicate sends | deployment boundary porous | rotate; laptops get `crm_app`-tier read-only creds; superuser via short-lived access only |
| D-09 | P2 | 70% of tables without FKs; 0 RLS | migrations create tables without constraints | catalog | latent orphans as tables grow | integrity by convention | constraint policy for new tables; `verify_invariants` coverage |
| D-10 | P2 | Migration integrity split: two checks, two answers | `migrate --check` vs `deploy_state.check_migrations` measure different things | endpoint vs CLI | operator confusion; false green | spec exists, DESIGN ONLY | implement the 3-axis model |
| D-11 | P2 | Status endpoints are per-worker | in-process state reported over HTTP | `/agent-bus/status running:false` | operators misread health | observability lies | derive from DB (`agent_bus_watermark`, leader lock holder) |
| D-12 | P2 | Retrieval abstention not gated; 7/10 out-of-scope grounded | floor cannot separate distributions; audit not persisted | `knowledge.py`, coverage audit | confident wrong answers to customers | RAG "knows when it doesn't know" unproven | persist audit runs; gate FALSE_COVERAGE ≤ threshold; per-query evidence score surfaced to guard |
| D-13 | P2 | One real role (`admin`); scoping deferred | no rep sessions exist | `auth_sessions` | no least privilege for staff | authorization is binary | introduce `member`/`viewer` in practice + owner-scoped predicate wired |
| D-14 | P2 | Silent degradation at DEBUG in ~40 places | best-effort philosophy | grep | failures invisible | observability gap | promote to WARNING with a counter; `platform_health` reads the counter |
| D-15 | P2 | No OTel/metrics export; alerts stay in-app | none wired | pip list | no pager; nobody woke for D-04 | ops maturity | export `/platform/health` metrics; page on critical |
| D-16 | P3 | `allowed_callers` never seeded | evidence-first approach, never followed through | DB | layer 4 inert | guardrail claim overstated | seed from 30-day `a2a_from_agents` |
| D-17 | P3 | `postdoploy_verify` self-description contradicts itself | doc drift | argparse vs header | run against prod thinking read-only | none | fix text; add `--read-only` mode that skips red team |
| D-18 | P3 | Eval results not persisted | supervisor alert only | `evals.py` | no prompt-change trend | prompt governance incomplete | `eval_runs` table keyed by prompt/model hash |
| D-19 | P3 | Consensus attestation never wired | 0 attestations | `/deploy/consensus` | none | dead control | wire at startup or delete |
| D-20 | P3 | `structuredIntent` accepted from request body | cutover shortcut | router models | bypass of operation resolver by writers | none today | strip at edge; set only by orchestrator |
| D-21 | P4 | pgvector column written, numpy read path | parity gate | `content_index.py` | none | fine | measure parity, switch |
| D-22 | P4 | Local `master` 18 commits behind | branch hygiene | git | none | none | pull |

---

## 16. Root-cause analysis

Consolidated: the 22 defects share **five** root causes.

| Root cause | Defects | Invariant that was missing |
|---|---|---|
| **RC-1 Ownership was never a first-class identity.** Owners share a key space with customers; eligibility is a read-side predicate. | D-02, D-04 (alerts), D-05 (actor), D-13 | *No consequential record, proposal or alert may exist without an eligible human owner, or an explicit "unowned" state that is itself an alert.* |
| **RC-2 Governance was built as mechanism, not as an operated service.** No SLA, no delegation, no default policy per class. | D-03, D-07, D-16 | *Every proposal class has a declared decision policy (human / sampled / auto) and a decision SLA; expiry is a breach, not a decision.* |
| **RC-3 "Best-effort everywhere" made failure silent.** Correct for customer-facing paths; wrong for control paths. | D-04, D-11, D-14, D-15, D-19 | *A control that degrades must emit a counted, owned signal; a status endpoint must report cluster state from durable storage.* |
| **RC-4 Write boundaries are enumerated, not unified.** Three paths; field history and atomicity depend on which one was used. | D-05, D-06, D-09, D-20 | *One mutation primitive per entity (lock → validate → write → history → commit), reachable from SP, a2a, and direct DML alike; actor stamped from session GUC.* |
| **RC-5 Demo posture leaks into production semantics.** | D-01, D-08, D-10 | *Production configuration is derived from an environment class, never from what the demo needed.* |

**Previous recommendations that were wrong, stated plainly:**

- The 2026-07-18 audit scored "Security & governance ✅" and "Safe DB access ✅"
  on the strength of mechanism. This review finds the mechanism sound and the
  operation absent; a ✅ was premature.
- The 2026-08-23 review answered "Is the truth boundary reliable? PARTIALLY —
  flattened at the audit boundary." That was fixed (outcome column) and is now
  VERIFIED. Credit where due.
- The 2026-07-23 blindspots pass marked #7 "Scale / HA — DONE (leader election +
  doc)". Leader election is done; "scale/HA" is not, and the status endpoints
  that would tell an operator the truth report per-worker state. DONE was overstated.
- Memory note "get_connection is connect-per-call (no pool)" is stale: a pool
  exists (`pool_max: 16`, verified on `/health`).

---

## 17. Target architecture

**CURRENT → TARGET → OPTIONAL FUTURE**, per layer. Only what the evidence justifies.

| Layer | CURRENT | TARGET | OPTIONAL FUTURE |
|---|---|---|---|
| System | single FastAPI, 2 workers, in-process singletons on leader | same shape; singletons as a separate worker process with the same leader lock; status from DB | separate bus worker service |
| Agents | 12 governed endpoints + supervisor/planner/bus/critic/custom | unchanged topology; each agent declares owner-eligibility requirements for its writes | agent marketplace — no |
| Governance | proposal → critic → route → human/expire | + decision policy per action class (human / sampled / auto with explicit row), decision SLA, breach alert with owner, batch decisions, delegation | ML-assisted triage |
| Authorization | admin + posture + RO txn | + rep/manager roles; owner-scoped predicate wired; RLS on entity tables keyed by session GUC; principal stamped as GUC for triggers | ABAC |
| Data | SPs + triggers; 30% FK coverage | constraint policy; `staff` identity space split from `contacts`; owner columns FK to staff; `verify_invariants` covers links | custom objects |
| Events | events → queue → leader consumer, cutoff + watermark | orphan = failure state with owner; consumer coverage proof per window; detector unbounded; replay endpoint audited | external broker |
| Audit | a2a rows + approvals + audit_log (no actor) + field history (cases) | field history on every entity via one mutation primitive; actor on audit_log; signed per-action audit bundle export | WORM store |
| Retrieval | FTS + embeddings + RRF + audience gate | persisted coverage audit; abstention gated; evidence score passed to guard; pgvector read path after parity | graph retrieval — not yet |
| Workflow | bus handlers + sequences + playbooks | compensating action per write capability (undo coverage 16/16) or explicit "irreversible" declaration surfaced to approver | visual designer — no |
| Observability | `/platform/health`, `/trace`, health JSON | OTel export, pager on critical, degradation counters, cluster-aware status | SLO dashboards |
| Deployment | push master → Railway; CI gate; manual postdeploy_verify | postdeploy_verify runs automatically and records to DB; feature verification not SHA match; PITR | blue/green |
| Human escalation | escalations (customer) with SLA | same object for internal alerts; every supervisor alert has assignee + deadline + closure | on-call rotation |

---

## 18. Remediation roadmap

Ordered by trust → integrity → authorization → accountability → reliability →
production → AI safety → observability → performance → differentiation.

### Stage 0 — Decide the production posture (1 week)
- **Objective:** remove D-01 as a category.
- **Change:** `locked` on Railway; a separate demo deployment with a synthetic-only
  corpus and a provenance flag that refuses real records.
- **Tests:** `test_public_read_posture` inverted for prod class; release guard refuses `public-read` when `APP_ENV=production`.
- **Production evidence:** anonymous `/contact-chat` → 401.
- **Exit:** no PII reachable without a session. **Rollback:** posture flag.
- DECISION REQUIRED · HUMAN ACTION REQUIRED (rotate the superuser DSN, D-08).

### Stage 1 — Accountable ownership (3–4 weeks)
- **Objective:** RC-1. Defects D-02, D-05 (actor), D-13.
- **Change:** `staff` identity table separate from `contacts`; owner columns
  reference it; E2 predicate enforced at proposal/alert creation and on owner
  writes; principal stamped as `SET LOCAL app.principal` so triggers can record actor.
- **Tests:** mutation tests that an ineligible owner is rejected by the constraint (name it); alert without owner is unroutable.
- **Production evidence:** `owners` with contacts = 0; 100% of new alerts owned.
- **Exit:** ownership NULL rate on new orders < 1%. **Rollback:** feature flag on the check, constraint kept NOT VALID until backfill.

### Stage 2 — Event fabric you can trust (2 weeks)
- **Objective:** D-04, D-11, D-14, D-15.
- **Change:** orphan state with owner; detector age-unbounded; cluster-aware status from DB; degradation counters; OTel export and pager.
- **Production evidence:** zero orphaned > 1 h for 14 days; alert acknowledged within SLA.

### Stage 3 — Governance as an operated service (3 weeks)
- **Objective:** RC-2. D-03, D-06, D-07, D-16.
- **Change:** decision policy per action class; atomic claim on approve; stranded-row sweeper; AUTOACT requires explicit per-type policy; `allowed_callers` seeded.
- **Production evidence:** expiry rate < 10%; 0 stranded rows; every executed action names a human or a named policy with a human owner.

### Stage 4 — One mutation primitive (4–6 weeks)
- **Objective:** RC-4. D-05, D-09, D-20.
- **Change:** `_mutate`-style boundary for accounts/contacts/opportunities/orders/leads; field history everywhere; constraint policy; strip `structuredIntent` at the edge.
- **Production evidence:** `record_field_history` rows per day > 0 for every entity with writes.

### Stage 5 — Retrieval that abstains (2 weeks)
- **Objective:** D-12, D-18.
- **Change:** persist coverage runs; gate FALSE_COVERAGE; evidence score to outbound guard.
- **Production evidence:** out-of-scope grounding rate ≤ 10% on the golden set.

### Stage 6 — Migration integrity and deployment proof (2 weeks)
- D-10, D-17, D-19. Implement the 3-axis model; postdeploy_verify recorded in DB; PITR enabled.

Stages 1–3 are the ones that change the verdict. Nothing here is a rewrite.

---

## 19. Readiness gates

A gate passes only when the named evidence exists in production.

| Gate | Passes when |
|---|---|
| **Architecture Ready** | One mutation primitive per entity is designed and the three write boundaries are reduced to two (SP + a2a) with direct DML limited to declared background modules. Owner identity space is separate from customers. |
| **Development Ready** | Every write capability declares `params_schema`, an undo handler or `irreversible=True`, and an owner-eligibility requirement. CI gate runs the control subset plus the mutation tests for Stage 1. |
| **Test Ready** | Full suite runs against a populated CI database twice green. Coverage audit and eval suite persist results. Red team includes approval double-submit and orphaned-event scenarios. |
| **Security Ready** | Production posture `locked`; no superuser credential on any workstation; RLS on entity tables; third-party pentest report for the public surfaces (widget, SDR, portal, voice). |
| **AI Governance Ready** | Every action class has a decision policy; expiry rate < 10%; AUTOACT per-type explicit; `allowed_callers` non-null for all writes; every executed action resolves to a human or a named policy with a human owner; abstention gated. |
| **Production Ready** | Zero orphaned events > 1 h over 30 days; pager on critical; postdeploy_verify recorded per deploy; PITR enabled; cluster-aware status. |
| **Enterprise Ready** | Rep/manager roles in use; field history on all entities; signed audit bundle export; SOC 2 Type I or equivalent; multi-tenant isolation proven for at least schema-per-tenant; load test at 10× current traffic with p95 targets. |
| **World-Class Ready** | Everything above, plus: an external auditor reconstructs any consequential action from the audit bundle without platform access; 90 days with governance SLA met and zero unowned consequential actions; replayable route decisions; published incident postmortems. |

---

## 20. Decisions required from the owner

1. **Production posture.** Keep `public-read` on the same deployment that
   holds possibly-real data, or split demo from production. (D-01)
2. **AUTOACT.** Keep supervisor/objectives auto-action live behind one numeric
   threshold, or require explicit per-type policy rows. (D-07)
3. **Governance operating model.** Who works the queue, with what SLA, and
   which classes may be sampled or auto-approved. (D-03)
4. **Ownership identity.** Split `staff` from `contacts` (breaking change to
   owner columns) or keep the mixed table with an enforced eligibility layer. (D-02)
5. **PITR.** A billing decision that closes the 24 h RPO gap. (Stage 6)

## 21. Human actions required

- Rotate the Railway superuser password; remove it from every `.env`. (D-08)
- Drain or explicitly cancel the 39 orphaned Railway events and send or waive
  the 18 missing shipped notifications. (D-04)
- Decide the 2 pending Railway approvals and the 8 pending local ones before they expire.
- Pull `origin/master` locally (18 commits).
- Fix the `postdeploy_verify` self-description before anyone runs it against Railway. (D-17)

---

## 22. World-class scorecard

| Dimension | Score /10 | One-line justification |
|---|---|---|
| CRM architecture | 6 | clean spine, SP discipline; 70% of tables unconstrained; no custom objects; no decision history |
| AI architecture | 7 | LLM never emits SQL; structured intent authoritative; retrieval abstention unproven |
| Agent orchestration | 6 | routing + dispatch + bounded planner; no conflict resolution, no replay, alerts unowned |
| Governance | 5 | best-in-class mechanism; unoperated (89% expiry, layers unconfigured) |
| Security | 5 | fail-closed write path verified in prod; public-read PII; one role; no RLS; creds on laptop |
| Data integrity | 5 | 0 orphans on spine; ownership NULLs; no field history; mixed owner identity |
| Reliability | 6 | leader election, job ledger, idempotent notifications; orphaned events, non-atomic approvals, single node |
| Observability | 6 | correlation trace and self-health are real; per-worker lies, no export, no pager, silent degradation |
| Auditability | 6 | a2a + approvals + honest outcomes; audit_log without actor; field history cases-only |
| Enterprise readiness | 3 | single tenant, one role, no attestation, demo traffic |
| Scalability | 4 | pool + leader; in-process singletons; unmeasured |
| Product differentiation | 7 | governed-action record and outcome honesty are real and rare |

## 23. Direct answers

**1. Governed AI-agent CRM, or AI-enabled CRM?** AI-enabled CRM with a governed
write boundary. It becomes a governed AI-agent CRM when the governance is
operated (Stage 3) and every consequential action has an accountable human (Stage 1).

**2. Single biggest architectural weakness.** Ownership is not an identity. The
`owners` table is a mixed population of customers and staff, eligibility is a
read-side predicate that enforces nothing, and therefore no downstream layer —
routing, alerts, approvals, audit — can name the human accountable for most
records.

**3. Strongest architectural advantage.** The deterministic write boundary: the
model never emits SQL, authorization is enforced by a PostgreSQL read-only
transaction rather than by a prompt or a blocklist, every write carries a
frozen principal, and outcomes are classified honestly. Verified fail-closed
against production in this review.

**4. Defensible moat.** The governed-action record as the *only* path to any
consequential effect, human-owned by construction, replayable, and exportable
as a signed audit bundle. §14.

**5. Fix before claiming enterprise readiness.** D-01, D-02, D-03, D-04, D-05,
D-06, D-08; rep/manager roles; field history; PITR; cluster-aware status.

**6. True before claiming world-class.** §19 last row: external reconstruction
of any action from the audit bundle; 90 days of met governance SLA with zero
unowned consequential actions; replayable routing; third-party assurance;
measured scale.

**7. Do not build yet.**
- Multi-tenancy beyond the seam (no second tenant exists).
- An external message broker (the queue is 39 rows; the defect is semantics, not throughput).
- A visual workflow designer.
- Graph retrieval or a second vector store (parity on pgvector first).
- More agents, capabilities, or channels. Sixteen write capabilities with a
  governance queue nobody works is already too many.
- Zero-gap HA failover (single node; solve status truthfulness first).
- Any new "blindspot" feature until Stages 1–3 are in production.

**8. Highest-leverage decision, 12–24 months.** Make the governed-action
record the sole mutation primitive and require an eligible human owner on it.
Every other weakness — audit depth, alert ownership, approval operation, role
scoping — becomes a property of that one object instead of a separate project.

---

## 24. Final recommendation

**Do not rewrite. Do not add features. Operate what exists and close the
accountability gap.**

The architecture is coherent and, at the write boundary, better than the
platforms it is compared against. Its weakness is not design; it is that the
governance and accountability layers were built to a demo's traffic and a
single operator's attention, and the production evidence shows what happens
under those conditions: proposals expire, alerts go unowned, shipped orders go
unnotified, and the platform's own health page reports "critical" to no one.

Stages 0–3 (roughly ten weeks) change the verdict from "AI-enabled" to
"governed". Stages 4–6 make the audit claims true at field level and close the
retrieval and migration gaps. Only after that does "enterprise" become an honest
word, and "world-class" requires the external, measurable evidence in §19.

*Appendix A (test-suite result) follows.*

---

## Appendix A — Test-suite result (local, 2026-09-05)

Command: `python -m pytest governance/tests -q` against the local database
(memory benchmark excluded; it runs in CI).

```
25 failed, 2752 passed, 1 skipped in 236 s
```

| Failures | File | Cause (FACT, from the assertion text) |
|---|---|---|
| 21 | `test_order_lifecycle_notifications.py` | `assert 'queued' == 'accepted'` — the 2026-09-04 `autosend_allowed()` change requires `is_deployed()`; on a laptop the provider is never called, so every send-path assertion fails. The fix was right; the suite was not updated (or needs `AGENT_BUS_AUTOSEND_LOCAL=1`). |
| 2 | `test_email_send_sp.py` | `'rejected' == 'accepted'`; same gate. |
| 1 | `test_hybrid_retrieval.py` | frozen recency-only output changed (`payment reminder` 4 → 1): corpus drift, not code — the pin needs a decision. |
| 1 | `test_work_ownership.py::test_K_H` | "the live surface must not be the size of the historical population" — a real invariant on the ownership work in progress. |

**What this proves about the gate.** CI runs 12 control files. None of these 25
tests is among them, so the branch under review is green in CI and red on a
developer machine. The workflow header says exactly this ("the remainder is run by
a person against a populated database"), which is honest — and it means the
platform's green tick currently certifies about 1 test in 200. That is a
Test-Ready gate failure under §19, not a surprise.

RECOMMENDATION: make the deployment gate injectable (`AGENT_BUS_AUTOSEND_LOCAL=1`
in `pytest.ini` env, or a fixture), re-pin the retrieval snapshot as a recorded
decision, and resolve K_H before merging the branch.
