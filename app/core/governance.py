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

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
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
        # Independent critique FIRST (best-effort) so the routed notification
        # and email carry the second opinion alongside the proposal.
        critique = None
        try:
            from app.core import critic
            critique = critic.review(aid, action_type, params or {},
                                     entity_type, entity_id)
        except Exception as exc:
            logger.warning(f"[governance] critique skipped for {aid[:8]}: {exc}")
        # Route to the right decision-maker (best-effort: tolerates the
        # governance_routing migration not being applied yet).
        try:
            route_approval(aid, action_type, params or {}, critique=critique)
        except Exception as exc:
            logger.warning(f"[governance] routing skipped for {aid[:8]}: {exc}")
        return aid
    finally:
        conn.close()


# ============================================================================
# Executive routing — who decides this approval?
# ============================================================================

GOV_ROUTE_EMAIL = _flag("GOV_ROUTE_EMAIL")   # 1 = email the assigned executive

_AMOUNT_KEYS = ("amount", "total_amount", "total", "value", "balance",
                "computed_balance_due", "ltv")

# action_type keyword → executive role that owns the call
_ROLE_AFFINITY = (
    (("invoice", "payment", "dunning", "refund", "credit", "write_off", "ar"), "CFO"),
    (("opportunity", "deal", "discount", "campaign", "winback", "lead", "email"), "CRO"),
    (("order", "inventory", "shipment", "stock", "activity"), "COO"),
)


def _amount_from(params: Dict[str, Any]) -> float:
    for k in _AMOUNT_KEYS:
        v = (params or {}).get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _affinity_role(action_type: str) -> str:
    at = (action_type or "").lower()
    for keywords, role in _ROLE_AFFINITY:
        if any(k in at for k in keywords):
            return role
    return "CEO"


_STANCE_ICON = {"endorse": "✅", "caution": "⚠️", "object": "⛔"}


def _critic_line(critique: Optional[Dict[str, Any]]) -> str:
    """One human line summarizing the critic's verdict ('' when absent)."""
    if not critique:
        return ""
    icon = _STANCE_ICON.get(critique.get("stance", ""), "•")
    return (f"{icon} Critic ({critique.get('stance', '?').upper()}): "
            f"{critique.get('summary', '')}")


def route_approval(approval_uuid: str, action_type: str,
                   params: Dict[str, Any],
                   critique: Optional[Dict[str, Any]] = None
                   ) -> Optional[Dict[str, Any]]:
    """Assign the approval to an executive: prefer the role that owns this kind
    of action, then the SMALLEST sufficient approval_authority_limit (delegate
    down, escalate only when the amount demands it; NULL limit = unlimited).
    Records the assignment, emits an approval.routed audit event, notifies the
    executive (in_app when they map to an employee; email when GOV_ROUTE_EMAIL).
    When a critic critique is supplied, it rides along in the notification,
    the audit event, and the one-click decision email."""
    amount = _amount_from(params)
    role = _affinity_role(action_type)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT executive_id::text, role_code, full_name, email,
                          approval_authority_limit, auto_email_enabled,
                          employee_uuid::text
                   FROM executives WHERE is_active""")
            execs = [dict(zip([d[0] for d in cur.description], r))
                     for r in cur.fetchall()]
            if not execs:
                return None

            def limit_of(e):
                lim = e["approval_authority_limit"]
                return float(lim) if lim is not None else float("inf")

            candidates = [e for e in execs if limit_of(e) >= amount] or \
                [max(execs, key=limit_of)]   # nobody sufficient → highest authority
            # role match first; then smallest sufficient limit; then stable order
            chosen = sorted(candidates,
                            key=lambda e: (e["role_code"] != role,
                                           limit_of(e), e["role_code"]))[0]
            label = f"{chosen['role_code']} {chosen['full_name']}"

            cur.execute(
                """UPDATE action_approvals
                   SET amount=%s, assigned_executive_id=%s::uuid, assigned_to=%s
                   WHERE approval_uuid=%s::uuid""",
                (amount, chosen["executive_id"], label, approval_uuid))

            cur.execute(
                "SELECT emit_event(%s,%s,%s,%s,%s,%s)",
                ("approval.routed", "approval", approval_uuid,
                 json.dumps({"context": {"action_type": action_type,
                                         "amount": amount, "assigned_to": label,
                                         "affinity_role": role,
                                         "critic_stance": (critique or {}).get("stance")}}),
                 None, "governance"))
            event_uuid = cur.fetchone()[0]

            if chosen.get("employee_uuid"):
                critic_note = _critic_line(critique)
                cur.execute(
                    """INSERT INTO notifications
                         (employee_uuid, event_uuid, channel, status, title, body, metadata)
                       VALUES (%s::uuid, %s, 'in_app', 'pending', %s, %s, %s)""",
                    (chosen["employee_uuid"], event_uuid,
                     f"🛡️ Approval needed: {action_type}" +
                     (f" (${amount:,.0f})" if amount else ""),
                     f"Proposed action awaits your decision. Review in the "
                     f"governance queue (approval {approval_uuid[:8]})."
                     + (f"\n{critic_note}" if critic_note else ""),
                     json.dumps({"kind": "approval_routed",
                                 "approval_uuid": approval_uuid,
                                 "critic_stance": (critique or {}).get("stance")})))
        conn.commit()
    finally:
        conn.close()

    if GOV_ROUTE_EMAIL and chosen.get("auto_email_enabled") and chosen.get("email"):
        try:
            from app.agents.email.smtp_imap import send_email
            links = decision_links(approval_uuid)
            # Critic block — the independent second opinion, right above the
            # decision buttons where it matters.
            findings = [f for f in (critique or {}).get("findings", [])
                        if f.get("verdict") in ("fail", "warn")][:4]
            critic_html = ""
            critic_text = ""
            if critique:
                col = {"endorse": "#1e7c45", "caution": "#a8720a",
                       "object": "#a33a3a"}.get(critique.get("stance"), "#26304a")
                items = "".join(f'<li style="margin:2px 0;">{f["note"]}</li>'
                                for f in findings)
                critic_html = (
                    f'<div style="border-left:4px solid {col};background:#f6f8fb;'
                    f'padding:10px 14px;margin:14px 0;font-size:13px;">'
                    f'<b style="color:{col};">{_critic_line(critique)}</b>'
                    + (f'<ul style="margin:6px 0 0 18px;padding:0;">{items}</ul>'
                       if items else "")
                    + '</div>')
                critic_text = ("\n" + _critic_line(critique) + "\n"
                               + "".join(f"  - {f['note']}\n" for f in findings))
            send_email(
                to=chosen["email"],
                subject=f"Approval needed: {action_type}"
                        + (f" (${amount:,.0f})" if amount else ""),
                body_html=(f"<p>A proposed action requires your decision "
                           f"(within your authority as {label}).</p>"
                           f"<p><b>{action_type}</b>"
                           + (f" — ${amount:,.0f}" if amount else "")
                           + f"<br>Approval ID: {approval_uuid}</p>"
                           + critic_html +
                           f'<p style="margin:18px 0;">'
                           f'<a href="{links["approve"]}" style="background:#1e7c45;'
                           f'color:#fff;padding:10px 22px;border-radius:6px;'
                           f'text-decoration:none;font-weight:700;">✓ Approve</a>'
                           f'&nbsp;&nbsp;'
                           f'<a href="{links["reject"]}" style="background:#a33a3a;'
                           f'color:#fff;padding:10px 22px;border-radius:6px;'
                           f'text-decoration:none;font-weight:700;">✕ Reject</a></p>'
                           f'<p style="color:#7b8497;font-size:12px;">One-click, '
                           f'signed links — no sign-in needed. Or review the full '
                           f'context in the governance queue.</p>'),
                body_text=(f"Approval needed: {action_type}"
                           + (f" (${amount:,.0f})" if amount else "")
                           + f"\nApproval ID: {approval_uuid}\n"
                           f"Assigned to: {label}\n"
                           + critic_text +
                           f"\nApprove: {links['approve']}\n"
                           f"Reject:  {links['reject']}\n"),
            )   # internal/administrative — transactional, not commercial
        except Exception as exc:
            logger.warning(f"[governance] route email failed: {exc}")

    logger.info(f"[governance] routed {approval_uuid[:8]} → {label} "
                f"(amount=${amount:,.0f}, affinity={role})")
    return {"assigned_to": label, "amount": amount,
            "executive_id": chosen["executive_id"]}


def _row(approval_uuid: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT approval_uuid, action_type, proposed_by, entity_type,
                          entity_id, params, confidence, severity, status,
                          created_at, expires_at, result, executed_at
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
            try:
                cur.execute(
                    """SELECT approval_uuid, action_type, proposed_by, entity_type,
                              entity_id, params, confidence, severity, created_at,
                              expires_at, amount, assigned_to, critique
                       FROM action_approvals
                       WHERE status='pending' AND (expires_at IS NULL OR expires_at>now())
                       ORDER BY created_at""")
            except Exception:
                conn.rollback()   # routing/critic migrations not applied yet
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
    out = {"ok": res.ok, "output": res.output, "error": res.error}
    # Persist the structured result when it serializes — undo() needs it to
    # know WHAT was created (e.g. the campaign_uuid to cancel).
    try:
        json.dumps(res.data)
        out["data"] = res.data
    except (TypeError, ValueError):
        pass
    return out


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
# Stale-approval expiry — pending items don't linger as silent liabilities
# ============================================================================

def expire_stale() -> Dict[str, Any]:
    """Flip pending approvals past their expires_at to 'expired' (audited).
    pending() already filters them out of the live queue; this makes the state
    honest in the table too. Scheduled nightly + POST /governance/expire."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE action_approvals
                   SET status='expired', decided_by='system',
                       decided_at=now(),
                       decision_reason='expired unactioned (TTL passed)'
                   WHERE status='pending' AND expires_at IS NOT NULL
                     AND expires_at < now()
                   RETURNING approval_uuid::text, action_type""")
            rows = cur.fetchall()
        conn.commit()
    finally:
        conn.close()
    if rows:
        logger.info(f"[governance] expired {len(rows)} stale approval(s): "
                    f"{[r[1] for r in rows]}")
    return {"expired": len(rows),
            "items": [{"approval_uuid": r[0], "action_type": r[1]} for r in rows]}


# ============================================================================
# Reversibility — undo an EXECUTED action (per-action-type handlers)
# ============================================================================

UNDO_WINDOW_HOURS = int(os.getenv("GOV_UNDO_WINDOW_HOURS", "72"))


def _undo_campaign_winback(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import marketing
    cid = (((ap.get("result") or {}).get("data") or {}) or {}).get("campaign_uuid")
    if not cid:
        return {"ok": False, "error": "no campaign_uuid recorded on the approval"}
    return marketing.cancel_campaign(
        cid, f"undone via governance approval {ap['approval_uuid'][:8]}")


def _undo_tuning_adjust(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import tuning
    param = (ap.get("params") or {}).get("param")
    if not param:
        return {"ok": False, "error": "no param recorded on the approval"}
    return tuning.revert(param)


# action_type → handler(approval_row) -> {'ok': bool, ...}. An action is
# reversible only if a handler exists AND the handler can still unwind it —
# handlers must report what could NOT be reversed (e.g. real emails sent).
_UNDO = {
    "campaign.winback": _undo_campaign_winback,
    "tuning.adjust": _undo_tuning_adjust,
}


async def undo(approval_uuid: str, decided_by: str = "human",
               reason: Optional[str] = None) -> Dict[str, Any]:
    """Reverse an executed action within GOV_UNDO_WINDOW_HOURS. Status becomes
    'undone'; the undo outcome is merged into the audit row's result."""
    ap = _row(approval_uuid)
    if not ap:
        return {"ok": False, "error": "not found"}
    if ap["status"] != "executed":
        return {"ok": False, "error": f"only executed actions can be undone "
                                      f"(status={ap['status']})"}
    handler = _UNDO.get(ap["action_type"])
    if not handler:
        return {"ok": False, "error": f"'{ap['action_type']}' is not reversible "
                f"(no undo handler; e.g. sent email cannot be unsent)"}
    executed_at = ap.get("executed_at")
    if executed_at is not None:
        from datetime import datetime, timezone, timedelta
        if datetime.now(timezone.utc) - executed_at > timedelta(hours=UNDO_WINDOW_HOURS):
            return {"ok": False, "error": f"undo window closed "
                    f"({UNDO_WINDOW_HOURS}h after execution)"}
    try:
        res = await asyncio.to_thread(handler, ap)
    except Exception as exc:
        return {"ok": False, "error": f"undo handler failed: {exc}"}
    if res.get("ok"):
        _set(approval_uuid, "undone", decided_by,
             reason or "undone via governance",
             result={**(ap.get("result") or {}), "undo": res})
    return {"ok": bool(res.get("ok")), "status": "undone" if res.get("ok") else ap["status"],
            "approval_uuid": approval_uuid, "undo": res}


# ============================================================================
# One-click decisions — HMAC-signed approve/reject links (like unsubscribe)
# ============================================================================
# The routed-approval email carries per-action signed links, so the executive
# decides from their phone without a CRM session. The token binds
# (approval_uuid, action) to a server secret; approve()/reject() refuse
# non-pending rows, so links are single-use by construction.

def _link_secret() -> bytes:
    s = (os.getenv("GOV_LINK_SECRET") or os.getenv("UNSUBSCRIBE_SECRET")
         or os.getenv("ADMIN_API_TOKEN") or "")
    return s.encode("utf-8")


def decision_token(approval_uuid: str, action: str) -> str:
    import hashlib
    import hmac as _hmac
    return _hmac.new(_link_secret(), f"{approval_uuid}:{action}".encode("utf-8"),
                     hashlib.sha256).hexdigest()[:32]


def _verify_token(approval_uuid: str, action: str, token: str) -> bool:
    import hmac as _hmac
    if not _link_secret() or not token:
        return False
    return _hmac.compare_digest(decision_token(approval_uuid, action), token)


def decision_links(approval_uuid: str) -> Dict[str, str]:
    base = (os.getenv("APP_URL", "") or "http://localhost:8000").rstrip("/")
    return {a: (f"{base}/governance/decide?g={approval_uuid}"
                f"&a={a}&t={decision_token(approval_uuid, a)}")
            for a in ("approve", "reject")}


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


@router.post("/governance/undo/{approval_uuid}")
async def governance_undo(approval_uuid: str, body: _Decision = _Decision()):
    """Reverse an executed action (within GOV_UNDO_WINDOW_HOURS, if its
    action_type has an undo handler)."""
    return await undo(approval_uuid, body.decided_by, body.reason)


@router.post("/governance/expire")
def governance_expire():
    """Flip pending approvals past their TTL to 'expired' (also runs nightly)."""
    return expire_stale()


@router.post("/governance/critique/{approval_uuid}")
async def governance_critique_one(approval_uuid: str):
    """(Re)run the independent critic on one approval — e.g. after resolving a
    complaint, to refresh the verdict before deciding."""
    import asyncio as _aio
    from app.core import critic
    return await _aio.to_thread(critic.review, approval_uuid)


@router.post("/governance/critique")
async def governance_critique_backfill():
    """Critique every live pending approval that has none yet."""
    import asyncio as _aio
    from app.core import critic
    return await _aio.to_thread(critic.review_pending)


# ── Public one-click decision endpoint (token IS the auth) ──────────────────

public_router = APIRouter(tags=["governance-public"])

_DECIDE_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#eef1f6;margin:0;padding:40px 16px;">
<div style="max-width:460px;margin:0 auto;background:#fff;border:1px solid #e1e6ef;border-radius:8px;padding:28px;">
<div style="font-family:Georgia,serif;font-size:11px;letter-spacing:.22em;color:#b08a46;font-weight:700;text-transform:uppercase;">Conscestra CRM</div>
<h2 style="color:#15233f;margin:10px 0 6px;">{title}</h2>
<p style="color:#26304a;font-size:14px;line-height:1.55;">{body}</p>
</div></body></html>"""


@public_router.get("/governance/decide", response_class=HTMLResponse)
async def governance_decide_link(g: str = "", a: str = "", t: str = ""):
    """One-click approve/reject from the routed-approval email. The HMAC token
    binds (approval, action); non-pending rows refuse re-decisions, so a link
    can only ever be used once."""
    action = (a or "").strip().lower()
    if action not in ("approve", "reject") or not _verify_token(g, action, t):
        return HTMLResponse(_DECIDE_PAGE.format(
            title="Link not valid",
            body="This decision link is invalid or was tampered with. "
                 "Open the governance queue in the CRM instead."), status_code=403)
    ap = _row(g)
    if not ap:
        return HTMLResponse(_DECIDE_PAGE.format(
            title="Not found", body="This approval no longer exists."), status_code=404)
    if ap["status"] != "pending":
        return HTMLResponse(_DECIDE_PAGE.format(
            title="Already decided",
            body=f"This approval was already <b>{ap['status']}</b>. "
                 f"Nothing further happened."))
    if action == "approve":
        res = await approve(g, decided_by="email-link")
        ok = res.get("ok")
        return HTMLResponse(_DECIDE_PAGE.format(
            title="Approved ✓" if ok else "Approved — execution failed",
            body=(f"<b>{ap['action_type']}</b> was approved and "
                  f"{'executed' if ok else 'queued but FAILED to execute'}."
                  + (f"<br><br>{res.get('result', {}).get('error') or ''}"
                     if not ok else ""))))
    res = reject(g, decided_by="email-link")
    return HTMLResponse(_DECIDE_PAGE.format(
        title="Rejected ✓",
        body=f"<b>{ap['action_type']}</b> was rejected. No action was taken."))
