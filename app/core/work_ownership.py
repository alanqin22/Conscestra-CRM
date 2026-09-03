"""Work ownership — can this work item name a human who is accountable for it?

    A notification recipient is not an owner.
    An owner_id is not a person.
    A person is not necessarily staff.

THE QUESTION THIS MODULE EXISTS TO ANSWER, AND THE ONE IT REFUSES

    For a given work item: is there a deterministic, eligible HUMAN owner?

It answers OWNED, UNOWNED or IDENTITY_UNRESOLVED, and there is deliberately no
fourth answer — nothing here falls through to "route it anyway". What it will
not do is *establish* an identity: no name matching, no email matching, no
domain inference, no probabilistic linking. Every rule below reads a
deterministic key or a declaration a human recorded.

    docs/staff_email_authorization_gate.md
    docs/identity_resolution_spec.md §3 (the evidence hierarchy)

WHY THIS IS NOT `notifications.employee_uuid`

`fan_out_notifications_for_event()` populates that column from
`event_subscriptions.employee_uuid` — the rows are SUBSCRIBERS. The base table
is named `notification_recipients`; `notifications` is a view over it. One
`invoice.overdue` event fans out to five of them: two AI agents, one service
account and two humans. Nothing in that path expresses accountability, and a
column that means "was copied to" cannot be read as "is responsible for" no
matter how convenient that would be.

WHY IT IS NOT THE ENTITY'S `owner_id` EITHER — THE MEASUREMENT THAT DECIDED IT

The obvious repair is to walk the work item to its entity and read the owner
there. It is deterministic, it is a real foreign key, and on this corpus it is
POPULATED — `leads.owner_id` is 100/100. It is also, measured 2026-09-02:

    37 distinct lead owners
      36  are CUSTOMER CONTACTS   (contacts.contact_id = owner_id, shared PK)
       1  is an employee — and it is a1451ad6…, the F1 collision itself
       0  are authorized to receive work

`owners` corpus-wide is 39 of 44 customer contacts. So routing on entity
ownership would have delivered internal staff worklists to CUSTOMERS, and it
would have bypassed the collision quarantine in `staff_email.resolve_recipient`
entirely, because it never touches the column that quarantine guards.

That is why an owner_id is checked against personhood and customer-ness here
rather than being trusted because it resolved.

PERSONHOOD IS DECLARED, NOT INFERRED

`dsar.staff_personhood()` already answers "which of these rows is a natural
person", fail-closed, and it is reused rather than re-derived. It keys on
`role='agent'` plus an individually declared exception list, because the two
available signals are each insufficient alone: `role` misses `sysadmin`, whose
role reads 'Administrator', and the `@system.internal` domain is a MUTABLE
ATTRIBUTE — the exact mistake that silently re-labelled 39 customers during a
seed migration. Measured today: 8 people, 13 service identities, 0
unclassifiable.

This module reads. It never writes, grants, merges or repairs.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.database import get_connection

logger = logging.getLogger("work_ownership")

# The three states, and there is no fourth. `_TOTAL_IS_PARTITIONED` in the
# tests asserts that every work item lands in exactly one of them.
OWNED = "OWNED"
UNOWNED = "UNOWNED"
IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
STATES = (OWNED, UNOWNED, IDENTITY_UNRESOLVED)

# Reasons, carried alongside the state because "no legitimate human owner"
# covers four materially different situations with four different remedies.
# Collapsing them would make one fix look like it had moved all four.
REASONS = (
    "eligible_human_owner",     # OWNED
    "no_owner_field",           # UNOWNED — the entity type has no owner column
    "no_owner_value",           # UNOWNED — column exists, row does not carry one
    "system_actor",             # UNOWNED — the owner is declared software
    "customer_contact",         # UNOWNED — resolves, but to a customer
    "not_staff",                # UNOWNED — resolves to no staff identity at all
    "identity_collision",       # IDENTITY_UNRESOLVED — names two people
    "unknown_identity_space",   # IDENTITY_UNRESOLVED — names nobody we know
)

# Which entity types can be walked to an owner, and how. Explicit rather than
# reflective: a table acquiring an `owner_id` column must be a decision
# somebody makes here, not a capability the system grows by accident.
#
# CORRECTED 2026-09-02. The first version omitted `invoice` and recorded that
# `invoice.overdue` work was "structurally unownable". That was WRONG, and
# wrong in the way this module warns about everywhere else: it read
# `accounting_invoice_pipeline` — a pipeline view — and concluded the entity
# had no owner. `invoices.owner_id` exists, is 1949/2053 populated, and is one
# of only four owner columns carrying an actual FOREIGN KEY to `owners`
# (accounts, contacts, invoices, opportunities have one; leads, activities,
# orders and cases do not).
#
# The correction does not change the verdict, and that is worth stating: the
# invoice owner resolves, and 1,089 of 1,265 agent-created dunning tasks
# resolve it to a CUSTOMER CONTACT. Ownable and correctly-owned are different
# questions, and only the second one matters here.
_OWNER_SOURCES: Dict[str, Dict[str, str]] = {
    "lead": {"table": "leads", "key": "lead_id", "owner": "owner_id"},
    "invoice": {"table": "invoices", "key": "invoice_id", "owner": "owner_id"},
    "activity": {"table": "activities", "key": "activity_id", "owner": "owner_id"},
}


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
        logger.debug(f"[work_ownership] query failed: {exc}")
        return []
    finally:
        if conn is not None:
            conn.close()


def service_identities() -> Dict[str, str]:
    """uuid -> why, for every staff row DECLARED to be software.

    Delegates to `dsar.staff_personhood()` rather than re-deriving: two
    independent answers to "is this a person" is how they drift apart, and this
    one would drift in the direction of emailing a robot.

    Fail-closed. If personhood cannot be certified (`unclassifiable` non-empty,
    or the call raises), EVERY staff identity is treated as unusable rather
    than as safe — a routing decision made while personhood is unknown is the
    decision this refuses to make.
    """
    try:
        from app.core import dsar
        report = dsar.staff_personhood()
    except Exception as exc:                                   # pragma: no cover
        logger.warning(f"[work_ownership] personhood unavailable: {exc}")
        return {"*": "personhood could not be established"}

    if report.get("unclassifiable"):
        return {"*": f"{len(report['unclassifiable'])} staff row(s) could not "
                     f"be classified as person or service"}
    out: Dict[str, str] = {}
    for svc in report.get("services") or []:
        uid = str(svc.get("employee_uuid") or svc.get("uuid") or "").strip()
        if uid:
            out[uid] = str(svc.get("why") or svc.get("role") or "service identity")
    return out


def is_service_identity(candidate: Optional[str]) -> bool:
    """Is this uuid declared software? `'*'` in the map means personhood itself
    is unknown, in which case the answer is yes for everyone — the fail-closed
    direction, because the cost is a refusal and the other default is mail to a
    robot or, worse, past a check nobody noticed had failed."""
    svc = service_identities()
    if "*" in svc:
        return True
    return str(candidate or "").strip() in svc


def _classify_owner(owner_id: Optional[str], svc: Dict[str, str],
                    facts: Dict[str, Dict[str, bool]]) -> tuple:
    """(state, reason) for one owner value. Deterministic reads only."""
    oid = str(owner_id or "").strip()
    if not oid:
        return UNOWNED, "no_owner_value"

    f = facts.get(oid) or {}

    # ORDER MATTERS. The collision is checked FIRST because a uuid that names
    # two people cannot be safely classified by any later rule — whichever
    # branch it fell into would be answering about one of two people, chosen by
    # the order of the ifs rather than by evidence.
    if f.get("in_employees") and f.get("in_owners"):
        return IDENTITY_UNRESOLVED, "identity_collision"

    if "*" in svc:
        # Personhood unknown -> nothing here can be called a human owner.
        return IDENTITY_UNRESOLVED, "unknown_identity_space"
    if oid in svc:
        return UNOWNED, "system_actor"

    # A customer contact is a real, deterministically resolved person. They are
    # simply not the person accountable for internal work, and the shared
    # primary key says so without consulting a single mutable attribute.
    if f.get("is_contact"):
        return UNOWNED, "customer_contact"

    if f.get("in_employees"):
        return OWNED, "eligible_human_owner"

    if f.get("in_owners"):
        # Resolves in the owner space but names no staff row. Under the decided
        # model (spec §2.2) an owner may legitimately be a contractor or an
        # external consultant — so this is not a defect, and it is also not
        # somebody this system knows how to reach.
        return UNOWNED, "not_staff"

    return IDENTITY_UNRESOLVED, "unknown_identity_space"


def classify(tier: str = "actionable") -> Dict[str, Any]:
    """Every unread work item at `tier`, partitioned into the three states.

    Read-only. Returns counts, reason codes and uuids — never names or
    addresses: "does this have an owner" never requires knowing who they are.
    """
    items = _rows(
        """SELECT n.notification_uuid::text AS id,
                  n.employee_uuid::text     AS holder,
                  m.metadata->>'entity_type' AS entity_type,
                  m.metadata->>'entity_uuid' AS entity_uuid
             FROM notifications n
             JOIN notification_messages m
               ON m.notification_uuid = n.message_uuid
            WHERE n.channel = 'in_app' AND n.status <> 'read'
              AND m.tier = %s""", (tier,))

    # Resolve entity -> owner, one query per entity type rather than per item.
    owner_of: Dict[str, Optional[str]] = {}
    for etype, src in _OWNER_SOURCES.items():
        ids = [i["entity_uuid"] for i in items
               if i["entity_type"] == etype and i["entity_uuid"]]
        if not ids:
            continue
        for row in _rows(
                f"""SELECT {src['key']}::text AS k, {src['owner']}::text AS o
                      FROM {src['table']} WHERE {src['key']} = ANY(%s::uuid[])""",
                (ids,)):
            owner_of[row["k"]] = row["o"]

    candidates = sorted({o for o in owner_of.values() if o})
    facts: Dict[str, Dict[str, bool]] = {}
    if candidates:
        for row in _rows(
                """SELECT c.id::text AS id,
                          EXISTS(SELECT 1 FROM employees e
                                  WHERE e.employee_uuid = c.id) AS in_employees,
                          EXISTS(SELECT 1 FROM owners o
                                  WHERE o.owner_id = c.id)      AS in_owners,
                          EXISTS(SELECT 1 FROM contacts ct
                                  WHERE ct.contact_id = c.id)   AS is_contact,
                          EXISTS(SELECT 1 FROM assignable_identity a
                                  WHERE a.owner_id = c.id AND a.is_active)
                            AS authorized
                     FROM unnest(%s::uuid[]) AS c(id)""", (candidates,)):
            facts[row["id"]] = row

    svc = service_identities()
    by_state: Dict[str, int] = {s: 0 for s in STATES}
    by_reason: Dict[str, int] = {}
    owned_owners, detail = set(), []

    for it in items:
        etype = it["entity_type"]
        if etype not in _OWNER_SOURCES:
            state, reason, owner = UNOWNED, "no_owner_field", None
        else:
            owner = owner_of.get(it["entity_uuid"] or "")
            state, reason = _classify_owner(owner, svc, facts)

        by_state[state] += 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if state == OWNED and owner:
            owned_owners.add(owner)
        detail.append({"entity_type": etype, "owner_id": owner,
                       "state": state, "reason": reason})

    # How many OWNED items could then be evaluated for email? Reported, never
    # acted on — this gate does not decide authorization, it only measures how
    # far the question can currently be taken.
    authorized_owners = sum(
        1 for o in owned_owners if (facts.get(o) or {}).get("authorized"))

    return {
        "tier": tier,
        "items_total": len(items),
        "by_state": by_state,
        "by_reason": by_reason,
        "owned_distinct_owners": len(owned_owners),
        "owned_owners_authorized": authorized_owners,
        "entity_types_with_owner_source": sorted(_OWNER_SOURCES),
        "personhood_certified": "*" not in svc,
        "service_identities": len(svc) if "*" not in svc else None,
        "detail": detail[:25],
        "note": ("state is derived ONLY from deterministic keys and declared "
                 "personhood; no name, email, domain or fuzzy matching"),
    }


# ============================================================================
# E2 — THE ELIGIBILITY PREDICATE
# ============================================================================
# Eligibility is a property OF AN IDENTITY, never an inference from where that
# identity came from or what work it currently holds. Everything below is
# read-side: it answers and refuses, and it assigns, grants and repairs nothing.
#
# WHY MEMBERSHIP ALONE IS NOT THE PREDICATE. The obvious implementation is
# "has an active `assignable_identity` row". That is necessary and NOT
# sufficient, because `grant()` enforces nothing:
#
#     grant(email, owner_id=...) accepts a customer contact, a service account
#     and a colliding uuid alike. It validates that the email contains '@' and
#     that owner_id parses as a uuid. That is all.
#
# So a membership-only predicate would inherit every gap in the primitive that
# creates memberships. The conjuncts below re-check what `grant()` does not.
#
# `uq_assignable_email` is UNIQUE on lower(email); there is NO unique index on
# `owner_id` -- only a partial plain one. Two memberships may therefore name
# one owner, which is why "exactly one identity" is a real check rather than a
# formality.

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

ELIGIBLE = "ELIGIBLE"
IDENTITY_COLLISION = "IDENTITY_COLLISION"
IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
INELIGIBLE_NOT_HUMAN = "INELIGIBLE_NOT_HUMAN"
INELIGIBLE_CUSTOMER_IDENTITY = "INELIGIBLE_CUSTOMER_IDENTITY"
INELIGIBLE_NOT_EXPLICITLY_GRANTED = "INELIGIBLE_NOT_EXPLICITLY_GRANTED"
INELIGIBLE_NOT_ACTIVE = "INELIGIBLE_NOT_ACTIVE"

# PRECEDENCE IS PART OF THE CONTRACT, not an implementation detail. Evaluated
# strictly in this order, first match final -- so every candidate has exactly
# one classification and none can reach ELIGIBLE by falling through a case
# nobody handled.
#
#   1 COLLISION      names two people. Nothing later can be evaluated safely:
#                    each subsequent question would be answered about one of
#                    two people, chosen by the order of the checks.
#   2 UNRESOLVED     names nobody, is not a uuid, or resolves to more than one
#                    membership. "Exactly one identity" fails here.
#   3 NOT_HUMAN      a declared service identity, or personhood uncertifiable.
#   4 CUSTOMER       a customer contact. Ahead of the grant checks ON PURPOSE:
#                    `grant()` cannot exclude them, so if one were ever granted
#                    the answer must still be no, and the reason must still say
#                    "customer" rather than the incidental "not granted".
#   5 NOT_GRANTED    no membership record at all.
#   6 NOT_ACTIVE     a membership record exists and was revoked.
#   7 ELIGIBLE       reachable only by passing all six.
ELIGIBILITY_PRECEDENCE = (
    IDENTITY_COLLISION,
    IDENTITY_UNRESOLVED,
    INELIGIBLE_NOT_HUMAN,
    INELIGIBLE_CUSTOMER_IDENTITY,
    INELIGIBLE_NOT_EXPLICITLY_GRANTED,
    INELIGIBLE_NOT_ACTIVE,
    ELIGIBLE,
)


def _eligibility_facts(candidates: List[str]) -> Dict[str, Dict[str, Any]]:
    """One batched read of every deterministic fact the predicate needs.

    Batched because the predicate runs over populations of hundreds; a
    per-candidate round trip would make the honest implementation the slow one,
    and that is how shortcuts get taken later.
    """
    if not candidates:
        return {}
    rows = _rows(
        """SELECT c.id::text AS id,
                  EXISTS(SELECT 1 FROM employees e
                          WHERE e.employee_uuid = c.id)          AS in_employees,
                  EXISTS(SELECT 1 FROM owners o
                          WHERE o.owner_id = c.id)               AS in_owners,
                  EXISTS(SELECT 1 FROM contacts ct
                          WHERE ct.contact_id = c.id)            AS is_contact,
                  (SELECT count(*) FROM assignable_identity a
                    WHERE a.owner_id = c.id)                     AS grants,
                  (SELECT count(*) FROM assignable_identity a
                    WHERE a.owner_id = c.id AND a.is_active)     AS active_grants
             FROM unnest(%s::uuid[]) AS c(id)""", (candidates,))
    return {r["id"]: r for r in rows}


def eligibility(candidate: Optional[str],
                facts: Optional[Dict[str, Dict[str, Any]]] = None,
                svc: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The E2 predicate. Returns {candidate, state, reason} -- never raises,
    never returns None, and never answers ELIGIBLE by default.

    `facts` and `svc` may be supplied by a batch caller; when omitted they are
    read here, so a single-candidate call is correct on its own.
    """
    raw = str(candidate or "").strip()
    out = {"candidate": raw or None, "state": IDENTITY_UNRESOLVED, "reason": ""}

    def _s(state, reason):
        out["state"], out["reason"] = state, reason
        return out

    if not _UUID_RE.match(raw):
        return _s(IDENTITY_UNRESOLVED, "not a uuid -- no identity to evaluate")

    if svc is None:
        svc = service_identities()
    if facts is None:
        facts = _eligibility_facts([raw])
    f = facts.get(raw) or {}

    # 1. Collision.
    if f.get("in_employees") and f.get("in_owners"):
        # The message names the NEXT LEGITIMATE ACTION without prescribing an
        # answer. The refusal alone is technically correct and operationally a
        # dead end: an admin trying to grant a real person hits it and cannot
        # tell a bug from a governance stop. It must not hint which identity is
        # the right one — that is the F1 decision, and this is not the place it
        # gets made by implication.
        return _s(IDENTITY_COLLISION,
                  "this identifier names more than one identity across "
                  "employees and owners (F1). Authorization cannot be granted "
                  "against it until the identity collision is explicitly "
                  "resolved; resolving it is a governance decision, not a "
                  "choice this check may make")

    # 2. Resolves to nobody, or to more than one membership.
    if not (f.get("in_employees") or f.get("in_owners")):
        return _s(IDENTITY_UNRESOLVED, "resolves in no known identity space")
    if int(f.get("active_grants") or 0) > 1:
        # No unique index on owner_id makes this reachable. Two active
        # memberships for one owner is an ambiguous membership, and picking one
        # would be exactly the heuristic this contract forbids.
        return _s(IDENTITY_UNRESOLVED,
                  f"{f['active_grants']} active memberships name this owner")

    # 3. Personhood -- declared, never inferred from a domain or a name.
    if "*" in svc:
        return _s(INELIGIBLE_NOT_HUMAN,
                  "personhood could not be certified for the roster")
    if raw in svc:
        return _s(INELIGIBLE_NOT_HUMAN, f"declared service identity: {svc[raw]}")

    # 4. Customer identity -- the shared primary key, never the email.
    if f.get("is_contact"):
        return _s(INELIGIBLE_CUSTOMER_IDENTITY,
                  "is a customer contact (shared contact_id); an outsider "
                  "must not be routed work")

    # 5 / 6. Explicit membership, and whether it still stands.
    if int(f.get("grants") or 0) == 0:
        return _s(INELIGIBLE_NOT_EXPLICITLY_GRANTED,
                  "no membership record -- eligibility is an explicit grant, "
                  "never inferred from being human, employed or busy")
    if int(f.get("active_grants") or 0) == 0:
        return _s(INELIGIBLE_NOT_ACTIVE, "membership exists but was revoked")

    return _s(ELIGIBLE, "declared person, one identity, active explicit grant")


def is_eligible_owner(candidate: Optional[str]) -> bool:
    """The boolean E1 will eventually call. Deliberately thin: every judgement
    lives in `eligibility()`, so a caller can never get a different answer than
    the report gives."""
    return eligibility(candidate)["state"] == ELIGIBLE


def owner_population() -> Dict[str, Any]:
    """Classify every row in `owners` through the predicate. Counts and states
    only -- no names, no addresses."""
    ids = [r["owner_id"] for r in
           _rows("SELECT owner_id::text FROM owners ORDER BY owner_id")]
    svc, facts = service_identities(), _eligibility_facts(ids)
    by_state: Dict[str, int] = {}
    for oid in ids:
        st = eligibility(oid, facts, svc)["state"]
        by_state[st] = by_state.get(st, 0) + 1
    # CONDITION 2. `eligible` says the mechanism works; `eligible_production`
    # says the owner is a real party. After a synthetic grant the first rises
    # and the second must not.
    return {"owners_total": len(ids), "by_state": by_state,
            **_split_production(ids, svc, facts)}


def activity_ownership(status: Optional[str] = None) -> Dict[str, Any]:
    """Classify `activities.owner_id` through the predicate.

    NULL owners are reported separately from ineligible ones: "nobody is
    accountable yet" and "the wrong party is recorded as accountable" are
    different problems with different remedies, and one 'bad' bucket would hide
    which of them is growing.
    """
    where = "WHERE status = %s" if status else ""
    args = (status,) if status else ()
    rows = _rows(f"""SELECT owner_id::text AS owner_id, count(*) AS n
                       FROM activities {where}
                      GROUP BY 1""", args)
    unowned = sum(int(r["n"]) for r in rows if not r["owner_id"])
    ids = [r["owner_id"] for r in rows if r["owner_id"]]
    svc, facts = service_identities(), _eligibility_facts(ids)

    by_state: Dict[str, int] = {}
    for r in rows:
        if not r["owner_id"]:
            continue
        st = eligibility(r["owner_id"], facts, svc)["state"]
        by_state[st] = by_state.get(st, 0) + int(r["n"])

    total = unowned + sum(by_state.values())
    return {"status": status or "all", "activities_total": total,
            "unowned": unowned, "by_state": by_state,
            "eligible": by_state.get(ELIGIBLE, 0),
            "distinct_owners": len(ids)}


# ============================================================================
# E1 — SHADOW OBSERVATION AT THE HANDLER BOUNDARY
# ============================================================================
# The handlers copy the entity's owner onto the accountable activity without
# evaluating it. That is the mechanism still producing new customer-owned work.
#
# P3 RATIFIED 2026-09-02: the transition is UNASSIGNED. An ineligible candidate
# means the activity is still created, with `owner_id` left NULL and the
# refusal recorded. The work is never dropped and never reassigned.
#
#     ELIGIBLE      -> write the candidate
#     anything else -> write NULL. Never substitute the account owner, never
#                      substitute the notification holder, never derive one.
#
# SHIPPED DEFAULT OFF. `OWNER_ELIGIBILITY_ENFORCE` gates the transition and is
# 0 unless set, so this function returns the candidate unchanged until somebody
# deliberately flips it. Ratifying the decision and enabling it are two acts,
# and the brief's rollout posture is a shadow window before the flip.
#
# WHY SHADOW IS STILL WORTH RUNNING. The predicate can classify the stored
# population offline, so the counts are not what shadow adds. It adds the live
# rate and, more importantly, the chance to meet a candidate shape the stored
# data does not contain -- a handler path supplying something the offline
# classification never saw. That is not observable from a code read.
#
# NAMED FOR WHAT IT DOES. This was `observe_candidate_owner` while it could
# only watch. It can now change what is written, and a function that alters a
# write while calling itself an observation is the same defect as a constraint
# named for a guarantee it does not provide.

_SHADOW: Dict[str, Any] = {"seen": 0, "ineligible": 0, "by_state": {},
                           "by_handler": {}, "since": None}


def enforcing() -> bool:
    """Read at call time, not import: the flag is flipped per environment and a
    module-level constant would freeze whatever was set when the worker booted."""
    return (os.getenv("OWNER_ELIGIBILITY_ENFORCE", "0").strip().lower()
            in ("1", "true", "yes", "on"))


def owner_for_write(candidate: Optional[str], *,
                    handler: str,
                    entity_type: Optional[str] = None,
                    entity_id: Optional[str] = None) -> Optional[str]:
    """What a handler should write as the accountable owner.

    Returns the candidate when the contract certifies it. Returns None -- the
    ratified UNASSIGNED transition -- when it does not AND enforcement is on.
    Returns the candidate unchanged when enforcement is off, which is the
    default.

    Never raises and never blocks a write: a handler that failed because its
    eligibility check failed would be a worse defect than the one being fixed,
    and it would drop real work to protect a bookkeeping property.
    """
    import datetime as _dt
    if _SHADOW["since"] is None:
        _SHADOW["since"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        state = eligibility(candidate)["state"]
        if state == ELIGIBLE:
            # CONDITION 3. An eligible owner may still be the wrong owner for
            # THIS work: a synthetic identity must never be recorded as
            # accountable for attested-real work. Checked only for candidates
            # that would otherwise be written, so it costs nothing on the
            # overwhelming majority that are already refused.
            refusal = synthetic_owner_refusal(candidate, entity_type, entity_id)
            if refusal:
                state = refusal
    except Exception as exc:                                   # pragma: no cover
        logger.warning(f"[work_ownership] shadow observation failed "
                       f"({handler}): {exc}")
        return candidate

    _SHADOW["seen"] += 1
    _SHADOW["by_state"][state] = _SHADOW["by_state"].get(state, 0) + 1
    if state != ELIGIBLE:
        _SHADOW["ineligible"] += 1
        h = _SHADOW["by_handler"].setdefault(handler, {})
        h[state] = h.get(state, 0) + 1
        # INFO, not WARNING. Today this fires on the overwhelming majority of
        # Tier-2 activity creations -- it is the known defect, not news -- and
        # a warning per occurrence would bury the log it is meant to inform.
        if enforcing():
            # THE RATIFIED TRANSITION. The activity is still created and still
            # due; what changes is that the system stops asserting a name
            # against it. WARNING because a refusal that reaches a write is the
            # last trace of which candidate was dropped -- the activity row
            # will not carry it, and no durable record of the refused value
            # exists at this layer. That is a known limit, not an oversight:
            # giving it one is a schema obligation and belongs with the
            # remediation work, not with a flag-gated handler change.
            logger.warning(f"[work_ownership] UNASSIGNED: {handler} refused "
                           f"owner {str(candidate)[:8]}… — {state}; activity "
                           f"created with no accountable owner")
            _SHADOW["enforced"] = _SHADOW.get("enforced", 0) + 1
            return None
        logger.info(f"[work_ownership] shadow: {handler} would refuse "
                    f"owner {str(candidate)[:8]}… — {state}")
    return candidate


def shadow_report() -> Dict[str, Any]:
    """What the shadow window has seen since process start.

    In-memory and per-process on purpose: this is a pre-enforcement
    measurement, not an audit record, and giving it a table would create a
    schema obligation for something meant to be read once and removed.
    """
    seen = _SHADOW["seen"]
    return {
        "since": _SHADOW["since"],
        "seen": seen,
        "ineligible": _SHADOW["ineligible"],
        "ineligible_rate": round(_SHADOW["ineligible"] / seen, 4) if seen else None,
        "by_state": dict(_SHADOW["by_state"]),
        "by_handler": {k: dict(v) for k, v in _SHADOW["by_handler"].items()},
        "enforced": _SHADOW.get("enforced", 0),
        "enforcing": enforcing(),
        "transition": "UNASSIGNED (owner_id = NULL) — P3, ratified 2026-09-02",
        "flag": "OWNER_ELIGIBILITY_ENFORCE",
    }


# ============================================================================
# THE SURFACING HALF OF THE RATIFIED P3 TRANSITION
# ============================================================================
# P3 is "create the work, leave owner_id NULL, AND SURFACE IT". The first two
# clauses shipped with `owner_for_write`; this is the third, and enforcement
# may not be enabled until it exists.
#
# THE SAFETY PROPERTY IT EXISTS FOR:
#
#     Changing an ineligible owner to NULL must never turn WRONGLY ATTRIBUTED
#     work into INVISIBLE work.
#
# That risk is real and was measured, not assumed. `sp_activities` filters as
# `(p_owner_id IS NULL OR a.owner_id = p_owner_id)`, so a NULL-owned row is
# returned by the unfiltered list and correctly excluded from every per-owner
# view; the by-owner rollup excludes it explicitly with `owner_id IS NOT NULL`.
# Those semantics are right and are NOT changed here -- unassigned work must
# not appear in someone's personal bucket. What was missing is a surface where
# it appears as itself.
#
# WHY THE REASON IS DERIVED RATHER THAN READ. `activities` has no metadata
# column, so a refusal reason has nowhere to be stored on the row -- the limit
# named in the P3 decision record. Instead the reason is derived at read time
# from the activity's ENTITY: the lead or invoice it was raised for still
# carries the owner the handler would have copied.
#
# THE LIMITATION THAT FOLLOWS, stated rather than hidden: this reports why the
# row WOULD be unassigned now, not why it was unassigned then. An entity owner
# can change. For the operational purpose -- someone has to assign this -- the
# current state is the one that matters, and for the audit purpose it is not
# sufficient. A durable reason is a schema obligation and belongs with
# remediation.

# Exactly four codes. The finer eligibility state travels in a detail field
# rather than as more codes, so two names never mean one thing.
UNASSIGNED_OWNER_INELIGIBLE = "UNASSIGNED_OWNER_INELIGIBLE"
UNASSIGNED_OWNER_UNRESOLVED = "UNASSIGNED_OWNER_UNRESOLVED"
UNASSIGNED_OWNER_COLLISION = "UNASSIGNED_OWNER_COLLISION"
UNASSIGNED_NO_OWNER_RECORDED = "UNASSIGNED_NO_OWNER_RECORDED"

UNASSIGNED_REASONS = (UNASSIGNED_OWNER_INELIGIBLE, UNASSIGNED_OWNER_UNRESOLVED,
                      UNASSIGNED_OWNER_COLLISION, UNASSIGNED_NO_OWNER_RECORDED)

# eligibility state -> surfacing reason. INELIGIBLE collapses the three
# not-eligible-but-resolved states on purpose: to an assigner they are one
# situation ("this owner may not hold work"), and the state itself is still
# reported alongside for anyone who needs the distinction.
_REASON_OF = {
    IDENTITY_COLLISION: UNASSIGNED_OWNER_COLLISION,
    IDENTITY_UNRESOLVED: UNASSIGNED_OWNER_UNRESOLVED,
    INELIGIBLE_NOT_HUMAN: UNASSIGNED_OWNER_INELIGIBLE,
    INELIGIBLE_CUSTOMER_IDENTITY: UNASSIGNED_OWNER_INELIGIBLE,
    INELIGIBLE_NOT_EXPLICITLY_GRANTED: UNASSIGNED_OWNER_INELIGIBLE,
    INELIGIBLE_NOT_ACTIVE: UNASSIGNED_OWNER_INELIGIBLE,
}

# Live means OPEN. Section 7 of the gate is explicit: ~1,851 activities are
# already NULL-owned and only ~5 are open. Surfacing the closed ones would
# turn a decade of history into a work queue overnight, which is the opposite
# of making live work visible.
_LIVE_STATUSES = ("open",)


def unassigned_work(limit: int = 200) -> Dict[str, Any]:
    """Open activities with no accountable owner, and why.

    Read-only. Returns counts, reason codes and identifiers -- never names or
    addresses; deciding that something needs assigning does not require
    knowing who the customer is.
    """
    rows = _rows(
        """SELECT a.activity_id::text AS id, a.type, a.subject, a.status,
                  a.due_at, a.related_type,
                  (a.due_at IS NOT NULL AND a.due_at < now()) AS overdue,
                  CASE WHEN a.lead_id IS NOT NULL THEN 'lead'
                       WHEN a.related_type = 'invoice' THEN 'invoice'
                       ELSE a.related_type END AS entity_type,
                  COALESCE(l.owner_id, i.owner_id)::text AS entity_owner
             FROM activities a
             LEFT JOIN leads    l ON l.lead_id    = a.lead_id
             LEFT JOIN invoices i ON a.related_type = 'invoice'
                                 AND i.invoice_id = a.related_id
            WHERE a.owner_id IS NULL
              AND a.status = ANY(%s)
            ORDER BY a.due_at NULLS LAST
            LIMIT %s""", (list(_LIVE_STATUSES), int(limit)))

    candidates = sorted({r["entity_owner"] for r in rows if r["entity_owner"]})
    svc = service_identities()
    facts = _eligibility_facts(candidates)

    items, by_reason, by_state = [], {}, {}
    assignable_now = 0
    for r in rows:
        owner = r["entity_owner"]
        if not owner:
            reason, state = UNASSIGNED_NO_OWNER_RECORDED, None
        else:
            state = eligibility(owner, facts, svc)["state"]
            # An entity owner who IS eligible means the reason this row is
            # unassigned is not the owner -- somebody simply has not assigned
            # it. Reported as "no owner recorded" with the state alongside,
            # rather than as a fifth code meaning almost the same as another.
            reason = _REASON_OF.get(state, UNASSIGNED_NO_OWNER_RECORDED)
            if state == ELIGIBLE:
                assignable_now += 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if state:
            by_state[state] = by_state.get(state, 0) + 1
        items.append({
            "activity_id": r["id"], "type": r["type"], "status": r["status"],
            "entity_type": r["entity_type"], "entity_owner": owner,
            "due_at": r["due_at"], "overdue": bool(r["overdue"]),
            "reason": reason, "entity_owner_state": state,
            # Tier 2 is the population the ownership requirement governs; the
            # two live Tier-2 event types raise work against these entities.
            "tier2": r["entity_type"] in ("lead", "invoice"),
            "needs_assignment": True,
        })

    return {
        "surface": "unassigned",
        "statuses": list(_LIVE_STATUSES),
        "count": len(items),
        "overdue": sum(1 for i in items if i["overdue"]),
        "tier2": sum(1 for i in items if i["tier2"]),
        "assignable_now": assignable_now,
        "by_reason": by_reason,
        "by_entity_owner_state": by_state,
        "items": items[:limit],
        "reason_is": ("derived from the entity's CURRENT owner — why this row "
                      "would be unassigned now, not why it was. `activities` "
                      "has no column to record the refused candidate."),
    }


def owner_eligibility_readiness() -> Dict[str, Any]:
    """The readiness dimension: of open activities, how many are owned by
    someone the contract certifies.

    Counts UNOWNED as neither good nor bad in the ratio -- an unassigned row is
    the CORRECT outcome of the ratified transition, and scoring it as a defect
    would make the readiness report fall as enforcement did its job. It is
    reported separately, because it still needs a human.
    """
    rows = _rows(
        """SELECT owner_id::text AS owner_id, count(*) AS n
             FROM activities WHERE status = ANY(%s) GROUP BY 1""",
        (list(_LIVE_STATUSES),))
    unowned = sum(int(r["n"]) for r in rows if not r["owner_id"])
    ids = [r["owner_id"] for r in rows if r["owner_id"]]
    svc, facts = service_identities(), _eligibility_facts(ids)

    # The five states the gate requires, kept distinct.
    states = {"ELIGIBLE": 0, "INELIGIBLE": 0, "UNRESOLVED": 0,
              "COLLISION": 0, "UNOWNED": unowned}
    for r in rows:
        if not r["owner_id"]:
            continue
        st = eligibility(r["owner_id"], facts, svc)["state"]
        n = int(r["n"])
        if st == ELIGIBLE:
            states["ELIGIBLE"] += n
        elif st == IDENTITY_COLLISION:
            states["COLLISION"] += n
        elif st == IDENTITY_UNRESOLVED:
            states["UNRESOLVED"] += n
        else:
            # A resolved owner who may not hold work. A customer contact lands
            # here and NEVER in ELIGIBLE, however cleanly its foreign key
            # resolves.
            states["INELIGIBLE"] += n

    owned = sum(v for k, v in states.items() if k != "UNOWNED")
    # Activity counts weighted by rows, so the production split is computed
    # over rows too — an owner holding 200 items must not count once.
    prov = owner_provenance(ids)
    production_rows = sum(
        int(r["n"]) for r in rows
        if r["owner_id"]
        and eligibility(r["owner_id"], facts, svc)["state"] == ELIGIBLE
        and prov.get(r["owner_id"]) == PROV_REAL)
    return {"states": states, "owned": owned, "unowned": unowned,
            "good": states["ELIGIBLE"], "total": owned,
            "eligible_production": production_rows}


def owner_eligibility_check() -> Tuple[Optional[int], Optional[int]]:
    """(good, total) for `data_readiness`. Never raises: a readiness report
    that crashed would take every other dimension down with it."""
    try:
        r = owner_eligibility_readiness()
        return r["good"], r["total"]
    except Exception as exc:                                   # pragma: no cover
        logger.warning(f"[work_ownership] eligibility readiness failed: {exc}")
        return None, None


# ============================================================================
# CONDITION 2 — A GRANT CARRIES ITS PROVENANCE
# ============================================================================
# Eight demo personas are about to become eligible. The eligible-owner count
# must not move as a result, or the readiness report starts calling seed data
# accountability.
#
# NO NEW SCHEMA, AND THAT IS DELIBERATE. `corpus_provenance_entity_chk` already
# admits entity_type='owners'; zero owner rows are classified today. A minted
# owner is therefore classified in the SAME governed table, by the same rule
# vocabulary, as the eight employee attestations already recorded. A second
# provenance concept would be a second thing to drift.
#
# UNKNOWN IS NOT REAL. Fail-closed, and it has a consequence worth stating
# rather than discovering in a review: the four executives are eligible and
# UNCLASSIFIED, so `eligible_production` reads 0 today — not 4. That is the
# honest number. Nothing in this corpus attests that any owner is a real
# person, and the count rises when somebody attests one.

PROV_REAL = "real"
PROV_SYNTHETIC = "synthetic"
PROV_AMBIGUOUS = "ambiguous"
PROV_UNKNOWN = "unknown"          # no row — never counted as real


def owner_provenance(owner_ids: List[str]) -> Dict[str, str]:
    """uuid -> provenance state, for owners. Absent rows read PROV_UNKNOWN.

    THE SHARED PRIMITIVE. Condition 3's guard reads the same map, so "what is
    this owner's provenance" is answered once. Two answers to that question is
    how they drift, and the direction of drift here is a synthetic identity
    quietly counting as real.
    """
    ids = [i for i in (owner_ids or []) if i]
    if not ids:
        return {}
    rows = _rows(
        """SELECT entity_id::text AS id, state
             FROM corpus_provenance
            WHERE entity_type = 'owners' AND entity_id = ANY(%s::text[])""",
        (ids,))
    found = {r["id"]: r["state"] for r in rows}
    return {i: found.get(i, PROV_UNKNOWN) for i in ids}


def is_production_accountable(owner_id: Optional[str]) -> bool:
    """Eligible AND attested real. Both halves required.

    Eligibility proves the MECHANISM works; provenance proves the owner is a
    real party. After a synthetic grant the first rises and the second must
    not, which is the entire point of Condition 2.
    """
    oid = str(owner_id or "").strip()
    if not oid or not is_eligible_owner(oid):
        return False
    return owner_provenance([oid]).get(oid) == PROV_REAL


def _split_production(ids: List[str], svc, facts) -> Dict[str, int]:
    """Counts of ELIGIBLE owners, split by whether provenance attests real."""
    prov = owner_provenance(ids)
    eligible = [i for i in ids if eligibility(i, facts, svc)["state"] == ELIGIBLE]
    by_prov: Dict[str, int] = {}
    for i in eligible:
        p = prov.get(i, PROV_UNKNOWN)
        by_prov[p] = by_prov.get(p, 0) + 1
    return {
        "eligible": len(eligible),
        "eligible_production": by_prov.get(PROV_REAL, 0),
        "eligible_by_provenance": by_prov,
    }


# ============================================================================
# CONDITION 3 — REAL WORK MUST NEVER LAND ON A SYNTHETIC OWNER
# ============================================================================
# Synthetic owner on synthetic work is a demo corpus behaving consistently.
# Synthetic owner on REAL work is a corrupted accountability record, and it is
# the only outcome P5 was scoped to prevent.
#
# THE GUARD KEYS ON ATTESTED-REAL, NOT ON "NOT ATTESTED SYNTHETIC", and the
# trade-off is deliberate. Work has no provenance of its own, so realness is
# inherited from its subject entity, and almost every entity is unclassified.
# Treating unknown as real would refuse every synthetic owner on every item and
# the P5 test path would never run; treating it as not-real means the guard
# bites only on attested-real work. That is a ratchet: it protects nothing
# today because nothing is declared real, and protects real data from the
# instant real data is declared — by the same attestation act that recorded the
# eight employees.
#
# INVOICES CANNOT BE ATTESTED. `corpus_provenance_entity_chk` admits contacts,
# leads, accounts, customers, owners, employees and executives — not invoices.
# So invoice work inherits provenance from the ACCOUNT or CONTACT it belongs
# to, which are classifiable. Recorded here because it is a limit of the
# vocabulary, not an oversight in the guard.

SYNTHETIC_OWNER_ON_REAL_WORK = "synthetic_owner_on_real_work"


def _subject_of(entity_type: Optional[str], entity_id: Optional[str]) -> Tuple:
    """Map a work item to the classifiable entity its realness comes from.

    Returns (provenance_entity_type, entity_id) or (None, None) when the work
    has no subject the provenance vocabulary can describe.
    """
    et = (entity_type or "").strip().lower()
    eid = str(entity_id or "").strip()
    if not eid:
        return None, None
    if et == "lead":
        return "leads", eid
    if et in ("account", "accounts"):
        return "accounts", eid
    if et in ("contact", "contacts"):
        return "contacts", eid
    if et == "invoice":
        # Invoices are outside the vocabulary; inherit from the account, else
        # the contact. Never invent a classification for the invoice itself.
        for row in _rows("""SELECT account_id::text AS a, contact_id::text AS c
                              FROM invoices WHERE invoice_id = %s::uuid""", (eid,)):
            if row["a"]:
                return "accounts", row["a"]
            if row["c"]:
                return "contacts", row["c"]
    return None, None


def work_is_attested_real(entity_type: Optional[str],
                          entity_id: Optional[str]) -> bool:
    """True only when the work's subject is ATTESTED real. Unknown is not real
    — see the ratchet note above."""
    et, eid = _subject_of(entity_type, entity_id)
    if not et:
        return False
    rows = _rows("""SELECT state FROM corpus_provenance
                     WHERE entity_type = %s AND entity_id = %s""", (et, eid))
    return bool(rows) and rows[0]["state"] == PROV_REAL


def synthetic_owner_refusal(owner_id: Optional[str],
                            entity_type: Optional[str] = None,
                            entity_id: Optional[str] = None) -> Optional[str]:
    """The Condition 3 check. Returns a reason to refuse, or None.

    Ordered cheapest-first: a candidate that is not synthetic can never trip
    this, and most candidates are not, so the entity lookup is skipped for
    them entirely.
    """
    oid = str(owner_id or "").strip()
    if not oid:
        return None
    if owner_provenance([oid]).get(oid) != PROV_SYNTHETIC:
        return None
    if not work_is_attested_real(entity_type, entity_id):
        return None
    return SYNTHETIC_OWNER_ON_REAL_WORK


# ============================================================================
# DIGEST RECIPIENT RESOLUTION
# ============================================================================
# `digest_items()` receives an OWNER identifier and matches it against
# `notifications.employee_uuid`, a column of EMPLOYEE identifiers. Nothing
# joins the two spaces; the comparison assumes they are one.
#
# It works today because the only four callers satisfy the assumption by
# accident: the executives' owner ids sit in that column because somebody once
# created event subscriptions keyed to them. Mint a fresh owner uuid — which a
# correct grant must — and the lookup matches nothing. The grant succeeds, the
# digest is empty, and nothing reports a fault. That is the same shape as the
# defect this whole programme began with.
#
# THE COLUMN IS THREE SPACES, NOT ONE. It is filled from
# `event_subscriptions.employee_uuid`, whose live values are employee
# identities, agent identities and the executives' owner identities. So the
# resolution states which space it is asking for rather than absorbing the
# ambiguity.

RECIPIENT_SPACE_EMPLOYEE = "employee"
RECIPIENT_SPACE_OWNER = "owner"


def recipient_identities(owner_id: Optional[str]) -> List[Dict[str, Any]]:
    """The subscriber identities that represent this owner, with evidence.

    Every member is a RECORDED FACT, never an inference:

      employee  the governed link `owners.employee_uuid` is populated
      owner     a subscription keyed to the owner id demonstrably exists

    The second is a bounded LEGACY exception, not a fallback. A subscription
    either exists for that identifier or it does not, and the answer is read.
    A grant issued from now on must key its subscription to the EMPLOYEE
    identity, or every grant widens the polymorphism this exists to contain.

    NEVER resolves by name, email, role or domain. NEVER returns an agent
    identity: subscribing and receiving a worklist are different things, and
    the column cannot tell them apart.
    """
    oid = str(owner_id or "").strip()
    if not _UUID_RE.match(oid):
        return []

    out: List[Dict[str, Any]] = []
    svc = service_identities()

    for row in _rows("""SELECT employee_uuid::text AS e FROM owners
                         WHERE owner_id = %s::uuid AND employee_uuid IS NOT NULL""",
                     (oid,)):
        if row["e"] and row["e"] not in svc:
            out.append({"identity": row["e"], "space": RECIPIENT_SPACE_EMPLOYEE,
                        "evidence": "owners.employee_uuid"})

    # THE LEGACY BRANCH, and the evidence for it was got wrong first time.
    # The gate brief said an `event_subscriptions` row justified it. Measured:
    # the CEO owner id has ZERO subscriptions and FOURTEEN recipient rows —
    # the executives are addressed DIRECTLY by the approval path, not through
    # the fan-out, so no subscription was ever created for them.
    #
    # The typed, deterministic fact is the `executives` row itself: that table
    # keys its members by a value living in the OWNER space (the E7 finding),
    # and the approval path uses it as the recipient key. So an active
    # executive resolves to their own owner id, on the evidence of a row that
    # says so — not on the coincidence of having received something.
    if oid not in svc and _rows(
            """SELECT 1 FROM executives
                WHERE employee_uuid = %s::uuid AND is_active LIMIT 1""", (oid,)):
        out.append({"identity": oid, "space": RECIPIENT_SPACE_OWNER,
                    "evidence": "executives.employee_uuid (legacy owner-space key)"})

    return out


def recipient_keys(owner_id: Optional[str]) -> List[str]:
    """Just the identifiers, for the digest's IN (...) lookup. Empty is a valid
    answer and means the owner has no resolvable recipient identity — which is
    a DIFFERENT state from having no work, and must stay distinguishable."""
    return [r["identity"] for r in recipient_identities(owner_id)]


# ============================================================================
# GRANT ISSUANCE — the composite operation
# ============================================================================
# Four writes that must happen together or not at all:
#
#   1. mint an owner row      fresh uuid, NEVER the employee's
#   2. link it                owners.employee_uuid -> the employee
#   3. classify it            corpus_provenance, inheriting the employee's state
#   4. grant membership       assignable_identity, via the governed primitive
#
# DRY RUN BY DEFAULT. A function that widens who may receive work should not do
# so because somebody forgot an argument. `apply=False` reports exactly what it
# would write and touches nothing.
#
# PROVENANCE IS A PREREQUISITE, NOT AN OUTPUT. An identity whose provenance is
# unrecorded cannot be granted at all. That makes Condition 2 structurally
# required rather than merely recommended: you cannot create an eligible owner
# whose realness nobody has stated, so the eligible population can never
# contain an identity of unknown status.
#
# NO SUBSCRIPTION IS CREATED. These eight already subscribe under their
# employee identity, which is the identity the resolution reads. A grant for
# somebody who does NOT already subscribe must create one keyed to the EMPLOYEE
# identity — never the owner id, or every grant widens the polymorphism the
# resolution exists to contain.

GRANT_REFUSALS = ("not_an_employee", "service_identity", "provenance_unrecorded",
                  "already_linked", "identity_collision")


def grant_employee_owner(employee_uuid: str, *, added_by: str,
                         apply: bool = False) -> Dict[str, Any]:
    """Mint an owner for an employee and grant it work eligibility.

    Returns a plan when `apply` is False, and the same shape plus the written
    identifiers when True. Refuses rather than raises; every refusal names
    itself.
    """
    eid = str(employee_uuid or "").strip()
    out: Dict[str, Any] = {"employee_uuid": eid, "applied": False,
                           "refused": None, "reason": ""}

    def _no(code: str, why: str) -> Dict[str, Any]:
        out["refused"], out["reason"] = code, why
        return out

    if not _UUID_RE.match(eid):
        return _no("not_an_employee", "not a uuid")

    rows = _rows("""SELECT employee_name, email FROM employees
                     WHERE employee_uuid = %s::uuid""", (eid,))
    if not rows:
        return _no("not_an_employee", "no employee row with that identifier")
    name, email = rows[0]["employee_name"], rows[0]["email"]
    out["employee"], out["email"] = name, email

    if is_service_identity(eid):
        return _no("service_identity",
                   "declared software; a service identity is never accountable")

    # The collision, refused early and by name rather than left to surface as a
    # confusing failure three writes later.
    if _rows("SELECT 1 FROM owners WHERE owner_id = %s::uuid", (eid,)):
        return _no("identity_collision",
                   "this employee identifier is also an owner id (F1); "
                   "granting it would authorise an identifier naming two "
                   "people. Resolving F1 is a governance decision")

    prov = _rows("""SELECT state FROM corpus_provenance
                     WHERE entity_type='employees' AND entity_id=%s""", (eid,))
    if not prov:
        return _no("provenance_unrecorded",
                   "no provenance recorded for this employee — Condition 2 "
                   "requires that an owner's realness be stated before it can "
                   "become eligible")
    out["provenance"] = prov[0]["state"]

    if _rows("""SELECT 1 FROM owners WHERE employee_uuid = %s::uuid""", (eid,)):
        return _no("already_linked", "an owner row already links this employee")

    out["plan"] = {
        "mint_owner": "fresh uuid (never the employee uuid)",
        "link": "owners.employee_uuid -> this employee",
        "classify_owner_as": prov[0]["state"],
        "grant_membership_for": email,
        "auto_email_enabled": False,
        "creates_subscription": False,
    }
    if not apply:
        return out

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO owners (owner_id, first_name, last_name, email,
                                       role, is_active, employee_uuid,
                                       created_at, updated_at)
                   VALUES (gen_random_uuid(), %s, %s, %s, %s, true, %s::uuid,
                           now(), now())
                   RETURNING owner_id::text""",
                (name, "(employee)", email, "employee", eid))
            owner_id = cur.fetchone()[0]

            cur.execute(
                """INSERT INTO corpus_provenance
                     (entity_type, entity_id, state, rule, evidence,
                      decided_at, decided_by)
                   VALUES ('owners', %s, %s, 'human_attested',
                           jsonb_build_object('inherited_from_employee', %s,
                                              'gate', 'grant-issuance'),
                           now(), %s)
                   ON CONFLICT (entity_type, entity_id) DO NOTHING""",
                (owner_id, prov[0]["state"], eid, added_by))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return _no("write_failed", str(exc)[:200])
    finally:
        if not conn.closed:
            conn.close()

    from app.core import assignable
    g = assignable.grant(email, owner_id=owner_id, display_name=name,
                         source="employee", source_ref=eid, added_by=added_by)
    out["owner_id"] = owner_id
    out["grant"] = g
    out["applied"] = bool(g.get("ok"))
    if not g.get("ok"):
        out["reason"] = f"owner minted but membership refused: {g.get('error')}"
    return out
