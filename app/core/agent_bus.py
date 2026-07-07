"""Agent Bus — Phase 1 consumer daemon (event-driven agent cooperation).

WHAT THIS IS
------------
Your DB already has the full latent event bus:

    emit_event() ─▶ events ──(AFTER INSERT trigger)──▶ event_queue (pending)
                                       │
                                       └─▶ notifications (channel='agent_inbox')
                                           fanned out per event_subscriptions

…but nothing ever *consumed* event_queue (19k+ rows sat 'pending'). This module
is the missing consumer: a single background loop that claims pending queue rows,
routes each event to a registered Python handler (one per event_type), and marks
the work done / failed-with-retry. A handler embodies an agent ACTING on an event
and may delegate to peer agents in-process (the Accounting→Email pilot below).

SAFETY / GOVERNANCE
-------------------
  • Opt-in: does nothing unless AGENT_BUS_ENABLED=1.
  • Only event_types with a registered handler are ever touched — UNLESS
    AGENT_BUS_CATCHALL=1, in which case the Orchestrator's handle_default
    settles every other type too (react → blackboard signal / observe →
    last-touch note / ack — see the DEFAULT HANDLER section).
  • Boot cutoff: by default only events created at/after daemon start are
    processed (set AGENT_BUS_BACKFILL_MINUTES>0 to reach back). No mass replay.
  • Batch-capped, locked (locked_by/locked_at, 5-min stale-lock reclaim), and
    retried with exponential backoff up to AGENT_BUS_MAX_ATTEMPTS.
  • No outbound side effects by default: the pilot DRAFTS + logs + hands off to
    the Email agent via the bus. Real SMTP only when AGENT_BUS_AUTOSEND=1.

CONFIG (env)
------------
  AGENT_BUS_ENABLED            0     master on/off
  AGENT_BUS_POLL_SECS          30    seconds between ticks
  AGENT_BUS_BATCH              10    max events claimed per tick
  AGENT_BUS_MAX_ATTEMPTS       5     retries before status='failed'
  AGENT_BUS_AUTOSEND           0     1 = actually send via Email agent
  AGENT_BUS_BACKFILL_MINUTES   0     >0 = also process recent pre-boot events
  AGENT_BUS_CATCHALL           0     1 = Orchestrator settles unhandled types
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("agent_bus")

# ── Config ────────────────────────────────────────────────────────────────────
def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")

ENABLED      = _flag("AGENT_BUS_ENABLED")
POLL_SECS    = int(os.getenv("AGENT_BUS_POLL_SECS", "30"))
BATCH        = int(os.getenv("AGENT_BUS_BATCH", "10"))
MAX_ATTEMPTS = int(os.getenv("AGENT_BUS_MAX_ATTEMPTS", "5"))
AUTOSEND     = _flag("AGENT_BUS_AUTOSEND")
BACKFILL_MIN = int(os.getenv("AGENT_BUS_BACKFILL_MINUTES", "0"))

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# Materiality floor — matches the AR settlement tolerance used elsewhere.
MATERIAL_BALANCE = 50.0

# Set at start(); only events at/after this instant are eligible (minus backfill).
_CUTOFF: Optional[datetime] = None
_task: Optional[asyncio.Task] = None
_stop = asyncio.Event()

# event_type -> async handler(event_dict) -> result_dict
HANDLERS: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}


# ============================================================================
# QUEUE PLUMBING  (synchronous psycopg2 — run via asyncio.to_thread)
# ============================================================================

def _claim_batch_sync(cutoff: datetime) -> List[Dict[str, Any]]:
    """Atomically claim up to BATCH pending events whose type has a handler.
    With AGENT_BUS_CATCHALL=1, ANY pending type is claimed — unhandled types
    are settled by the Orchestrator's handle_default."""
    types = list(HANDLERS.keys())
    if not types and not CATCHALL:
        return []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH c AS (
                    SELECT q.queue_uuid
                    FROM   event_queue q
                    JOIN   events e ON e.event_uuid = q.event_uuid
                    WHERE  q.status = 'pending'
                      AND  (%(catchall)s OR e.event_type = ANY(%(types)s))
                      AND  e.created_at >= %(cutoff)s
                      AND  (q.next_attempt_at IS NULL OR q.next_attempt_at <= now())
                      AND  (q.locked_at IS NULL OR q.locked_at < now() - interval '5 minutes')
                    ORDER BY q.created_at
                    FOR UPDATE OF q SKIP LOCKED
                    LIMIT %(batch)s
                )
                UPDATE event_queue q
                SET    locked_by = %(worker)s,
                       locked_at = now(),
                       attempts  = COALESCE(q.attempts, 0) + 1
                FROM   c
                WHERE  q.queue_uuid = c.queue_uuid
                RETURNING q.event_uuid, q.attempts
                """,
                {"types": types, "cutoff": cutoff, "batch": BATCH,
                 "worker": WORKER_ID, "catchall": CATCHALL},
            )
            # Key claimed rows by event_uuid. There is now one queue row per event
            # (enforced by UNIQUE(event_uuid) + ON CONFLICT — see
            # sql/fix_event_queue_double_enqueue.sql), so this is defensive: it also
            # kept the consumer correct back when emit_event + the events trigger
            # double-enqueued. _complete/_fail act by event_uuid, settling every row.
            claimed = {str(r[0]): r[1] for r in cur.fetchall()}
            if not claimed:
                conn.commit()
                return []
            cur.execute(
                """
                SELECT event_uuid, event_type, entity_type, entity_uuid,
                       payload, correlation_id, created_at
                FROM   events
                WHERE  event_uuid = ANY(%(ids)s::uuid[])
                """,
                {"ids": list(claimed.keys())},
            )
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                ev = dict(zip(cols, r))
                ev["event_uuid"] = str(ev["event_uuid"])
                ev["attempts"] = claimed.get(ev["event_uuid"], 1)
                rows.append(ev)
        conn.commit()
        return rows
    finally:
        conn.close()


def _complete_sync(event_uuid: str, result: Dict[str, Any]) -> None:
    conn = get_connection()
    try:
        import json
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE event_queue
                   SET status='completed', last_attempt_at=now(), last_error=NULL,
                       error_context=%(ctx)s, locked_by=NULL
                   WHERE event_uuid=%(id)s::uuid""",
                {"id": event_uuid, "ctx": json.dumps(result)[:4000]},
            )
            # Settle the agent_inbox items this event fanned out to.
            cur.execute(
                """UPDATE notifications
                   SET status='sent', sent_at=now()
                   WHERE event_uuid=%(id)s::uuid AND channel='agent_inbox' AND status='pending'""",
                {"id": event_uuid},
            )
        conn.commit()
    finally:
        conn.close()


def _fail_sync(event_uuid: str, attempts: int, err: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE event_queue
                   SET status = CASE WHEN %(att)s >= %(max)s THEN 'failed' ELSE 'pending' END,
                       next_attempt_at = now() + (interval '30 seconds'
                                                  * power(2, LEAST(%(att)s, 6))),
                       last_attempt_at = now(), last_error = %(err)s, locked_by = NULL
                   WHERE event_uuid = %(id)s::uuid""",
                {"id": event_uuid, "att": attempts, "max": MAX_ATTEMPTS, "err": err[:2000]},
            )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# PILOT HANDLER  —  invoice.overdue  →  Accounting acts  →  Email handoff
# ============================================================================

def _load_invoice_ctx_sync(invoice_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.invoice_id, v.invoice_number, v.payment_status,
                       ROUND(v.computed_balance_due::numeric, 2)      AS balance,
                       (CURRENT_DATE - v.due_date::date)              AS days_overdue,
                       i.owner_id,
                       a.account_id, a.account_name,
                       ct.contact_id, ct.first_name AS contact_first, ct.email AS contact_email,
                       -- Only verified, opted-in recipients are eligible for real
                       -- outbound dunning. "Verified" = this contact is verified,
                       -- OR the same email is verified on any other contact row.
                       (COALESCE(ct.is_email_verified, false)
                        OR EXISTS (SELECT 1 FROM contacts c2
                                    WHERE lower(c2.email) = lower(ct.email)
                                      AND c2.is_email_verified)) AS is_email_verified,
                       ow.first_name AS owner_first, ow.email AS owner_email
                FROM   accounting_invoice_pipeline v
                JOIN   invoices  i  ON i.invoice_id = v.invoice_id
                LEFT   JOIN accounts a  ON a.account_id  = v.account_id
                LEFT   JOIN contacts ct ON ct.contact_id = v.contact_id
                LEFT   JOIN owners   ow ON ow.owner_id   = i.owner_id
                WHERE  v.invoice_id = %s
                """,
                (invoice_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return dict(zip([d[0] for d in cur.description], row))
    finally:
        conn.close()


def _already_dunned_sync(invoice_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM activities
                   WHERE related_type='invoice' AND related_id=%s
                     AND subject ILIKE 'Payment reminder%%'
                     AND created_at > now() - interval '20 hours'
                   LIMIT 1""",
                (invoice_id,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def _severity(days: int) -> str:
    if days > 45:
        return "urgent"
    if days > 14:
        return "firm"
    return "gentle"


def _compose_reminder(ctx: Dict[str, Any], tier: str) -> str:
    name = ctx.get("contact_first") or ctx.get("account_name") or "there"
    tone = {
        "gentle": "This is a friendly reminder that the following invoice is now past due.",
        "firm":   "Our records show the following invoice remains unpaid and is now significantly overdue.",
        "urgent": "URGENT: the following invoice is seriously overdue and requires immediate attention.",
    }[tier]
    return (
        f"Subject: Payment reminder — {ctx['invoice_number']}\n\n"
        f"Hi {name},\n\n{tone}\n\n"
        f"  Invoice:        {ctx['invoice_number']}\n"
        f"  Account:        {ctx.get('account_name') or '—'}\n"
        f"  Balance due:    ${ctx['balance']:,.2f}\n"
        f"  Days past due:  {ctx['days_overdue']}\n\n"
        f"Please arrange payment at your earliest convenience, or reply to discuss options.\n\n"
        f"— Accounts Receivable, Conscestra CRM"
    )


def _record_action_sync(ctx: Dict[str, Any], draft: str, tier: str,
                        correlation_id, sent: bool) -> None:
    """Log the Accounting agent's action and hand the draft to the Email agent
    over the bus (emit invoice.dunning_drafted → fans out to EmailAgent inbox)."""
    import json
    verb = "sent" if sent else "drafted"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, due_at, owner_id,
                      related_type, related_id, account_id, contact_id, channel,
                      created_at, updated_at)
                   VALUES ('task','open', %(subj)s, %(desc)s, now() + interval '1 day',
                           %(owner)s, 'invoice', %(inv)s, %(acct)s, %(ct)s, 'email',
                           now(), now())""",
                {
                    "subj": f"Payment reminder ({tier}) {verb} – {ctx['invoice_number']}",
                    "desc": draft,
                    "owner": ctx.get("owner_id"),
                    "inv": ctx["invoice_id"],
                    "acct": ctx.get("account_id"),
                    "ct": ctx.get("contact_id"),
                },
            )
            # Hand off to the Email agent via the bus (lineage-chained event).
            cur.execute(
                "SELECT emit_event(%s,%s,%s,%s,%s,%s,%s)",
                (
                    "invoice.dunning_drafted", "invoice", ctx["invoice_id"],
                    json.dumps({
                        "invoice_number": ctx["invoice_number"],
                        "balance": float(ctx["balance"]),
                        "days_overdue": ctx["days_overdue"],
                        "tier": tier,
                        "contact_email": ctx.get("contact_email"),
                        "draft": draft,
                        "delivered": sent,
                    }),
                    None, "agent_bus", correlation_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


async def handle_invoice_overdue(event: Dict[str, Any]) -> Dict[str, Any]:
    """AccountingAgent's reaction to an overdue invoice."""
    invoice_id = str(event["entity_uuid"])
    ctx = await asyncio.to_thread(_load_invoice_ctx_sync, invoice_id)

    if not ctx:
        return {"status": "skipped", "reason": "invoice not found"}
    # Re-check materiality at action time — it may have been paid since emit.
    if ctx["payment_status"] not in ("unpaid", "partial") or \
       (ctx["days_overdue"] or 0) <= 0 or float(ctx["balance"] or 0) <= MATERIAL_BALANCE:
        return {"status": "skipped", "reason": "no longer materially overdue"}
    if await asyncio.to_thread(_already_dunned_sync, invoice_id):
        return {"status": "skipped", "reason": "already actioned within 20h"}

    # Phase 4: respect shared context — another agent (e.g. Sales mid-renewal)
    # may have posted a 'dunning_hold' on this account. Read the blackboard
    # before acting, so agents coordinate through situational context, not calls.
    if ctx.get("account_id"):
        from app.core import blackboard
        holds = await asyncio.to_thread(
            blackboard.read, "account", str(ctx["account_id"]), "dunning_hold")
        if holds:
            return {"status": "skipped",
                    "reason": f"dunning held by {holds[0]['author_agent']}: "
                              f"{holds[0].get('note') or 'hold active'}"}

    tier = _severity(int(ctx["days_overdue"]))
    draft = _compose_reminder(ctx, tier)

    # Real outbound ONLY to verified, deliverable recipients — synthetic seed
    # contacts (is_email_verified=false) are drafted+logged but never emailed,
    # mirroring the order-email gate. Prevents dunning blasts to fake addresses.
    real = _is_real_email(ctx.get("contact_email"), ctx.get("is_email_verified"))
    sent = False
    if AUTOSEND and real:
        try:
            # Phase 2: typed, capability-routed A2A handoff. The Accounting
            # reaction delegates delivery to whichever agent owns the
            # 'email.send_payment_reminder' capability (the Email agent) — no
            # hardcoded endpoint, with correlation lineage carried through.
            from app.core.a2a import A2ARequest, EntityRef, dispatch
            res = await dispatch(A2ARequest(
                from_agent="accounting",
                intent="email.send_payment_reminder",
                entity=EntityRef("invoice", invoice_id),
                params={
                    "to": ctx["contact_email"],
                    "invoice_number": ctx["invoice_number"],
                    "amount": f"${ctx['balance']:,.2f}",
                    "days_overdue": ctx["days_overdue"],
                },
                correlation_id=(str(event.get("correlation_id"))
                                if event.get("correlation_id") else None),
                confidence=0.9,
            ))
            sent = res.ok or "sent" in (res.output or "").lower()
        except Exception as exc:  # delivery is best-effort; never fail the event
            logger.warning(f"[agent_bus] email handoff send failed: {exc}")

    await asyncio.to_thread(
        _record_action_sync, ctx, draft, tier, event.get("correlation_id"), sent
    )

    # Phase 4: post AR risk to the shared blackboard so other agents (Sales,
    # the supervisor, account 360s) see it without asking Accounting.
    if ctx.get("account_id"):
        from app.core import blackboard
        await asyncio.to_thread(
            blackboard.post, "account", str(ctx["account_id"]), "accounting", "ar_risk",
            f"Overdue {ctx['invoice_number']} ({tier}) — ${ctx['balance']:,.2f}, "
            f"{ctx['days_overdue']}d past due",
            {"invoice": ctx["invoice_number"], "balance": float(ctx["balance"]),
             "days_overdue": ctx["days_overdue"], "tier": tier},
            0.9, "critical" if tier == "urgent" else "warning", 168)

    return {
        "status": "ok",
        "action": "sent" if sent else "drafted",
        "invoice": ctx["invoice_number"],
        "tier": tier,
        "recipient_real": real,                       # verified + deliverable?
        "verified": bool(ctx.get("is_email_verified")),
        "autosend": AUTOSEND,
        "handoff": "invoice.dunning_drafted → EmailAgent inbox",
    }


HANDLERS["invoice.overdue"] = handle_invoice_overdue


# ============================================================================
# PILOT HANDLER #2  —  lead.scored (>=70)  →  Activity outreach + Notifications
# ============================================================================
# Demonstrates the pattern generalizing: a different event, a different pair of
# cooperating agents, the SAME consumer/queue/governance. The Lead agent's score
# triggers the Activity agent to auto-schedule outreach and the Notifications
# agent to raise an alert — entirely internal CRM records, safe by default.

HOT_SCORE = 70


def _load_lead_ctx_sync(lead_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT l.lead_id, l.first_name, l.last_name, l.company, l.email,
                          l.score, l.status, l.owner_id,
                          COALESCE(l.converted, false)  AS converted,
                          COALESCE(l.is_deleted, false) AS is_deleted
                   FROM leads l WHERE l.lead_id = %s""",
                (lead_id,),
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None
    finally:
        conn.close()


def _already_outreached_sync(lead_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM activities
                   WHERE related_type='lead' AND related_id=%s
                     AND subject ILIKE 'Hot lead outreach%%'
                     AND created_at > now() - interval '3 days'
                   LIMIT 1""",
                (lead_id,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def _record_lead_outreach_sync(ctx: Dict[str, Any], correlation_id) -> None:
    """ActivityAgent: create the outreach task, then hand off to Notifications
    via a lineage-chained lead.outreach_scheduled event."""
    import json
    name = f"{ctx.get('first_name') or ''} {ctx.get('last_name') or ''}".strip() or "lead"
    company = ctx.get("company") or "—"
    score = ctx["score"]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, due_at, direction, channel,
                      owner_id, related_type, related_id, lead_id, created_at, updated_at)
                   VALUES ('call','open', %(subj)s, %(desc)s, now() + interval '4 hours',
                           'outbound','phone', %(owner)s, 'lead', %(lead)s, %(lead)s,
                           now(), now())""",
                {
                    "subj": f"Hot lead outreach – {name} (score {score})",
                    "desc": (f"{name} at {company} scored {score} (Hot, >= {HOT_SCORE}). "
                             f"Call within 4 hours while intent is high. "
                             f"Auto-scheduled from lead.scored."),
                    "owner": ctx.get("owner_id"),
                    "lead": ctx["lead_id"],
                },
            )
            cur.execute(
                "SELECT emit_event(%s,%s,%s,%s,%s,%s,%s)",
                (
                    "lead.outreach_scheduled", "lead", ctx["lead_id"],
                    json.dumps({
                        "name": name, "company": company, "score": score,
                        "owner_id": str(ctx["owner_id"]) if ctx.get("owner_id") else None,
                        "action": "call scheduled within 4h",
                    }),
                    None, "agent_bus", correlation_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


async def handle_lead_scored(event: Dict[str, Any]) -> Dict[str, Any]:
    """LeadAgent's reaction to a (re)scored lead: if Hot, delegate to Activity
    (auto-outreach) and Notifications (alert)."""
    lead_id = str(event["entity_uuid"])
    ctx = await asyncio.to_thread(_load_lead_ctx_sync, lead_id)

    if not ctx:
        return {"status": "skipped", "reason": "lead not found"}
    if int(ctx["score"] or 0) < HOT_SCORE or ctx["converted"] or ctx["is_deleted"] \
       or (ctx.get("status") or "") in ("disqualified", "converted"):
        return {"status": "skipped", "reason": "not an actionable hot lead"}
    if await asyncio.to_thread(_already_outreached_sync, lead_id):
        return {"status": "skipped", "reason": "already actioned within 3 days"}

    await asyncio.to_thread(_record_lead_outreach_sync, ctx, event.get("correlation_id"))
    name = f"{ctx.get('first_name') or ''} {ctx.get('last_name') or ''}".strip()

    # Phase 4: post to the shared blackboard so other agents see this hot lead.
    from app.core import blackboard
    await asyncio.to_thread(
        blackboard.post, "lead", lead_id, "leads", "hot_lead",
        f"Hot lead (score {ctx['score']}) — outreach scheduled",
        {"score": ctx["score"], "name": name, "company": ctx.get("company")},
        0.9, "info", 72)

    # Start the multi-step follow-up cadence (no-op unless SEQUENCES_ENABLED;
    # one active run per lead). Best-effort — the outreach above already stands.
    cadence = None
    try:
        from app.core import sequences
        cadence = await asyncio.to_thread(
            sequences.start, "lead_followup", "lead", lead_id,
            {"score": ctx["score"], "name": name},
            "leads", event.get("correlation_id"))
    except Exception as exc:
        logger.warning(f"[agent_bus] lead_followup cadence start failed: {exc}")

    return {
        "status": "ok",
        "action": "outreach scheduled",
        "lead": name,
        "score": ctx["score"],
        "cadence": (cadence or {}).get("status"),
        "handoff": "lead.outreach_scheduled → Notifications inbox",
    }


HANDLERS["lead.scored"] = handle_lead_scored


# ============================================================================
# HANDLER #3  —  activity.overdue_flagged  →  Activity agent surfaces material
#               overdue work to its owner (nudge) + posts to the blackboard
# ============================================================================
# Division of labour with the nightly `sp_activities_auto_sweep` (which SNOOZES
# low-value overdue tasks, score<=15): this handler acts on the COMPLEMENT — the
# *material* overdue items the sweep deliberately leaves alone (linked to an open
# opportunity, a call/meeting, or score>15). It SURFACES them to the owner rather
# than auto-rescheduling, so important slipped work is never silently hidden.

ACTIVITY_AGENT_UUID = "00000000-0000-0000-0000-000000000005"


def _load_activity_ctx_sync(activity_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.activity_id, a.status, a.due_at, a.type, a.subject,
                          a.owner_id, a.opportunity_id, a.account_id, a.contact_id,
                          a.lead_id, a.activity_score,
                          (now()::date - a.due_at::date) AS days_overdue,
                          ow.first_name AS owner_first
                   FROM   activities a
                   LEFT   JOIN owners ow ON ow.owner_id = a.owner_id
                   WHERE  a.activity_id = %s""",
                (activity_id,),
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None
    finally:
        conn.close()


def _already_nudged_sync(activity_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM notifications
                   WHERE channel='in_app' AND status <> 'read'
                     AND metadata->>'kind'='activity_nudge'
                     AND metadata->>'activity_id' = %s
                     AND created_at > now() - interval '48 hours'
                   LIMIT 1""",
                (activity_id,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def _record_activity_nudge_sync(ctx: Dict[str, Any], event_uuid: str) -> None:
    """Notify the activity's owner that material overdue work needs attention.
    Anchored to the triggering event (notifications.event_uuid is NOT NULL); the
    digest/triage classifies activity.overdue_flagged as ACTIONABLE, so this nudge
    is preserved (not auto-digested) and is auto-resolved by triage once the
    activity is completed or brought current."""
    import json
    days = ctx.get("days_overdue") or 0
    subject = ctx.get("subject") or "(untitled)"
    link = ("opportunity" if ctx.get("opportunity_id") else
            "account" if ctx.get("account_id") else
            "lead" if ctx.get("lead_id") else "record")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO notifications
                     (employee_uuid, event_uuid, channel, status, title, body,
                      metadata, created_at)
                   VALUES (%(owner)s, %(ev)s::uuid, 'in_app', 'pending',
                           %(title)s, %(body)s, %(meta)s, now())""",
                {
                    "owner": ctx["owner_id"], "ev": event_uuid,
                    "title": f"⏰ Overdue {ctx.get('type') or 'task'}: {subject}",
                    "body": (f"'{subject}' is {days} day(s) overdue and tied to an open "
                             f"{link}. Please action or reschedule it."),
                    "meta": json.dumps({
                        "kind": "activity_nudge", "source": "agent_bus",
                        "activity_id": str(ctx["activity_id"]),
                        "days_overdue": days,
                        "opportunity_id": str(ctx["opportunity_id"]) if ctx.get("opportunity_id") else None,
                    }),
                },
            )
        conn.commit()
    finally:
        conn.close()


def _is_material_overdue(ctx: Dict[str, Any]) -> bool:
    return bool(ctx.get("opportunity_id")) \
        or (ctx.get("type") in ("call", "meeting")) \
        or (int(ctx.get("activity_score") or 0) > 15)


async def handle_activity_overdue_flagged(event: Dict[str, Any]) -> Dict[str, Any]:
    """ActivityAgent's reaction to an overdue-flagged activity: if it's material
    (vs. the low-value tasks the nightly snooze handles), nudge the owner."""
    activity_id = str(event["entity_uuid"])
    ctx = await asyncio.to_thread(_load_activity_ctx_sync, activity_id)

    if not ctx:
        return {"status": "skipped", "reason": "activity not found"}
    # Re-check at action time — it may have been completed/rescheduled since emit.
    if ctx["status"] != "open" or (ctx.get("days_overdue") or 0) <= 0:
        return {"status": "skipped", "reason": "no longer open & overdue"}
    if not _is_material_overdue(ctx):
        return {"status": "skipped", "reason": "low-value — left for nightly snooze sweep"}
    if not ctx.get("owner_id"):
        return {"status": "skipped", "reason": "no owner to nudge"}
    if await asyncio.to_thread(_already_nudged_sync, activity_id):
        return {"status": "skipped", "reason": "owner already nudged within 48h"}

    await asyncio.to_thread(_record_activity_nudge_sync, ctx, event["event_uuid"])

    # Phase 4: post to the shared blackboard so the supervisor / account 360s see
    # the slipped commitment without asking the Activity agent.
    if ctx.get("account_id"):
        from app.core import blackboard
        await asyncio.to_thread(
            blackboard.post, "account", str(ctx["account_id"]), "activities",
            "overdue_activity",
            f"Overdue {ctx.get('type') or 'task'} '{ctx.get('subject')}' "
            f"({ctx.get('days_overdue')}d) — owner nudged",
            {"activity_id": str(ctx["activity_id"]),
             "days_overdue": ctx.get("days_overdue"),
             "opportunity_id": str(ctx["opportunity_id"]) if ctx.get("opportunity_id") else None},
            0.85, "warning", 72)

    return {
        "status": "ok",
        "action": "owner nudged",
        "activity": ctx.get("subject"),
        "days_overdue": ctx.get("days_overdue"),
        "owner": ctx.get("owner_first"),
    }


HANDLERS["activity.overdue_flagged"] = handle_activity_overdue_flagged


# ============================================================================
# HANDLER #4  —  lead.created  →  Leads agent enriches via an EXTERNAL source
# ============================================================================
# The project's first OUTWARD function call (IBM: "agents use external tools —
# APIs, data sources, web"). A new lead is auto-enriched with firmographics from
# an external data source (stub by default — app/core/enrichment.py) and the
# result is posted to the shared blackboard so other agents / account 360s can use
# it. Non-disruptive: writes a blackboard note, never the lead row. Idempotent.

def _load_lead_basic_sync(lead_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT first_name, last_name, company, email FROM leads WHERE lead_id = %s",
                (lead_id,))
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None
    finally:
        conn.close()


async def handle_lead_created(event: Dict[str, Any]) -> Dict[str, Any]:
    """LeadAgent's reaction to a new lead: fetch external firmographics and post
    them to the shared blackboard."""
    lead_id = str(event["entity_uuid"])
    ctx = await asyncio.to_thread(_load_lead_basic_sync, lead_id)
    if not ctx:
        return {"status": "skipped", "reason": "lead not found"}
    if not (ctx.get("company") or ctx.get("email")):
        return {"status": "skipped", "reason": "no company/email to enrich"}

    from app.core import blackboard
    if await asyncio.to_thread(blackboard.read, "lead", lead_id, "enrichment"):
        return {"status": "skipped", "reason": "already enriched"}

    from app.core import enrichment
    data = await asyncio.to_thread(
        enrichment.enrich_company, ctx.get("company"), ctx.get("email"), None)
    if not data.get("matched"):
        return {"status": "skipped", "reason": "no enrichment match"}

    # Fill the lead's OWN fields (gap-fill — never overwrites). Best-effort: if the
    # enrichment columns aren't migrated yet, log and still record on the blackboard.
    filled = 0
    try:
        filled = await asyncio.to_thread(enrichment.apply_to_lead, lead_id, data)
    except Exception as exc:
        logger.warning(f"[agent_bus] lead field-fill skipped ({exc}); blackboard only")

    note = (f"{data.get('industry')} · {data.get('employee_band')} employees · "
            f"{data.get('revenue_band')} · {data.get('hq_location')}")
    await asyncio.to_thread(
        blackboard.post, "lead", lead_id, "leads", "enrichment", note, data,
        float(data.get("confidence") or 0.8), "info", 168)

    return {"status": "ok", "action": "enriched", "company": ctx.get("company"),
            "industry": data.get("industry"), "fields_filled": bool(filled),
            "source": data.get("source"), "handoff": "blackboard:lead/enrichment"}


HANDLERS["lead.created"] = handle_lead_created


# ── Activity ↔ Accounting/Order: real-time milestone-record completion ───────
def _complete_milestone_sync(rel_type: str, rel_id: str) -> Dict[str, Any]:
    """Close the auto-generated milestone-record task(s) for ONE just-settled
    entity, via the SHARED SP — so the rule is identical to the nightly sweep, no
    duplicated logic. The SP re-checks the entity is actually settled and is
    idempotent, so a redelivered event or a task already closed by the sweep is a
    safe no-op."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fn_complete_settled_milestone_activities(NULL, true, %s, %s::uuid)",
                (rel_type, rel_id),
            )
            res = (cur.fetchone() or [None])[0] or {}
        conn.commit()
        return res
    finally:
        conn.close()


async def handle_milestone_settled(event: Dict[str, Any]) -> Dict[str, Any]:
    """ActivityAgent's reaction when an invoice is fully paid (`invoice_paid`) or
    an order advances (`order.status_changed`): immediately close that entity's
    milestone-record task — sub-day latency instead of waiting for the 22:12
    nightly sweep. Scoped to the one event entity (cheap), shares the sweep's
    completion rule, idempotent + actionability re-checked inside the SP. The
    nightly sweep remains the backstop for anything missed while the bus is off."""
    et = event["event_type"]
    rel_type = "invoice" if et == "invoice_paid" else "order"
    rel_id = str(event["entity_uuid"])
    res = await asyncio.to_thread(_complete_milestone_sync, rel_type, rel_id)
    done = int((res or {}).get("completed") or 0)
    return {
        "status": "ok" if done else "skipped",
        "reason": None if done else "no open milestone record (already closed / not settled)",
        "entity": f"{rel_type}:{rel_id}",
        "completed": done,
    }


# invoice_paid → invoice milestone ("Payment complete … paid in full").
HANDLERS["invoice_paid"] = handle_milestone_settled
# order.status_changed is handled by handle_order_status_changed below (which also
# closes the order milestone via handle_milestone_settled).


# ── Order → Email: buyer order-lifecycle emails (confirmation + shipped) ──────
# Customers who SIGN UP with a real address (OTP-verified → contacts.is_email_verified)
# get genuine order emails; the synthetic seed contacts (example.com, demo domains,
# is_email_verified=false) never do — they stay draft+log. Real send only when
# AGENT_BUS_AUTOSEND=1 AND the recipient passes _is_real_email().

# Obvious placeholder / non-deliverable domains used by the seed data and RFC docs.
_PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com", "examples.com", "example.org", "example.net",
    "test.com", "test", "localhost", "invalid", "none.com",
}

def _is_real_email(addr: Optional[str], is_verified: bool) -> bool:
    """A deliverable, opted-in recipient: verified through the OTP flow AND not an
    obvious placeholder/seed domain. Seed contacts are is_email_verified=false, so
    only addresses a human actually confirmed (like a real home-store signup) pass."""
    if not addr or not is_verified:
        return False
    addr = addr.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr):
        return False
    domain = addr.rsplit("@", 1)[-1]
    if domain in _PLACEHOLDER_EMAIL_DOMAINS:
        return False
    if any(domain.endswith(sfx) for sfx in (".invalid", ".test", ".example", ".local")):
        return False
    return True


def _load_order_email_ctx_sync(order_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.order_number, o.status, o.total_amount,
                          o.account_id, o.contact_id, c.email,
                          NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' ||
                                      COALESCE(c.last_name,'')), '') AS contact_name,
                          -- "Verified" = the order's own contact is verified, OR the
                          -- buyer's email is verified on ANY contact row (fallback for
                          -- when checkout linked a duplicate/seed contact that shares
                          -- the real, signed-up email).
                          (COALESCE(c.is_email_verified, false)
                           OR EXISTS (
                                SELECT 1 FROM contacts c2
                                WHERE c.email IS NOT NULL
                                  AND lower(c2.email) = lower(c.email)
                                  AND c2.is_email_verified
                           )) AS is_email_verified
                   FROM orders o
                   LEFT JOIN contacts c ON c.contact_id = o.contact_id
                   WHERE o.order_id = %s""",
                (order_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "order_id": order_id, "order_number": row[0], "status": row[1],
                "total_amount": float(row[2] or 0),
                "account_id": row[3], "contact_id": row[4],
                "contact_email": row[5], "contact_name": row[6] or "there",
                "is_email_verified": bool(row[7]),
            }
    finally:
        conn.close()


def _order_email_already_sent_sync(order_id: str, kind: str) -> bool:
    """Idempotency: have we already logged this order-email kind for this order?"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM activities
                   WHERE related_type='order' AND related_id=%s
                     AND channel='email' AND subject ILIKE %s LIMIT 1""",
                (order_id, f"Order {kind} email%"),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def _compose_order_email(ctx: Dict[str, Any], kind: str):
    name = ctx["contact_name"]
    num = ctx["order_number"]
    total = f"${ctx['total_amount']:,.2f}"
    if kind == "confirmation":
        subject = f"Order confirmation — {num}"
        body_text = (f"Hi {name},\n\nThanks for your order! We've received {num} "
                     f"(total {total}) and it's now being processed. We'll email you "
                     f"again as soon as it ships.\n\n— Conscestra CRM")
        intro = "Thanks for your order! We've received it and it's now being processed."
        tail = "We'll email you again as soon as it ships."
    else:  # shipped
        subject = f"Your order has shipped — {num}"
        body_text = (f"Hi {name},\n\nGood news — your order {num} (total {total}) has "
                     f"shipped and is on its way.\n\n— Conscestra CRM")
        intro = "Good news — your order has shipped and is on its way."
        tail = "Thank you for shopping with us."
    body_html = (
        f'<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px">'
        f'<h2 style="color:#0d9488;margin-bottom:8px">Conscestra CRM</h2>'
        f'<p>Hi {name},</p><p>{intro}</p>'
        f'<div style="background:#f0fdfa;border-radius:8px;padding:16px;margin:16px 0">'
        f'<strong>Order:</strong> {num}<br><strong>Total:</strong> {total}</div>'
        f'<p style="color:#6b7280;font-size:0.875rem">{tail}</p></div>'
    )
    return subject, body_text, body_html


def _record_order_email_sync(ctx: Dict[str, Any], kind: str, body_text: str, sent: bool) -> None:
    """Audit the order-email action as a COMPLETED activity (never an open task —
    it's a record of a done send/draft, so it stays off the overdue worklist)."""
    verb = "sent" if sent else "drafted"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, due_at, completed_at,
                      related_type, related_id, account_id, contact_id, channel,
                      outcome, created_at, updated_at)
                   VALUES ('task','completed', %(subj)s, %(desc)s, now(), now(),
                           'order', %(oid)s, %(acct)s, %(ct)s, 'email',
                           %(out)s, now(), now())""",
                {"subj": f"Order {kind} email {verb} – {ctx['order_number']}",
                 "desc": body_text, "oid": ctx["order_id"],
                 "acct": ctx.get("account_id"), "ct": ctx.get("contact_id"),
                 "out": f"auto: order {kind} email {verb} to buyer"},
            )
        conn.commit()
    finally:
        conn.close()


def _send_order_email_sync(ctx: Dict[str, Any], subject: str, body_html: str, body_text: str) -> bool:
    from app.agents.email.smtp_imap import send_email
    res = send_email(to=ctx["contact_email"], subject=subject,
                     body_html=body_html, body_text=body_text)
    return bool(res.get("success"))


async def handle_order_status_changed(event: Dict[str, Any]) -> Dict[str, Any]:
    """Two reactions to an order status change:
      1. Close the order's milestone activity (existing behaviour).
      2. Email the BUYER — an order confirmation when the order is placed
         (pending/processing) or a shipped notice on dispatch. Real outbound only
         when AUTOSEND=1 AND the buyer's contact email is verified & deliverable
         (_is_real_email); otherwise draft + log. Idempotent per (order, kind)."""
    milestone = await handle_milestone_settled(event)

    order_id = str(event["entity_uuid"])
    payload = event.get("payload") or {}
    new_status = (((payload.get("diff") or {}).get("status") or {}).get("new")
                  or (payload.get("after") or {}).get("status"))
    kind = ("confirmation" if new_status in ("pending", "processing")
            else "shipped" if new_status == "shipped" else None)
    if not kind:
        return {"status": "ok", "milestone": milestone, "email": "skipped (status not emailable)"}

    ctx = await asyncio.to_thread(_load_order_email_ctx_sync, order_id)
    if not ctx:
        return {"status": "skipped", "reason": "order not found", "milestone": milestone}

    # Only real, opted-in customers get an order email (or a draft+log). The
    # synthetic seed contacts (is_email_verified=false) are skipped entirely — no
    # log row — so the high-volume generated orders don't flood the activity table.
    real = _is_real_email(ctx.get("contact_email"), ctx.get("is_email_verified"))
    if not real:
        return {"status": "skipped", "milestone": milestone,
                "reason": "recipient not verified/deliverable", "order": ctx["order_number"]}

    if await asyncio.to_thread(_order_email_already_sent_sync, order_id, kind):
        return {"status": "skipped", "reason": f"{kind} email already logged",
                "milestone": milestone}

    subject, body_text, body_html = _compose_order_email(ctx, kind)

    sent = False
    if AUTOSEND:
        try:
            sent = await asyncio.to_thread(
                _send_order_email_sync, ctx, subject, body_html, body_text)
        except Exception as exc:  # delivery is best-effort; never fail the event
            logger.warning(f"[agent_bus] order {kind} email send failed: {exc}")

    await asyncio.to_thread(_record_order_email_sync, ctx, kind, body_text, sent)

    return {
        "status": "ok",
        "milestone": milestone,
        "order": ctx["order_number"],
        "email_kind": kind,
        "action": "sent" if sent else "drafted",
        "recipient_real": real,           # verified + deliverable?
        "verified": ctx["is_email_verified"],
        "autosend": AUTOSEND,
    }


HANDLERS["order.status_changed"] = handle_order_status_changed


# ============================================================================
# HANDLER #7  —  email.received  →  Email agent escalates complaints
# ============================================================================
# The inbound bridge (app/agents/email/inbound_bridge.py) already recorded the
# inbound activity — that row IS the engagement signal for cadences/campaigns/
# intelligence, so most intents are simply acked here. COMPLAINTS escalate:
# a warning note on the shared blackboard (where churn_save's context check
# reads) + an urgent owner task. Idempotent per (entity, sender) per 24h.

async def handle_email_received(event: Dict[str, Any]) -> Dict[str, Any]:
    import json as _json
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = _json.loads(payload or "{}")
    ctx = payload.get("context") or {}
    intent = (ctx.get("intent") or "").lower()
    entity_type = (event.get("entity_type") or "").lower()
    entity_id = str(event["entity_uuid"]) if event.get("entity_uuid") else None

    if intent != "complaint" or entity_type not in ("account", "lead") or not entity_id:
        return {"status": "ok", "action": "acked",
                "reason": f"intent={intent or 'n/a'} — activity row is the signal"}

    sender = ctx.get("from") or "unknown sender"
    subject = ctx.get("subject") or "(no subject)"

    def _escalate_sync() -> str:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                id_col = "account_id" if entity_type == "account" else "lead_id"
                cur.execute(
                    f"""SELECT 1 FROM activities
                        WHERE {id_col} = %s::uuid AND type='task'
                          AND subject LIKE 'COMPLAINT –%%'
                          AND description LIKE %s
                          AND created_at > now() - interval '24 hours'
                        LIMIT 1""",
                    (entity_id, f"%{sender}%"))
                if cur.fetchone():
                    return "already escalated within 24h"
                cur.execute(
                    f"""SELECT owner_id FROM {'accounts' if entity_type == 'account' else 'leads'}
                        WHERE {'account_id' if entity_type == 'account' else 'lead_id'} = %s::uuid""",
                    (entity_id,))
                r = cur.fetchone()
                cur.execute(
                    """INSERT INTO activities
                         (type, status, subject, description, due_at, direction,
                          channel, owner_id, related_type, related_id,
                          account_id, lead_id, created_at, updated_at)
                       VALUES ('task', 'open', %s, %s, now() + interval '4 hours',
                               'outbound', 'email', %s, %s, %s::uuid,
                               %s::uuid, %s::uuid, now(), now())""",
                    (f"COMPLAINT – {sender}",
                     f"Inbound complaint ({subject!r}) from {sender}. "
                     f"Respond within 4 hours; a churn-save context check will "
                     f"pick this up from the blackboard.",
                     r[0] if r else None, entity_type, entity_id,
                     entity_id if entity_type == "account" else None,
                     entity_id if entity_type == "lead" else None))
            conn.commit()
            return "escalated"
        finally:
            conn.close()

    outcome = await asyncio.to_thread(_escalate_sync)
    if outcome == "escalated":
        from app.core import blackboard
        await asyncio.to_thread(
            blackboard.post, entity_type, entity_id, "support", "complaint",
            f"Complaint received from {sender}: {subject}",
            {"from": sender, "subject": subject}, 0.9, "warning", 24 * 14)
    return {"status": "ok", "action": outcome, "intent": "complaint",
            "handled_by": "email/support"}


HANDLERS["email.received"] = handle_email_received


# ============================================================================
# DEFAULT HANDLER — Orchestrator cooperation for every OTHER event type
# ============================================================================
# Before this, only event types with a bespoke handler were ever claimed —
# ~92% of the queue (CRUD echoes like activity.completed, product.updated)
# sat pending forever. With AGENT_BUS_CATCHALL=1 the Orchestrator handles the
# rest cooperatively and every row is settled with a proper mark:
#
#   REACT   — meaningful business moments post a typed signal to the shared
#             blackboard on the OWNING entity/account, where the other agents
#             already read context (ai_summary 360s, dunning holds, …).
#   OBSERVE — CRUD echoes upsert a 'recent_activity' last-touch note on the
#             entity (one row per entity — the blackboard upserts per
#             (entity, author, topic), so bulk noise coalesces).
#   ACK     — lineage/handler-emitted events (dunning_drafted, outreach_
#             scheduled, supervisor.alert) are acknowledged only — reacting to
#             them could feed back into the bus.
#
# All dispositions return ok → the consumer marks the row 'completed' and
# stores {"action": "reacted"|"observed"|"acked", ...} in error_context.
# Deterministic by design: no LLM per event (cost + loop safety).

CATCHALL = _flag("AGENT_BUS_CATCHALL", "0")   # v1.1 — orchestrator catch-all

# Lineage / self-emitted types — acknowledge only (loop safety).
_ACK_ONLY = {"invoice.dunning_drafted", "lead.outreach_scheduled",
             "supervisor.alert", "supervisor.briefing"}

# event_type → (blackboard topic, severity, human note template)
_REACTIONS = {
    "opportunity.closed_won":  ("deal_won",       "info",    "Deal WON — account is an advocate candidate"),
    "opportunity.closed_lost": ("deal_lost",      "warning", "Deal LOST — churn/competitor signal"),
    "opportunity.stage_changed": ("stage_moved",  "info",    "Deal moved stage"),
    "product.stock_changed":   ("stock_changed",  "info",    "Stock level changed"),
    "contact.email_verified":  ("email_verified", "info",    "Contact verified their email — engaged"),
    "lead.converted":          ("lead_converted", "info",    "Lead converted"),
    "account.created":         ("new_account",    "info",    "New account created"),
    "invoice_issued":          ("invoice_issued", "info",    "Invoice issued"),
}

# entity types whose activity is worth a last-touch note on the blackboard
_OBSERVABLE = {"account", "contact", "lead", "opportunity", "product",
               "order", "invoice", "payment", "activity"}


def _resolve_account_sync(entity_type: str, entity_id: str) -> Optional[str]:
    """Best-effort owning-account lookup so signals land where agents read."""
    col = {"opportunity": ("opportunities", "opportunity_id"),
           "contact": ("contacts", "contact_id"),
           "invoice": ("invoices", "invoice_id"),
           "order": ("orders", "order_id"),
           "activity": ("activities", "activity_id")}.get(entity_type)
    if not col:
        return None
    table, pk = col
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT account_id::text FROM {table} WHERE {pk}=%s::uuid",
                        (entity_id,))
            r = cur.fetchone()
            return r[0] if r and r[0] else None
    finally:
        conn.close()


async def handle_default(event: Dict[str, Any]) -> Dict[str, Any]:
    """Orchestrator catch-all — settle any event without a bespoke handler."""
    et = event["event_type"]
    entity_type = (event.get("entity_type") or "").lower()
    entity_id = str(event["entity_uuid"]) if event.get("entity_uuid") else None

    if et in _ACK_ONLY or not entity_id:
        return {"status": "ok", "action": "acked", "handled_by": "orchestrator",
                "reason": "lineage/no-entity event — acknowledged"}

    from app.core import blackboard

    reaction = _REACTIONS.get(et)
    if reaction:
        topic, severity, note = reaction
        # Post on the owning account when resolvable (where agents read
        # context), and always on the entity itself.
        target_acct = await asyncio.to_thread(_resolve_account_sync, entity_type, entity_id)
        posted_to = []
        try:
            await asyncio.to_thread(
                blackboard.post, entity_type, entity_id, "orchestrator", topic,
                f"{note} ({et})", {"event_type": et}, 0.7, severity, 168)
            posted_to.append(entity_type)
            if target_acct and entity_type != "account":
                await asyncio.to_thread(
                    blackboard.post, "account", target_acct, "orchestrator", topic,
                    f"{note} — via {entity_type} ({et})",
                    {"event_type": et, entity_type: entity_id}, 0.7, severity, 168)
                posted_to.append("account")
        except Exception as exc:
            logger.warning(f"[agent_bus] catchall blackboard post failed for {et}: {exc}")
        return {"status": "ok", "action": "reacted", "handled_by": "orchestrator",
                "topic": topic, "posted_to": posted_to}

    if entity_type in _OBSERVABLE:
        try:
            await asyncio.to_thread(
                blackboard.post, entity_type, entity_id, "orchestrator",
                "recent_activity", f"Last touch: {et}",
                {"event_type": et}, 0.5, "info", 72)
        except Exception as exc:
            logger.warning(f"[agent_bus] catchall observe failed for {et}: {exc}")
        return {"status": "ok", "action": "observed", "handled_by": "orchestrator",
                "topic": "recent_activity"}

    return {"status": "ok", "action": "acked", "handled_by": "orchestrator",
            "reason": f"no reaction defined for entity_type={entity_type!r}"}


# ============================================================================
# TICK + LOOP
# ============================================================================

async def run_once() -> Dict[str, Any]:
    """Process one batch. Safe to call manually (tests / admin endpoint)."""
    cutoff = _CUTOFF or (datetime.now(timezone.utc) - timedelta(minutes=BACKFILL_MIN))
    events = await asyncio.to_thread(_claim_batch_sync, cutoff)
    summary = {"claimed": len(events), "results": []}
    for ev in events:
        et = ev["event_type"]
        try:
            handler = HANDLERS.get(et) or handle_default
            result = await handler(ev)
            await asyncio.to_thread(_complete_sync, ev["event_uuid"], result)
            summary["results"].append({"event": et, **result})
        except Exception as exc:
            logger.error(f"[agent_bus] handler {et} failed: {exc}", exc_info=True)
            await asyncio.to_thread(_fail_sync, ev["event_uuid"], ev["attempts"], str(exc))
            summary["results"].append({"event": et, "status": "error", "error": str(exc)})
    if events:
        logger.info(f"[agent_bus] tick — {summary}")
    return summary


def _rollup_overdue_sync(apply: bool) -> Dict[str, Any]:
    """One-time per-OWNER rollup of the material overdue-activity backlog: instead
    of ~660 per-activity nudges, raise ONE 'N overdue items' summary per owner.
    Absorbs any per-activity nudges already created, and settles the pending
    activity.overdue_flagged queue rows (+ their agent_inbox copies) so the backlog
    is drained and won't regenerate individual nudges. The per-activity handler
    stays for go-forward (low daily volume). Idempotent: one rollup per owner/day."""
    import json
    from datetime import date
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Global fallback anchor (notifications.event_uuid is NOT NULL).
            cur.execute("""SELECT event_uuid::text FROM events
                           WHERE event_type='activity.overdue_flagged' LIMIT 1""")
            r = cur.fetchone()
            fallback_anchor = r[0] if r else None

            cur.execute("""
                WITH mat AS (
                    SELECT a.owner_id, a.subject,
                           (now()::date - a.due_at::date) AS days_overdue
                    FROM   activities a
                    WHERE  a.status='open' AND a.due_at < now() AND a.owner_id IS NOT NULL
                      AND  (a.opportunity_id IS NOT NULL OR a.type IN ('call','meeting')
                            OR COALESCE(a.activity_score,0) > 15)
                ),
                by_subj AS (
                    SELECT owner_id, subject, count(*) AS cnt, max(days_overdue) AS maxd
                    FROM mat GROUP BY owner_id, subject
                )
                SELECT t.owner_id::text, t.n, t.max_days,
                       (SELECT e.event_uuid::text FROM events e
                          JOIN activities a2 ON a2.activity_id = e.entity_uuid
                         WHERE a2.owner_id = t.owner_id
                           AND e.event_type='activity.overdue_flagged' LIMIT 1) AS anchor,
                       (SELECT json_agg(json_build_object('subject', s.subject,
                                        'cnt', s.cnt, 'maxd', s.maxd))
                          FROM (SELECT * FROM by_subj b WHERE b.owner_id = t.owner_id
                                ORDER BY cnt DESC, maxd DESC LIMIT 5) s) AS top_subjects
                FROM (SELECT owner_id, count(*) AS n, max(days_overdue) AS max_days
                      FROM mat GROUP BY owner_id) t
            """)
            owners = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

            total_items = sum(o["n"] for o in owners)
            if not apply:
                return {"owners": len(owners), "would_rollup_items": total_items,
                        "rollups": 0, "nudges_absorbed": 0, "queue_settled": 0}

            today = date.today().isoformat()
            rollups = 0
            for o in owners:
                anchor = o["anchor"] or fallback_anchor
                if not anchor:
                    continue
                tops = o["top_subjects"] or []
                lines = [f"### ⏰ {o['n']} overdue items need your attention", ""]
                shown = 0
                for s in tops:
                    suffix = f" ×{s['cnt']}" if s["cnt"] > 1 else ""
                    lines.append(f"- {s['subject']}{suffix} — up to **{s['maxd']}d** overdue")
                    shown += s["cnt"]
                if o["n"] > shown:
                    lines.append(f"- …and {o['n'] - shown} more")
                body = "\n".join(lines)
                meta = json.dumps({"kind": "overdue_rollup", "source": "agent_bus",
                                   "count": o["n"], "max_days": o["max_days"], "day": today})
                # One active rollup per owner: refresh the existing unread one
                # (supersede prior days) rather than stacking a new row each run.
                cur.execute(
                    """SELECT notification_uuid FROM notifications
                       WHERE employee_uuid=%(o)s::uuid AND channel='in_app'
                         AND status <> 'read' AND metadata->>'kind'='overdue_rollup'
                       ORDER BY created_at DESC LIMIT 1""",
                    {"o": o["owner_id"]})
                ex = cur.fetchone()
                title = f"⏰ {o['n']} overdue items to action"
                if ex:
                    cur.execute("""UPDATE notifications SET title=%s, body=%s, metadata=%s,
                                   created_at=now() WHERE notification_uuid=%s""",
                                (title, body, meta, ex[0]))
                else:
                    cur.execute(
                        """INSERT INTO notifications
                             (employee_uuid, event_uuid, channel, status, title, body, metadata, created_at)
                           VALUES (%s::uuid, %s::uuid, 'in_app', 'pending', %s, %s, %s, now())""",
                        (o["owner_id"], anchor, title, body, meta))
                # Absorb any per-activity nudges already raised for this owner.
                cur.execute("""UPDATE notifications SET status='read', read_at=now()
                               WHERE employee_uuid=%s::uuid AND channel='in_app'
                                 AND status <> 'read' AND metadata->>'kind'='activity_nudge'""",
                            (o["owner_id"],))
                rollups += 1

            # Settle the pending overdue_flagged backlog (+ agent_inbox copies) so it
            # is drained and won't regenerate per-activity nudges.
            cur.execute("""UPDATE event_queue q
                           SET status='completed', last_attempt_at=now(), locked_by=NULL,
                               error_context='{"settled_by":"overdue_rollup"}'
                           FROM events e
                           WHERE e.event_uuid=q.event_uuid AND q.status='pending'
                             AND e.event_type='activity.overdue_flagged'""")
            queue_settled = cur.rowcount
            cur.execute("""UPDATE notifications n SET status='read', read_at=now()
                           FROM events e
                           WHERE e.event_uuid=n.event_uuid AND n.channel='agent_inbox'
                             AND n.status <> 'read' AND e.event_type='activity.overdue_flagged'""")
            inbox_settled = cur.rowcount
        conn.commit()
        return {"owners": len(owners), "rollups": rollups,
                "items_rolled_up": total_items, "queue_settled": queue_settled,
                "agent_inbox_settled": inbox_settled}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def rollup_overdue_activities(apply: bool = False) -> Dict[str, Any]:
    """Per-owner rollup of the material overdue-activity backlog (see
    _rollup_overdue_sync). Dry-run unless apply=True."""
    return await asyncio.to_thread(_rollup_overdue_sync, apply)


async def drain_backlog(max_total: int = 500, since_days: int = 365) -> Dict[str, Any]:
    """One-off controlled drain of the HISTORICAL queue (events emitted before the
    daemon's boot cutoff). Temporarily widens the eligibility window, processes in
    BATCH-sized waves until the queue is clear or `max_total` is reached, then
    restores the live cutoff. Only handler-registered types are ever touched, and
    every handler re-validates + idempotency-guards, so stale events (paid invoice,
    converted lead, completed activity) are safely skipped. Concurrency-safe with
    the live loop (FOR UPDATE SKIP LOCKED). Restartable: re-run to continue."""
    global _CUTOFF
    saved = _CUTOFF
    _CUTOFF = datetime.now(timezone.utc) - timedelta(days=since_days)
    processed, agg = 0, {}
    try:
        while processed < max_total:
            s = await run_once()
            if not s["claimed"]:
                break
            processed += s["claimed"]
            for r in s["results"]:
                key = f'{r.get("event")}:{r.get("status")}'
                agg[key] = agg.get(key, 0) + 1
    finally:
        _CUTOFF = saved
    logger.info(f"[agent_bus] drain_backlog processed={processed} breakdown={agg}")
    return {"processed": processed, "max_total": max_total,
            "since_days": since_days, "breakdown": agg,
            "note": "re-run to continue; live cutoff restored"}


async def _loop() -> None:
    logger.info(
        f"[agent_bus] consumer started (worker={WORKER_ID}, poll={POLL_SECS}s, "
        f"batch={BATCH}, autosend={AUTOSEND}, handlers={list(HANDLERS)})"
    )
    while not _stop.is_set():
        try:
            await run_once()
        except Exception as exc:
            logger.error(f"[agent_bus] tick crashed: {exc}", exc_info=True)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=POLL_SECS)
        except asyncio.TimeoutError:
            pass


def start_agent_bus() -> bool:
    """Launch the consumer loop. No-op unless AGENT_BUS_ENABLED=1."""
    global _task, _CUTOFF
    if not ENABLED:
        logger.info("[agent_bus] disabled (set AGENT_BUS_ENABLED=1 to activate)")
        return False
    if _task and not _task.done():
        return True
    _CUTOFF = datetime.now(timezone.utc) - timedelta(minutes=BACKFILL_MIN)
    _stop.clear()
    _task = asyncio.create_task(_loop())
    return True


async def stop_agent_bus() -> None:
    _stop.set()
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()


# ============================================================================
# Admin/demo endpoints (read-only status + on-demand tick)
# ============================================================================

router = APIRouter(tags=["agent-bus"])


@router.get("/agent-bus/status")
def agent_bus_status():
    return {
        "enabled": ENABLED, "autosend": AUTOSEND, "worker": WORKER_ID,
        "poll_secs": POLL_SECS, "batch": BATCH, "handlers": list(HANDLERS),
        "running": bool(_task and not _task.done()),
        "cutoff": _CUTOFF.isoformat() if _CUTOFF else None,
    }


@router.post("/agent-bus/run-once")
async def agent_bus_run_once():
    """Drive one tick on demand (handy for demos without waiting for the poll)."""
    if not _CUTOFF:
        # allow manual ticks even when the loop isn't running
        globals()["_CUTOFF"] = datetime.now(timezone.utc) - timedelta(minutes=max(BACKFILL_MIN, 60))
    return await run_once()


@router.post("/agent-bus/drain")
async def agent_bus_drain(max_total: int = 500, since_days: int = 365):
    """Controlled drain of the historical backlog (handler types only, capped,
    restartable). Safe even while gated — handlers re-validate every event."""
    return await drain_backlog(max_total=max_total, since_days=since_days)


@router.post("/agent-bus/rollup-overdue")
async def agent_bus_rollup_overdue(apply: bool = False):
    """Per-owner rollup of the material overdue-activity backlog (one summary per
    owner instead of per-activity nudges). Dry-run unless ?apply=true."""
    return await rollup_overdue_activities(apply=apply)
