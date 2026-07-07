"""Lead qualification — win probability + recommended sales rep.

Completes the vision's Lead Qualification card:

    Lead Score: 91/100 · Hot
    Win probability: 82%          ← score-band conversion HISTORY, not a guess
    Recommended rep: David        ← industry experience + current load

DETERMINISTIC BY DESIGN
-----------------------
  • win_probability(score): historical conversion rate of SETTLED leads in the
    same score band (Hot ≥70 / Warm ≥40 / Cold), Laplace-smoothed so small
    samples don't scream 0% or 100%. As more leads settle, the number sharpens
    — the same learning-loop posture as churn calibration.
  • recommend_rep(industry): active owners ranked by (1) how many accounts they
    already manage in the lead's industry (domain experience), then (2) fewest
    open leads currently assigned (load balancing), then (3) total accounts.
    Every recommendation ships with its reason.

Consumers: handle_lead_scored posts the card onto the hot-lead blackboard
note; GET /qualification/lead/{id} serves it on demand.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("qualification")

HOT_MIN, WARM_MIN = 70, 40


def _band(score: int) -> str:
    return "Hot" if score >= HOT_MIN else "Warm" if score >= WARM_MIN else "Cold"


def _band_range(band: str):
    return {"Hot": (HOT_MIN, 1000), "Warm": (WARM_MIN, HOT_MIN),
            "Cold": (-1000, WARM_MIN)}[band]


def win_probability(score: int) -> Dict[str, Any]:
    """P(convert | score band) from settled leads. Laplace-smoothed (+1/+2)."""
    band = _band(int(score or 0))
    lo, hi = _band_range(band)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS settled,
                          count(*) FILTER (WHERE COALESCE(converted, false)
                                              OR status = 'converted') AS won
                   FROM leads
                   WHERE COALESCE(score, 0) >= %s AND COALESCE(score, 0) < %s
                     AND (COALESCE(converted, false)
                          OR status IN ('converted', 'disqualified')
                          OR COALESCE(is_deleted, false))""",
                (lo, hi))
            settled, won = cur.fetchone()
    finally:
        conn.close()
    prob = (won + 1) / (settled + 2)   # smoothing: no 0%/100% on tiny samples
    return {"band": band, "probability": round(prob, 3),
            "history": {"settled": settled, "converted": won},
            "basis": f"{won}/{settled} settled {band} leads converted"}


def recommend_rep(industry: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best active owner for this lead: industry experience first, then load."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.owner_id::text,
                          TRIM(COALESCE(o.first_name,'') || ' ' ||
                               COALESCE(o.last_name,''))              AS rep,
                          count(a.account_id) FILTER
                              (WHERE %(ind)s <> '' AND a.industry ILIKE %(indlike)s)
                                                                       AS industry_accounts,
                          (SELECT count(*) FROM leads l
                            WHERE l.owner_id = o.owner_id
                              AND COALESCE(l.converted, false) = false
                              AND COALESCE(l.is_deleted, false) = false
                              AND COALESCE(l.status,'') NOT IN
                                  ('converted','disqualified'))        AS open_leads,
                          count(a.account_id)                          AS total_accounts
                   FROM owners o
                   LEFT JOIN accounts a ON a.owner_id = o.owner_id
                                       AND COALESCE(a.is_deleted, false) = false
                   WHERE o.is_active
                   GROUP BY o.owner_id, rep
                   ORDER BY industry_accounts DESC, open_leads ASC,
                            total_accounts DESC, rep
                   LIMIT 1""",
                {"ind": industry or "", "indlike": f"%{industry or ''}%"})
            r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return None
    owner_id, rep, ind_n, load, total = r
    reason = (f"manages {ind_n} {industry} account(s), current load {load} open lead(s)"
              if industry and ind_n else
              f"lightest load ({load} open lead(s)) across {total} account(s)")
    return {"owner_id": owner_id, "rep": rep or "unassigned",
            "industry_accounts": ind_n, "open_leads": load, "reason": reason}


def qualify(lead_id: str) -> Dict[str, Any]:
    """The full qualification card for one lead."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT lead_id::text, first_name, last_name, company, industry,
                          COALESCE(score, 0), status
                   FROM leads WHERE lead_id = %s::uuid""", (lead_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"lead_id": lead_id, "error": "lead not found"}
    _id, first, last, company, industry, score, status = row
    wp = win_probability(score)
    rep = recommend_rep(industry)
    return {
        "lead_id": _id,
        "name": f"{first or ''} {last or ''}".strip() or company,
        "company": company, "industry": industry, "status": status,
        "score": score, "band": wp["band"],
        "win_probability": wp["probability"], "win_basis": wp["basis"],
        "recommended_rep": rep,
    }


router = APIRouter(tags=["qualification"])


@router.get("/qualification/lead/{lead_id}")
def qualification_lead(lead_id: str):
    """Score band + historical win probability + recommended rep for a lead."""
    return qualify(lead_id)
