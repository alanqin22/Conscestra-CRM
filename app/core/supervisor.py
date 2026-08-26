"""Phase 3 — Proactive supervisor tick.

Promotes the orchestrator from request-scoped to a STANDING loop. On a schedule
it reads the live executive KPI pack (sp_orchestrator('executive')), runs a set
of breach detectors (AR spike, slipped deals, unbilled orders, unworked leads),
and for each NEW breach emits a 'supervisor.alert' event onto the bus — which is
auditable and fans out to the Notifications + Orchestrator agent inboxes. When
SUPERVISOR_AUTOACT is on it also kicks the owning agent's loop (e.g. AR spike →
overdue dunning) — reusing the Phase 1 emitters.

This is the "agents act unprompted" piece: sense (KPIs) → decide (rules) → act
(bus/emit) → record (events) — closed loop, gated and idempotent.

CONFIG (env)
  SUPERVISOR_ENABLED        0     master on/off (scheduled tick is a no-op when 0)
  SUPERVISOR_AUTOACT        0     1 = also trigger owning-agent loops on breach
  SUPERVISOR_AR_OVERDUE_MIN 10    overdue-invoice count that trips an AR alert
  SUPERVISOR_SLIPPED_MIN    10    slipped-deal count that trips a forecast alert
  SUPERVISOR_UNBILLED_MIN   3     unbilled-order count that trips a leakage alert
  SUPERVISOR_UNWORKED_MIN   25    unworked-lead count that trips a coverage alert
  SUPERVISOR_LOWSTOCK_FLOOR 5     stock at/below this = a low-stock product
  SUPERVISOR_LOWSTOCK_MIN   3     low-stock product count that trips inventory_risk
  SUPERVISOR_SENT_MIN_N     5     min scored conversations (7d) before sentiment judges
  SUPERVISOR_SENT_NEG_PCT   40    negative share (%) that trips sentiment_drop
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid as _uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import execute_sp

logger = logging.getLogger("supervisor")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


ENABLED = _flag("SUPERVISOR_ENABLED")
AUTOACT = _flag("SUPERVISOR_AUTOACT")
# Phase 3.5 — planner bridge: on a breach, hand the headline to the bounded
# planner as a GOAL. It composes a multi-agent response play; reads execute,
# writes queue for governance approval (nothing outbound). Staged independently
# of AUTOACT because its only "action" is queuing proposals a human approves —
# so it can ship on well before AUTOACT. Off by default.
PLANNER = _flag("SUPERVISOR_PLANNER")
LOWSTOCK_FLOOR = _int("SUPERVISOR_LOWSTOCK_FLOOR", 5)
LOWSTOCK_MIN   = _int("SUPERVISOR_LOWSTOCK_MIN", 3)
SENT_MIN_N     = _int("SUPERVISOR_SENT_MIN_N", 5)
SENT_NEG_PCT   = _int("SUPERVISOR_SENT_NEG_PCT", 40)
AR_OVERDUE_MIN = _int("SUPERVISOR_AR_OVERDUE_MIN", 10)
SLIPPED_MIN    = _int("SUPERVISOR_SLIPPED_MIN", 10)
UNBILLED_MIN   = _int("SUPERVISOR_UNBILLED_MIN", 3)
UNWORKED_MIN   = _int("SUPERVISOR_UNWORKED_MIN", 25)
CHURN_MIN      = _int("SUPERVISOR_CHURN_MIN", 5)   # high-band at-risk customers

ALERT_DEDUPE_HOURS = 12   # one alert per rule per this window


# ============================================================================
# KPI source + helpers
# ============================================================================

def _load_pack() -> Dict[str, Any]:
    rows = execute_sp("SELECT sp_orchestrator('executive') AS result")
    return (rows[0].get("result") or {}) if rows else {}


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(v) -> str:
    return f"${_num(v):,.0f}"


def _signal(rule, severity, headline, metric, value, owner_agent,
            recommended_action, auto=None) -> Dict[str, Any]:
    return {"rule": rule, "severity": severity, "headline": headline,
            "metric": metric, "value": value, "owner_agent": owner_agent,
            "recommended_action": recommended_action, "auto": auto}


# ============================================================================
# DETECTORS  (pack -> Optional[signal])
# ============================================================================

def detect_ar_spike(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ar = pack.get("ar_summary") or {}
    n = int(_num(ar.get("overdue_count")))
    if n >= AR_OVERDUE_MIN:
        return _signal("ar_spike", "high" if n >= AR_OVERDUE_MIN * 2 else "medium",
                       f"{n} invoices overdue · {_money(ar.get('outstanding_total'))} outstanding",
                       "overdue_count", n, "accounting",
                       "Run overdue-invoice dunning (Accounting → Email)", auto="ar")
    return None


def detect_slipped_deals(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    f = pack.get("forecast") or {}
    n = int(_num(f.get("slipped_count")))
    if n >= SLIPPED_MIN:
        return _signal("slipped_deals", "high" if n >= SLIPPED_MIN * 2 else "medium",
                       f"{n} deals slipped past close date · {_money(f.get('slipped_value'))} at risk",
                       "slipped_count", n, "opportunities",
                       "Re-engage / re-date slipped opportunities")
    return None


def detect_unbilled_orders(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    u = pack.get("unbilled_orders") or {}
    n = int(_num(u.get("count")))
    if n >= UNBILLED_MIN:
        return _signal("unbilled_orders", "medium",
                       f"{n} shipped orders unbilled · {_money(u.get('value'))} revenue leakage",
                       "count", n, "accounting",
                       "Generate invoices for shipped-but-unbilled orders")
    return None


def detect_low_stock(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Inventory risk (2026-07-18): active products at/below the stock floor
    OR short of open-order demand. Own query — the executive pack has no
    inventory view; tolerates the tables being absent."""
    try:
        rows = execute_sp(
            """SELECT count(*) AS n FROM products p WHERE p.is_active
               AND (p.stock_quantity <= %(f)s OR p.stock_quantity <
                    COALESCE((SELECT SUM(oi.quantity)::int FROM order_items oi
                              JOIN orders o ON o.order_id = oi.order_id
                              WHERE oi.product_id = p.product_id
                              AND lower(o.status) IN ('pending','processing')), 0))""",
            {"f": LOWSTOCK_FLOOR})
        n = int(rows[0]["n"]) if rows else 0
    except Exception as exc:
        logger.debug(f"[supervisor] low-stock check skipped: {exc}")
        return None
    if n >= LOWSTOCK_MIN:
        return _signal("inventory_risk",
                       "high" if n >= LOWSTOCK_MIN * 3 else "medium",
                       f"{n} product(s) at/below stock floor ({LOWSTOCK_FLOOR}) "
                       f"or short of open-order demand",
                       "low_stock", n, "products",
                       "Review low-stock products and replenish before orders slip")
    return None


def detect_sentiment_drop(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Proactive revenue protection (2026-07-18): the share of NEGATIVE
    conversations across ALL channels (interaction_memories sentiment labels)
    over 7 days. Needs a minimum sample so one bad call can't trip it."""
    try:
        rows = execute_sp(
            "SELECT count(*) FILTER (WHERE sentiment='negative') AS neg, "
            "       count(*) AS n FROM interaction_memories "
            "WHERE sentiment IS NOT NULL "
            "AND created_at >= now() - interval '7 days'")
        neg = int(rows[0]["neg"]) if rows else 0
        n = int(rows[0]["n"]) if rows else 0
    except Exception as exc:
        logger.debug(f"[supervisor] sentiment check skipped: {exc}")
        return None
    if n >= SENT_MIN_N:
        pct = round(100.0 * neg / n)
        if pct >= SENT_NEG_PCT:
            return _signal("sentiment_drop",
                           "high" if pct >= SENT_NEG_PCT * 1.5 else "medium",
                           f"{neg} of {n} conversations negative in 7d ({pct}%) "
                           f"across all channels",
                           "neg_pct", pct, "accounts",
                           "Review the negative conversations; launch save "
                           "plays for the accounts involved")
    return None


def detect_unworked_leads(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    u = pack.get("unworked_leads") or {}
    n = int(_num(u.get("count")))
    if n >= UNWORKED_MIN:
        return _signal("unworked_leads", "medium",
                       f"{n} leads unworked — pipeline coverage at risk",
                       "count", n, "leads",
                       "Score + auto-schedule outreach for hot leads", auto="leads")
    return None


def detect_churn_risk(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reads the nightly customer-intelligence profile (account_intelligence)
    rather than the KPI pack. Tolerates the table not existing yet."""
    try:
        rows = execute_sp(
            "SELECT count(*) AS n, COALESCE(SUM(ltv),0) AS ltv "
            "FROM account_intelligence WHERE churn_band='high'")
        r = rows[0] if rows else {}
        n, ltv = int(_num(r.get("n"))), _num(r.get("ltv"))
    except Exception as exc:
        logger.debug(f"[supervisor] churn detector skipped: {exc}")
        return None
    if n >= CHURN_MIN:
        return _signal("churn_risk", "high" if n >= CHURN_MIN * 2 else "medium",
                       f"{n} customers at high churn risk · {_money(ltv)} lifetime value exposed",
                       "high_churn_accounts", n, "accounts",
                       "Review /intelligence/at-risk and launch save plays for the top-LTV accounts",
                       auto="churn_campaign")
    return None


def detect_bus_stall(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Ops heartbeat: the whole agent stack rides one process — if the bus
    consumer dies silently, cooperation stops with no error anywhere. Two
    independent checks: (a) bus enabled but the asyncio task is dead;
    (b) handler-type queue rows sitting unclaimed far beyond the poll cadence."""
    try:
        from app.core import agent_bus
        if agent_bus.ENABLED and not (agent_bus._task and not agent_bus._task.done()):
            return _signal("bus_stalled", "high",
                           "Agent bus ENABLED but the consumer loop is not running",
                           "consumer_running", 0, "orchestrator",
                           "Restart the backend; check logs for the crash that "
                           "killed the consumer task")
        stale_after = max(agent_bus.POLL_SECS * 10, 600)
        types = list(agent_bus.HANDLERS.keys())
        if agent_bus.ENABLED and types:
            rows = execute_sp(
                "SELECT count(*) AS n FROM event_queue q "
                "JOIN events e ON e.event_uuid = q.event_uuid "
                "WHERE q.status='pending' AND q.last_attempt_at IS NULL "
                "  AND e.event_type = ANY(%(t)s) "
                "  AND q.created_at BETWEEN now() - interval '1 day' "
                "                       AND now() - make_interval(secs => %(s)s)",
                {"t": types, "s": stale_after})
            n = int(rows[0].get("n") or 0) if rows else 0
            if n:
                return _signal("bus_stalled", "high",
                               f"{n} handler-type event(s) unclaimed for "
                               f">{stale_after // 60} min — consumer may be wedged",
                               "unclaimed_events", n, "orchestrator",
                               "Check GET /agent-bus/status and recent errors; "
                               "restart if the loop is stuck")
    except Exception as exc:
        logger.debug(f"[supervisor] bus heartbeat check failed: {exc}")
    return None


DETECTORS: List[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = [
    detect_ar_spike, detect_slipped_deals, detect_unbilled_orders, detect_unworked_leads,
    detect_churn_risk, detect_bus_stall, detect_low_stock, detect_sentiment_drop,
]

# Analytics trend anomalies (blindspot A1): win-rate WoW drop, stalled deals,
# revenue slump — the period-over-period detectors the Analytics prompt
# promised but nothing computed. Same signal shape, so they ride this loop's
# alert-emit + dedupe + governance + planner bridge (A5) unchanged. Loaded
# defensively so a problem there never disarms the core detectors above.
try:
    from app.core import analytics_signals as _analytics_signals
    DETECTORS += _analytics_signals.DETECTORS
except Exception as _exc:  # pragma: no cover
    logger.warning(f"[supervisor] analytics_signals not loaded: {_exc}")


def detect_escalation_sla(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """U1: an agent promised a human would follow up and the deadline passed.
    The SLA on an escalation is only a commitment if something checks it."""
    try:
        from app.core import escalation
        sigs = escalation.sla_breaches()
        return sigs[0] if sigs else None
    except Exception as exc:
        logger.debug(f"[supervisor] escalation SLA check skipped: {exc}")
        return None


DETECTORS.append(detect_escalation_sla)

# U3: the platform watching ITSELF (machinery degraded, governance drift) —
# loaded defensively so an observability fault never disarms the detectors that
# watch the business.
try:
    from app.core import platform_health as _platform_health
    DETECTORS += _platform_health.DETECTORS
except Exception as _exc:  # pragma: no cover
    logger.warning(f"[supervisor] platform_health not loaded: {_exc}")


# ============================================================================
# ACT (emit alert / auto-act) + idempotency
# ============================================================================

def _already_alerted(rule: str) -> bool:
    rows = execute_sp(
        """SELECT 1 AS x FROM events
           WHERE event_type='supervisor.alert' AND source_system='supervisor'
             AND payload->'context'->>'rule' = %(r)s
             AND created_at > now() - (%(h)s || ' hours')::interval
           LIMIT 1""",
        {"r": rule, "h": ALERT_DEDUPE_HOURS})
    return bool(rows)


def _emit_alert(sig: Dict[str, Any]) -> None:
    # Pack the signal under the canonical envelope's 'context' so emit_event
    # preserves it (it strips top-level non-envelope keys).
    payload = json.dumps({"context": {k: sig[k] for k in
                          ("rule", "severity", "headline", "metric", "value",
                           "owner_agent", "recommended_action")}})
    execute_sp(
        "SELECT emit_event('supervisor.alert','system',%(id)s::uuid,"
        "%(p)s::jsonb,NULL,'supervisor') AS r",
        {"id": str(_uuid.uuid4()), "p": payload})


AUTOACT_CONF = float(os.getenv("SUPERVISOR_AUTOACT_CONF", "0.85"))


def _govern_autoact(action: str, sig: Dict[str, Any]) -> Optional[str]:
    """Gate a supervisor auto-action through governance policy. Returns a note
    when governance DIVERTED the action (proposed/skipped); None means the
    policy says act — caller executes. With default thresholds (0.85 ≥ ACT_MIN
    0.8) behavior is unchanged but now policy-evaluated; tightening GOV_ACT_MIN
    above SUPERVISOR_AUTOACT_CONF routes these into the approval queue."""
    from app.core import governance
    if not governance.ENABLED:
        return None
    d = governance.decide(AUTOACT_CONF)
    if d == "act":
        return None
    if d == "skip":
        return f"{action} skipped by governance (conf {AUTOACT_CONF} < propose_min)"
    rows = execute_sp(
        "SELECT count(*) AS n FROM action_approvals "
        "WHERE action_type=%(a)s AND status='pending'", {"a": action})
    if rows and int(rows[0].get("n") or 0):
        return f"{action} already awaiting approval"
    aid = governance.propose(action, "supervisor", {"cap": 25},
                             severity=sig.get("severity"), confidence=AUTOACT_CONF)
    return f"proposed {action} for approval ({aid[:8]})"


def _autoact(sig: Dict[str, Any]) -> Optional[str]:
    if sig.get("auto") == "ar":
        note = _govern_autoact("supervisor.emit_dunning", sig)
        if note:
            return note
        execute_sp("SELECT fn_emit_overdue_invoice_events(25) AS r")
        return "emitted overdue-invoice events"
    if sig.get("auto") == "leads":
        note = _govern_autoact("supervisor.emit_hot_leads", sig)
        if note:
            return note
        execute_sp("SELECT fn_emit_hot_lead_events(25) AS r")
        return "emitted hot-lead events"
    if sig.get("auto") == "churn_campaign":
        # An agent INITIATING commercial action — so it goes through policy:
        # the campaign is PROPOSED to the governance queue and routed to the
        # right executive; only an approval executes campaign.winback (which
        # itself stays draft-only without AUTOSEND). Idempotent: one open
        # proposal / recent campaign at a time.
        from app.core import governance
        rows = execute_sp(
            "SELECT count(*) AS n FROM action_approvals "
            "WHERE action_type='campaign.winback' AND status='pending'")
        if rows and int(rows[0].get("n") or 0):
            return "win-back campaign already awaiting approval"
        rows = execute_sp(
            "SELECT count(*) AS n FROM marketing_campaigns "
            "WHERE name LIKE 'Win-back%%' AND created_at > now() - interval '7 days'")
        if rows and int(rows[0].get("n") or 0):
            return "win-back campaign already ran this week"
        aid = governance.propose(
            "campaign.winback", "supervisor",
            {"segment": {"churn_band": ["high"]},
             "goal": "win back high-churn-risk customers detected by the supervisor"},
            severity=sig.get("severity"), confidence=0.6)
        return f"proposed win-back campaign for approval ({aid[:8]})"
    return None


# ============================================================================
# PLANNER BRIDGE (Phase 3.5) — a breach becomes a governed multi-agent play
# ============================================================================

def _breach_goal(sig: Dict[str, Any]) -> Optional[str]:
    """Turn a breach signal into a plain-language GOAL for the planner. Returns
    None for breaches with no business play (e.g. the ops bus_stall heartbeat)."""
    headline = sig.get("headline") or ""
    # Analytics trend anomalies (A1) contribute their own goals so the planner
    # bridge composes a governed play for them too (A5). Merged first; the
    # explicit mapping below wins on any key collision.
    mapping: Dict[str, str] = {}
    try:
        for _k, _tmpl in _analytics_signals.BREACH_GOALS.items():
            mapping[_k] = _tmpl.format(headline=headline)
    except Exception:
        pass
    mapping.update({
        "ar_spike":
            f"Reduce overdue receivables ({headline}). Identify the overdue "
            f"invoices and send payment reminders to the customers who owe.",
        "slipped_deals":
            f"Recover slipped pipeline ({headline}). Review the slipped "
            f"opportunities and re-engage them to pull the deals back in.",
        "unbilled_orders":
            f"Close revenue leakage ({headline}). Find the shipped-but-unbilled "
            f"orders and generate their invoices.",
        "unworked_leads":
            f"Improve pipeline coverage ({headline}). Identify the hot unworked "
            f"leads and schedule outreach for them.",
        "churn_risk":
            f"Reduce churn ({headline}). Review the high-risk accounts and "
            f"launch save plays for the top-value ones.",
        "inventory_risk":
            f"Address inventory risk ({headline}). List the low-stock products "
            f"and summarize what needs replenishing before open orders slip.",
        "sentiment_drop":
            f"Address declining customer sentiment ({headline}). Identify the "
            f"accounts behind the recent negative conversations and propose "
            f"save actions.",
    })
    return mapping.get(sig.get("rule"))


def _plan_already_queued(goal: str) -> bool:
    """Idempotency: a plan for this exact goal already sits pending approval.
    Planner writes are tagged with params->>'plan_goal' (see planner.run_plan),
    so one active play per breach goal at a time."""
    try:
        rows = execute_sp(
            "SELECT 1 AS x FROM action_approvals "
            "WHERE status='pending' AND params->>'plan_goal' = %(g)s LIMIT 1",
            {"g": goal})
        return bool(rows)
    except Exception as exc:
        logger.debug(f"[supervisor] plan-dedupe check failed: {exc}")
        return False


def _run_coro(coro) -> Any:
    """Run an async coroutine from this SYNC tick. The tick always runs off the
    event loop (APScheduler threadpool job / asyncio.to_thread), so asyncio.run
    is the normal path; the running-loop branch is belt-and-braces for any
    future caller that invokes the tick from within a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)          # no loop here — the normal case
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


async def _dispatch_plan(goal: str, cid: str) -> Dict[str, Any]:
    """Execute the plan through the typed A2A layer (crm.plan_execute) so the
    play is audited + correlation-chained in one place — reads run, writes queue
    for governance. Never sends anything outbound."""
    from app.core.a2a import A2ARequest, Principal, dispatch
    res = await dispatch(A2ARequest(
        intent="crm.plan_execute", from_agent="supervisor",
        # Unattended work is not anonymous work. `from_agent` names the
        # component; the principal names the authority, and a write now
        # requires one. A scheduled supervisor tick that could not say who it
        # was would be indistinguishable from a request with a lost identity.
        principal=Principal.service("supervisor"),
        params={"goal": goal}, correlation_id=cid))
    return {"ok": res.ok, "error": res.error, "data": res.data}


def _plan_for_breach(sig: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    """Compose + execute a governed plan for a breach. Returns (note, queued) —
    `queued` is the number of write proposals now pending for this breach (a
    pre-existing plan counts as 1) — or None when the breach has no goal. The
    caller uses queued>0 to suppress the legacy AUTOACT path so the two don't
    double-act. Writes are proposed, never executed here — safe without AUTOACT."""
    goal = _breach_goal(sig)
    if not goal:
        return None
    if _plan_already_queued(goal):
        return "plan already queued for approval", 1
    cid = str(_uuid.uuid4())
    res = _run_coro(_dispatch_plan(goal, cid))
    if not res or not res.get("ok"):
        return f"plan dispatch failed: {(res or {}).get('error')}", 0
    data = res.get("data") or {}
    steps = len(data.get("trace") or [])
    proposed = len(data.get("proposed_approvals") or [])
    note = (f"planned {steps} step(s), {proposed} queued for approval"
            if proposed else f"planned {steps} step(s) (read-only, nothing to approve)")
    return note, proposed


def _briefing(breaches: List[Dict[str, Any]]) -> str:
    if not breaches:
        return "### 🛰️ Supervisor — all clear\nNo KPI breaches detected."
    icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    out = [f"### 🛰️ Supervisor Briefing — {len(breaches)} issue(s) need attention", ""]
    for b in breaches:
        out.append(f"- {icon.get(b['severity'], '•')} **{b['headline']}**  ")
        out.append(f"  → {b['recommended_action']}  _(owner: {b['owner_agent']})_")
    return "\n".join(out)


# ============================================================================
# TICK
# ============================================================================

def run_supervisor_tick(force: bool = False) -> Dict[str, Any]:
    """Sense → decide → act. Safe to call directly (scheduler / admin endpoint).
    force=True runs even when SUPERVISOR_ENABLED=0 (for on-demand testing)."""
    if not ENABLED and not force:
        return {"enabled": False, "skipped": True}
    try:
        pack = _load_pack()
    except Exception as exc:
        logger.error(f"[supervisor] failed to load KPI pack: {exc}", exc_info=True)
        return {"enabled": ENABLED, "error": str(exc)}

    breaches, alerted, acted = [], [], []
    for det in DETECTORS:
        try:
            sig = det(pack)
        except Exception as exc:
            logger.warning(f"[supervisor] detector {det.__name__} failed: {exc}")
            continue
        if not sig:
            continue
        breaches.append(sig)
        if _already_alerted(sig["rule"]):
            continue
        try:
            _emit_alert(sig)
            alerted.append(sig["rule"])
            # Planner bridge FIRST (independent of AUTOACT): compose a governed
            # multi-agent response play; writes queue for approval — safe.
            planned_writes = 0
            if PLANNER:
                presult = _plan_for_breach(sig)
                if presult:
                    pnote, planned_writes = presult
                    acted.append({f"{sig['rule']}/plan": pnote})
            # AUTOACT only when the planner did NOT already queue a governed play
            # for this breach — otherwise both paths act on it (e.g. both dun the
            # same AR spike). The planner's composed play takes precedence.
            if AUTOACT and sig.get("auto") and planned_writes == 0:
                note = _autoact(sig)
                if note:
                    acted.append({sig["rule"]: note})
        except Exception as exc:
            logger.error(f"[supervisor] act on {sig['rule']} failed: {exc}", exc_info=True)

    summary = {"enabled": ENABLED, "autoact": AUTOACT, "planner": PLANNER,
               "checked": len(DETECTORS),
               "breaches": [b["rule"] for b in breaches], "alerted": alerted,
               "acted": acted, "briefing": _briefing(breaches)}

    # Goal-oriented pass (Phase 8): detectors watch symptoms, objectives
    # pursue outcomes. Best-effort — a failure here never breaks the tick.
    try:
        from app.core import objectives
        obj = objectives.run_objectives_pass(force=force)
        if not obj.get("skipped"):
            summary["objectives"] = {
                "evaluated": obj.get("evaluated"),
                "alerted": obj.get("alerted"), "acted": obj.get("acted")}
            if obj.get("briefing"):
                summary["briefing"] += "\n\n" + obj["briefing"]
    except Exception as exc:
        logger.warning(f"[supervisor] objectives pass failed: {exc}")
    if breaches:
        logger.info(f"[supervisor] tick — breaches={summary['breaches']} "
                    f"alerted={alerted} acted={acted}")
    return summary


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["supervisor"])


@router.get("/supervisor/status")
def supervisor_status():
    return {"enabled": ENABLED, "autoact": AUTOACT, "planner": PLANNER,
            "detectors": [d.__name__ for d in DETECTORS],
            "thresholds": {"ar_overdue_min": AR_OVERDUE_MIN, "slipped_min": SLIPPED_MIN,
                           "unbilled_min": UNBILLED_MIN, "unworked_min": UNWORKED_MIN,
                           "churn_min": CHURN_MIN}}


@router.post("/supervisor/run-once")
async def supervisor_run_once():
    import asyncio
    return await asyncio.to_thread(run_supervisor_tick, True)
