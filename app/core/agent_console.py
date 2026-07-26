"""Live Human-Agent Takeover Console — the human SEAT on the conversation spine.

Blindspot #1 (Agentforce parity). Our human-in-the-loop was async APPROVAL
only: a proposal waits in a queue for an executive to ratify. That is the wrong
muscle for a live conversation. A real contact centre needs "the AI handles the
easy 80%, and warm-transfers the hard 20% to a human WITH context." This module
adds that seat on top of the existing Unified Conversation Object
([conversations]) — no new transport, no new memory.

  queue()              the work list: open conversations, whoever is waiting on a
                       human first, with a last-message preview + a no-LLM
                       sentiment read so the worst ones float up.
  transcript()         the full thread for one conversation (delegates to
                       conversations.get) + who is driving it.
  suggest_reply()      AGENT-ASSIST: an LLM drafts a reply grounded in the
                       transcript + approved KB (metered, read-only) that the rep
                       edits and sends — the co-pilot, never the autopilot.
  takeover()/release() flip `handling` between 'ai' and 'human'. While a human
                       holds it, the autonomous responders stand down
                       (is_human_handled(), checked by the email auto-reply).
  send_reply()         the human's message: screened by the SAME outbound guard
                       that gates the agents, delivered on the customer's channel
                       (email / SMS), threaded into the conversation, logged.

Everything is gated by require_admin at the router (like every other console).
Requires sql/agent_console.sql (adds handling/assigned_to/assigned_at/escalated
to conversations). Degrades to a clear error if the migration is absent.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("agent_console")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("AGENT_CONSOLE_ENABLED", "1")


def _sentiment(text: str):
    """No-LLM lexicon sentiment (score, label) — reuses the email scorer so the
    queue and the executive snapshot read a customer the same way."""
    try:
        from app.agents.email.auto_reply import score_sentiment
        return score_sentiment(text or "")
    except Exception:
        return 0.0, "neutral"


# ============================================================================
# Queue — the work list
# ============================================================================

def _live_escalations(conversation_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """conversation_id → its live escalation (U1), for the queue badges. Empty
    dict if the table is absent — the console predates it and must still run."""
    ids = [c for c in conversation_ids if c]
    if not ids:
        return {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT conversation_id::text, escalation_id::text, reason,
                          priority, contact_known, sla_due_at,
                          (sla_due_at < now()) AS breached
                   FROM escalations
                   WHERE status IN ('open','assigned')
                     AND conversation_id = ANY(%s::uuid[])""",
                (ids,))
            cols = [d[0] for d in cur.description]
            out: Dict[str, Dict[str, Any]] = {}
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                cid = d.pop("conversation_id")
                if d.get("sla_due_at"):
                    d["sla_due_at"] = d["sla_due_at"].isoformat()
                out[cid] = d
            return out
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[console] escalation badges skipped "
                     f"(apply sql/escalations.sql?): {exc}")
        return {}
    finally:
        conn.close()


def queue(limit: int = 50, needs_human_only: bool = False) -> Dict[str, Any]:
    """Open EXTERNAL conversations as a rep work list. Priority: a customer
    already handed to a human, then customers whose last message is inbound
    (waiting on us) — negative sentiment boosted — then recency."""
    if not ENABLED:
        return {"ok": False, "error": "agent console disabled"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.conversation_id::text, c.channel, c.handling,
                          c.assigned_to, c.party_type, c.party_id::text,
                          c.account_id::text, c.message_count, c.last_message_at,
                          c.escalated,
                          m.direction, m.author, m.handle, m.body
                   FROM conversations c
                   LEFT JOIN LATERAL (
                       SELECT direction, author, handle, body
                       FROM conversation_messages
                       WHERE conversation_id = c.conversation_id
                       ORDER BY created_at DESC LIMIT 1
                   ) m ON true
                   WHERE c.status = 'open' AND c.scope = 'external'
                   ORDER BY c.last_message_at DESC
                   LIMIT %s""",
                (max(1, min(limit, 200)),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[console] queue failed (migration applied?): {exc}")
        return {"ok": False, "error": f"{str(exc)[:180]} "
                "(apply sql/agent_console.sql?)"}
    finally:
        conn.close()

    # Live escalations (U1) — a promise someone made to these customers. Read
    # separately and merged in Python so a missing sql/escalations.sql degrades
    # to "no escalation badges" instead of breaking the whole queue.
    live_esc = _live_escalations([r["conversation_id"] for r in rows])

    out: List[Dict[str, Any]] = []
    for r in rows:
        body = (r.get("body") or "").strip()
        awaiting = (r.get("direction") == "inbound")   # customer spoke last
        score, label = _sentiment(body) if awaiting else (0.0, "neutral")
        esc = live_esc.get(r["conversation_id"])
        # Priority: an unmet PROMISE outranks everything — that is the whole
        # point of U1 — then human-held (mid-handoff), then awaiting reply;
        # a negative last message jumps the awaiting bucket.
        prio = 0
        if r.get("handling") == "human":
            prio = 30
        elif awaiting:
            prio = 20 + (5 if label == "negative" else 0)
        if esc:
            prio = max(prio, 40 if esc.get("breached") else 35)
            r["escalation"] = esc
        r["awaiting_reply"] = awaiting
        r["last_preview"] = body[:160]
        r["last_sentiment"] = label
        r["priority"] = prio
        r["last_message_at"] = (r["last_message_at"].isoformat()
                                if r.get("last_message_at") else None)
        if needs_human_only and prio < 20 and r.get("handling") != "human":
            continue
        out.append(r)

    # Highest priority first, then most recent first.
    out.sort(key=lambda x: (x["priority"], x["last_message_at"] or ""),
             reverse=True)
    return {"ok": True, "count": len(out), "conversations": out,
            "escalations_open": len(live_esc),
            "escalations_breached": sum(1 for e in live_esc.values()
                                        if e.get("breached"))}


# ============================================================================
# Transcript + takeover state
# ============================================================================

def transcript(conversation_id: str) -> Dict[str, Any]:
    from app.core import conversations
    base = conversations.get(conversation_id)
    if not base.get("ok"):
        return base
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT handling, assigned_to, assigned_at, escalated
                   FROM conversations WHERE conversation_id=%s::uuid""",
                (conversation_id,))
            r = cur.fetchone()
    except Exception:
        conn.rollback()
        r = None
    finally:
        conn.close()
    base["handling"] = {
        "handling": (r[0] if r else "ai"),
        "assigned_to": (r[1] if r else None),
        "assigned_at": (r[2].isoformat() if r and r[2] else None),
        "escalated": (bool(r[3]) if r else False),
    }
    # The live promise (U1), so the rep opening the thread sees WHAT was
    # committed to this customer and by when — not just that "it was escalated".
    base["escalation"] = _live_escalations([conversation_id]).get(conversation_id)
    return base


def _set_handling(conversation_id: str, handling: str,
                  agent: Optional[str]) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if handling == "human":
                cur.execute(
                    """UPDATE conversations
                       SET handling='human', assigned_to=%s, assigned_at=now(),
                           escalated=true, updated_at=now()
                       WHERE conversation_id=%s::uuid AND status='open'
                       RETURNING conversation_id""",
                    (agent or "agent", conversation_id))
            else:
                cur.execute(
                    """UPDATE conversations
                       SET handling='ai', assigned_to=NULL, assigned_at=NULL,
                           updated_at=now()
                       WHERE conversation_id=%s::uuid
                       RETURNING conversation_id""",
                    (conversation_id,))
            ok = cur.fetchone() is not None
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"{str(exc)[:180]} "
                "(apply sql/agent_console.sql?)"}
    finally:
        conn.close()
    if not ok:
        return {"ok": False, "error": "conversation not found or already closed"}
    return {"ok": True, "conversation_id": conversation_id, "handling": handling}


def takeover(conversation_id: str, agent: str) -> Dict[str, Any]:
    """A rep takes the wheel: AI stands down, a system line records the join."""
    res = _set_handling(conversation_id, "human", agent)
    if res.get("ok"):
        _system_note(conversation_id,
                     f"— {agent} (human agent) joined the conversation —")
        # Picking up the work discharges the promise (U1) — no second click.
        try:
            from app.core import escalation
            done = escalation.resolve_for_conversation(
                conversation_id, agent, "picked up in the live console")
            if done.get("resolved"):
                res["escalations_resolved"] = done["resolved"]
        except Exception as exc:
            logger.debug(f"[console] escalation discharge skipped: {exc}")
    return res


def release(conversation_id: str, agent: str = "agent") -> Dict[str, Any]:
    """Hand the conversation back to the AI."""
    res = _set_handling(conversation_id, "ai", None)
    if res.get("ok"):
        _system_note(conversation_id,
                     f"— {agent} handed the conversation back to the AI —")
    return res


def _system_note(conversation_id: str, body: str) -> None:
    """Append a 'system' marker line (agent joined/left) to the transcript WITHOUT
    rolling the conversation's live `channel` forward — append_outbound would move
    `channel` to 'system', which corrupts the queue display, send routing, and the
    anon-key match in is_human_handled(). Bumps the counters only."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO conversation_messages
                     (conversation_id, channel, direction, author, body)
                   VALUES (%s::uuid, 'system', 'outbound', 'system', %s)""",
                (conversation_id, body))
            cur.execute(
                """UPDATE conversations
                   SET message_count = message_count + 1, updated_at = now()
                   WHERE conversation_id = %s::uuid""", (conversation_id,))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[console] system note skipped: {exc}")
    finally:
        conn.close()


def is_human_handled(channel: str, handle: str) -> bool:
    """True if THIS sender currently has an OPEN, human-handled conversation —
    so the autonomous responders (email auto-reply, …) stand down and don't
    talk over the rep. Resolves the sender's party the same way threading does;
    falls back to the anonymous handle key. Fail-open (returns False) on any
    error so a migration gap never blocks the auto-reply path."""
    try:
        from app.core import identity
        ident = identity.resolve(channel, handle)
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if ident.resolved and ident.party_id:
                    cur.execute(
                        """SELECT 1 FROM conversations
                           WHERE status='open' AND handling='human'
                             AND party_id=%s::uuid LIMIT 1""",
                        (ident.party_id,))
                else:
                    key = f"{(channel or '').lower()}:{ident.handle}"
                    cur.execute(
                        """SELECT 1 FROM conversations
                           WHERE status='open' AND handling='human'
                             AND anon_key=%s LIMIT 1""", (key,))
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[console] is_human_handled fail-open: {exc}")
        return False


# ============================================================================
# Agent-assist — the LLM co-pilot draft
# ============================================================================

def suggest_reply(conversation_id: str) -> Dict[str, Any]:
    """Draft a reply the rep can edit and send — grounded in the transcript and
    approved KB. READ-ONLY: it proposes text, sends nothing. Metered like every
    other model call; degrades to a safe template on any failure."""
    base = transcript(conversation_id)
    if not base.get("ok"):
        return base
    msgs = base.get("messages") or []
    convo = base.get("conversation") or {}
    # Last customer message drives KB grounding.
    last_customer = ""
    for m in reversed(msgs):
        if m.get("direction") == "inbound":
            last_customer = (m.get("body") or "").strip()
            break

    kb_block = ""
    try:
        from app.core import knowledge
        # Rep-facing draft: retrieve but do NOT log a customer-visible gap.
        kb_block = knowledge.rag_block("", last_customer, gap_channel=None) \
            if hasattr(knowledge, "rag_block") else ""
    except Exception as exc:
        logger.debug(f"[console] KB retrieval skipped: {exc}")

    transcript_txt = "\n".join(
        f"{(m.get('author') or 'customer').capitalize()}: {(m.get('body') or '')[:400]}"
        for m in msgs[-12:])
    try:
        from app.core import privacy
        transcript_txt = privacy.mask(transcript_txt)
    except Exception:
        pass

    system = (
        "You are a support co-pilot for Conscestra CRM / Agentorc.ca. A human "
        "agent has taken over this conversation and wants a suggested reply to "
        "send to the customer. Write ONE concise, warm, professional reply "
        "(under 120 words) that moves the issue forward. Use only facts present "
        "in the transcript or the approved knowledge below — never invent "
        "prices, dates, account details, or promises. If a human decision is "
        "needed, say you'll follow up. Sign as: The Conscestra CRM Team.")
    # Multilingual (blindspot #2): draft in the customer's language so the rep
    # can send it as-is — detected from their last message.
    try:
        from app.core import language
        system += language.respond_in(last_customer)
    except Exception as exc:
        logger.debug(f"[console] language directive skipped: {exc}")
    user = (
        f"Conversation channel: {convo.get('channel')}\n\n"
        f"Recent transcript:\n{transcript_txt}\n\n"
        + (f"Approved knowledge that matches the customer's last message "
           f"(base the substance on it):\n{kb_block}\n\n" if kb_block else "")
        + "Return ONLY the reply text to send — no preamble.")

    draft = ""
    try:
        from app.core.graph_utils import _get_llm
        llm = _get_llm(tier="lite")
        resp = llm.invoke([{"role": "system", "content": system},
                           {"role": "user", "content": user}])
        draft = (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:
        logger.warning(f"[console] suggest_reply LLM failed: {exc}")
        draft = ("Thanks for reaching out — I'm looking into this now and "
                 "will follow up shortly with an update.\n\nThe Conscestra CRM Team")
    return {"ok": True, "conversation_id": conversation_id, "draft": draft,
            "grounded_in_kb": bool(kb_block)}


# ============================================================================
# The human's outbound message
# ============================================================================

def _last_inbound_route(conversation_id: str):
    """(channel, handle) of the most recent inbound message — where to reply."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT channel, handle FROM conversation_messages
                   WHERE conversation_id=%s::uuid AND direction='inbound'
                     AND handle IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""", (conversation_id,))
            r = cur.fetchone()
            return (r[0], r[1]) if r else (None, None)
    finally:
        conn.close()


def send_reply(conversation_id: str, agent: str, body: str,
               channel: Optional[str] = None) -> Dict[str, Any]:
    """Send the rep's message to the customer. Screened by the outbound guard
    (humans are gated exactly like agents), delivered on the customer's channel
    when we have a push adapter (email / SMS), always threaded into the
    conversation and logged. Taking over implicitly if not already."""
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "empty message"}

    # Guardrail 3 — the same deterministic wall the agents pass.
    try:
        from app.core.outbound_guard import screen
        g = screen(body, channel or "console")
        if not g["ok"]:
            return {"ok": False, "blocked": True,
                    "error": "blocked by outbound guard: "
                             + "; ".join(g["violations"])}
    except Exception:
        pass

    route_channel, handle = _last_inbound_route(conversation_id)
    channel = (channel or route_channel or "webchat").lower()

    # Ensure the conversation reads as human-driven while a rep is replying.
    _set_handling(conversation_id, "human", agent)

    delivered, delivery = False, "recorded only (no push adapter for this channel)"
    try:
        if channel == "email" and handle:
            from app.agents.email.smtp_imap import send_email
            html = "".join(f'<p style="margin:0 0 .75em">{ln}</p>'
                           for ln in body.split("\n") if ln.strip())
            res = send_email(to=handle, subject="Re: your message",
                             body_html=f"<html><body>{html}</body></html>",
                             body_text=body)
            delivered = bool(res.get("success"))
            delivery = "email sent" if delivered else \
                f"email failed: {res.get('message') or res.get('error')}"
        elif channel in ("sms", "text") and handle:
            from app.core import telephony
            res = telephony.send_sms(handle, body, sent_by=f"human:{agent}",
                                     transactional=True)
            delivered = bool(res.get("ok"))
            delivery = "sms sent" if delivered else \
                f"sms failed: {res.get('error')}"
    except Exception as exc:
        delivery = f"delivery error: {str(exc)[:160]}"

    # Thread it regardless (the transcript is the record of truth).
    from app.core import conversations
    rec = conversations.append_outbound(
        conversation_id, channel, body, author="employee",
        metadata={"sent_by": agent, "via": "agent_console",
                  "delivered": delivered})
    return {"ok": rec.get("ok", False), "delivered": delivered,
            "delivery": delivery, "channel": channel,
            "message_id": rec.get("message_id"),
            "conversation_id": conversation_id}


# ============================================================================
# Router (admin-gated at include time)
# ============================================================================

router = APIRouter(tags=["agent-console"])


@router.get("/console/queue")
def console_queue(limit: int = 50, needs_human: bool = False):
    return queue(limit, needs_human)


@router.get("/console/conversation/{conversation_id}")
def console_conversation(conversation_id: str):
    return transcript(conversation_id)


@router.post("/console/conversation/{conversation_id}/takeover")
def console_takeover(conversation_id: str, body: Dict[str, Any]):
    return takeover(conversation_id, str((body or {}).get("agent") or "agent"))


@router.post("/console/conversation/{conversation_id}/release")
def console_release(conversation_id: str, body: Dict[str, Any]):
    return release(conversation_id, str((body or {}).get("agent") or "agent"))


@router.get("/console/conversation/{conversation_id}/suggest")
def console_suggest(conversation_id: str):
    return suggest_reply(conversation_id)


@router.post("/console/conversation/{conversation_id}/reply")
def console_reply(conversation_id: str, body: Dict[str, Any]):
    b = body or {}
    return send_reply(conversation_id, str(b.get("agent") or "agent"),
                      str(b.get("body") or ""), b.get("channel"))


@router.post("/console/conversation/{conversation_id}/close")
def console_close(conversation_id: str):
    from app.core import conversations
    return conversations.close(conversation_id)


@router.get("/console-status")
def console_status():
    has = False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='conversations' AND column_name='handling'")
            has = cur.fetchone() is not None
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return {"enabled": ENABLED, "migration_applied": has}
