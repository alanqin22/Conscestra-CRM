"""Cases LangGraph — C1 Step 5. Standard 5-node topology.

    pre_router ─┬─[direct_db]──→ db ──→ format → END
                └─[ai_agent]──→ ai_agent → parse ─┬─[call_db]──→ db → format
                                                   └─[skip_db]──→ format

Uses build_graph_with_schema because the agent carries one extra state key
(`fallback_text`) beyond AgentState — the documented path for extra keys, the
same one the Orders agent takes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.core.graph_utils import (AgentState, _get_llm, build_graph_with_schema,
                                  parse_ai_json)
from app.core.memory import get_history, save_turn

from . import pre_router as _pre
from .formatter import format_response
from .prompt import SYSTEM_PROMPT
from .sql_builder import execute

logger = logging.getLogger(__name__)


class CasesState(AgentState):
    fallback_text: Optional[str]


def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    hit = _pre.route(state.get("chat_input") or {})
    if hit:
        action, params = hit
        logger.info(f"[cases] direct route -> {action}")
        return {**state, "router_action": True, "should_call_api": True,
                "parsed_json": {"action": action, "params": params}}
    return {**state, "router_action": False}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    session_id = state.get("session_id") or "cases"
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in (get_history(session_id) or [])[-6:]:
        msgs.append(turn)
    msgs.append({"role": "user", "content": state.get("user_input") or ""})
    try:
        out = _get_llm(tier="lite", caller="cases").invoke(msgs)
        text = getattr(out, "content", None) or str(out)
        if isinstance(text, list):          # some providers return blocks
            text = " ".join(str(b.get("text", b)) if isinstance(b, dict)
                            else str(b) for b in text)
    except Exception as exc:
        logger.error(f"[cases] LLM failed: {exc}")
        text = ""
    return {**state, "ai_output": text}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    parsed = parse_ai_json(state.get("ai_output") or "")
    action = (parsed or {}).get("action")
    if not parsed or not action or action == "none":
        reason = ((parsed or {}).get("params") or {}).get("reason") \
            or "I couldn't map that to a case action."
        return {**state, "parsed_json": None, "should_call_api": False,
                "fallback_text": reason}
    return {**state, "parsed_json": parsed, "should_call_api": True}


def db_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Executes the chosen ACTION.

    Named db_node for topology parity, but it never composes SQL for a write —
    those go through app/core/cases.py so the state machine, owner validation
    and field history cannot be bypassed by anything the model emitted."""
    parsed = state.get("parsed_json") or {}
    if not parsed:
        return {**state, "db_rows": {"ok": False, "action": "",
                                     "error": "nothing to run"}}
    params = dict(parsed.get("params") or {})
    params.setdefault("actor", (state.get("chat_input") or {}).get("agent")
                      or "cases-agent")
    return {**state, "db_rows": execute(parsed.get("action") or "", params)}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    if state.get("should_call_api"):
        output = format_response(state.get("db_rows") or {}).get("output", "")
    else:
        output = state.get("fallback_text") or "I couldn't help with that."
    try:
        save_turn(state.get("session_id") or "cases",
                  state.get("user_input") or "", output)
    except Exception as exc:
        logger.debug(f"[cases] memory save skipped: {exc}")
    return {**state, "final_output": output}


_graph_app = None


def get_graph():
    global _graph_app
    if _graph_app is None:
        _graph_app = build_graph_with_schema(
            CasesState, pre_router_node, ai_agent_node, parse_output_node,
            db_node, formatter_node, graph_label="Cases Agent")
    return _graph_app
