"""Apply the declared migrations, in order, exactly once.

ONE ORDERED MANIFEST. `deploy_state.REQUIRED_MIGRATIONS` already declared the
order and already detected drift; nothing applied them. So the order lived in
Python, the application lived in a human's shell history, and the two could not
disagree only because one of them did not exist. Migrations were applied by
hand, out of band, and the production database has been running broken erasure
semantics for that reason.

Idempotent: every file is expected to be re-runnable (CREATE ... IF NOT EXISTS,
CREATE OR REPLACE), and `schema_migrations` records what has been applied so a
re-run is a no-op rather than a gamble.

    python -m scripts.migrate              # apply what is missing
    python -m scripts.migrate --check      # exit 1 if anything is missing
    python -m scripts.migrate --dry-run    # print the plan
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings          # noqa: E402
from app.core.deploy_state import REQUIRED_MIGRATIONS  # noqa: E402

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

_LEDGER = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename    text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    applied_by  text NOT NULL DEFAULT session_user
);
"""


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report drift and exit non-zero; change nothing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reapply-changed", action="store_true",
                    help="re-run migrations whose file changed after being "
                         "applied. Correct during development, where files are "
                         "idempotent and edited in place. NEVER the answer in "
                         "production: there, an applied migration is immutable "
                         "and a change is a NEW file.")
    ap.add_argument("--target", choices=["railway"],
                    help="apply to Railway instead of the configured DSN. "
                         "Explicit and opt-in — production is never a default.")
    args = ap.parse_args()

    # WHY THIS OPTION EXISTS. This tool records every migration it applies, with
    # a checksum, and can therefore answer "has X been applied here?". It could
    # only ever talk to the local database, so every production migration went
    # through pgAdmin instead — which applies the SQL perfectly and records
    # nothing. A correct path that is harder to take than the incorrect one does
    # not get taken.
    import os
    if args.target == "railway":
        dsn = (os.getenv("RAILWAY_DB_URL") or "").strip()
        if not dsn:
            raise SystemExit("RAILWAY_DB_URL is not set")
        if "sslmode" not in dsn.lower():
            raise SystemExit(
                "RAILWAY_DB_URL has no sslmode. libpq defaults to 'prefer', "
                "which silently downgrades to plaintext. Append ?sslmode=require.")
        print(f"TARGET: RAILWAY ({dsn.split('@')[-1].split('/')[0]})")
        if not (args.check or args.dry_run):
            print("  applying migrations to PRODUCTION.")
    else:
        dsn = get_settings().db_dsn

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    applied, changed, adopt, missing_files = [], [], [], []
    try:
        with conn.cursor() as cur:
            cur.execute(_LEDGER)
            cur.execute("SELECT filename, checksum FROM schema_migrations")
            have = dict(cur.fetchall())

        for name in REQUIRED_MIGRATIONS:
            path = SQL_DIR / name
            if not path.exists():
                missing_files.append(name)
                continue
            csum = _checksum(path)
            if name in have:
                # Rows recorded before checksums were tracked carry ''. Treat
                # that as "unknown", adopt the current hash, and only report
                # drift when a KNOWN hash differs — otherwise the first run of
                # this tool reports every historical migration as changed, which
                # is noise that trains people to ignore the real signal.
                if not have[name]:
                    adopt.append((name, csum))
                elif have[name] != csum:
                    changed.append(name)
                continue
            applied.append((name, path, csum))

        if missing_files:
            print(f"MISSING FILES: {', '.join(missing_files)}", file=sys.stderr)
        for name in changed:
            print(f"CHANGED SINCE APPLIED: {name}", file=sys.stderr)
        if changed and args.reapply_changed and not (args.check or args.dry_run):
            for name in changed:
                path = SQL_DIR / name
                applied.append((name, path, _checksum(path)))
            changed = []

        if adopt and not (args.check or args.dry_run):
            with conn.cursor() as cur:
                for name, csum in adopt:
                    cur.execute("UPDATE schema_migrations SET checksum=%s "
                                " WHERE filename=%s", (csum, name))
            print(f"adopted checksums for {len(adopt)} pre-existing migration(s)")

        if args.check or args.dry_run:
            for name, _, _ in applied:
                print(f"would apply: {name}")
            bad = bool(missing_files) or (args.check and (applied or changed))
            if args.check and not bad:
                print("schema is current")
            return 1 if bad else 0

        for name, path, csum in applied:
            print(f"applying {name} ...", flush=True)
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO schema_migrations (filename, checksum) "
                    "VALUES (%s,%s) ON CONFLICT (filename) DO UPDATE "
                    "SET checksum=EXCLUDED.checksum, applied_at=now()",
                    (name, csum))
        print(f"done — {len(applied)} applied, {len(have)} already present")
        return 1 if missing_files else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
