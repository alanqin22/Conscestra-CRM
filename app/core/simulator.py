"""Scenario simulation — read-only what-if over the objectives math (audit #6).

    simulate: cut overdue invoices by 30%
    what if revenue grows 15% next quarter?

The executive layer could sense (KPIs), judge (objectives) and alert — but not
answer "what would happen IF". This module closes that, the Conscestra way:

    parse    the LLM (lite tier) maps the scenario onto ONE registered metric
             (objectives.METRICS) + a relative % change or an absolute set-to
             value; a deterministic keyword/percent fallback covers the
             no-LLM path. Anything it can't map is refused with the metric
             catalog — the simulator never invents a metric.
    ground   the metric's LIVE value (objectives.metric_value) — every
             simulation starts from reality, not an assumed number.
    project  pure math, reusing objectives.evaluate(): the scenario value is
             re-judged against every ACTIVE objective on that metric —
             status before → after, gap to target closed. One bounded
             deterministic ripple: overdue count ↔ overdue AR move
             proportionally (avg balance per overdue invoice), clearly
             labeled an estimate.
    render   chat markdown. READ-ONLY by construction: this module has no
             write path — nothing is persisted, proposed, or sent.

CONFIG (env)
  SIMULATE_ENABLED  1   kill switch for the handle + endpoints
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core import objectives

logger = logging.getLogger("simulator")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("SIMULATE_ENABLED", "1")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# keyword → metric for the deterministic fallback parser
_METRIC_WORDS = [
    (("overdue", "invoice"), "overdue_invoice_count"),
    (("ar",), "ar_outstanding"),
    (("receivable",), "ar_outstanding"),
    (("outstanding",), "ar_outstanding"),
    (("churn",), "high_churn_accounts"),
    (("conversion",), "lead_conversion_rate_30d"),
    (("revenue",), "revenue_30d"),
    (("sales",), "revenue_30d"),
    (("backlog",), "new_lead_backlog"),
    (("untouched", "lead"), "new_lead_backlog"),
    (("new", "lead"), "new_lead_backlog"),
    (("lead",), "lead_conversion_rate_30d"),
    (("invoice",), "overdue_invoice_count"),
]


# ============================================================================
# PARSE — scenario text → {metric, change_pct | set_to}
# ============================================================================

def _parse_fallback(text: str) -> Optional[Dict[str, Any]]:
    """Deterministic parse: metric keywords + a signed % (or halve/double)."""
    low = text.lower()
    metric = next((m for words, m in _METRIC_WORDS
                   if all(w in low for w in words)), None)
    if not metric:
        return None
    pct = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", low)
    if m:
        pct = float(m.group(1))
    elif "halve" in low or "half" in low:
        pct = 50.0
    elif "double" in low:
        pct = 100.0
    if pct is None:
        return None
    negative = any(w in low for w in ("cut", "reduce", "drop", "lower", "halve",
                                      "half", "shrink", "decrease", "down"))
    return {"metric": metric, "change_pct": -pct if negative else pct}


def _parse_llm(text: str) -> Optional[Dict[str, Any]]:
    catalog = "\n".join(f"- {k}: {v['label']} ({v['unit']})"
                        for k, v in objectives.METRICS.items())
    try:
        from app.core.graph_utils import _get_llm
        resp = _get_llm(tier="lite", caller="simulator").invoke([
            {"role": "system", "content":
                "Map a business what-if scenario onto EXACTLY ONE registered "
                "metric. Reply with ONLY JSON: {\"metric\": <key from the "
                "list>, \"change_pct\": <signed float, e.g. cutting by 30% = "
                "-30>, \"set_to\": <absolute new value, only when the "
                "scenario states one>}. Use change_pct OR set_to, never "
                "both. If the scenario fits no listed metric, reply "
                "{\"metric\": null}.\n\nMetrics:\n" + catalog},
            {"role": "user", "content": text[:400]}])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        m = _JSON_RE.search(raw)
        parsed = json.loads(m.group(0)) if m else None
        if not parsed or parsed.get("metric") not in objectives.METRICS:
            return None
        out: Dict[str, Any] = {"metric": parsed["metric"]}
        if parsed.get("set_to") is not None:
            out["set_to"] = float(parsed["set_to"])
        elif parsed.get("change_pct") is not None:
            out["change_pct"] = float(parsed["change_pct"])
        else:
            return None
        return out
    except Exception as exc:
        logger.debug(f"[simulate] LLM parse failed (fallback): {exc}")
        return None


def _parse(text: str) -> Optional[Dict[str, Any]]:
    return _parse_llm(text) or _parse_fallback(text)


# ============================================================================
# PROJECT — pure math over the live value + active objectives
# ============================================================================

def _fmt(v: float, unit: str) -> str:
    if unit == "$":
        return f"${v:,.0f}"
    if unit == "%":
        return f"{v:.1f}%"
    return f"{v:,.0f}"


def _ripple(metric: str, current: float, scen: float) -> Optional[Dict[str, Any]]:
    """The one bounded deterministic ripple: overdue invoice count and overdue
    AR balance move proportionally (average balance per overdue invoice)."""
    pair = {"overdue_invoice_count": "ar_outstanding",
            "ar_outstanding": "overdue_invoice_count"}.get(metric)
    if not pair or current <= 0:
        return None
    other = objectives.metric_value(pair)
    if other is None or other <= 0:
        return None
    est = other * (scen / current)
    spec = objectives.METRICS[pair]
    return {"metric": pair, "label": spec["label"], "unit": spec["unit"],
            "current": other, "estimate": round(est, 2),
            "note": "proportional estimate (average per overdue invoice)"}


def simulate(text: str) -> Dict[str, Any]:
    """One read-only what-if. Never raises; never writes."""
    if not ENABLED:
        return {"ok": False, "error": "simulation disabled (SIMULATE_ENABLED=0)"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty scenario"}
    parsed = _parse(text)
    if not parsed:
        return {"ok": False,
                "error": "couldn't map the scenario onto a registered metric",
                "metrics": {k: v["label"] for k, v in objectives.METRICS.items()}}

    metric = parsed["metric"]
    spec = objectives.METRICS[metric]
    current = objectives.metric_value(metric)
    if current is None:
        return {"ok": False, "error": f"live value for '{metric}' unavailable"}

    scen = (float(parsed["set_to"]) if "set_to" in parsed
            else current * (1.0 + float(parsed["change_pct"]) / 100.0))
    scen = max(scen, 0.0)          # counts, $ and % can't go negative

    impacts: List[Dict[str, Any]] = []
    try:
        active = [o for o in objectives._load_active() if o["metric"] == metric]
    except Exception as exc:
        logger.debug(f"[simulate] objectives unavailable: {exc}")
        active = []
    for o in active:
        before = objectives.evaluate(o["direction"], o["baseline"], o["target"],
                                     current, o["horizon_start"], o["horizon_end"])
        after = objectives.evaluate(o["direction"], o["baseline"], o["target"],
                                    scen, o["horizon_start"], o["horizon_end"])
        gap_before = abs(o["target"] - current)
        gap_after = 0.0 if after["status"] == "achieved" else abs(o["target"] - scen)
        closed = (100.0 * (gap_before - gap_after) / gap_before
                  if gap_before > 0 else 100.0)
        impacts.append({
            "objective": o["name"], "target": o["target"],
            "status_before": before["status"], "status_after": after["status"],
            "behind_by_before": before["behind_by"],
            "behind_by_after": after["behind_by"],
            "gap_closed_pct": round(closed, 1)})

    return {"ok": True, "scenario": text, "metric": metric,
            "label": spec["label"], "unit": spec["unit"],
            "current": current, "scenario_value": round(scen, 2),
            "change": parsed.get("change_pct"),
            "objectives": impacts, "ripple": _ripple(metric, current, scen),
            "read_only": True}


def render_markdown(r: Dict[str, Any]) -> str:
    """Chat rendering — sibling of the orchestrator's _format_plan."""
    if not r.get("ok"):
        lines = ["### 🔮 Scenario — could not simulate", "", f"_{r.get('error')}_"]
        if r.get("metrics"):
            lines += ["", "Registered metrics:"]
            lines += [f"- **{k}** — {v}" for k, v in r["metrics"].items()]
        return "\n".join(lines)
    u = r["unit"]
    lines = [f"### 🔮 Scenario — {r['scenario']}", "",
             f"**{r['label']}**: {_fmt(r['current'], u)} → "
             f"**{_fmt(r['scenario_value'], u)}**"
             + (f" ({r['change']:+.0f}%)" if r.get("change") is not None else "")]
    for o in r["objectives"] or []:
        arrow = ("✅" if o["status_after"] in ("achieved", "on_track")
                 else "⚠️" if o["status_after"] == "at_risk" else "🔴")
        lines += ["", f"{arrow} **{o['objective']}** (target "
                      f"{_fmt(o['target'], u)}): {o['status_before']} → "
                      f"**{o['status_after']}**, gap to target closed "
                      f"{o['gap_closed_pct']:.0f}%"]
    if not r["objectives"]:
        lines += ["", "_No active objective tracks this metric — showing the "
                      "raw projection only._"]
    if r.get("ripple"):
        rp = r["ripple"]
        lines += ["", f"↔️ Ripple — **{rp['label']}**: "
                      f"{_fmt(rp['current'], rp['unit'])} → "
                      f"~{_fmt(rp['estimate'], rp['unit'])} _({rp['note']})_"]
    lines += ["", "_Read-only simulation — nothing was changed, proposed or "
                  "sent._"]
    return "\n".join(lines)


# ============================================================================
# Admin endpoints (mounted with _ADMIN in main.py)
# ============================================================================

router = APIRouter(tags=["simulate"])


@router.get("/simulate")
def simulate_get(q: str):
    return simulate(q)
