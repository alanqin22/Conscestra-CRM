"""FastAPI router for authentication — Conscestra CRM Auth module.

Endpoints
---------
  GET  /auth-health                    — health check
  POST /auth/signup                    — register new account + credential (bcrypt)
  POST /auth/signin                    — verify credential, issue session token
  POST /auth/signout                   — invalidate session
  POST /auth/change-password           — authenticated password rotation
  POST /auth/password-reset/request    — generate single-use reset token
  POST /auth/password-reset/confirm    — consume reset token, set new password
  POST /auth/verify-email              — verify contact e-mail token

Design
------
  • No LangGraph — direct psycopg2 calls to SECURITY DEFINER functions
    defined in sql/sp_auth.sql.
  • Primary hash path: bcrypt (DB-side via pgcrypto) — zero extra Python deps.
  • Argon2id variant: set HASH_ALGO=argon2id in .env and install argon2-cffi;
    the router auto-detects and calls the _prehashed variants instead.
  • Sessions: in-process dict keyed by a 32-byte URL-safe token.
    For multi-worker deployments, swap _AUTH_SESSIONS for Redis.

DB functions used (all SECURITY DEFINER, owned by auth_owner)
--------------------------------------------------------------
  create_credential               — bcrypt signup
  verify_credential               — bcrypt signin
  change_password                 — bcrypt password rotation
  create_credential_prehashed     — Argon2id signup
  verify_credential_prehashed     — Argon2id signin  (app verifies via PH.verify)
  change_password_prehashed       — Argon2id rotation
  create_password_reset_token     — token generation
  consume_password_reset_token    — bcrypt reset
  create_email_verification_token — email verification token
  verify_email_token              — email verification confirm

v1.0.0 — initial implementation
"""

import hashlib
import logging
import os
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import psycopg2

from app.core import release_guard
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.database import get_connection
from app.core.rate_limit import (
    AUTH_RATE_LIMIT, client_ip, reset_requests,
    signin_ip_attempts, signin_user_fails,
)
from app.agents.email.smtp_imap import send_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Auth"])

# ---------------------------------------------------------------------------
# Optional Argon2id support — only imported when HASH_ALGO=argon2id
# ---------------------------------------------------------------------------
_HASH_ALGO = os.getenv("HASH_ALGO", "bcrypt").lower()
_ARGON2_PARAMS: Dict[str, Any] = {
    "memory_kib": 65536,
    "iterations": 2,
    "parallelism": 1,
}

if _HASH_ALGO == "argon2id":
    try:
        from argon2 import PasswordHasher, exceptions as _argon2_exc  # type: ignore
        _PH = PasswordHasher(
            time_cost=_ARGON2_PARAMS["iterations"],
            memory_cost=_ARGON2_PARAMS["memory_kib"],
            parallelism=_ARGON2_PARAMS["parallelism"],
            hash_len=32,
        )
        logger.info("Auth: Argon2id mode enabled")
    except ImportError:
        logger.warning("HASH_ALGO=argon2id but argon2-cffi is not installed — falling back to bcrypt")
        _HASH_ALGO = "bcrypt"
        _PH = None
        _argon2_exc = None
else:
    _PH = None
    _argon2_exc = None

# ---------------------------------------------------------------------------
# DB-backed session store (table: auth_sessions — see sql/auth_sessions.sql).
# Survives restarts and works across multiple instances. Only the SHA-256 hash
# of the token is stored, so a DB leak cannot replay live sessions.
#
# Two expiries: an absolute TTL (8h) and a sliding idle timeout — a session
# unused for AUTH_IDLE_MINUTES is invalidated on next use (0 disables).
# Activity slides last_seen_at forward (throttled to one UPDATE per minute).
# ---------------------------------------------------------------------------
_SESSION_TTL_HOURS = 8
_IDLE_TIMEOUT_MINUTES = int(os.getenv("AUTH_IDLE_MINUTES", "15").strip() or 0)
_LAST_SEEN_TOUCH_SECONDS = 60


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# OTP (email verification) store
# { identifier: { code, expires_at, attempts, last_resend, resend_count, session_data } }
# ---------------------------------------------------------------------------
_OTP_STORE: Dict[str, Dict[str, Any]] = {}
_OTP_TTL_MINUTES = 15
_OTP_MAX_ATTEMPTS = 5
_OTP_RESEND_COOLDOWN_SECONDS = 60
_OTP_MAX_RESENDS = 5


def _generate_otp() -> str:
    return f"{random.SystemRandom().randint(0, 999999):06d}"


def _send_otp_email(to_email: str, code: str, first_name: str = "") -> None:
    """Send OTP verification email via the shared email module."""
    greeting = f"Hi {first_name}," if first_name else "Hello,"
    subject = "Your Conscestra CRM Verification Code"
    body_text = (
        f"{greeting}\n\n"
        f"Your verification code is: {code}\n\n"
        f"This code expires in {_OTP_TTL_MINUTES} minutes.\n\n"
        "If you did not request this, you can ignore this email.\n\n"
        "— Conscestra CRM"
    )
    body_html = f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px">
  <h2 style="color:#0d9488;margin-bottom:8px">Conscestra CRM</h2>
  <p>{greeting}</p>
  <p>Your email verification code is:</p>
  <div style="font-size:2.5rem;font-weight:700;letter-spacing:0.25em;color:#0d9488;
              padding:16px;background:#f0fdfa;border-radius:8px;text-align:center;
              margin:24px 0">{code}</div>
  <p style="color:#6b7280;font-size:0.875rem">
    This code expires in {_OTP_TTL_MINUTES} minutes.<br>
    If you didn't create an Conscestra CRM account, you can safely ignore this email.
  </p>
</div>"""

    result = send_email(to=to_email, subject=subject, body_html=body_html, body_text=body_text)
    if not result.get("success"):
        raise RuntimeError(result.get("message", "Email send failed"))


def _txt(v: Any) -> Optional[str]:
    return str(v) if v is not None else None


def _new_session(
    account_id: str,
    credential_id: str,
    identifier: str,
    lead_id: Optional[str] = None,
    contact_id: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    source_table: Optional[str] = None,
    role: str = "member",
) -> str:
    """Create a DB-backed session; return the plaintext token (sent to the client).
    Only the token hash is persisted."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=_SESSION_TTL_HOURS)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO auth_sessions
                     (token_hash, account_id, credential_id, identifier, lead_id,
                      contact_id, first_name, last_name, source_table, role, expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (_token_hash(token), _txt(account_id), _txt(credential_id), identifier,
                 _txt(lead_id), _txt(contact_id), first_name, last_name, source_table,
                 role or "member", expires),
            )
        conn.commit()
    finally:
        conn.close()
    return token


def get_session(token: str) -> Optional[Dict[str, Any]]:
    """Return session dict if the token is valid, not expired, and not idle
    beyond AUTH_IDLE_MINUTES; else None. Dead rows are purged opportunistically;
    activity slides last_seen_at forward (throttled)."""
    if not token:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # SELECT * so a not-yet-migrated DB (no last_seen_at) keeps working —
            # the idle check simply skips when the column is absent.
            cur.execute("SELECT * FROM auth_sessions WHERE token_hash = %s",
                        (_token_hash(token),))
            row = cur.fetchone()
            if not row:
                return None
            sess = dict(zip([d[0] for d in cur.description], row))
            now = datetime.now(timezone.utc)
            last_seen = sess.get("last_seen_at")
            idle = (_IDLE_TIMEOUT_MINUTES > 0 and last_seen is not None and
                    last_seen + timedelta(minutes=_IDLE_TIMEOUT_MINUTES) <= now)
            if sess["expires_at"] <= now or idle:
                cur.execute("DELETE FROM auth_sessions WHERE token_hash = %s", (_token_hash(token),))
                conn.commit()
                return None
            if last_seen is not None and (now - last_seen).total_seconds() > _LAST_SEEN_TOUCH_SECONDS:
                cur.execute("UPDATE auth_sessions SET last_seen_at = now() WHERE token_hash = %s",
                            (_token_hash(token),))
                conn.commit()
            # Tenancy (P4 Phase 0): every session carries a tenant_id. Single-org
            # today → 'default'. `setdefault` is future-proof: once auth_sessions
            # gains a real tenant_id column, SELECT * fills it and this is a no-op.
            sess.setdefault("tenant_id", "default")
            return sess
    finally:
        conn.close()


def _invalidate_session(token: str) -> None:
    """Delete a session (used by signout)."""
    if not token:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_sessions WHERE token_hash = %s", (_token_hash(token),))
        conn.commit()
    finally:
        conn.close()


def active_auth_sessions() -> int:
    """Return count of live sessions (purges expired/idle ones as a side-effect)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
            if _IDLE_TIMEOUT_MINUTES > 0:
                try:
                    cur.execute(
                        "DELETE FROM auth_sessions "
                        "WHERE last_seen_at <= now() - make_interval(mins => %s)",
                        (_IDLE_TIMEOUT_MINUTES,),
                    )
                except psycopg2.Error:
                    conn.rollback()  # last_seen_at not migrated yet — skip idle purge
            cur.execute("SELECT count(*) FROM auth_sessions")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SignUpRequest(BaseModel):
    identifier:     str            # email (used as login identifier, stored lowercase)
    password:       str
    confirm_password: Optional[str] = None
    # Profile fields
    first_name:     Optional[str] = None
    last_name:      Optional[str] = None
    company_name:   Optional[str] = None
    phone:          Optional[str] = None
    role:           Optional[str] = None
    address_line1:  Optional[str] = None
    address_line2:  Optional[str] = None
    city:           Optional[str] = None
    province:       Optional[str] = None
    postal_code:    Optional[str] = None
    country:        Optional[str] = None
    # Business details (firmographics) — optional, power segmentation + scoring
    industry:       Optional[str] = None
    website:        Optional[str] = None
    employee_band:  Optional[str] = None
    revenue_band:   Optional[str] = None
    marketing_consent: bool = False
    # Legacy: supply existing IDs instead of auto-creating
    account_id:     Optional[str] = None
    contact_id:     Optional[str] = None


class SignInRequest(BaseModel):
    identifier: str
    password:   str


class SignOutRequest(BaseModel):
    session_token: str


class ChangePasswordRequest(BaseModel):
    identifier:       str
    current_password: str
    new_password:     str


class PasswordResetRequestModel(BaseModel):
    identifier:  str
    ttl_seconds: int = 3600


class PasswordResetConfirmRequest(BaseModel):
    token:        str
    new_password: str


class VerifyEmailRequest(BaseModel):
    contact_id: str
    token:      str


class VerifyCodeRequest(BaseModel):
    identifier: str
    code:       str


class ResendVerificationRequest(BaseModel):
    identifier: str


class AuthResponse(BaseModel):
    success:       bool
    message:       str
    session_token: Optional[str] = None
    credential_id: Optional[str] = None
    lead_id:       Optional[str] = None
    account_id:    Optional[str] = None
    contact_id:    Optional[str] = None
    identifier:    Optional[str] = None
    email:         Optional[str] = None
    first_name:    Optional[str] = None
    last_name:     Optional[str] = None
    source_table:  Optional[str] = None
    role:          Optional[str] = None   # access tier: admin | member | viewer
    needs_password: Optional[bool] = None
    # NOTE: reset_token is included only for development/demo mode.
    # In production, email the token to the user and remove this field.
    reset_token:   Optional[str] = None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_fetchone(sql: str, params: tuple = ()) -> Any:
    """Execute SQL and return the first row (or None)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return row
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _db_execute(sql: str, params: tuple = ()) -> None:
    """Execute SQL with no return value."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Account / contact bootstrap helpers (used during signup)
# ---------------------------------------------------------------------------

def _bootstrap_account(company_name: str) -> str:
    """INSERT a minimal account row; return its UUID."""
    acct_id = str(uuid.uuid4())
    _db_execute(
        """
        INSERT INTO public.accounts
          (account_id, account_name, type, status, is_deleted,
           created_at, updated_at)
        VALUES (%s, %s, 'customer', 'Active', false, now(), now())
        """,
        (acct_id, company_name),
    )
    return acct_id


def _bootstrap_contact(
    account_id: str, email: str, first_name: str, last_name: str
) -> str:
    """INSERT a minimal contact row; return its UUID."""
    contact_id = str(uuid.uuid4())
    _db_execute(
        """
        INSERT INTO public.contacts
          (contact_id, account_id, first_name, last_name, email,
           status, is_deleted, is_email_verified, is_customer,
           created_at, updated_at)
        VALUES (%s, %s, %s, %s, lower(%s),
                'Active', false, false, true,
                now(), now())
        """,
        (contact_id, account_id, first_name or "", last_name or "", email),
    )
    return contact_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/auth-health")
async def auth_health():
    from app.core.auth_dep import SECURITY_POSTURE
    return {
        "status":          "healthy",
        "module":          "auth",
        "version":         "1.0.0",
        "hash_algo":       _HASH_ALGO,
        "sessions_active": active_auth_sessions(),
        "security_mode":   SECURITY_POSTURE,  # open | public-read | locked
    }


@router.post("/auth/signup", response_model=AuthResponse)
async def signup(req: SignUpRequest):
    """Register a new user. Creates auth_credentials + lead record atomically via sp_signup_with_lead."""
    identifier = req.identifier.strip().lower()
    logger.info(f"Signup — identifier={identifier!r}")

    # Client-side password match check (belt-and-suspenders)
    if req.confirm_password is not None and req.password != req.confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match")

    try:
        row = _db_fetchone(
            "SELECT public.sp_signup_with_lead("
            "  %s::text, %s::text,"
            "  %s::text, %s::text, %s::text, %s::text, %s::text,"
            "  %s::text, %s::text, %s::text, %s::text, %s::text, %s::text"
            ") AS payload",
            (
                identifier, req.password,
                req.first_name, req.last_name, req.company_name, req.phone, req.role,
                req.address_line1, req.address_line2,
                req.city, req.province, req.postal_code, req.country,
            ),
        )
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="That email is already registered")
    except psycopg2.Error as exc:
        msg = str(exc)
        if "23505" in msg or "unique" in msg.lower() or "duplicate" in msg.lower() or "email_exists" in msg.lower():
            raise HTTPException(status_code=409, detail="That email is already registered")
        logger.error(f"sp_signup_with_lead error: {exc}")
        raise HTTPException(status_code=500, detail="Registration failed")

    if not row or not row[0]:
        raise HTTPException(status_code=500, detail="Signup returned no payload")

    payload = row[0]
    if not payload.get("success"):
        raise HTTPException(status_code=500, detail="Signup failed")

    credential_id = str(payload["credential_id"])
    lead_id       = str(payload["lead_id"])       if payload.get("lead_id")    else None
    account_id    = str(payload["account_id"])    if payload.get("account_id") else None
    contact_id    = str(payload["contact_id"])    if payload.get("contact_id") else None
    first_name    = payload.get("first_name", "")
    last_name     = payload.get("last_name", "")
    source_table  = payload.get("source_table", "leads")

    logger.info(f"Signup OK — lead={lead_id} account={account_id} cred={credential_id}")

    # Persist the optional firmographics + marketing consent onto the lead. Done
    # as a follow-up UPDATE (not new SP params) to avoid changing the signup SP's
    # signature. The conversion (sp_convert_signup_lead) carries these onto the
    # account/contact. enriched_at is stamped so enrichment won't clobber the
    # buyer's self-reported values.
    if lead_id and any([req.industry, req.website, req.employee_band,
                        req.revenue_band, req.marketing_consent]):
        try:
            _db_execute(
                """UPDATE public.leads SET
                       industry          = COALESCE(NULLIF(TRIM(%s), ''), industry),
                       website           = COALESCE(NULLIF(TRIM(%s), ''), website),
                       employee_band     = COALESCE(NULLIF(TRIM(%s), ''), employee_band),
                       revenue_band      = COALESCE(NULLIF(TRIM(%s), ''), revenue_band),
                       marketing_consent = %s,
                       consent_at        = CASE WHEN %s THEN now() ELSE consent_at END,
                       enriched_at       = CASE WHEN %s OR %s OR %s OR %s
                                                THEN now() ELSE enriched_at END,
                       updated_at        = now()
                   WHERE lead_id = %s::uuid""",
                (req.industry or "", req.website or "", req.employee_band or "",
                 req.revenue_band or "", req.marketing_consent, req.marketing_consent,
                 bool(req.industry), bool(req.website), bool(req.employee_band),
                 bool(req.revenue_band), lead_id),
            )
        except Exception as exc:
            logger.warning(f"Could not persist signup firmographics for lead {lead_id}: {exc}")

    # Generate OTP and store pending verification
    code = _generate_otp()
    _OTP_STORE[identifier] = {
        "code":        code,
        "expires_at":  datetime.now(timezone.utc) + timedelta(minutes=_OTP_TTL_MINUTES),
        "attempts":    0,
        "last_resend": datetime.now(timezone.utc),
        "resend_count": 0,
        "session_data": {
            "credential_id": credential_id,
            "lead_id":       lead_id,
            "account_id":    account_id,
            "contact_id":    contact_id,
            "first_name":    first_name,
            "last_name":     last_name,
            "source_table":  source_table,
        },
    }

    try:
        _send_otp_email(identifier, code, first_name)
        logger.info(f"OTP email sent to {identifier!r}")
    except Exception as exc:
        logger.error(f"Failed to send OTP email to {identifier!r}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Account created but verification email could not be sent. "
                   "Please use 'Resend Code' or contact support.",
        )

    return AuthResponse(
        success=True,
        message="Account created. Check your email for the verification code.",
        identifier=identifier,
        email=identifier,
        first_name=first_name,
        last_name=last_name,
    )


def _signin_rejected(identifier: str, ip: str) -> HTTPException:
    """Record a failed sign-in for rate limiting + alerting; return the 401."""
    fails = signin_user_fails.record(identifier) if AUTH_RATE_LIMIT else 0
    logger.warning(
        f"[security] signin FAILED — identifier={identifier!r} ip={ip} "
        f"fails_in_window={fails}"
    )
    return HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/auth/signin", response_model=AuthResponse)
async def signin(req: SignInRequest, request: Request):
    """Verify credential via sp_signin_multi_table (leads → accounts → contacts lookup)."""
    identifier = req.identifier.strip().lower()
    ip = client_ip(request)
    logger.info(f"Signin — identifier={identifier!r}")

    # Brute-force / credential-stuffing throttle (security hardening #4).
    if AUTH_RATE_LIMIT:
        if signin_ip_attempts.record(ip) > signin_ip_attempts.max_events:
            logger.warning(f"[security] signin RATE-LIMITED (per-IP) — ip={ip}")
            raise HTTPException(
                status_code=429,
                detail="Too many sign-in attempts. Please wait a few minutes and try again.",
            )
        if signin_user_fails.is_limited(identifier):
            logger.warning(
                f"[security] signin LOCKED (per-account fail limit) — "
                f"identifier={identifier!r} ip={ip}"
            )
            raise HTTPException(
                status_code=429,
                detail="Too many failed sign-in attempts for this account. "
                       "Please wait a few minutes or reset your password.",
            )

    try:
        row = _db_fetchone(
            "SELECT public.sp_signin_multi_table(%s::text, %s::text) AS payload",
            (identifier, req.password),
        )
    except psycopg2.Error as exc:
        logger.error(f"sp_signin_multi_table DB error: {exc}")
        raise HTTPException(status_code=500, detail="Authentication error")

    if not row or not row[0]:
        raise _signin_rejected(identifier, ip)

    payload = row[0]
    if not payload.get("success"):
        raise _signin_rejected(identifier, ip)

    credential_id = str(payload["credential_id"])
    lead_id       = str(payload["lead_id"])       if payload.get("lead_id")    else None
    account_id    = str(payload["account_id"])    if payload.get("account_id") else None
    contact_id    = str(payload["contact_id"])    if payload.get("contact_id") else None
    first_name    = payload.get("first_name", "")
    last_name     = payload.get("last_name", "")
    source_table  = payload.get("source_table", "leads")

    # Check email verified (contacts table only; leads don't require verification)
    if contact_id and source_table == "contacts":
        try:
            ver_row = _db_fetchone(
                "SELECT is_email_verified FROM public.contacts WHERE contact_id = %s::uuid",
                (contact_id,),
            )
            if ver_row and ver_row[0] is False:
                raise HTTPException(
                    status_code=403,
                    detail="Your email is not verified yet. Please check your inbox or request a new code.",
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(f"Could not check email verification status: {exc}")

    # Resolve the access tier (admin | member | viewer) from the login identity.
    access_role = "member"
    try:
        role_row = _db_fetchone(
            "SELECT access_role FROM auth_credentials WHERE credential_id = %s::uuid",
            (credential_id,),
        )
        if role_row and role_row[0] in ("admin", "member", "viewer"):
            access_role = role_row[0]
    except Exception as exc:
        logger.warning(f"access_role lookup failed (defaulting to member): {exc}")

    session_token = _new_session(
        account_id=account_id or "",
        credential_id=credential_id,
        identifier=identifier,
        lead_id=lead_id,
        contact_id=contact_id,
        first_name=first_name,
        last_name=last_name,
        source_table=source_table,
        role=access_role,
    )

    signin_user_fails.reset(identifier)  # successful login clears the fail window
    logger.info(f"Signin OK — lead={lead_id} account={account_id} role={access_role}")
    return AuthResponse(
        success=True,
        message="Signed in successfully",
        session_token=session_token,
        credential_id=credential_id,
        lead_id=lead_id,
        account_id=account_id,
        contact_id=contact_id,
        identifier=identifier,
        email=identifier,
        first_name=first_name,
        last_name=last_name,
        source_table=source_table,
        role=access_role,
    )


@router.post("/auth/signout", response_model=AuthResponse)
async def signout(req: SignOutRequest):
    """Invalidate a session token."""
    _invalidate_session(req.session_token)
    return AuthResponse(success=True, message="Signed out")


@router.post("/auth/change-password", response_model=AuthResponse)
async def change_password(req: ChangePasswordRequest):
    """Rotate password (requires the current password for verification)."""
    try:
        if _HASH_ALGO == "argon2id" and _PH and _argon2_exc:
            # Fetch stored hash
            hash_row = _db_fetchone(
                """
                SELECT ac.credential_id, ac.password_hash
                FROM   public.auth_credentials ac
                WHERE  ac.credential_type = 'password'
                  AND  ac.identifier      = lower(%s)
                  AND  ac.is_active       = true
                LIMIT 1
                """,
                (req.identifier,),
            )
            if not hash_row:
                raise HTTPException(status_code=401, detail="Account not found")
            stored_cred_id, stored_hash = hash_row
            try:
                _PH.verify(stored_hash, req.current_password)
            except _argon2_exc.VerifyMismatchError:
                raise HTTPException(status_code=401, detail="Current password is incorrect")

            new_hash = _PH.hash(req.new_password)
            row = _db_fetchone(
                "SELECT public.change_password_prehashed"
                "(%s::text,%s::text,%s::text,%s::text,%s::jsonb) AS ok",
                ("password", req.identifier, stored_hash, new_hash, _ARGON2_PARAMS),
            )
        else:
            row = _db_fetchone(
                "SELECT public.change_password(%s::text,%s::text,%s::text,%s::text) AS ok",
                ("password", req.identifier, req.current_password, req.new_password),
            )
    except HTTPException:
        raise
    except psycopg2.Error as exc:
        logger.error(f"change_password DB error: {exc}")
        raise HTTPException(status_code=500, detail="Password change failed")

    if not row or not row[0]:
        raise HTTPException(
            status_code=401,
            detail="Current password is incorrect or account not found",
        )
    return AuthResponse(success=True, message="Password changed successfully")


def _send_reset_email(to_email: str, token: str, ttl_seconds: int) -> None:
    """Deliver a reset code to the account holder. The ONLY channel in production.

    Until 2026-09-06 this endpoint sent nothing at all. Its own docstring said
    "Production note: email the returned token to the user", and that was never
    implemented — the flow appeared to work only because the token came back in
    the HTTP response, which is what made it an unauthenticated account
    takeover. Closing that hole without building this would have left the
    deployed system with no password reset whatsoever.

    Never logs the token. A secret in a log is a secret in whatever ships logs.
    """
    minutes = max(1, int(ttl_seconds) // 60)
    window = (f"{minutes // 60} hour" + ("s" if minutes // 60 > 1 else "")
              if minutes >= 60 else f"{minutes} minutes")

    # The origin a PERSON should see, which is not the API origin. No *.html is
    # in git, so the backend cannot serve auth.html in production and a link
    # built from APP_URL would land on a 500. order_status._public_site is the
    # single place that rule is written down; re-deriving it here is how the
    # two would drift, and the failure mode is a broken button in every email.
    from app.core.order_status import _public_site
    # ?tab=reset opens the Reset panel with the code box already showing. The
    # bare page opens on Sign In, and on 2026-09-06 a recipient did the natural
    # thing with a long random string on a sign-in form: pasted it into
    # Password. It is a one-time token, not a password, and the form had no way
    # to say so. NO TOKEN IN THE URL — a query string lands in browser history,
    # in the referrer of every asset the page loads, and in any proxy log on the
    # way. The code is pasted by hand, from the mail, once.
    link = f"{_public_site()}/auth.html?tab=reset"

    send_email(
        to=to_email,
        subject="Conscestra CRM — Password reset code",
        body_html=(
            "<div style=\"font-family:sans-serif;max-width:480px;margin:0 auto;"
            "padding:32px 24px\">"
            "<h2 style=\"color:#0d9488;margin-bottom:8px\">Conscestra CRM</h2>"
            "<p>You asked to reset your password.</p>"
            f"<p><b>1.</b> Open <a href=\"{link}\">{link}</a> — it opens on the "
            "<b>Reset</b> tab.<br>"
            "<b>2.</b> Paste the code below into <b>Reset token</b>.<br>"
            "<b>3.</b> Type the new password you want, then press "
            "<b>Set New Password</b>.</p>"
            "<div style=\"font-size:1.1rem;font-weight:700;background:#f0fdfa;"
            "border-radius:8px;padding:14px;text-align:center;"
            f"letter-spacing:.04em;margin:24px 0\">{token}</div>"
            "<p style=\"color:#6b7280;font-size:.875rem\">"
            "This code is <b>not your password</b> — it is a one-time code that "
            "lets you choose one. Do not type it into the Sign In form.<br>"
            f"It expires in {window}. If you did not request it, you can ignore "
            "this email — your password has not changed.</p></div>"
        ),
        body_text=(
            "You asked to reset your Conscestra CRM password.\n\n"
            f"Reset code: {token}\n\n"
            f"1. Open {link} — it opens on the Reset tab.\n"
            "2. Paste the code above into 'Reset token'.\n"
            "3. Type the new password you want, then press 'Set New Password'.\n\n"
            "This code is NOT your password. It is a one-time code that lets you "
            "choose one, so do not type it into the Sign In form.\n"
            f"It expires in {window}.\n\n"
            "If you did not request this, ignore this email — your password has "
            "not changed.\n\n— Conscestra CRM"
        ),
        # Transactional. commercial=True would check the marketing opt-out list,
        # and an unsubscribe from a newsletter must never be able to lock
        # somebody out of their own account.
        commercial=False,
    )


@router.post("/auth/password-reset/request", response_model=AuthResponse)
async def password_reset_request(req: PasswordResetRequestModel, request: Request):
    """Generate a single-use password reset token.

    Production note: email the returned token to the user; remove
    ``reset_token`` from the response and never log it.
    """
    identifier = req.identifier.strip().lower()
    if AUTH_RATE_LIMIT and reset_requests.record(identifier) > reset_requests.max_events:
        logger.warning(
            f"[security] password-reset RATE-LIMITED — "
            f"identifier={identifier!r} ip={client_ip(request)}"
        )
        raise HTTPException(
            status_code=429,
            detail="Too many reset requests for this account. Please try again later.",
        )
    try:
        row = _db_fetchone(
            "SELECT public.create_password_reset_token(%s::text,%s::text,%s::int) AS token",
            ("password", req.identifier, req.ttl_seconds),
        )
    except psycopg2.Error as exc:
        errmsg = str(exc)
        if "No active credential" in errmsg:
            # Do not reveal whether the account exists
            return AuthResponse(
                success=True,
                message="If that account exists, a reset link has been sent",
            )
        logger.error(f"password_reset_request DB error: {exc}")
        raise HTTPException(status_code=500, detail="Reset request failed")

    if not row or not row[0]:
        return AuthResponse(
            success=True,
            message="If that account exists, a reset link has been sent",
        )

    reset_token = str(row[0])
    logger.info(f"Reset token created for identifier={req.identifier!r} (never log the token itself)")

    # ── THE TOKEN IS NOT HANDED TO THE CALLER UNLESS SOMEBODY SAID SO ────────
    # This used to return `reset_token` unconditionally, with a comment reading
    # "DEMO ONLY — remove in production". Nothing enforced the comment. The
    # endpoint takes no authentication, so on 2026-09-05 an ANONYMOUS caller
    # could post any real identifier and receive a working password-reset token
    # for it — verified locally against `cto@agentorc.ca`, and the commit
    # Railway reported as deployed (cfaef1776d54) carries the same line.
    #
    # It stayed survivable only while one shared administrator credential
    # existed and the console decided nothing. Individual executive credentials
    # changed that: every governed decision is now bound to an authenticated
    # executive session, so this endpoint had become the cheapest way to forge
    # one — no impersonation needed, just the CFO's email address.
    #
    # FAILS CLOSED, and deliberately not on `is_deployed()` alone: that helper
    # documents itself as treating absence of evidence as "not deployed" so a
    # developer is never blocked, which is the correct bias for a warning and
    # the wrong one for a secret. A host that sets neither APP_ENV nor the
    # RAILWAY_* markers would hand out tokens. So disclosure requires a POSITIVE
    # opt-in that someone has to type, AND the absence of deployment evidence.
    # Unset anywhere, including a misconfigured production box: no token.
    disclose = (
        os.getenv("AUTH_RESET_TOKEN_IN_RESPONSE", "").strip().lower()
        in ("1", "true", "yes")
        and not release_guard.is_deployed()
    )
    if disclose:
        return AuthResponse(
            success=True,
            message="Reset token generated. In production this is emailed — never logged.",
            reset_token=reset_token,   # local only; see the gate above
        )

    # DELIVERY, and deliberately in the branch where the token was NOT handed
    # back. Exactly one channel carries it, never two: on a laptop the response
    # already contains it and no mail leaves the machine, and on a deployed host
    # the response contains nothing and the inbox is the only way to get it.
    #
    # Failures are logged and swallowed on purpose. This reply must be identical
    # whether or not the account exists, so nothing about delivery may change it
    # — an error here would re-open the account-existence oracle that the shared
    # message below closes.
    try:
        _send_reset_email(identifier, reset_token, req.ttl_seconds)
    except Exception as exc:                       # never the token in a log
        logger.warning(f"[security] reset email failed for {identifier!r}: "
                       f"{str(exc)[:160]}")

    # THE DEFAULT PATH, and the SAME sentence the unknown-identifier branch
    # returns. A different reply here would leave the endpoint an
    # account-existence oracle: post an address, and the answer tells you
    # whether it is a real credential. Closing the takeover while still
    # confirming the account would trade a break-in for an enumeration of every
    # executive who has a sign-in.
    return AuthResponse(
        success=True,
        message="If that account exists, a reset link has been sent",
    )


@router.post("/auth/password-reset/confirm", response_model=AuthResponse)
async def password_reset_confirm(req: PasswordResetConfirmRequest):
    """Consume a reset token and set a new password."""
    try:
        row = _db_fetchone(
            "SELECT public.consume_password_reset_token(%s::text,%s::text) AS ok",
            (req.token, req.new_password),
        )
    except psycopg2.Error as exc:
        logger.error(f"password_reset_confirm DB error: {exc}")
        raise HTTPException(status_code=500, detail="Password reset failed")

    if not row or not row[0]:
        raise HTTPException(
            status_code=400,
            detail="Invalid, expired, or already-used reset token",
        )
    return AuthResponse(success=True, message="Password reset. Please sign in.")


@router.post("/auth/verify-code", response_model=AuthResponse)
async def verify_code(req: VerifyCodeRequest):
    """Verify 6-digit OTP sent after signup. Issues session on success."""
    identifier = req.identifier.strip().lower()
    entry = _OTP_STORE.get(identifier)

    if not entry:
        raise HTTPException(status_code=404, detail="No pending verification for this email")

    if datetime.now(timezone.utc) > entry["expires_at"]:
        _OTP_STORE.pop(identifier, None)
        raise HTTPException(status_code=410, detail="Verification code has expired. Request a new one.")

    if entry["attempts"] >= _OTP_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Request a new code.")

    if req.code != entry["code"]:
        entry["attempts"] += 1
        remaining = _OTP_MAX_ATTEMPTS - entry["attempts"]
        raise HTTPException(
            status_code=401,
            detail=f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} remaining.",
        )

    # Code correct — the email is now proven. CONVERT the signup lead into a real
    # customer (account + contact, email-verified) and link the credential, so the
    # session carries account_id/contact_id and store checkout works with no error.
    # Signup only created a lead (account_id/contact_id NULL); conversion happens
    # here, on verification — see sp_convert_signup_lead.
    sd = entry["session_data"]
    account_id = sd.get("account_id")
    contact_id = sd.get("contact_id")
    if sd.get("lead_id"):
        try:
            row = _db_fetchone(
                "SELECT public.sp_convert_signup_lead(%s::uuid, %s::uuid) AS payload",
                (sd["lead_id"], sd.get("credential_id")),
            )
            conv = (row[0] if row else None) or {}
            if conv.get("success"):
                account_id = conv.get("account_id") or account_id
                contact_id = conv.get("contact_id") or contact_id
                logger.info(f"Signup lead converted on verify — account={account_id} contact={contact_id}")
            else:
                logger.warning(f"Lead conversion returned no success for {identifier!r}: {conv}")
        except Exception as exc:
            logger.warning(f"Could not convert signup lead for {identifier!r}: {exc}")
    elif contact_id:
        # Edge case: a contact already existed (no lead to convert) — just verify it.
        try:
            _db_execute(
                "UPDATE public.contacts SET is_email_verified = true, updated_at = now() "
                "WHERE contact_id = %s::uuid",
                (contact_id,),
            )
        except Exception as exc:
            logger.warning(f"Could not mark email verified for contact {contact_id}: {exc}")

    _OTP_STORE.pop(identifier, None)

    session_token = _new_session(
        account_id=account_id or "",
        credential_id=sd["credential_id"],
        identifier=identifier,
        lead_id=sd.get("lead_id"),
        contact_id=contact_id,
        first_name=sd.get("first_name", ""),
        last_name=sd.get("last_name", ""),
        source_table="contacts" if account_id else sd.get("source_table", "leads"),
    )

    logger.info(f"Email verified and session issued for {identifier!r}")
    return AuthResponse(
        success=True,
        message="Email verified. Welcome to Conscestra CRM.",
        session_token=session_token,
        credential_id=sd["credential_id"],
        lead_id=sd.get("lead_id"),
        account_id=account_id,
        contact_id=contact_id,
        identifier=identifier,
        email=identifier,
        first_name=sd.get("first_name", ""),
        last_name=sd.get("last_name", ""),
        source_table="contacts" if account_id else sd.get("source_table", "leads"),
    )


@router.post("/auth/resend-verification", response_model=AuthResponse)
async def resend_verification(req: ResendVerificationRequest):
    """Resend OTP to the given email. Rate-limited."""
    identifier = req.identifier.strip().lower()
    entry = _OTP_STORE.get(identifier)

    if entry:
        now = datetime.now(timezone.utc)
        elapsed = (now - entry["last_resend"]).total_seconds()
        if elapsed < _OTP_RESEND_COOLDOWN_SECONDS:
            wait = int(_OTP_RESEND_COOLDOWN_SECONDS - elapsed)
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {wait} second{'s' if wait != 1 else ''} before requesting a new code.",
            )
        if entry["resend_count"] >= _OTP_MAX_RESENDS:
            raise HTTPException(
                status_code=429,
                detail="Too many resend requests. Please try again later or contact support.",
            )
        first_name = entry["session_data"].get("first_name", "")
        resend_count = entry["resend_count"] + 1
        sd = entry["session_data"]
    else:
        # Allow resend for existing unverified contacts (e.g. from signin 403 flow)
        try:
            row = _db_fetchone(
                """
                SELECT c.contact_id, c.first_name, c.last_name, c.is_email_verified,
                       ac.credential_id,
                       l.lead_id, c.account_id
                FROM   public.contacts c
                LEFT   JOIN public.auth_credentials ac
                       ON  ac.identifier = lower(c.email)
                       AND ac.credential_type = 'password' AND ac.is_active = true
                LEFT   JOIN public.leads l ON l.converted_contact_id = c.contact_id
                WHERE  c.email = lower(%s) AND c.is_deleted = false
                LIMIT  1
                """,
                (identifier,),
            )
        except Exception as exc:
            logger.error(f"resend_verification DB lookup error: {exc}")
            raise HTTPException(status_code=500, detail="Lookup failed")

        if not row:
            # Respond vaguely to avoid account enumeration
            return AuthResponse(
                success=True,
                message="If that account exists, a new verification code has been sent.",
            )
        contact_id, first_name, last_name, is_verified, credential_id, lead_id, account_id = row
        if is_verified:
            return AuthResponse(success=True, message="Email is already verified. Please sign in.")

        sd = {
            "credential_id": str(credential_id) if credential_id else "",
            "lead_id":       str(lead_id)       if lead_id       else None,
            "account_id":    str(account_id)    if account_id    else None,
            "contact_id":    str(contact_id),
            "first_name":    first_name or "",
            "last_name":     last_name  or "",
            "source_table":  "contacts",
        }
        resend_count = 1

    code = _generate_otp()
    _OTP_STORE[identifier] = {
        "code":        code,
        "expires_at":  datetime.now(timezone.utc) + timedelta(minutes=_OTP_TTL_MINUTES),
        "attempts":    0,
        "last_resend": datetime.now(timezone.utc),
        "resend_count": resend_count,
        "session_data": sd,
    }

    try:
        _send_otp_email(identifier, code, first_name or "")
        logger.info(f"OTP resent to {identifier!r}")
    except Exception as exc:
        logger.error(f"Failed to resend OTP to {identifier!r}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to send verification email. Please try again.")

    return AuthResponse(
        success=True,
        message="A new verification code has been sent to your email.",
    )


@router.get("/auth/address")
async def get_address(
    lead_id:    Optional[str] = None,
    contact_id: Optional[str] = None,
    account_id: Optional[str] = None,
    email:      Optional[str] = None,
):
    """Return the shipping address for the given IDs.

    Priority order:
      1. leads.address_line* (inline columns — checked whenever lead_id is supplied)
      2. addresses table via contact_id  (parent_type='contact')
      3. addresses table via account_id  (parent_type='account')
    """
    address = None

    # 1. Leads Table — High Priority (Direct lead_id or conversion links)
    if lead_id:
        try:
            row = _db_fetchone(
                """
                SELECT address_line1, address_line2, city, province, postal_code, country
                FROM   public.leads
                WHERE  lead_id = %s::uuid
                """,
                (lead_id,),
            )
            if row and row[0]: # row[0] is address_line1
                address = {
                    "line1": row[0], "line2": row[1], "city": row[2],
                    "province": row[3], "postal_code": row[4], "country": row[5],
                    "source": "leads (direct)",
                }
        except psycopg2.Error as exc:
            logger.warning(f"get_address leads direct lookup error: {exc}")

    # 1b. Leads Table — via converted_contact_id
    if not address and contact_id:
        try:
            row = _db_fetchone(
                """
                SELECT address_line1, address_line2, city, province, postal_code, country
                FROM   public.leads
                WHERE  converted_contact_id = %s::uuid
                  AND  address_line1 IS NOT NULL
                ORDER  BY created_at DESC
                LIMIT  1
                """,
                (contact_id,),
            )
            if row and row[0]:
                address = {
                    "line1": row[0], "line2": row[1], "city": row[2],
                    "province": row[3], "postal_code": row[4], "country": row[5],
                    "source": "leads (via contact conversion)",
                }
        except psycopg2.Error as exc:
            logger.warning(f"get_address leads contact conversion lookup error: {exc}")

    # 1c. Leads Table — via converted_account_id
    if not address and account_id:
        try:
            row = _db_fetchone(
                """
                SELECT address_line1, address_line2, city, province, postal_code, country
                FROM   public.leads
                WHERE  converted_account_id = %s::uuid
                  AND  address_line1 IS NOT NULL
                ORDER  BY created_at DESC
                LIMIT  1
                """,
                (account_id,),
            )
            if row and row[0]:
                address = {
                    "line1": row[0], "line2": row[1], "city": row[2],
                    "province": row[3], "postal_code": row[4], "country": row[5],
                    "source": "leads (via account conversion)",
                }
        except psycopg2.Error as exc:
            logger.warning(f"get_address leads account conversion lookup error: {exc}")

    # 1d. Leads Table — via email fallback
    if not address and email:
        try:
            row = _db_fetchone(
                """
                SELECT address_line1, address_line2, city, province, postal_code, country
                FROM   public.leads
                WHERE  email = %s
                  AND  address_line1 IS NOT NULL
                ORDER  BY created_at DESC
                LIMIT  1
                """,
                (email,),
            )
            if row and row[0]:
                address = {
                    "line1": row[0], "line2": row[1], "city": row[2],
                    "province": row[3], "postal_code": row[4], "country": row[5],
                    "source": "leads (via email)",
                }
        except psycopg2.Error as exc:
            logger.warning(f"get_address leads email lookup error: {exc}")

    # 2. Addresses Table — Secondary Priority (Standard addresses)
    if not address and contact_id:
        try:
            row = _db_fetchone(
                """
                SELECT line1, line2, city, province, postal_code, country
                FROM   public.addresses
                WHERE  parent_type = 'contact' AND parent_id = %s::uuid
                ORDER  BY is_default DESC, created_at DESC
                LIMIT  1
                """,
                (contact_id,),
            )
            if row and row[0]:
                address = {
                    "line1": row[0], "line2": row[1], "city": row[2],
                    "province": row[3], "postal_code": row[4], "country": row[5],
                    "source": "addresses (contact)",
                }
        except psycopg2.Error as exc:
            logger.warning(f"get_address contact addresses lookup error: {exc}")

    if not address and account_id:
        try:
            row = _db_fetchone(
                """
                SELECT line1, line2, city, province, postal_code, country
                FROM   public.addresses
                WHERE  parent_type = 'account' AND parent_id = %s::uuid
                ORDER  BY is_default DESC, created_at DESC
                LIMIT  1
                """,
                (account_id,),
            )
            if row and row[0]:
                address = {
                    "line1": row[0], "line2": row[1], "city": row[2],
                    "province": row[3], "postal_code": row[4], "country": row[5],
                    "source": "addresses (account)",
                }
        except psycopg2.Error as exc:
            logger.warning(f"get_address account addresses lookup error: {exc}")

    return {"success": True, "address": address}


@router.post("/auth/verify-email", response_model=AuthResponse)
async def verify_email(req: VerifyEmailRequest):
    """Mark a contact's email as verified using the token from contacts table."""
    try:
        row = _db_fetchone(
            "SELECT public.verify_email_token(%s::uuid,%s::text) AS ok",
            (req.contact_id, req.token),
        )
    except psycopg2.Error as exc:
        logger.error(f"verify_email DB error: {exc}")
        raise HTTPException(status_code=500, detail="Email verification failed")

    if not row or not row[0]:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired email verification token",
        )
    return AuthResponse(success=True, message="Email verified successfully")
