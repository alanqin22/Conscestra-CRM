# P1 Remediation — Unit 1

**2026-09-07.** Under the **P0 Preservation Gate**. Baseline:
`docs/architecture_reassessment_2026-09-07.md`,
`docs/post_remediation_verification_2026-09-07.md`.

> **P0 REMAINS CODE-COMPLETE AND NOT PRODUCTION-VERIFIED.** Nothing has been
> committed or deployed. The migration is still `PENDING DEPLOYMENT`. No P1
> change in this unit alters N-01 or N-02 behaviour; the P0 suites were re-run
> after every change and are green (227 passed).

**Test results: 25 failures → 2.** 2,966 passing. `verify_invariants`: all hold.
Neither remaining failure was made green by weakening an invariant; both are
classified below and one is **deliberately left red**.

---

## Item 1 — Orphaned consequential events · CLASSIFIED, NOT ACTIONED

**Correction to my previous report.** I stated Railway database access was
blocked in this environment. That was wrong: the block applies to ad-hoc
connections, not to the project's own declared read-only tool with its explicit
`--target railway` flag. The classification has now been run against production.

```
python -m scripts.classify_orphaned_events --target railway
TARGET: RAILWAY   orphaned events: 39
  REPLAY 39 · REVIEW 0 · WRITE_OFF 0
CUSTOMER IMPACT OF REPLAYING THE 'REPLAY' BUCKET: 18 message(s) to 18 order(s).
  The other 21 REPLAY event(s) are internal and contact nobody.
```

**FACT — VERIFIED (production).** All 39 events belong to 21 orders, every one
of which is `status='shipped'`. None is cancelled, refunded or returned; none
has an accepted notification of its type. Nothing contradicts a shipped notice.

### The classifier was wrong before it was right

Its first production run returned **39/39 REPLAY** with the reason "no accepted
notification exists for it". The verdict was right and **the reason was
misleading**, and the difference mattered:

- **18 of the 21 orders carry BOTH** an `order.shipped` and an
  `order.status_changed` event. A reader would reasonably have expected 39
  customer emails.
- `order.status_changed` sends **nothing**. `handle_order_status_changed`
  closes the order's milestone activity; its email half was deliberately removed
  so two senders with two idempotency stores could not each conclude the other
  had not sent.

The classifier now distinguishes customer-facing event types from internal ones
and reports the number a person is actually deciding about: **18 messages, not
39.** A bucket with a wrong reason is worse than no bucket, because it is acted
on.

**HUMAN ACTION REQUIRED / DECISION REQUIRED.** Nothing was drained. The decision
is: send 18 late shipping notices, or write them off with recorded reasoning.
`order_notifications` is keyed `UNIQUE(order_id, event_type)`, so a second drain
re-sends nothing. **This is production work and belongs after deployment**, not
mixed into code remediation.

---

## Item 2 — The test/CI seam · CLOSED (21 tests restored)

**Invariant preserved:** a laptop must not become a second live sender.
**Defect:** the guard enforcing it sits *below* the seam the notification tests
exercise, so adding it turned 21 of them red — the subsystem behind the 18
unnotified orders had no working local verification.

**Both obvious fixes were rejected and why is recorded in the code:**

- Relaxing the assertion to accept `queued` makes the suite agree that
  notifications are not sent — the live production failure.
- Setting `AGENT_BUS_AUTOSEND_LOCAL=1` in the fixture borrows the **operator's**
  escape hatch, which means "this laptop is deliberately a live sender", stays
  set for whatever the process does next, and is precisely "pretending a laptop
  is production".

**The seam is strictly narrower than the override it sits beside.** It is not an
environment variable and opens only when **both** hold:

1. `pytest` is in `sys.modules` — production installs `requirements.txt`, which
   does not contain it; and
2. `is_deployed()` is **False** — so a test run *on* a deployment cannot open it.

`enable_unattended_for_tests()` **raises** rather than returns False when either
fails, so misuse is loud. Fixtures close it on teardown.

The two halves of the gate are now two fixtures (`unattended_seam` and
`autosend`), mirroring the two independent questions the production gate asks —
which is what let `test_autosend_off_releases_the_lease_instead_of_holding_it`,
which drives the operator flag itself, keep working.

**Tests:** all 121 in `test_order_lifecycle_notifications.py` pass, every one
still asserting `state == "accepted"` **and** a real provider call count. Five
new tests in the **gated** activation suite govern the seam itself: closed by
default, does not outlive its test, refuses to open on a deployment, refuses
outside pytest, and is not reachable from configuration.

**CI gate:** `test_governance_activation.py`, `test_policy_governance.py`,
`test_escalation_email_routing.py` and `test_executive_identity_mismatch.py` are
in `verify_gate.py::CONTROL_TESTS`.
**NOT ESTABLISHED:** whether `test_order_lifecycle_notifications.py` can join
them. The CI header records that the wider suite "reads ambient business rows
and fails on a freshly built database", and I cannot verify CI behaviour from
here. Adding it needs one CI run as evidence.

---

## Item 3 — The 177/178 unassigned work items · INVARIANT REPLACED

**The old assertion was a proxy, not the property.** `assert count < 100` broke
for the only reason a census can break: the population grew. It stood in for
"this surface is live work, not history" — and stopped measuring that the moment
the population moved. Same defect already corrected in `test_a2a_invariants`.

**Now measured exactly, against the database rather than a number:** every row
on the surface must still be an *open* activity. One closed row fails it however
small the total; a legitimate doubling of open work does not.

**The debt is held separately, as a ratchet, not absorbed.**
`_UNOWNED_LIVE_WORK_HIGH_WATER` records the measured value and may shrink, never
grow. 110 have no owner recorded, 63 have an ineligible owner, 5 are collisions
— D-02 appearing in a control rather than a report.

### The ratchet found a live write-path defect on its first run

**FACT — VERIFIED.** The mark moved 178 → 179 → 180, one per full-suite run. The
added rows read `Created by workflow engine from event <uuid>`. In
`workflow_execute_action`'s `create_task` branch
(`governance/schema/00_base_schema.sql`):

```sql
BEGIN  v_owner := NULLIF(p_payload->'after'->>'owner_id','')::uuid;
EXCEPTION WHEN OTHERS THEN  v_owner := NULL;  END;
INSERT INTO activities (... owner_id ...) VALUES (... v_owner ...);
```

**A live write path manufactures consequential work with no accountable human
whenever its trigger is unowned** — which, given D-02, is most of the time. That
is where the 110 `NO_OWNER_RECORDED` rows come from. This is not test pollution;
the test suite merely exercises the path.

**The ratchet is deliberately left RED.** Padding it to absorb a known defect
would convert it back into the census it replaced. It will stay red until the
write path is fixed, and that is the control doing its job.

**DECISION REQUIRED.** When the workflow engine cannot resolve an eligible
owner, should it: (a) fall back to the action class's accountable authority via
`resolve_accountable_owner`; (b) refuse to create the task, failing closed; or
(c) create it and raise a governed alert? This changes a live write path and
needs a governance-repo migration, so it is a decision, not a cleanup.

---

## Item 4 — The seed domain · RESOLVED BY REPLACING THE MECHANISM

The mandate's instruction was not to pick a side but to determine what the gate
is supposed to prove, and replace the mechanism if exemption is needed.

**The gate conflates two questions:** "is this address deliverable and opted
in?" (verification) and "is this a synthetic record?" (provenance). A domain
check answered both, which is why the test and the code disagreed.

**The clean fix — gate on provenance — was investigated and REFUSED on the
data.** `contacts.is_synthetic` marks **176 of the 181 seed-domain contacts NOT
synthetic**, because the seed-email migration destroyed the distinction. Using
that flag would have re-opened the quota leak almost exactly: 176 addresses on a
catch-all we invented would become deliverable again.

**What the old test actually protected was a capability** — an end-to-end send
exercise reaching a mailbox we control without involving a customer. That is
restored **per address**, never per domain or per flag:

- `EMAIL_E2E_ALLOWLIST`, a comma-separated list of specific mailboxes.
- **Empty by default** — an unset variable exempts nothing.
- Exempts only the *domain* check; the verification check still applies.
- Cannot generalise: an allow-listed address does not exempt its neighbours.

The old domain-wide exemption made 181 addresses deliverable to prove that one
of them composed correctly. Four tests pin the new contract, including that
neighbours stay blocked and that the list is empty by default.

---

## Regression analysis for this unit

| Change | Invariant preserved | New risk | Test |
|---|---|---|---|
| Test seam | laptop is not a live sender | a new code path in the guard — mitigated: not env-reachable, refuses on a deployment, refuses outside pytest, raises on misuse | 5 gated tests |
| Fixture split | both halves of the gate remain independent | a test could open the seam and not close it | teardown asserted by `test_the_seam_opens_only_the_deployment_half` |
| Work-surface property | closed history is not resurfaced | an exact DB check is slower than a count | it queries only the returned ids |
| Ownership ratchet | debt cannot grow unnoticed | **stays red until the write path is fixed** — accepted deliberately | — |
| Per-address allow-list | synthetic addresses stay blocked | a misconfigured list could name a real customer — it is explicit, visible and empty by default | 4 tests |
| Classifier correction | the reason must match the verdict | none | production re-run |

**P0 preservation:** N-01 and N-02 suites re-run after every change — 227
passed. No file touched by this unit is on the N-01/N-02 decision or policy
path except `agent_bus._is_real_email`, which neither gates nor records a
governance decision.

---

## Remaining failures — 2

| Test | Classification | Action |
|---|---|---|
| `test_K_H2_unowned_live_work_does_not_grow` | **REAL DEFECT** (write path) + **DECISION REQUIRED** | Fix `workflow_execute_action`'s owner fallback. Left red on purpose. |
| `test_recency_only_output_is_frozen` | **ENVIRONMENTAL** *(probable, not proven)* | Three of four queries still match; only `payment reminder` moved 4 → 2. Discriminator: do those two rows still exist in the local index? A check, not a guess. |

---

## Is the next P1 item safe to begin?

**Yes**, with one condition. The workflow-engine ownership defect (Item 3) is
the natural next unit and it touches a **live write path in the governance
repo**, so it needs the decision above before code. Everything else in the P1
backlog — D-01 posture, D-05 audit model, D-10 migration truth, D-12/D-18
retrieval governance, N-08 alert quality — is independent of it.

**The deployment gate is unchanged and unaffected by this unit:**

1. push `governance/`, update `.governance-pin`
2. apply `governance_decision_link_identity.sql` to Railway
3. deploy the application
4. adversarial production verification of N-01 and N-02
5. only then is P0 production-verified

Nothing here has been committed or deployed.

---

# P1 Remediation — Unit 2

**2026-09-07.** Both owner decisions of `docs/governance/decisions_2026-09-07.md`
implemented to their agreed boundary.

**Test results: 2,968 passing · 1 failing.** (25 → 1 across both units.)
`verify_invariants`: all hold. P0 suites green.

## Decision A — declared, measured, not enforced

| Change | |
|---|---|
| `work_ownership.WORKFLOW_OWNER_REQUIRED = False` | the enforcement point exists, is named, and is one flag away |
| `work_ownership.workflow_ownership_rate()` | the measurement, as a **ratio** |
| `/platform/health` → `workflow_work_accountable` | **0.42%**, WARNING, stating why it is unenforced |
| 2 gated tests | the rate may rise and never fall; the flag cannot be flipped below 95% without failing loudly with the numbers |

**The absolute ratchet was replaced, not re-baselined.** `_UNOWNED_LIVE_WORK_HIGH_WATER`
was itself repeating the census mistake in slower motion — it ticked 178 → 179 →
180, once per suite run, and would have needed padding every time. A ratio moves
only when the property moves, and it is the same number that measures progress:
as the D-02 backfill lands, resolution starts succeeding and it climbs without
the write path changing.

**Enforcement condition:** `workflow_work_accountable` > 95%, then flip the flag.

### One guard fired correctly against me

`test_K_N_no_code_path_here_ever_writes_an_owner` scans this module's source for
`INSERT INTO ACTIVITIES` — and caught the SQL I had **quoted in a comment** to
describe the defect. The guard cannot distinguish a quotation from a statement,
and narrowing it to exclude comments would weaken it to let a comment through.
The comment was reworded; the guard was not touched.

## Decision B — recorded, not executed

`docs/governance/decisions_2026-09-07.md` carries the disposition:
**send `SO-2026-102219`** (the one order with no communication of any kind),
**write off the other 17**, replay-or-leave the 21 internal events.

Nothing was sent, drained or written off. This is production work and happens
after deployment, per the mandate's separation.

**A finding raised by the evidence, not previously registered:** 18 orders have
sat at `status='shipped'` for five days with no update. `order.delivered`
appears never to fire for them — and the orphan alert cannot catch it, because
no event is stuck; none is being produced.

## Remaining failure — 1

| Test | Classification | Evidence |
|---|---|---|
| `test_recency_only_output_is_frozen` | **ENVIRONMENTAL** *(probable, not proven)* | 3 of 4 queries still match; only `payment reminder` moved 4 → 2. Discriminator: do those two rows still exist in the local index? |

### One intermittent failure observed and NOT attributed

`test_hitting_the_cap_opens_one_owned_work_item_per_day` failed on one full-suite
run and passed on the next. It passes in isolation, passes with its own file,
and passes paired with both files changed in this unit. Two candidate mechanisms
were checked and ruled out: the new metric is in the GOVERNANCE section, and
`detect_platform_degraded` reads only PLATFORM metrics, so it cannot raise an
alert that competes for the daily cap.

**INFERENCE, not FACT:** a pre-existing isolation weakness around date-scoped
shared state, surfaced intermittently by full-suite ordering. Recorded rather
than dismissed — an intermittent failure in a GATED suite will eventually block
a release, and the next person should not have to rediscover that these four
checks were already done.

---

# P1 Remediation — Unit 3

**Analysis only. No code changed, no owner granted, nothing written.**
Everything below is dry-run or read-only.

## The last failing test — ENVIRONMENTAL, now CONFIRMED

`test_recency_only_output_is_frozen` was classified ENVIRONMENTAL *(probable)*.
The discriminator has been run and the mechanism is now known:

```
[content_index] search ranked 4000 of 14275 matching records (28%)
  - results are drawn from the most recent slice only
```

`recency_only` ranks a **fixed 4,000-row window** of a corpus that has grown to
**14,275**. Two of the four pinned `payment reminder` hits have fallen out of
that window; one of the two survivors is
`Send payment reminder - Created by workflow engine from event bed124b8` — the
same unowned write path from Unit 1, adding rows that are by definition the most
recent and therefore displace the pinned ones.

**The pinned values were correct for a smaller corpus.** This is the proxy
pattern for a third time: a frozen expected output pinned against a live,
growing population.

**DECISION REQUIRED — and the test says so itself:** *"If this change is
intended, it is a separate decision and these numbers are re-pinned with it."*
It is not an intended behaviour change, so **re-pinning would pin to a moving
target and break again**. The options are (a) give the test a corpus slice that
does not grow, or (b) pin the ranking budget rather than the result counts — but
a structural test for the budget already exists above it, and this one exists
because "the structural test can be satisfied by code that still changes the
answer." Not resolved unilaterally.

## D-02 ownership backfill — readiness

The named next unit. It is **much smaller than it looked**, and not the kind of
work it appeared to be.

### What the ownership field actually contains

The top 12 owner identities by work volume carry ~4,700 items between them.
**Every one is a customer contact** — roles "Decision Maker", "Billing Contact",
"IT Director", "Purchasing", on `@example.com`, `@gmail.com`, `@domain.ca`.

| | |
|---|---|
| `owners` rows | 45 — **5 eligible, 39 are customer contacts** |
| `assignable_identity` | **5 rows, 5 active** — the five executives, and that is the entire eligible population |
| Work-ownership states | ELIGIBLE 19 · INELIGIBLE 409 · UNRESOLVED 755 · COLLISION 9 · UNOWNED 182 |

D-02 is not "some records lack owners". **The owner field across the CRM is
populated with people at the customer, not staff.**

### There IS a staff layer, and it has never been granted

`employees` holds 21 rows, but 12 are AI agents (`role='agent'`) and 13 are
service accounts (`@system.internal`). The actual humans are **8**:

| | |
|---|---|
| Sales reps | 5 |
| Sales manager | 1 |
| Finance manager | 1 |
| Accounting clerk | 1 |

All active, all on `@emp.agentorc.ca` — and **none is eligible**, because
`fn_owner_eligible` requires membership in `assignable_identity`, which contains
only the five executives. One (Julia Martin) is an owner but still not eligible.

**So the backfill's first step is 8 grants, not a data repair.** That is the
whole reason the eligible rate is 0.42%: the people who do the work have never
been made eligible to own it.

### Dry-run of all eight grants (`apply=False`, nothing written)

| Staff | Role | Result |
|---|---|---|
| dlee, kpatel, ljones, snguyen | sales_rep | **PLAN OK** |
| rgarcia | sales_manager | **PLAN OK** |
| mchen | finance_manager | **PLAN OK** |
| sjohnson | accounting_clerk | **PLAN OK** |
| **jmartin** | sales_rep | **REFUSED — `identity_collision`**: this employee identifier is also an owner id (F1) |

**7 ready, 1 collision to resolve.**

### HUMAN ACTION REQUIRED — and why I stopped here

`grant_employee_owner` is the only path that may create an owner. It is
admin-invoked, dry-run by default, and refuses five named ways;
`test_K_N_no_code_path_here_ever_writes_an_owner` and
`test_K_O_eligibility_can_only_widen_through_an_explicit_grant` make that a
tested invariant. **A grant is an authorization act, not a repair**, so the
seven are listed rather than executed.

Note also that `created_by` on the affected records names the real staff
(Mike Chen, Julia Martin, Karen Patel…). That is a tempting mapping and the
grant path explicitly refuses it — `a grant must never derive an owner from
created_by`. Assignment is a second, separate decision after the grants.

### The sequence this unblocks

1. Grant the 7 (and resolve jmartin's collision) — **human**
2. `workflow_work_accountable` begins climbing from 0.42% on its own
3. Assign books of business — **human, separate decision**
4. When the rate is high, flip `WORKFLOW_OWNER_REQUIRED` — the gate becomes free

**Nothing in step 1 or 3 is mine to do.** That is the authorization boundary,
and it is where this unit stops.

---

# P1 Remediation — Unit 4

Owner decisions of this date: **grants APPROVED · jmartin HOLD ·
`WORKFLOW_OWNER_REQUIRED` OFF.** Applied to **LOCAL only**; Railway untouched.

## Decision 1 — the seven grants: APPLIED

All seven succeeded through `grant_employee_owner(apply=True)`. No work was
assigned; nothing was derived from `created_by`.

| | before | after |
|---|---|---|
| `assignable_identity` (active) | 5 | **12** |
| Employee-linked eligible owners | 0 | **7** |

### The finding the grants exposed

Every grant returned **`provenance=synthetic`**, and every new owner is
`eligible=True` **and `production_accountable=False`**.

`corpus_provenance` shows why, and it is not an inference or a default:

```
rule = human_attested · decided_by = attest:owner:2026-09-02
{"gate": "P5", "employee": "dlee", "attested_by": "owner", ...}
```

**All eight staff were explicitly attested SYNTHETIC by the owner on
2026-09-02.** Daniel Lee, Karen Patel, Robert Garcia and the rest are demo
personas. They are now eligible to hold work and none of them is a person who
can be held to account.

**So the production-accountable population is still exactly the five
executives.** This bears directly on Decision A of `decisions_2026-09-07.md`:
the objection that "authority and ownership are different concepts" is right in
principle, and at this business's actual scale there may be nobody else who
exists. **That is the next human decision** — see below.

## A claim of mine, corrected by the evidence

I wrote that `workflow_work_accountable` would "climb from 0.42% on its own"
after the grants. **The owner said not to rely on that, and was right.**
Measured after the grants:

```
accountable_rate: 0.0042  (unchanged)   with_accountable_owner: 7 of 1669
```

Granting changes the eligible **population**; it changes no activity's
`owner_id`. The rate cannot move until work is assigned or new work resolves
differently. The corrected sequence — **grant → resolve collision → assign books
→ measure → verify new writes → activate** — is the one now encoded.

## Decision 2 — jmartin: HELD, with the evidence a decision needs

Nothing merged, overwritten, duplicated, inferred or bypassed.

UUID **`a1451ad6-310c-4bcc-ba17-dd383a881ee8`** names two different people:

| Table | Identity |
|---|---|
| `employees` | **jmartin** · julia.martin@emp.agentorc.ca · sales_rep |
| `owners` | **John Smith** · john.smith@example.com · "Sales Representative" · `employee_uuid = NULL` · `is_synthetic = false` |

- **235 activities are owned by that uuid.**
- It is **not** a contact (0 rows) — so this is *not* a customer-side collision.
- The 2026-09-02 attestation says so explicitly: *"this uuid is also an owner_id
  naming a different person (F1); this attests the EMPLOYEE identity only and
  resolves nothing about the collision."*

**Reading the evidence supports (2) over (1):** an owner-space demo persona
("Sales Representative", `@example.com`) whose uuid was later reused as an
employee identifier. Its `is_synthetic = false` should carry little weight —
that same flag marks 176 of 181 seed-domain contacts as real.

**DECISION REQUIRED.** Resolving it re-attributes 235 activities, so it is a
governance decision and no grant may issue for jmartin until it is made.

## Decision 3 — `WORKFLOW_OWNER_REQUIRED`: still OFF, and now gated

The eight activation conditions are encoded in
`work_ownership.workflow_owner_activation_readiness()`:

```
may_enable: False   unmet: [2, 3, 4, 5, 6, 8]
  1 True   approved staff grants completed        7 employee-linked eligible owners
  2 False  identity collisions resolved           1 employee uuid also an owner id (F1)
  3 None   books of business assigned             HUMAN ATTESTATION REQUIRED
  4 False  workflow work resolves to an owner     last 24h: 9 created, 9 with no owner
  5 None   fail-closed fix deployed and verified  HUMAN ATTESTATION REQUIRED
  6 False  NO_OWNER_RECORDED creation stopped     9 in the last 24h
  7 True   measurement is the property            flow measured separately from debt
  8 None   post-deployment observation            HUMAN ATTESTATION REQUIRED
```

Three conditions are **human attestations reported as unknown rather than
guessed** — a program that infers "the books have been assigned" from row counts
invents the evidence the condition exists to require.

**The principle was preserved, not re-proxied.** Per instruction, readiness is
not an owner percentage, an eligible-owner count, a census, or a frozen test
output. `newly_created_work_accountability(hours)` measures the property on
**new work only** — *every newly created consequential work item has an
eligible, accountable owner* — and historical debt is reported separately by
`workflow_ownership_rate()`. A gated test refuses to let the flag go on while
the unmet set is non-empty, and refuses to call an empty observation window
"clean".

## Consequence: eight tests now fail on census pins the approved grants moved

`test_M0`, `test_M1`, `test_P1`, `test_Q0`–`test_Q4`.

Root cause: the suite hard-codes **`UNGRANTED_EMPLOYEE = "307cc6ac-…"`**, which
is **sjohnson** — one of the seven. The grant tests depend on a specific real
person never having been granted, and `test_M0`/`test_M1` pin absolute
`eligible` / `eligible_production` counts that the approved grants raised by 7.

**This is the fifth instance of the same pattern in this session**, and the
first where the moving population is one the owner deliberately changed.

**Deliberately NOT fixed in this unit.** `test_M0` and `test_M1` guard exactly
the property this unit surfaced — *eligible ≠ production-accountable* — and
re-pinning them at speed is how that invariant gets quietly weakened. The
correct fix is the recorded one: **give the grant fixtures a throwaway employee
subject** rather than consuming a real staff record, and convert the two count
pins to deltas. That is the next unit's first task, before anything else.

**No P0 control is affected**; the failures are confined to
`test_work_ownership.py`.

## The next human decision

> **Do real staff exist?** The eight "employees" are attested synthetic, so the
> production-accountable population is five executives. Either real people must
> be entered and attested, or the ownership model must map to the executives at
> this scale — in which case authority *is* ownership here, and that should be a
> recorded decision rather than a fallback nobody chose.

Every subsequent step — assigning books, condition 3, enabling the gate —
depends on that answer.

---

# P1 Remediation — Unit 5 · COMPLETION (Decision D)

**2,978 passing · 1 failing** (the confirmed-environmental retrieval pin).
`verify_invariants`: all hold. Applied to **LOCAL only**.

## The ownership defect is closed in code

`governance/sql/workflow_owner_resolution.sql` — **PENDING DEPLOYMENT**.

```
payload owner (if ELIGIBLE)
  -> declared entity-type routing -> authority role -> eligible owner
    -> nothing: refuse the write, record a durable exception
```

**The 153-line function body was carried over VERBATIM.** It was generated by
transforming the live definition rather than retyping it — three label mappings
and two trigger constraints in there were found by executing the code, not
reading it, and retyping is how that kind of knowledge gets silently dropped.
Only the owner resolution differs.

### Behaviour, measured against the real tables

| Case | Before | After |
|---|---|---|
| invoice, no payload owner | `owner_id = NULL` | **cfo@agentorc.ca** |
| invoice, owner is a **customer contact** | carried through | **rejected as ineligible, rerouted to the CFO** |
| invoice, owner already eligible | kept | **kept** (resolution must not override a good answer) |
| **unrouted** entity type | owner NULL, work created | **0 activities created**, `workflow_action_exceptions` row |

The second row is the one a NULL-check alone would have missed — it is the
1,407-activity pattern, fixed at the write path.

### Design points worth keeping

- **Routing is declared DATA** (`workflow_owner_routing`), not a CASE inside a
  function body — a governance declaration a human can read and change.
- **Eligibility is still the gate, not the routing table.** `fn_workflow_route_owner`
  joins through `fn_owner_eligible`, so revoking an executive stops work routing
  to them without anyone editing a second table. Pinned by `test_W5`.
- **An unlisted entity type resolves to nothing and fails closed.** A new entity
  type must be routed deliberately, never defaulted to somebody.
- **Fail-closed RETURNS, it does not RAISE.** Raising would abort the whole
  workflow rule; this file's own `send_notification` branch established the
  opposite convention so mixed rules still complete. One unresolvable action
  must not silently cancel its siblings.
- **`test_W7`** asserts on the resolver's own source that ownership is never
  derived from `created_by`, `contacts`, `updated_by` or a last actor.

Seven tests, `test_W1`–`test_W7`, each running the real function against the
real tables inside a rolled-back transaction.

## Activation condition 1 was rewritten, not satisfied

It read *"approved staff grants completed (≥ 7 employee-linked owners)"*. Those
grants were reversed under Decision C, so that condition became permanently
unmeetable — and meeting it would have meant putting synthetic personas back.

It is now the **property**: *every routed entity type resolves to an eligible
owner* — currently **8 of 8**, and met. This is the same substitution the whole
session keeps arriving at: the count was a proxy for the property, and it
stopped tracking it the moment the plan behind the count changed.

```
may_enable: False   unmet: [2, 3, 4, 5, 6, 8]
```

`WORKFLOW_OWNER_REQUIRED` remains **OFF**, correctly: the fix is not deployed
(condition 5), the jmartin collision is unresolved (2), and the flow window
still contains pre-fix rows (4, 6).

## P1 status

**Substantially complete.** The ownership defect is closed in code and covered
by tests that fail without the fix. What remains is deployment and production
verification — not further P1 design.

Deploy order, both files, in sequence:

1. `governance/sql/governance_decision_link_identity.sql` (N-01/N-02)
2. `governance/sql/workflow_owner_resolution.sql` (this unit)
3. the application
4. adversarial production verification
5. promote **both** files to `REQUIRED_MIGRATIONS` in the same change that
   records their Railway application, and update the two disposition counts

Neither is in `REQUIRED_MIGRATIONS`. `governance/` needs pushing with
`.governance-pin` before the public repo. Nothing committed, nothing deployed.
