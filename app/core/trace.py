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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("trace")


def _rows(sql: str, args: tuple, _status: Optional[Dict[str, Any]] = None,
          _name: str = "") -> List[tuple]:
    """One best-effort query on its own connection ([] on any failure).

    When `_status` is supplied, the outcome is RECORDED there. That is the
    whole of G-08: this function returns [] both when a source genuinely has no
    rows for this play and when the source could not be read at all, and the
    caller could not tell those apart. An incident review then reads a
    four-step trace and concludes the play was simple, when in truth one source
    was unreachable and contributed nothing, silently — the same
    "absence of evidence read as evidence of absence" this codebase kills
    everywhere else.

    Still best-effort by construction: a trace that fails because one of its
    sources is missing is a trace nobody can use during an incident.
    """
    # get_connection() IS INSIDE THE TRY, and that is a fix rather than a
    # style choice. It used to sit above it, so this function tolerated a
    # failing QUERY (a missing table) but not a failing CONNECTION — the
    # module docstring promised best-effort and the code delivered it for only
    # one of the two ways a source can be unavailable. A database blip took
    # the whole trace down at precisely the moment someone needed it.
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        if _status is not None:
            _status[_name] = {"ok": True, "rows": len(rows)}
        return rows
    except Exception as exc:
        logger.debug(f"[trace] source skipped: {exc}")
        if _status is not None:
            _status[_name] = {"ok": False, "rows": 0, "error": str(exc)[:160]}
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:               # a broken connection cannot leak
                pass


def build(correlation_id: str) -> Dict[str, Any]:
    """Stitch every recorded step for one correlation id, oldest first.

    The result carries `sources`: per-source {ok, rows}. A trace that cannot
    say which of its inputs it actually read is a trace that cannot be trusted
    during the incident it exists for.
    """
    cid = (correlation_id or "").strip()
    steps: List[Dict[str, Any]] = []
    sources: Dict[str, Any] = {}

    for r in _rows(
            """SELECT at, from_agent, agent, intent, kind, ok, error, latency_ms,
                      outcome, principal
               FROM a2a_dispatches WHERE correlation_id=%s""", (cid,),
            sources, "a2a_dispatches"):
        steps.append({
            "at": r[0].isoformat(), "source": "a2a",
            "label": f"{r[1] or '?'} → {r[2] or '?'}.{r[3]}",
            # `outcome` is the authoritative result; `ok` answers a narrower
            # question and is kept so existing readers do not break. A consumer
            # deciding whether to RETRY must read outcome — `rejected` mixes
            # retryable and non-retryable causes and `failed` does not.
            "detail": {"kind": r[4], "ok": r[5],
                       "outcome": (r[8] if len(r) > 8 else None),
                       "principal": (r[9] if len(r) > 9 else None),
                       "error": r[6],
                       "latency_ms": r[7]}})

    for r in _rows(
            """SELECT created_at, event_type, entity_type, entity_uuid::text,
                      source_system
               FROM events WHERE correlation_id::text=%s""", (cid,),
            sources, "events"):
        steps.append({
            "at": r[0].isoformat(), "source": "event",
            "label": f"{r[1]} ({r[2] or 'no entity'})",
            "detail": {"entity_uuid": r[3], "source_system": r[4]}})

    for r in _rows(
            """SELECT created_at, approval_uuid::text, action_type, proposed_by,
                      status, decided_by, decided_at, confidence
               FROM action_approvals
               WHERE params->>'_correlation_id'=%s
                  OR params->>'plan_correlation_id'=%s""", (cid, cid),
            sources, "action_approvals"):
        steps.append({
            "at": r[0].isoformat(), "source": "approval",
            "label": f"{r[2]} [{r[4]}]",
            "detail": {"approval_uuid": r[1], "proposed_by": r[3],
                       "decided_by": r[5],
                       "decided_at": r[6].isoformat() if r[6] else None,
                       "confidence": float(r[7]) if r[7] is not None else None}})

    for r in _rows(
            """SELECT created_at, playbook, entity_type, step_no, status, outcome
               FROM agent_sequences WHERE correlation_id=%s""", (cid,),
            sources, "agent_sequences"):
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
        found = grounding.for_correlation(cid)
        for g in found:
            steps.append({
                "at": g["at"], "source": "memory",
                "label": (f"retrieved {g['result_count']} record(s) "
                          f"[{g['audience']}] for "
                          f"{g.get('entity_type') or '?'} — {(g.get('query') or '')[:60]}"),
                "detail": {"audience": g["audience"], "sources": g["sources"],
                           "entity_id": g.get("entity_id")}})
        sources["memory_retrievals"] = {"ok": True, "rows": len(found)}
    except Exception as exc:
        logger.debug(f"[trace] grounding section skipped: {exc}")
        sources["memory_retrievals"] = {"ok": False, "rows": 0,
                                        "error": str(exc)[:160]}

    steps.sort(key=lambda s: s["at"])
    unread = sorted(n for n, s in sources.items() if not s["ok"])
    return {"correlation_id": cid, "entries": len(steps), "trace": steps,
            # WHAT THIS TRACE COULD AND COULD NOT SEE. `complete` is the single
            # field a reader must check before concluding anything from the
            # absence of a step: a short trace and a partially-read trace look
            # identical without it.
            "sources": sources,
            "complete": not unread,
            "unread_sources": unread}


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
