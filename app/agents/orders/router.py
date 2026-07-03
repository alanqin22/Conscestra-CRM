"""FastAPI router for the Orders domain.

Endpoints:
  POST /order-chat   — primary endpoint
  POST /orders-chat  — alias (HTML may use either)
  GET  /order-health

Version: 4.1.0

Drop-in replacement for the standalone order_agent endpoints.
Key differences from simpler agents:
  - chatInput.batchData carries structured form data
  - format_response returns a dict (not str) with side-channel fields
    (order_id, order_number, entity, reportMode, result)
  - A path-normalisation middleware collapses leading '//'
  - The initial_state carries extra fields (body, current_message, raw_message)
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.write_guard import WritePermissionError

from .graph import get_graph

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Orders"])


# ============================================================================
# REQUEST MODELS
# ============================================================================

class BatchData(BaseModel):
    """Structured batchData sent by the web page for direct operations."""
    context:            Optional[str]  = None
    action:             Optional[str]  = None
    mode:               Optional[str]  = None
    order_id:           Optional[str]  = None
    order_number:       Optional[str]  = None
    account_id:         Optional[str]  = None
    contact_id:         Optional[str]  = None
    product_id:         Optional[str]  = None
    product_pricing_id: Optional[str]  = None
    order_item_id:      Optional[str]  = None
    created_by:         Optional[str]  = None
    updated_by:         Optional[str]  = None
    status:             Optional[str]  = None
    order_date:         Optional[str]  = None
    quantity:           Optional[int]  = None
    price_type:         Optional[str]  = None
    payload:            Optional[Dict[str, Any]] = None
    include_deleted:    Optional[bool] = None
    force_hard_delete:  Optional[bool] = None
    page_size:          Optional[int]  = None
    page_number:        Optional[int]  = None
    search:             Optional[str]  = None
    year:               Optional[int]  = None
    category_id:        Optional[str]  = None
    sort_field:         Optional[str]  = None
    sort_order:         Optional[str]  = None
    # camelCase aliases
    orderId:            Optional[str]  = None
    orderNumber:        Optional[str]  = None
    accountId:          Optional[str]  = None
    contactId:          Optional[str]  = None
    productId:          Optional[str]  = None
    productPricingId:   Optional[str]  = None
    orderItemId:        Optional[str]  = None
    createdBy:          Optional[str]  = None
    updatedBy:          Optional[str]  = None
    orderDate:          Optional[str]  = None
    priceType:          Optional[str]  = None
    includeDeleted:     Optional[bool] = None
    forceHardDelete:    Optional[bool] = None
    pageSize:           Optional[int]  = None
    pageNumber:         Optional[int]  = None
    categoryId:         Optional[str]  = None
    sortField:          Optional[str]  = None
    sortOrder:          Optional[str]  = None
    startDate:          Optional[str]  = None
    endDate:            Optional[str]  = None


class OrderChatInput(BaseModel):
    message:   Optional[str]       = None
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent
    batchData: Optional[BatchData] = None


class OrderChatRequest(BaseModel):
    sessionId:  Optional[str]            = None
    chatInput:  Optional[OrderChatInput] = None
    message:    Optional[str]            = None


# ============================================================================
# HELPERS
# ============================================================================

def _build_body(req: OrderChatRequest) -> dict:
    """Reconstruct the 'body' dict the pre-router expects."""
    body: dict = {}
    if req.sessionId:
        body["sessionId"] = req.sessionId
    if req.message:
        body["message"] = req.message
    if req.chatInput:
        ci = req.chatInput
        ci_dict: dict = {}
        if ci.message is not None:
            ci_dict["message"] = ci.message
        if ci.batchData is not None:
            bd = ci.batchData
            bd_dict: dict = {}
            # Merge snake_case → camelCase; camelCase wins on conflict
            for snake, camel in [
                ("order_id",           "orderId"),
                ("order_number",       "orderNumber"),
                ("account_id",         "accountId"),
                ("contact_id",         "contactId"),
                ("product_id",         "productId"),
                ("product_pricing_id", "productPricingId"),
                ("order_item_id",      "orderItemId"),
                ("created_by",         "createdBy"),
                ("updated_by",         "updatedBy"),
                ("order_date",         "orderDate"),
                ("price_type",         "priceType"),
                ("include_deleted",    "includeDeleted"),
                ("force_hard_delete",  "forceHardDelete"),
                ("page_size",          "pageSize"),
                ("page_number",        "pageNumber"),
                ("category_id",        "categoryId"),
                ("sort_field",         "sortField"),
                ("sort_order",         "sortOrder"),
            ]:
                snake_v = getattr(bd, snake, None)
                camel_v = getattr(bd, camel, None)
                merged  = camel_v if camel_v is not None else snake_v
                if merged is not None:
                    bd_dict[camel] = merged
            # Pass-through fields (name identical in both cases)
            for f in ("context", "action", "mode", "status", "quantity",
                      "payload", "search", "year", "startDate", "endDate"):
                v = getattr(bd, f, None)
                if v is not None:
                    bd_dict[f] = v
            ci_dict["batchData"] = bd_dict
        body["chatInput"] = ci_dict
    return body


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/order-health")
async def order_health():
    return {
        "status":  "healthy",
        "module":  "orders",
        "version": "4.1.0",
        "graph_initialized": get_graph() is not None,
    }


async def _handle_chat(req: OrderChatRequest) -> JSONResponse:
    """Shared handler used by both /order-chat and /orders-chat."""
    logger.info("=== Orders Chat Request ===")
    body       = _build_body(req)
    chat_input = body.get("chatInput", {})
    session_id = req.sessionId or body.get("sessionId") or "default-session"

    user_input = chat_input.get("message") or req.message or ""
    raw_msg    = user_input
    logger.info(f"Session: {session_id!r}  Input: {user_input[:120]}")

    initial_state = {
        "session_id":       session_id,
        "body":             body,
        "chat_input":       chat_input,
        "user_input":       user_input,
        "current_message":  user_input,
        "raw_message":      raw_msg,
        "router_action":    False,
        "params":           None,
        "ai_output":        None,
        "parsed_json":      None,
        "should_call_api":  False,
        "db_rows":          None,
        "format_result":    None,
        "final_output":     None,
    }

    try:
        graph  = get_graph()
        result = graph.invoke(initial_state)

        fmt    = result.get("format_result") or {}
        output = result.get("final_output") or fmt.get("output", "")

        response: dict = {
            "output":     output,
            "mode":       fmt.get("mode",       "unknown"),
            "reportMode": fmt.get("reportMode", "generic"),
            "entity":     fmt.get("entity",     "orders"),
            "success":    fmt.get("success",    True),
        }
        # Pass through optional side-channel fields from the formatter
        for key in ("context", "order_id", "order_number", "result"):
            if fmt.get(key) is not None:
                response[key] = fmt[key]

        logger.info(
            f"Orders response — mode={response['mode']} "
            f"entity={response['entity']} len={len(output)}"
        )
        return JSONResponse(content=response)

    except WritePermissionError as e:
        return JSONResponse(
            status_code=e.http_status,
            content={"detail": str(e), "output": str(e), "success": False},
        )
    except Exception as e:
        logger.error(f"Orders graph error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"output": f"Agent error: {str(e)}", "success": False},
        )


@router.post("/order-chat")
async def order_chat(req: OrderChatRequest):
    return await _handle_chat(req)


@router.post("/orders-chat")
async def orders_chat(req: OrderChatRequest):
    """Alias — some HTML pages may POST to /orders-chat."""
    return await _handle_chat(req)
