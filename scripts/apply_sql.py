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


def _strip_outer_transaction(sql: str) -> tuple[str, bool]:
    """Remove the file's own outer BEGIN;/COMMIT; and report whether it had them.

    THE BUG THIS FIXES, found the first time this script ran. Our .sql files
    wrap themselves in BEGIN;…COMMIT; so they are atomic under psql and pgAdmin,
    which are autocommit by default. psycopg2 is NOT: it has already opened a
    transaction, so the file's BEGIN is a no-op that merely warns, and the
    file's COMMIT commits OUR transaction. The subsequent conn.rollback() then
    warns 'no transaction in progress' and does nothing.

    So `--dry-run` printed "ROLLED BACK — nothing changed" while having applied
    the file in full. A safety flag that silently does the dangerous thing is
    worse than no flag, and it is the same shape as everything else this feature
    was built to remove: a reassuring message with no mechanism behind it.

    Stripping the outer pair puts the transaction back under this script's
    control, so commit and rollback both mean what they say. The file is left
    unchanged on disk and stays correct under psql.
    """
    import re
    body = sql
    had = False
    m = re.match(r"\A(\s*(?:--[^\n]*\n|\s)*)BEGIN\s*;", body, re.IGNORECASE)
    if m:
        body = body[:m.start()] + m.group(1) + body[m.end():]
        had = True
    m = re.search(r"COMMIT\s*;\s*\Z", body, re.IGNORECASE)
    if m and had:
        body = body[:m.start()] + body[m.end():]
    return body, had


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
    print("NOT recorded in schema_migrations — this is a deliberate one-off, "
          "not a tracked migration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
