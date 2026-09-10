# Decision Record — Where escalation-reminder state belongs

**Date:** 2026-09-09 · **Phase:** state-placement and atomicity decision only.
**No code, schema, migration or configuration was changed.** ·
**Prerequisite:** `docs/remediation_plan_2026-09-09_defects_A_B_C.md` §14

---

## 0. The distinction this record exists to make

Two guarantees are needed and only one exists.

| guarantee | statement | status |
|---|---|---|
| **Ledger** | *"For this specific reminder intent `(escalation, ordinal)`, only one send attempt may be accepted."* | **MEASURED** — proved under real concurrency, mutation-checked 3/3 |
| **Scheduling state** | *"This worker is entitled to create the next reminder for this escalation now."* | **MISSING** — nothing in the system can answer it |

The ledger answers *"has this exact intent already been sent?"* It cannot
answer *"is this the intent I am entitled to form?"* Every finding below
follows from that gap.

---

## 1. Required reminder state semantics

To satisfy the ten required proofs, the state must support **one atomic
transition** that simultaneously decides and allocates:

| must decide | from |
|---|---|
| liveness | `status IN ('open','assigned')` |
| unclaimed | `assigned_at IS NULL` — never `assigned_to` |
| threshold reached | `created_at`, `sla_due_at` (§2) |
| cooldown elapsed | last-reminder instant |
| below `REMIND_MAX` | reminder count |
| **and allocate** | the next ordinal, as the same statement's result |

**The allocation must be the same statement as the decision.** If eligibility
is decided in one statement and the ordinal allocated in another, a worker can
be entitled at read time and not at write time — which is the whole defect
class this engagement has been cataloguing.

---

## 2. The SLA anchor — decided: **`sla_due_at`**, expressed as a fraction of the live window

**FACT.** `sla_breaches()` tests `sla_due_at < now()`. `sla_due_at` is
therefore *the promise currently in force*, and it is the field every existing
consumer already treats as the deadline.

**FACT.** `open()`'s priority re-ask lowers it —
`sla_due_at = LEAST(sla_due_at, now() + interval)` — and **does not update
`sla_minutes`**. After a fold, `sla_minutes` is the *original* promise and
`sla_due_at` is the *current* one.

**Decision.** The reminder must follow the promise in force. A reminder is a
prompt *before* a breach; anchored on the original window it could fire after a
tightened deadline had already passed, which inverts its purpose.

**Expressed using only co-updated fields:**

```
now() >= created_at + REMIND_AT_FRACTION * (sla_due_at - created_at)
```

This deliberately avoids `sla_minutes`, which the fold leaves stale.

**MEASURED — 0 of 138 rows currently diverge**, so this choice changes nothing
about today's data. **It is a correctness choice for the folded case, not a
tuning preference**, and it is recorded because the two expressions are
indistinguishable in the current population and would silently differ later.

---

## 3. The four `status='assigned' AND assigned_at IS NULL` rows

**Disposition: an unenforced invariant, violated by direct INSERT — not a
second valid assignment state and not a live code defect.**

**MEASURED:** all four are `source='test'`, `reason='customer_requested_human'`,
with `created_at = updated_at` (never updated after insert) and no
`conversation_id`. **They were born `assigned`.** No code path can produce this
state: both claim paths write `assigned_to` and `assigned_at` together.

**INFERRED:** they are test-fixture residue that INSERTed a terminal-ish status
directly rather than transitioning into it — the same shape as the fixture
ownership defect remediated in B.

**The real finding, and it is a schema one:** the invariant *"`status='assigned'`
implies `assigned_at IS NOT NULL`"* holds in code and is **not enforced by the
database**, so any direct writer can violate it.

**Kept as a separate state-integrity finding. Not remediated here, and
explicitly not folded into the reminder work** — otherwise the reminder
implementation quietly becomes a historical-data project.

**What the future producer must require:** it must treat `assigned_at IS NULL`
as the definition of unclaimed and **must not infer claimed-ness from
`status='assigned'`**. Under that rule these four rows are eligible for
reminders while the queue displays them as assigned. That is a consequence the
producer's authorization must accept explicitly, or the rows must be dispositioned
first.

**Context that bounds all local reasoning — MEASURED:** the escalation
population is `test` 104, `test-suite` 24, `e2e-suite` 8, `voice` 2. **136 of
138 rows are test data.** The "126 eligible escalations" figure from §14
therefore says nothing about production volume.

---

## 4. Option A — state on `escalations`

**Viable, and it does NOT reproduce the known race — but only if written one
specific way.**

The `approval_remind` / `alert_remind` race is *not* caused by keeping a counter
on the row. It is caused by **computing the new value in the application**:

```python
n = int(sent or 0) + 1                                  # read, compute in Python
UPDATE ... SET escalation_notices=%(n)s WHERE ... AND status='pending'
```

Two workers read the same `sent`, compute the same `n`, and both UPDATE
successfully because the predicate never tests the counter.

The correct form computes the new value **in the database** and puts every
eligibility condition in the predicate:

```
UPDATE escalations
   SET <count> = <count> + 1,
       <last_at> = now()
 WHERE escalation_id = ...
   AND status IN ('open','assigned')
   AND assigned_at IS NULL
   AND now() >= created_at + FRACTION * (sla_due_at - created_at)
   AND (<last_at> IS NULL OR <last_at> < now() - <cooldown>)
   AND <count> < <REMIND_MAX>
RETURNING <count>
```

*(Shape only. Column names are not chosen here.)*

**Why this is atomic.** Under READ COMMITTED two concurrent UPDATEs on one row
serialize, and the second re-evaluates its predicate against the committed new
version. The first sets the last-reminder instant; the second then fails the
cooldown clause and returns no row. **Exactly one worker allocates, and the
`RETURNING` value *is* the ordinal** — there is no separate allocation step to
race.

This is the same mechanism as `acquire()`, as `agent_console._set_handling`, and
as the `escalation.assign()` fix proved in §13. **The house already has this
pattern three times over.**

**It also closes the candidate-selection race**, which no other option does:
selection and allocation become one statement, so there is no window between
"chosen" and "entitled".

| criterion | assessment |
|---|---|
| ordinal allocated atomically | **yes**, by `RETURNING` |
| two workers cannot allocate the same ordinal | **yes**, row-level serialization |
| cooldown enforced atomically | **yes**, in the predicate |
| `REMIND_MAX` enforceable | **yes**, from authoritative state |
| terminal escalations excluded | **yes**, in the predicate |
| new state surface | **2 columns** on the table that already owns escalation lifecycle |
| reproduces the known race? | **No — provided the increment is SQL-side.** If written the `approval_remind` way, yes |

---

## 5. Option B — ledger-derived state

**Not viable for cadence.**

**FACT — the ledger has no ordinal column.** Its columns are `email_id,
idempotency_key, email_kind, tier, recipient_*, subject_*, state,
decision_reason, provider*, failure_reason, attempts, *_attempted_at,
accepted_at, event_uuid, correlation_id, created_at, updated_at, origin`. The
ordinal exists **only inside the key text** (`…:remind:<n>`), so deriving it
means parsing structure out of a value whose own contract calls it
*"deterministic and content-free"*.

**Ordinal allocation would actually be safe** — two workers computing the same
`MAX+1` collide on `uq_staff_email_idem`, and exactly one proceeds. That much
is already proved.

**Cadence would not be.** Workers a moment apart read `MAX=3` and `MAX=4`,
produce different keys, and both send. Nothing collides, because they are by
construction different intents. Enforcing cooldown would require the cooldown
test and the claim to be one transaction —

**FACT: impossible without modifying the API.** `claim()` and `begin_send()`
each open their own connection via `get_connection()`, commit, and close. There
is no parameter by which a caller can enlist the claim in its transaction.

Option B therefore requires: text parsing of the key, **plus** an advisory lock
or `SERIALIZABLE`, **plus** a signature change to `begin_send()`/`claim()` to
accept an external connection. That is a larger change to a boundary this
engagement just finished hardening, for a weaker guarantee.

---

## 6. Option C — atomic ledger-side allocation

Two distinct variants, evaluated separately.

**C-1: a dedicated allocation table** (`escalation_id` → ordinal, last-at).
Functionally identical to Option A's state, relocated. It adds a table and a
join, puts escalation lifecycle state in a second place, and provides **no
guarantee A does not**. Rejected on state surface, not on safety.

**C-2: cooldown as an identity property.** Genuinely different, and worth
recording. Make the reminder's `ref` carry a time bucket —
`ref = f"{escalation_id}:{floor(now / cooldown)}"` — so two sends inside one
cooldown window compute **the same key** and collide on the existing unique
constraint. Cooldown stops being a state comparison and becomes part of the
send's identity, enforced by the constraint already proved to work, with **no
mutable counter anywhere**.

Its costs are real:

- **`REMIND_MAX` still has no home** — a bound on *how many* cannot be derived
  from a bucket, so a counter returns anyway.
- **The ordinal stops being a human-meaningful count.** The design's body text
  says *"Reminder number: n"*; a bucket index is not that.
- **Clock skew between workers shifts bucket boundaries**, so the guarantee
  becomes "one per window per worker clock" rather than one per window.

**Assessment:** C-2 is an elegant answer to cooldown alone and does not answer
the rest. Recorded rather than adopted.

---

## 7. Concurrency proof requirements

For **Option A**, the eventual producer must demonstrate:

1. Two threads on two connections, barrier-released, attempting allocation on
   one escalation → **exactly one receives a `RETURNING` row**; the other
   receives none.
2. **Mutation proof:** moving the increment out of SQL into the application
   (the `approval_remind` shape) makes that test fail. Without this the test
   does not distinguish the correct design from the known-broken one.
3. Cooldown: a second attempt inside the window returns no row.
4. `REMIND_MAX`: allocation stops at the bound and does not increment past it.
5. A terminal escalation allocates nothing.
6. End-to-end: allocation → `begin_send` with the allocated ordinal → exactly
   one accepted ledger row.

For **Option B**, additionally: proof that the chosen lock actually serialises
across connections, and that `begin_send()`'s separate transaction does not
break it. **That proof is the reason B is not recommended.**

---

## 8. Crash and retry

Allocation commits **before** the ledger claim, because the claim needs the
ordinal. So a crash between them consumes ordinal *n* with no email sent.

**Consequence, stated rather than smoothed over: the counter is an allocator,
not a send count.** It will legitimately exceed the number of accepted ledger
rows. Any operator reading it as "reminders sent" would be wrong; the ledger is
the record of what was sent.

This fails in the safe direction — **a lost reminder, never a duplicate** — and
the next tick allocates *n+1* once cooldown elapses. The existing lease model is
unaffected: it governs the interval between claim and provider call, which is
downstream of allocation.

---

## 9. The invariant the producer must satisfy

> **A worker may form a reminder intent only by winning a single atomic state
> transition that simultaneously verifies liveness, unclaimed status, threshold,
> cooldown and maximum, and allocates the ordinal it will use. Eligibility that
> is read before it is written is not eligibility.**

Plus the two inherited constraints:

- Unclaimed is `assigned_at IS NULL`. **Never `assigned_to`** (contaminated on
  36 live rows), **never `status`** (violated on 4).
- The ledger remains the exactly-once guard for the intent once formed. It is
  not duplicated and not relied on for cadence.

---

## 10. Recommendation — **Option A**

Smallest new state surface that provides the required proof, using a pattern
this codebase has already proved three times (`acquire()`,
`_set_handling`, `assign()`).

It is the only option that closes the **candidate-selection** race as well as
the allocation race, because selection and allocation become the same
statement.

**The decisive caveat, and the reason this record exists:** Option A is only
safe if the increment is computed by the database and every condition sits in
the predicate. Written the `approval_remind` way it reproduces that race
exactly. **The difference is not the column; it is where the new value is
computed.**

---

## 11. Schema change actually required

**Two columns on `escalations`:** a reminder count and a last-reminder instant.
Names deliberately not chosen — §3 of the C1 record showed what reusing a name
with an established different meaning costs, and `escalation_notices` already
means something on two other tables.

`REMIND_AT_FRACTION`, `REMIND_MAX` and the cooldown are **configuration, not
state**, and must not become columns.

**Separately and not part of this:** the missing DB-level invariant from §3
(`status='assigned'` ⇒ `assigned_at IS NOT NULL`).

---

## 12. Existing APIs needing modification

**Under Option A: none.** `begin_send()`, `claim()`, `ledger_identity()`,
`assign()` and `_update()` are all untouched. The producer allocates, then calls
`begin_send()` with the allocated ordinal through the existing boundary.

**Under Option B:** `begin_send()` and `claim()` would need to accept an
external connection — a signature change to the boundary just hardened in A.
This asymmetry is the clearest argument between the options.

---

## 13. NOT ESTABLISHED

- **That any escalation reminder is needed in production.** 136 of 138 local
  escalations are test data, and production has produced zero escalations.
  Local eligibility counts prove nothing about production.
- **That `REMIND_MAX` and the cooldown values in the design are still intended.**
  They come from an unbuilt stage and were never operated.
- **That the four ambiguous rows are the only violations.** The invariant is
  unenforced, so others may be created at any time by a direct writer.
- **That Option A's predicate is complete.** It is a shape, not an
  implementation; a real one may need conditions this analysis has not found.
- **That the recommendation survives contact with the producer.** The proof
  obligations in §7 are exactly the things that could refute it.

---

## 14. Next authorization requested

**Not the producer.** In order:

1. **Ratify Option A** (or reject it) as the state-placement architecture.
2. **Decide the two column names and their semantics**, avoiding
   `escalation_notices` — a name that means something else on two tables.
3. **Disposition the four ambiguous rows**, and decide whether the missing
   `status='assigned'` ⇒ `assigned_at IS NOT NULL` invariant becomes a
   constraint. Separate finding, separate decision.
4. **Then** authorize the migration, under the existing governance path:
   `OUT_OF_BAND_SQL` / PENDING DEPLOYMENT, never `REQUIRED_MIGRATIONS` until
   Railway has run it.
5. **Only then** authorize `unclaimed_reminders()`, with the §7 proofs required
   before it is considered complete.

**Classifications carried forward unchanged:** A remediated locally · B
remediated locally · `escalation.assign()` race remediated with concurrent
proof, production occurrence NOT ESTABLISHED · `approval_remind` race separate ·
`alert_remind` race separate · duplicate governance mail NOT ESTABLISHED ·
reminder-ref collision HYPOTHESIS · four ambiguous rows a separate
state-integrity finding · C untouched.
