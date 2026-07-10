"""Predictive lead scoring v2 (advanced improvement #5).

The rule-based Fit+Intent score (fn_score_lead) stays the operational
score; THIS module predicts what sales actually cares about — P(convert) —
by learning from the leads the business already settled:

    dataset    settled leads (converted=1, disqualified=0) → 6 deterministic
               features (rule score, has_email/phone/company, enriched)
    train      pure-Python logistic regression (no ML dependencies; the
               coefficients ARE the explanation), stratified holdout,
               Brier score vs the predict-the-base-rate baseline
    candidate  every trained version lands in lead_scoring_model as a
               CANDIDATE with its metrics — nothing changes yet
    activate   a governed action (scoring.activate, A2A) — critic checks the
               evidence (sample size, lift over baseline, vs current model),
               an executive approves, undo restores the previous version
    predict    consumers (qualification card, context packs) use the single
               ACTIVE version, with per-feature contributions attached;
               no active model → the band-history heuristic still answers

Same doctrine as calibration→tuning: the system trains and proposes;
people ratify. A model can never activate itself.

CONFIG (env)
  SCORING_TRAIN_ENABLED  0    weekly train→propose job on/off
  SCORING_MIN_SAMPLES    30   settled leads required to train
  SCORING_MIN_POSITIVE   5    conversions required in the training set
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException

from app.core.database import get_connection

logger = logging.getLogger("scoring")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("SCORING_TRAIN_ENABLED")
MIN_SAMPLES = int(os.getenv("SCORING_MIN_SAMPLES", "30"))
MIN_POSITIVE = int(os.getenv("SCORING_MIN_POSITIVE", "5"))

FEATURES = ["intercept", "score_norm", "has_email", "has_phone",
            "has_company", "enriched"]

_EPOCHS, _LR, _L2 = 400, 0.5, 0.01


# ============================================================================
# DATASET
# ============================================================================

def _featurize(lead: Dict[str, Any]) -> List[float]:
    return [
        1.0,
        min(max(float(lead.get("score") or 0) / 100.0, 0.0), 1.0),
        1.0 if (lead.get("email") or "").strip() else 0.0,
        1.0 if (lead.get("phone") or "").strip() else 0.0,
        1.0 if (lead.get("company") or "").strip() else 0.0,
        1.0 if ((lead.get("industry") or "").strip()
                or (lead.get("website") or "").strip()) else 0.0,
    ]


def _settled_leads() -> List[Tuple[List[float], int]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT score, email, phone, company, industry, website,
                          (COALESCE(converted, false) OR status='converted')::int AS y
                   FROM leads
                   WHERE deleted_at IS NULL
                     AND (COALESCE(converted, false)
                          OR status IN ('converted', 'disqualified'))""")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return [(_featurize(r), int(r["y"])) for r in rows]


# ============================================================================
# LOGISTIC REGRESSION — pure Python, deterministic, explainable
# ============================================================================

def _sigmoid(z: float) -> float:
    if z < -30:
        return 1e-13
    if z > 30:
        return 1.0 - 1e-13
    return 1.0 / (1.0 + math.exp(-z))


def _fit(data: List[Tuple[List[float], int]]) -> List[float]:
    w = [0.0] * len(FEATURES)
    n = len(data)
    for _ in range(_EPOCHS):
        grad = [0.0] * len(w)
        for x, y in data:
            err = _sigmoid(sum(wi * xi for wi, xi in zip(w, x))) - y
            for j, xj in enumerate(x):
                grad[j] += err * xj
        for j in range(len(w)):
            reg = _L2 * w[j] if j > 0 else 0.0     # don't regularize intercept
            w[j] -= _LR * (grad[j] / n + reg)
    return [round(x, 6) for x in w]


def _brier(w: List[float], data: List[Tuple[List[float], int]]) -> float:
    if not data:
        return 1.0
    return sum((_sigmoid(sum(wi * xi for wi, xi in zip(w, x))) - y) ** 2
               for x, y in data) / len(data)


def train() -> Dict[str, Any]:
    """Train a CANDIDATE model on settled leads. Refuses on thin/one-sided
    history; stores the version with holdout metrics; activates nothing."""
    data = _settled_leads()
    positives = sum(y for _, y in data)
    negatives = len(data) - positives
    if len(data) < MIN_SAMPLES or positives < MIN_POSITIVE or negatives < MIN_POSITIVE:
        return {"ok": False,
                "error": f"insufficient settled history to train: "
                         f"{len(data)} settled ({positives} converted / "
                         f"{negatives} disqualified) — need ≥{MIN_SAMPLES} with "
                         f"≥{MIN_POSITIVE} of each outcome"}

    rng = random.Random(42)                      # deterministic split
    pos = [d for d in data if d[1] == 1]
    neg = [d for d in data if d[1] == 0]
    rng.shuffle(pos)
    rng.shuffle(neg)
    cut_p, cut_n = max(1, len(pos) // 5), max(1, len(neg) // 5)
    holdout = pos[:cut_p] + neg[:cut_n]
    trainset = pos[cut_p:] + neg[cut_n:]

    w = _fit(trainset)
    base_rate = sum(y for _, y in trainset) / len(trainset)
    brier = round(_brier(w, holdout), 4)
    baseline = round(sum((base_rate - y) ** 2 for _, y in holdout) / len(holdout), 4)

    metrics = {"samples": len(data), "positives": positives,
               "negatives": negatives, "holdout": len(holdout),
               "brier": brier, "baseline_brier": baseline,
               "lift": round(baseline - brier, 4), "base_rate": round(base_rate, 3)}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO lead_scoring_model (features, coefficients, metrics)
                   VALUES (%s::jsonb, %s::jsonb, %s::jsonb)
                   RETURNING version""",
                (json.dumps(FEATURES), json.dumps(w), json.dumps(metrics)))
            version = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[scoring] trained candidate v{version}: {metrics}")
    return {"ok": True, "version": version, "coefficients": dict(zip(FEATURES, w)),
            "metrics": metrics}


# ============================================================================
# MODEL STORE — active model + governed activation
# ============================================================================

def _model_row(where: str, params=()) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT version, features, coefficients, metrics, active,
                           trained_at
                    FROM lead_scoring_model {where}
                    ORDER BY version DESC LIMIT 1""", params)
            r = cur.fetchone()
            if not r:
                return None
            return {"version": r[0], "features": r[1], "coefficients": r[2],
                    "metrics": r[3], "active": r[4],
                    "trained_at": r[5].isoformat() if r[5] else None}
    except Exception as exc:
        logger.debug(f"[scoring] model read skipped: {exc}")
        return None
    finally:
        conn.close()


def active_model() -> Optional[Dict[str, Any]]:
    return _model_row("WHERE active")


def activate(version: int, activated_by: str = "governance") -> Dict[str, Any]:
    """Make one version the active model (the governed scoring.activate
    action). Returns the previous version for undo."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM lead_scoring_model WHERE active")
            prev = (cur.fetchone() or [None])[0]
            cur.execute("SELECT 1 FROM lead_scoring_model WHERE version=%s",
                        (int(version),))
            if not cur.fetchone():
                return {"ok": False, "error": f"model v{version} not found"}
            cur.execute("UPDATE lead_scoring_model SET active=false WHERE active")
            cur.execute(
                """UPDATE lead_scoring_model
                   SET active=true, activated_at=now(), activated_by=%s
                   WHERE version=%s""", (activated_by, int(version)))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[scoring] activated model v{version} (prev v{prev})")
    return {"ok": True, "version": int(version), "previous_version": prev}


def deactivate_to(previous_version: Optional[int]) -> Dict[str, Any]:
    """Undo helper: restore the previous version (or no active model)."""
    if previous_version:
        return activate(int(previous_version), activated_by="governance-undo")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE lead_scoring_model SET active=false WHERE active")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "version": None, "note": "no model active"}


# ============================================================================
# PREDICT
# ============================================================================

def predict(lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """P(convert) for a lead dict under the ACTIVE model (None when there is
    none — callers fall back to the band-history heuristic)."""
    m = active_model()
    if not m:
        return None
    x = _featurize(lead)
    w = [float(c) for c in m["coefficients"]]
    p = _sigmoid(sum(wi * xi for wi, xi in zip(w, x)))
    contributions = {f: round(wi * xi, 3)
                     for f, wi, xi in zip(m["features"], w, x) if f != "intercept"}
    return {"probability": round(p, 3), "model_version": m["version"],
            "contributions": contributions,
            "basis": f"predictive model v{m['version']} "
                     f"(trained on {m['metrics'].get('samples')} settled leads)"}


def predict_for(lead_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT score, email, phone, company, industry, website
                   FROM leads WHERE lead_id=%s::uuid AND deleted_at IS NULL""",
                (lead_id,))
            r = cur.fetchone()
            if not r:
                return None
            cols = [d[0] for d in cur.description]
            return predict(dict(zip(cols, r)))
    except Exception as exc:
        logger.debug(f"[scoring] predict_for skipped: {exc}")
        return None
    finally:
        conn.close()


# ============================================================================
# WEEKLY TRAIN → PROPOSE (the learning-loop doctrine)
# ============================================================================

def train_and_propose(force: bool = False) -> Dict[str, Any]:
    """Train a candidate; when it beats the baseline (and there's no pending
    activation), PROPOSE activating it through governance. Never activates."""
    if not ENABLED and not force:
        return {"enabled": False, "skipped": True}
    from app.core import governance

    res = train()
    if not res.get("ok"):
        return {"enabled": ENABLED, "trained": False, "reason": res.get("error")}
    metrics = res["metrics"]
    if metrics["lift"] <= 0:
        return {"enabled": ENABLED, "trained": True, "version": res["version"],
                "proposed": None,
                "reason": f"candidate v{res['version']} shows no lift over the "
                          f"base rate (brier {metrics['brier']} vs baseline "
                          f"{metrics['baseline_brier']}) — not proposed"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM action_approvals "
                "WHERE action_type='scoring.activate' AND status='pending' "
                "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1")
            pending = cur.fetchone() is not None
    finally:
        conn.close()
    if pending:
        return {"enabled": ENABLED, "trained": True, "version": res["version"],
                "proposed": None, "reason": "an activation is already awaiting approval"}
    cur_m = active_model()
    aid = governance.propose(
        "scoring.activate", "scoring",
        {"version": res["version"], "metrics": metrics,
         "coefficients": res["coefficients"],
         "replaces_version": (cur_m or {}).get("version"),
         "why": (f"candidate v{res['version']} beats the base-rate baseline "
                 f"(brier {metrics['brier']} vs {metrics['baseline_brier']}, "
                 f"n={metrics['samples']})")},
        confidence=0.6, severity="medium")
    logger.info(f"[scoring] proposed activation of v{res['version']} ({aid[:8]})")
    return {"enabled": ENABLED, "trained": True, "version": res["version"],
            "proposed": {"approval_uuid": aid}, "metrics": metrics}


def activate_sp(p: Dict[str, Any]) -> Dict[str, Any]:
    """A2A structured handler for scoring.activate."""
    return activate(int(p.get("version") or 0))


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["scoring"])


@router.get("/scoring/status")
def scoring_status():
    m = active_model()
    latest = _model_row("")
    return {"train_enabled": ENABLED, "min_samples": MIN_SAMPLES,
            "features": FEATURES, "active": m, "latest_candidate": latest}


@router.post("/scoring/train")
async def scoring_train():
    """Train a candidate + propose activation if it earns it (forced;
    the weekly job self-gates)."""
    import asyncio
    return await asyncio.to_thread(train_and_propose, True)


@router.get("/scoring/predict/{lead_id}")
def scoring_predict(lead_id: str):
    p = predict_for(lead_id)
    if p is None:
        raise HTTPException(status_code=404,
                            detail="no active model (or lead not found) — "
                                   "band-history heuristic applies")
    return p
