"""C1 Step 4 — the console bridge.

The boundary this step defends:

    KB-contained conversation -> no escalation -> NO CASE

Taking over a conversation does NOT create a case. The console cannot tell a
rep who answered one quick question from a rep who accepted work that outlives
the interaction, and guessing either way is worse than asking: guess "durable"
and every assisted chat becomes a case, destroying containment; guess
"temporary" and real work keeps evaporating into human memory. So creating a
case is an explicit, separate act.
"""
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                      # noqa: E402

register_uuid()

from app.core import agent_console, cases, escalation          # noqa: E402
from app.core.database import get_connection                   # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_name='cases'
                             AND column_name='source_assignee'""")
            if not cur.fetchone():
                pytest.skip("case migrations not applied")
        c.close()
    except Exception as exc:                                   # pragma: no cover
        pytest.skip(f"no database reachable: {exc}")


def _purge_case(case_id):
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
def convo():
    """A real open conversation, cleaned up with anything it spawned."""
    made = []

    def _make(subject="console step4 thread", channel="webchat"):
        c = get_connection()
        try:
            with c.cursor() as cur:
                cur.execute(
                    """INSERT INTO conversations
                         (scope, channel, status, subject, anon_key,
                          message_count, handling)
                       VALUES ('external', %s, 'open', %s, %s, 0, 'ai')
                       RETURNING conversation_id::text""",
                    (channel, subject, f"test:{uuid.uuid4()}"))
                cid = cur.fetchone()[0]
            c.commit()
        finally:
            c.close()
        made.append(cid)
        return cid

    yield _make
    for cid in made:
        c = get_connection()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT case_id::text FROM cases "
                            "WHERE conversation_id=%s::uuid", (cid,))
                ids = [r[0] for r in cur.fetchall()]
            for i in ids:
                _purge_case(i)
            with c.cursor() as cur:
                cur.execute("DELETE FROM escalations WHERE conversation_id=%s::uuid",
                            (cid,))
                cur.execute("DELETE FROM conversation_messages "
                            "WHERE conversation_id=%s::uuid", (cid,))
                cur.execute("DELETE FROM conversations WHERE conversation_id=%s::uuid",
                            (cid,))
            c.commit()
        finally:
            c.close()


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


def _case_count(conversation_id):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM cases WHERE conversation_id=%s::uuid",
                        (conversation_id,))
            return cur.fetchone()[0]
    finally:
        c.close()


# ── 1. containment ───────────────────────────────────────────────────────────

def test_01_a_conversation_alone_creates_no_case(convo):
    cid = convo()
    assert _case_count(cid) == 0
    assert cases.case_for_conversation(cid) is None


def test_02_takeover_does_not_create_a_case(convo):
    """The heart of Step 4: a rep assisting is not, by itself, durable work."""
    cid = convo()
    res = agent_console.takeover(cid, "alan")
    assert res["ok"] is True
    assert _case_count(cid) == 0, (
        "takeover created a case — every assisted chat would become one and "
        "containment would stop meaning anything")


def test_03_takeover_reports_an_existing_case_read_only(convo, an_owner):
    cid = convo()
    made = cases.open_for_conversation(cid, actor=an_owner[1])
    res = agent_console.takeover(cid, "alan")
    assert res["case"]["case_id"] == made["case_id"]
    assert _case_count(cid) == 1


def test_04_takeover_reports_none_when_there_is_no_case(convo):
    assert agent_console.takeover(convo(), "alan")["case"] is None


def test_05_send_reply_does_not_create_a_case(convo):
    cid = convo()
    agent_console.send_reply(cid, "alan", "just answering a quick question")
    assert _case_count(cid) == 0


# ── 2. explicit creation ─────────────────────────────────────────────────────

def test_10_explicit_action_creates_exactly_one_case(convo):
    cid = convo()
    r = cases.open_for_conversation(cid, actor="alan")
    assert r["ok"] and r["created"] and _case_count(cid) == 1
    assert cases.get(r["case_id"])["status"] == "new"


def test_11_the_case_links_back_to_the_conversation(convo):
    cid = convo()
    r = cases.open_for_conversation(cid, actor="alan")
    assert cases.get(r["case_id"])["conversation_id"] == cid


def test_12_unknown_conversation_is_refused():
    r = cases.open_for_conversation(str(uuid.uuid4()), actor="alan")
    assert r["ok"] is False and "no such conversation" in r["error"]


# ── 3. idempotency ───────────────────────────────────────────────────────────

def test_20_repeat_clicks_return_the_same_case(convo):
    cid = convo()
    a = cases.open_for_conversation(cid, actor="alan")
    b = cases.open_for_conversation(cid, actor="alan")
    assert b["case_id"] == a["case_id"]
    assert a["created"] is True and b["created"] is False
    assert _case_count(cid) == 1


def test_21_repeated_takeover_creates_nothing(convo):
    cid = convo()
    for _ in range(3):
        agent_console.takeover(cid, "alan")
        agent_console.release(cid, "alan")
    assert _case_count(cid) == 0


def test_22_the_invariant_is_database_enforced(convo):
    cid = convo()
    cases.open_for_conversation(cid, actor="alan")
    c = get_connection()
    try:
        with c.cursor() as cur, pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("""INSERT INTO cases (subject, status, conversation_id)
                           VALUES ('duplicate', 'new', %s::uuid)""", (cid,))
        c.rollback()
    finally:
        c.close()
    assert _case_count(cid) == 1


def test_23_escalation_created_case_is_attached_not_duplicated(convo):
    """Both operational paths converge on one row."""
    cid = convo()
    e = escalation.open("customer_requested_human", "test-suite",
                        summary="asked for a person", channel="webchat",
                        handle="step4@example.invalid", conversation_id=cid)
    bridged = cases.open_from_escalation(e["escalation_id"], actor="test")
    r = cases.open_for_conversation(cid, actor="alan")
    assert r["created"] is False
    assert r["case_id"] == bridged["case_id"]
    assert _case_count(cid) == 1


def test_24_console_case_carries_the_originating_escalation(convo):
    cid = convo()
    e = escalation.open("complaint", "test-suite", summary="unhappy",
                        channel="webchat", handle="step4b@example.invalid",
                        conversation_id=cid)
    r = cases.open_for_conversation(cid, actor="alan")
    assert r["escalation_id"] == e["escalation_id"]


# ── 4. an existing case is never reset ───────────────────────────────────────

def test_30_existing_case_keeps_its_status(convo):
    cid = convo()
    r = cases.open_for_conversation(cid, actor="alan")
    cases.transition(r["case_id"], "in_progress", actor="test")
    again = cases.open_for_conversation(cid, actor="alan")
    assert again["created"] is False
    assert cases.get(r["case_id"])["status"] == "in_progress"


def test_31_existing_case_keeps_its_owner(convo, an_owner):
    owner_id, email = an_owner
    cid = convo()
    r = cases.open_for_conversation(cid, actor=email)
    cases.open_for_conversation(cid, actor="somebody-else")
    assert cases.get(r["case_id"])["owner_id"] == owner_id


def test_32_first_response_is_not_refabricated(convo):
    cid = convo()
    r = cases.open_for_conversation(cid, actor="alan")
    agent_console.send_reply(cid, "alan", "first human reply")
    first = cases.get(r["case_id"])["first_response_at"]
    assert first is not None
    agent_console.send_reply(cid, "alan", "second human reply")
    assert cases.get(r["case_id"])["first_response_at"] == first


def test_33_reply_threads_into_the_case(convo):
    cid = convo()
    r = cases.open_for_conversation(cid, actor="alan")
    agent_console.send_reply(cid, "alan", "here is your answer")
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT comment, is_internal FROM case_comments "
                        "WHERE case_id=%s::uuid", (r["case_id"],))
            rows = cur.fetchall()
    finally:
        c.close()
    assert ("here is your answer", False) in rows


def test_34_closed_case_frees_the_conversation_for_new_work(convo):
    cid = convo()
    r = cases.open_for_conversation(cid, actor="alan")
    for s in ("in_progress", "resolved", "closed"):
        cases.transition(r["case_id"], s, actor="test")
    again = cases.open_for_conversation(cid, actor="alan")
    assert again["created"] is True and again["case_id"] != r["case_id"]


# ── 5. ownership ─────────────────────────────────────────────────────────────

def test_40_resolvable_console_identity_becomes_the_owner(convo, an_owner):
    owner_id, email = an_owner
    r = cases.open_for_conversation(convo(), actor=email)
    assert r["owner_resolved"] is True
    assert cases.get(r["case_id"])["owner_id"] == owner_id


@pytest.mark.parametrize("who", ["agent", "alan", "nobody@example.invalid"])
def test_41_unresolvable_identity_leaves_the_case_unowned(convo, who):
    r = cases.open_for_conversation(convo(), actor=who)
    assert r["created"] is True and r["owner_resolved"] is False
    assert cases.get(r["case_id"])["owner_id"] is None


def test_42_console_identity_stays_traceable(convo):
    """console_takeover defaults the agent to the literal string 'agent'."""
    r = cases.open_for_conversation(convo(), actor="agent")
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT owner_id, source_assignee FROM cases "
                        "WHERE case_id=%s::uuid", (r["case_id"],))
            owner, src = cur.fetchone()
    finally:
        c.close()
    assert owner is None and src == "agent"
    assert "not a known CRM owner" in r["unowned_reason"]


def test_43_reassignment_uses_the_field_history_writer(convo, an_owner):
    owner_id, _ = an_owner
    r = cases.open_for_conversation(convo(), actor="agent")   # unowned
    cases.assign(r["case_id"], owner_id, actor="alan", source="console")
    rows = [h for h in cases.history(r["case_id"]) if h["field"] == "owner_id"]
    assert len(rows) == 1
    assert rows[0]["old_value"] is None and rows[0]["new_value"] == owner_id


# ── 6. feature flags ─────────────────────────────────────────────────────────

def test_50_disabled_blocks_console_case_writes(convo, monkeypatch):
    cid = convo()
    monkeypatch.setattr(cases, "ENABLED", False)
    r = cases.open_for_conversation(cid, actor="alan")
    assert r["ok"] is False and "disabled" in r["skipped"]
    assert _case_count(cid) == 0


def test_51_auto_open_off_does_not_block_explicit_creation(convo, monkeypatch):
    """AUTO_OPEN governs AUTOMATIC escalation bridging. An explicit human
    action is a different trigger and must not be suppressed by it."""
    monkeypatch.setattr(cases, "AUTO_OPEN", False)
    r = cases.open_for_conversation(convo(), actor="alan")
    assert r["created"] is True


def test_52_auto_open_off_still_suppresses_escalation_bridging(convo):
    assert cases.AUTO_OPEN is False
    cid = convo()
    escalation.open("complaint", "test-suite", summary="still suppressed",
                    channel="webchat", handle="step4c@example.invalid",
                    conversation_id=cid)
    assert _case_count(cid) == 0


# ── 7. failure isolation ─────────────────────────────────────────────────────

def test_60_case_lookup_failure_does_not_break_takeover(convo, monkeypatch):
    cid = convo()

    def boom(*a, **k):
        raise RuntimeError("case layer down")

    monkeypatch.setattr(cases, "case_for_conversation", boom)
    res = agent_console.takeover(cid, "alan")
    assert res["ok"] is True, "the rep must still get the conversation"


def test_61_case_annotation_failure_does_not_fail_the_send(convo, monkeypatch):
    """The customer's message has already gone out; failing to annotate the
    case must never report that send as failed."""
    cid = convo()
    cases.open_for_conversation(cid, actor="alan")

    def boom(*a, **k):
        raise RuntimeError("annotation down")

    monkeypatch.setattr(cases, "mark_first_response", boom)
    res = agent_console.send_reply(cid, "alan", "still delivered")
    assert res["ok"] is True


def test_62_failed_creation_leaves_no_partial_case(convo, monkeypatch):
    cid = convo()

    def boom(*a, **k):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(cases, "open_case", boom)
    with pytest.raises(RuntimeError):
        cases.open_for_conversation(cid, actor="alan")
    assert _case_count(cid) == 0


# ── 8. Step 4b: an obligation is discharged only when something carries it ───

def _esc_status(conversation_id):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT status, assigned_to FROM escalations
                           WHERE conversation_id=%s::uuid
                           ORDER BY created_at DESC LIMIT 1""",
                        (conversation_id,))
            return cur.fetchone()
    finally:
        c.close()


def test_70_takeover_without_a_case_assigns_rather_than_resolves(convo):
    """Before 4b this resolved the escalation, closing the EVENT while no work
    record existed — the obligation survived only in someone's memory."""
    cid = convo()
    escalation.open("customer_requested_human", "test-suite",
                    summary="asked for a person", channel="webchat",
                    handle="s4b-a@example.invalid", conversation_id=cid)
    res = agent_console.takeover(cid, "alan")
    status, assigned_to = _esc_status(cid)
    assert status == "assigned", "the obligation was discharged with nothing carrying it"
    assert assigned_to == "alan"
    assert res.get("escalations_assigned")
    assert "escalations_resolved" not in res


def test_71_takeover_with_a_case_resolves_the_escalation(convo):
    """A case now carries the work, so the event may close."""
    cid = convo()
    escalation.open("complaint", "test-suite", summary="unhappy",
                    channel="webchat", handle="s4b-b@example.invalid",
                    conversation_id=cid)
    cases.open_for_conversation(cid, actor="alan")
    res = agent_console.takeover(cid, "alan")
    assert _esc_status(cid)[0] == "resolved"
    assert res.get("escalations_resolved")


def test_72_an_assigned_obligation_stays_visible(convo):
    """Nothing downstream needed changing: every consumer already treats
    'assigned' as live, which is what makes forgetting loud instead of silent."""
    cid = convo()
    escalation.open("complaint", "test-suite", summary="still owed",
                    channel="webchat", handle="s4b-c@example.invalid",
                    conversation_id=cid)
    agent_console.takeover(cid, "alan")
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM escalations
                           WHERE conversation_id=%s::uuid
                             AND status IN ('open','assigned')""", (cid,))
            assert cur.fetchone()[0] == 1
    finally:
        c.close()


def test_73_recording_the_work_then_taking_over_discharges_it(convo):
    """The intended path: assign first, then the rep records the work and the
    obligation closes on the next pickup."""
    cid = convo()
    escalation.open("customer_requested_human", "test-suite",
                    summary="two-step", channel="webchat",
                    handle="s4b-d@example.invalid", conversation_id=cid)
    agent_console.takeover(cid, "alan")
    assert _esc_status(cid)[0] == "assigned"
    cases.open_for_conversation(cid, actor="alan")
    agent_console.takeover(cid, "alan")
    assert _esc_status(cid)[0] == "resolved"


def test_74_creating_a_case_alone_does_not_discharge_the_obligation(convo):
    """Recording work is not the same as starting it. Nobody has picked this
    up, so the promise clock must keep running."""
    cid = convo()
    escalation.open("complaint", "test-suite", summary="recorded not started",
                    channel="webchat", handle="s4b-e@example.invalid",
                    conversation_id=cid)
    cases.open_for_conversation(cid, actor="alan")
    assert _esc_status(cid)[0] == "open"


def test_75_takeover_still_succeeds_if_escalation_handling_fails(convo, monkeypatch):
    cid = convo()

    def boom(*a, **k):
        raise RuntimeError("escalation layer down")

    monkeypatch.setattr(escalation, "assign_for_conversation", boom)
    monkeypatch.setattr(escalation, "resolve_for_conversation", boom)
    assert agent_console.takeover(cid, "alan")["ok"] is True
