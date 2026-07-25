"""Semantic model for ad-hoc analytics (blindspot A2).

The Analytics agent could only answer from ~15 pre-baked sections of one stored
proc. This registry is the curated menu that makes genuine self-service
exploration possible WITHOUT letting a model write SQL: an "explore" declares a
vetted FROM/JOIN base plus an allow-list of dimensions, measures and filterable
fields — EVERY one mapped to a SQL fragment WE wrote here. The LLM (and the
compiler in semantic_query.py) only ever reference these string KEYS; no
identifier is ever taken from model output. Adding a new explore/field is a data
edit in this file, not new code.

Each field's `sql` is trusted, hand-written, and read-only by construction
(SELECT-side expressions only — no subqueries that write, no DDL). Filter VALUES
supplied by a caller are bound as parameters by the compiler, never inlined.
"""

from __future__ import annotations

from typing import Any, Dict, List

# The canonical win-rate fragment lives in the Metric Registry (P0 step 2), so the
# explore's win_rate is THE SAME definition as /metrics and the anomaly detectors
# — unified by construction, not three formulas that happen to agree. metrics.py
# imports nothing from this module, so this import is safe (no cycle).
from app.core.metrics import WIN_RATE_PCT_SQL


# ── Reusable time-grain dimension builders ──────────────────────────────────
def _month(col: str) -> Dict[str, str]:
    return {"sql": f"to_char(date_trunc('month', {col}), 'YYYY-MM')", "label": "Month"}


def _quarter(col: str) -> Dict[str, str]:
    return {"sql": f"to_char(date_trunc('quarter', {col}), 'YYYY \"Q\"Q')", "label": "Quarter"}


def _year(col: str) -> Dict[str, str]:
    return {"sql": f"EXTRACT(YEAR FROM {col})::int", "label": "Year"}


# ── The model ────────────────────────────────────────────────────────────────
# explore → {label, base (FROM/JOIN), mandatory_where[], dimensions{}, measures{}, filters{}}
EXPLORES: Dict[str, Dict[str, Any]] = {
    "opportunities": {
        "label": "Opportunities (pipeline / sales)",
        "base": "opportunities o LEFT JOIN accounts a ON a.account_id = o.account_id",
        "mandatory_where": [],
        "time_field": "o.close_date",
        # Optional joins the compiler adds ONLY when a selected field needs them.
        # cardinality 'one' = at most one joined row (safe for additive measures);
        # 'many' = fan-out (the compiler refuses additive measures — see #5 guard).
        "joins": {
            "owner": {"sql": "LEFT JOIN owners ow ON ow.owner_id = o.owner_id",
                      "cardinality": "one", "label": "Owner (sales rep)"},
        },
        "dimensions": {
            "owner_name":     {"sql": "NULLIF(TRIM(COALESCE(ow.first_name,'')||' '||"
                                      "COALESCE(ow.last_name,'')),'')",
                               "label": "Owner", "join": "owner"},
            "stage":          {"sql": "o.stage",          "label": "Stage"},
            "status":         {"sql": "o.status",         "label": "Status"},
            "lead_source":    {"sql": "o.lead_source",    "label": "Lead Source"},
            "industry":       {"sql": "a.industry",       "label": "Industry"},
            "employee_band":  {"sql": "a.employee_band",  "label": "Employee Band"},
            "revenue_band":   {"sql": "a.revenue_band",   "label": "Revenue Band"},
            "month":          _month("o.close_date"),
            "quarter":        _quarter("o.close_date"),
            "year":           _year("o.close_date"),
        },
        "measures": {
            "count":            {"sql": "count(*)", "label": "Count", "format": "number"},
            "total_amount":     {"sql": "COALESCE(sum(o.amount),0)", "label": "Total Amount", "format": "currency"},
            "avg_amount":       {"sql": "COALESCE(avg(o.amount),0)", "label": "Avg Amount", "format": "currency"},
            "weighted_amount":  {"sql": "COALESCE(sum(o.amount*COALESCE(o.probability,0)/100.0),0)", "label": "Weighted Amount", "format": "currency"},
            "avg_probability":  {"sql": "ROUND(COALESCE(avg(o.probability),0),1)", "label": "Avg Probability", "format": "number"},
            "total_margin":     {"sql": "COALESCE(sum(o.total_margin),0)", "label": "Total Margin", "format": "currency"},
            "won_count":        {"sql": "count(*) FILTER (WHERE o.status='closed_won')", "label": "Won", "format": "number"},
            "lost_count":       {"sql": "count(*) FILTER (WHERE o.status='closed_lost')", "label": "Lost", "format": "number"},
            "win_rate":         {"sql": WIN_RATE_PCT_SQL,
                                 "label": "Win Rate %", "format": "percentage"},
        },
        "filters": {
            "status": "o.status", "stage": "o.stage", "lead_source": "o.lead_source",
            "industry": "a.industry", "employee_band": "a.employee_band",
            "revenue_band": "a.revenue_band", "amount": "o.amount",
            "close_date": "o.close_date", "created_at": "o.created_at",
            "owner_name": {"sql": "NULLIF(TRIM(COALESCE(ow.first_name,'')||' '||"
                                  "COALESCE(ow.last_name,'')),'')", "join": "owner"},
        },
    },

    "orders": {
        "label": "Orders (revenue)",
        "base": "orders ord LEFT JOIN accounts a ON a.account_id = ord.account_id",
        "mandatory_where": ["ord.deleted_at IS NULL"],
        "time_field": "ord.order_date",
        "dimensions": {
            "status":    {"sql": "ord.status",  "label": "Status"},
            "channel":   {"sql": "ord.channel", "label": "Channel"},
            "source":    {"sql": "ord.source",  "label": "Source"},
            "industry":  {"sql": "a.industry",  "label": "Industry"},
            "month":     _month("ord.order_date"),
            "quarter":   _quarter("ord.order_date"),
            "year":      _year("ord.order_date"),
        },
        "measures": {
            "count":         {"sql": "count(*)", "label": "Count", "format": "number"},
            "total_amount":  {"sql": "COALESCE(sum(ord.total_amount),0)", "label": "Total Amount", "format": "currency"},
            "avg_amount":    {"sql": "COALESCE(avg(ord.total_amount),0)", "label": "Avg Order Value", "format": "currency"},
        },
        "filters": {
            "status": "ord.status", "channel": "ord.channel", "source": "ord.source",
            "industry": "a.industry", "total_amount": "ord.total_amount",
            "order_date": "ord.order_date",
        },
    },

    "leads": {
        "label": "Leads (marketing funnel)",
        "base": "leads l",
        "mandatory_where": ["COALESCE(l.is_deleted, false) = false"],
        "time_field": "l.created_at",
        "dimensions": {
            "source":     {"sql": "l.source",    "label": "Source"},
            "status":     {"sql": "l.status",    "label": "Status"},
            "rating":     {"sql": "l.rating",    "label": "Rating"},
            "industry":   {"sql": "l.industry",  "label": "Industry"},
            "province":   {"sql": "l.province",  "label": "Province"},
            "converted":  {"sql": "l.converted", "label": "Converted"},
            "month":      _month("l.created_at"),
            "quarter":    _quarter("l.created_at"),
            "year":       _year("l.created_at"),
        },
        "measures": {
            "count":            {"sql": "count(*)", "label": "Count", "format": "number"},
            "avg_score":        {"sql": "ROUND(COALESCE(avg(l.score),0),1)", "label": "Avg Score", "format": "number"},
            "converted_count":  {"sql": "count(*) FILTER (WHERE l.converted)", "label": "Converted", "format": "number"},
            "conversion_rate":  {"sql": "ROUND(100.0*count(*) FILTER (WHERE l.converted) / NULLIF(count(*),0),1)",
                                 "label": "Conversion Rate %", "format": "percentage"},
        },
        "filters": {
            "source": "l.source", "status": "l.status", "rating": "l.rating",
            "industry": "l.industry", "province": "l.province",
            "converted": "l.converted", "score": "l.score", "created_at": "l.created_at",
        },
    },

    # ── Accounts ───────────────────────────────────────────────────────────
    "accounts": {
        "label": "Accounts (customers / companies)",
        "base": "accounts a",
        "mandatory_where": ["COALESCE(a.is_deleted,false)=false"],
        "time_field": "a.created_at",
        "joins": {
            # FAN-OUT join: one account has many opportunities. The compiler
            # refuses ordinary aggregates while it is active (they would
            # double-count accounts); only fanout_safe measures are allowed.
            "opportunities": {"sql": "LEFT JOIN opportunities o ON o.account_id = a.account_id",
                              "cardinality": "many", "label": "Opportunities"},
        },
        "dimensions": {
            "industry":       {"sql": "a.industry",       "label": "Industry"},
            "type":           {"sql": "a.type",           "label": "Type"},
            "status":         {"sql": "a.status",         "label": "Status"},
            "employee_band":  {"sql": "a.employee_band",  "label": "Employee Band"},
            "revenue_band":   {"sql": "a.revenue_band",   "label": "Revenue Band"},
            "month":          _month("a.created_at"),
            "year":           _year("a.created_at"),
            "opp_stage":      {"sql": "o.stage", "label": "Opportunity Stage",
                               "join": "opportunities"},
        },
        "measures": {
            "count":             {"sql": "count(*)", "label": "Count", "format": "number"},
            "distinct_accounts": {"sql": "count(DISTINCT a.account_id)",
                                  "label": "Accounts", "format": "number",
                                  "fanout_safe": True},
            "owned_pct":         {"sql": "ROUND(100.0*count(*) FILTER (WHERE a.owner_id IS NOT NULL)"
                                         " / NULLIF(count(*),0),1)",
                                  "label": "Owned %", "format": "percentage"},
        },
        "filters": {
            "industry": "a.industry", "type": "a.type", "status": "a.status",
            "employee_band": "a.employee_band", "revenue_band": "a.revenue_band",
            "created_at": "a.created_at",
        },
    },

    # ── Contacts ───────────────────────────────────────────────────────────
    "contacts": {
        "label": "Contacts (people)",
        "base": "contacts c LEFT JOIN accounts a ON a.account_id = c.account_id",
        "mandatory_where": ["COALESCE(c.is_deleted,false)=false"],
        "time_field": "c.created_at",
        "dimensions": {
            "status":      {"sql": "c.status",      "label": "Status"},
            "role":        {"sql": "c.role",        "label": "Role"},
            "industry":    {"sql": "a.industry",    "label": "Account Industry"},
            "is_customer": {"sql": "COALESCE(c.is_customer,false)", "label": "Is Customer"},
            "month":       _month("c.created_at"),
            "year":        _year("c.created_at"),
        },
        "measures": {
            "count":          {"sql": "count(*)", "label": "Count", "format": "number"},
            "verified_pct":   {"sql": "ROUND(100.0*count(*) FILTER (WHERE COALESCE(c.is_email_verified,false))"
                                      " / NULLIF(count(*),0),1)",
                               "label": "Email Verified %", "format": "percentage"},
            "reachable_pct":  {"sql": "ROUND(100.0*count(*) FILTER (WHERE COALESCE(c.email,'')<>''"
                                      " OR COALESCE(c.phone,'')<>'') / NULLIF(count(*),0),1)",
                               "label": "Reachable %", "format": "percentage"},
        },
        "filters": {
            "status": "c.status", "role": "c.role", "industry": "a.industry",
            "is_customer": "c.is_customer", "created_at": "c.created_at",
        },
    },

    # ── Activities ─────────────────────────────────────────────────────────
    "activities": {
        "label": "Activities (calls / meetings / tasks)",
        "base": "activities act",
        "mandatory_where": [],
        "time_field": "act.created_at",
        "dimensions": {
            "type":      {"sql": "act.type",      "label": "Type"},
            "status":    {"sql": "act.status",    "label": "Status"},
            "channel":   {"sql": "act.channel",   "label": "Channel"},
            "direction": {"sql": "act.direction", "label": "Direction"},
            "outcome":   {"sql": "act.outcome",   "label": "Outcome"},
            "month":     _month("act.created_at"),
            "quarter":   _quarter("act.created_at"),
        },
        "measures": {
            "count":          {"sql": "count(*)", "label": "Count", "format": "number"},
            "completed_pct":  {"sql": "ROUND(100.0*count(*) FILTER (WHERE act.completed_at IS NOT NULL)"
                                      " / NULLIF(count(*),0),1)",
                               "label": "Completed %", "format": "percentage"},
        },
        "filters": {
            "type": "act.type", "status": "act.status", "channel": "act.channel",
            "direction": "act.direction", "outcome": "act.outcome",
            "created_at": "act.created_at",
        },
    },

    # ── Invoices (AR) ──────────────────────────────────────────────────────
    "invoices": {
        "label": "Invoices (accounts receivable)",
        "base": "invoices i LEFT JOIN accounts a ON a.account_id = i.account_id",
        "mandatory_where": ["COALESCE(i.is_deleted,false)=false"],
        "time_field": "i.issue_date",
        "dimensions": {
            "status":    {"sql": "i.status",    "label": "Status"},
            "industry":  {"sql": "a.industry",  "label": "Industry"},
            "month":     _month("i.issue_date"),
            "quarter":   _quarter("i.issue_date"),
            "year":      _year("i.issue_date"),
        },
        "measures": {
            "count":         {"sql": "count(*)", "label": "Count", "format": "number"},
            "total_amount":  {"sql": "COALESCE(sum(i.total_amount),0)",
                              "label": "Invoiced", "format": "currency"},
            "balance_due":   {"sql": "COALESCE(sum(i.balance_due),0)",
                              "label": "Balance Due", "format": "currency"},
            "avg_amount":    {"sql": "COALESCE(avg(i.total_amount),0)",
                              "label": "Avg Invoice", "format": "currency"},
            "overdue_pct":   {"sql": "ROUND(100.0*count(*) FILTER (WHERE i.due_date < CURRENT_DATE"
                                     " AND COALESCE(i.balance_due,0) > 0) / NULLIF(count(*),0),1)",
                              "label": "Overdue %", "format": "percentage"},
            "avg_days_to_pay": {"sql": "ROUND(AVG(EXTRACT(EPOCH FROM (i.paid_at - i.issue_date))"
                                       "/86400.0)::numeric,1)",
                                "label": "Avg Days to Pay", "format": "number"},
        },
        "filters": {
            "status": "i.status", "industry": "a.industry",
            "total_amount": "i.total_amount", "balance_due": "i.balance_due",
            "issue_date": "i.issue_date", "due_date": "i.due_date",
        },
    },

    # ── Payments ───────────────────────────────────────────────────────────
    "payments": {
        "label": "Payments (cash received)",
        "base": "payments p LEFT JOIN accounts a ON a.account_id = p.account_id",
        "mandatory_where": ["COALESCE(p.is_deleted,false)=false"],
        "time_field": "p.payment_date",
        "dimensions": {
            "status":         {"sql": "p.status",         "label": "Status"},
            "payment_method": {"sql": "p.payment_method", "label": "Method"},
            "industry":       {"sql": "a.industry",       "label": "Industry"},
            "month":          _month("p.payment_date"),
            "quarter":        _quarter("p.payment_date"),
        },
        "measures": {
            "count":        {"sql": "count(*)", "label": "Count", "format": "number"},
            "total_amount": {"sql": "COALESCE(sum(p.amount),0)",
                             "label": "Received", "format": "currency"},
            "avg_amount":   {"sql": "COALESCE(avg(p.amount),0)",
                             "label": "Avg Payment", "format": "currency"},
        },
        "filters": {
            "status": "p.status", "payment_method": "p.payment_method",
            "industry": "a.industry", "amount": "p.amount",
            "payment_date": "p.payment_date",
        },
    },

    # ── Support cases ──────────────────────────────────────────────────────
    "cases": {
        "label": "Support cases",
        "base": "cases cs LEFT JOIN accounts a ON a.account_id = cs.account_id",
        "mandatory_where": [],
        "time_field": "cs.created_at",
        "dimensions": {
            "status":   {"sql": "cs.status",   "label": "Status"},
            "priority": {"sql": "cs.priority", "label": "Priority"},
            "origin":   {"sql": "cs.origin",   "label": "Origin"},
            "industry": {"sql": "a.industry",  "label": "Industry"},
            "month":    _month("cs.created_at"),
        },
        "measures": {
            "count":       {"sql": "count(*)", "label": "Count", "format": "number"},
            "closed_pct":  {"sql": "ROUND(100.0*count(*) FILTER (WHERE cs.closed_at IS NOT NULL)"
                                   " / NULLIF(count(*),0),1)",
                            "label": "Closed %", "format": "percentage"},
        },
        "filters": {
            "status": "cs.status", "priority": "cs.priority", "origin": "cs.origin",
            "industry": "a.industry", "created_at": "cs.created_at",
        },
    },
}


# ── Entity relationship graph (introspection / documentation) ────────────────
# What the semantic layer KNOWS about how entities relate. The compiler enforces
# the cardinality declared on each explore's `joins`; this is the readable map of
# the same knowledge, exposed via /analytics/explore/catalog so a planner (or a
# human) can see which cross-entity questions are answerable.
RELATIONSHIPS: List[Dict[str, str]] = [
    {"from": "opportunities", "to": "accounts", "kind": "belongs_to", "cardinality": "one"},
    {"from": "opportunities", "to": "owners",   "kind": "assigned_to", "cardinality": "one"},
    {"from": "contacts",      "to": "accounts", "kind": "belongs_to", "cardinality": "one"},
    {"from": "orders",        "to": "accounts", "kind": "belongs_to", "cardinality": "one"},
    {"from": "invoices",      "to": "accounts", "kind": "belongs_to", "cardinality": "one"},
    {"from": "invoices",      "to": "orders",   "kind": "bills",      "cardinality": "one"},
    {"from": "payments",      "to": "invoices", "kind": "settles",    "cardinality": "one"},
    {"from": "payments",      "to": "accounts", "kind": "belongs_to", "cardinality": "one"},
    {"from": "cases",         "to": "accounts", "kind": "belongs_to", "cardinality": "one"},
    {"from": "accounts",      "to": "opportunities", "kind": "has_many", "cardinality": "many"},
]


# ── Catalog helpers ──────────────────────────────────────────────────────────

# ── Custom-field sources (Platform P3) ──────────────────────────────────────
# Which entity + id-expression carries admin-defined custom fields, per explore.
# Custom fields declared on these entities become extra dimensions/filters so
# "total pipeline by customer_tier" or "filter renewal_date < 90d" work. The
# entity + id-expression here are OURS (safe to inline); field keys come from
# validated def slugs; filter VALUES stay bound params in semantic_query.compile.
CUSTOM_SOURCES: Dict[str, List] = {
    "opportunities": [("accounts", "o.account_id"), ("opportunities", "o.opportunity_id")],
    "orders":        [("accounts", "ord.account_id")],
    "leads":         [("leads", "l.lead_id")],
    "accounts":      [("accounts", "a.account_id")],
    "contacts":      [("contacts", "c.contact_id"), ("accounts", "c.account_id")],
    "invoices":      [("accounts", "i.account_id")],
    "payments":      [("accounts", "p.account_id")],
    "cases":         [("accounts", "cs.account_id")],
}


# Guarded text→typed expressions used ONLY when the typed columns aren't migrated
# yet. A raw `value_text::numeric` aborts the WHOLE query on one malformed value
# (the cast sits inside an aggregate); the regex guard yields NULL for that row
# instead, so a single dirty value can no longer break an entire explore.
_SAFE_NUMBER = (r"CASE WHEN v.value_text ~ '^\s*-?[0-9]+(\.[0-9]+)?\s*$' "
                r"THEN v.value_text::numeric END")
_SAFE_DATE = (r"CASE WHEN v.value_text ~ "
              r"'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$' "
              r"THEN v.value_text::date END")
_SAFE_BOOL = ("CASE WHEN lower(v.value_text) IN ('true','1','yes','y','on') THEN true "
              "WHEN lower(v.value_text) IN ('false','0','no','n','off') THEN false END")


def _cf_expr(entity: str, id_expr: str, field_key: str, ftype: str) -> str:
    """SQL for one custom field as a dimension/filter expression.

    Prefers the TYPED column (value_number/value_date/value_bool — no cast in the
    query path at all) once sql/custom_field_typed.sql is applied; otherwise falls
    back to a REGEX-GUARDED cast of value_text. Either way a malformed value
    resolves to NULL rather than erroring the query. The scalar subquery is served
    by the (entity, entity_id, field_key) primary key, so it is an index lookup —
    a JOIN rewrite would add fan-out risk with several custom fields for no gain."""
    try:
        from app.core import custom_fields
        typed = custom_fields.has_typed_columns()
    except Exception:
        typed = False

    if ftype == "number":
        inner = "v.value_number" if typed else _SAFE_NUMBER
    elif ftype == "date":
        inner = "v.value_date" if typed else _SAFE_DATE
    elif ftype == "bool":
        inner = "v.value_bool" if typed else _SAFE_BOOL
    else:
        inner = "v.value_text"
    return (f"(SELECT {inner} FROM custom_field_values v "
            f"WHERE v.entity='{entity}' AND v.entity_id={id_expr} "
            f"AND v.field_key='{field_key}')")


def resolved_explore(name: str) -> Dict[str, Any]:
    """The explore's static model MERGED with admin-defined custom fields (as
    dimensions + filters). Built fresh per call so newly-authored fields appear
    immediately. Custom keys never shadow a built-in (built-in wins)."""
    e = EXPLORES.get(name)
    if not e:
        return {}
    sources = CUSTOM_SOURCES.get(name)
    if not sources:
        return e
    try:
        from app.core import custom_fields
        import copy
        merged = copy.deepcopy(e)
        seen = set(merged["dimensions"]) | set(merged["measures"]) | set(merged["filters"])
        for (entity, id_expr) in sources:
            for d in custom_fields.list_defs(entity):
                key = d["field_key"]
                if key in seen:
                    continue
                expr = _cf_expr(entity, id_expr, key, d["field_type"])
                merged["dimensions"][key] = {"sql": expr, "label": d["label"], "custom": True}
                merged["filters"][key] = expr
                seen.add(key)
        return merged
    except Exception:
        return e


def explore(name: str) -> Dict[str, Any]:
    return resolved_explore(name)


def catalog_for_prompt() -> str:
    """Compact catalog the LLM planner is grounded in — explores + the exact
    field KEYS it may use (never any raw column/table names). Includes
    admin-defined custom fields so the planner can group/filter by them."""
    lines: List[str] = []
    for ename in EXPLORES:
        e = resolved_explore(ename)
        lines.append(f"- explore \"{ename}\" — {e['label']}")
        lines.append(f"    dimensions: {', '.join(e['dimensions'].keys())}")
        lines.append(f"    measures:   {', '.join(e['measures'].keys())}")
        lines.append(f"    filters:    {', '.join(e['filters'].keys())}")
    return "\n".join(lines)


def field_label(ename: str, kind: str, key: str) -> str:
    """kind ∈ {dimensions, measures}."""
    return resolved_explore(ename).get(kind, {}).get(key, {}).get("label", key)
