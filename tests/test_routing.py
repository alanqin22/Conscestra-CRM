"""C2.1 — routing rules: recommend, never assign.

    routing recommendation  !=  assignment
    assignment              !=  work acceptance

The rules are DATA because "small cases to staff, large ones to executives" is
business policy owned by a manager. A model that infers it cannot be audited,
cannot be edited by the person accountable, and changes between releases.

And "account value" is CUSTOMER SIGNIFICANCE — a case carries no monetary
column at all, so a value rule says "this customer matters", never "this
problem is expensive".
"""
import inspect

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import register_uuid                       # noqa: E402

register_uuid()

from app.core import assignable, cases, routing                 # noqa: E402
from app.core.database import get_connection                    # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.routing_rules')")
            if cur.fetchone()[0] is None:
                pytest.skip("sql/routing_rules.sql not applied")
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
            cur.execute("DELETE FROM cases WHERE case_id=%s::uuid", (cid,))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def case():
    made = []

    def _make(**kw):
        cid = cases.open_case(kw.pop("subject", "routing probe"),
                              actor="test", source="test", **kw)["case_id"]
        made.append(cid)
        return cid

    yield _make
    for cid in made:
        _purge_case(cid)


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
        kw.setdefault("name", "test rule")
        kw.setdefault("position", 1)
        kw.setdefault("target_source", "executive")
        r = routing.save_rule(kw, actor="test")
        made.append(r["rule_id"])
        return r["rule_id"]

    yield _make
    for rid in made:
        routing.delete_rule(rid)
        _purge_rule_history(rid)


# ── 1. recommendation is not assignment ──────────────────────────────────────

def test_01_recommend_never_writes_an_owner(case, rule):
    rule(target_source="executive")
    cid = case()
    before = cases.get(cid)["owner_id"]
    routing.recommend(cid)
    assert cases.get(cid)["owner_id"] == before


def test_02_recommend_writes_no_history(case, rule):
    rule()
    cid = case()
    before = len(cases.history(cid))
    routing.recommend(cid)
    routing.preview(50)
    assert len(cases.history(cid)) == before


def test_03_the_module_contains_no_assignment_path():
    """Checked against executable code: the module docstring necessarily names
    _mutate() while explaining that routing must never reach it."""
    import re as _re
    src = inspect.getsource(routing)
    code = _re.sub(r'"""[\s\S]*?"""', "", src)
    code = _re.sub(r"(?m)^\s*#.*$", "", code)
    for verb in ("cases.assign", "UPDATE cases", "INSERT INTO cases",
                 "_mutate"):
        assert verb not in code, f"routing can assign: {verb}"


def test_04_preview_says_it_changed_nothing():
    p = routing.preview(5)
    assert p["ok"] and "Nothing was assigned" in p["note"]


# ── 2. the policy is data, editable by a human ───────────────────────────────

def test_10_rules_are_ordered_and_first_match_wins(case, rule):
    cid = case(priority="low")
    rule(name="zz catch-all", position=90, target_source="employee")
    rule(name="aa specific", position=1, target_source="executive")
    r = routing.recommend(cid)
    assert r["matched_rule"]["name"] == "aa specific"


def test_11_an_inactive_rule_does_not_match(case, rule):
    cid = case(priority="low")
    rid = rule(name="disabled", position=1, target_source="executive",
               is_active=False)
    r = routing.recommend(cid)
    assert (r.get("matched_rule") or {}).get("rule_id") != rid


def test_12_a_rule_naming_nobody_is_refused():
    r = routing.save_rule({"name": "targets nothing"}, actor="test")
    assert r["ok"] is False and "must name a target" in r["error"]


def test_13_an_unnamed_rule_is_refused():
    assert routing.save_rule({"target_source": "executive"},
                             actor="test")["ok"] is False


def test_14_editing_a_rule_is_a_human_act_not_an_inference():
    src = inspect.getsource(routing)
    for llm in ("_get_llm", "invoke(", "openai", "llm"):
        assert llm not in src.lower().replace("llm's role", ""), \
            "a model reached into the routing decision"


def test_15_reseeding_never_overwrites_a_human_edit():
    """The seed only fires into an empty table — the whole point of making the
    policy data is that an edit survives the next migration run."""
    sql = open("sql/routing_rules.sql", encoding="utf-8").read()
    assert "WHERE NOT EXISTS (SELECT 1 FROM routing_rules)" in sql


# ── 3. conditions ────────────────────────────────────────────────────────────

def test_20_priority_condition(case, rule):
    rule(name="urgent only", position=1, priorities=["urgent"],
         target_source="executive")
    assert routing.recommend(case(priority="urgent"))["matched_rule"]["name"] \
        == "urgent only"
    assert routing.recommend(case(priority="low"))["matched_rule"]["name"] \
        != "urgent only"


def test_21_subject_condition(case, rule):
    rule(name="refunds", position=1, subject_like="%refund%",
         target_source="executive")
    assert routing.recommend(case(subject="please process my refund"))[
        "matched_rule"]["name"] == "refunds"
    assert routing.recommend(case(subject="printer is offline"))[
        "matched_rule"]["name"] != "refunds"


def test_22_value_floor_excludes_a_zero_value_account(case, rule):
    rule(name="whales", position=1, min_account_value=50000,
         target_source="executive")
    r = routing.recommend(case())
    assert (r.get("matched_rule") or {}).get("name") != "whales"


def test_23_a_rule_with_no_conditions_is_the_catch_all(case, rule):
    rule(name="everything", position=1, target_source="executive")
    assert routing.recommend(case())["matched_rule"]["name"] == "everything"


# ── 4. "amount" means customer significance ──────────────────────────────────

def test_30_a_case_has_no_monetary_column():
    cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='cases'""")}
    assert not {"amount", "value", "revenue", "total"} & cols


def test_31_value_is_pipeline_plus_ar_not_case_cost():
    v = routing.account_value(None)
    assert v["known"] is False
    src = inspect.getsource(routing.account_value)
    assert "opportunities" in src and "invoices" in src


def test_32_every_recommendation_states_the_basis(case, rule):
    rule()
    r = routing.recommend(case())
    assert "customer significance" in r["value_basis"]
    assert "no monetary value of its own" in r["value_basis"]


# ── 5. targets must be assignable, with no silent fallback ───────────────────

def test_40_a_rule_targeting_an_ungranted_tier_produces_nobody(case, rule):
    """The C2.0 finding surfacing in the product: employees are demo seed data
    and nobody granted them, so the staff tier routes to no one."""
    rule(name="to staff", position=1, target_source="employee")
    r = routing.recommend(case())
    assert r["candidates"] == []
    assert "nobody there is currently assignable" in r["blocked"]


def test_41_a_matched_rule_never_falls_back_to_someone_else(case, rule):
    rule(name="to staff", position=1, target_source="employee")
    r = routing.recommend(case())
    assert not r["candidates"], "routing fell back to an arbitrary person"


def test_42_a_granted_tier_produces_real_candidates(case, rule):
    rule(name="to an exec", position=1, target_source="executive")
    r = routing.recommend(case())
    assert len(r["candidates"]) == 4
    assert all(c["email"].endswith("@agentorc.ca") for c in r["candidates"])
    assert all(c["owner_id"] for c in r["candidates"])


def test_43_a_revoked_person_stops_being_recommended(case, rule):
    assignable.grant("router.probe@agentorc.ca",
                     owner_id=assignable.resolve("ceo@agentorc.ca"),
                     added_by="test")
    try:
        rule(name="to the probe", position=1,
             target_email="router.probe@agentorc.ca")
        assert routing.recommend(case())["candidates"]
        assignable.revoke("router.probe@agentorc.ca", by="test")
        r = routing.recommend(case())
        assert r["candidates"] == [] and r["blocked"]
    finally:
        _sql("DELETE FROM assignable_identity WHERE lower(email)="
             "'router.probe@agentorc.ca'", fetch=False)


def test_44_rules_target_an_email_not_a_frozen_owner_id():
    """A rule written today must not keep pointing at someone after their
    membership is revoked."""
    cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='routing_rules'""")}
    assert "target_email" in cols and "target_owner_id" not in cols


# ── 6. safety ────────────────────────────────────────────────────────────────

def test_50_unknown_case_is_refused():
    import uuid
    assert routing.recommend(str(uuid.uuid4()))["ok"] is False


def test_51_preview_excludes_historical_cases():
    src = inspect.getsource(routing.preview)
    assert "is_historical = false" in src


def test_52_recommend_never_raises_on_a_broken_lookup(monkeypatch):
    monkeypatch.setattr(routing, "_rows", lambda *a, **k: [])
    r = routing.recommend("whatever")
    assert r["ok"] is False
