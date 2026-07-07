"""Agent Sequences — multi-step timed playbooks (cadences) on the agent bus.

WHAT THIS IS
------------
Bus handlers are one-shot reactions; this module adds the missing primitive for
the "Sales Agent" story — act, WAIT DAYS, re-check, act again, escalate:

    lead goes Hot ─▶ 1 intro email draft (+2h)
                 ─▶ 2 no reply? follow-up reminder (+3d)
                 ─▶ 3 still silent? offer a meeting (+4d)
                 ─▶ 4 move to nurture (+7d) and complete

One durable row per running instance lives in `agent_sequences`
(sql/agent_sequences.sql). A scheduled emitter (fn_emit_sequence_step_events,
wired in app/main.py) turns "a step fell due" into a normal `sequence.step_due`
bus event; the handler here re-validates the target, EXITS EARLY when the goal
is met (lead engaged / converted) or moot (disqualified / deleted), performs
the step, and advances the pointer. The consumer's retry/idempotency semantics
apply unchanged.

SAFETY
------
  • Opt-in: SEQUENCES_ENABLED=0 → start() no-ops and the emitter is skipped.
  • Steps are internal CRM records only (activities, blackboard notes) — no
    SMTP; the step-1 "email" is a DRAFT task for the owner. Outbound sending
    stays the Email agent's job under AGENT_BUS_AUTOSEND + governance.
  • Every step re-validates the entity fresh (never trusts the payload) and
    skips stale/duplicate step events (payload step_no vs row step_no).
  • One active run per (playbook, entity) — enforced by a partial unique index.

ADD A PLAYBOOK
--------------
Append to PLAYBOOKS: a list of steps, each {"wait_hours": H, "action": name}.
wait_hours is the delay from the PREVIOUS step (step 1: from start()). Implement
the action as `def _act_<name>(seq, ctx) -> dict` and add it to _ACTIONS, and an
exit-check per entity type in _exit_reason() if the defaults don't fit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("sequences")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("SEQUENCES_ENABLED")

# ── Playbooks ────────────────────────────────────────────────────────────────
# wait_hours = delay from the previous step (step 1: from sequence start).
PLAYBOOKS: Dict[str, List[Dict[str, Any]]] = {
    "lead_followup": [
        {"wait_hours": 2,   "action": "draft_intro_email"},
        {"wait_hours": 72,  "action": "reminder_task"},
        {"wait_hours": 96,  "action": "offer_meeting"},
        {"wait_hours": 168, "action": "move_to_nurture"},
    ],
    # The churn-save play (accounts) — started by the intelligence scorer when
    # an account ENTERS the high churn band. The vision's "Sales flags churn →
    # Support checks complaints → offer goes out → escalate" story:
    #   1 (+1h)  Support beat: consolidate complaint/risk context, task the owner
    #   2 (+2d)  Marketing beat: personalized win-back offer DRAFT (their channel)
    #   3 (+5d)  Escalation beat: still silent → executive-outreach task
    # Exits early when the account is SAVED: new order (won_back), inbound touch
    # (re-engaged), or churn risk back to low (risk_subsided).
    "churn_save": [
        {"wait_hours": 1,   "action": "churn_context_check"},
        {"wait_hours": 48,  "action": "churn_offer_draft"},
        {"wait_hours": 120, "action": "churn_exec_escalation"},
    ],
}


# ============================================================================
# STATE (synchronous psycopg2 — call via asyncio.to_thread from handlers)
# ============================================================================

def start(playbook: str, entity_type: str, entity_uuid: str,
          context: Optional[Dict[str, Any]] = None,
          started_by: str = "orchestrator",
          correlation_id: Optional[str] = None) -> Dict[str, Any]:
    """Start a playbook instance. No-op unless SEQUENCES_ENABLED; no-op if an
    active run already exists for (playbook, entity)."""
    if not ENABLED:
        return {"status": "skipped", "reason": "SEQUENCES_ENABLED=0"}
    steps = PLAYBOOKS.get(playbook)
    if not steps:
        return {"status": "error", "reason": f"unknown playbook {playbook!r}"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_sequences
                     (playbook, entity_type, entity_uuid, step_no, next_step_at,
                      context, started_by, correlation_id)
                   VALUES (%s, %s, %s::uuid, 1,
                           now() + make_interval(hours => %s),
                           %s::jsonb, %s, %s::uuid)
                   ON CONFLICT (playbook, entity_type, entity_uuid)
                       WHERE status = 'active'
                   DO NOTHING
                   RETURNING sequence_uuid::text, next_step_at""",
                (playbook, entity_type, entity_uuid,
                 int(steps[0]["wait_hours"]),
                 json.dumps(context or {}), started_by, correlation_id),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return {"status": "skipped", "reason": "already active for this entity"}
        logger.info(f"[sequences] started {playbook} for {entity_type}/{entity_uuid} "
                    f"(seq={row[0]}, step 1 at {row[1]})")
        return {"status": "ok", "sequence_uuid": row[0], "playbook": playbook,
                "next_step_at": row[1].isoformat()}
    finally:
        conn.close()


def _load_sync(sequence_uuid: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT sequence_uuid::text, playbook, entity_type,
                          entity_uuid::text, step_no, status, context,
                          correlation_id::text, created_at
                   FROM agent_sequences WHERE sequence_uuid = %s::uuid""",
                (sequence_uuid,),
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None
    finally:
        conn.close()


def _finish_sync(sequence_uuid: str, status: str, outcome: str,
                 result: Dict[str, Any], correlation_id: Optional[str]) -> None:
    """Terminal transition + lineage event (audit copy → Notifications inbox)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE agent_sequences
                   SET status=%s, outcome=%s, last_result=%s::jsonb, updated_at=now()
                   WHERE sequence_uuid=%s::uuid AND status='active'""",
                (status, outcome, json.dumps(result), sequence_uuid),
            )
            if cur.rowcount:
                cur.execute(
                    "SELECT emit_event(%s,%s,%s,%s,%s,%s,%s)",
                    ("sequence.completed", "sequence", sequence_uuid,
                     json.dumps({"context": {"status": status, "outcome": outcome}}),
                     None, "agent_bus", correlation_id),
                )
        conn.commit()
    finally:
        conn.close()


def _aligned_step_at(wait_hours: int, preferred_hour: Optional[int]):
    """Learning-loop nicety: for MULTI-DAY waits, land the step at the
    customer's preferred engagement hour (profile preferred_hour, ET) instead
    of an arbitrary time-of-day. Short waits keep their exact delay."""
    if preferred_hour is None or wait_hours < 24:
        return None
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    base = datetime.now(et) + timedelta(hours=int(wait_hours))
    target = base.replace(hour=int(preferred_hour), minute=0, second=0, microsecond=0)
    if target < base:
        target += timedelta(days=1)
    return target


def _advance_sync(sequence_uuid: str, from_step: int, wait_hours: int,
                  result: Dict[str, Any], next_at=None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE agent_sequences
                   SET step_no = %s,
                       next_step_at = COALESCE(%s, now() + make_interval(hours => %s)),
                       last_result = %s::jsonb, updated_at = now()
                   WHERE sequence_uuid = %s::uuid
                     AND status = 'active' AND step_no = %s""",
                (from_step + 1, next_at, wait_hours, json.dumps(result),
                 sequence_uuid, from_step),
            )
        conn.commit()
    finally:
        conn.close()


def cancel(sequence_uuid: str, reason: str = "manual") -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE agent_sequences
                   SET status='cancelled', outcome=%s, updated_at=now()
                   WHERE sequence_uuid=%s::uuid AND status='active'""",
                (reason, sequence_uuid),
            )
            n = cur.rowcount
        conn.commit()
        return {"status": "ok" if n else "skipped", "cancelled": bool(n)}
    finally:
        conn.close()


# ============================================================================
# EXIT CHECKS — has the cadence's goal been met (or become moot)?
# ============================================================================

def _lead_exit_reason_sync(lead_id: str, since) -> Optional[Dict[str, str]]:
    """(status, outcome) if the lead_followup cadence should stop, else None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(l.converted, false), COALESCE(l.is_deleted, false),
                          COALESCE(l.status, '')
                   FROM leads l WHERE l.lead_id = %s::uuid""",
                (lead_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"status": "cancelled", "outcome": "lead not found"}
            converted, deleted, status = row
            if converted or status == "converted":
                return {"status": "completed", "outcome": "converted"}
            if deleted or status == "disqualified":
                return {"status": "cancelled", "outcome": f"lead {status or 'deleted'}"}
            # Engagement = any inbound touch since the cadence started → goal met.
            cur.execute(
                """SELECT 1 FROM activities
                   WHERE lead_id = %s::uuid AND direction = 'inbound'
                     AND created_at > %s LIMIT 1""",
                (lead_id, since),
            )
            if cur.fetchone():
                return {"status": "completed", "outcome": "engaged"}
            return None
    finally:
        conn.close()


def _account_exit_reason_sync(account_id: str, since) -> Optional[Dict[str, str]]:
    """(status, outcome) if the churn_save cadence should stop, else None.
    'Saved' = a new order, an inbound touch, or risk back to low."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(is_deleted, false), COALESCE(status, '')
                   FROM accounts WHERE account_id = %s::uuid""",
                (account_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"status": "cancelled", "outcome": "account not found"}
            deleted, status = row
            if deleted or status.lower() in ("inactive", "closed", "archived"):
                return {"status": "cancelled", "outcome": f"account {status or 'deleted'}"}
            cur.execute(
                """SELECT 1 FROM orders
                   WHERE account_id = %s::uuid AND deleted_at IS NULL
                     AND created_at > %s LIMIT 1""",
                (account_id, since),
            )
            if cur.fetchone():
                return {"status": "completed", "outcome": "won_back"}
            cur.execute(
                """SELECT 1 FROM activities
                   WHERE account_id = %s::uuid AND direction = 'inbound'
                     AND created_at > %s LIMIT 1""",
                (account_id, since),
            )
            if cur.fetchone():
                return {"status": "completed", "outcome": "re-engaged"}
            cur.execute(
                """SELECT churn_band FROM account_intelligence
                   WHERE account_id = %s::uuid""",
                (account_id,),
            )
            r = cur.fetchone()
            if r and r[0] == "low":
                return {"status": "completed", "outcome": "risk_subsided"}
            return None
    finally:
        conn.close()


def _exit_reason_sync(seq: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if seq["entity_type"] == "lead":
        return _lead_exit_reason_sync(seq["entity_uuid"], seq["created_at"])
    if seq["entity_type"] == "account":
        return _account_exit_reason_sync(seq["entity_uuid"], seq["created_at"])
    return None


# ============================================================================
# STEP ACTIONS — internal CRM records only (drafts/tasks), never SMTP
# ============================================================================

def _load_lead_sync(lead_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT lead_id::text, first_name, last_name, company, email,
                          score, owner_id
                   FROM leads WHERE lead_id = %s::uuid""",
                (lead_id,),
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None
    finally:
        conn.close()


def _insert_activity_sync(a_type: str, subject: str, description: str,
                          due_hours: int, *, owner_id=None, channel=None,
                          lead_id: Optional[str] = None,
                          account_id: Optional[str] = None) -> None:
    related_type = "lead" if lead_id else "account"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                     (type, status, subject, description, due_at, direction, channel,
                      owner_id, related_type, related_id, lead_id, account_id,
                      created_at, updated_at)
                   VALUES (%s, 'open', %s, %s, now() + make_interval(hours => %s),
                           'outbound', %s, %s, %s, %s::uuid, %s::uuid, %s::uuid,
                           now(), now())""",
                (a_type, subject, description, due_hours,
                 channel or ("email" if a_type == "email" else "phone"),
                 owner_id, related_type, lead_id or account_id,
                 lead_id, account_id),
            )
        conn.commit()
    finally:
        conn.close()


def _lead_display(lead: Dict[str, Any]) -> str:
    name = f"{lead.get('first_name') or ''} {lead.get('last_name') or ''}".strip()
    return name or lead.get("company") or "lead"


def _act_draft_intro_email(seq: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, Any]:
    name, company = _lead_display(lead), lead.get("company") or "their company"
    _insert_activity_sync(
        "email",
        f"Cadence 1/4: intro email draft – {name}",
        (f"Draft ready for review — personalized intro for {name} at {company} "
         f"(score {lead.get('score')}). Reference their interest and offer a quick "
         f"call. Auto-drafted by the lead_followup cadence; send via the Email agent."),
        due_hours=4, owner_id=lead.get("owner_id"), lead_id=lead["lead_id"])
    return {"action": "draft_intro_email", "activity": "email draft created"}


def _act_reminder_task(seq: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, Any]:
    name = _lead_display(lead)
    _insert_activity_sync(
        "task",
        f"Cadence 2/4: no response from {name} – send follow-up",
        (f"No inbound engagement from {name} 3 days after the intro. "
         f"Send a short follow-up referencing the first note."),
        due_hours=8, owner_id=lead.get("owner_id"), lead_id=lead["lead_id"])
    return {"action": "reminder_task", "activity": "follow-up task created"}


def _act_offer_meeting(seq: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, Any]:
    name = _lead_display(lead)
    _insert_activity_sync(
        "meeting",
        f"Cadence 3/4: offer meeting – {name}",
        (f"Still no reply from {name}. Offer a 15-minute intro meeting with two "
         f"concrete time slots — lower-friction than another email thread."),
        due_hours=24, owner_id=lead.get("owner_id"), lead_id=lead["lead_id"])
    return {"action": "offer_meeting", "activity": "meeting offer created"}


def _act_move_to_nurture(seq: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import blackboard
    name = _lead_display(lead)
    blackboard.post(
        "lead", lead["lead_id"], "orchestrator", "nurture",
        f"Cadence exhausted with no engagement — {name} moved to nurture",
        {"playbook": seq["playbook"], "score": lead.get("score")},
        0.8, "info", 24 * 30)
    _insert_activity_sync(
        "task",
        f"Cadence 4/4: moved to nurture – {name}",
        (f"No engagement across the 2-week cadence. {name} parked in nurture; "
         f"revisit on the next scoring cycle or inbound touch."),
        due_hours=24 * 7, owner_id=lead.get("owner_id"), lead_id=lead["lead_id"])
    return {"action": "move_to_nurture", "note": "nurture note posted"}


# ── churn_save step actions (account entity) ────────────────────────────────

def _load_account_sync(account_id: str) -> Optional[Dict[str, Any]]:
    """Account + its intelligence profile (LEFT JOIN — profile may not exist)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.account_id::text, a.account_name, a.email, a.owner_id,
                          i.churn_risk, i.ltv, i.preferred_channel,
                          i.preferred_hour, i.order_recency_days,
                          i.typical_gap_days, i.open_ar_balance, i.overdue_invoices
                   FROM accounts a
                   LEFT JOIN account_intelligence i ON i.account_id = a.account_id
                   WHERE a.account_id = %s::uuid""",
                (account_id,),
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None
    finally:
        conn.close()


def _act_churn_context_check(seq: Dict[str, Any], acct: Dict[str, Any]) -> Dict[str, Any]:
    """Support beat: consolidate the account's risk/complaint context from the
    shared blackboard + profile, post a churn_context note, task the owner."""
    from app.core import blackboard
    name = acct.get("account_name") or "account"
    notes = []
    try:
        notes = blackboard.read("account", acct["account_id"])
    except Exception as exc:
        logger.warning(f"[sequences] churn context blackboard read failed: {exc}")
    concerns = [n for n in notes
                if n.get("topic") in ("ar_risk", "deal_lost", "dunning_hold",
                                      "churn_risk", "overdue_activity", "complaint")]
    summary = "; ".join(f"{n['topic']}: {n['note']}" for n in concerns[:4]) \
        or "no adverse signals on the blackboard"
    risk = float(acct.get("churn_risk") or 0)
    ar = float(acct.get("open_ar_balance") or 0)
    blackboard.post(
        "account", acct["account_id"], "support", "churn_context",
        f"Churn-save context: {len(concerns)} adverse signal(s); AR ${ar:,.0f}",
        {"signals": [n.get("topic") for n in concerns], "open_ar_balance": ar},
        0.8, "warning", 24 * 14)
    _insert_activity_sync(
        "task",
        f"Churn-save 1/3: review {name} (risk {risk:.2f})",
        (f"{name} entered HIGH churn risk. Context check: {summary}. "
         f"{acct.get('order_recency_days')}d since last order vs typical "
         f"{acct.get('typical_gap_days')}d gap. Review before the win-back "
         f"offer goes out in 2 days."),
        due_hours=8, owner_id=acct.get("owner_id"), account_id=acct["account_id"])
    return {"action": "churn_context_check", "adverse_signals": len(concerns)}


def _act_churn_offer_draft(seq: Dict[str, Any], acct: Dict[str, Any]) -> Dict[str, Any]:
    """Marketing beat: personalized win-back offer DRAFT on the customer's
    preferred channel. Draft only — sending stays with the Email agent under
    AUTOSEND + governance (and CASL consent)."""
    from app.core import blackboard
    name = acct.get("account_name") or "account"
    channel = acct.get("preferred_channel") or "email"
    ltv = float(acct.get("ltv") or 0)
    _insert_activity_sync(
        "email" if channel == "email" else "call",
        f"Churn-save 2/3: win-back offer draft – {name}",
        (f"Win-back offer draft for {name} (LTV ${ltv:,.0f}, prefers {channel}). "
         f"Suggest a loyalty discount or account review referencing their usual "
         f"order rhythm. Approval per governance before any commercial send; "
         f"deliver via the Email agent (CASL consent applies)."),
        due_hours=8, owner_id=acct.get("owner_id"), channel=channel,
        account_id=acct["account_id"])
    blackboard.post(
        "account", acct["account_id"], "marketing", "winback_offer",
        f"Win-back offer drafted for {name} ({channel})",
        {"channel": channel, "ltv": ltv}, 0.8, "info", 24 * 14)
    return {"action": "churn_offer_draft", "channel": channel}


def _act_churn_exec_escalation(seq: Dict[str, Any], acct: Dict[str, Any]) -> Dict[str, Any]:
    """Escalation beat: still no engagement — surface to management."""
    from app.core import blackboard
    name = acct.get("account_name") or "account"
    ltv = float(acct.get("ltv") or 0)
    _insert_activity_sync(
        "task",
        f"Churn-save 3/3: ESCALATION – {name} unresponsive",
        (f"No re-engagement from {name} across the save play (context check + "
         f"win-back offer). LTV ${ltv:,.0f} at risk. Recommend direct executive "
         f"outreach or a account-review call this week."),
        due_hours=24, owner_id=acct.get("owner_id"), account_id=acct["account_id"])
    blackboard.post(
        "account", acct["account_id"], "orchestrator", "churn_escalated",
        f"Churn-save play exhausted — {name} escalated to management "
        f"(LTV ${ltv:,.0f})",
        {"ltv": ltv, "playbook": seq["playbook"]}, 0.9, "warning", 24 * 30)
    return {"action": "churn_exec_escalation", "escalated": True}


_ACTIONS = {
    "draft_intro_email":     _act_draft_intro_email,
    "reminder_task":         _act_reminder_task,
    "offer_meeting":         _act_offer_meeting,
    "move_to_nurture":       _act_move_to_nurture,
    "churn_context_check":   _act_churn_context_check,
    "churn_offer_draft":     _act_churn_offer_draft,
    "churn_exec_escalation": _act_churn_exec_escalation,
}

# entity_type → fresh-context loader used by the step handler
_LOADERS = {"lead": _load_lead_sync, "account": _load_account_sync}


# ============================================================================
# BUS HANDLER — sequence.step_due
# ============================================================================

async def handle_sequence_step_due(event: Dict[str, Any]) -> Dict[str, Any]:
    """Run one due step of a playbook instance: re-validate, exit early if the
    goal is met/moot, act, advance (or complete after the last step)."""
    seq_id = str(event["entity_uuid"])
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    # emit_event() canonical envelope: business keys live under 'context'.
    ctx = payload.get("context") or {}

    seq = await asyncio.to_thread(_load_sync, seq_id)
    if not seq or seq["status"] != "active":
        return {"status": "skipped", "reason": "sequence not active"}

    step_no = int(seq["step_no"])
    ev_step = ctx.get("step_no")
    if ev_step is not None and int(ev_step) != step_no:
        return {"status": "skipped", "reason": "stale step event"}

    steps = PLAYBOOKS.get(seq["playbook"])
    if not steps or not (1 <= step_no <= len(steps)):
        await asyncio.to_thread(_finish_sync, seq_id, "failed",
                                f"unknown playbook/step {seq['playbook']}#{step_no}",
                                {}, seq.get("correlation_id"))
        return {"status": "error", "reason": f"unknown playbook {seq['playbook']!r}"}

    # Goal met or moot? End the cadence instead of acting.
    exit_ = await asyncio.to_thread(_exit_reason_sync, seq)
    if exit_:
        await asyncio.to_thread(_finish_sync, seq_id, exit_["status"],
                                exit_["outcome"], {"exited_at_step": step_no},
                                seq.get("correlation_id"))
        return {"status": "ok", "action": "exited", **exit_}

    loader = _LOADERS.get(seq["entity_type"])
    entity = await asyncio.to_thread(loader, seq["entity_uuid"]) if loader else None
    if loader and not entity:
        await asyncio.to_thread(_finish_sync, seq_id, "cancelled", "entity missing",
                                {}, seq.get("correlation_id"))
        return {"status": "skipped", "reason": "entity missing"}

    action_name = steps[step_no - 1]["action"]
    result = await asyncio.to_thread(_ACTIONS[action_name], seq, entity)

    if step_no >= len(steps):
        await asyncio.to_thread(_finish_sync, seq_id, "completed", "exhausted",
                                result, seq.get("correlation_id"))
        return {"status": "ok", "step": f"{step_no}/{len(steps)}", **result,
                "sequence": "completed (exhausted)"}

    next_wait = int(steps[step_no]["wait_hours"])
    aligned = _aligned_step_at(next_wait, (entity or {}).get("preferred_hour"))
    await asyncio.to_thread(_advance_sync, seq_id, step_no, next_wait, result, aligned)
    return {"status": "ok", "step": f"{step_no}/{len(steps)}", **result,
            "next_step_in_hours": next_wait,
            **({"aligned_to_preferred_hour": aligned.isoformat()} if aligned else {})}


# Register with the bus (module import is enough — main.py includes our router).
from app.core import agent_bus as _bus  # noqa: E402
_bus.HANDLERS["sequence.step_due"] = handle_sequence_step_due


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["sequences"])


@router.get("/sequences/status")
def sequences_status():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM agent_sequences GROUP BY 1")
            counts = dict(cur.fetchall())
    finally:
        conn.close()
    return {"enabled": ENABLED,
            "playbooks": {k: [s["action"] for s in v] for k, v in PLAYBOOKS.items()},
            "counts": counts}


@router.get("/sequences/list")
def sequences_list(status: str = "active", limit: int = 50):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT sequence_uuid::text, playbook, entity_type, entity_uuid::text,
                          step_no, status, outcome, next_step_at, updated_at
                   FROM agent_sequences WHERE status = %s
                   ORDER BY next_step_at LIMIT %s""",
                (status, limit),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"status": status, "count": len(rows), "sequences": rows}


@router.post("/sequences/start")
def sequences_start(body: Dict[str, Any]):
    return start(body.get("playbook", ""), body.get("entity_type", ""),
                 body.get("entity_uuid", ""), body.get("context"),
                 body.get("started_by", "admin"))


@router.post("/sequences/{sequence_uuid}/cancel")
def sequences_cancel(sequence_uuid: str, reason: str = "manual"):
    return cancel(sequence_uuid, reason)


@router.post("/sequences/run-once")
async def sequences_run_once():
    """Emit due steps now and drive one bus tick — demo/testing convenience."""
    def _emit() -> int:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT fn_emit_sequence_step_events(50)")
                return cur.fetchone()[0]
        finally:
            conn.close()

    emitted = await asyncio.to_thread(_emit)
    tick = await _bus.run_once()
    return {"emitted": emitted, "tick": tick}
