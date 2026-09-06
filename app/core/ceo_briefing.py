"""
CEO Morning Briefing — a strategic, action-oriented daily email.

Answers "What requires my attention today to maximize growth and reduce risk?"
— not "what happened yesterday". Real CRM data only (no fabricated metrics).

Design notes:
  • Recipient is configured via CEO_BRIEFING_EMAIL — the CEO is an internal
    stakeholder, so their address lives in env config, NEVER in accounts/contacts.
  • This is an INTERNAL admin email; it does NOT go through the customer
    email-verification gate (_is_real_email) used by dunning/order emails, and is
    independent of AGENT_BUS_AUTOSEND.
  • Gated by CEO_BRIEFING_ENABLED; scheduled daily 08:00 ET from app/main.py.
  • Admin endpoints /ceo-briefing/{preview,send-now} for review before enabling.
"""
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import execute_sp, get_connection

logger = logging.getLogger("ceo_briefing")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED   = _flag("CEO_BRIEFING_ENABLED")
RECIPIENT = (os.getenv("CEO_BRIEFING_EMAIL", "") or "").strip()


def _money(n) -> str:
    try:
        return "${:,.0f}".format(float(n or 0))
    except Exception:
        return "$0"


def _rows(cur, sql: str, params=None) -> List[tuple]:
    cur.execute(sql, params or ())
    return cur.fetchall()


def _one(cur, sql: str, params=None):
    cur.execute(sql, params or ())
    r = cur.fetchone()
    return r if r else ()


# ── Metric Registry bridge ───────────────────────────────────────────────────
# The briefing must never restate a registered metric. It builds its SQL from
# the registry's OWN fragments and runs them on the cursor it already holds, so
# every figure in one briefing comes from a single database snapshot (metrics.
# compute() would open a separate connection and transaction per metric).

from app.core import metrics as _metrics   # noqa: E402  (after helpers, before use)

_WON_COND = _metrics.WIN_NUM_COND                      # o.status = 'closed_won'
_DECIDED_LAST_7D = _metrics.window_predicate("won_revenue", "last_7d")


def _registry_value(name: str, window: str, cur) -> float:
    """Compute a registered metric on THIS cursor. Falls back to 0.0 rather than
    breaking the whole briefing if the metric or schema is unavailable."""
    try:
        sql, _ = _metrics.compute_sql(name, window)
        row = _one(cur, sql)
        return round(float(row[0] or 0), 2) if row else 0.0
    except Exception as exc:
        logger.warning(f"[ceo_briefing] registry metric '{name}' failed: {exc}")
        return 0.0


# ── Metric snapshot (powers "▲ vs yesterday" deltas + trend history) ────────────
# key, label, unit, higher_is_better, importance
_METRICS = [
    ("captured_7d",       "Captured (7d)",       "usd",   True,  9),
    ("revenue_at_risk",   "Revenue at risk",     "usd",   False, 10),
    ("forecast_30d",      "Likely to close 30d", "usd",   True,  9),
    ("advocates_7d",      "New advocates (7d)",  "count", True,  6),
    ("pipeline",          "Active pipeline",     "usd",   True,  7),
    ("weighted_forecast", "Weighted forecast",   "usd",   True,  7),
    ("overdue_ar",        "Overdue AR",          "usd",   False, 8),
    ("slipped_value",     "Slipped deals",       "usd",   False, 7),
    ("new_leads_7d",      "New leads (7d)",      "count", True,  5),
    ("overdue_activities","Overdue activities",  "count", False, 6),
    ("open_opps",         "Open opportunities",  "count", True,  7),
    ("won_7d",            "Won deals (7d)",      "usd",   True,  8),
    ("email_sentiment_7d","Email sentiment (7d)","score", True,  6),
    ("csat_proxy_30d",    "Customer satisfaction (30d)","score", True, 7),
    ("conv_neg_7d",       "Negative conversations (7d)","count", False, 6),
    ("low_stock_count",   "Low-stock products",  "count", False, 6),
]
_HIB = {k: hib for (k, _l, _u, hib, _i) in _METRICS}


def _metric_values(d: Dict[str, Any]) -> Dict[str, float]:
    vals = {
        "captured_7d":        float(d["rev_7d"] or 0),
        "revenue_at_risk":    float(d["ar_amt"] or 0) + float(d["slipped_amt"] or 0),
        "forecast_30d":       float(d["close_weighted"] or 0),
        "advocates_7d":       float(d["advocates"] or 0),
        "pipeline":           float(d["pipeline"] or 0),
        "weighted_forecast":  float(d["weighted"] or 0),
        "overdue_ar":         float(d["ar_amt"] or 0),
        "slipped_value":      float(d["slipped_amt"] or 0),
        "new_leads_7d":       float(d.get("new_leads_7d") or 0),
        "overdue_activities": float(d.get("overdue_acts") or 0),
        "open_opps":          float(d.get("open_cnt") or 0),
        "won_7d":             float(d.get("won_amt") or 0),
    }
    # Only record sentiment once there's real inbound mail to score.
    if d.get("sentiment_7d") is not None:
        vals["email_sentiment_7d"] = float(d["sentiment_7d"])
    # Cross-channel sentiment (interaction_memories) + inventory — only when
    # there's data to stand on.
    if d.get("csat_proxy") is not None:
        vals["csat_proxy_30d"] = float(d["csat_proxy"])
    if d.get("conv_n_7d"):
        vals["conv_neg_7d"] = float(d.get("conv_neg_7d") or 0)
    if d.get("low_stock") is not None:
        vals["low_stock_count"] = float(d["low_stock"])
    return vals


def _previous_metrics(cur) -> Dict[str, float]:
    """Metrics from the latest snapshot strictly BEFORE today (for deltas)."""
    cur.execute(
        "SELECT m.metric_key, m.value FROM executive_metric m "
        "JOIN executive_snapshot s ON s.snapshot_id = m.snapshot_id "
        "WHERE s.period_type='daily' AND s.snapshot_date = ("
        "  SELECT MAX(snapshot_date) FROM executive_snapshot "
        "  WHERE period_type='daily' AND snapshot_date < CURRENT_DATE)")
    return {k: float(v) for k, v in cur.fetchall() if v is not None}


def _compute_deltas(values: Dict[str, float], prev: Dict[str, float]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in values.items():
        pv = prev.get(k)
        out[k] = {"abs": None, "pct": None} if pv is None else \
                 {"abs": v - pv, "pct": ((v - pv) / pv * 100.0) if pv else None}
    return out


def _persist_snapshot(cur, values, deltas, summary) -> None:
    """Upsert today's daily snapshot + its metric rows (idempotent per day)."""
    cur.execute(
        "INSERT INTO executive_snapshot (snapshot_date, period_type, summary_text) "
        "VALUES (CURRENT_DATE, 'daily', %s) "
        "ON CONFLICT (snapshot_date, period_type) "
        "DO UPDATE SET summary_text=EXCLUDED.summary_text, created_at=now() RETURNING snapshot_id",
        (summary,))
    sid = cur.fetchone()[0]
    meta = {k: (lbl, unit, hib, imp) for (k, lbl, unit, hib, imp) in _METRICS}
    for k, v in values.items():
        m = meta.get(k, (k, "", True, 0)); dd = deltas.get(k, {})
        cur.execute(
            "INSERT INTO executive_metric (snapshot_id, metric_key, value, unit, delta_abs, delta_pct, importance) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (snapshot_id, metric_key) "
            "DO UPDATE SET value=EXCLUDED.value, delta_abs=EXCLUDED.delta_abs, delta_pct=EXCLUDED.delta_pct",
            (sid, k, v, m[1], dd.get("abs"), dd.get("pct"), m[3]))


def _delta_html(deltas, key) -> str:
    dd = (deltas or {}).get(key) or {}
    pct = dd.get("pct")
    if pct is None or abs(pct) < 0.1:
        return ""
    up = pct > 0
    color = "#16a34a" if (up == _HIB.get(key, True)) else "#dc2626"
    return f' <span style="color:{color};font-size:11px;font-weight:700;">{"▲" if up else "▼"} {abs(pct):.1f}%</span>'


def _delta_text(deltas, key) -> str:
    dd = (deltas or {}).get(key) or {}
    pct = dd.get("pct")
    if pct is None or abs(pct) < 0.1:
        return ""
    return f" ({'+' if pct > 0 else '-'}{abs(pct):.1f}% vs prev)"


# ── Data gathering ──────────────────────────────────────────────────────────────

def gather() -> Dict[str, Any]:
    """Pull the strategic numbers from real CRM tables.

    ONE TRANSACTION, ONE SNAPSHOT. Sharing a connection is not enough: Postgres
    defaults to READ COMMITTED, where every STATEMENT takes a fresh snapshot.
    Measured — two counts in one READ COMMITTED transaction returned 1001 then
    1002 with a concurrent insert between them. A briefing built that way reports
    a dozen figures from a dozen database states under a single `as_of`
    timestamp, which is exactly the internal inconsistency this was supposed to
    fix. REPEATABLE READ pins one snapshot for the whole gather.

    Safe here because gather() is strictly read-only: a serialization failure
    cannot occur without writes, and a repeatable-read reader never blocks a
    writer."""
    conn = get_connection()
    try:
        try:
            conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        except Exception as exc:      # pre-existing txn / unsupported driver
            logger.debug(f"[briefing] snapshot isolation unavailable: {exc}")
        with conn.cursor() as cur:
            discount_pressure = _discount_pressure(cur)
            rev_yest = _one(cur, "SELECT COALESCE(SUM(amount),0) FROM payments "
                                 "WHERE payment_date::date = CURRENT_DATE - 1")[0]
            rev_7d = _one(cur, "SELECT COALESCE(SUM(amount),0) FROM payments "
                               "WHERE payment_date::date >= CURRENT_DATE - 7")[0]
            # Most recent day that actually had revenue — so a gap day (weekend /
            # generator didn't run) shows the last active day instead of a bare $0.
            _recent = _one(cur, "SELECT payment_date::date, COALESCE(SUM(amount),0) "
                                "FROM payments GROUP BY payment_date::date "
                                "ORDER BY 1 DESC LIMIT 1")
            rev_recent_date = _recent[0] if _recent else None
            rev_recent_amt  = _recent[1] if _recent else 0

            pipeline, weighted, open_cnt = _one(cur,
                "SELECT COALESCE(SUM(amount),0), "
                "       COALESCE(SUM(amount*COALESCE(probability,0)/100.0),0), COUNT(*) "
                "FROM opportunities WHERE status='open'")

            # Forecast horizon = next 30 days (B2B deals rarely close within 7).
            close_amt, close_weighted, close_cnt = _one(cur,
                "SELECT COALESCE(SUM(amount),0), "
                "       COALESCE(SUM(amount*COALESCE(probability,0)/100.0),0), COUNT(*) "
                "FROM opportunities WHERE status='open' "
                "  AND close_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 30")

            ar_amt, ar_cnt = _one(cur,
                "SELECT COALESCE(SUM(computed_balance_due),0), COUNT(*) "
                "FROM accounting_invoice_pipeline "
                "WHERE payment_status IN ('unpaid','partial') AND due_date::date < CURRENT_DATE")

            slipped_amt, slipped_cnt = _one(cur,
                "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM opportunities "
                "WHERE status='open' AND close_date < CURRENT_DATE")

            # Decision axis, not updated_at — see the won_amt note below.
            advocates = _one(cur,
                "SELECT COUNT(DISTINCT account_id) FROM opportunities o "
                f"WHERE {_WON_COND} AND {_DECIDED_LAST_7D}")[0]

            new_leads_7d = _one(cur, "SELECT COUNT(*) FROM leads "
                                     "WHERE created_at >= now() - interval '7 days'")[0]
            overdue_acts = _one(cur, "SELECT COUNT(*) FROM activities "
                                     "WHERE status='open' AND due_at < now()")[0]
            _sent = _one(cur, "SELECT AVG(score), COUNT(*) FROM email_sentiment "
                              "WHERE received_at >= now() - interval '7 days'")
            sentiment_7d = float(_sent[0]) if _sent and _sent[0] is not None else None
            sentiment_n  = int(_sent[1]) if _sent else 0

            # Cross-channel sentiment: every distilled conversation (voice,
            # SMS, chat, email, whatsapp) carries a sentiment label in
            # interaction_memories — the customer-health pulse.
            try:
                _x = _one(cur,
                    "SELECT COUNT(*) FILTER (WHERE sentiment='negative'), "
                    "       COUNT(*) FILTER (WHERE sentiment='positive'), "
                    "       COUNT(*) FILTER (WHERE sentiment IS NOT NULL) "
                    "FROM interaction_memories "
                    "WHERE created_at >= now() - interval '7 days'")
                conv_neg_7d, conv_pos_7d, conv_n_7d = (
                    int(_x[0] or 0), int(_x[1] or 0), int(_x[2] or 0))
                _c = _one(cur,
                    "SELECT COUNT(*) FILTER (WHERE sentiment <> 'negative'), "
                    "       COUNT(*) FROM interaction_memories "
                    "WHERE sentiment IS NOT NULL "
                    "AND created_at >= now() - interval '30 days'")
                csat_proxy = (round(100.0 * _c[0] / _c[1], 1)
                              if _c and _c[1] else None)
            except Exception:
                conv_neg_7d = conv_pos_7d = conv_n_7d = 0
                csat_proxy = None

            # Inventory risk: active products at/below the stock floor OR
            # short of open-order demand (same rule as the supervisor's
            # inventory_risk detector).
            try:
                _floor = int(os.getenv("SUPERVISOR_LOWSTOCK_FLOOR", "5"))
                low_stock = int(_one(cur,
                    "SELECT COUNT(*) FROM products p WHERE p.is_active "
                    "AND (p.stock_quantity <= %s OR p.stock_quantity < "
                    "COALESCE((SELECT SUM(oi.quantity)::int FROM order_items oi "
                    "JOIN orders o ON o.order_id = oi.order_id "
                    "WHERE oi.product_id = p.product_id "
                    "AND lower(o.status) IN ('pending','processing')), 0))",
                    (_floor,))[0] or 0)
            except Exception:
                low_stock = None

            # THE registry's won_revenue over last_7d — not a local restatement.
            # This used to be `status='closed_won' AND updated_at >= now()-7d`,
            # i.e. every deal EDITED in the last week regardless of when it was
            # won. On real data that reported $5,276,400 where the registry says
            # $402,101 — 13x, to the highest-stakes reader in the product
            # (audit re-verification, 2026-07-30).
            won_amt = _registry_value("won_revenue", "last_7d", cur)

            closing = _rows(cur,
                "SELECT o.name, COALESCE(a.account_name,'—'), ROUND(o.amount::numeric,2), "
                "       COALESCE(o.probability,0), o.close_date "
                "FROM opportunities o LEFT JOIN accounts a ON a.account_id=o.account_id "
                "WHERE o.status='open' AND o.close_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 30 "
                "ORDER BY o.amount DESC LIMIT 5")

            biggest = _rows(cur,
                "SELECT o.name, COALESCE(a.account_name,'—'), ROUND(o.amount::numeric,2), "
                "       o.stage, COALESCE(o.probability,0) "
                "FROM opportunities o LEFT JOIN accounts a ON a.account_id=o.account_id "
                "WHERE o.status='open' AND COALESCE(o.amount,0) > 0 "
                "ORDER BY o.amount DESC NULLS LAST LIMIT 5")

            atrisk = _rows(cur,
                "SELECT o.name, COALESCE(a.account_name,'—'), ROUND(o.amount::numeric,2), "
                "       (CURRENT_DATE - o.close_date) AS days "
                "FROM opportunities o LEFT JOIN accounts a ON a.account_id=o.account_id "
                "WHERE o.status='open' AND o.close_date < CURRENT_DATE "
                "ORDER BY o.amount DESC LIMIT 5")

            big_inv = _rows(cur,
                "SELECT v.invoice_number, COALESCE(a.account_name,'—'), "
                "       ROUND(v.computed_balance_due::numeric,2), (CURRENT_DATE - v.due_date::date) AS days "
                "FROM accounting_invoice_pipeline v LEFT JOIN accounts a ON a.account_id=v.account_id "
                "WHERE v.payment_status IN ('unpaid','partial') AND v.due_date::date < CURRENT_DATE "
                "ORDER BY v.computed_balance_due DESC LIMIT 3")
        # Governance approvals awaiting a decision (routed to executives),
        # with the independent critic's verdict when the migration exists.
        # Best-effort: tolerate the governance/routing/critic migrations not existing.
        approvals = []
        for cols in ("       COALESCE(amount,0), created_at::date, "
                     "       critique->>'stance', critique->>'summary' ",
                     "       COALESCE(amount,0), created_at::date "):
            try:
                with conn.cursor() as cur2:
                    # No expires_at filter: a breached proposal is the most
                    # important line in this section, not the one hidden from it.
                    approvals = _rows(cur2,
                        "SELECT action_type, proposed_by, COALESCE(assigned_to,'unassigned'), "
                        + cols +
                        "FROM action_approvals "
                        "WHERE status='pending' "
                        "ORDER BY COALESCE(amount,0) DESC, created_at LIMIT 5")
                break
            except Exception as exc:
                logger.warning(f"[ceo_briefing] approvals query fallback: {exc}")
                conn.rollback()
        # Governance today (activation §21) — the numbers leadership must see or
        # decide on. Own connections; best-effort.
        governance_today = None
        try:
            from app.core import governance as _gov, governance_alerts as _galerts
            governance_today = {"approvals": _gov.metrics(), "alerts": _galerts.metrics(),
                                "escalated": [], "breached": [], "due_soon": []}
            for p in _gov.pending():
                item = {"action_type": p.get("action_type"), "authority": p.get("authority_role"),
                        "owner": p.get("accountable_owner"), "due_at": p.get("due_at"),
                        "hours_left": p.get("hours_left"), "id": p.get("approval_uuid")}
                if p.get("escalation_status") == "escalated":
                    governance_today["escalated"].append(item)
                elif p.get("escalation_status") == "breached":
                    governance_today["breached"].append(item)
                elif (p.get("hours_left") is not None and 0 <= p["hours_left"] <= 12):
                    governance_today["due_soon"].append(item)
            governance_today["critical_alerts"] = [
                {"headline": a.get("headline"), "owner": a.get("accountable_owner"),
                 "status": a.get("status"), "hours_left": a.get("hours_left")}
                for a in _galerts.list_alerts()
                if a.get("severity") == "critical" or a.get("status") == "escalated"][:8]
        except Exception as exc:
            logger.warning(f"[ceo_briefing] governance section skipped: {exc}")
        # Agent performance report card (learning loop) — best-effort.
        try:
            from app.core.learning import agent_performance
            perf = agent_performance(30)
        except Exception as exc:
            logger.warning(f"[ceo_briefing] agent performance skipped: {exc}")
            perf = None
        # Business objectives (goal-oriented supervisor) — best-effort.
        try:
            from app.core.objectives import report as objectives_report
            objectives = objectives_report()
        except Exception as exc:
            logger.warning(f"[ceo_briefing] objectives skipped: {exc}")
            objectives = []
        # Analytics trend anomalies (A4) — the proactive push of A1's detectors
        # into this daily briefing (win-rate WoW drop, stalled deals, revenue
        # slump). Best-effort: a failure here never breaks the briefing.
        try:
            from app.core import analytics_signals
            anomalies = analytics_signals.detect_all()
        except Exception as exc:
            logger.warning(f"[ceo_briefing] anomalies skipped: {exc}")
            anomalies = []
        # What this briefing is ALLOWED to claim. The snapshot makes every
        # figure describe one moment of the DATABASE; it says nothing about
        # whether the database reflects the BUSINESS. If a source is behind its
        # SLA, the briefing has to say so rather than imply currency it lacks.
        try:
            from app.core.data_sources import as_of_qualifier
            source_caveat = as_of_qualifier()
        except Exception as exc:
            logger.debug(f"[ceo_briefing] source freshness unavailable: {exc}")
            source_caveat = None

        return {
            "source_caveat": source_caveat,
            "rev_yest": rev_yest, "rev_7d": rev_7d,
            "rev_recent_date": rev_recent_date, "rev_recent_amt": rev_recent_amt,
            "pipeline": pipeline, "weighted": weighted, "open_cnt": open_cnt,
            "close_amt": close_amt, "close_weighted": close_weighted, "close_cnt": close_cnt,
            "ar_amt": ar_amt, "ar_cnt": ar_cnt, "slipped_amt": slipped_amt, "slipped_cnt": slipped_cnt,
            "advocates": advocates, "won_amt": won_amt,
            "new_leads_7d": new_leads_7d, "overdue_acts": overdue_acts,
            "sentiment_7d": sentiment_7d, "sentiment_n": sentiment_n,
            "conv_neg_7d": conv_neg_7d, "conv_pos_7d": conv_pos_7d,
            "conv_n_7d": conv_n_7d, "csat_proxy": csat_proxy,
            "low_stock": low_stock,
            "closing": closing, "biggest": biggest, "atrisk": atrisk, "big_inv": big_inv,
            "approvals": approvals, "perf": perf, "objectives": objectives,
            "governance": governance_today,
            "discount_pressure": discount_pressure,
            "anomalies": anomalies,
        }
    finally:
        conn.close()


def _decision(d: Dict[str, Any]) -> str:
    """The single most important decision: biggest at-risk deal vs biggest overdue invoice."""
    deal = d["atrisk"][0] if d["atrisk"] else None
    inv  = d["big_inv"][0] if d["big_inv"] else None
    deal_amt = float(deal[2]) if deal else 0
    inv_amt  = float(inv[2])  if inv  else 0
    if deal_amt == 0 and inv_amt == 0:
        return "No critical risk today — focus on advancing the largest open deal."
    if deal_amt >= inv_amt:
        return (f"Re-engage **{deal[1]}** on the slipped deal “{deal[0]}” "
                f"({_money(deal[2])}, {deal[3]} days past close) before it decays.")
    return (f"Chase **{inv[1]}** on overdue invoice {inv[0]} "
            f"({_money(inv[2])}, {inv[3]} days overdue).")


# ── Live web intelligence (app/core/web_tools.py — ddgs free, Tavily fallback) ──
# One external-market topic per role, fetched once per day (in-process cache).
# The briefing NEVER fails on web errors — the section is simply omitted.
_WEB_TOPICS = {
    "CEO": ("Market &amp; External Intelligence",
            "CRM software industry news and market trends this week"),
    "CFO": ("Market Rates &amp; Finance Watch",
            "current Bank of Canada interest rate and CAD to USD exchange rate"),
    "CRO": ("Market &amp; Competitive Watch",
            "B2B sales trends and enterprise software buying news this week"),
    "COO": ("Operations &amp; Supply-Chain Watch",
            "supply chain and business operations news this week"),
}
_web_cache: Dict[str, Any] = {}


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or url).replace("www.", "")
    except Exception:
        return url


def _web_intel(role: str):
    """Fetch the role's daily external topic from the live web.
    Returns {'title', 'body_md', 'sources': [urls]} or None (never raises)."""
    cfg = _WEB_TOPICS.get(role)
    if not cfg:
        return None
    title, topic = cfg
    key = f"{role}:{datetime.now().strftime('%Y-%m-%d')}"
    if key in _web_cache:
        return _web_cache[key]
    result = None
    try:
        from app.core.web_tools import web_answer
        md = web_answer(topic) or ""
        if md and "couldn't reach" not in md:
            import re as _re
            lines = md.split("\n")
            src_at = next((i for i in range(len(lines) - 1, -1, -1)
                           if _re.match(r"^\s*(\*\*)?sources?:?", lines[i], _re.IGNORECASE)), -1)
            body = "\n".join(lines[:src_at]).strip() if src_at >= 0 else md.strip()
            src_block = "\n".join(lines[src_at:]) if src_at >= 0 else ""
            urls = [u.rstrip(".,;") for u in _re.findall(r"https?://[^\s)>\]]+", src_block)]
            if body:
                result = {"title": title, "body_md": body, "sources": urls[:4]}
    except Exception as exc:
        logger.warning(f"[ceo_briefing] web intel fetch failed for {role}: {exc}")
    _web_cache[key] = result
    return result


def _web_intel_html(web, INK, MUTE, ACCENT) -> str:
    """Markdown body + source links → email-safe inline-CSS HTML."""
    import re as _re
    items, blocks = [], []
    for ln in (web.get("body_md") or "").split("\n"):
        s = ln.strip()
        if not s:
            continue
        s = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = _re.sub(r"^#{1,3}\s+", "", s)
        if s.startswith(("- ", "* ")) or s.startswith("• "):
            items.append(f'<li style="margin:4px 0;">{s.lstrip("-*• ").strip()}</li>')
        else:
            blocks.append(f'<div style="margin:4px 0;">{s}</div>')
    html = "".join(blocks)
    if items:
        html += f'<ul style="margin:4px 0 0;padding-left:18px;">{"".join(items)}</ul>'
    if web.get("sources"):
        links = " &nbsp;&middot;&nbsp; ".join(
            f'<a href="{u}" style="color:{ACCENT};text-decoration:none;font-weight:600;">{_host(u)}</a>'
            for u in web["sources"])
        html += f'<div style="font-size:11px;color:{MUTE};margin-top:8px;">Sources: {links}</div>'
    return f'<div style="font-size:13px;line-height:1.5;color:{INK};">{html}</div>'


def _web_intel_text(web) -> List[str]:
    out = [ln.strip() for ln in (web.get("body_md") or "").split("\n") if ln.strip()]
    lines = [f"   {ln}" for ln in out]
    if web.get("sources"):
        lines.append("   Sources: " + ", ".join(web["sources"]))
    return lines


# ── Rendering ───────────────────────────────────────────────────────────────────

_CRITIC_ICON = {"endorse": "✅", "caution": "⚠️", "object": "⛔"}
_CRITIC_COLOR = {"endorse": "#1e7c45", "caution": "#a8720a", "object": "#a33a3a"}


def _critic_text(r) -> str:
    """Approval row → critic verdict line ('' when the row carries none)."""
    if len(r) > 6 and r[5]:
        return f"{_CRITIC_ICON.get(r[5], '•')} critic {r[5].upper()}: {r[6]}"
    return ""


def _critic_html(r) -> str:
    if len(r) > 6 and r[5]:
        col = _CRITIC_COLOR.get(r[5], "#7b8497")
        return (f'<br><span style="color:{col};font-size:12px;">'
                f'{_CRITIC_ICON.get(r[5], "•")} critic {r[5]}: {r[6]}</span>')
    return ""


def _objective_lines(objs: List[Any]) -> List[str]:
    """Business objectives → compact human lines (shared by text + HTML)."""
    out: List[str] = []
    for o in objs or []:
        if o.get("status") == "achieved":
            out.append(f"{o['name']}: ACHIEVED (target {o['target']:g})")
            continue
        if o.get("value") is None:
            continue
        st = (o.get("status") or "").replace("_", " ").upper()
        trend = f", {o['trend']}" if o.get("trend") else ""
        unit = "%" if o.get("unit") == "%" else ""
        out.append(f"{o['name']}: {o['value']:g}{unit} vs expected "
                   f"{o.get('expected', o['target']):g} → target {o['target']:g}"
                   f" · {st}{trend}")
    return out


def _discount_pressure(cur) -> Dict[str, Any]:
    """Quotes whose requested discount was cut by the brand policy — C3.1.

    Deliberately a REPORT, not a proposal. The governance queue's dominant
    outcome is expiry (17 expired vs 5 executed at the time of writing), so
    routing every over-ask into it would add noise to a backlog nobody
    finishes. A sales leader who keeps seeing "we cut this one" is the signal
    that a real exception path is warranted; until then, visibility is the
    honest intervention.

    Returns {} when the C3.0 table is absent, so a database without the
    migration degrades to today's briefing exactly."""
    try:
        cur.execute("SELECT to_regclass('public.quotes')")
        if not cur.fetchone()[0]:
            return {}
        cur.execute("""
            SELECT count(*)                                   AS clamped,
                   COALESCE(sum(total), 0)                    AS value,
                   COALESCE(max(discount_pct_requested), 0)   AS worst_ask,
                   COALESCE(max(discount_cap_pct), 0)         AS cap
            FROM quotes
            WHERE discount_clamped
              AND created_at > now() - interval '7 days'""")
        n, value, worst, cap = cur.fetchone()
        if not n:
            return {}
        return {"clamped": int(n), "value": float(value or 0),
                "worst_ask": float(worst or 0), "cap": float(cap or 0)}
    except Exception as exc:
        logger.debug(f"[briefing] discount pressure skipped: {exc}")
        return {}


def _discount_lines(dp: Dict[str, Any]) -> List[str]:
    """One line, stated as a commercial fact rather than a policy violation:
    somebody judged the deal needed more room than the brand allows."""
    if not dp or not dp.get("clamped"):
        return []
    return [f"💸 {dp['clamped']} quote(s) worth {_money(dp['value'])} went out "
            f"with the discount cut by policy (largest ask {dp['worst_ask']:.0f}% "
            f"vs {dp['cap']:.0f}% cap) — review whether any deserved an "
            f"exception."]


def _anomaly_lines(anoms: List[Dict[str, Any]]) -> List[str]:
    """Analytics trend anomalies → compact human lines (shared by text + HTML).
    A4: the proactive push of A1's detectors (win-rate WoW drop, stalled deals,
    revenue slump) into the daily executive briefing — insight at the point of
    decision, with the recommended action attached."""
    icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    order = {"high": 0, "medium": 1, "low": 2}
    out: List[str] = []
    for s in sorted(anoms or [], key=lambda x: order.get(x.get("severity"), 3)):
        out.append(f"{icon.get(s.get('severity'), '•')} {s.get('headline')} "
                   f"→ {s.get('recommended_action')}")
    return out


def _governance_lines(g: Optional[Dict[str, Any]]) -> List[str]:
    """The fourteen questions of activation §21, as briefing lines. Only lines
    with something to say are emitted; a fully quiet day says so in one line."""
    if not g:
        return []
    a = g.get("approvals") or {}
    al = g.get("alerts") or {}
    out: List[str] = []
    if not a.get("available"):
        return ["governance metrics unavailable (activation migration not applied?)"]
    if a.get("pending"):
        by = ", ".join(f"{k} {v}" for k, v in (a.get("pending_by_authority") or {}).items())
        out.append(f"Approvals requiring attention: {a['pending']}" + (f" ({by})" if by else ""))
    if g.get("due_soon"):
        out.append("Approaching 48h: " + "; ".join(
            f"{i['action_type']} → {i['authority']} ({i['hours_left']}h left)" for i in g["due_soon"][:6]))
    if a.get("breached_open") or a.get("escalated_open"):
        out.append(f"SLA BREACHES: {a.get('breached_open', 0) + a.get('escalated_open', 0)} "
                   f"pending past 48h ({a.get('breached_7d', 0)} breached this week, "
                   f"breach rate {a.get('breach_rate_7d_pct', 0)}%)")
    if g.get("escalated"):
        out.append("CEO ESCALATIONS awaiting decision: " + "; ".join(
            f"{i['action_type']} (was {i['authority']}, {i['id'][:8]})" for i in g["escalated"][:8]))
    if a.get("pending_ownership_exceptions") or al.get("ownership_exceptions"):
        out.append(f"Unowned consequential work (CEO holding as exception): "
                   f"{a.get('pending_ownership_exceptions', 0)} approvals, "
                   f"{al.get('ownership_exceptions', 0)} alerts")
    if a.get("stranded"):
        out.append(f"STRANDED executions needing a human look: {a['stranded']}")
    if al.get("available"):
        if al.get("critical") or al.get("escalated"):
            out.append(f"Critical/escalated alerts open: {al.get('critical', 0)} critical, "
                       f"{al.get('escalated', 0)} escalated")
        for c in (g.get("critical_alerts") or [])[:5]:
            out.append(f"   · {c['headline']} — owner {c.get('owner') or '?'} · {c.get('status')}")
    out.append(f"AI actions last 24h: {a.get('approved_24h', 0)} human-approved, "
               f"{a.get('auto_executed_24h', 0)} under standing policy, "
               f"{a.get('rejected_24h', 0)} rejected, {a.get('failed_24h', 0)} failed, "
               f"{a.get('pending', 0)} awaiting approval")
    if a.get("verification_failed_7d"):
        out.append(f"Governance exceptions: {a['verification_failed_7d']} executed action(s) "
                   f"this week whose effect could not be verified")
    if a.get("median_decision_hours") is not None:
        out.append(f"Decision time (30d): median {a['median_decision_hours']}h · "
                   f"p95 {a.get('p95_decision_hours')}h")
    return out


def _perf_lines(perf: Dict[str, Any]) -> List[str]:
    """Agent report card → compact human lines (shared by text + HTML)."""
    if not perf:
        return []
    out: List[str] = []
    for pb, d in sorted((perf.get("cadences") or {}).items()):
        mix = ", ".join(f"{o} {n}" for o, n in sorted(d["outcomes"].items()))
        rate = (f"{d['success_rate']:.0%}" if d.get("success_rate") is not None
                else "n/a")
        out.append(f"{pb}: {d['ended']} play(s) ended · success {rate} ({mix})")
    c = perf.get("campaigns") or {}
    if c.get("launched"):
        sends = ", ".join(f"{k} {v}" for k, v in sorted((c.get("sends") or {}).items()))
        out.append(f"campaigns: {c['launched']} launched · {sends or 'no sends'} · "
                   f"{c['accounts_replied']} account(s) replied · "
                   f"{c['orders']} order(s) ({_money(c['order_value'])})")
    for v in (perf.get("churn_calibration") or {}).get("verdict") or []:
        out.append(f"churn model: {v}")
    out.extend(perf.get("ai_spend") or [])
    return out


def render(d: Dict[str, Any], deltas: Dict[str, Any] = None) -> Dict[str, str]:
    today = datetime.now().strftime("%B %-d, %Y") if os.name != "nt" else datetime.now().strftime("%B %d, %Y")
    at_risk_total = float(d["ar_amt"] or 0) + float(d["slipped_amt"] or 0)
    decision = _decision(d)
    web = _web_intel("CEO")

    # Card #1 — "captured": yesterday if it had revenue, else the most recent
    # active day (so a gap day shows real revenue, not a bare $0).
    if float(d["rev_yest"] or 0) > 0:
        cap_label, cap_val = "Captured yesterday", _money(d["rev_yest"])
        cap_sub = f"{_money(d['rev_7d'])} last 7 days"
    else:
        rd = d.get("rev_recent_date")
        rd_s = (f"{rd.strftime('%b')} {rd.day}" if rd else "recent")
        cap_label, cap_val = f"Captured {rd_s}", _money(d.get("rev_recent_amt"))
        cap_sub = f"{_money(d['rev_7d'])} last 7d · $0 yesterday"

    # ── plain text ──
    t: List[str] = []
    t.append(f"MORNING CEO BRIEFING — {today}")
    t.append("")
    t.append("THE FIVE NUMBERS")
    t.append(f"  1. {cap_label:<24}: {cap_val}  ({cap_sub}){_delta_text(deltas,'captured_7d')}")
    t.append(f"  2. Revenue at risk           : {_money(at_risk_total)}  "
             f"({_money(d['ar_amt'])} overdue AR + {_money(d['slipped_amt'])} slipped deals){_delta_text(deltas,'revenue_at_risk')}")
    t.append(f"  3. Likely to close (30d)     : {_money(d['close_weighted'])} weighted "
             f"({_money(d['close_amt'])} gross, {d['close_cnt']} deals){_delta_text(deltas,'forecast_30d')}")
    t.append(f"  4. New advocates (won, 7d)   : {d['advocates']} accounts ({_money(d['won_amt'])}){_delta_text(deltas,'advocates_7d')}")
    t.append(f"  5. #1 decision today         : {decision}")
    t.append("")
    anom_lines = _anomaly_lines(d.get("anomalies"))
    if anom_lines:
        t.append("⚠ WHAT CHANGED — TREND ANOMALIES (auto-detected)")
        for ln in anom_lines:
            t.append(f"   - {ln}")
        t.append("")
    disc_lines = _discount_lines(d.get("discount_pressure"))
    if disc_lines:
        t.append("💸 DISCOUNT PRESSURE (last 7 days)")
        for ln in disc_lines:
            t.append(f"   - {ln}")
        t.append("")
    t.append("1. REVENUE SNAPSHOT")
    t.append(f"   Active pipeline      : {_money(d['pipeline'])} ({d['open_cnt']} open)")
    t.append(f"   Weighted forecast    : {_money(d['weighted'])}")
    t.append(f"   Closing next 30 days : {_money(d['close_amt'])} ({d['close_cnt']} deals)")
    t.append(f"   {cap_label:<20}: {cap_val} ({_money(d['rev_7d'])} last 7 days)")
    t.append("")
    t.append("2. REVENUE AT RISK")
    t.append(f"   Overdue AR: {_money(d['ar_amt'])} across {d['ar_cnt']} invoices")
    t.append(f"   Slipped deals: {_money(d['slipped_amt'])} across {d['slipped_cnt']} opportunities")
    for r in d["atrisk"]:
        t.append(f"     - {r[0]} ({r[1]}) — {_money(r[2])}, {r[3]}d past close")
    t.append("")
    t.append("3. LIKELY TO CLOSE — NEXT 30 DAYS")
    for r in d["closing"]:
        t.append(f"   - {r[0]} ({r[1]}) — {_money(r[2])} @ {int(r[3])}% · {r[4]}")
    if not d["closing"]:
        t.append("   (none scheduled to close in the next 30 days)")
    t.append("")
    t.append("4. GROWTH — BIGGEST DEALS IN PLAY")
    for r in d["biggest"]:
        t.append(f"   - {r[0]} ({r[1]}) — {_money(r[2])} · {r[3]} @ {int(r[4])}%")
    t.append("")
    t.append("5. CRITICAL EVENTS")
    for r in d["big_inv"]:
        flag = "  (large balance, 45d+)" if float(r[2]) > 25000 and int(r[3]) >= 45 else ""
        t.append(f"   - Overdue invoice {r[0]} — {r[1]}: {_money(r[2])}, {r[3]}d overdue{flag}")
    t.append("")
    if d.get("approvals"):
        t.append("APPROVALS AWAITING DECISION (governance queue)")
        for r in d["approvals"]:
            amt = f" — {_money(r[3])}" if float(r[3] or 0) else ""
            t.append(f"   - {r[0]}{amt} · proposed by {r[1]} · assigned to {r[2]} ({r[4]})")
            ct = _critic_text(r)
            if ct:
                t.append(f"       {ct}")
        t.append("")
    gov_lines = _governance_lines(d.get("governance"))
    if gov_lines:
        t.append("GOVERNANCE TODAY — what leadership must know or decide")
        for ln in gov_lines:
            t.append(f"   - {ln}")
        t.append("")
    obj_lines = _objective_lines(d.get("objectives"))
    if obj_lines:
        t.append("BUSINESS OBJECTIVES — what the agent fleet is pursuing")
        for ln in obj_lines:
            t.append(f"   - {ln}")
        t.append("")
    perf_lines = _perf_lines(d.get("perf"))
    if perf_lines:
        t.append("AGENT PERFORMANCE (30D) — is the automation earning its keep?")
        for ln in perf_lines:
            t.append(f"   - {ln}")
        t.append("")
    if web:
        t.append("6. MARKET & EXTERNAL INTELLIGENCE (LIVE WEB)")
        t.extend(_web_intel_text(web))
        t.append("")
    t.append(f"#1 CEO ACTION: {decision}")
    t.append("")
    t.append("— Conscestra CRM · the orchestration of customer intelligence")
    text = "\n".join(t).replace("**", "")

    # ── HTML (executive letterhead; email-client-safe tables + inline CSS) ──────
    NAVY, INK, MUTE, LINE, CARD, ACCENT = "#15233f", "#26304a", "#7b8497", "#e7ecf3", "#f7f9fc", "#b08a46"
    SANS = "Arial,Helvetica,sans-serif"

    def kpi(num, label, val, sub="", delta=""):
        return (f'<td width="25%" valign="top" style="background:{CARD};border:1px solid {LINE};'
                f'border-radius:8px;padding:12px 9px;font-family:{SANS};">'
                f'<div style="font-size:9px;color:{MUTE};text-transform:uppercase;letter-spacing:.01em;font-weight:700;white-space:nowrap;">{num} &middot; {label}</div>'
                f'<div style="font-size:21px;line-height:1.05;font-weight:700;color:{NAVY};margin-top:7px;">{val}{delta}</div>'
                f'<div style="font-size:11px;color:{MUTE};margin-top:5px;min-height:13px;">{sub}</div></td>')

    def lis(rows, fmt):
        body = "".join(f'<li style="margin:4px 0;">{fmt(r)}</li>' for r in rows) or f'<li style="color:{MUTE};">None.</li>'
        return f'<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5;color:{INK};">{body}</ul>'

    def section(title, inner):
        return (f'<tr><td style="padding:16px 28px 0;font-family:{SANS};">'
                f'<div style="font-size:12px;font-weight:700;color:{NAVY};text-transform:uppercase;letter-spacing:.06em;'
                f'border-bottom:2px solid {LINE};padding-bottom:6px;margin-bottom:9px;">{title}</div>{inner}</td></tr>')

    h: List[str] = []
    h.append(f'<div style="background:#eef1f6;padding:26px 12px;">')
    h.append('<table role="presentation" align="center" width="640" cellpadding="0" cellspacing="0" '
             'style="width:640px;max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #e1e6ef;border-radius:4px;">')
    # Letterhead
    h.append(f'<tr><td style="height:6px;background:{NAVY};border-top:3px solid {ACCENT};font-size:0;line-height:0;">&nbsp;</td></tr>')
    h.append(f'<tr><td style="padding:24px 28px 8px;">'
             f'<div style="font-family:Georgia,\'Times New Roman\',serif;font-size:12px;letter-spacing:.24em;'
             f'color:{ACCENT};font-weight:700;text-transform:uppercase;">Conscestra CRM</div>'
             f'<div style="font-family:Georgia,\'Times New Roman\',serif;font-size:25px;font-weight:700;color:{NAVY};margin-top:5px;">Morning Executive Briefing</div>'
             f'<div style="font-family:{SANS};font-size:12.5px;color:{MUTE};margin-top:5px;">{today} &nbsp;&middot;&nbsp; Prepared for the Office of the CEO</div>'
             f'</td></tr>')
    # KPI grid (4 across) + the one decision (full width)
    h.append(f'<tr><td style="padding:10px 20px 2px;font-family:{SANS};">')
    h.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="8" style="border-collapse:separate;"><tr>')
    h.append(kpi("1", cap_label, cap_val, cap_sub, _delta_html(deltas, "captured_7d")))
    h.append(kpi("2", "Revenue at risk", _money(at_risk_total), f"{_money(d['ar_amt'])} AR + {_money(d['slipped_amt'])} deals", _delta_html(deltas, "revenue_at_risk")))
    h.append(kpi("3", "Likely to close (30d)", _money(d['close_weighted']), f"{d['close_cnt']} deals, weighted", _delta_html(deltas, "forecast_30d")))
    h.append(kpi("4", "New advocates (7d)", str(d['advocates']), _money(d['won_amt']), _delta_html(deltas, "advocates_7d")))
    h.append('</tr></table>')
    h.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="8" style="border-collapse:separate;"><tr>'
             f'<td style="background:{NAVY};border-radius:8px;padding:14px 16px;">'
             f'<div style="font-size:10px;color:{ACCENT};text-transform:uppercase;letter-spacing:.08em;font-weight:700;">5 &middot; The one decision today</div>'
             f'<div style="font-size:15px;font-weight:600;color:#ffffff;margin-top:6px;line-height:1.45;">{decision.replace("**","")}</div>'
             '</td></tr></table>')
    h.append('</td></tr>')

    # What Changed — trend anomalies (A4), high in the layout since "what
    # changed / needs attention" is decision-grade.
    if d.get("anomalies"):
        _acol = {"high": "#b91c1c", "medium": "#b45309", "low": "#a16207"}
        _adot = {"high": "🔴", "medium": "🟠", "low": "🟡"}
        h.append(section(
            'What Changed — Trend Anomalies '
            '<span style="background:#fdecec;color:#b91c1c;font-size:9px;font-weight:700;'
            'letter-spacing:.05em;padding:2px 7px;border-radius:999px;vertical-align:middle;">AUTO-DETECTED</span>',
            lis(sorted(d["anomalies"], key=lambda s: {"high": 0, "medium": 1, "low": 2}.get(s.get("severity"), 3)),
                lambda s: (f'<b style="color:{_acol.get(s.get("severity"), INK)};">'
                           f'{_adot.get(s.get("severity"), "•")} {s["headline"]}</b>'
                           f'<br><span style="color:{MUTE};">→ {s["recommended_action"]}</span>'))))

    h.append(section("1 &middot; Revenue Snapshot", lis([
        ("Active pipeline",       f'{_money(d["pipeline"])} ({d["open_cnt"]} open)' + _delta_html(deltas, "pipeline")),
        ("Weighted forecast",     _money(d["weighted"]) + _delta_html(deltas, "weighted_forecast")),
        ("Closing next 30 days",  f'{_money(d["close_amt"])} ({d["close_cnt"]} deals)'),
        (cap_label,               f'{cap_val} &middot; {_money(d["rev_7d"])} last 7 days'),
    ], lambda r: f'{r[0]}: <b>{r[1]}</b>')))

    h.append(section("2 &middot; Revenue at Risk",
        f'<div style="font-size:13px;color:{INK};margin-bottom:7px;">Overdue AR <b>{_money(d["ar_amt"])}</b> '
        f'({d["ar_cnt"]} invoices) &nbsp;&middot;&nbsp; Slipped deals <b>{_money(d["slipped_amt"])}</b> ({d["slipped_cnt"]})</div>'
        + lis(d["atrisk"], lambda r: f'{r[0]} <span style="color:{MUTE}">({r[1]})</span> — <b>{_money(r[2])}</b>, {r[3]}d past close')))

    h.append(section("3 &middot; Likely to Close — Next 30 Days",
        lis(d["closing"], lambda r: f'{r[0]} <span style="color:{MUTE}">({r[1]})</span> — <b>{_money(r[2])}</b> at {int(r[3])}% &middot; {r[4]}')))

    h.append(section("4 &middot; Growth — Biggest Deals in Play",
        lis(d["biggest"], lambda r: f'{r[0]} <span style="color:{MUTE}">({r[1]})</span> — <b>{_money(r[2])}</b> &middot; {r[3]} at {int(r[4])}%')))

    h.append(section("5 &middot; Critical Events",
        lis(d["big_inv"], lambda r: (f'Overdue invoice {r[0]} <span style="color:{MUTE}">({r[1]})</span> — '
            f'<b>{_money(r[2])}</b>, {r[3]}d overdue'
            + (f' <span style="color:{MUTE}">(large balance, 45d+)</span>' if float(r[2])>25000 and int(r[3])>=45 else '')))))

    # Customer health & operations — the vision dashboard's CSAT + inventory
    # lines, shown once there's data to stand on.
    _health = []
    if d.get("conv_n_7d"):
        _health.append(("Customer sentiment (7d, all channels)",
                        f'{d["conv_pos_7d"]} positive · {d["conv_neg_7d"]} negative '
                        f'of {d["conv_n_7d"]} conversations'
                        + _delta_html(deltas, "conv_neg_7d")))
    if d.get("csat_proxy") is not None:
        _health.append(("Customer satisfaction (30d proxy)",
                        f'{d["csat_proxy"]:.0f}% non-negative interactions'
                        + _delta_html(deltas, "csat_proxy_30d")))
    if d.get("low_stock") is not None:
        _lvl = ("Low" if d["low_stock"] == 0 else
                "Elevated" if d["low_stock"] < 10 else "High")
        _health.append(("Inventory risk",
                        f'{_lvl} — {d["low_stock"]} product(s) at/below stock '
                        f'floor or short of open-order demand'
                        + _delta_html(deltas, "low_stock_count")))
    if _health:
        h.append(section("Customer Health &amp; Operations",
                         lis(_health, lambda r: f'{r[0]}: <b>{r[1]}</b>')))

    if d.get("approvals"):
        h.append(section(
            'Approvals Awaiting Your Decision',
            lis(d["approvals"], lambda r: (
                f'<b>{r[0]}</b>'
                + (f' — <b>{_money(r[3])}</b>' if float(r[3] or 0) else '')
                + f' <span style="color:{MUTE}">proposed by {r[1]} · assigned to {r[2]} ({r[4]})</span>'
                + _critic_html(r)))))

    if gov_lines:
        h.append(section('Governance Today — What Leadership Must Know or Decide',
                         lis(gov_lines, lambda ln: ln)))

    if obj_lines:
        h.append(section('Business Objectives — What the Agent Fleet Is Pursuing',
                         lis(obj_lines, lambda ln: ln)))

    if perf_lines:
        h.append(section('Agent Performance — Last 30 Days',
                         lis(perf_lines, lambda ln: ln)))

    if web:
        h.append(section(
            f'6 &middot; {web["title"]} '
            f'<span style="background:#e8f3ec;color:#1e7c45;font-size:9px;font-weight:700;'
            f'letter-spacing:.05em;padding:2px 7px;border-radius:999px;vertical-align:middle;">LIVE WEB</span>',
            _web_intel_html(web, INK, MUTE, ACCENT)))

    # Footer
    h.append(f'<tr><td style="padding:20px 28px 24px;font-family:{SANS};">'
             f'<div style="border-top:1px solid {LINE};padding-top:13px;font-size:11px;color:{MUTE};line-height:1.55;">'
             f'Generated by <b style="color:{NAVY};">Conscestra CRM</b> — the orchestration of customer intelligence. '
             f'Reply to this email to ask the Orchestrator a follow-up.<br>'
             f'<span style="color:#aab2c0;">Confidential · prepared exclusively for the Office of the CEO.</span></div>'
             f'</td></tr>')
    h.append('</table></div>')

    # Plain-ASCII subject — shared-host spam filters down-rank $/non-ASCII headers.
    subject = f"Morning CEO Briefing - {today}"
    return {"subject": subject, "html": "".join(h), "text": text}


# ── Role-specific briefings (CFO / CRO / COO) ───────────────────────────────────
_META = {k: (lbl, unit, hib, imp) for (k, lbl, unit, hib, imp) in _METRICS}


def _fmt_metric(key: str, val) -> str:
    unit = _META.get(key, (key, "", True, 0))[1]
    if val is None:
        return "—"
    if unit == "usd":
        return _money(val)
    if unit == "count":
        return f"{int(val):,}"
    if unit == "score":
        return f"{val:+.2f}"
    return str(val)


# role -> (subtitle, four KPI metric keys, ordered detail sections)
_ROLE_CFG = {
    "CFO": ("Cash & collections focus",
            ["captured_7d", "overdue_ar", "revenue_at_risk", "forecast_30d"],
            ["overdue_invoices", "atrisk"]),
    "CRO": ("Revenue engine focus",
            ["pipeline", "forecast_30d", "slipped_value", "advocates_7d"],
            ["closing", "biggest", "atrisk"]),
    "COO": ("Execution & operations focus",
            ["new_leads_7d", "overdue_activities", "advocates_7d", "captured_7d"],
            ["atrisk", "overdue_invoices"]),
}


def render_role(d: Dict[str, Any], deltas: Dict[str, Any], role: str) -> Dict[str, str]:
    today = datetime.now().strftime("%B %d, %Y")
    subtitle, kpi_keys, sections = _ROLE_CFG[role]
    vals = _metric_values(d)
    web = _web_intel(role)
    NAVY, INK, MUTE, LINE, CARD, ACCENT = "#15233f", "#26304a", "#7b8497", "#e7ecf3", "#f7f9fc", "#b08a46"
    SANS = "Arial,Helvetica,sans-serif"

    def kpi(n, key):
        return (f'<td width="25%" valign="top" style="background:{CARD};border:1px solid {LINE};border-radius:8px;padding:12px 9px;font-family:{SANS};">'
                f'<div style="font-size:9px;color:{MUTE};text-transform:uppercase;letter-spacing:.01em;font-weight:700;white-space:nowrap;">{n} &middot; {_META.get(key,(key,))[0]}</div>'
                f'<div style="font-size:21px;line-height:1.05;font-weight:700;color:{NAVY};margin-top:7px;">{_fmt_metric(key, vals.get(key))}{_delta_html(deltas, key)}</div></td>')

    def lis(rows, fmt):
        body = "".join(f'<li style="margin:4px 0;">{fmt(r)}</li>' for r in rows) or f'<li style="color:{MUTE};">None.</li>'
        return f'<ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5;color:{INK};">{body}</ul>'

    def section(title, inner):
        return (f'<tr><td style="padding:16px 28px 0;font-family:{SANS};">'
                f'<div style="font-size:12px;font-weight:700;color:{NAVY};text-transform:uppercase;letter-spacing:.06em;'
                f'border-bottom:2px solid {LINE};padding-bottom:6px;margin-bottom:9px;">{title}</div>{inner}</td></tr>')

    sec_html = {
        "overdue_invoices": ("Overdue Invoices", lis(d["big_inv"], lambda r: f'{r[0]} <span style="color:{MUTE}">({r[1]})</span> — <b>{_money(r[2])}</b>, {r[3]}d overdue')),
        "atrisk":           ("Slipped / At-risk Deals", lis(d["atrisk"], lambda r: f'{r[0]} <span style="color:{MUTE}">({r[1]})</span> — <b>{_money(r[2])}</b>, {r[3]}d past close')),
        "closing":          ("Likely to Close — Next 30 Days", lis(d["closing"], lambda r: f'{r[0]} <span style="color:{MUTE}">({r[1]})</span> — <b>{_money(r[2])}</b> at {int(r[3])}%')),
        "biggest":          ("Biggest Deals in Play", lis(d["biggest"], lambda r: f'{r[0]} <span style="color:{MUTE}">({r[1]})</span> — <b>{_money(r[2])}</b> &middot; {r[3]}')),
    }

    h = [f'<div style="background:#eef1f6;padding:26px 12px;">'
         '<table role="presentation" align="center" width="640" cellpadding="0" cellspacing="0" style="width:640px;max-width:640px;margin:0 auto;background:#fff;border:1px solid #e1e6ef;border-radius:4px;">']
    h.append(f'<tr><td style="height:6px;background:{NAVY};border-top:3px solid {ACCENT};font-size:0;line-height:0;">&nbsp;</td></tr>')
    h.append(f'<tr><td style="padding:24px 28px 8px;">'
             f'<div style="font-family:Georgia,serif;font-size:12px;letter-spacing:.24em;color:{ACCENT};font-weight:700;text-transform:uppercase;">Conscestra CRM</div>'
             f'<div style="font-family:Georgia,serif;font-size:25px;font-weight:700;color:{NAVY};margin-top:5px;">{role} Morning Briefing</div>'
             f'<div style="font-family:{SANS};font-size:12.5px;color:{MUTE};margin-top:5px;">{today} &nbsp;&middot;&nbsp; {subtitle}</div></td></tr>')
    h.append(f'<tr><td style="padding:10px 20px 2px;font-family:{SANS};"><table role="presentation" width="100%" cellpadding="0" cellspacing="8" style="border-collapse:separate;"><tr>')
    for i, key in enumerate(kpi_keys, 1):
        h.append(kpi(i, key))
    h.append('</tr></table></td></tr>')
    # What Changed — trend anomalies (A4), shared with the flagship briefing.
    if d.get("anomalies"):
        _acol = {"high": "#b91c1c", "medium": "#b45309", "low": "#a16207"}
        _adot = {"high": "🔴", "medium": "🟠", "low": "🟡"}
        h.append(section(
            'What Changed — Trend Anomalies '
            '<span style="background:#fdecec;color:#b91c1c;font-size:9px;font-weight:700;'
            'letter-spacing:.05em;padding:2px 7px;border-radius:999px;vertical-align:middle;">AUTO-DETECTED</span>',
            lis(sorted(d["anomalies"], key=lambda s: {"high": 0, "medium": 1, "low": 2}.get(s.get("severity"), 3)),
                lambda s: (f'<b style="color:{_acol.get(s.get("severity"), INK)};">'
                           f'{_adot.get(s.get("severity"), "•")} {s["headline"]}</b>'
                           f'<br><span style="color:{MUTE};">→ {s["recommended_action"]}</span>'))))
    for s in sections:
        title, inner = sec_html[s]
        h.append(section(title, inner))
    # Governance today — the CEO sees the whole picture (escalations land on
    # that desk, D4); every other authority sees it only when it has lines.
    _gl = _governance_lines(d.get("governance"))
    if _gl and (role == "CEO" or any("BREACH" in ln or "STRANDED" in ln for ln in _gl)):
        h.append(section('Governance Today', lis(_gl, lambda ln: ln)))
    # Approvals routed to THIS executive (assigned_to is "ROLE Full Name")
    mine = [r for r in d.get("approvals", []) if str(r[2]).startswith(role)]
    if mine:
        h.append(section(
            'Approvals Awaiting Your Decision',
            lis(mine, lambda r: (
                f'<b>{r[0]}</b>'
                + (f' — <b>{_money(r[3])}</b>' if float(r[3] or 0) else '')
                + f' <span style="color:{MUTE}">proposed by {r[1]} ({r[4]})</span>'
                + _critic_html(r)))))
    if web:
        h.append(section(
            f'{web["title"]} '
            f'<span style="background:#e8f3ec;color:#1e7c45;font-size:9px;font-weight:700;'
            f'letter-spacing:.05em;padding:2px 7px;border-radius:999px;vertical-align:middle;">LIVE WEB</span>',
            _web_intel_html(web, INK, MUTE, ACCENT)))
    h.append(f'<tr><td style="padding:20px 28px 24px;font-family:{SANS};">'
             f'<div style="border-top:1px solid {LINE};padding-top:13px;font-size:11px;color:{MUTE};">'
             f'Generated by <b style="color:{NAVY};">Conscestra CRM</b> for the {role}. Reply to ask the Orchestrator a follow-up.</div></td></tr>')
    h.append('</table></div>')

    text = (f"{role} MORNING BRIEFING — {today}  ({subtitle})\n\n"
            + "\n".join(f"  {_META.get(k,(k,))[0]}: {_fmt_metric(k, vals.get(k))}{_delta_text(deltas,k)}" for k in kpi_keys))
    _anom = _anomaly_lines(d.get("anomalies"))
    if _anom:
        text += "\n\nWHAT CHANGED — TREND ANOMALIES\n" + "\n".join(f"  - {ln}" for ln in _anom)
    _disc = _discount_lines(d.get("discount_pressure"))
    if _disc:
        text += "\n\nDISCOUNT PRESSURE\n" + "\n".join(f"  - {ln}" for ln in _disc)
    if web:
        text += ("\n\n" + web["title"].replace("&amp;", "&").upper() + " (LIVE WEB)\n"
                 + "\n".join(_web_intel_text(web))).replace("**", "")
    return {"subject": f"{role} Morning Briefing - {today}", "html": "".join(h), "text": text}


# category -> (role label, builder). CEO uses the flagship render(); others the role view.
_BRIEFINGS = {
    "ceo_briefing": ("CEO", lambda d, dl: render(d, dl)),
    "cfo_briefing": ("CFO", lambda d, dl: render_role(d, dl, "CFO")),
    "cro_briefing": ("CRO", lambda d, dl: render_role(d, dl, "CRO")),
    "coo_briefing": ("COO", lambda d, dl: render_role(d, dl, "COO")),
}


def build_briefing(persist: bool = False) -> Dict[str, str]:
    """Render the CEO briefing with deltas vs the previous snapshot. When
    persist=True (the real daily send), also store today's snapshot."""
    d = gather()
    values = _metric_values(d)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            deltas = _compute_deltas(values, _previous_metrics(cur))
            msg = render(d, deltas)
            if persist:
                _persist_snapshot(cur, values, deltas, msg.get("text", "")[:4000])
                conn.commit()
        return msg
    finally:
        conn.close()


# ── Send ────────────────────────────────────────────────────────────────────────

def recipients() -> List[tuple]:
    """Resolve briefing recipients from the executives table (the human-interface
    layer): active execs with auto-email on and 'ceo_briefing' in their
    notification_categories. Falls back to the CEO_BRIEFING_EMAIL env var if the
    table is empty/unavailable, so the briefing always has a destination."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT email, full_name FROM executives "
                    "WHERE is_active AND auto_email_enabled "
                    "  AND 'ceo_briefing' = ANY(notification_categories) "
                    "  AND email IS NOT NULL "
                    "ORDER BY role_code")
                rows = [(r[0], r[1]) for r in cur.fetchall() if r[0]]
        finally:
            conn.close()
        if rows:
            return rows
    except Exception as exc:
        logger.warning(f"[ceo_briefing] executives lookup failed, using env fallback: {exc}")
    return [(RECIPIENT, "CEO")] if RECIPIENT else []


def _subscribers(cur, category: str) -> List[tuple]:
    cur.execute(
        "SELECT email, full_name FROM executives "
        "WHERE is_active AND auto_email_enabled AND email IS NOT NULL "
        "  AND %s = ANY(notification_categories) ORDER BY role_code", (category,))
    return [(r[0], r[1]) for r in cur.fetchall() if r[0]]


def _alert_silent_failure(results: Dict[str, Any], sent: int, expected: int,
                          reasons: List[str]) -> None:
    """Page when a scheduled internal briefing produced no mail.

    The CEO briefing failed silently for five consecutive days: the outbound
    guard rejected it, send_briefing counted the rejection into `results`, and
    nothing looked at `results`. A scheduled job that is SUPPOSED to send and
    sends nothing is an incident, not a statistic — the whole value of the
    briefing is that it arrives unprompted, so nobody is waiting to notice it
    missing.

    Two conditions page, because they fail differently:
      sent == 0 with subscribers   total outage (the guard-block signature)
      sent  < expected             partial — one role silently dropped

    Emitted as `supervisor.alert`, which is an already-registered event type
    with existing consumers. A new event type would need registering first and
    would otherwise be dropped on the floor — the same silence this fixes.
    Deduped per rule per day so a persistent fault pages once, not hourly.
    """
    if expected <= 0:
        return                                    # nothing was owed; not a fault
    if sent >= expected:
        return
    total_outage = sent == 0
    rule = "ceo_briefing.silent" if total_outage else "ceo_briefing.partial"
    detail = "; ".join(dict.fromkeys(reasons))[:400] or "no reason reported"
    headline = (f"Executive briefing sent NOTHING ({expected} subscriber(s) "
                f"expected) — {detail}") if total_outage else (
                f"Executive briefing sent {sent}/{expected} — {detail}")
    logger.error(f"[ceo_briefing] ALERT {rule}: {headline}")
    try:
        import json as _json
        import uuid as _uuid
        dup = execute_sp(
            """SELECT 1 AS x FROM events
                WHERE event_type='supervisor.alert' AND source_system='ceo_briefing'
                  AND payload->'context'->>'rule' = %(r)s
                  AND created_at > now() - interval '20 hours' LIMIT 1""",
            {"r": rule})
        if dup:
            return
        payload = _json.dumps({"context": {
            "rule": rule,
            "severity": "critical" if total_outage else "warning",
            "headline": headline,
            "metric": "briefings_sent",
            "value": sent,
            "expected": expected,
            "owner_agent": "ceo_briefing",
            "recommended_action": (
                "Check the outbound guard verdict and executive subscriptions "
                "(executives.notification_categories)."),
            "by_briefing": results}})
        execute_sp(
            "SELECT emit_event('supervisor.alert','system',%(id)s::uuid,"
            "%(p)s::jsonb,NULL,'ceo_briefing') AS r",
            {"id": str(_uuid.uuid4()), "p": payload})
    except Exception as exc:
        # Never let the alerter break the job it is watching — but do not let it
        # fail quietly either, since quiet failure is the defect being fixed.
        logger.error(f"[ceo_briefing] could not emit {rule} alert: {exc}",
                     exc_info=True)


def send_briefing(force: bool = False) -> Dict[str, Any]:
    """Capture today's snapshot (always), then deliver each role briefing
    (CEO/CFO/CRO/COO) to its subscribers — execs whose notification_categories
    include that briefing's category. Internal email; bypasses the customer gate."""
    d = gather()
    values = _metric_values(d)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            deltas = _compute_deltas(values, _previous_metrics(cur))
            ceo_msg = render(d, deltas)
            _persist_snapshot(cur, values, deltas, ceo_msg.get("text", "")[:4000])  # history
            conn.commit()
            subs = {cat: _subscribers(cur, cat) for cat in _BRIEFINGS}
    finally:
        conn.close()

    if not ENABLED and not force:
        return {"enabled": False, "skipped": True, "snapshot_captured": True}

    from app.agents.email.smtp_imap import send_email
    results: Dict[str, Any] = {}
    total = 0
    any_sub = False
    expected = 0                      # how many sends were OWED, for the alert
    reasons: List[str] = []           # why the ones that failed, failed
    for cat, (role, builder) in _BRIEFINGS.items():
        rc = subs.get(cat) or []
        if not rc:
            continue
        any_sub = True
        expected += len(rc)
        msg = ceo_msg if cat == "ceo_briefing" else builder(d, deltas)
        s = f = 0
        for email, _name in rc:
            try:
                res = send_email(email, msg["subject"], msg["html"], msg["text"], from_name="Conscestra CRM")
                ok = bool(res.get("success", True)) if isinstance(res, dict) else True
                s += 1 if ok else 0; f += 0 if ok else 1; total += 1 if ok else 0
                if not ok:
                    # Carry the provider/guard reason into the alert. Without it
                    # the page says "nothing sent" and the operator still has to
                    # go digging for why.
                    why = res.get("message") if isinstance(res, dict) else None
                    reasons.append(f"{role}: {why or 'send reported failure'}")
            except Exception as exc:
                logger.error(f"[ceo_briefing] {role} send to {email} failed: {exc}", exc_info=True)
                f += 1
                reasons.append(f"{role}: {type(exc).__name__}: {exc}")
        results[cat] = {"role": role, "sent": s, "failed": f}

    # Fallback: no executive subscribers at all → send CEO briefing to env address.
    if not any_sub and RECIPIENT:
        expected += 1
        res = send_email(RECIPIENT, ceo_msg["subject"], ceo_msg["html"], ceo_msg["text"], from_name="Conscestra CRM")
        ok = bool(res.get("success", True)) if isinstance(res, dict) else True
        results["env_fallback"] = {"role": "CEO", "sent": 1 if ok else 0, "failed": 0 if ok else 1}
        total += 1 if ok else 0
        if not ok:
            reasons.append(f"env_fallback: {res.get('message') if isinstance(res, dict) else 'failed'}")

    # No subscribers AND no env fallback is its own failure: the job is enabled,
    # ran, and could not have delivered to anyone. Silent by construction.
    if not any_sub and not RECIPIENT:
        logger.error("[ceo_briefing] ALERT ceo_briefing.no_recipients: enabled "
                     "but no executive subscribes to any briefing and "
                     "CEO_BRIEFING_EMAIL is unset — nothing can be delivered")
        results["no_recipients"] = True

    _alert_silent_failure(results, total, expected, reasons)

    # Proactive Slack post (#5) — broadcast the CEO briefing into the team channel.
    # Best-effort + self-gated (SLACK_PROACTIVE_ENABLED + a configured channel);
    # a no-op draft when disabled, so this never affects email delivery.
    try:
        from app.core import transports
        pr = transports.post_internal("internal_briefing",
                                      f"*{ceo_msg['subject']}*\n\n{ceo_msg.get('text', '')}")
        if pr.get("sent"):
            results["slack"] = {"channel": pr.get("channel"), "sent": 1}
    except Exception as exc:
        logger.debug(f"[ceo_briefing] proactive Slack post skipped: {exc}")

    logger.info(f"[ceo_briefing] delivered total={total} by_briefing={results}")
    return {"sent_count": total, "expected": expected, "by_briefing": results,
            "failures": reasons, "subject": ceo_msg["subject"]}


# ── Admin endpoints ───────────────────────────────────────────────────────────────

router = APIRouter(tags=["ceo-briefing"])


@router.get("/ceo-briefing/status")
def ceo_briefing_status():
    rc = recipients()
    return {"enabled": ENABLED, "recipients": [n for _, n in rc],
            "recipient_emails": len(rc), "env_fallback": bool(RECIPIENT)}


@router.get("/ceo-briefing/preview")
def ceo_briefing_preview(role: str = "CEO"):
    """Render a briefing WITHOUT sending (HTML + text). Admin-gated.
    ?role=CEO|CFO|CRO|COO — CEO is the flagship layout, others the role view."""
    role = (role or "CEO").upper()
    if role == "CEO":
        return build_briefing()
    if role not in _ROLE_CFG:
        return {"error": f"Unknown role '{role}'. Use CEO, CFO, CRO or COO."}
    d = gather()
    values = _metric_values(d)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            deltas = _compute_deltas(values, _previous_metrics(cur))
    finally:
        conn.close()
    return render_role(d, deltas, role)


@router.post("/ceo-briefing/send-now")
async def ceo_briefing_send_now():
    """Send the briefing now (force, regardless of the enabled flag). Admin-gated."""
    import asyncio
    return await asyncio.to_thread(send_briefing, True)


@router.get("/executive-snapshot/history")
def executive_snapshot_history(days: int = 30):
    """Snapshot history shaped for the Executive Dashboard: one series per metric
    (latest value + delta + a value-per-date series for sparklines)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.snapshot_date::text, m.metric_key, m.value, m.delta_pct "
                "FROM executive_snapshot s JOIN executive_metric m ON m.snapshot_id = s.snapshot_id "
                "WHERE s.period_type='daily' AND s.snapshot_date >= CURRENT_DATE - %s "
                "ORDER BY s.snapshot_date, m.metric_key", (int(days),))
            rows = cur.fetchall()
    finally:
        conn.close()

    dates = sorted({r[0] for r in rows})
    meta = {k: (lbl, unit, hib, imp) for (k, lbl, unit, hib, imp) in _METRICS}
    by_key: Dict[str, Dict[str, Any]] = {}
    for sdate, key, value, dpct in rows:
        m = meta.get(key, (key, "", True, 0))
        e = by_key.setdefault(key, {
            "key": key, "label": m[0], "unit": m[1], "higher_is_better": m[2],
            "importance": m[3], "series": {}, "latest": None, "delta_pct": None})
        e["series"][sdate] = float(value) if value is not None else None
        e["latest"] = float(value) if value is not None else e["latest"]
        e["delta_pct"] = float(dpct) if dpct is not None else e["delta_pct"]

    metrics = []
    for e in by_key.values():
        e["series"] = [{"date": d, "value": e["series"].get(d)} for d in dates]
        metrics.append(e)
    metrics.sort(key=lambda x: (-x["importance"], x["label"]))
    return {"dates": dates, "metrics": metrics}
