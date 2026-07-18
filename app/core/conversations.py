"""Unified Conversation Object — the "One Conversation" spine.

    ingest(InboundMessage) -> threads the message into ONE cross-channel conversation

Every channel is an interface; the conversation is the intelligence. A message
on WhatsApp and a later phone call or email by the same resolved person land in
the SAME open conversation — so the customer never has to repeat "as I explained
yesterday on WhatsApp…". The channel is temporary; the relationship is permanent.

HOW THREADING WORKS
  1. identity.resolve(channel, handle) → one party (external contact / internal
     employee) or unresolved.
  2. Find the OPEN conversation for that party (+scope), within a recency window
     (CONVO_WINDOW_HOURS). Resolved → keyed by party; unresolved → keyed by
     anon_key ('channel:handle') so anonymous senders still thread with
     themselves until identified.
  3. Append the message (tagged with the channel it arrived on) and roll the
     conversation's `channel` forward to wherever the person now is.

EXTERNAL (customer↔business) and INTERNAL (employee↔intelligence) conversations
are separated by `scope`; the Orchestrator decides what may cross the boundary.

On close() an EXTERNAL conversation is distilled into One Customer Memory
(customer_memory.remember) — the Conversation Object feeds the memory that every
agent reads back. Ties into [identity], [customer_memory], [conductor].

DEGRADES: the tables are optional at import time; ingest raises a clear error
only when actually called without them (see sql/unified_comms_conversations.sql).

CONFIG (env)
  CONVERSATIONS_ENABLED  1    kill switch
  CONVO_WINDOW_HOURS     72   an open conversation older than this starts fresh
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection
from app.core import identity

logger = logging.getLogger("conversations")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("CONVERSATIONS_ENABLED", "1")
WINDOW_HOURS = int(os.getenv("CONVO_WINDOW_HOURS", "72"))


@dataclass
class InboundMessage:
    """The common normalized envelope every channel adapter produces."""
    channel: str
    handle: str                              # sender's channel handle (phone/email/id)
    body: str
    direction: str = "inbound"               # inbound | outbound
    external_ref: Optional[str] = None       # channel-native message/thread id
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Threading core
# ============================================================================

def _find_open(cur, scope: str, party_id: Optional[str], anon_key: Optional[str]):
    """The most recent OPEN conversation for this party (or anon handle) inside
    the recency window, or None."""
    if party_id:
        cur.execute(
            """SELECT conversation_id::text FROM conversations
               WHERE status='open' AND scope=%s AND party_id=%s::uuid
                 AND last_message_at > now() - make_interval(hours => %s)
               ORDER BY last_message_at DESC LIMIT 1""",
            (scope, party_id, WINDOW_HOURS))
    else:
        cur.execute(
            """SELECT conversation_id::text FROM conversations
               WHERE status='open' AND scope=%s AND anon_key=%s
                 AND last_message_at > now() - make_interval(hours => %s)
               ORDER BY last_message_at DESC LIMIT 1""",
            (scope, anon_key, WINDOW_HOURS))
    r = cur.fetchone()
    return r[0] if r else None


def ingest(msg: InboundMessage) -> Dict[str, Any]:
    """Thread one message into a conversation. Resolves identity, finds-or-opens
    the conversation, appends the message. Returns the resolution + thread info.
    Never raises for an unknown sender — they thread anonymously."""
    if not ENABLED:
        return {"ok": False, "error": "conversations disabled"}

    ident = identity.resolve(msg.channel, msg.handle)
    scope = ident.scope
    party_type = ident.party_type if ident.resolved else None
    party_id = ident.party_id if ident.resolved else None
    account_id = ident.account_id if ident.resolved else None
    anon_key = None if party_id else f"{(msg.channel or '').lower()}:{ident.handle}"
    author = "employee" if scope == "internal" else "customer"
    if (msg.direction or "inbound") == "outbound":
        author = "agent"
    chan = (msg.channel or "").lower()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conv_id = _find_open(cur, scope, party_id, anon_key)
            is_new = conv_id is None
            if is_new:
                cur.execute(
                    """INSERT INTO conversations
                         (scope, party_type, party_id, account_id, anon_key, channel)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING conversation_id::text""",
                    (scope, party_type, party_id, account_id, anon_key, chan))
                conv_id = cur.fetchone()[0]
            else:
                # Roll the conversation forward to the current channel, and let a
                # now-resolved identity fill in party fields it was missing while
                # anonymous (COALESCE never overwrites a known value).
                cur.execute(
                    """UPDATE conversations SET
                         channel=%s,
                         party_type=COALESCE(party_type,%s),
                         party_id=COALESCE(party_id,%s::uuid),
                         account_id=COALESCE(account_id,%s::uuid),
                         anon_key=CASE WHEN %s::uuid IS NOT NULL THEN NULL ELSE anon_key END
                       WHERE conversation_id=%s::uuid""",
                    (chan, party_type, party_id, account_id, party_id, conv_id))

            cur.execute(
                """INSERT INTO conversation_messages
                     (conversation_id, channel, direction, author, handle, body,
                      external_ref, metadata)
                   VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s::jsonb)
                   RETURNING message_id::text""",
                (conv_id, chan, msg.direction or "inbound", author, ident.handle,
                 msg.body, msg.external_ref, json.dumps(msg.metadata or {})))
            mid = cur.fetchone()[0]

            cur.execute(
                """UPDATE conversations
                   SET message_count=message_count+1, last_message_at=now(), updated_at=now()
                   WHERE conversation_id=%s::uuid""", (conv_id,))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[conversations] ingest failed (tables migrated?): {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()

    logger.info(f"[conversations] {chan} msg → conv {conv_id[:8]} "
                f"({'new' if is_new else 'continued'}, party={party_type or 'anon'})")
    return {"ok": True, "conversation_id": conv_id, "message_id": mid,
            "is_new_conversation": is_new, "scope": scope,
            "resolved": ident.resolved, "party_type": party_type,
            "party_id": party_id, "account_id": account_id,
            "verified": ident.verified, "display_name": ident.display_name}


def append_outbound(conversation_id: str, channel: str, body: str,
                    author: str = "agent", intent: Optional[str] = None,
                    metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Record an OUTBOUND message (an agent/human reply) into a conversation and
    roll its channel forward. The write-side companion to ingest()."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO conversation_messages
                     (conversation_id, channel, direction, author, body, intent, metadata)
                   VALUES (%s::uuid,%s,'outbound',%s,%s,%s,%s::jsonb)
                   RETURNING message_id::text""",
                (conversation_id, channel.lower(), author, body, intent,
                 json.dumps(metadata or {})))
            mid = cur.fetchone()[0]
            cur.execute(
                """UPDATE conversations SET channel=%s, message_count=message_count+1,
                     last_message_at=now(), updated_at=now()
                   WHERE conversation_id=%s::uuid""", (channel.lower(), conversation_id))
        conn.commit()
        return {"ok": True, "message_id": mid, "conversation_id": conversation_id}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()


# ============================================================================
# Reads
# ============================================================================

def get(conversation_id: str, message_limit: int = 100) -> Dict[str, Any]:
    """The full conversation: header + messages (oldest first)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT conversation_id::text, scope, party_type, party_id::text,
                          account_id::text, channel, status, intent, message_count,
                          last_message_at, created_at
                   FROM conversations WHERE conversation_id=%s::uuid""",
                (conversation_id,))
            r = cur.fetchone()
            if not r:
                return {"ok": False, "error": "not found"}
            header = dict(zip([d[0] for d in cur.description], r))
            for k in ("last_message_at", "created_at"):
                header[k] = header[k].isoformat() if header[k] else None
            cur.execute(
                """SELECT channel, direction, author, body, intent, created_at
                   FROM conversation_messages
                   WHERE conversation_id=%s::uuid
                   ORDER BY created_at LIMIT %s""", (conversation_id, message_limit))
            cols = [d[0] for d in cur.description]
            msgs = []
            for row in cur.fetchall():
                m = dict(zip(cols, row))
                m["created_at"] = m["created_at"].isoformat() if m["created_at"] else None
                msgs.append(m)
        return {"ok": True, "conversation": header, "messages": msgs}
    finally:
        conn.close()


def history_for_party(party_type: str, party_id: str, limit: int = 20) -> Dict[str, Any]:
    """Every conversation for ONE person — the continuous-relationship view that
    makes cross-channel history answerable ('what happened with Acme?')."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT conversation_id::text, channel, status, intent,
                          message_count, last_message_at
                   FROM conversations
                   WHERE party_type=%s AND party_id=%s::uuid
                   ORDER BY last_message_at DESC LIMIT %s""",
                (party_type, party_id, limit))
            cols = [d[0] for d in cur.description]
            out = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                d["last_message_at"] = (d["last_message_at"].isoformat()
                                        if d["last_message_at"] else None)
                out.append(d)
        return {"ok": True, "party_type": party_type, "party_id": party_id,
                "conversations": out}
    finally:
        conn.close()


def _transcript(conversation_id: str) -> str:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT author, body FROM conversation_messages
                   WHERE conversation_id=%s::uuid ORDER BY created_at""",
                (conversation_id,))
            return "\n".join(f"{(a or 'customer').capitalize()}: {b or ''}"
                             for a, b in cur.fetchall())
    finally:
        conn.close()


def close(conversation_id: str) -> Dict[str, Any]:
    """Close a conversation. For an EXTERNAL conversation with a resolved contact,
    distill it into One Customer Memory (best-effort) so it's readable next time
    the same person contacts us on any channel."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE conversations SET status='closed', updated_at=now()
                   WHERE conversation_id=%s::uuid AND status<>'closed'
                   RETURNING scope, party_type, party_id::text, channel""",
                (conversation_id,))
            r = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not r:
        return {"ok": True, "note": "already closed or not found"}
    scope, party_type, party_id, channel = r
    remembered = None
    if scope == "external" and party_type == "contact" and party_id:
        try:
            from app.core import customer_memory
            remembered = customer_memory.remember(
                "contact", party_id, channel or "conversation",
                f"conv:{conversation_id}", _transcript(conversation_id))
        except Exception as exc:
            logger.warning(f"[conversations] memory distill skipped: {exc}")
    return {"ok": True, "conversation_id": conversation_id, "closed": True,
            "memory": remembered}


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["conversations"])


@router.post("/conversations/ingest")
def conversations_ingest(body: Dict[str, Any]):
    b = body or {}
    return ingest(InboundMessage(
        channel=str(b.get("channel") or ""), handle=str(b.get("handle") or ""),
        body=str(b.get("body") or ""), direction=str(b.get("direction") or "inbound"),
        external_ref=b.get("external_ref"), metadata=b.get("metadata") or {}))


@router.get("/conversations/{conversation_id}")
def conversations_get(conversation_id: str):
    return get(conversation_id)


@router.get("/conversations/party/{party_type}/{party_id}")
def conversations_history(party_type: str, party_id: str):
    return history_for_party(party_type, party_id)


@router.post("/conversations/{conversation_id}/reply")
def conversations_reply(conversation_id: str, body: Dict[str, Any]):
    b = body or {}
    return append_outbound(conversation_id, str(b.get("channel") or "webchat"),
                           str(b.get("body") or ""), str(b.get("author") or "agent"),
                           b.get("intent"), b.get("metadata"))


@router.post("/conversations/{conversation_id}/close")
def conversations_close(conversation_id: str):
    return close(conversation_id)


@router.get("/conversations-status")
def conversations_status():
    has = False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.conversations') IS NOT NULL")
            has = bool(cur.fetchone()[0])
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return {"enabled": ENABLED, "window_hours": WINDOW_HOURS,
            "conversations_table": has}
