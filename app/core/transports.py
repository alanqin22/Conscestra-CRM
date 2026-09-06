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

PARTICIPATION (agents as first-class members of team channels):
  • #6 In-thread approvals — a routed governance approval arrives with native Block
    Kit Approve/Reject buttons; POST /slack/interactive verifies (signature +
    linked-employee + HMAC token) and applies the decision through the SAME
    governance flow as the one-click email links, updating the message in place.
  • #5 Proactive posting — post_internal() / POST /comms/announce broadcast an
    alert/briefing INTO a channel (draft-first: SLACK_PROACTIVE_ENABLED + a token).
  • #8 Teams outbound — a 1:1 (personal) chat gets a real Bot Framework reply when
    MICROSOFT_APP_ID/PASSWORD are set; channel/group replies stay withheld (no
    ephemeral → audience scoping preserved).
  • Mention-gating — in a shared channel the bot answers only when @mentioned.

CONFIG (env)
  SLACK_SIGNING_SECRET   ''  verify Slack request signatures (REQUIRED in prod)
  SLACK_BOT_TOKEN        ''  send Slack replies/blocks (chat.postMessage) when set
  SLACK_OPEN_CHANNELS    ''  channel ids allowed to receive in-channel CRM answers
  SLACK_BOT_USER_ID      ''  the bot's own user id — enables @mention-gating
  SLACK_REQUIRE_MENTION   1  in a shared channel, answer only when @mentioned
  SLACK_PROACTIVE_ENABLED 0  allow proactive posts into channels (#5)
  SLACK_ALERTS_CHANNEL   ''  target channel for internal_alert proactive posts
  SLACK_BRIEFING_CHANNEL ''  target channel for internal_briefing proactive posts
  SLACK_DEFAULT_CHANNEL  ''  fallback proactive channel when a kind has none
  TEAMS_INBOUND_SECRET   ''  shared bearer secret authenticating Teams activities
  MICROSOFT_APP_ID       ''  Bot Framework app id — enables Teams outbound (#8)
  MICROSOFT_APP_PASSWORD ''  Bot Framework app secret
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
import json
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
# Participation: in a SHARED channel the bot is a well-behaved participant — it
# answers only when @mentioned (a DM is always 1:1, so always answered). Needs the
# bot's own user id to detect the mention; if unknown, mention-gating is skipped.
_SLACK_BOT_USER_ID = os.getenv("SLACK_BOT_USER_ID", "").strip()
_REQUIRE_MENTION = _flag("SLACK_REQUIRE_MENTION", "1")
# #5 Proactive posting — agents post briefings/alerts INTO channels on their own.
# Off by default (draft-first posture); real posts also need SLACK_BOT_TOKEN.
_PROACTIVE_ENABLED = _flag("SLACK_PROACTIVE_ENABLED", "0")
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

    # Opt-out keywords, ahead of the assistant — the same rule as inbound SMS.
    # WhatsApp has NO outbound sender wired today (the reply below is drafted,
    # not sent), so this cannot yet stop a message going out. It is here anyway
    # because the WITHDRAWAL must be recorded the moment it is expressed: when
    # a sender is eventually wired, consent.allows("whatsapp", …) must already
    # know about everyone who said stop, not start from an empty list.
    try:
        from app.core import consent
        verdict = consent.classify_inbound(text or "")
        if verdict:
            consent.record("whatsapp", sender, verdict,
                           reason=f"inbound keyword: {(text or '')[:40]}",
                           source="inbound_whatsapp", actor=sender)
            logger.info(f"[transports] WhatsApp {verdict} recorded for {sender}")
            return JSONResponse(_thread_and_reply(
                cap, "whatsapp",
                ("You have been unsubscribed and will receive no further "
                 "marketing messages. Reply START to resubscribe."
                 if verdict == "opted_out" else
                 "You are resubscribed. Reply STOP to unsubscribe."),
                {"sent": False, "reason": "consent confirmation (drafted — no "
                                          "WhatsApp sender wired)"}))
    except Exception as exc:                                # noqa: BLE001
        logger.warning(f"[transports] whatsapp consent check skipped: {exc}")
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


def _slack_post_blocks(channel: str, text: str,
                       blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Post an interactive Block Kit message (buttons etc.). `text` is the
    notification/fallback shown where blocks can't render."""
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        return {"sent": False, "reason": "no SLACK_BOT_TOKEN (drafted)"}
    try:
        r = httpx.post("https://slack.com/api/chat.postMessage",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"channel": channel, "text": text[:3000],
                             "blocks": blocks}, timeout=10)
        j = r.json()
        return {"sent": bool(j.get("ok")),
                "error": None if j.get("ok") else j.get("error"),
                "ts": j.get("ts")}
    except Exception as exc:
        return {"sent": False, "error": str(exc)[:120]}


def _slack_respond(response_url: str, text: str,
                   replace_original: bool = True) -> Dict[str, Any]:
    """Update the originating interactive message via its (signed, temporary)
    response_url — no bot token needed."""
    try:
        r = httpx.post(response_url, json={"text": text[:3000],
                       "replace_original": replace_original}, timeout=10)
        return {"sent": r.status_code == 200}
    except Exception as exc:
        return {"sent": False, "error": str(exc)[:120]}


def approval_blocks(approval_uuid: str, action_type: str, amount: float,
                    summary: str, approve_token: str, reject_token: str
                    ) -> Tuple[str, List[Dict[str, Any]]]:
    """Build the (fallback text, Block Kit blocks) for an in-thread approval. The
    button `value` carries the HMAC-signed (approval, action) token so the
    interactivity endpoint verifies the decision is untampered — the same token
    the one-click email links use. Returns (text, blocks)."""
    amt = f" (${amount:,.0f})" if amount else ""
    text = f"🛡️ Approval needed: {action_type}{amt}"
    ctx = (summary or "").strip()
    blocks: List[Dict[str, Any]] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🛡️ Approval needed:* {action_type}{amt}"}},
    ]
    if ctx:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": ctx[:2900]}})
    blocks.append({"type": "actions", "block_id": f"gov:{approval_uuid}", "elements": [
        {"type": "button", "style": "primary", "action_id": "gov_approve",
         "text": {"type": "plain_text", "text": "Approve"},
         "value": f"approve:{approval_uuid}:{approve_token}"},
        {"type": "button", "style": "danger", "action_id": "gov_reject",
         "text": {"type": "plain_text", "text": "Reject"},
         "value": f"reject:{approval_uuid}:{reject_token}"},
    ]})
    return text, blocks


def _proactive_channel(kind: str) -> str:
    """Resolve the target Slack channel for a proactive (#5) post by its kind."""
    m = {"internal_alert": os.getenv("SLACK_ALERTS_CHANNEL", ""),
         "internal_briefing": os.getenv("SLACK_BRIEFING_CHANNEL", "")}
    return (m.get(kind) or os.getenv("SLACK_DEFAULT_CHANNEL", "")).strip()


def post_internal(kind: str, text: str, channel: Optional[str] = None,
                  blocks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """#5 — proactively post an internal alert/briefing INTO a team channel.
    Gated: SLACK_PROACTIVE_ENABLED must be on AND a channel must resolve (explicit
    arg, or the kind's configured channel). Draft-first when disabled or when no
    bot token is set — the composed message is returned, never silently dropped."""
    target = (channel or _proactive_channel(kind)).strip()
    if not _PROACTIVE_ENABLED:
        return {"sent": False, "reason": "SLACK_PROACTIVE_ENABLED off (drafted)",
                "channel": target, "kind": kind, "text": text}
    if not target:
        return {"sent": False, "reason": f"no channel configured for '{kind}' "
                "(set SLACK_ALERTS_CHANNEL / SLACK_BRIEFING_CHANNEL / "
                "SLACK_DEFAULT_CHANNEL)", "kind": kind, "text": text}
    res = _slack_post_blocks(target, text, blocks) if blocks else _slack_post(target, text)
    res.update({"channel": target, "kind": kind})
    logger.info(f"[transports] proactive {kind} → {target}: sent={res.get('sent')}")
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
    # Participation: in a shared channel, only respond when @mentioned (a DM is
    # always 1:1). Keeps the bot from answering every message in channels it's in.
    if channel_type != "im" and _REQUIRE_MENTION and _SLACK_BOT_USER_ID:
        mention = f"<@{_SLACK_BOT_USER_ID}>"
        if mention not in text:
            return JSONResponse({"ok": True, "ignored": "not-mentioned"})
        text = text.replace(mention, "").strip()     # answer the actual ask
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


async def _slack_apply_decision(action: str, approval_uuid: str, token: str,
                                response_url: str, decided_by: str) -> None:
    """#6 — run an in-thread approval decision AFTER we've ACK'd Slack (3s rule),
    then replace the buttoned message with the outcome via response_url. The HMAC
    token is re-verified here (defense in depth alongside the Slack signature +
    linked-employee check already done in the handler)."""
    from app.core import governance
    # NB: this runs on the event loop, so every blocking call (DB, outbound HTTP)
    # is offloaded with asyncio.to_thread — a sync httpx.post here would stall the
    # single worker (and can deadlock against a response_url on this same server).
    async def _respond(text: str) -> None:
        await asyncio.to_thread(_slack_respond, response_url, text)

    if not governance._verify_token(approval_uuid, action, token):
        await _respond("⚠️ This decision link is invalid or was tampered with — "
                       "no action taken.")
        return
    row = await asyncio.to_thread(governance._row, approval_uuid)
    if not row:
        await _respond("This approval no longer exists.")
        return
    if row.get("status") != "pending":
        await _respond(f"↩️ Already *{row.get('status')}* — nothing further happened.")
        return
    try:
        if action == "approve":
            res = await governance.approve(approval_uuid, decided_by=decided_by, via="chat")
            ok = res.get("ok")
            if res.get("refused") == "authority":
                msg = f"⛔ <@{decided_by}> is not an approval authority for this item: {res.get('error')}"
            else:
                msg = (f"✅ *Approved* by <@{decided_by}> — "
                       + ("executed." if ok else f"execution FAILED: "
                          f"{(res.get('result') or {}).get('error') or 'unknown error'}"))
        else:
            res = await asyncio.to_thread(governance.reject, approval_uuid, decided_by, None, "chat")
            if res.get("refused") == "authority":
                msg = f"⛔ <@{decided_by}> is not an approval authority for this item: {res.get('error')}"
            else:
                msg = f"🚫 *Rejected* by <@{decided_by}> — no action was taken."
    except Exception as exc:
        logger.warning(f"[transports] Slack decision apply failed: {exc}")
        msg = "⚠️ Something went wrong applying that decision."
    await _respond(msg)


@router.post("/slack/interactive")
async def slack_interactive(request: Request, background: BackgroundTasks):
    """#6 — Slack interactivity endpoint: in-thread Approve/Reject buttons on a
    routed governance approval. Signature-verified, then FAIL-CLOSED on identity
    (only a linked employee may decide) AND on the HMAC decision token. ACKs within
    3s and applies the decision in the background, updating the message in place."""
    if not ENABLED:
        return JSONResponse({"ok": False, "error": "transports disabled"}, status_code=503)
    raw = await request.body()
    if not _slack_verify(raw, request.headers):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=403)
    try:
        form = await request.form()
        payload = json.loads(form.get("payload") or "{}")
    except Exception:
        return JSONResponse({"ok": False, "error": "bad payload"}, status_code=400)
    if payload.get("type") != "block_actions":
        return JSONResponse({"ok": True, "ignored": payload.get("type")})

    slack_user = ((payload.get("user") or {}).get("id")) or ""
    authorized, _refusal = _authorize_internal("slack", slack_user)
    if not authorized:
        # Ephemeral, private refusal — never acts, never leaks.
        return JSONResponse({"text": "🔒 Only linked staff accounts can approve or "
                             "reject. Ask an admin to link your Slack ID.",
                             "response_type": "ephemeral"})
    actions = payload.get("actions") or []
    value = (actions[0].get("value") if actions else "") or ""
    parts = value.split(":", 2)
    if len(parts) != 3 or parts[0] not in ("approve", "reject"):
        return JSONResponse({"ok": True, "ignored": "unknown-action"})
    action, approval_uuid, token = parts
    response_url = payload.get("response_url") or ""
    # ACK now (Slack's 3s rule); apply + update the message in the background.
    background.add_task(_slack_apply_decision, action, approval_uuid, token,
                        response_url, slack_user)
    return JSONResponse({"text": f"⏳ Processing your *{action}*…",
                         "replace_original": False, "response_type": "ephemeral"})


# ============================================================================
# INTERNAL — Microsoft Teams (Bot Framework Activity)
# ============================================================================

_TEAMS_TOKEN: Dict[str, Any] = {"token": "", "exp": 0.0}


def _teams_token() -> str:
    """Bot Framework app token (client credentials), cached until ~expiry. Empty
    when MICROSOFT_APP_ID/PASSWORD aren't set — i.e. connector not configured."""
    app_id = os.getenv("MICROSOFT_APP_ID", "")
    secret = os.getenv("MICROSOFT_APP_PASSWORD", "")
    if not app_id or not secret:
        return ""
    now = time.time()
    if _TEAMS_TOKEN["token"] and _TEAMS_TOKEN["exp"] > now + 30:
        return _TEAMS_TOKEN["token"]
    try:
        r = httpx.post(
            "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": app_id,
                  "client_secret": secret,
                  "scope": "https://api.botframework.com/.default"}, timeout=10)
        j = r.json()
        _TEAMS_TOKEN["token"] = j.get("access_token", "")
        _TEAMS_TOKEN["exp"] = now + float(j.get("expires_in", 3600))
        return _TEAMS_TOKEN["token"]
    except Exception as exc:
        logger.warning(f"[transports] Teams token fetch failed: {exc}")
        return ""


def _teams_post(activity: Dict[str, Any], text: str) -> Dict[str, Any]:
    """#8 — reply into the SAME Teams conversation as the inbound activity via the
    Bot Framework connector. Credential-gated (MICROSOFT_APP_ID/PASSWORD); drafts
    otherwise (draft-first, like every other outbound on the platform)."""
    token = _teams_token()
    if not token:
        return {"sent": False, "reason": "no Bot Framework connector configured (drafted)"}
    service_url = (activity.get("serviceUrl") or "").rstrip("/")
    conv = activity.get("conversation") or {}
    conv_id = conv.get("id") or ""
    if not service_url or not conv_id:
        return {"sent": False, "reason": "activity missing serviceUrl/conversation id"}
    reply = {"type": "message", "text": text[:3500],
             "from": activity.get("recipient") or {},
             "recipient": activity.get("from") or {}, "conversation": conv}
    if activity.get("id"):
        reply["replyToId"] = activity["id"]
    try:
        r = httpx.post(f"{service_url}/v3/conversations/{conv_id}/activities",
                       headers={"Authorization": f"Bearer {token}"},
                       json=reply, timeout=10)
        return {"sent": r.status_code in (200, 201, 202), "status": r.status_code}
    except Exception as exc:
        return {"sent": False, "error": str(exc)[:120]}


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
    # Audience scoping (#3), now with real outbound (#8): a 'personal' (1:1) chat is
    # answered in place via the Bot Framework connector; a 'channel'/'groupChat' has
    # no ephemeral equivalent, so CRM data is WITHHELD from the room (drafted) rather
    # than broadcast — the private-by-default guarantee holds for Teams too.
    conv_type = ((act.get("conversation") or {}).get("conversationType") or "").lower()
    if conv_type == "personal":
        sent = _teams_post(act, answer)
        sent["scope"] = "personal"
    else:
        sent = {"sent": False, "scope": "private",
                "reason": "Teams channel/group reply withheld (no private delivery) — drafted"}
    logger.info(f"[transports] Teams msg from {user} → conv "
                f"{(cap or {}).get('conversation_id', '?')[:8]} "
                f"(answered, scope={sent.get('scope')}, sent={sent.get('sent')})")
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
                      "require_mention": _REQUIRE_MENTION and bool(_SLACK_BOT_USER_ID),
                      "interactive_approvals": True,
                      "proactive_enabled": _PROACTIVE_ENABLED,
                      "fail_closed": bool(os.getenv("SLACK_SIGNING_SECRET"))
                      or not _DEV_INSECURE},
            "teams": {"inbound_secret": bool(os.getenv("TEAMS_INBOUND_SECRET")),
                      "connector": bool(_teams_token()),
                      "fail_closed": bool(os.getenv("TEAMS_INBOUND_SECRET"))
                      or not _DEV_INSECURE}}


# ============================================================================
# Admin — proactive posting trigger (#5), mounted admin-gated in main.py
# ============================================================================

admin_router = APIRouter(tags=["transports-admin"])


@admin_router.post("/comms/announce")
def comms_announce(body: Dict[str, Any]):
    """Proactively post an internal alert/briefing into a team channel (#5).
    Dry-run by default (composes without sending); pass send=true to deliver.
    Body: {kind?: 'internal_alert'|'internal_briefing', text: str, channel?: str,
    send?: bool}. Real delivery also needs SLACK_PROACTIVE_ENABLED + SLACK_BOT_TOKEN."""
    b = body or {}
    text = str(b.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    kind = str(b.get("kind") or "internal_alert")
    if not b.get("send"):
        return {"ok": True, "dry_run": True, "kind": kind,
                "channel": (b.get("channel") or _proactive_channel(kind)),
                "preview": text[:400]}
    res = post_internal(kind, text, channel=b.get("channel"))
    return {"ok": bool(res.get("sent")), **res}
