"""Guards for the panel's seven prioritized remediations.

Each test exists because a specific attack succeeded or a specific claim was
false. Ordered by the panel's priority.

    python -m pytest tests/test_panel_remediations.py -v
"""

from __future__ import annotations

import pytest

from app.core import deploy_state as DS
from app.core import memory_assurance as MA
from app.core import memory_consolidation as MC


@pytest.fixture
def conn():
    try:
        from app.core.database import get_connection
        c = get_connection()
        c.autocommit = True
    except Exception:
        pytest.skip("no database reachable")
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.memory_topic_policy')")
            if cur.fetchone()[0] is None:
                pytest.skip("run sql/memory_invariants.sql")
        yield c
    finally:
        c.close()


# ===========================================================================
# 1. DB-layer invariants — the top-ranked attack
# ===========================================================================

def test_sql_and_python_claim_hash_agree(conn):
    """One definition, two implementations is how win_rate came to mean three
    things. The trigger recomputes the hash server-side; if it disagrees with
    Python, every legitimate verification breaks or every forgery passes."""
    stmt, ev = "Raised billing 2 times.", "abc123"
    with conn.cursor() as cur:
        cur.execute("SELECT memory_claim_hash(%s,%s)", (stmt, ev))
        sql_hash = cur.fetchone()[0]
    assert sql_hash == MC.claim_hash(stmt, ev)


def test_direct_sql_forgery_is_blocked(conn):
    """`UPDATE customer_memories SET kind='fact', verified_by='x'` bypassed the
    entire application gate. The panel ranked this the top attack."""
    with conn.cursor() as cur:
        cur.execute("SELECT memory_id::text FROM customer_memories LIMIT 1")
        r = cur.fetchone()
        if not r:
            pytest.skip("no memories")
        mid = r[0]
        with pytest.raises(Exception):
            cur.execute("""UPDATE customer_memories
                              SET kind='fact', verified_by='mallory',
                                  verified_actor=true
                            WHERE memory_id=%s::uuid""", (mid,))


def test_verification_trail_is_append_only(conn):
    """A forger who can rewrite the audit trail can make any forgery coherent.

    Must REFUSE, not quietly discard. This was enforced with
    `DO INSTEAD NOTHING`, which silently swallowed the statement — and swallowed
    the DELETE inside the sanctioned erasure function too, so GDPR erasure was a
    no-op for months while reporting success. Silence is the wrong failure mode
    in both directions."""
    conn.autocommit = True
    for stmt in ("DELETE FROM memory_verifications",
                 "UPDATE memory_verifications SET performed_by='x'"):
        with conn.cursor() as cur:
            with pytest.raises(Exception) as e:
                cur.execute(stmt)
            assert "append-only" in str(e.value), f"{stmt} was not refused"


def test_erasure_escape_hatch_actually_erases(conn):
    """Append-only must not defeat GDPR. One sanctioned, auditable path — and it
    has to WORK. The previous version of this test asserted only that the
    function EXISTED, which it did, while every call it made deleted nothing."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regprocedure('public.erase_memory_verifications(uuid[])')")
        assert cur.fetchone()[0] is not None
        cur.execute("""SELECT memory_id::text, entity_type, entity_id::text
                         FROM customer_memories LIMIT 1""")
        row = cur.fetchone()
        if not row:
            pytest.skip("no memories")
        mid, et, eid = row
        cur.execute("""INSERT INTO memory_verifications
              (memory_id,action,actor_confirmed,evidence_hash,evidence_shown,
               statement_shown,performed_by,role,entity_type,entity_id)
              VALUES (%s::uuid,'verified',true,'h',1,'escape hatch probe',
                      'pytest','admin',%s,%s::uuid)""", (mid, et, eid))
        cur.execute("SELECT erase_memory_verifications(ARRAY[%s]::uuid[])", (mid,))
        assert cur.fetchone()[0] >= 1, "the sanctioned erasure path deleted nothing"
        cur.execute("""SELECT count(*) FROM memory_verifications
                        WHERE memory_id=%s::uuid AND performed_by='pytest'""", (mid,))
        assert cur.fetchone()[0] == 0


def test_unsigned_verification_is_not_assertable():
    """The signature is the control a database writer cannot satisfy. With no
    key configured NOTHING is assertable — an unconfigured deployment refuses
    rather than silently trusting the database."""
    assert not MC.signature_valid("m", "claim", "alice", "forged")
    assert not MC.signature_valid("m", "claim", None, None)
    if MC._SIGNING_KEY:
        good = MC.signature_for("m", "claim", "alice")
        assert MC.signature_valid("m", "claim", "alice", good)
        assert not MC.signature_valid("m", "claim", "bob", good)


def test_signature_blocks_assertion_when_invalid():
    blockers = MC._assertion_blockers(
        verified_by="alice", verified_actor=True, verification_expires_at=None,
        kind=MC.FACT, visibility=MC.CUSTOMER, actor="customer_said",
        contradicts=[], conflict_severity=None, evidence_missing=0,
        truncated=False, effective_certainty=0.9,
        verified_claim_hash="h", current_claim_hash="h", signature_ok=False)
    assert any("signature" in b for b in blockers)


# ===========================================================================
# 2. Topic classifier hardening
# ===========================================================================

def test_catch_all_topic_requires_dual_approval(conn):
    """The classifier reads customer-authored text, so an attacker can steer a
    claim out of a named topic. 12.6% of memories already land in `general`.
    A catch-all with the weakest policy is an invitation."""
    assert MC.required_approvals_for("general") >= 2


def test_unknown_topic_fails_strict(conn):
    """Adding a topic without a policy row must fail SAFE. Python previously
    returned 1 while the trigger used 2 — the same one-rule-two-implementations
    drift this codebase has paid for three times."""
    assert MC.required_approvals_for("a-topic-nobody-defined") >= 2
    assert MC.required_approvals_for("") >= 2
    assert MC.required_approvals_for(None) >= 2


def test_topic_policy_lives_in_the_database(conn):
    """An env var can differ between replicas; a table cannot."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_topic_policy")
        assert cur.fetchone()[0] > 0


def test_python_and_sql_agree_on_approval_counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT topic, required_approvals FROM memory_topic_policy")
        for topic, required in cur.fetchall():
            assert MC.required_approvals_for(topic) == required, \
                f"policy drift on '{topic}'"


# ===========================================================================
# 3. Shadow mode
# ===========================================================================

def test_shadow_records_what_would_be_said(conn):
    MA.ensure_tables()
    assert MA.record_utterance("Test utterance.", audience="customer",
                               channel="pytest")
    with conn.cursor() as cur:
        cur.execute("SELECT shadow, text FROM agent_utterances "
                    "ORDER BY utterance_id DESC LIMIT 1")
        shadow, text = cur.fetchone()
    assert shadow is True, "utterances must default to SHADOW (send nothing)"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_utterances WHERE channel='pytest'")


def test_autonomy_is_not_ready_without_reviewed_evidence():
    """An unreviewed shadow log is not evidence. The criteria must be explicit
    and must not be satisfiable by volume alone."""
    r = MA.shadow_report(30)
    assert "criteria" in r
    if r.get("reviewed", 0) < 100:
        assert not r["autonomy_ready"]


def test_shadow_stores_pointers_not_memory_text(conn):
    """memory_ids, not statements — the statements live in customer_memories,
    which is erased with the customer."""
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                        WHERE table_name='agent_utterances'""")
        cols = {r[0] for r in cur.fetchall()}
    assert "memory_ids" in cols and "asserted_ids" in cols
    assert "memory_statements" not in cols


# ===========================================================================
# 4. Calibration
# ===========================================================================

def test_calibration_reports_thin_buckets_rather_than_guessing():
    """A bucket with three samples is noise. Reporting `calibrated: true` from
    it would be worse than reporting nothing."""
    cal = MA.calibration()
    assert cal["ok"]
    for b in cal["buckets"]:
        assert "thin" in b
    if all(b["thin"] for b in cal["buckets"]):
        assert cal["calibrated"] is None


# ===========================================================================
# 5. Migration state
# ===========================================================================

def test_migration_order_is_declared_and_checked():
    st = DS.check_migrations()
    assert st["required"], "no migration order is declared"
    assert "out_of_order" in st
    v1 = st["required"].index("customer_memories.sql")
    v2 = st["required"].index("customer_memories_v2.sql")
    assert v1 < v2, "declared order is wrong: v2 depends on v1"


def test_migration_state_is_recorded(conn):
    DS.ensure_table()
    st = DS.check_migrations()
    assert st["applied"], "nothing recorded — a bad deploy is unrecoverable"


# ===========================================================================
# 6. Config attestation
# ===========================================================================

def test_fingerprint_covers_the_safety_parameters():
    fp = DS.safety_fingerprint()
    for key in ("MEMORY_ASSERT_FLOOR", "MEMORY_VERIFY_ROLES",
                "MEMORY_DUAL_APPROVALS", "PROVENANCE_TRUST_FLOOR"):
        assert key in fp["params"], f"{key} is not attested"


def test_fingerprint_never_exposes_a_secret():
    """An attestation endpoint must not become a key oracle."""
    import os
    fp = DS.safety_fingerprint()
    assert "MEMORY_SIGNING_KEY" not in fp["params"]
    assert fp["params"].get("MEMORY_SIGNING_KEY__set") in ("0", "1")
    key = os.getenv("MEMORY_SIGNING_KEY", "")
    if key:
        assert key not in str(fp)


def test_divergent_replicas_are_detected(conn):
    """Config divergence between replicas was previously undetectable."""
    import os
    DS.attest("pytest-A")
    saved = os.environ.get("MEMORY_ASSERT_FLOOR")
    os.environ["MEMORY_ASSERT_FLOOR"] = "0.01"
    try:
        DS.attest("pytest-B")
        c = DS.consensus()
        assert not c["ok"]
        assert "MEMORY_ASSERT_FLOOR" in c["diverging_params"]
    finally:
        if saved is None:
            os.environ.pop("MEMORY_ASSERT_FLOOR", None)
        else:
            os.environ["MEMORY_ASSERT_FLOOR"] = saved
        with conn.cursor() as cur:
            cur.execute("DELETE FROM replica_attestations WHERE replica LIKE 'pytest-%'")


# ===========================================================================
# 7. Safety observability
# ===========================================================================

def test_safety_metrics_expose_the_gate_state():
    m = MA.safety_metrics(30)
    assert m["ok"]
    for key in ("active", "facts", "verified", "signed", "unattributed"):
        assert key in m["memories"], f"safety metric missing: {key}"


def test_verification_bias_is_measurable():
    """A reviewer who never rejects is not reviewing. An approval rate near
    100% is indistinguishable from rubber-stamping."""
    m = MA.safety_metrics(30)
    assert "bias_suspected" in m
    for v in m.get("verifiers", []):
        assert "approval_rate" in v and "bias_flag" in v


# ===========================================================================
# Live visibility re-check (found by the suite during remediation)
# ===========================================================================

def test_memory_visibility_is_rechecked_live(conn):
    """A memory's visibility is a snapshot taken at consolidation. content_index
    learned not to trust its cached visibility; customer_memories had not.
    Measured: 2 of 3 customer-visible memories cited evidence that was by then
    internal."""
    import inspect
    src = inspect.getsource(MC.recall)
    assert "_evidence_visibility" in src
    assert "effective_vis" in src
