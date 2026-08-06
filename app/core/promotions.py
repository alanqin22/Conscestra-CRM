"""Promotions & coupons — the read/validate layer the Store agent uses to
answer discount questions HONESTLY from real data (never an invented offer).

Data model: sql/promotions_coupons.sql (coupons, coupon_redemptions,
price_match_requests). Design + authority tiers: docs/promotions_coupons_design.md.

Everything here degrades gracefully — nothing raises into a customer turn — but
a degraded read is never dressed up as a real answer. A failed lookup returns
`lookup_failed=True` with its own reason, distinct from a code that genuinely
does not exist, and is logged at WARNING and counted in promotions_health().

That distinction is not cosmetic. Between 2026-07-21 and 2026-08-05 the coupons
tables were missing from production; every lookup raised, every raise was
swallowed into "no such coupon" at DEBUG level, and every customer who typed a
valid code was told it did not exist. Nothing alerted, because a rejection is
what a wrong code looks like. An outage that returns a plausible answer is more
expensive than one that returns an error.

Authority (enforced by callers, documented here):
  AUTOMATIC  active published Promo price (product_pricing) + a VALID coupon
             within its window/limits and under the brand discount cap.
  APPROVAL   competitor price-match, or a coupon flagged requires_approval /
             above brand.max_discount_pct → governance.propose(), never auto.
  NEVER      arbitrarily changing a listed price.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.core.database import get_connection

logger = logging.getLogger("promotions")

# Lookup failures, so "the promotions layer is broken" is a value something can
# read rather than a line in a log nobody tails. Process-local and unbounded in
# neither direction: four counters, no history.
_FAILURES: Dict[str, Dict[str, Any]] = {}


def _lookup_failed(fn: str, exc: BaseException) -> None:
    """Record an infrastructure failure. WARNING, not DEBUG — the caller is
    about to return an answer that looks like a normal negative result."""
    slot = _FAILURES.setdefault(fn, {"count": 0, "last_error": None, "last_at": None})
    slot["count"] += 1
    slot["last_error"] = f"{type(exc).__name__}: {exc}"
    slot["last_at"] = time.time()
    logger.warning(f"[promotions] {fn} lookup FAILED (not a negative result): {exc}")


def promotions_health() -> Dict[str, Any]:
    """Whether the promotions layer is answering from data or from exceptions.

    ok=False means at least one lookup has failed this process. A caller that
    sees ok=False should not report "no coupons" as a fact."""
    total = sum(s["count"] for s in _FAILURES.values())
    return {"ok": total == 0, "total_failures": total,
            "by_function": {k: dict(v) for k, v in _FAILURES.items()}}


# ============================================================================
# READ — advertisable coupons
# ============================================================================

def active_public_coupons(product: Optional[Dict[str, Any]] = None,
                          limit: int = 5) -> List[Dict[str, Any]]:
    """Live, advertisable coupons the agent may mention unprompted. Filters to
    the viewed product's scope when a product is given (all / its category /
    itself). Never raises — returns [] when the table is missing or empty.

    [] is ambiguous by construction. Callers that need to tell "no offers" from
    "could not look" must use _checked() below; this signature is kept because
    the ambiguity is harmless to anyone who only wants something to display."""
    return _active_public_coupons_checked(product, limit)[0]


def _active_public_coupons_checked(
        product: Optional[Dict[str, Any]] = None,
        limit: int = 5) -> tuple[List[Dict[str, Any]], bool]:
    """(coupons, lookup_succeeded). The second value is the whole point: an
    empty list with ok=False means we do not know what offers exist, which is
    a different sentence to a shopper than "there are none"."""
    category_id = (product or {}).get("category_id")
    product_id = (product or {}).get("product_id")
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT code, description, discount_type, discount_value,
                              min_subtotal, max_discount, applies_to, ends_at
                         FROM coupons
                        WHERE is_active = true AND is_public = true
                          AND starts_at <= now()
                          AND (ends_at IS NULL OR ends_at > now())
                          AND (usage_limit IS NULL OR times_redeemed < usage_limit)
                          AND (applies_to = 'all'
                               OR (applies_to = 'category' AND category_id = %(cat)s::uuid)
                               OR (applies_to = 'product'  AND product_id  = %(pid)s::uuid))
                        ORDER BY discount_value DESC
                        LIMIT %(lim)s""",
                    {"cat": category_id, "pid": product_id, "lim": limit})
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()], True
        finally:
            conn.close()
    except Exception as exc:
        # Still empty — the agent must not raise mid-turn — but paired with
        # ok=False so the emptiness cannot pass for "this product has no offers".
        _lookup_failed("active_public_coupons", exc)
        return [], False


def _coupon_line(c: Dict[str, Any]) -> str:
    if c["discount_type"] == "percent":
        amt = f"{float(c['discount_value']):g}% off"
        if c.get("max_discount"):
            amt += f" (up to ${float(c['max_discount']):,.0f})"
    else:
        amt = f"${float(c['discount_value']):,.2f} off"
    cond = (f" on orders over ${float(c['min_subtotal']):,.0f}"
            if c.get("min_subtotal") else "")
    return f"code {c['code']} — {amt}{cond}"


# What the model is told when we could not read the offer table. It is phrased
# as an instruction because the consuming prompt renders an empty block as
# "None available." — an assertion we have no basis for during an outage. The
# shopper hears "I can't confirm", never "there are none".
_OFFERS_UNKNOWN = (
    "UNKNOWN — the promotions lookup failed, so we do not know what offers "
    "exist right now.\n"
    "Do NOT say there are no discounts, nothing is on sale, or that you "
    "checked — any of those may be false.\n"
    "Say you can't confirm current promotions this moment, and offer to "
    "connect the shopper with a specialist who can.")


def summarize_for_agent(product: Optional[Dict[str, Any]] = None) -> str:
    """One short block for the agent prompt: the REAL advertisable coupons,
    or '' when there genuinely are none, or an explicit UNKNOWN when the
    lookup failed.

    The three cases were previously two. '' meant both "no offers" and "the
    query raised", and the caller renders '' as "[ACTIVE PROMOTIONS] None
    available." — so a failed read put a false statement into the prompt and
    the model repeated it to the shopper with full confidence. An agent that
    cannot distinguish silence from ignorance will assert the first one."""
    coupons, ok = _active_public_coupons_checked(product)
    if coupons:
        return "Active coupons the shopper can use now:\n" + "\n".join(
            f"- {_coupon_line(c)}" for c in coupons)
    return "" if ok else _OFFERS_UNKNOWN


# ============================================================================
# VALIDATE — a code the customer typed (does NOT redeem; that's checkout)
# ============================================================================

def validate_coupon(code: str, subtotal: float = 0.0,
                    product: Optional[Dict[str, Any]] = None,
                    account_id: Optional[str] = None) -> Dict[str, Any]:
    """Check a typed coupon code against its rules. Returns
    {ok, reason, code, discount_amount, requires_approval}. Never raises.
    Read-only: redemption happens at checkout, not here."""
    code = (code or "").strip()
    if not code:
        return {"ok": False, "reason": "no code given"}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT coupon_id, code, discount_type, discount_value,
                              min_subtotal, max_discount, applies_to, category_id,
                              product_id, ends_at, usage_limit, per_customer_limit,
                              times_redeemed, is_active, requires_approval
                         FROM coupons WHERE lower(code) = lower(%s)""", (code,))
                row = cur.fetchone()
                if not row:
                    return {"ok": False, "reason": "no such coupon", "code": code}
                cols = [d[0] for d in cur.description]
                c = dict(zip(cols, row))

                # Per-customer usage (only when we know the verified account).
                used_by_customer = 0
                if account_id and c.get("per_customer_limit") is not None:
                    cur.execute(
                        "SELECT count(*) FROM coupon_redemptions "
                        "WHERE coupon_id=%s AND account_id=%s::uuid",
                        (c["coupon_id"], account_id))
                    used_by_customer = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        # THE bug this module exists to not repeat. A missing table, a dropped
        # connection and a revoked grant all land here, and none of them mean
        # the customer's code is wrong. Saying "no such coupon" would be a
        # false statement about their coupon, not a description of our outage.
        _lookup_failed("validate_coupon", exc)
        return {"ok": False, "lookup_failed": True, "code": code,
                "reason": "coupon lookup unavailable"}

    # ── rule checks ──
    from datetime import datetime, timezone
    if not c["is_active"]:
        return {"ok": False, "reason": "coupon inactive", "code": code}
    if c["ends_at"] and c["ends_at"] < datetime.now(timezone.utc):
        return {"ok": False, "reason": "coupon expired", "code": code}
    if c["usage_limit"] is not None and c["times_redeemed"] >= c["usage_limit"]:
        return {"ok": False, "reason": "coupon fully redeemed", "code": code}
    if c["per_customer_limit"] is not None and used_by_customer >= c["per_customer_limit"]:
        return {"ok": False, "reason": "you've already used this coupon", "code": code}
    if c["applies_to"] == "category" and str((product or {}).get("category_id")) != str(c["category_id"]):
        return {"ok": False, "reason": "coupon doesn't apply to this item", "code": code}
    if c["applies_to"] == "product" and str((product or {}).get("product_id")) != str(c["product_id"]):
        return {"ok": False, "reason": "coupon doesn't apply to this item", "code": code}
    if float(subtotal or 0) < float(c["min_subtotal"] or 0):
        return {"ok": False, "code": code,
                "reason": f"needs a minimum subtotal of ${float(c['min_subtotal']):,.2f}"}

    # ── compute discount ──
    if c["discount_type"] == "percent":
        disc = float(subtotal or 0) * float(c["discount_value"]) / 100.0
        if c["max_discount"]:
            disc = min(disc, float(c["max_discount"]))
    else:
        disc = min(float(c["discount_value"]), float(subtotal or 0)) if subtotal else float(c["discount_value"])

    # Over the brand discount cap → still valid, but needs approval.
    requires_approval = bool(c["requires_approval"])
    try:
        from app.core import governance
        cap = float(governance.policy_value("brand.max_discount_pct", 15.0))
        eff_pct = (disc / float(subtotal) * 100.0) if subtotal else float(
            c["discount_value"] if c["discount_type"] == "percent" else 0)
        if eff_pct > cap:
            requires_approval = True
    except Exception:
        pass

    return {"ok": True, "code": c["code"], "discount_amount": round(disc, 2),
            "requires_approval": requires_approval,
            "reason": "valid"}


# ============================================================================
# REDEEM — apply a coupon to an order mid-checkout (AUTOMATIC tier only)
# ============================================================================

def apply_coupon_to_order(order_id: str, code: str,
                          account_id: Optional[str] = None,
                          contact_id: Optional[str] = None) -> Dict[str, Any]:
    """Apply a coupon to an order that already has its items (subtotal known)
    but is still 'processing' — i.e. between checkout step B and step C.

    Reduces orders.total_amount by the discount (the invoice trigger copies
    total_amount into the invoice at status→pending) WITHOUT touching
    order_items (which would refire the line trigger and overwrite the total),
    then records the redemption and bumps usage — all in one transaction.

    Only the AUTOMATIC tier is applied here: a coupon that validates and is
    under the brand discount cap. requires_approval / invalid → not applied
    (the order proceeds at full price; approval is a separate governed flow).
    Returns {applied, discount_amount, code, reason, requires_approval}."""
    code = (code or "").strip()
    if not code:
        return {"applied": False, "reason": "no code given"}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT subtotal_amount::float, total_amount::float "
                            "FROM orders WHERE order_id=%s::uuid", (order_id,))
                row = cur.fetchone()
            if not row:
                return {"applied": False, "reason": "order not found"}
            subtotal = float(row[1] if row[1] is not None else (row[0] or 0))

            res = validate_coupon(code, subtotal, account_id=account_id)
            if not res.get("ok"):
                return {"applied": False, **res}
            if res.get("requires_approval"):
                # Valid but over the cap / flagged — do NOT auto-apply.
                return {"applied": False, "requires_approval": True, **res}

            disc = min(float(res["discount_amount"]), subtotal)
            if disc <= 0:
                return {"applied": False, "reason": "no discount", "code": res["code"]}

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE orders SET total_amount = GREATEST(total_amount - %s, 0), "
                    "updated_at = now() WHERE order_id=%s::uuid", (disc, order_id))
                cur.execute("SELECT coupon_id FROM coupons WHERE lower(code)=lower(%s)",
                            (code,))
                cid = cur.fetchone()[0]
                cur.execute(
                    """INSERT INTO coupon_redemptions
                         (coupon_id, account_id, contact_id, order_id, discount_amount)
                       VALUES (%s, %s::uuid, %s::uuid, %s::uuid, %s)""",
                    (cid, account_id, contact_id, order_id, disc))
                cur.execute("UPDATE coupons SET times_redeemed = times_redeemed + 1 "
                            "WHERE coupon_id=%s", (cid,))
            conn.commit()
            logger.info(f"[promotions] applied {res['code']} -${disc:.2f} to order {order_id}")
            return {"applied": True, "discount_amount": round(disc, 2),
                    "code": res["code"]}
        finally:
            conn.close()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        _lookup_failed("apply_coupon_to_order", exc)
        return {"applied": False, "lookup_failed": True, "code": code,
                "reason": "could not apply coupon"}


# ============================================================================
# PRICE-MATCH — approval-required tier
# ============================================================================

def create_price_match_request(product: Dict[str, Any],
                               competitor: Dict[str, Any],
                               account_id: Optional[str] = None,
                               contact_id: Optional[str] = None,
                               channel: str = "store_chat") -> Dict[str, Any]:
    """Record a competitor price-match ask and route it to human approval via
    governance. Returns {ok, request_id, governance_ref}. Never raises."""
    name = str(product.get("name") or product.get("product_name") or "")[:200]
    try:
        gov_ref = None
        try:
            from app.core import governance
            gov_ref = governance.propose(
                "price_match", proposed_by=f"store_chat:{channel}",
                params={"product": name,
                        "product_id": product.get("product_id"),
                        "our_price": product.get("our_price"),
                        "competitor_name": competitor.get("name"),
                        "competitor_price": competitor.get("price"),
                        "competitor_url": competitor.get("url"),
                        "account_id": account_id},
                entity_type="account", entity_id=account_id,
                severity="info")
        except Exception as exc:
            logger.warning(f"[promotions] price-match governance propose failed: {exc}")

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO price_match_requests
                         (account_id, contact_id, product_id, product_name,
                          our_price, competitor_name, competitor_price,
                          competitor_url, channel, governance_ref)
                       VALUES (%(aid)s::uuid,%(cid)s::uuid,%(pid)s::uuid,%(pn)s,
                               %(op)s,%(cn)s,%(cp)s,%(cu)s,%(ch)s,%(gr)s)
                       RETURNING request_id::text""",
                    {"aid": account_id, "cid": contact_id,
                     "pid": product.get("product_id"), "pn": name,
                     "op": product.get("our_price"),
                     "cn": competitor.get("name"), "cp": competitor.get("price"),
                     "cu": competitor.get("url"), "ch": channel, "gr": gov_ref})
                rid = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "request_id": rid, "governance_ref": gov_ref}
    except Exception as exc:
        # {"ok": False} alone read as "we declined the request". It never was:
        # the ask was lost. Callers get a reason they can act on.
        _lookup_failed("create_price_match_request", exc)
        return {"ok": False, "lookup_failed": True,
                "reason": "price-match request could not be recorded"}
