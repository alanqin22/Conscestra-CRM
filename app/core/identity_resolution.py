"""Identity Resolution — fuzzy duplicate CANDIDATES (P1, detection increment).

`identity.py` maps a channel handle → an EXISTING party (the unified-comms spine).
`data_quality.py` finds EXACT duplicate contacts (same account + same email). Neither
catches the classic account-dedup problem: "Acme Inc", "Acme, Inc." and "ACME
Incorporated" are three account rows for one company. This module is the fuzzy
candidate detector that does — the foundation of a single customer view.

APPROACH (this increment): deterministic NORMALIZATION + BLOCKING, no extension,
no new table, READ-ONLY.
  • accounts — normalize the name (strip legal suffixes inc/llc/ltd/corp/…,
    punctuation, articles); rows sharing a normalized key are candidates. A shared
    email/website domain or phone raises confidence.
  • contacts / leads — block on normalized email and E.164 phone (exact), which
    already catches the bulk of person duplicates.

Every candidate carries a CONFIDENCE and a TIER (high / review / low). Nothing is
merged or linked here — detection only. The REVERSIBLE link/merge (never a
destructive rewrite of records) is the next increment; this surface lets a human
see the candidates first, exactly like data_quality's report → governed-fix flow.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("identity_resolution")

# Legal-form / noise tokens dropped when normalizing an organization name. Kept
# conservative — meaningful words ('group','holdings','international') stay, so we
# don't over-merge distinct businesses.
_LEGAL = {"inc", "incorporated", "llc", "l", "ltd", "limited", "corp", "corporation",
          "co", "plc", "gmbh", "ag", "sa", "srl", "bv", "nv", "pty", "llp", "lp",
          "company", "the", "and"}

HIGH, REVIEW = 0.90, 0.75


def _tier(conf: float) -> str:
    return "high" if conf >= HIGH else "review" if conf >= REVIEW else "low"


def _norm_org(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    toks = [t for t in s.split() if t and t not in _LEGAL]
    return " ".join(toks).strip()


def _domain(email: Optional[str], website: Optional[str]) -> Optional[str]:
    if email and "@" in email:
        return email.split("@", 1)[1].strip().lower() or None
    if website:
        w = re.sub(r"^https?://", "", website.strip().lower())
        w = re.sub(r"^www\.", "", w).split("/", 1)[0]
        return w or None
    return None


def _rows(sql: str) -> List[tuple]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[identity-res] query skipped: {exc}")
        return []
    finally:
        conn.close()


# ============================================================================
# Account candidates — normalized-name blocking + domain/phone corroboration
# ============================================================================

# ── F-9.11: tombstones are not candidates ───────────────────────────────────
# An erased person's row SURVIVES anonymised, so referential integrity holds.
# It therefore still arrives in every duplicate-detection query, where it has
# no business being: a record with no name and no contactable identifier
# cannot legitimately be "the same person" as anyone, and proposing a merge
# against one asks a human to adjudicate a person who exercised their right to
# be forgotten.
#
# This matters beyond noise. identity_links.decided() feeds REJECTED pairs back
# as a do-not-merge memory, and erasure deletes those rows (Option A). Without
# this filter the tombstone becomes re-proposable with the memory that said
# "not the same person" already gone.
#
# The marker is imported from lifecycle, which writes it — see ERASED_EMAIL_LIKE.
def _not_erased(col: str = "email") -> str:
    """SQL fragment excluding anonymised rows. Fails OPEN (empty string) if
    lifecycle cannot be imported: dedupe noise is a smaller harm than a
    detector that stops running."""
    try:
        from app.core.lifecycle import ERASED_EMAIL_LIKE
        return (f" AND COALESCE({col},'') NOT LIKE "
                f"'{ERASED_EMAIL_LIKE}'")
    except Exception:                                       # noqa: BLE001
        logger.warning("[identity-res] erased-record filter unavailable")
        return ""


def account_candidates() -> List[Dict[str, Any]]:
    rows = _rows(
        "SELECT account_id::text, account_name, email, website, phone, owner_id::text, "
        "created_at FROM accounts WHERE COALESCE(is_deleted,false)=false"
        + _not_erased())
    groups: Dict[str, List[dict]] = defaultdict(list)
    for aid, name, email, website, phone, owner, created in rows:
        key = _norm_org(name)
        if not key:
            continue
        groups[key].append({"id": aid, "account_id": aid, "account_name": name,
                            "name": name, "domain": _domain(email, website),
                            "phone": (phone or "").strip() or None, "owner_id": owner,
                            "created_at": created.isoformat() if created else None,
                            "_created": created})

    out = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        raw_names = {m["account_name"].strip().lower() for m in members}
        domains = {m["domain"] for m in members if m["domain"]}
        phones = {m["phone"] for m in members if m["phone"]}
        # identical raw names → near-certain; suffix/punct-only variants → strong
        conf = 0.97 if len(raw_names) == 1 else 0.86
        reason = ["identical name" if len(raw_names) == 1 else "name matches after normalizing legal suffix/punctuation"]
        if len(domains) == 1 and domains:
            conf = min(0.99, conf + 0.10); reason.append("shared email/web domain")
        if len(phones) == 1 and phones:
            conf = min(0.99, conf + 0.05); reason.append("shared phone")
        out.append({"entity": "accounts", "key": key, "size": len(members),
                    "confidence": round(conf, 2), "tier": _tier(conf),
                    "reason": "; ".join(reason), "members": members})
    return sorted(out, key=lambda g: (-g["confidence"], -g["size"]))


# ============================================================================
# Person candidates (contacts / leads) — exact block on normalized email / phone
# ============================================================================

def _person_candidates(entity: str, sql: str) -> List[Dict[str, Any]]:
    rows = _rows(sql)   # (id, name, email, phone, created_at)
    by_email: Dict[str, List[dict]] = defaultdict(list)
    by_phone: Dict[str, List[dict]] = defaultdict(list)
    for pid, name, email, phone, created in rows:
        m = {"id": pid, "name": (name or "").strip() or None,
             "created_at": created.isoformat() if created else None,
             "_created": created}
        if email and email.strip():
            by_email[email.strip().lower()].append(m)
        if phone and phone.strip():
            by_phone[re.sub(r"[^0-9+]", "", phone)].append(m)

    out, seen = [], set()
    for kind, blocks, conf in (("email", by_email, 0.95), ("phone", by_phone, 0.85)):
        for key, members in blocks.items():
            ids = tuple(sorted(m["id"] for m in members))
            if len(members) < 2 or ids in seen:
                continue
            seen.add(ids)
            out.append({"entity": entity, "block": kind, "key": key, "size": len(members),
                        "confidence": conf, "tier": _tier(conf),
                        "reason": f"shared {kind}", "members": members})
    return sorted(out, key=lambda g: (-g["confidence"], -g["size"]))


def contact_candidates() -> List[Dict[str, Any]]:
    return _person_candidates("contacts",
        "SELECT contact_id::text, TRIM(COALESCE(first_name,'')||' '||COALESCE(last_name,'')), "
        "email, phone, created_at FROM contacts WHERE COALESCE(is_deleted,false)=false"
        + _not_erased())


def lead_candidates() -> List[Dict[str, Any]]:
    return _person_candidates("leads",
        "SELECT lead_id::text, TRIM(COALESCE(first_name,'')||' '||COALESCE(last_name,'')), "
        "email, phone, created_at FROM leads WHERE COALESCE(is_deleted,false)=false "
        "AND merged_into_lead_id IS NULL" + _not_erased())


# ============================================================================
# Report
# ============================================================================

# ============================================================================
# Fuzzy (trigram) account candidates — the second, independent signal
# ============================================================================

SIMILARITY_MIN = 0.62   # trigram floor; below this, name pairs are mostly noise


def _has_trgm() -> bool:
    return bool(_rows("SELECT 1 FROM pg_extension WHERE extname='pg_trgm'"))


def account_candidates_fuzzy() -> List[Dict[str, Any]]:
    """Account pairs whose names are SIMILAR but do NOT normalize to the same key
    — i.e. exactly what normalization misses (typos, word order, spacing). Uses
    pg_trgm similarity; returns [] when the extension isn't installed, so this
    degrades to normalization-only rather than failing.

    Confidence is deliberately capped below the normalized-match tiers: a trigram
    hit is evidence for REVIEW, not grounds to auto-link. A shared domain lifts it."""
    if not _has_trgm():
        return []
    rows = _rows(
        f"""SELECT a.account_id::text, a.account_name, a.email, a.website, a.created_at,
                   b.account_id::text, b.account_name, b.email, b.website, b.created_at,
                   round(similarity(lower(a.account_name), lower(b.account_name))::numeric, 3)
            FROM accounts a
            JOIN accounts b
              ON a.account_id < b.account_id
             AND similarity(lower(a.account_name), lower(b.account_name)) >= {SIMILARITY_MIN}
            WHERE COALESCE(a.is_deleted,false)=false
              AND COALESCE(b.is_deleted,false)=false
              {_not_erased("a.email")}
              {_not_erased("b.email")}
            ORDER BY 11 DESC
            LIMIT 200""")
    out = []
    for (aid, an, ae, aw, ac, bid, bn, be, bw, bc, sim) in rows:
        # Skip pairs the normalized detector already reports (same key) — this
        # detector exists to find what THAT one misses.
        if _norm_org(an) == _norm_org(bn):
            continue
        sim = float(sim)
        da, db = _domain(ae, aw), _domain(be, bw)
        conf = min(0.88, 0.55 + 0.35 * sim)
        reason = [f"name similarity {sim:.0%}"]
        if da and da == db:
            conf = min(0.93, conf + 0.12)
            reason.append("shared email/web domain")
        out.append({"entity": "accounts", "block": "similarity",
                    "key": f"{_norm_org(an)}~{_norm_org(bn)}", "size": 2,
                    "confidence": round(conf, 2), "tier": _tier(conf),
                    "reason": "; ".join(reason), "similarity": sim,
                    "members": [
                        {"id": aid, "account_name": an, "name": an,
                         "created_at": ac.isoformat() if ac else None, "_created": ac},
                        {"id": bid, "account_name": bn, "name": bn,
                         "created_at": bc.isoformat() if bc else None, "_created": bc}]})
    return sorted(out, key=lambda g: -g["confidence"])


def account_candidates_all() -> List[Dict[str, Any]]:
    """Normalized-key candidates + trigram candidates (deduped by member pair)."""
    groups = account_candidates()
    seen = {frozenset(m["id"] for m in g["members"]) for g in groups if g["size"] == 2}
    for g in account_candidates_fuzzy():
        pair = frozenset(m["id"] for m in g["members"])
        if pair not in seen:
            groups.append(g)
            seen.add(pair)
    return groups


ENTITY_DETECTORS = {
    "accounts": account_candidates_all,
    "contacts": contact_candidates,
    "leads":    lead_candidates,
}


def candidate_groups(entity: str) -> List[Dict[str, Any]]:
    """Undecided candidate groups for one entity, WITH internal keys (`_created`)
    retained so the link layer can pick a survivor deterministically."""
    det = ENTITY_DETECTORS.get(entity)
    if not det:
        raise ValueError(f"unknown entity '{entity}'. Valid: {', '.join(ENTITY_DETECTORS)}")
    return _apply_decisions(entity, det())


def _decided(entity: str) -> Tuple[set, set]:
    """(ids already CONFIRMED as duplicates, pairs a human REJECTED) so decided
    pairs stop resurfacing as candidates. Defensive: no identity_links table yet
    → nothing is filtered."""
    try:
        from app.core import identity_links
        return identity_links.decided(entity)
    except Exception as exc:
        logger.debug(f"[identity-res] decision filter skipped: {exc}")
        return set(), set()


def _components(members: List[dict], rejected: set) -> List[List[dict]]:
    """Split a blocked cluster into connected components after REMOVING the pairs
    a human rejected.

    A cluster is fully connected by construction (every member shares the blocking
    key), so treating rejections as removed EDGES is the correct generalization of
    the 2-member case: reject the only edge in a pair and the group disappears;
    reject one edge in a larger cluster and the member stays only while it is still
    joined to the cluster by some un-rejected pair. A member separates out exactly
    when EVERY edge to it has been rejected."""
    ids = [m["id"] for m in members]
    by_id = {m["id"]: m for m in members}
    adj: Dict[str, set] = {i: set() for i in ids}
    for idx, a in enumerate(ids):
        for b in ids[idx + 1:]:
            if frozenset((a, b)) not in rejected:
                adj[a].add(b)
                adj[b].add(a)

    seen: set = set()
    comps: List[List[dict]] = []
    for start in ids:
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            node = stack.pop()
            comp.append(node)
            for nb in adj[node]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if len(comp) >= 2:
            comps.append([by_id[i] for i in comp])
    return comps


def _apply_decisions(entity: str, groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply human decisions to detected clusters: drop records already CONFIRMED
    as duplicates (resolved elsewhere), then remove REJECTED pairs as edges and
    re-split each cluster into connected components. `rejected_pairs` reports how
    many decisions were applied, so the report visibly reflects review progress."""
    confirmed, rejected = _decided(entity)
    out = []
    for g in groups:
        members = [m for m in g["members"] if m["id"] not in confirmed]
        if len(members) < 2:
            continue
        ids = [m["id"] for m in members]
        n_rej = sum(1 for i, a in enumerate(ids) for b in ids[i + 1:]
                    if frozenset((a, b)) in rejected)
        for comp in _components(members, rejected):
            out.append({**g, "size": len(comp), "members": comp,
                        "rejected_pairs": n_rej})
    return out


def _public(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip internal keys (raw datetimes) so the payload is JSON-safe."""
    return [{**g, "members": [{k: v for k, v in m.items() if not k.startswith("_")}
                              for m in g["members"]]} for g in groups]


def scan() -> Dict[str, Any]:
    acc = _apply_decisions("accounts", account_candidates_all())
    con = _apply_decisions("contacts", contact_candidates())
    lead = _apply_decisions("leads", lead_candidates())

    def summarize(groups):
        dupes = sum(g["size"] - 1 for g in groups)   # records beyond the survivor
        return {"groups": len(groups), "duplicate_records": dupes,
                "high": sum(1 for g in groups if g["tier"] == "high"),
                "review": sum(1 for g in groups if g["tier"] == "review")}

    return {
        "accounts": {"summary": summarize(acc), "candidates": _public(acc[:100])},
        "contacts": {"summary": summarize(con), "candidates": _public(con[:100])},
        "leads":    {"summary": summarize(lead), "candidates": _public(lead[:100])},
        "note": ("Duplicate CANDIDATES only — nothing is merged. Tiers: high "
                 "(≥0.90), review (≥0.75). The next increment persists a REVERSIBLE "
                 "link a human confirms; records are never destructively rewritten."),
    }


# ============================================================================
# Router (admin — same posture as data_quality / readiness)
# ============================================================================
router = APIRouter(tags=["identity-resolution"])


@router.get("/identity/duplicates/report")
def duplicates_report():
    """Fuzzy duplicate candidates across accounts / contacts / leads."""
    return scan()
