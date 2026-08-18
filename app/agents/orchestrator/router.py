"""Orchestrator Agent v1.0 — server-side cross-module routing.

Previously the Orchestrator page did all routing client-side (keyword
router + symphony fan-out in JS), and /orchestrator-chat returned 404.
This module gives every client (web page, voice, API) the same brain:

  1. "company pulse" / "system overview"  → sp_orchestrator('overview')
     — one SQL round-trip gathering headline KPIs from every module.
  2. Symphony workflows (daily briefing, weekly report, …) — fans out to
     the underlying agent endpoints IN-PROCESS (httpx ASGITransport, no
     network hop) and weaves the responses into one sectioned report.
  3. Everything else — keyword-routes to the single best agent endpoint
     and passes its response through, annotated with `routedTo`.

The keyword rules mirror orchestrator-mgmt.html's sendMessage() router:
most-specific first (notifications beat 'invoice'; analytics report
names beat module keywords; bare 'account' → Accounts; sales summaries
→ Orders).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.database import execute_sp
from app.agents.orchestrator.executive import (
    match_exec_question, format_exec_answer,
)

logger = logging.getLogger(__name__)


def intent_boundary_classify(message: str) -> str:
    """Intent label, or '' if the boundary module is unavailable.

    Wrapped so a failure here can never take down routing: an unavailable
    classifier must degrade to the previous behaviour, not to a 500.
    """
    try:
        from app.core import intent_boundary
        return str(intent_boundary.classify(message).get('intent') or '')
    except Exception as exc:                                # pragma: no cover
        logger.debug(f'intent classify unavailable: {exc}')
        return ''


router = APIRouter()

# Knowledge boundary kill switch (Phase 5). On by default; set to 0 to restore
# pure keyword routing if the classifier is ever found to claim traffic it
# should not. Kept as a flag because this changes which subsystem answers a
# whole class of user requests.
KNOWLEDGE_ROUTING = os.getenv("CRM_KNOWLEDGE_ROUTING", "1").strip().lower() in (
    "1", "true", "yes", "on")


# ============================================================================
# REQUEST MODEL
# ============================================================================

class OrchChatInput(BaseModel):
    message: Optional[str] = None


class OrchChatRequest(BaseModel):
    chatInput: Optional[OrchChatInput] = None
    sessionId: Optional[str] = None
    message: Optional[str] = None


# ============================================================================
# SYMPHONY WORKFLOWS — mirrors CHIP_DEFS in orchestrator-mgmt.html
# ============================================================================

def _today() -> str:
    return date.today().isoformat()


def _symphony_defs() -> Dict[str, dict]:
    t = _today()
    return {
        'daily': {
            'title': 'Daily Briefing',
            'calls': [
                ('/activity-chat', '📅', 'Activities',
                 f"Today's activities: summarise overdue tasks, upcoming meetings, and calls due today. Today is {t}."),
                ('/notifications-chat', '🔔', 'Alerts',
                 'Show unread notifications'),
                ('/opportunity-chat', '🎯', 'Pipeline',
                 'Show opportunities closing this month and any at-risk deals'),
            ],
        },
        'pipeline': {
            'title': 'Pipeline Health',
            'calls': [
                ('/opportunity-chat', '🎯', 'Opportunities',
                 'Pipeline health: list open opportunities by stage with expected close dates and amounts'),
                ('/lead-chat', '🌟', 'Leads', 'list leads:'),
            ],
        },
        'followup': {
            'title': 'Follow-ups Due',
            'calls': [
                ('/activity-chat', '📅', 'Overdue Activities',
                 'Show overdue activities'),
                ('/lead-chat', '🌟', 'Leads', 'list leads:'),
            ],
        },
        'revenue': {
            'title': 'Revenue Snapshot',
            'calls': [
                ('/order-chat', '📦', 'Orders', 'Sales summary this month'),
                ('/accounting-chat', '💳', 'Accounting', 'accounting summary'),
            ],
        },
        'alerts': {
            'title': 'System Alerts',
            'calls': [
                ('/notifications-chat', '🔔', 'Notifications',
                 'Show unread notifications from this week'),
            ],
        },
        'weekly': {
            'title': 'Weekly Report',
            'calls': [
                ('/activity-chat', '📅', 'Activities',
                 'Show activities created last week'),
                ('/opportunity-chat', '🎯', 'Pipeline',
                 'Show opportunities created or updated in the past 7 days'),
                ('/accounting-chat', '💳', 'Revenue', 'accounting summary'),
            ],
        },
        'team': {
            'title': 'Team Activity',
            'calls': [
                ('/activity-chat', '📅', 'Activities by Rep',
                 f'Team activity: show activities grouped by sales representative for this week. Today is {t}.'),
                ('/lead-chat', '🌟', 'Leads', 'list leads:'),
            ],
        },
        'newbiz': {
            'title': 'New Business',
            'calls': [
                ('/lead-chat', '🌟', 'Leads', 'list leads:'),
                ('/opportunity-chat', '🎯', 'New Opportunities',
                 f'Show opportunities created this month. Today is {t}.'),
            ],
        },
    }


# Symphony phrase detection — revenue narrowed to "snapshot" phrasings so
# "revenue summary for 2025" still reaches the Accounting agent's
# deterministic year/quarter parsing.
_SYMPHONY_PATTERNS = [
    ('daily',    r'daily\s*brief|morning\s*brief|daily\s*summary|start\s*of\s*day'),
    ('pipeline', r'pipeline\s*(health|status|check|review)|health.*pipeline'),
    ('followup', r'follow.?ups?\s*(due|pending|overdue)?$|overdue\s*follow.?ups?'),
    ('revenue',  r'revenue\s*snap(shot)?|financial\s*snap'),
    ('alerts',   r'system\s*alerts?$'),
    ('weekly',   r'weekly\s*(report|summary|brief|review)|week\s*in\s*review'),
    ('team',     r'team\s*activit|team\s*(report|summary|performance|breakdown)'),
    ('newbiz',   r'new\s*business|new\s*biz'),
]

_PULSE_RE = re.compile(
    r'company\s+pulse|system\s+overview|company\s+overview|crm\s+overview|\bpulse\b',
    re.IGNORECASE)


def _route_single(lower: str) -> str:
    """Keyword router — mirrors orchestrator-mgmt.html sendMessage()."""
    if re.search(r'notif|unread|\balerts?\b', lower):
        return '/notifications-chat'
    if re.search(r'cash\s*flow|lead\s+sources?|owner\s+breakdown|productivity'
                 r'|invoiced\s+revenue|ar\s+age?ing|analytic|forecast|dashboard', lower):
        return '/analytics-chat'
    if re.search(r'lead|prospect|conver', lower):
        return '/lead-chat'
    if re.search(r'opportunit|deal|pipeline|stage|win\s*rate', lower):
        return '/opportunity-chat'
    if re.search(r'order|fulfil|ship|sales\s+(summary|by)', lower):
        return '/order-chat'
    if re.search(r'invoic|payment|accounting|revenue|cash', lower):
        return '/accounting-chat'
    if re.search(r'contact|person|people', lower):
        return '/contact-chat'
    if re.search(r'account|company|compan', lower):
        return '/account-chat'
    if re.search(r'product|catalogue|inventor|stock', lower):
        return '/prod-chat'   # NB: the Products agent route is /prod-chat
    if re.search(r'email|message|outreach', lower):
        return '/email-chat'
    return '/activity-chat'


# ============================================================================
# IN-PROCESS AGENT CALLS (ASGI — no network hop)
# ============================================================================

async def _call_agent(path: str, message: str, session_id: str,
                      structured: dict | None = None) -> dict:
    from app.main import app as _app  # lazy import avoids a circular import
    transport = httpx.ASGITransport(app=_app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url='http://orchestrator.internal',
                                 timeout=300) as client:
        _ci = {'message': message}
        if structured is not None:
            # Stage 3: the module consumes this INSTEAD of parsing the prose.
            _ci['structuredIntent'] = structured
        resp = await client.post(path, json={
            'sessionId': session_id,
            'chatInput': _ci,
        })
        try:
            data = resp.json()
        except Exception:
            return {'output': resp.text[:2000], 'mode': 'raw'}
        if isinstance(data, list):
            data = data[0] if data else {}
        return data if isinstance(data, dict) else {'output': str(data)[:2000]}


# ============================================================================
# FORMATTERS
# ============================================================================

def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _format_pulse(result: dict) -> str:
    a  = result.get('activities', {})
    n  = result.get('notifications', {})
    o  = result.get('opportunities', {})
    l  = result.get('leads', {})
    od = result.get('orders', {})
    iv = result.get('invoices', {})
    pm = result.get('payments', {})
    ac = result.get('accounts', {})
    pr = result.get('products', {})

    out = [
        '### 💓 Company Pulse',
        f"**As of:** {str(result.get('metadata', {}).get('as_of', ''))[:16]}",
        '',
        '| Module | Headline Metrics |',
        '| --- | --- |',
        f"| 🎯 Opportunities | **{o.get('open_count', 0)}** open worth **{_fmt_money(o.get('open_value', 0))}** · {o.get('closing_this_month', 0)} closing this month |",
        f"| 📦 Orders | **{od.get('this_month_count', 0)}** this month · revenue **{_fmt_money(od.get('this_month_revenue', 0))}** · {od.get('pending', 0)} pending |",
        f"| 🧾 Invoices | **{_fmt_money(iv.get('outstanding_total', 0))}** outstanding · {iv.get('overdue_count', 0)} past due |",
        f"| 💳 Payments | **{_fmt_money(pm.get('this_month_total', 0))}** received this month |",
        f"| 🌟 Leads | **{l.get('total', 0)}** total · {l.get('new_this_week', 0)} new this week |",
        f"| 📅 Activities | **{a.get('overdue', 0)}** overdue · {a.get('due_today', 0)} due today · {a.get('total', 0)} total |",
        f"| 🔔 Notifications | **{n.get('unread', 0)}** unread |",
        f"| 🏢 Accounts | **{ac.get('active', 0)}** active |",
        f"| 🛒 Products | **{pr.get('active', 0)}** active · {pr.get('low_stock', 0)} low stock |",
        '',
        '*Powered by sp_orchestrator — all modules in one query.*',
    ]
    return '\n'.join(out)


def _weave_symphony(title: str, calls: List[tuple], results: List[Any]) -> str:
    out = [f'### ⚙️ {title}',
           f'*{len(calls)} agents queried by the Orchestrator*', '']
    for (path, icon, label, _msg), res in zip(calls, results):
        out.append('---')
        out.append(f'### {icon} {label}')
        if isinstance(res, Exception):
            out.append(f'_Agent unavailable: {str(res)[:120]}_')
        else:
            body = (res.get('output') or res.get('error')
                    or '_No data returned from this agent._')
            out.append(str(body))
        out.append('')
    return '\n'.join(out)


def _format_plan(result: Dict[str, Any], executed: bool) -> str:
    """Render a planner draft (preview) or an executed plan (reads run, writes
    queued for approval) as chat markdown. Sibling of _weave_symphony /
    _format_pulse. Handles three shapes: failed draft (ok False, no trace),
    draft preview (steps), and executed plan (trace + proposed_approvals)."""
    goal = result.get('goal') or ''

    # Failed draft / planner declined — surface the reasons.
    if not result.get('ok') and not result.get('trace'):
        errs = result.get('errors') or ['the planner could not draft a plan']
        lines = ['### 🧭 Plan — could not build', '']
        if goal:
            lines += [f'**Goal:** {goal}', '']
        lines += [f'- ⚠️ {e}' for e in errs]
        return '\n'.join(lines)

    kind_icon = {'read': '📖', 'write': '✍️'}

    # Draft preview — nothing has run.
    if not executed:
        lines = [f'### 🧭 Plan — {goal}']
        if result.get('summary'):
            lines += [f'_{result["summary"]}_', '']
        lines += ['| # | capability | kind | why |', '| --- | --- | --- | --- |']
        steps = result.get('steps') or []
        for i, s in enumerate(steps, 1):
            lines.append(
                f"| {i} | `{s.get('intent')}` | "
                f"{kind_icon.get(s.get('kind'), '')} {s.get('kind') or ''} | "
                f"{s.get('why') or ''} |")
        writes = sum(1 for s in steps if s.get('kind') == 'write')
        lines += ['', (
            '_Preview only — reply with the same goal plus **confirm** to '
            f'execute: reads run immediately, {writes} write step(s) queue for '
            'approval._' if writes else
            '_Preview only — reply with the same goal plus **confirm** to '
            'execute (read-only plan)._')]
        return '\n'.join(lines)

    # Executed plan — reads ran, writes were proposed to governance.
    lines = [f'### 🧭 Plan executed — {goal}']
    if result.get('summary'):
        lines += [f'_{result["summary"]}_', '']
    trace = sorted(result.get('trace') or [], key=lambda t: t.get('step', 0))
    reads = [t for t in trace if t.get('kind') == 'read']
    writes = [t for t in trace if t.get('kind') == 'write']
    if reads:
        lines.append('**📖 Reads — executed**')
        for t in reads:
            status = '✓' if t.get('ok') else '✕'
            body = (t.get('output') or t.get('error') or '').strip() or '(no output)'
            lines.append(f"- {status} `{t.get('intent')}` — {body[:200]}")
        lines.append('')
    if writes:
        lines.append('**✍️ Writes — queued for approval**')
        by_step = {p.get('step'): p for p in (result.get('proposed_approvals') or [])}
        for t in writes:
            p = by_step.get(t.get('step'), {})
            aid = t.get('queued_approval') or p.get('approval_uuid') or ''
            why = p.get('why') or ''
            lines.append(f"- `{t.get('intent')}` — approval "
                         f"`{str(aid)[:8]}`{(' — ' + why) if why else ''}")
        lines.append('')
    if result.get('note'):
        lines.append(f"_{result['note']}_")
    return '\n'.join(lines)


# ============================================================================
# ROUTE
# ============================================================================

@router.get('/orchestrator-health')
async def orchestrator_health():
    return {'status': 'healthy', 'module': 'orchestrator', 'version': '1.0.0'}


@router.post('/orchestrator-chat')
async def orchestrator_chat(req: OrchChatRequest, request: Request):
    session_id = (req.sessionId or 'orch-session').strip()
    message = ((req.chatInput.message if req.chatInput else None)
               or req.message or '').strip()
    lower = message.lower()
    logger.info(f'=== Orchestrator Chat === {message[:100]!r}')

    if not message:
        return JSONResponse({'sessionId': session_id, 'success': False,
                             'output': 'Please provide a message.', 'mode': 'error'})

    # ── 0a. Live web lookup — deterministic "search the web…" route ──────────
    # Handled by the Orchestrator itself (app/core/web_tools.py) so web
    # questions never depend on keyword routing, which could land them on
    # agents without web_search (e.g. notifications-chat via 'alerts').
    _web_m = re.match(
        r'^(?:please\s+)?(?:can\s+you\s+)?(?:search|browse|look\s*up|google)\s+'
        r'(?:on\s+|in\s+)?the\s+(?:web|internet)\s*(?:for|about|:)?\s*(.*)$',
        message, re.IGNORECASE)
    if _web_m:
        from app.core.web_tools import web_answer
        _q = (_web_m.group(1) or '').strip().rstrip('?.!') or message
        logger.info(f'→ web_search (deterministic): {_q[:80]!r}')
        text = await asyncio.to_thread(web_answer, _q)
        return JSONResponse({'sessionId': session_id, 'success': True,
                             'mode': 'web_search', 'output': text,
                             'rawParams': {'mode': 'web_search', 'query': _q}})

    # ── 0b. Premise firewall (Phase 4) ───────────────────────────────────────
    # _route_single() below is pure keyword matching with no notion of whether
    # a message is a QUESTION or an INSTRUCTION. "Why did the nightly duplicate
    # cleanup merge my contacts?" contains 'contact', so it routed to
    # /contact-chat, which dutifully ran the duplicates report — and the model,
    # handed a report and a question presupposing a nightly job, answered
    # "the account clean-up job runs nightly at 02:30 AM server local time".
    # No such job exists. That was a measured P0.
    #
    # The KB fixes could not reach this: nothing on this path calls rag_block,
    # so the customer channel's scope caution never applied here.
    #
    # This intercepts ONLY messages that assert something the implementation
    # contradicts — an automation, a schedule, a past event, or a capability on
    # an object that does not have it. Everything else, including every
    # legitimate lookup, report, form and action, falls through untouched. That
    # asymmetry is deliberate: this path's job is executing work, and a
    # firewall that swallowed ambiguous traffic would break the thing it is
    # meant to protect.
    #
    # Deliberately deterministic — see premise_firewall's module docstring:
    # asking a model to catch an invented schedule reintroduces the faculty
    # that invented it.
    from app.core import premise_firewall
    _pf = premise_firewall.check(message)
    if _pf:
        logger.info(f"→ premise firewall [{_pf['rule']}]: {message[:80]!r}")
        return JSONResponse({
            'sessionId': session_id, 'success': True,
            'mode': f"premise_correction:{_pf['rule']}",
            'output': premise_firewall.as_answer(_pf),
            'rawParams': {'rule': _pf['rule'], 'capability': _pf['capability'],
                          'objects': _pf['objects'], 'blocked_action': _pf['is_action']},
        })

    # ── 0c. Knowledge boundary (Phase 5 — H2) ────────────────────────────────
    # Informational questions are answered from approved knowledge instead of
    # being keyword-routed to whichever module owns a word in them. "Can I
    # merge duplicate contacts?" previously landed on the executive agent and
    # was answered from model knowledge; now it is grounded in the KB.
    #
    # ONLY interrogatives are claimed. Actions, lookups, reports and anything
    # unclassified continue down the existing path untouched — misrouting an
    # ACTION into prose is a functional regression on a path whose job is
    # execution, so the classifier requires positive evidence of a question.
    #
    # A retrieval miss REFUSES and logs a gap. It must never fall back to the
    # module agent: that fallthrough is exactly how Phase 3 produced "the
    # account clean-up job runs nightly at 02:30 AM" — the KB found nothing,
    # the executive agent answered anyway, and a gap became a fabrication.
    if KNOWLEDGE_ROUTING:
        from app.core import intent_boundary
        _cls = intent_boundary.classify(message)
        if _cls['intent'] == intent_boundary.KNOWLEDGE:
            from app.core import knowledge_route
            _ka = knowledge_route.answer(message)
            if _ka:
                logger.info(f"→ knowledge boundary [{_ka['mode']}]: {message[:70]!r}")
                return JSONResponse({
                    'sessionId': session_id, 'success': True,
                    'mode': _ka['mode'], 'output': _ka['output'],
                    'rawParams': {'grounded': _ka['grounded'],
                                  'source': _ka['source'],
                                  'articles': _ka['articles'],
                                  'gapLogged': _ka['gap_logged']},
                })
        elif _cls['intent'] == intent_boundary.MIXED:
            # Answer the knowledge half, then let the operational half run.
            # The knowledge answer never executes anything itself.
            from app.core import knowledge_route
            _ka = knowledge_route.answer(message)
            if _ka:
                _mixed_prefix = _ka['output']
                logger.info(f"→ mixed intent: knowledge first, then routing")
                request.state.mixed_prefix = _mixed_prefix   # noqa: attribute

    # ── 0. Capability routing (Phase 2 — A2A) ────────────────────────────────
    # Route by *capability* via the A2A registry instead of keyword matching.
    # Additive: only these explicit handles trigger it.
    #   "capabilities"                    → list what can be routed
    #   "route: <intent> [k=v k2=v2 ...]" → dispatch to the owning agent
    # Write capabilities dry-run by default unless the message says "confirm".
    if lower in ('capabilities', 'list capabilities', 'what can you route'):
        from app.core.a2a import manifest
        m = manifest()
        lines = [f"### 🧭 A2A Capabilities ({m['count']})",
                 '| intent | agent | kind | structured |',
                 '| --- | --- | --- | --- |']
        lines += [f"| `{c['intent']}` | {c['agent']} | {c['kind']} | "
                  f"{'✓' if c['structured'] else ''} |" for c in m['capabilities']]
        return JSONResponse({'sessionId': session_id, 'success': True,
                             'mode': 'a2a_manifest', 'output': '\n'.join(lines)})

    _cap_m = re.match(r'^(?:route|intent)\s*[:=]\s*([a-z0-9_.]+)\s*(.*)$',
                      message.strip(), re.IGNORECASE)
    if _cap_m:
        from app.core.a2a import dispatch, resolve, A2ARequest
        intent = _cap_m.group(1)
        params = {}
        for k, v in re.findall(r'(\w+)=(\S+)', _cap_m.group(2) or ''):
            params[k] = int(v) if v.lstrip('-').isdigit() else v
        cap = resolve(intent)
        dry = cap is not None and cap.kind == 'write' and 'confirm' not in lower
        res = await dispatch(A2ARequest(from_agent='orchestrator', intent=intent,
                                        params=params), dry_run=dry)
        if not res.ok:
            return JSONResponse({'sessionId': session_id, 'success': False, 'mode': 'a2a',
                                 'output': f"❌ A2A `{intent}` → {res.agent}: {res.error}"})
        body = res.output
        if res.data is not None:
            import json as _json
            body += '\n\n```json\n' + _json.dumps(res.data, default=str)[:1500] + '\n```'
        tag = ' (dry-run — add "confirm" to execute)' if dry else ''
        return JSONResponse({'sessionId': session_id, 'success': True, 'mode': 'a2a',
                             'output': f"### 🔗 A2A → {intent} (via {res.agent}){tag}\n{body}"})

    # ── 0c. Bounded planner — "plan: <goal>" decomposes a novel multi-step ───
    # goal into a coordinated multi-agent plan (app/core/planner.py). Preview by
    # default (draft only); add a trailing "confirm" to execute — reads run
    # immediately, writes queue for governance approval (nothing outbound). This
    # is the conductor on the conversational path: the same registered-capability
    # rails the /planner/plan endpoint uses, opt-in by the "plan:" token so it
    # can never shadow ordinary single-agent routing.
    _plan_m = re.match(r'^(?:plan|goal)\s*[:=]\s*(.*)$', message.strip(),
                       re.IGNORECASE | re.DOTALL)
    if _plan_m:
        from app.core import planner
        goal = _plan_m.group(1).strip()
        confirm = False
        _cm = re.search(r'\s+confirm\s*$', goal, re.IGNORECASE)
        if _cm:
            confirm, goal = True, goal[:_cm.start()].strip()
        logger.info(f'[planner] goal={goal[:80]!r} confirm={confirm}')

        # G3: executing (confirm) queues governance proposals + spends LLM, so
        # gate it behind write-auth and a per-IP rate limit. The draft PREVIEW
        # stays open to anyone; an unauthorized "confirm" degrades to a preview
        # (not a 403) so the plan is still shown, just not queued.
        if confirm:
            from app.core.auth_dep import caller_can_write
            if not await caller_can_write(request):
                draft = await asyncio.to_thread(planner.draft_plan, goal)
                out = _format_plan(draft, executed=False)
                if draft.get('ok'):
                    out += ('\n\n_Sign in (or send an admin token) to execute — '
                            'execution queues actions for approval._')
                return JSONResponse({'sessionId': session_id,
                                     'success': bool(draft.get('ok')),
                                     'mode': 'plan', 'workflow': 'plan', 'output': out})
            from app.core.rate_limit import plan_exec_ip, client_ip
            if plan_exec_ip.record(client_ip(request)) > plan_exec_ip.max_events:
                return JSONResponse({'sessionId': session_id, 'success': False,
                                     'mode': 'plan', 'workflow': 'plan',
                                     'output': '### 🧭 Plan — rate limited\n\nToo many '
                                     'plan executions this hour. Please try again later.'})
        try:
            result = (await planner.run_plan(goal) if confirm
                      else await asyncio.to_thread(planner.draft_plan, goal))
        except Exception as e:
            logger.error(f'planner failed: {e}', exc_info=True)
            return JSONResponse({'sessionId': session_id, 'success': False,
                                 'mode': 'error', 'output': f'Planner failed: {e}'})
        return JSONResponse({
            'sessionId': session_id, 'success': bool(result.get('ok')),
            'mode': 'plan', 'workflow': 'plan',
            'output': _format_plan(result, executed=confirm),
        })

    # ── 0d. Scenario simulation — "simulate: …" / "what if …" runs a READ-ONLY
    # what-if over the objectives math (app/core/simulator.py). Nothing is
    # written, proposed or sent — safe at any auth level, so no gate needed.
    _sim_m = re.match(r'^(?:simulate|what\s+if)\s*[:=]?\s*(.+)$',
                      message.strip(), re.IGNORECASE | re.DOTALL)
    if _sim_m:
        try:
            from app.core import simulator
            result = await asyncio.to_thread(simulator.simulate,
                                             _sim_m.group(1).strip())
            return JSONResponse({'sessionId': session_id,
                                 'success': bool(result.get('ok')),
                                 'mode': 'simulate', 'workflow': 'simulate',
                                 'output': simulator.render_markdown(result)})
        except Exception as e:
            logger.error(f'simulator failed: {e}', exc_info=True)
            return JSONResponse({'sessionId': session_id, 'success': False,
                                 'mode': 'error', 'output': f'Simulation failed: {e}'})

    # ── 1. Company pulse — sp_orchestrator overview ──────────────────────────
    if _PULSE_RE.search(lower):
        try:
            rows = execute_sp("SELECT sp_orchestrator('overview') AS result")
            result = rows[0].get('result') if rows else {}
            return JSONResponse({
                'sessionId': session_id, 'success': True,
                'mode': 'pulse', 'output': _format_pulse(result or {}),
            })
        except Exception as e:
            logger.error(f'pulse failed: {e}', exc_info=True)
            return JSONResponse({'sessionId': session_id, 'success': False,
                                 'mode': 'error', 'output': f'Pulse failed: {e}'})

    # ── 1b. Executive question bank (CEO / CFO / VP Finance / VP Sales) ──────
    # Matches before symphonies and single-agent routing so phrases like
    # "weighted forecast vs commit" get the executive pack, not keyword
    # routing. Out-of-CRM topics return an honest scope note + best proxy.
    # Guarded by intent: the executive bank answers QUESTIONS, and its 138
    # patterns are unanchored keyword matches. One of them is a bare
    # `duplicates?`, so "Merge these duplicate contacts." matched, was
    # answered with a lead-funnel report, and never reached the contact merge
    # route at all (I2). The word appears mid-sentence in an imperative; the
    # matcher has no notion of mood, and 137 other patterns can misfire the
    # same way.
    #
    # Fixing the one pattern would leave the class of defect in place, so the
    # guard is on intent instead: an imperative is a request to DO something
    # and is never an executive question, whatever words it contains.
    # Skips ACTION and LOOKUP. A lookup that names a record type ("show me
    # duplicate contacts") belongs to that module, not to the executive pack —
    # the same bare `duplicates?` pattern was answering it with a
    # company-wide lead-funnel report. REPORT is deliberately NOT skipped:
    # "show me the executive dashboard" is exactly what the pack is for.
    _exec_intent = intent_boundary_classify(message)
    exec_match = (None if _exec_intent in ('action', 'lookup')
                  else match_exec_question(message))
    if exec_match:
        sections, note = exec_match
        try:
            rows = execute_sp("SELECT sp_orchestrator('executive') AS result")
            pack = (rows[0].get('result') or {}) if rows else {}
            # Enrich with forecast calibration on demand (kept out of the big
            # sp_orchestrator pack — sourced from fn_forecast_accuracy instead).
            if 'forecast_calibration' in sections:
                try:
                    cr = execute_sp("SELECT fn_forecast_accuracy(12) AS result")
                    cal = (cr[0].get('result') or {}) if cr else {}
                    pack['forecast_calibration'] = ((cal.get('data') or {}).get('periods')) or []
                except Exception as ce:
                    logger.error(f'forecast calibration enrich failed: {ce}')
                    pack['forecast_calibration'] = []
            return JSONResponse({
                'sessionId': session_id, 'success': True,
                'mode': 'executive', 'sections': sections,
                'output': format_exec_answer(pack, sections, note),
            })
        except Exception as e:
            logger.error(f'executive pack failed: {e}', exc_info=True)
            return JSONResponse({'sessionId': session_id, 'success': False,
                                 'mode': 'error',
                                 'output': f'Executive pack failed: {e}'})

    # ── 2. Symphony workflows — multi-agent fan-out ──────────────────────────
    for key, pat in _SYMPHONY_PATTERNS:
        if re.search(pat, lower, re.IGNORECASE):
            defs = _symphony_defs()[key]
            calls = defs['calls']
            logger.info(f'[symphony] {key} → {len(calls)} agents')
            results = await asyncio.gather(
                *[_call_agent(p, m, session_id) for (p, _i, _l, m) in calls],
                return_exceptions=True)
            return JSONResponse({
                'sessionId': session_id, 'success': True,
                'mode': 'symphony', 'workflow': key,
                'output': _weave_symphony(defs['title'], calls, results),
            })

    # ── 3. Single-agent delegation — capability-routed via the A2A layer ──────
    # Orchestrator v2: the LLM intent router selects the agent (keyword
    # `_route_single` remains its fallback); the call then goes through the
    # typed A2A dispatch (correlation + capability registry), falling back to
    # a direct _call_agent if no passthrough capability is registered.
    from app.core.intent_router import aroute
    decision = await aroute(message)

    # Step 4: automatic plan routing — when the intent router labels the message
    # a confident MULTI-STEP GOAL (only when INTENT_PLAN_ROUTING is on), decompose
    # it with the bounded planner and return a PREVIEW. Never auto-executed: the
    # user runs it via the explicit "plan: … confirm" handle (section 0c above).
    if getattr(decision, 'kind', 'single_agent') == 'plan':
        from app.core import planner
        logger.info(f'[route] → planner preview (auto) for {message[:60]!r}')
        result = await asyncio.to_thread(planner.draft_plan, message)
        out = _format_plan(result, executed=False)
        if result.get('ok'):
            out += ('\n\n_Auto-detected a multi-step goal. To run it, reply:_ '
                    f'`plan: {message} confirm`')
        return JSONResponse({'sessionId': session_id,
                             'success': bool(result.get('ok')),
                             'mode': 'plan', 'workflow': 'plan-auto',
                             'output': out, 'routedBy': decision.label})

    path = decision.endpoint
    from app.core.a2a import dispatch, resolve, query_intent_for_endpoint, A2ARequest
    q_intent = query_intent_for_endpoint(path)
    logger.info(f'[route] → {path} via {decision.label} (a2a intent={q_intent})')
    # ── Stage 3 cutover (Phase 9) ────────────────────────────────────────────
    # Placed ABOVE the a2a/else split because BOTH branches delegate. The first
    # attempt sat in the else branch and never fired: contacts requests travel
    # via `a2a:contacts.query`, so a merge was being dispatched through a
    # capability named *query* — which is the substitution problem restated as
    # a routing topology.
    #
    # A resolved write therefore bypasses the query capability deliberately and
    # goes straight to the module with its operation attached.
    _cut = None
    try:
        from app.core import structured_cutover
        _cut = structured_cutover.resolve_for_route(message, path)
    except Exception as _exc:                               # pragma: no cover
        logger.warning(f'cutover skipped: {_exc}')
    if _cut and _cut['kind'] in ('ask', 'refuse'):
        logger.info(f"[cutover] {_cut['kind']} -> returning without executing")
        return JSONResponse({
            'sessionId': session_id, 'success': True,
            'mode': f"structured_{_cut['kind']}", 'routedTo': path,
            'output': _cut['output'],
            'rawParams': {'object': _cut.get('object'),
                          'operation': _cut.get('operation'), 'executed': False}})
    _structured = _cut['params'] if _cut and _cut['kind'] == 'intent' else None

    try:
        if _structured:
            data = dict(await _call_agent(path, message, session_id, _structured))
            data['routedVia'] = 'structured_intent'
            data['structuredIntent'] = _structured
        elif q_intent and resolve(q_intent):
            res = await dispatch(A2ARequest(
                intent=q_intent, from_agent='orchestrator',
                params={'message': message}, prose=True, correlation_id=session_id))
            data = dict(res.raw or {'success': res.ok, 'output': res.output,
                                    'error': res.error})
            data['routedVia'] = f'a2a:{q_intent}'
        else:
            data = dict(await _call_agent(path, message, session_id))
    except Exception as e:
        logger.error(f'delegation to {path} failed: {e}', exc_info=True)
        return JSONResponse({'sessionId': session_id, 'success': False,
                             'mode': 'error', 'routedTo': path,
                             'output': f'Agent call failed: {e}'})
    data.setdefault('sessionId', session_id)
    data['routedTo'] = path
    data['routedBy'] = decision.label

    # ── Stage 1: structured-intent SHADOW (Phase 9) ─────────────────────────
    # Resolves the operation and records whether the module agreed. Changes
    # nothing — the module's answer is returned exactly as before. Shipping the
    # resolver dark is what makes the next stage a migration rather than a
    # rewrite: the agreement rate is measured on real traffic first, and a
    # resolver that disagrees on cases nobody anticipated shows up here instead
    # of in production.
    try:
        from app.core import operation_resolver
        _obj_hint = {'/contact-chat': 'contact', '/account-chat': 'account',
                     '/lead-chat': 'lead', '/opportunity-chat': 'opportunity',
                     '/order-chat': 'order', '/accounting-chat': 'invoice',
                     '/prod-chat': 'product'}.get(path)
        _sh = operation_resolver.shadow(message, str(data.get('mode') or ''),
                                        object_hint=_obj_hint)
        data['intentShadow'] = _sh['outcome']
    except Exception as _exc:                               # pragma: no cover
        logger.debug(f'intent shadow skipped: {_exc}')

    # ── H3: never return a silent response ──────────────────────────────────
    # "Show me the executive dashboard." came back with an EMPTY output and no
    # success flag, which read as a broken formatter. It was not: analytics is
    # role-gated, `require_analytics_access` raised a 403, and FastAPI's error
    # body carries the reason under `detail` — a key nothing downstream maps to
    # `output`. So a correct, deliberate authorization refusal was delivered to
    # the user as silence.
    #
    # Surfacing it is the fix, not bypassing the gate: the user should be told
    # they lack access, not left staring at nothing, and certainly not handed a
    # KB article as a substitute for a permission they do not have.
    _mp = getattr(request.state, 'mixed_prefix', None)
    if _mp and str(data.get('output') or '').strip():
        # Mixed intent: knowledge answer first, then the operational result.
        data['output'] = _mp + "\n\n---\n\n" + str(data['output'])
        data['mode'] = f"mixed:{data.get('mode', 'routed')}"

    if not str(data.get('output') or '').strip():
        reason = (data.get('detail') or data.get('error')
                  or data.get('message') or '')
        if reason:
            data['output'] = str(reason)
            data.setdefault('success', False)
            data.setdefault('mode', 'refused')
        else:
            # Genuinely empty with no reason given — still not silence.
            data['output'] = ("The request reached the right module but came "
                              "back with no content. Nothing was changed. "
                              "Please rephrase, or report this if it repeats.")
            data.setdefault('success', False)
            data.setdefault('mode', 'empty_response')
    return JSONResponse(data)
