"""Verify a DEPLOYED environment. Run this after every Railway deploy.

WHY THIS EXISTS. CI runs 222 module imports and the memory benchmark. It runs
no tests, no invariants and no red team, because `tests/` and `sql/` live
outside the repository by policy — so a runner cannot build the schema and
cannot execute a single database control. Every safety property this system
claims is therefore verified only where the schema and the secrets actually
exist, which is the deployed environment itself.

A green CI tick has never been evidence about the database. This is.

    python -m scripts.postdeploy_verify                  # uses DB_DSN/DATABASE_URL
    python -m scripts.postdeploy_verify --target railway # uses RAILWAY_DB_URL

WHAT IT RUNS
  secret_health      are the guarded secrets real, strong and distinct
  verify_invariants  the DB-layer controls, asserted in SQL against live schema
  red_team           attacks executed, not enumerated
  dsar --coverage    can a data subject actually be given everything we hold
  runtime ddl        do the objects the app creates lazily already exist
  schema drift       does the target have every table the working schema has

EXIT CODE is what a deploy pipeline should gate on: 0 means every control was
exercised and held. Anything else means a control is missing, disabled, or was
never installed on this database.

WRITES: verify_invariants and red_team both MUTATE rows — they plant probes,
attack them, and revert. Both check their own residue. That is deliberate: a
control tested only by reading catalogs is a control tested against its
description rather than its behaviour, which is how three separate broken
controls survived here for months.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Importing config is what loads .env. Any entry point that reads os.getenv
# before this sees a bare environment and concludes nothing is configured —
# which for a verification tool means reporting a false problem, or worse,
# verifying a database it did not mean to.
from app.core import config as _config          # noqa: E402,F401


def _target_dsn(argv) -> tuple[str, str]:
    """Return (label, dsn). Railway is opt-in and explicit — never a default."""
    if "--target" in argv:
        i = argv.index("--target")
        name = argv[i + 1] if i + 1 < len(argv) else ""
        if name == "railway":
            dsn = (os.getenv("RAILWAY_DB_URL") or "").strip()
            if not dsn:
                raise SystemExit("RAILWAY_DB_URL is not set")
            if "sslmode" not in dsn.lower():
                # 'prefer' silently falls back to plaintext over a public proxy.
                raise SystemExit(
                    "RAILWAY_DB_URL has no sslmode. libpq defaults to 'prefer', "
                    "which downgrades to an unencrypted connection without "
                    "telling you. Append ?sslmode=require.")
            return "railway", dsn
        raise SystemExit(f"unknown target {name!r} (known: railway)")
    dsn = (os.getenv("DATABASE_URL") or os.getenv("DB_DSN") or "").strip()
    if not dsn:
        raise SystemExit("no DATABASE_URL or DB_DSN configured")
    return "configured DSN", dsn


def _run(label: str, module: str, env: dict,
         args: tuple = ()) -> tuple[str, int, str]:
    proc = subprocess.run([sys.executable, "-m", module, *args], cwd=str(ROOT),
                          env=env, capture_output=True, text=True)
    return label, proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _schema_drift(target_dsn: str) -> Optional[str]:
    """Tables present in the working schema but missing from the deploy target.

    This check exists because of a real fifteen-day outage. sql/promotions_
    coupons.sql was applied locally on 2026-07-21 and never to Railway; the
    store agent caught the missing-table error and answered "no such coupon",
    which is exactly what a wrong code produces, so every valid coupon a
    customer typed was refused and nothing looked wrong from either side.

    The migration ledger did not catch it and could not: schema_migrations held
    25 rows against 194 files in sql/, because migrations applied by hand in
    pgAdmin never call record_migration(). It also reported three migrations as
    missing from production that were in fact applied there. Wrong in both
    directions is worse than absent — so this compares the LIVE SCHEMAS and
    ignores the ledger entirely.

    Returns None when it cannot run (one DSN, or both pointing at the same
    database). 'Could not compare' is reported as skipped, never as clean."""
    import psycopg2

    working = (os.getenv("DB_DSN") or "").strip()
    if not working or working == target_dsn:
        return None

    def tables(dsn: str) -> set:
        conn = psycopg2.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT c.relname FROM pg_class c
                                 JOIN pg_namespace n ON n.oid = c.relnamespace
                                WHERE n.nspname='public' AND c.relkind='r'""")
                return {r[0] for r in cur.fetchall()}
        finally:
            conn.close()

    try:
        here, there = tables(working), tables(target_dsn)
    except Exception as exc:                                    # noqa: BLE001
        return f"SKIPPED — could not compare schemas: {type(exc).__name__}: {exc}"

    missing = sorted(here - there)
    extra = sorted(there - here)
    if not missing and not extra:
        return f"OK — {len(here)} public tables, identical on both"
    parts = []
    if missing:
        parts.append("MISSING FROM TARGET (code may reference these): "
                     + ", ".join(missing))
    if extra:
        parts.append("present on target only: " + ", ".join(extra))
    return " | ".join(parts)


def _app_identity(app_url: str) -> Optional[str]:
    """Ask the RUNNING APPLICATION which database role it connects as.

    This is the only place that fact exists. The verifier's own connection is
    an admin account by design, the catalog cannot say which credentials a
    remote process used, and `pg_stat_activity` only shows roles that happen to
    hold a session right now — an idle app shows nothing.

    So the app reports it about itself, on /health. Everything else here is
    inference; this is observation.
    """
    import json as _json
    import urllib.request
    # Accept a base URL OR a full /health URL.
    #
    # This appended "/health" unconditionally, so the documented invocation —
    # `--app-url https://<app>/health`, which is what the runbooks, the PR
    # template and every instruction in this repository say — produced
    # `/health/health`, a 404, and a silent SKIP. The app role was then never
    # learned, so the red team judged the ADMIN connection this script uses and
    # reported a breach that says nothing about the application.
    #
    # The check reported "skipped" the whole time, which is honest and easy to
    # read past. Nobody noticed because the run still ended in a failure that
    # LOOKED like the expected admin-DSN artefact.
    base = app_url.rstrip("/")
    url = base if base.endswith("/health") else base + "/health"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            body = _json.loads(r.read().decode("utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        print(f"  SKIP  app identity — {url} unreachable ({str(exc)[:60]})\n")
        return None
    db = body.get("database")
    if db is None:
        print(f"  SKIP  app identity — {url} does not report `database`; that "
              f"build predates the health-check fix, so it cannot say whether "
              f"it can reach the database at all\n")
        return None
    if not db.get("ok"):
        print(f"  FAIL  app identity — the app CANNOT reach its database: "
              f"{str(db.get('error'))[:120]}\n")
        return None
    who = db.get("connected_as")
    print(f"  PASS  app identity — the application connects as '{who}'\n")
    return who


def _report_connected_roles(dsn: str) -> None:
    """Which roles are ACTUALLY connected to this database?

    Everything else here reasons about the connection the VERIFIER opened,
    which is deliberately an owner account — the harness needs owner rights.
    That means the red team's 'the app connects as a superuser' finding is
    expected here and says nothing about the application, which was read as a
    live breach on a deployment that had already been switched over.

    A privilege-separation claim is about the running app, and the only honest
    way to see that from outside is to look at who holds sessions. It is an
    OBSERVATION, not a gate: an idle app holds no connections, so absence
    proves nothing and this never fails the run.
    """
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
    except Exception:
        return
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""SELECT usename, count(*)
                             FROM pg_stat_activity
                            WHERE datname = current_database()
                              AND pid <> pg_backend_pid()
                            GROUP BY 1 ORDER BY 2 DESC""")
            rows = cur.fetchall()
        print("observed connections (who is actually using this database):")
        if not rows:
            print("    none besides this check — an idle app holds no "
                  "connections, so this is not evidence either way")
        for user, n in rows:
            print(f"    {user:12} {n} session(s)")
        print("    the app's own view is authoritative: "
              "GET /health -> database.connected_as\n")
    finally:
        conn.close()


def main() -> int:
    label, dsn = _target_dsn(sys.argv)

    env = dict(os.environ)
    # Both variables are set, because different modules read different ones and
    # a half-applied override would silently verify the WRONG database — the
    # single worst outcome for a tool whose entire job is to tell the truth
    # about which database is safe.
    env["DB_DSN"] = dsn
    env["DATABASE_URL"] = dsn

    user = dsn.split("//", 1)[-1].split(":", 1)[0] if "//" in dsn else "?"
    host = dsn.split("@")[-1].split("/")[0] if "@" in dsn else "?"
    print(f"post-deploy verification — target: {label}")
    print(f"  connecting as '{user}' to {host}\n")

    # Learn the application's real role BEFORE the red team runs, so the
    # trigger-disable attack can be judged against the app rather than against
    # this admin connection.
    app_url = ""
    if "--app-url" in sys.argv:
        i = sys.argv.index("--app-url")
        app_url = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    app_url = app_url or os.getenv("RAILWAY_APP_URL", "") or ""
    app_role = _app_identity(app_url) if app_url else None
    if app_role:
        env["REDTEAM_APP_ROLE"] = app_role

    # AN EXPLICITLY REQUESTED CHECK THAT CANNOT RUN IS A FAILURE, NOT A SKIP.
    #
    # This used to continue when identity could not be learned. The red team
    # then judged the ADMIN connection this script opens rather than the
    # application's role, and reported a breach that says nothing about the app
    # — while the run ended in a failure that LOOKED like the expected admin-DSN
    # artefact. A `/health/health` typo hid behind that for the entire life of
    # the flag.
    #
    # Not passing --app-url is a choice and stays a skip. Passing one that does
    # not answer is a broken invocation, and the checks downstream of it are
    # then measuring something other than what the operator asked for.
    early_fail: List[str] = []
    if app_url and not app_role:
        print("  FAIL  app identity — --app-url was given and did not yield the "
              "app's database role.\n        Everything downstream would judge "
              "THIS admin connection instead, so the\n        red-team result "
              "below would be about the wrong subject.\n")
        early_fail.append("app identity")

    stages = [("secrets", "app.core.secret_health", ()),
              ("invariants", "scripts.verify_invariants", ()),
              ("red team", "scripts.red_team", ()),
              # A subject-linked table nobody declared makes every Art. 15
              # export silently narrower than it claims to be. --coverage exits
              # 1 in exactly that case, so a migration that adds one is caught
              # at deploy rather than at the next access request.
              ("dsar coverage", "app.core.dsar", ("--coverage",)),
              # Objects the app creates lazily cannot be created by the app's
              # own role any more. They exist today only because they predate
              # the privilege separation; the next one added will be inert in
              # production and silent about it.
              ("runtime ddl", "scripts.verify_runtime_ddl", ())]

    failures = list(early_fail)
    for name, module, args in stages:
        stage, code, output = _run(name, module, env, args)
        ok = code == 0
        print(f"  {'PASS' if ok else 'FAIL'}  {stage}")
        if not ok:
            failures.append(stage)
            for line in output.strip().splitlines()[-14:]:
                print(f"        {line}")
        print()

    drift = _schema_drift(dsn)
    if drift is not None:
        verdict = drift.startswith("OK")
        skipped = drift.startswith("SKIPPED")
        print(f"  {'PASS' if verdict else 'SKIP' if skipped else 'FAIL'}  "
              f"schema drift")
        print(f"        {drift}\n")
        if not verdict and not skipped:
            failures.append("schema drift")

    _report_connected_roles(dsn)

    if failures:
        print(f"FAILED: {', '.join(failures)}")
        print("A deployed environment failing any of these is not safe to serve "
              "customer-facing claims. Do not roll forward.")
        return 1
    print("all post-deploy checks passed")
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
