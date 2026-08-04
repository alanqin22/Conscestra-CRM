"""Shared embedding transport, storage format and similarity math.

One place for the three things every embedding index needs, so a fix lands once
instead of per-index. Before this, `semantic.py` owned all of it privately and
carried a silent correctness bug (audit finding #3):

    staleness was keyed on the CONTENT HASH ALONE.

The `model` column was written on insert and never compared on refresh. Change
EMBED_MODEL and every stored vector stays in the OLD model's space while queries
are embedded in the NEW one — cosine across incompatible spaces. Nothing raises,
nothing logs; retrieval just quietly gets worse. `index_key()` below makes the
model and dimension part of the staleness identity, and `decode()` REFUSES a
vector whose geometry does not match the query's, so the failure mode is a
visible miss rather than a plausible wrong answer.

STORAGE — float32 bytea, not jsonb. jsonb stores a vector as its text
representation (~20 KB per 1536-dim vector); packed float32 is 4 bytes per
dimension. Across the ~6.8k-row CRM corpus that is ~136 MB versus ~14 MB at 512
dims, which is the difference between fitting and not fitting on the Railway
volume that is already near full.

DIMENSIONS — 512, not the model default of 1536. text-embedding-3-small is
Matryoshka-trained, so a truncated 512-dim vector keeps almost all of its
retrieval quality at a third of the storage and a third of the dot-product cost.
Raising EMBED_DIMS is safe: rows re-embed automatically because dims are part of
the staleness key.

NO PROCESS-LOCAL VECTOR CACHE. Callers fetch the candidate rows they need and
pass them here. That keeps every replica consistent under HA and removes the
whole cache-invalidation failure class — see content_index.search().
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
import threading
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("embeddings")

MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
DIMS = int(os.getenv("EMBED_DIMS", "512"))
TIMEOUT = int(os.getenv("EMBED_TIMEOUT_SECS", "30"))
MAX_CHARS = int(os.getenv("EMBED_MAX_CHARS", "8000"))

# Models that accept the `dimensions` parameter. For anything else we send no
# dimensions and store whatever the model returns (recorded in the dims column,
# so the staleness key still protects us).
_SUPPORTS_DIMS = ("text-embedding-3-small", "text-embedding-3-large")


def index_key(text: str, model: Optional[str] = None,
              dims: Optional[int] = None) -> Tuple[str, str, int]:
    """The full staleness identity of an embedded row: (content_hash, model, dims).

    A row is stale if ANY of the three differs from current. Keying on the hash
    alone — the pre-2026-07-30 behaviour — meant a model change was invisible."""
    h = hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()
    return (h, model or MODEL, int(dims or DIMS))


# ── Storage format ───────────────────────────────────────────────────────────

def encode(vec: Sequence[float]) -> bytes:
    """Pack a vector as little-endian float32. 4 bytes/dim, no text overhead."""
    return struct.pack(f"<{len(vec)}f", *vec)


def decode(blob: bytes, expect_dims: Optional[int] = None) -> Optional[List[float]]:
    """Unpack a float32 blob. Returns None when the width does not match what
    the caller expects — a vector of a different geometry is not comparable, and
    silently comparing it is the bug this module exists to prevent."""
    if not blob:
        return None
    n = len(blob) // 4
    if expect_dims is not None and n != int(expect_dims):
        return None
    try:
        return list(struct.unpack(f"<{n}f", blob[:n * 4]))
    except struct.error:
        return None


# ── Transport ────────────────────────────────────────────────────────────────

def embed(texts: List[str], model: Optional[str] = None,
          dims: Optional[int] = None) -> Optional[List[List[float]]]:
    """Embed a batch. Returns None on ANY failure so callers degrade to keyword
    retrieval rather than surfacing an error to a customer."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key or not texts:
        return None
    model = model or MODEL
    dims = int(dims or DIMS)
    payload: Dict[str, Any] = {
        "model": model,
        "input": [(t or "")[:MAX_CHARS] for t in texts],
    }
    if model.startswith(_SUPPORTS_DIMS):
        payload["dimensions"] = dims
    try:
        import requests
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"[embeddings] {model} HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        vecs = [d["embedding"] for d in data]
    except Exception as exc:
        logger.warning(f"[embeddings] call failed: {exc}")
        return None

    if len(vecs) != len(texts):
        logger.warning(f"[embeddings] arity mismatch: {len(vecs)} for {len(texts)}")
        return None
    # A model that ignored `dimensions` must not be stored as if it honoured it.
    got = len(vecs[0]) if vecs else 0
    if got != dims:
        logger.info(f"[embeddings] {model} returned {got} dims (asked {dims}); "
                    f"storing {got}")
    return vecs


# ── Similarity ───────────────────────────────────────────────────────────────

def rank(query_vec: Sequence[float],
         candidates: Iterable[Tuple[Any, bytes, int]],
         limit: int = 5, min_sim: float = 0.0) -> List[Tuple[Any, float]]:
    """Cosine-rank (key, blob, dims) candidates against a query vector.

    Vectorized through numpy when available — one matrix product over the whole
    candidate set instead of a Python loop per row — with a pure-Python fallback
    so a missing numpy degrades speed, never correctness.

    Candidates whose dims differ from the query's are DROPPED, not coerced. That
    is what turns a mid-flight model change into "no semantic hits" (visible,
    and the caller falls back to keyword search) instead of "confidently wrong
    neighbours" (invisible)."""
    q_dims = len(query_vec)
    keys: List[Any] = []
    blobs: List[bytes] = []
    skipped = 0
    # COLLECT BYTES, DECODE ONCE.
    #
    # This used to call decode() per candidate — struct.unpack returning a
    # Python list — and then hand np.asarray a list of 4,000 lists. Both steps
    # are Python-level and hold the GIL, which made ranking, not PostgreSQL,
    # the throughput ceiling of the whole search path: measured 9.8 searches/s
    # single-threaded and 9.2/s on sixteen threads, i.e. no concurrency benefit
    # at all.
    #
    # Concatenating the blobs and taking ONE np.frombuffer moves the work into
    # C. Measured on the same 4,000 candidates: 101 ms -> 5.0 ms single-threaded
    # (20x), and 9.2/s -> 573/s at sixteen threads (62x), with byte-identical
    # results — same top-5 keys, zero score delta.
    #
    # The geometry check is unchanged and still happens per candidate. A vector
    # of a different width is DROPPED, never coerced: that is what turns a
    # mid-flight model change into "no semantic hits" rather than "confidently
    # wrong neighbours", and it is worth the one Python-level comparison.
    expected = q_dims * 4
    for key, blob, dims in candidates:
        if int(dims or 0) != q_dims or not blob or len(blob) < expected:
            skipped += 1
            continue
        keys.append(key)
        # SLICE, DO NOT CONVERT. psycopg2 hands back a memoryview, and
        # `bytes(blob)[:n]` on one allocates twice per candidate: measured
        # 66 ms for 4,000 memoryviews against 4.7 ms when they were already
        # bytes — a 14x difference in the dominant cost of the whole search.
        # Slicing a memoryview is O(1) and copies nothing, and b"".join()
        # consumes memoryviews and bytes alike.
        blobs.append(blob[:expected])
    if skipped:
        logger.info(f"[embeddings] skipped {skipped} vector(s) of a different "
                    f"model/geometry — re-index to include them")
    if not blobs:
        return []

    try:
        import numpy as np
        M = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), q_dims)
        q = np.asarray(query_vec, dtype=np.float32)
        norms = np.linalg.norm(M, axis=1)
        qn = float(np.linalg.norm(q)) or 1.0
        norms = np.where(norms == 0, 1.0, norms)
        sims = (M @ q) / (norms * qn)
        order = np.argsort(-sims)[:max(int(limit), 1)]
        return [(keys[i], float(sims[i])) for i in order
                if float(sims[i]) >= min_sim]
    except ImportError:
        # numpy absent: decode row by row. Slow, correct, and never the path a
        # deployment takes — numpy is a hard requirement in requirements.txt.
        import math
        rows = [decode(b, q_dims) for b in blobs]
        qn = math.sqrt(sum(x * x for x in query_vec)) or 1.0
        out = []
        for k, v in zip(keys, rows):
            vn = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append((k, sum(a * b for a, b in zip(v, query_vec)) / (vn * qn)))
        out.sort(key=lambda t: -t[1])
        return [t for t in out[:max(int(limit), 1)] if t[1] >= min_sim]


# Query-embedding cache.
#
# MEASURED: a novel query costs 433-471 ms, and re-embedding the SAME query
# seconds later cost 467 ms — there was no cache of any kind. After ranking was
# vectorised this became the largest remaining component of search latency by a
# wide margin, and unlike everything else in the path it is unaffected by
# indexes, hardware or corpus size.
#
# Keyed on (model, dims, exact text). The embedding of a string is deterministic
# for a given model, so there is nothing to invalidate and no TTL: a changed
# model changes the key. Whitespace is normalised because "billing  error" and
# "billing error" are the same question asked twice.
#
# FAILURES ARE NOT CACHED. Storing a None would convert one transient API error
# into a permanently dead query, which is a far worse failure than paying for
# the call again.
_EMBED_CACHE: "OrderedDict[Tuple[str, int, str], List[float]]" = OrderedDict()
_EMBED_CACHE_LOCK = threading.Lock()
# 512 floats ≈ 2 KB, so 20k entries ≈ 40 MB — bounded, and far more than the
# distinct-query volume any support corpus produces in a day.
EMBED_CACHE_MAX = int(os.getenv("EMBED_CACHE_MAX", "20000"))
_EMBED_STATS = {"hits": 0, "misses": 0}


def embedding_cache_stats() -> Dict[str, Any]:
    """Hit rate, for the observability surface. A cache nobody measures is a
    cache nobody knows is working."""
    with _EMBED_CACHE_LOCK:
        h, m = _EMBED_STATS["hits"], _EMBED_STATS["misses"]
        return {"hits": h, "misses": m, "entries": len(_EMBED_CACHE),
                "capacity": EMBED_CACHE_MAX,
                "hit_rate": round(h / (h + m), 4) if (h + m) else None}


def embed_one(text: str, model: Optional[str] = None,
              dims: Optional[int] = None) -> Optional[List[float]]:
    key = (model or MODEL, int(dims or DIMS), " ".join((text or "").split()))
    if not key[2]:
        return None
    with _EMBED_CACHE_LOCK:
        hit = _EMBED_CACHE.get(key)
        if hit is not None:
            _EMBED_CACHE.move_to_end(key)       # LRU
            _EMBED_STATS["hits"] += 1
            return list(hit)
        _EMBED_STATS["misses"] += 1

    vecs = embed([text], model, dims)
    vec = vecs[0] if vecs else None
    if vec:
        with _EMBED_CACHE_LOCK:
            _EMBED_CACHE[key] = list(vec)
            while len(_EMBED_CACHE) > EMBED_CACHE_MAX:
                _EMBED_CACHE.popitem(last=False)
    return vec


__all__ = ["MODEL", "DIMS", "index_key", "encode", "decode", "embed",
           "embed_one", "rank", "embedding_cache_stats"]
