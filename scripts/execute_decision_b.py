"""Execute the owner's disposition for the 18 orphaned order.shipped events.

    docs/governance/decisions_2026-09-07.md, Decision B

THE DECISION, verbatim: write off by default with durable reasoning; send only
where the evidence shows the customer still materially needs it; never
blind-replay.

    SEND       SO-2026-102219 -- the ONLY one of the 18 that has received no
               communication of any kind. No order confirmation, no shipping
               notice. That customer does not know the order exists.
    WRITE OFF  the other 17. The governed path has only the "your order has
               shipped" template, which today announces as news something that
               happened on 30 August. Sending it states something misleading.
               The reason is NOT "it probably arrived" -- these orders have not
               been touched since 2026-09-02, so the system's KNOWLEDGE stopped
               then, and delivery is an assumption about a state nobody tracks.

HOW THE WRITE-OFF IS RECORDED, and why here. `order_notifications` is the
subsystem's own idempotency ledger, keyed UNIQUE(order_id, event_type), and
`state='skipped'` is one of its two TERMINAL_STATES. A row written here means
notify() short-circuits on its next delivery with "already skipped -- no second
email", so the write-off is enforced by the same mechanism that prevents
duplicate sends rather than by anything bolted on beside it. It is also visible
in order detail, where a human looking at the order can see the decision.

WHY NOT MARK THE EVENTS. Setting event_queue rows to 'completed' would record
that they were processed, which is false. The events were not processed; a
person decided not to act on them. The ledger can say that; the queue cannot.

WHAT HAPPENS AFTER THIS RUNS. `POST /agent-bus/drain` becomes safe and exact:
the 21 order.status_changed events contact nobody (that handler has no email
half), the 17 written-off shipped events short-circuit on their terminal row,
and SO-2026-102219 -- the one with no ledger row -- sends. One email, through
the governed path, with no new code deployed.

    python -m scripts.execute_decision_b --target railway            # DRY RUN
    python -m scripts.execute_decision_b --target railway --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config as _config          # noqa: E402,F401  (loads .env)

SEND = "SO-2026-102219"

WRITE_OFF_REASON = (
    "Decision B, 2026-09-07 (docs/governance/decisions_2026-09-07.md): written "
    "off, not sent. The order.shipped event was orphaned on 2026-09-02 and the "
    "only available template announces the shipment as news, which it is no "
    "longer. NOT written off on the grounds that the parcel arrived -- the "
    "order has not been touched since 2026-09-02, so that is an assumption "
    "about a state the system stopped tracking. Decided by the owner; recorded "
    "here so the decision is visible on the order."
)

FIND = """
SELECT DISTINCT o.order_id::text, o.order_number
  FROM event_queue q
  JOIN events e ON e.event_uuid = q.event_uuid
  JOIN orders o ON o.order_id = e.entity_uuid
 WHERE q.status = 'orphaned' AND e.event_type = 'order.shipped'
 ORDER BY o.order_number
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.execute_decision_b",
        description="Record the 17 write-offs so a drain sends exactly one email.")
    ap.add_argument("--target", choices=("railway",), default=None)
    ap.add_argument("--apply", action="store_true",
                    help="write the rows. Without it, nothing is changed.")
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
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(FIND)
            orders = cur.fetchall()
            if not orders:
                print(f"TARGET: {label} -- no orphaned order.shipped events found.")
                return 0

            send = [o for o in orders if o[1] == SEND]
            hold = [o for o in orders if o[1] != SEND]
            print(f"TARGET: {label}   {'APPLYING' if args.apply else 'DRY RUN'}\n")
            print(f"  SEND      {len(send)}: " +
                  (", ".join(o[1] for o in send) or "NONE FOUND -- check the order number"))
            print(f"  WRITE OFF {len(hold)}: {', '.join(o[1] for o in hold[:4])}"
                  f"{' ...' if len(hold) > 4 else ''}\n")

            if not send:
                print("  REFUSING: " + SEND + " is not among the orphaned events. "
                      "The disposition names it explicitly; if it is absent the "
                      "situation has changed and a person should look.")
                return 1

            written = skipped_existing = 0
            for oid, num in hold:
                cur.execute(
                    """INSERT INTO order_notifications
                         (order_id, event_type, state, failure_reason)
                       VALUES (%s::uuid, 'order.shipped', 'skipped', %s)
                       ON CONFLICT (order_id, event_type) DO NOTHING
                       RETURNING notification_id""",
                    (oid, WRITE_OFF_REASON))
                if cur.fetchone():
                    written += 1
                else:
                    skipped_existing += 1

            # The one that must NOT get a row -- assert it, do not assume it.
            cur.execute("""SELECT state FROM order_notifications
                            WHERE order_id=%s::uuid AND event_type='order.shipped'""",
                        (send[0][0],))
            existing = cur.fetchone()
            print(f"  write-off rows created : {written}")
            print(f"  already had a row      : {skipped_existing}")
            print(f"  {SEND} ledger row      : "
                  f"{existing[0] if existing else 'NONE (correct -- it will send)'}")
            if existing:
                print(f"\n  WARNING: {SEND} already has a '{existing[0]}' row, so a "
                      f"drain will NOT send it. Investigate before draining.")

            if args.apply:
                conn.commit()
                print("\nCOMMITTED. `POST /agent-bus/drain` now sends exactly one "
                      "email, to " + SEND + ".")
            else:
                conn.rollback()
                print("\nROLLED BACK -- nothing changed. Re-run with --apply.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
