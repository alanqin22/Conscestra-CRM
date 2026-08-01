"""C1 Step 7 — case-lifecycle analytics, and the direct-mode contract.

The failure this step exists to prevent is a reader taking a CONVERSATION
metric as an answer to a WORK question:

    conversation resolved != case resolved
    case created         != work accepted
    work accepted        != work completed

`agent_ops` measures the conversation and is left untouched; the case figures
live in a separate module with its own vocabulary.
"""
import inspect
import pathlib
import re

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.agents.cases import pre_router, sql_builder            # noqa: E402
from app.core import agent_ops, case_analytics, cases           # noqa: E402
from app.core.database import get_connection                    # noqa: E402


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
            cur.execute("DELETE FROM cases WHERE case_id=%s::uuid", (case_id,))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def case():
    made = []

    def _make(**kw):
        cid = cases.open_case(kw.pop("subject", "analytics case"),
                              actor="test", source="test", **kw)["case_id"]
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
        pytest.skip("need an owner")
    return row


# ── 1. the DIRECT_MODES contract ─────────────────────────────────────────────

def test_01_write_modes_are_an_explicit_finite_set():
    assert pre_router.WRITE_MODES == ("transition", "assign", "priority",
                                      "comment")
    assert set(pre_router.DIRECT_MODES) == set(pre_router.READ_MODES) | \
        set(pre_router.WRITE_MODES)


@pytest.mark.parametrize("hatch", ["write", "mutate", "raw_sql",
                                   "execute_action", "sql", "exec", "unknown"])
def test_02_no_generic_escape_hatch_exists(hatch):
    assert not pre_router.is_known_mode(hatch)
    assert pre_router.route({"mode": hatch, "caseId": "x"}) is None


def test_03_unknown_mode_fails_closed_at_the_endpoint(case):
    """Without this a body carrying mode='write' silently drops to the natural
    language path, so the caller believes an explicit operation ran."""
    from app.agents.cases.router import case_chat, CaseChatRequest
    r = case_chat(CaseChatRequest(chatInput={"mode": "write",
                                             "caseId": case(),
                                             "message": "close it"}))
    assert r["data"]["ok"] is False and r["data"]["refused"] is True


@pytest.mark.parametrize("msg", ["resolve this case", "close case 123",
                                 "assign this to David", "reopen it"])
def test_04_a_keyword_can_never_trigger_a_write(msg):
    hit = pre_router.route({"message": msg})
    assert hit is None or hit[0] in sql_builder.READ_ACTIONS


def test_05_every_mode_declares_its_input_fields():
    assert set(pre_router.MODE_FIELDS) == set(pre_router.DIRECT_MODES)


def test_06_unexpected_fields_never_reach_the_domain_layer(case):
    """Params are built field by field; the request body is never forwarded
    wholesale."""
    cid = case()
    action, params = pre_router.route({
        "mode": "transition", "caseId": cid, "toStatus": "in_progress",
        "owner_id": "00000000-0000-0000-0000-000000000000",
        "is_historical": True, "resolved_at": "1999-01-01", "evil": "x"})
    assert set(params) <= {"case_id", "to_status", "actor"}


def test_07_every_write_mode_reaches_the_case_layer_and_nothing_else():
    src = inspect.getsource(sql_builder)
    body = src.split("# ── writes", 1)[1]
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "sp_cases"):
        assert verb not in body, f"a write mode composes {verb!r} directly"
    assert body.count("cases.") >= len(sql_builder.WRITE_ACTIONS)


def test_08_direct_modes_do_not_weaken_authorization():
    main = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    assert re.search(r"app\.include_router\(cases_router,\s*dependencies=_DATA\)",
                     main)


def test_09_flags_still_gate_direct_mode_writes(case, monkeypatch):
    cid = case()
    monkeypatch.setattr(cases, "ENABLED", False)
    action, params = pre_router.route({"mode": "transition", "caseId": cid,
                                       "toStatus": "in_progress"})
    assert sql_builder.execute(action, params)["ok"] is False
    monkeypatch.undo()
    assert cases.get(cid)["status"] == "new"


def test_10_auto_open_and_kb_feedback_remain_unrelated_and_off():
    assert cases.AUTO_OPEN is False and cases.KB_FEEDBACK is False


# ── 2. the metric semantics ──────────────────────────────────────────────────

def test_20_conversation_metrics_are_unchanged():
    """agent_ops keeps its shipped definitions — changing a metric's meaning in
    place invalidates every trend built on it."""
    m = agent_ops.metrics(30)
    for key in ("containment_rate", "escalation_rate", "volume",
                "resolution", "csat_proxy_pct"):
        assert key in m


def test_21_agent_ops_declares_its_basis():
    src = pathlib.Path("app/core/agent_ops.py").read_text(encoding="utf-8")
    assert "CONVERSATION LIFECYCLE, not the" in src
    assert "avg_hours` below is CONVERSATION DURATION" in src


def test_22_case_metrics_are_a_separate_module_and_vocabulary():
    m = case_analytics.metrics(30)
    assert m["basis"] == "case lifecycle"
    assert "containment_rate" not in m, "a conversation metric leaked in"


def test_23_the_three_distinctions_are_published():
    s = case_analytics.semantics()
    pairs = {(d["not"], d["is"]) for d in s["distinctions"]}
    assert ("conversation resolved", "case resolved") in pairs
    assert ("case created", "work accepted") in pairs
    assert ("work accepted", "work completed") in pairs


def test_24_the_four_moments_map_to_real_sources():
    m = case_analytics.semantics()["moments"]
    assert m["obligation"].startswith("escalations")
    assert "record_field_history" in m["work_accepted"]
    assert m["work_record"].startswith("cases.created_at")
    assert m["work_completed"].startswith("cases.resolved_at")


# ── 3. acceptance is measured from history, not from owner_id ────────────────

def test_30_work_accepted_is_the_first_assignment(case, an_owner):
    owner_id, _ = an_owner
    before = case_analytics.metrics(1)["acceptance"]["accepted"]
    cid = case()
    after_create = case_analytics.metrics(1)["acceptance"]["accepted"]
    assert after_create == before, "an unowned case must not count as accepted"
    cases.assign(cid, owner_id, actor="test", source="test")
    assert case_analytics.metrics(1)["acceptance"]["accepted"] == before + 1


def test_31_created_is_not_accepted(case):
    cid = case()
    m = case_analytics.metrics(1)
    assert m["volume"]["created"] >= 1
    assert m["acceptance"]["never_accepted"] >= 1


def test_32_accepted_is_not_completed(case, an_owner):
    owner_id, _ = an_owner
    cid = case()
    cases.assign(cid, owner_id, actor="test", source="test")
    m = case_analytics.metrics(1)
    assert m["acceptance"]["accepted"] >= 1
    # Assigning must not resolve anything. (Was `== 0 or True`, which could
    # never fail — found during the Step 9 coverage audit.)
    assert cases.get(cid)["resolved_at"] is None
    assert cases.get(cid)["status"] == "new"


# ── 4. historical honesty ────────────────────────────────────────────────────

def test_40_historical_cases_are_counted_not_averaged():
    m = case_analytics.metrics(3650)
    assert m["historical"]["count"] == 120
    assert "UNKNOWN, not zero" in m["historical"]["note"]


def test_41_historical_rows_are_excluded_from_every_duration():
    """Every AVG over cases must exclude them. The deliberate historical COUNT
    is the one query that must NOT — it exists to report them."""
    src = inspect.getsource(case_analytics)
    blocks = re.findall(r'"""(.*?)"""', src, re.S)
    averaging = [b for b in blocks if "AVG(EXTRACT" in b]
    assert averaging, "no duration queries found — did they move?"
    for b in averaging:
        assert "is_historical = false" in b, b[:200]
    assert "SELECT COUNT(*) FROM cases WHERE is_historical" in src


def test_42_averages_publish_their_sample_size():
    """A mean over three rows must not read like a stable one."""
    d = case_analytics.metrics(30)["durations"]
    assert "first_response_measured_n" in d and "resolution_measured_n" in d


def test_43_unknown_first_response_is_reported_not_hidden():
    assert "first_response_unknown_n" in case_analytics.metrics(30)["durations"]


# ── 5. obligations stay distinct from work ───────────────────────────────────

def test_50_obligations_are_measured_on_escalations():
    o = case_analytics.metrics(30).get("obligations")
    assert o is not None and set(o) == {"live", "sla_breached", "without_a_case"}


def test_51_obligations_without_a_case_are_surfaced():
    """The number Step 4b exists to make visible: a promise nothing records."""
    assert "without_a_case" in case_analytics.metrics(30)["obligations"]


def test_52_no_sla_calculation_was_invented():
    """Checked against the QUERY CODE, not the prose. The module documents that
    it adds no entitlement or pause logic, and a naive substring search would
    flag the very sentence promising that."""
    src = inspect.getsource(case_analytics.metrics).lower()
    for banned in ("pause", "entitlement", "business_hours", "interval '"):
        assert banned not in src, f"metrics() contains SLA logic: {banned!r}"


def test_53_never_raises_on_a_reporting_failure(monkeypatch):
    monkeypatch.setattr(case_analytics, "get_connection",
                        lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    m = case_analytics.metrics(30)
    assert m.get("ok") is not True and "error" in m
