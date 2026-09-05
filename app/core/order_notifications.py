"""Automated customer emails for four order lifecycle events.

    order.created    →  Order Confirmation — {order_number}
    order.shipped    →  Your Order {order_number} Has Shipped
    order.delivered  →  Your Order {order_number} Has Been Delivered
    order.cancelled  →  Your Order {order_number} Has Been Cancelled

WHERE THE TRIGGER LIVES, AND WHY NOT HERE

Nothing in this module decides that an event happened. The authority is
`trgfn_order_lifecycle_notify` (sql/order_lifecycle_notifications.sql), an AFTER
trigger on `orders` that fires inside the committing transaction of whichever
client wrote the row — the order agent, the store checkout, the daily
fn_advance_order_statuses() run, or an API written next year. A browser click is
not a business event, and neither is one stored procedure among several: only
the row transition is common to all of them.

The trigger detects the TRANSITION, never the current value. A re-save, a
double-click, or a batch UPDATE that rewrites 'shipped' over 'shipped' emits
nothing at all. (The legacy path did emit one — event 5f47b859, 2026-07-21,
diff status old=shipped new=shipped.)

THE IDEMPOTENCY GUARANTEE

`order_notifications` carries UNIQUE (order_id, event_type). The row is CLAIMED
before the provider is called, so every duplicate delivery path — a replayed
queue row, a restart, a worker retry, a second consumer replica, a repeated API
call — converges on the same row instead of on a second email.

This replaces an idempotency check that read activity SUBJECT TEXT
(`subject ILIKE 'Order shipped email%'`) and ran AFTER the send. That pattern
matched drafted rows as well as sent ones, so a failed send blocked its own
retry forever, and a crash between the send and the write produced a duplicate.

WHAT THE STATES MEAN, AND WHAT IS DELIBERATELY MISSING

    queued     claimed; provider not called (or autosend is off — composed only)
    attempted  the provider call was made and gave no usable answer
    accepted   the provider took the message for transmission
    failed     the attempt did not complete — RETRYABLE under the same key
    skipped    deliberately not sent (no address, unverified, guard, opt-out)

There is no `sent` and no `delivered`, in this module or in the schema. Provider
acceptance is the strongest evidence available in-process; delivery is
unknowable without webhook or bounce ingestion, which this system does not have.
A CHECK constraint enforces that accepted_at exists if and only if the state is
`accepted`, so a failure cannot acquire the shape of a success by accident.

RETRY

A retryable failure RAISES. The agent bus already owns retry — locking,
exponential backoff, AGENT_BUS_MAX_ATTEMPTS — so raising hands the event back to
a mechanism that will redeliver it, and redelivery lands on the same claimed row
under the same key. Retry therefore cannot duplicate the email.
"""

from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.core.database import get_connection

logger = logging.getLogger("order_notifications")

# ── The three business events, and the template each one uses ────────────────
EVENT_TEMPLATES: Dict[str, str] = {
    "order.created":   "order_created",
    "order.shipped":   "order_shipped",
    "order.delivered": "order_delivered",
    "order.cancelled": "order_cancelled",
}

# Terminal states. A row in one of these is never re-sent.
#   accepted — the customer has the message.
#   skipped  — we decided not to send, and the decision will not change by
#              trying again (no address, unverified recipient, guard, opt-out).
TERMINAL_STATES = frozenset({"accepted", "skipped"})

COMPANY_NAME = "Conscestra CRM"
SUPPORT_EMAIL = os.getenv("EMAIL_ADDRESS", "info@agentorc.ca")
SUPPORT_HOURS = "Mon–Fri, 9:00 AM – 6:00 PM ET"


class RetryableNotificationError(RuntimeError):
    """A provider/transport failure. Raised so the agent bus redelivers the
    event; redelivery re-claims the SAME (order_id, event_type) row."""


def _autosend() -> bool:
    """Read at call time, not import time — the flag is environment state, and
    a test that sets it must not depend on import order.

    Delegates to `agent_bus.autosend_allowed()`, which asks the flag AND
    whether this process is the deployment that was meant to send. This module
    used to read the environment variable itself, and that second reader is how
    a laptop sent 50 order emails in a night that Railway's ledger knew nothing
    about: same code, same BCC, different database. Imported lazily, matching
    how this module already reaches `_is_real_email`."""
    from app.core.agent_bus import autosend_allowed
    return autosend_allowed()


# ============================================================================
# CONTEXT — everything a template may say about the order, and nothing else
# ============================================================================

def load_context(order_id: str) -> Optional[Dict[str, Any]]:
    """Load the order, its items, its buyer and its shipping address.

    Only columns that EXIST are read. `orders` has no carrier, tracking_number,
    shipped_at or delivered_at column, so those never enter the context and the
    templates below cannot print them. That is the mechanism by which tracking
    information is not fabricated: there is nothing to fabricate it from.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.order_id, o.order_number, o.status, o.order_date,
                          o.total_amount, o.subtotal_amount, o.tax_amount,
                          o.currency, o.account_id, o.contact_id, o.updated_at,
                          c.email,
                          NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' ||
                                      COALESCE(c.last_name,'')), '') AS contact_name,
                          -- Verified = this contact is verified, OR the same
                          -- address is verified on any contact row. Mirrors the
                          -- fallback the order-email path already used, for the
                          -- case where checkout linked a duplicate contact that
                          -- shares the address a human actually confirmed.
                          (COALESCE(c.is_email_verified, false)
                           OR EXISTS (SELECT 1 FROM contacts c2
                                       WHERE c.email IS NOT NULL
                                         AND lower(c2.email) = lower(c.email)
                                         AND c2.is_email_verified)) AS verified,
                          a.account_name,
                          -- SAME resolution chain sp_orders uses for
                          -- 'shipping_address' on the order detail view. Reading
                          -- orders.shipping_address_id alone would disagree with
                          -- what the CRM displays, and it is not even reliable:
                          -- trgfn_order_items_update_order overwrites that column
                          -- with NULL whenever an order has a contact but no
                          -- contact-level 'shipping' address, discarding a value
                          -- the caller explicitly set. An email must ship to the
                          -- address the record means, not to a column that a
                          -- line-item edit may have cleared.
                          COALESCE(
                            CASE WHEN sa.address_id IS NOT NULL THEN
                              jsonb_build_object('line1', sa.line1, 'line2', sa.line2,
                                'city', sa.city, 'province', sa.province,
                                'postal_code', sa.postal_code, 'country', sa.country)
                            END,
                            (SELECT jsonb_build_object('line1', ad.line1, 'line2', ad.line2,
                                'city', ad.city, 'province', ad.province,
                                'postal_code', ad.postal_code, 'country', ad.country)
                               FROM addresses ad
                              WHERE ad.parent_type='order' AND ad.parent_id = o.order_id
                                AND lower(ad.label)='shipping'
                              ORDER BY ad.is_default DESC LIMIT 1),
                            (SELECT jsonb_build_object('line1', ad.line1, 'line2', ad.line2,
                                'city', ad.city, 'province', ad.province,
                                'postal_code', ad.postal_code, 'country', ad.country)
                               FROM addresses ad
                              WHERE ad.parent_type='contact' AND ad.parent_id = o.contact_id
                                AND lower(ad.label)='shipping'
                              ORDER BY ad.is_default DESC LIMIT 1),
                            (SELECT jsonb_build_object('line1', ad.line1, 'line2', ad.line2,
                                'city', ad.city, 'province', ad.province,
                                'postal_code', ad.postal_code, 'country', ad.country)
                               FROM addresses ad
                              WHERE ad.parent_type='contact' AND ad.parent_id = o.contact_id
                                AND lower(ad.label)='billing'
                              ORDER BY ad.is_default DESC LIMIT 1),
                            (SELECT jsonb_build_object('line1', ad.line1, 'line2', ad.line2,
                                'city', ad.city, 'province', ad.province,
                                'postal_code', ad.postal_code, 'country', ad.country)
                               FROM addresses ad
                              WHERE ad.parent_type='account' AND ad.parent_id = o.account_id
                              ORDER BY ad.is_default DESC LIMIT 1)
                          ) AS shipping
                     FROM orders o
                     LEFT JOIN contacts  c  ON c.contact_id = o.contact_id
                     LEFT JOIN accounts  a  ON a.account_id = o.account_id
                     LEFT JOIN addresses sa ON sa.address_id = o.shipping_address_id
                    WHERE o.order_id = %s::uuid""",
                (str(order_id),))
            row = cur.fetchone()
            if not row:
                return None

            ship = row[15] or {}
            ctx: Dict[str, Any] = {
                "order_id":      str(row[0]),
                "order_number":  row[1] or "(no number)",
                "status":        row[2],
                "order_date":    row[3],
                "total_amount":  row[4],
                "subtotal":      row[5],
                "tax_amount":    row[6],
                "currency":      row[7] or "CAD",
                "account_id":    row[8],
                "contact_id":    row[9],
                "updated_at":    row[10],
                "contact_email": (row[11] or "").strip() or None,
                "contact_name":  row[12],
                "verified":      bool(row[13]),
                "account_name":  row[14],
                "shipping_address": [p for p in (
                    ship.get("line1"), ship.get("line2"),
                    ", ".join(x for x in (ship.get("city"), ship.get("province"),
                                          ship.get("postal_code")) if x),
                    ship.get("country")) if p],
            }

            cur.execute(
                """SELECT COALESCE(oi.description, p.product_name, 'Item'),
                          p.sku, oi.quantity, oi.line_total
                     FROM order_items oi
                     LEFT JOIN products p ON p.product_id = oi.product_id
                    WHERE oi.order_id = %s::uuid
                    ORDER BY oi.created_at, oi.order_item_id""",
                (str(order_id),))
            ctx["items"] = [
                {"name": r[0], "sku": r[1], "quantity": r[2], "line_total": r[3]}
                for r in cur.fetchall()
            ]

            # CONFIRMED money only, and only for the cancellation template.
            # A cancellation email must never promise a refund that no payment
            # record supports. Measured 2026-08-19: zero of the 55 cancellable
            # orders (ready/processing) carry an invoice or a payment, so the
            # refund block is normally absent — by evidence, not by omission.
            # Same doctrine as the missing carrier/tracking columns above: the
            # template cannot print what the context does not hold.
            cur.execute(
                """SELECT COALESCE(SUM(amount), 0)::numeric,
                          COUNT(*),
                          MAX(payment_method)
                     FROM payments
                    WHERE order_id = %s::uuid
                      AND COALESCE(is_deleted, false) = false
                      AND confirmed_at IS NOT NULL
                      AND refunded_at  IS NULL""",
                (str(order_id),))
            prow = cur.fetchone()
            ctx["paid_amount"] = prow[0] if prow and prow[1] else None
            ctx["paid_method"] = prow[2] if prow and prow[1] else None
        return ctx
    finally:
        conn.close()


# ============================================================================
# COMPOSITION — deterministic, no LLM
# ============================================================================
# These messages assert facts about a customer's money and their delivery. They
# are rendered from the record, not regenerated per send, so two customers with
# the same order state receive the same statement of it.

def _money(v: Any, currency: str = "CAD") -> str:
    if v is None:
        return "—"
    try:
        return f"${Decimal(str(v)):,.2f} {currency}"
    except Exception:                                     # noqa: BLE001
        return str(v)


def _day(v: Any) -> str:
    if isinstance(v, datetime):
        return v.strftime("%B %d, %Y")
    return str(v) if v else "—"


def _greeting_name(ctx: Dict[str, Any]) -> str:
    """The customer's own name where we have it. Never 'Hi there' when the CRM
    knows who this is — that failure has already been paid for once."""
    return (ctx.get("contact_name") or ctx.get("account_name") or "there").strip()


def _addressee_lines(ctx: Dict[str, Any]) -> List[str]:
    """WHO the parcel is addressed to, above the street address.

    An address block with no name is not a shipping label — it is a location.
    The customer cannot tell whether the parcel is addressed to them, to a
    colleague, or to a company mailroom, which is exactly the question the block
    exists to answer.

    Person first, then company when it differs: a B2B parcel needs both, and
    repeating "Bennett Foods / Bennett Foods" for an account named after its
    only contact is noise. Falls back to whichever is known and returns nothing
    when neither is — deliberately NOT _greeting_name(), whose 'there' fallback
    is fine at the top of a letter and absurd on a parcel.
    """
    name = (ctx.get("contact_name") or "").strip()
    account = (ctx.get("account_name") or "").strip()
    lines = [n for n in (name, account) if n]
    if len(lines) == 2 and lines[0].lower() == lines[1].lower():
        lines = lines[:1]
    return lines


def _address_text(ctx: Dict[str, Any], label: str) -> str:
    """The '<label>:\\n  Name\\n  Street…' block, or nothing when no address."""
    ship = ctx.get("shipping_address") or []
    if not ship:
        return ""
    body = _addressee_lines(ctx) + list(ship)
    return f"\n\n{label}:\n" + "\n".join(f"  {ln}" for ln in body)


def _address_html(ctx: Dict[str, Any], label: str) -> str:
    ship = ctx.get("shipping_address") or []
    if not ship:
        return ""
    names = _addressee_lines(ctx)
    rendered = ([f"<strong>{html.escape(str(n))}</strong>" for n in names]
                + [html.escape(str(ln)) for ln in ship])
    return (f'<p style="margin-top:16px"><strong>{html.escape(label)}</strong><br>'
            + "<br>".join(rendered) + '</p>')


def _items_text(items: List[Dict[str, Any]], currency: str) -> str:
    if not items:
        return "  (no line items recorded on this order)"
    lines = []
    for it in items:
        qty = it["quantity"]
        qty = int(qty) if qty is not None and float(qty) == int(float(qty)) else qty
        sku = f" [{it['sku']}]" if it.get("sku") else ""
        lines.append(f"  {qty} × {it['name']}{sku}"
                     f"{'  —  ' + _money(it['line_total'], currency) if it.get('line_total') is not None else ''}")
    return "\n".join(lines)


def _items_html(items: List[Dict[str, Any]], currency: str) -> str:
    if not items:
        return ('<tr><td colspan="3" style="padding:8px;color:#6b7280">'
                'No line items recorded on this order.</td></tr>')
    rows = []
    for it in items:
        qty = it["quantity"]
        qty = int(qty) if qty is not None and float(qty) == int(float(qty)) else qty
        sku = (f'<br><span style="color:#6b7280;font-size:0.8rem">'
               f'{html.escape(str(it["sku"]))}</span>') if it.get("sku") else ""
        rows.append(
            f'<tr><td style="padding:8px;border-bottom:1px solid #e5e7eb">'
            f'{html.escape(str(it["name"]))}{sku}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;text-align:center">{qty}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;text-align:right">'
            f'{_money(it.get("line_total"), currency)}</td></tr>')
    return "".join(rows)


def _shell(title: str, intro: str, blocks: str, closing: str) -> str:
    return (
        '<div style="font-family:system-ui,-apple-system,sans-serif;max-width:600px;'
        'margin:0 auto;padding:32px 24px;color:#111827">'
        f'<h2 style="color:#0d9488;margin:0 0 4px">{html.escape(COMPANY_NAME)}</h2>'
        f'<h3 style="margin:0 0 16px;font-weight:600">{html.escape(title)}</h3>'
        f'<p>{intro}</p>{blocks}'
        f'<p style="color:#6b7280;font-size:0.875rem;margin-top:24px">{closing}</p>'
        f'<p style="color:#6b7280;font-size:0.8rem;border-top:1px solid #e5e7eb;'
        f'padding-top:12px;margin-top:20px">{html.escape(COMPANY_NAME)}<br>'
        f'Customer service: <a href="mailto:{html.escape(SUPPORT_EMAIL)}">'
        f'{html.escape(SUPPORT_EMAIL)}</a><br>{html.escape(SUPPORT_HOURS)}</p></div>')


def _footer_text() -> str:
    return (f"\n\n—\n{COMPANY_NAME}\nCustomer service: {SUPPORT_EMAIL}\n"
            f"{SUPPORT_HOURS}\n")


def _status_button(order_id: str) -> Tuple[str, str]:
    """(text, html) for the Order Status button, or ('', '') when the link
    cannot be signed.

    OMISSION IS THE CORRECT FAILURE. order_status.order_status_url returns None
    when no signing secret is configured, and the only alternatives to omitting
    the button would be to print an unsigned link (which the page must reject,
    so the customer clicks through to an error) or to sign with something
    guessable (which would let anyone open anyone's order). The email still
    sends, and every other route to us is unchanged.

    The button is a table, not a styled <a>, because Outlook renders CSS
    padding on inline anchors inconsistently and would otherwise produce a bare
    word where a button should be. The URL is repeated as text underneath for
    clients that strip the button entirely.
    """
    try:
        from app.core.order_status import order_status_url
        url = order_status_url(order_id)
    except Exception as exc:                              # noqa: BLE001
        logger.warning(f"[order_notifications] status link unavailable: {exc}")
        url = None
    if not url:
        return "", ""

    safe = html.escape(url, quote=True)
    text = (f"\n\nTrack or manage this order:\n  {url}\n"
            f"  You can view its current status there, cancel it while it has "
            f"not yet shipped, or read our return policy once it has.")
    button = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" style="margin:24px 0"><tr>'
        f'<td align="center" bgcolor="#0d9488" style="border-radius:6px">'
        f'<a href="{safe}" style="display:inline-block;padding:12px 28px;'
        f'font-family:system-ui,-apple-system,sans-serif;font-size:0.95rem;'
        f'font-weight:600;color:#ffffff;text-decoration:none;border-radius:6px">'
        f'Order Status</a></td></tr></table>'
        f'<p style="color:#6b7280;font-size:0.8rem;margin:-12px 0 0">'
        f'View the current status, cancel it while it has not yet shipped, or '
        f'read our return policy once it has.<br>'
        f'If the button does not work, copy this link: {html.escape(url)}</p>')
    return text, button


def _refund_block(ctx: Dict[str, Any]) -> Tuple[str, str]:
    """(text, html) describing the refund — or ('', '') when there is nothing
    truthful to say.

    A cancellation email is the moment a customer most wants to hear about their
    money, which is exactly why this must not guess. If no CONFIRMED, un-refunded
    payment exists on the order, the section is omitted entirely rather than
    softened into "any refund due will be processed" — a sentence that sounds
    reassuring and commits the business to nothing it can verify.

    Note what is deliberately NOT here: a date, a duration, or "5-10 business
    days". The system holds no refund SLA, so printing one would be invention of
    the same kind as a fabricated tracking number.
    """
    amount = ctx.get("paid_amount")
    if not amount:
        return "", ""
    money = _money(amount, ctx.get("currency") or "CAD")
    method = (ctx.get("paid_method") or "").strip()
    via = (f" to your original payment method ({method})" if method
           else " to your original payment method")
    text = (f"\n\nRefund:\n  We hold a confirmed payment of {money} on this "
            f"order. It will be refunded{via}. You will receive a separate "
            f"confirmation once the refund has been issued.")
    html_block = (
        f'<p style="margin-top:16px"><strong>Refund</strong><br>'
        f'We hold a confirmed payment of {html.escape(money)} on this order. '
        f'It will be refunded{html.escape(via)}. You will receive a separate '
        f'confirmation once the refund has been issued.</p>')
    return text, html_block


def compose(ctx: Dict[str, Any], event_type: str) -> Tuple[str, str, str]:
    """(subject, body_text, body_html) for one lifecycle event.

    Every optional element is present only when the record holds it. There is no
    branch anywhere below that invents a carrier, a tracking number or a
    delivery estimate — the system does not store them, so the templates omit
    the section entirely rather than printing a plausible placeholder.
    """
    name = _greeting_name(ctx)
    num = ctx["order_number"]
    cur = ctx["currency"]
    # The self-service link. Present on the three lifecycle emails; deliberately
    # absent from the cancellation email, which is terminal — there is nothing
    # left to manage, and a button inviting the customer back to a dead order
    # would read as though the cancellation had not taken.
    btn_t, btn_h = _status_button(ctx["order_id"])
    total = _money(ctx.get("total_amount"), cur)
    ship = ctx.get("shipping_address") or []

    if event_type == "order.created":
        subject = f"Order Confirmation — {num}"
        addr_t = _address_text(ctx, "Shipping to")
        body_text = (
            f"Hi {name},\n\n"
            f"Thank you for your order. We have received it and it is now being "
            f"processed.\n\n"
            f"Order number: {num}\n"
            f"Order date:   {_day(ctx.get('order_date'))}\n\n"
            f"Items ordered:\n{_items_text(ctx['items'], cur)}\n\n"
            f"Order total:  {total}"
            f"{addr_t}\n\n"
            f"What happens next: we will prepare your order for dispatch and "
            f"email you again as soon as it ships."
            + _footer_text())
        addr_h = _address_html(ctx, "Shipping to")
        body_html = _shell(
            f"Order Confirmation — {html.escape(num)}",
            "Thank you for your order. We have received it and it is now being processed.",
            f'<div style="background:#f0fdfa;border-radius:8px;padding:12px 16px;margin:16px 0">'
            f'<strong>Order number:</strong> {html.escape(num)}<br>'
            f'<strong>Order date:</strong> {html.escape(_day(ctx.get("order_date")))}</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:0.9rem">'
            f'<thead><tr><th align="left" style="padding:8px;border-bottom:2px solid #0d9488">Item</th>'
            f'<th style="padding:8px;border-bottom:2px solid #0d9488">Qty</th>'
            f'<th align="right" style="padding:8px;border-bottom:2px solid #0d9488">Total</th></tr></thead>'
            f'<tbody>{_items_html(ctx["items"], cur)}</tbody>'
            f'<tfoot><tr><td colspan="2" style="padding:8px;text-align:right">'
            f'<strong>Order total</strong></td>'
            f'<td style="padding:8px;text-align:right"><strong>{total}</strong></td></tr></tfoot>'
            f'</table>{addr_h}{btn_h}',
            "What happens next: we will prepare your order for dispatch and email "
            "you again as soon as it ships.")

    elif event_type == "order.shipped":
        subject = f"Your Order {num} Has Shipped"
        # Ship date is the moment of the transition we are reacting to. It is
        # read from updated_at rather than a shipped_at column, because there is
        # no shipped_at column — and inventing one for the email would be
        # inventing the fact it reports.
        ship_day = _day(ctx.get("updated_at"))
        addr_t = _address_text(ctx, "Shipping to")
        body_text = (
            f"Hi {name},\n\n"
            f"Good news — your order has shipped and is on its way.\n\n"
            f"Order number: {num}\n"
            f"Ship date:    {ship_day}\n\n"
            f"Items shipped:\n{_items_text(ctx['items'], cur)}"
            f"{addr_t}\n\n"
            f"Carrier and tracking details are not recorded for this order. If "
            f"you need them, reply to this email and we will follow up."
            + _footer_text())
        addr_h = _address_html(ctx, "Shipping to")
        body_html = _shell(
            f"Your Order {html.escape(num)} Has Shipped",
            "Good news — your order has shipped and is on its way.",
            f'<div style="background:#f0fdfa;border-radius:8px;padding:12px 16px;margin:16px 0">'
            f'<strong>Order number:</strong> {html.escape(num)}<br>'
            f'<strong>Ship date:</strong> {html.escape(ship_day)}</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:0.9rem">'
            f'<thead><tr><th align="left" style="padding:8px;border-bottom:2px solid #0d9488">Item shipped</th>'
            f'<th style="padding:8px;border-bottom:2px solid #0d9488">Qty</th>'
            f'<th align="right" style="padding:8px;border-bottom:2px solid #0d9488">Total</th></tr></thead>'
            f'<tbody>{_items_html(ctx["items"], cur)}</tbody></table>{addr_h}{btn_h}',
            "Carrier and tracking details are not recorded for this order. If you "
            "need them, reply to this email and we will follow up.")

    elif event_type == "order.delivered":
        subject = f"Your Order {num} Has Been Delivered"
        del_day = _day(ctx.get("updated_at"))
        addr_t = _address_text(ctx, "Delivered to")
        body_text = (
            f"Hi {name},\n\n"
            f"Your order has been marked as delivered.\n\n"
            f"Order number:  {num}\n"
            f"Delivery date: {del_day}\n\n"
            f"Items delivered:\n{_items_text(ctx['items'], cur)}"
            f"{addr_t}\n\n"
            f"If anything is missing or damaged, reply to this email or contact "
            f"customer service below and we will put it right."
            + _footer_text())
        addr_h = _address_html(ctx, "Delivered to")
        body_html = _shell(
            f"Your Order {html.escape(num)} Has Been Delivered",
            "Your order has been marked as delivered.",
            f'<div style="background:#f0fdfa;border-radius:8px;padding:12px 16px;margin:16px 0">'
            f'<strong>Order number:</strong> {html.escape(num)}<br>'
            f'<strong>Delivery date:</strong> {html.escape(del_day)}</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:0.9rem">'
            f'<thead><tr><th align="left" style="padding:8px;border-bottom:2px solid #0d9488">Item delivered</th>'
            f'<th style="padding:8px;border-bottom:2px solid #0d9488">Qty</th>'
            f'<th align="right" style="padding:8px;border-bottom:2px solid #0d9488">Total</th></tr></thead>'
            f'<tbody>{_items_html(ctx["items"], cur)}</tbody></table>{addr_h}{btn_h}',
            "If anything is missing or damaged, reply to this email or contact "
            "customer service below and we will put it right.")
    elif event_type == "order.cancelled":
        subject = f"Your Order {num} Has Been Cancelled"
        # THE cancellation time is the DB `updated_at` returned by the guarded
        # UPDATE — never a Python clock, and never the moment this email is
        # composed. See voice_support.cancel_order_sp.
        when = _day(ctx.get("updated_at"))
        refund_t, refund_h = _refund_block(ctx)
        body_text = (
            f"Hi {name},\n\n"
            f"Your order has been cancelled as you requested, and nothing "
            f"further is needed from you.\n\n"
            f"Order number:      {num}\n"
            f"Cancelled on:      {when}\n"
            f"Order total:       {total}\n\n"
            f"Items cancelled:\n{_items_text(ctx['items'], cur)}"
            f"{refund_t}\n\n"
            f"If you did not request this cancellation, contact customer "
            f"service below straight away."
            + _footer_text())
        body_html = _shell(
            f"Your Order {html.escape(num)} Has Been Cancelled",
            "Your order has been cancelled as you requested, and nothing "
            "further is needed from you.",
            f'<div style="background:#fef2f2;border-radius:8px;padding:12px 16px;margin:16px 0">'
            f'<strong>Order number:</strong> {html.escape(num)}<br>'
            f'<strong>Cancelled on:</strong> {html.escape(when)}<br>'
            f'<strong>Order total:</strong> {html.escape(total)}</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:0.9rem">'
            f'<thead><tr><th align="left" style="padding:8px;border-bottom:2px solid #0d9488">Item cancelled</th>'
            f'<th style="padding:8px;border-bottom:2px solid #0d9488">Qty</th>'
            f'<th align="right" style="padding:8px;border-bottom:2px solid #0d9488">Total</th></tr></thead>'
            f'<tbody>{_items_html(ctx["items"], cur)}</tbody></table>{refund_h}',
            "If you did not request this cancellation, contact customer service "
            "below straight away.")

    else:
        raise ValueError(f"not a customer lifecycle event: {event_type!r}")

    # The plain-text half of the button. Appended by matching the footer this
    # module itself produced, rather than by rebuilding four message bodies —
    # and guarded by endswith, so a future template that ends differently
    # silently keeps its text intact instead of being corrupted.
    if btn_t and event_type != "order.cancelled":
        footer = _footer_text()
        if body_text.endswith(footer):
            body_text = body_text[:-len(footer)] + btn_t + footer
        else:
            logger.warning("[order_notifications] %s text body does not end "
                           "with the standard footer — status link omitted "
                           "from the plain-text part", event_type)

    return subject, body_text, body_html


# ============================================================================
# LEDGER — the idempotency key and the audit record are the same row
# ============================================================================

_COLS = ("notification_id, order_id, event_type, idempotency_key, state, "
         "template, subject, recipient_email, contact_id, account_id, "
         "provider, provider_message_id, provider_response, failure_reason, "
         "attempts, first_attempted_at, last_attempted_at, accepted_at, "
         "event_uuid, correlation_id, created_at, updated_at")


def _row(cur) -> Optional[Dict[str, Any]]:
    r = cur.fetchone()
    if not r:
        return None
    return dict(zip([c.strip() for c in _COLS.split(",")], r))


def claim(order_id: str, event_type: str, event_uuid: Optional[str] = None,
          correlation_id: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
    """Take ownership of (order_id, event_type). Returns (row, is_new).

    The INSERT runs BEFORE any provider call. That ordering is the whole
    guarantee: if this process dies mid-send, the next delivery of the event
    finds a claimed row rather than an empty table, and converges on it.

    ON CONFLICT DO NOTHING plus a follow-up SELECT — rather than a SELECT then
    an INSERT — so two consumer replicas racing on the same event cannot both
    conclude the row is absent.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO order_notifications
                      (order_id, event_type, state, template, event_uuid, correlation_id)
                    VALUES (%s::uuid, %s, 'queued', %s, %s::uuid, %s::uuid)
                    ON CONFLICT (order_id, event_type) DO NOTHING
                    RETURNING {_COLS}""",
                (str(order_id), event_type, EVENT_TEMPLATES.get(event_type),
                 event_uuid, correlation_id))
            row = _row(cur)
            if row is not None:
                conn.commit()
                return row, True
            cur.execute(f"SELECT {_COLS} FROM order_notifications "
                        "WHERE order_id=%s::uuid AND event_type=%s",
                        (str(order_id), event_type))
            row = _row(cur)
        conn.commit()
        return row, False
    finally:
        conn.close()


def _update(notification_id: str, **fields) -> Dict[str, Any]:
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    params = dict(fields, nid=str(notification_id))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE order_notifications SET {sets} "
                        f"WHERE notification_id = %(nid)s::uuid RETURNING {_COLS}",
                        params)
            row = _row(cur)
        conn.commit()
        return row
    finally:
        conn.close()


# How long one worker owns a send before it is presumed dead and reclaimable.
# Longer than any plausible SMTP/HTTP timeout (send_email caps at 15s), short
# enough that a killed process does not strand a notification for a shift.
ATTEMPT_LEASE = "15 minutes"


def acquire(notification_id: str) -> Optional[Dict[str, Any]]:
    """Take EXCLUSIVE ownership of this send. Returns None if someone else has it.

    THE BUG THIS FIXES. Claiming the row and reading its state are two separate
    operations, and `claim()` deliberately returns the existing row when it loses
    the INSERT race. Every loser then read state='queued', concluded the work was
    unclaimed, and sent. Measured before this existed: **8 concurrent notify()
    calls for one business event produced 8 emails and 1 ledger row** — the worst
    possible shape, an audit record asserting one notification while the customer
    received eight.

    The check must therefore BE the claim, not precede it. This is a
    compare-and-swap: PostgreSQL takes a row lock for the UPDATE, and under READ
    COMMITTED a waiting statement re-evaluates its WHERE against the committed
    new version — so exactly one caller can move the row out of a sendable state.

    Reclaimable states:
      queued / failed  — nobody is sending; go.
      attempted        — someone WAS sending. Only reclaim after ATTEMPT_LEASE,
                         so a crashed worker cannot strand the notification
                         forever while a live one cannot be double-sent.
    accepted / skipped are terminal and never reclaimed.

    `attempts` is NOT incremented here. It counts PROVIDER calls, and acquiring
    the lease is not one — the autosend-off path acquires and releases without
    ever reaching a provider.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE order_notifications
                       SET state = 'attempted',
                           first_attempted_at = COALESCE(first_attempted_at, now()),
                           last_attempted_at  = now()
                     WHERE notification_id = %s::uuid
                       AND (state IN ('queued', 'failed')
                            OR (state = 'attempted'
                                AND last_attempted_at
                                    < now() - interval '{ATTEMPT_LEASE}'))
                 RETURNING {_COLS}""",
                (str(notification_id),))
            row = _row(cur)
        conn.commit()
        return row
    finally:
        conn.close()


def release(notification_id: str, reason: str) -> Dict[str, Any]:
    """Hand the lease back without sending, leaving the row eligible again.
    Used when the send is deliberately not attempted (autosend off)."""
    return _update(notification_id, state="queued", failure_reason=reason[:2000])


def mark_attempted(notification_id: str, recipient: str, subject: str,
                   ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Record that we are about to call the PROVIDER. Written before the call, so
    an attempt that never returns is still visible as an attempt. The state is
    already 'attempted' — `acquire()` set it — so this only records who the
    message is for and counts the provider call."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE order_notifications
                       SET recipient_email=%s, subject=%s,
                           contact_id=%s, account_id=%s,
                           attempts = attempts + 1,
                           last_attempted_at = now(),
                           failure_reason = NULL
                     WHERE notification_id = %s::uuid
                 RETURNING {_COLS}""",
                (recipient, subject, ctx.get("contact_id"), ctx.get("account_id"),
                 str(notification_id)))
            row = _row(cur)
        conn.commit()
        return row
    finally:
        conn.close()


# Legacy subjects written by handle_order_status_changed before this module
# existed. Only 'sent' counts: a 'drafted' row means nothing was transmitted.
_LEGACY_SUBJECT = {
    "order.created": "Order confirmation email sent%",
    "order.shipped": "Order shipped email sent%",
    # The legacy path had no delivered notice at all.
}


def legacy_already_sent(order_id: str, event_type: str) -> bool:
    """Did the OLD path already email this order for this event?

    THE ORDERING TRAP THIS REMOVES. sql/order_lifecycle_notifications.sql adopts
    legacy `activities` rows into this ledger so an order already emailed is not
    emailed again. That backfill is a snapshot, and during a cutover the two
    systems overlap: on 2026-08-15 the database had the new trigger while the app
    still ran the old sender, so two orders were emailed by the legacy path
    AFTER the backfill and existed nowhere the new code looks. Deploying would
    have sent those customers a second confirmation.

    Making the batch backfill correct requires running it at exactly the right
    moment, between the deploy and the next transition. Checking here instead
    makes that ordering irrelevant: the guard travels with the send.

    Costs one indexed lookup on the first claim of each notification, and becomes
    permanently inert once no legacy row can be written — which is true the
    moment the old sender is gone.

    ERRORS ARE NOT SWALLOWED, and the first draft of this got it wrong. It
    caught the exception and returned True — "if we cannot tell, assume it was
    already sent" — which sounds like the safe default and is not. True marks
    the row `accepted` with provider='legacy-activity', a TERMINAL state, so one
    transient database blip would permanently and silently deprive a customer of
    a notification nobody would ever retry.

    Letting it raise is the genuinely safe answer: `notify()` has done nothing
    irreversible yet, the exception reaches the bus, and the event is retried
    under its existing backoff. Neither sent nor suppressed — just not decided.
    """
    pattern = _LEGACY_SUBJECT.get(event_type)
    if not pattern:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM activities
                    WHERE related_type='order' AND related_id=%s::uuid
                      AND channel='email' AND subject LIKE %s
                    LIMIT 1""",
                (str(order_id), pattern))
            return cur.fetchone() is not None
    finally:
        conn.close()


def mark_accepted(notification_id: str, provider: str,
                  message_id: Optional[str], response: str) -> Dict[str, Any]:
    return _update(notification_id, state="accepted", accepted_at=datetime.now(),
                   provider=provider, provider_message_id=message_id,
                   provider_response=response[:2000], failure_reason=None)


def mark_failed(notification_id: str, reason: str,
                provider: Optional[str] = None,
                response: Optional[str] = None) -> Dict[str, Any]:
    """Retryable. accepted_at is left NULL, which the CHECK constraint requires
    and which makes 'a failure that looks like a success' unrepresentable."""
    return _update(notification_id, state="failed", failure_reason=reason[:2000],
                   provider=provider, provider_response=(response or "")[:2000] or None)


def mark_skipped(notification_id: str, reason: str,
                 recipient: Optional[str] = None,
                 subject: Optional[str] = None) -> Dict[str, Any]:
    """Deliberately not sent, and trying again would reach the same conclusion.
    Distinct from `failed`: this is a decision, not an error."""
    fields: Dict[str, Any] = {"state": "skipped", "failure_reason": reason[:2000]}
    if recipient is not None:
        fields["recipient_email"] = recipient
    if subject is not None:
        fields["subject"] = subject
    return _update(notification_id, **fields)


def history(order_id: str) -> List[Dict[str, Any]]:
    """Every lifecycle notification recorded for one order, oldest first.
    Read by the Orders get_detail view."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT event_type, state, recipient_email, template, subject,
                          provider, provider_message_id, failure_reason,
                          attempts, last_attempted_at, accepted_at, created_at,
                          idempotency_key
                     FROM order_notifications
                    WHERE order_id = %s::uuid
                    ORDER BY created_at, event_type""",
                (str(order_id),))
            keys = ("event_type", "state", "recipient_email", "template",
                    "subject", "provider", "provider_message_id",
                    "failure_reason", "attempts", "last_attempted_at",
                    "accepted_at", "created_at", "idempotency_key")
            return [dict(zip(keys, r)) for r in cur.fetchall()]
    except Exception as exc:                                   # noqa: BLE001
        # The order detail view must not break because the migration has not
        # been applied to this database yet.
        conn.rollback()
        logger.debug(f"[order_notifications] history unavailable: {exc}")
        return []
    finally:
        conn.close()


# ============================================================================
# RECIPIENT GATE
# ============================================================================

def resolve_recipient(ctx: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(address, refusal_reason). Exactly one is non-None.

    The rules for 'what counts as a real, deliverable address' are NOT restated
    here. `agent_bus._is_real_email` owns them, and a second copy is precisely
    how the placeholder/seed corpus would leak back into outbound mail.

    The reasons are distinguished because 'no contact on the order' and 'the
    address exists but nobody confirmed it' call for different fixes, and an
    audit row that says only 'skipped' tells the reader neither.
    """
    from app.core.agent_bus import _is_real_email

    if not ctx.get("contact_id"):
        return None, "order has no contact — there is no customer to email"
    addr = ctx.get("contact_email")
    if not addr:
        return None, "customer contact record has no email address"
    if not _is_real_email(addr, ctx.get("verified", False)):
        return None, (f"{addr} is not a verified, deliverable recipient "
                      f"(is_email_verified is false, or the domain is a "
                      f"reserved placeholder)")
    return addr, None


# ============================================================================
# PROVIDER OUTCOME — success requires positive evidence
# ============================================================================

# accepted — the provider took the message
# skipped  — a deliberate refusal by a control we own (outbound guard, consent)
# failed   — the attempt did not complete, and may be retried
ACCEPTED, SKIPPED, FAILED = "accepted", "skipped", "failed"


def classify_send_result(result: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """(outcome, reason) from what send_email actually returned.

    The default is FAILURE and only positive evidence promotes it. This inverts
    the predicate that produced 25 false 'sent' activity records in June: that
    one read `resp.get("success") is not False and not resp.get("error")`, so a
    403 body carrying neither key was read as a successful send.

    A resend acceptance without a message id is treated as FAILED rather than
    accepted. send_email's Resend path calls raise_for_status(), so a 4xx/5xx
    already becomes success=False — but that is one line in another module, and
    this check means a future edit that drops it cannot silently convert HTTP
    403 into a customer notification recorded as accepted.
    """
    if not isinstance(result, dict):
        return FAILED, f"email provider returned no usable result ({type(result).__name__})"

    # Deliberate refusals by controls this platform owns. Retrying reaches the
    # same wall, so these are decisions, not errors.
    if result.get("blocked"):
        return SKIPPED, str(result.get("message") or "blocked by outbound guard")
    if result.get("skipped"):
        return SKIPPED, str(result.get("message")
                            or f"skipped: {result.get('skipped')}")

    if result.get("success") is not True:
        return FAILED, str(result.get("message") or result.get("error")
                           or "provider did not confirm acceptance")

    provider = str(result.get("provider") or "")
    if provider == "resend" and not result.get("provider_message_id"):
        return FAILED, ("provider reported success without a message id — "
                        "acceptance not evidenced")
    return ACCEPTED, str(result.get("message") or "accepted by provider")


# ============================================================================
# AUDIT ACTIVITY — the existing, human-visible audit surface
# ============================================================================

def _record_activity(ctx: Dict[str, Any], event_type: str, row: Dict[str, Any]) -> None:
    """Mirror the ledger row into `activities`, the surface people already read.

    The subject states the STATE, never 'sent'. A skip and a failure are written
    too: a notification that did not happen is exactly the thing an audit trail
    exists to make visible, and the previous path recorded nothing at all when
    the recipient was unusable.
    """
    state = row.get("state")
    label = {"accepted": "accepted by provider",
             "skipped":  "not sent",
             "failed":   "failed",
             "attempted": "attempted",
             "queued":   "queued"}.get(state, state)
    # .get, not [] — and the difference is not stylistic. On the first live
    # cancellation this dict was missing 'order.cancelled', so a KeyError was
    # raised AFTER the provider had accepted the email. The caller caught it,
    # reported the send as failed, told an employee to follow up, and opened an
    # escalation — for an email that had actually gone out. A label this module
    # cannot find is a cosmetic gap; it must never be able to contradict the
    # ledger row.
    kind = {"order.created":   "confirmation",
            "order.shipped":   "shipment",
            "order.delivered": "delivery",
            "order.cancelled": "cancellation"}.get(event_type, "notification")
    detail = (row.get("failure_reason")
              or (f"provider={row.get('provider')} "
                  f"id={row.get('provider_message_id') or 'n/a'}"))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, due_at, completed_at,
                      direction, related_type, related_id, order_id,
                      account_id, contact_id, channel, outcome,
                      created_at, updated_at)
                   VALUES ('task','completed', %(subj)s, %(desc)s, now(), now(),
                           'outbound', 'order', %(oid)s::uuid, %(oid)s::uuid,
                           %(acct)s, %(ct)s, 'email', %(out)s, now(), now())""",
                {"subj": f"Order {kind} notification {label} – {ctx['order_number']}",
                 # Recipient address, not the message body: the audit answers
                 # "which address" without copying the customer's order contents
                 # into a second, longer-lived table.
                 "desc": (f"{event_type} · key={row.get('idempotency_key')} · "
                          f"to={row.get('recipient_email') or 'n/a'} · "
                          f"template={row.get('template')} · "
                          f"attempts={row.get('attempts')} · {detail}"),
                 "oid": ctx["order_id"],
                 "acct": ctx.get("account_id"), "ct": ctx.get("contact_id"),
                 "out": f"auto: {event_type} → {state}"})
        conn.commit()
    except Exception as exc:                                   # noqa: BLE001
        conn.rollback()
        logger.warning(f"[order_notifications] audit activity not written "
                       f"for {ctx.get('order_number')}: {exc}")
    finally:
        conn.close()


# ============================================================================
# THE SERVICE
# ============================================================================

def notify(order_id: str, event_type: str, event_uuid: Optional[str] = None,
           correlation_id: Optional[str] = None) -> Dict[str, Any]:
    """Send (at most once) the customer email for one order lifecycle event.

    Raises RetryableNotificationError on a provider/transport failure, so the
    agent bus redelivers under its own backoff. Every other outcome returns.
    """
    if event_type not in EVENT_TEMPLATES:
        raise ValueError(f"not a customer lifecycle event: {event_type!r}")

    row, is_new = claim(order_id, event_type, event_uuid, correlation_id)
    if row is None:
        raise RetryableNotificationError(
            f"could not claim {order_id}:{event_type}")

    # ── Idempotency, layer 1: durable terminal state ────────────────────────
    if row["state"] in TERMINAL_STATES:
        logger.info(f"[order_notifications] {row['idempotency_key']} already "
                    f"{row['state']} — no second email")
        return {"status": "ok", "action": f"already_{row['state']}",
                "idempotency_key": row["idempotency_key"],
                "state": row["state"], "duplicate_suppressed": True}

    # ── Idempotency, layer 2: the path this one replaced ────────────────────
    # Only on a brand-new claim: if the row already existed, this was decided
    # the first time round and re-asking every retry would be wasted work.
    if is_new and legacy_already_sent(order_id, event_type):
        adopted = _update(row["notification_id"], state="accepted",
                          accepted_at=datetime.now(), provider="legacy-activity",
                          provider_response="already emailed by the legacy "
                                            "handler before this module existed")
        logger.info(f"[order_notifications] {row['idempotency_key']} was already "
                    f"emailed by the legacy path — adopted, not re-sent")
        return {"status": "ok", "action": "already_accepted", "state": "accepted",
                "idempotency_key": row["idempotency_key"],
                "duplicate_suppressed": True, "provider": "legacy-activity"}

    # ── Idempotency, layer 3: exclusive ownership of THIS send ──────────────
    # Everything above is a read. Two callers can pass all of it simultaneously
    # and both send — measured at 8 emails for one event. This is the layer that
    # is also the claim.
    owned = acquire(row["notification_id"])
    if owned is None:
        logger.info(f"[order_notifications] {row['idempotency_key']} is being "
                    f"sent by another worker — not sending a second copy")
        return {"status": "ok", "action": "in_flight_elsewhere",
                "idempotency_key": row["idempotency_key"],
                "state": "attempted", "duplicate_suppressed": True}
    row = owned

    ctx = load_context(order_id)
    if ctx is None:
        mark_skipped(row["notification_id"], "order no longer exists")
        return {"status": "ok", "action": "skipped", "state": "skipped",
                "reason": "order not found"}

    recipient, refusal = resolve_recipient(ctx)
    if refusal:
        # NOT silence, and NOT a pretend send. The attempt is recorded as a
        # deliberate skip with the reason, and mirrored into the activity feed.
        updated = mark_skipped(row["notification_id"], refusal)
        _record_activity(ctx, event_type, updated)
        logger.info(f"[order_notifications] {ctx['order_number']} {event_type} "
                    f"skipped: {refusal}")
        return {"status": "ok", "action": "skipped", "state": "skipped",
                "order": ctx["order_number"], "reason": refusal,
                "idempotency_key": row["idempotency_key"]}

    subject, body_text, body_html = compose(ctx, event_type)

    if not _autosend():
        # Draft-first, the platform's existing posture. The lease is RELEASED
        # back to 'queued', which is true: it was composed and not transmitted,
        # and enabling autosend must let it proceed. Not 'skipped' — that is a
        # decision, and this one reverses.
        _update(row["notification_id"], recipient_email=recipient,
                subject=subject, contact_id=ctx.get("contact_id"),
                account_id=ctx.get("account_id"))
        updated = release(row["notification_id"],
                          "AGENT_BUS_AUTOSEND=0 — composed, not transmitted")
        _record_activity(ctx, event_type, updated)
        return {"status": "ok", "action": "drafted", "state": "queued",
                "order": ctx["order_number"], "to": recipient,
                "subject": subject, "autosend": False,
                "idempotency_key": row["idempotency_key"]}

    # Written before the call, so an attempt that never returns is still an
    # attempt in the record. The lease is already held by acquire().
    row = mark_attempted(row["notification_id"], recipient, subject, ctx)

    from app.agents.email.smtp_imap import send_email
    try:
        # Transactional, not commercial: an order confirmation concerns a
        # transaction the customer initiated. CASL's commercial path would
        # attach an unsubscribe link, and "unsubscribe from delivery notices"
        # is not a choice this system should offer.
        result = send_email(to=recipient, subject=subject,
                            body_html=body_html, body_text=body_text,
                            from_name=COMPANY_NAME, commercial=False)
    except Exception as exc:                                   # noqa: BLE001
        updated = mark_failed(row["notification_id"],
                              f"{type(exc).__name__}: {exc}")
        _record_activity(ctx, event_type, updated)
        raise RetryableNotificationError(
            f"{event_type} for {ctx['order_number']}: {exc}") from exc

    outcome, reason = classify_send_result(result)

    if outcome == ACCEPTED:
        updated = mark_accepted(row["notification_id"],
                                str(result.get("provider") or "unknown"),
                                result.get("provider_message_id"), reason)
        _record_activity(ctx, event_type, updated)
        logger.info(f"[order_notifications] {ctx['order_number']} {event_type} "
                    f"ACCEPTED by {updated.get('provider')} "
                    f"(id={updated.get('provider_message_id') or 'n/a'})")
        return {"status": "ok", "action": "accepted", "state": "accepted",
                "order": ctx["order_number"], "to": recipient,
                "provider": updated.get("provider"),
                "provider_message_id": updated.get("provider_message_id"),
                "idempotency_key": row["idempotency_key"]}

    if outcome == SKIPPED:
        updated = mark_skipped(row["notification_id"], reason,
                               recipient=recipient, subject=subject)
        _record_activity(ctx, event_type, updated)
        logger.info(f"[order_notifications] {ctx['order_number']} {event_type} "
                    f"refused: {reason}")
        return {"status": "ok", "action": "skipped", "state": "skipped",
                "order": ctx["order_number"], "reason": reason,
                "idempotency_key": row["idempotency_key"]}

    updated = mark_failed(row["notification_id"], reason,
                          provider=str(result.get("provider") or "") or None,
                          response=str(result.get("message") or ""))
    _record_activity(ctx, event_type, updated)
    logger.warning(f"[order_notifications] {ctx['order_number']} {event_type} "
                   f"FAILED (retryable): {reason}")
    raise RetryableNotificationError(
        f"{event_type} for {ctx['order_number']}: {reason}")
