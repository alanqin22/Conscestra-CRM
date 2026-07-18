# Conductor + Autonomous Workflow Engine — Gap Audit

_Audit date: 2026-07-17 · Scope: the "AI Orchestrator as conductor" and the
"autonomous workflow engine" pillars of the Agentorc / Autonomous Customer
Intelligence Platform vision, measured against the current codebase._

---

## TL;DR

Conscestra already implements ~85–90% of the "AI Operating System for Customer
Relationships" vision. The orchestration stack is not one component but **four
strong, cooperating layers**, and the safety architecture (reads-execute /
writes-propose, governance, blackboard coordination, idempotency) is **more
mature than the reference vision describes**.

The gaps in these two pillars are **wiring, not construction**:

1. 🔴 The goal **planner is built but off the conversational path**.
2. 🔴 Multi-agent collaboration on chat is limited to **8 hardcoded symphonies**.
3. 🟠 The **supervisor and the planner are two disconnected autonomy engines**.
4. 🟠 There is **no unified per-turn "conductor decision"** object.
5. 🟡 The **"act unprompted" leg is still gated** (`*_AUTOACT=0`), even locally.

The highest-leverage move is to **put the conductor you already built on the
podium** — Gaps 1–3 collapse into roughly two small changes (see
`conductor_workflow_impl_plan.md`).

---

## What actually exists (grounded in code)

| Layer | File | Role | Maturity |
|---|---|---|---|
| Conversational router | `app/agents/orchestrator/router.py` | Fast-paths → single-agent routing → A2A dispatch | ✅ Solid |
| Intent classifier | `app/core/intent_router.py` | LLM picks **one** agent, keyword fallback | ✅ Solid |
| Goal planner | `app/core/planner.py` | Novel goal → validated multi-step plan; reads execute, writes propose | ✅ Excellent — **off-path** |
| Standing supervisor | `app/core/supervisor.py` | Sense KPIs → detect breaches → emit/act, governed | ✅ Excellent |
| Event bus | `app/core/agent_bus.py` | 7 handlers + catch-all, blackboard coordination | ✅ Excellent |
| A2A layer | `app/core/a2a.py` | Typed capability registry + in-process dispatch | ✅ Excellent |

**Flag state (local `.env`, 2026-07-17):** `AGENT_BUS_ENABLED=1`,
`AGENT_BUS_AUTOSEND=1`, `AGENT_BUS_CATCHALL=1`, `SUPERVISOR_ENABLED=1`,
`OBJECTIVES_ENABLED=1` — all on. Only `SUPERVISOR_AUTOACT=0` and
`OBJECTIVES_AUTOACT=0` are held back. The remaining gap to production is the
manual Railway cutover, not code.

The safety architecture is the genuinely hard part and it is done well:
reads-execute / writes-propose (`planner.py:200-212`), materiality re-checks at
action time (`agent_bus.py:338`), idempotency guards on every handler,
`_is_real_email` outbound gating, and agents coordinating through the
**blackboard** rather than direct calls (`agent_bus.py:347-354`).

---

## The gaps

### 🔴 Gap 1 — The planner is built but not on the conversational path

`app/core/planner.py` does exactly what the vision's "conductor" demands:
decompose a plain-language goal into a coordinated multi-agent plan, run the
reads, and queue the writes for governance. But it is only reachable via the
admin-gated endpoint `POST /planner/plan` (`planner.py:232`, admin dep at
`main.py:1097`) and the read-only `crm.plan` A2A capability
(`a2a.py:497`, which calls **`draft_plan` only** — never `run_plan`).

It is **not invoked from `orchestrator_chat`**. Trace the conversational path:
`router.py:404-433` — a novel multi-step goal falls through to `aroute()`, which
returns **exactly one agent**. The decomposition never fires.

**Consequence:** the best "conductor" capability is invisible to every
conversational channel (web chat, voice, SMS). The orchestra plays one
instrument per turn unless the request matches a hardcoded symphony.

### 🔴 Gap 2 — Symphonies are hardcoded; dynamic composition isn't used

Multi-agent fan-out on chat is 8 fixed dictionaries (`router.py:66-136`): daily,
pipeline, followup, revenue, alerts, weekly, team, newbiz. Anything outside
those phrases cannot trigger collaboration. The planner already composes
collaboration **dynamically** over the live capability manifest — but (Gap 1) it
isn't wired in.

### 🟠 Gap 3 — The two autonomy systems don't feed each other

- **Supervisor** senses breaches and emits `supervisor.alert` (`supervisor.py:214`)
  or kicks a **single** hardcoded emitter (`fn_emit_overdue_invoice_events`,
  `supervisor.py:258`) via a 3-case switch in `_autoact` (`supervisor.py:253`).
- **Planner** composes multi-agent plans — but only when a human hits an endpoint.

The vision's thesis ("a single trigger → coordinated multi-agent workflow, fully
traceable") wants these joined: a supervisor breach should be able to **invoke
the planner** to compose a governed response play, not just fire one canned SQL
function.

### 🟠 Gap 4 — No unified "conductor decision" object

The vision lists 8 decisions the Orchestrator should make per interaction (which
agent, which knowledge, auth required, human-in-loop, multi-agent, safe
actions…). In reality these are **scattered**: agent selection in
`intent_router`, auth per-agent, knowledge via per-agent RAG, collaboration via
hardcoded symphonies, human-in-loop via governance at write-time. There is no
single function that reasons about a turn holistically, and `intent_router` is
**structurally single-agent** — it cannot express "this needs Contacts *and*
Accounting."

### 🟡 Gap 5 — The "act unprompted" leg is still gated

`SUPERVISOR_AUTOACT=0` and `OBJECTIVES_AUTOACT=0`. This is the line between "an
AI that alerts you" and "an autonomous employee." Correctly conservative, but a
gap against the vision — worth a deliberate, staged flip with governance in
`propose` mode (already supported at `supervisor.py:229`, `_govern_autoact`).

---

## Recommendations (ranked by leverage)

1. **Wire the planner into the conversational path.** Add a tier in
   `orchestrator_chat` between symphonies and single-agent routing that drafts a
   plan for a novel multi-step goal, executes reads, and shows writes as
   governance proposals. One file, ~30 lines, reuses everything.

2. **Connect supervisor breaches → planner.** Replace the hardcoded `_autoact`
   cases with an optional path that hands the breach headline to the planner as a
   goal; writes queue for approval. Realizes the autonomous workflow engine and
   unifies both focus areas with one bridge.

3. **Give the router a real "conductor decision."** Elevate `intent_router.route()`
   into a decision that returns one agent, a symphony, or a plan — and later
   *multiple* agents. Structural fix for Gap 4; do it after 1–2 prove the pattern.

4. **Stage the AUTOACT flip** behind governance-propose mode, one detector at a
   time, gated by the eval harness (`app/core/evals.py`).

The through-line: **you don't need to build a conductor — you need to put the
conductor you already built on the podium.** Recs 1–3 are wiring, and they
collapse into roughly two small changes.
