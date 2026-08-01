"""C1 Step 8 — the knowledge bridge.

The distinction the whole step turns on:

    resolved case    = evidence that WORK WAS COMPLETED
    approved article = information TRUSTED FOR FUTURE ANSWERS

They are not the same claim, so nothing here publishes. A resolved case becomes
a CANDIDATE on exactly the path email threads and call transcripts already
take — LLM draft -> governance.propose('kb.publish') -> a human — and the case
source is a third input to that one pipeline, never a second knowledge system.
"""
import inspect

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import case_analytics, cases, knowledge           # noqa: E402
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
            cur.execute("DELETE FROM case_comments WHERE case_id=%s::uuid",
                        (case_id,))
            cur.execute("DELETE FROM cases WHERE case_id=%s::uuid", (case_id,))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def resolved_case():
    made = []

    def _make(subject="Printer will not connect over wifi",
              description="Customer could not reach the printer from a laptop.",
              comment="Reset the print spooler and re-paired on the 5GHz band. "
                      "Works for any printer showing the same symptom.",
              resolve=True):
        cid = cases.open_case(subject, actor="test", source="test",
                              description=description)["case_id"]
        made.append(cid)
        if comment:
            cases.comment(cid, comment, internal=False)
        if resolve:
            cases.transition(cid, "in_progress", actor="test")
            cases.transition(cid, "resolved", actor="test")
        return cid

    yield _make
    for cid in made:
        _purge(cid)


def _mineable_ids(cap=50):
    return {r["case_id"] for r in knowledge._resolved_cases(cap)}


# ── 1. no second knowledge system ────────────────────────────────────────────

def test_01_the_case_source_feeds_the_existing_pipeline():
    """One miner, three sources — not a parallel loop with its own approval,
    dedupe and privacy handling to drift out of agreement."""
    src = inspect.getsource(knowledge.draft_pass)
    assert "_resolved_threads" in src and "_resolved_calls" in src
    assert "_resolved_cases" in src
    assert src.count("_propose(") >= 3


def test_02_the_bridge_publishes_nothing():
    src = inspect.getsource(knowledge._resolved_cases)
    for verb in ("INSERT INTO knowledge_articles", "publish(", "UPDATE knowledge"):
        assert verb not in src


def test_03_candidates_go_through_governance():
    src = inspect.getsource(knowledge.draft_pass)
    assert 'governance.propose("kb.publish"' in src


def test_04_no_second_candidate_table_was_invented():
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM information_schema.tables
                           WHERE table_schema='public'
                             AND (table_name LIKE '%knowledge_candidate%'
                               OR table_name LIKE '%kb_candidate%')""")
            assert cur.fetchone()[0] == 0
    finally:
        c.close()


# ── 2. evidence is filtered before it becomes a candidate ────────────────────

def test_10_a_resolved_case_with_substance_is_mineable(resolved_case):
    assert resolved_case() in _mineable_ids()


def test_11_an_unresolved_case_is_not(resolved_case):
    """Closure is not completion; an open case has no solution to teach."""
    assert resolved_case(resolve=False) not in _mineable_ids()


def test_12_a_thin_resolution_is_not(resolved_case):
    """'done' teaches nothing."""
    assert resolved_case(description="", comment="done") not in _mineable_ids()


def test_13_historical_rows_are_never_mined():
    """They have no resolution context, and inventing one is exactly what the
    historical flag exists to prevent."""
    ids = _mineable_ids()
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT case_id::text FROM cases WHERE is_historical")
            hist = {r[0] for r in cur.fetchall()}
    finally:
        c.close()
    assert not (ids & hist)


def test_14_a_case_is_offered_once(resolved_case):
    """Same NOT EXISTS pair the other two sources use."""
    cid = resolved_case()
    assert cid in _mineable_ids()
    src = inspect.getsource(knowledge._resolved_cases)
    assert "NOT EXISTS" in src and "knowledge_articles" in src
    assert "action_approvals" in src and "'pending','approved','executed'" in src


def test_15_reuses_the_existing_classifier_not_a_new_vocabulary():
    """The LLM returning None IS the classifier; a second vocabulary would have
    to be kept in agreement with the first."""
    src = inspect.getsource(knowledge.draft_pass)
    assert "_draft_llm(" in src
    for invented in ("reusable_solution", "one_time_action", "candidate_type"):
        assert invented not in inspect.getsource(knowledge)


# ── 3. privacy and reach ─────────────────────────────────────────────────────

def test_20_case_text_is_masked_before_it_reaches_the_model():
    assert "privacy.mask" in inspect.getsource(knowledge._draft_llm)


def test_21_case_candidates_default_to_the_internal_tier():
    """U2's reach_invariant means an external agent reads only the `public`
    tier, so a fresh customer-derived candidate cannot reach a customer-facing
    agent until a human deliberately re-tiers it."""
    src = inspect.getsource(knowledge.draft_pass)
    assert 'setdefault("audience", "internal")' in src


def test_22_provenance_is_the_case_id():
    src = inspect.getsource(knowledge.draft_pass)
    assert '_propose(art, cs["case_id"])' in src


# ── 4. the flag ──────────────────────────────────────────────────────────────

def test_30_kb_feedback_is_off_by_default():
    assert cases.KB_FEEDBACK is False


def test_31_the_case_source_is_inert_while_the_flag_is_off(resolved_case):
    resolved_case()
    r = knowledge.draft_pass(force=True)
    assert r["cases"] == 0, "cases were mined with CASES_KB_FEEDBACK=0"


def test_32_the_flag_gates_only_the_case_source():
    src = inspect.getsource(knowledge.draft_pass)
    assert "_cases.ENABLED and _cases.KB_FEEDBACK" in src


# ── 5. signals are evidence, never conclusions ───────────────────────────────

def test_40_signals_write_nothing():
    src = inspect.getsource(case_analytics.knowledge_signals)
    for verb in ("INSERT", "UPDATE", "DELETE", "propose", "publish"):
        assert verb not in src.upper().replace("PUBLISHES ANYTHING", "")


def test_41_repeated_subjects_are_surfaced(resolved_case):
    resolved_case(subject="VPN drops every ten minutes")
    resolved_case(subject="VPN drops every ten minutes")
    s = case_analytics.knowledge_signals(1)
    subs = {r["subject"] for r in s["repeated_subjects"]}
    assert "vpn drops every ten minutes" in subs


def test_42_signals_say_what_they_are():
    s = case_analytics.knowledge_signals(30)
    assert s["basis"] == "evidence, not truth"
    assert "not that an answer is correct" in s["note"]


def test_43_signals_never_raise(monkeypatch):
    monkeypatch.setattr(case_analytics, "get_connection",
                        lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert "error" in case_analytics.knowledge_signals(30)


# ── 6. metric semantics stay separate ────────────────────────────────────────

def test_50_conversation_and_case_metrics_remain_distinct_modules():
    from app.core import agent_ops
    assert "containment_rate" in agent_ops.metrics(30)
    assert "containment_rate" not in case_analytics.metrics(30)


def test_51_no_ambiguous_merged_lifecycle_metric():
    m = case_analytics.metrics(30)
    for ambiguous in ("lifecycle_time", "total_resolution", "avg_hours"):
        assert ambiguous not in m
