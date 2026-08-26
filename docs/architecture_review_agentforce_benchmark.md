# Conscestra CRM — Architecture Review, benchmarked against Agentforce

**Hostile architecture review, 2026-08-23.** Reconstructed from the repository,
not from documentation. 265 Python modules, 110,971 lines. Every claim below
names its evidence.

Salesforce Agentforce is used as a **reference model**, not a specification.
Several of its principles are rejected here as inappropriate for Conscestra's
scale, and in three places Conscestra's architecture is judged **stronger**.

---

## Executive verdict

| | |
|---|---|
| **A. Is the architecture fundamentally sound?** | **YES** |
| **B. Is the multi-agent architecture credible?** | **PARTIALLY** — credible as a governed capability mesh; not as autonomous agents |
| **C. Is the Orchestrator a true orchestration layer?** | **PARTIALLY** — a router + dispatcher + opt-in planner front-end. Not a supervisor |
| **D. Clean Agent → Action → deterministic boundary?** | **YES** — the strongest part of the system |
| **E. Is authorization independent of LLM reasoning?** | **YES** — enforced in Postgres, not in a prompt |
| **F. Is the truth boundary reliable?** | **PARTIALLY** — correct in memory, **flattened at the audit boundary** |
| **G. Can it scale to many more agents/workflows?** | **WITH CONDITIONS** — see P1-1 and P1-2 |
| **H. Does it need major Agentforce-inspired change?** | **NO.** Four targeted fixes, none of them structural |

---

## 1. The architecture, as built

```
CUSTOMER / STAFF
  web chat · store widget · voice (Telnyx) · email (IMAP) · Slack/Teams · MCP client
      │
      ▼
INTERACTION LAYER
  app/main.py (FastAPI, 2,374 ln) · auth_dep · write_guard ContextVars
  app/core/transports.py · telephony.py · voice_stream.py · portal.py
      │  auth: session/admin token/HMAC · authz: role + channel stamped here
      ▼
ORCHESTRATION
  app/agents/orchestrator/router.py (798 ln)
    ├─ symphony workflows   predefined fan-out (daily briefing, weekly report)
    ├─ intent_router.aroute LLM intent → endpoint; keyword _route_single fallback
    ├─ planner.draft_plan   multi-step goal → PREVIEW (never auto-executed)
    ├─ structured_cutover   resolves a WRITE into typed params before dispatch
    └─ a2a.dispatch         capability-routed, correlated
      │
      ▼
CAPABILITY MESH (the action layer)
  app/core/a2a.py (1,209 ln) — 45 capabilities, 16 write, 31 typed (sp=)
    gates, in order: registry enabled → allowed_callers → governance confidence
                     → HITL amount floor → structured|prose branch
      │
      ▼
SPECIALIST AGENTS  (12 CRM + email + voice + store)
  per agent, uniformly: pre_router · sql_builder · graph · prompt · formatter · router
      │
      ▼
DETERMINISTIC EXECUTION
  execute_sp() → write_guard.guard_query() → PostgreSQL SPs (~165)
  order_notifications · staff_email · governance · escalation
      │
      ▼
EXTERNAL
  Resend/SES · Telnyx · Azure Speech · Gemini/Anthropic · MCP
```

**Cross-cutting:** `agent_bus.py` (event consumer, 1,838 ln), `governance.py`
(approvals, 1,512 ln), `trace.py` (correlation stitcher), `leader.py` (HA
singleton), `release_guard.py` (startup controls).

### Implementation status, by evidence

| Component | Status | Evidence |
|---|---|---|
| A2A capability mesh | **active** | 3,895 dispatch rows local / 466 Railway |
| Event-driven bus | **active** | `event_queue` 1,902 · `workflow_runs` 6,592 |
| Governance approvals | **active** | `action_approvals` 114 local / 55 Railway |
| Planner | **active, opt-in** | requires `plan:` token or `INTENT_PLAN_ROUTING` |
| Semantic retrieval | **active** | `content_embeddings` 11,723 · `kb_embeddings` 210 |
| Capability RBAC (`allowed_callers`) | **DORMANT** | `capability_registry` **0 rows** on both DBs |
| Operator capability kill-switch | **DORMANT** | same table, 0 rows |
| Agent versioning (U2) | **not deployed** | `agent_versions` **absent** on both |
| Agent capability grants (U4) | **dormant** | `agent_capability_grants` 0 rows |
| Guardrail policy overrides | **not deployed** | `guardrail_policies` absent |
| Customer memory | **partial** | `interaction_memories` 3 rows |
| Staff email | **dark by design** | `APPLY=0`, ledger 0 rows |

---

## 2. Where Conscestra is architecturally STRONGER than the Agentforce model

These are not politeness. Each is a boundary Agentforce's published architecture
does not draw as sharply.

### S1 — The truth boundary is an explicit architectural invariant

`a2a.py:76-108` defines four outcomes and states the rule:

> *"THE INVARIANT: there is no path here in which the ABSENCE of an error
> produces ACCEPTED. Acceptance requires a 2xx — a positive statement from the
> server — plus a body that is readable and does not itself report failure."*

Agentforce's action framework reports success/failure; it does not architect a
distinction between *refused*, *failed*, and *we could not tell*. Conscestra
does, because it was burned: 25 activities recorded "payment reminder sent" for
sends that returned 403. `UNKNOWN` exists precisely so "we cannot tell" stops
being silently coerced into "it worked."

**This is the single most valuable architectural decision in the codebase.** It
is also the hardest thing to retrofit into a system that did not start with it.

### S2 — Deterministic-first agents; the LLM is the last resort

Every agent carries the same rule (`accounts/pre_router.py:7-10`):

> *"Every SP mode that accepts deterministic parameters MUST be handled here.
> The AI Agent is invoked ONLY as a last resort for free-form natural-language
> queries that cannot be structured here."*

Measured ratio of deterministic code to prompt, per agent:

```
accounts       pre_router 576 + sql_builder 259 + formatter 629  vs  prompt 280
leads          624 + 291 + 1098                                  vs  302
orders         733 + 351 + 778                                   vs  352
opportunities  588 + 280 + 1163                                  vs  247
```

**Roughly 6:1.** Agentforce's Topics/Instructions model puts far more behaviour
into natural language. Conscestra's ratio means cost, latency and
nondeterminism do not scale linearly with usage — a structural advantage that
compounds.

### S3 — Every write capability is typed; writes never travel the NL path

45 capabilities: 31 typed (`sp=`), 12 prose-only, 2 composite. **All 16 write
capabilities are typed** — the 12 prose-only are reads. A write therefore never
reaches the database through a rendered English sentence.

### S4 — The planner proposes writes; it does not execute them

`planner.py:14-24`: reads execute immediately (side-effect free); **write steps
are never executed — each becomes a governance proposal**, critic-reviewed,
routed to an executive, one-click decidable, undoable. Bounds are hard:
`MAX_STEPS 6`, `MAX_WRITES 2`, and the LLM sees only the registered capability
manifest — *"the planner never improvises around its vocabulary."*

Agentforce's Atlas reasoning engine plans **and executes**. Conscestra's choice
is more conservative and, at this scale, better: it makes the blast radius of a
bad plan equal to the blast radius of a bad suggestion.

### S5 — Authorization is enforced by the database, not by a mode list

`write_guard.py` is explicit that its own blocklist is untrustworthy:

> *"WRITE_MODES is a blocklist and is known to be incomplete (log_call,
> add_note, create_task, checkout, bulk_adjust_stock … all write but are
> absent), so a public channel must not depend on it. The database refuses the
> write whatever the mode is called."*

The read-only channel opens a **PostgreSQL read-only transaction**.
`customer_scope` goes further and **fails closed** — while set, `execute_sp`
refuses *all* SP access, forcing the verified-customer tier through explicitly
account-scoped parameterised queries with the account id injected from the
scope, *"never from anything the caller said."*

That is defense-in-depth with the innermost layer outside the application's
control. It is stronger than a permission check in application code.

---

## 3. P0 / P1 / P2 findings

No finding here exists merely because Salesforce does something differently.

### P0 — none

No condition was found that can currently cause unauthorized action, false
system state, or data corruption. The two candidates (`allowed_callers` inert,
no principal in the envelope) are P1 because the current actor universe is four
authorized identities and one operator.

---

### P1-1 · The audit table records the boolean the doctrine says not to trust

**Evidence.** `a2a.py:145-164` defines `A2AResult` with both `ok: bool` and
`outcome: str`, carrying this comment:

> *"Callers that must not over-claim (recording a send, say) should read THIS,
> not `ok`."*

`a2a.py:831-846` — `_log_dispatch`, the function that writes the audit trail —
reads `res.ok`:

```sql
INSERT INTO a2a_dispatches (correlation_id, intent, from_agent, agent,
                            kind, ok, error, latency_ms)
```

`a2a_dispatches` has no `outcome` column. 3,895 rows locally, 466 on Railway.

**Why it matters.** `GET /trace/{cid}` — the system's own execution history —
cannot distinguish *the agent refused this* from *the transport failed* from
*we could not tell*. The four-state model was built specifically because a
boolean cannot say "we could not tell", and it is flattened back to a boolean
at exactly the point where the record becomes permanent.

Worse: `__post_init__` defaults `outcome = ACCEPTED if ok else FAILED`, so a
`REJECTED` constructed positionally is *recorded* as `FAILED` — conflating an
authorization refusal with a server error.

**Agentforce reference.** Action execution observability.

**Recommended fix.** Add `outcome text` to `a2a_dispatches`; write `res.outcome`
alongside `ok`. Roughly ten lines and one migration. Do **not** drop `ok` —
existing readers depend on it.

**Severity P1, not P0:** nothing acts on the flattened value today. It becomes
P0 the moment retry logic keys off the trace, because `REJECTED` mixes
retryable and non-retryable causes — a hazard the code itself already documents
at `a2a.py:96-99` and then makes unreadable.

---

### P1-2 · There is no principal in the envelope

**Evidence.** `A2ARequest` (`a2a.py:51-65`) carries `intent`, `from_agent`,
`entity`, `params`, `correlation_id`, `confidence`, `prose`, `govern_bypass`.
**No user, no session, no permission context.** The session is synthesised at
`a2a.py:1084`: `f"a2a-{req.from_agent}-{cid[:8]}"`.

Authorization is real but **role- and channel-scoped, not principal-scoped**:
`write_guard._role` holds a role string; `_readonly_channel` a channel name;
`_customer_scope` a verified account. Two staff users with the same role are
indistinguishable to every layer below the HTTP edge.

**Why it matters.** Every one of these needs a principal and cannot get one:
per-user record ownership, "show me *my* accounts", per-user rate limits,
per-user audit attribution, delegated authority, and real multi-tenancy beyond
the single-org `tenancy.py` context. This is the same defect the staff-email
work kept hitting from a different direction — `assignable_identity` exists
precisely because `owner_id` could not answer "who is allowed to receive work".

**Agentforce reference.** Identity and permission boundary; Agentforce runs
actions as a defined running user and carries that identity through.

**Recommended fix — smallest durable boundary.** Add one optional field:

```python
principal: Optional[Principal] = None   # {kind: user|service|customer,
                                        #  id, roles, scopes}
```

Populate it at the HTTP edge; require it for `kind == "write"`; log it in
`a2a_dispatches`. Do **not** build a policy engine — the point is that the
identity *travels*, so capabilities can begin to key on it. Everything else can
follow later; retrofitting the field itself is the expensive part.

**Do not defer past the next multi-user feature.** Retrofitting identity through
a mesh that has run without it is materially harder than adding it now, and the
mesh currently has 45 capabilities.

---

### P1-3 · The Orchestrator does not evaluate results and cannot recover

**Evidence.** `orchestrator/router.py:723-741`. The dispatch is single-shot:

```python
except Exception as e:
    return JSONResponse({'success': False, 'mode': 'error', ...})
```

There is no re-route, no retry, no alternate strategy, no check that the
specialist's answer satisfies the objective. `data` is passed through with
`routedTo`/`routedBy` annotations. Multi-step coordination exists only in the
planner, which produces a **preview** and requires an explicit
`plan: … confirm` from the user.

Per §4's taxonomy the Orchestrator is: **a router, a dispatcher, and an opt-in
planner front-end.** It is not a supervisor (A ✓ intent, B ✓ selection,
C ✓ delegation, D ✓ context, **E ✗ result evaluation**, F partial,
**G ✗ recovery**, H ✓ authorization, I partial, J ✓ scope).

**Why it matters.** This is fine for one-shot Q&A and is the reason the system
is predictable. It becomes the constraint the moment a request needs two agents
to succeed *together* — the failure of step 2 is currently the user's problem.

**Recommended fix — and a warning.** Do **not** build an autonomous re-planning
loop. The minimum durable boundary is narrower: give `dispatch` a *declared*
retry policy per capability (`retryable: bool`, derived from `outcome`, not from
`ok` — which requires P1-1 first), and have the orchestrator surface a
structured failure the caller can act on rather than a prose error string.

**Defer the rest.** An orchestrator that re-plans on failure is where agentic
systems become unpredictable and expensive. Conscestra's current
predictability is a feature, and the product is not yet constrained by it.

---

### P1-4 · Guardrail layer 4 is built, wired, and inert

**Evidence.** `a2a.py:961-970` reads `allowed_callers` from `capability_registry`
to restrict *who* may dispatch a capability, and `reg.get("enabled", True)` for
an operator kill-switch. Measured:

```
capability_registry   0 rows   local
capability_registry   0 rows   RAILWAY
  ...disabled caps: 0   ...caps with allowed_callers: 0
```

**Both gates default open when the row is absent.** Agent RBAC has never fired
in production, and the operator has never been able to disable a capability
because there is nothing to disable.

**Why it matters.** A control that has never fired is a control nobody has
tested. The design is right; the seed is missing. And a capability mesh whose
RBAC defaults open is one `_reg()` entry away from a new capability being
dispatchable by anything.

**Recommended fix.** Seed `capability_registry` from `CAPABILITIES` at startup
(the same generate-then-verify pattern `sync_tier_rules()` already uses for
notification tiers), defaulting `allowed_callers` to the capability's own agent
plus `orchestrator`. Then the default is *closed* and widening is deliberate.

---

### P2-1 · No parameter schema at the capability boundary

`Capability` (`a2a.py:172-186`) declares `intent, agent, endpoint, kind, render,
description, sp, compose`. Parameters are `Dict[str, Any]`. Validation lives
inside each agent's `sql_builder`. There is no contract a caller — or the
planner's LLM — can read to know what a capability accepts.

Agentforce Actions declare typed inputs/outputs. Adopt the *principle*: add an
optional `params_schema` and validate on the write path first. Reject the
*mechanism* (a full schema registry) — 45 capabilities do not need one.

### P2-2 · No agent versioning or evaluation gate in production

`agent_versions` is **absent** on both databases; `custom_agents` holds 3 rows
locally / 2 on Railway with no version history. `eval_suite.py` exists but is
not a deploy gate. An agent's instructions can change with no record of what was
live when, and no run of the evaluation before it ships.

### P2-3 · The trace records the shape of a call, not its content

`a2a_dispatches` records intent, agents, kind, ok, error, latency. It does not
record parameters, results, which authorization gate fired, which knowledge was
retrieved, or what was retried. §11's checklist is answerable for roughly half
its questions. **Do not fix this by logging payloads** — that is a privacy
liability. Record the *decisions*: which gate, which outcome, which principal.

---

## 4. Red-teaming Agentforce itself

| Salesforce concept | Verdict for Conscestra | Reasoning |
|---|---|---|
| Deterministic actions with typed contracts | **ADOPT (mostly done)** | 31/45 typed; all writes typed. Close the gap with `params_schema` on writes |
| Identity carried through execution | **ADOPT** | P1-2. The one genuinely missing boundary |
| Action execution observability | **ADAPT** | Record decisions, not payloads — P1-1, P2-3 |
| Data 360 / unified context layer | **ADAPT, do not build** | `content_index` (11,723 embeddings) + `interaction_memories` + the CRM schema already provide grounding. A separate data cloud is a Salesforce answer to Salesforce's federation problem. Conscestra has one Postgres |
| Atlas-style autonomous reasoning loop | **REJECT** | Conscestra deliberately previews plans and proposes writes. That is *better* at this scale. Adopting auto-execution would trade the system's main safety property for autonomy nobody has asked for |
| Topics as an agent-scoping abstraction | **ALREADY EQUIVALENT** | `pre_router` + `prompt` + `sql_builder` per agent is the same decomposition, with more of it in code |
| A2A wire protocol / MCP for agent interop | **DEFER** | An MCP *server* already exists (7 tools) and is the right surface for outside consumers. The internal `a2a.py` is a function-call mesh, and should stay one — a wire protocol between modules in one process buys serialization overhead and solves no problem Conscestra has |
| Agent lifecycle & versioning | **ADOPT, small** | P2-2. Version the instructions, gate on `eval_suite` |
| Guardrails as runtime policy | **ADOPT (finish it)** | P1-4 — the layer exists and is empty |
| Multi-agent supervisor with recovery | **DEFER** | P1-3. Predictability is currently worth more than autonomy |

**The pattern:** most of what Agentforce offers architecturally, Conscestra has
already built in a simpler form. The genuine gaps are *identity propagation* and
*audit fidelity* — neither of which is an agent-framework problem.

---

## 5. Maturity assessment

0 absent · 1 ad hoc · 2 partially structured · 3 production-capable · 4 mature ·
5 enterprise-grade. Every score carries evidence.

| Category | Score | Evidence |
|---|---:|---|
| Agent orchestration | **3** | Routes, fans out, plans, dispatches with correlation. No result evaluation or recovery (P1-3) |
| Agent specialization | **4** | 12 agents, uniform 6-part contract, 6:1 deterministic:prompt |
| Context / data architecture | **3** | One Postgres, `interaction_memories`, provenance stamping. Identity spaces still collide (`assignable.identity_space`) |
| Knowledge / RAG | **4** | Hybrid retrieval, tiered audience gating, RAGAS-lite evals, corrective rewrite |
| Action architecture | **4** | 45 capabilities, all 16 writes typed, read/write declared. No param schema (P2-1) |
| Deterministic execution | **5** | ~165 SPs; agents cannot bypass `execute_sp`; pre-router-first design |
| Authorization | **3** | DB-enforced, fail-closed customer scope — but role-scoped, no principal (P1-2) |
| Guardrails | **3** | Four layers designed; **layer 4 inert** (P1-4); outbound guard active |
| Identity | **2** | `assignable_identity` = 4 rows; `owner_id` polymorphic across 3 spaces; one live uuid collision |
| Observability | **2** | Correlation stitching works; content and decisions not recorded (P2-3) |
| Auditability | **3** | `action_approvals`, `events` (136k), field history — but the dispatch outcome is flattened (P1-1) |
| Human handoff | **4** | Console with CAS takeover, escalations with SLA, U1 durable obligations |
| Agent-to-agent communication | **3** | Typed envelope + registry + correlation. No principal, no task contract |
| Event-driven architecture | **4** | `event_queue`, watermark, 6,592 workflow runs, handler registry |
| Reliability / idempotency | **4** | `UNIQUE(idempotency_key)` + CAS ledgers; 8 concurrent workers → 1 email, proven |
| Workflow durability | **3** | `workflow_runs` + watermark survive restart; no cancellation or compensation |
| External integration | **3** | Resend, Telnyx, Azure, MCP server. Verified sender topology |
| Agent lifecycle / versioning | **1** | `agent_versions` absent in production (P2-2) |
| Evaluation / testing | **4** | 2,031 tests, mutation-verified guards, `mutation.py` harness. Not a deploy gate |
| Governance | **4** | Approvals, critic, HITL floor, undo, one-click decisions, 114 approvals |

**Mean 3.25.** The distribution matters more than the mean: execution and
governance are mature; **identity and observability are the laggards**, and they
are the two that gate everything else.

---

## 6. The five-year question

> If Conscestra were competing with Agentforce in five years, which decisions
> made today become its greatest strengths — and which become its greatest
> liabilities?

### Greatest strengths

**1. The truth boundary.** Almost every agent platform will eventually ship an
incident where the system reported an action it did not perform. Conscestra has
already had that incident, diagnosed it, and encoded the fix as an invariant
with a mutation-tested predicate. Competitors will retrofit this under
regulatory pressure; retrofitting it means auditing every success path in the
product. Conscestra's is load-bearing today.

**2. Deterministic-first execution.** At 6:1 deterministic-to-prompt, unit
economics do not degrade as usage grows. A competitor whose Topics carry the
business logic pays per token for work a `CASE` statement could do, and gets
nondeterminism for the money. This advantage widens with scale, not narrows.

**3. Writes as proposals.** `MAX_WRITES 2`, every write a governance proposal,
undoable, routed by authority limit. When the industry's first serious
agent-caused data incident lands, "our agents cannot write without a human" will
be worth more than any capability count.

### Greatest liabilities

**1. No principal in the envelope — by far the largest.** Every enterprise
requirement that arrives in year two is per-user: record-level permissions,
delegated authority, per-user audit, real multi-tenancy, customer-facing agents
acting on behalf of a named person. All of them need identity to travel with the
request. Retrofitting a principal through 45 capabilities and every SP that
trusts its caller is the kind of work that takes a quarter and breaks things.
**Adding the field now costs a day.**

**2. An audit trail that records less than the system knows.** You cannot
reconstruct history you did not record. Every month this runs, more decisions
become permanently unexplainable — and the compliance conversation that
eventually demands them cannot be satisfied retroactively.

**3. Identity spaces that collide.** `owner_id` is polymorphic across `owners`,
`employees` and `assignable_identity`; one live uuid resolves to two different
people. It is contained today by explicit membership and fail-closed routing.
At 40 employees and 3 tenants it stops being containable, and the fix is a data
migration nobody will want to run.

### What is *not* a liability

Not having an autonomous re-planning loop. Not having a separate data cloud. Not
speaking the A2A wire protocol. These are Salesforce's answers to Salesforce's
problems — federation across acquired products, and a platform that must be
generic across every customer's schema. Conscestra has one schema and one
database. **Adopting that machinery would import the complexity without the
constraint that justifies it.**

---

## 7. Recommended sequence

Smallest change that creates a durable boundary, in dependency order:

| | Change | Effort | Why now |
|---|---|---|---|
| 1 | `outcome` column on `a2a_dispatches`; log `res.outcome` | hours | Unblocks any retry logic; stops the audit lying by omission |
| 2 | `principal` on `A2ARequest`; require on writes; log it | ~1 day | The one boundary that gets dramatically harder with time |
| 3 | Seed `capability_registry` from `CAPABILITIES`, defaulting closed | hours | Activates a guardrail layer that has never fired |
| 4 | `params_schema` on write capabilities | ~1 day | Closes the last untyped surface on the write path |

**Deliberately not recommended:** an autonomous supervisor loop, a Data-360
equivalent, the A2A wire protocol, or a policy engine. Each would add more
complexity than the deficiency it addresses, at Conscestra's current scale.

---

## 8. Answering the brief's framing directly

> **"Salesforce has this"** vs **"Conscestra actually needs this"**

Salesforce has: a reasoning engine that executes plans, a federated context
layer, a wire protocol for agent interop, a topic abstraction, and a trust
layer. Conscestra needs: **identity in the envelope**, and **an audit record as
honest as its in-memory model**. Those two are worth more than the other five
combined, and neither of them is an agent-framework feature.

The most interesting conclusion of this review is that Conscestra's agentic
architecture is not weak where a Salesforce comparison would predict. It is weak
in exactly the places any 111,000-line system that grew feature-first is weak:
**identity and observability.** The agent layer is the healthy part.
