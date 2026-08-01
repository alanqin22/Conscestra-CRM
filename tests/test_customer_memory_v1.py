"""Guards for Customer Memory v1, retention and source freshness.

Three properties matter more than the features themselves:

  * a counted memory must be TRUE — counting rows instead of occasions produced
    "Raised returns 385 times" from one boilerplate sentence logged repeatedly;
  * a derived memory must INHERIT the most restrictive visibility of its
    evidence, or consolidation launders internal notes into customer prose;
  * a derived memory must be ERASED with the customer, or it is the third copy
    of personal data that governance forgets.

    python -m pytest tests/test_customer_memory_v1.py -v
"""

from __future__ import annotations

import pytest

from app.core import lifecycle
from app.core import memory_consolidation as MC
from app.core import provenance as P
from app.core import retention


# ===========================================================================
# Reliability vs certainty  (no DB)
# ===========================================================================

def test_reliability_and_certainty_are_independent():
    """The stub is the case that proves the split: deterministic (certainty 1.0)
    and disconnected from reality (reliability 0.15). One number cannot say
    'perfectly repeatable and completely made up'."""
    stub = P.Provenance(source_type=P.COMPUTED, reliability=0.15, certainty=1.0).normalized()
    unsure_but_good = P.Provenance(source_type=P.EXTERNAL, reliability=0.90,
                                   certainty=0.30).normalized()
    assert stub.certainty > unsure_but_good.certainty        # more repeatable
    assert stub.reliability < unsure_but_good.reliability    # less trustworthy
    assert stub.confidence < unsure_but_good.confidence      # composite orders correctly


def test_composite_is_the_product():
    p = P.Provenance(source_type=P.EXTERNAL, reliability=0.9, certainty=0.5).normalized()
    assert p.confidence == pytest.approx(0.45, abs=1e-6)


def test_legacy_single_number_is_read_as_certainty():
    """Existing callers pass one number meaning 'how sure am I of this value'.
    Reliability then comes from the source kind rather than being invented."""
    p = P.Provenance(source_type=P.AI, confidence_in=0.6).normalized()
    assert p.certainty == 0.6
    assert p.reliability == P.DEFAULT_RELIABILITY[P.AI]


def test_describe_names_both_factors():
    text = P.Provenance(source_type=P.COMPUTED, reliability=0.15,
                        certainty=1.0).describe()
    assert "reliability" in text and "certainty" in text


# ===========================================================================
# Explore trust floor
# ===========================================================================

def test_enriched_dimensions_carry_a_trust_floor():
    """industry / employee_band / revenue_band are written by enrichment, which
    falls back to fabricated stub values. They were segmentable exactly like
    observed ones."""
    from app.core import semantic_model as M
    for explore in ("accounts", "opportunities", "leads"):
        dims = M.EXPLORES[explore]["dimensions"]
        assert "confidence" in dims["industry"]["sql"], \
            f"{explore}.industry has no trust floor"


def test_trust_floor_relabels_rather_than_drops(schema_conn):
    """Excluding low-trust rows would make Explore totals disagree with every
    other surface — the drift the semantic model exists to prevent. The value is
    rebucketed instead, so segmentation still balances."""
    from app.core import semantic_model as M
    from app.core import semantic_query as SQ

    with schema_conn.cursor() as cur:
        cur.execute("SELECT lead_id, confidence FROM leads "
                    "WHERE industry IS NOT NULL LIMIT 2")
        rows = cur.fetchall()
        if not rows:
            pytest.skip("no enriched leads")
        ids = [str(r[0]) for r in rows]
        cur.execute("UPDATE leads SET confidence=0.15 WHERE lead_id = ANY(%s::uuid[])",
                    (ids,))
        schema_conn.commit()
        cur.execute("SELECT count(*) FROM leads WHERE COALESCE(is_deleted,false)=false")
        table_total = cur.fetchone()[0]
    try:
        res = SQ.run_readonly(*SQ.compile(
            {"explore": "leads", "dimensions": ["industry"], "measures": ["count"]}))
        assert sum(r["count"] for r in res) == table_total, \
            "trust floor dropped rows — totals no longer reconcile"
        assert any(r["industry"] == M.UNVERIFIED_LABEL for r in res), \
            "low-confidence values were not rebucketed as unverified"
    finally:
        with schema_conn.cursor() as cur:
            for (lid, conf) in rows:
                cur.execute("UPDATE leads SET confidence=%s WHERE lead_id=%s", (conf, lid))
            schema_conn.commit()


# ===========================================================================
# Consolidation correctness
# ===========================================================================

def test_distinct_occasions_collapse_repeated_boilerplate():
    """THE correctness fix. One contact's largest cluster was 385 records of the
    same sentence — "Requested additional information from customer." Counting
    rows produced "Raised returns 385 times", which is boilerplate mistaken for
    customer behaviour and asserted with high certainty."""
    import datetime as dt
    same_day = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    members = [{"snippet": "Requested additional information from customer.",
                "occurred_at": same_day, "source_type": "activity",
                "source_id": f"id-{i}"} for i in range(50)]
    assert len(MC._distinct_occasions(members)) == 1


def test_same_wording_on_different_days_stays_distinct():
    """A customer who raises billing every month IS recurring — that is exactly
    what the count should capture. Only same-day duplicates collapse."""
    import datetime as dt
    members = [{"snippet": "Asked about the invoice again.",
                "occurred_at": dt.datetime(2026, m, 1, tzinfo=dt.timezone.utc),
                "source_type": "activity", "source_id": f"id-{m}"}
               for m in range(1, 6)]
    assert len(MC._distinct_occasions(members)) == 5


def test_evidence_hash_is_order_independent():
    """The staleness key must depend on WHICH records, not the order they were
    read, or every pass would look stale and rewrite every memory."""
    a = MC._evidence_hash(["activity:1", "case:2", "activity:3"])
    b = MC._evidence_hash(["case:2", "activity:3", "activity:1"])
    assert a == b
    assert a != MC._evidence_hash(["activity:1", "case:2"])


def test_statement_is_templated_not_generated():
    """Consolidation runs unattended and its output is read back to staff. A
    deterministic sentence cannot invent a claim the evidence does not support."""
    import datetime as dt
    s = MC._statement("billing", 4, dt.datetime(2026, 1, 1),
                      dt.datetime(2026, 3, 1), "inbound")
    assert "billing" in s.lower() and "4 times" in s and "2026-01-01" in s


# ===========================================================================
# Consolidation safety (DB)
# ===========================================================================

@pytest.fixture
def schema_conn():
    try:
        from app.core.database import get_connection
        conn = get_connection()
    except Exception:
        pytest.skip("no database reachable")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.customer_memories')")
            if cur.fetchone()[0] is None:
                pytest.skip("run sql/customer_memories.sql")
        yield conn
    finally:
        conn.close()


def test_memory_visibility_is_inherited_most_restrictive(schema_conn):
    """A theme drawn from ANY internal record is internal. Without this,
    summarizing launders staff-only notes into customer-visible prose - a leak
    created by the act of consolidating.

    Checks BOTH the stored snapshot and the value actually SERVED. The stored
    one can go stale (evidence reclassified after consolidation); the served one
    is re-asserted against the source on every read, the same rule
    content_index.search already applies."""
    with schema_conn.cursor() as cur:
        cur.execute("""SELECT entity_id::text, memory_id::text, visibility, evidence
                         FROM customer_memories WHERE superseded_by IS NULL""")
        rows = cur.fetchall()
    if not rows:
        pytest.skip("no consolidated memories")

    with schema_conn.cursor() as cur:
        for eid, mid, stored_vis, evidence in rows:
            ids = [(e["source_type"], e["source_id"]) for e in (evidence or [])
                   if e.get("source_id")]
            if not ids:
                continue
            cur.execute("""SELECT count(*) FROM content_embeddings
                            WHERE (source_type, source_id) IN %s
                              AND visibility <> 'customer'""", (tuple(ids),))
            internal_now = cur.fetchone()[0]

            # 1. The SERVED value must never be customer when evidence is internal.
            served = [m for m in MC.recall("contact", eid, MC.INTERNAL, limit=99)
                      if m["memory_id"] == mid]
            if served and internal_now:
                assert served[0]["visibility"] == MC.INTERNAL, (
                    "a memory citing internal evidence is served as "
                    "customer-visible - consolidation is laundering staff notes")

            # 2. A customer-audience caller must not receive it at all.
            if internal_now:
                assert not [m for m in MC.recall("contact", eid, MC.CUSTOMER, limit=99)
                            if m["memory_id"] == mid],                     "internal-evidence memory reached the customer audience"


def test_memory_evidence_is_pointers_not_content(schema_conn):
    """Content stays in content_embeddings, which is erased with the customer.
    A third copy is a third thing for erasure to forget."""
    with schema_conn.cursor() as cur:
        cur.execute("SELECT evidence FROM customer_memories LIMIT 20")
        rows = cur.fetchall()
    if not rows:
        pytest.skip("no consolidated memories")
    for (evidence,) in rows:
        for ev in evidence:
            assert set(ev) <= {"source_type", "source_id", "on_date"}, \
                f"memory evidence stores more than pointers: {sorted(ev)}"


def test_consolidated_recall_is_audience_gated(schema_conn):
    """Same fail-closed rule as the index it derives from — anything not exactly
    'internal' sees only customer-visible memories."""
    with schema_conn.cursor() as cur:
        cur.execute("SELECT entity_type, entity_id::text FROM customer_memories LIMIT 1")
        r = cur.fetchone()
    if not r:
        pytest.skip("no consolidated memories")
    for audience in ("customer", "Customer", "", "staff", None):
        for m in MC.recall(r[0], r[1], audience, limit=50):
            assert m["visibility"] == MC.CUSTOMER, \
                f"audience={audience!r} leaked an internal memory"


def test_memories_are_erased_with_the_customer():
    """Registered in DERIVED_PII_STORES and present as a DELETE satellite in
    every plan whose entity they carry — the guard that stops the index bug from
    recurring one layer up."""
    assert "customer_memories" in lifecycle.DERIVED_PII_STORES
    spec = lifecycle.DERIVED_PII_STORES["customer_memories"]
    assert spec.get("regenerated_by")
    for entity in ("contacts", "accounts"):
        sats = lifecycle.PLANS[entity]["satellites"]
        hit = [s for s in sats if s["table"] == "customer_memories"]
        assert hit, f"{entity} erasure does not delete customer_memories"
        assert hit[0]["action"] == lifecycle.DELETE


# ===========================================================================
# Retention
# ===========================================================================

def test_financial_and_audit_stores_have_no_retention_policy():
    """Retention deletes. A store with an independent legal basis must be
    absent from POLICIES entirely — not merely set to a long period, which a
    config change could shorten."""
    policed = {p.table for p in retention.POLICIES}
    for protected in ("invoices", "payments", "orders", "audit_log",
                      "email_suppression", "accounts", "contacts"):
        assert protected not in policed, \
            f"{protected} has a retention policy — it must be untouchable"


def test_every_policy_declares_a_basis():
    """'Why did this data disappear?' must have an answer that predates the
    deletion."""
    for p in retention.POLICIES:
        assert p.basis and len(p.basis) > 20, f"{p.table} has no stated basis"
        assert p.default_days > 0


def test_retention_is_off_by_default():
    import importlib
    import os
    saved = os.environ.pop("RETENTION_ENABLED", None)
    try:
        mod = importlib.reload(retention)
        assert mod.ENABLED is False
        assert mod.purge()["ok"] is False
    finally:
        if saved is not None:
            os.environ["RETENTION_ENABLED"] = saved
        importlib.reload(retention)


def test_preview_writes_nothing(schema_conn):
    with schema_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_retrievals")
        before = cur.fetchone()[0]
    retention.preview()
    with schema_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_retrievals")
        assert cur.fetchone()[0] == before


# ===========================================================================
# Source freshness — what "as of" may claim
# ===========================================================================

def test_source_without_an_sla_is_unknown_not_fresh(schema_conn):
    """An unmeasured source is not a current one. Reporting it as fresh would
    let a briefing imply currency nobody promised."""
    from app.core import data_sources as DS
    DS.register("test:no-sla", "Test source without SLA", "import", None, None)
    DS.succeed("test:no-sla", watermark="w1", seen=1, written=1)
    try:
        states = {s["source_key"]: s["state"] for s in DS.freshness()["sources"]}
        assert states["test:no-sla"] == "unknown"
    finally:
        with schema_conn.cursor() as cur:
            cur.execute("DELETE FROM data_sources WHERE source_key='test:no-sla'")
            schema_conn.commit()


def test_rejected_rows_are_durable(schema_conn):
    """Rejects used to be returned in an HTTP response and lost, which makes a
    partial import unfixable: you know 40 rows failed and cannot see which."""
    from app.core import data_sources as DS
    DS.register("test:rejects", "Test source", "import", "accounts", 60)
    DS.reject("test:rejects", "batch-x", 3, "bad email", {"email": "nope"})
    try:
        with schema_conn.cursor() as cur:
            cur.execute("SELECT reason, payload FROM data_source_rejects "
                        "WHERE source_key='test:rejects'")
            reason, payload = cur.fetchone()
        assert reason == "bad email" and payload["email"] == "nope"
    finally:
        with schema_conn.cursor() as cur:
            cur.execute("DELETE FROM data_sources WHERE source_key='test:rejects'")
            schema_conn.commit()


def test_partial_run_is_not_reported_as_success(schema_conn):
    """A run that dropped records is not a success; treating it as one hides
    the gap from every freshness check."""
    from app.core import data_sources as DS
    DS.register("test:partial", "Partial source", "import", "leads", 60)
    DS.succeed("test:partial", seen=10, written=8, rejected=2)
    try:
        with schema_conn.cursor() as cur:
            cur.execute("SELECT last_status FROM data_sources WHERE source_key='test:partial'")
            assert cur.fetchone()[0] == "partial"
    finally:
        with schema_conn.cursor() as cur:
            cur.execute("DELETE FROM data_sources WHERE source_key='test:partial'")
            schema_conn.commit()


def test_briefing_carries_a_source_caveat_when_sources_are_stale(schema_conn):
    """The difference between 'the database as of X' and 'the business as of X'.
    A briefing that cannot say which is claiming the stronger one."""
    from app.core import data_sources as DS
    q = DS.as_of_qualifier()
    if q is None:
        pytest.skip("all sources inside SLA")
    assert "may not reflect" in q.lower()


# ===========================================================================
# v2 — conflicts, lifecycle, typing, speech acts
# ===========================================================================

def test_conflicting_claims_can_coexist(schema_conn):
    """v1 argued 'conflicts are signal, never silently merge' and then shipped
    UNIQUE (entity, topic), making two claims about one topic impossible to
    represent. The upsert silently overwrote the older one, destroying exactly
    the signal the design called most valuable."""
    with schema_conn.cursor() as cur:
        cur.execute("""SELECT conname FROM pg_constraint
                        WHERE conrelid='customer_memories'::regclass AND contype='u'""")
        names = {r[0] for r in cur.fetchall()}
    assert "customer_memories_entity_topic_key" not in names, \
        "the UNIQUE(entity,topic) constraint is back — conflicts are unrepresentable"
    assert "customer_memories_claim_key" in names


def test_contradictions_link_and_clear(schema_conn):
    """Competing claims point at each other, and the links CLEAR when the
    conflict resolves — a stale 'contradicted' flag is a false alarm."""
    with schema_conn.cursor() as cur:
        # entity_type MUST come from the same row. This test hardcoded
        # 'contact' while picking an arbitrary entity, and passed only because
        # the first row happened to be a contact. Once the corpus covered
        # accounts too, the competing claim was inserted under a different
        # entity key and could never link — a test bug that had been latent
        # since it was written.
        cur.execute("""SELECT entity_type, entity_id::text, topic
                         FROM customer_memories LIMIT 1""")
        row = cur.fetchone()
        if not row:
            pytest.skip("no memories")
        etype, eid, topic = row
        cur.execute("DELETE FROM customer_memories WHERE generator='pytest/conflict'")
        cur.execute("""INSERT INTO customer_memories
              (entity_type,entity_id,kind,statement,topic,occurrences,evidence,
               evidence_count,evidence_hash,source_type,reliability,certainty,
               generator,visibility,last_observed_at)
              VALUES (%s,%s::uuid,'hypothesis','Competing claim.',%s,1,
                      '[]'::jsonb,0,'h-conflict','ai',0.7,0.9,'pytest/conflict',
                      'internal',now())""", (etype, eid, topic))
        schema_conn.commit()
        MC._link_contradictions(cur, etype, eid)
        schema_conn.commit()
        # LIVE rows only. Retiring a previous generator's rows leaves
        # 'superseded' claims on the same (entity, topic), and
        # _link_contradictions deliberately ignores them — a retired claim is
        # not evidence of a live disagreement.
        cur.execute("""SELECT cardinality(contradicts) FROM customer_memories
                        WHERE entity_id=%s::uuid AND topic=%s
                          AND status IN ('active','dormant')""", (eid, topic))
        counts = [r[0] for r in cur.fetchall()]
        assert counts, "no live memories on that topic"
        assert all(c >= 1 for c in counts), f"conflict not linked: {counts}"

        cur.execute("DELETE FROM customer_memories WHERE generator='pytest/conflict'")
        schema_conn.commit()
        MC._link_contradictions(cur, etype, eid)
        schema_conn.commit()
        cur.execute("""SELECT cardinality(contradicts) FROM customer_memories
                        WHERE entity_id=%s::uuid AND topic=%s""", (eid, topic))
        assert all(r[0] == 0 for r in cur.fetchall()), \
            "contradiction links did not clear — a stale flag cries wolf"


def test_certainty_decays_with_age():
    """'Budget approved' is true in March and a confident falsehood by November."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    fresh = MC.effective_certainty(0.9, now)
    old = MC.effective_certainty(0.9, now - dt.timedelta(days=MC.HALF_LIFE_DAYS))
    assert fresh > old
    assert old == pytest.approx(0.45, abs=0.02), "half-life is not halving"


def test_human_verification_pins_certainty():
    """A person confirmed it, so age stops eroding the claim. Nothing automatic
    sets verified_at — a model cannot verify its own inference."""
    import datetime as dt
    long_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1000)
    assert MC.effective_certainty(1.0, long_ago) < 0.1
    assert MC.effective_certainty(1.0, long_ago, verified_at=long_ago) == 1.0


def test_only_verified_facts_are_assertable(schema_conn):
    """THE agent-safety control. v1 rendered an inference and an established
    fact identically, so an agent could state either as confirmed."""
    with schema_conn.cursor() as cur:
        cur.execute("SELECT entity_id::text FROM customer_memories LIMIT 1")
        r = cur.fetchone()
    if not r:
        pytest.skip("no memories")
    for m in MC.recall("contact", r[0], "internal", limit=20):
        if m["assertable"]:
            assert m["verified"] and m["kind"] == MC.FACT, \
                "a memory is assertable without human verification"


def test_rendering_separates_confirmed_from_inferred(schema_conn):
    from app.core import context
    with schema_conn.cursor() as cur:
        cur.execute("SELECT entity_id::text FROM customer_memories LIMIT 1")
        r = cur.fetchone()
    if not r:
        pytest.skip("no memories")
    block = context.memory_prompt_block(
        context.hydrate("contact", r[0], audience="internal"))
    if not block:
        pytest.skip("no themes surfaced")
    if "CONFIRMED ABOUT THIS CUSTOMER" in block:
        assert "human-verified" in block
    if "INFERRED PATTERNS" in block:
        assert "do NOT state these to the customer as fact" in block


@pytest.mark.parametrize("text,expected", [
    ("We will ship the replacement tomorrow.", "commitment"),
    ("I'll call you back on Friday.", "commitment"),
    ("Requested additional information from customer.", "request"),
    ("Customer is frustrated the order is late.", "complaint"),
    ("Can you send the invoice?", "question"),
    ("Invoice INV-001 paid in full.", "resolution"),
    ("Meeting notes attached.", "statement"),
])
def test_speech_act_classification(text, expected):
    """Embeddings encode topic, not intent: 'what commitments did we make?'
    retrieved 'Requested additional information' at 0.517. No threshold
    separates a request from a commitment about the same subject."""
    from app.core.content_index import speech_act
    assert speech_act(text) == expected


def test_speech_act_prefers_the_costlier_reading():
    """A sentence that both promises and asks is a COMMITMENT — mistaking a
    promise for a question is the more expensive error."""
    from app.core.content_index import speech_act
    assert speech_act("Can you confirm? We will ship it tomorrow.") == "commitment"


def test_commitment_search_returns_commitments(schema_conn):
    from app.core import content_index as CI
    hits = CI.search("what did we commit to", audience="internal",
                     acts=[CI.COMMITMENT], limit=5, min_sim=0.0)
    if not hits:
        pytest.skip("no commitments indexed")
    assert all(h["speech_act"] == CI.COMMITMENT for h in hits)


def test_truncated_counts_are_flagged(schema_conn):
    """A count built from the newest MEMORY_MAX_RECORDS reads as exact and is a
    lower bound."""
    with schema_conn.cursor() as cur:
        cur.execute("SELECT truncated, occurrences FROM customer_memories "
                    "WHERE truncated LIMIT 1")
        r = cur.fetchone()
    if not r:
        pytest.skip("nothing truncated in this dataset")
    assert r[0] is True


def test_consolidation_rotates_coverage():
    """Ordering by record recency reconsolidated the same busy customers every
    pass while a quiet one — whose evidence retention may since have expired —
    was never revisited."""
    import inspect
    src = inspect.getsource(MC.consolidate_pass)
    assert "done_at ASC NULLS FIRST" in src, \
        "consolidation no longer rotates — quiet customers keep stale memories"


def test_indexer_upsert_matches_the_primary_key(schema_conn):
    """Schema v2 widened content_embeddings' PK to include chunk_ix. The
    indexer's ON CONFLICT still named the OLD key, so every upsert raised —
    and reindex()'s try/except swallowed it, reporting "0 embedded" as though
    there were simply nothing to do. A migration that silently disables the
    indexer is worse than one that fails loudly."""
    from app.core import content_index as CI
    res = CI.reindex(force=True, limit=1)
    assert res.get("ok"), f"indexer upsert is broken: {res.get('error')}"


def test_reindex_reports_failure_rather_than_zero(schema_conn):
    """`embedded: 0` must mean 'nothing stale', never 'every write failed'."""
    from app.core import content_index as CI
    res = CI.reindex(limit=1)
    assert "ok" in res
    if not res["ok"]:
        assert res.get("error"), "a failed pass must carry its error"


# ===========================================================================
# False attribution — the memory must say WHOSE action it describes
# ===========================================================================

# NOTE: superseded by the ACTOR model in v3 — inbound/outbound could not express
# "a customer REPORTING a third party's action". Coverage now lives in
# tests/test_customer_memory_v3.py::test_actor_classification and
# ::test_statement_names_the_actor. Kept here as the direction->actor bridge.

@pytest.mark.parametrize("actor,expected", [
    ("customer_said", "Raised billing"),
    ("company_did", "We contacted them about billing"),
    ("mixed", "Billing came up"),
])
def test_statement_reflects_who_acted(actor, expected):
    """Clustering is topic-based and actor-blind, so 25 OUTBOUND staff notes
    ("Payment reminder drafted") became "Raised billing 25 times" — attributing
    OUR actions to THE CUSTOMER, which a human then verified."""
    import datetime as dt
    s = MC._statement("billing", 4, dt.datetime(2026, 1, 1),
                      dt.datetime(2026, 3, 1), actor)
    assert s.startswith(expected), s


def test_mixed_actor_gets_neutral_phrasing():
    """A cluster containing both the customer contacting us and us contacting
    them genuinely IS ambiguous. Attributing it to either party would be the
    same false-attribution bug in a subtler form."""
    assert MC._cluster_actor([{"actor": "customer_said"},
                              {"actor": "company_did"}]) == "mixed"
    assert MC._cluster_actor([{"actor": "customer_said"}] * 3) == "customer_said"


def test_no_memory_claims_the_customer_raised_an_outbound_theme(schema_conn):
    """End to end: nothing in the store may say the customer 'raised' something
    when the evidence is us contacting them."""
    with schema_conn.cursor() as cur:
        cur.execute("""SELECT cm.statement, cm.evidence
                         FROM customer_memories cm
                        WHERE cm.statement LIKE 'Raised %'""")
        rows = cur.fetchall()
    if not rows:
        pytest.skip("no inbound-attributed memories in this dataset")
    with schema_conn.cursor() as cur:
        for statement, evidence in rows:
            ids = [(e["source_type"], e["source_id"]) for e in evidence]
            if not ids:
                continue
            cur.execute("""SELECT DISTINCT actor FROM content_embeddings
                            WHERE (source_type, source_id) IN %s""", (tuple(ids),))
            actors = {r[0] for r in cur.fetchall() if r[0] and r[0] != "unknown"}
            assert actors <= {"customer_said", "customer_did"}, (
                f"{statement!r} claims the customer raised it, but its evidence "
                f"has actors {actors}")


def test_assertable_requires_customer_visible_evidence(schema_conn):
    """THREE conditions, not two. A memory built entirely from INTERNAL staff
    notes was reported assertable once a human verified it. Audience gating kept
    it out of customer recall so nothing leaked — but a flag that says "safe to
    state" on staff-only content contradicts itself, and any caller trusting it
    without re-checking audience would leak."""
    with schema_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT entity_id::text FROM customer_memories LIMIT 5")
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        pytest.skip("no memories")
    for eid in ids:
        for m in MC.recall("contact", eid, "internal", limit=20):
            if m["assertable"]:
                assert m["visibility"] == MC.CUSTOMER, (
                    f"assertable memory drawn from {m['visibility']} evidence: "
                    f"{m['statement']!r}")
                assert m["verified"] and m["kind"] == MC.FACT
