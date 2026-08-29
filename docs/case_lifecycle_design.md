# C1 — The Case Lifecycle: reconnecting the orphaned service record

Axis 5, Phase 1. Design brief — **not yet implemented**.

## What this is not

This is not a Case CRUD package. A `case-mgmt.html` with a list and an edit
form would satisfy the letter of the finding and miss all of its leverage.

The `cases` table has been sitting in the database since **2026-01-09** with
120 rows, 480 threaded comments, a five-state lifecycle and an owner column,
while four separate blind-spot axes built an entire service capability *around*
it — knowledge retrieval, escalations with SLA clocks, the takeover console,
CSAT proxy, containment and deflection metrics. The architectural cost was
already paid. What was never done is the wiring.

So the deliverable is **connective tissue**, and the acceptance test is a
lifecycle, not a page:

    Conversation → Escalation → Case → Assignment → SLA → Comments
                 → Resolution → Knowledge → Analytics

## The governing distinction

> **An escalation is an EVENT. A case is the durable unit of WORK that event
> creates.**

Today the event *is* the work record, and that is the root of the measurement
problem. `agent_ops` can report that a conversation was resolved. It cannot
answer any of these:

- Who owned the resulting work?
- What was first-response time? Resolution time?
- How many times was it reassigned?
- Was it reopened?
- Which account / contact / product was affected?
- What was the final resolution?
- Did the resolution produce reusable knowledge?

Every one of those is a property of a case, not of a conversation.

## What already exists (do not rebuild)

| Piece | Where | Note |
|---|---|---|
| `cases` | 120 rows | `case_id, account_id, contact_id, subject, description, priority, status, origin, owner_id, summary, summary_generated_at, created_at/by, updated_at/by, closed_at` |
| `case_comments` | 480 rows | `case_comment_id, case_id, comment, is_internal, created_at, created_by` — **`is_internal` already distinguishes public reply from internal note** |
| Escalation objects | `escalation.py` | `open()`, `resolve_for_conversation()`, `sla_breaches()`, reason taxonomy, priority ranking |
| Conversation spine | `conversations.py`, `channel_adapters.py` | cross-channel; `conversation_id` is the join key |
| Takeover console | `agent_console.py` | `queue()`, `transcript()`, `takeover()`, `suggest_reply()`, `send_reply()` |
| Knowledge loop | `knowledge.py` | `draft_pass()`, `publish()`, `log_gap()`, `retrieve()` |
| Governance | `governance.py` + critic | proposal → approval → execution, with undo |
| Identity | `identity.py` | (channel, handle) → party, account |
| Owners | `owners` (44) | has `role`; **no language or skill attribute yet — C2's seam** |

### Correction (2026-07-26) — a legacy `sp_cases()` exists and is LIVE

The blind-spot pass reported *"no stored procedures — nothing matching `case`
in `sp/`"*. That was **filename-based and wrong**: `sp_cases()` lives inside the
schema dump and is present in the database.

It is a 14-mode procedure (`create update assign close resolve reopen escalate
add_comment list get queue summary timeline sla_report`) that writes `status`
and `owner_id` across 8 `UPDATE`/`INSERT` statements and has **no
`record_field_history` awareness**. It is therefore a **second case mutation
boundary** with its own lifecycle semantics, bypassing the state machine, owner
validation and field history in a single call.

This makes the C1 risk **stronger** than first reported, not weaker. The gap
itself stands unchanged: there is still no governed Cases agent, no UI and no
integrated lifecycle around the existing records.

**Disposition (Step 5b, superseded — see 2026-08-28 below):**

- **Application layer — effective.** `write_guard.FORBIDDEN_PROCEDURES` rejects
  any `sp_cases(` query unconditionally, before every role early-return, for
  every caller including the system context and admin. It fails visibly; the
  query is never rewritten and never rerouted to the governed layer.
- **Database layer — inert, deliberately not applied.** The application connects
  as `postgres`, which owns the function *and* is a superuser, and superusers
  bypass privilege checks. A `REVOKE` empties the ACL and changes nothing
  (verified: `has_function_privilege` still returns true afterwards). Applying
  it would look like a control while being none, so it is documented here
  instead.
- **Not dropped.** Signature and recovery path recorded; removal waits.

### Resolution (2026-08-28) — `sp_cases()` is DROPPED

`sql/drop_sp_cases.sql` removed it. The deferral above was reasonable when it
was written and wrong to keep, for a reason the original disposition could not
have known: **it described the procedure as legacy without ever measuring how
much of it still ran.**

Before dropping, every mode was executed against a real database inside a
rolled-back transaction and the rows re-read:

| outcome | modes |
|---|---|
| **still executed** | `assign` (set `cases.owner_id`), `close` (set `cases.status`), `update`, `list`, `queue`, `summary`, `timeline` |
| already broken | `get`, `create`, `resolve`, `sla_report` (`case_metrics` gone), `escalate`, `add_comment`, `reopen` (`case_comments.id` gone) |

So this was never fourteen dead modes. It was a **half-working bypass**, and the
half that worked wrote precisely the two fields `app/core/cases.py` exists to
audit. That is worse than a wholly broken one: the working half is the half that
gets found.

**Why DROP and not the deferred REVOKE.** `PUBLIC` held `EXECUTE`, so revoking
from `crm_app` alone would have changed nothing; making it real meant `REVOKE …
FROM PUBLIC`, a broader production change that costs the same as a drop while
leaving a re-grantable object behind a permission wall. Privilege separation
shipping to production (2026-08-03) made the ACL half *meaningful* there, which
is exactly what made deferring it pointless rather than what made it viable.

**The application guard stays.** A dropped function can be recreated by anyone
restoring an old dump. `FORBIDDEN_PROCEDURES` keeps its `sp_cases` entry, and
`test_20` now matches `sp\_case%` by pattern — so re-creating `sp_cases`, or
authoring `sp_case_mgmt` next quarter, both turn the suite red. Verified by
planting a stub and watching it fail.

```
DROPPED 2026-08-28 · sql/drop_sp_cases.sql
recovery: sp/crm_db.sql (CREATE FUNCTION public.sp_cases) — restoring it
          restores a procedure guard_query still refuses, which is correct
```

## Design decisions

### D1 — The case is created from the escalation, not instead of it

Escalations stay exactly as they are. `escalation.open()` gains one additional
effect: it opens (or joins) a case and stores `case_id` on the escalation row.

Rationale: escalations already solved idempotency-per-conversation, SLA tiering
by reason, and the promised-followup detector. Rewriting that into the case
layer would duplicate a working mechanism. The escalation remains the *trigger
record*; the case becomes the *work record*.

One case per conversation, mirroring the existing partial unique index. A
second escalation on the same conversation attaches to the same case and may
raise its priority — never forks a second case.

### D2 — Not every conversation deserves a case

A case is opened when work is genuinely owed:

- any escalation opens (customer asked for a human, complaint, legal/safety,
  refund, repeated failure, agent promised follow-up)
- the takeover console takes a conversation over
- a human creates one manually
- inbound email that `auto_reply` could not answer from approved knowledge

A conversation the AI fully answered from the KB does **not** open a case. The
containment metric depends on that distinction staying honest — if every chat
becomes a case, containment rate becomes meaningless and the queue becomes
noise a human learns to ignore.

### D3 — Status transitions are governed, not free-text

    new → in_progress → waiting → resolved → closed
                ↑            ↓
                └────────────┘
    resolved → in_progress  (REOPEN — counted, never silent)

Illegal transitions are refused at the write layer, not merely discouraged in a
prompt. `closed` is terminal; reopening a closed case creates a linked new case
rather than resurrecting it, so resolution-time statistics stay truthful.

### D4 — The C7 history seam ships WITH C1, not after it

Per the Axis-5 constraint — *do not build major new objects without deciding how
their history is recorded* — cases are exactly the object whose reassignments
and status changes must be provable. Retrofitting history after the reassignment
data has already been overwritten is impossible.

A general `record_field_history` table lands here, written by the case layer
first and available to every other object afterwards:

    record_field_history(
      history_id, entity, entity_id, field,
      old_value, new_value, actor, source, changed_at)

This answers the middle of the three distinct questions Axis 5 separates:

| Question | Answered by |
|---|---|
| Who did what, when? | `audit_log` (exists) |
| **What was this field before?** | **`record_field_history` (this)** |
| Where did this value originate? | `provenance.py` (exists) |

Scope for C1: `status`, `owner_id`, `priority` on cases. Deliberately narrow —
a general "log every column" trigger is how history tables become unusable.

### D5 — Assignment is a durable state transition, not a suggestion

C2 builds the routing intelligence. C1 builds the *record* it will write into:
`owner_id` changes are history-tracked (D4), timestamped, and attributed to an
actor. When C2 arrives it supplies a recommended owner; the assignment itself
is already a first-class auditable transition.

> The AI may recommend the owner. The assignment must become a durable,
> auditable state transition.

### D6 — Resolution feeds the knowledge loop that already exists

On `resolved`, the case summary + comment thread become a candidate for
`knowledge.draft_pass()` — the same governed proposal path email threads and
call transcripts already use. A human approves every word before any customer
sees it, unchanged.

This closes the loop the axis is named for: work becomes knowledge, and the
next conversation is contained rather than escalated.

## Build order

| Step | Deliverable | Notes |
|---|---|---|
| 1 | `sql/case_lifecycle.sql` | `record_field_history`; `cases.conversation_id`, `first_response_at`, `resolved_at`, `reopen_count`, `escalation_id`; indexes; **no destructive change to the 120 existing rows** |
| 2 | `app/core/cases.py` | `open_from_escalation()`, `transition()` (governed state machine + history), `assign()`, `comment()`, `resolve()`, `reopen()`, `sla_state()`, `queue()` |
| 3 | Escalation bridge | `escalation.open()` → `cases.open_from_escalation()`; `resolve_for_conversation()` → case resolve. Additive; escalations keep working if cases are disabled |
| 4 | Console bridge | `agent_console.takeover()` opens/attaches a case; `send_reply()` writes a public `case_comment` and stamps `first_response_at` |
| 5 | `app/agents/cases/` | Standard package (`router`, `graph`, `prompt`, `pre_router`, `sql_builder`, `formatter`) — conversational case management, matching every other entity agent |
| 6 | `case-mgmt.html` | The list page every other entity has. **Must be added to `_CHAT_PAGES`** |
| 7 | Analytics | Real service metrics off real cases: first-response, resolution time, reassignment count, reopen rate, backlog by owner/status/SLA. Surfaces in `agent_ops` and Analytics `mode='service_analytics'` |
| 8 | Knowledge bridge | Resolved case → `draft_pass()` candidate (D6) |
| 9 | Tests | State machine legality, idempotency per conversation, history completeness on reassignment, containment honesty (KB-answered conversation opens no case), SLA breach detection |

## Feature flags

    CASES_ENABLED=1          master switch; 0 = escalations behave exactly as today
    CASES_AUTO_OPEN=1        escalation/takeover auto-opens a case
    CASES_KB_FEEDBACK=1      resolved cases feed the knowledge draft pass

Ship with `CASES_AUTO_OPEN=0` first so the case layer can be exercised manually
against real escalations before it starts creating records automatically.

## Migration note

`cases` already holds 120 rows with `created_at` up to 2026-01-09 and no
conversation linkage. They are backfilled as historical: `conversation_id` NULL,
`first_response_at` NULL, `reopen_count` 0. They must not be counted in
first-response or resolution-time statistics — a NULL there means *unknown*, not
*instant*, and the metrics layer has to say so rather than average it in.

## Open questions for the build

1. Should `waiting` pause the SLA clock? (Standard practice: yes, "waiting on
   customer" stops the business clock. It also makes SLA gameable by parking
   cases in `waiting` — so pausing needs a reason code.)
2. Do case comments thread into the unified Conversation Object, or stay a
   parallel record? Leaning: comments are case-local; the *conversation* remains
   the customer-facing spine, and the case references it.
3. Does a case need its own SLA fields, or does it read the escalation's? C6
   eventually makes SLA entitlement-driven — the case should own the deadline so
   there is one place for C6 to change.
