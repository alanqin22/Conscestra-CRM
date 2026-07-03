"""FastAPI router for the Notifications domain.

Endpoints: POST /notifications-chat  (primary)
           POST /notification-chat   (alias)
Version:  1.0.0

Drop-in replacement for the standalone notification_agent endpoints.
Response is intentionally lean — the frontend drives interactivity through
embedded marker tokens in the output string rather than structured arrays.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .graph import get_graph
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Notifications"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class NotificationChatInput(BaseModel):
    """chatInput envelope — prefixed message plus all SP params."""
    message:      Optional[str]  = None   # optional — AI chat path only
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent

    notificationId: Optional[str] = None
    employeeId:     Optional[str] = None
    module:         Optional[str] = None
    search:         Optional[str] = None
    limit:          Optional[int] = None
    offset:         Optional[int] = None


class NotificationChatRequest(BaseModel):
    chatInput: Optional[NotificationChatInput] = None
    sessionId: Optional[str]                   = "default-session"
    message:   Optional[str]                   = None  # legacy top-level


class NotificationChatResponse(BaseModel):
    sessionId:      str
    output:         str
    rawParams:      Optional[dict] = None
    calledDatabase: bool           = False
    mode:           Optional[str]  = None
    success:        bool           = True


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/notification-health")
async def notification_health():
    return {
        "status":  "healthy",
        "module":  "notifications",
        "version": "1.0.0",
        "graph_initialized": get_graph() is not None,
    }


async def _handle_notifications(req: NotificationChatRequest) -> NotificationChatResponse:
    """Shared handler for /notifications-chat and /notification-chat."""
    session_id = (req.sessionId or "default-session").strip()
    ci         = req.chatInput or NotificationChatInput(message=req.message or "")
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

        logger.info(f"Notifications complete — DB={called_db}, mode={mode}")

        return NotificationChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            success=fmt_result.get("success", True),
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Notifications chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/notifications-chat", response_model=NotificationChatResponse)
async def notifications_chat(req: NotificationChatRequest):
    """Primary notifications endpoint."""
    logger.info("=== New Notifications Chat Request ===")
    return await _handle_notifications(req)


@router.post("/notification-chat", response_model=NotificationChatResponse)
async def notification_chat_alias(req: NotificationChatRequest):
    """Alias — some HTML pages may POST to /notification-chat (singular)."""
    logger.info("=== New Notifications Chat Request (alias /notification-chat) ===")
    return await _handle_notifications(req)
