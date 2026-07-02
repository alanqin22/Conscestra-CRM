"""Activities Pre-Router v3.0 — Python conversion of n8n 'Code in JavaScript' node.

OVERVIEW
  Inspects every incoming request message and either ROUTES it directly
  to the SQL builder (router_action=True) OR passes it through to the
  AI Agent (router_action=False).

  ARCHITECTURAL PRINCIPLE:
    Deterministic SQL operations → ALWAYS routed directly (no AI)
    Ambiguous / NL queries       → AI Agent fallback

KEY TRIGGERS — all produce router_action=True
  ┌──────────────────────────────────────┬───────────────────────────────┐
  │ Message prefix (chatInput.message)   │ SP mode                       │
  ├──────────────────────────────────────┼───────────────────────────────┤
  │ "list activities:"                   │ list                          │
  │ "overdue activities:"                │ overdue                       │
  │ "upcoming activities:"               │ upcoming                      │
  │ "activity summary:"                  │ summary                       │
  │ "activity timeline:"                 │ timeline                      │
  │ "get activity:"                      │ get                           │
  │ "create activity:"                   │ create                        │
  │ "update activity:"                   │ update                        │
  │ "complete activity:"                 │ complete                      │
  │ "reopen activity:"                   │ reopen                        │
  │ "delete activity:"                   │ delete                        │
  │ "get owners:"                        │ get_owners                    │
  │ All other messages                   │ Passthru → AI Agent (NL)      │
  └──────────────────────────────────────┴───────────────────────────────┘

CHANGELOG
  v3.0 — Added direct route for "get owners:" → mode get_owners.
  v2.0 — Full rewrite. Direct routes for all 11 SP modes from HTML UI.
  v1.0 — Original release (passthru / NL normalisation only).
"""

from __future__ import annotations

import re
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _val(v: Any) -> Optional[Any]:
    """Coerce empty string / None → None; otherwise return as-is."""
    if v is None or v == '':
        return None
    return v


def _extract_uuid(s: str) -> Optional[str]:
    m = UUID_RE.search(str(s or ''))
    return m.group(0) if m else None


def _routed(params: dict) -> dict:
    logger.info(f"→ ROUTED: mode={params.get('mode')}  params={str(params)[:200]}")
    return {'router_action': True, 'params': params}


def _passthru(raw: str, chat_input: dict) -> dict:
    current = raw
    if chat_input.get('pageSize'):   current += f", page size {chat_input['pageSize']}"
    if chat_input.get('pageNumber'): current += f", page number {chat_input['pageNumber']}"
    logger.info(f"→ PASSTHRU: AI Agent | currentMessage={current[:80]!r}")
    return {'router_action': False, 'current_message': current.strip()}


# ============================================================================
# MAIN ROUTER
# ============================================================================

def route_request(body: dict, chat_input: dict, session_id: str) -> dict:
    """
    Inspect the incoming request and return a routing decision.

    Routed:   { 'router_action': True,  'params': { ... } }
    Passthru: { 'router_action': False, 'current_message': str }
    """
    logger.info('=== Activities Pre-Router v3.0 ===')
    logger.info(f'SessionId: {session_id}')

    raw = (chat_input.get('message') or body.get('message') or '').strip()
    msg = raw.lower()

    # ── Deterministic web-search route ───────────────────────────────────────
    # Explicit "search the web…" phrasings go straight to mode=web_search so
    # they never depend on the AI agent's JSON discipline (with session history
    # it sometimes replies "I can only assist with CRM queries" instead of
    # emitting the web_search JSON).
    _web_m = re.match(
        r'^(?:please\s+)?(?:can\s+you\s+)?(?:search|browse|look\s*up|google)\s+'
        r'(?:on\s+|in\s+)?the\s+(?:web|internet)\s*(?:for|about|:)?\s*(.*)$',
        raw, re.IGNORECASE)
    if _web_m:
        _q = (_web_m.group(1) or '').strip().rstrip('?.!') or raw
        logger.info(f'-> ROUTED: web_search (deterministic) query={_q[:80]!r}')
        return {'router_action': True, 'params': {'mode': 'web_search', 'query': _q}}
    logger.info(f'Message: {raw[:120]}')

    # ── Web-intent guard — "search the web…", "look up … online" must reach
    # the AI agent (mode=web_search), not the deterministic search regexes.
    if msg and re.search(r'\b(?:web|internet|online)\b', msg):
        logger.info('→ PASSTHRU: web-intent message (AI agent handles web_search)')
        return {'router_action': False, 'current_message': raw}

    # ── routerAction short-circuit (v3.1) ───────────────────────────────────
    # HTML direct-SP calls send routerAction=True + mode in chatInput with no
    # message text.  Detect this here before message-pattern matching so we
    # never fall through to the AI Agent (which needs Ollama / OpenAI).
    _SKIP = {'routerAction', 'message', 'sessionId', 'chatInput',
             'originalBody', 'webhookUrl', 'executionMode',
             'currentMessage', 'chatHistory'}
    if chat_input.get('routerAction') and chat_input.get('mode'):
        _params = {k: v for k, v in chat_input.items()
                   if k not in _SKIP and v is not None}
        logger.info(f'→ routerAction SHORT-CIRCUIT: mode={_params.get("mode")}')
        return {'router_action': True, 'params': _params}

    # ── Executive questions (CEO / CFO / VP bank) ─────────────────────────────
    # Interrogative phrasings route to the shared executive Q&A layer with the
    # decision-grade format. Imperative commands ("Show overdue activities")
    # keep their deterministic routes below.
    _is_exec_q = raw.rstrip().endswith('?') or bool(re.match(
        r'^(?:are|what|which|how|do|does|where|when|who|why|if)\b|^show\s+audit',
        msg))
    if _is_exec_q:
        try:
            from app.agents.orchestrator.executive import match_exec_question
            _exec = match_exec_question(raw)
        except Exception:
            _exec = None
        if _exec:
            _sections, _note = _exec
            logger.info(f'[executive] sections={_sections}')
            return _routed({'mode': 'executive_question',
                            'sections': _sections, 'note': _note})

    # ── "list activities:" ───────────────────────────────────────────────────
    if msg.startswith('list activities:'):
        include_completed = chat_input.get('includeCompleted')
        if include_completed is None:
            include_completed = True
        only_completed = _val(chat_input.get('onlyCompleted'))
        return _routed({
            'mode':             'list',
            'pageNumber':       _val(chat_input.get('pageNumber')) or 1,
            'pageSize':         _val(chat_input.get('pageSize'))   or 50,
            'search':           _val(chat_input.get('search')),
            'includeCompleted': include_completed,
            'onlyCompleted':    only_completed,
        })

    # ── "overdue activities:" ────────────────────────────────────────────────
    if msg.startswith('overdue activities:'):
        return _routed({'mode': 'overdue'})

    # ── "upcoming activities:" ───────────────────────────────────────────────
    if msg.startswith('upcoming activities:'):
        return _routed({'mode': 'upcoming'})

    # ── "activity summary:" ──────────────────────────────────────────────────
    if msg.startswith('activity summary:'):
        return _routed({'mode': 'summary'})

    # ── "activity timeline:" ─────────────────────────────────────────────────
    if msg.startswith('activity timeline:'):
        related_type = _val(chat_input.get('relatedType'))
        related_id   = _val(chat_input.get('relatedId'))
        if related_type and related_id:
            logger.info(f'[timeline] relatedType={related_type} relatedId={related_id}')
            return _routed({'mode': 'timeline', 'relatedType': related_type, 'relatedId': related_id})
        # No entity provided — passthru so the AI Agent can ask for a name
        logger.warning('[timeline] missing relatedType/relatedId — passthru to AI Agent')
        return _passthru(raw, chat_input)

    # ── "get activity:" ──────────────────────────────────────────────────────
    if msg.startswith('get activity:'):
        activity_id = _val(chat_input.get('activityId')) or _extract_uuid(raw)
        if activity_id:
            return _routed({'mode': 'get', 'activityId': activity_id})
        logger.warning('[get] no activityId — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "create activity:" ───────────────────────────────────────────────────
    if msg.startswith('create activity:'):
        if not (chat_input.get('relatedType') and chat_input.get('relatedId') and chat_input.get('type')):
            logger.warning('[create] Missing required fields — falling through to AI Agent')
            return _passthru(raw, chat_input)
        logger.info(f"[create] type={chat_input.get('type')} relatedType={chat_input.get('relatedType')}")
        return _routed({
            'mode':        'create',
            'relatedType': _val(chat_input.get('relatedType')),
            'relatedId':   _val(chat_input.get('relatedId')),
            'type':        _val(chat_input.get('type')),
            'subject':     _val(chat_input.get('subject')),
            'description': _val(chat_input.get('description')),
            'dueDate':     _val(chat_input.get('dueDate')),
            'direction':   _val(chat_input.get('direction')),
            'channel':     _val(chat_input.get('channel')),
            'ownerId':     _val(chat_input.get('ownerId')),
        })

    # ── "update activity:" ───────────────────────────────────────────────────
    if msg.startswith('update activity:'):
        activity_id = _val(chat_input.get('activityId'))
        if not activity_id:
            logger.warning('[update] Missing activityId — falling through to AI Agent')
            return _passthru(raw, chat_input)
        # completedAt may be explicitly None (reopen/clear) — preserve that distinction
        completed_at = chat_input.get('completedAt', None)
        return _routed({
            'mode':        'update',
            'activityId':  activity_id,
            'subject':     _val(chat_input.get('subject')),
            'description': _val(chat_input.get('description')),
            'dueDate':     _val(chat_input.get('dueDate')),
            'direction':   _val(chat_input.get('direction')),
            'channel':     _val(chat_input.get('channel')),
            'ownerId':     _val(chat_input.get('ownerId')),
            'completedAt': completed_at,
            'relatedType': _val(chat_input.get('relatedType')),
            'relatedId':   _val(chat_input.get('relatedId')),
        })

    # ── "complete activity:" ─────────────────────────────────────────────────
    if msg.startswith('complete activity:'):
        activity_id = _val(chat_input.get('activityId')) or _extract_uuid(raw)
        if activity_id:
            return _routed({'mode': 'complete', 'activityId': activity_id})
        logger.warning('[complete] no activityId — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "reopen activity:" ───────────────────────────────────────────────────
    if msg.startswith('reopen activity:'):
        activity_id = _val(chat_input.get('activityId')) or _extract_uuid(raw)
        if activity_id:
            return _routed({'mode': 'reopen', 'activityId': activity_id})
        logger.warning('[reopen] no activityId — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "delete activity:" ───────────────────────────────────────────────────
    if msg.startswith('delete activity:'):
        activity_id = _val(chat_input.get('activityId')) or _extract_uuid(raw)
        if activity_id:
            return _routed({'mode': 'delete', 'activityId': activity_id})
        logger.warning('[delete] no activityId — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "get owners:" ────────────────────────────────────────────────────────
    if msg.startswith('get owners:'):
        logger.info('[get_owners] direct route — returning active owner list')
        return _routed({'mode': 'get_owners'})

    # "list/show/get [active] [activity] owners" — NL shortcut for get_owners
    if re.match(r'^(?:get|list|show)\s+(?:active\s+)?(?:activity\s+)?owners?\b', msg):
        logger.info('[NL] → get_owners shortcut')
        return _routed({'mode': 'get_owners'})

    # ── Natural-language shortcuts ────────────────────────────────────────────
    # Catch common typed/voice queries before they reach the AI Agent. Specific
    # intents (writes, timelines, searches) are matched first; the master list
    # parser below combines type / status / name / date / page filters so
    # combo queries ("show pending calls for Bob Brown") stay deterministic.

    # Vague complete/reopen/delete (no activity ID) → AI asks which activity.
    # Must come before the completed-list shortcut, which used to swallow
    # "Complete activity" and wrongly return the completed list.
    if re.match(r'^(?:complete|finish|close|reopen|delete)\s+(?:an?\s+)?activit(?:y|ies)\s*$', msg):
        logger.info('[NL] vague complete/reopen/delete — passthru so AI asks for the activity')
        return _passthru(raw, chat_input)

    # "log a call for/with <name>" → log_call (graph resolves name → UUID)
    _log_m = re.match(r'^log\s+(?:a\s+)?call\s+(?:for|with|to)\s+(.+?)\s*$', msg)
    if _log_m:
        pname = raw[_log_m.start(1):_log_m.end(1)].strip().rstrip('?.!,')
        if pname:
            logger.info(f'[NL] → log_call for {pname!r}')
            return _routed({
                'mode': 'log_call', 'relatedType': 'account', 'relatedId': pname,
                'subject': f'Call with {pname}', 'direction': 'outbound', 'channel': 'phone',
            })

    # "schedule a meeting with <name> [next monday|tomorrow|YYYY-MM-DD]"
    _meet_m = re.match(
        r'^schedule\s+(?:a\s+)?meeting\s+(?:with|for)\s+(.+?)'
        r'(?:\s+(next\s+\w+|tomorrow|today|on\s+\d{4}-\d{2}-\d{2}))?\s*$',
        msg
    )
    if _meet_m:
        pname = raw[_meet_m.start(1):_meet_m.end(1)].strip().rstrip('?.!,')
        when = (_meet_m.group(2) or '').strip()
        due = _parse_due_phrase(when)
        if pname:
            logger.info(f'[NL] → schedule_meeting with {pname!r} due {due}')
            return _routed({
                'mode': 'schedule_meeting', 'relatedType': 'account', 'relatedId': pname,
                'subject': f'Meeting with {pname}', 'dueDate': due, 'channel': 'video',
            })

    # "search/find activities for <term>"
    _search_m = re.match(
        r'^(?:search|find)\s+activit(?:y|ies)\s+(?:for|about|containing|with|mentioning)\s+(.+?)\s*$',
        msg
    )
    if _search_m:
        term = raw[_search_m.start(1):_search_m.end(1)].strip().rstrip('?.!,')
        if term:
            logger.info(f'[NL] → list search={term!r}')
            return _routed({'mode': 'list', 'search': term,
                            'pageNumber': 1, 'pageSize': 50})

    # "timeline for [account|contact|lead|opportunity] <name>"
    _tl_m = re.search(
        r'\btimeline\s+(?:for|of)\s+(?:(account|contact|lead|opportunity)\s+)?(.+?)\s*$',
        msg
    )
    if _tl_m:
        pname = raw[_tl_m.start(2):_tl_m.end(2)].strip().rstrip('?.!,')
        if pname:
            rtype = _tl_m.group(1) or 'account'
            logger.info(f'[NL] → timeline {rtype} {pname!r}')
            return _routed({'mode': 'timeline', 'relatedType': rtype, 'relatedId': pname})

    # "activities related to orders/invoices/payments/opportunities/leads…"
    _rel_m = re.search(
        r'\bactivit(?:y|ies)\s+(?:related\s+to|linked\s+to|attached\s+to)\s+'
        r'(orders?|invoices?|payments?|opportunit(?:y|ies)|leads?|contacts?|accounts?|cases?)\b',
        msg
    )
    if _rel_m:
        _rel_word = _rel_m.group(1)
        _rel_map = {'orders': 'order', 'order': 'order', 'invoices': 'invoice', 'invoice': 'invoice',
                    'payments': 'payment', 'payment': 'payment', 'opportunities': 'opportunity',
                    'opportunity': 'opportunity', 'leads': 'lead', 'lead': 'lead',
                    'contacts': 'contact', 'contact': 'contact', 'accounts': 'account',
                    'account': 'account', 'cases': 'case', 'case': 'case'}
        rel_type = _rel_map.get(_rel_word, _rel_word.rstrip('s'))
        logger.info(f'[NL] → list relatedType={rel_type}')
        return _routed({'mode': 'list', 'relatedType': rel_type,
                        'pageNumber': 1, 'pageSize': 50})

    # "activity summary" / "activity status summary" / "activity counts by type"
    if re.search(r'\bactivit(?:y|ies)\s+(?:status\s+)?summary\b', msg) \
       or re.search(r'\bsummary\s+of\s+activit', msg) \
       or re.search(r'\bactivity\s+counts?\b', msg):
        logger.info('[NL] → summary shortcut')
        return _routed({'mode': 'summary'})

    # ── Master NL list parser — combinable filters ───────────────────────────
    # Write-style verbs (create/update/log/…) are excluded: those flow to the
    # AI Agent, which collects the required fields conversationally.
    if (re.search(r'\bactivit(?:y|ies)\b', msg)
            or re.search(r'\b(?:calls?|meetings?|tasks?|emails?|notes?|sms|voip|systems?)\b', msg)) \
       and not re.match(r'^(?:create|update|edit|modify|add|log|schedule|mark|record)\b', msg):

        _type_m   = re.search(r'\b(call|meeting|task|email|note|sms|voip|system)s?\b', msg)
        _page_m   = re.search(r'\bpage\s+(\d+)\b', msg)
        _pending  = re.search(r'\b(?:pending|incomplete|not\s+completed?|open)\b', msg)
        _completd = re.search(r'\b(?:completed?|finished|done|closed)\b', msg)
        _overdue  = re.search(r'\b(?:overdue|past\s+due|late)\b', msg)
        _upcoming = re.search(r'\b(?:upcoming|scheduled|future)\b', msg)

        # Trailing proper-noun name: "… for Bob Brown", "… for Ibotta, Inc"
        _name_m = re.search(
            r'\b(?:for|with)\s+([A-Z][\w&.\'-]*(?:[,]?\s+[A-Z][\w&.\'-]*)*)\s*$', raw)
        _name = _name_m.group(1).strip().rstrip('?.!,') if _name_m else None

        # Date windows
        _date_from = _date_to = None
        _due_from = _due_to = None
        today = date.today()
        _between_m = re.search(r'\bbetween\s+(\d{4}-\d{2}-\d{2})\s+and\s+(\d{4}-\d{2}-\d{2})\b', msg)
        _lastn_m   = re.search(r'\b(?:last|past)\s+(\d+)\s+days?\b', msg)
        _nextn_m   = re.search(r'\b(?:next|coming)\s+(\d+)\s+days?\b', msg)
        if _between_m:
            _date_from, _date_to = _between_m.group(1), _between_m.group(2)
        elif _lastn_m:
            _date_from = (today - timedelta(days=int(_lastn_m.group(1)))).isoformat()
            _date_to   = today.isoformat()
        elif re.search(r'\b(?:created\s+)?this\s+month\b', msg):
            _date_from = today.replace(day=1).isoformat()
            _date_to   = today.isoformat()
        elif re.search(r'\b(?:created\s+)?last\s+month\b', msg):
            first_this = today.replace(day=1)
            last_end   = first_this - timedelta(days=1)
            _date_from = last_end.replace(day=1).isoformat()
            _date_to   = last_end.isoformat()
        elif re.search(r'\b(?:created\s+)?last\s+week\b', msg):
            _date_from = (today - timedelta(days=today.weekday() + 7)).isoformat()
            _date_to   = (today - timedelta(days=today.weekday() + 1)).isoformat()
        # Due-date windows (upcoming mode + Python post-filter in graph)
        if re.search(r'\bdue\s+today\b', msg):
            _due_from = _due_to = today.isoformat()
        elif re.search(r'\bdue\s+tomorrow\b', msg):
            _due_from = _due_to = (today + timedelta(days=1)).isoformat()
        elif _nextn_m and re.search(r'\bdue\b', msg):
            _due_from = today.isoformat()
            _due_to   = (today + timedelta(days=int(_nextn_m.group(1)))).isoformat()

        # Mode selection
        # NOTE: overdue/upcoming SP modes cap their result set at p_page_size
        # (default 50) before Python post-filters by type/name below. For
        # "upcoming" the 14-day window naturally bounds the candidate pool to
        # well under 200 (the SP's max page size), so bumping pageSize there
        # when post-filtering guarantees the full window is searched. For
        # "overdue" the candidate pool can be in the thousands — a bump would
        # balloon the response size for little benefit (the type filter rarely
        # removes more than a handful of rows), so it's left at the default.
        if _overdue:
            params: Dict[str, Any] = {'mode': 'overdue'}
            if _type_m:
                params['typeFilter'] = _type_m.group(1)
            if _name:
                params['nameFilter'] = _name
            logger.info(f'[NL-master] → overdue {params}')
            return _routed(params)

        if _due_from or _due_to:
            params = {'mode': 'upcoming', 'dueFrom': _due_from, 'dueTo': _due_to}
            if _type_m:
                params['typeFilter'] = _type_m.group(1)
            if _name:
                params['nameFilter'] = _name
            if _type_m or _name:
                params['pageSize'] = 200
            logger.info(f'[NL-master] → upcoming due-window {params}')
            return _routed(params)

        if _upcoming or re.search(r'\bthis\s+week\b', msg):
            params = {'mode': 'upcoming'}
            if _type_m:
                params['typeFilter'] = _type_m.group(1)
            if _name:
                params['nameFilter'] = _name
            if _type_m or _name:
                params['pageSize'] = 200
            logger.info(f'[NL-master] → upcoming {params}')
            return _routed(params)

        params = {
            'mode':             'list',
            'pageNumber':       int(_page_m.group(1)) if _page_m else 1,
            'pageSize':         50,
            'includeCompleted': False if _pending else True,
        }
        if _completd and not _pending:
            params['onlyCompleted'] = True
        if _type_m:
            params['type'] = _type_m.group(1)
        if _name:
            params['search'] = _name
        if _date_from:
            params['dateFrom'] = _date_from
        if _date_to:
            params['dateTo'] = _date_to
        logger.info(f'[NL-master] → list {params}')
        return _routed(params)

    # ── Fallback: AI Agent ───────────────────────────────────────────────────
    return _passthru(raw, chat_input)


def _parse_due_phrase(when: str) -> str:
    """'next monday' / 'tomorrow' / 'today' / 'on YYYY-MM-DD' → ISO date.
    Defaults to tomorrow when no phrase is given."""
    today = date.today()
    w = (when or '').strip().lower()
    if not w or w == 'tomorrow':
        return (today + timedelta(days=1)).isoformat()
    if w == 'today':
        return today.isoformat()
    m = re.match(r'^on\s+(\d{4}-\d{2}-\d{2})$', w)
    if m:
        return m.group(1)
    m = re.match(r'^next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$', w)
    if m:
        target = ['monday', 'tuesday', 'wednesday', 'thursday',
                  'friday', 'saturday', 'sunday'].index(m.group(1))
        delta = (target - today.weekday()) % 7
        return (today + timedelta(days=delta or 7)).isoformat()
    return (today + timedelta(days=1)).isoformat()
