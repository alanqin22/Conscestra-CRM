"""Governed operational work items — an alert is an obligation, not an event.

    docs/governance/activation_plan.md §11 · baseline docs/architecture_assessment_2026-09-05.md

WHAT THE ASSESSMENT MEASURED. 109 `supervisor.alert` events in 30 days on
Railway; none had an assignee, a deadline or a closure state. The `bus_stalled`
rule fired once and then went silent because its detector only looked back 24
hours, while 39 events sat orphaned for 85 hours and the health page said
"critical" to nobody. An alert that is only an event row disappears the moment
the detection window closes.

A `governance_alerts` row does not disappear. It has:

    an ELIGIBLE HUMAN OWNER   fn_owner_eligible() enforced by trigger — the
                              authority named by the alert policy for its rule,
                              or the CEO with ownership_exception=true
    an SLA                    due_at from the policy's sla_hours
    an enforced LIFECYCLE     OPEN → ASSIGNED → ACKNOWLEDGED → IN_PROGRESS →
                              RESOLVED → CLOSED, with ESCALATED / CANCELLED as
                              the only side exits (trigger-enforced)
    an append-only HISTORY    governance_alert_transitions
    a DEDUPE KEY              one live obligation per rule; a re-detection
                              while it is open raises severity, never a twin

`sweep_sla()` escalates anything past due_at to the escalation authority (CEO,
D4) and records it. Nothing here resolves an alert on its own: resolution and
closure are human acts with an actor and evidence, except that the bus may
mark an event_orphaned alert RESOLVED (not closed) once it has replayed the
rows, because that resolution is the verifiable state of the queue.

Requires governance/sql/governance_activation.sql. Best-effort at every call
site that opens alerts: a missing migration degrades to a warning, never to a
broken supervisor tick or bus tick — but that degradation is itself reported
by platform_health as `alerts_unavailable`.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.database import get_connection
from app.core import governance_policy as gp

logger = logging.getLogger("governance_alerts")

LIVE = ("open", "assigned", "acknowledged", "in_progress", "escalated")
TERMINAL = ("resolved", "closed", "cancelled")
_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# HOW LONG AN ACKNOWLEDGEMENT QUIETS THE SWEEP (A-02).
#
# `sweep_sla` re-escalates anything live whose due_at has passed, and nothing
# moved due_at, so an already-overdue alert re-escalated on the next 15-minute
# tick however many times a human acknowledged it. Measured on production: the
# CEO acknowledged alert b9a224e2 at 15:55:05 and the sweep re-escalated it at
# 15:56:28 — 82 seconds. Three acknowledgements, none of which held.
#
# A WINDOW, NOT A NEW DEADLINE. due_at is deliberately untouched: the breach is
# a true fact and the audit trail keeps saying so. Extending due_at instead
# would let an alert be held forever by acknowledging it, and would erase the
# breach while doing so. What this suppresses is re-ANNOUNCEMENT, not the
# breach.
ACK_WINDOW_HOURS = float(os.getenv("GOV_ALERT_ACK_WINDOW_HOURS", "4"))

# THE CLOSED VOCABULARY FOR A RESOLUTION (A-06).
#
# Seven of the eight resolution notes in production are instructions that never
# executed — "bill these 5 shipped orders", "email this alter to CFO", "find the
# root cause, then resolve the issue". All four alerts open in production today
# are re-raises of rules "resolved" that way. Free text let an instruction
# masquerade as an outcome, so the outcome is now a separate, closed field.
#
# `delegated` is the entry that matters. It is the honest answer an executive
# had no way to give: somebody else will do this. Without it, the only way to
# get an alert off the desk was to claim it was handled.
DISPOSITIONS = ("worked", "no_action_needed", "delegated")

# A paragraph break inside a composed planner goal.
_BLANK_LINE = chr(10) + chr(10)


def _norm_sev(s: Optional[str]) -> str:
    s = (s or "medium").lower()
    return {"warning": "medium", "warn": "medium", "info": "low", "critical": "critical",
            "high": "high", "medium": "medium", "low": "low"}.get(s, "medium")


def _row_to_dict(cur, r) -> Dict[str, Any]:
    d = dict(zip([c[0] for c in cur.description], r))
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif k.endswith("_id") and v is not None and not isinstance(v, str):
            d[k] = str(v)
    return d


def _notify_owner(cur, owner_id: str, title: str, body: str, meta: Dict[str, Any],
                  event_type: str = "governance.alert_opened") -> None:
    """In-app notice to the accountable owner. The event is emitted first so the
    notification carries an event_uuid (NOT NULL in some deployments)."""
    try:
        cur.execute(
            "SELECT emit_event(%s,'alert',%s::uuid,%s::jsonb,NULL,'governance')",
            (event_type, meta.get("alert_id"), json.dumps({"context": meta})))
        ev = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO notifications
                 (employee_uuid, event_uuid, channel, status, title, body, metadata)
               VALUES (%s::uuid, %s, 'in_app', 'pending', %s, %s, %s::jsonb)""",
            (owner_id, ev, title[:200], body[:2000], json.dumps(meta)))
    except Exception as exc:                                       # noqa: BLE001
        logger.debug(f"[governance_alerts] notify skipped: {str(exc)[:140]}")


def open_alert(alert_class: str, headline: str, *, rule: Optional[str] = None,
               severity: str = "medium", source: str = "system",
               affected_type: Optional[str] = None, affected_id: Optional[str] = None,
               detail: Optional[Dict[str, Any]] = None,
               owner_role: Optional[str] = None, owner_id: Optional[str] = None,
               sla_hours: Optional[int] = None, dedupe_key: Optional[str] = None,
               correlation_id: Optional[str] = None) -> Dict[str, Any]:
    """Create the obligation (or fold into the live one with the same dedupe key).

    Returns {ok, alert_id, created, owner, exception}. Never raises: the caller
    is a supervisor tick, a bus tick or an SLA sweep, none of which may die
    because the obligation table is missing — but the failure is logged at
    WARNING, not DEBUG, because a silent degradation of this module is the
    exact defect it replaces."""
    sev = _norm_sev(severity)
    pol = gp.alert_policy_for(rule) if rule else gp._default_policy(f"alert:{alert_class}", "alert")
    hours = int(sla_hours or pol.get("sla_hours") or gp.DEFAULT_SLA_HOURS)
    try:
        if owner_id:
            owner = {"owner_id": owner_id, "label": None, "role": owner_role, "exception": False}
        else:
            owner = gp.resolve_accountable_owner(owner_role or pol.get("approver_role"))
    except gp.GovernanceConfigError as exc:
        logger.error(f"[governance_alerts] cannot own alert {alert_class}/{rule}: {exc}")
        return {"ok": False, "error": str(exc)}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if dedupe_key:
                cur.execute(
                    """SELECT alert_id::text, severity, status FROM governance_alerts
                        WHERE dedupe_key=%s AND status = ANY(%s) LIMIT 1""",
                    (dedupe_key, list(LIVE)))
                hit = cur.fetchone()
                if hit:
                    aid, cur_sev, st = hit
                    if _SEV_RANK[sev] > _SEV_RANK.get(cur_sev, 1):
                        cur.execute(
                            """UPDATE governance_alerts
                                  SET severity=%s, detail = detail || %s::jsonb
                                WHERE alert_id=%s::uuid""",
                            (sev, json.dumps({"re_detected": detail or {}}), aid))
                        conn.commit()
                    return {"ok": True, "alert_id": aid, "created": False,
                            "status": st, "owner": owner}
            cur.execute("SET LOCAL app.actor = %s", (source[:120],))
            cur.execute(
                """INSERT INTO governance_alerts
                     (alert_class, rule, severity, source, headline, detail,
                      affected_type, affected_id, accountable_owner_id, accountable_owner,
                      ownership_exception, sla_hours, due_at, escalation_role,
                      dedupe_key, correlation_id)
                   VALUES (%(cls)s, %(rule)s, %(sev)s, %(src)s, %(head)s, %(det)s::jsonb,
                           %(at)s, %(aid)s, %(oid)s::uuid, %(olabel)s, %(exc)s,
                           %(hrs)s, now() + make_interval(hours => %(hrs)s), %(esc)s,
                           %(dk)s, %(cid)s::uuid)
                   RETURNING alert_id::text, due_at""",
                {"cls": alert_class, "rule": rule, "sev": sev, "src": source[:120],
                 "head": headline[:400], "det": json.dumps(detail or {}, default=str),
                 "at": affected_type, "aid": str(affected_id) if affected_id else None,
                 "oid": owner["owner_id"], "olabel": owner.get("label"),
                 "exc": bool(owner.get("exception")), "hrs": hours,
                 "esc": pol.get("escalation_role") or gp.ESCALATION_ROLE,
                 "dk": dedupe_key, "cid": correlation_id})
            aid, due = cur.fetchone()
            _notify_owner(cur, owner["owner_id"],
                          f"⚠️ {sev.title()} alert needs an owner's attention: {headline[:80]}",
                          f"{headline}\n\nClass: {alert_class}"
                          + (f" · rule {rule}" if rule else "")
                          + f"\nDue: {due.isoformat() if due else '?'} (SLA {hours}h)"
                          + ("\nOwnership exception: routed to the CEO because the "
                             "responsible role has no eligible executive." if owner.get("exception") else "")
                          + "\nOpen Governance → Alert Center to acknowledge and work it.",
                          {"kind": "governance_alert", "alert_id": aid,
                           "alert_class": alert_class, "rule": rule, "severity": sev})
        conn.commit()

        # EMAIL THE OWNER, not just the CEO on breach.
        #
        # Reported 2026-09-08: alerts owned by the CTO and the CFO were arriving
        # in the CEO's inbox instead. They were -- and correctly, because the
        # ONLY two alert emails in this module were escalate() and
        # remind_escalated(), and both address gp.ESCALATION_ROLE. An alert
        # owned by Bill Wang was notified to Bill Wang IN-APP and emailed to
        # nobody, sat until its SLA breached, and then emailed the CEO. So the
        # CEO received mail about every alert in the system, and no other
        # executive ever received any.
        #
        # This is the activation plan's own reasoning (§26.6), applied to
        # approvals and never to alerts: "an in-app notice is only seen by
        # someone already looking at the CRM, and the failure this whole
        # activation exists to fix is precisely that nobody was looking."
        # route_for_approval mails the assigned executive; open_alert did not.
        # An owned obligation whose owner is never told is functionally unowned.
        #
        # ONLY ON CREATION. A dedupe fold returns the live alert and must not
        # re-mail -- the supervisor re-raises these every tick, and a rule that
        # mails on every tick is the alert storm this module already refuses
        # elsewhere. email_authority is additionally ledgered per (kind, ref)
        # through staff_email, so a retry cannot double-send.
        try:
            _owner_row = gp.authority_owner(owner.get("role")) if owner.get("role") else None
            if _owner_row:
                gp.email_authority(
                    _owner_row,
                    f"[Action needed] Alert assigned to you: {headline[:70]}",
                    f"{headline}\n\n"
                    f"Class:    {alert_class}\n"
                    f"Rule:     {rule or '-'}\n"
                    f"Severity: {sev}\n"
                    f"Due:      {due.isoformat() if due else '?'} (SLA {hours}h)\n\n"
                    + ("This was routed to you as an ownership exception, because "
                       "the responsible role has no eligible executive.\n\n"
                       if owner.get("exception") else
                       "It is yours to work. If it is not decided by the deadline "
                       f"it escalates to the {pol.get('escalation_role') or gp.ESCALATION_ROLE}.\n\n")
                    + f"Open it here:\n{console_link(aid)}\n\n"
                    f"Acknowledge it, work it, and close it with evidence.",
                    kind="alert_assigned", ref=aid)
        except Exception as exc:                                   # noqa: BLE001
            # WARNING, not debug: an owner who is never told is the defect this
            # block exists to fix, and a silent failure recreates it exactly.
            logger.warning(f"[governance_alerts] owner email FAILED for "
                           f"{alert_class}/{rule}: {str(exc)[:160]}")
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[governance_alerts] open failed ({alert_class}/{rule}): "
                       f"{str(exc).splitlines()[0][:200]}")
        return {"ok": False, "error": str(exc).splitlines()[0][:200]}
    finally:
        conn.close()
    logger.info(f"[governance_alerts] OPEN {aid[:8]} {alert_class}/{rule} sev={sev} "
                f"owner={owner.get('label') or owner['owner_id']}"
                + (" (exception)" if owner.get("exception") else ""))
    return {"ok": True, "alert_id": aid, "created": True, "owner": owner,
            "due_at": due.isoformat() if due else None}


def transition(alert_id: str, to_status: str, actor: str, *, note: Optional[str] = None,
               assignee: Optional[str] = None, evidence: Optional[Dict[str, Any]] = None,
               escalated_to_owner_id: Optional[str] = None,
               disposition: Optional[str] = None) -> Dict[str, Any]:
    """One lifecycle step. The trigger decides legality; this records who and why."""
    if not (actor or "").strip():
        return {"ok": False, "error": "actor is required"}
    if to_status == "resolved" and disposition not in DISPOSITIONS:
        # Refused HERE as well as by the trigger, so a caller gets a sentence it
        # can act on rather than a CheckViolation from two layers down. The
        # trigger stays the enforcement point — this is the error message.
        return {"ok": False,
                "error": f"resolving an alert requires a disposition, one of "
                         f"{', '.join(DISPOSITIONS)}. A note records what you "
                         f"thought; the disposition records what happened."}
    sets = ["status=%(st)s"]
    params: Dict[str, Any] = {"st": to_status, "id": alert_id, "actor": actor[:120],
                              "note": note}
    if to_status == "assigned":
        sets.append("assignee=%(assignee)s")
        params["assignee"] = assignee or actor
    if to_status == "acknowledged":
        sets.append("acknowledged_by=%(actor)s")
        # A-02: quiet the sweep for a window WITHOUT moving due_at. The alert
        # stays breached and keeps saying so; it simply stops being re-announced
        # every 15 minutes at somebody who has already said they have it.
        sets.append("ack_suppressed_until=now() + make_interval(secs => %(ackw)s)")
        params["ackw"] = ACK_WINDOW_HOURS * 3600.0
    if to_status == "resolved":
        sets += ["resolved_by=%(actor)s", "resolution_note=%(note)s",
                 "resolution_disposition=%(disp)s"]
        params["disp"] = disposition
    if to_status == "closed":
        sets += ["closed_by=%(actor)s", "closure_evidence=%(ev)s::jsonb"]
        params["ev"] = json.dumps(evidence or {}, default=str) if evidence is not None else None
    if to_status == "escalated":
        sets.append("escalated_to_owner_id=%(esc)s::uuid")
        params["esc"] = escalated_to_owner_id
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.actor = %(actor)s", params)
            if note:
                cur.execute("SET LOCAL app.note = %(note)s", params)
            cur.execute(
                f"UPDATE governance_alerts SET {', '.join(sets)} "
                f"WHERE alert_id=%(id)s::uuid RETURNING alert_id::text, status, "
                f"accountable_owner_id::text, headline, rule, alert_class", params)
            r = cur.fetchone()
            if not r:
                conn.rollback()
                return {"ok": False, "error": "alert not found"}
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc).splitlines()[0][:240]}
    finally:
        conn.close()
    logger.info(f"[governance_alerts] {alert_id[:8]} → {to_status} by {actor}")
    return {"ok": True, "alert_id": r[0], "status": r[1]}


def console_link(alert_id: Optional[str] = None) -> str:
    """Deep link to the Alert Center, for the person the mail is waking up.

    Escalation mail said "Open Governance -> Alert Center" and gave no address.
    Approval mail has carried one-click links for months; alert mail did not, so
    the one class of message that means "this is now YOUR problem" was the one
    that made the reader go and find it. On 2026-09-06 the CEO received seven of
    them and asked for the link, which is the right instinct: an escalation that
    costs effort to act on is an escalation that waits.

    Built from the PAGE origin, never the API origin. No `*.html` is in git, so
    the backend cannot serve this page in production and an APP_URL link would
    land on a 500 -- the same rule the password-reset mail follows.

    The alert id rides in the fragment rather than the query string. It is not a
    secret, but a fragment is not sent to the server, stays out of access logs,
    and is not forwarded as a referrer when the page loads its assets.
    """
    from app.core.order_status import _public_site
    base = f"{_public_site()}/governance-mgmt.html#alertCenter"
    return f"{base}:{alert_id}" if alert_id else base


def escalate(alert_id: str, actor: str = "sla-sweep", note: Optional[str] = None) -> Dict[str, Any]:
    """Escalate to the escalation authority (D4: CEO) and tell them."""
    try:
        ceo = gp.resolve_accountable_owner(gp.ESCALATION_ROLE)
    except gp.GovernanceConfigError as exc:
        return {"ok": False, "error": str(exc)}
    res = transition(alert_id, "escalated", actor, note=note or "SLA passed without closure",
                     escalated_to_owner_id=ceo["owner_id"])
    if not res.get("ok"):
        return res
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT headline, rule, alert_class, severity, due_at FROM governance_alerts "
                        "WHERE alert_id=%s::uuid", (alert_id,))
            h = cur.fetchone()
            if h:
                _notify_owner(cur, ceo["owner_id"],
                              f"🚨 Escalated to you: {h[0][:80]}",
                              f"{h[0]}\n\nThis {h[3]} {h[2]} alert passed its SLA "
                              f"({h[4].isoformat() if h[4] else '?'}) without being resolved. "
                              f"It is now yours to decide.",
                              {"kind": "governance_alert_escalated", "alert_id": alert_id,
                               "rule": h[1], "severity": h[3]},
                              event_type="governance.alert_escalated")
        conn.commit()
    except Exception as exc:                                       # noqa: BLE001
        conn.rollback()
        logger.warning(f"[governance_alerts] escalation notice skipped: {exc}")
    finally:
        conn.close()
    # EMAIL IMMEDIATELY (§26.6). An escalation that only exists in-app is seen
    # by whoever happens to open the CRM, which is the failure mode this whole
    # activation exists to remove. Ledgered and idempotent per alert.
    try:
        if h:
            _ceo_row = gp.authority_owner(gp.ESCALATION_ROLE)
            gp.email_authority(
                _ceo_row,
                f"[Action needed] Alert escalated to you: {h[0][:70]}",
                f"{h[0]}\n\n"
                f"Class:    {h[2]}\n"
                f"Rule:     {h[1] or '-'}\n"
                f"Severity: {h[3]}\n"
                f"Deadline: {h[4].isoformat() if h[4] else '?'} (passed)\n\n"
                f"It was not resolved before its deadline, so per policy it is now "
                f"yours.\n\n"
                f"Open it here:\n{console_link(alert_id)}\n\n"
                f"Acknowledge it, work it, and close it with evidence.",
                kind="alert_escalated", ref=alert_id)
    except Exception as exc:                                       # noqa: BLE001
        logger.warning(f"[governance_alerts] escalation email skipped: {exc}")
    return res


def delegate_to_agent(alert_id: str, actor: str, instruction: str,
                      *, principal: Optional[Any] = None) -> Dict[str, Any]:
    """Hand a live alert to the planner as governed work.

    WHY THIS EXISTS. On 2026-09-09 five alerts were closed by typing an
    instruction into the Resolve dialog: "bill these 5 shipped orders", "find
    the root cause, then resolve the issue", "remove them". Nothing executed
    them, because the resolution note is an audit field and not a command
    channel. The conditions were unchanged, the supervisor re-detected them the
    same morning, and the re-raised alerts breached their deadline and
    escalated to the CEO. A-06 has since made that particular mistake
    impossible, but it closed the wrong door on its own: it stopped an
    executive recording a delegation as an outcome without giving them any way
    to actually delegate. This is that way.

    IT DOES NOT RESOLVE THE ALERT, AND THAT IS THE POINT. Delegation is the
    start of work, not its completion. The alert moves to `in_progress`, the
    deadline keeps running, and the SLA sweep still escalates it if the plan
    produces nothing. Resolving on delegation would let an alert leave the desk
    on a promise, which is a quieter version of the failure above.

    THE TRANSITION IS CONTINGENT ON THE DISPATCH. If the planner cannot be
    reached, or refuses, the alert is left exactly where it was and the caller
    is told. An alert that says `in_progress` when nothing was dispatched would
    reproduce the original defect in a new place.

    WHAT THE PLANNER MAY DO. `crm.plan_execute` runs reads and turns writes into
    governed proposals; it sends nothing outbound. Delegating therefore queues
    work for approval and does not perform it.

    IT DOES NOT CONSULT `SUPERVISOR_PLANNER`, DELIBERATELY. That flag gates the
    scheduled tick, where the question is whether the platform may compose plans
    UNATTENDED. This path is attended by definition: a named executive pressed a
    button and typed the instruction. Requiring the flag would leave the button
    silently inert until an unrelated environment change, which is the failure
    mode the whole engagement keeps finding -- a control that appears to work
    and does nothing. If the intent is disabled at the mesh, the dispatch fails
    and the alert is left alone, which is the correct outcome and a visible one.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False,
                "error": "delegation requires an instruction saying what the "
                         "agent should do. Without one there is nothing to "
                         "dispatch, and the alert would move on an empty act."}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, headline, rule, severity "
                        "FROM governance_alerts WHERE alert_id=%s::uuid",
                        (alert_id,))
            row = cur.fetchone()
    except Exception as exc:                                       # noqa: BLE001
        return {"ok": False, "error": str(exc).splitlines()[0][:200]}
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "alert not found"}
    status, headline, rule, severity = row
    if status not in LIVE:
        return {"ok": False,
                "error": f"alert is {status}; only a live alert can be delegated"}

    # Imported rather than restated. `_breach_goal` already holds the business
    # play for each rule -- "Close revenue leakage ... find the shipped-but-
    # unbilled orders and generate their invoices" -- and a second copy here
    # would drift from the one the scheduled tick uses. The import is local
    # because supervisor imports this module.
    from app.core import supervisor as _sup

    base = _sup._breach_goal({"rule": rule, "headline": headline,
                              "severity": severity}) or headline
    goal = base + _BLANK_LINE + f"Instruction from {actor}: {instruction}"

    # Dedupe on the COMPOSED goal, which is what the planner tags its proposals
    # with. Re-sending the same instruction for the same breach is the double
    # dispatch worth refusing; a different instruction is different work and is
    # allowed through.
    if _sup._plan_already_queued(goal):
        return {"ok": False,
                "error": "a plan for this instruction is already queued for "
                         "approval; decide that one before dispatching another"}

    cid = str(_uuid.uuid4())
    try:
        res = _sup._run_coro(_sup._dispatch_plan(
            goal, cid, principal=principal, from_agent="governance-alerts"))
    except Exception as exc:                                       # noqa: BLE001
        res = {"ok": False, "error": str(exc).splitlines()[0][:200]}
    if not res or not res.get("ok"):
        err = (res or {}).get("error") or "unknown error"
        logger.warning(f"[governance_alerts] delegation dispatch FAILED for "
                       f"{alert_id[:8]}: {err}")
        return {"ok": False, "dispatched": False,
                "error": f"the agent could not be given this work ({err}). "
                         f"The alert is unchanged."}

    data = res.get("data") or {}
    steps = len(data.get("trace") or [])
    proposed = len(data.get("proposed_approvals") or [])
    note = (f"Delegated to the agent by {actor}: {instruction[:140]} "
            f"-- planned {steps} step(s), {proposed} queued for approval "
            f"(correlation {cid[:8]})")

    # open and assigned cannot reach in_progress directly; the lifecycle routes
    # them through acknowledged. Delegating an alert means the delegator has
    # read it, so acknowledging on their behalf states something already true.
    if status in ("open", "assigned"):
        ack = transition(alert_id, "acknowledged", actor, note=note)
        if not ack.get("ok"):
            return {"ok": False, "dispatched": True, "correlation_id": cid,
                    "error": f"the plan was dispatched but the alert could not "
                             f"be acknowledged: {ack.get('error')}"}
        status = "acknowledged"

    if status != "in_progress":
        mv = transition(alert_id, "in_progress", actor, note=note)
        if not mv.get("ok"):
            return {"ok": False, "dispatched": True, "correlation_id": cid,
                    "error": f"the plan was dispatched but the alert could not "
                             f"be moved to in_progress: {mv.get('error')}"}

    logger.info(f"[governance_alerts] {alert_id[:8]} DELEGATED by {actor} -- "
                f"{steps} step(s), {proposed} queued, cid={cid[:8]}")
    return {"ok": True, "alert_id": alert_id, "status": "in_progress",
            "dispatched": True, "steps": steps, "queued_for_approval": proposed,
            "correlation_id": cid, "goal": goal, "note": note}


def remind_escalated(hours: float) -> Dict[str, Any]:
    """Re-announce alerts that are ESCALATED and still nobody's work.

    Acknowledging pauses this — a human has picked it up and the alert's own
    lifecycle takes over. Resolving or closing stops it. Doing nothing does
    not: that is the whole point (plan §8)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT alert_id::text, headline, severity, escalation_notices
                     FROM governance_alerts
                    WHERE status='escalated'
                      AND COALESCE(last_escalation_notice_at, escalated_at)
                          < now() - make_interval(hours => %(h)s)
                    ORDER BY escalated_at LIMIT 50""", {"h": int(hours)})
            due = cur.fetchall()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[governance_alerts] reminder pass unavailable: {str(exc)[:120]}")
        return {"reminded": 0}
    finally:
        conn.close()

    sent = []
    for aid, headline, sev, count in due:
        n = int(count or 0) + 1
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # Stamped before the send, for the same reason as approvals: a
                # failed mail must not become a 15-minute loop.
                cur.execute(
                    """UPDATE governance_alerts
                          SET escalation_notices=%s, last_escalation_notice_at=now()
                        WHERE alert_id=%s::uuid AND status='escalated'""", (n, aid))
                if cur.rowcount != 1:
                    conn.rollback(); continue
                ceo = gp.authority_owner(gp.ESCALATION_ROLE)
                if ceo:
                    _notify_owner(cur, ceo["owner_id"],
                                  f"🔁 Reminder {n}: still open — {headline[:70]}",
                                  f"{headline}\n\nEscalated to you and still not "
                                  f"acknowledged. Reminder {n}; repeats every "
                                  f"{int(hours)}h until someone acknowledges it in "
                                  f"the Alert Center.",
                                  {"kind": "governance_alert_reminder",
                                   "alert_id": aid, "severity": sev, "reminder": n},
                                  event_type="governance.alert_escalated")
            conn.commit()
        except Exception as exc:                                   # noqa: BLE001
            conn.rollback()
            logger.warning(f"[governance_alerts] reminder failed for {aid[:8]}: {exc}")
            continue
        finally:
            conn.close()
        sent.append(aid)
        try:
            gp.email_authority(
                gp.authority_owner(gp.ESCALATION_ROLE),
                f"[Reminder {n}] Escalated alert still open: {headline[:60]}",
                f"{headline}\n\nSeverity {sev}. Escalated to you and still not "
                f"acknowledged. This is reminder {n} and repeats every "
                f"{int(hours)}h.\n\n"
                f"Open it here:\n{console_link(aid)}\n\n"
                f"Acknowledging it "
                f"stops the reminders; resolving and closing it ends the "
                f"obligation.",
                # `_remind` is the vocabulary's repeat suffix (staff_email.
                # EMAIL_KINDS); `reescalation` was this caller's own spelling
                # and was never a declared kind.
                kind="alert_remind", ref=f"{aid}:reminder:{n}")
        except Exception as exc:                                   # noqa: BLE001
            logger.debug(f"[governance_alerts] reminder email skipped: {exc}")
    if sent:
        logger.warning(f"[governance_alerts] re-announced {len(sent)} escalated alert(s)")
    return {"reminded": len(sent), "ids": sent}


def sweep_sla() -> Dict[str, Any]:
    """Escalate every live, un-escalated alert past its deadline, then
    re-announce anything escalated that nobody has picked up. Idempotent."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # A-02: `ack_suppressed_until` is the ONLY new clause. due_at is
            # still the breach test, so a breached alert is still breached and
            # still reported as such — it is merely not re-announced while
            # somebody who acknowledged it is inside their window.
            cur.execute(
                """SELECT alert_id::text FROM governance_alerts
                    WHERE status IN ('open','assigned','acknowledged','in_progress')
                      AND due_at < now()
                      AND (ack_suppressed_until IS NULL
                           OR ack_suppressed_until < now())
                    ORDER BY due_at LIMIT 200""")
            ids = [r[0] for r in cur.fetchall()]
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc).splitlines()[0][:160], "escalated": 0}
    finally:
        conn.close()
    done, failed = [], []
    for aid in ids:
        r = escalate(aid)
        (done if r.get("ok") else failed).append(aid)
    if done:
        logger.warning(f"[governance_alerts] SLA sweep escalated {len(done)} alert(s) to "
                       f"{gp.ESCALATION_ROLE}")
    import os as _os
    rem = remind_escalated(float(_os.getenv("GOV_REESCALATE_HOURS", "24")))
    return {"ok": True, "escalated": len(done), "failed": failed, "ids": done,
            "reminded": rem.get("reminded", 0)}


def resolve_by_class(alert_class: str, actor: str, note: str,
                     rule: Optional[str] = None,
                     disposition: str = "worked") -> Dict[str, Any]:
    """Resolve (not close) every live alert of a class — used by the bus after a
    replay proves the queue is drained. Closure stays human.

    `disposition` defaults to 'worked' because the only caller reaches here
    having actually replayed the rows, and a machine that did the work should
    say so in the same vocabulary a human uses. It is a PARAMETER rather than a
    literal so the next caller has to choose rather than inherit.

    WHAT THIS DISPOSITION DOES NOT CLAIM (A-04). 'worked' says the queue was
    drained, which is true and verifiable. It does NOT say a customer was
    notified: the 18 order.shipped events replayed on 2026-09-08 all produced
    order_notifications rows in state 'skipped', and the alert's note —
    "processed 39" — was read as if it meant delivery. Correcting that claim is
    a separate change to what the bus WRITES in the note, not to this field."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT alert_id::text FROM governance_alerts
                    WHERE alert_class=%s AND (%s IS NULL OR rule=%s)
                      AND status = ANY(%s)""",
                (alert_class, rule, rule, list(LIVE)))
            ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    out = []
    for aid in ids:
        # acknowledge first when needed so the transition is legal
        st = transition(aid, "acknowledged", actor, note=note)
        if not st.get("ok") and "cannot move" not in (st.get("error") or ""):
            out.append(st); continue
        out.append(transition(aid, "resolved", actor, note=note,
                              disposition=disposition))
    return {"resolved": sum(1 for o in out if o.get("ok")), "results": out}


def list_alerts(status: Optional[str] = None, owner_id: Optional[str] = None,
                include_terminal: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.*,
                          EXTRACT(EPOCH FROM (a.due_at - now()))/3600.0 AS hours_left,
                          (SELECT json_agg(json_build_object('at', t.at, 'from', t.from_status,
                                                             'to', t.to_status, 'actor', t.actor,
                                                             'note', t.note) ORDER BY t.at)
                             FROM governance_alert_transitions t WHERE t.alert_id=a.alert_id) AS history
                     FROM governance_alerts a
                    WHERE (%(st)s IS NULL OR a.status=%(st)s)
                      AND (%(own)s IS NULL OR a.accountable_owner_id=%(own)s::uuid
                           OR a.escalated_to_owner_id=%(own)s::uuid)
                      AND (%(term)s OR a.status NOT IN ('resolved','closed','cancelled'))
                    ORDER BY CASE a.status WHEN 'escalated' THEN 0 ELSE 1 END,
                             a.due_at NULLS LAST
                    LIMIT %(lim)s""",
                {"st": status, "own": owner_id, "term": bool(include_terminal), "lim": int(limit)})
            rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[governance_alerts] list unavailable: {exc}")
        return []
    finally:
        conn.close()
    for r in rows:
        if r.get("hours_left") is not None:
            r["hours_left"] = round(float(r["hours_left"]), 1)
    return rows


def metrics() -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FILTER (WHERE status = ANY(%(live)s)) AS live,
                          count(*) FILTER (WHERE status='open') AS open,
                          count(*) FILTER (WHERE status='escalated') AS escalated,
                          count(*) FILTER (WHERE status = ANY(%(live)s) AND due_at < now()) AS past_due,
                          count(*) FILTER (WHERE status = ANY(%(live)s) AND ownership_exception) AS ownership_exceptions,
                          count(*) FILTER (WHERE status = ANY(%(live)s) AND severity='critical') AS critical,
                          count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS opened_24h,
                          count(*) FILTER (WHERE resolved_at > now() - interval '24 hours') AS resolved_24h,
                          count(*) FILTER (WHERE closed_at > now() - interval '24 hours') AS closed_24h
                     FROM governance_alerts""", {"live": list(LIVE)})
            r = cur.fetchone()
            cols = [c[0] for c in cur.description]
            return {"available": True, **{k: int(v or 0) for k, v in zip(cols, r)}}
    except Exception as exc:
        conn.rollback()
        return {"available": False, "error": str(exc).splitlines()[0][:120]}
    finally:
        conn.close()


# ── Router (admin) ───────────────────────────────────────────────────────────

router = APIRouter(tags=["governance-alerts"])


@router.get("/governance/alerts")
def api_list(status: Optional[str] = None, owner_id: Optional[str] = None,
             include_terminal: bool = False, limit: int = 200):
    return {"alerts": list_alerts(status, owner_id, include_terminal, limit),
            "metrics": metrics()}


class _Open(BaseModel):
    headline: str
    alert_class: str = "manual"
    rule: Optional[str] = None
    severity: str = "medium"
    owner_role: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    source: str = "manual"


@router.post("/governance/alerts")
def api_open(body: _Open):
    return open_alert(body.alert_class, body.headline, rule=body.rule, severity=body.severity,
                      source=body.source, detail=body.detail, owner_role=body.owner_role)


class _Step(BaseModel):
    actor: str
    note: Optional[str] = None
    assignee: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    # A-06. Required to resolve; ignored on every other action. Accepted from
    # the body because it is a STATEMENT BY the actor, not a claim ABOUT them —
    # unlike `actor` itself, which is now taken from the session below.
    disposition: Optional[str] = None


_ACTIONS = {"assign": "assigned", "acknowledge": "acknowledged", "start": "in_progress",
            "resolve": "resolved", "close": "closed", "cancel": "cancelled",
            "reopen": "in_progress"}


# Transitions that CLEAR an accountability signal or MOVE it to someone else.
# Anyone may acknowledge that they have seen an alert; saying it is dealt with,
# that it never needed doing, or that it is now somebody else's, is a claim
# about the world — and N-02's rule is that a claim of that kind carries a name
# from the session rather than from a request body.
from app.core.governance_policy import bound_authority

# A-03. `resolve` and `close` were bound; the other three were not, and an ops
# token reached them with an actor of its own choosing. Verified against
# production on 2026-09-09: cancel, acknowledge, assign and escalate all
# returned "alert not found" (i.e. reached the handler) for a request carrying
# `"actor": "audit-probe-not-a-person"`.
#
#   cancel    removes the alert from the live surface exactly as resolve does —
#             `cancelled` is not in LIVE — and is the strongest claim of the
#             three: this never needed working. It was the one unbound
#             transition the local database had used 1,982 times.
#   assign    makes the alert someone else's obligation.
#   escalate  moves it to the escalation authority.
#
# `acknowledge` is deliberately still open, and that is a decision rather than
# an oversight: it asserts only that a human has seen the alert, it cannot
# clear or move accountability, and automation legitimately acknowledges on
# receipt. It is the one transition where a body-supplied actor costs nothing.
_BOUND_ACTIONS = {"resolve", "close", "cancel", "assign", "escalate",
                  "delegate"}

# `delegate` is bound for the same reason as the five above: it states
# that the work is now the agent's, which is a claim about the world and
# must carry a name from the session. It is also the only action that
# causes work to be dispatched, so the identity it records is the one the
# resulting plan and its proposals are attributed to.


@router.post("/governance/alerts/{alert_id}/{action}")
def api_step(request: Request, alert_id: str, action: str, body: _Step):
    actor = body.actor
    if action in _BOUND_ACTIONS:
        ex = bound_authority(request)
        actor = ex["email"]
    if action == "escalate":
        return escalate(alert_id, actor, body.note)
    if action == "delegate":
        # The principal is built from the session, not from `actor`, so the
        # plan and every proposal it queues are attributed to the executive who
        # asked for them rather than to a background service.
        from app.core.a2a import Principal
        principal = Principal.from_session(getattr(request.state, "session", None))
        res = delegate_to_agent(alert_id, actor, body.note or "",
                                principal=principal)
        if not res.get("ok"):
            raise HTTPException(status_code=409, detail=res.get("error"))
        return res
    if action not in _ACTIONS:
        raise HTTPException(status_code=404, detail=f"unknown action; one of {sorted(_ACTIONS)}, escalate or delegate")
    res = transition(alert_id, _ACTIONS[action], actor, note=body.note,
                     assignee=body.assignee, evidence=body.evidence,
                     disposition=body.disposition)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error"))
    return res


@router.post("/governance/alerts/sweep")
def api_sweep():
    return sweep_sla()
