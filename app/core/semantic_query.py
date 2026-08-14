"""Ad-hoc analytics query compiler + safe executor (blindspot A2).

The "Ask Data" brain — genuine self-service exploration over the curated
[semantic_model], done safely:

  NL  ──plan_explore (LLM)──▶  JSON spec  ──compile──▶  (parameterized SQL, params)
                                                          │
                                              run_readonly (DB read-only txn)
                                                          ▼
                                                    rows + columns

SAFETY (defense in depth — see module tests):
  1. The LLM emits field KEYS from the model catalog, never SQL.
  2. compile() validates every explore/dimension/measure/filter/op against the
     model allow-list; anything unknown is REJECTED (no passthrough).
  3. Filter VALUES are psycopg2 bind params — never string-interpolated.
  4. run_readonly() executes in a PostgreSQL read-only transaction
     (set_session(readonly=True)) + a statement_timeout + a hard LIMIT, so a
     write/DDL is refused by the database itself regardless of the compiler.
  5. Single statement by construction (compiler emits no ';').
  6. Router is gated (authenticated analytics users); ANALYTICS_EXPLORE_ENABLED
     kill switch.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from psycopg2.extras import RealDictCursor

from app.core.database import get_connection
from app.core import semantic_model as M

logger = logging.getLogger("semantic_query")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("ANALYTICS_EXPLORE_ENABLED", "1")

MAX_LIMIT = 1000
DEFAULT_LIMIT = 200
MAX_DIMENSIONS = 4
MAX_MEASURES = 8
STMT_TIMEOUT_MS = int(os.getenv("ANALYTICS_EXPLORE_TIMEOUT_MS", "5000"))

# Scalar comparison operators → SQL. 'in' / 'contains' / 'between' are special.
_OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ExploreError(ValueError):
    """Raised on any spec that fails validation. The message lists valid options
    so the planner (or a caller) can repair it."""


# ============================================================================
# COMPILE  (spec → parameterized SQL)
# ============================================================================

_DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_date_only(value: Any) -> bool:
    """True when a filter bound names a DAY rather than an instant.

    The distinction decides whether a `between` upper bound means "midnight
    that morning" or "the end of that day". datetime is checked before date
    because datetime IS a date in Python — the subclass would otherwise match
    first and every timestamp would be treated as a bare day.
    """
    import datetime as _dt
    if isinstance(value, _dt.datetime):
        return False
    if isinstance(value, _dt.date):
        return True
    return bool(_DATE_ONLY_RE.fullmatch(str(value).strip()))


def _window_predicate(ename: str, e: Dict[str, Any], window: str) -> str:
    """Resolve a named time window against this explore's `time_field`, reusing
    the Metric Registry's window vocabulary so 'last_30d' means the same thing in
    an explore, a metric and a detector."""
    t = e.get("time_field")
    if not t:
        raise ExploreError(f"explore '{ename}' has no time field — 'window' is not supported")
    try:
        from app.core import metrics
        table = metrics._windows(t)
    except Exception as exc:                       # pragma: no cover - defensive
        raise ExploreError(f"time windows unavailable: {str(exc)[:80]}")
    if window not in table:
        raise ExploreError(f"unknown window '{window}'. Valid: {', '.join(table)}")
    pred = table[window]
    if pred is None:
        raise ExploreError("window 'all_time' needs no predicate")
    return pred


def compile(spec: Dict[str, Any]) -> Tuple[str, List[Any]]:
    if not isinstance(spec, dict):
        raise ExploreError("spec must be an object")

    ename = str(spec.get("explore") or "").strip()
    if ename not in M.EXPLORES:
        raise ExploreError(f"unknown explore '{ename}'. Valid: {', '.join(M.EXPLORES)}")
    e = M.resolved_explore(ename)   # static model + admin custom fields (P3)

    dims: List[str] = list(spec.get("dimensions") or [])
    meas: List[str] = list(spec.get("measures") or [])
    if not meas:
        meas = ["count"]
    if len(dims) > MAX_DIMENSIONS:
        raise ExploreError(f"too many dimensions (max {MAX_DIMENSIONS})")
    if len(meas) > MAX_MEASURES:
        raise ExploreError(f"too many measures (max {MAX_MEASURES})")

    for d in dims:
        if d not in e["dimensions"]:
            raise ExploreError(f"unknown dimension '{d}' for {ename}. "
                               f"Valid: {', '.join(e['dimensions'])}")
    for m in meas:
        if m not in e["measures"]:
            raise ExploreError(f"unknown measure '{m}' for {ename}. "
                               f"Valid: {', '.join(e['measures'])}")

    # Aliases are our own model keys — but assert the invariant anyway before
    # we ever place one in SQL text.
    for k in dims + meas:
        if not _KEY_RE.match(k):
            raise ExploreError(f"invalid field key '{k}'")

    select_parts: List[str] = []
    for d in dims:
        select_parts.append(f"{e['dimensions'][d]['sql']} AS {d}")
    for m in meas:
        select_parts.append(f"{e['measures'][m]['sql']} AS {m}")

    # ── JOINS (#5): add only the joins the selected fields actually need ─────
    needed: List[str] = []
    for d in dims:
        j = e["dimensions"][d].get("join")
        if j and j not in needed:
            needed.append(j)
    for m in meas:
        j = e["measures"][m].get("join")
        if j and j not in needed:
            needed.append(j)
    for f in (spec.get("filters") or []):
        if isinstance(f, dict):
            fd = e.get("filters", {}).get(str(f.get("field") or ""))
            if isinstance(fd, dict) and fd.get("join") and fd["join"] not in needed:
                needed.append(fd["join"])

    available = e.get("joins") or {}
    join_sql: List[str] = []
    fanout: List[str] = []
    for name in needed:
        j = available.get(name)
        if not j:
            raise ExploreError(f"internal: explore '{ename}' has no join '{name}'")
        join_sql.append(j["sql"])
        if j.get("cardinality") == "many":
            fanout.append(j.get("label") or name)

    # FAN-OUT GUARD: a one-to-many join multiplies the base rows, so ANY ordinary
    # aggregate over it silently double-counts (the classic way self-service BI
    # produces confidently wrong numbers). Only measures explicitly marked
    # fanout_safe (e.g. count(DISTINCT ...)) may cross such a join.
    if fanout:
        unsafe = [m for m in meas if not e["measures"][m].get("fanout_safe")]
        if unsafe:
            safe = [k for k, v in e["measures"].items() if v.get("fanout_safe")]
            raise ExploreError(
                f"measure(s) {', '.join(unsafe)} cannot be combined with the "
                f"one-to-many join(s) {', '.join(fanout)} — each base row is "
                f"multiplied, so the total would double-count. "
                + (f"Use {', '.join(safe)} instead, or drop the "
                   f"{', '.join(fanout)} field." if safe
                   else f"Drop the {', '.join(fanout)} field."))

    # ── WHERE: mandatory guards + validated, PARAMETERIZED filters ──────────
    where: List[str] = list(e.get("mandatory_where") or [])
    params: List[Any] = []

    # ── TIME WINDOW: the registry's window vocabulary, applied to the explore's
    # declared time_field — so period selection is one shared semantic operation
    # instead of every caller hand-writing a date filter.
    win = spec.get("window")
    if win and win != "all_time":
        where.append(_window_predicate(ename, e, str(win)))
    for f in (spec.get("filters") or []):
        if not isinstance(f, dict):
            raise ExploreError("each filter must be an object {field, op, value}")
        field = str(f.get("field") or "")
        op = str(f.get("op") or "eq").lower()
        val = f.get("value")
        if field not in e["filters"]:
            raise ExploreError(f"unknown filter field '{field}' for {ename}. "
                               f"Valid: {', '.join(e['filters'])}")
        fdef = e["filters"][field]         # trusted model expression (str or {sql, join})
        col = fdef["sql"] if isinstance(fdef, dict) else fdef
        if op in _OPS:
            where.append(f"{col} {_OPS[op]} %s")
            params.append(val)
        elif op == "in":
            if not isinstance(val, (list, tuple)) or not val:
                raise ExploreError(f"filter '{field}' op 'in' needs a non-empty list value")
            where.append(f"{col} = ANY(%s)")
            params.append(list(val))
        elif op == "contains":
            where.append(f"{col} ILIKE %s")
            params.append(f"%{val}%")
        elif op == "between":
            if not isinstance(val, (list, tuple)) or len(val) != 2:
                raise ExploreError(f"filter '{field}' op 'between' needs a [low, high] value")
            if _is_date_only(val[1]):
                # A CALENDAR-DAY range must include the whole of its last day.
                #
                # `BETWEEN lo AND hi` on a TIMESTAMP column resolves `hi` to
                # midnight, so "between 2026-05-16 and today" silently discarded
                # everything that happened after 00:00 today — the user asked
                # for a day and got an instant. Measured against /metrics, which
                # bounds the same window as `< hi + 1`: two opportunities whose
                # decision timestamp fell at 20:00 were counted by one surface
                # and not the other, so the two disagreed on the size of the
                # same question (873 vs 874 deals over 90 days).
                #
                # Half-open [lo, hi+1) is the fix, and it is correct for DATE
                # columns too: `d < hi + 1` is equivalent to `d <= hi` there.
                # A bound carrying an explicit TIME still means that instant, so
                # it keeps plain BETWEEN.
                where.append(f"{col} >= %s AND {col} < (%s::date + 1)")
            else:
                where.append(f"{col} BETWEEN %s AND %s")
            params.extend([val[0], val[1]])
        else:
            raise ExploreError(f"unknown operator '{op}'. "
                               f"Valid: {', '.join(list(_OPS) + ['in', 'contains', 'between'])}")

    # ── ORDER BY: default to first measure desc (or first dim asc) ──────────
    selectable = set(dims) | set(meas)
    ob = spec.get("order_by")
    order_field, order_dir = None, "desc"
    if isinstance(ob, dict) and ob.get("field"):
        order_field = str(ob["field"])
        order_dir = "asc" if str(ob.get("dir", "desc")).lower() == "asc" else "desc"
    elif isinstance(ob, str) and ob:
        order_field = ob
    if order_field:
        if order_field not in selectable:
            raise ExploreError(f"order_by '{order_field}' must be a selected dimension or measure")
    else:
        order_field = meas[0] if meas else (dims[0] if dims else None)
        order_dir = "desc" if meas else "asc"

    # ── LIMIT (clamped int we control — safe to inline) ─────────────────────
    try:
        limit = int(spec.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    sql = f"SELECT {', '.join(select_parts)}\nFROM {e['base']}"
    for js in join_sql:
        sql += f"\n{js}"
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if dims:
        sql += "\nGROUP BY " + ", ".join(str(i + 1) for i in range(len(dims)))
    if order_field:
        sql += f"\nORDER BY {order_field} {order_dir.upper()} NULLS LAST"
    sql += f"\nLIMIT {limit}"

    if ";" in sql:  # invariant: single statement, always true by construction
        raise ExploreError("internal: multi-statement rejected")
    return sql, params


def run_with_comparison(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Run an explore for `window` and again for `compare_to`, then join the two
    on the grouped dimensions and emit per-row delta / pct_change.

    This makes period-over-period a SEMANTIC operation available to every explore
    (blind spot #10) rather than something each detector re-implements. Two
    guarded read-only queries — no cross-period SQL gymnastics.

    BOTH QUERIES RUN IN ONE SNAPSHOT. They used to open a transaction each, and
    a period-over-period comparison is the worst place for that: a deal that
    closes between the two reads lands in one period's numbers while the other
    period was measured before it existed, so the delta reports a change that
    never happened. The whole point of this function is to compare two windows
    of the SAME world."""
    cur_spec = dict(spec)
    prior = str(cur_spec.pop("compare_to"))
    base_window = cur_spec.get("window") or "all_time"
    prior_spec = {**cur_spec, "window": prior}

    with snapshot():
        cur_rows = run_readonly(*compile(cur_spec))
        prior_rows = run_readonly(*compile(prior_spec))

    dims = list(cur_spec.get("dimensions") or [])
    meas = list(cur_spec.get("measures") or ["count"])

    def key(r):
        return tuple(r.get(d) for d in dims)

    prior_by_key = {key(r): r for r in prior_rows}
    out_rows = []
    for r in cur_rows:
        row = dict(r)
        p = prior_by_key.get(key(r))
        for m in meas:
            cv, pv = r.get(m), (p or {}).get(m)
            try:
                cvf = float(cv) if cv is not None else None
                pvf = float(pv) if pv is not None else None
            except (TypeError, ValueError):
                cvf = pvf = None
            row[f"{m}__prior"] = pv
            row[f"{m}__delta"] = (round(cvf - pvf, 2)
                                  if cvf is not None and pvf is not None else None)
            row[f"{m}__pct_change"] = (round(100.0 * (cvf - pvf) / pvf, 1)
                                       if cvf is not None and pvf not in (None, 0) else None)
        out_rows.append(row)

    cols = columns_for(cur_spec)
    for m in meas:
        md = M.resolved_explore(cur_spec["explore"])["measures"][m]
        cols.append({"key": f"{m}__prior", "label": f"{md['label']} (prior)",
                     "format": md.get("format", "number"), "kind": "measure"})
        cols.append({"key": f"{m}__delta", "label": f"{md['label']} Δ",
                     "format": md.get("format", "number"), "kind": "measure"})
        cols.append({"key": f"{m}__pct_change", "label": f"{md['label']} %Δ",
                     "format": "percentage", "kind": "measure"})

    return {"spec": cur_spec, "compare_to": prior, "columns": cols, "rows": out_rows,
            "note": (f"{_interpret(cur_spec)}\n\nComparing **{base_window}** vs "
                     f"**{prior}** — each measure carries `__prior`, `__delta` and "
                     f"`__pct_change`.")}


def columns_for(spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """Ordered column metadata (dims then measures) for the formatter."""
    e = M.resolved_explore(spec["explore"])
    cols: List[Dict[str, str]] = []
    for d in (spec.get("dimensions") or []):
        cols.append({"key": d, "label": e["dimensions"][d]["label"], "format": "text", "kind": "dimension"})
    for m in (spec.get("measures") or ["count"]):
        md = e["measures"][m]
        cols.append({"key": m, "label": md["label"], "format": md.get("format", "number"), "kind": "measure"})
    return cols


# ============================================================================
# EXECUTE  (read-only transaction — the DB-enforced guarantee)
# ============================================================================

# ── Shared read snapshot ─────────────────────────────────────────────────────
# Postgres defaults to READ COMMITTED, where EVERY STATEMENT takes a fresh
# snapshot — so several metrics read back-to-back come from several database
# states even on one connection (measured: 1001 then 1002 in one transaction
# with a concurrent insert). Any surface that presents multiple numbers together
# was therefore internally inconsistent while stamping a single `as_of`.
#
# `snapshot()` pins ONE repeatable-read connection for a block; run_readonly
# reuses it when active and otherwise behaves exactly as before. A ContextVar
# rather than a parameter, because the callers that need this (compute, compare,
# series, the detectors) are several layers above the executor and threading a
# connection through them would put transaction plumbing in every signature.
_snapshot_conn: "contextvars.ContextVar[Optional[Any]]" = contextvars.ContextVar(
    "readonly_snapshot_conn", default=None)


# A leaked or long-running snapshot holds a transaction open, which pins the
# xmin horizon and stops VACUUM reclaiming dead tuples anywhere in the database.
# On a volume that is already near full, that turns a forgotten `with snapshot()`
# into disk exhaustion rather than a slow query. The timeout makes an abandoned
# snapshot self-terminate instead of accumulating bloat indefinitely.
SNAPSHOT_IDLE_TIMEOUT_MS = int(os.getenv("ANALYTICS_SNAPSHOT_IDLE_MS", "30000"))


@contextlib.contextmanager
def snapshot():
    """Pin one REPEATABLE READ snapshot for every run_readonly inside the block.

    Read-only by construction, so a serialization failure cannot occur and the
    reader never blocks a writer. Degrades to per-statement behaviour if the
    isolation level cannot be set — a slightly inconsistent answer beats none.

    NESTING REUSES THE OUTER SNAPSHOT. A nested block that opened its own
    connection would silently read a DIFFERENT point in time while the code read
    as though everything inside shared one — the precise illusion this function
    exists to remove. Composition has to be safe: `compute_many` and
    `detect_all` both open snapshots, and either may end up inside the other.

    NOT INHERITED BY THREADS (verified): a thread starts with a fresh context, so
    work handed to a thread inside a block runs UNPINNED. That is safe — no two
    threads ever share this connection — but it is not consistent, so read work
    that must share the snapshot has to stay on the calling thread.

    ASYNCIO TASKS DO INHERIT (verified). Tasks spawned inside a block share this
    one blocking connection; keep concurrent fan-out out of a snapshot."""
    outer = _snapshot_conn.get()
    if outer is not None:
        yield outer                      # reuse — do not open a second snapshot
        return

    conn = None
    token = None
    try:
        conn = get_connection()
        try:
            conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
            with conn.cursor() as cur:
                cur.execute("SET LOCAL idle_in_transaction_session_timeout = %s",
                            (SNAPSHOT_IDLE_TIMEOUT_MS,))
        except Exception as exc:
            logger.debug(f"[analytics] snapshot isolation unavailable: {exc}")
        token = _snapshot_conn.set(conn)
        yield conn
    finally:
        if token is not None:
            _snapshot_conn.reset(token)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


def in_snapshot() -> bool:
    return _snapshot_conn.get() is not None


def run_readonly(sql: str, params: List[Any]) -> List[Dict[str, Any]]:
    pinned = _snapshot_conn.get()
    if pinned is not None:
        # Inside a snapshot: reuse the pinned transaction. Do NOT close it —
        # the context manager owns its lifecycle.
        with pinned.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(STMT_TIMEOUT_MS)}")
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    conn = get_connection()
    try:
        conn.set_session(readonly=True)          # DB refuses any write/DDL
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(STMT_TIMEOUT_MS)}")
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        conn.rollback()                          # nothing to commit
        return rows
    finally:
        conn.close()


# ============================================================================
# PLAN  (NL → spec, via the LLM, grounded in the catalog)
# ============================================================================

_PLANNER_SYSTEM = """You translate a natural-language analytics question into a \
JSON query over a FIXED semantic model. Output ONLY a JSON object — no prose, no \
markdown.

Shape:
{"explore": "<one explore name>",
 "dimensions": ["<dimension key>", ...],   // group-by fields; may be empty
 "measures": ["<measure key>", ...],       // aggregates; default ["count"]
 "filters": [{"field":"<filter key>","op":"eq|ne|gt|gte|lt|lte|in|contains|between","value":<v>}],
 "order_by": {"field":"<a selected dimension or measure key>","dir":"asc|desc"},  // optional
 "limit": <int 1-1000>}

Rules:
- Use ONLY the explore names and field KEYS in the catalog below. Never invent keys.
- Pick the single explore that best fits the question.
- "by X" / "per X" / "grouped by X" → X is a dimension. "how many" → measure count.
  revenue/amount → total_amount; win rate → win_rate; conversion → conversion_rate.
- If the question cannot be answered from the catalog, output {"error":"<short reason>"}.

CATALOG:
"""


def _parse_spec_json(raw: str) -> Optional[Dict[str, Any]]:
    """Parse the planner's JSON spec. NOT graph_utils.parse_ai_json — that one
    only accepts objects carrying a `mode` key (the report-agent contract); an
    explore spec has explore/dimensions/measures instead. Tolerant of ```json
    fences and surrounding prose."""
    if not raw:
        return None
    txt = raw.strip()
    m = re.search(r"```json\s*([\s\S]*?)```", txt) or re.search(r"```\s*([\s\S]*?)```", txt)
    if m:
        txt = m.group(1).strip()
    else:
        b = re.search(r"\{[\s\S]*\}", txt)   # first {...} through last }
        if b:
            txt = b.group(0)
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _llm_spec(nl: str, repair: Optional[str] = None) -> Optional[Dict[str, Any]]:
    from app.core.graph_utils import _get_llm
    sys = _PLANNER_SYSTEM + M.catalog_for_prompt()
    user = nl if not repair else f"{nl}\n\n(Your previous answer was invalid: {repair}. Fix it.)"
    try:
        resp = _get_llm().invoke([{"role": "system", "content": sys},
                                  {"role": "user", "content": user}])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        return _parse_spec_json(raw)
    except Exception as exc:
        logger.warning(f"[explore] planner LLM failed: {exc}")
        return None


def plan_and_run(nl: str) -> Dict[str, Any]:
    """NL question → executed result. Returns {spec, columns, rows, note} or
    {error}. Never raises; never returns SQL to the caller."""
    if not ENABLED:
        return {"error": "Ad-hoc exploration is disabled (ANALYTICS_EXPLORE_ENABLED=0)."}

    spec = _llm_spec(nl)
    if not spec:
        return {"error": "Could not interpret that as an analytics query."}
    if spec.get("error"):
        return {"error": str(spec["error"])}

    # Compile with one repair round-trip on a validation error.
    try:
        sql, params = compile(spec)
    except ExploreError as first:
        spec = _llm_spec(nl, repair=str(first)) or {}
        if spec.get("error"):
            return {"error": str(spec["error"])}
        try:
            sql, params = compile(spec)
        except ExploreError as second:
            return {"error": f"Could not build a valid query: {second}"}

    try:
        rows = run_readonly(sql, params)
    except Exception as exc:
        logger.warning(f"[explore] execution failed: {exc}")
        return {"error": f"Query failed to run: {str(exc)[:160]}"}

    return {
        "spec": spec,
        "columns": columns_for(spec),
        "rows": rows,
        "note": _interpret(spec),
    }


def _interpret(spec: Dict[str, Any]) -> str:
    e = M.EXPLORES[spec["explore"]]
    dims = spec.get("dimensions") or []
    meas = spec.get("measures") or ["count"]
    dim_lbls = ", ".join(e["dimensions"][d]["label"] for d in dims) or "(overall)"
    meas_lbls = ", ".join(e["measures"][m]["label"] for m in meas)
    txt = f"Explored **{e['label']}** grouped by _{dim_lbls}_ — measures: {meas_lbls}"
    if spec.get("filters"):
        fl = "; ".join(f"{f.get('field')} {f.get('op')} {f.get('value')}"
                       for f in spec["filters"])
        txt += f" · filters: {fl}"
    # Trusted Context: when a measure IS a registered metric, state its canonical
    # definition so the number is self-explaining and provably the same metric the
    # briefings and detectors use.
    for line in _measure_definitions(meas):
        txt += f"\n\n_{line}_"
    return txt


def _measure_definitions(measures: List[str]) -> List[str]:
    """Canonical definitions for any selected measure that is a registered metric."""
    out = []
    try:
        from app.core import metrics
        for name in measures:
            m = metrics.REGISTRY.get(name)
            if m:
                out.append(f"{m.label}: {m.definition}")
    except Exception as exc:
        logger.debug(f"[explore] metric definitions skipped: {exc}")
    return out


# ============================================================================
# Router
# ============================================================================

router = APIRouter(tags=["analytics-explore"])


@router.get("/analytics/explore/catalog")
def explore_catalog():
    """The semantic model the planner is grounded in (explores + field keys)."""
    return {"enabled": ENABLED,
            "relationships": M.RELATIONSHIPS,
            "explores": {
                n: {"label": e["label"],
                    "dimensions": list(e["dimensions"]),
                    "measures": list(e["measures"]),
                    "filters": list(e["filters"]),
                    "joins": {j: {"label": d.get("label", j),
                                  "cardinality": d.get("cardinality")}
                              for j, d in (e.get("joins") or {}).items()}}
                for n, e in M.EXPLORES.items()}}


@router.post("/analytics/explore")
def explore_run(body: Dict[str, Any]):
    """Run an ad-hoc explore. Body is either {"nl": "<question>"} (LLM-planned)
    or {"spec": {...}} (a compiled spec directly, for the UI/tests).

    Wrapped in a snapshot. One spec compiles to one statement today, so this is
    already consistent — the pin makes the guarantee structural rather than
    incidental, so a future compiler that emits a second query (a total row, a
    cardinality probe) cannot silently mix two database states.

    CROSS-REQUEST consistency is NOT achievable and should not be implied: an
    explore result and a /metrics tile fetched separately are two snapshots.
    Use GET /metrics?names=… when several numbers must agree with each other."""
    if not ENABLED:
        return {"error": "Ad-hoc exploration is disabled."}
    if body.get("spec"):
        try:
            spec = body["spec"]
            if spec.get("compare_to"):
                return run_with_comparison(spec)
            sql, params = compile(spec)
            rows = run_readonly(sql, params)
            return {"spec": spec, "columns": columns_for(spec),
                    "rows": rows, "note": _interpret(spec)}
        except ExploreError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Query failed: {str(exc)[:160]}"}
    nl = str(body.get("nl") or "").strip()
    if not nl:
        return {"error": "Provide 'nl' (a question) or 'spec' (a compiled query)."}
    return plan_and_run(nl)
