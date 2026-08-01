"""C1 Step 3 — the escalation → case bridge.

An escalation is the EVENT; a case is the durable unit of WORK it creates.
These tests hold the bridge to the ratified contract:

  * the WORK RECORD outranks a perfect owner mapping — an unresolvable
    assignee produces an UNOWNED case, never a refusal and never a fabricated
    owner
  * one source escalation → at most one originating case, enforced by the
    database rather than by application care
  * an escalation's timestamps are NOT case response or resolution times
  * escalation behaviour is byte-identical while CASES_AUTO_OPEN=0
"""
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                      # noqa: E402

register_uuid()

from app.core import cases, escalation                         # noqa: E402
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
                pytest.skip("sql/case_escalation_bridge.sql not applied")
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
            cur.execute("DELETE FROM cases WHERE case_id=%s::uuid", (case_id,))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def esc():
    """A real escalation row, removed afterwards along with any case it spawned."""
    made = []

    def _make(status="open", priority="normal", assigned_to=None,
              channel="webchat", reason="customer_requested_human",
              summary="Caller asked for a person", conversation_id=None):
        c = get_connection()
        try:
            with c.cursor() as cur:
                cur.execute(
                    """INSERT INTO escalations
                         (source, reason, summary, transcript_excerpt, status,
                          priority, sla_minutes, sla_due_at, channel,
                          assigned_to, conversation_id, contact_known)
                       VALUES ('test', %s, %s, 'excerpt', %s, %s, 60,
                               now() + interval '60 minutes', %s, %s,
                               %s::uuid, true)
                       RETURNING escalation_id::text""",
                    (reason, summary, status, priority, channel, assigned_to,
                     conversation_id))
                eid = cur.fetchone()[0]
            c.commit()
        finally:
            c.close()
        made.append(eid)
        return eid

    yield _make
    for eid in made:
        c = get_connection()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT case_id::text FROM cases "
                            "WHERE escalation_id=%s::uuid", (eid,))
                for r in cur.fetchall():
                    _purge_case(r[0])
                cur.execute("DELETE FROM escalations WHERE escalation_id=%s::uuid",
                            (eid,))
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


# ── eligibility ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["open", "assigned"])
def test_01_owed_escalations_bridge(esc, status):
    r = cases.open_from_escalation(esc(status=status), actor="test")
    assert r["ok"] and r["created"] and r["case_id"]


def test_02_resolved_escalations_do_not_bridge(esc):
    """Bridging finished work would manufacture a backlog."""
    r = cases.open_from_escalation(esc(status="resolved"), actor="test")
    assert r["ok"] is False and "not bridgeable" in r["skipped"]


def test_03_unknown_escalation_is_refused():
    r = cases.open_from_escalation(str(uuid.uuid4()), actor="test")
    assert r["ok"] is False and "no such escalation" in r["error"]


# ── idempotency: one escalation → at most one case ───────────────────────────

def test_10_second_bridge_returns_the_same_case(esc):
    eid = esc()
    a = cases.open_from_escalation(eid, actor="test")
    b = cases.open_from_escalation(eid, actor="test")
    assert b["case_id"] == a["case_id"]
    assert a["created"] is True and b["created"] is False


def test_11_the_invariant_is_enforced_by_the_database(esc):
    """Not merely by the application's check-then-insert, which races."""
    eid = esc()
    cid = cases.open_from_escalation(eid, actor="test")["case_id"]
    c = get_connection()
    try:
        with c.cursor() as cur, pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("""INSERT INTO cases (subject, status, escalation_id)
                           VALUES ('sneaky duplicate', 'new', %s::uuid)""",
                        (eid,))
        c.rollback()
    finally:
        c.close()
    assert cases.get(cid) is not None


def test_12_backlog_bridge_is_idempotent(esc):
    esc(); esc()
    first = cases.bridge_backlog(limit=50, actor="test")
    second = cases.bridge_backlog(limit=50, actor="test")
    assert first["created"] >= 2
    assert second["created"] == 0


# ── ownership ────────────────────────────────────────────────────────────────

def test_20_resolvable_assignee_becomes_the_owner(esc, an_owner):
    owner_id, email = an_owner
    r = cases.open_from_escalation(esc(assigned_to=email), actor="test")
    assert r["owner_resolved"] is True
    assert cases.get(r["case_id"])["owner_id"] == owner_id


@pytest.mark.parametrize("assignee", ["agent", "Alan Qin", "nobody@example.invalid"])
def test_21_unresolvable_assignee_still_creates_the_case(esc, assignee):
    """The work record outranks a perfect owner mapping."""
    r = cases.open_from_escalation(esc(assigned_to=assignee), actor="test")
    assert r["created"] is True and r["owner_resolved"] is False
    assert cases.get(r["case_id"])["owner_id"] is None


def test_22_the_raw_string_is_never_cast_into_owner_id(esc):
    r = cases.open_from_escalation(esc(assigned_to="agent"), actor="test")
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT owner_id, source_assignee FROM cases "
                        "WHERE case_id=%s::uuid", (r["case_id"],))
            owner, src = cur.fetchone()
    finally:
        c.close()
    assert owner is None and src == "agent"


def test_23_unresolved_assignee_stays_traceable(esc):
    """Answers "why is this unowned, and who was originally named?"."""
    r = cases.open_from_escalation(esc(assigned_to="Alan Qin"), actor="test")
    assert r["source_assignee"] == "Alan Qin"
    assert "not a known CRM owner" in r["unowned_reason"]


def test_24_no_assignee_is_distinguishable_from_an_unresolvable_one(esc):
    r = cases.open_from_escalation(esc(assigned_to=None), actor="test")
    assert r["source_assignee"] is None
    assert "no assignee" in r["unowned_reason"]


def test_25_the_bridge_never_invents_an_owner(esc):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM owners")
            before = cur.fetchone()[0]
    finally:
        c.close()
    cases.open_from_escalation(esc(assigned_to="ghost@example.invalid"),
                               actor="test")
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM owners")
            assert cur.fetchone()[0] == before
    finally:
        c.close()


def test_26_initial_ownership_is_recorded_as_a_real_change(esc, an_owner):
    """Deliberate, per Step 2: the case genuinely had no owner before it
    existed, so NULL -> owner is truthful, not a fabricated reassignment."""
    owner_id, email = an_owner
    r = cases.open_from_escalation(esc(assigned_to=email), actor="test")
    rows = [h for h in cases.history(r["case_id"]) if h["field"] == "owner_id"]
    assert len(rows) == 1
    assert rows[0]["old_value"] is None and rows[0]["new_value"] == owner_id


def test_27_unowned_case_writes_no_owner_history(esc):
    r = cases.open_from_escalation(esc(assigned_to="agent"), actor="test")
    assert [h for h in cases.history(r["case_id"])
            if h["field"] == "owner_id"] == []


# ── field mapping ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("esc_priority,case_priority", [
    ("low", "low"), ("normal", "medium"), ("high", "high"),
    ("urgent", "urgent"),
])
def test_30_priority_vocabularies_are_mapped(esc, esc_priority, case_priority):
    """escalations say 'normal'; cases say 'medium'. An unmapped value would be
    rejected outright by open_case()'s validation."""
    r = cases.open_from_escalation(esc(priority=esc_priority), actor="test")
    assert cases.get(r["case_id"])["priority"] == case_priority


def test_31_case_starts_at_new(esc):
    r = cases.open_from_escalation(esc(), actor="test")
    assert cases.get(r["case_id"])["status"] == "new"


def test_32_escalation_timestamps_are_not_case_timestamps(esc):
    """No established semantic equivalence between "the escalation was raised"
    and "a human responded"."""
    r = cases.open_from_escalation(esc(), actor="test")
    c = cases.get(r["case_id"])
    assert c["first_response_at"] is None
    assert c["resolved_at"] is None
    assert c["closed_at"] is None


def test_33_new_bridged_cases_are_not_historical(esc):
    r = cases.open_from_escalation(esc(), actor="test")
    assert cases.get(r["case_id"])["is_historical"] is False


def test_34_channel_is_preserved_verbatim(esc):
    """Mapping sdr_chat/store_chat/voice onto the legacy chat/email/phone/web
    vocabulary would invent equivalences and lose the producing channel."""
    r = cases.open_from_escalation(esc(channel="sdr_chat"), actor="test")
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT origin FROM cases WHERE case_id=%s::uuid",
                        (r["case_id"],))
            assert cur.fetchone()[0] == "sdr_chat"
    finally:
        c.close()


def test_35_escalation_linkage_is_recorded(esc):
    eid = esc()
    r = cases.open_from_escalation(eid, actor="test")
    assert cases.get(r["case_id"])["escalation_id"] == eid


# ── the bridge never writes to escalations ───────────────────────────────────

def test_40_source_escalation_is_untouched(esc):
    eid = esc(status="open", priority="normal")
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT status, priority, updated_at FROM escalations "
                        "WHERE escalation_id=%s::uuid", (eid,))
            before = cur.fetchone()
    finally:
        c.close()
    cases.open_from_escalation(eid, actor="test")
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT status, priority, updated_at FROM escalations "
                        "WHERE escalation_id=%s::uuid", (eid,))
            assert cur.fetchone() == before, (
                "an escalation must never be marked bridged — there is no "
                "second write that could outlive a failed case creation")
    finally:
        c.close()


# ── feature flags ────────────────────────────────────────────────────────────

def test_50_disabled_blocks_all_case_writes(esc, monkeypatch):
    eid = esc()
    monkeypatch.setattr(cases, "ENABLED", False)
    r = cases.open_from_escalation(eid, actor="test")
    assert r["ok"] is False and "disabled" in r["skipped"]
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM cases WHERE escalation_id=%s::uuid",
                        (eid,))
            assert cur.fetchone()[0] == 0
    finally:
        c.close()


def test_51_auto_open_is_off_so_escalations_create_no_cases(monkeypatch):
    """Escalation behaviour must be byte-identical to before the case layer."""
    assert cases.AUTO_OPEN is False
    called = []
    monkeypatch.setattr(cases, "open_from_escalation",
                        lambda *a, **k: called.append(a) or {})
    res = escalation.open("manual", "test-suite",
                          summary="flag safety probe", channel="webchat",
                          handle="flag-probe@example.invalid")
    try:
        assert called == [], "a case was created with CASES_AUTO_OPEN=0"
        assert "case_id" not in res
    finally:
        if res.get("escalation_id"):
            c = get_connection()
            try:
                with c.cursor() as cur:
                    cur.execute("DELETE FROM escalations WHERE escalation_id=%s::uuid",
                                (res["escalation_id"],))
                c.commit()
            finally:
                c.close()


def test_52_bridge_failure_never_breaks_an_escalation(monkeypatch):
    """open() is documented to never raise; an obligation that failed to spawn
    a case is still an obligation."""
    monkeypatch.setattr(cases, "ENABLED", True)
    monkeypatch.setattr(cases, "AUTO_OPEN", True)

    def boom(*a, **k):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(cases, "open_from_escalation", boom)
    res = escalation.open("manual", "test-suite",
                          summary="bridge failure probe", channel="webchat",
                          handle="boom-probe@example.invalid")
    try:
        assert res["ok"] is True and res["escalation_id"]
    finally:
        c = get_connection()
        try:
            with c.cursor() as cur:
                cur.execute("DELETE FROM escalations WHERE escalation_id=%s::uuid",
                            (res["escalation_id"],))
            c.commit()
        finally:
            c.close()


def test_53_auto_open_on_creates_the_case(monkeypatch):
    monkeypatch.setattr(cases, "ENABLED", True)
    monkeypatch.setattr(cases, "AUTO_OPEN", True)
    res = escalation.open("customer_requested_human", "test-suite",
                          summary="auto-open probe", channel="webchat",
                          handle="auto-probe@example.invalid")
    try:
        assert res.get("case_id"), "AUTO_OPEN=1 must bridge"
        assert cases.get(res["case_id"])["status"] == "new"
    finally:
        if res.get("case_id"):
            _purge_case(res["case_id"])
        c = get_connection()
        try:
            with c.cursor() as cur:
                cur.execute("DELETE FROM escalations WHERE escalation_id=%s::uuid",
                            (res["escalation_id"],))
            c.commit()
        finally:
            c.close()
