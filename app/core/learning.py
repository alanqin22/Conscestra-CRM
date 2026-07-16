"""Learning loop — agent outcome analytics ("did the automation work?").

The agents now MEASURE (sequence outcomes, campaign sends/replies/orders,
churn-prediction snapshots) but nothing read the totals. This module is the
accountability read-side:

    agent_performance(days) →
        cadences:  per playbook — plays ended, outcome mix, save/engage rate
        campaigns: launched, sends by status, replies + orders attributed
        churn model: calibration verdict (intelligence.calibrate())

    detect_bottlenecks() →   WHERE work silently piles up: untouched new
        leads, stalled deals, overdue invoices nobody has chased, aging
        open tasks by owner, inbound messages never answered. Pure reads;
        the weekly bottleneck_pass consolidates any findings into ONE
        Orchestrator notification (upserted, never a pile) so the queue
        carries a process-health heartbeat instead of per-record noise.

Surfaced in the CEO morning briefing ("Agent Performance") and at
GET /learning/performance + GET /learning/bottlenecks. Read-only; every
query tolerates its table being absent so the briefing never breaks on a
partial deployment.

CONFIG (env)
  BOTTLENECKS_ENABLED  1   weekly bottleneck notification on/off (reads only)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("learning")

# Outcomes that count as the play WORKING, per playbook.
_SUCCESS = {
    "churn_save":    ("won_back", "re-engaged", "risk_subsided"),
    "lead_followup": ("engaged", "converted"),
}


def _q(conn, sql: str, params=None) -> List[tuple]:
    """Query that tolerates a missing table (returns [])."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[learning] query skipped: {exc}")
        return []


def agent_performance(days: int = 30) -> Dict[str, Any]:
    conn = get_connection()
    try:
        # ── Cadences: outcome mix per playbook ─────────────────────────────
        cadences: Dict[str, Any] = {}
        for playbook, outcome, n in _q(conn,
                """SELECT playbook, COALESCE(outcome, status), count(*)
                   FROM agent_sequences
                   WHERE status IN ('completed','cancelled')
                     AND updated_at > now() - make_interval(days => %s)
                   GROUP BY 1, 2""", (days,)):
            cadences.setdefault(playbook, {"ended": 0, "outcomes": {}})
            cadences[playbook]["ended"] += n
            cadences[playbook]["outcomes"][outcome] = n
        for playbook, d in cadences.items():
            wins = sum(n for o, n in d["outcomes"].items()
                       if o in _SUCCESS.get(playbook, ()))
            d["success"] = wins
            d["success_rate"] = round(wins / d["ended"], 3) if d["ended"] else None

        # ── Campaigns: sends + attributed engagement ───────────────────────
        camp_rows = _q(conn,
            """SELECT c.campaign_uuid::text, c.name, c.launched_at
               FROM marketing_campaigns c
               WHERE c.status IN ('launched','completed')
                 AND c.launched_at > now() - make_interval(days => %s)""", (days,))
        campaigns = {"launched": len(camp_rows), "sends": {},
                     "accounts_replied": 0, "orders": 0, "order_value": 0.0}
        for cid, _name, launched_at in camp_rows:
            for status, n in _q(conn,
                    "SELECT status, count(*) FROM marketing_sends "
                    "WHERE campaign_uuid=%s::uuid GROUP BY 1", (cid,)):
                campaigns["sends"][status] = campaigns["sends"].get(status, 0) + n
            r = _q(conn,
                """SELECT count(DISTINCT ac.account_id) FROM activities ac
                   WHERE ac.direction='inbound' AND ac.created_at > %s
                     AND ac.account_id IN (SELECT account_id FROM marketing_sends
                                           WHERE campaign_uuid=%s::uuid)""",
                (launched_at, cid))
            campaigns["accounts_replied"] += (r[0][0] if r else 0)
            r = _q(conn,
                """SELECT count(*), COALESCE(SUM(total_amount),0) FROM orders o
                   WHERE o.created_at > %s AND o.deleted_at IS NULL
                     AND o.account_id IN (SELECT account_id FROM marketing_sends
                                          WHERE campaign_uuid=%s::uuid)""",
                (launched_at, cid))
            if r:
                campaigns["orders"] += r[0][0]
                campaigns["order_value"] += float(r[0][1] or 0)
    finally:
        conn.close()

    # ── Churn model: is it earning its predictions? ─────────────────────────
    try:
        from app.core import intelligence
        calibration = intelligence.calibrate()
    except Exception as exc:
        logger.debug(f"[learning] calibration skipped: {exc}")
        calibration = {"verdict": [f"calibration unavailable: {exc}"], "bands": {}}

    # ── AI fuel gauge: what did the automation COST? (best-effort) ──────────
    try:
        from app.core import llm_meter
        ai_spend = llm_meter.spend_lines(1)
    except Exception as exc:
        logger.debug(f"[learning] llm spend skipped: {exc}")
        ai_spend = []

    # ── Data-quality pulse (best-effort) ────────────────────────────────────
    try:
        from app.core import data_quality
        dq = data_quality.scan_lines()
    except Exception as exc:
        logger.debug(f"[learning] data-quality skipped: {exc}")
        dq = []

    return {"window_days": days, "cadences": cadences,
            "campaigns": campaigns, "churn_calibration": calibration,
            "ai_spend": ai_spend + dq}


# ============================================================================
# BOTTLENECK DETECTION — where work silently piles up
# ============================================================================

import os as _os


def _flag(name: str, default: str = "1") -> bool:
    return _os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


BOTTLENECKS_ENABLED = _flag("BOTTLENECKS_ENABLED", "1")
_ORCH_AGENT = "00000000-0000-0000-0000-000000000012"   # Orchestrator Agent


def detect_bottlenecks() -> Dict[str, Any]:
    """Deterministic process-health scan (pure reads, no LLM). Each check
    reports only when it finds something; a missing table just skips."""
    from datetime import datetime, timezone
    conn = get_connection()
    findings: List[Dict[str, Any]] = []
    try:
        # 1. New leads nobody has touched — acquisition leaking at the top.
        rows = _q(conn, """
            SELECT count(*),
                   array_agg(COALESCE(NULLIF(TRIM(COALESCE(first_name,'')||' '||
                       COALESCE(last_name,'')),''), company)
                       ORDER BY created_at) FILTER (WHERE true)
            FROM leads l
            WHERE l.status='new' AND l.deleted_at IS NULL
              AND l.created_at < now() - interval '3 days'
              AND NOT EXISTS (SELECT 1 FROM activities a
                              WHERE a.lead_id = l.lead_id)""")
        if rows and int(rows[0][0] or 0):
            findings.append({
                "kind": "untouched_leads", "count": int(rows[0][0]),
                "note": "new leads older than 3 days with no activity at all",
                "worst": (rows[0][1] or [])[:3]})

        # 2. Open deals nothing has happened to in two weeks.
        rows = _q(conn, """
            SELECT count(*), array_agg(name ORDER BY amount DESC NULLS LAST)
            FROM opportunities
            WHERE status='open' AND updated_at < now() - interval '14 days'""")
        if rows and int(rows[0][0] or 0):
            findings.append({
                "kind": "stalled_opportunities", "count": int(rows[0][0]),
                "note": "open deals untouched for 14+ days",
                "worst": (rows[0][1] or [])[:3]})

        # 3. Overdue invoices with NO outbound chase in two weeks — dunning
        #    exists, so silence here means the loop is broken somewhere.
        rows = _q(conn, """
            SELECT count(*), array_agg(x.label ORDER BY x.due)
            FROM (SELECT (i.invoice_number || ' (' ||
                          COALESCE(a.account_name,'?') || ')') AS label,
                         i.due_date AS due
                  FROM invoices i
                  LEFT JOIN accounts a ON a.account_id = i.account_id
                  WHERE i.status='overdue'
                    AND (i.is_deleted IS NULL OR i.is_deleted=false)
                    AND COALESCE(i.balance_due,0) > 0
                    AND NOT EXISTS (SELECT 1 FROM activities t
                                    WHERE t.account_id = i.account_id
                                      AND t.direction='outbound'
                                      AND t.created_at > now() - interval '14 days')
                 ) x""")
        if rows and int(rows[0][0] or 0):
            findings.append({
                "kind": "silent_overdue_invoices", "count": int(rows[0][0]),
                "note": "overdue invoices with no outbound touch in 14 days",
                "worst": (rows[0][1] or [])[:3]})

        # 4. Open tasks a week or more past due, by owner — WHO is the
        #    bottleneck matters more than the raw count.
        rows = _q(conn, """
            SELECT COALESCE(NULLIF(TRIM(COALESCE(w.first_name,'')||' '||
                       COALESCE(w.last_name,'')),''),'Unassigned'),
                   count(*)
            FROM activities a
            LEFT JOIN owners w ON w.owner_id = a.owner_id
            WHERE a.status='open' AND a.due_at < now() - interval '7 days'
            GROUP BY 1 ORDER BY 2 DESC""")
        total = sum(int(r[1]) for r in rows)
        if total:
            findings.append({
                "kind": "aging_tasks", "count": total,
                "note": "open activities 7+ days past due",
                "worst": [f"{r[0]}: {r[1]}" for r in rows[:3]]})

        # 5. Inbound messages that never got an answer within 48 h.
        rows = _q(conn, """
            SELECT count(*), array_agg(x.subj ORDER BY x.at DESC)
            FROM (SELECT LEFT(COALESCE(i.subject,'(no subject)'), 60) AS subj,
                         i.created_at AS at
                  FROM activities i
                  WHERE i.direction='inbound'
                    AND i.created_at BETWEEN now() - interval '14 days'
                                         AND now() - interval '48 hours'
                    AND i.account_id IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM activities o
                                    WHERE o.account_id = i.account_id
                                      AND o.direction='outbound'
                                      AND o.created_at BETWEEN i.created_at
                                          AND i.created_at + interval '48 hours')
                 ) x""")
        if rows and int(rows[0][0] or 0):
            findings.append({
                "kind": "unanswered_inbound", "count": int(rows[0][0]),
                "note": "inbound messages with no outbound reply within 48 h",
                "worst": (rows[0][1] or [])[:3]})
    finally:
        conn.close()
    return {"as_of": datetime.now(timezone.utc).isoformat(),
            "findings": findings,
            "checks_run": 5, "clear": 5 - len(findings)}


def bottleneck_pass(force: bool = False) -> Dict[str, Any]:
    """Weekly: scan → ONE consolidated Orchestrator notification (upserted —
    a heartbeat, not a pile). No findings = no notification."""
    if not BOTTLENECKS_ENABLED and not force:
        return {"enabled": False, "skipped": True}
    report = detect_bottlenecks()
    findings = report["findings"]
    if not findings:
        logger.info("[learning] bottleneck pass: all clear")
        return {**report, "notified": False}

    title = f"⏳ Process bottlenecks: {len(findings)} area(s) piling up"
    lines = []
    for f in findings:
        worst = ("; ".join(str(w) for w in f["worst"])
                 if f.get("worst") else "")
        lines.append(f"• {f['count']} {f['note']}"
                     + (f" — e.g. {worst}" if worst else ""))
    body = ("Weekly process-health scan (learning loop):\n" + "\n".join(lines)
            + "\n\nDetails: GET /learning/bottlenecks")
    try:
        import json as _json
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT notification_uuid FROM notifications
                   WHERE employee_uuid=%s::uuid
                     AND title LIKE '⏳ Process bottlenecks%%'
                     AND status = ANY(%s) LIMIT 1""",
                (_ORCH_AGENT, ["pending", "sent", "unread"]))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE notifications SET title=%s, body=%s, created_at=now() "
                    "WHERE notification_uuid=%s",
                    (title, body, row[0]))
            else:
                # notifications is a VIEW whose insert trigger fans out to
                # notification_messages, which requires an anchoring event —
                # emit one first (governance's routed-approval pattern).
                cur.execute(
                    "SELECT emit_event(%s,%s,%s,%s,%s,%s)",
                    ("bottleneck.detected", "agent", _ORCH_AGENT,
                     _json.dumps({"context": {
                         "areas": [f["kind"] for f in findings]}}),
                     None, "learning"))
                event_uuid = cur.fetchone()[0]
                # metadata.kind marks this a DIGEST row — without it the
                # view's insert trigger dedupes onto the event's auto-fanout
                # message and discards our title/body.
                cur.execute(
                    """INSERT INTO notifications
                         (employee_uuid, event_uuid, channel, status, title,
                          body, metadata, created_at)
                       VALUES (%s::uuid, %s, 'in_app', 'pending', %s, %s,
                               %s::jsonb, now())""",
                    (_ORCH_AGENT, event_uuid, title, body,
                     _json.dumps({"kind": "bottleneck_digest",
                                  "areas": [f["kind"] for f in findings]})))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"[learning] bottleneck notification skipped: {exc}")
        return {**report, "notified": False}
    logger.info(f"[learning] bottleneck pass: {len(findings)} finding(s) "
                "→ notification upserted")
    return {**report, "notified": True}


router = APIRouter(tags=["learning"])


@router.get("/learning/performance")
def learning_performance(days: int = 30):
    """The agents' report card: cadence save rates, campaign conversion,
    churn-prediction calibration — everything the CEO briefing shows."""
    return agent_performance(days)


@router.get("/learning/bottlenecks")
def learning_bottlenecks():
    """Live process-health scan: where work is silently piling up."""
    return detect_bottlenecks()


@router.post("/learning/bottleneck-pass")
def learning_bottleneck_pass():
    """Run the weekly scan→notify pass now (forced; the job self-gates)."""
    return bottleneck_pass(True)
