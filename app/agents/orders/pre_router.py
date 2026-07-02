"""Orders Pre-Router v4.1 — Python conversion of n8n 'Pre Router' node.

OVERVIEW
  Routes every incoming request to either Build SQL Query (router_action=True)
  or the AI Agent (router_action=False).

  THREE routing paths:
    1. order_direct_operation — structured batchData from the web page (highest priority)
         • context='direct_query'  → generic params object stripped of context and sent directly
         • context='create_order'  → create mode with structured fields
         • action='batch_update'   → batch_update mode with payload JSONB
    2. Text prefix routes — deterministic NL-adjacent commands
    3. Passthru — AI Agent for everything else

DIRECT ROUTES (text prefix):
  "search accounts: <query>"              → account_search
  "search contacts: <query>"             → contact_search
  "list contacts for account <uuid>"     → contact_search (scoped)
  "get pricing for product <uuid> type X"→ get_pricing
  "list employees"                       → list_employees
  "show orders for account <uuid>"       → list (accountId filter)
  "show order <uuid>"                    → get_detail
  "get order <uuid>"                     → get_detail
  "view order <uuid>"                    → get_detail
  "find order <SO-YYYY-XXXXXX>"          → get_detail (by orderNumber)
  "find order <uuid>"                    → get_detail (by orderId)
  "show order details for order number…" → get_detail
  "show all orders" / "list orders" etc. → list
  "show deleted orders"                  → list with includeDeleted=True
  "show <status> orders"                 → list with status filter
  "sales summary"                        → sales_summary
  "account summary" / "show top spending"→ account_summary
  "category summary" / "analyze sales"   → category_summary
  "find orders for customer <name>"      → list with search
  "soft delete order <uuid>"             → update/soft_delete
  "delete order <uuid>"                  → update/soft_delete
  "restore order <uuid>"                 → update/restore
  "create order for account <uuid>"      → create
  "update order <uuid>"                  → update/update_header or change_status
  "list categories" / "get categories"   → get_category
  "list products" / "get products"       → get_product

CONTEXT FIELD:
  The 'context' field is a routing-only param injected by the web page:
    'direct_query'  — generic structured params, context stripped before routing
    'create_order'  — create mode with structured payload
    'create_add_items' — silent add-items after create (suppressed output in formatter)
    'update'        — regular batch update

CHANGELOG
  v4.1 — get_category, get_product text routes added.
  v4.0 — context='direct_query' universal direct path; many new text routes.
  v3.2 — context='create_order' structured payload.
  v3.1 — orderDate defaults to today if absent on create.
  v3.0 — Renamed trigger 'batch_update_order' → 'order_direct_operation'; context field.
  v2.0 — routerAction changed to boolean.
"""

from __future__ import annotations

import re
import calendar
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)
ORDER_NO_RE = re.compile(r'SO-\d{4}-\d{6}', re.IGNORECASE)

VALID_STATUSES = [
    'pending', 'processing', 'ready', 'invoiced',
    'shipped', 'delivered', 'completed', 'cancelled', 'refunded',
]

# List-filter pseudo-statuses: 'active'/'open' expand to the fulfilment queue
# (pending+processing+ready) in sp_orders — matches the home KPI count. These
# are valid for LIST filtering only, NOT for change_status writes, so they are
# kept out of VALID_STATUSES above.
LIST_STATUS_FILTERS = VALID_STATUSES + ['active', 'open']

UUID_PARAMS = [
    'orderId', 'accountId', 'contactId', 'productId',
    'productPricingId', 'orderItemId', 'createdBy', 'updatedBy',
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_uuid(s: Any) -> Optional[str]:
    """Strip trailing garbage (emojis, icons) from a UUID string."""
    m = UUID_RE.search(str(s or ''))
    return m.group(0) if m else None


def _sanitize_uuids(params: dict) -> dict:
    for key in UUID_PARAMS:
        if key in params and params[key]:
            cleaned = _clean_uuid(params[key])
            if cleaned:
                params[key] = cleaned
    return params


def _kv(text: str, key: str) -> Optional[str]:
    m = re.search(rf'{key}\s*=\s*["\']?([\w\-\.@]+)["\']?', text, re.IGNORECASE)
    return m.group(1) if m else None


def _uuids(text: str) -> list:
    return [m.group(0) for m in UUID_RE.finditer(text)]


def _order_numbers(text: str) -> list:
    return [m.group(0) for m in ORDER_NO_RE.finditer(text)]


def _today() -> str:
    return date.today().isoformat()


def _routed(params: dict) -> dict:
    logger.info(f"→ ROUTED: mode={params.get('mode')} action={params.get('action', '')}")
    return {'router_action': True, 'params': _sanitize_uuids(params)}


def _passthru() -> dict:
    logger.info('→ PASSTHRU: AI Agent')
    return {'router_action': False}


# ============================================================================
# MAIN ROUTER
# ============================================================================

def route_request(body: dict, chat_input: dict, session_id: str) -> dict:
    logger.info('=== Orders Pre-Router v4.1 ===')

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
    # decision-grade format. Imperative commands ("Show all orders", "Sales
    # summary") keep their deterministic routes below.
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

    # ── 1. order_direct_operation — highest priority structured path ──────────
    if raw in ('order_direct_operation', 'batch_update_order') or \
       (chat_input.get('batchData', {}) or {}).get('action') == 'batch_update':

        bd = chat_input.get('batchData') or {}
        if bd:
            # ── direct_query: generic structured params from web page ─────────
            if bd.get('context') == 'direct_query':
                sp_params = {k: v for k, v in bd.items() if k != 'context'}
                logger.info(f"[direct_query] mode={sp_params.get('mode')}")
                return _routed(sp_params)

            # ── create_order: structured create payload ───────────────────────
            if bd.get('context') == 'create_order':
                qty = bd.get('quantity')
                params: dict = {
                    'mode':             'create',
                    'accountId':        bd.get('accountId') or bd.get('account_id'),
                    'status':           bd.get('status') or 'pending',
                    'orderDate':        (bd.get('orderDate') or bd.get('order_date') or _today()).split('T')[0],
                    'createdBy':        bd.get('createdBy') or bd.get('created_by'),
                    'productId':        bd.get('productId') or bd.get('product_id'),
                    'quantity':         int(qty) if qty is not None else None,
                    'priceType':        bd.get('priceType') or bd.get('price_type') or 'Retail',
                    'productPricingId': bd.get('productPricingId') or bd.get('product_pricing_id'),
                    'contactId':        bd.get('contactId') or bd.get('contact_id'),
                    'context':          'create_order',
                }
                # Drop None values
                params = {k: v for k, v in params.items() if v is not None}
                logger.info('[create_order] structured create')
                return _routed(params)

            # ── batch_update ──────────────────────────────────────────────────
            if bd.get('action') == 'batch_update':
                return _routed({
                    'mode':      'update',
                    'action':    'batch_update',
                    'orderId':   bd.get('orderId') or bd.get('order_id'),
                    'updatedBy': bd.get('updatedBy') or bd.get('updated_by'),
                    'payload':   bd.get('payload'),
                    'context':   bd.get('context') or 'update',
                })

    # ── 2. Text prefix routes ─────────────────────────────────────────────────

    # search accounts: <query>
    if msg.startswith('search accounts:'):
        query = raw[len('search accounts:'):].strip()
        if query:
            return _routed({'mode': 'account_search', 'search': query})

    # search contacts: <query>
    if msg.startswith('search contacts:'):
        query = raw[len('search contacts:'):].strip()
        if query:
            return _routed({'mode': 'contact_search', 'search': query})

    # list contacts for account <uuid>
    if msg.startswith('list contacts for account'):
        ids = _uuids(raw)
        if ids:
            return _routed({'mode': 'contact_search', 'accountId': ids[0]})

    # get pricing for product <uuid> type <priceType>
    if msg.startswith('get pricing for product'):
        ids = _uuids(raw)
        if ids:
            pt_m = re.search(r'type\s+(Retail|Wholesale|Promo)', raw, re.IGNORECASE)
            price_type = pt_m.group(1).capitalize() if pt_m else 'Retail'
            return _routed({'mode': 'get_pricing', 'productId': ids[0], 'priceType': price_type})

    # list employees
    if msg == 'list employees' or msg.startswith('list employees '):
        return _routed({'mode': 'list_employees'})

    # show orders for account <uuid>
    if msg.startswith('show orders for account'):
        ids = _uuids(raw)
        if ids:
            return _routed({
                'mode': 'list', 'accountId': ids[0],
                'includeDeleted': False, 'sortField': 'order_date',
                'sortOrder': 'DESC', 'pageSize': 50, 'pageNumber': 1,
            })

    # show order / get order / view order <uuid> → get_detail
    if msg.startswith(('show order ', 'get order ', 'view order ')):
        ids = _uuids(raw)
        if ids:
            return _routed({'mode': 'get_detail', 'orderId': ids[0]})

    # find order <SO-number> | find order <uuid>
    if msg.startswith('find order'):
        ons = _order_numbers(raw)
        if ons:
            return _routed({'mode': 'get_detail', 'orderNumber': ons[0]})
        ids = _uuids(raw)
        if ids:
            return _routed({'mode': 'get_detail', 'orderId': ids[0]})

    # show order details for order number <SO-...>
    if msg.startswith('show order details for order number'):
        ons = _order_numbers(raw)
        if ons:
            return _routed({'mode': 'get_detail', 'orderNumber': ons[0].upper()})

    # ── any "show/view details / 360" mention of an SO-number → get_detail ──
    # "Show details for SO-2026-100202", "order 360 for SO-2026-100202".
    # Uppercased because the SP compares order_number case-sensitively.
    _det_ons = _order_numbers(raw)
    if _det_ons and (re.search(r'\b(?:details?|360|info(?:rmation)?)\b', msg)
                     or re.match(r'^(?:show|view|get|find|display)\b', msg)):
        return _routed({'mode': 'get_detail', 'orderNumber': _det_ons[0].upper()})

    # ── sales by month → sales_summary (optionally year-scoped) ──────────────
    if re.search(r'\bsales\s+by\s+month\b', msg) or re.search(r'\bmonthly\s+breakdown\b', msg):
        params = {'mode': 'sales_summary'}
        yr_m = re.search(r'\b(20\d{2})\b', raw)
        if yr_m:
            params['year'] = int(yr_m.group(1))
        return _routed(params)

    # ── change status of <SO-number|uuid> to <status> ────────────────────────
    # "Change status of SO-2026-100202 to shipped" / "Set SO-2026-100202 to
    # shipped" → deterministic update/change_status (explicit single-order
    # write; the AI previously handled this slowly and unreliably).
    _st_m = re.match(
        r'^(?:change|update|set)\s+(?:the\s+)?(?:status\s+of\s+)?(?:order\s+)?'
        r'(\S+)\s+(?:status\s+)?to\s+([a-z]+)\s*$',
        msg, re.IGNORECASE
    )
    if _st_m and _st_m.group(2).lower() in VALID_STATUSES:
        _target, _status = _st_m.group(1), _st_m.group(2).lower()
        ons = _order_numbers(_target.upper())
        ids = _uuids(_target)
        if ons or ids:
            p = {'mode': 'update', 'action': 'change_status', 'status': _status}
            if ons:
                p['orderNumber'] = ons[0]
            else:
                p['orderId'] = ids[0]
            return _routed(p)

    # ── show orders page N ────────────────────────────────────────────────────
    _page_m = re.match(
        r'^(?:please\s+)?(?:show|list|get|find|display|give|fetch)?\s*(?:me\s+)?'
        r'(?:all\s+)?(?:the\s+)?orders?,?\s+(?:on\s+)?page\s+(\d+)\s*$',
        msg, re.IGNORECASE
    )
    if _page_m:
        return _routed({
            'mode': 'list', 'includeDeleted': False,
            'sortField': 'order_date', 'sortOrder': 'DESC',
            'pageSize': 50, 'pageNumber': int(_page_m.group(1)),
        })

    # ── Orders list intents — flexible NL matching ───────────────────────────
    # Captures variations like:
    #   "show pending orders"            "please show me pending orders"
    #   "list cancelled orders"          "can you give me shipped orders"
    #   "find all orders"                "pending orders"
    # The leading "please / can you / could you" is optional, as is "me"
    # after the verb. The verb itself can be any of show/list/get/find/
    # display/give/fetch. Status orders also match without a verb at all.
    # Allow optional "the" after the verb/me, AND optional "the" before "orders",
    # so "show me all the orders" / "show me the pending orders" both match.
    _verb_prefix = (
        r'(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?'
        r'(?:show|list|get|find|display|give|fetch)\s+'
        r'(?:in\s+)?'          # handles "find IN pending orders"
        r'(?:me\s+)?(?:the\s+)?'
    )

    # orders by value/total — "top orders", "largest/biggest orders",
    # "highest value orders", "orders by value/total/amount". Sorts by the
    # order total DESC so the biggest-revenue orders lead (used by the
    # orchestrator Revenue Snapshot; also a handy ad-hoc query). The verb is
    # OPTIONAL here so bare phrasings ("largest orders") also match.
    _value_prefix = (r'(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?'
                     r'(?:(?:show|list|get|find|display|give|fetch)\s+)?'
                     r'(?:in\s+)?(?:me\s+)?(?:the\s+)?')
    if re.match(rf'^{_value_prefix}(?:top|largest|biggest|highest(?:[ -]value)?|high[ -]value)\s+orders?\b', msg, re.IGNORECASE) \
       or re.match(rf'^{_value_prefix}(?:all\s+(?:the\s+)?)?orders?\s+by\s+(?:value|total|amount|size|highest(?:\s+value)?)\b', msg, re.IGNORECASE):
        return _routed({
            'mode': 'list', 'includeDeleted': False,
            'sortField': 'total_amount', 'sortOrder': 'DESC',
            'pageSize': 50, 'pageNumber': 1,
        })

    # show all orders (or any phrasing without a status filter)
    if re.match(rf'^{_verb_prefix}all\s+(?:the\s+)?orders?\b', msg, re.IGNORECASE) \
       or re.match(rf'^{_verb_prefix}orders?\s*$', msg, re.IGNORECASE):
        return _routed({
            'mode': 'list', 'includeDeleted': False,
            'sortField': 'order_date', 'sortOrder': 'DESC',
            'pageSize': 50, 'pageNumber': 1,
        })

    # show deleted orders
    if re.match(rf'^{_verb_prefix}(?:all\s+(?:the\s+)?)?deleted\s+orders?\b', msg, re.IGNORECASE):
        return _routed({
            'mode': 'list', 'includeDeleted': True,
            'sortField': 'order_date', 'sortOrder': 'DESC',
            'pageSize': 50, 'pageNumber': 1,
        })

    # show <status> orders — verb-prefix OR bare "<status> orders"
    # Also captures "find pending orders for David Chen" → status + search
    _status_alt = '|'.join(LIST_STATUS_FILTERS)
    _status_match = (
        re.match(rf'^{_verb_prefix}(?:all\s+(?:the\s+)?)?({_status_alt})\s+orders?\b', msg, re.IGNORECASE)
        or re.match(rf'^({_status_alt})\s+orders?\b', msg, re.IGNORECASE)
    )
    if _status_match:
        s = _status_match.group(1).lower()
        params: dict = {
            'mode': 'list',
            'status': s,
            'includeDeleted': False,
            'sortField': 'order_date', 'sortOrder': 'DESC',
            'pageSize': 50, 'pageNumber': 1,
        }
        # Extract optional customer/account name that follows "orders".
        # Handles both:
        #   "find pending orders for David Chen"  (with preposition)
        #   "find pending orders David Chen"       (no preposition — name directly after orders)
        # Use `raw` (original case) so capitalised words identify proper nouns.
        _name_m = re.search(
            r'\borders?\s+(?:(?:for|from|by|of)\s+)?'        # optional preposition
            r'([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+)',       # ≥2 capitalised words = name
            raw
        )
        if not _name_m:
            # Fallback: lowercased message with explicit preposition
            _name_m = re.search(
                r'\borders?\s+(?:for|from|by|of)\s+'
                r'(.+?)(?:\s+(?:this|last|in|on|at|during)\b.*)?$',
                raw, re.IGNORECASE
            )
        if _name_m:
            name = _name_m.group(1).strip().rstrip('?.,;')
            if name:
                params['search'] = name
        return _routed(params)

    # "update an order" / "update order" / "edit an order" → open the Update form
    # Must come BEFORE the advance_statuses check so "update order" doesn't
    # get mistaken for "advance/process orders".
    if re.match(
        r'^(?:i\s+want\s+to\s+)?(?:please\s+)?'
        r'(?:update|edit|modify|change)\s+(?:an?\s+|the\s+)?orders?\s*$',
        msg, re.IGNORECASE
    ):
        return _routed({'mode': 'show_order_form'})

    # "change order status" / "update order status" (vague, no target order)
    # → open the Update form so the user picks the order + status. Must come
    # BEFORE the advance_statuses check — a vague singular phrase must never
    # trigger the bulk lifecycle advancement (which mutates dozens of orders).
    if re.match(
        r'^(?:i\s+want\s+to\s+)?(?:please\s+)?'
        r'(?:change|update|set|modify)\s+(?:an?\s+|the\s+)?order\s+status\s*$',
        msg, re.IGNORECASE
    ):
        return _routed({'mode': 'show_order_form'})

    # advance / process orders — calls fn_advance_order_statuses()
    # Requires an explicit "advance", "auto-advance", "process", "progress",
    # "move", or the PLURAL "update order statuses". The singular "update/
    # change order status" opens the Update form above instead — a vague
    # single-order intent must not trigger the bulk advancement.
    if re.search(
        r'\b(advance|auto.?advance|process|progress|move)\b.*\borders?\b'
        r'|ship\s+(?:all\s+)?ready\s+orders?'
        r'|\border\s+status(?:es)?\s+(?:update|advance)'
        r'|\bupdate\s+order\s+statuses\b',
        msg, re.IGNORECASE
    ):
        return _routed({'mode': 'advance_statuses'})

    # sales summary — with optional period parsing:
    #   "sales summary for 2025"      → year filter
    #   "sales summary for Q1 2026"   → quarter date range
    #   "sales summary this month"    → month-to-date
    #   "sales summary last month" / "this quarter" / "last quarter" / "this year"
    if msg == 'sales summary' or msg.startswith('sales summary'):
        params = {'mode': 'sales_summary'}
        today = date.today()
        q_m = re.search(r'\bq([1-4])\s*,?\s*(20\d{2})\b', msg, re.IGNORECASE)
        if q_m:
            q, yr = int(q_m.group(1)), int(q_m.group(2))
            q_start_month = (q - 1) * 3 + 1
            q_end_month = q_start_month + 2
            params['startDate'] = f"{yr}-{q_start_month:02d}-01"
            params['endDate'] = f"{yr}-{q_end_month:02d}-{calendar.monthrange(yr, q_end_month)[1]:02d}"
        elif re.search(r'\bthis\s+month\b', msg):
            params['startDate'] = today.replace(day=1).isoformat()
            params['endDate'] = today.isoformat()
        elif re.search(r'\blast\s+month\b', msg):
            first_this = today.replace(day=1)
            last_end = first_this - timedelta(days=1)
            params['startDate'] = last_end.replace(day=1).isoformat()
            params['endDate'] = last_end.isoformat()
        elif re.search(r'\bthis\s+quarter\b', msg):
            q_start_month = ((today.month - 1) // 3) * 3 + 1
            params['startDate'] = today.replace(month=q_start_month, day=1).isoformat()
            params['endDate'] = today.isoformat()
        elif re.search(r'\blast\s+quarter\b', msg):
            q_start_month = ((today.month - 1) // 3) * 3 + 1
            this_q_start = today.replace(month=q_start_month, day=1)
            last_q_end = this_q_start - timedelta(days=1)
            lq_start = ((last_q_end.month - 1) // 3) * 3 + 1
            params['startDate'] = last_q_end.replace(month=lq_start, day=1).isoformat()
            params['endDate'] = last_q_end.isoformat()
        elif re.search(r'\bthis\s+year\b', msg):
            params['startDate'] = today.replace(month=1, day=1).isoformat()
            params['endDate'] = today.isoformat()
        else:
            yr_m = re.search(r'\b(20\d{2})\b', raw)
            if yr_m:
                params['year'] = int(yr_m.group(1))
        return _routed(params)

    # revenue summary for <account name> — resolved to accountId in graph.py
    # via an account_search lookup ("Revenue summary for Bob Brown").
    _rev_name_m = re.match(
        r'^(?:revenue|account\s+revenue|sales)\s+summary\s+for\s+([A-Za-z].*?)\s*$',
        raw, re.IGNORECASE
    )
    if _rev_name_m and not re.match(r'^(?:q[1-4]\b|20\d{2}\b|this\b|last\b)', _rev_name_m.group(1), re.IGNORECASE):
        _acct_name = _rev_name_m.group(1).strip().rstrip('?.,;')
        if _acct_name and not _acct_name.isdigit():
            return _routed({'mode': 'account_summary', 'accountSearch': _acct_name})

    # account summary / show top spending customers
    if msg in ('account summary',) or \
       msg.startswith(('account summary', 'show top spending', 'top spending customers', 'analyze accounts')):
        yr_raw = _kv(raw, 'year')
        params = {'mode': 'account_summary'}
        if yr_raw and yr_raw.isdigit():
            yr = int(yr_raw)
            if 1900 <= yr <= 3000:
                params['year'] = yr
        return _routed(params)

    # category summary / analyze sales by category
    if msg in ('category summary',) or \
       msg.startswith(('category summary', 'analyze sales by category', 'sales by category')):
        yr_raw = _kv(raw, 'year')
        params = {'mode': 'category_summary'}
        if yr_raw and yr_raw.isdigit():
            yr = int(yr_raw)
            if 1900 <= yr <= 3000:
                params['year'] = yr
        return _routed(params)

    # find orders for <name> — with or without the word "customer"
    # Handles: "find orders for David Chen", "orders for customer Acme Corp",
    #          "show orders for David Chen", "orders from Acme Corp"
    _cust_m = re.search(
        r'\borders?\s+(?:for\s+(?:customer\s+)?|from\s+|by\s+)(.+?)(?:\s+(?:this|last|in|on|at)\b.*)?$',
        msg, re.IGNORECASE
    )
    if _cust_m:
        search_term = _cust_m.group(1).strip().rstrip('?.,;')
        if search_term:
            return _routed({
                'mode': 'list', 'search': search_term,
                'includeDeleted': False,
                'sortField': 'order_date', 'sortOrder': 'DESC',
                'pageSize': 50, 'pageNumber': 1,
            })

    # soft delete order <uuid> | <SO-number>
    if msg.startswith('soft delete order'):
        ids = _uuids(raw)
        ons = _order_numbers(raw)
        if ids or ons:
            p = {'mode': 'update', 'action': 'soft_delete'}
            if ids:
                p['orderId'] = ids[0]
            else:
                p['orderNumber'] = ons[0].upper()
            upd = _kv(raw, 'updatedBy') or _kv(raw, 'updated_by')
            if upd:
                p['updatedBy'] = upd
            return _routed(p)

    # delete order <uuid> | <SO-number>
    if msg.startswith('delete order'):
        ids = _uuids(raw)
        ons = _order_numbers(raw)
        if ids or ons:
            p = {'mode': 'update', 'action': 'soft_delete'}
            if ids:
                p['orderId'] = ids[0]
            else:
                p['orderNumber'] = ons[0].upper()
            upd = _kv(raw, 'updatedBy') or _kv(raw, 'updated_by')
            if upd:
                p['updatedBy'] = upd
            return _routed(p)

    # restore order <uuid> | <SO-number>
    if msg.startswith('restore order'):
        ids = _uuids(raw)
        ons = _order_numbers(raw)
        if ids or ons:
            p = {'mode': 'update', 'action': 'restore'}
            if ids:
                p['orderId'] = ids[0]
            else:
                p['orderNumber'] = ons[0].upper()
            upd = _kv(raw, 'updatedBy') or _kv(raw, 'updated_by')
            if upd:
                p['updatedBy'] = upd
            return _routed(p)

    # "restore a deleted order" (vague — no UUID/SO number) → list including
    # deleted orders so the user can locate the one to restore.
    if re.search(r'\brestore\b', msg, re.IGNORECASE) and re.search(r'\border', msg, re.IGNORECASE) \
       and not _uuids(raw) and not _order_numbers(raw):
        return _routed({
            'mode': 'list', 'includeDeleted': True,
            'sortField': 'order_date', 'sortOrder': 'DESC',
            'pageSize': 50, 'pageNumber': 1,
        })

    # create order for account <uuid>
    if msg.startswith('create order for account'):
        ids = _uuids(raw)
        if ids:
            p: dict = {'mode': 'create', 'accountId': ids[0]}
            contact_id  = _kv(raw, 'contact')   or _kv(raw, 'contactId')
            product_id  = _kv(raw, 'product')   or _kv(raw, 'productId')
            qty         = _kv(raw, 'quantity')  or _kv(raw, 'qty')
            ptype       = _kv(raw, 'priceType') or _kv(raw, 'price_type')
            status      = _kv(raw, 'status')
            created_by  = _kv(raw, 'createdBy') or _kv(raw, 'created_by')
            pricing_id  = _kv(raw, 'pricingId') or _kv(raw, 'productPricingId')
            if contact_id: p['contactId']        = contact_id
            if product_id: p['productId']        = product_id
            if qty:        p['quantity']         = int(qty)
            if ptype:      p['priceType']        = ptype
            if status:     p['status']           = status
            if created_by: p['createdBy']        = created_by
            if pricing_id: p['productPricingId'] = pricing_id
            od = _kv(raw, 'orderDate') or _kv(raw, 'order_date')
            p['orderDate'] = od.split('T')[0] if od else _today()
            return _routed(p)

    # update order <uuid>
    if msg.startswith('update order'):
        ids = _uuids(raw)
        if ids:
            status      = _kv(raw, 'status')
            contact_id  = _kv(raw, 'contact') or _kv(raw, 'contactId')
            updated_by  = _kv(raw, 'updatedBy') or _kv(raw, 'updated_by')
            # change_status if only status changing; otherwise update_header
            action = _kv(raw, 'action') or (
                'change_status' if status and not contact_id else 'update_header'
            )
            p = {'mode': 'update', 'action': action, 'orderId': ids[0]}
            if status:     p['status']    = status
            if contact_id: p['contactId'] = contact_id
            if updated_by: p['updatedBy'] = updated_by
            return _routed(p)

    # list categories / get categories → get_category
    if msg in ('list categories', 'get categories') or \
       msg.startswith(('list categories', 'get categories')):
        p = {'mode': 'get_category'}
        q = _kv(raw, 'search')
        if q:
            p['search'] = q
        return _routed(p)

    # list products / get products → get_product
    if msg in ('list products', 'get products') or \
       msg.startswith(('list products', 'get products')):
        ids = _uuids(raw)
        p = {'mode': 'get_product'}
        if ids:
            p['categoryId'] = ids[0]
        q = _kv(raw, 'search')
        if q:
            p['search'] = q
        return _routed(p)

    # ── "show order details" (bare, no UUID) → ask for identifier ────────────
    if re.match(r'^(?:show|view|get|display|fetch)\s+(?:order\s+)?details?\s*$', msg, re.IGNORECASE):
        return _routed({'mode': 'ask_order_identifier'})

    # ── recent orders → list sorted by date DESC ─────────────────────────────
    if re.search(r'\brecent\b', msg, re.IGNORECASE) and re.search(r'\borders?\b', msg, re.IGNORECASE):
        return _routed({
            'mode': 'list', 'includeDeleted': False,
            'sortField': 'order_date', 'sortOrder': 'DESC',
            'pageSize': 20, 'pageNumber': 1,
        })

    # ── quarterly / annual / monthly sales → sales_summary ───────────────────
    if re.search(r'\b(?:quarterly|annual|monthly|weekly)\s+(?:sales|revenue)\b', msg, re.IGNORECASE) \
       or re.search(r'\b(?:show|view|get)\s+(?:(?:quarterly|annual|monthly)\s+)?(?:sales|revenue)\s+summary\b', msg, re.IGNORECASE):
        yr_m = re.search(r'\b(20\d{2})\b', raw)
        params: dict = {'mode': 'sales_summary'}
        if yr_m:
            params['year'] = int(yr_m.group(1))
        elif re.search(r'\bquarterly\b', msg, re.IGNORECASE):
            # No explicit year — use current quarter date range
            today = date.today()
            q = (today.month - 1) // 3 + 1
            q_start_month = (q - 1) * 3 + 1
            q_end_month = q_start_month + 2
            q_end_day = calendar.monthrange(today.year, q_end_month)[1]
            params['startDate'] = f"{today.year}-{q_start_month:02d}-01"
            params['endDate'] = f"{today.year}-{q_end_month:02d}-{q_end_day:02d}"
        return _routed(params)

    # ── account revenue summary — broader phrasing ───────────────────────────
    if msg.startswith(('account revenue', 'revenue by account', 'revenue summary by account')):
        return _routed({'mode': 'account_summary'})

    # ── Vague create/update order intent → open the inline form ─────────────
    # Catches "I want to create order", "new order", "create or update order",
    # "I want to update order", etc. Skipped when the user supplied a UUID,
    # an account UUID (which the specific "create order for account <uuid>"
    # branch above handles), or full structured order fields.
    _has_uuid = bool(_uuids(raw))
    if not _has_uuid:
        if re.search(r'\b(create|new|add|make|open)\b.*\border', msg) \
           or re.search(r'\bcreate\s+or\s+update\s+order', msg):
            return _routed({'mode': 'show_order_form'})
        if re.search(r'\bupdate\b.*\border', msg):
            return _routed({'mode': 'show_order_form'})

    # ── 3. Passthru → AI Agent ────────────────────────────────────────────────
    return _passthru()
