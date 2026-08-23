"""Universal Escalation Object — U1 (round-2 Agentforce blindspots, 2026-07-25).

THE BUG THIS CLOSES
    `custom_agents.run()` instructed the model to say "a human teammate will
    follow up" when it had no approved answer. Nothing else happened. No queue
    item, no owner, no deadline, no notification, no entry in the takeover
    console we built for exactly this moment. On an embedded widget — running
    on someone ELSE's website — a visitor could ask for a human and the
    organization would never learn of it. The takeover console (#1) and the
    no-code/embedded agents (#3/#6) were two shipped features with no wire
    between them.

THE RULE
    Never let an agent promise an action that does not create a durable system
    record. `open()` IS that record.

        agent → escalation intent → escalations row
                                  → conversations.escalated (console queue)
                                  → in-app notification (SSE)
                                  → SLA deadline the supervisor can breach-check

DESIGN NOTES
  • Own table, not a conversation flag. An escalation has an owner, a priority
    and a DEADLINE, and it must exist even when the conversation could not be
    threaded (an anonymous visitor on a third-party site).
  • Idempotent per conversation: a partial unique index means asking three
    times in one thread yields ONE obligation. A re-ask escalates PRIORITY
    instead of creating a duplicate.
  • Honest about reachability. If we hold no email/phone, `contact_known` is
    false and the caller is told to ask the visitor for one — a promise we
    cannot deliver on is the failure mode we are fixing, not reproducing.
  • Never raises into a customer conversation. Every DB path is defensive; a
    missing migration degrades to a logged warning and `{"ok": False}` — the
    visitor still gets their answer.

Requires sql/escalations.sql. See also [agent_console] (the human seat),
[custom_agents] (the agents that raise these), [supervisor] (SLA breach).

CONFIG (env)
  ESCALATION_ENABLED        1    kill switch
  ESCALATION_SLA_MINUTES    240  default deadline for a normal escalation
  ESCALATION_NOTIFY         1    also raise an in-app notification (high/urgent)
  ESCALATION_EMAIL          0    ALSO email the role mailbox (exceptions only)
  ESCALATION_EMAIL_TO       support@agentorc.ca   where those exceptions go
"""

from __future__ import annotations

import json
import logging
import os
import re
from html import escape as html_escape
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("escalation")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("ESCALATION_ENABLED", "1")
NOTIFY = _flag("ESCALATION_NOTIFY", "1")
# Ships DARK. Turn on after watching a few land in the console first.
EMAIL_ENABLED = _flag("ESCALATION_EMAIL", "0")
ESCALATION_EMAIL_TO = os.getenv("ESCALATION_EMAIL_TO",
                                "support@agentorc.ca").strip()
DEFAULT_SLA_MINUTES = int(os.getenv("ESCALATION_SLA_MINUTES", "240"))

# Machine reasons — the WHY, kept small and stable so the queue can group them.
REASONS = {
    "customer_requested_human": "The customer explicitly asked for a person.",
    "no_approved_answer": "The agent had no approved knowledge and deferred.",
    "high_value_intent": "A purchase / contract intent above the self-serve bar.",
    "complaint": "The customer expressed frustration or threatened to leave.",
    "agent_promised_followup": "The agent's reply promised a human follow-up.",
    "manual": "Raised by a human.",

    # ── Order cancellation by phone (docs/order_cancellation_by_phone_design.md)
    # These were being passed by voice_support and SILENTLY COLLAPSED to
    # "manual" by the guard above, because an unregistered reason is not an
    # error here — it is a default. The truth survived only in
    # metadata.internal_reason, so the queue could not be filtered by what had
    # actually gone wrong, and any routing keyed on `reason` would have matched
    # nothing. Registering them is what makes the email routing below possible.
    "order_cancel_race": "An order changed status mid-call; NOT cancelled, and "
                         "the customer was promised a callback.",
    "order_cancel_email_failed": "The order WAS cancelled but the customer's "
                                 "confirmation email did not complete.",
    "order_cancel_unexpected_status": "An order was in a state the agent will "
                                      "not act on; it refused to guess.",
    "order_cancel_unverified": "A caller could not be verified for a "
                               "cancellation; no order was changed.",
    "order_cancel_unverifiable": "No phone on file, so the caller could not be "
                                 "verified by one-time code at all.",
    "order_cancel_lockout": "Too many incorrect verification codes.",
    "order_cancel_unavailable": "A cancellation was asked for while the "
                                "capability is switched off.",
}

# ── WHICH ESCALATIONS ALSO GO OUT BY EMAIL ──────────────────────────────────
# Deliberately NOT every escalation, and deliberately NOT successful actions.
#
# A successful AI cancellation is a completed self-service action with an audit
# trail, a customer confirmation and an in-app notice. Emailing about it asks a
# human to read something that needs no decision, and the fastest way to make an
# alerting channel ignored is to send things that require no action.
#
# What is here is the set where a HUMAN MUST DO SOMETHING and no other mechanism
# will make that happen: a customer promised a callback, a cancellation the
# customer has no confirmation of, or a state the agent declined to judge.
# The subject line is the whole message for someone triaging an inbox: it has to
# say what to DO, not name an enum. "[Action needed] order_cancel_unverified"
# makes a support agent open the mail to find out whether it matters. The reason
# code is kept in parentheses so mail rules can still filter on it — humans read
# the front, machines read the back.
_EMAIL_ACTIONS = {
    "order_cancel_race": "Cancellation did not complete — call the customer back",
    "order_cancel_email_failed": "Order cancelled, but no confirmation reached "
                                 "the customer",
    "order_cancel_unexpected_status": "Order in an unexpected state — needs a "
                                      "decision",
    "order_cancel_unverified": "Caller could not be verified — call them back",
    "customer_requested_human": "Customer asked to speak to a person",
}

_EMAIL_REASONS = {
    "order_cancel_race",
    "order_cancel_email_failed",
    "order_cancel_unexpected_status",
    "order_cancel_unverified",
    "customer_requested_human",
}

# Priority → SLA multiplier. Urgent work gets a tighter deadline, not just a
# louder label — a priority that doesn't change a deadline is decoration.
_SLA_BY_PRIORITY = {"urgent": 0.25, "high": 0.5, "normal": 1.0, "low": 2.0}
_PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


# ============================================================================
# Detection — did this turn earn an escalation?
# ============================================================================

# Deliberately deterministic (no LLM, no cost, no latency) and deliberately
# NARROW: a false positive costs a human a wasted glance at the queue, which is
# far cheaper than the failure we are fixing (a silent broken promise).
_HUMAN_RE = re.compile(
    # "speak to a person", "transfer me to an agent", "put me through to sales"
    r"\b(speak|talk|chat|connect|transfer|forward|put)\s+(me|us)?\s*(through\s+)?"
    r"(to|with)\s+(a|an|the|some)?\s*"
    r"(human|person|people|agent|rep|representative|advisor|someone|somebody|"
    r"manager|supervisor|operator|receptionist|sales|support|customer\s+service|"
    r"sales\s*rep|account\s*manager)\b"
    r"|\b(real|actual|live)\s+(human|person|agent)\b"
    r"|\bhuman\s+(agent|being|support|help)\b"
    r"|\b(get|give)\s+me\s+(a|an)\s+(human|person|manager|rep)\b"
    r"|\b(parler|parlez)\s+(à|a|avec)\s+(un|une)\s+(humain|personne|conseiller|agent)\b"
    # es — the voice line answers in Spanish, so it must hear the ask in Spanish
    # Spanish attaches the pronoun to the infinitive — "pasarme con", not
    # "pasar me con" — so the enclitic has to be part of the verb token.
    r"|\b(hablar|pasar|comunicar)(me|nos)?\s+(con|a)\s+(un|una|alguien)\s*"
    r"(persona|agente|representante|humano|asesor)?\b"
    # zh — 人工 is "a human operator", but 人工智能 is "artificial intelligence";
    # without this lookahead every caller ASKING ABOUT OUR AI gets transferred.
    r"|人工(?!智能)|真人|转接|人工客服|找人工|人工服务",
    re.I,
)
_COMPLAINT_RE = re.compile(
    r"\b(unacceptable|ridiculous|furious|outrageous|disgusted|appalled)\b"
    r"|\b(cancel|canceling|cancelling|terminate|close)\s+(my|our|the)\s+"
    r"(account|subscription|contract|service)\b"
    r"|\b(speak\s+to|escalate\s+to)\s+(your|a)\s+(manager|supervisor)\b"
    r"|\b(lawyer|legal action|sue|refund)\b"
    r"|\b(third|3rd)\s+time\s+(i|we)('ve|\s+have)?\s+(asked|contacted|emailed)\b",
    re.I,
)
_HIGH_VALUE_RE = re.compile(
    r"\b(enterprise|bulk|volume|wholesale)\s+(purchase|order|pricing|deal|plan|licen[cs]e)\b"
    r"|\b(quote|proposal|rfp|rfq|contract|procurement|invoice)\b"
    r"|\$\s?\d{1,3}(,\d{3})+(\.\d+)?\b"                    # $50,000
    r"|\b\d+\s?(k|K)\s+(budget|deal|contract|purchase)\b",
    re.I,
)


def detect(text: str) -> Optional[str]:
    """Return an escalation REASON for this customer message, or None.

    Order matters: an explicit ask for a person outranks inference about what
    they might want."""
    t = (text or "").strip()
    if len(t) < 3:
        return None
    if _HUMAN_RE.search(t):
        return "customer_requested_human"
    if _COMPLAINT_RE.search(t):
        return "complaint"
    if _HIGH_VALUE_RE.search(t):
        return "high_value_intent"
    return None


# The literal enforcement of the rule: did the AGENT's own reply promise that a
# person would follow up? If it did, that promise must become a row. This is
# checked on the outgoing text, so it catches the promise however the model
# chose to phrase it — including the module's own canned fallback.
_PROMISE_RE = re.compile(
    r"\b(a\s+)?(human|team\s?mate|teammate|colleague|team\s+member|specialist|"
    r"representative|rep|advisor|someone\s+from\s+(our|the)\s+team)\b[^.!?]{0,60}"
    r"\b(will|'ll|can|shall)\b[^.!?]{0,40}\b(follow\s?up|get\s+back|reach\s+out|"
    r"contact|be\s+in\s+touch|assist|help)\b"
    r"|\b(i'?ll|i\s+will|we'?ll|we\s+will)\b[^.!?]{0,40}\b(have|ask|get)\b"
    r"[^.!?]{0,40}\b(teammate|colleague|human|someone|specialist)\b"
    r"|\b(pass|escalate|forward)\s+(this|it|you)\b[^.!?]{0,30}"
    r"\b(to|on\s+to)\b[^.!?]{0,30}\b(a\s+)?(human|person|colleague|teammate|team)\b"
    r"|\b(un|une)\s+(coll[èe]gue|humain|conseiller)\b[^.!?]{0,40}"
    r"\b(vous\s+)?(contactera|recontactera|reviendra)\b",
    re.I,
)


def promised_followup(reply: str) -> bool:
    """True when an agent's outgoing reply commits a human to follow up."""
    return bool(_PROMISE_RE.search(reply or ""))


def priority_for(reason: str, text: str = "") -> str:
    """Priority reads the WHOLE message, not just the reason that won detection.

    "I need to speak with someone about a $50,000 enterprise purchase" is
    detected as customer_requested_human (an explicit ask outranks inference),
    but it is still a high-value conversation — so the money and the anger are
    scored independently of which pattern matched first."""
    if reason == "complaint" or _COMPLAINT_RE.search(text or ""):
        return "high"
    if reason == "high_value_intent" or _HIGH_VALUE_RE.search(text or ""):
        return "high"
    return "normal"


# ============================================================================
# The durable record
# ============================================================================

def _reachable(channel: Optional[str], handle: Optional[str]) -> bool:
    """True when `handle` is something a human can actually reply to. A webchat
    session key is NOT a contact — that distinction is the whole point."""
    h = (handle or "").strip().lower()
    if not h or h.startswith("session:") or h.startswith("anon"):
        return False
    if "@" in h and "." in h.split("@")[-1]:
        return True
    return bool(re.fullmatch(r"\+?\d[\d\s\-().]{6,}", h))


def open(reason: str, source: str, *,
         summary: str = "",
         transcript_excerpt: str = "",
         conversation_id: Optional[str] = None,
         channel: Optional[str] = None,
         handle: Optional[str] = None,
         party_type: Optional[str] = None,
         party_id: Optional[str] = None,
         priority: str = "normal",
         sla_minutes: Optional[int] = None,
         metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create (or re-prioritize) the obligation. Idempotent per conversation.

    Returns {ok, escalation_id, created, contact_known, sla_due_at}. NEVER
    raises — a failure here must not break the customer's conversation, it must
    only be loud in the log."""
    if not ENABLED:
        return {"ok": False, "error": "escalations disabled"}
    if reason not in REASONS:
        reason = "manual"
    priority = priority if priority in _PRIORITY_RANK else "normal"
    minutes = int(sla_minutes if sla_minutes is not None else
                  max(15, DEFAULT_SLA_MINUTES * _SLA_BY_PRIORITY.get(priority, 1.0)))
    known = _reachable(channel, handle)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Idempotency: fold a repeat ask into the LIVE escalation for this
            # conversation, raising its priority if the new ask is more urgent.
            if conversation_id:
                cur.execute(
                    """SELECT escalation_id::text, priority FROM escalations
                       WHERE conversation_id=%s::uuid
                         AND status IN ('open','assigned')
                       ORDER BY created_at DESC LIMIT 1""",
                    (conversation_id,))
                row = cur.fetchone()
                if row:
                    eid, cur_prio = row[0], row[1]
                    if _PRIORITY_RANK.get(priority, 1) > _PRIORITY_RANK.get(cur_prio, 1):
                        cur.execute(
                            """UPDATE escalations
                               SET priority=%s,
                                   sla_due_at = LEAST(sla_due_at,
                                       now() + make_interval(mins => %s)),
                                   updated_at = now()
                               WHERE escalation_id=%s::uuid""",
                            (priority, minutes, eid))
                        conn.commit()
                    logger.info(f"[escalation] re-ask folded into {eid[:8]} "
                                f"({reason}, priority={priority})")
                    return {"ok": True, "escalation_id": eid, "created": False,
                            "contact_known": known, "reason": reason}

            cur.execute(
                """INSERT INTO escalations
                     (source, reason, summary, transcript_excerpt, status,
                      priority, sla_minutes, sla_due_at, conversation_id,
                      channel, handle, contact_known, party_type, party_id,
                      metadata)
                   VALUES (%(src)s, %(rsn)s, %(sum)s, %(exc)s, 'open',
                           %(pri)s, %(min)s, now() + make_interval(mins => %(min)s),
                           %(cid)s::uuid, %(chan)s, %(hnd)s, %(known)s,
                           %(pt)s, %(pid)s::uuid, %(meta)s::jsonb)
                   RETURNING escalation_id::text, sla_due_at""",
                {"src": source[:120], "rsn": reason,
                 "sum": (summary or REASONS.get(reason, reason))[:400],
                 "exc": (transcript_excerpt or "")[:2000],
                 "pri": priority, "min": minutes,
                 "cid": conversation_id, "chan": channel, "hnd": handle,
                 "known": known, "pt": party_type, "pid": party_id,
                 "meta": json.dumps(metadata or {})})
            eid, due = cur.fetchone()

            # Light up the EXISTING takeover console for this conversation.
            if conversation_id:
                try:
                    cur.execute(
                        """UPDATE conversations SET escalated=true, updated_at=now()
                           WHERE conversation_id=%s::uuid""", (conversation_id,))
                except Exception as exc:      # migration not applied — non-fatal
                    logger.debug(f"[escalation] console flag skipped: {exc}")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[escalation] open failed (apply sql/escalations.sql?): {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()

    logger.info(f"[escalation] OPEN {eid[:8]} {reason} via {source} "
                f"priority={priority} reachable={known}")
    if NOTIFY and priority in ("high", "urgent"):
        _notify(eid, reason, priority, summary, channel, handle, known)

    # DELIBERATELY NOT nested in the block above. The in-app notice is gated on
    # PRIORITY; the email is gated on REASON, and the two answer different
    # questions — "is this urgent" versus "must a person do something". Nesting
    # them silently dropped the email for order_cancel_email_failed and
    # order_cancel_unverified, both of which open at priority 'normal' and both
    # of which need a human. Caught by the tests, not by review.
    # ── Staff-email Stage 2: SHADOW OBSERVATION ─────────────────────────────
    # Records what the new tier/recipient/budget rules WOULD decide. The
    # existing email behaviour below is untouched either way, and this sits in
    # its own guard because an observer that can break its host is worse than
    # no observer — the host here is a customer's escalation.
    #
    # IT MUST RUN BEFORE _email_escalation, AND THAT WAS WRONG FOR A WHILE.
    # Stage 3 gave _email_escalation a ledger claim. With the observation after
    # it, decide() correctly answered "already in the ledger in a terminal
    # state" — so every Tier 1 escalation recorded as `already_handled` instead
    # of as the decision actually taken.
    #
    # It was LATENT: with ESCALATION_EMAIL=0 the send returns before claiming,
    # so the observation looked right. It would have activated on exactly the
    # flag Stage 5 turns on, corrupting Stage 5's evidence from its first day.
    # Found by an adversarial audit, not by a test — hence test_45b now covers
    # both this call site and governance's.
    try:
        from app.core import staff_email
        staff_email.observe(kind="escalation",
                            tier=staff_email.tier_for_escalation(reason),
                            ref=eid,
                            assignee=None)   # nothing owns it at open() time
    except Exception as exc:                                   # noqa: BLE001
        logger.debug(f"[escalation] staff-email observation skipped: {exc}")

    _email_escalation(eid, reason, priority, summary, channel, handle,
                      known, transcript_excerpt, metadata)

    out = {"ok": True, "escalation_id": eid, "created": True,
           "contact_known": known, "reason": reason,
           "sla_due_at": due.isoformat() if due else None}

    # ── C1 Step 3: the escalation → case bridge ─────────────────────────────
    # An escalation is the EVENT; the case is the durable unit of WORK it
    # creates. This is the ONLY automatic path between them, and it is doubly
    # gated: CASES_ENABLED and CASES_AUTO_OPEN must BOTH be on. With
    # CASES_AUTO_OPEN=0 (the default) this block is a no-op and escalation
    # behaviour is byte-identical to before the case layer existed.
    #
    # It can never break an escalation: open() is documented to never raise,
    # and an obligation that failed to spawn a case is still an obligation.
    try:
        from app.core import cases
        if cases.ENABLED and cases.AUTO_OPEN:
            bridged = cases.open_from_escalation(eid, actor=f"escalation:{source}"[:120])
            if bridged.get("case_id"):
                out["case_id"] = bridged["case_id"]
    except Exception as exc:
        logger.warning(f"[escalation] case bridge skipped for {eid[:8]}: {exc}")
    return out


def _email_escalation(escalation_id: str, reason: str, priority: str,
                      summary: str, channel: Optional[str],
                      handle: Optional[str], contact_known: bool,
                      transcript_excerpt: str = "",
                      metadata: Optional[Dict[str, Any]] = None) -> None:
    """Email the on-call ROLE mailbox about an escalation a human must action.

    WHY A ROLE MAILBOX AND NOT A PERSON. The obvious answer — "tell whoever owns
    the order" — does not exist in this database and cannot be faked:

        orders.owner_id on cancellable orders : 0 of 42
        accounts.owner_id                     : 42 of 42, but every one of them
                                                classifies as customer_contact
        assignable_identity (real staff)      : 4 (the executives)

    So the account "owner" is a CUSTOMER, and the eight people with
    @emp.agentorc.ca mailboxes are the demo-seed cohort that assignable.py
    describes as "never granted". Emailable is not the same as routable, and
    inventing a routing rule from job titles is exactly the inference that
    module exists to refuse. A role mailbox is honest about that: it survives
    someone leaving, and it does not pretend the system knows whose desk this
    belongs on.

    When ownership becomes real — orders owned, staff granted assignability —
    this function is where "prefer the owner, fall back to the role mailbox"
    goes, and nothing else has to change.

    NEVER RAISES, and never blocks the caller: an escalation is already durable
    in its own table before this runs. The send outcome is written back to the
    escalation's metadata, so a reader can tell "we emailed" from "we meant to".
    """
    if not EMAIL_ENABLED or reason not in _EMAIL_REASONS:
        return
    to = (ESCALATION_EMAIL_TO or "").strip()
    if not to:
        logger.warning("[escalation] ESCALATION_EMAIL_TO is empty — not sending")
        return

    md = metadata or {}
    who = handle if contact_known else "an unidentified caller"
    order = md.get("order_number") or "—"
    lines = [
        f"Reason:      {reason} — {REASONS.get(reason, '')}",
        f"Priority:    {priority}",
        f"Order:       {order}",
        f"Channel:     {channel or 'unknown'}",
        f"Customer:    {who}",
        f"Escalation:  {escalation_id}",
    ]
    if md.get("internal_reason"):
        lines.append(f"Detail:      {md['internal_reason']}")
    if transcript_excerpt:
        lines.append(f"Heard:       {transcript_excerpt[:300]}")
    if not contact_known:
        lines.append("NOTE:        we hold no way to reach this person.")

    body_text = (
        f"{summary or REASONS.get(reason, reason)}\n\n"
        + "\n".join(lines)
        + "\n\nThis is an EXCEPTION notice: the AI agent stopped and a person "
          "needs to act. Successful automated actions are not emailed — they "
          "are in the console with their audit trail.\n"
    )
    body_html = (
        f"<p>{html_escape(summary or REASONS.get(reason, reason))}</p>"
        "<table style='border-collapse:collapse;font-family:system-ui,sans-serif;"
        "font-size:0.92rem'>"
        + "".join(
            f"<tr><td style='padding:2px 12px 2px 0;color:#6b7280'>"
            f"{html_escape(l.split(':', 1)[0])}</td>"
            f"<td style='padding:2px 0'>{html_escape(l.split(':', 1)[1].strip())}</td></tr>"
            for l in lines if ':' in l)
        + "</table>"
        "<p style='color:#6b7280;font-size:0.85rem'>This is an <b>exception</b> "
        "notice: the AI agent stopped and a person needs to act. Successful "
        "automated actions are not emailed — they are in the console with their "
        "audit trail.</p>")

    subject = (f"[Action needed] {_EMAIL_ACTIONS.get(reason, reason)}"
               + (f" — {order}" if order and order != "—" else "")
               + f" ({reason})")

    # ── Staff-email Stage 3: claim BEFORE the provider call ─────────────────
    # The composition, the recipient and the send below are unchanged. What is
    # new is a ledger row around the attempt, so this send has an idempotency
    # key and a recorded provider outcome instead of only a log line and a
    # metadata note.
    #
    # FAIL-OPEN ON BOOKKEEPING, FAIL-CLOSED ON DUPLICATES. If the ledger is
    # unavailable — which it is on any database where the migration has not
    # been applied yet — we send anyway and say so. If another worker already
    # holds this send, we stop. Those two look alike and are opposites.
    claim_info = {"proceed": True, "recorded": False, "email_id": None,
                  "why": "staff-email ledger not consulted"}
    try:
        from app.core import staff_email
        claim_info = staff_email.begin_send(
            kind="escalation",
            tier=staff_email.tier_for_escalation(reason),
            ref=escalation_id,
            recipient_email=to,
            recipient_kind="role_mailbox",
            subject=subject,
            subject_ref_type="escalation",
            subject_ref_id=escalation_id,
            decision_reason=f"exception reason {reason}")
    except Exception as exc:                                   # noqa: BLE001
        logger.debug(f"[escalation] staff-email claim skipped: {exc}")

    if not claim_info.get("proceed"):
        logger.info(f"[escalation] {escalation_id[:8]} email not sent: "
                    f"{claim_info.get('why')}")
        _record_email_outcome(escalation_id, "skipped", to,
                              str(claim_info.get("why"))[:200])
        return

    state, detail = "failed", ""
    try:
        from app.agents.email.smtp_imap import send_email
        res = send_email(
            to=to,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            from_name="Conscestra Agent Ops",
        )
        staff_email_outcome = None
        try:
            from app.core import staff_email
            staff_email_outcome = staff_email.finish_send(
                claim_info.get("email_id"), res)
        except Exception as exc:                               # noqa: BLE001
            logger.debug(f"[escalation] staff-email outcome skipped: {exc}")
        if staff_email_outcome:
            logger.info(f"[escalation] {escalation_id[:8]} ledger outcome: "
                        f"{staff_email_outcome}")
        # Same evidence rule the customer emails use: success requires the
        # provider to say so, not merely the absence of an exception.
        if isinstance(res, dict) and res.get("success") is True:
            state = "accepted"
            detail = str(res.get("message") or "")[:200]
        else:
            detail = str((res or {}).get("error")
                         or (res or {}).get("message")
                         or "provider did not confirm acceptance")[:200]
    except Exception as exc:                                  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"[:200]

    if state == "accepted":
        logger.info(f"[escalation] {escalation_id[:8]} emailed to {to}")
    else:
        logger.warning(f"[escalation] {escalation_id[:8]} email {state}: {detail}")

    # If the send raised before finish_send() could run, the ledger row is
    # stranded in 'attempted' and would only be reclaimed after the lease
    # expires. Close it honestly instead: the attempt did not complete, which
    # is exactly what 'failed' means, and failed is reclaimable.
    if state != "accepted" and claim_info.get("email_id"):
        try:
            from app.core import staff_email
            if staff_email.get(
                    staff_email.idempotency_key("escalation", escalation_id)
            ) is not None:
                staff_email.mark_failed(claim_info["email_id"], detail)
        except Exception as exc:                              # noqa: BLE001
            logger.debug(f"[escalation] ledger failure note skipped: {exc}")

    _record_email_outcome(escalation_id, state, to, detail)


def _record_email_outcome(escalation_id: str, state: str, to: str,
                          detail: str) -> None:
    """Record the outcome ON the escalation. Without this the only evidence an
    email was attempted is a log line, and a log line is not a record.

    Extracted in Stage 3 so the new 'we did not send, because the ledger says
    it is already handled' path records itself the same way every other outcome
    does — a non-send that leaves no trace is the failure mode this function
    exists to prevent, and a second, quieter copy of it would have been easy to
    introduce."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE escalations
                          SET metadata = COALESCE(metadata,'{}'::jsonb)
                                         || %s::jsonb
                        WHERE escalation_id = %s::uuid""",
                    (json.dumps({"email_state": state, "email_to": to,
                                 "email_detail": detail}), escalation_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:                                  # noqa: BLE001
        logger.debug(f"[escalation] could not record email outcome: {exc}")


def _notify(escalation_id: str, reason: str, priority: str, summary: str,
            channel: Optional[str], handle: Optional[str],
            contact_known: bool) -> None:
    """In-app notification to the linked executives (the audience that already
    receives approval notifications, and the only owner-linked audience that
    exists today). Best-effort: the escalation is durable with or without it."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT employee_uuid::text FROM executives
                               WHERE is_active AND employee_uuid IS NOT NULL""")
                owners = [r[0] for r in cur.fetchall()]
                if not owners:
                    logger.debug("[escalation] no linked executives to notify")
                    return
                who = handle if contact_known else "an unidentified visitor"
                title = f"🙋 {priority.title()} escalation: a customer asked for a human"
                body = (f"{summary or REASONS.get(reason, reason)}\n\n"
                        f"Channel: {channel or 'unknown'} · Contact: {who}\n"
                        + ("" if contact_known else
                           "⚠ We have no way to reach this person — the agent has "
                           "asked them for an email.\n")
                        + "Open the Live Agent Console to pick it up.")
                for owner in owners:
                    cur.execute(
                        """INSERT INTO notifications
                             (employee_uuid, channel, status, title, body,
                              metadata, created_at)
                           VALUES (%(o)s::uuid, 'in_app', 'pending', %(t)s, %(b)s,
                                   %(m)s::jsonb, now())""",
                        {"o": owner, "t": title, "b": body,
                         "m": json.dumps({"kind": "escalation",
                                          "source": "escalation",
                                          "escalation_id": escalation_id,
                                          "priority": priority,
                                          "reason": reason})})
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # notifications.event_uuid is NOT NULL in some deployments — the
        # escalation still stands, the console still shows it.
        logger.info(f"[escalation] notification skipped (non-fatal): "
                    f"{str(exc)[:160]}")


# ============================================================================
# Work list + close-out
# ============================================================================

def list_open(limit: int = 100, include_resolved: bool = False) -> Dict[str, Any]:
    """The escalation work list: soonest deadline first, breaches flagged."""
    if not ENABLED:
        return {"ok": False, "error": "escalations disabled"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT escalation_id::text, source, reason, summary,
                           transcript_excerpt, status, priority, assigned_to,
                           conversation_id::text, channel, handle, contact_known,
                           sla_due_at, created_at, resolved_at, resolution_note,
                           (status IN ('open','assigned') AND sla_due_at < now())
                             AS breached
                    FROM escalations
                    {"" if include_resolved else "WHERE status IN ('open','assigned')"}
                    ORDER BY (status IN ('open','assigned')) DESC,
                             sla_due_at ASC
                    LIMIT %s""",
                (max(1, min(limit, 500)),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[escalation] list failed: {exc}")
        return {"ok": False, "error": f"{str(exc)[:180]} (apply sql/escalations.sql?)"}
    finally:
        conn.close()

    for r in rows:
        for k in ("sla_due_at", "created_at", "resolved_at"):
            if r.get(k):
                r[k] = r[k].isoformat()
    breached = sum(1 for r in rows if r.get("breached"))
    return {"ok": True, "count": len(rows), "breached": breached,
            "escalations": rows}


def assign(escalation_id: str, agent: str) -> Dict[str, Any]:
    return _update(escalation_id,
                   """SET status='assigned', assigned_to=%(who)s,
                          assigned_at=now(), updated_at=now()""",
                   {"who": (agent or "agent")[:120]})


def resolve(escalation_id: str, agent: str = "agent",
            note: str = "") -> Dict[str, Any]:
    return _update(escalation_id,
                   """SET status='resolved', resolved_by=%(who)s,
                          resolved_at=now(), resolution_note=%(note)s,
                          updated_at=now()""",
                   {"who": (agent or "agent")[:120], "note": (note or "")[:1000]})


def _update(escalation_id: str, set_clause: str,
            params: Dict[str, Any]) -> Dict[str, Any]:
    if not ENABLED:
        return {"ok": False, "error": "escalations disabled"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE escalations {set_clause}
                    WHERE escalation_id=%(id)s::uuid
                    RETURNING escalation_id::text, status""",
                {**params, "id": escalation_id})
            row = cur.fetchone()
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[escalation] update failed: {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "escalation not found"}
    return {"ok": True, "escalation_id": row[0], "status": row[1]}


def assign_for_conversation(conversation_id: str, agent: str,
                            note: str = "") -> Dict[str, Any]:
    """Own the live escalation on a conversation WITHOUT discharging it.

    C1 Step 4b. An obligation may only be discharged when something durable
    carries it — either a case now owns the work, or the work genuinely
    finished. A rep taking the wheel proves neither, so takeover assigns rather
    than resolves when no case exists.

    Nothing downstream changes: the console queue, the escalation list,
    sla_breaches() and platform health all already treat 'assigned' as LIVE.
    The effect is that forgetting to record the work stops being silent — the
    obligation stays on the queue with its clock running instead of being
    marked handled by someone's memory."""
    if not (ENABLED and conversation_id):
        return {"ok": False}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE escalations
                   SET status='assigned', assigned_to=%s, assigned_at=now(),
                       updated_at=now()
                   WHERE conversation_id=%s::uuid AND status='open'
                   RETURNING escalation_id::text""",
                ((agent or "agent")[:120], conversation_id))
            ids = [r[0] for r in cur.fetchall()]
        conn.commit()
        return {"ok": True, "assigned": ids}
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[escalation] conversation assign skipped: {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()


def resolve_for_conversation(conversation_id: str, agent: str,
                             note: str = "") -> Dict[str, Any]:
    """Close the live escalation on a conversation — called when a rep takes
    the conversation over or closes it, so picking up the work discharges the
    obligation without a second click."""
    if not (ENABLED and conversation_id):
        return {"ok": False}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE escalations
                   SET status='resolved', resolved_by=%s, resolved_at=now(),
                       resolution_note=%s, updated_at=now()
                   WHERE conversation_id=%s::uuid AND status IN ('open','assigned')
                   RETURNING escalation_id::text""",
                ((agent or "agent")[:120], (note or "handled in console")[:1000],
                 conversation_id))
            ids = [r[0] for r in cur.fetchall()]
        conn.commit()
        return {"ok": True, "resolved": ids}
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[escalation] conversation resolve skipped: {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()


# ============================================================================
# SLA breach — what makes the deadline real (supervisor detector, U3-adjacent)
# ============================================================================

def sla_breaches() -> List[Dict[str, Any]]:
    """Signal in the supervisor's exact shape: live escalations past their
    deadline. An SLA nobody checks is a comment, not a commitment."""
    if not ENABLED:
        return []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*),
                          count(*) FILTER (WHERE NOT contact_known),
                          min(sla_due_at)
                   FROM escalations
                   WHERE status IN ('open','assigned') AND sla_due_at < now()""")
            n, unreachable, oldest = cur.fetchone()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[escalation] breach check skipped: {exc}")
        return []
    finally:
        conn.close()

    if not n:
        return []
    return [{
        "rule": "escalation_sla_breach",
        "severity": "critical" if n >= 5 else "warning",
        "headline": (f"{n} escalation(s) past their promised follow-up time"
                     + (f" — {unreachable} with no way to reach the customer"
                        if unreachable else "")),
        "metric": "escalations_breached",
        "value": int(n),
        "owner_agent": "orchestrator",
        "recommended_action": ("Open the Live Agent Console and clear the "
                               "escalation queue; oldest deadline was "
                               f"{oldest.isoformat() if oldest else 'unknown'}."),
    }]


# ============================================================================
# Router (admin — the console's escalation surface)
# ============================================================================

router = APIRouter(tags=["escalations"])


@router.get("/escalations")
def api_list(limit: int = 100, include_resolved: bool = False):
    return list_open(limit, include_resolved)


@router.post("/escalations")
def api_open(body: Dict[str, Any]):
    b = body or {}
    return open(str(b.get("reason") or "manual"),
                str(b.get("source") or "manual"),
                summary=str(b.get("summary") or ""),
                transcript_excerpt=str(b.get("transcript_excerpt") or ""),
                conversation_id=b.get("conversation_id"),
                channel=b.get("channel"), handle=b.get("handle"),
                priority=str(b.get("priority") or "normal"),
                metadata=b.get("metadata") if isinstance(b.get("metadata"), dict) else None)


@router.post("/escalations/{escalation_id}/assign")
def api_assign(escalation_id: str, body: Dict[str, Any]):
    return assign(escalation_id, str((body or {}).get("agent") or "agent"))


@router.post("/escalations/{escalation_id}/resolve")
def api_resolve(escalation_id: str, body: Dict[str, Any]):
    b = body or {}
    return resolve(escalation_id, str(b.get("agent") or "agent"),
                   str(b.get("note") or ""))


@router.get("/escalations-status")
def api_status():
    has = False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.escalations') IS NOT NULL")
            has = bool(cur.fetchone()[0])
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return {"enabled": ENABLED, "table": has,
            "default_sla_minutes": DEFAULT_SLA_MINUTES, "reasons": REASONS}
