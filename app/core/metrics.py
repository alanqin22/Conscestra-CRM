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
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

logger = logging.getLogger("metrics")


# ============================================================================
# Decision-event timestamp. Prefer opportunities.decided_at; fall back to
# close_date on a pre-migration schema. Resolved once per process.
# Alias `o` — the opportunities metrics use base "opportunities o".
#
# The fallback is close_date, NOT updated_at (changed 2026-07-30). updated_at is
# the LAST EDIT time: with it, "win rate last 7 days" silently meant "deals
# EDITED in the last 7 days", so detect_win_rate_drop fired on editing activity
# rather than on outcomes. close_date is at least a decision-shaped date. The
# migration backfills decided_at so the fallback should reach ~0 rows — see
# sql/metric_registry_migration.sql.
# ============================================================================
_DECISION_TS: Optional[str] = None

# The canonical decision timestamp, as SQL. Kept as a module constant so the
# SQL generator (metric_sql.py) emits the SAME expression the Python path uses.
#
# The fallback cast is `::timestamp AT TIME ZONE 'UTC'`, NOT `::timestamptz`.
# A plain date→timestamptz cast reads the session TimeZone, so Postgres marks it
# STABLE and REFUSES to build an index on it ("functions in index expression
# must be marked IMMUTABLE"). Anchoring the zone as a literal makes it immutable,
# which is what lets idx_opportunities_decided_ts exist AND match this predicate.
# Same string in Python, in the generated view, and in the index — a mismatch
# would silently cost the index and re-open the drift this module closes.
DECISION_TS_SQL = "COALESCE(o.decided_at, o.close_date::timestamp AT TIME ZONE 'UTC')"
DECISION_TS_FALLBACK_SQL = "(o.close_date::timestamp AT TIME ZONE 'UTC')"


def decision_ts() -> str:
    """The canonical decision-date SQL expression, probed once.

    CACHING RULE: the POSITIVE result is cached forever (a column does not
    disappear), the NEGATIVE result is NOT. Caching the fallback meant a worker
    that started before sql/metric_registry_migration.sql ran would keep using
    close_date for its whole lifetime — so during a rolling deploy, replicas
    could answer the same question on two different time axes with no restart
    scheduled anywhere. Re-probing on the fallback path costs one cheap
    catalogue query per call until the migration lands, then never again."""
    global _DECISION_TS
    if _DECISION_TS is not None:
        return _DECISION_TS
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_name='opportunities' AND column_name='decided_at'")
                if cur.fetchone():
                    _DECISION_TS = DECISION_TS_SQL      # cache the positive only
                    return _DECISION_TS
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[metrics] decided_at probe failed, using close_date: {exc}")
    return DECISION_TS_FALLBACK_SQL


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
    # What a ratio COUNTS. Default 'count(*)' = one vote per row (deal-weighted).
    # Override with 'sum(o.amount)' for a value-weighted ratio (win_rate_value).
    #
    # MUST be a BARE aggregate call — no COALESCE/ROUND wrapper. compute() emits
    # `COALESCE(<ratio_agg> FILTER (WHERE cond), 0)`, and Postgres only accepts
    # FILTER directly after an aggregate; wrapping it here is a syntax error at
    # query time (caught by tests/test_metric_registry.py).
    ratio_agg: str = "count(*)"
    # sum / avg / count:
    agg_expr: Optional[str] = None    # the summed/averaged expression ('*' implied for count)
    row_cond: Optional[str] = None    # optional row filter (e.g. only closed_won)
    # Which external systems feed this metric (data_sources.source_key). Lets a
    # freshness caveat name only the sources that actually matter: warning about
    # a stale accounting sync on a metric accounting does not feed trains people
    # to ignore the warning, which is worse than not showing one.
    sources: Tuple[str, ...] = ()

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
        # Semantically redundant (the denominator is the same set), but it puts
        # the status predicate in WHERE rather than only inside FILTER — which is
        # what lets the planner use the partial idx_opportunities_decided_ts.
        # Without it this query seq-scanned while the SQL path used the index.
        base_where=WIN_DEN_COND,
        numerator_cond=WIN_NUM_COND, denominator_cond=WIN_DEN_COND,
        definition=("Closed-won ÷ (closed-won + closed-lost) opportunities, counted "
                    "as DEALS. Excludes open deals. Time-scoped by decision date "
                    "(decided_at). `status` is the authoritative outcome column — "
                    "`stage` is pipeline position only, so closed_paid is already "
                    "status='closed_won'."),
    ),
    # The dollar-weighted twin. Registered SEPARATELY and labelled distinctly
    # because it answers a different question: win_rate says "we win 9 of 10
    # deals", win_rate_value says "we win 99% of the dollars we compete for".
    # Both were previously rendered under the single label "Win Rate" (the SP's
    # win_rate_count_pct / win_rate_amount_pct) with no way for /metrics or
    # Explore to reproduce the second one.
    "win_rate_value": Metric(
        name="win_rate_value", label="Revenue Win Rate", entity="opportunities",
        base="opportunities o", unit="percentage", kind="ratio",
        time_field="decided",
        base_where=WIN_DEN_COND,          # index-enabling; see win_rate above
        numerator_cond=WIN_NUM_COND, denominator_cond=WIN_DEN_COND,
        ratio_agg="sum(o.amount)",
        definition=("Closed-won AMOUNT ÷ (closed-won + closed-lost) amount. The "
                    "same win/loss definition as win_rate, weighted by deal value "
                    "instead of deal count. A ratio — never sum it across groups."),
    ),
    "won_revenue": Metric(
        name="won_revenue", label="Won Revenue", entity="opportunities",
        base="opportunities o", unit="currency", kind="sum",
        time_field="decided",
        base_where=WIN_DEN_COND,          # index-enabling; won ⊂ decided, so a no-op
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

# The reporting calendar. date_trunc() on a timestamptz resolves month/quarter/
# year boundaries in the SESSION's TimeZone, so "this month" silently meant
# different things on different connections: a deal decided 2026-08-01 02:00 UTC
# buckets into AUGUST under TimeZone=UTC and into JULY under America/New_York.
# One definition string, two meanings — the property this registry exists to
# guarantee, broken by session state rather than by a second formula.
#
# Anchoring the zone in the expression makes every bucket deterministic
# regardless of who connects. Default matches the scheduler's America/New_York
# so reported months line up with the business day the rest of the platform uses.
METRICS_TZ = os.getenv("METRICS_TZ", "America/New_York")


def _cal(expr: str) -> str:
    """A timestamp expression resolved in the reporting calendar's zone."""
    return f"(({expr}) AT TIME ZONE '{METRICS_TZ}')"


def _windows(t: str) -> Dict[str, Optional[str]]:
    # NOT-YET-HAPPENED GUARD (added 2026-07-30 — found while unifying the SQL
    # surfaces). Every forward-open window used to be `{t} >= now() - N days`
    # with NO upper bound, so any record carrying a FUTURE decision date fell
    # into it. That is not hypothetical: 45 opportunities here are marked decided
    # with dates up to 2.5 months ahead (closed deals whose close_date is a
    # future forecast date, which decided_at falls back to). last_30d counted
    # 352 deals where the bounded SQL path counted 307 — the 45 were the gap.
    #
    # It also made period-over-period comparisons ASYMMETRIC: prev_7d was bounded
    # on both sides while last_7d was not, so detect_win_rate_drop compared a
    # window with a future tail against one without. An outcome you cannot have
    # observed yet must never land in a trailing window.
    #
    # all_time deliberately keeps them: they ARE won/lost deals, just misdated.
    # sql/metric_registry_migration.sql exposes them via
    # v_opportunity_future_decision for repair.
    now = f" AND {t} <= now()"
    return {
        "all_time":     None,
        "last_7d":      f"{t} >= now() - interval '7 days'{now}",
        "prev_7d":      f"{t} >= now() - interval '14 days' AND {t} < now() - interval '7 days'",
        "last_14d":     f"{t} >= now() - interval '14 days'{now}",
        "last_30d":     f"{t} >= now() - interval '30 days'{now}",
        "prev_30d":     f"{t} >= now() - interval '60 days' AND {t} < now() - interval '30 days'",
        "last_90d":     f"{t} >= now() - interval '90 days'{now}",
        "this_month":   f"date_trunc('month', {_cal(t)})   = date_trunc('month', {_cal('now()')}){now}",
        "this_quarter": f"date_trunc('quarter', {_cal(t)}) = date_trunc('quarter', {_cal('now()')}){now}",
        "this_year":    f"date_trunc('year', {_cal(t)})    = date_trunc('year', {_cal('now()')}){now}",
        "qtd":          f"{_cal(t)} >= date_trunc('quarter', {_cal('now()')}){now}",
        "ytd":          f"{_cal(t)} >= date_trunc('year', {_cal('now()')}){now}",
        # ── period-over-period partners (MoM / QoQ / YoY) ────────────────────
        "prev_month":   f"date_trunc('month', {_cal(t)}) = date_trunc('month', {_cal("now() - interval '1 month'")})",
        "prev_quarter": f"date_trunc('quarter', {_cal(t)}) = date_trunc('quarter', {_cal("now() - interval '3 months'")})",
        "prev_year":    f"date_trunc('year', {_cal(t)}) = date_trunc('year', {_cal("now() - interval '1 year'")})",
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

def window_predicate(name: str, window: str) -> str:
    """The registry's OWN time predicate for (metric, window), as SQL.

    For a caller that must hand-write a related query (a COUNT DISTINCT the
    registry does not model, say) and needs the SAME window semantics. Returns
    'TRUE' for all_time so it is always safe to AND into a WHERE clause."""
    return _window_pred(_metric(name), window) or "TRUE"


def compute_sql(name: str, window: str = "all_time",
                extra_where: Optional[str] = None) -> Tuple[str, Metric]:
    """The SQL for (metric, window), plus the Metric, WITHOUT executing it.

    Lets a caller that already holds a cursor run a registered metric on its own
    connection — so, for example, every figure in one CEO briefing comes from a
    single database snapshot instead of one transaction per metric. Pair it with
    `value_from_row` so the arithmetic is shared too; a caller that re-derives
    the value itself has re-introduced exactly the drift this module exists to
    prevent."""
    m = _metric(name)
    guards = _where(m.base_where, _window_pred(m, window), extra_where)

    if m.kind == "ratio":
        return ((f"SELECT COALESCE({m.ratio_agg} FILTER (WHERE {m.numerator_cond}), 0)   AS numerator, "
                 f"COALESCE({m.ratio_agg} FILTER (WHERE {m.denominator_cond}), 0) AS denominator "
                 f"FROM {m.base} {guards}"), m)

    agg = {"sum": "sum", "avg": "avg", "count": "count"}[m.kind]
    inner = "*" if m.kind == "count" else m.agg_expr
    expr = f"{agg}({inner})"
    if m.row_cond:
        expr += f" FILTER (WHERE {m.row_cond})"
    if m.kind in ("sum", "count"):
        expr = f"COALESCE({expr}, 0)"
    return (f"SELECT {expr} AS value FROM {m.base} {guards}", m)


def value_from_row(m: Metric, row: Any) -> Optional[float]:
    """Interpret a compute_sql row the way compute() does. `row` may be a dict
    (RealDictCursor) or a plain tuple, so any cursor style works."""
    def _col(key: str, idx: int):
        if isinstance(row, dict):
            return row.get(key)
        return row[idx] if row and len(row) > idx else None

    if m.kind == "ratio":
        return _ratio_value(_col("numerator", 0), _col("denominator", 1), m.unit)
    v = _col("value", 0)
    return round(float(v), 2) if v is not None else (0.0 if m.additive else None)


def compute(name: str, window: str = "all_time",
            extra_where: Optional[str] = None) -> Dict[str, Any]:
    """Compute `name` over `window`. Returns a self-describing result carrying
    the value AND its canonical definition (Trusted Context). `extra_where` is a
    trusted predicate a governed caller may supply (step 3 row-scoping); it is
    never user/model text."""
    m = _metric(name)
    sql, _ = compute_sql(name, window, extra_where)

    if m.kind == "ratio":
        r = _run(sql)
        num, den = r.get("numerator"), r.get("denominator")
        value = value_from_row(m, r)
        # A value-weighted ratio's num/den are money, not row counts — don't
        # truncate them to int (that silently dropped cents on win_rate_value).
        cast = int if m.ratio_agg == "count(*)" else (lambda v: round(float(v), 2))
        extra = {"numerator": cast(num or 0), "denominator": cast(den or 0)}
    else:  # sum | count | avg — sql already built by compute_sql above
        value = value_from_row(m, _run(sql))
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
            f"COALESCE({m.ratio_agg} FILTER (WHERE {m.numerator_cond}   AND {pa}), 0) AS num_a, "
            f"COALESCE({m.ratio_agg} FILTER (WHERE {m.denominator_cond} AND {pa}), 0) AS den_a, "
            f"COALESCE({m.ratio_agg} FILTER (WHERE {m.numerator_cond}   AND {pb}), 0) AS num_b, "
            f"COALESCE({m.ratio_agg} FILTER (WHERE {m.denominator_cond} AND {pb}), 0) AS den_b "
            f"FROM {m.base} {scope}")
        r = _run(sql)
        cast = int if m.ratio_agg == "count(*)" else (lambda v: round(float(v), 2))
        a = {"value": _ratio_value(r.get("num_a"), r.get("den_a"), m.unit),
             "numerator": cast(r.get("num_a") or 0), "denominator": cast(r.get("den_a") or 0)}
        b = {"value": _ratio_value(r.get("num_b"), r.get("den_b"), m.unit),
             "numerator": cast(r.get("num_b") or 0), "denominator": cast(r.get("den_b") or 0)}
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


def compute_many(names: List[str], window: str = "all_time",
                 extra_where: Optional[str] = None) -> Dict[str, Any]:
    """Several metrics from ONE database snapshot.

    Reading metrics one at a time gives one snapshot each — under READ COMMITTED
    every statement sees a different state — so a dashboard showing win rate,
    revenue and conversion side by side could show three moments while claiming
    a single `as_of`. That is the same internal inconsistency the CEO briefing
    had, one layer up, and it cannot be fixed client-side: six HTTP calls are
    six snapshots no matter how they are batched in the UI.

    `as_of` here is therefore a real claim rather than a decoration."""
    from app.core.semantic_query import snapshot

    out: Dict[str, Any] = {}
    with snapshot():
        for n in names:
            try:
                out[n] = compute(n, window, extra_where)
            except MetricError as exc:
                out[n] = {"metric": n, "error": str(exc)}
    return {
        "window": window,
        "as_of": _dt.datetime.utcnow().isoformat() + "Z",
        "snapshot": "repeatable_read",
        "metrics": out,
    }


@router.get("/metrics")
def metrics_batch(names: str = "", window: str = "all_time"):
    """Several metrics over one window, from ONE snapshot.

    `names` is a comma-separated list; empty means the whole registry. Prefer
    this over N calls to /metrics/{name} whenever the numbers will be shown
    together — it is the only way their `as_of` is truthful."""
    wanted = [n.strip() for n in (names or "").split(",") if n.strip()] or list(REGISTRY)
    unknown = [n for n in wanted if n not in REGISTRY]
    if unknown:
        return {"error": f"unknown metric(s): {', '.join(unknown)}. "
                         f"Valid: {', '.join(REGISTRY)}"}
    try:
        return compute_many(wanted, window)
    except Exception as exc:
        logger.warning(f"[metrics] batch failed: {exc}")
        return {"error": f"metrics batch failed: {str(exc)[:160]}"}


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
