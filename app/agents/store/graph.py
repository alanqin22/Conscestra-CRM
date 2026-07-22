"""Store Graph  v1.0

Simplified 3-node LangGraph topology — no AI Agent, no parse node.

    pre_router ──[direct_db]──→ db ──→ format ──→ END
               └──[error]──────────────────────→ END

Because every store operation is deterministic, the AI agent and parse nodes
from build_standard_graph() are not needed.  We build a custom graph here.

The db_node has special handling for the checkout 'mode':
  Step A — sp_orders create (status='processing')
  Step B — sp_store checkout_add_items (add all items with computed line values)
  Step C — sp_orders change_status → 'pending' (fires trigger cascade)
  Step D — sp_store get_invoice_by_order (captures auto-created invoice)

All other modes are a single execute_sp() call.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from langgraph.graph import StateGraph, END

from app.core.graph_utils import AgentState
from app.core.database import execute_sp

from .pre_router import route_request
from .sql_builder import build_store_query, CHECKOUT_SENTINEL
from .formatter import format_response
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)


# ============================================================================
# NODES
# ============================================================================

def pre_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-Router — all store requests are deterministic, always routed."""
    logger.info("=== Store Pre-Router Node ===")
    user_input = state.get("user_input", "")
    chat_input = state.get("chat_input", {})

    result = route_request(user_input, chat_input)

    if result["router_action"]:
        params = result["params"]
        logger.info(f"Store pre-router ROUTED → sp={params.get('sp')} mode={params.get('mode')}")
        return {**state, "router_action": True, "parsed_json": params, "should_call_api": True}

    logger.warning("Store pre-router UNRECOGNISED — returning error")
    return {**state, "router_action": False, "should_call_api": False,
            "final_output": json.dumps({
                "status": "error",
                "mode":   "unknown",
                "sp":     "unknown",
                "error":  f"Unrecognised store context: {chat_input.get('storeData', {}).get('context', 'none')}",
            })}


def db_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Database node.

    Standard modes  → single execute_sp() call.
    Checkout mode   → 4-step sequence (A/B/C/D) with compensation on failure.
    """
    logger.info("=== Store DB Node ===")

    if not state.get("should_call_api"):
        return state

    params = state.get("parsed_json") or {}
    mode   = params.get("mode", "")
    sp     = params.get("sp", "")

    # ── CHECKOUT — multi-step ─────────────────────────────────────────────────
    if sp == "checkout" and mode == "checkout":
        return _checkout_node(state, params)

    # ── STANDARD — single SP call ─────────────────────────────────────────────
    try:
        query, _ = build_store_query(params)
        db_rows  = execute_sp(query)
        logger.info(f"Store SP returned {len(db_rows)} rows")
        return {**state, "db_rows": db_rows}
    except WritePermissionError:
        raise
    except Exception as exc:
        logger.error(f"Store DB error: {exc}", exc_info=True)
        error_row = [{"result": {
            "metadata": {"status": "error", "code": -500, "message": str(exc)}
        }}]
        return {**state, "db_rows": error_row}


def _resolve_buyer(contact_id: Optional[str] = None,
                   email: Optional[str] = None) -> Dict[str, Any]:
    """Resolve {account_id, contact_id} for a checkout from a contact_id or the
    buyer's email — used when the signed-in session didn't carry the account_id
    (e.g. a converted lead whose session only persisted lead_id/email).
    Prefers a verified contact; falls back to any contact, then the converted lead."""
    # 1) direct by contact_id
    if contact_id:
        rows = execute_sp(
            "SELECT jsonb_build_object('account_id', account_id, 'contact_id', contact_id) AS result "
            "FROM contacts WHERE contact_id = %(cid)s::uuid AND account_id IS NOT NULL LIMIT 1",
            {"cid": contact_id})
        r = (rows[0].get("result") if rows else None) or {}
        if r.get("account_id"):
            return r
    # 2) by email — verified contact first, then any contact, then converted lead
    if email:
        rows = execute_sp(
            """SELECT jsonb_build_object('account_id', x.account_id,
                                         'contact_id', x.contact_id) AS result
               FROM (
                   SELECT c.account_id, c.contact_id, 1 AS pri,
                          COALESCE(c.is_email_verified, false)::int AS vrank, c.created_at
                   FROM contacts c
                   WHERE lower(c.email) = lower(%(em)s) AND c.account_id IS NOT NULL
                   UNION ALL
                   SELECT l.converted_account_id, l.converted_contact_id, 2 AS pri,
                          0 AS vrank, l.created_at
                   FROM leads l
                   WHERE lower(l.email) = lower(%(em)s)
                     AND COALESCE(l.converted, false) AND l.converted_account_id IS NOT NULL
               ) x
               ORDER BY x.pri, x.vrank DESC, x.created_at
               LIMIT 1""",
            {"em": email})
        r = (rows[0].get("result") if rows else None) or {}
        if r.get("account_id"):
            return r
    return {}


def _checkout_node(state: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runs the 4-step checkout sequence and returns assembled result in db_rows.

    Step A — create order (status='processing')   → no trigger
    Step B — sp_store checkout_add_items            → trgfn_order_items_update_order
    Step C — change_status → 'pending'             → full trigger cascade
    Step D — get_invoice_by_order                  → captures auto-created invoice
    """
    order_id     = None
    order_number = None

    def _exec(sql: str) -> Dict:
        rows = execute_sp(sql)
        return (rows[0].get("result") or {}) if rows else {}

    def _sp_ok(data: Dict) -> bool:
        return (data.get("metadata") or {}).get("status") == "success"

    # ── Step 0: resolve the billing account / contact ─────────────────────────
    # account_id is required to create the order. A signed-in session may only
    # carry the buyer's email (e.g. a converted lead whose session didn't persist
    # the account/contact IDs), so resolve from contact_id, then email.
    account_id = params.get("accountId")
    contact_id = params.get("contactId")
    if not account_id:
        resolved = _resolve_buyer(contact_id=contact_id, email=params.get("email"))
        account_id = resolved.get("account_id")
        contact_id = contact_id or resolved.get("contact_id")
        if not account_id:
            return {**state, "db_rows": [{
                "error": "We couldn't match your sign-in to a billing account. "
                         "Please make sure you're signed in with the email you registered.",
                "step": "0"}]}
        logger.info(f"Checkout Step 0 — resolved account_id={account_id} contact_id={contact_id}")

    # ── Step A: create order as 'processing' ─────────────────────────────────
    try:
        query_a, _ = build_store_query({
            "sp":        "sp_orders",
            "mode":      "create",
            "accountId": account_id,
            "contactId": contact_id,
            "createdBy": params.get("createdBy"),
        })
        data_a = _exec(query_a)
        if not _sp_ok(data_a):
            msg = (data_a.get("metadata") or {}).get("message", "create failed")
            return {**state, "db_rows": [{"error": msg, "step": "A"}]}

        order_obj    = data_a.get("order") or {}
        order_id     = order_obj.get("order_id")
        order_number = order_obj.get("order_number")

        if not order_id:
            return {**state, "db_rows": [{"error": "Step A: no order_id returned", "step": "A"}]}

        logger.info(f"Checkout Step A OK — order_id={order_id} order_number={order_number}")

    except Exception as exc:
        logger.error(f"Checkout Step A failed: {exc}", exc_info=True)
        return {**state, "db_rows": [{"error": f"Step A: {exc}", "step": "A"}]}

    # ── Step B: checkout_add_items — insert items with computed line values ────
    #
    # WHY NOT sp_orders(batch_update):
    #   batch_update inserts order_items without line_subtotal/line_total/description.
    #   trgfn_order_items_update_order computes: orders.total_amount = SUM(NULL) = 0.
    #   Zero flows into the invoice trigger guard → $0 invoice, no auto-payment.
    #
    # sp_store(checkout_add_items) inserts each item with:
    #   description   = product name (from products table)
    #   line_subtotal = qty × retail price  ← trigger reads this correctly
    #   line_total    = qty × retail price
    #   discount      = 0.00
    #   After each INSERT, trgfn_order_items_update_order accumulates the real total.
    # ─────────────────────────────────────────────────────────────────────────
    try:
        import json as _json
        items_list = [
            {"product_id": item["product_id"], "quantity": int(item["quantity"])}
            for item in (params.get("items") or [])
        ]

        query_b, _ = build_store_query({
            "sp":        "sp_store",
            "mode":      "checkout_add_items",
            "orderId":   order_id,
            "items":     items_list,
            "createdBy": params.get("createdBy"),
        })
        data_b = _exec(query_b)
        if not _sp_ok(data_b):
            msg = (data_b.get("metadata") or {}).get("message", "checkout_add_items failed")
            try:
                from .sql_builder import _uuid
                execute_sp(f"""SELECT sp_orders(
                  p_mode := 'update', p_action := 'change_status',
                  p_order_id := {_uuid(order_id)}, p_status := 'cancelled'
                ) AS result;""")
                logger.info(f"Compensation cancel sent for order {order_id}")
            except Exception:
                pass
            return {**state, "db_rows": [{"error": msg, "step": "B",
                                           "order_id": order_id, "order_number": order_number}]}

        items_inserted = data_b.get("items_inserted", 0)
        skipped = data_b.get("skipped_items", [])
        if skipped:
            logger.warning(f"Checkout Step B: {len(skipped)} item(s) skipped: {skipped}")
        logger.info(f"Checkout Step B OK — {items_inserted} item(s) inserted via checkout_add_items")

    except Exception as exc:
        logger.error(f"Checkout Step B failed: {exc}", exc_info=True)
        return {**state, "db_rows": [{"error": f"Step B: {exc}", "step": "B",
                                       "order_id": order_id, "order_number": order_number}]}

    # ── Step B2: apply a coupon (optional) — BEFORE status→pending so the
    # invoice (created by trgfn_order_create_invoice from orders.total_amount)
    # reflects the discount. Never touches order_items (would refire the line
    # trigger). Failure/invalid coupon is non-fatal: order proceeds full price.
    coupon_result = None
    coupon_code = params.get("couponCode")
    if coupon_code:
        try:
            from app.core import promotions
            coupon_result = promotions.apply_coupon_to_order(
                order_id, coupon_code,
                account_id=account_id, contact_id=contact_id)
            if coupon_result.get("applied"):
                logger.info(f"Checkout Step B2 OK — coupon {coupon_result.get('code')} "
                            f"applied -${coupon_result.get('discount_amount')}")
            else:
                logger.info(f"Checkout Step B2 — coupon not applied: "
                            f"{coupon_result.get('reason') or coupon_result}")
        except Exception as exc:
            logger.warning(f"Checkout Step B2 (coupon) skipped: {exc}")

    # ── Step C: change_status → 'pending' — fires trigger cascade ────────────
    try:
        query_c, _ = build_store_query({
            "sp":        "sp_orders",
            "mode":      "change_status",
            "orderId":   order_id,
            "updatedBy": params.get("createdBy"),
        })
        data_c = _exec(query_c)
        if not _sp_ok(data_c):
            msg = (data_c.get("metadata") or {}).get("message", "change_status failed")
            return {**state, "db_rows": [{"error": msg, "step": "C",
                                           "order_id": order_id, "order_number": order_number}]}
        logger.info("Checkout Step C OK — status→pending, trigger cascade fired")

    except Exception as exc:
        logger.error(f"Checkout Step C failed: {exc}", exc_info=True)
        return {**state, "db_rows": [{"error": f"Step C: {exc}", "step": "C",
                                       "order_id": order_id, "order_number": order_number}]}

    # ── Step D: capture the auto-created invoice ──────────────────────────────
    invoice_id     = None
    invoice_number = None
    invoice_status = "issued"
    total_amount   = None
    currency       = None

    try:
        query_d, _ = build_store_query({
            "sp":      "sp_store",
            "mode":    "get_invoice_by_order",
            "orderId": order_id,
        })
        data_d = _exec(query_d)
        if _sp_ok(data_d):
            inv            = data_d.get("invoice") or {}
            invoice_id     = inv.get("invoice_id")
            invoice_number = inv.get("invoice_number")
            total_amount   = inv.get("total_amount")
            currency       = inv.get("currency")
            invoice_status = inv.get("status", "issued")
            logger.info(f"Checkout Step D OK — invoice_id={invoice_id} "
                        f"invoice_number={invoice_number} status={invoice_status} "
                        f"total={total_amount}")
        else:
            invoice_status = "issued"
            logger.warning("Checkout Step D: invoice lookup failed (order created successfully)")

    except Exception as exc:
        logger.warning(f"Checkout Step D warning: {exc}")

    # ── Assemble success result ───────────────────────────────────────────────
    result = {
        "order_id":       order_id,
        "order_number":   order_number,
        "invoice_id":     invoice_id,
        "invoice_number": invoice_number,
        "invoice_status": invoice_status,   # 'paid' when trigger chain ran correctly
        "total_amount":   total_amount,
        "currency":       currency,
    }
    if coupon_result:
        if coupon_result.get("applied"):
            result["coupon"] = {"code": coupon_result.get("code"),
                                "discount_amount": coupon_result.get("discount_amount")}
        else:
            # Surface why a supplied code didn't apply (e.g. min-subtotal, expired,
            # needs approval) so the UI can tell the shopper.
            result["coupon_error"] = coupon_result.get("reason") or (
                "needs approval" if coupon_result.get("requires_approval") else "not applied")
    return {**state, "db_rows": [result]}


def formatter_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Formatter node — converts db_rows to JSON envelope for the store HTML."""
    logger.info("=== Store Formatter Node ===")
    try:
        db_rows    = state.get("db_rows") or []
        parsed_json = state.get("parsed_json") or {}

        if state.get("should_call_api"):
            final_output = format_response(db_rows, parsed_json)
        else:
            # pre_router already set final_output for error cases
            final_output = state.get("final_output") or json.dumps({
                "status": "error", "mode": "unknown", "sp": "unknown",
                "error":  "No operation performed",
            })

        return {**state, "final_output": final_output}

    except Exception as exc:
        logger.error(f"Store formatter error: {exc}", exc_info=True)
        return {**state, "final_output": json.dumps({
            "status": "error", "mode": "format", "sp": "unknown",
            "error":  f"Formatter error: {exc}",
        })}


# ============================================================================
# GRAPH SINGLETON — custom 3-node topology (no AI agent)
# ============================================================================

_graph_app = None


def get_graph():
    global _graph_app
    if _graph_app is None:
        logger.info("Building Store LangGraph (3-node, no AI agent)...")

        graph = StateGraph(AgentState)
        graph.add_node("pre_router", pre_router_node)
        graph.add_node("db",         db_node)
        graph.add_node("format",     formatter_node)

        graph.set_entry_point("pre_router")

        def _route_after_pre_router(state):
            # Always go to db — error state is handled inside db_node
            return "db"

        graph.add_conditional_edges(
            "pre_router",
            _route_after_pre_router,
            {"db": "db"},
        )
        graph.add_edge("db",     "format")
        graph.add_edge("format", END)

        _graph_app = graph.compile()
        logger.info("Store LangGraph built successfully")
    return _graph_app
