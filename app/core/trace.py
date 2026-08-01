"""Correlation-id trace — one id, the whole play (audit 2026-07-18 item #5).

The platform already threads a correlation id through every layer — A2A
envelopes, bus events, planner-tagged approvals, sequences — but nothing
stitched them back together. This is the read-side:

    GET /trace/{correlation_id}   every recorded step of one play, across
                                  a2a_dispatches + events + action_approvals
                                  + agent_sequences, time-ordered
    GET /trace-recent             the latest correlation ids with activity,
                                  as entry points

Sources are queried BEST-EFFORT and independently — a missing table just
contributes zero rows, so the trace works on a partially-migrated DB.
Read-only; admin-gated in main.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("trace")


def _rows(sql: str, args: tuple) -> List[tuple]:
    """One best-effort query on its own connection ([] on any failure)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    except Exception as exc:
        logger.debug(f"[trace] source skipped: {exc}")
        return []
    finally:
        conn.close()


def build(correlation_id: str) -> Dict[str, Any]:
    """Stitch every recorded step for one correlation id, oldest first."""
    cid = (correlation_id or "").strip()
    steps: List[Dict[str, Any]] = []

    for r in _rows(
            """SELECT at, from_agent, agent, intent, kind, ok, error, latency_ms
               FROM a2a_dispatches WHERE correlation_id=%s""", (cid,)):
        steps.append({
            "at": r[0].isoformat(), "source": "a2a",
            "label": f"{r[1] or '?'} → {r[2] or '?'}.{r[3]}",
            "detail": {"kind": r[4], "ok": r[5], "error": r[6],
                       "latency_ms": r[7]}})

    for r in _rows(
            """SELECT created_at, event_type, entity_type, entity_uuid::text,
                      source_system
               FROM events WHERE correlation_id::text=%s""", (cid,)):
        steps.append({
            "at": r[0].isoformat(), "source": "event",
            "label": f"{r[1]} ({r[2] or 'no entity'})",
            "detail": {"entity_uuid": r[3], "source_system": r[4]}})

    for r in _rows(
            """SELECT created_at, approval_uuid::text, action_type, proposed_by,
                      status, decided_by, decided_at, confidence
               FROM action_approvals
               WHERE params->>'_correlation_id'=%s
                  OR params->>'plan_correlation_id'=%s""", (cid, cid)):
        steps.append({
            "at": r[0].isoformat(), "source": "approval",
            "label": f"{r[2]} [{r[4]}]",
            "detail": {"approval_uuid": r[1], "proposed_by": r[3],
                       "decided_by": r[5],
                       "decided_at": r[6].isoformat() if r[6] else None,
                       "confidence": float(r[7]) if r[7] is not None else None}})

    for r in _rows(
            """SELECT created_at, playbook, entity_type, step_no, status, outcome
               FROM agent_sequences WHERE correlation_id=%s""", (cid,)):
        steps.append({
            "at": r[0].isoformat(), "source": "sequence",
            "label": f"{r[1]} ({r[2]}) step {r[3]} [{r[4]}]",
            "detail": {"outcome": r[5]}})

    # Retrieval grounding — WHICH stored memories were placed in an agent's
    # context during this play. Without it a bad reply is uninvestigable: the
    # index is mutable, so re-running the search later does not reconstruct what
    # the model actually saw.
    try:
        from app.core import grounding
        for g in grounding.for_correlation(cid):
            steps.append({
                "at": g["at"], "source": "memory",
                "label": (f"retrieved {g['result_count']} record(s) "
                          f"[{g['audience']}] for "
                          f"{g.get('entity_type') or '?'} — {(g.get('query') or '')[:60]}"),
                "detail": {"audience": g["audience"], "sources": g["sources"],
                           "entity_id": g.get("entity_id")}})
    except Exception as exc:
        logger.debug(f"[trace] grounding section skipped: {exc}")

    steps.sort(key=lambda s: s["at"])
    return {"correlation_id": cid, "entries": len(steps), "trace": steps}


router = APIRouter(tags=["trace"])


@router.get("/trace-recent")
def trace_recent(limit: int = 20):
    """Latest correlation ids with recorded activity — trace entry points."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in _rows(
            """SELECT correlation_id, max(at), count(*)
               FROM a2a_dispatches GROUP BY correlation_id
               ORDER BY max(at) DESC LIMIT %s""", (int(limit),)):
        out[r[0]] = {"correlation_id": r[0], "last_at": r[1].isoformat(),
                     "a2a_steps": r[2], "events": 0}
    for r in _rows(
            """SELECT correlation_id::text, max(created_at), count(*)
               FROM events WHERE correlation_id IS NOT NULL
               GROUP BY correlation_id
               ORDER BY max(created_at) DESC LIMIT %s""", (int(limit),)):
        cur = out.setdefault(r[0], {"correlation_id": r[0],
                                    "last_at": r[1].isoformat(),
                                    "a2a_steps": 0, "events": 0})
        cur["events"] = r[2]
        cur["last_at"] = max(cur["last_at"], r[1].isoformat())
    rows = sorted(out.values(), key=lambda x: x["last_at"], reverse=True)
    return {"count": len(rows[:limit]), "recent": rows[:limit]}


@router.get("/trace/{correlation_id}")
def trace_get(correlation_id: str):
    return build(correlation_id)
