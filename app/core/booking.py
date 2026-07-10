"""Real meeting booking — the agent books, not just tasks (advanced #3).

The autonomous-SDR promise: when a prospect engages, the agent checks the
rep's REAL calendar availability (the CRM's own meeting activities), books a
concrete slot, and sends the prospect a calendar invite — a hot reply becomes
a confirmed meeting with zero human latency.

    availability   free business-hour slots for an owner (ET, Mon–Fri 9–17),
                   conflict-checked against their existing meetings, aligned
                   to the customer's learned preferred hour when known
    book           insert the meeting activity (start_at/end_at), post a
                   `meeting_booked` blackboard note, and deliver an invite:
                   an HMAC-signed public .ics link (same pattern as
                   unsubscribe/decision links) emailed to the prospect —
                   ONLY under AGENT_BUS_AUTOSEND and only to verified,
                   non-placeholder addresses; otherwise the owner gets a
                   send-manually task and the link is returned
    undo           governance can cancel a booked meeting (status→cancelled)

Consumers: the lead_followup cadence's `book_meeting` step (auto-books when
BOOKING_AUTOBOOK=1, falls back to the urgent task), A2A `meeting.book`
(governed write), and /booking/* endpoints.

CONFIG (env)
  BOOKING_AUTOBOOK    1    cadence auto-booking kill switch (module always
                           serves availability/book for humans + A2A)
  BOOKING_SLOT_MIN    30   default meeting length (minutes)
  BOOKING_DAYS        5    availability search horizon (business days)
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.core.database import get_connection

logger = logging.getLogger("booking")

ET = ZoneInfo("America/New_York")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


AUTOBOOK = _flag("BOOKING_AUTOBOOK", "1")
SLOT_MIN = int(os.getenv("BOOKING_SLOT_MIN", "30"))
DAYS = int(os.getenv("BOOKING_DAYS", "5"))

BUSINESS_START, BUSINESS_END = 9, 17     # ET
LEAD_TIME_HOURS = 2                      # earliest bookable slot


# ============================================================================
# AVAILABILITY
# ============================================================================

def _candidate_slots(days: int, duration_min: int,
                     preferred_hour: Optional[int]) -> List[datetime]:
    """Business-hour slot starts (ET-aware datetimes) over the next N business
    days, earliest first — preferred-hour slots first within each day."""
    now = datetime.now(ET)
    earliest = now + timedelta(hours=LEAD_TIME_HOURS)
    out: List[datetime] = []
    day = now.date() - timedelta(days=1)
    found_days = 0
    for _ in range(days * 4 + 7):         # safety bound over weekends/holidays
        if found_days >= days:
            break
        day += timedelta(days=1)
        if day.weekday() >= 5:            # Sat/Sun
            continue
        day_slots = []
        for hour in range(BUSINESS_START, BUSINESS_END):
            for minute in (0, 30):
                if hour * 60 + minute + duration_min > BUSINESS_END * 60:
                    continue              # would run past closing
                start = datetime(day.year, day.month, day.day, hour, minute,
                                 tzinfo=ET)
                if start >= earliest:
                    day_slots.append(start)
        if day_slots:
            if preferred_hour is not None and BUSINESS_START <= preferred_hour < BUSINESS_END:
                day_slots.sort(key=lambda s: (s.hour != preferred_hour, s))
            out.extend(day_slots)
            found_days += 1
    return out


def _conflicts(owner_id, start: datetime, end: datetime) -> bool:
    """The owner already has a live meeting overlapping this window.
    NULL owner never conflicts (nobody's calendar to collide with)."""
    if not owner_id:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM activities
                   WHERE owner_id = %s AND type = 'meeting'
                     AND status NOT IN ('completed', 'cancelled')
                     AND COALESCE(start_at, due_at) < %s
                     AND COALESCE(end_at, COALESCE(start_at, due_at)
                                          + make_interval(mins => %s)) > %s
                   LIMIT 1""",
                (owner_id, end, SLOT_MIN, start))
            return cur.fetchone() is not None
    finally:
        conn.close()


def availability(owner_id, days: int = DAYS, duration_min: int = SLOT_MIN,
                 preferred_hour: Optional[int] = None,
                 limit: int = 10) -> List[str]:
    """Free slot starts (ISO, ET) for an owner."""
    out = []
    for start in _candidate_slots(days, duration_min, preferred_hour):
        if not _conflicts(owner_id, start,
                          start + timedelta(minutes=duration_min)):
            out.append(start.isoformat())
            if len(out) >= limit:
                break
    return out


# ============================================================================
# ENTITY LOOKUP — who are we meeting, and can we reach them?
# ============================================================================

def _entity_info(entity_type: str, entity_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if entity_type == "lead":
                cur.execute(
                    """SELECT COALESCE(NULLIF(TRIM(COALESCE(first_name,'')||' '||
                              COALESCE(last_name,'')),''), company),
                              email, owner_id
                       FROM leads WHERE lead_id=%s::uuid AND deleted_at IS NULL""",
                    (entity_id,))
                r = cur.fetchone()
                if not r:
                    return None
                # leads have no OTP verification — invites only send under
                # AUTOSEND *and* a non-placeholder address (verified=False
                # blocks _is_real_email, so leads get the manual-send task).
                return {"display": r[0], "email": r[1], "verified": False,
                        "owner_id": r[2], "preferred_hour": None,
                        "lead_id": entity_id, "account_id": None}
            if entity_type == "account":
                cur.execute(
                    """SELECT a.account_name, a.owner_id, i.preferred_hour,
                              c.contact_id::text, c.email,
                              COALESCE(c.is_email_verified, false)
                       FROM accounts a
                       LEFT JOIN account_intelligence i ON i.account_id = a.account_id
                       LEFT JOIN LATERAL (
                           SELECT contact_id, email, is_email_verified
                           FROM contacts c
                           WHERE c.account_id = a.account_id
                             AND COALESCE(c.is_deleted,false) = false
                             AND c.email IS NOT NULL AND c.email <> ''
                           ORDER BY COALESCE(c.is_email_verified,false) DESC,
                                    c.created_at
                           LIMIT 1) c ON true
                       WHERE a.account_id=%s::uuid
                         AND COALESCE(a.is_deleted,false)=false""",
                    (entity_id,))
                r = cur.fetchone()
                if not r:
                    return None
                return {"display": r[0], "owner_id": r[1],
                        "preferred_hour": r[2], "contact_id": r[3],
                        "email": r[4], "verified": bool(r[5]),
                        "lead_id": None, "account_id": entity_id}
            return None
    finally:
        conn.close()


# ============================================================================
# ICS INVITE — HMAC-signed public link (no attachment plumbing needed)
# ============================================================================

def _link_secret() -> bytes:
    s = (os.getenv("BOOKING_LINK_SECRET") or os.getenv("UNSUBSCRIBE_SECRET")
         or os.getenv("ADMIN_API_TOKEN") or "")
    return s.encode("utf-8")


def invite_token(activity_id: str) -> str:
    return _hmac.new(_link_secret(), f"ics:{activity_id}".encode("utf-8"),
                     hashlib.sha256).hexdigest()[:32]


def _verify_token(activity_id: str, token: str) -> bool:
    if not _link_secret() or not token:
        return False
    return _hmac.compare_digest(invite_token(activity_id), token)


def invite_url(activity_id: str) -> str:
    base = (os.getenv("APP_URL", "") or "http://localhost:8000").rstrip("/")
    return f"{base}/booking/invite?a={activity_id}&t={invite_token(activity_id)}"


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", r"\;") \
                    .replace(",", r"\,").replace("\n", r"\n")


def build_ics(activity_id: str, summary: str, start: datetime, end: datetime,
              description: str = "") -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    dtstamp = datetime.now(timezone.utc).strftime(fmt)
    return "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Conscestra CRM//Booking//EN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{activity_id}@conscestra-crm",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{start.astimezone(timezone.utc).strftime(fmt)}",
        f"DTEND:{end.astimezone(timezone.utc).strftime(fmt)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR", ""])


# ============================================================================
# BOOK
# ============================================================================

def book(entity_type: str, entity_id: str, start_iso: Optional[str] = None,
         duration_min: int = SLOT_MIN, booked_by: str = "agent",
         notes: str = "") -> Dict[str, Any]:
    """Book a real meeting: pick/validate the slot, insert the meeting
    activity, post the blackboard note, deliver the invite (AUTOSEND +
    verified address) or task the owner to send it."""
    info = _entity_info(entity_type, entity_id)
    if not info:
        return {"ok": False, "error": f"{entity_type} {entity_id} not found"}

    if start_iso:
        start = datetime.fromisoformat(start_iso)
        if start.tzinfo is None:
            start = start.replace(tzinfo=ET)
        end = start + timedelta(minutes=int(duration_min))
        if _conflicts(info["owner_id"], start, end):
            return {"ok": False, "error": "requested slot conflicts with an "
                                          "existing meeting on the owner's calendar"}
    else:
        free = availability(info["owner_id"], preferred_hour=info.get("preferred_hour"),
                            duration_min=int(duration_min), limit=1)
        if not free:
            return {"ok": False, "error": "no free business-hour slot in the "
                                          "search horizon"}
        start = datetime.fromisoformat(free[0])
        end = start + timedelta(minutes=int(duration_min))

    summary = f"Meeting with {info['display']} (auto-booked)"
    description = (f"Booked by {booked_by} via Conscestra CRM."
                   + (f" {notes}" if notes else ""))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, direction, channel,
                      owner_id, related_type, related_id, account_id, lead_id,
                      start_at, end_at, due_at, created_at, updated_at)
                   VALUES ('meeting', 'open', %s, %s, 'outbound', 'video',
                           %s, %s, %s::uuid, %s::uuid, %s::uuid,
                           %s, %s, %s, now(), now())
                   RETURNING activity_id::text""",
                (summary, description, info["owner_id"], entity_type, entity_id,
                 info.get("account_id"), info.get("lead_id"), start, end, start))
            activity_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    link = invite_url(activity_id)
    when = start.astimezone(ET).strftime("%A %B %d, %I:%M %p ET")

    try:
        from app.core import blackboard
        blackboard.post(entity_type, entity_id, "activities", "meeting_booked",
                        f"Meeting auto-booked with {info['display']} — {when}",
                        {"activity_id": activity_id, "start": start.isoformat(),
                         "booked_by": booked_by}, 0.9, "info", 24 * 14)
    except Exception as exc:
        logger.debug(f"[booking] blackboard note skipped: {exc}")

    # Deliver the invite — outbound email stays behind the platform's
    # established gates: AUTOSEND on AND a verified, non-placeholder address.
    emailed = False
    from app.core import agent_bus
    if agent_bus.AUTOSEND and agent_bus._is_real_email(info.get("email"),
                                                       info.get("verified", False)):
        try:
            from app.agents.email.smtp_imap import send_email
            res = send_email(
                to=info["email"],
                subject=f"Meeting confirmed — {when}",
                body_html=(f"<p>Hi {info['display']},</p>"
                           f"<p>Your meeting with the Conscestra CRM team is "
                           f"confirmed for <b>{when}</b> ({int(duration_min)} minutes).</p>"
                           f'<p><a href="{link}" style="background:#0d9488;color:#fff;'
                           f'padding:10px 22px;border-radius:6px;text-decoration:none;'
                           f'font-weight:700;">Add to calendar (.ics)</a></p>'
                           f"<p>Need a different time? Just reply to this email.</p>"
                           f"<p>The Conscestra CRM Team | info@agentorc.ca</p>"),
                body_text=(f"Hi {info['display']},\n\nYour meeting is confirmed for "
                           f"{when} ({int(duration_min)} minutes).\n\n"
                           f"Add to calendar: {link}\n\n"
                           f"Need a different time? Just reply to this email.\n\n"
                           f"The Conscestra CRM Team | info@agentorc.ca"),
            )   # transactional (a meeting the recipient asked for), not commercial
            emailed = bool(res.get("success"))
        except Exception as exc:
            logger.warning(f"[booking] invite email failed: {exc}")
    if not emailed:
        # Invite couldn't (or mustn't) go out automatically — the human closes
        # that last yard; the booked slot is already protected either way.
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO activities
                         (type, status, subject, description, due_at, direction,
                          channel, owner_id, related_type, related_id,
                          account_id, lead_id, created_at, updated_at)
                       VALUES ('task', 'open', %s, %s,
                               now() + interval '4 hours', 'outbound', 'email',
                               %s, %s, %s::uuid, %s::uuid, %s::uuid, now(), now())""",
                    (f"Send meeting invite — {info['display']} ({when})",
                     f"Meeting auto-booked for {when} but the invite was NOT "
                     f"emailed (autosend off or unverified address "
                     f"{info.get('email') or 'n/a'}). Send it manually: {link}",
                     info["owner_id"], entity_type, entity_id,
                     info.get("account_id"), info.get("lead_id")))
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"[booking] manual-send task failed: {exc}")

    logger.info(f"[booking] booked {entity_type}/{entity_id} at "
                f"{start.isoformat()} (activity {activity_id[:8]}, "
                f"emailed={emailed})")
    return {"ok": True, "activity_id": activity_id, "display": info["display"],
            "start": start.isoformat(), "end": end.isoformat(), "when": when,
            "invite_url": link, "emailed": emailed}


def cancel(activity_id: str, reason: str = "cancelled") -> Dict[str, Any]:
    """Cancel a booked meeting (governance undo handler)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE activities SET status='cancelled',
                       description = COALESCE(description,'') || %s,
                       updated_at=now()
                   WHERE activity_id=%s::uuid AND type='meeting'
                     AND status NOT IN ('completed','cancelled')
                   RETURNING subject""",
                (f"\n[{reason}]", activity_id))
            r = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not r:
        return {"ok": False, "error": "meeting not found or already closed"}
    logger.info(f"[booking] cancelled meeting {activity_id[:8]} ({reason})")
    return {"ok": True, "activity_id": activity_id, "status": "cancelled"}


def book_sp(p: Dict[str, Any]) -> Dict[str, Any]:
    """A2A structured handler for meeting.book."""
    et = str(p.get("entity_type") or ("lead" if p.get("lead_id") else "account"))
    eid = str(p.get("entity_id") or p.get("lead_id") or p.get("account_id") or "")
    return book(et, eid, p.get("start"), int(p.get("duration_min", SLOT_MIN)),
                booked_by=str(p.get("booked_by", "a2a")),
                notes=str(p.get("notes", "")))


# ============================================================================
# Endpoints
# ============================================================================

router = APIRouter(tags=["booking"])


@router.get("/booking/availability")
def booking_availability(owner_id: Optional[str] = None, days: int = DAYS,
                         duration_min: int = SLOT_MIN,
                         preferred_hour: Optional[int] = None):
    return {"owner_id": owner_id, "slot_minutes": duration_min,
            "slots": availability(owner_id, days, duration_min, preferred_hour)}


@router.post("/booking/book")
def booking_book(body: Dict[str, Any]):
    res = book_sp(body or {})
    if not res.get("ok"):
        raise HTTPException(status_code=422, detail=res.get("error"))
    return res


# Public: the signed .ics link from the invite email (token IS the auth).
public_router = APIRouter(tags=["booking-public"])


@public_router.get("/booking/invite")
def booking_invite(a: str = "", t: str = ""):
    if not _verify_token(a, t):
        raise HTTPException(status_code=403, detail="invalid invite link")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT subject, description, start_at, end_at
                   FROM activities WHERE activity_id=%s::uuid AND type='meeting'""",
                (a,))
            r = cur.fetchone()
    finally:
        conn.close()
    if not r or not r[2]:
        raise HTTPException(status_code=404, detail="meeting not found")
    ics = build_ics(a, r[0] or "Meeting", r[2],
                    r[3] or (r[2] + timedelta(minutes=SLOT_MIN)), r[1] or "")
    return Response(ics, media_type="text/calendar",
                    headers={"Content-Disposition":
                             'attachment; filename="meeting.ics"'})
