"""Executive Intelligence — the executive as a ROLE + intelligence profile on top
of the Person/Employee identity, not a duplicate person record.

    owners (Person / Employee)  ──employee_uuid──▶  executives (role + authority
                                                    + Executive Intelligence Profile)

WHY THIS SHAPE
  • Executives are still employees — the CEO needs an employment identity
    (permissions, org hierarchy, the notification feed). Linking to `owners`
    (the Person/Employee layer, which `notifications.employee_uuid` references)
    means no duplicate person record, and in-app notifications actually reach
    them (the gap that made approval notifications email-only).
  • The role/authority/profile is a layer ON TOP — a person can change roles,
    stop being an executive, or hold several roles, without a new identity.

THE PROFILE personalizes WHAT each executive needs to know and HOW they're
reached — authority domain, strategic priorities, preferred channel, briefing
hour, risk threshold, escalation level. The per-role briefings
(ceo_briefing.render_role) and approval routing already act role-by-role; this
makes that configurable per executive and links them to their employee identity.

  link_employees()  ensure every active executive has an employee (owner) — match
                    by email, else create one (is_synthetic=false). Idempotent.
  seed_profiles()   fill the intelligence profile from role defaults where unset
                    (never overwrites an edited value). Idempotent.

Requires sql/executive_intelligence.sql (profile columns) applied.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("executive_intelligence")


# Role → default Executive Intelligence Profile. Mirrors the perspectives the
# per-role briefings already produce (ceo_briefing._ROLE_CFG / _WEB_TOPICS).
_DEFAULT_PROFILES: Dict[str, Dict[str, Any]] = {
    "CEO": {"authority_domain": "all", "briefing_hour": 7,
            "risk_threshold": "high", "escalation_level": "critical",
            "strategic_priorities": ["Revenue growth", "Strategic accounts at risk",
                                     "Cash position", "Company-wide risk"]},
    "CFO": {"authority_domain": "financial", "briefing_hour": 7,
            "risk_threshold": "high", "escalation_level": "critical",
            "strategic_priorities": ["Cash flow", "Accounts receivable",
                                     "Profitability", "Collection risk"]},
    "CRO": {"authority_domain": "sales", "briefing_hour": 7,
            "risk_threshold": "medium", "escalation_level": "high",
            "strategic_priorities": ["Pipeline growth", "Win rate",
                                     "At-risk opportunities", "Conversion"]},
    "COO": {"authority_domain": "operations", "briefing_hour": 8,
            "risk_threshold": "medium", "escalation_level": "high",
            "strategic_priorities": ["Fulfillment", "Support escalations",
                                     "Process efficiency", "Unbilled orders"]},
}

_PROFILE_COLS = ("authority_domain", "strategic_priorities", "preferred_channel",
                 "briefing_hour", "risk_threshold", "escalation_level")


# ============================================================================
# Employee linkage — executive = role on an employee (owner) identity
# ============================================================================

def _split_name(full_name: str):
    parts = (full_name or "").strip().split()
    if not parts:
        return "Executive", ""
    return parts[0], " ".join(parts[1:])


def link_employees() -> Dict[str, Any]:
    """Ensure every active executive is linked to an employee (owner): match by
    email, else create the owner (is_synthetic=false) and link it. Idempotent —
    executives already linked are skipped."""
    linked, created, skipped = [], [], []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT executive_id::text, role_code, full_name, email, "
                        "employee_uuid::text FROM executives WHERE is_active")
            execs = [dict(zip([d[0] for d in cur.description], r))
                     for r in cur.fetchall()]
            for e in execs:
                if e["employee_uuid"]:
                    skipped.append(e["role_code"])
                    continue
                owner_id = None
                if e["email"]:
                    cur.execute("SELECT owner_id::text FROM owners "
                                "WHERE lower(email)=lower(%s) LIMIT 1", (e["email"],))
                    r = cur.fetchone()
                    owner_id = r[0] if r else None
                if not owner_id:
                    fn, ln = _split_name(e["full_name"])
                    cur.execute(
                        """INSERT INTO owners (owner_id, first_name, last_name, email,
                                               role, is_active, is_synthetic)
                           VALUES (gen_random_uuid(),%s,%s,%s,%s,true,false)
                           RETURNING owner_id::text""",
                        (fn, ln, e["email"], e["role_code"]))
                    owner_id = cur.fetchone()[0]
                    created.append(e["role_code"])
                cur.execute("UPDATE executives SET employee_uuid=%s::uuid, updated_at=now() "
                            "WHERE executive_id=%s::uuid", (owner_id, e["executive_id"]))
                linked.append({"role": e["role_code"], "employee_uuid": owner_id})
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[exec_intel] linked={len(linked)} created_owners={len(created)} "
                f"skipped={len(skipped)}")
    return {"linked": linked, "created_owners": created, "already_linked": skipped}


# ============================================================================
# Profiles
# ============================================================================

def seed_profiles() -> Dict[str, Any]:
    """Fill the intelligence profile from role defaults where a value is unset —
    never overwrites an edited profile. Idempotent."""
    updated = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT executive_id::text, role_code, authority_domain "
                        "FROM executives WHERE is_active")
            for eid, role, dom in cur.fetchall():
                if dom is not None:                       # already profiled
                    continue
                dflt = _DEFAULT_PROFILES.get((role or "").upper())
                if not dflt:
                    continue
                cur.execute(
                    """UPDATE executives SET
                         authority_domain=%s, strategic_priorities=%s::jsonb,
                         briefing_hour=%s, risk_threshold=%s, escalation_level=%s,
                         updated_at=now()
                       WHERE executive_id=%s::uuid""",
                    (dflt["authority_domain"], json.dumps(dflt["strategic_priorities"]),
                     dflt["briefing_hour"], dflt["risk_threshold"],
                     dflt["escalation_level"], eid))
                updated.append(role)
        conn.commit()
    finally:
        conn.close()
    return {"seeded": updated}


def profiles() -> List[Dict[str, Any]]:
    """Every active executive with their employee link + intelligence profile."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT role_code, full_name, email, employee_uuid::text,
                          authority_domain, strategic_priorities, preferred_channel,
                          briefing_hour, risk_threshold, escalation_level,
                          approval_authority_limit
                   FROM executives WHERE is_active ORDER BY role_code""")
            cols = [d[0] for d in cur.description]
            out = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["is_employee"] = bool(d.get("employee_uuid"))
                if d.get("approval_authority_limit") is not None:
                    d["approval_authority_limit"] = float(d["approval_authority_limit"])
                out.append(d)
            return out
    finally:
        conn.close()


def profile(role: str) -> Optional[Dict[str, Any]]:
    role = (role or "").upper()
    return next((p for p in profiles() if (p["role_code"] or "").upper() == role), None)


def update_profile(role: str, **fields) -> Dict[str, Any]:
    """Update editable profile fields for a role (partial). Returns the profile."""
    allowed = {k: v for k, v in fields.items() if k in _PROFILE_COLS and v is not None}
    if not allowed:
        return {"ok": False, "error": "no updatable fields"}
    sets, vals = [], []
    for k, v in allowed.items():
        if k == "strategic_priorities":
            sets.append(f"{k}=%s::jsonb")
            vals.append(json.dumps(v))
        else:
            sets.append(f"{k}=%s")
            vals.append(v)
    vals.append(role.upper())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE executives SET {', '.join(sets)}, updated_at=now() "
                        f"WHERE upper(role_code)=%s", vals)
            n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"ok": n > 0, "updated": list(allowed), "profile": profile(role)}


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["executive-intelligence"])


@router.get("/executive/profiles")
def executive_profiles():
    return {"profiles": profiles()}


@router.post("/executive/profiles/setup")
def executive_profiles_setup():
    """One-shot: link executives to employees + seed intelligence profiles.
    Idempotent — safe to re-run."""
    return {"link": link_employees(), "seed": seed_profiles(), "profiles": profiles()}


@router.post("/executive/profiles/{role}")
def executive_profile_update(role: str, body: Dict[str, Any]):
    return update_profile(role, **(body or {}))
