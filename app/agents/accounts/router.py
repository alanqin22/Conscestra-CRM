"""FastAPI router for the Accounts domain.

Endpoint: POST /account-chat
Version:  2.0.0

This module replaces app/main.py from the standalone account_agent zip.
It is mounted into the unified crm_agent application in app/main.py.

Pipeline (unchanged from standalone):
  1. Build chat_input dict from all AccountChatInput fields.
  2. Invoke LangGraph:
       pre_router_node → [router_action=True]  → db → format
                       → [router_action=False] → ai_agent → parse → [db →] format
  3. Return AccountChatResponse.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.graph_utils import AgentState
from app.core.config import settings
from .graph import get_graph
from .formatter import _parse_response
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Accounts"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class AccountChatInput(BaseModel):
    """All chatInput fields used by the Account pre-router and HTML forms."""
    # Stage 3 (Phase 9): the operation the orchestrator resolved. Declared
    # explicitly because pydantic drops undeclared fields — the intent was
    # being sent, silently discarded here, and the module fell back to parsing
    # prose, which looked exactly like the cutover not working.
    structuredIntent: Optional[Dict[str, Any]] = None
    message:      Optional[str]  = None   # optional — AI chat path only
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent

    # ── Legacy preprocessor ──────────────────────────────────────────────────
    city:       Optional[str] = None
    pageSize:   Optional[int] = None
    pageNumber: Optional[int] = None
    customerId: Optional[str] = None

    # ── Account identity ──────────────────────────────────────────────────────
    account_id:   Optional[str] = None
    accountId:    Optional[str] = None
    account_name: Optional[str] = None
    accountName:  Optional[str] = None

    # ── Core fields ───────────────────────────────────────────────────────────
    type:     Optional[str] = None
    industry: Optional[str] = None
    phone:    Optional[str] = None
    email:    Optional[str] = None
    website:  Optional[str] = None
    employeeBand: Optional[str]   = None
    revenueBand:  Optional[str]   = None
    revenueMin:   Optional[float] = None
    focus:        Optional[str]   = None
    sort:         Optional[str]   = None
    status:   Optional[str] = None

    # ── Address fields (JSONB) ────────────────────────────────────────────────
    billing_address:  Optional[Dict[str, Any]] = None
    shipping_address: Optional[Dict[str, Any]] = None
    billingAddress:   Optional[Dict[str, Any]] = None
    shippingAddress:  Optional[Dict[str, Any]] = None

    # ── Ownership / audit ─────────────────────────────────────────────────────
    owner_id:   Optional[str] = None
    ownerId:    Optional[str] = None
    created_by: Optional[str] = None
    createdBy:  Optional[str] = None
    updated_by: Optional[str] = None
    updatedBy:  Optional[str] = None

    # ── Merge ─────────────────────────────────────────────────────────────────
    operation: Optional[str] = None

    # ── Filtering / pagination ────────────────────────────────────────────────
    search:         Optional[str] = None
    includeDeleted: Optional[bool] = None
    deletedOnly:    Optional[bool] = None
    dateFrom:       Optional[str] = None
    dateTo:         Optional[str] = None
    date_from:      Optional[str] = None
    date_to:        Optional[str] = None

    # ── Direct-SQL list mode params ───────────────────────────────────────────
    minOrders:      Optional[int] = None   # list_min_orders: HAVING COUNT(orders) > N

    # ── Generic payload ───────────────────────────────────────────────────────
    payload: Optional[Dict[str, Any]] = None


class AccountChatRequest(BaseModel):
    chatInput: AccountChatInput
    sessionId: Optional[str] = "default-session"


class AccountChatResponse(BaseModel):
    sessionId:      str
    output:         str
    rawParams:      Optional[dict] = None
    calledDatabase: bool = False
    mode:           Optional[str] = None
    reportMode:     Optional[str] = None
    account:        Optional[dict] = None
    accounts:       Optional[list] = None
    owners:         Optional[list] = None
    success:        bool = True


# Mode → reportMode mapping consumed by the HTML frontend
_REPORT_MODE_MAP = {
    "web_search":         "web_search",
    "list":       "list",
    "get":        "get",
    "create":     "create",
    "update":     "update",
    "timeline":   "timeline",
    "financials": "financials",
    "duplicates": "duplicates",
    "merge":      "merge",
    "archive":    "archive",
    "restore":    "restore",
    "summary":    "summary",
    "list_owner": "list_owner",
    # UI-only markers — frontend opens the inline form
    "show_account_form":         "show_account_form",
    "show_account_update_form":  "show_account_update_form",
}


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/account-health")
async def account_health():
    return {
        "status":  "healthy",
        "module":  "accounts",
        "version": "2.0.0",
        "graph_initialized": get_graph() is not None,
    }


@router.post("/account-chat", response_model=AccountChatResponse)
async def account_chat(req: AccountChatRequest):
    """
    Main accounts endpoint — drop-in replacement for the standalone
    account_agent /account-chat webhook.
    """
    logger.info("=== New Account Chat Request ===")
    session_id = (req.sessionId or "default-session").strip()
    ci = req.chatInput
    logger.info(f"Session: {session_id}  Message: {(ci.message or "")[:100]}")

    chat_input: Dict[str, Any] = ci.model_dump(exclude_none=True)
    chat_input["message"] = ci.message or ""

    try:
        graph_app = get_graph()

        initial_state: AgentState = {
            "session_id":      session_id,
            "chat_input":      chat_input,
            "user_input":      ci.message or "",
            "router_action":   False,
            "ai_output":       None,
            "parsed_json":     None,
            "should_call_api": False,
            "db_rows":         None,
            "final_output":    None,
        }

        final_state  = graph_app.invoke(initial_state)
        raw_params   = final_state.get("parsed_json")
        called_db    = final_state.get("should_call_api", False)
        mode         = (raw_params.get("mode") or "unknown") if raw_params else "unknown"
        report_mode  = _REPORT_MODE_MAP.get(mode, "generic")
        final_output = final_state.get("final_output") or ""

        db_rows = final_state.get("db_rows") or []
        parsed_db = _parse_response(db_rows) if called_db and db_rows else {}
        accounts  = parsed_db.get("accounts")
        account   = parsed_db.get("account")
        owners    = parsed_db.get("owners")

        logger.info(f"Accounts request complete — DB={called_db}, mode={mode}")

        return AccountChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            reportMode=report_mode,
            account=account,
            accounts=accounts,
            owners=owners,
            success=True,
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Account chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
