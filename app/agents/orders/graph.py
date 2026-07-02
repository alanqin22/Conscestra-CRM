"""LangGraph agent module for the Orders domain.

Orders has a richer state than the base agents — it carries:
  body             : the full reconstructed request body (for batchData routing)
  current_message  : preprocessed message (may differ from user_input)
  raw_message      : original unmodified message (used by sql_builder enrichment)
  params           : alias for parsed_json set by pre_router direct path
  format_result    : structured dict returned by format_response (orders formatter
                     returns a dict, not a string, so it can pass side-channel data
                     like order_id, order_number, entity, reportMode back to the
                     HTTP layer)

The graph topology is identical to the standard 5-node DAG; we use
build_graph_with_schema() so the StateGraph is typed against OrderAgentState.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, TypedDict

from app.core.graph_utils import (
    _get_llm,
    _call_ollama_direct,
    parse_ai_json,
    build_graph_with_schema,
)
from app.core.memory import get_history, save_turn
from app.core.config import get_settings
from app.core.database import execute_sp

from .prompt import SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_orders_query
from .formatter import format_response

logger = logging.getLogger(__name__)


# ============================================================================
# EXTENDED STATE
# Orders carries extra fields beyond the standard AgentState.
# ============================================================================

class OrderAgentState(TypedDict):
    session_id:       str
    body:             dict           # full reconstructed request body
    chat_input:       dict
    user_input:       str
    current_message:  str            # preprocessed message (may equal user_input)
    raw_message:      str            # original unmodified message
    router_action:    bool
    params:           Optional[Dict[str, Any]]   # alias for parsed_json (direct path)
    ai_output:        Optional[str]
    parsed_json:      Optional[Dict[str, Any]]
    should_call_api:  bool
    db_rows:          Optional[List]
    format_result:    Optional[Dict[str, Any]]   # structured dict from formatter
    final_output:     Optional[str]


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-Router — mirrors n8n Pre Router Code node v4.1."""
    logger.info("=== Orders Pre-Router Node (v4.1) ===")
    body       = state.get("body", {})
    chat_input = state.get("chat_input", {})
    session_id = state.get("session_id", "")

    result = route_request(body, chat_input, session_id)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Orders pre-router ROUTED → mode={params.get('mode')} action={params.get('action', '')}")
        return {
            **state,
            "router_action":   True,
            "params":          params,
            "parsed_json":     params,
            "should_call_api": True,
        }

    logger.info("Orders pre-router PASSTHRU → AI Agent")
    return {**state, "router_action": False}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the orders system prompt."""
    logger.info("=== Orders AI Agent Node ===")
    session_id = state.get("session_id", "default-session")
    # Orders uses current_message (may be enriched) as the LLM input
    current_message = state.get("current_message") or state.get("user_input", "")
    logger.info(f"Session: {session_id!r}  Input: {current_message[:120]}")

    settings = get_settings()
    history  = get_history(session_id)
    messages = history + [{"role": "user", "content": current_message}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Orders AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Orders AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Orders Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")

    if parsed and parsed.get("mode") == "list" and not parsed.get("search"):
        user_input = state.get("user_input") or ""
        name_m = re.search(
            r'(?:find|search|show|get|look\s*up|list)\s+'
            r'(?:orders?\s+)?(?:named?\s+|called\s+|for\s+)?'
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
    """Database node — builds and executes sp_orders().

    Orders sql_builder takes an extra raw_message argument used to recover
    missing fields (productId, productPricingId, orderDate) from the original
    message text when the structured params are incomplete.
    """
    logger.info("=== Orders Database Node ===")
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
        _ui_only_modes = {'show_order_form', 'ask_order_identifier'}
        if parsed_json.get("mode") in _ui_only_modes:
            logger.info(f"db_node: UI-only mode '{parsed_json.get('mode')}' — skipping DB call")
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": parsed_json.get("mode")}
            }}]}

        raw_message = state.get("raw_message") or state.get("user_input", "")

        # ── account_summary by name: resolve accountSearch → accountId ──────
        # "Revenue summary for Bob Brown" — pre-router passes accountSearch;
        # look the name up via account_search, prefer an exact match, and run
        # the summary for that account. Falls back to the global summary when
        # the name can't be resolved unambiguously.
        if (parsed_json.get("mode") == "account_summary"
                and parsed_json.get("accountSearch")):
            _name = str(parsed_json.pop("accountSearch")).strip()
            try:
                _q, _ = build_orders_query({"mode": "account_search", "search": _name})
                _rows = execute_sp(_q)
                _res = (_rows[0].get("result") or {}) if _rows else {}
                _accts = _res.get("accounts") or _res.get("matches") or []
                _exact = [a for a in _accts
                          if str(a.get("account_name") or "").lower() == _name.lower()]
                _pick = (_exact[0] if _exact else
                         (_accts[0] if len(_accts) == 1 else None))
                if _pick and _pick.get("account_id"):
                    parsed_json = {**parsed_json, "accountId": _pick["account_id"]}
                    logger.info(f"[account-summary-resolve] {_name!r} → {_pick['account_id']}")
                else:
                    logger.info(f"[account-summary-resolve] {_name!r}: {len(_accts)} matches — global summary")
            except Exception as _re:
                logger.warning(f"[account-summary-resolve] lookup failed: {_re}")
            state = {**state, "parsed_json": parsed_json}

        # ── list by name: resolve search → accountId for account-name matches ──
        # "Show orders for Carlos Martinez" / "Show orders for Bob Brown" — the
        # pre-router passes a broad `search` term. sp_orders' list-mode p_search
        # ALSO matches contact first/last/full name across ALL accounts, so a
        # search like "Carlos Martinez" (an account name AND a contact on a
        # *different* account's order) leaks that unrelated order into the
        # results. When the term resolves to a single account (preferring an
        # exact name match), filter by that accountId instead so results are
        # scoped to that customer's own orders only.
        if (parsed_json.get("mode") == "list"
                and parsed_json.get("search")
                and not parsed_json.get("accountId")):
            _name = str(parsed_json["search"]).strip()
            try:
                _q, _ = build_orders_query({"mode": "account_search", "search": _name})
                _rows = execute_sp(_q)
                _res = (_rows[0].get("result") or {}) if _rows else {}
                _accts = _res.get("accounts") or _res.get("matches") or []
                _exact = [a for a in _accts
                          if str(a.get("account_name") or "").lower() == _name.lower()]
                _pick = (_exact[0] if _exact else
                         (_accts[0] if len(_accts) == 1 else None))
                if _pick and _pick.get("account_id"):
                    parsed_json = {**parsed_json, "accountId": _pick["account_id"]}
                    parsed_json.pop("search", None)
                    logger.info(f"[list-search-resolve] {_name!r} → accountId {_pick['account_id']}")
            except Exception as _re:
                logger.warning(f"[list-search-resolve] lookup failed: {_re}")
            state = {**state, "parsed_json": parsed_json}

        query, _ = build_orders_query(dict(parsed_json), raw_message)
        logger.info(f"Built sp_orders query — mode={parsed_json.get('mode')} action={parsed_json.get('action', '')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_orders returned {len(db_rows)} rows")

        # ── Python-side status safety filter ─────────────────────────────────
        # The SP applies status + search filters in SQL, but as a safety net we
        # also enforce them here so combined queries always return correct results
        # even if the deployed SP version doesn't AND both filters together.
        req_status = (parsed_json.get("status") or "").strip().lower()
        # 'active'/'open' are pseudo-statuses that expand to the fulfilment
        # queue (pending+processing+ready) — match the SP's expansion so the
        # safety net doesn't strip the very rows the SP correctly returned.
        _ACTIVE_GROUP = {'pending', 'processing', 'ready'}
        if req_status and db_rows:
            try:
                filtered = []
                for row in db_rows:
                    result = row.get("result") if isinstance(row, dict) else None
                    if isinstance(result, dict) and "orders" in result:
                        orders = result["orders"] or []
                        if req_status in ('active', 'open'):
                            orders_filtered = [
                                o for o in orders
                                if str(o.get("status") or "").lower() in _ACTIVE_GROUP
                            ]
                        else:
                            orders_filtered = [
                                o for o in orders
                                if str(o.get("status") or "").lower() == req_status
                            ]
                        # Only rewrite the payload when this safety net actually
                        # removed rows — the SP already filters by status, and
                        # overwriting total_records with the page row count on a
                        # no-op pass broke multi-page totals (e.g. "completed"
                        # showed "Total: 50" instead of 331).
                        if len(orders_filtered) < len(orders):
                            logger.info(
                                f"[status-filter] Filtered {len(orders)} → "
                                f"{len(orders_filtered)} orders for status={req_status!r}"
                            )
                            result = dict(result)
                            result["orders"] = orders_filtered
                            meta = dict(result.get("metadata") or {})
                            meta["total_records"] = len(orders_filtered)
                            _ps = int(meta.get("page_size") or 50)
                            meta["total_pages"] = max(1, (len(orders_filtered) + _ps - 1) // _ps)
                            result["metadata"] = meta
                            filtered.append({"result": result})
                        else:
                            filtered.append(row)
                    else:
                        filtered.append(row)
                db_rows = filtered
            except Exception as fe:
                logger.warning(f"[status-filter] Python filter skipped: {fe}")

        return {**state, "db_rows": db_rows}

    except ValueError as e:
        # Validation error from sql_builder — surface cleanly
        logger.error(f"Orders SQL validation error: {e}")
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -1, "message": str(e)}
        }}]}
    except Exception as e:
        logger.error(f"Orders database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": f"Database error: {str(e)}"}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output (returns dict) and persists turn to memory.

    Orders formatter returns a dict with keys:
        output, mode, reportMode, entity, success,
        and optionally: order_id, order_number, result, context
    """
    logger.info("=== Orders Formatter Node ===")
    try:
        if state.get("should_call_api"):
            db_rows     = state.get("db_rows") or []
            parsed_json = state.get("parsed_json") or {}
            fmt_result  = format_response(db_rows, parsed_json)
            final_output = fmt_result.get("output", "")
            logger.info(
                f"Orders formatted — mode={fmt_result.get('mode')} "
                f"entity={fmt_result.get('entity')} len={len(final_output)}"
            )
        else:
            ai_output = state.get("ai_output") or "No response generated"
            fmt_result = {
                "output":     ai_output,
                "mode":       "conversational",
                "reportMode": "conversational",
                "entity":     "orders",
                "success":    True,
            }
            final_output = ai_output
            logger.info("Orders returning conversational response")

        # Persist to session memory
        session_id = state.get("session_id", "default-session")
        user_input = state.get("user_input", "")
        if user_input and final_output:
            save_turn(session_id, user_input, final_output)

        return {**state, "format_result": fmt_result, "final_output": final_output}

    except Exception as e:
        logger.error(f"Orders formatter error: {e}", exc_info=True)
        err = {
            "output":     f"Error formatting response: {str(e)}",
            "mode":       "error",
            "reportMode": "error",
            "entity":     "orders",
            "success":    False,
        }
        return {**state, "format_result": err, "final_output": err["output"]}


# ============================================================================
# GRAPH SINGLETON
# ============================================================================

_graph_app = None


def get_graph():
    global _graph_app
    if _graph_app is None:
        _graph_app = build_graph_with_schema(
            state_schema=OrderAgentState,
            pre_router_node=pre_router_node,
            ai_agent_node=ai_agent_node,
            parse_output_node=parse_output_node,
            db_node=db_node,
            formatter_node=formatter_node,
            graph_label="Orders Agent",
        )
    return _graph_app
