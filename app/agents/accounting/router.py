"""FastAPI router for the Accounting domain.

Endpoint: POST /accounting-chat
Version:  7.0.0

Drop-in replacement for the standalone accounting_agent /accounting-chat endpoint.
Uses the standard request/response pattern (string output, no side-channel arrays).
All 14 SP modes: generate_invoice, record_payment, void_invoice, list_invoices,
list_invoices_for_account, get_invoice_360, list_payments, get_payment_360,
account_balance, account_balance_lookup, accounting_summary, account_search,
get_invoiceable_orders, list_employee.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.graph_utils import AgentState
from .graph import get_graph
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Accounting"])

_REPORT_MODE_MAP = {
    "web_search":         "web_search",
    "generate_invoice":          "invoice_generated",
    "record_payment":            "payment_recorded",
    "void_invoice":              "invoice_voided",
    "list_invoices":             "invoice_list",
    "list_invoices_for_account": "invoice_list_for_account",
    "get_invoice_360":           "invoice_detail",
    "list_payments":             "payment_list",
    "get_payment_360":           "payment_detail",
    "account_balance":           "account_balance",
    "account_balance_lookup":    "account_balance_lookup",
    "accounting_summary":        "accounting_summary",
    "account_search":            "account_search",
    "get_invoiceable_orders":    "invoiceable_orders",
    "list_employee":             "list_employee",
    # UI-only markers — frontend opens the inline form
    "show_invoice_form":         "show_invoice_form",
    "show_payment_form":         "show_payment_form",
    "show_void_invoice_form":    "show_void_invoice_form",
}


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class AccountingChatInput(BaseModel):
    message:      Optional[str]  = None   # optional — AI chat path only
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent

    # Legacy preprocessor fields
    city:       Optional[str] = None
    pageSize:   Optional[int] = None
    pageNumber: Optional[int] = None
    customerId: Optional[str] = None

    # Home-page search bar
    searchText: Optional[str] = None

    # UUID identity fields
    accountId:  Optional[str] = None
    invoiceId:  Optional[str] = None
    paymentId:  Optional[str] = None
    contactId:  Optional[str] = None

    # Generate Invoice form
    orderIds:       Optional[List[str]] = None
    invoiceNumber:  Optional[str]   = None
    invoiceType:    Optional[str]   = None
    dueDate:        Optional[str]   = None
    currency:       Optional[str]   = None
    taxRate:        Optional[float] = None
    discountAmount: Optional[float] = None
    shippingAmount: Optional[float] = None
    adjustment:     Optional[float] = None
    ownerId:        Optional[str]   = None
    employeeUuid:   Optional[str]   = None
    notes:          Optional[str]   = None

    # Record Payment form
    amount:               Optional[float] = None
    paymentMethod:        Optional[str]   = None
    paymentSource:        Optional[str]   = None
    transactionReference: Optional[str]   = None

    # AccountingFilterBar
    listingType: Optional[str] = None
    startDate:   Optional[str] = None
    endDate:     Optional[str] = None

    # List-pagination context replay (frontend echoes rawParams back via the
    # routerAction short-circuit so Next/Prev Page keeps the original filters)
    search:       Optional[str]  = None
    statusFilter: Optional[str]  = None
    overdue:      Optional[bool] = None


class AccountingChatRequest(BaseModel):
    chatInput: AccountingChatInput
    sessionId: Optional[str] = "default-session"


class AccountingChatResponse(BaseModel):
    sessionId:      str
    output:         str
    rawParams:      Optional[dict] = None
    calledDatabase: bool           = False
    mode:           Optional[str]  = None
    reportMode:     Optional[str]  = None
    success:        bool           = True
    page:           int            = 1
    totalPages:     int            = 1
    totalRecords:   int            = 0


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/accounting-health")
async def accounting_health():
    return {
        "status":  "healthy",
        "module":  "accounting",
        "version": "7.0.0",
        "graph_initialized": get_graph() is not None,
    }


@router.post("/accounting-chat", response_model=AccountingChatResponse)
async def accounting_chat(req: AccountingChatRequest):
    """
    Main accounting endpoint — drop-in replacement for the standalone
    accounting_agent /accounting-chat webhook.
    """
    logger.info("=== New Accounting Chat Request (v7) ===")
    session_id = (req.sessionId or "default-session").strip()
    ci = req.chatInput
    logger.info(f"Session: {session_id}  Message: {(ci.message or "")[:100]}")

    # Build full chat_input dict (all fields, None-safe) — pre-router reads by key
    chat_input: Dict[str, Any] = {
        "message":    ci.message or "",
        "city":       ci.city,
        "pageSize":   ci.pageSize,
        "pageNumber": ci.pageNumber,
        "customerId": ci.customerId,
        "searchText": ci.searchText,
        "accountId":  ci.accountId,
        "invoiceId":  ci.invoiceId,
        "paymentId":  ci.paymentId,
        "contactId":  ci.contactId,
        "orderIds":       ci.orderIds,
        "invoiceNumber":  ci.invoiceNumber,
        "invoiceType":    ci.invoiceType,
        "dueDate":        ci.dueDate,
        "currency":       ci.currency,
        "taxRate":        ci.taxRate,
        "discountAmount": ci.discountAmount,
        "shippingAmount": ci.shippingAmount,
        "adjustment":     ci.adjustment,
        "ownerId":        ci.ownerId,
        "employeeUuid":   ci.employeeUuid,
        "notes":          ci.notes,
        "amount":               ci.amount,
        "paymentMethod":        ci.paymentMethod,
        "paymentSource":        ci.paymentSource,
        "transactionReference": ci.transactionReference,
        "listingType": ci.listingType,
        "startDate":   ci.startDate,
        "endDate":     ci.endDate,
        "search":       ci.search,
        "statusFilter": ci.statusFilter,
        "overdue":      ci.overdue,
        # ── Routing control (must be forwarded so pre_router short-circuit fires) ──
        "mode":         ci.mode,
        "routerAction": ci.routerAction,
    }

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

        # Extract pagination metadata from SP result for structured response fields
        page = 1
        total_pages = 1
        total_records = 0
        try:
            import json as _json
            db_rows_state = final_state.get("db_rows") or []
            if db_rows_state:
                first_row = db_rows_state[0]
                if isinstance(first_row, dict):
                    sp_data = (first_row.get("sp_accounting")
                               or first_row.get("result")
                               or first_row)
                    if isinstance(sp_data, str):
                        try: sp_data = _json.loads(sp_data)
                        except Exception: sp_data = {}
                    if isinstance(sp_data, dict):
                        meta = sp_data.get("metadata") or {}
                        if meta.get("page")          is not None: page          = int(meta["page"])
                        if meta.get("total_pages")   is not None: total_pages   = int(meta["total_pages"])
                        if meta.get("total_records") is not None: total_records = int(meta["total_records"])
        except Exception:
            pass

        logger.info(f"Accounting complete — DB={called_db}, mode={mode}, page={page}/{total_pages}, records={total_records}")

        return AccountingChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            reportMode=report_mode,
            success=True,
            page=page,
            totalPages=total_pages,
            totalRecords=total_records,
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Accounting chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
