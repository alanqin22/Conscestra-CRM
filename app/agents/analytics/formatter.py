"""Response formatter for Analytics Dashboard v3.1.

CHANGELOG v3.1
  - Replaced owner_id with owner_name + owner_role in:
    • Owner Breakdown table
    • AR Aging by Owner table
    • Activity Productivity table
  - All owner-related tables now show human-readable Name and Role columns.

Supported data sections (15):
  forecast_summary, ai_vs_human_forecast, owner_breakdown, period_trend,
  forecast_accuracy, open_pipeline_summary, booked_revenue,
  recent_invoiced_revenue, recent_cashflow, ar_aging, ar_aging_by_owner,
  ar_aging_by_account, ar_aging_by_product, lead_source_performance,
  activity_productivity.

Side-channel output (passed to ChatResponse):
  dashboardData   — raw dict keyed by section name (for HTML chart rendering)
  summaryMetrics  — computed KPIs dict (for KPI card widgets)
  params          — echoed SP params (for display and re-query)
  meta            — generation timestamp + record counts per section

Result key from SP:  'sp_analytics_dashboard'  (no explicit alias in SQL)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(value) -> str:
    if not value:
        return 'N/A'
    try:
        if isinstance(value, (datetime, date)):
            return value.strftime('%b %d, %Y')
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.strftime('%b %d, %Y')
    except (ValueError, AttributeError):
        return str(value) or 'N/A'


def _fmt_currency(value, currency: str = 'USD') -> str:
    if value is None:
        return '$0.00'
    try:
        num = float(value)
        return f'${num:,.2f}'
    except (TypeError, ValueError):
        return '$0.00'


def _fmt_number(value) -> str:
    if value is None:
        return '0'
    try:
        return f'{int(value):,}'
    except (TypeError, ValueError):
        return '0'


def _fmt_pct(value) -> str:
    if value is None:
        return '0.00%'
    try:
        return f'{float(value):.2f}%'
    except (TypeError, ValueError):
        return '0.00%'


def _fmt_uuid_short(value) -> str:
    if not value:
        return 'N/A'
    s = str(value)
    return s[:8] + '...' if len(s) > 8 else s


def _safe_arr(v) -> list:
    return v if isinstance(v, list) else []


def _sum_field(arr: list, field: str) -> float:
    return sum(float(row.get(field) or 0) for row in _safe_arr(arr))


def _count_field(arr: list, field: str) -> int:
    return sum(int(row.get(field) or 0) for row in _safe_arr(arr))


def _parse_response(db_rows: List[Dict]) -> Dict:
    """
    Extract the SP response from database rows.
    sp_analytics_dashboard has no explicit alias in the SQL, so psycopg2
    returns data under 'sp_analytics_dashboard'. Also checks 'result' as
    a fallback for any middleware that renames the column.
    """
    if not db_rows:
        return {}
    first = db_rows[0]
    for key in ('sp_analytics_dashboard', 'result'):
        val = first.get(key)
        if val is not None:
            if isinstance(val, str):
                try:
                    parsed = json.loads(val)
                    return parsed
                except json.JSONDecodeError:
                    pass
            elif isinstance(val, dict):
                return val
    # Fallback: treat the row itself as the response
    return first


# ============================================================================
# TABLE BUILDER
# ============================================================================

_FORMATTERS = {
    'currency':   _fmt_currency,
    'percentage': _fmt_pct,
    'number':     _fmt_number,
    'uuid':       _fmt_uuid_short,
}


def _add_table(out: List[str], title: str, icon: str, data: list, columns: list,
                skip_if_empty: bool = False) -> None:
    """Append a markdown table section to `out`.

    When `skip_if_empty` is set (narrowed report types), sections with no
    data are omitted entirely instead of rendering a "No data available"
    placeholder — the full dashboard still shows every section so users can
    see what's empty, but a single-report view shouldn't be 90% placeholders.
    """
    if not data and skip_if_empty:
        return
    out.append(f'**{icon} {title}**')
    out.append('')
    if not data:
        out.append('No data available.')
        out.append('')
        return

    header = '| ' + ' | '.join(c['label'] for c in columns) + ' |'
    sep    = '| ' + ' | '.join('---' for _ in columns) + ' |'
    out.append(header)
    out.append(sep)

    for row in data:
        cells = []
        for c in columns:
            v   = row.get(c['key'])
            fmt = c.get('format')
            if fmt and fmt in _FORMATTERS:
                cells.append(_FORMATTERS[fmt](v))
            else:
                cells.append(str(v) if v is not None else 'N/A')
        out.append('| ' + ' | '.join(cells) + ' |')

    out.append('')


# ============================================================================
# SUMMARY METRICS
# ============================================================================

def _compute_summary(d: dict) -> dict:
    pipeline_data = d.get('pipeline_summary') or d.get('open_pipeline_summary')
    invoiced_data = d.get('invoiced_revenue') or d.get('recent_invoiced_revenue')
    cashflow_data = d.get('cashflow') or d.get('recent_cashflow')

    sm: dict = {
        'totalPipelineAmount':        _sum_field(pipeline_data, 'total_amount'),
        'totalWeightedPipeline':      _sum_field(pipeline_data, 'weighted_amount'),
        'totalPipelineOpportunities': _count_field(pipeline_data, 'opportunity_count'),

        'totalForecastAmount':        _sum_field(d.get('forecast_summary'), 'forecast_amount'),
        'totalForecastPipeline':      _sum_field(d.get('forecast_summary'), 'total_amount'),
        'totalForecastOpportunities': _count_field(d.get('forecast_summary'), 'opportunity_count'),

        'totalBookedRevenue':         _sum_field(d.get('booked_revenue'), 'booked_revenue'),
        'totalDiscounts':             _sum_field(d.get('booked_revenue'), 'discount_total'),
        'totalLineItems':             _count_field(d.get('booked_revenue'), 'line_count'),

        'totalInvoiced':              _sum_field(invoiced_data, 'invoiced_amount'),
        'totalPaid':                  _sum_field(invoiced_data, 'paid_amount'),
        'totalOutstanding':           _sum_field(invoiced_data, 'outstanding_amount'),

        'totalAROutstanding':         _sum_field(d.get('ar_aging'), 'outstanding_amount'),
        'totalARInvoices':            _count_field(d.get('ar_aging'), 'invoice_count'),

        'totalARByOwner':             _sum_field(d.get('ar_aging_by_owner'), 'outstanding_amount'),
        'totalAROwnerInvoices':       _count_field(d.get('ar_aging_by_owner'), 'invoice_count'),

        'totalARByAccount':           _sum_field(d.get('ar_aging_by_account'), 'outstanding_amount'),
        'totalARAccountInvoices':     _count_field(d.get('ar_aging_by_account'), 'invoice_count'),

        'totalARByProduct':           _sum_field(d.get('ar_aging_by_product'), 'outstanding_amount'),
        'totalARProductInvoices':     _count_field(d.get('ar_aging_by_product'), 'invoice_count'),

        'totalCashReceived':          _sum_field(cashflow_data, 'paid_amount'),
        'totalPayments':              _count_field(cashflow_data, 'payment_count'),

        'totalActivities':            _count_field(d.get('activity_productivity'), 'activity_count'),
        'totalCompleted':             _count_field(d.get('activity_productivity'), 'completed_count'),
        'totalOverdue':               _count_field(d.get('activity_productivity'), 'overdue_count'),
    }

    sm['activityCompletionRate'] = (
        round((sm['totalCompleted'] / sm['totalActivities']) * 100, 1)
        if sm['totalActivities'] > 0 else 0
    )
    return sm


# ============================================================================
# PUBLIC API
# ============================================================================

from app.core.text_clean import clean_obj


# ---------------------------------------------------------------------------
# Cross-domain analytics renderers (blindspot A3 — service + marketing brought
# INTO the Analytics agent so it spans sales, service AND marketing)
# ---------------------------------------------------------------------------

def _pct_str(v) -> str:
    return f'{float(v):.1f}%' if v is not None else 'N/A'


def _format_service_analytics(d: Dict[str, Any]) -> str:
    """agent_ops.metrics(days) dict → markdown support-ops scorecard."""
    if not d or d.get('error'):
        return ('### 🎧 Service Analytics\n\n'
                f'Not available: {d.get("error", "no data")}.')
    days = d.get('window_days', 30)
    vol  = d.get('volume') or {}
    res  = d.get('resolution') or {}
    cost = d.get('cost') or {}
    out: List[str] = []
    out.append(f'### 🎧 Service Analytics — last {days} days')
    out.append(f'**Time:** {_fmt_date(datetime.now())}')
    out.append('')
    out.append('**📊 Support-Ops Scorecard**')
    out.append('')
    out.append('| Metric | Value |')
    out.append('| --- | --- |')
    out.append(f'| Conversations handled | {_fmt_number(vol.get("total"))} |')
    out.append(f'| Resolved (closed) | {_fmt_number(vol.get("closed"))} |')
    out.append(f'| Containment rate (AI-resolved, no human) | {_pct_str(d.get("containment_rate"))} |')
    out.append(f'| Escalation rate (ever handed to a human) | {_pct_str(d.get("escalation_rate"))} |')
    out.append(f'| Awaiting a human now | {_fmt_number(d.get("awaiting_human"))} |')
    out.append(f'| CSAT proxy (% non-negative) | {_pct_str(d.get("csat_proxy_pct"))} |')
    out.append(f'| Avg messages to close | {res.get("avg_messages") if res.get("avg_messages") is not None else "N/A"} |')
    out.append(f'| Avg hours to close | {res.get("avg_hours_to_close") if res.get("avg_hours_to_close") is not None else "N/A"} |')
    _pcu = cost.get('per_conversation_usd')
    out.append(f'| Cost per conversation | {_fmt_currency(_pcu) if _pcu is not None else "N/A"} |')
    _lu = cost.get('llm_usd')
    out.append(f'| LLM spend (window) | {_fmt_currency(_lu) if _lu is not None else "N/A"} |')
    out.append('')
    if vol.get('by_channel'):
        out.append('**📡 By Channel**')
        out.append('')
        out.append('| Channel | Conversations |')
        out.append('| --- | --- |')
        for c in vol['by_channel']:
            out.append(f'| {c.get("channel")} | {_fmt_number(c.get("count"))} |')
        out.append('')
    if not d.get('migration_applied'):
        out.append('_Containment/escalation need the takeover columns '
                   '(sql/agent_console.sql); other metrics shown regardless._')
    out.append('---')
    out.append('Service, sales and marketing analytics now live in one place. '
               'Try "marketing performance" or "show pipeline summary".')
    return '\n'.join(out)


def _format_explore(result: Dict[str, Any]) -> str:
    """Ad-hoc explore result → markdown table + interpreted-spec echo (trust).
    `result` = {spec, columns, rows, note} or {error}."""
    if not result or result.get('error'):
        return ('### 🔎 Ad-hoc Analytics\n\n'
                f'{result.get("error", "No result.")}\n\n'
                '_Try grouping a known area — e.g. "opportunities by stage", '
                '"win rate by lead source", "orders by month", "leads by source"._')
    cols = result.get('columns') or []
    rows = result.get('rows') or []
    out: List[str] = []
    out.append('### 🔎 Ad-hoc Analytics')
    out.append(f'**Time:** {_fmt_date(datetime.now())}')
    out.append('')
    if result.get('note'):
        out.append(result['note'])
        out.append('')
    if not cols:
        out.append('No columns resolved.')
        return '\n'.join(out)
    out.append('| ' + ' | '.join(c['label'] for c in cols) + ' |')
    out.append('| ' + ' | '.join('---' for _ in cols) + ' |')
    if not rows:
        out.append('| ' + ' | '.join('—' for _ in cols) + ' |')
        out.append('')
        out.append('_No rows matched._')
        return '\n'.join(out)
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c['key'])
            fmt = c.get('format')
            if fmt in _FORMATTERS and c['kind'] == 'measure':
                cells.append(_FORMATTERS[fmt](v))
            else:
                cells.append(str(v) if v is not None else 'N/A')
        out.append('| ' + ' | '.join(cells) + ' |')
    out.append('')
    out.append(f'_{len(rows)} row(s). Ad-hoc query over the governed semantic '
               'model (read-only)._')
    return '\n'.join(out)


def _format_marketing_analytics(d: Dict[str, Any]) -> str:
    """marketing.marketing_analytics(days) dict → markdown campaign portfolio."""
    if not d or d.get('error'):
        return ('### 📣 Marketing Analytics\n\n'
                f'Not available: {d.get("error", "no data")}.')
    days = d.get('window_days', 90)
    camp = d.get('campaigns') or {}
    snd  = d.get('sends') or {}
    eng  = d.get('engagement') or {}
    out: List[str] = []
    out.append(f'### 📣 Marketing Analytics — last {days} days')
    out.append(f'**Time:** {_fmt_date(datetime.now())}')
    out.append('')
    out.append('**📊 Campaign Portfolio**')
    out.append('')
    out.append('| Metric | Value |')
    out.append('| --- | --- |')
    out.append(f'| Campaigns | {_fmt_number(camp.get("total"))} |')
    _bys = camp.get('by_status') or {}
    if _bys:
        out.append(f'| By status | {", ".join(f"{k}: {v}" for k, v in _bys.items())} |')
    out.append(f'| Emails sent | {_fmt_number(snd.get("sent"))} |')
    out.append(f'| Suppressed (CASL opt-out) | {_fmt_number(snd.get("suppressed"))} |')
    out.append(f'| Accounts replied | {_fmt_number(eng.get("accounts_replied"))} |')
    out.append(f'| Reply rate | {_pct_str(eng.get("reply_rate_pct"))} |')
    out.append(f'| Orders since launch | {_fmt_number(eng.get("orders"))} |')
    out.append(f'| Attributed order value | {_fmt_currency(eng.get("order_value"))} |')
    out.append('')
    if d.get('top_campaigns'):
        out.append('**🏆 Top Campaigns (by attributed order value)**')
        out.append('')
        out.append('| Campaign | Status | Sent | Attributed Revenue |')
        out.append('| --- | --- | --- | --- |')
        for c in d['top_campaigns']:
            out.append(f'| {c.get("name")} | {c.get("status")} | '
                       f'{_fmt_number(c.get("sent"))} | '
                       f'{_fmt_currency(c.get("attributed_order_value"))} |')
        out.append('')
    if not camp.get('total'):
        out.append('_No campaigns in this window. Lead-source performance is '
                   'available via the sales dashboard ("lead source performance")._')
    out.append('---')
    out.append('Marketing, sales and service analytics now live in one place.')
    return '\n'.join(out)


def format_response(db_rows: List[Dict], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format sp_analytics_dashboard DB rows into the output dict expected by main.py.

    Returns dict with keys:
      output         — formatted markdown string
      mode           — 'dashboard'
      success        — bool
      dashboardData  — raw section dict (for HTML chart/table rendering)
      summaryMetrics — computed KPI dict (for KPI card widgets)
      params         — echoed SP param names (p_start_date etc.)
      meta           — generation time + record counts per section
    """
    response = clean_obj(_parse_response(db_rows))
    logger.info(f'Format Response (sp_analytics_dashboard v3.1) — keys: {list(response.keys())}')

    # ── Executive answer — pre-formatted by the shared executive layer ───────
    if str(params.get('mode') or '') == 'executive_question':
        return {
            'output': response.get('exec_markdown') or 'No executive data available.',
            'mode': 'executive_question', 'success': True,
            'dashboardData': {}, 'summaryMetrics': {}, 'params': {}, 'meta': {}
        }

    # ── Anomalies — pre-formatted by app/core/analytics_signals.py ───────────
    # dashboardData carries the STRUCTURED signals so the UI can render a per-
    # anomaly "Act on this" button (A5); the markdown output is the text fallback.
    if str(params.get('mode') or '') == 'anomalies':
        return {
            'output': response.get('anomalies_markdown') or 'No anomalies detected.',
            'mode': 'anomalies', 'success': True,
            'dashboardData': {'anomalies': response.get('anomalies_data') or []},
            'summaryMetrics': {}, 'params': {}, 'meta': {}
        }

    # ── Service analytics (support-ops scorecard) — blindspot A3 ─────────────
    if str(params.get('mode') or '') == 'service_analytics':
        data = response.get('service_data') or {}
        return {
            'output': _format_service_analytics(data),
            'mode': 'service_analytics', 'success': not data.get('error'),
            'dashboardData': {'service_metrics': data}, 'summaryMetrics': {},
            'params': {}, 'meta': {}
        }

    # ── Ad-hoc exploration (semantic layer) — blindspot A2 ───────────────────
    if str(params.get('mode') or '') == 'explore':
        result = response.get('explore_result') or {}
        return {
            'output': _format_explore(result),
            'mode': 'explore', 'success': not result.get('error'),
            'dashboardData': ({'explore': result.get('rows') or []}
                              if not result.get('error') else {}),
            'summaryMetrics': {}, 'params': {}, 'meta': {'spec': result.get('spec')}
        }

    # ── Marketing analytics (campaign portfolio) — blindspot A3 ──────────────
    if str(params.get('mode') or '') == 'marketing_analytics':
        data = response.get('marketing_data') or {}
        return {
            'output': _format_marketing_analytics(data),
            'mode': 'marketing_analytics', 'success': not data.get('error'),
            'dashboardData': {'marketing_analytics': data}, 'summaryMetrics': {},
            'params': {}, 'meta': {}
        }

    # ── Web answer — pre-formatted by app/core/web_tools.py ──────────────────
    if str(params.get('mode') or '') == 'web_search':
        return {
            'output': response.get('web_markdown') or 'No web results found.',
            'mode': 'web_search', 'success': True,
            'dashboardData': {}, 'summaryMetrics': {}, 'params': {}, 'meta': {}
        }

    # ── Error check ───────────────────────────────────────────────────────────
    if not response or response.get('error'):
        error_msg = response.get('message') or 'Unknown error'
        error_code = response.get('code') or -999
        output = (
            f'### ❌ ERROR\n'
            f'**Time:** {_fmt_date(datetime.now())}\n'
            f'**Error Code:** {error_code}\n'
            f'**Error Message:** {error_msg}\n\n'
            f'Please fix the input and try again.'
        )
        return {
            'output': output, 'mode': 'error', 'success': False,
            'dashboardData': {}, 'summaryMetrics': {}, 'params': {}, 'meta': {}
        }

    dashboard_data = response

    # ── Summary metrics ───────────────────────────────────────────────────────
    summary_metrics = _compute_summary(dashboard_data)

    # ── Echoed params (p_snake_case format matching n8n formatter) ────────────
    echoed_params = {
        'p_start_date':  params.get('startDate'),
        'p_end_date':    params.get('endDate'),
        'p_owner_id':    params.get('ownerId'),
        'p_account_id':  params.get('accountId'),
        'p_product_id':  params.get('productId'),
        'p_report_type': params.get('reportType'),
    }

    # ── Text output ───────────────────────────────────────────────────────────
    # Title reflects the requested report section (falls back to the full
    # dashboard title when reportType is NULL / full_dashboard).
    _REPORT_TITLES = {
        'forecast_summary':      '### 📈 Forecast Summary',
        'pipeline_summary':      '### 🛤️ Pipeline Summary',
        'revenue_summary':       '### 💰 Revenue Summary',
        'ar_aging':              '### 📅 AR Aging Report',
        'cashflow':              '### 💸 Cashflow Analysis',
        'invoiced_revenue':      '### 🧾 Invoiced Revenue',
        'lead_source':           '### 🎯 Lead Source Performance',
        'owner_breakdown':       '### 👥 Owner Breakdown',
        'activity_productivity': '### ⚡ Activity Productivity',
        'ai_vs_human':           '### 🤖 AI vs Human Forecast',
        'firmographics':         '### 🏭 Pipeline by Firmographics',
    }
    # Narrowed (single-section) report types skip empty sections entirely —
    # the full dashboard keeps "No data available" placeholders so users can
    # see what's empty across the whole CRM.
    _report_type = str(params.get('reportType') or '').lower()
    _narrow = _report_type not in ('', 'full_dashboard')

    out: List[str] = []
    out.append(_REPORT_TITLES.get(_report_type, '### 📊 Analytics Dashboard'))
    out.append(f'**Time:** {_fmt_date(datetime.now())}')
    out.append(
        f'**Date Range:** {_fmt_date(params.get("startDate"))} '
        f'to {_fmt_date(params.get("endDate"))}'
    )
    if params.get('ownerId'):   out.append(f'**Owner Filter:** {params["ownerId"]}')
    if params.get('accountId'): out.append(f'**Account Filter:** {params["accountId"]}')
    if params.get('productId'): out.append(f'**Product Filter:** {params["productId"]}')
    out.append('')

    # Key metrics summary card
    out.append('**📈 Key Metrics Summary**')
    out.append('')
    out.append('| Metric | Value |')
    out.append('| --- | --- |')
    out.append(f'| Total Pipeline | {_fmt_currency(summary_metrics["totalPipelineAmount"])} |')
    out.append(f'| Weighted Pipeline | {_fmt_currency(summary_metrics["totalWeightedPipeline"])} |')
    out.append(f'| Booked Revenue | {_fmt_currency(summary_metrics["totalBookedRevenue"])} |')
    out.append(f'| Cash Received | {_fmt_currency(summary_metrics["totalCashReceived"])} |')
    out.append(f'| AR Outstanding | {_fmt_currency(summary_metrics["totalAROutstanding"])} |')
    out.append(f'| Activity Completion | {summary_metrics["activityCompletionRate"]}% |')
    out.append('')

    # ── Section tables ────────────────────────────────────────────────────────

    _add_table(out, 'Forecast Summary', '🎯',
               sorted(_safe_arr(dashboard_data.get('forecast_summary')),
                      key=lambda r: str(r.get('period_key') or ''), reverse=True), [
                   {'key': 'period_key',        'label': 'Period'},
                   {'key': 'forecast_type',     'label': 'Type'},
                   {'key': 'total_amount',      'label': 'Total Amount',    'format': 'currency'},
                   {'key': 'forecast_amount',   'label': 'Forecast Amount', 'format': 'currency'},
                   {'key': 'opportunity_count', 'label': 'Opportunities',   'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'AI vs Human Forecast', '🤖',
               _safe_arr(dashboard_data.get('ai_vs_human_forecast')), [
                   {'key': 'period_key',      'label': 'Period'},
                   {'key': 'ai_forecast',     'label': 'AI Forecast',  'format': 'currency'},
                   {'key': 'human_commit',    'label': 'Human Commit', 'format': 'currency'},
                   {'key': 'human_best_case', 'label': 'Best Case',    'format': 'currency'},
               ], skip_if_empty=_narrow)

    # Owner Breakdown — shows name + role (v3.1)
    _add_table(out, 'Owner Breakdown', '👥',
               _safe_arr(dashboard_data.get('owner_breakdown')), [
                   {'key': 'owner_name',       'label': 'Owner Name'},
                   {'key': 'owner_role',       'label': 'Role'},
                   {'key': 'forecast_type',    'label': 'Type'},
                   {'key': 'forecast_amount',  'label': 'Forecast Amount', 'format': 'currency'},
                   {'key': 'total_amount',     'label': 'Total Amount',    'format': 'currency'},
                   {'key': 'opportunity_count','label': 'Opportunities',   'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'Period Trend', '📈',
               _safe_arr(dashboard_data.get('period_trend')), [
                   {'key': 'period_key',      'label': 'Period'},
                   {'key': 'forecast_amount', 'label': 'Forecast Amount', 'format': 'currency'},
                   {'key': 'total_amount',    'label': 'Total Amount',    'format': 'currency'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'Forecast Accuracy', '🎯',
               sorted(_safe_arr(dashboard_data.get('forecast_accuracy')),
                      key=lambda r: str(r.get('period_key') or ''), reverse=True), [
                   {'key': 'period_key',        'label': 'Period'},
                   {'key': 'forecast_type',     'label': 'Type'},
                   {'key': 'avg_ai_confidence', 'label': 'Avg AI Confidence', 'format': 'percentage'},
                   {'key': 'snapshot_count',    'label': 'Snapshots',         'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'Pipeline Summary', '📊',
               _safe_arr(dashboard_data.get('pipeline_summary') or dashboard_data.get('open_pipeline_summary')), [
                   {'key': 'period_key',        'label': 'Period'},
                   {'key': 'stage',             'label': 'Stage'},
                   {'key': 'status',            'label': 'Status'},
                   {'key': 'total_amount',      'label': 'Amount',   'format': 'currency'},
                   {'key': 'weighted_amount',   'label': 'Weighted', 'format': 'currency'},
                   {'key': 'opportunity_count', 'label': 'Count',    'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'Booked Revenue', '💰',
               _safe_arr(dashboard_data.get('booked_revenue')), [
                   {'key': 'period_key',    'label': 'Period'},
                   {'key': 'booked_revenue','label': 'Revenue',    'format': 'currency'},
                   {'key': 'discount_total','label': 'Discounts',  'format': 'currency'},
                   {'key': 'line_count',    'label': 'Line Items', 'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'Invoiced Revenue', '📃',
               _safe_arr(dashboard_data.get('invoiced_revenue') or dashboard_data.get('recent_invoiced_revenue')), [
                   {'key': 'period_key',         'label': 'Period'},
                   {'key': 'invoiced_amount',    'label': 'Invoiced',     'format': 'currency'},
                   {'key': 'outstanding_amount', 'label': 'Outstanding',  'format': 'currency'},
                   {'key': 'paid_amount',        'label': 'Paid',         'format': 'currency'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'Cashflow', '💵',
               _safe_arr(dashboard_data.get('cashflow') or dashboard_data.get('recent_cashflow')), [
                   {'key': 'period_key',   'label': 'Period'},
                   {'key': 'paid_amount',  'label': 'Cash Received', 'format': 'currency'},
                   {'key': 'payment_count','label': 'Payments',      'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'AR Aging', '🧾',
               _safe_arr(dashboard_data.get('ar_aging')), [
                   {'key': 'aging_bucket',       'label': 'Bucket'},
                   {'key': 'outstanding_amount', 'label': 'Outstanding', 'format': 'currency'},
                   {'key': 'invoice_count',      'label': 'Invoices',    'format': 'number'},
               ], skip_if_empty=_narrow)

    # AR Aging by Owner — shows name + role (v3.1)
    _add_table(out, 'AR Aging by Owner', '👤',
               _safe_arr(dashboard_data.get('ar_aging_by_owner')), [
                   {'key': 'owner_name',         'label': 'Owner Name'},
                   {'key': 'owner_role',         'label': 'Role'},
                   {'key': 'outstanding_amount', 'label': 'Outstanding', 'format': 'currency'},
                   {'key': 'invoice_count',      'label': 'Invoices',    'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'AR Aging by Account', '🏢',
               _safe_arr(dashboard_data.get('ar_aging_by_account')), [
                   {'key': 'account_name',       'label': 'Account Name'},
                   {'key': 'account_id',         'label': 'Account ID',  'format': 'uuid'},
                   {'key': 'outstanding_amount', 'label': 'Outstanding', 'format': 'currency'},
                   {'key': 'invoice_count',      'label': 'Invoices',    'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'AR Aging by Product', '📦',
               _safe_arr(dashboard_data.get('ar_aging_by_product')), [
                   {'key': 'product_name',       'label': 'Product Name'},
                   {'key': 'product_id',         'label': 'Product ID',  'format': 'uuid'},
                   {'key': 'outstanding_amount', 'label': 'Outstanding', 'format': 'currency'},
                   {'key': 'invoice_count',      'label': 'Invoices',    'format': 'number'},
               ], skip_if_empty=_narrow)

    _add_table(out, 'Lead Source Performance', '📣',
               sorted(_safe_arr(dashboard_data.get('lead_source_performance')),
                      key=lambda r: float(r.get('weighted_pipeline') or 0), reverse=True), [
                   {'key': 'lead_source',       'label': 'Source'},
                   {'key': 'pipeline_amount',   'label': 'Pipeline',     'format': 'currency'},
                   {'key': 'weighted_pipeline', 'label': 'Weighted',     'format': 'currency'},
                   {'key': 'opportunity_count', 'label': 'Opportunities','format': 'number'},
               ], skip_if_empty=_narrow)

    # Activity Productivity — shows name + role (v3.1)
    _add_table(out, 'Activity Productivity', '📝',
               _safe_arr(dashboard_data.get('activity_productivity')), [
                   {'key': 'owner_name',      'label': 'Owner Name'},
                   {'key': 'owner_role',      'label': 'Role'},
                   {'key': 'activity_type',   'label': 'Type'},
                   {'key': 'activity_count',  'label': 'Total',     'format': 'number'},
                   {'key': 'completed_count', 'label': 'Completed', 'format': 'number'},
                   {'key': 'overdue_count',   'label': 'Overdue',   'format': 'number'},
               ], skip_if_empty=_narrow)

    # ── Pipeline by Firmographics (industry / company size / revenue) ─────────
    _firmo = dashboard_data.get('pipeline_by_firmographics') or {}
    _firmo_cols = [
        {'key': 'pipeline_amount',    'label': 'Pipeline',      'format': 'currency'},
        {'key': 'weighted_pipeline',  'label': 'Weighted',      'format': 'currency'},
        {'key': 'opportunity_count',  'label': 'Opportunities', 'format': 'number'},
    ]
    if isinstance(_firmo, dict) and any(_firmo.get(k) for k in ('by_industry', 'by_company_size', 'by_revenue_band')):
        _add_table(out, 'Pipeline by Industry', '🏭',
                   _safe_arr(_firmo.get('by_industry')),
                   [{'key': 'bucket', 'label': 'Industry'}] + _firmo_cols,
                   skip_if_empty=_narrow)
        _add_table(out, 'Pipeline by Company Size', '👔',
                   _safe_arr(_firmo.get('by_company_size')),
                   [{'key': 'bucket', 'label': 'Employees'}] + _firmo_cols,
                   skip_if_empty=_narrow)
        _add_table(out, 'Pipeline by Revenue Band', '💵',
                   _safe_arr(_firmo.get('by_revenue_band')),
                   [{'key': 'bucket', 'label': 'Revenue'}] + _firmo_cols,
                   skip_if_empty=_narrow)

    # ── Forecast Calibration (predicted-vs-actual by month) ───────────────────
    # Always surface for forecast-flavoured reports + the full dashboard, even
    # when empty (shows the "accumulating" explainer), since other report types
    # legitimately omit it.
    _calib = _safe_arr(dashboard_data.get('forecast_calibration'))
    _calib_relevant = ('forecast_calibration' in dashboard_data) and (
        not _narrow or _report_type in ('forecast_summary', 'forecast_calibration', 'forecast_accuracy'))
    if _calib:
        _add_table(out, 'Forecast Calibration (predicted vs actual)', '🎯', _calib, [
                       {'key': 'period_key',        'label': 'Month'},
                       {'key': 'forecast_weighted', 'label': 'Forecast', 'format': 'currency'},
                       {'key': 'actual_won',        'label': 'Actual',   'format': 'currency'},
                       {'key': 'variance',          'label': 'Variance', 'format': 'currency'},
                       {'key': 'attainment_pct',    'label': 'Attainment %'},
                   ])
    elif _calib_relevant:
        out.append('**🎯 Forecast Calibration (predicted vs actual)**')
        out.append('')
        out.append('_History is still accumulating — the monthly snapshot job populates '
                   'predicted-vs-actual once a month with a pre-period snapshot completes._')
        out.append('')

    out.append('---')
    out.append('Need more details? Try filtering by owner, account, or product!')

    # ── Meta record counts ────────────────────────────────────────────────────
    meta = {
        'generatedAt': datetime.now().isoformat(),
        'recordCounts': {
            'forecast_summary':      len(_safe_arr(dashboard_data.get('forecast_summary'))),
            'owner_breakdown':       len(_safe_arr(dashboard_data.get('owner_breakdown'))),
            'period_trend':          len(_safe_arr(dashboard_data.get('period_trend'))),
            'ai_vs_human_forecast':  len(_safe_arr(dashboard_data.get('ai_vs_human_forecast'))),
            'forecast_accuracy':     len(_safe_arr(dashboard_data.get('forecast_accuracy'))),
            'pipeline_summary':      len(_safe_arr(dashboard_data.get('pipeline_summary') or dashboard_data.get('open_pipeline_summary'))),
            'booked_revenue':        len(_safe_arr(dashboard_data.get('booked_revenue'))),
            'invoiced_revenue':      len(_safe_arr(dashboard_data.get('invoiced_revenue') or dashboard_data.get('recent_invoiced_revenue'))),
            'cashflow':              len(_safe_arr(dashboard_data.get('cashflow') or dashboard_data.get('recent_cashflow'))),
            'ar_aging':              len(_safe_arr(dashboard_data.get('ar_aging'))),
            'lead_source_performance': len(_safe_arr(dashboard_data.get('lead_source_performance'))),
            'activity_productivity': len(_safe_arr(dashboard_data.get('activity_productivity'))),
            'ar_aging_by_owner':     len(_safe_arr(dashboard_data.get('ar_aging_by_owner'))),
            'ar_aging_by_account':   len(_safe_arr(dashboard_data.get('ar_aging_by_account'))),
            'ar_aging_by_product':   len(_safe_arr(dashboard_data.get('ar_aging_by_product'))),
        }
    }

    return {
        'output':         '\n'.join(out),
        'mode':           'dashboard',
        'success':        True,
        'dashboardData':  dashboard_data,
        'summaryMetrics': summary_metrics,
        'params':         echoed_params,
        'meta':           meta,
    }
