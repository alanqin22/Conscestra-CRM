"""Staff email — the attention budget, and the only door operational mail
to employees may leave through.

    Notifications describe what happened.
    Worklists describe what needs attention.
    Email signals what cannot safely wait.

THE INVARIANT THIS MODULE EXISTS TO ENFORCE

    A notification row is NEVER sufficient authority to send an email.

7,956 in-app notifications exist; 1,728 arrived on the peak day; 7,209 of the
last week's 7,301 carry no classification at all. Mirroring that into mailboxes
would produce ~560 emails a day to eight people, and the four identities
actually authorized to receive work account for **101 of 7,650 notifications —
1.3%**. Email is therefore not a second copy of the notification system. It is a
scarce channel with a per-recipient budget, and spending it is a decision this
module makes, records, and can be asked to justify.

    docs/employee_email_notifications_design.md

WHERE THIS MODULE MAY SEND, AND ONLY THERE

Stages 1–3 could not send at all. Stage 4 added the digest, so the guarantee
changed shape rather than being dropped:

    send_email is called from EXACTLY ONE function — `_deliver` — and `_deliver`
    is unreachable except through claim -> acquire -> mark_attempted.

Everything else here decides, records and refuses. The approval and escalation
mail still leaves from governance.py and escalation.py; this module wraps those
attempts in a ledger rather than taking them over.

THE FAIL RULE IS NOT UNIFORM, AND THE ASYMMETRY IS DELIBERATE:

    wrapping mail that ALREADY EXISTS (Stage 3)  -> fail OPEN.
        bookkeeping must never cost an executive an approval they were
        already receiving.
    sending NEW mail (Stage 4 digest)            -> fail CLOSED.
        nobody is waiting for it, nothing regresses if it does not arrive,
        and mail we cannot record is how a volume incident goes unnoticed.

THE THREE RULES THAT SHAPE EVERYTHING BELOW

  1. TIER DECIDES WHETHER, PREFERENCE DECIDES WHO ASKED FOR IT, AND THE DEFAULT
     IS NO. `notification_triage.classify()` already computes the taxonomy and
     throws it away; this module persists and honours it. Anything unclassified
     is `informational`, and informational never emails. A new event type cannot
     acquire email merely by being added.

  2. EMAILABLE IS NOT THE SAME AS ROUTABLE — `app/core/assignable.py`.
     Two live traps make this concrete rather than theoretical:

       * `notifications.employee_uuid` is not a foreign key and resolves in
         three identity spaces. `a1451ad6-…` is julia.martin@emp.agentorc.ca in
         `employees` AND john.smith@example.com in `owners` — one uuid, two
         people, 486 notifications in a week. This module never resolves an
         address from that column, and refuses any uuid that
         `identity_space()` reports as a collision.
       * `escalations.assigned_to` is free text. It currently contains a
         customer's address, two `.invalid` test fixtures, a display name and
         the literal 'agent'. A value that does not resolve to explicit
         membership is DISCARDED, never used, never fallen back to raw.

     When nothing resolves, the answer is the ROLE MAILBOX. That is honest —
     it survives someone leaving, and it does not pretend the system knows
     whose desk this belongs on.

  3. TOO MUCH EMAIL IS A PRODUCT FAILURE EVEN IF EVERY MESSAGE IS VALID.
     Hence a per-recipient cap and a global breaker, both counted from the
     LEDGER rather than in process — `leader.py` gives one scheduler leader,
     but HTTP replicas send too, and `rate_limit.py` documents itself as
     per-instance, "which only makes limits more generous, never stricter."
     That is the wrong direction for this.

CONFIG (env)
  STAFF_EMAIL_ENABLED                 0   master switch; 0 = decide nothing
  STAFF_EMAIL_APPLY                   0   1 = actually send (Stage 4+)
  STAFF_EMAIL_MAX_PER_RECIPIENT_HOUR  6   2x the busiest measured normal day
  STAFF_EMAIL_MAX_PER_RECIPIENT_DAY  12
  STAFF_EMAIL_BREAKER_PER_HOUR       25   global; trips the 2026-08-12 burst
                                          (93 emailable escalations) at msg 25
  STAFF_EMAIL_BREAKER_PER_DAY        60   global; 4x the busiest legitimate day
  STAFF_EMAIL_ROLE_MAILBOX               defaults to ESCALATION_EMAIL_TO

Requires sql/staff_email_ledger.sql.
"""

from __future__ import annotations

import logging
import os
import re
from html import escape as html_escape
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("staff_email")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def enabled() -> bool:
    """Read at call time, not import time: the flag is flipped per stage and a
    module-level constant would freeze whatever was set when the worker booted."""
    return _flag("STAFF_EMAIL_ENABLED", "0")


def applying() -> bool:
    """Stage 4+. Even when true, Stage 1 has no send path to reach."""
    return _flag("STAFF_EMAIL_APPLY", "0")


def role_mailbox() -> str:
    return (os.getenv("STAFF_EMAIL_ROLE_MAILBOX")
            or os.getenv("ESCALATION_EMAIL_TO")
            or "support@agentorc.ca").strip()


# Tiers, named as notification_triage.classify() names them. Not a second
# taxonomy — the SAME one, persisted.
TIER_INTERRUPT = "critical"        # Tier 1 — may email immediately
TIER_WORKLIST = "actionable"       # Tier 2 — digest only
TIER_AMBIENT = "informational"     # Tier 3 — never

EMAIL_KINDS = ("approval", "escalation", "escalation_remind", "digest")

# How long one worker owns a send before it is presumed dead and reclaimable.
# Matches order_notifications.ATTEMPT_LEASE deliberately: one lease policy for
# "we are contacting somebody", not two that can drift apart.
ATTEMPT_LEASE = "15 minutes"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ============================================================================
# TIER — persist the classification that already exists
# ============================================================================

def tier_of(event_type: Optional[str]) -> str:
    """The tier for an event type. Delegates to the ONE classifier.

    Importing notification_triage rather than restating its sets is the whole
    point: two copies of "which events matter" drift, and the copy that drifts
    is the one nobody is watching. If triage cannot be imported we return
    ambient — the fail-safe direction, because ambient never emails."""
    try:
        from app.core.notification_triage import classify
        return classify(event_type)
    except Exception as exc:                                   # pragma: no cover
        logger.warning(f"[staff_email] classify unavailable, defaulting to "
                       f"ambient: {exc}")
        return TIER_AMBIENT


def tier_for_escalation(reason: Optional[str]) -> str:
    """An escalation's tier, read off the gate that already decides this.

    NOT A SECOND TAXONOMY. `escalation._EMAIL_REASONS` is already the set that
    answers "must a human do something about this", it is already curated, and
    it is already tested. This function translates that existing answer into
    tier vocabulary — it does not re-decide it. Copying the five reasons into a
    literal here is exactly the drift this codebase keeps paying for.

    Everything outside that set is Tier 2: a real queue item, worth a line in
    somebody's digest, not worth interrupting them for.
    """
    try:
        from app.core.escalation import _EMAIL_REASONS
        return TIER_INTERRUPT if reason in _EMAIL_REASONS else TIER_WORKLIST
    except Exception as exc:                                   # pragma: no cover
        logger.warning(f"[staff_email] escalation gate unavailable, "
                       f"defaulting to worklist: {exc}")
        return TIER_WORKLIST


def may_email(tier: Optional[str]) -> bool:
    """Only Tier 1 and Tier 2 can ever produce mail — and Tier 2 only as a
    digest. NULL, unknown and ambient are all 'no', which is what makes the
    default deny rather than allow."""
    return tier in (TIER_INTERRUPT, TIER_WORKLIST)


def set_message_tier(notification_uuid: str, tier: str) -> bool:
    """Stamp the tier on a notification MESSAGE (content), not a recipient row."""
    if tier not in (TIER_INTERRUPT, TIER_WORKLIST, TIER_AMBIENT):
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE notification_messages SET tier=%s
                           WHERE notification_uuid=%s::uuid""",
                        (tier, notification_uuid))
            n = cur.rowcount
        conn.commit()
        return n > 0
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[staff_email] tier stamp skipped: {exc}")
        return False
    finally:
        conn.close()


def sync_tier_rules() -> Dict[str, Any]:
    """Copy the taxonomy from Python into `notification_tier_rules`.

    The database needs the map so the notifications INSERT trigger can stamp a
    tier at the one choke point every writer passes through. But the map itself
    must have ONE author, or the SQL copy and the Python copy drift and the one
    that drifts is whichever nobody is watching. So Python is the author, this
    function is the copier, and `test_11_the_database_rules_match_the_classifier`
    fails if they ever disagree.

    ONLY critical and actionable are stored. Informational is expressed as
    ABSENCE — an event type with no row is not email-worthy — which is what
    makes 'a new event type cannot acquire email by being added' a structural
    property rather than a promise. Rules that vanish from Python are DELETED
    here, so removing a type from ACTIONABLE_TYPES actually demotes it.
    """
    from app.core.notification_triage import ACTIONABLE_TYPES, CRITICAL_TYPES
    rules: Dict[str, str] = {t: TIER_INTERRUPT for t in CRITICAL_TYPES}
    rules.update({t: TIER_WORKLIST for t in ACTIONABLE_TYPES})

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for event_type, tier in rules.items():
                cur.execute(
                    """INSERT INTO notification_tier_rules (event_type, tier)
                       VALUES (%s, %s)
                       ON CONFLICT (event_type) DO UPDATE
                         SET tier = EXCLUDED.tier, synced_at = now()""",
                    (event_type, tier))
            cur.execute("DELETE FROM notification_tier_rules "
                        "WHERE NOT (event_type = ANY(%s))",
                        (list(rules.keys()) or [""],))
            removed = cur.rowcount
        conn.commit()
        logger.info(f"[staff_email] tier rules synced: {len(rules)} rules, "
                    f"{removed} removed")
        return {"ok": True, "rules": len(rules), "removed": removed,
                "map": rules}
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[staff_email] tier sync failed "
                       f"(apply sql/staff_email_stage2.sql?): {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()


def tier_rules() -> Dict[str, str]:
    """What the DATABASE currently believes. Read back rather than recomputed,
    so a test can compare it against Python instead of comparing Python to
    itself."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT event_type, tier FROM notification_tier_rules")
            return {r[0]: r[1] for r in cur.fetchall()}
    except Exception:
        conn.rollback()
        return {}
    finally:
        conn.close()


# ============================================================================
# RECIPIENT — the single choke point
# ============================================================================
# Nothing else in the codebase may compute a staff email address. One function,
# one set of rules, one place to test.

def resolve_recipient(*, assignee: Optional[str] = None,
                      executive_id: Optional[str] = None,
                      owner_id: Optional[str] = None) -> Dict[str, Any]:
    """Who, if anyone, should hear about this — and WHY that answer.

    Returns {owner_id, email, kind, why}. `kind` is one of
    'executive' | 'assignable' | 'role_mailbox'. NEVER raises, and never
    returns None: an unresolvable assignee degrades to the role mailbox rather
    than to nothing, because a person who cannot be identified is not a reason
    to drop an obligation on the floor.

    Three modes, in order of how much the caller already knows:

      owner_id      the caller ALREADY holds an authorized identity — the
                    digest, which iterates the directory. Nothing to discard;
                    the only question is what address that identity carries.
      executive_id  an approval, carrying a properly typed executive id.
      assignee      free text of unknown provenance. This is the dangerous
                    one, and the DISCARD below is the entire safety property.

    Applied to today's live data every distinct `escalations.assigned_to` value
    misses, so every live escalation routes to the role mailbox — which is both
    the correct answer and identical to today's behaviour.
    """
    # 0. The caller already knows who this is, and membership was the reason
    #    they knew. Re-derive the ADDRESS from membership anyway rather than
    #    trusting one handed in: an owner_id whose grant was revoked between
    #    the caller's read and this call must stop resolving.
    if owner_id:
        row = _one("""SELECT email FROM assignable_identity
                      WHERE owner_id=%s::uuid AND is_active""", (owner_id,))
        if row and row[0]:
            return {"owner_id": owner_id, "email": row[0], "kind": "assignable",
                    "why": "authorized identity, address from membership"}
        return _role(f"owner {owner_id[:8]}… is not an active assignable "
                     "identity; refused")

    # 1. An approval already carries a properly typed executive id.
    if executive_id:
        row = _one("""SELECT employee_uuid::text, email, full_name
                      FROM executives
                      WHERE executive_id=%s::uuid AND is_active""",
                   (executive_id,))
        if row and row[1]:
            return {"owner_id": row[0], "email": row[1], "kind": "executive",
                    "why": f"routed to {row[2]}"}

    # 2. A free-text assignee. Resolve it, or throw it away — never use it raw.
    if assignee:
        raw = assignee.strip()

        # A colliding uuid names more than one person. `identity_space()` sets
        # `collision: True` for exactly this, and its top-level `space` key is
        # NOT safe to branch on — it picks one of the colliding spaces and
        # answers with confident wrongness. Only `collision` is trustworthy.
        if _UUID_RE.match(raw) and _is_collision(raw):
            return _role("assignee uuid identifies more than one person "
                         "(identity collision); refused")

        owner_id = _resolve_assignable(raw)
        if owner_id:
            email = _one("""SELECT email FROM assignable_identity
                            WHERE owner_id=%s::uuid AND is_active""",
                         (owner_id,))
            if email and email[0]:
                return {"owner_id": owner_id, "email": email[0],
                        "kind": "assignable",
                        "why": "resolved through explicit membership"}
        return _role(f"assignee {raw!r} is not in assignable_identity; "
                     "discarded rather than used as an address")

    # 3. Nobody named. The role mailbox is the honest answer.
    return _role("no assignee; ownership is not recorded for this work")


def _role(why: str) -> Dict[str, Any]:
    return {"owner_id": None, "email": role_mailbox(),
            "kind": "role_mailbox", "why": why}


def _is_collision(candidate: str) -> bool:
    try:
        from app.core import assignable
        return bool(assignable.identity_space(candidate).get("collision"))
    except Exception as exc:                                   # pragma: no cover
        # Cannot prove it is safe -> treat it as unsafe. The cost is one email
        # to a role mailbox; the cost of the other default is a customer
        # receiving staff mail.
        logger.warning(f"[staff_email] collision check failed, refusing "
                       f"{candidate[:8]}: {exc}")
        return True


def _resolve_assignable(identifier: str) -> Optional[str]:
    try:
        from app.core import assignable
        return assignable.resolve(identifier)
    except Exception as exc:                                   # pragma: no cover
        logger.warning(f"[staff_email] assignable.resolve failed: {exc}")
        return None


def preference_for(recipient: Dict[str, Any]) -> Dict[str, Any]:
    """How this recipient wants to be reached.

    A ROLE MAILBOX HAS NO PREFERENCE. It is a destination, not a person — it
    exists precisely so that unowned work still reaches somebody, and letting
    it carry an opt-out would give un-owned exceptions a way to go silent."""
    if recipient.get("kind") == "role_mailbox":
        return {"preferred_channel": "email", "auto_email_enabled": True,
                "source": "role_mailbox"}
    from app.core import assignable
    return assignable.email_preference(recipient.get("owner_id"))


def preference_allows(pref: Dict[str, Any], tier: str) -> Tuple[bool, str, str]:
    """Preference outranks urgency. An employee who has turned operational
    email off does not receive it because an interrupt happened — that is what
    'off' means, and a switch that a sufficiently important event can override
    is not a switch.

    Returns (allowed, sentence, reason_class). The SENTENCE is for a human
    reading a log; the CLASS is a short stable slug an aggregate query can
    GROUP BY. Deriving the slug from the sentence later would make the
    observability table depend on prose nobody promised to keep stable."""
    if not pref.get("auto_email_enabled"):
        return (False,
                f"preference: auto_email_enabled is off ({pref.get('source')})",
                "preference_off")
    channel = (pref.get("preferred_channel") or "in_app").lower()
    if channel not in ("email", "all"):
        return False, f"preference: channel is {channel!r}, not email",             "preference_channel"
    return True, f"preference: {channel} ({pref.get('source')})", "allowed"


# ============================================================================
# BUDGET — per-recipient caps and the global breaker
# ============================================================================

def budget(recipient_email: Optional[str] = None) -> Dict[str, Any]:
    """Current spend against the attention budget, counted from the ledger.

    Only `accepted` rows count. A refusal cost nobody any attention, so
    counting skips would let a classifier bug throttle the mail it was already
    correctly refusing to send."""
    caps = {
        "recipient_hour": _int_env("STAFF_EMAIL_MAX_PER_RECIPIENT_HOUR", 6),
        "recipient_day": _int_env("STAFF_EMAIL_MAX_PER_RECIPIENT_DAY", 12),
        "global_hour": _int_env("STAFF_EMAIL_BREAKER_PER_HOUR", 25),
        "global_day": _int_env("STAFF_EMAIL_BREAKER_PER_DAY", 60),
    }
    row = _one(
        """SELECT
             count(*) FILTER (WHERE accepted_at > now() - interval '1 hour'),
             count(*) FILTER (WHERE accepted_at > now() - interval '1 day'),
             count(*) FILTER (WHERE accepted_at > now() - interval '1 hour'
                                AND recipient_email = %(to)s),
             count(*) FILTER (WHERE accepted_at > now() - interval '1 day'
                                AND recipient_email = %(to)s)
           FROM staff_email_ledger
           WHERE state='accepted' AND accepted_at > now() - interval '1 day'
             AND origin = 'live'""",
        {"to": recipient_email or ""})
    g_hour, g_day, r_hour, r_day = (row or (0, 0, 0, 0))
    return {"caps": caps,
            "spent": {"global_hour": int(g_hour), "global_day": int(g_day),
                      "recipient_hour": int(r_hour), "recipient_day": int(r_day)},
            "recipient_email": recipient_email}


def budget_allows(recipient_email: str) -> Tuple[bool, str, str]:
    """Per-recipient cap first, then the global breaker.

    ORDER MATTERS. Checking the breaker first would let one runaway recipient
    trip a GLOBAL stop and silence everybody else's legitimate mail — the
    per-recipient cap is the narrower instrument and gets to act first."""
    b = budget(recipient_email)
    caps, spent = b["caps"], b["spent"]
    if spent["recipient_hour"] >= caps["recipient_hour"]:
        return False, (f"rate limit: {recipient_email} has had "
                       f"{spent['recipient_hour']} in the last hour "
                       f"(cap {caps['recipient_hour']})"), "rate_limit_recipient"
    if spent["recipient_day"] >= caps["recipient_day"]:
        return False, (f"rate limit: {recipient_email} has had "
                       f"{spent['recipient_day']} today "
                       f"(cap {caps['recipient_day']})"), "rate_limit_recipient"
    if spent["global_hour"] >= caps["global_hour"]:
        return False, (f"BREAKER: {spent['global_hour']} staff emails in the "
                       f"last hour (cap {caps['global_hour']})"), "breaker_global"
    if spent["global_day"] >= caps["global_day"]:
        return False, (f"BREAKER: {spent['global_day']} staff emails today "
                       f"(cap {caps['global_day']})"), "breaker_global"
    return True, "within budget", "allowed"


# ============================================================================
# DECISION
# ============================================================================

def idempotency_key(kind: str, ref: str, ordinal: Optional[int] = None) -> str:
    """Deterministic and content-free, so the same business event recomputes
    the same key and collides instead of sending twice. Never include a
    timestamp, a subject line or a body hash: those change between two
    deliveries of one event, which is exactly when the collision must happen."""
    base = f"{kind}:{ref}"
    return f"{base}:remind:{ordinal}" if ordinal is not None else base


def decide(*, kind: str, tier: str, ref: str,
           assignee: Optional[str] = None,
           executive_id: Optional[str] = None,
           owner_id: Optional[str] = None,
           ordinal: Optional[int] = None,
           items: Optional[int] = None) -> Dict[str, Any]:
    """Should this become an email, and why / why not.

    Every refusal names itself. "Why was this sent to this person?" and "why
    was this otherwise valid notification NOT emailed?" must both be answerable
    from the record, and a refusal that only says False answers neither.

    Ordering is deliberate: the cheap, certain refusals come first, so a Tier 3
    event costs one dictionary lookup and never touches the database. That is
    what makes it safe to call this on all 1,728 notifications of a peak day.
    """
    out: Dict[str, Any] = {
        "send": False, "kind": kind, "tier": tier, "ref": ref,
        "idempotency_key": idempotency_key(kind, ref, ordinal),
        "recipient": None, "reason": "", "reason_class": "",
        # A Tier 3 refusal must not write a ledger row: at 1,728/day that IS
        # the volume problem, restated in a different table.
        "ledgerable": False,
    }

    def _no(reason: str, cls: str) -> Dict[str, Any]:
        out["reason"], out["reason_class"] = reason, cls
        return out

    if kind not in EMAIL_KINDS:
        return _no(f"unknown email kind {kind!r}", "unknown_kind")
    if not enabled():
        return _no("STAFF_EMAIL_ENABLED=0", "disabled")
    if not may_email(tier):
        return _no(f"tier {tier!r} never emails", "tier_never_emails")
    if tier == TIER_WORKLIST and kind != "digest":
        return _no("tier 2 worklist item — deferred to the daily digest",
                   "deferred_to_digest")

    # A digest with nothing in it is not eligible, and that is a GATE rather
    # than a special case outside this function. An empty digest is worse than
    # no digest — it teaches the recipient that this sender is noise — and the
    # rule belongs with the other refusals so it is counted, explained and
    # mutation-tested like them.
    #
    # Deliberately ABOVE `ledgerable`: the refusal is worth an OBSERVATION (it
    # is the difference between "the digest ran and found nothing" and "the
    # digest did not run") but not a ledger row, because nothing was contacted.
    if kind == "digest" and items is not None and items <= 0:
        return _no("nothing actionable — digest skipped", "nothing_actionable")

    # Past this line a real decision is being made about a real person, so it
    # is worth a ledger row whichever way it goes.
    out["ledgerable"] = True

    recipient = resolve_recipient(assignee=assignee, executive_id=executive_id,
                                  owner_id=owner_id)
    out["recipient"] = recipient

    allowed, why, cls = preference_allows(preference_for(recipient), tier)
    if not allowed:
        return _no(why, cls)

    if is_already_handled(out["idempotency_key"]):
        return _no("already in the ledger in a terminal state", "already_handled")

    allowed, why, cls = budget_allows(recipient["email"])
    if not allowed:
        return _no(why, cls)

    out["send"] = True
    out["reason"] = f"eligible — {recipient['why']}; {why}"
    out["reason_class"] = "eligible"
    return out


# ============================================================================
# LEDGER — the idempotency key and the audit record are the same row
# ============================================================================

_COLS = ("email_id, idempotency_key, email_kind, tier, recipient_owner_id, "
         "recipient_email, recipient_kind, subject_ref_type, subject_ref_id, "
         "subject, state, decision_reason, provider, provider_message_id, "
         "provider_response, failure_reason, attempts, first_attempted_at, "
         "last_attempted_at, accepted_at, event_uuid, correlation_id, origin, "
         "created_at, updated_at")

_TERMINAL = ("accepted", "skipped", "rejected")


def _cols() -> List[str]:
    return [c.strip() for c in _COLS.split(",")]


def _row(cur) -> Optional[Dict[str, Any]]:
    r = cur.fetchone()
    return dict(zip(_cols(), r)) if r else None


def _one(sql: str, args=()) -> Optional[tuple]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[staff_email] query failed "
                     f"(apply sql/staff_email_ledger.sql?): {exc}")
        return None
    finally:
        conn.close()


def claim(decision: Dict[str, Any], *, subject: str = "",
          subject_ref_type: Optional[str] = None,
          subject_ref_id: Optional[str] = None,
          event_uuid: Optional[str] = None,
          correlation_id: Optional[str] = None
          ) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Take ownership of this decision. Returns (row, is_new).

    The INSERT runs BEFORE any provider call. That ordering is the whole
    guarantee: if this process dies mid-send, the next delivery of the event
    finds a claimed row rather than an empty table, and converges on it.

    ON CONFLICT DO NOTHING plus a follow-up SELECT — rather than SELECT then
    INSERT — so two replicas racing the same event cannot both conclude the row
    is absent. Same shape as order_notifications.claim(), for the same reason.
    """
    rec = decision.get("recipient") or {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO staff_email_ledger
                      (idempotency_key, email_kind, tier, recipient_owner_id,
                       recipient_email, recipient_kind, subject_ref_type,
                       subject_ref_id, subject, state, decision_reason,
                       event_uuid, correlation_id, origin)
                    VALUES (%(key)s, %(kind)s, %(tier)s, %(oid)s::uuid,
                            %(to)s, %(rkind)s, %(srt)s, %(sri)s::uuid,
                            %(subj)s, 'queued', %(why)s,
                            %(ev)s::uuid, %(cid)s::uuid, %(origin)s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING {_COLS}""",
                {"key": decision["idempotency_key"], "kind": decision["kind"],
                 "tier": decision["tier"], "oid": rec.get("owner_id"),
                 "to": rec.get("email") or role_mailbox(),
                 "rkind": rec.get("kind") or "role_mailbox",
                 "srt": subject_ref_type, "sri": subject_ref_id,
                 "subj": (subject or "")[:400] or None,
                 "why": decision.get("reason"),
                 "ev": event_uuid, "cid": correlation_id,
                 "origin": _origin()})
            row = _row(cur)
            if row is not None:
                conn.commit()
                return row, True
            cur.execute(f"SELECT {_COLS} FROM staff_email_ledger "
                        "WHERE idempotency_key=%s", (decision["idempotency_key"],))
            row = _row(cur)
        conn.commit()
        return row, False
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[staff_email] claim failed "
                       f"(apply sql/staff_email_ledger.sql?): {exc}")
        return None, False
    finally:
        conn.close()


def acquire(email_id: str) -> Optional[Dict[str, Any]]:
    """Take EXCLUSIVE ownership of this send. None if someone else has it.

    The check MUST BE the claim, not precede it. `claim()` deliberately returns
    the existing row when it loses the INSERT race, so every loser would
    otherwise read state='queued', conclude the work was unclaimed, and send.
    This is a compare-and-swap: PostgreSQL takes a row lock for the UPDATE, and
    under READ COMMITTED a waiting statement re-evaluates its WHERE against the
    committed new version — so exactly one caller moves the row out of a
    sendable state.

    Reclaimable: queued / failed (nobody is sending), and `attempted` only
    after ATTEMPT_LEASE, so a crashed worker cannot strand the message forever
    while a live one cannot be double-sent. accepted / skipped / rejected are
    terminal and never reclaimed.

    `attempts` is NOT incremented here — it counts PROVIDER calls, and taking
    the lease is not one.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE staff_email_ledger
                       SET state='attempted',
                           first_attempted_at = COALESCE(first_attempted_at, now()),
                           last_attempted_at  = now(),
                           updated_at = now()
                     WHERE email_id = %s::uuid
                       AND (state IN ('queued', 'failed')
                            OR (state = 'attempted'
                                AND last_attempted_at
                                    < now() - interval '{ATTEMPT_LEASE}'))
                 RETURNING {_COLS}""",
                (str(email_id),))
            row = _row(cur)
        conn.commit()
        return row
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[staff_email] acquire failed: {exc}")
        return None
    finally:
        conn.close()


def _update(email_id: str, _extra_sql: str = "", **fields) -> Optional[Dict[str, Any]]:
    """Set named columns on one ledger row. `_extra_sql` carries expressions
    that are not literal values (`attempts = attempts + 1`, `accepted_at =
    now()`), which cannot be passed as parameters."""
    sets = [f"{k} = %({k})s" for k in fields]
    if _extra_sql:
        sets.append(_extra_sql)
    sets.append("updated_at = now()")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE staff_email_ledger SET {', '.join(sets)} "
                f"WHERE email_id = %(eid)s::uuid RETURNING {_COLS}",
                dict(fields, eid=str(email_id)))
            row = _row(cur)
        conn.commit()
        return row
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[staff_email] update failed: {exc}")
        return None
    finally:
        conn.close()


def release(email_id: str, reason: str) -> Optional[Dict[str, Any]]:
    """Hand the lease back without sending, leaving the row eligible again.
    Used when the send is deliberately not attempted (STAFF_EMAIL_APPLY=0)."""
    return _update(email_id, state="queued", decision_reason=reason[:2000])


def mark_attempted(email_id: str, recipient: str,
                   subject: str) -> Optional[Dict[str, Any]]:
    """Record that we are about to call the PROVIDER. Written before the call,
    so an attempt that never returns is still visible as an attempt. State is
    already 'attempted' — acquire() set it — so this only counts the call."""
    return _update(email_id,
                   "attempts = attempts + 1, last_attempted_at = now()",
                   recipient_email=recipient,
                   subject=(subject or "")[:400],
                   failure_reason=None)


def mark_accepted(email_id: str, provider: str,
                  provider_message_id: Optional[str] = None,
                  response: str = "") -> Optional[Dict[str, Any]]:
    """The provider took responsibility for the message and said so.

    'accepted' is the STRONGEST claim this system may make, and the ledger's
    CHECK constraint has no 'delivered' precisely so that nobody upgrades it
    later without adding webhook ingestion first. Acceptance is not receipt."""
    return _update(email_id, "accepted_at = now()",
                   state="accepted", provider=provider,
                   provider_message_id=provider_message_id,
                   provider_response=(response or "")[:2000],
                   failure_reason=None)


def mark_failed(email_id: str, reason: str) -> Optional[Dict[str, Any]]:
    """Transport or server error — the request did not complete. Retryable:
    acquire() reclaims 'failed'."""
    return _update(email_id, state="failed", failure_reason=(reason or "")[:2000])


def mark_rejected(email_id: str, reason: str) -> Optional[Dict[str, Any]]:
    """The provider evaluated the message and declined it. NOT retryable by
    retrying — the same request meets the same wall."""
    return _update(email_id, state="rejected", failure_reason=(reason or "")[:2000])


def mark_skipped(email_id: str, reason: str) -> Optional[Dict[str, Any]]:
    """A deliberate refusal by a control this platform owns — preference, the
    breaker, an unauthorized recipient. Terminal, and it is what makes a
    non-send visible rather than merely absent."""
    return _update(email_id, state="skipped", decision_reason=(reason or "")[:2000])


def is_already_handled(key: str) -> bool:
    """Has this exact decision already reached a terminal state?"""
    row = _one("SELECT state FROM staff_email_ledger WHERE idempotency_key=%s",
               (key,))
    return bool(row and row[0] in _TERMINAL)


def get(key: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM staff_email_ledger "
                        "WHERE idempotency_key=%s", (key,))
            return _row(cur)
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


# ============================================================================
# OBSERVATION (Stage 2) — decide, record, do nothing
# ============================================================================

def _origin() -> str:
    """Is this a real decision, or one a test caused?

    The suite opens REAL escalations, which fire the real shadow observer.
    Keeping that path exercised is the point — but one suite run put 45
    synthetic 'send' decisions into today's counters, which would have quietly
    made the Stage 2 exit gate lie.

    Labelling beats suppressing. Suppressing under test would leave the write
    path untested and hide a whole class of bug; labelling keeps the code
    exercised, keeps the rows visible, and lets observations() judge the gates
    on 'live' rows alone.

    THIS DEFINITION HAS BEEN WRONG TWICE, IN OPPOSITE DIRECTIONS.

      1. `PYTEST_CURRENT_TEST` alone. Set PER TEST and unset between tests and
         after the session, so a background thread outliving its test wrote as
         `live` — two `kb.publish` approvals from a test run were labelled
         production evidence.
      2. `"pytest" in sys.modules`. Fixed that, but keys on an IMPORT. If any
         dependency ever imports pytest at runtime, real production activity is
         relabelled `test` — and that is the worse direction, because the
         evidence silently disappears and the gate reads a clean zero.

    The answer is neither inference: `CRM_TEST_SESSION` is set once by
    `tests/conftest.py::pytest_configure`, lives for the whole session, is
    inherited by every thread and subprocess, and cannot be acquired by
    accident in production. `PYTEST_CURRENT_TEST` is kept as a secondary for a
    pytest run that somehow does not load our conftest.

    `sys.modules` is DELIBERATELY NOT CHECKED. Mistaking production for a test
    is the failure that hides itself.
    """
    return "test" if (os.getenv("CRM_TEST_SESSION")
                      or os.getenv("PYTEST_CURRENT_TEST")) else "live"


def observe(*, kind: str, tier: str, ref: str,
            assignee: Optional[str] = None,
            executive_id: Optional[str] = None,
            owner_id: Optional[str] = None,
            ordinal: Optional[int] = None,
            items: Optional[int] = None) -> Dict[str, Any]:
    """Take a decision, record that it was taken, and ACT ON NOTHING.

    This is the whole of Stage 2. The two Tier-1 call sites (escalation and
    governance) call it beside their existing behaviour, which is unchanged and
    stays unchanged: they still email exactly what they emailed before, through
    exactly the code they emailed it through. What this adds is seven days of
    evidence about what the new rules WOULD have done, gathered before anything
    depends on them.

    NEVER RAISES, and never returns anything a caller could act on by accident.
    A shadow observer that can break its host is worse than no observer — the
    host here is a customer escalation and an executive approval.

    The counter is AGGREGATE, at (day, kind, tier, decision, reason_class,
    recipient_kind). One row per decision would rebuild the volume problem
    inside the observability table, which is the mistake this whole project
    exists to avoid.
    """
    try:
        d = decide(kind=kind, tier=tier, ref=ref, assignee=assignee,
                   executive_id=executive_id, owner_id=owner_id,
                   ordinal=ordinal, items=items)
    except Exception as exc:                                   # pragma: no cover
        logger.warning(f"[staff_email] observe: decide failed: {exc}")
        return {"ok": False, "error": str(exc)[:200]}

    rec_kind = (d.get("recipient") or {}).get("kind") or "none"
    logger.info(
        f"[staff_email] OBSERVE kind={kind} tier={tier} "
        f"decision={'send' if d['send'] else 'refuse'} "
        f"class={d['reason_class']} to={rec_kind} — {d['reason']}")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO staff_email_observations
                     (day, email_kind, tier, decision, reason_class,
                      recipient_kind, origin, n)
                   VALUES (current_date, %s, %s, %s, %s, %s, %s, 1)
                   ON CONFLICT (day, email_kind, tier, decision, reason_class,
                                recipient_kind, origin)
                   DO UPDATE SET n = staff_email_observations.n + 1,
                                 last_seen_at = now()""",
                (kind, tier, "send" if d["send"] else "refuse",
                 d["reason_class"] or "unclassified", rec_kind, _origin()))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[staff_email] observation not recorded "
                     f"(apply sql/staff_email_stage2.sql?): {exc}")
    finally:
        conn.close()
    return d


def observations(days: int = 7) -> Dict[str, Any]:
    """The Stage 2 exit report: what the rules would have done, and what tiers
    actually arrived.

    Two questions have to be answerable before Stage 3 is allowed to proceed:
      * is the Tier 1 rate single-digit per day?
      * was any Tier 3 event ever judged email-worthy?
    The second must be ZERO, and it is a hard gate rather than a trend — a
    single Tier 3 'send' means the classifier is wrong, and no amount of good
    behaviour elsewhere makes that safe.
    """
    days = max(1, min(int(days or 7), 90))
    # Every row is returned so nothing is hidden, but the GATES below count
    # only origin='live'. A decision a test caused is not evidence about
    # production, and one suite run is enough to make it look like one.
    decisions = _rows(
        """SELECT day::text, email_kind, tier, decision, reason_class,
                  recipient_kind, origin, n
           FROM staff_email_observations
           WHERE day > current_date - %s::int
           ORDER BY day DESC, n DESC""", (days,))
    live = [r for r in decisions if r.get("origin") == "live"]
    tiers = _rows(
        f"""SELECT date_trunc('day', created_at)::date::text AS day,
                   COALESCE(tier, 'unstamped') AS tier, count(*) AS n
            FROM notification_messages
            WHERE created_at > now() - interval '{days} days'
            GROUP BY 1, 2 ORDER BY 1 DESC, 3 DESC""")

    tier1_per_day: Dict[str, int] = {}
    for r in tiers:
        if r["tier"] == TIER_INTERRUPT:
            tier1_per_day[r["day"]] = tier1_per_day.get(r["day"], 0) + int(r["n"])

    leaked = [r for r in live
              if r["decision"] == "send" and r["tier"] == TIER_AMBIENT]

    return {
        "ok": True,
        "window_days": days,
        "decisions": decisions,
        "live_decisions": len(live),
        "test_decisions": len(decisions) - len(live),
        "tier_counts": tiers,
        "gates": {
            "tier1_per_day": tier1_per_day,
            "tier1_max_per_day": max(tier1_per_day.values(), default=0),
            "tier1_single_digit": max(tier1_per_day.values(), default=0) < 10,
            # HARD GATE. Not a threshold, not a trend — a count that must be 0.
            "tier3_sends": sum(int(r["n"]) for r in leaked),
            "tier3_never_emailable": not leaked,
        },
    }


def _rows(sql: str, args=()) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[staff_email] rows query failed: {exc}")
        return []
    finally:
        conn.close()


# ============================================================================
# STAGE 3 — the ledger wraps the senders that ALREADY EXIST
# ============================================================================
# Two paths email staff today: the routed-approval mail in governance.py and the
# exception mail in escalation.py. Stage 3 does not move, rewrite or re-gate
# either of them. They keep their own composition, their own recipients and
# their own send_email call; what they gain is a ledger row around the attempt,
# so "we emailed" stops being a log line and becomes a record with an
# idempotency key and a provider message id.
#
# THIS MODULE STILL DOES NOT SEND. begin_send() claims; finish_send()
# interprets what the CALLER's provider returned. Neither reaches a sender, and
# test_60 parses the AST to keep it that way.
#
# THE FAIL-OPEN DISTINCTION, which is the whole safety argument:
#
#     "the ledger is unavailable"      -> PROCEED, unrecorded.
#     "someone else already has this"  -> SKIP.
#
# They look alike in code and are opposites in consequence. The first is our
# bookkeeping failing, and bookkeeping must never cost an executive their
# approval email — especially while sql/staff_email_ledger.sql is applied
# locally and not yet on Railway. The second is the duplicate this ledger exists
# to prevent. Collapsing them either drops real mail or sends it twice.

def begin_send(*, kind: str, tier: str, ref: str,
               recipient_email: str,
               recipient_kind: str = "role_mailbox",
               recipient_owner_id: Optional[str] = None,
               subject: str = "",
               subject_ref_type: Optional[str] = None,
               subject_ref_id: Optional[str] = None,
               ordinal: Optional[int] = None,
               event_uuid: Optional[str] = None,
               correlation_id: Optional[str] = None,
               decision_reason: str = "stage 3: recording an existing send"
               ) -> Dict[str, Any]:
    """Claim this send before the provider is called. NEVER raises.

    Returns {proceed, recorded, email_id, why}:
      proceed=True,  recorded=True   -> claimed; call finish_send() after
      proceed=True,  recorded=False  -> ledger unavailable; send anyway
      proceed=False                  -> already sent, or another worker owns it
    """
    key = idempotency_key(kind, ref, ordinal)
    decision = {
        "idempotency_key": key, "kind": kind, "tier": tier,
        "reason": decision_reason,
        "recipient": {"owner_id": recipient_owner_id, "email": recipient_email,
                      "kind": recipient_kind, "why": decision_reason},
    }

    row, is_new = claim(decision, subject=subject,
                        subject_ref_type=subject_ref_type,
                        subject_ref_id=subject_ref_id,
                        event_uuid=event_uuid, correlation_id=correlation_id)
    if row is None:
        # Bookkeeping is down. Proceeding unrecorded is the lesser harm, and
        # saying so out loud is the difference between a known gap and a
        # silent one.
        logger.warning(f"[staff_email] ledger unavailable for {key} — "
                       f"proceeding UNRECORDED")
        return {"proceed": True, "recorded": False, "email_id": None,
                "why": "ledger unavailable; sent without a record"}

    if not is_new and row.get("state") in _TERMINAL:
        logger.info(f"[staff_email] {key} already {row['state']} — not resending")
        return {"proceed": False, "recorded": True,
                "email_id": row["email_id"],
                "why": f"already {row['state']}"}

    held = acquire(row["email_id"])
    if held is None:
        logger.info(f"[staff_email] {key} is held by another worker — skipping")
        return {"proceed": False, "recorded": True, "email_id": row["email_id"],
                "why": "another worker holds this send"}

    mark_attempted(row["email_id"], recipient_email, subject)
    return {"proceed": True, "recorded": True, "email_id": row["email_id"],
            "why": "claimed"}


def finish_send(email_id: Optional[str],
                result: Optional[Dict[str, Any]]) -> str:
    """Write the terminal state from what the CALLER's provider returned.

    The classification is `order_notifications.classify_send_result` —
    imported, not restated. It already encodes the doctrine that produced 25
    false 'sent' records when it was absent: the default is failure, and only
    positive evidence promotes it. A second copy of that predicate is a second
    chance to get it subtly wrong.

    NEVER raises. Returns the outcome name, or 'unrecorded' when there was no
    ledger row to write to.
    """
    if not email_id:
        return "unrecorded"
    try:
        from app.core.order_notifications import (ACCEPTED, FAILED, SKIPPED,
                                                  classify_send_result)
        outcome, reason = classify_send_result(result)
        if outcome == ACCEPTED:
            mark_accepted(email_id,
                          str((result or {}).get("provider") or "smtp"),
                          (result or {}).get("provider_message_id"),
                          reason)
        elif outcome == SKIPPED:
            # A control we own refused: the outbound guard, or CASL. A decision,
            # not an error — and terminal, because retrying meets the same wall.
            mark_skipped(email_id, reason)
        else:
            mark_failed(email_id, reason)
        return outcome
    except Exception as exc:                                   # noqa: BLE001
        logger.warning(f"[staff_email] finish_send could not record: {exc}")
        return "unrecorded"


# ============================================================================
# STAGE 4 — THE DIGEST. The first mail this module sends itself.
# ============================================================================
# Stages 1–3 could not send at all, and a test enforced it. Stage 4 is where
# that changes, so the guarantee changes shape rather than disappearing:
#
#     send_email is called from EXACTLY ONE function, `_deliver`, and `_deliver`
#     is unreachable except through claim -> acquire -> mark_attempted.
#
# `test_40_send_email_is_called_from_exactly_one_place` parses the AST for the
# first half; `test_41` proves the second by breaking the ledger and asserting
# nothing goes out.
#
# THE FAIL RULE INVERTS HERE, and this is the most important paragraph in the
# module. Stage 3 wrapped emails that ALREADY EXISTED, so a broken ledger had to
# fail OPEN — bookkeeping must never cost an executive an approval they were
# already getting. A digest is NEW mail. Nobody is waiting for it, nobody
# regresses if it does not arrive, and sending mail we cannot record is how a
# volume incident becomes invisible. So Stage 4 fails CLOSED: no ledger, no
# digest.
#
# AND THE DIGEST IS SKIPPED WHEN IT WOULD SAY NOTHING. An empty digest is worse
# than no digest — it teaches the recipient that this sender is noise, which is
# the precise failure the whole design exists to avoid. Measured today: all four
# authorized recipients have ZERO Tier 2 items, so a live run right now sends
# zero emails. That is the rule working, not the feature failing.

def digest_items(owner_id: str) -> List[Dict[str, Any]]:
    """The Tier 2 worklist for one authorized person: still actionable, still
    theirs, and never Tier 3.

    ON READING `notifications.employee_uuid` AT ALL. F1 says that column is an
    ambiguous identity space and must never yield an address. This does the
    SAFE direction: it starts from an owner_id already proven authorized and
    asks what is waiting for them. The forbidden direction is the reverse —
    taking a notification's uuid and deriving who to email. One resolves work
    for a known person; the other invents a person from a row.

    Tier comes from the stamped column, so a message written before Stage 2 is
    NULL and therefore never included. That is deliberate: those rows were
    written before an email decision existed, and pretending otherwise would
    manufacture a worklist nobody agreed to.
    """
    items = _rows(
        """SELECT n.notification_uuid::text AS id, m.title, m.body,
                  m.tier, n.created_at
           FROM notifications n
           JOIN notification_messages m ON m.notification_uuid = n.message_uuid
           WHERE n.employee_uuid = %s::uuid
             AND n.channel = 'in_app'
             AND n.status <> 'read'
             AND m.tier = %s
           ORDER BY n.created_at DESC
           LIMIT 50""",
        (owner_id, TIER_WORKLIST))

    # Live escalations this person owns, whose reason is NOT one the exception
    # mail already covers. Resolved through assignable.resolve(), so a
    # free-text assignee that means nothing contributes nothing — which today
    # means this half is empty, because not one live value resolves. Correct
    # and honest: an empty section beats a section populated by guessing.
    for esc in _rows(
        """SELECT escalation_id::text AS id, reason, priority, summary,
                  assigned_to, sla_due_at
           FROM escalations
           WHERE status IN ('open','assigned') AND assigned_to IS NOT NULL
           ORDER BY sla_due_at LIMIT 200"""):
        if _resolve_assignable(esc.get("assigned_to") or "") != owner_id:
            continue
        if tier_for_escalation(esc.get("reason")) == TIER_INTERRUPT:
            continue          # the exception mail already carries these
        items.append({"id": esc["id"], "tier": TIER_WORKLIST,
                      "title": f"Escalation: {esc.get('reason')}",
                      "body": esc.get("summary") or "",
                      "created_at": esc.get("sla_due_at")})
    return items


def _app_url() -> Optional[str]:
    """The base for deep links, or None when it cannot be trusted.

    F7 is a recorded production incident: APP_URL defaulted to localhost, the
    approval buttons rendered, the mail went out, and the recipient clicked a
    link that could never work. A link we cannot stand behind is worse than no
    link, so this returns None and the caller omits the button entirely."""
    url = (os.getenv("APP_URL") or "").strip().rstrip("/")
    if not url or "localhost" in url or "127.0.0.1" in url:
        return None
    return url


def _compose_digest(display_name: str,
                    items: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    """Subject, HTML, text. Deterministic — no LLM.

    A worklist is a list of facts about the reader's own queue. Regenerating
    its wording per send would make two mornings' digests differ for reasons
    that have nothing to do with the work, and would put a model between a
    person and their own task list."""
    n = len(items)
    subject = f"Your worklist — {n} item{'s' if n != 1 else ''} need attention"
    console = _app_url()

    lines = [f"{display_name}, {n} item{'s' if n != 1 else ''} on your list:", ""]
    rows_html = []
    for it in items:
        title = str(it.get("title") or "").strip()
        body = " ".join(str(it.get("body") or "").split())[:160]
        lines.append(f"  • {title}")
        if body:
            lines.append(f"      {body}")
        rows_html.append(
            f"<tr><td style='padding:6px 0;border-bottom:1px solid #e5e7eb'>"
            f"<b>{html_escape(title)}</b>"
            + (f"<br><span style='color:#6b7280;font-size:0.9em'>"
               f"{html_escape(body)}</span>" if body else "")
            + "</td></tr>")

    tail = ("\nThis is a once-a-day summary. Urgent items are emailed "
            "separately when they happen.\n")
    if console:
        tail = f"\nOpen the console: {console}/agent-console.html\n" + tail
    body_text = "\n".join(lines) + "\n" + tail

    body_html = (
        f"<p>{html_escape(display_name)}, {n} item"
        f"{'s' if n != 1 else ''} on your list:</p>"
        "<table style='border-collapse:collapse;font-family:system-ui,sans-serif;"
        "font-size:0.95rem;width:100%'>" + "".join(rows_html) + "</table>"
        + (f"<p><a href='{console}/agent-console.html'>Open the console</a></p>"
           if console else "")
        + "<p style='color:#6b7280;font-size:0.85rem'>This is a once-a-day "
          "summary. Urgent items are emailed separately when they happen.</p>")
    return subject, body_html, body_text


def _deliver(to: str, subject: str, body_html: str,
             body_text: str) -> Dict[str, Any]:
    """THE ONLY PLACE THIS MODULE SENDS ANYTHING.

    Kept as a one-line wrapper on purpose: a single named function is something
    a test can assert about, and something a reviewer can find. `commercial` is
    left at its default — this is internal operational mail, and "unsubscribe
    from your own worklist" is not a choice this system should offer. The
    preference columns are the opt-out, and they are checked before we get
    here.
    """
    from app.agents.email.smtp_imap import send_email
    return send_email(to=to, subject=subject, body_html=body_html,
                      body_text=body_text, from_name="Conscestra Agent Ops")


def send_digest(owner_id: str, *, email: Optional[str] = None,
                display_name: str = "") -> Dict[str, Any]:
    """Build and (if warranted) send one person's daily worklist digest.

    Returns a dict that always says what happened and why — including, and
    especially, when nothing was sent.
    """
    out: Dict[str, Any] = {"owner_id": owner_id, "sent": False, "items": 0}

    items = digest_items(owner_id)
    out["items"] = len(items)
    day = _utc_day()

    # ── ONE DECISION PATH (F12) ─────────────────────────────────────────────
    # This used to re-implement the gates inline: preference, then address,
    # then budget. They were the same gates in the same order, so it was never
    # a safety defect — but it was a SECOND decision path, which is exactly the
    # drift this design fights everywhere else, and it meant the digest was
    # invisible to observations(). The Stage 4 exit criterion was being
    # measured from data the digest did not contribute to.
    #
    # observe() calls decide() and records the outcome, so the digest now
    # answers "why was this sent / not sent" the same way every other decision
    # does — including the empty-worklist refusal, which is now a counted
    # `nothing_actionable` rather than an early return nobody could see.
    d = observe(kind="digest", tier=TIER_WORKLIST, ref=f"{owner_id}:{day}",
                owner_id=owner_id, items=len(items))
    out["reason"] = d.get("reason") or d.get("error") or ""
    out["reason_class"] = d.get("reason_class")
    if not d.get("send"):
        return out

    # decide() resolved the address from MEMBERSHIP, not from anything the
    # caller passed. `email` survives only as a fallback for a directory row
    # mid-migration; it can no longer redirect a digest somewhere else.
    to = (d["recipient"] or {}).get("email") or (email or "").strip()
    if not to:
        out["reason"] = "no address on the authorized identity"
        return out

    subject, body_html, body_text = _compose_digest(
        display_name or to.split("@")[0], items)
    claim_info = begin_send(
        kind="digest", tier=TIER_WORKLIST, ref=f"{owner_id}:{day}",
        recipient_email=to, recipient_kind="assignable",
        recipient_owner_id=owner_id, subject=subject,
        subject_ref_type="digest",
        decision_reason=f"{len(items)} tier-2 items on {day}")

    # FAIL CLOSED. Stage 3 fails open because it wraps mail that was already
    # going out; this is new mail, and new mail we cannot record must not go.
    if not claim_info.get("recorded"):
        out["reason"] = f"not sent: {claim_info.get('why')}"
        return out
    if not claim_info.get("proceed"):
        out["reason"] = claim_info.get("why")
        return out

    if not applying():
        # Hand the lease back so the day's key stays claimable. Same posture as
        # order_notifications.release(): a deliberate non-attempt is not a
        # failure, and must not burn the idempotency key.
        release(claim_info["email_id"], "STAFF_EMAIL_APPLY=0 — composed, not sent")
        out["reason"] = "STAFF_EMAIL_APPLY=0 — composed, not sent"
        out["subject"] = subject
        return out

    result = _deliver(to, subject, body_html, body_text)
    outcome = finish_send(claim_info["email_id"], result)
    out["sent"] = outcome == "accepted"
    out["outcome"] = outcome
    out["reason"] = f"provider outcome: {outcome}"
    out["subject"] = subject
    return out


def run_digest() -> Dict[str, Any]:
    """The daily pass over everyone authorized to receive work. Today: four
    people. NEVER raises — one recipient's failure must not cost the others
    theirs."""
    if not enabled():
        return {"ok": True, "skipped": "STAFF_EMAIL_ENABLED=0"}
    try:
        from app.core import assignable
        people = assignable.directory()
    except Exception as exc:                                   # pragma: no cover
        logger.warning(f"[staff_email] digest: no directory: {exc}")
        return {"ok": False, "error": str(exc)[:200]}

    results = []
    for p in people:
        try:
            results.append(send_digest(
                p["owner_id"], email=p.get("email"),
                display_name=(p.get("display_name") or "").split(" ")[0]))
        except Exception as exc:                               # noqa: BLE001
            logger.warning(f"[staff_email] digest failed for "
                           f"{p.get('email')}: {exc}")
            results.append({"owner_id": p.get("owner_id"), "sent": False,
                            "reason": f"error: {str(exc)[:120]}"})
    sent = sum(1 for r in results if r.get("sent"))
    logger.info(f"[staff_email] digest pass: {sent} sent of {len(results)} "
                f"recipients (apply={applying()})")
    return {"ok": True, "recipients": len(results), "sent": sent,
            "applying": applying(), "results": results}


def _utc_day() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


# ============================================================================
# Status (admin, read-only) — a dark feature nobody can see is a dark feature
# nobody can verify.
# ============================================================================

router = APIRouter(tags=["staff-email"])


def status() -> Dict[str, Any]:
    counts = _one(
        """SELECT count(*),
                  count(*) FILTER (WHERE state='accepted'),
                  count(*) FILTER (WHERE state='skipped'),
                  count(*) FILTER (WHERE state IN ('failed','rejected'))
           FROM staff_email_ledger""") or (0, 0, 0, 0)
    rules = tier_rules()
    return {
        "ok": True,
        "stage": "4 — digest (dark unless STAFF_EMAIL_APPLY=1); sends only via _deliver",
        "enabled": enabled(),
        "applying": applying(),
        "role_mailbox": role_mailbox(),
        "ledger": {"rows": int(counts[0]), "accepted": int(counts[1]),
                   "skipped": int(counts[2]), "failed_or_rejected": int(counts[3])},
        "budget": budget(),
        "tier_rules": {"count": len(rules), "rules": rules},
        "migration": ("applied" if _one("SELECT 1 FROM information_schema.tables "
                                        "WHERE table_name='staff_email_ledger'")
                      else "run sql/staff_email_ledger.sql"),
        "migration_stage2": ("applied" if _one(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name='staff_email_observations'")
            else "run sql/staff_email_stage2.sql"),
    }


@router.get("/staff-email/status")
def api_status():
    return status()


@router.get("/staff-email/observations")
def api_observations(days: int = 7):
    """The Stage 2 exit report. Read-only; asks the ledger and the tier stamps
    what the last N days actually looked like."""
    return observations(days)


@router.post("/staff-email/digest-preview")
def api_digest_preview(body: Dict[str, Any]):
    """Compose one person's digest and return it WITHOUT sending or claiming.

    The point of a dark feature is that somebody can look at what it would do.
    Reading a rendered digest is how "is this useful?" — the Stage 4 exit
    criterion the code cannot answer for itself — gets answered by the person
    who would receive it."""
    owner_id = str((body or {}).get("owner_id") or "").strip()
    if not _UUID_RE.match(owner_id):
        return {"ok": False, "error": "owner_id must be a uuid"}
    items = digest_items(owner_id)
    if not items:
        return {"ok": True, "items": 0,
                "would_send": False, "reason": "nothing actionable"}
    subject, _html, text = _compose_digest(
        str((body or {}).get("display_name") or "there"), items)
    return {"ok": True, "items": len(items), "would_send": True,
            "subject": subject, "text": text}


@router.post("/staff-email/sync-tier-rules")
def api_sync_tier_rules():
    """Copy the Python taxonomy into the database so the notifications trigger
    can stamp it. Idempotent; safe to re-run after any edit to
    notification_triage's type sets."""
    return sync_tier_rules()


@router.post("/staff-email/explain")
def api_explain(body: Dict[str, Any]):
    """Ask the module to justify a decision WITHOUT taking it. No ledger row,
    no send — this is the observability surface Stage 2 reads."""
    b = body or {}
    return decide(kind=str(b.get("kind") or "escalation"),
                  tier=str(b.get("tier") or tier_of(b.get("event_type"))),
                  ref=str(b.get("ref") or "explain"),
                  assignee=b.get("assignee"),
                  executive_id=b.get("executive_id"),
                  ordinal=b.get("ordinal"))
