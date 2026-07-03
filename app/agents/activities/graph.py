"""LangGraph agent module for the Activities domain.

State notes vs base AgentState:
  + body            — full request body (for pre-router CASE 1)
  + current_message — pre-router passthru may rewrite this (NL supplement)
  + format_result   — formatter returns a dict, not a str

route_request(body, chat_input, session_id) — 3-arg signature.
format_response(db_rows, params)            — returns Dict[str, Any].
build_activities_query(params)              — 1-arg, standard.
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

from .prompt import ACTIVITY_AGENT_SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_activities_query
from .formatter import format_response
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)


# ============================================================================
# EXTENDED STATE
# ============================================================================

class ActivityAgentState(TypedDict):
    session_id:       str
    body:             dict           # full request body for pre-router CASE 1
    chat_input:       dict
    user_input:       str
    current_message:  str            # may be supplemented by pre-router passthru
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
    """Pre-Router — mirrors n8n Activities Pre Router (prefix-based)."""
    logger.info("=== Activities Pre-Router Node ===")
    body       = state.get("body", {})
    chat_input = state.get("chat_input", {})
    session_id = state.get("session_id", "")
    logger.info(f"Message: {chat_input.get('message', '')[:120]}")

    result = route_request(body, chat_input, session_id)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Activities pre-router ROUTED → mode={params.get('mode')}")
        return {
            **state,
            "router_action":   True,
            "parsed_json":     params,
            "should_call_api": True,
            "current_message": state.get("user_input", ""),
        }

    current_msg = result.get("current_message", state.get("user_input", ""))
    logger.info(f"Activities pre-router PASSTHRU → {current_msg[:80]}")
    return {**state, "router_action": False, "current_message": current_msg}


_MAX_HISTORY_CHARS = 40_000   # ~10K tokens budget for history


def _trim_history(history: list, max_chars: int) -> list:
    """Drop oldest message pairs until total history fits within max_chars."""
    while history:
        total = sum(len(m.get("content", "")) for m in history)
        if total <= max_chars:
            break
        # Drop the oldest user+assistant pair (2 messages)
        history = history[2:]
    return history


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """AI Agent node — invokes LLM with the activities system prompt."""
    logger.info("=== Activities AI Agent Node ===")
    session_id      = state.get("session_id", "default-session")
    current_message = state.get("current_message") or state.get("user_input", "")

    settings = get_settings()
    history  = _trim_history(list(get_history(session_id)), _MAX_HISTORY_CHARS)
    messages = history + [{"role": "user", "content": current_message}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": ACTIVITY_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(ACTIVITY_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"Activities AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"Activities AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Parse AI Output — extract JSON from the LLM's response."""
    logger.info("=== Activities Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")
    if parsed:
        return {**state, "parsed_json": parsed, "should_call_api": True}
    return {**state, "parsed_json": None, "should_call_api": False}


_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)

# Map relatedType → (SP name, id_field, result_list_key, display_name_fields)
_ENTITY_SP_MAP = {
    'account':     ('sp_accounts',     'account_id',     'accounts',      ['account_name']),
    'contact':     ('sp_contacts',      'contact_id',     'contacts',      ['first_name', 'last_name']),
    'lead':        ('sp_leads',         'lead_id',        'leads',         ['first_name', 'last_name']),
    'opportunity': ('sp_opportunities', 'opportunity_id', 'opportunities', ['opportunity_name']),
}

# Search order when relatedType is unknown or the primary type yields no results
_SEARCH_ORDER = ['account', 'contact', 'lead', 'opportunity']


def _search_entity_type(entity_type: str, name: str):
    """Search one entity type. Returns (uuid, actual_type) or (None, None).

    For contacts: returns the contact's account_id with relatedType='account'
    so that sp_activities timeline finds all account-level activities, matching
    the behaviour of the Account Management module.
    """
    mapping = _ENTITY_SP_MAP.get(entity_type)
    if not mapping:
        return None, None
    sp_name, id_field, list_key, name_fields = mapping
    safe_name = name.replace("'", "''")
    try:
        rows = execute_sp(
            f"SELECT {sp_name}(p_mode := 'list', p_search := '{safe_name}', p_page_size := 5) AS result;"
        )
        if not rows:
            return None, None
        result = rows[0].get('result') if isinstance(rows[0], dict) else None
        if not isinstance(result, dict):
            return None, None
        entities = result.get(list_key) or []

        # ── Rank candidates before declaring ambiguity ───────────────────
        #   1. Exact (case-insensitive) name matches beat partial matches.
        #   2. Active records beat duplicate_merged / inactive ones.
        #   3. Contacts that all share one account collapse to that account.
        #   4. Identical-name duplicates (e.g. two active "Québec Robotics
        #      Lab" accounts) pick the first — an unanswerable "be more
        #      specific" error helps nobody when the names are identical.
        def _disp(e):
            return ' '.join(filter(None, [str(e.get(f) or '') for f in name_fields])).strip()

        if entities:
            exact = [e for e in entities if _disp(e).lower() == name.lower()]
            pool = exact or entities
            active = [e for e in pool
                      if str(e.get('status') or 'active').lower() == 'active']
            pool = active or pool

            if entity_type == 'contact':
                acct_ids = {str(e['account_id']) for e in pool if e.get('account_id')}
                if len(acct_ids) == 1:
                    acct_id = acct_ids.pop()
                    logger.info(
                        f"Contact '{name}': {len(pool)} match(es) share account_id "
                        f"{acct_id} — using account-level timeline"
                    )
                    return (acct_id, 'account')

            distinct_names = {_disp(e).lower() for e in pool}
            if len(pool) == 1 or len(distinct_names) == 1:
                if len(pool) > 1:
                    logger.warning(
                        f"'{name}': {len(pool)} identical-name {entity_type} "
                        f"duplicates — picking the first match"
                    )
                entity = pool[0]
                if entity_type == 'contact' and entity.get('account_id'):
                    return (str(entity['account_id']), 'account')
                uuid = entity.get(id_field)
                return (str(uuid), entity_type) if uuid else (None, None)

            display = ', '.join(_disp(e) or '?' for e in pool[:5])
            return (f'__multiple__:{entity_type}:{display}', entity_type)
    except Exception as exc:
        logger.warning(f"Entity search failed [{entity_type}] '{name}': {exc}")
    return None, None


def _resolve_entity_name(
    related_type: str, name: str
) -> tuple[Optional[str], Optional[str], str]:
    """Resolve a name to (uuid, resolved_type, error_msg).

    Tries the given relatedType first, then falls back to other types so that
    "show timeline for Steven Brahms" works even if the AI guessed 'account'
    but Steven Brahms is actually a 'contact'.
    """
    # Build search priority: hint type first, then others
    search_order = [related_type.lower()] + [t for t in _SEARCH_ORDER if t != related_type.lower()]

    for entity_type in search_order:
        uuid, found_type = _search_entity_type(entity_type, name)
        if uuid and uuid.startswith('__multiple__:'):
            _, et, display = uuid.split(':', 2)
            return None, None, (
                f"Multiple {et}s match '{name}': {display}. "
                "Please be more specific (e.g. include last name or company)."
            )
        if uuid:
            # found_type may differ from entity_type (e.g. contact → account_id)
            if found_type != related_type.lower():
                logger.info(f"Resolved '{name}' as {found_type} (searched {entity_type}) → {uuid}")
            return uuid, found_type, ''   # return found_type, not entity_type

    return None, None, (
        f"No account, contact, or lead found matching '{name}'. "
        "Please check the spelling or try a different name."
    )


def db_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Database node — builds and executes sp_activities().

    Auto-resolves relatedId names → UUIDs so users can say
    'Show timeline for Bob Brown' instead of providing a UUID.
    """
    logger.info("=== Activities Database Node ===")
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

        # Safety guard: mode:get without activityId → fall back to list search
        if parsed_json.get("mode") in ("get", "update", "complete", "reopen", "delete") \
                and not parsed_json.get("activityId"):
            user_input = state.get("user_input", "").strip()
            logger.warning(f"mode:{parsed_json['mode']} missing activityId — falling back to list search")
            parsed_json = {"mode": "list", "search": user_input or None, "pageSize": 20}

        # ── Auto-resolve relatedId name → UUID ──────────────────────────────
        # Triggered for any mode that uses relatedId (timeline, create, log_call, etc.)
        # when the value is a name rather than a UUID.
        # Falls back across entity types so "Steven Brahms" works even if AI
        # guessed relatedType:"account" but he is actually a contact.
        related_id   = (parsed_json.get("relatedId") or "").strip()
        related_type = (parsed_json.get("relatedType") or "account").strip()
        if related_id and not _UUID_RE.match(related_id):
            logger.info(f"relatedId '{related_id}' is not UUID — resolving (hint={related_type})")
            resolved_uuid, resolved_type, err_msg = _resolve_entity_name(related_type, related_id)
            if not resolved_uuid:
                mode_hint = parsed_json.get("mode", "")
                if mode_hint in ("create", "log_call", "log_email", "schedule_meeting",
                                 "create_task", "add_note"):
                    err_msg += (
                        f" To create a {mode_hint.replace('_',' ')} for this person, "
                        "please verify their exact name in the Leads, Contacts, or Accounts module first."
                    )
                return {**state, "db_rows": [{"result": {
                    "metadata": {"status": "error", "code": -404, "message": err_msg}
                }}]}
            logger.info(f"Resolved '{related_id}' → {resolved_type} UUID {resolved_uuid}")
            parsed_json = {**parsed_json, "relatedId": resolved_uuid, "relatedType": resolved_type}
        elif not related_id and parsed_json.get("mode") == "timeline":
            # Timeline without entity — return a helpful prompt instead of a -80 SP error
            logger.warning("timeline mode with no relatedId — returning prompt to user")
            return {**state, "db_rows": [{"result": {
                "metadata": {
                    "status": "error", "code": -1,
                    "message": (
                        "Timeline requires an account or contact name. "
                        "Please type a name in the search bar and try again. "
                        "Example: 'Show timeline for Bob Brown'"
                    )
                }
            }}]}

        # ── Timeline bypass: use direct account_id query ─────────────────
        _tl_mode  = parsed_json.get("mode")
        _tl_rtype = parsed_json.get("relatedType")
        _tl_rid   = parsed_json.get("relatedId")
        logger.info(f"[bypass-check] mode={_tl_mode!r} relType={_tl_rtype!r} relId={str(_tl_rid)[:16]!r}")
        if (_tl_mode == "timeline"
                and _tl_rtype == "account"
                and _tl_rid):
            acct_uuid = parsed_json["relatedId"]
            safe_id   = acct_uuid.replace("'", "")  # UUID — no injection risk
            direct_sql = f"""
SELECT json_build_object(
    'metadata', json_build_object('status','success','code',0,
                                  'timestamp',NOW(),
                                  'entity_name',(
                                      SELECT COALESCE(a.account_name,
                                          c.first_name||' '||c.last_name)
                                      FROM accounts a
                                      FULL OUTER JOIN contacts c
                                          ON c.account_id=a.account_id
                                      WHERE a.account_id='{safe_id}'::uuid
                                         OR c.account_id='{safe_id}'::uuid
                                      LIMIT 1
                                  )),
    'timeline', COALESCE((
        SELECT json_agg(
            json_build_object(
                'event_type', event_type,
                'event_id',   event_id,
                'timestamp',  ts,
                'summary',    summary,
                'details',    details
            ) ORDER BY ts DESC
        )
        FROM (
            SELECT a.created_at AS ts,
                   'activity'::TEXT AS event_type,
                   a.activity_id::TEXT AS event_id,
                   json_build_object(
                       'subject',    a.subject,
                       'type',       a.type,
                       'direction',  a.direction,
                       'channel',    a.channel,
                       'account_name', (SELECT account_name FROM accounts WHERE account_id='{safe_id}'::uuid),
                       'contact_name', CASE WHEN c.contact_id IS NOT NULL
                                       THEN c.first_name||' '||c.last_name ELSE NULL END
                   ) AS details,
                   'Activity: '||COALESCE(a.subject,a.type) AS summary
            FROM activities a
            LEFT JOIN contacts c ON c.contact_id = a.contact_id
            WHERE a.account_id = '{safe_id}'::uuid
               OR (a.related_type='account' AND a.related_id='{safe_id}'::uuid)
            UNION ALL
            SELECT o.created_at AS ts, 'order'::TEXT, o.order_id::TEXT,
                   json_build_object(
                       'order_number', o.order_number,
                       'status',       o.status,
                       'amount',       o.total_amount,
                       'account_name', (SELECT account_name FROM accounts WHERE account_id='{safe_id}'::uuid)
                   ),
                   'Order: '||COALESCE(o.order_number::TEXT,o.order_id::TEXT)
            FROM orders o WHERE o.account_id = '{safe_id}'::uuid
            UNION ALL
            SELECT i.created_at AS ts, 'invoice'::TEXT, i.invoice_id::TEXT,
                   json_build_object(
                       'invoice_number', i.invoice_number,
                       'status',         i.status,
                       'amount',         i.total_amount,
                       'account_name',   (SELECT account_name FROM accounts WHERE account_id='{safe_id}'::uuid)
                   ),
                   'Invoice: '||COALESCE(i.invoice_number::TEXT,i.invoice_id::TEXT)
            FROM invoices i WHERE i.account_id = '{safe_id}'::uuid
            UNION ALL
            SELECT p.payment_date AS ts, 'payment'::TEXT, p.payment_id::TEXT,
                   json_build_object(
                       'amount',       p.amount,
                       'method',       p.payment_method,
                       'status',       p.status,
                       'account_name', (SELECT account_name FROM accounts WHERE account_id='{safe_id}'::uuid)
                   ),
                   'Payment: '||p.amount::TEXT
            FROM payments p WHERE p.account_id = '{safe_id}'::uuid
        ) all_events
    ), '[]'::json)
) AS result;"""
            logger.info(f"Timeline direct query for account {safe_id}")
            db_rows = execute_sp(direct_sql)
            logger.info(f"Direct timeline returned {len(db_rows)} rows")
            return {**state, "db_rows": db_rows}

        # Post-filter params (consumed here, not by the SP): the overdue /
        # upcoming SP modes take no search/type/date-window params, so the
        # pre-router passes these for Python-side narrowing of the result.
        _name_filter = (parsed_json.pop("nameFilter", None) or "").strip().lower()
        _type_filter = (parsed_json.pop("typeFilter", None) or "").strip().lower()
        _due_from    = parsed_json.pop("dueFrom", None)
        _due_to      = parsed_json.pop("dueTo", None)

        query, _ = build_activities_query(parsed_json)
        logger.info(f"Built sp_activities query for mode: {parsed_json.get('mode')}")

        db_rows = execute_sp(query)
        logger.info(f"sp_activities returned {len(db_rows)} rows")

        if (_name_filter or _type_filter or _due_from or _due_to) and db_rows:
            _list_keys = ('activities', 'overdue', 'upcoming')
            try:
                filtered_rows = []
                for row in db_rows:
                    result = row.get("result") if isinstance(row, dict) else None
                    if not isinstance(result, dict):
                        filtered_rows.append(row)
                        continue
                    result = dict(result)
                    for lk in _list_keys:
                        items = result.get(lk)
                        if not isinstance(items, list):
                            continue
                        before = len(items)
                        if _type_filter:
                            items = [a for a in items
                                     if str(a.get("type") or "").lower() == _type_filter]
                        if _name_filter:
                            def _names(a):
                                rel = a.get("related")
                                rel_name = rel.get("name") if isinstance(rel, dict) else None
                                return " ".join(str(v or "") for v in (
                                    rel_name, a.get("related_name"),
                                    a.get("owner_name"), a.get("owner"),
                                    a.get("subject"))).lower()
                            items = [a for a in items if _name_filter in _names(a)]
                        if _due_from or _due_to:
                            def _due_ok(a):
                                d = str(a.get("due_at") or "")[:10]
                                if not d:
                                    return False
                                if _due_from and d < _due_from:
                                    return False
                                if _due_to and d > _due_to:
                                    return False
                                return True
                            items = [a for a in items if _due_ok(a)]
                        if len(items) != before:
                            logger.info(f"[post-filter] {lk}: {before} → {len(items)} "
                                        f"(name={_name_filter!r} type={_type_filter!r} "
                                        f"due={_due_from}..{_due_to})")
                        result[lk] = items
                        total_key = f"total_{lk}"
                        if total_key in result:
                            result[total_key] = len(items)
                        meta = dict(result.get("metadata") or {})
                        for ck in ("overdue_count", "upcoming_count", "total_records", "count"):
                            if ck in meta:
                                meta[ck] = len(items)
                        result["metadata"] = meta
                    filtered_rows.append({"result": result})
                db_rows = filtered_rows
            except Exception as fe:
                logger.warning(f"[post-filter] skipped: {fe}")

        return {**state, "db_rows": db_rows}

    except WritePermissionError:
        raise
    except Exception as e:
        logger.error(f"Activities database error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — formats output (dict) and persists turn to memory."""
    logger.info("=== Activities Formatter Node ===")
    try:
        if state.get("should_call_api"):
            db_rows     = state.get("db_rows") or []
            parsed_json = state.get("parsed_json") or {}
            fmt_result  = format_response(db_rows, parsed_json)
            final_output = fmt_result.get("output", "")
            logger.info(f"Activities formatted — mode={fmt_result.get('mode')} len={len(final_output)}")
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
            # Cap the stored assistant message to avoid bloating context on future turns.
            # Full output is still returned to the user; only the memory copy is trimmed.
            _MEM_CAP = 800
            memory_output = (final_output[:_MEM_CAP] + "…[truncated]") if len(final_output) > _MEM_CAP else final_output
            save_turn(session_id, user_input, memory_output)

        return {**state, "format_result": fmt_result, "final_output": final_output}

    except Exception as e:
        logger.error(f"Activities formatter error: {e}", exc_info=True)
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
            state_schema=ActivityAgentState,
            pre_router_node=pre_router_node,
            ai_agent_node=ai_agent_node,
            parse_output_node=parse_output_node,
            db_node=db_node,
            formatter_node=formatter_node,
            graph_label="Activities Agent",
        )
    return _graph_app
