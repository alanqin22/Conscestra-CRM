"""Assert that no SECURITY DEFINER function is executable by PUBLIC.

WHY THIS EXISTS. A SECURITY DEFINER function runs with its owner's privileges,
so EXECUTE on one is a grant of the owner's reach for whatever that function
does. PostgreSQL grants EXECUTE to PUBLIC on every new function by default, so
the safe state is not the default state -- it has to be asserted.

Measured on 2026-10-16: 16 of production's 22 SECURITY DEFINER functions were
PUBLIC-executable, including credential-domain functions reachable by roles
holding no privilege on any auth table. They were closed on 2026-10-17. This
check is what stops that recurring.

WHY A SEPARATE CHECK AND NOT ONLY A DATABASE TRIGGER. An event trigger closes
a function at CREATE or ALTER time, and that covers most of the ways one
appears. Three paths escape it, each measured:

  * GRANT EXECUTE ... TO PUBLIC. PostgreSQL event triggers do not fire on
    GRANT, so an artifact that creates a function and then opens it cannot be
    stopped in the database.
  * A function in a schema the trigger is not scoped to.
  * Anything created BEFORE the trigger exists -- which is what pg_restore
    does, since the trigger is just another object in the restore stream.

So prevention and detection are different jobs and this is the detection half.
It is cheap, read-only, and belongs anywhere the database's state is being
accepted as correct: CI, post-deploy, post-restore, and on a schedule.

SCOPE. SECURITY DEFINER only. A SECURITY INVOKER function runs with the
CALLER's privileges, and hundreds of them are PUBLIC-executable by design;
closing those would be a blanket revocation of ordinary application behaviour.
This check must never be read as covering them.

EXIT CODES
  0  no SECURITY DEFINER function is executable by PUBLIC
  1  at least one is, or the check could not run over its full scope
  2  the facts could not be established -- no database, unreadable catalogue.
     NEVER treated as a pass: a check that could not run is not a check that
     passed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CLOSED, OPEN, UNDETERMINED = 0, 1, 2

# Signature -> reason. EMPTY BY DESIGN. A SECURITY DEFINER function that is
# meant to be callable by PUBLIC is a governance decision and belongs here in
# writing, with the reason. "It is already open" is not a reason.
PUBLIC_EXECUTE_ALLOWED: dict = {}

# Not ours. An extension owns its own functions and their privileges.
EXTENSION_SCHEMAS = (
    "pg_catalog", "information_schema", "extensions", "pgbouncer",
    "graphql", "graphql_public", "vault", "cron", "pg_toast",
)

_POPULATION = """
    SELECT n.nspname, p.proname,
           pg_get_function_identity_arguments(p.oid),
           p.proacl::text,
           p.proconfig IS NOT NULL,
           has_function_privilege('public', p.oid, 'EXECUTE'),
           coalesce((SELECT bool_or(a.grantee = 0
                     AND a.privilege_type = 'EXECUTE')
                     FROM aclexplode(p.proacl) a), false)
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE p.prosecdef
       AND (%s OR n.nspname = 'public')
       AND NOT (n.nspname = ANY(%s))
       AND n.nspname NOT LIKE 'pg\\_%%'
     ORDER BY n.nspname, p.proname, p.oid
"""


def _dsn(explicit: str | None) -> tuple[str | None, str]:
    """Resolve the target, and say WHERE it came from.

    The environment is consulted BEFORE the application settings, because
    postdeploy_verify invokes this as a subprocess with DB_DSN and
    DATABASE_URL overridden to the deployed target. Falling through to
    get_settings() first would risk verifying the local database and
    reporting it as the deployed one -- the single worst outcome for a check
    whose job is to say which database is safe. The source is returned so the
    output can name it.
    """
    if explicit:
        return explicit, "--dsn"
    for var in ("DEFINER_CHECK_DSN", "DATABASE_URL", "DB_DSN"):
        if os.getenv(var):
            return os.environ[var], var
    try:
        from app.core.config import get_settings
        return get_settings().db_dsn, "app settings"
    except Exception:
        return None, "none"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dsn", help="defaults to DEFINER_CHECK_DSN, then the "
                                  "configured application DSN")
    ap.add_argument("--json", action="store_true")
    # EVERY SCHEMA IS THE DEFAULT. A new schema must not become an
    # unmonitored authorization boundary because an operator forgot a flag.
    # The narrowed scope exists only to diagnose the event trigger's own
    # coverage, and it is reported as a FAILURE so it can never read as a pass.
    ap.add_argument("--public-only", action="store_true",
                    help="DIAGNOSTIC ONLY: restrict to schema public. Reported "
                         "as a failure; never use for verification")
    args = ap.parse_args(argv)
    all_schemas = not args.public_only

    dsn, dsn_source = _dsn(args.dsn)
    if not dsn:
        print("UNDETERMINED: no DSN available", file=sys.stderr)
        return UNDETERMINED
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
    except Exception as exc:
        print("UNDETERMINED: cannot connect: %s"
              % str(exc).strip().splitlines()[0], file=sys.stderr)
        return UNDETERMINED

    failures: list = []
    advisories: list = []
    rows: list = []
    dbname = None
    try:
        cur.execute("SELECT current_database()")
        dbname = cur.fetchone()[0]
        cur.execute(_POPULATION, (all_schemas, list(EXTENSION_SCHEMAS)))
        rows = cur.fetchall()
    except Exception as exc:
        print("UNDETERMINED: catalogue unreadable: %s"
              % str(exc).strip().splitlines()[0], file=sys.stderr)
        return UNDETERMINED
    finally:
        cur.close()
        conn.close()

    for nsp, name, idargs, acl, pinned, pub, pub_acl in rows:
        sig = "%s.%s(%s)" % (nsp, name, idargs or "")
        if pub or pub_acl:
            if sig in PUBLIC_EXECUTE_ALLOWED:
                advisories.append("%s is open by recorded exception: %s"
                                  % (sig, PUBLIC_EXECUTE_ALLOWED[sig]))
            else:
                failures.append(
                    "%s is executable by PUBLIC "
                    "(has_function_privilege=%s aclexplode=%s acl=%s)"
                    % (sig, pub, pub_acl, acl or "NULL"))
        # The two routes must agree. They diverge exactly when a NULL ACL is
        # being misread as "no grants" rather than "the built-in default",
        # which is the mistake this whole control exists to prevent.
        if acl is not None and bool(pub) != bool(pub_acl):
            failures.append("%s: privilege routes disagree (%s vs %s)"
                            % (sig, pub, pub_acl))
        if not pinned:
            advisories.append("%s has no search_path pin" % sig)

    # An empty census is not a pass.
    if not rows:
        failures.append("no SECURITY DEFINER function was examined -- the "
                        "census returned nothing, which is not a clean result")
    # A narrowed scope is not a pass.
    if not all_schemas:
        failures.append("--public-only was passed, so non-public schemas were "
                        "not examined; this cannot be reported as a pass")

    verdict = OPEN if failures else CLOSED
    if args.json:
        print(json.dumps({
            "database": dbname,
            "dsn_source": dsn_source,
            "scope": ("every non-extension schema" if all_schemas
                      else "schema public ONLY (diagnostic)"),
            "examined": len(rows),
            "verdict": "OPEN" if failures else "CLOSED",
            "exit": verdict,
            "failures": failures,
            "advisories": advisories,
        }, indent=1))
        return verdict

    print("SECURITY DEFINER PUBLIC-EXECUTE check: %s (dsn from %s), "
          "%d examined (%s)"
          % (dbname, dsn_source, len(rows),
             "all schemas" if all_schemas else "public only, DIAGNOSTIC"))
    for f in failures:
        print("  FAIL  %s" % f)
    for a in advisories[:10]:
        print("  note  %s" % a)
    if len(advisories) > 10:
        print("  note  ... and %d more advisories" % (len(advisories) - 10))
    print("  %s" % ("OPEN: %d problem(s)" % len(failures) if failures
                    else "CLOSED: none executable by PUBLIC"))
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
