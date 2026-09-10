# Re-Assessment — Defects A, B, C

**Date:** 2026-09-09 · **Revision:** 2, superseding
`assessment_2026-09-09_defects_A_B_C.md` · **Status:** assessment only. No code,
schema, configuration or data was changed.

---

## Purpose and standing of this revision

Revision 1 established measurements that contradicted the original descriptions
of all three defects. This revision treats those measurements as evidence and
re-derives each finding from them, rather than restating revision 1's
conclusions.

**Two of revision 1's own conclusions do not survive that process:**

| | revision 1 said | revision 2 measures |
|---|---|---|
| **A** | the constraint was never widened when a new channel was added | the vocabulary is **complete and agreed** in both layers; the new channel **bypasses the gate that enforces it**. Constraint staleness is not the defect |
| **C** | three failing tests, two causes — the third "fails for a different reason" | **one** root cause. The third test is a second instrument measuring the same phenomenon. The mechanism revision 1 left unresolved is resolved below |

Revision 1's architectural question for A — *"is the ledger authoritative?"* —
is **withdrawn**. It is answered by measurement, not by decision, and it is
answered *yes*.

**Labels.** **MEASURED** — observed directly here. **INFERRED** — concluded from
measurement, with the inference named. **UNEXPLAINED** — a mechanism that
remains open, distinguished from merely unverified.

---

# Defect A — A ledgered write path bypasses the vocabulary gate that governs it

## A.1 Corrected finding

**The finding is not that a constraint went stale.** The system declares its
email vocabulary in two places and **they agree exactly**:

- `staff_email.EMAIL_KINDS = ("approval", "escalation", "escalation_remind", "digest")`
- `staff_email_ledger_email_kind_check` — the identical four values

Set difference in both directions is **empty**. There is no drift between code
and schema, and nothing was left un-widened relative to anything else.

The system also already has a validation gate for exactly this failure, and it
names it precisely — `decide()`, `staff_email.py:665`:

```python
if kind not in EMAIL_KINDS:
    return _no(f"unknown email kind {kind!r}", "unknown_kind")
```

It is the **first** check in that function, placed there deliberately as the
cheapest and most certain refusal.

**The corrected finding: there are two write paths into the ledger, and only one
passes through the gate.**

| path | route | vocabulary enforced by |
|---|---|---|
| `observe()` | → `decide()` → validated | **the gate**, refusing cleanly with `reason_class="unknown_kind"` |
| `email_authority()` | → `begin_send()` → `claim()` → `INSERT` | **the database CHECK alone** — reached after the fail-open is already structured to swallow it |

`begin_send()` never calls `decide()`. So five undeclared kinds
(`alert_assigned`, `alert_escalated`, `alert_reescalation`, `approval_breach`,
`approval_reescalation`) reach PostgreSQL, where the CHECK is the only remaining
enforcement — and `claim()` catches the violation, rolls back, logs a WARNING,
and returns a value `begin_send()` interprets as *proceed unrecorded*.

**The vocabulary is wrong in both directions**, which is the clearest evidence
that it was never reconciled with its producers: `escalation_remind` is declared
in both layers and has **zero** emitters, while five emitted kinds are declared
in neither.

**The false invariant stands, and its wording matters.**
`governance_alerts.py:189` asserts *"email_authority is additionally ledgered per
(kind, ref) through staff_email, so a retry cannot double-send."* The word is
*additionally* — the author was describing a **second** layer. That is
diagnostic: the primary guards were built deliberately, and the ledger was
believed to be defence in depth behind them.

## A.2 Measured evidence

**MEASURED — the two declarations are identical.**

```
EMAIL_KINDS (python): ('approval', 'escalation', 'escalation_remind', 'digest')
DB CHECK            : ['approval', 'digest', 'escalation', 'escalation_remind']
in python not DB    : []
in DB not python    : []
emitted by email_authority, in NEITHER:
    ['alert_assigned', 'alert_escalated', 'alert_reescalation',
     'approval_breach', 'approval_reescalation']
```

**MEASURED — the gate exists and is bypassed.** `EMAIL_KINDS` is referenced at
exactly three points in `staff_email.py`: its definition (171), a comment (587),
and the validation inside `decide()` (665). **`begin_send()` does not reference
it and does not call `decide()`.**

**MEASURED — producers per declared kind:** `approval` 2, `escalation` 2,
`digest` 3, **`escalation_remind` 0**.

**MEASURED — the gap was known and written down.** `staff_email.py:587`:
*"`EMAIL_KINDS` has no `alert` member, and `escalation_remind` has no producer
either."* The vocabulary shortfall was documented; the consequence for
`begin_send()` was not drawn.

**MEASURED — each of the five kinds has a sound independent first-layer guard.**
This materially narrows the impact and revision 1 understated it:

| kind | primary guard | assessment |
|---|---|---|
| `alert_assigned` | mails **only on creation**; a dedupe fold returns without mailing | sound by construction |
| `alert_escalated` | requires the `open → escalated` transition, which the lifecycle trigger permits once | sound by construction |
| `alert_reescalation` | conditional `UPDATE … SET escalation_notices=%s … WHERE alert_id=… AND status='escalated'`, `rowcount != 1 → rollback; continue`, **stamped before the send** | a correct compare-and-set |
| `approval_breach` | approval state machine | **not traced** |
| `approval_reescalation` | reminder counter, same shape as alerts | **not traced** |

**MEASURED — no control would detect this class of defect.** The schema holds
**38** vocabulary CHECK constraints. No test, script or startup check
cross-references any of them against the literals the code emits. The only test
touching email kinds (`test_B0_approval_and_digest_are_different_email_kinds`)
asserts that two members exist — not that every emitted kind is a member.

**MEASURED — production.** `migrate.py --check --target railway` reports
`schema is current`; `staff_email_ledger.sql` is in `REQUIRED_MIGRATIONS`; no
migration in the repository alters the constraint.
**INFERRED:** production carries the identical constraint. The inference step is
that "schema is current" implies constraint-level identity, which the tooling
does not verify directly.

## A.3 The architectural question

**Classification: a policy gate that is not on the path it governs.**

Not a data-modelling question, not a schema-maintenance question. The
vocabulary, its declaration and its enforcement all exist and are mutually
consistent. What does not exist is any guarantee that a *writer* passes through
the enforcement.

The question this raises, stated at the level it actually lives at:

> **When a system has a policy gate and a persistence layer that both enforce
> the same rule, what guarantees that every writer traverses the gate rather
> than reaching persistence directly — and what should happen when one does?**

Three properties of this instance make it worth generalising:

1. **The database CHECK was the last line, and a fail-open disarmed it.** The
   fail-open is correct — *"an executive must not miss an escalation because an
   audit table was missing"* — but its effect is that the final enforcement
   layer produces a WARNING and no other consequence. A control whose only
   remaining enforcement is fail-open is not a control.

2. **The bypass is invisible in every default environment.**
   `email_authority` returns early when `GOV_ROUTE_EMAIL` is unset, which is the
   local default. The violation cannot occur in a normal development run, so the
   WARNING never appears where a developer would see it.

3. **`begin_send()` is the more privileged path and has the weaker checks.**
   `observe()` acts on nothing and validates fully; `begin_send()` claims a send
   and validates nothing. The gate strength is inverted relative to authority.

**Revision 1's question is withdrawn.** *"Is the ledger authoritative for
governance mail?"* presupposed ambiguity. Python and schema agree exactly, the
gate names the failure explicitly, and the code comment claims the guarantee —
every layer asserts the ledger is authoritative. It is not a decision to be
taken; it is a fact the write path fails to honour.

## A.4 Blocking decision

**D-A1 (blocking) — Must every ledger writer traverse `decide()`?**
Everything else is downstream. If yes, `begin_send()` gains a gate and
undeclared kinds are refused *before* the fail-open, changing the failure from
silent-unrecorded to refused-and-named. If no, the vocabulary contract is
advisory, and both `EMAIL_KINDS` and the CHECK are documentation rather than
enforcement — which must then be stated, because the code currently claims
otherwise.

**D-A2 (blocking, independent) — What is the correct vocabulary?** Distinct from
D-A1 and not answered by it. Five kinds are emitted and undeclared; one is
declared with no producer. Whether governance mail deserves five distinct kinds,
or is one kind with a subtype, or belongs in a separate ledger, is a modelling
decision. **Widening the CHECK to five values without taking this decision
ratifies whatever the callers happened to pass.**

**D-A3 — Must a fail-open control raise an alarm?** The fail-open should stay.
Whether "proceeding UNRECORDED" is permitted to remain a log line, in a channel
disabled by default, is a policy decision about the class of controls.

**D-A4 — Does the false invariant get corrected, or made true?** The comment
claims a guarantee that has never held. Deleting it and implementing it are both
defensible; leaving it is not.

**D-A5 — Is the vocabulary-conformance gap itself a finding?** 38 constraints,
zero conformance controls. Whether that becomes a control is a decision beyond
this defect, and this defect is the evidence for it.

## A.5 Unresolved mechanisms

- **UNEXPLAINED — why `begin_send()` was built without the gate.** Whether
  `decide()` post-dates it, or the two were always intended as separate
  contracts, was not established. This determines whether D-A1 is a repair or a
  design change.
- **UNVERIFIED — the guards for `approval_breach` and `approval_reescalation`.**
  Three of five were traced and found sound. These two were not. **Until they
  are, "every kind has a sound primary guard" is a claim about three fifths of
  the surface.**
- **UNVERIFIED — how many sends have gone unrecorded, over what period.**
  Requires production log history, not the database.
- **UNVERIFIED — whether any duplicate governance mail was ever transmitted.**
  State neither that it occurred nor that it did not.
- **UNVERIFIED — whether the other 37 vocabulary constraints have live
  violations.** The conformance *gap* is measured; instances beyond this one are
  not.
- **UNVERIFIED — direct production read of the constraint.**
  `SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'staff_email_ledger_email_kind_check';`

---

# Defect B — A test asserts a census over shared, persistent governance state

## B.1 Corrected finding

Revision 1's account survives re-derivation intact; what changes is the level at
which the defect is stated.

The proximate mechanism is confirmed: a test at `:1005` trips the proposal cap
and leaves its `sampled_review` alert open; the cap alert's dedupe key is
date-stamped; at the UTC date change the key differs, the fold stops, two open
rows exist, and `assert len(rows) == 1` fails. The failing test's own `finally`
then cancels both, so the failure is self-healing and presents as flake.

**The corrected framing:** the date stamp is not the defect. It is the trigger
that exposes one. The defect is that **a test asserts an absolute count over a
table it shares with every other test and with prior runs**, so its correctness
depends on the cleanup discipline of unrelated tests and on the wall clock.
The date rollover is simply the cheapest way to violate that dependency; a
crashed test, a parallel worker, or a re-ordered run would do the same.

## B.2 Measured evidence

**MEASURED — the leak, reproducibly.** After cancelling all open
`sampled_review` rows and running the file once:

```
LEFTOVER OPEN sampled_review rows after one full file run: 1
   ('test.activation_write', 'proposal_cap:test.activation_write:2026-09-09', 'open', 'governance')
```

**MEASURED — the producer and the key.** `governance.py:807-818`, with
`dedupe_key = f"proposal_cap:{action_type}:{day}"`, `day` in UTC, and
`affected_id = action_type`.

**MEASURED — the leaking test.** `:1005` asserts only on `action_approvals` and
performs no alert cleanup. `:1013` cleans up, but only the rows **it queried**.

**MEASURED — the rollover, reproduced rather than argued.** Backdating the
leftover row's key by one day and re-running:

```
assert len(rows) == 1, "one work item per class per day, not one per refusal"
AssertionError: assert 2 == 1
```

Identical assertion and shape to the failure observed in the wild. Simulated
rows were cancelled afterwards; no state was left behind.

**MEASURED — a second producer accumulates in the same class.** `a2a.py:1705`
opens `sampled_review` alerts with **no dedupe key** and `affected_id` set to an
approval UUID: 43 rows all-time. They do not match the test's predicate today,
and none is currently open. **INFERRED:** with no dedupe key, this producer has
no fold and accumulates one row per occurrence; a broadened predicate would
capture them.

**MEASURED — production behaviour is correct.** One cap alert per class per day,
folding on redetection, is what the producer does.

## B.3 The architectural question

**Classification: a control whose correctness depends on state it does not own.**

This is the same proxy-versus-property shape corrected six times in production
code during P1 — a control keyed on a count rather than on the property it
protects — surviving in the test layer, which that sweep did not cover.

> **May a test assert an absolute count over a shared, persistent table it does
> not exclusively own — and if not, what replaces the assertion, given that the
> property being guarded is genuinely a count?**

The difficulty is that the invariant is real and valuable: *one work item per
class per day, not one per refusal* is an attention-budget guarantee, and
counting is the natural way to express it. The defect is not that the test
counts; it is that it counts a population it does not control. Any answer must
preserve the invariant while removing the shared dependency.

Secondary property: **the self-healing is what makes this expensive.** A control
that fails at most once per day and repairs itself trains its readers to
re-run rather than read, which is a slow tax on every unrelated failure.

## B.4 Blocking decision

**D-B1 (blocking) — Structural isolation, or continued cleanup?**
Cleanup is per-test discipline that the next author to forget breaks again, and
the leak is evidence the discipline already failed once. Isolation — a dedicated
action class per test, or a transactional boundary — makes the class of defect
impossible and changes how governance tests are written.

**D-B2 — Does the ruling against census assertions extend to tests?** The
project has already ruled against count-versus-constant controls in production
code. Whether that ruling binds the test suite is unmade, and it governs more
than this assertion.

**D-B3 — Is a once-a-day self-healing failure accepted until B is scheduled?**
Genuinely low severity. Accepting it *explicitly*, with the mechanism recorded,
differs from leaving it to be rediscovered as flake.

**D-B4 — Should the keyless `a2a` producer have a dedupe key?** A production
question surfaced by this defect and not part of it: a `sampled_review` producer
with no fold accumulates one row per occurrence.

## B.5 Unresolved mechanisms

- **UNVERIFIED — determinism across orderings.** Measured once, in source order.
  Under randomised or parallel execution the interaction may differ, and
  parallel workers would break the assertion by a second route.
- **UNVERIFIED — whether other test files leak non-terminal governance rows.**
  Only `sampled_review` was examined, because only it is asserted on.
- **UNVERIFIED — whether the failure has occurred in CI.** History not consulted.
- **UNVERIFIED — behaviour across a non-UTC boundary.** The key is UTC; the
  measured environment is not.
- **UNVERIFIED — whether the `a2a` producer's rows are bounded** by any retention
  or sweep.

---

# Defect C — Template crowding, measured by two instruments, with one root cause

## C.1 Corrected finding

**Revision 1's unresolved mechanism is resolved, and resolving it merges two
findings into one.**

Revision 1 reported three failing tests with two causes, and could not explain
why a pinned result count **fell** from 4 to 2 when crowding would be expected
to add near-duplicates rather than remove them. The measurement below answers
it, and the answer is that crowding **reduces** the distinct result count.

The retrieval path ranks a budget of candidates, applies a similarity floor, and
then **collapses near-identical results by template fingerprint**. As one
template grows to dominate the ranked budget, more of that budget is spent on
rows that dedupe collapses into a single entry — so the number of *distinct*
results falls. More crowding, fewer answers.

**Therefore all three failing tests share one root cause.** Two report it
through the drift guard's ratio; the third reports it behaviourally, as a moved
result count. Revision 1's claim that the third "fails for a different reason"
is **withdrawn**. It fails for the same reason, observed through a different
instrument — which makes it corroboration, not a separate defect.

The corrected description of the underlying condition, restated from revision 1
and unchanged by this revision:

- `548` is the **largest template group**, not corpus size.
- Corpus growth (12,976 → 14,785, +13.9%) trips nothing; the threshold is ×2.
- `duplicate_share` **fell** (0.530 → 0.525).
- The grown group is workflow-engine output:
  `"Send payment reminder — Created by workflow engine from even…"`.
- The index is **91% `activity` rows** (13,388 of 14,785).
- The number actually outgrown is `MAX_CANDIDATES = 4000`: **27%** of the corpus
  is ranked, disclosed by the search path on every call.

## C.2 Measured evidence

**MEASURED — the mechanism, from the search path's own diagnostics.** Under
`recency_only`, for the three pinned queries:

| query | candidates | ranked | **dedupe_count** | final |
|---|---|---|---|---|
| `payment reminder` | 4000 | 30 | **28** | 2 |
| `order shipped` | 4000 | 30 | **28** | 2 |
| `pricing discussion` | 4000 | 30 | **29** | 1 |

`ranked_count = 30` is the full budget (`limit * 6` in recency-only mode), so
**every candidate cleared the similarity floor**. Between 93% and 97% of them
were then collapsed as template duplicates.

This is the resolution: the ranked budget is saturated with near-identical
workflow-generated rows, and dedupe reduces them to one entry each. The pinned
count fell because fewer *distinct templates* now survive in the top 30 — not
because retrieval failed, and not because the kill switch changed.

**It is not specific to the payment-reminder query.** All three queries show the
same saturation, which makes this a property of the corpus, not of one template.

**MEASURED — the code is unchanged.** The structural kill-switch test
(`test_recency_only_keeps_the_legacy_budget`), which asserts the mode-dependent
ranking budget is present in `search()`, **passes**. Recent commits to
`content_index.py` concern status reporting, not ranking.

**MEASURED — the guard's own numbers:**

| property | validated 2026-09-01 | now | threshold | fires |
|---|---|---|---|---|
| rows | 12,976 | 14,785 | ×2 or ÷2 | no |
| largest template group | 480 | **548** | ratio > 1.056 | **yes** |
| duplicate share | 0.530 | **0.525** | +0.15 | no |

The exceedance is narrow: the trip point is 528, and the measured value is 548.

**MEASURED — composition.** The group largest at validation (480,
`"Requested additional information from customer."`) is **unchanged at 480**.
The group that overtook it is workflow-engine output. Top groups are
overwhelmingly machine-generated.

**INFERRED (well-supported):** the growth is the workflow engine indexing its own
generated activity rows, not ingestion of human content. Supported by the group
text, the 91% activity share, and the untouched 480 group. **Not established:**
the rate, or whether it is bounded.

## C.3 The architectural question

**Classification: a retrieval corpus that is predominantly the system's own
output, tuned against a baseline taken when that was already true.**

The drift guard behaved exactly as designed. `N_VEC` carries a written
instruction — *"revalidate if the corpus changes materially, do not silently
retune"* — and the codebase converted that instruction into a check that fires,
maintaining two flags separately and deliberately so that a permanent disclosure
and a change signal cannot silence each other. **This should not be read as a
system failure. It is a control reporting that its evidence has expired.**

The question underneath is not about pool size:

> **Should a retrieval index over "what the business knows" contain the
> business's own automated output — and if it must, should that output compete
> for the same ranked budget as human-authored content?**

The measurement gives this force. The ranked budget is not merely *influenced*
by machine-generated rows; it is **saturated** by them — 28 of 30 candidates
discarded as template duplicates, across every query tested. Retrieval currently
spends almost its entire working budget on text the system wrote to itself, and
recovers a usable answer only because dedupe collapses the crowd afterwards.

Three consequences follow, and only the first is about tuning:

1. **The quality guarantee is out of warranty.** Zero-content-loss was measured
   at one composition, which no longer holds. Nothing here shows quality has
   degraded — it shows the evidence has expired. These are different claims and
   the second must not be reported as the first.
2. **Every threshold is being tuned against the system's own exhaust.**
   `N_VEC`, `MAX_CANDIDATES` and the dedupe fingerprint are all calibrated
   against a corpus that is 91% activity rows. Raising the pool would enlarge
   the crowd along with everything else.
3. **Coverage decays by construction.** A fixed 4,000-row recency slice against
   a growing corpus means the disclosed invisible share rises monotonically —
   69% at validation, 73% now. Disclosed on every search, and the property most
   likely to be mistaken for a bug.

**On the third test.** It pins absolute counts against a live corpus, and its
docstring defends this soundly — *"a self-comparing test passes when both sides
drift together, which is the failure it exists to catch."* The argument is
correct and the test did its job: it detected a real change. Its limitation is
that it cannot say **which** change, reporting *"the kill switch no longer
restores the pre-hybrid contract"* when the kill switch is intact and the corpus
moved. It is the same shape as Defect B — an assertion over a population the
test does not control — with a materially better justification behind it.

## C.4 Blocking decision

**D-C1 (blocking, and prior to all others) — Should system-generated content be
indexed?** Everything else depends on it. If workflow-engine activity rows are
indexed because *everything* is indexed rather than because retrieval needs
them, then every threshold here is calibrated against noise and revalidation
would re-validate the noise. **Answer this before any measurement is commissioned.**

**D-C2 — Revalidate, re-tune, or accept?** Three different commitments.
Revalidation means re-running the end-to-end content-loss measurement at the
current composition and re-recording the baseline — which the codebase states is
*"a decision, not a heuristic"*. Re-tuning `N_VEC` without that measurement
replaces validated evidence with a guess. Accepting means recording that the
configuration is out of warranty and tolerating it.

**D-C3 — Should machine-generated and human-authored content share one ranked
budget?** Raised directly by the 28-of-30 measurement. Separate pools, a
composition quota, or exclusion are different systems, and the question is
prior to any threshold change.

**D-C4 — Should the recency slice scale with the corpus?** `MAX_CANDIDATES` is
fixed while the corpus is not, so disclosed coverage decays by construction.
Whether that is intended steady-state behaviour is unmade.

**D-C5 — What should a behavioural pin over live data assert?** The count, the
identity of results, or a property such as *non-empty and correctly ordered*.
Governs this test and every future test of its shape. Shares its substance with
**D-B2**.

**D-C6 — Is a standing red suite acceptable?** Three tests correctly report an
expired baseline and will stay red until D-C2 is taken. A permanently red suite
is itself a control that decays.

## C.5 Unresolved mechanisms

- **RESOLVED — why the pinned count fell 4 → 2.** Template dedupe collapses the
  crowd: 30 ranked, 28 removed as duplicates, 2 distinct survivors. Crowding
  reduces distinct results rather than increasing them. Revision 1 listed this
  as unexplained; it is closed.
- **UNEXPLAINED — why `duplicate_share` fell while the largest group grew.**
  A larger dominant group with a lower overall duplicate share implies smaller
  groups thinned or many unique rows were added. Not investigated, and it bears
  on whether the corpus is diluting or concentrating.
- **UNEXPLAINED — what the pinned counts would be at the validated composition.**
  Whether 4 was ever stable, or was itself a snapshot of a moving population, is
  unknown. This determines whether the pin was ever a sound control.
- **UNVERIFIED — growth rate and bound of the workflow template group.**
  Measured once. Linear, retention-bounded, or unbounded changes the urgency
  entirely and is the single most decision-relevant unknown here.
- **UNVERIFIED — production corpus state.** All figures are local. Production may
  sit on a different side of every threshold.
- **UNVERIFIED — actual retrieval quality.** Nothing here measures it. **Do not
  report this as a retrieval-quality regression.**

---

## Summary

| | classification | severity | blocking decision |
|---|---|---|---|
| **A** | policy gate not on the path it governs | **High** — audit integrity, plus a written invariant that is false | D-A1: must every ledger writer traverse `decide()`? |
| **B** | control depending on state it does not own | Low — no production impact | D-B1: structural isolation, or continued cleanup? |
| **C** | index predominantly the system's own output | Medium — warranty expired, degradation unproven | D-C1: should system-generated content be indexed at all? |

**What re-derivation changed.** Both corrections moved the finding *up* a level.
A is not a stale constraint but an unguarded write path — so widening the CHECK
would have resolved the symptom and left the bypass in place. C is not two
defects but one, and the instrument that looked broken was corroborating the
other two — so re-pinning that test would have discarded a true signal.

**A remains the one that matters.** B is a test-hygiene defect with a measured
mechanism and no production consequence. C is a control correctly reporting
expired evidence — the system behaving as designed, requiring a decision rather
than a repair. A is a governance control that has never operated, in a system
whose central claim is that governed actions leave durable evidence, and whose
source asserts the guarantee as fact.

**Three cross-cutting questions outlive all three defects**, and each is the
general form of a specific finding above:

1. **What guarantees a writer traverses the gate that governs it?** (A)
2. **May a control assert over a population it does not own?** (B, and C's
   pinned test)
3. **Is a system's own output evidence about the business?** (C)

**No remediation is proposed, by design.** In two of three cases the obvious fix
— widen the constraint, raise the pool — resolves the symptom while leaving the
architectural question unasked, and in both cases the re-derivation above is
what makes that visible.
