"""Response formatter for Contact Management — aligned with n8n Format Response v4x.

Supports 12 modes. Fully aligned with sp_contacts v3e redesigned summary mode.

v4x changes:
  - SUMMARY: Parses card-based response from sp_contacts v3e.
    Cards: Totals (metrics), Top Accounts (ranking), Role/Status/Owner (bar).
    Each card has: value, value_prev, change_pct, trend, sparkline, score.
  - Summary tables include: Δ%, Trend, Sparkline, Score, % of Total, Severity.
  - Forecast columns included when SP provides them.
  - create, update, get_details all return full 360° contact objects.
  - All 12 modes supported: list, get_details, create, update,
    send_verification, verify_email, duplicates, merge,
    archive, restore, activities, summary.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Unicode normalisation — remote PostgreSQL encoding fix
# (same table as activities/formatter.py — keeps activity subjects clean)
# ---------------------------------------------------------------------------
_UNICODE_NORMALISE = str.maketrans({
    '­': '-',  '‐': '-',  '‑': '-',  '‒': '-',
    '–': '-',  '—': '-',  '―': '-',  '−': '-',
    '﹘': '-',  '﹣': '-',  '－': '-',
    '‘': "'",  '’': "'",  '‚': "'",  '‛': "'",
    '“': '"',  '”': '"',  '„': '"',  '‟': '"',
    '…': '...', '•': '*', '·': '*',  '‣': '*',
    ' ': ' ',
})

_MANGLED_DASH_RE = re.compile(r' \?{2,3} ')

def _clean_text(value):
    if not value:
        return value
    s = str(value).translate(_UNICODE_NORMALISE)
    # Bytes E2 80 93 (UTF-8 en-dash) decoded with errors='replace' on the
    # remote psycopg2 connection collapse to '??' or '???' between words.
    # Restore them to a plain ASCII hyphen so "Payment complete ??? INV-..."
    # becomes "Payment complete - INV-...".
    return _MANGLED_DASH_RE.sub(' - ', s)


# Mode alias (legacy 'get' → 'get_details')
MODE_ALIASES = {'get': 'get_details'}

MODE_NAMES = {
    'list':              'List Contacts',
    'get_details':       'Contact 360° View',
    'create':            'Create Contact',
    'update':            'Update Contact',
    'send_verification': 'Send Email Verification',
    'verify_email':      'Verify Email',
    'duplicates':        'Find Duplicates',
    'merge':             'Merge Duplicates',
    'archive':           'Archive Contact',
    'restore':           'Restore Contact',
    'activities':        'Activity Timeline',
    'summary':           'Contact Statistics',
}


# ============================================================================
# HELPERS
# ============================================================================

def _safe_num(n, fallback: float = 0.0) -> float:
    try:
        return float(n) if n is not None else fallback
    except (ValueError, TypeError):
        return fallback


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
    parts = [
        addr.get('street') or addr.get('line1'),
        addr.get('line2'),
        addr.get('city'),
        addr.get('province'),
        addr.get('postal_code'),
        addr.get('country'),
    ]
    parts = [p for p in parts if p]
    return ', '.join(parts) if parts else 'N/A'


def _safe_json(v):
    try:
        return json.loads(v) if isinstance(v, str) else v
    except (json.JSONDecodeError, TypeError):
        return None


def _trend_arrow(curr, prev) -> str:
    if prev is None:
        return '→'
    try:
        c, p = float(curr), float(prev)
        if c > p: return '↑'
        if c < p: return '↓'
    except (TypeError, ValueError):
        pass
    return '→'


def _pct_change(curr, prev) -> Optional[float]:
    try:
        c, p = float(curr), float(prev)
        if p == 0:
            return None
        return round(((c - p) / p) * 1000) / 10
    except (TypeError, ValueError):
        return None


def _pct(value, total) -> str:
    try:
        v, t = float(value), float(total)
        if t == 0:
            return '—'
        return f"{round((v / t) * 1000) / 10}%"
    except (TypeError, ValueError):
        return '—'


def _severity_tag(value) -> str:
    try:
        v = float(value)
        if v >= 50: return 'High'
        if v >= 20: return 'Medium'
        if v > 0:  return 'Low'
    except (TypeError, ValueError):
        pass
    return 'None'


def _parse_response(db_rows: List[Dict]) -> Dict:
    """Extract the sp_contacts JSON response from raw DB rows."""
    if not db_rows:
        return {}
    first = db_rows[0]
    for key in ('sp_contacts', 'result'):
        val = first.get(key)
        if val:
            if isinstance(val, str):
                parsed = _safe_json(val)
                if parsed is not None:
                    return parsed
            if isinstance(val, dict):
                return val
    return first


# ============================================================================
# PUBLIC API
# ============================================================================

from app.core.text_clean import clean_obj


def format_response(db_rows: List[Dict], params: Dict[str, Any]) -> str:
    """
    Format sp_contacts DB rows into the markdown string expected by the
    HTML frontend.

    Returns
    -------
    str — placed directly into ChatResponse.output.
    """
    raw_mode = str(params.get('mode') or 'unknown').lower().strip()
    mode = MODE_ALIASES.get(raw_mode, raw_mode)

    response = clean_obj(_parse_response(db_rows))
    metadata = response.get('metadata', {})

    logger.info(f'Format Response (sp_contacts v4x) — mode={mode}')

    # ── Error short-circuit  ──────────────────────────────────────────────────
    is_error = (
        metadata.get('status') == 'error' or
        (metadata.get('code') and metadata.get('code') != 0)
    )
    if is_error:
        code = metadata.get('code', -999)
        msg  = metadata.get('message', 'Unknown error')
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

    # ── AI contact summary — pre-formatted markdown card body ────────────────
    if mode == 'ai_summary':
        return response.get('ai_markdown') or 'No summary available.'

    # ── UI-only marker modes — frontend opens the inline form ────────────────
    _ui_form_messages = {
        'show_contact_form':        'Opening the Create Contact form below…',
        'show_contact_update_form': 'Opening the Update Contact search below…',
    }
    if mode in _ui_form_messages:
        return f'[MODE:{mode}]\n{_ui_form_messages[mode]}'

    out: List[str] = []
    mode_label = MODE_NAMES.get(mode, 'Unknown')
    out.append(f"[MODE:{mode}] **{mode_label.upper()}**")
    out.append('')

    # ── LIST  ─────────────────────────────────────────────────────────────────
    if mode == 'list':
        contacts   = response.get('contacts', [])
        page       = metadata.get('page', 1)
        total_pages = metadata.get('total_pages', 1)
        total_rec  = metadata.get('total_records', len(contacts))
        show_del   = metadata.get('showing_deleted_only', False)
        incl_del   = metadata.get('including_deleted', False)

        out.append(f"**Page:** {page} of {total_pages} | **Total:** {total_rec} contacts")
        if show_del:
            out.append('**Showing:** Archived contacts only')
        elif incl_del:
            out.append('**Showing:** Active + archived contacts')
        out.append('')

        for i, c in enumerate(contacts, start=1):
            out.append(f"**{i}. {c.get('first_name', '')} {c.get('last_name', '')}**")
            out.append(f"   ID: {_fmt_uuid(c.get('contact_id'))}")
            if c.get('account_name'): out.append(f"   Account: {c['account_name']}")
            if c.get('email'):        out.append(f"   Email: {c['email']}")
            if c.get('phone'):        out.append(f"   Phone: {c['phone']}")
            if c.get('role'):         out.append(f"   Role: {c['role']}")
            out.append(f"   Status: {c.get('status', 'active')}")
            # Billing address from flat SP fields
            addr_parts = [
                c.get('billing_street'), c.get('billing_street2'),
                c.get('billing_city'), c.get('billing_province'),
                c.get('billing_postal_code'), c.get('billing_country'),
            ]
            addr_parts = [p for p in addr_parts if p]
            if addr_parts:
                out.append(f"   Address: {', '.join(addr_parts)}")
            if c.get('created_by_name'): out.append(f"   Created by: {c['created_by_name']}")
            if c.get('updated_by_name'): out.append(f"   Updated by: {c['updated_by_name']}")
            if c.get('created_at'): out.append(f"   Created: {_fmt_dt(c['created_at'])}")
            if c.get('updated_at'): out.append(f"   Updated: {_fmt_dt(c['updated_at'])}")
            if c.get('is_deleted'): out.append('   **[ARCHIVED]**')
            out.append('')

        return '\n'.join(out)

    # ── 360° DETAIL (get_details, create, update)  ────────────────────────────
    if mode in ('get_details', 'create', 'update'):
        contact = response.get('contact') or {}
        billing  = contact.get('billing_address') or {}
        shipping = contact.get('shipping_address') or {}
        all_addr = contact.get('all_addresses', [])
        opps     = contact.get('opportunities', [])
        cases    = contact.get('cases', [])
        activities = contact.get('recent_activities', [])

        if mode == 'create':
            out.append('[SUCCESS] **CONTACT CREATED SUCCESSFULLY!**')
        elif mode == 'update':
            out.append('[SUCCESS] **CONTACT UPDATED SUCCESSFULLY!**')
        else:
            out.append('[360°] **CONTACT 360° VIEW**')
        out.append('')
        out.append(f"**{contact.get('first_name', '')} {contact.get('last_name', '')}**")
        out.append('')

        # Basic Information
        out.append('### Basic Information')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        out.append(f"| Contact ID | {_fmt_uuid(contact.get('contact_id'))} |")
        out.append(f"| Full Name | {contact.get('first_name', '')} {contact.get('last_name', '')} |")
        out.append(f"| Email | {contact.get('email') or 'N/A'} |")
        out.append(f"| Is Email Verified | {'Yes' if contact.get('is_email_verified') else 'No'} |")
        out.append(f"| Phone | {contact.get('phone') or 'N/A'} |")
        out.append(f"| Role | {contact.get('role') or 'N/A'} |")
        out.append(f"| Status | {contact.get('status') or 'active'} |")
        out.append(f"| Is Customer | {'Yes' if contact.get('is_customer') else 'No'} |")
        out.append(f"| Is Deleted | {'Yes' if contact.get('is_deleted') else 'No'} |")
        out.append('')

        # Account Information
        out.append('### Account Information')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        out.append(f"| Account ID | {_fmt_uuid(contact.get('account_id'))} |")
        out.append(f"| Account Name | {contact.get('account_name') or 'N/A'} |")
        out.append('')

        # Owner Information
        out.append('### Owner Information')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        out.append(f"| Owner ID | {_fmt_uuid(contact.get('owner_id'))} |")
        out.append('')

        # Billing Address
        if billing:
            out.append('### Billing Address')
            out.append('| Field | Value |')
            out.append('|-------|-------|')
            out.append(f"| Address ID | {_fmt_uuid(billing.get('address_id'))} |")
            out.append(f"| Street | {billing.get('street') or billing.get('line1') or 'N/A'} |")
            out.append(f"| Line2 | {billing.get('line2') or 'N/A'} |")
            out.append(f"| City | {billing.get('city') or 'N/A'} |")
            out.append(f"| Province | {billing.get('province') or 'N/A'} |")
            out.append(f"| Postal Code | {billing.get('postal_code') or 'N/A'} |")
            out.append(f"| Country | {billing.get('country') or 'N/A'} |")
            out.append(f"| Label | {billing.get('label') or 'billing'} |")
            out.append(f"| Is Default | {'Yes' if billing.get('is_default') else 'No'} |")
            out.append('')

        # Shipping Address
        if shipping:
            out.append('### Shipping Address')
            out.append('| Field | Value |')
            out.append('|-------|-------|')
            out.append(f"| Address ID | {_fmt_uuid(shipping.get('address_id'))} |")
            out.append(f"| Street | {shipping.get('street') or shipping.get('line1') or 'N/A'} |")
            out.append(f"| Line2 | {shipping.get('line2') or 'N/A'} |")
            out.append(f"| City | {shipping.get('city') or 'N/A'} |")
            out.append(f"| Province | {shipping.get('province') or 'N/A'} |")
            out.append(f"| Postal Code | {shipping.get('postal_code') or 'N/A'} |")
            out.append(f"| Country | {shipping.get('country') or 'N/A'} |")
            out.append(f"| Label | {shipping.get('label') or 'shipping'} |")
            out.append(f"| Is Default | {'Yes' if shipping.get('is_default') else 'No'} |")
            out.append('')

        # All Addresses
        if all_addr:
            out.append(f"### All Addresses ({len(all_addr)})")
            out.append('| Label | Street | City | Province | Postal Code | Country | Is Default |')
            out.append('|-------|--------|------|----------|-------------|---------|------------|')
            for a in all_addr:
                out.append(
                    f"| {a.get('label') or 'N/A'} | {a.get('street') or a.get('line1') or 'N/A'} | "
                    f"{a.get('city') or 'N/A'} | {a.get('province') or 'N/A'} | "
                    f"{a.get('postal_code') or 'N/A'} | {a.get('country') or 'N/A'} | "
                    f"{'Yes' if a.get('is_default') else 'No'} |"
                )
            out.append('')

        # Opportunities
        if opps:
            out.append(f"### Opportunities ({len(opps)})")
            out.append('| Name | Stage | Amount | Status |')
            out.append('|------|-------|--------|--------|')
            for o in opps:
                out.append(
                    f"| {o.get('name') or 'N/A'} | {o.get('stage') or 'N/A'} | "
                    f"${_safe_num(o.get('amount'))} | {o.get('status') or 'N/A'} |"
                )
            out.append('')

        # Cases
        if cases:
            out.append(f"### Cases ({len(cases)})")
            out.append('| Subject | Priority | Status |')
            out.append('|---------|----------|--------|')
            for c in cases:
                out.append(
                    f"| {c.get('subject') or 'N/A'} | {c.get('priority') or 'N/A'} | "
                    f"{c.get('status') or 'N/A'} |"
                )
            out.append('')

        # Recent Activities
        if activities:
            out.append(f"### Recent Activities ({len(activities)})")
            out.append('| Type | Subject | Created At |')
            out.append('|------|---------|------------|')
            for a in activities:
                out.append(
                    f"| {a.get('type') or 'N/A'} | {_clean_text(a.get('subject')) or 'N/A'} | "
                    f"{_fmt_dt(a.get('created_at'))} |"
                )
            out.append('')

        # Audit Information
        out.append('### Audit Information')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        out.append(f"| Created At | {_fmt_dt(contact.get('created_at'))} |")
        out.append(f"| Created By | {contact.get('created_by_name') or contact.get('created_by') or 'N/A'} |")
        out.append(f"| Updated At | {_fmt_dt(contact.get('updated_at'))} |")
        out.append(f"| Updated By | {contact.get('updated_by_name') or contact.get('updated_by') or 'N/A'} |")
        out.append('')

        return '\n'.join(out)

    # ── SEND VERIFICATION  ────────────────────────────────────────────────────
    if mode == 'send_verification':
        # sp_contacts may return fields at root level OR nested under
        # 'verification', 'data', or 'result'. Try all paths.
        v = (response.get('verification') or
             response.get('data') or
             response.get('result') or
             response)

        # Field name variants the SP might use
        contact_id = (
            v.get('contact_id') or
            response.get('contact_id')
        )
        email = (
            v.get('email') or
            v.get('contact_email') or
            response.get('email') or
            response.get('contact_email')
        )
        token = (
            v.get('token') or
            v.get('verification_token') or
            v.get('email_verification_token') or
            response.get('token') or
            response.get('verification_token') or
            response.get('email_verification_token')
        )
        expires_at = (
            v.get('expires_at') or
            v.get('token_expires_at') or
            v.get('expiry') or
            response.get('expires_at') or
            response.get('token_expires_at') or
            response.get('expiry')
        )

        # Debug: log what the SP actually returned so you can see the structure
        logger.info(
            f'send_verification raw response keys: {list(response.keys())} '
            f'| contact_id={contact_id} | email={email} | '
            f'token_present={bool(token)} | expires_at={expires_at}'
        )

        out.append('[EMAIL] **VERIFICATION TOKEN**')   # ← "VERIFICATION TOKEN" triggers frontend extractModeFromText fallback
        out.append('')
        out.append(f"**Contact ID:** {_fmt_uuid(contact_id)}")
        out.append(f"**Email:** {email or 'N/A'}")
        out.append(f"**Token:** `{token or 'N/A'}`")
        out.append(f"**Expires:** {_fmt_dt(expires_at)}")
        out.append('')
        out.append('_Send this token in a verification link._')
        out.append('')
        return '\n'.join(out)

    # ── VERIFY EMAIL  ─────────────────────────────────────────────────────────
    if mode == 'verify_email':
        v = (response.get('verification') or
             response.get('data') or
             response.get('result') or
             response)

        contact_id = v.get('contact_id') or response.get('contact_id')
        is_verified = (
            v.get('is_email_verified') or
            v.get('email_verified') or
            v.get('verified') or
            response.get('is_email_verified') or
            response.get('email_verified') or
            response.get('verified')
        )

        logger.info(
            f'verify_email raw response keys: {list(response.keys())} '
            f'| contact_id={contact_id} | is_verified={is_verified}'
        )

        out.append('[CHECK] **EMAIL VERIFIED SUCCESSFULLY!**')
        out.append('')
        out.append(f"**Contact ID:** {_fmt_uuid(contact_id)}")
        out.append(f"**Verified:** {'Yes' if is_verified else 'No'}")
        out.append('')
        return '\n'.join(out)

    # ── DUPLICATES  ───────────────────────────────────────────────────────────
    if mode == 'duplicates':
        out.append('[SEARCH] **DUPLICATE CONTACTS REPORT**')
        out.append('')

        dups       = response.get('duplicates', {})
        by_email   = dups.get('by_email', [])
        by_phone   = dups.get('by_phone', [])
        by_name    = dups.get('by_name_and_city', [])

        def _render_group(title: str, arr: list, label_key: str):
            if not arr:
                return
            out.append(f"**{title} ({len(arr)})**")
            for i, d in enumerate(arr, start=1):
                val = d.get(label_key) or d.get('duplicate_value') or 'N/A'
                out.append(f"   {i}. {val} — {d.get('count', 0)} contacts")
                matches = d.get('matches', [])
                if matches:
                    out.append('      Matches:')
                    for j, m in enumerate(matches, start=1):
                        out.append(f"         {j}. {m.get('name') or 'N/A'} ({_fmt_uuid(m.get('contact_id'))})")
            out.append('')

        _render_group('By Email',        by_email, 'duplicate_value')
        _render_group('By Phone',        by_phone, 'duplicate_value')
        _render_group('By Name + City',  by_name,  'norm_name')

        if not by_email and not by_phone and not by_name:
            out.append('No duplicates found.')

        return '\n'.join(out)

    # ── MERGE  ────────────────────────────────────────────────────────────────
    if mode == 'merge':
        out.append('[MERGE] **CONTACTS MERGED SUCCESSFULLY!**')
        out.append('')
        out.append(f"**Master Contact ID:** {_fmt_uuid(response.get('kept_contact_id'))}")
        out.append(f"**Merged Contacts:** {response.get('archived_count', 0)}")
        out.append('')
        return '\n'.join(out)

    # ── ARCHIVE  ──────────────────────────────────────────────────────────────
    if mode == 'archive':
        out.append('[ARCHIVE] **CONTACT ARCHIVED**')
        out.append('')
        out.append('_This contact is now soft-deleted._')
        out.append('')
        return '\n'.join(out)

    # ── RESTORE  ──────────────────────────────────────────────────────────────
    if mode == 'restore':
        out.append('[RESTORE] **CONTACT RESTORED**')
        out.append('')
        out.append('_This contact is active again._')
        out.append('')
        return '\n'.join(out)

    # ── ACTIVITIES  ──────────────────────────────────────────────────────────
    if mode == 'activities':
        out.append('[CALENDAR] **ACTIVITY TIMELINE**')
        out.append('')

        contact_id = response.get('contact_id')
        if contact_id:
            out.append(f"**Contact ID:** {_fmt_uuid(contact_id)}")
            out.append('')

        acts = response.get('activities', [])
        if acts:
            out.append(f"**Activities ({len(acts)})**")
            out.append('')
            for idx, a in enumerate(acts, start=1):
                direction = a.get('direction', '')
                if direction == 'inbound':
                    icon = '📥'
                elif direction == 'outbound':
                    icon = '📤'
                else:
                    icon = '📋'

                out.append(f"**{idx}. {icon} {a.get('type') or 'Activity'}**")
                if a.get('subject'):     out.append(f"   Subject: {_clean_text(a['subject'])}")
                if a.get('description'): out.append(f"   {_clean_text(a['description'])}")
                if a.get('completed_at'): out.append(f"   Completed: {_fmt_dt(a['completed_at'])}")
                out.append(f"   Created: {_fmt_dt(a.get('created_at'))}")
                out.append('')
        else:
            out.append('No activities found.')
            out.append('')

        return '\n'.join(out)

    # ── SUMMARY (card-based, sp_contacts v3e)  ───────────────────────────────
    if mode == 'summary':
        out.append('[SUMMARY] **CONTACT STATISTICS**')
        out.append('')

        summary = response.get('summary', {})
        cards   = summary.get('cards', [])

        # Compute total active contacts for percentage columns
        totals_card  = next((c for c in cards if c.get('title') == 'Totals'), None)
        total_active = 0
        if totals_card:
            active_item = next(
                (i for i in totals_card.get('items', []) if i.get('label') == 'Active'),
                None,
            )
            if active_item:
                total_active = _safe_num(active_item.get('value', 0))

        for card in cards:
            title      = card.get('title') or 'Untitled Section'
            card_type  = card.get('type') or 'list'
            items      = card.get('items') or []

            out.append(f"### {title}")
            out.append('')

            # ── METRICS (Totals card) ─────────────────────────────────────────
            if card_type == 'metrics':
                out.append(
                    '| Metric | Value | Prev | Δ% | Trend | Sparkline | '
                    'Forecast | Forecast Δ% | Forecast Trend |'
                )
                out.append(
                    '|--------|-------|------|----|-------|-----------|'
                    '----------|-------------|----------------|'
                )
                for m in items:
                    arrow      = _trend_arrow(m.get('value'), m.get('value_prev'))
                    chg        = _pct_change(m.get('value'), m.get('value_prev'))
                    spark      = m.get('sparkline') or ''
                    forecast   = m.get('forecast_next_month', '')
                    fc_chg     = _pct_change(forecast, m.get('value'))
                    fc_trend   = _trend_arrow(forecast, m.get('value'))
                    out.append(
                        f"| {m.get('label')} | {m.get('value')} | {m.get('value_prev', '')} | "
                        f"{chg if chg is not None else ''}% | {arrow} | {spark} | "
                        f"{forecast} | {fc_chg if fc_chg is not None else ''}% | {fc_trend} |"
                    )
                out.append('')

            # ── RANKING (Top Accounts) ────────────────────────────────────────
            elif card_type == 'ranking':
                out.append(
                    '| Rank | Account | Value | Prev | Δ% | Trend | Sparkline | '
                    'Score | % of Total | Forecast | Forecast Δ% | Forecast Trend |'
                )
                out.append(
                    '|------|---------|-------|------|----|-------|-----------|'
                    '-------|------------|----------|-------------|----------------|'
                )
                for r in items:
                    arrow    = _trend_arrow(r.get('value'), r.get('value_prev'))
                    chg      = _pct_change(r.get('value'), r.get('value_prev'))
                    spark    = r.get('sparkline') or ''
                    score    = f"{r['score']:.2f}" if r.get('score') is not None else ''
                    pct_tot  = _pct(r.get('value', 0), total_active)
                    forecast = r.get('forecast_next_month', '')
                    fc_chg   = _pct_change(forecast, r.get('value'))
                    fc_trend = _trend_arrow(forecast, r.get('value'))
                    prev_v   = r.get('value_prev')
                    prev_str = str(prev_v) if prev_v is not None else '—'
                    chg_str  = f"{chg}%" if chg is not None else '—'
                    fc_str   = f"{fc_chg}%" if fc_chg is not None else '—'
                    out.append(
                        f"| {r.get('rank')} | {r.get('label')} | {r.get('value')} | "
                        f"{prev_str} | {chg_str} | "
                        f"{arrow} | {spark} | {score} | {pct_tot} | "
                        f"{forecast} | {fc_str} | {fc_trend} |"
                    )
                out.append('')

            # ── BAR (Role, Status, Owner) ─────────────────────────────────────
            elif card_type == 'bar':
                out.append(
                    '| Category | Count | Prev | Δ% | Trend | Sparkline | '
                    'Score | % of Total | Severity | Forecast | Forecast Δ% | Forecast Trend |'
                )
                out.append(
                    '|----------|-------|------|----|-------|-----------|'
                    '-------|------------|----------|----------|-------------|----------------|'
                )
                for b in items:
                    label    = '(unset)' if b.get('label') == 'unset' else (b.get('label') or 'N/A')
                    arrow    = _trend_arrow(b.get('value'), b.get('value_prev'))
                    chg      = _pct_change(b.get('value'), b.get('value_prev'))
                    spark    = b.get('sparkline') or ''
                    score    = f"{b['score']:.2f}" if b.get('score') is not None else ''
                    pct_tot  = _pct(b.get('value', 0), total_active)
                    severity = _severity_tag(b.get('value'))
                    forecast = b.get('forecast_next_month', '')
                    fc_chg   = _pct_change(forecast, b.get('value'))
                    fc_trend = _trend_arrow(forecast, b.get('value'))
                    prev_v   = b.get('value_prev')
                    prev_str = str(prev_v) if prev_v is not None else '—'
                    chg_str  = f"{chg}%" if chg is not None else '—'
                    fc_str   = f"{fc_chg}%" if fc_chg is not None else '—'
                    out.append(
                        f"| {label} | {b.get('value')} | {prev_str} | "
                        f"{chg_str} | {arrow} | {spark} | "
                        f"{score} | {pct_tot} | {severity} | "
                        f"{forecast} | {fc_str} | {fc_trend} |"
                    )
                out.append('')

            # ── FALLBACK ──────────────────────────────────────────────────────
            else:
                for item in items:
                    out.append(f"- {item.get('label')}: {item.get('value')}")
                out.append('')

        return '\n'.join(out)

    # ── GENERIC FALLBACK  ─────────────────────────────────────────────────────
    out.append(f'[INFO] No data available for mode: {mode}')
    out.append('')
    out.append(json.dumps(response, indent=2, default=str))
    return '\n'.join(out)
