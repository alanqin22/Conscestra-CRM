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

NAMING — "TwiML" here does NOT mean "Twilio only". Telnyx TeXML is deliberately
Twilio-compatible XML: the same <Say>/<Gather>/<Dial>/<Connect> verbs, the same
form-encoded callbacks, the same parameter names (CallSid, From, SpeechResult).
So helpers named *twiml* (`_twiml_escape`, `_say_document`, `_next_twiml`) build
the documents BOTH carriers consume, and they are on the live path for Telnyx
users. Do not read the name as dead Twilio code and delete it.

The differences that DO matter are per-carrier and are marked at each site:
Telnyx needs a numeric `speechTimeout` (it rejects "auto"), and bidirectional
`<Stream>` needs `bidirectionalMode`/`bidirectionalCodec`, which are Telnyx
attributes Twilio does not define. Both are gated on `_provider()`.

An XML rule bites regardless of carrier: attribute values must be escaped. A
URL with a query string carries a bare `&`, which is not legal XML, and an
unescaped one makes the whole document unparseable — the carrier rejects the
response and the call fails in a way that looks like a network problem.

TRIAL NOTES: a Twilio trial only reaches VERIFIED numbers and prefixes
messages with "Sent from your Twilio trial account". A funded Telnyx account
reaches any number. Rotate secrets after sharing them anywhere.

TIERED INBOUND REPLIES: inbound SMS is a PUBLIC channel — the carrier
signature proves the request came from the carrier, NOT who texted, and
caller ID is spoofable. So live CRM data is released only to the explicit
`SMS_OPERATOR_NUMBERS` allowlist (staff), which routes through the
in-process orchestrator (accounts, contacts, leads, opportunities, orders,
products, activities, accounting, analytics). Every other sender keeps the
KB-grounded, PII-masked customer reply that never touches the database.
Empty allowlist (the default) = nobody gets CRM data — KB for all.

CONFIG (env)
  TELEPHONY_PROVIDER   telnyx   telnyx | twilio (which carrier is live).
                                An unrecognised value is logged at ERROR and
                                falls back to the default rather than being
                                silently treated as the other carrier.
  SMS_AUTOSEND    0   1 = really send SMS/calls; else draft as owner tasks
  SMS_AUTOREPLY   1   inbound webhook replies with the LLM/KB answer
  SMS_OPERATOR_NUMBERS  ''  comma-separated E.164 staff numbers that may
                            query the LIVE CRM over SMS (blank = none)
  -- Twilio --  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
  -- Telnyx --  TELNYX_API_KEY / TELNYX_FROM_NUMBER
                TELNYX_FROM_POOL  optional comma-separated list of EVERY sender
                                     number (any mix of US + Canadian lines). When
                                     set it is the full pool the agent calls/texts
                                     from; a destination is matched to a same-country
                                     pool number, picked STABLY per destination so a
                                     customer always sees one caller ID. Unset → the
                                     pool falls back to the two legacy single vars.
                TELNYX_FROM_NUMBER_US  legacy optional 2nd (US) sender; used only to
                                     seed the pool when TELNYX_FROM_POOL is unset.
                                     Override per send with from='us'|'ca'|<E.164>
                                     in the A2A/admin call (a full E.164 must be a
                                     pool member to be reachable inbound)
                TELNYX_PUBLIC_KEY    account Ed25519 public key (webhook auth)
                TELNYX_TEXML_APP_ID  TeXML application id (outbound calls)
                TELNYX_PUBLIC_BASE   public base URL for outbound-call TeXML
                                     (falls back to APP_URL)
"""

from __future__ import annotations

import asyncio
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


_PROVIDERS = ("telnyx", "twilio")
_DEFAULT_PROVIDER = "telnyx"
_provider_warned: set = set()      # warn once per bad value, not once per call


def _provider() -> str:
    """Which carrier is live. Defaults to Telnyx, and says so when the value
    is missing or unrecognised.

    This defaulted to "twilio", which made an absent or misspelled
    TELEPHONY_PROVIDER fail in three silent ways at once on a Telnyx account:
    webhook signatures verified with Twilio's HMAC (so every carrier request
    401s), no sender number (the Twilio branch reads TWILIO_FROM_NUMBER), and
    speechTimeout="auto", which Telnyx rejects so the Gather never completes.
    None of those announce themselves — the phone line simply stops working.

    A default cannot prevent a typo, so an unrecognised value is logged at
    ERROR rather than quietly treated as Twilio. _provider() is called on
    every TeXML build, so the warning is emitted once per distinct value."""
    raw = os.getenv("TELEPHONY_PROVIDER", "").strip().lower()
    if not raw:
        return _DEFAULT_PROVIDER
    if raw not in _PROVIDERS:
        if raw not in _provider_warned:
            _provider_warned.add(raw)
            logger.error(
                "[telephony] TELEPHONY_PROVIDER=%r is not one of %s — using "
                "%r. Fix the variable: a wrong carrier means webhook "
                "signatures, sender numbers and speech timeouts are all "
                "chosen for the wrong provider.",
                raw, "/".join(_PROVIDERS), _DEFAULT_PROVIDER)
        return _DEFAULT_PROVIDER
    return raw


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


# Canadian NANP area codes. The US and Canada share country code +1, so the
# destination country can only be told apart by its area code (NPA). Anything
# +1 that is NOT in this set (and not toll-free) is treated as US.
_CA_AREA_CODES = frozenset({
    "204", "226", "236", "249", "250", "263", "289", "306", "343", "354",
    "365", "367", "368", "382", "387", "403", "416", "418", "428", "431",
    "437", "438", "450", "468", "474", "506", "514", "519", "548", "579",
    "581", "584", "587", "600", "604", "613", "639", "647", "672", "683",
    "705", "709", "742", "753", "778", "780", "782", "807", "819", "825",
    "867", "873", "879", "902", "905",
})
# Toll-free NANP NPAs are shared US/Canada — never used to pick the US line.
_TOLLFREE_AREA_CODES = frozenset({
    "800", "833", "844", "855", "866", "877", "888",
})


def _npa(phone: Optional[str]) -> Optional[str]:
    """The 3-digit NANP area code of an E.164/phone string, else None."""
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11 and d[0] == "1":
        return d[1:4]
    if len(d) == 10:
        return d[:3]
    return None


def _is_us_number(e164: Optional[str]) -> bool:
    """True for a +1 number whose NANP area code is US — i.e. not Canadian and
    not toll-free (shared). Non-NANP / unrecognized numbers are treated as
    not-US, so they group with the Canadian/default lines."""
    npa = _npa(e164)
    return bool(npa and npa not in _CA_AREA_CODES
                and npa not in _TOLLFREE_AREA_CODES)


def _from_pool() -> list:
    """Every configured Telnyx sender number — E.164, de-duped, order preserved.

    TELNYX_FROM_POOL (comma-separated) is the full pool when set; otherwise the
    pool is seeded from the two legacy single-line vars TELNYX_FROM_NUMBER +
    TELNYX_FROM_NUMBER_US, so pre-pool configs keep working unchanged."""
    raw = os.getenv("TELNYX_FROM_POOL", "").strip()
    if raw:
        candidates = [normalize_phone(p) for p in raw.split(",")]
    else:
        candidates = [os.getenv("TELNYX_FROM_NUMBER", "").strip(),
                      os.getenv("TELNYX_FROM_NUMBER_US", "").strip()]
    out: list = []
    for n in candidates:
        n = (n or "").strip()
        if n and n not in out:
            out.append(n)
    return out


def _pick_stable(numbers: list, dest: Optional[str]) -> str:
    """Deterministically pick one number from `numbers` for a destination, so a
    given customer always sees the SAME caller ID (their phone threads the calls
    and texts together, and it reads as one consistent line) while load still
    spreads across the pool. Sorted first so the choice is independent of how
    the pool happens to be ordered. Empty destination → the first entry."""
    pool = sorted(n for n in numbers if n)
    if not pool:
        return ""
    key = re.sub(r"\D", "", dest or "")
    if not key:
        return pool[0]
    idx = int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(pool)
    return pool[idx]


def _from_number(dest: Optional[str] = None,
                 override: Optional[str] = None) -> str:
    """The originating sender number for the current provider.

    Twilio is single-number and ignores dest/override. Telnyx draws from a POOL
    of sender numbers (TELNYX_FROM_POOL, or the two legacy single-line vars) and
    chooses one as:
      1. explicit override — a full E.164, or an alias 'us' / 'ca' / 'default';
      2. automatic by destination country — a +1 US destination originates from
         a US pool number, Canadian/other/international from a non-US one, picked
         STABLY per destination so a customer always sees the same caller ID;
      3. the default (TELNYX_FROM_NUMBER, else the pool's first entry) when the
         destination is unknown.
    Called with no args (configured()/status) it returns the default."""
    if _provider() != "telnyx":
        return os.getenv("TWILIO_FROM_NUMBER", "").strip()
    pool = _from_pool()
    default = (os.getenv("TELNYX_FROM_NUMBER", "").strip()
               or (pool[0] if pool else ""))
    # 1. explicit override wins
    if override:
        low = override.strip().lower()
        if low in ("us", "usa"):
            us = [n for n in pool if _is_us_number(n)]
            return _pick_stable(us, dest) or default
        if low in ("ca", "can", "canada", "default"):
            ca = [n for n in pool if not _is_us_number(n)]
            return _pick_stable(ca, dest) or default
        return normalize_phone(override) or default
    if not pool:
        return default
    # 2. automatic by destination country — match each line's country to the
    #    destination's, then pick stably within that country's numbers.
    if dest:
        want_us = _is_us_number(dest)
        cands = [n for n in pool if _is_us_number(n) == want_us]
        return _pick_stable(cands or pool, dest)
    # 3. destination unknown → default
    return default


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
             owner_id=None, sent_by: str = "agent",
             transactional: bool = False,
             from_number: Optional[str] = None) -> Dict[str, Any]:
    """Send (or draft) one SMS. Trial accounts only reach verified numbers.

    transactional=True bypasses the AUTOSEND draft gate: AUTOSEND exists to
    hold back agent-INITIATED outreach for review, but a caller-requested
    security code (voice OTP) is useless as a draft — the caller is on the
    line waiting for it. Transactional bodies carry secrets, so the activity
    log records THAT a message went out, never its content."""
    to_n = normalize_phone(to)
    if not to_n:
        return {"ok": False, "error": f"unusable phone number {to!r}"}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "empty message body"}
    # Outbound guard (guardrail 3) — transactional bodies (OTP codes) are
    # code-built and screened too; the checks cost microseconds.
    try:
        from app.core.outbound_guard import screen
        g = screen(body, "sms")
        if not g["ok"]:
            return {"ok": False, "blocked": True,
                    "error": "blocked by outbound guard: "
                             + "; ".join(g["violations"])}
    except ImportError:
        pass
    if not configured():
        return {"ok": False, "error": "Twilio not configured "
                                      "(TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM_NUMBER)"}

    if not AUTOSEND and not transactional:
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
                json={"from": _from_number(to_n, from_number), "to": to_n,
                      "text": body[:1600]},
                timeout=20)
        else:
            r = requests.post(
                f"{_API}/{_sid()}/Messages.json",
                data={"To": to_n, "From": _from_number(to_n, from_number),
                      "Body": body[:1600]},
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
                  f"Sent by {sent_by} via {prov} ({sid or ''}):\n"
                  + ("(transactional — body withheld from the log)"
                     if transactional else body),
                  direction="outbound", lead_id=lead_id, account_id=account_id,
                  owner_id=owner_id)
    logger.info(f"[telephony] SMS → {to_n} via {prov} ({str(sid)[:12]})")
    return {"ok": True, "sent": True, "to": to_n, "sid": sid,
            "status": (payload.get("data") or {}).get("status") if prov == "telnyx"
                      else payload.get("status")}


def transfer_call(call_control_id: str, to: str, from_number: str = "",
                  timeout_secs: int = 25) -> Dict[str, Any]:
    """Transfer a LIVE call to another number via Telnyx Call Control.

    The <Dial> verb is a TeXML answer to a webhook, and a media-stream call has
    no webhook left to answer — the carrier is holding a WebSocket open, not
    waiting for XML. So the streaming transport cannot transfer the way the
    Gather transport does, and without this a caller on the streaming path who
    asked for a person simply could not be given one.

    `call_control_id` arrives in the stream's `start` event. Returns
    {"ok": bool, ...}; never raises, because failing to transfer must degrade
    to taking a message rather than dropping the call."""
    key = _telnyx_key()
    if not key:
        return {"ok": False, "error": "TELNYX_API_KEY not set"}
    if not call_control_id:
        return {"ok": False, "error": "no call_control_id for this call"}
    to_n = normalize_phone(to) or to
    body: Dict[str, Any] = {"to": to_n, "timeout_secs": int(timeout_secs)}
    frm = normalize_phone(from_number) if from_number else ""
    if frm:
        body["from"] = frm
    try:
        import requests
        r = requests.post(
            f"{_TELNYX_API}/calls/{call_control_id}/actions/transfer",
            headers={"Authorization": f"Bearer {key}"},
            json=body, timeout=15)
        if r.status_code >= 300:
            logger.warning(f"[telephony] transfer failed {r.status_code}: "
                           f"{r.text[:200]}")
            return {"ok": False, "error": f"HTTP {r.status_code}",
                    "detail": r.text[:300]}
        logger.info(f"[telephony] transferred call to {to_n}")
        return {"ok": True, "to": to_n}
    except Exception as exc:
        logger.warning(f"[telephony] transfer error: {exc}")
        return {"ok": False, "error": str(exc)[:200]}


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
               owner_id=None, called_by: str = "agent",
               from_number: Optional[str] = None) -> Dict[str, Any]:
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
                json={"To": to_n, "From": _from_number(to_n, from_number),
                      "Url": _say_url(say_text)}, timeout=20)
        else:
            twiml = (f'<Response><Say voice="alice">'
                     f'{_twiml_escape(say_text[:800])}</Say></Response>')
            r = requests.post(
                f"{_API}/{_sid()}/Calls.json",
                data={"To": to_n, "From": _from_number(to_n, from_number),
                      "Twiml": twiml},
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
                    sent_by=str(p.get("sent_by", "a2a")),
                    from_number=(p.get("from") or p.get("from_number") or None))


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


def _operator_numbers() -> set:
    """E.164 numbers allowed to query the LIVE CRM over SMS.

    Read per call (not cached at import) so the allowlist can be corrected
    with a restart-free env reload, and so an empty/blank value can never
    silently widen to 'everyone'."""
    out = set()
    for part in os.getenv("SMS_OPERATOR_NUMBERS", "").split(","):
        n = normalize_phone(part.strip())
        if n:
            out.add(n)
    return out


def _is_operator(sender_e164: Optional[str]) -> bool:
    """Fail-closed: unknown/blank sender is never an operator."""
    return bool(sender_e164) and sender_e164 in _operator_numbers()


# Bare acknowledgements carry no routable keyword, so the orchestrator's
# keyword router would drop them on its catch-all agent (activities) — which
# then invents a write from the session's leftover context. Follow-ups stay
# with whichever agent the sender was last talking to instead.
_SMS_FOLLOWUP_RE = re.compile(
    r"^\s*(y|yes|yep|yeah|yup|ok|okay|sure|please|thanks|thank\s+you|"
    r"do\s+it|go\s+ahead|confirm|correct|right)\b[\s,.!]*(please|thanks)?"
    r"[\s,.!?]*$", re.IGNORECASE)

# sender E.164 → agent endpoint last used, so a follow-up has somewhere to go.
_LAST_AGENT: Dict[str, str] = {}


def _route_sms(sender_e164: str, body: str) -> str:
    """Intent-route (LLM with keyword fallback — Orchestrator v2), but keep
    bare follow-ups on the sender's last agent. Blocking; call off-loop."""
    from app.core.intent_router import route
    if _SMS_FOLLOWUP_RE.match(body or "") and sender_e164 in _LAST_AGENT:
        return _LAST_AGENT[sender_e164]
    path = route(body).endpoint
    _LAST_AGENT[sender_e164] = path
    return path


async def _compose_operator_reply(sender_e164: str, body: str) -> str:
    """OPERATOR TIER — answer from the live CRM, READ-ONLY.

    Routes the question through the orchestrator's keyword router to the
    owning agent in-process (ASGI, no network hop, no admin token needed),
    then condenses that agent's markdown answer down to SMS size. Internal
    data is intentionally NOT masked here — this tier is staff-only.

    Writes are refused: caller ID is spoofable and a misrouted word ("yes")
    is enough for an agent's NL router to resolve a create, so the session is
    marked a read-only channel. That runs every SP the agent reaches inside a
    PostgreSQL read-only transaction — enforcement the agent cannot talk its
    way past, and which does not rely on the write-mode blocklist."""
    from app.agents.orchestrator.router import _call_agent
    from app.core.write_guard import WritePermissionError, set_readonly_channel

    set_readonly_channel("sms")
    path = await asyncio.to_thread(_route_sms, sender_e164, body)
    logger.info(f"[telephony] operator SMS from {sender_e164} → {path}")
    try:
        data = await _call_agent(path, body, f"sms-{sender_e164}")
    except WritePermissionError:
        return ("I can look things up by text, but I can't create, edit or "
                "delete records here — use the web app for changes.")

    # A write blocked deeper in the stack comes back as the router's 403 body
    # rather than an exception, and UI-form modes ('show_lead_form') are a
    # browser affordance — over SMS there is no form to open. Both mean the
    # same thing to a texter: this channel reads, it doesn't write.
    mode = str(data.get("mode") or (data.get("rawParams") or {}).get("mode") or "")
    if mode.startswith("show_") and mode.endswith("_form"):
        return ("I can look things up by text, but creating or editing records "
                "needs the web app.")

    raw = str(data.get("output") or "").strip()
    if not raw:
        detail = str(data.get("detail") or "").strip()
        if "read-only" in detail.lower():
            return ("I can look things up by text, but I can't create, edit or "
                    "delete records here — use the web app for changes.")
        return "No answer came back from the CRM for that — try rephrasing?"
    # Never text a raw stored-procedure stack trace.
    if raw.lstrip().startswith("### ERROR") or "Validation failed for sp_" in raw:
        logger.warning(f"[telephony] agent error for {sender_e164}: {raw[:200]}")
        return ("That didn't come back cleanly — try asking with a name, e.g. "
                "'show the latest order for David Chen'.")
    try:
        from app.core.graph_utils import _get_llm
        resp = await asyncio.to_thread(_get_llm(tier="lite").invoke, [
            {"role": "system", "content":
                "Condense this CRM answer into ONE SMS for an authorized "
                "internal operator. Under 300 characters, plain text — no "
                "markdown, tables or links. Keep names, amounts, dates and "
                "statuses EXACT; never invent a detail that isn't there. "
                "Lead with the answer itself."},
            {"role": "user", "content": f"Question: {body[:200]}\n\n"
                                        f"CRM answer:\n{raw[:3000]}\n\nSMS:"},
        ])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        return text[:300] if text else raw[:300]
    except Exception as exc:
        # The data is already in hand — degrade to a truncated raw answer
        # rather than losing the reply to a wording failure.
        logger.warning(f"[telephony] operator condense failed: {exc}")
        return raw[:300]


async def _compose_sms_reply(sender_display: str, body: str,
                             sender_e164: Optional[str] = None) -> str:
    """KB-grounded, PII-safe, SMS-sized auto-reply (fallback: plain ack).

    Staff numbers on the SMS_OPERATOR_NUMBERS allowlist get the live-CRM
    tier instead; every other sender stays on the KB-only path below."""
    ack = ("Thanks for your message — the Conscestra CRM team has it and "
           "will follow up shortly.")
    if not AUTOREPLY:
        return ack
    if _is_operator(sender_e164):
        try:
            return await _compose_operator_reply(sender_e164, body)
        except Exception as exc:
            logger.warning(f"[telephony] operator reply failed: {exc}")
            return ("I couldn't reach the CRM just now — please try again "
                    "in a moment.")
    try:
        from app.core import knowledge, privacy
        from app.core.graph_utils import _get_llm
        # Empty subject (channel labels pollute matching); a KB miss is
        # logged as an 'sms' gap for the nightly gap miner.
        kb = await asyncio.to_thread(knowledge.rag_block, "", body, "sms")
        resp = await asyncio.to_thread(_get_llm(tier="lite").invoke, [
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


async def _bridge_inbound_sms(sender: str, body: str,
                              via: str = "provider") -> Optional[str]:
    """Provider-agnostic core: match the sender, log the inbound activity,
    emit sms.received, and compose the auto-reply text. Returns the reply."""
    sender = normalize_phone(sender) or sender
    body = (body or "").strip()

    # Unified Communication Layer: thread this inbound SMS into the sender's ONE
    # cross-channel conversation (best-effort; the adapter swallows any failure,
    # so this never affects the SMS reply). Threads matched AND unmatched senders.
    try:
        from app.core import channel_adapters
        channel_adapters.capture_sms(sender, body)
    except Exception:
        pass

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
                f"({(who or {}).get('kind') or 'unmatched'}"
                f"{', operator' if _is_operator(sender) else ''})")
    reply = await _compose_sms_reply(display, body, sender_e164=sender)
    # Unified customer memory: remember the exchange for a MATCHED sender
    # (customers only — operator traffic is staff querying the CRM, not a
    # customer conversation). Recall never serves unverified SMS; writing is
    # safe because a spoofed sender gains nothing by planting a question.
    if who and not _is_operator(sender):
        try:
            from app.core import customer_memory
            customer_memory.remember_later(
                who["kind"], who.get("account_id") or who.get("lead_id"),
                "sms", None, f"customer: {body}\nagent: {reply}")
        except Exception as exc:
            logger.debug(f"[telephony] memory write skipped: {exc}")
    return reply


async def handle_inbound_sms(url: str, params: Dict[str, str],
                             signature: str) -> Optional[str]:
    """Twilio inbound SMS: validate HMAC then bridge. Returns the reply text
    (None = invalid signature). Telnyx inbound is handled in the endpoint
    (JSON body + Ed25519 + separate-message reply)."""
    if not _valid_signature(url, params, signature):
        return None
    return await _bridge_inbound_sms(params.get("From", ""),
                                     params.get("Body", ""), via="Twilio")


# ============================================================================
# Endpoints
# ============================================================================

router = APIRouter(tags=["telephony"])


@router.get("/telephony/status")
def telephony_status():
    pool = _from_pool() if _provider() == "telnyx" else []
    return {"provider": _provider(), "configured": configured(),
            "from_number": _from_number() or None,
            "from_pool": pool,
            "from_pool_us": [n for n in pool if _is_us_number(n)],
            "from_pool_ca": [n for n in pool if not _is_us_number(n)],
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
                      called_by=str(body.get("called_by", "admin")),
                      from_number=(body.get("from") or body.get("from_number")
                                   or None))


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
        reply = await _bridge_inbound_sms(sender, payload.get("text", ""),
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
    reply = await handle_inbound_sms(str(request.url),
                                     {k: str(v) for k, v in form.items()},
                                     request.headers.get("X-Twilio-Signature", ""))
    if reply is None:
        return Response("invalid signature", status_code=403)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>'
                    f"<Message>{_twiml_escape(reply)}</Message></Response>",
                    media_type="text/xml")


@public_router.api_route("/telephony/texml/say", methods=["GET", "POST"])
def telephony_texml_say(text: str = "", t: str = ""):
    """Serve a one-shot TeXML <Say> document for outbound Telnyx calls. The
    HMAC token authorizes it (Telnyx fetches this server-side). Accepts both
    GET and POST: Telnyx fetches this URL with whatever the TeXML app's Voice
    Method is set to (POST by default), while text/t stay in the query string."""
    if not _hmac.compare_digest(_say_token(text[:800]), t):
        return Response("invalid token", status_code=403)
    return Response(_say_document(text), media_type="text/xml")
