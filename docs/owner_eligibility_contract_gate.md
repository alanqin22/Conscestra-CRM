# E2 — Owner Eligibility Contract Gate

**Read-side contract definition and verification.** No handler change, no FK,
no grant, no data repair, no schema change, `STAFF_EMAIL_APPLY=0`, F1
unresolved, Railway on `d251612b886c`, all production access read-only.
Measured 2026-09-02T15:48Z.

Governing decision: **P1-C** — `owners` is a mixed population; eligibility is a
separate explicit layer.

---

## 1. E2 verdict

```
E2-READY
```

The predicate is deterministic, total, fail-closed, and evidence-backed. Every
candidate in both live populations receives exactly one classification, and
`ELIGIBLE` is reachable only by passing all six prior checks.

**P4 does not block this.** See §12: it affects 12 identities that hold zero
work and zero grants, and every candidate that actually exists today
classifies identically under either interpretation.

---

## 2. Eligibility definition

> An identity is an **eligible work owner** iff it is a declared natural
> person, names exactly one identity, is not a customer, and holds an active
> explicit membership.

### The finding that shaped the whole contract

The obvious predicate is *"has an active `assignable_identity` row"*. It is
**necessary and not sufficient**, because `grant()` enforces nothing:

```python
grant(email, owner_id=…)
  validates:  '@' in email;  owner_id parses as a uuid
  enforces:   nothing else
```

It will accept a customer contact, a service account and a colliding uuid
alike. A membership-only predicate would therefore inherit every gap in the
primitive that creates memberships. **The predicate re-checks what `grant()`
does not** — that is why personhood, customer-identity and single-identity are
conjuncts rather than assumptions.

---

## 3. Eligibility state machine

Seven states, evaluated in strict precedence, first match final:

| # | State | Meaning |
|---|---|---|
| 1 | `IDENTITY_COLLISION` | names two people; nothing later can be answered honestly |
| 2 | `IDENTITY_UNRESOLVED` | not a uuid, names nobody, or >1 active membership |
| 3 | `INELIGIBLE_NOT_HUMAN` | declared service identity, or personhood uncertifiable |
| 4 | `INELIGIBLE_CUSTOMER_IDENTITY` | customer contact (shared `contact_id`) |
| 5 | `INELIGIBLE_NOT_EXPLICITLY_GRANTED` | no membership record |
| 6 | `INELIGIBLE_NOT_ACTIVE` | membership exists, revoked |
| 7 | `ELIGIBLE` | passed all six |

`INELIGIBLE_CUSTOMER_IDENTITY` is **added beyond the required minimum**, and it
is placed *ahead of* the grant checks deliberately: since `grant()` cannot
exclude a customer, a granted customer must still be refused — and the reason
must say "customer", not the incidental "not granted".

Precedence is exported as `ELIGIBILITY_PRECEDENCE` and asserted by `test_F0`.
Ordering that lives only in the sequence of `if`s cannot be reviewed.

---

## 4–6. The three contracts

**Identity / personhood.** `dsar.staff_personhood()` remains authoritative: 8
people, 13 services, 0 unclassifiable, keyed on declared `role='agent'` plus a
named exception for `sysadmin` — never on `@system.internal`, a mutable
attribute. Uncertifiable personhood refuses **everyone**, including otherwise
eligible members (`test_F9`).

**Active.** `assignable_identity.is_active`. Nothing else — not recent work,
not login, not notification presence, not the existence of an address.
`revoke()` sets the flag and keeps the row, so *"was eligible once"* and *"is
eligible now"* stay distinguishable and the second never inherits from the
first.

**Explicit membership.** An `assignable_identity` row. Verified `grant()`
semantics:

| Question | Answer |
|---|---|
| What does it authorize? | Membership in the assignable population — **not** email |
| What does it key? | **`lower(email)`** — `uq_assignable_email`. `owner_id` is optional metadata |
| Reversible? | Yes, `revoke()`; row retained |
| Revocation immediate? | Yes — every read filters `AND is_active` |
| Can a customer be granted? | **Yes — no check** |
| Can a system identity? | **Yes — no check** |
| Can a colliding identity? | **Yes — no check** |
| Does it enforce eligibility? | **No** |
| Ownership vs email authorization distinct? | Yes — `auto_email_enabled` is separate and defaults false |

**Two structural findings.** `grant()` keys on a **mutable attribute** — the
address — which is the key class the spec prohibits for identity. And there is
**no unique index on `owner_id`**, only a partial plain one, so two active
memberships may name one owner. That makes "exactly one identity" a reachable
failure, not a formality (`test_F7`).

**Conclusion: `grant()` is necessary but not sufficient.** It stays unchanged;
the predicate compensates. Whether it should itself refuse ineligible grants is
a future contract change, recorded as **E4**.

---

## 7. The 44 owners, through the predicate

```
owners_total 44
  ELIGIBLE                       4     the executives
  INELIGIBLE_CUSTOMER_IDENTITY  39
  IDENTITY_COLLISION             1     a1451ad6…
```

Identical on local and Railway. Sums exactly; no residue.

---

## 8. Activities, through the predicate

**Open — 638**

| | n |
|---|---|
| `INELIGIBLE_CUSTOMER_IDENTITY` | **338** |
| `IDENTITY_UNRESOLVED` | **272** |
| `ELIGIBLE` | **12** |
| `IDENTITY_COLLISION` | 11 |
| unowned (NULL) | 5 |

**All — 12,806**

| | n |
|---|---|
| `INELIGIBLE_CUSTOMER_IDENTITY` | **9,974** |
| unowned (NULL) | 1,851 |
| `IDENTITY_UNRESOLVED` | 423 |
| `IDENTITY_COLLISION` | 251 |
| `ELIGIBLE` | 204 |
| `INELIGIBLE_NOT_EXPLICITLY_GRANTED` | 83 |
| `INELIGIBLE_NOT_HUMAN` | **20** |

Both partition exactly (`test_F11`).

**This reconciles the previous gate's numbers and explains its residue.** Stage 4
counted "11 staff-owned" open activities by asking *is the owner in
`employees`*; the predicate reclassifies those 11 as `IDENTITY_COLLISION` (they
are jmartin's) and separately identifies **12** `ELIGIBLE` — the executives,
which the cruder count had left unaccounted for. 5 + 338 + 11 + 272 + 12 = 638.

The 83 `INELIGIBLE_NOT_EXPLICITLY_GRANTED` are precisely the six real employees
who hold work and hold no membership (rgarcia 39, kpatel 17, dlee 14,
sjohnson 9, ljones 2, mchen 2). That number is the exact cost of the
explicit-grant rule, and it is the right cost.

---

## 9. The 20 `sysadmin`-owned activities — NOT REPAIRED

The predicate rediscovers them independently as `INELIGIBLE_NOT_HUMAN`.

| Property | Value |
|---|---|
| Owner | `25eaf35e…` — `admin@system.internal`, declared service account |
| Count | 20 |
| Type / status | `task` / **all `completed`** |
| Related | `invoice`, all 20 |
| Created | **2026-02-16 → 2026-02-25** — a 10-day window, seven months ago |
| Subjects | *"Confirm receipt – Invoice INV-000xxx"*, *"Invoice INV-000xxx issued – confirm payment"* |
| Tier-2 handler output? | **No** — the Tier-2 dunning subject is *"Payment reminder (…)"*. Different shape, different path |
| Open work affected | **None** |

**Assessment: a historical defect, not a live leak.** No current write path
produces it — the window closed in February, and **zero** activities are owned
by any AI-agent uuid. This materially lowers its urgency relative to the
customer-ownership defect, which is ongoing.

Not repaired, not reassigned, not nulled. Recorded as **E5** for a later
remediation gate.

---

## 10. Customer-owner boundary

`test_F2` proves a customer-contact owner is refused despite resolving, sitting
in `owners`, being referenced by live records, carrying a role that may read
*"CEO"*, having an address, and being active. The discriminator is the
**shared primary key** (`contacts.contact_id = owner_id`) — structural, not the
role string, which is ambiguous by construction: a customer's own *"CEO"* is
indistinguishable from an internal one.

Scale: **9,974** activities and **1,089 of 1,265** dunning tasks are owned by
this class.

---

## 11. F1

Hard-fail, unresolved, untouched. `IDENTITY_COLLISION` is precedence rank 1 —
ahead of personhood, customer-ness and membership — because every later
question would be answered about one of two people, chosen by the order of the
checks rather than by evidence. No heuristic resolution: not first-match, not
employee, not owner, not email, not name. That uuid owns **251** activities.

---

## 12. D2 / P4 — not silently reconciled

D2 permits an assignee to be an AI agent. Stage 3 said agents may never be
accountable. **This gate does not decide between them.**

`test_F4` asserts a **refusal, not a ruling**: an identity whose accountability
status has never been ratified cannot be *certified* eligible. Fail-closed is
the only defensible position while the question is open, and it is not the same
statement as "agents may never be accountable".

**Why this does not block E2-READY:** P4 affects the 12 AI-agent identities.
They own **0** activities and hold **0** grants. `sysadmin` is unaffected — it
is a service account, not an AI agent, and is refused under both
interpretations. If P4 later ratifies Interpretation B, exactly one conjunct
changes and every affected candidate is enumerable.

Recorded as **P4 — unresolved product/governance decision**.

---

## 13–15. Downstream contracts

**E1 (handler enforcement)** — specified, not built:

```
handler receives candidate owner
   → eligibility(candidate)
       ELIGIBLE      → create the accountable activity
       anything else → do NOT substitute, do NOT fall back to the account
                       owner, do NOT use the notification holder
                     → apply the P3-approved transition
```

**P3 dependency** — the transition remains `UNASSIGNED` (create with
`owner_id = NULL`, surface it) as a **recommendation, not a settled
requirement**. E1 cannot be built until P3 is ratified.

**E3 (FK)** — still last. An FK added now would enforce membership of the mixed
`owners` population and would make all 338 open customer-owned activities
structurally *valid* while remaining semantically invalid.

---

## 16. Employee-email status

Completely unchanged and out of scope. `STAFF_EMAIL_APPLY=0`,
`ESCALATION_EMAIL=0`, `assignable_identity` 4 rows, no grants, no digest
routing change. An eligible owner is still not an authorized recipient — and
the four eligible owners carry `auto_email_enabled=false`, so the two concepts
differ in production data today.

---

## 17. Changes made

| Change | Kind |
|---|---|
| `eligibility()`, `is_eligible_owner()` — the predicate | read-side |
| `_eligibility_facts()` — one batched read | read-side |
| `owner_population()`, `activity_ownership()` — classification reports | read-side |
| `ELIGIBILITY_PRECEDENCE` — the ordering, exported and asserted | contract |
| 15 tests (`test_F0`–`test_F13`) | tests |

Nothing writes. Nothing is called from a handler, a router or a send path.

## 18. Changes deliberately not made

`grant()` · `assignable_identity` (4 rows) · `owners` (44 rows) ·
`activities.owner_id` · handlers · FK · the 20 `sysadmin` rows · the 9,974
customer-owned rows · F1 · `executives.employee_uuid` · `corpus_provenance`
(12 rows) · `STAFF_EMAIL_APPLY=0` · retrieval code · Railway deployment.

No employee grants. P5 remains separate.

---

## 19. Tests and mutation results

38 tests in `test_work_ownership.py`; suite **2721 passed, 1 failed, 1
skipped** (was 2706/1/1). Zero new failures.

**Nine E2 mutations, all caught.** Every test calls the real predicate against
real rows; none patches `eligibility()` and then asserts about the patch.

| # | Mutation | Caught by |
|---|---|---|
| 20 | customer owner classified ELIGIBLE | `test_F2` |
| 21 | personhood check removed | `test_F3`, `test_F4` |
| 22 | employee existence treated as sufficient | `test_F5` |
| 23 | collision treated as eligible | `test_F6` |
| 24 | multiple identities accepted | `test_F7` |
| 25 | uncertifiable personhood fails OPEN | `test_F9` |
| 26 | predicate bypassed for non-uuid input | `test_F10b` ← *initially SURVIVED* |
| 27 | `is_eligible_owner` short-circuits to True | `test_F2/F3/F6/F9/F10` (5) |
| 28 | revoked membership still eligible | `test_F8` |

**Mutation 26 survived its first run, for an interesting reason.** Deleting the
uuid guard does not change the *answer*: junk falls through to
`unnest(%s::uuid[])`, Postgres errors, `_rows` swallows it, and empty facts
produce `IDENTITY_UNRESOLVED` anyway. **Safe by accident, through an exception
nobody intended.** A test asserting only the state cannot tell the two apart, so
`test_F10b` breaks `_rows` and asserts the refusal happens *before* the
database is touched.

That is the third wrong-reason test this programme has found (M13 patched its
own subject; M16 matched a substring; M26 asserted an outcome that two
different mechanisms produce). All three were found by running the mutation,
never by reading the test.

---

## 20. Production verification

```
owners                44 (unchanged)      assignable_identity   4 (unchanged)
corpus_provenance     12 (unchanged)      digest ledger rows    0
STAFF_EMAIL_APPLY      0                  ESCALATION_EMAIL      0
Railway commit         d251612b886c       Railway access        read-only
```

---

## 21. Remaining blockers

**Product decisions**

| # | Decision | Blocks |
|---|---|---|
| **P3** | Ratify `UNASSIGNED` as the ineligible-owner transition | **E1** |
| P2 | Leaver policy — open work when an owner becomes ineligible | E1 edge case |
| P4 | AI agent: assignment vs accountability | nothing today (0 work, 0 grants) |
| P5 | Grant the 8 human employees | 83 activities' eligibility |

**Engineering defects**

| # | Defect | Scale |
|---|---|---|
| E1 | Handlers propagate ineligible owners | ongoing |
| E3 | No FK on `activities.owner_id` | — |
| E4 | **`grant()` enforces no eligibility** — new this gate | latent |
| E5 | 20 `sysadmin`-owned activities (I11) | historical, closed window |
| E6 | 9,974 customer-owned + 423 unresolved + 251 collision activities | remediation gate |
| E7 | `executives.employee_uuid` holds owner-space values | — |
| E8 | `test_recency_only_output_is_frozen` — corpus-size-sensitive pin | unrelated |

---

## 22. Exact next gate

> **P3 — Ineligible-Owner Transition Gate.** A single product decision: what a
> handler does when the candidate owner is not eligible.

It is the only thing standing between E2 and E1. `UNASSIGNED` is recommended
and evidenced (§8 of the previous gate), but it must be ratified rather than
assumed — it changes what the system records about accountability, and the
predicate now makes its consequences exactly countable: **338 open activities**
would have been created unowned instead of customer-owned.

Path: **P3 → E1 → remediation (E5, E6) → E3 → P5 → employee-email
authorization.** Email remains the last step and remains out of scope.

---

## Appendix — retrieval defect (unchanged, untouched)

`test_recency_only_output_is_frozen` still fails on the pre-gate tree:
`payment reminder: 3`, pinned `4`. `recency_only` ranks a fixed 4,000-record
slice of 13,201, so the pin is corpus-size sensitive by construction. Not
touched, not suppressed, not re-pinned.
