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
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# Tables where a leaked row changes another test's answer. Not every table —
# only those whose contents are read as ambient state by some test.
ORPHAN_WATCH: Tuple[str, ...] = (
    "routing_rules", "cases", "case_comments", "customer_memories",
    "content_embeddings", "agent_utterances", "memory_verifications",
    "conversations",
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
