"""Opportunity Pre-Router v3.1 — Python conversion of n8n 'Code in JavaScript' node.

PRIMARY RULE: Every request with a known mode or recognisable natural-language
pattern is routed DIRECTLY to the SQL builder (router_action=True).
The AI Agent is called ONLY when no deterministic path exists.

SUPPORTED DIRECT MODES (always bypass AI):
  list, get, create, update, delete
  add_product, update_product, remove_product
  pipeline, forecast
  search_accounts, search_products        ← typeahead modes
  search_opportunities                    ← typeahead mode (v3)
  get_owners                              ← owner dropdown (v3.1)

NL COMMAND PATTERNS (v3):
  show_details:<UUID>       → { mode: 'get',    opportunity_id: UUID }
  update_opportunity:<UUID> → { mode: 'get',    opportunity_id: UUID }
  pipeline / sales pipeline → { mode: 'pipeline' }
  forecast                  → { mode: 'forecast' }
  list/show/display ... opportunities → { mode: 'list', ... }
  search/find ... opportunities       → { mode: 'list', search: ... }

ROUTING DECISION:
  router_action=True  → skip AI, send directly to SQL builder
  router_action=False → send to AI Agent
"""

from __future__ import annotations

import re
import logging
import calendar
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# All modes the SP can handle directly
# ---------------------------------------------------------------------------
DIRECT_MODES = {
    'list', 'get', 'create', 'update', 'delete',
    'add_product', 'update_product', 'remove_product',
    'pipeline', 'forecast', 'win_rate',
    'search_accounts', 'search_products',
    'search_opportunities',
    'get_owners',
}

# SP parameter keys (snake_case, no p_ prefix).
# Anything NOT in this set is routing metadata and must be stripped.
SP_PARAMS = {
    'mode',
    'opportunity_id', 'account_id', 'contact_id',
    'name', 'amount', 'stage', 'probability', 'close_date',
    'description', 'lead_source', 'owner_id', 'status',
    'product_id', 'quantity', 'selling_price', 'discount', 'opp_product_id',
    'payload', 'created_by', 'updated_by',
    'page_size', 'page_number', 'search', 'date_from', 'date_to',
    'min_probability', 'max_probability',
    'min_amount', 'max_amount', 'sort_by',
}

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)

STAGE_MAP = {
    'prospecting':   re.compile(r'\bprospecting\b', re.IGNORECASE),
    'qualification': re.compile(r'\bqualification\b', re.IGNORECASE),
    'proposal':      re.compile(r'\bproposal\b', re.IGNORECASE),
    'negotiation':   re.compile(r'\bnegotiation\b', re.IGNORECASE),
    'closed_won':    re.compile(r'\bclosed.?won\b', re.IGNORECASE),
    'closed_lost':   re.compile(r'\bclosed.?lost\b', re.IGNORECASE),
    'closed_paid':   re.compile(r'\bclosed.?paid\b', re.IGNORECASE),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_sp_params(obj: dict) -> dict:
    """Keep only keys recognised by sp_opportunities; drop routing metadata."""
    return {
        k: v for k, v in obj.items()
        if k in SP_PARAMS and v is not None and v != ''
    }


def _routed(params: dict) -> dict:
    logger.info(f"→ ROUTED: mode={params.get('mode')}")
    return {'router_action': True, 'params': params}


def _passthru(message: str) -> dict:
    logger.info(f"→ PASSTHRU: AI Agent  msg={message[:80]!r}")
    return {'router_action': False}


# ---------------------------------------------------------------------------
# Amount filter helpers — "over $50000", "under $10k", "between $50k and $100k"
# ---------------------------------------------------------------------------
_AMT_RE = r'\$?\s*([\d,]+(?:\.\d+)?)\s*([kKmM]?)\b'


def _amt_to_num(num_str: str, suffix: str) -> float:
    val = float(num_str.replace(',', ''))
    if suffix.lower() == 'k':
        val *= 1_000
    elif suffix.lower() == 'm':
        val *= 1_000_000
    return val


def _extract_amount_filters(msg: str, params: dict) -> None:
    """Detect amount-range phrases and add min_amount/max_amount to params."""
    between_m = re.search(
        rf'\bbetween\s+{_AMT_RE}\s*(?:and|-|to)\s+{_AMT_RE}',
        msg, re.IGNORECASE
    )
    if between_m:
        v1 = _amt_to_num(between_m.group(1), between_m.group(2))
        v2 = _amt_to_num(between_m.group(3), between_m.group(4))
        params['min_amount'] = min(v1, v2)
        params['max_amount'] = max(v1, v2)
        return

    min_m = re.search(
        rf'\b(?:over|above|more than|greater than|at least|exceeding)\s+{_AMT_RE}',
        msg, re.IGNORECASE
    )
    if min_m:
        params['min_amount'] = _amt_to_num(min_m.group(1), min_m.group(2))

    max_m = re.search(
        rf'\b(?:under|below|less than|at most|up to)\s+{_AMT_RE}',
        msg, re.IGNORECASE
    )
    if max_m:
        params['max_amount'] = _amt_to_num(max_m.group(1), max_m.group(2))


# ---------------------------------------------------------------------------
# Sort/ranking helpers — "top 10 ... by value", "highest value", "smallest deals"
# ---------------------------------------------------------------------------
def _extract_sort_by(msg: str, params: dict) -> None:
    """Detect ranking/sort phrases and add sort_by to params."""
    if re.search(r'\b(highest|largest|biggest|greatest)\b.*\b(value|amount|deal|deals)\b', msg) \
            or re.search(r'\bby\s+(value|amount)\b', msg) \
            or re.search(r'\btop\b.*\b(value|amount|deal|deals)\b', msg):
        params['sort_by'] = 'amount_desc'
    elif re.search(r'\b(lowest|smallest)\b.*\b(value|amount|deal|deals)\b', msg):
        params['sort_by'] = 'amount_asc'
    elif re.search(r'\bhighest\s+probability\b', msg):
        params['sort_by'] = 'probability_desc'
    elif re.search(r'\b(closing soonest|soonest to close|closest close date)\b', msg):
        params['sort_by'] = 'close_date_asc'


# ---------------------------------------------------------------------------
# Relative date-range helpers — "this month", "this quarter", "next quarter"...
# ---------------------------------------------------------------------------
def _month_bounds(d: date, offset: int = 0) -> tuple:
    total = d.year * 12 + (d.month - 1) + offset
    year, month0 = divmod(total, 12)
    month = month0 + 1
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def _quarter_bounds(d: date, offset: int = 0) -> tuple:
    q = (d.month - 1) // 3 + offset
    year = d.year + q // 4
    q %= 4
    start_month = q * 3 + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    end = date(year, end_month, calendar.monthrange(year, end_month)[1])
    return start, end


_REL_DATE_RE = re.compile(r'\b(this|next|last)\s+(week|month|quarter|year)\b', re.IGNORECASE)


def _extract_date_range(msg: str, params: dict) -> None:
    """Detect 'this/next/last week|month|quarter|year' and set date_from/date_to
    (applied to close_date by the SP's list-mode filters)."""
    if 'date_from' in params or 'date_to' in params:
        return
    m = _REL_DATE_RE.search(msg)
    if not m:
        return
    rel  = m.group(1).lower()
    unit = m.group(2).lower()
    offset = {'this': 0, 'next': 1, 'last': -1}[rel]
    today = date.today()

    if unit == 'month':
        start, end = _month_bounds(today, offset)
    elif unit == 'quarter':
        start, end = _quarter_bounds(today, offset)
    elif unit == 'week':
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        start, end = monday, monday + timedelta(days=6)
    else:  # year
        year = today.year + offset
        start, end = date(year, 1, 1), date(year, 12, 31)

    params['date_from'] = start.isoformat()
    params['date_to']   = end.isoformat()


# Generic words that can follow "opportunities by/for/from/of" but are NOT
# account/contact names — e.g. "top 10 opportunities by value".
_NON_NAME_TERMS = {
    'value', 'amount', 'amounts', 'price', 'date', 'dates', 'name', 'names',
    'size', 'stage', 'stages', 'probability', 'owner', 'owners', 'source',
    'sources', 'status', 'rating', 'score', 'margin', 'weight', 'weighted',
}

# Known lead-source values (lowercased) — when a message says "opportunities
# from <X>" and X is one of these, route to a dedicated lead_source= filter
# (exact match in the SP) instead of a fuzzy name search. Anything else after
# "from" (e.g. an account name) still falls through to the search path.
LEAD_SOURCES = {
    'website', 'referral', 'cold call', 'trade show', 'email campaign',
    'partner', 'partner program', 'webinar', 'paid ads', 'event',
    'cold outreach', 'newsletter', 'google ads', 'linkedin',
}


# ---------------------------------------------------------------------------
# Natural-language trigger patterns
# Order matters — first match wins.
# ---------------------------------------------------------------------------

def _match_nl(message: str) -> Optional[dict]:
    """
    Test NL trigger patterns against message.
    Returns params dict if matched, else None.
    """
    raw = message.strip()
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
        # _match_nl returns bare params — the caller wraps them in the
        # routed envelope, unlike the other agents' route_request functions.
        return {'mode': 'web_search', 'query': _q}

    # ── Deterministic AI-summary route (bare params — caller wraps) ─────────
    # "Summarize <deal>" / "AI summary for <deal>" → mode=ai_summary.
    _ais_m = re.match(
        r'^(?:ai\s+summary\s+(?:of|for)\s+|summari[sz]e\s+)(?:the\s+)?'
        r'(?:deal\s+|opp(?:ortunity)?\s+)?["\']?(.+?)["\']?\s*$',
        raw, re.IGNORECASE)
    if _ais_m:
        _nm = _ais_m.group(1).strip()
        _generic = {'opportunity', 'opportunities', 'all opportunities', 'deal',
                    'deals', 'all deals', 'pipeline', 'summary', 'statistics'}
        if _nm.lower() not in _generic and ' by ' not in _nm.lower():
            logger.info(f'-> ROUTED: ai_summary (deterministic) opp={_nm!r}')
            return {'mode': 'ai_summary', 'opportunityName': _nm}

    # show_details:<UUID>
    m = re.match(r'^show_details:([0-9a-f-]{36})$', raw, re.IGNORECASE)
    if m:
        return {'mode': 'get', 'opportunity_id': m.group(1)}

    # update_opportunity:<UUID> — pre-load form (fetch details, don't update yet)
    m = re.match(r'^update_opportunity:([0-9a-f-]{36})$', raw, re.IGNORECASE)
    if m:
        return {'mode': 'get', 'opportunity_id': m.group(1)}

    # ── Vague UI-form intents → emit a marker so the frontend opens the
    # inline form instead of bouncing to the AI for a list of required
    # fields. Each detector skips itself when the user already supplied a
    # UUID (so the SP can act directly).
    #
    # ORDER MATTERS: the product-specific detectors run FIRST so phrases
    # like "add product to opportunity" don't get swallowed by the looser
    # "add … opportunity" pattern in the create-opportunity detector below.
    _has_uuid = bool(re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', raw, re.IGNORECASE))

    # Add product to opportunity (no UUID) — opens the opportunity search bar.
    if not _has_uuid and re.search(r'\b(add|attach|insert)\b.*\bproduct', msg):
        return {'mode': 'show_opportunity_add_product_form'}

    # Update product on opportunity (no opp_product_id) — opens the search bar.
    if not _has_uuid and re.search(r'\bupdate\b.*\bproduct', msg):
        return {'mode': 'show_opportunity_update_product_form'}

    # Create opportunity — covers "create", "new", "add", "make", and the
    # combined "create or update opportunity". Also requires that 'product'
    # NOT appear in the message, so "add product to opportunity" is left to
    # the product detector above.
    if not _has_uuid and 'product' not in msg and (
        re.search(r'\b(create|new|add|make)\b.*\bopportunit', msg)
        or re.search(r'\bcreate\s+or\s+update\s+opportunit', msg)
        or re.match(r'^\s*(create|new|add)\s+opportunit', msg)
    ):
        return {'mode': 'show_opportunity_form'}

    # Update opportunity (no UUID, no product mention).
    if not _has_uuid and 'product' not in msg and re.search(r'\b(?:update|edit)\b.*\bopportunit', msg):
        return {'mode': 'show_opportunity_update_form'}

    # Single-section focus for the analytical reports — "pipeline by owner",
    # "forecast by lead source", "win rate by owner", "top accounts", etc. → the
    # formatter emits only that breakdown instead of the whole dashboard.
    def _detect_focus(m: str):
        if re.search(r'\bby\s+stage\b', m):                                return 'by_stage'
        if re.search(r'\bby\s+(owner|owners|rep|reps|sales\s*rep)\b', m):  return 'by_owner'
        if re.search(r'\bby\s+(account|accounts|company)\b', m):           return 'by_account'
        if re.search(r'\bby\s+(lead\s*source|sources?)\b', m):             return 'by_lead_source'
        if re.search(r'\bby\s+(close\s*)?horizon\b', m):                   return 'by_horizon'
        if re.search(r'\bby\s+(product|products)\b', m):                   return 'by_product'
        if re.search(r'\bby\s+month\b', m):                                return 'by_month'
        if re.search(r'\btop\s+accounts?\b', m):                          return 'top_accounts'
        if re.search(r'\btop\s+products?\b', m):                          return 'top_products'
        if re.search(r'\bmargin\s+health\b', m):                          return 'margin_health'
        return None

    # Pipeline / opportunity summary
    if re.search(r'\b(pipeline|sales pipeline)\b', msg) \
            or re.search(r'\bopportunit\w*\s+summary\b', msg) \
            or re.search(r'\bsummary\b.*\bopportunit', msg):
        params = {'mode': 'pipeline'}
        _f = _detect_focus(msg)
        if _f:
            params['focus'] = _f
        return params

    # Win rate / win-loss statistics
    if re.search(r'\bwin.?(rate|loss)\b', msg) or re.search(r'\bwin\s*/\s*loss\b', msg):
        params = {'mode': 'win_rate'}
        _f = _detect_focus(msg)
        if _f:
            params['focus'] = _f
        return params

    # Forecast accuracy — predicted-vs-actual by month from the monthly
    # snapshots. Checked BEFORE the plain 'forecast' route because these phrases
    # also contain the word "forecast".
    if re.search(r'\bforecast\s+(accuracy|attainment|variance|error)\b', msg) \
            or re.search(r'\b(accuracy|attainment|variance)\b.*\bforecast\b', msg) \
            or re.search(r'\bforecast\b.*\b(vs\.?|versus|against)\b.*\bactual', msg) \
            or re.search(r'\bactual\w*\b.*\b(vs\.?|versus|against)\b.*\bforecast\b', msg) \
            or re.search(r'\b(predicted|forecast\w*)\s+(vs\.?|versus)\s+(actual|real)', msg) \
            or re.search(r'\bhow\s+accurate\b.*\bforecast', msg):
        return {'mode': 'forecast_accuracy'}

    # Forecast — honour an explicit "N month" horizon by passing a close-date
    # window to the SP (which defaults to ±6 months when no dates are given).
    # Without this, "3 / 6 / 12 month forecast" all returned identical output.
    if re.search(r'\bforecast\b', msg) or re.search(r'\btop\s+accounts?\b.*\bforecast\b|\bforecast\b.*\btop\s+accounts?\b', msg):
        params = {'mode': 'forecast'}
        _f = _detect_focus(msg)
        if _f:
            params['focus'] = _f
        hm = re.search(r'\b(\d{1,2})\s*[-]?\s*month', msg)
        if hm:
            n = max(1, min(int(hm.group(1)), 36))
            today = date.today()
            total = today.year * 12 + (today.month - 1) + n
            y, m0 = divmod(total, 12)
            last = calendar.monthrange(y, m0 + 1)[1]
            params['date_from'] = today.isoformat()
            params['date_to']   = date(y, m0 + 1, min(today.day, last)).isoformat()
        return params

    # List opportunities (with optional stage filter)
    if re.search(r'\b(list|show|display|get)\b.*\bopportunit', msg):
        params: dict = {'mode': 'list', 'page_size': 50, 'page_number': 1}
        for stage, pattern in STAGE_MAP.items():
            if pattern.search(msg):
                params['stage'] = stage
                break
        # Bare "won"/"lost" (without "closed") → stage filter
        if 'stage' not in params:
            if re.search(r'\bwon\b', msg):
                params['stage'] = 'closed_won'
            elif re.search(r'\blost\b', msg):
                params['stage'] = 'closed_lost'
        # "open opportunities" → status filter (open is a status, not a stage).
        # Without this it fell through to no filter and returned ALL rows.
        if 'stage' not in params and 'status' not in params and re.search(r'\bopen\b', msg):
            params['status'] = 'open'
        # "opportunities from <known lead source>" → dedicated lead_source filter
        # (exact match) rather than a fuzzy name search that also hit name/desc.
        _src_m = re.search(r'\bfrom\s+([A-Za-z][A-Za-z &/]+?)\s*$', raw, re.IGNORECASE)
        if _src_m and _src_m.group(1).strip().lower() in LEAD_SOURCES:
            params['lead_source'] = _src_m.group(1).strip()
        # "unassigned opportunities" → owner_id IS NULL (sentinel UUID)
        if re.search(r'\bunassigned\b', msg):
            params['owner_id'] = '00000000-0000-0000-0000-000000000000'
        _extract_date_range(msg, params)
        ps = re.search(r'\bpage.?size\s+(\d+)\b', msg)
        if ps:
            params['page_size'] = int(ps.group(1))
        pg = re.search(r'\bpage\s+(\d+)\b', msg)
        if pg:
            params['page_number'] = int(pg.group(1))
        # "top N opportunities" / "top N ... by value" → page_size = N
        top_m = re.search(r'\btop\s+(\d+)\b', msg)
        if top_m:
            params['page_size'] = max(1, min(int(top_m.group(1)), 200))
            params['top_n'] = True
        _extract_amount_filters(msg, params)
        _extract_sort_by(msg, params)
        # Value-filter queries ("Deals over $50k", "Deals under $10k") default
        # to highest-value-first unless the user requested a different order.
        if ('min_amount' in params or 'max_amount' in params) and 'sort_by' not in params:
            params['sort_by'] = 'amount_desc'
        # Extract account/contact name: "for X", "from X", "of X", or proper-noun name after "opportunit*"
        _name_m = re.search(
            r'\bopportunit\w*\s+(?:(?:for|from|by|of)\s+)?([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+)',
            raw
        )
        if not _name_m:
            _name_m = re.search(
                r'\bopportunit\w*\s+(?:for|from|by|of)\s+(.+?)(?:\s+(?:in|at|this|last|by)\b.*)?$',
                raw, re.IGNORECASE
            )
        if not _name_m:
            # "details for X" / "info for X" before the word "opportunity"
            _name_m = re.search(
                r'(?:details?|info|information)\s+for\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+)',
                raw
            )
        if _name_m and 'lead_source' not in params:
            name = _name_m.group(1).strip().rstrip('?.,;')
            if name and name.lower() not in _NON_NAME_TERMS:
                params['search'] = name
        return params

    # Connective lead-ins between "opportunity" and the name the user means:
    # "…opportunity WITH ACCOUNT agentorc.ca", "…opportunities FOR Sophia".
    # Applied repeatedly (with → account → …) so stacked prepositions peel off.
    _SEARCH_LEADIN_RE = re.compile(
        r'^(?:(?:with|for|from|by|of|at|on|in|to)\s+|'
        r'(?:the|a|an)\s+|'
        r'(?:account|customer|client|company|contact|owner|named?|called?)\s+)+',
        re.IGNORECASE)

    # Search / find opportunities
    if re.search(r'\b(search|find)\b.*\bopportunit', msg):
        params = {'mode': 'list', 'page_size': 50, 'page_number': 1}

        # "named X" / "called X" — take the WHOLE name. A quoted name wins;
        # otherwise it runs to the end, minus a trailing time/place qualifier
        # ("… in Q3"). The name is non-greedy up to that qualifier, so
        # "named Big Deal" yields "big deal" rather than the first word alone.
        term = ''
        nm = re.search(
            r"(?:named?|called?)\s+(?:[\"']([^\"']+)[\"']|(.+?))"
            r"(?:\s+(?:in|at|on|during|this|last)\b.*)?$",
            msg, re.IGNORECASE)
        if nm:
            term = nm.group(1) or nm.group(2) or ''

        if not term.strip():
            # "find opportunities: David" / "find opportunities David" /
            # "find the opportunity with account agentorc.ca?"
            colon = re.search(r'opportunit\w*[:\s]+(.+)', msg, re.IGNORECASE)
            if colon:
                # The capture is greedy, so conversational phrasing lands here
                # whole: "opportunity with account agentorc.ca?" yields
                # "with account agentorc.ca?", which is then searched verbatim
                # and matches nothing. Peel the connective lead-in back to the
                # actual name.
                term = _SEARCH_LEADIN_RE.sub('', colon.group(1).strip().strip('"\''))

        # NB: no "for/from/by" fallback here. There used to be one, but the
        # greedy colon pattern above matches every phrasing it could have
        # caught (it ran first), so it was unreachable and is gone.
        term = term.strip().strip('"\'').rstrip('?.,;!').strip()
        if term:
            params['search'] = term
        return params

    return None


# ============================================================================
# MAIN ROUTER
# ============================================================================

def route_request(body: dict, chat_input: dict, session_id: str) -> dict:
    """
    Inspect the request and return a routing decision.

    Routed:   { 'router_action': True,  'params': { 'mode': ..., ... } }
    Passthru: { 'router_action': False }
    """
    logger.info('=== Opportunity Pre-Router v3.1 ===')


    # ── routerAction short-circuit (v3.1) ───────────────────────────────────
    _SKIP = {'routerAction', 'message', 'sessionId', 'chatInput',
             'originalBody', 'webhookUrl', 'executionMode',
             'currentMessage', 'chatHistory'}
    if chat_input.get('routerAction') and chat_input.get('mode'):
        _params = {k: v for k, v in chat_input.items()
                   if k not in _SKIP and v is not None}
        logger.info(f'→ routerAction SHORT-CIRCUIT: mode={_params.get("mode")}')
        return {'router_action': True, 'params': _params}
    # ── CASE 1: Structured body with a known mode ──────────────────────────
    # HTML forms and programmatic callers send { mode: 'create', ... }
    # directly in the request body.
    body_mode = str(body.get('mode') or '').lower().strip()
    if body_mode and body_mode in DIRECT_MODES:
        params = _extract_sp_params(body)
        params['mode'] = body_mode
        logger.info(f"CASE 1 – direct body mode: {body_mode}")
        return _routed(params)

    # ── CASE 2: Natural-language message matching a known pattern ──────────
    nl_message = (chat_input.get('message') or '').strip()

    # ── Executive questions (CEO / CFO / VP bank) ───────────────────────────
    # Interrogative phrasings route to the shared executive Q&A layer with
    # the decision-grade format (headline, confidence, drivers, action,
    # drill-down). Imperative commands ("Show open opportunities") keep
    # their deterministic routes below.
    _msg_l = nl_message.lower()
    _is_exec_q = nl_message.rstrip().endswith('?') or bool(re.match(
        r'^(?:are|what|which|how|do|does|where|when|who|why|if)\b|^show\s+audit',
        _msg_l))
    if _is_exec_q:
        try:
            from app.agents.orchestrator.executive import match_exec_question
            _exec = match_exec_question(nl_message)
        except Exception:
            _exec = None
        if _exec:
            _sections, _note = _exec
            logger.info(f'[executive] sections={_sections}')
            return _routed({'mode': 'executive_question',
                            'sections': _sections, 'note': _note})

    # Supplement with legacy structured fields
    if chat_input.get('city'):        nl_message += f", city {chat_input['city']}"
    if chat_input.get('pageSize'):    nl_message += f", page size {chat_input['pageSize']}"
    if chat_input.get('pageNumber'):  nl_message += f", page number {chat_input['pageNumber']}"
    if chat_input.get('customerId'):  nl_message += f", customer ID {chat_input['customerId']}"

    if nl_message:
        matched = _match_nl(nl_message)
        if matched is not None:
            logger.info(f"CASE 2 – NL pattern matched: mode={matched.get('mode')}")
            return _routed(matched)

    # ── CASE 3: Fallback — unknown NL → AI Agent ───────────────────────────
    # Generate a descriptive message for webhook modes lacking chatInput
    if not nl_message:
        bm = body.get('mode', '')
        if bm == 'add_product':
            nl_message = (
                f"Add product {body.get('product_id','?')} to opportunity "
                f"{body.get('opportunity_id','?')} qty {body.get('quantity',1)} "
                f"disc {body.get('discount',0)} price {body.get('selling_price','default')}"
            )
        elif bm == 'update_product':
            nl_message = (
                f"Update product line {body.get('opp_product_id','?')} "
                f"qty {body.get('quantity','?')} disc {body.get('discount','?')} "
                f"price {body.get('selling_price','?')}"
            )
        else:
            import json
            nl_message = json.dumps(body)

    logger.info("CASE 3 – passthru to AI Agent")
    return _passthru(nl_message)
