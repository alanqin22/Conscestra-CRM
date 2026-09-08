# P2 — Independent World-Class Architecture Assessment

**Brief for a NEW session. Authored 2026-09-07 at the close of P1;
state updated 2026-09-08 after both migrations were deployed.**

Start the session with: *"Follow `docs/p2_assessment_brief.md`."*

> **You are not the P1 implementer. Treat every P1 conclusion — including the
> new owner-routing architecture — as a hypothesis to independently falsify,
> while making no changes during this assessment.**

---

## Why this is a new session, and what that is for

P1 was: *find known defects → make targeted changes → prove the invariant.*

P2 is: *forget the remediation narrative → re-read the architecture from first
principles → discover defects nobody knows exist.*

Carrying P1's context forward risks confirmation bias — an agent that spent a
day building the ownership-routing design is poorly placed to ask whether that
design is wrong. **The epistemic role is different, so the session is
different.**

---

## 1. Baseline documents — evidence, NOT ground truth

Read all four before forming conclusions:

1. `docs/architecture_reassessment_2026-09-07.md`
2. `docs/post_remediation_verification_2026-09-07.md`
3. `docs/p1_remediation_2026-09-07.md`
4. `docs/governance/decisions_2026-09-07.md`

**Where a prior report says a control is fixed, independently inspect the
implementation and determine whether the underlying invariant actually holds.**
Do not silently inherit their assumptions. Do not assume a remediation decision
is architecturally correct merely because its tests pass.

---

## 2. State at handoff (2026-09-08)

**FACT — verified locally.**

| | |
|---|---|
| Tests | **2,978 passing · 1 failing · 2 skipped** |
| `verify_invariants` | all hold |
| P0 suites | green (227 passed) |
| Eligible owners | **5**, all attested real (the executives) |
| `WORKFLOW_OWNER_REQUIRED` | **OFF** — `may_enable: False`, unmet `[2,3,4,5,6,8]` |
| jmartin identity collision | **HELD**, unresolved by decision |
| Historical customer-owned work | **~4,700 items** — separate business debt, untouched |
| Migrations | **2, both DEPLOYED and PROMOTED** (see below) |
| Committed / pushed | `feat/p0-p1-governance-remediation`, both repos, **CI green** |
| Merged | **not yet** — both PRs open |

Closed in code during P1: workflow owner-resolution (routing is declared data;
eligibility is an independent gate; unlisted entity types fail closed;
customer-contact owners cannot be carried through). Seven employee grants were
applied and then **reversed** — all eight employees are attested SYNTHETIC, so
the accountable population is the five executives (Decision C).

### The two migrations are DEPLOYED — verify them, do not touch them

**Superseded 2026-09-08.** An earlier draft of this brief called them
PENDING DEPLOYMENT. Both are now applied to Railway, promoted into
`REQUIRED_MIGRATIONS`, and `migrate --check` reports **"schema is
current"** on local and Railway alike.

They remain **in scope for independent verification and out of scope for
modification**:

- **Do** read `workflow_owner_resolution.sql` and
  `governance_decision_link_identity.sql`, identify the invariant each
  claims, find the alternate routes around it, and try to falsify it.
- **Do not** amend, re-apply, or "improve" either one. If verification
  finds a defect, record it as a P2 finding with a recommendation.

The owner-routing design is new and, apart from its author, untested. It
is the single most likely place for P2 to find something, and the single
most likely place for a P2 agent to slip from assessing into building.

### What the deployment itself taught, and P2 should carry forward

**FACT.** The application shipped ahead of both schema changes on
2026-09-08 04:03 UTC. The governance decision path was **down** until they
were applied ~20 minutes later: `_row()` 500s without
`decision_link_nonce`, and `_row()` is on `approve`, `reject`, `delegate`,
`undo` and the SLA sweep.

**The startup log was entirely green throughout.** `release_guard` passed
every check, all 36 jobs scheduled, the capability registry reported 46 of
46. The missing columns are only touched when somebody *decides*
something, and nobody had in the four minutes since boot. **A clean boot
is not evidence that the schema is present.** The assumption that
migration 1 would fail loudly *at startup* was wrong; it fails loudly on
first use, which is a different thing.

**FACT.** Regenerating the baseline to carry the promotion introduced a
second defect: the file is two `pg_dump` outputs concatenated, and
splicing the ledger in from its `COPY` block kept that dump's closing
restrict directive without its opener. CI could not build the database at
all. It was found by CI rather than by `scripts/verify_gate`, which the
baseline's own header names as its verification and which had not been run
after regenerating.

Both belong in the Alternate-Route and Failure-Mode sections: one is *a
control that only fails when exercised*, the other *a generated artefact
whose stated verification was skipped*.

### Where P2 sits in the sequence

```text
P0  code complete, test verified
 └─ P1  COMPLETE, DEPLOYED, PRODUCTION-VERIFIED  ← you are here
     └─ P2  INDEPENDENT ASSESSMENT   (this brief — no changes)
         └─ P2 findings / decisions
             └─ P2 remediation
                 └─ (P0 + P1 migrations DEPLOYED 2026-09-08)
                     └─ adversarial production verification
                         └─ final World-Class assessment
```

**Do NOT deploy anything during P2, and do not merge the open PRs.** A P2
agent that discovers a defect, patches it, and then assesses its own patch
has destroyed the independence this separation exists to create.

### One operational item outranks the assessment

**`SO-2026-102219` has had no communication of any kind** — no order
confirmation, no shipping notice — and its order shipped 2026-08-30.
The owner's disposition (Decision B) is: **send that one, write off the
other 17**, then close the `event_orphaned` alert with closure evidence.
That alert is owned by CTO Bill Wang and is already past its 24h SLA.

This is production work and NOT part of P2. It is recorded here only so
it is not lost between sessions: a real customer is waiting, and that
outranks an assessment.

---

## 3. The known environmental failure — out of scope unless

`test_recency_only_output_is_frozen`. `recency_only` ranks a fixed **4,000-row**
recent window of a corpus now holding **14,275** records; two pinned rows fell
out of that window.

**Do not repin it to make the suite green. Do not classify it as a defect**
unless you independently establish that the fixed window violates an intended
invariant. If it is in scope, the architectural question is:

> Is a fixed recent candidate window an intentional scalability design, and if
> so, what invariant governs recall as the corpus grows?

---

## 4. Two invariants P1 established — look for them being violated elsewhere

These were earned, and they generalise:

> **Declared routing determines where something should go; eligibility
> determines whether that destination is currently allowed.** Revoking
> eligibility must prevent routing without a second governance change.

> **Unlisted ≠ default.** A case the declaration does not cover must fail
> closed, never acquire a default.

Find where else in the architecture these do *not* hold.

---

## 5. The recurring failure this project keeps producing

Six times in one day, a control measured a **proxy** that diverged from the
**property** it stood for:

| Proxy | Property it stood for | How it diverged |
|---|---|---|
| `assert len(writes) == 16` | every write capability declares its parameters | the population grew |
| `assert count < 100` | the surface is live work, not history | the population grew |
| absolute unowned high-water mark | the debt is not worsening | +1 per suite run |
| **all-time accountable ratio** | the debt is not worsening | **the denominator was the broken thing** |
| seed email domain | this record is synthetic | 176 of 181 seed contacts flagged "real" |
| `is_deployed()` | should this process send | sat below the test seam, disabling 21 tests |
| "≥ 7 employee-linked owners" | staff can own work | the plan behind the count was withdrawn |

**The mandatory proxy audit (§ below) is the highest-value part of P2.** Search
for controls keyed on fixed counts, percentages, population snapshots, domains,
environment flags, implementation-specific output, fixture identity, or inferred
provenance — and for each ask what real property it is attempting to measure,
and whether it can diverge as the system evolves. **Do not replace proxies
automatically**; first establish that they are actually inappropriate.

---

## 6. Assessment mindset

> What assumption would have to fail for this architecture to become unsafe?

- Where does the primary route have stronger governance than an alternate route?
- Where can stale state outlive authorization?
- Where can configuration become an authority mechanism?
- Where can AI-agent composition create authority no individual agent has?
- Where can the database defeat application-layer assumptions?
- Where can retries produce a second consequential effect?
- Where can an operation be recorded successful without proving it occurred?
- Where can an operation occur without producing its durable record?
- Where are ownership, authority, identity and accountability confused?
- Where does failure become success through a fallback?

For every domain: **inspect the implementation → identify the intended invariant
→ identify the enforcement mechanism → identify alternate paths → attempt to
falsify → classify the evidence.** A test proves only what it actually
exercises.

### Second-order governance — the proven risk category

Identity · Authority · Ownership · Policy · Execution · Audit · Events ·
Notifications · AI agents · Configuration · Database. In each, the pattern that
keeps recurring is: *the first control was correct and an adjacent mechanism
bypassed its intent.*

### Mandatory alternate-route analysis

For every consequential operation enumerate API · UI · email · scheduler ·
worker · agent · agent-to-agent · webhook · import · migration · repair script ·
retry · replay · administrative · fallback · direct database. Then:

> **No alternate route may provide a weaker governance boundary than the
> primary route.**

### Mandatory lifecycle analysis

`intent → authorization → policy → ownership → claim → execution → external
effect → verification → audit → terminal state`, tested against crash, timeout,
retry, duplicate request, stale token, revoked identity, changed policy, changed
owner, worker restart, partial external success, database rollback, provider
failure.

---

## 7. Evidence discipline

Label every material statement **FACT · INFERENCE · RISK · RECOMMENDATION ·
DECISION REQUIRED · HUMAN ACTION REQUIRED**.

Evidence order: production behaviour → database state → deployed implementation
→ CI/release gate → tests → source → configuration → documentation.

**Do not claim production verification where production access was not actually
exercised. Do not convert absence of evidence into evidence of absence.**

### An environment fact that cost this session time

Ad-hoc database scripts against Railway are refused by the sandbox classifier;
**the project's own declared tools with an explicit `--target railway` flag are
not.** `python -m scripts.classify_orphaned_events --target railway` works.
Do not conclude "Railway is unreachable" from an ad-hoc script being blocked.

---

## 8. No remediation during the assessment

Do not modify code, tests, schema, production data, governance policies; do not
deploy, commit, or repin. Characterize each defect and recommend a remediation
separately. The assessment must remain independently auditable.

---

## 9. Deliverable

`docs/p2_world_class_architecture_assessment_2026-09-07.md`, with sections:

Executive Summary · Baseline and Scope · Independent P0/P1 Verification ·
Architecture Map · Governance Invariants · Identity and Authority · Ownership
and Accountability · AI-Agent Governance · Agent-to-Agent Authority Propagation
· Policy Lifecycle · Consequential Execution Lifecycle · Event Integrity ·
Notification Integrity · Data Provenance · Database Integrity · Auditability ·
Observability · Alerting · SLA Integrity · Security · Configuration Governance ·
Test Architecture · CI/CD Governance · Production/Demo Boundary ·
Alternate-Route Analysis · Proxy/Assumption Audit · Failure-Mode Analysis ·
Findings · Decision Register · Human Action Register · P2 Remediation Roadmap ·
Final Architecture Verdict

Each significant finding: invariant · evidence · failure mechanism · blast
radius · safe reproduction path · severity · confidence · recommended
remediation · whether a human decision is required.

---

## 10. Verdict — exactly one

**WORLD-CLASS** · **WORLD-CLASS WITH CONDITIONS** ·
**GOVERNED — NOT YET WORLD-CLASS** · **NOT READY**

Not WORLD-CLASS because the suite is green. Not WORLD-CLASS because P0/P1
findings were fixed. The verdict must reflect demonstrated ability to preserve
governance under growth, retries, failure, stale state, identity changes, policy
changes, alternate routes, AI-agent composition, configuration changes, database
behaviour, operator error and adversarial behaviour.

> **Can the governance invariants survive contact with reality — including
> situations the original designers did not anticipate?**

Do the assessment first. Remediation comes afterward.
