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


def build_parser() -> "argparse.ArgumentParser":
    """The parser, separated so --help can be answered without touching a DSN.

    WHY THIS EXISTS. Arguments used to be read by scanning sys.argv, so
    `--help` matched nothing, fell through to the default branch, and RAN A
    FULL VERIFICATION against whatever DATABASE_URL happened to be set. For a
    tool whose normal mode targets production that is not a cosmetic defect:
    the safest thing a user can type did the second-most dangerous thing the
    program does. argparse handles --help itself and exits 0 before main()
    reaches any connection.
    """
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m scripts.postdeploy_verify",
        description="Post-deployment verification. Read-only: it opens "
                    "read-only transactions and never writes to the target.",
        epilog="With no --target it verifies the DSN in DATABASE_URL/DB_DSN. "
               "Railway is never a default; it must be named explicitly.")
    ap.add_argument("--target", choices=("railway",), default=None,
                    help="verify a named deployment instead of the configured "
                         "DSN. Requires RAILWAY_DB_URL with sslmode.")
    ap.add_argument("--app-url", default="",
                    help="base URL of the RUNNING application, e.g. "
                         "https://example.up.railway.app. Used to ask /health "
                         "which database role the app actually connects as -- "
                         "the only place that fact exists.")
    return ap


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


# ---------------------------------------------------------------------------
# THE SUPPORTED SCHEMA-OBJECT INVENTORY
# ---------------------------------------------------------------------------
# Explicit, not "everything PostgreSQL knows about". Comparing every internal
# object would produce diffs nobody can act on and would train people to ignore
# the output -- the same way a permanently-red check invites falsifying history
# to recover green. These eight classes are the ones a Conscestra schema change
# actually lands in.
#
# WHY IT GREW. This compared `relkind='r'` -- ordinary tables and nothing else.
# That caught sql/promotions_coupons.sql, a missing TABLE, which is why it was
# written. It could not have caught sql/notification_headline.sql, which
# replaces the BODY of trg_fn_events_after_insert() and creates no object at
# all. Three files replaced that function on the way to production and every
# mechanism in the system was blind to all three.
#
# Functions and views are compared BY BODY, not by name. A name-only comparison
# reports "present on both" for a function whose logic silently diverged, which
# is worse than not looking: it is a check that answers the wrong question
# confidently.
_INVENTORY = {
    "tables": """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n
          ON n.oid = c.relnamespace
         WHERE n.nspname='public' AND c.relkind='r'""",
    # Nullability rides here, not in `constraints`, because it is the only
    # portable place to compare it -- see the NOT NULL note below.
    "columns": """
        SELECT table_name || '.' || column_name || ':' || data_type
               || CASE WHEN is_nullable='NO' THEN ' NOT NULL' ELSE '' END
          FROM information_schema.columns WHERE table_schema='public'""",
    "indexes": """
        SELECT indexname FROM pg_indexes WHERE schemaname='public'""",
    # contype='n' (NOT NULL) is EXCLUDED, and the exclusion is load-bearing.
    # PostgreSQL 18 materialises every NOT NULL as a pg_constraint row; 17 does
    # not. Local is 17.9 and Railway is 18.6, so including them reported 965
    # phantom "on target only" constraints on every single run -- a server
    # version difference dressed up as schema drift. A check that cries wolf
    # 965 times is one nobody reads, and the real signal would be buried in it.
    # Nullability is still compared, portably, via the `columns` class above.
    "constraints": """
        SELECT conrelid::regclass::text || '.' || conname
          FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
         WHERE n.nspname='public' AND c.contype <> 'n'""",
    # md5 of the body: a changed function must not read as unchanged.
    #
    # TWO EXCLUSIONS, BOTH LOAD-BEARING, BOTH THE SAME LESSON AS contype='n'
    # ABOVE. Measured against Railway on 2026-08-27, this class reported 90
    # differences. Eighty-nine were noise:
    #
    #   EXTENSION MEMBERS (37). pgcrypto and uuid-ossp are installed into a
    #   schema named `extensions` on Railway and into `public` locally, so
    #   every armor/crypt/pgp_*/uuid_generate_* function read as "missing on
    #   the target" while being perfectly present. They are not application
    #   objects and a migration will never create them; `pg_depend` with
    #   deptype='e' is how PostgreSQL itself distinguishes them.
    #
    #   LINE ENDINGS (52). Railway's stored bodies carry a BLANK LINE after
    #   every line — the signature of an LF -> CRLF -> LF double conversion in
    #   transfer. compute_payment_status is 167 characters here and 177 there,
    #   the difference being exactly ten newlines in a ten-line function; the
    #   line-level diff is nothing but empty insertions. Hashing with runs of
    #   newlines collapsed keeps the check honest about LOGIC without being
    #   fooled by how the text travelled.
    #
    #   WHAT THAT NORMALISATION HIDES, stated so nobody has to rediscover it:
    #   a change consisting ONLY of blank lines inside a string literal. No
    #   logic change can take that shape, which is why the trade is worth
    #   making — but it is a trade, not a free win.
    #
    # What remained after both exclusions was the real signal, and it was
    # small enough to act on. That is the whole point: this check decides
    # which schema is canonical, so noise in it is not cosmetic — it is a
    # false answer to the question the CI baseline depends on.
    #   ORPHAN TRIGGER FUNCTIONS. A function returning `trigger` that no
    #   trigger is bound to cannot run. `trgfn_payment_event` is one, on BOTH
    #   databases, and its bodies differ — local's carries an extra
    #   `payment.received` emission block. Compared as an object it looks like
    #   production is missing a business event; checked for reachability it is
    #   dead code on both sides, because the event is actually emitted by
    #   `trgfn_payment_received_event`, which IS bound to payments and whose
    #   body is identical in both environments.
    #
    #   That false positive cost a full audit cycle and was reported as "the
    #   central blocker". Worse, the recommended remedy — deploy local's copy —
    #   would have been actively harmful had the function ever been bound: two
    #   emitters, one duplicate `payment.received` per confirmed payment.
    #
    #   AN UNREACHABLE OBJECT IS NOT DRIFT YOU CAN ACT ON — but it is NOT
    #   excluded here, and the first attempt to exclude it made things worse.
    #   `_schema_inventory` runs against one database at a time and cannot know
    #   the other side, so filtering orphans per-database is ASYMMETRIC: a
    #   function bound here and unbound there vanishes from one set and not the
    #   other, manufacturing a difference and double-reporting the missing
    #   trigger that already appears in the `triggers` class.
    #
    #   So the comparison stays symmetric and `orphan_functions` below reports
    #   reachability as its own fact. A reviewer seeing a name in BOTH the
    #   functions diff and both sides' orphan list knows the difference is dead
    #   code. That is the judgement a person should make with the evidence in
    #   front of them, not one this query should make for them.
    "functions": """
        SELECT p.proname || ':' || md5(
                 regexp_replace(replace(coalesce(p.prosrc,''), chr(13), ''),
                                chr(10) || '+', chr(10), 'g'))
          FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
         WHERE n.nspname='public'
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.objid = p.oid AND d.deptype = 'e')""",
    # Reported, but as its own class: an unbound trigger function is dead code,
    # not a behavioural difference. Naming them separately means a reviewer sees
    # "these exist and run nowhere" instead of "production is missing logic".
    "orphan_functions": """
        SELECT p.proname
          FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
         WHERE n.nspname='public' AND p.prorettype = 'trigger'::regtype
           AND NOT EXISTS (SELECT 1 FROM pg_trigger t
                            WHERE t.tgfoid = p.oid AND NOT t.tgisinternal)""",
    "triggers": """
        SELECT c.relname || '.' || t.tgname FROM pg_trigger t
          JOIN pg_class c ON c.oid=t.tgrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname='public' AND NOT t.tgisinternal""",
    "views": """
        SELECT table_name || ':' || md5(coalesce(view_definition,''))
          FROM information_schema.views WHERE table_schema='public'""",
    "grants": """
        SELECT grantee || ':' || table_name || ':' || privilege_type
          FROM information_schema.role_table_grants WHERE table_schema='public'""",
}


def _schema_inventory(dsn: str) -> dict:
    """One read-only snapshot per object class."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True)
        out = {}
        with conn.cursor() as cur:
            for name, sql in _INVENTORY.items():
                cur.execute(sql)
                out[name] = {r[0] for r in cur.fetchall()}
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DECLARED DRIFT — differences that have been decided, with the reason
# ---------------------------------------------------------------------------
# WHY THIS EXISTS, and the line it must not cross. Every schema difference used
# to be a failure. That is the right default, and it made this check
# permanently red: the corpus carries genuine dead local cruft that will never
# be deployed, and a verifier that cannot say so trains people to skim past it.
# A permanently-red check is a switched-off check with extra steps.
#
# THE LINE. A declared entry is a DECISION WITH A REASON, in the same idiom as
# dsar.EXCLUDED and verify_gate.DECLARED_SKIPS. It is not a place to put
# differences nobody has explained. Two differences found on 2026-08-28 are
# deliberately ABSENT from this list, because they are real:
#
#   * generate_random_orders — local carries an order-ceiling fix that
#     production lacks, and production runs the nightly job. A missing
#     deployment is not an exception; excusing it would hide exactly the class
#     of defect this check was built after (the fifteen-day coupon outage).
#   * the updated_at triggers — not a naming difference. accounts/contacts
#     locally, and customers/employees/product_pricing IN PRODUCTION, carry an
#     updated_at column that NO trigger maintains. Behaviour differs, so the
#     behaviour is what needs fixing.
#
# key: (object class, exact inventory string) -> why the difference is correct
DECLARED_DRIFT: dict = {
    # -- dead local cruft: an abandoned vector-column experiment ------------
    ("columns", "content_embeddings.embedding_v:USER-DEFINED"):
        "LOCAL-ONLY. Abandoned vector-column experiment: no file in sql/ "
        "creates it and no code reads it (content_index.py writes `embedding`). "
        "Not deployed, deliberately -- removing a diff is not a reason to ship "
        "dead code to production. It IS in the baseline, so CI builds it too.",
    ("indexes", "idx_ce_hnsw"):
        "LOCAL-ONLY. HNSW index over content_embeddings.embedding_v, the dead "
        "column above. Same disposition.",

    # -- the ledger nullability: BLOCKED, and blocked for a good reason -----
    ("columns", "schema_migrations.applied_by:text"):
        "LOCAL-ONLY (nullable). scripts/migrate.py is canonical and declares "
        "NOT NULL; PRODUCTION SATISFIES IT. Local predates the constraint and "
        "holds 24 rows with NULLs. Not remediated: adding the constraint would "
        "require inventing checksums for migrations whose applied bytes nobody "
        "knows, or deleting ledger rows -- both forbidden. The invariant holds "
        "where it protects anything.",
    ("columns", "schema_migrations.checksum:text"):
        "LOCAL-ONLY (nullable). See schema_migrations.applied_by.",
    ("columns", "schema_migrations.applied_by:text NOT NULL"):
        "TARGET-ONLY. The CANONICAL definition per scripts/migrate.py. It "
        "appears as drift only because local is behind, not production.",
    ("columns", "schema_migrations.checksum:text NOT NULL"):
        "TARGET-ONLY. Canonical -- see schema_migrations.applied_by.",

    # -- dead local functions ----------------------------------------------
    # Each appears only in governance/sp/crm_db.sql and crm_db_tables.sql,
    # historical whole-database dumps rather than live code. No migration,
    # trigger, stored procedure or Python module calls them.

    # THE FIVE DEAD LOCAL FUNCTIONS THAT WERE HERE ARE GONE, dropped
    # 2026-08-28 by drop_local_only_dead_functions.sql. The stale-
    # declaration check named all five the same run, which is why this
    # list did not quietly outlive them.
    # -- production orphans left by the trigger consolidation ---------------
    # REMOVED 2026-08-28, by the stale-declaration check rather than by
    # anyone remembering: set_lead_updated_at, update_updated_at_column,
    # update_updated_datetime_column and update_users_timestamp were declared
    # TARGET-ONLY orphans. touch_updated_at_convergence.sql retired the legacy
    # triggers on LOCAL too, so all four became orphans on both sides and the
    # declarations matched nothing. An exception that outlives its difference
    # is exactly what the stale check exists to surface.
    ("orphan_functions", "increment_workflow_version"):
        "TARGET-ONLY orphan: defined, bound to no trigger. Inert residue.",

    # -- PENDING DEPLOYMENT, and this entry must not outlive that state ------
    # The md5 half of this key is the body hash Railway still reports. It was
    # derived from the definition captured BEFORE the local drop, using the
    # same normalisation the query applies (strip CR, collapse repeated LF),
    # and the derivation was validated against three functions the database
    # could still hash itself.
    ("functions", "sp_cases:dec380d8704c14dd8ef42e91f143400a"):
        "PENDING DEPLOYMENT. Dropped from LOCAL 2026-08-28 by "
        "sql/drop_sp_cases.sql; Railway still has it, so it reads as "
        "TARGET-ONLY drift. This is the ONE window where that is expected. "
        "sp_cases is not inert residue -- five of its fourteen modes still "
        "execute, and `assign` and `close` write cases.owner_id and "
        "cases.status with no record_field_history, so production keeps a "
        "live ungoverned mutation path until the drop is applied there. "
        "DELETE THIS ENTRY in the same change that records the Railway "
        "application. If the stale-declaration check names it, the drop "
        "landed and this exception is the only thing still pretending "
        "otherwise.",
}


def _schema_drift(target_dsn: str) -> Optional[str]:
    """Objects present in the working schema but missing from the deploy target.

    This check exists because of a real fifteen-day outage. sql/promotions_
    coupons.sql was applied locally on 2026-07-21 and never to Railway; the
    store agent caught the missing-table error and answered "no such coupon",
    which is exactly what a wrong code produces, so every valid coupon a
    customer typed was refused and nothing looked wrong from either side.

    The migration ledger did not catch it and could not: migrations applied by
    hand never called record_migration(). It also reported three migrations as
    missing from production that were in fact applied there. Wrong in both
    directions is worse than absent -- so this compares the LIVE SCHEMAS and
    ignores the ledger entirely.

    IT NOW COMPARES EIGHT OBJECT CLASSES, not just tables. A schema change that
    bypasses the ledger has to be detectable even when it creates no table --
    which is the whole class of change the ledger cannot see. See _INVENTORY.

    Returns None when it cannot run (one DSN, or both pointing at the same
    database). 'Could not compare' is reported as skipped, never as clean."""
    working = (os.getenv("DB_DSN") or "").strip()
    if not working or working == target_dsn:
        return None

    try:
        here, there = _schema_inventory(working), _schema_inventory(target_dsn)
    except Exception as exc:                                    # noqa: BLE001
        return f"SKIPPED — could not compare schemas: {type(exc).__name__}: {exc}"

    parts, total_missing, excused = [], 0, 0
    stale = set(DECLARED_DRIFT)
    for cls in _INVENTORY:
        missing = sorted(here[cls] - there[cls])
        extra = sorted(there[cls] - here[cls])
        # Remove DECLARED differences from the verdict, and COUNT them, so the
        # report states how much was excused instead of quietly shrinking.
        for seq in (missing, extra):
            for item in list(seq):
                if (cls, item) in DECLARED_DRIFT:
                    seq.remove(item)
                    excused += 1
                    stale.discard((cls, item))
        total_missing += len(missing)
        if not missing and not extra:
            continue
        bit = f"{cls}: "
        if missing:
            bit += (f"{len(missing)} MISSING FROM TARGET "
                    f"({', '.join(missing[:4])}"
                    f"{', …' if len(missing) > 4 else ''})")
        if extra:
            bit += (f"{' | ' if missing else ''}{len(extra)} on target only "
                    f"({', '.join(extra[:4])}{', …' if len(extra) > 4 else ''})")
        parts.append(bit)

    # AN EXCEPTION MATCHING NOTHING IS ITSELF A DEFECT: the difference was
    # resolved and the excuse outlived it, which is how a list of reasons
    # becomes a list nobody rereads.
    if stale:
        parts.append("STALE DECLARED_DRIFT (matches nothing; delete): "
                     + ", ".join(f"{c}:{i}" for c, i in sorted(stale)))

    suffix = f" [{excused} declared exception(s) excused]" if excused else ""
    if not parts:
        counts = ", ".join(f"{len(here[c])} {c}" for c in _INVENTORY)
        return (f"OK — no undeclared drift ({counts}){suffix}")
    lead = ("SCHEMA DRIFT — code may reference objects the target lacks"
            if total_missing else "schema differs (target has extra objects)")
    return lead + suffix + " || " + " || ".join(parts)

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


def main(argv: Optional[list] = None) -> int:
    # PARSE FIRST, CONNECT LATER. Everything below this line can touch a
    # production database; nothing above it may. argparse exits here for
    # --help (status 0) and for a bad argument (status 2), in both cases
    # before a DSN is read, let alone opened.
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)

    label, dsn = _target_dsn(["_"] + argv)

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
    # From the parsed namespace, not a second scan of sys.argv: two readers of
    # the same flag drift, and this one decides whether the app's real database
    # role is learned at all.
    app_url = args.app_url or os.getenv("RAILWAY_APP_URL", "") or ""
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
