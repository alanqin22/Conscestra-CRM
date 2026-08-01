"""The second case-mutation boundary — sp_cases().

A 14-mode stored procedure authored in January 2026 beside the cases table and
never governed. It writes `status` and `owner_id` and has no
record_field_history awareness, so executing it skips the state machine, owner
validation and field history in one call.

WHY THE DEFENCE IS APPLICATION-LAYER ONLY (for now):
the application connects as `postgres`, which OWNS the function and is a
SUPERUSER. PostgreSQL superusers bypass privilege checks, so REVOKE empties the
ACL and changes nothing — verified in test_20 below. Until the app runs as a
non-superuser role, guard_query IS the control, not a courtesy on top of one.
"""
import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.core import write_guard                                # noqa: E402
from app.core.database import get_connection                    # noqa: E402
from app.core.write_guard import WritePermissionError           # noqa: E402

SIG = ("public.sp_cases(text,uuid,uuid,uuid,text,text,text,text,text,uuid,"
       "text,boolean,uuid,text,jsonb,uuid,uuid,integer,integer,text,date,"
       "date,boolean)")


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        get_connection().close()
    except Exception as exc:                                    # pragma: no cover
        pytest.skip(f"no database reachable: {exc}")


# ── 1. the guard rejects it, for every caller ────────────────────────────────

@pytest.mark.parametrize("q", [
    "SELECT sp_cases(p_mode := 'close', p_case_id := '...') AS result",
    "SELECT sp_cases(p_mode := 'list') AS result",
    "select  SP_CASES ( p_mode := 'assign' ) as result",
    "SELECT public.sp_cases(p_mode:='reopen') AS result",
])
def test_01_forbidden_procedure_is_refused(q):
    with pytest.raises(WritePermissionError) as e:
        write_guard.guard_query(q)
    assert "forbidden legacy write path" in str(e.value)


def test_02_refused_for_the_system_context_too():
    """role None (background/system) returns early from every role check below
    — which is exactly why the forbidden check runs FIRST."""
    assert write_guard._role.get() is None
    with pytest.raises(WritePermissionError):
        write_guard.guard_query("SELECT sp_cases(p_mode := 'update') AS result")


def test_03_refused_for_a_write_capable_role(monkeypatch):
    """Admin is the caller most able to do damage, and the one the ordinary
    role logic waves through."""
    tok = write_guard._role.set("admin")
    try:
        with pytest.raises(WritePermissionError):
            write_guard.guard_query("SELECT sp_cases(p_mode := 'close') AS result")
    finally:
        write_guard._role.reset(tok)


def test_04_the_error_names_the_governed_path():
    with pytest.raises(WritePermissionError) as e:
        write_guard.guard_query("SELECT sp_cases(p_mode := 'resolve') AS result")
    assert "app/core/cases.py" in str(e.value)


def test_05_the_query_is_never_rewritten_or_rerouted():
    """A forbidden path must fail visibly, so the caller learns their code is
    wrong instead of appearing to work."""
    q = "SELECT sp_cases(p_mode := 'close') AS result"
    with pytest.raises(WritePermissionError):
        write_guard.guard_query(q)
    assert q == "SELECT sp_cases(p_mode := 'close') AS result"


def test_06_execute_sp_refuses_it_end_to_end():
    from app.core.database import execute_sp
    with pytest.raises(WritePermissionError):
        execute_sp("SELECT sp_cases(p_mode := 'list') AS result")


def test_07_unrelated_procedures_are_unaffected():
    """The denylist must not become a blanket SP ban."""
    for q in ("SELECT sp_accounts(p_mode := 'list') AS result",
              "SELECT sp_contacts(p_mode := 'create') AS result",
              "SELECT sp_activities(p_mode := 'list') AS result"):
        write_guard.guard_query(q)          # must not raise


def test_08_a_mere_mention_without_a_call_is_not_matched():
    write_guard.guard_query("-- sp_cases is forbidden; see write_guard")


# ── 2. the database layer is INERT, and that is the point ────────────────────

def test_20_revoke_cannot_stop_a_superuser(tmp_path):
    """Documents WHY there is no REVOKE in the migration set.

    Runs inside a transaction that is rolled back, so no privilege is actually
    changed. If this test ever FAILS, the application has been moved to a
    non-superuser role and the database half of the defence has become real —
    at which point applying the REVOKE is worthwhile."""
    c = get_connection()
    try:
        cur = c.cursor()
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
        if not cur.fetchone()[0]:
            pytest.skip("app role is no longer a superuser — revisit the REVOKE")
        cur.execute(f"REVOKE ALL ON FUNCTION {SIG} FROM PUBLIC")
        cur.execute(f"REVOKE ALL ON FUNCTION {SIG} FROM postgres")
        cur.execute("SELECT has_function_privilege(current_user, p.oid, "
                    "'EXECUTE') FROM pg_proc p WHERE p.proname='sp_cases'")
        still_has = cur.fetchone()[0]
        c.rollback()
    finally:
        c.close()
    assert still_has is True, (
        "the app role no longer bypasses privilege checks — the REVOKE half of "
        "the defence is now effective and should be applied")


def test_21_the_function_still_exists_and_that_is_expected():
    """DROP is deliberately deferred: the signature and recovery path are
    documented, but removal waits until the revocation has been observed."""
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='sp_cases'")
            assert cur.fetchone()[0] == 1
    finally:
        c.close()


# ── 3. the canonical path is untouched ───────────────────────────────────────

def test_30_governed_case_operations_still_work():
    from app.core import cases
    r = cases.open_case("second-boundary probe", actor="test", source="test")
    cid = r["case_id"]
    try:
        cases.transition(cid, "in_progress", actor="test", source="test")
        cases.set_priority(cid, "high", actor="test", source="test")
        assert cases.get(cid)["status"] == "in_progress"
        assert len(cases.history(cid)) >= 3
    finally:
        c = get_connection()
        try:
            with c.cursor() as cur:
                cur.execute("ALTER TABLE record_field_history DISABLE TRIGGER "
                            "trg_rfh_append_only")
                cur.execute("DELETE FROM record_field_history WHERE entity='case' "
                            "AND entity_id=%s::uuid", (cid,))
                cur.execute("ALTER TABLE record_field_history ENABLE TRIGGER "
                            "trg_rfh_append_only")
                cur.execute("DELETE FROM cases WHERE case_id=%s::uuid", (cid,))
            c.commit()
        finally:
            c.close()


def test_31_no_business_data_was_touched_by_the_defence():
    """The guard is code; it changes no rows. These are the counts the case
    work has held at every step.

    Scoped to entity='case': the table is shared, and routing policy edits
    legitimately write `routing_rule` rows into it (C2.1). A whole-table count
    would make this test fail for a reason that has nothing to do with the
    boundary it guards."""
    c = get_connection()
    try:
        with c.cursor() as cur:
            for q, want in (("SELECT count(*) FROM cases", 120),
                            ("SELECT count(*) FROM cases WHERE is_historical", 120),
                            ("SELECT count(*) FROM case_comments", 480),
                            ("SELECT count(*) FROM record_field_history "
                             "WHERE entity='case'", 0)):
                cur.execute(q)
                assert cur.fetchone()[0] == want, q
    finally:
        c.close()
