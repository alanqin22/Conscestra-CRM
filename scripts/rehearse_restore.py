"""Rehearse the PRODUCTION restore path — the parts the nightly drill cannot.

WHAT THE NIGHTLY DRILL ALREADY PROVES
-------------------------------------
scripts/backup_railway.py restores every dump into an EMPTY scratch database and
compares all 200 tables. That is real evidence, and it is evidence about a case
that will never happen in an incident: production is not empty.

WHAT THIS REHEARSES INSTEAD
---------------------------
The three things a real restore does that the drill does not:

  1. --clean --if-exists against a POPULATED database. Every object is dropped
     and recreated. Dependency order, extensions, and objects the dump does not
     know about all behave differently here than on an empty target.

  2. Grant survival. The dump is taken --no-owner --no-acl, which STRIPS
     ownership and privileges. A restore therefore recreates every table owned
     by whoever ran it, with no grants at all. If that happens to production,
     `crm_app` loses SELECT on everything and the application is down —
     or, far worse, someone "fixes" it by pointing the app at the superuser
     and the entire privilege separation is silently gone.

  3. Restore-over-restore timing, which is the number an incident actually
     needs. The drill's 3.6s is a load into emptiness.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not restore into Railway. Production's volume free space is not
observable from SQL, and this project has already had a volume fill and
crash-loop recovery. Rehearsing a disaster by causing one is not a rehearsal.
The container here runs the same PostgreSQL 18 image as production and the same
pg_restore flags, so the mechanics are faithful; the network path and the
platform are not covered and are reported as such.

    python -m scripts.rehearse_restore                 # newest dump
    python -m scripts.rehearse_restore --dump <file>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import config as _config          # noqa: E402,F401

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(ROOT / "backups")))
PG_IMAGE = os.getenv("BACKUP_PG_IMAGE", "pgvector/pgvector:pg18")
CNAME = "crm_restore_rehearsal"
PORT = "55433"
DB = "rehearsal"
PW = "rehearse"


def _sh(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(list(args), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(args[:4])}\n{p.stderr[-700:]}")
    return p


def _psql(sql: str, db: str = DB) -> Tuple[int, str]:
    p = _sh("docker", "exec", CNAME, "psql", "-U", "postgres", "-d", db,
            "-tAc", sql)
    return p.returncode, (p.stdout or p.stderr).strip()


def _wait_ready(timeout: int = 90) -> None:
    """Two successful connections a second apart.

    pg_isready answers during initdb's TEMPORARY server, which then restarts —
    a single successful probe is not evidence the real server is up. That
    mistake once produced a printed RTO for a restore that never ran."""
    import socket
    ok = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(PORT)), timeout=2):
                ok += 1
                if ok >= 2:
                    time.sleep(1.0)
                    rc, _ = _psql("SELECT 1", db="postgres")
                    if rc == 0:
                        return
        except OSError:
            ok = 0
        time.sleep(1.0)
    raise SystemExit("container never became ready")


def _table_count(db: str = DB) -> int:
    rc, out = _psql("SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname='public' "
                    "AND c.relkind='r'", db)
    try:
        return int(out)
    except ValueError:
        return -1


def _restore(dump: Path, clean: bool) -> Tuple[float, int, str]:
    """Returns (seconds, returncode, tail-of-stderr)."""
    args = ["docker", "exec", CNAME, "pg_restore", "-U", "postgres",
            "-d", DB, "--no-owner", "--no-acl", "-j", "4"]
    if clean:
        args += ["--clean", "--if-exists"]
    args += ["/tmp/d.dump"]
    t0 = time.time()
    p = _sh(*args)
    return time.time() - t0, p.returncode, (p.stderr or "")[-400:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump")
    ap.add_argument("--keep", action="store_true",
                    help="leave the container running for inspection")
    a = ap.parse_args()

    dumps = sorted(BACKUP_DIR.glob("railway-*.dump"))
    if not dumps:
        raise SystemExit(f"no dumps in {BACKUP_DIR}")
    dump = Path(a.dump) if a.dump else dumps[-1]
    if not dump.exists():
        raise SystemExit(f"{dump} not found")

    print("PRODUCTION RESTORE REHEARSAL")
    print(f"  dump   : {dump.name} ({dump.stat().st_size/1048576:.1f} MB)")
    print(f"  image  : {PG_IMAGE}")
    print(f"  target : throwaway container — production is NOT touched\n")

    _sh("docker", "rm", "-f", CNAME)
    _sh("docker", "run", "-d", "--name", CNAME,
        "-e", f"POSTGRES_PASSWORD={PW}", "-p", f"{PORT}:5432", PG_IMAGE,
        check=True)
    try:
        _wait_ready()
        _sh("docker", "exec", CNAME, "createdb", "-U", "postgres", DB, check=True)
        _sh("docker", "cp", str(dump), f"{CNAME}:/tmp/d.dump", check=True)

        # ── PASS 1: populate (this is what the nightly drill does) ──────────
        print("PASS 1 — restore into an EMPTY database (the drill's case)")
        s1, rc1, err1 = _restore(dump, clean=False)
        n1 = _table_count()
        print(f"  {s1:.1f}s, rc={rc1}, {n1} public tables")
        if n1 <= 0:
            print(f"  restore produced no tables:\n{err1}")
            return 1

        # ── set up the production privilege model on the restored copy ──────
        print("\nSET UP the production privilege model on this copy")
        for sql in (
            "CREATE ROLE crm_app NOLOGIN NOSUPERUSER",
            "GRANT USAGE ON SCHEMA public TO crm_app",
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO crm_app",
            "REVOKE CREATE ON SCHEMA public FROM crm_app",
        ):
            rc, out = _psql(sql)
            if rc != 0 and "already exists" not in out:
                print(f"  warn: {sql[:44]} -> {out[:70]}")
        before_sel = _psql("SELECT has_table_privilege('crm_app',"
                           "'public.accounts','SELECT')")[1]
        before_cre = _psql("SELECT has_schema_privilege('crm_app','public',"
                           "'CREATE')")[1]
        print(f"  crm_app SELECT on accounts : {before_sel}")
        print(f"  crm_app CREATE on public   : {before_cre}   (must be false)")

        # ── PASS 2: THE REAL TEST ───────────────────────────────────────────
        print("\nPASS 2 — restore --clean --if-exists OVER the populated copy")
        print("  (this is what a production restore actually does)")
        s2, rc2, err2 = _restore(dump, clean=True)
        n2 = _table_count()
        print(f"  {s2:.1f}s, rc={rc2}, {n2} public tables")

        after_sel = _psql("SELECT has_table_privilege('crm_app',"
                          "'public.accounts','SELECT')")[1]
        after_cre = _psql("SELECT has_schema_privilege('crm_app','public',"
                          "'CREATE')")[1]
        role_ok = _psql("SELECT count(*) FROM pg_roles WHERE rolname='crm_app'")[1]

        print("\nGRANT SURVIVAL — the finding this rehearsal exists for")
        print(f"  crm_app role still exists  : {role_ok}")
        print(f"  SELECT on accounts  before : {before_sel}   after : {after_sel}")
        print(f"  CREATE on public    before : {before_cre}   after : {after_cre}")

        verdict = 0
        if after_sel != "t":
            print("\n  *** THE APPLICATION WOULD BE DOWN AFTER THIS RESTORE ***")
            print("  crm_app has no SELECT. --no-owner --no-acl strips privileges,")
            print("  and --clean drops the tables the old grants were attached to.")
            print("  A production restore MUST be followed by re-applying")
            print("  sql/app_role.sql before the app is scaled back up.")
            verdict = 1
        else:
            print("\n  grants survived — no post-restore privilege step needed")
        if after_cre == "t":
            print("  *** crm_app gained CREATE — privilege separation weakened ***")
            verdict = 1

        # ── PASS 3: apply the remedy and prove it restores service ──────────
        # Finding a defect is half the rehearsal. An incident needs the fix to
        # be known-good and TIMED, not inferred.
        print("\nPASS 3 — apply the remedy (what sql/app_role.sql does)")
        t0 = time.time()
        for sql in (
            "GRANT USAGE ON SCHEMA public TO crm_app",
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public "
            "TO crm_app",
            "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO crm_app",
            "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO crm_app",
            "REVOKE CREATE ON SCHEMA public FROM crm_app",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO crm_app",
        ):
            rc, out = _psql(sql)
            if rc != 0:
                print(f"  FAILED: {sql[:50]} -> {out[:70]}")
        regrant_s = time.time() - t0
        fixed_sel = _psql("SELECT has_table_privilege('crm_app',"
                          "'public.accounts','SELECT')")[1]
        fixed_ins = _psql("SELECT has_table_privilege('crm_app',"
                          "'public.accounts','INSERT')")[1]
        fixed_cre = _psql("SELECT has_schema_privilege('crm_app','public',"
                          "'CREATE')")[1]
        # Does the app role actually WORK, not merely hold a privilege bit?
        rc, rows = _psql("SET ROLE crm_app; SELECT count(*) FROM accounts")
        print(f"  regrant took {regrant_s:.1f}s")
        print(f"  SELECT : {fixed_sel}   INSERT : {fixed_ins}   "
              f"CREATE : {fixed_cre} (must stay false)")
        print(f"  actual query as crm_app: {'OK, ' + rows + ' accounts' if rc == 0 else 'FAILED ' + rows[:60]}")
        if fixed_sel != "t" or rc != 0:
            print("  *** THE DOCUMENTED REMEDY DOES NOT RESTORE SERVICE ***")
            verdict = 1
        elif fixed_cre == "t":
            print("  *** remedy over-granted: crm_app can CREATE ***")
            verdict = 1
        else:
            print("  remedy verified: service restored, separation intact")
            verdict = 0

        print(f"\nTIMING (the number an incident needs)")
        print(f"  into empty     : {s1:.1f}s   (what the nightly drill measures)")
        print(f"  over populated : {s2:.1f}s   <-- the realistic restore cost")
        print(f"  + regrant      : {regrant_s:.1f}s")
        print(f"  = DB RECOVERY  : {s2 + regrant_s:.1f}s, plus provisioning, "
              f"app restart and verification")
        print(f"  tables         : {n1} -> {n2}"
              + ("  MISMATCH" if n1 != n2 else ""))
        if n1 != n2:
            verdict = 1
        if rc2 != 0:
            print(f"\n  pg_restore rc={rc2} (warnings are normal; the table count "
                  f"is the signal). tail:\n  {err2[:300]}")
        return verdict
    finally:
        if not a.keep:
            _sh("docker", "rm", "-f", CNAME)
            print("\n  rehearsal container removed")
        else:
            print(f"\n  container {CNAME} left running on port {PORT}")


if __name__ == "__main__":
    raise SystemExit(main())
