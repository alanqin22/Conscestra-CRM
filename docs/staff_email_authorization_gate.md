# Staff Email — Reachability & Authorization Gate

**Analysis only. No data mutated, no authorization granted, no flag flipped,
nothing deployed.** Measured against Railway (read-only) and local,
2026-09-02T14:08Z. Companion to `employee_email_notifications_design.md` §K.4
and `identity_resolution_spec.md`.

---

## The finding that changes the question

The previous report concluded that recipients and work live in disjoint
identity spaces. That is true, and it is not the most important thing here.

Going one level deeper into *what the work actually is* found that
`notifications.employee_uuid` **is not a work-ownership column at all.** It is
a broadcast subscriber list:

```
one invoice.overdue event, expanded (Railway, most recent):

  00000000-…-0008   Accounting Agent      agent.accounting@system.internal
  00000000-…-0010   Notifications Agent   agent.notifications@system.internal
  25eaf35e-…        sysadmin              admin@system.internal
  307cc6ac-…        sjohnson              sarah.johnson@emp.agentorc.ca
  67f0a5b1-…        mchen                 mike.chen@emp.agentorc.ca

fan-out over 10 days:  112 events × 5 holders
                       146 events × 4 holders
                        14 events × 1 holder
```

Two AI agents, one service account and two humans receive **the same copy of
the same event**. Nothing in the row says whose job it is. The digest's stated
premise — *"the Tier 2 worklist for one authorized person: still actionable,
still **theirs**"* — is not satisfiable from this column, because the column
does not express *theirs*.

**Consequence for the authorization question.** Granting sjohnson today would
not give her *her* worklist. It would give her *every* `invoice.overdue` in the
system, and give mchen an identical duplicate. The authorization gap is real,
but it sits on top of a missing work-assignment layer, and closing the top one
first would mail broadcast noise to eight people and call it a worklist.

### And `read` does not mean a human read it

`notification_triage.py` Pass C re-validates actionable alerts against live
entity state and resolves them automatically — seven distinct `SET
status='read'` sites:

* `invoice.overdue` → read once the invoice is paid, cancelled, or under the floor
* `lead.scored` → read once the lead drops below 70, converts, or is deleted
* plus a dedup pass marking all but the newest per (entity, holder, event_type)

Read latency runs **13 minutes to 1.15 days** — sweep-shaped, not human-shaped.
So the Tier-2 queue is **self-clearing**: items close because the underlying
business condition cleared, not because anyone acted on a notification. A
digest of this queue would describe work that resolves itself.

This also corrects the previous report's "76 unread": that population is
transient, not a standing backlog.

```
2026-09-01T02:50Z    76 unread, 3 holders
2026-09-02T14:08Z    14 unread, 1 holder
```

---

## A. Employee Email Authorization

```
NOT AUTHORIZED
```

No explicit authorization decision exists, and none may be inferred from
employees existing, from employees holding notifications, or from
`@emp.agentorc.ca` addresses existing. The decision is the owner's.

Beyond the absence of a decision, the evidence does not currently *support*
one: there is no work-ownership signal to authorize against.

---

## B. Identity Reconciliation

| Table | Rows | Key | Notes |
|---|---|---|---|
| `employees` | 21 | `employee_uuid` | **13 are `@system.internal` service accounts / AI agents**; only 8 are `@emp.agentorc.ca` humans |
| `owners` | 44 | `owner_id` | `employee_uuid` populated 0/44 — **correct**, not a defect (spec §2.2) |
| `assignable_identity` | 4 | `owner_id` | all 4 resolve in `owners` ✓ and `executives` ✓, **none in `employees`** |
| `notifications` | — | `employee_uuid` | not an FK; holds employee-space *and* agent uuids; a fan-out list, not ownership |

**Deterministic relationships that exist:**

* `assignable_identity.owner_id → owners.owner_id` — Rank 1 evidence, 4 links.
* `executives.employee_uuid → owners.owner_id` — 4 links, but the **column name
  asserts a space its values do not live in** (0/4 resolve in `employees`).

**Deterministic relationships that do not exist:**

* `employees.employee_uuid → owners.owner_id` — **zero**. Spec §1 measured
  owners↔employees at 0 by email, 1 by name, **0 deterministic**.
* Therefore `employee → authorized recipient` **cannot be established today by
  any permitted rule.** Name and email matching are prohibited (§3), and they
  are the only signals present.

### F1 collision — verified live on Railway

```
a1451ad6-310c-4bcc-ba17-dd383a881ee8
  employees : jmartin      julia.martin@emp.agentorc.ca
  owners    : John Smith   john.smith@example.com
```

Exactly **1 of 21** employee uuids is also an `owner_id`. It is not a customer
contact (checked: no shared `contact_id`, no matching contact email), so the
immediate blast radius is smaller than feared — but the identifier still names
two different people, and `identity_space()` reports `collision: true`.

**Why this is one grant away from being live:** `digest_items()` matches
`assignable_identity.owner_id` against `notifications.employee_uuid`. Those
spaces are disjoint today, which is the only reason the join is safe. A grant
keyed on an employee uuid would make them overlap — and for `a1451ad6…` that
single grant would deliver one person's queue to the other's identity. Not
hypothetical: that uuid holds 621 notifications.

---

## C. Current Reachability

Railway, 2026-09-02T14:08Z, via the new status contract:

```
tier2_total              14
  assigned               14
  unassigned              0
holders_total             1
  identifiable            1
  authorized              0
reachable                 0
unreachable              14
  recipient_not_authorized  14
  identity_unresolved        0
authorized recipients     4
sending      enabled=true  applying=false
structural_silence     true
```

**Unresolved identities:** 0 in the current 14 (the single holder, `sysadmin`,
resolves cleanly in `employees`). 1 collision exists corpus-wide and is
currently dormant.

**Unauthorized holders:** 1 of 1 — and historically 3 of 3, all in the
`employees` space.

Note `sysadmin` is `admin@system.internal`: under the identity spec a **system
actor, which must never become a party** (§2.1, invariant I11). The single
largest holder of "unreachable work" is a machine.

---

## D. Root Cause

```
COMBINATION — and NOT a transport failure
```

Ranked by what must be fixed first:

| Rank | Cause | Evidence |
|---|---|---|
| 1 | **Work assignment gap** | one event → 4–5 holders incl. AI agents; no column expresses accountability |
| 2 | **Recipient authorization gap** | 0 of 1 holders granted; 14/14 `recipient_not_authorized` |
| 3 | **Identity mapping gap** | 0 deterministic `employees ↔ owners` links; 1 live collision |
| 4 | **Synthetic-data artifact** | 13 of 21 "employees" are service accounts; provenance unclassified (§7 blocker) |
| — | ~~Transport failure~~ | **excluded** — approval mail delivered to ceo@/cro@/cfo@ on 2026-09-01 |

The previously reported "identity spaces are disjoint" is cause 2+3. Cause 1 was
underneath it and is the one that determines whether a digest is meaningful.

---

## E. Authorization Changes Proposed

```
NONE. No grant() is proposed, and none was executed.
```

Every grant I could construct today would be unjustified:

| Candidate | Why not |
|---|---|
| `sjohnson`, `mchen` (real humans, hold work) | no deterministic key from `employees.employee_uuid` to an owner identity; the grant would have to invent one, which §3 prohibits |
| `sysadmin` | a **system actor** — I11 forbids it becoming a party; `admin@system.internal` is not a deliverable address |
| the 2 AI agents | same, and self-evidently so |
| `jmartin` | the F1 collision; the grant would authorize an identifier that also names John Smith |
| remaining 5 `@emp.agentorc.ca` staff | hold no Tier-2 work at all; a grant would authorize nothing |

---

## F. Production Safety

```
STAFF_EMAIL_APPLY               = 0   (unchanged, local and Railway)
production email sent           = none
production identity mutation    = none
production authorization change = none
Railway deployment              = unchanged (commit d251612b886c)
all Railway queries             = read-only transactions
```

Test writes were confined to the local database and cleaned by fixture.

---

## G. Next Action

```
PROCEED TO IDENTITY RESOLUTION
```

With one scope correction. The blocking question is **not** the full party
migration in `identity_resolution_spec.md` — that is a larger programme with
E6/E7 still open. The blocking question for staff email is narrower and comes
first:

> **Does a work-ownership signal exist at all, and if not, where would it come
> from?**

Until a notification can say *whose* it is, "who may receive email" has nothing
to attach to. The recommended order:

1. **Decide whether Tier-2 notifications need an owner** distinct from the
   fan-out subscriber list. This is a product decision, not a data repair.
2. **Ratify E6** — the ~163 ambiguous candidates' disposition is "leave
   separate"; it must be recorded, not assumed.
3. **Resolve F1** by explicit decision (two parties, per I7). One uuid, one
   decision, no merge.
4. **Classify `employees` and `owners` in `corpus_provenance`** — see the
   blocker below.
5. Only then revisit whether any human should receive a worklist digest.

### §7 blocker — provenance cannot classify these records

```
corpus_provenance:  Railway 12 rows   local 44 rows
  entity_type in ('employees','owners'):  0     0
```

The Phase-0 classification covers `accounts` and `contacts` only. **Not one
Tier-2 work holder is classified.** So the synthetic/real boundary cannot be
drawn for exactly the population this gate is about, and per §7 that is
reported rather than repaired by heuristic. The `@emp.agentorc.ca` domain is
**not** evidence of a real employee — the same class of mutable-attribute
inference that re-labelled 39 customers.

---

## What was implemented under this gate

Instrumentation and guards only — no behaviour that sends anything.

| Change | Section |
|---|---|
| `ZERO_SEND_REASONS` — six distinct causes of a zero-send, enumerated | §12 |
| `worklist_reachability()` — the eight-question contract, counts + reason codes + uuids only, no names or addresses | §13 |
| Preview distinguishes three zeros: no work / unreachable / send disabled | §14 |
| **A digest may no longer fall back to the role mailbox** — a live defect: a revoked grant would have mailed a personal worklist to `support@` | §12 |
| 21 tests, **8 mutations verified** | §15, §16 |

### Mutation results

| # | Mutation | Caught by |
|---|---|---|
| 1 | idle system counts as structural silence | `test_92` |
| 2 | incident recorded unconditionally | `test_95`, `test_97`, `test_97b` |
| 3 | holder authorization check removed | `test_A3`, `test_A5` |
| 4 | identity collision treated as a missing grant | `test_A1` |
| 5 | authorization gate bypassed (worklist → role mailbox) | `test_A3`, `test_A4` |
| 6 | unreachable work discarded from the total | `test_91`, `test_A5` |
| 7 | digest gate applied uniformly (would silence escalations) | `test_B2` |
| 8 | `send_disabled` branch removed | `test_97b` |

### Suite

```
2683 passed, 1 failed, 1 skipped     (was 2671 passed, 1 failed)
```

The single failure is `test_hybrid_retrieval.py::test_recency_only_output_is_frozen`
and is **pre-existing and unrelated** — reproduced on a fully stashed tree:

```
payment reminder: 3   expected 4     (the other three queries match exactly)
```

One query lost one result. `content_index` logs *"search ranked 4000 of 13201
matching records (30%) — results are drawn from the most recent slice only"*, so
the pinned counts drift as the corpus grows past the recency window. Not
touched, not suppressed, reported separately — the pin will keep drifting and
needs its own decision.
