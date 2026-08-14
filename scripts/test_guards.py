"""Safety guards for the test session, in tracked code.

WHY THIS LIVES IN scripts/ AND NOT tests/
------------------------------------------
`tests/` is deliberately untracked. That is a reasonable policy for test DATA
and test CASES, and it had an unreasonable consequence: two guards that exist
because of real incidents lived only on one laptop, so the next clone would
silently reintroduce both.

They are not really tests. They are TOOLING that happens to run under pytest —
the same category as scripts/verify_invariants.py — so they belong here, where
version control keeps them. `tests/conftest.py` stays a thin shim that imports
these; if the shim is lost, the guards are still in the repository and can be
re-wired in one line.

WHAT THEY GUARD
---------------
two_dsn_warning()
    The application connects as `crm_app`, which owns nothing — that is what
    stops an attacker holding the app's credentials from disabling every
    database-layer control. The TEST HARNESS needs owner rights, because
    fixtures purge themselves by disabling an append-only trigger. Point the
    suite at the app role and 172 tests fail in TEARDOWN with "must be owner of
    table record_field_history": passing tests, collapsing cleanup, and an hour
    to find the cause. The tempting fix — grant crm_app ownership — silently
    undoes the privilege separation.

row_baseline() / report_orphans()
    Two routing rules named 'aa first' and 'aa specific' were left by a
    teardown that never completed. They sat at position=1 with no language
    requirement, so every routing test matched the LEFTOVER instead of the rule
    it had just created: 19 tests failed for two days across three files, with
    assertion messages that pointed at the product. 176 probe cases accumulated
    the same way.

    Naming heuristics would have missed both — 'aa first' looks like nothing in
    particular. So this compares each table to ITSELF: anything the session
    added and did not remove is an orphan, whatever it is called.

preexisting_orphans()
    The delta guard above has one blind spot, and the blind spot is the case
    that actually hurts. It compares end-of-session to START-of-session, so it
    catches what THIS run leaked and is silent about what a PREVIOUS run left.
    Inherited rows are already in the baseline.

    That is not a corner case — it is the original incident. The five 'aa first'
    / 'aa specific' rules written on 2026-08-12 survived every later run: each
    one started with them present, ended with them present, leaked nothing, and
    reported a clean bill of health while 19 tests failed against them. Found
    2026-08-14, two days later.

    So this guard asks the complementary question — not "did we grow?" but "is
    there test-authored data here before we start?" — and answers it from
    authorship markers the fixtures themselves write. It is deliberately narrow:
    a marker is only listed where the fixtures write one that survives, and a
    guard that pretended to cover tables it cannot see would be worse than one
    that states its scope.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# Tables where a leaked row changes another test's answer. Not every table —
# only those whose contents are read as ambient state by some test.
#
# `activities` was added 2026-08-14, after it leaked 60 rows over three days
# unseen. A test created an opportunity; the workflow rule "Discovery call for
# new opportunity" reacted by writing an ACTIVITY; the teardown deleted the
# opportunity it had created and knew nothing about the activity it had caused.
# The guard stayed quiet because activities was not on this list — the leak only
# became visible when the content indexer picked those rows up and
# `content_embeddings` grew by one, so the symptom appeared in a different table
# from the cause.
#
# The lesson generalises: a fixture cleans what it CREATED, while the product
# reacts by writing somewhere else entirely. Watch the tables that receive those
# reactions, not only the ones tests write to directly.
ORPHAN_WATCH: Tuple[str, ...] = (
    "routing_rules", "cases", "case_comments", "customer_memories",
    "content_embeddings", "agent_utterances", "memory_verifications",
    "conversations", "activities",
)


# Authorship markers the fixtures actually write, and that SURVIVE a leak.
# (table, column, marker values, the fixture that writes it)
#
# Checked against the live schema on 2026-08-14. `cases` is absent on purpose:
# its fixtures pass actor="test"/source="test" to cases.open(), but `cases` has
# no text author column — created_by is a uuid and origin holds a channel — so
# there is no marker to read back. Listing it would imply coverage that does
# not exist. The delta guard still covers `cases` within a session.
PREEXISTING_MARKERS: Tuple[Tuple[str, str, Tuple[str, ...], str], ...] = (
    ("routing_rules", "created_by", ("test",), "the `rule` fixture"),
    ("customer_memories", "generator", ("pytest/seed",), "seeded_corpus"),
)


def _connect():
    try:
        import psycopg2
        from app.core.config import get_settings
        return psycopg2.connect(get_settings().db_dsn)
    except Exception:
        return None


def row_counts() -> Dict[str, int]:
    """Row count per watched table. Empty dict when no database is reachable,
    so a developer without Postgres is unaffected."""
    conn = _connect()
    if conn is None:
        return {}
    out: Dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            for table in ORPHAN_WATCH:
                try:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    out[table] = cur.fetchone()[0]
                except Exception:
                    conn.rollback()          # table absent in this schema
    finally:
        conn.close()
    return out


def two_dsn_warning() -> Optional[str]:
    """Message when the suite is pointed at a role that owns nothing, else None.

    Returns text rather than printing so the caller decides where it goes and
    whether it is fatal. Reporting beats failing here: a developer who ran the
    wrong DSN wants the reason in the first line of output, not a refusal.
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_user, "
                "pg_catalog.pg_get_userbyid(c.relowner) = current_user "
                "FROM pg_class c WHERE c.relname = 'customer_memories'")
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or row[1]:
        return None
    return (
        "\n*** WRONG DSN FOR THE TEST SUITE ***\n"
        f"Connected as '{row[0]}', which does not own the schema.\n"
        "Fixture teardown disables append-only triggers to purge test data; a "
        "non-owner cannot, so tests will PASS and then fail in TEARDOWN with "
        "'must be owner of table ...'.\n"
        "Run the SUITE as the owner. The APP runs as crm_app — do NOT grant "
        "crm_app ownership to make this go away; that undoes the privilege "
        "separation the role exists for.\n")


def preexisting_orphans() -> Optional[str]:
    """Message naming test-authored rows that were here BEFORE this run, else None.

    Reported rather than fatal, following two_dsn_warning: a developer who
    inherited a dirty database wants the reason in the first line of output, not
    a refusal to run. The rows will cause failures on their own — this makes the
    failures legible instead of leaving them pointing at the product.
    """
    conn = _connect()
    if conn is None:
        return None
    found = []
    try:
        with conn.cursor() as cur:
            for table, column, values, writer in PREEXISTING_MARKERS:
                try:
                    cur.execute(
                        f"SELECT count(*) FROM {table} WHERE {column} = ANY(%s)",
                        (list(values),))
                    n = cur.fetchone()[0]
                    if n:
                        found.append((table, column, values, writer, n))
                except Exception:
                    conn.rollback()          # table/column absent in this schema
    finally:
        conn.close()
    if not found:
        return None
    lines = ["\n" + "=" * 70,
             "TEST DATA WAS ALREADY HERE — left by an EARLIER run, not this one"]
    for table, column, values, writer, n in found:
        lines.append(f"    {table:20} {n:>4} row(s) with "
                     f"{column} in {list(values)}   (written by {writer})")
    lines += [
        "",
        "  These are ambient state for every test that reads the table. Leftover",
        "  routing rules sit at position=1 and shadow the rule a test just",
        "  created, so the test asserts against someone else's data and the",
        "  failure message points at the product. That cost 19 tests over two",
        "  days.",
        "",
        "  The end-of-run orphan guard cannot see these: it compares against the",
        "  count at session START, and these were already in it.",
        "",
        "  Clear them (local database only) with:",
        "      python -m scripts.test_guards --clean-preexisting",
        "=" * 70]
    return "\n".join(lines)


def clean_preexisting(dry_run: bool = True) -> Dict[str, int]:
    """Delete the rows preexisting_orphans() reports. Local databases only.

    Separate from the reporter, and dry by default, because deleting rows is not
    something a test session should do behind the developer's back — the guard
    reports, a human decides.

    It removes the ROWS and leaves their record_field_history entries alone.
    That is deliberate: the history is append-only audit, purging it needs the
    guards lowered, and this codebase does not destroy historical evidence to
    tidy up. The orphaned rules are what shadow a test's own data; their audit
    trail harms nothing.
    """
    conn = _connect()
    if conn is None:
        return {}
    removed: Dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT inet_server_addr()::text")
            addr = (cur.fetchone() or [None])[0]
            if addr is not None and not str(addr).startswith(("127.", "::1")):
                raise RuntimeError(
                    f"refusing to delete on a non-loopback server ({addr}); "
                    "this is a local-database cleanup only")
            for table, column, values, _writer in PREEXISTING_MARKERS:
                try:
                    cur.execute(
                        f"SELECT count(*) FROM {table} WHERE {column} = ANY(%s)",
                        (list(values),))
                    n = cur.fetchone()[0]
                    if not n:
                        continue
                    removed[table] = n
                    if not dry_run:
                        cur.execute(
                            f"DELETE FROM {table} WHERE {column} = ANY(%s)",
                            (list(values),))
                except Exception:
                    conn.rollback()
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return removed


def report_orphans(baseline: Dict[str, int]) -> Optional[str]:
    """Message naming every table the session grew, else None."""
    if not baseline:
        return None
    after = row_counts()
    leaked = {t: after[t] - baseline[t] for t in baseline
              if t in after and after[t] > baseline[t]}
    if not leaked:
        return None
    lines = ["\n" + "=" * 70,
             "ORPHANED TEST DATA — this run added rows and did not remove them"]
    for table, n in sorted(leaked.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {table:24} +{n}")
    lines += [
        "",
        "  Left in place, these become the next run's ambient state: leftover",
        "  routing rules shadow the rules tests create, and leftover cases",
        "  inflate every count. Find the fixture whose teardown did not run.",
        "=" * 70]
    return "\n".join(lines)


if __name__ == "__main__":                                  # pragma: no cover
    import sys

    if "--clean-preexisting" in sys.argv:
        dry = "--yes" not in sys.argv
        found = clean_preexisting(dry_run=dry)
        if not found:
            print("no pre-existing test data found")
        else:
            for table, n in sorted(found.items()):
                print(f"  {table:20} {n:>4} row(s) "
                      f"{'would be removed' if dry else 'REMOVED'}")
            if dry:
                print("\ndry run — re-run with --yes to delete")
    else:
        print(preexisting_orphans() or "no pre-existing test data found")
