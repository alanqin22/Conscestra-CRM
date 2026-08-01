"""C1 Step 9 — end-to-end lifecycle and the cross-boundary invariants.

Every other case suite is LAYER-LOCAL: it proves its own boundary in isolation.
234 of them pass without anything proving the layers compose, or that the five
conceptual distinctions C1 exists to protect still hold end to end.

    conversation resolved  !=  case resolved
    case created           !=  work accepted
    work accepted          !=  work completed
    resolved               !=  closed
    resolved case          !=  approved knowledge

Each has a named guard below, because a distinction with no test is a
distinction a future refactor collapses quietly.

The sharpest test here is test_31: a case assigned and then UNASSIGNED. Its
current owner_id is NULL while acceptance genuinely happened, so if analytics
ever read owner_id instead of the history chain, every existing test would
still pass and only this one would fail.
"""
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import (agent_console, case_analytics, cases,      # noqa: E402
                      escalation, knowledge)
from app.core.database import get_connection                     # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.record_field_history')")
            if cur.fetchone()[0] is None:
                pytest.skip("case migrations not applied")
        c.close()
    except Exception as exc:                                     # pragma: no cover
        pytest.skip(f"no database reachable: {exc}")


def _sql(q, args=(), fetch=True):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute(q, args)
            r = cur.fetchall() if fetch else None
        c.commit()
        return r
    finally:
        c.close()


def _purge_case(cid):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE record_field_history DISABLE TRIGGER "
                        "trg_rfh_append_only")
            cur.execute("DELETE FROM record_field_history WHERE entity='case' "
                        "AND entity_id=%s::uuid", (cid,))
            cur.execute("ALTER TABLE record_field_history ENABLE TRIGGER "
                        "trg_rfh_append_only")
            cur.execute("DELETE FROM case_comments WHERE case_id=%s::uuid", (cid,))
            cur.execute("DELETE FROM cases WHERE case_id=%s::uuid", (cid,))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def world():
    """A conversation + escalation + whatever cases they spawn, all removed."""
    convos, escs, kases = [], [], []

    class W:
        def conversation(self, subject="e2e thread"):
            cid = _sql("""INSERT INTO conversations
                            (scope, channel, status, subject, anon_key,
                             message_count, handling)
                          VALUES ('external','webchat','open',%s,%s,0,'ai')
                          RETURNING conversation_id::text""",
                       (subject, f"e2e:{uuid.uuid4()}"))[0][0]
            convos.append(cid)
            return cid

        def escalation(self, conversation_id=None, reason="customer_requested_human",
                       assigned_to=None):
            r = escalation.open(reason, "e2e-suite",
                                summary="Customer asked for a person",
                                transcript_excerpt="…the caller asked for a human…",
                                channel="webchat",
                                handle="e2e@example.invalid",
                                conversation_id=conversation_id)
            escs.append(r["escalation_id"])
            if assigned_to:
                escalation.assign(r["escalation_id"], assigned_to)
            return r["escalation_id"]

        def track(self, case_id):
            kases.append(case_id)
            return case_id

    yield W()
    for cid in kases:
        _purge_case(cid)
    for cid in convos:
        _sql("SELECT case_id::text FROM cases WHERE conversation_id=%s::uuid",
             (cid,))
        for row in _sql("SELECT case_id::text FROM cases "
                        "WHERE conversation_id=%s::uuid", (cid,)):
            _purge_case(row[0])
    for eid in escs:
        for row in _sql("SELECT case_id::text FROM cases "
                        "WHERE escalation_id=%s::uuid", (eid,)):
            _purge_case(row[0])
        _sql("DELETE FROM escalations WHERE escalation_id=%s::uuid", (eid,),
             fetch=False)
    for cid in convos:
        _sql("DELETE FROM conversation_messages WHERE conversation_id=%s::uuid",
             (cid,), fetch=False)
        _sql("DELETE FROM conversations WHERE conversation_id=%s::uuid", (cid,),
             fetch=False)


@pytest.fixture
def owners():
    rows = _sql("""SELECT owner_id::text, email FROM owners
                   WHERE email IS NOT NULL AND coalesce(is_active,true) LIMIT 2""")
    if len(rows) < 2:
        pytest.skip("need two owners")
    return rows


# ════════════════════════════════════════════════════════════════════════════
# A. the whole chain, in one walk
# ════════════════════════════════════════════════════════════════════════════

def test_01_escalation_to_knowledge_signal_end_to_end(world, owners):
    """obligation -> case -> accepted -> worked -> waiting -> resolved ->
    closed -> measured -> knowledge evidence. Nothing else proves the layers
    compose."""
    (owner_a, _), _ = owners
    convo = world.conversation("VPN keeps dropping")
    esc = world.escalation(conversation_id=convo)

    # obligation exists and is live
    assert _sql("SELECT status FROM escalations WHERE escalation_id=%s::uuid",
                (esc,))[0][0] in ("open", "assigned")

    # -> durable work record
    b = cases.open_from_escalation(esc, actor="e2e")
    cid = world.track(b["case_id"])
    assert b["created"] is True
    assert cases.get(cid)["escalation_id"] == esc

    # -> work accepted
    cases.assign(cid, owner_a, actor="e2e", source="e2e")
    # -> worked, blocked, worked again, completed
    cases.transition(cid, "in_progress", actor="e2e", source="e2e")
    cases.transition(cid, "waiting", actor="e2e", source="e2e")
    cases.transition(cid, "in_progress", actor="e2e", source="e2e")
    cases.comment(cid, "Re-paired on the 5GHz band; applies to any client "
                       "showing the same symptom.", internal=False)
    cases.transition(cid, "resolved", actor="e2e", source="e2e")

    c = cases.get(cid)
    assert c["status"] == "resolved" and c["resolved_at"] and not c["closed_at"]

    # -> measured
    m = case_analytics.metrics(1)
    assert m["ok"] and m["volume"]["resolved"] >= 1
    assert m["acceptance"]["accepted"] >= 1

    # -> knowledge EVIDENCE (not truth): eligible to be offered, nothing published
    assert cid in {r["case_id"] for r in knowledge._resolved_cases(50)}
    assert _sql("SELECT count(*) FROM knowledge_articles WHERE source_ref=%s",
                (cid,))[0][0] == 0

    # -> administrative closure, distinct from resolution
    cases.transition(cid, "closed", actor="e2e", source="e2e")
    c = cases.get(cid)
    assert c["closed_at"] and c["resolved_at"] and c["next_states"] == []

    # the full provable chain
    fields = [h["field"] for h in cases.history(cid)]
    assert fields.count("status") == 6      # open + 5 transitions
    assert "owner_id" in fields


# ════════════════════════════════════════════════════════════════════════════
# B. the alternative paths
# ════════════════════════════════════════════════════════════════════════════

def test_10_escalation_without_a_case_stays_visible(world):
    """The obligation must not vanish just because nobody recorded the work."""
    convo = world.conversation()
    esc = world.escalation(conversation_id=convo)
    agent_console.takeover(convo, "someone-unknown")
    status = _sql("SELECT status FROM escalations WHERE escalation_id=%s::uuid",
                  (esc,))[0][0]
    assert status == "assigned", "takeover discharged an obligation nothing carries"
    assert case_analytics.metrics(1)["obligations"]["without_a_case"] >= 1


def test_11_unowned_case_is_visible_then_assignable(world, owners):
    (owner_a, _), _ = owners
    esc = world.escalation(assigned_to="agent")     # unresolvable on purpose
    b = cases.open_from_escalation(esc, actor="e2e")
    cid = world.track(b["case_id"])

    assert b["owner_resolved"] is False
    assert cases.get(cid)["owner_id"] is None
    assert "not a known CRM owner" in b["unowned_reason"]
    assert b["source_assignee"] == "agent"
    assert case_analytics.metrics(1)["volume"]["unowned"] >= 1

    cases.assign(cid, owner_a, actor="e2e", source="e2e")
    chain = [(h["old_value"], h["new_value"]) for h in cases.history(cid)
             if h["field"] == "owner_id"]
    assert chain == [(None, owner_a)]


def test_12_resolved_reopened_reworked_resolved_again(world, owners):
    (owner_a, _), _ = owners
    cid = world.track(cases.open_case("recurring fault", actor="e2e",
                                      source="e2e")["case_id"])
    cases.assign(cid, owner_a, actor="e2e")
    cases.transition(cid, "in_progress", actor="e2e")
    cases.transition(cid, "resolved", actor="e2e")
    first_resolved = cases.get(cid)["resolved_at"]

    cases.reopen(cid, actor="e2e", source="e2e")
    assert cases.get(cid)["status"] == "in_progress"
    assert cases.get(cid)["reopen_count"] == 1

    cases.transition(cid, "resolved", actor="e2e")
    c = cases.get(cid)
    assert c["reopen_count"] == 1
    assert c["resolved_at"] >= first_resolved, "the second resolution was lost"
    assert case_analytics.metrics(1)["volume"]["reopened"] >= 1


def test_13_historical_case_contract_holds_as_one_thing():
    """Countable, NOT time-measurable, NOT mineable — asserted together, since
    each half currently lives in a different suite."""
    m = case_analytics.metrics(3650)
    assert m["historical"]["count"] == 120                    # countable

    hist = {r[0] for r in _sql("SELECT case_id::text FROM cases "
                               "WHERE is_historical")}
    assert _sql("""SELECT count(*) FROM cases WHERE is_historical
                   AND (first_response_at IS NOT NULL
                     OR resolved_at IS NOT NULL)""")[0][0] == 0
    assert _sql("""SELECT count(*) FROM record_field_history h
                   JOIN cases c ON c.case_id=h.entity_id
                   WHERE h.entity='case' AND c.is_historical""")[0][0] == 0
    assert not (hist & {r["case_id"] for r in knowledge._resolved_cases(200)})


# ════════════════════════════════════════════════════════════════════════════
# C. the five cross-boundary invariants
# ════════════════════════════════════════════════════════════════════════════

def test_20_conversation_resolved_is_not_case_resolved(world):
    """A chat can end in four minutes while the work runs for three days."""
    convo = world.conversation()
    cid = world.track(cases.open_for_conversation(convo, actor="e2e")["case_id"])
    _sql("UPDATE conversations SET status='closed' WHERE conversation_id=%s::uuid",
         (convo,), fetch=False)

    assert cases.get(cid)["status"] == "new"
    assert cases.get(cid)["resolved_at"] is None
    m = case_analytics.metrics(1)
    assert m["volume"]["open"] >= 1, "a closed conversation closed its case"


def test_21_case_created_is_not_work_accepted(world):
    cid = world.track(cases.open_case("created not accepted", actor="e2e",
                                      source="e2e")["case_id"])
    m = case_analytics.metrics(1)
    assert m["volume"]["created"] >= 1
    assert m["acceptance"]["never_accepted"] >= 1
    assert cases.get(cid)["owner_id"] is None


def test_22_work_accepted_is_not_work_completed(world, owners):
    (owner_a, _), _ = owners
    cid = world.track(cases.open_case("accepted not completed", actor="e2e",
                                      source="e2e")["case_id"])
    cases.assign(cid, owner_a, actor="e2e")
    assert cases.get(cid)["resolved_at"] is None
    assert case_analytics.metrics(1)["acceptance"]["accepted"] >= 1


def test_23_resolved_is_not_closed(world):
    """Resolution is evidence the work was done; closure is administrative."""
    cid = world.track(cases.open_case("resolved not closed", actor="e2e",
                                      source="e2e")["case_id"])
    cases.transition(cid, "in_progress", actor="e2e")
    cases.transition(cid, "resolved", actor="e2e")
    c = cases.get(cid)
    assert c["resolved_at"] is not None and c["closed_at"] is None
    assert "closed" in c["next_states"], "resolution must not auto-close"


def test_24_resolved_case_is_not_approved_knowledge(world):
    """The whole premise of Step 8."""
    cid = world.track(cases.open_case("printer wifi pairing fails", actor="e2e",
                                      source="e2e",
                                      description="Laptop cannot see the printer")
                      ["case_id"])
    cases.comment(cid, "Reset the spooler and re-paired on 5GHz. General fix "
                       "for this symptom on any client.", internal=False)
    cases.transition(cid, "in_progress", actor="e2e")
    cases.transition(cid, "resolved", actor="e2e")

    assert cid in {r["case_id"] for r in knowledge._resolved_cases(50)}
    assert _sql("SELECT count(*) FROM knowledge_articles WHERE source_ref=%s",
                (cid,))[0][0] == 0
    assert _sql("""SELECT count(*) FROM action_approvals
                   WHERE action_type='kb.publish' AND params->>'source_ref'=%s""",
                (cid,))[0][0] == 0


# ════════════════════════════════════════════════════════════════════════════
# D. the discriminating tests
# ════════════════════════════════════════════════════════════════════════════

def test_31_acceptance_is_read_from_history_not_from_owner_id(world, owners):
    """THE test that separates a real acceptance metric from a lookalike.

    After unassigning, cases.owner_id is NULL — yet the organisation DID accept
    this work. If analytics ever read owner_id instead of the NULL -> value
    history event, every other test in the suite would still pass."""
    (owner_a, _), _ = owners
    cid = world.track(cases.open_case("accepted then handed back", actor="e2e",
                                      source="e2e")["case_id"])
    before = case_analytics.metrics(1)["acceptance"]["accepted"]
    cases.assign(cid, owner_a, actor="e2e")
    cases.unassign(cid, actor="e2e")

    assert cases.get(cid)["owner_id"] is None
    assert case_analytics.metrics(1)["acceptance"]["accepted"] == before + 1, (
        "acceptance was read from the current owner instead of the history")


def test_32_an_invalid_transition_writes_no_history(world):
    """Refusals must leave nothing behind — not the status, not a phantom row."""
    cid = world.track(cases.open_case("refusal leaves nothing", actor="e2e",
                                      source="e2e")["case_id"])
    before = len(cases.history(cid))
    for bad in ("closed", "waiting", "resolved"):
        with pytest.raises(cases.InvalidTransition):
            cases.transition(cid, bad, actor="e2e")
    assert cases.get(cid)["status"] == "new"
    assert len(cases.history(cid)) == before


def test_33_waiting_does_not_alter_timestamps_or_the_sla(world):
    """`waiting` means BLOCKED. Step 6 must not have quietly given it clock
    semantics."""
    cid = world.track(cases.open_case("blocked on the customer", actor="e2e",
                                      source="e2e")["case_id"])
    cases.transition(cid, "in_progress", actor="e2e")
    before = cases.get(cid)
    cases.transition(cid, "waiting", actor="e2e")
    after = cases.get(cid)
    assert after["resolved_at"] is None and after["closed_at"] is None
    assert after["first_response_at"] == before["first_response_at"]
    assert after["reopen_count"] == before["reopen_count"]


def test_34_the_obligation_and_the_work_are_measured_from_different_tables():
    """A conversation timestamp may never substitute for a case timestamp."""
    m = case_analytics.semantics()["moments"]
    assert m["obligation"].startswith("escalations.")
    assert m["work_record"].startswith("cases.")
    assert m["work_completed"].startswith("cases.")
    assert "record_field_history" in m["work_accepted"]
    for v in m.values():
        assert "conversation" not in v.lower()
