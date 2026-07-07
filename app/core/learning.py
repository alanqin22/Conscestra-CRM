"""Learning loop — agent outcome analytics ("did the automation work?").

The agents now MEASURE (sequence outcomes, campaign sends/replies/orders,
churn-prediction snapshots) but nothing read the totals. This module is the
accountability read-side:

    agent_performance(days) →
        cadences:  per playbook — plays ended, outcome mix, save/engage rate
        campaigns: launched, sends by status, replies + orders attributed
        churn model: calibration verdict (intelligence.calibrate())

Surfaced in the CEO morning briefing ("Agent Performance") and at
GET /learning/performance. Read-only; every query tolerates its table being
absent so the briefing never breaks on a partial deployment.
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

    return {"window_days": days, "cadences": cadences,
            "campaigns": campaigns, "churn_calibration": calibration}


router = APIRouter(tags=["learning"])


@router.get("/learning/performance")
def learning_performance(days: int = 30):
    """The agents' report card: cadence save rates, campaign conversion,
    churn-prediction calibration — everything the CEO briefing shows."""
    return agent_performance(days)
