# Identity Resolution & Migration Specification

**Status: APPROVED FOR SPECIFICATION, NOT FOR EXECUTION.**
Written 2026-08-31. No migration may begin until §9's entry criteria all hold.

This document exists because a party spine is the right architecture and the
wrong thing to start building. The design is settled; the *evidence* it would
have to run on is not, and a merge is the one migration operation that destroys
information rather than moving it.

---

## 1. Why this is a specification and not a ticket

The dossier proposed one `party` table with typed links to every role table.
The validation gate tested whether it could actually be populated. It cannot —
not yet — and the measurement is the whole reason this document is careful:

```
contacts ↔ leads     by email :   0     by name : 121     deterministic : 9
contacts ↔ customers by email :   0     by name :  50     deterministic : 0
owners   ↔ employees by email :   0     by name :   1     deterministic : 0
owners   ↔ executives                                     deterministic : 4
```

**Email is dead as a matching key on this corpus.** `sql/seed_email_migration.sql`
ran `UPDATE … SET email = seed_email(…) WHERE email IS NOT NULL` across
contacts, leads and accounts — unconditionally, with no synthetic filter, and
kept no backup. The same logical person holds a different address in every
table they appear in.

**Name matching finds 121 of 122 leads and is wrong about 112 of them.** Only 9
are corroborated by `leads.converted_contact_id`. The parsimonious explanation
is that the seed generator drew contacts and leads from one name pool. A
migration keyed on names would fuse 112 pairs of unrelated people.

This codebase has already made this mistake once, one table over: a seed
migration keyed identity off a mutable attribute and silently re-labelled 39
customers. The lesson was recorded. It did not bind the next person, because it
was written as advice rather than as a constraint.

---

## 2. What a Party is

A **party** is a claim that a real-world actor exists. Nothing more. It carries
no role, no permissions, no contact details, no business state.

| Concept | Classification | Why |
|---|---|---|
| contact | **role** | a person in a commercial relationship with an account |
| lead | **role + status** | a pre-qualification role; `converted` ends it |
| customer | **status of a contact** | `contacts.is_customer` already models it; the legacy `customers` table is a fossil |
| owner | **role (assignment target)** | answers "who is accountable"; must survive a person leaving |
| employee | **role (employment)** | answers "who works here"; own lifecycle, own termination |
| executive | **role + authority** | authority limits and briefing preferences, not a separate person |
| account membership | **relationship** | a party→party edge, typed `works_for` |
| assignable | **preference** | routing configuration attached to a party |

Every role table keeps its own primary key and gains a nullable `party_id`.
**A party may hold many roles; a role may exist without a party.** The second
half is what makes the migration additive.

### 2.1 Actors are not all data subjects

Decided 2026-08-31: **an owner is a governance role assignment, not an
employee.** The assignee may be an employee, a contractor, an external
consultant, a business owner, or an **AI agent**.

That last one changes the model, and improves it. If a non-human can hold an
assignment, then the target of a role is an **actor**, and only *some* actors
are data subjects:

```
                         ACTOR
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
       person         organisation      system_actor
    (data subject)   (data subject)     (AI agent, service)
          │                │                │
          └──── party ─────┘                └── NOT a party
                                                never in the DSAR manifest
                                                no consent state
                                                not erasable under Art. 17
```

**A system actor must never enter `party`.** Putting agents there would drag
them into every subject-linked pathway the compliance layer walks — and the
DSAR manifest is designed to *break* when a new subject-linked table appears,
so the error would surface as a broken export rather than as a design
discussion.

`assignable_identity` is the embryo of the role-assignment table: it already
carries email, `owner_id`, source, display name, skills, languages and
preferred channel. Its `source` CHECK (`executive|employee|auth|manual|import`)
needs `contractor`, `consultant` and `agent` added. It holds 4 rows, so this is
a design decision, not a migration.

### 2.2 The evidenced default

All 44 owners are `is_active` **and** all 44 are referenced by live records,
against 21 employees. So 23 active, in-use owners are not employees in the
data. That describes the present, not the intent — but it means the safe
default is evidenced rather than assumed: **owner and employee are separate
roles that may share a party.** Non-destructive under either answer to the
business question.

Consequences that follow immediately:

- `owners.employee_uuid` at 0/44 is **not a defect**. It is an optional link
  that applies only when the assignee happens to be an employee. Do not
  backfill it.
- `executives.employee_uuid` at 4/4 populated and **0/4 resolving** *is* a
  defect: populated-but-dangling is different from deliberately empty.
- The one live UUID collision remains a defect under every model:
  `a1451ad6-310c-…` is **John Smith** in `owners` and **Julia Martin** in
  `employees`. Two different people, one identifier.

---

## 3. The evidence hierarchy

**INVARIANT: identity may be established only by a deterministic key or a
recorded human confirmation.**

| Rank | Evidence | Present on this corpus | Count |
|---|---|---|---|
| 1 | Deterministic foreign key | `leads.converted_contact_id`, `assignable_identity.owner_id` | **13** |
| 2 | Canonical immutable external id | none exists | 0 |
| 3 | Verified identifier | `is_email_verified` — but see below | **void** |
| 4 | Corroborated attributes | needs two independent trustworthy attributes; there is one, and it is a name | 0 |
| 5 | Human confirmation | `identity_links` is the right home | available |
| 6 | Never infer | everything else → separate parties | ~163 |

**Prohibited, each for a measured reason:** name matching (112 known-false
positives), email matching (rewritten by a migration), address matching,
trigram or fuzzy similarity, and any LLM-proposed merge.

### 3.1 The quiet casualty

`is_email_verified` is the project's stated gate for whether an address is
real, and 172 contacts carry it. But the verification attested to an address
the seed migration later **replaced**. The flag survived; what it attested to
did not. It must not count as identity evidence until a re-verification
establishes what it now refers to.

---

## 4. Disposition of ambiguous matches

Decided 2026-08-31. **Ambiguous matches are never auto-merged.** Disposition
depends on corpus provenance:

| Provenance of the pair | Disposition |
|---|---|
| both **synthetic** | bulk-reject (they are distinct generated people) |
| either side **real** | manual review, per pair, by a person |
| either side **ambiguous**, neither real | **leave separate, queue untouched** |

**A pair's disposition follows its most-protected member.** The rule as first
stated was corpus-level; the data is pair-level, and mixed pairs will exist.
Fail toward separation in every mixed case.

Two consequences that are easy to get wrong:

- **Bulk-reject is not the null action.** Rejecting writes a decision to
  `identity_links` and removes the pair from future candidacy. If provenance
  later reclassifies one side as real, there are ~163 recorded rejections no
  human ever reviewed, and a genuine duplicate has become invisible. **Leaving
  the queue empty is the null action.**
- **Populating the queue is itself an identity operation.** Generating
  candidates by name matching — even at `status='candidate'`, even without
  deciding anything — enshrines the 92%-false-positive signal as the discovery
  mechanism. Candidate generation must be provenance-aware, and therefore
  comes *after* classification, not before.

**Today the corpus is 44 synthetic, 0 real, 548 ambiguous.** Under the rule
above, today's disposition for every candidate pair is *leave separate*. That
is not a stalled migration; it is the correct answer for this corpus.

---

## 5. Historical preservation

**Never overwrite an identifier.** Every repointed reference keeps its original
value in a sibling column (`owner_id_legacy`) plus a row in an identity-decision
log recording: the old value, the space it was assumed to belong to, the new
`party_id`, the rule that decided it, the evidence, the actor, and the
timestamp.

An audit two years from now must be able to answer *what did this identifier
mean before the migration*, and no compaction may remove that.

**Merge** sets `merged_into_party_id` on the loser and leaves the row present —
a merge is a redirection, never a deletion, so it is reversible by clearing one
column. **Split** is only ever the reversal of a recorded merge; splitting a
party that was never merged means the original creation was wrong, which is a
data-entry correction, not an identity operation. **Canonical selection**
prefers the earliest creation with the most inbound references, and the choice
is recorded rather than recomputed.

**DSAR and erasure.** `party` becomes a new subject-linked table, which by
design breaks the DSAR export manifest until declared. That is the manifest
working correctly and must be planned for, not patched around. Erasure removes
the party and its roles while leaving the decision log's *structure* intact
minus the personal values — the same carve-out `governed_deletions` already
makes for erasure.

---

## 6. Executable post-migration invariants

None may be waived to make a phase green.

```
I1  every party has ≥ 1 role, or an explicit recorded reason
I2  no party_id appears in two role tables unless a deterministic link says so
I3  every legacy identifier is preserved and resolvable to its decision record
I4  no reference repaired by a prohibited rule — the decision log's `rule`
      column contains only {deterministic_fk, human_confirmed}
I5  count(parties) − count(merges) is stable across a re-run   (idempotence)
I6  every merge is reversible: clearing merged_into_party_id restores the
      pre-merge read for every dependent query
I7  the known collision (a1451ad6…) resolves to exactly TWO distinct parties
I8  activities/orders owner resolution: resolved + unresolvable = the original
      non-null count. No row silently loses its owner
I9  DSAR export for a party returns every role and observation linked to it
I10 no FK is added in a phase before the phase that repairs its referents
I11 no system_actor appears in `party`
```

---

## 7. Phased migration

Additive until Phase 4. Every phase independently reversible.

```
PHASE 0   CORPUS PROVENANCE                                   ← DONE 2026-08-31
          corpus_provenance table, rule CHECK, settled-state trigger,
          classification (44 synthetic / 0 real / 548 ambiguous), tripwire
          exit: every subject in exactly one state ✓

PHASE 1   PARTY TABLE + DECISION LOG                          additive
          create party, party_role, identity_decision_log
          populate 1:1 from every role table; apply the 13 deterministic links
          NO existing table altered. NO read changes.
          exit: I1, I3, I5, I7, I11 pass

PHASE 2   ROLE RELATIONSHIPS                                  additive
          party_id column (nullable, no FK) on each role table; backfill
          typed party→party edges: works_for from contacts.account_id
          exit: I2 passes; every existing query returns identical results

PHASE 3   AMBIGUITY REVIEW                                    human-in-the-loop
          provenance-aware candidate generation; disposition per §4
          default REJECT; today's answer is "leave separate"
          exit: queue dispositioned or explicitly deferred; I4 passes

PHASE 4   OWNERSHIP REPAIR                                    behaviour change
          fix the orders.owner_id WRITE PATH before any backfill
          repair the 615 cross-space activity references
          mark the 1,998 unresolvable as such — do not invent an owner
          exit: I8 passes

PHASE 5   OBSERVATION REPOINTING                              behaviour change
          blackboard · memories · embeddings · conversations gain party_id
          dual-read (old key OR party) until parity is measured
          exit: parity proven on a fixed query set

PHASE 6   FOREIGN-KEY ENFORCEMENT                             enforcement
          one table at a time, each after its referents are repaired
          a rejected row is a FINDING, never a reason to drop the constraint
          exit: I10 passes

PHASE 7   RETIREMENT                                          cleanup
          legacy customers/professionals; polymorphic entity_type paths
          only after every consumer reads through party
```

---

## 8. Projected shape

```
  182 contacts + 122 leads + 44 owners + 21 employees
+   4 executives + 38 customers(legacy) + 181 accounts
−   9 lead→contact deterministic links
−   4 owner↔executive deterministic links
= 579 parties, of which 13 are multi-role and 566 single-role

candidate merges deferred to human review, never automated : ~163
system actors, which are NOT parties                        : 0 today
```

---

## 9. Entry criteria — none of this may start until all seven hold

```
E1  D1 decided and recorded          ✓ public-read is intentional (2026-08-31)
E2  D2 decided and recorded          ✓ owner is a governance role (2026-08-31)
E3  corpus provenance classified     ✓ 44 / 0 / 548, recorded and immutable
E4  forward provenance invariant enforced, not documented
                                     ✓ rule CHECK + settled-state trigger
E5  evidence hierarchy signed off, with name and email matching prohibited
                                     ✓ §3, enforced by corpus_provenance's CHECK
E6  the ~163 ambiguous candidates have a recorded disposition
                                     — OPEN: §4 gives the rule; today's answer
                                       is "leave separate", which must be
                                       RATIFIED rather than assumed
E7  the eleven invariants are executable and passing on a REHEARSAL COPY,
      never on production
                                     — OPEN: not yet written as code
```

**Two criteria remain open, and neither is expensive.** E6 is a decision to
record; E7 is a test file against a restored backup. Until both hold, Phase 1
does not begin — not because the design is uncertain, but because a migration
whose invariants have never executed is a migration nobody has tested.

---

## 10. What this specification deliberately does not do

- **It does not build a graph database.** The graph is hundreds of nodes and
  tens of thousands of edges, queried two to three joins deep, and must be
  transactionally consistent with the CRM tables it describes. What is missing
  is graph *integrity*, not graph *technology*.
- **It does not merge anything.** Not one pair, on this corpus, today.
- **It does not repair `owners.employee_uuid`.** Under the decided model that
  emptiness is correct.
- **It does not treat ambiguity as a backlog.** 548 ambiguous subjects is the
  honest state of a demonstration corpus whose provenance evidence was
  destroyed by a migration, and pretending otherwise would be the same class of
  error as the merge it refuses to perform.
