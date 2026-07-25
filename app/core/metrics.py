"""Metric Registry — the ONE canonical definition per business metric.

P0 "Trusted Semantic Core", step 2. Before this, `win_rate` was computed in at
least four places with different formulas and time semantics (semantic_model
explore, analytics_signals WoW detector, the opportunity SP's mode='win_rate',
the executive pack) — so "what's our win rate?" could answer three different
numbers depending on which surface you asked. That is the opposite of Trusted
Context.

This module is the single source of truth. A metric is defined ONCE here — as
hand-written, trusted SQL CONDITIONS (never full aggregates) plus a `kind` that
classifies how it may be aggregated — and every Python surface consumes it:

    Metric Registry  ──▶  semantic_model explore measure (win_rate)
                     ──▶  analytics_signals WoW detectors (compare two windows)
                     ──▶  /metrics endpoint (self-describing "Trusted Context")

The crucial principle (from the design review): **a metric has ONE definition
but MANY valid time windows.** A detector asks for win_rate over last_7d vs
prev_7d; the exec briefing asks for this_quarter; Explore asks all_time. None of
them redefines win_rate — they parameterize the window.

SAFETY: every SQL fragment here (base, conditions, agg exprs, window predicates)
is OURS — hand-written and read-only by construction. `window` names are looked
up in a fixed table (never interpolated raw). `extra_where`, when supplied by a
future governed caller (step 3 row-scoping), is a trusted predicate the caller
builds — never model/user text. Execution runs through semantic_query.run_readonly
(a read-only txn + statement_timeout + LIMIT), so the DB refuses any write.

AGGREGATION SANITY: `kind` marks each metric additive or not. A `ratio` or `avg`
is NON-additive — callers must never SUM it across groups. `additive` on the
returned dict makes that explicit so downstream (and step 3's relationship-graph
planner) can refuse an illegal roll-up.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

logger = logging.getLogger("metrics")


# ============================================================================
# Decision-event timestamp (shared with step 1). Prefer opportunities.decided_at;
# fall back to updated_at on a pre-migration schema. Resolved once per process.
# Alias `o` — the opportunities metrics use base "opportunities o".
# ============================================================================
_DECISION_TS: Optional[str] = None


def decision_ts() -> str:
    global _DECISION_TS
    if _DECISION_TS is not None:
        return _DECISION_TS
    expr = "o.updated_at"
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='opportunities' AND column_name='decided_at'")
                if cur.fetchone():
                    expr = "COALESCE(o.decided_at, o.updated_at)"
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[metrics] decided_at probe failed, using updated_at: {exc}")
    _DECISION_TS = expr
    return expr


# ============================================================================
# The metric definition
# ============================================================================

@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    entity: str
    base: str                       # FROM clause, e.g. "opportunities o"
    unit: str                       # 'percentage' | 'currency' | 'number'
    kind: str                       # 'ratio' | 'sum' | 'avg' | 'count'
    definition: str                 # plain-language grain (for Trusted-Context display)
    time_field: Optional[str] = None  # 'decided' sentinel → decision_ts(); else a literal
                                      # column expr (e.g. "l.created_at"); None = not time-scopable
    base_where: Optional[str] = None  # mandatory guard (e.g. soft-delete), always applied
    # ratio:
    numerator_cond: Optional[str] = None
    denominator_cond: Optional[str] = None
    # sum / avg / count:
    agg_expr: Optional[str] = None    # the summed/averaged expression ('*' implied for count)
    row_cond: Optional[str] = None    # optional row filter (e.g. only closed_won)

    @property
    def additive(self) -> bool:
        """True only when the metric may be summed across groups. Ratios and
        averages are NON-additive — summing them is a correctness bug."""
        return self.kind in ("sum", "count")

    def resolved_time(self) -> Optional[str]:
        if self.time_field is None:
            return None
        return decision_ts() if self.time_field == "decided" else self.time_field


# ── Canonical shared fragments (imported by semantic_model so the explore's
#    win_rate is THE SAME definition as the registry's — unified by construction) ──
WIN_NUM_COND = "o.status = 'closed_won'"
WIN_DEN_COND = "o.status IN ('closed_won','closed_lost')"
WIN_RATE_PCT_SQL = (
    f"ROUND(100.0 * count(*) FILTER (WHERE {WIN_NUM_COND}) "
    f"/ NULLIF(count(*) FILTER (WHERE {WIN_DEN_COND}), 0), 1)"
)


# ============================================================================
# THE REGISTRY
# ============================================================================
REGISTRY: Dict[str, Metric] = {
    "win_rate": Metric(
        name="win_rate", label="Win Rate", entity="opportunities",
        base="opportunities o", unit="percentage", kind="ratio",
        time_field="decided",
        numerator_cond=WIN_NUM_COND, denominator_cond=WIN_DEN_COND,
        definition=("Closed-won ÷ (closed-won + closed-lost) opportunities. "
                    "Excludes open deals. Time-scoped by decision date (decided_at)."),
    ),
    "won_revenue": Metric(
        name="won_revenue", label="Won Revenue", entity="opportunities",
        base="opportunities o", unit="currency", kind="sum",
        time_field="decided",
        agg_expr="o.amount", row_cond=WIN_NUM_COND,
        definition=("Sum of amount on closed-won opportunities, by decision date "
                    "(decided_at). Additive — safe to total across groups."),
    ),
    "lead_conversion_rate": Metric(
        name="lead_conversion_rate", label="Lead Conversion Rate", entity="leads",
        base="leads l", unit="percentage", kind="ratio",
        time_field="l.created_at", base_where="COALESCE(l.is_deleted,false)=false",
        numerator_cond="l.converted", denominator_cond="TRUE",
        definition=("Converted ÷ all leads (excludes deleted), by created date. "
                    "A ratio — never sum it across groups."),
    ),
}


# ============================================================================
# Time windows — one definition, many windows. Given the metric's resolved time
# expression T, each name maps to a SQL predicate (or None for all_time).
# ============================================================================

def _windows(t: str) -> Dict[str, Optional[str]]:
    return {
        "all_time":     None,
        "last_7d":      f"{t} >= now() - interval '7 days'",
        "prev_7d":      f"{t} >= now() - interval '14 days' AND {t} < now() - interval '7 days'",
        "last_14d":     f"{t} >= now() - interval '14 days'",
        "last_30d":     f"{t} >= now() - interval '30 days'",
        "prev_30d":     f"{t} >= now() - interval '60 days' AND {t} < now() - interval '30 days'",
        "last_90d":     f"{t} >= now() - interval '90 days'",
        "this_month":   f"date_trunc('month', {t})   = date_trunc('month', now())",
        "this_quarter": f"date_trunc('quarter', {t}) = date_trunc('quarter', now())",
        "this_year":    f"date_trunc('year', {t})    = date_trunc('year', now())",
        "qtd":          f"{t} >= date_trunc('quarter', now())",
        "ytd":          f"{t} >= date_trunc('year', now())",
        # ── period-over-period partners (MoM / QoQ / YoY) ────────────────────
        "prev_month":   f"date_trunc('month', {t}) = date_trunc('month', now() - interval '1 month')",
        "prev_quarter": f"date_trunc('quarter', {t}) = date_trunc('quarter', now() - interval '3 months')",
        "prev_year":    f"date_trunc('year', {t}) = date_trunc('year', now() - interval '1 year')",
        # same slice one year back — the correct YoY partner for a seasonal metric
        "same_month_last_year":   f"date_trunc('month', {t}) = "
                                  f"date_trunc('month', now() - interval '1 year')",
        "same_quarter_last_year": f"date_trunc('quarter', {t}) = "
                                  f"date_trunc('quarter', now() - interval '1 year')",
        "ytd_last_year": f"{t} >= date_trunc('year', now() - interval '1 year') "
                         f"AND {t} < date_trunc('year', now() - interval '1 year') "
                         f"+ (now() - date_trunc('year', now()))",
    }


# Named period-over-period comparisons — so no caller hand-rolls "this vs last".
COMPARISONS: Dict[str, Tuple[str, str]] = {
    "wow": ("last_7d", "prev_7d"),
    "mom": ("this_month", "prev_month"),
    "qoq": ("this_quarter", "prev_quarter"),
    "yoy": ("this_year", "prev_year"),
    "yoy_month": ("this_month", "same_month_last_year"),
    "yoy_quarter": ("this_quarter", "same_quarter_last_year"),
    "yoy_ytd": ("ytd", "ytd_last_year"),
}

# Time buckets a rolling series may use.
BUCKETS = {"day": "day", "week": "week", "month": "month", "quarter": "quarter"}


WINDOW_NAMES = list(_windows("x").keys())


class MetricError(ValueError):
    """Unknown metric, unknown/invalid window, or an un-scopable request."""


def _metric(name: str) -> Metric:
    m = REGISTRY.get(name)
    if not m:
        raise MetricError(f"unknown metric '{name}'. Valid: {', '.join(REGISTRY)}")
    return m


def _window_pred(m: Metric, window: str) -> Optional[str]:
    if window == "all_time":
        return None
    t = m.resolved_time()
    if t is None:
        raise MetricError(f"metric '{m.name}' is not time-scopable (window must be all_time)")
    table = _windows(t)
    if window not in table:
        raise MetricError(f"unknown window '{window}'. Valid: {', '.join(WINDOW_NAMES)}")
    return table[window]


def _where(*parts: Optional[str]) -> str:
    live = [p for p in parts if p]
    return ("WHERE " + " AND ".join(f"({p})" for p in live)) if live else ""


def _ratio_value(num: Any, den: Any, unit: str) -> Optional[float]:
    n, d = float(num or 0), float(den or 0)
    if d == 0:
        return None
    raw = n / d
    return round(100.0 * raw, 1) if unit == "percentage" else round(raw, 4)


def _run(sql: str) -> Dict[str, Any]:
    """One-row read-only execute, reusing semantic_query's guarded executor
    (read-only txn + statement_timeout). Lazy import avoids an import cycle
    (semantic_query → semantic_model → metrics)."""
    from app.core.semantic_query import run_readonly
    rows = run_readonly(sql, [])
    return rows[0] if rows else {}


# ============================================================================
# COMPUTE — one metric, one window
# ============================================================================

def compute(name: str, window: str = "all_time",
            extra_where: Optional[str] = None) -> Dict[str, Any]:
    """Compute `name` over `window`. Returns a self-describing result carrying
    the value AND its canonical definition (Trusted Context). `extra_where` is a
    trusted predicate a governed caller may supply (step 3 row-scoping); it is
    never user/model text."""
    m = _metric(name)
    pred = _window_pred(m, window)
    guards = _where(m.base_where, pred, extra_where)

    if m.kind == "ratio":
        sql = (f"SELECT count(*) FILTER (WHERE {m.numerator_cond})   AS numerator, "
               f"count(*) FILTER (WHERE {m.denominator_cond}) AS denominator "
               f"FROM {m.base} {guards}")
        r = _run(sql)
        num, den = r.get("numerator"), r.get("denominator")
        value = _ratio_value(num, den, m.unit)
        extra = {"numerator": int(num or 0), "denominator": int(den or 0)}
    else:  # sum | count | avg
        agg = {"sum": "sum", "avg": "avg", "count": "count"}[m.kind]
        inner = "*" if m.kind == "count" else m.agg_expr
        expr = f"{agg}({inner})"
        if m.row_cond:
            expr += f" FILTER (WHERE {m.row_cond})"
        if m.kind in ("sum", "count"):
            expr = f"COALESCE({expr}, 0)"
        sql = f"SELECT {expr} AS value FROM {m.base} {guards}"
        r = _run(sql)
        v = r.get("value")
        value = round(float(v), 2) if v is not None else (0.0 if m.additive else None)
        extra = {}

    return {
        "metric": m.name, "label": m.label, "unit": m.unit, "kind": m.kind,
        "additive": m.additive, "window": window, "value": value,
        "definition": m.definition, "as_of": _dt.datetime.utcnow().isoformat() + "Z",
        **extra,
    }


# ============================================================================
# COMPARE — one metric, two windows, in a SINGLE query (period-over-period)
# ============================================================================

def compare(name: str, window_a: str, window_b: str,
            extra_where: Optional[str] = None) -> Dict[str, Any]:
    """Compare `name` across two windows in one query. Powers the WoW anomaly
    detectors WITHOUT them redefining the metric. Returns {a, b, delta,
    pct_change} where a/b carry value (+ numerator/denominator for ratios)."""
    m = _metric(name)
    pa, pb = _window_pred(m, window_a), _window_pred(m, window_b)
    if pa is None or pb is None:
        raise MetricError("compare needs two bounded windows (not all_time)")
    scope = _where(m.base_where, f"({pa}) OR ({pb})", extra_where)

    if m.kind == "ratio":
        sql = (
            f"SELECT "
            f"count(*) FILTER (WHERE {m.numerator_cond}   AND {pa}) AS num_a, "
            f"count(*) FILTER (WHERE {m.denominator_cond} AND {pa}) AS den_a, "
            f"count(*) FILTER (WHERE {m.numerator_cond}   AND {pb}) AS num_b, "
            f"count(*) FILTER (WHERE {m.denominator_cond} AND {pb}) AS den_b "
            f"FROM {m.base} {scope}")
        r = _run(sql)
        a = {"value": _ratio_value(r.get("num_a"), r.get("den_a"), m.unit),
             "numerator": int(r.get("num_a") or 0), "denominator": int(r.get("den_a") or 0)}
        b = {"value": _ratio_value(r.get("num_b"), r.get("den_b"), m.unit),
             "numerator": int(r.get("num_b") or 0), "denominator": int(r.get("den_b") or 0)}
    else:
        agg = {"sum": "sum", "avg": "avg", "count": "count"}[m.kind]
        inner = "*" if m.kind == "count" else m.agg_expr
        rc = f"{m.row_cond} AND " if m.row_cond else ""
        sql = (
            f"SELECT "
            f"COALESCE({agg}({inner}) FILTER (WHERE {rc}{pa}), 0) AS val_a, "
            f"COALESCE({agg}({inner}) FILTER (WHERE {rc}{pb}), 0) AS val_b "
            f"FROM {m.base} {scope}")
        r = _run(sql)
        a = {"value": round(float(r.get("val_a") or 0), 2)}
        b = {"value": round(float(r.get("val_b") or 0), 2)}

    va, vb = a["value"], b["value"]
    delta = (va - vb) if (va is not None and vb is not None) else None
    pct = (100.0 * (va - vb) / vb) if (va is not None and vb not in (None, 0)) else None
    return {
        "metric": m.name, "label": m.label, "unit": m.unit, "kind": m.kind,
        "additive": m.additive, "definition": m.definition,
        "window_a": window_a, "window_b": window_b,
        "a": a, "b": b,
        "delta": round(delta, 2) if delta is not None else None,
        "pct_change": round(pct, 1) if pct is not None else None,
    }


def compare_named(name: str, comparison: str = "wow",
                  extra_where: Optional[str] = None) -> Dict[str, Any]:
    """Named period-over-period comparison ('wow'|'mom'|'qoq'|'yoy'|'yoy_month'|
    'yoy_quarter'|'yoy_ytd') so no caller re-derives "this vs last period"."""
    pair = COMPARISONS.get(comparison)
    if not pair:
        raise MetricError(f"unknown comparison '{comparison}'. "
                          f"Valid: {', '.join(COMPARISONS)}")
    out = compare(name, pair[0], pair[1], extra_where)
    out["comparison"] = comparison
    return out


# ============================================================================
# SERIES / ROLLING AVERAGE — a metric over consecutive buckets
# ============================================================================

def series(name: str, bucket: str = "month", periods: int = 6,
           rolling: int = 3, extra_where: Optional[str] = None) -> Dict[str, Any]:
    """The metric bucketed over the last `periods` buckets, plus a trailing
    `rolling`-bucket average — ONE definition, many periods (a rolling average is
    a time operator on the metric, not a new metric).

    The rolling mean is computed over the bucket VALUES; for a ratio metric that
    is a mean-of-ratios, so numerator/denominator are returned per bucket too and
    the ratio is additionally recomputed from summed parts (`rolling_pooled`),
    which is the statistically correct pooled figure."""
    m = _metric(name)
    if bucket not in BUCKETS:
        raise MetricError(f"unknown bucket '{bucket}'. Valid: {', '.join(BUCKETS)}")
    t = m.resolved_time()
    if t is None:
        raise MetricError(f"metric '{m.name}' is not time-scopable")
    periods = max(2, min(int(periods), 36))
    rolling = max(2, min(int(rolling), periods))

    label_expr = (f"to_char(date_trunc('{bucket}', {t}), 'YYYY-MM-DD')" if bucket == "day"
                  else f"to_char(date_trunc('{bucket}', {t}), 'YYYY-MM')" if bucket == "month"
                  else f"to_char(date_trunc('{bucket}', {t}), 'YYYY-MM-DD')")
    horizon = f"{t} >= date_trunc('{bucket}', now()) - interval '{periods - 1} {bucket}'"
    guards = _where(m.base_where, horizon, extra_where)

    if m.kind == "ratio":
        sql = (f"SELECT {label_expr} AS bucket, "
               f"count(*) FILTER (WHERE {m.numerator_cond})   AS numerator, "
               f"count(*) FILTER (WHERE {m.denominator_cond}) AS denominator "
               f"FROM {m.base} {guards} GROUP BY 1 ORDER BY 1")
    else:
        agg = {"sum": "sum", "avg": "avg", "count": "count"}[m.kind]
        inner = "*" if m.kind == "count" else m.agg_expr
        expr = f"{agg}({inner})"
        if m.row_cond:
            expr += f" FILTER (WHERE {m.row_cond})"
        sql = (f"SELECT {label_expr} AS bucket, COALESCE({expr}, 0) AS value "
               f"FROM {m.base} {guards} GROUP BY 1 ORDER BY 1")

    from app.core.semantic_query import run_readonly
    rows = run_readonly(sql, [])

    points: List[Dict[str, Any]] = []
    for r in rows:
        if m.kind == "ratio":
            num, den = int(r.get("numerator") or 0), int(r.get("denominator") or 0)
            points.append({"bucket": r.get("bucket"),
                           "value": _ratio_value(num, den, m.unit),
                           "numerator": num, "denominator": den})
        else:
            v = r.get("value")
            points.append({"bucket": r.get("bucket"),
                           "value": round(float(v), 2) if v is not None else 0.0})

    # trailing rolling mean (and, for ratios, the pooled ratio over the window)
    for i, p in enumerate(points):
        window = points[max(0, i - rolling + 1): i + 1]
        vals = [w["value"] for w in window if w["value"] is not None]
        p["rolling_avg"] = round(sum(vals) / len(vals), 2) if vals else None
        if m.kind == "ratio":
            num = sum(w.get("numerator") or 0 for w in window)
            den = sum(w.get("denominator") or 0 for w in window)
            p["rolling_pooled"] = _ratio_value(num, den, m.unit)

    return {"metric": m.name, "label": m.label, "unit": m.unit, "kind": m.kind,
            "definition": m.definition, "bucket": bucket, "periods": periods,
            "rolling": rolling, "points": points,
            "note": (f"{m.label} per {bucket} with a trailing {rolling}-{bucket} "
                     f"average." + (" `rolling_pooled` is the pooled ratio (correct "
                                    "for a rate); `rolling_avg` is the mean of the "
                                    "per-bucket rates." if m.kind == "ratio" else ""))}


# ============================================================================
# CATALOG / DESCRIBE — Trusted Context surfaces
# ============================================================================

def catalog() -> Dict[str, Any]:
    return {
        "windows": WINDOW_NAMES,
        "comparisons": {k: {"current": v[0], "prior": v[1]} for k, v in COMPARISONS.items()},
        "buckets": sorted(BUCKETS),
        "metrics": [
            {"name": m.name, "label": m.label, "entity": m.entity, "unit": m.unit,
             "kind": m.kind, "additive": m.additive,
             "time_scopable": m.time_field is not None, "definition": m.definition}
            for m in REGISTRY.values()
        ],
    }


# ============================================================================
# Router
# ============================================================================
router = APIRouter(tags=["metrics"])


@router.get("/metrics/catalog")
def metrics_catalog():
    """The canonical metric registry + valid time windows."""
    return catalog()


@router.get("/metrics/{name}")
def metrics_value(name: str, window: str = "all_time"):
    """One metric over one window — value WITH its definition (Trusted Context)."""
    try:
        return compute(name, window)
    except MetricError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.warning(f"[metrics] compute failed: {exc}")
        return {"error": f"metric failed: {str(exc)[:160]}"}


@router.get("/metrics/{name}/compare")
def metrics_compare(name: str, a: str = "last_7d", b: str = "prev_7d",
                    comparison: Optional[str] = None):
    """Period-over-period comparison. Either explicit windows (a, b) or a named
    `comparison` (wow / mom / qoq / yoy / yoy_month / yoy_quarter / yoy_ytd)."""
    try:
        return compare_named(name, comparison) if comparison else compare(name, a, b)
    except MetricError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.warning(f"[metrics] compare failed: {exc}")
        return {"error": f"metric compare failed: {str(exc)[:160]}"}


@router.get("/metrics/{name}/series")
def metrics_series(name: str, bucket: str = "month", periods: int = 6,
                   rolling: int = 3):
    """The metric per bucket with a trailing rolling average."""
    try:
        return series(name, bucket, periods, rolling)
    except MetricError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.warning(f"[metrics] series failed: {exc}")
        return {"error": f"metric series failed: {str(exc)[:160]}"}
