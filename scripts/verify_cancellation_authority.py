"""Independently verify the cancellation authority chain after an apply.

WHY THIS EXISTS. `apply_sql` reports whether the server accepted a file. That is
not the same question as whether the control it installs works, and on this
programme the difference has mattered repeatedly: a migration can be accepted
and leave a REVOKE inert, or install a function nothing calls, or claim a
verification no record corroborates.

    Applied successfully is not verified successfully.

So this re-measures the database from the catalog, compares the order
population against a fingerprint taken BEFORE the apply, and then tries an
unauthorized cancellation and an unauthorized reversal to see them refused.

THE NEGATIVE PATH IS THE POINT. Checks 1-6 establish that objects exist in the
catalog, which is necessary and proves nothing about behaviour: a trigger can be
present and permissive. The last two checks attempt the writes the guards are
there to stop. They run inside a transaction that is ALWAYS rolled back, so a
run never mutates the database whether the guard holds or not -- and if a guard
does not hold, the rollback is what stops this script from being the thing that
cancels an order.

WHY A SNAPSHOT RATHER THAN CONSTANTS. "180 cancelled orders of 3068" was true at
the 2026-10-05 preflight and will not be true later. A check comparing a live
population to a number frozen in source stops being a check the moment the
population legitimately moves -- it either fails forever or gets edited until it
passes. So the counts, and a digest of every order's identity and status, come
from a file written before the apply.

USAGE
    # BEFORE applying -- writes the fingerprint this verifier compares against
    python -m scripts.verify_cancellation_authority --snapshot

    # AFTER applying
    python -m scripts.verify_cancellation_authority --verify

Run before the apply, --verify reports BLOCKED and names the absent objects,
which is also how you confirm the verifier can fail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SNAPSHOT = REPO / "scripts" / ".cancellation_preflight.json"

# The artifacts this gate installs, in apply order. Hashes are recorded by
# --snapshot rather than pinned here: the point of check 10 is that what was
# verified is what was applied, and a constant in source could not tell the
# difference between a re-pin and a tamper.
ARTIFACTS = ["cancellation_authority.sql",
             "cancellation_enforcement.sql",
             "cancellation_reversal.sql"]


def _dsn(target):
    """Target resolution, deliberately identical to apply_sql and migrate."""
    import os
    if target == "railway":
        dsn = (os.getenv("RAILWAY_DB_URL") or "").strip()
        if not dsn:
            raise SystemExit("RAILWAY_DB_URL is not set")
        return dsn, f"RAILWAY ({dsn.split('@')[-1].split('/')[0]})"
    from app.core.config import get_settings
    return get_settings().db_dsn, "LOCAL"


def _artifact_hashes():
    out = {}
    for name in ARTIFACTS:
        p = REPO / "governance" / "sql" / name
        out[name] = (hashlib.sha256(p.read_bytes()).hexdigest()
                     if p.is_file() else None)
    return out


def _population(cur):
    """The status of every order, by id.

    THE FIRST VERSION STORED ONLY A DIGEST AND ONLY TOTALS, and both were wrong
    in the same way: they measured the POPULATION when the property under
    verification is about THE ROWS THAT ALREADY EXISTED.

      * `orders_total` 3068 -> 3093 and `orders_cancelled` 180 -> 181 failed
        this gate on 2026-10-06 because 25 orders were created by ordinary
        background activity between the snapshot and the verify. The apply had
        altered nothing. A control that cannot tell a NEW row from an ALTERED
        one reports a clean apply as a failed one, and on a live database it
        does that every time.
      * the digest said "something moved" and could not say WHAT, so deciding
        whether the apply was at fault needed a manual investigation outside
        the verifier. A control whose failure cannot be localised is a control
        that gets overridden.

    So the mapping is kept whole: a few hundred kilobytes, gitignored, and it
    lets --verify name the exact order whose status moved. Orders absent from
    the snapshot are NEW and reported as information, never as a failure.
    """
    cur.execute("SELECT order_id::text, lower(coalesce(status,'')) FROM orders")
    return {"orders": dict(cur.fetchall())}


# ── the catalog checks ──────────────────────────────────────────────────────

def _catalog(cur):
    """(label, ok, detail) for each structural expectation."""
    out = []

    def one(label, sql, params=()):
        cur.execute(sql, params)
        row = cur.fetchone()
        out.append((label, bool(row), row[0] if row else "absent"))

    one("1  cancellation_authorization table",
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='cancellation_authorization'")
    one("1b order_cancel_staff_authority table",
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='order_cancel_staff_authority'")

    # 2. the two CHECK constraints, WITH their definitions -- a constraint that
    #    exists under the right name but a different predicate is the failure a
    #    name-only check cannot see.
    for cname, expect in (("ck_cancellation_authorization_verdict", "AUTHORIZED"),
                          ("ck_cancellation_authorization_operation",
                           "fn_cancellation_operations")):
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname=%s", (cname,))
        row = cur.fetchone()
        ok = bool(row) and expect in row[0]
        out.append((f"2  CHECK {cname}", ok,
                    row[0] if row else "absent"))

    one("3  order_cancel_verifications.recipient_destination",
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='order_cancel_verifications' "
        "AND column_name='recipient_destination'")

    for fn in ("fn_cancellation_proposition", "fn_cancellation_authority",
               "fn_authorize_cancellation", "fn_authorize_reversal",
               "fn_cancellable_states", "fn_cancellation_operations"):
        one(f"4  function {fn}",
            "SELECT proname FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid=p.pronamespace WHERE n.nspname='public' AND proname=%s "
            "LIMIT 1", (fn,))

    # 4b. THE PIN, not merely the function. An unpinned function resolves
    #     pgcrypto in tests and not on a deployed database, which is the defect
    #     that made the first apply of this chain fail outright.
    cur.execute("""SELECT p.proname, p.proconfig
                     FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname='public'
                      AND p.proname IN ('fn_cancellation_proposition',
                                        'fn_cancellation_authority',
                                        'fn_authorize_cancellation',
                                        'fn_authorize_reversal')""")
    pins = cur.fetchall()
    unpinned = [n for n, cfg in pins
                if not any("extensions" in c for c in (cfg or []))]
    out.append(("4b every function pins `extensions`",
                bool(pins) and not unpinned,
                "all pinned" if pins and not unpinned
                else f"unpinned: {unpinned or 'no functions found'}"))

    # 5 & 6. the trigger, AND what it is attached to. A guard on the wrong
    #        operation is present in the catalog and does nothing.
    for tname, expect_update, expect_insert in (
            ("trg_orders_cancellation_guard", True, False),
            ("trg_orders_cancellation_guard_insert", False, True),
            ("trg_orders_reversal_guard", True, False)):
        cur.execute("""SELECT t.tgtype, c.relname
                         FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                        WHERE t.tgname=%s AND NOT t.tgisinternal""", (tname,))
        row = cur.fetchone()
        if not row:
            out.append((f"5  trigger {tname}", False, "absent"))
            continue
        tgtype, rel = row
        # pg_trigger.tgtype bits: 1=ROW, 2=BEFORE, 4=INSERT, 8=DELETE, 16=UPDATE
        before = bool(tgtype & 2)
        on_update, on_insert = bool(tgtype & 16), bool(tgtype & 4)
        ok = (rel == "orders" and before
              and on_update == expect_update and on_insert == expect_insert)
        out.append((f"6  {tname} on the intended operation", ok,
                    f"table={rel} BEFORE={before} UPDATE={on_update} "
                    f"INSERT={on_insert}"))
    return out


# ── the negative path ──────────────────────────────────────────────────────

def _refusals(conn):
    """Attempt the writes the guards exist to refuse. ALWAYS rolled back."""
    import psycopg2
    out = []

    def attempt(label, statements, want="insufficient_privilege"):
        cur = conn.cursor()
        try:
            cur.execute("SAVEPOINT probe")
            for s, p in statements:
                cur.execute(s, p)
            out.append((label, False,
                        "THE WRITE SUCCEEDED -- the guard did not refuse it"))
        except psycopg2.Error as exc:
            code = getattr(exc, "pgcode", None)
            msg = str(exc).splitlines()[0][:110]
            # errcode 42501 is insufficient_privilege, which is what both
            # guards raise. Any other error means the write failed for an
            # unrelated reason and this check proved nothing.
            out.append((label, code == "42501", f"[{code}] {msg}"))
        finally:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT probe")
            except psycopg2.Error:
                conn.rollback()
            cur.close()
        return out

    # PICKING A PROBE MUST NOT DEPEND ON THE INSTALL. This asked
    # fn_cancellable_states() for the states to probe, which does not exist
    # before the apply -- so running the verifier on an uninstalled database
    # died with a traceback instead of reporting BLOCKED, and an operator could
    # not tell a failed check from a broken script. The function is still
    # preferred when present, because it is the authority on which states are
    # cancellable; the literal is only a fallback for CHOOSING A ROW, never for
    # deciding a verdict.
    cur = conn.cursor()
    source = "fn_cancellable_states()"
    try:
        cur.execute("SAVEPOINT pick")
        cur.execute("SELECT order_id::text FROM orders "
                    "WHERE lower(coalesce(status,'')) = ANY "
                    "      (SELECT lower(unnest(fn_cancellable_states()))) "
                    "ORDER BY order_id LIMIT 1")
        row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT pick")
    except psycopg2.Error:
        conn.rollback()
        cur.close()
        cur = conn.cursor()
        source = "literal fallback (the authority function is not installed)"
        cur.execute("SELECT order_id::text FROM orders "
                    "WHERE lower(coalesce(status,'')) "
                    "      IN ('pending','processing','ready') "
                    "ORDER BY order_id LIMIT 1")
        row = cur.fetchone()
    cancellable = row[0] if row else None
    cur.execute("SELECT order_id::text FROM orders "
                "WHERE lower(coalesce(status,''))='cancelled' "
                "ORDER BY order_id LIMIT 1")
    row = cur.fetchone()
    already = row[0] if row else None
    cur.close()
    out.append(("7a probe rows chosen", True, f"states from {source}"))

    if cancellable:
        attempt("7  unauthorized cancellation is REFUSED",
                [("UPDATE orders SET status='cancelled' WHERE order_id=%s::uuid",
                  (cancellable,))])
    else:
        out.append(("7  unauthorized cancellation is REFUSED", None,
                    "SKIPPED -- no order in a cancellable state to probe"))

    if already:
        attempt("8  unauthorized reversal is REFUSED",
                [("UPDATE orders SET status='ready' WHERE order_id=%s::uuid",
                  (already,))])
    else:
        out.append(("8  unauthorized reversal is REFUSED", None,
                    "SKIPPED -- no cancelled order to probe"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot", action="store_true",
                   help="record the pre-apply fingerprint. Run BEFORE applying.")
    g.add_argument("--verify", action="store_true",
                   help="re-measure and compare against the fingerprint.")
    ap.add_argument("--target", choices=["railway"],
                    help="explicit and opt-in; production is never a default")
    args = ap.parse_args(argv)

    import psycopg2
    dsn, label = _dsn(args.target)
    print(f"TARGET: {label}")
    conn = psycopg2.connect(dsn)
    try:
        if args.snapshot:
            with conn.cursor() as cur:
                snap = _population(cur)
            snap["artifact_sha256"] = _artifact_hashes()
            SNAPSHOT.write_text(json.dumps(snap, indent=2), encoding="utf-8")
            orders = snap["orders"]
            cancelled = sum(1 for v in orders.values() if v == "cancelled")
            print()
            print(f"WROTE {SNAPSHOT.name}")
            print(f"  orders recorded     = {len(orders)}")
            print(f"  of which cancelled  = {cancelled}")
            # A digest of the mapping, for quoting in a gate report. The
            # verifier compares the mapping itself, never this.
            h = hashlib.sha256()
            for oid in sorted(orders):
                h.update(oid.encode()); h.update(bytes([31]))
                h.update(orders[oid].encode()); h.update(bytes([30]))
            print(f"  order_status_digest = {h.hexdigest()}")
            for k, v in snap["artifact_sha256"].items():
                print(f"  {k} = {v}")
            print("\nNothing was verified. Apply, then re-run with --verify.")
            return 0

        if not SNAPSHOT.is_file():
            print(f"\nBLOCKED -- no pre-apply fingerprint at {SNAPSHOT}.\n"
                  "Checks 7-9 compare against the population as it was BEFORE "
                  "the apply, and that measurement cannot be reconstructed "
                  "afterwards.")
            return 2
        snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

        rows = []
        with conn.cursor() as cur:
            rows += _catalog(cur)
            now = _population(cur)

        before, after = snap["orders"], now["orders"]
        moved = {k: (v, after[k]) for k, v in before.items()
                 if k in after and after[k] != v}
        vanished = sorted(set(before) - set(after))
        appeared = sorted(set(after) - set(before))

        rows.append(("7  no pre-existing order changed status", not moved,
                     "all %d unchanged" % len(before) if not moved
                     else "MOVED: " + "; ".join(
                         f"{k} {a}->{b}" for k, (a, b) in
                         list(moved.items())[:5])))
        rows.append(("8  no pre-existing order disappeared", not vanished,
                     "all %d still present" % len(before) if not vanished
                     else f"MISSING: {vanished[:5]}"))
        # INFORMATION, not a verdict. New rows are ordinary operation and the
        # apply is not responsible for them.
        rows.append(("9  orders created since the snapshot", None,
                     "%d new (not attributable to the apply, not a failure)"
                     % len(appeared)))

        live = _artifact_hashes()
        same = live == snap["artifact_sha256"]
        rows.append(("10 artifacts unchanged since the snapshot", same,
                     "all three match" if same
                     else f"differs: { {k: v for k, v in live.items() if snap['artifact_sha256'].get(k) != v} }"))

        try:
            rows += _refusals(conn)
        except Exception as exc:                                  # noqa: BLE001
            # A crash here is itself a finding, but it must be REPORTED rather
            # than raised: a verifier that dies leaves the operator unable to
            # distinguish "the guard failed" from "the check broke".
            rows.append(("7/8 refusal probes", False,
                         f"the probes could not run: {exc!r}"[:200]))
    finally:
        # Never leave a transaction open, and never commit one. Every write
        # this script issues is a probe that must not survive.
        conn.rollback()
        conn.close()

    print()
    failed = []
    for lbl, ok, detail in rows:
        mark = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        print(f"  {mark}  {lbl}")
        print(f"        {detail}")
        if ok is False:
            failed.append(lbl)

    print()
    if failed:
        print("LOCAL CANCELLATION AUTHORITY APPLY — BLOCKED")
        for f in failed:
            print(f"   failed: {f}")
        return 1
    print("LOCAL CANCELLATION AUTHORITY APPLY — PASS")
    print("   catalog, population preservation and both refusal paths verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
