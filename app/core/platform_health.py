"""Platform Self-Observability — U3 (round-2 blindspots, 2026-07-25).

THE GAP THIS CLOSES
    `agent_ops.py` (#4) measures what the AI workforce ACHIEVES: containment,
    CSAT, cost per conversation. Nothing measured whether the workforce itself
    is FUNCTIONING. The proof is that a 12,000-event queue backlog was found by
    accident, not by an alert — the platform had no way to notice its own
    failure, and we would have learned of an agent-fleet outage from a customer.

    A company dashboard reading "revenue excellent, CSAT excellent" tells you
    nothing about whether the phones are down and 12,000 orders are stuck in the
    warehouse. This module is the second dashboard.

THREE LAYERS — because the platform now makes promises and grants exceptions
    PLATFORM HEALTH       is the machinery running?
      event queue depth · failed/stuck events · LLM error rate · LLM latency ·
      budget exhaustion. (`llm_usage.ok` and `.latency_ms` have been WRITTEN
      since the meter shipped and never once READ — this is where they finally
      get used.)

    CUSTOMER OBLIGATIONS  are we keeping the promises we made? (U1)
      open escalations · at-risk (deadline approaching) · breached ·
      unreachable (promised a follow-up to someone we cannot contact).
      U1 gave the platform the ability to PROMISE; a promise nobody watches is
      the failure U1 existed to fix, reintroduced one level up.

    GOVERNANCE HEALTH     are our own controls being respected? (U2)
      safety-gate overrides · agents live without a passing evaluation ·
      approval queue depth + expiry. U2 made the gate overridable ON PURPOSE;
      one override is a judgement call, seventeen in a day is a broken process,
      and only a cross-agent view can tell those apart.

EVERY probe is defensive: a missing table or column degrades that ONE metric to
`null` with a note, never breaks the report. An observability tool that goes
down with the thing it observes is worse than none.

Exposed as `GET /platform/health` (admin) + `platform-health.html`, and the
alert-worthy subset rides the EXISTING supervisor detector loop, so breaches
reach people through the paths already built rather than a new channel.

CONFIG (env)
  PLATFORM_HEALTH_ENABLED   1     kill switch
  PH_QUEUE_WARN            500    pending events → warning
  PH_QUEUE_CRIT           5000    pending events → critical
  PH_QUEUE_AGE_WARN_HOURS    6    oldest pending event age → warning
  PH_QUEUE_AGE_CRIT_HOURS   24    oldest pending event age → critical
  PH_LLM_ERROR_WARN_PCT      5    LLM error rate % → warning
  PH_LATENCY_WARN_MS      6000    avg LLM latency → warning
  PH_SLA_RISK_MINUTES       30    escalation "at risk" window before its deadline
  PH_OVERRIDE_WARN_DAILY     3    gate overrides in 24h → governance warning
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("platform_health")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


ENABLED = _flag("PLATFORM_HEALTH_ENABLED", "1")
QUEUE_WARN = _int("PH_QUEUE_WARN", 500)
QUEUE_CRIT = _int("PH_QUEUE_CRIT", 5000)
# Age matters more than depth: a small queue that never drains is a stalled
# consumer, which is exactly the failure a depth threshold sails past.
QUEUE_AGE_WARN_HOURS = _int("PH_QUEUE_AGE_WARN_HOURS", 6)
QUEUE_AGE_CRIT_HOURS = _int("PH_QUEUE_AGE_CRIT_HOURS", 24)
LLM_ERROR_WARN_PCT = _int("PH_LLM_ERROR_WARN_PCT", 5)
LATENCY_WARN_MS = _int("PH_LATENCY_WARN_MS", 6000)
SLA_RISK_MINUTES = _int("PH_SLA_RISK_MINUTES", 30)
OVERRIDE_WARN_DAILY = _int("PH_OVERRIDE_WARN_DAILY", 3)

OK, WARN, CRIT, UNKNOWN = "ok", "warning", "critical", "unknown"


def _metric(key: str, label: str, value: Any, state: str = OK,
            detail: str = "", unit: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "value": value, "state": state,
            "detail": detail, "unit": unit}


def _qall(sql: str, params: tuple = ()) -> Optional[List[tuple]]:
    """Defensive multi-row read (see _q)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[platform_health] probe failed: {str(exc)[:140]}")
        return None
    finally:
        conn.close()


def _q(sql: str, params: tuple = ()) -> Optional[tuple]:
    """One defensive read. Returns None on ANY failure (missing table, bad
    column, unreachable DB) so a single gap cannot take the report down."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[platform_health] probe failed: {str(exc)[:140]}")
        return None
    finally:
        conn.close()


# ============================================================================
# 1. PLATFORM HEALTH — is the machinery running?
# ============================================================================

def _handler_types() -> Optional[List[str]]:
    """Event types the bus actually consumes. Everything else stays 'pending'
    by design and is inert, not backlog.

    Returns None when EVERY type is consumable — with AGENT_BUS_CATCHALL=1 the
    orchestrator's handle_default settles unhandled types too, so there is no
    inert class at all. Missing that (as the first cut of this module did)
    under-reports the backlog wherever catchall is on, which is the case
    locally."""
    try:
        from app.core.agent_bus import HANDLERS, CATCHALL
        if CATCHALL:
            return None
        return sorted(HANDLERS)
    except Exception:
        return []


def platform_metrics() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    # -- Event queue depth. The metric whose absence let 12k events pile up.
    #
    #    ONLY events whose type has a REGISTERED HANDLER are counted as backlog.
    #    agent_bus deliberately leaves handler-less types pending forever, so
    #    including them would put a permanent floor under this number: the
    #    dashboard would sit red on a healthy system, and a metric that is
    #    always red gets ignored within a week. Found on the live DB — of 70
    #    pending, 20 (account.created, product.stock_changed) have no handler
    #    and are inert by design, while 50 with handlers were up to 13 days old.
    handled = _handler_types()
    catchall = handled is None      # every type is consumable → no inert class
    if handled:
        row = _q("""SELECT count(*) FILTER (WHERE q.status='pending'
                                            AND e.event_type = ANY(%s)),
                           count(*) FILTER (WHERE q.status='failed'),
                           count(*) FILTER (WHERE q.status='processing'),
                           min(q.created_at) FILTER (WHERE q.status='pending'
                                            AND e.event_type = ANY(%s))
                    FROM event_queue q JOIN events e USING (event_uuid)""",
                 (handled, handled))
    else:
        row = _q("""SELECT count(*) FILTER (WHERE status='pending'),
                           count(*) FILTER (WHERE status='failed'),
                           count(*) FILTER (WHERE status='processing'),
                           min(created_at) FILTER (WHERE status='pending')
                    FROM event_queue""")
    if row is None:
        out.append(_metric("queue_depth", "Event queue depth", None, UNKNOWN,
                           "event_queue not readable"))
    else:
        pending, failed, processing, oldest = row
        state = CRIT if pending >= QUEUE_CRIT else (WARN if pending >= QUEUE_WARN else OK)
        age = ""
        hrs = 0.0
        if oldest:
            from datetime import datetime, timezone
            hrs = (datetime.now(timezone.utc) - oldest).total_seconds() / 3600
            age = f" · oldest {hrs:.1f}h old"
            # DEPTH ALONE IS THE WRONG ALARM. A queue of 70 whose oldest entry is
            # 13 days old is stalled; a queue of 400 draining in seconds is
            # healthy. Found on the live DB the first time this report ran —
            # count was under threshold while nothing had moved in 320 hours.
            if hrs >= QUEUE_AGE_CRIT_HOURS:
                state = CRIT
            elif hrs >= QUEUE_AGE_WARN_HOURS and state == OK:
                state = WARN
        out.append(_metric("queue_depth",
                           "Event backlog" if catchall
                           else "Event backlog (handled types)",
                           int(pending), state,
                           f"threshold {QUEUE_WARN}/{QUEUE_CRIT} events, "
                           f"{QUEUE_AGE_WARN_HOURS}/{QUEUE_AGE_CRIT_HOURS}h age{age}"
                           + (" — NOT DRAINING" if hrs >= QUEUE_AGE_WARN_HOURS else ""),
                           "events"))
        # ORPHANS: pending, dispatchable events created BEFORE the running
        # consumer's cutoff. Not a slow queue — work the bus has silently
        # decided not to do. Invisible until 2026-07-25, when 50 such events
        # aged up to 13 days were found; every restart used to create more.
        try:
            from app.core import agent_bus
            orph = agent_bus.orphaned_sync()
            n_orph = orph.get("orphaned")
            if n_orph is None:
                out.append(_metric("queue_orphaned", "Orphaned events", None,
                                   UNKNOWN, orph.get("error") or "unreadable"))
            else:
                out.append(_metric(
                    "queue_orphaned", "Orphaned (before cutoff)", int(n_orph),
                    WARN if n_orph else OK,
                    (f"never processed, oldest {str(orph.get('oldest'))[:10]} — "
                     "POST /agent-bus/drain to action them deliberately")
                    if n_orph else "no events stranded behind the cutoff",
                    "events"))
        except Exception as exc:
            out.append(_metric("queue_orphaned", "Orphaned events", None, UNKNOWN,
                               f"agent_bus unreadable: {str(exc)[:60]}"))

        # Reported separately and never alerted on: visible, but it cannot make
        # a healthy platform look broken.
        if handled:
            inert = _q("""SELECT count(*) FROM event_queue q
                          JOIN events e USING (event_uuid)
                          WHERE q.status='pending' AND NOT (e.event_type = ANY(%s))""",
                       (handled,))
            n_inert = int(inert[0]) if inert else 0
            out.append(_metric(
                "queue_inert", "Pending, no handler", n_inert, OK,
                "emitted for audit/blackboard only — never consumed, by design"
                if n_inert else "none", "events"))
        # DRAIN RATE — throughput, not just depth. Catches a stalled consumer
        # independently of age: pending work with zero completions in the last
        # hour means nothing is moving, whatever the queue's size or age.
        rate_row = _q("""SELECT count(*) FROM event_queue
                         WHERE status='completed'
                           AND last_attempt_at > now() - interval '1 hour'""")
        rate = int(rate_row[0]) if rate_row else 0
        if pending and not rate:
            r_state, r_detail = CRIT, ("nothing completed in the last hour while "
                                       f"{int(pending)} wait — consumer stalled?")
        elif pending and rate:
            eta = pending / rate
            r_state = WARN if eta > 6 else OK
            r_detail = f"{int(pending)} pending would clear in ~{eta:.1f}h at this rate"
        else:
            r_state, r_detail = OK, "queue empty" if not pending else ""
        out.append(_metric("queue_drain_rate", "Drain rate (last hour)", rate,
                           r_state, r_detail, "events/h"))

        # Per-handler breakdown, shown only when something is actually failing —
        # a permanent empty table is noise, and "which handler" is the first
        # question anyone asks once the count is non-zero.
        fail_detail = "no exhausted events"
        if failed:
            brk = _qall("""SELECT e.event_type, count(*), max(q.last_error)
                           FROM event_queue q JOIN events e USING (event_uuid)
                           WHERE q.status='failed' GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
            if brk:
                fail_detail = " · ".join(
                    f"{t}: {n} ({(err or '')[:40]})" for t, n, err in brk)
            else:
                fail_detail = "events that exhausted their retries"
        out.append(_metric("queue_failed", "Failed events", int(failed),
                           CRIT if failed else OK, fail_detail, "events"))
        # 'processing' rows are claimed by a worker. Many, and stale, means a
        # consumer died holding the lock — the wedge that looks like health.
        stale = _q("""SELECT count(*) FROM event_queue
                      WHERE status='processing'
                        AND locked_at < now() - interval '15 minutes'""")
        n_stale = int(stale[0]) if stale else 0
        out.append(_metric("queue_stuck", "Stuck (locked >15m)", n_stale,
                           CRIT if n_stale else OK,
                           "a consumer may have died mid-event" if n_stale
                           else f"{int(processing)} in flight", "events"))

    # -- LLM error rate + latency. Both columns have been written since the
    #    meter shipped and never read until now.
    row = _q("""SELECT count(*), count(*) FILTER (WHERE NOT ok),
                       round(avg(latency_ms)), round(max(latency_ms))
                FROM llm_usage WHERE at > now() - interval '24 hours'""")
    if row is None or not row[0]:
        out.append(_metric("llm_error_rate", "LLM error rate (24h)", None, UNKNOWN,
                           "no LLM calls recorded in the last 24h"))
        out.append(_metric("llm_latency", "LLM avg latency (24h)", None, UNKNOWN, ""))
    else:
        total, errors, avg_ms, max_ms = row
        pct = round(100.0 * (errors or 0) / total, 2)
        out.append(_metric(
            "llm_error_rate", "LLM error rate (24h)", pct,
            CRIT if pct >= LLM_ERROR_WARN_PCT * 3 else
            (WARN if pct >= LLM_ERROR_WARN_PCT else OK),
            f"{errors} failed of {total} calls · warn ≥{LLM_ERROR_WARN_PCT}%", "%"))
        out.append(_metric(
            "llm_latency", "LLM avg latency (24h)", int(avg_ms or 0),
            WARN if (avg_ms or 0) >= LATENCY_WARN_MS else OK,
            f"peak {int(max_ms or 0)}ms · warn ≥{LATENCY_WARN_MS}ms", "ms"))

    # -- FAILOVER READINESS (U5): a configured-but-unusable failover target must
    #    be visible BEFORE an outage, not discovered during one. Proven usable by
    #    a real generation, never a catalogue lookup — `gemini-2.5-flash` is
    #    LISTED by Google and 404s on use, exactly like the misconfigured
    #    `gpt-oss:20b` Ollama model.
    try:
        from app.core import llm_router
        rd = llm_router.readiness()
        if not rd.get("enabled"):
            out.append(_metric(
                "failover_readiness", "LLM failover", "off", OK,
                rd.get("note", "disabled"), ""))
        else:
            bad = [t for t in rd.get("targets", []) if not t.get("usable")]
            out.append(_metric(
                "failover_readiness", "LLM failover",
                "ready" if not bad else "broken",
                OK if not bad else WARN,
                ("all targets usable: " + ", ".join(
                    f"{t['provider']}/{t['model']}" for t in rd["targets"]))
                if not bad else
                ("configured but UNUSABLE: " + "; ".join(
                    f"{t['provider']}/{t['model']} — {t['detail'][:50]}"
                    for t in bad)), ""))
    except Exception as exc:
        out.append(_metric("failover_readiness", "LLM failover", None, UNKNOWN,
                           f"router unreadable: {str(exc)[:60]}", ""))

    # -- Budget exhaustion: a caller over budget is silently running on
    #    deterministic fallbacks — degraded, not down, which is why nobody notices.
    try:
        from app.core import llm_meter
        callers = (llm_meter.usage_summary(1) or {}).get("callers", {})
        over = [c for c, v in callers.items()
                if isinstance(v, dict) and v.get("budget_today")
                and (v.get("spent_today") or 0) >= v["budget_today"]]
        out.append(_metric("budget_exhausted", "Callers over LLM budget", len(over),
                           WARN if over else OK,
                           (", ".join(over) + " running on deterministic fallbacks")
                           if over else "no caller has hit its daily cap", "callers"))
    except Exception as exc:
        out.append(_metric("budget_exhausted", "Callers over LLM budget", None,
                           UNKNOWN, f"meter unreadable: {str(exc)[:60]}"))

    return out


# ============================================================================
# 2. CUSTOMER OBLIGATIONS — are we keeping the promises we made? (U1)
# ============================================================================

def obligation_metrics() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    row = _q(f"""SELECT count(*),
                        count(*) FILTER (WHERE sla_due_at < now()),
                        count(*) FILTER (WHERE sla_due_at >= now()
                              AND sla_due_at < now() + interval '{SLA_RISK_MINUTES} minutes'),
                        count(*) FILTER (WHERE NOT contact_known),
                        min(sla_due_at)
                 FROM escalations WHERE status IN ('open','assigned')""")
    if row is None:
        out.append(_metric("escalations_open", "Open escalations", None, UNKNOWN,
                           "escalations table not readable "
                           "(apply sql/escalations.sql?)"))
        return out
    total, breached, at_risk, unreachable, oldest = row
    out.append(_metric("escalations_open", "Open escalations", int(total), OK,
                       "customers waiting on a human", "open"))
    out.append(_metric(
        "escalations_breached", "SLA breached", int(breached),
        CRIT if breached else OK,
        "promised follow-ups now overdue" if breached
        else "every promise still within its deadline", "overdue"))
    out.append(_metric(
        "escalations_at_risk", "SLA at risk", int(at_risk),
        WARN if at_risk else OK,
        f"deadline inside {SLA_RISK_MINUTES} min", "soon"))
    out.append(_metric(
        "escalations_unreachable", "Unreachable customers", int(unreachable),
        WARN if unreachable else OK,
        "we promised a follow-up to someone we have no email or phone for"
        if unreachable else "every open escalation has a contact route", "open"))
    return out


# ============================================================================
# 3. GOVERNANCE HEALTH — are our own controls being respected? (U2)
# ============================================================================

def governance_metrics() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    # -- Safety-gate overrides. One is a judgement call; a cluster is a process
    #    that stopped working. Only a cross-agent, time-windowed view shows it.
    row = _q("""SELECT count(*) FILTER (WHERE published_at > now() - interval '24 hours'),
                       count(*) FILTER (WHERE published_at > now() - interval '30 days')
                FROM custom_agent_versions
                WHERE COALESCE((evaluation->>'forced')::boolean, false)""")
    if row is None:
        out.append(_metric("gate_overrides", "Safety-gate overrides", None, UNKNOWN,
                           "custom_agent_versions not readable "
                           "(apply sql/custom_agent_versions.sql?)"))
    else:
        d1, d30 = int(row[0]), int(row[1])
        out.append(_metric(
            "gate_overrides", "Gate overrides (24h)", d1,
            CRIT if d1 >= OVERRIDE_WARN_DAILY * 2 else (WARN if d1 else OK),
            f"{d30} in the last 30 days · warn ≥{OVERRIDE_WARN_DAILY}/day"
            if d1 else f"{d30} in the last 30 days", "overrides"))

    # -- Live agents that never passed an evaluation. Not an override — they
    #    simply never met the gate (pre-U2 baselines, or a forced publish).
    row = _q("""SELECT count(*) FROM custom_agent_versions
                WHERE status='published' AND eval_passed IS NOT TRUE""")
    if row is not None:
        n = int(row[0])
        out.append(_metric(
            "live_never_passed", "Live without a passing evaluation", n,
            WARN if n else OK,
            "pre-U2 baselines or forced publishes — evaluate them to clear this"
            if n else "every live agent passed its gate", "agents"))

    # -- Approval queue: the HITL path itself can back up or silently expire.
    row = _q("""SELECT count(*) FILTER (WHERE status='pending'),
                       count(*) FILTER (WHERE status='pending'
                             AND expires_at IS NOT NULL AND expires_at < now()),
                       count(*) FILTER (WHERE status='expired'
                             AND created_at > now() - interval '7 days')
                FROM action_approvals""")
    if row is None:
        out.append(_metric("approvals_pending", "Approvals pending", None, UNKNOWN,
                           "action_approvals not readable"))
    else:
        pending, overdue, expired7 = int(row[0]), int(row[1]), int(row[2])
        out.append(_metric("approvals_pending", "Approvals pending", pending,
                           WARN if overdue else OK,
                           f"{overdue} past their expiry" if overdue
                           else "none past expiry", "waiting"))
        out.append(_metric(
            "approvals_expired", "Approvals expired (7d)", expired7,
            WARN if expired7 else OK,
            "proposals nobody decided before they lapsed" if expired7
            else "nothing lapsed undecided", "expired"))
    return out


# ============================================================================
# The report
# ============================================================================

def _worst(metrics: List[Dict[str, Any]]) -> str:
    states = {m["state"] for m in metrics}
    for s in (CRIT, WARN, UNKNOWN):
        if s in states:
            return s
    return OK


def health() -> Dict[str, Any]:
    """The full self-observability report: three sections, one overall state."""
    if not ENABLED:
        return {"ok": False, "error": "platform health disabled"}
    sections = [
        {"key": "platform", "label": "Platform health",
         "question": "Is the machinery running?",
         "metrics": platform_metrics()},
        {"key": "obligations", "label": "Customer obligations",
         "question": "Are we keeping the promises we made?",
         "metrics": obligation_metrics()},
        {"key": "governance", "label": "Governance health",
         "question": "Are our own controls being respected?",
         "metrics": governance_metrics()},
    ]
    for s in sections:
        s["state"] = _worst(s["metrics"])
    overall = _worst([m for s in sections for m in s["metrics"]])
    problems = [f"{m['label']}: {m['value']}{m['unit']}"
                for s in sections for m in s["metrics"]
                if m["state"] in (CRIT, WARN)]
    return {"ok": True, "state": overall, "sections": sections,
            "problems": problems,
            "summary": ("everything nominal" if overall == OK
                        else " · ".join(problems[:6]))}


# ============================================================================
# Supervisor detectors — breaches ride the EXISTING alert path
# ============================================================================

def _signal(rule: str, severity: str, headline: str, metric: str, value: Any,
            action: str) -> Dict[str, Any]:
    return {"rule": rule, "severity": severity, "headline": headline,
            "metric": metric, "value": value, "owner_agent": "orchestrator",
            "recommended_action": action}


def detect_platform_degraded(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One detector for the machinery: queue backlog, stuck/failed events, LLM
    errors. Reports the WORST live problem rather than one alert per metric —
    an alert storm during an incident is its own outage."""
    try:
        metrics = platform_metrics()
    except Exception as exc:
        logger.debug(f"[platform_health] detector skipped: {exc}")
        return None
    bad = [m for m in metrics if m["state"] in (CRIT, WARN)]
    if not bad:
        return None
    bad.sort(key=lambda m: 0 if m["state"] == CRIT else 1)
    worst = bad[0]
    return _signal(
        "platform_degraded",
        "critical" if worst["state"] == CRIT else "warning",
        f"Agent platform degraded — {worst['label']}: {worst['value']}"
        f"{worst['unit']}" + (f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""),
        worst["key"], worst["value"],
        f"{worst['detail']}. Open /platform-health.html for the full picture.")


def detect_governance_drift(pack: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """U2's gate was made overridable on purpose. This is what notices when the
    exception becomes the routine."""
    try:
        metrics = {m["key"]: m for m in governance_metrics()}
    except Exception:
        return None
    ov = metrics.get("gate_overrides") or {}
    n = ov.get("value") or 0
    if not isinstance(n, int) or n < OVERRIDE_WARN_DAILY:
        return None
    return _signal(
        "governance_drift", "critical" if n >= OVERRIDE_WARN_DAILY * 2 else "warning",
        f"{n} agent safety-gate override(s) in 24h — the exception is becoming "
        "the process",
        "gate_overrides", n,
        "Review GET /agent-gate-overrides: who is overriding, and whether the "
        "evaluation is wrong or the changes are.")


DETECTORS = [detect_platform_degraded, detect_governance_drift]


# ============================================================================
# Router (admin)
# ============================================================================

router = APIRouter(tags=["platform-health"])


@router.get("/platform/health")
def api_health():
    return health()


@router.get("/platform/health/section/{key}")
def api_section(key: str):
    fn = {"platform": platform_metrics, "obligations": obligation_metrics,
          "governance": governance_metrics}.get(key)
    if not fn:
        return {"ok": False, "error": "unknown section"}
    return {"ok": True, "key": key, "metrics": fn()}
