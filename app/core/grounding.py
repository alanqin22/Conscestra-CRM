"""Retrieval grounding — what was in the model's context when it answered.

Once retrieved customer text reaches agent prompts, a bad reply has to be
answerable: "the AI said we promised a refund" needs an answer better than
re-running the search and hoping the corpus has not moved since. The index is
mutable — records are added, reclassified and erased — so a retrieval is NOT
reproducible after the fact. It has to be recorded when it happens.

    search() ──▶ record(...) ──▶ memory_retrievals ──▶ GET /trace/{cid}

CORRELATION ID comes from a context variable rather than a parameter. The read
path crosses `context.hydrate` → `customer_memory.recall_relevant` →
`content_index.search`, none of which take a correlation id, and threading one
through every signature would put audit plumbing in three modules that have no
other reason to know about it. `a2a` already mints the id per play; it sets the
var and clears it in a finally, matching the existing tenancy/write_guard
pattern.

WHAT IS NOT STORED: snippet text. It already lives in content_embeddings, which
is registered in lifecycle.DERIVED_PII_STORES and erased with the customer.
Copying it here would be a THIRD copy of personal data and a third thing for
erasure to forget — the exact bug that shipped in the index. Pointers to erased
rows are harmless; copies are not.

Recording NEVER fails a retrieval. An audit trail that can break the feature it
audits gets disabled, and then there is no audit trail.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("grounding")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("MEMORY_GROUNDING_LOG", "1")
MAX_RESULTS = int(os.getenv("MEMORY_GROUNDING_MAX_RESULTS", "20"))

# Request/play-scoped correlation id. Default None — an unset id records the
# retrieval anyway (still auditable by entity + time), it just cannot be
# stitched into a trace.
_correlation: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "grounding_correlation_id", default=None)


def set_correlation_id(cid: Optional[str]):
    """Bind a correlation id for this context. Returns the token — callers MUST
    reset it in a finally, or a worker thread inherits the previous play's id
    and the trace silently attributes one play's grounding to another."""
    return _correlation.set(cid)


def reset_correlation_id(token) -> None:
    try:
        _correlation.reset(token)
    except (ValueError, LookupError):
        pass


def correlation_id() -> Optional[str]:
    return _correlation.get()


def record(query: str, audience: str, results: List[Dict[str, Any]],
           entity_type: Optional[str] = None,
           entity_id: Optional[str] = None) -> None:
    """Record one retrieval. Best-effort and silent on failure by design."""
    if not ENABLED:
        return
    try:
        from app.core.database import get_connection
        payload = [{"source_type": r.get("source_type"),
                    "source_id": r.get("source_id"),
                    "similarity": r.get("similarity")}
                   for r in (results or [])[:MAX_RESULTS]]
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO memory_retrievals
                         (correlation_id, entity_type, entity_id, audience,
                          query, results, result_count)
                       VALUES (%s,%s,%s::uuid,%s,%s,%s::jsonb,%s)""",
                    (correlation_id(), entity_type, entity_id, audience,
                     (query or "")[:500], json.dumps(payload), len(results or [])))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[grounding] retrieval not recorded: {exc}")


def for_correlation(cid: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Grounding steps for one play — consumed by trace.build()."""
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT created_at, entity_type, entity_id::text, audience,
                              query, results, result_count
                         FROM memory_retrievals
                        WHERE correlation_id=%s
                        ORDER BY created_at LIMIT %s""", (cid, int(limit)))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[grounding] trace read skipped: {exc}")
        return []
    return [{"at": r[0].isoformat(), "kind": "memory_retrieval",
             "entity_type": r[1], "entity_id": r[2], "audience": r[3],
             "query": r[4], "sources": r[5], "result_count": r[6]}
            for r in rows]


def for_entity(entity_type: str, entity_id: str,
               limit: int = 25) -> List[Dict[str, Any]]:
    """Recent retrievals about one customer — 'what have we been reading about
    this person, and under which audience?'"""
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT created_at, correlation_id, audience, query,
                              result_count, results
                         FROM memory_retrievals
                        WHERE entity_type=%s AND entity_id=%s::uuid
                        ORDER BY created_at DESC LIMIT %s""",
                    (entity_type, entity_id, int(limit)))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[grounding] entity read skipped: {exc}")
        return []
    return [{"at": r[0].isoformat(), "correlation_id": r[1], "audience": r[2],
             "query": r[3], "result_count": r[4], "sources": r[5]} for r in rows]
