# Phase 1 — where `PublicRead(s) → ProvenDemo(s)` must be enforced

**2026-09-11. ANALYSIS ONLY. Nothing was changed: no code, schema, configuration,
guard, authorization, environment variable, production data, commit, push or
deploy.**

Baseline: `docs/reassessment_public_read_corpus_2026-09-11.md` (accepted).
Evidence: deployed commit `49c5e9dedf9d`, production read via `crm_readonly`,
and the anonymous public surface.

---

## 1. The safety invariant, restated and not weakened

> **For every customer subject `s`: `PublicRead(s) → ProvenDemo(s)`.**

| Provenance state | Public-read |
|---|---|
| proven synthetic (`state='synthetic'`, rule ∈ `flagged_synthetic`/`seed_generator`/`human_attested`) | **ALLOW** |
| proven real | DENY |
| unknown / no row | DENY |
| unclassified | DENY |
| `ambiguous` / `no_evidence` | DENY |
| contradictory | DENY |
| provenance lookup fails | **DENY — fail closed** |

`¬ProvenReal(s)` is **not** a substitute for `ProvenDemo(s)`. Domain ownership,
`is_synthetic=false`, an absent provenance row, and the absence of a negative
signal are **not** evidence and are not used as such anywhere below.

---

## 2. D-01 — the two options, analysed independently

### Option A — retire anonymous public-read

**Mechanism.** `API_SECURITY_MODE=locked`. One configuration value; the
posture machinery already exists and `auth_dep` already implements `locked`.

**What disappears.** Anonymous reads of every CRM data endpoint — contacts,
accounts, leads, orders, opportunities, activities, analytics. The unauthenticated
demo of the working CRM, which is the product reason the posture exists.

**What remains.** Everything authenticated. The governance plane, ops and deploy
surfaces are already gated (7/7 probed → 403). The store/SDR public surfaces are
separate paths and would need checking individually, not assumed.

**Does P-01 become moot?** **Yes, completely.** `PublicRead(s)` becomes false for
all `s`, so `PublicRead(s) → ProvenDemo(s)` is vacuously true. The invariant
cannot be violated by a system with no anonymous read.

**Regressions.** The marketing/demo motion, which is a product loss and not a
technical one. **DEPLOYMENT-VERIFIED:** `release_guard._check_api_auth` already
handles `locked` and would stop warning.

**Cost.** Effectively zero engineering. The cost is entirely product.

### Option B — retain anonymous public-read, and enforce the invariant

**What it requires.** `ProvenDemo(s)` becomes a mandatory precondition of row
visibility for anonymous callers, at the point where rows are selected — not at
startup, and not in a health check.

**Projected visible set — DATABASE-VERIFIED:**

| Subject | Live rows | Proven demo | **Visible after enforcement** |
|---|---|---|---|
| `contacts` | 129 | 6 | **6** |
| `accounts` | 118 (13 soft-deleted already excluded) | 6 | **6** |
| `leads` | 105 | 0 | **0** |
| **Total** | **352** | **12** | **12 (3.4%)** |

**The demo becomes 12 contacts-and-accounts and zero leads.** That is the correct
behaviour under the invariant — unnecessary exclusion is the safe failure — and
it is also, bluntly, not a demo. Anyone choosing B must choose it knowing the
public surface is nearly empty until subjects are attested.

**Correction to the baseline report.** It said "353 of 365". That counted the 13
soft-deleted accounts as exposed; they are not — the SP already filters
`is_deleted`. The exposed set is **352, of which 340 are unproven**. The finding
is unchanged; the number is now exact.

---

## 3. Trace — where the check must live if B is chosen

```
anonymous POST /contact-chat
  │
  ├─ auth_dep         posture=public-read → read permitted, NO subject knowledge
  ├─ router.py        no per-row authority
  ├─ graph.py         builds intent
  ├─ sql_builder.py   emits  SELECT sp_contacts(...) AS result
  ├─ execute_sp()     ◄── THE ONLY APPLICATION CHOKEPOINT
  │                       already consults request context:
  │                       customer_scope() · readonly_context() · guard_query()
  ├─ PostgreSQL       sp_contacts()  ◄── selects rows AND computes pagination
  ├─ formatter.py     renders
  └─ response
```

### Why `execute_sp` is the wrong place, in its own words

`database.py:436-444` already faces this exact problem for the verified-customer
channel and resolves it by **denying entirely**:

> *"SPs are CRM-wide by construction and **cannot be row-scoped from here**, so a
> customer-scoped context gets no SP access at all."*

Applying that pattern to anonymous callers means anonymous callers get no SP
access — which is Option A implemented one layer lower. It cannot produce
"12 visible rows".

### Why post-query filtering is the wrong place

**CODE-INSPECTED + DIRECTLY OBSERVED.** The SP returns rows *and* the pagination
metadata: the anonymous response carries `Page 2 of 43 | Total: 129 contacts`.
Filtering after the SP would:

- leave `Total: 129` describing a population the caller cannot see — an
  **enumeration disclosure of excluded subjects**, which §6 explicitly forbids;
- produce short or empty pages with no honest page count;
- require the same filter in every formatter, forever.

### Why the application layer cannot carry it at all

**DATABASE-VERIFIED / CODE-INSPECTED.** **50 modules** read `contacts`,
`accounts` or `leads` directly, outside the SP path. At least two are
anonymous-reachable (`sdr.py`, `portal.py`). An application-layer predicate would
have to be applied in 50+ places and in every future one, and its coverage could
only ever be asserted by enumeration — the same control shape this codebase has
already found insufficient for write call sites.

### The enforcement point the architecture already supports

**Row-level security on `contacts`, `accounts`, `leads`.** Four measured facts
make this viable, and all four had to hold:

| Fact | Measured |
|---|---|
| The SPs are **INVOKER** rights (`prosecdef = false`) | DATABASE-VERIFIED |
| `crm_app` does **not** hold `BYPASSRLS` | DATABASE-VERIFIED |
| RLS is currently unused (0 policies, `relrowsecurity=false` ×3) | DATABASE-VERIFIED |
| The codebase already stamps a session GUC on connection borrow | CODE-INSPECTED |

Because the SPs run as the invoker, **a row policy filters inside the SP**. The
SP's own `COUNT` and pagination are then computed over the visible set, so
`Total:` stays truthful with **no SP rewrite** — 238KB of `sp_contacts` +
`sp_accounts` + `sp_leads` source is untouched. RLS also covers all 50 direct
readers by construction.

**This is not a second parallel security mechanism.** It is the mechanism
`execute_sp`'s own comment says is missing ("cannot be row-scoped from here").

### The hazard that would break the obvious implementation

**CODE-INSPECTED — this is the most important finding in Phase 1.**

The existing GUC seam (`database.py:238-254`) stamps `app.correlation_id` with:

```python
set_config('app.correlation_id', cid, false)   # is_local = FALSE → SESSION-scoped
...
if not cid:
    return                                      # leaves any previous value in place
```

Connections are **pooled** (`pool_max 16`). Copying this pattern for an
authorization GUC would be a vulnerability, not a control:

1. session-scoped values **survive into the pool** and are inherited by the next
   request on that connection;
2. the early `return` leaves a **stale value** rather than clearing it.

So an authenticated request that set `anonymous=off`, returning its connection to
the pool, would hand `off` to the next **anonymous** request — exposing unproven
subjects. The failure is silent, intermittent, and load-dependent.

**Required instead:** the GUC must be set **unconditionally on every borrow, in
both directions** (never skipped), or be transaction-local (`is_local=true`)
inside an explicit transaction. And the policy must treat *unset* as
**anonymous**, so a forgotten stamp fails closed toward restriction.

---

## 4. Production impact — measured, not inferred

| Category | Count | Grade |
|---|---|---|
| Total customer subjects (corpus) | 365 | DATABASE-VERIFIED |
| Live / anonymously exposed today | **352** | DATABASE-VERIFIED |
| Proven demonstration | **12** | DATABASE-VERIFIED |
| Proven real | **0** | DATABASE-VERIFIED |
| `ambiguous` / `no_evidence` | **0** | DATABASE-VERIFIED |
| Unclassified (no row) | **340** of the exposed set | DATABASE-VERIFIED |
| Unknown vs unrecoverable, subdivided | **NOT PROVABLE** | — |
| Projected visible under the invariant | **12** | DATABASE-VERIFIED |

No historical provenance was inferred. No production data was mutated.

---

## 5. The three findings

### P-01 · HIGH — root cause

**The root cause is solely the missing exposure-boundary check.** Everything
upstream is correct:

- the classifier preserves uncertainty (verified strength);
- the CHECK constraints make a heuristic unrecordable as evidence;
- the startup guard blocks on proven-real and refuses to start deployed.

What does not exist is any point at which `ProvenDemo(s)` conditions row
visibility. **Even with fully recoverable provenance the invariant would still
fail**, because nothing would consult it. The historical loss determines the
*size* of the violation (340 vs some smaller number), not its existence.

### P-02 · MEDIUM — required independently

**Yes, and it must not be conflated with P-01.**

- **Truthful observability** (P-02) = the operator can see that 12 of 352 are
  proven. This is required whichever D-01 option is chosen, and it is required
  *even if the invariant is enforced* — because after enforcement the interesting
  number becomes "how much of the corpus is invisible", which is the same
  measurement.
- **Security enforcement** (P-01) = unproven subjects are not served.

Fixing P-02 alone would make the log honest while leaving 340 subjects exposed.
**P-02 must never be reported as progress on P-01.**

### P-03 · LOW — has a real governance consequence

Not merely a modelling gap. With `ambiguous`/`no_evidence` unused (0 rows),
*never examined* and *examined, nothing found* are indistinguishable. Under
enforcement both are denied, so it does not affect safety — but it does affect
**remediation**: an operator working through 340 subjects cannot tell which have
already been investigated. It becomes a work-tracking defect the moment anyone
starts attesting subjects, and not before.

---

## 6. Implementation contract — if B is chosen

### Authorization

| Condition | Result |
|---|---|
| Proven synthetic | ALLOW |
| Proven real | DENY |
| Unknown / no row | DENY |
| Unclassified | DENY |
| Missing provenance | DENY |
| Ambiguous / no-evidence | DENY |
| Contradictory | DENY |
| Provenance unavailable / GUC unset | **DENY — fail closed** |

### Behaviour

- **Filtering happens before results are returned** — inside the SP, via RLS, so
  the rows never reach the application.
- **HTTP behaviour is unchanged: 200 with fewer rows.** Not 403. An anonymous
  caller is not entitled to learn that a subject exists and was withheld; a
  refusal would disclose exactly that.
- **Pagination and counts remain truthful** because the SP computes them over the
  filtered set. `Total:` must report the *visible* population.
- **An anonymous caller must not be able to enumerate excluded subjects** by
  count, page arithmetic, ID probing, search, aggregate, or any other endpoint.
- **All three customer-subject paths obey the same property**, plus the 50 direct
  readers, by construction.
- **Caching cannot bypass it** — DIRECTLY OBSERVED: no caching exists on these
  paths, and RLS is evaluated per query regardless.
- **Authenticated/internal access is unchanged.** The policy is keyed to the
  per-request GUC, not to the role; `crm_app` serves both anonymous and
  authenticated traffic.

### Required tests

1. proven synthetic → allowed
2. proven real → denied
3. unknown → denied
4. unclassified → denied
5. missing provenance row → denied
6. `ambiguous`/`no_evidence` → denied
7. contradictory provenance → denied
8. provenance lookup failure / GUC unset → **fail closed**
9. anonymous enumeration cannot retrieve excluded subjects
10. pagination and counts do not disclose excluded subjects
11. all customer-subject read paths enforce the same property, including a
    direct-SQL reader that bypasses `execute_sp`
12. authenticated/internal access is not broken
13. release-guard and governance behaviour intact
14. **pooled-connection inheritance**: an authenticated request followed by an
    anonymous request on the *same pooled connection* must not leak. This is the
    §3 hazard and it will not be caught by any of 1–13.

### Required production verification

An **anonymous request to the deployed boundary** must be shown unable to obtain
an unproven subject: re-run the cross-reference that produced "25 returned, 23
unclassified" and require **0 unclassified**. Unit tests, code inspection,
startup logs and database classification are each insufficient alone.

---

## 7. Conclusion — decision required

### Findings

1. The root cause of P-01 is solely the absent exposure-boundary check.
2. `execute_sp` is a real chokepoint but **cannot row-scope**, by its own design
   note; using it means denying anonymous SP access entirely (= Option A).
3. **RLS is the only mechanism the existing architecture supports** that filters
   rows while keeping pagination truthful and covering all 50 direct readers. It
   requires no SP rewrite because the SPs are INVOKER-rights.
4. The obvious GUC implementation would be **unsafe under connection pooling**.
5. Enforcement leaves **12 of 352 subjects visible**, and **zero leads**.

### The decision only you can make (D-01)

**Is anonymous public-read worth 12 visible subjects?**

### Recommendation: **Option A — retire anonymous public-read.**

Rationale, from evidence rather than from implementation cost:

- **B does not deliver the thing B exists to protect.** The posture exists so a
  prospective client can see the CRM working without a login. Enforced correctly,
  it shows 6 contacts, 6 accounts and no leads. The product reason for accepting
  the risk does not survive the control that makes the risk acceptable.
- **A makes the invariant vacuously true**, closes P-01 completely, and needs no
  new mechanism, no RLS, no GUC, and no exposure to the pooling hazard.
- **A is one configuration value**, already implemented and already understood by
  `release_guard`.
- B is the right answer **only** if attesting a substantial fraction of the 340 is
  realistic. That is a data-provenance project on a corpus whose provenance is
  **NOT PROVABLE** — the attestation would have to be `human_attested`, subject by
  subject, with no evidence to draw on.

If the demo matters more than the corpus, the defensible form of B is a
**separate deployment with a corpus that is synthetic by construction and attested
at generation time** — where `ProvenDemo(s)` is true for every row because the
generator wrote the provenance. That is neither A nor B on this database; it is a
third option, and it is the only one that yields both a working demo and the
invariant.

### If you choose B anyway

Enforcement point: **RLS policies on `contacts`, `accounts`, `leads`**, keyed to a
per-request GUC stamped unconditionally on every connection borrow, defaulting to
anonymous-when-unset. Contract in §6; test 14 is mandatory.

---

## PHASE 1 ENDS HERE

**No implementation has been performed and none is authorized.** Nothing was
changed: the working tree carries only this document and the baseline report;
production remains on `49c5e9dedf9d`; `corpus_provenance` holds the same 32 rows
it held before this analysis began.

Phase 2 requires your explicit authorization and a D-01 decision.
