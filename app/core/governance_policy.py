"""Decision policy by action class — who decides, how, and by when.

    docs/governance/activation_plan.md §9 · baseline docs/architecture_assessment_2026-09-05.md

THE RULE THIS MODULE ENFORCES

    A numeric confidence threshold must never by itself convert an AI proposal
    into an autonomous business action.

Before this module, `governance.decide(confidence)` was the whole policy: a
write with confidence >= gov.act_min executed, and `gov.act_min` was an
editable row. The supervisor's auto-actions carried a hard-coded 0.75, so one
policy edit (act_min <= 0.75) would have silently turned every "propose" into
"execute" with no second gate. The assessment called that D-07.

Now every action type has an explicit row in `governance_action_policies`:

    decision_mode   HUMAN_APPROVAL | SAMPLED_REVIEW | AUTO_EXECUTE
    approver_role   CEO | CRO | CFO | CTO | COO      (D2 — the five authorities)
    escalation_role default CEO                      (D4)
    sla_hours       default 48                       (D3)
    auto_execute    true ONLY with a named policy_owner and a non-human mode

`may_auto_execute()` is the only function that can answer "yes" to executing a
write without a human, and it answers from the row, never from a score. A
missing row is HUMAN_APPROVAL routed to the CEO — fail closed, and visible as
status 'decision_required' so the gap is a queue item rather than a surprise.

OWNERSHIP. `resolve_accountable_owner(role)` maps an authority role to an
ELIGIBLE human owner (fn_owner_eligible, the E2 predicate). When the role has
no eligible executive the CEO owns it and the row is flagged
ownership_exception=true — a visible DATA gap (an executive without a
membership row), no longer a policy question. When even the CEO cannot be
resolved, the caller gets GovernanceConfigError and the write does not happen:
an unowned consequential action is the failure this whole activation exists to
remove.

BOUND IDENTITY. `session_authority(request)` answers which authority a request
is AUTHENTICATED as, and the decision endpoints refuse anything else. An
administrator may look at every desk and decide on none — see the section at
the foot of this module.

Requires governance/sql/governance_activation.sql. Reads only; the one writer
is `set_policy`, which is admin-gated and versioned by trigger.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.database import get_connection

logger = logging.getLogger("governance_policy")

HUMAN_APPROVAL = "HUMAN_APPROVAL"
SAMPLED_REVIEW = "SAMPLED_REVIEW"
AUTO_EXECUTE = "AUTO_EXECUTE"
DECISION_MODES = (HUMAN_APPROVAL, SAMPLED_REVIEW, AUTO_EXECUTE)

# D2 (REVISED 2026-09-05 by the owner — plan §26). FIVE authorities, not four.
#
# The first cut encoded four and, because no CTO executive existed, routed every
# technology/data class to the CEO as an ownership exception. The owner's
# decision replaces that: CTO and COO are first-class approval authorities, and
# the classes belong to them rather than being parked with the CEO.
#
#     CEO   enterprise / exceptional decisions · every escalation (D4)
#     CRO   revenue / commercial
#     CFO   financial / payment / accounting
#     CTO   technology / data / architecture / security
#     COO   operations / fulfilment / operational policy
#
# The database CHECK on governance_action_policies.approver_role says the same
# thing, so a sixth role cannot be introduced by application code alone.
AUTHORITY_ROLES = ("CEO", "CRO", "CFO", "CTO", "COO")
ESCALATION_ROLE = "CEO"          # D4
DEFAULT_SLA_HOURS = 48           # D3

_TTL = int(os.getenv("GOV_POLICY_TTL_SECS", "30"))
_cache: Dict[str, Any] = {"at": 0.0, "rows": {}, "available": None}


class GovernanceConfigError(RuntimeError):
    """The governance configuration cannot name an accountable human. Fail closed."""


def _rows(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _default_policy(action_type: str, kind: str = "action") -> Dict[str, Any]:
    """Fail-closed default: a human decides, the CEO is asked, 48 hours."""
    return {
        "action_type": action_type, "kind": kind, "action_class": "unclassified",
        "decision_mode": HUMAN_APPROVAL, "approver_role": ESCALATION_ROLE,
        "escalation_role": ESCALATION_ROLE, "sla_hours": DEFAULT_SLA_HOURS,
        "auto_execute": False, "policy_owner": None, "owner_required": True,
        "reversible": None, "sample_rate": None, "delegation_allowed": False,
        "policy_version": 0, "status": "decision_required", "notes": None,
        "daily_proposal_cap": None, "source": "default",
    }


def available() -> bool:
    """Is the policy table present? Cached with the rows."""
    _load()
    return bool(_cache["available"])


def _load() -> Dict[str, Dict[str, Any]]:
    if time.time() - _cache["at"] < _TTL:
        return _cache["rows"]
    rows: Dict[str, Dict[str, Any]] = {}
    avail = False
    try:
        for r in _rows("SELECT * FROM governance_action_policies"):
            r["source"] = "db"
            if r.get("sample_rate") is not None:
                r["sample_rate"] = float(r["sample_rate"])
            for k in ("updated_at", "created_at"):
                if r.get(k) is not None:
                    r[k] = r[k].isoformat()
            rows[r["action_type"]] = r
        avail = True
    except Exception as exc:
        logger.debug(f"[governance_policy] table unavailable: {str(exc)[:120]}")
    _cache.update(at=time.time(), rows=rows, available=avail)
    return rows


def invalidate_cache() -> None:
    _cache["at"] = 0.0


def policy_for(action_type: str) -> Dict[str, Any]:
    """The decision policy in force for a write capability or supervisor action."""
    return dict(_load().get(action_type) or _default_policy(action_type))


def alert_policy_for(rule: str) -> Dict[str, Any]:
    """Which authority owns a supervisor/platform rule, and its SLA."""
    return dict(_load().get(f"alert:{rule}") or _default_policy(f"alert:{rule}", "alert"))


def may_auto_execute(action_type: str,
                     policy: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """May this action execute WITHOUT a human decision? From the row only.

    Every 'no' names its reason, because the caller records it on the proposal
    so the approver sees why it is in front of them."""
    pol = policy or policy_for(action_type)
    if pol.get("source") != "db":
        return False, "no decision policy declared for this action type (fail closed)"
    if pol.get("decision_mode") == HUMAN_APPROVAL:
        return False, f"policy {pol['decision_mode']} v{pol.get('policy_version')}"
    if not pol.get("auto_execute"):
        return False, (f"policy {pol['decision_mode']} v{pol.get('policy_version')} "
                       f"without auto_execute")
    if not pol.get("policy_owner"):
        return False, "auto_execute without a named policy_owner (refused)"
    return True, (f"policy {pol['decision_mode']} v{pol.get('policy_version')} "
                  f"owned by {pol['policy_owner']}")


# ── Authority → accountable human ────────────────────────────────────────────

def authorities() -> List[Dict[str, Any]]:
    """Active executives holding one of the five authority roles, with the
    owner id that governance work is attributed to and whether it is eligible.

    Also reports `identity_mismatch`: whether the name on the authority row and
    the name on the owner row it points at describe the same person. See the
    SQL comment below for why that is not a cosmetic check."""
    try:
        rows = _rows(
            """SELECT e.executive_id::text AS executive_id, e.role_code, e.full_name,
                      lower(e.email) AS email,
                      COALESCE(e.owner_id, e.employee_uuid)::text AS owner_id,
                      fn_owner_eligible(COALESCE(e.owner_id, e.employee_uuid)) AS eligible,
                      -- §26.7: an authority must be able to SIGN IN AS ITSELF.
                      -- Without a credential its decisions cannot be bound to a
                      -- session, and the console must say so rather than let an
                      -- administrator pick the role and act on its behalf.
                      EXISTS (SELECT 1 FROM auth_credentials c
                               WHERE lower(c.identifier) = lower(e.email)
                                 AND COALESCE(c.is_active, true)) AS has_credential,
                      -- WHO THE OWNER ROW ACTUALLY IS. The authority row carries
                      -- a display name; the owner row carries the identity that
                      -- every decision is stamped with. Nothing kept them
                      -- describing the same person, and on 2026-09-05 they
                      -- stopped: the COO was renamed and the edit reached
                      -- `executives.full_name` only, leaving the owner row and
                      -- the directory membership on the old name.
                      -- `resolve_accountable_owner` then reported the label
                      -- "COO Alex Zhou" over a row reading Yongmei Qin, with
                      -- exception=false and eligible=true, and nothing was
                      -- flagged: the role had an eligible owner and a name.
                      --
                      -- That instance was harmless, because it was one person.
                      -- The identical symptom is produced by an office that has
                      -- genuinely changed hands, where the incoming officer's
                      -- approvals are attributed to their predecessor under a
                      -- label that looks correct on every screen. This does not
                      -- try to tell the two apart; it exists so the system stops
                      -- asserting that a name and an identity agree when they do
                      -- not, and a person decides which case it is. A divergence
                      -- here is how an audit trail lies while every row in it
                      -- looks fine.
                      trim(concat_ws(' ', o.first_name, o.last_name)) AS owner_name,
                      lower(o.email) AS owner_email,
                      e.preferred_channel, e.auto_email_enabled, e.employee_uuid::text
                 FROM executives e
                 LEFT JOIN owners o
                        ON o.owner_id = COALESCE(e.owner_id, e.employee_uuid)
                WHERE e.is_active AND e.role_code = ANY(%s)
                ORDER BY array_position(%s::text[], e.role_code)""",
            (list(AUTHORITY_ROLES), list(AUTHORITY_ROLES)))
    except Exception as exc:
        logger.warning(f"[governance_policy] authorities unavailable: {str(exc)[:140]}")
        return []
    for r in rows:
        r["identity_mismatch"] = _identity_mismatch(r)
    return rows


def _identity_mismatch(row: Dict[str, Any]) -> Optional[str]:
    """Do the authority row and its owner row name the same person?

    Returns a sentence describing the divergence, or None. Compared on a
    normalised name rather than on equality of the raw strings, so a change of
    case or spacing is not reported as a change of person — the question is
    "is this somebody else", not "was this edited"."""
    owner_name = (row.get("owner_name") or "").strip()
    if not row.get("owner_id") or not owner_name:
        return None                      # no owner linked: a different defect

    def norm(v: str) -> str:
        return " ".join((v or "").lower().split())

    if norm(owner_name) == norm(row.get("full_name") or ""):
        return None
    return (f"authority row says {row.get('full_name')!r} but the owner it is "
            f"attributed to ({row['owner_id']}) is {owner_name!r}"
            + (f" <{row['owner_email']}>" if row.get("owner_email") else "")
            + ". Decisions made under this authority would be stamped with the "
              "second person's identity.")


def authority_owner(role: str) -> Optional[Dict[str, Any]]:
    """The eligible executive owner for one authority role, or None."""
    for a in authorities():
        if a["role_code"] == role and a.get("eligible") and a.get("owner_id"):
            return a
    return None


def resolve_accountable_owner(role: Optional[str]) -> Dict[str, Any]:
    """Name the human accountable for work routed to `role`.

    Returns {owner_id, label, role, exception}. `exception` is true when the
    requested role had no eligible executive and the CEO holds it instead.
    Raises GovernanceConfigError when nobody eligible exists at all."""
    role = (role or ESCALATION_ROLE).upper()
    a = authority_owner(role)
    if a:
        return {"owner_id": a["owner_id"], "label": f"{a['role_code']} {a['full_name']}",
                "role": a["role_code"], "email": a.get("email"), "exception": False}
    ceo = authority_owner(ESCALATION_ROLE)
    if ceo:
        return {"owner_id": ceo["owner_id"], "label": f"{ceo['role_code']} {ceo['full_name']}",
                "role": ceo["role_code"], "email": ceo.get("email"),
                "exception": role != ESCALATION_ROLE,
                "requested_role": role}
    raise GovernanceConfigError(
        f"no eligible executive holds role {role} and no eligible CEO exists — "
        f"governance work cannot be owned; refusing (D4 requires a CEO)")


def decider_role(decided_by: Optional[str]) -> Optional[str]:
    """Resolve a decider identifier to an authority role, or None.

    Accepts an executive email (the console sends the session's email or the
    chosen authority's email) or a bare role code. Anything else is not an
    approval authority and is refused by the caller."""
    d = (decided_by or "").strip().lower()
    if not d:
        return None
    if d.upper() in AUTHORITY_ROLES:
        return d.upper()
    for a in authorities():
        if a.get("email") == d:
            return a["role_code"]
    return None


# ── Admin surface ────────────────────────────────────────────────────────────

def list_policies() -> Dict[str, Any]:
    rows = _load()
    actions = sorted((r for r in rows.values() if r.get("kind") == "action"),
                     key=lambda r: (r["action_class"], r["action_type"]))
    alerts = sorted((r for r in rows.values() if r.get("kind") == "alert"),
                    key=lambda r: (r["action_class"], r["action_type"]))
    # Write capabilities with no policy row are the gap the queue must show.
    undeclared: List[str] = []
    try:
        from app.core.a2a import CAPABILITIES
        undeclared = sorted(c.intent for c in CAPABILITIES.values()
                            if c.kind == "write" and c.intent not in rows)
    except Exception:
        pass
    return {"available": bool(_cache["available"]), "authorities": AUTHORITY_ROLES,
            "decision_modes": DECISION_MODES,
            "actions": actions, "alerts": alerts,
            "undeclared_write_capabilities": undeclared,
            "decision_required": [r["action_type"] for r in rows.values()
                                  if r.get("status") == "decision_required"]}


_EDITABLE = {"decision_mode", "approver_role", "escalation_role", "sla_hours",
             "auto_execute", "policy_owner", "reversible", "sample_rate",
             "delegation_allowed", "status", "notes", "action_class",
             # How much of ONE executive's day this class may consume. NULL is
             # uncapped; the database refuses a cap on an AUTO_EXECUTE class,
             # which consumes no attention at all.
             "daily_proposal_cap"}


def set_policy(action_type: str, changes: Dict[str, Any], updated_by: str,
               reason: str) -> Dict[str, Any]:
    """Change a policy row. Versioned and historied by trigger; the reason is
    mandatory because widening authority without saying why is the failure
    mode. Creates the row when absent (a declaration, not a repair)."""
    if not (reason or "").strip():
        raise ValueError("a reason is required to change a decision policy")
    fields = {k: v for k, v in (changes or {}).items() if k in _EDITABLE}
    if not fields:
        raise ValueError(f"nothing to change; editable: {sorted(_EDITABLE)}")
    if "decision_mode" in fields and fields["decision_mode"] not in DECISION_MODES:
        raise ValueError(f"decision_mode must be one of {DECISION_MODES}")
    for k in ("approver_role", "escalation_role"):
        if k in fields and str(fields[k]).upper() not in AUTHORITY_ROLES:
            raise ValueError(f"{k} must be one of {AUTHORITY_ROLES}")
        if k in fields:
            fields[k] = str(fields[k]).upper()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM governance_action_policies WHERE action_type=%s",
                        (action_type,))
            exists = cur.fetchone() is not None
            note_suffix = f"\n[{updated_by} — {reason.strip()}]"
            if exists:
                sets = ", ".join(f"{k}=%({k})s" for k in fields)
                params = dict(fields, action_type=action_type, by=updated_by,
                              suffix=note_suffix)
                cur.execute(
                    f"""UPDATE governance_action_policies
                           SET {sets}, updated_by=%(by)s,
                               notes = COALESCE(notes,'') || %(suffix)s
                         WHERE action_type=%(action_type)s""", params)
            else:
                base = _default_policy(action_type)
                base.update(fields)
                base["action_class"] = fields.get("action_class") or "unclassified"
                cur.execute(
                    """INSERT INTO governance_action_policies
                         (action_type, kind, action_class, decision_mode, approver_role,
                          escalation_role, sla_hours, auto_execute, policy_owner,
                          owner_required, reversible, sample_rate, delegation_allowed,
                          status, notes, updated_by)
                       VALUES (%(action_type)s, 'action', %(action_class)s, %(decision_mode)s,
                               %(approver_role)s, %(escalation_role)s, %(sla_hours)s,
                               %(auto_execute)s, %(policy_owner)s, %(owner_required)s,
                               %(reversible)s, %(sample_rate)s, %(delegation_allowed)s,
                               %(status)s, %(notes)s, %(by)s)""",
                    dict(base, by=updated_by,
                         status=fields.get("status") or "active",
                         notes=(fields.get("notes") or "") + note_suffix))
            try:
                cur.execute(
                    "SELECT emit_event('governance.policy_changed','policy',%s,%s::jsonb,NULL,'governance')",
                    ("00000000-0000-0000-0000-000000000000",
                     json.dumps({"context": {"action_type": action_type,
                                             "changes": fields, "by": updated_by,
                                             "reason": reason.strip()}})))
            except Exception as exc:                          # noqa: BLE001
                logger.debug(f"[governance_policy] change event skipped: {exc}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    invalidate_cache()
    logger.info(f"[governance_policy] {action_type} changed by {updated_by}: {fields} ({reason})")
    return policy_for(action_type)


def owner_eligibility(owner_id: str) -> Dict[str, Any]:
    """Explain the E2 verdict for one uuid — the read-side companion to the trigger."""
    try:
        r = _rows(
            """SELECT fn_owner_eligible(%(o)s::uuid) AS eligible,
                      EXISTS (SELECT 1 FROM assignable_identity a WHERE a.owner_id=%(o)s::uuid AND COALESCE(a.is_active,true)) AS has_membership,
                      EXISTS (SELECT 1 FROM contacts c WHERE c.contact_id=%(o)s::uuid) AS is_customer_contact,
                      EXISTS (SELECT 1 FROM employees e WHERE e.employee_uuid=%(o)s::uuid
                              AND (lower(COALESCE(e.role,''))='agent' OR lower(COALESCE(e.email,'')) LIKE '%%@system.internal')) AS is_service_identity,
                      EXISTS (SELECT 1 FROM employees e JOIN owners o ON o.owner_id=e.employee_uuid
                              WHERE e.employee_uuid=%(o)s::uuid AND lower(COALESCE(o.email,''))<>lower(COALESCE(e.email,''))) AS identity_collision""",
            {"o": owner_id})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"not a valid owner id: {str(exc)[:120]}")
    return {"owner_id": owner_id, **(r[0] if r else {})}


# ============================================================================
# BOUND IDENTITY — a decision belongs to the person who is signed in
# ============================================================================
#
# THE RULE (plan §6 / §26.7, owner decision 2026-09-05):
#
#     No consequential approval is attributable to an executive merely because
#     an administrator selected that executive's authority in a console. The
#     approval must be bound to the authenticated executive principal, or
#     explicitly recorded as delegated authority with BOTH the actual actor and
#     the represented authority.
#
# `session_authority()` answers "which authority is this request AUTHENTICATED
# as" from the session `require_admin` already validated and stamped on
# request.state. A machine token, or an admin session that is not an executive,
# resolves to None — and the decision endpoints refuse rather than recording a
# decision against someone who did not make it.
#
# The ONE non-session path that survives is the HMAC decision link, and it is
# not an exception to the rule: the token was mailed to that authority's own
# mailbox, so possession is the authentication, and the row records
# decided_actor='email-link' so nobody can later read it as a person at a
# keyboard.

def executive_for_identifier(identifier: Optional[str]) -> Optional[Dict[str, Any]]:
    """The active executive whose email is `identifier`, or None."""
    ident = (identifier or "").strip().lower()
    if not ident:
        return None
    for a in authorities():
        if a.get("email") == ident:
            return a
    return None


def session_authority(request) -> Optional[Dict[str, Any]]:
    """The executive an HTTP request is signed in as, or None.

    Reads request.state.session (set by require_admin / require_session). The
    session's `identifier` is the sign-in email and must equal an active
    executive's. Nothing here trusts a request body."""
    sess = getattr(getattr(request, "state", None), "session", None)
    if not sess:
        return None
    return executive_for_identifier(sess.get("identifier") or sess.get("email"))


def email_authority(exec_row: Optional[Dict[str, Any]], subject: str, body_text: str,
                    *, kind: str, ref: str,
                    body_html: Optional[str] = None) -> Dict[str, Any]:
    """Email an executive about a governance event — a breached approval, an
    escalation, an alert now theirs (§26.6: email immediately; a real paging
    channel afterwards).

    Gated exactly like routed-approval mail — GOV_ROUTE_EMAIL and the
    executive's own auto_email_enabled — and ledgered through staff_email so the
    send is idempotent per (kind, ref) and its provider outcome is recorded.
    FAIL-OPEN on the bookkeeping, never on the address: an executive must not
    miss an escalation because an audit table was missing."""
    if not _flag_env("GOV_ROUTE_EMAIL"):
        return {"sent": False, "why": "GOV_ROUTE_EMAIL off"}
    if not exec_row or not exec_row.get("email") or not exec_row.get("auto_email_enabled"):
        return {"sent": False, "why": "no email on file, or auto_email disabled"}
    html = body_html or (
        "<pre style=\"font-family:Arial,Helvetica,sans-serif;white-space:pre-wrap\">"
        + body_text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>")
    claim: Dict[str, Any] = {"proceed": True, "email_id": None}
    try:
        from app.core import staff_email
        claim = staff_email.begin_send(
            kind=kind, tier=staff_email.TIER_INTERRUPT, ref=ref,
            recipient_email=exec_row["email"], recipient_kind="executive",
            recipient_owner_id=exec_row.get("owner_id"), subject=subject,
            subject_ref_type="approval", subject_ref_id=ref,
            decision_reason=f"{kind} -> {exec_row.get('role_code')}")
    except Exception as exc:                                       # noqa: BLE001
        logger.debug(f"[governance_policy] staff-email claim skipped: {exc}")
    if not claim.get("proceed"):
        return {"sent": False, "why": claim.get("why") or "already sent (ledger)"}
    try:
        from app.agents.email.smtp_imap import send_email
        res = send_email(to=exec_row["email"], subject=subject,
                         body_html=html, body_text=body_text)
    except Exception as exc:                                       # noqa: BLE001
        logger.warning(f"[governance_policy] escalation email to "
                       f"{exec_row.get('role_code')} failed: {exc}")
        res = {"success": False, "error": str(exc)[:200]}
    try:
        from app.core import staff_email
        staff_email.finish_send(claim.get("email_id"), res)
    except Exception as exc:                                       # noqa: BLE001
        logger.debug(f"[governance_policy] staff-email outcome skipped: {exc}")
    return {"sent": bool((res or {}).get("success", False)), "result": res}


def _flag_env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


router = APIRouter(tags=["governance-policy"])


@router.get("/governance/whoami")
def api_whoami(request: Request):
    """Which authority this session is signed in as.

    The console binds every decision to this answer: an administrator who is
    not an executive may look at every desk and decide on none."""
    ex = session_authority(request)
    sess = getattr(getattr(request, "state", None), "session", None) or {}
    return {
        "signed_in_as": sess.get("identifier"),
        "executive": ({"role_code": ex["role_code"], "full_name": ex["full_name"],
                       "email": ex["email"], "eligible": ex.get("eligible"),
                       "has_credential": ex.get("has_credential")} if ex else None),
        "can_decide": bool(ex and ex.get("eligible")),
        "why_not": (None if (ex and ex.get("eligible")) else
                    (f"{ex['role_code']} is signed in but is not an eligible owner "
                     f"(no active membership) — grant it before deciding." if ex else
                     "this session is not one of the approval authorities "
                     f"({', '.join(AUTHORITY_ROLES)}); sign in with the executive's "
                     "own credential")),
    }


@router.get("/governance/action-policies")
def api_list_policies():
    return list_policies()


@router.get("/governance/authorities")
def api_authorities():
    rows = authorities()
    have = {a["role_code"] for a in rows if a.get("eligible")}
    return {"authorities": rows,
            "required": AUTHORITY_ROLES,
            "missing": [r for r in AUTHORITY_ROLES if r not in have],
            # An authority with no sign-in credential cannot decide anything
            # under the bound-identity rule. Reported so the gap is a task, not
            # a mystery 403.
            "without_credential": [a["role_code"] for a in rows
                                   if not a.get("has_credential")],
            # An authority whose display name and owner identity name DIFFERENT
            # PEOPLE. Summarised alongside the other two gaps because it is the
            # least visible of the three: a missing owner or a missing
            # credential stops something working, while this one keeps working
            # and attributes the decision to the wrong human under a label that
            # looks right. Listed so it is a task rather than a discovery.
            "identity_mismatch": [{"role_code": a["role_code"],
                                   "detail": a["identity_mismatch"]}
                                  for a in rows if a.get("identity_mismatch")],
            "escalation_role": ESCALATION_ROLE}


@router.get("/governance/owner-eligibility/{owner_id}")
def api_owner_eligibility(owner_id: str):
    return owner_eligibility(owner_id)


class _PolicyChange(BaseModel):
    changes: Dict[str, Any]
    updated_by: str
    reason: str


@router.put("/governance/action-policies/{action_type}")
def api_set_policy(action_type: str, body: _PolicyChange):
    try:
        return {"ok": True, "policy": set_policy(action_type, body.changes,
                                                 body.updated_by, body.reason)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        # A CHECK violation (e.g. auto_execute without an owner) surfaces as a
        # refusal with the constraint's message, not a 500.
        raise HTTPException(status_code=409, detail=str(exc).splitlines()[0][:300])
