"""Assignable identity — C2.0 (Axis 5).

    Who is allowed to receive CRM work?

Nothing in this database could answer that. `owners` is the FK target for every
`owner_id` column but is 90% customer contacts; `employees` is demo seed data
(on @emp.agentorc.ca since 2026-08-20, previously @company.com) abandoned in
January; `auth_credentials` authenticates customers.
So assignability is EXPLICIT MEMBERSHIP, never inference — see
sql/assignable_identity.sql for the full evidence.

WHAT THIS MODULE DOES

    resolve(email)      lookup only. NEVER provisions. Returns the validated
                        owner_id assignment already uses, so C1's mutation
                        contract is untouched.
    identity_space(id)  'assignable' | 'legacy_owner' | 'demo_employee' |
                        'customer_contact' | 'unresolvable' — turns a bare uuid
                        into a statement about WHERE it came from.
    inventory()         the unresolved population, reported rather than guessed.
    grant() / revoke()  provisioning, as a DELIBERATE and separate operation.

THE SEPARATION THAT MATTERS: routing resolves membership; an admin grants it.
A worker must never be created as a side effect of routing, which is why
resolve() cannot write and grant() is not called from anywhere automatic.

CONFIG (env)
  ASSIGNABLE_STRICT  0  when 1, assignment resolution requires membership.
                        Default OFF so C1 behaviour is byte-identical until the
                        directory is actually populated — with four rows and
                        one of them the CEO, strict mode today would refuse
                        nearly every assignment.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from app.core.database import get_connection

logger = logging.getLogger("assignable")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


STRICT = _flag("ASSIGNABLE_STRICT", "0")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


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
        logger.debug(f"[assignable] query failed: {exc}")
        return []
    finally:
        if conn is not None:
            conn.close()


def available() -> bool:
    """Whether the C2.0 migration is applied. Everything degrades to today's
    behaviour when it is not, so a Railway database without it is unaffected."""
    return bool(_rows("SELECT to_regclass('public.assignable_identity') AS t")
                and _rows("SELECT to_regclass('public.assignable_identity') AS t"
                          )[0]["t"])


# ============================================================================
# RESOLUTION — lookup only
# ============================================================================

def resolve(identifier: Optional[str]) -> Optional[str]:
    """An email (or an owner uuid) -> the validated owner_id, if that person is
    assignable. None otherwise.

    NEVER creates anything. A router that could provision a worker would mean
    an unrecognised name silently becomes an employee, which is how identity
    directories rot. Provisioning is grant(), and nothing calls it
    automatically.

    A display name is never accepted: two people share one, and the console
    supplies literals like "agent".
    """
    raw = (identifier or "").strip()
    if not raw or "@" not in raw and not _UUID_RE.match(raw):
        return None
    if _UUID_RE.match(raw):
        rows = _rows("""SELECT owner_id::text FROM assignable_identity
                        WHERE owner_id = %s::uuid AND is_active""", (raw,))
    else:
        rows = _rows("""SELECT owner_id::text FROM assignable_identity
                        WHERE lower(email) = lower(%s) AND is_active""", (raw,))
    return rows[0]["owner_id"] if rows and rows[0]["owner_id"] else None


def is_assignable(identifier: Optional[str]) -> bool:
    return resolve(identifier) is not None


def directory(include_inactive: bool = False) -> List[Dict[str, Any]]:
    """Everyone who may receive work. Today: four people."""
    where = "" if include_inactive else "WHERE is_active"
    return _rows(f"""SELECT assignable_id::text, email, owner_id::text,
                            source, source_ref, display_name, is_active,
                            languages, skills, added_by, added_at
                     FROM assignable_identity {where}
                     ORDER BY display_name, email""")


# ============================================================================
# CLASSIFICATION — what IS this uuid?
# ============================================================================

def identity_space(candidate: Optional[str]) -> Dict[str, Any]:
    """Where a bare owner_id value actually comes from.

    `owner_id` is polymorphic across three identity spaces by history:
    6,255 activity rows point into `owners`, 769 into `employees`, 598 into
    neither. A foreign key cannot express that and would fail; this can, and it
    turns "invalid" into a statement about provenance — which is what the
    inventory step needs before anything is repaired.
    """
    raw = (candidate or "").strip()
    if not _UUID_RE.match(raw):
        return {"space": "unresolvable", "spaces": [], "reason": "not a uuid"}

    # EVERY space is checked, not the first that hits.
    #
    # The identity spaces COLLIDE. `a1451ad6-310c-4bcc-ba17-dd383a881ee8` is
    # julia.martin@emp.agentorc.ca in `employees` AND john.smith@example.com in
    # `owners` — one uuid, two different people — and it is the owner of all
    # 120 historical cases. A first-match-wins chain would answer confidently
    # with whichever table it happened to try first, which is precisely the
    # class of silent wrongness C2.0 exists to end.
    found: List[Dict[str, Any]] = []

    for row in _rows("""SELECT email, display_name FROM assignable_identity
                        WHERE owner_id = %s::uuid AND is_active""", (raw,)):
        found.append({"space": "assignable", "email": row["email"],
                      "who": row["display_name"]})

    # Is this owner actually a CUSTOMER contact? Two independent signals, OR'd.
    #
    # The shared PRIMARY KEY is the authoritative one: `contact_id = owner_id`
    # means the two tables are describing one identity, and it cannot drift
    # because it IS the identity. Email equality was the original — and only —
    # signal, and it silently went to zero when the synthetic contact addresses
    # were migrated to @seed.agentorc.ca while `owners` kept theirs: 39 real
    # customer contacts re-labelled `legacy_owner` overnight, with no error.
    # Deriving an identity from a MUTABLE ATTRIBUTE is the defect; the email
    # check is kept only as a widening secondary signal.
    #
    # OR is deliberate and fail-safe. `customer_contact` is the cautious answer
    # — it marks someone as an outsider who must not be routed work — so a
    # false positive costs a missed assignment, while a false negative would
    # present a customer as ex-staff. Widening can only err toward caution.
    for row in _rows("""SELECT o.email, o.first_name, o.last_name,
                          EXISTS (SELECT 1 FROM contacts c
                                  WHERE c.contact_id = o.owner_id)
                            AS contact_by_id,
                          EXISTS (SELECT 1 FROM contacts c
                                  WHERE lower(c.email) = lower(o.email))
                            AS contact_by_email
                        FROM owners o WHERE o.owner_id = %s::uuid""", (raw,)):
        signals = [s for s, hit in (("shared contact_id", row["contact_by_id"]),
                                    ("matching email", row["contact_by_email"]))
                   if hit]
        found.append({
            "space": "customer_contact" if signals else "legacy_owner",
            "email": row["email"],
            "who": f"{row['first_name'] or ''} {row['last_name'] or ''}".strip(),
            "evidence": signals,
            "reason": (f"in `owners`, but is a customer contact "
                       f"({' + '.join(signals)}); not granted assignability"
                       if signals else
                       "in `owners`; not granted assignability")})

    for row in _rows("""SELECT email, employee_name FROM employees
                        WHERE employee_uuid = %s::uuid""", (raw,)):
        found.append({"space": "demo_employee", "email": row["email"],
                      "who": row["employee_name"],
                      "reason": "in `employees` (demo seed cohort); "
                                "never granted"})

    if not found:
        return {"space": "unresolvable", "spaces": [],
                "reason": "not in assignable_identity, owners or employees"}

    emails = {f.get("email", "").lower() for f in found}
    collision = len(found) > 1 and len(emails) > 1
    return {
        # Assignability wins as the PRIMARY answer when present — it is the
        # only space that was ever deliberately granted.
        "space": found[0]["space"],
        "spaces": found,
        "collision": collision,
        "reason": ("this uuid identifies DIFFERENT people in different tables "
                   f"({', '.join(sorted(e for e in emails if e))}) — it cannot "
                   "be treated as one identity"
                   if collision else found[0].get("reason", "")),
    }


# ============================================================================
# INVENTORY — report the gap, never guess it
# ============================================================================

def inventory() -> Dict[str, Any]:
    """The unresolved population, so the directory's incompleteness is VISIBLE.

    This is the honest answer to "who works here?": four people are declared,
    everyone else is a candidate somebody has to decide about. Nothing here
    promotes anyone — the whole point of C2.0 is that promotion is a deliberate
    act."""
    out: Dict[str, Any] = {"ok": True}
    out["assignable"] = len(directory())
    out["unlinked_members"] = _rows(
        """SELECT email, display_name, source FROM assignable_identity
           WHERE is_active AND owner_id IS NULL""")

    out["candidates"] = {
        "executives_not_granted": _rows(
            """SELECT e.email, e.full_name FROM executives e
               WHERE e.is_active AND NOT EXISTS (
                 SELECT 1 FROM assignable_identity a
                 WHERE lower(a.email) = lower(e.email))"""),
        "employees_not_granted": _rows(
            """SELECT e.email, e.employee_name, e.department, e.job_title
               FROM employees e
               WHERE coalesce(e.is_active, true) AND NOT EXISTS (
                 SELECT 1 FROM assignable_identity a
                 WHERE lower(a.email) = lower(e.email))
               ORDER BY e.department NULLS LAST, e.email"""),
    }
    out["note"] = (
        "employees_not_granted is the DEMO SEED cohort (moved to @emp.agentorc.ca 2026-08-20, formerly @company.com; created "
        "2025-12-14 / 2026-01-07). It is listed as candidates, not imported — "
        "promoting seed data to 'may receive work' is the false inference this "
        "model exists to prevent.")
    return out


def owner_id_provenance(table: str = "activities") -> Dict[str, Any]:
    """Classify an owner-bearing table's owner_id values by identity space.

    The 598 activity rows that resolve to nothing are QUARANTINED here — named
    and counted, never rewritten. Historical ownership evidence is not
    destroyed to satisfy a constraint that does not exist yet."""
    if table not in ("activities", "cases", "orders", "leads"):
        return {"ok": False, "error": f"table {table!r} not inventoried"}
    rows = _rows(f"""
        SELECT
          count(*) FILTER (WHERE a.owner_id IS NULL)              AS unowned,
          count(*) FILTER (WHERE ai.owner_id IS NOT NULL)         AS assignable,
          count(*) FILTER (WHERE ai.owner_id IS NULL
                             AND o.owner_id IS NOT NULL)          AS legacy_owner,
          count(*) FILTER (WHERE ai.owner_id IS NULL
                             AND o.owner_id IS NULL
                             AND e.employee_uuid IS NOT NULL)     AS demo_employee,
          count(*) FILTER (WHERE a.owner_id IS NOT NULL
                             AND o.owner_id IS NULL
                             AND e.employee_uuid IS NULL)         AS unresolvable,
          count(*)                                                AS total
        FROM {table} a
        LEFT JOIN assignable_identity ai
               ON ai.owner_id = a.owner_id AND ai.is_active
        LEFT JOIN owners o    ON o.owner_id = a.owner_id
        LEFT JOIN employees e ON e.employee_uuid = a.owner_id""")
    if not rows:
        return {"ok": False, "error": "inventory query failed"}
    out = {"ok": True, "table": table, **{k: int(v or 0)
                                          for k, v in rows[0].items()}}
    out["note"] = ("Counts only. Nothing is rewritten: an owner_id pointing at "
                   "a legacy or demo identity is historical evidence of who "
                   "held the work, and destroying it to satisfy a future "
                   "foreign key would be worse than the inconsistency.")
    return out


# ============================================================================
# PROVISIONING — deliberate, and separate from resolution
# ============================================================================

def grant(email: str, *, owner_id: Optional[str] = None,
          display_name: str = "", source: str = "manual",
          source_ref: str = "", added_by: str = "admin") -> Dict[str, Any]:
    """Declare that a person may receive work. An ADMIN ACT.

    Never called from routing, assignment or any agent path — a worker created
    as a side effect of routing is how a directory silently fills with people
    nobody decided on."""
    email = (email or "").strip()
    if "@" not in email:
        return {"ok": False, "error": "an email is required — a display name "
                                      "is not an identity"}
    if owner_id and not _UUID_RE.match(owner_id):
        return {"ok": False, "error": "owner_id must be a uuid"}
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO assignable_identity
                     (email, owner_id, source, source_ref, display_name, added_by)
                   VALUES (%s, %s::uuid, %s, %s, %s, %s)
                   ON CONFLICT (lower(email)) DO UPDATE
                     SET is_active = true,
                         owner_id = COALESCE(EXCLUDED.owner_id,
                                             assignable_identity.owner_id),
                         updated_at = now()
                   RETURNING assignable_id::text, owner_id::text""",
                (email, owner_id, source, source_ref or None,
                 display_name or None, added_by))
            r = cur.fetchone()
        conn.commit()
        logger.info(f"[assignable] granted {email} by {added_by}")
        return {"ok": True, "assignable_id": r[0], "owner_id": r[1],
                "linked": r[1] is not None}
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        if conn is not None:
            conn.close()


def set_attributes(email: str, *, languages=None, skills=None,
                   by: str = "admin") -> Dict[str, Any]:
    """Record what a person can actually work in — a CURATED fact, not a guess.

    NULL stays NULL: passing None leaves the attribute alone rather than
    clearing it, and an unrecorded language means UNKNOWN. Nothing infers
    language from a name, a domain or a job title, because a routing engine
    that guessed would send a French caller to somebody who cannot help
    them."""
    email = (email or "").strip()
    if "@" not in email:
        return {"ok": False, "error": "an email is required"}
    sets, args = [], []
    if languages is not None:
        sets.append("languages=%s")
        args.append([str(x).strip().lower() for x in languages if str(x).strip()]
                    or None)
    if skills is not None:
        sets.append("skills=%s")
        args.append([str(x).strip().lower() for x in skills if str(x).strip()]
                    or None)
    if not sets:
        return {"ok": False, "error": "nothing to set"}
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(f"""UPDATE assignable_identity
                            SET {', '.join(sets)}, updated_at=now()
                            WHERE lower(email)=lower(%s)""", tuple(args) + (email,))
            n = cur.rowcount
        conn.commit()
        logger.info(f"[assignable] attributes updated for {email} by {by}")
        return {"ok": bool(n), "updated": n,
                "error": None if n else f"{email} is not in the directory"}
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        if conn is not None:
            conn.close()


def provision_owner(email: str, *, display_name: str = "",
                    by: str = "admin") -> Dict[str, Any]:
    """Create the CRM owner identity a person needs in order to hold work.

    A DELIBERATE, SEPARATE ACT — never reached from routing or assignment.

    It exists because of a concrete gap: the only real administrator
    (admin@conscestra.local) is a LEAD-sourced auth credential and appears in
    no CRM identity table at all. Granting them assignability produces a
    membership with owner_id NULL, which cannot receive work — so somebody has
    to mint the owner row, and it must be an explicit decision rather than a
    side effect of the first case that needs an owner.

    Idempotent: an existing owner with this email is linked, not duplicated."""
    email = (email or "").strip()
    if "@" not in email:
        return {"ok": False, "error": "an email is required"}
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT owner_id::text FROM owners "
                        "WHERE lower(email)=lower(%s) LIMIT 1", (email,))
            row = cur.fetchone()
            if row:
                owner_id, created = row[0], False
            else:
                parts = (display_name or email.split("@")[0]).split()
                first = parts[0] if parts else email.split("@")[0]
                last = " ".join(parts[1:]) if len(parts) > 1 else ""
                cur.execute(
                    """INSERT INTO owners (owner_id, first_name, last_name,
                                           email, role, is_active, is_synthetic)
                       VALUES (gen_random_uuid(), %s, %s, %s, 'Staff', true, false)
                       RETURNING owner_id::text""", (first, last, email))
                owner_id, created = cur.fetchone()[0], True
            cur.execute(
                """UPDATE assignable_identity
                   SET owner_id=%s::uuid, updated_at=now()
                   WHERE lower(email)=lower(%s)""", (owner_id, email))
            linked = cur.rowcount
        conn.commit()
        logger.info(f"[assignable] provisioned owner for {email} by {by} "
                    f"(created={created})")
        return {"ok": True, "owner_id": owner_id, "owner_created": created,
                "membership_linked": bool(linked)}
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return {"ok": False, "error": str(exc)[:220]}
    finally:
        if conn is not None:
            conn.close()


def environment() -> Dict[str, Any]:
    """Is this a real staffed organisation, or synthetic demo data?

    Reported so the product cannot appear staffed when it is not. The judgement
    is DERIVED FROM COUNTS, never from an email domain — 40 of 44 owners are
    customer contacts and the employee cohort is seed data, so the only honest
    measure is how many people a human has actually authorised."""
    granted = len(directory())
    linked = len([d for d in directory() if d.get("owner_id")])
    emp = _rows("SELECT count(*) AS n FROM employees")
    return {
        "assignable": granted,
        "assignable_and_linked": linked,
        "directory_records": int(emp[0]["n"]) if emp else 0,
        "synthetic": linked <= 4,
        "message": ("Synthetic organisation — employee records are simulated. "
                    "Work eligibility is granted explicitly, so directory "
                    "records are not automatically able to receive work."),
    }


def revoke(email: str, *, by: str = "admin") -> Dict[str, Any]:
    """Deactivate a membership. The row is KEPT so past assignments stay
    explicable — who could receive work last quarter is a real question."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""UPDATE assignable_identity
                           SET is_active = false, updated_at = now()
                           WHERE lower(email) = lower(%s)""", (email,))
            n = cur.rowcount
        conn.commit()
        logger.info(f"[assignable] revoked {email} by {by}")
        return {"ok": True, "revoked": n}
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        if conn is not None:
            conn.close()
