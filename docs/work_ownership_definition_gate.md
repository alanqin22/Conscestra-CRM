# Stage 3 — Work Ownership Definition Gate

**Decision gate. No schema added, no data migrated, no owner backfilled, no
grant, no `assignable_identity` change, `STAFF_EMAIL_APPLY=0`, nothing
deployed.** Railway read-only, 2026-09-02T15:17Z.

Governing evidence: `work_ownership_gate.md`, `staff_email_authorization_gate.md`.

---

## 1. Executive decision

```
DECISION A — HUMAN OWNERSHIP REQUIRED
```

Tier-2 work requires an accountable human owner, **and the ownership contract
already exists.** It is `activities.owner_id`.

### This corrects the previous gate

Stage 2 concluded *"NOT READY — WORK OWNERSHIP MODEL MISSING"*. The model is not
missing. Stage 2 traced the notification and the entity and stopped there; it
never followed the **agent handlers**, which are where the product states its
accountability requirement.

Two factual errors in that report, both now corrected:

| Stage 2 said | Actually |
|---|---|
| `invoice.overdue` is structurally unownable — no owner column | **`invoices.owner_id` exists**, 1949/2053 populated, and is one of only four owner columns with a real FK. Stage 2 read `accounting_invoice_pipeline`, a view |
| No ownership object exists | **`activities` is the ownership object**, and every Tier-2 handler writes one |

**The verdict does not change, and that matters.** Re-running the corrected
classifier against Railway still gives `OWNED 0`. The problem was never that
ownership had nowhere to live. It is that what gets written into it is the
wrong identity space.

---

## 2. Product requirement evidence

Four independent, already-shipped assertions:

**1. Every Tier-2 event type has a registered agent handler, and every handler
writes an owned, due-dated task.**

```python
HANDLERS["invoice.overdue"]           = handle_invoice_overdue
HANDLERS["lead.scored"]               = handle_lead_scored
HANDLERS["activity.overdue_flagged"]  = handle_activity_overdue_flagged
```

```sql
-- _record_lead_outreach_sync
INSERT INTO activities (type, status, subject, description, due_at, …, owner_id, …)
VALUES ('call','open', 'Hot lead outreach – … (score …)',
        'Call within 4 hours while intent is high.',
        now() + interval '4 hours', …, %(owner)s, …)

-- _record_action_sync
INSERT INTO activities (type, status, subject, description, due_at, owner_id, …)
VALUES ('task','open', 'Payment reminder (…) – …', …,
        now() + interval '1 day', %(owner)s, …)
```

A due-dated open task with an owner and the instruction *"call within 4 hours"*
is an accountability claim. The system makes one on every Tier-2 occurrence.

**2. `data_readiness.py` ships a scored dimension literally named "Rep
accountability"**, and treats absent ownership as blocking it:

* *"{bad_pct}% of opportunities are unowned (**blocks rep accountability**)"*
* *"{bad_pct}% of orders are unowned (**blocks rep accountability** and any owner-scoped read policy)"*
* *"{bad} case(s) are in progress with no owner (**nobody is accountable for them**)"*
* *"**Who owns an order is a product decision**"*

**3. `routing.py`** — *"Recommends; never assigns… assignment stays reached by
an explicit human act."* An assignment model presupposes accountable humans.

**4. `identity_resolution_spec.md` §2** classifies `owner` as
*"role (assignment target); answers **who is accountable**; must survive a
person leaving."*

### The conditional refinement, already reasoned out

`data_readiness`'s `cases` check is explicitly conditional and its reasoning
generalises:

> *"DELIBERATELY CONDITIONAL. Raw case ownership is 26%… that number is not a
> defect: 487 cases are `status='new'`, and an untriaged case sitting in the
> queue with no owner is the CORRECT state… What is genuinely wrong is a case
> being WORKED with nobody working it."*

So the product rule is **ownership is required for work in a worked state, not
for work that merely exists.** This is the answer to "A or C": the rule is
conditional, and **no Tier-2 class escapes it**, because every Tier-2-derived
activity is created `status='open'` with a due date — born worked. Hence A.

---

## 3. Work semantics — the permanent vocabulary

| Term | Means | Stored |
|---|---|---|
| **subscribed** | wants to hear about this event type | `event_subscriptions.employee_uuid` |
| **notified / held** | was sent a copy | `notification_recipients.employee_uuid` |
| **owned** | is accountable for doing it | **`activities.owner_id`** |
| **assigned** | ownership set by an explicit act | `cases.py _mutate()` + field history |
| **executed** | did the work | an AI agent, via `HANDLERS` |

`notification_recipients.employee_uuid` means **"copied to", never "owned by"**,
and that boundary is now pinned by `test_D3`: the table has no owner column and
must not grow one.

---

## 4. Ownership decision

**The `activities` row is accountable, and its owner is the human named in
`activities.owner_id`.** The Tier-2 notification is an alert *about* that task
and carries no ownership of its own.

Rejections, per §3 of the gate:

| Candidate | Verdict |
|---|---|
| entity `owner_id` | **Not accountability itself** — it is the *source* the handler inherits from, and on this corpus it names customers. Rejected as the accountability record; retained as the (defective) input |
| `notification_recipients.employee_uuid` | **Rejected.** Fan-out. Meaning unchanged |
| event subscription | **Rejected.** Interest and routing, not responsibility |
| employee/owner identity spaces | **Not collapsed.** 0 deterministic links; intentional boundary (spec §2.2) |
| system actors / AI agents | **Execute** work and hold technical state; **never** the accountable owner. I11 and `staff_personhood()` unweakened |

---

## 5. The ownership contract

Existing, from the code that implements it:

| Question | Answer |
|---|---|
| Where does ownership live? | `activities` |
| Identifier | `activities.owner_id` |
| Identity space referenced | `owners` — **this is the defect** |
| Is it an FK? | **No.** `accounts`, `contacts`, `invoices`, `opportunities` have one; `leads`, `activities`, `orders`, `cases` do not |
| Mandatory? | Yes when `status IN ('open','in_progress','waiting')`; an untriaged item may be unowned |
| Assigned when? | At task creation by the agent handler, inherited from the entity |
| Can it change? | Yes — recommended by `routing.py`, set by an explicit human act |
| Audited? | `cases` keeps field history; `activities` does not |
| Owner becomes ineligible? | **Undefined today** — a gap |
| Ownership absent? | **Silently allowed today** — a gap |
| Multiple accountable? | No. One `owner_id` |
| Which of many notified is accountable? | None of them. The task's owner is, and the fan-out is irrelevant to it |
| Can a system actor own? | **Never** |

### The three things missing — none of them a new table

1. **An eligibility dimension.** `data_readiness` asks *is it set* (completeness)
   and *does it resolve* (integrity). **Nothing asks whether the owner is
   staff.** A task owned by a customer contact passes both checks today.
2. **`owners` conflates two populations** — 39 of 44 are customer contacts. Until
   staff ownership and relationship ownership are separable, propagating the
   entity owner will keep producing customer-owned tasks.
3. **No FK on `activities.owner_id`**, so a value resolving to nobody is
   possible and present.

---

## 6. Current population impact — not modified

Under DECISION A the 14 Railway items are **violations of an existing
contract**, not legitimate unowned work:

```
14 items → 0 OWNED · 13 UNOWNED (customer_contact) · 1 IDENTITY_UNRESOLVED (F1)
```

Corpus-wide, the same defect at scale:

| Agent-created task | total | owned | staff-owned | customer-owned |
|---|---|---|---|---|
| `invoice.overdue` dunning | 1,455 | 1,265 | **48** | **1,089** |
| `lead.scored` outreach | 121 | 103 | **5** | **98** |
| *all open activities* | 638 | 633 | **11** | **338** |

The contract is honoured *syntactically* on nearly every row and violated
*semantically* on ~85% of them. This is a **write-path defect against an
existing contract** — the same shape as the documented `orders.owner_id`
regression, and it must not be repaired by backfill for the same reason that
one must not.

---

## 7. `invoice.overdue` decision

```
(1) It legitimately REQUIRES human accountability.
```

`handle_invoice_overdue` already creates `activities(type='task', status='open',
due_at=now()+1 day, owner_id=invoices.owner_id)`. The product has already
decided; Stage 2 simply misread the schema. No `owner_id` is being added to
satisfy the digest — the column exists, is FK-constrained, and is 95%
populated.

---

## 8. System actors and personhood

Unchanged and unweakened. `dsar.staff_personhood()` remains authoritative:
**8 people, 13 service identities, 0 unclassifiable**, keyed on declared
`role='agent'` plus a named exception for `sysadmin` — never on the
`@system.internal` domain, which is a mutable attribute.

An AI agent **executes** work, holds transient technical state, and may
recommend an owner. It may never be the accountable owner. `sysadmin` — the
largest single holder of Tier-2 notifications — is a service account and is
therefore not a candidate owner at all.

---

## 9. Identity implications

| Area | Implication |
|---|---|
| `employees` | The 8 real humans are the only eligible owner population today |
| `owners` | Must become separable from customer contacts, or staff ownership needs its own space. **The blocking question** |
| **F1** | Untouched, unresolved, unmerged. The decision holds without resolving it: a colliding uuid is `IDENTITY_UNRESOLVED` under either reading |
| `executives.employee_uuid` | Still holds owner-space values under an employee-space name. Separate defect |
| provenance | Still blocks *merging*; does not block this decision |

**E6/E7 dependency classification** (§11 of the gate):

| Dependency | Status |
|---|---|
| E6 — ambiguous dispositions ratified | **Required later**, not now. This decision merges nothing |
| E7 — invariants executable on a rehearsal copy | **Required later**, before any migration |
| Full party migration | **Unrelated** to this decision |

---

## 10. Authorization implications

Email authorization remains a **separate future gate**. The sequence is
unchanged:

```
work ownership → human identity → recipient eligibility
              → communication authorization → delivery
```

`grant()` untouched, `assignable_identity` unchanged (4 rows),
`STAFF_EMAIL_APPLY=0`. **An owner existing does not make that owner emailable**,
and nothing in this gate moves that line.

---

## 11. Minimum implementation required *after* approval

Specified as design only. Nothing below was built.

1. **An eligibility check on ownership** — extend `data_readiness` with a
   dimension asking whether an owner is *staff*, alongside completeness and
   integrity. Cheapest, highest-value, no schema change. It would have surfaced
   this in the same report that already scored "Rep accountability".
2. **Separate staff ownership from relationship ownership** in `owners`, or give
   staff ownership its own space. The blocking design decision.
3. **Fix the write path** — handlers must refuse to propagate an ineligible
   owner rather than copying it. Refuse, do not substitute.
4. **FK on `activities.owner_id`** — only after (2), per invariant I10.
5. **A defined ineligible-owner transition** — what happens when an owner leaves.

Explicitly **not** required: a new ownership table, a party migration, an
employee↔owner mapping, or any backfill.

---

## 12. Deliberately unchanged

Production data · `assignable_identity` (4 rows) · `corpus_provenance` (12 rows)
· `grant()` · `STAFF_EMAIL_APPLY=0` · `ESCALATION_EMAIL=0` · Railway deployment
(`d251612b886c`) · all schema · F1 · `executives.employee_uuid` · retrieval code
· agent handler behaviour.

The only source edits were to `work_ownership.py`'s `_OWNER_SOURCES` — correcting
a factual error in my own diagnostic — and its tests.

---

## 13. Tests and evidence

**21 tests** in `test_work_ownership.py` (16 + 5 new contract tests); suite
**2704 passed, 1 failed, 1 skipped** (was 2699/1/1). Zero new failures.

**18 mutations verified.** Two survived first and both were tests passing for
the wrong reason:

| # | Mutation | Caught by |
|---|---|---|
| 16 | owner dropped from the lead-outreach INSERT | `test_D1` ← *initially SURVIVED* |
| 17 | task owner taken from the notification holder | `test_D2` |
| 18 | `lead.scored` handler unregistered | `test_D0` |

**Mutation 16 survived because `test_D1` asserted `"owner_id" in src`** — still
true after the column was deleted, because `ctx.get("owner_id")` remained
further down the function. It now parses the INSERT's **column list**. This is
the second wrong-reason test this programme has found (M13 was the first), and
both were found only by running the mutation, never by reading the test.

Production verification: `assignable_identity` 4 rows, digest ledger 0 rows,
`corpus_provenance` 12 rows, Railway on `d251612b886c`, all Railway access in
read-only transactions.

---

## 14. Remaining blockers

**Product decisions**

| # | Decision | Why it blocks |
|---|---|---|
| P1 | **Should `owners` hold staff, customers, or both?** | Every downstream repair depends on it. The spec says the present state *"describes the present, not the intent"* |
| P2 | What happens to open work when its owner becomes ineligible? | Undefined |
| P3 | Should the 8 `@emp.agentorc.ca` humans be the eligible owner population? | Determines the target of any repair |

**Engineering defects**

| # | Defect |
|---|---|
| E1 | Handlers propagate an ineligible owner (~85% customer-owned tasks) |
| E2 | No eligibility dimension in `data_readiness` |
| E3 | No FK on `activities.owner_id` (also `leads`, `orders`, `cases`) |
| E4 | `executives.employee_uuid` holds owner-space values |
| E5 | F1 collision unresolved |
| E6 | `test_recency_only_output_is_frozen` — see below |

---

## 15. Exact next gate

> **Owner Population Definition Gate** — decide P1: whether `owners` is a staff
> directory, a relationship directory, or both, and where staff accountability
> lives if it is not `owners`.

It is a single product decision, it blocks E1–E3, and it needs no code. Until
it is answered, any repair to the write path would be moving values between
populations whose meanings are not agreed.

**Do not** proceed to employee email authorization. Ownership now has a
defined contract, but the contract is violated on ~85% of live rows, and an
owner who is a customer is not a candidate recipient under any authorization
model.

---

## Appendix — retrieval defect (unchanged, untouched)

`test_hybrid_retrieval.py::test_recency_only_output_is_frozen` still fails on
the pre-gate tree: `payment reminder: 3`, pinned `4`, other three exact.
`recency_only` ranks a fixed 4,000-record recent slice of 13,201, so the pin is
corpus-size sensitive by construction. Not touched, not suppressed, not
re-pinned. Recommendation stands: pin against a fixed fixture corpus rather
than re-pinning numbers that will drift again.
