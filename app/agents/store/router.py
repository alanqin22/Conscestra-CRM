"""FastAPI router for the CRM Commerce View (Store module).

Endpoints:
  POST /store-chat       — primary endpoint, matches CRM agent convention
  GET  /store-health     — health check

Version: 1.0.0

Design notes:
  • No AI agent — every request is a direct SP call via the store graph.
  • chatInput.storeData carries all structured data (context + parameters).
  • Response shape mirrors other CRM routers (sessionId, output, mode, etc.)
    so crm_index.html can navigate to store-home.html without changes.
  • output is a JSON string — store-home.html parses it for rendering.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.graph_utils import AgentState
from .graph import get_graph
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Store"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class StoreItem(BaseModel):
    """Single cart line item for checkout."""
    product_id: str
    quantity:   int = 1


class StoreData(BaseModel):
    """Structured payload for all store operations."""
    # Routing
    context:        Optional[str]  = None   # list_products | get_product_details |
                                             # get_active_categories | contact_search |
                                             # checkout | get_order_detail |
                                             # get_invoice_360 | get_account_balance |
                                             # get_product_metadata
    # Product / catalog
    product_id:     Optional[str]  = None
    product_number: Optional[int]  = None
    sku:            Optional[str]  = None
    search:         Optional[str]  = None
    category_id:    Optional[str]  = None
    page_size:      Optional[int]  = None
    page_number:    Optional[int]  = None
    sort_field:     Optional[str]  = None
    sort_order:     Optional[str]  = None
    in_stock_only:  Optional[bool] = None

    # Checkout
    account_id:     Optional[str]       = None
    contact_id:     Optional[str]       = None
    items:          Optional[List[StoreItem]] = None
    notes:          Optional[str]       = None
    created_by:     Optional[str]       = None
    coupon_code:    Optional[str]       = None

    # Post-checkout reads
    order_id:       Optional[str]  = None
    order_number:   Optional[str]  = None
    invoice_id:     Optional[str]  = None


class StoreChatInput(BaseModel):
    message:    str         = "store_operation"
    storeData:  Optional[StoreData] = None
    routerAction: Optional[bool]   = None
    sp:         Optional[str]      = None
    mode:       Optional[str]      = None


class StoreChatRequest(BaseModel):
    chatInput:  StoreChatInput
    sessionId:  Optional[str] = "store-session"


class StoreChatResponse(BaseModel):
    sessionId:      str
    output:         str         # JSON envelope string — parsed by store-home.html
    mode:           Optional[str] = None
    sp:             Optional[str] = None
    calledDatabase: bool = False
    success:        bool = True


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/store-health")
async def store_health():
    return {
        "status":  "healthy",
        "module":  "store",
        "version": "1.0.0",
        "graph_initialized": get_graph() is not None,
        "ai_agent": False,
        "note": "Direct SP routing only — no AI agent",
    }


@router.post("/store-chat", response_model=StoreChatResponse)
async def store_chat(req: StoreChatRequest):
    """
    CRM Commerce View primary endpoint.

    Accepts structured storeData and routes directly to the appropriate
    stored procedure.  Returns a JSON envelope in output that the store
    HTML frontend parses for rendering.
    """
    logger.info("=== New Store Chat Request ===")
    session_id = (req.sessionId or "store-session").strip()
    ci = req.chatInput
    context = (ci.storeData.context if ci.storeData else None) or ci.mode or "unknown"
    logger.info(f"Session: {session_id}  Context: {context}")

    # Build flat chat_input dict for graph state (matches AgentState convention)
    chat_input: Dict[str, Any] = ci.model_dump(exclude_none=False)
    chat_input["message"] = ci.message
    if ci.storeData:
        sd = ci.storeData.model_dump(exclude_none=False)
        # Serialise items list for the pre_router / graph
        if sd.get("items"):
            sd["items"] = [
                {"product_id": it["product_id"], "quantity": it["quantity"]}
                for it in (sd["items"] or [])
            ]
        chat_input["storeData"] = sd

    try:
        graph_app = get_graph()

        initial_state: AgentState = {
            "session_id":      session_id,
            "chat_input":      chat_input,
            "user_input":      ci.message,
            "router_action":   False,
            "ai_output":       None,
            "parsed_json":     None,
            "should_call_api": False,
            "db_rows":         None,
            "final_output":    None,
        }

        final_state  = graph_app.invoke(initial_state)
        raw_params   = final_state.get("parsed_json") or {}
        called_db    = final_state.get("should_call_api", False)
        final_output = final_state.get("final_output") or "{}"

        logger.info(f"Store request complete — context={context} DB={called_db}")

        return StoreChatResponse(
            sessionId=session_id,
            output=final_output,
            mode=raw_params.get("mode"),
            sp=raw_params.get("sp"),
            calledDatabase=called_db,
            success=True,
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as exc:
        logger.error(f"Store chat error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(exc)}")
