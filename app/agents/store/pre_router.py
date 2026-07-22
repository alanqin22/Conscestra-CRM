"""Store Pre-Router  v1.0

All store operations are deterministic — every message maps directly to a
specific sp_store / sp_products / sp_orders / sp_accounting mode.
There is NO AI agent passthru path for this module.

Context values sent by store-home.html  (via storeData.context):
  'list_products'            → sp_products list
  'get_product_details'      → sp_products get_details
  'get_active_categories'    → sp_store get_active_categories
  'contact_search'           → sp_orders contact_search
  'checkout'                 → 3-step: sp_orders create / batch_update / change_status
                               + sp_store get_invoice_by_order
  'get_order_detail'         → sp_orders get_detail
  'get_invoice_360'          → sp_accounting get_invoice_360
  'get_account_balance'      → sp_accounting account_balance
  'get_product_metadata'     → sp_store get_product_metadata
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def route_request(message: str, chat_input: dict) -> Dict[str, Any]:
    """
    Inspect the incoming message / storeData and return a routing decision.

    Always returns router_action=True — every store operation is deterministic.
    Returns router_action=False only for genuinely unrecognised messages so
    the graph formatter can return a clean error without crashing.
    """
    logger.info("=== Store Pre-Router v1.0 ===")
    logger.info(f"Message: {(message or '')[:120]}")

    def routed(params: dict) -> dict:
        logger.info(f"→ ROUTED: sp={params.get('sp')} mode={params.get('mode')}")
        return {"router_action": True, "params": params}

    def unrecognised(reason: str = "") -> dict:
        logger.warning(f"→ UNRECOGNISED{f' ({reason})' if reason else ''}")
        return {"router_action": False}

    sd = chat_input.get("storeData") or {}
    context = sd.get("context") or ""

    # ── routerAction short-circuit ────────────────────────────────────────────
    # HTML can send routerAction=True + sp + mode directly without storeData
    if chat_input.get("routerAction") and chat_input.get("sp"):
        _skip = {"routerAction", "message", "sessionId", "chatInput"}
        params = {k: v for k, v in chat_input.items()
                  if k not in _skip and v is not None}
        return routed(params)

    # ─────────────────────────────────────────────────────────────────────────
    # storeData.context routing — highest priority
    # ─────────────────────────────────────────────────────────────────────────

    if context == "list_products":
        return routed({
            "sp":              "sp_products",
            "mode":            "list",
            "isActiveFilter":  True,
            "isSynthetic":     False,
            "search":          sd.get("search") or None,
            "categoryFilter":  sd.get("category_id") or None,
            "pageSize":        int(sd.get("page_size") or 24),
            "pageNumber":      int(sd.get("page_number") or 1),
            "sortField":       sd.get("sort_field") or "name",
            "sortOrder":       sd.get("sort_order") or "ASC",
        })

    if context == "get_product_details":
        return routed({
            "sp":            "sp_products",
            "mode":          "get_details",
            "productId":     sd.get("product_id") or None,
            "productNumber": int(sd["product_number"]) if sd.get("product_number") else None,
            "sku":           sd.get("sku") or None,
        })

    if context == "get_active_categories":
        return routed({
            "sp":    "sp_store",
            "mode":  "get_active_categories",
            "search": sd.get("search") or None,
        })

    if context == "contact_search":
        search = sd.get("search") or ""
        if len(search) < 3:
            return unrecognised("contact_search requires 3+ chars")
        return routed({
            "sp":     "sp_orders",
            "mode":   "contact_search",
            "search": search,
        })

    if context == "checkout":
        # Full checkout payload — passed directly; sql_builder handles multi-step
        items = sd.get("items") or []
        if not items:
            return unrecognised("checkout has no items")
        account_id = sd.get("account_id") or None
        contact_id = sd.get("contact_id") or None
        email      = (sd.get("email") or "").strip() or None
        # We need SOME way to identify the buyer. account_id is ideal, but a
        # signed-in session may only carry the email (e.g. a converted lead whose
        # session didn't persist the account/contact IDs) — the checkout node
        # resolves account_id/contact_id from contact_id or email when missing.
        if not account_id and not contact_id and not email:
            return unrecognised("checkout missing account_id / contact_id / email")
        return routed({
            "sp":         "checkout",       # special marker — sql_builder handles
            "mode":       "checkout",
            "accountId":  account_id,
            "contactId":  contact_id,
            "email":      email,
            "createdBy":  sd.get("created_by") or None,
            "notes":      sd.get("notes") or None,
            "items":      items,            # [{product_id, quantity}]
            "couponCode": (sd.get("coupon_code") or "").strip() or None,
        })

    if context == "get_order_detail":
        return routed({
            "sp":          "sp_orders",
            "mode":        "get_detail",
            "orderNumber": sd.get("order_number") or None,
            "orderId":     sd.get("order_id") or None,
        })

    if context == "get_invoice_360":
        invoice_id = sd.get("invoice_id")
        if not invoice_id:
            return unrecognised("get_invoice_360 missing invoice_id")
        return routed({
            "sp":        "sp_accounting",
            "mode":      "get_invoice_360",
            "invoiceId": invoice_id,
        })

    if context == "get_account_balance":
        account_id = sd.get("account_id")
        if not account_id:
            return unrecognised("get_account_balance missing account_id")
        return routed({
            "sp":        "sp_accounting",
            "mode":      "account_balance",
            "accountId": account_id,
        })

    if context == "get_product_metadata":
        product_id = sd.get("product_id")
        if not product_id:
            return unrecognised("get_product_metadata missing product_id")
        return routed({
            "sp":        "sp_store",
            "mode":      "get_product_metadata",
            "productId": product_id,
        })

    # ─────────────────────────────────────────────────────────────────────────
    # No matching context — return unrecognised so formatter emits clean error
    # ─────────────────────────────────────────────────────────────────────────
    return unrecognised(f"unknown context: '{context}'")
