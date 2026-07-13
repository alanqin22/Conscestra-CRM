"""Telephony channel — SMS + voice over Twilio (advanced improvement #6).

The omnichannel gap closes: the profile already LEARNS each customer's
preferred channel; now the agents can actually use it.

    send_sms      outbound SMS via the Twilio REST API. Gated exactly like
                  email: SMS_AUTOSEND=0 → the message is DRAFTED as an owner
                  task, never sent. Every real send is logged as an outbound
                  activity. Governed for agents via A2A `sms.send`
                  (critic-checked; irreversible — no undo, like email).
    place_call    agent-initiated voice call that SPEAKS a message (TwiML
                  <Say> passed inline — no public URL needed). Same gates.
    inbound SMS   POST /telephony/sms/inbound (PUBLIC, X-Twilio-Signature
                  validated — the signature IS the auth): sender matched to
                  contact/lead by E.164 phone, inbound activity logged,
                  `sms.received` event emitted, and a KB-grounded, PII-safe
                  auto-reply returned as TwiML (SMS_AUTOREPLY=0 to ack only).

PROVIDERS: Twilio and Telnyx behind one `TELEPHONY_PROVIDER` switch. Twilio
uses SID+token basic auth, form posts, HMAC-SHA1 webhook signatures, and
inline TwiML for calls. Telnyx uses a Bearer API key, JSON, Ed25519 webhook
signatures, and a TeXML application + webhook URL for calls. Everything above
the adapter (governance, critic checks, activity logging, KB-grounded
replies, PII masking, the SDR loop) is provider-agnostic and unchanged.

TRIAL NOTES: a Twilio trial only reaches VERIFIED numbers and prefixes
messages with "Sent from your Twilio trial account". A funded Telnyx account
reaches any number. Rotate secrets after sharing them anywhere.

CONFIG (env)
  TELEPHONY_PROVIDER   twilio   twilio | telnyx (which carrier is live)
  SMS_AUTOSEND    0   1 = really send SMS/calls; else draft as owner tasks
  SMS_AUTOREPLY   1   inbound webhook replies with the LLM/KB answer
  -- Twilio --  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
  -- Telnyx --  TELNYX_API_KEY / TELNYX_FROM_NUMBER
                TELNYX_PUBLIC_KEY    account Ed25519 public key (webhook auth)
                TELNYX_TEXML_APP_ID  TeXML application id (outbound calls)
                TELNYX_PUBLIC_BASE   public base URL for outbound-call TeXML
                                     (falls back to APP_URL)
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json as _json
import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, Response

from app.core.database import get_connection

logger = logging.getLogger("telephony")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _provider() -> str:
    return os.getenv("TELEPHONY_PROVIDER", "twilio").strip().lower()


# ── Twilio creds ──
def _sid() -> str:
    return os.getenv("TWILIO_ACCOUNT_SID", "").strip()


def _token() -> str:
    return os.getenv("TWILIO_AUTH_TOKEN", "").strip()


# ── Telnyx creds ──
def _telnyx_key() -> str:
    return os.getenv("TELNYX_API_KEY", "").strip()


def _telnyx_public_key() -> str:
    return os.getenv("TELNYX_PUBLIC_KEY", "").strip()


def _telnyx_texml_app() -> str:
    return os.getenv("TELNYX_TEXML_APP_ID", "").strip()


def _from_number() -> str:
    """The active sender number for the current provider."""
    if _provider() == "telnyx":
        return os.getenv("TELNYX_FROM_NUMBER", "").strip()
    return os.getenv("TWILIO_FROM_NUMBER", "").strip()


AUTOSEND = _flag("SMS_AUTOSEND", "0")
AUTOREPLY = _flag("SMS_AUTOREPLY", "1")

_API = "https://api.twilio.com/2010-04-01/Accounts"
_TELNYX_API = "https://api.telnyx.com/v2"
SMS_MAX_CHARS = 320          # ~2 segments; the critic warns beyond this


def configured() -> bool:
    if _provider() == "telnyx":
        return bool(_telnyx_key() and _from_number())
    return bool(_sid() and _token() and _from_number())


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Best-effort E.164 (defaults NANP when 10 digits). None when hopeless."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if (raw or "").strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if len(digits) >= 8 else None


def _log_activity(kind: str, subject: str, description: str, *,
                  direction: str, lead_id=None, account_id=None,
                  owner_id=None, status: str = "completed") -> None:
    if not (lead_id or account_id):
        # activities.related_id is NOT NULL — sends with no CRM entity
        # (ad-hoc admin/live tests) are audit-logged only.
        logger.info(f"[telephony] {subject} (no CRM entity — activity skipped)")
        return
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, direction, channel,
                      owner_id, related_type, related_id, account_id, lead_id,
                      due_at, completed_at, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, 'sms', %s, %s, %s::uuid,
                           %s::uuid, %s::uuid,
                           CASE WHEN %s='open' THEN now() + interval '4 hours' END,
                           CASE WHEN %s='completed' THEN now() END, now(), now())""",
                (kind, status, subject[:180], description[:2000], direction,
                 owner_id, "lead" if lead_id else "account",
                 lead_id or account_id, account_id, lead_id, status, status))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"[telephony] activity log skipped: {exc}")


# ============================================================================
# OUTBOUND — SMS + spoken call (both AUTOSEND-gated)
# ============================================================================

def send_sms(to: str, body: str, *, lead_id=None, account_id=None,
             owner_id=None, sent_by: str = "agent") -> Dict[str, Any]:
    """Send (or draft) one SMS. Trial accounts only reach verified numbers."""
    to_n = normalize_phone(to)
    if not to_n:
        return {"ok": False, "error": f"unusable phone number {to!r}"}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "empty message body"}
    if not configured():
        return {"ok": False, "error": "Twilio not configured "
                                      "(TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM_NUMBER)"}

    if not AUTOSEND:
        _log_activity("task", f"Send SMS to {to_n} (draft)",
                      f"SMS drafted by {sent_by} (SMS_AUTOSEND=0 — not sent):\n"
                      f"{body}", direction="outbound", lead_id=lead_id,
                      account_id=account_id, owner_id=owner_id, status="open")
        return {"ok": True, "sent": False, "drafted": True, "to": to_n,
                "note": "SMS_AUTOSEND=0 — drafted as an owner task"}

    import requests
    prov = _provider()
    try:
        if prov == "telnyx":
            r = requests.post(
                f"{_TELNYX_API}/messages",
                headers={"Authorization": f"Bearer {_telnyx_key()}"},
                json={"from": _from_number(), "to": to_n, "text": body[:1600]},
                timeout=20)
        else:
            r = requests.post(
                f"{_API}/{_sid()}/Messages.json",
                data={"To": to_n, "From": _from_number(), "Body": body[:1600]},
                auth=(_sid(), _token()), timeout=20)
        payload = r.json() if r.content else {}
    except Exception as exc:
        return {"ok": False, "error": f"{prov} request failed: {exc}"}
    if r.status_code >= 300:
        return {"ok": False, "error": f"{prov} {r.status_code}: "
                                      f"{payload.get('message') or payload.get('errors') or r.text[:200]}"}
    sid = ((payload.get("data") or {}).get("id") if prov == "telnyx"
           else payload.get("sid"))
    _log_activity("call", f"SMS sent to {to_n}",
                  f"Sent by {sent_by} via {prov} ({sid or ''}):\n{body}",
                  direction="outbound", lead_id=lead_id, account_id=account_id,
                  owner_id=owner_id)
    logger.info(f"[telephony] SMS → {to_n} via {prov} ({str(sid)[:12]})")
    return {"ok": True, "sent": True, "to": to_n, "sid": sid,
            "status": (payload.get("data") or {}).get("status") if prov == "telnyx"
                      else payload.get("status")}


def _twiml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;").replace('"', "&quot;")


def _say_document(text: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Say voice="alice">{_twiml_escape(text[:800])}</Say></Response>')


def _public_base() -> str:
    return (os.getenv("TELNYX_PUBLIC_BASE") or os.getenv("APP_URL")
            or "http://localhost:8000").rstrip("/")


def _say_token(text: str) -> str:
    """HMAC over the spoken text — authorizes the public TeXML say endpoint
    (Telnyx fetches it server-side to play the message)."""
    secret = (_telnyx_key() or os.getenv("ADMIN_API_TOKEN") or "").encode()
    return _hmac.new(secret, text.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _say_url(text: str) -> str:
    from urllib.parse import quote
    return (f"{_public_base()}/telephony/texml/say?text={quote(text[:800])}"
            f"&t={_say_token(text[:800])}")


def place_call(to: str, say_text: str, *, lead_id=None, account_id=None,
               owner_id=None, called_by: str = "agent") -> Dict[str, Any]:
    """Place a call that speaks `say_text`. Twilio: inline TwiML (no public
    URL). Telnyx: a TeXML application call pointed at a signed say URL."""
    to_n = normalize_phone(to)
    if not to_n:
        return {"ok": False, "error": f"unusable phone number {to!r}"}
    if not configured():
        return {"ok": False, "error": f"{_provider()} not configured"}
    if not AUTOSEND:
        _log_activity("task", f"Place call to {to_n} (draft)",
                      f"Voice message drafted by {called_by} (SMS_AUTOSEND=0):\n"
                      f"{say_text}", direction="outbound", lead_id=lead_id,
                      account_id=account_id, owner_id=owner_id, status="open")
        return {"ok": True, "called": False, "drafted": True, "to": to_n}

    import requests
    prov = _provider()
    try:
        if prov == "telnyx":
            app = _telnyx_texml_app()
            if not app:
                return {"ok": False, "error": "TELNYX_TEXML_APP_ID not set — "
                        "outbound Telnyx voice needs the TeXML application id"}
            r = requests.post(
                f"{_TELNYX_API}/texml/calls/{app}",
                headers={"Authorization": f"Bearer {_telnyx_key()}"},
                json={"To": to_n, "From": _from_number(),
                      "Url": _say_url(say_text)}, timeout=20)
        else:
            twiml = (f'<Response><Say voice="alice">'
                     f'{_twiml_escape(say_text[:800])}</Say></Response>')
            r = requests.post(
                f"{_API}/{_sid()}/Calls.json",
                data={"To": to_n, "From": _from_number(), "Twiml": twiml},
                auth=(_sid(), _token()), timeout=20)
        payload = r.json() if r.content else {}
    except Exception as exc:
        return {"ok": False, "error": f"{prov} request failed: {exc}"}
    if r.status_code >= 300:
        return {"ok": False, "error": f"{prov} {r.status_code}: "
                                      f"{payload.get('message') or payload.get('errors') or r.text[:200]}"}
    sid = ((payload.get("data") or {}).get("call_sid")
           or (payload.get("data") or {}).get("id") if prov == "telnyx"
           else payload.get("sid"))
    _log_activity("call", f"Voice call to {to_n}",
                  f"Agent call placed by {called_by} via {prov} "
                  f"({sid or ''}). Spoken message:\n{say_text}",
                  direction="outbound", lead_id=lead_id, account_id=account_id,
                  owner_id=owner_id)
    logger.info(f"[telephony] call → {to_n} via {prov} ({str(sid)[:12]})")
    return {"ok": True, "called": True, "to": to_n, "sid": sid,
            "status": (payload.get("data") or {}).get("status") if prov == "telnyx"
                      else payload.get("status")}


def send_sms_sp(p: Dict[str, Any]) -> Dict[str, Any]:
    """A2A structured handler for sms.send (governed write)."""
    return send_sms(str(p.get("to") or ""), str(p.get("body") or ""),
                    lead_id=p.get("lead_id"), account_id=p.get("account_id"),
                    sent_by=str(p.get("sent_by", "a2a")))


# ============================================================================
# INBOUND SMS — Twilio webhook (signature IS the auth)
# ============================================================================

def _valid_signature(url: str, params: Dict[str, str], signature: str) -> bool:
    """Twilio X-Twilio-Signature: base64(HMAC-SHA1(token, url + k1v1k2v2…)).

    Behind a TLS-terminating proxy (Railway, tunnels) the app may see the URL
    as http:// while Twilio signed the https:// one — so an http URL is also
    checked with the scheme upgraded. Still a pure HMAC against the token."""
    if not _token() or not signature:
        return False

    def _check(u: str) -> bool:
        payload = u + "".join(k + params[k] for k in sorted(params))
        digest = _hmac.new(_token().encode(), payload.encode(),
                           hashlib.sha1).digest()
        return _hmac.compare_digest(base64.b64encode(digest).decode(), signature)

    if _check(url):
        return True
    if url.startswith("http://"):
        return _check("https://" + url[len("http://"):])
    return False


def _telnyx_valid_signature(raw_body: bytes, sig_b64: str,
                            timestamp: str) -> bool:
    """Telnyx Ed25519: verify base64(sig) over `{timestamp}|{raw_body}` with
    the account public key. Fail-closed on any missing piece or error."""
    pub = _telnyx_public_key()
    if not (pub and sig_b64 and timestamp):
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub))
        signed = timestamp.encode("utf-8") + b"|" + (raw_body or b"")
        key.verify(base64.b64decode(sig_b64), signed)
        return True
    except Exception as exc:
        logger.debug(f"[telephony] telnyx signature check failed: {exc}")
        return False


async def verified_form(request: Request) -> Optional[Dict[str, str]]:
    """Provider-aware webhook auth for FORM-encoded voice webhooks (both
    Twilio and Telnyx TeXML are form-encoded + Twilio-compatible params).
    Returns the parsed params if authentic, else None. Reads the raw body
    first (Telnyx signs raw bytes); Starlette caches it so .form() still
    parses."""
    raw = await request.body()
    if _provider() == "telnyx":
        ok = _telnyx_valid_signature(
            raw, request.headers.get("telnyx-signature-ed25519", ""),
            request.headers.get("telnyx-timestamp", ""))
        if not ok:
            return None
        form = {k: str(v) for k, v in dict(await request.form()).items()}
        return form
    form = {k: str(v) for k, v in dict(await request.form()).items()}
    if not _valid_signature(str(request.url), form,
                            request.headers.get("X-Twilio-Signature", "")):
        return None
    return form


def _match_sender(phone_e164: str) -> Optional[Dict[str, Any]]:
    """E.164 → contact(account)/lead (phones are stored normalized)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.account_id::text, a.owner_id,
                          COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'')||' '||
                                   COALESCE(c.last_name,'')),''), a.account_name)
                   FROM contacts c JOIN accounts a ON a.account_id=c.account_id
                   WHERE regexp_replace(COALESCE(c.phone,''),'\\D','','g')
                         = regexp_replace(%s,'\\D','','g')
                     AND COALESCE(c.is_deleted,false)=false
                     AND c.phone IS NOT NULL AND c.phone <> ''
                   ORDER BY c.created_at LIMIT 1""", (phone_e164,))
            r = cur.fetchone()
            if r:
                return {"kind": "account", "account_id": r[0], "lead_id": None,
                        "owner_id": r[1], "display": r[2]}
            cur.execute(
                """SELECT lead_id::text, owner_id,
                          COALESCE(NULLIF(TRIM(COALESCE(first_name,'')||' '||
                                   COALESCE(last_name,'')),''), company)
                   FROM leads
                   WHERE regexp_replace(COALESCE(phone,''),'\\D','','g')
                         = regexp_replace(%s,'\\D','','g')
                     AND deleted_at IS NULL AND phone IS NOT NULL AND phone <> ''
                   ORDER BY created_at LIMIT 1""", (phone_e164,))
            r = cur.fetchone()
            if r:
                return {"kind": "lead", "lead_id": r[0], "account_id": None,
                        "owner_id": r[1], "display": r[2]}
            return None
    finally:
        conn.close()


def _compose_sms_reply(sender_display: str, body: str) -> str:
    """KB-grounded, PII-safe, SMS-sized auto-reply (fallback: plain ack)."""
    ack = ("Thanks for your message — the Conscestra CRM team has it and "
           "will follow up shortly.")
    if not AUTOREPLY:
        return ack
    try:
        from app.core import knowledge, privacy
        from app.core.graph_utils import _get_llm
        kb = knowledge.rag_block("sms inquiry", body)
        resp = _get_llm(tier="lite").invoke([
            {"role": "system", "content":
                "You write SMS replies for Conscestra CRM. ONE short paragraph, "
                "under 300 characters, plain text, no links unless given one, "
                "warm and concrete. Never reveal internal data."},
            {"role": "user", "content":
                f"Customer SMS (from {sender_display}):\n{privacy.mask(body)[:400]}\n\n"
                + (f"Approved knowledge that matches:\n{kb}\n\n" if kb else "")
                + "Reply:"},
        ])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        return text[:300] if text else ack
    except Exception as exc:
        logger.warning(f"[telephony] SMS auto-reply LLM failed: {exc}")
        return ack


def _bridge_inbound_sms(sender: str, body: str,
                        via: str = "provider") -> Optional[str]:
    """Provider-agnostic core: match the sender, log the inbound activity,
    emit sms.received, and compose the auto-reply text. Returns the reply."""
    sender = normalize_phone(sender) or sender
    body = (body or "").strip()
    who = _match_sender(sender) if sender else None
    display = (who or {}).get("display") or sender

    _log_activity("call", f"Inbound SMS from {display}",
                  f"From {sender} · via {via}\n\n{body}",
                  direction="inbound",
                  lead_id=(who or {}).get("lead_id"),
                  account_id=(who or {}).get("account_id"),
                  owner_id=(who or {}).get("owner_id"))
    if who:
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT emit_event(%s,%s,%s,%s,%s,%s)",
                    ("sms.received", who["kind"],
                     who.get("account_id") or who.get("lead_id"),
                     _json.dumps({"context": {"from": sender,
                                              "body": body[:200]}}),
                     None, "telephony"))
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"[telephony] sms.received emit skipped: {exc}")
    logger.info(f"[telephony] inbound SMS from {sender} via {via} "
                f"({(who or {}).get('kind') or 'unmatched'})")
    return _compose_sms_reply(display, body)


def handle_inbound_sms(url: str, params: Dict[str, str],
                       signature: str) -> Optional[str]:
    """Twilio inbound SMS: validate HMAC then bridge. Returns the reply text
    (None = invalid signature). Telnyx inbound is handled in the endpoint
    (JSON body + Ed25519 + separate-message reply)."""
    if not _valid_signature(url, params, signature):
        return None
    return _bridge_inbound_sms(params.get("From", ""),
                               params.get("Body", ""), via="Twilio")


# ============================================================================
# Endpoints
# ============================================================================

router = APIRouter(tags=["telephony"])


@router.get("/telephony/status")
def telephony_status():
    return {"provider": _provider(), "configured": configured(),
            "from_number": _from_number() or None,
            "autosend": AUTOSEND, "autoreply": AUTOREPLY,
            "sms_max_chars": SMS_MAX_CHARS}


@router.post("/telephony/sms/send")
def telephony_send(body: Dict[str, Any]):
    """Admin send (human-initiated). Agents go through A2A sms.send."""
    return send_sms_sp(body or {})


@router.post("/telephony/call")
def telephony_call(body: Dict[str, Any]):
    return place_call(str(body.get("to") or ""), str(body.get("say") or ""),
                      lead_id=body.get("lead_id"),
                      account_id=body.get("account_id"),
                      called_by=str(body.get("called_by", "admin")))


# Public webhooks: the carrier can't send admin headers — the request
# signature (Twilio HMAC, or Telnyx Ed25519) IS the authorization.
public_router = APIRouter(tags=["telephony-public"])


@public_router.post("/telephony/sms/inbound")
async def telephony_inbound(request: Request):
    if _provider() == "telnyx":
        raw = await request.body()
        if not _telnyx_valid_signature(
                raw, request.headers.get("telnyx-signature-ed25519", ""),
                request.headers.get("telnyx-timestamp", "")):
            return Response("invalid signature", status_code=403)
        try:
            payload = (_json.loads(raw or b"{}").get("data") or {}).get("payload") or {}
        except Exception:
            payload = {}
        # Only bridge genuine inbound messages — ignore delivery receipts and
        # outbound echoes (they carry the same event stream).
        if payload.get("direction") != "inbound":
            return Response("", status_code=200)
        sender = (payload.get("from") or {}).get("phone_number", "")
        reply = _bridge_inbound_sms(sender, payload.get("text", ""),
                                    via="Telnyx")
        # Telnyx has no inline reply — send the auto-reply as a separate msg.
        if reply and AUTOSEND:
            try:
                send_sms(sender, reply, sent_by="auto-reply")
            except Exception as exc:
                logger.warning(f"[telephony] telnyx auto-reply send failed: {exc}")
        return Response("", status_code=200)

    # Twilio: form-encoded + HMAC, reply inline as TwiML.
    form = dict(await request.form())
    reply = handle_inbound_sms(str(request.url),
                               {k: str(v) for k, v in form.items()},
                               request.headers.get("X-Twilio-Signature", ""))
    if reply is None:
        return Response("invalid signature", status_code=403)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>'
                    f"<Message>{_twiml_escape(reply)}</Message></Response>",
                    media_type="text/xml")


@public_router.get("/telephony/texml/say")
def telephony_texml_say(text: str = "", t: str = ""):
    """Serve a one-shot TeXML <Say> document for outbound Telnyx calls. The
    HMAC token authorizes it (Telnyx fetches this server-side)."""
    if not _hmac.compare_digest(_say_token(text[:800]), t):
        return Response("invalid token", status_code=403)
    return Response(_say_document(text), media_type="text/xml")
