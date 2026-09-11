"""Prove the read-only role is read-only, by trying to write with it.

    python -m scripts.verify_readonly_role                  # local
    python -m scripts.verify_readonly_role --target railway # production

D-08. `governance/sql/readonly_role.sql` creates `crm_readonly`; this checks
that it does what the file claims, and it checks by ATTEMPTING THE WRITES rather
than by reading the grant tables.

WHY ATTEMPT RATHER THAN INSPECT. `scripts/verify_invariants` already reads
pg_roles to confirm crm_app holds no elevated attributes, and that check is
correct. It is also the weaker half: a role can hold no scary attribute and
still write, through a default privilege granted by a later migration, through
membership in a group, through a SECURITY DEFINER function, or because somebody
granted the wrong thing while fixing something else. The catalogue says what was
INTENDED. An INSERT that gets refused says what is TRUE.

Every probe here runs inside a transaction that is rolled back, and each is
written so that the FAILURE mode is the safe one: if a write unexpectedly
succeeds, it is rolled back and reported as a failed check, never left behind.

WHAT A PASS HERE DOES NOT MEAN. It does not mean D-08 is closed. This proves the
replacement credential is safe; D-08 closes when the `postgres` password is
ROTATED, because until then the old DSN still works from everywhere it was ever
copied. The final check below tests exactly that and is the only one that can
report the difference.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config as _config          # noqa: E402,F401  (loads .env)

ROLE = "crm_readonly"

_PASS, _FAIL, _WARN = "PASS", "FAIL", "WARN"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((_PASS if ok else _FAIL, name, detail))


def warn(name: str, ok: bool, detail: str = "") -> None:
    _results.append((_PASS if ok else _WARN, name, detail))


def _refuses(cur, label: str, sql: str) -> None:
    """This statement MUST be refused. Rolled back either way.

    A distinct message for 'it was accepted' versus 'it was refused', because
    those are the two outcomes that matter and collapsing them into one boolean
    is how a control ends up reported as working when it is not."""
    cur.execute("SAVEPOINT probe")
    try:
        cur.execute(sql)
    except psycopg2.Error as exc:
        cur.execute("ROLLBACK TO SAVEPOINT probe")
        check(label, True, str(exc).splitlines()[0][:110])
        return
    cur.execute("ROLLBACK TO SAVEPOINT probe")
    check(label, False, "ACCEPTED — the statement was not refused (rolled back)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Attempt writes as crm_readonly and require every one to be "
                    "refused. Read-only by outcome: every probe is rolled back.")
    ap.add_argument("--target", choices=("local", "railway"), default="local")
    ap.add_argument("--app-url", default="",
                    help="optional; checked only to report that the app is up")
    args = ap.parse_args()

    if args.target == "railway":
        dsn = (os.getenv("RAILWAY_DB_URL") or "").strip()
        if not dsn:
            raise SystemExit("RAILWAY_DB_URL is not set")
    else:
        dsn = (os.getenv("DATABASE_URL") or os.getenv("DB_DSN") or "").strip()
        if not dsn:
            raise SystemExit("DATABASE_URL / DB_DSN is not set")

    print(f"TARGET: {args.target.upper()}")

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), version()")
            who, db, ver = cur.fetchone()
            print(f"CONNECTED AS: {who}  DB: {db}")
            print(f"SERVER: {ver.split(',')[0]}\n")

            # ── THE FIRST CHECK IS THE POINT OF THE WHOLE EXERCISE ──────────
            # If this DSN is still the owner, every probe below would pass for
            # the wrong reason -- an owner is refused nothing, so the probes
            # would only be measuring that the connection works.
            check(f"the configured DSN connects as {ROLE}", who == ROLE,
                  "" if who == ROLE
                  else f"connected as {who!r}. This run proves NOTHING about "
                       f"{ROLE}; point RAILWAY_DB_URL at it first.")
            if who != ROLE:
                return _report()

            cur.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, "
                        "rolbypassrls, rolreplication FROM pg_roles "
                        "WHERE rolname = %s", (ROLE,))
            row = cur.fetchone()
            check(f"{ROLE} exists", row is not None)
            if row:
                elevated = any(row)
                check(f"{ROLE} holds no elevated attributes", not elevated,
                      "" if not elevated else f"attributes: {row}")

            cur.execute("SHOW default_transaction_read_only")
            ro = cur.fetchone()[0]
            check("sessions start read-only", ro == "on",
                  "" if ro == "on"
                  else f"default_transaction_read_only={ro!r} — a write would "
                       f"be refused only by a missing grant, which is the "
                       f"weaker of the two controls")

            cur.execute(f"SELECT has_schema_privilege('{ROLE}','public','CREATE')")
            check("cannot CREATE in schema public", not cur.fetchone()[0])

            # ── Reads must still work, or the channel is useless ────────────
            cur.execute("SELECT count(*) FROM governance_alerts")
            n_alerts = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM action_approvals")
            n_appr = cur.fetchone()[0]
            check("reads work (the evidence channel survives)",
                  n_alerts >= 0 and n_appr >= 0,
                  f"governance_alerts={n_alerts}, action_approvals={n_appr}")

            # Coverage, not just reachability: a role that can read three
            # tables and not the other 163 is a broken audit channel that looks
            # like a working one.
            cur.execute("""SELECT count(*) FROM pg_class c
                             JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname='public' AND c.relkind='r'""")
            total = cur.fetchone()[0]
            cur.execute("""SELECT count(*) FROM pg_class c
                             JOIN pg_namespace n ON n.oid = c.relnamespace
                            WHERE n.nspname='public' AND c.relkind='r'
                              AND has_table_privilege(%s, c.oid, 'SELECT')""",
                        (ROLE,))
            readable = cur.fetchone()[0]
            check("can read every public table", readable == total,
                  f"{readable}/{total} readable"
                  + ("" if readable == total
                     else " — a partial view is an audit that cannot tell it is partial"))

            # ── The writes. Every one must be refused. ──────────────────────
            _refuses(cur, "INSERT is refused",
                     "INSERT INTO governance_alerts (alert_class, headline) "
                     "VALUES ('manual','readonly-probe')")
            _refuses(cur, "UPDATE is refused",
                     "UPDATE governance_alerts SET severity='low' "
                     "WHERE alert_id IS NOT NULL")
            _refuses(cur, "DELETE is refused",
                     "DELETE FROM governance_alerts WHERE alert_id IS NOT NULL")
            _refuses(cur, "DDL (CREATE TABLE) is refused",
                     "CREATE TABLE readonly_probe (x int)")
            # THE RED-TEAM MOVE app_role.sql exists because of: switching a
            # control off rather than defeating it.
            _refuses(cur, "ALTER TABLE ... DISABLE TRIGGER is refused",
                     "ALTER TABLE governance_alerts "
                     "DISABLE TRIGGER trg_governance_alerts_lifecycle")
            _refuses(cur, "GRANT (privilege escalation) is refused",
                     f"GRANT ALL ON governance_alerts TO {ROLE}")
            # THE ESCAPE HATCH, AND THE REASON DEFINER RIGHTS DO NOT DEFEAT
            # THIS ROLE. An earlier version of this script probed "calling a
            # SECURITY DEFINER function is refused" and failed -- correctly,
            # because that was never true and never needed to be: EXECUTE is
            # granted to PUBLIC by default and a REVOKE from this role does not
            # undo it. Calling a READ-ONLY function is fine and is checked
            # below as something that must keep WORKING.
            #
            # What matters is that a definer function cannot WRITE on this
            # role's behalf. Definer rights raise the privileges the body runs
            # WITH; they do not change the read-write mode of the transaction
            # the body runs IN. Verified on 2026-09-11 with a throwaway
            # superuser-owned definer function granted to PUBLIC whose body was
            # a bare INSERT: "cannot execute INSERT in a read-only transaction".
            #
            # That leaves exactly one escape, and this is it. If this probe
            # ever reports ACCEPTED, every other refusal in this script becomes
            # unreliable, because the transaction could be switched to
            # read-write before the write is attempted.
            _refuses(cur, "read-only mode cannot be switched off",
                     "SET transaction_read_only = off")

            # The channel must survive: a role that cannot evaluate a predicate
            # cannot audit the predicate, and fn_owner_eligible is the one this
            # audit uses most.
            cur.execute("SAVEPOINT ro_fn")
            try:
                cur.execute("SELECT fn_owner_eligible(NULL)")
                cur.execute("RELEASE SAVEPOINT ro_fn")
                check("read-only functions remain callable", True,
                      "fn_owner_eligible evaluated")
            except psycopg2.Error as exc:
                cur.execute("ROLLBACK TO SAVEPOINT ro_fn")
                check("read-only functions remain callable", False,
                      str(exc).splitlines()[0][:110])

            conn.rollback()

            # ── Rotation: the check that distinguishes 'safer' from 'closed' ─
            #
            # EVERY DSN-BEARING VARIABLE, not the resolved one. The first
            # version of this check read only the DSN this run had connected
            # with, and passed while DB_DSN held an owner credential three
            # inches away -- it was measuring "did I connect as an owner", a
            # proxy, when the property is "does an owner credential PERSIST on
            # this machine". Those diverge exactly when it matters: the moment
            # somebody points one variable at the new role and leaves the old
            # one in place.
            # REMOTE owner credentials only. D-08 is about a PRODUCTION
            # superuser DSN sitting on a laptop; the local owner DSN is a
            # different, lower-severity item (verify_invariants already carries
            # it as "application DSN points at crm_app") and migrations and the
            # test harness legitimately need it. Reporting both under one label
            # would make the production finding permanently noisy, which is how
            # a real signal gets ignored.
            _DSN_VARS = ("RAILWAY_DB_URL", "DATABASE_URL", "DB_DSN", "db_dsn",
                         "RAILWAY_ADMIN_DB_URL", "POSTGRES_URL")
            _LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "host.docker.internal")

            def _remote_owner(val: str) -> str:
                if not any(t in val for t in ("//postgres:", "//postgres@")):
                    return ""
                host = val.split("@")[-1].split("/")[0] if "@" in val else ""
                bare = host.split(":")[0].lower()
                return "" if bare in _LOCAL_HOSTS else host

            offenders = []
            for v in _DSN_VARS:
                host = _remote_owner(os.getenv(v) or "")
                if host:
                    offenders.append(f"{v} -> {host}")
            warn("no REMOTE owner DSN persists in the environment", not offenders,
                 "" if not offenders
                 else "; ".join(offenders) + " — still name(s) `postgres` "
                      "against a remote host. Point them at crm_readonly and "
                      "supply the owner credential per-command; see the runbook "
                      "in governance/sql/readonly_role.sql")
    finally:
        conn.close()

    return _report()


def _report() -> int:
    print("Read-only role invariants")
    width = max(len(n) for _, n, _ in _results) + 2
    for status, name, detail in _results:
        line = f"  {status:4}  {name:<{width}}"
        if detail:
            line += f" - {detail}"
        print(line)
    bad = [r for r in _results if r[0] == _FAIL]
    warns = [r for r in _results if r[0] == _WARN]
    print()
    if bad:
        print(f"{len(bad)} CHECK(S) FAILED — this role is not read-only.")
        return 1
    if warns:
        print("all write probes refused; "
              f"{len(warns)} OUTSTANDING ITEM(S) above. D-08 is NOT closed "
              "until the postgres password is rotated.")
        return 0
    print("all write probes refused, all reads succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
