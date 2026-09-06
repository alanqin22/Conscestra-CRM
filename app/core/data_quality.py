"""Data-quality agent — garbage in, governed out (round-3 improvement #5).

Every prediction, campaign and cadence is only as good as the rows beneath
it, and until now data hygiene was one-off SQL scripts. This module makes it
a STANDING agent behavior:

    scan       nightly read-only detectors — duplicate contacts (same
               account + email), unnormalized phones, unreachable contacts
               (no email, no phone), leads with no contact info, ownerless
               accounts, duplicate account names (report-only; merging
               accounts stays a human job)
    report     GET /data-quality/report + a compact line in the CEO
               briefing's Agent Performance section
    fix        the two SAFE fixes become governance proposals (critic-
               checked, one-click, and UNDOABLE — every fix records its
               before-state):
                 data.normalize_phones  contact/lead phones → E.164
                 data.merge_contacts    exact-duplicate contacts merged into
                                        the oldest (activities reassigned,
                                        dupes soft-deleted)

Deterministic SQL only; caps per run (DQ_FIX_LIMIT); idempotent proposals
(one pending per fix type). Nothing mutates without an approval.

CONFIG (env)
  DQ_ENABLED     0    nightly scan→propose job on/off
  DQ_FIX_LIMIT   50   max rows/groups fixed per approved run
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("data_quality")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("DQ_ENABLED")
FIX_LIMIT = int(os.getenv("DQ_FIX_LIMIT", "50"))

_E164 = r"^\+[0-9]{8,15}$"


def _rows(sql: str, params=None) -> List[tuple]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    except Exception as exc:
        logger.debug(f"[dq] query skipped: {exc}")
        return []
    finally:
        conn.close()


# ============================================================================
# DETECTORS — read-only, each returns {count, samples}
# ============================================================================

def detect_duplicate_contacts() -> Dict[str, Any]:
    rows = _rows(
        """SELECT c.account_id::text, lower(c.email), count(*),
                  (array_agg(COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'')||' '||
                       COALESCE(c.last_name,'')),''), c.email)
                   ORDER BY c.created_at))[1]
           FROM contacts c
           WHERE COALESCE(c.is_deleted,false)=false
             AND c.email IS NOT NULL AND c.email <> ''
             AND c.account_id IS NOT NULL
           GROUP BY 1, 2 HAVING count(*) > 1
           ORDER BY count(*) DESC LIMIT 200""")
    return {"count": len(rows),
            "samples": [f"{r[3]} ({r[1]}) ×{r[2]}" for r in rows[:5]]}


def detect_unnormalized_phones() -> Dict[str, Any]:
    rows = _rows(
        f"""SELECT 'contact' AS kind, phone FROM contacts
            WHERE COALESCE(is_deleted,false)=false
              AND COALESCE(phone,'') <> '' AND phone !~ '{_E164}'
            UNION ALL
            SELECT 'lead', phone FROM leads
            WHERE deleted_at IS NULL
              AND COALESCE(phone,'') <> '' AND phone !~ '{_E164}'""")
    return {"count": len(rows),
            "samples": [f"{k}: {p}" for k, p in rows[:5]]}


def detect_unreachable_contacts() -> Dict[str, Any]:
    rows = _rows(
        """SELECT count(*) FROM contacts
           WHERE COALESCE(is_deleted,false)=false
             AND COALESCE(email,'') = '' AND COALESCE(phone,'') = ''""")
    return {"count": int(rows[0][0]) if rows else 0, "samples": []}


def detect_unreachable_leads() -> Dict[str, Any]:
    rows = _rows(
        """SELECT count(*) FROM leads
           WHERE deleted_at IS NULL
             AND COALESCE(email,'') = '' AND COALESCE(phone,'') = ''""")
    return {"count": int(rows[0][0]) if rows else 0, "samples": []}


def detect_ownerless_accounts() -> Dict[str, Any]:
    rows = _rows(
        """SELECT count(*) FROM accounts
           WHERE COALESCE(is_deleted,false)=false AND owner_id IS NULL""")
    return {"count": int(rows[0][0]) if rows else 0, "samples": []}


def detect_duplicate_account_names() -> Dict[str, Any]:
    """Report-only: merging ACCOUNTS moves money and history — human's call."""
    rows = _rows(
        """SELECT lower(trim(account_name)), count(*)
           FROM accounts WHERE COALESCE(is_deleted,false)=false
           GROUP BY 1 HAVING count(*) > 1
           ORDER BY count(*) DESC LIMIT 100""")
    return {"count": len(rows), "samples": [f"{r[0]} ×{r[1]}" for r in rows[:5]]}


DETECTORS = {
    "duplicate_contacts":      detect_duplicate_contacts,
    "unnormalized_phones":     detect_unnormalized_phones,
    "unreachable_contacts":    detect_unreachable_contacts,
    "unreachable_leads":       detect_unreachable_leads,
    "ownerless_accounts":      detect_ownerless_accounts,
    "duplicate_account_names": detect_duplicate_account_names,
}


def scan() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, det in DETECTORS.items():
        try:
            out[name] = det()
        except Exception as exc:
            out[name] = {"count": 0, "error": str(exc)[:120]}
    return out


def scan_lines() -> List[str]:
    """Compact briefing line(s) — only what's actually nonzero."""
    s = scan()
    bits = []
    label = {"duplicate_contacts": "duplicate contact group(s)",
             "unnormalized_phones": "unnormalized phone(s)",
             "unreachable_contacts": "contact(s) with no email/phone",
             "unreachable_leads": "lead(s) with no email/phone",
             "ownerless_accounts": "ownerless account(s)",
             "duplicate_account_names": "duplicate account name(s)"}
    for k, v in s.items():
        if v.get("count"):
            bits.append(f"{v['count']} {label[k]}")
    return [f"data quality: {' · '.join(bits)}"] if bits else []


# ============================================================================
# GOVERNED FIXES — executed only on approval; every fix records its
# before-state so governance undo can reverse it exactly.
# ============================================================================

def normalize_phones_sp(p: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize contact/lead phones to E.164 (cap `limit`). Result carries
    before/after per row — the undo contract."""
    from app.core.telephony import normalize_phone
    limit = min(int(p.get("limit", FIX_LIMIT)), FIX_LIMIT)
    changed, skipped = [], 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for table, pk, alive in (
                    ("contacts", "contact_id", "COALESCE(is_deleted,false)=false"),
                    ("leads", "lead_id", "deleted_at IS NULL")):
                remaining = limit - len(changed)
                if remaining <= 0:
                    break
                cur.execute(
                    f"""SELECT {pk}::text, phone FROM {table}
                        WHERE {alive} AND COALESCE(phone,'') <> ''
                          AND phone !~ '{_E164}' LIMIT %s""", (remaining,))
                for rid, phone in cur.fetchall():
                    norm = normalize_phone(phone)
                    if not norm or norm == phone:
                        skipped += 1
                        continue
                    cur.execute(f"UPDATE {table} SET phone=%s, updated_at=now() "
                                f"WHERE {pk}=%s::uuid", (norm, rid))
                    changed.append({"table": table, "id": rid,
                                    "before": phone, "after": norm})
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[dq] normalized {len(changed)} phone(s), skipped {skipped}")
    return {"ok": True, "normalized": len(changed), "skipped": skipped,
            "changes": changed}


def undo_normalize_phones(result_data: Dict[str, Any]) -> Dict[str, Any]:
    changes = (result_data or {}).get("changes") or []
    restored = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for ch in changes:
                pk = "contact_id" if ch["table"] == "contacts" else "lead_id"
                cur.execute(f"UPDATE {ch['table']} SET phone=%s, updated_at=now() "
                            f"WHERE {pk}=%s::uuid AND phone=%s",
                            (ch["before"], ch["id"], ch["after"]))
                restored += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "restored": restored, "of": len(changes)}


def merge_contacts_sp(p: Dict[str, Any]) -> Dict[str, Any]:
    """Merge exact-duplicate contacts (same account + same email): keep the
    OLDEST, reassign the dupes' activities to it, soft-delete the dupes.
    Result records every move — the undo contract."""
    limit = min(int(p.get("limit", FIX_LIMIT)), FIX_LIMIT)
    merges = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT array_agg(contact_id::text ORDER BY created_at)
                   FROM contacts
                   WHERE COALESCE(is_deleted,false)=false
                     AND email IS NOT NULL AND email <> ''
                     AND account_id IS NOT NULL
                   GROUP BY account_id, lower(email)
                   HAVING count(*) > 1 LIMIT %s""", (limit,))
            groups = [r[0] for r in cur.fetchall()]
            for ids in groups:
                keeper, dupes = ids[0], ids[1:]
                for dupe in dupes:
                    cur.execute(
                        """UPDATE activities SET contact_id=%s::uuid
                           WHERE contact_id=%s::uuid
                           RETURNING activity_id::text""", (keeper, dupe))
                    moved = [r[0] for r in cur.fetchall()]
                    cur.execute("UPDATE contacts SET is_deleted=true, "
                                "updated_at=now() WHERE contact_id=%s::uuid",
                                (dupe,))
                    merges.append({"keeper": keeper, "removed": dupe,
                                   "moved_activities": moved})
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[dq] merged {len(merges)} duplicate contact(s) "
                f"across {len(groups)} group(s)")
    return {"ok": True, "groups": len(groups), "merged": len(merges),
            "merges": merges}


def undo_merge_contacts(result_data: Dict[str, Any]) -> Dict[str, Any]:
    merges = (result_data or {}).get("merges") or []
    restored = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for m in merges:
                cur.execute("UPDATE contacts SET is_deleted=false, "
                            "updated_at=now() WHERE contact_id=%s::uuid",
                            (m["removed"],))
                restored += cur.rowcount
                for aid in m.get("moved_activities") or []:
                    cur.execute("UPDATE activities SET contact_id=%s::uuid "
                                "WHERE activity_id=%s::uuid",
                                (m["removed"], aid))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "restored_contacts": restored, "of": len(merges)}


# ============================================================================
# PROPOSE — the nightly agent behavior (fixes only ever queue)
# ============================================================================

def propose_fixes(force: bool = False) -> Dict[str, Any]:
    if not ENABLED and not force:
        return {"enabled": False, "skipped": True}
    from app.core import governance
    s = scan()
    proposed, skipped = [], []
    plans = [
        ("data.normalize_phones", "unnormalized_phones",
         "normalize contact/lead phone numbers to E.164"),
        ("data.merge_contacts", "duplicate_contacts",
         "merge exact-duplicate contacts (same account + email) into the oldest"),
    ]
    for action, detector, why in plans:
        n = s.get(detector, {}).get("count", 0)
        if not n:
            skipped.append({action: "nothing to fix"})
            continue
        pending = _rows("SELECT 1 FROM action_approvals WHERE action_type=%s "
                        "AND status='pending' LIMIT 1", (action,))
        if pending:
            skipped.append({action: "already awaiting approval"})
            continue
        try:
            aid = governance.propose(
                action, "data_quality",
                {"limit": FIX_LIMIT, "why": why,
                 "evidence": {"detector": detector, "count": n,
                              "samples": s[detector].get("samples") or []}},
                confidence=0.6, severity="low")
        except governance.ProposalCapReached as capped:
            # `skipped` already carries the other reasons a detector produced
            # no proposal ("nothing to fix", "already awaiting approval"), and
            # this belongs beside them: a reason, reported, not an aborted pass
            # that leaves the remaining detectors unrun.
            skipped.append({action: str(capped)})
            continue
        proposed.append({"action": action, "count": n, "approval_uuid": aid})
    return {"enabled": ENABLED, "scan": {k: v.get("count", 0) for k, v in s.items()},
            "proposed": proposed, "skipped": skipped}


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["data-quality"])


@router.get("/data-quality/report")
def dq_report():
    return {"enabled": ENABLED, "fix_limit": FIX_LIMIT, "scan": scan()}


@router.post("/data-quality/propose")
async def dq_propose():
    """Run the scan→propose pass now (forced; the nightly job self-gates)."""
    import asyncio
    return await asyncio.to_thread(propose_fixes, True)
