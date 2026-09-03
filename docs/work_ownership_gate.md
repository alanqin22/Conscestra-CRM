# Stage 2 — Work Ownership & Identity Resolution Gate

**Analysis, instrumentation and tests only.** No authorization granted, no
`assignable_identity` row touched, no identity mapping invented, no production
data mutated, nothing deployed. Railway read-only, 2026-09-02T14:21Z.

Governing source of truth: `staff_email_authorization_gate.md`.
Identity rules: `identity_resolution_spec.md` §3.

---

## 1. Executive verdict

```
NOT READY — WORK OWNERSHIP MODEL MISSING
```

Two candidate ownership signals exist. **Both fail, for different reasons, and
there is no third.**

```
Railway, unread Tier-2 work, 2026-09-02T14:21Z

  items_total            14
  OWNED                   0
  UNOWNED                13     customer_contact
  IDENTITY_UNRESOLVED     1     identity_collision  (a1451ad6…)
```

Zero work items can be attributed to an eligible human owner. Employee email
authorization therefore **cannot yet be evaluated**, and the question is not
close enough to be worth re-asking after a small repair.

---

## 2. Work ownership model

### The lifecycle, traced

```
event
  └─> event_subscriptions        (WHO SUBSCRIBED to this event_type)
        └─> fan_out_notifications_for_event()
              └─> notification_recipients.employee_uuid
                    └─> view `notifications`.employee_uuid
```

`notifications` is a **VIEW**. The base table is named
`notification_recipients`, and the fan-out function inserts
`v_sub.employee_uuid` — a subscription row. So the holder column means
**"was copied to"**, and the schema says so in its own table name.

Measured fan-out over 10 days:

```
112 events × 5 holders      146 events × 4 holders      14 events × 1 holder
```

One `invoice.overdue`, expanded: `Accounting Agent`, `Notifications Agent`,
`sysadmin`, `sjohnson`, `mchen` — two AI agents, one service account, two
humans, all receiving the same copy. Nothing in that path expresses
accountability.

**Answers to the ten questions:**

| # | Question | Answer |
|---|---|---|
| 1 | What owns a work item? | Nothing does. The *entity* has an owner; the work item does not |
| 2 | Is ownership stored explicitly? | **No** |
| 3 | What field is intended to represent it? | `<entity>.owner_id` — but see §5 |
| 4 | What is `notifications.employee_uuid`? | **Notification fan-out / subscriber.** Not ownership, not assignment |
| 5 | Multiple holders per item? | **Yes** — up to 5 today |
| 6 | Can a system actor hold work? | **Yes, and does** — `sysadmin` is the single largest holder |
| 7 | Can a service account be accountable? | No. `dsar.staff_personhood()` declares it software |
| 8 | Where is the system-actor invariant? | `dsar.SERVICE_IDENTITY_ROLES` + `SERVICE_IDENTITY_EXCEPTIONS`; spec I11. **Existed, was never consulted by routing** |
| 9 | What connects employee → authorized owner? | **Nothing** |
| 10 | Can it be established deterministically? | **No** — see §5 |

---

## 3. Current population analysis

Railway, unread Tier-2, read-only:

| entity_type | items | owner source | state | reason |
|---|---|---|---|---|
| `lead` | 13 | `leads.owner_id` | UNOWNED | `customer_contact` |
| `lead` | 1 | `leads.owner_id` | IDENTITY_UNRESOLVED | `identity_collision` |

Local, same classifier, larger population (78 items) — same verdict, and it
surfaces the third failure mode:

```
OWNED 0 · UNOWNED 78 · IDENTITY_UNRESOLVED 0
  no_owner_field   42     invoice.overdue — no owner column exists at all
  customer_contact 28
  no_owner_value    8
```

This explains the earlier `14 → 14 assigned → 1 holder → 0 authorized → 14
unreachable` exactly: the single holder is `sysadmin`, a service account; the
work behind it belongs to leads owned by customers.

---

## 4. System-actor analysis

Verdict: **A — a system actor is an invalid work owner** (and an invalid
recipient), with B's caveat that it may legitimately remain a technical
*holder*.

The invariant is real and already implemented — `dsar.staff_personhood()`,
fail-closed:

```
people 8 · services 13 · unclassifiable 0 · ok True
```

It keys on `role='agent'` plus an individually declared exception list, because
neither available signal suffices alone: `role` misses `sysadmin` (whose role
reads `Administrator`), and `@system.internal` is a **mutable attribute** — the
same class of key that silently re-labelled 39 customers.

**The gap was that nothing in the routing or authorization path consulted it.**
`assignable.grant()` performs no eligibility check whatsoever: it would accept
`agent.lead@system.internal`. That primitive was left unchanged (§8), and the
refusal was placed on the *email decision* instead — see §9.

---

## 5. Employee ↔ owner identity analysis

```
employees.employee_uuid  →  owners.owner_id     deterministic links: 0
```

Classification: **an intentional architectural boundary, not an incomplete
implementation.** Spec §2.2 decided that an owner is a governance role
assignment whose assignee may be a contractor, consultant or AI agent, so
`owners.employee_uuid` at 0/44 is correct and must not be backfilled.

### Why `<entity>.owner_id` is not the answer either

It is deterministic, a real FK, and fully populated — and it does not point at
staff:

```
leads                 100 rows, owner_id 100/100, 37 distinct owners
  customer contacts    36 / 37     (contacts.contact_id = owner_id, shared PK)
  employees             1 / 37     — and it is a1451ad6…, the collision
  authorized            0 / 37

owners                 44 rows
  shared PK with a contact   39 / 44      (88.6%)
  also an employee            1 / 44
  authorized                  4 / 44      (the executives)
```

**Routing on entity ownership would have emailed internal staff worklists to
customers** — 36 of 37 — and would have done so past every guard in
`staff_email`, because it never touches `notifications.employee_uuid`, the
column those guards watch. This is the single most dangerous available "fix"
and it is the reason the gate's answer is NOT READY rather than "wire it up".

`accounting_invoice_pipeline` has **no owner column at all**, so
`invoice.overdue` work is structurally unownable today.

### `executives.employee_uuid` — a separate, reportable defect

4/4 populated; **0/4 resolve in `employees`; 4/4 resolve in `owners`.** The
column name asserts an identity space its values do not live in. Not repaired
here — documented as its own defect, unrelated to employee email.

---

## 6. F1 collision analysis

```
a1451ad6-310c-4bcc-ba17-dd383a881ee8
  employees : jmartin      julia.martin@emp.agentorc.ca
  owners    : John Smith   john.smith@example.com
```

Verified live on Railway. Exactly 1 of 21 employee uuids collides. Not a
customer contact. Holds 621 notifications, and **is one of the 37 lead
owners** — so it sits on both candidate ownership paths simultaneously.

### Cross-space audit

| Path | Before | Now |
|---|---|---|
| `resolve_recipient(assignee=…)` | quarantined via `_is_collision` | unchanged |
| `resolve_recipient(owner_id=…)` | **NO CHECK** — reads `assignable_identity` and trusts it | refused in `decide()` |
| `digest_items(owner_id)` | safe only because the spaces are disjoint *by data* | unchanged; now unreachable behind the refusal |
| `work_ownership.classify()` | n/a | `IDENTITY_UNRESOLVED`, checked first |

The asymmetry was one grant wide: the guard covered the path where the
identifier is untrusted and left open the path where it had been written down.
The new check runs on the **input**, ahead of resolution, so the refusal does
not depend on whether that uuid happens to be granted today.

**Not resolved, deliberately.** No identity was chosen as correct, nothing was
merged. Per invariant I7 this must become exactly two parties, by explicit
decision.

---

## 7. Corpus provenance analysis

| Question | Answer |
|---|---|
| What does it classify today? | `accounts` and `contacts` only — 12 rows on Railway, 44 local |
| Why are employees/owners absent? | Phase 0 scoped itself to customer-side subjects |
| Is it expected to cover them? | Spec §7 Phase 0's exit criterion is "every **subject** in exactly one state"; staff were never in scope |
| Would adding them be a design change? | **Yes** — and it interacts with I11: a service identity is not a subject and must not be classified as one |
| Is it required for *this* gate? | **No** |

Provenance is required to decide whether an ambiguous *pair* may be merged. This
gate merged nothing and proposed no grant, and it reached its verdict from
deterministic keys plus declared personhood. **It remains a blocker for identity
merging, and is not a blocker for the ownership verdict.** No provenance rows
were added.

---

## 8. Existing-contract evidence

| Claim | Evidence |
|---|---|
| holder = subscriber | `fan_out_notifications_for_event()` inserts `v_sub.employee_uuid` from `event_subscriptions` |
| `notifications` is a view | `pg_get_viewdef` over `notification_recipients` ⋈ `notification_messages` |
| personhood is declared | `dsar.py` `SERVICE_IDENTITY_ROLES` / `SERVICE_IDENTITY_EXCEPTIONS`, `staff_personhood()` |
| owner ≠ employee is intentional | `identity_resolution_spec.md` §2.2 |
| name/email matching prohibited | spec §3 evidence hierarchy |
| owners are mostly customers | `assignable.py` docstring, confirmed 39/44 by query |

---

## 9. Changes made

| Change | Kind |
|---|---|
| `app/core/work_ownership.py` — three-state classifier, read-only, deterministic keys + declared personhood only | new |
| `decide()` refuses a **declared service identity** as recipient | restriction |
| `decide()` refuses a **colliding uuid** on the `owner_id` path, checked on the input before resolution | restriction |
| `governance/tests/test_work_ownership.py` — 16 cases | tests |

Both refusals are fail-closed and narrow the system. Neither creates, modifies
or removes any authorization.

## 10. Changes deliberately NOT made

* No `grant()` — none proposed, none executed
* No `assignable_identity` row added, modified or removed
* No eligibility check added *inside* `grant()` — that is the authorization
  primitive, and §8 puts it out of scope. The refusal sits on the email
  decision instead
* No `employees ↔ owners` mapping invented
* No F1 repair; neither identity chosen as correct
* No `executives.employee_uuid` repair — reported separately
* No `corpus_provenance` rows added
* No `STAFF_EMAIL_APPLY` change; no deployment
* No retrieval code touched

---

## 11. Tests and mutation results

37 tests across the two staff-email/ownership files; full suite **2699 passed,
1 failed, 1 skipped** (was 2683/1/1). **Zero new failures.**

**15 mutations verified.** One initially survived, and that is the most useful
line in this table:

| # | Mutation | Caught by |
|---|---|---|
| 1 | idle system counts as structural silence | `test_92` |
| 2 | incident recorded unconditionally | `test_95`, `test_97`, `test_97b` |
| 3 | holder authorization check removed | `test_A3`, `test_A5` |
| 4 | collision treated as a missing grant | `test_A1` |
| 5 | worklist role-mailbox gate bypassed | `test_A3`, `test_A4` |
| 6 | unreachable work discarded from the total | `test_91`, `test_A5` |
| 7 | digest gate applied uniformly (would silence escalations) | `test_B2` |
| 8 | `send_disabled` branch removed | `test_97b` |
| 9 | **customer contact reclassified as OWNED** | `test_C4` |
| 10 | collision check dropped from the classifier | `test_C7` |
| 11 | collision check removed from the recipient path | `test_C8` |
| 12 | service-identity check removed from the recipient path | `test_C9` |
| 13 | **personhood failure treated as fail-OPEN** | `test_C10b` ← *initially SURVIVED* |
| 14 | owner-source map grown reflectively | `test_C2` |
| 15 | `unclassifiable` roster ignored | `test_C10c` |

**Mutation 13 survived the first battery.** `test_C10` monkeypatched
`service_identities()` itself, so it proved how *callers* behave and nothing
about the function — a test passing for the wrong reason. `test_C10b` and
`test_C10c` break `dsar.staff_personhood()` for real, and both mutations are
now caught. The gap was found only by running the mutation, which is the
argument for running them.

---

## 12. Production verification

```
STAFF_EMAIL_APPLY               = 0   (unchanged)
ESCALATION_EMAIL                = 0   (unchanged)
employee digest emails sent     = 0
authorization grants created    = 0
assignable_identity writes      = 0
identity mappings invented      = 0
production data mutated         = none
Railway deployment              = unchanged (commit d251612b886c)
all Railway access              = read-only transactions
approval / escalation behaviour = unchanged (test_B1, test_B2)
```

---

## 13. Unrelated retrieval defect

`test_hybrid_retrieval.py::test_recency_only_output_is_frozen`.

Reproduced on the **pre-gate tree** (everything stashed, `git status` clean):

```
payment reminder: 3    pinned: 4
the other three queries match exactly
```

**Mechanism.** In `recency_only` mode the ranking budget is a fixed slice —
`content_index` logs *"search ranked 4000 of 13201 matching records (30%) —
results are drawn from the most recent slice only"*. The corpus now holds
13,201 records. One record that previously matched `payment reminder` has aged
out of the most-recent 4,000. The pin is therefore **corpus-size sensitive by
construction** and will keep drifting as the corpus grows.

Not touched, not suppressed, not re-pinned. **Recommended remediation** (a
separate decision): pin the test against a fixed fixture corpus, or make the
recency slice deterministic for the test, rather than re-pinning numbers that
will drift again. Re-pinning treats the symptom and resets the clock.

---

## 14. Remaining blockers

| # | Blocker | Owner |
|---|---|---|
| B1 | **No work-ownership model.** Work items have no accountable owner; the entity's owner is a customer 36/37 of the time | product decision |
| B2 | `invoice.overdue` work is structurally unownable — no owner column | schema decision |
| B3 | F1 unresolved; must become two parties (I7) by explicit decision | governance |
| B4 | `executives.employee_uuid` holds owner-space values under an employee-space name | schema-contract defect |
| B5 | `grant()` has no eligibility check — would accept a service identity | authorization gate |
| B6 | E6 (ambiguous dispositions) and E7 (invariants on a rehearsal copy) still open | spec §9 |
| B7 | `corpus_provenance` does not cover staff — blocks *merging*, not this verdict | spec Phase 0 scope |

---

## 15. Exact next gate

**Not** employee email, and **not** the party migration.

> **Work Ownership Definition Gate** — decide whether a Tier-2 work item is
> required to have an accountable human owner, and if so, where that is
> recorded.

It is a product/schema decision, not a data repair, and B1+B2 are its whole
content. Everything downstream — recipient authorization, digest usefulness,
Stage 4's exit criterion — is blocked on it and cannot be sensibly evaluated
before it.

If the answer is that Tier-2 work does **not** need a human owner, then the
correct conclusion is that the worklist digest should not exist, and Stage 4
should be withdrawn rather than gated. That outcome is on the table and would
be a legitimate result.
