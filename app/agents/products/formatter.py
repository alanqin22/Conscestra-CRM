"""Response formatter for Product Management — aligned with n8n Format Response v3.4.

v3.4 (list_categories mode — Amazon-style search bar dropdown):
  + list_categories mode: returns a machine-parseable output consumed by
    the HTML frontend _loadCategories() to populate the category dropdown.
    Output format: "[MODE:list_categories]\n[{...}]" (JSON array on line 2).
    The frontend parses this response directly and never renders it into
    responseBox. Handles both sp_products_list_categories() response key
    and the generic 'result' / raw row fallback.

v3.3 (gallery images in list mode):
  + list mode: sp_products v3g now returns images[] per product via a
    correlated subquery on product_image. normalise_product() maps
    images[] from the SP response. The list output block now emits
    Image-URL: (primary, sort_order = 1) and Image-URL-2 … Image-URL-N
    lines per product so buildProductListHTML can parse gallery images
    and pass them into renderProductRecord, matching the detail view.

v3.2 (gallery in detail view):
  + get_details mode: emits Image-URL-2 through Image-URL-N lines for
    additional gallery images (sort_order > 1) from response.product.images[].
    The HTML detail view renders an Amazon-style thumbnail strip.

v3.1 (defensive multi-key fallback for remote PostgreSQL compatibility):
  + _parse_response: logs actual top-level and inner keys returned by SP
    (INFO level) so key mismatches are visible in server logs.
  + Image support: primary_image_url + image_alt_text passed through
    _normalise_product() and emitted as "Image-URL:" line in list,
    get_details, add, create, and update output blocks.
  + Defensive multi-key fallback chains for low_stock, bulk_adjust_stock,
    inventory_summary, and price_matrix array keys.

v3.0:
  + product_search mode — typeahead search for the unified Add/Update form.
  + Full UUIDs always emitted (never truncated).
  + Markdown tables for bulk modes and reports.
  + Normalised product fields.
  + Price order: Retail → Promo → Wholesale (hide missing).

All 11 modes supported:
  list, get_details, add, create, update,
  bulk_adjust_stock, inventory_summary, low_stock,
  price_history, price_matrix, product_search,
  list_categories.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote as _url_quote

# Absolute base URL for product images on agentorc.ca.
# Must be absolute so URLs work regardless of which page path renders them.
_IMAGE_BASE_URL = "https://agentorc.ca/image"

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_num(n, fallback: float = 0.0) -> float:
    try:
        return float(n)
    except (TypeError, ValueError):
        return fallback


def _fmt_currency(n, currency: str = 'USD') -> str:
    v = _safe_num(n)
    return f'${v:,.2f} {currency}'.strip()


def _fmt_big(n) -> str:
    v = _safe_num(n)
    return f'{v:,.0f}'


def _fmt_dt(value) -> str:
    if not value:
        return 'N/A'
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        elif isinstance(value, datetime):
            dt = value
        else:
            return 'N/A'
        return dt.strftime('%b %d, %Y %I:%M %p')
    except (ValueError, AttributeError):
        return 'N/A'


def _fmt_date(value) -> str:
    if not value:
        return 'N/A'
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        elif isinstance(value, datetime):
            dt = value
        else:
            return 'N/A'
        return dt.strftime('%b %d, %Y')
    except (ValueError, AttributeError):
        return 'N/A'


def _full_uuid(u) -> str:
    """Return the full UUID string; never truncate."""
    return str(u) if u else 'N/A'


def _safe_json(v):
    try:
        return json.loads(v) if isinstance(v, str) else v
    except (json.JSONDecodeError, TypeError):
        return None


def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    """Build a GitHub-flavoured markdown table."""
    if not headers:
        return ''
    sep = '| ' + ' | '.join('---' for _ in headers) + ' |'
    lines = [
        '| ' + ' | '.join(headers) + ' |',
        sep,
    ] + [
        '| ' + ' | '.join(str(c) for c in row) + ' |'
        for row in rows
    ]
    return '\n'.join(lines)


def _build_image_gallery(product_name: str, category_name: str) -> list:
    """Construct 5 standard image-record dicts from the hemera.canspace.ca upload pattern.

    Used as a fallback when sp_products returns no images[] for a product.
    URLs follow the pattern uploaded by upload_cpanel.py:
        {IMAGE_BASE_URL}/{category}/{product_name}/image_N.jpg
    """
    if not product_name or not category_name:
        return []
    cat_enc  = _url_quote(category_name,  safe='')
    name_enc = _url_quote(product_name,   safe='')
    return [
        {
            'sort_order': i,
            'image_url':  f"{_IMAGE_BASE_URL}/{cat_enc}/{name_enc}/image_{i}.jpg",
            'alt_text':   f"{product_name} - image {i}",
        }
        for i in range(1, 6)
    ]


def _normalise_product(p: dict) -> dict:
    """Normalise stored-procedure product shape to a consistent internal shape."""
    if not p:
        return {}

    # images[] — full gallery array keyed by sort_order (v3.2)
    images = p.get('images') or []

    # If the SP returned no images[], construct them from the known upload pattern
    # so the gallery works before (or instead of) running update_product_images.sql.
    if not images:
        images = _build_image_gallery(
            p.get('product_name') or '',
            p.get('category_name') or '',
        )

    # primary_image_url: prefer the explicit top-level field the SP returns,
    # fall back to images[sort_order=1] so both SP shapes work without change.
    primary_image_url = p.get('primary_image_url') or None
    if not primary_image_url and images:
        sort1 = next((img for img in images
                      if isinstance(img, dict) and img.get('sort_order') == 1), None)
        candidate = sort1 or (images[0] if images else None)
        if isinstance(candidate, dict):
            primary_image_url = candidate.get('image_url') or None

    return {
        'product_id':        p.get('product_id'),
        'product_number':    p.get('product_number'),
        'product_name':      p.get('product_name'),
        'sku':               p.get('sku'),
        'category_name':     p.get('category_name'),
        'category_id':       p.get('category_id'),
        'category_number':   p.get('category_number'),
        'description':       p.get('description'),
        'stock':             p.get('stock_quantity'),
        'is_active':         p.get('is_active'),
        'currency':          p.get('currency_code', 'USD'),
        'wholesale':         p.get('wholesale_price'),
        'retail':            p.get('retail_price'),
        'promo':             p.get('promo_price'),
        'created_at':        p.get('created_at'),
        'updated_at':        p.get('updated_at'),
        # Audit attribution — nested {employee_uuid, employee_name, first_name,
        # last_name, email} from sp_products v3h get_details. May be {} or None
        # for older SP versions or NULL FK rows (rendered as "—").
        'created_by':        p.get('created_by') or {},
        'updated_by':        p.get('updated_by') or {},
        'prices':            p.get('prices'),
        'primary_image_url': primary_image_url,
        'image_alt_text':    p.get('image_alt_text') or None,
        'images':            images,
    }


def _employee_display(emp: Optional[dict]) -> str:
    """Render an employee dict as a display name. Prefers 'First Last';
    falls back to employee_name, then email, then em-dash."""
    if not emp or not isinstance(emp, dict):
        return '—'
    first = (emp.get('first_name') or '').strip()
    last  = (emp.get('last_name')  or '').strip()
    full  = f'{first} {last}'.strip()
    if full:
        return full
    return (emp.get('employee_name') or emp.get('email') or '—').strip() or '—'


def _price_triplet(p: dict) -> str:
    """Format prices in Retail → Promo → Wholesale order; hide missing."""
    currency = p.get('currency', 'USD')
    parts = []
    if p.get('retail')    is not None: parts.append(f"Retail: {_fmt_currency(p['retail'], currency)}")
    if p.get('promo')     is not None: parts.append(f"Promo: {_fmt_currency(p['promo'], currency)}")
    if p.get('wholesale') is not None: parts.append(f"Wholesale: {_fmt_currency(p['wholesale'], currency)}")
    return ' | '.join(parts)


def _parse_response(db_rows: List[Dict]) -> Dict:
    """Extract the sp_products JSON response from raw DB rows.

    v3.4: also handles sp_products_list_categories response key.
    """
    if not db_rows:
        logger.warning('_parse_response: db_rows is empty')
        return {}
    first = db_rows[0]
    logger.debug(f'_parse_response: top-level keys = {list(first.keys())}')
    # Check all known SP function result keys in priority order
    for key in ('sp_products', 'sp_products_list_categories', 'result'):
        val = first.get(key)
        if val:
            parsed = _safe_json(val) if isinstance(val, str) else val
            if isinstance(parsed, dict):
                logger.debug(f'_parse_response: found response under "{key}", inner keys = {list(parsed.keys())}')
                return parsed
    logger.warning(
        f'_parse_response: no known SP key found in row — returning raw row. '
        f'Keys: {list(first.keys())}'
    )
    return first


def _product_detail_block(out: List[str], p: dict, price_history: list, mode_label: str,
                          gallery_images: Optional[list] = None):
    """Emit the detail block used by get_details, add, create, update."""
    currency = p.get('currency', 'USD')
    out.append(f'{p.get("product_name", "N/A")}')
    out.append(f'Product ID: {_full_uuid(p.get("product_id"))}')
    out.append(f'Product#: {p.get("product_number") or "N/A"}')
    out.append(f'SKU: {p.get("sku") or "N/A"}')
    out.append(f'Category: {p.get("category_name") or "N/A"}')
    out.append(f'Category#: {p.get("category_number") or "N/A"}')
    if p.get('category_id'):
        out.append(f'Category ID: {_full_uuid(p["category_id"])}')
    if p.get('description'):
        out.append('')
        out.append(f'Description: {p["description"]}')
    out.append('')
    out.append(f'Stock: {p.get("stock", 0)} units')
    price_line = _price_triplet(p)
    if price_line:
        out.append(price_line)
    out.append('')
    out.append(f'Status: {"Active" if p.get("is_active") else "Inactive"}')
    out.append(f'Created: {_fmt_dt(p.get("created_at"))}')
    out.append(f'Updated: {_fmt_dt(p.get("updated_at"))}')
    # v3h — audit attribution. Hyphenated keys avoid collision with the
    # frontend's existing /Created:/ /Updated:/ regex parsers.
    _cb = p.get('created_by') or {}
    _ub = p.get('updated_by') or {}
    out.append(f'Created-By: {_employee_display(_cb)}')
    out.append(f'Updated-By: {_employee_display(_ub)}')
    # UUID lines let the frontend pre-select the form's Created By / Updated By
    # dropdowns by exact employee_uuid (so an untouched edit preserves created_by).
    out.append(f'Created-By-UUID: {_full_uuid(_cb.get("employee_uuid")) if _cb.get("employee_uuid") else ""}')
    out.append(f'Updated-By-UUID: {_full_uuid(_ub.get("employee_uuid")) if _ub.get("employee_uuid") else ""}')
    # Primary image — v3.1
    if p.get('primary_image_url'):
        out.append(f'Image-URL: {p["primary_image_url"]}')
    # Additional gallery images (sort_order 2-N) — v3.2
    if gallery_images:
        for img in gallery_images:
            so = img.get('sort_order') if isinstance(img, dict) else None
            url = img.get('image_url') if isinstance(img, dict) else None
            if so and so > 1 and url:
                out.append(f'Image-URL-{so}: {url}')
    if price_history:
        out.append('')
        out.append('**Pricing History**')
        out.append('')
        for idx, h in enumerate(price_history, start=1):
            current_tag = ' [ACTIVE]' if h.get('is_current') else ''
            out.append(
                f'   {idx}. {h.get("price_type")}: '
                f'{_fmt_currency(h.get("price_value"), currency)}{current_tag}'
            )
            eff_to = _fmt_date(h.get('effective_to')) if h.get('effective_to') else 'Present'
            out.append(f'      {_fmt_date(h.get("effective_from"))} → {eff_to}')


# ============================================================================
# PUBLIC API
# ============================================================================

from app.core.text_clean import clean_obj


def format_response(db_rows: List[Dict], params: Dict[str, Any]) -> str:
    """
    Format sp_products DB rows into the markdown string expected by the
    HTML frontend.

    Returns
    -------
    str — placed directly into ChatResponse.output.
    """
    mode = str(params.get('mode') or 'unknown').lower().strip()
    response = clean_obj(_parse_response(db_rows))
    metadata = response.get('metadata', {})

    logger.info(f'Format Response (sp_products v3.4) — mode={mode}')

    # ── Error short-circuit ───────────────────────────────────────────────────
    if metadata.get('status') == 'error' or (
        metadata.get('code') and metadata.get('code') != 0
    ):
        code = metadata.get('code', -999)
        msg  = metadata.get('message', 'Unknown error occurred')
        return (
            f'[ERROR] ERROR REPORT\n'
            f'Date: {_fmt_dt(datetime.utcnow().isoformat())}\n'
            f'Error Code: {code}\n'
            f'Error Message: {msg}\n\n'
            f'Please try again or contact support.'
        )

    out: List[str] = []

    # ── Executive answer — pre-formatted by the shared executive layer ───────
    if mode == 'executive_question':
        return response.get('exec_markdown') or 'No executive data available.'

    # ── Web answer — pre-formatted by app/core/web_tools.py ──────────────────
    if mode == 'web_search':
        return response.get('web_markdown') or 'No web results found.'

    # ── UI-only marker modes (no DB hit) ─────────────────────────────────────
    # The HTML response dispatcher matches [MODE:show_*_form] and opens the
    # corresponding inline form instead of rendering text.
    if mode == 'show_bulk_stock_form':
        return ('[MODE:show_bulk_stock_form]\n'
                'Opening the Bulk Stock Adjustment form below…')
    if mode == 'show_price_history_form':
        return ('[MODE:show_price_history_form]\n'
                'Opening the Price History search form below…')
    if mode == 'show_low_stock_form':
        return ('[MODE:show_low_stock_form]\n'
                'Opening the Low Stock Alert form below…')
    if mode == 'show_product_form':
        return ('[MODE:show_product_form]\n'
                'Opening the Add / Update Product form below…')
    if mode == 'ask_product_identifier':
        return ('Please provide the specific product name, ID, or SKU for which '
                'you would like to see the details.')

    # ── list_categories (v3.4) — machine-readable JSON for dropdown ──────────
    # Never rendered to the user — the HTML _loadCategories() function parses
    # [MODE:list_categories] from output and populates the category <select>.
    if mode == 'list_categories':
        cats = response.get('categories') or []
        logger.info(f'list_categories: {len(cats)} categories returned')

        # displayFormat='table' — user-facing chat request ("List all product
        # categories" chip): render a readable markdown table instead of the
        # machine-parseable JSON used by the search-bar dropdown loader.
        if str(params.get('displayFormat') or '').lower() == 'table':
            out.append('**[MODE:list_categories_table] Product Categories**')
            out.append('')
            out.append(f'{len(cats)} categories')
            out.append('')
            out.append('| Category # | Category Name |')
            out.append('| --- | --- |')
            for c in cats:
                out.append(f"| {c.get('category_number', '—')} | {c.get('category_name', '—')} |")
            return '\n'.join(out)

        # Normalise to the shape the HTML expects
        normalised = [
            {
                'category_id':     str(c.get('category_id') or ''),
                'category_number': c.get('category_number'),
                'category_name':   c.get('category_name') or '',
            }
            for c in cats
        ]
        out.append('[MODE:list_categories]')
        out.append(json.dumps(normalised))
        return '\n'.join(out)

    # ── product_search — typeahead compact table ──────────────────────────────
    if mode == 'product_search':
        products = [
            {
                'product_id':     p.get('product_id'),
                'product_number': p.get('product_number'),
                'product_name':   p.get('product_name'),
                'sku':            p.get('sku'),
                'category_name':  p.get('category_name'),
                'category_id':    p.get('category_id'),
                'category_number':p.get('category_number'),
                'is_active':      p.get('is_active'),
            }
            for p in (response.get('products') or [])
        ]
        search_term  = metadata.get('search_term') or params.get('search') or ''
        result_count = metadata.get('result_count') or len(products)

        out.append('**[MODE:product_search] Product Search**')
        out.append('')
        if not products:
            out.append(f'Search: "{search_term}"')
            out.append('No products found matching your search.')
        else:
            out.append(f'Search: "{search_term}" — {result_count} result(s)')
            out.append('')
            rows = [
                [
                    p['product_name'] or 'N/A',
                    _full_uuid(p['product_id']),
                    p['sku'] or '',
                    p['category_name'] or '',
                ]
                for p in products
            ]
            out.append(_md_table(['Product Name', 'Product ID', 'SKU', 'Category'], rows))
        return '\n'.join(out)

    # ── list ──────────────────────────────────────────────────────────────────
    if mode == 'list':
        products   = [_normalise_product(p) for p in (response.get('products') or [])]
        page       = metadata.get('page', 1)
        page_size  = metadata.get('page_size', 50)
        total_rec  = metadata.get('total_records', len(products))
        total_pages= metadata.get('total_pages', 1)
        search_term= metadata.get('search_term')

        out.append('**[MODE:list] Product List**')
        out.append('')
        if search_term:
            out.append(f'Search: "{search_term}"')
        out.append(f'Showing {len(products)} of {total_rec} products (Page {page} of {total_pages})')
        out.append('')

        for idx, p in enumerate(products, start=1):
            stock = p.get('stock', 0) or 0
            if stock == 0:
                stock_status = 'Out of Stock'
            elif stock <= 10:
                stock_status = 'Low Stock'
            else:
                stock_status = 'In Stock'

            out.append(f'**{idx}. {p.get("product_name")}** (Product ID: {_full_uuid(p.get("product_id"))})')
            out.append(f'   Product#: {p.get("product_number") or "N/A"}')
            out.append(f'   SKU: {p.get("sku") or "N/A"}')
            out.append(f'   Category: {p.get("category_name") or "N/A"}')
            out.append(f'   Category#: {p.get("category_number") or "N/A"}')
            if p.get('category_id'):
                out.append(f'   Category ID: {_full_uuid(p["category_id"])}')
            if p.get('description'):
                out.append(f'   Description: {p["description"]}')
            out.append(f'   Stock: {stock} units ({stock_status})')
            price_line = _price_triplet(p)
            if price_line:
                out.append(f'   {price_line}')
            out.append(f'   Status: {"Active" if p.get("is_active") else "Inactive"}')
            out.append(f'   Created: {_fmt_dt(p.get("created_at"))}')
            out.append(f'   Updated: {_fmt_dt(p.get("updated_at"))}')
            # Audit attribution — emitted as "Created-By:" / "Updated-By:" so the
            # frontend's /Created-By:/ /Updated-By:/ regexes parse them without
            # colliding with the /Created:/ /Updated:/ timestamp parsers above.
            if p.get('created_by_name'):
                out.append(f'   Created-By: {p["created_by_name"]}')
            if p.get('updated_by_name'):
                out.append(f'   Updated-By: {p["updated_by_name"]}')
            # Primary image (sort_order = 1) — v3.1
            if p.get('primary_image_url'):
                out.append(f'   Image-URL: {p["primary_image_url"]}')
            # Gallery images (sort_order 2-N) — v3.3
            for img in (p.get('images') or []):
                so  = img.get('sort_order') if isinstance(img, dict) else None
                url = img.get('image_url')  if isinstance(img, dict) else None
                if so and so > 1 and url:
                    out.append(f'   Image-URL-{so}: {url}')
            out.append('')
        return '\n'.join(out)

    # ── get_details ───────────────────────────────────────────────────────────
    if mode == 'get_details':
        raw_product = response.get('product')
        p = _normalise_product(raw_product) if raw_product else {}
        history = response.get('price_history') or []
        gallery_images = (raw_product or {}).get('images') or []
        out.append('**[MODE:get_details] Product Details**')
        out.append('')
        if p:
            _product_detail_block(out, p, history, 'get_details',
                                  gallery_images=gallery_images)
        else:
            out.append('_No product data returned._')
        return '\n'.join(out)

    # ── add ───────────────────────────────────────────────────────────────────
    if mode == 'add':
        raw_product = response.get('product')
        p = _normalise_product(raw_product) if raw_product else {}
        history = (raw_product or {}).get('pricing_history') or []
        out.append('**[MODE:add] Product Added Successfully**')
        out.append('')
        if p:
            _product_detail_block(out, p, history, 'add')
        if metadata.get('message'):
            out.append('')
            out.append(metadata['message'])
        return '\n'.join(out)

    # ── create ────────────────────────────────────────────────────────────────
    if mode == 'create':
        raw_product = response.get('product')
        p = _normalise_product(raw_product) if raw_product else {}
        history = (raw_product or {}).get('pricing_history') or []
        out.append('**[MODE:create] Product Created Successfully**')
        out.append('')
        if p:
            _product_detail_block(out, p, history, 'create')
        if metadata.get('message'):
            out.append('')
            out.append(metadata['message'])
        return '\n'.join(out)

    # ── update ────────────────────────────────────────────────────────────────
    if mode == 'update':
        raw_product = response.get('product')
        p = _normalise_product(raw_product) if raw_product else {}
        history = (raw_product or {}).get('pricing_history') or []
        out.append('**[MODE:update] Product Updated Successfully**')
        out.append('')
        if p:
            _product_detail_block(out, p, history, 'update')
        if metadata.get('message'):
            out.append('')
            out.append(metadata['message'])
        return '\n'.join(out)

    # ── bulk_adjust_stock ─────────────────────────────────────────────────────
    if mode == 'bulk_adjust_stock':
        rows_affected = metadata.get('rows_affected', 0)
        adjustment    = metadata.get('adjustment_amount', 0)
        updated = (
            response.get('updated_products') or
            response.get('products') or
            response.get('items') or
            response.get('data') or
            []
        )
        logger.info(f'bulk_adjust_stock: updated list has {len(updated)} items. '
                    f'Response keys: {list(response.keys())}')

        out.append('**[MODE:bulk_adjust_stock] Bulk Stock Adjustment**')
        out.append('')
        out.append(f'Rows Affected: {rows_affected}')
        sign = '+' if _safe_num(adjustment) > 0 else ''
        out.append(f'Adjustment Applied: {sign}{adjustment} units')
        out.append('')

        if updated:
            rows = []
            for p in updated:
                old = p.get('old_stock')
                new = p.get('new_stock')
                try:
                    diff = int(new) - int(old)
                    change = f'+{diff}' if diff > 0 else str(diff)
                except (TypeError, ValueError):
                    change = 'N/A'
                rows.append([
                    p.get('product_name') or 'N/A',
                    p.get('sku') or 'N/A',
                    str(p.get('product_number') or 'N/A'),
                    str(old) if old is not None else 'N/A',
                    str(new) if new is not None else 'N/A',
                    change,
                ])
            out.append(_md_table(
                ['Product Name', 'SKU', 'Product#', 'Old Stock', 'New Stock', 'Change'],
                rows,
            ))
        else:
            out.append('_No detailed product list returned._')
        return '\n'.join(out)

    # ── inventory_summary ─────────────────────────────────────────────────────
    if mode == 'inventory_summary':
        summary = (
            response.get('summary') or
            response.get('categories') or
            response.get('items') or
            response.get('data') or
            []
        )
        logger.info(f'inventory_summary: {len(summary)} rows. Response keys: {list(response.keys())}')
        threshold = next((v for v in (metadata.get('low_stock_threshold'),
                                      params.get('lowStockThreshold'), 70)
                          if v is not None))

        out.append('**[MODE:inventory_summary] Inventory Summary by Category**')
        out.append('')
        out.append(f'Low Stock Threshold: {threshold}')
        out.append('')

        if summary:
            rows = []
            for cat in summary:
                total_cost    = _safe_num(cat.get('total_inventory_cost'))
                total_revenue = _safe_num(cat.get('total_potential_revenue'))
                profit        = total_revenue - total_cost
                rows.append([
                    cat.get('category_name') or 'N/A',
                    str(cat.get('category_number') or 'N/A'),
                    str(cat.get('product_count') or 0),
                    _fmt_big(cat.get('total_stock') or 0),
                    _fmt_currency(total_cost),
                    _fmt_currency(total_revenue),
                    _fmt_currency(profit),
                ])
            out.append(_md_table(
                ['Category', 'Category#', 'Products', 'Total Stock',
                 'Inventory Cost', 'Potential Revenue', 'Profit'],
                rows,
            ))
        else:
            out.append('_No inventory summary data returned._')
        return '\n'.join(out)

    # ── low_stock ─────────────────────────────────────────────────────────────
    if mode == 'low_stock':
        raw_list = (
            response.get('alerts') or
            response.get('products') or
            response.get('low_stock_items') or
            response.get('items') or
            response.get('data') or
            response.get('records') or
            []
        )
        logger.info(f'low_stock: array key resolved, {len(raw_list)} items found. '
                    f'Available response keys: {list(response.keys())}')
        products    = [_normalise_product(p) for p in raw_list]
        # next()/is-None chain (not `or`) — threshold 0 = "out of stock" report
        threshold   = next((v for v in (metadata.get('low_stock_threshold'),
                                        params.get('lowStockThreshold'), 70)
                            if v is not None))
        alert_count = metadata.get('alert_count') or len(products)

        out.append('**[MODE:low_stock] Low Stock Alert Report**')
        out.append('')
        out.append(f'Threshold: {threshold} units')
        out.append(f'Total Alerts: {alert_count}')
        out.append('')

        if products:
            rows = []
            for p in products:
                currency = p.get('currency', 'USD')
                rows.append([
                    p.get('product_name') or 'N/A',
                    p.get('sku') or 'N/A',
                    str(p.get('product_number') or 'N/A'),
                    p.get('category_name') or 'N/A',
                    str(p.get('category_number') or 'N/A'),
                    str(p.get('stock') or 0),
                    _fmt_currency(p['retail'],    currency) if p.get('retail')    is not None else '',
                    _fmt_currency(p['promo'],     currency) if p.get('promo')     is not None else '',
                    _fmt_currency(p['wholesale'], currency) if p.get('wholesale') is not None else '',
                ])
            out.append(_md_table(
                ['Product Name', 'SKU', 'Product#', 'Category', 'Category#',
                 'Stock', 'Retail', 'Promo', 'Wholesale'],
                rows,
            ))
        else:
            all_keys = list(response.keys())
            out.append('_No low stock products found._')
            if all_keys:
                out.append('')
                out.append(f'_(Debug: SP response keys were: {", ".join(all_keys)})_')
        return '\n'.join(out)

    # ── price_history ─────────────────────────────────────────────────────────
    if mode == 'price_history':
        p       = _normalise_product(response.get('product')) if response.get('product') else {}
        history = response.get('price_history') or []
        currency= p.get('currency', 'USD')

        out.append('**[MODE:price_history] Price History**')
        out.append('')
        if p:
            out.append(f'Product: {p.get("product_name") or "N/A"}')
            out.append(f'SKU: {p.get("sku") or "N/A"}')
            out.append(f'Product#: {p.get("product_number") or "N/A"}')
            out.append('')

        if history:
            rows = []
            for h in history:
                eff_to = _fmt_date(h.get('effective_to')) if h.get('effective_to') else 'Present'
                rows.append([
                    h.get('price_type') or 'N/A',
                    _fmt_currency(h.get('price_value'), currency),
                    _fmt_date(h.get('effective_from')),
                    eff_to,
                    'Yes' if h.get('is_current') else 'No',
                ])
            out.append(_md_table(['Type', 'Price', 'From', 'To', 'Current'], rows))
        else:
            out.append('_No price history available._')
        return '\n'.join(out)

    # ── price_matrix ──────────────────────────────────────────────────────────
    if mode == 'price_matrix':
        raw_matrix = (
            response.get('matrix') or
            response.get('products') or
            response.get('items') or
            response.get('data') or
            []
        )
        logger.info(f'price_matrix: {len(raw_matrix)} rows. Response keys: {list(response.keys())}')
        products = [_normalise_product(p) for p in raw_matrix]

        out.append('**[MODE:price_matrix] Price Matrix Report**')
        out.append('')

        if products:
            rows = []
            for p in products:
                prices   = p.get('prices') or {}
                currency = p.get('currency', 'USD')
                rows.append([
                    p.get('product_name') or 'N/A',
                    p.get('sku') or 'N/A',
                    str(p.get('product_number') or 'N/A'),
                    p.get('category_name') or 'N/A',
                    str(p.get('category_number') or 'N/A'),
                    _fmt_currency(prices.get('Retail'),    currency) if prices.get('Retail')    is not None else '',
                    _fmt_currency(prices.get('Promo'),     currency) if prices.get('Promo')     is not None else '',
                    _fmt_currency(prices.get('Wholesale'), currency) if prices.get('Wholesale') is not None else '',
                ])
            out.append(_md_table(
                ['Product Name', 'SKU', 'Product#', 'Category', 'Category#',
                 'Retail', 'Promo', 'Wholesale'],
                rows,
            ))
        else:
            out.append('_No products with pricing details found._')
        return '\n'.join(out)

    # ── Fallback ──────────────────────────────────────────────────────────────
    out.append('[INFO] No data returned or unsupported mode.')
    if mode:
        out.append(f'Mode: {mode}')
    return '\n'.join(out)
