"""FastAPI router for the Opportunities domain.

Endpoint: POST /opportunity-chat
Version:  2.0.0

Drop-in replacement for the standalone opportunity_agent /opportunity-chat endpoint.

Key characteristics:
  - Accepts BOTH direct body-mode calls (HTML forms) and NL chat calls
  - _build_body() reconstructs the flat body dict the pre-router expects for CASE 1
  - format_response returns a dict; response includes search_results, product_lines,
    owners side-channel fields consumed by the HTML frontend
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .graph import get_graph
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Opportunities"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class OpportunityChatInput(BaseModel):
    """chatInput — NL message plus optional legacy fields."""
    message:      Optional[str]  = None   # optional — AI chat path only
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent
    city:       Optional[str] = None
    pageSize:   Optional[int] = None
    pageNumber: Optional[int] = None
    customerId: Optional[str] = None


class OpportunityChatRequest(BaseModel):
    """
    Full request body — two usage patterns:
    1. Direct form: { mode, opportunity_id, account_id, ... }
    2. Chat:        { chatInput: { message: '...' }, sessionId: '...' }
    """
    chatInput:  Optional[OpportunityChatInput] = None
    sessionId:  Optional[str]                  = "default-session"

    # SP params (direct form / programmatic calls)
    mode:            Optional[str]   = None
    opportunity_id:  Optional[str]   = None
    account_id:      Optional[str]   = None
    contact_id:      Optional[str]   = None
    name:            Optional[str]   = None
    amount:          Optional[float] = None
    stage:           Optional[str]   = None
    probability:     Optional[int]   = None
    close_date:      Optional[str]   = None
    description:     Optional[str]   = None
    lead_source:     Optional[str]   = None
    owner_id:        Optional[str]   = None
    status:          Optional[str]   = None
    product_id:      Optional[str]   = None
    quantity:        Optional[float] = None
    selling_price:   Optional[float] = None
    discount:        Optional[float] = None
    opp_product_id:  Optional[str]   = None
    payload:         Optional[dict]  = None
    created_by:      Optional[str]   = None
    updated_by:      Optional[str]   = None
    page_size:       Optional[int]   = None
    page_number:     Optional[int]   = None
    search:          Optional[str]   = None
    date_from:       Optional[str]   = None
    date_to:         Optional[str]   = None
    min_probability: Optional[int]   = None
    max_probability: Optional[int]   = None
    min_amount:      Optional[float] = None
    max_amount:      Optional[float] = None
    sort_by:         Optional[str]   = None


class OpportunityChatResponse(BaseModel):
    sessionId:      str
    output:         str
    rawParams:      Optional[dict]  = None
    calledDatabase: bool            = False
    mode:           Optional[str]   = None
    reportMode:     Optional[str]   = None
    success:        bool            = True
    # Side-channel arrays for HTML frontend
    searchResults:  Optional[List]  = None
    searchQuery:    Optional[str]   = None
    searchMode:     Optional[str]   = None
    productLines:   Optional[List]  = None
    owners:         Optional[List]  = None


# SP param fields extracted into body for CASE 1 pre-router detection
_SP_FIELDS = [
    "mode", "opportunity_id", "account_id", "contact_id", "name", "amount",
    "stage", "probability", "close_date", "description", "lead_source",
    "owner_id", "status", "product_id", "quantity", "selling_price",
    "discount", "opp_product_id", "payload", "created_by", "updated_by",
    "page_size", "page_number", "search", "date_from", "date_to",
    "min_probability", "max_probability", "min_amount", "max_amount", "sort_by",
]


def _build_body(req: OpportunityChatRequest) -> dict:
    """Build the flat body dict the pre-router reads for CASE 1 detection."""
    body: dict = {}
    for f in _SP_FIELDS:
        v = getattr(req, f, None)
        if v is not None:
            body[f] = v
    return body


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/opportunity-health")
async def opportunity_health():
    return {
        "status":  "healthy",
        "module":  "opportunities",
        "version": "2.0.0",
        "graph_initialized": get_graph() is not None,
    }


@router.post("/opportunity-chat", response_model=OpportunityChatResponse)
async def opportunity_chat(req: OpportunityChatRequest):
    """
    Main opportunities endpoint — drop-in replacement for the standalone
    opportunity_agent /opportunity-chat webhook.

    Accepts both:
      - Direct structured calls: { mode: 'create', account_id: '...', name: '...' }
      - Chat calls: { chatInput: { message: 'Show pipeline' }, sessionId: '...' }
    """
    logger.info("=== New Opportunity Chat Request ===")
    session_id = (req.sessionId or "default-session").strip()
    ci         = req.chatInput or OpportunityChatInput(message="")
    body       = _build_body(req)

    logger.info(f"Session: {session_id}  Message: {(ci.message or "")[:120]!r}")
    if body.get("mode"):
        logger.info(f"Direct body mode: {body['mode']}")

    chat_input: Dict[str, Any] = ci.model_dump(exclude_none=True)

    try:
        graph_app = get_graph()

        initial_state = {
            "session_id":      session_id,
            "body":            body,
            "chat_input":      chat_input,
            "user_input":      ci.message or "",
            "router_action":   False,
            "ai_output":       None,
            "parsed_json":     None,
            "should_call_api": False,
            "db_rows":         None,
            "format_result":   None,
            "final_output":    None,
        }

        final_state  = graph_app.invoke(initial_state)
        fmt_result   = final_state.get("format_result") or {}
        raw_params   = final_state.get("parsed_json")
        called_db    = final_state.get("should_call_api", False)
        final_output = final_state.get("final_output") or ""
        mode         = fmt_result.get("mode") or (raw_params.get("mode") if raw_params else "unknown")
        report_mode  = fmt_result.get("report_mode", "generic")

        logger.info(f"Opportunities complete — DB={called_db}, mode={mode}")

        return OpportunityChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            reportMode=report_mode,
            success=True,
            searchResults=fmt_result.get("search_results"),
            searchQuery=fmt_result.get("search_query"),
            searchMode=fmt_result.get("search_mode"),
            productLines=fmt_result.get("product_lines"),
            owners=fmt_result.get("owners"),
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Opportunity chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
