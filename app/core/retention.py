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
    # F-9.12, found by the F-9.11 synthetic erasure probe rather than by
    # reasoning: creating or anonymising a contact emits a notification whose
    # metadata carries the BEFORE/AFTER image of the row, email included. The
    # probe produced 21 such rows in seconds. Production held 0 at the time of
    # writing, so this is a LATENT exposure, not a live one — but the mechanism
    # is proven, and the store is neither exported nor erasable. Bounded here
    # for the same reason as `events`: notification exhaust has no independent
    # basis for indefinite retention.
    Policy("notification_messages", "created_at", 180,
           "notification delivery log — metadata carries before/after row "
           "images that can include a subject's email; no subject FK, so "
           "neither exported nor reachable by erasure"),
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


# ── Cascade accounting ───────────────────────────────────────────────────────
# preview() used to count each store's OWN rows and call that the total. That
# under-reported the blast radius by 4.7x on the `events` policy: 2,816 events
# measured, 13,188 rows actually removed, because `events` is a parent with a
# TWO-LEVEL cascade tree —
#
#     events ─┬─ event_queue                CASCADE
#             ├─ notification_messages      CASCADE
#             │    └─ notification_recipients  CASCADE
#             └─ workflow_runs              CASCADE
#                  └─ workflow_run_steps       CASCADE
#
# A one-level fix would still have been wrong by 5,186 rows (workflow_run_steps),
# which is why this walks the FK graph recursively rather than to a fixed depth.
#
# Three distinct effects are reported, because they are not the same risk:
#   CASCADE   -> child rows are DELETED
#   SET NULL  -> child rows SURVIVE but are silently MUTATED
#   RESTRICT / NO ACTION -> the DELETE would FAIL (the purge aborts for that store)
#
# NOTE ON `max_delete_per_pass`: it bounds only the PARENT. The LIMIT lives in
# the subquery selecting the store's own rows; Postgres applies the cascade
# afterwards, unbounded. A pass capped at 5,000 parents can delete an arbitrary
# number of descendants. The counts below therefore reflect the LIMITed parent
# set, which is what a real pass would actually remove.

_FK_ACTION = {"c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT",
              "r": "RESTRICT", "a": "NO ACTION"}
_MAX_CASCADE_DEPTH = 6


def _fk_edges(cur, table: str) -> List[Dict[str, str]]:
    """Direct FK dependents of `table`, with the action Postgres will take.

    Read from pg_constraint.confdeltype rather than by string-matching a
    constraint definition, so the classification cannot drift from what the
    database will actually do."""
    cur.execute("""
        SELECT c.conrelid::regclass::text AS child,
               ac.attname                 AS child_col,
               ap.attname                 AS parent_col,
               c.confdeltype              AS action
          FROM pg_constraint c
          JOIN LATERAL unnest(c.conkey, c.confkey) AS k(ck, pk) ON true
          JOIN pg_attribute ac ON ac.attrelid = c.conrelid  AND ac.attnum = k.ck
          JOIN pg_attribute ap ON ap.attrelid = c.confrelid AND ap.attnum = k.pk
         WHERE c.contype = 'f' AND c.confrelid = %s::regclass
         ORDER BY 1, 2
    """, (table,))
    return [{"child": r[0], "child_col": r[1], "parent_col": r[2],
             "action": _FK_ACTION.get(r[3], r[3])} for r in cur.fetchall()]


def _policy_rowset(p: "Policy") -> str:
    """SQL for exactly the rows a purge pass would delete from p.table.

    Mirrors purge()'s predicate INCLUDING the LIMIT, so cascade counts describe
    a real pass rather than the whole eligible population. Values are inlined as
    ints from code-defined policy fields — no caller input reaches this."""
    where = f" AND ({p.where})" if p.where else ""
    return (f"SELECT * FROM {p.table} "
            f"WHERE {p.ts_column} < now() - (interval '1 day' * {int(p.days)})"
            f"{where} LIMIT {int(MAX_DELETE)}")


def _walk_cascade(cur, table: str, rowset: str, depth: int,
                  path: tuple, out: List[Dict[str, Any]]) -> None:
    """Recursively count what a delete of `rowset` drags with it.

    `path` guards against cycles — the schema has self-referencing FKs
    (crm_agent_memory.in_reply_to, customer_memories.superseded_by), which
    would otherwise recurse forever."""
    if depth > _MAX_CASCADE_DEPTH:
        return
    # Group by (child, action): a pair can be joined by SEVERAL FKs
    # (payments->employees has three: created_by / updated_by / deleted_by).
    # Counting per EDGE would report the same child row up to three times, so
    # the columns are OR'd into ONE predicate and each row counted once.
    groups: Dict[tuple, List[tuple]] = {}
    for e in _fk_edges(cur, table):
        groups.setdefault((e["child"], e["action"]), []).append(
            (e["child_col"], e["parent_col"]))

    for (child, action), cols in groups.items():
        via = ", ".join(f"{child}.{cc} -> {table}.{pc}" for cc, pc in cols)
        if child in path:
            out.append({"table": child, "depth": depth, "effect": action,
                        "rows": None, "note": "cycle — not followed", "via": via})
            continue
        pred = " OR ".join(
            f"c.{cc} IN (SELECT p.{pc} FROM ({rowset}) p)" for cc, pc in cols)
        try:
            cur.execute(f"SELECT count(*) FROM {child} c WHERE {pred}")
            n = int(cur.fetchone()[0])
        except Exception as exc:
            out.append({"table": child, "depth": depth, "effect": action,
                        "rows": None, "via": via,
                        "error": str(exc).splitlines()[0][:120]})
            continue
        out.append({"table": child, "depth": depth, "effect": action,
                    "rows": n, "via": via, "fk_edges": len(cols)})
        # Only a CASCADE propagates further: SET NULL/SET DEFAULT keep the child
        # row alive, and RESTRICT/NO ACTION abort the delete outright.
        if n and action == "CASCADE":
            _walk_cascade(cur, child,
                          f"SELECT c.* FROM {child} c WHERE {pred}",
                          depth + 1, path + (child,), out)


def preview() -> Dict[str, Any]:
    """What WOULD be deleted, per store, INCLUDING cascade effects. Writes nothing.

    The approval surface: retention is irreversible, so the counts have to be
    readable — and complete — before anyone enables it."""
    out: List[Dict[str, Any]] = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for p in POLICIES:
                row: Dict[str, Any] = {"store": p.table, "days": p.days,
                                       "basis": p.basis, "rows": 0,
                                       "present": False, "enabled": p.days > 0,
                                       "cascade": [], "cascade_rows": 0,
                                       "mutates_rows": 0, "blockers": []}
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
                    continue
                # Cascade accounting is advisory: a failure here must degrade the
                # preview, never break it.
                try:
                    if row["rows"]:
                        eff: List[Dict[str, Any]] = []
                        _walk_cascade(cur, p.table, _policy_rowset(p), 1,
                                      (p.table,), eff)
                        row["cascade"] = eff
                        row["cascade_rows"] = sum(
                            e["rows"] or 0 for e in eff if e["effect"] == "CASCADE")
                        row["mutates_rows"] = sum(
                            e["rows"] or 0 for e in eff
                            if e["effect"] in ("SET NULL", "SET DEFAULT"))
                        row["blockers"] = [
                            e for e in eff
                            if e["effect"] in ("RESTRICT", "NO ACTION") and e["rows"]]
                except Exception as exc:
                    conn.rollback()
                    row["cascade_error"] = str(exc).splitlines()[0][:120]
                out.append(row)
    finally:
        conn.close()

    own = sum(r["rows"] for r in out)
    casc = sum(r["cascade_rows"] for r in out)
    return {"enabled": ENABLED, "max_delete_per_pass": MAX_DELETE,
            "stores": out,
            # `total_rows` keeps its original meaning (own rows only) so existing
            # readers do not silently change behaviour; the honest number is
            # total_rows_including_cascade.
            "total_rows": own,
            "cascade_rows": casc,
            "total_rows_including_cascade": own + casc,
            "mutated_rows": sum(r["mutates_rows"] for r in out),
            "blocked_stores": [r["store"] for r in out if r["blockers"]],
            "protected": ["invoices", "payments", "orders", "audit_log",
                          "email_suppression"],
            "note": "Stores with an independent legal basis have no policy and "
                    "cannot be expired by this module.",
            "cascade_note": "max_delete_per_pass bounds the PARENT rows only; "
                            "ON DELETE CASCADE is applied by the database "
                            "afterwards and is not limited. Read "
                            "total_rows_including_cascade, not total_rows."}


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
