"""Leads Pre-Router v2.0 — Python conversion of n8n 'pre router' node.

OVERVIEW
  Inspects every incoming request and either ROUTES it directly to the SQL
  builder (router_action=True) OR passes it through to the AI Agent
  (router_action=False).

  ARCHITECTURAL PRINCIPLE:
    Deterministic SQL operations → ALWAYS routed directly (no AI)
    Ambiguous / NL queries       → AI Agent fallback

KEY TRIGGERS — all produce router_action=True
  ┌──────────────────────────────────────┬───────────────────────────────────┐
  │ Message prefix (chatInput.message)   │ SP mode                           │
  ├──────────────────────────────────────┼───────────────────────────────────┤
  │ "list leads:"                        │ list  (+page/search/status/rating)│
  │ "get lead:"                          │ get   (leadId or email)           │
  │ "create lead:"                       │ create (full form fields)         │
  │ "update lead:"                       │ update (leadId + changed fields)  │
  │ "qualify lead:"                      │ qualify (leadId + optional reason)│
  │ "convert lead:"                      │ convert (leadId)                  │
  │ "pipeline leads:"                    │ pipeline (no params)              │
  │ "get employees:"                     │ list_employee (no params)         │
  │ All other messages                   │ Passthru → AI Agent (NL)         │
  └──────────────────────────────────────┴───────────────────────────────────┘

CHANGELOG
  v2.1 — Fixed employee mode name: get_employees → list_employee (matches SP).
  v2.0 — Complete rewrite. Direct routes for all SP modes from HTML UI.
          create/update forms now bypass the AI Agent entirely.
  v1.0 — Original release (passthru / NL normalisation only).
"""

from __future__ import annotations

import re
import logging
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
    logger.info(f"→ ROUTED: mode={params.get('mode')}  params={str(params)[:250]}")
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
    logger.info('=== Leads Pre-Router v2.0 ===')
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

    # ── Firmographics focused insight (Lead AI "executive insight" chips) ─────
    # Route specific analytical firmographics questions to a single-topic report
    # (mode=pipeline + focus) so each chip yields a distinct, card-styled answer
    # instead of all returning the full pipeline dashboard. Imperative segment
    # filters ("show software leads", "$50M revenue") are NOT caught here — they
    # lack these analytical keywords and fall through to the filter routes below.
    _focus = None
    if re.search(r'\benrich', msg) and re.search(r'how many|percent|coverage|enriched|%', msg):
        _focus = 'enrichment'
    elif re.search(r'\bindustr', msg) and re.search(r'\bconver', msg):
        _focus = 'industry_conversion'
    elif re.search(r'\bindustr', msg) and re.search(r'\bscore\b|priorit', msg):
        _focus = 'industry_score'
    elif (re.search(r'\bcompany[\s-]?size\b', msg) or (re.search(r'\bsize\b', msg) and re.search(r'\bsegment\b', msg))) \
            and re.search(r'\bconver', msg):
        _focus = 'size_conversion'
    elif (re.search(r'\benterprise\b', msg) and re.search(r'\bsmb\b', msg)) \
            or re.search(r'\bcompany[\s-]?size\b', msg) \
            or (re.search(r'\bsize\b', msg) and re.search(r'\bmix\b|\bsegment\b|\benterprise\b|\bsmb\b', msg)):
        _focus = 'employee_size'
    elif re.search(r'\brevenue\b', msg) \
            and re.search(r'distribut|\bband\b|\bmix\b|\bshare\b|\blargest\b|breakdown', msg) \
            and '$' not in msg:
        _focus = 'revenue'
    elif re.search(r'\bindustr', msg) \
            and re.search(r'concentrat|\bmix\b|\bmost\b|underrepresent|target|priorit|spread|breakdown|by industr', msg):
        _focus = 'industry'
    if _focus:
        logger.info(f'[firmographics] focus={_focus}')
        return _routed({'mode': 'pipeline', 'focus': _focus})

    # ── Executive questions (CEO / CFO / VP bank) ─────────────────────────────
    # Interrogative phrasings route to the shared executive Q&A layer with the
    # decision-grade format. Imperative commands ("Show hot leads", "Show
    # leads from website") keep their deterministic routes below.
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

    # ── "list leads:" ────────────────────────────────────────────────────────
    if msg.startswith('list leads:'):
        logger.info(
            f"[list] pageNumber={chat_input.get('pageNumber')} "
            f"pageSize={chat_input.get('pageSize')} "
            f"search={chat_input.get('search')} "
            f"status={chat_input.get('status')} "
            f"rating={chat_input.get('rating')}"
        )
        return _routed({
            'mode':       'list',
            'pageNumber': _val(chat_input.get('pageNumber')) or 1,
            'pageSize':   _val(chat_input.get('pageSize'))   or 50,
            'search':     _val(chat_input.get('search')),
            'status':     _val(chat_input.get('status')),
            'rating':     _val(chat_input.get('rating')),
            'source':     _val(chat_input.get('source')),
            'city':       _val(chat_input.get('city')),
            'province':   _val(chat_input.get('province')),
            'postalCode': _val(chat_input.get('postalCode')),
            'country':    _val(chat_input.get('country')),
            'industry':     _val(chat_input.get('industry')),
            'employeeBand': _val(chat_input.get('employeeBand')),
            'revenueBand':  _val(chat_input.get('revenueBand')),
            'revenueMin':   _val(chat_input.get('revenueMin')),
        })

    # ── "get lead:" ──────────────────────────────────────────────────────────
    if msg.startswith('get lead:'):
        lead_id = _val(chat_input.get('leadId')) or _extract_uuid(raw)
        email   = _val(chat_input.get('email'))
        if lead_id or email:
            logger.info(f'[get] leadId={lead_id} email={email}')
            return _routed({'mode': 'get', 'leadId': lead_id, 'email': email})
        logger.warning('[get] no leadId or email — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "create lead:" ───────────────────────────────────────────────────────
    if msg.startswith('create lead:'):
        if not (chat_input.get('firstName') or chat_input.get('lastName')):
            logger.warning('[create] Missing firstName and lastName — falling through to AI Agent')
            return _passthru(raw, chat_input)

        logger.info(
            f"[create] firstName={chat_input.get('firstName')} "
            f"lastName={chat_input.get('lastName')} "
            f"company={chat_input.get('company')} "
            f"email={chat_input.get('email')}"
        )

        raw_score = _val(chat_input.get('score'))
        score = int(raw_score) if raw_score is not None else None

        return _routed({
            'mode':         'create',
            'firstName':    _val(chat_input.get('firstName')),
            'lastName':     _val(chat_input.get('lastName')),
            'company':      _val(chat_input.get('company')),
            'email':        _val(chat_input.get('email')),
            'phone':        _val(chat_input.get('phone')),
            'role':         _val(chat_input.get('role')),
            'source':       _val(chat_input.get('source')),
            'rating':       _val(chat_input.get('rating')),
            'score':        score,
            'addressLine1': _val(chat_input.get('addressLine1')),
            'addressLine2': _val(chat_input.get('addressLine2')),
            'city':         _val(chat_input.get('city')),
            'province':     _val(chat_input.get('province')),
            'postalCode':   _val(chat_input.get('postalCode')),
            'country':      _val(chat_input.get('country')),
            'industry':     _val(chat_input.get('industry')),
            'website':      _val(chat_input.get('website')),
            'employeeBand': _val(chat_input.get('employeeBand')),
            'revenueBand':  _val(chat_input.get('revenueBand')),
            'ownerId':      _val(chat_input.get('ownerId')),
            'createdBy':    _val(chat_input.get('createdBy')),
        })

    # ── "update lead:" ───────────────────────────────────────────────────────
    if msg.startswith('update lead:'):
        lead_id = _val(chat_input.get('leadId')) or _extract_uuid(raw)
        if not lead_id:
            logger.warning('[update] Missing leadId — falling through to AI Agent')
            return _passthru(raw, chat_input)

        logger.info(
            f"[update] leadId={lead_id} "
            f"firstName={chat_input.get('firstName')} "
            f"email={chat_input.get('email')} "
            f"city={chat_input.get('city')}"
        )

        raw_score = _val(chat_input.get('score'))
        score = int(raw_score) if raw_score is not None else None

        return _routed({
            'mode':         'update',
            'leadId':       lead_id,
            'firstName':    _val(chat_input.get('firstName')),
            'lastName':     _val(chat_input.get('lastName')),
            'company':      _val(chat_input.get('company')),
            'email':        _val(chat_input.get('email')),
            'phone':        _val(chat_input.get('phone')),
            'role':         _val(chat_input.get('role')),
            'source':       _val(chat_input.get('source')),
            'rating':       _val(chat_input.get('rating')),
            'score':        score,
            'addressLine1': _val(chat_input.get('addressLine1')),
            'addressLine2': _val(chat_input.get('addressLine2')),
            'city':         _val(chat_input.get('city')),
            'province':     _val(chat_input.get('province')),
            'postalCode':   _val(chat_input.get('postalCode')),
            'country':      _val(chat_input.get('country')),
            'industry':     _val(chat_input.get('industry')),
            'website':      _val(chat_input.get('website')),
            'employeeBand': _val(chat_input.get('employeeBand')),
            'revenueBand':  _val(chat_input.get('revenueBand')),
            'ownerId':      _val(chat_input.get('ownerId')),
            'updatedBy':    _val(chat_input.get('updatedBy')),
        })

    # ── "qualify lead:" ──────────────────────────────────────────────────────
    if msg.startswith('qualify lead:'):
        lead_id = _val(chat_input.get('leadId')) or _extract_uuid(raw)
        if lead_id:
            reason  = _val(chat_input.get('reason'))
            payload = {'reason': reason} if reason else None
            logger.info(f'[qualify] leadId={lead_id} reason={reason}')
            return _routed({'mode': 'qualify', 'leadId': lead_id, 'payload': payload})
        logger.warning('[qualify] no leadId — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "convert lead:" ──────────────────────────────────────────────────────
    if msg.startswith('convert lead:'):
        lead_id = _val(chat_input.get('leadId')) or _extract_uuid(raw)
        if lead_id:
            logger.info(f'[convert] leadId={lead_id}')
            return _routed({'mode': 'convert', 'leadId': lead_id})
        logger.warning('[convert] no leadId — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "archive lead:" ──────────────────────────────────────────────────────
    if msg.startswith('archive lead:'):
        lead_id = _val(chat_input.get('leadId')) or _extract_uuid(raw)
        if lead_id:
            logger.info(f'[archive] leadId={lead_id}')
            return _routed({'mode': 'archive', 'leadId': lead_id})
        logger.warning('[archive] no leadId — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "restore lead:" ──────────────────────────────────────────────────────
    if msg.startswith('restore lead:'):
        lead_id = _val(chat_input.get('leadId')) or _extract_uuid(raw)
        if lead_id:
            logger.info(f'[restore] leadId={lead_id}')
            return _routed({'mode': 'restore', 'leadId': lead_id})
        logger.warning('[restore] no leadId — falling through to AI Agent')
        return _passthru(raw, chat_input)

    # ── "pipeline leads:" ────────────────────────────────────────────────────
    if msg.startswith('pipeline leads:'):
        logger.info('[pipeline] direct route')
        return _routed({'mode': 'pipeline'})

    # ── "get employees:" ─────────────────────────────────────────────────────
    if msg.startswith('get employees:'):
        logger.info('[list_employee] direct route — returning active employee list')
        return _routed({'mode': 'list_employee'})

    # ── NL: bare lead list ────────────────────────────────────────────────────
    if re.match(r'^(?:show|list|display)(?:\s+me)?\s+(?:all\s+)?leads?\s*$', msg):
        return _routed({'mode': 'list', 'pageSize': 50, 'pageNumber': 1})

    # ── NL: "show/list [status] leads" — status filter ───────────────────────
    # Trailing time phrases ("this week", "today", ...) are accepted but not
    # used as a date filter — leads list has no created-date param, so the
    # status filter alone is the closest available match.
    _status_m = re.match(
        r'^(?:show|list|find|display|get)(?:\s+me)?\s+'
        r'(new|working|qualified|converted|disqualified)\s+leads?'
        r'(?:\s+(?:this\s+week|this\s+month|today|recently))?\s*$',
        msg,
    )
    if _status_m:
        return _routed({
            'mode': 'list', 'status': _status_m.group(1),
            'pageSize': 50, 'pageNumber': 1,
        })

    # ── NL: "show/list [hot/warm/cold] leads" — rating filter ─────────────────
    _rating_m = re.match(
        r'^(?:show|list|find|display|get)(?:\s+me)?\s+(hot|warm|cold)\s+leads?\s*$',
        msg,
    )
    if _rating_m:
        return _routed({
            'mode': 'list', 'rating': _rating_m.group(1),
            'pageSize': 50, 'pageNumber': 1,
        })

    # ── NL: "show archived/deleted leads" ─────────────────────────────────────
    if re.search(r'\b(?:archiv|delet)\w*\b', msg) and re.search(r'\bleads?\b', msg):
        return _routed({'mode': 'list', 'deletedOnly': True, 'pageSize': 50, 'pageNumber': 1})

    # ── NL: score filters — high ≥ 80, medium 50–79, low < 50 ────────────────
    if re.search(r'\b(?:high|top)[\s\-]?scor', msg):
        return _routed({'mode': 'list', 'scoreMin': 80, 'pageSize': 50, 'pageNumber': 1})
    if re.search(r'\b(?:medium|mid)[\s\-]?scor', msg) or re.search(r'\bscor.*\b(?:medium|mid)\b', msg):
        return _routed({'mode': 'list', 'scoreMin': 50, 'pageSize': 50, 'pageNumber': 1})
    if re.search(r'\blow[\s\-]?scor', msg) or re.search(r'\bscor.*\blow\b', msg):
        return _routed({'mode': 'list', 'scoreMin': 1, 'scoreMax': 49, 'pageSize': 50, 'pageNumber': 1})

    # ── NL: score above / below N ────────────────────────────────────────────
    _score_above_m = re.search(r'\bscor[e]?\s+(?:above|over|greater\s+than|>)\s*(\d+)', msg)
    if _score_above_m:
        return _routed({'mode': 'list', 'scoreMin': int(_score_above_m.group(1)),
                        'pageSize': 50, 'pageNumber': 1})
    _score_below_m = re.search(r'\bscor[e]?\s+(?:below|under|less\s+than|<)\s*(\d+)', msg)
    if _score_below_m:
        return _routed({'mode': 'list', 'scoreMax': int(_score_below_m.group(1)),
                        'pageSize': 50, 'pageNumber': 1})

    # ── NL: "show leads from [source]" — source filter ───────────────────────
    _KNOWN_SOURCES = {
        'website': 'website', 'referral': 'referral',
        'google ads': 'google_ads', 'google_ads': 'google_ads',
        'facebook ads': 'facebook_ads', 'facebook_ads': 'facebook_ads',
        'linkedin': 'linkedin',
        'social media': 'social_media', 'social_media': 'social_media', 'social': 'social_media',
        'email campaign': 'email_campaign', 'email_campaign': 'email_campaign',
        'cold call': 'cold_call', 'cold_call': 'cold_call',
        'trade show': 'trade_show', 'trade_show': 'trade_show',
        'advertisement': 'advertisement', 'ads': 'advertisement',
        'newsletter': 'newsletter',
        'webinar': 'webinar',
        'partner': 'partner', 'import': 'import', 'other': 'other',
    }
    _source_m = re.search(r'\bfrom\s+(?:the\s+)?([\w][\w\s]*)$', msg)
    if _source_m:
        # Strip a trailing "leads"/"lead" only; do NOT blanket-strip a trailing
        # 's' (that turned "ads" → "ad" and broke "google ads"/"facebook ads").
        _raw = re.sub(r'\s+leads?$', '', _source_m.group(1).strip()).strip()
        # Try an exact match first, then a singular fallback so plural source
        # phrasings ("referrals", "webinars") still resolve.
        _src = (_KNOWN_SOURCES.get(_raw)
                or _KNOWN_SOURCES.get(_raw.replace('_', ' '))
                or _KNOWN_SOURCES.get(_raw.rstrip('s'))
                or _KNOWN_SOURCES.get(_raw.rstrip('s').replace('_', ' ')))
        if _src:
            return _routed({
                'mode': 'list', 'source': _src,
                'pageSize': 50, 'pageNumber': 1,
            })

    # ── NL: industry filter — "show <industry> leads", "leads in <X> industry" ─
    _KNOWN_INDUSTRIES = {
        'software': 'Software', 'tech': 'Software', 'technology': 'Software', 'saas': 'Software',
        'manufacturing': 'Manufacturing', 'industrial': 'Manufacturing',
        'marketing': 'Marketing', 'advertising': 'Marketing', 'agency': 'Marketing',
        'creative': 'Marketing', 'digital': 'Marketing',
        'healthcare': 'Healthcare', 'health': 'Healthcare', 'medical': 'Healthcare',
        'financial': 'Financial', 'finance': 'Financial', 'fintech': 'Financial', 'banking': 'Financial',
        'retail': 'Retail', 'consulting': 'Consulting', 'construction': 'Construction',
        'energy': 'Energy', 'utilities': 'Energy', 'utility': 'Energy',
        'food': 'Food', 'beverage': 'Food', 'hospitality': 'Food',
        'logistics': 'Logistics', 'transportation': 'Logistics', 'transport': 'Logistics',
        'real estate': 'Real Estate', 'property': 'Real Estate',
        'education': 'Education', 'legal': 'Legal', 'law': 'Legal',
        'professional services': 'Professional Services',
    }
    _ind_m = re.search(
        r'\b(?:in|from|within)\s+(?:the\s+)?([a-z][a-z&\s]{1,30}?)\s+(?:industry|sector|vertical|space)\b',
        msg,
    )
    if not _ind_m:
        _ind_m = re.match(
            r'^(?:show|list|find|display|get)(?:\s+me)?\s+(?:all\s+)?([a-z][a-z&\s]{1,30}?)\s+leads?\s*$',
            msg,
        )
    if _ind_m:
        _ind_raw = _ind_m.group(1).strip()
        _ind = (_KNOWN_INDUSTRIES.get(_ind_raw)
                or _KNOWN_INDUSTRIES.get(_ind_raw.rstrip('s'))
                or _KNOWN_INDUSTRIES.get(_ind_raw.replace(' ', '')))
        if _ind:
            logger.info(f'[list] industry filter → {_ind}')
            return _routed({'mode': 'list', 'industry': _ind, 'pageSize': 50, 'pageNumber': 1})

    # ── NL: company size (employee band) — "leads with 51-200 employees" ─────
    _emp_m = re.search(r'(\d{1,5})\s*[-–]\s*(\d{1,5})\s*(?:employees|emp\b|staff|people|headcount)', msg)
    if _emp_m:
        return _routed({'mode': 'list', 'employeeBand': f'{_emp_m.group(1)}-{_emp_m.group(2)}',
                        'pageSize': 50, 'pageNumber': 1})

    # ── NL: revenue floor — "leads at $50M+ revenue" (>= $50M, not the band that
    #    merely contains "$50M" — '$10M-$50M' must NOT match a "$50M+" query) ───
    _rev_m = re.search(r'\$\s*(\d{1,4})\s*([mb])\b', msg)
    if _rev_m and re.search(r'\brevenue\b', msg):
        _amt = int(_rev_m.group(1)) * (1000 if _rev_m.group(2).lower() == 'b' else 1)
        return _routed({'mode': 'list', 'revenueMin': _amt, 'pageSize': 50, 'pageNumber': 1})

    # ── NL: "leads with a website (on file)" — has-website filter ────────────
    if re.search(r'\b(?:with|has|have|having)\b.*\bwebsite\b', msg) and re.search(r'\bleads?\b', msg):
        return _routed({'mode': 'list', 'search': 'https', 'pageSize': 50, 'pageNumber': 1})

    # ── NL: "show leads in <city/province>" — address filter ─────────────────
    _CA_PROV_MAP = {
        'ontario': 'ON', 'british columbia': 'BC', 'quebec': 'QC', 'alberta': 'AB',
        'manitoba': 'MB', 'saskatchewan': 'SK', 'nova scotia': 'NS', 'new brunswick': 'NB',
        'newfoundland': 'NL', 'prince edward island': 'PE', 'northwest territories': 'NT',
        'yukon': 'YT', 'nunavut': 'NU',
    }
    _in_m = re.search(r'\bin\s+([a-z][a-z\s]{1,40}?)(?:\s+leads?)?(?:,\s*([a-z]{2,}))?\s*$', msg)
    if _in_m:
        _loc  = _in_m.group(1).strip()
        _loc2 = (_in_m.group(2) or '').strip()
        _loc  = re.sub(r'\s+leads?$', '', _loc).strip()
        _prov = _CA_PROV_MAP.get(_loc) or (_loc.upper() if len(_loc) == 2 else None)
        if _prov:
            return _routed({'mode': 'list', 'province': _prov, 'pageSize': 50, 'pageNumber': 1})
        _city_val = _loc.title()
        _prov_val = _CA_PROV_MAP.get(_loc2) or (_loc2.upper() if _loc2 else None)
        _addr_params = {'mode': 'list', 'city': _city_val, 'pageSize': 50, 'pageNumber': 1}
        if _prov_val:
            _addr_params['province'] = _prov_val
        return _routed(_addr_params)

    # ── NL: "pipeline summary", "lead pipeline", "conversion rate(s)" ─────────
    if re.search(r'\bpipeline\b', msg) or re.search(r'\blead\s+summary\b', msg) \
            or re.search(r'\bconversion\s+rates?\b', msg):
        return _routed({'mode': 'pipeline'})

    # ── NL: "find duplicate leads", "duplicates report" ──────────────────────
    if re.search(r'\bduplicat', msg):
        return _routed({'mode': 'duplicates'})

    # ── NL: "show details for [name]" — auto-resolve name → lead detail ──────
    _details_m = re.match(
        r'^(?:show|get|fetch|display|find)\s+details?\s+for\s+(.+)$',
        raw, re.IGNORECASE,
    )
    if _details_m:
        name = _details_m.group(1).strip()
        if name and not UUID_RE.search(name):
            # detailsRequested flag tells db_node to auto-resolve single result → get mode
            return _routed({'mode': 'list', 'search': name, 'pageSize': 10, 'pageNumber': 1,
                            'detailsRequested': True})
        if UUID_RE.search(name):
            return _routed({'mode': 'get', 'leadId': _extract_uuid(name)})

    # ── NL: "convert lead for [name]" — look up lead, check qualification ────
    # Routes as a list search with convertRequested=True so the formatter can
    # warn if the lead is not yet qualified, instead of blindly attempting convert.
    _convert_nl_m = re.match(
        r'^convert(?:\s+lead)?\s+for\s+(.+)$',
        raw, re.IGNORECASE,
    )
    if _convert_nl_m:
        name = _convert_nl_m.group(1).strip()
        if UUID_RE.search(name):
            return _routed({'mode': 'convert', 'leadId': _extract_uuid(name)})
        if name:
            return _routed({'mode': 'list', 'search': name, 'pageSize': 5,
                            'pageNumber': 1, 'convertRequested': True})

    # ── Natural-language name search (no other prefix matched) ───────────────
    # Catches: "find Sophia", "search Smith", "show me Chen", "look up Alice"
    # Strips the verb prefix and routes as a list search so the AI never
    # receives a phrase like "find Sophia" and mistakenly includes "find" in
    # the search value.
    # EXCLUDED: any term containing a known command keyword (pipeline, duplicate,
    # summary, report, list, hot, warm, cold, status names, etc.) — those must
    # pass through to the AI so it can pick the correct mode.
    # ── NL: "find/show/list leads named [X]" — name search shortcut ──────────
    _leads_named_m = re.match(
        r'^(?:find|search|show|list|display)\s+leads?\s+(?:named?|with\s+name)\s+(.+)$',
        raw, re.IGNORECASE,
    )
    if _leads_named_m:
        term = _leads_named_m.group(1).strip()
        if term and not UUID_RE.search(term):
            logger.info(f'→ NAMED SEARCH: term={term!r}')
            return _routed({'mode': 'list', 'search': term, 'pageSize': 50, 'pageNumber': 1})

    # ── NL: "show/list leads assigned to <name>" — owner filter ─────────────
    _assigned_m = re.search(
        r'\bassigned\s+to\s+([A-Za-z][A-Za-z\s\-\.]{1,40}?)(?:\s*$|[.,?!])',
        raw, re.IGNORECASE,
    )
    if _assigned_m:
        owner_name = _assigned_m.group(1).strip()
        if len(owner_name) > 1:
            logger.info(f'→ OWNER FILTER: ownerSearch={owner_name!r}')
            return _routed({'mode': 'list', 'ownerSearch': owner_name,
                            'pageSize': 50, 'pageNumber': 1})

    _COMMAND_KEYWORDS = re.compile(
        r'\b(?:pipeline|duplicat|summary|report|statistic|convert|qualify|archiv|restor'
        r'|hot|warm|cold|new|working|qualified|converted|disqualified'
        r'|website|referral|google|facebook|linkedin|social|email|campaign'
        r'|cold\s+call|trade\s+show|advertisement|partner|import'
        r'|all leads?|lead list|lead pipeline|lead summary'
        r'|assigned\s+to)\b',
        re.IGNORECASE,
    )
    _SEARCH_VERBS = re.compile(
        r'^(?:find|search(?:\s+for)?|show(?:\s+me)?|look\s*up|fetch|get|display|list)\s+(.+)$',
        re.IGNORECASE,
    )
    m = _SEARCH_VERBS.match(raw)
    if m:
        term = m.group(1).strip()
        # Only treat as a name search if the term has no command keywords and no UUID
        if term and not UUID_RE.search(term) and not _COMMAND_KEYWORDS.search(term):
            logger.info(f'→ NL NAME SEARCH: term={term!r}')
            return _routed({'mode': 'list', 'search': term, 'pageSize': 50, 'pageNumber': 1})

    # ── Vague UI-form intents → emit a marker so the frontend opens the
    # inline form instead of bouncing to the AI for a list of required
    # fields. Each detector skips itself when the user already supplied a
    # UUID (so the SP/AI can act directly).
    _has_uuid = bool(_extract_uuid(raw))

    # Create lead (no UUID).
    if not _has_uuid and (
        re.search(r'\b(create|new|add|make)\b.*\blead', msg)
        or re.search(r'\bcreate\s+or\s+update\s+lead', msg)
        or re.match(r'^\s*(create|new|add)\s+lead', msg)
    ):
        return _routed({'mode': 'show_lead_form'})

    # Update lead (no UUID) — the Update Lead form has its own built-in
    # search bar at the top so the user can pick which lead to edit.
    if not _has_uuid and re.search(r'\b(update|edit)\b.*\blead', msg):
        return _routed({'mode': 'show_lead_update_form'})

    # ── Fallback: AI Agent ───────────────────────────────────────────────────
    return _passthru(raw, chat_input)
