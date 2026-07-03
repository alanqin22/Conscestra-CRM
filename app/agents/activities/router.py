"""FastAPI router for the Activities domain.

Endpoint: POST /activity-chat
Version:  2.0.0

Drop-in replacement for the standalone activity_agent /activity-chat endpoint.

Key characteristics:
  - chatInput.message is prefix-based ("list activities:", "get owners:", etc.)
  - format_response returns a dict; response includes owners[] side-channel
  - initial_state carries body + current_message extra keys
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .graph import get_graph
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Activities"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class ActivityChatInput(BaseModel):
    message:      Optional[str]  = None   # optional — AI chat path only
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent

    pageNumber:       Optional[int]  = None
    pageSize:         Optional[int]  = None
    search:           Optional[str]  = None
    includeCompleted: Optional[bool] = None

    activityId:  Optional[str] = None
    relatedType: Optional[str] = None
    relatedId:   Optional[str] = None

    type:        Optional[str] = None
    subject:     Optional[str] = None
    description: Optional[str] = None
    dueDate:     Optional[str] = None
    direction:   Optional[str] = None
    channel:     Optional[str] = None
    ownerId:     Optional[str] = None
    completedAt: Optional[str] = None

    createdBy: Optional[str] = None
    updatedBy: Optional[str] = None
    dateFrom:  Optional[str] = None
    dateTo:    Optional[str] = None


class ActivityChatRequest(BaseModel):
    chatInput: Optional[ActivityChatInput] = None
    sessionId: Optional[str]              = "default-session"
    message:   Optional[str]              = None   # legacy top-level message


class ActivityChatResponse(BaseModel):
    sessionId:      str
    output:         str
    rawParams:      Optional[dict] = None
    calledDatabase: bool           = False
    mode:           Optional[str]  = None
    reportMode:     Optional[str]  = None
    success:        bool           = True
    owners:         Optional[List] = None   # populated for get_owners mode
    activity:       Optional[dict] = None   # populated for get/update/create/complete/reopen modes
    timeline:       Optional[List] = None   # populated for timeline mode — structured events with full details
    entity_name:    Optional[str]  = None   # populated for timeline mode — account/contact name for title


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/activity-health")
async def activity_health():
    return {
        "status":  "healthy",
        "module":  "activities",
        "version": "2.0.0",
        "graph_initialized": get_graph() is not None,
    }


@router.post("/activity-chat", response_model=ActivityChatResponse)
async def activity_chat(req: ActivityChatRequest):
    """
    Main activities endpoint — drop-in replacement for the standalone
    activity_agent /activity-chat webhook.
    """
    logger.info("=== New Activity Chat Request ===")
    session_id = (req.sessionId or "default-session").strip()
    ci         = req.chatInput or ActivityChatInput(message=req.message or "")
    logger.info(f"Session: {session_id}  Message: {(ci.message or "")[:120]!r}")

    # body carries legacy top-level message (for pre-router CASE check)
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

        logger.info(f"Activities complete — DB={called_db}, mode={mode}")

        return ActivityChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            reportMode=report_mode,
            success=fmt_result.get("success", True),
            owners=fmt_result.get("owners"),
            activity=fmt_result.get("activity"),
            timeline=fmt_result.get("timeline"),
            entity_name=fmt_result.get("entity_name"),
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Activity chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
