# P1 Remediation — Decision Memo

**2026-08-23.** Four changes implemented and independently verified. The
Agentforce comparison is revisited only where implementation changed a previous
conclusion.

---

## Executive verdict

### **ACCEPTED WITH CONDITIONS**

All four findings were independently confirmed before any code was written, and
all four are now fixed and guarded. **Two of the fixes were wrong on the first
attempt** and were caught by the test suite rather than by review — both
mistakes are now encoded as tests so they cannot be rediscovered.

The conditions are three residual gaps, none of them regressions, listed in
§Remaining gaps. The largest is deliberate and is defended in the final section.

```
2071 tests pass (was 2031 — +40 invariant tests)
a2a_dispatches now distinguishes:  rejected 120 · accepted 22 · failed 2
                                   (all 122 were an indistinguishable ok=false)
capability_registry:               45 of 45 seeded, closed-by-default ACTIVE
```

---

## Findings

### F1 — audit outcome flattened to boolean · **P1 · FIXED**

**Evidence, verified independently.** `A2AResult` carries both `ok` and
`outcome`, the latter commented *"Callers that must not over-claim should read
THIS, not `ok`."* `_log_dispatch` wrote `res.ok`. `a2a_dispatches` had no
`outcome` column. `/trace/{cid}` selected `ok`. The loss was total across the
whole lifecycle.

**Root cause — and it is worse than the report said.** `__post_init__` defaults
`outcome = ACCEPTED if ok else FAILED`. Measured: **9 `A2AResult(False, …)`
constructions, 6 naming no outcome — and 4 of those 6 are refusals**, including
both authorization gates:

```
no capability registered          -> recorded FAILED   (was a refusal)
capability disabled in registry   -> recorded FAILED   (an operator's kill switch)
caller not in allowed_callers     -> recorded FAILED   (an authz refusal)
skipped by governance confidence  -> recorded FAILED   (a policy decision)
```

An operator reading that trace saw "failed" and might retry an authorization
refusal.

**Fix.** `outcome` and `principal` columns on `a2a_dispatches` with a CHECK
constraint; `_log_dispatch` writes `res.outcome`; `trace.py` returns both. The
four refusals now name `REJECTED` explicitly. `ok` is **kept** — existing
readers depend on it, and removing it to force the new column would break things
to make a point.

**Verification.** `test_10`–`test_14`, `test_50`–`test_53`. Live proof: 120
rejected vs 2 failed now separable in the same table where they were one value.

**Residual risk.** 3,895 historical rows keep `outcome IS NULL`, deliberately.
`ok=false` could have been any of the three modes, and inventing one would be the
fabrication the column exists to prevent.

---

### F2 — no principal in the envelope · **P1 · FIXED**

**Evidence.** `A2ARequest` fields were `confidence, correlation_id, entity,
from_agent, govern_bypass, intent, params, prose, requires_ack`. No user, no
session. The session was synthesised as `a2a-{from_agent}-{cid}`. The
authenticated edge *did* resolve a full session (`auth_sessions`: identifier,
role, contact_id, tenant_id) and then kept only the **role** on a ContextVar.

**Fix — deliberately narrow.** A frozen `Principal(kind, id, display, role,
tenant_id)`, stamped at `require_data_access`, inherited by `dispatch` from a
request-scoped ContextVar so ~40 existing call sites acquire identity without
being rewritten, **required for writes**, and logged. Background callers name
themselves via `Principal.service(...)` — unattended work is not anonymous work.

**No policy engine, no identity provider, no IAM framework.** The existing
`auth_sessions` is the identity system; this makes what it already knew travel.

**Verification.** `test_20`–`test_27`. Notably `test_25`: a principal cannot be
supplied through `params`, which is what an LLM fills.

---

### F2a — the first write gate broke the approval flow · **SELF-INFLICTED · FIXED**

**Found by the tests, not by review.** Requiring a principal for writes refused
`governance._execute_approved`, which re-dispatches an approved action with
`govern_bypass=True` and no principal. Every approved write would have failed.

**Root cause.** `govern_bypass` skips the *confidence* gate; it was silently
assumed to skip identity too. **Bypassing "is the agent sure enough" must not
bypass "on whose authority".**

**Fix, and it is architecturally better than an exemption.** The authority for
an approved action is *the human who approved it*: the principal is now built
from `ap["decided_by"]`. Not "governance", which is the mechanism, and not the
proposing agent, which was refused permission to act alone.

---

### F3 — capability registry empty, gates default open · **P1 · FIXED**

**Evidence.** `reg.get("enabled", True)` and a null `allowed_callers` mean a
missing row permits everything. **0 rows on local and Railway** — agent RBAC and
the kill switch had never fired.

**Fix.** `sync_capability_registry()` seeds from `CAPABILITIES` (the
authoritative manifest, in code — a SQL seed would be a second copy that
drifts). The gate is now **closed by default**: a seeded registry that does not
name an intent refuses it.

The empty-registry case remains permissive, and that is not fail-open dressed
up: an unseeded database cannot distinguish "nothing is permitted" from "nobody
ran the seed", and refusing everything on a fresh checkout would make a missing
migration a total outage. It logs loudly and `registry_state()` exposes it.

**Verification.** `test_30`–`test_35`. `test_33` demonstrates the kill switch
**firing**, which it had never done in production.

---

### F3a — the first registry seed invented an RBAC policy · **SELF-INFLICTED · FIXED**

**Found by 14 failing tests.** The seed set `allowed_callers = {owning agent,
orchestrator, system}`. But `cap.agent` names who **implements** a capability,
not who may **call** it. `accounting` legitimately dispatches
`email.send_payment_reminder` — cross-agent delegation is the entire point of a
mesh — and the seed refused it.

**The lesson generalises: an RBAC policy cannot be derived from the manifest,
because the manifest describes ownership and the policy describes traffic.**
Guessing one produces a control that breaks real work, which is how controls get
switched off.

**Fix.** The seed registers every capability — which is what arms the kill
switch and the closed-by-default gate — and leaves `allowed_callers` NULL.
`observed_callers()` was added so an operator can narrow it against 3,895 rows
of real traffic rather than against a guess. `test_31` asserts the seed invents
nothing.

---

### F4 — no parameter schema on writes · **P1 · PARTIALLY FIXED (7 of 16)**

**§4 asked what a schema adds that `sp=` does not already guarantee. Measured
before writing any:** of the twelve resolvable write targets, **5 guard their
required fields, 7 do not, and none rejects an unexpected key.**

So a missing parameter travelled into the domain layer to be discovered late or
not at all, and an LLM-supplied stray key travelled all the way in.

**Fix — the smallest useful shape.** `params_schema = (required, optional)`.
Two checks: required-and-non-blank, and unknown-field rejection. **Deliberately
not types, ranges, or enums** — those belong in the SP and the SQL predicate,
where they are checked against the committed row under a lock. A second
business-rule engine would be two places to change one rule.

`required` names only fields the domain function already guards, so nothing that
works today begins to fail.

**Coverage is 7 of 16, and that is reported rather than hidden** (`test_45`).
The nine without a schema are unvalidated at the boundary exactly as before —
no regression, but no improvement either.

---

## Architecture after remediation

```
Principal                     auth_sessions → Principal.from_session
    │                         (or Principal.service for named background work)
    ▼
Interaction boundary          auth_dep.require_data_access
    │                         stamps role (write_guard) + principal (a2a ctx)
    ▼
Orchestrator                  intent → agent; planner previews; no autonomy
    │                         principal inherited from context
    ▼
A2A capability mesh           dispatch()
    ├─ 1. capability resolves?              else REJECTED
    ├─ 2. REGISTERED?  (closed by default)  else REJECTED   ← newly armed
    ├─ 3. enabled?     (operator kill)      else REJECTED   ← newly armed
    ├─ 4. allowed_callers?                  else REJECTED   (policy: operator's)
    ├─ 5. WRITE: principal present?         else REJECTED   ← new
    ├─ 6. WRITE: params valid?              else REJECTED   ← new, 7/16
    └─ 7. WRITE: governance confidence      → act | propose | skip
    ▼
Deterministic execution       sp= → domain fn → execute_sp → write_guard
    │                         → Postgres (read-only txn / customer_scope)
    ▼
Verified outcome              classify_outcome / classify_sp_result
    │                         accepted · rejected · failed · unknown
    ▼
Audit trace                   a2a_dispatches(outcome, principal) → /trace/{cid}
```

Every gate refuses with `REJECTED`, so a refusal is now distinguishable from a
failure at every layer including the permanent record.

---

## Remaining architectural gaps

### Must fix now
**None.** No finding in this round is a correctness or safety regression.

### Should fix before scale
- **`params_schema` on the remaining 9 write capabilities.** Currently
  unvalidated at the boundary. Not urgent — the SP layer still refuses malformed
  work — but the gap is the difference between "refused early with a clear
  message" and "discovered somewhere inside a domain function".
- **`allowed_callers` remains NULL on all 45.** See the final section.
- **Registry cache TTL is 30s**, so disabling a capability takes up to half a
  minute to bite. Acceptable for an operational control; not for an incident
  response one.

### Deliberately deferred
- **P1-3, orchestrator recovery.** The previous review recommended against an
  autonomous supervisor and nothing here changes that. A declared retry policy
  is now *possible* (outcome is persisted and `REJECTED` is distinguishable from
  `FAILED`), but the repository still shows no production requirement for it.
  Building it would be building for a problem nobody has reported.

### Explicitly rejected
Unchanged from the previous review: autonomous re-planning, a Data-360
equivalent, an A2A wire protocol between in-process modules, a generalised
policy engine, a new identity provider. Nothing implemented here made any of
them more justified.

---

## Agentforce reassessment

Only one conclusion changed. The previous review scored **Auditability 3** and
**Identity 2**, and named identity propagation and audit fidelity as the two
genuine gaps against Agentforce. Both are now materially closed:

| | Before | After |
|---|---|---|
| Auditability | outcome flattened to a boolean | four states persisted and constrained by the database |
| Identity | role- and channel-scoped only | principal travels, required for writes, in the audit record |

**Nothing here reopens a rejected feature.** The fixes borrowed *boundaries*
Agentforce draws — identity through execution, action observability — and none
of its *machinery*.

---

## Verdicts

| | | |
|---|---|---|
| A | Is P1-1 (audit outcome) fixed? | **YES** |
| B | Is P1-2 (identity) fixed? | **YES** |
| C | Is P1-4 (guardrail) fixed? | **YES** — kill switch armed and proven firing; caller policy deliberately left to an operator |
| D | Did `params_schema` materially improve the write boundary? | **PARTIALLY** — 7 of 16, and it closed a measured gap (7 targets with no required-field guard, 0 rejecting unknown fields) |
| E | Is the Orchestrator still appropriately non-autonomous? | **YES** — unchanged |
| F | Did any fix introduce a new boundary problem? | **YES, two — both caught by tests and fixed** (F2a, F3a) |
| G | Did any fix require more complexity than justified? | **NO** — one dataclass, two columns, one seeder, one validator |

---

## The single most important remaining weakness

> **`allowed_callers` is NULL on all 45 capabilities. Agent RBAC is armed but
> unpoliced: any component may still dispatch any capability.**

This is the one place I declined to act, and it is the honest answer rather than
the comfortable one — the kill switch and closed-by-default registration are
genuinely fixed, but the *caller policy* is exactly as permissive as it was.

**Why it is safe to leave now, specifically:**

1. **Guessing it demonstrably breaks the product.** The first attempt derived
   the policy from ownership and refused a legitimate cross-agent call, failing
   14 tests. A control that breaks real work is a control that gets switched
   off — which is worse than one that is honestly empty.

2. **It is not a security boundary in this architecture.** The mesh is
   single-process, single-tenant, and every caller is first-party code in one
   repository. The threat RBAC defends against is a component calling a
   capability it should not — and **31 of 32 `_sp_*` handlers are directly
   importable**, so an attacker who can execute in-process bypasses the mesh
   entirely by importing `_sp_order_cancel`. Agent RBAC here is a *blast-radius
   and operational* control, not a security perimeter, and describing it as the
   latter would be the more dangerous error.

3. **The control that actually matters is now live.** An operator can disable
   any capability instantly, and `test_33` proves that fires. That is the
   response an incident needs; a caller allowlist is not.

4. **The evidence to fix it properly now exists.** `observed_callers()` reads
   3,895 real dispatches. Narrowing can be done against traffic, deliberately,
   one capability at a time — which is how it should have been done in the
   first place.

**It stops being safe** when agents cease to be first-party in-process code:
when `custom_agents` (3 rows locally, 2 on Railway) can be authored by someone
outside the team, or when U4 capability grants let an authored agent act. At
that point the mesh becomes a trust boundary and the empty policy becomes a
real hole. That is the trigger to watch — not a date, and not a scale threshold.
