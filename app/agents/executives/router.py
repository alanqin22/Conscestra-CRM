"""
Executives console API — CRUD for the `executives` table (the human-leadership
interface AI agents resolve recipients/owners from). Admin-gated, direct
parameterized SQL (mirrors admin_users). Drives executives-mgmt.html.
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth_dep import require_admin
from app.core.database import get_connection

logger = logging.getLogger("executives")

router = APIRouter(tags=["executives"], dependencies=[Depends(require_admin)])

# Whitelisted writable columns (never interpolate client keys into SQL).
_WRITABLE = [
    "role_code", "full_name", "title", "email", "phone", "timezone",
    "notification_categories", "approval_authority_limit",
    "is_active", "auto_email_enabled", "start_date", "end_date", "notes",
]


def _fetchall(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _exec(sql: str, params: tuple = ()):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            out = cur.fetchone() if cur.description else None
            conn.commit()
            return out
    finally:
        conn.close()


def _clean(payload: Dict[str, Any]) -> Dict[str, Any]:
    f = payload or {}
    nc = f.get("notification_categories") or []
    if isinstance(nc, str):
        nc = [s.strip() for s in nc.replace(",", "\n").splitlines() if s.strip()]
    nc = [str(s).strip().lower() for s in nc if str(s).strip()]
    fields: Dict[str, Any] = {
        "role_code": (f.get("role_code") or "").strip().upper(),
        "full_name": (f.get("full_name") or "").strip(),
        "title": (f.get("title") or "").strip() or None,
        "email": (f.get("email") or "").strip(),
        "phone": (f.get("phone") or "").strip() or None,
        "timezone": (f.get("timezone") or "America/New_York").strip(),
        "notification_categories": nc,                       # psycopg2 -> text[]
        "approval_authority_limit": f.get("approval_authority_limit") or None,
        "is_active": bool(f.get("is_active", True)),
        "auto_email_enabled": bool(f.get("auto_email_enabled", True)),
        "start_date": f.get("start_date") or None,
        "end_date": f.get("end_date") or None,
        "notes": (f.get("notes") or "").strip() or None,
    }
    if not fields["role_code"] or not fields["full_name"] or not fields["email"]:
        raise HTTPException(400, "role_code, full_name and email are required")
    return fields


@router.get("/executives")
def list_executives():
    rows = _fetchall(
        "SELECT executive_id, role_code, full_name, title, email, phone, timezone, "
        "       notification_categories, approval_authority_limit, is_active, "
        "       auto_email_enabled, start_date, end_date, notes, updated_at "
        "FROM executives ORDER BY is_active DESC, role_code, full_name")
    return {"executives": rows}


@router.post("/executives")
async def upsert_executive(payload: Dict[str, Any]):
    fields = _clean(payload)
    eid = (payload or {}).get("executive_id")
    cols = list(fields.keys())
    vals = tuple(fields.values())
    if eid:
        sets = ", ".join(f"{c}=%s" for c in cols) + ", updated_at=now()"
        _exec(f"UPDATE executives SET {sets} WHERE executive_id=%s", vals + (eid,))
        logger.info(f"[executives] updated {fields['role_code']} {fields['email']}")
        return {"status": "updated", "executive_id": eid}
    placeholders = ", ".join(["%s"] * len(cols))
    rid = _exec(
        f"INSERT INTO executives ({', '.join(cols)}) VALUES ({placeholders}) RETURNING executive_id",
        vals)
    logger.info(f"[executives] created {fields['role_code']} {fields['email']}")
    return {"status": "created", "executive_id": str(rid[0]) if rid else None}


@router.delete("/executives/{executive_id}")
def delete_executive(executive_id: str):
    _exec("DELETE FROM executives WHERE executive_id=%s", (executive_id,))
    return {"status": "deleted", "executive_id": executive_id}


# ── Company profile (single row) — CASL sender identity ────────────────────
# Read by app/core/consent.py for the commercial-email footer; edited from the
# Company Profile card on executives-mgmt.html.

def _ensure_company_profile(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS company_profile ("
        " profile_id INT PRIMARY KEY DEFAULT 1 CHECK (profile_id = 1),"
        " company_name TEXT NOT NULL DEFAULT 'Conscestra CRM',"
        " mailing_address TEXT NOT NULL DEFAULT '',"
        " contact_email TEXT NOT NULL DEFAULT 'info@agentorc.ca',"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " updated_by TEXT)")
    cur.execute(
        "INSERT INTO company_profile (profile_id) VALUES (1) "
        "ON CONFLICT (profile_id) DO NOTHING")


@router.get("/company-profile")
def get_company_profile():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _ensure_company_profile(cur)
            cur.execute("SELECT company_name, mailing_address, contact_email, "
                        "updated_at FROM company_profile WHERE profile_id=1")
            r = cur.fetchone()
        conn.commit()
        return {"company_name": r[0], "mailing_address": r[1],
                "contact_email": r[2], "updated_at": r[3]}
    finally:
        conn.close()


@router.put("/company-profile")
def update_company_profile(payload: Dict[str, Any]):
    f = payload or {}
    name = (f.get("company_name") or "").strip()
    addr = (f.get("mailing_address") or "").strip()
    email = (f.get("contact_email") or "").strip()
    if not name or not addr:
        raise HTTPException(400, "company_name and mailing_address are required "
                                 "(CASL sender identification)")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _ensure_company_profile(cur)
            cur.execute(
                "UPDATE company_profile SET company_name=%s, mailing_address=%s, "
                "contact_email=%s, updated_at=now() WHERE profile_id=1",
                (name, addr, email or "info@agentorc.ca"))
        conn.commit()
        logger.info(f"[executives] company profile updated: {name} / {addr[:60]}")
        return {"status": "updated"}
    finally:
        conn.close()
