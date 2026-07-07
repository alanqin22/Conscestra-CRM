"""Inbound→CRM bridge — turn every inbound email into first-class CRM signal.

WHY THIS EXISTS
---------------
Inbound email used to be logged to audit_log only, which the rest of the agent
network can't see. Everything engagement-driven keys off `activities` rows with
direction='inbound': the lead_followup cadence's 'engaged' exit, churn_save's
're-engaged' exit, marketing campaign reply attribution, and the intelligence
profile's engagement recency. Without this bridge, a customer replying to a
win-back offer would NOT stop the escalation play.

WHAT IT DOES (called from auto_reply.process_inbound_email, best-effort)
------------------------------------------------------------------------
  1. Match the sender to a CONTACT (→ its account) or a LEAD by email.
  2. Insert a completed inbound `activities` row on that entity
     (idempotent: same sender+subject within 1h is one touch).
  3. Emit an `email.received` bus event — the Email agent's handler escalates
     complaints (blackboard note + owner task); other intents are acked
     because the activity row itself is the signal.

Unmatched senders (no contact/lead) are left to audit_log/sentiment only.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.core.database import get_connection

logger = logging.getLogger("inbound_bridge")

_PREVIEW_CHARS = 500


def _resolve_sender_sync(sender: str) -> Optional[Dict[str, Any]]:
    """email → {'kind': 'account'|'lead', ids, owner_id, display} or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.contact_id::text, c.account_id::text, a.owner_id,
                          COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' ||
                                   COALESCE(c.last_name,'')), ''), a.account_name)
                   FROM contacts c
                   LEFT JOIN accounts a ON a.account_id = c.account_id
                   WHERE lower(c.email) = %s
                     AND COALESCE(c.is_deleted, false) = false
                   ORDER BY c.created_at LIMIT 1""",
                (sender,),
            )
            r = cur.fetchone()
            if r and r[1]:
                return {"kind": "account", "contact_id": r[0], "account_id": r[1],
                        "owner_id": r[2], "display": r[3]}
            cur.execute(
                """SELECT lead_id::text, owner_id,
                          COALESCE(NULLIF(TRIM(COALESCE(first_name,'') || ' ' ||
                                   COALESCE(last_name,'')), ''), company)
                   FROM leads
                   WHERE lower(email) = %s AND COALESCE(is_deleted, false) = false
                   ORDER BY created_at LIMIT 1""",
                (sender,),
            )
            r = cur.fetchone()
            if r:
                return {"kind": "lead", "lead_id": r[0], "owner_id": r[1],
                        "display": r[2]}
            return None
    finally:
        conn.close()


def record_inbound(email: Dict[str, Any], intent: str) -> Dict[str, Any]:
    """Bridge one inbound email into the CRM. Never raises to the caller's
    hot path — the poller must survive any DB hiccup."""
    try:
        return _record_inbound(email, intent)
    except Exception as exc:
        logger.warning(f"[inbound_bridge] failed: {exc}")
        return {"status": "error", "error": str(exc)[:200]}


def _record_inbound(email: Dict[str, Any], intent: str) -> Dict[str, Any]:
    from app.agents.email.auto_reply import _extract_email_addr  # loaded by caller

    sender = _extract_email_addr(email.get("from", ""))
    subject = (email.get("subject") or "(no subject)").strip()[:180]
    preview = str(email.get("body_text") or email.get("body")
                  or email.get("preview") or "").strip()[:_PREVIEW_CHARS]

    who = _resolve_sender_sync(sender)
    if not who:
        return {"status": "unmatched", "sender": sender}

    entity_type = who["kind"]                              # 'account' | 'lead'
    entity_id = who.get("account_id") or who.get("lead_id")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # One touch per (entity, sender, subject) per hour — IMAP redelivery
            # and rate-limited repeat processing must not stack rows.
            cur.execute(
                f"""SELECT 1 FROM activities
                    WHERE {'account_id' if entity_type == 'account' else 'lead_id'}
                          = %s::uuid
                      AND direction = 'inbound' AND subject = %s
                      AND created_at > now() - interval '1 hour'
                    LIMIT 1""",
                (entity_id, f"Inbound: {subject}"),
            )
            if cur.fetchone():
                return {"status": "duplicate", "sender": sender,
                        entity_type: entity_id}

            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, direction, channel,
                      owner_id, related_type, related_id, account_id, lead_id,
                      completed_at, created_at, updated_at)
                   VALUES ('email', 'completed', %s, %s, 'inbound', 'email',
                           %s, %s, %s::uuid, %s::uuid, %s::uuid,
                           now(), now(), now())
                   RETURNING activity_id::text""",
                (f"Inbound: {subject}",
                 f"From {who['display'] or sender} <{sender}> · intent: {intent}"
                 + (f"\n\n{preview}" if preview else ""),
                 who.get("owner_id"), entity_type, entity_id,
                 who.get("account_id"), who.get("lead_id")),
            )
            activity_id = cur.fetchone()[0]

            cur.execute(
                "SELECT emit_event(%s,%s,%s,%s,%s,%s)",
                ("email.received", entity_type, entity_id,
                 json.dumps({"context": {
                     "from": sender, "subject": subject, "intent": intent,
                     "activity_id": activity_id,
                     "contact_id": who.get("contact_id")}}),
                 None, "email_agent"),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"[inbound_bridge] {sender} → {entity_type} {entity_id} "
                f"(intent={intent}, activity={activity_id[:8]})")
    return {"status": "ok", "sender": sender, "intent": intent,
            entity_type: entity_id, "activity_id": activity_id}
