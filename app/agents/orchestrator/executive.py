"""Executive Q&A layer — answers the CEO / CFO / VP question bank.

Maps natural-language executive questions onto sections of the
sp_orchestrator('executive') pack. Questions whose ground truth lives
outside the CRM (cash burn, CAC, comp, deferred revenue, ERP) get an
honest scope note plus the closest CRM proxy instead of a hallucination.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger("executive")


# ---------------------------------------------------------------------------
# Section formatters
# ---------------------------------------------------------------------------

def _money(v) -> str:
    try:
        return f"${float(v or 0):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_quarter_revenue(p: dict) -> List[str]:
    q = p.get('quarter_revenue', {})
    tq, lq = float(q.get('this_quarter') or 0), float(q.get('last_quarter') or 0)
    delta = (tq - lq) / lq * 100 if lq else None
    out = ['#### 📈 Quarterly Revenue',
           '| Metric | Value |', '| --- | --- |',
           f"| Booked this quarter | **{_money(tq)}** |",
           f"| Booked last quarter | {_money(lq)}"
           + (f" ({delta:+.1f}% QoQ) |" if delta is not None else " |"),
           f"| Invoiced this quarter | {_money(q.get('invoiced_this_quarter'))} |",
           f"| Collected this quarter | {_money(q.get('collected_this_quarter'))} |", '']
    return out


def _fmt_monthly_trend(p: dict) -> List[str]:
    rows = p.get('monthly_trend') or []
    header = ['#### 📅 Month-over-Month Trend',
              '| Month | Revenue | Orders |', '| --- | --- | --- |']
    # Compute the MoM % in CHRONOLOGICAL order so each month's delta is vs the
    # month before it...
    body = []
    prev = None
    for r in rows:
        rev = float(r.get('revenue') or 0)
        mom = f" ({(rev - prev) / prev * 100:+.0f}%)" if prev else ''
        body.append(f"| {r.get('month')} | {_money(rev)}{mom} | {r.get('orders')} |")
        prev = rev or prev
    # ...then show newest month first — execs want the latest period on top.
    body.reverse()
    return header + body + ['']


def _fmt_forecast(p: dict) -> List[str]:
    f = p.get('forecast', {})
    return ['#### 🔮 Forecast vs Commit',
            '| Metric | Value |', '| --- | --- |',
            f"| Open pipeline | **{_money(f.get('open_pipeline'))}** |",
            f"| Weighted forecast (Σ amount × probability) | **{_money(f.get('weighted'))}** |",
            f"| Commit (probability ≥ 70%) | {_money(f.get('commit'))} |",
            f"| Closing this quarter | {f.get('closing_this_quarter_count')} deals · {_money(f.get('closing_this_quarter_value'))} |",
            f"| Slipped (past close date, still open) | {f.get('slipped_count')} deals · {_money(f.get('slipped_value'))} |",
            f"| Pipeline coverage (open ÷ quarter bookings) | {f.get('pipeline_coverage')}× |", '']


def _fmt_by_owner(p: dict) -> List[str]:
    # Rank by weighted forecast DESC — the probability-weighted pipeline each rep
    # is accountable to deliver is the actionable metric (Open Value is gross
    # volume). Mirrors the analytics Owner Breakdown, which leads on Forecast.
    rows = sorted((p.get('by_owner') or []),
                  key=lambda r: float(r.get('weighted') or 0), reverse=True)[:10]
    out = ['#### 👥 Pipeline & Performance by Rep',
           '_Ranked by weighted forecast — the probability-weighted pipeline each rep is accountable to deliver._',
           '',
           '| Rep | Weighted Forecast ▼ | Open | Open Value | Won Value | Win Rate | Avg Cycle |',
           '| --- | --- | --- | --- | --- | --- | --- |']
    for r in rows:
        out.append(f"| {r.get('owner')} | {_money(r.get('weighted'))} | {r.get('open_count')} "
                   f"| {_money(r.get('open_value'))} | {_money(r.get('won_value'))} "
                   f"| {r.get('win_rate_pct') or '—'}% | {r.get('avg_cycle_days') or '—'}d |")
    out.append('')
    return out


def _fmt_win_rate(p: dict) -> List[str]:
    """Win rate comes from the METRIC REGISTRY (app/core/metrics.py) — the one
    canonical definition, so this briefing can never drift from what the Explore
    agent, the anomaly detectors or /metrics report. The KPI pack still supplies
    the sales-cycle figure (not a registered metric). Falls back to the pack's own
    number if the registry is unavailable, so a briefing never breaks."""
    w = p.get('win_rate', {})
    pct, definition = w.get('overall_pct'), None
    won, lost = w.get('won'), w.get('lost')
    try:
        from app.core import metrics
        m = metrics.compute("win_rate", "all_time")
        if m.get("value") is not None:
            pct = m["value"]
            won = m.get("numerator", won)
            lost = (m.get("denominator") or 0) - (m.get("numerator") or 0)
            definition = m.get("definition")
    except Exception as exc:
        logger.debug(f"[executive] win_rate registry lookup failed, using pack: {exc}")
    out = ['#### 🏆 Win Rate & Sales Cycle',
           f"Overall win rate **{pct}%** ({won} won / {lost} lost) · "
           f"average cycle **{w.get('avg_cycle_days')} days**."]
    if definition:
        out.append(f"  _{definition}_")
    out.append('')
    return out


def _fmt_top_deals(p: dict) -> List[str]:
    rows = p.get('top_deals') or []
    out = ['#### 💎 Top Open Deals',
           '| Deal | Account | Owner | Amount | Prob | Close | Last Activity |',
           '| --- | --- | --- | --- | --- | --- | --- |']
    for r in rows:
        flag = ' ⚠️' if r.get('slipped') else ''
        out.append(f"| {r.get('name')} | {r.get('account') or '—'} | {r.get('owner')} "
                   f"| {_money(r.get('amount'))} | {r.get('probability')}% "
                   f"| {r.get('close_date')}{flag} | {r.get('last_activity') or '—'} |")
    out.append('')
    return out


def _fmt_at_risk(p: dict) -> List[str]:
    rows = p.get('at_risk_deals') or []
    out = ['#### 🚨 At-Risk Deals',
           '| Deal | Account | Owner | Amount | Prob | Close | Risk |',
           '| --- | --- | --- | --- | --- | --- | --- |']
    for r in rows:
        out.append(f"| {r.get('name')} | {r.get('account') or '—'} | {r.get('owner')} "
                   f"| {_money(r.get('amount'))} | {r.get('probability')}% "
                   f"| {r.get('close_date')} | {r.get('risk')} |")
    out.append('')
    return out


def _fmt_concentration(p: dict) -> List[str]:
    c = p.get('concentration', {})
    out = ['#### 🏦 Customer Concentration (trailing 12 months)',
           f"Top-10 customers = **{c.get('top10_pct')}%** of "
           f"{_money(c.get('total_revenue_12m'))} revenue.", '',
           '| Account | Revenue | % of Total |', '| --- | --- | --- |']
    for r in (c.get('top_customers') or []):
        out.append(f"| {r.get('account') or '—'} | {_money(r.get('revenue'))} | {r.get('pct')}% |")
    out.append('')
    return out


def _fmt_activity_pulse(p: dict) -> List[str]:
    ap = p.get('activity_pulse', {}) or {}
    types = ap.get('by_type_30d', {}) or {}
    type_str = ' · '.join(f"{k}: **{v}**" for k, v in sorted(types.items(), key=lambda x: -x[1]))
    tm, lm = ap.get('this_month'), ap.get('last_month')
    trend = ''
    try:
        trend = f" ({(tm - lm) / lm * 100:+.0f}% MoM)" if lm else ''
    except (TypeError, ZeroDivisionError):
        pass
    out = ['#### 💓 Activity Pulse',
           f"This month **{tm}** activities vs last month {lm}{trend} · "
           f"completed (30d) **{ap.get('completed_30d')}** · "
           f"**{ap.get('overdue_open')}** tasks/meetings overdue.",
           f"Mix (30d): {type_str}", '']
    dec = ap.get('declining_accounts') or []
    if dec:
        out += ['**Accounts with declining engagement (30d vs prior 30d):**', '',
                '| Account | Prev 30d | Last 30d |', '| --- | --- | --- |']
        out += [f"| {r.get('account')} | {r.get('prev_30d')} | {r.get('last_30d')} |" for r in dec]
        out.append('')
    return out


def _fmt_idle_deals(p: dict) -> List[str]:
    rows = p.get('idle_deals') or []
    out = ['#### 😴 Idle Open Deals (no activity ≥ 14 days)',
           '| Deal | Account | Amount | Prob | Close | Days Idle |',
           '| --- | --- | --- | --- | --- | --- |']
    out += [f"| {r.get('deal')} | {r.get('account') or '—'} | {_money(r.get('amount'))} "
            f"| {r.get('probability')}% | {r.get('close_date')} | **{r.get('days_idle')}** |"
            for r in rows]
    out.append('')
    return out


def _fmt_unworked_leads(p: dict) -> List[str]:
    u = p.get('unworked_leads', {}) or {}
    out = ['#### 🚨 Unworked Leads (3+ days old, zero logged activity)',
           f"**{u.get('count')}** leads have never had a follow-up activity — "
           'SLA breaches to triage first.', '',
           '| Lead | Company | Source | Score | Age (days) |',
           '| --- | --- | --- | --- | --- |']
    out += [f"| {r.get('lead') or '—'} | {r.get('company') or '—'} | {r.get('source') or '—'} "
            f"| {r.get('score')} | {r.get('age_days')} |" for r in (u.get('top') or [])]
    out.append('')
    return out


def _fmt_product_performance(p: dict) -> List[str]:
    rows = p.get('product_performance') or []
    out = ['#### 📦 Product Performance (trailing 90 days)',
           '| Product | Revenue | Share | Margin | Margin % | This Mo | Last Mo |',
           '| --- | --- | --- | --- | --- | --- | --- |']
    for r in rows:
        name = str(r.get('product') or '—')
        if len(name) > 38:
            name = name[:37] + '…'
        out.append(f"| {name} | {_money(r.get('revenue_90d'))} | {r.get('share_pct')}% "
                   f"| {_money(r.get('margin'))} | {r.get('margin_pct')}% "
                   f"| {_money(r.get('rev_this_month'))} | {_money(r.get('rev_last_month'))} |")
    out += ['', '_Cost basis: current Wholesale price (same convention as the '
            'Accounting margin analytics)._', '']
    return out


def _fmt_product_discounts(p: dict) -> List[str]:
    rows = p.get('product_discounts') or []
    out = ['#### 🏷️ Discount Exposure by Product (trailing 90 days)',
           '| Product | Discount Given | Revenue | Discount % |', '| --- | --- | --- | --- |']
    for r in rows:
        name = str(r.get('product') or '—')
        if len(name) > 38:
            name = name[:37] + '…'
        out.append(f"| {name} | {_money(r.get('discount_total'))} "
                   f"| {_money(r.get('revenue'))} | {r.get('discount_pct')}% |")
    out.append('')
    return out


def _fmt_lead_funnel(p: dict) -> List[str]:
    lf = p.get('lead_funnel', {}) or {}
    ratings = lf.get('by_rating', {}) or {}
    rating_str = ' · '.join(f"{k}: **{v}**" for k, v in sorted(ratings.items()))
    tm, lm = lf.get('new_this_month'), lf.get('new_last_month')
    trend = ''
    try:
        trend = f" ({(tm - lm) / lm * 100:+.0f}% MoM)" if lm else ''
    except (TypeError, ZeroDivisionError):
        pass
    out = ['#### 🚿 Lead Funnel',
           f"**{lf.get('total')}** open leads · {rating_str}",
           f"New this month **{tm}** vs last month {lm}{trend} · "
           f"converted all-time **{lf.get('converted_total')}** "
           f"worth {_money(lf.get('converted_value'))} · "
           f"**{lf.get('duplicate_flagged')}** flagged as possible duplicates.", '']
    provs = lf.get('top_provinces') or []
    if provs:
        out += ['| Province | Leads |', '| --- | --- |']
        out += [f"| {r.get('province')} | {r.get('leads')} |" for r in provs]
        out.append('')
    return out


def _fmt_hot_leads(p: dict) -> List[str]:
    rows = p.get('hot_leads') or []
    out = ['#### 🔥 Hot Leads (top unconverted by score)',
           '| Lead | Company | Source | Rating | Score | Created |',
           '| --- | --- | --- | --- | --- | --- |']
    out += [f"| {r.get('lead') or '—'} | {r.get('company') or '—'} | {r.get('source') or '—'} "
            f"| {r.get('rating')} | {r.get('score')} | {r.get('created')} |" for r in rows]
    out.append('')
    return out


def _fmt_lead_source_quality(p: dict) -> List[str]:
    rows = p.get('lead_source_quality') or []
    out = ['#### 🎯 Lead Source Quality & Volume Trend',
           '| Source | Leads | Hot | New (this mo) | New (last mo) | Converted | Converted Value |',
           '| --- | --- | --- | --- | --- | --- | --- |']
    out += [f"| {r.get('source')} | {r.get('leads')} | {r.get('hot')} | {r.get('new_this_month')} "
            f"| {r.get('new_last_month')} | {r.get('converted')} | {_money(r.get('converted_value'))} |"
            for r in rows]
    out.append('')
    return out


def _fmt_pipeline_contacts(p: dict) -> List[str]:
    pc = p.get('pipeline_contacts', {}) or {}
    missing = pc.get('deals_missing_contact')
    out = ['#### 🧑‍💼 Decision Makers on Top Open Deals',
           f"**{missing}** open deals have **no contact attached** — "
           'buying-committee gaps to fix first.', '',
           '| Deal | Account | Amount | Contact | Role |',
           '| --- | --- | --- | --- | --- |']
    for r in (pc.get('top_deal_contacts') or []):
        out.append(f"| {r.get('deal')} | {r.get('account') or '—'} | {_money(r.get('amount'))} "
                   f"| {r.get('contact') or '⚠️ none'} | {r.get('role') or '—'} |")
    out.append('')
    return out


def _fmt_collections_contacts(p: dict) -> List[str]:
    rows = p.get('collections_contacts') or []
    out = ['#### 📞 Collections Contacts (who to call about past-due balances)',
           '| Contact | Role | Account | Past Due | Invoices |',
           '| --- | --- | --- | --- | --- |']
    out += [f"| {r.get('contact') or '—'} | {r.get('role') or '—'} | {r.get('account') or '—'} "
            f"| {_money(r.get('past_due'))} | {r.get('invoices')} |" for r in rows]
    out.append('')
    return out


def _fmt_contact_engagement(p: dict) -> List[str]:
    ce = p.get('contact_engagement', {}) or {}
    out = ['#### 🔥 Most-Engaged Contacts (last 30 days)',
           '| Contact | Role | Account | Activities (30d) |', '| --- | --- | --- | --- |']
    out += [f"| {r.get('contact') or '—'} | {r.get('role') or '—'} | {r.get('account') or '—'} "
            f"| {r.get('activities_30d')} |" for r in (ce.get('most_engaged') or [])]
    out += ['', '#### 🧊 Silent Key Contacts (top-revenue accounts, no activity 30d)',
            '| Contact | Role | Account | Account Revenue (12m) |', '| --- | --- | --- | --- |']
    out += [f"| {r.get('contact') or '—'} | {r.get('role') or '—'} | {r.get('account') or '—'} "
            f"| {_money(r.get('account_revenue_12m'))} |" for r in (ce.get('silent_key_contacts') or [])]
    out.append('')
    return out


def _fmt_discount_by_account(p: dict) -> List[str]:
    rows = p.get('discount_by_account') or []
    out = ['#### 🏷️ Discount Exposure by Account (trailing 90 days)',
           '| Account | Discount Given | Revenue | Discount % |', '| --- | --- | --- | --- |']
    out += [f"| {r.get('account') or '—'} | {_money(r.get('discount_total'))} "
            f"| {_money(r.get('revenue'))} | {r.get('discount_pct')}% |" for r in rows]
    out.append('')
    return out


def _fmt_industry_concentration(p: dict) -> List[str]:
    rows = p.get('industry_concentration') or []
    out = ['#### 🏭 Industry Concentration (trailing 12 months)',
           '| Industry | Revenue | % of Total |', '| --- | --- | --- |']
    out += [f"| {r.get('industry')} | {_money(r.get('revenue'))} | {r.get('pct')}% |" for r in rows]
    out.append('')
    return out


def _fmt_forecast_calibration(p: dict) -> List[str]:
    """Predicted-vs-actual by month, from the monthly forecast snapshots
    (fn_forecast_accuracy). Sparse until pre-period snapshots accrue."""
    rows = p.get('forecast_calibration') or []
    out = ['#### 🎯 Forecast Calibration (predicted vs actual)']
    if not rows:
        out += ['Calibration history is still accumulating — a forecast snapshot is captured '
                'monthly, and the first predicted-vs-actual comparison appears once a month '
                'that had a pre-period snapshot completes.', '']
        return out
    out += ['| Month | Forecast | Actual | Variance | Attainment |',
            '| --- | --- | --- | --- | --- |']
    t_f = t_a = 0.0
    for r in rows:
        fw = float(r.get('forecast_weighted') or 0)
        ac = float(r.get('actual_won') or 0)
        var = float(r.get('variance') or 0)
        t_f += fw
        t_a += ac
        att = r.get('attainment_pct')
        att_s = f"{float(att):.1f}%" if att is not None else '—'
        arrow = '▲' if var >= 0 else '▼'
        label = (f"{r.get('period_key')} (in progress)"
                 if r.get('is_current_month') else r.get('period_key'))
        out.append(f"| {label} | {_money(fw)} | {_money(ac)} | {arrow} {_money(abs(var))} | {att_s} |")
    tot_arrow = '▲' if (t_a - t_f) >= 0 else '▼'
    tot_att = f"{(t_a / t_f * 100):.1f}%" if t_f > 0 else '—'
    out.append(f"| **Total** | {_money(t_f)} | {_money(t_a)} | "
               f"{tot_arrow} {_money(abs(t_a - t_f))} | {tot_att} |")
    out.append('')
    return out


def _fmt_low_engagement(p: dict) -> List[str]:
    rows = p.get('low_engagement_accounts') or []
    out = ['#### 🥶 High-Value, Low-Engagement Accounts',
           f"**{len(rows)}** of the top trailing-12m revenue accounts have had "
           '**zero activities in the last 30 days** — account-plan candidates.', '',
           '| Account | Trailing-12m Revenue |', '| --- | --- |']
    out += [f"| {r.get('account') or '—'} | {_money(r.get('revenue_12m'))} |" for r in rows]
    out.append('')
    return out


def _fmt_unbilled(p: dict) -> List[str]:
    u = p.get('unbilled_orders', {}) or {}
    out = ['#### 🧾 Unbilled Orders (fulfilled, not yet invoiced)',
           f"**{u.get('count')}** orders worth **{_money(u.get('value'))}** are "
           'shipped/delivered/completed but have no invoice — direct cash-flow lever.', '']
    top = u.get('top') or []
    if top:
        out += ['| Order | Account | Value | Order Date |', '| --- | --- | --- | --- |']
        out += [f"| {r.get('order_number')} | {r.get('account') or '—'} "
                f"| {_money(r.get('value'))} | {r.get('order_date')} |" for r in top]
        out.append('')
    return out


def _fmt_pipeline_concentration(p: dict) -> List[str]:
    c = p.get('pipeline_concentration', {}) or {}
    out = ['#### 🧲 Pipeline Concentration (open opportunities by account)',
           f"Top-10 accounts hold **{c.get('top10_pct')}%** of the "
           f"{_money(c.get('open_pipeline_total'))} open pipeline.", '',
           '| Account | Open Value | % of Pipeline |', '| --- | --- | --- |']
    for r in (c.get('top_accounts') or []):
        out.append(f"| {r.get('account') or '—'} | {_money(r.get('open_value'))} | {r.get('pct')}% |")
    out.append('')
    return out


def _fmt_churn(p: dict) -> List[str]:
    c = p.get('churn_risk', {})
    out = ['#### 📉 Churn Risk (bought last quarter, silent this quarter)',
           f"**{c.get('silent_accounts')}** account(s) silent · "
           f"{_money(c.get('revenue_at_risk'))} last-quarter revenue at risk.", '']
    top = c.get('top_at_risk') or []
    if top:
        out += ['| Account | Last-Quarter Revenue |', '| --- | --- |']
        out += [f"| {r.get('account') or '—'} | {_money(r.get('last_quarter_revenue'))} |" for r in top]
        out.append('')
    return out


def _fmt_ar(p: dict) -> List[str]:
    a = p.get('ar_summary', {})
    out = ['#### 🧾 Receivables & Debtor Concentration',
           f"Outstanding **{_money(a.get('outstanding_total'))}** · "
           f"{a.get('overdue_count')} invoices past due.", '',
           '| Debtor | Balance | % of Outstanding |', '| --- | --- | --- |']
    for r in (a.get('top_debtors') or []):
        out.append(f"| {r.get('account') or '—'} | {_money(r.get('balance'))} | {r.get('pct_of_outstanding')}% |")
    out.append('')
    return out


def _fmt_discounts(p: dict) -> List[str]:
    d = p.get('discounts', {})
    return ['#### 🏷️ Discount Exposure (trailing 90 days)',
            '| Metric | Value |', '| --- | --- |',
            f"| Revenue (90d) | {_money(d.get('revenue_90d'))} |",
            f"| Discount vs retail-equivalent | **{_money(d.get('discount_total'))}** "
            f"({d.get('discount_pct_of_revenue')}% of retail value) |",
            f"| Lines sold below Retail price type | {d.get('non_retail_line_share_pct')}% |", '']


def _fmt_lead_sources(p: dict) -> List[str]:
    out = ['#### 🎯 Lead Source Effectiveness',
           '| Source | Opps | Open Value | Won | Won Value |',
           '| --- | --- | --- | --- | --- |']
    for r in (p.get('lead_sources') or []):
        out.append(f"| {r.get('source')} | {r.get('opps')} | {_money(r.get('open_value'))} "
                   f"| {r.get('won_count')} | {_money(r.get('won_value'))} |")
    out.append('')
    return out


def _fmt_aging(p: dict) -> List[str]:
    a = p.get('aging_deals', {})
    out = ['#### ⏳ Aging Open Deals',
           f"**{a.get('count')}** open deals older than the average won cycle "
           f"({a.get('avg_cycle_days')} days), worth {_money(a.get('value'))}.", '']
    old = a.get('oldest') or []
    if old:
        out += ['| Deal | Account | Amount | Age (days) |', '| --- | --- | --- | --- |']
        out += [f"| {r.get('name')} | {r.get('account') or '—'} | {_money(r.get('amount'))} | {r.get('age_days')} |"
                for r in old]
        out.append('')
    return out


def _fmt_scenario(p: dict) -> List[str]:
    """CFO scenario: if the quarter closes down 10%."""
    q = p.get('quarter_revenue', {})
    f = p.get('forecast', {})
    tq = float(q.get('this_quarter') or 0)
    closing = float(f.get('closing_this_quarter_value') or 0)
    base = tq + closing
    down = base * 0.9
    return ['#### 🧮 Scenario: Quarter Closes Down 10%',
            '| Metric | Base Case | −10% Case |', '| --- | --- | --- |',
            f"| Booked so far + closing pipeline | {_money(base)} | {_money(down)} |",
            f"| Revenue impact | — | **−{_money(base - down)}** |", '',
            '_Cash and headcount impact require expense/payroll data that is '
            'not in this CRM; pair this revenue delta with your finance model._', '']


def _fmt_recurring(p: dict) -> List[str]:
    r = p.get('recurring_split', {}) or {}
    return ['#### 🔁 Recurring vs First-Time Revenue (this quarter)',
            '| Metric | Value |', '| --- | --- |',
            f"| Repeat-customer revenue | **{_money(r.get('recurring_revenue'))}** "
            f"({r.get('recurring_pct')}%) from {r.get('repeat_accounts')} accounts |",
            f"| First-time customer revenue | {_money(r.get('new_revenue'))} "
            f"from {r.get('new_accounts')} accounts |", '',
            '_Subscriptions are not modeled; repeat-customer purchases are the '
            'recurring-revenue proxy._', '']


def _fmt_cash_timing(p: dict) -> List[str]:
    c = p.get('cash_timing', {}) or {}
    return ['#### ⏱️ Bookings → Cash Timing (trailing 90 days)',
            '| Metric | Value |', '| --- | --- |',
            f"| Average order → cash | **{c.get('avg_order_to_cash_days')} days** |",
            f"| Median order → cash | {c.get('median_order_to_cash_days')} days |",
            f"| Payments analysed | {c.get('payments_90d')} |", '']


def _fmt_actions(p: dict) -> List[str]:
    """Recovery levers for a ~5% shortfall — computed from the pack."""
    q = p.get('quarter_revenue', {})
    f = p.get('forecast', {})
    a = p.get('ar_summary', {})
    base = float(q.get('this_quarter') or 0) + float(f.get('closing_this_quarter_value') or 0)
    gap = base * 0.05
    return ['#### 🛠️ Recovery Levers (~5% shortfall = ' + _money(gap) + ')',
            f"1. **Collect overdue AR** — {_money(a.get('outstanding_total'))} outstanding, "
            f"{a.get('overdue_count')} invoices past due; the top debtors below can close most of the gap.",
            f"2. **Rescue slipped deals** — {f.get('slipped_count')} open deals worth "
            f"{_money(f.get('slipped_value'))} are past their close date.",
            f"3. **Pull forward commit-grade pipeline** — {_money(f.get('weighted'))} weighted; "
            f"{f.get('closing_this_quarter_count')} deals ({_money(f.get('closing_this_quarter_value'))}) "
            "already dated this quarter.",
            '4. **Tighten discounting** — see discount exposure; each point of discount '
            'recovered drops straight through.', '']


def build_headline(p: dict) -> str:
    """One-line, decision-grade summary of the financial impact right now."""
    q = p.get('quarter_revenue', {})
    f = p.get('forecast', {})
    a = p.get('ar_summary', {})
    d = p.get('discounts', {})
    tq, lq = float(q.get('this_quarter') or 0), float(q.get('last_quarter') or 0)
    qoq = f" ({(tq - lq) / lq * 100:+.0f}% QoQ)" if lq else ''
    return (f"📌 **Impact now:** Q bookings {_money(tq)}{qoq} · weighted pipeline "
            f"{_money(f.get('weighted'))} ({f.get('closing_this_quarter_count')} deals dated this Q) · "
            f"AR outstanding {_money(a.get('outstanding_total'))} "
            f"({a.get('overdue_count')} past due) · discount leakage "
            f"{d.get('discount_pct_of_revenue')}% of retail value.")


def _confidence(note) -> str:
    """High = fully CRM-backed · Medium = proxy/baseline · Low = data gap."""
    if not note:
        return 'High'
    low_markers = ('not in this CRM', 'cannot be computed', 'not stored',
                   'No ERP', 'not recorded', 'unavailable', 'not yet computable',
                   'not modeled', 'not tracked', 'not configured')
    if any(m.lower() in note.lower() for m in low_markers):
        return 'Low'
    return 'Medium'


def build_drivers(p: dict, sections: List[str]) -> List[str]:
    """Top-2 ranked drivers — most material dollar facts, biased toward the
    sections the question selected."""
    q = p.get('quarter_revenue', {})
    f = p.get('forecast', {})
    a = p.get('ar_summary', {})
    c = p.get('churn_risk', {})
    d = p.get('discounts', {})
    cn = p.get('concentration', {})
    cands = []  # (magnitude, related_section, text)
    tq, lq = float(q.get('this_quarter') or 0), float(q.get('last_quarter') or 0)
    if lq:
        delta = tq - lq
        cands.append((abs(delta), 'quarter_revenue',
                      f"bookings {'down' if delta < 0 else 'up'} "
                      f"{abs(delta) / lq * 100:.0f}% QoQ ({_money(abs(delta))})"))
    sv = float(f.get('slipped_value') or 0)
    if sv:
        cands.append((sv, 'forecast',
                      f"{f.get('slipped_count')} slipped deals worth {_money(sv)} past close date"))
    debtors = a.get('top_debtors') or []
    if debtors:
        t = debtors[0]
        cands.append((float(t.get('balance') or 0), 'ar_summary',
                      f"top debtor {t.get('account')} owes {_money(t.get('balance'))} "
                      f"({t.get('pct_of_outstanding')}% of AR)"))
    crv = float(c.get('revenue_at_risk') or 0)
    if crv:
        cands.append((crv, 'churn_risk',
                      f"{c.get('silent_accounts')} silent accounts put {_money(crv)} at churn risk"))
    dv = float(d.get('discount_total') or 0)
    if dv:
        cands.append((dv, 'discounts',
                      f"discounting gave up {_money(dv)} "
                      f"({d.get('discount_pct_of_revenue')}% of retail) in 90 days"))
    topc = (cn.get('top_customers') or [{}])[0]
    if topc.get('revenue'):
        cands.append((float(topc['revenue']), 'concentration',
                      f"{topc.get('account')} alone is {topc.get('pct')}% of trailing-12m revenue"))
    ub = p.get('unbilled_orders', {}) or {}
    if float(ub.get('value') or 0) > 0:
        cands.append((float(ub['value']), 'unbilled_orders',
                      f"{ub.get('count')} fulfilled orders worth {_money(ub.get('value'))} "
                      "are not yet invoiced"))
    pc = p.get('pipeline_concentration', {}) or {}
    ptop = (pc.get('top_accounts') or [{}])[0]
    if ptop.get('open_value'):
        cands.append((float(ptop['open_value']), 'pipeline_concentration',
                      f"{ptop.get('account')} holds {ptop.get('pct')}% of open pipeline "
                      f"({_money(ptop.get('open_value'))})"))
    # Prefer drivers tied to the question's sections, then magnitude
    cands.sort(key=lambda x: (x[1] in sections, x[0]), reverse=True)
    return [t for _, _, t in cands[:3]]


_ACTIONS = {
    'ar_summary':     lambda p: ('Accounting Agent',
        f"collect {_money((p.get('ar_summary', {}).get('top_debtors') or [{}])[0].get('balance'))} "
        f"from {(p.get('ar_summary', {}).get('top_debtors') or [{}])[0].get('account')} and the "
        f"{p.get('ar_summary', {}).get('overdue_count')} past-due invoices"),
    'at_risk_deals':  lambda p: ('Deal owners',
        f"re-date or close the {p.get('forecast', {}).get('slipped_count')} slipped deals "
        f"({_money(p.get('forecast', {}).get('slipped_value'))}) this week"),
    'top_deals':      lambda p: ('VP Sales',
        'run a deal-desk on the top open deals — start with the ⚠️ slipped ones'),
    'forecast':       lambda p: ('VP Sales',
        f"convert weighted pipeline ({_money(p.get('forecast', {}).get('weighted'))}) to commit; "
        f"{p.get('forecast', {}).get('closing_this_quarter_count')} deals are dated this quarter"),
    'discounts':      lambda p: ('Sales leadership',
        f"review sub-retail pricing — {_money(p.get('discounts', {}).get('discount_total'))} "
        'given up in the last 90 days'),
    'churn_risk':     lambda p: ('Account owners',
        f"re-engage {(p.get('churn_risk', {}).get('top_at_risk') or [{}])[0].get('account')} and the other "
        f"silent accounts ({_money(p.get('churn_risk', {}).get('revenue_at_risk'))} at risk)"),
    'by_owner':       lambda p: ('VP Sales',
        'coach reps whose Activities-30d and win-rate both trail the table average'),
    'win_rate':       lambda p: ('VP Sales',
        'coach reps whose Activities-30d and win-rate both trail the table average'),
    'lead_sources':   lambda p: ('Marketing',
        f"shift budget toward {((p.get('lead_sources') or [{}])[0]).get('source')} — "
        'the top source by won value'),
    'aging_deals':    lambda p: ('Deal owners',
        f"revive or close-lost the {p.get('aging_deals', {}).get('count')} deals older than the "
        f"average cycle ({_money(p.get('aging_deals', {}).get('value'))})"),
    'cash_timing':    lambda p: ('Accounting Agent',
        f"tighten dunning — order→cash is averaging "
        f"{p.get('cash_timing', {}).get('avg_order_to_cash_days')} days"),
    'recurring_split': lambda p: ('Account managers',
        f"grow repeat-revenue share (currently "
        f"{p.get('recurring_split', {}).get('recurring_pct')}%) with re-order campaigns"),
    'quarter_revenue': lambda p: ('Revenue ops',
        'reconcile booked vs invoiced vs collected and chase the gap'),
    'monthly_trend':  lambda p: ('Revenue ops',
        'investigate the latest month-over-month change before quarter close'),
    'actions':        lambda p: ('Accounting Agent',
        f"start with AR — {_money(p.get('ar_summary', {}).get('outstanding_total'))} outstanding "
        'is the fastest lever'),
    'scenario':       lambda p: ('CFO',
        'pair the revenue delta with the finance model for cash/covenant impact'),
    'concentration':  lambda p: ('CEO / VP Sales',
        'build pipeline outside the top-10 accounts to reduce concentration risk'),
    'pipeline_concentration': lambda p: ('VP Sales',
        'source new-logo pipeline outside the top-10 accounts holding most open value'),
    'activity_pulse': lambda p: ('VP Sales',
        f"activity is {p.get('activity_pulse', {}).get('this_month')} this month vs "
        f"{p.get('activity_pulse', {}).get('last_month')} last — re-engage the declining "
        'accounts in the table'),
    'idle_deals': lambda p: ('Deal owners',
        f"touch {((p.get('idle_deals') or [{}])[0]).get('deal')} "
        f"({_money(((p.get('idle_deals') or [{}])[0]).get('amount'))}) first — "
        f"idle {((p.get('idle_deals') or [{}])[0]).get('days_idle')} days"),
    'unworked_leads': lambda p: ('Lead owners',
        f"work the {p.get('unworked_leads', {}).get('count')} untouched leads — "
        'highest scores first (table below)'),
    'product_performance': lambda p: ('Product / VP Sales',
        f"protect {((p.get('product_performance') or [{}])[0]).get('product')} — "
        f"{((p.get('product_performance') or [{}])[0]).get('share_pct')}% of 90-day revenue — "
        'and investigate the decelerating lines below'),
    'product_discounts': lambda p: ('Sales leadership',
        f"review pricing on {((p.get('product_discounts') or [{}])[0]).get('product')} — "
        'the most-discounted product in 90 days'),
    'lead_funnel': lambda p: ('Marketing',
        f"lead flow is {p.get('lead_funnel', {}).get('new_this_month')} this month vs "
        f"{p.get('lead_funnel', {}).get('new_last_month')} last — rebalance campaigns toward "
        'the strongest converting sources'),
    'hot_leads': lambda p: ('Lead owners',
        f"work {((p.get('hot_leads') or [{}])[0]).get('lead')} "
        f"({((p.get('hot_leads') or [{}])[0]).get('company')}) first — "
        'highest score in the unconverted queue'),
    'lead_source_quality': lambda p: ('Marketing',
        'shift spend toward the sources with the best converted value per lead below'),
    'pipeline_contacts': lambda p: ('Deal owners',
        f"attach contacts to the {p.get('pipeline_contacts', {}).get('deals_missing_contact')} "
        'open deals with no buying-committee member recorded'),
    'collections_contacts': lambda p: ('Accounting Agent',
        f"call {((p.get('collections_contacts') or [{}])[0]).get('contact')} first — "
        f"{_money(((p.get('collections_contacts') or [{}])[0]).get('past_due'))} past due"),
    'contact_engagement': lambda p: ('Account owners',
        'book outreach with the silent key contacts below; reinforce the most-engaged champions'),
    'discount_by_account': lambda p: ('Sales leadership',
        f"review pricing with {((p.get('discount_by_account') or [{}])[0]).get('account')} — "
        'the largest discount recipient in 90 days'),
    'industry_concentration': lambda p: ('CEO / VP Sales',
        'diversify pipeline beyond the top industry to reduce sector risk'),
    'low_engagement_accounts': lambda p: ('Account owners',
        f"book outreach with {((p.get('low_engagement_accounts') or [{}])[0]).get('account')} and the "
        'other silent top-revenue accounts this week'),
    'unbilled_orders': lambda p: ('Accounting Agent',
        f"invoice the {p.get('unbilled_orders', {}).get('count')} unbilled orders "
        f"({_money(p.get('unbilled_orders', {}).get('value'))}) to pull cash forward"),
}

_DRILL = {
    'ar_summary':      ('Accounting workspace', 'accounting-mgmt.html'),
    'cash_timing':     ('Accounting workspace', 'accounting-mgmt.html'),
    'discounts':       ('Accounting workspace', 'accounting-mgmt.html'),
    'quarter_revenue': ('Order analytics', 'order-mgmt.html'),
    'recurring_split': ('Order analytics', 'order-mgmt.html'),
    'monthly_trend':   ('Order analytics', 'order-mgmt.html'),
    'forecast':        ('Opportunity pipeline', 'opportunity-mgmt.html'),
    'forecast_calibration': ('Opportunity forecast', 'opportunity-mgmt.html'),
    'top_deals':       ('Opportunity pipeline', 'opportunity-mgmt.html'),
    'at_risk_deals':   ('Opportunity pipeline', 'opportunity-mgmt.html'),
    'aging_deals':     ('Opportunity pipeline', 'opportunity-mgmt.html'),
    'win_rate':        ('Opportunity pipeline', 'opportunity-mgmt.html'),
    'by_owner':        ('Analytics dashboard', 'analytics-mgmt.html'),
    'lead_sources':    ('Lead management', 'lead-mgmt.html'),
    'churn_risk':      ('Account management', 'account-mgmt.html'),
    'concentration':   ('Account management', 'account-mgmt.html'),
    'pipeline_concentration': ('Opportunity pipeline', 'opportunity-mgmt.html'),
    'unbilled_orders':  ('Order management', 'order-mgmt.html'),
    'activity_pulse': ('Activity management', 'activity-mgmt.html'),
    'idle_deals':     ('Activity management', 'activity-mgmt.html'),
    'unworked_leads': ('Lead management', 'lead-mgmt.html'),
    'product_performance': ('Product management', 'product-mgmt.html'),
    'product_discounts':   ('Product management', 'product-mgmt.html'),
    'lead_funnel':         ('Lead management', 'lead-mgmt.html'),
    'hot_leads':           ('Lead management', 'lead-mgmt.html'),
    'lead_source_quality': ('Lead management', 'lead-mgmt.html'),
    'pipeline_contacts':    ('Contact management', 'contact-mgmt.html'),
    'collections_contacts': ('Contact management', 'contact-mgmt.html'),
    'contact_engagement':   ('Contact management', 'contact-mgmt.html'),
    'discount_by_account': ('Account management', 'account-mgmt.html'),
    'industry_concentration': ('Account management', 'account-mgmt.html'),
    'low_engagement_accounts': ('Account management', 'account-mgmt.html'),
    'actions':         ('Accounting workspace', 'accounting-mgmt.html'),
    'scenario':        ('Analytics dashboard', 'analytics-mgmt.html'),
}


def build_decision_block(p: dict, sections: List[str], note) -> List[str]:
    """Confidence · top-2 drivers · recommended action · drill-down link."""
    out = []
    drivers = build_drivers(p, sections)
    drv = ' '.join(f"{i}) {d}" for i, d in enumerate(drivers, 1)) or '—'
    out.append(f"**Confidence:** {_confidence(note)} · **Top drivers:** {drv}")
    lead = next((sec for sec in sections if sec in _ACTIONS), None)
    if lead:
        owner, step = _ACTIONS[lead](p)
        out.append(f"**Recommended action:** {owner} — {step}.")
    lead_link = next((sec for sec in sections if sec in _DRILL), None)
    if lead_link:
        label, href = _DRILL[lead_link]
        out.append(f"**Drill-down:** [{label}]({href})")
    out.append('')
    return out


SECTION_FMT = {
    'quarter_revenue': _fmt_quarter_revenue,
    'monthly_trend':   _fmt_monthly_trend,
    'forecast':        _fmt_forecast,
    'by_owner':        _fmt_by_owner,
    'win_rate':        _fmt_win_rate,
    'top_deals':       _fmt_top_deals,
    'at_risk_deals':   _fmt_at_risk,
    'concentration':   _fmt_concentration,
    'pipeline_concentration': _fmt_pipeline_concentration,
    'unbilled_orders': _fmt_unbilled,
    'activity_pulse': _fmt_activity_pulse,
    'idle_deals':     _fmt_idle_deals,
    'unworked_leads': _fmt_unworked_leads,
    'product_performance': _fmt_product_performance,
    'product_discounts':   _fmt_product_discounts,
    'lead_funnel':         _fmt_lead_funnel,
    'hot_leads':           _fmt_hot_leads,
    'lead_source_quality': _fmt_lead_source_quality,
    'pipeline_contacts':    _fmt_pipeline_contacts,
    'collections_contacts': _fmt_collections_contacts,
    'contact_engagement':   _fmt_contact_engagement,
    'discount_by_account': _fmt_discount_by_account,
    'industry_concentration': _fmt_industry_concentration,
    'forecast_calibration': _fmt_forecast_calibration,
    'low_engagement_accounts': _fmt_low_engagement,
    'churn_risk':      _fmt_churn,
    'ar_summary':      _fmt_ar,
    'discounts':       _fmt_discounts,
    'lead_sources':    _fmt_lead_sources,
    'aging_deals':     _fmt_aging,
    'scenario':        _fmt_scenario,
    'recurring_split': _fmt_recurring,
    'cash_timing':     _fmt_cash_timing,
    'actions':         _fmt_actions,
}

ALL_SECTIONS = ['quarter_revenue', 'recurring_split', 'monthly_trend', 'forecast', 'win_rate',
                'by_owner', 'top_deals', 'at_risk_deals', 'concentration', 'pipeline_concentration', 'unbilled_orders', 'discount_by_account', 'industry_concentration', 'low_engagement_accounts', 'pipeline_contacts', 'collections_contacts', 'contact_engagement', 'lead_funnel', 'hot_leads', 'lead_source_quality', 'product_performance', 'product_discounts', 'activity_pulse', 'idle_deals', 'unworked_leads',
                'churn_risk', 'ar_summary', 'discounts', 'lead_sources',
                'aging_deals', 'cash_timing']


# ---------------------------------------------------------------------------
# Question bank — (pattern, sections, scope note)
# First match wins; most-specific patterns first.
# ---------------------------------------------------------------------------

_NO_FINANCE = ('This CRM holds bookings, invoices and collections — it has no '
               'general-ledger, expense or payroll data, so {topic} cannot be '
               'computed here. Closest CRM proxy below.')

EXEC_QA: List[Tuple[str, List[str], Optional[str]]] = [
    # ── Hard out-of-CRM topics (honest scope + proxy) ────────────────────────
    (r'cash\s+(balance|runway)|burn\s*rate|runway|covenant',
     ['quarter_revenue', 'ar_summary'],
     _NO_FINANCE.format(topic='cash balance, burn rate or runway')),
    (r'\bcac\b|acquisition\s+cost|\bltv\b|lifetime\s+value',
     ['lead_sources'],
     _NO_FINANCE.format(topic='CAC/LTV (no marketing-spend data)')),
    (r'deferred\s+revenue|recognition\s+(timing|cliff)|rev\s*rec',
     ['quarter_revenue'],
     'Deferred-revenue schedules require a revenue-recognition ledger that is '
     'not in this CRM. Booked vs invoiced vs collected below is the closest view.'),
    (r'sales\s+compensation|comp\s+run.?rate|cost\s+of\s+sales|headcount',
     ['quarter_revenue', 'by_owner'],
     _NO_FINANCE.format(topic='compensation or cost-of-sales run-rate')),
    (r'\berp\b|month.?end\s+close',
     ['quarter_revenue', 'ar_summary'],
     'No ERP integration is connected; CRM-side booked / invoiced / collected '
     'figures below are the reconciliation starting point.'),
    (r'contract\s+(terms|amendments|concentration)|amendments?\b|contracts?\s+contain|delay\s+recognition',
     ['concentration'],
     'Contract documents and amendment terms are not stored in this CRM; '
     'revenue concentration below is the contract-risk proxy.'),
    (r'forecast\s+(accuracy|bias|calibration)|forecast\s+(?:vs\.?|versus)\s+actual'
     r'|predicted\s+(?:vs\.?|versus)\s+actual|\bcalibrat\w+|how\s+accurate\w*\b[^.]*\bforecast',
     ['forecast_calibration', 'forecast', 'by_owner'],
     'Forecast calibration compares the snapshot taken before each month against what '
     'actually closed; history accrues from the monthly snapshot job, so recent months '
     'may be sparse. Current weighted-vs-commit and per-rep numbers are shown alongside.'),
    (r'strategic\s+initiative|partnership',
     ['lead_sources', 'monthly_trend'],
     'Strategic initiatives are not tracked as CRM objects; lead-source and '
     'trend data below show where new revenue is actually coming from.'),
    (r'upsell|expansion\b|cross.sell|cohort\s+retention|retention\s+curve',
     ['churn_risk', 'concentration'],
     'Opportunities are not typed as new-vs-expansion and subscriptions are not '
     'modeled, so cohort/expansion views are unavailable. Churn-risk and '
     'concentration below are the closest signals.'),
    (r'audit|document\s+links',
     ['top_deals'],
     'Top deals with owner and last-activity timestamps below; document links '
     'are not stored in this CRM.'),

    # ── Scenario ──────────────────────────────────────────────────────────────
    (r'scenario|closes?\s+down|down\s+10', ['scenario', 'quarter_revenue', 'forecast'], None),

    # ── Accounting executive variants ────────────────────────────────────────
    (r'ebitda',
     ['quarter_revenue', 'discounts'],
     'EBITDA requires operating-expense data that is not in this CRM; revenue '
     'performance and margin pressure below are the CRM-side inputs.'),
    (r'one.?time\s+vs\.?\s+recurring|recurring\s+revenue|revenue\s+split',
     ['recurring_split', 'monthly_trend'], None),
    (r'confidence\s+score|change\s+since\s+last\s+update',
     ['forecast'],
     'Only one forecast snapshot exists, so confidence drift is not yet '
     'computable — weighted vs commit below is the current confidence view.'),
    (r'bookings?[\s-]+to[\s-]+cash|cash\s+timing',
     ['cash_timing', 'quarter_revenue'], None),
    (r'recover\s+a?\s*\d+%|shortfall|immediate\s+actions|miss\s+plan|recovery\s+(actions|plan)|fall\s+short',
     ['actions', 'at_risk_deals', 'ar_summary'], None),
    (r'\b(roi|payback)\b', ['lead_sources'],
     'Marketing spend is not recorded, so ROI/payback is revenue-side only.'),
    (r'coaching|activity\s*(?:→|to)\s*conversion|conversion\s+gaps?',
     ['by_owner'],
     'Reps with low 30-day activity AND below-average win rate are the '
     'coaching candidates (compare the Activities-30d and Win-Rate columns).'),
    (r'recognition\s+timing|recognition\s+issues|special\s+billing|recognition\s+treatment',
     ['quarter_revenue', 'cash_timing'],
     'Revenue-recognition rules and ledger are not in this CRM; booked vs '
     'invoiced vs collected plus cash timing below are the closest signals.'),
    (r'missing\s+docs|unusual\s+contract|audit\s+flags?',
     ['top_deals'],
     'Documents and contract terms are not stored in this CRM; deals lacking '
     'recent activity (Last Activity column) are the audit-attention proxy.'),
    (r'\bcogs\b|cost\s+drivers|commissions?\b',
     ['discounts', 'quarter_revenue'],
     'COGS and commission data are not in this CRM. The Accounting agent has '
     'product cost/margin analytics in "Accounting summary"; discount '
     'pressure below.'),

    # ── Analytics executive variants ─────────────────────────────────────────
    (r'drivers?\s+of\s+revenue|revenue\s+variance',
     ['quarter_revenue', 'monthly_trend', 'discounts'], None),
    (r'predicted\s+revenue|next\s+90\s+days',
     ['forecast', 'monthly_trend'],
     'Prediction = weighted open pipeline dated in the window; an ML forecast '
     'model is not configured.'),
    (r'leading\s+\w*\s*indicators?',
     ['lead_sources', 'forecast', 'monthly_trend'],
     'Lead flow, pipeline creation and weighted forecast are the CRM-side '
     'leading indicators for next quarter.'),
    (r'over.?\s*(or|and)\s*under.?forecast|consistently\s+(over|under)|probability\s+drift',
     ['by_owner', 'forecast'],
     'Per-rep forecast history needs more snapshots; current per-rep pipeline '
     'vs win-rate below is the bias starting point.'),
    (r'\brevive\b', ['aging_deals', 'actions'], None),
    (r'missing\s+stakeholders?|stakeholder\s+coverage', ['top_deals', 'at_risk_deals'],
     'Stakeholder roles are not modeled on deals; stalled and at-risk deals '
     'below are the intervention list.'),

    # ── Opportunity executive variants ───────────────────────────────────────
    (r'pipeline\s+\w*\s*concentrat|concentrated?\s+.*pipeline|concentration\s+increasing',
     ['pipeline_concentration', 'concentration'], None),
    (r'cause\s+the\s+biggest|slip\s+or\s+lose|biggest\s+shortfall|delayed\s+or\s+lost|if\s+delayed',
     ['top_deals', 'at_risk_deals', 'forecast'], None),
    (r'executive\s+sponsors?|sponsorship|strategic\s+deals|executive\s+touch|executive\s+outreach',
     ['top_deals', 'at_risk_deals'],
     'Sponsor assignments are not modeled; the highest-value open and at-risk '
     'deals below are the sponsorship candidates.'),
    (r'recover\s+\w+%?\s*of\s+target|move\s+the\s+needle',
     ['actions', 'top_deals', 'ar_summary'], None),
    (r'payment\s+terms|payment\s+schedules?|deferred\s+billing|nonstandard|billing\s+terms|finance\s+review|complex\s+terms|payment\s+or\s+billing',
     ['cash_timing', 'top_deals'],
     'Payment-term clauses are not stored on deals; order→cash timing and the '
     'top open deals below are the finance-review starting point.'),
    (r'growth\s+or\s+decline|largest\s+opportunity\s+growth|top\s+movers',
     ['monthly_trend', 'lead_sources'],
     'For product-line movers, ask the Order agent for "Sales by category".'),
    (r'\bcorrelate\b|forecast\s+inaccuracy|should\s+be\s+enforced|predictive\s+fields',
     ['forecast', 'by_owner'],
     'Field-level forecast-accuracy correlation needs more forecast snapshots; '
     'probability and close-date hygiene (slipped deals below) are the first '
     'fields to enforce.'),
    (r'compress\s+cycles?|close.?date\s+slippage',
     ['forecast', 'win_rate', 'aging_deals'], None),
    (r'procurement|legal\s+signals?|missing\s+legal|(?:legal|finance)\s+approvals?',
     ['top_deals', 'at_risk_deals'],
     'Legal/procurement checklists are not modeled; stalled and slipped deals '
     'below are where close-blockers concentrate.'),

    # ── Orders executive variants ────────────────────────────────────────────
    (r'unbilled|uninvoiced|fast\s+billing|prioriti[sz]ed?\s+for\s+\w*\s*billing',
     ['unbilled_orders', 'cash_timing'], None),
    (r'stuck\s+in\s+fulfillment|blocking\s+revenue\s+recognition',
     ['unbilled_orders', 'cash_timing'],
     'Fulfillment/legal hold flags are not modeled; fulfilled-but-uninvoiced '
     'orders below are where billing is actually blocked.'),
    (r'cancellation|cancelled?\s+before\s+invoicing',
     ['unbilled_orders', 'churn_risk'],
     'Cancellation-risk flags are not modeled on orders; unbilled orders and '
     'churn-risk accounts below are the closest signals.'),
    (r'order\s+(size|book|mix)|mix\s+by\s+product',
     ['monthly_trend', 'recurring_split', 'discounts'],
     'For product-line mix detail, ask the Order agent for "Sales by category".'),
    (r'bundl\w*|\bskus?\b|selling\s+fastest',
     ['monthly_trend', 'lead_sources'],
     'SKU/bundle velocity by region is in the Order agent — ask it for '
     '"Sales by category"; revenue trend below.'),
    (r'largest\s+orders|closing\s+the\s+largest',
     ['by_owner', 'concentration'],
     'Orders are not rep-attributed; opportunity owners below are the '
     'closest rep-level view.'),
    (r'upcoming\s+orders|sufficient\s+to\s+meet',
     ['quarter_revenue', 'forecast', 'unbilled_orders'],
     'No explicit quota/target table is configured — last quarter is used as the baseline.'),
    (r'\basc\b|\bifrs\b',
     ['quarter_revenue', 'cash_timing'],
     'Accounting-standard treatment clauses are not in this CRM; booked vs '
     'invoiced vs collected and cash timing below are the review inputs.'),

    # ── Accounts executive variants ──────────────────────────────────────────
    (r'sentiment|escalations?',
     ['churn_risk', 'low_engagement_accounts'],
     'Support tickets and sentiment are not modeled in this CRM; silent and '
     'churn-risk accounts below are the closest warning signals.'),
    (r'net\s+revenue\s+retention|\bnrr\b',
     ['recurring_split', 'monthly_trend', 'churn_risk'],
     'Subscriptions are not modeled, so true NRR is unavailable; repeat-customer '
     'revenue share, trend, and churn risk below are the proxies.'),
    (r'renewal',
     ['churn_risk', 'low_engagement_accounts'],
     'Contract renewal dates are not stored; churn-risk and disengaged accounts '
     'below are the retention-play candidates.'),
    (r'(industry|geograph\w*|sector)\s+.*concentrat|concentrated\s+in\s+a\s+single',
     ['industry_concentration', 'concentration'], None),
    (r'disputes?|credit\s+memos?',
     ['ar_summary', 'unbilled_orders'],
     'Disputes and credit memos are not modeled; past-due invoices and unbilled '
     'orders below are where billing friction shows up.'),
    (r'low.engagement|high.value\s+but\s+low|account\s+plan\b',
     ['low_engagement_accounts', 'concentration'], None),
    (r'pilot\b|reference\s+(programs?|calls?)|case\s+stud(?:y|ies)',
     ['concentration', 'low_engagement_accounts'],
     'Reference/pilot fit is not scored; top-revenue accounts with healthy '
     'engagement (NOT in the low-engagement list) are the natural candidates.'),

    # ── Notifications executive variants (alert-style phrasings) ─────────────
    (r'regulatory|compliance\s+flag|board\s+notification',
     ['top_deals', 'ar_summary'],
     'Regulatory/compliance flags are not modeled in this CRM; large deals '
     'and receivable exposure below are the board-attention items.'),
    (r'reversal|order\s+amendment',
     ['unbilled_orders', 'quarter_revenue'],
     'Order reversals/amendments are not journaled; booked-vs-invoiced-vs-'
     'collected and unbilled orders below are the reconciliation view.'),
    (r'manual\s+(?:journal|adjustments?|entries)',
     ['ar_summary', 'discounts'],
     'Manual journal entries live in the accounting system, not this CRM; '
     'AR and discount movements below are the CRM-side signals.'),
    (r'policy\s+violation|outside\s+policy|flagged\s+for\s+approval',
     ['discounts', 'product_discounts'],
     'Approval workflows are not modeled; discount exposure below shows '
     'where pricing policy pressure is concentrated.'),
    (r'revenue\s+at.risk\s+alert|at.risk\s+alert',
     ['at_risk_deals', 'forecast', 'actions'], None),
    (r'\bat.?risk\s+deals?\b', ['at_risk_deals'], None),
    (r'\balert\b|\bdigest\b|needs?\s+attention',
     ['actions', 'at_risk_deals', 'ar_summary', 'unbilled_orders'], None),
    (r'discounting\s+spike|sudden\s+increase\s+in\s+discount',
     ['discounts', 'product_discounts', 'discount_by_account'], None),
    (r'concentration\s+shift|share\s+moved',
     ['concentration', 'pipeline_concentration'],
     'Concentration history is not snapshotted yet; the current trailing-12m '
     'and open-pipeline shares below are the baseline to track.'),
    (r'commission\s+liability',
     ['quarter_revenue', 'by_owner'],
     'Commission plans are not in this CRM; bookings by rep below are the '
     'liability-estimation inputs.'),
    (r'uncontacted|routed\s+but',
     ['unworked_leads'], None),
    (r'bad\s+data|contact\s+conflicts?|duplicate\s+opportunit',
     ['lead_funnel', 'pipeline_contacts'],
     'Duplicate-flagged leads and deals missing contacts below are the '
     'data-quality cleanup queue.'),

    # ── Activities executive variants ────────────────────────────────────────
    (r'engagement\s+(levels?|index)|sufficient\s+to\s+hit|activity\s+shortfalls?',
     ['activity_pulse', 'by_owner'],
     'No activity quota is configured; month-over-month volume and per-rep '
     'counts below are the engagement view.'),
    (r'declining\s+engagement|engagement\s+drop',
     ['activity_pulse', 'low_engagement_accounts'], None),
    (r'insufficient\s+activity|days\s+idle|missing\s+or\s+stale|no\s+recent\s+activity',
     ['idle_deals', 'top_deals'], None),
    (r'no\s+follow.?up|\bsla\b|reassign',
     ['unworked_leads', 'by_owner'], None),
    (r'activity\s+thresholds?|convert\s+activity|activity\s+into\s+opportunit',
     ['by_owner', 'activity_pulse'],
     'Activity quotas are not configured; per-rep activity vs win-rate below '
     'is the threshold baseline.'),
    (r'multi.threading|single\s+contact',
     ['pipeline_contacts', 'contact_engagement'], None),
    (r'marketing\s+campaigns?|campaigns?\s+produced',
     ['lead_source_quality', 'lead_funnel'],
     'Campaign objects are not modeled; lead sources are the campaign proxy.'),
    (r'predictive\s+of\s+fast|fast\s+closes|faster\s+closes',
     ['win_rate', 'by_owner'],
     'Activity→outcome attribution needs more history; win-rate and cycle '
     'data below are the available evidence.'),
    (r'accelerate\s+pipeline|activity\s+plays',
     ['idle_deals', 'actions', 'unworked_leads'], None),
    (r'contradict|stage\s+claims',
     ['idle_deals', 'at_risk_deals'],
     'Deals idle for weeks but claiming active stages are the first '
     'reconciliation targets.'),
    (r'login\s+drop|support\s+volume|usage\s+drop',
     ['activity_pulse', 'churn_risk'],
     'Usage/support telemetry is not in this CRM; activity decline and '
     'churn-risk below are the proxies.'),

    # ── Products executive variants ──────────────────────────────────────────
    # Split margin-EROSION from best-margin, and accelerat/decelerat-vs-plan from
    # driving/dragging, so each capability chip yields a DISTINCT answer instead
    # of colliding on the shared product_performance sections. Narrow patterns,
    # placed first so they win over the broader product matchers below.
    (r'margin\s+erosion|eroding[^?.]*\bmargin\b|\bmargin\b[^?.]*eroding|largest\s+margin\s+(?:loss|decline)',
     ['product_performance', 'product_discounts', 'discounts'], None),
    (r'accelerat\w*\s+or\s+decelerat\w*\s+vs\s+plan|product\s+lines?[^?.]*decelerat\w*\s+vs\s+plan',
     ['product_performance', 'forecast'], None),
    (r'discount[^?.]*(?:product|sku)|(?:product|sku)s?[^?.]*discount',
     ['product_discounts', 'discounts'], None),
    (r'product\s+portfolio|products?[^?.]*driving|products?[^?.]*dragging'
     r'|products?[^?.]*affect\s+next\s+quarter|adoption\s+changes',
     ['product_performance', 'monthly_trend'], None),
    (r'product\s+concentration|products?[^?.]*top\s+customers',
     ['product_performance', 'concentration'], None),
    (r'(?:gross|best)\s+margin|margin\s+erosion|products?[^?.]*margin|margin[^?.]*products?',
     ['product_performance', 'product_discounts'],
     'Margin uses the Wholesale-cost convention; full product cost analytics '
     'live in the Accounting agent ("Accounting summary").'),
    (r'adoption\s+targets?|time.to.value|product\s+features|usage\b',
     ['product_performance'],
     'Product usage/adoption telemetry is not in this CRM; order-revenue '
     'trend per product below is the adoption proxy.'),
    (r'launches?\b',
     ['product_performance', 'monthly_trend'],
     'Launch dates are not modeled; newest revenue lines and trend below '
     'are the launch-tracking proxy.'),
    (r'pricing\s+changes?|promotional|campaigns?\b',
     ['product_performance', 'product_discounts', 'actions'],
     'Campaign objects are not modeled; highest-share and most-discounted '
     'products below are the pricing/promo levers.'),
    (r'enablement|playbooks?\b',
     ['win_rate', 'by_owner', 'product_performance'],
     'Enablement content is not tracked; rep win-rate spread below shows '
     'where coaching/playbooks would lift velocity.'),
    (r'lost\s+deals|features?\s+\w*\s*requested',
     ['win_rate', 'at_risk_deals'],
     'Loss reasons and feature requests are not captured on deals; win/loss '
     'and at-risk lists below are the starting point.'),
    (r'stalled\s+delivery',
     ['unbilled_orders', 'cash_timing'], None),

    # ── Leads executive variants ─────────────────────────────────────────────
    (r'strategic\s+accounts?[^?.]*leads?|represented\s+in\s+new\s+leads',
     ['hot_leads', 'concentration'],
     'Lead→account matching is not automated; compare hot-lead companies '
     'below against the top-customer list.'),
    (r'leads?[^?.]*propensity|propensity[^?.]*leads?',
     ['hot_leads', 'lead_funnel'], None),
    (r'incoming\s+leads|lead\s+volume|lead\s+flow|sufficient\s+in\s+quality',
     ['lead_funnel', 'lead_source_quality'],
     'No lead-volume quota is configured; month-over-month flow and source '
     'quality below are the sufficiency view.'),
    (r'\bhot\s+leads?\b|leads?,?\s+if\s+converted|\d+\s+leads',
     ['hot_leads', 'lead_source_quality'], None),
    (r'lead\s+sources?[^?.]*trend|trending\s+(up|down)',
     ['lead_source_quality'], None),
    (r'expected\s+arr\s+from|weighted\s+expected\s+arr',
     ['lead_funnel', 'forecast'],
     'Leads carry no deal amount; converted-lead value and the weighted '
     'opportunity forecast below are the expected-ARR proxies.'),
    (r'intent\s+signals?|\brfp\b|renewed\s+intent|cold\s+but|immediate\s+outreach|prevent\s+slip',
     ['hot_leads', 'lead_funnel'],
     'Intent/RFP signals are not captured; lead score and rating below are '
     'the available prioritisation signals.'),
    (r'regions?\s+or\s+segments?\s+lack|lack\s+sufficient\s+lead',
     ['lead_funnel', 'lead_source_quality'], None),
    (r'lead\s+volume\s+drops|conversion\s+falls',
     ['lead_funnel', 'actions', 'lead_source_quality'], None),
    (r'convert\s+into\s+orders|leads?[^?.]*90\s+days',
     ['lead_source_quality', 'cash_timing', 'hot_leads'], None),
    (r'nurture|sequences?\b',
     ['lead_source_quality', 'lead_funnel'],
     'Nurture sequences are not modeled in this CRM; source velocity below '
     'shows where nurture is paying off.'),
    (r'routed?\s+to\s+which\s+rep|lead\s+routing|lead[^?.]{0,30}follow.?up'
     r'|follow.?up[^?.]{0,30}lead',
     ['by_owner', 'lead_funnel'],
     'Routing rules are not configured; per-rep load and win rates below are '
     'the assignment inputs.'),
    (r'duplicates?\b|should\s+be\s+disqualified',
     ['lead_funnel'],
     'Duplicate-flagged counts below; use "Find duplicate leads" on the Lead '
     'page for the merge workflow.'),
    (r'scoring\s+thresholds?|lead\s+scoring|lead\s+attributes?|lead\s+fields?',
     ['lead_funnel', 'hot_leads'],
     'Score distribution below; threshold tuning needs more conversion '
     'history — revisit once more leads convert.'),
    (r'handoffs?\b',
     ['lead_funnel', 'unbilled_orders'],
     'Lead→opportunity handoff steps are not instrumented; conversion counts '
     'and billing gaps below are the exception signals.'),

    # ── Contacts executive variants ──────────────────────────────────────────
    # Order matters (first match wins). committee-gaps is split out ABOVE the
    # broader decision-makers pattern, and champions / most-engaged / silent-key
    # are separated so each capability chip yields a DISTINCT decision-grade
    # answer instead of three colliding pairs.
    (r'committee\s+gaps?|incomplete\s+(?:buying\s+)?committee|committees?\s+(?:are\s+)?incomplete|single.threaded',
     ['pipeline_contacts', 'at_risk_deals'],
     'Buying-committee coverage is inferred from contacts attached to open '
     'deals; deals with thin coverage that are also at risk are below.'),
    (r'decision\s+makers?|buying\s+committees?|primary\s+contacts?\s+on',
     ['pipeline_contacts'], None),
    (r'champions?\b|advocates?\b|strongest\s+advocate',
     ['contact_engagement', 'lead_sources'],
     'Advocacy is not directly scored; most-engaged contacts plus the best-'
     'performing lead sources below are the champion proxies.'),
    (r'most\s+engaged|engaged\s+this\s+week|newly\s+added\s+to',
     ['contact_engagement', 'pipeline_contacts'], None),
    (r'high\s+influence|influential\s+contacts?|silent\s+key|gone\s+silent|key\s+contacts?\s+\w*\s*(?:silent|quiet)',
     ['contact_engagement', 'churn_risk'], None),
    (r'collections?\b|late\s+payments?',
     ['collections_contacts', 'ar_summary'], None),
    (r'contacts?,?\s+if\s+lost|threaten\s+next\s+quarter',
     ['pipeline_contacts', 'concentration'], None),
    (r'\bnps\b|net\s+promoter|negative\s+sentiment',
     ['contact_engagement', 'churn_risk'],
     'NPS/sentiment is not captured in this CRM; engagement and churn-risk '
     'signals below are the proxies.'),
    (r'newly\s+promoted|changed\s+roles?|role\s+data|approval\s+workflows?',
     ['pipeline_contacts', 'contact_engagement'],
     'Role-change history is not tracked; current roles on top deals below — '
     'enforce the contact role field to enable this.'),
    (r'propensity\s+to\s+convert|past\s+behavior',
     ['contact_engagement', 'lead_sources'],
     'Propensity scoring is not modeled; recent engagement and source quality '
     'below are the available signals.'),
    (r'refunds?\b|chargebacks?',
     ['collections_contacts', 'ar_summary'],
     'Refunds and chargebacks are not modeled; past-due exposure by contact '
     'below is the receivable-risk view.'),
    (r'data\s+confidence|low\s+data|\bmetadata\b|inconsistent\s+role',
     ['pipeline_contacts'],
     'Field-level data-quality scoring is not configured; deals missing '
     'contacts below are the most material hygiene gap.'),
    (r'procurement\s+or\s+legal|delaying\s+invoicing|po\s+issuance',
     ['collections_contacts', 'unbilled_orders'],
     'Procurement/legal roles are not flagged; billing contacts with past-due '
     'invoices and unbilled orders below are where issuance is stuck.'),

    # ── High-precedence answerables (would otherwise be swallowed by broader
    #    patterns below: 'concentration', 'by rep', 'amendments', 'sales cycle') ─
    (r'(receivab|ar\s+aging|aging.*receivab)', ['ar_summary'], None),
    (r'discount|concession|\bleakage\b|credits',
     ['discounts', 'discount_by_account'],
     'Computed as the gap between retail-equivalent and actual line prices; '
     'credit memos are not modeled.'),
    (r'(opportunit\w*|deals?)\s+(older|aging)|older\s+than\s+.*cycle',
     ['aging_deals'], None),
    (r'revenue\s+vs\.?\s+forecast|variance\s+driver|month.?to.?date|compare\s+to\s+forecast|drives?\s+the\s+variance',
     ['quarter_revenue', 'monthly_trend', 'forecast'], None),

    # ── Fully answerable from the pack ───────────────────────────────────────
    (r'on\s+track|revenue\s+target|arr\s+target|hit\s+(this\s+)?(quarter|plan)',
     ['quarter_revenue', 'forecast', 'monthly_trend'],
     'No explicit quota/target table is configured — last quarter is used as the baseline.'),
    (r'top\s+\d*\s*risks?|risks?\s+to\s+(hitting\s+)?plan',
     ['at_risk_deals', 'churn_risk', 'ar_summary'], None),
    (r'net\s+new|month.?over.?month|mom\s+trend',
     ['monthly_trend', 'churn_risk'],
     'Subscription ARR is not modeled; order revenue is the net-new proxy.'),
    (r'customers?\s+(by\s+revenue\s+)?at\s+risk|downgrade',
     ['churn_risk', 'ar_summary'], None),
    (r'(product\s+lines?|segments?)\s+(are\s+)?(accelerat|decelerat)|accelerat\w*\s+or\s+decelerat',
     ['product_performance', 'monthly_trend'],
     'Per-product month-over-month revenue below; for category rollups ask '
     'the Order agent for "Sales by category".'),
    (r'concentrat\w*|top\s+10\s+customers|%\s*revenue\s+from',
     ['concentration'], None),
    (r'weighted\s+forecast|commit|forecast\s+confidence',
     ['forecast'], None),
    (r'pipeline\s+coverage|quota',
     ['forecast', 'by_owner'],
     'Per-rep quotas are not configured; coverage is computed against quarterly bookings.'),
    (r'(win\s+rate|win\s+probability|sales\s+cycle)', ['win_rate', 'by_owner'], None),
    (r'reps?\s+(are\s+)?off|rep\s+performance|by\s+rep\b',
     ['by_owner'], None),
    (r'lead\s+source', ['lead_sources'], None),
    (r'stalled|deal\s+health|intervention|top\s+(10\s+|20\s+)?deals|cross.functional|\bblockers?\b',
     ['top_deals', 'at_risk_deals'], None),
    (r'discount|concession|\bleakage\b|credits',
     ['discounts', 'discount_by_account'],
     'Computed as the gap between retail-equivalent and actual line prices; '
     'credit memos are not modeled.'),
    (r'(receivab|ar\s+aging|aging.*receivab)', ['ar_summary'], None),
    (r'slippage|slipped', ['forecast', 'at_risk_deals'], None),
    (r'bookings?\s+(to|vs\.?)\s+billings?|billings',
     ['quarter_revenue'],
     'Booked (orders) vs invoiced vs collected this quarter shown below.'),
    (r'churn', ['churn_risk'], None),
    (r'(aging|older\s+than).*(opportunit|deal)|opportunit\w*\s+aging',
     ['aging_deals'], None),
    (r'margin',
     ['discounts'],
     'Full product/account margin analytics live in the Accounting agent — ask '
     'it for "Accounting summary". Discount-driven margin pressure below.'),
    (r'executive\s+brief|exec\s+brief|board\s+pack', ALL_SECTIONS, None),
]


def match_exec_question(message: str) -> Optional[Tuple[List[str], Optional[str]]]:
    """Return (sections, note) when the message is an executive question."""
    for pat, sections, note in EXEC_QA:
        if re.search(pat, message, re.IGNORECASE):
            return sections, note
    return None


def format_exec_answer(pack: dict, sections: List[str], note: Optional[str]) -> str:
    out = ['### 💼 Executive Answer',
           f"**As of:** {str(pack.get('metadata', {}).get('as_of', ''))[:16]}", '',
           build_headline(pack), '']
    out += build_decision_block(pack, sections, note)
    if note:
        out += [f'> ℹ️ {note}', '']
    seen = set()
    for sec in sections:
        if sec in seen:
            continue
        seen.add(sec)
        fmt = SECTION_FMT.get(sec)
        if fmt:
            out += fmt(pack)
    out.append('_Source: sp_orchestrator executive pack — live CRM data._')
    return '\n'.join(out)
