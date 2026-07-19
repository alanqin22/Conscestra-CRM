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
    global _LAST_REFRESH
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
            cur.execute("SELECT article_uuid::text, content_hash, embedding "
                        "FROM kb_embeddings")
            stored = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            stale = [u for u in stored if u not in active]
            if stale:
                cur.execute("DELETE FROM kb_embeddings "
                            "WHERE article_uuid = ANY(%s::uuid[])", (stale,))

            todo = [u for u, a in active.items()
                    if stored.get(u, ("",))[0] != _content_hash(a)]
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
                        stored[u] = (_content_hash(active[u]), v)
                    embedded = len(batch)
        conn.commit()

        _VECTORS.clear()
        for u, (_h, emb) in stored.items():
            if u not in active:
                continue
            vec = emb if isinstance(emb, list) else json.loads(emb)
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            _VECTORS[u] = (vec, norm)
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
    qv = _embed([query.strip()[:2000]])
    if not qv:
        return []
    q = qv[0]
    qnorm = math.sqrt(sum(x * x for x in q)) or 1.0
    floor = MIN_SIM if min_sim is None else min_sim
    sims = []
    for u, (vec, norm) in _VECTORS.items():
        dot = sum(a * b for a, b in zip(q, vec))
        sim = dot / (qnorm * norm)
        if sim >= floor:
            sims.append({"article_uuid": u, "sim": round(sim, 4)})
    sims.sort(key=lambda h: h["sim"], reverse=True)
    return sims[:limit]


# ============================================================================
# Admin endpoints (mounted with _ADMIN in main.py)
# ============================================================================

router = APIRouter(tags=["semantic"])


@router.get("/kb/semantic-status")
def semantic_status():
    return {"enabled": ENABLED, "model": EMBED_MODEL, "min_sim": MIN_SIM,
            "indexed": len(_VECTORS),
            "last_refresh_age_s": (int(time.time() - _LAST_REFRESH)
                                   if _LAST_REFRESH else None)}


@router.post("/kb/semantic-reindex")
def semantic_reindex():
    return ensure_index(force=True)


@router.get("/kb/semantic-search")
def semantic_search_api(q: str, limit: int = 4):
    return {"query": q, "hits": search(q, limit=limit)}
