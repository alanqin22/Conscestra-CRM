"""Admin Users console — manage auth_credentials (security #2 follow-up).

Every endpoint requires an admin (require_admin: an admin-role session OR the
ADMIN_API_TOKEN). Password hashes are NEVER returned. Password resets are
email-based (the admin never sees or sets the password).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth_dep import KNOWN_ROLES, require_admin
from app.core.database import get_connection
from app.agents.auth.router import _db_fetchone
from app.agents.email.smtp_imap import send_email

logger = logging.getLogger("admin_users")

# Admin-gated for ALL routes in this router.
router = APIRouter(tags=["admin-users"], dependencies=[Depends(require_admin)])

# Includes `registrant`, so an administrator can both activate a new signup
# (registrant -> viewer or member) and withdraw access from an existing account
# (-> registrant) without deactivating the login.
VALID_ROLES = set(KNOWN_ROLES)


# ── models ──────────────────────────────────────────────────────────────────
class RoleReq(BaseModel):
    identifier: str
    role: str


class ActiveReq(BaseModel):
    identifier: str
    is_active: bool


class IdentifierReq(BaseModel):
    identifier: str


def _ident(s: str) -> str:
    return (s or "").strip().lower()


def _update(sql: str, params: tuple) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


# ── list ────────────────────────────────────────────────────────────────────
@router.get("/admin/users")
async def list_users():
    """All logins with role/status/verification — never password hashes."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ac.identifier,
                       ac.access_role,
                       ac.credential_type,
                       ac.is_active,
                       ac.created_at,
                       ac.last_used_at,
                       CASE WHEN ac.lead_id    IS NOT NULL THEN 'lead'
                            WHEN ac.contact_id IS NOT NULL THEN 'contact'
                            WHEN ac.account_id IS NOT NULL THEN 'account'
                            ELSE NULL END                              AS linked_type,
                       COALESCE(ct.is_email_verified, ac.contact_id IS NULL) AS email_verified
                FROM   auth_credentials ac
                LEFT   JOIN contacts ct ON ct.contact_id = ac.contact_id
                ORDER  BY (ac.access_role <> 'member') DESC, ac.created_at DESC
                """
            )
            cols = [d[0] for d in cur.description]
            users = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"success": True, "count": len(users), "users": users}
    finally:
        conn.close()


# ── set role ────────────────────────────────────────────────────────────────
@router.post("/admin/users/role")
async def set_role(req: RoleReq):
    role = (req.role or "").strip().lower()
    if role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(VALID_ROLES)}")
    n = _update("UPDATE auth_credentials SET access_role=%s WHERE identifier=%s",
                (role, _ident(req.identifier)))
    if not n:
        raise HTTPException(404, "Account not found")
    return {"success": True,
            "message": f"Role set to '{role}'. The user must sign in again for it to take effect."}


# ── enable / disable ─────────────────────────────────────────────────────────
@router.post("/admin/users/active")
async def set_active(req: ActiveReq):
    n = _update("UPDATE auth_credentials SET is_active=%s WHERE identifier=%s",
                (bool(req.is_active), _ident(req.identifier)))
    if not n:
        raise HTTPException(404, "Account not found")
    # Disabling should also kill live sessions so it takes effect immediately.
    if not req.is_active:
        _update("DELETE FROM auth_sessions WHERE identifier=%s", (_ident(req.identifier),))
    return {"success": True, "message": "Account enabled." if req.is_active else "Account disabled."}


# ── mark email verified ──────────────────────────────────────────────────────
@router.post("/admin/users/verify")
async def mark_verified(req: IdentifierReq):
    n = _update(
        """UPDATE contacts SET is_email_verified=TRUE
           WHERE contact_id = (SELECT contact_id FROM auth_credentials WHERE identifier=%s)""",
        (_ident(req.identifier),),
    )
    return {"success": True,
            "message": "Email marked verified." if n else "No linked contact — nothing to verify."}


# ── send password reset email ────────────────────────────────────────────────
@router.post("/admin/users/reset-password")
async def send_reset(req: IdentifierReq):
    ident = _ident(req.identifier)
    try:
        row = _db_fetchone(
            "SELECT public.create_password_reset_token(%s::text,%s::text,%s::int) AS token",
            ("password", ident, 3600),
        )
    except Exception as exc:
        logger.error(f"reset token creation failed: {exc}")
        raise HTTPException(500, "Could not create reset token")
    if not row or not row[0]:
        raise HTTPException(404, "No active credential for that identifier")
    token = str(row[0])
    try:
        send_email(
            to=ident,
            subject="Conscestra CRM — Password reset",
            body_html=(
                "<div style='font-family:sans-serif;max-width:480px;margin:auto'>"
                "<h2 style='color:#0d9488'>Conscestra CRM</h2>"
                "<p>An administrator initiated a password reset for your account.</p>"
                "<p>Use this code on the <b>Reset</b> tab of the sign-in page, then choose a new password:</p>"
                f"<div style='font-size:1.1rem;font-weight:700;background:#f0fdfa;border-radius:8px;"
                f"padding:14px;text-align:center;letter-spacing:.04em'>{token}</div>"
                "<p style='color:#6b7280;font-size:.85rem'>This code expires in 1 hour. "
                "If you didn't expect this, you can ignore it.</p></div>"
            ),
            body_text=f"Password reset code: {token} (expires in 1 hour).",
        )
    except Exception as exc:
        logger.warning(f"reset email send failed for {ident}: {exc}")
        # Still return the token so the admin can convey it out-of-band (local/dev).
        return {"success": True, "emailed": False,
                "message": "Reset token created, but the email failed to send (check SMTP).",
                "reset_token": token}
    return {"success": True, "emailed": True, "message": f"Reset email sent to {ident}."}
