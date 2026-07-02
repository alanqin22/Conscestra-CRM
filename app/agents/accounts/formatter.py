"""Response formatter for Account Management — aligned with n8n Format Response v2d.

Supports 11 modes.

v2d changes vs v2c:
  GET mode: account return object now includes flattened billing_line1/2,
  billing_city/province/postal/country and shipping equivalents alongside
  the existing nested billing_address / shipping_address objects.
  This allows normalizeAccountData() in the HTML frontend to populate the
  Update Account Form with shipping data without extra parsing.

v2c changes vs v2b:
  TIMELINE: header is now **N. TYPE** (single word); Date: → Created:; adds Subject:/Category: fields.
  TIMELINE: account_id read from metadata.account_id when top-level is empty string.
  FINANCIALS: reads nested fin.orders/fin.invoices/fin.payments/fin.opportunities.
  FINANCIALS: outputs Orders/Invoices/Payments/Opportunities sections.

v2b changes vs v2a:
  Full LIST mode formatter.
  type normalization: 'other'/'internal' → 'customer'.
  Location field built from street/city/province/country.
  Revenue formatted as currency in Relationships block.
  Pagination header (**Page:** / **Total:**) read by web page parser.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ============================================================================
# HELPERS
# ============================================================================

def _safe_num(n, fallback: float = 0.0) -> float:
    try:
        return float(n) if n is not None else fallback
    except (ValueError, TypeError):
        return fallback


def _fmt_currency(n) -> str:
    return f"${_safe_num(n):,.2f}"


def _fmt_big(n) -> str:
    return f"{int(_safe_num(n)):,}"


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


def _fmt_uuid(u) -> str:
    return str(u) if u else 'N/A'


def _fmt_addr(addr: dict) -> str:
    if not addr:
        return 'N/A'
    parts = []
    for f in ('street', 'line2', 'city', 'province', 'postal_code', 'country'):
        if addr.get(f):
            parts.append(addr[f])
    return ', '.join(parts) if parts else 'N/A'


def _build_location(a: dict) -> str:
    """One-line location from flat account fields (list mode)."""
    parts = [a[f] for f in ('city', 'province', 'country') if a.get(f)]
    return ', '.join(parts) if parts else 'N/A'


def _normalize_type(raw) -> str:
    """Map raw account type to one of the tokens the web page recognises."""
    t = (raw or '').lower()
    return {'customer':   'CUSTOMER',   'partner':  'PARTNER',
            'vendor':     'VENDOR',     'prospect': 'PROSPECT',
            'competitor': 'COMPETITOR', 'reseller': 'RESELLER',
            'investor':   'INVESTOR',   'internal': 'INTERNAL'}.get(t, 'CUSTOMER')


def _mode_name(m: str) -> str:
    return {
        'list':       'List Accounts',
        'get':        'Account 360 View',
        'create':     'Create Account',
        'update':     'Update Account',
        'timeline':   'Activity Timeline',
        'financials': 'Financial Summary',
        'duplicates': 'Find Duplicates',
        'merge':      'Merge Duplicates',
        'archive':    'Archive Account',
        'restore':    'Restore Account',
        'summary':    'Account Statistics',
    }.get(m, 'Account Management')


def _parse_response(db_rows: List[Dict]) -> Dict:
    """
    Extract the sp_accounts JSON response from raw DB rows.

    The psycopg2 execute returns rows with key 'result' (aliased in the
    SELECT sp_accounts(...) AS result query).
    """
    if not db_rows:
        return {}

    first = db_rows[0]

    # psycopg2 with RealDictCursor + json parsing in database.py returns
    # the parsed dict under 'sp_accounts' or 'result'.
    for key in ('sp_accounts', 'result'):
        val = first.get(key)
        if val:
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except json.JSONDecodeError:
                    pass
            if isinstance(val, dict):
                return val

    # Flat fallback — row IS the response
    return first


# ============================================================================
# PUBLIC API
# ============================================================================

from app.core.text_clean import clean_obj


def _build_account_focus(summary: dict, focus: str) -> str:
    """Single-topic firmographics report for accounts (rendered as a card)."""
    now = _fmt_dt(datetime.now().isoformat())

    def _rows(key):
        v = summary.get(key) or []
        if isinstance(v, list):
            return [{'name': (r.get('name') or r.get('industry') or 'Unknown'),
                     'count': r.get('count', 0)} for r in v]
        if isinstance(v, dict):
            return [{'name': k, 'count': c} for k, c in v.items()]
        return []

    def _table(seg, rows, suffix=''):
        out = [f'| {seg} | Accounts |', '|--------|------:|']
        for r in rows:
            out.append(f"| {r['name']} | **{r['count']}{suffix}** |")
        return out

    total = sum(r['count'] for r in _rows('by_industry'))
    out: List[str] = []

    if focus == 'employee_size':
        rows = _rows('by_employee_band')
        smb_bands = {'1-10', '11-50', '51-200'}
        smb = sum(r['count'] for r in rows if r['name'] in smb_bands)
        ent = sum(r['count'] for r in rows if r['name'] not in smb_bands and r['name'] != 'Unknown')
        out += ['### 👥 Enterprise vs SMB Account Mix', f'**Time:** {now}', '',
                f"**Verdict:** **{ent}** enterprise (201+ employees) vs **{smb}** SMB (1–200).", '']
        out += _table('Company Size', rows)
    elif focus == 'revenue':
        rows = _rows('by_revenue_band')
        top = max(rows, key=lambda r: r['count']) if rows else None
        out += ['### 💰 Account Revenue-Band Distribution', f'**Time:** {now}', '']
        if top:
            out += [f"**Verdict:** Most accounts sit in the **{top['name']}** revenue band "
                    f"(**{top['count']}** accounts).", '']
        out += _table('Revenue Band', rows)
    elif focus == 'enrichment':
        enriched = summary.get('enriched_count') or 0
        pct = round(100.0 * enriched / total, 1) if total else 0
        out += ['### 🌐 Account Firmographics Enrichment Coverage', f'**Time:** {now}', '',
                f"**Verdict:** **{enriched}** of **{total}** accounts enriched (**{pct}%** coverage).", '']
        out += _table('Industry', _rows('by_industry'))
    else:  # industry
        rows = _rows('by_industry')
        top = rows[0] if rows else None
        out += ['### 🌐 Account Industry Mix', f'**Time:** {now}', '']
        if top:
            out += [f"**Verdict:** Accounts span **{len(rows)}** industries — the largest is "
                    f"**{top['name']}** (**{top['count']}** accounts).", '']
        out += _table('Industry', rows)

    return '\n'.join(out)


def format_response(db_rows: List[Dict], params: Dict[str, Any]) -> str:
    """
    Format sp_accounts DB rows into a human-readable / machine-parseable
    markdown string for the HTML frontend.

    Returns a string that is placed directly into ChatResponse.output.
    """
    mode = str(params.get('mode') or 'unknown').lower().strip()

    response = clean_obj(_parse_response(db_rows))
    metadata = response.get('metadata', {})

    logger.info(f'Format Response (sp_accounts v2d) — mode={mode}')

    # ── Error short-circuit ──────────────────────────────────────────────────
    if metadata.get('status') == 'error' or (
            metadata.get('code') and metadata.get('code') != 0):
        code = metadata.get('code', -999)
        msg  = metadata.get('message', 'Unknown error occurred')
        return (
            f"[ERROR] ERROR REPORT\n"
            f"Date: {_fmt_dt(datetime.now().isoformat())}\n"
            f"Error Code: {code}\n"
            f"Error Message: {msg}\n\n"
            f"Please try again or contact support."
        )

    # ── Executive answer — pre-formatted by the shared executive layer ───────
    if mode == 'executive_question':
        return response.get('exec_markdown') or 'No executive data available.'

    # ── Web answer — pre-formatted by app/core/web_tools.py ──────────────────
    if mode == 'web_search':
        return response.get('web_markdown') or 'No web results found.'

    # ── AI account summary — pre-formatted markdown card body ────────────────
    if mode == 'ai_summary':
        return response.get('ai_markdown') or 'No summary available.'

    # ── Firmographics focused report (account exec-insight chips) ────────────
    if mode == 'summary' and params.get('focus'):
        return _build_account_focus(response.get('summary') or {}, params['focus'])

    # ── UI-only marker modes — frontend opens the inline form ────────────────
    _ui_form_messages = {
        'show_account_form':        'Opening the Create Account form below…',
        'show_account_update_form': 'Opening the Update Account form below…',
    }
    if mode in _ui_form_messages:
        return f'[MODE:{mode}]\n{_ui_form_messages[mode]}'

    lines: List[str] = []
    lines.append(f"[MODE:{mode}] **{_mode_name(mode).upper()}**")
    lines.append('')

    # ── LIST ─────────────────────────────────────────────────────────────────
    if mode == 'list':
        accounts   = response.get('accounts', [])
        page       = metadata.get('page', 1)
        total_pages = metadata.get('total_pages', 1)
        total_rec  = metadata.get('total_records', len(accounts))

        lines.append(f"**Page:** {page} of {total_pages} | **Total:** {total_rec} accounts")
        lines.append('')

        if not accounts:
            lines.append('No accounts found matching your search criteria.')
        else:
            for idx, a in enumerate(accounts, start=1):
                name     = a.get('account_name') or a.get('name') or 'Unknown'
                acc_type = _normalize_type(a.get('type') or a.get('account_type'))
                acc_id   = a.get('account_id') or a.get('id') or ''
                industry = a.get('industry') or 'N/A'
                location = _build_location(a)
                email    = a.get('email') or 'N/A'
                phone    = a.get('phone') or 'N/A'
                website  = a.get('website') or 'N/A'
                status   = a.get('status') or 'active'
                contacts = a.get('contact_count', 0)
                opps     = a.get('opportunity_count', 0)
                orders   = a.get('order_count', 0)
                revenue  = _fmt_currency(a.get('total_revenue', 0))
                created  = _fmt_dt(a.get('created_at'))
                updated  = _fmt_dt(a.get('updated_at'))

                emp_band = a.get('employee_band')
                rev_band = a.get('revenue_band')

                lines.append(f"**{idx}. {name}** [{acc_type}]")
                lines.append(f"   ID: {acc_id}")
                lines.append(f"   Industry: {industry}")
                if emp_band:
                    lines.append(f"   Company Size: {emp_band} employees")
                if rev_band:
                    lines.append(f"   Revenue: {rev_band}")
                lines.append(f"   Location: {location}")
                lines.append(f"   Email: {email}")
                lines.append(f"   Phone: {phone}")
                lines.append(f"   Website: {website}")
                lines.append(f"   **Relationships:**")
                lines.append(
                    f"   Contacts: {contacts} | Opportunities: {opps} | "
                    f"Orders: {orders} | Revenue: {revenue}"
                )
                lines.append(f"   Status: {status}")
                created_by = a.get('created_by_name')
                updated_by = a.get('updated_by_name')
                if created_by:
                    lines.append(f"   Created by: {created_by}")
                if updated_by:
                    lines.append(f"   Updated by: {updated_by}")
                lines.append(f"   Created: {created}")
                lines.append(f"   Updated: {updated}")
                lines.append('')

        return '\n'.join(lines)

    # ── SUMMARY ──────────────────────────────────────────────────────────────
    if mode == 'summary':
        summary = response.get('summary', {})

        lines.append('[BAR_CHART] **ACCOUNT STATISTICS**')
        lines.append('')

        totals = summary.get('totals', {})
        if totals:
            lines.append('**Overview**')
            if totals.get('total_active')         is not None: lines.append(f"   Total Active: {_fmt_big(totals['total_active'])}")
            if totals.get('total_archived')       is not None: lines.append(f"   Total Archived: {_fmt_big(totals['total_archived'])}")
            if totals.get('created_this_month')   is not None: lines.append(f"   Created This Month: {_fmt_big(totals['created_this_month'])}")
            lines.append('')

        by_type = summary.get('by_type')
        if by_type:
            lines.append('**By Type**')
            if isinstance(by_type, list):
                for item in by_type:
                    lines.append(f"   {item.get('type') or item.get('name') or 'N/A'}: {item.get('count', 0)}")
            elif isinstance(by_type, dict):
                for t, c in by_type.items():
                    lines.append(f"   {t}: {c}")
            lines.append('')

        by_industry = summary.get('by_industry')
        if by_industry:
            lines.append('**By Industry**')
            if isinstance(by_industry, list):
                for item in by_industry:
                    lines.append(f"   {item.get('industry') or item.get('name') or 'N/A'}: {item.get('count', 0)}")
            elif isinstance(by_industry, dict):
                for ind, c in by_industry.items():
                    lines.append(f"   {ind or 'N/A'}: {c}")
            lines.append('')

        by_emp = summary.get('by_employee_band')
        if by_emp:
            lines.append('**By Company Size**')
            for item in (by_emp if isinstance(by_emp, list) else [{'name': k, 'count': v} for k, v in by_emp.items()]):
                lines.append(f"   {item.get('name') or 'Unknown'}: {item.get('count', 0)}")
            lines.append('')

        by_rev = summary.get('by_revenue_band')
        if by_rev:
            lines.append('**By Revenue**')
            for item in (by_rev if isinstance(by_rev, list) else [{'name': k, 'count': v} for k, v in by_rev.items()]):
                lines.append(f"   {item.get('name') or 'Unknown'}: {item.get('count', 0)}")
            lines.append('')

        by_status = summary.get('by_status', {})
        if by_status:
            lines.append('**By Status**')
            for s, c in by_status.items():
                lines.append(f"   {s}: {c}")
            lines.append('')

        revenue = summary.get('revenue', {})
        if revenue:
            lines.append('**Revenue**')
            if revenue.get('total_invoiced')    is not None: lines.append(f"   Total Invoiced: {_fmt_currency(revenue['total_invoiced'])}")
            if revenue.get('total_paid')        is not None: lines.append(f"   Total Paid: {_fmt_currency(revenue['total_paid'])}")
            if revenue.get('total_outstanding') is not None: lines.append(f"   Total Outstanding: {_fmt_currency(revenue['total_outstanding'])}")
            lines.append('')

        top = summary.get('top_accounts_by_revenue', [])
        if top:
            lines.append('**Top Accounts by Revenue**')
            for idx, a in enumerate(top, start=1):
                lines.append(
                    f"   {idx}. {a.get('account_name') or 'N/A'} "
                    f"(ID: {_fmt_uuid(a.get('account_id'))}) - "
                    f"{_fmt_currency(a.get('total_invoiced', 0))}"
                )
            lines.append('')

        return '\n'.join(lines)

    # ── GET (Account 360° View) ───────────────────────────────────────────────
    if mode == 'get':
        a = response.get('account') or response

        billing_addr  = a.get('billing_address')  or {}
        shipping_addr = a.get('shipping_address') or {}

        lines.append(f"**Account ID:** {_fmt_uuid(a.get('account_id') or a.get('id'))}")
        lines.append(f"**Name:** {a.get('account_name') or a.get('name') or 'N/A'}")
        lines.append(f"**Type:** {_normalize_type(a.get('type') or a.get('account_type'))}")
        lines.append(f"**Industry:** {a.get('industry') or 'N/A'}")
        lines.append(f"**Status:** {a.get('status') or 'N/A'}")
        lines.append('')
        lines.append('**Contact Info**')
        lines.append(f"   Email: {a.get('email') or 'N/A'}")
        lines.append(f"   Phone: {a.get('phone') or 'N/A'}")
        lines.append(f"   Website: {a.get('website') or 'N/A'}")
        lines.append('')
        lines.append('**Billing Address**')
        lines.append(f"   {_fmt_addr(billing_addr)}")
        lines.append('')
        lines.append('**Shipping Address**')
        lines.append(f"   {_fmt_addr(shipping_addr)}")
        lines.append('')
        lines.append('**Relationships**')
        lines.append(f"   Contacts: {a.get('contact_count', 0)}")
        lines.append(f"   Opportunities: {a.get('opportunity_count', 0)}")
        lines.append(f"   Orders: {a.get('order_count', 0)}")
        lines.append(f"   Total Revenue: {_fmt_currency(a.get('total_revenue', 0))}")
        lines.append('')
        lines.append('**Audit**')
        lines.append(f"   Created: {_fmt_dt(a.get('created_at'))}")
        lines.append(f"   Updated: {_fmt_dt(a.get('updated_at'))}")
        lines.append(f"   Owner ID: {_fmt_uuid(a.get('owner_id'))}")

        return '\n'.join(lines)

    # ── CREATE / UPDATE ───────────────────────────────────────────────────────
    if mode in ('create', 'update'):
        a    = response.get('account') or response
        verb = 'Created' if mode == 'create' else 'Updated'

        lines.append(f"✅ Account {verb} Successfully")
        lines.append('')
        lines.append(f"**Account ID:** {_fmt_uuid(a.get('account_id') or a.get('id'))}")
        lines.append(f"**Name:** {a.get('account_name') or a.get('name') or 'N/A'}")
        lines.append(f"**Type:** {_normalize_type(a.get('type') or a.get('account_type'))}")
        lines.append(f"**Industry:** {a.get('industry') or 'N/A'}")
        lines.append(f"**Status:** {a.get('status') or 'N/A'}")
        lines.append(f"**Email:** {a.get('email') or 'N/A'}")
        lines.append(f"**Phone:** {a.get('phone') or 'N/A'}")
        lines.append(f"**Website:** {a.get('website') or 'N/A'}")
        lines.append('')
        lines.append(f"**{verb} At:** {_fmt_dt(a.get('updated_at') or a.get('created_at'))}")

        return '\n'.join(lines)

    # ── ARCHIVE / RESTORE ─────────────────────────────────────────────────────
    if mode in ('archive', 'restore'):
        a    = response.get('account') or response
        verb = 'Archived' if mode == 'archive' else 'Restored'

        lines.append(f"✅ Account {verb} Successfully")
        lines.append('')
        lines.append(f"**Account ID:** {_fmt_uuid(a.get('account_id') or a.get('id'))}")
        lines.append(f"**Name:** {a.get('account_name') or a.get('name') or 'N/A'}")
        lines.append(f"**Status:** {a.get('status') or 'N/A'}")
        lines.append(f"**{verb} At:** {_fmt_dt(a.get('updated_at'))}")

        return '\n'.join(lines)

    # ── TIMELINE ─────────────────────────────────────────────────────────────
    if mode == 'timeline':
        activities = response.get('activities') or response.get('timeline') or []
        acct_id    = response.get('account_id') or metadata.get('account_id') or ''
        acct_name  = response.get('account_name') or ''
        page       = metadata.get('page', 1)
        total_pages = metadata.get('total_pages', 1)
        total_rec  = metadata.get('total_records', len(activities))

        lines.append(f"**Account:** {acct_name or acct_id or 'N/A'}")
        lines.append(f"**Page:** {page} of {total_pages} | **Total:** {total_rec} activities")
        if acct_id:
            lines.append(f"**Account ID:** {acct_id}")
        lines.append('')

        if not activities:
            lines.append('No activities found for this account.')
        else:
            for idx, act in enumerate(activities, start=1):
                act_type = (
                    act.get('type') or act.get('category') or
                    act.get('activity_type') or 'note'
                ).lower()
                direction = (act.get('direction') or '').lower()
                dir_icon  = '📥' if direction == 'inbound' else ('📤' if direction == 'outbound' else '')
                header    = f"{dir_icon} {act_type.upper()}" if dir_icon else act_type.upper()

                lines.append(f"**{idx}. {header}**")
                lines.append(f"   Subject:    {act.get('subject') or act.get('title') or 'No subject'}")
                lines.append(f"   Category:   {act_type}")
                lines.append(f"   Channel:    {act.get('channel') or 'N/A'}")
                lines.append(f"   Direction:  {act.get('direction') or 'N/A'}")
                lines.append(f"   Owner:      {act.get('owner_name') or 'N/A'}")
                if act.get('owner_id'):
                    lines.append(f"   Owner ID:   {act['owner_id']}")
                lines.append(f"   Created:    {_fmt_dt(act.get('activity_date') or act.get('created_at'))}")
                if act.get('id'):
                    lines.append(f"   Activity ID: {act['id']}")
                if act.get('notes'):
                    lines.append(f"   Notes:      {act['notes']}")
                lines.append('')

        return '\n'.join(lines)

    # ── FINANCIALS ────────────────────────────────────────────────────────────
    if mode == 'financials':
        fin      = response.get('financials') or response
        acct_id  = response.get('account_id') or metadata.get('account_id') or ''
        acct_name = response.get('account_name') or ''

        ord_ = fin.get('orders', {})
        inv  = fin.get('invoices', {})
        pay  = fin.get('payments', {})
        opp  = fin.get('opportunities', {})

        if acct_id:
            lines.append(f"Account ID: {acct_id}")
        if acct_name:
            lines.append(f"Account: {acct_name}")
        lines.append('')

        lines.append('**Orders**')
        lines.append(f"   Total Orders: {ord_.get('total_orders', 0)}")
        lines.append(f"   Total Amount: {_fmt_currency(ord_.get('total_amount', 0))}")
        lines.append(f"   Pending Orders: {ord_.get('pending_orders', 0)}")
        if ord_.get('by_status'):
            lines.append('   By Status:')
            for s, n in ord_['by_status'].items():
                lines.append(f"      {s}: {n}")
        lines.append('')

        lines.append('**Invoices**')
        lines.append(f"   Total Invoices: {inv.get('total_invoices', 0)}")
        lines.append(f"   Total Invoiced: {_fmt_currency(inv.get('total_invoiced', 0))}")
        lines.append(f"   Total Paid: {_fmt_currency(inv.get('total_paid', 0))}")
        lines.append(f"   Total Outstanding: {_fmt_currency(inv.get('total_outstanding', 0))}")
        lines.append(f"   Overdue: {inv.get('overdue', 0)}")
        if inv.get('by_status'):
            lines.append('   By Status:')
            for s, n in inv['by_status'].items():
                lines.append(f"      {s}: {n}")
        lines.append('')

        lines.append('**Payments**')
        lines.append(f"   Total Payments: {pay.get('total_payments', 0)}")
        lines.append(f"   Total Received: {_fmt_currency(pay.get('total_received', 0))}")
        if pay.get('by_method'):
            lines.append('   By Method:')
            for m, v in pay['by_method'].items():
                lines.append(f"      {m}: {_fmt_currency(v)}")
        lines.append('')

        lines.append('**Opportunities**')
        lines.append(f"   Total Value: {_fmt_currency(opp.get('total_value', 0))}")
        lines.append(f"   Won Value: {_fmt_currency(opp.get('won_value', 0))}")
        lines.append(f"   Pipeline Value: {_fmt_currency(opp.get('pipeline_value', 0))}")
        lines.append('')

        return '\n'.join(lines)

    # ── DUPLICATES ────────────────────────────────────────────────────────────
    if mode == 'duplicates':
        dup_data = response.get('duplicates') or response

        lines.append('**Duplicate Account Report**')
        lines.append('')

        by_name  = dup_data.get('by_name') or dup_data.get('by_name_city') or []
        by_email = dup_data.get('by_email') or []

        if by_name:
            lines.append(f"**Duplicates by Name/City ({len(by_name)} groups)**")
            for i, grp in enumerate(by_name, start=1):
                name = grp.get('account_name') or grp.get('name') or 'N/A'
                lines.append(f"   {i}. \"{name}\" — {grp.get('count', 0)} records")
                ids = grp.get('ids', [])
                if ids:
                    lines.append(f"      IDs: {', '.join(_fmt_uuid(x) for x in ids[:5])}")
            lines.append('')

        if by_email:
            lines.append(f"**Duplicates by Email ({len(by_email)} groups)**")
            for i, grp in enumerate(by_email, start=1):
                lines.append(f"   {i}. \"{grp.get('email')}\" — {grp.get('count', 0)} records")
            lines.append('')

        if not by_name and not by_email:
            lines.append('No duplicate accounts found. ✅')

        return '\n'.join(lines)

    # ── MERGE ─────────────────────────────────────────────────────────────────
    if mode == 'list_owner':
        owner_list = response.get('owners') or []
        lines.append(f"Found {len(owner_list)} owner(s).")
        return '\n'.join(lines)

    if mode == 'merge':
        mr = response.get('merge') or response

        lines.append('✅ Accounts Merged Successfully')
        lines.append('')
        lines.append(f"**Primary Account ID:** {_fmt_uuid(mr.get('primary_id'))}")
        lines.append(f"**Merged Account ID:** {_fmt_uuid(mr.get('merged_id'))}")
        lines.append(f"**Contacts Transferred:** {mr.get('contacts_transferred', 0)}")
        lines.append(f"**Addresses Transferred:** {mr.get('addresses_transferred', 0)}")
        lines.append(f"**Opportunities Transferred:** {mr.get('opportunities_transferred', 0)}")
        lines.append(f"**Orders Transferred:** {mr.get('orders_transferred', 0)}")
        lines.append(f"**Merged At:** {_fmt_dt(mr.get('merged_at'))}")

        return '\n'.join(lines)

    # ── GENERIC FALLBACK ──────────────────────────────────────────────────────
    lines.append(f'Mode "{mode}" response received.')
    lines.append('')
    lines.append(json.dumps(response, indent=2, default=str))

    return '\n'.join(lines)
