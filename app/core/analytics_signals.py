"""Analytics trend anomalies (Blindspot A1 + A5).

The Analytics agent's system prompt has always ADVERTISED anomaly detection
("win rate drops > 10% week-over-week", "stalled deal: no stage change in 5
business days", "always surface actionable insights") and a HEARTBEAT/ALERT
protocol — but nothing computed those trend anomalies. Every existing detector
in supervisor.py is threshold/count based (N invoices overdue, N unworked
leads); none is period-over-period. This module fills that exact gap.

Design — reuse, don't rebuild:
  * Each detector returns the SAME signal dict shape supervisor.py already
    emits (rule / severity / headline / metric / value / owner_agent /
    recommended_action / auto), so supervisor imports DETECTORS and they ride
    its alert-emit + 12h dedupe + governance + planner-bridge machinery for
    free. That planner bridge is exactly the "insight → governed action"
    write-back (A5): a breach becomes a proposal a human approves — nothing
    mutates directly.
  * BREACH_GOALS gives the supervisor planner a plain-language goal per rule so
    it can compose a governed multi-agent play (A5).
  * detect_all() returns the current anomalies WITHOUT dedupe, so the Analytics
    agent surface (mode='anomalies') can answer "what needs attention?" live —
    closing A1 at the exact surface that advertised it.

All detectors are READ-ONLY and defensive: any SQL/shape error → no signal
(never breaks the supervisor tick or the analytics chat). All thresholds are
env-tunable.

CONFIG (env)
  ANALYTICS_WINRATE_DROP_PP   10   win-rate drop (percentage points, WoW) that trips
  ANALYTICS_WINRATE_MIN_N     5    min decided deals in EACH 7d window before judging
  ANALYTICS_STALL_DAYS        5    open deal untouched this many days = stalled
  ANALYTICS_STALL_MIN         8    stalled-deal count that trips
  ANALYTICS_REVENUE_DROP_PCT  30   closed-won revenue drop (%, WoW) that trips
  ANALYTICS_REVENUE_MIN_BASE  1000 min prior-week won revenue before a slump can trip
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import execute_sp

logger = logging.getLogger("analytics_signals")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


WINRATE_DROP_PP  = _int("ANALYTICS_WINRATE_DROP_PP", 10)
WINRATE_MIN_N    = _int("ANALYTICS_WINRATE_MIN_N", 5)
STALL_DAYS       = _int("ANALYTICS_STALL_DAYS", 5)
STALL_MIN        = _int("ANALYTICS_STALL_MIN", 8)
REVENUE_DROP_PCT = _int("ANALYTICS_REVENUE_DROP_PCT", 30)
REVENUE_MIN_BASE = _int("ANALYTICS_REVENUE_MIN_BASE", 1000)


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# The win-rate and revenue definitions (and their decided_at time-scoping) now
# live ONCE in the Metric Registry (app/core/metrics.py, P0 step 2). These
# detectors consume metrics.compare() over two windows rather than re-deriving
# the metric — so a "win rate drop" is measured against the exact same win_rate
# the /metrics endpoint, the exec briefing and the Explore agent report.


def _money(v) -> str:
    return f"${_num(v):,.0f}"


def _signal(rule, severity, headline, metric, value, owner_agent,
            recommended_action, auto=None) -> Dict[str, Any]:
    """Same shape as supervisor._signal so these plug straight into its loop."""
    return {"rule": rule, "severity": severity, "headline": headline,
            "metric": metric, "value": value, "owner_agent": owner_agent,
            "recommended_action": recommended_action, "auto": auto}


# ============================================================================
# DETECTORS  (period-over-period — the trend anomalies the prompt promised)
# ----------------------------------------------------------------------------
# All take an optional `pack` for supervisor-detector call-compatibility; these
# run their own queries (like supervisor.detect_low_stock / detect_sentiment_drop)
# because the executive KPI pack's win_rate is an all-time snapshot, not WoW.
# The decision timestamp for a closed deal is `decided_at` (stamped by the
# opportunity SP the moment status flips to closed_won/closed_lost); _decision_ts
# falls back to `updated_at` on a pre-migration schema.
# ============================================================================

def detect_win_rate_drop(pack: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Win rate over the trailing 7 days vs the prior 7 days. Trips when it
    falls by >= WINRATE_DROP_PP points AND each window decided >= WINRATE_MIN_N
    deals (so one week of thin volume can't trip it). This is the analytics
    prompt's headline promise: "win rate drops > 10% week-over-week"."""
    try:
        from app.core import metrics
        cmp = metrics.compare("win_rate", "last_7d", "prev_7d")
    except Exception as exc:
        logger.debug(f"[analytics] win-rate detector skipped: {exc}")
        return None
    cur, prev = cmp["a"], cmp["b"]
    dec_cur, dec_prev = cur["denominator"], prev["denominator"]      # decided-deal counts
    won_cur = cur["numerator"]
    cur_pct, prev_pct = cur["value"], prev["value"]                  # already win_rate %
    if dec_cur < WINRATE_MIN_N or dec_prev < WINRATE_MIN_N:
        return None
    if cur_pct is None or prev_pct is None:
        return None
    drop = prev_pct - cur_pct
    if drop >= WINRATE_DROP_PP:
        return _signal(
            "win_rate_drop", "high" if drop >= WINRATE_DROP_PP * 2 else "medium",
            f"Win rate fell {drop:.0f} pts week-over-week "
            f"({prev_pct:.0f}% → {cur_pct:.0f}%; {won_cur}/{dec_cur} won this week)",
            "win_rate_drop_pp", round(drop, 1), "opportunities",
            "Review this week's lost deals for a common loss reason; "
            "run a deal-desk on the open pipeline")
    return None


def detect_stalled_deals(pack: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Open opportunities with no record change in >= STALL_DAYS days. The
    prompt's "stalled deal: no stage change in N business days"; `updated_at`
    is the proxy (no dedicated stage_changed_at column exists). Distinct from
    supervisor.detect_slipped_deals (past close date) and the executive pack's
    idle_deals (no logged ACTIVITY) — this is record-level stagnation."""
    try:
        # run_readonly, NOT execute_sp: execute_sp opens its own connection and
        # therefore escapes the snapshot detect_all() pins, so this detector was
        # reading a different instant from the two that use metrics.compare().
        # A pass that mixes instants can both invent a breach and hide one.
        from app.core.semantic_query import run_readonly
        rows = run_readonly(
            "SELECT count(*) AS n, COALESCE(SUM(amount),0) AS val "
            "FROM opportunities "
            "WHERE status='open' AND updated_at < now() - (%s || ' days')::interval",
            [STALL_DAYS])
        r = rows[0] if rows else {}
        n, val = int(_num(r.get("n"))), _num(r.get("val"))
    except Exception as exc:
        logger.debug(f"[analytics] stalled-deal detector skipped: {exc}")
        return None
    if n >= STALL_MIN:
        return _signal(
            "stalled_deals", "high" if n >= STALL_MIN * 3 else "medium",
            f"{n} open deals stalled ≥ {STALL_DAYS}d with no movement · "
            f"{_money(val)} in limbo",
            "stalled_count", n, "opportunities",
            "Re-engage the highest-value stalled deals or re-stage them so the "
            "forecast reflects reality")
    return None


def detect_revenue_slump(pack: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Closed-won revenue over the trailing 7 days vs the prior 7 days. Trips
    when it drops >= REVENUE_DROP_PCT% and the prior week booked at least
    REVENUE_MIN_BASE (so a slow baseline can't trip a false slump)."""
    try:
        from app.core import metrics
        cmp = metrics.compare("won_revenue", "last_7d", "prev_7d")
    except Exception as exc:
        logger.debug(f"[analytics] revenue-slump detector skipped: {exc}")
        return None
    cur, prev = _num(cmp["a"]["value"]), _num(cmp["b"]["value"])
    if prev < REVENUE_MIN_BASE:
        return None
    drop_pct = 100.0 * (prev - cur) / prev
    if drop_pct >= REVENUE_DROP_PCT:
        return _signal(
            "revenue_slump", "high" if drop_pct >= REVENUE_DROP_PCT * 1.5 else "medium",
            f"Closed-won revenue down {drop_pct:.0f}% week-over-week "
            f"({_money(prev)} → {_money(cur)})",
            "revenue_drop_pct", round(drop_pct, 1), "opportunities",
            "Check which deals were expected to close this week and pull the "
            "slipping ones back in")
    return None


# Ordered list — supervisor extends its DETECTORS with this.
DETECTORS: List[Callable[[Optional[Dict[str, Any]]], Optional[Dict[str, Any]]]] = [
    detect_win_rate_drop, detect_stalled_deals, detect_revenue_slump,
]

# Plain-language goals for the supervisor planner bridge (A5). {headline} is
# filled from the live signal so the composed governed play is specific.
BREACH_GOALS: Dict[str, str] = {
    "win_rate_drop":
        "Investigate the win-rate decline ({headline}). Review the deals lost "
        "this week for a common loss reason and propose a corrective play.",
    "stalled_deals":
        "Unstick stalled pipeline ({headline}). Identify the open deals with no "
        "movement and schedule re-engagement for the highest-value ones.",
    "revenue_slump":
        "Address the closed-won revenue slump ({headline}). Surface the deals "
        "that were expected to close this week and re-engage the ones slipping.",
}


# ============================================================================
# PUBLIC API — used by the Analytics agent (mode='anomalies') + admin endpoint
# ============================================================================

def detect_all() -> List[Dict[str, Any]]:
    """Run every detector and return the CURRENT anomalies (no dedupe — that is
    a supervisor concern for alert emission; here we want the live picture).

    ONE SNAPSHOT for the whole pass. Each detector previously opened its own
    transaction, so a pass could observe the win rate before a deal closed and
    the revenue after it — "the live picture" was several pictures. Anomaly
    detection compares thresholds across metrics, so mixed instants can both
    invent a breach and hide one."""
    from app.core.semantic_query import snapshot

    out: List[Dict[str, Any]] = []
    with snapshot():
        for det in DETECTORS:
            try:
                sig = det(None)
            except Exception as exc:
                logger.warning(f"[analytics] detector {det.__name__} failed: {exc}")
                continue
            if sig:
                out.append(sig)
    return out


def format_briefing(signals: List[Dict[str, Any]]) -> str:
    """Markdown for the Analytics agent's anomalies answer — insight + the
    recommended action + how it becomes an approved action (A5)."""
    if not signals:
        return ("### 🟢 Analytics — no anomalies\n"
                "Win rate, deal movement and closed-won revenue are all within "
                "normal week-over-week range. Nothing needs attention right now.")
    icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    order = {"high": 0, "medium": 1, "low": 2}
    sigs = sorted(signals, key=lambda s: order.get(s.get("severity"), 3))
    out = [f"### 📊 Analytics — {len(sigs)} anomaly(ies) need attention", ""]
    for s in sigs:
        out.append(f"- {icon.get(s['severity'], '•')} **{s['headline']}**  ")
        out.append(f"  → {s['recommended_action']}  _(owner: {s['owner_agent']})_")
    out.append("")
    # Trusted Context: state the canonical definition behind each metric these
    # detectors judged, so a surfaced anomaly is explainable and can't be read as
    # a different win-rate/revenue definition than the rest of the product uses.
    defs = _metric_definitions(sigs)
    if defs:
        out.append("**How these are measured**")
        for line in defs:
            out.append(f"- {line}")
        out.append("")
    out.append("_When the proactive supervisor runs, each anomaly is raised as "
               "an alert and — with the planner bridge on — composed into a "
               "governed action you approve from the queue (nothing is sent or "
               "changed automatically)._")
    return "\n".join(out)


# Which registered metric backs each detector rule (for the definition footnote).
_RULE_METRIC = {"win_rate_drop": "win_rate", "revenue_slump": "won_revenue"}


def _metric_definitions(signals: List[Dict[str, Any]]) -> List[str]:
    """Canonical definitions (from the Metric Registry) for the metrics behind
    the given signals. Best-effort — never breaks the briefing."""
    out, seen = [], set()
    try:
        from app.core import metrics
        for s in signals:
            name = _RULE_METRIC.get(s.get("rule"))
            if not name or name in seen:
                continue
            seen.add(name)
            m = metrics.REGISTRY.get(name)
            if m:
                out.append(f"**{m.label}** — {m.definition}")
    except Exception as exc:
        logger.debug(f"[analytics] metric definitions skipped: {exc}")
    return out


# ============================================================================
# Admin endpoint
# ============================================================================

router = APIRouter(tags=["analytics-signals"])


@router.get("/analytics/anomalies")
def analytics_anomalies():
    """Live analytics trend anomalies + the thresholds that define them."""
    sigs = detect_all()
    return {
        "count": len(sigs),
        "anomalies": sigs,
        "briefing": format_briefing(sigs),
        "thresholds": {
            "winrate_drop_pp": WINRATE_DROP_PP, "winrate_min_n": WINRATE_MIN_N,
            "stall_days": STALL_DAYS, "stall_min": STALL_MIN,
            "revenue_drop_pct": REVENUE_DROP_PCT, "revenue_min_base": REVENUE_MIN_BASE,
        },
    }


# ── On-demand "act on this" (blindspot A5) ──────────────────────────────────
# Turns a surfaced anomaly into a GOVERNED action without waiting for the
# scheduled supervisor: composes a bounded plan for the anomaly's breach goal
# via the SAME planner bridge the supervisor uses — reads run, writes queue for
# human approval. Nothing is sent or changed automatically. Gated at _DATA so
# the analytics page can call it (the resulting proposal still needs an admin
# approval in the governance queue — no privilege escalation).
act_router = APIRouter(tags=["analytics-signals"])


@act_router.post("/analytics/anomalies/act")
async def analytics_anomalies_act(body: Dict[str, Any]):
    rule = str(body.get("rule") or "")
    headline = str(body.get("headline") or "")
    if rule not in BREACH_GOALS:
        return {"ok": False, "error": f"unknown anomaly rule '{rule}'"}
    goal = BREACH_GOALS[rule].format(headline=headline or rule)

    # Idempotency: one open plan per goal (reuse the supervisor's dedupe).
    try:
        from app.core import supervisor
        if supervisor._plan_already_queued(goal):
            return {"ok": True, "already_queued": True, "proposed": 0,
                    "note": "A plan for this anomaly is already awaiting approval."}
    except Exception as exc:
        logger.debug(f"[analytics] act dedupe check skipped: {exc}")

    try:
        from app.core import planner
        res = await planner.run_plan(goal)
    except Exception as exc:
        logger.warning(f"[analytics] act plan failed: {exc}")
        return {"ok": False, "error": f"planner failed: {str(exc)[:160]}"}

    if not res.get("ok"):
        return {"ok": False, "error": "; ".join(res.get("errors") or ["planner declined"])}
    proposed = len(res.get("proposed_approvals") or [])
    steps = len(res.get("trace") or [])
    note = (f"Composed a plan ({steps} step(s)); queued {proposed} action(s) for approval."
            if proposed else
            f"Composed a plan ({steps} step(s)); nothing required approval (read-only).")
    return {"ok": True, "rule": rule, "steps": steps, "proposed": proposed,
            "correlation_id": res.get("correlation_id"), "note": note}
