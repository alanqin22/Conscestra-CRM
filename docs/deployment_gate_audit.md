# Production Readiness and Deployment Gate Audit

**Read-only. 2026-08-23.** No code changed, nothing committed, nothing deployed.
State reconstructed from code, tests, migrations, configuration and both
databases — not from the supplied summary.

---

## VERDICT

# NOT READY

Two conditions remain unresolved, **both inside the remediation itself**, and
one of them is an observability-correctness defect that the remediation
introduced. Neither is a security failure; both are cheap to fix.

The migration is safe, the schema changes are trivially compatible, and the
architecture is sound. What is not ready is:

| | Blocker | Class |
|---|---|---|
| **B1** | The principal minted from `decided_by` records **policies, the system and a channel as `kind="user"`** | observability correctness — a *false* record, not a missing one |
| **B2** | The registry seed has **no production-reachable trigger**; the migration's own NOTICE names an endpoint that does not exist | deployment gap — the closed-by-default gate cannot be armed on Railway |

---

## 1. Actual post-remediation state — verified, not assumed

```
modules changed (uncommitted) : app/core/a2a.py · agent_bus.py · auth_dep.py
                                governance.py · planner.py · staff_email.py
                                supervisor.py · trace.py            = 8 ✓
migration (untracked, sql/ is gitignored)
                                sql/a2a_outcome_and_principal.sql   = 1 ✓
tests                           test_a2a_invariants.py (new, 40)
                                test_email_send_sp.py (helper updated)  = 2 ✓
suite                           2071 passed, 1 skipped              ✓
```

`staff_email.py` is in the changed set from the **earlier** staff-email work,
not this remediation. It is committed as part of `83ad48d` and carries no P1
change. Flagging it because "eight modified modules" implies eight remediation
modules, and that is not the case — **seven** carry remediation changes.

### The four invariants, verified live

```
outcome model     accepted 22 · rejected 120 · failed 2 · (pre-column) 3895
principal         required on writes; inherited from context; absent from
                  _DispatchBody so it cannot be supplied over the API
registry          45/45 seeded locally; closed_by_default = True
write schemas     7 of 16 declared; validate_params enforces required +
                  unknown-key rejection only
```

---

## 2. Migration audit

`sql/a2a_outcome_and_principal.sql`

| Object | Change | Local | Railway | Rollback |
|---|---|---|---|---|
| `a2a_dispatches.outcome` | `ADD COLUMN text` (nullable) | present | **absent** | `DROP COLUMN` — no data loss beyond new rows |
| `a2a_dispatches.principal` | `ADD COLUMN text` (nullable) | present | **absent** | same |
| `a2a_dispatches_outcome_check` | CHECK in 4 values or NULL | present | absent | `DROP CONSTRAINT` |
| `ix_a2a_dispatches_outcome` | partial index, `WHERE outcome <> 'accepted'` | present | absent | `DROP INDEX` |
| `capability_registry_callers_check` | CHECK jsonb array non-empty or NULL | present | absent | `DROP CONSTRAINT` |
| `capability_registry` rows | **none inserted by SQL** — seeded from code | 45 | **0** | `DELETE` |

**Safe.** Both `ADD COLUMN` are nullable with no default, which in PostgreSQL 11+
is a catalogue-only change — no table rewrite, no long lock. Measured on Railway:
**486 rows, 240 kB.** Duration risk is nil.

**Idempotent.** `ADD COLUMN IF NOT EXISTS`; both CHECKs guarded by
`pg_constraint` lookups; `CREATE INDEX IF NOT EXISTS`. Verified by applying it
twice locally.

**Constraint-compatible with existing data.** `outcome` is new, so every
pre-existing row is NULL and the CHECK admits NULL. On Railway,
`capability_registry` has **0 rows with a non-null `allowed_callers`**, so the
jsonb CHECK validates against nothing.

**One correction to the migration's own text.** Its verification block prints
*"seed via POST /a2a/registry/sync"*. **That endpoint does not exist** — see B2.

---

## 3. Railway drift — the two missing columns

```
a2a_dispatches columns on Railway:
  id · correlation_id · intent · from_agent · agent · kind · ok · error
  latency_ms · at
                          ^ outcome and principal are BOTH absent
capability_registry rows on Railway: 0
```

1. **Which two columns** — `a2a_dispatches.outcome`, `a2a_dispatches.principal`.
2. **Which table** — `a2a_dispatches`, 486 rows.
3. **Safely applicable** — yes, see §2.
4. **Existing rows satisfy the constraints** — yes, trivially (all NULL).
5. **What creates the 45 rows** — `a2a.sync_capability_registry()`, in code.
6. **Deterministic and repeatable** — yes, see §6.
7. **Ordering matters** — yes, see §4.

> **The system is not "fixed in production." It is fixed on one laptop.**

---

## 4. Migration and application ordering

**The seed has an ordering dependency the SQL cannot satisfy.** Rows come from
`CAPABILITIES`, which lives in the *new* application code. Railway is running a
build that predates it.

Verified: `sync_capability_registry` is **not** called from `app/main.py`
startup, and **no endpoint exposes it** (`/a2a/capabilities`, `/a2a/registry`,
`/a2a/registry/{intent}`, `/a2a/dispatch` — that is the whole router).

So the only supported sequence is:

```
1. migration       apply_sql / migrate --target railway     (safe alone)
2. app deploy      the new build reaches Railway
3. seed            ← NO SUPPORTED PATH TODAY  (B2)
4. verify
```

**Between steps 1 and 3 the registry is empty**, which the code treats as
permissive by documented exception. That is not an outage — but it does mean
**the closed-by-default gate is inert for the whole window**, and if step 3
never happens it is inert forever, which is precisely the state this remediation
set out to end.

Wider chain: `migrate --check` reports **schema is current** locally with 36
declared migrations; this file is deliberately **undeclared** (applied via
`apply_sql`), consistent with the project's rule that a migration is declared in
the same change that applies it everywhere. Four historical out-of-order
inversions persist and are unrelated to these objects.

---

## 5. Production compatibility

| Change | Old app + new schema | New app + old schema | Class |
|---|---|---|---|
| `outcome` column | writes NULL, reads named columns — fine | INSERT names a missing column → caught by `_log_dispatch`'s except → **no audit row at all** | **migration-first** |
| `principal` column | same | same | **migration-first** |
| capability registry | old app ignores the table | empty registry → permissive path | either order |
| write principal gate | not present in old app | enforced regardless of schema | either order |
| write param schemas | not present in old app | enforced regardless of schema | either order |

**Verdict: migration-first is required.** Deploying the app first silently
disables the dispatch trace — which fails in the invisible direction and would
look like "no traffic" rather than "no recording."

Backward compatibility of the migration itself is total: the old build never
names the new columns.

---

## 6. Is the capability seed safe?

Inspected the generated contents, not just the count.

```
45 rows · enabled=true on all · notes = "<kind> · owned by <agent>"
allowed_callers IS NULL on all 45          ← the seed invents no policy
```

**Confirmed: `cap.agent` is NOT transformed into `allowed_callers`.** It appears
only in the human-readable `notes` string. `test_31` asserts
`count(*) WHERE allowed_callers IS NOT NULL = 0`.

**Re-run safety, verified by reading the statement:** the seed is
`INSERT … ON CONFLICT (intent) DO NOTHING`. Therefore re-running:

- does **not** duplicate rows (`intent` is the primary key);
- does **not** change an operator's decisions;
- does **not** re-enable a disabled capability;
- does **not** overwrite manually modified governance state.

The corollary is a real trap, and the test suite hit it: **`sync` is not a
reset.** A teardown that called `sync()` believing it restored state left a
disabled row disabled and broke the next test. `_reset_registry()` in the
invariant suite now DELETEs first. Any operator runbook must say the same.

---

## 7. Kill-switch path

Verified end to end in `test_33`, and each hop separately:

```
operator disables       POST /a2a/registry/{intent} {"enabled": false}   ✓ exists, admin-gated
registry state changes  capability_registry.enabled = false              ✓
runtime observes        _registry_rows(), TTL 30s                        ✓ ⚠ up to 30s lag
invocation rejected     A2AResult(outcome=REJECTED, error names the note)✓
no DB mutation          returns before the structured/prose branch       ✓
outcome recorded        outcome='rejected' in a2a_dispatches             ✓ (needs the migration)
audit identifies it     /trace/{cid} detail.outcome + detail.error       ✓ (needs the migration)
```

**Against production configuration:** the endpoint is mounted with
`dependencies=_ADMIN`, and Railway has `ADMIN_API_TOKEN` set (confirmed in the
release-guard startup log). The path is therefore reachable there — *once the
migration and seed exist*. Until then the last two rows of that table are not
achievable in production.

**Residual:** the 30-second cache means a disable is not instantaneous.
Acceptable for an operational control; state it in the runbook rather than
discover it during an incident.

---

## 8. Four-state outcome model

| State | Persisted | In API response | In `/trace` | Retry understands | Metrics distinguish |
|---|---|---|---|---|---|
| ACCEPTED | ✓ (after migration) | ✓ `asdict` includes `outcome` | ✓ | n/a — no retry logic | ✗ no metric consumes it |
| REJECTED | ✓ | ✓ | ✓ | n/a | ✗ |
| FAILED | ✓ | ✓ | ✓ | n/a | ✗ |
| UNKNOWN | ✓ | ✓ | ✓ | n/a | ✗ |

**REJECTED never becomes FAILED**: the four refusal sites now name it
explicitly, and `test_14` asserts each by source inspection. Live proof —
Railway-shaped local data shows **120 rejected vs 2 failed**, values that were a
single indistinguishable `ok=false` before.

**UNKNOWN cannot become ACCEPTED**: `test_13` asserts the three ways it arises
never promote. `classify_outcome` requires a 2xx *and* a parsed *and* non-empty
body that does not self-report failure.

**Customer-facing over-claim**: `outcome` is returned in the API response
(`asdict` includes it), so a consumer *can* read it. Nothing in the agent
formatters currently does — they read `ok`/`output`. That is unchanged
behaviour, not a regression.

**Metrics gap**: no metric, dashboard, or alert reads `outcome`. The column is
queryable but nothing watches it. → *required before scale*, not before this
deployment.

---

## 9. Retry semantics

| Outcome | Retry? | Why | Safe? |
|---|---|---|---|
| ACCEPTED | — | nothing to retry | — |
| REJECTED | — | **must not**: mixes non-retryable (bad address, policy) with retryable (a correctable credential) | would be unsafe |
| FAILED | — | genuinely retryable in principle | would need idempotency per capability |
| UNKNOWN | — | **must not assume retryable**: the side effect may have occurred | would be unsafe |

**No retry policy is implemented, and none is invented here.** A repository-wide
search found retry only in `graph_utils` (`max_retries=0`, LLM calls explicitly
disabled), `job_ledger` (a 15-minute disabled-job interval), `leader` (election),
and a Slack `x-slack-retry-num` *header check*. **None consumes A2A outcome.**

One outcome-keyed retry does exist, one layer down: `staff_email.acquire()` and
`order_notifications.acquire()` reclaim `failed` rows and never terminal ones.
That is ledger-level and correct; it predates this work.

---

## 10. Principal propagation — end to end

| Transition | Preserved? | Can disappear? | Can be overwritten? | LLM influence? |
|---|---|---|---|---|
| authenticated user → HTTP | ✓ `Principal.from_session` | if session invalid → None (correct) | no | no |
| HTTP → context | ✓ ContextVar | ✓ if `require_data_access` is not a dependency on that route | no | no |
| context → `A2ARequest` | ✓ `dispatch` inherits | ✓ explicit `None` from a non-HTTP caller | explicit wins over ambient (by design) | no |
| A2ARequest → specialist | ⚠ **NOT propagated over the prose/ASGI hop** | ✓ | — | — |
| capability → SP | ⚠ **not passed as a parameter** | ✓ | — | — |
| → audit record | ✓ `a2a_dispatches.principal` | only if the migration is absent | no | no |

### Explicit tests

```
missing principal        → write REJECTED           test_20 ✓
forged principal         → impossible over the API: _DispatchBody has no
                           principal field; only the ContextVar sets it     ✓
principal from prose     → params never read for it test_25 ✓
principal from agent name→ from_agent is separate; never coerced           ✓
mismatched/leaked        → cleared context yields None, no bleed  test_23/24 ✓
approved action          → built from decided_by     ⚠ see B1
```

**The two ⚠ rows are the honest limit of this fix.** The principal reaches the
*mesh boundary and the audit record*. It does **not** reach the specialist agent
over the in-process ASGI hop (which carries only `{chatInput:{message}}`), and it
is not passed into stored procedures. So a capability cannot yet make a
per-principal decision. That is the *next* boundary, not this one — and the
review that recommended this change said so explicitly: *"the point is that
identity travels, not that a new policy engine decides."*

---

## 11. `decided_by` — B1, the first blocker

**Who can populate it:** the request body. `_Decision.decided_by: str = "human"`,
posted to `/governance/approve/{uuid}`. The endpoint is admin-gated, so the
*caller* is authenticated — but the *value* is free text they choose.

**The one-click path hardcodes it:** `approve(g, decided_by="email-link")`,
authorised by HMAC token possession.

**What is actually recorded**, measured on 118 local approvals:

```
policy:web_order_cancel     46   ← a POLICY auto-decided it
system                      36   ← expiry
email-link                  13   ← a CHANNEL
admin@conscestra.local      12   ← an actual identity
policy:voice_order_cancel    3
ui-test@local                1
(null)                       7   pending
```

**Only 12 of 111 decided approvals name a person.**

My remediation builds `Principal(kind="user", id=decided_by)` from all of them.
The audit column meant to answer *"who initiated this"* will therefore say
`user:policy:web_order_cancel`, `user:system`, `user:email-link`.

**Classification.** Not an authorization failure — execution still requires an
admin session or a valid HMAC token, and that is unchanged. It is an
**observability-correctness defect**: the record asserts a category (`user`) that
is false for ~89% of decided approvals. By this codebase's own standard — *a
system must never use its own unverified record as proof* — **a false record is
worse than an absent one**, and before this change the field was absent.

**Can `govern_bypass=True` execute without legitimate human approval identity?**
Authorization: **no** — a gate always precedes it. Attribution: **yes** — the
recorded identity is frequently not a human. The brief asks for a blocker if the
former; it is the latter, so I classify it as a **must-fix-before-deploy
observability blocker**, not a security blocker.

**Recommended fix (not implemented — read-only audit).** Derive `kind` from the
shape rather than asserting `user`:

```
decided_by startswith "policy:"  → kind="policy"
decided_by == "system"           → kind="service"
decided_by == "email-link"       → kind="token"    (HMAC possession)
otherwise                        → kind="user"
```

Five lines, and the audit column stops lying. `assigned_executive_id` is *not* a
usable fallback: 0 approved rows carry one.

**Other `decided_by` properties, verified:** `_set` uses
`COALESCE(%(by)s, decided_by)` so it cannot be nulled once set; re-decision is
blocked by a `status != 'pending'` guard, so a link cannot be replayed; the HMAC
binds (approval, action).

---

## 12. Write schema coverage — the nine

| Capability | Reachable via HTTP | Existing validation | Risk | Action |
|---|---|---|---|---|
| `email.send_payment_reminder` | **yes** — `/email-chat` | required-field guard present; `is_email_verified` gate | low | add schema |
| `data.erase_record` | no endpoint | required guard present | low | add schema |
| `identity.materialize_link` | no endpoint | required guard present | low | add schema |
| `kb.publish` | no endpoint | required guard present | low | add schema |
| `data.merge_contacts` | no endpoint | `limit` only, bounded in SQL | **negligible** | skip — duplication |
| `data.normalize_phones` | no endpoint | `limit` only, bounded in SQL | **negligible** | skip — duplication |
| `supervisor.emit_dunning` | no endpoint | no params | **negligible** | skip — nothing to validate |
| `supervisor.emit_hot_leads` | no endpoint | no params | **negligible** | skip — nothing to validate |
| `tuning.adjust` | no endpoint | bounds enforced in the SP | low | add schema |

**Eight of nine have no HTTP endpoint** — they are structured-only, reachable
through `/a2a/dispatch` (admin-gated) or internal callers. So this is
**5 genuinely missing protections and 4 cases where a schema would duplicate
validation that already belongs elsewhere** — which is the distinction the brief
asked for. Not a deployment blocker.

---

## 13. Direct `_sp_*` access — refining the previous claim

The prior review said 31 of 32 handlers are directly importable, and used that
to classify Agent RBAC as a blast-radius control. **That classification stands,
but the reachability claim needs narrowing.**

| Path | Reaches `_sp_*` directly? |
|---|---|
| HTTP routes | **no** — no route imports one; a repo-wide search finds only unrelated `_sp_`-prefixed names |
| MCP `dispatch_intent` | **no** — it POSTs `/a2a/dispatch`, so it passes every gate |
| Tests | yes, by import |
| Any in-process Python | yes, by import |
| Plugin / dynamically loaded code | **none exists** — see §14 |

**So the bypass is real in principle and unexercised in practice: no current
code path reaches an `_sp_*` handler except `dispatch()`.**

**A sharper reason RBAC is not a perimeter:** `/a2a/dispatch` accepts
**caller-supplied `from_agent`**. If `allowed_callers` were populated, any admin
caller could satisfy it by declaring the permitted agent name. `allowed_callers`
constrains *first-party components that declare themselves honestly* — which is
a blast-radius control by construction, not an authorization boundary.

---

## 14. The Agent-RBAC trigger — does it exist today?

**Trigger 1 — externally authored `custom_agents`: NO.** `custom_agents`
mounts an `admin_router` (gated with `_ADMIN`) for authoring and a `public_router`
for *running* published agents. Creation requires the admin token. 3 rows local,
2 on Railway, all first-party.

**Trigger 2 — U4 letting an authored agent act: NO, and for a reason worth
recording.** `agent_capabilities.py:326` constructs:

```python
req = a2a.A2ARequest(capability=capability, params=params, caller=f"agent:{slug}")
```

`A2ARequest` has neither `capability` nor `caller`. Verified:

```
TypeError: A2ARequest.__init__() got an unexpected keyword argument 'capability'
```

The call is wrapped in a bare `except Exception` that records `outcome=ERROR`.
**The authored-agent READ path has therefore never executed a capability** — a
constructor mismatch has been silently reported as a capability failure. It is
pre-existing, unrelated to this remediation, and 0 grants exist
(`agent_capability_grants` = 0 rows on both databases), so nothing has been able
to exercise it.

> **Neither trigger exists. Deferring `allowed_callers` as a perimeter remains
> justified.** It is now *more* justified than the previous audit assumed, since
> the path that would have met trigger 2 does not function.

This should be logged as a separate P2 defect — not because it is dangerous
today, but because the day someone fixes those two kwargs, an untested execution
path opens with no RBAC behind it.

---

## 15. Fail-open inventory

| Condition | Behaviour | Justified? |
|---|---|---|
| missing registry row, registry seeded | **REFUSED** | ✓ the fix |
| **registry entirely empty** | **PERMISSIVE** | ✓ documented — an unseeded DB cannot distinguish "nothing permitted" from "seed not run"; logs loudly. **But this is Railway's state today** |
| missing principal, WRITE | REFUSED | ✓ |
| missing principal, READ | ALLOWED | ✓ deliberate — public browsing |
| no `params_schema` | unvalidated | ⚠ 9 of 16, §12 |
| missing `outcome` column | audit row silently not written | ⚠ makes app-first deployment lossy — §5 |
| registry cache TTL 30s | stale permit window | ✓ operational, document it |

No environment variable can revert the principal or outcome behaviour — neither
is flag-gated. That is deliberate and correct: a safety property behind a flag is
a safety property somebody turns off.

---

## 16. Observability after remediation

| Question | Answerable? | From |
|---|---|---|
| Who? | ✓ *after B1 is fixed* | `a2a_dispatches.principal` |
| Which agent? | ✓ | `from_agent` / `agent` |
| Which capability? | ✓ | `intent` |
| Which action? | ✗ | params are not recorded (deliberate — privacy) |
| Authorized? | ~ | inferable from `outcome=rejected` + `error`, not recorded as a field |
| Approved by whom? | ~ | `action_approvals.decided_by`, with B1's caveat |
| What happened? | ✓ | `outcome` |
| What outcome? | ✓ | four states |
| Why? | ✓ | `error` carries the gate's own message |

**Required for this deployment:** B1 only.
**Required before scale:** a metric or alert that reads `outcome` — nothing
watches the column today.
**Strategic:** recording *which gate* fired as a field rather than as prose in
`error`.

---

## 17. Data integrity — pre-migration

```
Railway  a2a_dispatches        486 rows · 240 kB · 3 existing indexes
         outcome / principal   columns absent → every row NULL after ADD
         CHECK compatibility   admits NULL → 0 rows can violate
         capability_registry   0 rows · 0 with non-null allowed_callers
         lock                  ACCESS EXCLUSIVE, but catalogue-only: two
                               nullable ADD COLUMNs with no DEFAULT do not
                               rewrite the table
         duration risk         negligible at this size
```

The migration was **not** declared safe on an empty dev database — it was
validated against the actual production row counts above.

---

## 18. Deployment gate checklist

- [x] migration reviewed — §2
- [ ] **migration applied to Railway** — 0 of 2 columns present
- [ ] schema verified on Railway
- [ ] 45 registry rows on Railway — **currently 0, and no supported path (B2)**
- [ ] registry contents verified (`allowed_callers` NULL on all)
- [ ] closed-by-default verified on Railway
- [ ] operator kill-switch verified on Railway
- [ ] outcome persistence verified on Railway
- [ ] principal propagation verified on Railway
- [ ] **approval identity verified — blocked by B1**
- [ ] production smoke tests
- [ ] rollback strategy documented — *drafted in §2; not yet exercised*

**Eleven of twelve unmet. NOT READY.**

---

## 19. Executive answers

### 1. What was fixed?

Four confirmed findings. The dispatch audit now persists a four-state `outcome`
and a `principal` instead of a boolean — **120 rejections are now
distinguishable from 2 failures**, values that were one indistinguishable
`ok=false`. Identity travels from the authenticated boundary to the mesh and
into the audit record, and writes refuse without it. The capability registry is
seeded and closed-by-default, arming a kill switch that had never fired. Seven of
sixteen write capabilities validate required and unknown parameters. Two
self-inflicted defects — a write gate that broke the approval flow, and an RBAC
seed derived from ownership rather than traffic — were caught by tests and are
now guarded.

### 2. What remains deliberately unfixed?

`allowed_callers` is NULL on all 45 (§14 — neither trigger exists). Orchestrator
recovery (P1-3). Nine write capabilities without a schema, of which **four are
deliberate non-duplication** rather than gaps. Params and gate-identity are not
recorded in the trace. The principal does not yet cross the ASGI hop into
specialists or into stored procedures.

### 3. Single biggest remaining architectural risk?

**The principal reaches the audit record but not the decision.** A capability
still cannot make a per-principal choice — `Principal` stops at the mesh
boundary. Every per-user feature (record ownership, "my accounts", delegated
authority, real multi-tenancy) needs it one layer deeper.

### 4. Why is that risk acceptable now?

Because the expensive half is done. The costly, irreversible part of retrofitting
identity is threading the *field* through ~40 call sites and an in-process
transport; that now works and is tested. Extending it into capability signatures
is additive and mechanical. Meanwhile authorization has not regressed: it is
still enforced where it always was — `write_guard` at the SQL choke point, with a
PostgreSQL read-only transaction and a fail-closed `customer_scope` — which does
not depend on the principal at all.

### 5. What concrete event makes it unacceptable?

The first feature whose *correctness* depends on which user is asking — not
which role. Concretely: a record-ownership filter, a per-user rate limit, or a
second tenant. Any of those makes a role-scoped guard produce plausible, wrong
answers rather than refusals.

### 6. What must the human operator do before production deployment?

1. **Fix B1** — five lines in `governance._execute_approved`, so the audit stops
   recording policies and channels as users.
2. **Decide B2** — either add a `POST /a2a/registry/sync` endpoint (matching what
   the migration already tells operators to call), or call
   `sync_capability_registry()` against the Railway DSN from a local process and
   record that this was done out of band.
3. Commit and merge — 7 modules, 1 migration, 2 test files.
4. **Migration first**, then deploy, then seed, then verify (§4/§5).
5. Verify each checklist item in §18 against the running process.

### 7. What evidence proves production is running the new architecture?

Not a green branch. Four independent signals, in order:

```
1  GET /health                     → commit != dfb249b7d0a9   (new build live)
2  GET /a2a/registry               → 45 rows, allowed_callers null   (seeded)
3  SELECT outcome, count(*) FROM a2a_dispatches
     WHERE at > <deploy time>      → non-null values appearing   (persisting)
4  disable one capability, dispatch it, re-enable
                                   → outcome='rejected' in the trace   (armed)
```

Signal 3 is the one that cannot be faked by configuration: it requires the new
code, the new schema, and real traffic simultaneously.

```
CODE CORRECT      ✓  2071 tests, 40 new invariants
MIGRATION APPLIED ✗  0 of 2 columns on Railway
PRODUCTION DEPLOYED ✗  build dfb249b7d0a9 predates the remediation
PRODUCTION VERIFIED ✗  no signal above has been observed
```

---

# ADDENDUM — B1 and B2 resolved (2026-08-25)

Both blockers are fixed in code and verified locally. The verdict above moves
from **NOT READY** to **READY PENDING TWO OPERATOR ACTIONS**, both of which are
deliberately outside what this work may perform: applying a migration to Railway
and deploying. Neither was done.

## B1 — the principal now tells the truth about its own category

`app/core/governance.py` gained `principal_for_decider()`, and `_execute()` now
builds its principal through it. `decided_by` is a mixed vocabulary, and the
previous code stamped `user:` on all of it:

| `decided_by`               | was            | now                        |
|----------------------------|----------------|----------------------------|
| `policy:web_order_cancel`  | `user:policy:…`| `policy:web_order_cancel`  |
| `system` (expiry sweep)    | `user:system`  | `service:governance-expiry`|
| `email-link`               | `user:email-link` | `token:email-link`      |
| `alan@conscestra.local`    | `user:…`       | `user:…` (unchanged)       |
| `NULL` / blank             | `user:`        | `service:governance`       |

`Principal.kind` gained `policy` and `token`, and the docstring now enumerates
all five kinds rather than the three it listed while emitting four.

The property that matters is not the formatting: **no automatic decision is
attributable to a person any more.** That is the difference between an audit
trail and a fabricated one, and it is asserted directly
(`test_no_automatic_decision_is_labelled_a_user`).

## B2 — the registry seed is reachable, and armed without a human remembering

Three additions, each closing a different half of the gap:

1. **`POST /a2a/registry/sync`** — the endpoint `sql/a2a_outcome_and_principal.sql`
   already instructed operators to call. It did not exist, so the instruction was
   unfollowable. Admin-gated like the rest of the router.
2. **A startup seed in `lifespan`** — so a fresh environment arms the gate on
   first boot rather than sitting permanently in the permissive
   empty-registry exception.
3. **`GET /a2a/registry/observed-callers`** — the evidence needed before
   narrowing `allowed_callers`, which is the mistake F3a already made once.
4. **`release_guard._check_capability_registry`** — advisory, not blocking, so
   that "armed" and "unarmed" stop looking identical in the logs.

### Two defects found while verifying the fix, not after shipping it

**Route shadowing.** The new route was first placed after
`POST /a2a/registry/{intent}`. Starlette matches in declaration order, so
`/a2a/registry/sync` was captured as `intent="sync"` — answering 422, or a 200
carrying `unknown capability 'sync'` if a body was sent. B2 would have appeared
fixed and remained entirely inert. The static routes now precede the
parameterised one, with a comment saying why, and the test asserts through the
ASGI stack because no direct function call can fail this way.

**Guard ordering.** The seed was first placed *after* `release_guard.enforce()`.
The guard would then read an empty registry and report the gate INERT some
milliseconds before the seed armed it — a false alarm on the first boot of
every new environment, which is the one boot anybody reads. The seed now runs
first, and a test pins the ordering (`test_startup_seed_runs_before_the_release_guard`).

## Verification

30 new tests in `tests/test_b1_b2_principal_and_registry.py`; full suite
**2101 passed, 1 skipped** (baseline 2071 + 30, same single skip).

Eight mutation experiments, each reverting one half of the fix, **all detected**:
every-decider-is-a-user, the double `policy:` prefix, governance dropping its
principal, the endpoint removed, sync made destructive, the startup seed removed,
and the guard silenced in each of its two reporting branches.

The registry scenarios assert `rowcount` on their own setup SQL. An earlier run
of these same scenarios passed for the wrong reason: it disabled and deleted the
intent `crm.query`, which does not exist, so every statement touched zero rows
and the guard dutifully reported an unchanged registry as healthy. That is the
house failure mode — *absence of an error read as evidence of success* — and it
appeared again here, inside the very work meant to remove it.

## What is still required of the operator, in this order

1. **Apply `sql/a2a_outcome_and_principal.sql` to Railway.** Still local-only:
   Railway has 0 of 2 columns.
2. **Then deploy.** The order is load-bearing and the failure is silent:
   `_log_dispatch` writes `outcome` and `principal`, catches every exception at
   *debug* level, and uses its own connection. Deploying first does not break
   dispatch — it makes every trace row vanish quietly, so `GET /trace/{cid}`
   goes dark without an error anywhere.
3. **Confirm the seed.** The startup seed runs automatically; `POST /a2a/registry/sync`
   is there for the case where step 1 ran after the deploy. Expect 45 rows.
4. **Read the guard line.** `capability_registry: 45 of 45 capabilities
   registered; closed-by-default ACTIVE` is the signal the gate is real. Anything
   else means it is not.

`REQUIRED_MIGRATIONS` deliberately still does **not** name this migration. That
line belongs in the same change that applies it, never before — declaring an
unapplied migration is how the ledger starts lying.

**Not committed. Not deployed. Railway state unchanged.**

---

# ADDENDUM 2 — post-audit fixes (2026-08-25)

Three defects the gate audit surfaced are now fixed. Full suite **2113 passed,
1 skipped** (2101 + 12 new). Five mutation experiments, all detected. Still not
committed, not deployed; the Railway migration is still not applied.

## Fixed — the principal vocabulary no longer collapses customers into staff

`Principal.from_session()` returned `kind="user"` for every session, including
ones identified by `contact_id`. That is B1's defect in the other half of the
vocabulary, and it made the migration's own column comment false, since the
comment promised `customer:<contact_id>` for a kind the code never wrote.

**The discriminator is deliberately not `source_table` alone.** Every session on
this database is `source_table='leads'` with `role='admin'` — staff sign in
through lead-backed credential rows — so keying the kind on the table would
have relabelled 18 real administrators as customers: a new false category
introduced by the change meant to remove one. The rule is
`source_table='contacts'` (set only where an account_id resolved) **and** a role
outside `WRITE_ROLES`, which makes it fail *toward* `user`. A misread lands on
the previous behaviour, never on a new falsehood.

## Fixed — the migration comment now names the vocabulary the code writes

It published `user | service | customer` — naming a kind never minted and
omitting `policy` and `token`, which are. It now names all five and says why
`policy` and `token` exist. A test extracts the comment from the SQL and fails
if the code mints a kind the comment does not name, so the two cannot drift
apart silently again.

## Fixed — the unwired dedup helpers now declare that they are unwired

`alert_key()` and `recent_duplicate()` are called only from tests.

**They were deliberately not wired, and the restraint is the fix.** There is no
sender to attach them to: the repeats are `supervisor.alert` *notifications*,
`EMAIL_KINDS` has no `alert` member, and `escalation_remind` has no producer.
Wiring them into the escalation sender would have been theatre — Railway has
never recorded a single escalation, and escalation ledger rows are keyed per
escalation uuid, so re-keying them by content would make two genuinely distinct
escalations sharing a summary collide *permanently* rather than for a cooldown.

Both docstrings now state **STATUS: NOT WIRED**, name why, and specify what a
future caller must do to its idempotency key (the content ref must be a prefix,
since `recent_duplicate` matches on `kind:ref%`). The 28-of-76 duplicate
interrupt problem remains unsolved, and now says so in the place someone would
look.

## Added — route shadowing is now a standing test, not a one-off inspection

A sweep asserts that no static route in the application is declared after a
parameterised sibling that would capture it. It runs over all 429 routes, and a
companion test rebuilds the pre-fix ordering to prove the sweep would actually
have caught the original bug — a guard nobody has watched fail is a guard
nobody should trust.

## Not fixed, and why

- **231 of 243 test setup mutations across the suite assert nothing about what
  they touched.** Pre-existing, unrelated to this release, and a mechanical
  change of that size during a deployment window trades a known risk for an
  unknown one.
- **20 other silently-swallowed database writes.** Only `_log_dispatch` depends
  on new schema; the rest are unchanged by this release and are governed by the
  migrate-before-deploy invariant already stated.
- **`notification_headline.sql` applied to both databases but in neither
  ledger.** Correcting it means recording a migration as applied, which is a
  ledger mutation — and the rule here is never to mutate the ledger to make a
  check green. It needs its own change, with the ledger write as the point of
  the change rather than a side effect.
