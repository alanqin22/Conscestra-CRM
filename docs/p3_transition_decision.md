# P3 — Ineligible-Owner Transition: DECISION RECORD

**Ratified 2026-09-02.** Recorded here because a decision that lives only in a
conversation is not a decision anyone can audit later.

---

## The decision

```
UNASSIGNED
```

When a handler's candidate owner is not certified by the eligibility contract,
the activity is **still created** with `owner_id = NULL`, and the refusal is
recorded.

```
ELIGIBLE       →  write the candidate
anything else  →  write NULL
                  never substitute the account owner
                  never substitute the notification holder
                  never derive one
```

## What it costs, measured

**338 open activities** would have been created unowned rather than
customer-owned. That figure is the measured difference on live data at the E2
baseline, not an estimate.

What the cost is *not*: no work is dropped, no task disappears, no due date
moves. The activity is created either way. What changes is whether the system
asserts a name against it.

## Why this and not the alternatives

| Option | Why not |
|---|---|
| `REFUSE` | Drops real work. A hot lead still needs calling |
| `ROUTE` | Auto-assigns, which `routing.py` exists to prevent — *"Recommends; never assigns"* |
| `ESCALATE` | No exception workflow exists for this, and it would rebuild the volume problem |

And the supporting precedent, already shipped: `data_readiness`'s cases check
records that *"an untriaged case sitting in the queue with no owner is the
CORRECT state."* Unowned is an accepted state. **Wrongly owned is not.**

The distinction the transition preserves: `owner_id = NULL` records that nobody
is accountable yet; a customer in `owner_id` records that the wrong party is.
Only the second is a false statement about a person.

---

## Ratified is not enabled

Two separate acts, and only the first has happened.

```
OWNER_ELIGIBILITY_ENFORCE = 0        (default; unset means off)
```

The transition ships behind the flag and is **off by default**, matching the
rollout posture the repair brief specified: a shadow window first, then the
flip. Until it is flipped, `owner_for_write()` returns every candidate
unchanged and behaviour is identical to before.

`test_H0` pins the default-off guarantee; a mutation defaulting the flag to on
fails it.

## Implementation

| Piece | Where |
|---|---|
| The decision | `work_ownership.owner_for_write()` |
| The flag | `work_ownership.enforcing()`, read at call time |
| Call sites | `agent_bus._record_lead_outreach_sync`, `_record_action_sync`, via `_owner_for_write` |
| Observability | `work_ownership.shadow_report()` — counts, states, per handler |

The function was renamed from `observe_candidate_owner`. It can now change what
is written, and a function that alters a write while calling itself an
observation is the same defect as a constraint named for a guarantee it does
not provide.

### A named limit

When enforcement drops a candidate, **the activity row does not record which
identifier was refused.** The only trace is a WARNING log line. Giving it a
durable record is a schema obligation and belongs with the remediation work,
not with a flag-gated handler change. This is a known limit, not an oversight.

---

## Verification

**Tests:** `test_H0` (default off), `test_H1a` (ineligible → NULL), `test_H1b`
(eligible survives), `test_H1c` (only two values are ever returned), `test_H2`
(never breaks a handler), `test_H3`/`H4` (wiring, failure isolation), `test_H5`
(report names transition and flag).

**Mutations, five run:**

| # | Mutation | Caught by |
|---|---|---|
| 36 | flag defaults ON — ratification silently becomes rollout | `test_H0`, `test_H5` |
| 37 | enforcement returns the candidate anyway | `test_H1a`, `test_H1c` |
| 38 | unresolved candidates slip past the predicate | `test_F10c` ← *initially SURVIVED* |
| 39 | enforcement ignores the flag | `test_H0` |
| 40 | customer candidates quietly exempted | `test_H1a` |

**Mutation 38 survived its first run**, on a reason change alone: deleting the
"resolves in no known identity space" branch left such a candidate falling
through to `INELIGIBLE_NOT_EXPLICITLY_GRANTED` — still not eligible, so every
outcome assertion passed. The reason is not cosmetic: *"this identifier names
nobody"* is an identity defect and *"this person holds no grant"* is P5. They
have different remedies, and **423 activities sit in the first class**. A report
filing them under the second would send someone to issue grants for people who
do not exist.

**Suite:** 2,743 passed, 1 failed, 1 skipped. The single failure is E8, the
pre-existing retrieval pin, unrelated.

## What this unblocks

`E1 → Phase C (E5, E6) → Phase D (E3)`. Nothing downstream can proceed until
the flag is flipped and a shadow window has run.

## What remains unchanged

`STAFF_EMAIL_APPLY=0` · no grants issued · no data mutated · no owner
reassigned · Railway on `d251612b886c` · every Railway population count
identical to the E2 baseline.
