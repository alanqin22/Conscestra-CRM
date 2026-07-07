"""Marketing agent v1 — segment → CASL-gated campaign → measure.

WHAT THIS IS
------------
The vision's Marketing Agent, built on the CASL infrastructure that already
exists (consent.py suppression list + HMAC unsubscribe footer + the
verified-address gate the dunning pilot uses):

    define SEGMENT ─▶ preview recipients ─▶ launch (drafts / real sends)
                                                   │
                                          measure: sent / suppressed /
                                          replies + orders since launch

A campaign is a SEGMENT over the customer base — accounts filtered by the
customer-intelligence profile (churn band, LTV, preferred channel) and/or
firmographics (industry) — with a personalized subject/body template.
Recipients are the segment accounts' CONTACTS.

SAFETY / COMPLIANCE (all enforced at launch, per recipient)
------------------------------------------------------------
  • DRAFT-ONLY unless AGENT_BUS_AUTOSEND=1 AND the launch passes confirm=true —
    two explicit gates before any real email.
  • Real sends go through send_email(commercial=True): CASL suppression check
    + sender-identification/unsubscribe footer appended.
  • Verified-address gate (_is_real_email): only OTP-verified, non-placeholder
    addresses are ever really mailed — seed/synthetic contacts get
    'skipped_unverified'.
  • One row per (campaign, email) — a re-launch can't double-send.

Campaign launches here are human-initiated admin actions (the human IS the
approver). An agent-initiated campaign should instead dispatch through A2A so
governance confidence-gating applies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("marketing")

_SEGMENT_KEYS = ("churn_band", "min_ltv", "industry", "preferred_channel")


# ============================================================================
# SEGMENTS
# ============================================================================

def _segment_where(criteria: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """criteria → (WHERE fragment, params). Unknown keys are ignored."""
    clauses, params = [], []
    bands = criteria.get("churn_band")
    if bands:
        bands = [bands] if isinstance(bands, str) else list(bands)
        clauses.append("i.churn_band = ANY(%s)")
        params.append(bands)
    if criteria.get("min_ltv") is not None:
        clauses.append("i.ltv >= %s")
        params.append(float(criteria["min_ltv"]))
    if criteria.get("industry"):
        clauses.append("a.industry ILIKE %s")
        params.append(f"%{criteria['industry']}%")
    if criteria.get("preferred_channel"):
        clauses.append("i.preferred_channel = %s")
        params.append(criteria["preferred_channel"])
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def resolve_segment(criteria: Dict[str, Any], limit: int = 500) -> List[Dict[str, Any]]:
    """Recipients: contacts (with an email) of active accounts matching the
    segment. Suppression/verification are enforced at SEND time, not here —
    the preview shows the audience, the launch shows what actually happens."""
    where, params = _segment_where(criteria or {})
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT DISTINCT ON (lower(c.email))
                           a.account_id::text, a.account_name,
                           c.contact_id::text, c.first_name, c.last_name,
                           lower(c.email) AS email,
                           COALESCE(c.is_email_verified, false) AS verified,
                           i.churn_band, i.ltv, i.preferred_channel
                    FROM accounts a
                    JOIN contacts c ON c.account_id = a.account_id
                    LEFT JOIN account_intelligence i ON i.account_id = a.account_id
                    WHERE COALESCE(a.is_deleted, false) = false
                      AND a.status = 'active'
                      AND COALESCE(c.is_deleted, false) = false
                      AND c.email IS NOT NULL AND c.email <> ''
                      {where}
                    ORDER BY lower(c.email), c.created_at
                    LIMIT %s""",
                params + [int(limit)],
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _personalize(template: str, r: Dict[str, Any]) -> str:
    out = template or ""
    for key, val in (("first_name", r.get("first_name") or "there"),
                     ("last_name", r.get("last_name") or ""),
                     ("account_name", r.get("account_name") or "your company"),
                     ("ltv", f"${float(r.get('ltv') or 0):,.0f}"),
                     ("churn_band", r.get("churn_band") or "")):
        out = out.replace("{{" + key + "}}", str(val))
    return out


# ============================================================================
# CAMPAIGNS
# ============================================================================

def create_campaign(name: str, subject: str, body_template: str,
                    segment: Dict[str, Any], created_by: str = "admin") -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO marketing_campaigns
                     (name, subject, body_template, segment, created_by)
                   VALUES (%s, %s, %s, %s::jsonb, %s)
                   RETURNING campaign_uuid::text""",
                (name, subject, body_template, json.dumps(segment or {}), created_by),
            )
            cid = cur.fetchone()[0]
        conn.commit()
        return {"status": "ok", "campaign_uuid": cid,
                "audience_preview": len(resolve_segment(segment))}
    finally:
        conn.close()


def _campaign(campaign_uuid: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT campaign_uuid::text, name, status, segment, subject,
                          body_template, channel, created_by, created_at,
                          launched_at, stats
                   FROM marketing_campaigns WHERE campaign_uuid=%s::uuid""",
                (campaign_uuid,),
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None
    finally:
        conn.close()


def launch_campaign(campaign_uuid: str, confirm: bool = False) -> Dict[str, Any]:
    """Resolve the segment and process every recipient through the CASL gates.
    Real email only when AGENT_BUS_AUTOSEND=1 AND confirm — otherwise every
    eligible recipient is recorded as 'drafted' (full dry-run with real gating)."""
    from app.core import agent_bus, consent

    camp = _campaign(campaign_uuid)
    if not camp:
        return {"status": "error", "reason": "campaign not found"}
    if camp["status"] != "draft":
        return {"status": "skipped", "reason": f"campaign is {camp['status']}"}

    live = bool(agent_bus.AUTOSEND and confirm)
    recipients = resolve_segment(camp["segment"] or {})
    counts = {"drafted": 0, "sent": 0, "suppressed": 0,
              "skipped_unverified": 0, "failed": 0}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for r in recipients:
                status, detail = "drafted", None
                if consent.is_suppressed(r["email"]):
                    status, detail = "suppressed", "CASL opt-out"
                elif live:
                    if not agent_bus._is_real_email(r["email"], r["verified"]):
                        status, detail = "skipped_unverified", "unverified/placeholder address"
                    else:
                        try:
                            from app.agents.email.smtp_imap import send_email
                            res = send_email(
                                to=r["email"],
                                subject=_personalize(camp["subject"], r),
                                body_html=_personalize(camp["body_template"], r)
                                    .replace("\n", "<br>"),
                                body_text=_personalize(camp["body_template"], r),
                                commercial=True,
                            )
                            if res.get("success"):
                                status = "sent"
                            elif res.get("skipped"):
                                status, detail = "suppressed", str(res.get("skipped"))
                            else:
                                status, detail = "failed", str(res.get("message"))[:200]
                        except Exception as exc:
                            status, detail = "failed", str(exc)[:200]
                cur.execute(
                    """INSERT INTO marketing_sends
                         (campaign_uuid, account_id, contact_id, email, status,
                          detail, sent_at)
                       VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s,
                               CASE WHEN %s = 'sent' THEN now() END)
                       ON CONFLICT (campaign_uuid, email) DO NOTHING""",
                    (campaign_uuid, r["account_id"], r["contact_id"], r["email"],
                     status, detail, status),
                )
                if cur.rowcount:
                    counts[status] += 1
            cur.execute(
                """UPDATE marketing_campaigns
                   SET status='launched', launched_at=now(), stats=%s::jsonb
                   WHERE campaign_uuid=%s::uuid""",
                (json.dumps({**counts, "live": live}), campaign_uuid),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"[marketing] launched {camp['name']!r} live={live} {counts}")
    return {"status": "ok", "live": live, "recipients": len(recipients), **counts}


def campaign_results(campaign_uuid: str) -> Dict[str, Any]:
    """Sends by status + engagement since launch (inbound replies and orders
    from the targeted accounts) — the deterministic 'measure response' step."""
    camp = _campaign(campaign_uuid)
    if not camp:
        return {"status": "error", "reason": "campaign not found"}
    since = camp["launched_at"] or datetime.now(timezone.utc)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) FROM marketing_sends "
                "WHERE campaign_uuid=%s::uuid GROUP BY 1", (campaign_uuid,))
            sends = dict(cur.fetchall())
            cur.execute(
                """SELECT count(DISTINCT ac.account_id) FROM activities ac
                   WHERE ac.direction='inbound' AND ac.created_at > %s
                     AND ac.account_id IN (SELECT account_id FROM marketing_sends
                                           WHERE campaign_uuid=%s::uuid)""",
                (since, campaign_uuid))
            replies = cur.fetchone()[0]
            cur.execute(
                """SELECT count(*), COALESCE(SUM(total_amount),0) FROM orders o
                   WHERE o.created_at > %s AND o.deleted_at IS NULL
                     AND o.account_id IN (SELECT account_id FROM marketing_sends
                                          WHERE campaign_uuid=%s::uuid)""",
                (since, campaign_uuid))
            n_orders, order_value = cur.fetchone()
    finally:
        conn.close()
    return {"campaign": camp["name"], "status": camp["status"],
            "launched_at": camp["launched_at"].isoformat() if camp["launched_at"] else None,
            "sends": sends,
            "engagement": {"accounts_replied": replies,
                           "orders_since_launch": n_orders,
                           "order_value_since_launch": float(order_value or 0)}}


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["marketing"])


@router.get("/marketing/campaigns")
def marketing_campaigns(limit: int = 20):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT campaign_uuid::text, name, status, segment, created_at,
                          launched_at, stats
                   FROM marketing_campaigns ORDER BY created_at DESC LIMIT %s""",
                (int(limit),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"count": len(rows), "campaigns": rows}


@router.post("/marketing/campaigns")
def marketing_create(body: Dict[str, Any]):
    for k in ("name", "subject", "body_template"):
        if not body.get(k):
            return {"status": "error", "reason": f"missing {k}"}
    return create_campaign(body["name"], body["subject"], body["body_template"],
                           body.get("segment") or {}, body.get("created_by", "admin"))


@router.post("/marketing/segment-preview")
def marketing_segment_preview(body: Dict[str, Any]):
    rows = resolve_segment(body or {}, limit=int(body.get("limit", 50)))
    return {"count": len(rows), "recipients": rows}


@router.post("/marketing/campaigns/{campaign_uuid}/launch")
async def marketing_launch(campaign_uuid: str, confirm: bool = False):
    return await asyncio.to_thread(launch_campaign, campaign_uuid, confirm)


@router.get("/marketing/campaigns/{campaign_uuid}/results")
def marketing_results(campaign_uuid: str):
    return campaign_results(campaign_uuid)
