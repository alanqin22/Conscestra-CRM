"""
CASL consent & unsubscribe — suppression list + signed unsubscribe links +
the compliance footer for commercial outbound email.

Scope (what counts as commercial):
  • Email-agent compose / template sends (mode=send_email / send_template).
  • Any future marketing/outreach sends.
  Transactional messages are EXEMPT and unchanged: order confirmations,
  dunning (enforcing a right/obligation), auth verification, password reset,
  auto-replies to inbound mail, internal executive briefings.

Design:
  • email_suppression table (sql/consent_casl.sql) — global opt-out list,
    lazily created so the feature works before the migration runs.
  • Unsubscribe links are stateless: HMAC-SHA256(email, UNSUBSCRIBE_SECRET),
    so no token table and links can't be forged for other addresses.
  • GET /email/unsubscribe is PUBLIC (registered un-gated in app/main.py) —
    recipients must be able to opt out without a login.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.database import ensure_table, get_connection

logger = logging.getLogger("consent")


def _secret() -> bytes:
    """The key behind every unsubscribe link.

    The last resort used to be the literal string 'conscestra-unsubscribe-default'.
    That is published in source, so anyone could compute a valid token for any
    address and opt arbitrary customers out of mail they had asked for — and the
    only signal was a warning line in a log nobody reads at 3am.

    A guessable key is not a weaker key, it is no key. Raising means commercial
    mail stops until it is configured, which is also the CASL-correct outcome:
    a message you cannot offer a working unsubscribe for is one you may not send.
    ADMIN_API_TOKEN is kept as a fallback because it is a real secret, but it is
    reported loudly — sharing one secret across two purposes means rotating
    either one breaks the other.
    """
    s = (os.getenv("UNSUBSCRIBE_SECRET", "") or "").strip()
    if s:
        return s.encode()
    s = (os.getenv("ADMIN_API_TOKEN", "") or "").strip()
    if s:
        logger.warning("[consent] UNSUBSCRIBE_SECRET unset — falling back to "
                       "ADMIN_API_TOKEN. Set a dedicated secret: rotating the "
                       "admin token would invalidate every live unsubscribe link.")
        return s.encode()
    raise RuntimeError(
        "UNSUBSCRIBE_SECRET is not configured and there is no ADMIN_API_TOKEN to "
        "fall back to. Unsubscribe links would be signed with a key published in "
        "this repository, letting anyone forge an opt-out for any address. "
        "Commercial email is refused until UNSUBSCRIBE_SECRET is set.")


def _app_url() -> str:
    return (os.getenv("APP_URL", "") or "http://localhost:8000").rstrip("/")


# Company identity comes from the company_profile table (edited on
# executives-mgmt.html). Cached for 5 minutes; env var is the fallback for
# fresh databases where the profile row doesn't exist yet.
_profile_cache: Dict[str, Any] = {"at": 0.0, "data": None}


def _company_profile() -> Dict[str, str]:
    import time
    now = time.time()
    if _profile_cache["data"] and now - _profile_cache["at"] < 300:
        return _profile_cache["data"]
    data = None
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT company_name, mailing_address, contact_email "
                            "FROM company_profile WHERE profile_id=1")
                r = cur.fetchone()
                if r and (r[1] or "").strip():
                    data = {"name": r[0], "address": r[1], "email": r[2]}
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[consent] company_profile read failed, using env fallback: {exc}")
    if not data:
        data = {"name": "Conscestra CRM",
                "address": (os.getenv("BUSINESS_MAILING_ADDRESS", "") or
                            "Conscestra CRM · agentorc.ca").strip(),
                "email": "info@agentorc.ca"}
    _profile_cache.update(at=now, data=data)
    return data


def _mailing_address() -> str:
    return _company_profile()["address"]


# ── Suppression list ────────────────────────────────────────────────────────

def _ensure_table(cur) -> None:
    """Create the suppression list on first use.

    Wrapped because a non-owner role (crm_app) is refused CREATE on the schema
    even when the table already exists, and the failed statement would poison
    the transaction. `is_suppressed` fails OPEN, so that would have silently
    reported every unsubscribed address as mailable — a CASL breach reached
    through a permission error. See database.ensure_table.
    """
    ensure_table(cur, "public.email_suppression",
        "CREATE TABLE IF NOT EXISTS email_suppression ("
        " email TEXT PRIMARY KEY,"
        " reason TEXT NOT NULL DEFAULT 'unsubscribed',"
        " source TEXT NOT NULL DEFAULT 'unsubscribe_link',"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now())")





# ── channel-aware consent (Axis 6 V1) ───────────────────────────────────────
# Consent used to be email-shaped: one table keyed on an address, consulted by
# one caller. SMS, WhatsApp and voice arrived afterwards and inherited nothing,
# because consent modelled PER CHANNEL always lags the newest channel. This is
# the single policy every channel asks.
#
# STATE MACHINE — UNKNOWN is the absence of a row, never a stored value:
#
#        no row ── opt_in ──► opted_in ── STOP/ARRÊT ──► opted_out
#       (UNKNOWN)                ▲                          │
#            │                   └──── START/UNSTOP ────────┘
#            └──── STOP/ARRÊT ──────────────────────────────►
#
# WHAT UNKNOWN MEANS IS A LEGAL POSTURE, NOT AN ENGINEERING DEFAULT, so it is
# configuration — per channel, because the honest answer differs per channel:
#
#   email  PERMISSIVE. This is the behaviour that exists today and is lawful
#          today: a suppression list, where absence means mailable. Applying
#          strict retroactively would block every commercial email in the
#          system on deploy, since no opt-IN records exist. Changing that is a
#          migration, not a flag flip.
#   sms /
#   whatsapp /
#   voice  STRICT by default. Nothing has ever been recorded for these
#          channels, so strict costs nothing today and is the safe side of a
#          CASL question. Flip with CONSENT_UNKNOWN_PERMISSIVE=sms,voice.
CHANNELS = ("sms", "email", "whatsapp", "voice")
_PHONE_CHANNELS = ("sms", "whatsapp", "voice")

UNKNOWN, OPTED_IN, OPTED_OUT = "unknown", "opted_in", "opted_out"

_PERMISSIVE_DEFAULT = "email"


def _permissive_channels() -> set:
    raw = os.getenv("CONSENT_UNKNOWN_PERMISSIVE", _PERMISSIVE_DEFAULT)
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def normalize_identifier(channel: str, identifier: str) -> str:
    """The SAME normalisation on write and on read.

    A consent row that cannot be found because the number was stored as
    +1 416 555 0123 and looked up as 4165550123 is worse than no row: it reads
    as consent that was never given.
    """
    v = (identifier or "").strip()
    if not v:
        return ""
    if channel == "email":
        return v.lower()
    try:
        from app.core.telephony import normalize_phone
        return normalize_phone(v) or v
    except Exception:                                       # noqa: BLE001
        return v


def state(channel: str, identifier: str) -> str:
    """opted_in | opted_out | unknown. Fails to UNKNOWN on a database error —
    `allows()` then applies the channel's policy, which for a strict channel
    means refusing to send. A consent lookup that fails open is how an
    opted-out person receives a message."""
    ident = normalize_identifier(channel, identifier)
    if not ident:
        return UNKNOWN
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM consent_state "
                            "WHERE channel=%s AND identifier=%s",
                            (channel, ident))
                r = cur.fetchone()
                return r[0] if r else UNKNOWN
        finally:
            conn.commit()
            conn.close()
    except Exception as exc:                                # noqa: BLE001
        logger.error(f"[consent] state({channel}, {ident}) failed: {exc}")
        return UNKNOWN


def allows(channel: str, identifier: str, commercial: bool = True) -> bool:
    """THE enforcement predicate. Every outbound chokepoint asks this one.

    Transactional messages (OTP, order confirmations, an answer to a question
    the person just asked) are not commercial electronic messages and are not
    gated — the same carve-out send_email already makes. OPTED_OUT still blocks
    commercial traffic regardless of channel policy; that is the whole point of
    the record.
    """
    if channel not in CHANNELS:
        logger.error(f"[consent] unknown channel {channel!r} — refusing")
        return False
    if not commercial:
        return True
    st = state(channel, identifier)
    if st == OPTED_OUT:
        return False
    if st == OPTED_IN:
        return True
    return channel in _permissive_channels()        # UNKNOWN


def record(channel: str, identifier: str, new_state: str,
           reason: str = "", source: str = "unknown",
           actor: str = "system") -> bool:
    """Set consent and append the evidence row. Both, or neither."""
    if channel not in CHANNELS or new_state not in (OPTED_IN, OPTED_OUT):
        return False
    ident = normalize_identifier(channel, identifier)
    if not ident:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO consent_state "
                "  (channel, identifier, state, reason, source) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (channel, identifier) DO UPDATE SET "
                "  state=EXCLUDED.state, reason=EXCLUDED.reason, "
                "  source=EXCLUDED.source, updated_at=now()",
                (channel, ident, new_state, reason or None, source))
            cur.execute(
                "INSERT INTO consent_log "
                "  (channel, identifier, state, reason, source, actor) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (channel, ident, new_state, reason or None, source, actor))
        conn.commit()
        logger.info(f"[consent] {channel}:{ident} -> {new_state} "
                    f"({source})")
        return True
    except Exception as exc:                                # noqa: BLE001
        conn.rollback()
        logger.error(f"[consent] record({channel},{ident}) failed: {exc}")
        return False
    finally:
        conn.close()


def withdraw_all(identifiers: Any, reason: str = "withdraw-all",
                 source: str = "request", actor: str = "system") -> Dict[str, Any]:
    """One person, every channel. The global escape hatch that per-channel
    consent needs in order to be usable: a person who says "stop contacting me"
    should not have to say it four times.

    Each identifier is applied to the channels it can actually address — an
    email address is not a voice channel — so this never writes a row that
    could never be matched.
    """
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    out: Dict[str, Any] = {"opted_out": [], "skipped": []}
    for raw in identifiers or []:
        v = (raw or "").strip()
        if not v:
            continue
        targets = ("email",) if "@" in v else _PHONE_CHANNELS
        for ch in targets:
            if record(ch, v, OPTED_OUT, reason, source, actor):
                out["opted_out"].append(f"{ch}:{normalize_identifier(ch, v)}")
            else:
                out["skipped"].append(f"{ch}:{v}")
    # Keep the legacy email list in step, so the old reader and the new one can
    # never disagree about the same address.
    for raw in identifiers or []:
        if "@" in (raw or ""):
            try:
                suppress(raw, reason=reason, source=source)
            except Exception:                               # noqa: BLE001
                pass
    return out


# ── inbound opt-out keywords ────────────────────────────────────────────────
# Recognised BEFORE the message reaches the assistant. Previously "STOP" was
# treated as a question: it went to the KB-grounded responder, which composed a
# reply and — with AUTOSEND on — sent another SMS to someone who had just asked
# to stop.
#
# French matters here, not as politeness but because the product answers in
# French: ARRÊT is the opt-out word a French-speaking recipient will send, and
# it arrives both accented and unaccented depending on the handset.
_STOP_WORDS = {
    "stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout",
    "opt-out", "arret", "arrêt", "arrête", "arrete", "desabonnement",
    "désabonnement", "desabonner", "se desabonner",
    "baja", "cancelar", "parar",                 # es
    "退订", "取消订阅",                            # zh
}
_START_WORDS = {"start", "unstop", "yes", "subscribe", "optin", "opt-in",
                "demarrer", "démarrer", "oui", "alta", "si", "订阅"}


def classify_inbound(body: str) -> Optional[str]:
    """OPTED_OUT / OPTED_IN for a recognised keyword, else None.

    Deliberately strict: the WHOLE message must be the keyword (after
    stripping punctuation and case). "stop sending me invoices at 3am" is a
    complaint that a human should read, not an opt-out — and silently
    unsubscribing someone who asked a question is its own failure.
    """
    t = (body or "").strip().lower()
    t = t.strip(".!?,;:'\"()[]").strip()
    if not t:
        return None
    if t in _STOP_WORDS:
        return OPTED_OUT
    if t in _START_WORDS:
        return OPTED_IN
    return None


def is_suppressed(email: str) -> bool:
    """True when the address opted out. Fails OPEN on DB errors for
    transactional continuity — commercial callers log the failure.

    Now reads BOTH stores: the legacy email_suppression list and the
    channel-aware consent_state. Either saying "out" means out, so an address
    suppressed through the new path is honoured by the old reader and vice
    versa — the two cannot drift into disagreeing about the same person.
    """
    if state("email", email) == OPTED_OUT:
        return True
    e = (email or "").strip().lower()
    if not e:
        return False
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute("SELECT 1 FROM email_suppression WHERE email=%s", (e,))
                return cur.fetchone() is not None
        finally:
            conn.commit()
            conn.close()
    except Exception as exc:
        logger.error(f"[consent] is_suppressed({e}) failed: {exc}")
        return False


def suppress(email: str, reason: str = "unsubscribed",
             source: str = "unsubscribe_link") -> bool:
    """Unsubscribe an address. Writes BOTH stores.

    `consent_state` is now the system of record — it is what `allows()` reads
    for every channel. The legacy `email_suppression` row is still written
    because it is declared in dsar.DIRECT and read by the HMAC unsubscribe
    page; dropping it is a separate change. Writing one and not the other is
    how the two would come to disagree about the same person, so this writes
    both or reports failure.
    """
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return False
    record("email", e, OPTED_OUT, reason=reason, source=source,
           actor="unsubscribe")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(
                "INSERT INTO email_suppression (email, reason, source) "
                "VALUES (%s,%s,%s) ON CONFLICT (email) DO NOTHING", (e, reason, source))
        conn.commit()
        logger.info(f"[consent] suppressed {e} ({reason} via {source})")
        return True
    finally:
        conn.close()


def unsuppress(email: str, reason: str = "resubscribed",
               source: str = "admin") -> bool:
    """Re-subscribe. Both stores again, in the opposite direction — without
    this, an address could be opted back in on one store and stay blocked by
    the other, which reads to the operator as the feature being broken."""
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return False
    record("email", e, OPTED_IN, reason=reason, source=source, actor="admin")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("DELETE FROM email_suppression WHERE email=%s", (e,))
        conn.commit()
        logger.info(f"[consent] unsuppressed {e} ({reason} via {source})")
        return True
    finally:
        conn.close()


# ── Signed unsubscribe links ────────────────────────────────────────────────

def unsubscribe_token(email: str) -> str:
    e = (email or "").strip().lower().encode()
    return hmac.new(_secret(), e, hashlib.sha256).hexdigest()[:32]


def unsubscribe_url(email: str) -> str:
    e = (email or "").strip().lower()
    eb = base64.urlsafe_b64encode(e.encode()).decode().rstrip("=")
    return f"{_app_url()}/email/unsubscribe?e={eb}&t={unsubscribe_token(e)}"


def _decode_email(eb: str) -> str:
    pad = "=" * (-len(eb) % 4)
    return base64.urlsafe_b64decode((eb + pad).encode()).decode()


# ── CASL footer (sender identification + unsubscribe) ───────────────────────

def casl_footer_html(recipient: str) -> str:
    url = unsubscribe_url(recipient)
    p = _company_profile()
    return (
        '<div style="margin-top:24px;padding-top:12px;border-top:1px solid #e5e7eb;'
        'font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#8a97a5;line-height:1.6;">'
        f'You are receiving this email from {p["name"]} ({p["address"]}), '
        f'{p["email"]}.<br>'
        f'<a href="{url}" style="color:#6b7f95;">Unsubscribe</a> from commercial emails at any time.'
        '</div>')


def casl_footer_text(recipient: str) -> str:
    p = _company_profile()
    return ("\n\n--\n"
            f"You are receiving this email from {p['name']} ({p['address']}), "
            f"{p['email']}.\n"
            f"Unsubscribe: {unsubscribe_url(recipient)}\n")


def guard_outbound(to: str, body_html: str, body_text: str
                   ) -> Tuple[bool, str, str]:
    """For COMMERCIAL sends: (allowed, html_with_footer, text_with_footer).
    allowed=False when the recipient has unsubscribed."""
    if is_suppressed(to):
        logger.info(f"[consent] blocked commercial email to unsubscribed {to}")
        return False, body_html, body_text
    try:
        return True, (body_html or "") + casl_footer_html(to), \
               (body_text or "") + casl_footer_text(to)
    except RuntimeError as exc:
        # No usable signing secret. Refusing the send is the compliant outcome —
        # a commercial message without a working unsubscribe must not go out —
        # and it is reported as a blocked send rather than a 500 from whichever
        # agent happened to call us.
        logger.error(f"[consent] commercial send refused, unsubscribe links "
                     f"cannot be signed: {exc}")
        return False, body_html, body_text


# ── Public unsubscribe endpoint ─────────────────────────────────────────────

router = APIRouter(tags=["consent"])

_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#f4f6f9;margin:0;padding:48px 16px;">
<div style="max-width:480px;margin:0 auto;background:#fff;border:1px solid #e1e6ef;border-radius:10px;padding:32px;text-align:center;">
<div style="font-size:40px;">{icon}</div>
<h2 style="color:#15233f;margin:12px 0 8px;">{title}</h2>
<p style="color:#6b7f95;font-size:14px;line-height:1.6;">{body}</p>
</div></body></html>"""


@router.get("/email/unsubscribe", response_class=HTMLResponse)
def email_unsubscribe(e: str = "", t: str = ""):
    """Public one-click unsubscribe. Validates the HMAC token, adds the address
    to the suppression list, and confirms — no login, no extra steps (CASL
    requires opt-out to work without further action by the recipient)."""
    try:
        email = _decode_email(e)
    except Exception:
        return HTMLResponse(_PAGE.format(icon="⚠️", title="Invalid link",
            body="This unsubscribe link is malformed. Please use the link from your email."), status_code=400)
    if not hmac.compare_digest(unsubscribe_token(email), (t or "")):
        return HTMLResponse(_PAGE.format(icon="⚠️", title="Invalid link",
            body="This unsubscribe link is invalid or has expired."), status_code=403)
    suppress(email)
    return HTMLResponse(_PAGE.format(icon="✅", title="You're unsubscribed",
        body=f"<b>{email}</b> will no longer receive commercial emails from "
             "Conscestra CRM. Transactional messages (order confirmations, "
             "invoices, account notices) may still be sent."))


@router.get("/consent/status")
def consent_status(email: str = ""):
    """Check suppression status for an address (used by admin/UI/tests)."""
    e = (email or "").strip().lower()
    return {"email": e, "suppressed": is_suppressed(e)}
