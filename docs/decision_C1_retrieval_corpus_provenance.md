# Decision Record — C1: Retrieval corpus provenance

**Date:** 2026-09-09 · **Status:** EVIDENCE AND OPTIONS. The decision is not
taken here. · **Gates:** Step 14 (C implementation) and D-C2 (revalidate /
re-tune / accept) both depend on this.

**The question, as posed:**

> What classes of system-generated content are legitimate retrieval evidence,
> and what provenance must exist at generation time to distinguish legitimate
> business evidence from operational/self-generated output?

Nothing in C was modified to produce this record. The three C failures remain
the baseline.

---

## 1. The finding that decides the shape of every answer

**The provenance semantics this question needs already exist in this schema,
declared, and they are simply absent from the table that dominates the index.**

**FACT.** Three tables carry a provenance vocabulary as an enforced CHECK:

```
accounts :: human, ai, import, external, computed, unknown
contacts :: human, ai, import, external, computed, unknown
leads    :: human, ai, import, external, computed, unknown
```

That is precisely the distinction C1 asks for — *what produced this row* — and
it is already a governed contract on the three tables where someone thought
about it.

**FACT.** `activities` — **91% of the retrieval index** (13,388 of 14,785 rows)
— carries no such column. Its only candidate fields are `created_at`,
`created_by`, `related_type`, `type`.

So the answer to *"what provenance must exist at generation time"* is not an
invention. It is an existing declared vocabulary that was never extended to the
table retrieval actually reads.

---

## 2. Every field that could be mistaken for provenance, and why each fails

The instruction was not to press an existing field into service unless it
actually carries the required semantics. Each candidate was tested, and **all
four fail** — three of them in ways that would have looked plausible.

| candidate | what it actually is | verdict |
|---|---|---|
| `activities.created_by` | an actor FK, nullable | **FAILS — measured** |
| snippet text markers | prose written by the generator | **FAILS — measured** |
| `content_embeddings.source_type` | **the entity table the row came from** | **FAILS — and it is a trap** |
| `corpus_provenance` | synthetic-vs-real seed classification | **FAILS — different axis** |

**`created_by` — FACT.** NULL for **100%** of the 548-row template group, and
NULL for **7,520** activities overall. A field that is empty on the entire
population in question cannot classify it. It also answers a different question
— *which actor* — where the requirement is *which class of producer*.

**Text markers — FACT.** `"Created by workflow engine…"` and `"accepted by
provider…"` together match **3,123 of 14,785 rows (21%)**, while measured
near-duplication is **52.5%**. The marker under-detects by more than half, and
detection depends on prose the generator happens to emit. This is a proxy for
the property, which is the antipattern this codebase has removed repeatedly.

**`content_embeddings.source_type` — FACT, and the most dangerous of the four.**
The column exists on the index table itself, which makes it the obvious thing to
reach for. **It means the entity table the row came from** — `activity`, `case`,
`case_comment`, `conversation_message`, `interaction_memory` — not who authored
it. Its values are unconstrained by any enumerating CHECK.

> **The same column name carries two incompatible meanings in one schema.** On
> `accounts`/`contacts`/`leads`, `source_type` is authorship. On
> `content_embeddings`, it is entity type. Code that "uses the existing
> `source_type` for provenance" would read *which table* and report *who wrote
> it*, and would be wrong in a way that reviews well.

**`corpus_provenance` — FACT.** Classifies `synthetic | real | ambiguous`, i.e.
seeded-versus-genuine data. Orthogonal: a real customer's activity and a
workflow-generated task are both `real`.

**Conclusion — INFERENCE, from four measured failures:** provenance filtering,
admission control and weighting are **all unimplementable today**. Not
difficult — unimplementable, because the attribute does not exist. Any C
remediation that depends on distinguishing producers is blocked on a
generation-time data contract.

---

## 3. The classes the corpus actually contains

**FACT — the largest template groups, by measurement:**

| rows | leading text | proposed class |
|---|---|---|
| 548 | `Send payment reminder — Created by workflow engine from even…` | **operational self-output** |
| 480 | `Requested additional information from customer.` | ambiguous — no marker, templated |
| 473 | `Thank-you call to customer — Created by workflow engine from…` | **operational self-output** |
| 349 | `Order confirmation notification accepted by provider — SO-…` | **recorded business event** |
| 343 | `Order shipment notification accepted by provider — SO-…` | **recorded business event** |
| 332 | `Escalate overdue invoice — Created by workflow engine from e…` | **operational self-output** |
| 221 | `Qualify new opportunity — Review account fit and potential d…` | ambiguous — no marker, templated |

**PROPOSED — three classes, offered for the decision, not asserted as settled:**

1. **Human-authored evidence.** Call notes, inbound email, case comments.
   Someone observed something about the business and wrote it down.
2. **Recorded business events.** *"Order shipment notification accepted by
   provider."* The system is the **scribe** of a fact that is true about the
   world independently of the system having noticed it.
3. **Operational self-output.** *"Send payment reminder — Created by workflow
   engine from event…"* The system describing **its own intention to act**.
   True about the system; asserts nothing about the customer.

**The line the decision has to draw:** *does the row assert a fact about the
business, or about the system's own work?* Class 3 is evidence about the
system. Whether that belongs in an index answering questions about customers is
the decision.

**Not resolved by measurement — the 480 and 221 groups.** They are heavily
templated and carry no generator marker, so they cannot currently be assigned to
a class at all. That is itself the strongest argument that classification must
happen **at generation time** rather than by later inspection: today, roughly a
third of the largest template groups are unclassifiable by any available signal.

---

## 4. What a generation-time contract would have to cover

**FACT — 12 distinct code paths INSERT into `activities`:**

```
agents/email/inbound_bridge.py   core/customer_memory.py     core/quotes.py
agents/email/structured.py       core/integrations.py        core/sequences.py
core/agent_bus.py                core/order_notifications.py core/telephony.py
core/booking.py                  core/pipeline_hygiene.py    core/voice_support.py
```

A provenance attribute is only as good as its **weakest writer**: one path that
omits it produces `unknown`, and `unknown` cannot be safely admitted or safely
excluded. Note that the existing vocabulary already anticipates this — it
includes `unknown` as a declared value rather than pretending every row can be
classified.

**HYPOTHESIS, not measured:** that all 12 paths can determine their own class at
write time. `agent_bus.py` plainly can. `inbound_bridge.py` (human email) plainly
can. Whether `pipeline_hygiene` or `sequences` rows are class 2 or class 3 is a
semantic question about those features that this record does not answer.

---

## 5. The decision

**D-C1.** Which classes are legitimate retrieval evidence?

| option | consequence |
|---|---|
| **1 only** | Smallest index, highest precision. Discards genuine business facts recorded by the system — an order really did ship |
| **1 + 2** | The likely intent. Requires the class-2/class-3 line to be drawn per write path |
| **1 + 2 + 3, weighted** | Keeps everything; needs provenance-aware ranking, so it depends on the same missing attribute plus a weighting policy |
| **all, unweighted (today's state)** | Measured outcome: the ranked budget is 93–97% template duplicates |

**D-C1a — the prerequisite, which is not optional under any of the above except
the last.** Extend the declared `source_type` vocabulary (or a
deliberately-named successor) to `activities`, written at generation time by
all 12 paths.

> **Naming warning, from §2.** If the column on `activities` is called
> `source_type`, the schema will contain a third meaning of that name adjacent
> to `content_embeddings.source_type`, which the indexer reads. A distinct name
> — `produced_by`, `authorship` — costs nothing now and removes a permanent
> collision.

**D-C1b — backfill policy.** Existing rows cannot be classified (§2). Either
they are `unknown` and the retrieval policy must state how `unknown` is treated,
or the index is rebuilt from a date. **`unknown` is the honest value; a
guessed backfill would manufacture the provenance this record says does not
exist.**

---

## 6. Explicitly out of scope, and why

**The mechanism defect is separate and must not be conflated with this.** The
measured saturation — 30 candidates ranked, 28 collapsed as duplicates, 2
distinct results — is caused by **dedupe running downstream of the ranking
budget**, so the budget is spent on rows that are then discarded. That is a
mechanism-ordering defect, and it would still be a defect on a corpus with
perfect provenance.

It is **gated at step 14** and is not addressed here. Fixing corpus admission
would reduce the crowd; it would not fix the ordering.

**Not proposed anywhere in this record:** lowering the similarity floor, raising
`top_k` / `N_VEC` / `MAX_CANDIDATES`, or re-pinning any test.

---

## 7. What remains unverified

- **UNVERIFIED — production corpus state.** All figures are local. Production
  may sit on a different side of every threshold.
- **UNVERIFIED — growth rate and bound of the workflow template group.**
  Measured once. Linear, retention-bounded, or unbounded changes urgency
  entirely, and it is the most decision-relevant unknown here.
- **UNVERIFIED — whether all 12 write paths can classify their own output.**
  Stated as hypothesis in §4.
- **UNVERIFIED — actual retrieval quality.** Nothing here measures it. The claim
  is that the **evidence has expired**, not that quality has degraded. **Do not
  report this as a retrieval-quality regression.**
- **UNEXPLAINED — why `duplicate_share` fell (0.530 → 0.525) while the largest
  group grew.** Bears on whether the corpus is diluting or concentrating.
- **UNEXPLAINED — what the pinned counts would have been at the validated
  composition**, and therefore whether that pin was ever a sound control.
