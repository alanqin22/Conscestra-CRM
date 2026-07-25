"""Analytics Pre-Router v2.0 — Python conversion of n8n 'pre router' node.

OVERVIEW
  Inspects every incoming request and either ROUTES it directly to the SQL
  builder (router_action=True) OR passes it through to the AI Agent
  (router_action=False).

  ARCHITECTURAL PRINCIPLE:
    Structured report triggers  → ROUTED directly (no AI Agent)
    Ambiguous / NL queries      → AI Agent fallback

  Unlike every other CRM agent, the Analytics pre-router has only ONE trigger
  prefix: "analytics report:". ALL structured calls from the HTML dashboard
  use this single prefix, distinguished by the chatInput.reportType field.

DIRECT-ROUTE TRIGGERS
  All messages prefixed with "analytics report:" are routed directly.

  ┌────────────────────────────┬────────────────────────────────────────────┐
  │ chatInput.reportType       │ SP p_report_type                           │
  ├────────────────────────────┼────────────────────────────────────────────┤
  │ "full_dashboard"           │ NULL (all sections — reportType omitted)   │
  │ "forecast_summary"         │ "forecast_summary"                         │
  │ "pipeline_summary"         │ "pipeline_summary"                         │
  │ "revenue_summary"          │ "revenue_summary"                          │
  │ "ar_aging"                 │ "ar_aging"                                 │
  │ "cashflow"                 │ "cashflow"                                 │
  │ "invoiced_revenue"         │ "invoiced_revenue"                         │
  │ "lead_source"              │ "lead_source"                              │
  │ "owner_breakdown"          │ "owner_breakdown"                          │
  │ "activity_productivity"    │ "activity_productivity"                    │
  │ "ai_vs_human"              │ "ai_vs_human"                              │
  └────────────────────────────┴────────────────────────────────────────────┘

  chatInput fields used:
    reportType  : TEXT           (see table above)
    startDate   : YYYY-MM-DD     (null → AR Aging snapshot mode)
    endDate     : YYYY-MM-DD     (null → AR Aging snapshot mode)
    ownerId     : UUID (optional)
    accountId   : UUID (optional)
    productId   : UUID (optional)

AR AGING SPECIAL CASE:
  When reportType='ar_aging' and no dates are provided, the SP enters snapshot
  mode (returns current-state AR aging, recent cashflow, open pipeline).
  In this case startDate and endDate are intentionally omitted from params.

FULL DASHBOARD:
  When reportType='full_dashboard' (or not set), reportType is omitted from
  params so the SP defaults to returning all sections (p_report_type = NULL).

PASSTHRU (AI Agent fallback):
  Any message that does NOT start with "analytics report:" is forwarded to the
  AI Agent as a natural-language query.
  Examples:
    "Show pipeline for Q1 2025"
    "What's our year-to-date revenue?"
    "Compare AI forecast vs actuals for last quarter"

CHANGELOG
  v2.0 — Complete rewrite. Direct-route architecture for all 11 report types.
          AR Aging snapshot mode (no dates). NL fallback preserved.
  v1.0 — Original release (passthru / NL normalisation only).
"""

from __future__ import annotations

import calendar
import re
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)

VALID_REPORT_TYPES = {
    'full_dashboard',
    'forecast_summary',
    'pipeline_summary',
    'revenue_summary',
    'ar_aging',
    'cashflow',
    'invoiced_revenue',
    'lead_source',
    'owner_breakdown',
    'activity_productivity',
    'ai_vs_human',
    'firmographics',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _val(v: Any) -> Optional[Any]:
    """Coerce empty string / None → None; otherwise return as-is."""
    if v is None or v == '':
        return None
    return v


def _extract_uuid(s: Any) -> Optional[str]:
    m = UUID_RE.search(str(s or ''))
    return m.group(0) if m else None


def _win_days(msg: str, default: int) -> int:
    """Parse an optional trailing-window ("last 14 days", "past 3 months") for
    the service/marketing analytics modes. Falls back to `default`."""
    m = re.search(r'\b(?:last|past|trailing)\s+(\d+)\s+days?\b', msg)
    if m:
        return max(1, min(int(m.group(1)), 365))
    m = re.search(r'\b(?:last|past|trailing)\s+(\d+)\s+months?\b', msg)
    if m:
        return max(1, min(int(m.group(1)) * 30, 365))
    if re.search(r'\bthis\s+(?:week|month|quarter)\b|\blast\s+week\b', msg):
        return {'week': 7, 'month': 30, 'quarter': 90}.get(
            re.search(r'week|month|quarter', msg).group(0), default)
    return default


def _routed(params: dict) -> dict:
    logger.info(
        f"→ ROUTED: reportType={params.get('reportType', 'full_dashboard')} "
        f"| startDate={params.get('startDate')} | endDate={params.get('endDate')}"
    )
    return {'router_action': True, 'params': params}


def _passthru(raw: str) -> dict:
    logger.info(f"→ PASSTHRU: AI Agent | message={raw[:80]!r}")
    return {'router_action': False, 'current_message': raw.strip()}


# ============================================================================
# MAIN ROUTER
# ============================================================================

def route_request(body: dict, chat_input: dict, session_id: str) -> dict:
    """
    Inspect the incoming request and return a routing decision.

    Routed:   { 'router_action': True,  'params': { ... } }
    Passthru: { 'router_action': False, 'current_message': str }
    """
    logger.info('=== Analytics Pre-Router v2.0 ===')
    logger.info(f'SessionId: {session_id}')

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

    # ── Deterministic anomaly route (blindspot A1) ───────────────────────────
    # "any anomalies?", "what needs attention?", "what changed this week?",
    # "any red flags / risks?" → live trend-anomaly detection (win-rate WoW
    # drop, stalled deals, revenue slump) with recommended actions. Routed here
    # so it never depends on the AI agent's JSON discipline. Kept tight so
    # normal report requests (which carry a report keyword) don't get stolen.
    if re.search(
        r'\banomal\w*\b|\bwhat\s+needs\s+attention\b|\bneeds?\s+(?:my\s+)?attention\b'
        r'|\bwhat(?:\'s|\s+is)?\s+(?:wrong|off|going\s+on)\b|\bred\s+flags?\b'
        r'|\b(?:any|some)thing\s+(?:wrong|off|amiss|concerning)\b'
        r'|\bwhat\s+changed\b|\bany\s+(?:risks?|issues?|concerns?|alerts?)\b'
        r'|\bactionable\s+insights?\b|\bwhat\s+should\s+i\s+(?:worry|know)\b',
        msg, re.IGNORECASE):
        logger.info('-> ROUTED: anomalies (deterministic)')
        return {'router_action': True, 'params': {'mode': 'anomalies'}}

    # ── Service (support-ops) analytics route (blindspot A3) ─────────────────
    # Unifies the agent-ops service scorecard (containment / deflection /
    # escalation / CSAT / cost-per-conversation) INTO the Analytics agent, so
    # "sales, service AND marketing" is one place. Optional "last N days/months"
    # window; defaults handled downstream.
    if re.search(
        r'\bservice\s+(?:analytics|metrics|performance|report)\b|\bsupport\s+(?:analytics|metrics|performance|ops|report)\b'
        r'|\bcontainment\b|\bdeflection\b|\bescalation\s+rate\b|\bcsat\b'
        r'|\bcost\s+per\s+(?:resolution|resolved|conversation|ticket)\b'
        r'|\bagent[\s-]ops\b|\bresolution\s+(?:rate|time)\b|\bhow\s+is\s+support\b',
        msg, re.IGNORECASE):
        logger.info('-> ROUTED: service_analytics (deterministic)')
        return {'router_action': True,
                'params': {'mode': 'service_analytics', 'days': _win_days(msg, 30)}}

    # ── Marketing analytics route (blindspot A3) ─────────────────────────────
    # Campaign portfolio performance. Does NOT steal 'lead source' (a sales
    # report type) — matches campaign/marketing phrasings only.
    if re.search(
        r'\bmarketing\s+(?:analytics|metrics|performance|report|roi)\b'
        r'|\bcampaign\s+(?:analytics|performance|results?|roi|report)\b'
        r'|\bcampaigns?\s+(?:performance|doing|results?)\b|\bemail\s+campaigns?\b'
        r'|\bhow\s+are\s+(?:my\s+)?campaigns\b',
        msg, re.IGNORECASE):
        logger.info('-> ROUTED: marketing_analytics (deterministic)')
        return {'router_action': True,
                'params': {'mode': 'marketing_analytics', 'days': _win_days(msg, 90)}}
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

    # ── "analytics report:" ──────────────────────────────────────────────────
    if msg.startswith('analytics report:'):
        report_type = _val(chat_input.get('reportType')) or 'full_dashboard'
        start_date  = _val(chat_input.get('startDate'))
        end_date    = _val(chat_input.get('endDate'))
        owner_id    = _val(chat_input.get('ownerId'))    or _extract_uuid(chat_input.get('ownerId', ''))
        account_id  = _val(chat_input.get('accountId')) or _extract_uuid(chat_input.get('accountId', ''))
        product_id  = _val(chat_input.get('productId')) or _extract_uuid(chat_input.get('productId', ''))

        logger.info(
            f'[analytics report] type={report_type} '
            f'| from={start_date} | to={end_date}'
        )

        # AR Aging special case: no dates → SP snapshot mode (NULL date mode)
        if report_type == 'ar_aging' and not start_date and not end_date:
            logger.info('[ar_aging] No date range → SP snapshot mode (NULL dates)')
            return _routed({'reportType': 'ar_aging'})

        # Build params — for full_dashboard, omit reportType so SP returns all sections
        params: dict = {}
        if start_date:  params['startDate']  = start_date
        if end_date:    params['endDate']     = end_date
        if owner_id:    params['ownerId']     = owner_id
        if account_id:  params['accountId']   = account_id
        if product_id:  params['productId']   = product_id
        if report_type != 'full_dashboard':
            params['reportType'] = report_type

        return _routed(params)

    # ── Executive questions (CEO / CFO / VP bank) ─────────────────────────────
    # Interrogative phrasings ("Are we on track…?", "Which deals…?") route to
    # the shared executive Q&A layer with the decision-grade answer format.
    # Imperative report commands ("Show forecast summary") keep their existing
    # deterministic report routes below.
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

    # ── Natural-language report queries ──────────────────────────────────────
    # The AI previously guessed both reportType and dates for typed queries —
    # often wrongly (full dashboard for every report; hallucinated years).
    # Detect the report type by keyword and parse the date phrase here.
    _rt = _detect_report_type(msg)
    if _rt:
        start_date, end_date = _parse_date_range(msg)
        params: dict = {}
        if start_date and end_date:
            params['startDate'], params['endDate'] = start_date, end_date
        elif _rt == 'ar_aging':
            # No dates → SP snapshot mode (current AR aging state) — intended.
            pass
        else:
            # Default: current calendar year. End-of-year (not today) so
            # future-dated forecast periods are included.
            today = date.today()
            params['startDate'] = today.replace(month=1, day=1).isoformat()
            params['endDate']   = today.replace(month=12, day=31).isoformat()
        if _rt != 'full_dashboard':
            params['reportType'] = _rt
        logger.info(f'[NL-report] type={_rt} range={params.get("startDate")}..{params.get("endDate")}')
        return _routed(params)

    # ── Ad-hoc exploration (blindspot A2) ────────────────────────────────────
    # Last resort before the AI agent: "group-by" style questions the ~15
    # canned report types above don't cover (e.g. "opportunities by stage",
    # "leads by source", "win rate by lead source", "orders by month"). Runs
    # only AFTER the canned detectors, so pipeline/owner_breakdown/firmographics
    # etc. keep winning; the semantic-layer explore handles the long tail.
    _dim = (r'stage|status|lead\s*source|source|month|quarter|year|province|'
            r'rating|channel|employee\s*band|revenue\s*band|industry|owner|region')
    if re.search(rf'\b(?:by|per)\s+(?:{_dim})\b', msg) or re.search(
            r'\b(?:group(?:ed)?\s+by|broken?\s+down\s+by|breakdown|split\s+by)\b', msg):
        logger.info('-> ROUTED: explore (deterministic, semantic layer)')
        return {'router_action': True, 'params': {'mode': 'explore', 'nl': raw}}

    # ── Fallback: AI Agent ───────────────────────────────────────────────────
    return _passthru(raw)


# ---------------------------------------------------------------------------
# NL parsing helpers
# ---------------------------------------------------------------------------

# Ordered keyword → reportType detection. Specific phrases (ai_vs_human,
# invoiced_revenue) must be tested before their generic prefixes
# (forecast, revenue).
_RT_PATTERNS = [
    ('ai_vs_human',           r'\bai\s+(?:vs\.?|versus)\s+human\b|\bcompare\s+forecast'
                              r'|\bforecast\s+(?:vs\.?|versus)\s+actuals?\b|\bforecast\s+comparison\b'
                              r'|\bforecast\s+accuracy\b'),
    ('invoiced_revenue',      r'\binvoiced\s+revenue\b'),
    # Firmographics — must precede pipeline/revenue (which match "pipeline by
    # industry" / "revenue band" generically).
    ('firmographics',         r'\bfirmograph\w*\b|\bby\s+industry\b|\bby\s+company\s+size\b'
                              r'|\bby\s+revenue\s+band\b|\bby\s+employee\s+band\b'
                              r'|\bpipeline\s+by\s+(?:industry|company\s+size|employees?|employee\s+band|revenue(?:\s+band)?|size)\b'
                              r'|\bindustry\s+breakdown\b'),
    ('forecast_summary',      r'\bforecast\b'),
    ('pipeline_summary',      r'\bpipeline\b|\bsales\s+funnel\b|\bfunnel\b'),
    ('ar_aging',              r'\bar\s+age?ing\b|\bage?ing\s+(?:report|snapshot)\b|\bage?ing\b'
                              r'|\breceivables\b'),
    ('cashflow',              r'\bcash\s*flow\b'),
    ('lead_source',           r'\blead\s+sources?\b'),
    ('owner_breakdown',       r'\bowner\s+breakdown\b|\bby\s+owner\b|\bowner\s+performance\b'
                              r'|\bteam\s+performance\b|\brep\s+performance\b'),
    ('activity_productivity', r'\bactivity\s+productivity\b|\bproductivity\b'),
    ('revenue_summary',       r'\brevenue\b'),
    ('full_dashboard',        r'\bdashboard\b|\banalytics\b'),
]

_MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july',
           'august', 'september', 'october', 'november', 'december']


def _detect_report_type(msg: str) -> Optional[str]:
    for rt, pat in _RT_PATTERNS:
        if re.search(pat, msg, re.IGNORECASE):
            return rt
    return None


def _parse_date_range(msg: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a natural-language date phrase → (startDate, endDate) ISO strings.

    Supports: between/from A to B · Q[1-4] YYYY · <month name> YYYY ·
    last/next N days · last N months · this/last week · this/last month ·
    this/last quarter · this year / YTD · last year · bare year.
    Returns (None, None) when no recognisable phrase is present.
    """
    today = date.today()

    m = re.search(r'\b(?:between|from)\s+(\d{4}-\d{2}-\d{2})\s+(?:and|to)\s+(\d{4}-\d{2}-\d{2})\b', msg)
    if m:
        return m.group(1), m.group(2)

    m = re.search(r'\bq([1-4])\s*,?\s*(20\d{2})\b', msg, re.IGNORECASE)
    if m:
        q, yr = int(m.group(1)), int(m.group(2))
        sm = (q - 1) * 3 + 1
        em = sm + 2
        return (f'{yr}-{sm:02d}-01',
                f'{yr}-{em:02d}-{calendar.monthrange(yr, em)[1]:02d}')

    # H1 / H2 half-year ("H1 2026")
    m = re.search(r'\bh([12])\s*,?\s*(20\d{2})\b', msg, re.IGNORECASE)
    if m:
        h, yr = int(m.group(1)), int(m.group(2))
        return (f'{yr}-01-01', f'{yr}-06-30') if h == 1 else (f'{yr}-07-01', f'{yr}-12-31')

    m = re.search(rf'\b({"|".join(_MONTHS)})\s+(20\d{{2}})\b', msg, re.IGNORECASE)
    if m:
        mo = _MONTHS.index(m.group(1).lower()) + 1
        yr = int(m.group(2))
        return (f'{yr}-{mo:02d}-01',
                f'{yr}-{mo:02d}-{calendar.monthrange(yr, mo)[1]:02d}')

    # "since January" / "since 2026-02-15" → start to today
    m = re.search(rf'\bsince\s+({"|".join(_MONTHS)})\b', msg, re.IGNORECASE)
    if m:
        mo = _MONTHS.index(m.group(1).lower()) + 1
        yr = today.year if mo <= today.month else today.year - 1
        return f'{yr}-{mo:02d}-01', today.isoformat()
    m = re.search(r'\bsince\s+(\d{4}-\d{2}-\d{2})\b', msg)
    if m:
        return m.group(1), today.isoformat()

    # Month name without a year ("for June", "in May") → current year
    m = re.search(rf'\b(?:for|in|of|during)\s+({"|".join(_MONTHS)})\b(?!\s+20\d{{2}})', msg, re.IGNORECASE)
    if m:
        mo = _MONTHS.index(m.group(1).lower()) + 1
        yr = today.year
        return (f'{yr}-{mo:02d}-01',
                f'{yr}-{mo:02d}-{calendar.monthrange(yr, mo)[1]:02d}')

    if re.search(r'\btoday\b', msg):
        return today.isoformat(), today.isoformat()
    if re.search(r'\byesterday\b', msg):
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat()

    m = re.search(r'\b(?:last|past)\s+(\d+)\s+days?\b', msg)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat(), today.isoformat()

    m = re.search(r'\b(?:last|past)\s+(\d+)\s+weeks?\b', msg)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).isoformat(), today.isoformat()

    m = re.search(r'\b(?:last|past|trailing)\s+(\d+)\s+months?\b', msg)
    if m:
        n = int(m.group(1))
        yr, mo = today.year, today.month - n
        while mo <= 0:
            mo += 12
            yr -= 1
        return date(yr, mo, min(today.day, calendar.monthrange(yr, mo)[1])).isoformat(), today.isoformat()

    if re.search(r'\bthis\s+week\b', msg):
        return (today - timedelta(days=today.weekday())).isoformat(), today.isoformat()
    if re.search(r'\blast\s+week\b', msg):
        start = today - timedelta(days=today.weekday() + 7)
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if re.search(r'\bthis\s+month\b', msg):
        return today.replace(day=1).isoformat(), today.isoformat()
    if re.search(r'\blast\s+month\b', msg):
        first_this = today.replace(day=1)
        last_end = first_this - timedelta(days=1)
        return last_end.replace(day=1).isoformat(), last_end.isoformat()
    if re.search(r'\bthis\s+quarter\b', msg):
        sm = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=sm, day=1).isoformat(), today.isoformat()
    if re.search(r'\blast\s+quarter\b', msg):
        sm = ((today.month - 1) // 3) * 3 + 1
        this_q_start = today.replace(month=sm, day=1)
        last_q_end = this_q_start - timedelta(days=1)
        lsm = ((last_q_end.month - 1) // 3) * 3 + 1
        return last_q_end.replace(month=lsm, day=1).isoformat(), last_q_end.isoformat()
    if re.search(r'\byear\s+to\s+date\b|\bytd\b', msg):
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if re.search(r'\bthis\s+year\b', msg):
        return today.replace(month=1, day=1).isoformat(), today.replace(month=12, day=31).isoformat()
    if re.search(r'\blast\s+year\b', msg):
        y = today.year - 1
        return f'{y}-01-01', f'{y}-12-31'

    m = re.search(r'\b(?:for|in|of)\s+(20\d{2})\b', msg)
    if m:
        y = int(m.group(1))
        return f'{y}-01-01', f'{y}-12-31'

    return None, None
