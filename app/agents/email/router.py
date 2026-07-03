"""FastAPI router for the Email domain.

Endpoint: POST /email-chat
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from .graph import get_graph
from app.core.write_guard import WritePermissionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Email"])


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class EmailChatInput(BaseModel):
    message:      Optional[str]  = None
    mode:         Optional[str]  = None
    routerAction: Optional[bool] = None

    # Email operation params
    limit:        Optional[int]  = None
    query:        Optional[str]  = None
    entityId:     Optional[str]  = None
    entityType:   Optional[str]  = None
    templateType: Optional[str]  = None
    leadId:       Optional[str]  = None
    contactId:    Optional[str]  = None
    accountId:    Optional[str]  = None

    # Custom send_email params
    to:           Optional[str]  = None
    subject:      Optional[str]  = None
    bodyHtml:     Optional[str]  = None
    bodyText:     Optional[str]  = None


class EmailChatRequest(BaseModel):
    chatInput: Optional[EmailChatInput] = None
    sessionId: Optional[str]            = "default-session"
    message:   Optional[str]            = None


class EmailChatResponse(BaseModel):
    sessionId:      str
    output:         str
    rawParams:      Optional[dict] = None
    calledDatabase: bool           = False
    mode:           Optional[str]  = None
    success:        bool           = True

    emails:    Optional[List] = None
    events:    Optional[List] = None
    templates: Optional[List] = None
    to:        Optional[str]  = None
    subject:   Optional[str]  = None


# ============================================================================
# ROUTES
# ============================================================================

@router.get("/email-test")
async def email_test():
    """Send a test email and return SMTP diagnostics. Use to debug auth issues."""
    import os
    from app.agents.email.smtp_imap import send_email, _email_address, _smtp_host, _smtp_port
    addr = _email_address()
    pwd  = os.environ.get("EMAIL_PASSWORD", "")
    result = send_email(
        to=addr,
        subject="Conscestra CRM — SMTP test",
        body_html="<p>SMTP test. If you see this, email is working.</p>",
        body_text="SMTP test. If you see this, email is working.",
    )
    return {
        "smtp_host":      _smtp_host(),
        "smtp_port":      _smtp_port(),
        "email_address":  addr,
        "password_set":   bool(pwd),
        "password_length": len(pwd),
        "password_preview": pwd[:3] + "***" if pwd else "(empty)",
        "result":         result,
    }


@router.get("/email-health")
async def email_health():
    return {
        "status":           "healthy",
        "module":           "email",
        "version":          "1.0.0",
        "graph_initialized": get_graph() is not None,
    }


@router.post("/email-chat", response_model=EmailChatResponse)
async def email_chat(req: EmailChatRequest):
    logger.info("=== New Email Chat Request ===")

    session_id = (req.sessionId or "default-session").strip()
    ci         = req.chatInput or EmailChatInput(message=req.message or "")
    logger.info(f"Session: {session_id}  Message: {(ci.message or '')[:120]!r}")

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
            "extra":           {},
        }

        final_state  = graph_app.invoke(initial_state)
        fmt_result   = final_state.get("format_result") or {}
        called_db    = final_state.get("should_call_api", False)
        final_output = final_state.get("final_output") or ""
        raw_params   = final_state.get("parsed_json")
        mode         = fmt_result.get("mode") or (raw_params.get("mode") if raw_params else "unknown")

        logger.info(f"EmailAgent complete — DB={called_db}, mode={mode}")

        return EmailChatResponse(
            sessionId=session_id,
            output=final_output,
            rawParams=raw_params,
            calledDatabase=called_db,
            mode=mode,
            success=fmt_result.get("success", True),
            emails=fmt_result.get("emails"),
            events=fmt_result.get("events"),
            templates=fmt_result.get("templates"),
            to=fmt_result.get("to"),
            subject=fmt_result.get("subject"),
        )

    except WritePermissionError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    except Exception as e:
        logger.error(f"Email chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# ── Contact Us form ────────────────────────────────────────────────────────────

class ContactFormRequest(BaseModel):
    name:    str
    email:   str
    message: str
    company: str = ""
    phone:   str = ""


def _send_contact_notification(data: ContactFormRequest) -> None:
    """Fire-and-forget: forward contact form to info@agentorc.ca and auto-reply to sender."""
    from app.agents.email.smtp_imap import send_email

    first_name = data.name.strip().split()[0] if data.name.strip() else 'there'

    # 1. Internal notification to the team
    notif_subject = f"Contact Form: message from {data.name}"
    notif_text = (
        f"Name:    {data.name}\n"
        f"Email:   {data.email}\n"
        f"Company: {data.company or '—'}\n"
        f"Phone:   {data.phone or '—'}\n\n"
        f"{data.message}"
    )
    notif_html = f"""
<html><body style="font-family:Arial,sans-serif;color:#1a202c;max-width:600px;margin:auto;padding:2rem;font-size:0.95rem;line-height:1.6;">
<h2 style="color:#0d9488;">New Contact Form Submission</h2>
<table style="border-collapse:collapse;width:100%;">
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;white-space:nowrap;">Name</td><td>{data.name}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;white-space:nowrap;">Email</td><td><a href="mailto:{data.email}">{data.email}</a></td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;white-space:nowrap;">Company</td><td>{data.company or '—'}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;font-weight:600;white-space:nowrap;">Phone</td><td>{data.phone or '—'}</td></tr>
</table>
<hr style="margin:1rem 0;border:none;border-top:1px solid #e2e8f0;">
<p style="white-space:pre-wrap;">{data.message}</p>
</body></html>
"""
    r1 = send_email(to="info@agentorc.ca", subject=notif_subject,
                    body_html=notif_html, body_text=notif_text)
    if r1.get("success"):
        logger.info(f"Contact form notification forwarded from {data.email!r}")
    else:
        logger.warning(f"Contact form notification failed: {r1.get('message')}")

    # 2. Auto-reply to the person who submitted the form
    reply_subject = "We received your message — Conscestra CRM / Agentorc.ca"
    reply_text = (
        f"Hi {first_name},\n\n"
        "Thank you for reaching out to Conscestra CRM / Agentorc.ca!\n\n"
        "We've received your message and a member of our team will follow up with you shortly. "
        "In the meantime, feel free to explore our AI-powered CRM platform at agentorc.ca.\n\n"
        "If you have any urgent questions, you can reply directly to this email.\n\n"
        "The Conscestra CRM Team\n"
        "info@agentorc.ca\n"
        "https://agentorc.ca"
    )
    reply_html = f"""
<html><body style="font-family:Arial,sans-serif;color:#1a202c;max-width:600px;margin:auto;padding:2rem;font-size:0.95rem;line-height:1.6;">
<p>Hi {first_name},</p>
<p>Thank you for reaching out to <strong>Conscestra CRM / Agentorc.ca</strong>!</p>
<p>We've received your message and a member of our team will follow up with you shortly.
In the meantime, feel free to explore our AI-powered CRM platform at
<a href="https://agentorc.ca" style="color:#0d9488;">agentorc.ca</a>.</p>
<p>If you have any urgent questions, you can reply directly to this email.</p>
<p style="margin-top:1.5rem;">
  The Conscestra CRM Team<br>
  <a href="mailto:info@agentorc.ca" style="color:#0d9488;">info@agentorc.ca</a> &nbsp;|&nbsp;
  <a href="https://agentorc.ca" style="color:#0d9488;">agentorc.ca</a>
</p>
</body></html>
"""
    r2 = send_email(to=data.email, subject=reply_subject,
                    body_html=reply_html, body_text=reply_text)
    if r2.get("success"):
        logger.info(f"Contact form auto-reply sent to {data.email!r}")
    else:
        logger.warning(f"Contact form auto-reply failed: {r2.get('message')}")


# Public sub-router: the ONLY email endpoint anonymous visitors may call.
# The main email router is admin-gated in main.py (it can send mail as
# info@agentorc.ca), but the site's Contact Us form must keep working.
public_router = APIRouter(prefix="", tags=["Email"])


@public_router.post("/contact")
async def contact_form(data: ContactFormRequest, background_tasks: BackgroundTasks):
    """Receive Contact Us form, forward to info@agentorc.ca in background."""
    if not data.name.strip() or not data.email.strip() or not data.message.strip():
        raise HTTPException(status_code=422, detail="name, email, and message are required")
    background_tasks.add_task(_send_contact_notification, data)
    return {"ok": True}
