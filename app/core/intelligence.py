"""Customer Intelligence — nightly deterministic profile scorer.

WHAT THIS IS
------------
The vision's "living profile". ai_summary computes a 360 only when asked; this
module PERSISTS per-account learned attributes every night in
`account_intelligence` (sql/account_intelligence.sql):

    churn risk (0..1 + band)   RFM / LTV        preferred channel
    purchase rhythm            expected next purchase   AR exposure

so agents can act on them proactively:

  • accounts ai_summary reads the profile row into its fact sheet, and the
    churn_risk blackboard note (high band only) shows up as a live agent signal.
  • the supervisor's detect_churn_risk() watches the high-band aggregate and
    raises a supervisor.alert when it breaches.

DETERMINISTIC BY DESIGN — no LLM per account (cost + explainability). The churn
score is a transparent weighted sum; every component is stored in `signals`:

    lateness   0.55  how far past the account's OWN typical order gap we are
    engagement 0.25  days since any activity touch
    ar         0.10  overdue invoices outstanding
    lost_deal  0.10  a lost opportunity in the last 90d with no win since

    churn = clamp(Σ wᵢ·cᵢ)   band: ≥0.7 high, ≥0.4 medium, else low

Only accounts with ≥1 order are scored (customers, not prospects).

SAFETY
------
  • Nightly job self-gates on INTEL_ENABLED (default 0); the admin endpoint
    /intelligence/run-once works regardless (explicit, on-demand).
  • Writes are UPSERTS to account_intelligence + blackboard notes (high band
    only, 48h TTL so notes self-expire if the scorer stops). Nothing else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("intelligence")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("INTEL_ENABLED")

# Churn bands + component weights (documented in the module docstring).
HIGH_BAND, MEDIUM_BAND = 0.70, 0.40
W_LATENESS, W_ENGAGEMENT, W_AR, W_LOST = 0.55, 0.25, 0.10, 0.10
MIN_GAP_DAYS = 30       # floor for "typical gap" so one-off buyers aren't instantly late
DEFAULT_GAP_DAYS = 90   # assumed rhythm for single-order accounts


# ============================================================================
# METRICS — one set-based pass over the whole customer base
# ============================================================================

_METRICS_SQL = """
WITH cust AS (                       -- customers = accounts with ≥1 order
    SELECT o.account_id,
           COUNT(*)                                            AS orders_total,
           COALESCE(SUM(o.total_amount), 0)                    AS ltv,
           COUNT(*)    FILTER (WHERE o.order_date > now() - interval '365 days') AS orders_12m,
           COALESCE(SUM(o.total_amount)
                    FILTER (WHERE o.order_date > now() - interval '365 days'), 0) AS revenue_12m,
           MAX(o.order_date)::date                             AS last_order_at,
           (CURRENT_DATE - MAX(o.order_date)::date)            AS order_recency_days
    FROM   orders o
    WHERE  o.deleted_at IS NULL AND o.account_id IS NOT NULL
    GROUP  BY o.account_id
),
gaps AS (                            -- median days between consecutive orders
    SELECT account_id,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_days) AS median_gap
    FROM (
        SELECT account_id,
               EXTRACT(epoch FROM order_date
                       - lag(order_date) OVER (PARTITION BY account_id
                                               ORDER BY order_date)) / 86400.0 AS gap_days
        FROM orders WHERE deleted_at IS NULL AND account_id IS NOT NULL
    ) g
    WHERE gap_days IS NOT NULL
    GROUP BY account_id
),
touch AS (                           -- engagement recency + modal channel (12m)
    SELECT account_id,
           (CURRENT_DATE - MAX(created_at)::date) AS engagement_days,
           (SELECT a2.channel FROM activities a2
             WHERE a2.account_id = a.account_id AND a2.channel IS NOT NULL
               AND a2.channel <> 'system'   -- human channels only, not event logs
               AND a2.created_at > now() - interval '365 days'
             GROUP BY a2.channel ORDER BY count(*) DESC LIMIT 1) AS preferred_channel,
           -- "replies after 8 PM": modal ET hour of the customer's own
           -- (inbound) touches — when they actually engage
           (SELECT EXTRACT(hour FROM a3.created_at
                           AT TIME ZONE 'America/New_York')::int
             FROM activities a3
             WHERE a3.account_id = a.account_id AND a3.direction = 'inbound'
               AND a3.created_at > now() - interval '365 days'
             GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1) AS preferred_hour
    FROM activities a
    WHERE a.account_id IS NOT NULL
    GROUP BY a.account_id
),
interest AS (                        -- top 3 ordered product categories (12m)
    SELECT account_id,
           (array_agg(category_name ORDER BY cnt DESC))[1:3] AS interests
    FROM (
        SELECT o.account_id, cat.category_name, count(*) AS cnt
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.order_id
        JOIN products p     ON p.product_id = oi.product_id
        JOIN category cat   ON cat.category_id = p.category_id
        WHERE o.deleted_at IS NULL AND o.account_id IS NOT NULL
          AND o.order_date > now() - interval '365 days'
        GROUP BY 1, 2
    ) t GROUP BY account_id
),
senti AS (                           -- 90-day customer-voice sentiment
    SELECT ct.account_id,
           ROUND(AVG(es.score), 3) AS sentiment_score,
           count(*)                AS sentiment_n
    FROM email_sentiment es
    JOIN contacts ct ON lower(ct.email) = lower(es.from_addr)
    WHERE ct.account_id IS NOT NULL
      AND es.created_at > now() - interval '90 days'
    GROUP BY 1
),
ar AS (                              -- overdue exposure
    SELECT account_id,
           COUNT(*)                              AS overdue_invoices,
           COALESCE(SUM(computed_balance_due),0) AS open_ar_balance
    FROM   accounting_invoice_pipeline
    WHERE  payment_status IN ('unpaid','partial') AND due_date::date < CURRENT_DATE
    GROUP  BY account_id
),
lost AS (                            -- lost a deal in 90d with no win since
    SELECT o1.account_id, TRUE AS lost_recent
    FROM   opportunities o1
    WHERE  o1.status ILIKE '%%lost%%'
      AND  o1.updated_at > now() - interval '90 days'
      AND  NOT EXISTS (SELECT 1 FROM opportunities o2
                       WHERE o2.account_id = o1.account_id
                         AND o2.status ILIKE '%%won%%'
                         AND o2.updated_at > o1.updated_at)
    GROUP  BY o1.account_id
)
SELECT c.account_id::text, a.account_name,
       c.ltv, c.orders_total, c.orders_12m, c.revenue_12m,
       c.last_order_at, c.order_recency_days,
       ROUND(g.median_gap)::int          AS typical_gap_days,
       t.engagement_days, t.preferred_channel, t.preferred_hour,
       i.interests,
       s.sentiment_score, s.sentiment_n,
       COALESCE(r.open_ar_balance, 0)    AS open_ar_balance,
       COALESCE(r.overdue_invoices, 0)   AS overdue_invoices,
       COALESCE(l.lost_recent, FALSE)    AS lost_recent
FROM   cust c
JOIN   accounts a ON a.account_id = c.account_id
LEFT   JOIN gaps     g ON g.account_id = c.account_id
LEFT   JOIN touch    t ON t.account_id = c.account_id
LEFT   JOIN interest i ON i.account_id = c.account_id
LEFT   JOIN senti    s ON s.account_id = c.account_id
LEFT   JOIN ar       r ON r.account_id = c.account_id
LEFT   JOIN lost     l ON l.account_id = c.account_id
"""


def _sentiment_label(score) -> Optional[str]:
    if score is None:
        return None
    s = float(score)
    return "positive" if s > 0.2 else ("negative" if s < -0.2 else "neutral")


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score(m: Dict[str, Any]) -> Dict[str, Any]:
    """Transparent weighted churn score; returns score, band + components."""
    gap = int(m.get("typical_gap_days") or DEFAULT_GAP_DAYS)
    gap = max(gap, MIN_GAP_DAYS)
    recency = int(m.get("order_recency_days") or 0)
    # 0 when inside the account's own rhythm; 1 when ~3x past it.
    c_late = _clamp((recency / gap - 1.0) / 2.0)

    eng = m.get("engagement_days")
    # unknown engagement is mildly suspicious, not damning
    c_eng = 0.5 if eng is None else _clamp((int(eng) - 60) / 120.0)

    c_ar = _clamp(int(m.get("overdue_invoices") or 0) / 3.0)
    c_lost = 1.0 if m.get("lost_recent") else 0.0

    risk = _clamp(W_LATENESS * c_late + W_ENGAGEMENT * c_eng
                  + W_AR * c_ar + W_LOST * c_lost)
    band = "high" if risk >= HIGH_BAND else "medium" if risk >= MEDIUM_BAND else "low"
    return {
        "churn_risk": round(risk, 3), "churn_band": band,
        "components": {"lateness": round(c_late, 3), "engagement": round(c_eng, 3),
                       "ar": round(c_ar, 3), "lost_deal": round(c_lost, 3)},
        "inputs": {"order_recency_days": recency, "typical_gap_days": gap,
                   "engagement_days": eng,
                   "overdue_invoices": int(m.get("overdue_invoices") or 0),
                   "lost_recent": bool(m.get("lost_recent"))},
    }


def run_scoring_sync(post_blackboard: bool = True,
                     start_sequences: bool = True) -> Dict[str, Any]:
    """Score every customer account and upsert account_intelligence. Posts a
    churn_risk blackboard note for HIGH-band accounts (48h TTL — self-expiring)
    and starts a churn_save cadence for accounts that ENTER the high band
    (transition-triggered, so a persistently-high account isn't replayed;
    sequences.start() self-gates on SEQUENCES_ENABLED)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Prior bands — to detect the low/medium → high TRANSITION.
            cur.execute("SELECT account_id::text, churn_band FROM account_intelligence")
            prior_band = dict(cur.fetchall())

            cur.execute(_METRICS_SQL)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            bands = {"low": 0, "medium": 0, "high": 0}
            high: List[Dict[str, Any]] = []
            for m in rows:
                s = _score(m)
                bands[s["churn_band"]] += 1
                gap = s["inputs"]["typical_gap_days"]
                cur.execute(
                    """INSERT INTO account_intelligence
                         (account_id, ltv, orders_12m, revenue_12m, last_order_at,
                          order_recency_days, typical_gap_days, next_purchase_due,
                          engagement_days, preferred_channel, preferred_hour,
                          interests, sentiment_score, sentiment_label,
                          open_ar_balance, overdue_invoices, churn_risk,
                          churn_band, signals, computed_at)
                       VALUES (%(id)s::uuid, %(ltv)s, %(o12)s, %(r12)s, %(last)s,
                               %(rec)s, %(gap)s,
                               (%(last)s::date + make_interval(days => %(gap)s))::date,
                               %(eng)s, %(chan)s, %(hour)s,
                               %(ints)s, %(sent)s, %(sentl)s,
                               %(ar)s, %(ov)s, %(risk)s, %(band)s, %(sig)s::jsonb, now())
                       ON CONFLICT (account_id) DO UPDATE SET
                           ltv = EXCLUDED.ltv, orders_12m = EXCLUDED.orders_12m,
                           revenue_12m = EXCLUDED.revenue_12m,
                           last_order_at = EXCLUDED.last_order_at,
                           order_recency_days = EXCLUDED.order_recency_days,
                           typical_gap_days = EXCLUDED.typical_gap_days,
                           next_purchase_due = EXCLUDED.next_purchase_due,
                           engagement_days = EXCLUDED.engagement_days,
                           preferred_channel = EXCLUDED.preferred_channel,
                           preferred_hour = EXCLUDED.preferred_hour,
                           interests = EXCLUDED.interests,
                           sentiment_score = EXCLUDED.sentiment_score,
                           sentiment_label = EXCLUDED.sentiment_label,
                           open_ar_balance = EXCLUDED.open_ar_balance,
                           overdue_invoices = EXCLUDED.overdue_invoices,
                           churn_risk = EXCLUDED.churn_risk,
                           churn_band = EXCLUDED.churn_band,
                           signals = EXCLUDED.signals, computed_at = now()""",
                    {"id": m["account_id"], "ltv": m["ltv"], "o12": m["orders_12m"],
                     "r12": m["revenue_12m"], "last": m.get("last_order_at"),
                     "rec": m.get("order_recency_days"), "gap": gap,
                     "eng": m.get("engagement_days"), "chan": m.get("preferred_channel"),
                     "hour": m.get("preferred_hour"), "ints": m.get("interests"),
                     "sent": m.get("sentiment_score"),
                     "sentl": _sentiment_label(m.get("sentiment_score")),
                     "ar": m["open_ar_balance"], "ov": m["overdue_invoices"],
                     "risk": s["churn_risk"], "band": s["churn_band"],
                     "sig": json.dumps({"components": s["components"],
                                        "inputs": s["inputs"],
                                        "weights": {"lateness": W_LATENESS,
                                                    "engagement": W_ENGAGEMENT,
                                                    "ar": W_AR, "lost_deal": W_LOST}})},
                )
                # Prediction history — the raw material for calibrate().
                cur.execute(
                    """INSERT INTO account_intelligence_history
                         (account_id, snapshot_date, churn_risk, churn_band,
                          order_recency_days)
                       VALUES (%s::uuid, CURRENT_DATE, %s, %s, %s)
                       ON CONFLICT (account_id, snapshot_date) DO UPDATE SET
                           churn_risk = EXCLUDED.churn_risk,
                           churn_band = EXCLUDED.churn_band,
                           order_recency_days = EXCLUDED.order_recency_days""",
                    (m["account_id"], s["churn_risk"], s["churn_band"],
                     m.get("order_recency_days")))
                if s["churn_band"] == "high":
                    high.append({"account_id": m["account_id"],
                                 "name": m.get("account_name"),
                                 "risk": s["churn_risk"], "ltv": float(m["ltv"] or 0),
                                 "recency": s["inputs"]["order_recency_days"],
                                 "gap": gap,
                                 "entered": prior_band.get(m["account_id"]) != "high"})
        conn.commit()
    finally:
        conn.close()

    # High-band accounts get a live blackboard signal other agents (and
    # ai_summary) already read. TTL 48h — self-expiring if the scorer stops.
    posted = 0
    if post_blackboard:
        from app.core import blackboard
        for h in high:
            try:
                blackboard.post(
                    "account", h["account_id"], "intelligence", "churn_risk",
                    (f"Churn risk HIGH ({h['risk']:.2f}) — {h['recency']}d since "
                     f"last order vs typical {h['gap']}d gap; LTV ${h['ltv']:,.0f}"),
                    {"churn_risk": h["risk"], "ltv": h["ltv"],
                     "order_recency_days": h["recency"], "typical_gap_days": h["gap"]},
                    h["risk"], "warning", 48)
                posted += 1
            except Exception as exc:
                logger.warning(f"[intelligence] blackboard post failed for "
                               f"{h['account_id']}: {exc}")

    # Accounts ENTERING the high band kick off the churn_save cadence
    # (Phase 6 sequences engine) — the "flag churn → check complaints →
    # offer → escalate" play. No-op unless SEQUENCES_ENABLED.
    saves_started = 0
    if start_sequences:
        try:
            from app.core import sequences
            for h in high:
                if not h["entered"]:
                    continue
                r = sequences.start(
                    "churn_save", "account", h["account_id"],
                    {"churn_risk": h["risk"], "ltv": h["ltv"], "name": h["name"]},
                    "intelligence")
                if r.get("status") == "ok":
                    saves_started += 1
        except Exception as exc:
            logger.warning(f"[intelligence] churn_save start failed: {exc}")

    out = {"scored": len(rows), "bands": bands,
           "high_risk_notes_posted": posted,
           "churn_saves_started": saves_started,
           "top_high_risk": sorted(high, key=lambda h: -h["ltv"])[:5]}
    logger.info(f"[intelligence] scoring pass — scored={out['scored']} bands={bands} "
                f"notes={posted} churn_saves={saves_started}")
    return out


def calibrate(horizon_days: int = 30, window_days: int = 30) -> Dict[str, Any]:
    """Score the scorer: take each account's prediction from `horizon_days` ago
    (up to horizon+window back), and check whether they ACTUALLY churned —
    defined as placing no order in the `horizon_days` after that snapshot.

    Returns per-band churn rates + sample sizes and a plain-language verdict:
      • high-band precision — of those we flagged, how many really went quiet
      • low-band miss rate  — churners we called safe (the expensive mistake)
    Purely evidential; it never mutates weights. Use it to justify tuning
    W_* / HIGH_BAND rather than trusting the initial guesses."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """WITH snap AS (
                       SELECT DISTINCT ON (account_id)
                              account_id, snapshot_date, churn_band, churn_risk
                       FROM account_intelligence_history
                       WHERE snapshot_date BETWEEN CURRENT_DATE - %(back)s
                                               AND CURRENT_DATE - %(h)s
                       ORDER BY account_id, snapshot_date DESC
                   )
                   SELECT s.churn_band, count(*) AS n,
                          count(*) FILTER (WHERE NOT EXISTS (
                              SELECT 1 FROM orders o
                              WHERE o.account_id = s.account_id
                                AND o.deleted_at IS NULL
                                AND o.order_date::date >  s.snapshot_date
                                AND o.order_date::date <= s.snapshot_date + %(h)s
                          )) AS churned
                   FROM snap s GROUP BY 1""",
                {"h": horizon_days, "back": horizon_days + window_days})
            bands = {r[0]: {"n": r[1], "churned": r[2],
                            "churn_rate": round(r[2] / r[1], 3) if r[1] else None}
                     for r in cur.fetchall()}
    finally:
        conn.close()

    high = bands.get("high", {"n": 0, "churned": 0, "churn_rate": None})
    low = bands.get("low", {"n": 0, "churned": 0, "churn_rate": None})
    verdict = []
    if not bands:
        verdict.append(f"No snapshots aged {horizon_days}–"
                       f"{horizon_days + window_days} days yet — calibration "
                       f"needs the nightly scorer running for a month.")
    else:
        if high["n"]:
            verdict.append(f"High-band precision {high['churn_rate']:.0%} "
                           f"(n={high['n']})"
                           + (" — many false alarms; consider raising HIGH_BAND "
                              "or the lateness weight."
                              if high["churn_rate"] is not None
                              and high["churn_rate"] < 0.3 else ""))
        if low["n"]:
            verdict.append(f"Low-band miss rate {low['churn_rate']:.0%} "
                           f"(n={low['n']})"
                           + (" — churners slipping through as 'safe'; consider "
                              "lowering MEDIUM_BAND."
                              if low["churn_rate"] is not None
                              and low["churn_rate"] > 0.2 else ""))
        if (high.get("churn_rate") is not None and low.get("churn_rate") is not None
                and high["churn_rate"] < low["churn_rate"]):
            verdict.append("⚠ Bands are INVERTED (low churns more than high) — "
                           "review the component weights.")
    return {"horizon_days": horizon_days, "window_days": window_days,
            "bands": bands, "verdict": verdict,
            "thresholds": {"high": HIGH_BAND, "medium": MEDIUM_BAND},
            "weights": {"lateness": W_LATENESS, "engagement": W_ENGAGEMENT,
                        "ar": W_AR, "lost_deal": W_LOST}}


def profile(account_id: str) -> Optional[Dict[str, Any]]:
    """Read one account's persisted profile (None if never scored)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT account_id::text, ltv, orders_12m, revenue_12m,
                          last_order_at, order_recency_days, typical_gap_days,
                          next_purchase_due, engagement_days, preferred_channel,
                          open_ar_balance, overdue_invoices, churn_risk,
                          churn_band, signals, computed_at
                   FROM account_intelligence WHERE account_id = %s::uuid""",
                (account_id,),
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None
    finally:
        conn.close()


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["intelligence"])


@router.get("/intelligence/status")
def intelligence_status():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT churn_band, count(*), COALESCE(SUM(ltv),0),
                                  MAX(computed_at)
                           FROM account_intelligence GROUP BY 1""")
            bands = {r[0]: {"accounts": r[1], "ltv": float(r[2]),
                            "last_computed": r[3].isoformat() if r[3] else None}
                     for r in cur.fetchall()}
    finally:
        conn.close()
    return {"enabled": ENABLED, "bands": bands,
            "weights": {"lateness": W_LATENESS, "engagement": W_ENGAGEMENT,
                        "ar": W_AR, "lost_deal": W_LOST},
            "thresholds": {"high": HIGH_BAND, "medium": MEDIUM_BAND}}


@router.get("/intelligence/account/{account_id}")
def intelligence_account(account_id: str):
    p = profile(account_id)
    return p or {"account_id": account_id, "profile": None,
                 "note": "not scored yet (no orders, or scorer has not run)"}


@router.get("/intelligence/at-risk")
def intelligence_at_risk(limit: int = 20):
    """High/medium-band customers, biggest LTV first — the save-list."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT i.account_id::text, a.account_name, i.churn_risk,
                          i.churn_band, i.ltv, i.order_recency_days,
                          i.typical_gap_days, i.preferred_channel, i.open_ar_balance
                   FROM account_intelligence i
                   JOIN accounts a ON a.account_id = i.account_id
                   WHERE i.churn_band IN ('high','medium')
                   ORDER BY i.churn_band = 'high' DESC, i.ltv DESC
                   LIMIT %s""",
                (limit,),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"count": len(rows), "at_risk": rows}


@router.get("/intelligence/calibration")
def intelligence_calibration(horizon_days: int = 30, window_days: int = 30):
    """How good were last month's churn predictions? (evidence for tuning)"""
    return calibrate(horizon_days, window_days)


@router.post("/intelligence/run-once")
async def intelligence_run_once(post_blackboard: bool = True,
                                start_sequences: bool = True):
    """Run a full scoring pass now (works even when INTEL_ENABLED=0)."""
    return await asyncio.to_thread(run_scoring_sync, post_blackboard, start_sequences)
