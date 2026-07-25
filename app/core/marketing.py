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
                    segment: Dict[str, Any], created_by: str = "admin",
                    subject_b: Optional[str] = None) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO marketing_campaigns
                     (name, subject, subject_b, body_template, segment, created_by)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                   RETURNING campaign_uuid::text""",
                (name, subject, subject_b, body_template,
                 json.dumps(segment or {}), created_by),
            )
            cid = cur.fetchone()[0]
        conn.commit()
        return {"status": "ok", "campaign_uuid": cid,
                "ab_test": bool(subject_b),
                "audience_preview": len(resolve_segment(segment))}
    finally:
        conn.close()


# ── Content generation (the "generate" half) ─────────────────────────────────

_FALLBACK_CONTENT = {
    "high": {
        "subject": "We miss you at {{account_name}}, {{first_name}}",
        "subject_b": "{{first_name}}, a thank-you for {{account_name}}",
        "body_template": (
            "Hi {{first_name}},\n\nIt's been a while since {{account_name}} last "
            "ordered, and we wanted to check in. As a thank-you for your business "
            "(you've trusted us with {{ltv}} over the years), here's 10% off your "
            "next order.\n\nIs there anything we could be doing better? Just reply "
            "— a human reads every answer.\n\nWarm regards,\nThe Conscestra team"),
    },
    "default": {
        "subject": "Something new for {{account_name}}",
        "subject_b": "{{first_name}}, picked for {{account_name}}",
        "body_template": (
            "Hi {{first_name}},\n\nBased on what {{account_name}} usually orders, "
            "we thought you'd want to see what's new this month.\n\nReply to this "
            "email and we'll put a tailored list together.\n\nBest,\n"
            "The Conscestra team"),
    },
}


def draft_campaign_content(segment: Dict[str, Any],
                           goal: str = "") -> Dict[str, Any]:
    """LLM-drafted subject/subject_b/body for a segment, with a deterministic
    fallback so campaign creation always succeeds. Placeholders are preserved:
    {{first_name}} {{account_name}} {{ltv}}."""
    bands = segment.get("churn_band") or []
    bands = [bands] if isinstance(bands, str) else list(bands)
    fallback_key = "high" if "high" in bands else "default"
    fallback = {**_FALLBACK_CONTENT[fallback_key], "source": "template"}

    seg_desc = ", ".join(f"{k}={v}" for k, v in (segment or {}).items()) or "all customers"
    prompt = (
        "You write concise B2B win-back/nurture emails for a CRM. Produce ONLY a "
        "JSON object with keys subject, subject_b (an alternative subject for an "
        "A/B test), and body_template (plain text, under 120 words, friendly, no "
        "hard sell). You MUST use the literal placeholders {{first_name}}, "
        "{{account_name}} and may use {{ltv}} — do not invent other placeholders "
        "or any numbers.\n"
        f"Audience segment: {seg_desc}.\n"
        f"Campaign goal: {goal or 'reconnect and invite a reply'}.")
    try:
        import re as _re
        from app.core.graph_utils import _get_llm
        resp = _get_llm().invoke([{"role": "user", "content": prompt}])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        m = _re.search(r"\{.*\}", raw, _re.S)   # tolerate ```json fences/prose
        data = json.loads(m.group(0)) if m else {}
        if data.get("subject") and data.get("body_template"):
            return {"subject": str(data["subject"])[:200],
                    "subject_b": (str(data["subject_b"])[:200]
                                  if data.get("subject_b") else None),
                    "body_template": str(data["body_template"])[:4000],
                    "source": "llm"}
        logger.warning("[marketing] LLM draft missing keys — using template")
    except Exception as exc:
        logger.warning(f"[marketing] LLM draft failed ({exc}) — using template")
    return fallback


def _campaign(campaign_uuid: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT campaign_uuid::text, name, status, segment, subject,
                          subject_b, body_template, channel, created_by,
                          created_at, launched_at, stats
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
            for i, r in enumerate(recipients):
                # A/B: alternate recipients between subject variants when a
                # subject_b exists (deterministic 50/50 by list position).
                variant = "B" if camp.get("subject_b") and i % 2 else "A"
                subject_tpl = camp["subject_b"] if variant == "B" else camp["subject"]
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
                                subject=_personalize(subject_tpl, r),
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
                          detail, variant, sent_at)
                       VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s,
                               CASE WHEN %s = 'sent' THEN now() END)
                       ON CONFLICT (campaign_uuid, email) DO NOTHING""",
                    (campaign_uuid, r["account_id"], r["contact_id"], r["email"],
                     status, detail, variant, status),
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
            # A/B: replies attributed per subject variant
            cur.execute(
                """SELECT ms.variant, count(*) AS recipients,
                          count(*) FILTER (WHERE EXISTS (
                              SELECT 1 FROM activities ac
                              WHERE ac.account_id = ms.account_id
                                AND ac.direction = 'inbound'
                                AND ac.created_at > %s)) AS replied
                   FROM marketing_sends ms
                   WHERE ms.campaign_uuid = %s::uuid GROUP BY 1 ORDER BY 1""",
                (since, campaign_uuid))
            variants = {v: {"recipients": n, "replied": rep,
                            "reply_rate": round(rep / n, 3) if n else None}
                        for v, n, rep in cur.fetchall()}
    finally:
        conn.close()
    out = {"campaign": camp["name"], "status": camp["status"],
           "launched_at": camp["launched_at"].isoformat() if camp["launched_at"] else None,
           "sends": sends,
           "engagement": {"accounts_replied": replies,
                          "orders_since_launch": n_orders,
                          "order_value_since_launch": float(order_value or 0)}}
    if camp.get("subject_b") and len(variants) > 1:
        a, b = variants.get("A", {}), variants.get("B", {})
        winner = None
        if a.get("reply_rate") is not None and b.get("reply_rate") is not None \
                and a["reply_rate"] != b["reply_rate"]:
            winner = "A" if a["reply_rate"] > b["reply_rate"] else "B"
        out["ab_test"] = {"subject_a": camp["subject"], "subject_b": camp["subject_b"],
                          "variants": variants, "leading": winner}
    return out


def cancel_campaign(campaign_uuid: str, reason: str = "cancelled") -> Dict[str, Any]:
    """Cancel a campaign (governance undo target). Honest about limits: draft
    rows are neutralized, but emails ALREADY SENT cannot be unsent — the count
    of irreversible sends is reported back to the audit trail."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status, count(*) FROM marketing_sends
                   WHERE campaign_uuid=%s::uuid GROUP BY 1""", (campaign_uuid,))
            sends = dict(cur.fetchall())
            cur.execute(
                """UPDATE marketing_campaigns
                   SET status='cancelled',
                       stats = stats || jsonb_build_object('cancelled_reason', %s)
                   WHERE campaign_uuid=%s::uuid AND status <> 'cancelled'
                   RETURNING name""",
                (reason, campaign_uuid))
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "campaign not found or already cancelled"}
    sent = int(sends.get("sent", 0))
    logger.info(f"[marketing] cancelled campaign {row[0]!r} ({reason}); "
                f"irreversible sends={sent}")
    return {"ok": True, "campaign": row[0], "status": "cancelled",
            "drafts_neutralized": int(sends.get("drafted", 0)),
            "already_sent_irreversible": sent,
            "note": ("all recipients were drafts — fully reversed" if not sent
                     else f"{sent} email(s) were already sent and cannot be unsent")}


# ── Agent-initiated win-back (the supervisor → governance → A2A showcase) ────

def winback_campaign_sp(params: Dict[str, Any]) -> Dict[str, Any]:
    """A2A structured handler for intent 'campaign.winback' — executed when a
    governance-queued proposal is APPROVED (or dispatched by an admin). Creates
    and launches a win-back campaign for high-churn customers. The approval IS
    the launch confirmation; real email still additionally requires
    AGENT_BUS_AUTOSEND=1 (drafts otherwise). Idempotent: one win-back per 7d."""
    from datetime import date
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT campaign_uuid::text, name FROM marketing_campaigns
                   WHERE name LIKE 'Win-back%%' AND status <> 'cancelled'
                     AND created_at > now() - interval '7 days' LIMIT 1""")
            dup = cur.fetchone()
    finally:
        conn.close()
    if dup:
        return {"status": "skipped",
                "reason": f"win-back campaign already exists this week ({dup[1]})",
                "campaign_uuid": dup[0]}

    segment = params.get("segment") or {"churn_band": ["high"]}
    content = {k: params[k] for k in ("subject", "subject_b", "body_template")
               if params.get(k)}
    if "subject" not in content or "body_template" not in content:
        content = draft_campaign_content(segment, params.get("goal", "win back "
                                         "high-churn-risk customers"))
    created = create_campaign(
        params.get("name") or f"Win-back — {date.today().isoformat()}",
        content["subject"], content["body_template"], segment,
        created_by=params.get("proposed_by", "supervisor"),
        subject_b=content.get("subject_b"))
    launch = launch_campaign(created["campaign_uuid"], confirm=True)
    return {"status": "ok", "campaign_uuid": created["campaign_uuid"],
            "content_source": content.get("source", "params"),
            "ab_test": created["ab_test"], "launch": launch}


# ============================================================================
# Marketing analytics rollup (blindspot A3 — surface marketing INTO the
# Analytics agent alongside sales + service). Aggregates the per-campaign data
# `campaign_results` computes into a portfolio view. Read-only; degrades
# cleanly when the marketing tables are absent.
# ============================================================================

def marketing_analytics(days: int = 90) -> Dict[str, Any]:
    days = max(1, min(days, 365))
    out: Dict[str, Any] = {"window_days": days}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.marketing_campaigns') IS NOT NULL")
            if not cur.fetchone()[0]:
                return {**out, "error": "marketing tables not found "
                        "(apply sql/marketing.sql)"}

            # Campaigns created in the window, by status.
            cur.execute(
                "SELECT status, count(*) FROM marketing_campaigns "
                "WHERE created_at > now() - make_interval(days => %s) GROUP BY 1",
                (days,))
            camp_by_status = {s: int(n) for s, n in cur.fetchall()}
            total_campaigns = sum(camp_by_status.values())

            # Sends by status for those campaigns (drafted / sent / suppressed / …).
            sends_by_status: Dict[str, int] = {}
            cur.execute("SELECT to_regclass('public.marketing_sends') IS NOT NULL")
            if cur.fetchone()[0]:
                cur.execute(
                    "SELECT ms.status, count(*) FROM marketing_sends ms "
                    "JOIN marketing_campaigns c ON c.campaign_uuid = ms.campaign_uuid "
                    "WHERE c.created_at > now() - make_interval(days => %s) "
                    "GROUP BY 1", (days,))
                sends_by_status = {s: int(n) for s, n in cur.fetchall()}
            total_sends = sum(sends_by_status.values())

            # Engagement attributed to campaign accounts since each launch:
            # inbound replies + orders/value (same attribution as campaign_results).
            accounts_replied = 0
            n_orders, order_value = 0, 0.0
            if total_campaigns:
                cur.execute(
                    "SELECT count(DISTINCT ms.account_id) FROM marketing_sends ms "
                    "JOIN marketing_campaigns c ON c.campaign_uuid = ms.campaign_uuid "
                    "JOIN activities ac ON ac.account_id = ms.account_id "
                    "  AND ac.direction='inbound' "
                    "  AND ac.created_at > COALESCE(c.launched_at, c.created_at) "
                    "WHERE c.created_at > now() - make_interval(days => %s)", (days,))
                accounts_replied = int(cur.fetchone()[0] or 0)
                cur.execute(
                    "SELECT count(*), COALESCE(SUM(o.total_amount),0) FROM orders o "
                    "WHERE o.deleted_at IS NULL "
                    "  AND o.created_at > now() - make_interval(days => %s) "
                    "  AND o.account_id IN (SELECT ms.account_id FROM marketing_sends ms "
                    "     JOIN marketing_campaigns c ON c.campaign_uuid = ms.campaign_uuid "
                    "     WHERE c.created_at > now() - make_interval(days => %s))",
                    (days, days))
                n_orders, order_value = cur.fetchone()

            # Top campaigns by attributed order value (portfolio leaderboard).
            top = []
            if total_campaigns:
                cur.execute(
                    "SELECT c.name, c.status, "
                    "  COALESCE((SELECT count(*) FROM marketing_sends ms "
                    "            WHERE ms.campaign_uuid=c.campaign_uuid AND ms.status='sent'),0), "
                    "  COALESCE((SELECT SUM(o.total_amount) FROM orders o "
                    "            WHERE o.deleted_at IS NULL "
                    "              AND o.created_at > COALESCE(c.launched_at, c.created_at) "
                    "              AND o.account_id IN (SELECT account_id FROM marketing_sends "
                    "                                   WHERE campaign_uuid=c.campaign_uuid)),0) "
                    "FROM marketing_campaigns c "
                    "WHERE c.created_at > now() - make_interval(days => %s) "
                    "ORDER BY 4 DESC NULLS LAST LIMIT 5", (days,))
                top = [{"name": nm, "status": st, "sent": int(sn or 0),
                        "attributed_order_value": float(ov or 0)}
                       for nm, st, sn, ov in cur.fetchall()]
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[marketing] analytics failed: {exc}")
        return {**out, "error": str(exc)[:200]}
    finally:
        conn.close()

    sent = sends_by_status.get("sent", 0)
    out.update({
        "campaigns": {"total": total_campaigns, "by_status": camp_by_status},
        "sends": {"total": total_sends, "by_status": sends_by_status,
                  "sent": sent, "suppressed": sends_by_status.get("suppressed", 0)},
        "engagement": {
            "accounts_replied": accounts_replied,
            "reply_rate_pct": (round(100.0 * accounts_replied / total_sends, 1)
                               if total_sends else None),
            "orders": int(n_orders or 0),
            "order_value": float(order_value or 0),
        },
        "top_campaigns": top,
    })
    return out


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["marketing"])


@router.get("/marketing/analytics")
def marketing_analytics_endpoint(days: int = 90):
    """Portfolio-level marketing analytics — campaigns, sends, engagement."""
    return marketing_analytics(days)


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
                           body.get("segment") or {}, body.get("created_by", "admin"),
                           subject_b=body.get("subject_b"))


@router.post("/marketing/draft")
def marketing_draft(body: Dict[str, Any]):
    """LLM-draft campaign content for a segment (deterministic fallback).
    Pass create=true to also create the draft campaign in one step."""
    segment = body.get("segment") or {}
    content = draft_campaign_content(segment, body.get("goal", ""))
    if body.get("create"):
        created = create_campaign(
            body.get("name") or "Drafted campaign", content["subject"],
            content["body_template"], segment, body.get("created_by", "admin"),
            subject_b=content.get("subject_b"))
        return {**content, **created}
    return content


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
