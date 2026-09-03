# P5 — Human Employee Work-Email Authorization: DECISION RECORD

**Ratified 2026-09-02.** Recorded because a decision that lives only in a
conversation is not one anyone can audit later.

---

## The decision

```
P5 = APPROVED, scoped to declared-synthetic identities
```

A `@emp.agentorc.ca` employee identity **may** be granted eligibility as an
accountable owner and may receive Tier-2 work email — under the three
conditions below, and by **individual explicit grant only**.

It does **not** mean every employee becomes assignable, that employee existence
implies eligibility, or that `STAFF_EMAIL_APPLY` may be enabled.

## The fact that shaped it

The owner attested directly, 2026-09-02: **all eight `@emp.agentorc.ca`
employees are synthetic.**

That reframed the question. The risk in authorising them is not one risk but
two, with opposite profiles:

| Question | Risk | Verdict |
|---|---|---|
| Can mail reach `sarah.johnson@emp.agentorc.ca`? | **None** — a catch-all the owner controls; that mail lands in their own inbox | Safe to exercise |
| Is Sarah Johnson **accountable** for real work? | **Real** — a synthetic identity recorded as accountable corrupts the model | Must stay out |

Only the second bites. An earlier draft of this analysis conflated them and
recommended outright rejection; that was wrong, and the correction is recorded
rather than quietly dropped.

The corpus is also coherent here: the leads and invoices these identities would
receive digests about are themselves seed data. A synthetic owner accountable
for synthetic work is a demo corpus behaving consistently. The corruption risk
appears only if **real** work is later attributed to them — which is condition 3.

### Why not relabel them as real

Rejected outright. The attestation is the strongest evidence this corpus
admits, and `corpus_provenance` held **zero** rows for `employees` before this
gate. Recording them as real would destroy that fact at the moment of its
creation — and it is precisely the failure this whole programme traces to: the
seed-email migration overwrote provenance and left a corpus where real and
synthetic can no longer be told apart. Doing that deliberately would be worse
than doing it by accident.

"Hybrid" was considered and rejected for the same reason: it is not a
provenance state, it is *"synthetic but treated as real"* — relabelling with an
extra step, after which every check keyed on provenance reads a value meaning
two things.

---

## The three conditions

**1. The provenance is recorded first.** — DONE, this gate.

```
governance/sql/employees_provenance_attestation.sql
  applied LOCAL   2026-09-02   corpus_provenance 44 → 52
  applied RAILWAY 2026-09-02   corpus_provenance 12 → 20
  8 attested synthetic · all resolve to employee rows · 0 service identities
```

`state='synthetic'`, `rule='human_attested'` — a combination the table's own
CHECK already anticipated. Eight UUIDs **named individually, never a pattern**:
a predicate like `email NOT LIKE '%@system.internal'` would classify whoever
arrives next as synthetic too, and a real employee hired tomorrow must not
inherit an attestation nobody made about them.

**2. A grant carries its provenance.** Eligibility reporting must be able to
answer *"how many production-accountable owners exist"* and get **0**, not 8.
Without this, eight demo personas enter the eligible population
indistinguishably from real staff and the readiness dimension begins reporting
seed data as accountability — the exact defect this programme removed.

**3. Real work must never land on a synthetic owner.** A guard, not a
convention: the moment a genuine lead or invoice is attributed to one of these
identities, the model is corrupted.

Conditions 2 and 3 are **not yet implemented.** They are prerequisites for any
grant, and no grant has been issued.

---

## What is unchanged by this decision

```
grants issued                 0        assignable_identity      4 (unchanged)
STAFF_EMAIL_APPLY             0        E1 enforcement           unset
employee UUIDs reused as owner IDs     0
F1                            unresolved, and the jmartin attestation says so explicitly
production employee email     NONE SENT
invoice randomization trigger untouched
```

The attestation changed no employee row, no owner, no activity, and no
ownership. It records what these identities **are**.

### The F1 note, carried in the data

The `jmartin` attestation carries its own caveat in the evidence JSON: that
UUID is also an `owner_id` naming a different person. The attestation is about
the **employee** identity only and resolves nothing about the collision.

---

## What still stands between this and an email

Unchanged by P5 and still required, in order:

1. **Condition 2** — grants carry provenance; synthetic never counts as production accountability
2. **Condition 3** — a guard that real work cannot land on a synthetic owner
3. **Per-person grants** with fresh owner UUIDs and `owners.employee_uuid` as the only link
4. **Digest identity resolution** — `digest_items()` still assumes `notifications.employee_uuid = owner_id`, which a fresh owner UUID breaks; it must resolve `owner → owners.employee_uuid → employee`
5. **A separate activation gate** for `STAFF_EMAIL_APPLY=1`

Steps 3 and 4 are **governed identity changes, not implementation details** — an
earlier draft called them "mechanical" and that was too loose.

---

## Next gate

> **Employee Grant & Digest Identity Resolution** — conditions 2 and 3, then
> per-person grants, then the digest resolution path.

The invoice write-provenance gate may proceed independently; it shares no
surface with this one.

**Email activation remains separate.** A successful grant proves *authorization
and reachability*. It does not start sending production mail.
