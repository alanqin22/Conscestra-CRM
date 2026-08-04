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

**MEASURED.** It is a cliff, not a slope. At today's volume the cap already
drops one of the five best answers; at roughly **2× this data** a search returns
nothing relevant while still returning confident-looking results. This binds at
~15k records — long before any capacity limit.

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

Lifting the cap is not an option at any tier. The cap is load-bearing, which is
why §2.1 has to be solved by indexing rather than by raising it.

### 2.4 Consolidation throughput — binds ~1M records

MEASURED 252 ms for a 737-record entity. At avg 16.8 records/entity,
EXTRAPOLATED entity counts and a full pass, single-threaded:

| Records | Entities | Full pass (est. 30 ms/entity) |
|---|---|---|
| 100k | ~6k | ~3 min |
| 1M | ~60k | ~30 min |
| 10M | ~600k | ~5 h |
| 100M | ~6M | **~50 h** |

A nightly full pass stops fitting in a night between 1M and 10M.

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

1. **Now** — query-embedding cache (largest latency win, tier-independent) and
   the `search.reachable_share` metric, so §2.1 is visible while it is still
   mild.
2. **Before 15k records** — pgvector + HNSW, retire `MAX_CANDIDATES`. This is
   urgent on *quality* grounds, not capacity; §2.1 already costs one of five
   best answers.
3. **Before 1M** — incremental consolidation watermark; concurrent soak.
4. **Before 10M** — monthly partitioning; read replica.
5. **Before 100M** — tenant isolation, then sharding. Not before.
