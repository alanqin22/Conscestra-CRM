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
    structured results — a real agent-to-agent handoff. Returns (data, hops)."""
    fin = await delegate(req, "accounting.summary")
    hot = await delegate(req, "leads.list", {"scoreMin": 70, "pageSize": 5})
    recs = (hot.data or {}).get("records") or (hot.data or {}).get("leads") or []
    data = {
        "financials": fin.data if fin.ok else {"error": fin.error},
        "hot_leads": len(recs),
        "top_hot": [{"name": f"{r.get('first_name','')} {r.get('last_name','')}".strip(),
                     "score": r.get("score")} for r in recs[:3]],
    }
    hops = [fin.intent, hot.intent]   # intents are already agent-namespaced
    return data, hops


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
               "send an overdue-invoice payment reminder"),
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
    # Composite (peer handoff): fans out to Accounting + Leads and composes.
    Capability("crm.pipeline_snapshot", "orchestrator", "", "read",
               lambda p: "", "financial + hot-lead snapshot composed from peers",
               compose=_compose_pipeline_snapshot),
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
# IN-PROCESS INVOKE (ASGI — no network hop)
# ============================================================================

async def _invoke(endpoint: str, message: str, session_id: str) -> dict:
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
            return {"output": resp.text[:2000]}
        if isinstance(data, list):
            data = data[0] if data else {}
        return data if isinstance(data, dict) else {"output": str(data)[:2000]}


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
    """Resolve the intent's capability and invoke the owning agent in-process."""
    cid = req.correlation_id or str(_uuid.uuid4())
    req.correlation_id = cid          # so delegated sub-calls share the lineage
    cap = resolve(req.intent, getattr(req, "to_agent", None))
    if not cap:
        # Negotiation: no exact capability — offer the closest matches.
        sugg = _suggest(req.intent)
        return A2AResult(False, req.intent, "none", cid,
                         error=f"No capability registered for intent '{req.intent}'"
                               + (f". Did you mean: {', '.join(sugg)}?" if sugg else ""),
                         data={"suggestions": sugg} if sugg else None)

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

    # Structured input contract: deterministic params → SP → structured data.
    # No NL parsing, no AI, no HTTP — the default for agent-to-agent calls.
    if structured:
        try:
            data = await asyncio.to_thread(cap.sp, req.params)
        except Exception as exc:
            return A2AResult(False, req.intent, cap.agent, cid,
                             error=f"sp failed for '{req.intent}': {exc}")
        logger.info(f"[a2a] {req.from_agent} → {cap.agent}.{req.intent} "
                    f"(structured) cid={cid[:8]}")
        return A2AResult(True, req.intent, cap.agent, cid, data=data,
                         output=_summarize(req.intent, data))

    # Phase 5 governance: gate WRITE/outbound actions by confidence.
    #   act → execute; propose → queue for human approval; skip → don't act.
    # No-op unless GOV_ENABLED. govern_bypass=True is set by an approved action.
    if cap.kind == "write" and not req.govern_bypass:
        from app.core import governance
        if governance.ENABLED:
            d = governance.decide(req.confidence)
            if d == "skip":
                return A2AResult(False, req.intent, cap.agent, cid,
                                 error=f"skipped by governance — confidence "
                                       f"{req.confidence} < {governance.PROPOSE_MIN}")
            if d == "propose":
                aid = governance.propose(
                    req.intent, req.from_agent, dict(req.params),
                    req.entity.type if req.entity else None,
                    req.entity.id if req.entity else None, req.confidence)
                logger.info(f"[a2a] {req.intent} gated → proposed {aid[:8]} "
                            f"(conf={req.confidence})")
                return A2AResult(True, req.intent, cap.agent, cid,
                                 output=f"proposed for approval (confidence {req.confidence})",
                                 data={"status": "pending_approval", "approval_uuid": aid})
            # d == "act" → fall through and execute

    try:
        message = cap.render(req.params)
    except Exception as exc:
        return A2AResult(False, req.intent, cap.agent, cid,
                         error=f"render failed for '{req.intent}': {exc}")

    session = f"a2a-{req.from_agent}-{cid[:8]}"
    logger.info(f"[a2a] {req.from_agent} → {cap.agent}.{req.intent} "
                f"(conf={req.confidence}) cid={cid[:8]}")
    resp = await _invoke(cap.endpoint, message, session)
    return A2AResult(
        ok=(resp.get("success") is not False) and not resp.get("error"),
        intent=req.intent, agent=cap.agent, correlation_id=cid,
        data=resp.get("records") if "records" in resp else resp.get("result"),
        output=str(resp.get("output", ""))[:4000],
        error=resp.get("error"),
        raw=resp,
    )


# ============================================================================
# DISCOVERY + DISPATCH ENDPOINTS
# ============================================================================

router = APIRouter(tags=["a2a"])


@router.get("/a2a/capabilities")
def a2a_capabilities():
    return manifest()


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
