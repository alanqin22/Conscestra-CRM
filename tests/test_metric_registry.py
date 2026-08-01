"""Guards for the Trusted Semantic Core.

Two classes of regression, both of which shipped silently before 2026-07-30:

  DRIFT  (finding #1) — a metric redefined in SQL while metrics.py says otherwise.
                        The generated sql/metric_registry.sql is the contract;
                        if it no longer matches the generator, the SQL surfaces
                        are running an older definition than the Python ones.

  SCHEMA (finding #9) — ~100 hardcoded column references across metrics.py and
                        semantic_model.py, none validated. A renamed column
                        surfaced as a 500 to whoever asked the question.

The schema tests need a database and SKIP without one, so the drift tests still
run in a bare CI container.

    python -m pytest tests/test_metric_registry.py -v
"""

from __future__ import annotations

import os
import re

import pytest

from app.core import metric_sql, metrics
from app.core import semantic_model as M


# ===========================================================================
# Drift: the generated SQL must match the registry
# ===========================================================================

def test_generated_sql_is_current():
    """sql/metric_registry.sql matches what metrics.py produces right now.

    Fails when someone edits a metric definition in Python and forgets to run
    `python -m app.core.metric_sql`. Before this guard, the equivalent mistake
    was invisible: SQL simply kept its own formula forever."""
    assert metric_sql.check(), (
        "sql/metric_registry.sql is stale — regenerate with:\n"
        "    python -m app.core.metric_sql")


def test_generated_sql_carries_the_registry_conditions():
    """The win/loss conditions in the artifact are the registry's own strings,
    not a hand-copied paraphrase that could drift on the next edit."""
    sql = metric_sql.generate()
    assert metrics.WIN_NUM_COND in sql
    assert metrics.WIN_DEN_COND in sql
    assert metrics.DECISION_TS_SQL in sql


def test_no_deployed_function_redefines_an_outcome_metric(schema):
    """No function IN THE DATABASE may re-derive a win/loss outcome ratio.

    This replaces an earlier grep over sp/*.sql, which was inadequate twice over:
      • sp/ is gitignored, so it SKIPPED on every clean checkout — it guarded
        nothing in CI while passing locally;
      • it keyed on the literal token 'win_rate'. A stored procedure restating
        the formula against `stage` without that token passed the guard cleanly
        (demonstrated during review).

    Checking pg_proc.prosrc tests what is actually DEPLOYED, which is the thing
    that can serve a wrong number, and catches any spelling of the formula."""
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.proname, p.prosrc
                FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND p.prokind = 'f'
            """)
            funcs = cur.fetchall()
    finally:
        conn.close()

    if not funcs:
        pytest.skip("no functions deployed to this database")

    offenders = []
    for name, src in funcs:
        if name.startswith("fn_metric_"):
            continue                                   # IS the registry artifact
        body = src or ""
        if "fn_metric_win_rate" in body or "v_opportunity_outcome" in body:
            continue                                   # consumes the registry
        # An outcome ratio = a won-count/sum divided by a won+lost denominator,
        # in either column. Deliberately spelling-agnostic.
        for m in re.finditer(r"(?is)(count|sum)\s*\([^)]*\)\s*FILTER\s*\(\s*WHERE[^)]*"
                             r"closed_won[^)]*\)\s*(?:::[a-z]+)?\s*/", body):
            tail = body[m.end():m.end() + 400]
            if "closed_lost" in tail.lower():
                line = body[:m.start()].count("\n") + 1
                offenders.append(f"{name}() line ~{line}")
                break

    assert not offenders, (
        "these DEPLOYED functions compute their own win/loss ratio instead of "
        "calling fn_metric_win_rate / reading v_opportunity_outcome:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_no_python_surface_redefines_a_registered_metric():
    """No Python module may hand-roll SQL for a metric the registry owns.

    The SQL-side guard above would never have caught ceo_briefing, which
    computed won_revenue as `status='closed_won' AND updated_at >= now()-7d` —
    the wrong time axis, reporting $5,276,400 where the registry said $402,101.
    Registered metrics must go through metrics.compute / compute_sql."""
    app_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app")

    # Modules allowed to write raw outcome SQL, with the reason.
    ALLOWED = {
        "metrics.py",          # the registry itself
        "metric_sql.py",       # the generator
        "semantic_model.py",   # consumes the registry's shared fragments
        "data_readiness.py",   # profiles data QUALITY, not the business metric
        "demo.py",             # seed/demo integrity counts, not reporting
    }

    # sum(amount) over closed_won scoped by a NON-decision timestamp = a
    # re-derived won_revenue on the wrong clock.
    pat = re.compile(r"(?is)sum\s*\(\s*[a-z_.]*amount\s*\)(?:[^;]{0,300}?)"
                     r"closed_won(?:[^;]{0,300}?)(updated_at|created_at)\s*>=")
    offenders = []
    for root, _dirs, files in os.walk(app_dir):
        for fname in files:
            if not fname.endswith(".py") or fname in ALLOWED:
                continue
            path = os.path.join(root, fname)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            for m in pat.finditer(body):
                rel = os.path.relpath(path, app_dir).replace("\\", "/")
                offenders.append(f"app/{rel}:{body[:m.start()].count(chr(10)) + 1} "
                                 f"(scoped by {m.group(1)}, not the decision date)")

    assert not offenders, (
        "these modules re-derive a registered metric instead of calling "
        "metrics.compute / metrics.compute_sql:\n  " + "\n  ".join(offenders))


def test_ratio_metrics_are_not_additive():
    """A ratio summed across groups is a correctness bug; `additive` must say so."""
    for name, m in metrics.REGISTRY.items():
        if m.kind == "ratio":
            assert not m.additive, f"{name} is a ratio but claims to be additive"


def test_win_rate_pair_shares_one_definition():
    """win_rate and win_rate_value differ ONLY in weighting — same win/loss
    conditions, same time axis. If they ever diverge, 'we win 90% of deals but
    99% of dollars' stops being one coherent statement."""
    count_wr, value_wr = metrics.REGISTRY["win_rate"], metrics.REGISTRY["win_rate_value"]
    assert count_wr.numerator_cond == value_wr.numerator_cond
    assert count_wr.denominator_cond == value_wr.denominator_cond
    assert count_wr.time_field == value_wr.time_field
    assert count_wr.ratio_agg != value_wr.ratio_agg


# ===========================================================================
# Schema: every hardcoded column reference must exist  (finding #9)
# ===========================================================================

_IDENT = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")

# Alias → table, for the aliases the semantic model and registry use in `base`.
_ALIAS_RE = re.compile(r"\b([a-z_]+)\s+(?:AS\s+)?([a-z]{1,4})\b", re.IGNORECASE)

# SQL keywords / functions that look like alias.column but are not.
_NOT_COLUMNS = {"to_char", "date_trunc", "extract", "interval", "now"}


def _schema():
    """{table: {column, ...}} from the live DB, or None when unreachable."""
    try:
        from app.core.database import get_connection
        conn = get_connection()
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public'")
            out = {}
            for table, col in cur.fetchall():
                out.setdefault(table, set()).add(col)
        return out or None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def schema():
    s = _schema()
    if s is None:
        pytest.skip("no database reachable — schema validation skipped")
    return s


def _aliases(base: str, joins: dict) -> dict:
    """Map every alias in a FROM/JOIN clause to its table name."""
    text = base + " " + " ".join(j["sql"] for j in (joins or {}).values())
    text = re.sub(r"\b(LEFT|RIGHT|INNER|OUTER|FULL|JOIN|ON|AND|OR)\b", " ",
                  text, flags=re.IGNORECASE)
    out = {}
    for part in re.split(r"\s*=\s*|\s{2,}|,", text):
        m = _ALIAS_RE.search(part.strip())
        if m and m.group(1).lower() not in _NOT_COLUMNS:
            out[m.group(2).lower()] = m.group(1).lower()
    return out


def _refs(sql: str) -> set:
    return {(a.lower(), c.lower()) for a, c in _IDENT.findall(sql or "")
            if a.lower() not in _NOT_COLUMNS}


def test_semantic_model_columns_exist(schema):
    """Every dimension / measure / filter in every explore resolves against the
    real schema. A rename used to surface as a runtime 500 on the first question
    that touched the field."""
    bad = []
    for name, ex in M.EXPLORES.items():
        alias = _aliases(ex["base"], ex.get("joins"))
        fields = list(ex.get("dimensions", {}).values()) + \
                 list(ex.get("measures", {}).values()) + \
                 list(ex.get("filters", {}).values())
        sqls = [f["sql"] for f in fields if isinstance(f, dict) and f.get("sql")]
        sqls += ex.get("mandatory_where", [])
        for sql in sqls:
            for a, col in _refs(sql):
                table = alias.get(a)
                if table is None or table not in schema:
                    continue      # alias we could not resolve — not a column claim
                if col not in schema[table]:
                    bad.append(f"{name}: {a}.{col} (no {table}.{col})")
    assert not bad, "semantic_model references columns that do not exist:\n  " + \
                    "\n  ".join(sorted(set(bad)))


def test_registry_columns_exist(schema):
    """Same guard for the Metric Registry's own SQL fragments."""
    bad = []
    for name, m in metrics.REGISTRY.items():
        alias = _aliases(m.base, {})
        parts = [m.numerator_cond, m.denominator_cond, m.agg_expr,
                 m.row_cond, m.base_where, m.ratio_agg]
        if m.time_field and m.time_field != "decided":
            parts.append(m.time_field)
        for sql in [p for p in parts if p]:
            for a, col in _refs(sql):
                table = alias.get(a)
                if table and table in schema and col not in schema[table]:
                    bad.append(f"{name}: {a}.{col} (no {table}.{col})")
    assert not bad, "metrics.REGISTRY references columns that do not exist:\n  " + \
                    "\n  ".join(sorted(set(bad)))


def test_decision_timestamp_columns_exist(schema):
    """decided_at / close_date must both exist — the canonical time axis is a
    COALESCE over them, and a missing one silently changes what a window means."""
    opp = schema.get("opportunities", set())
    assert "decided_at" in opp, "opportunities.decided_at missing — run sql/metric_registry_migration.sql"
    assert "close_date" in opp


# ===========================================================================
# Live agreement: Python and SQL must return the SAME number
# ===========================================================================

def test_python_and_sql_win_rate_agree(schema):
    """The end-to-end contract. /metrics (Python) and fn_metric_win_rate (SQL)
    must produce identical figures — that equality IS the fix for finding #1."""
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # to_regclass resolves RELATIONS only and returns NULL for a
            # function — using it here made this test skip itself silently.
            cur.execute("SELECT to_regprocedure('public.fn_metric_win_rate(date,date)')")
            if cur.fetchone()[0] is None:
                pytest.skip("sql/metric_registry.sql not applied to this database")
            cur.execute("SELECT fn_metric_win_rate(NULL, NULL) -> 'summary'")
            sql_summary = cur.fetchone()[0]
    finally:
        conn.close()

    py_count = metrics.compute("win_rate", "all_time")
    py_value = metrics.compute("win_rate_value", "all_time")

    assert float(sql_summary["win_rate"]) == pytest.approx(py_count["value"], abs=0.05), \
        f"SQL win_rate {sql_summary['win_rate']} != Python {py_count['value']}"
    assert float(sql_summary["win_rate_value"]) == pytest.approx(py_value["value"], abs=0.05), \
        f"SQL win_rate_value {sql_summary['win_rate_value']} != Python {py_value['value']}"
    assert int(sql_summary["won_count"]) == py_count["numerator"]
    assert int(sql_summary["decided_count"]) == py_count["denominator"]


@pytest.mark.parametrize("days_back", [90, 365])
def test_explore_and_metrics_agree_on_a_time_window(schema, days_back):
    """Explore and /metrics must bucket a TIME-FILTERED question identically.

    The original verification only ever compared them with NO time filter, which
    is exactly the case where they cannot disagree. With one, they did: Explore
    ran on close_date and /metrics on decided_at, so "win rate this year" was
    99.5% vs 89.2% over 868 vs 1072 deals.

    Both sides are driven through their PUBLIC interfaces over one explicit date
    range — no string surgery on compiled SQL, so the test exercises the paths a
    user actually reaches."""
    import datetime as dt

    from app.core import semantic_query as SQ

    hi = dt.date.today()
    lo = hi - dt.timedelta(days=days_back)

    # /metrics side: the registry's own decision expression, bounded.
    ts = metrics.decision_ts()
    py = metrics.compute(
        "win_rate", "all_time",
        extra_where=f"{ts} >= DATE '{lo}' AND {ts} < DATE '{hi}' + 1")

    # Explore side: the same bounds via the explore's `decided_at` filter.
    spec = {"explore": "opportunities", "dimensions": [],
            "measures": ["win_rate", "won_count", "lost_count"],
            "filters": [{"field": "decided_at", "op": "between",
                         "value": [str(lo), str(hi)]}]}
    ex = SQ.run_readonly(*SQ.compile(spec))[0]

    decided = int(ex["won_count"] or 0) + int(ex["lost_count"] or 0)
    assert decided == py["denominator"], (
        f"last {days_back}d: Explore sees {decided} decided deals, /metrics sees "
        f"{py['denominator']} — the two surfaces are on different time axes")
    if py["value"] is not None:
        assert float(ex["win_rate"]) == pytest.approx(py["value"], abs=0.05)


def test_explore_default_time_grain_is_the_decision_date():
    """`month`/`quarter`/`year` on the opportunities explore must be the DECISION
    grain, so "win rate by month" matches /metrics month by month. Close-date
    grains remain available under explicit close_* names."""
    dims = M.EXPLORES["opportunities"]["dimensions"]
    for key in ("month", "quarter", "year"):
        assert "decided_at" in dims[key]["sql"], \
            f"explore dimension '{key}' is not on the decision axis"
    for key in ("close_month", "close_quarter", "close_year"):
        assert key in dims and "close_date" in dims[key]["sql"]
        assert "Close" in dims[key]["label"]


def test_every_explore_guards_soft_deletes(schema):
    """Each explore must exclude soft-deleted rows, or Explore silently reports
    on records every other surface has dropped. Opportunities use
    status='deleted' rather than an is_deleted flag, which is why that explore
    was missed."""
    missing = []
    for name, ex in M.EXPLORES.items():
        guards = " ".join(ex.get("mandatory_where") or [])
        table = ex["base"].split()[0]
        cols = schema.get(table, set())
        needs = ("is_deleted" in cols or "deleted_at" in cols
                 or (table == "opportunities"))
        if needs and not any(t in guards for t in
                             ("is_deleted", "deleted_at", "deleted")):
            missing.append(f"{name} (base {table})")
    assert not missing, "explores with no soft-delete guard: " + ", ".join(missing)


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Asia/Tokyo"])
@pytest.mark.parametrize("window", ["this_month", "this_quarter", "ytd", "prev_month"])
def test_calendar_windows_do_not_move_with_session_timezone(schema, zone, window):
    """A metric definition must mean the same thing on every connection.

    date_trunc() on a timestamptz resolves boundaries in the SESSION's TimeZone,
    so a deal decided 2026-08-01 02:00 UTC bucketed into AUGUST under
    TimeZone=UTC and into JULY under America/New_York — one definition string,
    two meanings, decided by session state rather than by a second formula."""
    from app.core.database import get_connection
    m = metrics.REGISTRY["win_rate"]
    pred = metrics._window_pred(m, window)
    counts = []
    for z in (zone, "UTC"):
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET TimeZone='{z}'")
                cur.execute(f"SELECT count(*) FROM {m.base} "
                            f"WHERE ({m.base_where}) AND ({pred})")
                counts.append(cur.fetchone()[0])
        finally:
            conn.close()
    assert counts[0] == counts[1], (
        f"{window} population differs between TimeZone={zone} and UTC: {counts}")


def test_ceo_briefing_holds_one_snapshot(schema):
    """Sharing a connection is NOT sharing a snapshot. Under the READ COMMITTED
    default every statement takes a fresh one — measured at 1001 then 1002 in a
    single transaction with a concurrent insert. A briefing built that way
    reports a dozen figures from a dozen database states under one `as_of`."""
    import inspect

    src = inspect.getsource(__import__("app.core.ceo_briefing",
                                       fromlist=["gather"]).gather)
    assert "REPEATABLE READ" in src, \
        "ceo_briefing.gather no longer pins a snapshot — figures can disagree"


def test_multiple_metrics_share_one_snapshot(schema):
    """Several metrics read together must come from ONE database state.

    Under READ COMMITTED each statement takes a fresh snapshot, so reading three
    metrics gives three moments while the response claims a single `as_of`.
    Measured before the fix: won_revenue read 402,100.81 then 902,100.81 across a
    concurrent insert."""
    import threading
    import time

    from app.core.database import get_connection
    from app.core.semantic_query import snapshot

    writer = get_connection()
    writer.autocommit = True

    def add_won():
        time.sleep(0.05)
        with writer.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunities "
                "(account_id,name,stage,status,amount,close_date,decided_at) "
                "SELECT account_id,'ZZ snapshot test','closed_won','closed_won',"
                "500000,current_date,now() FROM accounts LIMIT 1")

    try:
        with snapshot():
            a = metrics.compute("won_revenue", "last_7d")["value"]
            t = threading.Thread(target=add_won)
            t.start()
            t.join()
            time.sleep(0.05)
            b = metrics.compute("won_revenue", "last_7d")["value"]
        assert a == b, (f"snapshot did not hold: {a} then {b} — metrics read "
                        f"together are seeing different database states")
    finally:
        with writer.cursor() as cur:
            cur.execute("DELETE FROM opportunities WHERE name='ZZ snapshot test'")
        writer.close()


def test_batch_endpoint_declares_its_snapshot(schema):
    """The batch read is the only way grouped numbers can share an `as_of`;
    six HTTP calls are six snapshots however the UI batches them."""
    res = metrics.compute_many(["win_rate", "won_revenue"], "all_time")
    assert res["snapshot"] == "repeatable_read"
    assert set(res["metrics"]) == {"win_rate", "won_revenue"}
    assert res["metrics"]["win_rate"]["value"] == \
        metrics.compute("win_rate", "all_time")["value"]


def test_nested_snapshots_reuse_the_outer_one(schema):
    """A nested block must NOT open a second snapshot.

    It would read a different point in time while the code reads as though
    everything inside shared one — the exact illusion snapshots exist to remove.
    Composition matters here: compute_many and detect_all both open snapshots
    and either can end up inside the other."""
    from app.core.semantic_query import _snapshot_conn, snapshot

    with snapshot():
        outer = _snapshot_conn.get()
        assert outer is not None
        with snapshot():
            assert _snapshot_conn.get() is outer, \
                "nested snapshot opened a second connection — two points in time"
        assert _snapshot_conn.get() is outer and not outer.closed, \
            "inner block closed the outer snapshot"


def test_detector_pass_never_escapes_the_snapshot(schema):
    """Every query in a detector pass must be pinned.

    detect_stalled_deals used execute_sp, which opens its own connection and
    escaped the pin, so the pass mixed instants: a breach could be invented by
    one detector reading after a deal closed while another read before."""
    from app.core import analytics_signals as AS
    from app.core import semantic_query as SQ

    pinned_flags = []
    original = SQ.run_readonly

    def spy(sql, params):
        pinned_flags.append(SQ.in_snapshot())
        return original(sql, params)

    SQ.run_readonly = spy
    try:
        AS.detect_all()
    finally:
        SQ.run_readonly = original

    assert pinned_flags, "detect_all issued no run_readonly queries"
    assert all(pinned_flags), \
        f"{pinned_flags.count(False)} detector query/queries ran outside the snapshot"


def test_snapshot_bounds_its_own_lifetime(schema):
    """An abandoned snapshot holds a transaction open, pinning the xmin horizon
    so VACUUM cannot reclaim dead tuples anywhere in the database. On a nearly
    full volume that is disk exhaustion, not a slow query."""
    from app.core.semantic_query import SNAPSHOT_IDLE_TIMEOUT_MS, snapshot

    assert 0 < SNAPSHOT_IDLE_TIMEOUT_MS <= 300_000
    with snapshot() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW idle_in_transaction_session_timeout")
            assert cur.fetchone()[0] not in ("0", "0ms"), \
                "snapshot connection has no idle timeout — a leak pins the xmin horizon"


def test_period_comparison_runs_in_one_snapshot():
    """Explore's compare_to path runs TWO queries. Split across snapshots, a
    deal closing between them lands in one period and not the other, so the
    delta reports a change that never happened."""
    import inspect

    from app.core import semantic_query as SQ
    src = inspect.getsource(SQ.run_with_comparison)
    assert "with snapshot():" in src, \
        "run_with_comparison no longer pins a snapshot across its two queries"


def test_ceo_briefing_uses_the_registry(schema):
    """The briefing's won-revenue figure must equal the registry's won_revenue
    over last_7d. This is the assertion that would have caught the 13x gap."""
    from app.core import ceo_briefing
    from app.core.database import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            brief = ceo_briefing._registry_value("won_revenue", "last_7d", cur)
    finally:
        conn.close()
    assert brief == pytest.approx(metrics.compute("won_revenue", "last_7d")["value"],
                                  abs=0.01)
