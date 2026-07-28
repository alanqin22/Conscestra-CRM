"""Work routing — C2.1 (Axis 5). Recommends; never assigns.

    routing recommendation   !=   assignment
    assignment               !=   work acceptance

This module answers "who SHOULD take this, and why?" and stops there. The
assignment itself stays where C1 put it: app/core/cases.py -> _mutate() ->
field history, reached by an explicit human act. A recommendation that assigned
itself would make the routing engine a second, ungoverned write path — the
thing C1 spent nine steps eliminating.

WHY DETERMINISTIC RULES AND NOT A MODEL
"Small cases to staff, large ones to executives" is business policy owned by a
manager. A model that infers it cannot be audited, cannot be edited by the
person accountable, and quietly changes between releases. Ordered rules can be
read, edited and explained. The LLM's role here is to phrase the explanation,
never to choose the target.

WHAT "AMOUNT" MEANS
A case has no monetary column. `account_value` is the CUSTOMER's significance —
open pipeline plus outstanding AR — so a value rule says "this customer
matters", not "this problem is expensive". Named explicitly because the field
reads like the latter.

HONEST LIMITATION
A recommendation can only name someone who is assignable (C2.0). With four
members today, most rules will match and then report that their target is not
yet grantable. That is the true state and the UI shows it rather than silently
falling back to someone arbitrary.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.core import assignable
from app.core.database import get_connection

logger = logging.getLogger("routing")


def _rows(sql: str, args=()) -> List[Dict[str, Any]]:
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.debug(f"[routing] query failed: {exc}")
        return []
    finally:
        if conn is not None:
            conn.close()


def available() -> bool:
    r = _rows("SELECT to_regclass('public.routing_rules') AS t")
    return bool(r and r[0]["t"])


# ============================================================================
# RULES
# ============================================================================

def _norm(v):
    """Compare the way the database stores it, so 50000 and Decimal('50000')
    are not mistaken for a change."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v).strip()


def rule_history(rule_id: str):
    """What this rule USED to route to. Policy is auditable like any other
    consequential record."""
    from app.core import history
    return history.read("routing_rule", rule_id)


def rules(include_inactive: bool = False) -> List[Dict[str, Any]]:
    where = "" if include_inactive else "WHERE is_active"
    return _rows(f"""SELECT rule_id::text, name, position, is_active,
                            min_account_value, max_account_value, priorities,
                            origins, subject_like, target_email, target_source,
                            requires_language, requires_skills,
                            reason, created_by, updated_at
                     FROM routing_rules {where}
                     ORDER BY position, name""")


def save_rule(rule: Dict[str, Any], *, actor: str = "admin") -> Dict[str, Any]:
    """Create or update one rule. The human edits the policy; nothing infers it."""
    name = str(rule.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "a rule needs a name"}
    if not (rule.get("target_email") or rule.get("target_source")):
        return {"ok": False, "error": "a rule must name a target — an email or "
                                      "a source tier; a rule that names nobody "
                                      "can never produce a recommendation"}
    # The BEFORE state, read before the write so the history chain is truthful.
    existing = None
    if rule.get("rule_id"):
        found = _rows("""SELECT position, is_active, min_account_value,
                                max_account_value, target_email, target_source,
                                requires_language
                         FROM routing_rules WHERE rule_id=%s::uuid""",
                      (rule["rule_id"],))
        existing = found[0] if found else None

    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            args = (name, int(rule.get("position") or 100),
                    bool(rule.get("is_active", True)),
                    rule.get("min_account_value"), rule.get("max_account_value"),
                    rule.get("priorities") or None, rule.get("origins") or None,
                    (rule.get("subject_like") or None),
                    (rule.get("target_email") or None),
                    (rule.get("target_source") or None),
                    (rule.get("requires_language") or None),
                    (rule.get("requires_skills") or None),
                    str(rule.get("reason") or ""), actor)
            if rule.get("rule_id"):
                cur.execute(
                    """UPDATE routing_rules SET name=%s, position=%s,
                         is_active=%s, min_account_value=%s,
                         max_account_value=%s, priorities=%s, origins=%s,
                         subject_like=%s, target_email=%s, target_source=%s,
                         requires_language=%s, requires_skills=%s,
                         reason=%s, created_by=%s, updated_at=now()
                       WHERE rule_id=%s::uuid RETURNING rule_id::text""",
                    args + (rule["rule_id"],))
            else:
                cur.execute(
                    """INSERT INTO routing_rules
                         (name, position, is_active, min_account_value,
                          max_account_value, priorities, origins, subject_like,
                          target_email, target_source, requires_language,
                          requires_skills, reason, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING rule_id::text""", args)
            row = cur.fetchone()
            rid = row[0] if row else None
            # Routing policy decides who gets work, so an edit to it is exactly
            # as consequential as a case reassignment — and audited the same
            # way, through the ONE shared writer. Only the fields that change
            # the routing OUTCOME are recorded; noise makes history unread.
            if rid:
                from app.core import history
                for field in ("position", "is_active", "min_account_value",
                              "max_account_value", "target_email",
                              "target_source", "requires_language"):
                    after = rule.get(field if field != "is_active"
                                     else "is_active",
                                     True if field == "is_active" else None)
                    before = (existing or {}).get(field)
                    if _norm(before) != _norm(after):
                        history.write(cur, "routing_rule", rid, field,
                                      before, after, actor=actor,
                                      source="routing-policy")
        conn.commit()
        return {"ok": True, "rule_id": rid}
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return {"ok": False, "error": str(exc)[:220]}
    finally:
        if conn is not None:
            conn.close()


def delete_rule(rule_id: str) -> Dict[str, Any]:
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM routing_rules WHERE rule_id=%s::uuid",
                        (rule_id,))
            n = cur.rowcount
        conn.commit()
        return {"ok": True, "deleted": n}
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        if conn is not None:
            conn.close()


# ============================================================================
# THE SIGNAL: customer significance
# ============================================================================

def account_value(account_id: Optional[str]) -> Dict[str, Any]:
    """Open pipeline + outstanding AR for the case's account.

    NOT the value of the case — a case has no monetary column. This measures
    how much the CUSTOMER matters, which is what "route big ones to an
    executive" actually means in a service context."""
    if not account_id:
        return {"value": 0.0, "known": False,
                "reason": "case is not linked to an account"}
    r = _rows("""SELECT
          COALESCE((SELECT sum(o.amount) FROM opportunities o
                    WHERE o.account_id=%s::uuid AND o.status='open'), 0) AS pipeline,
          COALESCE((SELECT sum(i.balance_due) FROM invoices i
                    WHERE i.account_id=%s::uuid AND i.balance_due > 0), 0) AS ar
        """, (account_id, account_id))
    if not r:
        return {"value": 0.0, "known": False, "reason": "lookup failed"}
    pipeline = float(r[0]["pipeline"] or 0)
    ar = float(r[0]["ar"] or 0)
    return {"value": round(pipeline + ar, 2), "known": True,
            "pipeline": round(pipeline, 2), "ar": round(ar, 2)}


# ============================================================================
# RECOMMENDATION — the whole public point of this module
# ============================================================================

def _matches(rule: Dict[str, Any], case: Dict[str, Any], value: float) -> bool:
    if rule.get("min_account_value") is not None and \
            value < float(rule["min_account_value"]):
        return False
    if rule.get("max_account_value") is not None and \
            value >= float(rule["max_account_value"]):
        return False
    if rule.get("priorities") and case.get("priority") not in rule["priorities"]:
        return False
    if rule.get("origins") and case.get("origin") not in rule["origins"]:
        return False
    if rule.get("subject_like"):
        frag = rule["subject_like"].strip("%").lower()
        if frag and frag not in str(case.get("subject") or "").lower():
            return False
    return True


def workload() -> Dict[str, int]:
    """Open cases per owner, COUNTED LIVE.

    Not a stored counter: that would drift the moment anything wrote a case
    outside its knowledge, and it would be a second source of truth for a
    number the cases table already answers exactly."""
    rows = _rows("""SELECT owner_id::text AS owner_id, count(*) AS n
                    FROM cases
                    WHERE owner_id IS NOT NULL AND is_historical = false
                      AND status IN ('new','in_progress','waiting')
                    GROUP BY 1""")
    return {r["owner_id"]: int(r["n"]) for r in rows}


def _eligible(person: Dict[str, Any], rule: Dict[str, Any]):
    """Why this person CANNOT take it, or None if they can.

    Absent data never fabricates a match. NULL languages means "we do not know
    what they speak", not "they speak everything" — so a rule requiring French
    excludes anybody not recorded as speaking it, possibly everyone, which is a
    correct and visible answer rather than a plausible wrong one."""
    need_lang = (rule.get("requires_language") or "").strip().lower()
    if need_lang:
        have = [str(x).lower() for x in (person.get("languages") or [])]
        if not have:
            return f"no recorded languages (needs {need_lang})"
        if need_lang not in have:
            return f"does not work in {need_lang} (has {', '.join(have)})"
    need_skills = [str(x).lower() for x in (rule.get("requires_skills") or [])]
    if need_skills:
        have = [str(x).lower() for x in (person.get("skills") or [])]
        missing = [k for k in need_skills if k not in have]
        if missing:
            return ("no recorded skills" if not have
                    else f"missing skill(s): {', '.join(missing)}")
    return None


def _candidates(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Who this rule names, filtered to people who may actually receive work.

    A rule may name someone who was never granted assignability, or whose
    membership was revoked. That produces NO candidate rather than a fallback —
    silently routing to whoever is left is how work lands on the wrong desk."""
    people = assignable.directory()
    if rule.get("target_email"):
        want = rule["target_email"].strip().lower()
        named = [p for p in people if (p["email"] or "").lower() == want]
    elif rule.get("target_source"):
        named = [p for p in people if p["source"] == rule["target_source"]]
    else:
        return []

    load = workload()
    out = []
    for p in named:
        why_not = _eligible(p, rule)
        out.append(dict(p, open_cases=load.get(p.get("owner_id") or "", 0),
                        ineligible_reason=why_not))

    eligible = [p for p in out if not p["ineligible_reason"]]
    # Least loaded first, then a STABLE tiebreak on email — two runs of the
    # same policy against the same data must produce the same order, or a
    # "recommendation" is a coin flip wearing a reason.
    eligible.sort(key=lambda p: (p["open_cases"], (p["email"] or "").lower()))
    for rank, p in enumerate(eligible, 1):
        p["rank"] = rank
        p["why"] = (f"least loaded of the eligible ({p['open_cases']} open)"
                    if rank == 1 else f"{p['open_cases']} open cases")
    # The excluded are RETURNED, not dropped: "nobody could take it" is only
    # useful if you can see who was considered and what disqualified them.
    return eligible + [p for p in out if p["ineligible_reason"]]


def recommend(case_id: str) -> Dict[str, Any]:
    """Who should take this case, and why. NEVER assigns.

    Returns the matched rule, the candidates it names, the reason to show a
    human, and — when a rule matches but names nobody assignable — says so
    explicitly instead of falling back."""
    if not available():
        return {"ok": False, "error": "routing rules not migrated "
                                      "(apply sql/routing_rules.sql)"}
    case = _rows("""SELECT case_id::text, subject, priority, origin,
                           account_id::text, owner_id::text, status,
                           is_historical
                    FROM cases WHERE case_id=%s::uuid""", (case_id,))
    if not case:
        return {"ok": False, "error": f"no such case: {case_id}"}
    case = case[0]

    val = account_value(case.get("account_id"))
    out: Dict[str, Any] = {
        "ok": True, "case_id": case["case_id"], "subject": case["subject"],
        "current_owner_id": case["owner_id"],
        "account_value": val,
        # Stated on every recommendation so nobody reads it as case cost.
        "value_basis": "customer significance (open pipeline + outstanding AR) "
                       "— a case carries no monetary value of its own",
    }

    for rule in rules():
        if not _matches(rule, case, val["value"]):
            continue
        considered = _candidates(rule)
        people = [p for p in considered if not p["ineligible_reason"]]
        out["matched_rule"] = {"rule_id": rule["rule_id"], "name": rule["name"],
                               "position": rule["position"],
                               "reason": rule["reason"],
                               "requires_language": rule.get("requires_language"),
                               "requires_skills": rule.get("requires_skills")}
        out["candidates"] = [{"email": p["email"], "owner_id": p["owner_id"],
                              "display_name": p["display_name"],
                              "source": p["source"], "rank": p["rank"],
                              "open_cases": p["open_cases"], "why": p["why"]}
                             for p in people]
        out["excluded"] = [{"email": p["email"],
                            "display_name": p["display_name"],
                            "reason": p["ineligible_reason"]}
                           for p in considered if p["ineligible_reason"]]
        if not people:
            target = (rule["target_email"] if rule.get("target_email")
                      else f"the {rule['target_source']} tier")
            if out["excluded"]:
                out["blocked"] = (
                    f"Rule {rule['name']!r} matched and "
                    f"{len(out['excluded'])} person(s) in {target} were "
                    f"considered, but none is eligible: "
                    + "; ".join(f"{e['display_name'] or e['email']} — "
                                f"{e['reason']}" for e in out["excluded"][:4]))
            else:
                out["blocked"] = (
                    f"Rule {rule['name']!r} matched, but it names {target}, "
                    "and nobody there is currently assignable. Grant "
                    "assignability before this rule can route anything.")
        return out

    out["matched_rule"] = None
    out["candidates"] = []
    out["blocked"] = "No rule matched. Add a catch-all rule to route the rest."
    return out


def preview(limit: int = 25) -> Dict[str, Any]:
    """What the current policy WOULD do across the live queue.

    The point of a dry run: a manager edits a rule and sees the consequence
    before a single case moves."""
    if not available():
        return {"ok": False, "error": "routing rules not migrated"}
    live = _rows("""SELECT case_id::text FROM cases
                    WHERE is_historical = false
                      AND status IN ('new','in_progress','waiting')
                    ORDER BY created_at LIMIT %s""", (max(1, min(limit, 200)),))
    results, blocked = [], 0
    for row in live:
        r = recommend(row["case_id"])
        if not r.get("ok"):
            continue
        if r.get("blocked"):
            blocked += 1
        av = r["account_value"]
        results.append({
            "case_id": r["case_id"], "subject": r["subject"],
            # The BREAKDOWN travels with every row. "Case value: $Z" would
            # imply a field the data model does not have.
            "account_value": av["value"],
            "account_significance": (
                f"${av.get('pipeline', 0):,.0f} open pipeline + "
                f"${av.get('ar', 0):,.0f} outstanding AR"
                if av.get("known") else "no linked account"),
            "rule": (r.get("matched_rule") or {}).get("name"),
            "candidates": [c["email"] for c in r.get("candidates", [])],
            "top_candidate": (r["candidates"][0]["email"]
                              if r.get("candidates") else None),
            "top_open_cases": (r["candidates"][0]["open_cases"]
                               if r.get("candidates") else None),
            "excluded": r.get("excluded", []),
            "blocked": r.get("blocked"),
        })
    return {"ok": True, "previewed": len(results), "blocked": blocked,
            "assignable_people": len(assignable.directory()),
            "value_basis": "customer significance (linked account: open "
                           "pipeline + outstanding AR) — a case carries no "
                           "monetary value of its own",
            "note": "Preview only. Nothing was assigned; assignment remains an "
                    "explicit act through app/core/cases.py.",
            "results": results}
