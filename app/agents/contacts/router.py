"""FastAPI router for the Contacts domain.

Endpoint: POST /contact-chat
Version:  3.0.0

This module replaces app/main.py from the standalone contact_agent zip.
It is mounted into the unified crm_agent application in app/main.py.

Pipeline (unchanged from standalone):
  1. Build chat_input dict from all ContactChatInput fields.
  2. Invoke LangGraph:
       pre_router_node → [router_action=True]  → db → format
                       → [router_action=False] → ai_agent → parse → [db →] format
  3. Return ContactChatResponse.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.graph_utils import AgentState
from .graph import get_graph
from .formatter import _parse_response
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Contacts"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class ContactChatInput(BaseModel):
    """All chatInput fields used by the Contact pre-router (v3.1) and HTML forms."""
    # Stage 3 (Phase 9): the operation the orchestrator resolved. Declared
    # explicitly because pydantic drops undeclared fields — the intent was
    # being sent, silently discarded here, and the module fell back to parsing
    # prose, which looked exactly like the cutover not working.
    structuredIntent: Optional[Dict[str, Any]] = None
    message:      Optional[str]  = None   # optional — AI chat path only
    mode:         Optional[str]  = None   # direct SP route
    routerAction: Optional[bool] = None   # True → bypass AI agent

    # ── Pagination / legacy ───────────────────────────────────────────────────
    pageSize:   Optional[int] = None
    pageNumber: Optional[int] = None
    city:       Optional[str] = None
    customerId: Optional[str] = None

    # ── Contact identity ──────────────────────────────────────────────────────
    contactId:  Optional[str] = None
    firstName:  Optional[str] = None
    lastName:   Optional[str] = None
    email:      Optional[str] = None
    phone:      Optional[str] = None
    role:       Optional[str] = None
    status:     Optional[str] = None

    # ── Relationships ─────────────────────────────────────────────────────────
    accountId:  Optional[str] = None
    ownerId:    Optional[str] = None

    # ── Audit ─────────────────────────────────────────────────────────────────
    createdBy:  Optional[str] = None
    updatedBy:  Optional[str] = None

    # ── Address fields (JSONB) ────────────────────────────────────────────────
    billingAddress:  Optional[Dict[str, Any]] = None
    shippingAddress: Optional[Dict[str, Any]] = None

    # ── Filtering ─────────────────────────────────────────────────────────────
    search:         Optional[str] = None
    includeDeleted: Optional[bool] = None
    deletedOnly:    Optional[bool] = None
    dateFrom:       Optional[str] = None
    dateTo:         Optional[str] = None

    # ── Verification ─────────────────────────────────────────────────────────
    token:             Optional[str] = None
    tokenExpiresHours: Optional[int] = None
    verificationIp:    Optional[str] = None

    # ── Merge ─────────────────────────────────────────────────────────────────
    operation: Optional[str] = None

    # ── Generic payload ───────────────────────────────────────────────────────
    payload: Optional[Dict[str, Any]] = None


class ContactChatRequest(BaseModel):
    chatInput: ContactChatInput
    sessionId: Optional[str] = "default-session"


class ContactChatResponse(BaseModel):
    sessionId:      str
    output:         str
    rawParams:      Optional[dict] = None
    calledDatabase: bool = False
    mode:           Optional[str] = None
    reportMode:     Optional[str] = None
    contact:        Optional[dict] = None
    contacts:       Optional[list] = None
    duplicates:     Optional[dict] = None
    success:        bool = True


# Mode → reportMode mapping consumed by the HTML frontend
_REPORT_MODE_MAP = {
    "web_search":         "web_search",
    "list":              "list",
    "get_details":       "detail",
    "create":            "detail",
    "update":            "detail",
    "send_verification": "verification_sent",
    "verify_email":      "verification_confirmed",
    "duplicates":        "duplicates",
    "merge":             "merge_confirmation",
    "archive":           "archive_confirmation",
    "restore":           "restore_confirmation",
    "activities":        "activities",
    "summary":           "summary",
    # UI-only markers — frontend opens the inline form
    "show_contact_form":         "show_contact_form",
    "show_contact_update_form":  "show_contact_update_form",
}


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/contact-health")
async def contact_health():
    return {
        "status":  "healthy",
        "module":  "contacts",
        "version": "3.0.0",
        "graph_initialized": get_graph() is not None,
    }


@router.post("/contact-chat", response_model=ContactChatResponse)
async def contact_chat(req: ContactChatRequest):
    """
    Main contacts endpoint — drop-in replacement for the standalone
    contact_agent /contact-chat webhook.
    """
    logger.info("=== New Contact Chat Request ===")
    session_id = (req.sessionId or "default-session").strip()
    ci = req.chatInput
    logger.info(f"Session: {session_id}  Message: {(ci.message or "")[:120]}")

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

        db_rows   = final_state.get("db_rows") or []
        parsed_db = _parse_response(db_rows) if called_db and db_rows else {}
        contacts  = parsed_db.get("contacts")
        contact   = parsed_db.get("contact")
        duplicates = parsed_db.get("duplicates") if mode == "duplicates" else None

        logger.info(f"Contacts request complete — DB={called_db}, mode={mode}")

        return ContactChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            reportMode=report_mode,
            contact=contact,
            contacts=contacts,
            duplicates=duplicates,
            success=True,
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Contact chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
