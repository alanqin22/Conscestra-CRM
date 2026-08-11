"""Data lifecycle / erasure — the distributed copies, not just the row (#8).

Deleting a CRM record is not one DELETE. A person's data is scattered across
custom fields, AI memories, conversation transcripts, channel identity links,
duplicate links, financial history and audit rows — and the right treatment
DIFFERS per store. Relying on FK cascades silently leaves PII behind (memories,
transcripts, custom fields) while risking the destruction of records that must be
kept (invoices, audit trail).

This module makes the policy EXPLICIT per store:

    DELETE     derived / personal data with no retention basis
               (custom field values, AI memories, transcripts, identity links)
    ANONYMIZE  the core record — the row survives so financial and audit history
               stays referentially intact, but the personal fields are redacted
    RETAIN     records with an independent legal/compliance basis
               (invoices, payments, orders, audit_log — and email_suppression,
                which MUST survive: deleting a suppression would re-permit
                emailing someone who opted out)

So an erasure is: anonymize the core row + delete the PII satellites + keep the
financial/audit skeleton. That satisfies a GDPR/CCPA erasure request without
corrupting the books.

IRREVERSIBLE. `erase()` is therefore proposed into the GOVERNANCE queue like any
other consequential write — but unlike the others it has NO undo, and says so.
`preview()` is read-only and shows exactly what would happen first.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("lifecycle")

DELETE, ANONYMIZE, RETAIN = "delete", "anonymize", "retain"

# ── The mark an anonymised record carries ────────────────────────────────────
# An erased core row SURVIVES (so invoices and audit history stay referentially
# intact) with its personal fields redacted. `email` gets a UNIQUE placeholder
# rather than NULL, because a NOT NULL/UNIQUE constraint would otherwise block
# the erasure — and that placeholder is the only durable, machine-readable
# signal that a row is a tombstone rather than a person.
#
# It is defined HERE, next to the code that writes it, and imported by anything
# that needs to recognise one. A second copy of this string somewhere else is a
# copy that will still say `redacted+` after this one changes.
ERASED_EMAIL_PREFIX = "redacted+"
ERASED_EMAIL_DOMAIN = "@invalid.local"
# SQL LIKE pattern for "this row was erased" — see identity_resolution, which
# uses it to keep tombstones out of duplicate detection (F-9.11).
ERASED_EMAIL_LIKE = f"{ERASED_EMAIL_PREFIX}%{ERASED_EMAIL_DOMAIN}"


def erased_email(record_id: str) -> str:
    """The placeholder written into `email` when a record is anonymised."""
    return f"{ERASED_EMAIL_PREFIX}{record_id[:8]}{ERASED_EMAIL_DOMAIN}"


def _sat(table: str, column: str, action: str, why: str,
         via: Optional[Tuple[str, str, str]] = None,
         also: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    """A satellite store. `via` = (parent_table, parent_key, parent_match_col) for
    child rows reachable only through a parent (e.g. messages via conversations).

    `also` = EXTRA columns OR'd with `column` when matching the subject. A table
    can reference the same person through more than one column, and matching
    only the first leaves the rest behind. F-9.5: identity_links names two
    parties per row (primary_id, duplicate_id) and erasure matched only
    duplicate_id, so a subject on the PRIMARY side survived their own erasure
    with their identifier still in `evidence` — measured, 7 of 20 rows carried
    an email there.
    """
    return {"table": table, "column": column, "action": action, "why": why,
            "via": via, "also": tuple(also or ())}


# ── Derived-copy registry ────────────────────────────────────────────────────
# Stores that hold a SECOND copy of personal text produced from somewhere else —
# an index, a cache, a distillation. They are the ones governance forgets,
# because nobody thinks of an index as a place where personal data lives.
#
# This registry exists because it already happened: `content_embeddings` shipped
# holding a verbatim `snippet` of every indexed activity, case comment and
# customer message — 7,051 rows across 228 contacts — and was absent from every
# plan below. A completed erasure would have left the person's own words fully
# retrievable. Retention (the next policy to land) would have missed it the same
# way, for the same reason.
#
# Anything added here MUST also appear in the satellite list of every plan whose
# entity it can carry — `test_lifecycle_covers_derived_stores` fails otherwise.
# Deleting a derived copy is always safe: it regenerates from whatever survives.
DERIVED_PII_STORES: Dict[str, Dict[str, str]] = {
    "content_embeddings": {
        "why": "semantic index stores indexed text verbatim in `snippet`",
        "contacts": "contact_id",
        "accounts": "account_id",
        "regenerated_by": "app.core.content_index.reindex",
    },
    "memory_verifications": {
        # FOURTH instance of this bug class, found by adversarial review. This
        # table stores `statement_shown` — the exact claim ABOUT A PERSON that a
        # human approved — and was in no erasure plan. It is deliberately
        # append-only, which made it easier to argue it should survive; that
        # argument does not survive an erasure request.
        #
        # REACHED BY ENTITY, NOT BY PARENT. It was previously erased by joining
        # through customer_memories, so a verification whose memory row had been
        # swept away was unreachable — and 10 of 10 rows in the live database
        # were exactly that. The sweep deletes any memory with verified_by IS
        # NULL, and re-derivation CLEARS verified_by whenever the evidence hash
        # moves — so a memory that was verified, then re-derived, then lost its
        # topic is swept while its verification rows remain. (`reject()` does
        # set verified_by, so rejected memories are NOT swept; an earlier note
        # here claimed otherwise.) `entity_id` is denormalised onto
        # the row (sql/memory_audit_erasure.sql) so erasure never depends on a
        # parent the system is expected to delete.
        "why": "verification records quote the approved claim about the person",
        "contacts": "entity_id",
        "accounts": "entity_id",
        "regenerated_by": "(not regenerated — audit history, erased with the person)",
    },
    "customer_memories": {
        "why": "consolidated memories are derived assertions ABOUT the person; "
               "their evidence points at records that are being erased",
        "contacts": "entity_id",
        "accounts": "entity_id",
        "regenerated_by": "app.core.memory_consolidation.consolidate_entity",
    },
    "interaction_memories": {
        "why": "AI-distilled summaries quote the person's own words",
        "contacts": "entity_id",
        "accounts": "entity_id",
        "leads": "entity_id",
        "regenerated_by": "app.core.conversations.distill_idle",
    },
}


# ── Per-entity plan ──────────────────────────────────────────────────────────
# pii: core columns to redact. `email` gets a unique placeholder (never NULL) so a
# NOT NULL/UNIQUE constraint can't block an erasure.
PLANS: Dict[str, Dict[str, Any]] = {
    "contacts": {
        "pk": "contact_id",
        "pii": {"first_name": "'Redacted'", "last_name": "'Contact'",
                "email": None, "phone": "NULL", "role": "NULL"},
        "satellites": [
            _sat("custom_field_values", "entity_id", DELETE,
                 "admin-defined field values are personal data with no independent basis"),
            _sat("interaction_memories", "entity_id", DELETE,
                 "AI memories quote the person's own words"),
            _sat("memory_verifications", "entity_id", DELETE,
                 "verification records quote the approved claim about the person"),
            _sat("customer_memories", "entity_id", DELETE,
                 "consolidated memories are derived assertions about the person"),
            # The undo log holds full JSONB images of rows deleted BEFORE this
            # request. Leaving them behind would mean an erasure completes while
            # a mechanically restorable copy of the person's memories remains.
            _sat("governed_deletions", "entity_id", DELETE,
                 "undo-log images of this person's previously deleted rows"),
            # The semantic index keeps a VERBATIM copy of the indexed text in
            # `snippet`. Erasing the source rows while leaving the index intact
            # would leave the person's own words retrievable after a completed
            # erasure. Deleting the index rows is safe and self-healing: the
            # next indexer pass re-indexes whatever survived erasure (e.g. an
            # anonymized activity), so nothing legitimate is lost.
            _sat("content_embeddings", "contact_id", DELETE,
                 "semantic index stores the person's text verbatim in `snippet`"),
            _sat("crm_agent_memory", "entity_id", DELETE, "agent scratch memory"),
            _sat("channel_identities", "party_id", DELETE,
                 "phone/email/handle → party links ARE the identifiers"),
            _sat("identity_links", "duplicate_id", DELETE,
                 "identity links naming this person on EITHER side — the row "
                 "records a match between two parties and carries the matched "
                 "identifier in `evidence` (F-9.5)", also=("primary_id",)),
            _sat("conversation_messages", "conversation_id", DELETE,
                 "transcripts contain personal content",
                 via=("conversations", "conversation_id", "party_id")),
            _sat("conversations", "party_id", DELETE, "conversation threads"),
            _sat("activities", "contact_id", ANONYMIZE,
                 "activity history is retained but de-linked from the person"),
            _sat("invoices", "contact_id", RETAIN, "financial record — legal retention"),
            _sat("payments", "contact_id", RETAIN, "financial record — legal retention"),
            _sat("orders", "contact_id", RETAIN, "financial record — legal retention"),
            _sat("cases", "contact_id", RETAIN, "service history — de-linked, not deleted"),
            _sat("email_suppression", "email", RETAIN,
                 "MUST survive: deleting a suppression would re-permit emailing them"),
            _sat("audit_log", "entity_id", RETAIN, "audit trail — tamper-evidence"),
        ],
    },
    "leads": {
        "pk": "lead_id",
        "pii": {"first_name": "'Redacted'", "last_name": "'Lead'",
                "email": None, "phone": "NULL", "company": "NULL",
                "address_line1": "NULL", "address_line2": "NULL",
                "city": "NULL", "postal_code": "NULL"},
        "satellites": [
            _sat("custom_field_values", "entity_id", DELETE, "personal field values"),
            _sat("interaction_memories", "entity_id", DELETE, "AI memories"),
            _sat("memory_verifications", "entity_id", DELETE,
                 "verification records quote the approved claim"),
            _sat("customer_memories", "entity_id", DELETE, "consolidated memories"),
            _sat("governed_deletions", "entity_id", DELETE,
                 "undo-log images of this person's previously deleted rows"),
            _sat("crm_agent_memory", "entity_id", DELETE, "agent scratch memory"),
            _sat("channel_identities", "party_id", DELETE, "identifier links"),
            _sat("identity_links", "duplicate_id", DELETE,
                 "identity links naming this person on EITHER side — the row records a match between two parties and carries the matched identifier in `evidence` (F-9.5)", also=("primary_id",)),
            _sat("activities", "lead_id", ANONYMIZE, "history retained, de-linked"),
            _sat("email_suppression", "email", RETAIN,
                 "MUST survive: consent/suppression proof"),
            _sat("audit_log", "entity_id", RETAIN, "audit trail"),
        ],
    },
    "accounts": {
        # An account is an organization: erasure applies to its contact details.
        "pk": "account_id",
        "pii": {"email": None, "phone": "NULL", "website": "NULL"},
        "satellites": [
            _sat("custom_field_values", "entity_id", DELETE, "personal field values"),
            _sat("interaction_memories", "entity_id", DELETE, "AI memories"),
            _sat("memory_verifications", "entity_id", DELETE,
                 "verification records quote the approved claim"),
            _sat("customer_memories", "entity_id", DELETE, "consolidated memories"),
            _sat("governed_deletions", "entity_id", DELETE,
                 "undo-log images of this person's previously deleted rows"),
            _sat("content_embeddings", "account_id", DELETE,
                 "semantic index stores indexed text verbatim in `snippet`"),
            _sat("identity_links", "duplicate_id", DELETE,
                 "identity links naming this person on EITHER side — the row records a match between two parties and carries the matched identifier in `evidence` (F-9.5)", also=("primary_id",)),
            _sat("invoices", "account_id", RETAIN, "financial record — legal retention"),
            _sat("payments", "account_id", RETAIN, "financial record — legal retention"),
            _sat("orders", "account_id", RETAIN, "financial record — legal retention"),
            _sat("audit_log", "entity_id", RETAIN, "audit trail"),
        ],
    },
}


class LifecycleError(ValueError):
    """Unknown entity / missing record."""


def _plan(entity: str) -> Dict[str, Any]:
    p = PLANS.get(entity)
    if not p:
        raise LifecycleError(f"unknown entity '{entity}'. Valid: {', '.join(PLANS)}")
    return p


def _exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
    return bool(cur.fetchone()[0])


def _entity_email(cur, entity: str, pk: str, record_id: str) -> Optional[str]:
    cur.execute(f"SELECT email FROM {entity} WHERE {pk}=%s::uuid", (record_id,))
    r = cur.fetchone()
    return r[0] if r else None


def _count(cur, sat: Dict[str, Any], entity: str, record_id: str,
           email: Optional[str]) -> Optional[int]:
    """Rows this satellite holds for the record (None = table absent/not countable)."""
    t, col = sat["table"], sat["column"]
    if not _exists(cur, t):
        return None
    try:
        if sat["via"]:
            parent, pkey, pmatch = sat["via"]
            if not _exists(cur, parent):
                return None
            cur.execute(f"SELECT count(*) FROM {t} WHERE {col} IN "
                        f"(SELECT {pkey} FROM {parent} WHERE {pmatch}=%s::uuid)",
                        (record_id,))
        elif col == "email":
            if not email:
                return 0
            cur.execute(f"SELECT count(*) FROM {t} WHERE lower({col})=lower(%s)", (email,))
        elif t == "custom_field_values" or t == "interaction_memories" \
                or t == "crm_agent_memory":
            # entity-scoped stores: filter on entity too when the column exists
            if _has_col(cur, t, "entity"):
                cur.execute(f"SELECT count(*) FROM {t} WHERE entity=%s AND {col}=%s::uuid",
                            (entity, record_id))
            else:
                cur.execute(f"SELECT count(*) FROM {t} WHERE {col}=%s::uuid", (record_id,))
        elif t == "identity_links":
            # MUST use the same predicate erase_sp uses, `also` columns
            # included. preview() is what a human approves against: if it
            # counts on duplicate_id while the erasure deletes on
            # duplicate_id OR primary_id, the approver is shown fewer rows
            # than will actually be destroyed. A preview that undercounts is
            # worse than no preview, because it is trusted.
            cols = [col] + [a for a in sat.get("also", ())
                            if _has_col(cur, t, a)]
            pred = " OR ".join(f"{x}=%s::uuid" for x in cols)
            cur.execute(f"SELECT count(*) FROM {t} WHERE entity=%s AND ({pred})",
                        (entity, *([record_id] * len(cols))))
        elif t == "audit_log":
            cur.execute(f"SELECT count(*) FROM {t} WHERE {col}=%s::uuid", (record_id,))
        else:
            cur.execute(f"SELECT count(*) FROM {t} WHERE {col}=%s::uuid", (record_id,))
        return int(cur.fetchone()[0])
    except Exception as exc:
        logger.debug(f"[lifecycle] count skipped for {t}: {exc}")
        return None


_COLS: Dict[str, set] = {}


def _has_col(cur, table: str, col: str) -> bool:
    if table not in _COLS:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s", (table,))
        _COLS[table] = {r[0] for r in cur.fetchall()}
    return col in _COLS[table]


# ============================================================================
# PREVIEW (read-only)
# ============================================================================

def preview(entity: str, record_id: str) -> Dict[str, Any]:
    """Exactly what an erasure would do — per store, with counts. Writes nothing."""
    p = _plan(entity)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {entity} WHERE {p['pk']}=%s::uuid", (record_id,))
            if not cur.fetchone():
                raise LifecycleError(f"{entity} record '{record_id}' not found")
            email = _entity_email(cur, entity, p["pk"], record_id) \
                if _has_col(cur, entity, "email") else None
            items = []
            for sat in p["satellites"]:
                n = _count(cur, sat, entity, record_id, email)
                items.append({"store": sat["table"], "matched_on": sat["column"],
                              "action": sat["action"], "rows": n, "why": sat["why"],
                              "present": n is not None})
        conn.rollback()
    finally:
        conn.close()

    buckets = {DELETE: 0, ANONYMIZE: 0, RETAIN: 0}
    for it in items:
        if it["rows"]:
            buckets[it["action"]] += it["rows"]
    return {
        "entity": entity, "record_id": record_id,
        "core_record": {"action": ANONYMIZE,
                        "fields": sorted(p["pii"].keys()),
                        "why": ("the row survives so financial and audit history stays "
                                "referentially intact; personal fields are redacted")},
        "stores": items,
        "totals": {"rows_to_delete": buckets[DELETE],
                   "rows_to_de_link": buckets[ANONYMIZE],
                   "rows_retained": buckets[RETAIN]},
        "reversible": False,
        "note": ("Nothing has changed. Erasure is IRREVERSIBLE and requires a "
                 "governance approval; financial, suppression and audit records are "
                 "retained by policy."),
    }


# ============================================================================
# ERASE (the governed executor — IRREVERSIBLE)
# ============================================================================

def _survivors_after_erase(cur, plan, entity: str, record_id: str) -> Dict[str, int]:
    """Re-read every DELETE satellite and report anything still standing.

    Verifies the OUTCOME rather than the statement's return value. Written
    because a rule rewrote one erasure to NOTHING and nothing noticed: the
    caller only checked `if cur.rowcount:`, so zero rows removed was
    indistinguishable from zero rows present."""
    left: Dict[str, int] = {}
    for sat in plan["satellites"]:
        if sat["action"] != DELETE or not _exists(cur, sat["table"]):
            continue
        t, col = sat["table"], sat["column"]
        if sat["via"]:
            parent, pkey, pmatch = sat["via"]
            if not _exists(cur, parent):
                continue
            cur.execute(f"SELECT count(*) FROM {t} WHERE {col} IN "
                        f"(SELECT {pkey} FROM {parent} WHERE {pmatch}=%s::uuid)",
                        (record_id,))
        elif not _has_col(cur, t, col):
            continue
        elif _has_col(cur, t, "entity"):
            cur.execute(f"SELECT count(*) FROM {t} WHERE entity=%s AND {col}=%s::uuid",
                        (entity, record_id))
        else:
            cur.execute(f"SELECT count(*) FROM {t} WHERE {col}=%s::uuid", (record_id,))
        n = cur.fetchone()[0]
        if n:
            left[t] = n
    return left


def erase_sp(params: Dict[str, Any]) -> Dict[str, Any]:
    """Execute an erasure (called on governance approval). One transaction: any
    failure rolls the whole thing back. NO UNDO EXISTS — by design."""
    entity = str((params or {}).get("entity") or "")
    record_id = str((params or {}).get("record_id") or "")
    p = _plan(entity)
    if not record_id:
        raise LifecycleError("record_id is required")

    deleted: Dict[str, int] = {}
    delinked: Dict[str, int] = {}
    retained: List[str] = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # memory_verifications is append-only and REFUSES a delete without
            # this. SET LOCAL scopes it to this transaction, so the exemption
            # cannot leak onto a pooled connection.
            # One protocol for the whole transaction: suppresses the deletion
            # undo log (an erasure must not stay recoverable) and unlocks the
            # append-only verification trail. See sql/governed_mutation.sql.
            cur.execute("SET LOCAL app.erasure = 'on'")
            cur.execute("SET LOCAL app.memory_audit_erase = 'on'")
            cur.execute(f"SELECT 1 FROM {entity} WHERE {p['pk']}=%s::uuid", (record_id,))
            if not cur.fetchone():
                raise LifecycleError(f"{entity} record '{record_id}' not found")

            for sat in p["satellites"]:
                t, col, action = sat["table"], sat["column"], sat["action"]
                if action == RETAIN:
                    retained.append(t)
                    continue
                if not _exists(cur, t):
                    continue
                if action == DELETE:
                    if sat["via"]:
                        parent, pkey, pmatch = sat["via"]
                        if not _exists(cur, parent):
                            continue
                        cur.execute(f"DELETE FROM {t} WHERE {col} IN "
                                    f"(SELECT {pkey} FROM {parent} WHERE {pmatch}=%s::uuid)",
                                    (record_id,))
                    elif t in ("custom_field_values", "interaction_memories",
                               "crm_agent_memory", "identity_links"):
                        # `also` columns are OR'd in, and only if they really
                        # exist — a satellite that names a column the table
                        # does not have must not abort the whole erasure.
                        cols = [col] + [a for a in sat.get("also", ())
                                        if _has_col(cur, t, a)]
                        pred = " OR ".join(f"{x}=%s::uuid" for x in cols)
                        args = [record_id] * len(cols)
                        if _has_col(cur, t, "entity"):
                            cur.execute(
                                f"DELETE FROM {t} WHERE entity=%s AND ({pred})",
                                (entity, *args))
                        else:
                            cur.execute(f"DELETE FROM {t} WHERE {pred}", tuple(args))
                    else:
                        cols = [col] + [a for a in sat.get("also", ())
                                        if _has_col(cur, t, a)]
                        pred = " OR ".join(f"{x}=%s::uuid" for x in cols)
                        cur.execute(f"DELETE FROM {t} WHERE {pred}",
                                    tuple([record_id] * len(cols)))
                    if cur.rowcount:
                        deleted[t] = deleted.get(t, 0) + cur.rowcount
                else:  # ANONYMIZE a satellite = drop the pointer, keep the row
                    cur.execute(f"UPDATE {t} SET {col}=NULL WHERE {col}=%s::uuid",
                                (record_id,))
                    if cur.rowcount:
                        delinked[t] = delinked.get(t, 0) + cur.rowcount

            # POST-CONDITION. rowcount cannot distinguish "nothing matched"
            # from "the delete was silently discarded" — a DO INSTEAD NOTHING
            # rule swallowed erasure of memory_verifications and every run
            # reported success while the claims stayed on disk. Re-read instead
            # of trusting the write.
            survivors = _survivors_after_erase(cur, p, entity, record_id)
            if survivors:
                raise LifecycleError(
                    "erasure incomplete — rows survived a DELETE satellite: "
                    + ", ".join(f"{t}={n}" for t, n in survivors.items()))

            # Core record: redact the personal fields (row survives).
            sets, vals = [], []
            for col, expr in p["pii"].items():
                if not _has_col(cur, entity, col):
                    continue
                if expr is None:            # unique placeholder (email)
                    sets.append(f"{col}=%s")
                    vals.append(erased_email(record_id))
                else:
                    sets.append(f"{col}={expr}")
            if _has_col(cur, entity, "updated_at"):
                sets.append("updated_at=now()")
            cur.execute(f"UPDATE {entity} SET {', '.join(sets)} "
                        f"WHERE {p['pk']}=%s::uuid", tuple(vals) + (record_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info(f"[lifecycle] erased {entity} {record_id[:8]}: "
                f"deleted={deleted} delinked={delinked}")
    return {"ok": True, "entity": entity, "record_id": record_id,
            "core_record": "anonymized", "deleted": deleted, "de_linked": delinked,
            "retained": sorted(set(retained)), "reversible": False,
            "note": "Erasure complete and IRREVERSIBLE. Financial, suppression and "
                    "audit records were retained by policy."}


def propose_erase(entity: str, record_id: str, reason: str = "",
                  requested_by: str = "admin") -> Dict[str, Any]:
    """Queue an erasure for approval (nothing changes here)."""
    from app.core import governance
    pv = preview(entity, record_id)
    aid = governance.propose(
        "data.erase_record", "lifecycle",
        {"entity": entity, "record_id": record_id, "reason": reason,
         "why": f"erase personal data for {entity} {record_id[:8]} (irreversible)",
         "evidence": {"rows_to_delete": pv["totals"]["rows_to_delete"],
                      "rows_to_de_link": pv["totals"]["rows_to_de_link"],
                      "rows_retained": pv["totals"]["rows_retained"],
                      "stores": [s["store"] for s in pv["stores"]
                                 if s["action"] == DELETE and s["rows"]]}},
        confidence=1.0, severity="high")
    return {"ok": True, "approval_uuid": aid, "preview": pv,
            "note": ("Queued for approval. This action CANNOT be undone once "
                     "approved — review the preview carefully.")}


# ============================================================================
# Router (admin)
# ============================================================================
router = APIRouter(tags=["lifecycle"])


@router.get("/lifecycle/policy")
def lifecycle_policy():
    """The erasure policy per entity — which stores are deleted / de-linked / retained."""
    return {"entities": {
        e: {"core_action": ANONYMIZE, "pii_fields": sorted(p["pii"].keys()),
            "stores": [{"store": s["table"], "action": s["action"], "why": s["why"]}
                       for s in p["satellites"]]}
        for e, p in PLANS.items()}}


@router.get("/lifecycle/preview/{entity}/{record_id}")
def lifecycle_preview(entity: str, record_id: str):
    try:
        return preview(entity, record_id)
    except LifecycleError as exc:
        return {"error": str(exc)}


@router.post("/lifecycle/erase")
def lifecycle_erase(body: Dict[str, Any]):
    """PROPOSE an erasure (governance approval required; irreversible)."""
    b = body or {}
    try:
        return propose_erase(str(b.get("entity") or ""), str(b.get("record_id") or ""),
                             str(b.get("reason") or ""),
                             str(b.get("requested_by") or "admin"))
    except LifecycleError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning(f"[lifecycle] propose erase failed: {exc}")
        return {"ok": False, "error": str(exc)[:160]}
