# Reassessment — `public_read_corpus` and its provenance claim

**2026-09-11. Investigation only. No code, schema, configuration, data, guard,
migration or deployment was changed.**

Evidence base: deployed commit `49c5e9dedf9d` (= `origin/master`), the production
database read via `crm_readonly`, `app/core/release_guard.py`,
`app/core/corpus_provenance.py`, and the anonymous public-read surface.

Governing principle applied throughout: **evidence of uncertainty must remain
uncertainty.**

---

## 1. Executive finding

**The guard's sentence is true. The control it implements is not the control its
own docstring names, and the safety invariant fails at the exposure boundary.**

`_check_public_read_corpus` blocks startup on **positive evidence that a subject
is real**. The required invariant is the converse: exposure permitted only on
**positive evidence that a subject is demonstration data**. Those two predicates
agree everywhere except on the unknown set — and on this deployment the unknown
set is **96.7% of all customer subjects**.

Measured: of 25 contacts returned to an anonymous, unauthenticated caller in a
single request, **23 had no established provenance at all**.

The guard is not an exposure boundary. It is a startup-time, whole-deployment
admission check. **No read path in the system consults provenance**, so the
per-subject rule the invariant requires does not exist anywhere to be enforced.

| | |
|---|---|
| Guard wording honest? | **Yes** — no overclaim in the string |
| Guard severity honest? | **No** — reports `ok` over a 96.7% unproven corpus |
| Invariant enforced at the exposure boundary? | **No** |
| Finding | **OPEN — new control finding, HIGH** |

---

## 2. Current proven property

**DATABASE-VERIFIED (production).** What the system can positively establish:

| Claim | Status |
|---|---|
| No customer subject is classified `real` | **PROVEN** — 0 rows where `entity_type ∈ CUSTOMER_SUBJECTS AND state='real'` |
| 12 customer subjects are proven demonstration data | **PROVEN** — 6 contacts + 6 accounts, `state='synthetic'` |
| Every customer-subject email is on a deployment-owned domain | **PROVEN** — contacts 129/129, accounts 126/126, leads 105/105 on `seed.agentorc.ca` or `agentorc.ca` |
| Classification cannot be forged from a heuristic | **PROVEN** — `corpus_provenance_rule_supports_state` CHECK |
| The tripwire fails *tripped* when it cannot read the corpus | **PROVEN** (CODE-INSPECTED) |

The `real` classifications that do exist (5) are on `owners` — the five
executives — which is **not** a customer subject and is correct.

## 3. Current unproven property

**The safety invariant.**

> `PublicRead(s) ∧ ¬ProvenDemo(s) ⇒ SAFETY VIOLATION`

**Measured violations: 353.**

| Subject | Total | Proven demo | Proven real | `ambiguous` | **Unclassified** | Publicly readable |
|---|---|---|---|---|---|---|
| `contacts` | 129 | 6 | 0 | 0 | **123** (95.3%) | yes |
| `accounts` | 131 | 6 | 0 | 0 | **125** (95.4%) | yes |
| `leads` | 105 | 0 | 0 | 0 | **105** (100%) | yes |
| `customers` | 0 | — | — | — | — | retired (ledgered) |
| **Total** | **365** | **12 (3.3%)** | **0** | **0** | **353 (96.7%)** | |

**NOT PROVABLE:** how many of the 353 are genuinely synthetic. The seed-email
migration rewrote every address with no synthetic filter and kept no backup, so
the real-vs-synthetic distinction for those rows cannot be reconstructed from any
surviving evidence. This is an explicit fact of this audit, not a gap to be
closed by more querying.

## 4. Provenance evidence and limitations

**The `ambiguous` state exists and is unused.** The schema can express *"we
looked and found no evidence"* — `state='ambiguous'` with `rule='no_evidence'`,
enforced by CHECK. Production holds **zero** such rows.

The consequence is a conflation the model was built to avoid: a subject absent
from `corpus_provenance` could mean *never examined* or *examined, nothing
found*, and nothing distinguishes them. All 353 are absent, not `ambiguous`.

**The domain signal is not evidence, and the module says so.** From
`corpus_provenance.py`:

> *"Not evidence of provenance — the seed migration put real and generated
> records alike behind seed.agentorc.ca — but a usable floor for the tripwire,
> which is asking a different question."*

This matters more than it first appears. The guard's "no-signal" branch is
satisfied **because** every address is on `seed.agentorc.ca`. That uniformity is
the *direct artefact of the migration that destroyed provenance*. The absence of
a domain signal is therefore not weak evidence of demonstration status — it is a
fingerprint of the event that made provenance unknowable.

**`is_synthetic` is not evidence either.** `contacts.is_synthetic` has
`DEFAULT false`, nullable. Production: 6 `true`, 123 `false`, 0 `null`. The 123
`false` values are an unset default, **not an assertion that those subjects are
real**. Correctly, nothing treats them as one — the column feeds
`rule='flagged_synthetic'` only in the `true` direction.

## 5. Guard implementation assessment

Answering the eight questions from evidence.

| # | Question | Answer | Grade |
|---|---|---|---|
| 1 | What data does it inspect? | `corpus_provenance` rows for the 4 customer subjects, plus each subject table's email column | CODE-INSPECTED |
| 2 | What signals does it rely on? | (1) `state='real'` — definitive; (2) email outside owned domains — explicitly *not* evidence | CODE-INSPECTED |
| 3 | What happens when provenance is missing? | **Nothing. The subject passes.** Absence contributes to neither signal | CODE-INSPECTED + DATABASE-VERIFIED |
| 4 | What happens when provenance is contradictory? | `state='real'` blocks regardless of domain. A `synthetic` row on a foreign domain trips signal 2 → **advisory only** | CODE-INSPECTED |
| 5 | Can an unknown subject pass? | **Yes — and 353 do** | DATABASE-VERIFIED |
| 6 | Does it assert unknown *is* demo? | **No.** The disjunction is honest | CODE-INSPECTED |
| 7 | Is the wording consistent with the evidence? | **The proposition, yes. The `severity: ok`, no** — see §6 | DEPLOYMENT-VERIFIED |
| 8 | Could a future migration silently change the conclusion? | **Yes, and this has already happened once** — see below | CODE-INSPECTED |

**On question 8, specifically.** Any future seed or import writing into
`seed.agentorc.ca` adds unclassified customer subjects that trip neither signal.
The guard's conclusion therefore gets *quantitatively weaker* while its output
stays byte-identical and green. There is no coverage term — no count, no
percentage, no floor — anywhere in the check. The 2026-08 seed-email migration is
the worked example: it moved every address onto an owned domain, which *silenced
signal 2 across the whole corpus* and simultaneously destroyed the provenance the
guard would need to say anything stronger.

### The structural finding: this is not an exposure boundary

**CODE-INSPECTED.** Consumers of `corpus_provenance` are: `release_guard`
(startup), `work_ownership` (owner eligibility), `assignable`, and its own admin
router. A search of the contacts and accounts read paths for
`corpus_provenance|is_synthetic|provenance` returns **nothing**.

Traced end to end:

```
source data ──► corpus_provenance ──► tripwire() ──► _check_public_read_corpus
                                                         │
                                                    enforce() at STARTUP
                                                         │
                                    UnsafeConfiguration ─┘ (whole deployment)

public-read exposure:  auth_dep.SECURITY_POSTURE == "public-read"
                              │
                              └──► anonymous read permitted
                                   ▲
                                   └── consults provenance: NEVER
```

The chain is complete up to `enforce()` and then **stops**. The posture is a
global on/off; provenance never reaches a per-request decision. `ProvenDemo(s)`
is therefore not a precondition of `PublicRead(s)` in any code path.

## 6. Is the guard's claim honest?

The sentence under audit:

> `posture=public-read and every customer subject is demonstration data or
> unclassified-with-no-signal.`

**As a proposition: TRUE, and it is not an overclaim.** 12 subjects are
demonstration data; 353 are unclassified and trip no domain signal. The
disjunction is real, `unclassified` is named rather than absorbed into
`demonstration`, and the phrase "with-no-signal" is accurate. **It does not
collapse unknown into synthetic.** That restraint is a genuine strength and is
recorded as one.

**As a control signal: NOT honest**, and this is the defect.

1. **It is emitted at `severity: "ok"`** — the same level as
   `db_privileges: connected as 'crm_app'`, which is a proven property. A reader
   scanning the startup log sees a green line over a corpus that is 96.7%
   unproven.
2. **The disjunction hides its own distribution.** Both branches read as benign,
   and nothing says one branch holds 12 subjects and the other 353. The
   strongest truthful statement is not *"demonstration data or
   unclassified-with-no-signal"* but something closer to: *"12 of 365 customer
   subjects are proven demonstration data; the remaining 353 have no established
   provenance and are served anonymously anyway."* Both are true. Only the
   second lets a reader judge the risk.
3. **The docstring states the correct safety condition and the code implements a
   weaker one.** The function opens:

   > *"public-read is safe only while the corpus is demonstration data."*

   That is `∀s: ProvenDemo(s)`. The implementation is `¬∃s: ClassifiedReal(s)`.
   **The overclaim is not in the output string — it is in the gap between the
   stated safety condition and the implemented predicate.**

## 7. Negative-path results

| Case | Behaviour | Verdict | Grade |
|---|---|---|---|
| Known demonstration subject | passes | correct | DATABASE-VERIFIED |
| Known real subject | **blocks startup** (deployed), overridable only by `PUBLIC_READ_ACCEPT_REAL_DATA=1`, never silently | **strength** | CODE-INSPECTED |
| Unknown provenance | **passes, and is served** | **DEFECT** | DATABASE-VERIFIED |
| Missing provenance field | contributes to no signal; passes | **DEFECT** | CODE-INSPECTED |
| Unclassified subject | passes; 353 measured | **DEFECT** | DATABASE-VERIFIED |
| Contradictory signals (`real` + owned domain) | blocks — signal 1 wins | correct, fails closed | CODE-INSPECTED |
| Contradictory signals (`synthetic` + foreign domain) | advisory only | acceptable; the classification is authoritative and the domain is declared non-evidence | CODE-INSPECTED |
| Subject introduced by the seed-email migration | passes, silently, in bulk | **DEFECT** | DATABASE-VERIFIED |
| Valid customer shape, no provenance signal | passes | **DEFECT** | DATABASE-VERIFIED |
| Corpus unreadable | tripwire returns `tripped=True` ("fail tripped, never clear") — but the guard maps tripped → **advisory**, so the deployment still starts | fail-closed in *reporting*, fail-open in *enforcement* | CODE-INSPECTED |
| Subject table dropped | `UNDECLARED` → raise → tripped; only a ledgered retirement excuses absence | **strength** — `DROP TABLE` cannot silence the control | CODE-INSPECTED |

**The critical test — can an unknown subject become represented as known-safe
demonstration data?**

- **In the classification store: NO.** `corpus_provenance_rule_supports_state`
  makes it impossible to record `synthetic` without `flagged_synthetic`,
  `seed_generator` or `human_attested`. A domain heuristic cannot be written as
  evidence. **Verified strength.**
- **In the guard's wording: NO.** The disjunction keeps them distinct.
- **In the authorization outcome: YES.** Proven-demo and unknown subjects are
  served identically, by a boundary that never asks. **Control defect.**

The system tells the truth about what it knows and then acts as though it knew
more.

## 8. Production measurements

**DIRECTLY OBSERVED — anonymous, unauthenticated, deployed
`49c5e9dedf9d`:**

- `POST /contact-chat {mode:list,pageSize:25}` → **25 contacts**, of which
  **23 UNCLASSIFIED**, 2 `synthetic` (cross-referenced by `contact_id` against
  `corpus_provenance`).
- Fields returned per contact: `email`, `phone`, `billing_street`,
  `billing_city`, `billing_province`, `billing_postal_code`, `billing_country`,
  `created_by_name`, `updated_by_name`, `owner_id`, `account_id`, `contact_id`.
- `POST /account-chat` → "Page 1 of 40 | Total: 118 accounts".
- `POST /lead-chat` → "Page 1 of 35 | Total: 105 leads".

All three customer subjects are anonymously enumerable, with PII and internal
identifiers, irrespective of provenance state.

## 9. Historical finding reassessment

| | |
|---|---|
| **Original finding (D-01 / N-03)** | "Production serves contact PII to anonymous callers." |
| **What the original evidence established** | Anonymous enumeration of 129 contacts with addresses and staff names. Correct, and still true. |
| **What the original finding did NOT examine** | Whether the control that *licenses* the posture establishes what it claims. |
| **What the current system establishes** | No customer subject is classified real; 12 are proven demo; classification cannot be forged; the guard blocks on proven-real and refuses to start a deployed environment. |
| **What remains unproven** | That the 353 served subjects are demonstration data. **NOT PROVABLE** from surviving evidence. |
| **Status** | **D-01/N-03: OPEN, unchanged** — a declared posture decision. **New: the control finding below is OPEN.** |

Applying the user's own test — *"does the current control enforce the required
safety property given the provenance evidence that actually exists?"* — the
answer is **no**, and the reason is not the historical provenance loss. Even if
provenance were fully recoverable, **the exposure boundary does not consult it**.
The control would still not enforce the invariant. The historical loss changes
the *size* of the violation, not its existence.

This finding is therefore **not excused** by unrecoverable provenance, and it is
**not closed** by the guard's cautious wording.

## 10. New findings

### P-01 · HIGH · The public-read control licenses exposure on absence of counter-evidence

- **Component:** `release_guard._check_public_read_corpus` + `auth_dep` posture
- **Violated property:** *A subject may be publicly readable only when the system
  has positive, authoritative evidence that it is permitted demonstration data.*
- **Evidence:** §3, §5, §7, §8 — DATABASE-VERIFIED and DIRECTLY OBSERVED.
- **Failure path:** a customer subject with no `corpus_provenance` row trips
  neither signal; the guard passes at `severity: ok`; the posture permits
  anonymous reads; no read path consults provenance. 353 subjects, measured.
- **Consequence:** the control that exists to make `public-read` safe establishes
  only *"nothing is proven dangerous"*. Its own docstring claims the stronger
  property. An operator reading a green startup log is told the corpus is
  demonstration data or unremarkable; 96.7% of it is neither established as one
  nor examined.
- **Likelihood:** certain — it is the current state.
- **Severity rationale:** HIGH, not CRITICAL. No subject is *known* real; the
  posture is a declared owner decision; the classification store cannot be
  forged; a proven-real subject *would* block startup. It is not MEDIUM because
  the control is load-bearing for a decision to expose customer PII anonymously,
  and it does not bear that load.

### P-02 · MEDIUM · The guard has no coverage term, so its conclusion weakens silently

- **Evidence:** CODE-INSPECTED — no count, ratio or floor appears in the check.
- **Consequence:** every future seed or import into an owned domain enlarges the
  unproven set while the output stays byte-identical and green. Already
  demonstrated once: the seed-email migration silenced signal 2 corpus-wide.
- **Note:** this is the *census-pin inverse* — rather than a control comparing a
  population to a constant, here a control reports a *proportion it never
  computes*.

### P-03 · LOW · `ambiguous` / `no_evidence` is modelled and unused

- **Evidence:** DATABASE-VERIFIED — 0 rows; CHECK permits it.
- **Consequence:** *never examined* and *examined, no evidence found* are
  indistinguishable. The model can express the distinction the audit most needs
  and production does not use it.

## 11. Recommended remediation — NOT IMPLEMENTED

Smallest change that makes the control **truthful**, and separately, what would
make it **correct**. These are different, and only the second enforces the
invariant.

**(a) Truthfulness — small, and it does not fix the defect.**
Give the check a coverage term and downgrade the green line: report
`12 of 365 customer subjects proven demonstration data; 353 unproven` at
`severity: advisory` whenever the unproven count is non-zero, with an explicit
named acceptance (`PUBLIC_READ_ACCEPT_UNCLASSIFIED=1`) mirroring the existing
`PUBLIC_READ_ACCEPT_REAL_DATA`. This converts a silent pass into an accepted,
reported risk. **It does not enforce the invariant** and must not be described as
if it did.

**(b) Correctness — enforces the invariant, larger.**
Make `ProvenDemo(s)` a precondition of exposure at the read boundary: the
public-read path returns only subjects with an authoritative `synthetic`
classification. On today's data that surfaces 12 subjects and hides 353 — which
is the *correct* behaviour under the invariant, and is also why it is a product
decision, not a patch. Unnecessary exclusion is the safe failure; the current
direction is the unsafe one.

**(c) The decision that precedes both.**
Whether `public-read` is retained at all (D-01, open since the first assessment).
If it is retired, P-01 becomes moot; if it is retained, (b) is what makes it
defensible and (a) is what makes it honest in the meantime.

**Sequencing note:** (a) before (b). (a) is a few lines and surfaces the true
state immediately; (b) changes what anonymous callers can see and needs the
posture decision first.

## 12. Conclusion — NO CHANGES MADE

**Question: can we truthfully say that the current public-read control prevents
customer data of unknown provenance from being treated as known demonstration
data?**

**No — with one important split.**

- **In its record-keeping and its wording: yes.** The schema makes it impossible
  to record unknown as synthetic; the guard's sentence keeps the two distinct and
  names uncertainty as uncertainty. The system does not *believe* anything it
  cannot prove. This is a genuine, verified strength and it should not be lost in
  remediation.
- **In its authorization outcome: no.** Proven-demo and unknown subjects are
  served identically to anonymous callers, because the exposure boundary never
  consults provenance. 353 of 365 customer subjects are exposed without their
  demonstration status being established. The invariant

  > `PublicRead(s) ∧ ¬ProvenDemo(s) ⇒ SAFETY VIOLATION`

  is violated 353 times, measured.

The distinction the audit brief drew is exactly the one the evidence supports:
**a truthful classifier does not protect a system if the unknown record can still
be served.** This system has the truthful classifier and lacks the gate.

**No code, schema, configuration, data, guard, migration, seed or deployment was
changed. Nothing was committed, pushed or deployed.** Production remains on
`49c5e9dedf9d`; the working tree carries only this document.
