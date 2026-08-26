# Canonical Migration-Integrity Model — Specification

**Status: DESIGN ONLY. Nothing implemented. §7 lists the governance inputs that
must be settled before any code is written.**

Supersedes the situation in which `scripts/migrate.py --check` and
`app.core.deploy_state.check_migrations()` both report "current" while measuring
different, non-overlapping properties of the same ledger.

The discrepancy is caused by a semantic mismatch between two individually valid
checks. That mismatch is nevertheless a **system-level defect**, because both are
consumed as if they represented the same migration-health state.

---

## 1. Three axes, not one boolean

The current model compresses several unrelated truths into `ok` plus one
free-text `note`, which is how production came to report `ok: false` alongside
`"schema is current"`. The replacement separates them:

```
                     MIGRATION INTEGRITY
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
    PROCESS HISTORY     SCHEMA SAFETY      GOVERNANCE
          │                  │                  │
     out_of_order          replay          undeclared
          │                  │           checksum_unverifiable
          │                  │                  │
       warning             GATE              warning
          │                  │                  │
   known_deviations    CI / release        disposition
```

**A deviation on the history axis is not evidence about the safety axis.**
Measured on the three historical deviations: every object they reference is
created by an earlier-declared migration, so their declared positions were
satisfiable and the late application was harmless *in those cases*.

**Selection bias, stated explicitly.** A migration applied genuinely before its
dependency existed would normally have raised at apply time and never been
recorded. A dependency check computed against the *ledger* is therefore close to
empty by construction, and its emptiness is not evidence of safety.

---

## 2. The five dimensions

| # | Dimension | Question | CLI today | App today | Axis |
|---|---|---|:--:|:--:|---|
| D1 | Missing declared | Is every manifest entry in the ledger? | yes | yes | safety |
| D2 | Checksum integrity | Was a file edited after being applied? | yes | no | safety |
| D3 | Application order | Did apply order match declared order? | no | yes | history |
| D4 | Undeclared entries | Is anything applied the manifest does not declare? | **no** | **no** | governance |
| D5 | Duplicate entries | Is a filename recorded twice? | — | — | impossible |
| **D6** | **Corpus classification** | **Does every `sql/*.sql` declare which path it belongs to?** | **yes** | **yes** | **governance** |

D5 is structurally impossible — `schema_migrations_pkey` is on `filename`.
D4 is unmeasured by both mechanisms and production currently has two.

**D6 was added 2026-08-25 and is implemented.** See §10.

### Why D6 had to exist

D1–D5 are all computed from the ledger and the manifest:

```
D1  missing declared        ledger ∩ manifest
D2  checksum integrity      ledger ∩ manifest
D3  application order       ledger
D4  undeclared entries      ledger − manifest
D5  duplicates              impossible (PK)

    NOT COVERED             applied − ledger
```

A SQL file that changed production **without ever entering the ledger** is
outside all five. That is not a hypothetical: three successive replacements of
`trg_fn_events_after_insert()` reached production that way, and every mechanism
in the system reported healthy throughout. `migrate.py --check` iterates
`REQUIRED_MIGRATIONS`, `ledger_health()` divides by `REQUIRED_MIGRATIONS`, and
`postdeploy_verify` compared `relkind='r'` only.

D6 closes it by changing the denominator: it is computed from the **directory**,
not from the ledger. Computing it from the ledger would reproduce the blind
spot exactly, because the population at issue is the files the ledger never
saw.

---

## 3. Exact definition of dependency violation

**A dependency violation is a replay failure, detected — never inferred.**

    REPLAY TEST
      given: an empty database, and REQUIRED_MIGRATIONS in declared order
      when:  each file is applied in sequence
      then:  every file completes without error
    A failure attributable to a missing relation, function, type or column
    IS the dependency violation. Its identity is the failing file plus the
    PostgreSQL error.

**Static name-analysis is rejected as a safety mechanism.** It was tried during
the investigation and produced a *confidently wrong* verdict: matching referenced
identifiers against created identifiers reported that all three out-of-order
files depended on later-declared ones. Establishing the *first* creator of each
object reversed the finding entirely — none of them did.

The failure is structural, not an implementation slip:

  * `CREATE OR REPLACE FUNCTION` bodies are not validated at creation, so a
    function may legally reference a table that does not yet exist;
  * `IF NOT EXISTS` and idempotent patterns mask ordering entirely;
  * base-schema objects have no declaring migration and would read as
    unsatisfied.

Static analysis MAY remain a fast advisory pre-check. It MUST NOT set `failed`.

---

## 4. The corpus is not in the repository

**Highest-priority architectural finding. The replay gate cannot exist until this
is resolved.**

```
tracked files under sql/ : 0
.gitignore rule          : /sql/
files on disk            : 220
```

> **The repository cannot reproduce the production schema, because the migration
> corpus is not version-controlled.**

Two consequences:

**Replay cannot gate a merge.** CI has nothing to replay. The question replay
exists to answer — *can this repository reconstruct the schema it claims to
define?* — currently answers **no**, for want of the definition.

**Checksum validation is a local-machine property, not a repository property.**
`schema_migrations.checksum` records a hash of a file that exists on one laptop.
No other developer and no CI runner can independently establish what those hashes
are supposed to represent, or detect that a file changed.

    Production ledger
           │
           └── checksum ──▶ file on one laptop
                                  │
                        not necessarily available
                             to anyone else

So D2 currently detects:

    "Has this local copy changed since application?"      ← yes, reliably

but NOT:

    "Is this still the canonical migration file?"         ← cannot answer

Those are fundamentally different guarantees, and only the second is what a
schema-integrity control is normally assumed to provide.

This is a **repository-governance** question — is `/sql/` the authoritative
schema source, and should it be tracked? — and it is deliberately **out of scope**
for the migration-integrity implementation. See §8.

### 4a. Layout constraint — migrations are flat in `sql/`

The tooling resolves a declared migration by bare filename against one directory:

```python
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"   # scripts/migrate.py:33
path = SQL_DIR / name                                   # :96, :120
```

`REQUIRED_MIGRATIONS` holds bare filenames with no path component. A declared
file placed in a subdirectory is therefore **not found**, and `--check` reports
`MISSING FILES` and exits 1. Two categories follow, and they must not be
conflated:

| Category | Location | Rule |
|---|---|---|
| **Migration** — the ledger tracks it, `migrate.py` must find it | flat in `sql/` | **non-negotiable** while the tooling assumes a flat directory |
| **Trigger-function reference** — canonical definitions, grouped for reading | `trg_fn/` | organisational only; never the path a migration resolves |

**The overlap case is the hazard.** A file can be both — `executives_audit_and_touch.sql`
is a declared migration *and* the definition site of `trgfn_touch_updated_at`
and `trgfn_audit_row_history`. For such a file:

  * the migration path **wins** — it stays flat in `sql/`;
  * any `trg_fn/` presence is a pointer, never a second copy.

**Two copies of a trigger function in different folders is the worst outcome.**
Nothing declares which is authoritative, they drift silently, and the drift is
invisible to every check in this document — `checksum_drift` (D2) compares the
ledger against `sql/<name>` only, so an edited copy under `trg_fn/` would never
be compared to anything. That is the same shape as the one-laptop checksum
problem in §4: a control that appears to cover a file it cannot see.

The alternative — teaching `migrate.py` to resolve subdirectories — is a small
change to migration tooling and therefore a decision, not a detail. It is listed
in §8.

---

## 5. Result contract

One producer. Both callers consume it unchanged.

```
{
  "status": "healthy" | "warning" | "failed",

  "missing":               [filename, ...],   # D1
  "checksum_drift":        [filename, ...],   # D2 — known checksum != file
  "checksum_unverifiable": [filename, ...],   # D2 — empty checksum recorded
  "files_absent":          [filename, ...],   # D2 — declared, not on disk
  "out_of_order":          [filename, ...],   # D3 — a LIST, not a bool
  "undeclared":            [filename, ...],   # D4
  "replay":                "pass" | "fail" | "not_run",
  "dependency_violations": [{file, error}, ...],

  "acknowledged": [finding, ...],   # matched a known_deviations entry
  "errors":       [human-readable, ...],
  "warnings":     [human-readable, ...]
}
```

`out_of_order` becomes a list because "which ones" is the first question an
operator asks and a boolean cannot answer it.

**There is no `note` field.** A single sentence derived from one dimension is
what produced `ok: false` beside `"schema is current"`. Callers render from
`errors` and `warnings`, which cannot disagree with `status`.

---

## 6. `known_deviations`

Accepted findings are declared, with a reason and a date:

```yaml
known_deviations:
  - type: out_of_order
    migration: memory_invariants.sql
    reason: "Applied after declared successors on 2026-08-01. Dependency-safe:
             every object it references is created by an earlier-declared file."
    acknowledged_at: 2026-08-13
```

    out_of_order + known_deviation  ->  acknowledged (quiet)
    out_of_order + no disposition   ->  NEW warning  (loud)

**Acknowledgement never changes the underlying fact.** The finding still appears
in `out_of_order`; only its disposition changes. `acknowledged` is an additional
field, never a subtraction from the evidence.

`known_deviations` is an **audit disposition, not a suppression mechanism**. It
must never become equivalent to `ignore_this = true`. An acknowledged deviation
remains evidence; it merely stops being *new, unreviewed* evidence.

This exists to prevent warning-wallpaper. Three permanent warnings become seven
in a month, and nobody reads the seventh — the same uselessness as a permanently
red gate, arrived at more slowly.

---

## 7. Status mapping

**failed** — the schema is untrustworthy or cannot be rebuilt:
  * `missing` non-empty
  * `checksum_drift` non-empty
  * `files_absent` non-empty
  * `replay == "fail"` / `dependency_violations` non-empty

**warning** — the schema is complete and correct; governance has drifted:
  * `out_of_order` non-empty
  * `undeclared` non-empty
  * `checksum_unverifiable` non-empty

**healthy** — none of the above.

Production as it stands today:

```
status: warning
missing: []                     checksum_drift: []
out_of_order:          memory_invariants.sql, governed_mutation.sql,
                       memory_audit_erasure.sql
undeclared:            promotions_coupons.sql, railway_catchup_20260805.sql
checksum_unverifiable: promotions_coupons.sql, railway_catchup_20260805.sql
replay:                not_run
```

> **`status: warning` does not mean the production schema is unsafe. It means
> production's migration governance contains unresolved historical drift.**

---

## 8. Governance inputs — required before implementation

1. **`warning` never blocks a release.** `failed` blocks; `warning` reports and
   is preserved as machine-readable output. Rationale: a permanently-red pipeline
   creates an incentive to falsify history to recover green — precisely what this
   model exists to prevent.

2. **Accepted warnings require a `known_deviations` entry.** No silent
   suppression; no unacknowledged permanent warning.

3. **Replay is triggered by `sql/**` changed OR `REQUIRED_MIGRATIONS` changed** —
   not manifest-only. Editing a `.sql` file without touching the manifest is
   exactly the case D2 exists to catch. Plus a scheduled nightly full replay.
   Ordinary application-code changes require no replay.

4. **Replay cannot become a CI gate until the canonical corpus is available to
   CI.** Most likely by deciding whether `/sql/` leaves `.gitignore`. Until then
   replay is `not_run` and reports only. See §4.

5. **The two undeclared files are adopted IF AND ONLY IF they are intended schema
   definition.** Adoption asserts *"a clean database should execute this"* — a
   stronger claim than *"this once ran in production."* If they were one-time
   production repairs, record them explicitly as out-of-band instead. What is not
   acceptable is the third state: present, undeclared, undocumented.
   *Measured: adoption is position-independent — chronological and appended
   placements yield an identical `out_of_order` set, so placement is not a
   decision variable. Adoption surfaces one further deviation (`app_role.sql`),
   which is honest: it was always inside that timestamp tie.*

6. **Empty historical checksums remain unverifiable forever**, unless genuine
   evidence recovers the original bytes. Adopting today's hash would record
   *"this is the exact content applied on 2026-08-05"* — a fabricated historical
   claim. `applied: true, checksum: unknown` is strictly more truthful than a
   confident wrong hash.

7. **Declared migrations stay flat in `sql/` (§4a).** `trg_fn/` is reference
   grouping, never a resolution path. A file that is both a migration and a
   trigger-function definition lives in `sql/`; any `trg_fn/` presence is a
   pointer, not a copy. **Open question:** should `migrate.py` learn to resolve
   subdirectories? That would let layout follow meaning rather than tooling, but
   it edits migration tooling and needs its own review. Until decided, the flat
   rule holds and a violation is caught as `files_absent` → `failed`.

---

## 9. Invariants

> **No production ledger or manifest mutation is permitted merely to make an
> integrity check green.**

This specification provides **no justification** for rewriting the ledger,
reordering `REQUIRED_MIGRATIONS`, deleting ledger entries, or re-running
historical migrations. The declared order is correct as written; the three
historical deviations are a true record of what happened; reordering the manifest
to make the check pass would make it pass by destroying the thing being checked.

> **A migration-integrity fix must not quietly become a repository-governance
> change.**

The `/sql/` tracking decision (§4) is a prerequisite for one capability, not part
of this implementation. It requires its own review and its own authorization.

---

## 10. The classification invariant (D6) — implemented 2026-08-25

### The rule

> Every file in `sql/` carries **exactly one** disposition: *governed migration*
> or *out-of-band operation*. A file with no disposition is an **error**, not a
> default. Only `migrate.py` may apply a governed migration; only `apply_sql.py`
> may apply an out-of-band operation; neither may apply the other's.

A change establishing persistent schema definition — table, column, index,
view, constraint, function, trigger, type or grant — is a governed migration
*unless* its out-of-band classification records why. Data repair against
specific rows is out-of-band by nature: a clean database must not replay it.

### Where it lives

| Concern | Location |
|---|---|
| Governed set | `deploy_state.REQUIRED_MIGRATIONS` (34) |
| Out-of-band set | `deploy_state.OUT_OF_BAND_SQL` (217, filename → reason) |
| Invariant | `deploy_state.classify_sql_corpus()` |
| Boundary | `deploy_state.require_disposition()` |
| Blocking gate | `migrate.py` — refuses to run while anything is unclassified |
| Advisory report | `release_guard._check_sql_disposition` |

```
set(sql/*.sql) == REQUIRED_MIGRATIONS ∪ OUT_OF_BAND_SQL
REQUIRED_MIGRATIONS ∩ OUT_OF_BAND_SQL == ∅
```

### Out-of-band is not a demotion

It records a true fact — *this file is not replayed by `migrate.py`* — and for
101 of the 217 it is the only correct answer: a backfill repairs rows a clean
database does not have. Declaring everything a migration would convert one-time
repairs into permanent obligations.

Most historical files are out-of-band by **observation, not judgement**: none of
them is in the governed chain today. Adopting one asserts *"a clean database
should execute this"*, which is a stronger claim than *"this once ran in
production"* and one the evidence does not support file by file.

### Open dispositions are named, never resolved by default

Five entries carry a reason beginning `REVIEW`. They are exactly the cases where
the stronger claim may in fact be true, and they remain **human decisions**:

| File | Why it is open |
|---|---|
| `fix_event_queue_double_enqueue.sql` | link 1 of 3 replacing `trg_fn_events_after_insert()` |
| `fix_event_emit_guard.sql` | link 2 of 3 |
| `notification_headline.sql` | link 3 of 3 — the current production body |
| `promotions_coupons.sql` | schema definition whose absence caused a 15-day outage |
| `a2a_outcome_and_principal.sql` | prepared migration, not yet applied to Railway |

**Never adopt only the newest member of an incremental schema-definition chain.**
No declared migration defines `trg_fn_events_after_insert()` at all, so adopting
one link would imply a clean database can reproduce production, which it cannot.
The chain moves together or not at all — a test enforces that it is never split.

### What D6 does *not* claim

It does not assert reproducibility. `REQUIRED_MIGRATIONS` is an **incremental**
manifest: no declared migration creates `contacts`, `accounts`, `orders` or
`leads`, and there is no base schema. It means *"these 34 increments have been
applied to an existing database"*, never *"this reconstructs the schema"*.
Judging the ledger against reproducibility is the wrong denominator — the same
mistake `ledger_health()` documents having made once, when it divided by 196
files and reported 12.8% coverage of nothing.

Provenance remains unsolved and is **not** solved by D6: 0 of the 34 governed
migrations exist in version control, so a checksum proves the applied file
matches this laptop, not that it matches a reviewed artifact. That is §8.4's
decision and remains open.
