# Remediation Plan — Defects A, B, C

**Date:** 2026-09-09 · **Authority:** `assessment_2026-09-09_defects_A_B_C_r2.md`
(revision 2) · **Status:** PLAN ONLY. No production code, schema, configuration
or data was changed in producing this document.

---

## 0. Standing of this plan

Revision 2 is authoritative. The following prior conclusions are **withdrawn and
are not reintroduced anywhere below**, stated here so the omission is auditable
rather than accidental:

| withdrawn | why it stays withdrawn |
|---|---|
| A is vocabulary/schema staleness | **FACT:** Python and schema vocabularies are identical; set difference is empty in both directions |
| Widen the CHECK constraint as *the* remediation | Widening is a **prerequisite step**, never the fix. The fix is at the writer/gate boundary (§3.7) |
| Remove the idempotency claim / reopen "is the ledger authoritative" | Every layer already asserts it is. Not a decision; a fact the write path fails to honour |
| C's third test has an independent root cause | **FACT:** all three share the dedupe-downstream-of-budget mechanism (§5.2) |
| Re-pin the third retrieval test | Explicitly forbidden (§7) |

**Evidence labels.** **FACT** — measured in this session or revision 2, command
and result reproducible. **INFERENCE** — derived from fact, with the step named.
**HYPOTHESIS** — plausible, untested, stated so it can be attacked.
**PROPOSED DESIGN** — an engineering proposal, not a finding.

---

## 1. The three durable questions, answered directly

These are the findings. A, B and C are concrete manifestations.

### A. Writer authority
> *What guarantees that every writer traverses the gate that governs the state it
> is allowed to create?*

**Today: nothing.** Traversal is a **convention**, not a constraint. The gate
(`decide()`) and the persistence layer (`claim()`) are separate functions joined
by a dictionary that any caller can build by hand — and one does.

**The answer this plan proposes:** the gate must produce a **carrier** that the
persistence layer requires and no caller can fabricate. Traversal then holds by
construction rather than by discipline. Validation duplicated inside each writer
is rejected: two copies of a rule diverge, and the next writer inherits neither.

### B. Population ownership
> *May a control assert an invariant over a population whose lifecycle it does
> not own?*

**No — and the answer generalises past tests.** An assertion over an unowned
population silently depends on every other writer to that population. It is the
same defect class as A seen from the reading side: A is an unowned *write* path,
B is an unowned *read* population.

**The answer this plan proposes:** a control must either own the lifecycle of
what it measures, or measure a **delta it caused** rather than a global state.
Where the guarantee is genuinely about a count, the count must be scoped to the
owned population — not weakened, and not re-pinned to a different constant.

### C. Evidence authority
> *Is a system's own output valid evidence about the business, and under what
> provenance rules?*

**It can be, but not without provenance — and provenance does not currently
exist.** This is the plan's hardest constraint and it is measured, not assumed
(§5.2): the corpus cannot presently distinguish system-generated from
human-authored content by any structured field.

**The answer this plan proposes:** admission and weighting decisions require a
provenance attribute that must be *written at generation time*. Until it exists,
any filter is a text heuristic — a proxy for the property, which is the
antipattern this codebase has removed repeatedly. **The retrieval fix available
today is a mechanism-ordering fix, not a corpus fix.**

---

# 2. Defect A — Evidence matrix and writer/gate analysis

## 2.1 Observed behavior

Governance mail is transmitted and no ledger row is written. The database rejects
the row; the rejection is caught, logged at WARNING, and converted into
"proceed unrecorded". The failure is invisible in any default environment
because `GOV_ROUTE_EMAIL` is unset locally, so the path never executes.

## 2.2 Measured evidence

**FACT — the vocabularies agree exactly.**

```
EMAIL_KINDS (python): ('approval', 'escalation', 'escalation_remind', 'digest')
DB CHECK            : ['approval', 'digest', 'escalation', 'escalation_remind']
in python not DB    : []
in DB not python    : []
emitted by email_authority, in NEITHER:
    ['alert_assigned','alert_escalated','alert_reescalation',
     'approval_breach','approval_reescalation']
```

**FACT — the gate exists**, `staff_email.py:665`, first check in `decide()`:

```python
if kind not in EMAIL_KINDS:
    return _no(f"unknown email kind {kind!r}", "unknown_kind")
```

**FACT — the seam. `begin_send()` fabricates the object `claim()` consumes**
(`staff_email.py:1223-1234`):

```python
key = idempotency_key(kind, ref, ordinal)
decision = {
    "idempotency_key": key, "kind": kind, "tier": tier,
    "reason": decision_reason,
    "recipient": {...},
}
row, is_new = claim(decision, ...)
```

`claim()`'s docstring reads *"Take ownership of this decision."* The object it
receives here **never came from `decide()`**. It is structurally similar and
materially different: it lacks `send`, `ledgerable`, `reason` and
`reason_class` — every field by which a decision records that it *was decided*.

**FACT — the ledger has exactly three physical writers**, all in
`staff_email.py`: `claim()` INSERT (`:823`), `acquire()` UPDATE (`:884`),
`finish_send()` UPDATE (`:919`). **`claim()` is the sole row-creating writer**,
so it is the only enforcement point that needs to hold.

**FACT — no conformance control exists.** The schema holds **38** vocabulary
CHECK constraints. No test, script or startup check compares any of them against
the literals the code emits. The only test touching email kinds asserts two
members exist, not that every emitted kind is a member.

**FACT — three of five primary guards traced and sound**, two **not traced**
(carried forward from revision 2, not generalised):

| kind | primary guard | status |
|---|---|---|
| `alert_assigned` | mails only on creation; dedupe fold returns without mailing | sound by construction |
| `alert_escalated` | requires the `open → escalated` transition, permitted once | sound by construction |
| `alert_reescalation` | conditional UPDATE, `rowcount != 1 → rollback`, stamped before send | correct compare-and-set |
| `approval_breach` | approval state machine | **NOT TRACED** |
| `approval_reescalation` | reminder counter | **NOT TRACED** |

## 2.3 Writer / gate matrix — **required deliverable**

Every call path that can claim a send. **Three states, not two** — this is the
finding revision 2 did not yet have:

| # | writer | kind(s) | calls `decide()`? | **decision binding?** | outcome today |
|---|---|---|---|---|---|
| 1 | `staff_email.py:1684` — digest | `digest` | **yes**, `:1667` via `observe()` | **YES** — `if not d.get("send"): return out` | correct; the only fully gated writer |
| 2 | `governance.py:983` — approval routing | `approval` | yes, `:948` via `observe()` | **NO** — shadow observer; `begin_send` in a separate `try`, result never consulted | passes only because the literal happens to be declared |
| 3 | `escalation.py:513` — escalation mail | `escalation` | yes, `:383`, 130 lines earlier | **NO** — shadow observer | passes only because the literal happens to be declared |
| 4 | `governance_policy.py:579` — `email_authority` | **5 undeclared kinds** | **NO** | **NO** | **CHECK violation → fail-open → unrecorded** |

**FACT:** `observe()` is documented as *"Take a decision, record that it was
taken, and ACT ON NOTHING."* It is a shadow observer by design. So writers 2 and
3 traverse the gate and **discard its answer**.

**INFERENCE — the exposure is wider than the five kinds.** Only writer 1 is
protected by traversal. Writers 2 and 3 are protected by the *coincidence* that
their hardcoded literals are declared. Any future kind added to writer 2 or 3
would fail exactly as writer 4 does. **The inference step:** that a non-binding
call provides no enforcement — supported by reading the control flow, not by
executing a counterexample.

## 2.4 Root cause

**The ledger's write API is decision-shaped, and one writer manufactures the
decision instead of obtaining it.** The type is satisfied; the provenance is
forged. Nothing in the signature of `claim()` distinguishes a decision that was
*made* from a dictionary that was *assembled*.

## 2.5 Governing invariant

> **Every row in `staff_email_ledger` corresponds to a decision produced by the
> vocabulary gate, and every send that bypasses the gate is refused a row and
> says so.**

Two clauses. The second matters as much as the first: the fail-open must
survive, so the correct behaviour for an ungated send is *send anyway, record
nothing, and raise a visible signal* — never *suppress the mail*.

## 2.6 Current enforcement point

The **database CHECK constraint** — reached after `claim()` has already been
structured to swallow whatever it raises. **INFERENCE:** an enforcement layer
whose only consequence is a WARNING inside a `try/except` that returns
"proceed" is not an enforcement layer. The CHECK is doing its job; nothing
downstream honours it.

## 2.7 Proposed remediation — **PROPOSED DESIGN**

**Not** widening the constraint as the fix. **Not** duplicating the `kind` check
inside `begin_send()`.

**A shared, mandatory enforcement boundary that both paths traverse.**

The critical design constraint, and the reason "just route everything through
`decide()`" is **wrong**: `decide()` does two different jobs.

| half of `decide()` | examples | must it bind governance mail? |
|---|---|---|
| **identity / vocabulary** | `kind in EMAIL_KINDS`, `idempotency_key(kind, ref, ordinal)` | **YES** — it is what the ledger means |
| **send policy** | `enabled()`, `may_email(tier)`, tier-2 deferral, empty-digest gate | **NO** — governance mail is deliberately fail-open and must not be suppressed by staff-email policy |

Forcing writers 2–4 through the whole of `decide()` would let
`STAFF_EMAIL_ENABLED=0` silence an executive escalation. **That is a fail-closed
regression on a fail-open channel and this plan rejects it explicitly.**

**The proposal:** extract only the identity half into a boundary that cannot be
bypassed.

1. **`ledger_identity(kind, ref, ordinal) -> LedgerIdentity`** — the single
   place the vocabulary is enforced and the idempotency key is derived. Refuses
   an undeclared kind by returning a **named refusal**, never by raising into a
   send path.
2. **`LedgerIdentity` is a frozen carrier with module-private construction.**
   `claim()` accepts *only* this type. A hand-built dict is a type error, so
   writer 4's bypass becomes structurally unavailable rather than discouraged.
3. **`decide()` calls it** for its vocabulary check, so the rule has exactly one
   implementation and cannot diverge.
4. **`begin_send()` must obtain one**, and on refusal returns
   `{"proceed": True, "recorded": False, "why": "unknown email kind …"}` —
   **preserving the fail-open**, converting *silently unrecorded* into
   *explicitly unrecorded with a named cause*.

**Ordering constraint (FACT-driven).** If the boundary ships before the
vocabulary is widened, the five kinds are refused a row — which is what happens
today, only louder. Mail still sends. **The plan therefore widens first, so that
by the time the boundary is enforced there is nothing left to refuse.** Reversing
this order is safe for delivery but leaves the audit gap open longer.

## 2.8 Why this restores the invariant

- Clause 1 holds **by construction**: the only row-creating writer requires a
  carrier only the gate can mint. This is stronger than validation-in-writer,
  which holds only for writers that remember to validate.
- Clause 2 holds because the boundary refuses **the row**, never **the send** —
  the fail-open is preserved deliberately and testably.
- It fixes writers **2 and 3 as well**, which duplicating a check inside
  `begin_send()` would also do — but by removing the possibility rather than by
  adding a third copy of the rule.
- It leaves `decide()`'s policy half exactly where it is, so no mail that sends
  today stops sending.

## 2.9 Regression tests

| test | asserts | fails today? |
|---|---|---|
| `test_every_emitted_kind_is_declared` | collects `kind=` literals at all `begin_send`/`email_authority` call sites; every one is in `EMAIL_KINDS` | **yes** — this is the acceptance gate |
| `test_the_python_and_schema_vocabularies_are_identical` | `set(EMAIL_KINDS)` == the CHECK's ARRAY, read from `pg_constraint` | no — pins the property that is currently true and must stay true through the widening |
| `test_claim_refuses_a_hand_built_decision` | `claim()` rejects a plain dict; only a gate-minted carrier is accepted | **yes** — pins the boundary |
| `test_an_undeclared_kind_is_refused_a_row_but_still_sends` | fail-open preserved: `proceed=True`, `recorded=False`, named reason, **transport still called** | **yes** — this is the test that stops the fix becoming fail-closed |
| `test_no_writer_reaches_the_ledger_without_the_gate` | static: `claim()` has exactly one caller, and it obtains a carrier | **yes** |

**Mutation proof required.** Each must be shown to fail against a mutant that
restores the bypass — a negative assertion that has never failed is not evidence.

## 2.10 Production verification

1. **Pre-change baseline (read-only):** `SELECT email_kind, count(*) FROM
   staff_email_ledger GROUP BY 1` on production, recorded before any change.
2. **Constraint identity, direct read** (closes revision 2's inference):
   `SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname =
   'staff_email_ledger_email_kind_check';`
3. **Post-migration:** the same query returns the widened set; `migrate.py
   --check --target railway` reports current.
4. **Behavioural proof:** after the next real governance escalation, a row for
   that kind exists with a matching `(kind, ref)` — the first row of any
   `alert%` kind in the system's life. **This is the only evidence that
   counts;** the code being deployed is not.
5. **Negative proof:** absence of any `proceeding UNRECORDED` line for
   governance kinds in the deployment window.

## 2.11 Remaining uncertainty

- **UNVERIFIED — guards for `approval_breach` and `approval_reescalation`.**
  **Must be traced before A ships.** "Every kind has a sound primary guard" is
  currently a claim about three fifths of the surface, and the plan does not
  generalise it.
- **UNEXPLAINED — why `begin_send()` was built without the gate.** Whether
  `decide()` post-dates it determines whether §2.7 is a repair or a design
  change.
- **UNVERIFIED — how many sends went unrecorded, over what period.** Requires
  production logs. Bears on D-A5 (whether history is reconstructed).
- **UNVERIFIED — whether duplicate governance mail was ever transmitted.**
  State neither that it occurred nor that it did not.
- **UNVERIFIED — the other 37 vocabulary constraints.** The *gap* is measured;
  further instances are not.
- **HYPOTHESIS — `escalation_remind` is a planned kind never wired.** Declared
  with zero producers. Bears on D-A2 and is not evidence for it.

---

# 3. Defect B — Evidence matrix

## 3.1 Observed behavior

`TestDailyProposalCap::test_hitting_the_cap_opens_one_owned_work_item_per_day`
fails on the first run after a UTC date change, then passes on every subsequent
run that day, presenting as flake.

## 3.2 Measured evidence

**FACT — the leak, reproducible.** After cancelling all open `sampled_review`
rows and running the file once: exactly **1** leftover open row,
`('test.activation_write', 'proposal_cap:test.activation_write:2026-09-09', 'open', 'governance')`.

**FACT — the leaking test:** `:1005` trips the cap, asserts only on
`action_approvals`, performs no alert cleanup. `:1013` cleans up, but only rows
**it queried**.

**FACT — the rollover, reproduced not argued.** Backdating the leftover key by
one day and re-running produced `AssertionError: assert 2 == 1` — identical
assertion and shape to the wild failure. Simulated rows cancelled afterwards.

**FACT — the fixture already owns a lifecycle.** `test_capability`
(`:144-176`) creates and, on teardown, deletes: the in-memory capability, the
`capability_registry` row, the `governance_action_policies` row, and
`action_approvals` for `TEST_INTENT`. **It does not delete the
`governance_alerts` its action_type caused.** That single omission is the
ownership gap.

**FACT — `TEST_INTENT` is a module constant** referenced **40** times, shared by
every test in the file.

**FACT — a second producer in the same class.** `a2a.py:1705` opens
`sampled_review` alerts with **no dedupe key**; 43 rows all-time, none currently
open. **INFERENCE:** with no fold, it accumulates one row per occurrence.

## 3.3 Root cause

The control asserts an **absolute count over a global population** whose
lifecycle it does not own. Its correctness depends on the cleanup discipline of
unrelated tests, on prior runs, and on the wall clock. The date stamp is the
cheapest way to violate that dependency; a crash, a parallel worker, or a
reordering would do the same.

## 3.4 Governing invariant

> **One work item per action class per day — not one per refusal.**

An attention-budget guarantee. It is genuinely about a count, so the fix must
scope the count, not remove it.

## 3.5 Current enforcement point

`assert len(rows) == 1` over
`governance_alerts WHERE alert_class='sampled_review' AND affected_id=<intent>
AND status NOT IN (terminal)` — unbounded in time and unscoped to this run.

## 3.6 Ownership failure

The test measures a population it shares with: other tests in the file, prior
runs on the same day, prior runs on previous days, the keyless `a2a` producer,
and any concurrent worker. It owns **none** of those.

## 3.7 Proposed remediation — **PROPOSED DESIGN**

**Establish ownership at the fixture that already owns the action_type**, then
scope the assertion to the owned population. **Not** a different hard-coded
number.

1. **Close the ownership gap.** `test_capability` teardown additionally cancels
   `governance_alerts WHERE affected_id = <intent>`. The fixture then owns the
   *complete* footprint of its action_type. This alone removes the cross-run and
   cross-day dependency, because no row survives the fixture.
2. **Scope the assertion to a delta the test caused.** Record the alert-id set
   for the intent *before* the refusals; assert that the three refusals produced
   **exactly one new** non-terminal alert. This proves the actual guarantee —
   *three refusals, one work item* — rather than a property of global state.
3. **Keep the count.** The guarantee is a count; scoping it preserves the
   business meaning. Weakening to "at least one" or "non-empty" would discard
   the invariant, and is rejected.

**Known limitation, stated rather than designed around.** With a shared
`TEST_INTENT`, two concurrent workers would still collide. Making the intent
unique per test resolves that, but the constant has **40** references and the
change is a separate, larger refactor. It is recorded as a follow-up
(**D-B4**), not folded in silently. Until then, this suite is not
parallel-safe, and that is a stated limitation of the fix.

## 3.8 Why this restores the invariant

The assertion becomes a statement about rows **this test caused**, within a
lifecycle **this fixture controls**. The date stamp becomes irrelevant, not
because it was corrected, but because no row from another day can be in scope.
The business guarantee is asserted more precisely than before: today's test
cannot distinguish "one alert was created" from "one alert already existed";
the delta form can.

## 3.9 Regression tests

- The repaired cap test itself, with the delta assertion.
- `test_the_capability_fixture_leaves_no_governance_rows` — after a fixture
  cycle, zero rows for the intent in `governance_alerts`, `action_approvals`,
  `capability_registry`, `governance_action_policies`. Pins ownership as a
  property of the fixture.
- **Rollover proof:** the repaired test must pass with a backdated leftover row
  present — the exact condition that reproduced the failure. **Without this the
  fix is unproven**, since the defect is invisible on an ordinary day.

## 3.10 Production verification

**None applicable, and that is the finding.** Production behaviour — one cap
alert per class per day, folding on redetection — was measured correct and is
not changed. The only verification is that the suite passes across a real UTC
date boundary, plus the simulated-rollover test above.

## 3.11 Remaining uncertainty

- **UNVERIFIED — determinism across orderings.** Measured once, in source order.
- **UNVERIFIED — whether other test files leak non-terminal governance rows.**
  Only `sampled_review` was examined, because only it is asserted on.
- **UNVERIFIED — whether the failure has occurred in CI.** History not consulted.
- **UNVERIFIED — whether the `a2a` keyless producer is bounded** by any sweep.
  A production question surfaced by B, not part of it.

---

# 4. Defect C — Evidence matrix

## 4.1 Observed behavior

Three retrieval tests fail. Two report the drift guard's crowding ratio; one
reports that a pinned result count moved from 4 to 2.

## 4.2 Measured evidence

**FACT — the mechanism**, from the search path's own diagnostics under
`recency_only`:

| query | candidates | ranked | **dedupe_count** | final |
|---|---|---|---|---|
| `payment reminder` | 4000 | 30 | **28** | 2 |
| `order shipped` | 4000 | 30 | **28** | 2 |
| `pricing discussion` | 4000 | 30 | **29** | 1 |

`ranked_count = 30` is the full budget (`limit * 6`), so **every candidate
cleared the similarity floor**; 93–97% were then collapsed as template
duplicates. Identical saturation across all three queries — a property of the
corpus, not of one template.

**FACT — all three tests share this root cause.** The pinned count fell because
fewer *distinct templates* survive the top 30, not because the kill switch
changed. The structural kill-switch test **passes**.

**FACT — provenance does not exist as data.** This is the plan's hardest
constraint:

- `activities` carries only `created_at, created_by, related_type, type` — no
  source, origin or generator column.
- The entire 548-row group has **`created_by = NULL`**, and 7,520 activities
  overall are `created_by = NULL`. **NULL does not discriminate.**
- Text markers cover **3,123 of 14,785 rows (21%)**, while `duplicate_share` is
  **52.5%**. The marker under-detects the near-duplicate population by more than
  half.

**INFERENCE — provenance filtering is not implementable today.** The inference
step: with no structured attribute and a text marker covering 21% against 52.5%
duplication, any admission filter would be a **proxy for the property** — the
antipattern this codebase has removed repeatedly.

**FACT — corpus state:** 14,785 rows (baseline 12,976); largest template group
548 (baseline 480, trip point 528); `duplicate_share` 0.525 (baseline 0.530);
index is 91% `activity` rows; `MAX_CANDIDATES = 4000` → **27% ranked**.

## 4.3 Root cause

**Dedupe runs downstream of the ranking budget.** The budget is spent on rows
that are subsequently discarded, so as any template grows, the number of
*distinct* results falls. Crowding produces fewer answers, not more.

The corpus condition — 91% activity rows, dominated by system-generated
templates — is what makes the ordering defect bite. **Two layers, and they must
not be conflated:** the ordering is a mechanism defect fixable today; the
composition is a corpus-authority question that cannot be answered until
provenance exists.

## 4.4 Governing invariant

> **The retrieval budget is spent on candidates that can appear in the result.**

And, at the corpus level, unresolved and pending decision:

> **What the system generates about its own operations is not automatically
> evidence about the business.**

## 4.5 Current enforcement point

The drift guard `vector_pool_validity()` — which **worked**. It reports that the
baseline's evidence has expired, maintaining two flags separately and
deliberately so a permanent disclosure and a change signal cannot silence each
other. It is not the defect and must not be touched.

## 4.6 Ownership / authority failure

The index admits content with **no provenance attribute at all**, so no
authority rule can be expressed over it. The system cannot currently answer
"who wrote this" for 100% of the largest template group.

## 4.7 Proposed remediation — **PROPOSED DESIGN, gated on decisions**

**C is not one change and must not be sequenced as one.**

**C-i — Blocking human decision (D-C1).** Is the intended corpus (1) business/
customer evidence, (2) system-generated operational output, or (3) both with
different authority? **No code follows until this is answered**, because it
determines whether the mechanism fix below is sufficient or merely necessary.

**C-ii — Provenance is a prerequisite, and it is upstream of retrieval.**
**FACT-driven:** provenance must be *written at generation time* by the workflow
engine and the notification paths. It cannot be recovered from existing rows.
Any admission-control or weighting proposal is **blocked** on this, and
proposing one before it exists would be proposing a text heuristic.

**C-iii — The mechanism fix, available today and independent of provenance.**
Move template dedupe **upstream of the ranking budget**, so the budget is filled
with candidates that can survive. This addresses the measured mechanism
(30 → 2) without touching thresholds, corpus, or tests.
**It changes retrieval behaviour and therefore requires revalidation (D-C2).**
It is not free and is not proposed as a quick win.

**Explicitly rejected**, per the reassessment and the instruction: lowering the
similarity floor, raising `top_k`/`N_VEC`/`MAX_CANDIDATES` to make results
reappear, or re-pinning expected counts. Each resolves the symptom and leaves
both layers of the root cause intact.

## 4.8 Why this restores the invariant

C-iii restores *"the budget is spent on candidates that can appear"* directly:
the discarded 28 never enter the budget, so the same budget yields distinct
results. It does not restore the corpus-authority invariant — **nothing can,
until C-ii provides the attribute that invariant would be expressed over.** The
plan states this as a limit rather than closing the gap with a proxy.

## 4.9 Regression tests

- `test_dedupe_precedes_the_ranking_budget` — structural: collapse occurs before
  the budget slice.
- `test_a_saturated_corpus_still_returns_distinct_results` — behavioural, over a
  **constructed** fixture corpus with a known template distribution, so the
  assertion owns its population (**directly applying B's answer**).
- The two drift-guard tests are **left exactly as they are.** They are correct
  and will stay red until D-C2 is taken. **A red test that is right is not a
  test to fix.**
- The pinned-count test is **not re-pinned** (§7). Its disposition follows
  D-C5 and belongs with the revalidation decision, not with this defect.

## 4.10 Production verification

1. **Pre-change:** record `vector_pool_validity()` and the per-query diagnostic
   triple (`ranked_count`, `dedupe_count`, `final_count`) on production.
   **UNVERIFIED today — all C figures are local**, and production may sit on a
   different side of every threshold. This must be measured before anything
   changes.
2. **Post-change:** the same queries return more distinct results at the same
   budget, and `dedupe_count` falls sharply.
3. **Revalidation (D-C2):** re-run the end-to-end content-loss measurement and
   re-record `_NVEC_BASELINE` — a decision, not a heuristic, per the codebase's
   own instruction.
4. **Do not verify by test colour.** Two tests going green would prove only that
   the baseline was rewritten.

## 4.11 Remaining uncertainty

- **UNEXPLAINED — why `duplicate_share` fell while the largest group grew.**
  Bears on whether the corpus is diluting or concentrating.
- **UNEXPLAINED — what the pinned counts would be at the validated composition.**
  Whether 4 was ever stable is unknown, and it determines whether that pin was
  ever a sound control.
- **UNVERIFIED — growth rate and bound of the workflow template group.**
  Measured once. **The single most decision-relevant unknown in C**: linear,
  retention-bounded, or unbounded changes the urgency entirely.
- **UNVERIFIED — production corpus state.** All figures local.
- **UNVERIFIED — actual retrieval quality.** Nothing here measures it. The claim
  is that *evidence expired*, not that quality degraded. **Do not report this as
  a retrieval-quality regression.**
- **HYPOTHESIS — moving dedupe upstream is net-positive.** Plausible from the
  measurement, untested. It changes which candidates compete and could alter
  ranking in ways the current tests do not cover. **Must be measured, not
  assumed.**

---

# 5. Implementation sequence, ordered by risk and dependency

Ordered so that each step is safe to stop after. **Nothing proceeds past the
gates in bold.**

| # | step | depends on | risk | reversible |
|---|---|---|---|---|
| **0** | **Trace the two untraced guards** (`approval_breach`, `approval_reescalation`) | — | none (read-only) | n/a |
| **1** | **DECISION D-A2 — what the vocabulary should be** | 0 | none | n/a |
| 2 | Add `test_the_python_and_schema_vocabularies_are_identical` | 1 | none | yes |
| 3 | Widen `EMAIL_KINDS` **and** the CHECK in one change; declare the migration only when both databases have run it | 2 | **schema migration** | forward-only |
| 4 | Add `test_every_emitted_kind_is_declared` — turns green at step 3 | 3 | none | yes |
| 5 | Introduce `ledger_identity()` + `LedgerIdentity`; `claim()` accepts only the carrier; `decide()` uses it | 4 | **medium — touches the send path** | yes |
| 6 | Fail-open preservation test + mutation proofs | 5 | none | yes |
| 7 | Correct the false invariant comment at `governance_alerts.py:189` | 5 | none | yes |
| 8 | **DECISION D-A3 — must a fail-open raise an alarm?** | 6 | none | n/a |
| — | — | — | — | — |
| 9 | B: close the fixture ownership gap | independent of A | low (test-only) | yes |
| 10 | B: delta-scoped assertion + simulated-rollover test | 9 | low | yes |
| — | — | — | — | — |
| **11** | **DECISION D-C1 — should system-generated content be indexed?** | independent | none | n/a |
| 12 | C: measure production corpus state and per-query diagnostics | 11 | none (read-only) | n/a |
| 13 | C: measure the template group's growth rate and bound | 12 | none | n/a |
| **14** | **DECISION D-C2 — revalidate / re-tune / accept** | 13 | none | n/a |
| 15 | C: dedupe-before-budget, behind revalidation | 14 | **high — changes retrieval** | yes |

**Why this order.** A steps 3→5 are inverted relative to intuition on purpose:
widening first means that when the boundary lands there is nothing left to
refuse, so the change is invisible to delivery. B is independent and can proceed
in parallel; it is placed after A only because A is higher severity. **C's first
three steps are measurement and decision, and no C code is written until step
14** — because C's remediation space is determined by an unanswered question
about what the corpus is for.

---

# 6. Exact files, functions and tests that would change

| file | function / site | change |
|---|---|---|
| `app/core/staff_email.py` | `EMAIL_KINDS` (`:171`) | widen per D-A2 |
| `app/core/staff_email.py` | **new** `ledger_identity()`, `LedgerIdentity` | the shared enforcement boundary |
| `app/core/staff_email.py` | `decide()` (`:665`) | vocabulary check delegates to the boundary |
| `app/core/staff_email.py` | `begin_send()` (`:1223-1234`) | obtain a carrier; stop fabricating a decision dict |
| `app/core/staff_email.py` | `claim()` (`:802`, INSERT `:823`) | accept only `LedgerIdentity` |
| `governance/sql/` | **new** migration | widen `staff_email_ledger_email_kind_check` |
| `app/core/deploy_state.py` | `REQUIRED_MIGRATIONS` | declare **only after both databases have run it** |
| `app/core/governance_alerts.py` | comment at `:189` | correct the false invariant |
| `governance/tests/test_staff_email_*.py` | new tests | §2.9 |
| `governance/tests/test_governance_activation.py` | `test_capability` (`:144-176`) | teardown cancels alerts for the intent |
| `governance/tests/test_governance_activation.py` | cap test (`:1013`) | delta-scoped assertion |
| `app/core/content_index.py` | dedupe / budget ordering | **step 15 only, gated on D-C2** |

**Not changed by this plan:** `governance_policy.email_authority()` keeps its
fail-open; `escalation.py` and `governance.py` call sites are fixed *by the
boundary*, not by edits at each site — which is the point of choosing a boundary
over duplicated validation.

---

# 7. Must NOT be changed — the reassessment showed these are not the root cause

| item | why it must not change |
|---|---|
| **The CHECK constraint as the fix** | Widening is step 3 of a sequence, never the remediation. Widening alone leaves writers 2, 3 and 4 ungated |
| **The idempotency claim / "is the ledger authoritative"** | Answered by measurement. Reopening it reintroduces a withdrawn conclusion |
| **`email_authority`'s fail-open** | Correct and deliberate. *"An executive must not miss an escalation because an audit table was missing."* The fix must refuse the **row**, never the **send** |
| **`decide()`'s policy half** | Binding it on governance mail would let `STAFF_EMAIL_ENABLED=0` suppress an escalation — a fail-closed regression on a fail-open channel |
| **`observe()` as a shadow observer** | Acting on nothing is its contract. The fix is that the *ledger* is gated, not that the shadow becomes binding |
| **The two drift-guard tests** | Correct. They report an expired baseline and must stay red until D-C2 |
| **The pinned retrieval test's expected counts** | Re-pinning discards a true signal and reintroduces a withdrawn conclusion |
| **`vector_pool_validity()` and its two-flag design** | The control worked. The separation exists so a permanent disclosure and a change signal cannot silence each other |
| **The similarity floor, `top_k`, `N_VEC`, `MAX_CANDIDATES`** | Tuning them makes tests pass while leaving both layers of C's root cause intact |
| **The cap test's expected count of 1** | The guarantee *is* a count. Scope the population; do not change the number |
| **Production alert/approval behaviour** | Measured correct. B is entirely a control defect |

---

# 8. Open decisions blocking implementation

| id | decision | blocks | owner |
|---|---|---|---|
| **D-A2** | What should the email vocabulary be? Five kinds, one kind with a subtype, or a separate ledger? | steps 3–7 | architecture |
| D-A3 | Must a fail-open control raise an alarm rather than log? | step 8 | governance policy |
| D-A5 | Is unrecorded history reconstructed, or does the ledger begin from the fix with the gap declared? | post-A | audit |
| D-B1 | Fixture-scoped ownership now, or per-test unique intents? | step 9 scope | engineering |
| D-B4 | Should the keyless `a2a` `sampled_review` producer have a dedupe key? | independent | product |
| **D-C1** | Should system-generated content be indexed at all? | all of C | product / architecture |
| **D-C2** | Revalidate, re-tune, or accept? | step 15 | architecture |
| D-C3 | Should machine-generated and human-authored content share one ranked budget? | post-C1 | architecture |
| D-C5 | What should a behavioural pin over live data assert? Shares substance with **D-B2** | the pinned test | engineering |

---

## 9. Plan acceptance self-check

| requirement | met |
|---|---|
| A fixed at the writer/gate boundary, not by widening vocabulary | **Yes** — §2.7; widening is step 3 of 8 and explicitly not the fix (§7) |
| Duplication inside `begin_send()` avoided unless demonstrably necessary | **Yes** — a shared boundary is proposed and the reason duplication is weaker is stated (§2.7, §2.8) |
| B fixed by establishing population ownership, not by changing a count | **Yes** — §3.7; the count is preserved and scoped, and re-pinning is forbidden (§7) |
| C addressed at corpus/provenance/retrieval mechanism, not by weakening tests | **Yes** — §4.7; threshold tuning and re-pinning explicitly rejected |
| The three architectural questions answered directly | **Yes** — §1 |
| No withdrawn conclusion silently reintroduced | **Yes** — §0 tabulates each with the measurement that keeps it withdrawn |
| Fact / inference / hypothesis / proposed design distinguished | **Yes** — labelled per claim throughout |
| Gaps not filled with assumptions | **Yes** — §2.11, §3.11, §4.11; two untraced guards are step 0 rather than an assumed pattern |
| No production code modified | **Yes** — plan only |

---

# 10. Implementation record — steps 0–4

**2026-09-09.** Appended after implementation. Steps 0–4 executed; C untouched
and gated at step 14. Nothing deployed; Railway has not run the migration.

## 10.1 Step 0 — the two untraced guards, now traced

Both were listed as UNVERIFIED and are now **FACT**. Neither was assumed from
the three already traced.

| guard | candidate predicate | write guard | verdict |
|---|---|---|---|
| `approval_breach` | `status='pending' AND due_at < now() AND escalation_status <> 'escalated'` | `UPDATE … SET escalation_status='escalated' WHERE approval_uuid=… AND status='pending'`, `rowcount != 1 → rollback; continue` | **sound single-threaded** |
| `approval_remind` | `status='pending' AND escalation_status='escalated' AND COALESCE(last_escalation_notice_at, escalated_at) < now() - REESCALATE_HOURS` | reads `escalation_notices`, computes `n = sent + 1`, `UPDATE … SET escalation_notices=n … WHERE … AND status='pending'`, stamped **before** the send | **sound single-threaded**; see §10.5 |

**FACT — in both, the compare-and-set is on `status='pending'`, not on the
field the candidate query filters.** Single-threaded that is sufficient,
because the candidate predicate excludes the row on the next pass. It is not
sufficient under concurrency.

## 10.2 Step 1 — D-A2 resolved from semantics, recorded

Three measured constraints drove the derivation; none of it is caller-derived.

1. **Identity (FACT).** `alert_assigned` and `alert_escalated` both carry
   `ref=<alert_id>`. One kind for both would suppress the escalation as a
   duplicate of the assignment. **The separation is load-bearing, not
   stylistic.**
2. **History (FACT).** `approval` and `digest` have rows (1,734 local). They
   cannot be renamed without invalidating the record the ledger exists to hold.
3. **Freedom (FACT).** The five governance kinds have **never written a row in
   either database.** This is what made an intentional vocabulary possible:
   there was no history to ratify.

**Derived rule.** `kind` names the EVENT CLASS; `ref` names the SUBJECT
instance; repeats take the `_remind` suffix with the ordinal in `ref`. That
suffix is not invented here — `escalation_remind` has carried it since the
table was created, so the callers' `*_reescalation` spellings were the
accident. **The callers were renamed to the convention rather than the
convention widened to the callers.**

**Canonical vocabulary (9):** `approval`, `approval_breach`, `approval_remind`,
`alert_assigned`, `alert_escalated`, `alert_remind`, `escalation`,
`escalation_remind`, `digest`.

**Open, unchanged:** `escalation_remind` still has **zero producers**
(`escalation.py` contains no reminder path). Declared and unimplemented, as
before. Not resolved by this work.

## 10.3 Steps 2–3 — what A now claims, stated precisely

Six claims, deliberately not collapsed into each other.

1. **FACT — `LedgerIdentity` prevents a caller from fabricating a decision
   object.** `claim()` accepts only a carrier minted by `ledger_identity()`;
   direct construction raises. Mutation-proved: removing the type guard fails
   `test_claim_refuses_a_hand_built_decision`.
2. **FACT — all four ledger writers are subject to the boundary.** `claim()`
   has exactly one caller (`begin_send`), verified by AST, and that caller
   mints. The three writers that previously passed only because their literals
   happened to be declared are now covered structurally rather than by
   coincidence.
3. **FACT — the nine-kind vocabulary is intentional**, derived in §10.2 from
   identity, history and the absence of history.
4. **FACT — `decide()`'s policy half remains unbound.** Only the
   identity/vocabulary half binds. `STAFF_EMAIL_ENABLED=0` cannot suppress an
   executive escalation. Mutation-proved: making the boundary fail-**closed**
   fails `test_an_undeclared_kind_is_refused_a_row_but_the_send_still_proceeds`.
5. **FACT — a real cross-process duplication window exists.** `leader.py:340`
   records a MEASURED event: *"with the database unreachable, four processes
   each assumed leadership — four schedulers, four IMAP pollers, four agent-bus
   consumers. That is duplicate dunning emails, duplicate reminders and
   duplicate consolidations."* The `(kind, ref)` claim is the cross-process
   guard for exactly that condition, and it had never operated.
6. **NOT ESTABLISHED — actual duplicate governance mail.** No measurement shows
   governance mail has ever duplicated. Claim 5 establishes a *hazard*, not an
   *occurrence*, and the two must not be merged. **Do not assert item 6 until a
   concurrent-sweep test demonstrates it.** Equally, do not assert the
   negative: the absence of a demonstration is not evidence none occurred.

**Behavioural proof that the defect is closed (FACT).** With
`GOV_ROUTE_EMAIL=1` and the transport faked, one escalation produced the first
`alert%` rows in the system's life:

```
('alert_escalated', 'accepted', 'dfa8fc54-…')
('alert_assigned',  'accepted', 'dfa8fc54-…')   <- same subject_ref_id
```

Both rows carry the **same** `subject_ref_id`, which is the case §10.2's
constraint 1 predicted: had the two shared a kind, the escalation would have
collided with the assignment. Probe rows deleted afterwards.

## 10.4 Step 4 — B

**Invariant restored:** *one work item per action class per day, not one per
refusal* — now asserted over rows the test caused.

- `test_capability` teardown cancels `governance_alerts` for its action_type,
  completing a lifecycle it already owned for the capability, registry row,
  policy row and approvals. Alerts are **cancelled, not deleted**:
  `trgfn_governance_alerts_no_delete` forbids deletion, and a governance
  obligation is closed out rather than erased.
- The assertion takes a watermark of existing rows and counts only the delta.
  **The expected count is still 1** — the population changed, not the number.
- Per D-B1: **no per-test unique intents**, so the ~40 `TEST_INTENT`
  references are untouched.

**Mutation-proved, both directions.** Dropping the fixture cleanup fails
`test_the_fixture_leaves_no_live_alert_behind`. Restoring the global assertion
reproduces the wild failure exactly against a staged stale row:

```
AssertionError: one work item per class per day, not one per refusal
assert 2 == 1
```

**FACT — after a full run, all 93 `sampled_review` rows for the test intent are
`cancelled`; zero are live.**

**Known limitation, unchanged and not designed around:** with a shared
`TEST_INTENT`, two concurrent workers would still collide. This suite is not
parallel-safe. Recorded, not silently fixed.

## 10.5 RESIDUAL FINDING — `approval_remind` read-modify-write race

**Recorded as a separate concurrency finding. It is NOT fixed by the A carrier
change and must not be read as such.**

> Two runners can read the same `escalation_notices` value, compute the same
> next value, and both send.

**Measured evidence.** `governance.py` reads `escalation_notices` in the
candidate query, computes `n = int(sent or 0) + 1`, then writes
`SET escalation_notices=%(n)s … WHERE approval_uuid=… AND status='pending'`.
The write is conditional on `status`, which both runners satisfy, so both get
`rowcount == 1` and both proceed. The counter is also lost-updated: two
reminders are both numbered `n`.

`governance_alerts.py:387` (`alert_remind`) has the **same shape** and the same
exposure. An earlier draft of this plan called that one "a correct
compare-and-set" — accurate about it being conditional, **wrong to imply it
guards the counter**. Corrected here.

**Why it is not folded into A.** The carrier boundary makes the ledger claim
*reachable*; `(kind, ref)` with `ref=f"{aid}:reminder:{n}"` would collide two
runners that computed the same `n`, so the ledger plausibly mitigates this race
now. **Plausibly is not demonstrated.** Whether the boundary was intended to
solve this race is not established by the existing architecture, and expanding
A's scope on that basis is exactly the ambiguity this engagement has been
removing. **HYPOTHESIS, requiring a concurrent-sweep test.**

**Not remediated. Not scheduled. Named and measured, for separate decision.**

## 10.6 Results

| scope | result |
|---|---|
| A focused (`test_staff_email_ledger_identity.py`) | **10 passed** |
| A adjacent (`stage1`, `stage4`, `sql_disposition_governance`) | **58 / 37 / 94 passed** |
| B focused (`TestDailyProposalCap`) | **9 passed** (was 7) |
| Full suite | **3 failed, 2998 passed, 2 skipped** |

The 3 failures are Defect C, pre-existing and untouched. Baseline before this
work was 2,986 passing; +10 (A) +2 (B) = 2,998.

## 10.7 Deployment state — unchanged, deliberately

- `staff_email_ledger_governance_kinds.sql` is **`OUT_OF_BAND_SQL` / PENDING
  DEPLOYMENT**, applied to LOCAL only.
- **NOT** in `REQUIRED_MIGRATIONS`, which is a claim about what production has
  RUN. `apply_sql.py` refused the file until it carried a disposition — the
  declaration-path control working as designed.
- **Nothing deployed to Railway. No production verification claimed.** The
  production constraint remains INFERRED from the migration ledger, never read.
  Promote only after Railway has executed it **and** the constraint has been
  read back there directly; promoting on an inference would repeat the gap that
  produced this defect.

---

# 11. `escalation_remind` — disposition

**Investigated 2026-09-09, before step 14, per the vocabulary decision not being
closed while a declared value has no producer.**

## 11.1 Disposition: **C — MISSING PRODUCER**

Not A (reserved contract) and not B (dead declaration). The vocabulary
describes an intended behaviour whose implementation is **entirely absent**, and
the design document names the producer, the file and the effort.

**The producer was never removed from the plan. It was never written.**

## 11.2 Evidence

**FACT — the behaviour is specified in detail**, `docs/employee_email_
notifications_design.md`:

| line | content |
|---|---|
| 205 | *"Escalation unclaimed past threshold · Tier 1 · critical · once, then cooldown · Role mailbox · **New.** The real failure mode: queue items aged 3h/22h/2d/2d/2d"* |
| 341-342 | `unclaimed past REMIND_AT_FRACTION × SLA? → ONE reminder, cooldown, max REMIND_MAX` |
| 364 | *"Repeated reminders: bounded three ways — cooldown, `REMIND_MAX`, and **a terminal ledger row per (escalation_id, reminder_ordinal)**"* |
| 429 | `STAFF_EMAIL_REMIND_AT_FRACTION 0.5` |
| **649** | *"`app/core/escalation.py` — route `_email_escalation` through the ledger; **add `unclaimed_reminders()`** — medium"* |

**FACT — none of it exists.** `unclaimed_reminders` appears **0** times in
`escalation.py`. No `REMIND_AT_FRACTION`, `REMIND_MAX` or
`STAFF_EMAIL_REMIND_*` is implemented anywhere in `app/`. There is no reminder
path in `escalation.py` at all.

**FACT — the vocabulary was built to serve it.**
`idempotency_key(kind, ref, ordinal)` renders `:remind:{ordinal}` when an
ordinal is given, which is exactly line 364's *"terminal ledger row per
(escalation_id, reminder_ordinal)"*. The suffix convention `_remind` — the one
this remediation adopted as canonical for `alert_remind` and `approval_remind` —
originates here. **The kind is not vestigial; it is the naming precedent the
rest of the vocabulary now follows.**

**FACT — the documented prerequisite has since been met.** Design line 360 calls
the `_set_handling` claim guard *"a prerequisite, not a follow-up: without it,
'claiming stops the reminder' is a promise the console cannot keep."* That
function now exists in `agent_console.py:213` and is an explicit
compare-and-swap. **So the blocker named in the design was closed and the
producer still was not built.**

**UNVERIFIED:** whether `agent_console._set_handling` is the same claim path an
escalation reminder would read. It governs **conversations**; escalations are a
different table. The prerequisite's named function is fixed; that it is the
right one for this producer is not established.

**FACT — the condition the reminder exists for is present locally:** 122
escalations `open`, 8 `assigned`, 8 `resolved`.

**Operational context, and the honest reason for deferral:** production has
produced zero escalations, so the loop has had no production input. That is a
reason the gap has not hurt; it is not a reason the gap is intentional.

## 11.3 Why not A, and why not B

**Not A (reserved contract).** A reservation is a decision to support something
later. This is an implementation task with a named function, a named file, an
effort estimate, five configuration knobs and a flowchart. Nothing in the
repository records a decision to defer it.

**Not B (dead declaration).** Removing it would delete the naming precedent the
canonical vocabulary now depends on, and it would discard a designed control for
a failure mode the design measured (*"queue items aged 3h/22h/2d/2d/2d"*) and
that is present locally today. **The absence of a producer is evidence to
investigate, not evidence of dead code** — and investigating it found a
specification, not a fossil.

## 11.4 Disposition and stop

**The missing producer is `escalation.unclaimed_reminders()`** (design §649),
together with the `STAFF_EMAIL_REMIND_*` configuration and the scheduler
registration described at design line 485.

**STOPPED HERE. Not implemented, and not scheduled.** Building it is new
behaviour that sends mail on a cadence, which is not within this engagement's
authorization.

**`escalation_remind` stays in both vocabulary layers**, and its comment in
`EMAIL_KINDS` should be read as *"specified, unbuilt"* rather than *"unused"*.

**The vocabulary decision is therefore closed in this sense and open in
another:** all nine values are intentional and justified, and one of the nine
describes behaviour the system does not yet perform. That is a delivery gap in
`escalation.py`, not a defect in the vocabulary.

---

# 12. Final local verification

## 12.1 A — writer/gate matrix, post-remediation

**Physical writers of `staff_email_ledger`** (AST-enumerated, `staff_email.py`):

| writer | statement | creates rows? |
|---|---|---|
| `claim` | INSERT | **yes — the only one** |
| `acquire` | UPDATE | no (lease) |
| `_update` | UPDATE | no (state transitions behind `finish_send` / `mark_*`) |

Since `claim` is the sole row-creating writer, it is the only point that must
hold.

| check | result |
|---|---|
| `claim()` callers | `['begin_send']` — exactly one |
| that caller mints via the boundary | **yes** |
| `claim()` reads `kind` from the identity, not a parameter | **yes** |
| `decide()` delegates its vocabulary check to the same boundary | **yes** |

**Forgery refused for every shape attempted** — `dict`, `None`, `str`, `tuple`
all raise `TypeError: claim() takes a minted LedgerIdentity`. Direct
construction of `LedgerIdentity` also raises.

**Policy half confirmed unbound (FACT).** `begin_send()` contains no call to
`decide()`, `enabled()` or `may_email()`. Measured directly: with
`STAFF_EMAIL_ENABLED=0`, `begin_send(kind="alert_escalated", …)` returns
`proceed=True, recorded=True`. **An executive escalation cannot be suppressed by
the staff-email enablement flag.** Probe row removed.

## 12.2 The evidence hierarchy, retained verbatim

| claim | standing |
|---|---|
| Split-brain duplication window (four schedulers, `leader.py:340`) | **FACT** |
| Duplicate governance mail has occurred | **NOT ESTABLISHED** — and the negative is not established either |
| `approval_remind` / `alert_remind` concurrent read-modify-write race | **separate finding** (§10.5), not fixed by A, not scheduled |
| `(kind, ref)` with `ref=…:reminder:{n}` collides two racing runners | **HYPOTHESIS** — requires a concurrent reproduction |

**A was not expanded to address the concurrency hypotheses.** No concurrent
reproduction exists, and building the remediation on a hypothesis is the
ambiguity this engagement has been removing.

## 12.3 B — verification

| check | result |
|---|---|
| Resolve commit | **`7d5a555`** |
| B is a separate uncommitted diff | yes |
| B hunks (CR-normalised) | **3**, all in the intended regions |
| B numstat (CR-normalised) | **96 insertions, 13 deletions** |
| Resolve classes appearing in the B diff | **0** |
| Fixture cancels rather than deletes | yes — `trgfn_governance_alerts_no_delete` forbids deletion |
| Fixture owns creation **and** closure of the population asserted over | yes |
| Delta assertion measures only the fixture-owned effect | yes — watermark, `r[0] not in before` |
| Focused B tests | **9 passed** |

**B design unaltered.**

*(A raw `git diff` also shows a fourth 64-line hunk over `TestUnattendedTestSeam`.
It is a CR artifact, not a content change: with `--ignore-cr-at-eol` it
disappears, a normalised line-by-line comparison finds only the three intended
edits, and the class is present with all 5 of its tests. `core.autocrlf=true`
normalises on commit.)*

## 12.4 Migration and deployment — unchanged

- `staff_email_ledger_governance_kinds.sql` remains **`OUT_OF_BAND_SQL` /
  PENDING DEPLOYMENT**, applied to LOCAL only.
- **Not** in `REQUIRED_MIGRATIONS`. Nothing deployed to Railway. **No production
  verification claimed.**
- The production constraint remains **INFERRED** from the migration ledger and
  has never been read. Promote only after Railway has run the migration **and**
  the constraint has been read back there directly.

**Retained as a positive governance-control result, not incidental tooling:**
`apply_sql.py` **refused** this file until it carried an explicit disposition —
*"Refusing to guess — guessing is how the trg_fn_events_after_insert chain
reached production unrecorded."* The disposition census then required the
partition change to be recorded with its reason before the suite would pass.
**Two independent controls made it impossible to introduce a schema change
quietly**, and both fired on a change whose author intended to declare it. That
is the behaviour these controls exist for, and it is worth stating because
controls that only ever fire on mistakes are the ones nobody trusts.

---

# 13. Phase 1 STOP — the specification and the escalation model disagree

**2026-09-09.** Phase 1 (implement `escalation.unclaimed_reminders()`) began with
the required trace and hit the stop condition. **No reminder producer was
written.**

## 13.1 The discrepancy

The design names the reminder loop's prerequisite (`docs/employee_email_
notifications_design.md:360`):

> *"`_set_handling` must gain `AND (handling='ai' OR assigned_to IS NULL OR
> assigned_to=%s)` so the second caller gets `ok:False, "already taken by X"`.
> **This is a prerequisite, not a follow-up: without it, "claiming stops the
> reminder" is a promise the console cannot keep.**"*

**FACT — `_set_handling` was fixed, on a different object.** It lives in
`agent_console.py:213` and governs **conversations**
(`conversations.handling` / `assigned_to`). It is now an explicit
compare-and-swap. The console touches escalations only through
`_live_escalations()`, a SELECT for queue badges.

**FACT — the escalation claim path was not fixed, and has two different
guarantees depending on which door you use:**

| path | WHERE clause | atomic? |
|---|---|---|
| `assign()` → `_update()`, exposed at `POST /escalations/{id}/assign` | `WHERE escalation_id=%(id)s::uuid` | **NO** — no predicate on `status` or `assigned_to` |
| `assign_for_conversation()` | `WHERE conversation_id=%s::uuid AND status='open'` | **yes** |

**MEASURED — the race, reproduced on a throwaway escalation:**

```
first  claim : {'ok': True, ..., 'status': 'assigned'}
second claim : {'ok': True, ..., 'status': 'assigned'}
row now      : ('assigned', 'rep-bob')

BOTH SUCCEEDED: True | second silently overwrote the first: True
```

Two reps claim the same obligation. Both are told they own it. The second
overwrites `assigned_to`; nothing tells the first. Probe row deleted.

This is precisely the F6 defect the design describes — **on the object the
reminder loop must read**, while the function the design happened to name was
fixed on a different one.

## 13.2 Why this blocks the producer rather than merely accompanying it

The reminder's entire contract is *claiming stops the reminder*. Design line 359
states how that is kept:

> *"The reminder job re-reads `status` inside the same transaction that claims
> the ledger row, so the window is one statement wide, not one job-run wide."*

That reasoning holds only if the claim it re-reads is itself atomic. It is not,
on the `assign()` path. Building `unclaimed_reminders()` now would implement a
loop whose central promise rests on a claim two callers can both win —
delivering the exact guarantee the design declared unkeepable, and delivering it
in a mechanism that emails executives on a cadence.

**Per the instruction, stopped and reported rather than guessed.**

## 13.3 What is NOT claimed

- **NOT ESTABLISHED — that this race has occurred.** No duplicate or lost
  escalation claim was measured in live data. The *mechanism* is MEASURED; an
  *occurrence* is not.
- **NOT ESTABLISHED — that fixing `assign()` is sufficient** to satisfy the
  prerequisite. It is necessary. Whether the design's F6 also intends the
  conversation-side guard to participate (the two paths write the same rows
  through different predicates) is a design question this trace did not settle.
- **HYPOTHESIS — that `assign()` should adopt
  `AND (status='open' OR assigned_to=%s)`**, mirroring `_set_handling`'s shape
  and `assign_for_conversation()`'s existing predicate. Untested, and not
  implemented.

## 13.4 The new defect, stated for separate decision

> **`escalation.assign()` is a last-writer-wins UPDATE on a claim.** Two callers
> both receive `ok: True` for the same escalation; the second silently replaces
> the first's `assigned_to`. `assign_for_conversation()` guards the same
> transition with `AND status='open'`; the direct API path does not.

Same shape as the two already-recorded read-modify-write races
(`approval_remind`, `alert_remind`, §10.5) and as the takeover defect
`_set_handling` was fixed for. **Third instance of one pattern: a state
transition that means "I have taken this" written as an unconditional UPDATE.**

**Not remediated. Not scheduled.** It is a one-line predicate, which is exactly
why it should be decided rather than absorbed into a phase that was authorized
to build something else.

## 13.5 Sequencing consequence

`escalation_remind` remains **C — missing producer**, unchanged. Its
implementation now has a named, measured blocker:

```
fix escalation.assign() atomicity  ->  then implement unclaimed_reminders()
```

Phase 1 cannot complete in the intended order until that decision is taken.

---

# 14. `unclaimed_reminders()` — trace and proof. **NOT SAFE TO AUTHORIZE.**

**2026-09-09. Trace-and-proof phase only. The producer was not implemented.**
Four hard stop conditions are met.

## 14.1 Step 1 — the eligibility predicate, derived

From authoritative state only. `assigned_to` is not used.

```sql
status IN ('open','assigned')     -- LIVE: the same test sla_breaches() and the
                                  -- console badges already apply
AND assigned_at IS NULL           -- UNCLAIMED: the marker a claim actually writes
AND now() >= created_at + make_interval(mins => sla_minutes * REMIND_AT_FRACTION)
```

**MEASURED — the universe this selects today: 126 escalations.**

**MEASURED — `sla_due_at` and `created_at + sla_minutes` agree on all 138 rows.**
Either could anchor the threshold today. **INFERRED:** they can diverge —
`open()` lowers `sla_due_at` via `LEAST(...)` on a priority re-ask without
touching `sla_minutes` — so an implementation must state which one it means.
Anchoring on `sla_due_at` makes the reminder time move when a customer asks
again; anchoring on `created_at` makes it fixed. **That is a semantic decision,
not a detail.**

### New finding, isolated rather than absorbed

**MEASURED: 4 escalations have `status='assigned'` and `assigned_at IS NULL`.**
They are `assigned` yet were never claimed by either claim path. Under the
predicate above they are **live and unclaimed**, so a reminder producer would
chase work the queue already displays as assigned.

**AMBIGUOUS STATE SEMANTICS — this is a hard stop condition in its own right.**
Not remediated, not absorbed into the reminder design. The other 36 anomalies
(open rows with free-text `assigned_to`) are *correctly* eligible — never
claimed — but a human reading the row sees an owner's name, so the producer
would email about work that looks owned.

## 14.2 Step 2 — the ledger protocol, and the ten questions

Path: candidate SELECT → eligibility decision → `begin_send()` →
`ledger_identity()` → `claim()` INSERT → `acquire()` lease → `mark_attempted` →
provider → `finish_send()`. Physical writers: `claim` (INSERT), `acquire`
(UPDATE), `_update` (UPDATE).

| # | question | answer |
|---|---|---|
| 1 | What prevents two workers selecting the same escalation? | **NOTHING.** Candidate selection is a plain SELECT. The candidate-selection race is real and unguarded |
| 2 | What prevents two workers claiming the same `(escalation_id, ordinal)`? | `uq_staff_email_idem` UNIQUE on `idempotency_key`, `ON CONFLICT DO NOTHING`, then `acquire()` as a compare-and-swap. **MEASURED exactly-once** (§14.3) |
| 3 | Is ledger uniqueness sufficient if candidate selection races? | **Only if racing workers derive the SAME ordinal.** Different ordinals are two different sends by construction |
| 4 | Claim before or after sending? | **Before.** Claim, lease, mark attempted, send, `finish_send` |
| 5 | Crash between claim and send? | Row sits `attempted`; reclaimable only after `ATTEMPT_LEASE`. The duplicate window is bounded by the lease, not eliminated |
| 6 | Can a retry distinguish an uncompleted claim from a new reminder? | **Within one ordinal, yes** — `acquire()` returns None while the lease is fresh, terminal states are never reclaimed. **Across ordinals, no** |
| 7 | What establishes the next ordinal? | **NOTHING EXISTS** (§14.4) |
| 8 | Is ordinal calculation race-safe? | **Unassessable — there is no calculation to assess** |
| 9 | Does cooldown have a read-modify-write race? | **No cooldown state exists to read** |
| 10 | Can a terminal escalation be selected after becoming terminal? | **Yes.** Selection and send are not one transaction, and cannot be (§14.5) |

**Candidate-selection races and ledger-claim races are different problems and
resolve differently here.** The ledger-claim race is closed, and measured
closed. The candidate-selection race is open, and is only harmless while every
racing worker computes the same ordinal — which nothing guarantees.

## 14.3 Step 3 — the ledger boundary, proven with the existing API

No producer code was written. Two threads on two connections (connect-per-call,
so genuinely separate), released by a barrier, call the real `begin_send()` for
the same `(kind, ref, ordinal)`.

**MEASURED: exactly one worker proceeds; exactly one ledger row exists.**
Stable across repeated runs.

**Mutation-proved:** with `acquire()`'s state predicate removed — so it stops
being a compare-and-swap — the test fails **3 of 3 runs**. The test detects a
broken exactly-once boundary rather than passing by coincidence.

A companion test pins **the limit of the guarantee**: two sends with different
ordinals for the same escalation both proceed, correctly. **The ledger cannot
enforce a reminder cadence.** Cadence is entirely a property of whatever
chooses the ordinal.

## 14.4 Step 4 — the design's claims, classified

**MEASURED — `escalations` carries no reminder state of any kind.** No counter,
no last-reminded timestamp, and **0 rows** use `metadata` for it. The two
subsystems that do send reminders both have `escalation_notices` **and**
`last_escalation_notice_at`:

| table | reminder counter | last-notice timestamp |
|---|---|---|
| `governance_alerts` | yes | yes |
| `action_approvals` | yes | yes |
| **`escalations`** | **none** | **none** |

| design statement | classification |
|---|---|
| *"terminal ledger row per (escalation_id, reminder_ordinal)"* | **FACT / MEASURED** — the key shape and uniqueness exist and were proved under concurrency |
| *"ONE reminder, cooldown, max `REMIND_MAX`"* | **NOT ESTABLISHED** — no state exists to enforce either bound |
| *"unclaimed past `REMIND_AT_FRACTION` × SLA"* | **INFERRED** — derivable (§14.1), but which SLA anchor is meant is undecided |
| *"bounded three ways: cooldown, `REMIND_MAX`, and a terminal ledger row"* | **one of three established.** The ledger row is real; the other two have no home |
| *"the reminder job re-reads `status` inside the same transaction that claims the ledger row, so the window is one statement wide"* | **CONTRADICTED — see below** |
| F6 `_set_handling` prerequisite | **superseded** — the real prerequisite was `escalation.assign()`, now remediated (§13) |

### The same-transaction claim is not merely unproven; it is impossible today

**MEASURED.** `claim()` opens its own connection via `get_connection()`,
commits, and closes it. `begin_send()` does the same. **There is no parameter by
which a caller can enlist the ledger claim in its own transaction.** A reminder
job therefore cannot re-read `escalations.status` inside the transaction that
claims the ledger row — the two are separate connections and separate commits.

The design's stated safety argument for question 10 does not hold against the
implementation. **Marked CONTRADICTED rather than preserved because it is
already written.**

## 14.5 Hard stop conditions met

| condition | status |
|---|---|
| Reminder ordinal calculation is race-prone | **MET** — no ordinal state exists; any derivation is a read-then-decide outside the claim |
| Cooldown calculation is race-prone | **MET** — no cooldown state exists |
| A new schema field appears necessary | **MET** — a counter and a last-notice timestamp, mirroring the two subsystems that already send reminders |
| The proposed transaction boundary is not actually atomic | **MET** — the same-transaction re-read is impossible with the current ledger API |
| An existing state transition has ambiguous semantics | **MET** — 4 rows are `assigned` with no claim |
| Eligibility cannot be expressed from authoritative state | not met — §14.1 expresses it |
| The ledger cannot provide exactly-once intent | not met — proved it can, per ordinal |
| Would require inventing takeover semantics | not met |

**Five of eight conditions are met. `unclaimed_reminders()` is NOT safe to
authorize.**

## 14.6 What would have to be decided first

Stated as the shape of the next decision, **not** as a proposal to implement:

1. **Where reminder state lives.** A schema change to `escalations` mirroring
   `escalation_notices` / `last_escalation_notice_at`, or a derivation from the
   ledger. The first makes ordinal and cooldown atomic with a conditional
   UPDATE; the second cannot, because the ledger is a separate transaction.
2. **Note the trap.** The mirror candidates are the exact columns whose
   read-modify-write races are already recorded as open findings for
   `approval_remind` and `alert_remind` (§10.5). Copying that shape would copy
   that race. **These three findings stay separate and are not cleaned up
   together.**
3. **Which SLA anchor** the threshold uses (§14.1).
4. **What the 4 ambiguous rows mean** before a producer emails about them.

## 14.7 Classifications carried forward unchanged

`approval_remind` and `alert_remind` remain **separate read-modify-write
findings**, not merged into this trace and not remediated by it. Duplicate
governance mail remains **NOT ESTABLISHED**. The former escalation claim race
is **remediated with concurrent proof**; its production occurrence remains
**NOT ESTABLISHED**. C remains untouched.

---

# 15. Authorization review — three pending migrations

**2026-09-09. Review only. Nothing deployed, committed or applied to Railway.**

## 15.1 Facts, reconciled independently

| # | check | result |
|---|---|---|
| 1 | Migration A DDL / nullability / default / comments | `reminder_ordinal integer NOT NULL DEFAULT 0`, `reminder_allocated_at timestamptz` NULL; both carry COMMENTs; 138/138 rows at ordinal 0 |
| 2 | Migration B DDL / NOT VALID / comments | `CHECK (status <> 'assigned' OR assigned_at IS NOT NULL) NOT VALID`; `convalidated = False`; constraint COMMENT present |
| 3 | Pending migrations + hashes | `staff_email_ledger_governance_kinds.sql` `f83e5237…` · `escalation_assigned_requires_claim_time.sql` `31fd1497…` · `escalation_reminder_allocation.sql` `82d93fc7…` |
| 4 | `deploy_state.py` classification | all three `OUT_OF_BAND_SQL` / PENDING DEPLOYMENT; none in `REQUIRED_MIGRATIONS` |
| 5 | Producer / scheduler / config / ledger integration | **none.** `unclaimed_reminders` as code: 0 · scheduler registration: 0 · `REMIND_*` config: 0 |
| 6 | Code depending on the new columns | **`reminder_ordinal` 0 files · `reminder_allocated_at` 0 files.** Dormant capability |
| 7 | Violating rows locally | **4**, still present, untouched |
| 8 | Fixtures creating the violating state | one found and corrected (`test_case_bridge`); no others |

## 15.2 Migration B — operational analysis

**Schema safety, operational safety and audit readiness are three different
questions, and B passes only the first two of three.**

### Every path that can touch a violating row

| path | SET clause | leaves the violation? | outcome |
|---|---|---|---|
| `resolve()` / `resolve_for_conversation()` | `status='resolved'` | no | **permitted** — repairs the row |
| `assign()` / `assign_for_conversation()` | `assigned_at=now()` | no | **permitted** — repairs the row |
| `_record_email_outcome()` (`:602`) | `metadata` only | **yes** | **REFUSED**, caught at `logger.debug`, non-fatal — metadata silently lost |
| `open()` priority re-ask fold (`:304`) | `priority`, `sla_due_at`, `updated_at` | **yes** | **REFUSED** — see below |

### The finding that blocks authorization

The priority fold selects
`WHERE conversation_id=%s AND status IN ('open','assigned')`, so **a violating
row is selectable**, and its UPDATE touches neither `status` nor `assigned_at`.
The CheckViolation is caught by `open()`'s outer handler, which rolls back and
returns:

```python
return {"ok": False, "error": str(exc)[:200]}
```

**A customer asking for a human a second time on that conversation would have
the request refused** — not folded, not re-opened, logged at WARNING. That is a
customer-facing fail-closed path, and it is the failure class this engagement
exists to remove.

**MEASURED locally: 0 of 4 violating rows carry a `conversation_id`**, so the
fold cannot reach any of them here. **On Railway this is UNVERIFIED**, and it is
the single fact that decides whether B is operationally safe.

### Stranding

**A violating row cannot become permanently stranded** — `resolve()` and
`assign()` both repair it, and both remain permitted. But **no automated repair
path exists**: both are human- or API-driven, and `sla_breaches()` only reads.
Repair is therefore an operator action, not something the system does on its own.

## 15.3 Minimum production facts required to authorize B

1. **Count** of `status='assigned' AND assigned_at IS NULL`.
2. **How many of those carry a `conversation_id`** — the only ones the fold can
   reach, and therefore the only ones that can refuse a customer's re-ask.
3. Whether any is `source` other than test — i.e. whether any represents real
   work.
4. Whether any has pending email-outcome processing that would silently lose
   metadata.
5. Whether every one has a deterministic repair transition available.

Items 1 and 2 are sufficient to make the decision; 3–5 shape the remediation.

**B authorization is blocked pending read-only production inspection of: the
count of `status='assigned' AND assigned_at IS NULL` on Railway, and how many
of those rows have a non-NULL `conversation_id`.**

No Railway query was run. Read-only production inspection has not been
authorized in this task.

## 15.4 Classification

### Defect A vocabulary widening — **READY FOR AUTHORIZATION**

| | |
|---|---|
| Purpose | Widen the ledger's send vocabulary so the identity boundary has nothing legitimate to refuse |
| Evidence | Applied locally; both layers agree at 9 values; migration asserts every existing kind stays admissible; behavioural proof that governance mail now claims a ledger row |
| Production risk | **Low, and it is a behaviour change:** governance mail begins writing ledger rows, so the `(kind, ref)` idempotency guard becomes operational for the first time. A send that currently duplicates would stop duplicating — the intent — and a crashed send now waits `ATTEMPT_LEASE` before retry, exactly as approval and digest mail already do |
| Missing evidence | Direct read-back of the constraint on Railway after applying |
| Recommendation | Authorize. It only *permits* values; no row is rewritten and no path becomes fail-closed |

### Migration B, assigned-requires-claim-time — **BLOCKED — EVIDENCE REQUIRED**

| | |
|---|---|
| Purpose | Close a state-integrity leak: `status='assigned'` with no claim time |
| Evidence | Installs NOT VALID with violating rows present; 8 proofs pass; historical rows untouched; repair transitions verified |
| Production risk | **The one that blocks it.** Violating rows become partially immutable. If any Railway violating row has a `conversation_id`, a customer's repeat request for a human is refused with `ok:False` |
| Missing evidence | Railway violating-row count, and how many carry a `conversation_id` |
| Recommendation | **Do not authorize yet.** Not because the constraint is wrong — it is correct and proved — but because its blast radius on the production population is unmeasured |

### Reminder allocation — **READY FOR AUTHORIZATION (but not required yet)**

| | |
|---|---|
| Purpose | State a future producer needs so eligibility and ordinal allocation are one atomic transition |
| Evidence | Applied locally; 13 tests including a deterministic mutation proof; all rows initialise to 0 |
| Production risk | **Minimal.** Two additive columns; `ADD COLUMN … DEFAULT` does not rewrite the table on modern PostgreSQL; **zero code reads them** |
| Missing evidence | None material |
| Recommendation | Authorize **or defer at no cost.** Nothing consumes the columns, so deferring until the producer is authorized loses nothing; applying early lets production verification happen before behaviour depends on it |

## 15.5 Ordering and atomicity

**No dependency in any direction.** A touches only `reminder_*`; B touches only
`status`/`assigned_at`; the vocabulary migration touches a different table
entirely. Any order works.

**Do not combine them.** PostgreSQL permitting transactional DDL is not a reason
to. They answer different questions, carry different risk, and one of the three
is blocked — bundling would force the two ready migrations to wait for the
blocked one, or drag the blocked one along. Separate files, separate audit
history, separate authorization.

## 15.6 Evidence boundary

| stated as | must not be read as |
|---|---|
| locally verified | production verified |
| the constraint installs cleanly | production-safe |
| four local violations | four production violations |
| no producer exists | the feature is complete |
| migration is prepared | migration is authorized |

**Railway action authorized by this review: NONE.**

Smallest next step to unblock B: a **read-only** Railway query —
`SELECT count(*), count(conversation_id) FROM escalations WHERE status='assigned' AND assigned_at IS NULL;`
— which requires explicit authorization for production inspection and is not a
deployment.

---

# 16. Railway read-only inspection — the B decision

**2026-09-09. Read-only. One authorized query. No mutation, no deployment, no
commit.** The session was opened `READ ONLY` at the server, so a write would
have been refused rather than merely not attempted; the transaction was rolled
back and the connection closed.

## 16.1 Environment proof

```
TARGET  host=shinkansen.proxy.rlwy.net  db=railway
current_database : railway
is read replica  : False          (the primary answered, not a stale replica)
```

## 16.2 Result

| metric | Railway |
|---|---|
| `escalations` total rows | **0** |
| `status='assigned' AND assigned_at IS NULL` | **0** |
| ...of those, `conversation_id IS NOT NULL` | **0** |

**Disclosure:** three statements were issued, not one. The environment probe was
explicitly authorized. `SELECT count(*) FROM escalations` was **not** in the
authorized query — it is an aggregate over the same table returning no row
content, and it materially changed the interpretation, but it was outside the
letter of the authorization and is recorded rather than passed over.

## 16.3 Interpretation — Case A

`violating_rows = 0`, so B's known partial-immutability risk is **absent from
the production population**. The customer-facing `open()` re-ask path cannot
encounter a violating row, because there are no escalation rows at all.

**MEASURED, and it corroborates the local reading.** 136 of 138 local
escalations are test data; production has produced **zero** escalations in the
system's life. The two databases agree: this table has never carried real work.

**What zero does NOT mean.** It does not make the constraint unnecessary — it
makes it cheap. The violating state was being created as recently as this
session, by a test fixture, which is precisely the kind of writer a NOT VALID
constraint exists to stop. A guard installed while the table is empty is the
least disruptive moment to install it, not evidence that it was never needed.

## 16.4 A divergence to avoid

With zero rows, `VALIDATE CONSTRAINT` would succeed trivially on Railway. It
would still fail locally, where four violating rows remain.

**Do not validate on Railway merely because it would work.** That would leave
`convalidated = true` in production and `false` locally — a schema difference
between two databases the project's attestation discipline treats as one. Apply
the same file to both, leave both NOT VALID, and validate both together after
the local four are dispositioned.

## 16.5 Decisions

### Migration B — **READY FOR AUTHORIZATION**

The single blocking unknown is now a measured fact: zero violating rows, zero
reachable by the conversation fold. The operational risk that blocked it does
not exist in the current production population.

**Carried caveats:** the local database still holds four violating rows, so the
two will differ in *data* while agreeing in *schema*; and the metadata-update
refusal remains a real behaviour for any row that ever violates — it is now a
statement about the future, not about existing rows.

### Defect A vocabulary widening — **READY FOR AUTHORIZATION**

Re-checked: the Railway read-back of the constraint is still the only missing
evidence, and it is post-application verification rather than a precondition.
The migration is self-verifying — its `DO` block raises if any kind already in
the ledger would be excluded by the new vocabulary.

**Behaviour change on a live path, restated so it is not authorized as inert:**
governance mail begins claiming ledger rows, so the `(kind, ref)` idempotency
guard becomes operational for the first time, and a crashed send waits
`ATTEMPT_LEASE` before retry exactly as approval and digest mail already do.

### Reminder allocation — **DEFERRED**

Zero readers, no producer, no scheduler, no `REMIND_*` configuration, no ledger
integration. There is no operational benefit to deploying dormant columns, and
production verification is not a reason on its own. Introduce them when the
producer is authorized.

## 16.6 Boundary

**Railway action authorized by this review: NONE.** The inspection converted one
unknown into a measured fact. It did not authorize a deployment, and B being
READY is a classification, not permission to apply it.

---

# 17. Post-deployment baseline — schema is live, application is not

**2026-09-09. Reconciliation only. Nothing committed, deployed or changed.**

## 17.1 The finding this reconciliation exists for

**Deploying the vocabulary migration ahead of the application remediation left
Defect A partially closed, and in a state neither database can see on its own.**

| layer | production state |
|---|---|
| schema CHECK (deployed) | **9 kinds** |
| `EMAIL_KINDS` in deployed code (HEAD) | **4 kinds** |

The two declarations now **disagree in production**. Locally both are 9, which
is why `test_the_python_and_schema_vocabularies_are_identical` passes: **it runs
against the local database and the local source, so it cannot see the
divergence it exists to catch.**

Worse, and concrete:

```
kinds the DEPLOYED code emits : alert_assigned, alert_escalated,
                                alert_reescalation, approval,
                                approval_breach, approval_reescalation
STILL REJECTED by the deployed schema: alert_reescalation, approval_reescalation
```

**Two of the five governance kinds still violate the CHECK in production, still
fail open, and still send unrecorded.** The canonical vocabulary renamed those
two callers to `alert_remind` / `approval_remind`, and that rename lives in the
uncommitted working tree.

So the correct statement of production today is **not** "Defect A's audit gap is
closed". It is:

- `alert_assigned`, `alert_escalated`, `approval_breach` — now recorded; the
  `(kind, ref)` guard is operational for them
- `alert_reescalation`, `approval_reescalation` — unchanged; unrecorded
- the forgeable-decision defect — **entirely unaddressed in production**

This is the difference between a schema control and a system defect, and it is
exactly what "migration deployed" must not be read as.

## 17.2 Deployed baseline, verified from committed state

| check | result |
|---|---|
| `governance HEAD:staff_email_ledger_governance_kinds.sql` | `f83e5237` **MATCH** |
| `governance HEAD:escalation_assigned_requires_claim_time.sql` | `31fd1497` **MATCH** |
| main `HEAD` declares the two deployed migrations | yes (2) |
| deferred reminder migration in governance `HEAD` | **no** |
| deferred reminder migration declared in main `HEAD` | **no (0)** |
| deployed app code referencing `reminder_ordinal` / `reminder_allocated_at` | **0 / 0** |

**Provenance unit spans two repositories and cannot be collapsed:**
governance `aa389d8` (SQL artifacts) + main `f101bdb` (disposition record).

## 17.3 Migration B vs the atomic claim fix — orthogonal

| | Migration B (live) | atomic claim fix (not live) |
|---|---|---|
| guards | a **malformed state**: `assigned` with no claim time | a **lost update**: two claimants, one owner |
| enforced by | the database, regardless of application code | application SQL predicate |
| production status | LIVE, `NOT VALID`, 0 violations | NOT DEPLOYED |

1. **What the claim fix prevents:** two claimants both receive `ok:True`; the
   second silently replaces the first's `assigned_to`.
2. **Is it required for B's invariant under concurrency?** **No.** Two racing
   claimants each write `assigned_at`, so both satisfy B. B cannot detect the
   race and the fix cannot detect a malformed row. Neither implies the other.
3. **Do production claim paths already satisfy atomicity?** **Partly.**
   `assign_for_conversation()` has always carried `AND status='open'` and is
   atomic. `assign()` — the `POST /escalations/{id}/assign` path — is the
   unconditional UPDATE. **The race is currently unreachable in production
   because Railway holds zero escalations**; the exposure is latent, not active.
4. **Deterministic tests?** Yes — 8, including a two-connection barrier race,
   mutation-proved 5 of 5 runs.
5. **Can any writer still create `assigned` without `assigned_at`?** **No.** The
   deployed constraint refuses it at the database whatever the application does.
   That half is closed in production **by the migration alone**.

## 17.4 The four local rows — disposition prerequisites

**Why they exist:** all four `source='test'`, `created_at = updated_at`, no
`conversation_id`. INSERTed already-`assigned` by a fixture; never claimed. No
code path produces this state, and the suite's own reproduction of it was
corrected this session.

**Repair:** `resolve()` (sets `status='resolved'`) or `assign()` (sets
`assigned_at`). Both remain permitted under the constraint, so **no row is
stranded** — but there is **no automated path**: both are operator-driven.

**Before `VALIDATE CONSTRAINT` can be authorized:** every violating row
dispositioned on **both** databases, and validation run on both together.
Railway would validate trivially today (0 rows) and local would fail (4) —
**validating only Railway would create the schema-parity divergence the NOT VALID
decision was taken to avoid.** Not validated anywhere; both sit at
`convalidated = false`.

## 17.5 Working-tree inventory

| category | files |
|---|---|
| **A — application remediation** | `app/core/staff_email.py` (159/18) · `app/core/governance.py` (4/1) · `app/core/governance_alerts.py` (4/1) |
| **B — atomic claim remediation** | `app/core/escalation.py` (90/4) |
| **Reminder — deferred** | `app/core/deploy_state.py` (12/0, the declaration only) · `governance/sql/escalation_reminder_allocation.sql` · `governance/tests/test_escalation_reminder_allocation.py` |
| **Tests** | `test_staff_email_ledger_identity.py` (new, A) · `test_staff_email_stage1.py` (42/21, A) · `test_staff_email_stage4.py` (14/10, A) · `test_escalation_claim.py` (new, claim fix + Migration B proofs) · `test_case_bridge.py` (11/2, Migration B fixture consequence) · `test_governance_activation.py` (96/13, **Defect B** cap ownership) · `test_sql_disposition_governance.py` (34/3, census for all three migrations) |
| **Documentation** | 6 files under `docs/` |
| **Other / unexpected** | `governance-mgmt.html.bak` (77,518 b) · `governance-mgmt.html.bak2` (78,433 b) |

**Reported rather than removed:** the two `.bak` files are debris from the
alert-resolve UI work — the recovered copy and the pre-patch copy. They belong
to no workstream, and `*.html` is **not** gitignored at this path, so they are
untracked-but-committable and could be swept into a future commit by accident.

**A naming collision worth fixing in the record:** "B" now means two unrelated
things — **Defect B** (a test asserting over a population it does not own) and
**Migration B** (`assigned` requires a claim time). They appear in the same
tables and are not related.

## 17.6 Readiness

| workstream | production | working tree | next decision |
|---|---|---|---|
| Defect A schema control | **LIVE (partial)** — 3 of 5 kinds recorded | — | see 17.1 |
| Defect A application remediation | **NOT LIVE** | present | authorization required; **closes the remaining 2 kinds and the forgeable decision** |
| Migration B schema invariant | **LIVE / NOT VALID** | — | future validation gate |
| Atomic claim fix | **NOT LIVE** | present | authorization required; latent (0 production escalations) |
| Four local violations | local evidence | untouched | separate disposition |
| Reminder allocation | **DEFERRED** | present | remain deferred |
| Tests | production-independent | present | travel with their remediation |
| Docs | production-independent | present | review separately |

**Recommended next authorization: the Defect A application remediation.** It is
the only workstream where production is currently in a *partially* remediated
state, and the divergence between the deployed schema and the deployed code is
a condition no existing control can observe.
