"""Case-lifecycle analytics — C1 Step 7 (Axis 5).

WHY THIS IS A SEPARATE MODULE FROM agent_ops
--------------------------------------------
`agent_ops.metrics()` measures the CONVERSATION lifecycle: how many external
threads opened, how many reached `status='closed'`, how many were escalated,
how long a closed thread stayed open. Those numbers are correct and are NOT
changed here — changing a shipped metric's meaning in place would silently
invalidate every trend built on it.

But a closed conversation is not completed work:

    conversation resolved   !=   case resolved
    case created            !=   work accepted
    work accepted           !=   work completed

A customer chat can end in four minutes while the case it produced stays open
for three days. `agent_ops`' `avg_hours` is CONVERSATION DURATION and has never
been case resolution time; read as the latter it flatters the operation badly.

THE FOUR DISTINCT MOMENTS, and where each is recorded:

    Escalation   the obligation was incurred      escalations.created_at
    Assignment   the work was ACCEPTED            record_field_history
                                                  (owner_id NULL -> value)
    Case         the durable work record exists   cases.created_at
    Resolution   the work was COMPLETED           cases.resolved_at

Acceptance is the one that could not be measured before C1. It is not a column
on `cases` — the current `owner_id` only says who owns it NOW. The moment
ownership was first taken exists solely in `record_field_history`, which is
precisely why C7 was promoted into a constraint on C1 rather than deferred.

HISTORICAL ROWS ARE EXCLUDED FROM EVERY DURATION. The 120 pre-C1 cases have
NULL timestamps meaning UNKNOWN, not zero; averaging them in would manufacture
an instant response time for work nobody measured. They are reported as a
separate count so they are visible rather than quietly dropped.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.core.database import get_connection

logger = logging.getLogger("case_analytics")


def _pct(numer: float, denom: float) -> Optional[float]:
    return round(100.0 * numer / denom, 1) if denom else None


def _rows(cur, sql: str, params) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def metrics(days: int = 30) -> Dict[str, Any]:
    """Case-lifecycle metrics. Never raises — a reporting failure must not take
    a page down, so it degrades to an `error` key."""
    days = max(1, min(days, 365))
    out: Dict[str, Any] = {"window_days": days, "basis": "case lifecycle"}
    # Acquired INSIDE the try: a failure to connect is exactly the case where
    # "never raises" matters most, and leaving it outside made the promise
    # false for the most likely failure of all.
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.record_field_history')")
            if cur.fetchone()[0] is None:
                return {**out, "error": "case lifecycle not migrated "
                                        "(apply sql/case_lifecycle.sql)"}

            win = "make_interval(days => %s)"

            # ── volume ──────────────────────────────────────────────────────
            cur.execute(f"""
                SELECT COUNT(*)                                        AS created,
                       COUNT(*) FILTER (WHERE status IN ('new','in_progress','waiting')) AS open,
                       COUNT(*) FILTER (WHERE status = 'new')          AS unaccepted,
                       COUNT(*) FILTER (WHERE owner_id IS NULL)        AS unowned,
                       COUNT(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved,
                       COUNT(*) FILTER (WHERE closed_at IS NOT NULL)   AS closed,
                       COUNT(*) FILTER (WHERE reopen_count > 0)        AS reopened
                FROM cases
                WHERE is_historical = false
                  AND created_at > now() - {win}""", (days,))
            v = dict(zip([d[0] for d in cur.description], cur.fetchone()))
            v = {k: int(x or 0) for k, x in v.items()}
            out["volume"] = v
            out["resolution_rate"] = _pct(v["resolved"], v["created"])
            out["reopen_rate"] = _pct(v["reopened"], v["resolved"])
            out["unowned_rate"] = _pct(v["unowned"], v["created"])

            # ── work ACCEPTED — the moment ownership was first taken ─────────
            # Only record_field_history knows this. cases.owner_id says who
            # owns it now; it cannot say when, or that it was ever unowned.
            cur.execute(f"""
                SELECT COUNT(*) AS accepted,
                       AVG(EXTRACT(EPOCH FROM (first_assign - c.created_at))/3600.0)
                                AS avg_hours_to_accept
                FROM cases c
                JOIN LATERAL (
                    SELECT MIN(h.changed_at) AS first_assign
                    FROM record_field_history h
                    WHERE h.entity = 'case' AND h.entity_id = c.case_id
                      AND h.field = 'owner_id' AND h.new_value IS NOT NULL
                ) fa ON fa.first_assign IS NOT NULL
                WHERE c.is_historical = false
                  AND c.created_at > now() - {win}""", (days,))
            accepted, hrs_accept = cur.fetchone()
            out["acceptance"] = {
                "accepted": int(accepted or 0),
                "never_accepted": max(0, v["created"] - int(accepted or 0)),
                "avg_hours_to_accept": (round(float(hrs_accept), 2)
                                        if hrs_accept is not None else None),
                "acceptance_rate": _pct(int(accepted or 0), v["created"]),
            }

            # ── durations — measured rows only, never assumed ────────────────
            cur.execute(f"""
                SELECT AVG(EXTRACT(EPOCH FROM (first_response_at - created_at))/3600.0),
                       COUNT(*) FILTER (WHERE first_response_at IS NOT NULL),
                       AVG(EXTRACT(EPOCH FROM (resolved_at - created_at))/3600.0),
                       COUNT(*) FILTER (WHERE resolved_at IS NOT NULL)
                FROM cases
                WHERE is_historical = false
                  AND created_at > now() - {win}""", (days,))
            fr_h, fr_n, res_h, res_n = cur.fetchone()
            out["durations"] = {
                # `measured_n` is reported beside every average so a mean over
                # three rows can never be mistaken for a stable one.
                "avg_hours_to_first_response": (round(float(fr_h), 2)
                                                if fr_h is not None else None),
                "first_response_measured_n": int(fr_n or 0),
                "first_response_unknown_n": max(0, v["created"] - int(fr_n or 0)),
                "avg_hours_to_resolution": (round(float(res_h), 2)
                                            if res_h is not None else None),
                "resolution_measured_n": int(res_n or 0),
            }

            # ── the obligation side — escalations, NOT cases ─────────────────
            cur.execute("SELECT to_regclass('public.escalations')")
            if cur.fetchone()[0] is not None:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE e.status IN ('open','assigned')),
                           COUNT(*) FILTER (WHERE e.status IN ('open','assigned')
                                             AND e.sla_due_at < now()),
                           COUNT(*) FILTER (WHERE c.case_id IS NULL
                                             AND e.status IN ('open','assigned'))
                    FROM escalations e
                    LEFT JOIN cases c ON c.escalation_id = e.escalation_id""")
                live, breached, uncased = cur.fetchone()
                out["obligations"] = {
                    "live": int(live or 0),
                    "sla_breached": int(breached or 0),
                    # An obligation with no case is work someone promised and
                    # nothing durable records. This is the number Step 4b was
                    # built to make visible.
                    "without_a_case": int(uncased or 0),
                }

            # ── historical rows: counted, never averaged ─────────────────────
            cur.execute("SELECT COUNT(*) FROM cases WHERE is_historical")
            out["historical"] = {
                "count": int(cur.fetchone()[0] or 0),
                "note": "pre-lifecycle records. Their response and resolution "
                        "times are UNKNOWN, not zero, so they are excluded "
                        "from every average above rather than counted as "
                        "instant.",
            }

            # ── backlog by owner — who is actually carrying the work ─────────
            out["backlog_by_owner"] = _rows(cur, """
                SELECT COALESCE(o.email, '(unowned)') AS owner,
                       COUNT(*)                       AS open_cases,
                       COUNT(*) FILTER (WHERE c.priority IN ('high','urgent'))
                                                      AS high_priority
                FROM cases c
                LEFT JOIN owners o ON o.owner_id = c.owner_id
                WHERE c.is_historical = false
                  AND c.status IN ('new','in_progress','waiting')
                GROUP BY 1 ORDER BY 2 DESC LIMIT 25""", ())
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.error(f"[case_analytics] failed: {exc}")
        return {**out, "error": str(exc)[:250]}
    finally:
        if conn is not None:
            conn.close()
    out["ok"] = True
    return out


def knowledge_signals(days: int = 90, limit: int = 20) -> Dict[str, Any]:
    """Evidence that the knowledge base may be missing something — C1 Step 8.

    Every number here is a SIGNAL, not a conclusion. Repetition means people
    keep hitting the same wall; it does not establish what the right answer is,
    and nothing in this function writes, proposes or publishes anything. The
    governed path from a resolved case to an article runs through
    knowledge.draft_pass() -> governance.propose('kb.publish') -> a human.

    Two signals, both cheap and both deterministic:

      repeated_subjects   the same issue produced several cases. Either the
                          answer is missing from the KB or the one that is
                          there does not work.
      escalated_with_kb   a case exists on a conversation where the agent DID
                          have knowledge to draw on. The KB answered and the
                          work still had to be done by a person, which is the
                          more interesting failure of the two.
    """
    days = max(1, min(days, 365))
    out: Dict[str, Any] = {"window_days": days, "basis": "evidence, not truth"}
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.cases')")
            if cur.fetchone()[0] is None:
                return {**out, "error": "cases not migrated"}

            out["repeated_subjects"] = _rows(cur, """
                SELECT lower(trim(subject)) AS subject,
                       COUNT(*)             AS cases,
                       COUNT(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved
                FROM cases
                WHERE is_historical = false
                  AND created_at > now() - make_interval(days => %s)
                GROUP BY 1 HAVING COUNT(*) > 1
                ORDER BY 2 DESC LIMIT %s""", (days, limit))

            cur.execute("""
                SELECT COUNT(*) FROM cases c
                WHERE c.is_historical = false
                  AND c.escalation_id IS NOT NULL
                  AND c.created_at > now() - make_interval(days => %s)""",
                        (days,))
            out["cases_from_an_obligation"] = int(cur.fetchone()[0] or 0)

            # Candidates the miner WOULD offer if the flag were on — shown so
            # the loop can be judged before it is switched on, not after.
            try:
                from app.core import knowledge
                out["mineable_now"] = len(knowledge._resolved_cases(limit))
            except Exception:
                out["mineable_now"] = None
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.error(f"[case_analytics] knowledge signals failed: {exc}")
        return {**out, "error": str(exc)[:250]}
    finally:
        if conn is not None:
            conn.close()
    out["ok"] = True
    out["note"] = ("Signals only. A resolved case is evidence that work was "
                   "completed, not that an answer is correct — every candidate "
                   "still goes through governance approval before it can be "
                   "retrieved by any agent.")
    return out


def semantics() -> Dict[str, Any]:
    """What each metric MEANS, and what it deliberately does not.

    Published beside the numbers because the failure this module exists to
    prevent is a reader assuming a conversation metric answers a work question.
    """
    return {
        "ok": True,
        "distinctions": [
            {"not": "conversation resolved", "is": "case resolved",
             "why": "a chat can end in minutes while the work it created runs "
                    "for days; agent_ops measures the chat"},
            {"not": "case created", "is": "work accepted",
             "why": "a case can sit unowned. Acceptance is the first owner "
                    "assignment, readable only from record_field_history"},
            {"not": "work accepted", "is": "work completed",
             "why": "acceptance sets an owner; completion sets resolved_at"},
        ],
        "moments": {
            "obligation": "escalations.created_at",
            "work_accepted": "record_field_history: owner_id NULL -> value",
            "work_record": "cases.created_at",
            "work_completed": "cases.resolved_at",
        },
        "unchanged_elsewhere": {
            "agent_ops.containment_rate": "CONVERSATION metric — closed threads "
                                          "with no human takeover. Unchanged.",
            "agent_ops.escalation_rate": "CONVERSATION metric. Unchanged.",
            "agent_ops.avg_hours": "CONVERSATION DURATION (created -> last "
                                   "update on a closed thread). It has never "
                                   "been case resolution time; the case figure "
                                   "is durations.avg_hours_to_resolution here.",
        },
        "exclusions": {
            "historical_cases": "excluded from every duration — NULL means "
                                "unknown, not zero",
            "sla": "read from the linked escalation only. This module adds no "
                   "SLA calculation, no waiting-pause policy and no "
                   "entitlement logic.",
        },
    }
