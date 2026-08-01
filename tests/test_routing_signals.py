"""C2.1 — allocation intelligence: language, skill, capacity.

C2's stated gap: "no queue, capacity, skill or language routing anywhere … we
answer in 4 languages and have no way to route a French case to a French
speaker."

THE RULE THAT MATTERS MOST: absent data never fabricates a match. NULL
languages means "we do not know what this person speaks", never "they speak
everything". A rule requiring French with nobody recorded as speaking it finds
NOBODY — and names who was considered and what disqualified each of them.
Routing nowhere loudly beats routing to someone who cannot help the customer.
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


@pytest.fixture(scope="module", autouse=True)
def _requires_db():
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_name='assignable_identity'
                             AND column_name='languages'""")
            if not cur.fetchone():
                pytest.skip("sql/routing_signals.sql not applied")
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


def _purge_rule_history(rule_id):
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
def case():
    made = []

    def _make(**kw):
        cid = cases.open_case(kw.pop("subject", "signal probe"),
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


@pytest.fixture
def rule():
    made = []

    def _make(**kw):
        kw.setdefault("name", "signal rule")
        kw.setdefault("position", 1)
        kw.setdefault("target_source", "executive")
        made.append(routing.save_rule(kw, actor="test")["rule_id"])
        return made[-1]

    yield _make
    for rid in made:
        routing.delete_rule(rid)
        _purge_rule_history(rid)


@pytest.fixture
def attrs():
    """Set attributes and always put them back."""
    saved = {}

    def _set(email, **kw):
        if email not in saved:
            row = _sql("""SELECT languages, skills FROM assignable_identity
                          WHERE lower(email)=lower(%s)""", (email,))
            saved[email] = row[0] if row else (None, None)
        return assignable.set_attributes(email, by="test", **kw)

    yield _set
    for email, (langs, skills) in saved.items():
        _sql("""UPDATE assignable_identity SET languages=%s, skills=%s
                WHERE lower(email)=lower(%s)""", (langs, skills, email),
             fetch=False)


# ── 1. language routing — the named C2 gap ───────────────────────────────────

def test_01_a_french_case_finds_nobody_when_nobody_speaks_french(case, rule):
    """The exact scenario C2 was written about."""
    rule(name="french support", requires_language="fr")
    r = routing.recommend(case())
    assert r["candidates"] == []
    assert r["excluded"], "nobody was even reported as considered"
    assert all("fr" in e["reason"] for e in r["excluded"])


def test_02_the_block_names_who_was_considered_and_why(case, rule):
    rule(name="french support", requires_language="fr")
    b = routing.recommend(case())["blocked"]
    assert "were considered" in b and "does not work in fr" in b


def test_03_a_recorded_speaker_becomes_eligible(case, rule, attrs):
    attrs("cfo@agentorc.ca", languages=["en", "fr"])
    rule(name="french support", requires_language="fr")
    r = routing.recommend(case())
    assert [c["email"] for c in r["candidates"]] == ["cfo@agentorc.ca"]


def test_04_unknown_languages_never_mean_all(case, rule, attrs):
    """NULL is UNKNOWN. A router that read it as 'speaks everything' would send
    a French caller to someone who cannot help them."""
    attrs("cfo@agentorc.ca", languages=[])
    rule(name="french support", requires_language="fr")
    r = routing.recommend(case())
    assert not [c for c in r["candidates"] if c["email"] == "cfo@agentorc.ca"]
    reason = [e["reason"] for e in r["excluded"]
              if e["email"] == "cfo@agentorc.ca"][0]
    assert "no recorded languages" in reason


def test_05_language_matching_is_case_insensitive(case, rule, attrs):
    attrs("cfo@agentorc.ca", languages=["FR"])
    rule(name="french support", requires_language="fr")
    assert "cfo@agentorc.ca" in [c["email"]
                                 for c in routing.recommend(case())["candidates"]]


# ── 2. skills ────────────────────────────────────────────────────────────────

def test_10_a_missing_skill_excludes_with_a_named_reason(case, rule, attrs):
    attrs("cfo@agentorc.ca", skills=["billing"])
    rule(name="vpn work", requires_skills=["vpn"])
    r = routing.recommend(case())
    assert r["candidates"] == []
    reason = [e["reason"] for e in r["excluded"]
              if e["email"] == "cfo@agentorc.ca"][0]
    assert "missing skill(s): vpn" in reason


def test_11_all_required_skills_must_be_present(case, rule, attrs):
    attrs("cfo@agentorc.ca", skills=["vpn"])
    rule(name="vpn + billing", requires_skills=["vpn", "billing"])
    assert routing.recommend(case())["candidates"] == []
    attrs("cfo@agentorc.ca", skills=["vpn", "billing"])
    assert "cfo@agentorc.ca" in [c["email"]
                                 for c in routing.recommend(case())["candidates"]]


def test_12_no_requirement_means_no_filtering(case, rule):
    rule(name="anyone")
    assert len(routing.recommend(case())["candidates"]) == 4


# ── 3. capacity: least loaded first, deterministically ───────────────────────

def test_20_workload_is_counted_live_not_stored():
    src = inspect.getsource(routing.workload)
    assert "FROM cases" in src
    cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                  WHERE table_name='assignable_identity'""")}
    for stored in ("open_cases", "workload", "capacity", "load"):
        assert stored not in cols, f"a stored counter appeared: {stored}"


def test_21_the_least_loaded_eligible_person_ranks_first(case, rule):
    """Give one executive a case, and they must stop being the top pick."""
    busy = assignable.resolve("ceo@agentorc.ca")
    cid = case()
    cases.assign(cid, busy, actor="test", source="test")
    try:
        rule(name="anyone")
        r = routing.recommend(case())
        top = r["candidates"][0]
        assert top["email"] != "ceo@agentorc.ca"
        assert top["open_cases"] == 0
        loaded = [c for c in r["candidates"] if c["email"] == "ceo@agentorc.ca"]
        assert loaded and loaded[0]["open_cases"] == 1
    finally:
        cases.unassign(cid, actor="test", source="test")


def test_22_ranking_is_stable_across_runs(case, rule):
    """Two runs of the same policy on the same data must agree, or a
    recommendation is a coin flip wearing a reason."""
    rule(name="anyone")
    cid = case()
    a = [c["email"] for c in routing.recommend(cid)["candidates"]]
    b = [c["email"] for c in routing.recommend(cid)["candidates"]]
    assert a == b and a == sorted(a)


def test_23_every_candidate_explains_its_rank(case, rule):
    rule(name="anyone")
    for c in routing.recommend(case())["candidates"]:
        assert c["why"] and isinstance(c["open_cases"], int)
    assert "least loaded" in routing.recommend(case())["candidates"][0]["why"]


# ── 4. the boundaries C2 inherited still hold ────────────────────────────────

def test_30_signals_never_assign(case, rule, attrs):
    attrs("cfo@agentorc.ca", languages=["en", "fr"])
    rule(name="french", requires_language="fr")
    cid = case()
    before = cases.get(cid)["owner_id"]
    routing.recommend(cid)
    routing.preview(50)
    assert cases.get(cid)["owner_id"] == before


def test_31_no_llm_reaches_the_eligibility_decision():
    code = re.sub(r'"""[\s\S]*?"""', "", inspect.getsource(routing))
    for token in ("_get_llm", "openai", "completion", "embedding"):
        assert token not in code


def test_32_no_silent_fallback_when_everyone_is_excluded(case, rule):
    rule(name="impossible", requires_language="xx")
    r = routing.recommend(case())
    assert r["candidates"] == []
    assert len(r["excluded"]) == 4, "someone was dropped from the report"


def test_33_requirements_are_policy_not_a_case_field():
    """A case has no language column, and inferring one from a transcript is
    circular — the recogniser already committed to a language before the
    customer spoke. So the RULE states the requirement."""
    case_cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                       WHERE table_name='cases'""")}
    assert "language" not in case_cols and "required_language" not in case_cols
    rule_cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                       WHERE table_name='routing_rules'""")}
    assert "requires_language" in rule_cols and "requires_skills" in rule_cols


def test_34_attributes_live_on_the_curated_directory():
    """Not on `employees` (demo seed) or `owners` (customer contacts) — neither
    describes anyone who can receive work."""
    for table in ("employees", "owners"):
        cols = {r[0] for r in _sql("""SELECT column_name FROM information_schema.columns
                                      WHERE table_name=%s""", (table,))}
        assert "languages" not in cols and "skills" not in cols


def test_35_seeding_claimed_only_english(case):
    """The voice line answers in four languages; nobody was recorded as
    speaking three of them, because nobody verified it."""
    for d in assignable.directory():
        assert d["languages"] == ["en"] or d["languages"] is None or \
            "en" in (d["languages"] or [])
    sql = pathlib.Path("sql/routing_signals.sql").read_text(encoding="utf-8")
    assert "ARRAY['en']" in sql
    assert "'fr'" not in sql and "'zh'" not in sql


def test_36_setting_attributes_is_curation_not_inference():
    code = re.sub(r'"""[\s\S]*?"""', "",
                  inspect.getsource(assignable.set_attributes))
    for guess in ("detect", "infer", "domain", "job_title", "department"):
        assert guess not in code
