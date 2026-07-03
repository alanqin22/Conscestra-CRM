"""LangGraph agent module for the Notifications domain.

Same extended-state pattern as Activities, Leads, and Analytics:
  body + current_message + format_result in state.
  route_request(body, chat_input, session_id) — 3-arg.
  format_response(db_rows, params) — returns Dict[str, Any].
  build_notifications_query(params) — 1-arg, standard.

The notifications frontend drives all interactivity through embedded marker
tokens in the output string ([TOGGLE:...], [INSPECT:...], [ACTIONBAR], etc.)
rather than structured side-channel arrays, so the response model is lean.
"""

from __future__ import annotations

import json
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

from .prompt import NOTIFICATION_AGENT_SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_notifications_query
from .formatter import format_response
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)


# ============================================================================
# EXTENDED STATE
# ============================================================================

class NotificationAgentState(TypedDict):
    session_id:       str
    body:             dict
    chat_input:       dict
    user_input:       str
    current_message:  str
    router_action:    bool
    ai_output:        Optional[str]
    parsed_json:      Optional[Dict[str, Any]]
    should_call_api:  bool
    db_rows:          Optional[List]
    format_result:    Optional[Dict[str, Any]]
    final_output:     Optional[str]


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-Router — prefix-based routing (list notifications:, mark read:, etc.)."""
    logger.info("=== Notifications Pre-Router Node ===")
    body       = state.get("body", {})
    chat_input = state.get("chat_input", {})
    session_id = state.get("session_id", "")
    logger.info(f"Message: {chat_input.get('message', '')[:120]}")

    result = route_request(body, chat_input, session_id)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Notifications pre-router ROUTED → mode={params.get('mode')}")
        return {
            **state,
            "router_action":   True,
            "parsed_json":     params,
            "should_call_api": True,
            "current_message": state.get("user_input", ""),
        }

    current_msg = result.get("current_message", state.get("user_input", ""))
    logger.info(f"Notifications pre-router PASSTHRU → {current_msg[:80]}")
    return {**state, "router_action": False, "current_message": current_msg}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the notifications system prompt."""
    logger.info("=== Notifications AI Agent Node ===")
    session_id      = state.get("session_id", "default-session")
    current_message = state.get("current_message") or state.get("user_input", "")

    settings = get_settings()
    history  = get_history(session_id)
    messages = history + [{"role": "user", "content": current_message}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": NOTIFICATION_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(NOTIFICATION_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Notifications AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Notifications AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Notifications Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")
    if parsed:
        return {**state, "parsed_json": parsed, "should_call_api": True}
    return {**state, "parsed_json": None, "should_call_api": False}


def _resolve_employee_name(name: str):
    """Resolve an employee display name → employee_uuid.

    Exact (case-insensitive) full-name matches win; otherwise a unique
    ILIKE match is accepted. Returns (uuid, error_message)."""
    safe = name.replace("'", "''")
    rows = execute_sp(
        "SELECT employee_uuid, first_name || ' ' || COALESCE(last_name, '') AS full_name "
        f"FROM employees WHERE first_name || ' ' || COALESCE(last_name, '') ILIKE '%{safe}%' "
        "LIMIT 5"
    )
    matches = [(str(r.get('employee_uuid')), str(r.get('full_name') or '').strip())
               for r in rows if r.get('employee_uuid')]
    if not matches:
        return None, f"No employee found matching '{name}'."
    exact = [m for m in matches if m[1].lower() == name.lower()]
    pool = exact or matches
    if len({m[1].lower() for m in pool}) > 1:
        names = ', '.join(sorted({m[1] for m in pool}))
        return None, f"Multiple employees match '{name}': {names}. Please be more specific."
    return pool[0][0], None


def db_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Database node — builds and executes sp_notifications()."""
    logger.info("=== Notifications Database Node ===")
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

        # ── Resolve employeeName → employeeId (pre-router NL path) ──────────
        if parsed_json.get("employeeName"):
            _name = str(parsed_json.get("employeeName")).strip()
            parsed_json = {k: v for k, v in parsed_json.items() if k != "employeeName"}
            if not parsed_json.get("employeeId"):
                _uuid, _err = _resolve_employee_name(_name)
                if _err:
                    logger.warning(f"[employee-resolve] {_err}")
                    return {**state, "db_rows": [{"result": {
                        "metadata": {"status": "error", "code": -404, "message": _err}
                    }}]}
                logger.info(f"[employee-resolve] {_name!r} → {_uuid}")
                parsed_json = {**parsed_json, "employeeId": _uuid}
            state = {**state, "parsed_json": parsed_json}

        query, _ = build_notifications_query(parsed_json)
        logger.info(f"Built sp_notifications query for mode: {parsed_json.get('mode')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_notifications returned {len(db_rows)} rows")
        return {**state, "db_rows": db_rows}

    except WritePermissionError:
        raise
    except Exception as e:
        logger.error(f"Notifications database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output (dict) and persists turn to memory."""
    logger.info("=== Notifications Formatter Node ===")
    try:
        if state.get("should_call_api"):
            db_rows     = state.get("db_rows") or []
            parsed_json = state.get("parsed_json") or {}
            fmt_result  = format_response(db_rows, parsed_json)
            final_output = fmt_result.get("output", "")
            logger.info(f"Notifications formatted — mode={fmt_result.get('mode')} len={len(final_output)}")
        else:
            ai_output  = state.get("ai_output") or "No response generated"
            fmt_result = {
                "output":  ai_output,
                "mode":    "conversational",
                "success": True,
            }
            final_output = ai_output

        # Persist a COMPACT assistant turn. For DB operations save the structured
        # command (not the rendered list/table) so large outputs don't pollute the
        # LLM context and bleed module/filter values into the next query.
        session_id = state.get("session_id", "default-session")
        user_input = state.get("user_input", "")
        if user_input:
            if state.get("should_call_api"):
                memory_turn = json.dumps(state.get("parsed_json") or {}, separators=(",", ":"))
            else:
                memory_turn = final_output
            if memory_turn:
                save_turn(session_id, user_input, memory_turn)

        return {**state, "format_result": fmt_result, "final_output": final_output}

    except Exception as e:
        logger.error(f"Notifications formatter error: {e}", exc_info=True)
        err = {"output": f"Error: {str(e)}", "mode": "error", "success": False}
        return {**state, "format_result": err, "final_output": err["output"]}


# ============================================================================
# GRAPH SINGLETON
# ============================================================================

_graph_app = None


def get_graph():
    global _graph_app
    if _graph_app is None:
        _graph_app = build_graph_with_schema(
            state_schema=NotificationAgentState,
            pre_router_node=pre_router_node,
            ai_agent_node=ai_agent_node,
            parse_output_node=parse_output_node,
            db_node=db_node,
            formatter_node=formatter_node,
            graph_label="Notifications Agent",
        )
    return _graph_app
