"""Metric Registry → SQL. The bridge that makes the registry actually canonical.

THE PROBLEM THIS SOLVES (audit finding #1, 2026-07-30):
`metrics.py` declared itself "the ONE canonical definition per business metric",
and it genuinely unified every PYTHON surface. But the highest-traffic surface —
the opportunities chat agent — reads `sp_opportunities(mode='win_rate')`, and a
stored procedure cannot `import app.core.metrics`. So the SP kept its own
formula, and so did `sp_orchestrator`. Three definitions of "win rate" stayed
live, differing in:

    outcome column   status          vs  stage (with closed_paid OR'd in)
    time axis        decided_at      vs  close_date         vs  none at all
    weighting        by deal count   vs  by dollar amount (unregistered)

They agreed numerically only by luck: `stage` and `status` happened to be in
sync, with one row already disagreeing and no CHECK constraint to keep them that
way. Over a 30-day window the two axes already sorted 353 vs 305 opportunities
into the period — the same percentage computed over a 16%-different population.

THE FIX: the registry stays the single source of truth, and SQL CONSUMES it
rather than restating it. This module renders the registry's own fragments into
a checked-in SQL artifact (a view + a function); the SPs call that artifact and
define nothing themselves. `tests/test_metric_registry.py` fails the build if the
checked-in .sql drifts from what this generator emits — so changing a definition
in Python and forgetting the SQL is a red test, not a silent disagreement.

    metrics.REGISTRY ──generate()──▶ sql/metric_registry.sql
                                          │
                          ┌───────────────┴────────────────┐
                          ▼                                ▼
                  v_opportunity_outcome            fn_metric_win_rate()
                  (one row per opp:                (summary + by_owner +
                   is_win / is_loss / decided_ts)   by_lead_source)
                          │                                │
                          └──────── sp_opportunities ──────┘
                                    sp_orchestrator

REGENERATE:  python -m app.core.metric_sql          (writes sql/metric_registry.sql)
             python -m app.core.metric_sql --check  (exit 1 if stale; used by CI)

The output is intentionally checked in rather than applied at boot: a stored
procedure's dependencies must exist before the SP is created, and DB deploys here
are a deliberate, reviewed step (never automatic — see the deploy runbook).
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from app.core.metrics import (
    DECISION_TS_SQL,
    REGISTRY,
    WIN_DEN_COND,
    WIN_NUM_COND,
)

# sql/ sits beside app/ at the repo root.
from app.core.artifact_paths import SQL_DIR as _SQL_DIR
OUT_PATH = _SQL_DIR / "metric_registry.sql"

_BANNER = """\
-- ===========================================================================
--  GENERATED FILE — DO NOT EDIT BY HAND.
--
--  Source of truth : app/core/metrics.py  (the Metric Registry)
--  Generator       : app/core/metric_sql.py
--  Regenerate      : python -m app.core.metric_sql
--  CI guard        : tests/test_metric_registry.py (fails if this file is stale)
--
--  Every SQL surface that reports a win rate MUST read from the objects below.
--  Restating the formula in a stored procedure is what produced three divergent
--  "win rate" answers before 2026-07-30. If you need a new breakdown, add it
--  here (via the generator) — never re-derive is_win in the caller.
-- ===========================================================================
"""


def _view_sql() -> str:
    """One row per opportunity, carrying the registry's OWN win/loss conditions
    and decision timestamp. This is the only place SQL learns what a 'win' is."""
    return f"""
-- ---------------------------------------------------------------------------
-- v_opportunity_outcome — canonical sales outcome, per opportunity.
--
--   is_win / is_loss   from REGISTRY['win_rate'] numerator/denominator conds
--   decided_ts         from metrics.DECISION_TS_SQL
--
-- `status` is the authoritative outcome column; `stage` is pipeline POSITION
-- only. stage='closed_paid' is a post-win collections milestone and already
-- carries status='closed_won', so it needs no special case here — that OR'd
-- special case in the old SP is exactly what made the two surfaces diverge.
-- ---------------------------------------------------------------------------
-- RESTRICT (the default), never CASCADE. CASCADE was silently destroying any
-- view built on top of this one every time the artifact was re-applied —
-- demonstrated during review. Failing loudly on a dependency is the correct
-- behaviour: whoever owns the dependent view must be told, not surprised.
DROP VIEW IF EXISTS public.v_opportunity_outcome RESTRICT;
CREATE VIEW public.v_opportunity_outcome AS
SELECT
    o.opportunity_id,
    o.account_id,
    o.owner_id,
    o.name,
    o.lead_source,
    o.stage,
    o.status,
    o.amount,
    o.probability,
    o.close_date,
    o.created_at,
    ({WIN_NUM_COND})                       AS is_win,
    ({WIN_DEN_COND}) AND NOT ({WIN_NUM_COND}) AS is_loss,
    ({WIN_DEN_COND})                       AS is_decided,
    {DECISION_TS_SQL}                      AS decided_ts
FROM public.opportunities o;

COMMENT ON VIEW public.v_opportunity_outcome IS
  'GENERATED from app/core/metrics.py. Canonical win/loss/decision-date per '
  'opportunity. Do not re-derive these conditions in a stored procedure.';
"""


def _function_sql() -> str:
    """Both registered win-rate metrics + the standard breakdowns, in one call.

    Returns count-weighted AND value-weighted figures together so a caller can
    never render one while believing it is the other (they were both labelled
    'Win Rate' in the chat table before this change)."""
    return """
-- ---------------------------------------------------------------------------
-- fn_metric_win_rate(p_from, p_to) — the ONLY SQL entry point for win rate.
--
-- Window is applied to decided_ts (the registry's time axis), NOT close_date.
-- NULL bounds mean unbounded, so a caller can ask for all-time.
--
-- Emits BOTH registered metrics, each under its registry name:
--     win_rate        deal-weighted   (REGISTRY['win_rate'])
--     win_rate_value  value-weighted  (REGISTRY['win_rate_value'])
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.fn_metric_win_rate(date, date);
CREATE FUNCTION public.fn_metric_win_rate(
    p_from date DEFAULT NULL,
    p_to   date DEFAULT NULL
) RETURNS json
LANGUAGE sql STABLE AS $$
WITH decided AS (
    SELECT *
    FROM public.v_opportunity_outcome
    WHERE is_decided
      AND (p_from IS NULL OR decided_ts >= p_from::timestamptz)
      AND (p_to   IS NULL OR decided_ts <  (p_to::timestamptz + interval '1 day'))
),
summary AS (
    SELECT
        count(*) FILTER (WHERE is_win)                    AS won_count,
        count(*) FILTER (WHERE is_loss)                   AS lost_count,
        count(*)                                          AS decided_count,
        COALESCE(sum(amount) FILTER (WHERE is_win),  0)    AS won_amount,
        COALESCE(sum(amount) FILTER (WHERE is_loss), 0)    AS lost_amount,
        COALESCE(sum(amount), 0)                          AS decided_amount
    FROM decided
),
by_owner AS (
    SELECT
        d.owner_id,
        COALESCE(ow.first_name || ' ' || ow.last_name, 'Unassigned') AS owner_name,
        count(*) FILTER (WHERE d.is_win)                  AS won_count,
        count(*) FILTER (WHERE d.is_loss)                 AS lost_count,
        count(*)                                          AS decided_count,
        COALESCE(sum(d.amount) FILTER (WHERE d.is_win), 0) AS won_amount,
        COALESCE(sum(d.amount), 0)                        AS decided_amount
    FROM decided d
    LEFT JOIN public.owners ow ON ow.owner_id = d.owner_id
    GROUP BY d.owner_id, ow.first_name, ow.last_name
),
by_lead_source AS (
    SELECT
        COALESCE(lead_source, 'Unknown')                  AS lead_source,
        count(*) FILTER (WHERE is_win)                    AS won_count,
        count(*) FILTER (WHERE is_loss)                   AS lost_count,
        count(*)                                          AS decided_count,
        COALESCE(sum(amount) FILTER (WHERE is_win), 0)     AS won_amount,
        COALESCE(sum(amount), 0)                          AS decided_amount
    FROM decided
    GROUP BY COALESCE(lead_source, 'Unknown')
)
SELECT json_build_object(
    'window', json_build_object('from', p_from, 'to', p_to, 'axis', 'decided_at'),
    'summary', (
        SELECT json_build_object(
            'won_count', won_count,
            'lost_count', lost_count,
            'decided_count', decided_count,
            'win_rate', ROUND(100.0 * won_count / NULLIF(decided_count, 0), 1),
            'won_amount', won_amount,
            'lost_amount', lost_amount,
            'decided_amount', decided_amount,
            'win_rate_value', ROUND(100.0 * won_amount / NULLIF(decided_amount, 0), 1)
        ) FROM summary
    ),
    'by_owner', COALESCE((
        SELECT json_agg(json_build_object(
            'owner_id', owner_id,
            'owner_name', owner_name,
            'won_count', won_count,
            'lost_count', lost_count,
            'decided_count', decided_count,
            'win_rate', ROUND(100.0 * won_count / NULLIF(decided_count, 0), 1),
            'won_amount', won_amount,
            'decided_amount', decided_amount,
            'win_rate_value', ROUND(100.0 * won_amount / NULLIF(decided_amount, 0), 1)
        ) ORDER BY won_amount DESC) FROM by_owner
    ), '[]'::json),
    'by_lead_source', COALESCE((
        SELECT json_agg(json_build_object(
            'lead_source', lead_source,
            'won_count', won_count,
            'lost_count', lost_count,
            'decided_count', decided_count,
            'win_rate', ROUND(100.0 * won_count / NULLIF(decided_count, 0), 1),
            'won_amount', won_amount,
            'decided_amount', decided_amount,
            'win_rate_value', ROUND(100.0 * won_amount / NULLIF(decided_amount, 0), 1)
        ) ORDER BY won_amount DESC) FROM by_lead_source
    ), '[]'::json)
);
$$;

COMMENT ON FUNCTION public.fn_metric_win_rate(date, date) IS
  'GENERATED from app/core/metrics.py. The only SQL entry point for win rate. '
  'Returns win_rate (deal-weighted) and win_rate_value (dollar-weighted) under '
  'their registry names, windowed on decided_at.';
"""


def _definitions_sql() -> str:
    """The registry's plain-language definitions, materialized so a SQL-side
    consumer (or a DBA reading the schema) sees the same Trusted-Context text
    the /metrics endpoint returns."""
    rows = []
    for m in REGISTRY.values():
        defn = m.definition.replace("'", "''")
        rows.append(
            f"    ('{m.name}', '{m.label}', '{m.unit}', '{m.kind}', "
            f"{'true' if m.additive else 'false'}, '{defn}')"
        )
    values = ",\n".join(rows)
    return f"""
-- ---------------------------------------------------------------------------
-- v_metric_definitions — the registry itself, readable from SQL. Lets a report
-- or a DBA answer "what exactly does this number mean?" without reading Python.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS public.v_metric_definitions RESTRICT;
CREATE VIEW public.v_metric_definitions
    (metric, label, unit, kind, additive, definition) AS
VALUES
{values};

COMMENT ON VIEW public.v_metric_definitions IS
  'GENERATED from app/core/metrics.py REGISTRY. Do not edit.';
"""


def generate() -> str:
    """The full SQL artifact, as a string."""
    return (_BANNER + _view_sql() + _function_sql() + _definitions_sql()).rstrip() + "\n"


def write(path: Path = OUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate(), encoding="utf-8")
    return path


def check(path: Path = OUT_PATH) -> bool:
    """True when the checked-in file matches the generator. Prints a diff if not."""
    want = generate()
    if not path.exists():
        print(f"MISSING: {path} — run: python -m app.core.metric_sql", file=sys.stderr)
        return False
    have = path.read_text(encoding="utf-8")
    if have == want:
        return True
    print(f"STALE: {path} no longer matches app/core/metrics.py\n", file=sys.stderr)
    sys.stderr.writelines(difflib.unified_diff(
        have.splitlines(keepends=True), want.splitlines(keepends=True),
        fromfile="checked-in", tofile="generated"))
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Render the Metric Registry to SQL.")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the checked-in SQL is stale (CI guard)")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check() else 1)
    print(f"wrote {write()}")
