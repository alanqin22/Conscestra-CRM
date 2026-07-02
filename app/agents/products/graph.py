"""LangGraph agent module for the Products domain.

Standard 5-node topology — identical to Accounts and Contacts.
Domain-specific concerns (prompt, pre_router, sql_builder, formatter)
stay in this package; shared utilities come from app.core.
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

from .prompt import PRODUCT_AGENT_SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_products_query
from .formatter import format_response

logger = logging.getLogger(__name__)


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-Router — mirrors n8n Pre Router Code node v1.0."""
    logger.info("=== Products Pre-Router Node ===")
    user_input = state.get("user_input", "")
    chat_input = state.get("chat_input", {})
    logger.info(f"Message: {user_input[:120]}")

    result = route_request(user_input, chat_input)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Products pre-router ROUTED → mode={params.get('mode')}")
        return {**state, "router_action": True, "parsed_json": params, "should_call_api": True}

    logger.info("Products pre-router PASSTHRU → AI Agent")
    return {**state, "router_action": False}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the products system prompt."""
    logger.info("=== Products AI Agent Node ===")
    session_id = state.get("session_id", "default-session")
    user_input = state.get("user_input", "")

    settings = get_settings()
    history  = get_history(session_id)
    messages = history + [{"role": "user", "content": user_input}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": PRODUCT_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(PRODUCT_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Products AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Products AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Products Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")

    if parsed and parsed.get("mode") == "list" and not parsed.get("search"):
        user_input = state.get("user_input") or ""
        name_m = re.search(
            r'(?:find|search|show|get|look\s*up|list)\s+'
            r'(?:products?\s+)?(?:named?\s+|called\s+|for\s+)?'
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
    """Database node — builds and executes sp_products()."""
    logger.info("=== Products Database Node ===")
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

        # ── UI-only marker modes — no DB call, formatter renders the marker
        # text that the HTML response router uses to open an inline form.
        _ui_only_modes = {
            'show_bulk_stock_form',
            'show_price_history_form',
            'show_low_stock_form',
            'show_product_form',
            'ask_product_identifier',
        }
        if parsed_json.get("mode") in _ui_only_modes:
            logger.info(f"db_node: UI-only mode '{parsed_json.get('mode')}' — skipping DB call")
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": parsed_json.get("mode")}
            }}]}

        query, _ = build_products_query(parsed_json)
        logger.info(f"Built sp_products query for mode: {parsed_json.get('mode')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_products returned {len(db_rows)} rows")

        # Auto-resolve: "pricing history for <name>" → list search with 1 result
        # → automatically fetch price_history for that product.
        if parsed_json.get('priceHistoryRequested') and parsed_json.get('mode') == 'list':
            sp_result = db_rows[0] if db_rows else {}
            # Products SQL builder emits SELECT sp_products(...) without AS result,
            # so the row key is 'sp_products'; leads uses AS result → 'result'.
            inner = {}
            for _key in ('sp_products', 'result'):
                _val = sp_result.get(_key) if isinstance(sp_result, dict) else None
                if _val and isinstance(_val, dict):
                    inner = _val
                    break
            products = inner.get('products') or []
            if len(products) == 1:
                product_id = str(products[0].get('product_id') or '')
                if product_id:
                    ph_params = {'mode': 'price_history', 'productId': product_id}
                    ph_query, _ = build_products_query(ph_params)
                    db_rows = execute_sp(ph_query)
                    parsed_json = ph_params
                    logger.info(f"priceHistoryRequested auto-resolved → price_history productId={product_id}")
            else:
                # 0 or 2+ matches — can't auto-resolve; open price history search
                # form so the user can pick the exact product via typeahead.
                parsed_json = {'mode': 'show_price_history_form'}
                db_rows = [{"result": {"metadata": {"status": "success", "code": 0,
                                                    "mode": "show_price_history_form"}}}]
                logger.info(f"priceHistoryRequested: {len(products)} match(es) → show_price_history_form")
            state = {**state, 'parsed_json': parsed_json}

        # Auto-resolve: "show details for <name>" → list search with exactly
        # 1 result → automatically fetch get_details for that product.
        # 0 or 2+ matches simply render the (possibly empty) list so the user
        # can pick via each row's View Details button.
        if parsed_json.get('detailsRequested') and parsed_json.get('mode') == 'list':
            sp_result = db_rows[0] if db_rows else {}
            inner = {}
            for _key in ('sp_products', 'result'):
                _val = sp_result.get(_key) if isinstance(sp_result, dict) else None
                if _val and isinstance(_val, dict):
                    inner = _val
                    break
            products = inner.get('products') or []
            if len(products) == 1:
                product_id = str(products[0].get('product_id') or '')
                if product_id:
                    gd_params = {'mode': 'get_details', 'productId': product_id}
                    gd_query, _ = build_products_query(gd_params)
                    db_rows = execute_sp(gd_query)
                    parsed_json = gd_params
                    logger.info(f"detailsRequested auto-resolved → get_details productId={product_id}")
                    state = {**state, 'parsed_json': parsed_json}
            else:
                logger.info(f"detailsRequested: {len(products)} match(es) — rendering list")

        return {**state, "db_rows": db_rows, "parsed_json": parsed_json}

    except Exception as e:
        logger.error(f"Products database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output and persists turn to memory."""
    logger.info("=== Products Formatter Node ===")
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
        logger.error(f"Products formatter error: {e}", exc_info=True)
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
            graph_label="Products Agent",
        )
    return _graph_app
