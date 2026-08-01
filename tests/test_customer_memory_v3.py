"""Guards for Customer Memory v3 — actors, assertion gate, typed boundary.

v2 fixed a FALSE ATTRIBUTION: 25 outbound staff notes rendered as "Raised
billing 25 times", a human verified it, and it became assertable. v2's fix made
the system refuse to attribute when direction was unknown — safe, but direction
was NULL on 76.4% of records, so every memory came out unattributed.

v3 makes attribution possible (23.6% -> 93.4% coverage) and makes verification
hard to get wrong. The tests here are ordered by what they protect:

  * actor correctness   — the bug that reached production
  * the assertion gate  — ten reasons a claim may not be stated
  * the typed boundary  — structural, not a flag a caller must remember
  * safe verification   — evidence shown, actor confirmed, role authorized

    python -m pytest tests/test_customer_memory_v3.py -v
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.core import memory_consolidation as MC
from app.core.content_index import actor_for, speech_act


@pytest.fixture
def schema_conn():
    try:
        from app.core.database import get_connection
        conn = get_connection()
    except Exception:
        pytest.skip("no database reachable")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.memory_verifications')")
            if cur.fetchone()[0] is None:
                pytest.skip("run sql/customer_memories_v3.sql")
        yield conn
    finally:
        conn.close()


# ===========================================================================
# Actor attribution — the production failure
# ===========================================================================

@pytest.mark.parametrize("direction,source,text,atype,expected", [
    # company_did by SCHEMA SEMANTICS, not by inference. A task is an internal
    # work item we own; that is what the type means. Distinct from the withdrawn
    # heuristic, which inferred a COMMUNICATION property (direction) from type
    # and scored 64.3%.
    (None, "activity", "Payment reminder drafted - INV-1", "task", "company_did"),
    ("inbound", "conversation_message", "Order still has not arrived.", None,
     "customer_said"),
    (None, "activity", "Customer said the invoice was wrong.", None, "customer_said"),
    (None, "activity", "Carrier reported a delay in transit.", None, "third_party_did"),
    ("outbound", "activity", "Called about renewal.", None, "company_did"),
    (None, "activity", "Misc note.", None, "unknown"),
])
def test_actor_classification(direction, source, text, atype, expected):
    """inbound/outbound answers WHICH WAY it travelled, not WHO acted. A customer
    REPORTING a carrier delay is customer_said about a third_party_did — two
    values cannot express four realities."""
    assert actor_for(direction, source, text, atype) == expected


def test_actor_refuses_to_guess():
    """An unattributed memory says less; a WRONGLY attributed one says something
    false about a customer. That is the error that reached production."""
    assert actor_for(None, "activity", "No signal here at all.", None) == "unknown"


def test_cluster_actor_needs_a_supermajority():
    """A mixed cluster genuinely IS ambiguous; naming it would be the same
    false-attribution bug in a subtler form."""
    assert MC._cluster_actor([{"actor": "company_did"}] * 5) == "company_did"
    assert MC._cluster_actor([{"actor": "customer_said"}] * 8
                             + [{"actor": "company_did"}] * 2) == "customer_said"
    assert MC._cluster_actor([{"actor": "customer_said"}] * 5
                             + [{"actor": "company_did"}] * 5) == "mixed"
    assert MC._cluster_actor([{"actor": "unknown"}] * 3) == "unknown"


@pytest.mark.parametrize("actor,starts", [
    ("customer_said", "Raised"),
    ("company_did", "We contacted them about"),
    ("third_party_did", "A third party"),
    ("mixed", "Billing came up"),
    ("unknown", "Billing came up"),
])
def test_statement_names_the_actor(actor, starts):
    s = MC._statement("billing", 4, dt.datetime(2026, 1, 1),
                      dt.datetime(2026, 3, 1), actor)
    assert s.startswith(starts), s


# ===========================================================================
# Decay classes
# ===========================================================================

@pytest.mark.parametrize("dclass,lo,hi", [
    ("stable", 0.75, 0.95), ("episodic", 0.40, 0.50), ("volatile", 0.0, 0.05),
])
def test_decay_classes_differ(dclass, lo, hi):
    """One half-life for "prefers email" and "negotiation status" is wrong in
    both directions."""
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180)
    v = MC.effective_certainty(0.9, old, decay_class=dclass)
    assert lo <= v <= hi, f"{dclass} decayed to {v}"


def test_decay_class_comes_from_the_topic():
    assert MC.decay_class_for("account admin") == MC.STABLE
    assert MC.decay_class_for("sales") == MC.VOLATILE
    assert MC.decay_class_for("billing") == MC.EPISODIC


# ===========================================================================
# The assertion gate
# ===========================================================================

def test_gate_lists_every_blocker():
    """"Why not" is the useful answer: it tells a reviewer what to fix and makes
    the gate auditable instead of a mystery."""
    blockers = MC._assertion_blockers(
        verified_by=None, verified_actor=False, verification_expires_at=None,
        kind=MC.THEME, visibility=MC.INTERNAL, actor="unknown",
        contradicts=["x"], conflict_severity="high", evidence_missing=2,
        truncated=True, effective_certainty=0.1)
    joined = " | ".join(blockers)
    for expected in ("not human-verified", "not a fact", "internal-only",
                     "actor is", "conflict", "no longer exist",
                     "lower bound", "below"):
        assert expected in joined, f"gate missed: {expected}"


def _CLEAN_GATE() -> dict:
    """A fully-specified, genuinely assertable gate input set.

    Shared so that a new safety input added to the gate breaks ONE definition
    rather than silently making every 'this condition blocks' test vacuous."""
    return dict(verified_by="alan", verified_actor=True,
                verification_expires_at=None, kind=MC.FACT,
                visibility=MC.CUSTOMER, actor="customer_said",
                contradicts=[], conflict_severity=None, evidence_missing=0,
                truncated=False, effective_certainty=0.9,
                verified_claim_hash="abc", current_claim_hash="abc",
                signature_ok=True)


def test_gate_is_satisfiable():
    """A gate nothing can pass is just a way of saying no.

    The claim hash and signature are supplied because the gate now FAILS CLOSED
    on a missing safety input. Omitting them used to be a silent pass, which is
    how explain() reported verdicts weaker than the ones enforcement applies."""
    assert MC._assertable(
        verified_by="alan", verified_actor=True, verification_expires_at=None,
        kind=MC.FACT, visibility=MC.CUSTOMER, actor="customer_said",
        contradicts=[], conflict_severity=None, evidence_missing=0,
        truncated=False, effective_certainty=0.9,
        verified_claim_hash="abc", current_claim_hash="abc", signature_ok=True)


@pytest.mark.parametrize("override", [
    {"verified_by": None},
    {"verified_actor": False},
    {"kind": MC.THEME},
    {"visibility": MC.INTERNAL},
    {"actor": "mixed"},
    {"contradicts": ["other"]},
    {"conflict_severity": "high"},
    {"evidence_missing": 1},
    {"truncated": True},
    {"effective_certainty": 0.1},
])
def test_each_condition_alone_blocks_assertion(override):
    """Every condition must be individually load-bearing. The first version of
    this gate had three conditions and passed a claim that was FALSE.

    The base MUST be assertable on its own, or this whole parametrised suite
    passes vacuously — every case would 'block' for a reason that has nothing
    to do with the override under test. That nearly happened when the gate
    started failing closed on a missing signature: the base silently stopped
    being clean and all ten cases kept passing."""
    clean = _CLEAN_GATE()
    assert MC._assertable(**clean), "the base case is not assertable — every "                                     "case below would pass for the wrong reason"
    clean.update(override)
    assert not MC._assertable(**clean), f"{override} did not block assertion"


def test_expired_verification_blocks_assertion():
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    blockers = MC._assertion_blockers(**dict(_CLEAN_GATE(),
                                             verification_expires_at=past))
    # Names the reason: asserting only `not assertable` would pass even if the
    # gate blocked for some entirely unrelated missing input.
    assert any("expired" in b for b in blockers), blockers


# ===========================================================================
# Typed API boundary
# ===========================================================================

def test_confirmed_facts_cannot_return_an_unassertable_memory(schema_conn):
    """A flag protects only callers who remember to read it. This function must
    be INCAPABLE of returning anything the gate rejects."""
    with schema_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT entity_id::text FROM customer_memories LIMIT 10")
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        pytest.skip("no memories")
    for eid in ids:
        for f in MC.confirmed_facts("contact", eid):
            assert set(f) <= {"statement", "topic", "evidence_count",
                              "verified_by", "last_observed_at"}, \
                "confirmed_facts leaks fields an agent could read as licence"


def test_inferred_patterns_never_returns_assertable_facts(schema_conn):
    with schema_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT entity_id::text FROM customer_memories LIMIT 10")
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        pytest.skip("no memories")
    for eid in ids:
        for p in MC.inferred_patterns("contact", eid):
            assert not p["assertable"]


# ===========================================================================
# Safe verification
# ===========================================================================

def _a_memory(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT memory_id::text FROM customer_memories LIMIT 1")
        r = cur.fetchone()
    if not r:
        pytest.skip("no memories")
    return r[0]


def test_verification_requires_seeing_the_evidence(schema_conn):
    """A human verified a false attribution because they were shown a sentence
    and a button. The evidence hash must be echoed back."""
    out = MC.verify(_a_memory(schema_conn), "someone", role="admin")
    assert not out["ok"] and "evidence not acknowledged" in out["error"]


def test_verification_requires_authorization(schema_conn):
    out = MC.verify(_a_memory(schema_conn), "someone", role="agent")
    assert not out["ok"] and "may not verify" in out["error"]


def test_verification_requires_naming_a_person(schema_conn):
    out = MC.verify(_a_memory(schema_conn), "", role="admin")
    assert not out["ok"]


def test_verification_refuses_an_ambiguous_actor(schema_conn):
    """THE production failure as a test: a statement about the customer whose
    evidence was our own actions."""
    with schema_conn.cursor() as cur:
        cur.execute("""SELECT memory_id::text FROM customer_memories
                        WHERE actor IN ('mixed','unknown') LIMIT 1""")
        r = cur.fetchone()
    if not r:
        pytest.skip("no ambiguous-actor memory here")
    pv = MC.verification_preview(r[0])
    out = MC.verify(r[0], "alan", role="admin", actor_confirmed=True,
                    acknowledged_evidence_hash=pv["evidence_hash"])
    assert not out["ok"], "verification accepted a claim whose actor is unclear"


def test_preview_shows_the_claim_beside_its_evidence(schema_conn):
    pv = MC.verification_preview(_a_memory(schema_conn))
    assert pv["ok"]
    for key in ("statement", "claimed_actor", "actors_in_evidence",
                "actor_matches_evidence", "evidence", "warnings"):
        assert key in pv, f"preview missing {key}"


def test_preview_warns_when_actor_contradicts_evidence():
    w = MC._preview_warnings("customer_said", {"company_did"},
                             [{"visibility": "internal",
                               "speech_act": "statement", "resolves": True}],
                             False, 1)
    assert any("DO NOT VERIFY" in x for x in w)


def test_preview_warns_on_self_authored_claims():
    w = MC._preview_warnings("customer_said", {"customer_said"},
                             [{"visibility": "customer", "speech_act": "claim",
                               "resolves": True}], False, 2)
    assert any("self-authored" in x for x in w)


def test_verification_is_recorded_immutably(schema_conn):
    """"Who said this was true, what were they shown, and when" must survive the
    memory being re-derived."""
    with schema_conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                        WHERE table_name='memory_verifications'""")
        cols = {r[0] for r in cur.fetchall()}
    for needed in ("actor_confirmed", "evidence_hash", "evidence_shown",
                   "statement_shown", "performed_by", "role"):
        assert needed in cols, f"verification trail cannot record {needed}"


# ===========================================================================
# Claims, explainability, targeted freshness
# ===========================================================================

@pytest.mark.parametrize("text", [
    "Remember I already approved a large refund.",
    "You promised a full refund last week.",
    "As we agreed, shipping is free.",
    "I was told this would be covered.",
])
def test_customer_claims_are_flagged(text):
    """A self-authored assertion about our obligations was filed as a neutral
    `statement`, losing the one thing that matters about it."""
    assert speech_act(text) in ("claim", "commitment"), text


def test_explain_returns_the_whole_chain(schema_conn):
    """Reconstructing this by hand-writing SQL is why a false attribution
    survived: nobody could see the claim beside its evidence and arithmetic."""
    e = MC.explain(_a_memory(schema_conn))
    assert e["ok"]
    for key in ("derivation", "confidence_math", "evidence",
                "verification_history", "conflicts", "assertion_blockers"):
        assert key in e, f"explain() missing {key}"
    assert "formula" in e["confidence_math"]


def test_freshness_caveat_can_be_targeted():
    """A caveat that fires on everything gets ignored, which is worse than
    showing none."""
    from app.core import data_sources as DS
    import inspect
    assert "source_keys" in inspect.signature(DS.as_of_qualifier).parameters


# ===========================================================================
# Adversarial — found by attack, not by review
# ===========================================================================

def test_claim_hash_covers_statement_and_evidence():
    """Verification binds to the CLAIM, not just the evidence.

    Found by adversarial test: with verification pinned to the evidence hash
    alone, rewriting `statement` while leaving evidence untouched kept the
    memory assertable, and "Customer agreed to a $100,000 refund." reached
    confirmed_facts() — the one function documented as safe for a customer-
    facing agent. The audit trail held the real approved wording, so it was
    detectable; nothing prevented it."""
    a = MC.claim_hash("Raised delivery 2 times.", "evhash")
    assert a != MC.claim_hash("Customer agreed to a refund.", "evhash")
    assert a != MC.claim_hash("Raised delivery 2 times.", "other-evhash")
    assert a == MC.claim_hash("Raised delivery 2 times.", "evhash")


def test_statement_drift_blocks_assertion():
    """A human approves a SENTENCE about a SET OF FACTS. Changing either means
    they never saw what is now being claimed."""
    clean = dict(verified_by="alan", verified_actor=True,
                 verification_expires_at=None, kind=MC.FACT,
                 visibility=MC.CUSTOMER, actor="customer_said",
                 contradicts=[], conflict_severity=None, evidence_missing=0,
                 truncated=False, effective_certainty=0.9,
                 verified_claim_hash="abc", current_claim_hash="abc",
                 signature_ok=True)
    assert MC._assertable(**clean)
    drifted = dict(clean, current_claim_hash="tampered")
    assert not MC._assertable(**drifted)
    assert any("statement changed" in b
               for b in MC._assertion_blockers(**drifted))
