"""Phase 8 — Goal-oriented supervisor (business objectives).

The reference vision's defining trait: agents pursue OBJECTIVES ("cut overdue
invoices in half in 90 days") rather than react to hard-coded thresholds.
Objectives live in `business_objectives` (sql/business_objectives.sql); each
pass — every supervisor tick plus a nightly job — this module:

    sense    compute each active objective's metric (pure SQL, no LLM)
    project  where SHOULD the metric be today on the linear baseline→target
             path over the horizon? (standing guardrails compare to target)
    judge    achieved | on_track | at_risk | off_track — deterministic,
             explainable (value, expected, slack all reported)
    record   one snapshot per objective per day → trend + trajectory history
    alert    at_risk/off_track → 'objective.at_risk' bus event (audited,
             fans out to exec in_app + agent inboxes; 24h dedupe)
    act      OBJECTIVES_AUTOACT=1: an off-track objective with a `play`
             runs the owning agent's loop — through the SAME governed
             path as supervisor auto-actions (supervisor._autoact →
             governance policy → approval queue when policy says so)

Detectors watch symptoms; objectives pursue outcomes. Both coexist.

CONFIG (env)
  OBJECTIVES_ENABLED   0   master on/off (pass is a no-op when 0)
  OBJECTIVES_AUTOACT   0   1 = off-track objectives may run their play
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.database import execute_sp, get_connection

logger = logging.getLogger("objectives")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("OBJECTIVES_ENABLED")
AUTOACT = _flag("OBJECTIVES_AUTOACT")

ALERT_DEDUPE_HOURS = 24      # one at-risk alert per objective per this window
SLACK_FRACTION = 0.15        # tolerance band, as a fraction of |target-baseline|
TREND_LOOKBACK_DAYS = 3      # compare against the newest snapshot at least this old


# ============================================================================
# METRICS — metric key → live value (direct SQL; every metric tolerates
# missing tables by returning None, so a partial deployment never breaks the
# pass). Seed baselines in sql/business_objectives.sql use the SAME queries.
# ============================================================================

METRICS: Dict[str, Dict[str, str]] = {
    "overdue_invoice_count": {
        "label": "Overdue invoices", "unit": "count",
        "sql": "SELECT count(*)::float AS v FROM invoices "
               "WHERE status='overdue' AND deleted_at IS NULL"},
    "ar_outstanding": {
        "label": "Overdue AR balance", "unit": "$",
        "sql": "SELECT COALESCE(sum(balance_due),0)::float AS v FROM invoices "
               "WHERE status='overdue' AND deleted_at IS NULL"},
    "high_churn_accounts": {
        "label": "High-churn-band customers", "unit": "count",
        "sql": "SELECT count(*)::float AS v FROM account_intelligence "
               "WHERE churn_band='high'"},
    "lead_conversion_rate_30d": {
        "label": "Lead conversion (30d)", "unit": "%",
        "sql": "SELECT COALESCE(round(100.0 * count(*) FILTER (WHERE status='converted')"
               " / NULLIF(count(*),0), 1), 0)::float AS v "
               "FROM leads WHERE deleted_at IS NULL "
               "AND created_at > now() - interval '30 days'"},
    "revenue_30d": {
        "label": "Order revenue (30d)", "unit": "$",
        "sql": "SELECT COALESCE(sum(total_amount),0)::float AS v FROM orders "
               "WHERE deleted_at IS NULL AND created_at > now() - interval '30 days'"},
    "new_lead_backlog": {
        "label": "Untouched new leads", "unit": "count",
        "sql": "SELECT count(*)::float AS v FROM leads "
               "WHERE deleted_at IS NULL AND status='new'"},
}

# Plays an off-track objective may trigger — the supervisor's governed
# auto-actions (supervisor._autoact routes each through governance policy).
PLAYS = {
    "ar":             "kick the Accounting dunning loop",
    "leads":          "kick the hot-lead outreach loop",
    "churn_campaign": "propose a win-back campaign (governance-routed)",
}


def metric_value(metric: str) -> Optional[float]:
    spec = METRICS.get(metric)
    if not spec:
        return None
    try:
        rows = execute_sp(spec["sql"])
        return float(rows[0]["v"]) if rows and rows[0].get("v") is not None else None
    except Exception as exc:
        logger.debug(f"[objectives] metric {metric} unavailable: {exc}")
        return None


# ============================================================================
# EVALUATION — deterministic trajectory judgment
# ============================================================================

def evaluate(direction: str, baseline: float, target: float, value: float,
             horizon_start: Optional[date], horizon_end: Optional[date],
             today: Optional[date] = None) -> Dict[str, Any]:
    """Judge an objective. Timed objectives are held to the LINEAR path from
    baseline (at horizon_start) to target (at horizon_end) with a slack band
    of SLACK_FRACTION·|target-baseline|; standing guardrails (no horizon)
    compare the value to the target directly."""
    today = today or date.today()
    up = direction == "up"
    ahead = (lambda a, b: a >= b) if up else (lambda a, b: a <= b)

    if ahead(value, target):
        return {"status": "achieved", "expected": float(target), "behind_by": 0.0}

    if horizon_end is None:                       # standing guardrail
        gap = abs(value - target) / max(abs(target), 1.0)
        return {"status": "at_risk" if gap <= 0.25 else "off_track",
                "expected": float(target),
                "behind_by": round(abs(value - target), 2)}

    total = max((horizon_end - (horizon_start or today)).days, 1)
    elapsed = min(max((today - (horizon_start or today)).days / total, 0.0), 1.0)
    expected = baseline + (target - baseline) * elapsed
    span = max(abs(target - baseline), 1.0)
    slack = SLACK_FRACTION * span
    behind = (expected - value) if up else (value - expected)
    regressed = (value < baseline - slack) if up else (value > baseline + slack)

    if behind <= slack:
        status = "on_track"
    elif regressed or behind > 3 * slack:
        status = "off_track"
    else:
        status = "at_risk"
    return {"status": status, "expected": round(expected, 2),
            "behind_by": round(max(behind, 0.0), 2), "elapsed": round(elapsed, 3)}


def _trend(objective_uuid: str, value: float, direction: str) -> Optional[str]:
    """improving / worsening / flat vs the newest snapshot ≥3 days old."""
    try:
        rows = execute_sp(
            "SELECT value::float AS v FROM objective_snapshots "
            "WHERE objective_uuid=%(o)s::uuid AND snapshot_date <= %(d)s "
            "ORDER BY snapshot_date DESC LIMIT 1",
            {"o": objective_uuid,
             "d": date.today() - timedelta(days=TREND_LOOKBACK_DAYS)})
    except Exception:
        return None
    if not rows:
        return None
    prev = float(rows[0]["v"])
    delta = value - prev
    if abs(delta) < max(abs(prev), 1.0) * 0.01:
        return "flat"
    better = delta > 0 if direction == "up" else delta < 0
    return "improving" if better else "worsening"


# ============================================================================
# PERSISTENCE
# ============================================================================

def _load_active() -> List[Dict[str, Any]]:
    return execute_sp(
        "SELECT objective_uuid::text AS objective_uuid, name, metric, direction, "
        "       baseline_value::float AS baseline, target_value::float AS target, "
        "       horizon_start, horizon_end, owner_agent, play, status "
        "FROM business_objectives WHERE status='active' ORDER BY created_at")


def _snapshot(objective_uuid: str, value: float, expected: Optional[float],
              status: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO objective_snapshots "
                "(objective_uuid, snapshot_date, value, expected_value, eval_status) "
                "VALUES (%s::uuid, CURRENT_DATE, %s, %s, %s) "
                "ON CONFLICT (objective_uuid, snapshot_date) DO UPDATE "
                "SET value=EXCLUDED.value, expected_value=EXCLUDED.expected_value, "
                "    eval_status=EXCLUDED.eval_status",
                (objective_uuid, value, expected, status))
        conn.commit()
    finally:
        conn.close()


def _set_status(objective_uuid: str, status: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE business_objectives SET status=%s, updated_at=now() "
                "WHERE objective_uuid=%s::uuid", (status, objective_uuid))
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# ALERT + ACT (mirrors the supervisor's idempotent emit pattern)
# ============================================================================

def _already_alerted(objective_uuid: str) -> bool:
    rows = execute_sp(
        """SELECT 1 AS x FROM events
           WHERE event_type='objective.at_risk' AND source_system='objectives'
             AND payload->'context'->>'objective_uuid' = %(o)s
             AND created_at > now() - (%(h)s || ' hours')::interval
           LIMIT 1""",
        {"o": objective_uuid, "h": ALERT_DEDUPE_HOURS})
    return bool(rows)


def _emit(event_type: str, objective_uuid: str, context: Dict[str, Any]) -> None:
    # Business data must ride under 'context' — emit_event strips any other
    # top-level payload key.
    execute_sp(
        "SELECT emit_event(%(t)s,'system',%(id)s::uuid,"
        "%(p)s::jsonb,NULL,'objectives') AS r",
        {"t": event_type, "id": objective_uuid,
         "p": json.dumps({"context": context})})


def _run_play(play: str, severity: str) -> Optional[str]:
    """Route the play through the supervisor's governed auto-actions."""
    from app.core import supervisor
    return supervisor._autoact({"auto": play, "severity": severity})


# ============================================================================
# PASS  (sense → project → judge → record → alert → act)
# ============================================================================

def run_objectives_pass(force: bool = False) -> Dict[str, Any]:
    """Evaluate every active objective. Safe to call from the supervisor tick,
    the nightly job, or the admin endpoint. force=True runs even when
    OBJECTIVES_ENABLED=0 (on-demand testing)."""
    if not ENABLED and not force:
        return {"enabled": False, "skipped": True}

    results, alerted, acted = [], [], []
    for obj in _load_active():
        oid = obj["objective_uuid"]
        value = metric_value(obj["metric"])
        if value is None:
            results.append({**obj, "value": None, "eval": "metric_unavailable"})
            continue
        ev = evaluate(obj["direction"], obj["baseline"], obj["target"], value,
                      obj["horizon_start"], obj["horizon_end"])
        trend = _trend(oid, value, obj["direction"])
        row = {**obj, "lifecycle": obj["status"], "value": value, "trend": trend, **ev}
        results.append(row)
        try:
            _snapshot(oid, value, ev.get("expected"), ev["status"])
        except Exception as exc:
            logger.warning(f"[objectives] snapshot failed for {obj['name']}: {exc}")

        try:
            if ev["status"] == "achieved":
                # Timed objectives COMPLETE on target (one-shot event via the
                # status transition); standing guardrails stay active silently.
                if obj["horizon_end"] is not None:
                    _set_status(oid, "achieved")
                    _emit("objective.achieved", oid, {
                        "objective_uuid": oid, "name": obj["name"],
                        "metric": obj["metric"], "value": value,
                        "target": obj["target"]})
                continue
            if obj["horizon_end"] and date.today() > obj["horizon_end"]:
                _set_status(oid, "expired")
                continue
            if ev["status"] in ("at_risk", "off_track") and not _already_alerted(oid):
                unit = METRICS[obj["metric"]]["unit"]
                headline = (f"Objective '{obj['name']}' {ev['status']}: "
                            f"{METRICS[obj['metric']]['label']} at {value:g}{unit if unit == '%' else ''}"
                            f" vs expected {ev['expected']:g} (target {obj['target']:g})")
                _emit("objective.at_risk", oid, {
                    "objective_uuid": oid, "name": obj["name"],
                    "metric": obj["metric"], "value": value,
                    "target": obj["target"], "expected": ev["expected"],
                    "status": ev["status"], "trend": trend,
                    "owner_agent": obj["owner_agent"], "play": obj["play"],
                    "headline": headline})
                alerted.append(obj["name"])
                if AUTOACT and ev["status"] == "off_track" and obj["play"] in PLAYS:
                    note = _run_play(obj["play"], "high")
                    if note:
                        acted.append({obj["name"]: note})
        except Exception as exc:
            logger.error(f"[objectives] act on {obj['name']} failed: {exc}",
                         exc_info=True)

    summary = {"enabled": ENABLED, "autoact": AUTOACT,
               "evaluated": len(results), "alerted": alerted, "acted": acted,
               "objectives": results, "briefing": _briefing(results)}
    if alerted or acted:
        logger.info(f"[objectives] pass — alerted={alerted} acted={acted}")
    return summary


def _briefing(results: List[Dict[str, Any]]) -> str:
    if not results:
        return ""
    icon = {"achieved": "🏁", "on_track": "🟢", "at_risk": "🟠",
            "off_track": "🔴", "metric_unavailable": "⚪"}
    out = ["### 🎯 Business Objectives"]
    for r in results:
        st = r.get("status") or r.get("eval")
        line = f"- {icon.get(st, '•')} **{r['name']}** — "
        if r.get("value") is None:
            out.append(line + "metric unavailable")
            continue
        line += f"{r['value']:g} → target {r['target']:g} ({st}"
        if r.get("trend"):
            line += f", {r['trend']}"
        out.append(line + ")")
    return "\n".join(out)


def report() -> List[Dict[str, Any]]:
    """Live read-only view (list endpoint, A2A capability, CEO briefing):
    every non-expired objective with current value, judgment and trend."""
    rows = execute_sp(
        "SELECT objective_uuid::text AS objective_uuid, name, metric, direction, "
        "       baseline_value::float AS baseline, target_value::float AS target, "
        "       horizon_start, horizon_end, owner_agent, play, status "
        "FROM business_objectives WHERE status IN ('active','paused','achieved') "
        "ORDER BY status, created_at")
    out = []
    for obj in rows:
        value = metric_value(obj["metric"]) if obj["status"] == "active" else None
        # 'lifecycle' = DB state (active/paused/achieved); 'status' becomes the
        # live judgment (achieved/on_track/at_risk/off_track) when evaluated.
        item = {**obj, "lifecycle": obj["status"], "value": value,
                "unit": METRICS.get(obj["metric"], {}).get("unit"),
                "label": METRICS.get(obj["metric"], {}).get("label")}
        item["horizon_start"] = str(obj["horizon_start"]) if obj["horizon_start"] else None
        item["horizon_end"] = str(obj["horizon_end"]) if obj["horizon_end"] else None
        if value is not None:
            item.update(evaluate(obj["direction"], obj["baseline"], obj["target"],
                                 value, obj["horizon_start"], obj["horizon_end"]))
            item["trend"] = _trend(obj["objective_uuid"], value, obj["direction"])
        out.append(item)
    return out


# ============================================================================
# CRUD + admin endpoints
# ============================================================================

class ObjectiveCreate(BaseModel):
    name: str
    metric: str
    direction: str                      # 'up' | 'down'
    target_value: float
    horizon_days: Optional[int] = 90    # None/0 = standing guardrail
    owner_agent: str = "orchestrator"
    play: Optional[str] = None          # PLAYS key
    baseline_value: Optional[float] = None   # default: current metric value


def create(spec: ObjectiveCreate, created_by: str = "api") -> str:
    if spec.metric not in METRICS:
        raise ValueError(f"unknown metric '{spec.metric}' — "
                         f"choose from {sorted(METRICS)}")
    if spec.direction not in ("up", "down"):
        raise ValueError("direction must be 'up' or 'down'")
    if spec.play and spec.play not in PLAYS:
        raise ValueError(f"unknown play '{spec.play}' — choose from {sorted(PLAYS)}")
    baseline = spec.baseline_value
    if baseline is None:
        baseline = metric_value(spec.metric)
        if baseline is None:
            raise ValueError(f"metric '{spec.metric}' unavailable — "
                             "pass baseline_value explicitly")
    horizon_end = (date.today() + timedelta(days=spec.horizon_days)
                   if spec.horizon_days else None)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO business_objectives (name, metric, direction, "
                "  baseline_value, target_value, horizon_end, owner_agent, play, "
                "  created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING objective_uuid::text",
                (spec.name, spec.metric, spec.direction, baseline,
                 spec.target_value, horizon_end, spec.owner_agent, spec.play,
                 created_by))
            oid = cur.fetchone()[0]
        conn.commit()
        return oid
    finally:
        conn.close()


router = APIRouter(tags=["objectives"])


@router.get("/objectives/status")
def objectives_status():
    return {"enabled": ENABLED, "autoact": AUTOACT,
            "metrics": sorted(METRICS), "plays": PLAYS,
            "slack_fraction": SLACK_FRACTION,
            "alert_dedupe_hours": ALERT_DEDUPE_HOURS}


@router.get("/objectives/list")
def objectives_list():
    return {"objectives": report()}


@router.post("/objectives")
def objectives_create(spec: ObjectiveCreate):
    try:
        oid = create(spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"objective_uuid": oid}


@router.post("/objectives/{objective_uuid}/pause")
def objectives_pause(objective_uuid: str):
    _set_status(objective_uuid, "paused")
    return {"objective_uuid": objective_uuid, "status": "paused"}


@router.post("/objectives/{objective_uuid}/resume")
def objectives_resume(objective_uuid: str):
    _set_status(objective_uuid, "active")
    return {"objective_uuid": objective_uuid, "status": "active"}


@router.post("/objectives/run-once")
async def objectives_run_once():
    import asyncio
    return await asyncio.to_thread(run_objectives_pass, True)
