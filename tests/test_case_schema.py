"""C1 Step 1 — the case lifecycle schema (sql/case_lifecycle.sql).

These assert the INVARIANTS the migration exists to create, not the presence of
columns for its own sake:

  * history is append-only, enforced by the database (editable history is not
    history, and owner reassignment cannot be reconstructed after the fact)
  * the 120 pre-C1 rows are marked historical, and their unknown timestamps
    stayed NULL rather than being invented from closed_at
  * one LIVE case per conversation, so a repeat escalation attaches instead of
    forking a second unit of work
  * the migration is additive — closed_at, status and the row count survive

Skipped when no database is reachable, so the suite still runs standalone.
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
# psycopg2 does not adapt uuid.UUID unless told to; without this every
# parameterised uuid raises "can't adapt type 'UUID'".
from psycopg2.extras import register_uuid                      # noqa: E402

register_uuid()


@pytest.fixture(scope="module")
def conn():
    try:
        from app.core.config import settings
        dsn = os.getenv("DATABASE_URL") or settings.db_dsn
        c = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as exc:                                  # pragma: no cover
        pytest.skip(f"no database reachable: {exc}")
    c.autocommit = True
    yield c
    c.close()


@pytest.fixture(scope="module", autouse=True)
def _migrated(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.record_field_history')")
        if cur.fetchone()[0] is None:
            pytest.skip("sql/case_lifecycle.sql not applied to this database")


@pytest.fixture
def tx(conn):
    """A cursor whose work is always rolled back.

    Note the cleanup CANNOT be DELETE: the append-only guard refuses it, which
    is the trigger doing precisely its job. ROLLBACK removes the rows without
    firing it — so these tests exercise the real table and leave no residue."""
    c = conn
    old = c.autocommit
    c.autocommit = False
    cur = c.cursor()
    try:
        yield cur
    finally:
        c.rollback()
        cur.close()
        c.autocommit = old


def _cols(conn, table):
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name=%s""", (table,))
        return {r[0] for r in cur.fetchall()}


# ── the connective columns exist ─────────────────────────────────────────────

@pytest.mark.parametrize("col", [
    "conversation_id", "escalation_id", "first_response_at",
    "resolved_at", "reopen_count", "is_historical",
])
def test_01_cases_gained_the_lifecycle_columns(conn, col):
    assert col in _cols(conn, "cases")


def test_02_sla_columns_were_NOT_added_to_cases(conn):
    """Open design question 3 must stay open. Adding sla_due_at here would
    silently decide that the case owns the deadline, and C6 would then have to
    reconcile two populated SLA columns."""
    cols = _cols(conn, "cases")
    assert "sla_due_at" not in cols and "sla_minutes" not in cols


def test_03_waiting_pause_columns_were_NOT_added(conn):
    """Open design question 1 (does `waiting` stop the clock?) stays open."""
    cols = _cols(conn, "cases")
    assert not {"sla_paused_at", "waiting_reason", "clock_paused"} & cols


def test_04_case_comments_untouched(conn):
    """Open design question 2 (comment locality) stays open."""
    assert _cols(conn, "case_comments") == {
        "case_comment_id", "case_id", "comment", "is_internal",
        "created_at", "created_by"}


# ── history is append-only, and enforced ─────────────────────────────────────

def test_10_history_insert_then_update_is_refused(tx):
    tx.execute("""INSERT INTO record_field_history
          (entity, entity_id, field, old_value, new_value, actor)
          VALUES ('case', %s, 'status', 'new', 'in_progress', 'test')
          RETURNING history_id""", (uuid.uuid4(),))
    hid = tx.fetchone()[0]
    with pytest.raises(psycopg2.errors.RaiseException):
        tx.execute("UPDATE record_field_history SET new_value='closed' "
                   "WHERE history_id=%s", (hid,))


def test_11_history_delete_is_refused(tx):
    eid = uuid.uuid4()
    tx.execute("""INSERT INTO record_field_history
          (entity, entity_id, field, old_value, new_value, actor)
          VALUES ('case', %s, 'owner_id', NULL, %s, 'test')""",
               (eid, str(uuid.uuid4())))
    with pytest.raises(psycopg2.errors.RaiseException):
        tx.execute("DELETE FROM record_field_history WHERE entity_id=%s", (eid,))


def test_12_owner_reassignment_stays_provable(tx):
    """The reason C7 was promoted into a constraint on C1: once owner_id is
    overwritten the previous owner is gone unless it was recorded first."""
    eid, a, b = uuid.uuid4(), str(uuid.uuid4()), str(uuid.uuid4())
    for old, new in ((None, a), (a, b)):
        tx.execute("""INSERT INTO record_field_history
              (entity, entity_id, field, old_value, new_value, actor)
              VALUES ('case', %s, 'owner_id', %s, %s, 'test')""", (eid, old, new))
    tx.execute("""SELECT old_value, new_value FROM record_field_history
          WHERE entity_id=%s AND field='owner_id' ORDER BY changed_at""", (eid,))
    assert tx.fetchall() == [(None, a), (a, b)], \
        "the chain of custody must be readable"


def test_13_null_old_value_means_previously_unset(tx):
    """NULL must survive as NULL - not become '' or 'unknown'."""
    eid = uuid.uuid4()
    tx.execute("""INSERT INTO record_field_history
          (entity, entity_id, field, old_value, new_value, actor)
          VALUES ('case', %s, 'priority', NULL, 'urgent', 'test')""", (eid,))
    tx.execute("SELECT old_value FROM record_field_history WHERE entity_id=%s",
               (eid,))
    assert tx.fetchone()[0] is None


def test_14_actor_supports_non_human_actors(tx):
    """'agent:sdr' and 'escalation' have no uuid; a uuid-only actor column
    could not represent the majority of writes."""
    eid = uuid.uuid4()
    tx.execute("""INSERT INTO record_field_history
          (entity, entity_id, field, new_value, actor, actor_id, source)
          VALUES ('case', %s, 'status', 'in_progress', 'agent:sdr', NULL,
                  'escalation')""", (eid,))
    tx.execute("SELECT actor, actor_id FROM record_field_history "
               "WHERE entity_id=%s", (eid,))
    assert tx.fetchone() == ("agent:sdr", None)


# ── historical honesty ───────────────────────────────────────────────────────

def test_20_pre_existing_cases_are_marked_historical(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cases WHERE conversation_id IS NULL "
                    "AND is_historical = false")
        assert cur.fetchone()[0] == 0, (
            "a pre-C1 row escaped the backfill and would be averaged into "
            "first-response metrics as if it were instant")


def test_21_closed_at_was_not_copied_into_resolved_at(conn):
    """Resolution and closure are different events. Copying one into the other
    invents data and corrupts every resolution-time statistic."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cases "
                    "WHERE is_historical AND resolved_at IS NOT NULL")
        assert cur.fetchone()[0] == 0


def test_22_historical_rows_keep_unknown_timestamps_null(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cases "
                    "WHERE is_historical AND first_response_at IS NOT NULL")
        assert cur.fetchone()[0] == 0


def test_23_new_cases_default_to_not_historical(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT column_default, is_nullable
                       FROM information_schema.columns
                       WHERE table_name='cases' AND column_name='is_historical'""")
        default, nullable = cur.fetchone()
        assert "false" in (default or "") and nullable == "NO"


def test_24_migration_was_additive(conn):
    """The 120 rows, their statuses and their closed_at survive untouched."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cases")
        assert cur.fetchone()[0] >= 120
        cur.execute("SELECT count(*) FROM cases WHERE closed_at IS NOT NULL")
        assert cur.fetchone()[0] == 48
        cur.execute("SELECT count(DISTINCT status) FROM cases")
        assert cur.fetchone()[0] == 5


# ── one live case per conversation ───────────────────────────────────────────

def test_30_second_live_case_on_same_conversation_is_refused(tx):
    """D1: a repeat escalation ATTACHES to the existing case; it must not fork
    a second unit of work for the same thread."""
    tx.execute("SELECT conversation_id FROM conversations LIMIT 1")
    row = tx.fetchone()
    if not row:
        pytest.skip("no conversations to bind to")
    tx.execute("""INSERT INTO cases (subject, status, conversation_id)
          VALUES ('c1 schema test A', 'new', %s)""", (row[0],))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        tx.execute("""INSERT INTO cases (subject, status, conversation_id)
              VALUES ('c1 schema test B', 'new', %s)""", (row[0],))


def test_31_closed_case_frees_the_conversation(tx):
    """D3 defines a reopen as a NEW linked case, so 'closed' must not block one."""
    tx.execute("SELECT conversation_id FROM conversations LIMIT 1")
    row = tx.fetchone()
    if not row:
        pytest.skip("no conversations to bind to")
    tx.execute("""INSERT INTO cases (subject, status, conversation_id)
          VALUES ('c1 closed', 'closed', %s)""", (row[0],))
    tx.execute("""INSERT INTO cases (subject, status, conversation_id)
          VALUES ('c1 reopen', 'new', %s)""", (row[0],))


def test_32_losing_the_conversation_does_not_delete_the_work(conn):
    """ON DELETE SET NULL, not CASCADE — the work record outlives the event
    that created it. That asymmetry is the point of the whole axis."""
    with conn.cursor() as cur:
        cur.execute("""SELECT confdeltype FROM pg_constraint
                       WHERE conname='cases_conversation_id_fkey'""")
        assert cur.fetchone()[0] == "n"          # 'n' = SET NULL
        cur.execute("""SELECT confdeltype FROM pg_constraint
                       WHERE conname='cases_escalation_id_fkey'""")
        assert cur.fetchone()[0] == "n"
