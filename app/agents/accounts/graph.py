"""LangGraph agent module for the Accounts domain.

Mirrors the n8n Account API v2 workflow topology:
  pre_router ─┬─[direct_db]──→ db ──→ format → END
              └─[ai_agent]──→ ai_agent → parse ─┬─[call_db]──→ db → format → END
                                                 └─[skip_db]──→ format → END

All shared utilities (AgentState, LLM factory, JSON parser, graph builder)
come from app.core.graph_utils.  Domain-specific concerns (prompt, pre_router,
sql_builder, formatter) stay in this package.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from app.core.graph_utils import (
    AgentState,
    _get_llm,
    _call_ollama_direct,
    parse_ai_json,
    build_standard_graph,
)
from app.core.memory import get_history, save_turn
from app.core.config import get_settings
from app.core.database import execute_sp

from .prompt import ACCOUNT_AGENT_SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_accounts_query
from .formatter import format_response

logger = logging.getLogger(__name__)


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-Router — mirrors n8n Pre Router Code node v3.0."""
    logger.info("=== Accounts Pre-Router Node (v3.0) ===")
    user_input = state.get("user_input", "")
    chat_input = state.get("chat_input", {})
    logger.info(f"Session: {state.get('session_id')!r}  Message: {user_input[:100]}")

    result = route_request(user_input, chat_input)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Accounts pre-router ROUTED → mode={params.get('mode')}")
        return {**state, "router_action": True, "parsed_json": params, "should_call_api": True}

    logger.info(f"Accounts pre-router PASSTHRU → {result['current_message'][:80]}")
    return {**state, "router_action": False, "user_input": result["current_message"]}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the accounts system prompt."""
    logger.info("=== Accounts AI Agent Node ===")
    session_id = state.get("session_id", "default-session")
    user_input = state.get("user_input", "")

    settings   = get_settings()
    history    = get_history(session_id)
    messages   = history + [{"role": "user", "content": user_input}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": ACCOUNT_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(ACCOUNT_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Accounts AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Accounts AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Accounts Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")

    # When the AI emits only a bare [MODE:list] tag with no search term,
    # recover the search term from the original user message.
    if parsed and parsed.get("mode") == "list" and not parsed.get("search"):
        user_input = state.get("user_input") or ""
        name_m = re.search(
            r'(?:find|search|show|get|look\s*up|list)\s+'
            r'(?:accounts?\s+)?(?:named?\s+|called\s+|for\s+)?'
            r'["\']?([A-Za-z][A-Za-z\s\-\']{1,40}?)["\']?'
            r'\s*(?:$|[.,!?])',
            user_input, re.IGNORECASE,
        )
        if name_m:
            parsed["search"] = name_m.group(1).strip()
            logger.info(f"Recovered search term from user_input: {parsed['search']!r}")

    if parsed:
        return {**state, "parsed_json": parsed, "should_call_api": True}
    return {**state, "parsed_json": None, "should_call_api": False}


def db_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Database node — builds and executes sp_accounts()."""
    logger.info("=== Accounts Database Node ===")
    try:
        parsed_json = state.get("parsed_json") or {}
        if not parsed_json:
            return {**state, "db_rows": []}

        # ── Executive questions — sp_orchestrator('executive') pack with the
        # shared decision-grade format (headline, confidence, drivers, action).
        if parsed_json.get("mode") == "executive_question":
            from app.agents.orchestrator.executive import format_exec_answer
            rows = execute_sp("SELECT sp_orchestrator('executive') AS result")
            pack = (rows[0].get("result") or {}) if rows else {}
            text = format_exec_answer(pack,
                                      parsed_json.get("sections") or [],
                                      parsed_json.get("note"))
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": "executive_question"},
                "exec_markdown": text,
            }}]}

        # ── web_search — live internet lookup (ddgs free → Tavily fallback) ──
        if parsed_json.get("mode") == "web_search":
            from app.core.web_tools import web_answer
            text = web_answer(
                parsed_json.get("query") or state.get("user_input", ""),
                url=parsed_json.get("url"),
            )
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": "web_search"},
                "web_markdown": text,
            }}]}

        # ── UI-only marker modes — no DB call; formatter emits a [MODE:*]
        # marker the frontend uses to open an inline form.
        _ui_only_modes = {'show_account_form', 'show_account_update_form'}
        if parsed_json.get("mode") in _ui_only_modes:
            logger.info(f"db_node: UI-only mode '{parsed_json.get('mode')}' — skipping DB call")
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": parsed_json.get("mode")}
            }}]}

        # ── list_no_orders — direct SQL; result shape matches sp_accounts list mode
        if parsed_json.get("mode") == "list_no_orders":
            logger.info("db_node: list_no_orders — running direct SQL")
            _no_orders_sql = """
SELECT json_build_object(
    'accounts', COALESCE(json_agg(
        json_build_object(
            'account_id',        a.account_id::text,
            'account_name',      a.account_name,
            'type',              a.type,
            'industry',          a.industry,
            'status',            a.status,
            'email',             a.email,
            'phone',             a.phone,
            'city',              adr.city,
            'province',          adr.province,
            'country',           adr.country,
            'order_count',       0,
            'contact_count',     (SELECT COUNT(*) FROM contacts c  WHERE c.account_id  = a.account_id),
            'opportunity_count', (SELECT COUNT(*) FROM opportunities op WHERE op.account_id = a.account_id),
            'total_revenue',     0,
            'created_at',        a.created_at,
            'updated_at',        a.updated_at
        ) ORDER BY a.account_name
    ), '[]'::json),
    'metadata', json_build_object(
        'page', 1, 'total_pages', 1,
        'total_records', (
            SELECT COUNT(*) FROM accounts a2
            WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.account_id = a2.account_id)
              AND (a2.is_deleted IS NULL OR a2.is_deleted = false)
        )
    )
) AS result
FROM accounts a
LEFT JOIN LATERAL (
    SELECT city, province, country FROM addresses
    WHERE parent_id = a.account_id AND parent_type = 'account' AND label = 'billing'
    ORDER BY is_default DESC NULLS LAST, created_at LIMIT 1
) adr ON true
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.account_id = a.account_id)
  AND (a.is_deleted IS NULL OR a.is_deleted = false)
"""
            db_rows = execute_sp(_no_orders_sql)
            updated_params = {**parsed_json, "mode": "list", "_listMode": "list_no_orders"}
            return {**state, "parsed_json": updated_params, "db_rows": db_rows}

        # ── list_top_orders — accounts ranked by order count, direct SQL
        if parsed_json.get("mode") == "list_top_orders":
            logger.info("db_node: list_top_orders — running direct SQL")
            _limit = int(parsed_json.get("pageSize") or 20)
            _top_orders_sql = f"""
SELECT json_build_object(
    'accounts', COALESCE(json_agg(sub ORDER BY sub.order_count DESC), '[]'::json),
    'metadata', json_build_object(
        'page', 1, 'total_pages', 1,
        'total_records', (SELECT COUNT(DISTINCT account_id) FROM orders)
    )
) AS result
FROM (
    SELECT
        a.account_id::text,
        a.account_name,
        a.type,
        a.industry,
        a.status,
        a.email,
        a.phone,
        adr.city,
        adr.province,
        adr.country,
        COUNT(o.order_id)::int                                              AS order_count,
        (SELECT COUNT(*) FROM contacts c   WHERE c.account_id  = a.account_id) AS contact_count,
        (SELECT COUNT(*) FROM opportunities op WHERE op.account_id = a.account_id) AS opportunity_count,
        COALESCE(SUM(o.total_amount), 0)                                    AS total_revenue,
        a.created_at,
        a.updated_at
    FROM accounts a
    JOIN orders o ON o.account_id = a.account_id
    LEFT JOIN LATERAL (
        SELECT city, province, country FROM addresses
        WHERE parent_id = a.account_id AND parent_type = 'account' AND label = 'billing'
        ORDER BY is_default DESC NULLS LAST, created_at LIMIT 1
    ) adr ON true
    WHERE (a.is_deleted IS NULL OR a.is_deleted = false)
    GROUP BY a.account_id, a.account_name, a.type, a.industry, a.status,
             a.email, a.phone, adr.city, adr.province, adr.country,
             a.created_at, a.updated_at
    ORDER BY order_count DESC
    LIMIT {_limit}
) sub
"""
            db_rows = execute_sp(_top_orders_sql)
            updated_params = {**parsed_json, "mode": "list", "_listMode": "list_top_orders"}
            return {**state, "parsed_json": updated_params, "db_rows": db_rows}

        # ── list_top_revenue — accounts ranked by total revenue, direct SQL
        if parsed_json.get("mode") == "list_top_revenue":
            logger.info("db_node: list_top_revenue — running direct SQL")
            _limit = int(parsed_json.get("pageSize") or 20)
            _top_revenue_sql = f"""
SELECT json_build_object(
    'accounts', COALESCE(json_agg(sub ORDER BY sub.total_revenue DESC), '[]'::json),
    'metadata', json_build_object(
        'page', 1, 'total_pages', 1,
        'total_records', (SELECT COUNT(DISTINCT account_id) FROM orders)
    )
) AS result
FROM (
    SELECT
        a.account_id::text,
        a.account_name,
        a.type,
        a.industry,
        a.status,
        a.email,
        a.phone,
        adr.city,
        adr.province,
        adr.country,
        COUNT(o.order_id)::int                                                   AS order_count,
        (SELECT COUNT(*) FROM contacts c   WHERE c.account_id  = a.account_id)  AS contact_count,
        (SELECT COUNT(*) FROM opportunities op WHERE op.account_id = a.account_id) AS opportunity_count,
        COALESCE(SUM(o.total_amount), 0)                                         AS total_revenue,
        a.created_at,
        a.updated_at
    FROM accounts a
    JOIN orders o ON o.account_id = a.account_id
    LEFT JOIN LATERAL (
        SELECT city, province, country FROM addresses
        WHERE parent_id = a.account_id AND parent_type = 'account' AND label = 'billing'
        ORDER BY is_default DESC NULLS LAST, created_at LIMIT 1
    ) adr ON true
    WHERE (a.is_deleted IS NULL OR a.is_deleted = false)
    GROUP BY a.account_id, a.account_name, a.type, a.industry, a.status,
             a.email, a.phone, adr.city, adr.province, adr.country,
             a.created_at, a.updated_at
    ORDER BY total_revenue DESC
    LIMIT {_limit}
) sub
"""
            db_rows = execute_sp(_top_revenue_sql)
            updated_params = {**parsed_json, "mode": "list", "_listMode": "list_top_revenue"}
            return {**state, "parsed_json": updated_params, "db_rows": db_rows}

        # ── list_no_phone — accounts with no phone number, direct SQL
        if parsed_json.get("mode") == "list_no_phone":
            logger.info("db_node: list_no_phone — running direct SQL")
            _no_phone_sql = """
SELECT json_build_object(
    'accounts', COALESCE(json_agg(
        json_build_object(
            'account_id',        a.account_id::text,
            'account_name',      a.account_name,
            'type',              a.type,
            'industry',          a.industry,
            'status',            a.status,
            'email',             a.email,
            'phone',             a.phone,
            'city',              adr.city,
            'province',          adr.province,
            'country',           adr.country,
            'order_count',       (SELECT COUNT(*) FROM orders o WHERE o.account_id = a.account_id),
            'contact_count',     (SELECT COUNT(*) FROM contacts c WHERE c.account_id = a.account_id),
            'opportunity_count', (SELECT COUNT(*) FROM opportunities op WHERE op.account_id = a.account_id),
            'total_revenue',     COALESCE((SELECT SUM(o2.total_amount) FROM orders o2 WHERE o2.account_id = a.account_id), 0),
            'created_at',        a.created_at,
            'updated_at',        a.updated_at
        ) ORDER BY a.account_name
    ), '[]'::json),
    'metadata', json_build_object(
        'page', 1, 'total_pages', 1,
        'total_records', (
            SELECT COUNT(*) FROM accounts a2
            WHERE (a2.phone IS NULL OR TRIM(a2.phone) = '')
              AND (a2.is_deleted IS NULL OR a2.is_deleted = false)
        )
    )
) AS result
FROM accounts a
LEFT JOIN LATERAL (
    SELECT city, province, country FROM addresses
    WHERE parent_id = a.account_id AND parent_type = 'account' AND label = 'billing'
    ORDER BY is_default DESC NULLS LAST, created_at LIMIT 1
) adr ON true
WHERE (a.phone IS NULL OR TRIM(a.phone) = '')
  AND (a.is_deleted IS NULL OR a.is_deleted = false)
"""
            db_rows = execute_sp(_no_phone_sql)
            updated_params = {**parsed_json, "mode": "list", "_listMode": "list_no_phone"}
            return {**state, "parsed_json": updated_params, "db_rows": db_rows}

        # ── list_overdue_invoices — accounts with invoices past due_date  ────────
        if parsed_json.get("mode") == "list_overdue_invoices":
            logger.info("db_node: list_overdue_invoices — running direct SQL")
            _overdue_sql = """
SELECT json_build_object(
    'accounts', COALESCE(json_agg(
        json_build_object(
            'account_id',          a.account_id::text,
            'account_name',        a.account_name,
            'type',                a.type,
            'industry',            a.industry,
            'status',              a.status,
            'email',               a.email,
            'phone',               a.phone,
            'city',                adr.city,
            'province',            adr.province,
            'country',             adr.country,
            'order_count',         (SELECT COUNT(*) FROM orders o WHERE o.account_id = a.account_id),
            'contact_count',       (SELECT COUNT(*) FROM contacts c WHERE c.account_id = a.account_id),
            'opportunity_count',   (SELECT COUNT(*) FROM opportunities op WHERE op.account_id = a.account_id),
            'total_revenue',       COALESCE((SELECT SUM(o2.total_amount) FROM orders o2 WHERE o2.account_id = a.account_id), 0),
            'overdue_invoices',    inv.overdue_count,
            'overdue_balance',     inv.overdue_balance,
            'created_at',          a.created_at,
            'updated_at',          a.updated_at
        ) ORDER BY inv.overdue_balance DESC
    ), '[]'::json),
    'metadata', json_build_object(
        'page', 1, 'total_pages', 1,
        'total_records', (
            SELECT COUNT(DISTINCT account_id) FROM invoices
            WHERE due_date < NOW() AND status != 'paid'
        )
    )
) AS result
FROM accounts a
JOIN LATERAL (
    SELECT COUNT(*)::int                    AS overdue_count,
           COALESCE(SUM(balance_due), 0)   AS overdue_balance
    FROM   invoices i
    WHERE  i.account_id = a.account_id
      AND  i.due_date  < NOW()
      AND  i.status   != 'paid'
) inv ON inv.overdue_count > 0
LEFT JOIN LATERAL (
    SELECT city, province, country FROM addresses
    WHERE parent_id = a.account_id AND parent_type = 'account' AND label = 'billing'
    ORDER BY is_default DESC NULLS LAST, created_at LIMIT 1
) adr ON true
WHERE (a.is_deleted IS NULL OR a.is_deleted = false)
"""
            db_rows = execute_sp(_overdue_sql)
            updated_params = {**parsed_json, "mode": "list", "_listMode": "list_overdue_invoices"}
            return {**state, "parsed_json": updated_params, "db_rows": db_rows}

        # ── list_min_orders — accounts with at least N orders, paginated  ──────
        if parsed_json.get("mode") == "list_min_orders":
            logger.info("db_node: list_min_orders — running direct SQL")
            try:
                _min = max(0, int(parsed_json.get('minOrders') or 1))
            except (ValueError, TypeError):
                _min = 1
            _page  = max(1, int(parsed_json.get('pageNumber') or 1))
            _limit = min(max(1, int(parsed_json.get('pageSize') or 20)), 200)
            _offset = (_page - 1) * _limit
            logger.info(f"list_min_orders: minOrders={parsed_json.get('minOrders')!r} → _min={_min}, page={_page}, limit={_limit}, offset={_offset}")
            _min_orders_sql = f"""
SELECT json_build_object(
    'accounts', COALESCE(json_agg(
        json_build_object(
            'account_id',        sub.account_id::text,
            'account_name',      sub.account_name,
            'type',              sub.type,
            'industry',          sub.industry,
            'status',            sub.status,
            'email',             sub.email,
            'phone',             sub.phone,
            'city',              adr.city,
            'province',          adr.province,
            'country',           adr.country,
            'order_count',       sub.order_count,
            'contact_count',     sub.contact_count,
            'opportunity_count', sub.opportunity_count,
            'total_revenue',     sub.total_revenue,
            'created_at',        sub.created_at,
            'updated_at',        sub.updated_at
        ) ORDER BY sub.order_count DESC
    ), '[]'::json),
    'metadata', json_build_object(
        'page', {_page},
        'total_pages', GREATEST(1, CEIL(
            (SELECT COUNT(*)::float FROM (
                SELECT a2.account_id
                FROM accounts a2
                LEFT JOIN orders o2 ON o2.account_id = a2.account_id AND o2.deleted_at IS NULL
                WHERE (a2.is_deleted IS NULL OR a2.is_deleted = false)
                GROUP BY a2.account_id
                HAVING COUNT(o2.order_id) > {_min}
            ) _tc) / {_limit}
        )::int),
        'total_records', (
            SELECT COUNT(*) FROM (
                SELECT a2.account_id
                FROM accounts a2
                LEFT JOIN orders o2 ON o2.account_id = a2.account_id AND o2.deleted_at IS NULL
                WHERE (a2.is_deleted IS NULL OR a2.is_deleted = false)
                GROUP BY a2.account_id
                HAVING COUNT(o2.order_id) > {_min}
            ) t
        )
    )
) AS result
FROM (
    SELECT
        a.account_id,
        a.account_name,
        a.type,
        a.industry,
        a.status,
        a.email,
        a.phone,
        COUNT(o.order_id)::int AS order_count,
        (SELECT COUNT(*) FROM contacts c  WHERE c.account_id  = a.account_id)      AS contact_count,
        (SELECT COUNT(*) FROM opportunities op WHERE op.account_id = a.account_id) AS opportunity_count,
        COALESCE(SUM(o.total_amount), 0) AS total_revenue,
        a.created_at,
        a.updated_at
    FROM accounts a
    LEFT JOIN orders o ON o.account_id = a.account_id AND o.deleted_at IS NULL
    WHERE (a.is_deleted IS NULL OR a.is_deleted = false)
    GROUP BY a.account_id, a.account_name, a.type, a.industry, a.status,
             a.email, a.phone, a.created_at, a.updated_at
    HAVING COUNT(o.order_id) > {_min}
    ORDER BY order_count DESC
    LIMIT {_limit} OFFSET {_offset}
) sub
LEFT JOIN LATERAL (
    SELECT city, province, country FROM addresses
    WHERE parent_id = sub.account_id AND parent_type = 'account' AND label = 'billing'
    ORDER BY is_default DESC NULLS LAST, created_at LIMIT 1
) adr ON true
"""
            db_rows = execute_sp(_min_orders_sql)
            updated_params = {**parsed_json, "mode": "list", "_listMode": "list_min_orders"}
            return {**state, "parsed_json": updated_params, "db_rows": db_rows}

        # ── Name → accountId resolution ──────────────────────────────────────
        # For get/timeline/financials/update: if only accountName is present,
        # resolve to UUID via direct SQL before calling sp_accounts.
        # Tries exact match first, then word-based ILIKE (e.g. "Apex Solutions"
        # matches "Apex Digital Solutions") — only accepts unambiguous results.
        _lookup_modes = {'get', 'timeline', 'financials', 'update', 'archive', 'restore',
                         'ai_summary'}
        if (parsed_json.get('mode') in _lookup_modes and
                parsed_json.get('accountName') and
                not parsed_json.get('accountId')):
            _aname = parsed_json['accountName']
            # "restore" targets archived accounts, so look among is_deleted=true
            # rows; every other mode operates on live (non-deleted) accounts.
            _del_clause = ("is_deleted = true" if parsed_json.get('mode') == 'restore'
                            else "(is_deleted IS NULL OR is_deleted = false)")
            try:
                _lookup_rows = execute_sp(
                    "SELECT account_id::text FROM accounts"
                    " WHERE LOWER(TRIM(account_name)) = LOWER(TRIM(%(name)s))"
                    f"   AND {_del_clause}"
                    " LIMIT 1",
                    params={'name': _aname},
                )
                if not _lookup_rows:
                    # Word-based fallback: all significant words must appear in name
                    _words = [w for w in re.split(r'\s+', _aname.lower()) if len(w) >= 3]
                    if _words:
                        _conds = " AND ".join(
                            f"LOWER(account_name) LIKE %(w{i})s" for i in range(len(_words))
                        )
                        _wparams = {f'w{i}': f'%{w}%' for i, w in enumerate(_words)}
                        _fallback = execute_sp(
                            f"SELECT account_id::text FROM accounts WHERE ({_conds})"
                            f"   AND {_del_clause} LIMIT 2",
                            params=_wparams,
                        )
                        if len(_fallback) == 1:
                            _lookup_rows = _fallback
                if _lookup_rows and _lookup_rows[0].get('account_id'):
                    _aid = str(_lookup_rows[0]['account_id'])
                    logger.info(f"Name resolved: '{_aname}' → {_aid}")
                    parsed_json = {**parsed_json, 'accountId': _aid}
                    # For update/archive/restore: remove accountName — the SP
                    # only takes accountId (and update would otherwise rename it)
                    if parsed_json.get('mode') in ('update', 'archive', 'restore'):
                        parsed_json = {k: v for k, v in parsed_json.items() if k != 'accountName'}
            except Exception as _ne:
                logger.warning(f"Name resolution failed for '{_aname}': {_ne}")

        # ── ai_summary — decision-grade AI synthesis of the account 360 ──────
        # Runs AFTER name resolution so accountName has become accountId.
        if parsed_json.get("mode") == "ai_summary":
            aid = parsed_json.get("accountId")
            if not aid:
                _missing = parsed_json.get("accountName") or "that account"
                return {**state, "db_rows": [{"result": {
                    "metadata": {"status": "error", "code": -404,
                                 "message": f"Could not find an account matching '{_missing}'"}}}]}
            from app.agents.accounts.ai_summary import build_account_ai_summary
            text = build_account_ai_summary(str(aid))
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": "ai_summary"},
                "ai_markdown": text,
            }}]}

        query, _ = build_accounts_query(parsed_json)
        logger.info(f"Built sp_accounts query for mode: {parsed_json.get('mode')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_accounts returned {len(db_rows)} rows")
        return {**state, "parsed_json": parsed_json, "db_rows": db_rows}

    except Exception as e:
        logger.error(f"Accounts database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output and persists turn to memory."""
    logger.info("=== Accounts Formatter Node ===")
    try:
        if state.get("should_call_api"):
            db_rows     = state.get("db_rows") or []
            parsed_json = state.get("parsed_json") or {}
            final_output = format_response(db_rows, parsed_json)
        else:
            final_output = state.get("ai_output") or "No response generated"

        session_id = state.get("session_id", "default-session")
        user_input = state.get("user_input", "")
        if user_input and final_output:
            save_turn(session_id, user_input, final_output)

        return {**state, "final_output": final_output}

    except Exception as e:
        logger.error(f"Accounts formatter error: {e}", exc_info=True)
        return {**state, "final_output": f"Error formatting response: {str(e)}"}


# ============================================================================
# GRAPH SINGLETON
# ============================================================================

_graph_app = None


def get_graph():
    global _graph_app
    if _graph_app is None:
        _graph_app = build_standard_graph(
            pre_router_node=pre_router_node,
            ai_agent_node=ai_agent_node,
            parse_output_node=parse_output_node,
            db_node=db_node,
            formatter_node=formatter_node,
            graph_label="Accounts Agent",
        )
    return _graph_app
