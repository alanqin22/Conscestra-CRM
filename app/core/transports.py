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

SECURITY POSTURE (the internal Slack/Teams surface answers from EVERY module, so
it is FAIL-CLOSED on both authentication and authorization):
  • Authentication — Slack requests are signature-verified; with no signing secret
    configured we REFUSE real events (not dev-permissive) unless TRANSPORTS_DEV_INSECURE
    is explicitly set for local work. Teams requires a shared bearer secret until a
    real Bot Framework JWT validator is wired.
  • Authorization — only a Slack/Teams user id that resolves to a KNOWN, linked
    EMPLOYEE (via identity.resolve) may ask; an unlinked/unknown id gets a refusal
    with linking instructions and NEVER touches the CRM. Mirrors the voice line's
    allowed-caller posture for chat channels.
  • Audience — a shared channel may contain people not entitled to CRM data, so an
    answer there is delivered PRIVATELY (ephemeral) to the asker by default. Only a
    DM (already 1:1) or a channel explicitly listed in SLACK_OPEN_CHANNELS gets an
    in-channel post. Surface-based (not content-classified), so nothing leaks on a
    misclassification, and delivery fails closed (ephemeral failure → draft, never a
    public post).
  • Abuse/cost — every inbound fires the METERED orchestrator, so each (channel,
    user) is rate-limited (TRANSPORTS_RATE_LIMIT / 10 min) BEFORE any DB/LLM work.

CONFIG (env)
  SLACK_SIGNING_SECRET   ''  verify Slack request signatures (REQUIRED in prod)
  SLACK_BOT_TOKEN        ''  send Slack replies (chat.postMessage) when set
  SLACK_OPEN_CHANNELS    ''  channel ids allowed to receive in-channel CRM answers
  TEAMS_INBOUND_SECRET   ''  shared bearer secret authenticating Teams activities
  WHATSAPP_VERIFY_TOKEN  ''  Meta webhook GET verification challenge token
  TRANSPORTS_ENABLED      1  master kill switch for all transport webhooks
  TRANSPORTS_RATE_LIMIT  20  inbound messages per (channel, user) per 10 minutes
  TRANSPORTS_DEV_INSECURE 0  dev only: accept internal webhooks with no secret set
  TRANSPORTS_ALLOW_UNLINKED_INTERNAL 0  dev only: let unlinked staff ids ask the CRM
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.core import channel_adapters, conversations, identity

logger = logging.getLogger("transports")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("TRANSPORTS_ENABLED", "1")
# Dev escape hatches — both default OFF so production is fail-closed by default.
_DEV_INSECURE = _flag("TRANSPORTS_DEV_INSECURE", "0")
_ALLOW_UNLINKED_INTERNAL = _flag("TRANSPORTS_ALLOW_UNLINKED_INTERNAL", "0")
_SLACK_MAX_SKEW = 60 * 5          # replay window for Slack timestamps (Slack's rec.)
# Channels explicitly designated for in-channel CRM answers. Any OTHER shared
# channel is private-by-default: answers go EPHEMERALLY to the asker so CRM data
# is never broadcast to a room that may include people not entitled to it. DMs are
# always answered in place (already 1:1). Comma-separated Slack channel ids.
_SLACK_OPEN_CHANNELS = {c.strip() for c in
                        os.getenv("SLACK_OPEN_CHANNELS", "").split(",") if c.strip()}
# Abuse / cost guard — every inbound fires the METERED orchestrator, so cap the
# rate per (channel, user). In-process sliding window (per-replica, like the embed
# widget's limiter); staff volume doesn't warrant a shared store.
_RATE_LIMIT = int(os.getenv("TRANSPORTS_RATE_LIMIT", "20"))
_RATE_WINDOW = 600                # seconds
_RATES: Dict[str, List[float]] = {}


def _rate_ok(channel: str, who: str) -> bool:
    """True if (channel, who) is under _RATE_LIMIT within _RATE_WINDOW; records the
    hit when allowed. Protects the metered orchestrator from a chatty channel or an
    abusive sender before we spend any DB/LLM work on the message."""
    bucket = f"{channel}:{who}"
    now = time.time()
    hits = [t for t in _RATES.get(bucket, []) if now - t < _RATE_WINDOW]
    if len(hits) >= max(1, _RATE_LIMIT):
        _RATES[bucket] = hits
        return False
    hits.append(now)
    _RATES[bucket] = hits
    return True

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


def _authorize_internal(channel: str, user: str) -> Tuple[bool, Optional[str]]:
    """FAIL-CLOSED authorization for the internal (employee) surface.

    The Orchestrator answers from EVERY module, so a raw platform user id is not
    enough — it must resolve (identity.resolve) to a KNOWN, linked EMPLOYEE. An
    unlinked/unknown id is refused with linking instructions and NEVER reaches the
    CRM (no read, no LLM spend). Returns (authorized, refusal_text)."""
    ident = None
    try:
        ident = identity.resolve(channel, user)
    except Exception as exc:                 # resolver must never open the gate
        logger.warning(f"[transports] identity resolve failed for "
                       f"{channel}:{user!r}: {exc}")
    authorized = bool(ident and ident.resolved and ident.party_type == "employee")
    if authorized or _ALLOW_UNLINKED_INTERNAL:
        if not authorized:
            logger.warning(f"[transports] {channel} user {user!r} unlinked but "
                           f"TRANSPORTS_ALLOW_UNLINKED_INTERNAL — allowing (dev)")
        return True, None
    logger.warning(f"[transports] REFUSED unlinked {channel} user {user!r} "
                   f"(fail-closed; TRANSPORTS_ALLOW_UNLINKED_INTERNAL=1 to relax)")
    return False, (f"🔒 I can only answer CRM questions for linked staff accounts. "
                   f"Your {channel.title()} ID isn't linked to an employee yet — "
                   f"ask an admin to link it (POST /identity/link) and try again.")


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
    """Verify Slack's request signature. FAIL-CLOSED: with no signing secret we
    refuse (return False) unless TRANSPORTS_DEV_INSECURE is set for local work.
    Also rejects stale timestamps to defeat replay of a captured request."""
    secret = os.getenv("SLACK_SIGNING_SECRET", "")
    if not secret:
        if _DEV_INSECURE:
            logger.warning("[transports] SLACK_SIGNING_SECRET unset — accepting "
                           "UNVERIFIED Slack event (TRANSPORTS_DEV_INSECURE)")
            return True
        logger.error("[transports] SLACK_SIGNING_SECRET unset — refusing Slack "
                     "event (set the secret; TRANSPORTS_DEV_INSECURE=1 for dev)")
        return False
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    try:                                     # replay window (Slack's own guidance)
        if abs(time.time() - int(ts)) > _SLACK_MAX_SKEW:
            logger.warning("[transports] Slack timestamp outside replay window")
            return False
    except (TypeError, ValueError):
        return False
    base = f"v0:{ts}:{raw.decode('utf-8', 'ignore')}"
    mac = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, sig or "")


def _teams_verify(headers) -> bool:
    """Authenticate a Teams activity. Real Bot Framework does OAuth JWT validation
    against Azure AD signing keys; until that connector is wired we require a
    shared bearer secret so the endpoint is never anonymously open. FAIL-CLOSED
    unless TRANSPORTS_DEV_INSECURE is set for local work."""
    secret = os.getenv("TEAMS_INBOUND_SECRET", "")
    if not secret:
        if _DEV_INSECURE:
            logger.warning("[transports] TEAMS_INBOUND_SECRET unset — accepting "
                           "UNVERIFIED Teams activity (TRANSPORTS_DEV_INSECURE)")
            return True
        logger.error("[transports] TEAMS_INBOUND_SECRET unset — refusing Teams "
                     "activity (set the secret; TRANSPORTS_DEV_INSECURE=1 for dev)")
        return False
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    return bool(token) and hmac.compare_digest(token, secret)


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


def _slack_post_ephemeral(channel: str, user: str, text: str) -> Dict[str, Any]:
    """Post a message only the target `user` can see in `channel` (chat.postEphemeral)
    — the private-by-default delivery for shared channels."""
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        return {"sent": False, "reason": "no SLACK_BOT_TOKEN (drafted)"}
    try:
        r = httpx.post("https://slack.com/api/chat.postEphemeral",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"channel": channel, "user": user, "text": text[:3500]},
                       timeout=10)
        j = r.json()
        return {"sent": bool(j.get("ok")),
                "error": None if j.get("ok") else j.get("error")}
    except Exception as exc:
        return {"sent": False, "error": str(exc)[:120]}


def _slack_deliver(channel_type: str, channel_id: str, user: str,
                   text: str) -> Dict[str, Any]:
    """Audience-scoped Slack delivery (see module SECURITY POSTURE). A DM (im) or a
    channel on SLACK_OPEN_CHANNELS is answered in place; any other shared channel is
    private-by-default → the answer goes EPHEMERALLY to the asker so CRM data is
    never broadcast. Fail-closed: on an ephemeral failure we DRAFT (the returned
    dict has sent=False) and NEVER fall back to a public post."""
    ct = (channel_type or "").lower()
    if ct == "im" or channel_id in _SLACK_OPEN_CHANNELS:
        res = _slack_post(channel_id, text)
        res["scope"] = "dm" if ct == "im" else "channel"
        return res
    res = _slack_post_ephemeral(channel_id, user, text)
    res["scope"] = "ephemeral"
    if not res.get("sent") and "no SLACK_BOT_TOKEN" not in (res.get("reason") or ""):
        logger.warning(f"[transports] ephemeral send failed ({res.get('error')}) — "
                       "drafting, NOT posting to channel (fail-closed)")
    return res


async def _slack_answer_async(text: str, user: str, channel_id: str,
                              channel_type: str, cap: Optional[Dict[str, Any]]) -> None:
    """Do the (slow) orchestrator round-trip AFTER we've already ack'd Slack, then
    deliver the reply (audience-scoped) and thread it. Runs as a BackgroundTask so
    the webhook meets Slack's 3-second deadline (a slow LLM would otherwise trigger
    retries and, eventually, auto-disable the event subscription)."""
    try:
        answer = await _answer_via_orchestrator(text, f"slack-{user}")
    except Exception as exc:
        logger.warning(f"[transports] Slack async answer failed: {exc}")
        return
    sent = await asyncio.to_thread(_slack_deliver, channel_type, channel_id, user, answer)
    await asyncio.to_thread(_thread_and_reply, cap, "slack", answer, sent)
    logger.info(f"[transports] Slack msg from {user} → conv "
                f"{(cap or {}).get('conversation_id', '?')[:8]} "
                f"(answered, scope={sent.get('scope')})")


@router.post("/slack/events")
async def slack_events(request: Request, background: BackgroundTasks):
    """Slack Events API: URL-verification handshake + inbound message events.
    A LINKED employee's message is threaded (internal) and answered by the
    Orchestrator asynchronously; we ACK within Slack's 3s window first."""
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

    # A retry means Slack didn't get our (now-fast) ACK — the first attempt is
    # already being answered, so ACK the retry without re-answering (dedupe).
    if request.headers.get("x-slack-retry-num"):
        return JSONResponse({"ok": True, "ignored": "retry"})

    event = body.get("event") or {}
    # Ignore bot echoes / non-message events to avoid loops.
    if event.get("type") != "message" or event.get("bot_id") or event.get("subtype"):
        return JSONResponse({"ok": True, "ignored": "non-user-message"})

    user = event.get("user") or ""
    text = event.get("text") or ""
    channel_id = event.get("channel", "")
    channel_type = event.get("channel_type", "")     # im | mpim | channel | group
    # Rate-limit BEFORE any DB/LLM work. ACK with 200 (not 429) — Slack treats a
    # non-2xx as a delivery failure and retries, which would defeat the limit and
    # eventually disable the subscription.
    if not _rate_ok("slack", user or (request.client.host if request.client else "?")):
        logger.warning(f"[transports] Slack rate limit hit for {user!r} — dropped")
        return JSONResponse({"ok": True, "ignored": "rate_limited"})
    # FAIL-CLOSED: only a linked employee may query the CRM. Thread the attempt
    # either way (audit), but an unlinked id gets a refusal, never CRM data. The
    # refusal is audience-scoped too (no channel noise / no CRM data leaked).
    authorized, refusal = _authorize_internal("slack", user)
    cap = channel_adapters.capture_slack(user, text)
    if not authorized:
        sent = _slack_deliver(channel_type, channel_id, user, refusal or "")
        return JSONResponse(_thread_and_reply(cap, "slack", refusal or "", sent)
                            | {"authorized": False})
    # Authorized: ACK now, answer in the background (meets the 3s deadline).
    background.add_task(_slack_answer_async, text, user, channel_id, channel_type, cap)
    return JSONResponse({"ok": True, "channel": "slack", "queued": True,
                         "conversation_id": (cap or {}).get("conversation_id")})


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
    if not _teams_verify(request.headers):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=403)
    try:
        act = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if (act.get("type") or "").lower() != "message":
        return JSONResponse({"ok": True, "ignored": act.get("type")})

    user = ((act.get("from") or {}).get("id")) or ""
    text = act.get("text") or ""
    if not _rate_ok("teams", user or (request.client.host if request.client else "?")):
        logger.warning(f"[transports] Teams rate limit hit for {user!r} — dropped")
        return JSONResponse({"ok": True, "ignored": "rate_limited"})
    # FAIL-CLOSED: only a linked employee may query the CRM (thread either way).
    authorized, refusal = _authorize_internal("teams", user)
    cap = channel_adapters.capture_teams(user, text)
    if not authorized:
        sent = {"sent": False, "reason": "unlinked employee (refused)"}
        return JSONResponse(_thread_and_reply(cap, "teams", refusal or "", sent)
                            | {"authorized": False})
    answer = await _answer_via_orchestrator(text, f"teams-{user}")
    # Audience scope for when the connector is wired: 'personal' (1:1) posts in
    # place; 'channel'/'groupChat' must deliver privately (the Bot Framework
    # equivalent of ephemeral) — mirrors the Slack posture. Recorded now, honored
    # when real Teams outbound lands. Nothing is sent today (drafted).
    conv_type = ((act.get("conversation") or {}).get("conversationType") or "").lower()
    scope = "personal" if conv_type == "personal" else "private"
    sent = {"sent": False, "scope": scope,
            "reason": "no Bot Framework connector configured (drafted)"}
    logger.info(f"[transports] Teams msg from {user} → conv "
                f"{(cap or {}).get('conversation_id', '?')[:8]} (answered, scope={scope})")
    return JSONResponse(_thread_and_reply(cap, "teams", answer, sent))


# ============================================================================
# Status
# ============================================================================

@router.get("/transports/status")
def transports_status():
    return {"enabled": ENABLED,
            "dev_insecure": _DEV_INSECURE,
            "allow_unlinked_internal": _ALLOW_UNLINKED_INTERNAL,
            "rate_limit": f"{_RATE_LIMIT}/{_RATE_WINDOW}s per (channel,user)",
            "whatsapp": {"meta_verify": bool(os.getenv("WHATSAPP_VERIFY_TOKEN"))},
            "slack": {"signing_secret": bool(os.getenv("SLACK_SIGNING_SECRET")),
                      "bot_token": bool(os.getenv("SLACK_BOT_TOKEN")),
                      "open_channels": len(_SLACK_OPEN_CHANNELS),
                      "private_by_default": True,
                      "fail_closed": bool(os.getenv("SLACK_SIGNING_SECRET"))
                      or not _DEV_INSECURE},
            "teams": {"inbound_secret": bool(os.getenv("TEAMS_INBOUND_SECRET")),
                      "connector": False,
                      "fail_closed": bool(os.getenv("TEAMS_INBOUND_SECRET"))
                      or not _DEV_INSECURE}}
