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

BRANCHING (conditional routing) — playbooks are graphs now, not just lines
--------------------------------------------------------------------------
Optional step fields (all backward compatible — plain steps run linearly):
  "id": name          addressable step name (default: the action name)
  "next": id | None   where the flow goes after acting (default: next list
                      item; None = complete the run). Side-branch steps —
                      reachable only via a goto — sit AFTER a `next: None`
                      step so the linear walk never falls into them.
  "branch": [{"when": <condition>, "goto": <id>, "outcome": <opt>}]
                      evaluated when the step comes DUE, before acting:
                      first matching condition redirects — the TARGET step
                      acts now instead. Conditions live in _CONDITIONS
                      (deterministic entity/blackboard checks, no LLM).
Signal routing: SIGNAL_ROUTES lets an exit-check outcome ROUTE instead of
merely ending the run (e.g. lead replies mid-cadence → book the meeting
while it's hot, then complete). Each signal routes at most once per run and
branch jumps are capped (MAX_JUMPS) — no cycles.
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
        {"wait_hours": 168, "action": "move_to_nurture", "next": None},
        # Side branch — reached only when the lead ENGAGES mid-cadence (see
        # SIGNAL_ROUTES): don't just end the run, book the meeting while hot.
        {"id": "book_meeting", "wait_hours": 1, "action": "book_meeting",
         "next": None},
    ],
    # The churn-save play (accounts) — started by the intelligence scorer when
    # an account ENTERS the high churn band. The vision's "Sales flags churn →
    # Support checks complaints → offer goes out → escalate" story:
    #   1 (+1h)  Support beat: consolidate complaint/risk context, task the owner
    #   2 (+2d)  Marketing beat: personalized win-back offer DRAFT (their channel)
    #            — UNLESS there's an open complaint: a discount on top of an
    #            unresolved grievance reads as tone-deaf; escalate instead.
    #   3 (+5d)  Escalation beat: still silent → executive-outreach task
    # Exits early when the account is SAVED: new order (won_back), inbound touch
    # (re-engaged), or churn risk back to low (risk_subsided).
    "churn_save": [
        {"wait_hours": 1,   "action": "churn_context_check"},
        {"wait_hours": 48,  "action": "churn_offer_draft",
         "branch": [{"when": "complaint_on_blackboard",
                     "goto": "churn_exec_escalation",
                     "outcome": "complaint_escalated"}]},
        {"wait_hours": 120, "action": "churn_exec_escalation"},
    ],
}

# Exit-check outcomes that ROUTE to a step instead of ending the run.
# Only 'completed'-status signals route (moot/cancelled always end); each
# signal routes at most once per run (context.routed), so a re-fire of the
# same signal completes the run normally.
SIGNAL_ROUTES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "lead_followup": {"engaged": {"goto": "book_meeting"}},
}

MAX_JUMPS = 3   # branch-goto cap per run — a playbook typo can't loop forever


def _step_index(steps: List[Dict[str, Any]]) -> Dict[str, int]:
    """step id (default: action name) → 0-based list index."""
    return {(s.get("id") or s["action"]): i for i, s in enumerate(steps)}


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
    spec = get_playbook(playbook)
    if not spec:
        return {"status": "error", "reason": f"unknown playbook {playbook!r}"}
    if spec.get("entity_type") and spec["entity_type"] != entity_type:
        return {"status": "error",
                "reason": f"playbook {playbook!r} targets "
                          f"{spec['entity_type']!r} entities, not {entity_type!r}"}
    steps = spec["steps"]
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


def _advance_sync(sequence_uuid: str, from_step: int, to_step: int,
                  wait_hours: int, result: Dict[str, Any], next_at=None,
                  context: Optional[Dict[str, Any]] = None) -> None:
    """Move the pointer to `to_step` (1-based; linear = from_step+1, but branch
    gotos may land anywhere). Optionally persists updated run context (routing
    history). Guarded on the current step so a redelivered event can't double-fire."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE agent_sequences
                   SET step_no = %s,
                       next_step_at = COALESCE(%s, now() + make_interval(hours => %s)),
                       last_result = %s::jsonb,
                       context = COALESCE(%s::jsonb, context),
                       updated_at = now()
                   WHERE sequence_uuid = %s::uuid
                     AND status = 'active' AND step_no = %s""",
                (to_step, next_at, wait_hours, json.dumps(result),
                 json.dumps(context) if context is not None else None,
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
# BRANCH CONDITIONS — deterministic checks a due step can route on.
# fn(seq, entity) -> bool; keep them cheap, fresh-state, and LLM-free.
# ============================================================================

def _cond_complaint_on_blackboard(seq: Dict[str, Any],
                                  entity: Optional[Dict[str, Any]]) -> bool:
    """An unexpired complaint note exists for this entity (posted by the
    email.received handler / support agent)."""
    try:
        from app.core import blackboard
        return bool(blackboard.read(seq["entity_type"], seq["entity_uuid"],
                                    "complaint"))
    except Exception as exc:
        logger.warning(f"[sequences] complaint condition failed (treat False): {exc}")
        return False


def _cond_overdue_ar(seq: Dict[str, Any],
                     entity: Optional[Dict[str, Any]]) -> bool:
    """The account carries overdue invoices (from the intelligence profile) —
    e.g. don't lead with a discount while their balance is past due."""
    return bool(int((entity or {}).get("overdue_invoices") or 0))


_CONDITIONS = {
    "complaint_on_blackboard": _cond_complaint_on_blackboard,
    "overdue_ar":              _cond_overdue_ar,
}


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


def _act_book_meeting(seq: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, Any]:
    """Signal-routed step: the lead ENGAGED mid-cadence. Strike while hot —
    urgent owner task to lock a meeting within 24h, plus a blackboard note so
    every agent sees the lead is live."""
    from app.core import blackboard
    name = _lead_display(lead)
    _insert_activity_sync(
        "meeting",
        f"Cadence WIN: {name} replied – book the meeting NOW",
        (f"{name} engaged mid-cadence (inbound reply). Momentum is highest in "
         f"the first 24 hours — propose two concrete slots today and confirm "
         f"the meeting. Auto-routed by the lead_followup cadence."),
        due_hours=4, owner_id=lead.get("owner_id"), lead_id=lead["lead_id"])
    blackboard.post(
        "lead", lead["lead_id"], "orchestrator", "hot_engagement",
        f"{name} replied mid-cadence — meeting-booking task issued",
        {"playbook": seq["playbook"], "score": lead.get("score")},
        0.9, "info", 24 * 7)
    return {"action": "book_meeting", "activity": "meeting-booking task created"}


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
    "book_meeting":          _act_book_meeting,
    "move_to_nurture":       _act_move_to_nurture,
    "churn_context_check":   _act_churn_context_check,
    "churn_offer_draft":     _act_churn_offer_draft,
    "churn_exec_escalation": _act_churn_exec_escalation,
}

# entity_type → fresh-context loader used by the step handler
_LOADERS = {"lead": _load_lead_sync, "account": _load_account_sync}


# ============================================================================
# PLAYBOOKS AS DATA — agent_playbooks rows override the code PLAYBOOKS
# (improvement #5: a new cadence ships as a ROW, not a deploy). Rows are
# validated against the action/condition registries so arbitrary code can
# never enter through the table; invalid rows are skipped with a warning
# (code fallback still applies).
# ============================================================================

_pb_cache = {"at": 0.0, "v": {}}


def validate_playbook(spec: Dict[str, Any]) -> List[str]:
    """All the reasons this playbook spec is unusable ([] = valid)."""
    errs: List[str] = []
    et = spec.get("entity_type")
    if et and et not in _LOADERS:
        errs.append(f"entity_type {et!r} has no loader "
                    f"(known: {sorted(_LOADERS)})")
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return errs + ["steps must be a non-empty list"]
    ids: List[str] = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            errs.append(f"step {i}: not an object")
            continue
        if s.get("action") not in _ACTIONS:
            errs.append(f"step {i}: unknown action {s.get('action')!r} "
                        f"(registered: {sorted(_ACTIONS)})")
        try:
            if int(s.get("wait_hours")) < 0:
                raise ValueError
        except (TypeError, ValueError):
            errs.append(f"step {i}: wait_hours must be an int ≥ 0")
        ids.append(s.get("id") or str(s.get("action")))
    if len(set(ids)) != len(ids):
        errs.append(f"duplicate step ids: {sorted({x for x in ids if ids.count(x) > 1})}")
    known = set(ids)
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            continue
        if "next" in s and s["next"] is not None and s["next"] not in known:
            errs.append(f"step {i}: next {s['next']!r} is not a step id")
        for rule in (s.get("branch") or []):
            if not isinstance(rule, dict):
                errs.append(f"step {i}: branch rule not an object")
                continue
            if rule.get("when") not in _CONDITIONS:
                errs.append(f"step {i}: unknown branch condition {rule.get('when')!r} "
                            f"(registered: {sorted(_CONDITIONS)})")
            if rule.get("goto") not in known:
                errs.append(f"step {i}: branch goto {rule.get('goto')!r} is not a step id")
    for outcome, route in (spec.get("signal_routes") or {}).items():
        if not isinstance(route, dict) or route.get("goto") not in known:
            errs.append(f"signal route {outcome!r}: goto "
                        f"{(route or {}).get('goto')!r} is not a step id")
    return errs


def _load_db_playbooks_sync() -> Dict[str, Dict[str, Any]]:
    """Enabled agent_playbooks rows → validated specs (invalid rows skipped).
    Tolerates the table not existing (returns {})."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT playbook, entity_type, steps, signal_routes "
                        "FROM agent_playbooks WHERE enabled")
            rows = cur.fetchall()
    except Exception as exc:
        logger.debug(f"[sequences] agent_playbooks read skipped: {exc}")
        return {}
    finally:
        conn.close()
    out: Dict[str, Dict[str, Any]] = {}
    for name, et, steps, routes in rows:
        if isinstance(steps, str):
            steps = json.loads(steps or "[]")
        if isinstance(routes, str):
            routes = json.loads(routes or "{}")
        spec = {"steps": steps, "signal_routes": routes or {},
                "entity_type": et, "source": "db"}
        errs = validate_playbook(spec)
        if errs:
            logger.warning(f"[sequences] DB playbook {name!r} invalid — "
                           f"skipped (code fallback applies): {errs}")
            continue
        out[name] = spec
    return out


def db_playbooks(force: bool = False) -> Dict[str, Dict[str, Any]]:
    import time
    if force or time.time() - _pb_cache["at"] > 60:
        _pb_cache["v"] = _load_db_playbooks_sync()
        _pb_cache["at"] = time.time()
    return _pb_cache["v"]


def get_playbook(name: str) -> Optional[Dict[str, Any]]:
    """Resolved spec {'steps','signal_routes','entity_type','source'} —
    a DB row overrides the code playbook of the same name."""
    spec = db_playbooks().get(name)
    if spec:
        return spec
    steps = PLAYBOOKS.get(name)
    if steps is None:
        return None
    return {"steps": steps, "signal_routes": SIGNAL_ROUTES.get(name) or {},
            "entity_type": None, "source": "code"}


# ============================================================================
# BUS HANDLER — sequence.step_due
# ============================================================================

async def handle_sequence_step_due(event: Dict[str, Any]) -> Dict[str, Any]:
    """Run one due step of a playbook instance: re-validate, exit early if the
    goal is met/moot (or ROUTE if the signal has a route), evaluate the due
    step's branch conditions, act, advance along the graph (or complete)."""
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

    spec = await asyncio.to_thread(get_playbook, seq["playbook"])
    steps = (spec or {}).get("steps")
    if not steps or not (1 <= step_no <= len(steps)):
        await asyncio.to_thread(_finish_sync, seq_id, "failed",
                                f"unknown playbook/step {seq['playbook']}#{step_no}",
                                {}, seq.get("correlation_id"))
        return {"status": "error", "reason": f"unknown playbook {seq['playbook']!r}"}

    index = _step_index(steps)
    run_ctx = seq.get("context") or {}
    if isinstance(run_ctx, str):
        run_ctx = json.loads(run_ctx or "{}")
    routed = dict(run_ctx.get("routed") or {})
    jumps = list(run_ctx.get("jumps") or [])

    act_idx = step_no - 1          # 0-based step that will act
    redirect: Optional[str] = None
    outcome_override: Optional[str] = None
    ctx_dirty = False

    # Goal met or moot? Either ROUTE (signal has a route, once per run) or end.
    exit_ = await asyncio.to_thread(_exit_reason_sync, seq)
    if exit_:
        route = (spec.get("signal_routes") or {}).get(exit_["outcome"])
        tgt = index.get(route["goto"]) if route else None
        if (route and exit_["status"] == "completed" and tgt is not None
                and exit_["outcome"] not in routed):
            act_idx = tgt
            redirect = f"signal:{exit_['outcome']}->{route['goto']}"
            outcome_override = route.get("outcome") or exit_["outcome"]
            routed[exit_["outcome"]] = route["goto"]
            ctx_dirty = True
        else:
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

    # Branch conditions of the DUE step (skipped when a signal already routed).
    if not redirect:
        for rule in (steps[act_idx].get("branch") or []):
            cond = _CONDITIONS.get(rule.get("when", ""))
            tgt = index.get(rule.get("goto", ""))
            if not cond or tgt is None or tgt == act_idx:
                continue
            if len(jumps) >= MAX_JUMPS:
                logger.warning(f"[sequences] {seq_id} hit MAX_JUMPS — "
                               f"continuing linearly")
                break
            if await asyncio.to_thread(cond, seq, entity):
                redirect = f"branch:{rule['when']}->{rule['goto']}"
                jumps.append({"from": step_no, "to": tgt + 1,
                              "when": rule["when"]})
                act_idx = tgt
                outcome_override = rule.get("outcome")
                ctx_dirty = True
                break

    step = steps[act_idx]
    result = await asyncio.to_thread(_ACTIONS[step["action"]], seq, entity)
    if redirect:
        result["routed"] = redirect

    # Where does the flow go after the ACTED step? Explicit `next` wins
    # (None = complete); default is the following list item; past the end
    # completes. An unknown `next` id completes too (fail safe, logged).
    if "next" in step:
        next_idx = index.get(step["next"]) if step["next"] else None
        if step["next"] and next_idx is None:
            logger.warning(f"[sequences] {seq['playbook']}: unknown next "
                           f"{step['next']!r} — completing run")
    else:
        next_idx = act_idx + 1 if act_idx + 1 < len(steps) else None

    acted = f"{act_idx + 1}/{len(steps)}"
    if next_idx is None:
        outcome = outcome_override or "exhausted"
        await asyncio.to_thread(_finish_sync, seq_id, "completed", outcome,
                                result, seq.get("correlation_id"))
        return {"status": "ok", "step": acted, **result,
                "sequence": f"completed ({outcome})"}

    next_wait = int(steps[next_idx]["wait_hours"])
    aligned = _aligned_step_at(next_wait, (entity or {}).get("preferred_hour"))
    new_ctx = {**run_ctx, "routed": routed, "jumps": jumps} if ctx_dirty else None
    await asyncio.to_thread(_advance_sync, seq_id, step_no, next_idx + 1,
                            next_wait, result, aligned, new_ctx)
    return {"status": "ok", "step": acted, **result,
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
    merged = {k: {"source": "code", "steps": [s["action"] for s in v]}
              for k, v in PLAYBOOKS.items()}
    for k, spec in db_playbooks(force=True).items():
        merged[k] = {"source": "db",
                     "steps": [s["action"] for s in spec["steps"]]}
    return {"enabled": ENABLED, "playbooks": merged, "counts": counts}


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


@router.get("/sequences/playbooks")
def sequences_playbooks():
    """Every playbook, resolved: DB rows (including disabled) + code built-ins.
    A DB row named like a code playbook overrides it while enabled."""
    out = {k: {"source": "code", "entity_type": None, "enabled": True,
               "steps": v, "signal_routes": SIGNAL_ROUTES.get(k) or {}}
           for k, v in PLAYBOOKS.items()}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT playbook, entity_type, steps, signal_routes, "
                        "enabled, description, updated_by, updated_at "
                        "FROM agent_playbooks ORDER BY playbook")
            for name, et, steps, routes, enabled, desc, by, at in cur.fetchall():
                out[name] = {
                    "source": "db", "entity_type": et, "enabled": enabled,
                    "steps": steps, "signal_routes": routes or {},
                    "description": desc, "updated_by": by,
                    "updated_at": at.isoformat() if at else None,
                    "overrides_code": name in PLAYBOOKS,
                    "errors": validate_playbook(
                        {"entity_type": et, "steps": steps,
                         "signal_routes": routes or {}}) or None}
    except Exception as exc:
        logger.debug(f"[sequences] playbooks listing (table missing?): {exc}")
    finally:
        conn.close()
    return {"registries": {"actions": sorted(_ACTIONS),
                           "conditions": sorted(_CONDITIONS),
                           "entity_types": sorted(_LOADERS)},
            "playbooks": out}


@router.put("/sequences/playbooks/{name}")
def sequences_playbook_upsert(name: str, body: Dict[str, Any]):
    """Create or update a playbook as data. Validated against the registered
    action/condition registries — a row that names an unregistered action is
    refused, so this endpoint can never introduce new code paths."""
    spec = {"entity_type": body.get("entity_type"),
            "steps": body.get("steps"),
            "signal_routes": body.get("signal_routes") or {}}
    if not spec["entity_type"]:
        return {"ok": False, "errors": ["entity_type is required (lead|account)"]}
    errs = validate_playbook(spec)
    if errs:
        return {"ok": False, "errors": errs}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_playbooks
                     (playbook, entity_type, steps, signal_routes, enabled,
                      description, updated_by)
                   VALUES (%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)
                   ON CONFLICT (playbook) DO UPDATE
                   SET entity_type=EXCLUDED.entity_type, steps=EXCLUDED.steps,
                       signal_routes=EXCLUDED.signal_routes,
                       enabled=EXCLUDED.enabled,
                       description=EXCLUDED.description,
                       updated_by=EXCLUDED.updated_by, updated_at=now()""",
                (name, spec["entity_type"], json.dumps(spec["steps"]),
                 json.dumps(spec["signal_routes"]),
                 bool(body.get("enabled", True)), body.get("description"),
                 body.get("updated_by", "admin")))
        conn.commit()
    finally:
        conn.close()
    _pb_cache["at"] = 0.0     # take effect immediately
    logger.info(f"[sequences] playbook {name!r} upserted "
                f"(enabled={body.get('enabled', True)})")
    return {"ok": True, "playbook": name, "source": "db",
            "overrides_code": name in PLAYBOOKS,
            "steps": len(spec["steps"])}


@router.delete("/sequences/playbooks/{name}")
def sequences_playbook_delete(name: str):
    """Remove a DB playbook (code fallback, if any, applies again). Refused
    while active runs depend on it and no code fallback exists — they would
    orphan and fail their next step."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if name not in PLAYBOOKS:
                cur.execute("SELECT count(*) FROM agent_sequences "
                            "WHERE playbook=%s AND status='active'", (name,))
                n = cur.fetchone()[0]
                if n:
                    return {"ok": False,
                            "error": f"{n} active run(s) use {name!r} and no code "
                                     f"fallback exists — cancel them first"}
            cur.execute("DELETE FROM agent_playbooks WHERE playbook=%s "
                        "RETURNING playbook", (name,))
            deleted = cur.fetchone() is not None
        conn.commit()
    finally:
        conn.close()
    _pb_cache["at"] = 0.0
    return {"ok": deleted, "playbook": name,
            "fallback": "code" if name in PLAYBOOKS else None}


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
