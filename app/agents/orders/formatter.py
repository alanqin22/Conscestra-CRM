"""Response formatter for Orders — Python conversion of n8n Format Response v5.5.

CHANGELOG v5.5 — get_category, get_product mode handlers; entity mapping.
CHANGELOG v5.4 — order_id + order_number in JSON envelope for create_confirmation.
CHANGELOG v5.3 — context awareness; 'create_add_items' suppressed output.
CHANGELOG v5.2 — entity field ('orders' vs 'accounts') for tab correction.
CHANGELOG v5.1 — update_confirmation full order detail.
CHANGELOG v4.0 — account_search, list_employees pipe-delimited tables.

Modes handled:
  list, get_detail, create, update, delete,
  account_summary, category_summary, sales_summary,
  account_search, list_employees, contact_search,
  get_pricing, get_category, get_product.

Side-channel output:
  reportMode  — sub-classification of mode (e.g. 'detail', 'create_confirmation')
  entity      — 'orders' or 'accounts' (for HTML tab correction)
  order_id    — create_confirmation: order UUID for web page consumption
  order_number— create_confirmation: SO-YYYY-XXXXXX
  result      — structured dict (orders[], pricing{}, categories[], products[])
  context     — routing context ('update', 'create_add_items', 'create_order', None)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ACCOUNT_MODES = {'account_summary', 'account_search', 'contact_search'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v, fallback: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def _fmt_currency(v) -> str:
    return f'${_safe_float(v):,.2f}'


def _fmt_big(v) -> str:
    try:
        return f'{int(v):,}'
    except (TypeError, ValueError):
        return '0'


def _fmt_dt(value) -> str:
    if not value:
        return 'N/A'
    try:
        if isinstance(value, (datetime, date)):
            return value.strftime('%b %d, %Y %I:%M %p')
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.strftime('%b %d, %Y %I:%M %p')
    except (ValueError, AttributeError):
        return str(value) or 'N/A'


def _fmt_date(value) -> str:
    if not value:
        return 'N/A'
    try:
        if isinstance(value, date):
            return value.strftime('%b %d, %Y')
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.strftime('%b %d, %Y')
    except (ValueError, AttributeError):
        return str(value) or 'N/A'


def _full_uuid(v) -> str:
    return str(v) if v else 'N/A'


def _parse_response(db_rows: List[Dict]) -> Dict:
    if not db_rows:
        return {}
    first = db_rows[0]
    for key in ('result', 'sp_orders'):
        val = first.get(key)
        if val is not None:
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except json.JSONDecodeError:
                    pass
            elif isinstance(val, dict):
                return val
    if 'metadata' in first:
        return first
    return first


def _detect_mode(response: dict, params_mode: str) -> str:
    """Fallback mode detection from response shape."""
    if params_mode and params_mode != 'unknown':
        return params_mode
    meta_mode = (response.get('metadata') or {}).get('mode', '')
    if meta_mode:
        return meta_mode
    if response.get('accounts') and (response.get('metadata') or {}).get('mode') == 'account_search':
        return 'account_search'
    if response.get('employees'):
        return 'list_employees'
    if response.get('contacts'):
        return 'contact_search'
    if response.get('pricing') and response.get('pricing', {}).get('price_value') is not None:
        return 'get_pricing'
    if response.get('categories') and (response.get('metadata') or {}).get('mode') == 'get_category':
        return 'get_category'
    if response.get('products') and (response.get('metadata') or {}).get('mode') == 'get_product':
        return 'get_product'
    if response.get('orders'):
        return 'list'
    if response.get('order') and response.get('order', {}).get('items') is not None:
        return 'get_detail'
    if response.get('sales_summary') or (response.get('summary') and response.get('by_status')):
        return 'sales_summary'
    if response.get('account_summary') or (response.get('accounts') and not response.get('orders')):
        return 'account_summary'
    if response.get('category_summary'):
        return 'category_summary'
    return 'unknown'


# ============================================================================
# PUBLIC API
# ============================================================================

from app.core.text_clean import clean_obj


def format_response(db_rows: List[Dict], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format sp_orders DB rows into the output dict expected by main.py.

    Returns dict with keys:
      output, mode, reportMode, entity, order_id, order_number, result, context, success
    """
    params_mode = str(params.get('mode') or '').lower()

    # advance_statuses: fn_advance_order_statuses() returns table rows directly
    # (not wrapped in the sp_orders JSON envelope). Handle before _parse_response.
    if params_mode == 'advance_statuses':
        rows = db_rows or []
        total = sum(r.get('orders_advanced', 0) for r in rows)
        out = ['### 🔄 Order Status Advancement', '']
        if total == 0:
            out.append('No orders needed advancement — all are already at the correct stage.')
        else:
            out.append(f'**{total} orders advanced** across the lifecycle:')
            out.append('')
            out.append('| Transition | Orders Advanced |')
            out.append('|---|---|')
            for r in rows:
                if r.get('orders_advanced', 0) > 0:
                    out.append(f"| {r['transition']} | **{r['orders_advanced']}** |")
            out.append('')
            out.append('> ⚠️ Orders reaching **shipped** had invoices created and payments processed.')
            out.append('> Return window: customers have **30 days** from delivery to return for a full refund.')
            out.append('> Orders move to **completed** after 32 days (30-day window + 2-day buffer).')
        return {
            'output': '\n'.join(out),
            'mode': 'advance_statuses',
            'reportMode': 'advance_statuses',
            'entity': 'orders',
            'result': {'transitions': rows, 'total_advanced': total},
            'success': True,
        }

    # ── Executive answer — pre-formatted by the shared executive layer ───────
    if params_mode == 'executive_question':
        _resp = clean_obj(_parse_response(db_rows))
        return {
            'output': _resp.get('exec_markdown') or 'No executive data available.',
            'mode': 'executive_question',
            'reportMode': 'executive_question',
            'entity': 'orders',
            'success': True,
        }

    # ── Web answer — pre-formatted by app/core/web_tools.py ──────────────────
    if params_mode == 'web_search':
        _resp = clean_obj(_parse_response(db_rows))
        return {
            'output': _resp.get('web_markdown') or 'No web results found.',
            'mode': 'web_search',
            'reportMode': 'web_search',
            'entity': 'orders',
            'success': True,
        }

    response    = clean_obj(_parse_response(db_rows))
    mode        = _detect_mode(response, params_mode)
    context     = params.get('context')
    metadata    = response.get('metadata') or {}
    action      = response.get('action') or params.get('action')

    logger.info(f'Format Response (sp_orders v5.5) — mode={mode} action={action} context={context}')

    # ── UI-only marker modes — frontend opens inline form or shows message ─────
    if mode == 'show_order_form':
        return {
            'output': '[MODE:show_order_form]\nOpening the Create / Update Order form below…',
            'mode': mode,
            'reportMode': mode,
            'entity': 'orders',
            'result': None,
            'success': True,
        }
    if mode == 'ask_order_identifier':
        return {
            'output': ('Please provide the specific order ID, order number (e.g. SO-2025-000123), '
                       'or customer name for which you would like to see the order details.'),
            'mode': mode,
            'reportMode': mode,
            'entity': 'orders',
            'result': None,
            'success': True,
        }

    # ── Error check ───────────────────────────────────────────────────────────
    if metadata.get('status') == 'error' or (metadata.get('code') and metadata.get('code') != 0):
        output = (
            f"[ERROR] ERROR REPORT\n"
            f"Date: {_fmt_dt(datetime.utcnow())}\n"
            f"Error Code: {metadata.get('code', -999)}\n"
            f"Error Message: {metadata.get('message', 'Unknown error occurred')}\n\n"
            f"Please try again or contact support for assistance."
        )
        return {'output': output, 'error': True, 'mode': mode, 'success': False,
                'reportMode': 'error', 'entity': 'orders', 'result': None}

    # ── Suppress output for silent create_add_items calls ────────────────────
    if context == 'create_add_items':
        batch_ops  = metadata.get('batch_ops_applied', 0)
        has_errors = bool(metadata.get('batch_errors'))
        text = (
            f"[create_add_items] {batch_ops} ops applied with errors: {json.dumps(metadata.get('batch_errors'))}"
            if has_errors else
            f"[create_add_items] {batch_ops} item(s) added successfully"
        )
        return {
            'output': text, 'mode': mode, 'context': context,
            'reportMode': 'create_add_items', 'entity': 'orders',
            'success': True, 'result': None,
        }

    # ── Prepare data ──────────────────────────────────────────────────────────
    orders     = response.get('orders') or []
    order      = response.get('order') or None
    items_list = (order.get('items') if order else None) or []
    out: List[str] = []
    report_mode  = 'generic'
    result_data: dict = {}

    entity = 'accounts' if mode in ACCOUNT_MODES else 'orders'

    # ── account_search ────────────────────────────────────────────────────────
    if mode == 'account_search':
        report_mode = 'account_search'
        accounts = response.get('accounts') or []
        out.append('| Account Name | Account ID | Email |')
        out.append('| --- | --- | --- |')
        if not accounts:
            out.append('| No accounts found | — | — |')
        else:
            for acc in accounts:
                out.append(f"| {acc.get('account_name') or 'N/A'} | {_full_uuid(acc.get('account_id'))} | {acc.get('email') or ''} |")

    # ── list_employees ────────────────────────────────────────────────────────
    elif mode == 'list_employees':
        report_mode = 'list_employees'
        employees = response.get('employees') or []
        out.append('| Employee Name | Employee ID | Email |')
        out.append('| --- | --- | --- |')
        if not employees:
            out.append('| No employees found | — | — |')
        else:
            for emp in employees:
                out.append(f"| {emp.get('employee_name') or 'N/A'} | {_full_uuid(emp.get('employee_id'))} | {emp.get('email') or ''} |")

    # ── contact_search ────────────────────────────────────────────────────────
    elif mode == 'contact_search':
        report_mode = 'contact_search'
        contacts = response.get('contacts') or []
        out.append('| Contact Name | Contact ID | Email | Account Name | Account ID |')
        out.append('| --- | --- | --- | --- | --- |')
        if not contacts:
            out.append('| No contacts found | — | — | — | — |')
        else:
            for ct in contacts:
                out.append(
                    f"| {ct.get('contact_name') or 'N/A'} | {ct.get('contact_id') or ''} "
                    f"| {ct.get('email') or ''} | {ct.get('account_name') or 'N/A'} | {ct.get('account_id') or ''} |"
                )

    # ── get_pricing ───────────────────────────────────────────────────────────
    elif mode == 'get_pricing':
        report_mode = 'get_pricing'
        p = response.get('pricing') or {}
        result_data['pricing'] = p
        if p.get('price_value') is not None:
            out.append(
                f"${float(p['price_value']):.2f} | {p.get('product_pricing_id') or ''} "
                f"| {p.get('price_type') or ''} | {p.get('currency_code') or 'USD'} | SKU:{p.get('sku') or ''}"
            )
        else:
            out.append('N/A | — | — | —')

    # ── get_category ──────────────────────────────────────────────────────────
    elif mode == 'get_category':
        report_mode = 'get_category'
        cats = response.get('categories') or []
        result_data['categories'] = cats
        out.append('| Category ID | Category Name |')
        out.append('| --- | --- |')
        if not cats:
            out.append('| No categories found | — |')
        else:
            for c in cats:
                out.append(f"| {c.get('category_id') or ''} | {c.get('category_name') or 'N/A'} |")

    # ── get_product ───────────────────────────────────────────────────────────
    elif mode == 'get_product':
        report_mode = 'get_product'
        prods = response.get('products') or []
        result_data['products'] = prods
        out.append('| Product ID | Product Name | SKU | Category ID | Category Name |')
        out.append('| --- | --- | --- | --- | --- |')
        if not prods:
            out.append('| No products found | — | — | — | — |')
        else:
            for p in prods:
                out.append(
                    f"| {p.get('product_id') or ''} | {p.get('product_name') or 'N/A'} "
                    f"| {p.get('sku') or ''} | {p.get('category_id') or ''} | {p.get('category_name') or ''} |"
                )

    # ── list ──────────────────────────────────────────────────────────────────
    elif mode == 'list':
        report_mode = 'list'
        if orders:
            result_data['orders'] = orders
            pg = {
                'page':         metadata.get('page', 1),
                'pageSize':     metadata.get('page_size', 50),
                'totalRecords': metadata.get('total_records', len(orders)),
                'totalPages':   metadata.get('total_pages', 1),
                'sortBy':       metadata.get('sort_by', 'order_date'),
                'sortOrder':    metadata.get('sort_order', 'DESC'),
            }
            # Use '·' (not '|') as the separator: any line containing a pipe is
            # parsed as a table row by the orchestrator card renderer, which would
            # turn this one-liner into a stray phantom table (extra vertical space).
            out.append(f"**Page:** {pg['page']} of {pg['totalPages']} · **Total:** {pg['totalRecords']} orders")
            search_term = (params.get('search') or '').strip()
            if search_term:
                out.append(f'**Search:** "{search_term}"')
            out.append('')
            out.append('### Orders Summary')
            out.append('| # | Order Number | Order ID | Account | Contact | Date | Status | Total | Items |')
            out.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- |')
            for idx, o in enumerate(orders):
                badge = {'completed': '✅', 'cancelled': '❌', 'shipped': '🚚', 'delivered': '📦'}.get((o.get('status') or '').lower(), '🕐')
                out.append(
                    f"| {idx+1} | {o.get('order_number') or 'N/A'} | {_full_uuid(o.get('order_id'))} {badge} "
                    f"| {o.get('account_name') or 'N/A'} | {o.get('contact_name') or 'N/A'} "
                    f"| {_fmt_dt(o.get('order_date'))} | {o.get('status') or 'Unknown'} "
                    f"| {_fmt_currency(o.get('total_amount', 0))} | {o.get('item_count') or len(o.get('items') or [])} |"
                )
                if o.get('is_deleted') or o.get('deleted_at'):
                    out.append(f"| | | | | **[ARCHIVED]** | Deleted: {_fmt_dt(o.get('deleted_at'))} | | | |")
            out.append('')
            for idx, o in enumerate(orders):
                ois = o.get('items') or []
                out.append(f"### Items for Order #{idx+1} ({o.get('order_number') or _full_uuid(o.get('order_id'))})")
                if ois:
                    out.append('| # | Product | SKU | Qty | Price | Line Total | Price Type |')
                    out.append('| --- | --- | --- | --- | --- | --- | --- |')
                    for i, item in enumerate(ois):
                        out.append(
                            f"| {i+1} | {item.get('product_name') or 'N/A'} | {item.get('sku') or 'N/A'} "
                            f"| {item.get('quantity', 0)} | {_fmt_currency(item.get('price_value', 0))} "
                            f"| {_fmt_currency(item.get('line_total', 0))} | {item.get('price_type') or 'N/A'} |"
                        )
                    out.append('')
                else:
                    out.append('_No items_')
                    out.append('')
        else:
            out.append('**No orders found matching your criteria.**')
            out.append('')

    # ── get_detail ────────────────────────────────────────────────────────────
    elif mode == 'get_detail':
        report_mode = 'detail'
        if order:
            out.append(f"**Order Number:** {order.get('order_number') or 'N/A'}")
            out.append(f"**Order ID:** {_full_uuid(order.get('order_id'))}")
            out.append(f"**Status:** {order.get('status') or 'Unknown'}")
            out.append(f"**Order Date:** {_fmt_dt(order.get('order_date'))}")
            out.append('')
            out.append('**Account & Contact**')
            out.append(f"   Account: {order.get('account_name') or 'N/A'}")
            out.append(f"   Contact: {order.get('contact_name') or 'N/A'}")
            out.append(f"   Email:   {order.get('contact_email') or 'N/A'}")
            if order.get('contact_phone'):
                out.append(f"   Phone:   {order['contact_phone']}")
            out.append('')
            # Billing address
            ba = order.get('billing_address') or {}
            if ba:
                parts = [ba.get('line1'), ba.get('line2'),
                         ', '.join(filter(None, [ba.get('city'), ba.get('province'), ba.get('postal_code')])),
                         ba.get('country')]
                addr_lines = [p for p in parts if p]
                out.append('**Billing Address**')
                for line in addr_lines:
                    out.append(f"   {line}")
                out.append('')
            # Shipping address
            sa = order.get('shipping_address') or {}
            if sa:
                # Only show if different from billing.
                # Strip None-valued keys before comparing so that
                # {'line2': None} and {} (None dropped on serialization)
                # are treated as identical.
                def _compact(d: dict) -> dict:
                    return {k: v for k, v in d.items() if v is not None}
                same = (_compact(sa) == _compact(ba))
                parts = [sa.get('line1'), sa.get('line2'),
                         ', '.join(filter(None, [sa.get('city'), sa.get('province'), sa.get('postal_code')])),
                         sa.get('country')]
                addr_lines = [p for p in parts if p]
                out.append('**Shipping Address**')
                if same:
                    out.append('   Same as billing address')
                else:
                    for line in addr_lines:
                        out.append(f"   {line}")
                out.append('')
            if items_list:
                out.append(f"**Order Items ({len(items_list)})**")
                out.append('')
                out.append('| # | Product | SKU | Category | Qty | Price | Line Total | Price Type |')
                out.append('| --- | --- | --- | --- | --- | --- | --- | --- |')
                for idx, item in enumerate(items_list):
                    out.append(
                        f"| {idx+1} | {item.get('product_name') or 'N/A'} | {item.get('sku') or 'N/A'} "
                        f"| {item.get('category_name') or 'N/A'} | {item.get('quantity', 0)} "
                        f"| {_fmt_currency(item.get('price_value', 0))} | {_fmt_currency(item.get('line_total', 0))} "
                        f"| {item.get('price_type') or 'N/A'} |"
                    )
                out.append('')
            else:
                out.append('**Order Items:** No items')
                out.append('')
            summary = (order.get('summary') or {})
            if summary:
                out.append('**Order Totals**')
                if summary.get('total_items')    is not None: out.append(f"   Items:    {summary['total_items']}")
                if summary.get('total_quantity') is not None: out.append(f"   Quantity: {summary['total_quantity']}")
                if summary.get('total_amount')   is not None: out.append(f"   **Total:  {_fmt_currency(summary['total_amount'])}**")
                out.append('')
            out.append('**Audit Information**')
            if order.get('created_by_name') or order.get('created_by_id'):
                out.append(f"   Created By: {order.get('created_by_name') or order.get('created_by_id')}")
            if order.get('created_at'): out.append(f"   Created:    {_fmt_dt(order['created_at'])}")
            if order.get('updated_by_name') or order.get('updated_by_id'):
                out.append(f"   Updated By: {order.get('updated_by_name') or order.get('updated_by_id')}")
            if order.get('updated_at'): out.append(f"   Updated:    {_fmt_dt(order['updated_at'])}")
            if order.get('deleted_at') or order.get('is_deleted'):
                out.append(f"   **[ARCHIVED]** Deleted: {_fmt_dt(order.get('deleted_at'))}")
            out.append('')
        else:
            out.append('**No orders found matching your criteria.**')
            out.append('')

    # ── create_confirmation ───────────────────────────────────────────────────
    elif mode == 'create':
        report_mode = 'create_confirmation'
        if order:
            out.append('**✅ ORDER CREATED SUCCESSFULLY!**')
            out.append('')
            out.append(f"**Order Number:** {order.get('order_number') or 'N/A'}")
            out.append(f"**Order ID:** {_full_uuid(order.get('order_id'))}")
            out.append(f"**Account:** {order.get('account_name') or 'N/A'}")
            out.append(f"**Contact:** {order.get('contact_name') or 'N/A'}")
            out.append(f"**Status:** {order.get('status') or 'pending'}")
            out.append(f"**Order Date:** {_fmt_dt(order.get('order_date'))}")
            if order.get('created_by_name') or order.get('created_by_id'):
                out.append(f"**Created By:** {order.get('created_by_name') or order.get('created_by_id')}")
            out.append('')
            ois = order.get('items') or items_list or []
            if ois:
                out.append('**Initial Items:**')
                for idx, item in enumerate(ois):
                    out.append(
                        f"   {idx+1}. {item.get('product_name') or 'N/A'} — "
                        f"Qty: {item.get('quantity', 0)} × {_fmt_currency(item.get('price_value', 0))} "
                        f"({item.get('price_type') or 'N/A'})"
                    )
                out.append('')
            if metadata.get('message'):
                out.append(f"_{metadata['message']}_")
                out.append('')

    # ── update_confirmation ───────────────────────────────────────────────────
    elif mode == 'update':
        report_mode = 'update_confirmation'
        if order:
            action_label = (action or 'Update').replace('_', ' ').title()
            out.append(f"**✅ ORDER {action_label.upper()} SUCCESSFUL!**")
            out.append('')
            out.append('**Order Reference**')
            out.append(f"   Order Number: {order.get('order_number') or 'N/A'}")
            out.append(f"   Order ID:     {_full_uuid(order.get('order_id'))}")
            out.append(f"   Status:       {order.get('status') or 'Unknown'}")
            out.append(f"   Order Date:   {_fmt_dt(order.get('order_date'))}")
            if action: out.append(f"   Action:       {action}")
            out.append('')
            out.append('**Account & Contact**')
            out.append(f"   Account:  {order.get('account_name') or 'N/A'}")
            out.append(f"   Contact:  {order.get('contact_name') or 'N/A'}")
            if order.get('contact_email'): out.append(f"   Email:    {order['contact_email']}")
            if order.get('contact_phone'): out.append(f"   Phone:    {order['contact_phone']}")
            out.append('')
            update_items = items_list or (order.get('items') or [])
            if update_items:
                out.append(f"**Order Items ({len(update_items)})**")
                out.append('')
                out.append('| # | Product | SKU | Category | Qty | Price | Line Total | Price Type |')
                out.append('| --- | --- | --- | --- | --- | --- | --- | --- |')
                for idx, item in enumerate(update_items):
                    out.append(
                        f"| {idx+1} | {item.get('product_name') or 'N/A'} | {item.get('sku') or 'N/A'} "
                        f"| {item.get('category_name') or 'N/A'} | {item.get('quantity', 0)} "
                        f"| {_fmt_currency(item.get('price_value', 0))} | {_fmt_currency(item.get('line_total', 0))} "
                        f"| {item.get('price_type') or 'N/A'} |"
                    )
                out.append('')
            affected = response.get('item') or {}
            if affected:
                out.append(f"**Affected Item ({action_label})**")
                out.append(f"   Product:    {affected.get('product_name') or 'N/A'}")
                if affected.get('sku'):           out.append(f"   SKU:        {affected['sku']}")
                if affected.get('category_name'): out.append(f"   Category:   {affected['category_name']}")
                out.append(f"   Quantity:   {affected.get('quantity', 0)}")
                if affected.get('price_value') is not None: out.append(f"   Price:      {_fmt_currency(affected['price_value'])}")
                if affected.get('line_total')  is not None: out.append(f"   Line Total: {_fmt_currency(affected['line_total'])}")
                if affected.get('price_type'):              out.append(f"   Price Type: {affected['price_type']}")
                out.append('')
            summary = order.get('summary') or response.get('totals') or {}
            if summary:
                out.append('**Order Totals**')
                if summary.get('total_items')    is not None: out.append(f"   Items:    {summary['total_items']}")
                if summary.get('total_quantity') is not None: out.append(f"   Quantity: {summary['total_quantity']}")
                if summary.get('total_amount')   is not None: out.append(f"   **Total:  {_fmt_currency(summary['total_amount'])}**")
                out.append('')
            out.append('**Audit Information**')
            if order.get('created_by_name') or order.get('created_by_id'):
                out.append(f"   Created By: {order.get('created_by_name') or order.get('created_by_id')}")
            if order.get('created_at'): out.append(f"   Created:    {_fmt_dt(order['created_at'])}")
            if order.get('updated_by_name') or order.get('updated_by_id'):
                out.append(f"   Updated By: {order.get('updated_by_name') or order.get('updated_by_id')}")
            if order.get('updated_at'): out.append(f"   Updated:    {_fmt_dt(order['updated_at'])}")
            if order.get('deleted_at') or order.get('is_deleted'):
                out.append(f"   **[ARCHIVED]** Deleted: {_fmt_dt(order.get('deleted_at'))}")
            out.append('')
            if metadata.get('message'):
                out.append(f"_{metadata['message']}_")
                out.append('')

    # ── delete_confirmation ───────────────────────────────────────────────────
    elif mode == 'delete':
        report_mode = 'delete_confirmation'
        out.append('**🗑️ ORDER DELETED PERMANENTLY!**')
        out.append('')
        if response.get('deleted_order_id'):
            out.append(f"**Deleted Order ID:** {_full_uuid(response['deleted_order_id'])}")
        if response.get('deleted_items_count', 0) > 0:
            out.append(f"**Deleted Items:** {response['deleted_items_count']} item(s)")
        out.append('')
        out.append('**⚠️ WARNING:** This deletion is permanent and cannot be undone.')
        out.append('')
        if metadata.get('message'):
            out.append(f"_{metadata['message']}_")
            out.append('')

    # ── account_summary ───────────────────────────────────────────────────────
    elif mode == 'account_summary':
        report_mode = 'account_summary'
        accs     = response.get('accounts') or response.get('account_summary') or []
        summary  = response.get('summary') or {}
        if not summary and accs:
            summary = {
                'total_accounts': len(accs),
                'total_revenue':  sum(_safe_float(a.get('total_revenue') or a.get('total_amount')) for a in accs),
                'total_orders':   sum(_safe_float(a.get('order_count')) for a in accs),
                'total_items':    sum(_safe_float(a.get('total_items')) for a in accs),
            }
        if accs:
            if any(summary.get(k) for k in ('total_accounts', 'total_revenue', 'total_orders')):
                out.append('**Summary Statistics**')
                if summary.get('total_accounts'): out.append(f"   Accounts:      {_fmt_big(summary['total_accounts'])}")
                if summary.get('total_orders'):   out.append(f"   Total Orders:  {_fmt_big(summary['total_orders'])}")
                if summary.get('total_items'):    out.append(f"   Total Items:   {_fmt_big(summary['total_items'])}")
                if summary.get('total_revenue'):  out.append(f"   Total Revenue: {_fmt_currency(summary['total_revenue'])}")
                out.append('')
            out.append('### Account Performance')
            out.append('| Rank | Account | Orders | Items | Revenue | Avg Order |')
            out.append('| --- | --- | --- | --- | --- | --- |')
            for idx, acc in enumerate(accs):
                rev = _safe_float(acc.get('total_revenue') or acc.get('total_amount'))
                cnt = _safe_float(acc.get('order_count', 0))
                avg = rev / cnt if cnt > 0 else 0
                out.append(
                    f"| {idx+1} | {acc.get('account_name') or 'N/A'} "
                    f"| {acc.get('order_count', 0)} | {_fmt_big(acc.get('total_items', 0))} "
                    f"| {_fmt_currency(rev)} | {_fmt_currency(avg)} |"
                )
            out.append('')
        else:
            out.append('No account data found for the specified period.')
            out.append('')

    # ── category_summary ──────────────────────────────────────────────────────
    elif mode == 'category_summary':
        report_mode = 'category_summary'
        cats    = response.get('categories') or response.get('category_summary') or []
        summary = response.get('summary') or {}
        if not summary and cats:
            summary = {
                'total_categories': len(cats),
                'total_revenue':    sum(_safe_float(c.get('total_amount') or c.get('total_revenue')) for c in cats),
                'total_quantity':   sum(_safe_float(c.get('total_quantity')) for c in cats),
            }
        if cats:
            if any(summary.get(k) for k in ('total_categories', 'total_revenue', 'total_quantity')):
                out.append('**Summary Statistics**')
                if summary.get('total_categories'): out.append(f"   Categories:    {_fmt_big(summary['total_categories'])}")
                if summary.get('total_revenue'):    out.append(f"   Total Revenue: {_fmt_currency(summary['total_revenue'])}")
                if summary.get('total_quantity'):   out.append(f"   Total Qty Sold:{_fmt_big(summary['total_quantity'])}")
                out.append('')
            total_rev = sum(_safe_float(c.get('total_amount') or c.get('total_revenue')) for c in cats)
            out.append('### Category Breakdown')
            out.append('| Rank | Category | Orders | Qty Sold | Revenue | % of Total |')
            out.append('| --- | --- | --- | --- | --- | --- |')
            for idx, cat in enumerate(cats):
                rev = _safe_float(cat.get('total_amount') or cat.get('total_revenue'))
                pct = f"{(rev / total_rev * 100):.1f}" if total_rev > 0 else '0.0'
                out.append(
                    f"| {idx+1} | {cat.get('category_name') or 'N/A'} "
                    f"| {cat.get('order_count', 0)} | {_fmt_big(cat.get('total_quantity', 0))} "
                    f"| {_fmt_currency(rev)} | {pct}% |"
                )
            out.append('')
        else:
            out.append('No category data found for the specified period.')
            out.append('')

    # ── sales_summary ─────────────────────────────────────────────────────────
    elif mode == 'sales_summary':
        report_mode = 'sales_summary'
        summary   = response.get('summary') or {}
        by_status = response.get('by_status') or response.get('sales_summary') or []
        if not summary and by_status:
            tot = sum(_safe_float(r.get('order_count')) for r in by_status)
            rev = sum(_safe_float(r.get('total_amount') or r.get('total_revenue')) for r in by_status)
            summary = {'total_orders': tot, 'total_revenue': rev,
                       'avg_order_value': rev / tot if tot > 0 else 0}
        out.append('**Overall Performance**')
        for key, label in [
            ('total_orders', 'Total Orders'), ('total_revenue', 'Total Revenue'),
            ('total_items', 'Total Items'), ('total_quantity', 'Total Quantity'),
            ('avg_order_value', 'Avg Order Value'), ('unique_customers', 'Unique Customers'),
        ]:
            v = summary.get(key)
            if v is not None:
                fmt_v = _fmt_currency(v) if key in ('total_revenue', 'avg_order_value') else _fmt_big(v)
                out.append(f"   {label}: {fmt_v}")
        out.append('')
        if by_status:
            total = sum(_safe_float(r.get('order_count')) for r in by_status)
            out.append('### Orders by Status')
            out.append('| Status | Count | Revenue | % of Orders |')
            out.append('| --- | --- | --- | --- |')
            for row in by_status:
                cnt = _safe_float(row.get('order_count', 0))
                pct = f"{(cnt / total * 100):.1f}" if total > 0 else '0.0'
                out.append(
                    f"| {row.get('status') or row.get('date') or 'N/A'} "
                    f"| {_fmt_big(cnt)} "
                    f"| {_fmt_currency(row.get('total_amount') or row.get('total_revenue', 0))} "
                    f"| {pct}% |"
                )
            out.append('')

    # ── Fallback ──────────────────────────────────────────────────────────────
    else:
        out.append('[WARNING] Could not determine specific report format.')
        out.append(f'Mode: {mode}')
        out.append('')

    # ── Assemble result ───────────────────────────────────────────────────────
    response_dict: Dict[str, Any] = {
        'output':      '\n'.join(out),
        'mode':        mode,
        'reportMode':  report_mode,
        'entity':      entity,
        'success':     True,
    }
    if context:
        response_dict['context'] = context
    if result_data:
        response_dict['result'] = result_data
    if report_mode == 'create_confirmation' and order:
        response_dict['order_id']     = order.get('order_id')
        response_dict['order_number'] = order.get('order_number')

    return response_dict
