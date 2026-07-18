"""Channel Transports — provider webhooks that feed the Unified Communication Layer.

Each transport is a THIN edge: receive a provider's native webhook, translate it
through a Phase-2 adapter (channel_adapters.capture_*), and drive the natural
response for that side of the business:

  EXTERNAL  WhatsApp  → capture_whatsapp → the customer conversation → a
            KB-grounded auto-reply (reuses the SMS composer — WhatsApp is
            conversational text, same as SMS).
  INTERNAL  Slack / Teams → capture_slack/teams → the employee conversation →
            the ORCHESTRATOR answers ("what happened with Acme?") from the whole
            CRM, and the reply threads back.

Real outbound sends are GATED behind credentials (SLACK_BOT_TOKEN, a WhatsApp
sender): with none configured the composed reply is threaded as a DRAFT outbound
and returned — draft-first, exactly like the rest of the platform. Signature
verification engages when the provider secret is set, and is dev-permissive when
it isn't (mirrors telephony's tiered inbound).

CONFIG (env)
  SLACK_SIGNING_SECRET   ''  verify Slack request signatures when set
  SLACK_BOT_TOKEN        ''  send Slack replies (chat.postMessage) when set
  WHATSAPP_VERIFY_TOKEN  ''  Meta webhook GET verification challenge token
  TRANSPORTS_ENABLED      1  master kill switch for all transport webhooks
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.core import channel_adapters, conversations

logger = logging.getLogger("transports")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("TRANSPORTS_ENABLED", "1")

router = APIRouter(tags=["transports"])


# ============================================================================
# Shared — route an INTERNAL question to the Orchestrator (in-process ASGI)
# ============================================================================

async def _answer_via_orchestrator(message: str, session_id: str) -> str:
    """Employee asks the CRM anything → the Orchestrator answers from every
    module. Reads only under the current posture; no network hop (ASGI)."""
    try:
        from app.main import app as _app
        transport = httpx.ASGITransport(app=_app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://transports.internal",
                                     timeout=120) as client:
            r = await client.post("/orchestrator-chat", json={
                "sessionId": session_id, "chatInput": {"message": message}})
            d = r.json()
            if isinstance(d, list):
                d = d[0] if d else {}
            return str(d.get("output") or d.get("error") or "(no answer)")[:3500]
    except Exception as exc:
        logger.warning(f"[transports] orchestrator answer failed: {exc}")
        return "I couldn't reach the CRM just now — please try again shortly."


def _thread_and_reply(capture_result: Optional[Dict[str, Any]], channel: str,
                      reply: str, sent: Dict[str, Any]) -> Dict[str, Any]:
    """Thread the outbound reply into the same conversation (draft or sent) and
    build the transport's JSON result."""
    conv_id = (capture_result or {}).get("conversation_id")
    if conv_id and reply:
        try:
            conversations.append_outbound(conv_id, channel, reply, author="agent",
                                          metadata={"delivery": sent})
        except Exception as exc:
            logger.debug(f"[transports] outbound thread skipped: {exc}")
    return {"ok": True, "channel": channel, "conversation_id": conv_id,
            "resolved": (capture_result or {}).get("resolved"),
            "party_type": (capture_result or {}).get("party_type"),
            "reply": reply, "delivery": sent}


# ============================================================================
# EXTERNAL — WhatsApp (Twilio form OR Meta Cloud JSON)
# ============================================================================

def _parse_whatsapp(form: Dict[str, str], body_json: Any) -> Tuple[Optional[str], Optional[str]]:
    # Twilio: application/x-www-form-urlencoded, From='whatsapp:+1…', Body='…'
    if form and form.get("From"):
        return form.get("From", "").replace("whatsapp:", "").strip(), form.get("Body", "")
    # Meta WhatsApp Cloud API: entry[].changes[].value.messages[]
    try:
        v = body_json["entry"][0]["changes"][0]["value"]
        msg = v["messages"][0]
        frm = msg["from"]
        frm = frm if frm.startswith("+") else "+" + frm
        return frm, (msg.get("text") or {}).get("body", "")
    except Exception:
        return None, None


@router.get("/whatsapp/inbound")
def whatsapp_verify(request: Request):
    """Meta webhook verification handshake (GET with hub.challenge)."""
    q = request.query_params
    tok = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if q.get("hub.mode") == "subscribe" and tok and q.get("hub.verify_token") == tok:
        return PlainTextResponse(q.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/whatsapp/inbound")
async def whatsapp_inbound(request: Request):
    """Inbound WhatsApp → thread into the sender's ONE customer conversation and
    reply with a KB-grounded answer (draft unless a WhatsApp sender is wired)."""
    if not ENABLED:
        return JSONResponse({"ok": False, "error": "transports disabled"}, status_code=503)
    form: Dict[str, str] = {}
    body_json: Any = None
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body_json = await request.json()
        except Exception:
            body_json = None
    else:
        form = dict((await request.form()))
    sender, text = _parse_whatsapp(form, body_json)
    if not sender:
        return JSONResponse({"ok": False, "error": "no WhatsApp sender/body"}, status_code=400)

    cap = channel_adapters.capture_whatsapp(sender, text or "")
    # Reuse the SMS composer — WhatsApp is conversational text, same tier logic.
    from app.core.telephony import _compose_sms_reply
    reply = await _compose_sms_reply(sender, text or "", sender_e164=sender)
    # Real WhatsApp send is provider-specific + gated; draft otherwise.
    sent = {"sent": False, "reason": "no WhatsApp sender configured (drafted)"}
    logger.info(f"[transports] WhatsApp inbound from {sender} → conv "
                f"{(cap or {}).get('conversation_id', '?')[:8]}")
    return JSONResponse(_thread_and_reply(cap, "whatsapp", reply, sent))


# ============================================================================
# INTERNAL — Slack (Events API)
# ============================================================================

def _slack_verify(raw: bytes, headers) -> bool:
    secret = os.getenv("SLACK_SIGNING_SECRET", "")
    if not secret:
        return True                          # dev-permissive (mirrors telephony)
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    base = f"v0:{ts}:{raw.decode('utf-8', 'ignore')}"
    mac = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, sig or "")


def _slack_post(channel: str, text: str) -> Dict[str, Any]:
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        return {"sent": False, "reason": "no SLACK_BOT_TOKEN (drafted)"}
    try:
        r = httpx.post("https://slack.com/api/chat.postMessage",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"channel": channel, "text": text[:3500]}, timeout=10)
        return {"sent": bool(r.json().get("ok"))}
    except Exception as exc:
        return {"sent": False, "error": str(exc)[:120]}


@router.post("/slack/events")
async def slack_events(request: Request):
    """Slack Events API: URL-verification handshake + inbound message events.
    An employee's message is threaded (internal) and answered by the Orchestrator."""
    if not ENABLED:
        return JSONResponse({"ok": False, "error": "transports disabled"}, status_code=503)
    raw = await request.body()
    if not _slack_verify(raw, request.headers):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    if body.get("type") == "url_verification":      # one-time endpoint handshake
        return JSONResponse({"challenge": body.get("challenge")})

    event = body.get("event") or {}
    # Ignore bot echoes / non-message events to avoid loops.
    if event.get("type") != "message" or event.get("bot_id") or event.get("subtype"):
        return JSONResponse({"ok": True, "ignored": "non-user-message"})

    user = event.get("user") or ""
    text = event.get("text") or ""
    cap = channel_adapters.capture_slack(user, text)
    answer = await _answer_via_orchestrator(text, f"slack-{user}")
    sent = _slack_post(event.get("channel", ""), answer)
    logger.info(f"[transports] Slack msg from {user} → conv "
                f"{(cap or {}).get('conversation_id', '?')[:8]} (answered)")
    return JSONResponse(_thread_and_reply(cap, "slack", answer, sent))


# ============================================================================
# INTERNAL — Microsoft Teams (Bot Framework Activity)
# ============================================================================

@router.post("/teams/messages")
async def teams_messages(request: Request):
    """Teams Bot Framework: inbound message Activity → threaded (internal) +
    answered by the Orchestrator. Reply-send needs the Bot Framework connector
    credentials (gated); otherwise the answer is drafted + returned."""
    if not ENABLED:
        return JSONResponse({"ok": False, "error": "transports disabled"}, status_code=503)
    try:
        act = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if (act.get("type") or "").lower() != "message":
        return JSONResponse({"ok": True, "ignored": act.get("type")})

    user = ((act.get("from") or {}).get("id")) or ""
    text = act.get("text") or ""
    cap = channel_adapters.capture_teams(user, text)
    answer = await _answer_via_orchestrator(text, f"teams-{user}")
    sent = {"sent": False, "reason": "no Bot Framework connector configured (drafted)"}
    logger.info(f"[transports] Teams msg from {user} → conv "
                f"{(cap or {}).get('conversation_id', '?')[:8]} (answered)")
    return JSONResponse(_thread_and_reply(cap, "teams", answer, sent))


# ============================================================================
# Status
# ============================================================================

@router.get("/transports/status")
def transports_status():
    return {"enabled": ENABLED,
            "whatsapp": {"meta_verify": bool(os.getenv("WHATSAPP_VERIFY_TOKEN"))},
            "slack": {"signing_secret": bool(os.getenv("SLACK_SIGNING_SECRET")),
                      "bot_token": bool(os.getenv("SLACK_BOT_TOKEN"))},
            "teams": {"connector": False}}
