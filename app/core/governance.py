"""Phase 5 — Governance: confidence-gating + approval queue.

Makes the autonomy safe to turn on. Every WRITE/outbound A2A action is gated by
its confidence:

    confidence >= GOV_ACT_MIN      → ACT      (execute now)
    GOV_PROPOSE_MIN <= c < ACT_MIN → PROPOSE  (queue for human approval)
    c < GOV_PROPOSE_MIN            → SKIP     (don't act)

Proposed actions land in `action_approvals` (pending). A human approves/rejects
via the endpoints; approving re-dispatches the action through A2A (gate bypassed)
and records the result. Every proposal and decision is audited in the table.

Reads are never gated. Gating only engages when GOV_ENABLED=1 — otherwise writes
execute exactly as before (additive, opt-in).

CONFIG (env)
  GOV_ENABLED       0     master on/off (gating no-ops when 0)
  GOV_ACT_MIN       0.8   confidence at/above which a write auto-executes
  GOV_PROPOSE_MIN   0.5   confidence at/above which a write is queued (else skip)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.database import get_connection

logger = logging.getLogger("governance")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


ENABLED = _flag("GOV_ENABLED")
ACT_MIN = _float("GOV_ACT_MIN", 0.8)
PROPOSE_MIN = _float("GOV_PROPOSE_MIN", 0.5)


# ============================================================================
# Policy
# ============================================================================

def decide(confidence: float) -> str:
    """'act' | 'propose' | 'skip' from a confidence score."""
    c = float(confidence or 0)
    if c >= ACT_MIN:
        return "act"
    if c >= PROPOSE_MIN:
        return "propose"
    return "skip"


# ============================================================================
# Queue
# ============================================================================

def propose(action_type: str, proposed_by: str, params: Dict[str, Any],
            entity_type: Optional[str] = None, entity_id: Optional[str] = None,
            confidence: float = 0.0, severity: Optional[str] = None,
            ttl_hours: Optional[int] = 72) -> str:
    """Enqueue a pending action for human approval. Returns approval_uuid."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO action_approvals
                     (action_type, proposed_by, entity_type, entity_id, params,
                      confidence, severity, expires_at)
                   VALUES (%(at)s,%(by)s,%(et)s,%(eid)s,%(p)s::jsonb,%(cf)s,%(sev)s,
                           CASE WHEN %(ttl)s IS NULL THEN NULL
                                ELSE now() + (%(ttl)s||' hours')::interval END)
                   RETURNING approval_uuid""",
                {"at": action_type, "by": proposed_by, "et": entity_type,
                 "eid": str(entity_id) if entity_id else None,
                 "p": json.dumps(params or {}), "cf": confidence, "sev": severity,
                 "ttl": ttl_hours})
            aid = str(cur.fetchone()[0])
        conn.commit()
        logger.info(f"[governance] proposed {action_type} by {proposed_by} "
                    f"(conf={confidence}) → {aid[:8]}")
        return aid
    finally:
        conn.close()


def _row(approval_uuid: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT approval_uuid, action_type, proposed_by, entity_type,
                          entity_id, params, confidence, severity, status,
                          created_at, expires_at
                   FROM action_approvals WHERE approval_uuid=%s::uuid""",
                (approval_uuid,))
            r = cur.fetchone()
            if not r:
                return None
            d = dict(zip([c[0] for c in cur.description], r))
            d["approval_uuid"] = str(d["approval_uuid"])
            d["entity_id"] = str(d["entity_id"]) if d["entity_id"] else None
            d["confidence"] = float(d["confidence"]) if d["confidence"] is not None else None
            for k in ("created_at", "expires_at"):
                d[k] = d[k].isoformat() if d[k] else None
            return d
    finally:
        conn.close()


def pending() -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT approval_uuid, action_type, proposed_by, entity_type,
                          entity_id, params, confidence, severity, created_at,
                          expires_at
                   FROM action_approvals
                   WHERE status='pending' AND (expires_at IS NULL OR expires_at>now())
                   ORDER BY created_at""")
            cols = [c[0] for c in cur.description]
            out = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["approval_uuid"] = str(d["approval_uuid"])
                d["entity_id"] = str(d["entity_id"]) if d["entity_id"] else None
                d["confidence"] = float(d["confidence"]) if d["confidence"] is not None else None
                for k in ("created_at", "expires_at"):
                    d[k] = d[k].isoformat() if d[k] else None
                out.append(d)
            return out
    finally:
        conn.close()


def history(limit: int = 30) -> List[Dict[str, Any]]:
    """Recent decided/expired proposals — the audit trail for the admin UI."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT approval_uuid, action_type, proposed_by, entity_type,
                          entity_id, confidence, severity, status, decided_by,
                          decided_at, decision_reason, result, created_at
                   FROM action_approvals
                   WHERE status <> 'pending'
                   ORDER BY COALESCE(decided_at, created_at) DESC
                   LIMIT %s""", (int(limit),))
            cols = [c[0] for c in cur.description]
            out = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["approval_uuid"] = str(d["approval_uuid"])
                d["entity_id"] = str(d["entity_id"]) if d["entity_id"] else None
                d["confidence"] = float(d["confidence"]) if d["confidence"] is not None else None
                for k in ("created_at", "decided_at"):
                    d[k] = d[k].isoformat() if d[k] else None
                out.append(d)
            return out
    finally:
        conn.close()


def _set(approval_uuid: str, status: str, decided_by: Optional[str] = None,
         reason: Optional[str] = None, result: Optional[Dict] = None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE action_approvals
                   SET status=%(s)s,
                       decided_by=COALESCE(%(by)s, decided_by),
                       decided_at=CASE WHEN %(by)s IS NOT NULL THEN now() ELSE decided_at END,
                       decision_reason=COALESCE(%(r)s, decision_reason),
                       result=COALESCE(%(res)s::jsonb, result),
                       executed_at=CASE WHEN %(s)s IN ('executed','failed') THEN now() ELSE executed_at END
                   WHERE approval_uuid=%(id)s::uuid""",
                {"s": status, "by": decided_by, "r": reason,
                 "res": json.dumps(result) if result is not None else None,
                 "id": approval_uuid})
        conn.commit()
    finally:
        conn.close()


async def _execute(ap: Dict[str, Any]) -> Dict[str, Any]:
    """Run an approved action by re-dispatching it through A2A (gate bypassed)."""
    from app.core.a2a import A2ARequest, EntityRef, dispatch
    req = A2ARequest(
        intent=ap["action_type"], from_agent="governance", params=ap.get("params") or {},
        entity=EntityRef(ap["entity_type"], ap["entity_id"]) if ap.get("entity_type") else None,
        confidence=1.0, govern_bypass=True)
    res = await dispatch(req)
    return {"ok": res.ok, "output": res.output, "error": res.error}


async def approve(approval_uuid: str, decided_by: str = "human",
                  reason: Optional[str] = None) -> Dict[str, Any]:
    ap = _row(approval_uuid)
    if not ap:
        return {"ok": False, "error": "not found"}
    if ap["status"] != "pending":
        return {"ok": False, "error": f"not pending (status={ap['status']})"}
    _set(approval_uuid, "approved", decided_by, reason)
    res = await _execute(ap)
    _set(approval_uuid, "executed" if res["ok"] else "failed", result=res)
    return {"ok": res["ok"], "status": "executed" if res["ok"] else "failed",
            "approval_uuid": approval_uuid, "result": res}


def reject(approval_uuid: str, decided_by: str = "human",
           reason: Optional[str] = None) -> Dict[str, Any]:
    ap = _row(approval_uuid)
    if not ap:
        return {"ok": False, "error": "not found"}
    if ap["status"] != "pending":
        return {"ok": False, "error": f"not pending (status={ap['status']})"}
    _set(approval_uuid, "rejected", decided_by, reason)
    return {"ok": True, "status": "rejected", "approval_uuid": approval_uuid}


# ============================================================================
# Endpoints
# ============================================================================

router = APIRouter(tags=["governance"])


@router.get("/governance/status")
def governance_status():
    return {"enabled": ENABLED, "act_min": ACT_MIN, "propose_min": PROPOSE_MIN,
            "pending": len(pending())}


@router.get("/governance/queue")
def governance_queue():
    return {"pending": pending()}


@router.get("/governance/history")
def governance_history(limit: int = 30):
    return {"history": history(limit)}


class _DeleteBody(BaseModel):
    ids: List[str]


@router.post("/governance/history/delete")
def governance_history_delete(body: _DeleteBody):
    """Delete decided audit rows (executed/failed/rejected/expired). Pending
    actions can never be deleted — they must be approved or rejected first."""
    ids = [i for i in (body.ids or []) if i]
    if not ids:
        return {"deleted": 0}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM action_approvals "
                "WHERE approval_uuid = ANY(%s::uuid[]) AND status <> 'pending' "
                "RETURNING approval_uuid", (ids,))
            n = len(cur.fetchall())
        conn.commit()
        logger.info(f"[governance] deleted {n} decided history row(s)")
        return {"deleted": n, "requested": len(ids)}
    finally:
        conn.close()


class _Decision(BaseModel):
    decided_by: str = "human"
    reason: Optional[str] = None


@router.post("/governance/approve/{approval_uuid}")
async def governance_approve(approval_uuid: str, body: _Decision = _Decision()):
    return await approve(approval_uuid, body.decided_by, body.reason)


@router.post("/governance/reject/{approval_uuid}")
def governance_reject(approval_uuid: str, body: _Decision = _Decision()):
    return reject(approval_uuid, body.decided_by, body.reason)
