# Implementation Plan — Put the Conductor on the Podium

_Companion to `conductor_gap_audit.md`. Turns Recommendations 1 & 2 into
concrete, staged, low-risk changes. Both reuse existing machinery
(`planner.py`, `a2a.py`, `governance.py`) — this is wiring, not new subsystems._

Design principles carried over from the codebase:
- **Deterministic fast-paths first, LLM only where needed** (mirrors `router.py`).
- **Reads execute, writes propose** (never auto-send from a conductor path).
- **Opt-in behind a flag, dry-run by default, `confirm` to act** (mirrors the
  existing `route:` A2A handle at `router.py:321-342`).
- **Idempotent** — no duplicate plans/proposals for the same goal.

---

## Recommendation 1 — Wire the planner into the conversational path

**Goal:** a novel multi-step goal typed into the Orchestrator gets *decomposed*
(reads executed, writes queued for approval) instead of routed to one agent.

### Phase 1A — Explicit `plan:` handle (MVP, zero routing risk)

Add a deterministic handle in `orchestrator_chat`, placed **after** the `route:`
A2A block (`router.py:343`) and **before** the pulse/exec/symphony tiers so it
can't be shadowed. Pattern mirrors the existing `route:` handle exactly.

```
"plan: <goal>"            → planner.draft_plan(goal)   → render plan preview
"plan: <goal> confirm"    → planner.run_plan(goal)     → reads execute,
                                                          writes → governance
```

**New code:**
- `router.py`: a `_PLAN_RE = re.compile(r'^(?:plan|goal)\s*[:=]\s*(.+)$', I)`
  block. `confirm` in the message → `run_plan`; else `draft_plan`.
- `router.py`: `_format_plan(result) -> str` — a markdown renderer (sibling of
  `_weave_symphony` / `_format_pulse`). Shows summary, each step
  (intent · kind · why · read result / queued-approval uuid), and a footer
  telling the user what (if anything) was queued for approval.

**Response shape:** `{ mode: "plan", output: <markdown>, workflow: "plan" }` —
consistent with the `symphony` / `pulse` / `a2a` modes the UI already handles.

**Why an explicit prefix first:** it is impossible for it to hijack normal
single-agent queries (opt-in token), it needs no LLM, and it makes the
capability demonstrable in one line. This is the same staging the `route:`
handle used.

### Phase 1B — Automatic detection (closes Gap 1 fully)

Extend `intent_router` so routing returns a **decision type**, not just an agent.
Add a lightweight classifier tier (reusing the existing lite-LLM call at
`intent_router.py:126`) that labels a message as one of:

```
single_agent | symphony | plan
```

Only `plan`-labelled messages at/above a confidence floor take the planner path;
everything else keeps today's exact behavior (single-agent + keyword fallback).
Keep it **behind `INTENT_PLAN_ROUTING=0`** so it ships dark and is proven against
the eval harness before flipping.

- `intent_router.py`: extend `RouteDecision` with `kind: str` (default
  `"single_agent"`); add `classify_kind(message)`; default off.
- `router.py`: in the single-agent tier (`:404`), if `decision.kind == "plan"`,
  route to the planner preview instead of `aroute`'s single agent.

### Phase 1C — Expose executing plans over A2A (optional, symmetry)

Register a `crm.plan_execute` **write** capability in `a2a.py` that calls
`planner.run_plan` (today only the read-only `crm.plan` → `draft_plan` exists,
`a2a.py:497`). This lets any agent — including the supervisor (Rec 2) — request
a *governed* plan execution through the same typed, audited dispatch path,
rather than importing the planner directly.

### Testing / verification (Rec 1)
- Add `eval_conductor_plan_route` to `app/core/evals.py`: a known multi-step goal
  produces a valid bounded plan with ≥1 read and writes queued (not executed).
- Manual: `POST /orchestrator-chat {"message":"plan: recover overdue receivables
  and re-engage slipped deals this week"}` → preview; add `confirm` → reads run,
  writes appear in the governance queue (`governance-mgmt.html`).
- Regression: confirm ordinary queries ("show overdue invoices", "list leads")
  still single-agent route unchanged (Phase 1B stays off until proven).

### Risk / rollback (Rec 1)
- Phase 1A is opt-in by token — no regression surface. Remove the block to roll
  back.
- Phase 1B is flag-gated (`INTENT_PLAN_ROUTING=0`) — dark by default.
- Writes are **never executed** on the conductor path (planner queues them);
  worst case is an over-eager *proposal*, which a human declines.

---

## Recommendation 2 — Connect supervisor breaches → planner

**Goal:** a KPI breach can trigger a *composed multi-agent response play* (queued
for approval), not just one canned emitter — realizing the vision's "single
trigger → coordinated, traceable workflow."

### Change

In `supervisor.py`, add an optional planner bridge alongside `_autoact`
(`supervisor.py:253`), gated by a **new flag `SUPERVISOR_PLANNER=0`** (staged
independently of `SUPERVISOR_AUTOACT`).

For a breach carrying a goal-shaped headline, build a goal string and draft a
plan; queue its writes for approval:

```
detect_ar_spike        → goal: "resolve N overdue invoices, $X outstanding"
detect_slipped_deals   → goal: "re-engage N slipped deals worth $X"
detect_churn_risk      → goal: "launch save plays for N high-churn accounts"
```

- The supervisor tick is **sync** (run via `asyncio.to_thread`, `main.py:504`).
  `run_plan` is async — call it with `asyncio.run(run_plan(goal))` inside the
  worker thread (no running loop there), **or** (preferred) dispatch the
  `crm.plan_execute` capability from Phase 1C via a small sync wrapper. Preferring
  the A2A path keeps the call typed, audited, and governance-gated in one place.
- **Nothing sends.** `run_plan` executes reads and routes every write to
  `governance.propose` (`planner.py:200-212`), tagged with `plan_goal` +
  `plan_correlation_id`. Even "autonomous," the human still approves each write.

### Idempotency
- Reuse the existing per-rule dedupe (`_already_alerted`, `supervisor.py:203`,
  12h window) to gate plan generation.
- Before proposing, check for an open approval with the same `plan_goal`
  (proposals are already tagged, `planner.py:205`) — skip if one is pending.
  One active plan per breach type at a time.

### Governance fit
This is *safer* than the current `_autoact`: instead of executing
`fn_emit_overdue_invoice_events` directly under `SUPERVISOR_AUTOACT`, the breach
becomes a **plan of governed proposals**. The `SUPERVISOR_PLANNER` flag can ship
on well before `SUPERVISOR_AUTOACT`, because its "action" is only ever *queuing
proposals a human approves*.

### Testing / verification (Rec 2)
- `POST /supervisor/run-once` with `SUPERVISOR_PLANNER=1` against seeded breach
  conditions → assert a plan drafted and its writes visible as pending approvals
  tagged with the breach goal.
- Re-run immediately → assert **no duplicate** plan/proposals (idempotency).
- `SUPERVISOR_PLANNER=0` → assert behavior identical to today.
- Add `eval_supervisor_planner_bridge` to `evals.py` (drift → `supervisor.alert`,
  same pattern as the existing behavior evals).

### Risk / rollback (Rec 2)
- Flag-gated and dark by default (`SUPERVISOR_PLANNER=0`).
- No outbound side effects — only governance proposals. Rollback = flag off.

---

## Suggested sequencing

| Step | Change | Flag | Ships |
|---|---|---|---|
| 1 | Rec 1 Phase 1A — `plan:` handle + `_format_plan` | none (opt-in token) | immediately |
| 2 | Rec 1 Phase 1C — `crm.plan_execute` A2A write cap | governance | with step 1 |
| 3 | Rec 2 — supervisor → planner bridge | `SUPERVISOR_PLANNER=0` | dark, then flip |
| 4 | Rec 1 Phase 1B — automatic plan routing | `INTENT_PLAN_ROUTING=0` | dark, then flip |

Steps 1–2 are a single small PR (conversational conductor, opt-in). Step 3 is the
second small PR (autonomous conductor, dark-flagged). Step 4 follows once the
eval harness shows the plan-vs-single-agent classifier is trustworthy.

## Files touched
- `app/agents/orchestrator/router.py` — `plan:` handle + `_format_plan` (Rec 1A)
- `app/core/a2a.py` — `crm.plan_execute` capability (Rec 1C)
- `app/core/intent_router.py` — decision `kind` + `classify_kind` (Rec 1B)
- `app/core/supervisor.py` — planner bridge + `SUPERVISOR_PLANNER` flag (Rec 2)
- `app/core/evals.py` — two new behavior evals
- `.env` / `docs/agent_bus_rollout.md` — document the two new flags
