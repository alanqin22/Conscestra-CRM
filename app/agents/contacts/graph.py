"""LangGraph agent module for the Contacts domain.

Mirrors the n8n Contacts API v3 workflow topology:
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

from .prompt import CONTACT_AGENT_SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_contacts_query
from .formatter import format_response

logger = logging.getLogger(__name__)


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-Router — mirrors n8n Pre Router Code node v3.1."""
    logger.info("=== Contacts Pre-Router Node (v3.1) ===")
    user_input = state.get("user_input", "")
    chat_input = state.get("chat_input", {})
    logger.info(f"Session: {state.get('session_id')!r}  Message: {user_input[:120]}")

    result = route_request(user_input, chat_input)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Contacts pre-router ROUTED → mode={params.get('mode')}")
        return {**state, "router_action": True, "parsed_json": params, "should_call_api": True}

    logger.info(f"Contacts pre-router PASSTHRU → {result['current_message'][:80]}")
    return {**state, "router_action": False, "user_input": result["current_message"]}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the contacts system prompt."""
    logger.info("=== Contacts AI Agent Node ===")
    session_id = state.get("session_id", "default-session")
    user_input = state.get("user_input", "")

    settings = get_settings()
    history  = get_history(session_id)
    messages = history + [{"role": "user", "content": user_input}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": CONTACT_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(CONTACT_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Contacts AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Contacts AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Contacts Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")

    # When AI emits only a bare [MODE:list] or {"search":...} with no search term,
    # recover the search term from the original user message.
    if parsed and parsed.get("mode") == "list" and not parsed.get("search"):
        user_input = state.get("user_input") or ""
        name_m = re.search(
            r'(?:find|search|show|get|look\s*up|list)\s+'
            r'(?:contacts?\s+)?(?:named?\s+|called\s+|for\s+)?'
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
    """Database node — builds and executes sp_contacts()."""
    logger.info("=== Contacts Database Node ===")
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
        _ui_only_modes = {'show_contact_form', 'show_contact_update_form'}
        if parsed_json.get("mode") in _ui_only_modes:
            logger.info(f"db_node: UI-only mode '{parsed_json.get('mode')}' — skipping DB call")
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": parsed_json.get("mode")}
            }}]}

        # ── ai_summary — decision-grade AI synthesis of the contact ──────────
        if parsed_json.get("mode") == "ai_summary":
            from app.agents.contacts.ai_summary import (
                build_contact_ai_summary, resolve_contact)
            cid = parsed_json.get("contactId")
            if not cid:
                cid, _err = resolve_contact(parsed_json.get("contactName") or "")
                if not cid:
                    return {**state, "db_rows": [{"result": {
                        "metadata": {"status": "error", "code": -404,
                                     "message": f"Could not summarize: {_err}"}}}]}
            text = build_contact_ai_summary(str(cid))
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": "ai_summary"},
                "ai_markdown": text,
            }}]}

        query, _ = build_contacts_query(parsed_json)
        logger.info(f"Built sp_contacts query for mode: {parsed_json.get('mode')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_contacts returned {len(db_rows)} rows")
        return {**state, "db_rows": db_rows}

    except Exception as e:
        logger.error(f"Contacts database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output and persists turn to memory."""
    logger.info("=== Contacts Formatter Node ===")
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
        logger.error(f"Contacts formatter error: {e}", exc_info=True)
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
            graph_label="Contacts Agent",
        )
    return _graph_app
