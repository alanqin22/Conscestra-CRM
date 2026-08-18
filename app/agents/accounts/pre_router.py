"""Account Pre-Router — Python equivalent of n8n Pre Router v3.0.

OVERVIEW
  Inspects every incoming message and either:
    • ROUTES directly to Build SQL Query (router_action = True)  — deterministic SP call
    • Passes through to the AI Agent   (router_action = False)   — free-text NL

  DESIGN PRINCIPLE: Every SP mode that accepts deterministic parameters
  MUST be handled here. The AI Agent is invoked ONLY as a last resort for
  free-form natural-language queries that cannot be structured here.

ROUTING TABLE
  "account direct: <mode>"              → mode:<mode>, params from chatInput fields
      Sent by all form submits and quick-action buttons in the HTML page.
      Valid modes: create | update | get | list | timeline | financials |
                   duplicates | summary | archive | restore | merge
  "search accounts: <query>"            → mode:'list', search:<query>
      Home-page and form typeahead boxes.
  "list accounts" / "list accounts: …"  → mode:'list'
      Bare list request or legacy deep-link.
  "get account: <uuid>"                 → mode:'get', account_id:<uuid>
      Update-form fetchAccountGetMode().
  "show/get details for account with uuid" → mode:'get', accountId:<uuid>
      Legacy detail-link pattern.
  All other messages                    → Passthru → AI Agent

CHANGELOG
  v3.0 — Added "account direct: <mode>" universal direct-route pattern.
         All form submits and button actions bypass the AI Agent entirely.
         Added "list accounts" bare-list direct route.
  v2.1 — Added "get account: <uuid>" direct route for Update Account form.
  v2.0 — Full rewrite to routerAction pattern; added account search route.
  v1.0 — Original passthru-only pre-router.
"""

import re
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)

VALID_DIRECT_MODES = {
    'create', 'update', 'get', 'list',
    'timeline', 'financials', 'duplicates', 'summary',
    'archive', 'restore', 'merge', 'list_owner',
    'list_no_orders', 'list_top_orders', 'list_top_revenue',
    'list_no_phone', 'list_overdue_invoices', 'list_min_orders',
}

# Keys injected by the pre-router itself that must NOT be forwarded to sp_accounts
_ROUTING_ONLY = {
    'sessionId', 'chatInput', 'message', 'originalBody',
    'webhookUrl', 'executionMode', 'routerAction',
    'currentMessage', 'chatHistory',
    'mode', 'routerAction',          # prevent loop-overwrite of SP params
}


def _extract_uuids(s: str) -> list:
    return UUID_RE.findall(str(s))


def _val(v: Any) -> Any:
    """Coerce empty string / None to None."""
    if v is None or v == '':
        return None
    return v


def _build_passthru_message(raw: str, chat_input: dict) -> str:
    """Append structured chatInput fields to the message — mirrors the JS passthru() helper."""
    current = raw
    if chat_input.get('city'):
        current += f", city {chat_input['city']}"
    if chat_input.get('pageSize') is not None:
        current += f", page size {chat_input['pageSize']}"
    if chat_input.get('pageNumber') is not None:
        current += f", page number {chat_input['pageNumber']}"
    if chat_input.get('customerId'):
        current += f", customer ID {chat_input['customerId']}"
    return current.strip()



def _operation_intent(raw: str):
    """Operation extraction; None on any failure so routing never breaks."""
    try:
        from app.core.operation_intent import extract
        return extract(raw)
    except Exception as exc:                                # pragma: no cover
        logger.debug(f'operation_intent unavailable: {exc}')
        return None


def route_request(message: str, chat_input: dict) -> Dict[str, Any]:
    """
    Inspect the incoming message and return a routing decision dict.

    Routed (deterministic direct SP call):
        {
            "router_action": True,
            "params": { "mode": "...", ... }
        }

    Passthru (AI Agent handles):
        {
            "router_action": False,
            "current_message": "<preprocessed message string>"
        }

    Parameters
    ----------
    message    : Raw message string from chatInput.message
    chat_input : Full chatInput dict (all structured fields from the web page)
    """
    raw = (message or '').strip()
    msg = raw.lower()

    # ── Operation preservation (Phase 7) ────────────────────────────────────
    # The noun-phrase heuristics below never consult the leading VERB, so
    # "Merge these duplicate contacts." matched the substring 'duplicate
    # contacts' and became a duplicates SEARCH, and "Archive these contacts."
    # was parsed as a person named "these contacts". The request reached the
    # right module and then quietly became a different operation.
    #
    # This runs FIRST so an explicit verb always outranks a noun match. With a
    # named target the colon-protocol handlers below still do the work; with no
    # identifiable target the request is passed through to the agent, which
    # asks which records to act on. Passing through is the point: answering a
    # different question is what this guard exists to stop.
    _op = _operation_intent(raw)
    if _op and not _op['has_target']:
        logger.info(f"[operation-preserve] {_op['operation']} requested with no "
                    f"identifiable target -> passthrough (agent will ask)")
        return {'router_action': False,
                'current_message': _build_passthru_message(raw, chat_input)}


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

    # ── Deterministic AI-summary route ───────────────────────────────────────
    # "AI summary for <name>" / "Summarize <name>" → mode=ai_summary with the
    # account name. Requires a concrete name so it never hijacks the aggregate
    # statistics mode ("account summary", "summarize accounts by industry").
    _ais_m = re.match(
        r'^(?:ai\s+summary\s+(?:of|for)\s+|summari[sz]e\s+(?:account\s+)?|brief\s+me\s+on\s+)'
        r'["\']?([A-Za-z][A-Za-z0-9&.\-\' ]{2,60}?)["\']?\s*$',
        raw, re.IGNORECASE)
    if _ais_m:
        _nm = _ais_m.group(1).strip()
        _generic = {'account', 'accounts', 'all accounts', 'the account',
                    'this account', 'summary', 'statistics'}
        if _nm.lower() not in _generic and ' by ' not in _nm.lower():
            logger.info(f'-> ROUTED: ai_summary (deterministic) account={_nm!r}')
            return {'router_action': True,
                    'params': {'mode': 'ai_summary', 'accountName': _nm}}

    logger.info('=== Account Pre-Router v3.0 ===')
    logger.info(f'Message: {raw}')

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

    # ── Firmographics focused insight (account exec-insight chips) ────────────
    # Single-topic firmographics reports → summary mode + focus. Gated on
    # "account(s)" present and NOT a top/order ranking query so it never hijacks
    # the existing list_top_revenue / list_top_orders routes below.
    if re.search(r'\baccounts?\b', msg) and not re.search(r'\btop\b|\bhighest\b|\bbest\b|\bperforming\b|\border', msg):
        _afocus = None
        if re.search(r'\benrich', msg):
            _afocus = 'enrichment'
        elif re.search(r'\benterprise\b.*\bsmb\b|\bsmb\b.*\benterprise\b|company[\s-]?size', msg):
            _afocus = 'employee_size'
        elif re.search(r'\brevenue\b', msg) and re.search(r'distribut|\bmix\b|\bband\b|\bshare\b|breakdown', msg) and '$' not in msg:
            _afocus = 'revenue'
        elif re.search(r'\bindustr', msg) and re.search(r'concentrat|\bmix\b|\bmost\b|underrepresent|breakdown|spread', msg):
            _afocus = 'industry'
        if _afocus:
            logger.info(f'[firmographics] account focus={_afocus}')
            return {'router_action': True, 'params': {'mode': 'summary', 'focus': _afocus}}

    # ── Firmographics filters: company size / revenue floor ──────────────────
    _m = re.search(r'(\d{1,5})\s*[-–]\s*(\d{1,5})\s*(?:employees|emp|staff|people|headcount)', msg)
    if _m and re.search(r'\baccount', msg):
        return {'router_action': True, 'params': {'mode': 'list',
                'employeeBand': f'{_m.group(1)}-{_m.group(2)}', 'pageSize': 20, 'pageNumber': 1}}
    _m = re.search(r'\$\s*(\d{1,4})\s*([mb])\b', msg)
    if _m and re.search(r'\brevenue\b', msg) and re.search(r'\baccount', msg) \
            and not re.search(r'\btop\b|\bhighest\b|\bbest\b', msg):
        _amt = int(_m.group(1)) * (1000 if _m.group(2).lower() == 'b' else 1)
        return {'router_action': True, 'params': {'mode': 'list',
                'revenueMin': _amt, 'sort': 'revenue_desc', 'pageSize': 20, 'pageNumber': 1}}
    if re.search(r'\b(?:with|has|have|having)\b.*\bwebsite\b', msg) and re.search(r'\baccount', msg):
        return {'router_action': True, 'params': {'mode': 'list',
                'search': 'https', 'pageSize': 20, 'pageNumber': 1}}

    # ── "duplicate account(s)/entries/records [for <name>]" → duplicates  ───
    # Must run BEFORE the executive-question check below: the shared EXEC_QA
    # bank has a generic 'duplicates?' pattern (intended for the Leads page,
    # mapped to lead_funnel) that would otherwise hijack account-duplicate
    # questions like "Are there any duplicate entries for Brooks Education?"
    # into an unrelated lead-duplicates executive answer.
    if (re.search(r'\bduplicat\w*\b', msg)
            and re.search(r'\b(?:accounts?|entries|entry|records?)\b', msg)):
        logger.info('→ ROUTED: mode=duplicates')
        return {'router_action': True, 'params': {'mode': 'duplicates'}}

    # ── Executive questions (CEO / CFO / VP bank) ─────────────────────────────
    # Interrogative phrasings route to the shared executive Q&A layer with the
    # decision-grade format. Imperative commands ("Show all accounts",
    # "Education industry accounts") keep their deterministic routes below.
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
            return {'router_action': True,
                    'params': {'mode': 'executive_question',
                               'sections': _sections, 'note': _note}}

    def routed(params: dict) -> dict:
        logger.info(
            f'→ ROUTED: mode={params.get("mode")} '
            f'{params.get("search") or params.get("account_id") or params.get("accountId") or ""}'
        )
        return {'router_action': True, 'params': params}

    def passthru() -> dict:
        current_message = _build_passthru_message(raw, chat_input)
        logger.info(f'→ PASSTHRU: AI Agent | currentMessage: {current_message}')
        return {'router_action': False, 'current_message': current_message}

    # ── "archive/restore <account name>" → archive/restore with name lookup ──
    # Without this, these fall through to the AI agent, which is unreliable —
    # it sometimes hallucinates a placeholder accountId (e.g. "uuid-here") or
    # misroutes "restore <name>" to a plain list/search. db_node resolves
    # accountName → accountId the same way it does for update/get/etc.
    _m = re.match(r'^(archive|restore)\s+(?:the\s+)?(?:account\s+)?(.+)$', raw, re.IGNORECASE)
    if _m:
        _name = _m.group(2).strip().rstrip('.!?')
        if _name:
            return routed({'mode': _m.group(1).lower(), 'accountName': _name})

    # ── "account direct: <mode>" — universal direct-route  (v3.0) ────────────
    # Sent by submitCreateAccount(), submitUpdateAccount(), sendDirectRequest(),
    # viewAccountDetails(), and all Quick Action buttons.
    # chatInput fields (account_id, account_name, billing_address, …) are
    # forwarded into params; Build SQL Query normalises snake_case → camelCase.
    if msg.startswith('account direct:'):
        operation = raw[len('account direct:'):].strip().lower()

        if operation in VALID_DIRECT_MODES:
            # Build params: mode + every extra chatInput field (excluding routing sentinels)
            params: Dict[str, Any] = {'mode': operation}
            for key, value in chat_input.items():
                if key == 'message':
                    continue              # skip the routing sentinel itself
                if key in _ROUTING_ONLY:
                    continue
                params[key] = value
            return routed(params)

        logger.warning(f'[AccountDirect] unknown operation "{operation}" — passthru')
        return passthru()

    # ── "list owners: <query>" — owner typeahead for Create/Update forms  ────
    if msg.startswith('list owners:'):
        query = raw[len('list owners:'):].strip()
        params: Dict[str, Any] = {'mode': 'list_owner', 'pageSize': 50}
        if query:
            params['search'] = query
        return routed(params)

    # ── "search accounts: <query>" — typeahead search  ───────────────────────
    # Sent by home-page and Update-form typeahead boxes.
    if msg.startswith('search accounts:'):
        query = raw[len('search accounts:'):].strip()
        page_size = int(chat_input.get('pageSize') or 20)
        page_num  = int(chat_input.get('pageNumber') or 1)

        if len(query) >= 2:
            return routed({
                'mode':       'list',
                'search':     query,
                'pageSize':   page_size,
                'pageNumber': page_num,
            })
        logger.warning(f'[AccountSearch] query too short ("{query}") — passthru')
        return passthru()

    # ── "list accounts" / "show all accounts" — bare list  ───────────────────
    if msg == 'list accounts' or msg.startswith('list accounts:'):
        page_size = int(chat_input.get('pageSize') or 20)
        page_num  = int(chat_input.get('pageNumber') or 1)
        return routed({'mode': 'list', 'pageSize': page_size, 'pageNumber': page_num})

    if re.match(r'^(show|list|display)\s+(all\s+)?accounts?\s*$', msg):
        page_size = int(chat_input.get('pageSize') or 20)
        page_num  = int(chat_input.get('pageNumber') or 1)
        return routed({'mode': 'list', 'pageSize': page_size, 'pageNumber': page_num})

    # ── "list accounts in <X> industry" → industry filter  ───────────────────
    _m = re.match(r'^(?:list|show|display)\s+accounts?\s+in\s+([\w\s]+?)\s+industry\s*$', msg)
    if _m:
        industry = _m.group(1).strip().title()
        return routed({'mode': 'list', 'industry': industry, 'pageSize': 20, 'pageNumber': 1})

    # ── "list <X> industry accounts" → industry filter  ──────────────────────
    _m = re.match(r'^(?:list|show|display)\s+([\w\s]+?)\s+industry\s+accounts?\s*$', msg)
    if _m:
        industry = _m.group(1).strip().title()
        return routed({'mode': 'list', 'industry': industry, 'pageSize': 20, 'pageNumber': 1})

    # ── "list <industry> accounts in <city>" → industry + city (multi-condition) ──
    # Routed deterministically (industry filter + city search together) so it no
    # longer depends on the AI agent, which handled it inconsistently.
    _MULTI_GENERIC = {'all', 'the', 'my', 'these', 'some', 'active', 'inactive', 'archived'}
    _m = re.match(r'^(?:list|show|display)\s+([a-z][a-z\s]*?)\s+accounts?\s+in\s+([\w\s]+?)\s*$', msg)
    if _m and _m.group(1).strip() not in _MULTI_GENERIC:
        industry = _m.group(1).strip().title()
        city     = _m.group(2).strip().title()
        return routed({'mode': 'list', 'industry': industry, 'search': city,
                       'pageSize': 20, 'pageNumber': 1})

    # ── "accounts with no/zero/without orders" → list_no_orders  ────────────
    if re.search(r'\b(no|zero|without|0)\s+orders?\b', msg):
        return routed({'mode': 'list_no_orders'})
    if re.search(r'\baccounts?\s+(that\s+)?(have\s+)?no\s+orders?\b', msg):
        return routed({'mode': 'list_no_orders'})

    # ── "accounts with no phone / missing phone number" → list_no_phone  ─────
    if re.search(r'\bno\s+phone\b|\bwithout\s+(?:a\s+)?phone\b', msg):
        return routed({'mode': 'list_no_phone'})
    if re.search(r'\bphone\s+(?:number\s+)?(?:is\s+)?(?:missing|null|empty|blank|not\s+set)\b', msg):
        return routed({'mode': 'list_no_phone'})
    if re.search(r'\bmissing\s+(?:a\s+)?phone\b|\bphone\s+(?:number\s+)?not\s+(?:set|filled|provided)\b', msg):
        return routed({'mode': 'list_no_phone'})

    # ── "accounts with overdue invoices" → list_overdue_invoices  ───────────────
    if re.search(r'\boverdue\b', msg):
        return routed({'mode': 'list_overdue_invoices'})
    if re.search(r'\binvoices?\s+(?:not\s+)?(?:unpaid|past\s+due|outstanding)\b', msg):
        return routed({'mode': 'list_overdue_invoices'})

    # ── "more than N orders / at least N orders" → list_min_orders  ──────────
    _m = re.search(r'\bmore\s+than\s+(\d+)\s+orders?\b', msg)
    if not _m:
        _m = re.search(r'\bat\s+least\s+(\d+)\s+orders?\b', msg)
    if not _m:
        _m = re.search(r'\b(\d+)\s+or\s+more\s+orders?\b', msg)
    if _m:
        return routed({'mode': 'list_min_orders', 'minOrders': int(_m.group(1)),
                       'pageSize': 20, 'pageNumber': 1})

    # ── "accounts with <x> email" → search by email/domain  ───────────────────
    # Routed deterministically (was AI-passthru, which returned 0 inconsistently).
    _m = re.search(r'\baccounts?\s+with\s+(.+?)\s+e-?mails?\b', msg)
    if _m:
        _term = _m.group(1).strip()
        if _term and _term not in ('no', 'an', 'a', 'valid', 'any', 'the', 'their'):
            return routed({'mode': 'list', 'search': _term, 'pageSize': 20, 'pageNumber': 1})

    # ── "name contains <x>" / "where name contains <x>" → search  ─────────────
    _m = re.search(r'\bname\s+contains?\s+(.+?)(?:\s*$|[.,?!])', msg)
    if _m:
        _term = _m.group(1).strip()
        if _term:
            return routed({'mode': 'list', 'search': _term, 'pageSize': 20, 'pageNumber': 1})

    # ── "top accounts by revenue / top-performing" → list_top_revenue  ────────
    if re.search(r'\b(top|most|highest|best|biggest|largest|performing)\b.*\brevenue\b', msg):
        return routed({'mode': 'list_top_revenue'})
    if re.search(r'\brevenue\b.*\b(top|most|highest|ranked|sorted|performing)\b', msg):
        return routed({'mode': 'list_top_revenue'})
    if re.search(r'\btop[\s\-]?performing\s+accounts?\b', msg):
        return routed({'mode': 'list_top_revenue'})

    # ── "top accounts by/with most orders" → list_top_orders  ────────────────
    if re.search(r'\b(top|most|highest|best|biggest|largest)\b.*\borders?\b', msg):
        return routed({'mode': 'list_top_orders'})
    if re.search(r'\borders?\b.*\b(top|most|highest|ranked|sorted)\b', msg):
        return routed({'mode': 'list_top_orders'})

    # ── "list [all] inactive/active/archived accounts [sorted/by/...]" ─────────
    # Broader than the old exact-end match: allows "all", trailing sort hints, etc.
    _m = re.search(r'\b(?:list|show|display)\b.{0,8}\b(inactive|active|archived)\s+accounts?\b', msg)
    if _m:
        return routed({'mode': 'list', 'status': _m.group(1), 'pageSize': 50, 'pageNumber': 1})

    # ── "list accounts with no orders" → passthru (AI handles)  ─────────────
    # (Kept as passthru — SP list doesn't have a zero-orders filter parameter)

    # ── "list <X> accounts" (single word X, likely industry) ─────────────────
    # Map common synonyms to a substring that ILIKE-matches the account industry
    # taxonomy (e.g. "software" → "Tech" matches "Technology"). Falls back to the
    # title-cased word for industries that already match by name.
    _ACCT_INDUSTRY = {
        'software': 'Tech', 'tech': 'Tech', 'technology': 'Tech', 'saas': 'Tech', 'it': 'Tech',
        'finance': 'Finance', 'financial': 'Finance', 'fintech': 'Finance', 'banking': 'Finance',
        'healthcare': 'Health', 'health': 'Health', 'medical': 'Health',
        'manufacturing': 'Manufactur', 'industrial': 'Manufactur',
        'marketing': 'Marketing', 'advertising': 'Marketing', 'agency': 'Marketing', 'media': 'Media',
        'retail': 'Retail', 'consulting': 'Consult', 'construction': 'Construct',
        'energy': 'Energy', 'utilities': 'Energy',
        'logistics': 'Logistics', 'transport': 'Logistics', 'transportation': 'Logistics',
        'education': 'Education', 'legal': 'Legal', 'law': 'Legal',
        'insurance': 'Insurance', 'automotive': 'Automotive', 'agriculture': 'Agricultur',
        'hospitality': 'Hospitality', 'entertainment': 'Entertainment',
    }
    _GENERIC = {'all', 'the', 'my', 'these', 'some', 'active', 'inactive', 'archived'}
    _m = re.match(r'^(?:list|show|display)\s+([a-z]+)\s+accounts?\s*$', msg)
    if _m and _m.group(1) not in _GENERIC:
        industry = _ACCT_INDUSTRY.get(_m.group(1)) or _m.group(1).strip().title()
        return routed({'mode': 'list', 'industry': industry, 'pageSize': 20, 'pageNumber': 1})

    # ── "show accounts in <city/location>" → search  ─────────────────────────
    _m = re.match(r'^(?:show|list|display)\s+accounts?\s+in\s+([\w\s]+?)\s*$', msg)
    if _m:
        location = _m.group(1).strip().title()
        return routed({'mode': 'list', 'search': location, 'pageSize': 20, 'pageNumber': 1})

    # ── "get account: <uuid>" — Update-form fetchAccountGetMode()  (v2.1) ─────
    if re.match(r'^get account:\s+', raw, re.IGNORECASE):
        explicit_id = (_val(chat_input.get('account_id')) or '').strip()
        parsed_ids  = _extract_uuids(raw)
        account_id  = explicit_id or (parsed_ids[0] if parsed_ids else '')

        if account_id:
            logger.info(f'[AccountGet] accountId: {account_id}')
            return routed({'mode': 'get', 'account_id': account_id})
        logger.warning('[AccountGet] no UUID found — passthru')
        return passthru()

    # ── Legacy "show/get details for account with uuid"  ─────────────────────
    if (msg.startswith('show details for account with uuid') or
            msg.startswith('get details for account with uuid')):
        ids = _extract_uuids(raw)
        if ids:
            return routed({'mode': 'get', 'accountId': ids[0]})

    # ── Vague UI-form intents → emit a marker so the frontend opens the
    # inline form instead of bouncing to the AI for a list of required
    # fields. Each detector skips itself when a UUID is present.
    _has_uuid = bool(_extract_uuids(raw))

    # ── "timeline / activity timeline for <name>" → timeline  ────────────────
    _m = re.search(r'\btimeline\b.{0,30}\bfor\b\s+(.+?)(?:\s*$|[.,?!])', msg)
    if not _m:
        _m = re.search(r'\bactivit(?:y|ies)\b.{0,30}\bfor\b\s+(.+?)(?:\s*$|[.,?!])', msg)
    if _m and not _has_uuid:
        _name = raw[_m.start(1):_m.end(1)].strip()
        if len(_name) > 2:
            return routed({'mode': 'timeline', 'accountName': _name})

    # ── "orders for/does <name> have" → financials  ───────────────────────────
    _m = re.search(r'\borders?\s+does\s+(.+?)\s+have\b', msg)
    if not _m:
        _m = re.search(r'\borders?\s+(?:for|of)\s+(.+?)(?:\s*$|[.,?!])', msg)
    if _m and not _has_uuid:
        _name = raw[_m.start(1):_m.end(1)].strip()
        if len(_name) > 2:
            return routed({'mode': 'financials', 'accountName': _name})

    # ── "balance / financials for <name>" → financials  ───────────────────────
    _m = re.search(r'\bbalance\s+(?:for|of)\s+(.+?)(?:\s*$|[.,?!])', msg)
    if not _m:
        _m = re.search(r'\bfinancials?\b.{0,15}\b(?:for|of)\b\s+(.+?)(?:\s*$|[.,?!])', msg)
    if _m and not _has_uuid:
        _name = raw[_m.start(1):_m.end(1)].strip()
        if len(_name) > 2:
            return routed({'mode': 'financials', 'accountName': _name})

    # ── "show/get details for <name>" → get (360 view)  ───────────────────────
    _m = re.search(r'\bdetails?\b\s+for\s+(.+?)(?:\s*$|[.,?!])', msg)
    if _m and not _has_uuid:
        _name = raw[_m.start(1):_m.end(1)].strip()
        if len(_name) > 2:
            return routed({'mode': 'get', 'accountName': _name})

    # ── "show/list contacts for <name>" → get (360 view includes contacts)  ───
    _m = re.search(r'\bcontacts?\s+(?:for|of)\s+(.+?)(?:\s*$|[.,?!])', msg)
    if not _m:
        _m = re.search(r'\bfor\b\s+(.+?)\s+\bcontacts?\b(?:\s|$)', msg)
    if _m and not _has_uuid:
        _name = raw[_m.start(1):_m.end(1)].strip()
        if len(_name) > 2:
            return routed({'mode': 'get', 'accountName': _name})

    # Create account (no UUID).
    if not _has_uuid and (
        re.search(r'\b(create|new|add|make)\b.*\baccount', msg)
        or re.search(r'\bcreate\s+or\s+update\s+account', msg)
        or re.match(r'^\s*(create|new|add)\s+account', msg)
    ):
        return routed({'mode': 'show_account_form'})

    # Update account (no UUID) — the Update Account form has its own
    # built-in search bar so the user can pick which account to edit.
    if not _has_uuid and re.search(r'\b(?:update|change|set|modify)\b.*\baccount', msg):
        return routed({'mode': 'show_account_update_form'})

    # ── "update/change <field> for <name> to <value>" → direct field update  ──
    # Handles: "update phone for Brooks Education to 416-555-0100"
    #          "change industry for Apex Solutions to Technology"
    # Must come BEFORE the broad ^(update|change)\s+(.+)$ catch-all below.
    _FIELD_MAP = {
        'phone': 'phone', 'phone number': 'phone',
        'email': 'email', 'email address': 'email',
        'website': 'website',
        'industry': 'industry',
        'status': 'status',
        'type': 'type',
    }
    # Value terminator: end-of-string, or [,?!], or a "." that is itself
    # followed by whitespace/end (sentence-ending period). A bare "." that is
    # part of the value (e.g. "apexsolutions.com", "info@acme.ca") must NOT
    # terminate the match — otherwise domain suffixes get truncated.
    _fupd_m = re.search(
        r'\b(?:update|change|set|modify)\b\s+(?:the\s+)?'
        r'(phone(?:\s+number)?|email(?:\s+address)?|website|industry|status|type)'
        r'\s+(?:for|of)\s+(.+?)\s+to\s+(.+?)(?:\s*$|[,?!]|\.(?:\s|$))',
        msg,
    )
    if _fupd_m and not _has_uuid:
        _sp_key = _FIELD_MAP.get(_fupd_m.group(1))
        if _sp_key:
            _acct_name = raw[_fupd_m.start(2):_fupd_m.end(2)].strip()
            _new_val   = raw[_fupd_m.start(3):_fupd_m.end(3)].strip()
            if len(_acct_name) > 2 and _new_val:
                return routed({'mode': 'update', 'accountName': _acct_name, _sp_key: _new_val})

    # ── "update/change <name>" without "account" keyword — open Update form ───
    if not _has_uuid:
        _upd_m = re.match(r'^(?:update|change)\s+(.+)$', msg)
        if _upd_m:
            hint = _upd_m.group(1).strip()
            if hint and 'account' not in hint.lower():
                kw_len = len(_upd_m.group(0)) - len(_upd_m.group(1))
                return routed({'mode': 'show_account_update_form', 'hint_name': raw[kw_len:].strip()})

    # ── No match — AI Agent handles  ─────────────────────────────────────────
    return passthru()
