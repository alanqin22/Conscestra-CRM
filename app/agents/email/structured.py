"""Structured (non-NL) entry points for the Email agent.

WHY THIS EXISTS

`email.send_payment_reminder` was registered against the `/email-chat` endpoint,
so an A2A dispatch reached it over an in-process ASGI hop. `/email-chat` is
admin-gated and `httpx.ASGITransport` does NOT bypass FastAPI dependencies, so
every call answered **403 in every environment** — the route was never entered.
No SMTP, provider or generation problem: the path was blocked before the email
operation began.

The registry already had the right tool. `Capability.sp` is a direct-execution
callable that `dispatch()` prefers over the HTTP hop for deterministic
agent-to-agent work ("params → structured data, no NL parsing, no AI, no HTTP"),
and twenty capabilities already used it. This one had simply been left on the
prose path.

WHY REMOVING THE GATE IS NOT REMOVING A CONTROL

The admin gate on `/email-chat` exists to stop EXTERNAL callers. An A2A dispatch
runs in the same process and can already import and call anything here; there is
no privilege boundary between it and this module. Authenticating that call would
have performed a check that protects nothing and charged a new credential for
it. So the hop is removed rather than paid for.

WHAT REPLACES IT

The control that actually matters for outbound mail is not "is this caller an
admin" but "may we email THIS PERSON at all". That question is answered here, in
the capability, so it holds for every caller — the dunning loop, a governance
approval, the planner, MCP — instead of being restated (or forgotten) by each.

    AGENT_BUS_AUTOSEND    should the robot act unattended?     → the CALLER's gate
    is_email_verified     may we email this person at all?     → THIS module's gate

They are deliberately not merged. A human approving a single reminder should not
be blocked because the automatic loop is switched off, and the automatic loop
must never reach an address nobody confirmed.

`email.query` is deliberately NOT given an `sp=`. It is registered as a
"natural-language passthrough to the email agent" — prose IS its contract, and a
structured callable is the wrong shape for it. It keeps the HTTP path, and
therefore still meets the 403. Nothing calls it today, and since D it reports
that honestly as `rejected` instead of as success.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Tone by age. Mirrors agent_bus._compose_reminder so an approved reminder and an
# automatic one read the same to the customer.
_TIERS = (
    (60, "urgent", "URGENT: the following invoice is seriously overdue and "
                   "requires immediate attention."),
    (30, "firm",   "Our records show the following invoice remains unpaid and "
                   "is now significantly overdue."),
    (0,  "gentle", "This is a friendly reminder that the following invoice is "
                   "now past due."),
)


def _tier(days_overdue: int) -> tuple:
    for threshold, name, line in _TIERS:
        if days_overdue >= threshold:
            return name, line
    return _TIERS[-1][1], _TIERS[-1][2]


def _recipient_is_deliverable(to: str) -> Dict[str, Any]:
    """May we email this address? Answered from the DATABASE, not the caller.

    A caller that passes `to` is asserting an address, not consent. The verified
    flag is looked up here so the guarantee cannot be lost by a caller that
    forgets to check — which is exactly what a governance approval, the planner
    or an MCP client would do.

    Reuses agent_bus._is_real_email for the format/placeholder rules rather than
    restating them: two copies of "what counts as a real address" is how the
    seed-domain corpus would leak back in.
    """
    from app.core.agent_bus import _is_real_email
    from app.core.database import get_connection

    addr = (to or "").strip()
    if not addr:
        return {"ok": False, "reason": "no recipient address"}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Fail closed on ambiguity: if ANY contact holding this address is
            # unverified, treat the address as unverified. A duplicate row must
            # not be a way to acquire consent.
            cur.execute(
                """SELECT bool_and(COALESCE(is_email_verified, false)),
                          count(*)
                     FROM contacts
                    WHERE lower(email) = lower(%s)
                      AND COALESCE(is_deleted, false) = false""",
                (addr,))
            all_verified, n = cur.fetchone()
    finally:
        conn.close()

    if not n:
        return {"ok": False,
                "reason": f"{addr} is not a known contact — consent cannot be "
                          f"established"}
    if not _is_real_email(addr, bool(all_verified)):
        return {"ok": False,
                "reason": f"{addr} is not a verified, deliverable recipient "
                          f"(is_email_verified is false, or the domain is a "
                          f"reserved placeholder such as example.com)"}
    return {"ok": True}


# @seed.agentorc.ca is DELIVERABLE ON PURPOSE and is not filtered here.
#
# It is a catch-all this project owns, created so the synthetic corpus would
# stop pointing at RFC 2606 addresses that belong to nobody. Verification, not
# the domain, is the gate: a seed contact carries is_email_verified=false and is
# refused above like any other, and one that has been deliberately verified is
# how an end-to-end send is exercised into a mailbox we control.
#
# An earlier version of the message above claimed this function rejected
# "placeholder/seed" domains. It never did — `seed.agentorc.ca` is not in
# _PLACEHOLDER_EMAIL_DOMAINS and matches none of the reserved suffixes. Wording
# that overstates a control is the same defect class this module exists because
# of, so it is corrected rather than left as a comforting sentence.


def _compose(params: Dict[str, Any]) -> Dict[str, str]:
    """Deterministic composition. No LLM: a dunning notice states a debt, and
    the wording of a debt claim is not something to regenerate per send."""
    invoice = str(params.get("invoice_number") or "").strip()
    amount = str(params.get("amount") or "").strip()
    account = str(params.get("account_name") or "").strip()
    name = str(params.get("contact_first") or "").strip() or account or "there"
    try:
        days = int(params.get("days_overdue") or 0)
    except (TypeError, ValueError):
        days = 0

    _name, tone = _tier(days)
    lines = [f"Invoice:        {invoice or '—'}"]
    if account:
        lines.append(f"Account:        {account}")
    if amount:
        lines.append(f"Balance due:    {amount}")
    lines.append(f"Days past due:  {days}")
    detail = "\n".join(f"  {ln}" for ln in lines)

    text = (f"Hi {name},\n\n{tone}\n\n{detail}\n\n"
            f"Please arrange payment at your earliest convenience, or reply to "
            f"this email to discuss options.\n\n"
            f"— Accounts Receivable, Conscestra CRM")
    html = ("<p>Hi {n},</p><p>{t}</p><ul>{rows}</ul>"
            "<p>Please arrange payment at your earliest convenience, or reply "
            "to this email to discuss options.</p>"
            "<p>— Accounts Receivable, Conscestra CRM</p>").format(
        n=name, t=tone,
        rows="".join(f"<li>{ln.strip()}</li>" for ln in lines))
    return {"subject": f"Payment reminder — {invoice}" if invoice
                       else "Payment reminder",
            "text": text, "html": html}


def send_payment_reminder_sp(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Send an overdue-invoice payment reminder. Params → send → truthful result.

    Returns the provider's own answer. Every refusal path returns
    `{'ok': False, 'error': ...}` rather than raising, which a2a.classify_sp_result
    reads as REJECTED — so a blocked, unverified or refused send can never be
    recorded as 'sent'. That is the whole point of the audit this came from: no
    execution path may treat the absence of an error as evidence of success.
    """
    p = dict(params or {})
    to = str(p.get("to") or "").strip()
    invoice = str(p.get("invoice_number") or p.get("invoice_id") or "").strip()

    if not to:
        return {"ok": False, "error": "to is required"}
    if not invoice:
        return {"ok": False, "error": "invoice_number is required"}

    gate = _recipient_is_deliverable(to)
    if not gate["ok"]:
        logger.info(f"[email.sp] payment reminder NOT sent for {invoice}: "
                    f"{gate['reason']}")
        return {"ok": False, "skipped": "unverified_recipient",
                "to": to, "invoice_number": invoice, "error": gate["reason"]}

    msg = _compose(p)
    from app.agents.email.smtp_imap import send_email
    # Transactional, NOT commercial. A payment reminder concerns an existing
    # debt under an existing relationship; CASL's commercial path would attach
    # an unsubscribe link, and "unsubscribe from invoice reminders" is not a
    # choice this system should offer. Matches the order-confirmation path,
    # which sends transactionally for the same reason.
    result = send_email(to=to, subject=msg["subject"],
                        body_html=msg["html"], body_text=msg["text"],
                        from_name="Conscestra CRM")
    out = dict(result or {})
    out.setdefault("to", to)
    out["invoice_number"] = invoice
    if not out.get("success"):
        logger.warning(f"[email.sp] payment reminder FAILED for {invoice} → "
                       f"{to}: {out.get('message') or out.get('error')}")
    return out
