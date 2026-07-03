"""FastAPI router for the Leads domain.

Endpoints: POST /lead-chat  and  POST /leads-chat (alias)
Version:   2.0.0

Drop-in replacement for the standalone lead_agent endpoints.
Richest side-channel of all agents: leads, lead, pipeline, employees,
and all convert-mode entity objects (account, contact, opportunity, address)
plus their convenience UUIDs.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .graph import get_graph
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Leads"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class LeadChatInput(BaseModel):
    """chatInput envelope — message prefix + all SP params."""
    message:      Optional[str]  = None   # optional — AI chat path only
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent

    # Pagination / search / filters
    pageNumber:     Optional[int]  = None
    pageSize:       Optional[int]  = None
    search:         Optional[str]  = None
    status:         Optional[str]  = None
    rating:         Optional[str]  = None
    source:         Optional[str]  = None

    # Identity
    leadId:         Optional[str]  = None
    email:          Optional[str]  = None
    ownerId:        Optional[str]  = None
    campaignId:     Optional[str]  = None

    # Personal info
    firstName:      Optional[str]  = None
    lastName:       Optional[str]  = None
    company:        Optional[str]  = None
    phone:          Optional[str]  = None
    role:           Optional[str]  = None

    # Address
    addressLine1:   Optional[str]  = None
    addressLine2:   Optional[str]  = None
    city:           Optional[str]  = None
    province:       Optional[str]  = None
    postalCode:     Optional[str]  = None
    country:        Optional[str]  = None

    # Firmographics (external enrichment)
    industry:       Optional[str]  = None
    website:        Optional[str]  = None
    employeeBand:   Optional[str]  = None
    revenueBand:    Optional[str]  = None
    revenueMin:     Optional[float] = None

    # Lead scoring / qualify
    score:          Optional[int]  = None
    reason:         Optional[str]  = None

    # Merge
    operation:      Optional[str]  = None
    groupId:        Optional[str]  = None

    # Audit
    createdBy:      Optional[str]  = None
    updatedBy:      Optional[str]  = None

    # Flags
    includeDeleted: Optional[bool] = None
    deletedOnly:    Optional[bool] = None


class LeadChatRequest(BaseModel):
    chatInput: Optional[LeadChatInput] = None
    sessionId: Optional[str]           = "default-session"
    message:   Optional[str]           = None  # legacy top-level


class LeadChatResponse(BaseModel):
    sessionId:      str
    output:         str
    rawParams:      Optional[dict] = None
    calledDatabase: bool           = False
    mode:           Optional[str]  = None
    reportMode:     Optional[str]  = None
    success:        bool           = True

    # Side-channel data consumed directly by the HTML frontend
    leads:          Optional[List] = None   # full lead array for table rendering
    lead:           Optional[dict] = None   # single lead (detail / mutation)
    pipeline:       Optional[dict] = None   # pipeline analytics
    employees:      Optional[List] = None   # employee list (list_employee mode)

    # Convert-mode entity objects
    account:        Optional[dict] = None
    contact:        Optional[dict] = None
    opportunity:    Optional[dict] = None
    address:        Optional[dict] = None
    accountId:      Optional[str]  = None
    contactId:      Optional[str]  = None
    opportunityId:  Optional[str]  = None

    # Archive / restore
    lead_id:        Optional[str]  = None
    metadata:       Optional[dict] = None


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/lead-health")
async def lead_health():
    return {
        "status":  "healthy",
        "module":  "leads",
        "version": "2.0.0",
        "graph_initialized": get_graph() is not None,
    }


async def _handle_lead_chat(req: LeadChatRequest) -> LeadChatResponse:
    """Shared handler for /lead-chat and /leads-chat."""
    session_id = (req.sessionId or "default-session").strip()
    ci         = req.chatInput or LeadChatInput(message=req.message or "")
    logger.info(f"Session: {session_id}  Message: {(ci.message or "")[:120]!r}")

    body: dict = {}
    if req.message:
        body["message"] = req.message

    chat_input: Dict[str, Any] = ci.model_dump(exclude_none=True)

    try:
        graph_app = get_graph()

        initial_state = {
            "session_id":      session_id,
            "body":            body,
            "chat_input":      chat_input,
            "user_input":      ci.message or "",
            "current_message": ci.message,
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

        logger.info(f"Leads complete — DB={called_db}, mode={mode}")

        return LeadChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            reportMode=report_mode,
            success=fmt_result.get("success", True),
            leads=fmt_result.get("leads"),
            lead=fmt_result.get("lead"),
            pipeline=fmt_result.get("pipeline"),
            employees=fmt_result.get("employees"),
            account=fmt_result.get("account"),
            contact=fmt_result.get("contact"),
            opportunity=fmt_result.get("opportunity"),
            address=fmt_result.get("address"),
            accountId=fmt_result.get("accountId"),
            contactId=fmt_result.get("contactId"),
            opportunityId=fmt_result.get("opportunityId"),
            lead_id=fmt_result.get("lead_id"),
            metadata=fmt_result.get("metadata"),
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Lead chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/lead-chat",  response_model=LeadChatResponse)
async def lead_chat(req: LeadChatRequest):
    """Primary leads endpoint."""
    logger.info("=== New Lead Chat Request ===")
    return await _handle_lead_chat(req)


@router.post("/leads-chat", response_model=LeadChatResponse)
async def leads_chat(req: LeadChatRequest):
    """Alias — HTML frontend may POST to /leads-chat (plural)."""
    logger.info("=== New Lead Chat Request (alias /leads-chat) ===")
    return await _handle_lead_chat(req)
