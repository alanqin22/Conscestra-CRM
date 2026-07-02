"""LangGraph agent module for the Accounting domain.

Uses the standard 5-node topology via build_standard_graph() — same as
Accounts and Contacts.

Key characteristics:
  - route_request(message, chat_input) — 2-arg (pre-router reads chatInput fields)
  - format_response(db_rows, params)   — returns str (not dict)
  - preprocessor.py is a retained utility; the pre-router handles passthru
    message building internally — no extra wiring needed in the graph
  - Standard AgentState (no body, no format_result, no current_message)
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

from .prompt import ACCOUNTING_AGENT_SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_accounting_query
from .formatter import format_response

logger = logging.getLogger(__name__)


def _unwrap_sp_row(row: Any) -> tuple:
    """Return (key, response_dict) from an execute_sp row.

    execute_sp keys each row by the SQL column name — 'sp_accounting' for
    queries built by sql_builder ("SELECT sp_accounting(...)"), or 'result'
    when an explicit alias is used. Returns (None, None) when no SP payload
    is found.
    """
    if isinstance(row, dict):
        for key in ("sp_accounting", "result"):
            if key in row:
                return key, row[key]
    return None, None


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-Router — mirrors n8n Accounting Pre Router v3.4 (2-arg signature)."""
    logger.info("=== Accounting Pre-Router Node (v3.4) ===")
    user_input = state.get("user_input", "")
    chat_input = state.get("chat_input", {})
    logger.info(f"Session: {state.get('session_id')!r}  Message: {user_input[:100]}")

    result = route_request(user_input, chat_input)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Accounting pre-router ROUTED → mode={params.get('mode')}")
        return {
            **state,
            "router_action":   True,
            "parsed_json":     params,
            "should_call_api": True,
        }

    current_message = result.get("current_message", user_input)
    logger.info(f"Accounting pre-router PASSTHRU → {current_message[:80]}")
    return {**state, "router_action": False, "user_input": current_message}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the accounting system prompt."""
    logger.info("=== Accounting AI Agent Node ===")
    session_id = state.get("session_id", "default-session")
    user_input = state.get("user_input", "")

    settings = get_settings()
    history  = get_history(session_id)
    messages = history + [{"role": "user", "content": user_input}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": ACCOUNTING_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(ACCOUNTING_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Accounting AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Accounting AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Accounting Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")
    if parsed:
        return {**state, "parsed_json": parsed, "should_call_api": True}
    return {**state, "parsed_json": None, "should_call_api": False}


def db_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Database node — builds and executes sp_accounting()."""
    logger.info("=== Accounting Database Node ===")
    try:
        parsed_json = state.get("parsed_json") or {}
        if not parsed_json:
            return {**state, "db_rows": []}

        # ── Executive finance questions — sp_orchestrator('executive') pack,
        # formatted by the shared executive Q&A layer (decision headline +
        # the question's relevant sections).
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
        _ui_only_modes = {'show_invoice_form', 'show_payment_form', 'show_void_invoice_form'}
        if parsed_json.get("mode") in _ui_only_modes:
            logger.info(f"db_node: UI-only mode '{parsed_json.get('mode')}' — skipping DB call")
            return {**state, "db_rows": [{"result": {
                "metadata": {"status": "success", "code": 0, "mode": parsed_json.get("mode")}
            }}]}

        # ── Invoice-number → invoice_id resolution for get_invoice_360 ──────
        # Pre-router routes "Get invoice INV-000620" with invoiceNumber only;
        # resolve it to a UUID via a list_invoices search, then run the 360.
        # 0 or 2+ matches fall back to a filtered invoice list.
        if (parsed_json.get("mode") == "get_invoice_360"
                and not parsed_json.get("invoiceId")
                and parsed_json.get("invoiceNumber")):
            _invnum = str(parsed_json["invoiceNumber"]).strip()
            _lookup_sql, _ = build_accounting_query(
                {"mode": "list_invoices", "search": _invnum, "pageSize": 5})
            _lookup_rows = execute_sp(_lookup_sql)
            _invs = []
            if _lookup_rows:
                _, _res = _unwrap_sp_row(_lookup_rows[0])
                if isinstance(_res, dict):
                    _invs = _res.get("invoices") or []
            _exact = [i for i in _invs
                      if str(i.get("invoice_number") or "").upper() == _invnum.upper()]
            _pick = _exact[0] if len(_exact) == 1 else (_invs[0] if len(_invs) == 1 else None)
            if _pick and _pick.get("invoice_id"):
                parsed_json = {k: v for k, v in parsed_json.items() if k != "invoiceNumber"}
                parsed_json["invoiceId"] = _pick["invoice_id"]
                logger.info(f"[inv-number-resolve] {_invnum} → {parsed_json['invoiceId']}")
            else:
                logger.info(
                    f"[inv-number-resolve] {_invnum} matched {len(_invs)} invoices "
                    f"— falling back to filtered list")
                parsed_json = {"mode": "list_invoices", "search": _invnum}
            state = {**state, "parsed_json": parsed_json}

        query, _ = build_accounting_query(parsed_json)
        logger.info(f"Built sp_accounting query for mode: {parsed_json.get('mode')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_accounting returned {len(db_rows)} rows")

        # ── Python-side overdue filter ───────────────────────────────────────
        # sp_accounting's statusFilter='unpaid' catches unpaid invoices, but we
        # also need due_date < today to exclude invoices not yet past due.
        if parsed_json.get("overdue") and parsed_json.get("mode") == "list_invoices":
            from datetime import date as _date
            today_str = _date.today().isoformat()
            try:
                overdue_filtered = []
                for row in db_rows:
                    _row_key, result = _unwrap_sp_row(row)
                    if isinstance(result, dict) and "invoices" in result:
                        invoices = result["invoices"] or []
                        inv_f = [
                            inv for inv in invoices
                            if (inv.get("due_date") or "9999-12-31") < today_str
                            and str(inv.get("payment_status") or "").lower()
                               not in ("paid", "void")
                        ]
                        if len(inv_f) < len(invoices):
                            logger.info(f"[overdue-filter] invoices: {len(invoices)} → {len(inv_f)}")
                        result = dict(result)
                        result["invoices"] = inv_f
                        meta = dict(result.get("metadata") or {})
                        meta["total_records"] = len(inv_f)
                        _ps = int(meta.get("page_size") or 50)
                        meta["total_pages"] = max(1, (len(inv_f) + _ps - 1) // _ps)
                        result["metadata"] = meta
                        overdue_filtered.append({_row_key: result})
                    else:
                        overdue_filtered.append(row)
                db_rows = overdue_filtered
            except Exception as fe:
                logger.warning(f"[overdue-filter] skipped: {fe}")

        # ── Python-side search safety filter ────────────────────────────────
        req_search = (parsed_json.get("search") or "").strip().lower()
        req_mode   = (parsed_json.get("mode") or "").lower()
        if req_search and req_mode in ("list_payments", "list_invoices"):
            _list_key = "payments" if req_mode == "list_payments" else "invoices"
            try:
                filtered = []
                for row in db_rows:
                    _row_key, result = _unwrap_sp_row(row)
                    if isinstance(result, dict) and _list_key in result:
                        items = result[_list_key] or []
                        items_filtered = [
                            item for item in items
                            if req_search in str(item.get("account_name") or "").lower()
                            or req_search in str(item.get("invoice_number") or "").lower()
                            or req_search in str(item.get("contact_name") or "").lower()
                        ]
                        # Only rewrite the payload when the safety filter
                        # actually removed rows — the SP filters server-side,
                        # so overwriting total_records/total_pages on a no-op
                        # pass would clobber correct multi-page totals.
                        if len(items_filtered) < len(items):
                            logger.info(
                                f"[search-filter] {_list_key}: "
                                f"{len(items)} → {len(items_filtered)} "
                                f"for search={req_search!r}"
                            )
                            result = dict(result)
                            result[_list_key] = items_filtered
                            meta = dict(result.get("metadata") or {})
                            meta["total_records"] = len(items_filtered)
                            _ps = int(meta.get("page_size") or 50)
                            meta["total_pages"] = max(1, (len(items_filtered) + _ps - 1) // _ps)
                            result["metadata"] = meta
                            filtered.append({_row_key: result})
                        else:
                            filtered.append(row)
                    else:
                        filtered.append(row)
                db_rows = filtered
            except Exception as fe:
                logger.warning(f"[search-filter] Python filter skipped: {fe}")

        return {**state, "db_rows": db_rows}

    except Exception as e:
        logger.error(f"Accounting database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output (str) and persists turn to memory."""
    logger.info("=== Accounting Formatter Node ===")
    try:
        if state.get("should_call_api"):
            db_rows     = state.get("db_rows") or []
            parsed_json = state.get("parsed_json") or {}
            final_output = format_response(db_rows, parsed_json)
            logger.info(f"Accounting formatted — mode={parsed_json.get('mode')} len={len(final_output)}")
        else:
            final_output = state.get("ai_output") or "No response generated"
            logger.info("Accounting returning conversational response")

        session_id = state.get("session_id", "default-session")
        user_input = state.get("user_input", "")
        if user_input and final_output:
            save_turn(session_id, user_input, final_output)

        return {**state, "final_output": final_output}

    except Exception as e:
        logger.error(f"Accounting formatter error: {e}", exc_info=True)
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
            graph_label="Accounting Agent",
        )
    return _graph_app
