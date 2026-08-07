"""Semantic retrieval — the "semantic layer" over the knowledge base (audit #1).

FTS (knowledge.search) is deterministic and precise but vocabulary-brittle: a
caller asking about "sending something back" never matches an article titled
"Refund policy" unless they share words. This module adds the meaning side;
knowledge.rag_block fuses both (reciprocal-rank) so retrieval is HYBRID —
keyword precision plus semantic recall.

    index    kb_embeddings caches ONE vector per ACTIVE article, keyed by a
             content hash — re-embedding happens only when the article text
             actually changes; retired articles drop out. The index refreshes
             lazily, at most every SEM_REFRESH_SECS, so serving never waits
             on indexing more than once per window.
    search   embed the query (one small API call, metered via llm_usage) →
             cosine against the cached vectors IN PROCESS. At KB scale (tens
             of articles) this beats pgvector: zero extensions, zero volume
             risk on the near-full Railway DB.
    degrade  flag off / no key / table missing / API error → [] — every
             caller falls back to pure FTS, exactly the pre-semantic
             behavior. Retrieval NEVER breaks on the semantic path.

CONFIG (env)
  SEMANTIC_ENABLED  0      master switch (dark in code; .env flips it)
  SEM_MIN_SIM       0.33   cosine floor — below this a hit is noise
  SEM_REFRESH_SECS  300    how often the lazy index may refresh
  EMBED_MODEL       text-embedding-3-small
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("semantic")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("SEMANTIC_ENABLED", "0")
MIN_SIM = float(os.getenv("SEM_MIN_SIM", "0.33"))
REFRESH_SECS = int(os.getenv("SEM_REFRESH_SECS", "300"))
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")

_EMBED_BATCH = 16          # max articles embedded per lazy refresh pass

# "Decisive winner" fallback (see search()). DECISIVE_MIN is a hard noise
# floor — below it nothing is ever accepted, whatever the margin. Setting
# DECISIVE_MARGIN above 1.0 disables the rule entirely.
DECISIVE_MIN = float(os.getenv("SEM_DECISIVE_MIN", "0.25"))
DECISIVE_MARGIN = float(os.getenv("SEM_DECISIVE_MARGIN", "0.10"))

# ── Query-vector cache ───────────────────────────────────────────────────────
# Support questions repeat constantly across callers ("where is my order",
# "what's your refund policy"), and embedding the same string twice costs the
# same ~450ms network round trip every time. Keyed by (model, query) because a
# vector from a different model is not comparable — the same trap ensure_index
# already guards against on the article side. Bounded FIFO: no eviction policy
# cleverness is warranted for a few hundred short strings.
_QCACHE_MAX = int(os.getenv("SEM_QUERY_CACHE", "256"))
_QCACHE: "OrderedDict[Tuple[str, str], List[float]]" = OrderedDict()

# { article_uuid: (vector, l2norm) } — the whole KB fits in memory with room
# to spare (1536 floats × tens of articles).
_VECTORS: Dict[str, Tuple[List[float], float]] = {}
_LAST_REFRESH = 0.0


# ============================================================================
# Embedding call (metered, best-effort)
# ============================================================================

def _embed(texts: List[str]) -> Optional[List[List[float]]]:
    """Embed texts via the OpenAI embeddings API. None on any failure."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key or not texts:
        return None
    t0 = time.time()
    ok, p_tok = False, 0
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": EMBED_MODEL, "input": [t[:8000] for t in texts]},
            timeout=15)
        resp.raise_for_status()
        data = resp.json()
        p_tok = int((data.get("usage") or {}).get("prompt_tokens") or 0)
        vecs = [d["embedding"] for d in
                sorted(data["data"], key=lambda d: d["index"])]
        ok = len(vecs) == len(texts)
        return vecs if ok else None
    except Exception as exc:
        logger.warning(f"[semantic] embed failed (FTS-only fallback): {exc}")
        return None
    finally:
        try:
            from app.core.llm_meter import _record
            _record("kb_semantic", EMBED_MODEL, "embed", p_tok, 0,
                    p_tok == 0, ok, int((time.time() - t0) * 1000))
        except Exception:
            pass


def _content_hash(a: Dict[str, Any]) -> str:
    text = "|".join([a.get("title") or "", a.get("problem") or "",
                     a.get("answer") or "",
                     " ".join(a.get("keywords") or [])])
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32]


def _article_text(a: Dict[str, Any]) -> str:
    """What gets embedded — includes the ANSWER, which search_tsv leaves out."""
    return (f"{a.get('title') or ''}\n{a.get('problem') or ''}\n"
            f"{' '.join(a.get('keywords') or [])}\n{a.get('answer') or ''}")


# ============================================================================
# Lazy index
# ============================================================================

def ensure_index(force: bool = False) -> Dict[str, Any]:
    """Bring kb_embeddings + the in-process cache up to date with the ACTIVE
    article set. Cheap when nothing changed; embeds at most _EMBED_BATCH new/
    edited articles per pass (the next pass catches the rest). Never raises."""
    global _LAST_REFRESH, _VECTORS
    if not force and _VECTORS and time.time() - _LAST_REFRESH < REFRESH_SECS:
        return {"ok": True, "cached": len(_VECTORS), "refreshed": False}
    out = {"ok": False, "cached": len(_VECTORS), "refreshed": False}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT article_uuid::text, title, problem, answer,
                                  keywords
                           FROM knowledge_articles WHERE status='active'""")
            cols = [d[0] for d in cur.description]
            active = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
            cur.execute("SELECT article_uuid::text, content_hash, embedding, model "
                        "FROM kb_embeddings")
            stored = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

            stale = [u for u in stored if u not in active]
            if stale:
                cur.execute("DELETE FROM kb_embeddings "
                            "WHERE article_uuid = ANY(%s::uuid[])", (stale,))

            # Staleness is (content_hash, MODEL) — not the hash alone.
            # The model column was written on insert and never compared, so
            # changing EMBED_MODEL left every stored vector in the OLD model's
            # space while queries were embedded in the NEW one. Cosine across
            # two embedding spaces returns confident nonsense: no exception, no
            # log, retrieval just quietly degrades (audit finding #3).
            todo = [u for u, a in active.items()
                    if (stored.get(u, ("", None, ""))[0],
                        stored.get(u, ("", None, ""))[2]) != (_content_hash(a), EMBED_MODEL)]
            embedded = 0
            if todo:
                batch = todo[:_EMBED_BATCH]
                vecs = _embed([_article_text(active[u]) for u in batch])
                if vecs:
                    for u, v in zip(batch, vecs):
                        cur.execute(
                            """INSERT INTO kb_embeddings
                                 (article_uuid, content_hash, model, embedding)
                               VALUES (%s::uuid,%s,%s,%s::jsonb)
                               ON CONFLICT (article_uuid) DO UPDATE SET
                                 content_hash=EXCLUDED.content_hash,
                                 model=EXCLUDED.model,
                                 embedding=EXCLUDED.embedding,
                                 updated_at=now()""",
                            (u, _content_hash(active[u]), EMBED_MODEL,
                             json.dumps(v)))
                        stored[u] = (_content_hash(active[u]), v, EMBED_MODEL)
                    embedded = len(batch)
        conn.commit()

        # Build the new index in a SEPARATE dict and swap it in atomically.
        # Clearing the live one in place left a window — now widened, because
        # retrieval calls this from a worker thread — in which a concurrent
        # search saw a half-populated index and silently returned fewer hits,
        # or none. A rebind is atomic; readers hold the old dict until it is.
        fresh: Dict[str, Tuple[List[float], float]] = {}
        wrong_model = 0
        for u, (_h, emb, mdl) in stored.items():
            if u not in active:
                continue
            # Never load a vector from a different model into the search space.
            # Dropping it means a MISS (the caller falls back to FTS, visibly);
            # keeping it would mean a confident wrong neighbour, invisibly. The
            # next pass re-embeds it because model is now part of `todo`.
            if mdl and mdl != EMBED_MODEL:
                wrong_model += 1
                continue
            vec = emb if isinstance(emb, list) else json.loads(emb)
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            fresh[u] = (vec, norm)
        _VECTORS = fresh                      # atomic swap (see above)
        if wrong_model:
            logger.info(f"[semantic] {wrong_model} article vector(s) are on a "
                        f"previous model — excluded until re-embedded")
        _LAST_REFRESH = time.time()
        out.update(ok=True, cached=len(_VECTORS), refreshed=True,
                   embedded=embedded, pending=max(0, len(todo) - embedded))
        if embedded:
            logger.info(f"[semantic] embedded {embedded} article(s), "
                        f"index size {len(_VECTORS)}")
    except Exception as exc:
        logger.debug(f"[semantic] index refresh skipped (table applied?): {exc}")
        out["error"] = str(exc)[:200]
    finally:
        conn.close()
    return out


# ============================================================================
# Search
# ============================================================================

def search(query: str, limit: int = 4,
           min_sim: Optional[float] = None) -> List[Dict[str, Any]]:
    """Nearest ACTIVE articles by meaning: [{'article_uuid', 'sim'}, …] best
    first, [] when disabled or on any failure (callers stay FTS-only)."""
    if not ENABLED or not (query or "").strip():
        return []
    ensure_index()
    if not _VECTORS:
        return []
    qtext = query.strip()[:2000]
    ckey = (EMBED_MODEL, qtext)
    q = _QCACHE.get(ckey)
    if q is None:
        qv = _embed([qtext])
        if not qv:
            return []
        q = qv[0]
        _QCACHE[ckey] = q
        while len(_QCACHE) > _QCACHE_MAX:
            _QCACHE.popitem(last=False)
    qnorm = math.sqrt(sum(x * x for x in q)) or 1.0
    floor = MIN_SIM if min_sim is None else min_sim
    sims = []
    for u, (vec, norm) in _VECTORS.items():
        dot = sum(a * b for a, b in zip(q, vec))
        sims.append({"article_uuid": u, "sim": round(dot / (qnorm * norm), 4)})
    sims.sort(key=lambda h: h["sim"], reverse=True)
    kept = [h for h in sims if h["sim"] >= floor]

    # ── Decisive winner ──────────────────────────────────────────────────────
    # A single absolute floor asks one number to mean the same thing for every
    # query, and it does not. Measured: the article that ANSWERS a vaguer or
    # non-Latin question often lands just under 0.33 while still leading its
    # runner-up clearly — "你们的产品多少钱？" put the pricing article top at
    # 0.281 against 0.247. Meanwhile a genuine miss produces a FLAT spread:
    # the two noise cases measured 0.296 vs 0.295 and 0.310 vs 0.308.
    #
    # So the shape of the distribution separates a real answer from noise where
    # the absolute value cannot. When nothing clears the floor, accept the top
    # hit only if it is BOTH above a hard noise floor and clearly ahead of the
    # next one. This matters most for Chinese, which contributes no FTS terms
    # at all, so the vector leg is the only signal it has.
    if not kept and sims and sims[0]["sim"] >= DECISIVE_MIN:
        best = sims[0]["sim"]
        second = sims[1]["sim"] if len(sims) > 1 else 0.0
        if best > 0 and (best - second) / best >= DECISIVE_MARGIN:
            logger.debug(f"[semantic] decisive winner {best:.3f} over "
                         f"{second:.3f} (floor {floor})")
            kept = [sims[0]]
    return kept[:limit]


# ============================================================================
# Admin endpoints (mounted with _ADMIN in main.py)
# ============================================================================

router = APIRouter(tags=["semantic"])


@router.get("/kb/semantic-status")
def semantic_status():
    return {"enabled": ENABLED, "model": EMBED_MODEL, "min_sim": MIN_SIM,
            "indexed": len(_VECTORS),
            "query_cache": len(_QCACHE), "query_cache_max": _QCACHE_MAX,
            "last_refresh_age_s": (int(time.time() - _LAST_REFRESH)
                                   if _LAST_REFRESH else None)}


@router.post("/kb/semantic-reindex")
def semantic_reindex():
    return ensure_index(force=True)


@router.get("/kb/semantic-search")
def semantic_search_api(q: str, limit: int = 4):
    return {"query": q, "hits": search(q, limit=limit)}
