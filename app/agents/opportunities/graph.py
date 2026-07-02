"""LangGraph agent module for the Opportunities domain.

State notes vs base AgentState:
  + body          — full request body (HTML forms POST mode + params directly here)
  + format_result — formatter returns a dict with side-channel fields

route_request(body, chat_input, session_id) — 3-arg.
format_response(db_rows, params)            — returns Dict[str, Any] with:
    output, mode, report_mode, success,
    search_results, search_query, search_mode, product_lines, owners
build_opportunities_query(params)           — 1-arg, standard.

CASE 1 routing: HTML forms send { mode, opportunity_id, account_id, ... } directly
in the request body (not inside chatInput). The pre-router reads body.mode to detect
this path and routes straight to the SQL builder.
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

from .prompt import OPPORTUNITY_AGENT_SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_opportunities_query
from .formatter import format_response

logger = logging.getLogger(__name__)


# ============================================================================
# EXTENDED STATE
# ============================================================================

class OpportunityAgentState(TypedDict):
    session_id:       str
    body:             dict           # full request body for CASE 1 direct-mode routing
    chat_input:       dict
    user_input:       str
    router_action:    bool
    ai_output:        Optional[str]
    parsed_json:      Optional[Dict[str, Any]]
    should_call_api:  bool
    db_rows:          Optional[List]
    format_result:    Optional[Dict[str, Any]]   # dict from formatter
    final_output:     Optional[str]


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-Router — supports both direct body-mode (CASE 1) and NL chat (CASE 2/3)."""
    logger.info("=== Opportunities Pre-Router Node ===")
    body       = state.get("body", {})
    chat_input = state.get("chat_input", {})
    session_id = state.get("session_id", "")
    logger.info(f"body.mode={body.get('mode')!r}  message={chat_input.get('message', '')[:80]!r}")

    result = route_request(body, chat_input, session_id)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Opportunities pre-router ROUTED → mode={params.get('mode')}")
        return {
            **state,
            "router_action":   True,
            "parsed_json":     params,
            "should_call_api": True,
        }

    logger.info("Opportunities pre-router PASSTHRU → AI Agent")
    return {**state, "router_action": False}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the opportunities system prompt."""
    logger.info("=== Opportunities AI Agent Node ===")
    session_id = state.get("session_id", "default-session")
    user_input = state.get("user_input", "")

    settings = get_settings()
    history  = get_history(session_id)
    messages = history + [{"role": "user", "content": user_input}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": OPPORTUNITY_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(OPPORTUNITY_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Opportunities AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Opportunities AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Opportunities Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")

    if parsed and parsed.get("mode") == "list" and not parsed.get("search"):
        user_input = state.get("user_input") or ""
        name_m = re.search(
            r'(?:find|search|show|get|look\s*up|list)\s+'
            r'(?:opportunities?\s+)?(?:named?\s+|called\s+|for\s+)?'
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
    """Database node — builds and executes sp_opportunities()."""
    logger.info("=== Opportunities Database Node ===")
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
        # marker that the frontend dispatcher uses to open an inline form.
        _ui_only_modes = {
            'show_opportunity_form',
            'show_opportunity_update_form',
            'show_opportunity_add_product_form',
            'show_opportunity_update_product_form',
        }
        if parsed_json.get("mode") in _ui_only_modes:
            logger.info(f"db_node: UI-only mode '{parsed_json.get('mode')}' — skipping DB call")
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": parsed_json.get("mode")}
            }}]}

        # ── ai_summary — decision-grade AI synthesis of the deal ─────────────
        if parsed_json.get("mode") == "ai_summary":
            from app.agents.opportunities.ai_summary import (
                build_opportunity_ai_summary, resolve_opportunity)
            oid = parsed_json.get("opportunityId") or parsed_json.get("opportunity_id")
            if not oid:
                oid, _err = resolve_opportunity(
                    parsed_json.get("opportunityName") or parsed_json.get("name") or "")
                if not oid:
                    return {**state, "db_rows": [{"result": {
                        "metadata": {"status": "error", "code": -404,
                                     "message": f"Could not summarize: {_err}"}}}]}
            text = build_opportunity_ai_summary(str(oid))
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": "ai_summary"},
                "ai_markdown": text,
            }}]}

        query, _ = build_opportunities_query(parsed_json)
        logger.info(f"Built sp_opportunities query for mode: {parsed_json.get('mode')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_opportunities returned {len(db_rows)} rows")
        return {**state, "db_rows": db_rows}

    except Exception as e:
        logger.error(f"Opportunities database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output (dict) and persists turn to memory."""
    logger.info("=== Opportunities Formatter Node ===")
    try:
        if state.get("should_call_api"):
            db_rows     = state.get("db_rows") or []
            parsed_json = state.get("parsed_json") or {}
            fmt_result  = format_response(db_rows, parsed_json)
            final_output = fmt_result.get("output", "")
            logger.info(f"Opportunities formatted — mode={fmt_result.get('mode')} len={len(final_output)}")
        else:
            ai_output  = state.get("ai_output") or "No response generated"
            fmt_result = {
                "output":      ai_output,
                "mode":        "conversational",
                "report_mode": "conversational",
                "success":     True,
            }
            final_output = ai_output

        session_id = state.get("session_id", "default-session")
        user_input = state.get("user_input", "")
        if user_input and final_output:
            save_turn(session_id, user_input, final_output)

        return {**state, "format_result": fmt_result, "final_output": final_output}

    except Exception as e:
        logger.error(f"Opportunities formatter error: {e}", exc_info=True)
        err = {"output": f"Error: {str(e)}", "mode": "error",
               "report_mode": "error", "success": False}
        return {**state, "format_result": err, "final_output": err["output"]}


# ============================================================================
# GRAPH SINGLETON
# ============================================================================

_graph_app = None


def get_graph():
    global _graph_app
    if _graph_app is None:
        _graph_app = build_graph_with_schema(
            state_schema=OpportunityAgentState,
            pre_router_node=pre_router_node,
            ai_agent_node=ai_agent_node,
            parse_output_node=parse_output_node,
            db_node=db_node,
            formatter_node=formatter_node,
            graph_label="Opportunities Agent",
        )
    return _graph_app
