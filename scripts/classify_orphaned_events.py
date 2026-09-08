"""Classify the orphaned event backlog BEFORE anything is replayed.

WHY THIS EXISTS. `POST /agent-bus/drain` replays every orphaned event it can
handle. On the deployed instance that means real customer email: the 18
`order.shipped` events stranded since 2026-09-02 would each produce a "your
order has shipped" message, five days late, with no check that the statement is
still TRUE. An order that shipped on the 2nd and was since cancelled, returned,
or already told by another path would be told something false BY A GOVERNED
SYSTEM, which is worse than the silence it replaces.

The owner's decision (2026-09-07) was: classify first, decide after. This script
is the classification. It is READ ONLY -- it opens a read-only transaction, it
sends nothing, it replays nothing, and it changes no row.

    python -m scripts.classify_orphaned_events                  # local
    python -m scripts.classify_orphaned_events --target railway # production
    python -m scripts.classify_orphaned_events --target railway --json out.json

WHAT IT DECIDES, per event:

  REPLAY      the statement the notification would make is still true, the
              customer has not already been told, and the order is in a state
              consistent with the event.
  REVIEW      something disagrees -- the order moved on, the event is stale
              relative to the current status, or a later event supersedes it.
              A human reads these.
  WRITE_OFF   telling the customer now would be wrong or pointless: already
              notified, order cancelled, or the event is superseded by a
              terminal state the customer has already seen.

The recommendation is ADVISORY. Nothing here decides; §13 of the remediation
mandate puts that with a person, and the point of the three buckets is to make
the person's decision small rather than to make it for them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config as _config          # noqa: E402,F401  (loads .env)

# Statuses that make a "your order has shipped" note wrong rather than late.
_TERMINAL_CONTRADICTIONS = {"cancelled", "canceled", "refunded", "returned"}

# THE EVENT TYPES THAT REACH A CUSTOMER. Everything else is handled internally.
#
# CORRECTED after this script's first production run. It reported all 39 Railway
# orphans as REPLAY with the reason "no accepted notification exists for it" --
# true of `order.status_changed` only because that type never produces a
# notification at all. The VERDICT was right and the REASON was misleading, and
# a reader deciding on the strength of it would reasonably have expected 39
# customer emails where the real number is 18.
#
# `handle_order_status_changed` closes the order's milestone activity and
# nothing else: its email half was deliberately removed so that two senders with
# two idempotency stores could not each conclude the other had not sent. 18 of
# the 21 affected orders carry BOTH event types, so conflating them is precisely
# the double-notification that separation exists to prevent.
_CUSTOMER_FACING = {"order.created", "order.shipped", "order.delivered"}

SQL = """
WITH orphan AS (
    -- event_type and the entity live on `events`; event_queue carries only the
    -- delivery state. Joining rather than assuming: the first version of this
    -- query read q.event_type and did not exist.
    SELECT q.event_uuid, e.event_type, e.payload, e.created_at,
           e.entity_uuid AS order_id
      FROM event_queue q
      JOIN events e ON e.event_uuid = q.event_uuid
     WHERE q.status = 'orphaned'
        OR (q.status = 'pending' AND q.locked_at IS NULL
            AND e.created_at < now() - interval '24 hours')
)
SELECT o.event_uuid::text,
       o.event_type,
       o.created_at,
       o.order_id::text,
       ord.order_number,
       ord.status              AS order_status,
       ord.updated_at          AS order_updated_at,
       (SELECT count(*) FROM order_notifications n
         WHERE n.order_id = o.order_id AND n.event_type = o.event_type
           AND n.state = 'accepted')                AS already_notified,
       (SELECT count(*) FROM order_notifications n
         WHERE n.order_id = o.order_id AND n.state = 'accepted') AS notified_any
  FROM orphan o
  LEFT JOIN orders ord ON ord.order_id = o.order_id
 ORDER BY o.created_at, o.event_type
"""


def classify(row: dict) -> tuple[str, str]:
    """(bucket, why). The `why` is the whole point -- a bucket with no reason
    is an opinion, and the person deciding is entitled to the evidence."""
    et = row["event_type"]
    status = (row["order_status"] or "").lower()

    if et not in _CUSTOMER_FACING:
        return "REPLAY", ("internal handler only (closes the milestone "
                          "activity); replaying this contacts NOBODY")
    if row["order_id"] is None:
        return "REVIEW", "the event names no order this database can resolve"
    if row["order_number"] is None:
        return "WRITE_OFF", "the order no longer exists; there is nobody to tell"
    if row["already_notified"]:
        return "WRITE_OFF", (f"the customer has already had an accepted "
                             f"{et} notification; replaying duplicates it")
    if status in _TERMINAL_CONTRADICTIONS:
        return "WRITE_OFF", (f"the order is now '{status}'. A '{et}' message "
                             f"would state something that is no longer true")
    if et == "order.shipped" and status in ("delivered",):
        return "REVIEW", ("the order has since been DELIVERED. 'Your order has "
                          "shipped' is stale but not false -- a human decides "
                          "whether a late shipping note still helps")
    if et == "order.shipped" and status not in ("shipped", "delivered"):
        return "REVIEW", (f"order status is '{status}', which does not agree "
                          f"with a shipped event")
    if et == "order.delivered" and status != "delivered":
        return "REVIEW", f"order status is '{status}', not delivered"
    return "REPLAY", (f"order status '{status}' agrees with {et}, and no "
                      f"accepted notification exists for it")


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.classify_orphaned_events",
        description="Read-only classification of the orphaned event backlog. "
                    "Sends nothing, replays nothing, changes nothing.")
    ap.add_argument("--target", choices=("railway",), default=None,
                    help="classify the deployed database instead of the "
                         "configured DSN. Railway is never a default.")
    ap.add_argument("--json", default="", help="also write the rows to this file")
    args = ap.parse_args()

    if args.target == "railway":
        dsn = os.getenv("RAILWAY_DB_URL", "")
        if not dsn:
            print("RAILWAY_DB_URL is not set", file=sys.stderr)
            return 2
        if "sslmode" not in dsn:
            dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
        label = "RAILWAY"
    else:
        dsn = os.getenv("DATABASE_URL") or os.getenv("DB_DSN") or ""
        label = "LOCAL"

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.rollback()
        conn.close()

    print(f"TARGET: {label}   orphaned events: {len(rows)}\n")
    buckets: dict[str, list] = {"REPLAY": [], "REVIEW": [], "WRITE_OFF": []}
    for r in rows:
        b, why = classify(r)
        r["bucket"], r["why"] = b, why
        buckets[b].append(r)

    for b in ("REPLAY", "REVIEW", "WRITE_OFF"):
        # ASCII only: this runs in a Windows console under cp1252, where a
        # box-drawing character raises UnicodeEncodeError and takes the whole
        # report with it. A classification tool that crashes on its own heading
        # is not available at the moment it is needed.
        print(f"-- {b}  ({len(buckets[b])}) " + "-" * 44)
        for r in buckets[b]:
            print(f"  {r['event_type']:22} order {r['order_number'] or '?':<14} "
                  f"status={r['order_status'] or '?':<12} "
                  f"created={r['created_at']:%Y-%m-%d}")
            print(f"      {r['why']}")
        print()

    # THE NUMBER A PERSON IS ACTUALLY DECIDING ABOUT. "39 events" and "18
    # customer emails" are different quantities, and only one of them is a
    # business consequence.
    reach = [r for r in buckets["REPLAY"] if r["event_type"] in _CUSTOMER_FACING]
    orders = {r["order_number"] for r in reach}
    print(f"CUSTOMER IMPACT OF REPLAYING THE 'REPLAY' BUCKET: "
          f"{len(reach)} message(s) to {len(orders)} order(s).")
    print(f"  The other {len(buckets['REPLAY']) - len(reach)} REPLAY event(s) "
          f"are internal and contact nobody.")
    print("  order_notifications is keyed UNIQUE(order_id, event_type), so a "
          "second drain re-sends nothing.\n")
    print("NOTHING WAS SENT, REPLAYED OR CHANGED. The decision is a person's:")
    print("  REPLAY    -> POST /agent-bus/drain, then close the event_orphaned")
    print("               alert with closure_evidence naming what was sent.")
    print("  WRITE_OFF -> record the decision and the reason on the alert; the")
    print("               events stay visible, written off rather than aged out.")
    print("  REVIEW    -> read these individually first.")

    if args.json:
        Path(args.json).write_text(
            json.dumps(rows, indent=2, default=str), encoding="utf-8")
        print(f"\nrows written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
