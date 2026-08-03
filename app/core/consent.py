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
from typing import Any, Dict, Tuple

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





def is_suppressed(email: str) -> bool:
    """True when the address opted out. Fails OPEN on DB errors for
    transactional continuity — commercial callers log the failure."""
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
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return False
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
