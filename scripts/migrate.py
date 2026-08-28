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
from app.core.deploy_state import (                      # noqa: E402
    REQUIRED_MIGRATIONS, SqlDispositionError, classify_sql_corpus,
    require_disposition, residual_transaction_control,
    strip_outer_transaction)

from app.core.artifact_paths import SQL_DIR             # noqa: E402

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
    # AUTOCOMMIT OFF. It used to be on, which meant the migration DDL committed
    # and the ledger INSERT committed separately -- a crash between them left a
    # migration applied and unrecorded, the one state `migrate --check` cannot
    # distinguish from "never applied". Off, plus strip_outer_transaction()
    # below, makes "schema and ledger, or neither" true rather than intended.
    conn.autocommit = False
    applied, changed, unverifiable, missing_files = [], [], [], []
    try:
        with conn.cursor() as cur:
            cur.execute(_LEDGER)
            # Always qualified. This database has THREE tables named
            # schema_migrations (public, auth, realtime -- the last two are
            # Supabase's). Unqualified resolution happens to be correct today
            # only because neither of the others is on the search_path.
            cur.execute("SELECT filename, checksum "
                        "FROM public.schema_migrations")
            have = dict(cur.fetchall())
        conn.commit()

        # ---- THE COMPLETENESS INVARIANT, before anything is applied -------
        # A file in neither manifest is an ERROR, not a default. Checked here
        # because this is the last point at which an unclassified artifact can
        # be stopped before it reaches a database.
        corpus = classify_sql_corpus()
        if corpus.get("present") and not corpus["ok"]:
            for f in corpus["unclassified"]:
                print(f"UNCLASSIFIED: {f} -- add it to REQUIRED_MIGRATIONS or "
                      f"OUT_OF_BAND_SQL", file=sys.stderr)
            for f in corpus["both"]:
                print(f"DOUBLE DISPOSITION: {f}", file=sys.stderr)
            for f in corpus["missing_declared"]:
                print(f"DECLARED BUT ABSENT FROM DISK: {f}", file=sys.stderr)
            for f in corpus["missing_out_of_band"]:
                print(f"CLASSIFIED BUT ABSENT FROM DISK: {f}", file=sys.stderr)
            print("REFUSING TO PROCEED -- the SQL corpus is not fully "
                  "classified.", file=sys.stderr)
            return 1
        if not corpus.get("present"):
            print("note: sql/ not present -- classification not evaluated "
                  "(this is not the same as clean)")

        for name in REQUIRED_MIGRATIONS:
            path = SQL_DIR / name
            if not path.exists():
                missing_files.append(name)
                continue
            csum = _checksum(path)
            if not csum:                       # cannot happen; assert anyway
                print(f"REFUSING: empty checksum computed for {name}",
                      file=sys.stderr)
                return 1
            if name in have:
                # Rows recorded before checksums were tracked carry ''.
                #
                # THIS NO LONGER ADOPTS TODAY'S HASH. It used to, so the first
                # run would not report every historical migration as changed --
                # reasonable-sounding, and a fabrication: writing today's hash
                # into a 2026-08-05 row records "this is the content applied on
                # that date", which nobody knows. The specification's §8.6 is
                # explicit that `applied: true, checksum: unknown` is strictly
                # more truthful than a confident wrong hash.
                #
                # So an empty checksum is now reported as PERMANENTLY
                # UNVERIFIABLE and left exactly as recorded. It is not drift --
                # drift means a known hash differs -- and it does not block.
                if not have[name]:
                    unverifiable.append(name)
                elif have[name] != csum:
                    changed.append(name)
                continue
            applied.append((name, path, csum))

        if missing_files:
            print(f"MISSING FILES: {', '.join(missing_files)}", file=sys.stderr)
        for name in changed:
            print(f"CHANGED SINCE APPLIED: {name}", file=sys.stderr)
        for name in unverifiable:
            print(f"CHECKSUM UNVERIFIABLE (recorded empty; left as recorded): "
                  f"{name}")
        if changed and args.reapply_changed and not (args.check or args.dry_run):
            for name in changed:
                path = SQL_DIR / name
                applied.append((name, path, _checksum(path)))
            changed = []

        if args.check or args.dry_run:
            for name, _, _ in applied:
                print(f"would apply: {name}")
            bad = bool(missing_files) or (args.check and (applied or changed))
            if args.check and corpus.get("present") and not corpus["ok"]:
                bad = True
            if args.check and not bad:
                print("schema is current")
            return 1 if bad else 0

        for name, path, csum in applied:
            # THE PATH BOUNDARY. migrate.py applies governed migrations and
            # nothing else. Without this the manifest is documentation; the
            # refusal is what makes the two paths real.
            try:
                require_disposition(name, "governed")
            except SqlDispositionError as exc:
                print(f"REFUSING: {exc}", file=sys.stderr)
                conn.rollback()
                return 1

            print(f"applying {name} ...", flush=True)
            body, had_own_txn = strip_outer_transaction(
                path.read_text(encoding="utf-8"))

            # FAIL CLOSED RATHER THAN PROMISE WHAT WE CANNOT DELIVER. If any
            # top-level BEGIN/COMMIT survives, this file can still end the
            # transaction mid-way and the ledger row would land in a new one --
            # the exact window this loop exists to close. Refusing is the only
            # honest answer: an unverified guarantee is worse than none,
            # because it stops people looking.
            residual = residual_transaction_control(body)
            if residual:
                print(f"REFUSING {name}: transaction control survives "
                      f"stripping ({', '.join(residual)}); it cannot be applied "
                      f"atomically with its ledger row.", file=sys.stderr)
                conn.rollback()
                return 1
            try:
                with conn.cursor() as cur:
                    # ONE TRANSACTION for the schema change AND its ledger row.
                    # The file's own BEGIN/COMMIT is stripped first (22 of 34
                    # declared migrations carry one) because otherwise the
                    # file's COMMIT ends this transaction and the ledger insert
                    # lands in a new one -- reopening the very window this
                    # closes. Every declared migration was checked for
                    # statements PostgreSQL forbids inside a transaction block
                    # (CREATE INDEX CONCURRENTLY, VACUUM, ALTER TYPE ADD VALUE,
                    # REINDEX): there are none, so all 34 can be atomic. A
                    # future migration needing one of those must be applied
                    # deliberately outside this loop, not by relaxing it here.
                    cur.execute(body)
                    cur.execute(
                        "INSERT INTO public.schema_migrations "
                        "  (filename, checksum) VALUES (%s,%s) "
                        "ON CONFLICT (filename) DO UPDATE "
                        "  SET checksum=EXCLUDED.checksum, applied_at=now()",
                        (name, csum))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                print(f"FAILED: {name} rolled back -- schema and ledger are "
                      f"both unchanged: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                return 1
            if had_own_txn:
                print(f"   (file's own BEGIN/COMMIT stripped -- this runner "
                      f"owns the transaction)")
        print(f"done -- {len(applied)} applied, {len(have)} already present")
        return 1 if missing_files else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
