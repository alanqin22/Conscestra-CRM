"""Phase 2 — Agent-to-Agent (A2A) protocol.

Upgrades cross-agent calls from ad-hoc plain-English `_call_agent(path, string)`
to a typed, discoverable, capability-routed layer:

  • A typed ENVELOPE (A2ARequest / A2AResult) carrying intent, entity ref,
    params, correlation_id, and confidence — so a call is a structured contract,
    not a screen-scrape.
  • A capability REGISTRY (intent → which agent owns it, built on each agent's
    modes) — callers route by *capability*, not a hardcoded endpoint or the
    orchestrator's keyword `_route_single`.
  • dispatch() — resolves the capability, invokes the owning agent IN-PROCESS
    (httpx ASGI, no network hop), and returns a structured A2AResult with
    correlation lineage. Read vs write is declared per capability.

This is ADDITIVE and safe: each agent's input contract is unchanged (it still
receives `{chatInput:{message}}`), so the agents' existing deterministic/NL
routing is reused — A2A just wraps the call in a typed, governable envelope.
The messages used here are the same ones the orchestrator already calls agents
with (e.g. 'accounting summary', 'list leads:'), so routing is deterministic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("a2a")


# ============================================================================
# ENVELOPE
# ============================================================================

@dataclass
class EntityRef:
    type: str
    id: str


@dataclass
class A2ARequest:
    """A typed agent-to-agent request."""
    intent: str
    from_agent: str = "system"
    entity: Optional[EntityRef] = None
    params: Dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    confidence: float = 1.0          # caller's confidence in the request (0–1)
    requires_ack: bool = False
    prose: bool = False              # True = force the NL/agent path (formatted
                                     # output); default uses the structured SP
                                     # path when the capability declares one.
    govern_bypass: bool = False      # True = skip Phase 5 confidence-gating
                                     # (set when an approved action re-dispatches)


# ── Outcome of a dispatch ───────────────────────────────────────────────────
# Four states, because a boolean cannot say "we could not tell". The predicate
# this replaces was `ok = (success is not False) and not error`, which never
# looked at the HTTP status and therefore read 403/404/429/500, empty bodies and
# non-JSON alike as success.
ACCEPTED = "accepted"   # the request was processed and not refused
REJECTED = "rejected"   # it was refused — explicitly, or by authorization
FAILED   = "failed"     # transport or server error; it did not complete
UNKNOWN  = "unknown"    # completed with no readable signal — NOT a success


def classify_outcome(status: Optional[int], body: Optional[dict],
                     parsed: bool = True) -> str:
    """Map an HTTP exchange to one of the four outcomes.

    THE INVARIANT: there is no path here in which the ABSENCE of an error
    produces ACCEPTED. Acceptance requires a 2xx — a positive statement from the
    server — plus a body that is readable and does not itself report failure.

    401/403 are REJECTED by owner decision (2026-08-13). Note for whoever adds
    retries: REJECTED therefore mixes non-retryable causes (bad address,
    suppression) with retryable ones (a credential that can be corrected), so
    retry logic must key off something other than this enum alone.
    """
    if status is None:
        return UNKNOWN                      # transport failure / no response
    body = body or {}
    if status in (401, 403):
        return REJECTED                     # authorization refused it
    if 200 <= status < 300:
        if not parsed:
            return UNKNOWN                  # a 200 we could not read
        if not body:
            return UNKNOWN                  # 200 with an empty body says nothing
        if body.get("success") is False or body.get("error"):
            return REJECTED                 # the agent reported its own refusal
        return ACCEPTED
    return FAILED                           # every other 4xx/5xx


def classify_sp_result(data: Any, raised: bool = False) -> str:
    """The same doctrine, applied to the STRUCTURED (sp=) path.

    The HTTP path was fixed first because that is where the 25 false "sent"
    records came from. The structured path had the identical defect wearing
    different clothes: it returned `A2AResult(True, ...)` whenever `cap.sp` did
    not RAISE, so an SP that reported its own refusal by RETURN VALUE was read
    as success. Measured on 2026-08-14, six SPs already did exactly that —
    `sms.send` answering {'ok': False, 'error': "unusable phone number"} was
    dispatched as ACCEPTED.

    This matters beyond tidiness: moving email.send_payment_reminder onto the
    sp= path (the fix for the 403) would have carried it off the repaired path
    and back onto this one, restoring the original defect on the exact
    capability it was found in.

    Two refusal conventions are in use and both are honoured — `success` (the
    email module) and `ok` (sms, contact.update_profile, scoring.activate, the
    data.* family). Only the SINGULAR `error` key counts; `crm.plan` returns a
    plural `errors` list that is empty on success.

    Anything else that completed without raising is ACCEPTED, so the read
    capabilities returning bare lists and row dicts are unaffected.
    """
    if raised:
        return FAILED                       # did not complete
    if data is None:
        return UNKNOWN                      # completed, told us nothing
    if isinstance(data, dict):
        if data.get("success") is False or data.get("ok") is False:
            return REJECTED                 # the SP reported its own refusal
        if data.get("error"):
            return REJECTED
    return ACCEPTED


@dataclass
class A2AResult:
    ok: bool
    intent: str
    agent: str
    correlation_id: str
    data: Any = None
    output: str = ""
    error: Optional[str] = None
    hops: List[str] = field(default_factory=list)   # delegated sub-calls (audit)
    raw: Optional[Dict[str, Any]] = None            # full agent response (NL path)
    # Which of the four states this was. Defaults from `ok` so the many
    # positional A2AResult(True/False, ...) constructions keep working; the HTTP
    # path sets it explicitly. Callers that must not over-claim (recording a
    # send, say) should read THIS, not `ok`.
    outcome: str = ""
    status: Optional[int] = None                    # HTTP status, NL path only

    def __post_init__(self):
        if not self.outcome:
            self.outcome = ACCEPTED if self.ok else FAILED


# ============================================================================
# CAPABILITY REGISTRY  (intent → owning agent)
# ============================================================================

@dataclass
class Capability:
    intent: str
    agent: str
    endpoint: str
    kind: str                             # 'read' | 'write'
    render: Callable[[Dict[str, Any]], str]   # params → the agent's NL message
    description: str
    # Optional STRUCTURED input contract: params → structured data, via the
    # owning agent's SQL builder + SP (no NL parsing, no AI, no HTTP). When set,
    # dispatch() prefers this for deterministic agent-to-agent data exchange.
    sp: Optional[Callable[[Dict[str, Any]], Any]] = None
    # Optional COMPOSITE handler (peer handoff): an async fn(req) that delegates
    # sub-intents to peer agents and returns (data, hops). Set for capabilities
    # whose value comes from orchestrating several agents.
    compose: Optional[Callable[["A2ARequest"], Any]] = None


def _reg(*caps: Capability) -> Dict[str, Capability]:
    return {c.intent: c for c in caps}


# ---- structured (direct-SP) capability handlers ----------------------------
def _sp_exec(build: Callable, params: Dict[str, Any]) -> Any:
    """Build an SP call via an agent's sql_builder and return its structured
    result (unwrapping the {'result': ...} row when present)."""
    from app.core.database import execute_sp
    sql, _ = build(params)
    rows = execute_sp(sql)
    if rows and isinstance(rows[0], dict) and "result" in rows[0]:
        return rows[0]["result"]
    return rows


def _sp_accounting_summary(p: Dict[str, Any]) -> Any:
    from app.agents.accounting.sql_builder import build_accounting_query
    return _sp_exec(build_accounting_query, {"mode": "accounting_summary"})


def _sp_leads_list(p: Dict[str, Any]) -> Any:
    from app.agents.leads.sql_builder import build_leads_query
    q: Dict[str, Any] = {"mode": "list", "pageSize": p.get("pageSize", 20)}
    for k in ("scoreMin", "scoreMax", "status", "rating"):
        if p.get(k) is not None:
            q[k] = p[k]
    return _sp_exec(build_leads_query, q)


def _sp_leads_enrich(p: Dict[str, Any]) -> Any:
    """OUTWARD function call: enrich a lead with external firmographics. Accepts a
    lead_id (loads its company/email) or company/email/domain directly. See
    app/core/enrichment.py — stub by default, pluggable to a real provider."""
    from app.core import enrichment
    lead_id = p.get("lead_id") or p.get("leadId")
    company, email, domain = p.get("company"), p.get("email"), p.get("domain")
    if lead_id and not (company or email or domain):
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT company, email FROM leads WHERE lead_id = %s", (lead_id,))
                row = cur.fetchone()
                if row:
                    company, email = row[0], row[1]
        finally:
            conn.close()
    data = enrichment.enrich_company(company=company, email=email, domain=domain)
    # Gap-fill the lead row when called with apply=true (and a lead_id).
    applied = 0
    if lead_id and p.get("apply"):
        try:
            applied = enrichment.apply_to_lead(lead_id, data)
        except Exception:
            pass
    return {"lead_id": lead_id, "company": company, "enrichment": data,
            "applied": bool(applied)}


def _sp_orders_sales_summary(p: Dict[str, Any]) -> Any:
    from app.agents.orders.sql_builder import build_orders_query
    return _sp_exec(build_orders_query, {"mode": "sales_summary"})


def _sp_activities_list(p: Dict[str, Any]) -> Any:
    from app.agents.activities.sql_builder import build_activities_query
    return _sp_exec(build_activities_query, {"mode": "list", "pageSize": p.get("pageSize", 20)})


def _sp_contacts_list(p: Dict[str, Any]) -> Any:
    from app.agents.contacts.sql_builder import build_contacts_query
    return _sp_exec(build_contacts_query, {"mode": "list", "pageSize": p.get("pageSize", 20)})


def _sp_opportunities_pipeline(p: Dict[str, Any]) -> Any:
    from app.agents.opportunities.sql_builder import build_opportunities_query
    return _sp_exec(build_opportunities_query, {"mode": "pipeline"})


def _sp_products_low_stock(p: Dict[str, Any]) -> Any:
    from app.agents.products.sql_builder import build_products_query
    return _sp_exec(build_products_query, {"mode": "low_stock"})


def _sp_account_context(p: Dict[str, Any]) -> Any:
    """Phase 4: shared-blackboard context (all agents' notes) for an account."""
    from app.core import blackboard
    aid = p.get("account_id") or p.get("entity_id")
    if not aid:
        return {"error": "account_id required"}
    return blackboard.context("account", aid)


def _sp_email_send_payment_reminder(p: Dict[str, Any]) -> Any:
    """Email: send an overdue-invoice payment reminder (structured, no HTTP).

    Registered so dispatch() stops routing this over the in-process ASGI hop to
    the admin-gated /email-chat, which answered 403 in every environment and
    meant no payment reminder was ever transmitted.
    """
    from app.agents.email.structured import send_payment_reminder_sp
    return send_payment_reminder_sp(p or {})


def _sp_campaign_winback(p: Dict[str, Any]) -> Any:
    """Marketing: create + launch a win-back campaign (executed on approval)."""
    from app.core import marketing
    return marketing.winback_campaign_sp(p or {})


def _sp_supervisor_emit_dunning(p: Dict[str, Any]) -> Any:
    """Supervisor auto-action (executed on approval): kick the dunning loop."""
    from app.core.database import execute_sp
    rows = execute_sp("SELECT fn_emit_overdue_invoice_events(%(c)s) AS r",
                      {"c": int((p or {}).get("cap", 25))})
    return {"emitted_invoice_overdue_events": rows[0].get("r") if rows else 0}


def _sp_supervisor_emit_hot_leads(p: Dict[str, Any]) -> Any:
    """Supervisor auto-action (executed on approval): kick the hot-lead loop."""
    from app.core.database import execute_sp
    rows = execute_sp("SELECT fn_emit_hot_lead_events(%(c)s) AS r",
                      {"c": int((p or {}).get("cap", 25))})
    return {"emitted_lead_scored_events": rows[0].get("r") if rows else 0}


def _sp_objectives_report(p: Dict[str, Any]) -> Any:
    """Goal-oriented supervisor (Phase 8): objectives vs targets, live."""
    from app.core import objectives
    return {"objectives": objectives.report()}


def _sp_tuning_adjust(p: Dict[str, Any]) -> Any:
    """Learning loop: write a governed model parameter (executed on approval).
    tuning.apply() enforces the hard bounds + band ordering."""
    from app.core import tuning
    return tuning.apply(str(p.get("param") or ""), float(p.get("value")),
                        updated_by="governance",
                        reason=p.get("why") or "approved tuning.adjust")


def _sp_kb_publish(p: Dict[str, Any]) -> Any:
    """Knowledge loop: publish an approved article (executed on approval).
    knowledge.publish() validates fields + source_ref idempotency."""
    from app.core import knowledge
    return knowledge.publish(p or {}, created_by="governance")


def _sp_meeting_book(p: Dict[str, Any]) -> Any:
    """Booking: check the owner's real availability and book the meeting
    (invite via signed .ics link; email only under AUTOSEND + verified)."""
    from app.core import booking
    return booking.book_sp(p or {})


def _sp_crm_context(p: Dict[str, Any]) -> Any:
    """Context hydration: the compact 360 pack any agent starts work with."""
    from app.core import context as crm_context
    et = str(p.get("entity_type") or "account")
    eid = str(p.get("entity_id") or p.get("account_id") or p.get("lead_id") or "")
    pack = crm_context.hydrate(et, eid)
    return {"pack": pack, "rendered": crm_context.render(pack)}


def _sp_sms_send(p: Dict[str, Any]) -> Any:
    """Telephony: send one SMS (governed write; SMS_AUTOSEND=0 drafts as a
    task; irreversible once sent — no undo, same as email)."""
    from app.core import telephony
    return telephony.send_sms_sp(p or {})


def _sp_quote_generate(p: Dict[str, Any]) -> Any:
    """Email agent fill-in: build a priced quotation from LIVE retail
    pricing (deterministic math — the LLM only writes the greeting) and
    deliver it under the AUTOSEND + verified-address gates (else drafted
    as an owner task). CASL-flagged commercial send."""
    from app.core import quotes
    return quotes.generate_quote_sp(p or {})


def _sp_web_consult(p: Dict[str, Any]) -> Any:
    """Internet Agent: answer from the LIVE web with cited sources (ddgs →
    Tavily → trafilatura → LLM synthesis). Read-only and free-tier; any
    agent can delegate to it when its answer needs fresh outside facts."""
    from app.core.web_tools import web_answer
    q = str(p.get("query") or p.get("message") or "").strip()
    if not q and not p.get("url"):
        return {"error": "query (or url) required"}
    text = web_answer(q, url=p.get("url"))
    return {"query": q, "url": p.get("url"), "answer": text}


def _sp_contact_update_profile(p: Dict[str, Any]) -> Any:
    """Voice support: apply ONE possession-verified caller's profile change
    (phone or email, whitelisted) — executed on governance approval only;
    the before-value comes back for undo."""
    from app.core import voice_support
    return voice_support.profile_update_sp(p or {})


def _sp_order_cancel(p: Dict[str, Any]) -> Any:
    """Voice support: cancel ONE order at a verified customer's request.

    Registered so the action is inspectable, traceable and undoable like every
    other governed write. It is NOT the path the phone call uses — the call
    reaches voice_support.cancel_order_sp directly, because the customer is on
    the line waiting for a true answer. This entry exists so the capability is
    visible in the registry, and so governance.undo has a named action type.
    The status guard lives inside the UPDATE, so it applies to every caller of
    this function equally.
    """
    from app.core import voice_support
    return voice_support.cancel_order_sp(p or {})


def _sp_scoring_activate(p: Dict[str, Any]) -> Any:
    """Predictive scoring: make a trained candidate the active model
    (executed on approval); undo restores the previous version."""
    from app.core import scoring
    return scoring.activate_sp(p or {})


def _sp_dq_normalize_phones(p: Dict[str, Any]) -> Any:
    """Data quality: normalize phones to E.164 (executed on approval; undoable)."""
    from app.core import data_quality
    return data_quality.normalize_phones_sp(p or {})


def _sp_dq_merge_contacts(p: Dict[str, Any]) -> Any:
    """Data quality: merge exact-duplicate contacts (executed on approval; undoable)."""
    from app.core import data_quality
    return data_quality.merge_contacts_sp(p or {})


def _sp_identity_materialize(p: Dict[str, Any]) -> Any:
    """Identity: materialize a CONFIRMED duplicate link — re-point the duplicate's
    business rows to the primary and soft-delete it (executed on approval; undoable)."""
    from app.core import identity_links
    return identity_links.materialize_sp(p or {})


def _sp_data_erase_record(p: Dict[str, Any]) -> Any:
    """Lifecycle: erase a record's personal data (executed on approval).
    IRREVERSIBLE — there is deliberately no undo handler for this action."""
    from app.core import lifecycle
    return lifecycle.erase_sp(p or {})


def _sp_crm_plan(p: Dict[str, Any]) -> Any:
    """Bounded planner: draft + validate a plan for a goal — NO execution
    (run via POST /planner/plan with execute=true; writes queue for approval)."""
    from app.core import planner
    return planner.draft_plan(str(p.get("goal") or ""))


def _sp_crm_simulate(p: Dict[str, Any]) -> Any:
    """Read-only what-if over the objectives math — projects, never writes."""
    from app.core import simulator
    return simulator.simulate(str(p.get("scenario") or p.get("q") or ""))


def _sp_select_channel(p: Dict[str, Any]) -> Any:
    """Intelligent channel selection: best communication ACTION for an objective
    + party (Unified Communication Layer, Phase 4). Read-only — decides, never sends."""
    from app.core import channel_selector
    return channel_selector.select(
        str(p.get("objective") or "quick_update"),
        str(p.get("party_type") or "contact"),
        str(p.get("party_id") or p.get("entity_id") or ""),
        urgency=p.get("urgency"), sensitive=p.get("sensitive"))


# ---- peer handoff / negotiation -------------------------------------------
async def delegate(parent: "A2ARequest", sub_intent: str,
                   params: Optional[Dict[str, Any]] = None,
                   prose: bool = False) -> "A2AResult":
    """Hand a sub-intent off to its owning agent, propagating the parent's
    correlation_id so the whole multi-agent play shares one lineage."""
    sub = A2ARequest(intent=sub_intent, from_agent=parent.from_agent or "a2a",
                     params=params or {}, correlation_id=parent.correlation_id,
                     prose=prose)
    return await dispatch(sub)


async def _compose_pipeline_snapshot(req: "A2ARequest"):
    """Composite capability: one request fans out to peers and composes their
    structured results — a real agent-to-agent handoff. Returns (data, hops).
    The peer calls are independent — they run CONCURRENTLY."""
    fin, hot = await asyncio.gather(
        delegate(req, "accounting.summary"),
        delegate(req, "leads.list", {"scoreMin": 70, "pageSize": 5}))
    recs = (hot.data or {}).get("records") or (hot.data or {}).get("leads") or []
    data = {
        "financials": fin.data if fin.ok else {"error": fin.error},
        "hot_leads": len(recs),
        "top_hot": [{"name": f"{r.get('first_name','')} {r.get('last_name','')}".strip(),
                     "score": r.get("score")} for r in recs[:3]],
    }
    hops = [fin.intent, hot.intent]   # intents are already agent-namespaced
    return data, hops


async def _compose_crm_plan_execute(req: "A2ARequest"):
    """Composite: EXECUTE a bounded plan for a goal — run the READ steps and
    QUEUE every WRITE step for governance approval (nothing outbound). The
    executing counterpart of `crm.plan` (which only drafts). Registered as a
    compose (not sp) so planner.run_plan is awaited on the caller's loop — no
    nested event loop — and returns (result, hops) like the other composites.
    Read-kind by design: its only side effect is running reads + creating
    governance PROPOSALS, so it is not itself gated (the planner gates each
    write internally). Correlation lineage flows through run_plan → dispatch."""
    from app.core import planner
    goal = str((req.params or {}).get("goal") or "")
    res = await planner.run_plan(goal)
    hops = [str(t.get("intent")) for t in (res.get("trace") or []) if t.get("intent")]
    return res, hops


def _suggest(intent: str) -> List[str]:
    """Negotiation: closest registered intents for an unknown one."""
    base = (intent or "").split(".")[0].rstrip("s")  # 'lead' ~ 'leads'
    return [k for k in CAPABILITIES if base and base in k][:5]


# Seeded from the agents' own VALID_MODES; messages are deterministic prefixes
# / phrasings each target agent already routes (proven by the orchestrator).
CAPABILITIES: Dict[str, Capability] = _reg(
    Capability("accounting.summary", "accounting", "/accounting-chat", "read",
               lambda p: "accounting summary",
               "AR/AP financial health summary",
               sp=_sp_accounting_summary),
    Capability("accounting.account_balance", "accounting", "/accounting-chat", "read",
               lambda p: f"account balance: {p.get('account', '')}",
               "outstanding / paid / overdue balance for an account"),
    Capability("leads.enrich", "leads", "/lead-chat", "read",
               lambda p: f"enrich lead {p.get('company') or p.get('lead_id') or ''}".strip(),
               "enrich a lead with external firmographics (industry, size, revenue, "
               "website, HQ) — outward function call to an external data source",
               sp=_sp_leads_enrich),
    Capability("leads.list", "leads", "/lead-chat", "read",
               lambda p: "list leads:",
               "list leads (structured params: scoreMin/scoreMax/status/rating)",
               sp=_sp_leads_list),
    Capability("orders.sales_summary", "orders", "/order-chat", "read",
               lambda p: "Sales summary this month",
               "monthly sales summary", sp=_sp_orders_sales_summary),
    Capability("activities.list", "activities", "/activity-chat", "read",
               lambda p: "list activities:",
               "list activities", sp=_sp_activities_list),
    Capability("contacts.list", "contacts", "/contact-chat", "read",
               lambda p: "list contacts:",
               "list contacts", sp=_sp_contacts_list),
    Capability("opportunities.pipeline", "opportunities", "/opportunity-chat", "read",
               lambda p: "pipeline",
               "sales pipeline by stage", sp=_sp_opportunities_pipeline),
    Capability("products.low_stock", "products", "/prod-chat", "read",
               lambda p: "low stock",
               "low-stock products needing reorder", sp=_sp_products_low_stock),
    Capability("account.context", "accounts", "/account-chat", "read",
               lambda p: f"context: {p.get('account_id', '')}",
               "shared-blackboard context (all agents' notes) for an account",
               sp=_sp_account_context),
    Capability("email.send_payment_reminder", "email", "/email-chat", "write",
               lambda p: (f"send a payment reminder email to {p['to']} about invoice "
                          f"{p.get('invoice_number', '')} for {p.get('amount', '')}, "
                          f"{p.get('days_overdue', '')} days overdue"),
               "send an overdue-invoice payment reminder",
               sp=_sp_email_send_payment_reminder),
    Capability("campaign.winback", "marketing", "/marketing/campaigns", "write",
               lambda p: "launch a win-back campaign for high-churn customers",
               "create + launch a win-back marketing campaign (segment defaults "
               "to churn_band=high; content LLM-drafted with template fallback; "
               "CASL-gated, drafts unless AUTOSEND). Proposed by the supervisor "
               "on churn spikes; executes on governance approval",
               sp=_sp_campaign_winback),
    Capability("supervisor.emit_dunning", "supervisor", "", "write",
               lambda p: "emit overdue-invoice dunning events",
               "kick the Accounting dunning loop (supervisor auto-action; "
               "queued for approval when governance tightens ACT_MIN)",
               sp=_sp_supervisor_emit_dunning),
    Capability("supervisor.emit_hot_leads", "supervisor", "", "write",
               lambda p: "emit hot-lead outreach events",
               "kick the hot-lead outreach loop (supervisor auto-action; "
               "queued for approval when governance tightens ACT_MIN)",
               sp=_sp_supervisor_emit_hot_leads),
    Capability("objectives.report", "supervisor", "", "read",
               lambda p: "business objectives status",
               "goal-oriented supervisor: every business objective with live "
               "metric value, expected trajectory point, judgment "
               "(achieved/on_track/at_risk/off_track) and trend",
               sp=_sp_objectives_report),
    Capability("tuning.adjust", "learning", "", "write",
               lambda p: (f"set {p.get('param', '?')} to {p.get('value', '?')}"),
               "write a governed model parameter (churn-band thresholds); "
               "proposed by the learning loop from calibration evidence, "
               "bounds-enforced, executes on governance approval, undoable",
               sp=_sp_tuning_adjust),
    Capability("meeting.book", "activities", "", "write",
               lambda p: (f"book a meeting with "
                          f"{p.get('entity_type', 'lead')} "
                          f"{p.get('entity_id') or p.get('lead_id') or ''}"),
               "real meeting booking: availability-checked slot on the "
               "owner's calendar (ET business hours, preferred-hour aware), "
               "meeting activity + signed .ics invite link; invite emails "
               "only under AUTOSEND to verified addresses; undo cancels",
               sp=_sp_meeting_book),
    Capability("kb.publish", "email", "", "write",
               lambda p: f"publish KB article: {p.get('title', '')}",
               "publish a knowledge-base article (mined from a resolved "
               "support thread, LLM-drafted, critic-checked); executes on "
               "governance approval; undo retires the article",
               sp=_sp_kb_publish),
    Capability("crm.context", "orchestrator", "", "read",
               lambda p: f"context: {p.get('entity_type', 'account')} "
                         f"{p.get('entity_id', '')}",
               "context hydration: the compact deterministic 360 pack "
               "(profile, blackboard signals, open money, cadences, pending "
               "approvals, last touches) for an account/lead/contact — what "
               "every agent is 'born with'",
               sp=_sp_crm_context),
    Capability("sms.send", "email", "", "write",
               lambda p: f"send SMS to {p.get('to', '?')}",
               "send one SMS via the Twilio channel (drafts as an owner task "
               "unless SMS_AUTOSEND=1; trial accounts only reach verified "
               "numbers; irreversible once sent)",
               sp=_sp_sms_send),
    Capability("quote.generate", "email", "", "write",
               lambda p: (f"send a quotation to account "
                          f"{p.get('account_id', '?')} for "
                          f"{len(p.get('items') or [])} item(s)"),
               "build + deliver a priced quotation: products matched "
               "exactly/by prefix against the catalog, CURRENT retail "
               "pricing, deterministic totals (LLM never touches a number), "
               "30-day validity; emailed only under AUTOSEND to a verified "
               "address, otherwise drafted as an owner task; CASL-compliant",
               sp=_sp_quote_generate),
    Capability("web.consult", "orchestrator", "", "read",
               lambda p: f"search the web for {p.get('query', '')}",
               "consult the live internet with cited sources (ddgs/Tavily "
               "search + page fetch + LLM synthesis; web_answer). The "
               "Internet Agent capability: manufacturer docs, regulations, "
               "logistics, news — anything the CRM can't know from inside",
               sp=_sp_web_consult),
    Capability("contact.update_profile", "contacts", "", "write",
               lambda p: (f"update contact {p.get('contact_id', '?')} "
                          f"{p.get('field', '?')} to {p.get('new_value', '?')}"),
               "change a contact's phone or email at the request of a "
               "possession-verified support caller (voice OTP); ALWAYS "
               "proposed — never auto-executed from a call — validated again "
               "at execution time, undo restores the before-value",
               sp=_sp_contact_update_profile),
    Capability("order.cancel", "orders", "", "write",
               lambda p: (f"cancel order {p.get('order_number') or p.get('order_id', '?')} "
                          f"(verified {p.get('verified_via', '?')})"),
               "cancel an order at the request of a customer who has proven "
               "possession of the phone number on the order AND matched the "
               "name, shipping address and email on record; the allowed source "
               "statuses (pending/processing/ready) are enforced inside the "
               "UPDATE, not by the caller, so a shipped or delivered order "
               "cannot be cancelled through this capability by anyone; "
               "undo restores the prior status",
               sp=_sp_order_cancel),
    Capability("scoring.activate", "leads", "", "write",
               lambda p: f"activate lead-scoring model v{p.get('version', '?')}",
               "make a trained predictive lead-scoring candidate the active "
               "model (trained weekly on settled leads, proposed with holdout "
               "evidence, executes on governance approval, undo restores the "
               "previous version)",
               sp=_sp_scoring_activate),
    Capability("data.normalize_phones", "contacts", "", "write",
               lambda p: "normalize contact/lead phones to E.164",
               "data-quality fix: normalize unnormalized phone numbers "
               "(capped per run, before-values recorded, undoable); proposed "
               "nightly by the data-quality agent, executes on approval",
               sp=_sp_dq_normalize_phones),
    Capability("data.merge_contacts", "contacts", "", "write",
               lambda p: "merge exact-duplicate contacts",
               "data-quality fix: merge duplicate contacts (same account + "
               "email) into the oldest — activities reassigned, dupes "
               "soft-deleted, every move recorded, undoable",
               sp=_sp_dq_merge_contacts),
    Capability("data.erase_record", "lifecycle", "", "write",
               lambda p: f"erase personal data for {p.get('entity','record')} "
                         f"{str(p.get('record_id',''))[:8]}",
               "data lifecycle: honour an erasure request — delete the personal "
               "satellites (custom fields, AI memories, transcripts, identity "
               "links), de-link activity history, and redact the core record, "
               "while RETAINING financial, suppression and audit records by "
               "policy. IRREVERSIBLE — there is no undo",
               sp=_sp_data_erase_record),
    Capability("identity.materialize_link", "identity_resolution", "", "write",
               lambda p: f"merge duplicate {p.get('entity', 'record')} into its primary",
               "identity resolution: physically merge an ALREADY-CONFIRMED duplicate "
               "link — the duplicate's business/history rows are re-pointed to the "
               "primary and it is soft-deleted (logins and derived intelligence are "
               "never moved). One transaction, every move recorded, undoable. Reads "
               "already resolve through the link, so this is optional",
               sp=_sp_identity_materialize),
    Capability("crm.plan", "orchestrator", "", "read",
               lambda p: f"plan: {p.get('goal', '')}",
               "bounded goal→plan orchestration: draft a validated multi-step "
               "plan over the registered capabilities (≤6 steps, ≤2 writes) — "
               "draft only; execution runs reads and queues writes for "
               "governance approval",
               sp=_sp_crm_plan),
    Capability("crm.simulate", "orchestrator", "", "read",
               lambda p: f"simulate: {p.get('scenario', p.get('q', ''))}",
               "read-only what-if scenario over the registered business "
               "metrics + active objectives (e.g. scenario='cut overdue "
               "invoices by 30%') — projects status/gap impact, never writes",
               sp=_sp_crm_simulate),
    # Intelligent channel selection (Unified Communication Layer, Phase 4):
    # the best communication ACTION for an objective + party. Read-only.
    Capability("comms.select_channel", "orchestrator", "", "read",
               lambda p: (f"select channel for {p.get('objective','?')} to "
                          f"{p.get('party_type','contact')} {p.get('party_id','')}"),
               "pick the best channel/action for an objective + party (intent + "
               "identity + urgency + learned preference + authorization) — decides, "
               "never sends; the vision's 'what's the best way to accomplish this?'",
               sp=_sp_select_channel),
    # Composite (peer handoff): fans out to Accounting + Leads and composes.
    Capability("crm.pipeline_snapshot", "orchestrator", "", "read",
               lambda p: "", "financial + hot-lead snapshot composed from peers",
               compose=_compose_pipeline_snapshot),
    # Executing counterpart of crm.plan: runs a bounded plan for a goal —
    # reads execute, writes queue for governance approval (nothing outbound).
    Capability("crm.plan_execute", "orchestrator", "", "read",
               lambda p: f"execute plan: {p.get('goal', '')}",
               "execute a bounded goal→plan: run the READ steps and QUEUE every "
               "WRITE step for governance approval (nothing outbound) — the "
               "executing counterpart of crm.plan (draft-only)",
               compose=_compose_crm_plan_execute),
)

# Generic NL-passthrough capability per agent — lets the orchestrator route ANY
# single-agent query through the typed A2A layer (the agent still does its own
# NL/deterministic routing on the forwarded message). params['message'] is the
# user's text; render passes it straight through.
_QUERY_AGENTS = [
    ("accounts", "/account-chat"), ("contacts", "/contact-chat"),
    ("leads", "/lead-chat"), ("opportunities", "/opportunity-chat"),
    ("orders", "/order-chat"), ("products", "/prod-chat"),
    ("activities", "/activity-chat"), ("notifications", "/notifications-chat"),
    ("accounting", "/accounting-chat"), ("analytics", "/analytics-chat"),
    ("email", "/email-chat"),
]
for _a, _ep in _QUERY_AGENTS:
    CAPABILITIES[f"{_a}.query"] = Capability(
        f"{_a}.query", _a, _ep, "read",
        lambda p: p.get("message", ""),
        f"natural-language passthrough to the {_a} agent")

_ENDPOINT_TO_QUERY_INTENT = {ep: f"{a}.query" for a, ep in _QUERY_AGENTS}


def query_intent_for_endpoint(endpoint: str) -> Optional[str]:
    """Map a '/x-chat' endpoint to its NL-passthrough capability intent."""
    return _ENDPOINT_TO_QUERY_INTENT.get(endpoint)


def resolve(intent: str, to_agent: Optional[str] = None) -> Optional[Capability]:
    """Route by capability. If to_agent is given it must match (lets a caller
    pin a specific agent when several could serve an intent)."""
    cap = CAPABILITIES.get(intent)
    if cap and to_agent and cap.agent != to_agent:
        return None
    return cap


def manifest() -> Dict[str, Any]:
    """Discoverable capability manifest (what each agent can be asked to do)."""
    by_agent: Dict[str, List[str]] = {}
    caps = []
    for c in CAPABILITIES.values():
        caps.append({"intent": c.intent, "agent": c.agent, "endpoint": c.endpoint,
                     "kind": c.kind, "structured": c.sp is not None,
                     "composite": c.compose is not None,
                     "description": c.description})
        by_agent.setdefault(c.agent, []).append(c.intent)
    return {"count": len(caps), "capabilities": caps, "by_agent": by_agent}


# ============================================================================
# REGISTRY-AS-DATA (audit #4) — runtime enable/disable per capability
# ============================================================================
# The in-code registry stays the source of WHAT exists; capability_registry
# only overrides AVAILABILITY. No row (or no table) = enabled — the gate can
# degrade but never lock the platform out.

_REG_TTL = int(os.getenv("REGISTRY_TTL_SECS", "30"))
_reg_cache: Dict[str, Any] = {"at": 0.0, "rows": {}}


def _registry_rows() -> Dict[str, Dict[str, Any]]:
    """Cached {intent: {enabled, notes, updated_by, updated_at}} from the
    capability_registry table. {} on any failure (everything enabled)."""
    if time.time() - _reg_cache["at"] < _REG_TTL:
        return _reg_cache["rows"]
    rows: Dict[str, Dict[str, Any]] = {}
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute("SELECT intent, enabled, notes, updated_by, "
                                "updated_at, allowed_callers "
                                "FROM capability_registry")
                except Exception:
                    conn.rollback()   # pre-ACL schema — degrade to 5 columns
                    cur.execute("SELECT intent, enabled, notes, updated_by, "
                                "updated_at, NULL FROM capability_registry")
                for r in cur.fetchall():
                    rows[r[0]] = {"enabled": bool(r[1]), "notes": r[2],
                                  "updated_by": r[3],
                                  "updated_at": r[4].isoformat() if r[4] else None,
                                  "allowed_callers": r[5]}
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[a2a] registry table skipped: {exc}")
    _reg_cache.update(at=time.time(), rows=rows)
    return rows


def _invalidate_registry_cache() -> None:
    _reg_cache["at"] = 0.0


def capability_enabled(intent: str) -> bool:
    row = _registry_rows().get(intent)
    return True if row is None else bool(row.get("enabled", True))


# ============================================================================
# DISPATCH TRACE LOG (audit #5) — one row per real dispatch, by correlation id
# ============================================================================

_TRACE_RETENTION_DAYS = int(os.getenv("TRACE_RETENTION_DAYS", "30"))


def _log_dispatch(req: "A2ARequest", res: "A2AResult", ms: int) -> None:
    """Best-effort insert into a2a_dispatches — the spine of GET /trace/{cid}.
    Never raises; a missing table just means no trace rows."""
    try:
        from app.core.database import get_connection
        cap = CAPABILITIES.get(req.intent)
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO a2a_dispatches (correlation_id, intent,
                         from_agent, agent, kind, ok, error, latency_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (res.correlation_id, req.intent, req.from_agent,
                     res.agent, cap.kind if cap else None, res.ok,
                     (res.error or "")[:500] or None, ms))
                if random.random() < 0.01:      # opportunistic GC
                    cur.execute("DELETE FROM a2a_dispatches WHERE at < now() "
                                "- make_interval(days => %s)",
                                (_TRACE_RETENTION_DAYS,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[a2a] dispatch log skipped: {exc}")


# ============================================================================
# IN-PROCESS INVOKE (ASGI — no network hop)
# ============================================================================

async def _invoke(endpoint: str, message: str,
                  session_id: str) -> Tuple[Optional[int], dict, bool]:
    """POST the agent endpoint in-process. Returns (status, body, parsed).

    THE STATUS IS RETURNED BECAUSE DISCARDING IT WAS THE DEFECT. This used to
    return the body alone, so dispatch() judged success from body keys and never
    saw the HTTP code. A 403 from an admin-gated endpoint carries
    {"detail": "..."} — no `success`, no `error` — and was read as success:
    measured on 2026-06-26, 25 payment reminders were recorded as sent while the
    independent BCC archive holds none. `parsed` is False when the body was not
    JSON, so "we could not read the answer" stays distinct from "the answer said
    nothing was wrong".
    """
    from app.main import app as _app  # lazy import avoids a circular import
    transport = httpx.ASGITransport(app=_app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://a2a.internal",
                                 timeout=300) as client:
        resp = await client.post(endpoint, json={
            "sessionId": session_id,
            "chatInput": {"message": message},
        })
        try:
            data = resp.json()
        except Exception:
            return resp.status_code, {"output": resp.text[:2000]}, False
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return resp.status_code, {"output": str(data)[:2000]}, True
        return resp.status_code, data, True


# ============================================================================
# DISPATCH
# ============================================================================

def _summarize(intent: str, data: Any) -> str:
    if isinstance(data, dict):
        recs = data.get("records") or data.get("leads") or data.get("data")
        if isinstance(recs, list):
            return f"{intent}: {len(recs)} record(s)"
        return f"{intent}: {len(data)} field(s)"
    if isinstance(data, list):
        return f"{intent}: {len(data)} row(s)"
    return f"{intent}: ok"


async def dispatch(req: A2ARequest, dry_run: bool = False) -> A2AResult:
    """Public entry — dispatch + best-effort trace logging (a2a_dispatches,
    read back by GET /trace/{correlation_id}). Logging can never fail a call."""
    t0 = time.time()
    res = await _dispatch(req, dry_run)
    if not dry_run:
        try:
            await asyncio.to_thread(_log_dispatch, req, res,
                                    int((time.time() - t0) * 1000))
        except Exception as exc:
            logger.debug(f"[a2a] dispatch log skipped: {exc}")
    return res


async def _dispatch(req: A2ARequest, dry_run: bool = False) -> A2AResult:
    """Resolve the intent's capability and invoke the owning agent in-process."""
    cid = req.correlation_id or str(_uuid.uuid4())
    req.correlation_id = cid          # so delegated sub-calls share the lineage
    # Bind the id for this play so any memory retrieval underneath — which
    # happens several call-layers down, in modules that take no correlation id —
    # is recorded against this trace. Reset in the finally below; a leaked token
    # would attribute one play's grounding to the next.
    from app.core import grounding as _grounding
    _gtok = _grounding.set_correlation_id(cid)
    try:
        return await _dispatch_inner(req, cid, dry_run)
    finally:
        _grounding.reset_correlation_id(_gtok)


async def _dispatch_inner(req: A2ARequest, cid: str,
                          dry_run: bool = False) -> A2AResult:
    cap = resolve(req.intent, getattr(req, "to_agent", None))
    if not cap:
        # Negotiation: no exact capability — offer the closest matches.
        sugg = _suggest(req.intent)
        return A2AResult(False, req.intent, "none", cid,
                         error=f"No capability registered for intent '{req.intent}'"
                               + (f". Did you mean: {', '.join(sugg)}?" if sugg else ""),
                         data={"suggestions": sugg} if sugg else None)

    # Registry-as-data gate: an operator-disabled capability refuses cleanly
    # (structured error, traced) instead of executing.
    reg = _registry_rows().get(req.intent) or {}
    if not reg.get("enabled", True):
        notes = reg.get("notes")
        return A2AResult(False, req.intent, cap.agent, cid,
                         error=f"capability '{req.intent}' is disabled in the "
                               f"capability registry"
                               + (f" — {notes}" if notes else ""))

    # Agent RBAC (guardrail layer 4): allowed_callers restricts WHO may
    # dispatch this capability. Approved executions (govern_bypass) pass —
    # the human approval is the authority.
    allowed = reg.get("allowed_callers")
    if (isinstance(allowed, list) and allowed
            and req.from_agent not in allowed and not req.govern_bypass):
        return A2AResult(False, req.intent, cap.agent, cid,
                         error=f"caller '{req.from_agent}' is not permitted to "
                               f"dispatch '{req.intent}' (allowed: "
                               f"{', '.join(allowed)})")

    # Composite (peer handoff): the handler delegates to peer agents.
    if cap.compose is not None:
        if dry_run:
            return A2AResult(True, req.intent, cap.agent, cid,
                             output=f"[dry-run] '{req.intent}' composes from peer agents")
        try:
            data, hops = await cap.compose(req)
        except Exception as exc:
            return A2AResult(False, req.intent, cap.agent, cid,
                             error=f"compose failed for '{req.intent}': {exc}")
        logger.info(f"[a2a] {req.from_agent} → {req.intent} composed via {hops} cid={cid[:8]}")
        return A2AResult(True, req.intent, cap.agent, cid, data=data,
                         output=f"{req.intent}: composed from {len(hops)} agent(s)",
                         hops=hops)

    structured = cap.sp is not None and not req.prose
    if dry_run:
        via = "structured SP (data)" if structured else f"agent {cap.endpoint} (prose)"
        return A2AResult(True, req.intent, cap.agent, cid,
                         output=f"[dry-run] would route '{req.intent}' → "
                                f"{cap.agent} via {via}")

    # Phase 5 governance: gate WRITE/outbound actions by confidence.
    #   act → execute; propose → queue for human approval; skip → don't act.
    # No-op unless GOV_ENABLED. govern_bypass=True is set by an approved action.
    # 2026-07-19: this gate now sits BEFORE the structured branch — the six
    # structured writes (sms.send, quote.generate, …) previously slipped past
    # it, relying only on their SPs' internal safeguards.
    if cap.kind == "write" and not req.govern_bypass:
        from app.core import governance
        if governance.ENABLED:
            d = governance.decide(req.confidence)
            # HITL amount floor (guardrail layer 1): a big-ticket action pauses
            # for a human even at act-level confidence — confidence measures
            # how sure the agent is, not how much is at stake.
            if d == "act":
                hitl = governance.hitl_amount()
                amt = governance._amount_from(dict(req.params))
                if hitl > 0 and amt >= hitl:
                    d = "propose"
                    logger.info(f"[a2a] {req.intent} ${amt:,.0f} ≥ HITL "
                                f"${hitl:,.0f} → forced propose")
            if d == "skip":
                return A2AResult(False, req.intent, cap.agent, cid,
                                 error=f"skipped by governance — confidence "
                                       f"{req.confidence} < {governance.propose_min()}")
            if d == "propose":
                # _correlation_id ties the approval to this play's trace
                # (hidden from the approval UI, dropped before execution).
                p = dict(req.params)
                p["_correlation_id"] = cid
                aid = governance.propose(
                    req.intent, req.from_agent, p,
                    req.entity.type if req.entity else None,
                    req.entity.id if req.entity else None, req.confidence)
                logger.info(f"[a2a] {req.intent} gated → proposed {aid[:8]} "
                            f"(conf={req.confidence})")
                return A2AResult(True, req.intent, cap.agent, cid,
                                 output=f"proposed for approval (confidence {req.confidence})",
                                 data={"status": "pending_approval", "approval_uuid": aid})
            # d == "act" → fall through and execute

    # Structured input contract: deterministic params → SP → structured data.
    # No NL parsing, no AI, no HTTP — the default for agent-to-agent calls.
    if structured:
        try:
            data = await asyncio.to_thread(cap.sp, req.params)
        except Exception as exc:
            return A2AResult(False, req.intent, cap.agent, cid,
                             outcome=FAILED,
                             error=f"sp failed for '{req.intent}': {exc}")
        # Not raising is not the same as succeeding. An SP that refuses by
        # RETURN VALUE ({'ok': False}, {'success': False}, {'error': ...}) used
        # to arrive here as A2AResult(True, ...) — the structured twin of the
        # defect that recorded 25 payment reminders as sent.
        outcome = classify_sp_result(data)
        if outcome != ACCEPTED:
            detail = (data.get("error") or data.get("message")
                      if isinstance(data, dict) else None)
            logger.warning(f"[a2a] {req.from_agent} → {cap.agent}.{req.intent} "
                           f"(structured) → {outcome} cid={cid[:8]}"
                           + (f": {detail}" if detail else ""))
            return A2AResult(False, req.intent, cap.agent, cid, data=data,
                             outcome=outcome,
                             error=str(detail) if detail else
                             f"'{req.intent}' returned {outcome}")
        logger.info(f"[a2a] {req.from_agent} → {cap.agent}.{req.intent} "
                    f"(structured) cid={cid[:8]}")
        return A2AResult(True, req.intent, cap.agent, cid, data=data,
                         outcome=ACCEPTED,
                         output=_summarize(req.intent, data))

    try:
        message = cap.render(req.params)
    except Exception as exc:
        return A2AResult(False, req.intent, cap.agent, cid,
                         error=f"render failed for '{req.intent}': {exc}")

    # Context hydration ("born with context"): a request that carries an
    # entity gets the compact CRM context block prepended, so the receiving
    # agent starts already knowing the customer. Best-effort, read-only,
    # kill switch CONTEXT_HYDRATION_ENABLED.
    if req.entity and getattr(req.entity, "id", None):
        try:
            from app.core import context as _crm_context
            block = await asyncio.to_thread(
                _crm_context.render_for, req.entity.type, req.entity.id)
            if block:
                message = f"{block}\n\n{message}"
        except Exception as exc:
            logger.debug(f"[a2a] context hydration skipped: {exc}")

    session = f"a2a-{req.from_agent}-{cid[:8]}"
    logger.info(f"[a2a] {req.from_agent} → {cap.agent}.{req.intent} "
                f"(conf={req.confidence}) cid={cid[:8]}")
    try:
        status, resp, parsed = await _invoke(cap.endpoint, message, session)
    except Exception as exc:
        # A transport failure is UNKNOWN, not failure-shaped-as-success and not
        # a silent swallow: the call may or may not have reached the agent.
        logger.warning(f"[a2a] transport failure {cap.endpoint} cid={cid[:8]}: {exc}")
        return A2AResult(False, req.intent, cap.agent, cid,
                         error=f"transport failure: {exc}", outcome=UNKNOWN)

    outcome = classify_outcome(status, resp, parsed)
    if outcome != ACCEPTED:
        logger.warning(f"[a2a] {cap.agent}.{req.intent} → {outcome} "
                       f"(HTTP {status}) cid={cid[:8]}")
    return A2AResult(
        ok=(outcome == ACCEPTED),
        intent=req.intent, agent=cap.agent, correlation_id=cid,
        data=resp.get("records") if "records" in resp else resp.get("result"),
        output=str(resp.get("output", ""))[:4000],
        error=resp.get("error") or (None if outcome == ACCEPTED
                                    else f"{outcome} (HTTP {status})"),
        raw=resp, outcome=outcome, status=status,
    )


# ============================================================================
# DISCOVERY + DISPATCH ENDPOINTS
# ============================================================================

router = APIRouter(tags=["a2a"])


@router.get("/a2a/capabilities")
def a2a_capabilities():
    return manifest()


# ── Registry-as-data endpoints (audit #4) ────────────────────────────────────

class _RegistryBody(BaseModel):
    enabled: bool
    notes: Optional[str] = None
    updated_by: Optional[str] = None
    allowed_callers: Optional[List[str]] = None   # None = leave unchanged;
                                                  # [] = clear (anyone)


@router.get("/a2a/registry")
def a2a_registry():
    """The capability manifest merged with runtime availability state."""
    state = _registry_rows()
    caps = []
    for c in manifest()["capabilities"]:
        row = state.get(c["intent"]) or {}
        caps.append({**c, "enabled": row.get("enabled", True),
                     "notes": row.get("notes"),
                     "updated_by": row.get("updated_by"),
                     "updated_at": row.get("updated_at"),
                     "override": c["intent"] in state})
    orphans = sorted(k for k in state if k not in CAPABILITIES)
    disabled = sorted(c["intent"] for c in caps if not c["enabled"])
    return {"count": len(caps), "disabled": disabled, "capabilities": caps,
            **({"orphan_rows": orphans} if orphans else {})}


@router.post("/a2a/registry/{intent}")
def a2a_registry_set(intent: str, body: _RegistryBody):
    """Enable/disable one capability at runtime. Unknown intents are refused —
    the table overrides availability, it doesn't define capabilities."""
    if intent not in CAPABILITIES:
        return {"ok": False, "error": f"unknown capability '{intent}'"}
    import json as _json
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO capability_registry
                     (intent, enabled, notes, updated_by, allowed_callers)
                   VALUES (%s,%s,%s,%s,%s::jsonb)
                   ON CONFLICT (intent) DO UPDATE SET
                     enabled=EXCLUDED.enabled, notes=EXCLUDED.notes,
                     updated_by=EXCLUDED.updated_by,
                     allowed_callers=CASE WHEN %s THEN EXCLUDED.allowed_callers
                                          ELSE capability_registry.allowed_callers END,
                     updated_at=now()""",
                (intent, body.enabled, body.notes, body.updated_by or "admin",
                 _json.dumps(body.allowed_callers)
                 if body.allowed_callers is not None else None,
                 body.allowed_callers is not None))
        conn.commit()
    finally:
        conn.close()
    _invalidate_registry_cache()
    logger.info(f"[a2a] capability {intent} → "
                f"{'ENABLED' if body.enabled else 'DISABLED'}"
                + (f", callers={body.allowed_callers}"
                   if body.allowed_callers is not None else "")
                + f" (by {body.updated_by or 'admin'})")
    return {"ok": True, "intent": intent, "enabled": body.enabled,
            "allowed_callers": body.allowed_callers}


class _DispatchBody(BaseModel):
    intent: str
    from_agent: str = "system"
    params: Dict[str, Any] = {}
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    correlation_id: Optional[str] = None
    confidence: float = 1.0
    prose: bool = False
    dry_run: bool = False


@router.post("/a2a/dispatch")
async def a2a_dispatch(body: _DispatchBody):
    req = A2ARequest(
        intent=body.intent, from_agent=body.from_agent, params=body.params,
        entity=EntityRef(body.entity_type, body.entity_id) if body.entity_type else None,
        correlation_id=body.correlation_id, confidence=body.confidence,
        prose=body.prose,
    )
    return asdict(await dispatch(req, dry_run=body.dry_run))
