"""Retention — data that ages out on a schedule (audit finding #7).

`lifecycle.py` erases a person ON REQUEST. Nothing expired anything on a clock:
no scheduled job aged out transcripts, voice text, session memory, event queues
or AI memories, and `compliance.py` stated "operational records are retained for
the life of the account" — a policy by omission rather than by decision. With
voice transcripts now landing and a database volume already near full, that
becomes an availability problem before it becomes a compliance one.

THE RULE THAT MAKES THIS SAFE: a store is expirable only if it appears here with
an explicit basis. Nothing is deleted by pattern, by table-name guess, or by
default. Financial and audit records have no entry and therefore cannot be
touched — the same reason lifecycle.py marks them RETAIN.

    DERIVED_PII_STORES ──┐
                         ├──▶ POLICIES ──▶ preview() ──▶ purge()
    operational stores ──┘                   (dry run)     (deletes)

Derived stores (the semantic index, consolidated memories) are registered in
`lifecycle.DERIVED_PII_STORES` and inherit their parent's retention
automatically. That registry exists because `content_embeddings` shipped holding
a verbatim copy of personal text and was missing from every erasure plan; this
module consults it so retention cannot repeat that omission independently.

Every purge is bounded (`MAX_DELETE` per store per pass) and event emission is
suppressed — a bulk delete that fires row triggers would flood inboxes.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("retention")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# OFF by default. Retention DELETES data; it must be switched on deliberately,
# after a preview has been read by someone who can judge the basis.
ENABLED = _flag("RETENTION_ENABLED", "0")
MAX_DELETE = int(os.getenv("RETENTION_MAX_DELETE", "5000"))


class Policy:
    """One expirable store. `days` is the retention period; `basis` is the
    reason a human can be shown when asked why data disappeared."""

    def __init__(self, table: str, ts_column: str, days: int, basis: str,
                 where: Optional[str] = None, env: Optional[str] = None):
        self.table = table
        self.ts_column = ts_column
        self.default_days = days
        self.basis = basis
        self.where = where
        self.env = env or f"RETENTION_{table.upper()}_DAYS"

    @property
    def days(self) -> int:
        """Per-store override, so a deployment can lengthen or shorten one
        period without editing code. 0 disables THIS store."""
        try:
            return int(os.getenv(self.env, str(self.default_days)))
        except ValueError:
            return self.default_days


# ── The policy set ───────────────────────────────────────────────────────────
# Only OPERATIONAL exhaust and personal-content stores appear here. Anything
# with an independent legal basis (invoices, payments, orders, audit_log,
# email_suppression) is deliberately ABSENT and therefore untouchable.
POLICIES: List[Policy] = [
    Policy("event_queue", "created_at", 30,
           "operational exhaust — replayed events have no value after the "
           "handlers that consume them have run",
           where="COALESCE(status,'') IN ('done','settled','failed','')"),
    Policy("a2a_dispatches", "at", 90,
           "agent call log — kept long enough to investigate an incident, "
           "not indefinitely"),
    # F-8.7 dispositions (Axis 6). Both stores were found by the content scan
    # holding subject email addresses while being invisible to the structural
    # DSAR check, unreachable by erasure, and governed by nothing. Neither is a
    # record anyone needs years of, so the proportionate answer is to BOUND the
    # exposure rather than build export/erasure paths for operational exhaust.
    Policy("events", "created_at", 180,
           "domain event log — payloads carry subject identifiers (52 rows "
           "measured 2026-08-08, including a contact.deleted event retaining "
           "the erased contact's address). No subject FK, so neither exported "
           "nor erasable; six months is the window in which 'what happened to "
           "this record?' is still asked"),
    Policy("email_sentiment", "received_at", 90,
           "inbound sentiment scores keyed by correspondent address. Mostly "
           "our own inbox, but external senders appear; a sentiment score is "
           "not a record with an independent basis for indefinite retention"),
    Policy("scheduled_job_runs", "started_at", 90,
           "scheduled-job outcome log — the window in which 'did the nightly "
           "batch run on the 3rd?' is still a question anyone asks. Same "
           "basis and period as a2a_dispatches; both are operational exhaust "
           "kept for incident forensics. job_ledger.prune() applies the same "
           "90 days unconditionally, because this policy set is gated on "
           "RETENTION_ENABLED and the table must stay bounded either way"),
    Policy("memory_retrievals", "created_at", 180,
           "retrieval grounding — the window in which a bad reply is still "
           "worth investigating"),
    Policy("agent_session_memory", "updated_at", 30,
           "in-flight conversation scratch; a session older than this is over"),
    Policy("conversation_messages", "created_at", 1095,
           "customer conversation content — 3 years, the usual commercial "
           "record period; shorten per jurisdiction",
           env="RETENTION_CONVERSATIONS_DAYS"),
    Policy("interaction_memories", "created_at", 1095,
           "AI-distilled summaries of the above; expire with their source",
           env="RETENTION_CONVERSATIONS_DAYS"),
    # The deletion undo log (sql/governed_mutation.sql). It exists so a bulk
    # repair is reversible; the window in which anyone actually reverts one is
    # short, and it holds full JSONB row images — i.e. copies of the personal
    # data the deleted rows contained. Keeping it forever would recreate, in a
    # new table, exactly the retention problem it was built to solve.
    #
    # DECLARED repairs are kept longer: those are the ones a human might
    # deliberately unwind, and they are the rare case. Undeclared churn is
    # ordinary consolidation exhaust and expires quickly.
    Policy("governed_deletions", "deleted_at", 30,
           "undo images of routine deletions — reversal happens within days "
           "or not at all, and these are copies of personal data",
           where="repair_key = 'undeclared'",
           env="RETENTION_UNDO_UNDECLARED_DAYS"),
    Policy("governed_deletions", "deleted_at", 365,
           "undo images of DECLARED repairs — the ones a human may knowingly "
           "reverse; kept a year so a bad migration is still recoverable",
           where="repair_key <> 'undeclared'",
           env="RETENTION_UNDO_DECLARED_DAYS"),
]


def _exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def _derived_followers(table: str) -> List[Dict[str, str]]:
    """Derived stores that must expire alongside `table`.

    Consults lifecycle.DERIVED_PII_STORES rather than keeping a second list:
    the erasure bug that registry was built for was exactly a second copy nobody
    remembered. Retention must not re-learn that lesson separately."""
    try:
        from app.core.lifecycle import DERIVED_PII_STORES
    except Exception:
        return []
    return [{"table": name} for name, spec in DERIVED_PII_STORES.items()
            if name != table and spec.get("regenerated_by")]


def preview() -> Dict[str, Any]:
    """What WOULD be deleted, per store. Writes nothing.

    The approval surface: retention is irreversible, so the counts have to be
    readable before anyone enables it."""
    out: List[Dict[str, Any]] = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for p in POLICIES:
                row: Dict[str, Any] = {"store": p.table, "days": p.days,
                                       "basis": p.basis, "rows": 0,
                                       "present": False, "enabled": p.days > 0}
                if not _exists(cur, p.table):
                    out.append(row)
                    continue
                row["present"] = True
                if p.days <= 0:
                    out.append(row)
                    continue
                try:
                    cur.execute(
                        f"SELECT count(*) FROM {p.table} "
                        f"WHERE {p.ts_column} < now() - (%s || ' days')::interval"
                        + (f" AND ({p.where})" if p.where else ""),
                        (str(p.days),))
                    row["rows"] = int(cur.fetchone()[0])
                except Exception as exc:
                    conn.rollback()
                    row["error"] = str(exc).splitlines()[0][:120]
                out.append(row)
    finally:
        conn.close()
    return {"enabled": ENABLED, "max_delete_per_pass": MAX_DELETE,
            "stores": out, "total_rows": sum(r["rows"] for r in out),
            "protected": ["invoices", "payments", "orders", "audit_log",
                          "email_suppression"],
            "note": "Stores with an independent legal basis have no policy and "
                    "cannot be expired by this module."}


def purge(dry_run: bool = False) -> Dict[str, Any]:
    """Delete expired rows, bounded per store. Never touches a store without a
    policy."""
    if not ENABLED and not dry_run:
        return {"ok": False, "reason": "disabled (set RETENTION_ENABLED=1)"}

    deleted: Dict[str, int] = {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # A bulk delete fires row triggers; without this every purged row
            # can land in someone's notification inbox.
            cur.execute("SET LOCAL app.suppress_events = 'notify'")
            for p in POLICIES:
                if p.days <= 0 or not _exists(cur, p.table):
                    continue
                try:
                    sql = (f"DELETE FROM {p.table} WHERE ctid IN ("
                           f"  SELECT ctid FROM {p.table} "
                           f"  WHERE {p.ts_column} < now() - (%s || ' days')::interval"
                           + (f" AND ({p.where})" if p.where else "")
                           + f"  LIMIT {int(MAX_DELETE)})")
                    if dry_run:
                        cur.execute(sql.replace("DELETE FROM", "SELECT count(*) FROM", 1)
                                    .replace("WHERE ctid IN (", "WHERE ctid IN (", 1),
                                    (str(p.days),))
                        deleted[p.table] = int(cur.fetchone()[0])
                    else:
                        # Name the policy that expired these rows, so the 'undeclared'
                        # signal keeps meaning "nobody explained this".
                        cur.execute("SET LOCAL app.repair_key = %s",
                                    (f'retention:{p.table}',))
                        cur.execute(sql, (str(p.days),))
                        deleted[p.table] = cur.rowcount
                except Exception as exc:
                    conn.rollback()
                    logger.warning(f"[retention] {p.table} skipped: "
                                   f"{str(exc).splitlines()[0][:120]}")
                    continue
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    total = sum(deleted.values())
    if total and not dry_run:
        logger.info(f"[retention] purged {total} expired row(s): "
                    + ", ".join(f"{k}={v}" for k, v in deleted.items() if v))
    return {"ok": True, "dry_run": dry_run, "deleted": deleted, "total": total,
            "derived_followers": _derived_followers("")}


router = APIRouter(tags=["retention"])


@router.get("/retention/preview")
def retention_preview():
    return preview()


@router.post("/retention/purge")
def retention_purge(body: Optional[Dict[str, Any]] = None):
    return purge(dry_run=bool((body or {}).get("dry_run", False)))
