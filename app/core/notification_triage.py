"""Notification Triage — conscious orchestration of the alert backlog.

THE PROBLEM
-----------
Every DB change emits an event that fans out to ~12 agent service-account inboxes
(channel='agent_inbox') AND to the user (channel='in_app'). Nothing consumes or
*resolves* those rows, so "unread" grows without bound (≈7.8k and climbing). The
only control was running `sql/mark_old_notifications_read_8k.sql` by hand — a blunt
sweep that hides the count, discards genuinely-actionable signal, and refills the
next day.

WHAT THIS DOES (the Notifications/Orchestrator agent reading non-critical alerts
and taking positive action — digest + auto-read, instead of blanket-marking):

  Pass A — agent-inbox receipts.  An agent_inbox row is the agents' own mail. It is
    "resolved" once the agent has nothing left to do: the event_queue row is no
    longer pending, OR no Python handler subscribes to that event_type (it will
    never be actioned). Mark those read; leave the genuine bus worklist (pending +
    a registered handler) for the consumer.

  Pass B — informational digest.  In-app FYI events (`*.created`, `*.updated`,
    `account.updated`, `invoice_created`, `payment_created`, `invoice_paid`, …) get
    rolled up into ONE digest notification per recipient per day (counts preserved
    in metadata), then the originals are marked read. 642 "account updated" → 1.

  Pass C — stale-actionable cleanup.  In-app ACTIONABLE alerts (invoice.overdue,
    lead.scored) are re-validated against live entity state; if the invoice was
    paid / the lead converted-or-disqualified, the alert is resolved. Still-actionable
    alerts are LEFT unread — that is the legitimate human/agent worklist.

  Left untouched: fresh actionable alerts + CRITICAL (`supervisor.alert`). The
  dashboard "UNREAD ALERTS" then reflects real work (tens), not fan-out noise.

SAFETY / GOVERNANCE
-------------------
  • Opt-in: no-op unless NOTIF_TRIAGE_ENABLED=1.
  • Dry-run by default: computes and reports what WOULD change but writes nothing
    unless NOTIF_TRIAGE_APPLY=1 (mirrors AGENT_BUS_AUTOSEND).
  • Per-pass cap (NOTIF_TRIAGE_CAP) bounds a single run.
  • A digest is always created/updated in the SAME transaction that marks its
    originals read — visibility is never dropped without a rollup standing in.
  • Idempotent: re-running folds into the same per-recipient/day digest; already
    read rows are untouched.

CONFIG (env)
  NOTIF_TRIAGE_ENABLED   0     master on/off (scheduled tick is a no-op when 0)
  NOTIF_TRIAGE_APPLY     0     1 = actually write; else dry-run (report only)
  NOTIF_TRIAGE_CAP    5000     max rows touched per pass per run
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("notification_triage")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("NOTIF_TRIAGE_ENABLED")
APPLY   = _flag("NOTIF_TRIAGE_APPLY")
CAP     = int(os.getenv("NOTIF_TRIAGE_CAP", "5000"))

# Retention (Pass F) — keep the volume bounded with plain DELETE (autovacuum
# reuses the space; NEVER VACUUM FULL on a tight volume — see the 2026-06-27
# crash). 0 disables a given retention.
NOTIF_RETENTION_DAYS = int(os.getenv("NOTIF_RETENTION_DAYS", "7"))   # delete READ notifications older than this
EVENT_RETENTION_DAYS = int(os.getenv("EVENT_RETENTION_DAYS", "14"))  # delete settled/old events + queue rows
# Belt-and-braces: any HANDLED (non-unread) notification CREATED this long ago
# is deleted regardless of when it was marked read. 0 disables.
NOTIF_HANDLED_MAX_AGE_DAYS = int(os.getenv("NOTIF_HANDLED_MAX_AGE_DAYS", "30"))
# Settled queue rows carry `last_attempt_at`, which agent_bus._resume_cutoff()
# reads as its RESUME WATERMARK — the answer to "where do I start again after a
# restart?". This pass used to delete them UNCONDITIONALLY, with no age bound,
# and it runs roughly every two hours: the watermark was being destroyed about
# twelve times a day. What survived was incidental ('cancelled' rows, matched by
# no retention path), so the bus resumed from an arbitrary old timestamp — and
# had those gone too, from now(), which is precisely the bug _resume_cutoff()
# was written to fix (50 events stranded, found 2026-07-25).
#
# This value must stay comfortably above agent_bus MAX_CATCHUP_HOURS (24h) —
# asserted at import below, not merely assumed. 0 disables this retention.
QUEUE_SETTLED_RETENTION_DAYS = int(os.getenv("QUEUE_SETTLED_RETENTION_DAYS", "7"))
if QUEUE_SETTLED_RETENTION_DAYS and QUEUE_SETTLED_RETENTION_DAYS * 24 <= 24:
    logging.getLogger("notification_triage").warning(
        "[triage] QUEUE_SETTLED_RETENTION_DAYS=%s is not longer than agent_bus "
        "MAX_CATCHUP_HOURS (24h); the resume watermark may be deleted before "
        "the consumer can use it.", QUEUE_SETTLED_RETENTION_DAYS)

_UNREAD = ("pending", "sent", "unread")

# Materiality floor for "is this invoice still worth chasing" — matches agent_bus.
MATERIAL_BALANCE = 50.0

# ── Severity classification (event_type → tier) ─────────────────────────────────
# Anything not explicitly actionable/critical is treated as INFORMATIONAL (FYI):
# that default is what makes the digest drain the long tail of `*.created` /
# `*.updated` chatter without enumerating every type.
CRITICAL_TYPES = {
    "supervisor.alert",
}
ACTIONABLE_TYPES = {
    "activity.overdue_flagged",
    "invoice.overdue", "invoice_overdue",
    "lead.scored",
}


def classify(event_type: Optional[str]) -> str:
    if event_type in CRITICAL_TYPES:
        return "critical"
    if event_type in ACTIONABLE_TYPES:
        return "actionable"
    return "informational"


# ============================================================================
# PASS A — agent-inbox receipts
# ============================================================================

def _handler_types() -> List[str]:
    """Event types a Python handler subscribes to — their pending agent_inbox
    copies are the real bus worklist and must be LEFT for the consumer."""
    try:
        from app.core import agent_bus
        return list(agent_bus.HANDLERS.keys())
    except Exception:
        return []


# An agent_inbox row is resolvable unless it is still a live bus task:
# a pending queue row whose event_type has a registered handler.
_AGENT_INBOX_WHERE = """
    n.channel = 'agent_inbox'
    AND n.status = ANY(%(unread)s)
    AND NOT EXISTS (
        SELECT 1
        FROM   event_queue q
        JOIN   events e ON e.event_uuid = q.event_uuid
        WHERE  q.event_uuid = n.event_uuid
          AND  q.status = 'pending'
          AND  e.event_type = ANY(%(handler_types)s)
    )
"""


def _pass_a(cur, apply: bool) -> Dict[str, Any]:
    params = {"unread": list(_UNREAD), "handler_types": _handler_types(), "cap": CAP}
    cur.execute(f"SELECT count(*) FROM notifications n WHERE {_AGENT_INBOX_WHERE}", params)
    eligible = int(cur.fetchone()[0])
    affected = 0
    if apply and eligible:
        cur.execute(
            f"""UPDATE notifications n SET status='read', read_at=now()
                WHERE n.notification_uuid IN (
                    SELECT n2.notification_uuid FROM notifications n2
                    WHERE {_AGENT_INBOX_WHERE.replace('n.', 'n2.')}
                    LIMIT %(cap)s
                )""",
            params,
        )
        affected = cur.rowcount
    return {"pass": "agent_inbox_receipts", "eligible": eligible,
            "resolved": affected if apply else 0, "would_resolve": eligible}


# ============================================================================
# PASS B — informational digest (in_app)
# ============================================================================

def _informational_breakdown(cur) -> Dict[str, Dict[str, Any]]:
    """{employee_uuid: {'total': n, 'by_type': {...}, 'anchor': event_uuid}} for
    in-app FYI. `anchor` is one absorbed event_uuid, reused as the digest row's
    FK (notifications.event_uuid is NOT NULL) — no new event_type/migration."""
    cur.execute(
        """SELECT n.employee_uuid::text, e.event_type, count(*),
                  max(n.event_uuid::text) AS anchor
           FROM   notifications n
           JOIN   events e ON e.event_uuid = n.event_uuid
           WHERE  n.channel = 'in_app'
             AND  n.status = ANY(%(unread)s)
             AND  (n.metadata->>'kind' IS DISTINCT FROM 'digest')
             AND  NOT (e.event_type = ANY(%(actionable)s))
             AND  NOT (e.event_type = ANY(%(critical)s))
           GROUP BY 1, 2""",
        {"unread": list(_UNREAD),
         "actionable": list(ACTIONABLE_TYPES),
         "critical": list(CRITICAL_TYPES)},
    )
    out: Dict[str, Dict[str, Any]] = {}
    for emp, etype, n, anchor in cur.fetchall():
        rec = out.setdefault(emp, {"total": 0, "by_type": {}, "anchor": anchor})
        rec["total"] += int(n)
        rec["by_type"][etype] = int(n)
    return out


def _digest_body(breakdown: Dict[str, int], total: int) -> str:
    lines = [f"### 🧹 Notification digest — {total} informational updates rolled up", ""]
    for etype, n in sorted(breakdown.items(), key=lambda kv: -kv[1]):
        lines.append(f"- **{etype}** × {n}")
    lines.append("")
    lines.append("_Auto-summarised by the Notifications agent. Actionable and "
                 "critical alerts are kept separate._")
    return "\n".join(lines)


def _upsert_digest(cur, employee_uuid: str, by_type: Dict[str, int], total: int,
                   day: str, anchor: str) -> None:
    """One digest per (recipient, UTC day): fold re-runs into the same row."""
    cur.execute(
        """SELECT notification_uuid, metadata FROM notifications
           WHERE employee_uuid = %(emp)s::uuid AND channel='in_app'
             AND status = ANY(%(unread)s)
             AND metadata->>'kind' = 'digest'
             AND metadata->>'day' = %(day)s
           LIMIT 1""",
        {"emp": employee_uuid, "unread": list(_UNREAD), "day": day},
    )
    row = cur.fetchone()
    if row:
        prev = row[1] or {}
        prev_by = (prev.get("breakdown") or {}) if isinstance(prev, dict) else {}
        merged = dict(prev_by)
        for k, v in by_type.items():
            merged[k] = merged.get(k, 0) + v
        merged_total = sum(merged.values())
        cur.execute(
            """UPDATE notifications
               SET title=%(title)s, body=%(body)s, metadata=%(meta)s, created_at=now()
               WHERE notification_uuid=%(id)s""",
            {"id": row[0],
             "title": f"🧹 Notification digest ({merged_total} updates)",
             "body": _digest_body(merged, merged_total),
             "meta": json.dumps({"kind": "digest", "source": "notification_triage",
                                 "day": day, "breakdown": merged, "absorbed": merged_total})},
        )
    else:
        cur.execute(
            """INSERT INTO notifications
                 (employee_uuid, event_uuid, channel, status, title, body, metadata, created_at)
               VALUES (%(emp)s::uuid, %(anchor)s::uuid, 'in_app', 'pending', %(title)s, %(body)s,
                       %(meta)s, now())""",
            {"emp": employee_uuid, "anchor": anchor,
             "title": f"🧹 Notification digest ({total} updates)",
             "body": _digest_body(by_type, total),
             "meta": json.dumps({"kind": "digest", "source": "notification_triage",
                                 "day": day, "breakdown": by_type, "absorbed": total})},
        )


def _pass_b(cur, apply: bool) -> Dict[str, Any]:
    breakdown = _informational_breakdown(cur)
    recipients = len(breakdown)
    total = sum(r["total"] for r in breakdown.values())
    resolved = 0
    if apply and total:
        day = datetime.now(timezone.utc).date().isoformat()
        for emp, rec in breakdown.items():
            _upsert_digest(cur, emp, rec["by_type"], rec["total"], day, rec["anchor"])
            # Mark the originals read — same transaction as the digest upsert.
            cur.execute(
                """UPDATE notifications n SET status='read', read_at=now()
                   WHERE n.notification_uuid IN (
                       SELECT n2.notification_uuid
                       FROM notifications n2
                       JOIN events e ON e.event_uuid = n2.event_uuid
                       WHERE n2.employee_uuid = %(emp)s::uuid
                         AND n2.channel='in_app' AND n2.status = ANY(%(unread)s)
                         AND (n2.metadata->>'kind' IS DISTINCT FROM 'digest')
                         AND NOT (e.event_type = ANY(%(actionable)s))
                         AND NOT (e.event_type = ANY(%(critical)s))
                       LIMIT %(cap)s
                   )""",
                {"emp": emp, "unread": list(_UNREAD), "cap": CAP,
                 "actionable": list(ACTIONABLE_TYPES), "critical": list(CRITICAL_TYPES)},
            )
            resolved += cur.rowcount
    return {"pass": "informational_digest", "recipients": recipients,
            "would_digest": total, "digested": resolved if apply else 0}


# ============================================================================
# PASS C — stale-actionable cleanup (in_app)
# ============================================================================
# Re-validate in-app ACTIONABLE alerts against live entity state and resolve the
# ones that are no longer actionable. Still-actionable alerts are left unread.
# Set-based per entity type; unknown actionable entity types are left untouched.

_STALE_SQL = {
    # invoice.overdue is stale once paid/cancelled or balance below the floor.
    "invoice": """
        UPDATE notifications n SET status='read', read_at=now()
        FROM events e, accounting_invoice_pipeline v
        WHERE n.event_uuid = e.event_uuid AND e.entity_uuid = v.invoice_id
          AND n.channel='in_app' AND n.status = ANY(%(unread)s)
          AND e.event_type = ANY(%(types)s)
          AND ( v.payment_status NOT IN ('unpaid','partial')
                OR ROUND(v.computed_balance_due::numeric, 2) <= %(floor)s )
    """,
    # lead.scored is stale once the lead is no longer a workable hot lead — OR the
    # lead no longer exists at all (converted away / hard-deleted). LEFT JOIN so a
    # missing lead row (l.lead_id IS NULL) is treated as stale and resolved, instead
    # of lingering forever (the old inner join never matched a deleted lead).
    "lead": """
        UPDATE notifications n SET status='read', read_at=now()
        FROM events e
        LEFT JOIN leads l ON e.entity_uuid = l.lead_id
        WHERE n.event_uuid = e.event_uuid
          AND n.channel='in_app' AND n.status = ANY(%(unread)s)
          AND e.event_type = ANY(%(types)s)
          AND ( l.lead_id IS NULL
                OR COALESCE(l.score,0) < 70 OR COALESCE(l.converted,false)
                OR COALESCE(l.is_deleted,false)
                OR COALESCE(l.status,'') IN ('disqualified','converted') )
    """,
    # An activity_nudge (raised by the agent_bus activity.overdue_flagged handler)
    # is resolved once its activity is completed/closed or brought current — the
    # owner has actioned it, so the reminder closes itself.
    "activity_nudge": """
        UPDATE notifications n SET status='read', read_at=now()
        FROM activities a
        WHERE a.activity_id::text = (n.metadata->>'activity_id')
          AND n.channel='in_app' AND n.status = ANY(%(unread)s)
          AND n.metadata->>'kind' = 'activity_nudge'
          AND ( a.status <> 'open' OR a.due_at IS NULL OR a.due_at >= now() )
    """,
}
_STALE_TYPES = {
    "invoice":        ["invoice.overdue", "invoice_overdue"],
    "lead":           ["lead.scored"],
    "activity_nudge": [],
}


def _count_stale(cur, entity: str) -> int:
    # Reuse the UPDATE's predicate via a count: wrap the same FROM/WHERE.
    sql = _STALE_SQL[entity]
    # Turn the UPDATE…FROM…WHERE into SELECT count(*) by replacing the head.
    head, _, tail = sql.partition("WHERE")
    from_clause = head.split("FROM", 1)[1]
    cur.execute(
        f"SELECT count(*) FROM notifications n, {from_clause} WHERE {tail}",
        {"unread": list(_UNREAD), "types": _STALE_TYPES[entity], "floor": MATERIAL_BALANCE},
    )
    return int(cur.fetchone()[0])


def _pass_c(cur, apply: bool) -> Dict[str, Any]:
    detail: Dict[str, int] = {}
    total = 0
    for entity in _STALE_SQL:
        if apply:
            cur.execute(_STALE_SQL[entity],
                        {"unread": list(_UNREAD), "types": _STALE_TYPES[entity],
                         "floor": MATERIAL_BALANCE})
            n = cur.rowcount
        else:
            n = _count_stale(cur, entity)
        detail[entity] = n
        total += n
    return {"pass": "stale_actionable", "by_entity": detail,
            "resolved" if apply else "would_resolve": total}


# ── Pass D: de-dup re-emitted actionable alerts ─────────────────────────────────
# Actionable events (invoice.overdue, lead.scored, activity.overdue_flagged) are
# re-emitted on a schedule, so a single still-live entity accumulates one new
# notification per recipient per run — the count balloons (e.g. 24 overdue
# invoices → 576 unread) even though the signal is unchanged. Pass C only clears
# the ones that became stale (paid/converted/done); this collapses the surviving
# duplicates to the LATEST notification per (entity, recipient, type), keeping the
# alert live while removing the noise. Runs after Pass C so stale ones are gone first.
_DEDUP_SQL = """
    WITH ranked AS (
        SELECT n.notification_uuid,
               ROW_NUMBER() OVER (
                   PARTITION BY e.entity_uuid, n.employee_uuid, e.event_type
                   ORDER BY n.created_at DESC
               ) AS rn
        FROM notifications n
        JOIN events e ON e.event_uuid = n.event_uuid
        WHERE n.status = ANY(%(unread)s)
          AND e.event_type = ANY(%(types)s)
          AND COALESCE(n.metadata->>'kind','') NOT IN ('digest','ar_digest')
    )
    UPDATE notifications SET status='read', read_at=now()
    WHERE notification_uuid IN (SELECT notification_uuid FROM ranked WHERE rn > 1)
"""
_DEDUP_COUNT_SQL = """
    SELECT count(*) FROM (
        SELECT ROW_NUMBER() OVER (
                   PARTITION BY e.entity_uuid, n.employee_uuid, e.event_type
                   ORDER BY n.created_at DESC) AS rn
        FROM notifications n
        JOIN events e ON e.event_uuid = n.event_uuid
        WHERE n.status = ANY(%(unread)s)
          AND e.event_type = ANY(%(types)s)
          AND COALESCE(n.metadata->>'kind','') NOT IN ('digest','ar_digest')
    ) z WHERE rn > 1
"""


def _pass_d(cur, apply: bool) -> Dict[str, Any]:
    params = {"unread": list(_UNREAD), "types": sorted(ACTIONABLE_TYPES)}
    if apply:
        cur.execute(_DEDUP_SQL, params)
        n = cur.rowcount
    else:
        cur.execute(_DEDUP_COUNT_SQL, params)
        n = int(cur.fetchone()[0])
    return {"pass": "dedup_actionable",
            "collapsed" if apply else "would_collapse": n}


# ── Pass E: per-owner Accounts-Receivable digest ────────────────────────────────
# Even de-duped, each owner still carries one invoice.overdue alert per overdue
# invoice (e.g. 24 invoices → 24 alerts for one owner). Pass E rolls each owner's
# still-overdue invoices into ONE refreshable "AR summary" notification and marks
# the individual invoice.overdue alerts read. Self-healing: each tick re-validates
# the digest's invoices against live state (paid ones drop off; an emptied digest
# is resolved). One active AR digest per recipient (not per day) — refreshed, not
# stacked. Runs after Pass C/D so only genuinely-overdue alerts are folded in.

def _ar_overdue_by_owner(cur) -> Dict[str, Dict[str, Any]]:
    """recipient → {invoices:{invoice_number:{balance,days}}, anchor} from their
    still-overdue invoice.overdue alerts (live balance via the pipeline view)."""
    cur.execute(
        """
        SELECT n.employee_uuid::text,
               v.invoice_number,
               ROUND(v.computed_balance_due::numeric, 2)::float8 AS bal,
               (CURRENT_DATE - v.due_date::date)                  AS days,
               n.event_uuid::text
        FROM notifications n
        JOIN events e ON e.event_uuid = n.event_uuid
        JOIN accounting_invoice_pipeline v ON v.invoice_id = e.entity_uuid
        WHERE n.channel = 'in_app' AND n.status = ANY(%(unread)s)
          AND e.event_type IN ('invoice.overdue','invoice_overdue')
          AND COALESCE(n.metadata->>'kind','') NOT IN ('digest','ar_digest')
          AND v.payment_status IN ('unpaid','partial')
          AND ROUND(v.computed_balance_due::numeric, 2) > %(floor)s
        """,
        {"unread": list(_UNREAD), "floor": MATERIAL_BALANCE},
    )
    out: Dict[str, Dict[str, Any]] = {}
    for emp, inv_no, bal, days, anchor in cur.fetchall():
        rec = out.setdefault(emp, {"invoices": {}, "anchor": anchor})
        rec["invoices"][inv_no] = {"balance": float(bal), "days": int(days)}
    return out


def _ar_body(invoices: Dict[str, Dict[str, Any]], total: float) -> str:
    n = len(invoices)
    lines = [f"### 💰 Accounts-receivable summary — {n} overdue invoice"
             f"{'s' if n != 1 else ''}, ${total:,.2f} outstanding", ""]
    ordered = sorted(invoices.items(), key=lambda kv: -kv[1]["balance"])
    for inv_no, d in ordered[:20]:
        lines.append(f"- **{inv_no}** — ${d['balance']:,.2f}, {d['days']}d past due")
    if n > 20:
        lines.append(f"- …and {n - 20} more")
    lines.append("")
    lines.append("_Auto-summarised by the Accounting agent — one reminder per owner; "
                 "individual overdue alerts are rolled up here and refresh as invoices are paid._")
    return "\n".join(lines)


def _ar_meta(invoices: Dict[str, Dict[str, Any]], total: float) -> str:
    return json.dumps({"kind": "ar_digest", "source": "notification_triage",
                       "count": len(invoices), "total": round(total, 2),
                       "invoices": invoices})


def _ar_digest_key(emps: List[str]) -> str:
    """Stable dedup key for a recipient GROUP (owners sharing the same overdue set)."""
    return hashlib.md5((",".join(sorted(emps))).encode()).hexdigest()


def _write_ar_digest_group(cur, emps: List[str], invoices: Dict[str, Dict[str, Any]], anchor: str) -> None:
    """v2: ONE message for a group of owners that share the same overdue set, with
    every owner attached as a recipient (so it shows as one row, "N recipients").
    Operates on the BASE tables (the view's INSTEAD-OF INSERT would make a separate
    message per recipient). Upsert by dedup_key → refresh content, preserve read state."""
    total = sum(d["balance"] for d in invoices.values())
    title = f"💰 AR summary — {len(invoices)} overdue, ${total:,.0f} outstanding"
    key   = _ar_digest_key(emps)
    meta  = json.loads(_ar_meta(invoices, total)); meta["dedup_key"] = key

    cur.execute("""SELECT notification_uuid FROM notification_messages
                   WHERE metadata->>'kind'='ar_digest' AND metadata->>'dedup_key'=%s LIMIT 1""", (key,))
    row = cur.fetchone()
    if row:
        msg = row[0]
        cur.execute("""UPDATE notification_messages SET title=%s, body=%s, metadata=%s
                       WHERE notification_uuid=%s""",
                    (title, _ar_body(invoices, total), json.dumps(meta), msg))
    else:
        cur.execute("""INSERT INTO notification_messages (event_uuid, channel, title, body, metadata, created_at)
                       VALUES (%s::uuid, 'in_app', %s, %s, %s, now()) RETURNING notification_uuid""",
                    (anchor, title, _ar_body(invoices, total), json.dumps(meta)))
        msg = cur.fetchone()[0]
    for emp in emps:
        cur.execute("""INSERT INTO notification_recipients (notification_uuid, employee_uuid, channel, status, created_at)
                       VALUES (%s, %s::uuid, 'in_app', 'pending', now())
                       ON CONFLICT (notification_uuid, employee_uuid, channel) DO NOTHING""", (msg, emp))


def _revalidate_ar_digests(cur) -> int:
    """Self-heal: re-check each AR digest message's invoices against live state.
    Drop paid/settled ones; DELETE a digest (and its recipients) once nothing is
    overdue; refresh content otherwise. Operates on notification_messages."""
    cur.execute("SELECT notification_uuid, metadata FROM notification_messages WHERE metadata->>'kind'='ar_digest'")
    resolved = 0
    for mid, meta in cur.fetchall():
        invs = (meta or {}).get("invoices") or {}
        still: Dict[str, Dict[str, Any]] = {}
        if invs:
            cur.execute(
                """SELECT invoice_number, payment_status,
                          ROUND(computed_balance_due::numeric,2)::float8,
                          (CURRENT_DATE - due_date::date)
                   FROM accounting_invoice_pipeline WHERE invoice_number = ANY(%s)""",
                (list(invs.keys()),),
            )
            live = {r[0]: (r[1], float(r[2]), int(r[3])) for r in cur.fetchall()}
            for ino in invs:
                st = live.get(ino)
                if st and st[0] in ("unpaid", "partial") and st[1] > MATERIAL_BALANCE:
                    still[ino] = {"balance": st[1], "days": st[2]}
        if not still:
            cur.execute("DELETE FROM notification_messages WHERE notification_uuid=%s", (mid,))  # cascades recipients
            resolved += 1
        elif still != invs:
            total = sum(d["balance"] for d in still.values())
            meta2 = dict(meta); meta2["invoices"] = still
            meta2["count"] = len(still); meta2["total"] = round(total, 2)
            cur.execute("""UPDATE notification_messages SET title=%s, body=%s, metadata=%s
                           WHERE notification_uuid=%s""",
                        (f"💰 AR summary — {len(still)} overdue, ${total:,.0f} outstanding",
                         _ar_body(still, total), json.dumps(meta2), mid))
    return resolved


def _pass_e(cur, apply: bool) -> Dict[str, Any]:
    by_owner = _ar_overdue_by_owner(cur)
    owners = len(by_owner)
    folded = sum(len(r["invoices"]) for r in by_owner.values())
    if not apply:
        return {"pass": "ar_digest", "owners": owners, "would_fold": folded}

    # Clean up legacy per-recipient AR digests (pre-group, no dedup_key) so they
    # collapse into the new shared group messages.
    cur.execute("""DELETE FROM notification_messages
                   WHERE metadata->>'kind'='ar_digest' AND NOT (metadata ? 'dedup_key')""")

    # Group owners that share the SAME overdue set → one message, many recipients.
    groups: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for emp, rec in by_owner.items():
        sig = tuple(sorted(rec["invoices"].keys()))
        g = groups.setdefault(sig, {"emps": [], "invoices": rec["invoices"], "anchor": rec["anchor"]})
        g["emps"].append(emp)
    for g in groups.values():
        _write_ar_digest_group(cur, g["emps"], g["invoices"], g["anchor"])

    # Mark each owner's individual overdue alerts read — now folded into the digest.
    for emp in by_owner:
        cur.execute(
            """UPDATE notifications n SET status='read', read_at=now()
               FROM events e
               WHERE n.event_uuid = e.event_uuid
                 AND n.employee_uuid = %(emp)s::uuid AND n.channel='in_app'
                 AND n.status = ANY(%(unread)s)
                 AND e.event_type IN ('invoice.overdue','invoice_overdue')
                 AND COALESCE(n.metadata->>'kind','') NOT IN ('digest','ar_digest')""",
            {"emp": emp, "unread": list(_UNREAD)},
        )
    resolved = _revalidate_ar_digests(cur)
    return {"pass": "ar_digest", "owners": owners, "folded": folded,
            "groups": len(groups), "resolved_empty": resolved}


# ── Pass F: retention — bound the volume with plain DELETE ───────────────────────
# The structural fix for the volume creeping back: hard-delete history that no
# pass will ever surface again, so row counts (and therefore the on-disk files,
# via autovacuum reuse) stay bounded. ALWAYS plain DELETE — never VACUUM FULL on
# a tight volume (it rewrites the table and can crash-loop recovery on No-space).
#   • READ notifications older than NOTIF_RETENTION_DAYS  (the dominant grower)
#   • settled event_queue + legacy pending older than EVENT_RETENTION_DAYS
#   • events older than EVENT_RETENTION_DAYS no longer referenced anywhere
# Unread notifications and recent rows are always kept.

def _retention(cur, apply: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"pass": "retention",
                           "notif_days": NOTIF_RETENTION_DAYS, "event_days": EVENT_RETENTION_DAYS}
    verb = "deleted" if apply else "would_delete"

    # 1) READ notifications past the retention window (keyed on read_at), PLUS
    #    any handled row CREATED more than NOTIF_HANDLED_MAX_AGE_DAYS ago —
    #    covers rows whose read_at is recent because a bulk sweep marked them.
    n_sql_where = (
        "( (read_at IS NOT NULL AND read_at < now() - (%(d)s || ' days')::interval)"
        "  OR (%(h)s > 0 AND status NOT IN ('pending','sent','unread')"
        "      AND created_at < now() - (%(h)s || ' days')::interval) )")
    _params = {"d": NOTIF_RETENTION_DAYS, "h": NOTIF_HANDLED_MAX_AGE_DAYS}
    if apply:
        cur.execute(f"DELETE FROM notifications WHERE {n_sql_where}", _params)
        n_notif = cur.rowcount
    else:
        cur.execute(f"SELECT count(*) FROM notifications WHERE {n_sql_where}", _params)
        n_notif = int(cur.fetchone()[0])

    # 2) event_queue: settled rows past their window + legacy pending past theirs.
    #    COALESCE(last_attempt_at, created_at) is deliberate: 'completed' rows
    #    always carry last_attempt_at (written by agent_bus._complete_sync), but
    #    'superseded' rows are set elsewhere and may not. Without the fallback a
    #    NULL would make those rows immortal.
    _q = {"d": EVENT_RETENTION_DAYS, "s": QUEUE_SETTLED_RETENTION_DAYS}
    _settled = ("status IN ('completed','superseded') "
                "AND COALESCE(last_attempt_at, created_at) "
                "    < now() - (%(s)s || ' days')::interval")
    _stale_pending = ("status='pending' "
                      "AND created_at < now() - (%(d)s || ' days')::interval")
    if apply:
        cur.execute(f"DELETE FROM event_queue WHERE {_settled}", _q)
        nq = cur.rowcount
        cur.execute(f"DELETE FROM event_queue WHERE {_stale_pending}", _q)
        nq += cur.rowcount
    else:
        cur.execute(f"SELECT count(*) FROM event_queue "
                    f"WHERE ({_settled}) OR ({_stale_pending})", _q)
        nq = int(cur.fetchone()[0])

    # 3) events past the window, no longer referenced by any FK table
    ev_where = """e.created_at < now() - (%(d)s || ' days')::interval
        AND NOT EXISTS (SELECT 1 FROM event_queue q  WHERE q.event_uuid=e.event_uuid)
        AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.event_uuid=e.event_uuid)
        AND NOT EXISTS (SELECT 1 FROM workflow_runs w WHERE w.event_uuid=e.event_uuid)"""
    if apply:
        cur.execute(f"DELETE FROM events e WHERE {ev_where}", {"d": EVENT_RETENTION_DAYS})
        ne = cur.rowcount
    else:
        cur.execute(f"SELECT count(*) FROM events e WHERE {ev_where}", {"d": EVENT_RETENTION_DAYS})
        ne = int(cur.fetchone()[0])

    out[f"notifications_{verb}"] = n_notif
    out[f"event_queue_{verb}"] = nq
    out[f"events_{verb}"] = ne
    return out


# ============================================================================
# TICK
# ============================================================================

def run_triage_tick(force: bool = False, apply: Optional[bool] = None) -> Dict[str, Any]:
    """Sense → classify → resolve. Safe to call directly (scheduler / endpoint).
    force=True runs even when NOTIF_TRIAGE_ENABLED=0; apply overrides NOTIF_TRIAGE_APPLY
    for a single call (None = use the env default)."""
    if not ENABLED and not force:
        return {"enabled": False, "skipped": True}
    do_apply = APPLY if apply is None else bool(apply)

    conn = get_connection()
    try:
        before = _unread_count(conn)
        with conn.cursor() as cur:
            a = _pass_a(cur, do_apply)
            b = _pass_b(cur, do_apply)
            c = _pass_c(cur, do_apply)
            d = _pass_d(cur, do_apply)
            e = _pass_e(cur, do_apply)
            f = _retention(cur, do_apply)
        if do_apply:
            conn.commit()
        else:
            conn.rollback()
        after = _unread_count(conn)
    except Exception as exc:
        conn.rollback()
        logger.error(f"[notif_triage] tick failed: {exc}", exc_info=True)
        return {"enabled": ENABLED, "apply": do_apply, "error": str(exc)}
    finally:
        conn.close()

    summary = {"enabled": ENABLED, "apply": do_apply, "cap": CAP,
               "unread_before": before, "unread_after": after if do_apply else before,
               "passes": [a, b, c, d, e, f]}
    logger.info(f"[notif_triage] tick — apply={do_apply} before={before} "
                f"after={summary['unread_after']} passes={[p['pass'] for p in (a,b,c,d,e,f)]}")
    return summary


def _unread_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM notifications WHERE status = ANY(%s)", (list(_UNREAD),))
        return int(cur.fetchone()[0])


# ============================================================================
# Admin endpoints
# ============================================================================

router = APIRouter(tags=["notification-triage"])


@router.get("/notif-triage/status")
def notif_triage_status():
    return {"enabled": ENABLED, "apply": APPLY, "cap": CAP,
            "notif_retention_days": NOTIF_RETENTION_DAYS,
            "event_retention_days": EVENT_RETENTION_DAYS,
            "critical_types": sorted(CRITICAL_TYPES),
            "actionable_types": sorted(ACTIONABLE_TYPES),
            "policy": "everything else → informational digest + auto-read; "
                      "Pass F hard-deletes read notifications + settled/old events past retention"}


@router.post("/notif-triage/run-once")
async def notif_triage_run_once(apply: bool = False):
    """Drive one triage pass on demand. Defaults to dry-run; pass ?apply=true to
    actually resolve (still requires nothing — force=True so it runs while gated)."""
    import asyncio
    return await asyncio.to_thread(run_triage_tick, True, apply)
