"""LangGraph agent module for the Email domain."""

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
from app.core.database import execute_sp, get_connection

from .prompt import EMAIL_AGENT_SYSTEM_PROMPT
from .pre_router import route_request
from .sql_builder import build_email_query
from .formatter import format_response
from .smtp_imap import send_email, fetch_inbox, search_inbox
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)


# ============================================================================
# STATE
# ============================================================================

class EmailAgentState(TypedDict):
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
    extra:            Optional[Dict[str, Any]]   # SMTP/IMAP results, status_text


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("=== EmailAgent Pre-Router Node ===")
    body       = state.get("body", {})
    chat_input = state.get("chat_input", {})
    session_id = state.get("session_id", "")
    logger.info(f"Message: {chat_input.get('message', '')[:120]}")

    result = route_request(body, chat_input, session_id)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"EmailAgent pre-router ROUTED → mode={params.get('mode')}")
        return {
            **state,
            "router_action":   True,
            "parsed_json":     params,
            "should_call_api": True,
            "current_message": state.get("user_input", ""),
            "extra":           {},
        }

    current_msg = result.get("current_message", state.get("user_input", ""))
    return {**state, "router_action": False, "current_message": current_msg, "extra": {}}


def ai_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("=== EmailAgent AI Agent Node ===")
    session_id      = state.get("session_id", "default-session")
    current_message = state.get("current_message") or state.get("user_input", "")

    settings = get_settings()
    history  = get_history(session_id)
    messages = history + [{"role": "user", "content": current_message}]

    try:
        if settings.llm_provider == "openai":
            llm = _get_llm()
            full_messages = [{"role": "system", "content": EMAIL_AGENT_SYSTEM_PROMPT}] + messages
            response  = llm.invoke(full_messages)
            ai_output = response.content if hasattr(response, "content") else str(response)
        else:
            ai_output = _call_ollama_direct(EMAIL_AGENT_SYSTEM_PROMPT, messages)

        clean = re.sub(r"<think>[\s\S]*?</think>", "[think]", ai_output).strip()
        logger.info(f"EmailAgent AI output ({len(ai_output)} chars): {clean[:200]}")
        return {**state, "ai_output": ai_output}

    except Exception as e:
        logger.error(f"EmailAgent AI Agent error: {e}", exc_info=True)
        return {**state, "ai_output": f"Error: {str(e)}"}


def parse_output_node(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("=== EmailAgent Parse Output Node ===")
    parsed = parse_ai_json(state.get("ai_output") or "")
    if parsed:
        return {**state, "parsed_json": parsed, "should_call_api": True}
    return {**state, "parsed_json": None, "should_call_api": False}


def db_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Database / SMTP / IMAP node."""
    logger.info("=== EmailAgent DB/Email Node ===")
    try:
        parsed_json = state.get("parsed_json") or {}
        if not parsed_json:
            return {**state, "db_rows": [], "extra": {}}

        mode = str(parsed_json.get('mode') or '').lower().strip()
        extra: Dict[str, Any] = {}

        # ── IMAP operations ───────────────────────────────────────────────────
        if mode == 'imap_inbox':
            limit  = int(parsed_json.get('limit') or 20)
            emails = fetch_inbox(limit=limit, unseen_only=False)
            extra['emails'] = emails
            return {**state, "db_rows": [], "extra": extra}

        if mode == 'imap_search':
            query  = str(parsed_json.get('query') or '')
            limit  = int(parsed_json.get('limit') or 20)
            emails = search_inbox(query=query, limit=limit)
            extra['emails'] = emails
            return {**state, "db_rows": [], "extra": extra}

        # ── SMTP send_email (custom compose) ──────────────────────────────────
        if mode == 'send_email':
            result = send_email(
                to        = str(parsed_json.get('to') or ''),
                subject   = str(parsed_json.get('subject') or ''),
                body_html = str(parsed_json.get('bodyHtml') or parsed_json.get('body_html') or ''),
                body_text = str(parsed_json.get('bodyText') or parsed_json.get('body_text') or ''),
                commercial=True,   # CASL: suppression check + unsubscribe footer
            )
            # This one logged action='sent' UNCONDITIONALLY — a refused send
            # was recorded as sent mail.
            _log_send_attempt(str(parsed_json.get('to') or ''),
                              str(parsed_json.get('subject') or ''), mode, result)
            extra['sent_result'] = result
            return {**state, "db_rows": [], "extra": extra}

        # ── send_template: fetch entity + template + send ─────────────────────
        if mode == 'send_template':
            result = _handle_send_template(parsed_json)
            extra['sent_result'] = result
            return {**state, "db_rows": [], "extra": extra}

        # ── DB queries (sp_notifications, email_templates, audit_log, status) ──
        query, debug = build_email_query(parsed_json)
        if not query:
            logger.info(f"EmailAgent: no DB query for mode={mode}")
            return {**state, "db_rows": [], "extra": extra}

        raw_flag = debug.get('raw', False)
        if raw_flag:
            db_rows = _execute_raw(query)
        else:
            db_rows = execute_sp(query)

        logger.info(f"EmailAgent DB returned {len(db_rows)} rows for mode={mode}")
        return {**state, "db_rows": db_rows, "extra": extra}

    except WritePermissionError:
        raise
    except Exception as e:
        logger.error(f"EmailAgent DB/Email node error: {e}", exc_info=True)
        return {**state, "db_rows": [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(e)}
        }}], "extra": {"error": str(e)}}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("=== EmailAgent Formatter Node ===")
    try:
        if state.get("should_call_api"):
            db_rows     = state.get("db_rows") or []
            parsed_json = state.get("parsed_json") or {}
            extra       = state.get("extra") or {}
            fmt_result  = format_response(db_rows, parsed_json, extra)
            final_output = fmt_result.get("output", "")
            logger.info(f"EmailAgent formatted — mode={fmt_result.get('mode')} len={len(final_output)}")
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
        logger.error(f"EmailAgent formatter error: {e}", exc_info=True)
        err = {"output": f"Error: {str(e)}", "mode": "error", "success": False}
        return {**state, "format_result": err, "final_output": err["output"]}


# ============================================================================
# HELPERS
# ============================================================================

def _execute_raw(query: str) -> List[Dict[str, Any]]:
    """Execute a raw query that returns row_to_json rows (column: result)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
            result = []
            for row in rows:
                val = row[0] if row else None
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                result.append({'result': val})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _handle_send_template(params: dict) -> dict:
    """Fetch entity record, fill template, and send email."""
    from app.core.database import execute_sp as _sp

    entity_type   = str(params.get('entityType') or 'lead').lower()
    entity_id     = str(params.get('entityId') or '')
    template_type = str(params.get('templateType') or 'welcome').lower()

    # The friendly name a caller types is not the row's event_type, and the
    # lookup below searches event_type. 'welcome' is the default templateType
    # AND the one the pre-router sends for "send welcome email:", yet NO
    # template's event_type contains that word — the welcome copy lives under
    # event_type='lead.created', with "Welcome" only in subject_tpl. So every
    # welcome request has been answering "No active template found for type:
    # welcome" rather than sending anything.
    #
    # Mapped explicitly rather than by also searching subject_tpl: a LIKE over
    # subject text would match 'account.created' ("Welcome to Orbit, …") just as
    # readily and pick whichever the planner returned first, so a lead could be
    # sent an account welcome. Unmapped names fall through to the original
    # event_type search, which is what makes 'invoice' still resolve to
    # 'invoice_created'.
    _TEMPLATE_ALIASES = {
        'welcome':   'lead.created',
        'converted': 'lead.converted',
        'qualified': 'lead.qualified',
        'account':   'account.created',
    }
    if entity_type == 'account' and template_type == 'welcome':
        template_type = 'account.created'          # welcoming an ACCOUNT
    else:
        template_type = _TEMPLATE_ALIASES.get(template_type, template_type)

    if not entity_id:
        return {'success': False, 'message': 'entityId is required for send_template'}

    # Fetch the template
    tpl_query = (
        f"SELECT row_to_json(t) AS result FROM ("
        f"  SELECT subject_tpl, body_html_tpl, body_text_tpl, from_name, from_email, bcc_email "
        f"  FROM email_templates "
        f"  WHERE event_type ILIKE '%{template_type}%' AND is_active = TRUE "
        f"  LIMIT 1"
        f") t;"
    )
    tpl_rows = _execute_raw(tpl_query)
    if not tpl_rows:
        return {'success': False, 'message': f'No active template found for type: {template_type}'}

    tpl_data = tpl_rows[0].get('result') or {}

    # Fetch entity record
    entity_data = _fetch_entity(entity_type, entity_id)
    if not entity_data:
        return {'success': False, 'message': f'Entity not found: {entity_type} {entity_id}'}

    # Determine recipient email
    to_email = (
        entity_data.get('email') or
        entity_data.get('primary_email') or
        entity_data.get('contact_email') or ''
    )
    if not to_email:
        return {'success': False, 'message': f'No email address found for {entity_type} {entity_id}'}

    # Fill template tokens
    entity_data = _with_display_tokens(entity_data)
    subject   = _fill_tokens(str(tpl_data.get('subject_tpl',   '')), entity_data)
    body_html = _fill_tokens(str(tpl_data.get('body_html_tpl', '')), entity_data)
    body_text = _fill_tokens(str(tpl_data.get('body_text_tpl', '')), entity_data)
    from_name = str(tpl_data.get('from_name', 'Conscestra CRM Team'))
    bcc       = str(tpl_data.get('bcc_email', '')) or None

    result = send_email(
        to        = to_email,
        subject   = subject,
        body_html = body_html,
        body_text = body_text,
        from_name = from_name,
        bcc       = bcc,
        commercial=True,   # CASL: template sends are commercial messages
    )

    _log_send_attempt(to_email, subject, template_type, result)

    return result


def _fetch_entity(entity_type: str, entity_id: str) -> Optional[dict]:
    """Fetch a lead/contact/account and return its data dict."""
    from app.core.database import execute_sp as _sp

    sp_map = {
        'lead':    ('sp_leads',    'p_lead_id'),
        'contact': ('sp_contacts', 'p_contact_id'),
        'account': ('sp_accounts', 'p_account_id'),
    }
    entry = sp_map.get(entity_type)
    if not entry:
        return None

    # These SPs return an envelope keyed by entity name, not 'data'/'record':
    #   sp_leads    -> {'lead': {...},    'metadata': {...}}
    #   sp_accounts -> {'account': {...}, 'contacts': [...], 'stats': {...}, ...}
    # Unwrapping only 'data'/'record' therefore returned the ENVELOPE, whose
    # keys are 'lead' and 'metadata' — no 'email' — so every lead send died at
    # "No email address found". Try the entity-named key first.

    sp_name, id_param = entry
    query = f"SELECT {sp_name}({id_param} := '{entity_id}', p_mode := 'get') AS result;"
    try:
        rows = _sp(query)
        if not rows:
            return None
        result = rows[0].get('result') or {}
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                return None
        data = (result.get(entity_type)      # 'lead' / 'contact' / 'account'
                or result.get('data')
                or result.get('record')
                or result)
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            return None
        # An envelope that yielded no usable record is a miss, not a record with
        # a missing email — returning it would surface as "no email address"
        # and hide the real cause. sp_contacts answers {'metadata': ...} alone
        # when the id resolves to nothing.
        if not any(k in data for k in ('email', 'primary_email', 'contact_email')):
            logger.warning(
                f"_fetch_entity: {entity_type} {entity_id} returned no record "
                f"carrying an email (keys={sorted(data)[:8]})")
            return None
        return data
    except Exception as e:
        logger.error(f"_fetch_entity error: {e}")
        return None


def _with_display_tokens(data: dict) -> dict:
    """Add presentation-only tokens so warm prose survives missing data.

    Templates are flat string substitution — no conditionals — so a sentence
    like "learn more about {{company}} and how we might help" collapses into
    "learn more about  and how we might help" the moment a lead has no company.
    That is not hypothetical: 5 of 122 leads have no company, 4 no source, 1 no
    last name.

    A blank cell in a details TABLE is untidy; a blank hole mid-SENTENCE reads
    as a broken mail merge. So prose gets a fallback that still scans, and the
    table gets an em dash instead of an empty cell.

    Returns a copy — the caller's entity dict is not mutated.
    """
    out = dict(data)

    def _clean(key: str) -> str:
        return str(out.get(key) or '').strip()

    company, source = _clean('company'), _clean('source')
    first, last     = _clean('first_name'), _clean('last_name')

    # Prose: must read naturally either way.
    out['company_or_yours'] = company or 'your business'
    # Tables: visibly absent beats invisibly empty.
    out['company_display']  = company or '—'
    out['source_display']   = source  or '—'
    # Avoids the trailing space a blank last name leaves behind.
    out['full_name']        = ' '.join(p for p in (first, last) if p) or 'there'

    return out


def _fill_tokens(template: str, data: dict) -> str:
    """Replace {{token}} placeholders with data values."""
    for key, value in data.items():
        if value is not None:
            template = template.replace('{{' + key + '}}', str(value))
            # Also try common aliases
            snake = re.sub(r'([A-Z])', r'_\1', key).lower().lstrip('_')
            if snake != key:
                template = template.replace('{{' + snake + '}}', str(value))

    # Anything still unresolved would go out to the customer as literal
    # "{{source}}". The lead templates happen to fill completely from a lead
    # row, but the same welcome template is documented for contacts, which have
    # no `source` — so the leak is one entityType away, not hypothetical.
    # Blank it and say which tokens were missing: an empty label is a cosmetic
    # flaw, raw template syntax in a customer's inbox is not.
    leftover = re.findall(r'\{\{(\w+)\}\}', template)
    if leftover:
        logger.warning(f"_fill_tokens: unresolved tokens dropped: {sorted(set(leftover))}")
        template = re.sub(r'\{\{\w+\}\}', '', template)
    return template


def _log_send_attempt(to: str, subject: str, event_type: str, result: dict):
    """Record what actually happened to a template send.

    This wrote action='sent' whenever send_email returned success — but success
    on both transports means "the provider ACCEPTED this for delivery", which is
    the SMTP 250 or the Resend 200, not evidence anybody received it. The order
    notification path stops at 'accepted' for exactly this reason; this path
    claimed more than it knew, in the same audit table.

    So: record the provider's own answer. 'accepted' when it took the message,
    'failed' when it did not — and log the failures too, which the old
    success-only call discarded, leaving a refused send with no trace at all.
    """
    accepted = bool(result.get('success'))
    action   = 'accepted' if accepted else 'failed'

    payload = {
        'to':              to,
        'subject':         subject,
        'event_type':      event_type,
        'outcome':         action,
        'provider':        result.get('provider'),
        'provider_msg_id': result.get('provider_message_id'),
        'provider_status': result.get('provider_status'),
        # Only meaningful on failure; the provider's refusal reason.
        'reason':          None if accepted else result.get('message'),
    }

    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # Parameterised — the old build interpolated the subject and
                # recipient into JSON by hand, which a quote in a company name
                # was one token away from breaking.
                cur.execute(
                    "INSERT INTO audit_log (entity, entity_id, action, payload, created_at) "
                    "VALUES ('email', gen_random_uuid(), %s, %s::jsonb, now())",
                    (action, json.dumps(payload)),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"audit_log insert failed (non-fatal): {e}")


# ============================================================================
# GRAPH SINGLETON
# ============================================================================

_graph_app = None


def get_graph():
    global _graph_app
    if _graph_app is None:
        _graph_app = build_graph_with_schema(
            state_schema=EmailAgentState,
            pre_router_node=pre_router_node,
            ai_agent_node=ai_agent_node,
            parse_output_node=parse_output_node,
            db_node=db_node,
            formatter_node=formatter_node,
            graph_label="Email Agent",
        )
    return _graph_app
