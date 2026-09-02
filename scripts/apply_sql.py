"""Apply ONE .sql file to local or Railway, deliberately and visibly.

WHY THIS EXISTS

`scripts/migrate.py` applies the ORDERED MANIFEST — the files every database
must have — and records each with a checksum. That is the right tool for schema.

It is the wrong tool for a file that is deliberately NOT in the manifest, and
this codebase has one: `sql/verify_order_test_contacts.sql` flips
`is_email_verified` on a handful of seed contacts so live sends can be
exercised. Declaring it would assert that every database MUST have those people
emailable, which is false for a fresh environment and false for production.

Before this script the only way to run such a file against Railway was pgAdmin
— which applies the SQL perfectly and records nothing, tells you nothing, and
swallows the server's NOTICEs. That is how a migration verified one contact
instead of five and looked correct: the report it printed was never read,
because the tool being used could not show it.

So this script does the two things pgAdmin does not:

  * it PRINTS every NOTICE and WARNING the server raises, which for a
    self-reporting file like verify_order_test_contacts.sql IS the result; and
  * it states plainly that nothing was recorded in `schema_migrations`, so a
    one-off is never mistaken for a tracked migration.

It refuses to guess the target. `--target railway` is explicit and opt-in, the
same posture migrate.py takes: production is never a default.

USAGE
    python -m scripts.apply_sql sql/verify_order_test_contacts.sql
    python -m scripts.apply_sql sql/verify_order_test_contacts.sql --target railway
    python -m scripts.apply_sql sql/verify_order_test_contacts.sql --target railway --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2                                            # noqa: E402

from app.core.config import get_settings                   # noqa: E402  (loads .env)
from app.core.deploy_state import (                        # noqa: E402
    SqlDispositionError, disposition_of, require_disposition,
    strip_outer_transaction)


def _dsn(target: str | None) -> tuple[str, str]:
    """(dsn, label). Mirrors migrate.py's resolution, including its refusal to
    accept a Railway URL that would silently downgrade to plaintext."""
    if target == "railway":
        dsn = (os.getenv("RAILWAY_DB_URL") or "").strip()
        if not dsn:
            raise SystemExit("RAILWAY_DB_URL is not set")
        if "sslmode" not in dsn.lower():
            raise SystemExit(
                "RAILWAY_DB_URL has no sslmode. libpq defaults to 'prefer', "
                "which silently downgrades to plaintext. Append ?sslmode=require.")
        return dsn, f"RAILWAY ({dsn.split('@')[-1].split('/')[0]})"
    return get_settings().db_dsn, "LOCAL"


def _strip_outer_transaction(sql: str) -> "tuple[str, bool]":
    """Delegates to app.core.deploy_state.strip_outer_transaction.

    THE LOGIC MOVED, THE REASONING DID NOT. This rule was discovered here --
    our .sql files wrap themselves in BEGIN;...COMMIT;, psycopg2 has already
    opened a transaction, so the file's COMMIT committed OURS and `--dry-run`
    printed "ROLLED BACK -- nothing changed" while having applied the file in
    full. migrate.py needs exactly the same rule to make its schema-plus-ledger
    transaction atomic, and two copies of a rule this subtle is how the copies
    come to disagree. See the shared function for the full account."""
    return strip_outer_transaction(sql)

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="path to the .sql file")
    ap.add_argument("--target", choices=["railway"],
                    help="apply to Railway instead of the configured DSN. "
                         "Explicit and opt-in — production is never a default.")
    ap.add_argument("--dry-run", action="store_true",
                    help="run inside a transaction and ROLL BACK. The server's "
                         "NOTICEs still print, so a self-reporting file tells "
                         "you what it WOULD do without doing it.")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"no such file: {path}")
    # ---- THE PATH BOUNDARY ------------------------------------------------
    # apply_sql.py applies OUT-OF-BAND operations and nothing else. A governed
    # migration routed through here would change the database and leave no
    # ledger row -- which is precisely how the trg_fn_events_after_insert chain
    # reached production unrecorded, three times, with no mechanism noticing.
    #
    # Unclassified refuses too, and that is the point: the failure mode being
    # removed is not "the operator chose wrong", it is "nobody chose".
    try:
        require_disposition(path.name, "out_of_band")
    except SqlDispositionError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    sql, wrapped = _strip_outer_transaction(path.read_text(encoding="utf-8"))

    dsn, label = _dsn(args.target)
    print(f"TARGET: {label}")
    print(f"FILE:   {path}"
          + ("  (outer BEGIN/COMMIT stripped — this script owns the transaction)"
             if wrapped else ""))
    if args.dry_run:
        print("MODE:   DRY RUN — will roll back")
    elif args.target == "railway":
        print("MODE:   applying to PRODUCTION")
    print()

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        # The file's own BEGIN/COMMIT runs inside this transaction; psycopg2's
        # commit is what makes it durable, and rollback is what makes --dry-run
        # honest.
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception as exc:                               # noqa: BLE001
        conn.rollback()
        for n in conn.notices:
            print("  " + n.strip())
        print(f"\nFAILED — rolled back: {exc}")
        return 1
    finally:
        notices = list(conn.notices)
        conn.close()

    # THE POINT OF THIS SCRIPT. A file that reports on itself is only as useful
    # as the tool that shows you the report.
    if notices:
        print("server output:")
        for n in notices:
            print("  " + n.strip())
    else:
        print("server output: (none — this file raised no NOTICE)")

    print()
    if args.dry_run:
        print("ROLLED BACK — nothing changed.")
    else:
        print(f"APPLIED to {label}.")
        # Record what the schema looks like now, because a TOOL just
        # changed it. Anything that shifts the fingerprint WITHOUT
        # leaving one of these used neither door — which is the only
        # question this detector tries to answer.
        try:
            from app.core.deploy_state import record_schema_attestation
            att = record_schema_attestation("apply_sql", path.name, dsn=dsn)
            print(f"  schema attested on {att.get('database')!r}: "
                  f"{att.get('fingerprint')} "
                  f"({att.get('objects')} objects)"
                  if att.get("ok") else
                  f"  NOTE schema NOT attested on "
                  f"{att.get('database')!r}: {att.get('error')}")
        except Exception as exc:
            # Never fail an apply that succeeded. The cost is one
            # unexplained-looking drift next check — a false positive in
            # the safe direction.
            print(f"  NOTE could not attest the schema: {exc}")
    print(f"NOT recorded in schema_migrations — {path.name} is classified "
          f"'{disposition_of(path.name)}'. That classification is the "
          f"provenance record; the ledger deliberately has no row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
