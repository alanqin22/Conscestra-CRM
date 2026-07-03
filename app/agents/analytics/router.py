"""FastAPI router for the Analytics domain.

Endpoint: POST /analytics-chat
Version:  1.0.0

Drop-in replacement for the standalone analytics_agent /analytics-chat endpoint.

Unique characteristics:
  - Single message prefix "analytics report:" for ALL report types
  - reportType field selects which of the 11 dashboard sections to return
  - Rich side-channel response: dashboardData, summaryMetrics, params, meta
  - Double-slash path normalisation (also applied globally via app middleware)
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .graph import get_graph
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Analytics"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class AnalyticsChatInput(BaseModel):
    """chatInput — 'analytics report:' prefix + reportType + date/filter params."""
    message:      Optional[str]  = None   # optional — AI chat path only
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent
    reportType: Optional[str] = None   # full_dashboard | forecast_summary | pipeline_summary | ...
    startDate:  Optional[str] = None
    endDate:    Optional[str] = None
    ownerId:    Optional[str] = None
    accountId:  Optional[str] = None
    productId:  Optional[str] = None


class AnalyticsChatRequest(BaseModel):
    chatInput: Optional[AnalyticsChatInput] = None
    sessionId: Optional[str]               = "default-session"
    message:   Optional[str]               = None  # legacy top-level


class AnalyticsChatResponse(BaseModel):
    sessionId:       str
    output:          str
    rawParams:       Optional[dict] = None
    calledDatabase:  bool           = False
    mode:            Optional[str]  = None
    success:         bool           = True
    # Analytics-specific side-channel data for the HTML dashboard
    dashboardData:   Optional[dict] = None   # 15 keyed sections for chart/table rendering
    summaryMetrics:  Optional[dict] = None   # KPI card values
    params:          Optional[dict] = None   # echoed SP params for filter display
    meta:            Optional[dict] = None   # generation timestamp + record counts


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/analytics-health")
async def analytics_health():
    return {
        "status":  "healthy",
        "module":  "analytics",
        "version": "1.0.0",
        "graph_initialized": get_graph() is not None,
    }


@router.post("/analytics-chat", response_model=AnalyticsChatResponse)
async def analytics_chat(req: AnalyticsChatRequest):
    """
    Main analytics endpoint — drop-in replacement for the standalone
    analytics_agent /analytics-chat webhook.
    """
    logger.info("=== New Analytics Chat Request ===")
    session_id = (req.sessionId or "default-session").strip()
    ci         = req.chatInput or AnalyticsChatInput(message=req.message or "")
    logger.info(f"Session: {session_id}  Message: {(ci.message or "")[:80]!r}  reportType: {ci.reportType}")

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
        mode         = fmt_result.get("mode") or "dashboard"

        logger.info(f"Analytics complete — DB={called_db}, mode={mode}")

        return AnalyticsChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            success=fmt_result.get("success", True),
            dashboardData=fmt_result.get("dashboardData"),
            summaryMetrics=fmt_result.get("summaryMetrics"),
            params=fmt_result.get("params"),
            meta=fmt_result.get("meta"),
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Analytics chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
