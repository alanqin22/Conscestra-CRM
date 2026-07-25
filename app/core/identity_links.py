"""Reversible identity links + the resolved ("golden record") view (P1).

The decision half of identity resolution. `identity_resolution.py` DETECTS
duplicate candidates; this module persists what a human decided about each pair
and lets reads resolve through those decisions — WITHOUT ever rewriting the
underlying records:

    detect (identity_resolution)  ──▶  propose()   → status='candidate'
                                        confirm()  → status='confirmed'
                                        reject()   → 'rejected' (genuinely different)
                                        unlink()   → 'unlinked'  (reverses a confirm)

    canonical_id(entity, id)  → the surviving record id for any member
    group(entity, id)         → every record in the identity group
    resolved(entity, id)      → one merged view + WHERE each field came from

WHY LINKS, NOT MERGES (the invariant): a wrong auto-merge conflates two real
customers and is nearly undetectable afterwards — strictly worse than leaving two
duplicates. So both records survive untouched and every decision is reversible.
Destructive materialization (soft-deleting a dupe, reassigning its activities)
stays data_quality.merge_contacts' governed+undoable path — deliberately not here.

SURVIVORSHIP is a READ concern, computed in resolved(): the primary's non-null
value wins; gaps are filled from the most recently updated linked record, and each
field reports which record it came from. Nothing is persisted, so changing the
rule later re-renders the view rather than migrating data.

CONFIG (env)
  IDENTITY_AUTO_LINK      0     auto-confirm very-high-confidence candidates
  IDENTITY_AUTO_LINK_MIN  0.95  the threshold auto-confirm applies at
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("identity_links")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


AUTO_LINK = _flag("IDENTITY_AUTO_LINK", "0")
AUTO_LINK_MIN = float(os.getenv("IDENTITY_AUTO_LINK_MIN", "0.95"))

MAX_CHAIN = 10          # cycle/runaway guard when following confirmed links

# Entity → (primary-key column, survivorship field whitelist). Column names are
# OURS (never caller input) — safe to place in SQL.
ENTITIES: Dict[str, Tuple[str, List[str]]] = {
    "accounts": ("account_id", ["account_name", "type", "industry", "phone",
                                "email", "website", "owner_id", "status"]),
    "contacts": ("contact_id", ["first_name", "last_name", "email", "phone",
                                "role", "account_id", "owner_id", "status"]),
    "leads":    ("lead_id",    ["first_name", "last_name", "company", "email",
                                "phone", "source", "status", "owner_id"]),
}


# ── Materialization plan (see materialize_sp) ────────────────────────────────
# Business/history rows re-pointed from the duplicate to the primary. Table and
# column names are OURS (never caller input) — safe to place in SQL.
# DELIBERATELY EXCLUDED: auth_credentials (a login is never silently moved),
# account_intelligence(+_history) (derived — recomputed from the merged record),
# leads.converted_* (historical provenance of the original conversion).
_REASSIGN: Dict[str, List[Tuple[str, str]]] = {
    "accounts": [("contacts", "account_id"), ("opportunities", "account_id"),
                 ("orders", "account_id"), ("invoices", "account_id"),
                 ("payments", "account_id"), ("cases", "account_id"),
                 ("activities", "account_id")],
    "contacts": [("opportunities", "contact_id"), ("orders", "contact_id"),
                 ("invoices", "contact_id"), ("payments", "contact_id"),
                 ("cases", "contact_id"), ("activities", "contact_id")],
    "leads":    [("activities", "lead_id")],
}
_SOFT_DELETE = {
    "accounts": ("UPDATE accounts SET is_deleted=true, updated_at=now() "
                 "WHERE account_id=%s::uuid", "is_deleted"),
    "contacts": ("UPDATE contacts SET is_deleted=true, updated_at=now() "
                 "WHERE contact_id=%s::uuid", "is_deleted"),
    "leads":    ("UPDATE leads SET is_deleted=true, deleted_at=now(), "
                 "updated_at=now() WHERE lead_id=%s::uuid", "is_deleted"),
}
_RESTORE = {
    "accounts": "UPDATE accounts SET is_deleted=false, updated_at=now() WHERE account_id=%s::uuid",
    "contacts": "UPDATE contacts SET is_deleted=false, updated_at=now() WHERE contact_id=%s::uuid",
    "leads":    "UPDATE leads SET is_deleted=false, deleted_at=NULL, updated_at=now() WHERE lead_id=%s::uuid",
}


class LinkError(ValueError):
    """Invalid entity / unknown link / a decision that would break an invariant."""


def _entity(entity: str) -> Tuple[str, List[str]]:
    e = ENTITIES.get(entity)
    if not e:
        raise LinkError(f"unknown entity '{entity}'. Valid: {', '.join(ENTITIES)}")
    return e


# ============================================================================
# Low-level helpers
# ============================================================================

def _query(sql: str, params: tuple = ()) -> List[tuple]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[identity-links] query skipped: {exc}")
        return []
    finally:
        conn.close()


def _execute(sql: str, params: tuple = ()) -> int:
    """Write helper — raises (callers decide). Returns affected row count."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


# ============================================================================
# Decisions (consumed by identity_resolution.scan to suppress decided pairs)
# ============================================================================

def decided(entity: str) -> Tuple[Set[str], Set[frozenset]]:
    """(ids CONFIRMED as duplicates, pairs a human REJECTED)."""
    confirmed: Set[str] = set()
    rejected: Set[frozenset] = set()
    for pid, dup, status in _query(
            "SELECT primary_id::text, duplicate_id::text, status FROM identity_links "
            "WHERE entity=%s AND status IN ('confirmed','rejected')", (entity,)):
        if status == "confirmed":
            confirmed.add(dup)
        else:
            rejected.add(frozenset((pid, dup)))
    return confirmed, rejected


# ============================================================================
# PROPOSE — turn detected candidates into reviewable link rows
# ============================================================================

def _survivor(members: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The primary: OLDEST record wins (consistent with data_quality's
    merge_contacts keeper rule), id as a deterministic tie-break."""
    return sorted(members, key=lambda m: (m.get("_created") is None,
                                          m.get("_created"), m["id"]))[0]


def propose(entity: Optional[str] = None, min_confidence: float = 0.75,
            limit: int = 200, created_by: str = "detector") -> Dict[str, Any]:
    """Persist undecided candidate groups as pairwise link rows (idempotent —
    the pair unique constraint makes a re-run a no-op). Nothing is confirmed
    unless IDENTITY_AUTO_LINK is on and confidence clears AUTO_LINK_MIN."""
    from app.core import identity_resolution as IR

    targets = [entity] if entity else list(ENTITIES)
    out = {"proposed": 0, "auto_confirmed": 0, "existing": 0, "by_entity": {}}
    for ent in targets:
        _entity(ent)
        try:
            groups = IR.candidate_groups(ent)
        except Exception as exc:
            logger.warning(f"[identity-links] detect failed for {ent}: {exc}")
            out["by_entity"][ent] = {"error": str(exc)[:120]}
            continue

        n_prop = n_auto = n_exist = 0
        for g in groups:
            if float(g["confidence"]) < min_confidence or n_prop >= limit:
                continue
            keeper = _survivor(g["members"])
            method = g.get("block") or "normalized_name"
            auto = AUTO_LINK and float(g["confidence"]) >= AUTO_LINK_MIN
            for m in g["members"]:
                if m["id"] == keeper["id"]:
                    continue
                evidence = {"reason": g.get("reason"), "key": g.get("key"),
                            "group_size": g.get("size"),
                            "primary_name": keeper.get("name") or keeper.get("account_name"),
                            "duplicate_name": m.get("name") or m.get("account_name")}
                try:
                    n = _execute(
                        """INSERT INTO identity_links
                             (entity, primary_id, duplicate_id, status, confidence,
                              match_method, evidence, created_by, decided_at, decided_by)
                           VALUES (%s,%s::uuid,%s::uuid,%s,%s,%s,%s::jsonb,%s,
                                   CASE WHEN %s THEN now() ELSE NULL END,%s)
                           ON CONFLICT (entity, primary_id, duplicate_id) DO NOTHING""",
                        (ent, keeper["id"], m["id"],
                         "confirmed" if auto else "candidate",
                         float(g["confidence"]), method, json.dumps(evidence),
                         created_by, auto,
                         "system:auto" if auto else None))
                except Exception as exc:
                    logger.warning(f"[identity-links] propose failed ({ent}): {exc}")
                    continue
                if n:
                    n_prop += 1
                    if auto:
                        n_auto += 1
                else:
                    n_exist += 1
        out["proposed"] += n_prop
        out["auto_confirmed"] += n_auto
        out["existing"] += n_exist
        out["by_entity"][ent] = {"proposed": n_prop, "auto_confirmed": n_auto,
                                 "already_present": n_exist}

    out["auto_link_enabled"] = AUTO_LINK
    out["note"] = (f"{out['proposed']} link(s) recorded"
                   + (f", {out['auto_confirmed']} auto-confirmed" if out["auto_confirmed"] else "")
                   + ". Nothing was merged — records are untouched and every link is reversible.")
    return out


# ============================================================================
# DECIDE — confirm / reject / unlink (all reversible, all audited)
# ============================================================================

def _link(link_id: str) -> Dict[str, Any]:
    rows = _query(
        "SELECT link_id::text, entity, primary_id::text, duplicate_id::text, status, "
        "confidence, match_method, materialized_at FROM identity_links "
        "WHERE link_id=%s::uuid", (link_id,))
    if not rows:
        raise LinkError(f"unknown link '{link_id}'")
    keys = ["link_id", "entity", "primary_id", "duplicate_id", "status",
            "confidence", "match_method", "materialized_at"]
    return dict(zip(keys, rows[0]))


def _mirror_lead_scaffold(primary_id: str, duplicate_id: str,
                          confidence: Optional[float], linked: bool) -> None:
    """Mirror a LEAD decision into the pre-existing leads dedupe columns
    (merged_into_lead_id / dedupe_group_id / dedupe_confidence) so the existing
    leads UI + formatter and lead_candidates()'s own filter reflect it.

    This is a POINTER, not a deletion — fully reversible: unlink clears it. On
    unlink the primary's group marker is cleared only once it has no confirmed
    duplicates left. Best-effort: a missing column never fails the decision."""
    try:
        if linked:
            _execute("UPDATE leads SET merged_into_lead_id=%s::uuid, "
                     "dedupe_group_id=%s::uuid, dedupe_confidence=%s "
                     "WHERE lead_id=%s::uuid",
                     (primary_id, primary_id, confidence, duplicate_id))
            _execute("UPDATE leads SET dedupe_group_id=%s::uuid WHERE lead_id=%s::uuid "
                     "AND dedupe_group_id IS NULL", (primary_id, primary_id))
        else:
            _execute("UPDATE leads SET merged_into_lead_id=NULL, dedupe_group_id=NULL, "
                     "dedupe_confidence=NULL WHERE lead_id=%s::uuid", (duplicate_id,))
            remaining = _query("SELECT 1 FROM identity_links WHERE entity='leads' "
                               "AND primary_id=%s::uuid AND status='confirmed' LIMIT 1",
                               (primary_id,))
            if not remaining:
                _execute("UPDATE leads SET dedupe_group_id=NULL WHERE lead_id=%s::uuid",
                         (primary_id,))
    except Exception as exc:
        logger.warning(f"[identity-links] lead scaffold mirror skipped: {exc}")


def confirm(link_id: str, decided_by: str = "admin") -> Dict[str, Any]:
    """Accept a link: reads resolve the duplicate through to the primary. Guards
    against a cycle (confirming A→B when B already resolves to A) and against a
    record being confirmed into two different primaries."""
    lk = _link(link_id)
    ent, pid, dup = lk["entity"], lk["primary_id"], lk["duplicate_id"]

    if canonical_id(ent, pid) == dup:
        raise LinkError("refused: that would create a cycle — the primary already "
                        "resolves to this duplicate. Unlink the existing link first.")
    other = _query("SELECT primary_id::text FROM identity_links WHERE entity=%s "
                   "AND duplicate_id=%s::uuid AND status='confirmed' "
                   "AND link_id <> %s::uuid", (ent, dup, link_id))
    if other:
        raise LinkError(f"refused: that record is already linked into "
                        f"{other[0][0]}. Unlink that first.")

    _execute("UPDATE identity_links SET status='confirmed', decided_at=now(), "
             "decided_by=%s WHERE link_id=%s::uuid", (decided_by, link_id))
    if ent == "leads":
        _mirror_lead_scaffold(pid, dup, lk.get("confidence"), linked=True)
    return {"ok": True, "link_id": link_id, "status": "confirmed",
            "entity": ent, "primary_id": pid, "duplicate_id": dup,
            "canonical_id": canonical_id(ent, dup),
            "note": "Linked. Both records still exist untouched; reads resolve "
                    "the duplicate to the primary. Reversible via /unlink."}


def reject(link_id: str, decided_by: str = "admin") -> Dict[str, Any]:
    """Mark the pair as genuinely DIFFERENT records — suppresses re-proposal."""
    lk = _link(link_id)
    _execute("UPDATE identity_links SET status='rejected', decided_at=now(), "
             "decided_by=%s WHERE link_id=%s::uuid", (decided_by, link_id))
    return {"ok": True, "link_id": link_id, "status": "rejected",
            "entity": lk["entity"],
            "note": "Recorded as distinct records; this pair won't be proposed again."}


def unlink(link_id: str, decided_by: str = "admin") -> Dict[str, Any]:
    """Reverse a confirmed link. Nothing to undo in the records themselves —
    that is the whole point of the link model."""
    lk = _link(link_id)
    if lk["status"] != "confirmed":
        raise LinkError(f"link is '{lk['status']}', not 'confirmed' — nothing to reverse")
    if lk.get("materialized_at"):
        raise LinkError("this link was MATERIALIZED (records were physically "
                        "merged) — undo the merge from the governance audit "
                        "first, then unlink")
    _execute("UPDATE identity_links SET status='unlinked', decided_at=now(), "
             "decided_by=%s WHERE link_id=%s::uuid", (decided_by, link_id))
    if lk["entity"] == "leads":
        _mirror_lead_scaffold(lk["primary_id"], lk["duplicate_id"], None, linked=False)
    return {"ok": True, "link_id": link_id, "status": "unlinked",
            "entity": lk["entity"],
            "note": "Link reversed; the records were never modified, so both are "
                    "immediately independent again."}


# ============================================================================
# MATERIALIZE — the OPTIONAL destructive merge, governed + undoable
# ----------------------------------------------------------------------------
# Links alone are enough for reads (resolved() merges at read time). Some
# workflows want one physical record instead: this re-points the duplicate's
# business/history rows to the primary and soft-deletes the duplicate.
#
# It is deliberately the narrow, guarded path:
#   • only from an ALREADY-CONFIRMED link (a human agreed they are the same),
#   • proposed into the governance queue — never runs unattended,
#   • ONE transaction — any failure rolls the whole merge back,
#   • every moved row id recorded, so the governance undo restores it exactly,
#   • logins and derived intelligence are never moved (see _REASSIGN).
# ============================================================================

def _pk(table: str) -> Optional[str]:
    """The table's single-column primary key (needed to record exact moves for
    undo). None for a composite/missing PK — such a table is skipped, never
    reassigned blindly."""
    rows = _query(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = %s::regclass AND i.indisprimary", (table,))
    return rows[0][0] if len(rows) == 1 else None


def materialize_sp(p: Dict[str, Any]) -> Dict[str, Any]:
    """Execute the destructive merge for a confirmed link (governance-approved).
    Returns the full move manifest — the undo contract."""
    link_id = str((p or {}).get("link_id") or "")
    if not link_id:
        raise LinkError("link_id is required")
    lk = _link(link_id)
    ent, pid, dup = lk["entity"], lk["primary_id"], lk["duplicate_id"]
    if lk["status"] != "confirmed":
        raise LinkError(f"link is '{lk['status']}' — only a CONFIRMED link may be "
                        f"materialized (confirm it first)")
    if lk.get("materialized_at"):
        return {"ok": True, "already_materialized": True, "link_id": link_id,
                "note": "This link was already materialized."}

    moves: List[Dict[str, Any]] = []
    skipped: List[str] = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for table, col in _REASSIGN.get(ent, []):
                pk = _pk(table)
                if not pk:
                    skipped.append(f"{table} (no single-column primary key)")
                    continue
                cur.execute(
                    f"UPDATE {table} SET {col} = %s::uuid WHERE {col} = %s::uuid "
                    f"RETURNING {pk}::text", (pid, dup))
                ids = [r[0] for r in cur.fetchall()]
                if ids:
                    moves.append({"table": table, "column": col, "pk": pk,
                                  "ids": ids, "from": dup, "to": pid})
            sql, _flagcol = _SOFT_DELETE[ent]
            cur.execute(sql, (dup,))
            cur.execute("UPDATE identity_links SET materialized_at=now() "
                        "WHERE link_id=%s::uuid", (link_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    moved = sum(len(m["ids"]) for m in moves)
    logger.info(f"[identity-links] materialized {ent} {dup[:8]} -> {pid[:8]}: "
                f"{moved} row(s) across {len(moves)} table(s)")
    return {"ok": True, "link_id": link_id, "entity": ent,
            "primary_id": pid, "duplicate_id": dup,
            "rows_moved": moved, "moves": moves, "skipped": skipped,
            "note": (f"Merged: {moved} row(s) re-pointed to the primary and the "
                     f"duplicate soft-deleted. Undoable from the governance audit.")}


def undo_materialize(result_data: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse a materialization exactly: every recorded row goes back to the
    duplicate, the duplicate is un-deleted. The LINK itself stays confirmed (a
    separate, independently reversible decision) — use unlink() for that."""
    d = result_data or {}
    link_id, ent = d.get("link_id"), d.get("entity")
    dup, moves = d.get("duplicate_id"), d.get("moves") or []
    if not (ent and dup):
        return {"ok": False, "error": "nothing to undo (no manifest recorded)"}
    restored = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for m in moves:
                if not m.get("ids"):
                    continue
                cur.execute(
                    f"UPDATE {m['table']} SET {m['column']} = %s::uuid "
                    f"WHERE {m['pk']} = ANY(%s::uuid[])", (dup, m["ids"]))
                restored += cur.rowcount
            cur.execute(_RESTORE[ent], (dup,))
            if link_id:
                cur.execute("UPDATE identity_links SET materialized_at=NULL "
                            "WHERE link_id=%s::uuid", (link_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "rows_restored": restored, "duplicate_restored": dup,
            "note": "Merge reversed; the link remains confirmed (unlink separately "
                    "to also undo the link decision)."}


def propose_materialize(link_id: str, proposed_by: str = "admin") -> Dict[str, Any]:
    """Queue the destructive merge for human approval (never executes here)."""
    from app.core import governance
    lk = _link(link_id)
    if lk["status"] != "confirmed":
        raise LinkError(f"link is '{lk['status']}' — confirm it before proposing a merge")
    if lk.get("materialized_at"):
        raise LinkError("this link has already been materialized")
    existing = _query(
        "SELECT 1 FROM action_approvals WHERE action_type='identity.materialize_link' "
        "AND status='pending' AND params::text LIKE %s LIMIT 1", (f"%{link_id}%",))
    if existing:
        return {"ok": True, "already_queued": True,
                "note": "A merge for this link is already awaiting approval."}
    g = group(lk["entity"], lk["primary_id"])
    aid = governance.propose(
        "identity.materialize_link", "identity_resolution",
        {"link_id": link_id, "entity": lk["entity"],
         "primary_id": lk["primary_id"], "duplicate_id": lk["duplicate_id"],
         "why": (f"materialize the confirmed {lk['entity']} duplicate link "
                 f"(group of {g['size']})"),
         "evidence": {"confidence": float(lk["confidence"] or 0),
                      "match_method": lk["match_method"],
                      "group_size": g["size"]}},
        confidence=float(lk["confidence"] or 0),
        severity="high" if lk["entity"] == "accounts" else "medium")
    return {"ok": True, "approval_uuid": aid, "link_id": link_id,
            "note": ("Queued for approval. Nothing has changed yet — on approval "
                     "the duplicate's records move to the primary and it is "
                     "soft-deleted (undoable from the governance audit).")}


# ============================================================================
# RESOLVE — read through confirmed links
# ============================================================================

def canonical_id(entity: str, record_id: str) -> str:
    """The surviving record id for `record_id`, following confirmed links
    transitively (depth-capped so a bad chain can never loop forever)."""
    _entity(entity)
    current, seen = record_id, {record_id}
    for _ in range(MAX_CHAIN):
        rows = _query("SELECT primary_id::text FROM identity_links WHERE entity=%s "
                      "AND duplicate_id=%s::uuid AND status='confirmed' LIMIT 1",
                      (entity, current))
        if not rows:
            return current
        nxt = rows[0][0]
        if nxt in seen:
            logger.warning(f"[identity-links] cycle detected at {entity}:{nxt}")
            return current
        current, _ = nxt, seen.add(nxt)
    logger.warning(f"[identity-links] chain longer than {MAX_CHAIN} for {entity}:{record_id}")
    return current


def group(entity: str, record_id: str) -> Dict[str, Any]:
    """The whole identity group: the canonical record + every record confirmed
    into it (transitively, depth-capped)."""
    _entity(entity)
    root = canonical_id(entity, record_id)
    members, frontier = [root], [root]
    for _ in range(MAX_CHAIN):
        if not frontier:
            break
        rows = _query(
            "SELECT duplicate_id::text FROM identity_links WHERE entity=%s "
            "AND status='confirmed' AND primary_id = ANY(%s::uuid[])",
            (entity, frontier))
        frontier = [r[0] for r in rows if r[0] not in members]
        members.extend(frontier)
    return {"entity": entity, "canonical_id": root, "member_ids": members,
            "size": len(members)}


def resolved(entity: str, record_id: str) -> Dict[str, Any]:
    """The merged ("golden record") view of an identity group. The canonical
    record's non-null value wins; each remaining gap is filled from the most
    recently updated linked record, and every field reports its source record —
    so a merged value is always explainable and nothing is persisted."""
    pk, fields = _entity(entity)
    g = group(entity, record_id)
    ids = g["member_ids"]
    cols = ", ".join(fields)
    rows = _query(
        f"SELECT {pk}::text, {cols}, updated_at FROM {entity} "
        f"WHERE {pk} = ANY(%s::uuid[]) ORDER BY updated_at DESC NULLS LAST", (ids,))
    if not rows:
        return {**g, "resolved": {}, "sources": {}, "records": 0}

    recs = [dict(zip([pk] + fields + ["updated_at"], r)) for r in rows]
    canonical = next((r for r in recs if r[pk] == g["canonical_id"]), recs[0])
    others = [r for r in recs if r[pk] != canonical[pk]]

    merged, sources, filled = {}, {}, 0
    for f in fields:
        val = canonical.get(f)
        src = canonical[pk]
        if val in (None, ""):
            for o in others:                     # already ordered most-recent first
                if o.get(f) not in (None, ""):
                    val, src = o[f], o[pk]
                    filled += 1
                    break
        merged[f] = str(val) if val is not None else None
        sources[f] = src
    return {**g, "resolved": merged, "sources": sources, "records": len(recs),
            "fields_filled_from_duplicates": filled,
            "note": ("Merged view only — computed at read time from confirmed "
                     "links. No record was modified.")}


# ============================================================================
# Review queue / status
# ============================================================================

def pending(entity: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    where, params = ["status='candidate'"], []
    if entity:
        _entity(entity)
        where.append("entity=%s")
        params.append(entity)
    rows = _query(
        "SELECT link_id::text, entity, primary_id::text, duplicate_id::text, "
        "confidence, match_method, evidence::text, created_at "
        "FROM identity_links WHERE " + " AND ".join(where) +
        " ORDER BY confidence DESC, created_at LIMIT %s", tuple(params) + (limit,))
    items = []
    for r in rows:
        try:
            ev = json.loads(r[6]) if r[6] else {}
        except json.JSONDecodeError:
            ev = {}
        items.append({"link_id": r[0], "entity": r[1], "primary_id": r[2],
                      "duplicate_id": r[3], "confidence": float(r[4]) if r[4] else None,
                      "match_method": r[5], "evidence": ev,
                      "created_at": r[7].isoformat() if r[7] else None})
    return {"count": len(items), "pending": items}


def status() -> Dict[str, Any]:
    rows = _query("SELECT entity, status, count(*) FROM identity_links "
                  "GROUP BY 1,2 ORDER BY 1,2")
    by: Dict[str, Dict[str, int]] = {}
    for ent, st, n in rows:
        by.setdefault(ent, {})[st] = int(n)
    return {"auto_link_enabled": AUTO_LINK, "auto_link_min": AUTO_LINK_MIN,
            "table_present": bool(_query("SELECT to_regclass('public.identity_links')")
                                  and _query("SELECT to_regclass('public.identity_links')")[0][0]),
            "by_entity": by}


# ============================================================================
# Router (admin)
# ============================================================================
router = APIRouter(tags=["identity-links"])


@router.get("/identity/links/status")
def links_status():
    return status()


@router.post("/identity/links/propose")
def links_propose(body: Optional[Dict[str, Any]] = None):
    b = body or {}
    try:
        return propose(b.get("entity"), float(b.get("min_confidence", 0.75)),
                       int(b.get("limit", 200)), str(b.get("created_by") or "detector"))
    except (LinkError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/identity/links/pending")
def links_pending(entity: Optional[str] = None, limit: int = 100):
    try:
        return pending(entity, limit)
    except LinkError as exc:
        return {"error": str(exc)}


@router.post("/identity/links/{link_id}/confirm")
def links_confirm(link_id: str, body: Optional[Dict[str, Any]] = None):
    try:
        return confirm(link_id, str((body or {}).get("decided_by") or "admin"))
    except LinkError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/identity/links/{link_id}/reject")
def links_reject(link_id: str, body: Optional[Dict[str, Any]] = None):
    try:
        return reject(link_id, str((body or {}).get("decided_by") or "admin"))
    except LinkError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/identity/links/{link_id}/unlink")
def links_unlink(link_id: str, body: Optional[Dict[str, Any]] = None):
    try:
        return unlink(link_id, str((body or {}).get("decided_by") or "admin"))
    except LinkError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/identity/links/{link_id}/materialize")
def links_materialize(link_id: str, body: Optional[Dict[str, Any]] = None):
    """PROPOSE the destructive merge of a confirmed link (governance approval
    required — nothing changes here)."""
    try:
        return propose_materialize(link_id, str((body or {}).get("proposed_by") or "admin"))
    except LinkError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning(f"[identity-links] propose materialize failed: {exc}")
        return {"ok": False, "error": str(exc)[:160]}


@router.get("/identity/resolved/{entity}/{record_id}")
def links_resolved(entity: str, record_id: str):
    """The merged golden-record view for an identity group (read-time only)."""
    try:
        return resolved(entity, record_id)
    except LinkError as exc:
        return {"error": str(exc)}
