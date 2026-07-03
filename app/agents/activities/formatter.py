"""Response formatter for Activities — Python conversion of n8n Format Response v3.4.

CHANGELOG v3.4
  - Added _clean_text() helper: normalises Unicode typographic characters that
    remote PostgreSQL may store in free-text fields (subject, description, notes,
    summary) to plain ASCII equivalents. The root cause is that remote psycopg2
    connections sometimes return multi-byte UTF-8 sequences without proper
    decoding, causing each byte to render as '?' in the HTML. For example:
      U+2013 EN DASH (e2 80 93)  "Payment complete – INV-000034" → "Payment complete ??? INV-000034"
    _clean_text() translates the most common offenders:
      - En/em dashes → ASCII hyphen-minus
      - Curly quotes → straight quotes
      - Ellipsis → three dots
      - Bullet → asterisk
    Applied to subject, description, notes, summary, and details fields in all
    list/get/overdue/upcoming/timeline report modes.

CHANGELOG v3.3
  - get_owners mode: emits BOTH markdown output AND owners[] JSON array
    consumed directly by loadOwners() in the HTML frontend.
  - get_owners returns early (no footer actions appended).

CHANGELOG v3.2
  - get mode: displays ALL fields (activity info, related entity, scoring, audit, description).
  - Activity ID always shown in full (never truncated).

CHANGELOG v3.1
  - List/overdue/upcoming: Activity ID column replaced with [DETAILS:uuid] marker.
  - Score column added before Details column.
  - Footer actions use [ACTION:...] and [CREATEFORM:...] markers.

Supported modes (17):
  list, get, create, log_call, log_email, schedule_meeting, create_task,
  add_note, update, complete, reopen, delete, timeline, overdue, upcoming,
  summary, get_owners.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unicode normalisation — remote PostgreSQL encoding fix
# ---------------------------------------------------------------------------

# Characters that remote PostgreSQL may store in free-text fields.
# Each is a multi-byte UTF-8 sequence that renders as '???' per byte when
# the psycopg2 connection client_encoding doesn't match the server encoding.
# We normalise all of them to safe plain-ASCII equivalents.
_UNICODE_NORMALISE = str.maketrans({
    # Hyphens and dashes → ASCII hyphen-minus
    '\u00ad': '-',   # SOFT HYPHEN
    '\u2010': '-',   # HYPHEN
    '\u2011': '-',   # NON-BREAKING HYPHEN
    '\u2012': '-',   # FIGURE DASH
    '\u2013': '-',   # EN DASH          ← most common: "Payment complete – INV-..."
    '\u2014': '-',   # EM DASH
    '\u2015': '-',   # HORIZONTAL BAR
    '\u2212': '-',   # MINUS SIGN
    '\ufe58': '-',   # SMALL EM DASH
    '\ufe63': '-',   # SMALL HYPHEN-MINUS
    '\uff0d': '-',   # FULLWIDTH HYPHEN-MINUS
    # Curly / smart quotes → straight ASCII quotes
    '\u2018': "'",   # LEFT SINGLE QUOTATION MARK
    '\u2019': "'",   # RIGHT SINGLE QUOTATION MARK
    '\u201a': "'",   # SINGLE LOW-9 QUOTATION MARK
    '\u201b': "'",   # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    '\u201c': '"',   # LEFT DOUBLE QUOTATION MARK
    '\u201d': '"',   # RIGHT DOUBLE QUOTATION MARK
    '\u201e': '"',   # DOUBLE LOW-9 QUOTATION MARK
    '\u201f': '"',   # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    # Other common typographic chars
    '\u2026': '...',  # HORIZONTAL ELLIPSIS
    '\u2022': '*',    # BULLET
    '\u00b7': '*',    # MIDDLE DOT
    '\u2023': '*',    # TRIANGULAR BULLET
    '\u00a0': ' ',    # NON-BREAKING SPACE
})


_MANGLED_DASH_RE = re.compile(r' \?{2,3} ')

def _clean_text(value: Optional[str]) -> Optional[str]:
    """Normalise Unicode typographic characters in DB text fields to ASCII.

    Remote PostgreSQL instances may store en-dashes, curly quotes, ellipses
    and other typographic characters in subject/description/notes fields.
    When psycopg2 retrieves these without correct client_encoding, each UTF-8
    byte renders as '?' in the browser — e.g. U+2013 EN DASH (3 bytes) becomes
    '???' producing 'Payment complete ??? INV-000034' instead of
    'Payment complete - INV-000034'.

    Two-stage clean:
      1. translate() — catches Unicode chars when psycopg2 decoded correctly.
      2. regex     — catches the post-mangling " ??? " literal that lands here
                     when psycopg2 used errors='replace' before us.
    """
    if not value:
        return value
    s = str(value).translate(_UNICODE_NORMALISE)
    return _MANGLED_DASH_RE.sub(' - ', s)


# Free-text fields on an activity/event object that may carry a mangled dash.
_ACT_TEXT_FIELDS = ('subject', 'description', 'outcome', 'notes', 'details')


def _clean_activity(act: Any) -> Any:
    """Return a copy of an activity/event dict with its free-text fields
    dash-normalized, so the STRUCTURED side-channel (used by the detail view) is
    clean even when the stored data is mangled — no data fix required."""
    if not isinstance(act, dict):
        return act
    out = dict(act)
    for f in _ACT_TEXT_FIELDS:
        if isinstance(out.get(f), str):
            out[f] = _clean_text(out[f])
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_dt(value) -> str:
    if not value:
        return 'N/A'
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.strftime('%b %d, %Y %I:%M %p')
    except (ValueError, AttributeError):
        return str(value) or 'N/A'


def _fmt_date(value) -> str:
    if not value:
        return 'N/A'
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.strftime('%b %d, %Y')
    except (ValueError, AttributeError):
        return str(value) or 'N/A'


TYPE_ICONS = {
    'call':    '📞',
    'email':   '📧',
    'meeting': '🤝',
    'task':    '✅',
    'note':    '📝',
    'sms':     '💬',
    'voip':    '📱',
    'system':  '⚙️',
}


def _type_icon(t: str) -> str:
    return TYPE_ICONS.get(str(t or '').lower(), '•')


_NULL_UUID = '00000000-0000-0000-0000-000000000000'

def _owner_display(act: dict) -> str:
    """Return owner display name, falling back to '—' for null/missing owners."""
    name = act.get('owner_name') or ''
    oid  = act.get('owner_id')   or ''
    # Suppress the PostgreSQL null UUID — it means 'no owner assigned'
    if oid == _NULL_UUID:
        oid = ''
    return name or oid or '—'


def _mode_name(mode: str) -> str:
    return {
        'list':             'Activity List',
        'get':              'Activity Details',
        'create':           'Activity Created',
        'log_call':         'Call Logged',
        'log_email':        'Email Logged',
        'schedule_meeting': 'Meeting Scheduled',
        'create_task':      'Task Created',
        'add_note':         'Note Added',
        'update':           'Activity Updated',
        'complete':         'Activity Completed',
        'reopen':           'Activity Reopened',
        'delete':           'Activity Deleted',
        'timeline':         'Activity Timeline',
        'overdue':          'Overdue Activities',
        'upcoming':         'Upcoming Activities',
        'summary':          'Activity Summary',
        'get_owners':       'Owner List',
        'search':           'Activity Search',
        'bulk_update':      'Bulk Activities Updated',
        'assign':           'Activity Assigned',
        'reminder':         'Activity Reminder Sent',
    }.get(mode, 'Unknown')


def _parse_response(db_rows: List[Dict]) -> Dict:
    if not db_rows:
        return {}
    first = db_rows[0]
    # sp_activities returns via 'result' or 'sp_activities' key
    for key in ('result', 'sp_activities'):
        val = first.get(key)
        if val is not None:
            if isinstance(val, str):
                try:
                    parsed = json.loads(val)
                    # unwrap nested 'result' if present
                    return parsed.get('result', parsed) if isinstance(parsed, dict) else parsed
                except json.JSONDecodeError:
                    pass
            elif isinstance(val, dict):
                return val.get('result', val)
    return first


# ============================================================================
# PUBLIC API
# ============================================================================

def format_response(db_rows: List[Dict], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format sp_activities DB rows into the output dict expected by main.py.

    Returns dict with keys:
      output       — formatted markdown string
      mode         — SP mode string
      report_mode  — internal identifier
      owners       — list (only for get_owners mode, consumed by HTML frontend)
      success      — bool
    """
    mode     = str(params.get('mode') or 'unknown').lower().strip()
    response = _parse_response(db_rows)
    metadata = response.get('metadata', {}) or {}

    logger.info(f'Format Response (sp_activities v3.3) — mode={mode}')
    logger.info(f'metadata: {metadata}')

    # ── Executive answer — pre-formatted by the shared executive layer ───────
    if mode == 'executive_question':
        return {'output': response.get('exec_markdown') or 'No executive data available.',
                'mode': 'executive_question',
                'report_mode': 'executive_question',
                'success': True}

    # ── Web answer — pre-formatted by app/core/web_tools.py ──────────────────
    if mode == 'web_search':
        return {'output': response.get('web_markdown') or 'No web results found.',
                'mode': 'web_search',
                'report_mode': 'web_search',
                'success': True}

    is_error = (
        metadata.get('status') == 'error'
        or (isinstance(metadata.get('code'), (int, float)) and metadata.get('code') not in (0, None))
    )

    if is_error:
        output = (
            f'### ERROR\n'
            f'**Time:** {_fmt_dt(datetime.utcnow().isoformat())}\n'
            f'**Error Code:** {metadata.get("code")}\n'
            f'**Error Message:** {metadata.get("message")}\n\n'
            f'Please fix the input and try again.'
        )
        return {'output': output, 'mode': mode, 'report_mode': 'error', 'success': False}

    # ── Mode routing ──────────────────────────────────────────────────────────
    report_mode  = 'generic'
    activities:  list = []
    activity:    Optional[dict] = None
    report_data: dict = {}

    if mode == 'list' or mode == 'search':
        report_mode  = 'activity_list'
        activities   = response.get('activities') or []
        report_data['pagination'] = {
            'page':         metadata.get('page', 1),
            'pageSize':     metadata.get('page_size', 50),
            'totalRecords': metadata.get('total_records', len(activities)),
            'totalPages':   metadata.get('total_pages', 1),
        }

    elif mode == 'get':
        report_mode = 'activity_detail'
        activity    = response.get('activity')
        if activity:
            report_data['activity'] = _clean_activity(activity)

    elif mode in ('create', 'log_call', 'log_email', 'schedule_meeting', 'create_task', 'add_note'):
        report_mode = 'activity_created'
        activity    = response.get('activity')
        if activity:
            report_data['activity'] = _clean_activity(activity)

    elif mode in ('update', 'complete', 'reopen'):
        report_mode = 'activity_updated'
        activity    = response.get('activity')
        if activity:
            report_data['activity'] = _clean_activity(activity)

    elif mode == 'delete':
        report_mode = 'activity_deleted'

    elif mode == 'timeline':
        report_mode = 'timeline'
        activities  = response.get('timeline') or []
        # Pass the structured events through so the frontend can render full
        # details (amount, method, order/invoice numbers) instead of re-parsing
        # the markdown table — which only captures the "Details" cell text.
        report_data['timeline'] = [_clean_activity(a) for a in activities]
        # Forward entity_name so the frontend can display it in the timeline title
        if metadata.get('entity_name'):
            report_data['entity_name'] = metadata['entity_name']

    elif mode == 'overdue':
        report_mode = 'overdue'
        activities  = response.get('overdue') or []

    elif mode == 'upcoming':
        report_mode = 'upcoming'
        activities  = response.get('upcoming') or []

    elif mode == 'summary':
        report_mode = 'summary'
        report_data['summary'] = response.get('summary') or {}

    elif mode == 'get_owners':
        report_mode = 'owners_list'
        report_data['owners'] = response.get('owners') or []

    # =========================================================================
    # OUTPUT BUILDING
    # =========================================================================

    out: List[str] = []
    out.append(f'### {_mode_name(mode)}')
    out.append(f'**Time:** {_fmt_dt(datetime.utcnow().isoformat())}')
    out.append('')

    # ── Activity List ─────────────────────────────────────────────────────────
    if report_mode == 'activity_list':
        pg = report_data.get('pagination', {})
        if activities:
            out.append(
                f'**Page:** {pg.get("page", 1)} of {pg.get("totalPages", 1)} | '
                f'**Total:** {pg.get("totalRecords", len(activities))} activities'
            )
            out.append('')
            out.append('| Type | Subject | Owner | Due Date | Created | Related | Related Type | Score | Details |')
            out.append('|------|---------|-------|----------|---------|---------|--------------|-------|---------|')
            for act in activities:
                t    = act.get('type') or 'N/A'
                icon = _type_icon(t)
                out.append(
                    f'| {icon} {t} | **{_clean_text(act.get("subject")) or "No subject"}** | '
                    f'{_owner_display(act)} | '
                    f'{_fmt_date(act.get("due_at"))} | '
                    f'{_fmt_dt(act.get("created_at"))} | '
                    f'{act.get("related_name") or "N/A"} | '
                    f'{act.get("related_type") or "N/A"} | '
                    f'{act.get("activity_score") if act.get("activity_score") is not None else "N/A"} | '
                    f'[DETAILS:{act.get("activity_id") or ""}] |'
                )
            out.append('')

    # ── Activity Detail ───────────────────────────────────────────────────────
    elif report_mode == 'activity_detail' and activity:
        out.append(f'**{_clean_text(activity.get("subject")) or "No subject"}**')
        out.append('')

        out.append('**📋 Activity Information**')
        out.append('')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        t = activity.get('type') or 'N/A'
        out.append(f'| **Type** | {_type_icon(t)} {t} |')
        out.append(f'| **Subject** | {_clean_text(activity.get("subject")) or "N/A"} |')
        out.append(f'| **Owner** | {activity.get("owner_name") or "N/A"} |')
        out.append(f'| **Owner ID** | {activity.get("owner_id") or "N/A"} |')
        out.append(f'| **Direction** | {activity.get("direction") or "N/A"} |')
        out.append(f'| **Channel** | {activity.get("channel") or "N/A"} |')
        out.append(f'| **Due Date** | {_fmt_dt(activity.get("due_at"))} |')
        out.append(f'| **Completed At** | {_fmt_dt(activity.get("completed_at"))} |')
        out.append('')

        related = activity.get('related') or {}
        out.append('**🔗 Related Entity**')
        out.append('')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        out.append(f'| **Related Type** | {related.get("type") or activity.get("related_type") or "N/A"} |')
        out.append(f'| **Related Name** | {related.get("name") or activity.get("related_name") or "N/A"} |')
        out.append(f'| **Related ID** | {related.get("id") or activity.get("related_id") or "N/A"} |')
        out.append('')

        scoring = activity.get('scoring') or {}
        out.append('**📊 Scoring**')
        out.append('')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        score = scoring.get('activity_score') if scoring else activity.get('activity_score')
        out.append(f'| **Activity Score** | {score if score is not None else "N/A"} |')
        out.append(f'| **Touch Weight** | {scoring.get("touch_weight") if scoring else "N/A"} |')
        out.append(f'| **Last Touch** | {_fmt_dt(scoring.get("last_touch")) if scoring else "N/A"} |')
        out.append('')

        audit = activity.get('audit') or {}
        out.append('**🕐 Audit Trail**')
        out.append('')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        created_at = audit.get('created_at') or activity.get('created_at')
        updated_at = audit.get('updated_at') or activity.get('updated_at')
        out.append(f'| **Created At** | {_fmt_dt(created_at)} |')
        out.append(f'| **Created By** | {audit.get("created_by") or "N/A"} |')
        out.append(f'| **Updated At** | {_fmt_dt(updated_at)} |')
        out.append(f'| **Updated By** | {audit.get("updated_by") or "N/A"} |')
        out.append('')

        out.append('**📝 Description**')
        out.append('')
        out.append(_clean_text(activity.get('description')) or '_No description provided_')
        out.append('')

        out.append('**🔑 Activity ID**')
        out.append('')
        out.append(f'`{activity.get("activity_id") or "N/A"}`')
        out.append('')

    # ── Activity Created / Updated ────────────────────────────────────────────
    elif report_mode in ('activity_created', 'activity_updated') and activity:
        action_word = 'Created' if report_mode == 'activity_created' else 'Updated'
        out.append(f'**Activity {action_word} Successfully!**')
        out.append('')
        out.append('| Field | Value |')
        out.append('|-------|-------|')
        out.append(f'| **Activity ID** | {activity.get("activity_id") or "N/A"} |')
        t = activity.get('type') or 'N/A'
        out.append(f'| **Type** | {_type_icon(t)} {t} |')
        out.append(f'| **Subject** | {_clean_text(activity.get("subject")) or "N/A"} |')
        if activity.get('related_type'):
            out.append(f'| **Related Type** | {activity["related_type"]} |')
        if activity.get('related_name'):
            out.append(f'| **Related To** | {activity["related_name"]} |')
        if activity.get('owner_name'):
            out.append(f'| **Owner** | {activity["owner_name"]} |')
        if activity.get('due_at'):
            out.append(f'| **Due Date** | {_fmt_date(activity["due_at"])} |')
        if activity.get('completed_at'):
            out.append(f'| **Completed** | {_fmt_dt(activity["completed_at"])} |')
        out.append('')

    # ── Activity Deleted ──────────────────────────────────────────────────────
    elif report_mode == 'activity_deleted':
        out.append('**Activity Deleted Successfully**')
        out.append('')
        if metadata.get('message'):
            out.append(metadata['message'])

    # ── Timeline ──────────────────────────────────────────────────────────────
    elif report_mode == 'timeline':
        if not activities:
            out.append('**No timeline events found.**')
        else:
            out.append(f'**Timeline Events ({len(activities)})**')
            out.append('')
            out.append('| # | Event | Type | Time | Details |')
            out.append('|---|-------|------|------|---------|')
            event_icons = {'activity': '📝', 'order': '📦', 'invoice': '🧾', 'payment': '💰'}
            for idx, evt in enumerate(activities, 1):
                ts      = _fmt_dt(evt.get('timestamp'))
                summary = _clean_text(evt.get('summary') or evt.get('event_type') or 'N/A')
                icon    = event_icons.get(str(evt.get('event_type') or ''), '•')
                details = ''
                d = evt.get('details') or {}
                etype = evt.get('event_type')
                if etype == 'activity':
                    details = _clean_text(d.get('subject') or '')
                elif etype == 'order':
                    details = f"Order #{d.get('order_number') or 'N/A'}"
                elif etype == 'invoice':
                    details = f"Invoice #{d.get('invoice_number') or 'N/A'}"
                elif etype == 'payment':
                    details = f"${d.get('amount') or 'N/A'}"
                out.append(f'| {idx} | {icon} {summary} | {etype or "N/A"} | {ts} | {details} |')
            out.append('')

    # ── Overdue ───────────────────────────────────────────────────────────────
    elif report_mode == 'overdue':
        if not activities:
            out.append('**🎉 No overdue activities!**')
            out.append('')
            out.append('Great job! You have no overdue tasks or meetings.')
        else:
            total_overdue = response.get('total_overdue') or 0
            if total_overdue > len(activities):
                out.append(f'**⚠️ Overdue Activities (showing {len(activities)} of {total_overdue}, most overdue first)**')
            else:
                out.append(f'**⚠️ Overdue Activities ({len(activities)})**')
            out.append('')
            out.append('| Type | Subject | Owner | Due Date | Days Overdue | Related | Related Type | Score | Details |')
            out.append('|------|---------|-------|----------|--------------|---------|--------------|-------|---------|')
            for act in activities:
                t    = act.get('type') or 'N/A'
                icon = _type_icon(t)
                related     = act.get('related') or {}
                rel_name    = act.get('related_name') or related.get('name') or 'N/A'
                rel_type    = act.get('related_type') or related.get('type') or 'N/A'
                days_over   = act.get('days_overdue')
                score       = act.get('activity_score')
                out.append(
                    f'| {icon} {t} | **{_clean_text(act.get("subject")) or "No subject"}** | '
                    f'{_owner_display(act)} | '
                    f'{_fmt_date(act.get("due_at"))} | '
                    f'{days_over if days_over is not None else "N/A"} | '
                    f'{rel_name} | {rel_type} | '
                    f'{score if score is not None else "N/A"} | '
                    f'[DETAILS:{act.get("activity_id") or ""}] |'
                )

    # ── Upcoming ──────────────────────────────────────────────────────────────
    elif report_mode == 'upcoming':
        if not activities:
            out.append('**No upcoming activities in the next 14 days.**')
        else:
            total_upcoming = response.get('total_upcoming') or 0
            if total_upcoming > len(activities):
                out.append(f'**📅 Upcoming Activities (showing {len(activities)} of {total_upcoming}, soonest first)**')
            else:
                out.append(f'**📅 Upcoming Activities ({len(activities)})**')
            out.append('')
            out.append('| Type | Subject | Owner | Due Date | Days Until | Related | Related Type | Score | Details |')
            out.append('|------|---------|-------|----------|------------|---------|--------------|-------|---------|')
            for act in activities:
                t    = act.get('type') or 'N/A'
                icon = _type_icon(t)
                related    = act.get('related') or {}
                rel_name   = act.get('related_name') or related.get('name') or 'N/A'
                rel_type   = act.get('related_type') or related.get('type') or 'N/A'
                days_until = act.get('days_until')
                score      = act.get('activity_score')
                out.append(
                    f'| {icon} {t} | **{_clean_text(act.get("subject")) or "No subject"}** | '
                    f'{_owner_display(act)} | '
                    f'{_fmt_date(act.get("due_at"))} | '
                    f'{days_until if days_until is not None else "N/A"} | '
                    f'{rel_name} | {rel_type} | '
                    f'{score if score is not None else "N/A"} | '
                    f'[DETAILS:{act.get("activity_id") or ""}] |'
                )

    # ── Summary ───────────────────────────────────────────────────────────────
    elif report_mode == 'summary':
        summary = report_data.get('summary') or {}
        out.append('**📊 Activity Statistics**')
        out.append('')

        if summary.get('counts'):
            stats = summary['counts']
            out.append('**Overall Counts**')
            out.append('')
            out.append('| Metric | Count |')
            out.append('|--------|-------|')
            out.append(f'| Total | **{stats.get("total", 0)}** |')
            out.append(f'| Completed | {stats.get("completed", 0)} |')
            out.append(f'| Pending | {stats.get("pending", 0)} |')
            out.append(f'| Overdue | **{stats.get("overdue", 0)}** |')
            out.append('')

        if summary.get('by_type'):
            out.append('**By Type**')
            out.append('')
            out.append('| Type | Count |')
            out.append('|------|-------|')
            for item in summary['by_type']:
                t = item.get('type') or 'N/A'
                out.append(f'| {_type_icon(t)} {t} | **{item.get("count", 0)}** |')
            out.append('')

        if summary.get('by_owner'):
            out.append('**By Owner**')
            out.append('')
            out.append('| Owner Name | Count |')
            out.append('|------------|-------|')
            for owner in summary['by_owner']:
                out.append(f'| {owner.get("owner_name") or "N/A"} | **{owner.get("count", 0)}** |')
            out.append('')

        if summary.get('by_related_type'):
            out.append('**By Related Type**')
            out.append('')
            out.append('| Related Type | Count |')
            out.append('|--------------|-------|')
            for item in summary['by_related_type']:
                out.append(f'| {item.get("related_type") or "N/A"} | **{item.get("count", 0)}** |')
            out.append('')

        if summary.get('this_week') is not None or summary.get('this_month') is not None:
            out.append('**Time-Based Statistics**')
            out.append('')
            out.append('| Period | Count |')
            out.append('|--------|-------|')
            out.append(f'| Last 7 Days | **{summary.get("this_week", 0)}** |')
            out.append(f'| Last 30 Days | **{summary.get("this_month", 0)}** |')
            out.append('')

    # ── get_owners — early return (no footer actions) ─────────────────────────
    elif report_mode == 'owners_list':
        owners = report_data.get('owners') or []
        if not owners:
            out.append('**No active owners found.**')
        else:
            out.append(f'**Active Owners ({len(owners)})**')
            out.append('')
            out.append('| # | Name | Role |')
            out.append('|---|------|------|')
            for i, o in enumerate(owners, 1):
                display_name = f"{o.get('first_name') or ''} {o.get('last_name') or ''}".strip()
                role = o.get('role') or '—'
                out.append(f'| {i} | {display_name} | {role} |')
            out.append('')

        # get_owners: return early with owners array embedded — no footer
        return {
            'output':      '\n'.join(out),
            'owners':      owners,
            'mode':        mode,
            'report_mode': report_mode,
            'success':     True,
        }

    # ── Generic fallback ──────────────────────────────────────────────────────
    else:
        out.append('**Action completed successfully**')
        if metadata.get('message'):
            out.append(f'Message: {metadata["message"]}')

    # ── Footer Actions (all modes except get_owners) ──────────────────────────
    out.append('')
    out.append('---')
    out.append('Need anything else? Just ask!')
    out.append('[ACTION:Show overdue tasks]')
    out.append('[ACTION:Show upcoming activities]')
    out.append('[ACTION:Activity summary]')
    out.append('[CREATEFORM:Log a call or email]')

    return {
        'output':      '\n'.join(out),
        'mode':        mode,
        'report_mode': report_mode,
        'success':     True,
        **report_data,
    }
