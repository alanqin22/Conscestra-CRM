"""C1 Step 5 — the Cases agent package.

The architectural invariant this step must hold, and the reason the package
deviates from every other agent:

    An LLM may choose an ACTION. It may never compose a case WRITE.

Every other agent builds SQL or a stored-procedure call and hands it to
execute_sp(). Doing that for cases would route writes around app/core/cases.py
and silently bypass the state machine, owner validation and field history that
Steps 2-4 exist to enforce. So reads are allow-listed SELECTs and writes are
delegated — these tests hold that line.
"""
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                      # noqa: E402

register_uuid()

from app.agents.cases import pre_router, sql_builder           # noqa: E402
from app.agents.cases.formatter import format_response         # noqa: E402
from app.core import cases                                     # noqa: E402
from app.core.database import get_connection                   # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.record_field_history')")
            if cur.fetchone()[0] is None:
                pytest.skip("case migrations not applied")
        c.close()
    except Exception as exc:                                   # pragma: no cover
        pytest.skip(f"no database reachable: {exc}")


def _purge(case_id):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE record_field_history DISABLE TRIGGER "
                        "trg_rfh_append_only")
            cur.execute("DELETE FROM record_field_history WHERE entity='case' "
                        "AND entity_id=%s::uuid", (case_id,))
            cur.execute("ALTER TABLE record_field_history ENABLE TRIGGER "
                        "trg_rfh_append_only")
            cur.execute("DELETE FROM case_comments WHERE case_id=%s::uuid",
                        (case_id,))
            cur.execute("DELETE FROM cases WHERE case_id=%s::uuid", (case_id,))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def case():
    made = []

    def _make(subject="agent test case", **kw):
        r = sql_builder.execute("open_case", {"subject": subject, **kw})
        cid = r["result"]["case_id"]
        made.append(cid)
        return cid

    yield _make
    for cid in made:
        _purge(cid)


@pytest.fixture
def an_owner():
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT owner_id::text, email FROM owners "
                        "WHERE email IS NOT NULL AND coalesce(is_active,true) "
                        "LIMIT 1")
            row = cur.fetchone()
    finally:
        c.close()
    if not row:
        pytest.skip("need an owner with an email")
    return row


# ── 1. the write layer cannot be bypassed ────────────────────────────────────

def test_01_the_lifecycle_is_still_enforced(case):
    """The agent is not a second door into the case tables."""
    cid = case()
    r = sql_builder.execute("transition", {"case_id": cid, "to_status": "closed"})
    assert r["ok"] is False and r["refused"] is True
    assert cases.get(cid)["status"] == "new"


def test_02_unknown_status_is_refused(case):
    r = sql_builder.execute("transition", {"case_id": case(),
                                           "to_status": "escalated"})
    assert r["ok"] is False and r["refused"] is True


def test_03_writes_produce_field_history(case):
    cid = case()
    sql_builder.execute("transition", {"case_id": cid,
                                       "to_status": "in_progress",
                                       "actor": "rep@example.com"})
    rows = [h for h in cases.history(cid) if h["field"] == "status"]
    assert rows[-1]["old_value"] == "new"
    assert rows[-1]["new_value"] == "in_progress"
    assert rows[-1]["source"] == "cases-agent"


def test_04_assignment_requires_a_real_identity(case):
    r = sql_builder.execute("assign", {"case_id": case(),
                                       "owner_email": "ghost@example.invalid"})
    assert r["ok"] is False and r["refused"] is True
    assert "not a known CRM owner" in r["error"]


def test_05_assignment_by_email_resolves(case, an_owner):
    owner_id, email = an_owner
    cid = case()
    r = sql_builder.execute("assign", {"case_id": cid, "owner_email": email})
    assert r["ok"] and cases.get(cid)["owner_id"] == owner_id


def test_06_no_action_composes_sql_for_a_write():
    """Reads may build a SELECT; a write must delegate. This fails if someone
    adds an UPDATE/INSERT/DELETE into the executor."""
    import inspect
    src = inspect.getsource(sql_builder)
    body = src.split("# ── writes", 1)[1]
    for verb in ("UPDATE ", "INSERT ", "DELETE "):
        assert verb not in body.upper(), (
            f"the write path composes {verb.strip()} directly instead of "
            f"delegating to app/core/cases.py")


def test_07_every_write_action_delegates_to_the_case_layer():
    import inspect
    src = inspect.getsource(sql_builder)
    body = src.split("# ── writes", 1)[1]
    assert body.count("cases.") >= len(sql_builder.WRITE_ACTIONS)


# ── 2. reads ─────────────────────────────────────────────────────────────────

def test_10_list_excludes_historical_rows(case):
    """The 120 pre-lifecycle rows must not appear in live work lists."""
    case()
    rows = sql_builder.execute("list_cases", {"limit": 100})["rows"]
    assert rows and all(r["is_historical"] is False for r in rows)


def test_11_queue_shows_only_live_work(case):
    cid = case()
    for s in ("in_progress", "resolved", "closed"):
        cases.transition(cid, s, actor="test")
    ids = [r["case_id"] for r in
           sql_builder.execute("case_queue", {"limit": 100})["rows"]]
    assert cid not in ids


def test_12_get_case_includes_next_states_and_comments(case):
    cid = case()
    sql_builder.execute("add_comment", {"case_id": cid, "body": "a note",
                                        "internal": True})
    row = sql_builder.execute("get_case", {"case_id": cid})["rows"][0]
    assert row["next_states"] == ["in_progress"]
    assert row["comments"][0]["comment"] == "a note"


def test_13_unowned_filter(case):
    cid = case()
    ids = [r["case_id"] for r in
           sql_builder.execute("list_cases", {"unowned": True,
                                              "limit": 100})["rows"]]
    assert cid in ids


def test_14_bad_filters_are_refused_not_interpolated(case):
    r = sql_builder.execute("list_cases", {"status": "'; DROP TABLE cases;--"})
    assert r["ok"] is False and "unknown status" in r["error"]


def test_15_missing_case_id_is_refused():
    assert sql_builder.execute("get_case", {})["ok"] is False


def test_16_unknown_action_is_refused():
    r = sql_builder.execute("frobnicate", {})
    assert r["ok"] is False and "unknown action" in r["error"]


def test_17_history_is_readable_through_the_agent(case):
    cid = case()
    sql_builder.execute("transition", {"case_id": cid,
                                       "to_status": "in_progress"})
    rows = sql_builder.execute("case_history", {"case_id": cid})["rows"]
    assert any(r["field"] == "status" for r in rows)


# ── 3. pre-router ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,action", [
    ("list", "list_cases"), ("queue", "case_queue"),
    ("unowned", "list_cases"), ("get", "get_case"), ("history", "case_history"),
])
def test_20_direct_modes_bypass_the_model(mode, action):
    hit = pre_router.route({"mode": mode, "caseId": str(uuid.uuid4())})
    assert hit and hit[0] == action


def test_21_read_phrasings_route_deterministically():
    assert pre_router.route({"message": "show me the case queue"})[0] == "case_queue"
    assert pre_router.route({"message": "which cases are unowned?"})[0] == "list_cases"


def test_22_writes_are_never_inferred_from_a_keyword():
    """Moving a case through its lifecycle is consequential enough to need the
    model's reading of the whole sentence, or an explicit UI mode."""
    for msg in ("close case abc", "resolve this case", "assign it to me",
                "set priority urgent"):
        hit = pre_router.route({"message": msg})
        assert hit is None or hit[0] in sql_builder.READ_ACTIONS


def test_23_unknown_message_falls_through_to_the_model():
    assert pre_router.route({"message": "what is going on with Acme?"}) is None


# ── 4. formatter honesty ─────────────────────────────────────────────────────

def test_30_refusals_are_rendered_as_answers():
    out = format_response({"ok": False, "refused": True, "action": "transition",
                           "error": "new -> closed is not a permitted transition"})
    assert "isn't permitted" in out["output"]
    assert "not a permitted transition" in out["output"]


def test_31_historical_rows_say_unknown_not_zero():
    out = format_response({"ok": True, "action": "get_case", "rows": [{
        "subject": "old", "status": "closed", "priority": "low",
        "is_historical": True, "first_response_at": None, "created_at": ""}]})
    assert "unknown, not zero" in out["output"]


def test_32_unowned_says_why():
    out = format_response({"ok": True, "action": "list_cases", "rows": [{
        "subject": "s", "status": "new", "priority": "low", "case_id": "x" * 8,
        "owner_id": None, "source_assignee": "agent"}]})
    assert "source: agent" in out["output"]
    assert "1 of these are unowned" in out["output"]


# ── 5. flags ─────────────────────────────────────────────────────────────────

def test_40_disabled_blocks_agent_writes(case, monkeypatch):
    cid = case()
    monkeypatch.setattr(cases, "ENABLED", False)
    r = sql_builder.execute("transition", {"case_id": cid,
                                           "to_status": "in_progress"})
    assert r["ok"] is False
    monkeypatch.setattr(cases, "ENABLED", True)
    assert cases.get(cid)["status"] == "new"
