"""Case lifecycle — C1 Step 2 (Axis 5: Record Integrity & Business Continuity).

THE RULE THIS MODULE ENFORCES:
    "An escalation is an EVENT. A case is the durable unit of WORK that event
     creates."

`cases` (120 rows) and `case_comments` (480) existed since 2026-01-09 with a
five-state lifecycle and an owner column, while the whole service capability —
knowledge retrieval, escalations with SLA clocks, the takeover console, CSAT
proxy, containment metrics — was built AROUND them. This module is the write
layer that makes that record durable, owned and provable.

WHAT THIS MODULE IS
  * the AUTHORITATIVE transition boundary. There is no public raw-UPDATE path,
    so a caller cannot move a case sideways through the lifecycle.
  * the NARROW field-history writer: exactly `status`, `owner_id`, `priority`,
    written by code. Deliberately NOT a log-every-column trigger — a blanket
    trigger is how history tables become unreadable and therefore unused.

WHAT THIS MODULE IS NOT (Step 2 boundary — do not add these here)
  * no escalation bridge, console bridge, agent package, UI, analytics or
    knowledge feedback. Those are steps 3-9.
  * no SLA fields, no pause/resume, no deadline ownership. Open design
    questions 1 and 3 stay open; a case reads its escalation's deadline.
  * comments stay CASE-LOCAL. Open design question 2 stays open.

INERTNESS: nothing imports this module yet. Installing it changes no runtime
behaviour; escalations, the console and every existing path are untouched.

CONFIG (env)
  CASES_ENABLED      1   master switch for the write layer
  CASES_AUTO_OPEN    0   automatic case creation from escalation/takeover.
                         READ but not acted on here — step 3 owns the bridge.
  CASES_KB_FEEDBACK  0   resolved case -> knowledge draft. Step 8.

Requires sql/case_lifecycle.sql.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.database import get_connection

logger = logging.getLogger("cases")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("CASES_ENABLED", "1")
# Read here so the flag contract is visible in one place. Step 2 never acts on
# it: no call site in this module opens a case automatically.
AUTO_OPEN = _flag("CASES_AUTO_OPEN", "0")
KB_FEEDBACK = _flag("CASES_KB_FEEDBACK", "0")

# ============================================================================
# THE STATE MACHINE
# ============================================================================
# Ratified lifecycle (user, 2026-07-26 — direct resolution approved):
#
#     new -> in_progress -> resolved -> closed
#                  |            ^
#                  v            |
#               waiting --------+
#
#     resolved -> in_progress   (REOPEN - counted, never silent)
#
# `waiting` means BLOCKED pending an external response or condition. It is NOT
# a mandatory stop on the way to resolution — forcing every case through it
# would make the state meaningless and every "waiting" count a lie about how
# much work is actually blocked.
#
# ONE DELIBERATE OMISSION remains, awaiting a deliberate design rather than a
# guess: `closed` is TERMINAL. D3 defines reopening a closed case as creating a
# NEW case linked to the prior one, which needs a durable relationship the
# schema does not yet model. reopen() refuses it and names the gap. Note the
# two are different lifecycle concepts and must not be conflated:
#
#     resolved -> in_progress   the SAME work came back
#     closed   -> new case      NEW work that references old work
STATUSES = ("new", "in_progress", "waiting", "resolved", "closed")

TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "new":         ("in_progress",),
    "in_progress": ("waiting", "resolved"),
    "waiting":     ("in_progress", "resolved"),
    "resolved":    ("closed", "in_progress"),      # in_progress = reopen
    "closed":      (),                             # terminal
}

# The one transition that means "this work came back".
REOPEN_FROM, REOPEN_TO = "resolved", "in_progress"

PRIORITIES = ("low", "medium", "high", "urgent")

# The ONLY fields this writer records. Widening this set is a design decision,
# not a convenience: history is valuable in proportion to how little noise it
# carries.
TRACKED_FIELDS = ("status", "owner_id", "priority")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class CaseError(Exception):
    """Deterministic refusal from the write layer."""


class InvalidTransition(CaseError):
    pass


# ============================================================================
# OWNERSHIP — a case owner must be a real, auditable CRM identity
# ============================================================================

def resolve_owner(text: Optional[str]) -> Optional[str]:
    """Best-effort map an arbitrary assignee STRING to an `owners.owner_id`.

    Reuses the identity path `executive_intelligence.link_employees()` already
    established: match by EMAIL against `owners`.

    Two deliberate differences from link_employees:
      * it NEVER creates an owner. That function onboards a known executive; an
        escalation's `assigned_to` is a free-form UI string (console_takeover
        defaults it to the literal "agent"), and minting CRM identities from
        arbitrary strings is exactly the failure this boundary exists to stop.
      * an unresolved value returns None rather than a placeholder, so callers
        must decide explicitly what an unidentifiable assignee means. That
        decision belongs to the Step 3 escalation bridge.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # C2.0: when an assignable-identity directory is configured AND strict mode
    # is on, membership is the authority — being in `owners` stops implying
    # "works here", which C2's discovery proved false (40 of 44 owners match a
    # customer contact by email).
    #
    # OFF BY DEFAULT, deliberately: the directory currently holds four people
    # and one of them is the CEO, so strict mode today would refuse nearly
    # every assignment. Flipping ASSIGNABLE_STRICT=1 is the act that says the
    # directory is real, and it is a decision about DATA, not about code.
    try:
        from app.core import assignable
        if assignable.STRICT:
            return assignable.resolve(raw)
    except Exception as exc:
        logger.debug(f"[cases] assignable check skipped: {exc}")

    if _UUID_RE.match(raw):
        return raw if _owner_exists(raw) else None
    if "@" not in raw:
        # A display name is not an identity: two people can share one, and the
        # console supplies things like "agent". Refuse rather than guess.
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT owner_id::text FROM owners "
                        "WHERE lower(email)=lower(%s) AND coalesce(is_active,true) "
                        "LIMIT 1", (raw,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[cases] owner resolution failed for {raw!r}: {exc}")
        return None
    finally:
        conn.close()


def _owner_exists(owner_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM owners WHERE owner_id=%s::uuid", (owner_id,))
            return cur.fetchone() is not None
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


# ============================================================================
# THE SINGLE MUTATION PATH
# ============================================================================

def _mutate(case_id: str, changes: Dict[str, Any], *,
            actor: str, actor_id: Optional[str], source: str,
            extra_sets: Optional[Dict[str, Any]] = None,
            expect_transition: bool = False) -> Dict[str, Any]:
    """Apply field changes to ONE case and record their history ATOMICALLY.

    The whole point of this function is the transaction boundary. In one
    transaction:

        SELECT ... FOR UPDATE   <- lock the row and read the PREVIOUS values
        (validate)
        UPDATE cases
        INSERT record_field_history (one row per CHANGED tracked field)
        COMMIT

    FOR UPDATE is not incidental. Reading the old value without the lock lets a
    concurrent writer interleave between read and update, producing a history
    chain that reads plausibly and is false — the one failure mode that would
    make the whole history worthless.

    If the UPDATE fails the history rolls back with it; if a history INSERT
    fails the UPDATE rolls back with it. There is no ordering in which one
    survives without the other.
    """
    if not ENABLED:
        raise CaseError("cases disabled (CASES_ENABLED=0)")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, owner_id::text, priority, reopen_count, "
                "       is_historical "
                "FROM cases WHERE case_id=%s::uuid FOR UPDATE", (case_id,))
            row = cur.fetchone()
            if not row:
                raise CaseError(f"no such case: {case_id}")
            before = {"status": row[0], "owner_id": row[1], "priority": row[2]}
            reopen_count, is_historical = row[3], row[4]

            if expect_transition:
                _check_transition(before["status"], changes.get("status"))

            # Only fields whose value ACTUALLY changes are written or recorded.
            # A no-op UPDATE that emits a history row is a lie about what
            # happened, and it is how history tables fill with noise.
            actual = {f: v for f, v in changes.items()
                      if _norm(v) != _norm(before.get(f))}
            if not actual and not extra_sets:
                return {"ok": True, "changed": [], "case_id": case_id,
                        "note": "no change"}

            sets, params = [], {"cid": case_id}
            for f, v in actual.items():
                cast = "::uuid" if f == "owner_id" else ""
                sets.append(f"{f}=%({f})s{cast}")
                params[f] = v
            for f, v in (extra_sets or {}).items():
                if isinstance(v, _Raw):
                    sets.append(f"{f}={v.sql}")     # server-side, never input
                else:
                    sets.append(f"{f}=%({f})s")
                    params[f] = v
            sets.append("updated_at=now()")

            cur.execute(f"UPDATE cases SET {', '.join(sets)} "
                        f"WHERE case_id=%(cid)s::uuid", params)

            for f, v in actual.items():
                if f not in TRACKED_FIELDS:
                    continue
                _write_history(cur, case_id, f, before.get(f), v,
                               actor=actor, actor_id=actor_id, source=source)
        conn.commit()
        logger.info(f"[cases] {case_id[:8]} {sorted(actual)} by {actor} "
                    f"({source})")
        return {"ok": True, "case_id": case_id, "changed": sorted(actual),
                "before": before, "reopen_count": reopen_count,
                "is_historical": is_historical}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_history(cur, case_id: str, field: str, old: Any, new: Any, *,
                   actor: str, actor_id: Optional[str], source: str) -> None:
    """Case-shaped wrapper over THE shared writer (app/core/history.py).

    The implementation moved out when routing policy became the second object
    worth auditing — one writer, one definition of "before". This keeps the
    case call sites unchanged and still hands the caller's cursor down, so a
    change and its history commit together or not at all."""
    from app.core import history
    history.write(cur, "case", case_id, field, old, new,
                  actor=actor, actor_id=actor_id, source=source)


class _Raw:
    """A server-side SQL fragment for an UPDATE ... SET clause.

    Only ever constructed from the literals below — never from caller input —
    so this cannot become an injection path."""

    def __init__(self, sql: str):
        self.sql = sql


_RAW_NOW = _Raw("now()")
_RAW_REOPEN = _Raw("reopen_count + 1")


def _norm(v: Any) -> Any:
    """Compare values the way the database will store them, so a uuid passed as
    a differently-cased string is not mistaken for a change."""
    if v is None:
        return None
    s = str(v).strip()
    return s.lower() if _UUID_RE.match(s) else s


def _txt(v: Any) -> Optional[str]:
    """History stores text. NULL must survive as NULL — it means the field was
    PREVIOUSLY UNSET, which is different information from '' or 'unknown'."""
    return None if v is None else str(v)


def _check_transition(current: Optional[str], target: Optional[str]) -> None:
    if target is None:
        return
    if target not in STATUSES:
        raise InvalidTransition(
            f"'{target}' is not a case status; valid: {', '.join(STATUSES)}")
    allowed = TRANSITIONS.get(current or "", ())
    if target == current:
        raise InvalidTransition(f"case is already '{current}'")
    if target not in allowed:
        raise InvalidTransition(
            f"{current} -> {target} is not a permitted transition"
            + (f" (from '{current}' only: {', '.join(allowed)})" if allowed
               else f" ('{current}' is terminal)"))


# ============================================================================
# PUBLIC WRITE API — the only way a case changes
# ============================================================================

def open_case(subject: str, *, actor: str = "system",
              actor_id: Optional[str] = None, source: str = "manual",
              description: str = "", priority: str = "medium",
              origin: Optional[str] = None,
              account_id: Optional[str] = None,
              contact_id: Optional[str] = None,
              owner_id: Optional[str] = None,
              conversation_id: Optional[str] = None,
              escalation_id: Optional[str] = None,
              source_assignee: Optional[str] = None) -> Dict[str, Any]:
    """Create a case explicitly. Step 2 never calls this itself — automatic
    creation from an escalation or takeover is the Step 3 bridge, gated by
    CASES_AUTO_OPEN, which is off."""
    if not ENABLED:
        raise CaseError("cases disabled (CASES_ENABLED=0)")
    if not (subject or "").strip():
        raise CaseError("a case needs a subject")
    if priority not in PRIORITIES:
        raise CaseError(f"priority must be one of {', '.join(PRIORITIES)}")
    if owner_id and not _owner_exists(owner_id):
        raise CaseError(f"owner_id {owner_id} is not a known CRM owner")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO cases
                     (subject, description, priority, status, origin,
                      account_id, contact_id, owner_id,
                      conversation_id, escalation_id, source_assignee,
                      is_historical)
                   VALUES (%s,%s,%s,'new',%s,%s::uuid,%s::uuid,%s::uuid,
                           %s::uuid,%s::uuid,%s,false)
                   RETURNING case_id::text""",
                (subject[:500], description or "", priority, origin,
                 account_id, contact_id, owner_id,
                 conversation_id, escalation_id,
                 (source_assignee or None)))
            case_id = cur.fetchone()[0]
            # The opening state is recorded so the chain starts at a known
            # point rather than materialising mid-life. old_value is NULL —
            # the case genuinely had no prior status or owner.
            _write_history(cur, case_id, "status", None, "new",
                           actor=actor, actor_id=actor_id,
                           source=source or "manual")
            if owner_id:
                _write_history(cur, case_id, "owner_id", None, owner_id,
                               actor=actor, actor_id=actor_id,
                               source=source or "manual")
        conn.commit()
        logger.info(f"[cases] opened {case_id[:8]} ({source}) by {actor}")
        return {"ok": True, "case_id": case_id, "status": "new"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================================
# THE ESCALATION BRIDGE — Step 3
# ============================================================================
# An escalation is an EVENT; a case is the durable unit of WORK it creates.
# This is the only place that turns one into the other.

# Only escalations that still represent OWED work. Bridging a resolved
# escalation would manufacture a backlog of work that is already finished.
BRIDGEABLE_STATUSES = ("open", "assigned")

# escalations use 'normal'; cases use 'medium'. Same middle default, different
# word — an unmapped value would be rejected by open_case()'s validation, so
# this mapping is required, not cosmetic.
_PRIORITY_MAP = {"low": "low", "normal": "medium", "medium": "medium",
                 "high": "high", "urgent": "urgent"}


def open_from_escalation(escalation_id: str, *, actor: str = "system",
                         actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Create the case a bridgeable escalation owes. Idempotent.

    OWNERSHIP (ratified 2026-07-26): the WORK RECORD matters more than a
    perfect owner mapping. If `assigned_to` does not resolve to a real CRM
    identity the case is still created, unowned, with the raw string preserved
    in `source_assignee` — so C2 routing can later answer "why is this
    unowned, and who was originally named?". The string is NEVER cast into
    owner_id and no owner is ever invented.

    IDEMPOTENCY is enforced by the database (uq_cases_escalation), not by a
    check-then-insert race. A second call returns the existing case.

    TIMESTAMPS: the escalation's own timestamps are NOT case response or
    resolution times. There is no established semantic equivalence between
    "the escalation was raised" and "a human responded", so first_response_at
    and resolved_at stay NULL — unknown, never fabricated.

    This function NEVER writes to `escalations`. There is no second write that
    could mark a source as bridged when the case did not survive.
    """
    if not ENABLED:
        return {"ok": False, "skipped": "cases disabled (CASES_ENABLED=0)"}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT status, reason, summary, transcript_excerpt, priority,
                          assigned_to, conversation_id::text, channel,
                          party_type, party_id::text, source
                   FROM escalations WHERE escalation_id=%s::uuid""",
                (escalation_id,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": f"no such escalation: {escalation_id}"}
            esc = dict(zip([d[0] for d in cur.description], row))

            # Already bridged? Return it — reprocessing is not an error.
            cur.execute("SELECT case_id::text FROM cases "
                        "WHERE escalation_id=%s::uuid", (escalation_id,))
            existing = cur.fetchone()
    finally:
        conn.close()

    if existing:
        return {"ok": True, "case_id": existing[0], "created": False,
                "note": "escalation already bridged"}

    if esc["status"] not in BRIDGEABLE_STATUSES:
        return {"ok": False, "skipped": f"escalation status "
                                        f"'{esc['status']}' is not bridgeable",
                "bridgeable": list(BRIDGEABLE_STATUSES)}

    raw_assignee = (esc.get("assigned_to") or "").strip() or None
    owner_id = resolve_owner(raw_assignee) if raw_assignee else None

    # Party linkage only when the escalation actually resolved an identity.
    account_id = esc["party_id"] if esc.get("party_type") == "account" else None
    contact_id = esc["party_id"] if esc.get("party_type") == "contact" else None

    try:
        out = open_case(
            (esc.get("summary") or REASON_SUBJECT_FALLBACK)[:500],
            actor=actor, actor_id=actor_id,
            source=f"escalation:{esc.get('reason') or 'unknown'}",
            description=esc.get("transcript_excerpt") or "",
            priority=_PRIORITY_MAP.get((esc.get("priority") or "").lower(),
                                       "medium"),
            # Stored VERBATIM. Mapping 'sdr_chat'/'store_chat'/'voice' onto the
            # legacy chat/email/phone/web vocabulary would invent equivalences
            # (sms is not "phone") and lose the channel that actually produced
            # the work.
            origin=esc.get("channel"),
            account_id=account_id, contact_id=contact_id,
            owner_id=owner_id,
            conversation_id=esc.get("conversation_id"),
            escalation_id=escalation_id,
            source_assignee=raw_assignee)
    except Exception as exc:
        # A lost race on uq_cases_escalation means somebody else bridged it
        # first — the invariant held, so return their case rather than failing.
        if "uq_cases_escalation" in str(exc):
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT case_id::text FROM cases "
                                "WHERE escalation_id=%s::uuid", (escalation_id,))
                    r = cur.fetchone()
            finally:
                conn.close()
            if r:
                return {"ok": True, "case_id": r[0], "created": False,
                        "note": "bridged concurrently"}
        raise

    out.update({"created": True, "owner_resolved": owner_id is not None,
                "source_assignee": raw_assignee,
                "unowned_reason": (None if owner_id else
                                   ("no assignee on the escalation"
                                    if not raw_assignee else
                                    f"assignee {raw_assignee!r} is not a "
                                    f"known CRM owner"))})
    if not owner_id:
        logger.info(f"[cases] {out['case_id'][:8]} created UNOWNED from "
                    f"escalation {escalation_id[:8]} — {out['unowned_reason']}")
    return out


REASON_SUBJECT_FALLBACK = "Escalation"


def case_for_conversation(conversation_id: str) -> Optional[Dict[str, Any]]:
    """The LIVE case for a conversation, if any. Read-only.

    'Live' means not closed, matching uq_cases_live_conversation — a closed
    case does not block new work on the same thread."""
    if not conversation_id:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT case_id::text, status, owner_id::text, priority
                   FROM cases
                   WHERE conversation_id=%s::uuid AND status <> 'closed'
                   ORDER BY created_at DESC LIMIT 1""", (conversation_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"case_id": row[0], "status": row[1], "owner_id": row[2],
            "priority": row[3]}


def open_for_conversation(conversation_id: str, *, actor: str = "system",
                          actor_id: Optional[str] = None,
                          subject: Optional[str] = None,
                          source: str = "console") -> Dict[str, Any]:
    """Create the case a human intervention owes — EXPLICITLY.

    This is deliberately NOT called by takeover(). The console cannot tell a
    rep who answered one quick question from a rep who accepted work that
    outlives the interaction, and guessing in either direction is worse than
    asking: guess "durable" and every assisted chat becomes a case, destroying
    the meaning of containment; guess "temporary" and real work keeps
    evaporating. So the human declares it.

    IDEMPOTENT via uq_cases_live_conversation: a repeat click, a retry, or a
    conversation the escalation bridge already cased all converge on one row.

    CASES_AUTO_OPEN is NOT consulted. That flag governs AUTOMATIC escalation
    bridging; an explicit human action is a different trigger.
    """
    if not ENABLED:
        return {"ok": False, "skipped": "cases disabled (CASES_ENABLED=0)"}
    if not conversation_id:
        return {"ok": False, "error": "conversation_id is required"}

    existing = case_for_conversation(conversation_id)
    if existing:
        # Attach, never reset. The lifecycle, owner and timestamps of work
        # already under way are not the console's to overwrite.
        return {"ok": True, "case_id": existing["case_id"], "created": False,
                "status": existing["status"],
                "note": "conversation already has a live case"}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT subject, channel, account_id::text,
                                  party_type, party_id::text
                           FROM conversations WHERE conversation_id=%s::uuid""",
                        (conversation_id,))
            row = cur.fetchone()
            if not row:
                return {"ok": False,
                        "error": f"no such conversation: {conversation_id}"}
            convo = dict(zip([d[0] for d in cur.description], row))
            # Carry the originating EVENT through when one exists and is not
            # already claimed by another case (uq_cases_escalation).
            cur.execute(
                """SELECT e.escalation_id::text, e.priority
                   FROM escalations e
                   LEFT JOIN cases c ON c.escalation_id = e.escalation_id
                   WHERE e.conversation_id=%s::uuid
                     AND e.status = ANY(%s) AND c.case_id IS NULL
                   ORDER BY e.created_at DESC LIMIT 1""",
                (conversation_id, list(BRIDGEABLE_STATUSES)))
            esc = cur.fetchone()
    finally:
        conn.close()

    raw_assignee = (actor or "").strip() or None
    owner_id = resolve_owner(raw_assignee)

    try:
        out = open_case(
            (subject or convo.get("subject")
             or "Service work from a live conversation")[:500],
            actor=actor, actor_id=actor_id, source=source,
            priority=_PRIORITY_MAP.get((esc[1] if esc else "") or "", "medium"),
            origin=convo.get("channel"),
            account_id=convo.get("account_id"),
            contact_id=(convo.get("party_id")
                        if convo.get("party_type") == "contact" else None),
            owner_id=owner_id,
            conversation_id=conversation_id,
            escalation_id=(esc[0] if esc else None),
            source_assignee=raw_assignee)
    except Exception as exc:
        # Lost the race on uq_cases_live_conversation: someone else opened the
        # case first, which is the invariant holding, not a failure.
        if "uq_cases_live_conversation" in str(exc):
            again = case_for_conversation(conversation_id)
            if again:
                return {"ok": True, "case_id": again["case_id"],
                        "created": False, "note": "opened concurrently"}
        raise

    out.update({"created": True, "owner_resolved": owner_id is not None,
                "source_assignee": raw_assignee,
                "escalation_id": (esc[0] if esc else None),
                "unowned_reason": (None if owner_id else
                                   f"console identity {raw_assignee!r} is not "
                                   f"a known CRM owner")})
    if not owner_id:
        logger.info(f"[cases] {out['case_id'][:8]} created UNOWNED from "
                    f"console — {out['unowned_reason']}")
    return out


def bridge_backlog(limit: int = 50, *, actor: str = "system") -> Dict[str, Any]:
    """Bridge every un-bridged, still-owed escalation. Manual/explicit only —
    nothing calls this on a schedule in Step 3."""
    if not ENABLED:
        return {"ok": False, "skipped": "cases disabled"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.escalation_id::text
                   FROM escalations e
                   LEFT JOIN cases c ON c.escalation_id = e.escalation_id
                   WHERE e.status = ANY(%s) AND c.case_id IS NULL
                   ORDER BY e.created_at
                   LIMIT %s""",
                (list(BRIDGEABLE_STATUSES), max(1, min(limit, 500))))
            ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    made, skipped = [], []
    for eid in ids:
        try:
            r = open_from_escalation(eid, actor=actor)
            (made if r.get("created") else skipped).append(eid)
        except Exception as exc:
            logger.error(f"[cases] bridge failed for {eid[:8]}: {exc}")
            skipped.append(eid)
    return {"ok": True, "considered": len(ids), "created": len(made),
            "skipped": len(skipped)}


def transition(case_id: str, to_status: str, *, actor: str = "system",
               actor_id: Optional[str] = None,
               source: str = "manual") -> Dict[str, Any]:
    """Move a case through the lifecycle. Refuses anything not in TRANSITIONS.

    `resolved_at` and `closed_at` are stamped by the transitions that mean
    them. That is not added policy — it is what those states ARE."""
    extra: Dict[str, Any] = {}
    if to_status == "resolved":
        extra["resolved_at"] = _RAW_NOW
    elif to_status == "closed":
        extra["closed_at"] = _RAW_NOW
    return _mutate(case_id, {"status": to_status}, actor=actor,
                   actor_id=actor_id, source=source, extra_sets=extra,
                   expect_transition=True)


def reopen(case_id: str, *, actor: str = "system",
           actor_id: Optional[str] = None,
           source: str = "manual") -> Dict[str, Any]:
    """resolved -> in_progress, counted.

    A CLOSED case is refused. D3 defines that reopen as a NEW case linked to
    the old one, and Step 1 added no parent link column — so resurrecting the
    row would both contradict the design and silently destroy the closure
    record. The refusal names the missing piece instead of guessing."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM cases WHERE case_id=%s::uuid",
                        (case_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise CaseError(f"no such case: {case_id}")
    if row[0] == "closed":
        raise InvalidTransition(
            "a closed case cannot be reopened in place — D3 defines this as a "
            "NEW linked case, which needs a parent-case column that Step 1 did "
            "not add (pending approval)")
    out = _mutate(case_id, {"status": REOPEN_TO}, actor=actor,
                  actor_id=actor_id, source=source,
                  extra_sets={"reopen_count": _RAW_REOPEN},
                  expect_transition=True)
    out["reopened"] = True
    return out


def assign(case_id: str, owner_id: str, *, actor: str = "system",
           actor_id: Optional[str] = None,
           source: str = "manual") -> Dict[str, Any]:
    """Set the owner. Accepts a uuid ONLY, and validates it against `owners`.

    Deliberately not a free string: `escalations.assigned_to` is UI-supplied
    text that is frequently not an identity at all (console_takeover defaults
    it to the literal "agent"). Use resolve_owner() to map a string first, and
    handle a None result explicitly — the case's owner must be a real,
    auditable CRM identity."""
    if not owner_id or not _UUID_RE.match(str(owner_id).strip()):
        raise CaseError("assign() requires an owners.owner_id uuid — use "
                        "resolve_owner() to map a name/email first")
    if not _owner_exists(owner_id):
        raise CaseError(f"owner_id {owner_id} is not a known CRM owner")
    return _mutate(case_id, {"owner_id": str(owner_id).strip()}, actor=actor,
                   actor_id=actor_id, source=source)


def unassign(case_id: str, *, actor: str = "system",
             actor_id: Optional[str] = None,
             source: str = "manual") -> Dict[str, Any]:
    """Clear the owner. value -> NULL is a real, recorded change."""
    return _mutate(case_id, {"owner_id": None}, actor=actor,
                   actor_id=actor_id, source=source)


def set_priority(case_id: str, priority: str, *, actor: str = "system",
                 actor_id: Optional[str] = None,
                 source: str = "manual") -> Dict[str, Any]:
    if priority is not None and priority not in PRIORITIES:
        raise CaseError(f"priority must be one of {', '.join(PRIORITIES)}")
    return _mutate(case_id, {"priority": priority}, actor=actor,
                   actor_id=actor_id, source=source)


def mark_first_response(case_id: str) -> Dict[str, Any]:
    """Stamp first_response_at, once, if it is not already set.

    A HOOK, not a policy: this module never decides what counts as a first
    response. The console bridge (Step 4) calls it when a reply actually goes
    out. Idempotent, so a second reply cannot overwrite the first."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE cases SET first_response_at=now(), "
                        "updated_at=now() "
                        "WHERE case_id=%s::uuid AND first_response_at IS NULL",
                        (case_id,))
            stamped = cur.rowcount > 0
        conn.commit()
        return {"ok": True, "stamped": stamped}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def comment(case_id: str, body: str, *, internal: bool = False,
            created_by: Optional[str] = None) -> Dict[str, Any]:
    """Append a case comment. CASE-LOCAL by design.

    Open design question 2 (do comments enter the unified Conversation Object?)
    is NOT decided here. `case_comments.is_internal` already distinguishes a
    public reply from an internal note, which is all Step 2 needs."""
    if not ENABLED:
        raise CaseError("cases disabled (CASES_ENABLED=0)")
    if not (body or "").strip():
        raise CaseError("empty comment")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO case_comments (case_id, comment, is_internal,
                                              created_by)
                   VALUES (%s::uuid, %s, %s, %s::uuid)
                   RETURNING case_comment_id::text""",
                (case_id, body, bool(internal), created_by))
            cid = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "case_comment_id": cid}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================================
# READS
# ============================================================================

def get(case_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT case_id::text, subject, status, priority,
                          owner_id::text, conversation_id::text,
                          escalation_id::text, first_response_at, resolved_at,
                          closed_at, reopen_count, is_historical, created_at
                   FROM cases WHERE case_id=%s::uuid""", (case_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            out = dict(zip(cols, row))
            for k in ("first_response_at", "resolved_at", "closed_at",
                      "created_at"):
                if out.get(k):
                    out[k] = out[k].isoformat()
            out["next_states"] = list(TRANSITIONS.get(out["status"], ()))
            return out
    finally:
        conn.close()


def history(case_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """The provable chain: what each tracked field was BEFORE."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT field, old_value, new_value, actor, actor_id::text,
                          source, changed_at
                   FROM record_field_history
                   WHERE entity='case' AND entity_id=%s::uuid
                   ORDER BY changed_at, history_id
                   LIMIT %s""", (case_id, max(1, min(limit, 500))))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        if r.get("changed_at"):
            r["changed_at"] = r["changed_at"].isoformat()
    return rows


def status_snapshot() -> Dict[str, Any]:
    """Counts for a health view. Live and historical are reported SEPARATELY —
    a historical row's unknown timestamps must never be averaged into a
    first-response or resolution statistic."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT status, is_historical, count(*)
                           FROM cases GROUP BY 1,2""")
            live, hist = {}, {}
            for status, is_hist, n in cur.fetchall():
                (hist if is_hist else live)[status] = n
    finally:
        conn.close()
    return {"ok": True, "enabled": ENABLED, "auto_open": AUTO_OPEN,
            "kb_feedback": KB_FEEDBACK, "live": live, "historical": hist}
