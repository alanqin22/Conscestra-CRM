"""Customer portal — C5.0 (Axis 5). Read-only, on the proven security model.

THIS IS NOT A NEW PORTAL. It exposes capabilities that already exist behind a
customer-security boundary the VOICE CHANNEL already proved:

    customer authentication  ──▶  customer identity  ──▶  customer scope
                                                              │
                                                              ▼
                                                      authorized data

Customer auth was already complete (signup, signin, password reset, email
verification, /auth/address) and `auth_credentials` already keys on
account_id / contact_id / lead_id. The storefront already signs customers in.
What was missing is that a logged-in customer could reach exactly two
endpoints — /auth-health and /auth/signout. The phone line did more for a
verified caller than the website did for a logged-in one.

THE INVARIANTS (ratified 2026-07-27)

  ONE CUSTOMER IDENTITY   voice, chat, portal and anything later resolve to
                          the same model. The channel changes; the identity
                          model does not.
  ONE CUSTOMER SCOPE      there is exactly one implementation —
                          write_guard.set_customer_scope + scoped_rows. This
                          module adds an HTTP *entry* to it, never a second
                          copy. A per-channel copy would drift and the weakest
                          would decide what a customer can see.
  ACCOUNT-LEVEL SCOPE     account_id is the security boundary, mirroring the
                          voice channel. contact_id is recorded for
                          ATTRIBUTION ONLY — contact-ownership filtering
                          becomes unworkable the moment one account has
                          several contacts.
  READ-ONLY               no writes, no reorder, no payments, no case
                          creation. Prove the boundary before widening it.
  NO STORED PROCEDURES    every read is an explicitly account-scoped
                          parameterized query in a read-only transaction.
                          execute_sp is not widened; while a customer scope is
                          set it refuses ALL SP access, fail-closed.

LEAD-ONLY SESSIONS: a self-registered user is a LEAD, not yet a customer
account. They never receive an empty Orders page — an empty list reads as
"you have no orders" when the truth is "you are not linked yet", and that
distinction is the whole difference between a working product and a broken
one. They get an explicit onboarding state instead.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.write_guard import scoped_rows, set_customer_scope

logger = logging.getLogger("portal")


# ============================================================================
# THE HTTP ENTRY TO THE ONE CUSTOMER SCOPE
# ============================================================================

def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


async def customer_context(request: Request):
    """Resolve the caller's customer identity and open the scope for this
    request. A yield-dependency so the scope is ALWAYS cleared afterwards —
    a leaked ContextVar would hand the next request someone else's account.

    Fails closed: no token or no valid session is 401, never "read everything".
    A session WITHOUT an account is not an error — it is a lead, and the
    endpoints below answer it with an onboarding state rather than data.
    """
    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401,
                            detail="Sign in to view your account.")
    from app.agents.auth.router import get_session
    sess = get_session(token)
    if not sess:
        raise HTTPException(status_code=401,
                            detail="Session expired or invalid — "
                                   "please sign in again.")

    account_id = (sess.get("account_id") or "").strip() or None
    contact_id = (sess.get("contact_id") or None)
    ctx = {"session": sess, "account_id": account_id,
           "contact_id": contact_id, "linked": bool(account_id)}

    if account_id:
        # THE one scope. Everything downstream — including any stray path into
        # execute_sp — is now fail-closed to this account.
        set_customer_scope({"account_id": account_id, "contact_id": contact_id})
    try:
        yield ctx
    finally:
        set_customer_scope(None)


def _onboarding(ctx: Dict[str, Any], what: str) -> Dict[str, Any]:
    """What a lead-only session gets instead of an empty list.

    Never `{"orders": []}` — that reads as "you have no orders" when the truth
    is "your sign-in is not linked to a customer account yet"."""
    s = ctx["session"]
    return {
        "ok": True, "linked": False, "what": what,
        "state": "not_linked",
        "message": "Your sign-in is not yet linked to a customer account, so "
                   f"there is no {what} to show. This is not an empty "
                   f"{what} list — your account link is still pending.",
        "profile": {
            "identifier": s.get("identifier"),
            "first_name": s.get("first_name"),
            "last_name": s.get("last_name"),
            "source": s.get("source_table"),
        },
    }


def _money(rows: List[Dict[str, Any]], *keys: str) -> List[Dict[str, Any]]:
    """psycopg2 Decimals and datetimes are not JSON."""
    from decimal import Decimal
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, Decimal):
                r[k] = float(v)
            elif hasattr(v, "isoformat"):
                r[k] = v.isoformat()
    return rows


# ============================================================================
# READS — every one explicitly account-scoped
# ============================================================================

router = APIRouter(prefix="/portal", tags=["Customer Portal"])


@router.get("/me")
def portal_me(ctx: Dict[str, Any] = Depends(customer_context)) -> Dict[str, Any]:
    """Profile, verification and ACCOUNT-LINK status. The only endpoint a
    lead-only session gets real content from."""
    s = ctx["session"]
    out = {
        "ok": True,
        "linked": ctx["linked"],
        "profile": {
            "identifier": s.get("identifier"),
            "first_name": s.get("first_name"),
            "last_name": s.get("last_name"),
            "role": s.get("role"),
            "source": s.get("source_table"),
        },
        "account_link": {
            "linked": ctx["linked"],
            "account_id": ctx["account_id"],
            # contact_id is ATTRIBUTION, not authorization — stated here so the
            # distinction is visible in the API itself.
            "contact_id": ctx["contact_id"],
            "note": ("Your sign-in is linked to a customer account."
                     if ctx["linked"] else
                     "Your sign-in is not yet linked to a customer account. "
                     "Business records will appear once it is."),
        },
    }
    if ctx["linked"]:
        rows = scoped_rows(
            """SELECT a.account_name, a.industry, a.website
               FROM accounts a WHERE a.account_id = %(account_id)s::uuid""")
        out["account"] = (_money(rows)[0] if rows else None)
    return out


@router.get("/orders")
def portal_orders(limit: int = 25,
                  ctx: Dict[str, Any] = Depends(customer_context)) -> Dict[str, Any]:
    if not ctx["linked"]:
        return _onboarding(ctx, "orders")
    rows = scoped_rows(
        """SELECT o.order_id::text, o.order_number, o.status, o.order_date,
                  o.currency, o.subtotal_amount, o.tax_amount, o.total_amount
           FROM orders o
           WHERE o.account_id = %(account_id)s::uuid
           ORDER BY o.order_date DESC NULLS LAST
           LIMIT %(lim)s""", {"lim": max(1, min(int(limit), 100))})
    return {"ok": True, "linked": True, "orders": _money(rows)}


@router.get("/orders/{order_id}")
def portal_order(order_id: str,
                 ctx: Dict[str, Any] = Depends(customer_context)) -> Dict[str, Any]:
    """One order with its lines.

    The order_id is a FILTER, never the authorization: the account_id in the
    WHERE clause comes from the verified scope, so asking for somebody else's
    order returns nothing rather than their data."""
    if not ctx["linked"]:
        return _onboarding(ctx, "orders")
    head = scoped_rows(
        """SELECT o.order_id::text, o.order_number, o.status, o.order_date,
                  o.currency, o.subtotal_amount, o.tax_amount, o.total_amount
           FROM orders o
           WHERE o.account_id = %(account_id)s::uuid
             AND o.order_id = %(oid)s::uuid""", {"oid": order_id})
    if not head:
        raise HTTPException(status_code=404, detail="Order not found.")
    lines = scoped_rows(
        """SELECT oi.description, oi.quantity, oi.discount,
                  oi.line_subtotal, oi.line_total
           FROM order_items oi
           JOIN orders o ON o.order_id = oi.order_id
           WHERE o.account_id = %(account_id)s::uuid
             AND oi.order_id = %(oid)s::uuid""", {"oid": order_id})
    out = _money(head)[0]
    out["lines"] = _money(lines)
    return {"ok": True, "linked": True, "order": out}


@router.get("/invoices")
def portal_invoices(limit: int = 25,
                    ctx: Dict[str, Any] = Depends(customer_context)) -> Dict[str, Any]:
    if not ctx["linked"]:
        return _onboarding(ctx, "invoices")
    rows = scoped_rows(
        """SELECT i.invoice_id::text, i.invoice_number, i.status,
                  i.issue_date, i.due_date, i.currency, i.subtotal_amount,
                  i.tax_amount, i.total_amount, i.balance_due
           FROM invoices i
           WHERE i.account_id = %(account_id)s::uuid
           ORDER BY i.issue_date DESC NULLS LAST
           LIMIT %(lim)s""", {"lim": max(1, min(int(limit), 100))})
    rows = _money(rows)
    return {"ok": True, "linked": True, "invoices": rows,
            "balance_due": round(sum(float(r.get("balance_due") or 0)
                                     for r in rows), 2)}


@router.get("/cases")
def portal_cases(limit: int = 25,
                 ctx: Dict[str, Any] = Depends(customer_context)) -> Dict[str, Any]:
    """The customer's own service work (C1).

    Internal comments are NOT exposed: `is_internal` exists precisely so a note
    written for colleagues stays that way, and the portal is the first surface
    where getting that wrong would show the customer staff-only text."""
    if not ctx["linked"]:
        return _onboarding(ctx, "cases")
    rows = scoped_rows(
        """SELECT c.case_id::text, c.subject, c.status, c.priority,
                  c.created_at, c.first_response_at, c.resolved_at,
                  c.closed_at, c.is_historical
           FROM cases c
           WHERE c.account_id = %(account_id)s::uuid
           ORDER BY c.created_at DESC
           LIMIT %(lim)s""", {"lim": max(1, min(int(limit), 100))})
    return {"ok": True, "linked": True, "cases": _money(rows)}


@router.get("/cases/{case_id}")
def portal_case(case_id: str,
                ctx: Dict[str, Any] = Depends(customer_context)) -> Dict[str, Any]:
    if not ctx["linked"]:
        return _onboarding(ctx, "cases")
    head = scoped_rows(
        """SELECT c.case_id::text, c.subject, c.description, c.status,
                  c.priority, c.created_at, c.first_response_at,
                  c.resolved_at, c.closed_at
           FROM cases c
           WHERE c.account_id = %(account_id)s::uuid
             AND c.case_id = %(cid)s::uuid""", {"cid": case_id})
    if not head:
        raise HTTPException(status_code=404, detail="Case not found.")
    # PUBLIC comments only — is_internal = false, enforced in SQL rather than
    # filtered afterwards, so a future refactor cannot lose the condition.
    comments = scoped_rows(
        """SELECT cm.comment, cm.created_at
           FROM case_comments cm
           JOIN cases c ON c.case_id = cm.case_id
           WHERE c.account_id = %(account_id)s::uuid
             AND cm.case_id = %(cid)s::uuid
             AND cm.is_internal = false
           ORDER BY cm.created_at""", {"cid": case_id})
    out = _money(head)[0]
    out["comments"] = _money(comments)
    return {"ok": True, "linked": True, "case": out}


@router.get("/quotes")
def portal_quotes(limit: int = 25,
                  ctx: Dict[str, Any] = Depends(customer_context)) -> Dict[str, Any]:
    """Offers made to this customer (C3).

    The DISCOUNT POLICY fields are deliberately not exposed: requested-vs-
    granted and the cap are internal commercial facts. The customer sees the
    offer they were made, not the negotiation behind it."""
    if not ctx["linked"]:
        return _onboarding(ctx, "quotes")
    try:
        rows = scoped_rows(
            """SELECT q.quote_id::text, q.quote_number, q.status, q.currency,
                      q.subtotal, q.discount_amount, q.total, q.valid_until,
                      q.version, q.created_at
               FROM quotes q
               WHERE q.account_id = %(account_id)s::uuid
                 AND q.status IN ('sent','accepted','declined','expired')
               ORDER BY q.created_at DESC
               LIMIT %(lim)s""", {"lim": max(1, min(int(limit), 100))})
    except Exception as exc:
        # A database without the C3.0 migration answers honestly instead of 500.
        logger.debug(f"[portal] quotes unavailable: {exc}")
        return {"ok": True, "linked": True, "quotes": [],
                "unavailable": "quotes are not enabled on this deployment"}
    return {"ok": True, "linked": True, "quotes": _money(rows)}


# ============================================================================
# CUSTOMER AI — the same boundary, a different door
# ============================================================================
# "Show my recent orders" and clicking Orders must return the SAME authorized
# data. So the AI does NOT get its own data path: it picks one of the reads
# above and the read runs exactly as the page runs it, under the same scope
# opened by the same dependency.
#
# The model chooses WHICH question to answer. It never chooses WHOSE data to
# answer it from — that is settled by the session before the model is reached,
# and a model that could widen its own access would be a second authorization
# system with a language interface.

_INTENTS = {
    "orders":   ("your recent orders", portal_orders),
    "invoices": ("your invoices", portal_invoices),
    "cases":    ("your support cases", portal_cases),
    "quotes":   ("your quotes", portal_quotes),
    "profile":  ("your profile", portal_me),
}

# Deterministic first. A customer asking "where is my order" should not need an
# LLM round trip, and the keyword path cannot invent an intent that is not in
# the table above.
_PATTERNS = (
    ("invoices", r"\binvoice|\bbill|\bowe|\boverdue|\bbalance|\bpay"),
    ("orders",   r"\border|\bpurchase|\bshipment|\bdeliver|\bbought"),
    ("cases",    r"\bcase|\bticket|\bsupport|\bissue|\bproblem|\bcomplaint"),
    ("quotes",   r"\bquote|\bquotation|\bprice|\boffer|\bproposal"),
    ("profile",  r"\bprofile|\bmy account|\bmy details|\bwho am i"),
)


def _route_question(text: str) -> Optional[str]:
    import re as _re
    t = (text or "").lower()
    for intent, pattern in _PATTERNS:
        if _re.search(pattern, t):
            return intent
    return None


@router.get("/ask")
def portal_ask(q: str = "",
               ctx: Dict[str, Any] = Depends(customer_context)) -> Dict[str, Any]:
    """Answer a customer's question from THEIR OWN data.

    Identical authorization to the pages: same dependency, same scope, same
    reads. `answered_with` names the endpoint used so a caller can verify the
    AI and the UI agree — and so a support engineer can reproduce the answer.

    An unrecognised question does NOT fall back to a broad search. It says what
    it can answer, because a customer-facing assistant that guesses is a
    customer-facing assistant that eventually guesses wrong about money.
    """
    question = (q or "").strip()
    if not question:
        return {"ok": True, "answer": "Ask me about your orders, invoices, "
                                      "support cases or quotes.",
                "can_answer": sorted(_INTENTS)}

    intent = _route_question(question)
    if not intent:
        return {"ok": True, "understood": False,
                "answer": "I can help with your orders, invoices, support "
                          "cases and quotes. Could you rephrase?",
                "can_answer": sorted(_INTENTS)}

    label, fn = _INTENTS[intent]
    # THE SAME FUNCTION THE PAGE CALLS. Not a copy, not a broader query.
    data = fn(ctx=ctx)
    if not data.get("linked"):
        return {"ok": True, "understood": True, "intent": intent,
                "linked": False, "answer": data.get("message"),
                "answered_with": f"/portal/{intent}"}
    return {"ok": True, "understood": True, "intent": intent, "linked": True,
            "answer": f"Here is {label}.",
            "answered_with": f"/portal/{intent}",
            "data": data}


@router.get("/health")
def portal_health() -> Dict[str, Any]:
    """Unauthenticated liveness only — never data."""
    return {"ok": True, "read_only": True,
            "scope_model": "account_id via write_guard.set_customer_scope"}
