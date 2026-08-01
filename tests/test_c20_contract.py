"""The C2.0 contract — all twelve items, asserted in one place.

The environment is synthetic. There is exactly one real human who can work
cases, and the 21 `employees` rows are demo seed data that merely LOOK like
staff. So:

    synthetic employee          !=  real assignable worker
    exists in employee directory !=  authorized to receive live work
    employee record  =  directory identity
    assignable       =  explicit authorization to receive work
    assigned         =  a specific work record was given to that identity

Three concepts, not interchangeable.
"""
import inspect
import pathlib
import re

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import assignable, cases, routing                 # noqa: E402
from app.core.database import get_connection                    # noqa: E402

PAGE = pathlib.Path("case-mgmt.html")
HTML = PAGE.read_text(encoding="utf-8") if PAGE.exists() else ""


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.routing_rules')")
            if cur.fetchone()[0] is None:
                pytest.skip("C2 migrations not applied")
        c.close()
    except Exception as exc:                                    # pragma: no cover
        pytest.skip(f"no database reachable: {exc}")


def _sql(q, a=(), fetch=True):
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute(q, a)
            r = cur.fetchall() if fetch else None
        c.commit()
        return r
    finally:
        c.close()


@pytest.fixture
def case():
    made = []

    def _make(**kw):
        cid = cases.open_case(kw.pop("subject", "c2 contract probe"),
                              actor="test", source="test", **kw)["case_id"]
        made.append(cid)
        return cid

    yield _make
    for cid in made:
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


def _purge_rule_history(rule_id):
    """Rule history is append-only, so a test must lower the guard to clean up
    — the same privileged path the case tests use, and for the same reason."""
    c = get_connection()
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE record_field_history DISABLE TRIGGER "
                        "trg_rfh_append_only")
            cur.execute("DELETE FROM record_field_history "
                        "WHERE entity='routing_rule' AND entity_id=%s::uuid",
                        (rule_id,))
            cur.execute("ALTER TABLE record_field_history ENABLE TRIGGER "
                        "trg_rfh_append_only")
        c.commit()
    finally:
        c.close()


@pytest.fixture
def rule():
    made = []

    def _make(**kw):
        kw.setdefault("name", "contract rule")
        kw.setdefault("position", 1)
        kw.setdefault("target_source", "executive")
        made.append(routing.save_rule(kw, actor="test")["rule_id"])
        return made[-1]

    yield _make
    for rid in made:
        routing.delete_rule(rid)
        _purge_rule_history(rid)


# ── 1. explicit grant ────────────────────────────────────────────────────────

def test_01_grant_is_explicit_and_admin_authorized():
    src = inspect.getsource(assignable.grant)
    assert "added_by" in src, "a grant must record who authorized it"
    r = assignable.grant("contract.probe@agentorc.ca", added_by="test-admin")
    try:
        assert r["ok"]
        row = _sql("""SELECT added_by, source FROM assignable_identity
                      WHERE lower(email)='contract.probe@agentorc.ca'""")
        assert row[0][0] == "test-admin"
    finally:
        _sql("DELETE FROM assignable_identity WHERE lower(email)="
             "'contract.probe@agentorc.ca'", fetch=False)


# ── 2. explicit revocation ───────────────────────────────────────────────────

def test_02_revocation_is_immediate_and_keeps_the_record():
    assignable.grant("revoke.probe@agentorc.ca",
                     owner_id=assignable.resolve("ceo@agentorc.ca"),
                     added_by="test")
    try:
        assert assignable.is_assignable("revoke.probe@agentorc.ca")
        assignable.revoke("revoke.probe@agentorc.ca", by="test")
        assert assignable.is_assignable("revoke.probe@agentorc.ca") is False
        assert _sql("""SELECT count(*) FROM assignable_identity
                       WHERE lower(email)='revoke.probe@agentorc.ca'""")[0][0] == 1
    finally:
        _sql("DELETE FROM assignable_identity WHERE lower(email)="
             "'revoke.probe@agentorc.ca'", fetch=False)


# ── 3. synthetic employees stay unassignable ─────────────────────────────────

def test_03_no_synthetic_employee_is_assignable():
    for email, in _sql("SELECT email FROM employees WHERE email IS NOT NULL"):
        assert assignable.resolve(email) is None, email


def test_03b_the_employee_tier_yields_zero_candidates(case, rule):
    rule(name="to staff", target_source="employee")
    r = routing.recommend(case())
    assert r["candidates"] == []
    assert "nobody there is currently assignable" in r["blocked"]


def test_03c_nothing_auto_grants_from_a_directory_table():
    src = inspect.getsource(assignable)
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"(?m)^\s*#.*$", "", code)
    assert "FROM employees" not in code.split("def inventory")[0], \
        "assignability is derived from the employee table"
    sql = pathlib.Path("sql/assignable_identity.sql").read_text(encoding="utf-8")
    assert "FROM employees" not in sql, "the migration imports employees"
    assert "FROM executives" in sql


# ── 4-5. deterministic, ordered, visible ─────────────────────────────────────

def test_04_no_llm_participates_in_the_routing_decision():
    src = inspect.getsource(routing)
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    for token in ("_get_llm", "invoke(", "openai", "completion"):
        assert token not in code, f"a model reached the routing decision: {token}"


def test_05_first_match_ordering_is_deterministic_and_visible(case, rule):
    cid = case(priority="low")
    rule(name="zz later", position=90, target_source="employee")
    rule(name="aa first", position=1, target_source="executive")
    assert routing.recommend(cid)["matched_rule"]["name"] == "aa first"
    positions = [r["position"] for r in routing.rules(include_inactive=True)]
    assert positions == sorted(positions), "the list is not shown in run order"


# ── 6. preview is read-only ──────────────────────────────────────────────────

def test_06_preview_mutates_nothing(case, rule):
    rule()
    cid = case()
    before = (
        _sql("SELECT count(*) FROM cases")[0][0],
        _sql("SELECT count(*) FROM record_field_history")[0][0],
        _sql("SELECT count(*) FROM escalations")[0][0],
        _sql("SELECT count(*) FROM routing_rules")[0][0],
        cases.get(cid)["owner_id"],
    )
    routing.preview(50)
    after = (
        _sql("SELECT count(*) FROM cases")[0][0],
        _sql("SELECT count(*) FROM record_field_history")[0][0],
        _sql("SELECT count(*) FROM escalations")[0][0],
        _sql("SELECT count(*) FROM routing_rules")[0][0],
        cases.get(cid)["owner_id"],
    )
    assert before == after


def test_06b_preview_explains_each_zero_candidate(case, rule):
    rule(name="to staff", target_source="employee")
    case()
    p = routing.preview(50)
    blocked = [r for r in p["results"] if r.get("blocked")]
    assert blocked and all("assignable" in b["blocked"] for b in blocked)


# ── 7-8. no silent fallback ──────────────────────────────────────────────────

def test_08_a_zero_candidate_result_never_substitutes_anyone(case, rule):
    rule(name="to staff", target_source="employee")
    r = routing.recommend(case())
    assert r["candidates"] == []
    code = re.sub(r'"""[\s\S]*?"""', "", inspect.getsource(routing._candidates))
    for fallback in ("[0]", "or people", "default", "current_user"):
        assert fallback not in code, f"a fallback leaked in: {fallback}"


# ── 9. revoked targets disappear ─────────────────────────────────────────────

def test_09_a_revoked_target_stops_being_recommended(case, rule):
    assignable.grant("rvk.route@agentorc.ca",
                     owner_id=assignable.resolve("ceo@agentorc.ca"),
                     added_by="test")
    try:
        rule(name="to the probe", target_email="rvk.route@agentorc.ca")
        assert routing.recommend(case())["candidates"]
        assignable.revoke("rvk.route@agentorc.ca", by="test")
        assert routing.recommend(case())["candidates"] == []
    finally:
        _sql("DELETE FROM assignable_identity WHERE lower(email)="
             "'rvk.route@agentorc.ca'", fetch=False)


def test_09b_rules_store_an_email_not_a_frozen_owner_id():
    cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='routing_rules'""")}
    assert "target_email" in cols and "target_owner_id" not in cols


# ── 10. zero-candidate results explain why ───────────────────────────────────

def test_10_the_reason_names_the_target_and_the_fix(case, rule):
    rule(name="to staff", target_source="employee")
    b = routing.recommend(case())["blocked"]
    assert "employee tier" in b and "Grant assignability" in b


# ── 11. migrations are idempotent and non-destructive ────────────────────────

def test_11_reseeding_never_overwrites_an_edit():
    sql = pathlib.Path("sql/routing_rules.sql").read_text(encoding="utf-8")
    assert "WHERE NOT EXISTS (SELECT 1 FROM routing_rules)" in sql
    assert "DELETE FROM routing_rules" not in sql
    assert "TRUNCATE" not in sql.upper()
    assert "ON CONFLICT (lower(email)) DO NOTHING" in \
        pathlib.Path("sql/assignable_identity.sql").read_text(encoding="utf-8")


def test_11b_a_human_edit_survives_a_migration_rerun():
    import psycopg2 as pg
    import os
    from app.core.config import settings
    rid = routing.save_rule({"name": "edited by a human", "position": 42,
                             "target_source": "executive"}, actor="test")["rule_id"]
    try:
        c = pg.connect(os.getenv("DATABASE_URL") or settings.db_dsn)
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(pathlib.Path("sql/routing_rules.sql")
                        .read_text(encoding="utf-8"))
        c.close()
        still = [r for r in routing.rules(include_inactive=True)
                 if r["rule_id"] == rid]
        assert still and still[0]["position"] == 42
    finally:
        routing.delete_rule(rid)
        _purge_rule_history(rid)


# ── 12. only explicit assignment changes ownership ───────────────────────────

def test_12_recommendation_changes_no_ownership(case, rule):
    rule()
    cid = case()
    before = cases.get(cid)["owner_id"]
    routing.recommend(cid)
    routing.preview(50)
    assert cases.get(cid)["owner_id"] == before
    assert [h for h in cases.history(cid) if h["field"] == "owner_id"] == []


# ── policy edits are audited ─────────────────────────────────────────────────

def test_20_a_policy_edit_is_recorded_like_any_consequential_change():
    rid = routing.save_rule({"name": "audited", "position": 61,
                             "target_source": "executive"}, actor="alice")["rule_id"]
    try:
        routing.save_rule({"rule_id": rid, "name": "audited", "position": 62,
                           "target_source": "employee"}, actor="bob")
        h = routing.rule_history(rid)
        moves = {(x["field"], x["old_value"], x["new_value"]) for x in h}
        assert ("position", "61", "62") in moves
        assert ("target_source", "executive", "employee") in moves
        assert {x["actor"] for x in h} >= {"alice", "bob"}
    finally:
        routing.delete_rule(rid)
        _purge_rule_history(rid)


def test_21_policy_history_uses_the_one_shared_writer():
    hits = []
    for f in pathlib.Path("app").rglob("*.py"):
        n = f.read_text(encoding="utf-8", errors="ignore").count(
            "INSERT INTO record_field_history")
        if n:
            hits.append(f.as_posix())
    assert hits == ["app/core/history.py"]


# ── the environment is described honestly ────────────────────────────────────

def test_30_the_environment_reports_itself_as_synthetic():
    e = assignable.environment()
    assert e["synthetic"] is True
    assert e["directory_records"] == 21 and e["assignable_and_linked"] == 4
    assert "not automatically able to receive work" in e["message"]


def test_31_the_judgement_is_derived_from_counts_not_a_domain():
    code = re.sub(r'"""[\s\S]*?"""', "",
                  inspect.getsource(assignable.environment))
    for h in ("agentorc", "company.com", "endswith"):
        assert h not in code


def test_32_the_ui_shows_the_synthetic_banner():
    assert "Synthetic organisation" in HTML and "envBanner" in HTML
    assert "not</em>\n            + 'automatically able to receive work" in HTML \
        or "automatically able to receive work" in HTML


def test_33_the_ui_states_the_value_basis_not_case_value():
    assert "Account significance" in HTML
    assert "Case value" not in HTML
    assert "account_significance" in HTML


def test_34_preview_publishes_the_basis():
    p = routing.preview(3)
    assert "open pipeline + outstanding AR" in p["value_basis"]
    assert "no monetary value of its own" in p["value_basis"]
