"""C1 Step 6 — the Case Management surface.

The page is an operational surface, not CRUD, and it must not become a second
write path. Every button lands on the SAME execute() -> app/core/cases.py chain
the agent's actions take; the model is bypassed for determinism, the domain
layer is not bypassed at all.

The properties held here are the ones a UI most easily breaks:
  * the transition matrix is not duplicated in frontend code
  * a stale page cannot force an illegal move — the server refuses and the page
    refreshes, and the refusal reads as a business answer, not a generic error
  * unowned work is never hidden, and says why
  * unknown timestamps on historical rows render as UNKNOWN, never zero
"""
import pathlib
import re
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.agents.cases import pre_router, sql_builder            # noqa: E402
from app.core import cases, write_guard                         # noqa: E402
from app.core.database import get_connection                    # noqa: E402

PAGE = pathlib.Path("case-mgmt.html")
HTML = PAGE.read_text(encoding="utf-8") if PAGE.exists() else ""


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        get_connection().close()
    except Exception as exc:                                    # pragma: no cover
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

    def _make(**kw):
        cid = cases.open_case(kw.pop("subject", "ui test case"), actor="test",
                              source="test", **kw)["case_id"]
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


def ui(payload):
    """What the page's call() does: pre-route the mode, then execute."""
    hit = pre_router.route(payload)
    assert hit, f"no direct route for {payload.get('mode')!r}"
    action, params = hit
    return sql_builder.execute(action, params)


# ── 1. page integration ──────────────────────────────────────────────────────

def test_01_page_exists():
    assert HTML, "case-mgmt.html is missing"


def test_02_page_is_registered_in_chat_pages():
    main = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    block = main.split("_CHAT_PAGES = [", 1)[1].split("]", 1)[0]
    assert '"case-mgmt.html"' in block


def test_03_page_uses_the_shared_auth_shim():
    assert "orbit_auth_session" in HTML and "authBanner" in HTML


def test_04_page_distinguishes_auth_from_unreachable_from_failed():
    """A dashboard that collapses these into 'failed to load' has said nothing."""
    for kind in ("auth", "unreachable", "error"):
        assert f"'{kind}'" in HTML


def test_05_case_chat_authorization_is_not_weakened():
    main = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    assert re.search(r"app\.include_router\(cases_router,\s*dependencies=_DATA\)",
                     main)


# ── 2. the UI is not a second write path ─────────────────────────────────────

def test_10_page_never_talks_to_the_database_or_the_legacy_procedure():
    for forbidden in ("sp_cases", "UPDATE cases", "INSERT INTO cases",
                      "DELETE FROM cases", "record_field_history"):
        assert forbidden not in HTML, f"the page references {forbidden}"


def test_11_page_only_calls_case_chat():
    endpoints = set(re.findall(r"fetch\(API \+ '([^']+)'", HTML))
    assert endpoints <= {"/case-chat", "/cases-health"}, endpoints


def test_12_the_transition_matrix_is_not_duplicated_in_the_frontend():
    """The page renders whatever next_states the server sent. If it ever grows
    its own map of status -> allowed, a stale copy starts deciding."""
    assert "next_states" in HTML
    for pair in ("new':", "in_progress':", "waiting':"):
        assert f"'{pair}" not in HTML.replace(" ", "")


def test_13_ui_writes_land_on_the_governed_layer(case):
    cid = case()
    r = ui({"mode": "transition", "caseId": cid, "toStatus": "in_progress"})
    assert r["ok"] and cases.get(cid)["status"] == "in_progress"
    assert [h for h in cases.history(cid) if h["field"] == "status"][-1][
        "new_value"] == "in_progress"


# ── 3. lifecycle: the server stays authoritative ─────────────────────────────

def test_20_valid_transition_through_the_ui_path(case):
    cid = case()
    assert ui({"mode": "transition", "caseId": cid,
               "toStatus": "in_progress"})["ok"]


def test_21_stale_ui_cannot_force_an_illegal_move(case):
    """The page may be showing yesterday's buttons; the server decides."""
    cid = case()
    r = ui({"mode": "transition", "caseId": cid, "toStatus": "closed"})
    assert r["ok"] is False and r["refused"] is True
    assert cases.get(cid)["status"] == "new"


def test_22_refusal_carries_a_business_reason_not_a_stack_trace(case):
    r = ui({"mode": "transition", "caseId": case(), "toStatus": "closed"})
    assert "not a permitted transition" in r["error"]
    assert "Traceback" not in r["error"] and "psycopg2" not in r["error"]


def test_23_the_page_refreshes_after_a_refusal():
    assert "afterWrite" in HTML and "openCase(CURRENT.case_id)" in HTML


def test_24_terminal_state_offers_no_actions(case):
    cid = case()
    for s in ("in_progress", "resolved", "closed"):
        cases.transition(cid, s, actor="test")
    row = ui({"mode": "get", "caseId": cid})["rows"][0]
    assert row["next_states"] == []
    assert "terminal — no further transitions" in HTML


# ── 4. assignment ────────────────────────────────────────────────────────────

def test_30_owner_list_is_available_for_the_dropdown():
    rows = ui({"mode": "owners"})["rows"]
    assert rows and all(r["owner_id"] and r["email"] for r in rows)


def test_31_assignment_resolves_an_email_to_a_uuid(case, an_owner):
    owner_id, email = an_owner
    cid = case()
    assert ui({"mode": "assign", "caseId": cid, "ownerEmail": email})["ok"]
    assert cases.get(cid)["owner_id"] == owner_id


@pytest.mark.parametrize("bad", ["agent", "Alan Qin", "ghost@example.invalid"])
def test_32_a_name_or_placeholder_can_never_become_an_owner(case, bad):
    cid = case()
    r = ui({"mode": "assign", "caseId": cid, "ownerEmail": bad})
    assert r["ok"] is False and r["refused"] is True
    assert cases.get(cid)["owner_id"] is None


def test_33_reassignment_is_recorded(case, an_owner):
    owner_id, email = an_owner
    cid = case()
    ui({"mode": "assign", "caseId": cid, "ownerEmail": email})
    rows = [h for h in cases.history(cid) if h["field"] == "owner_id"]
    assert rows[-1]["old_value"] is None and rows[-1]["new_value"] == owner_id


def test_34_unowned_work_is_shown_with_its_reason(case):
    cid = case()
    rows = ui({"mode": "unowned", "limit": 100})["rows"]
    assert cid in [r["case_id"] for r in rows], "unowned work was hidden"
    assert "source: " in HTML and "unowned" in HTML


# ── 5. priority ──────────────────────────────────────────────────────────────

def test_40_priority_change_records_history(case):
    cid = case(priority="low")
    assert ui({"mode": "priority", "caseId": cid, "priority": "urgent"})["ok"]
    rows = [h for h in cases.history(cid) if h["field"] == "priority"]
    assert rows[-1]["old_value"] == "low" and rows[-1]["new_value"] == "urgent"


def test_41_no_op_priority_creates_no_false_history(case):
    cid = case(priority="high")
    before = len(cases.history(cid))
    ui({"mode": "priority", "caseId": cid, "priority": "high"})
    assert len(cases.history(cid)) == before


# ── 6. comments stay case-local ──────────────────────────────────────────────

def test_50_comment_through_the_ui_path(case):
    cid = case()
    assert ui({"mode": "comment", "caseId": cid, "body": "note",
               "internal": True})["ok"]
    assert ui({"mode": "get", "caseId": cid})["rows"][0]["comments"][0][
        "comment"] == "note"


def test_51_the_page_labels_comments_case_local():
    assert "case-local" in HTML


# ── 7. historical honesty ────────────────────────────────────────────────────

def test_60_historical_rows_are_excluded_from_the_work_lists():
    for mode in ("queue", "list", "unowned"):
        rows = ui({"mode": mode, "limit": 100})["rows"]
        assert all(r["is_historical"] is False for r in rows)


def test_61_unknown_is_rendered_as_unknown_not_zero():
    assert "unknown — predates the case lifecycle" in HTML
    assert "unknown, not zero" in HTML


def test_62_no_history_was_manufactured_for_historical_rows():
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM record_field_history h
                           JOIN cases c ON c.case_id = h.entity_id
                           WHERE h.entity='case' AND c.is_historical""")
            assert cur.fetchone()[0] == 0
    finally:
        c.close()


# ── 8. SLA is displayed, never invented ──────────────────────────────────────

def test_70_sla_comes_from_the_linked_escalation():
    assert "from the linked escalation" in HTML
    assert "sla_due_at" in HTML


def test_71_the_page_never_claims_waiting_pauses_the_clock():
    """The invariant is about what a USER sees, so source comments — including
    the one stating this rule — are stripped before the check."""
    visible = re.sub(r"/\*.*?\*/", " ", HTML, flags=re.S)
    visible = re.sub(r"<!--.*?-->", " ", visible, flags=re.S)
    visible = re.sub(r"(?m)^\s*//.*$", " ", visible).lower()
    for phrase in ("pause", "paused", "clock stopped", "sla on hold"):
        assert phrase not in visible, f"the page implies SLA pausing: {phrase!r}"


# ── 9. security boundary and flags ───────────────────────────────────────────

def test_80_the_legacy_procedure_is_still_refused():
    with pytest.raises(write_guard.WritePermissionError):
        write_guard.guard_query("SELECT sp_cases(p_mode := 'close') AS result")


def test_81_disabled_case_system_refuses_ui_writes(case, monkeypatch):
    cid = case()
    monkeypatch.setattr(cases, "ENABLED", False)
    r = ui({"mode": "transition", "caseId": cid, "toStatus": "in_progress"})
    assert r["ok"] is False
    monkeypatch.undo()
    assert cases.get(cid)["status"] == "new"


def test_82_auto_open_and_kb_feedback_remain_off():
    assert cases.AUTO_OPEN is False and cases.KB_FEEDBACK is False


def test_83_the_page_surfaces_a_disabled_case_system():
    assert "CASES_ENABLED=0" in HTML
