"""Quotation generation — governed quote emails from LIVE product pricing.

The email agent could answer about products and send emails, but never turn
"quote Acme for 3 label printers" into a priced document. This module does:

    build_quote()      DETERMINISTIC: resolve the account's primary contact,
                       match each requested product, price it from the
                       CURRENT retail row in product_pricing, compute line
                       totals / discount / total. The LLM never touches a
                       number — a quote with invented pricing is a liability,
                       so drafting failure means no quote, never a wrong one.
    generate_quote_sp  A2A `quote.generate` (write): build, then deliver
                       under the platform's established outbound gates —
                       AGENT_BUS_AUTOSEND on AND a real, verified address →
                       email sent; otherwise the quote is drafted as an
                       owner TASK with the full text ready to forward.
                       Either way the quote is logged as an outbound
                       activity (the audit copy) and the price table rides
                       in the result for the caller/approver to see.

Quotes are informational offers, not contracts: validity is stated (30
days), taxes are "plus applicable taxes", and the email invites a reply.
The send is transactional-adjacent (they asked for a price), but the CASL
footer costs nothing — commercial=True keeps it beyond reproach.

CONFIG (env)
  QUOTE_VALID_DAYS   30   validity stated on every quote
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.core.database import get_connection

logger = logging.getLogger("quotes")

VALID_DAYS = int(os.getenv("QUOTE_VALID_DAYS", "30"))
_MAX_ITEMS = 12


def _fmt(v: float) -> str:
    return f"${float(v):,.2f}"


# ============================================================================
# DETERMINISTIC BUILD — identity, products, math
# ============================================================================

def _account_recipient(account_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.contact_id::text, c.email,
                          COALESCE(c.is_email_verified, false),
                          COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'')||' '||
                                   COALESCE(c.last_name,'')),''), a.account_name),
                          a.account_name, a.owner_id
                   FROM accounts a
                   LEFT JOIN contacts c ON c.account_id=a.account_id
                        AND COALESCE(c.is_deleted,false)=false
                        AND COALESCE(c.email,'') <> ''
                   WHERE a.account_id=%s::uuid
                     AND COALESCE(a.is_deleted,false)=false
                   ORDER BY c.created_at LIMIT 1""", (account_id,))
            r = cur.fetchone()
        if not r:
            return None
        return {"contact_id": r[0], "email": r[1] or "", "verified": bool(r[2]),
                "display": r[3] or r[4], "account_name": r[4],
                "owner_id": r[5]}
    finally:
        conn.close()


def _match_product(cur, wanted: str) -> Optional[Dict[str, Any]]:
    """One product + its CURRENT retail price. Exact (case-insensitive)
    name/SKU first, then prefix for reasonably specific names (≥4 chars —
    a 1-letter 'prefix' is a wildcard grab). Never a bare substring scan:
    ambiguous matches misquote, and no match beats a wrong one."""
    attempts = [(("lower(p.product_name)=lower(%s) "
                  "OR lower(COALESCE(p.sku,''))=lower(%s)"), (wanted, wanted))]
    if len(wanted) >= 4:
        attempts.append(("p.product_name ILIKE %s", (wanted + "%",)))
    for cond, arg in attempts:
        cur.execute(
            f"""SELECT p.product_id::text, p.product_name,
                       pp.price_value::float
                FROM products p
                JOIN product_pricing pp ON pp.product_id=p.product_id
                WHERE p.is_active AND ({cond})
                  AND lower(pp.price_type)='retail'
                  AND pp.price_value IS NOT NULL
                  AND (pp.effective_to IS NULL OR pp.effective_to > now())
                ORDER BY pp.effective_from DESC NULLS LAST
                LIMIT 1""",
            arg if isinstance(arg, tuple) else (arg,))
        r = cur.fetchone()
        if r:
            return {"product_id": r[0], "name": r[1], "unit_price": r[2]}
    return None


def build_quote(account_id: str, items: List[Dict[str, Any]],
                discount_pct: float = 0.0) -> Dict[str, Any]:
    """{'ok': True, quote} or {'ok': False, error}. Pure data — no sending."""
    who = _account_recipient(str(account_id or ""))
    if not who:
        return {"ok": False, "error": f"account {account_id} not found"}
    if not items:
        return {"ok": False, "error": "no items requested"}
    # Deterministic brand boundary (guardrail layer 2): the max discount is an
    # editable governance policy, not a courtesy — requests above it are
    # CLAMPED and flagged, whoever (or whatever) asked.
    requested_pct = max(0.0, float(discount_pct or 0))
    try:
        from app.core import governance
        cap_pct = float(governance.policy_value("brand.max_discount_pct", 15.0))
    except Exception:
        cap_pct = 15.0
    discount_pct = min(requested_pct, cap_pct)
    discount_capped = requested_pct > cap_pct

    lines, missing = [], []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for it in items[:_MAX_ITEMS]:
                wanted = str(it.get("product") or it.get("name") or "").strip()
                qty = max(1, min(int(it.get("qty") or it.get("quantity") or 1),
                                 10_000))
                if not wanted:
                    continue
                m = _match_product(cur, wanted)
                if not m:
                    missing.append(wanted)
                    continue
                lines.append({**m, "qty": qty,
                              "line_total": round(m["unit_price"] * qty, 2)})
    finally:
        conn.close()
    if not lines:
        return {"ok": False, "error": "no requested product matched the "
                                      "catalog", "unmatched": missing}

    subtotal = round(sum(l["line_total"] for l in lines), 2)
    discount = round(subtotal * discount_pct / 100, 2)
    if discount_capped:
        logger.info(f"[quotes] discount clamped {requested_pct}% → {cap_pct}% "
                    f"(brand.max_discount_pct)")
    return {"ok": True, "quote": {
        "account_id": str(account_id), "account_name": who["account_name"],
        "recipient": who, "lines": lines, "unmatched": missing,
        "subtotal": subtotal, "discount_pct": discount_pct,
        "discount": discount, "total": round(subtotal - discount, 2),
        "valid_until": (date.today() + timedelta(days=VALID_DAYS)).isoformat(),
        **({"discount_capped": True, "requested_pct": requested_pct}
           if discount_capped else {}),
    }}


# ============================================================================
# RENDER — the LLM may polish the intro sentence; the TABLE is code-built
# ============================================================================

def _render_rows_text(q: Dict[str, Any]) -> str:
    rows = [f"  {l['qty']} x {l['name'][:60]} @ {_fmt(l['unit_price'])} = "
            f"{_fmt(l['line_total'])}" for l in q["lines"]]
    rows.append(f"  Subtotal: {_fmt(q['subtotal'])}")
    if q["discount"]:
        rows.append(f"  Discount ({q['discount_pct']:g}%): -{_fmt(q['discount'])}")
    rows.append(f"  Total: {_fmt(q['total'])} plus applicable taxes")
    return "\n".join(rows)


def render_quote_email(q: Dict[str, Any]) -> Dict[str, str]:
    display = q["recipient"]["display"]
    intro = (f"Thank you for your interest — here is your quotation from "
             f"Conscestra CRM, valid until {q['valid_until']}.")
    try:
        from app.core.graph_utils import _get_llm
        resp = _get_llm(tier="lite").invoke([
            {"role": "system", "content":
                "Write ONE warm, professional opening sentence for a business "
                "quotation email. No prices, no product names, no promises — "
                "those are in the attached table. Plain text."},
            {"role": "user", "content": f"Customer: {display} "
                                        f"({q['account_name']})"},
        ])
        text = (resp.content if hasattr(resp, "content") else "").strip()
        if 20 < len(text) < 220:
            intro = (f"{text} This quotation is valid until "
                     f"{q['valid_until']}.")
    except Exception as exc:
        logger.debug(f"[quotes] intro LLM skipped: {exc}")

    rows_html = "".join(
        f"<tr><td style='padding:6px 10px'>{l['name'][:80]}</td>"
        f"<td style='padding:6px 10px;text-align:center'>{l['qty']}</td>"
        f"<td style='padding:6px 10px;text-align:right'>{_fmt(l['unit_price'])}</td>"
        f"<td style='padding:6px 10px;text-align:right'>{_fmt(l['line_total'])}</td></tr>"
        for l in q["lines"])
    totals_html = (
        f"<tr><td colspan='3' style='padding:6px 10px;text-align:right'>Subtotal</td>"
        f"<td style='padding:6px 10px;text-align:right'>{_fmt(q['subtotal'])}</td></tr>"
        + (f"<tr><td colspan='3' style='padding:6px 10px;text-align:right'>"
           f"Discount ({q['discount_pct']:g}%)</td>"
           f"<td style='padding:6px 10px;text-align:right'>-{_fmt(q['discount'])}</td></tr>"
           if q["discount"] else "")
        + f"<tr><td colspan='3' style='padding:6px 10px;text-align:right'>"
          f"<b>Total (plus applicable taxes)</b></td>"
          f"<td style='padding:6px 10px;text-align:right'><b>{_fmt(q['total'])}</b></td></tr>")
    subject = (f"Quotation for {q['account_name']} — {_fmt(q['total'])} "
               f"(valid until {q['valid_until']})")
    body_html = (
        f"<p>Hi {display},</p><p>{intro}</p>"
        f"<table style='border-collapse:collapse;border:1px solid #e2e8f0'>"
        f"<tr style='background:#f8fafc'><th style='padding:6px 10px;text-align:left'>Item</th>"
        f"<th style='padding:6px 10px'>Qty</th><th style='padding:6px 10px'>Unit</th>"
        f"<th style='padding:6px 10px'>Total</th></tr>{rows_html}{totals_html}</table>"
        f"<p>To proceed or adjust anything, just reply to this email.</p>"
        f"<p>The Conscestra CRM Team | info@agentorc.ca</p>")
    body_text = (f"Hi {display},\n\n{intro}\n\n{_render_rows_text(q)}\n\n"
                 f"To proceed or adjust anything, just reply to this email.\n\n"
                 f"The Conscestra CRM Team | info@agentorc.ca")
    return {"subject": subject, "body_html": body_html, "body_text": body_text}


# ============================================================================
# EXECUTE — A2A `quote.generate` (send under the established gates)
# ============================================================================

def _log_quote_activity(q: Dict[str, Any], email: Dict[str, str],
                        emailed: bool) -> None:
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, due_at, direction,
                      channel, owner_id, related_type, related_id, account_id,
                      completed_at, created_at, updated_at)
                   VALUES (%s, %s, %s, %s,
                           CASE WHEN %s='open' THEN now() + interval '4 hours' END,
                           'outbound', 'email', %s, 'account', %s::uuid, %s::uuid,
                           CASE WHEN %s='completed' THEN now() END, now(), now())""",
                ("email" if emailed else "task",
                 "completed" if emailed else "open",
                 (email["subject"] if emailed
                  else f"Send quote — {q['account_name']} ({_fmt(q['total'])})")[:180],
                 (email["body_text"] if emailed else
                  "Quote built but NOT emailed (autosend off or unverified "
                  f"address {q['recipient'].get('email') or 'n/a'}). Review "
                  f"and send:\n\n{email['body_text']}")[:2000],
                 "completed" if emailed else "open",
                 q["recipient"].get("owner_id"), q["account_id"],
                 q["account_id"],
                 "completed" if emailed else "open"))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"[quotes] activity log skipped: {exc}")


def generate_quote_sp(p: Dict[str, Any]) -> Dict[str, Any]:
    """A2A structured handler for quote.generate. params:
    account_id, items=[{product, qty}], discount_pct (optional)."""
    built = build_quote(str(p.get("account_id") or ""),
                        list(p.get("items") or []),
                        float(p.get("discount_pct") or 0))
    if not built.get("ok"):
        return built
    q = built["quote"]
    email = render_quote_email(q)

    emailed = False
    from app.core import agent_bus
    who = q["recipient"]
    if agent_bus.AUTOSEND and agent_bus._is_real_email(who.get("email"),
                                                       who.get("verified", False)):
        try:
            from app.agents.email.smtp_imap import send_email
            res = send_email(to=who["email"], subject=email["subject"],
                             body_html=email["body_html"],
                             body_text=email["body_text"], commercial=True)
            emailed = bool(res.get("success"))
        except Exception as exc:
            logger.warning(f"[quotes] send failed: {exc}")
    _log_quote_activity(q, email, emailed)
    logger.info(f"[quotes] quote for {q['account_name']} "
                f"{_fmt(q['total'])} — {'sent' if emailed else 'drafted as task'}")
    return {"ok": True, "emailed": emailed,
            "drafted_as_task": not emailed,
            "to": who.get("email"), "subject": email["subject"],
            "total": q["total"], "valid_until": q["valid_until"],
            "lines": [{"name": l["name"], "qty": l["qty"],
                       "unit_price": l["unit_price"],
                       "line_total": l["line_total"]} for l in q["lines"]],
            "unmatched": q["unmatched"]}
