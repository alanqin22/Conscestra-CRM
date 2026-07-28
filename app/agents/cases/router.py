"""FastAPI router for the Cases domain — C1 Step 5.

Endpoint: POST /case-chat
Health:   GET  /cases-health
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import cases as case_layer

from . import pre_router
from .graph import get_graph

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Cases"])


class CaseChatInput(BaseModel):
    message: Optional[str] = None
    mode: Optional[str] = None           # list|get|history|queue|unowned|owners
                                         # |transition|assign|priority|comment
    sessionId: Optional[str] = None
    agent: Optional[str] = None          # who is asking (for history attribution)

    caseId: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    owner_email: Optional[str] = None
    limit: Optional[int] = None

    # write-mode fields (Case Management UI)
    toStatus: Optional[str] = None
    ownerEmail: Optional[str] = None      # never a name, never "agent"
    body: Optional[str] = None
    internal: Optional[bool] = None


class CaseChatRequest(BaseModel):
    chatInput: CaseChatInput


@router.post("/case-chat")
def case_chat(req: CaseChatRequest) -> Dict[str, Any]:
    body = req.chatInput.model_dump(exclude_none=True)
    session_id = body.get("sessionId") or "cases"
    if not case_layer.ENABLED:
        return {"output": "Case management is disabled (CASES_ENABLED=0).",
                "sessionId": session_id}
    # FAIL CLOSED on an unrecognised mode. Without this a body carrying
    # mode="write" would silently drop to the natural-language path, so the
    # caller would believe an explicit operation ran when none did.
    if body.get("mode") is not None and not pre_router.is_known_mode(body["mode"]):
        return {"output": f"Unknown mode {body['mode']!r}. Direct modes are: "
                          + ", ".join(pre_router.DIRECT_MODES),
                "sessionId": session_id,
                "data": {"ok": False, "refused": True,
                         "error": f"unknown mode {body['mode']!r}"}}

    state = {"session_id": session_id,
             "chat_input": body,
             "user_input": body.get("message") or "",
             "router_action": False,
             "ai_output": None,
             "parsed_json": None,
             "should_call_api": False,
             "db_rows": None,
             "final_output": None,
             "fallback_text": None}
    try:
        result = get_graph().invoke(state)
    except Exception as exc:
        logger.exception("[cases] graph invocation failed")
        return {"output": f"Case agent error: {str(exc)[:200]}",
                "sessionId": session_id}
    return {"output": result.get("final_output") or "",
            "sessionId": session_id,
            "mode": (result.get("parsed_json") or {}).get("action"),
            "data": result.get("db_rows")}


@router.get("/cases/analytics")
def cases_analytics(days: int = 30) -> Dict[str, Any]:
    """Case-LIFECYCLE metrics. Deliberately separate from /agent-ops, which
    measures the CONVERSATION lifecycle and is left unchanged."""
    from app.core import case_analytics
    return case_analytics.metrics(days)


@router.get("/cases/knowledge-signals")
def cases_knowledge_signals(days: int = 90) -> Dict[str, Any]:
    """Evidence the KB may be missing something. Signals, never conclusions —
    nothing here writes, proposes or publishes."""
    from app.core import case_analytics
    return case_analytics.knowledge_signals(days)


@router.get("/cases/analytics/semantics")
def cases_analytics_semantics() -> Dict[str, Any]:
    """What each number means — published beside the numbers because the
    failure this guards against is reading a conversation metric as a work
    metric."""
    from app.core import case_analytics
    return case_analytics.semantics()


# ── C2.1 routing: recommendation and policy. NEVER assignment. ─────────────

@router.get("/routing/rules")
def routing_rules(include_inactive: bool = True) -> Dict[str, Any]:
    from app.core import routing
    return {"ok": True, "rules": routing.rules(include_inactive)}


@router.post("/routing/rules")
def routing_save_rule(body: Dict[str, Any]) -> Dict[str, Any]:
    """Edit the policy. The human owns the rule; nothing infers it."""
    from app.core import routing
    return routing.save_rule(body or {}, actor=str((body or {}).get("actor")
                                                   or "admin"))


@router.delete("/routing/rules/{rule_id}")
def routing_delete_rule(rule_id: str) -> Dict[str, Any]:
    from app.core import routing
    return routing.delete_rule(rule_id)


@router.get("/routing/recommend/{case_id}")
def routing_recommend(case_id: str) -> Dict[str, Any]:
    """Who SHOULD take this, and why. Assignment stays an explicit act."""
    from app.core import routing
    return routing.recommend(case_id)


@router.get("/routing/preview")
def routing_preview(limit: int = 25) -> Dict[str, Any]:
    """What the policy WOULD do across the live queue — before anything moves."""
    from app.core import routing
    return routing.preview(limit)


@router.get("/routing/directory")
def routing_directory() -> Dict[str, Any]:
    """Who may receive work, plus the candidates nobody has decided about."""
    from app.core import assignable
    return {"ok": True, "directory": assignable.directory(include_inactive=True),
            "inventory": assignable.inventory(),
            "environment": assignable.environment()}


@router.post("/routing/directory")
def routing_grant(body: Dict[str, Any]) -> Dict[str, Any]:
    """Grant assignability — an ADMIN ACT, never a side effect of routing."""
    from app.core import assignable
    b = body or {}
    return assignable.grant(str(b.get("email") or ""),
                            owner_id=(b.get("owner_id") or None),
                            display_name=str(b.get("display_name") or ""),
                            source=str(b.get("source") or "manual"),
                            source_ref=str(b.get("source_ref") or ""),
                            added_by=str(b.get("actor") or "admin"))


@router.post("/routing/directory/attributes")
def routing_set_attributes(body: dict) -> Dict[str, Any]:
    """Record what a person can work in. CURATED by a human — nothing infers a
    language from a name, a domain or a job title."""
    from app.core import assignable
    b = body or {}
    return assignable.set_attributes(
        str(b.get("email") or ""),
        languages=b.get("languages"), skills=b.get("skills"),
        by=str(b.get("actor") or "admin"))


@router.post("/routing/directory/provision")
def routing_provision(body: Dict[str, Any]) -> Dict[str, Any]:
    """Mint the CRM owner identity a granted person needs to hold work.
    Explicit and separate — routing never creates a worker."""
    from app.core import assignable
    b = body or {}
    return assignable.provision_owner(str(b.get("email") or ""),
                                      display_name=str(b.get("display_name") or ""),
                                      by=str(b.get("actor") or "admin"))


@router.delete("/routing/directory/{email}")
def routing_revoke(email: str) -> Dict[str, Any]:
    from app.core import assignable
    return assignable.revoke(email, by="admin")


@router.get("/cases-health")
def cases_health() -> Dict[str, Any]:
    try:
        get_graph()
        ready = True
    except Exception as exc:
        logger.error(f"[cases] graph not ready: {exc}")
        ready = False
    return {"graph_ready": ready, **case_layer.status_snapshot()}
