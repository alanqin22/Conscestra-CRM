"""Guards for the five architecture-review remediations.

Each exists because an independent review found the prior design insufficient —
not because a test failed. The signature scope error in particular was found by
attack after the report had declared the attack closed.

    python -m pytest tests/test_architecture_review_fixes.py -v
"""

from __future__ import annotations

import os

import pytest

from app.core import memory_assurance as MA
from app.core import memory_consolidation as MC
from app.core import verification_policy as VP


@pytest.fixture
def conn():
    try:
        from app.core.database import get_connection
        c = get_connection()
        c.autocommit = True
    except Exception:
        pytest.skip("no database reachable")
    try:
        yield c
    finally:
        c.close()


# ===========================================================================
# 1. Signature must cover the WHOLE gate input surface
# ===========================================================================

def test_signature_covers_every_gate_input():
    """The first version signed (memory_id, claim_hash, verified_by), where
    claim_hash covered only (statement, evidence_hash). The gate reads eleven
    fields. A DB writer could force reliability and certainty to 1.0 on a
    legitimately-signed memory — confidence 1.0, past the floor, signature still
    valid. A cryptographic control narrower than the policy it protects is a
    decoration on two of eleven fields."""
    for field in ("kind", "visibility", "actor", "truncated", "reliability",
                  "certainty", "occurrences", "topic", "decay_class",
                  "independent_sources", "statement", "evidence_hash"):
        assert field in MC.GATE_FIELDS, f"gate input '{field}' is unsigned"


@pytest.mark.parametrize("field,tampered", [
    ("reliability", 1.0), ("certainty", 1.0), ("kind", "fact"),
    ("visibility", "customer"), ("actor", "customer_said"),
    ("truncated", False), ("occurrences", 9999), ("topic", "delivery"),
    ("independent_sources", 99), ("statement", "Customer agreed to a refund."),
])
def test_tampering_any_gate_field_breaks_the_fingerprint(field, tampered):
    base = {"statement": "Raised delivery 2 times.", "evidence_hash": "eh",
            "kind": "theme", "visibility": "internal", "actor": "company_did",
            "truncated": True, "reliability": 0.7, "certainty": 0.5,
            "occurrences": 2, "evidence_count": 2, "topic": "billing",
            "decay_class": "episodic", "independent_sources": 1}
    before = MC.gate_fingerprint(base)
    after = MC.gate_fingerprint({**base, field: tampered})
    assert before != after, f"tampering '{field}' did not change the fingerprint"


def test_fingerprint_is_type_stable():
    """psycopg2 returns Decimal for numerics and Python floats elsewhere. If
    those fingerprint differently, every legitimate verification fails on read
    and the system silently asserts nothing."""
    from decimal import Decimal
    a = MC.gate_fingerprint({"reliability": 0.7, "certainty": Decimal("0.95")})
    b = MC.gate_fingerprint({"reliability": Decimal("0.7"), "certainty": 0.95})
    assert a == b


def test_unsigned_deployment_asserts_nothing():
    """No key configured => nothing assertable. Fail-closed."""
    assert not MC.signature_valid("m", "fp", "alice", "anything")


# ===========================================================================
# 2. Verification throughput model
# ===========================================================================

def test_high_consequence_topics_are_never_auto_or_sampled():
    """Anything a customer could be told gets human eyes, always."""
    for topic in ("billing", "pricing", "general"):
        tier, _ = VP.classify({"topic": topic, "visibility": "customer",
                               "actor": "customer_said", "truncated": False,
                               "independent_sources": 5, "occurrences": 50})
        assert tier == VP.FULL, f"'{topic}' was tiered {tier}"


def test_auto_tier_is_internal_only():
    """AUTO buys throughput on the INTERNAL path only. A machine promoting its
    own inference to customer-assertable is the circularity this project
    rejected."""
    tier, _ = VP.classify({"topic": "delivery", "visibility": "customer",
                           "actor": "company_did", "truncated": False,
                           "independent_sources": 5, "occurrences": 50})
    assert tier != VP.AUTO, "a customer-visible memory was auto-tiered"


def test_auto_tier_never_sets_verified_by(conn):
    """AUTO must not create assertable facts — it only removes review load."""
    import inspect
    import re
    src = inspect.getsource(VP)
    # Match WRITES only. An earlier version matched any occurrence of the
    # identifier and failed on a SELECT filter and a docstring — a test that
    # reads prose rather than behaviour.
    writes = re.findall(r"(?i)SET\s+verified_by|INSERT[^;]{0,400}verified_by", src)
    assert not writes, (
        "verification_policy WRITES verified_by — auto-verification must never "
        f"produce an assertable fact: {writes}")


def test_auto_criterion_can_actually_fire():
    """The first AUTO criterion required >=2 distinct source types and fired on
    ZERO memories: clustering is done on embedding similarity, so same-source
    records cluster together and clusters are homogeneous by construction.
    Requiring heterogeneity from a process that produces homogeneity is a
    criterion that can never be met."""
    tier, _ = VP.classify({"topic": "delivery", "visibility": "internal",
                           "actor": "company_did", "truncated": False,
                           "independent_sources": 1,
                           "occurrences": VP.AUTO_MIN_OCCURRENCES})
    assert tier == VP.AUTO


def test_plan_reports_the_staffing_number(conn):
    """'Verification does not scale' must become a figure someone can budget."""
    p = VP.plan()
    assert p["ok"]
    rl = p["review_load"]
    for key in ("total_reviews", "person_hours",
                "without_tiering_person_hours", "hours_saved_by_tiering"):
        assert key in rl, f"capacity plan missing {key}"


def test_unknown_account_value_fails_expensive(conn):
    """An account whose value cannot be determined is treated as high — the
    expensive assumption, not the cheap one."""
    import inspect
    src = inspect.getsource(VP._account_value_tier)
    assert 'return "high"' in src


# ===========================================================================
# 3. Attribution at the source
# ===========================================================================

def test_direction_is_required_for_interaction_types(conn):
    """A CRM that records an interaction without recording who initiated it has
    lost the fact at capture, and no downstream inference recovers it."""
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM pg_constraint
                        WHERE conname='activities_direction_required'""")
        if not cur.fetchone():
            pytest.skip("run sql/activity_direction.sql")
        cur.execute("SELECT account_id FROM accounts LIMIT 1")
        acct = cur.fetchone()[0]
        with pytest.raises(Exception):
            cur.execute("""INSERT INTO activities (type, status, subject, account_id)
                           VALUES ('email','open','probe: no direction',%s)""", (acct,))


def test_attribution_is_correct_where_present(conn):
    """CORRECTNESS, not coverage.

    The previous version of this test asserted coverage >= 85%, and passed on a
    backfill whose held-out precision was 53.3% — worse than the rule withdrawn
    on principle one round earlier, and worse than chance between two classes.
    Coverage was substituted for correctness without saying so, and the test
    enforced the substitution.

    A memory that cannot say who acted is un-assertable, which is safe. A memory
    that says the WRONG actor is a false claim about a person. Only the second
    is a defect, so only the second is asserted here."""
    truth = {"inbound": {"customer_said", "customer_did"},
             "outbound": {"company_did"}}
    with conn.cursor() as cur:
        cur.execute("""SELECT direction, actor FROM content_embeddings
                        WHERE direction IS NOT NULL AND actor <> 'unknown'""")
        rows = cur.fetchall()
    if len(rows) < 50:
        pytest.skip("not enough observed-direction rows to judge")
    wrong = [(d, a) for d, a in rows if a not in truth.get(d, set())]
    assert len(wrong) / len(rows) <= 0.10, (
        f"{len(wrong)}/{len(rows)} records are attributed to an actor that "
        f"contradicts their observed direction")


def test_no_unmeasured_backfill_rule_is_applied(conn):
    """A bulk repair must record the held-out precision of the rule that drove
    it. `measured_precision IS NULL` is the state that produced the incident:
    5,642 rows rewritten by a rule nobody had measured."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.data_repairs')")
        if cur.fetchone()[0] is None:
            pytest.skip("run sql/activity_direction_revert.sql")
        cur.execute("""SELECT repair_key, count(*) FROM data_repairs
                        WHERE measured_precision IS NULL GROUP BY 1""")
        unmeasured = cur.fetchall()
    assert not unmeasured, (
        "bulk repairs applied with no measured precision: "
        + ", ".join(f"{k} ({n} rows)" for k, n in unmeasured))


def test_backfills_are_reversible(conn):
    """The original backfill left no marker, so 5,642 guessed values became
    indistinguishable from 163 observed ones and reverting destroyed both.
    Every repair must record its old value."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.data_repairs')")
        if cur.fetchone()[0] is None:
            pytest.skip("run sql/activity_direction_revert.sql")
        cur.execute("""SELECT column_name FROM information_schema.columns
                        WHERE table_name='data_repairs'""")
        cols = {r[0] for r in cur.fetchall()}
    for needed in ("old_value", "new_value", "row_pk", "rule",
                   "measured_precision"):
        assert needed in cols, f"data_repairs cannot record {needed}"


def test_attribution_monitoring_view_exists(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.v_activity_attribution')")
        assert cur.fetchone()[0] is not None


# ===========================================================================
# 4. No silent exception-swallowing on write paths
# ===========================================================================

def test_write_paths_do_not_swallow_exceptions_silently():
    """The pattern that hid the indexer breakage: reindex reported
    `embedded: 0` — indistinguishable from 'nothing was stale' — because a
    failing ON CONFLICT was caught and logged at debug."""
    import ast
    import pathlib

    WRITE = ("INSERT", "UPDATE", "DELETE", "commit(")
    AUDITED = {"content_index.py", "memory_consolidation.py", "grounding.py",
               "customer_memory.py", "memory_assurance.py", "deploy_state.py",
               "retention.py", "data_sources.py", "verification_policy.py"}
    offenders = []
    for f in sorted(pathlib.Path("app/core").glob("*.py")):
        if f.name not in AUDITED:
            continue
        src = f.read_text(encoding="utf-8")
        lines = src.splitlines()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            span = "\n".join(lines[node.lineno - 1:
                                   getattr(node, "end_lineno", node.lineno)])
            if not any(w in span for w in WRITE):
                continue
            for h in node.handlers:
                mod = ast.Module(body=h.body, type_ignores=[])
                loud = any(isinstance(n, ast.Attribute)
                           and n.attr in ("warning", "error", "exception", "critical")
                           for n in ast.walk(mod))
                raises = any(isinstance(n, ast.Raise) for n in ast.walk(mod))
                returns = any(isinstance(n, ast.Return) for n in ast.walk(mod))
                if not (loud or raises or returns):
                    offenders.append(f"{f.name}:{h.lineno}")
    assert not offenders, (
        "write-path handlers that neither log at warning+, re-raise, nor "
        "return an error: " + ", ".join(offenders))


# ===========================================================================
# 5. Shadow-mode review roster
# ===========================================================================

def test_roster_shards_are_disjoint(conn):
    """Two reviewers working simultaneously must never collide on one item."""
    saved = os.environ.get("SHADOW_REVIEWERS")
    os.environ["SHADOW_REVIEWERS"] = "alice,bob"
    import importlib
    m = importlib.reload(MA)
    try:
        m.ensure_tables()
        for i in range(6):
            m.record_utterance(f"probe {i}", audience="customer", channel="pytest")
        a = {i["utterance_id"] for i in m.review_queue("alice", 20)["queue"]}
        b = {i["utterance_id"] for i in m.review_queue("bob", 20)["queue"]}
        assert not (a & b), "reviewer shards overlap"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_utterances WHERE channel='pytest'")
        if saved is None:
            os.environ.pop("SHADOW_REVIEWERS", None)
        else:
            os.environ["SHADOW_REVIEWERS"] = saved
        importlib.reload(MA)


def test_empty_roster_is_reported_not_implied(conn):
    """A queue with no owner must SAY so — otherwise the shadow log looks like
    it is being worked and autonomy can never be earned."""
    saved = os.environ.get("SHADOW_REVIEWERS")
    os.environ["SHADOW_REVIEWERS"] = ""
    import importlib
    m = importlib.reload(MA)
    try:
        h = m.roster_health()
        assert h["roster_size"] == 0
        assert h["warning"] and "no reviewers" in h["warning"]
    finally:
        if saved is None:
            os.environ.pop("SHADOW_REVIEWERS", None)
        else:
            os.environ["SHADOW_REVIEWERS"] = saved
        importlib.reload(MA)


def test_asserted_utterances_are_high_priority(conn):
    """An utterance that ASSERTED a memory deserves more review time than one
    that merely had memories in context."""
    import inspect
    src = inspect.getsource(MA.review_queue)
    assert 'n_assert' in src and '"high"' in src


def test_days_to_clear_is_none_when_nothing_is_reviewed(conn):
    """A roster that exists on paper and reviews nothing must not report a
    finite burn-down."""
    h = MA.roster_health()
    if h.get("reviews_per_day_observed") == 0:
        assert h.get("days_to_clear_at_observed_rate") is None
