# Scalability Validation Plan — 100k to 100M records

**Status:** plan, not a result. Every figure below is labelled **MEASURED**
(observed on this system), **EXTRAPOLATED** (measured cost projected forward,
with the assumption stated), or **ESTIMATED** (external figure, with its
source). Nothing here is an achieved benchmark, and the plan is only worth as
much as the validation gates in §7.

---

## 1. Measured baseline

Everything that follows extrapolates from these, taken on the local corpus.

| Quantity | Value | How |
|---|---|---|
| Indexed records | 7,310 | MEASURED |
| `content_embeddings` on disk | 36.6 MB → **5,000 bytes/record** | MEASURED (incl. 3.1 MB indexes) |
| Embedding | `text-embedding-3-small` @ 512 dims (2,048 B raw) | MEASURED |
| Derived memories | 848 rows, 4.5 MB | MEASURED |
| Distinct entities | 433 — avg **16.8** records/entity, max 737 | MEASURED |
| Consolidate one entity (737 records) | **252 ms** | MEASURED |
| Search, end to end | **668 ms** | MEASURED |
| — embed the query | 450 ms (67%) | MEASURED |
| — fetch 4,000 candidates | 60 ms, 8.2 MB | MEASURED |
| — decode + rank in numpy | 158 ms | MEASURED |
| Per candidate | 15 µs fetch, 39 µs rank | MEASURED |

**The architecture today.** Vectors are `bytea`. There is no `pgvector`
extension and no ANN index. Search selects the most recent
`CONTENT_INDEX_MAX_CANDIDATES` (4,000) rows matching its filters, ships them to
the application, and ranks with numpy.

---

## 2. The constraints, in the order they bite

Not in the order they are usually discussed. This ordering is the plan's main
claim, and it is derived from §1 rather than from intuition.

### 2.1 Retrieval quality — **already degrading**

Ranking inside a recency window is a search only while the window holds most of
the corpus. Measured on the 6,824-row internal corpus, taking the true top 5 for
a query and asking how many survive the window:

| Window | Corpus visible | True top-5 reachable |
|---|---|---|
| 4,000 (current) | 58.6% | **4 of 5** |
| 2,000 | 29.3% | **0 of 5** |
| 1,000 | 14.7% | 0 of 5 |

**CORRECTED 2026-08-03.** The original table was a single query, and its five
"best" results were five copies of one boilerplate template — so it measured
one temporal cluster, not retrieval in general. Re-run across six queries,
counting how many of each true top-5 survive the window:

| Window | Coverage | recall@5 (6 queries) | Range |
|---|---|---|---|
| 4,000 (current) | 58.6% | **77%** | 3–5 of 5 |
| 2,000 | 29.3% | **27%** | 0–5 of 5 |
| 1,000 | 14.7% | 10% | 0–2 of 5 |

The cliff **generalises** — it is not an artefact of one query. Two caveats
that the single-query version hid: several queries return only 2–3 *distinct*
texts among their top 5, so part of the apparent loss is duplicate answers; and
one query kept 5/5 even at 29% coverage, so the loss is query-dependent rather
than uniform.

Severity is therefore lower than first stated, and the direction is confirmed:
at today's volume roughly a quarter of the best results are already unreachable,
and this binds well before any capacity limit.

### 2.2 Query latency — **dominated by the embedding call**

450 ms of the 668 ms is one network round trip to the embedding API, and it is
**constant in corpus size**. No amount of indexing improves it. At every tier
below, latency work that ignores this is optimising the smaller half.

### 2.3 Search compute — binds ~50k records without ANN

EXTRAPOLATED from 15 µs fetch + 39 µs rank per candidate, assuming linear cost
(valid: it is a table read plus one matrix product):

| Records | Fetch + rank a *full* scan | Bytes moved |
|---|---|---|
| 7,310 | 0.4 s | 15 MB |
| 100,000 | 5.5 s | 205 MB |
| 1,000,000 | 55 s | 2.0 GB |

**CORRECTED 2026-08-03.** The first version said "lifting the cap is not an
option at any tier". That is wrong at the current tier and was not measured
before being asserted. Measured:

| Cap | Coverage | Fetch + rank |
|---|---|---|
| 4,000 (current) | 55% | 151 ms |
| 8,000 | **100%** | **263 ms** |
| 20,000 | 100% | 266 ms |

**Full coverage costs 263 ms today.** Raising the cap restores 100% recall for
about 110 ms of added latency on a 668 ms search, and needs no migration. At
~33 µs/candidate it stays viable to roughly **15–20k records**, so it buys
2–3× headroom rather than a permanent answer — but it is the correct FIRST
action, and ANN is what it buys time for.

### 2.4 Consolidation throughput — binds ~1M records

**CORRECTED 2026-08-03.** The first version of this table used "est. 30 ms per
entity", a figure with no measurement behind it. Measured across 8 randomly
chosen entities of 5–40 records: **median 5.1 ms**, not 30. The original table
overstated consolidation cost by ~6× and mis-ranked this constraint.

MEASURED: 252 ms for a 737-record entity; median 5.1 ms for an average one.

| Records | Entities | Full pass (5.1 ms/entity median) |
|---|---|---|
| 100k | ~6k | ~30 s |
| 1M | ~60k | ~5 min |
| 10M | ~600k | ~51 min |
| 100M | ~6M | **~8.5 h** |

A nightly full pass stops fitting in a night between **10M and 100M**, not
between 1M and 10M. Incremental consolidation is therefore a 10M+ concern, and
the sequencing in §10 is corrected accordingly.

### 2.5 Storage — binds last

EXTRAPOLATED at 5,000 bytes/record MEASURED: 100k → 0.5 GB, 1M → 5 GB,
10M → 50 GB, 100M → **500 GB**. Storage is the least urgent constraint and the
one most often planned for first.

---

## 3. Indexing strategy and ANN migration

### 3.1 Migrate `bytea` → `vector` and index it

The single change that resolves §2.1 and §2.3 together.

1. `CREATE EXTENSION vector;`
2. `ALTER TABLE content_embeddings ADD COLUMN embedding_v vector(512);`
3. Backfill from the existing `bytea` — **no re-embedding required**, the floats
   are already there. Batched (§6.1) so it is resumable.
4. Build `HNSW` (not IVFFlat): no training step, no degradation as rows are
   added, and this table is append-heavy.
   ```sql
   CREATE INDEX CONCURRENTLY idx_ce_hnsw
       ON content_embeddings USING hnsw (embedding_v vector_cosine_ops)
       WITH (m = 16, ef_construction = 64);
   ```
5. Move ranking into SQL: `ORDER BY embedding_v <=> $1 LIMIT n`, with the
   existing filters as a `WHERE` clause.
6. **Retire `MAX_CANDIDATES`.** The recency window exists only because ranking
   happens in the application.

**Filtered ANN is the trap.** Searches filter by `visibility`, `source_type`
and `speech_act`. A selective filter plus HNSW can under-return, because the
graph is walked before the filter is applied. Mitigations, in order of
preference: partial indexes per `visibility` (there are two values, and the
customer/internal split is a hard security boundary anyway); raising
`hnsw.ef_search`; or pre-filtering to an ID set when the filter is very
selective. **This must be validated with a recall test, not assumed** — see §7.

### 3.2 Supporting indexes

Already present and adequate: entity, account, contact, party, parent, actor.
Add at 10M+: `(visibility, occurred_at DESC)` for the recency paths that remain,
and `BRIN` on `occurred_at` for partition pruning (§4).

### 3.3 Dimensionality

512 dims is already a reduction from the model's 1,536. At 10M+, halving to 256
via Matryoshka truncation would halve vector storage and roughly halve index
size, at a recall cost that **must be measured on this corpus** before adoption.
Listed as an option, not a recommendation.

---

## 4. Partitioning

Applies from **10M**; unnecessary below.

- `content_embeddings` **RANGE partitioned by `occurred_at`**, monthly. It is
  append-mostly and time-ordered, retention is time-based, and most queries are
  time-bounded — the three conditions that make partitioning pay.
- Each partition carries its own HNSW index. Smaller indexes build faster,
  rebuild independently, and old partitions can move to cheaper storage.
- Retention becomes `DROP PARTITION` instead of a mass `DELETE` — which also
  removes the largest source of `undeclared_deletions` noise.
- `customer_memories` stays unpartitioned to 100M records; at 848 rows per 7,310
  records it reaches ~11M rows at 100M and is comfortably indexed.

**Cost:** partitioned tables cannot have a global unique index that excludes the
partition key. `customer_memories_claim_key` is a uniqueness guarantee the
assertion gate depends on — this is precisely why `customer_memories` is not
partitioned.

---

## 5. Sharding

**Not before 100M, and only then by tenant.**

The natural shard key is the tenant, because every query is already
entity-scoped and no query needs to join across tenants. That is also the only
key that avoids cross-shard fan-out for search.

Blockers to state plainly: the platform currently runs **0 tenants**, `tenancy`
resolves to one schema, and RLS is not implemented (0 policies). Sharding before
tenant isolation exists would be building a distribution layer for a dimension
the data model does not yet have. **The correct order is: tenant isolation →
per-tenant partitioning → sharding.**

Read replicas are the cheaper intermediate step at 10M: search is read-only and
tolerates seconds of staleness.

---

## 6. Batching, incremental consolidation, caching

### 6.1 Batching

- **Backfill and re-embedding:** chunks of 5–10k rows by primary key, committed
  per chunk, resumable from a cursor. Never a single transaction — a 100M-row
  transaction is an outage.
- **Every bulk pass must declare itself**: `SET LOCAL app.repair_key` and
  `app.suppress_events='notify'`. Without the latter, row triggers emit an event
  per row and flood the notification queue; this has happened.
- **Embedding API:** batch 100–500 texts per call. The 450 ms is per *call*, not
  per text, so batching is a 100× throughput improvement on ingest.

### 6.2 Incremental consolidation

**The mechanism already exists and is the reason a full pass is avoidable.**
Every memory carries an `evidence_hash`; consolidation skips an entity whose
evidence is unchanged. What is missing is a cheap way to find the entities that
*did* change.

Add a `content_embeddings (entity, max(occurred_at))` watermark, and consolidate
only entities with new records since their last pass. EXTRAPOLATED: if 1% of
entities see activity daily, a 100M-record deployment re-consolidates ~60k
entities per night — **~30 min**, against ~50 h for a full pass.

Full passes remain necessary after any change to the derivation, because the
generator fingerprint changes and every memory is genuinely stale. Those are
planned, batched, and off-peak — not nightly.

### 6.3 Caching

In order of measured value:

1. **Query-embedding cache** — 450 ms of every 668 ms search. Keyed by exact
   normalised query text, TTL days, hit rate high on a support corpus where the
   same questions recur. **This is the single largest latency win at every
   tier**, and it is independent of record count.
2. **Recall cache** per `(entity, audience)`, invalidated on that entity's
   consolidation. Memory is derived data with a known invalidation point.
3. **No cache on the assertion gate.** It reads signature and verification state
   that must be current; a stale gate is a correctness failure, not a slow one.

---

## 7. Expected latency, and how each tier is validated

Targets are p95 for search. **Every row is a hypothesis until the gate passes.**

| Tier | Search (target) | Full consolidation | Validation gate |
|---|---|---|---|
| **100k** | ~470 ms cold, ~20 ms cached | ~3 min | HNSW recall@10 ≥ 0.95 vs exhaustive scan on this corpus; `search.reachable_share` = 1.0 |
| **1M** | ~480 ms cold, ~20 ms cached | ~30 min | The above, plus 50-concurrent-user soak with no pool exhaustion |
| **10M** | ~500 ms cold, ~25 ms cached | ~5 h (incremental: ~10 min) | The above, plus partition pruning confirmed in `EXPLAIN`, and index rebuild inside a maintenance window |
| **100M** | ~500 ms cold, ~25 ms cached | incremental only, ~30 min | The above, plus tenant isolation proven and cross-shard fan-out measured |

Search latency is **flat across tiers** because HNSW is sub-linear and the
embedding call dominates. That is the plan's central prediction and the first
thing the gate should try to falsify.

**Concurrency is the least-evidenced area.** Connection pooling has one suite
run behind it, and concurrency defects do not appear in single-threaded tests.
No tier passes without a concurrent soak.

---

## 8. Expected infrastructure cost

**ESTIMATED.** Compute and storage use typical managed-Postgres list pricing
(Railway/RDS class, 2026); embedding cost is computable from published rates.
Treat as order-of-magnitude for planning, not a quotation.

| Tier | Storage | Suggested instance | DB/month | One-time embedding |
|---|---|---|---|---|
| 100k | ~0.5 GB (+0.3 GB HNSW) | 2 vCPU / 4 GB | $20–50 | ~$0.02 |
| 1M | ~5 GB (+2.2 GB) | 4 vCPU / 16 GB | $100–200 | ~$0.20 |
| 10M | ~50 GB (+22 GB) | 8 vCPU / 64 GB + replica | $500–900 | ~$2 |
| 100M | ~500 GB (+220 GB) | 16 vCPU / 256 GB, sharded | $3,000–6,000 | ~$20 |

Notes that matter more than the totals:

- **HNSW wants to sit in RAM.** The index sizes above (ESTIMATED at
  `dims×4 + m×2×4` ≈ 2.2 KB/vector at m=16) drive the memory column, not the
  data. At 10M the index alone is ~22 GB — this is why the instance jumps.
- **Embedding is trivially cheap and effectively one-time**
  (`text-embedding-3-small`, $0.02/1M tokens, ~100 tokens/record). Re-embedding
  100M records costs ~$20 in API charges and days in wall-clock — the schedule
  is the constraint, not the invoice.
- **The query-embedding cache is a cost lever, not just a latency one**, at
  high query volume.
- Excluded: application compute, egress, backups, the LLM inference that
  dominates total spend in practice. This table covers the data layer only.

---

## 8b. Review board findings — 2026-08-03

Three claims failed when measured under conditions closer to production. All
three changed a recommendation.

**Throughput is capped at ~8 searches/second and does not improve with
concurrency.** MEASURED: 1 worker 6.6/s, 32 workers 8.3/s, while p50 latency
rose 150 ms → 3,084 ms. Adding concurrency adds only queueing. This ceiling is
independent of corpus size and was not in the original plan at all — it is
likely to bind before data volume does, and no tier gate covered it.

**Raising the cap to 8,000 costs 43% of throughput.** The "implement now,
110 ms" recommendation was measured SERIALLY. Under 16-way concurrency:
8.4/s → 4.8/s, p50 1,683 ms → 3,224 ms. It buys recall by spending capacity,
which is a real trade rather than a free win.

**The connection pool fails open into unbounded connections.** At 32 concurrent
against `POOL_MAX=16`, sixteen requests logged "pool unavailable, falling back
direct" and opened their own connections. No errors, which is the problem: under
sustained load this converts a queueing limit into `max_connections` pressure on
PostgreSQL — a database-wide outage rather than a slow endpoint.

**And the recall problem is narrower than stated.** MEASURED: only the
UNFILTERED internal search is truncated. Every filtered shape is complete at
this volume — customer audience 486 rows, cases 148, conversation messages 167,
commitments 10. `search.reachable_share` implied broad degradation and has been
replaced by `search.worst_case_coverage`, labelled as the ceiling it is.

## 8c. ANN measured, and the process model — 2026-08-04

pgvector 0.8.6 became available, so ANN moved from modelled to MEASURED.
Backfill 1,960 rows/s (100k ~1 min, 1M ~6 min); HNSW build 0.8 s on 7,543
vectors; index 17 MB, matching the 2.2 KB/vector estimate.

**Recall: the filtered-ANN trap was real and the predicted mitigation works.**
At the default ef_search=40, recall@10 was 60% unfiltered and 31.7% on the
customer path. The exact top-10 were all TIED at one distance — a template
cluster — and HNSW was missing it outright, not tie-breaking differently. At
**ef_search=100, recall is 100%** by both ID overlap and distance equivalence.

**The binding constraint was the single-process deployment.** `main.py` runs
uvicorn with no `workers=`. Same 16 concurrent requests, same index, same
database:

    1 process x 16 threads   p95 588 ms    44/s   FAIL
    4 processes x 4 threads  p95 181 ms   223/s   PASS

Throughput scales 8.42x to 8 processes with p95 flat at 167-198 ms. PostgreSQL
is nowhere near saturated.

**But processes alone are NOT sufficient — this refutes the previous entry.**
Measured across processes at 4 threads each, against the p95 <= 250 ms SLO:

| Concurrency | EXACT p95 | ANN p95 (ef=100) |
|---|---|---|
| 4 | 108 ms PASS | 68 ms PASS |
| 8 | 127 ms PASS | 168 ms PASS |
| 16 | **286 ms FAIL** | 154 ms PASS |
| 32 | **395 ms FAIL** | 214 ms PASS |

Exact search fails at 16+ concurrent however many processes it is given,
because each request still ships 4,000 rows. ANN passes everywhere tested and
delivers ~2x the throughput at every process count.

**Both changes are required.** The prior recommendation — "processes first, ANN
becomes nice-to-have" — is REFUTED by measurement. The resident numpy matrix is
no longer needed and its erasure and staleness costs are not worth paying.

## 9. What would invalidate this plan

Stated up front so the gates are meaningful:

- **Filtered HNSW under-returns.** If recall@10 with `visibility` and
  `speech_act` filters cannot reach 0.95, partial indexes are mandatory rather
  than preferred, and §3.1 needs rework.
- **The embedding call is not cacheable in practice** — if real queries are
  mostly unique, §6.3.1 evaporates and latency stays at ~500 ms.
- **Entity distribution is far more skewed at scale.** The plan assumes ~16.8
  records/entity. One entity with 10M records breaks incremental consolidation,
  and `MAX_RECORDS` clipping (already measured, already reported) becomes the
  binding correctness issue instead.
- **`customer_memories` grows faster than linearly** in records. It is 848 per
  7,310 today; if derivation changes raise that ratio, the unpartitioned
  decision in §4 needs revisiting.

---

## 10. Sequence

**CORRECTED 2026-08-03** after measuring the two figures the original sequence
depended on.

1. **Now, today** — raise `CONTENT_INDEX_MAX_CANDIDATES` to 8,000. Restores
   100% recall for ~110 ms, no migration, reversible by an environment
   variable. The original plan skipped this because it asserted the cap could
   not be raised, without measuring it.
2. **Now** — query-embedding cache (450 ms of 668 ms, tier-independent) and the
   `search.reachable_share` metric.
3. **Before ~20k records** — pgvector + HNSW, retire the cap. Still urgent on
   quality grounds; step 1 buys the time to do it properly rather than
   removing the need.
4. **Before 1M** — concurrent soak. Pooling has one suite run behind it.
5. **Before 10M** — monthly partitioning; read replica; incremental
   consolidation watermark (moved later: measured cost is 6× lower than the
   original estimate).
6. **Before 100M** — tenant isolation, then sharding. Not before.
