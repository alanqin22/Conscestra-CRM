# Stage 4 — Owner Population Definition Gate (P1)

**Decision gate. No implementation.** `owners` unmodified, no FK added, no
eligibility column, no backfill, no handler change, no grant,
`STAFF_EMAIL_APPLY=0`, F1 untouched, nothing deployed. Railway read-only,
2026-09-02T15:33Z.

Governing evidence: `work_ownership_definition_gate.md` (DECISION A —
accountability object is `activities.owner_id`).

---

## 1. P1 decision

```
P1-C — `owners` IS MIXED, WITH SEPARATE OWNER ELIGIBILITY
```

`owners` is a legacy FK target that mixes two populations by construction.
Work-ownership eligibility is a **separate, explicit concept** — and the system
has already built its first version.

This is decided on **recorded intent**, not on row counts. The row counts in §5
describe what the table contains; they are not evidence of what it is for.

### Why not P1-A

P1-A ("owners is the staff population; customers are not valid targets") is
superficially attractive and `data_readiness` even calls the owner a *"rep"*.
It fails on recorded intent:

> **D2, decided and recorded 2026-08-31:** *"an owner is a governance role
> assignment, **not an employee**. The assignee may be an employee, a
> contractor, an external consultant, a business owner, or an AI agent."*
> — `identity_resolution_spec.md` §2.1, ratified as entry criterion E2

The spec deliberately declines to equate `owners` with staff, and explicitly
instructs that `owners.employee_uuid` at 0/44 **must not be backfilled**.
Declaring `owners` a staff directory would overturn a ratified decision.

### Why C rather than B

B would require a discriminator *inside* `owners`. The recorded design puts it
outside, in a named object that already exists:

> *"`assignable_identity` is **the embryo of the role-assignment table**… Its
> `source` CHECK (`executive|employee|auth|manual|import`) needs `contractor`,
> `consultant` and `agent` added. It holds 4 rows, so **this is a design
> decision, not a migration**."* — spec §2.1

And the module that owns the question says the same thing in its first
paragraph:

> *"Who is allowed to receive CRM work? **Nothing in this database could answer
> that.** … So assignability is **EXPLICIT MEMBERSHIP, never inference**."*
> — `app/core/assignable.py`

The eligibility layer is not a thing to invent. It is a thing to finish.

---

## 2. Product evidence

| # | Evidence | Establishes |
|---|---|---|
| 1 | Spec §2.1 D2, ratified as E2 | owner = governance role assignment; assignee may be employee/contractor/consultant/business owner/AI agent. **Customer is not in that list** |
| 2 | Spec §2.1 | `assignable_identity` is the intended role-assignment table |
| 3 | `assignable.py` module docstring | `owners` cannot answer who may receive work; eligibility is explicit membership |
| 4 | `assignable.py` `identity_space()` | a customer contact is *"an outsider who **must not be routed work**"* |
| 5 | `assignable_identity.source` CHECK | `executive/employee/auth/manual/import` — **no customer class** |
| 6 | `owners.employee_uuid` FK `ON DELETE SET NULL` | an owner **survives** its employee — a role record, not a person record |
| 7 | `data_readiness.py` | the owner is a *"rep"*; unowned work "blocks rep accountability" |
| 8 | Spec §5 | never overwrite an identifier; a merge is a redirection, never a deletion |

Evidence 6 is the schema stating P1-C by itself: a table whose link to
`employees` is *optional and severable* is not an employee list, and a table
that keeps the row after the person leaves is a role record.

---

## 3. Definition of owner

> An **owner** is a governance role assignment: the record of who is
> accountable for a thing. It is not a person, not an employee, and not a
> mailbox. It survives the person leaving.

`owners` is the legacy storage for that record and is **also** the FK target of
relationship ownership on `accounts`, `contacts`, `invoices` and
`opportunities`. Those two uses were never separated, which is why the table
mixes populations.

---

## 4. Definition of eligible owner

Specification only — **not implemented by this gate.**

> An identity is an **eligible work owner** iff it is a declared natural
> person, is an active explicit member of the assignable population, and names
> exactly one identity.

| # | Question | Answer | Authority |
|---|---|---|---|
| 1 | Must the owner be human? | **Yes** | I11; `dsar.staff_personhood()` |
| 2 | Must they be an employee? | **No** | D2 — contractors and consultants qualify |
| 3 | May an executive be an owner? | **Yes** — the 4 today are | `assignable_identity.source='executive'` |
| 4 | May contractors? | **Yes**, once `source` gains the class | spec §2.1 |
| 5 | May former employees? | **Not for new assignment.** Existing rows preserved | `ON DELETE SET NULL`; spec §5. **Open — P2** |
| 6 | May system accounts? | **Never** | I11 |
| 7 | May AI agents? | **Never as accountable owner** — see the tension below | Stage 3; I11 |
| 8 | May customers/contacts? | **Never** | evidence 4, 5 |
| 9 | What makes an employee eligible? | **An explicit `grant()` — an admin act.** Not their domain, not their role, not their existence | `assignable.py`: "EXPLICIT MEMBERSHIP, never inference" |
| 10 | What makes a human ineligible? | No active membership; declared service identity; a colliding uuid; a customer-contact identity | `identity_space()`; `work_ownership._classify_owner()` |

### An unresolved tension that must be ratified, not papered over

D2 says an owner's assignee **may be an AI agent**. Stage 3 concluded an AI
agent may **never** be the accountable owner of Tier-2 work. Both can hold only
under this reading:

> an AI agent may hold an **assignment** (it executes the work) but may not
> hold **accountability** (a human answers for it).

That reading is consistent with everything shipped, but it is *my* reconciliation
of two recorded statements, not itself a recorded decision. **It needs
ratification** — listed as P4.

---

## 5. Current `owners` population — read-only, unmodified

```
44 rows, all is_active, all is_synthetic = false
```

| Class | n | `role` values it carries |
|---|---|---|
| customer_contact | **39** | Billing Contact, Support Contact, Technical Contact, Decision Maker, Purchasing, Operations, IT Director, VP Marketing, VP Sales, Sales Manager, Executive, CEO |
| eligible_today | **4** | CEO, CFO, COO, CRO |
| staff_candidate | **1** | Sales Representative — *this is the F1 uuid* |

The `role` column is the table describing its own mixture: *"Billing Contact"*
and *"Technical Contact"* are roles **at the customer's organisation**; *"CRO"*
and *"CFO"* are internal. Note the genuinely ambiguous middle — a customer's
own *"CEO"* or *"VP Sales"* reads exactly like an internal title, which is why
the discriminator must be the structural one (shared `contact_id`) and never
the role string.

`is_synthetic = false` on all 44 rows. Per the corpus-provenance work, real-vs-
synthetic **cannot be reconstructed** on this corpus, so that flag is a declared
value and not trustworthy evidence either way. It is reported, not relied on.

---

## 6. Current activity ownership — read-only, unmodified

| status | total | unowned | customer-owned | staff-owned | unresolved |
|---|---|---|---|---|---|
| completed | 11,998 | 1,801 | **9,520** | 343 | 146 |
| **open** | **638** | 5 | **338** | **11** | **272** |
| pending | 170 | 45 | 116 | 0 | 5 |

Tier-2 handler output, as previously observed and re-confirmed:

* `invoice.overdue` dunning tasks — **1,089 customer-owned vs 48 staff-owned**
* open activities — **338 customer-owned vs 11 staff-owned**

### A live invariant violation, found by this gate

```
20 activities are owned by a DECLARED SERVICE IDENTITY
```

`sysadmin` (`admin@system.internal`) is the accountable owner of 20 activity
rows. Under I11 and Stage 3's decision that is not a permissible state. Not
repaired here — reported as defect **E5**.

---

## 7. Impact on Tier-2 work

Unchanged: `14 items → 0 OWNED · 13 customer_contact · 1 identity_collision`.

P1-C does not make any Tier-2 item owned. It establishes **why** they are not,
and that the remedy is an eligibility contract plus a write-path fix — not a
new table and not a cleanup of `owners`.

Remediation magnitude, for planning only: **338 open + 116 pending
customer-owned activities** are the live surface; 9,520 completed rows are
history and, under spec §5, are preserved rather than rewritten.

---

## 8. Customer-owner behaviour (future E1 semantics)

**Recommended: `UNASSIGNED`.** Create the work; leave `owner_id` NULL; surface
it as a readiness finding requiring human assignment.

Evidenced rather than chosen for convenience:

* `routing.py` — *"Recommends; never assigns"*, and when its target is not
  grantable it **reports that** rather than falling back to someone arbitrary.
* `data_readiness` cases check — *"an untriaged case sitting in the queue with
  no owner is the CORRECT state"*. Unowned is an accepted state; **wrongly**
  owned is not.
* Stage 3 — handlers must **refuse, not substitute**.

Why not the others: `REFUSE` drops real work (a hot lead still needs calling);
`ROUTE` would auto-assign, which `routing.py` exists to prevent; `ESCALATE` has
no exception workflow for this and would rebuild the noise problem.

**This is a product decision and is recommended, not decided** — P3.

---

## 9. System / AI identity behaviour

Unchanged and unweakened. `staff_personhood()` remains authoritative: **8
people, 13 service identities, 0 unclassifiable**, keyed on declared
`role='agent'` plus a named exception for `sysadmin`, never on an email domain.

AI agents **execute** Tier-2 work — that is their designed role in
`agent_bus.HANDLERS`. They may never be the accountable owner. The 20
service-owned activities in §6 are a violation of this, not a counter-example
to it.

---

## 10. F1 implications

**Untouched. No identity selected, nothing merged, no alias.**

P1-C holds without resolving F1: under an explicit-membership model, a
colliding uuid is refused because it names two identities, not because of which
one it might be. The refusal is already implemented and mutation-verified
(`test_C7`, `test_C8`).

One observation for the eventual F1 decision, recorded and not acted on: the
`owners` side of the collision carries `role='Sales Representative'` — an
*internal* role. That makes it a genuine two-people-one-identifier case rather
than a customer/staff mix-up, and it does not change the disposition.

---

## 11–13. Required future contracts (specification only)

**E2 — eligibility contract** *(build first; it is the cheapest and unblocks the rest)*

> `is_eligible_owner(identity) -> bool` — true iff declared natural person
> **and** active explicit member **and** names exactly one identity.
> Fail-closed on unknown personhood. Never infers from domain, name, role
> string, or notification membership.

Plus a **third `data_readiness` dimension**. Today it asks *is it set*
(completeness) and *does it resolve* (integrity). A customer-owned task passes
both. The missing question is *is the owner eligible*.

**E1 — handler contract** *(after E2)*

> A handler receiving an ineligible entity owner writes the activity with
> `owner_id = NULL` and records why. It never substitutes, never falls back to
> the entity's account owner, never uses the notification holder.

**E3 — integrity contract** *(last, per invariant I10)*

> FK on `activities.owner_id` **only after** the population it must reference
> is agreed and repaired. A rejected row is a finding, never a reason to drop
> the constraint.

The ordering is load-bearing: an FK added first would enforce membership of a
population that still mixes customers with staff — it would make 338 open
customer-owned activities *valid*.

---

## 14. Employee-email implications

None. The layering is unchanged and the separation is the point:

```
identity → ownership → owner eligibility → communication eligibility
        → explicit authorization → delivery
```

An eligible work owner is **not** an authorized email recipient. Today's four
`assignable_identity` members are eligible owners **and** carry
`auto_email_enabled=false` — the two concepts already differ in production
data, which is the cleanest possible demonstration that they are separate.

---

## 15. Changes made

| Change | Kind |
|---|---|
| `test_E0` — assignable source classes exclude customers | invariant guard |
| `test_E1` — membership in `owners` alone never confers work ownership | invariant guard |

Both protect invariants that were established before this gate. Neither encodes
the P1 decision, and `test_E1` is written to hold under **every** P1 outcome.

## 16. Changes deliberately not made

`owners` (unmodified: no deletion, migration, merge, rename, eligibility column
or FK) · `activities.owner_id` · handlers · `grant()` · `assignable_identity`
(4 rows) · `STAFF_EMAIL_APPLY=0` · `ESCALATION_EMAIL=0` · F1 ·
`executives.employee_uuid` · `corpus_provenance` (12 rows) · retrieval code ·
Railway deployment (`d251612b886c`).

No E1/E2/E3 implementation. No data remediation.

---

## 17. Tests and evidence

23 tests in `test_work_ownership.py`; suite **2706 passed, 1 failed, 1
skipped** (was 2704/1/1). Zero new failures.

**19 mutations verified** across the programme. Mutation 19 (reclassifying a
customer contact as OWNED) is caught by `test_C4` **and** `test_E1` — the
double catch is deliberate: it is the single edit that would make the ownership
numbers look solved.

Two mutations in this programme survived their first run, both because a test
asserted a substring rather than a behaviour (M13 patched the wrapper it was
testing; M16 matched `owner_id` in a different line). Both are fixed and
re-verified. The discipline that found them — run the mutation, do not read the
test — remains mandatory.

Production verification: `owners` 44 rows, `assignable_identity` 4,
`corpus_provenance` 12, digest ledger 0, Railway `d251612b886c`, all Railway
access in read-only transactions.

---

## 18. Exact next gate

> **E2 — Owner Eligibility Contract Gate.** Implement `is_eligible_owner()` and
> add the eligibility dimension to `data_readiness`. Read-side only: it
> measures and refuses, it assigns nothing and repairs nothing.

It is the cheapest step, it is fully specified by §4 above, and it converts
every downstream question from an argument into a number. It also needs no
product decision — §4's contract follows from already-ratified rules.

### Open product decisions (none block E2)

| # | Decision |
|---|---|
| P2 | What happens to open work when its owner becomes ineligible (leaver policy) |
| P3 | Ratify `UNASSIGNED` as the invalid-owner behaviour (§8) |
| P4 | Ratify that an AI agent may hold an assignment but never accountability (§4) |
| P5 | Should the 8 `@emp.agentorc.ca` humans be granted? **A per-person admin act, deliberately out of scope** |

On P5: the eight humans hold real business roles (`sales_rep`, `sales_manager`,
`accounting_clerk`, `finance_manager`) and own real activities. That makes them
**candidates**. It does not make them eligible — under `assignable.py`'s own
rule, eligibility is an explicit grant and nothing else, and inferring it from
"they are human and they hold work" is precisely the inference the module was
written to prevent.

---

## Appendix — retrieval defect (unchanged, untouched)

`test_recency_only_output_is_frozen` still fails identically on the pre-gate
tree. `recency_only` ranks a fixed 4,000-record slice of 13,201, so the pin is
corpus-size sensitive by construction. Not touched, not suppressed, not
re-pinned.
