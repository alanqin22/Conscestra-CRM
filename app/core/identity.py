"""Identity Resolution — the "One Person" spine of the Unified Communication Layer.

    resolve(channel, handle) -> Identity

Maps a channel-specific handle (phone, email, Slack/Teams user id, WhatsApp
number) to ONE CRM party, so every channel and adapter threads into one
relationship + one memory. This is the primitive the vision hinges on:

    WhatsApp +1…  ┐
    Email a@b.c   ├─▶  resolve()  ─▶  One person · one account · one memory
    Slack alan.q  ┘

EXTERNAL vs INTERNAL — the boundary the Orchestrator polices:
  • EXTERNAL channels (voice/sms/email/whatsapp/webchat/social/mobile) resolve to
    a CONTACT — the customer↔business world.
  • INTERNAL channels (teams/slack/internal email) resolve to an EMPLOYEE/OWNER —
    the employee↔business-intelligence world.
The resolver only says WHO and WHICH SIDE; what may cross the boundary is the
Orchestrator's call.

RESOLUTION ORDER (most reliable first):
  1. channel_identities — an explicit (channel, handle) → party mapping. The ONLY
     reliable source for opaque platform ids (Slack/Teams). Populated by adapters
     / OTP via register(). Carries its own `verified` flag.
  2. email — contacts.email / owners.email (case-insensitive).
  3. phone — normalized E.164 against contacts.phone / owners.phone.

VERIFIED (identity we can TRUST, gating memory recall — same posture as
customer_memory): true only for a channel_identities row flagged verified, an
OTP-verified contact email, or any internal (directory) match. Caller-ID and
typed handles are unverified — claimable by anyone, so memory must not leak.

DEGRADES GRACEFULLY: if the channel_identities table isn't migrated yet, steps
2–3 still resolve by email/phone. See sql/unified_comms_identity.sql.

CONFIG (env)
  IDENTITY_ENABLED  1   kill switch (off → resolve() always returns unresolved)
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("identity")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("IDENTITY_ENABLED", "1")

# Channel → which side of the business a conversation on it belongs to.
INTERNAL_CHANNELS = {"teams", "slack", "internal_email", "internal"}
# Channels whose handle is a phone number (normalize to E.164 before matching).
_PHONE_CHANNELS = {"voice", "sms", "whatsapp"}


def channel_scope(channel: str) -> str:
    """'internal' for staff channels (Teams/Slack), else 'external' (customer)."""
    return "internal" if (channel or "").strip().lower() in INTERNAL_CHANNELS \
        else "external"


@dataclass
class Identity:
    resolved: bool
    scope: str                      # 'external' | 'internal'
    channel: str
    handle: str                     # normalized handle actually matched on
    party_type: Optional[str]       # 'contact' | 'employee' | None
    party_id: Optional[str]
    account_id: Optional[str]
    verified: bool
    match_method: str               # 'channel_identity' | 'email' | 'phone' | 'none'
    confidence: float
    display_name: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# Handle normalization
# ============================================================================

def _normalize_handle(channel: str, handle: str) -> str:
    """Canonicalize a handle so the same person matches regardless of format:
    emails lowercased, phone-channel handles to E.164, platform ids left as-is."""
    c = (channel or "").strip().lower()
    h = (handle or "").strip()
    if not h:
        return h
    if "@" in h:
        return h.lower()
    looks_phone = c in _PHONE_CHANNELS or h.startswith("+") or \
        h.replace(" ", "").replace("-", "").replace("(", "").replace(")", "").isdigit()
    if looks_phone:
        try:
            from app.core.telephony import normalize_phone
            return normalize_phone(h) or h
        except Exception:
            return h
    return h            # opaque platform user id (Slack/Teams) — match verbatim


def _unresolved(channel: str, handle: str, scope: str) -> Identity:
    return Identity(resolved=False, scope=scope, channel=channel, handle=handle,
                    party_type=None, party_id=None, account_id=None,
                    verified=False, match_method="none", confidence=0.0)


# ============================================================================
# Lookups (each defensive — a missing table/column never raises)
# ============================================================================

def _lookup_channel_identity(channel: str, handle: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT party_type, party_id::text, account_id::text, verified, scope
                   FROM channel_identities
                   WHERE channel=%s AND handle=%s LIMIT 1""",
                (channel.lower(), handle))
            r = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], r)) if r else None
    except Exception as exc:            # table not migrated yet → skip this tier
        conn.rollback()
        logger.debug(f"[identity] channel_identities lookup skipped: {exc}")
        return None
    finally:
        conn.close()


def _lookup_contact_by_email(email: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT contact_id::text AS party_id, account_id::text,
                          COALESCE(is_email_verified, false) AS verified,
                          NULLIF(TRIM(COALESCE(first_name,'')||' '||
                                      COALESCE(last_name,'')), '') AS display_name
                   FROM contacts
                   WHERE lower(email)=%s AND COALESCE(is_deleted,false)=false
                   ORDER BY COALESCE(is_email_verified,false) DESC, updated_at DESC
                   LIMIT 1""", (email,))
            r = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], r)) if r else None
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[identity] contact email lookup failed: {exc}")
        return None
    finally:
        conn.close()


def _lookup_contact_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT contact_id::text AS party_id, account_id::text,
                          NULLIF(TRIM(COALESCE(first_name,'')||' '||
                                      COALESCE(last_name,'')), '') AS display_name
                   FROM contacts
                   WHERE phone=%s AND COALESCE(is_deleted,false)=false
                   ORDER BY updated_at DESC LIMIT 1""", (phone,))
            r = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], r)) if r else None
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[identity] contact phone lookup failed: {exc}")
        return None
    finally:
        conn.close()


def _lookup_owner(by: str, value: str) -> Optional[Dict[str, Any]]:
    """Internal directory match against owners (employees). `by` is 'email'|'phone'."""
    col = "lower(email)" if by == "email" else "phone"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT owner_id::text AS party_id,
                           NULLIF(TRIM(COALESCE(first_name,'')||' '||
                                       COALESCE(last_name,'')), '') AS display_name
                    FROM owners
                    WHERE {col}=%s AND COALESCE(is_active,true)=true
                    ORDER BY updated_at DESC LIMIT 1""", (value,))
            r = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], r)) if r else None
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[identity] owner lookup failed: {exc}")
        return None
    finally:
        conn.close()


# ============================================================================
# Resolve
# ============================================================================

def resolve(channel: str, handle: str) -> Identity:
    """Map (channel, handle) → one CRM party. Never raises — an unknown handle
    returns an unresolved Identity (the conversation still threads anonymously)."""
    scope = channel_scope(channel)
    h = _normalize_handle(channel, handle)
    if not ENABLED or not h:
        return _unresolved(channel, h, scope)

    # 1. explicit mapping (the only reliable source for Slack/Teams ids)
    ci = _lookup_channel_identity(channel, h)
    if ci and ci.get("party_id"):
        return Identity(
            resolved=True, scope=ci.get("scope") or scope, channel=channel, handle=h,
            party_type=ci["party_type"], party_id=ci["party_id"],
            account_id=ci.get("account_id"), verified=bool(ci.get("verified")),
            match_method="channel_identity", confidence=0.99)

    internal = scope == "internal"

    # 2. email
    if "@" in h:
        if internal:
            r = _lookup_owner("email", h)
            if r:
                return Identity(True, scope, channel, h, "employee", r["party_id"],
                                None, True, "email", 0.9, r.get("display_name"))
        else:
            r = _lookup_contact_by_email(h)
            if r:
                return Identity(True, scope, channel, h, "contact", r["party_id"],
                                r.get("account_id"), bool(r.get("verified")),
                                "email", 0.9, r.get("display_name"))
        return _unresolved(channel, h, scope)

    # 3. phone (E.164) — caller-ID is spoofable, so NEVER verified on its own
    if h.startswith("+"):
        if internal:
            r = _lookup_owner("phone", h)
            if r:
                return Identity(True, scope, channel, h, "employee", r["party_id"],
                                None, False, "phone", 0.75, r.get("display_name"))
        else:
            r = _lookup_contact_by_phone(h)
            if r:
                return Identity(True, scope, channel, h, "contact", r["party_id"],
                                r.get("account_id"), False, "phone", 0.75,
                                r.get("display_name"))
        return _unresolved(channel, h, scope)

    # Opaque platform id (Slack/Teams) with no channel_identities row → unknown.
    return _unresolved(channel, h, scope)


def register(channel: str, handle: str, party_type: str, party_id: str,
             account_id: Optional[str] = None, verified: bool = False,
             scope: Optional[str] = None) -> Dict[str, Any]:
    """Persist a (channel, handle) → party mapping so future lookups are O(1) and
    opaque platform ids resolve at all. Upsert on (channel, handle). Adapters call
    this after an OTP verification or once they've resolved a handle another way.
    Returns {'ok', 'linked'} — {'ok': False} when the table isn't migrated yet."""
    h = _normalize_handle(channel, handle)
    sc = scope or channel_scope(channel)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO channel_identities
                     (channel, handle, party_type, party_id, account_id, verified, scope)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (channel, handle) DO UPDATE SET
                     party_type=EXCLUDED.party_type, party_id=EXCLUDED.party_id,
                     account_id=EXCLUDED.account_id,
                     verified=channel_identities.verified OR EXCLUDED.verified,
                     scope=EXCLUDED.scope, updated_at=now()
                   RETURNING identity_id::text""",
                (channel.lower(), h, party_type, party_id, account_id, verified, sc))
            iid = cur.fetchone()[0]
        conn.commit()
        logger.info(f"[identity] linked {channel}:{h} → {party_type} {party_id[:8]} "
                    f"(verified={verified})")
        return {"ok": True, "linked": iid, "channel": channel, "handle": h}
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[identity] register failed (table migrated?): {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["identity"])


@router.get("/identity/resolve")
def identity_resolve(channel: str, handle: str):
    return resolve(channel, handle).as_dict()


@router.post("/identity/link")
def identity_link(body: Dict[str, Any]):
    b = body or {}
    return register(str(b.get("channel") or ""), str(b.get("handle") or ""),
                    str(b.get("party_type") or "contact"),
                    str(b.get("party_id") or ""), b.get("account_id"),
                    bool(b.get("verified")), b.get("scope"))


@router.get("/identity/status")
def identity_status():
    has_table = False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.channel_identities') IS NOT NULL")
            has_table = bool(cur.fetchone()[0])
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return {"enabled": ENABLED, "channel_identities_table": has_table,
            "internal_channels": sorted(INTERNAL_CHANNELS)}
