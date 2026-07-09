"""Calibration → governance-proposed tuning (the learning loop's write-side).

The scorer MEASURES itself (intelligence.calibrate: did last month's churn
predictions come true?) but until now the numbers changed nothing. This module
closes the loop SAFELY — the reference vision's "self-improving agents" without
self-mutation:

    evidence   intelligence.calibrate() — per-band churn rates, sample sizes
    rule       deterministic proposal rules (mirror calibrate()'s verdicts):
                 high-band precision < 30% (n≥MIN_SAMPLE) → raise high_band a step
                 low-band miss rate > 20%  (n≥MIN_SAMPLE) → lower medium_band a step
                 bands INVERTED → no auto-proposal (weights need human review)
    propose    governance.propose('tuning.adjust', …evidence attached…) — the
               critic reviews it, it routes to an executive, the one-click email
               carries the case
    ratify     human approves → A2A `tuning.adjust` writes agent_tuning;
               governance undo reverts within the window
    consume    intelligence reads its band thresholds through current()

Safety rails: every parameter lives in TUNABLES with hard min/max bounds;
apply() refuses out-of-bounds and band-inversion; one pending proposal per
param; a cooldown blocks re-proposing right after a change; the weekly
proposer is gated on TUNING_PROPOSALS_ENABLED (default 0).

CONFIG (env)
  TUNING_PROPOSALS_ENABLED  0     weekly proposal job on/off (endpoint can force)
  TUNING_MIN_SAMPLE         10    band sample size below which we don't propose
  TUNING_STEP               0.05  size of one threshold adjustment
  TUNING_COOLDOWN_DAYS      14    days after a change before re-proposing
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("tuning")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("TUNING_PROPOSALS_ENABLED")
MIN_SAMPLE = int(os.getenv("TUNING_MIN_SAMPLE", "10"))
STEP = float(os.getenv("TUNING_STEP", "0.05"))
COOLDOWN_DAYS = int(os.getenv("TUNING_COOLDOWN_DAYS", "14"))

# Every governed parameter: hard bounds the system can NEVER propose or apply
# beyond, plus the code default used when agent_tuning has no row.
TUNABLES: Dict[str, Dict[str, Any]] = {
    "intelligence.high_band": {
        "default": 0.70, "min": 0.50, "max": 0.90,
        "desc": "churn risk at/above which an account is HIGH band "
                "(drives churn_save cadences, supervisor alerts, win-backs)"},
    "intelligence.medium_band": {
        "default": 0.40, "min": 0.20, "max": 0.60,
        "desc": "churn risk at/above which an account is MEDIUM band"},
}

# high_band must stay comfortably above medium_band.
_MIN_BAND_GAP = 0.10


# ============================================================================
# STORE — read / apply / revert
# ============================================================================

def current(param: str) -> float:
    """Effective value: agent_tuning row, else the code default. Raises on an
    unregistered param (typos must fail loudly, not read as 0)."""
    spec = TUNABLES[param]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM agent_tuning WHERE param=%s", (param,))
            r = cur.fetchone()
            return float(r[0]) if r else float(spec["default"])
    except Exception as exc:
        logger.debug(f"[tuning] read {param} fell back to default: {exc}")
        return float(spec["default"])
    finally:
        conn.close()


def all_current() -> Dict[str, Dict[str, Any]]:
    out = {p: {**spec, "value": float(spec["default"]), "overridden": False,
               "updated_at": None, "updated_by": None, "reason": None}
           for p, spec in TUNABLES.items()}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT param, value, prev_value, updated_by, reason, "
                        "updated_at FROM agent_tuning")
            for param, value, prev, by, reason, at in cur.fetchall():
                if param in out:
                    out[param].update(value=float(value), overridden=True,
                                      prev_value=float(prev) if prev is not None else None,
                                      updated_by=by, reason=reason,
                                      updated_at=at.isoformat() if at else None)
    except Exception as exc:
        logger.debug(f"[tuning] all_current fell back to defaults: {exc}")
    finally:
        conn.close()
    return out


def _validate(param: str, value: float) -> None:
    spec = TUNABLES.get(param)
    if not spec:
        raise ValueError(f"unknown tunable '{param}' — "
                         f"choose from {sorted(TUNABLES)}")
    if not (spec["min"] <= value <= spec["max"]):
        raise ValueError(f"{param}={value} outside hard bounds "
                         f"[{spec['min']}, {spec['max']}]")
    # Keep the band ordering sane whichever side moves.
    if param == "intelligence.high_band":
        if value < current("intelligence.medium_band") + _MIN_BAND_GAP:
            raise ValueError(f"high_band {value} would sit within {_MIN_BAND_GAP} "
                             f"of medium_band — inverts the bands")
    if param == "intelligence.medium_band":
        if value > current("intelligence.high_band") - _MIN_BAND_GAP:
            raise ValueError(f"medium_band {value} would sit within {_MIN_BAND_GAP} "
                             f"of high_band — inverts the bands")


def apply(param: str, value: float, updated_by: str = "governance",
          reason: Optional[str] = None) -> Dict[str, Any]:
    """Write a governed parameter (bounds-checked). Records the previous value
    for undo. This is the ONLY writer of agent_tuning."""
    value = round(float(value), 4)
    _validate(param, value)
    prev = current(param)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_tuning (param, value, prev_value, updated_by, reason)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (param) DO UPDATE
                   SET prev_value=agent_tuning.value, value=EXCLUDED.value,
                       updated_by=EXCLUDED.updated_by, reason=EXCLUDED.reason,
                       updated_at=now()""",
                (param, value, prev, updated_by, reason))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[tuning] {param}: {prev} → {value} (by {updated_by}: {reason})")
    return {"ok": True, "param": param, "previous": prev, "value": value}


def revert(param: str, updated_by: str = "governance-undo") -> Dict[str, Any]:
    """Undo the latest change: restore prev_value (refuses when none exists)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value, prev_value FROM agent_tuning WHERE param=%s",
                        (param,))
            r = cur.fetchone()
            if not r:
                return {"ok": False, "error": f"{param} has no override to revert"}
            value, prev = float(r[0]), (float(r[1]) if r[1] is not None else None)
            if prev is None:
                return {"ok": False, "error": f"{param} has no recorded previous value"}
            cur.execute(
                "UPDATE agent_tuning SET value=%s, prev_value=NULL, "
                "updated_by=%s, reason='reverted via governance undo', "
                "updated_at=now() WHERE param=%s",
                (prev, updated_by, param))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[tuning] {param}: reverted {value} → {prev}")
    return {"ok": True, "param": param, "reverted_from": value, "value": prev}


def _changed_recently(param: str, days: int) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agent_tuning WHERE param=%s "
                "AND updated_at > now() - make_interval(days => %s)", (param, days))
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        conn.close()


def _pending_proposal(param: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM action_approvals "
                "WHERE action_type='tuning.adjust' AND status='pending' "
                "  AND params->>'param' = %s "
                "  AND (expires_at IS NULL OR expires_at > now())", (param,))
            return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        conn.close()


# ============================================================================
# PROPOSE — calibration evidence → governance proposals
# ============================================================================

def propose_from_calibration(force: bool = False) -> Dict[str, Any]:
    """Read the churn model's calibration and, when the evidence is strong
    enough, queue bounded threshold adjustments for human approval. Never
    writes a parameter itself. force=True runs even when
    TUNING_PROPOSALS_ENABLED=0 (endpoint/testing)."""
    if not ENABLED and not force:
        return {"enabled": False, "skipped": True}
    from app.core import governance, intelligence

    cal = intelligence.calibrate()
    bands = cal.get("bands") or {}
    high = bands.get("high") or {}
    low = bands.get("low") or {}
    inverted = (high.get("churn_rate") is not None
                and low.get("churn_rate") is not None
                and high["churn_rate"] < low["churn_rate"])

    candidates: List[Dict[str, Any]] = []
    if not inverted:
        if (high.get("n", 0) >= MIN_SAMPLE
                and (high.get("churn_rate") or 0) < 0.30):
            cur_v = current("intelligence.high_band")
            candidates.append({
                "param": "intelligence.high_band",
                "current": cur_v,
                "value": round(min(cur_v + STEP,
                                   TUNABLES["intelligence.high_band"]["max"]), 3),
                "why": (f"high-band precision {high['churn_rate']:.0%} "
                        f"(n={high['n']}) is below 30% — most flagged accounts "
                        f"did NOT churn; raising the threshold cuts false alarms"),
                "evidence": {"band": "high", **high}})
        if (low.get("n", 0) >= MIN_SAMPLE
                and (low.get("churn_rate") or 0) > 0.20):
            cur_v = current("intelligence.medium_band")
            hi_v = current("intelligence.high_band")
            candidates.append({
                "param": "intelligence.medium_band",
                "current": cur_v,
                "value": round(max(min(cur_v - STEP, hi_v - _MIN_BAND_GAP),
                                   TUNABLES["intelligence.medium_band"]["min"]), 3),
                "why": (f"low-band miss rate {low['churn_rate']:.0%} "
                        f"(n={low['n']}) is above 20% — churners are slipping "
                        f"through as 'safe'; lowering the threshold widens the net"),
                "evidence": {"band": "low", **low}})

    proposed, skipped = [], []
    for c in candidates:
        if abs(c["value"] - c["current"]) < 1e-9:
            skipped.append({c["param"]: "already at its bound"})
            continue
        if _pending_proposal(c["param"]):
            skipped.append({c["param"]: "already awaiting approval"})
            continue
        if _changed_recently(c["param"], COOLDOWN_DAYS):
            skipped.append({c["param"]: f"changed within the {COOLDOWN_DAYS}d cooldown"})
            continue
        aid = governance.propose(
            "tuning.adjust", "learning",
            {"param": c["param"], "value": c["value"], "current": c["current"],
             "why": c["why"],
             "evidence": {**c["evidence"],
                          "horizon_days": cal.get("horizon_days"),
                          "window_days": cal.get("window_days"),
                          "bands": bands}},
            confidence=0.6, severity="medium")
        proposed.append({"param": c["param"], "current": c["current"],
                         "value": c["value"], "approval_uuid": aid})
        logger.info(f"[tuning] proposed {c['param']} "
                    f"{c['current']} → {c['value']} ({aid[:8]})")

    return {"enabled": ENABLED, "verdict": cal.get("verdict"),
            "inverted": inverted,
            "note": ("bands INVERTED — component weights need human review; "
                     "no threshold proposal made" if inverted else None),
            "proposed": proposed, "skipped": skipped}


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["tuning"])


@router.get("/tuning/status")
def tuning_status():
    return {"enabled": ENABLED, "min_sample": MIN_SAMPLE, "step": STEP,
            "cooldown_days": COOLDOWN_DAYS, "params": all_current()}


@router.post("/tuning/propose")
async def tuning_propose():
    """Run the calibration→proposal pass now (forced; the weekly job self-gates)."""
    import asyncio
    return await asyncio.to_thread(propose_from_calibration, True)
