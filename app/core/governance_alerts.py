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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.database import get_connection
from app.core import governance_policy as gp

logger = logging.getLogger("governance_alerts")

LIVE = ("open", "assigned", "acknowledged", "in_progress", "escalated")
TERMINAL = ("resolved", "closed", "cancelled")
_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


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
               escalated_to_owner_id: Optional[str] = None) -> Dict[str, Any]:
    """One lifecycle step. The trigger decides legality; this records who and why."""
    if not (actor or "").strip():
        return {"ok": False, "error": "actor is required"}
    sets = ["status=%(st)s"]
    params: Dict[str, Any] = {"st": to_status, "id": alert_id, "actor": actor[:120],
                              "note": note}
    if to_status == "assigned":
        sets.append("assignee=%(assignee)s")
        params["assignee"] = assignee or actor
    if to_status == "acknowledged":
        sets.append("acknowledged_by=%(actor)s")
    if to_status == "resolved":
        sets += ["resolved_by=%(actor)s", "resolution_note=%(note)s"]
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
        logger.debug(f"[governance_alerts] escalation notice skipped: {exc}")
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
                f"yours. Open Governance -> Alert Center to acknowledge it, work it, "
                f"and close it with evidence.",
                kind="alert_escalated", ref=alert_id)
    except Exception as exc:                                       # noqa: BLE001
        logger.debug(f"[governance_alerts] escalation email skipped: {exc}")
    return res


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
                f"{int(hours)}h. Acknowledging it in Governance -> Alert Center "
                f"stops the reminders; resolving and closing it ends the "
                f"obligation.",
                kind="alert_reescalation", ref=f"{aid}:reminder:{n}")
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
            cur.execute(
                """SELECT alert_id::text FROM governance_alerts
                    WHERE status IN ('open','assigned','acknowledged','in_progress')
                      AND due_at < now()
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
                     rule: Optional[str] = None) -> Dict[str, Any]:
    """Resolve (not close) every live alert of a class — used by the bus after a
    replay proves the queue is drained. Closure stays human."""
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
        out.append(transition(aid, "resolved", actor, note=note))
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


_ACTIONS = {"assign": "assigned", "acknowledge": "acknowledged", "start": "in_progress",
            "resolve": "resolved", "close": "closed", "cancel": "cancelled",
            "reopen": "in_progress"}


@router.post("/governance/alerts/{alert_id}/{action}")
def api_step(alert_id: str, action: str, body: _Step):
    if action == "escalate":
        return escalate(alert_id, body.actor, body.note)
    if action not in _ACTIONS:
        raise HTTPException(status_code=404, detail=f"unknown action; one of {sorted(_ACTIONS)} or escalate")
    res = transition(alert_id, _ACTIONS[action], body.actor, note=body.note,
                     assignee=body.assignee, evidence=body.evidence)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error"))
    return res


@router.post("/governance/alerts/sweep")
def api_sweep():
    return sweep_sla()
