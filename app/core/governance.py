"""Phase 5 — Governance: confidence-gating + approval queue.

Makes the autonomy safe to turn on. Every WRITE/outbound A2A action is gated by
its confidence:

    confidence >= GOV_ACT_MIN      → ACT      (execute now)
    GOV_PROPOSE_MIN <= c < ACT_MIN → PROPOSE  (queue for human approval)
    c < GOV_PROPOSE_MIN            → SKIP     (don't act)

Proposed actions land in `action_approvals` (pending). A human approves/rejects
via the endpoints; approving re-dispatches the action through A2A (gate bypassed)
and records the result. Every proposal and decision is audited in the table.

Reads are never gated. Gating only engages when GOV_ENABLED=1 — otherwise writes
execute exactly as before (additive, opt-in).

CONFIG (env)
  GOV_ENABLED       0     master on/off (gating no-ops when 0)
  GOV_ACT_MIN       0.8   confidence at/above which a write auto-executes
  GOV_PROPOSE_MIN   0.5   confidence at/above which a write is queued (else skip)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.core.database import get_connection

logger = logging.getLogger("governance")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


ENABLED = _flag("GOV_ENABLED")
ACT_MIN = _float("GOV_ACT_MIN", 0.8)
PROPOSE_MIN = _float("GOV_PROPOSE_MIN", 0.5)


# ============================================================================
# POLICY-AS-DATA (audit #4) — guardrail numbers become editable rows
# ============================================================================
# A governance_policies row overrides the env/code default at runtime — no
# deploy, no restart, fully audited (updated_by/updated_at). No row (or no
# table, or any DB failure) = the default applies: the guardrails can be
# TUNED from data but never LOST to it. Only whitelisted keys are accepted.

_KNOWN_POLICIES: Dict[str, Dict[str, Any]] = {
    "gov.act_min": {
        "description": "confidence at/above which a governed write auto-executes",
        "min": 0.0, "max": 1.0, "default": lambda: ACT_MIN},
    "gov.propose_min": {
        "description": "confidence at/above which a governed write queues for "
                       "approval (below = skip)",
        "min": 0.0, "max": 1.0, "default": lambda: PROPOSE_MIN},
    "planner.max_steps": {
        "description": "hard cap on steps in a bounded plan",
        "min": 1, "max": 12, "int": True, "default": lambda: 6},
    "planner.max_writes": {
        "description": "hard cap on WRITE steps per plan",
        "min": 0, "max": 6, "int": True, "default": lambda: 2},
    "gov.hitl_amount": {
        "description": "human-in-the-loop floor: a governed write whose params "
                       "carry an amount at/above this ALWAYS queues for "
                       "approval, regardless of confidence (0 = off)",
        "min": 0, "max": 10_000_000,
        "default": lambda: _float("GOV_HITL_AMOUNT", 1000.0)},
    "brand.max_discount_pct": {
        "description": "deterministic brand boundary: the largest discount any "
                       "agent-built quote may carry — requests above it are "
                       "clamped, never sent",
        "min": 0, "max": 100,
        "default": lambda: _float("BRAND_MAX_DISCOUNT_PCT", 15.0)},
}

_POL_TTL = int(os.getenv("POLICY_TTL_SECS", "30"))
_pol_cache: Dict[str, Any] = {"at": 0.0, "vals": {}}


def _policy_rows() -> Dict[str, Any]:
    """Cached {policy_key: value} from governance_policies ({} on failure)."""
    if time.time() - _pol_cache["at"] < _POL_TTL:
        return _pol_cache["vals"]
    vals: Dict[str, Any] = {}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT policy_key, value FROM governance_policies")
                for k, v in cur.fetchall():
                    vals[k] = v
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[governance] policy table skipped: {exc}")
    _pol_cache.update(at=time.time(), vals=vals)
    return vals


def invalidate_policy_cache() -> None:
    _pol_cache["at"] = 0.0


def policy_value(key: str, default: Optional[float] = None) -> float:
    """Effective numeric value for a policy key: DB row if present and valid,
    else the caller's default, else the registered default."""
    spec = _KNOWN_POLICIES.get(key) or {}
    fallback = default if default is not None else (
        spec["default"]() if spec.get("default") else 0.0)
    raw = _policy_rows().get(key)
    if raw is None:
        return fallback
    try:
        v = float(raw)
        if not (spec.get("min", v) <= v <= spec.get("max", v)):
            return fallback
        return int(v) if spec.get("int") else v
    except (TypeError, ValueError):
        return fallback


def act_min() -> float:
    return policy_value("gov.act_min")


def propose_min() -> float:
    return policy_value("gov.propose_min")


def hitl_amount() -> float:
    return policy_value("gov.hitl_amount")


# ============================================================================
# Policy
# ============================================================================

def decide(confidence: float) -> str:
    """'act' | 'propose' | 'skip' from a confidence score. Thresholds are the
    LIVE policy values (governance_policies row overrides the env default)."""
    c = float(confidence or 0)
    if c >= act_min():
        return "act"
    if c >= propose_min():
        return "propose"
    return "skip"


# ============================================================================
# Queue
# ============================================================================

def propose(action_type: str, proposed_by: str, params: Dict[str, Any],
            entity_type: Optional[str] = None, entity_id: Optional[str] = None,
            confidence: float = 0.0, severity: Optional[str] = None,
            ttl_hours: Optional[int] = 72) -> str:
    """Enqueue a pending action for human approval. Returns approval_uuid."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO action_approvals
                     (action_type, proposed_by, entity_type, entity_id, params,
                      confidence, severity, expires_at)
                   VALUES (%(at)s,%(by)s,%(et)s,%(eid)s,%(p)s::jsonb,%(cf)s,%(sev)s,
                           CASE WHEN %(ttl)s IS NULL THEN NULL
                                ELSE now() + (%(ttl)s||' hours')::interval END)
                   RETURNING approval_uuid""",
                {"at": action_type, "by": proposed_by, "et": entity_type,
                 "eid": str(entity_id) if entity_id else None,
                 "p": json.dumps(params or {}), "cf": confidence, "sev": severity,
                 "ttl": ttl_hours})
            aid = str(cur.fetchone()[0])
        conn.commit()
        logger.info(f"[governance] proposed {action_type} by {proposed_by} "
                    f"(conf={confidence}) → {aid[:8]}")
        # Independent critique FIRST (best-effort) so the routed notification
        # and email carry the second opinion alongside the proposal.
        critique = None
        try:
            from app.core import critic
            critique = critic.review(aid, action_type, params or {},
                                     entity_type, entity_id)
        except Exception as exc:
            logger.warning(f"[governance] critique skipped for {aid[:8]}: {exc}")
        # Critic→revise loop: when the critic OBJECTS to a revisable draft,
        # the drafting side gets the findings back for ONE bounded revision,
        # then the critic re-reviews. The human always sees the final state.
        try:
            params, critique = _revise_cycle(aid, action_type, params or {},
                                             entity_type, entity_id, critique)
        except Exception as exc:
            logger.warning(f"[governance] revise cycle skipped for {aid[:8]}: {exc}")
        # Route to the right decision-maker (best-effort: tolerates the
        # governance_routing migration not being applied yet).
        try:
            route_approval(aid, action_type, params or {}, critique=critique)
        except Exception as exc:
            logger.warning(f"[governance] routing skipped for {aid[:8]}: {exc}")
        return aid
    finally:
        conn.close()


def record_preauthorized(action_type: str, performed_by: str, policy: str,
                         params: Dict[str, Any], result: Dict[str, Any],
                         entity_type: Optional[str] = None,
                         entity_id: Optional[str] = None,
                         performed_at: Optional[Any] = None) -> Optional[str]:
    """Ledger an action that ALREADY HAPPENED under a standing policy.

    WHY THIS IS NOT propose()+approve(). A pending row that a confidence score
    rubber-stamps is governance theatre: the queue shows an item nobody will
    action, and the audit row then claims a human decided when none did. This
    writes the row directly in the terminal 'executed' state, with
    decided_by='policy:<name>', so:

        • it never appears in pending() — the human queue stays a list of things
          a human must actually do (every existing consumer of action_approvals
          filters status='pending', so nothing else miscounts either);
        • no record ever asserts a human authorised it;
        • undo() still works — it requires an 'executed' row plus a registered
          handler, which is the whole reason this lives in action_approvals
          rather than a separate log table.

    The authority is the POLICY, decided once by a human at design time and
    enforced by a deterministic gate (for order.cancel: an OTP round-trip plus a
    status predicate inside the UPDATE itself). This function only records what
    that gate already permitted — it decides nothing.

    Best-effort by construction: returns None on failure. A ledger write must
    never roll back the customer-visible action it describes.
    """
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO action_approvals
                         (action_type, proposed_by, entity_type, entity_id,
                          params, confidence, status, decided_by, decided_at,
                          decision_reason, result, executed_at, expires_at)
                       VALUES (%(at)s,%(by)s,%(et)s,%(eid)s,%(p)s::jsonb,1.0,
                               'executed', %(db)s, COALESCE(%(ts)s, now()),
                               %(why)s, %(r)s::jsonb, COALESCE(%(ts)s, now()),
                               NULL)
                       RETURNING approval_uuid""",
                    {"at": action_type, "by": performed_by,
                     "et": entity_type,
                     "eid": str(entity_id) if entity_id else None,
                     "p": json.dumps(params or {}),
                     "db": f"policy:{policy}",
                     "ts": performed_at,
                     "why": f"pre-authorized by standing policy '{policy}' — "
                            f"executed before this record was written",
                     "r": json.dumps(result or {})})
                aid = str(cur.fetchone()[0])
            conn.commit()
        finally:
            conn.close()
        logger.info(f"[governance] ledgered pre-authorized {action_type} "
                    f"under policy:{policy} → {aid[:8]}")
        return aid
    except Exception as exc:                              # noqa: BLE001
        logger.error(f"[governance] could not ledger pre-authorized "
                     f"{action_type}: {exc}")
        return None


# ============================================================================
# Critic→revise loop — one bounded self-correction before the human sees it
# ============================================================================

def _revise_kb_publish(params: Dict[str, Any],
                       findings: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    from app.core import knowledge
    return knowledge.revise_article(params, findings)


def _revise_meeting_book(params: Dict[str, Any],
                         findings: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Deterministic revision: a conflicting/unparseable requested slot is
    dropped — the booking engine then auto-picks the first free slot."""
    slot_failed = any(f.get("check") == "slot_free" and f.get("verdict") == "fail"
                      for f in findings or [])
    if slot_failed and params.get("start"):
        revised = dict(params)
        revised.pop("start", None)
        return revised
    return None


# action_type → reviser(params, critic findings) -> revised params or None.
# Only DRAFT-shaped actions belong here — a revision must never change what
# the human thinks they are approving in kind, only fix the flagged defects.
_REVISERS = {
    "kb.publish": _revise_kb_publish,
    "meeting.book": _revise_meeting_book,
}


def _revise_cycle(aid: str, action_type: str, params: Dict[str, Any],
                  entity_type: Optional[str], entity_id: Optional[str],
                  critique: Optional[Dict[str, Any]]):
    """If the critic objected and a reviser exists: revise ONCE, persist the
    new params, re-critique, and annotate the outcome. Returns the final
    (params, critique) — unchanged when no revision applies."""
    if not critique or critique.get("stance") != "object":
        return params, critique
    reviser = _REVISERS.get(action_type)
    if not reviser or params.get("_revised"):        # one iteration, ever
        return params, critique
    revised = reviser(dict(params), critique.get("findings") or [])
    if not revised:
        return params, critique
    revised["_revised"] = True
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE action_approvals SET params=%s::jsonb "
                        "WHERE approval_uuid=%s::uuid",
                        (json.dumps(revised), aid))
        conn.commit()
    finally:
        conn.close()
    from app.core import critic
    new_critique = critic.review(aid, action_type, revised,
                                 entity_type, entity_id)
    new_critique["revision"] = {
        "attempted": True,
        "improved": new_critique.get("stance") != "object",
        "previous_stance": "object",
        "fixed": [f.get("check") for f in (critique.get("findings") or [])
                  if f.get("verdict") == "fail"],
    }
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE action_approvals SET critique=%s::jsonb "
                        "WHERE approval_uuid=%s::uuid",
                        (json.dumps(new_critique), aid))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug(f"[governance] revision annotation skipped: {exc}")
    logger.info(f"[governance] {action_type} {aid[:8]} revised after critic "
                f"objection → {new_critique.get('stance')}")
    return revised, new_critique


# ============================================================================
# Executive routing — who decides this approval?
# ============================================================================

GOV_ROUTE_EMAIL = _flag("GOV_ROUTE_EMAIL")   # 1 = email the assigned executive

_AMOUNT_KEYS = ("amount", "total_amount", "total", "value", "balance",
                "computed_balance_due", "ltv")

# action_type keyword → executive role that owns the call
_ROLE_AFFINITY = (
    (("invoice", "payment", "dunning", "refund", "credit", "write_off", "ar"), "CFO"),
    (("opportunity", "deal", "discount", "campaign", "winback", "lead", "email"), "CRO"),
    (("order", "inventory", "shipment", "stock", "activity"), "COO"),
)


def _amount_from(params: Dict[str, Any]) -> float:
    for k in _AMOUNT_KEYS:
        v = (params or {}).get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _affinity_role(action_type: str) -> str:
    at = (action_type or "").lower()
    for keywords, role in _ROLE_AFFINITY:
        if any(k in at for k in keywords):
            return role
    return "CEO"


_STANCE_ICON = {"endorse": "✅", "caution": "⚠️", "object": "⛔"}


def _critic_line(critique: Optional[Dict[str, Any]]) -> str:
    """One human line summarizing the critic's verdict ('' when absent)."""
    if not critique:
        return ""
    icon = _STANCE_ICON.get(critique.get("stance", ""), "•")
    return (f"{icon} Critic ({critique.get('stance', '?').upper()}): "
            f"{critique.get('summary', '')}")


# Internal/bookkeeping params not worth showing the approver.
_HIDE_PARAMS = {"_revised", "_correlation_id", "plan_correlation_id",
                "plan_goal", "govern_bypass",
                "confidence"}

# Plain-language description of what each action DOES — shown when the params
# alone don't convey it (e.g. planner-proposed batch actions carry only a goal).
_ACTION_DESC = {
    "supervisor.emit_dunning": "Start the overdue-invoice dunning loop — drafts "
        "payment reminders for materially overdue invoices.",
    "supervisor.emit_hot_leads": "Start hot-lead outreach — auto-schedules calls "
        "for high-scoring leads.",
    "email.send_payment_reminder": "Send an overdue-invoice payment reminder email.",
    "data.normalize_phones": "Normalize contact/lead phone numbers to E.164 "
        "(capped batch, undoable).",
    "data.merge_contacts": "Merge exact-duplicate contacts into the oldest (undoable).",
    "data.erase_record": "Erase a record's personal data — deletes custom fields, AI "
        "memories, transcripts and identity links, de-links activity history and "
        "redacts the core record; financial, suppression and audit records are "
        "retained. CANNOT BE UNDONE.",
    "identity.materialize_link": "Physically merge a confirmed duplicate record into "
        "its primary — dependent records are re-pointed and the duplicate is "
        "soft-deleted (undoable).",
    "campaign.winback": "Create and launch a win-back marketing campaign.",
    "kb.publish": "Publish a knowledge-base article.",
    "tuning.adjust": "Change a governed model tuning parameter.",
    "scoring.activate": "Activate a trained lead-scoring model version.",
    "meeting.book": "Book a meeting on the owner's calendar.",
    "quote.generate": "Build and send a priced quotation.",
    "contact.update_profile": "Update a contact's profile field.",
    "order.cancel": "Cancel an order at a verified customer's request.",
}


def _summary_rows(action_type: str, params: Optional[Dict[str, Any]]):
    """The '(label, value)' rows describing WHAT an action does — the single
    source of truth shared by the email, the in-app notification, AND the
    governance queue UI. Never empty: falls back to an action description, then
    the goal it serves, then the action name."""
    p = params or {}

    def g(*keys, default=""):
        for k in keys:
            v = p.get(k)
            if v not in (None, "", []):
                return v
        return default

    at = (action_type or "").lower()
    rows: List = []
    if at == "kb.publish":
        rows = [("Title", str(g("title"))),
                ("Question", str(g("problem"))[:400]),
                ("Answer", str(g("answer"))[:600])]
        if p.get("keywords"):
            rows.append(("Keywords", ", ".join(map(str, p["keywords"]))[:200]))
    elif at == "email.send_payment_reminder":
        rows = [("To", str(g("to"))),
                ("Invoice", str(g("invoice_number", "invoice_id"))),
                ("Amount", str(g("amount"))),
                ("Days overdue", str(g("days_overdue")))]
    elif at == "campaign.winback":
        rows = [("Goal", str(g("goal")))]
        seg = g("segment")
        if seg:
            rows.append(("Segment", seg if isinstance(seg, str)
                         else json.dumps(seg, default=str)[:200]))
    elif at == "tuning.adjust":
        rows = [("Parameter", str(g("param"))), ("New value", str(g("value"))),
                ("Why", str(g("why", "reason")))]
    elif at == "contact.update_profile":
        rows = [("Contact", str(g("contact_id"))), ("Field", str(g("field"))),
                ("New value", str(g("new_value")))]
    elif at == "meeting.book":
        rows = [("With", f"{g('entity_type', 'lead')} "
                         f"{g('entity_id', 'lead_id', 'contact_id')}".strip()),
                ("When", str(g("start", "when", default="first available slot")))]
    elif at == "quote.generate":
        rows = [("Account", str(g("account_id"))),
                ("Line items", str(len(p.get("items") or [])))]
    elif at == "scoring.activate":
        rows = [("Model version", str(g("version")))]

    rows = [(k, v) for k, v in rows if v not in ("", "None")]
    # No specific fields (e.g. a planner-proposed batch action) → describe WHAT
    # the action does, then list any other visible params.
    if not rows:
        desc = _ACTION_DESC.get(at)
        if desc:
            rows.append(("Action", desc))
        for k, v in p.items():
            if k in _HIDE_PARAMS or str(k).startswith("_"):
                continue
            sv = json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
            rows.append((str(k).replace("_", " ").capitalize(), sv[:200]))
    # Planner-proposed actions carry the goal they serve — real approver context.
    goal = p.get("plan_goal")
    if goal:
        rows.append(("Proposed to accomplish", str(goal)[:200]))
    rows = [(k, v) for k, v in rows if v not in ("", "None")]
    if not rows:                    # never empty — at least name the action
        rows = [("Action", _ACTION_DESC.get(at, action_type))]
    return rows


def _action_summary(action_type: str, params: Optional[Dict[str, Any]]):
    """Render the summary rows as a 'what you are approving' block (html, text)
    for the email + in-app notification. Never empty (see _summary_rows)."""
    from html import escape as _esc
    rows = _summary_rows(action_type, params)
    html = ('<div style="background:#f6f8fb;border:1px solid #e1e6ef;border-radius:6px;'
            'padding:10px 14px;margin:12px 0;font-size:13px;">'
            '<div style="font-weight:700;color:#15233f;margin-bottom:6px;">'
            'What you are approving</div>'
            + "".join(f'<div style="margin:3px 0;"><b>{_esc(str(k))}:</b> '
                      f'{_esc(str(v))}</div>' for k, v in rows) + '</div>')
    text = "What you are approving:\n" + "".join(f"  - {k}: {v}\n" for k, v in rows)
    return html, text


def _build_approval_email(action_type: str, params: Optional[Dict[str, Any]],
                          amount: float, label: str, approval_uuid: str,
                          critique: Optional[Dict[str, Any]]):
    """Compose the routed-approval email (subject, html, text) — shared by the
    initial routing AND re-notification, so both carry the same rich 'what you
    are approving' context, critic opinion, and one-click decision links."""
    links = decision_links(approval_uuid)
    summ_html, summ_text = _action_summary(action_type, params)
    findings = [f for f in (critique or {}).get("findings", [])
                if f.get("verdict") in ("fail", "warn")][:4]
    critic_html = critic_text = ""
    if critique:
        col = {"endorse": "#1e7c45", "caution": "#a8720a",
               "object": "#a33a3a"}.get(critique.get("stance"), "#26304a")
        items = "".join(f'<li style="margin:2px 0;">{f["note"]}</li>' for f in findings)
        critic_html = (
            f'<div style="border-left:4px solid {col};background:#f6f8fb;'
            f'padding:10px 14px;margin:14px 0;font-size:13px;">'
            f'<b style="color:{col};">{_critic_line(critique)}</b>'
            + (f'<ul style="margin:6px 0 0 18px;padding:0;">{items}</ul>' if items else "")
            + '</div>')
        critic_text = ("\n" + _critic_line(critique) + "\n"
                       + "".join(f"  - {f['note']}\n" for f in findings))
    amt = f" (${amount:,.0f})" if amount else ""
    subject = f"Approval needed: {action_type}{amt}"
    body_html = (f"<p>A proposed action requires your decision "
                 f"(within your authority as {label}).</p>"
                 f"<p><b>{action_type}</b>{amt}"
                 f"<br>Approval ID: {approval_uuid}</p>"
                 + summ_html + critic_html +
                 f'<p style="margin:18px 0;">'
                 f'<a href="{links["approve"]}" style="background:#1e7c45;'
                 f'color:#fff;padding:10px 22px;border-radius:6px;'
                 f'text-decoration:none;font-weight:700;">✓ Approve</a>&nbsp;&nbsp;'
                 f'<a href="{links["reject"]}" style="background:#a33a3a;'
                 f'color:#fff;padding:10px 22px;border-radius:6px;'
                 f'text-decoration:none;font-weight:700;">✕ Reject</a></p>'
                 f'<p style="color:#7b8497;font-size:12px;">One-click, signed links '
                 f'— no sign-in needed. Or review the full context in the '
                 f'governance queue.</p>')
    body_text = (f"Approval needed: {action_type}{amt}\n"
                 f"Approval ID: {approval_uuid}\nAssigned to: {label}\n"
                 + (f"\n{summ_text}" if summ_text else "") + critic_text
                 + f"\nApprove: {links['approve']}\nReject:  {links['reject']}\n")
    return subject, body_html, body_text


def _deliver_approval_chat(chosen: Dict[str, Any], channel: str, approval_uuid: str,
                           action_type: str, amount: float, summ_text: str,
                           label: str) -> Dict[str, Any]:
    """Deliver an approval to an executive over their preferred chat channel
    (Slack/Teams) via the Unified Comms transports — the Executive-Intelligence →
    comms bridge. Best-effort: falls back to the in-app notification (already
    created) when the executive has no linked handle or the channel isn't
    configured, so an approval is never lost."""
    emp = chosen.get("employee_uuid")
    handle = None
    if emp:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT handle FROM channel_identities "
                            "WHERE party_type='employee' AND party_id=%s::uuid "
                            "AND channel=%s LIMIT 1", (emp, channel))
                r = cur.fetchone()
                handle = r[0] if r else None
        except Exception:
            conn.rollback()
        finally:
            conn.close()
    if not handle:
        logger.info(f"[governance] {label} prefers {channel} but has no linked "
                    f"{channel} handle — delivered in-app instead")
        return {"delivered": False, "reason": f"no {channel} handle (in-app fallback)"}
    links = decision_links(approval_uuid)
    amt = f" (${amount:,.0f})" if amount else ""
    text = (f"🛡️ Approval needed: {action_type}{amt}\n"
            + (summ_text + "\n" if summ_text else "")
            + f"Approve: {links['approve']}\nReject: {links['reject']}")
    from app.core import transports
    if channel == "slack":
        # Native in-thread Approve/Reject buttons (#6). The button values carry the
        # SAME HMAC (approval, action) token as the one-click email links, so the
        # /slack/interactive endpoint verifies the decision is untampered. The
        # link-bearing `text` is the fallback where blocks can't render.
        _, blocks = transports.approval_blocks(
            approval_uuid, action_type, amount, summ_text,
            decision_token(approval_uuid, "approve"),
            decision_token(approval_uuid, "reject"))
        res = transports._slack_post_blocks(handle, text, blocks)
    else:                                    # teams — connector-gated (drafted)
        res = {"sent": False, "reason": "teams connector not configured (drafted)"}
    logger.info(f"[governance] approval → {label} via {channel}: {res}")
    return {"delivered": bool(res.get("sent")), "channel": channel, "result": res}


def route_approval(approval_uuid: str, action_type: str,
                   params: Dict[str, Any],
                   critique: Optional[Dict[str, Any]] = None
                   ) -> Optional[Dict[str, Any]]:
    """Assign the approval to an executive: prefer the role that owns this kind
    of action, then the SMALLEST sufficient approval_authority_limit (delegate
    down, escalate only when the amount demands it; NULL limit = unlimited).
    Records the assignment, emits an approval.routed audit event, notifies the
    executive (in_app when they map to an employee; email when GOV_ROUTE_EMAIL).
    When a critic critique is supplied, it rides along in the notification,
    the audit event, and the one-click decision email."""
    amount = _amount_from(params)
    role = _affinity_role(action_type)
    _, summ_text = _action_summary(action_type, params)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT executive_id::text, role_code, full_name, email,
                          approval_authority_limit, auto_email_enabled,
                          employee_uuid::text, preferred_channel
                   FROM executives WHERE is_active""")
            execs = [dict(zip([d[0] for d in cur.description], r))
                     for r in cur.fetchall()]
            if not execs:
                return None

            def limit_of(e):
                lim = e["approval_authority_limit"]
                return float(lim) if lim is not None else float("inf")

            candidates = [e for e in execs if limit_of(e) >= amount] or \
                [max(execs, key=limit_of)]   # nobody sufficient → highest authority
            # role match first; then smallest sufficient limit; then stable order
            chosen = sorted(candidates,
                            key=lambda e: (e["role_code"] != role,
                                           limit_of(e), e["role_code"]))[0]
            label = f"{chosen['role_code']} {chosen['full_name']}"

            # Executive Intelligence → Unified Comms: deliver the way THIS
            # executive's profile prefers. 'all' (default) = email + in-app;
            # a single channel narrows it; slack/teams route over the comms
            # transports with in-app kept as a reliable safety net.
            pref = (chosen.get("preferred_channel") or "all").strip().lower()
            want_inapp = pref in ("all", "in_app", "both", "")
            want_email = pref in ("all", "email", "both", "")
            want_chat = pref in ("slack", "teams")
            if want_chat:
                want_inapp = True

            cur.execute(
                """UPDATE action_approvals
                   SET amount=%s, assigned_executive_id=%s::uuid, assigned_to=%s
                   WHERE approval_uuid=%s::uuid""",
                (amount, chosen["executive_id"], label, approval_uuid))

            cur.execute(
                "SELECT emit_event(%s,%s,%s,%s,%s,%s)",
                ("approval.routed", "approval", approval_uuid,
                 json.dumps({"context": {"action_type": action_type,
                                         "amount": amount, "assigned_to": label,
                                         "affinity_role": role,
                                         "critic_stance": (critique or {}).get("stance")}}),
                 None, "governance"))
            event_uuid = cur.fetchone()[0]

            # Prefer the executive's linked employee; else resolve an owner by
            # the executive's email so the in-app notification still reaches them
            # when they're a CRM user under a different id (self-healing).
            emp_uuid = chosen.get("employee_uuid")
            if not emp_uuid and chosen.get("email"):
                cur.execute("SELECT owner_id::text FROM owners "
                            "WHERE lower(email)=lower(%s) AND COALESCE(is_active,true) "
                            "LIMIT 1", (chosen["email"],))
                _o = cur.fetchone()
                emp_uuid = _o[0] if _o else None
            if want_inapp and emp_uuid:
                critic_note = _critic_line(critique)
                cur.execute(
                    """INSERT INTO notifications
                         (employee_uuid, event_uuid, channel, status, title, body, metadata)
                       VALUES (%s::uuid, %s, 'in_app', 'pending', %s, %s, %s)""",
                    (emp_uuid, event_uuid,
                     f"🛡️ Approval needed: {action_type}" +
                     (f" (${amount:,.0f})" if amount else ""),
                     f"Proposed action awaits your decision. Review in the "
                     f"governance queue (approval {approval_uuid[:8]})."
                     + (f"\n\n{summ_text}" if summ_text else "")
                     + (f"\n{critic_note}" if critic_note else ""),
                     json.dumps({"kind": "approval_routed",
                                 "approval_uuid": approval_uuid,
                                 "critic_stance": (critique or {}).get("stance")})))
        conn.commit()
    finally:
        conn.close()

    # ── Staff-email Stage 2: SHADOW OBSERVATION ─────────────────────────────
    # An assigned approval is the archetypal Tier 1 interrupt: a named person
    # must click Approve or Reject, and nothing else makes that happen. This
    # records what the new rules WOULD decide; the existing email below is
    # unchanged either way.
    #
    # ORDER MATTERS HERE, AND GETTING IT WRONG WAS SILENT. This block first sat
    # AFTER the email. By the time it ran, Stage 3 had already claimed the
    # ledger key, so decide() correctly answered "already in the ledger in a
    # terminal state" — and every approval observed as `already_handled`
    # instead of as the decision actually taken. The Stage 2 evidence for
    # approvals was quietly worthless, and no test failed; it was caught by
    # reading one live counter row.
    #
    # Passes executive_id rather than a name: this is the one path in the
    # codebase that already carries a properly typed identity, with no
    # free-text assignee to discard.
    try:
        from app.core import staff_email
        staff_email.observe(kind="approval", tier=staff_email.TIER_INTERRUPT,
                            ref=approval_uuid,
                            executive_id=chosen["executive_id"])
    except Exception as exc:                                   # noqa: BLE001
        logger.debug(f"[governance] staff-email observation skipped: {exc}")

    if want_email and GOV_ROUTE_EMAIL and chosen.get("auto_email_enabled") \
            and chosen.get("email"):
        try:
            from app.agents.email.smtp_imap import send_email
            subject, body_html, body_text = _build_approval_email(
                action_type, params, amount, label, approval_uuid, critique)

            # ── Staff-email Stage 3 ────────────────────────────────────────
            # The recipient, the template and the send are unchanged. The
            # ledger row around them is what is new: this approval email now
            # carries an idempotency key and a recorded provider outcome
            # instead of a bare call whose result was discarded.
            #
            # FAIL-OPEN ON BOOKKEEPING. If the ledger table is absent — as it
            # is anywhere the migration has not been applied yet — the approval
            # still goes out. No executive may miss a decision because our
            # audit table was missing.
            #
            # renotify_pending() is DELIBERATELY not wired: that is a human
            # explicitly asking to re-send with a better template, the one case
            # where suppressing a duplicate would be the wrong answer.
            claim_info = {"proceed": True, "recorded": False, "email_id": None}
            try:
                from app.core import staff_email
                claim_info = staff_email.begin_send(
                    kind="approval", tier=staff_email.TIER_INTERRUPT,
                    ref=approval_uuid,
                    recipient_email=chosen["email"],
                    recipient_kind="executive",
                    recipient_owner_id=emp_uuid,
                    subject=subject,
                    subject_ref_type="approval",
                    subject_ref_id=approval_uuid,
                    decision_reason=f"routed to {label}")
            except Exception as exc:                          # noqa: BLE001
                logger.debug(f"[governance] staff-email claim skipped: {exc}")

            if not claim_info.get("proceed"):
                logger.info(f"[governance] approval {approval_uuid[:8]} email "
                            f"not sent: {claim_info.get('why')}")
            else:
                res = send_email(to=chosen["email"], subject=subject,
                                 body_html=body_html, body_text=body_text)
                # internal/administrative — transactional, not commercial
                try:
                    from app.core import staff_email
                    staff_email.finish_send(claim_info.get("email_id"), res)
                except Exception as exc:                      # noqa: BLE001
                    logger.debug(f"[governance] staff-email outcome "
                                 f"skipped: {exc}")
        except Exception as exc:
            logger.warning(f"[governance] route email failed: {exc}")

    if want_chat:
        try:
            _deliver_approval_chat(chosen, pref, approval_uuid, action_type,
                                   amount, summ_text, label)
        except Exception as exc:
            logger.warning(f"[governance] chat delivery failed: {exc}")

    logger.info(f"[governance] routed {approval_uuid[:8]} → {label} "
                f"(amount=${amount:,.0f}, affinity={role}, channel={pref})")
    return {"assigned_to": label, "amount": amount,
            "executive_id": chosen["executive_id"]}


def renotify_pending(to: Optional[str] = None, limit: int = 20,
                     action_type: Optional[str] = None) -> Dict[str, Any]:
    """Re-send the routed-approval email for PENDING approvals using the CURRENT
    (richer 'what you are approving') template — so items proposed before the
    template improved get an informative email. `to` overrides the recipient (for
    testing); otherwise each goes to its assigned executive. Read-only: re-sends
    email only, never touches the approval or executes anything."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.approval_uuid::text AS approval_uuid,
                          a.action_type AS action_type, a.params AS params,
                          COALESCE(a.amount, 0) AS amount,
                          a.assigned_to AS assigned_to, a.critique AS critique,
                          e.email AS exec_email
                   FROM action_approvals a
                   LEFT JOIN executives e ON e.executive_id = a.assigned_executive_id
                   WHERE a.status='pending'
                     AND (a.expires_at IS NULL OR a.expires_at > now())
                     AND (%(at)s IS NULL OR a.action_type = %(at)s)
                   ORDER BY a.created_at DESC LIMIT %(lim)s""",
                {"at": action_type, "lim": int(limit)})
            rows = [dict(zip([d[0] for d in cur.description], r))
                    for r in cur.fetchall()]
    finally:
        conn.close()

    from app.agents.email.smtp_imap import send_email
    sent, skipped = [], []
    for r in rows:
        dest = to or r.get("exec_email")
        if not dest:
            skipped.append({"approval_uuid": r["approval_uuid"], "reason": "no recipient"})
            continue
        try:
            subject, body_html, body_text = _build_approval_email(
                r["action_type"], r["params"], float(r.get("amount") or 0),
                r.get("assigned_to") or "the approver", r["approval_uuid"],
                r.get("critique"))
            res = send_email(to=dest, subject=subject,
                             body_html=body_html, body_text=body_text)
            ok = bool((res or {}).get("success", True))
            (sent if ok else skipped).append(
                {"approval_uuid": r["approval_uuid"], "action_type": r["action_type"],
                 "to": dest, "ok": ok})
        except Exception as exc:
            skipped.append({"approval_uuid": r["approval_uuid"], "error": str(exc)[:150]})
    logger.info(f"[governance] renotify — sent {len(sent)}, skipped {len(skipped)}")
    return {"pending_matched": len(rows), "sent": sent, "skipped": skipped,
            "recipient_override": to}


def _row(approval_uuid: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT approval_uuid, action_type, proposed_by, entity_type,
                          entity_id, params, confidence, severity, status,
                          created_at, expires_at, result, executed_at
                   FROM action_approvals WHERE approval_uuid=%s::uuid""",
                (approval_uuid,))
            r = cur.fetchone()
            if not r:
                return None
            d = dict(zip([c[0] for c in cur.description], r))
            d["approval_uuid"] = str(d["approval_uuid"])
            d["entity_id"] = str(d["entity_id"]) if d["entity_id"] else None
            d["confidence"] = float(d["confidence"]) if d["confidence"] is not None else None
            for k in ("created_at", "expires_at"):
                d[k] = d[k].isoformat() if d[k] else None
            return d
    finally:
        conn.close()


def pending() -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """SELECT approval_uuid, action_type, proposed_by, entity_type,
                              entity_id, params, confidence, severity, created_at,
                              expires_at, amount, assigned_to, critique
                       FROM action_approvals
                       WHERE status='pending' AND (expires_at IS NULL OR expires_at>now())
                       ORDER BY created_at""")
            except Exception:
                conn.rollback()   # routing/critic migrations not applied yet
                cur.execute(
                    """SELECT approval_uuid, action_type, proposed_by, entity_type,
                              entity_id, params, confidence, severity, created_at,
                              expires_at
                       FROM action_approvals
                       WHERE status='pending' AND (expires_at IS NULL OR expires_at>now())
                       ORDER BY created_at""")
            cols = [c[0] for c in cur.description]
            out = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["approval_uuid"] = str(d["approval_uuid"])
                d["entity_id"] = str(d["entity_id"]) if d["entity_id"] else None
                d["confidence"] = float(d["confidence"]) if d["confidence"] is not None else None
                for k in ("created_at", "expires_at"):
                    d[k] = d[k].isoformat() if d[k] else None
                # 'What you are approving' — same rows the email/notification use.
                d["summary"] = [{"label": k, "value": v} for k, v in
                                _summary_rows(d.get("action_type"), d.get("params"))]
                out.append(d)
            return out
    finally:
        conn.close()


def history(limit: int = 30) -> List[Dict[str, Any]]:
    """Recent decided/expired proposals — the audit trail for the admin UI."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT approval_uuid, action_type, proposed_by, entity_type,
                          entity_id, confidence, severity, status, decided_by,
                          decided_at, decision_reason, result, created_at
                   FROM action_approvals
                   WHERE status <> 'pending'
                   ORDER BY COALESCE(decided_at, created_at) DESC
                   LIMIT %s""", (int(limit),))
            cols = [c[0] for c in cur.description]
            out = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["approval_uuid"] = str(d["approval_uuid"])
                d["entity_id"] = str(d["entity_id"]) if d["entity_id"] else None
                d["confidence"] = float(d["confidence"]) if d["confidence"] is not None else None
                for k in ("created_at", "decided_at"):
                    d[k] = d[k].isoformat() if d[k] else None
                out.append(d)
            return out
    finally:
        conn.close()


def _set(approval_uuid: str, status: str, decided_by: Optional[str] = None,
         reason: Optional[str] = None, result: Optional[Dict] = None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE action_approvals
                   SET status=%(s)s,
                       decided_by=COALESCE(%(by)s, decided_by),
                       decided_at=CASE WHEN %(by)s IS NOT NULL THEN now() ELSE decided_at END,
                       decision_reason=COALESCE(%(r)s, decision_reason),
                       result=COALESCE(%(res)s::jsonb, result),
                       executed_at=CASE WHEN %(s)s IN ('executed','failed') THEN now() ELSE executed_at END
                   WHERE approval_uuid=%(id)s::uuid""",
                {"s": status, "by": decided_by, "r": reason,
                 "res": json.dumps(result) if result is not None else None,
                 "id": approval_uuid})
        conn.commit()
    finally:
        conn.close()


def principal_for_decider(decided_by: Optional[str]) -> "Any":
    """The approving authority, WITH ITS CATEGORY TOLD TRUTHFULLY.

    THE DEFECT THIS REPLACES. The first version asserted
    `Principal(kind="user", id=decided_by)` on the reasoning that "the approver
    is the authority". The authority part is right; the `user` part is false
    most of the time. Measured across 118 approvals:

        policy:web_order_cancel     46   a POLICY decided it, not a person
        system                      36   the expiry sweep
        email-link                  13   a CHANNEL — one-click, HMAC-authorised
        admin@conscestra.local      12   an actual identity
        policy:voice_order_cancel    3
        ui-test@local                1

    Only 12 of 111 decided approvals name a human. Stamping `user:` on the other
    99 puts a false category into the one column that exists to answer "who
    initiated this" — and by this codebase's own doctrine a FALSE record is
    worse than an absent one, which is what the field was before.

    `decided_by` is caller-supplied (`_Decision.decided_by`, default "human")
    on an admin-gated endpoint, or the literal "email-link" on the HMAC path.
    So AUTHORIZATION is genuine either way — a gate always precedes execution —
    but the VALUE is not an authenticated identity, and the kind must not claim
    it is. Shape is the only honest signal available.
    """
    from app.core.a2a import Principal
    d = (decided_by or "").strip()
    if not d:
        return Principal.service("governance")
    if d.startswith("policy:"):
        # An auto-decision by a named policy. Not a person, and recording it as
        # one would hide that no human looked.
        # The kind already carries "policy"; keeping the prefix in the id too
        # renders as `policy:policy:web_order_cancel`.
        return Principal(kind="policy", id=d.split(":", 1)[1] or d,
                         display=d, role="auto-approver")
    if d == "system":
        return Principal.service("governance-expiry")
    if d == "email-link":
        # Authorised by possession of an HMAC token mailed to the assigned
        # executive. A real authorisation, an unknown person: the token proves
        # the mailbox was reached, not who clicked.
        return Principal(kind="token", id="email-link",
                         display="one-click approval link", role="approver")
    return Principal(kind="user", id=d, display=d, role="approver")


async def _execute(ap: Dict[str, Any]) -> Dict[str, Any]:
    """Run an approved action by re-dispatching it through A2A (gate bypassed)."""
    from app.core.a2a import A2ARequest, EntityRef, dispatch
    params = ap.get("params") or {}
    # THE APPROVER IS THE AUTHORITY, and its CATEGORY is told truthfully —
    # see principal_for_decider. Not "governance", which is the mechanism, and
    # not the agent that proposed it, which was refused permission to act alone.
    #
    # Found by the write gate refusing this path outright: the first version of
    # that gate would have broken every approved execution, because
    # `govern_bypass=True` skips the CONFIDENCE gate and was silently assumed to
    # skip identity too. Bypassing "is the agent sure enough" must not bypass
    # "on whose authority".
    req = A2ARequest(
        intent=ap["action_type"], from_agent="governance", params=params,
        principal=principal_for_decider(ap.get("decided_by")),
        entity=EntityRef(ap["entity_type"], ap["entity_id"]) if ap.get("entity_type") else None,
        confidence=1.0, govern_bypass=True,
        # reuse the originating play's lineage so the approved execution lands
        # in the same GET /trace/{cid} as the proposal that spawned it
        correlation_id=(params.get("_correlation_id")
                        or params.get("plan_correlation_id")))
    res = await dispatch(req)
    out = {"ok": res.ok, "output": res.output, "error": res.error}
    # Persist the structured result when it serializes — undo() needs it to
    # know WHAT was created (e.g. the campaign_uuid to cancel).
    try:
        json.dumps(res.data)
        out["data"] = res.data
    except (TypeError, ValueError):
        pass
    return out


async def approve(approval_uuid: str, decided_by: str = "human",
                  reason: Optional[str] = None) -> Dict[str, Any]:
    ap = _row(approval_uuid)
    if not ap:
        return {"ok": False, "error": "not found"}
    if ap["status"] != "pending":
        return {"ok": False, "error": f"not pending (status={ap['status']})"}
    _set(approval_uuid, "approved", decided_by, reason)
    res = await _execute(ap)
    _set(approval_uuid, "executed" if res["ok"] else "failed", result=res)
    return {"ok": res["ok"], "status": "executed" if res["ok"] else "failed",
            "approval_uuid": approval_uuid, "result": res}


def reject(approval_uuid: str, decided_by: str = "human",
           reason: Optional[str] = None) -> Dict[str, Any]:
    ap = _row(approval_uuid)
    if not ap:
        return {"ok": False, "error": "not found"}
    if ap["status"] != "pending":
        return {"ok": False, "error": f"not pending (status={ap['status']})"}
    _set(approval_uuid, "rejected", decided_by, reason)
    return {"ok": True, "status": "rejected", "approval_uuid": approval_uuid}


# ============================================================================
# Stale-approval expiry — pending items don't linger as silent liabilities
# ============================================================================

def expire_stale() -> Dict[str, Any]:
    """Flip pending approvals past their expires_at to 'expired' (audited).
    pending() already filters them out of the live queue; this makes the state
    honest in the table too. Scheduled nightly + POST /governance/expire."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE action_approvals
                   SET status='expired', decided_by='system',
                       decided_at=now(),
                       decision_reason='expired unactioned (TTL passed)'
                   WHERE status='pending' AND expires_at IS NOT NULL
                     AND expires_at < now()
                   RETURNING approval_uuid::text, action_type""")
            rows = cur.fetchall()
        conn.commit()
    finally:
        conn.close()
    if rows:
        logger.info(f"[governance] expired {len(rows)} stale approval(s): "
                    f"{[r[1] for r in rows]}")
    return {"expired": len(rows),
            "items": [{"approval_uuid": r[0], "action_type": r[1]} for r in rows]}


# ============================================================================
# Reversibility — undo an EXECUTED action (per-action-type handlers)
# ============================================================================

UNDO_WINDOW_HOURS = int(os.getenv("GOV_UNDO_WINDOW_HOURS", "72"))


def _undo_campaign_winback(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import marketing
    cid = (((ap.get("result") or {}).get("data") or {}) or {}).get("campaign_uuid")
    if not cid:
        return {"ok": False, "error": "no campaign_uuid recorded on the approval"}
    return marketing.cancel_campaign(
        cid, f"undone via governance approval {ap['approval_uuid'][:8]}")


def _undo_tuning_adjust(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import tuning
    param = (ap.get("params") or {}).get("param")
    if not param:
        return {"ok": False, "error": "no param recorded on the approval"}
    return tuning.revert(param)


def _undo_kb_publish(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import knowledge
    aid = (((ap.get("result") or {}).get("data") or {}) or {}).get("article_uuid")
    if not aid:
        return {"ok": False, "error": "no article_uuid recorded on the approval"}
    return knowledge.retire(aid, f"undone via governance approval "
                                 f"{ap['approval_uuid'][:8]}")


# action_type → handler(approval_row) -> {'ok': bool, ...}. An action is
# reversible only if a handler exists AND the handler can still unwind it —
# handlers must report what could NOT be reversed (e.g. real emails sent).
def _undo_meeting_book(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import booking
    aid = (((ap.get("result") or {}).get("data") or {}) or {}).get("activity_id")
    if not aid:
        return {"ok": False, "error": "no activity_id recorded on the approval"}
    return booking.cancel(aid, f"undone via governance approval "
                               f"{ap['approval_uuid'][:8]}")


def _undo_scoring_activate(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import scoring
    prev = (((ap.get("result") or {}).get("data") or {}) or {}).get("previous_version")
    return scoring.deactivate_to(prev)


def _undo_dq_normalize(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import data_quality
    return data_quality.undo_normalize_phones(
        ((ap.get("result") or {}).get("data")) or {})


def _undo_dq_merge(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import data_quality
    return data_quality.undo_merge_contacts(
        ((ap.get("result") or {}).get("data")) or {})


def _undo_identity_materialize(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import identity_links
    return identity_links.undo_materialize(
        ((ap.get("result") or {}).get("data")) or {})


def _undo_contact_update_profile(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import voice_support
    return voice_support.undo_profile_update(ap)


def _undo_order_cancel(ap: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import voice_support
    return voice_support.undo_order_cancel(ap)


_UNDO = {
    "campaign.winback": _undo_campaign_winback,
    "tuning.adjust": _undo_tuning_adjust,
    "kb.publish": _undo_kb_publish,
    "meeting.book": _undo_meeting_book,
    "scoring.activate": _undo_scoring_activate,
    "data.normalize_phones": _undo_dq_normalize,
    "data.merge_contacts": _undo_dq_merge,
    "identity.materialize_link": _undo_identity_materialize,
    "contact.update_profile": _undo_contact_update_profile,
    "order.cancel": _undo_order_cancel,
}


async def undo(approval_uuid: str, decided_by: str = "human",
               reason: Optional[str] = None) -> Dict[str, Any]:
    """Reverse an executed action within GOV_UNDO_WINDOW_HOURS. Status becomes
    'undone'; the undo outcome is merged into the audit row's result."""
    ap = _row(approval_uuid)
    if not ap:
        return {"ok": False, "error": "not found"}
    if ap["status"] != "executed":
        return {"ok": False, "error": f"only executed actions can be undone "
                                      f"(status={ap['status']})"}
    handler = _UNDO.get(ap["action_type"])
    if not handler:
        return {"ok": False, "error": f"'{ap['action_type']}' is not reversible "
                f"(no undo handler; e.g. sent email cannot be unsent)"}
    executed_at = ap.get("executed_at")
    if executed_at is not None:
        from datetime import datetime, timezone, timedelta
        if datetime.now(timezone.utc) - executed_at > timedelta(hours=UNDO_WINDOW_HOURS):
            return {"ok": False, "error": f"undo window closed "
                    f"({UNDO_WINDOW_HOURS}h after execution)"}
    try:
        res = await asyncio.to_thread(handler, ap)
    except Exception as exc:
        return {"ok": False, "error": f"undo handler failed: {exc}"}
    if res.get("ok"):
        _set(approval_uuid, "undone", decided_by,
             reason or "undone via governance",
             result={**(ap.get("result") or {}), "undo": res})
    return {"ok": bool(res.get("ok")), "status": "undone" if res.get("ok") else ap["status"],
            "approval_uuid": approval_uuid, "undo": res}


# ============================================================================
# One-click decisions — HMAC-signed approve/reject links (like unsubscribe)
# ============================================================================
# The routed-approval email carries per-action signed links, so the executive
# decides from their phone without a CRM session. The token binds
# (approval_uuid, action) to a server secret; approve()/reject() refuse
# non-pending rows, so links are single-use by construction.

def _link_secret() -> bytes:
    s = (os.getenv("GOV_LINK_SECRET") or os.getenv("UNSUBSCRIBE_SECRET")
         or os.getenv("ADMIN_API_TOKEN") or "")
    return s.encode("utf-8")


def decision_token(approval_uuid: str, action: str) -> str:
    import hashlib
    import hmac as _hmac
    return _hmac.new(_link_secret(), f"{approval_uuid}:{action}".encode("utf-8"),
                     hashlib.sha256).hexdigest()[:32]


def _verify_token(approval_uuid: str, action: str, token: str) -> bool:
    import hmac as _hmac
    if not _link_secret() or not token:
        return False
    return _hmac.compare_digest(decision_token(approval_uuid, action), token)


def decision_links(approval_uuid: str) -> Dict[str, str]:
    base = (os.getenv("APP_URL", "") or "http://localhost:8000").rstrip("/")
    return {a: (f"{base}/governance/decide?g={approval_uuid}"
                f"&a={a}&t={decision_token(approval_uuid, a)}")
            for a in ("approve", "reject")}


# ============================================================================
# Endpoints
# ============================================================================

router = APIRouter(tags=["governance"])


@router.get("/governance/status")
def governance_status():
    return {"enabled": ENABLED, "act_min": act_min(), "propose_min": propose_min(),
            "defaults": {"act_min": ACT_MIN, "propose_min": PROPOSE_MIN},
            "pending": len(pending())}


# ── Policy-as-data endpoints (audit #4) ──────────────────────────────────────

class _PolicyBody(BaseModel):
    value: float
    updated_by: Optional[str] = None


@router.get("/governance/policies")
def governance_policies():
    """Every known policy: its effective value and where it comes from."""
    rows = _policy_rows()
    out = []
    for key, spec in _KNOWN_POLICIES.items():
        out.append({"key": key, "description": spec["description"],
                    "effective": policy_value(key),
                    "default": spec["default"](),
                    "source": "db" if key in rows else "default",
                    "bounds": [spec.get("min"), spec.get("max")]})
    unknown = [k for k in rows if k not in _KNOWN_POLICIES]
    return {"policies": out, **({"unknown_rows": unknown} if unknown else {})}


@router.put("/governance/policies/{key}")
def governance_policy_put(key: str, body: _PolicyBody):
    spec = _KNOWN_POLICIES.get(key)
    if not spec:
        return {"ok": False, "error": f"unknown policy '{key}' — known: "
                                      f"{sorted(_KNOWN_POLICIES)}"}
    v = float(body.value)
    if not (spec.get("min", v) <= v <= spec.get("max", v)):
        return {"ok": False, "error": f"value {v} outside bounds "
                                      f"[{spec.get('min')}, {spec.get('max')}]"}
    if spec.get("int"):
        v = int(v)
    # the act/propose band must stay a band
    if key == "gov.act_min" and v < propose_min():
        return {"ok": False, "error": f"act_min {v} would fall below "
                                      f"propose_min {propose_min()}"}
    if key == "gov.propose_min" and v > act_min():
        return {"ok": False, "error": f"propose_min {v} would exceed "
                                      f"act_min {act_min()}"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO governance_policies
                     (policy_key, value, description, updated_by)
                   VALUES (%s, %s::jsonb, %s, %s)
                   ON CONFLICT (policy_key) DO UPDATE SET
                     value=EXCLUDED.value, updated_by=EXCLUDED.updated_by,
                     updated_at=now()""",
                (key, json.dumps(v), spec["description"],
                 body.updated_by or "admin"))
        conn.commit()
    finally:
        conn.close()
    invalidate_policy_cache()
    logger.info(f"[governance] policy {key} → {v} (by {body.updated_by or 'admin'})")
    return {"ok": True, "key": key, "effective": policy_value(key), "source": "db"}


@router.delete("/governance/policies/{key}")
def governance_policy_delete(key: str):
    """Remove the override — the env/code default applies again."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM governance_policies WHERE policy_key=%s",
                        (key,))
            n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    invalidate_policy_cache()
    return {"ok": True, "removed": n, "key": key,
            "effective": policy_value(key), "source": "default"}


@router.get("/governance/queue")
def governance_queue():
    return {"pending": pending()}


@router.get("/governance/history")
def governance_history(limit: int = 30):
    return {"history": history(limit)}


class _DeleteBody(BaseModel):
    ids: List[str]
    reason: Optional[str] = None
    deleted_by: Optional[str] = None


@router.post("/governance/history/delete")
def governance_history_delete(body: _DeleteBody):
    """Clear decided rows (executed/failed/rejected/expired) out of the queue.

    NOT destructive any more. trg_action_approvals_deletion_log archives the
    whole row into governed_deletions first, so this removes a decision from
    the working list without destroying the record that it happened — and
    restore_governed_deletion() can put one back. See
    sql/governance_history_audit.sql for why that record matters: these rows
    are the ONLY trace of a governed decision anywhere in the database.

    Pending actions can never be deleted — they must be approved or rejected
    first.
    """
    ids = [i for i in (body.ids or []) if i]
    if not ids:
        return {"deleted": 0}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # An EXECUTED row records a human authorising a real change. It may
            # still be cleared — the archive makes that safe — but not silently:
            # the reason is captured in the archive alongside the row, so the
            # question "who cleared the record of that approval, and why" has an
            # answer that does not depend on someone remembering.
            cur.execute(
                "SELECT count(*) FROM action_approvals "
                "WHERE approval_uuid = ANY(%s::uuid[]) AND status = 'executed'",
                (ids,))
            executed = cur.fetchone()[0]
            if executed and not (body.reason or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail=f"{executed} of these rows are 'executed' — they "
                           f"record an authorised action. Give a reason to "
                           f"clear them (the row is archived either way).")

            actor = (body.deleted_by or "").strip() or "admin"
            # Read by log_governed_deletion(). The repair_key must NOT be
            # 'undeclared': that tier is purged after 30 days, which would
            # quietly undo the archive. A declared key is kept for a year.
            cur.execute("SET LOCAL app.repair_key = 'governance:history-delete'")
            cur.execute("SET LOCAL app.actor = %s", (actor[:120],))

            cur.execute(
                "DELETE FROM action_approvals "
                "WHERE approval_uuid = ANY(%s::uuid[]) AND status <> 'pending' "
                "RETURNING approval_uuid, status", (ids,))
            rows = cur.fetchall()
            n = len(rows)

            # Prove the archive actually happened in this same transaction
            # rather than trusting that the trigger is attached. If the
            # migration has not been applied, the delete is still destructive
            # and the caller must find out now, not at the next audit.
            cur.execute(
                "SELECT count(*) FROM governed_deletions "
                "WHERE table_name = 'action_approvals' "
                "  AND txid = txid_current()")
            archived = cur.fetchone()[0]
            if n and archived < n:
                conn.rollback()
                raise HTTPException(
                    status_code=500,
                    detail="refused: deleting these rows would not have been "
                           "archived (apply sql/governance_history_audit.sql). "
                           "Nothing was deleted.")
        conn.commit()
        logger.info(f"[governance] cleared {n} decided history row(s) by "
                    f"{actor} ({executed} executed); archived to "
                    f"governed_deletions")
        return {"deleted": n, "requested": len(ids), "archived": archived,
                "executed_cleared": executed, "recoverable": True}
    finally:
        conn.close()


class _Decision(BaseModel):
    decided_by: str = "human"
    reason: Optional[str] = None


@router.post("/governance/approve/{approval_uuid}")
async def governance_approve(approval_uuid: str, body: _Decision = _Decision()):
    return await approve(approval_uuid, body.decided_by, body.reason)


@router.post("/governance/reject/{approval_uuid}")
def governance_reject(approval_uuid: str, body: _Decision = _Decision()):
    return reject(approval_uuid, body.decided_by, body.reason)


@router.post("/governance/undo/{approval_uuid}")
async def governance_undo(approval_uuid: str, body: _Decision = _Decision()):
    """Reverse an executed action (within GOV_UNDO_WINDOW_HOURS, if its
    action_type has an undo handler)."""
    return await undo(approval_uuid, body.decided_by, body.reason)


@router.post("/governance/expire")
def governance_expire():
    """Flip pending approvals past their TTL to 'expired' (also runs nightly)."""
    return expire_stale()


@router.post("/governance/renotify")
def governance_renotify(to: Optional[str] = None, limit: int = 20,
                        action_type: Optional[str] = None):
    """Re-send informative approval emails for pending items (current template).
    ?to= overrides the recipient (testing); ?action_type= filters; ?limit= caps.
    Re-sends email only — never executes or changes the approval."""
    return renotify_pending(to=to, limit=limit, action_type=action_type)


@router.post("/governance/critique/{approval_uuid}")
async def governance_critique_one(approval_uuid: str):
    """(Re)run the independent critic on one approval — e.g. after resolving a
    complaint, to refresh the verdict before deciding."""
    import asyncio as _aio
    from app.core import critic
    return await _aio.to_thread(critic.review, approval_uuid)


@router.post("/governance/critique")
async def governance_critique_backfill():
    """Critique every live pending approval that has none yet."""
    import asyncio as _aio
    from app.core import critic
    return await _aio.to_thread(critic.review_pending)


# ── Public one-click decision endpoint (token IS the auth) ──────────────────

public_router = APIRouter(tags=["governance-public"])

_DECIDE_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#eef1f6;margin:0;padding:40px 16px;">
<div style="max-width:460px;margin:0 auto;background:#fff;border:1px solid #e1e6ef;border-radius:8px;padding:28px;">
<div style="font-family:Georgia,serif;font-size:11px;letter-spacing:.22em;color:#b08a46;font-weight:700;text-transform:uppercase;">Conscestra CRM</div>
<h2 style="color:#15233f;margin:10px 0 6px;">{title}</h2>
<p style="color:#26304a;font-size:14px;line-height:1.55;">{body}</p>
</div></body></html>"""


@public_router.get("/governance/decide", response_class=HTMLResponse)
async def governance_decide_link(g: str = "", a: str = "", t: str = ""):
    """One-click approve/reject from the routed-approval email. The HMAC token
    binds (approval, action); non-pending rows refuse re-decisions, so a link
    can only ever be used once."""
    action = (a or "").strip().lower()
    if action not in ("approve", "reject") or not _verify_token(g, action, t):
        return HTMLResponse(_DECIDE_PAGE.format(
            title="Link not valid",
            body="This decision link is invalid or was tampered with. "
                 "Open the governance queue in the CRM instead."), status_code=403)
    ap = _row(g)
    if not ap:
        return HTMLResponse(_DECIDE_PAGE.format(
            title="Not found", body="This approval no longer exists."), status_code=404)
    if ap["status"] != "pending":
        return HTMLResponse(_DECIDE_PAGE.format(
            title="Already decided",
            body=f"This approval was already <b>{ap['status']}</b>. "
                 f"Nothing further happened."))
    if action == "approve":
        res = await approve(g, decided_by="email-link")
        ok = res.get("ok")
        return HTMLResponse(_DECIDE_PAGE.format(
            title="Approved ✓" if ok else "Approved — execution failed",
            body=(f"<b>{ap['action_type']}</b> was approved and "
                  f"{'executed' if ok else 'queued but FAILED to execute'}."
                  + (f"<br><br>{res.get('result', {}).get('error') or ''}"
                     if not ok else ""))))
    res = reject(g, decided_by="email-link")
    return HTMLResponse(_DECIDE_PAGE.format(
        title="Rejected ✓",
        body=f"<b>{ap['action_type']}</b> was rejected. No action was taken."))
