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

import html
import logging
from decimal import Decimal
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


# The same three tones, for a statement covering several invoices. _TIERS is
# written in the singular ("the following invoice IS seriously overdue"), which
# reads as a mistake above a table of eight. Kept as a parallel table rather than
# pluralised by string surgery: a debt claim's wording is deliberate, and
# regex-ing "invoice is" into "invoices are" is how a sentence nobody wrote ends
# up in front of a customer.
_TIERS_PLURAL = (
    (60, "urgent", "URGENT: the invoices below are seriously overdue and require "
                   "immediate attention."),
    (30, "firm",   "Our records show the invoices below remain unpaid and are now "
                   "significantly overdue."),
    (0,  "gentle", "This is a friendly reminder that the invoices below are now "
                   "past due."),
)


def _tier(days_overdue: int, plural: bool = False) -> tuple:
    table = _TIERS_PLURAL if plural else _TIERS
    for threshold, name, line in table:
        if days_overdue >= threshold:
            return name, line
    return table[-1][1], table[-1][2]


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
            #
            # The first name comes back from the SAME lookup. The caller's
            # params are not required to carry it, and the dunning loop did not:
            # it passed only to/invoice_number/amount/days_overdue, so a
            # customer the CRM knows as Alice Johnson-Smith was emailed "Hi
            # there". Resolving it here fixes every caller at once —
            # the dunning loop, a governance approval, the planner, MCP —
            # rather than requiring each to remember. Only unambiguous names are
            # used: with two contacts on one address, min() = max() fails and it
            # falls back to the neutral greeting rather than guessing which
            # person is being written to.
            cur.execute(
                """SELECT bool_and(COALESCE(is_email_verified, false)),
                          count(*),
                          CASE WHEN min(first_name) = max(first_name)
                               THEN min(first_name) END
                     FROM contacts
                    WHERE lower(email) = lower(%s)
                      AND COALESCE(is_deleted, false) = false""",
                (addr,))
            all_verified, n, first_name = cur.fetchone()
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
    return {"ok": True, "first_name": (first_name or "").strip() or None}


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


# How long one reminder covers an invoice. Mirrors agent_bus._already_dunned_sync
# EXACTLY — two windows that disagree is a second way to send twice.
REMINDER_WINDOW_HOURS = 20


def _reminder_claim(invoice_number: str) -> Dict[str, Any]:
    """Has this invoice already been reminded inside the window? Resolve it here,
    in the SENDER.

    THE DEFECT THIS CLOSES. Five customers received the same reminder twice on
    2026-08-15 — INV-000178, 180, 183, 204 and 598 — once at 12:21 from a direct
    A2A dispatch and again at 22:25 from the nightly loop. Both dunning guards
    read records that only the LOOP writes:

        agent_bus._already_dunned_sync    activities  ILIKE 'Payment reminder%'
        fn_emit_overdue_invoice_events    events      'invoice.overdue'

    This function writes neither, so it was invisible to its own idempotency.
    Any second caller — a human approving one reminder, the planner, MCP —
    reopened the same hole.

    The order path never had this problem, and the difference is structural
    rather than lucky: `order_notifications.notify()` claims its row BEFORE
    calling the provider, so the evidence is written by the thing that sends.
    Here the evidence was written by the CALLER, and a caller-held guard is only
    ever as good as the number of callers who remember to hold it.

    Returns {'ok': True, 'invoice_id': …} when the send may proceed.
    """
    from app.core.database import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT invoice_id, contact_id, balance_due FROM invoices "
                        "WHERE invoice_number = %s", (invoice_number,))
            row = cur.fetchone()
            if not row:
                # Not a known invoice — so the guard cannot be evaluated. PROCEED
                # anyway, and say so rather than pretending otherwise.
                #
                # Refusing here was the first version, and it was scope creep
                # wearing a safety costume: it broke 14 existing tests that
                # encode a deliberate prior decision — "a contact with no first
                # name must not block the reminder, the debt is the point" —
                # and it would silence real reminders whenever an invoice lookup
                # failed for any reason. The defect being fixed is DUPLICATES
                # against real invoices, which is every caller the dunning path
                # actually has. Turning that into a new way to send NOTHING
                # trades a known bug for an unknown one.
                logger.warning(
                    f"[email.sp] {invoice_number} is not a known invoice — "
                    f"sending without an idempotency claim; a repeat call "
                    f"cannot be suppressed")
                return {"ok": True, "invoice_id": None, "contact_id": None,
                        "balance": None, "idempotency": "unavailable"}
            invoice_id, contact_id, balance = row[0], row[1], row[2]
            cur.execute(
                f"""SELECT subject, created_at FROM activities
                     WHERE related_type='invoice' AND related_id=%s
                       AND subject ILIKE 'Payment reminder%%'
                       AND created_at > now() - interval '{REMINDER_WINDOW_HOURS} hours'
                     ORDER BY created_at DESC LIMIT 1""",
                (invoice_id,))
            prior = cur.fetchone()
    finally:
        conn.close()

    if prior:
        return {"ok": False, "already": True, "invoice_id": invoice_id,
                "contact_id": contact_id, "balance": balance,
                "error": f"{invoice_number} was already reminded at "
                         f"{prior[1]:%Y-%m-%d %H:%M} "
                         f"({REMINDER_WINDOW_HOURS}h window): {prior[0]}"}
    return {"ok": True, "invoice_id": invoice_id, "contact_id": contact_id,
            "balance": balance}


def _eligible_siblings(contact_id, exclude_invoice_id) -> list:
    """The customer's OTHER overdue invoices that this reminder should cover.

    WHY THE EMAIL ROLLS UP BUT THE RECORD DOES NOT. Dunning sent one email per
    invoice, and on Railway 90% of emailable customers hold more than one — 97%
    of the reminders went to them, and two were on course to receive EIGHT
    separate emails inside ten seconds. None of those is a duplicate; each
    states a distinct debt. But eight messages to one address from one sender in
    ten seconds is the pattern spam filters cluster on, so the burst threatens
    the deliverability of the reminders that matter. Real accounts-receivable
    practice sends a statement, not a letter per line.

    The unit of DELIVERY becomes the customer; the unit of RECORD stays the
    invoice. One send writes one claim row per invoice covered, so the 20-hour
    guard, _already_dunned_sync and _reminder_claim are all untouched, and an
    invoice covered by a roll-up is exactly as protected as one covered alone.

    That separation is also why the EMITTER is unchanged: the remaining
    per-invoice events still arrive, find their invoice already claimed, and
    settle without sending. The idempotency built last week does the grouping
    for free — no new event type, no change to fn_emit_overdue_invoice_events.

    Eligibility mirrors that function (materially overdue, balance > $50), so a
    roll-up never includes an invoice the loop would not have dunned by itself.
    """
    from app.core.database import get_connection

    if not contact_id:
        return []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT v.invoice_id, v.invoice_number,
                           ROUND(v.computed_balance_due::numeric, 2),
                           (CURRENT_DATE - v.due_date::date)
                      FROM accounting_invoice_pipeline v
                     WHERE v.contact_id = %s
                       AND v.invoice_id <> %s
                       AND v.payment_status IN ('unpaid', 'partial')
                       AND v.due_date < CURRENT_DATE
                       AND v.computed_balance_due > 50
                       AND NOT EXISTS (
                             SELECT 1 FROM activities a
                              WHERE a.related_type = 'invoice'
                                AND a.related_id = v.invoice_id
                                AND a.subject ILIKE 'Payment reminder%%'
                                AND a.created_at
                                    > now() - interval '{REMINDER_WINDOW_HOURS} hours')
                     ORDER BY (CURRENT_DATE - v.due_date::date) DESC""",
                (contact_id, exclude_invoice_id))
            return [{"invoice_id": r[0], "invoice_number": r[1],
                     "balance": r[2], "days_overdue": int(r[3] or 0)}
                    for r in cur.fetchall()]
    except Exception as exc:                                   # noqa: BLE001
        conn.rollback()
        # Degrade to the single-invoice reminder rather than failing the send.
        # A smaller message is never a wrong one; no message is.
        logger.warning(f"[email.sp] sibling lookup failed for contact "
                       f"{contact_id}: {exc} — sending a single-invoice reminder")
        return []
    finally:
        conn.close()


def _money(v) -> str:
    """Format a balance. Tolerant on input because callers hand this both
    Decimals from the database and display strings like "$1,234.50"."""
    if v is None:
        return "—"
    try:
        return f"${Decimal(str(v).replace('$', '').replace(',', '').strip()):,.2f}"
    except Exception:                                          # noqa: BLE001
        return str(v)


def _compose_rollup(rows: list, params: Dict[str, Any]) -> Dict[str, str]:
    """One statement covering N overdue invoices.

    Tone comes from the WORST invoice, not the first or the average. A customer
    with one debt 200 days overdue and four at 35 is in the urgent tier, and
    softening that because most of the list is merely 'firm' would understate a
    claim about money.

    A single invoice never reaches here — _compose() still renders it exactly as
    before, so the one-invoice experience and its tests are unchanged.
    """
    name = (str(params.get("contact_first") or "").strip()
            or str(params.get("account_name") or "").strip()
            or "there")
    worst = max((r["days_overdue"] for r in rows), default=0)
    _tier_name, tone = _tier(worst, plural=True)
    total = sum(Decimal(str(r["balance"] or 0)) for r in rows)
    width = max((len(str(r["invoice_number"])) for r in rows), default=10)

    lines = [
        f"  {str(r['invoice_number']):<{width}}  {_money(r['balance']):>12}  "
        f"{r['days_overdue']:>4} days past due"
        for r in rows
    ]
    text = (
        f"Hi {name},\n\n{tone}\n\n"
        f"You have {len(rows)} overdue invoices totalling {_money(total)}:\n\n"
        + "\n".join(lines)
        + f"\n\n  {'TOTAL':<{width}}  {_money(total):>12}\n\n"
        f"Please arrange payment at your earliest convenience, or reply to this "
        f"email to discuss options.\n\n"
        f"— Accounts Receivable, Conscestra CRM"
    )

    trs = "".join(
        f'<tr><td style="padding:6px 10px;border-bottom:1px solid #e5e7eb">'
        f'{html.escape(str(r["invoice_number"]))}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;text-align:right">'
        f'{_money(r["balance"])}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;text-align:right">'
        f'{r["days_overdue"]}</td></tr>'
        for r in rows
    )
    body_html = (
        f"<p>Hi {html.escape(name)},</p><p>{html.escape(tone)}</p>"
        f"<p>You have <strong>{len(rows)} overdue invoices</strong> totalling "
        f"<strong>{_money(total)}</strong>:</p>"
        f'<table style="border-collapse:collapse;font-size:0.9rem">'
        f'<thead><tr>'
        f'<th align="left"  style="padding:6px 10px;border-bottom:2px solid #0d9488">Invoice</th>'
        f'<th align="right" style="padding:6px 10px;border-bottom:2px solid #0d9488">Balance</th>'
        f'<th align="right" style="padding:6px 10px;border-bottom:2px solid #0d9488">Days past due</th>'
        f'</tr></thead><tbody>{trs}</tbody>'
        f'<tfoot><tr><td style="padding:6px 10px"><strong>Total</strong></td>'
        f'<td style="padding:6px 10px;text-align:right"><strong>{_money(total)}</strong></td>'
        f'<td></td></tr></tfoot></table>'
        f"<p>Please arrange payment at your earliest convenience, or reply to "
        f"this email to discuss options.</p>"
        f"<p>— Accounts Receivable, Conscestra CRM</p>"
    )
    return {
        "subject": f"Payment reminder — {len(rows)} overdue invoices "
                   f"({_money(total)})",
        "text": text,
        "html": body_html,
    }

def _record_reminder(covered: list, to: str, params: Dict[str, Any],
                     body_text: str) -> int:
    """Write the evidence — ONE ROW PER INVOICE COVERED — from the sender.

    `covered` is [(invoice_id, invoice_number, days_overdue), …]. A roll-up email
    covering eight invoices writes eight rows, which is what keeps the per-invoice
    20-hour guard exact: the unit of delivery is the customer, the unit of record
    stays the invoice. _already_dunned_sync and _reminder_claim are unchanged and
    cannot tell whether an invoice was covered alone or in a statement.

    All rows go in ONE transaction. A partial write would leave some invoices
    claimed and others not, so a retry would email the customer again about the
    subset that failed to record — a duplicate manufactured by the audit trail.

    Subjects keep the 'Payment reminder' prefix both guards match on, and the
    shared outcome vocabulary: "accepted by provider" is the strongest claim
    available; nothing here observes transmission or delivery.
    """
    from app.core.database import get_connection

    if not covered:
        return 0
    n = len(covered)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for invoice_id, invoice_number, days in covered:
                tier = _tier(int(days or 0))[0]
                note = (f" (one of {n} invoices in a single statement)"
                        if n > 1 else "")
                cur.execute(
                    """INSERT INTO activities
                         (type, status, subject, description, due_at, completed_at,
                          direction, related_type, related_id, account_id, contact_id,
                          channel, outcome, created_at, updated_at)
                       VALUES ('task','completed', %(subj)s, %(desc)s, now(), now(),
                               'outbound', 'invoice', %(inv)s, %(acct)s, %(ct)s,
                               'email', %(out)s, now(), now())""",
                    {"subj": f"Payment reminder ({tier}) accepted by provider "
                             f"– {invoice_number}",
                     "desc": body_text,
                     "inv": invoice_id,
                     "acct": params.get("account_id"),
                     "ct": params.get("contact_id"),
                     "out": f"auto: payment reminder accepted by provider "
                            f"→ {to}{note}"})
        conn.commit()
        return n
    except Exception as exc:                                   # noqa: BLE001
        conn.rollback()
        # The email HAS gone. Losing the record does not un-send it, and the
        # 20h guard now has nothing to see — so this is loud, not debug.
        logger.error(f"[email.sp] a reminder covering {n} invoice(s) was ACCEPTED "
                     f"but its records failed to write ({exc}) — the idempotency "
                     f"guard is blind to this send")
        return 0
    finally:
        conn.close()


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

    # Idempotency, held by the SENDER so every caller inherits it — the nightly
    # loop, a human approving one reminder, the planner, MCP. Checked after the
    # consent gate so an unverified recipient is still reported as unverified
    # rather than as a duplicate.
    claim = _reminder_claim(invoice)
    if not claim["ok"]:
        logger.info(f"[email.sp] payment reminder NOT sent for {invoice}: "
                    f"{claim['error']}")
        return {"ok": False, "skipped": "already_reminded",
                "to": to, "invoice_number": invoice, "error": claim["error"]}

    # The caller's name wins if it supplied one; otherwise use the name the
    # gate already resolved from the contact record.
    p.setdefault("contact_first", gate.get("first_name"))

    # ── Roll-up: one statement per CUSTOMER, not one letter per invoice ──────
    # Opt out with rollup=False for a caller that genuinely means this invoice
    # and no other (a human chasing one disputed line, say).
    try:
        days_primary = int(p.get("days_overdue") or 0)
    except (TypeError, ValueError):
        days_primary = 0
    covered = [(claim.get("invoice_id"), invoice, days_primary)]
    siblings = []
    if p.get("rollup", True) and claim.get("invoice_id"):
        siblings = _eligible_siblings(claim.get("contact_id"), claim["invoice_id"])

    if siblings:
        # The primary's balance comes from the DATABASE, not from the caller's
        # `amount` — that is a display string ("$480.00") and every sibling
        # carries a Decimal. Summing the two shapes raised InvalidOperation.
        rows = ([{"invoice_id": claim["invoice_id"], "invoice_number": invoice,
                  "balance": claim.get("balance"), "days_overdue": days_primary}]
                + siblings)
        msg = _compose_rollup(rows, p)
        covered = [(r["invoice_id"], r["invoice_number"], r["days_overdue"])
                   for r in rows]
        logger.info(f"[email.sp] rolling {len(rows)} overdue invoices for "
                    f"{to} into one statement")
    else:
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
    if out.get("success"):
        # Written by the sender, right after acceptance — this is what makes the
        # guard above true for the NEXT caller, whoever that turns out to be.
        # One row per invoice covered, so a roll-up protects all of them.
        if claim.get("invoice_id"):
            n = _record_reminder(covered, to, p, msg["text"])
            out["recorded"] = bool(n)
            out["invoices_covered"] = [c[1] for c in covered]
        else:
            # No invoice row to hang the record on (activities.related_id is NOT
            # NULL). Reported, never implied.
            out["recorded"] = False
            out["idempotency"] = "unavailable"
    else:
        logger.warning(f"[email.sp] payment reminder FAILED for {invoice} → "
                       f"{to}: {out.get('message') or out.get('error')}")
    return out
