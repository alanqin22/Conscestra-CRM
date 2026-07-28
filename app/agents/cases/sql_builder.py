"""Cases executor — C1 Step 5.

NAMED sql_builder.py to match every other agent package, but it is deliberately
NOT one, and that difference is the whole architectural point of this step.

Every other agent composes SQL or a stored-procedure call and hands it to
execute_sp(). A case agent cannot do that. There are no case stored procedures,
and building some would route case writes around app/core/cases.py — the
authoritative write layer that Steps 2-4 established. That layer is the only
thing enforcing:

    * the state machine (a case cannot skip or reverse the lifecycle)
    * owner validation (an owner is a real CRM identity or NULL, never a string)
    * field history (status/owner/priority changes are provable afterwards)
    * one transaction per mutation (history and change survive or fail together)
    * the feature flags

An LLM emitting SQL would bypass all five. So:

    READS   parameterised SELECTs over an allow-listed column set
    WRITES  delegated to app.core.cases — no exceptions

The model chooses an ACTION. It never composes a statement.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.core import cases
from app.core.database import get_connection

logger = logging.getLogger(__name__)

READ_ACTIONS = ("list_cases", "get_case", "case_history", "case_queue",
                "list_owners")
WRITE_ACTIONS = ("open_case", "transition", "assign", "set_priority",
                 "add_comment")
ACTIONS = READ_ACTIONS + WRITE_ACTIONS

# The only columns any read may project. Widening this is a deliberate act, not
# a convenience — the semantic layer's lesson applied to the case surface.
_LIST_COLS = """c.case_id::text, c.subject, c.status, c.priority,
                c.owner_id::text, o.email AS owner_email,
                c.source_assignee, c.conversation_id::text,
                c.escalation_id::text, c.first_response_at, c.resolved_at,
                c.closed_at, c.reopen_count, c.is_historical, c.origin,
                c.created_at,
                e.sla_due_at, e.reason AS escalation_reason,
                e.status AS escalation_status"""

# Read-only joins used by every case read. The SLA deadline is the LINKED
# ESCALATION's — the case does not own one (open design question 3), and this
# view must not imply otherwise or invent a calculation.
_JOINS = """LEFT JOIN owners o ON o.owner_id = c.owner_id
            LEFT JOIN escalations e ON e.escalation_id = c.escalation_id"""

_VALID_STATUS = set(cases.STATUSES)
_VALID_PRIORITY = set(cases.PRIORITIES)


def _rows(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            out = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in out:
        for k, v in list(r.items()):
            if hasattr(v, "isoformat"):
                r[k] = v.isoformat()
    return out


def execute(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run one action. Returns {ok, action, rows|result, error?}."""
    params = params or {}
    if action not in ACTIONS:
        return {"ok": False, "action": action,
                "error": f"unknown action '{action}'"}
    try:
        return _dispatch(action, params)
    except cases.InvalidTransition as exc:
        # A refused transition is a correct answer, not a crash — the user
        # asked for something the lifecycle does not permit and deserves to be
        # told exactly that.
        return {"ok": False, "action": action, "refused": True,
                "error": str(exc)}
    except cases.CaseError as exc:
        return {"ok": False, "action": action, "refused": True,
                "error": str(exc)}
    except Exception as exc:
        logger.exception(f"[cases-agent] {action} failed")
        return {"ok": False, "action": action, "error": str(exc)[:300]}


def _dispatch(action: str, p: Dict[str, Any]) -> Dict[str, Any]:
    # ── reads ───────────────────────────────────────────────────────────────
    if action == "list_cases":
        where, args = ["c.is_historical = false"], {}
        if p.get("status"):
            if p["status"] not in _VALID_STATUS:
                return {"ok": False, "action": action,
                        "error": f"unknown status '{p['status']}'"}
            where.append("c.status = %(status)s")
            args["status"] = p["status"]
        if p.get("priority"):
            if p["priority"] not in _VALID_PRIORITY:
                return {"ok": False, "action": action,
                        "error": f"unknown priority '{p['priority']}'"}
            where.append("c.priority = %(priority)s")
            args["priority"] = p["priority"]
        if p.get("unowned"):
            where.append("c.owner_id IS NULL")
        if p.get("owner_email"):
            where.append("lower(o.email) = lower(%(oe)s)")
            args["oe"] = p["owner_email"]
        args["lim"] = max(1, min(int(p.get("limit") or 20), 100))
        rows = _rows(
            f"""SELECT {_LIST_COLS} FROM cases c {_JOINS}
                WHERE {' AND '.join(where)}
                ORDER BY c.created_at DESC LIMIT %(lim)s""", args)
        return {"ok": True, "action": action, "rows": rows}

    if action == "case_queue":
        rows = _rows(
            f"""SELECT {_LIST_COLS} FROM cases c {_JOINS}
                WHERE c.is_historical = false
                  AND c.status IN ('new','in_progress','waiting')
                ORDER BY c.created_at LIMIT %(lim)s""",
            {"lim": max(1, min(int(p.get("limit") or 25), 100))})
        return {"ok": True, "action": action, "rows": rows}

    if action == "get_case":
        cid = _need(p, "case_id")
        rows = _rows(
            f"""SELECT {_LIST_COLS}, c.description,
                       a.account_name, c.account_id::text, c.contact_id::text
                FROM cases c {_JOINS}
                LEFT JOIN accounts a ON a.account_id = c.account_id
                WHERE c.case_id = %(cid)s::uuid""", {"cid": cid})
        if not rows:
            return {"ok": False, "action": action,
                    "error": f"no such case: {cid}"}
        rows[0]["comments"] = _rows(
            """SELECT comment, is_internal, created_at FROM case_comments
               WHERE case_id = %(cid)s::uuid
               ORDER BY created_at LIMIT 50""", {"cid": cid})
        rows[0]["next_states"] = list(cases.TRANSITIONS.get(rows[0]["status"], ()))
        return {"ok": True, "action": action, "rows": rows}

    if action == "list_owners":
        return {"ok": True, "action": action, "rows": _rows(
            """SELECT owner_id::text, email,
                      trim(coalesce(first_name,'') || ' ' ||
                           coalesce(last_name,'')) AS name
               FROM owners
               WHERE coalesce(is_active, true) AND email IS NOT NULL
               ORDER BY 3, 2 LIMIT 500""", {})}

    if action == "case_history":
        cid = _need(p, "case_id")
        return {"ok": True, "action": action,
                "rows": cases.history(cid, limit=int(p.get("limit") or 100))}

    # ── writes — every one of these goes through the case write layer ───────
    actor = str(p.get("actor") or "cases-agent")
    if action == "open_case":
        r = cases.open_case(str(p.get("subject") or "").strip(),
                            actor=actor, source="cases-agent",
                            description=str(p.get("description") or ""),
                            priority=str(p.get("priority") or "medium"))
        return {"ok": True, "action": action, "result": r}

    if action == "transition":
        r = cases.transition(_need(p, "case_id"),
                             str(p.get("to_status") or ""),
                             actor=actor, source="cases-agent")
        return {"ok": True, "action": action, "result": r}

    if action == "assign":
        email = str(p.get("owner_email") or "").strip()
        owner_id = cases.resolve_owner(email)
        if not owner_id:
            # Not an error to work around: an unresolvable identity leaves the
            # case unowned, which is a truthful state C2 routing can act on.
            return {"ok": False, "action": action, "refused": True,
                    "error": f"{email!r} is not a known CRM owner — the case "
                             f"stays unowned rather than acquiring an "
                             f"invented identity"}
        r = cases.assign(_need(p, "case_id"), owner_id, actor=actor,
                         source="cases-agent")
        return {"ok": True, "action": action, "result": r}

    if action == "set_priority":
        r = cases.set_priority(_need(p, "case_id"),
                               str(p.get("priority") or ""),
                               actor=actor, source="cases-agent")
        return {"ok": True, "action": action, "result": r}

    if action == "add_comment":
        r = cases.comment(_need(p, "case_id"), str(p.get("body") or ""),
                          internal=bool(p.get("internal")))
        return {"ok": True, "action": action, "result": r}

    return {"ok": False, "action": action, "error": "unhandled action"}


def _need(p: Dict[str, Any], key: str) -> str:
    v = str(p.get(key) or "").strip()
    if not v:
        raise cases.CaseError(f"{key} is required")
    return v
