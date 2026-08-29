"""Do the deployed functions still reference columns that exist?

WHY THIS EXISTS. `convert_lead` sat in BOTH databases writing to
`accounts.name`, `opportunities.source`, `opportunities.campaign_id` and
`activities.activity_type` -- four columns that had been renamed or removed
years earlier. It raised `UndefinedColumn` the moment it was reached. Nothing
noticed, because nothing called it: a function is only type-checked against the
schema when it RUNS, so a rename can leave a deployed function permanently
broken and completely silent until the day something invokes it.

That is the class. This finds the rest of them.

WHAT POSTGRES CANNOT DO FOR US, checked rather than assumed:

  * `plpgsql_check` is not installed and installing an extension in production
    is not a change this check gets to make on its own.
  * `pg_depend` records ZERO dependencies from public functions to tables. A
    PL/pgSQL body is an opaque string to the catalog, so introspection cannot
    see a column reference inside one. (SQL-language functions do register
    dependencies, which is why those 11 are comparatively safe.)

So this is a STATIC analyser, and the honest thing to do with a static analyser
is to state exactly how far it reaches.

WHAT IT CHECKS -- constructs where the target table is unambiguous:

    INSERT INTO <table> (col, col, ...)      the form that broke convert_lead
    UPDATE <table> SET col = ...             un-aliased only

WHAT IT DELIBERATELY DOES NOT CHECK, each because a wrong answer here is worse
than no answer:

    SELECT lists            joins and CTEs make the owning table ambiguous
    aliased references      `e.amount` may be a CTE, a record or a table
    NEW.x / OLD.x / v_rec.x composite fields, not table columns
    EXECUTE format(...)     dynamic SQL is not statically resolvable at all

Unresolvable constructs are COUNTED AND REPORTED, never silently treated as
clean -- a detector that says "no findings" when it means "I could not look" is
the failure mode this codebase keeps meeting.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple

from app.core.database import get_connection

# Schemas whose functions are not ours to police.
_OURS = ("public",)

# Languages worth analysing. `c` and `internal` are extension bodies.
_LANGS = ("plpgsql", "sql")

_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_SQ_STRING = re.compile(r"'(?:[^']|'')*'", re.S)
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_]\w*)?\$")

_INSERT = re.compile(
    r"\bINSERT\s+INTO\s+(?:public\.)?([A-Za-z_]\w*)\s*\(([^)]*)\)", re.I)
# UPDATE <table> SET col = ... -- only the un-aliased form, so the table that
# owns the column is not in doubt.
_UPDATE = re.compile(
    r"\bUPDATE\s+(?:public\.)?([A-Za-z_]\w*)\s+SET\s+(.*?)(?:\bWHERE\b|\bFROM\b|\bRETURNING\b|;)",
    re.I | re.S)
_DYNAMIC = re.compile(r"\bEXECUTE\b\s+(?!PROCEDURE|FUNCTION)", re.I)

# A reference wrapped in BEGIN ... EXCEPTION WHEN undefined_table/undefined_column
# ... END is not broken; it is OPTIONAL BY DESIGN. `trgfn_payment_update_invoice`
# writes to `invoice_events` exactly this way -- the table does not exist here,
# the trigger fires on every payment (513 in the last 30 days), and nothing
# fails because the author anticipated the absence.
#
# Reporting that as a defect would be the detector crying wolf on the very
# discipline it is meant to encourage. It is reported SEPARATELY instead:
# still visible, because a guard can also hide a rename nobody intended, but
# not counted as a finding.
_GUARDED_ERRORS = ("undefined_table", "undefined_column", "undefined_object",
                   "others")


# ---------------------------------------------------------------------------
# DISPOSITIONED STALE FUNCTIONS
# ---------------------------------------------------------------------------
# Triaged 2026-08-28 by reachability: trigger bindings, SP-to-SP calls, and
# Python call sites, each checked rather than assumed. An entry here is a
# DECISION WITH A REASON, in the same idiom as dsar.EXCLUDED and
# postdeploy_verify.DECLARED_DRIFT -- not a way to get the check green.
#
# THE ONE THAT IS DELIBERATELY ABSENT is fn_update_opportunity_momentum. It is
# reachable, it is broken, and it stays a finding until it is fixed. Adding it
# here would be using the disposition list to hide the defect the detector was
# built to find.
#
# function -> why its stale reference is not a defect to fix
DECLARED_STALE: Dict[str, str] = {
    # convert_lead's entry was REMOVED 2026-08-28, by the stale-declaration
    # check rather than by anyone remembering: drop_convert_lead.sql took the
    # function out and the declaration stopped matching anything the same run.
    # An excuse that outlives its defect is itself a defect.
    # customer_management was REMOVED 2026-08-28 with the local-only
    # fossil batch. The stale-declaration check reported it the same
    # run the drop landed, which is what that check is for.
    # sp_cases was DROPPED 2026-08-28 by sql/drop_sp_cases.sql, so its
    # declaration is gone from here. It was never really a stale-reference
    # exception: nine of its fourteen modes had rotted against case_metrics
    # and case_comments.id, but five still RAN, and two of those wrote the
    # exact fields (status, owner_id) that app/core/cases.py exists to audit.
    # Listing it as a broken-reference fossil described the nine and missed
    # the five. See tests/test_case_second_boundary.py.
    "sp_ai_assist":
        "DEAD. No caller anywhere. Writes case_sentiment, a table that does "
        "not exist -- part of the same retired case-management generation as "
        "sp_cases.",
    "sp_snapshot_forecast_accuracy":
        "DEAD. No caller. Writes forecast_accuracy_snapshots, which does not "
        "exist; forecast accuracy is captured by the scheduled "
        "_run_capture_forecast_snapshot path instead.",
    "trgfn_unified_events":
        "DEAD. Bound to no trigger and called by nothing, despite the name. "
        "Writes invoice_events, which does not exist. The live event path is "
        "trgfn_payment_update_invoice, which references the same table behind "
        "an EXCEPTION guard.",
    "sp_seed_accounts":
        "SEED FOSSIL. No caller; writes accounts.name, renamed to "
        "account_name. Seeding is done by app/core/demo.py now.",
    "sp_seed_contacts":
        "SEED FOSSIL. No caller; writes contacts.billing_address / "
        "shipping_address, both moved to the addresses table.",
    "sp_seed_products":
        "SEED FOSSIL. No caller; writes products.name, renamed to "
        "product_name.",
    "sp_opportunities_seed_owner_performance":
        "SEED FOSSIL. No caller; writes opportunity_products columns "
        "(opportunity_product_id, unit_price) that no longer exist.",
    "sp_sale_forecast_seed_product_pricing":
        "SEED FOSSIL. No caller; writes product_pricing columns (cost, "
        "promo_price, retail_price, wholesale_price) replaced by the "
        "price_type / price_value model.",
}


def _guarded_spans(body: str):
    """Character ranges covered by a BEGIN ... EXCEPTION WHEN <guard> ... END.

    Scanned rather than parsed: PL/pgSQL blocks nest, and a parser is not worth
    building for a heuristic whose only job is to keep a report honest. The
    span runs from the BEGIN preceding a guarded EXCEPTION back far enough to
    cover the statements it protects.
    """
    spans = []
    for m in re.finditer(r"\bEXCEPTION\s+WHEN\s+([a-z_ ]+?)\s+THEN", body, re.I):
        guards = [g.strip().lower() for g in re.split(r"\bOR\b", m.group(1), flags=re.I)]
        if not any(g in _GUARDED_ERRORS for g in guards):
            continue
        begin = body.rfind("BEGIN", 0, m.start())
        if begin != -1:
            spans.append((begin, m.start()))
    return spans


def _in_span(pos: int, spans) -> bool:
    return any(a <= pos < b for a, b in spans)


_CTE_NAME = re.compile(r"(?:\bWITH\b|,)\s*([A-Za-z_]\w*)\s+AS\s*\(", re.I)


_TEMP_TABLE = re.compile(r"CREATE\s+(?:GLOBAL\s+|LOCAL\s+)?TEMP(?:ORARY)?\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)", re.I)


def _cte_names(body: str) -> Set[str]:
    """Objects the function creates for itself, which are NOT public tables.

    Two kinds, both invisible to `information_schema.columns` scoped to public:

      * CTE names. PostgreSQL allows data-modifying CTEs, so
        `INSERT INTO <cte> (...)` inside a WITH is valid and names nothing in
        the catalog.
      * TEMP TABLES. `generate_random_orders` does
        `CREATE TEMP TABLE _new_order_ids(...)` and then inserts into it. A
        temp table lives in pg_temp_*, so a public-schema lookup cannot see it.

    Reporting either as a missing table would be the detector inventing a
    defect -- the failure mode that gets a check muted faster than missing one
    does. This function is named for what these have in common: the object is
    created by the function, for the function.
    """
    return ({m.group(1).lower() for m in _CTE_NAME.finditer(body)}
            | {m.group(1).lower() for m in _TEMP_TABLE.finditer(body)})


def _strip_noise(body: str) -> str:
    """Remove comments and string literals.

    Both are places a column NAME can appear without being a column
    REFERENCE -- an error message mentioning `activity_type`, for instance.
    Counting those would produce findings nobody can act on, which is how a
    detector gets muted.
    """
    body = _COMMENT_BLOCK.sub(" ", body)
    body = _COMMENT_LINE.sub(" ", body)
    body = _SQ_STRING.sub("''", body)
    return body


def _inner_body(definition: str) -> str:
    """The function body between its dollar quotes, or the whole definition."""
    tags = list(_DOLLAR_TAG.finditer(definition))
    if len(tags) >= 2:
        return definition[tags[0].end():tags[-1].start()]
    return definition


def _columns_of(cur) -> Dict[str, Set[str]]:
    cur.execute("""SELECT table_name, column_name
                     FROM information_schema.columns
                    WHERE table_schema = 'public'""")
    out: Dict[str, Set[str]] = {}
    for t, col in cur.fetchall():
        out.setdefault(t, set()).add(col)
    return out


def _split_cols(raw: str) -> List[str]:
    return [c.strip().strip('"') for c in raw.replace("\n", " ").split(",")
            if c.strip() and re.fullmatch(r'"?[A-Za-z_]\w*"?', c.strip())]


def analyse(only: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """Every application function whose writes name a column that is gone.

    `findings` are actionable and specific. `unresolved` is the part the
    analyser could not reason about, reported so the result is never mistaken
    for a clean bill of health.
    """
    findings: List[Dict[str, Any]] = []
    guarded_refs: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    checked = 0

    conn = get_connection()
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            live = _columns_of(cur)
            cur.execute("""SELECT p.proname, l.lanname, pg_get_functiondef(p.oid)
                             FROM pg_proc p
                             JOIN pg_namespace n ON n.oid = p.pronamespace
                             JOIN pg_language  l ON l.oid = p.prolang
                            WHERE n.nspname = ANY(%s) AND l.lanname = ANY(%s)
                              AND p.prokind IN ('f', 'p')
                            ORDER BY p.proname""", (list(_OURS), list(_LANGS)))
            rows = cur.fetchall()
    finally:
        conn.close()

    for name, lang, definition in rows:
        if only and name not in only:
            continue
        checked += 1
        body = _strip_noise(_inner_body(definition or ""))
        guarded = _guarded_spans(body)
        ctes = _cte_names(body)

        if _DYNAMIC.search(body):
            unresolved.append({"function": name, "reason": "builds SQL dynamically "
                               "(EXECUTE); column references cannot be resolved "
                               "statically"})

        for m in _INSERT.finditer(body):
            table, raw = m.group(1), m.group(2)
            if table.lower() in ctes:
                continue                     # a WITH name, not a table
            bucket = guarded_refs if _in_span(m.start(), guarded) else findings
            if table not in live:
                bucket.append({"function": name, "language": lang,
                               "statement": "INSERT", "table": table,
                               "missing": ["<table does not exist>"]})
                continue
            gone = [c for c in _split_cols(raw) if c not in live[table]]
            if gone:
                bucket.append({"function": name, "language": lang,
                               "statement": "INSERT", "table": table,
                               "missing": sorted(gone)})

        for m in _UPDATE.finditer(body):
            table, sets = m.group(1), m.group(2)
            if table not in live:
                continue                     # may be a CTE name; not a finding
            assigned = [c.strip().strip('"') for c in
                        re.findall(r"(?:^|,)\s*\"?([A-Za-z_]\w*)\"?\s*=", sets)]
            gone = [c for c in assigned if c not in live[table]]
            if gone:
                bucket = guarded_refs if _in_span(m.start(), guarded) else findings
                bucket.append({"function": name, "language": lang,
                               "statement": "UPDATE", "table": table,
                               "missing": sorted(set(gone))})

    # Split declared from undeclared LAST, so a disposition can never hide a
    # finding the analyser did not already surface.
    declared = [f for f in findings if f["function"] in DECLARED_STALE]
    findings = [f for f in findings if f["function"] not in DECLARED_STALE]
    stale_declarations = sorted(set(DECLARED_STALE) -
                                {f["function"] for f in declared})

    return {
        "checked": checked,
        "findings": findings,
        "declared": declared,
        "stale_declarations": stale_declarations,
        "guarded": guarded_refs,
        "unresolved": unresolved,
        # A declaration matching nothing is itself a defect: the function was
        # fixed or dropped and the excuse outlived it.
        "ok": not findings and not stale_declarations,
        "boundary": ("statically-resolvable INSERT column lists and un-aliased "
                     "UPDATE ... SET targets only. SELECT lists, aliased "
                     "references, NEW/OLD/record fields and dynamic SQL are "
                     "NOT analysed; the functions using them are listed under "
                     "`unresolved` rather than reported clean."),
    }


def main() -> int:
    import json
    r = analyse()
    print(json.dumps({k: r[k] for k in ("checked", "ok", "findings", "unresolved")},
                     indent=2))
    print(f"\nboundary: {r['boundary']}")
    if r["findings"]:
        print(f"\n{len(r['findings'])} function(s) write columns that no longer "
              f"exist. Each raises UndefinedColumn the moment it is reached, "
              f"and stays silent until then.")
        return 1
    print(f"\n{r['checked']} function(s) analysed, no stale column writes. "
          f"{len(r['unresolved'])} could not be fully analysed (see above).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
