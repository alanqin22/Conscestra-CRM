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

TRIAL NOTES: a Twilio trial only reaches VERIFIED numbers and prefixes
messages with "Sent from your Twilio trial account". Rotate the auth token
after sharing it anywhere.

CONFIG (env)
  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
  SMS_AUTOSEND    0   1 = really send SMS/calls; else draft as owner tasks
  SMS_AUTOREPLY   1   inbound webhook replies with the LLM/KB answer
                      (0 = plain acknowledgement)
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, Response

from app.core.database import get_connection

logger = logging.getLogger("telephony")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _sid() -> str:
    return os.getenv("TWILIO_ACCOUNT_SID", "").strip()


def _token() -> str:
    return os.getenv("TWILIO_AUTH_TOKEN", "").strip()


def _from_number() -> str:
    return os.getenv("TWILIO_FROM_NUMBER", "").strip()


AUTOSEND = _flag("SMS_AUTOSEND", "0")
AUTOREPLY = _flag("SMS_AUTOREPLY", "1")

_API = "https://api.twilio.com/2010-04-01/Accounts"
SMS_MAX_CHARS = 320          # ~2 segments; the critic warns beyond this


def configured() -> bool:
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
    try:
        r = requests.post(
            f"{_API}/{_sid()}/Messages.json",
            data={"To": to_n, "From": _from_number(), "Body": body[:1600]},
            auth=(_sid(), _token()), timeout=20)
        payload = r.json() if r.content else {}
    except Exception as exc:
        return {"ok": False, "error": f"Twilio request failed: {exc}"}
    if r.status_code >= 300:
        return {"ok": False, "error": f"Twilio {r.status_code}: "
                                      f"{payload.get('message', r.text[:200])}"}
    _log_activity("call", f"SMS sent to {to_n}",
                  f"Sent by {sent_by} via Twilio ({payload.get('sid', '')}):\n{body}",
                  direction="outbound", lead_id=lead_id, account_id=account_id,
                  owner_id=owner_id)
    logger.info(f"[telephony] SMS → {to_n} ({payload.get('sid', '')[:10]})")
    return {"ok": True, "sent": True, "to": to_n, "sid": payload.get("sid"),
            "status": payload.get("status")}


def _twiml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;").replace('"', "&quot;")


def place_call(to: str, say_text: str, *, lead_id=None, account_id=None,
               owner_id=None, called_by: str = "agent") -> Dict[str, Any]:
    """Place a call that speaks `say_text` (inline TwiML — no public URL)."""
    to_n = normalize_phone(to)
    if not to_n:
        return {"ok": False, "error": f"unusable phone number {to!r}"}
    if not configured():
        return {"ok": False, "error": "Twilio not configured"}
    twiml = (f'<Response><Say voice="alice">{_twiml_escape(say_text[:800])}'
             f'</Say></Response>')
    if not AUTOSEND:
        _log_activity("task", f"Place call to {to_n} (draft)",
                      f"Voice message drafted by {called_by} (SMS_AUTOSEND=0):\n"
                      f"{say_text}", direction="outbound", lead_id=lead_id,
                      account_id=account_id, owner_id=owner_id, status="open")
        return {"ok": True, "called": False, "drafted": True, "to": to_n}
    import requests
    try:
        r = requests.post(
            f"{_API}/{_sid()}/Calls.json",
            data={"To": to_n, "From": _from_number(), "Twiml": twiml},
            auth=(_sid(), _token()), timeout=20)
        payload = r.json() if r.content else {}
    except Exception as exc:
        return {"ok": False, "error": f"Twilio request failed: {exc}"}
    if r.status_code >= 300:
        return {"ok": False, "error": f"Twilio {r.status_code}: "
                                      f"{payload.get('message', r.text[:200])}"}
    _log_activity("call", f"Voice call to {to_n}",
                  f"Agent call placed by {called_by} via Twilio "
                  f"({payload.get('sid', '')}). Spoken message:\n{say_text}",
                  direction="outbound", lead_id=lead_id, account_id=account_id,
                  owner_id=owner_id)
    logger.info(f"[telephony] call → {to_n} ({payload.get('sid', '')[:10]})")
    return {"ok": True, "called": True, "to": to_n, "sid": payload.get("sid"),
            "status": payload.get("status")}


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


def handle_inbound_sms(url: str, params: Dict[str, str],
                       signature: str) -> Optional[str]:
    """Bridge one inbound SMS. Returns the reply text (None = invalid sig)."""
    if not _valid_signature(url, params, signature):
        return None
    sender = normalize_phone(params.get("From", "")) or params.get("From", "")
    body = (params.get("Body") or "").strip()
    who = _match_sender(sender) if sender else None
    display = (who or {}).get("display") or sender

    _log_activity("call", f"Inbound SMS from {display}",
                  f"From {sender} · via Twilio\n\n{body}",
                  direction="inbound",
                  lead_id=(who or {}).get("lead_id"),
                  account_id=(who or {}).get("account_id"),
                  owner_id=(who or {}).get("owner_id"))
    if who:
        try:
            import json as _json
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
    logger.info(f"[telephony] inbound SMS from {sender} "
                f"({(who or {}).get('kind') or 'unmatched'})")
    return _compose_sms_reply(display, body)


# ============================================================================
# Endpoints
# ============================================================================

router = APIRouter(tags=["telephony"])


@router.get("/telephony/status")
def telephony_status():
    return {"configured": configured(), "from_number": _from_number() or None,
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


# Public webhook: Twilio can't send admin headers — the request signature
# (HMAC of the exact URL + params with the auth token) IS the authorization.
public_router = APIRouter(tags=["telephony-public"])


@public_router.post("/telephony/sms/inbound")
async def telephony_inbound(request: Request):
    form = dict(await request.form())
    reply = handle_inbound_sms(str(request.url),
                               {k: str(v) for k, v in form.items()},
                               request.headers.get("X-Twilio-Signature", ""))
    if reply is None:
        return Response("invalid signature", status_code=403)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>'
                    f"<Message>{_twiml_escape(reply)}</Message></Response>",
                    media_type="text/xml")
