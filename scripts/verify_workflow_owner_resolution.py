"""Prove the DEPLOYED workflow write path resolves an accountable owner.

WHY THIS EXISTS. workflow_owner_resolution.sql fails SILENTLY if it is absent:
nothing crashes, the app keeps calling the old function, and unowned work
carries on being created with no alarm. "The migration applied" therefore
proves nothing about behaviour, and the only jobs that would exercise it
naturally run at 22:25/22:30 ET.

WHY NOT JUST RUN THOSE JOBS. `_run_emit_overdue_invoice_events` emits
invoice.overdue events, the bus consumes them within 30 seconds, and
handle_invoice_overdue sends dunning mail. Production runs autosend=True. A
test that emails real customers to find out whether a function resolves an
owner is not a test, it is an incident.

SO THIS EXERCISES THE FUNCTION DIRECTLY AND ROLLS BACK. Every probe runs
inside one transaction that is never committed. The rows it creates exist only
for the length of the check, and the script asserts its own residue is zero
before it exits.

    python -m scripts.verify_workflow_owner_resolution                  # local
    python -m scripts.verify_workflow_owner_resolution --target railway

EXIT CODE is what a deploy pipeline should gate on: 0 means the deployed write
path resolves an accountable owner and fails closed when it cannot.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config as _config          # noqa: E402,F401  (loads .env)

_ACTION = json.dumps({"action": "create_activity",
                      "params": {"activity_type": "Payment Reminder",
                                 "due_in_days": "1"}})


def _run(cur, entity_type: str, payload_owner: str | None = None):
    """Invoke the deployed function; return (event_uuid, entity_uuid, owner)."""
    ev, ent = str(uuid.uuid4()), str(uuid.uuid4())
    after = {"owner_id": payload_owner} if payload_owner else {}
    cur.execute(
        "SELECT workflow_execute_action(%s::jsonb, %s::uuid, %s, %s::uuid, %s::jsonb)",
        (_ACTION, ev, entity_type, ent, json.dumps({"after": after})))
    cur.execute("SELECT owner_id::text FROM activities WHERE related_id=%s::uuid", (ent,))
    row = cur.fetchone()
    return ev, ent, (row[0] if row else None)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.verify_workflow_owner_resolution",
        description="Exercise the deployed workflow write path inside a "
                    "transaction that is rolled back. Sends nothing.")
    ap.add_argument("--target", choices=("railway",), default=None,
                    help="verify the deployed database. Railway is never a default.")
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
    failures: list[str] = []
    planted: list[str] = []
    try:
        with conn.cursor() as cur:
            print(f"TARGET: {label}   (one transaction, rolled back at the end)\n")

            # 0. Is the migration even here? A clear answer beats a confusing
            #    failure three probes later.
            cur.execute("SELECT to_regclass('public.workflow_owner_routing')")
            if cur.fetchone()[0] is None:
                print("  MIGRATION ABSENT: workflow_owner_routing does not exist.")
                print("  The old function is still in force and unowned work is "
                      "still being created.")
                return 1

            # 1. Routing resolves to an ELIGIBLE owner for every declared type.
            cur.execute("SELECT entity_type, authority_role FROM workflow_owner_routing "
                        "ORDER BY entity_type")
            routes = cur.fetchall()
            print(f"  declared routes: {len(routes)}")
            for et, role in routes:
                cur.execute("SELECT fn_workflow_route_owner(%s)", (et,))
                owner = cur.fetchone()[0]
                mark = "ok " if owner else "!! "
                print(f"    {mark}{et:12} -> {role:4} -> "
                      f"{'resolved' if owner else 'NOTHING (would fail closed)'}")
                if not owner:
                    failures.append(f"{et} routes to {role} but resolves no eligible owner")

            # 2. Unowned entity -> the routed authority.
            ev, ent, owner = _run(cur, "invoice")
            planted += [ent]
            cur.execute("SELECT fn_owner_eligible(%s::uuid)", (owner,)) if owner else None
            elig = cur.fetchone()[0] if owner else False
            print(f"\n  invoice, no payload owner        -> "
                  f"{'owner ' + owner[:8] if owner else 'NULL'}  eligible={elig}")
            if not owner or not elig:
                failures.append("an unowned entity still produces work with no "
                                "accountable owner")

            # 3. A CUSTOMER CONTACT as owner must be refused and rerouted.
            cur.execute("SELECT contact_id::text FROM contacts LIMIT 1")
            got = cur.fetchone()
            if got:
                customer = got[0]
                ev, ent, owner2 = _run(cur, "invoice", payload_owner=customer)
                planted += [ent]
                print(f"  invoice, CUSTOMER as owner       -> "
                      f"{'owner ' + owner2[:8] if owner2 else 'NULL'}  "
                      f"(payload named {customer[:8]})")
                if owner2 == customer:
                    failures.append("a customer contact was carried through as the "
                                    "accountable owner")

            # 4. An unrouted type must FAIL CLOSED and record why.
            ev3, ent3, owner3 = _run(cur, "widget")
            cur.execute("SELECT count(*) FROM activities WHERE related_id=%s::uuid", (ent3,))
            made = cur.fetchone()[0]
            cur.execute("SELECT reason FROM workflow_action_exceptions "
                        "WHERE event_uuid=%s::uuid", (ev3,))
            exc = cur.fetchone()
            print(f"  unrouted 'widget'                -> activities={made}  "
                  f"exception={exc[0] if exc else 'NONE'}")
            if made or not exc:
                failures.append("an unrouted entity type did not fail closed with "
                                "a recorded exception")
    finally:
        conn.rollback()          # THE POINT. Nothing above is kept.
        # Prove the residue is zero rather than assuming the rollback worked.
        with conn.cursor() as cur:
            for ent in planted:
                cur.execute("SELECT count(*) FROM activities WHERE related_id=%s::uuid",
                            (ent,))
                if cur.fetchone()[0]:
                    failures.append(f"RESIDUE: probe row {ent[:8]} survived the rollback")
        conn.close()

    print()
    if failures:
        print("VERDICT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("VERDICT: PASS - the deployed write path resolves an accountable owner, "
          "refuses a customer contact, and fails closed when it cannot resolve.")
    print("         Nothing was committed; probe residue verified zero.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
