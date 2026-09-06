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
import contextvars
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


@dataclass(frozen=True)
class Principal:
    """WHO initiated this, as opposed to WHICH AGENT is carrying it.

    `from_agent` was doing both jobs and could only do one. It answers "which
    component is calling", which is what `allowed_callers` needs; it cannot
    answer "on whose authority", which is what an audit trail needs and what
    every per-user feature will need. Two staff users with the same role were
    indistinguishable below the HTTP edge.

    FROZEN, AND NEVER BUILT FROM PROSE. A principal is stamped at an
    authenticated boundary (`from_session`) or declared by a named background
    caller (`service`). There is no path from `params`, from an LLM's output,
    or from anything a user typed — which is why this is a dataclass and not a
    dict the request could carry ad hoc.

    Deliberately NOT a permission set. It carries identity and the role that
    identity already had; authorization stays where it is (write_guard at the
    SQL choke point, allowed_callers at the mesh, governance at the write).
    The point is that identity TRAVELS, not that a new policy engine decides.
    """
    # 'user'     an authenticated person
    # 'service'   a named background caller (scheduler, agent-bus, expiry)
    # 'customer'  a portal customer (source_table='contacts'), id=contact_id
    # 'policy'    an automatic decision by a named policy — NOT a person
    # 'token'     authorised by possession of a signed link; the mailbox is
    #             proven, the individual is not
    #
    # The last two exist because an approval's `decided_by` is frequently a
    # policy or a channel, and recording those as `user` put a false category
    # into the one column that answers "who initiated this".
    kind: str
    id: str
    display: str = ""
    role: str = ""
    tenant_id: Optional[str] = None

    def __str__(self) -> str:       # the audit representation
        return f"{self.kind}:{self.id}"

    @staticmethod
    def service(name: str) -> "Principal":
        """A named background caller — scheduler, agent-bus, a migration.

        Background work is not anonymous work. Requiring writes to carry a
        principal would break every scheduled job unless "no principal" were
        allowed to mean "system", and that is precisely the ambiguity worth
        removing: an unattended write must say which unattended thing did it.
        """
        return Principal(kind="service", id=name, display=name, role="system")

    @staticmethod
    def from_session(sess: Optional[Dict[str, Any]]) -> Optional["Principal"]:
        """Build from an `auth_sessions` row. None when there is no session —
        the caller decides whether that is allowed, because a read may proceed
        anonymously and a write may not."""
        if not sess:
            return None
        ident = (sess.get("identifier") or sess.get("credential_id")
                 or sess.get("contact_id") or "")
        if not ident:
            return None
        name = " ".join(x for x in (sess.get("first_name"),
                                    sess.get("last_name")) if x).strip()
        role = str(sess.get("role") or "")

        # A CUSTOMER IS NOT A STAFF USER, and this used to record both as
        # `user`. That is B1's defect in the other half of the vocabulary: the
        # column that answers "who initiated this" could not distinguish the
        # person who works here from the person who bought something.
        #
        # THE DISCRIMINATOR IS NOT `source_table` ALONE. Every session on this
        # database is source_table='leads' with role='admin' — staff sign in
        # through lead-backed credential rows — so keying on the table would
        # relabel real administrators as customers and invent a false category
        # while removing one. `source_table='contacts'` is set only where an
        # account_id resolved (see agents/auth/router.py), which is the portal
        # customer path.
        #
        # The role condition makes the rule FAIL TOWARD `user`: anything that
        # can write is staff, so a misread can only land on the previous
        # behaviour, never on a new falsehood. The id is the contact_id, which
        # is what the column comment promises for this kind.
        from app.core.auth_dep import WRITE_ROLES
        if str(sess.get("source_table") or "") == "contacts"                 and role not in WRITE_ROLES and sess.get("contact_id"):
            return Principal(kind="customer", id=str(sess["contact_id"]),
                             display=name, role=role or "customer",
                             tenant_id=(str(sess["tenant_id"])
                                        if sess.get("tenant_id") else None))

        return Principal(kind="user", id=str(ident), display=name,
                         role=role,
                         tenant_id=(str(sess["tenant_id"])
                                    if sess.get("tenant_id") else None))


# The principal for the CURRENT request, stamped at the authenticated boundary.
#
# A ContextVar rather than a parameter threaded through every call site, for the
# same reason write_guard uses one: the orchestrator hands work to agents over an
# in-process ASGI transport, and anything not carried in context is lost at that
# hop. `dispatch()` reads this when the caller did not pass one explicitly, so an
# existing caller inherits the right identity without being rewritten.
_principal_ctx: "contextvars.ContextVar[Optional[Principal]]" = \
    contextvars.ContextVar("a2a_principal", default=None)


def set_principal(p: Optional[Principal]) -> None:
    _principal_ctx.set(p)


def current_principal() -> Optional[Principal]:
    return _principal_ctx.get()


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
    principal: Optional["Principal"] = None
                                     # WHO, not which agent. Defaults from the
                                     # request-scoped context when unset, so an
                                     # existing caller inherits the right
                                     # identity without being rewritten.


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
    # Optional PARAMETER CONTRACT for write capabilities: (required, optional).
    #
    # DELIBERATELY THE SMALLEST USEFUL SHAPE. Not types, not ranges, not
    # enums — those belong in the stored procedure and in the SQL predicate,
    # where they are checked against the committed row under a lock rather than
    # against a dict in Python. A second business-rule engine here would be two
    # places to change one rule, and the copy that drifts is the one nobody
    # watches.
    #
    # What it DOES add was measured before it was written: of the twelve
    # resolvable write targets, five guard their required fields and SEVEN DO
    # NOT, and NONE rejects an unexpected key. So a missing parameter currently
    # travels into the domain layer to be discovered late or not at all, and an
    # LLM-supplied stray key travels all the way in. This stops both at the
    # boundary, and gives the planner a contract it can read.
    params_schema: Optional[tuple] = None       # (required: tuple, optional: tuple)


def _reg(*caps: Capability) -> Dict[str, Capability]:
    return {c.intent: c for c in caps}


# Params every caller may send that are never part of a capability's own
# contract — routing and audit metadata that `dispatch` and the agents thread
# through. Listing them once here keeps each schema to the fields that are
# actually the capability's business.
_AMBIENT_PARAMS = frozenset({
    "message", "correlation_id", "session_id", "sessionId",
    "actor", "created_by", "updated_by", "reason", "source",
    # JUSTIFICATION FOR THE APPROVER, not input for the capability.
    #
    # `data_quality.propose_fixes` attaches {"why": …, "evidence": {…}} to every
    # proposal it raises, so a human deciding it can see the detector, the count
    # and the reasoning. They are the same category as `reason`, which was
    # already here: written by the PROPOSAL layer for the person reading the
    # queue, never read by the handler that eventually executes.
    #
    # Added after declaring a contract for `data.merge_contacts` made a real
    # 2026-07-10 approval un-executable — the same defect as R-11, reintroduced
    # one capability over. Listing them here fixes it for every schema that
    # gets written later rather than for the one that happened to be noticed.
    "why", "evidence",
})


def validate_params(cap: "Capability", params: Optional[Dict[str, Any]]) -> str:
    """'' when the parameters satisfy the capability's contract, else why not.

    Two checks only, and the second is the one that did not exist anywhere:

      MISSING  a declared-required field is absent or blank. Seven of twelve
               write targets had no such guard, so the omission surfaced deep
               in the domain layer or not at all.
      UNKNOWN  a field nobody declared. NOTHING rejected these before. An LLM
               that hallucinates `{"order_id": …, "force": true}` had `force`
               carried all the way to the domain function, where an unrelated
               `p.get("force")` somewhere would silently honour it.

    Blank counts as missing: `{"order_id": ""}` is not a call that supplied an
    order id, and treating it as present is how an empty string reaches a WHERE
    clause.
    """
    if not cap.params_schema:
        return ""
    required, optional = cap.params_schema
    p = params or {}
    missing = [k for k in required
               if k not in p or p[k] is None or str(p[k]).strip() == ""]
    known = set(required) | set(optional) | _AMBIENT_PARAMS
    unknown = sorted(k for k in p if k not in known)
    parts = []
    if missing:
        parts.append(f"missing required {sorted(missing)}")
    if unknown:
        parts.append(f"unexpected {unknown} (declared: "
                     f"{sorted(set(required) | set(optional))})")
    return "; ".join(parts)


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
    # ABSENT IS NOT THE EMPTY STRING, and this line used to conflate them. The
    # `or ""` chain turned "no id was supplied" into `eid=""`, hydrate passed it
    # to `WHERE account_id=''::uuid`, PostgreSQL raised, hydrate swallowed the
    # error and returned an empty pack -- and the dispatch was recorded
    # ACCEPTED. An agent asking for context about nothing got a successful
    # answer about nothing, which is the exact shape this codebase keeps
    # finding: absence of an error read as evidence of success.
    #
    # Refused the same way _sp_account_context already refuses, so the outcome
    # is REJECTED (the request was never answerable) rather than FAILED (the
    # server broke) or ACCEPTED (it did not).
    eid = str(p.get("entity_id") or p.get("account_id")
              or p.get("lead_id") or "").strip()
    if not eid:
        return {"error": "entity_id required (or account_id / lead_id)"}
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
    correlation_id so the whole multi-agent play shares one lineage — AND its
    principal, so the play keeps its initiator.

    THE PRINCIPAL USED TO STOP HERE, and production showed it. Of 22 dispatches
    after the 2026-08-26 deploy, 20 carried a principal and 2 did not:
    `accounting.summary` and `leads.list`, both fanned out from a composite.
    They inherited `from_agent` and the correlation id and lost the one field
    that answers *who initiated this* — so a trace could follow the play and
    still not say whose play it was.

    IT IS ALSO A LATENT FUNCTIONAL BREAK, not only an audit gap. A write
    capability refuses to run without a principal. Every composite today fans
    out to reads, so the omission is invisible; the first composite that
    delegates to a write would be REJECTED in any background context, where
    there is no ambient principal for `dispatch()` to fall back on. That failure
    would arrive far from this line.
    """
    sub = A2ARequest(intent=sub_intent, from_agent=parent.from_agent or "a2a",
                     params=params or {}, correlation_id=parent.correlation_id,
                     principal=parent.principal, prose=prose)
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
               sp=_sp_campaign_winback,
               params_schema=((), ('segment', 'name', 'goal', 'proposed_by'))),
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
               sp=_sp_tuning_adjust,
               # PRESENCE AND SHAPE ONLY — the bounds stay in tuning.apply(),
               # which enforces the hard limits and band ordering against the
               # live parameter set. A range check here would be a second copy
               # of a rule with one owner, and the copy that drifts is the one
               # nobody watches. `value` is required so a missing one is
               # refused at the boundary rather than reaching float(None).
               params_schema=(("param", "value"), ("why",))),
    Capability("meeting.book", "activities", "", "write",
               lambda p: (f"book a meeting with "
                          f"{p.get('entity_type', 'lead')} "
                          f"{p.get('entity_id') or p.get('lead_id') or ''}"),
               "real meeting booking: availability-checked slot on the "
               "owner's calendar (ET business hours, preferred-hour aware), "
               "meeting activity + signed .ics invite link; invite emails "
               "only under AUTOSEND to verified addresses; undo cancels",
               sp=_sp_meeting_book,
               params_schema=((), ('start', 'duration_min', 'entity_type', 'entity_id', 'account_id', 'lead_id', 'notes', 'booked_by'))),
    Capability("kb.publish", "email", "", "write",
               lambda p: f"publish KB article: {p.get('title', '')}",
               "publish a knowledge-base article (mined from a resolved "
               "support thread, LLM-drafted, critic-checked); executes on "
               "governance approval; undo retires the article",
               sp=_sp_kb_publish,
               # knowledge.publish() already refuses a blank title/problem/
               # answer and an answer below MIN_ANSWER_CHARS, and holds the
               # source_ref idempotency. Declaring the contract here moves the
               # PRESENCE check to the boundary — so a malformed draft is
               # `rejected` before the handler runs rather than raising
               # ValueError inside it — and gives the planner a contract it can
               # read. The length rule stays where it can see the corpus.
               # `audience` is declared because knowledge.publish() now HONOURS
               # it. Declaring a field the handler ignores would be its own
               # small lie — and writing this contract is what exposed that it
               # was being ignored: the case-derived miner proposes
               # audience='internal' precisely so a fresh candidate stays away
               # from externally reachable agents, and the column default is
               # 'public'. See R-12 in knowledge.publish.
               params_schema=(("title", "problem", "answer"),
                              ("keywords", "source", "source_ref", "audience"))),
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
               sp=_sp_sms_send,
               params_schema=((), ('to', 'body', 'from', 'from_number', 'account_id', 'lead_id', 'sent_by'))),
    Capability("quote.generate", "email", "", "write",
               lambda p: (f"send a quotation to account "
                          f"{p.get('account_id', '?')} for "
                          f"{len(p.get('items') or [])} item(s)"),
               "build + deliver a priced quotation: products matched "
               "exactly/by prefix against the catalog, CURRENT retail "
               "pricing, deterministic totals (LLM never touches a number), "
               "30-day validity; emailed only under AUTOSEND to a verified "
               "address, otherwise drafted as an owner task; CASL-compliant",
               sp=_sp_quote_generate,
               params_schema=((), ('account_id', 'opportunity_id', 'items', 'discount_pct'))),
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
               sp=_sp_contact_update_profile,
               params_schema=(('contact_id', 'field', 'new_value'), ('channel', 'verified_via', 'call_sid'))),
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
               sp=_sp_order_cancel,
               params_schema=(('order_id',), ('reason', 'reason_detail', 'channel', 'verified_via', 'call_sid', 'updated_by', 'order_number'))),
    Capability("scoring.activate", "leads", "", "write",
               lambda p: f"activate lead-scoring model v{p.get('version', '?')}",
               "make a trained predictive lead-scoring candidate the active "
               "model (trained weekly on settled leads, proposed with holdout "
               "evidence, executes on governance approval, undo restores the "
               "previous version)",
               sp=_sp_scoring_activate,
               params_schema=((), ('version',))),
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
               sp=_sp_dq_merge_contacts,
               # No required field: the handler selects its own duplicate set
               # and takes no identifier. `limit` is the only knob, and it is
               # already clamped to FIX_LIMIT inside merge_contacts_sp — so the
               # value stays bounded there and the contract only says the field
               # exists. Declaring the empty contract still buys the
               # unexpected-key rejection, which is what stops an LLM handing
               # this capability a stray `force` or `account_id` it would
               # silently ignore.
               params_schema=((), ("limit",))),
    Capability("data.erase_record", "lifecycle", "", "write",
               lambda p: f"erase personal data for {p.get('entity','record')} "
                         f"{str(p.get('record_id',''))[:8]}",
               "data lifecycle: honour an erasure request — delete the personal "
               "satellites (custom fields, AI memories, transcripts, identity "
               "links), de-link activity history, and redact the core record, "
               "while RETAINING financial, suppression and audit records by "
               "policy. IRREVERSIBLE — there is no undo",
               sp=_sp_data_erase_record,
               # THE ONE WHERE THE BOUNDARY MATTERS MOST. There is no undo
               # handler for this action by design, so a malformed request must
               # be refused BEFORE the handler opens its transaction — not
               # discovered by `erase_sp` raising LifecycleError partway in.
               # Both fields are required: `_plan(entity)` decides what gets
               # deleted, de-linked and retained, so an absent or blank entity
               # is not a call this capability can safely interpret.
               params_schema=(("entity", "record_id"), ())),
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
    # party_id is REQUIRED and must be non-blank. Production recorded this
    # dispatch as FAILED with `invalid input syntax for type uuid: ""` — the
    # planner omitted the party, `_sp_select_channel`'s `or ""` turned that into
    # an empty string, and the empty string reached
    # `WHERE contact_id=%s::uuid`. A request that names no party cannot be
    # answered, and saying so is a REJECTION, not a server failure.
    Capability("comms.select_channel", "orchestrator", "", "read",
               lambda p: (f"select channel for {p.get('objective','?')} to "
                          f"{p.get('party_type','contact')} {p.get('party_id','')}"),
               "pick the best channel/action for an objective + party (intent + "
               "identity + urgency + learned preference + authorization) — decides, "
               "never sends; the vision's 'what's the best way to accomplish this?'",
               sp=_sp_select_channel,
               params_schema=(("party_id",),
                              ("objective", "party_type", "urgency",
                               "sensitive", "entity_id"))),
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


def sync_capability_registry(actor: str = "startup") -> Dict[str, Any]:
    """Seed the operator control plane from the code's own manifest.

    CAPABILITIES is authoritative — it is what `dispatch` resolves against. A
    hand-written SQL seed would be a second copy of that manifest, and the copy
    that drifts is always the one nobody is watching. Same reasoning as
    `notification_tier_rules`.

    `allowed_callers` IS SEEDED NULL — UNRESTRICTED — AND THAT IS DELIBERATE.

    The first version of this function seeded it as {owning agent, orchestrator,
    system}, which looks prudent and is wrong: `cap.agent` names who IMPLEMENTS
    a capability, not who may CALL it. `accounting` legitimately dispatches
    `email.send_payment_reminder` — cross-agent delegation is the entire point
    of the mesh — and that seed refused it. Fourteen tests caught it.

    The lesson generalises: an RBAC policy cannot be DERIVED from the manifest,
    because the manifest describes ownership and the policy describes traffic.
    Guessing one produces a control that breaks real work, which is how controls
    get switched off. So this seeds the half that IS derivable — every capability
    registered, which makes the closed-by-default gate and the `enabled` kill
    switch real — and leaves the half that is not to an operator, who can narrow
    it against `observed_callers()` rather than against a guess.

    Only INSERTS. An operator who disabled a capability or narrowed its callers
    must not have that decision reverted by the next deploy — so an existing row
    is left exactly as it is, and only genuinely new intents are added.
    """
    import json as _json
    from app.core.database import get_connection
    added, existing = 0, 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for cap in CAPABILITIES.values():
                cur.execute(
                    """INSERT INTO capability_registry
                         (intent, enabled, notes, updated_by, allowed_callers)
                       VALUES (%s, true, %s, %s, NULL)
                       ON CONFLICT (intent) DO NOTHING""",
                    (cap.intent, f"{cap.kind} · owned by {cap.agent}", actor))
                if cur.rowcount:
                    added += 1
                else:
                    existing += 1
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[a2a] registry sync failed "
                       f"(apply sql/a2a_outcome_and_principal.sql?): {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()
    _reg_cache["at"] = 0.0        # force the TTL cache to re-read on next gate
    logger.info(f"[a2a] capability registry: {added} added, {existing} already "
                f"present, {len(CAPABILITIES)} declared")
    return {"ok": True, "added": added, "existing": existing,
            "declared": len(CAPABILITIES)}


def observed_callers(days: int = 90) -> Dict[str, List[str]]:
    """intent → the agents that have ACTUALLY dispatched it, from the trace.

    The evidence an operator needs before narrowing `allowed_callers`, and the
    reason this function exists at all: seeding that policy from the manifest
    guessed wrong and refused a legitimate cross-agent call. Ownership is
    declared in code; TRAFFIC is only knowable by looking.

    Reads accepted dispatches only. A refused call is not evidence that a caller
    is legitimate — including, circularly, one refused by an earlier version of
    this very policy.
    """
    out: Dict[str, List[str]] = {}
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT intent, from_agent, count(*)
                         FROM a2a_dispatches
                        WHERE at > now() - make_interval(days => %s)
                          AND (outcome = 'accepted' OR (outcome IS NULL AND ok))
                        GROUP BY 1, 2 ORDER BY 1, 3 DESC""", (int(days),))
                for intent, agent, _n in cur.fetchall():
                    out.setdefault(intent, []).append(agent)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[a2a] observed_callers unavailable: {exc}")
    return out


def registry_state() -> Dict[str, Any]:
    """What the operator control plane currently says — the observable half of
    the guardrail. A gate nobody can inspect is a gate nobody trusts."""
    rows = _registry_rows()
    declared = set(CAPABILITIES)
    seeded = set(rows)
    return {
        "seeded": bool(rows),
        "declared": len(declared),
        "registered": len(seeded),
        "unregistered": sorted(declared - seeded),   # would be REFUSED
        "orphaned": sorted(seeded - declared),       # row with no capability
        "disabled": sorted(i for i, r in rows.items() if not r.get("enabled", True)),
        "closed_by_default": bool(rows),
    }


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
                         from_agent, agent, kind, ok, outcome, principal,
                         error, latency_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (res.correlation_id, req.intent, req.from_agent,
                     res.agent, cap.kind if cap else None, res.ok,
                     # THE FIX. `ok` answers "did it work"; `outcome` answers
                     # "and if not, in which of the three ways". This function
                     # previously wrote only the boolean — so the trace could
                     # not tell an authorization refusal from a server error,
                     # and an operator reading it might retry a refusal.
                     res.outcome or None,
                     str(req.principal) if req.principal else None,
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
    # WHO, inherited before anything else runs. A caller that constructed its
    # request without a principal gets the one stamped at the authenticated
    # boundary — which is how ~40 existing call sites acquire identity without
    # being rewritten. An explicitly supplied principal always wins: a
    # background job saying `Principal.service("agent-bus")` must not be
    # silently re-attributed to whichever request happened to be in flight.
    if req.principal is None:
        req.principal = current_principal()
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
        return A2AResult(False, req.intent, "none", cid, outcome=REJECTED,
                         error=f"No capability registered for intent '{req.intent}'"
                               + (f". Did you mean: {', '.join(sugg)}?" if sugg else ""),
                         data={"suggestions": sugg} if sugg else None)

    # ── Registry-as-data gate — CLOSED BY DEFAULT once the registry is seeded ─
    #
    # This gate was `reg.get("enabled", True)` against a table holding ZERO rows
    # on both local and Railway. A missing row permitted everything, so agent
    # RBAC and the operator kill-switch had never fired in production. A control
    # that has never fired is a control nobody has tested.
    #
    # Now: a seeded registry that does not name this intent REFUSES it. The
    # registry is populated from CAPABILITIES itself (sync_capability_registry),
    # so the only way to be missing is to be genuinely unregistered.
    #
    # The empty-registry case is the one deliberate exception, and it is NOT
    # fail-open dressed up: an unseeded database cannot distinguish "nothing is
    # permitted" from "nobody has run the seed", and refusing every capability
    # on a fresh checkout would make the failure mode of a missing migration a
    # total outage. It logs loudly and `release_guard` reports it, so the state
    # is visible rather than silent.
    _reg_all = _registry_rows()
    reg = _reg_all.get(req.intent) or {}
    if _reg_all and req.intent not in _reg_all:
        logger.warning(f"[a2a] '{req.intent}' is not in the capability registry "
                       f"— refused (closed by default)")
        return A2AResult(False, req.intent, cap.agent, cid, outcome=REJECTED,
                         error=f"capability '{req.intent}' is not registered in "
                               f"the capability registry — refused. Run "
                               f"POST /a2a/registry/sync if this is a new "
                               f"capability.")
    if not reg.get("enabled", True):
        notes = reg.get("notes")
        return A2AResult(False, req.intent, cap.agent, cid, outcome=REJECTED,
                         error=f"capability '{req.intent}' is disabled in the "
                               f"capability registry"
                               + (f" — {notes}" if notes else ""))

    # Agent RBAC (guardrail layer 4): allowed_callers restricts WHO may
    # dispatch this capability. Approved executions (govern_bypass) pass —
    # the human approval is the authority.
    allowed = reg.get("allowed_callers")
    if (isinstance(allowed, list) and allowed
            and req.from_agent not in allowed and not req.govern_bypass):
        return A2AResult(False, req.intent, cap.agent, cid, outcome=REJECTED,
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

    # ── WRITE PRECONDITIONS — identity, then parameters, then governance ─────
    #
    # Ordered cheapest-and-most-certain first, and deliberately BEFORE the
    # governance confidence gate: a write with no named initiator or a malformed
    # parameter set should be refused on its own terms, not queued for an
    # executive to approve. Approving a proposal whose caller is unknown is
    # exactly the rubber-stamp this platform's governance exists to avoid.
    if cap.kind == "write" and not dry_run:
        # WHO. `from_agent` names the component, never the authority. A write
        # with no principal is refused rather than attributed to "system" —
        # background work is not anonymous work, and Principal.service() exists
        # so an unattended write says which unattended thing did it.
        if req.principal is None:
            return A2AResult(False, req.intent, cap.agent, cid, outcome=REJECTED,
                             error=f"write capability '{req.intent}' requires a "
                                   f"principal; none was supplied and none is in "
                                   f"request context. Background callers must "
                                   f"pass Principal.service('<name>').")
    # WHAT. Required fields present, no unexpected fields. Deliberately NOT
    # types, ranges or business rules — those belong in the SP and in the SQL
    # predicate, where they are enforced against the committed row.
    #
    # THIS RUNS FOR ANY CAPABILITY THAT DECLARES A SCHEMA, not only writes, and
    # production is why. `comms.select_channel` is a READ, so it skipped this
    # gate entirely; the planner dispatched it with no party_id, an upstream
    # `or ""` turned absent into an empty string, and PostgreSQL was handed
    # `WHERE contact_id=''::uuid`. The dispatch was recorded FAILED — a server
    # error — when the truth was that the request was never valid.
    #
    # Opt-in, so nothing changes for the capabilities that declare no schema:
    # `params_schema` is None for all of them and this block is skipped. Reads
    # are not being made to validate by default; the ones whose parameters
    # actually matter can now say so.
    if cap.params_schema and not dry_run:
        bad = validate_params(cap, req.params)
        if bad:
            return A2AResult(False, req.intent, cap.agent, cid,
                             outcome=REJECTED,
                             error=f"invalid parameters for '{req.intent}': "
                                   f"{bad}")

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
    _policy_exec = None          # set when a declared policy authorises execution
    if cap.kind == "write" and not req.govern_bypass:
        from app.core import governance
        from app.core import governance_policy as gp
        if governance.ENABLED:
            # ACTIVATION (docs/governance/activation_plan.md §9–§10). The
            # decision is read from the action's POLICY ROW, never from the
            # confidence score. Confidence can only REFUSE (below propose_min a
            # write is not even worth an executive's time); it can never grant.
            pol = gp.policy_for(req.intent)
            auto, why = gp.may_auto_execute(req.intent, pol)
            # HITL amount floor (guardrail layer 1) still overrides a standing
            # policy: how much is at stake is a different question from whether
            # the class is routinely safe.
            if auto:
                hitl = governance.hitl_amount()
                amt = governance._amount_from(dict(req.params))
                if hitl > 0 and amt >= hitl:
                    auto, why = False, f"amount ${amt:,.0f} ≥ HITL floor ${hitl:,.0f}"
                    logger.info(f"[a2a] {req.intent} {why} → forced propose")
            d = "act" if auto else "propose"
            if float(req.confidence or 0) < governance.propose_min():
                d = "skip"
            if d == "skip":
                return A2AResult(False, req.intent, cap.agent, cid,
                                 outcome=REJECTED,
                                 error=f"skipped by governance — confidence "
                                       f"{req.confidence} < {governance.propose_min()}")
            if d == "act":
                _policy_exec = pol
                logger.info(f"[a2a] {req.intent} executes under {why}")
            if d == "propose":
                # _correlation_id ties the approval to this play's trace. It is
                # hidden from the approval UI and removed again by
                # `governance.strip_internal` before the approved action is
                # re-dispatched — this comment previously claimed the removal
                # happened and it did not, so every capability with a
                # params_schema refused its own approved execution.
                p = dict(req.params)
                p["_correlation_id"] = cid
                try:
                    aid = governance.propose(
                        req.intent, req.from_agent, p,
                        req.entity.type if req.entity else None,
                        req.entity.id if req.entity else None, req.confidence)
                except governance.ProposalCapReached as capped:
                    # A POLICY REFUSAL, and therefore a REJECTED result rather
                    # than an exception. dispatch() promises callers a
                    # structured outcome for every governed decision; letting
                    # this escape would turn "the CRO has already had their
                    # three for today" into a 500 several layers away, which is
                    # exactly the mislabelling the outcome column exists to
                    # stop. Nothing was written and nothing executed.
                    logger.info(f"[a2a] {req.intent} refused — {capped}")
                    return A2AResult(False, req.intent, cap.agent, cid,
                                     outcome=REJECTED, error=str(capped),
                                     data={"status": "deferred_by_cap",
                                           "daily_cap": capped.cap,
                                           "authority_role": capped.approver})
                logger.info(f"[a2a] {req.intent} gated → proposed {aid[:8]} "
                            f"(conf={req.confidence}; {why})")
                return A2AResult(True, req.intent, cap.agent, cid,
                                 output=f"proposed for approval — {why} "
                                        f"(confidence {req.confidence})",
                                 data={"status": "pending_approval", "approval_uuid": aid,
                                       "decision_mode": pol.get("decision_mode"),
                                       "authority_role": pol.get("approver_role"),
                                       "sla_hours": pol.get("sla_hours")})
            # d == "act" → fall through and execute under the declared policy

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
        if _policy_exec is not None:
            _ledger_policy_execution(req, cid, _policy_exec, data)
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
    elif _policy_exec is not None:
        _ledger_policy_execution(req, cid, _policy_exec,
                                 resp.get("result") if isinstance(resp, dict) else None)
    return A2AResult(
        ok=(outcome == ACCEPTED),
        intent=req.intent, agent=cap.agent, correlation_id=cid,
        data=resp.get("records") if "records" in resp else resp.get("result"),
        output=str(resp.get("output", ""))[:4000],
        error=resp.get("error") or (None if outcome == ACCEPTED
                                    else f"{outcome} (HTTP {status})"),
        raw=resp, outcome=outcome, status=status,
    )


def _ledger_policy_execution(req: "A2ARequest", cid: str, pol: Dict[str, Any],
                             data: Any) -> None:
    """A write that executed under a declared AUTO_EXECUTE / SAMPLED_REVIEW
    policy is ledgered in action_approvals with decided_by='policy:<owner>' —
    the technical decider is the policy, the accountable human is its owner
    (activation §10, §16). SAMPLED_REVIEW additionally raises a review work
    item for a `sample_rate` share of executions, owned by the approver role.
    Best-effort: the customer-visible action already happened."""
    try:
        from app.core import governance
        owner = pol.get("policy_owner") or pol.get("approver_role") or "unowned"
        result = data if isinstance(data, dict) else {"data": data}
        aid = governance.record_preauthorized(
            req.intent, req.from_agent, f"{owner}:{req.intent}",
            {**{k: v for k, v in dict(req.params).items() if not k.startswith("_")},
             "_correlation_id": cid,
             "principal": str(req.principal) if req.principal else None},
            {"ok": True, **{k: v for k, v in result.items() if k != "ok"}} if isinstance(result, dict) else {"ok": True},
            entity_type=req.entity.type if req.entity else None,
            entity_id=req.entity.id if req.entity else None)
        if pol.get("decision_mode") == "SAMPLED_REVIEW":
            rate = float(pol.get("sample_rate") or 0)
            if rate > 0 and random.random() < rate:
                from app.core import governance_alerts
                governance_alerts.open_alert(
                    "sampled_review",
                    f"Sampled review: {req.intent} executed under policy "
                    f"({owner}) — confirm it was right",
                    rule=None, severity="low", source="a2a",
                    affected_type="approval", affected_id=aid,
                    detail={"intent": req.intent, "params": dict(req.params),
                            "correlation_id": cid, "sample_rate": rate},
                    owner_role=pol.get("approver_role"), correlation_id=cid)
    except Exception as exc:                                       # noqa: BLE001
        logger.warning(f"[a2a] policy execution ledger skipped for {req.intent}: {exc}")


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


# ROUTE ORDER MATTERS. These two must stay ABOVE "/a2a/registry/{intent}":
# Starlette matches in declaration order, so a parameterised route declared
# first captures "/a2a/registry/sync" as intent="sync" — answering 422 for the
# body that handler requires, or "unknown capability" with a 200 if one is
# sent. Either way the endpoint looks present and does nothing.
@router.post("/a2a/registry/sync")
def a2a_registry_sync():
    """Seed the capability registry from the code's own manifest.

    THE GAP THIS CLOSES. The registry gate is closed-by-default — a seeded
    registry that does not name an intent refuses it — but nothing could
    perform the seed on a deployed environment. It is not called at startup,
    the rows come from `CAPABILITIES` (which lives in the application, so SQL
    cannot produce them), and the migration's own output instructed operators
    to call this endpoint, which did not exist. The control was armed on one
    laptop and unreachable everywhere else.

    Idempotent and non-destructive: INSERT … ON CONFLICT DO NOTHING, so a
    disabled capability stays disabled and a narrowed `allowed_callers` stays
    narrowed. It adds genuinely new intents and nothing else.

    NOTE FOR A RUNBOOK: sync is not a reset. Restoring a registry an operator
    has edited means DELETE then sync — calling this alone will not undo their
    changes, which is the point.
    """
    return {**sync_capability_registry("api"), "state": registry_state()}


@router.get("/a2a/registry/observed-callers")
def a2a_observed_callers(days: int = 90):
    """intent → the agents that have actually dispatched it.

    The evidence an operator needs before narrowing `allowed_callers`. Seeding
    that policy from the manifest guessed wrong once and refused a legitimate
    cross-agent call: ownership is declared in code, traffic is only knowable
    by looking. Reads accepted dispatches only."""
    return {"days": days, "callers": observed_callers(days)}


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
