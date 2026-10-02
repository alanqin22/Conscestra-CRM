# Personhood Domain Contract Gate

**DRAFT FOR REVIEW. Design contract, no implementation.** No SQL is edited by this
document, no `owner_personhood` row is inserted, no disposition changes, nothing is
deployed. Railway untouched. The certification predicate is to be derived from the
invariant recorded here once this contract is accepted, and not before.

Governing evidence: `owner_population_definition_gate.md` (P1-C),
`owner_eligibility_contract_gate.md` (E2-READY), `identity_resolution_spec.md` §2.1 (D2,
ratified as E2), `app/core/assignable.py`, `app/core/dsar.py`.

---

## 1. Why this document exists

`owner_personhood_register.sql` is authored and declared `_PENDING_OWNER_PERSONHOOD`, and
it carries two incompatible readings of its own subject.

Its header describes it as replacing `dsar.staff_personhood()` and the
`SERVICE_IDENTITY_ROLES` / `SERVICE_IDENTITY_EXCEPTIONS` constants, which enumerate
`employees`. That is a staff register. Its `subject_id` column is then deliberately not a
foreign key, so that the register "must be able to classify an identity that appears in
employees, in owners, in both, or in neither". That is an owner-principal register.

The consequence is measurable rather than theoretical. `fn_personhood_roster_certified()`
enumerates `employees`, while `fn_owner_eligibility_state()` applies the classification to
owner identities. Certifying the staff roster therefore establishes nothing about the
subject the eligibility contract asks about, and every undeclared owner is refused as
`INELIGIBLE_NOT_HUMAN` under a register the system reports as certified. A fail-closed
control that measures a different population than the one it gates reads as satisfied
while the property it exists to establish is unproven.

---

## 2. Domain

`owner_personhood` classifies the personhood of **role-assignment principals**, the
population represented by `assignable_identity`.

It is not a staff register, not a directory of natural persons in general, and not a
classification of `owners`. `owners` is a legacy foreign-key target that mixes populations
by construction (P1-C) and is a downstream representation rather than an authoritative
population.

## 3. Upstream source

`employees` is an authoritative source of **employee** personhood. A declaration held
against an employee identity may be carried into a role-assignment principal minted for
the same person, and `assignable.provision_owner` already performs that carry.

`employees` is not the certification universe. It is one source of declarations for one
class of principal. Principals who are not employees — executives, contractors,
consultants, business owners, service principals — acquire declarations by their own
evidence, not by an employee record they do not have.

## 4. Permitted principal classes

Recorded intent, D2 decided 2026-08-31 and ratified as E2:

> "an owner is a governance role assignment, **not an employee**. The assignee may be an
> employee, a contractor, an external consultant, a business owner, or an AI agent."

Two consequences follow, and both are load-bearing:

* `service` is a legitimate classification for a role-assignment principal, because AI
  agents are permitted assignees. The register is therefore a two-valued classification
  and not a person flag.
* **customer contacts are not assignable owners.** `assignable.identity_space()` calls a
  customer contact "an outsider who must not be routed work", and
  `assignable_identity.source` carries no customer class. The 39 owner rows that are
  contact identities are a recorded defect in `owners`, not a population this register
  governs.

  Stated positively, because the negative form invites a backfill: **those 39 owners are
  not missing personhood declarations.** They are not owner principals established by the
  role-assignment mechanism, so no declaration is owed for them and their absence from the
  register is the correct state rather than an incompleteness to repair. This is also what
  keeps the register from becoming a universal registry of human beings: it explains the
  present `INELIGIBLE_NOT_HUMAN` outcome for a customer owner without requiring every
  natural person in the database to be declared.

## 5. Identity boundary

Personhood attaches to the principal identity being classified. It is never inferred
from:

* membership of `owners`, which proves a foreign-key target and nothing about a person;
* membership of `contacts`, which establishes the opposite;
* address or name similarity between `employees` and `owners`. `dsar.py` states the
  prohibition and the reason: the two sets share exactly one name and zero addresses, so
  a join on either "would invent links that do not exist", and populating
  `owners.employee_uuid` "is a data decision for the product owner".

One exception is already in force and should be recorded rather than discovered later.
`provision_owner` carries a declaration across an employee-to-principal mapping keyed on
the address. That is defensible because the address is the subject of a human granting
act recorded in `assignable_identity`, not an inferred join — but it does key a personhood
declaration on a mutable attribute, which is the key class the identity spec prohibits.
The carry is conditional and fails closed: it writes nothing unless the employee identity
holds a current `person` declaration.

## 6. The certification denominator

This is the point the contract exists to fix, and it is derived rather than chosen.

| # | Established fact | Source |
|---|---|---|
| 1 | A declaration is held against `subject_id uuid` | `owner_personhood` schema |
| 2 | Explicit membership is **an `assignable_identity` row** | E2 gate §4–6 |
| 3 | Membership keys on `lower(email)`; **`owner_id` is optional metadata** | E2 gate §4–6, `uq_assignable_email` |
| 4 | Eligibility is answerable only for an identity present in `employees` or `owners` | precedence step 3, `IDENTITY_UNRESOLVED` |
| 5 | Active means `assignable_identity.is_active`, and nothing else | E2 gate §4–6 |
| 6 | A membership with `owner_id` NULL "cannot receive work", and minting "must be an explicit decision rather than a side effect" | `assignable.provision_owner` docstring |
| 7 | There is no unique index on `assignable_identity.owner_id`, only a partial plain one, so two active memberships may name one owner | E2 gate §4–6, `test_F7` |

### The population is a set of identities, not a set of rows

A declaration is held against a uuid (fact 1), and membership is established by a row
(fact 2). Those are different objects, and fact 7 makes the difference reachable: two
active memberships may name one `owner_id`.

> **Certification population.** The set of **distinct active minted owner principals**,
> represented by `assignable_identity.owner_id`. Each such principal must carry exactly
> one applicable personhood declaration.
>
> Multiple active memberships naming the same owner do **not** create multiple personhood
> subjects. They constitute an ambiguity in the role-assignment layer and must prevent
> certification (§7).

`assignable_identity` therefore remains the source that establishes membership, while the
semantic object being classified is the owner principal:

    assignable_identity rows
            -> distinct active minted owner_id principals
                    -> personhood declaration

and never a declaration per membership row.

### Active unminted memberships

E2 settles this rather than leaving it open. `provision_owner` records that granting
assignability "produces a membership with owner_id NULL, which cannot receive work — so
somebody has to mint the owner row, and it must be an explicit decision rather than a side
effect of the first case that needs an owner." An unminted membership is therefore an
**incomplete provisioning state** in a deliberate two-step lifecycle, not a defect and not
a certification failure.

> **Unminted memberships are outside the certification population.** No uuid subject
> exists, so no declaration can legitimately attach. They must not, however, allow
> certification to succeed silently: the certification and reporting interface must expose
> their count, so an incompletely provisioned population is visible rather than merely
> absent from every measurement.

### The population is not a constant

Measured on 2026-09-30 after the probe-residue cleanup the population is **12** — seven
principals of `source='employee'` and five of `source='executive'` — with zero active
unminted memberships. Before the cleanup the table also held 41 probe memberships, 35 of
them unminted, which is the shape facts 3, 4 and 6 exclude.

**That 12 is a measurement, not an invariant, and must not appear as an expected
cardinality in the contract, the predicate or a test.** A control that compares a
population to a constant stops measuring the property as soon as the population
legitimately changes. The invariant must hold at any population size.

## 7. Certification invariant

> **Certification holds only when every distinct active minted owner principal is
> established by exactly one active membership and carries exactly one current personhood
> declaration.**

Four properties, stated so the mechanism stays free:

**1. Population** — the distinct active minted owner principals of §6. Not membership
rows, and not `employees`.

**2. Completeness** — no principal in the population lacks a current personhood
declaration, and none holds more than one. A second current declaration for one subject
is an ambiguity about what the register says, and the register is effective-dated
precisely so that a classification is superseded rather than duplicated.

**3. Unambiguity of the mapping** — each principal in the population is established by
exactly one active membership. This is a certification term in its own right, not a
remark, because the mapping from membership to personhood subject can itself be
ambiguous:

| active memberships naming one `owner_id` | consequence |
|---|---|
| 0 | not in the population; the principal is not established by this mechanism |
| 1 | in the population and certifiable |
| more than 1 | **certification fails** — the role-assignment layer does not identify a single subject |

Without this term the predicate could report a fully certified register while the mapping
that selects its subjects is ambiguous. The failure is reachable rather than theoretical:
fact 7 of §6 records that no unique index prevents it, and `test_F7` pins it.

**4. Visibility** — active unminted memberships are counted and reported separately, and
cannot be mistaken for certified principals. Certification must not read as satisfied
merely because an incompletely provisioned membership fell outside the population.

**Fails closed.** Certification false refuses every identity rather than answering from an
incomplete register, reproducing the existing condition in `dsar.staff_personhood()` and
pinned by `test_F9`.

**Employee declarations are a prerequisite of the carry mechanism, not a term of the
invariant.** Principals of `source='employee'` acquire their declaration by carry, so the
corresponding employee declarations must exist for the carry to write anything — but an
employee who is not a principal is not part of what certification proves.

No SQL is prescribed here, and no expected cardinality forms part of any property. The
predicate is to be derived from these four properties.

## 8. Absence semantics

The register admits two classifications, `person` and `service`, and `rationale` is
`NOT NULL`. **There is no `UNCLASSIFIED` value.** An identity is unclassified by the
absence of a current row, which makes "record nothing" the only representation of "no
evidence" and prevents an inferred category from being stored as a decision.

## 9. Collision semantics

An identity present in both `employees` and `owners` is an identity-resolution failure,
reported as `IDENTITY_COLLISION` at precedence step 2 — before personhood at step 6.

A collision is not repaired by classification, and a perfectly populated register would
not make a colliding identity eligible. It belongs to the identity-resolution contract.
One such row exists today.

## 10. Test and probe identities

Verification artifacts are not part of the authoritative population. Forty-one
`prov-probe-*` memberships and six corresponding owner rows were removed on 2026-09-30
after an exhaustive reference census, with a pre-delete snapshot retained.

Their existence as owners never constituted evidence of personhood, and a register
populated from "whatever holds a grant" would have classified them.

## 11. No implicit classifications

A principal's existence does not establish its classification. Contractors, consultants,
business owners and service principals each require explicit evidence, and the absence of
evidence is recorded by the absence of a row (§8).

In particular, a principal must not be classified `service` merely because the owner model
permits service principals, nor `person` merely because it holds a grant. `grant()`
enforces nothing — it accepts a customer contact, a service account and a colliding uuid
alike — so membership cannot stand in for evidence of anything.

## 12. Certification and eligibility are separate

Certification establishes the **completeness** of the register over the denominator.

Eligibility independently applies a subject's classification, and its precedence is
unchanged by this contract: identity resolution first, then personhood, then customer
identity, then explicit grant, then active grant. Personhood remains a conjunct that
re-checks what `grant()` does not.

A consequence to record explicitly: because personhood precedes customer identity,
`INELIGIBLE_CUSTOMER_IDENTITY` is unreachable for a customer owner that holds no
declaration. Both outcomes are refusals and the boundary is not weakened, but the reported
ground differs from the one `test_F2` asserts. Whether the precedence or the test should
change is outside this contract and is not resolved here.

---

## Open questions this contract does not decide

1. Whether `test_F2`'s expectation or the personhood/customer precedence should change
   (§12). E4 already records the decision boundary around whether `grant()` should itself
   refuse ineligible assignments, which is where that tension is properly settled.
3. Whether `grant()` should itself refuse ineligible grants — already recorded as **E4**.
4. Whether `owners.employee_uuid` is populated, which remains a data decision for the
   product owner.
5. The classification evidence for each of the twelve principals, which is the subject of
   a separate data-authorization gate and is not established here.

## Status

**DRAFT — awaiting review.** On acceptance: derive the certification predicate from §6 and
§7, construct it read-only against `owner_personhood_register.sql`, re-baseline, and only
then run the Ownership implementation gate. No personhood row is inserted at any point in
this sequence.
