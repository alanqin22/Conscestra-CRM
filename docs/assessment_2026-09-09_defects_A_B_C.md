# Independent Assessment — Defects A, B, C

**Date:** 2026-09-09 · **Scope:** three defects assessed as newly discovered, on
their own evidence · **Status:** assessment only. No code, schema or
configuration was changed.

---

## Method and epistemic protocol

Each defect was re-measured from source. Nothing was inherited from the session
that first surfaced these items, including that session's own conclusions —
where re-measurement contradicts the description handed to this assessment, the
correction is stated in full rather than reconciled away.

**Labels used throughout, applied per claim rather than per section:**

| label | meaning |
|---|---|
| **MEASURED** | observed directly in this assessment; command and result reproducible |
| **INFERRED** | concluded from measurement, with the inference step named so it can be attacked |
| **UNVERIFIED** | not checked. Never silently upgraded to either of the above |

**Environments.** All direct measurement is against the local database and the
working tree. Production (Railway) was checked read-only through the declared
migration tooling; no production query was run. Where a production claim rests
on the migration ledger rather than on a direct read, it is labelled INFERRED
and the exact verifying query is given.

### Corrections to the descriptions as received

Three of the items were handed to this assessment with specific technical
claims. Two of those descriptions are materially wrong. This is reported first,
because the errors change the classification and the decision.

| item | as described | as measured |
|---|---|---|
| A | three rejected kinds, "alert mail" | **five** rejected kinds; the affected channel is not alert-specific |
| B | mechanism plausible, leaking test unidentified | mechanism **reproduced**; leaking test identified |
| C | "corpus size 548 vs validated pool 500" | 548 is the **largest template group**, not corpus size. Corpus is 14,785 against a baseline of 12,976 — and that movement trips nothing |

---

# Defect A — Governance mail is written to an unwidened ledger vocabulary

## A.1 Finding statement

`staff_email_ledger` constrains `email_kind` to a four-value vocabulary that was
correct when the table was created and was never widened when a second mail
channel was introduced with a vocabulary of its own. **Every send through
`governance_policy.email_authority()` — all five kinds it is called with — is
rejected by that constraint, swallowed by a deliberate fail-open, and
transmitted without a ledger record.**

The defect is therefore **not alert-specific**. Alert mail is one of three
affected subsystems; approval SLA-breach mail and approval re-escalation mail
are equally unrecorded. `email_authority` has, on this evidence, never
successfully written a ledger row in its operational life.

The sharpest expression of the defect is not the missing rows. It is that the
code states the guarantee as fact:

> `governance_alerts.py:189` — *"email_authority is additionally ledgered per
> (kind, ref) through staff_email, so a retry cannot double-send."*

That comment is a written invariant, it is false, and it was relied on when
reasoning about the correctness of the surrounding block.

## A.2 Measured evidence

**MEASURED — the constraint.**

```sql
staff_email_ledger_email_kind_check ::
  CHECK (email_kind = ANY (ARRAY['approval','escalation','escalation_remind','digest']))
```

**MEASURED — the five kinds passed to `email_authority`.** Every call site,
exhaustively:

| call site | kind | subsystem |
|---|---|---|
| `governance_alerts.py:194 → :209` | `alert_assigned` | alert ownership |
| `governance_alerts.py:335 → :347` | `alert_escalated` | alert SLA escalation |
| `governance_alerts.py:411 → :421` | `alert_reescalation` | alert reminder loop |
| `governance.py:1885 → :1888` | `approval_breach` | approval SLA breach |
| `governance.py:2024 → :2038` | `approval_reescalation` | approval reminder loop |

**None of the five is in the permitted set.** The intersection is empty.

**MEASURED — the failure path.** `email_authority` → `staff_email.begin_send()`
→ `claim()`. `claim()` (`staff_email.py:852-856`) catches `Exception`, rolls
back, logs at WARNING, and returns `(None, False)`. `begin_send()` then returns
`{"proceed": True, "recorded": False}` — the documented fail-open. Reproduced
directly:

```
[staff_email] claim failed (apply sql/staff_email_ledger.sql?): new row for
relation "staff_email_ledger" violates check constraint
"staff_email_ledger_email_kind_check"
DETAIL: Failing row contains (..., alert_escalated, critical, ...)
[staff_email] ledger unavailable for alert_escalated:607babe1... — proceeding UNRECORDED
```

**MEASURED — ledger contents.** Local: 1,734 rows, **every one `approval`**,
zero of any other kind. The `approval` rows originate from a different path
(`governance.py:948/984`, via `staff_email.observe`), not from
`email_authority`.

**MEASURED — the constraint is as declared and unmodified.** It is defined in
`governance/sql/staff_email_ledger.sql:130` and in the base schema. No migration
in the repository alters or drops it.

**MEASURED — production schema state.** `migrate.py --check --target railway`
reports **`schema is current`**, and `staff_email_ledger.sql` is in
`REQUIRED_MIGRATIONS`.

**INFERRED (production carries the identical constraint).** From: the migration
is declared required; the ledger reports the schema current; the constraint is
defined in that migration; and no later migration alters it. The inference step
is that "schema is current" implies constraint-level identity, which the tooling
does not verify directly.

**MEASURED — why this was invisible.** `email_authority` returns early when
`GOV_ROUTE_EMAIL` is unset, which is the local default. The constraint violation
cannot occur in a default local run, so no developer environment produces the
warning.

## A.3 Architectural impact

**Classification: Class I — a declared control that has never operated.**

This is not a bookkeeping gap. Three distinct properties are affected:

1. **The idempotency guard is absent for this channel.** `idempotency_key(kind,
   ref)` is the mechanism that makes a governance send exactly-once across
   retries and replicas. For all five kinds it computes a key, attempts to
   claim it, fails, and proceeds. **INFERRED:** a retry, a replica race, or an
   event redelivery on any of these five paths is unguarded by the ledger.

2. **Defence-in-depth is reduced to one layer, silently.** Independent guards do
   exist and were measured: `alert_assigned` mails **only on creation** (a
   dedupe fold returns without mailing), and escalation carries
   `escalation_notices` / `last_escalation_notice_at` on the alert row. So the
   observable failure rate today is plausibly zero. The defect is that the
   *declared second layer* is absent while the code asserts it is present — and
   the case the comment specifically cites, a retry, is the case the first layer
   does not cover.

3. **The audit surface is incomplete in a way that reads as evidence of
   absence.** A ledger queried for `alert%` or `approval_breach` returns
   nothing. Nothing distinguishes "no governance mail was sent" from "all
   governance mail was sent unrecorded". For a system whose stated model is
   *claim → execute → verify → durable evidence*, the durable-evidence step is
   missing on the channel that carries accountability notices.

**The generalisable shape.** A new channel was introduced with a new vocabulary,
and the schema that validates that vocabulary was not part of the change. The
project has an established pattern for exactly this — `governance_five_
authorities.sql` drops and re-adds role CHECK constraints when the role
vocabulary widened. The pattern exists; it was not applied here.

**Aggravating factor:** the fail-open is correct and should stay. *"An executive
must not miss an escalation because an audit table was missing"* is the right
trade. But a fail-open converts a schema mismatch into a silent, permanent,
unbounded condition — the log line is the only signal, and it is one WARNING per
send in a channel that is off by default in development.

## A.4 Decision required

**D-A1 — Is the ledger authoritative for governance mail, or is it not?**
Both answers are defensible and they lead to different systems.
*(a)* It is authoritative: the vocabulary must include every kind that reaches
it, and a kind outside the vocabulary is a deployment error.
*(b)* It is not: `email_authority` should not be calling it, and the
idempotency claim must be removed from the code comment and from the design
rather than left as an unmet promise.
**This must be decided before any change**, because (a) and (b) produce
opposite edits.

**D-A2 — Does a fail-open control require a mandatory alarm?** The fail-open is
right. A silent fail-open is what made this survive undetected. Whether
"proceeding UNRECORDED" must raise a governance alert — as opposed to a log
line — is a policy decision about the class of controls, not about this bug.

**D-A3 — Does correcting the schema require reconstructing the missing
history?** Unrecorded sends have already occurred in production. Whether the
ledger is backfilled from an independent source, or begins from the fix date
with the gap declared, is a decision about audit integrity. **Backfill from the
system's own logs would record sends the ledger never claimed, which is the
opposite of what the ledger means.**

*No remediation is proposed here. D-A1 determines what remediation even is.*

## A.5 What remains unverified

- **Direct production measurement.** The constraint on production was inferred,
  not read. Verify with:
  `SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'staff_email_ledger_email_kind_check';`
- **How many sends have gone unrecorded, and over what period.** Not counted.
  Requires the production log history, not the database.
- **Whether any duplicate governance mail has actually been transmitted.** The
  guard is absent; whether the absence has ever *mattered* was not established.
  The independent guards may have covered every real occurrence. **Do not state
  that duplicates occurred, and do not state that they did not.**
- **Whether `escalation_notices` is itself sound** as the sole remaining guard.
  Its increment path, and its behaviour under concurrent sweeps, were not
  examined.
- **The `observe()` path.** Only `begin_send` was traced. Whether `observe()`
  applies the same vocabulary, and whether it too can reject silently, was not
  checked.
- **Other tables with vocabulary CHECK constraints** that may have the same
  latent mismatch. Not audited.

---

# Defect B — A dated dedupe key makes a test control fail on a calendar boundary

## B.1 Finding statement

`TestDailyProposalCap::test_hitting_the_cap_opens_one_owned_work_item_per_day`
asserts that exactly one non-terminal `sampled_review` alert exists for the test
action class. An earlier test in the same file trips the proposal cap and leaves
its alert **open**. Within one calendar day this is invisible, because the cap
alert's dedupe key is date-stamped and the second alert folds into the first.
**At the next UTC date change the key differs, the fold stops, two open rows
exist, and the assertion fails.** The failing test's own cleanup then cancels
both, so every subsequent run that day passes.

The control is therefore **self-healing, which is precisely why it is
dangerous**: it fails once per day at most, in the first run after midnight,
and looks like flake to anyone who re-runs it.

## B.2 Measured evidence

**MEASURED — the leak is real and reproducible.** After cancelling all open
`sampled_review` rows and running the file once:

```
LEFTOVER OPEN sampled_review rows after one full file run: 1
   ('test.activation_write', 'proposal_cap:test.activation_write:2026-09-09', 'open', 'governance')
```

**MEASURED — the producer and the key.** `governance.py:807-818` opens the alert
with `dedupe_key=f"proposal_cap:{action_type}:{day}"` where `day` is
`datetime.now(timezone.utc).date().isoformat()`, and `affected_id=action_type`.

**MEASURED — the leaking test.** The cap is tripped at
`test_governance_activation.py:1005` (`pytest.raises(ProposalCapReached)`),
which asserts only on `action_approvals` and performs no alert cleanup. The
test at `:1013` does clean up, in a `finally`, but only the rows **it queried**.

**MEASURED — the rollover failure, reproduced rather than argued.** The leftover
row's key was backdated to the previous day to simulate the boundary, then the
cap test was run:

```
assert len(rows) == 1, "one work item per class per day, not one per refusal"
AssertionError: assert 2 == 1
 +  where 2 = len([( ... 'low', 'open'), ( ... 'low', 'open')])
```

This is the **same assertion, same shape** as the failure originally observed in
the wild. Simulated rows were cancelled afterwards; no state was left behind.

**MEASURED — a second, independent producer exists.** `a2a.py:1705` also opens
`sampled_review` alerts, with **no dedupe key** and `affected_id` set to an
approval UUID. These do not match the test's predicate today. They are recorded
because they share the alert class and would match a broadened predicate.

## B.3 Architectural impact

**Classification: Class II — a control that fails for a reason unrelated to the
property it guards.**

The assertion is a **census over a population that persists across runs and
across days**, compared to the constant `1`. This is the same proxy-versus-
property shape corrected six times elsewhere in this codebase during P1 — a
control keyed on a count rather than on the property it means to protect. It
survived in the test layer, where that class of defect was not swept.

The impact is bounded but specific:

1. **CI reliability.** The first pipeline run after any UTC date change, on a
   day where the earlier test has leaked, fails on an assertion that has nothing
   to do with the change under test. A red build with no relationship to the
   commit is how a team learns to re-run rather than read.

2. **It degrades the credibility of a real invariant.** The property being
   guarded — *one work item per class per day, not one per refusal* — is a
   genuine and valuable governance invariant about attention budget. A guard
   that cries wolf on a calendar boundary trains its readers to dismiss it.

3. **Test isolation is not enforced anywhere.** The leaked row is the visible
   instance. **INFERRED:** nothing structurally prevents other tests leaking
   non-terminal governance rows; this one is detectable only because a later
   test happens to count them.

**Not a production defect.** The production behaviour it exercises — one cap
alert per class per day, folding on redetection — is correct and was measured
correct. The defect is entirely in the control.

## B.4 Decision required

**D-B1 — Should test-created governance rows be isolated, or cleaned?** These
are two different architectures. Cleanup is per-test discipline that the next
test to forget will break again. Isolation — a dedicated action class, or a
transactional boundary — makes the leak structurally impossible. The second is
more work and changes how governance tests are written.

**D-B2 — Should assertions over live governance tables be permitted to compare
against constants at all?** This is the general form. The project has already
ruled against it for production controls. Whether that ruling extends to the
test suite is an unmade decision, and it governs more than this one assertion.

**D-B3 — Is a once-a-day self-healing failure acceptable until B is scheduled?**
It is genuinely low-severity. Deciding to accept it *explicitly*, with the
mechanism recorded, differs from leaving it to be rediscovered as flake.

## B.5 What remains unverified

- **Whether the leak is deterministic across orderings.** Measured once, in
  source order. Under `-p randomly` or `-n auto` the interaction may differ.
- **Whether other test files leak non-terminal governance rows.** Only
  `sampled_review` was examined, because only it is asserted on.
- **Whether the failure has ever actually occurred in CI**, as opposed to
  locally. The CI history was not consulted.
- **Behaviour across a timezone boundary other than UTC.** The key uses UTC; the
  local environment does not run in UTC. Not tested.
- **Whether the `a2a.py:1705` keyless producer leaks unboundedly.** Its rows were
  observed open and uncleaned, but its lifecycle was not traced.

---

# Defect C — The retrieval corpus has moved outside its validated envelope

## C.1 Finding statement

**The description as received is incorrect and the correction changes the
decision.** `548` is not the corpus size. It is the **largest template group** —
the count of indexed rows sharing an identical leading 60 characters. The guard
that fired measures *template crowding*, not corpus growth.

Measured accurately, three separate things are true:

1. **Template crowding has worsened past its validated ratio.** The largest
   group is 548 against `N_VEC=500` (ratio 1.10), versus 480 against 500
   (ratio 0.96) at validation. The guard's threshold is `1.1 ×` the baseline
   ratio; the exceedance is real but **narrow** — the trip point is 528.
2. **The corpus has grown, and that trips nothing.** 12,976 → 14,785 rows
   (+13.9%). The size threshold is a factor of two in either direction. This is
   not what fired, and *"the corpus has outgrown its validated pool"* is not the
   finding.
3. **Redundancy overall has not worsened.** `duplicate_share` moved 0.530 →
   0.525, i.e. slightly *down*. A single group grew; the corpus did not become
   more redundant.

**And a fourth thing, which no description mentioned and which is the one with
operational teeth:** the recency ranking slice is fixed at
`MAX_CANDIDATES=4000`, so as the corpus grows, the share of it any query can
see falls. It is now **27%**, disclosed by the search path itself on every call.

**Separately: only two of the three failing tests concern drift at all.** The
third fails for a different reason and was misattributed.

## C.2 Measured evidence

**MEASURED — current state versus the recorded baseline:**

| property | validated 2026-09-01 | now | threshold | fires? |
|---|---|---|---|---|
| rows | 12,976 | **14,785** | ×2 or ÷2 | no |
| largest template group | 480 | **548** | ratio > 1.056 | **yes** |
| duplicate share | 0.530 | **0.525** | +0.15 | no |
| crowding ratio | 0.96 | **1.096** | — | — |

**MEASURED — what the largest group actually is.** The top groups are
machine-generated activity records, not human content:

```
  548  'Send payment reminder — Created by workflow engine from even'
  480  'Requested additional information from customer.'
  473  'Thank-you call to customer — Created by workflow engine from'
  349  'Order confirmation notification accepted by provider — SO-20'
```

The group that was largest at validation (480) is **unchanged at 480**. The
group that overtook it is workflow-engine output. Corpus composition by source:
`activity` 13,388 of 14,785 — **91%**.

**INFERRED (well-supported):** the growth in the largest template group is the
workflow engine indexing its own generated activity rows, not ingestion of new
human content. Supported by the group's text, by the source-type distribution,
and by the untouched 480 group. Not established: the rate, or whether it is
bounded.

**MEASURED — the truncation disclosure**, emitted by the search path itself:

```
[content_index] search ranked 4000 of 14785 matching records (27%) — results are
drawn from the most recent slice only, and better matches may exist outside it
```

`MAX_CANDIDATES = 4000` (`content_index.py:308`). This is a **declared,
pre-existing limitation** — documented at `content_index.py:32` — that has
worsened with corpus growth: coverage fell from ~31% at validation to 27%.

**MEASURED — the three test failures do not share a cause.**

| test | cause |
|---|---|
| `test_status_carries_both_pool_signals` | drift guard: crowding ratio |
| `test_the_standing_disclosure_and_the_drift_signal_are_not_the_same_flag` | drift guard: crowding ratio |
| `test_recency_only_output_is_frozen` | **different** — a pinned result count moved: `payment reminder` 4 → 2 |

**MEASURED — the kill switch itself is intact.**
`test_recency_only_keeps_the_legacy_budget`, the *structural* test that the
mode-dependent ranking budget is present in `search()`, **passes**. The
behavioural pin moved while the code did not.

**MEASURED — the moved query and the grown template are the same subject.** The
pinned query whose count changed is `"payment reminder"`; the template group
that grew to 548 is `"Send payment reminder — Created by workflow engine…"`.
Running that query under `recency_only` returns 2 results, the first being a row
from that exact group.

**INFERRED:** the pinned counts moved because the corpus changed underneath a
fixed-size recency slice, not because the kill switch regressed. Supported by
the structural test passing, by no relevant change in `content_index.py`, and by
the query/template correspondence. **Not established:** the precise mechanism by
which the count *fell* rather than rose. Crowding would ordinarily be expected
to add near-duplicate results, not remove them. **This gap is stated rather than
filled.**

## C.3 Architectural impact

**Classification: Class III — a disclosed limitation that has drifted outside
its validated envelope — plus an embedded Class II control.**

**On the drift itself.** The system behaved correctly. `N_VEC` carries a written
instruction — *"revalidate if the corpus changes materially, do not silently
retune"* — and rather than leave that instruction in a comment where nobody runs
it, the codebase converted it into a check that fires. Two flags are maintained
separately and deliberately (`revalidate` = *is the margin thin?*, permanently
true and disclosed; `changed_since_validation` = *has it moved?*). **This is a
control working exactly as designed, and it should not be read as a system
failure.**

The genuine architectural exposure is narrower than "the pool is too small":

- **The retrieval quality guarantee is out of warranty.** Zero-content-loss was
  measured at one corpus composition. That composition no longer holds. Nothing
  says quality has degraded — it says **the evidence for the current
  configuration has expired**.
- **The tuned parameter is validated against a corpus the system generates
  itself.** 91% of the index is `activity` rows, and the fastest-growing
  template is workflow-engine output. **INFERRED:** left alone, the vector pool
  is increasingly spent on the system's own automation exhaust rather than on
  content a human wrote. This is a corpus-composition question, and pool
  expansion would postpone it rather than answer it.
- **Coverage decays with growth by construction.** A fixed 4,000-row recency
  slice against a growing corpus means the *declared* invisible share rises
  monotonically — 69% at validation, 73% now. This is disclosed on every search
  and is not a bug, but it is the property most likely to be mistaken for one.

**On the embedded control defect.** `test_recency_only_output_is_frozen` pins
absolute result counts against a live, growing corpus. Its docstring defends
this explicitly and the argument is sound — *"a self-comparing test passes when
both sides drift together, which is the failure it exists to catch."* But the
consequence is a control that **cannot distinguish the regression it guards
(the kill switch changed) from ordinary corpus movement (the data changed).**
Here it reported the former and the evidence indicates the latter. It is the
same census-versus-property shape as Defect B, with a stronger justification
behind it — which makes the decision harder, not easier.

## C.4 Decision required

**D-C1 — Revalidate, or re-tune, or accept?** These are three different
commitments. Revalidation means re-running the end-to-end content-loss
measurement at the current composition and re-recording the baseline — the
codebase states this is *"a decision, not a heuristic"*. Re-tuning `N_VEC`
without that measurement replaces validated evidence with a guess. Accepting
means recording that the configuration is out of warranty and that this is
tolerated.

**D-C2 — Should system-generated content be indexed at all?** The prior
question, and the one that determines whether D-C1 is even worth doing. If
workflow-engine activity rows are indexed because everything is indexed rather
than because retrieval needs them, the answer is corpus scoping, and every
threshold here is being tuned against noise. **Answer D-C2 before D-C1.**

**D-C3 — Should the recency slice scale with the corpus?** `MAX_CANDIDATES` is
fixed while the corpus is not, so disclosed coverage decays by construction.
Whether that is intended steady-state behaviour or an unmade decision is
unresolved.

**D-C4 — What should a behavioural pin over live data assert?** The count, the
identity of results, or a property such as *non-empty and correctly ordered*?
This governs the pinned-counts test and any future test of the same shape.

**D-C5 — Is a red suite acceptable as a standing signal?** Two tests are
correctly reporting an expired baseline. They will stay red until D-C1 is taken.
A permanently red suite is itself a control that decays.

*No remediation is proposed. D-C2 precedes D-C1, and D-C1 is a measurement
commitment rather than a code change.*

## C.5 What remains unverified

- **Why the pinned count fell from 4 to 2** rather than rising. The mechanism is
  not established, only bounded to "not a code change".
- **Whether retrieval quality has actually degraded.** Nothing here measures it.
  The claim is that the *evidence has expired*, which is not the same as
  degradation. **Do not report this as a retrieval-quality regression.**
- **The growth rate and bound of the workflow-engine template group.** Measured
  once. Whether it is linear, bounded by a retention policy, or unbounded is
  unknown, and it determines urgency entirely.
- **Production corpus state.** All figures are local. Production may sit on a
  different side of every threshold.
- **Whether the other two failing tests would pass after revalidation**, or
  whether they encode further assumptions about the baseline.
- **Whether `duplicate_share` falling while one group grows** indicates broader
  corpus dilution worth assessing on its own.

---

## Summary

| | classification | severity | blocking decision |
|---|---|---|---|
| **A** | Class I — declared control never operated | **High** — audit integrity + a false written invariant | D-A1: is the ledger authoritative for governance mail? |
| **B** | Class II — control fails on an unrelated property | Low — no production impact | D-B1: isolate test rows, or clean them? |
| **C** | Class III — expired validation envelope | Medium — warranty expired, degradation unproven | D-C2: should system-generated content be indexed? |

**A is the one that matters.** B is a test-hygiene defect with a measured
mechanism and no production consequence. C is a control correctly reporting that
its evidence has expired — the system behaving as designed, requiring a decision
rather than a fix. A is a governance control that has never operated, in a
system whose central claim is that governed actions leave durable evidence, and
whose source code asserts the guarantee as fact.

**No remediation is proposed in this document, by design.** Each defect's first
decision determines what remediation would mean, and in two of the three cases
the obvious fix — widen the constraint, raise the pool — would resolve the
symptom while leaving the architectural question unasked.
