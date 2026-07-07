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
"""

from __future__ import annotations

import json
import logging
import os
import uuid as _uuid
from typing import Any, Callable, Dict, List, Optional

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
    detect_churn_risk, detect_bus_stall,
]


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
            if AUTOACT and sig.get("auto"):
                note = _autoact(sig)
                if note:
                    acted.append({sig["rule"]: note})
        except Exception as exc:
            logger.error(f"[supervisor] act on {sig['rule']} failed: {exc}", exc_info=True)

    summary = {"enabled": ENABLED, "autoact": AUTOACT, "checked": len(DETECTORS),
               "breaches": [b["rule"] for b in breaches], "alerted": alerted,
               "acted": acted, "briefing": _briefing(breaches)}
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
    return {"enabled": ENABLED, "autoact": AUTOACT,
            "detectors": [d.__name__ for d in DETECTORS],
            "thresholds": {"ar_overdue_min": AR_OVERDUE_MIN, "slipped_min": SLIPPED_MIN,
                           "unbilled_min": UNBILLED_MIN, "unworked_min": UNWORKED_MIN,
                           "churn_min": CHURN_MIN}}


@router.post("/supervisor/run-once")
async def supervisor_run_once():
    import asyncio
    return await asyncio.to_thread(run_supervisor_tick, True)
