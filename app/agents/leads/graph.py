"""LangGraph agent module for the Leads domain.

State matches Activities (body + current_message + format_result).
route_request(body, chat_input, session_id) — 3-arg.
format_response(db_rows, params) — returns Dict[str, Any] with rich
side-channel fields: leads, lead, pipeline, employees, account, contact,
opportunity, address, accountId, contactId, opportunityId.
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

from .prompt import LEAD_AGENT_SYSTEM_PROMPT
from .pre_router import route_request, UUID_RE
from .sql_builder import build_leads_query
from .formatter import format_response

logger = logging.getLogger(__name__)


# ============================================================================
# EXTENDED STATE  (matches Activities pattern)
# ============================================================================

class LeadAgentState(TypedDict):
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
    """Pre-Router — prefix-based routing (matches Activities pattern)."""
    logger.info("=== Leads Pre-Router Node ===")
    body       = state.get("body", {})
    chat_input = state.get("chat_input", {})
    session_id = state.get("session_id", "")
    logger.info(f"Message: {chat_input.get('message', '')[:120]}")

    result = route_request(body, chat_input, session_id)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Leads pre-router ROUTED → mode={params.get('mode')}")
        return {
            **state,
            "router_action":   True,
            "parsed_json":     params,
            "should_call_api": True,
            "current_message": state.get("user_input", ""),
        }

    current_msg = result.get("current_message", state.get("user_input", ""))
    logger.info(f"Leads pre-router PASSTHRU → {current_msg[:80]}")
    return {**state, "router_action": False, "current_message": current_msg}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the leads system prompt."""
    logger.info("=== Leads AI Agent Node ===")
    session_id      = state.get("session_id", "default-session")
    current_message = state.get("current_message") or state.get("user_input", "")

    settings = get_settings()
    history  = get_history(session_id)
    messages = history + [{"role": "user", "content": current_message}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": LEAD_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(LEAD_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Leads AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Leads AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Leads Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")

    if parsed and parsed.get("mode") == "list" and not parsed.get("search"):
        user_input = state.get("user_input") or ""
        name_m = re.search(
            r'(?:find|search|show|get|look\s*up|list)\s+'
            r'(?:leads?\s+)?(?:named?\s+|called\s+|for\s+)?'
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
    """Database node — builds and executes sp_leads()."""
    logger.info("=== Leads Database Node ===")
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
        _ui_only_modes = {'show_lead_form', 'show_lead_update_form'}
        if parsed_json.get("mode") in _ui_only_modes:
            logger.info(f"db_node: UI-only mode '{parsed_json.get('mode')}' — skipping DB call")
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": parsed_json.get("mode")}
            }}]}

        # ── Owner name → UUID resolution (ownerSearch → ownerId) ─────────────
        # Pre-router sets ownerSearch when user says "assigned to <name>".
        # Also catch a non-UUID ownerId (e.g. the LLM router put a plain name
        # into ownerId for "Owner: Noah Williams") — treat it as a name to
        # resolve rather than passing it to SQL as a uuid (which errors -500).
        _owner_search = parsed_json.pop('ownerSearch', None)
        _owner_id_raw = parsed_json.get('ownerId')
        if not _owner_search and _owner_id_raw and not UUID_RE.search(str(_owner_id_raw)):
            _owner_search = str(_owner_id_raw).strip()
            parsed_json.pop('ownerId', None)
        if _owner_search:
            logger.info(f"db_node: resolving ownerSearch={_owner_search!r}")
            _owner_rows = execute_sp(
                "SELECT owner_id::text, first_name, last_name FROM owners "
                "WHERE is_active = TRUE "
                "  AND (LOWER(first_name) = %(name)s "
                "       OR LOWER(first_name || ' ' || last_name) = %(full)s "
                "       OR LOWER(first_name) LIKE %(partial)s) "
                "ORDER BY first_name, last_name LIMIT 5",
                params={
                    'name':    _owner_search.lower(),
                    'full':    _owner_search.lower(),
                    'partial': f'{_owner_search.lower()}%',
                },
            )
            if _owner_rows:
                _exact = [r for r in _owner_rows
                          if r['first_name'].lower() == _owner_search.lower()]
                _resolved = (_exact or _owner_rows)[0]
                parsed_json['ownerId'] = _resolved['owner_id']
                logger.info(f"db_node: owner resolved → {_resolved['first_name']} {_resolved['last_name']} ({_resolved['owner_id']})")
            else:
                logger.warning(f"db_node: ownerSearch {_owner_search!r} — no owner found")
                return {**state, "parsed_json": parsed_json, "db_rows": [{"result": {
                    "metadata": {"status": "success", "code": 0,
                                 "total_records": 0, "total_pages": 1, "page": 1},
                    "leads": [],
                    "message": f"No owner named '{_owner_search}' found.",
                }}]}

        query, _ = build_leads_query(parsed_json)
        logger.info(f"Built sp_leads query for mode: {parsed_json.get('mode')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_leads returned {len(db_rows)} rows")

        # ── auto-resolve: "show details for [name]" → get mode when 1 result ─
        if parsed_json.get('detailsRequested') and parsed_json.get('mode') == 'list':
            sp_result = db_rows[0] if db_rows else {}
            inner     = sp_result.get('result', {}) if isinstance(sp_result, dict) else {}
            leads     = inner.get('leads') or []
            if len(leads) == 1:
                lead_id = str(leads[0].get('lead_id') or '')
                if lead_id:
                    get_params = {'mode': 'get', 'leadId': lead_id}
                    get_query, _ = build_leads_query(get_params)
                    db_rows     = execute_sp(get_query)
                    parsed_json = get_params
                    logger.info(f"details auto-resolved → get leadId={lead_id}")
            state = {**state, "parsed_json": parsed_json}

        return {**state, "db_rows": db_rows, "parsed_json": parsed_json}

    except Exception as e:
        logger.error(f"Leads database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output (dict) and persists turn to memory."""
    logger.info("=== Leads Formatter Node ===")
    try:
        if state.get("should_call_api"):
            db_rows     = state.get("db_rows") or []
            parsed_json = state.get("parsed_json") or {}
            fmt_result  = format_response(db_rows, parsed_json)
            final_output = fmt_result.get("output", "")
            logger.info(f"Leads formatted — mode={fmt_result.get('mode')} len={len(final_output)}")
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
        logger.error(f"Leads formatter error: {e}", exc_info=True)
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
            state_schema=LeadAgentState,
            pre_router_node=pre_router_node,
            ai_agent_node=ai_agent_node,
            parse_output_node=parse_output_node,
            db_node=db_node,
            formatter_node=formatter_node,
            graph_label="Leads Agent",
        )
    return _graph_app
