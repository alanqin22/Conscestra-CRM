"""Response formatter for Accounting Management — aligned with n8n Format Response v2ag.

Supports 14 modes with pipeline margin analytics (revenue, cost, margin, margin_pct).

v2ag changes vs v2aa (previously in Python):
  • NEW MODE: list_invoices_for_account
    Compact 5-column table for RP / Void Invoice form invoice dropdowns.
    Columns: Invoice # | Invoice ID | Status | Due Date | Balance Due
  • list_invoices: Revenue falls back to total_amount when pipeline revenue absent.
  • list_invoices / list_invoices_for_account: Balance Due falls back to balance_due
    when computed_balance_due is absent.
  • list_invoices: Added 'issued' → 'ISSUED' status mapping.
  • generate_invoice / get_invoice_360: has_line_items / cost_data_complete pipeline
    check; shows N/A when invoice has no line items.
  • Soft warning support: metadata code > 0 with status 'success' prepends info banner.
  • account_search: expanded columns with owner/contact fields
    (Owner First Name, Owner Last Name, Owner ID, Contact First Name, Contact Last Name, Contact ID).
  • account_balance_lookup: simplified to lightweight (balance + status only,
    no total_invoiced / total_paid columns).
  • Mode resolution: 3-layer with prefix-detection for list_invoices_for_account.

SUPPORTED MODES (14):
  generate_invoice            → Returns get_invoice_360 data (pipeline-based)
  record_payment              → Returns get_payment_360 data (pipeline-based)
  void_invoice                → Returns inline void confirmation
  list_invoices               → Paginated listing with revenue/cost/margin from pipeline
  list_invoices_for_account   → Account-scoped listing for inline form dropdowns
  get_invoice_360             → Full invoice with pipeline financials, orders, payments
  list_payments               → Paginated listing with pipeline financials
  get_payment_360             → Full payment with pipeline financials, owner, employee
  account_balance             → Account balance with pipeline-based totals
  account_balance_lookup      → Account name search matches (lightweight: name + balance)
  accounting_summary          → Totals, aging buckets, by_status, top revenue,
                                 top overdue, account margin analytics, product profitability
  account_search              → Typeahead: Name | ID | Email | Owner FN/LN/ID | Contact FN/LN/ID
  get_invoiceable_orders      → Orders ready to invoice for a given account
  list_employee               → Active employees for UI dropdowns: Name | UUID
"""

import json
import logging
from typing import Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================================
# HELPERS
# ============================================================================

def safe_number(n, fallback: float = 0) -> float:
    try:
        return float(n) if n is not None else fallback
    except (ValueError, TypeError):
        return fallback


def safe_json(s):
    try:
        return json.loads(s) if isinstance(s, str) else s
    except (json.JSONDecodeError, TypeError):
        return None


def format_currency(n, currency: str = 'USD') -> str:
    value = safe_number(n)
    return f"${value:,.2f}"


def format_big_number(n) -> str:
    return f"{int(safe_number(n)):,}"


def format_percent(n) -> str:
    if n is None:
        return 'N/A'
    return f"{safe_number(n) * 100:.1f}%"


def format_datetime(value) -> str:
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


def format_date(value) -> str:
    if not value:
        return 'N/A'
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return dt.strftime('%b %d, %Y')
        elif isinstance(value, datetime):
            return value.strftime('%b %d, %Y')
        return str(value)[:10]
    except (ValueError, AttributeError):
        return str(value)[:10] if value else 'N/A'


def truncate_uuid(uuid) -> str:
    if not uuid:
        return 'N/A'
    s = str(uuid)
    return f"{s[:8]}..." if len(s) > 8 else s


def full_uuid(uuid) -> str:
    return str(uuid) if uuid else 'N/A'


def get_mode_name(mode: str) -> str:
    names = {
        'generate_invoice':           'Invoice Generated',
        'record_payment':             'Payment Recorded',
        'void_invoice':               'Invoice Voided',
        'list_invoices':              'Invoice List',
        'list_invoices_for_account':  'Account Invoice List',
        'get_invoice_360':            'Invoice 360 Details',
        'list_payments':              'Payment List',
        'get_payment_360':            'Payment 360 Details',
        'account_balance':            'Account Balance',
        'account_balance_lookup':     'Account Balance Lookup',
        'accounting_summary':         'Accounting Summary',
        'account_search':             'Account Search',
        'get_invoiceable_orders':     'Invoiceable Orders',
        'list_employee':              'Employee List',
    }
    return names.get(mode, 'Unknown')


def _normalize_status(raw: str) -> str:
    """Normalize pipeline/raw status string to uppercase display label."""
    mapping = {
        'paid':      'PAID',
        'partial':   'PARTIAL',
        'unpaid':    'UNPAID',
        'overdue':   'OVERDUE',
        'cancelled': 'CANCELLED',
        'issued':    'ISSUED',
    }
    return mapping.get((raw or '').lower(), (raw or 'ISSUED').upper())


def _person_name(obj) -> str:
    """Extract display name from owner/employee object."""
    if not obj:
        return ''
    name = obj.get('employee_name') or ''
    if not name:
        first = obj.get('first_name', '')
        last = obj.get('last_name', '')
        name = f"{first} {last}".strip()
    email = obj.get('email', '')
    if email:
        return f"{name} ({email})" if name else email
    return name


def _infer_mode_from_response(r: Dict) -> str:
    """Layer 2 mode inference from SP response structure."""
    if isinstance(r.get('employees'), list):
        return 'list_employee'
    if isinstance(r.get('orders'), list) and not r.get('invoice') and not r.get('payments'):
        return 'get_invoiceable_orders'
    if isinstance(r.get('accounts'), list) and not r.get('invoice'):
        return 'account_search'
    if isinstance(r.get('matches'), list):
        return 'account_balance_lookup'
    if isinstance(r.get('invoices'), list):
        return 'list_invoices'
    if isinstance(r.get('payments'), list) and not r.get('invoice'):
        return 'list_payments'
    if r.get('invoice') and r.get('invoice', {}).get('orders') and r.get('invoice', {}).get('payments') is not None:
        return 'get_invoice_360'
    if r.get('payment') and r.get('payment', {}).get('invoice_id'):
        return 'get_payment_360'
    if r.get('balance') and r.get('balance', {}).get('account_id'):
        return 'account_balance'
    if r.get('summary') and r.get('summary', {}).get('totals'):
        return 'accounting_summary'
    return None


# ============================================================================
# MAIN FORMATTER
# ============================================================================

from app.core.text_clean import clean_obj


def format_response(db_rows: List[Dict[str, Any]], params: Dict[str, Any]) -> str:
    """Format database response based on mode.

    Mode resolution — 3-layer (mirrors n8n Format Response v2ag):
      Layer 1: params dict (explicit, from sql_builder)
      Layer 2: infer from SP response structure
      Layer 3: 'unknown' fallback

    Prefix-detection exception: when the explicit mode starts with the
    inferred mode and is longer (e.g. 'list_invoices_for_account' vs
    inferred 'list_invoices'), inference is a structural false positive
    and the explicit mode wins.
    """
    # Parse JSONB response
    response = {}
    if db_rows and len(db_rows) > 0:
        first_row = db_rows[0]
        if 'sp_accounting' in first_row:
            response = clean_obj(safe_json(first_row['sp_accounting']) or first_row['sp_accounting'])
        elif 'result' in first_row:
            response = safe_json(first_row['result']) or first_row['result']
        else:
            response = first_row

    if isinstance(response, str):
        response = safe_json(response) or {}

    # ── Python-side search filter (safety net for SP versions without p_search) ─
    # Applied here because this is guaranteed to run regardless of graph caching.
    _search_term = (params.get('search') or '').strip().lower()
    _mode_for_filter = (params.get('mode') or '').strip().lower()
    if _search_term and isinstance(response, dict):
        for _list_key in ('payments', 'invoices'):
            if _list_key in response and isinstance(response[_list_key], list):
                _before = len(response[_list_key])
                response[_list_key] = [
                    item for item in response[_list_key]
                    if _search_term in str(item.get('account_name') or '').lower()
                    or _search_term in str(item.get('invoice_number') or '').lower()
                    or _search_term in str(item.get('contact_name') or '').lower()
                ]
                _after = len(response[_list_key])
                if _before != _after:
                    import logging as _log
                    _log.getLogger(__name__).info(
                        f'[fmt-search-filter] {_list_key}: {_before}→{_after} for {_search_term!r}'
                    )
                    # Update metadata total_records/total_pages to match filtered count
                    if isinstance(response.get('metadata'), dict):
                        response['metadata']['total_records'] = _after
                        _ps = int(response['metadata'].get('page_size') or 50)
                        response['metadata']['total_pages'] = max(1, (_after + _ps - 1) // _ps)

    # --- Mode resolution (3-layer) ---
    mode_from_params = (params.get('mode') or '').strip().lower()
    mode_inferred = _infer_mode_from_response(response)

    if not mode_from_params:
        mode = mode_inferred or 'unknown'
    elif not mode_inferred or mode_from_params == mode_inferred:
        mode = mode_from_params
    elif mode_from_params.startswith(mode_inferred) and len(mode_from_params) > len(mode_inferred):
        # Prefix detection: explicit is a more-specific variant of what was inferred
        logger.info(
            f"Mode refinement: inferred '{mode_inferred}' is prefix of explicit '{mode_from_params}'. "
            "Using explicit mode (more specific)."
        )
        mode = mode_from_params
    else:
        # Mismatch: trust SP response structure (ground truth)
        logger.warning(
            f"Mode mismatch: params says '{mode_from_params}' but SP structure indicates "
            f"'{mode_inferred}'. Using inferred mode."
        )
        mode = mode_inferred

    logger.info(f"Formatting response for mode: {mode}")

    # ── Executive finance answer — pre-formatted by the shared executive
    # Q&A layer (decision headline + relevant sections); pass through.
    if mode == 'executive_question':
        return response.get('exec_markdown') or 'No executive data available.'

    # ── Web answer — pre-formatted by app/core/web_tools.py ──────────────────
    if mode == 'web_search':
        return response.get('web_markdown') or 'No web results found.'

    # ── UI-only marker modes — frontend opens the inline form ────────────────
    _ui_form_messages = {
        'show_invoice_form':       'Opening the Generate Invoice form below…',
        'show_payment_form':       'Opening the Record Payment form below…',
        'show_void_invoice_form':  'Opening the Void Invoice form below…',
    }
    if mode in _ui_form_messages:
        # Optional account pre-fill: "[MODE:show_invoice_form|Bob Brown]" tells
        # the frontend to pre-search the form's account typeahead.
        _pf = (params or {}).get('prefillAccount')
        _marker = f'[MODE:{mode}|{_pf}]' if _pf else f'[MODE:{mode}]'
        return f'{_marker}\n{_ui_form_messages[mode]}'

    metadata = response.get('metadata', {})

    # --- Error / soft-warning classification ---
    is_error = (
        metadata.get('status') == 'error'
        or (isinstance(metadata.get('code'), (int, float)) and metadata.get('code', 0) < 0)
    )
    is_soft_warning = (
        not is_error
        and metadata.get('status') == 'success'
        and isinstance(metadata.get('code'), (int, float))
        and metadata.get('code', 0) > 0
    )

    if is_error:
        return _format_error(metadata)

    # Route to formatter
    formatters = {
        'generate_invoice':          _fmt_generate_invoice,
        'record_payment':            _fmt_record_payment,
        'void_invoice':              _fmt_void_invoice,
        'list_invoices':             _fmt_list_invoices,
        'list_invoices_for_account': _fmt_list_invoices_for_account,
        'get_invoice_360':           _fmt_get_invoice_360,
        'list_payments':             _fmt_list_payments,
        'get_payment_360':           _fmt_get_payment_360,
        'account_balance':           _fmt_account_balance,
        'account_balance_lookup':    _fmt_account_balance_lookup,
        'accounting_summary':        _fmt_accounting_summary,
        'account_search':            _fmt_account_search,
        'get_invoiceable_orders':    _fmt_get_invoiceable_orders,
        'list_employee':             _fmt_list_employee,
    }

    formatter = formatters.get(mode, _fmt_generic)

    lines = []

    # Soft-warning banner (metadata code > 0, status success)
    if is_soft_warning:
        soft_msg = metadata.get('message', 'Invoice loaded with partial data')
        lines.append(
            f"> ⚠️ **Note:** {soft_msg}. Revenue, cost and margin fields may show $0.00 or N/A "
            "because this invoice has no line items recorded in the system."
        )
        lines.append('')

    lines.extend(formatter(response, metadata, params))

    # Footer
    lines.append('')
    lines.append('---')
    lines.append('Need anything else? Just ask!')
    lines.append('• Run accounting summary')
    lines.append('• Show account balance')
    lines.append('• Record payment')
    lines.append('• Generate invoice')
    lines.append('• List invoices / List payments')

    return '\n'.join(lines)


def _format_error(metadata: Dict) -> str:
    lines = [
        "### ERROR",
        f"**Time:** {datetime.now().strftime('%b %d, %Y %I:%M %p')}",
        f"**Error Code:** {metadata.get('code', -999)}",
        f"**Error Message:** {metadata.get('message', 'Unknown error occurred')}",
        "",
        "Please fix the input and try again.",
    ]
    return '\n'.join(lines)


# ============================================================================
# MODE FORMATTERS
# ============================================================================

def _fmt_generate_invoice(response: Dict, metadata: Dict, params: Dict) -> list:
    """generate_invoice — SP returns get_invoice_360 data with pipeline financials."""
    lines = []
    inv = response.get('invoice', {})
    if not inv:
        lines.append("Invoice details not available.")
        return lines

    lines.append('**New Invoice Created!**')
    lines.append('')
    lines.append('| Field | Details |')
    lines.append('|-------|---------|')
    lines.append(f"| **Invoice Number** | {inv.get('invoice_number', 'N/A')} |")
    lines.append(f"| **Invoice ID** | **{truncate_uuid(inv.get('invoice_id'))}** |")
    if inv.get('account_name'):
        lines.append(f"| **Account** | {inv['account_name']} |")
    lines.append(f"| **Account ID** | {truncate_uuid(inv.get('account_id'))} |")
    lines.append(f"| **Type** | {inv.get('invoice_type', 'invoice')} |")
    lines.append(f"| **Status** | {inv.get('status', 'issued')} |")

    # Pipeline check — show N/A when invoice has no line items
    has_pipeline = inv.get('has_line_items') is not False and inv.get('cost_data_complete') is not False
    if has_pipeline:
        lines.append(f"| **Revenue** | **{format_currency(inv.get('revenue', 0))}** |")
        if inv.get('cost') is not None:
            lines.append(f"| **Cost** | {format_currency(inv.get('cost', 0))} |")
            lines.append(f"| **Margin** | {format_currency(inv.get('margin', 0))} |")
            lines.append(f"| **Margin %** | {format_percent(inv.get('margin_pct'))} |")
    else:
        lines.append("| **Revenue** | N/A _(no line items)_ |")
        lines.append("| **Cost** | N/A _(no line items)_ |")
        lines.append("| **Margin** | N/A _(no line items)_ |")

    lines.append(f"| **Total Payments** | {format_currency(inv.get('total_payments', 0))} |")
    lines.append(f"| **Balance Due** | **{format_currency(inv.get('computed_balance_due', 0))}** |")
    lines.append(f"| **Currency** | {inv.get('currency', 'USD')} |")
    lines.append(f"| **Issue Date** | {format_date(inv.get('issue_date'))} |")
    lines.append(f"| **Due Date** | {format_date(inv.get('due_date'))} |")
    owner = inv.get('owner')
    if owner and owner.get('first_name'):
        lines.append(f"| **Owner** | {owner['first_name']} {owner.get('last_name', '')} ({owner.get('email', '')}) |")
    cb = inv.get('created_by')
    if cb and (cb.get('employee_name') or cb.get('first_name')):
        name = cb.get('employee_name') or f"{cb.get('first_name', '')} {cb.get('last_name', '')}"
        lines.append(f"| **Created By** | {name} |")
    lines.append('')

    orders = inv.get('orders', [])
    if orders:
        lines.append(f"**Linked Orders ({len(orders)})**")
        for idx, o in enumerate(orders, 1):
            order_num = o.get('order_number', truncate_uuid(o.get('order_id')))
            lines.append(f"   {idx}. {order_num} - {format_currency(o.get('total_amount', 0))} ({o.get('status', 'N/A')})")
        lines.append('')

    lines.append('_Invoice issued – due in 30 days._')
    return lines


def _fmt_record_payment(response: Dict, metadata: Dict, params: Dict) -> list:
    """record_payment — SP returns get_payment_360 data with pipeline financials."""
    lines = []
    p = response.get('payment', {})
    if not p:
        lines.append("Payment details not available.")
        return lines

    lines.append('**Payment Recorded Successfully**')
    lines.append('')
    lines.append('| Detail | Value |')
    lines.append('|--------|-------|')
    lines.append(f"| **Payment ID** | **{p.get('payment_id', 'N/A')}** |")
    lines.append(f"| **Invoice Number** | {p.get('invoice_number', 'N/A')} |")
    lines.append(f"| **Invoice ID** | {truncate_uuid(p.get('invoice_id'))} |")
    if p.get('account_name'):
        lines.append(f"| **Account** | {p['account_name']} |")
    lines.append(f"| **Amount Paid** | **{format_currency(p.get('amount', 0))}** |")
    lines.append(f"| **Currency** | {p.get('currency', 'USD')} |")
    if p.get('base_currency') and p.get('base_currency') != p.get('currency'):
        lines.append(f"| **Base Amount** | {format_currency(p.get('base_amount', 0), p.get('base_currency', 'USD'))} |")
    lines.append(f"| **Payment Method** | {p.get('payment_method', 'N/A')} |")
    lines.append(f"| **Payment Source** | {p.get('payment_source', 'N/A')} |")
    lines.append(f"| **Transaction Reference** | {p.get('transaction_reference', 'N/A')} |")
    lines.append(f"| **Status** | **{p.get('status', 'confirmed')}** |")
    lines.append(f"| **Payment Date** | {format_datetime(p.get('payment_date'))} |")
    if p.get('confirmed_at'):
        lines.append(f"| **Confirmed At** | {format_datetime(p['confirmed_at'])} |")

    if p.get('invoice_revenue') is not None:
        lines.append(f"| **Invoice Revenue** | {format_currency(p.get('invoice_revenue', 0))} |")
        lines.append(f"| **Total Payments on Invoice** | {format_currency(p.get('total_payments', 0))} |")
        lines.append(f"| **Remaining Balance** | **{format_currency(p.get('computed_balance_due', 0))}** |")
        lines.append(f"| **Invoice Payment Status** | {p.get('payment_status', 'N/A')} |")

    owner = p.get('owner')
    if owner and owner.get('first_name'):
        lines.append(f"| **Owner** | {owner['first_name']} {owner.get('last_name', '')} ({owner.get('email', '')}) |")
    cb = p.get('created_by')
    if cb and (cb.get('employee_name') or cb.get('first_name')):
        name = cb.get('employee_name') or f"{cb.get('first_name', '')} {cb.get('last_name', '')}"
        lines.append(f"| **Recorded By** | {name} |")

    lines.append('')
    lines.append('**✓ Payment recorded and applied to invoice.**')
    return lines


def _fmt_void_invoice(response: Dict, metadata: Dict, params: Dict) -> list:
    lines = []
    inv = response.get('invoice', {})

    lines.append('**Invoice Successfully Voided**')
    lines.append('')
    lines.append('| Status | Details |')
    lines.append('|--------|---------|')
    lines.append(f"| **Invoice ID** | **{truncate_uuid(inv.get('invoice_id'))}** |")
    lines.append(f"| **New Status** | **CANCELLED** |")
    if inv.get('voided_at'):
        lines.append(f"| **Voided At** | {format_datetime(inv['voided_at'])} |")
    if inv.get('voided_by'):
        lines.append(f"| **Voided By** | {truncate_uuid(inv['voided_by'])} |")
    lines.append('')
    lines.append('_This invoice has been permanently voided. Linked orders have been reset to Processing._')
    return lines


def _fmt_list_invoices(response: Dict, metadata: Dict, params: Dict) -> list:
    """list_invoices — pipeline fields: revenue, cost, margin, computed_balance_due.

    v2af: revenue falls back to total_amount; balance falls back to balance_due;
    currency passed to format_currency; 'issued' added to status map.
    """
    lines = []
    invoices = response.get('invoices', [])
    page = metadata.get('page', 1)
    total_pages = metadata.get('total_pages', 1)
    total_records = metadata.get('total_records', len(invoices))

    if invoices:
        lines.append('**📋 Invoice List**')
        lines.append('')
        lines.append(f"Page {page} of {total_pages} — {total_records} invoices")
        lines.append('')

        lines.append('| Invoice # | Invoice ID | Status | Account | Issue Date | Due Date | Revenue | Balance Due |')
        lines.append('|-----------|------------|--------|---------|------------|----------|---------|-------------|')
        for inv in invoices:
            status = _normalize_status(inv.get('status', ''))
            currency = inv.get('currency', 'USD')
            revenue = format_currency(
                inv.get('revenue') if inv.get('revenue') is not None else inv.get('total_amount', 0),
                currency
            )
            balance = format_currency(
                inv.get('computed_balance_due') if inv.get('computed_balance_due') is not None
                else inv.get('balance_due', 0),
                currency
            )
            lines.append(
                f"| {inv.get('invoice_number', 'N/A')} "
                f"| {inv.get('invoice_id', 'N/A')} "
                f"| {status} "
                f"| {inv.get('account_name', 'N/A')} "
                f"| {format_date(inv.get('issue_date'))} "
                f"| {format_date(inv.get('due_date'))} "
                f"| {revenue} "
                f"| {balance} |"
            )
        lines.append('')

    return lines


def _fmt_list_invoices_for_account(response: Dict, metadata: Dict, params: Dict) -> list:
    """list_invoices_for_account — compact 5-column table for RP / Void Invoice form dropdowns.

    Column contract (MUST NOT change — frontend _parseInvoiceRows() detects by header text):
      Invoice #   → numCol   (invoice_number for display label)
      Invoice ID  → idCol    (full UUID stored as selected value)
      Status      → statusCol
      Due Date    → date context
      Total       → amountCol  ← CHANGED from 'Balance Due' to 'Total'
                                  Balance Due = $0.00 for paid invoices which caused the
                                  dropdown to show ($0.00) for every fully-paid invoice.
                                  Total = invoice face value regardless of payment status.

    Cancelled invoices are included so staff can investigate / void them.
    No pagination — all invoices are returned.
    """
    lines = []
    invoices = response.get('invoices', [])

    if not invoices:
        lines.append('No invoices found for this account.')
        return lines

    lines.append(f"**{len(invoices)} invoice{'s' if len(invoices) != 1 else ''} found**")
    lines.append('')
    lines.append('| Invoice # | Invoice ID | Status | Due Date | Total |')
    lines.append('|-----------|------------|--------|----------|-------|')

    for inv in invoices:
        raw_status = inv.get('status') or inv.get('invoice_status') or ''
        status = _normalize_status(raw_status)
        currency = inv.get('currency', 'USD')
        # Prefer the authoritative stored total_amount (new SP field); fall back to
        # invoice_revenue (pipeline aggregate) then total_payments as a last resort.
        # Do NOT use computed_balance_due — it is $0.00 for fully-paid invoices,
        # which made every paid invoice show ($0.00) in the dropdown.
        # Use `is not None` guard so a legitimate 0-value invoice is not skipped.
        raw_total = next(
            (inv.get(k) for k in ('total_amount', 'revenue', 'total_payments')
             if inv.get(k) is not None),
            0
        )
        total = format_currency(raw_total, currency)
        lines.append(
            f"| {inv.get('invoice_number', 'N/A')} "
            f"| {inv.get('invoice_id', 'N/A')} "
            f"| {status} "
            f"| {format_date(inv.get('due_date'))} "
            f"| {total} |"
        )

    lines.append('')
    return lines


def _fmt_get_invoice_360(response: Dict, metadata: Dict, params: Dict) -> list:
    """get_invoice_360 — full invoice with pipeline financials, orders, payments.

    v2ag: has_line_items / cost_data_complete check for N/A display.
    """
    lines = []
    inv = response.get('invoice', {})
    if not inv:
        lines.append("**Invoice not found.**")
        return lines

    lines.append(f"**{inv.get('invoice_number', 'N/A')}**")
    lines.append('')
    lines.append('| Field | Value |')
    lines.append('|-------|-------|')
    lines.append(f"| **Invoice ID** | {truncate_uuid(inv.get('invoice_id'))} |")
    if inv.get('account_name'):
        lines.append(f"| **Account** | {inv['account_name']} |")
    lines.append(f"| **Account ID** | {truncate_uuid(inv.get('account_id'))} |")
    lines.append(f"| **Type** | {inv.get('invoice_type', 'invoice')} |")
    lines.append(f"| **Status** | {inv.get('status', 'N/A')} |")
    lines.append(f"| **Issue Date** | {format_date(inv.get('issue_date'))} |")
    lines.append(f"| **Due Date** | {format_date(inv.get('due_date'))} |")
    if inv.get('issued_at'):
        lines.append(f"| **Issued At** | {format_datetime(inv['issued_at'])} |")
    if inv.get('paid_at'):
        lines.append(f"| **Paid At** | {format_datetime(inv['paid_at'])} |")
    if inv.get('cancelled_at'):
        lines.append(f"| **Cancelled At** | {format_datetime(inv['cancelled_at'])} |")
    if inv.get('overdue_at'):
        lines.append(f"| **Overdue At** | {format_datetime(inv['overdue_at'])} |")

    # Pipeline check — show N/A when invoice has no line items
    has_pipeline = inv.get('has_line_items') is not False and inv.get('cost_data_complete') is not False
    if has_pipeline:
        lines.append(f"| **Revenue** | **{format_currency(inv.get('revenue', 0))}** |")
        if inv.get('cost') is not None:
            lines.append(f"| **Cost** | {format_currency(inv.get('cost', 0))} |")
            lines.append(f"| **Margin** | {format_currency(inv.get('margin', 0))} |")
            lines.append(f"| **Margin %** | {format_percent(inv.get('margin_pct'))} |")
    else:
        lines.append("| **Revenue** | N/A _(no line items)_ |")
        lines.append("| **Cost** | N/A _(no line items)_ |")
        lines.append("| **Margin** | N/A _(no line items)_ |")

    lines.append(f"| **Total Payments** | {format_currency(inv.get('total_payments', 0))} |")
    lines.append(f"| **Balance Due** | **{format_currency(inv.get('computed_balance_due', 0))}** |")
    lines.append(f"| **Currency** | {inv.get('currency', 'USD')} |")
    if inv.get('notes'):
        lines.append(f"| **Notes** | {inv['notes']} |")
    owner = inv.get('owner')
    if owner and owner.get('first_name'):
        lines.append(f"| **Owner** | {owner['first_name']} {owner.get('last_name', '')} ({owner.get('email', '')}) |")
    cb = inv.get('created_by')
    if cb and (cb.get('employee_name') or cb.get('first_name')):
        name = cb.get('employee_name') or f"{cb.get('first_name', '')} {cb.get('last_name', '')}"
        lines.append(f"| **Created By** | {name} |")
    ub = inv.get('updated_by')
    if ub and (ub.get('employee_name') or ub.get('first_name')):
        name = ub.get('employee_name') or f"{ub.get('first_name', '')} {ub.get('last_name', '')}"
        lines.append(f"| **Updated By** | {name} |")
    lines.append('')

    orders = inv.get('orders', [])
    if orders:
        lines.append(f"**Linked Orders ({len(orders)})**")
        lines.append('')
        lines.append('| Order # | Amount | Status | Date |')
        lines.append('|---------|--------|--------|------|')
        for o in orders:
            order_num = o.get('order_number', truncate_uuid(o.get('order_id')))
            lines.append(f"| {order_num} | {format_currency(o.get('total_amount', 0))} | {o.get('status', 'N/A')} | {format_date(o.get('order_date'))} |")
        lines.append('')

    payments = inv.get('payments', [])
    if payments:
        lines.append(f"**Payments ({len(payments)})**")
        lines.append('')
        lines.append('| Amount | Method | Status | Date | Source | Ref |')
        lines.append('|--------|--------|--------|------|--------|-----|')
        for p in payments:
            lines.append(
                f"| {format_currency(p.get('amount', 0))} "
                f"| {p.get('payment_method', 'N/A')} "
                f"| {p.get('status', 'N/A')} "
                f"| {format_date(p.get('payment_date'))} "
                f"| {p.get('payment_source', 'N/A')} "
                f"| {p.get('transaction_reference', 'N/A')} |"
            )
        lines.append('')

    return lines


def _fmt_list_payments(response: Dict, metadata: Dict, params: Dict) -> list:
    lines = []
    payments = response.get('payments', [])
    page = metadata.get('page', 1)
    total_pages = metadata.get('total_pages', 1)
    total_records = metadata.get('total_records', len(payments))

    if payments:
        lines.append('**💳 Payment List**')
        lines.append('')
        lines.append(f"Page {page} of {total_pages} — {total_records} payments")
        lines.append('')

        lines.append('| Payment ID | Invoice # | Invoice ID | Account | Amount | Date | Method | Status |')
        lines.append('|------------|-----------|------------|---------|--------|------|--------|--------|')
        for p in payments:
            lines.append(
                f"| {p.get('payment_id', 'N/A')} "
                f"| {p.get('invoice_number', 'N/A')} "
                f"| {p.get('invoice_id', 'N/A')} "
                f"| {p.get('account_name', 'N/A')} "
                f"| {format_currency(p.get('amount', 0))} "
                f"| {format_date(p.get('payment_date'))} "
                f"| {p.get('payment_method', 'N/A')} "
                f"| {p.get('status', 'confirmed')} |"
            )
        lines.append('')

    return lines


def _fmt_get_payment_360(response: Dict, metadata: Dict, params: Dict) -> list:
    """get_payment_360 — full payment with pipeline financials, owner, employee."""
    lines = []
    p = response.get('payment', {})
    if not p:
        lines.append("**Payment not found.**")
        return lines

    lines.append(f"**Payment {truncate_uuid(p.get('payment_id'))}**")
    lines.append('')
    lines.append('| Field | Value |')
    lines.append('|-------|-------|')
    lines.append(f"| **Payment ID** | {truncate_uuid(p.get('payment_id'))} |")
    lines.append(f"| **Invoice Number** | {p.get('invoice_number', 'N/A')} |")
    lines.append(f"| **Invoice ID** | {truncate_uuid(p.get('invoice_id'))} |")
    if p.get('account_name'):
        lines.append(f"| **Account** | {p['account_name']} |")
    lines.append(f"| **Account ID** | {truncate_uuid(p.get('account_id'))} |")
    if p.get('contact_id'):
        lines.append(f"| **Contact ID** | {truncate_uuid(p['contact_id'])} |")
    if p.get('opportunity_id'):
        lines.append(f"| **Opportunity ID** | {truncate_uuid(p['opportunity_id'])} |")
    if p.get('order_id'):
        lines.append(f"| **Order ID** | {truncate_uuid(p['order_id'])} |")
    lines.append(f"| **Amount** | **{format_currency(p.get('amount', 0), p.get('currency', 'USD'))}** |")
    lines.append(f"| **Currency** | {p.get('currency', 'USD')} |")
    if p.get('base_currency') and p.get('base_currency') != p.get('currency'):
        lines.append(f"| **Base Currency** | {p['base_currency']} |")
        lines.append(f"| **Exchange Rate** | {p.get('exchange_rate', 1)} |")
        lines.append(f"| **Base Amount** | {format_currency(p.get('base_amount', 0), p['base_currency'])} |")
    lines.append(f"| **Payment Method** | {p.get('payment_method', 'N/A')} |")
    lines.append(f"| **Payment Source** | {p.get('payment_source', 'N/A')} |")
    lines.append(f"| **Transaction Reference** | {p.get('transaction_reference', 'N/A')} |")
    if p.get('reference_number'):
        lines.append(f"| **Reference Number** | {p['reference_number']} |")
    lines.append(f"| **Status** | {p.get('status', 'N/A')} |")
    lines.append(f"| **Payment Date** | {format_datetime(p.get('payment_date'))} |")
    if p.get('confirmed_at'):
        lines.append(f"| **Confirmed At** | {format_datetime(p['confirmed_at'])} |")
    if p.get('failed_at'):
        lines.append(f"| **Failed At** | {format_datetime(p['failed_at'])} |")
    if p.get('refunded_at'):
        lines.append(f"| **Refunded At** | {format_datetime(p['refunded_at'])} |")
    if p.get('cancelled_at'):
        lines.append(f"| **Cancelled At** | {format_datetime(p['cancelled_at'])} |")
    if p.get('processing_fee'):
        lines.append(f"| **Processing Fee** | {format_currency(p['processing_fee'])} |")
    if p.get('net_amount'):
        lines.append(f"| **Net Amount** | {format_currency(p['net_amount'])} |")
    if p.get('reconciled') is not None:
        lines.append(f"| **Reconciled** | {'Yes' if p['reconciled'] else 'No'} |")
        if p.get('reconciled_at'):
            lines.append(f"| **Reconciled At** | {format_datetime(p['reconciled_at'])} |")
    if p.get('parent_payment_id'):
        lines.append(f"| **Parent Payment** | {truncate_uuid(p['parent_payment_id'])} |")

    if p.get('invoice_revenue') is not None:
        lines.append('| --- | --- |')
        lines.append(f"| **Invoice Revenue** | {format_currency(p.get('invoice_revenue', 0))} |")
        if p.get('invoice_cost') is not None:
            lines.append(f"| **Invoice Cost** | {format_currency(p.get('invoice_cost', 0))} |")
            lines.append(f"| **Invoice Margin** | {format_currency(p.get('invoice_margin', 0))} |")
            lines.append(f"| **Invoice Margin %** | {format_percent(p.get('invoice_margin_pct'))} |")
        lines.append(f"| **Total Payments on Invoice** | {format_currency(p.get('total_payments', 0))} |")
        lines.append(f"| **Remaining Balance** | **{format_currency(p.get('computed_balance_due', 0))}** |")
        lines.append(f"| **Invoice Payment Status** | {p.get('payment_status', 'N/A')} |")

    if p.get('notes'):
        lines.append(f"| **Notes** | {p['notes']} |")
    owner = p.get('owner')
    if owner and owner.get('first_name'):
        lines.append(f"| **Owner** | {owner['first_name']} {owner.get('last_name', '')} ({owner.get('email', '')}) |")
    cb = p.get('created_by')
    if cb and (cb.get('employee_name') or cb.get('first_name')):
        name = cb.get('employee_name') or f"{cb.get('first_name', '')} {cb.get('last_name', '')}"
        lines.append(f"| **Created By** | {name} ({cb.get('email', '')}) |")
    ub = p.get('updated_by')
    if ub and (ub.get('employee_name') or ub.get('first_name')):
        name = ub.get('employee_name') or f"{ub.get('first_name', '')} {ub.get('last_name', '')}"
        lines.append(f"| **Updated By** | {name} ({ub.get('email', '')}) |")
    lines.append(f"| **Created At** | {format_datetime(p.get('created_at'))} |")
    lines.append(f"| **Updated At** | {format_datetime(p.get('updated_at'))} |")
    lines.append('')

    return lines


def _fmt_account_balance(response: Dict, metadata: Dict, params: Dict) -> list:
    """account_balance — pipeline-based totals."""
    lines = []
    balance = response.get('balance', {})

    lines.append('**Account Balance Overview**')
    lines.append('')
    lines.append('| Summary | Value |')
    lines.append('|---------|-------|')
    lines.append(f"| **Account ID** | {truncate_uuid(balance.get('account_id'))} |")
    lines.append(f"| **Account Name** | {balance.get('account_name', 'N/A')} |")
    lines.append(f"| **Total Invoiced** | **{format_currency(balance.get('total_invoiced', 0))}** |")
    lines.append(f"| **Total Paid** | {format_currency(balance.get('total_paid', 0))} |")
    lines.append(f"| **Balance Due** | **{format_currency(balance.get('balance_due', 0))}** |")
    lines.append(f"| **Invoice Count** | {balance.get('invoice_count', 0)} |")
    lines.append(f"| **Overdue Count** | {balance.get('overdue_count', 0)} |")
    lines.append('')

    recent = balance.get('recent_invoices', [])
    if recent:
        lines.append('**Recent Invoices**')
        lines.append('')
        lines.append('| Invoice Number | Status | Issue Date | Due Date | Revenue | Balance Due |')
        lines.append('|----------------|--------|------------|----------|---------|-------------|')
        for i in recent:
            lines.append(
                f"| {i.get('invoice_number', 'N/A')} "
                f"| {i.get('status', 'N/A')} "
                f"| {format_date(i.get('issue_date'))} "
                f"| {format_date(i.get('due_date'))} "
                f"| {format_currency(i.get('revenue', 0))} "
                f"| {format_currency(i.get('balance_due', 0))} |"
            )

    return lines


def _fmt_account_balance_lookup(response: Dict, metadata: Dict, params: Dict) -> list:
    """account_balance_lookup — lightweight: name + balance + status + lookup link.

    v2ab/v2ag: simplified columns (no total_invoiced / total_paid).
    """
    lines = []
    matches = response.get('matches', [])

    if matches:
        lines.append('**Account Matches**')
        lines.append('')
        lines.append('| # | Account Name | Balance | Status | Balance Lookup |')
        lines.append('|---|--------------|---------|--------|----------------|')
        for idx, m in enumerate(matches, 1):
            balance_val = m.get('balance') if m.get('balance') is not None else 0
            balance = format_currency(balance_val)
            lookup_link = f"[🔎  Lookup](agent://account_balance/{m.get('account_id', '')})"
            lines.append(
                f"| {idx} "
                f"| {m.get('account_name', 'N/A')} "
                f"| {balance} "
                f"| {m.get('status', 'N/A')} "
                f"| {lookup_link} |"
            )
    else:
        lines.append('**No matching accounts found.**')

    return lines


def _fmt_accounting_summary(response: Dict, metadata: Dict, params: Dict) -> list:
    """accounting_summary — totals, aging, status, top revenue, overdue, margin, product profitability."""
    lines = []
    summary = response.get('summary', {})
    totals = summary.get('totals', {})
    aging = summary.get('aging_buckets', {})
    by_status = summary.get('by_status', {})
    top_revenue = summary.get('top_accounts_by_revenue', [])
    top_overdue = summary.get('top_overdue_accounts', [])
    account_margin = summary.get('account_margin_analytics', [])
    product_profit = summary.get('product_profitability', [])

    lines.append('**Accounting Summary**')
    lines.append('')
    lines.append('| Total Metrics | Value |')
    lines.append('|---------------|-------|')
    lines.append(f"| **Total Invoices** | {totals.get('total_invoices', 0)} |")
    lines.append(f"| **Total Paid** | {format_currency(totals.get('total_paid', 0))} |")
    lines.append(f"| **Total Outstanding** | {format_currency(totals.get('total_outstanding', 0))} |")
    lines.append(f"| **Total Overdue** | {format_currency(totals.get('total_overdue', 0))} |")
    lines.append(f"| **Invoices This Month** | {totals.get('invoices_this_month', 0)} |")
    lines.append(f"| **Payments This Month** | {totals.get('payments_this_month', 0)} |")
    lines.append('')

    if aging:
        lines.append('**AR Aging Buckets**')
        lines.append('')
        lines.append('| Aging Bucket | Amount |')
        lines.append('|--------------|--------|')
        lines.append(f"| **Current** | {format_currency(aging.get('current', 0))} |")
        lines.append(f"| **0–30 Days** | {format_currency(aging.get('0_30', 0))} |")
        lines.append(f"| **31–60 Days** | {format_currency(aging.get('31_60', 0))} |")
        lines.append(f"| **61–90 Days** | {format_currency(aging.get('61_90', 0))} |")
        lines.append(f"| **90+ Days** | {format_currency(aging.get('90_plus', 0))} |")
        lines.append('')

    if by_status:
        lines.append('**By Payment Status**')
        lines.append('')
        lines.append('| Status | Count |')
        lines.append('|--------|-------|')
        for status, count in by_status.items():
            lines.append(f"| {status} | {count} |")
        lines.append('')

    if top_revenue:
        lines.append('**Top Accounts by Revenue**')
        lines.append('')
        lines.append('| Account Name | Total Revenue |')
        lines.append('|--------------|---------------|')
        for a in top_revenue:
            lines.append(f"| {a.get('account_name', 'N/A')} | {format_currency(a.get('total', 0))} |")
        lines.append('')

    if top_overdue:
        lines.append('**Top Overdue Accounts**')
        lines.append('')
        lines.append('| Account Name | Balance Due |')
        lines.append('|--------------|-------------|')
        for a in top_overdue:
            lines.append(f"| {a.get('account_name', 'N/A')} | {format_currency(a.get('balance_due', 0))} |")
        lines.append('')

    if account_margin:
        def _cost_badge(pct):
            if pct is None:
                return '-'
            try:
                v = float(pct)
            except (TypeError, ValueError):
                return '-'
            if v >= 0.999:
                return '✓'
            if v > 0:
                return '⚠'
            return '-'

        lines.append('**Account Margin Analytics**')
        lines.append('')
        lines.append('| Account Name | Revenue | Cost | Margin | Margin % | Data |')
        lines.append('|--------------|---------|------|--------|----------|------|')
        for a in account_margin:
            lines.append(
                f"| {a.get('account_name', 'N/A')} "
                f"| {format_currency(a.get('total_revenue', 0))} "
                f"| {format_currency(a.get('total_cost', 0))} "
                f"| {format_currency(a.get('total_margin', 0))} "
                f"| {format_percent(a.get('margin_pct'))} "
                f"| {_cost_badge(a.get('cost_complete_pct'))} |"
            )
        lines.append('')
        lines.append('> ⚠️ **Data column:** ✓ all invoices have full cost data · ⚠ partial · - none — rows marked ⚠/- may understate cost / overstate margin.')
        lines.append('')

    if product_profit:
        lines.append('**Product Profitability**')
        lines.append('')
        lines.append('| Product Name | Revenue | Cost | Margin | Margin % |')
        lines.append('|--------------|---------|------|--------|----------|')
        for p in product_profit:
            lines.append(
                f"| {p.get('product_name', 'N/A')} "
                f"| {format_currency(p.get('revenue', 0))} "
                f"| {format_currency(p.get('cost', 0))} "
                f"| {format_currency(p.get('margin', 0))} "
                f"| {format_percent(p.get('margin_pct'))} |"
            )

    return lines


def _fmt_account_search(response: Dict, metadata: Dict, params: Dict) -> list:
    """account_search — expanded typeahead with owner/contact fields.

    Column contract (frontend _giRenderAccountDropdown() reads by header text):
      Account Name, Account ID, Email,
      Owner First Name, Owner Last Name, Owner ID,
      Contact First Name, Contact Last Name, Contact ID
    """
    lines = []
    accounts = response.get('accounts', [])

    if accounts:
        lines.append(
            '| Account Name | Account ID | Email | Owner First Name | Owner Last Name | Owner ID '
            '| Contact First Name | Contact Last Name | Contact ID |'
        )
        lines.append(
            '|--------------|------------|-------|-----------------|-----------------|----------'
            '|------------------|------------------|------------|'
        )
        for a in accounts:
            lines.append(
                f"| {a.get('account_name', 'N/A')} "
                f"| {a.get('account_id', '')} "
                f"| {a.get('email', '')} "
                f"| {a.get('owner_first_name', '')} "
                f"| {a.get('owner_last_name', '')} "
                f"| {a.get('owner_id', '')} "
                f"| {a.get('contact_first_name', '')} "
                f"| {a.get('contact_last_name', '')} "
                f"| {a.get('contact_id', '')} |"
            )
    else:
        lines.append('No matching accounts found.')

    return lines


def _fmt_get_invoiceable_orders(response: Dict, metadata: Dict, params: Dict) -> list:
    """get_invoiceable_orders — orders ready to be invoiced.

    Column contract (frontend _giParseOrders() reads by header text):
      Order Number | Amount | Status | Date | Order ID
    """
    lines = []
    orders = response.get('orders', [])

    if orders:
        lines.append(f"**{len(orders)} order{'' if len(orders) == 1 else 's'} available to invoice**")
        lines.append('')
        lines.append('| Order Number | Amount | Status | Date | Order ID |')
        lines.append('|--------------|--------|--------|------|----------|')
        for o in orders:
            lines.append(
                f"| {o.get('order_number', 'N/A')} "
                f"| {format_currency(o.get('total_amount', 0))} "
                f"| {o.get('status', 'N/A')} "
                f"| {format_date(o.get('order_date'))} "
                f"| {o.get('order_id', '')} |"
            )
    else:
        lines.append('No invoiceable orders found for this account.')

    return lines


def _fmt_list_employee(response: Dict, metadata: Dict, params: Dict) -> list:
    """list_employee — active employees for dropdowns."""
    lines = []
    employees = response.get('employees', [])

    if employees:
        num = len(employees)
        lines.append(f"**{num} active employee{'' if num == 1 else 's'}**")
        lines.append('')
        lines.append('| # | First Name | Last Name | Employee UUID |')
        lines.append('|---|------------|-----------|---------------|')
        for idx, e in enumerate(employees, 1):
            lines.append(
                f"| {idx} | {e.get('first_name', 'N/A')} | {e.get('last_name', 'N/A')} | {e.get('employee_uuid', '')} |"
            )
    else:
        lines.append('**No active employees found.**')

    return lines


def _fmt_generic(response: Dict, metadata: Dict, params: Dict) -> list:
    lines = ['**Action completed successfully**']
    if metadata.get('message'):
        lines.append(f"Message: {metadata['message']}")
    return lines
