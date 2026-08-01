"""C1 Step 2 — the case state machine and narrow field-history writer.

Every test runs against the REAL tables inside a transaction that is always
rolled back, so nothing here can pollute the 120 historical rows or leave
history behind (which could not be deleted anyway — the append-only guard
refuses it, correctly).

The invariants under test, in order of how badly they fail if wrong:

  * a false history chain is worse than no history — so no-op writes record
    nothing, and NULL survives as NULL in both directions
  * history and mutation share ONE transaction; neither can survive alone
  * the write layer is the authoritative transition boundary
  * an owner is a real CRM identity, never an arbitrary string
  * historical rows acquire no fictional past
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                      # noqa: E402

register_uuid()

from app.core import cases                                     # noqa: E402


# ── harness ──────────────────────────────────────────────────────────────────
# cases.py opens its own connection per call, so a test transaction cannot wrap
# it. Instead each test gets a real case created through the real API and
# deletes it afterwards. `cases` has no append-only guard, so cleanup is legal;
# its history rows are cleaned with a privileged path that mirrors what a real
# operator would need (and proves the guard is the only thing standing in the
# way).

@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        from app.core.database import get_connection
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.record_field_history')")
            if cur.fetchone()[0] is None:
                pytest.skip("sql/case_lifecycle.sql not applied")
        c.close()
    except Exception as exc:                                   # pragma: no cover
        pytest.skip(f"no database reachable: {exc}")


def _purge(case_id):
    """Remove a test case and its history.

    History deletion needs the append-only trigger disabled — which is exactly
    the point of the guard, and why this helper exists only in the test file
    and never in application code."""
    from app.core.database import get_connection
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
        kw.setdefault("subject", "step2 test case")
        kw.setdefault("source", "test")
        cid = cases.open_case(actor="test", **kw)["case_id"]
        made.append(cid)
        return cid

    yield _make
    for cid in made:
        _purge(cid)


@pytest.fixture
def owner_id():
    from app.core.database import get_connection
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT owner_id::text, email FROM owners "
                        "WHERE email IS NOT NULL AND coalesce(is_active,true) "
                        "LIMIT 2")
            rows = cur.fetchall()
    finally:
        c.close()
    if len(rows) < 2:
        pytest.skip("need two owners with emails")
    return rows


def _hist(case_id, field=None):
    rows = cases.history(case_id)
    return [r for r in rows if field is None or r["field"] == field]


# ── 1. valid transitions: every designed edge ────────────────────────────────

@pytest.mark.parametrize("path", [
    ["in_progress"],
    ["in_progress", "waiting"],
    ["in_progress", "waiting", "in_progress"],
    ["in_progress", "resolved"],                                # direct
    ["in_progress", "waiting", "resolved"],
    ["in_progress", "waiting", "resolved", "closed"],
    ["in_progress", "waiting", "resolved", "in_progress"],      # reopen
])
def test_01_designed_paths_are_permitted(case, path):
    cid = case()
    for step in path:
        cases.transition(cid, step, actor="test", source="test")
    assert cases.get(cid)["status"] == path[-1]


def test_02_resolved_stamps_resolved_at_not_closed_at(case):
    cid = case()
    cases.transition(cid, "in_progress", actor="test")
    cases.transition(cid, "resolved", actor="test")
    c = cases.get(cid)
    assert c["resolved_at"] is not None
    assert c["closed_at"] is None, "resolution and closure are different events"


def test_03_closed_stamps_closed_at(case):
    cid = case()
    for s in ("in_progress", "resolved", "closed"):
        cases.transition(cid, s, actor="test")
    assert cases.get(cid)["closed_at"] is not None


def test_04_reopen_is_counted_never_silent(case):
    cid = case()
    for s in ("in_progress", "resolved"):
        cases.transition(cid, s, actor="test")
    assert cases.get(cid)["reopen_count"] == 0
    cases.reopen(cid, actor="test", source="test")
    c = cases.get(cid)
    assert c["status"] == "in_progress" and c["reopen_count"] == 1


# ── 2. invalid transitions fail deterministically ────────────────────────────

@pytest.mark.parametrize("path,bad", [
    ([], "waiting"),          # new -> waiting: skips in_progress
    ([], "resolved"),
    ([], "closed"),
    (["in_progress"], "closed"),
    (["in_progress"], "new"),
    (["in_progress", "resolved"], "waiting"),
    (["in_progress", "waiting"], "closed"),
])
def test_10_undesigned_transitions_are_refused(case, path, bad):
    cid = case()
    for s in path:
        cases.transition(cid, s, actor="test")
    with pytest.raises(cases.InvalidTransition):
        cases.transition(cid, bad, actor="test")


def test_11_closed_is_terminal(case):
    cid = case()
    for s in ("in_progress", "resolved", "closed"):
        cases.transition(cid, s, actor="test")
    for target in ("new", "in_progress", "waiting", "resolved"):
        with pytest.raises(cases.InvalidTransition):
            cases.transition(cid, target, actor="test")


def test_12_reopening_a_closed_case_is_refused_naming_the_gap(case):
    """D3 defines this as a NEW linked case; Step 1 added no parent column, so
    resurrecting the row would contradict the design AND destroy the closure."""
    cid = case()
    for s in ("in_progress", "resolved", "closed"):
        cases.transition(cid, s, actor="test")
    with pytest.raises(cases.InvalidTransition) as e:
        cases.reopen(cid, actor="test")
    assert "parent" in str(e.value).lower()


def test_13_unknown_status_is_refused(case):
    cid = case()
    with pytest.raises(cases.InvalidTransition):
        cases.transition(cid, "escalated", actor="test")


def test_14_transition_to_same_status_is_refused(case):
    cid = case()
    with pytest.raises(cases.InvalidTransition):
        cases.transition(cid, "new", actor="test")


def test_15_the_designed_vocabulary_is_preserved():
    assert cases.STATUSES == ("new", "in_progress", "waiting", "resolved",
                              "closed")


def test_16_direct_resolution_is_permitted():
    """Ratified 2026-07-26. `waiting` means BLOCKED pending an external
    response — making it a mandatory stop before resolution would make every
    "waiting" count a lie about how much work is actually blocked."""
    assert "resolved" in cases.TRANSITIONS["in_progress"]


# ── 3. history ───────────────────────────────────────────────────────────────

def test_20_status_change_creates_exactly_one_history_row(case):
    cid = case()
    before = len(_hist(cid, "status"))
    cases.transition(cid, "in_progress", actor="rep", source="console")
    rows = _hist(cid, "status")
    assert len(rows) == before + 1
    assert rows[-1]["old_value"] == "new"
    assert rows[-1]["new_value"] == "in_progress"
    assert rows[-1]["actor"] == "rep" and rows[-1]["source"] == "console"


def test_21_owner_reassignment_stays_provable(case, owner_id):
    """The reason C7 became a constraint on C1: after owner_id is overwritten
    the previous owner exists ONLY here."""
    (a, _), (b, _) = owner_id
    cid = case()
    cases.assign(cid, a, actor="test", source="test")
    cases.assign(cid, b, actor="test", source="test")
    chain = [(r["old_value"], r["new_value"]) for r in _hist(cid, "owner_id")]
    assert chain == [(None, a), (a, b)]
    assert cases.get(cid)["owner_id"] == b


def test_22_priority_change_creates_one_history_row(case):
    cid = case(priority="low")
    cases.set_priority(cid, "urgent", actor="test", source="test")
    rows = _hist(cid, "priority")
    assert len(rows) == 1 and rows[0]["old_value"] == "low"
    assert rows[0]["new_value"] == "urgent"


def test_23_null_to_value_is_preserved(case, owner_id):
    (a, _), _ = owner_id
    cid = case()                                  # opened with no owner
    cases.assign(cid, a, actor="test")
    assert _hist(cid, "owner_id")[0]["old_value"] is None


def test_24_value_to_null_is_preserved(case, owner_id):
    (a, _), _ = owner_id
    cid = case(owner_id=a)
    cases.unassign(cid, actor="test")
    last = _hist(cid, "owner_id")[-1]
    assert last["old_value"] == a and last["new_value"] is None


def test_25_unchanged_value_creates_no_false_history(case, owner_id):
    """A no-op that records a change is a lie about what happened."""
    (a, _), _ = owner_id
    cid = case(owner_id=a, priority="high")
    n = len(_hist(cid))
    cases.assign(cid, a, actor="test")            # same owner
    cases.set_priority(cid, "high", actor="test")  # same priority
    assert len(_hist(cid)) == n


def test_26_only_the_three_tracked_fields_are_recorded(case):
    """Not a log-every-column system."""
    assert cases.TRACKED_FIELDS == ("status", "owner_id", "priority")
    cid = case()
    cases.transition(cid, "in_progress", actor="test")
    assert {r["field"] for r in _hist(cid)} <= set(cases.TRACKED_FIELDS)


def test_27_opening_records_the_starting_point(case):
    cid = case()
    rows = _hist(cid, "status")
    assert rows[0]["old_value"] is None and rows[0]["new_value"] == "new"


def test_28_non_human_actors_are_representable(case):
    cid = case()
    cases.transition(cid, "in_progress", actor="agent:sdr", source="escalation")
    r = _hist(cid, "status")[-1]
    assert r["actor"] == "agent:sdr" and r["actor_id"] is None


# ── 4. atomicity in both directions ──────────────────────────────────────────

def test_30_failed_mutation_leaves_no_history(case):
    """The case UPDATE fails (bad status) -> no history may survive."""
    cid = case()
    n = len(_hist(cid))
    with pytest.raises(cases.CaseError):
        cases.transition(cid, "nonsense", actor="test")
    assert len(_hist(cid)) == n


def test_31_failed_history_rolls_back_the_mutation(case, monkeypatch):
    """Force the history INSERT to fail; the status change must not survive."""
    cid = case()

    def boom(*a, **k):
        raise RuntimeError("history write failed")

    monkeypatch.setattr(cases, "_write_history", boom)
    with pytest.raises(RuntimeError):
        cases.transition(cid, "in_progress", actor="test")
    monkeypatch.undo()
    assert cases.get(cid)["status"] == "new", (
        "the case moved even though its history could not be written")
    assert len(_hist(cid, "status")) == 1        # only the opening row


def test_32_missing_case_is_refused():
    with pytest.raises(cases.CaseError):
        cases.transition(str(uuid.uuid4()), "in_progress", actor="test")


# ── 5. ownership is a real CRM identity ──────────────────────────────────────

def test_40_assign_refuses_a_free_string(case):
    cid = case()
    for bad in ("agent", "Alan Qin", "alan@example.com", ""):
        with pytest.raises(cases.CaseError):
            cases.assign(cid, bad, actor="test")


def test_41_assign_refuses_an_unknown_uuid(case):
    cid = case()
    with pytest.raises(cases.CaseError):
        cases.assign(cid, str(uuid.uuid4()), actor="test")


def test_42_resolve_owner_matches_by_email(owner_id):
    (a, email), _ = owner_id
    assert cases.resolve_owner(email) == a
    assert cases.resolve_owner(email.upper()) == a


def test_43_resolve_owner_refuses_names_and_placeholders():
    """console_takeover defaults the assignee to the literal string 'agent'."""
    for bad in ("agent", "Alan Qin", "", None, "   "):
        assert cases.resolve_owner(bad) is None


def test_44_resolve_owner_never_creates_an_identity():
    from app.core.database import get_connection
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM owners")
            before = cur.fetchone()[0]
    finally:
        c.close()
    assert cases.resolve_owner("nobody-here@example.invalid") is None
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM owners")
            assert cur.fetchone()[0] == before
    finally:
        c.close()


# ── 6. historical rows keep no fictional past ────────────────────────────────

def test_50_historical_rows_are_untouched_by_installing_step2():
    from app.core.database import get_connection
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM cases WHERE is_historical")
            assert cur.fetchone()[0] == 120
            cur.execute("""SELECT count(*) FROM cases
                           WHERE is_historical
                             AND (first_response_at IS NOT NULL
                                  OR resolved_at IS NOT NULL
                                  OR reopen_count <> 0)""")
            assert cur.fetchone()[0] == 0
            cur.execute("""SELECT count(*) FROM record_field_history h
                           JOIN cases c ON c.case_id = h.entity_id
                           WHERE h.entity='case' AND c.is_historical""")
            assert cur.fetchone()[0] == 0, (
                "a historical case acquired a fabricated transition history")
    finally:
        c.close()


def test_51_new_cases_are_not_historical(case):
    assert cases.get(case())["is_historical"] is False


# ── 7. hooks stay hooks; open questions stay open ────────────────────────────

def test_60_first_response_is_idempotent(case):
    cid = case()
    assert cases.mark_first_response(cid)["stamped"] is True
    first = cases.get(cid)["first_response_at"]
    assert cases.mark_first_response(cid)["stamped"] is False
    assert cases.get(cid)["first_response_at"] == first


def test_61_transitions_do_not_auto_stamp_first_response(case):
    """What counts as a first response is the console bridge's decision."""
    cid = case()
    cases.transition(cid, "in_progress", actor="test")
    assert cases.get(cid)["first_response_at"] is None


def test_62_no_sla_logic_leaked_into_step2():
    """Open questions 1 and 3 stay open."""
    src = dir(cases)
    assert not [n for n in src if "sla" in n.lower()]
    assert not [n for n in src if "pause" in n.lower()]


def test_63_comments_stay_case_local(case):
    """Open question 2 stays open: no conversation write from comment()."""
    cid = case()
    r = cases.comment(cid, "internal note", internal=True, created_by=None)
    assert r["ok"] and r["case_comment_id"]


# ── 8. feature-flag safety ───────────────────────────────────────────────────

def test_70_automatic_creation_is_gated_at_the_call_site():
    """The bridge may create cases; only CASES_AUTO_OPEN may make it automatic.

    open_from_escalation() legitimately calls open_case() — that IS the bridge.
    The invariant that matters is that the escalation path cannot reach it
    without BOTH flags on."""
    import pathlib
    import re
    assert cases.AUTO_OPEN is False
    src = pathlib.Path("app/core/escalation.py").read_text(encoding="utf-8")
    call = src.index("open_from_escalation(")
    guard = src.rindex("cases.AUTO_OPEN", 0, call)
    assert call - guard < 200, (
        "escalation.py reaches the bridge without an adjacent AUTO_OPEN guard")
    assert re.search(r"cases\.ENABLED and cases\.AUTO_OPEN", src), (
        "the bridge must require BOTH flags")


def test_71_only_the_sanctioned_surfaces_import_the_case_layer():
    """Who may reach the case write layer is a governed question.

    Sanctioned: the escalation bridge (Step 3), the console bridge (Step 4),
    the cases agent package (Step 5) — which IS the case surface — and the
    knowledge bridge (Step 8), which reads the flags only. Anything
    else is an ungoverned path in and must be a deliberate step, not a
    convenience; this fails loudly when one appears."""
    import pathlib
    allowed = {"app/core/escalation.py", "app/core/agent_console.py",
               "app/core/knowledge.py"}
    hits = []
    for p in pathlib.Path("app").rglob("*.py"):
        rel = p.as_posix()
        if rel == "app/core/cases.py" or rel.startswith("app/agents/cases/"):
            continue
        t = p.read_text(encoding="utf-8", errors="ignore")
        if "from app.core import cases" in t or "from app.core.cases" in t:
            hits.append(rel)
    assert set(hits) == allowed, f"unexpected importers: {sorted(set(hits) - allowed)}"


def test_72_disabled_flag_refuses_writes(case, monkeypatch):
    cid = case()
    monkeypatch.setattr(cases, "ENABLED", False)
    with pytest.raises(cases.CaseError):
        cases.transition(cid, "in_progress", actor="test")


# ── 9. the mutation boundary: one API, one implementation, one policy ───────

def test_80_there_is_exactly_one_transition_matrix():
    """The hazard: a lifecycle policy maintained in two places drifts, and the
    weaker copy decides. This fails if a second matrix appears ANYWHERE —
    including in a stored procedure, which is why sql/ and sp/ are scanned too.

    (The Python matrix is authoritative precisely because sql/ and sp/ are
    gitignored: policy in an untracked file is not reviewed, not deployed with
    the app, and applied to Railway by hand.)"""
    import pathlib
    import re
    suspects = []

    # (a) Python: only cases.py may DEFINE the matrix. Reading cases.TRANSITIONS
    #     is exactly right and must not be flagged — the agent's executor does
    #     it to show a user their legal next moves.
    define = re.compile(r"^\s*TRANSITIONS\s*[:=]", re.M)
    for f in pathlib.Path("app").rglob("*.py"):
        if f.as_posix() == "app/core/cases.py":
            continue
        if define.search(f.read_text(encoding="utf-8", errors="ignore")):
            suspects.append(f.as_posix())

    # (b) SQL: an enforcing copy would live in a case procedure. Scoped to case
    #     artefacts on purpose — `in_progress` is also an ACTIVITY and ORDER
    #     status, so a bare word match flags legacy whole-schema dumps that
    #     have nothing to do with the case lifecycle.
    for root in ("sql", "sp"):
        base = pathlib.Path(root)
        if not base.exists():
            continue
        for f in base.rglob("*.sql"):
            t = f.read_text(encoding="utf-8", errors="ignore")
            u = t.upper()
            if "SP_CASE" in u:               # any case mutation procedure
                suspects.append(f.as_posix())
            elif "case" in f.name.lower() and "IN_PROGRESS" in u and "RAISE" in u:
                suspects.append(f.as_posix())

    # KNOWN LEGACY: sp_cases() — a 14-mode case procedure authored in January
    # 2026 beside the cases table and never governed. It EXISTS in the schema
    # (and in the live database), which is a different thing from being an
    # available application mutation path:
    #
    #     exists  = true    (DROP deliberately deferred; see test_21 there)
    #     usable  = false   (guard_query rejects it unconditionally)
    #
    # The usability half is asserted in tests/test_case_second_boundary.py. This
    # test only guarantees that no THIRD policy appears.
    KNOWN_LEGACY = {"sp/crm_db.sql", "sp/crm_db_tables.sql"}
    new = sorted(set(suspects) - KNOWN_LEGACY)
    assert new == [], f"a NEW second transition policy exists in: {new}"


def test_85_the_legacy_procedure_is_not_an_available_mutation_path():
    """`exists` and `is reachable` are different claims. This holds the second
    one so the known exception can never quietly become a live path again."""
    from app.core import write_guard
    assert "sp_cases" in write_guard.FORBIDDEN_PROCEDURES
    with pytest.raises(write_guard.WritePermissionError):
        write_guard.guard_query("SELECT sp_cases(p_mode := 'close') AS result")


def test_81_there_is_exactly_one_history_writer_in_the_codebase():
    """Three hand-rolled INSERTs were collapsed into one writer, which then
    MOVED to app/core/history.py when routing policy became the second object
    worth auditing. The invariant is codebase-wide, not file-local: one writer,
    one definition of what "before" means."""
    import pathlib
    hits = []
    for f in pathlib.Path("app").rglob("*.py"):
        n = f.read_text(encoding="utf-8", errors="ignore").count(
            "INSERT INTO record_field_history")
        if n:
            hits.append((f.as_posix(), n))
    assert hits == [("app/core/history.py", 1)], hits


def test_82_the_history_writer_joins_the_callers_transaction():
    """It must take a cursor. A helper that opened its own connection would
    silently break the atomicity guarantee while looking tidier."""
    import inspect
    from app.core import history
    assert list(inspect.signature(cases._write_history).parameters)[0] == "cur"
    assert list(inspect.signature(history.write).parameters)[0] == "cur"


def test_83_every_tracked_field_write_goes_through_one_mutation_path():
    """_mutate() is the single canonical mutation for tracked fields. A raw
    UPDATE of status/owner_id/priority elsewhere would bypass the state
    machine, owner validation and history in one move."""
    import pathlib
    import re
    src = pathlib.Path("app/core/cases.py").read_text(encoding="utf-8")
    # Every UPDATE cases ... SET outside _mutate must touch only untracked
    # columns (first_response_at is the sole such writer today).
    for m in re.finditer(r"UPDATE cases\s+SET ([^\"]{0,120})", src):
        clause = m.group(1)
        if "', '.join(sets)" in clause:      # the _mutate() builder itself
            continue
        for field in cases.TRACKED_FIELDS:
            assert f"{field}=" not in clause.replace(" ", ""), (
                f"a raw UPDATE writes the tracked field {field!r} outside "
                f"_mutate(), bypassing validation and history")


def test_84_no_caller_reaches_the_database_around_the_case_layer():
    """The public API boundary: bridges and the agent may call cases.py, but
    none of them may write the case tables directly."""
    import pathlib
    offenders = []
    for rel in ("app/core/escalation.py", "app/core/agent_console.py",
                "app/agents/cases/sql_builder.py",
                "app/agents/cases/graph.py", "app/agents/cases/router.py"):
        t = pathlib.Path(rel).read_text(encoding="utf-8")
        for verb in ("UPDATE cases", "INSERT INTO cases",
                     "INSERT INTO record_field_history", "DELETE FROM cases"):
            if verb in t:
                offenders.append(f"{rel}: {verb}")
    assert offenders == [], offenders
