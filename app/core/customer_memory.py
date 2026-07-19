"""Unified customer memory — "One Customer Memory" across every channel.

Each channel used to keep its own session state (sdr_sessions, the voice
line's in-call store, SMS stickiness, email threads in activities), so a
customer who explained their problem on the phone started from zero in the
next email. This module is the connective tissue:

    remember()        a conversation ENDS on any channel → the LLM distills
                      it into one compact row (what they wanted, resolved?,
                      what we promised, sentiment) in interaction_memories.
                      Deterministic fallback when the LLM fails — a truncated
                      raw summary beats amnesia. Idempotent per session_ref.
    commitments       every promise ("a teammate will follow up") ALSO
                      becomes an owner TASK activity, so it is visible,
                      assigned and fulfillable — not buried in a transcript.
    recall()/render_recall()
                      any channel that has IDENTIFIED its customer loads the
                      same memory: last conversations across ALL channels +
                      what we still owe them.

WHO GETS RECALL (identity before memory — same posture as the data tiers):
    • the context pack (context.hydrate) — so every agent, the A2A layer,
      and the email auto-reply to the ADDRESS ON FILE inherit it
    • the voice line AFTER OTP verification
    • never unverified web chat / SMS senders (caller ID and typed emails
      are claimable by anyone; memory must not leak to a claimant)

WRITER CALL SITES: voice_support._close_call, sdr.converse (session end),
telephony inbound-SMS bridge, email auto_reply after a sent reply. Writers
use remember_later() (background thread) — memory writing must never add
latency to a live goodbye.

CONFIG (env)
  CUSTOMER_MEMORY_ENABLED  1   kill switch for BOTH write and recall sides
  CUSTOMER_MEMORY_KEEP     10  recalled rows are drawn from this many recent
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("customer_memory")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("CUSTOMER_MEMORY_ENABLED", "1")
KEEP = int(os.getenv("CUSTOMER_MEMORY_KEEP", "10"))

_MAX_CONVERSATION = 4000     # chars of transcript fed to the distiller
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# ============================================================================
# DISTILL — LLM with a deterministic fallback (never lose the conversation)
# ============================================================================

def _distill_fallback(conversation: str) -> Dict[str, Any]:
    text = " ".join((conversation or "").split())
    return {"summary": text[:300] or "(empty conversation)", "intent": None,
            "resolved": None, "commitments": [], "sentiment": None}


def _distill_llm(channel: str, conversation: str) -> Optional[Dict[str, Any]]:
    try:
        from app.core.graph_utils import _get_llm
        resp = _get_llm(tier="lite").invoke([
            {"role": "system", "content":
                "You distill one finished customer conversation into the "
                "CRM's memory of it. Be factual and specific — this memory "
                "is read back next time the SAME customer contacts us, on "
                "any channel, so keep the details that make continuity "
                "possible (what exactly they asked about, order/invoice "
                "numbers they mentioned). commitments = ONLY explicit "
                "promises WE made that are still owed (follow-ups, "
                "callbacks, pending approvals); [] if none."},
            {"role": "user", "content":
                f"Channel: {channel}\nConversation:\n"
                f"{conversation[:_MAX_CONVERSATION]}\n\n"
                'Return ONLY JSON: {"summary": "<1-3 sentences, what they '
                'wanted and what happened>", "intent": "<2-4 word label>", '
                '"resolved": true/false, "commitments": [{"what": "<the '
                'promise, one line>"}], "sentiment": '
                '"positive"|"neutral"|"negative"}'},
        ])
        text = resp.content if hasattr(resp, "content") else str(resp)
        m = _JSON_RE.search(text)
        d = json.loads(m.group(0)) if m else None
        if not d or not str(d.get("summary") or "").strip():
            return None
        return {"summary": str(d["summary"])[:600],
                "intent": (str(d["intent"])[:60] if d.get("intent") else None),
                "resolved": d.get("resolved") if isinstance(d.get("resolved"), bool) else None,
                "commitments": [{"what": str(c.get("what"))[:200]}
                                for c in (d.get("commitments") or [])
                                if isinstance(c, dict) and c.get("what")][:5],
                "sentiment": (str(d["sentiment"])[:12]
                              if d.get("sentiment") in ("positive", "neutral",
                                                        "negative") else None)}
    except Exception as exc:
        logger.warning(f"[memory] distill failed (fallback): {exc}")
        return None


# ============================================================================
# REMEMBER — one row per ended conversation (+ a task per commitment)
# ============================================================================

def _owner_for(entity_type: str, entity_id: str) -> Optional[str]:
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            if entity_type == "account":
                cur.execute("SELECT owner_id FROM accounts WHERE account_id=%s::uuid",
                            (entity_id,))
            else:
                cur.execute("SELECT owner_id FROM leads WHERE lead_id=%s::uuid",
                            (entity_id,))
            r = cur.fetchone()
        conn.close()
        return r[0] if r else None
    except Exception:
        return None


def _commitment_task(entity_type: str, entity_id: str, channel: str,
                     what: str, owner_id) -> None:
    """A promise becomes an OPEN owner task — visible and fulfillable, not
    buried in a transcript. Best-effort."""
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, direction, channel,
                      owner_id, related_type, related_id, account_id, lead_id,
                      due_at, created_at, updated_at)
                   VALUES ('task', 'open', %s, %s, 'outbound', %s, %s,
                           %s, %s::uuid, %s::uuid, %s::uuid,
                           now() + interval '1 day', now(), now())""",
                (f"Commitment: {what[:160]}",
                 f"Promised to the customer during a {channel} conversation "
                 f"(recorded by customer memory): {what}",
                 channel, owner_id, entity_type, entity_id,
                 entity_id if entity_type == "account" else None,
                 entity_id if entity_type == "lead" else None))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"[memory] commitment task skipped: {exc}")


def remember(entity_type: str, entity_id: str, channel: str,
             session_ref: str, conversation: str) -> Dict[str, Any]:
    """Distill + store one ended conversation. Idempotent per session_ref;
    silently degrades when the interaction_memories migration is missing."""
    if not ENABLED:
        return {"ok": False, "skipped": "disabled"}
    if not (entity_id and (conversation or "").strip()):
        return {"ok": False, "skipped": "no entity or empty conversation"}
    d = _distill_llm(channel, conversation) or _distill_fallback(conversation)
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO interaction_memories
                         (entity_type, entity_id, channel, session_ref,
                          summary, intent, resolved, commitments, sentiment)
                       VALUES (%s,%s::uuid,%s,%s,%s,%s,%s,%s::jsonb,%s)
                       ON CONFLICT (session_ref) DO NOTHING
                       RETURNING memory_id""",
                    (entity_type, entity_id, channel,
                     session_ref[:120] if session_ref else None,
                     d["summary"], d["intent"], d["resolved"],
                     json.dumps(d["commitments"]), d["sentiment"]))
                r = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[memory] remember skipped (table missing?): {exc}")
        return {"ok": False, "skipped": str(exc)}
    if not r:
        return {"ok": True, "duplicate": True, "session_ref": session_ref}
    if d["commitments"]:
        owner = _owner_for(entity_type, entity_id)
        for c in d["commitments"]:
            _commitment_task(entity_type, entity_id, channel, c["what"], owner)
    # A NEGATIVE conversation becomes a blackboard signal on the entity —
    # the AI 360 summary and churn context pick it up automatically, and the
    # supervisor's sentiment_drop detector watches the aggregate. Expires so
    # a recovered relationship isn't haunted by one bad call.
    if d.get("sentiment") == "negative":
        try:
            from app.core import blackboard
            blackboard.post(entity_type, entity_id, "customer_memory",
                            "negative_sentiment",
                            note=f"Customer sentiment was NEGATIVE in a "
                                 f"{channel} conversation: {d['summary'][:140]}",
                            severity="medium", ttl_hours=14 * 24)
        except Exception as exc:
            logger.debug(f"[memory] sentiment signal skipped: {exc}")
    logger.info(f"[memory] remembered {channel} conversation for "
                f"{entity_type} {entity_id[:8]} "
                f"({len(d['commitments'])} commitment(s))")
    return {"ok": True, "memory_id": r[0], **d}


def remember_later(entity_type: str, entity_id: str, channel: str,
                   session_ref: str, conversation: str) -> None:
    """Fire-and-forget remember() — a goodbye must never wait on an LLM."""
    if not ENABLED or not entity_id:
        return
    threading.Thread(
        target=lambda: remember(entity_type, entity_id, channel,
                                session_ref, conversation),
        daemon=True, name=f"memory-{channel}").start()


# ============================================================================
# RECALL — the same memory, whatever channel asks
# ============================================================================

def recall(entity_type: str, entity_id: str, limit: int = 3) -> Dict[str, Any]:
    """Recent cross-channel conversations + outstanding commitments for one
    customer. Direct connection (not execute_sp) so it also works inside a
    verified-customer scope, where it serves that customer's OWN memory."""
    if not ENABLED:
        return {"interactions": [], "open_commitments": []}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT channel, summary, intent, resolved, commitments,
                              sentiment, created_at::date::text AS on_date
                       FROM interaction_memories
                       WHERE entity_type=%s AND entity_id=%s::uuid
                       ORDER BY created_at DESC LIMIT %s""",
                    (entity_type, entity_id, max(int(limit), KEEP)))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[memory] recall skipped (table missing?): {exc}")
        return {"interactions": [], "open_commitments": []}
    # A commitment is OWED until its task is closed; the memory rows keep the
    # promise text, the open-task check keeps the status honest.
    owed: List[Dict[str, Any]] = []
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT subject, created_at::date::text FROM activities
                   WHERE related_type=%s AND related_id=%s::uuid
                     AND type='task' AND status='open'
                     AND subject LIKE 'Commitment: %%'
                   ORDER BY created_at DESC LIMIT 5""",
                (entity_type, entity_id))
            owed = [{"what": s[len("Commitment: "):], "since": d}
                    for s, d in cur.fetchall()]
        conn.close()
    except Exception as exc:
        logger.debug(f"[memory] commitment check skipped: {exc}")
    for r in rows:
        if isinstance(r.get("commitments"), str):
            try:
                r["commitments"] = json.loads(r["commitments"])
            except ValueError:
                r["commitments"] = []
    return {"interactions": rows[:int(limit)], "open_commitments": owed}


def render_recall(entity_type: str, entity_id: str, limit: int = 3) -> str:
    """≤6-line prompt block ('' when nothing to say) — the channel-agnostic
    'we know you' context an agent reads before speaking."""
    mem = recall(entity_type, entity_id, limit)
    if not (mem["interactions"] or mem["open_commitments"]):
        return ""
    lines = ["[CUSTOMER MEMORY — recent conversations on all channels]"]
    for r in mem["interactions"]:
        res = ("resolved" if r.get("resolved") else
               "unresolved" if r.get("resolved") is False else "")
        lines.append(f"{r['on_date']} {r['channel']}: {r['summary'][:180]}"
                     + (f" ({res})" if res else ""))
    if mem["open_commitments"]:
        lines.append("Still owed to this customer: " + " · ".join(
            f"{c['what'][:80]} (since {c['since']})"
            for c in mem["open_commitments"][:3]))
    return "\n".join(lines[:6])


# ============================================================================
# Admin inspection
# ============================================================================

router = APIRouter(tags=["customer-memory"])


@router.get("/customer-memory/{entity_type}/{entity_id}")
def customer_memory_get(entity_type: str, entity_id: str, limit: int = 5):
    return {"enabled": ENABLED,
            **recall(entity_type, entity_id, limit),
            "rendered": render_recall(entity_type, entity_id, limit)}
