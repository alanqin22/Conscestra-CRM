"""Product Pre-Router — Python equivalent of n8n Pre Router v2.2.

Mirrors the order_pre_router_v3.2 architecture.

Inspects every incoming message and either ROUTES it directly to Build SQL
Query (router_action=True) or PASSES it through to the AI Agent
(router_action=False).

CHANGES IN v2.2 — Amazon-style search bar: list_categories support

  New context 'list_categories' routes to mode: 'list_categories'.
  Used by product_v26+ HTML on page load to populate the category dropdown
  in the Amazon-style search bar component via sp_products_list_categories().

CHANGES IN v2.1 — Image URL support

  create_product and update_product contexts now forward pd.image_url
  as imageUrl into the routed params object so sql_builder v4.4 can
  pass it as p_image_url to sp_products v3f, which INSERTs / UPSERTs
  a row in product_image (sort_order = 1).

CHANGES IN v2.0 — Maximum Direct SP Routing

  EXTENDED product_direct_operation handler:
    New context values route ALL read-only SP modes directly, bypassing
    the AI Agent entirely. The HTML page now uses these for every quick-
    action button instead of fillAndSend() with natural language.

    New contexts handled:
      'get_product_details'  → mode: get_details
      'list_products'        → mode: list
      'inventory_summary'    → mode: inventory_summary
      'low_stock_report'     → mode: low_stock
      'price_matrix_report'  → mode: price_matrix
      'price_history_report' → mode: price_history
      'bulk_adjust_stock'    → mode: bulk_adjust_stock

  EXTENDED TEXT-PATTERN ROUTES (fallback for voice / manual typing):
    Patterns added for: inventory summary, low stock, price matrix,
    price history, bulk stock adjustment, get details by UUID or #.

  AI AGENT is now invoked ONLY for:
    • Fully free-form natural-language queries with no deterministic
      parameter mapping (e.g., complex multi-field reasoning).
    • Delete product requests (no delete mode in sp_products).
    • Any product_direct_operation with an unrecognised context.

FROM v1.0:
  product_direct_operation for create_product / update_product.
  Text patterns: search products by name, show/get product details.
"""

import re
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)

# ── Category name → category_number map (category table) ────────────────────
# Includes common synonyms so "clothing products" resolves to Apparel etc.
# Category 7 (Personal Care) was merged into Health & Wellness (4); its
# synonyms now resolve there so legacy "personal care" queries still work.
_CATEGORY_SYNONYMS = {
    'apparel': 1, 'clothing': 1, 'clothes': 1, 'fashion': 1,
    'electronics': 2, 'electronic': 2, 'tech': 2, 'gadgets': 2,
    'grocery': 3, 'groceries': 3, 'food': 3,
    'health & wellness': 4, 'health and wellness': 4, 'health': 4, 'wellness': 4,
    'personal care': 4, 'personal-care': 4, 'personalcare': 4, 'grooming': 4,
    'home essentials': 5, 'home essential': 5, 'home': 5,
    'office supplies': 6, 'office supply': 6, 'office': 6, 'stationery': 6,
    'pet supplies': 8, 'pet supply': 8, 'pets': 8, 'pet': 8,
    'snacks & beverages': 9, 'snacks and beverages': 9, 'snacks': 9,
    'snack': 9, 'beverages': 9, 'beverage': 9, 'drinks': 9,
    'toys & games': 10, 'toys and games': 10, 'toys': 10, 'toy': 10, 'games': 10,
}


def _category_number_from_text(text: str) -> Optional[int]:
    """Return the category_number for the first category synonym found in
    *text* (longest synonyms checked first), or None."""
    t = (text or '').lower()
    for syn in sorted(_CATEGORY_SYNONYMS, key=len, reverse=True):
        if re.search(rf'\b{re.escape(syn)}\b', t):
            return _CATEGORY_SYNONYMS[syn]
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_uuids(s: str) -> list:
    return UUID_RE.findall(str(s or ''))


def _to_num(v, cast=int):
    """Convert to numeric type; return None if absent/invalid."""
    if v is None or v == '':
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def _to_bool(v):
    """Convert to bool; return None if absent/invalid."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).lower() == 'true'


# ============================================================================
# ROUTER
# ============================================================================

def route_request(message: str, chat_input: dict) -> Dict[str, Any]:
    """
    Inspect the incoming message and return a routing decision dict.

    Routed:   { "router_action": True,  "params": { "mode": ..., ... } }
    Passthru: { "router_action": False }
    """
    raw = (message or '').strip()
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

    logger.info('=== Product Pre-Router v2.2 ===')
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

    def routed(params: dict) -> dict:
        logger.info(f'→ ROUTED: mode={params.get("mode")} context={params.get("context", "")}')
        return {'router_action': True, 'params': params}

    def passthru(reason: Optional[str] = None) -> dict:
        logger.info(f'→ PASSTHRU: AI Agent {f"({reason})" if reason else ""}')
        return {'router_action': False}

    # ── Executive questions (CEO / CFO / VP bank) ─────────────────────────────
    # Interrogative phrasings route to the shared executive Q&A layer with the
    # decision-grade format. Imperative commands ("Show all products",
    # "Show low stock alerts") keep their deterministic routes below.
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
            return routed({'mode': 'executive_question',
                           'sections': _sections, 'note': _note})

    # ── product_direct_operation — highest priority ───────────────────────────
    # Sent by web page v11.0+ when Add Product / Update Product is clicked.
    # All structured data is in chatInput.productData.
    if chat_input.get('message') == 'product_direct_operation':
        pd = chat_input.get('productData') or {}

        if pd:
            context = pd.get('context')

            # ── Create Product ───────────────────────────────────────────────
            if context == 'create_product':
                params: Dict[str, Any] = {
                    'mode':           'create',
                    'context':        'create_product',
                    'name':           pd.get('name'),
                    'sku':            pd.get('sku') or None,
                    'categoryId':     pd.get('category_id') or None,
                    'categoryNumber': _to_num(pd.get('category_number'), int),
                    'categoryName':   pd.get('category_name') or None,
                    'retailPrice':    _to_num(pd.get('retail_price'), float),
                    'promoPrice':     _to_num(pd.get('promo_price'), float),
                    'wholesalePrice': _to_num(pd.get('wholesale_price'), float),
                    'stockQuantity':  _to_num(pd.get('stock_quantity'), int),
                    'description':    pd.get('description') or None,
                    'status':         pd.get('status') or 'Active',
                    'imageUrl':       pd.get('image_url') or None,   # v2.1
                    'createdBy':      pd.get('created_by') or chat_input.get('sessionId') or None,
                    # [v3h] real employee actor from the form's Created By /
                    # Updated By dropdowns → sp_products p_created_by_uuid /
                    # p_updated_by_uuid (writes products.created_by/updated_by).
                    'createdByUuid':  pd.get('created_by_uuid') or pd.get('actor_uuid') or None,
                    'updatedByUuid':  pd.get('updated_by_uuid') or pd.get('actor_uuid') or None
                }
                return routed(params)

            # ── Update Product ───────────────────────────────────────────────
            if context == 'update_product':
                params = {
                    'mode':           'update',
                    'context':        'update_product',
                    'productId':      pd.get('product_id'),
                    'name':           pd.get('name') or None,
                    'sku':            pd.get('sku') or None,
                    'categoryId':     pd.get('category_id') or None,
                    'categoryNumber': _to_num(pd.get('category_number'), int),
                    'categoryName':   pd.get('category_name') or None,
                    'retailPrice':    _to_num(pd.get('retail_price'), float),
                    'promoPrice':     _to_num(pd.get('promo_price'), float),
                    'wholesalePrice': _to_num(pd.get('wholesale_price'), float),
                    'stockQuantity':  _to_num(pd.get('stock_quantity'), int),
                    'description':    pd.get('description') or None,
                    'status':         pd.get('status') or None,
                    'imageUrl':       pd.get('image_url') or None,   # v2.1
                    'updatedBy':      pd.get('updated_by') or chat_input.get('sessionId') or None,
                    # [v3h] real employee actor from the form's dropdowns.
                    # created_by_uuid is sent pre-filled with the existing
                    # creator, so an untouched edit leaves created_by unchanged;
                    # the SP only overwrites created_by when this is non-NULL.
                    'createdByUuid':  pd.get('created_by_uuid') or None,
                    'updatedByUuid':  pd.get('updated_by_uuid') or pd.get('actor_uuid') or None
                }
                return routed(params)

            # ── Get Product Details ──────────────────────────────────────────
            if context == 'get_product_details':
                if not pd.get('product_id') and pd.get('product_number') is None and not pd.get('sku'):
                    logger.warning('get_product_details: no identifier supplied')
                    return passthru('get_product_details missing identifier')
                params = {
                    'mode':          'get_details',
                    'productId':     pd.get('product_id') or None,
                    'productNumber': _to_num(pd.get('product_number'), int),
                    'sku':           pd.get('sku') or None
                }
                return routed(params)

            # ── List Products ────────────────────────────────────────────────
            if context == 'list_products':
                params = {
                    'mode':           'list',
                    'search':         pd.get('search') or None,
                    'categoryFilter': pd.get('category_id') or None,
                    'categoryNumber': _to_num(pd.get('category_number'), int),
                    'isActiveFilter': _to_bool(pd.get('is_active_filter')),
                    'skuFilter':      pd.get('sku_filter') or None,
                    'nameFilter':     pd.get('name_filter') or None,
                    'pageSize':       _to_num(pd.get('page_size'), int) or 50,
                    'pageNumber':     _to_num(pd.get('page_number'), int) or 1,
                    'sortField':      pd.get('sort_field') or None,
                    'sortOrder':      pd.get('sort_order') or None
                }
                return routed(params)

            # ── Inventory Summary ────────────────────────────────────────────
            if context == 'inventory_summary':
                params = {
                    'mode':              'inventory_summary',
                    'categoryFilter':    pd.get('category_id') or None,
                    'categoryNumber':    _to_num(pd.get('category_number'), int),
                    'lowStockThreshold': _to_num(pd.get('low_stock_threshold'), int) or 70
                }
                return routed(params)

            # ── Low Stock Report ─────────────────────────────────────────────
            if context == 'low_stock_report':
                params = {
                    'mode':              'low_stock',
                    'categoryFilter':    pd.get('category_id') or None,
                    'categoryNumber':    _to_num(pd.get('category_number'), int),
                    'lowStockThreshold': _to_num(pd.get('low_stock_threshold'), int) or 70,
                    'skuFilter':         pd.get('sku_filter') or None,
                    'nameFilter':        pd.get('name_filter') or None
                }
                return routed(params)

            # ── Price Matrix Report ──────────────────────────────────────────
            if context == 'price_matrix_report':
                params = {
                    'mode':           'price_matrix',
                    'categoryFilter': pd.get('category_id') or None,
                    'categoryNumber': _to_num(pd.get('category_number'), int),
                    'isActiveFilter': _to_bool(pd.get('is_active_filter')),
                    'skuFilter':      pd.get('sku_filter') or None,
                    'nameFilter':     pd.get('name_filter') or None
                }
                return routed(params)

            # ── Price History Report ─────────────────────────────────────────
            if context == 'price_history_report':
                if not pd.get('product_id') and pd.get('product_number') is None:
                    logger.warning('price_history_report: no product identifier supplied')
                    return passthru('price_history_report missing product identifier')
                params = {
                    'mode':          'price_history',
                    'productId':     pd.get('product_id') or None,
                    'productNumber': _to_num(pd.get('product_number'), int)
                }
                return routed(params)

            # ── Bulk Adjust Stock ────────────────────────────────────────────
            if context == 'bulk_adjust_stock':
                adj = _to_num(pd.get('stock_adjustment'), int)
                if adj is None or adj == 0:
                    logger.warning('bulk_adjust_stock: invalid or zero stock_adjustment')
                    return passthru('bulk_adjust_stock missing valid stock_adjustment')
                params = {
                    'mode':           'bulk_adjust_stock',
                    'stockAdjustment': adj,
                    'categoryFilter': pd.get('category_id') or None,
                    'categoryNumber': _to_num(pd.get('category_number'), int),
                    'isActiveFilter': _to_bool(pd.get('is_active_filter')),
                    'skuFilter':      pd.get('sku_filter') or None,
                    'nameFilter':     pd.get('name_filter') or None
                }
                return routed(params)

            # ── List Categories (NEW v2.2) — populates Amazon-style search dropdown
            if context == 'list_categories':
                return routed({'mode': 'list_categories'})

            logger.warning(f'product_direct_operation: unknown context: {context}')

    # ── Text-Pattern Routes ──────────────────────────────────────────────────
    # Fallback for voice input and manually typed messages that map 1-to-1 to
    # a deterministic SP mode. Order: most specific patterns checked first.

    # ── show all products / list products (no filter) ───────────────────────
    if re.match(r'^(show|list|display|get)\s+all\s+products?$', msg) or msg in ('list products', 'show products', 'all products'):
        return routed({'mode': 'list', 'pageSize': 50, 'pageNumber': 1})

    # ── show products page N — pagination via plain text ─────────────────────
    page_match = re.match(
        r'^(?:show|list|display|get)?\s*(?:me\s+)?(?:all\s+)?products?,?\s+(?:on\s+)?page\s+(\d+)$',
        msg
    )
    if page_match:
        return routed({'mode': 'list', 'pageSize': 50, 'pageNumber': int(page_match.group(1))})

    # ── top N products by stock level ─────────────────────────────────────────
    top_match = re.search(r'\btop\s+(\d+)\s+products?\s+by\s+stock\b', msg)
    if top_match:
        return routed({'mode': 'list', 'pageSize': int(top_match.group(1)),
                       'pageNumber': 1, 'sortField': 'stock_quantity', 'sortOrder': 'desc'})

    # ── show <category-name> products — e.g. "Show electronics products",
    # "List grocery products", "List clothing products" (synonym-aware).
    cat_name_match = re.match(
        r'^(?:show|list|display|get)(?:\s+me)?\s+(?:all\s+)?([a-z&\'\s]+?)\s+products?(?:\s+page\s+(\d+))?$',
        msg
    )
    if cat_name_match:
        cat_num = _category_number_from_text(cat_name_match.group(1))
        if cat_num is not None:
            params = {'mode': 'list', 'categoryNumber': cat_num,
                      'pageSize': 50, 'pageNumber': int(cat_name_match.group(2) or 1)}
            return routed(params)
        # not a known category — fall through to later patterns / AI

    # ── list all product categories (chat chip) ──────────────────────────────
    # displayFormat='table' renders a readable table; the bare context call
    # from _loadCategories() keeps the machine-parseable JSON format.
    if re.match(r'^(?:show|list|display|get)\s+(?:me\s+)?(?:all\s+)?(?:product\s+)?categor(?:y|ies)(?:\s+list)?$', msg):
        return routed({'mode': 'list_categories', 'displayFormat': 'table'})

    # ── list/show products in/by/for category [number] N ─────────────────────
    cat_list_match = re.match(
        r'^(?:show|list|display|get)\s+products?\s+(?:in|by|for|from)?\s*(?:category\s*(?:number\s*|#\s*)?)?(\d+)',
        msg, re.IGNORECASE
    ) or re.match(
        r'^(?:show|list|display|get)\s+products?\s+(?:in|by|for|from)\s+category\s*(?:number\s*|#\s*)?(\d+)',
        msg, re.IGNORECASE
    )
    if cat_list_match:
        return routed({'mode': 'list', 'categoryNumber': int(cat_list_match.group(1)), 'pageSize': 50, 'pageNumber': 1})

    # Also catch the reverse word order: "list category [number] N products"
    cat_rev_match = re.match(
        r'^(?:show|list|display|get)\s+category\s*(?:number\s*|#\s*)?(\d+)\s+products?',
        msg, re.IGNORECASE
    )
    if cat_rev_match:
        return routed({'mode': 'list', 'categoryNumber': int(cat_rev_match.group(1)), 'pageSize': 50, 'pageNumber': 1})

    # ── search products: <query> — unified MODE:list path ────────────────────
    # Used by BOTH the home-page search bar AND the Create/Update form typeahead.
    # MODE:list is fast enough for both — product_search is retired (Option A).
    if msg.startswith('search products by name') or msg.startswith('search products:'):
        if msg.startswith('search products:'):
            query = raw[len('search products:'):].strip()
        else:
            query = re.sub(r'search products by name\s*', '', raw, flags=re.IGNORECASE).strip()
        if len(query) >= 1:
            logger.info(f'[list/search] query: {query}')
            return routed({'mode': 'list', 'search': query, 'pageSize': 20, 'pageNumber': 1})

    # ── "search for <term>" — home-page chip / voice phrasing ─────────────────
    search_for_match = re.match(r'^search\s+for\s+(.+?)\s*$', msg)
    if search_for_match:
        query = raw[search_for_match.start(1):search_for_match.end(1)].strip().rstrip('?.!,')
        if query:
            logger.info(f'[list/search-for] query: {query}')
            return routed({'mode': 'list', 'search': query, 'pageSize': 20, 'pageNumber': 1})

    # ── "show product details" (bare, no identifier) → ask which product ────
    if re.match(r'^(?:show|view|get|display|fetch)\s+(?:product\s+)?details?\s*$', msg, re.IGNORECASE):
        return routed({'mode': 'ask_product_identifier'})

    # ── get / show product details for <uuid-or-number> ──────────────────────
    if msg.startswith('show product details for') or msg.startswith('get product details for'):
        ids = _extract_uuids(raw)
        num_match = re.search(r'(?:product\s+)?(?:number|#)\s*(\d+)', raw, re.IGNORECASE)
        if ids or num_match:
            params = {'mode': 'get_details'}
            if ids:
                params['productId'] = ids[0]
            if num_match:
                params['productNumber'] = int(num_match.group(1))
            return routed(params)

    # ── show details for <product name> ───────────────────────────────────────
    # "Show details for AirPods Pro" → name search; graph.py auto-resolves to
    # get_details when exactly one product matches, otherwise shows the list.
    det_name_match = re.match(
        r'^(?:show|get|view|display)\s+(?:me\s+)?(?:full\s+)?(?:product\s+)?details?\s+(?:for|of|on)\s+(.+?)\s*$',
        msg
    )
    if det_name_match:
        pname = raw[det_name_match.start(1):det_name_match.end(1)].strip().rstrip('?.!,')
        if pname and not _extract_uuids(pname):
            logger.info(f'[get-details-by-name] search: {pname}')
            return routed({'mode': 'list', 'search': pname, 'pageSize': 5,
                           'pageNumber': 1, 'detailsRequested': True})

    # ── inventory summary / stock analytics ─────────────────────────────────
    if ('inventory summary' in msg or msg == 'show inventory' or msg == 'inventory summary'
            or 'stock analytics' in msg or 'inventory analytics' in msg
            or 'inventory by category' in msg or 'stock summary' in msg):
        cat_num_match = re.search(r'category\s+(?:number\s+)?(\d+)', raw, re.IGNORECASE)
        category_number = (int(cat_num_match.group(1)) if cat_num_match
                           else _category_number_from_text(msg))
        params: Dict[str, Any] = {'mode': 'inventory_summary', 'lowStockThreshold': 70}
        if category_number is not None:
            params['categoryNumber'] = category_number
        return routed(params)

    # ── out of stock — direct report (threshold 0: stock_quantity <= 0) ──────
    if 'out of stock' in msg:
        cat_num = (lambda m: int(m.group(1)) if m else None)(
            re.search(r'category\s+(?:number\s+)?(\d+)', raw, re.IGNORECASE)
        ) or _category_number_from_text(msg)
        params: Dict[str, Any] = {'mode': 'low_stock', 'lowStockThreshold': 0}
        if cat_num is not None:
            params['categoryNumber'] = cat_num
        return routed(params)

    # ── low stock ────────────────────────────────────────────────────────────
    if 'low stock' in msg or 'stock alert' in msg:
        thresh_match  = re.search(r'threshold\s+(?:of\s+)?(\d+)', raw, re.IGNORECASE)
        cat_num_match = re.search(r'category\s+(?:number\s+)?(\d+)', raw, re.IGNORECASE)
        name_match    = re.search(r'(?:named?|called|containing)\s+"?([A-Za-z][\w\s-]{0,40}?)"?(?:\s*$|[.,!?])', raw, re.IGNORECASE)

        # When the user didn't pin down the threshold AND didn't pre-filter,
        # surface the Low Stock form so they can set it interactively instead
        # of taking the SP default and hoping it matches what they wanted.
        if not thresh_match and not cat_num_match and not name_match:
            return routed({'mode': 'show_low_stock_form'})

        threshold = int(thresh_match.group(1)) if thresh_match else 70
        category_number = int(cat_num_match.group(1)) if cat_num_match else None
        params: Dict[str, Any] = {
            'mode': 'low_stock',
            'lowStockThreshold': threshold,
            'categoryNumber': category_number,
        }
        if name_match:
            params['nameFilter'] = name_match.group(1).strip()
        return routed(params)

    # ── price matrix / compare pricing ───────────────────────────────────────
    if 'price matrix' in msg or 'pricing matrix' in msg \
       or re.search(r'\bcompare\b.*\bpric(?:e|es|ing)\b', msg, re.IGNORECASE):
        has_active = 'active' in msg
        is_active = re.search(r'\bactive\b', msg, re.IGNORECASE) and not re.search(r'inactive', msg, re.IGNORECASE)
        is_active_filter = True if has_active and is_active else None
        cat_num_match = re.search(r'category\s+(?:number\s+)?(\d+)', raw, re.IGNORECASE)
        # Numeric category wins; otherwise resolve a category NAME mention
        # ("price matrix for electronics" → categoryNumber 2).
        category_number = (int(cat_num_match.group(1)) if cat_num_match
                           else _category_number_from_text(msg))
        return routed({
            'mode': 'price_matrix',
            'isActiveFilter': is_active_filter,
            'categoryNumber': category_number
        })

    # ── price history ────────────────────────────────────────────────────────
    if re.search(r'\bpric(?:e|ing)\s+history\b', msg, re.IGNORECASE):
        ids = _extract_uuids(raw)
        num_match = re.search(r'(?:product\s+)?(?:number|#)\s*(\d+)', raw, re.IGNORECASE)
        if ids or num_match:
            params = {'mode': 'price_history'}
            if ids:
                params['productId'] = ids[0]
            if num_match:
                params['productNumber'] = int(num_match.group(1))
            return routed(params)
        # Name-based: "pricing history for AirPods Pro" → search by name,
        # auto-resolve to price_history in graph.py if exactly 1 result.
        name_m = re.search(r'\bfor\s+(.+?)(?:\s*[?.!,]|$)', raw, re.IGNORECASE)
        if name_m:
            pname = name_m.group(1).strip()
            if pname:
                return routed({'mode': 'list', 'search': pname, 'pageSize': 5,
                               'pageNumber': 1, 'priceHistoryRequested': True})
        # No identifier → open the inline Price History form (typeahead +
        # voice) so the user can pick a product without bouncing to the AI.
        return routed({'mode': 'show_price_history_form'})

    # ── update <field> for <product name> ─────────────────────────────────────
    # "Update price for AirPods Pro", "Update stock quantity for Samsung
    # Galaxy S24", "Update description for AirPods Pro" → filtered product
    # list; each row's ✎ Edit button opens the prefilled update form.
    upd_field_match = re.match(
        r'^(?:update|edit|change|set|modify)\s+(?:the\s+)?'
        r'(?:price|pricing|prices|stock(?:\s+quantity)?|quantity|inventory|'
        r'description|name|sku|image|status)\s+'
        r'(?:for|of|on)\s+(.+?)\s*$',
        msg
    )
    if upd_field_match:
        pname = raw[upd_field_match.start(1):upd_field_match.end(1)].strip().rstrip('?.!,')
        if pname:
            logger.info(f'[update-field] product search: {pname}')
            return routed({'mode': 'list', 'search': pname, 'pageSize': 10, 'pageNumber': 1})

    # ── bulk stock form (vague intent, no specific number) ───────────────────
    # Catches phrases like "I want bulk stock update", "open bulk stock form",
    # "bulk stock adjustment" — anything that mentions 'bulk' + a stock keyword
    # but doesn't include a concrete "by N" number. Returns a marker the
    # frontend interprets as "open the inline Bulk Stock Adjustment form".
    if re.search(r'\bbulk\b', msg, re.IGNORECASE) \
       and re.search(r'\b(stock|adjust|update|adjustment|form)\b', msg, re.IGNORECASE) \
       and not re.search(r'\bby\s+[+-]?\d+', msg, re.IGNORECASE):
        return routed({'mode': 'show_bulk_stock_form'})

    # ── product add/update form (vague create-or-update intent) ──────────────
    # Catches "I want to add a product", "create a new product", "update a
    # product", "edit product information", "open the product form", etc.
    # Skips when the user already provided structured details (SKU + price),
    # since those go straight to the AI's create flow which needs the values.
    if re.search(r'\b(create|new|add|make)\b.*\bproduct', msg, re.IGNORECASE) \
       or re.search(r'\bcreate\s+or\s+update\s+product', msg, re.IGNORECASE) \
       or re.search(r'\b(open|show)\s+(?:the\s+)?(?:add|update|new|create|product)\s+(?:product\s+)?form\b', msg, re.IGNORECASE) \
       or re.search(r'\b(update|edit|modify)\b\s+(?:a\s+|an\s+|the\s+)?product\b', msg, re.IGNORECASE) \
       or re.search(r'\bedit\s+product\s+info', msg, re.IGNORECASE):
        # If the user supplied SKU + a price keyword, they likely want the
        # AI to actually create the product — fall through.
        has_sku   = re.search(r'\bsku[:\s]+\S', raw, re.IGNORECASE)
        has_price = re.search(r'\$\d|\b(price|cost|retail|wholesale)\s*[:=]\s*\d', raw, re.IGNORECASE)
        if not (has_sku and has_price):
            return routed({'mode': 'show_product_form'})

    # ── bulk adjust / update stock ───────────────────────────────────────────
    bulk_match = re.match(r'^(increase|decrease|add|subtract|adjust|update)\s+stock\s+by\s+(\d+)', msg, re.IGNORECASE)
    if bulk_match:
        action = bulk_match.group(1).lower()
        sign = -1 if action in ('decrease', 'subtract') else 1
        adj = sign * int(bulk_match.group(2))
        cat_num_match = re.search(r'category\s+(?:number\s+)?(\d+)', raw, re.IGNORECASE)
        category_number = int(cat_num_match.group(1)) if cat_num_match else None
        cat_id = _extract_uuids(raw)
        category_filter = cat_id[0] if cat_id else None
        sku_match = re.search(r'sku\s+([A-Za-z0-9\-_]+)', raw, re.IGNORECASE)
        sku_filter = sku_match.group(1) if sku_match else None
        name_match = re.search(r'(?:named?|called)\s+"?([^"]+)"?', raw, re.IGNORECASE)
        name_filter = name_match.group(1) if name_match else None
        return routed({
            'mode': 'bulk_adjust_stock',
            'stockAdjustment': adj,
            'categoryNumber': category_number,
            'categoryFilter': category_filter,
            'skuFilter': sku_filter,
            'nameFilter': name_filter
        })

    # ── reorder recommendations → low stock report ───────────────────────────
    if re.search(r'\breorder\b', msg, re.IGNORECASE):
        return routed({'mode': 'low_stock', 'lowStockThreshold': 70})

    # ── No match — AI Agent handles ───────────────────────────────────────────
    return passthru()
