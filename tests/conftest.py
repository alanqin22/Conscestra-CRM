"""Test-session policy.

THE DEFECT THIS EXISTS TO KILL. 591 of 1038 tests skipped when no database was
reachable — 57% of the suite, and disproportionately the safety case: erasure,
append-only enforcement, gate convergence, signature validation, contradiction
linking. `pytest.skip("no database reachable")` reports GREEN. So the strongest
claims in this codebase were guarded by tests that a clean checkout silently
does not run, and there was no CI to notice.

Skipping is right for a developer without Postgres. It is not right for the
pipeline that gates a release. `CRM_REQUIRE_DB=1` converts every skip into a
failure, so the pipeline cannot pass by not looking.

A SECOND, QUIETER DEFECT: these tests read whatever happens to be in the
database. One of them hardcoded entity_type='contact' while selecting an
arbitrary entity, and passed for months because the first row happened to be a
contact — it broke the moment the corpus covered accounts. `seeded_corpus`
gives deterministic data to the tests that need it, so passing stops being a
property of ambient state.
"""

from __future__ import annotations

import os
import uuid

import pytest


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


REQUIRE_DB = _truthy("CRM_REQUIRE_DB")

# Skips that mean "the environment is not set up" rather than "this case does
# not apply". Under CRM_REQUIRE_DB these become failures.
_ENV_SKIP_MARKERS = (
    "no database", "database not", "not applied", "migrations",
    "no memories", "no consolidated", "no indexed", "no themes",
    "no contact memories", "run sql/",
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "requires_db: needs a reachable, migrated database")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Turn environment-shaped skips into failures when the pipeline demands it.

    A hook rather than edits to 29 test files: the rule is a property of the
    RUN, not of any individual test, and encoding it per-file guarantees the
    next test written forgets it."""
    outcome = yield
    if not REQUIRE_DB:
        return
    report = outcome.get_result()
    if report.skipped and isinstance(report.longrepr, tuple):
        reason = str(report.longrepr[2]).lower()
        if any(m in reason for m in _ENV_SKIP_MARKERS):
            report.outcome = "failed"
            report.longrepr = (
                f"CRM_REQUIRE_DB=1: refusing to pass on a skipped "
                f"environment-dependent test.\n  reason: {report.longrepr[2]}\n"
                f"  fix: provision Postgres and run `python -m scripts.migrate`."
            )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Make the skip count impossible to miss. It was 591 and nobody knew."""
    skipped = len(terminalreporter.stats.get("skipped", []))
    passed = len(terminalreporter.stats.get("passed", []))
    total = skipped + passed + len(terminalreporter.stats.get("failed", []))
    if total and skipped / total > 0.10:
        terminalreporter.write_sep(
            "!", f"{skipped}/{total} tests SKIPPED "
                 f"({skipped / total:.0%}) — coverage is not what it looks like",
            red=True)


@pytest.fixture(scope="session")
def db_url() -> str:
    from app.core.config import get_settings
    return get_settings().db_dsn


@pytest.fixture(scope="session")
def db_ready(db_url) -> bool:
    """Reachable AND migrated. 'Reachable' alone was never the question —
    a database missing sql/memory_audit_erasure.sql passes a connection check
    and fails every erasure guarantee."""
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
    except Exception as exc:
        if REQUIRE_DB:
            pytest.fail(f"CRM_REQUIRE_DB=1 but no database: {exc}")
        pytest.skip("no database reachable")
    try:
        from app.core.deploy_state import check_migrations
        state = check_migrations()
        if not state.get("ok") and REQUIRE_DB:
            pytest.fail(f"database is not migrated: {state}")
        return True
    finally:
        conn.close()


@pytest.fixture
def seeded_corpus(db_ready):
    """A deterministic entity with known memories, removed afterwards.

    Tests that assert on 'the first row in customer_memories' are asserting on
    whatever the last person's script left behind. This gives them a fixed
    subject instead."""
    from app.core.database import get_connection
    eid = str(uuid.uuid4())
    conn = get_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for topic, kind, vis in (("billing", "theme", "internal"),
                                     ("delivery", "theme", "customer"),
                                     ("pricing", "hypothesis", "internal")):
                cur.execute(
                    """INSERT INTO customer_memories
                         (entity_type, entity_id, kind, statement, topic,
                          occurrences, evidence, evidence_count, evidence_hash,
                          source_type, reliability, certainty, generator,
                          visibility, last_observed_at, status)
                       VALUES ('contact',%s::uuid,%s,%s,%s,3,'[]'::jsonb,0,%s,
                               'ai',0.8,0.9,'pytest/seed',%s,now(),'active')""",
                    (eid, kind, f"Seeded {topic} claim.", topic,
                     f"seed-{topic}", vis))
        yield {"entity_type": "contact", "entity_id": eid,
               "topics": ["billing", "delivery", "pricing"]}
    finally:
        with conn.cursor() as cur:
            # Declared, so the undo log records WHY these rows vanished.
            cur.execute("SET app.repair_key = 'pytest:seeded_corpus'")
            cur.execute("DELETE FROM customer_memories WHERE entity_id=%s::uuid",
                        (eid,))
            cur.execute("RESET app.repair_key")
        conn.close()
