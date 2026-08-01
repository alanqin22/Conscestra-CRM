"""Guards for the semantic index over CRM unstructured text (audit finding #2).

The highest-value test in this file is the LEAK guard. This index deliberately
contains internal case comments and internal conversations alongside
customer-facing text, so the only thing making that safe is that retrieval is
audience-gated and fail-closed. If `test_customer_audience_never_sees_internal`
ever fails, the U1 reach_invariant is re-broken and internal notes are reachable
from a customer channel.

The rest guard the two silent-degradation modes: a model/dimension change
producing confident nonsense (finding #3), and templated CRM text crowding out
distinct answers.

DB-dependent tests skip cleanly; the pure-logic ones always run.

    python -m pytest tests/test_content_index.py -v
"""

from __future__ import annotations

import pytest

from app.core import content_index as CI
from app.core import embeddings as E


# ===========================================================================
# Storage format + staleness identity  (no DB, no API)
# ===========================================================================

def test_encode_decode_roundtrip():
    vec = [0.5, -0.25, 0.125, 0.0]
    out = E.decode(E.encode(vec), expect_dims=4)
    assert out == pytest.approx(vec, abs=1e-6)
    assert len(E.encode(vec)) == 4 * 4          # float32: 4 bytes/dim


def test_decode_refuses_a_different_geometry():
    """A vector of the wrong width is not comparable. Returning None (a miss)
    is correct; coercing it would produce a confident wrong neighbour."""
    blob = E.encode([1.0] * 8)
    assert E.decode(blob, expect_dims=8) is not None
    assert E.decode(blob, expect_dims=512) is None


def test_index_key_includes_model_and_dims():
    """Finding #3: staleness keyed on the content hash ALONE meant changing
    EMBED_MODEL left every stored vector in the old embedding space, compared
    against new-space queries, with no error and no log."""
    a = E.index_key("hello", model="text-embedding-3-small", dims=512)
    assert a != E.index_key("hello", model="text-embedding-3-large", dims=512)
    assert a != E.index_key("hello", model="text-embedding-3-small", dims=1536)
    assert a == E.index_key("hello", model="text-embedding-3-small", dims=512)
    assert a != E.index_key("goodbye", model="text-embedding-3-small", dims=512)


def test_rank_drops_mismatched_vectors_instead_of_comparing_them():
    """Mixed-geometry candidates must be excluded, not silently truncated."""
    q = [1.0, 0.0, 0.0, 0.0]
    good = ("ok", E.encode([1.0, 0.0, 0.0, 0.0]), 4)
    wrong = ("bad", E.encode([1.0] * 8), 8)
    out = E.rank(q, [good, wrong], limit=5)
    assert [k for k, _ in out] == ["ok"]


def test_rank_orders_by_cosine():
    q = [1.0, 0.0]
    cands = [("far", E.encode([0.0, 1.0]), 2),
             ("near", E.encode([0.9, 0.1]), 2),
             ("mid", E.encode([0.6, 0.6]), 2)]
    assert [k for k, _ in E.rank(q, cands, limit=3)] == ["near", "mid", "far"]


def test_rank_applies_min_sim():
    q = [1.0, 0.0]
    cands = [("orthogonal", E.encode([0.0, 1.0]), 2)]
    assert E.rank(q, cands, limit=3, min_sim=0.5) == []


# ===========================================================================
# Near-duplicate suppression
# ===========================================================================

def test_template_fingerprint_collapses_generated_records():
    """CRM text is mass-produced. Without normalizing the embedded ids, 400
    'Order shipped' notes read as 400 distinct records and flood every result
    set — observed on the live corpus before this was added."""
    a = "Order shipped - follow up with customer — Order SO-2026-100518 has been shipped."
    b = "Order shipped - follow up with customer — Order SO-2026-100353 has been shipped."
    assert CI.template_fingerprint(a) == CI.template_fingerprint(b)


def test_template_fingerprint_ignores_punctuation_variants():
    """The same template exists with '-' and with '—'; those are one template."""
    a = "Payment reminder (urgent) drafted - INV-000172"
    b = "Payment reminder (urgent) drafted — INV-000172"
    assert CI.template_fingerprint(a) == CI.template_fingerprint(b)


def test_template_fingerprint_keeps_genuinely_different_text_apart():
    assert (CI.template_fingerprint("Customer asked about annual pricing tiers")
            != CI.template_fingerprint("Shipment damaged in transit, refund issued"))


# ===========================================================================
# Source declarations
# ===========================================================================

def test_every_source_declares_a_visibility():
    """A source that forgets to classify itself would default to the table's
    restrictive 'internal', but silently — so require it explicitly."""
    for name, spec in CI.SOURCES.items():
        sql = spec["sql"]
        assert "'internal'" in sql or "'customer'" in sql, \
            f"source '{name}' does not classify visibility"
        assert spec.get("label"), f"source '{name}' has no label"


def test_case_comment_visibility_follows_is_internal():
    """The case module already decides what is internal; the index must inherit
    that judgement rather than invent a second policy that can drift."""
    sql = CI.SOURCES["case_comment"]["sql"]
    assert "is_internal" in sql
    assert "COALESCE(cc.is_internal,true)" in sql, \
        "an unset is_internal must read as INTERNAL (fail-closed)"


# ===========================================================================
# DB-backed: the leak guard
# ===========================================================================

def _conn():
    try:
        from app.core.database import get_connection
        return get_connection()
    except Exception:
        return None


@pytest.fixture(scope="module")
def indexed():
    """(contact_id, account_id) of a contact that has BOTH internal and any
    rows indexed — skip when the index has not been built here."""
    conn = _conn()
    if conn is None:
        pytest.skip("no database reachable")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.content_embeddings')")
            if cur.fetchone()[0] is None:
                pytest.skip("content_embeddings not applied — run sql/content_embeddings.sql")
            cur.execute("""SELECT contact_id, count(*) FILTER (WHERE visibility='internal')
                           FROM content_embeddings
                           WHERE contact_id IS NOT NULL
                           GROUP BY contact_id HAVING count(*) FILTER (WHERE visibility='internal') > 0
                           LIMIT 1""")
            row = cur.fetchone()
            if not row:
                pytest.skip("index empty — run: python -m app.core.content_index")
            return str(row[0])
    finally:
        conn.close()


@pytest.mark.parametrize("audience", ["customer", "Customer", "CUSTOMER", "staff",
                                      "", " ", "public", "external", None])
def test_customer_audience_never_sees_internal(indexed, audience):
    """THE invariant. Any audience that is not exactly 'internal' must be treated
    as the restrictive one — so a typo, a new caller, or None under-serves
    instead of leaking. Serving an internal note to a customer-facing agent is
    the leak the reach_invariant closed in U1."""
    hits = CI.search("notes about this customer", audience=audience,
                     contact_id=indexed, limit=50)
    leaked = [h for h in hits if h["visibility"] != CI.CUSTOMER]
    assert not leaked, (
        f"audience={audience!r} leaked {len(leaked)} internal record(s): "
        f"{[h['source_type'] for h in leaked][:5]}")


def test_customer_audience_requires_a_scope(indexed):
    """A customer-audience search with no account/contact/party scope is never a
    whole-corpus search — it returns nothing rather than every customer's text."""
    assert CI.search("anything at all", audience=CI.CUSTOMER, limit=50) == []


def test_internal_audience_is_scoped_when_asked(indexed):
    """Scoping must actually bound the result set, or 'this customer's history'
    silently becomes 'everyone's history'."""
    # min_sim=0 — this asserts SCOPE, not relevance. With the normal threshold
    # the test skipped itself whenever the sample contact's records happened not
    # to match the probe query, which is exactly when a scope bug would hide.
    hits = CI.search("order", audience=CI.INTERNAL, contact_id=indexed,
                     limit=50, min_sim=0.0)
    assert hits, "sample contact has indexed rows but scoped search returned none"
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for h in hits:
                cur.execute("""SELECT contact_id::text, party_key FROM content_embeddings
                               WHERE source_type=%s AND source_id=%s""",
                            (h["source_type"], h["source_id"]))
                c_id, party = cur.fetchone()
                assert c_id == indexed or (party or "").endswith(indexed), \
                    f"{h['source_type']}:{h['source_id']} is outside the requested scope"
    finally:
        conn.close()


def test_search_never_returns_a_row_from_another_model(indexed):
    """A row embedded by a PREVIOUS model must be invisible to search.

    Behavioural, not by inspection: plant a row whose text is a near-perfect
    match for the query but whose model/dims are stale, and assert it is not
    returned. This is finding #3's failure mode end-to-end — a half-finished
    re-index must degrade to FEWER hits, never to wrong ones."""
    marker = "zzz-stale-model-probe-unique-token"
    conn = _conn()
    try:
        with conn.cursor() as cur:
            # A real current-model vector, stored under a bogus model name.
            vec = E.embed_one(marker)
            if not vec:
                pytest.skip("no embedding API available")
            cur.execute("""INSERT INTO content_embeddings
                    (source_type, source_id, content_hash, model, dims, embedding,
                     contact_id, visibility, snippet)
                    VALUES ('activity','__stale_probe__','h','legacy-model-v0',%s,%s,
                            %s::uuid,'internal',%s)
                    ON CONFLICT (source_type, source_id, chunk_ix) DO UPDATE SET
                      model=EXCLUDED.model, embedding=EXCLUDED.embedding,
                      dims=EXCLUDED.dims, snippet=EXCLUDED.snippet""",
                        (len(vec), E.encode(vec), indexed, marker))
            conn.commit()

            hits = CI.search(marker, audience=CI.INTERNAL, contact_id=indexed,
                             limit=20, min_sim=0.0)
            assert not [h for h in hits if h["source_id"] == "__stale_probe__"], \
                "a vector from a previous model was ranked — stale rows are visible"

            # Same row on the CURRENT model is found, proving the probe is sound
            # and the exclusion above came from the model pin, not from a typo.
            cur.execute("UPDATE content_embeddings SET model=%s "
                        "WHERE source_type='activity' AND source_id='__stale_probe__'",
                        (E.MODEL,))
            conn.commit()
            hits = CI.search(marker, audience=CI.INTERNAL, contact_id=indexed,
                             limit=20, min_sim=0.0)
            assert [h for h in hits if h["source_id"] == "__stale_probe__"], \
                "control failed — the probe row is not findable even on the current model"
        with conn.cursor() as cur:
            cur.execute("DELETE FROM content_embeddings "
                        "WHERE source_type='activity' AND source_id='__stale_probe__'")
            conn.commit()
    finally:
        conn.close()


def test_reclassifying_a_record_removes_it_from_customer_reach(indexed):
    """Governance metadata can change WITHOUT the text changing.

    Flipping case_comments.is_internal makes a comment staff-only but leaves its
    wording alone — so its (content_hash, model, dims) key is unchanged and the
    indexer never revisited the row. The index went on serving it as
    visibility='customer' PERMANENTLY. Verified as a live leak before the fix.

    Two layers must hold, and this asserts both:
      1. the live re-check drops it IMMEDIATELY, before any reindex runs;
      2. the next indexer pass corrects the cached column WITHOUT re-embedding.
    Uniquely-worded probe text, because near-duplicate suppression will
    otherwise collapse a seeded comment into its templated siblings."""
    import uuid as _uuid
    conn = _conn()
    probe = "Zephyr quokka telemetry anomaly reported during onboarding review"
    cid = str(_uuid.uuid4())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT case_id, contact_id FROM cases "
                        "WHERE contact_id IS NOT NULL LIMIT 1")
            row = cur.fetchone()
            if not row:
                pytest.skip("no case with a contact to attach a probe comment to")
            case_id, contact_id = row
            cur.execute("""INSERT INTO case_comments
                             (case_comment_id, case_id, comment, is_internal, created_at)
                           VALUES (%s::uuid,%s::uuid,%s,false,now())""",
                        (cid, case_id, probe))
            conn.commit()

        if not CI.reindex(source_types=["case_comment"], limit=50).get("ok"):
            pytest.skip("embedding unavailable")

        def reachable(audience):
            return any(h["source_id"] == cid for h in CI.search(
                probe, audience=audience, contact_id=str(contact_id),
                limit=20, min_sim=0.0))

        assert reachable(CI.CUSTOMER), \
            "control failed — a customer-visible comment is not reachable at all"

        with conn.cursor() as cur:
            cur.execute("UPDATE case_comments SET is_internal=true "
                        "WHERE case_comment_id=%s::uuid", (cid,))
            conn.commit()

        assert not reachable(CI.CUSTOMER), (
            "a comment reclassified as INTERNAL is still reachable by the "
            "customer audience — the live visibility re-check is not running")

        r = CI.reindex(source_types=["case_comment"], limit=50)
        assert r["by_source"]["case_comment"]["meta_synced"] >= 1, \
            "the indexer did not re-sync governance metadata"
        assert r["embedded"] == 0, \
            "metadata-only changes must not spend an embedding call"
        with conn.cursor() as cur:
            cur.execute("SELECT visibility FROM content_embeddings "
                        "WHERE source_type='case_comment' AND source_id=%s", (cid,))
            assert cur.fetchone()[0] == CI.INTERNAL
        assert reachable(CI.INTERNAL), "staff lost access to their own note"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM case_comments WHERE case_comment_id=%s::uuid", (cid,))
            cur.execute("DELETE FROM content_embeddings WHERE source_id=%s", (cid,))
            conn.commit()
        conn.close()


def test_audience_has_no_default_anywhere_on_the_read_path():
    """Default-deny must hold at the SIGNATURE, not only inside the function.

    Both entry points previously defaulted audience='internal' — the permissive
    branch — so the enforcement was fail-closed while the API surface was
    fail-open: a caller that merely forgot the argument received internal notes.
    A required parameter turns that mistake into an immediate TypeError."""
    import inspect

    for fn in (CI.search,):
        p = inspect.signature(fn).parameters["audience"]
        assert p.default is inspect.Parameter.empty, \
            f"{fn.__qualname__} gives `audience` a default — a forgetful caller leaks"

    from app.core import customer_memory as CM
    p = inspect.signature(CM.recall_relevant).parameters["audience"]
    assert p.default is inspect.Parameter.empty, \
        "customer_memory.recall_relevant defaults `audience` — it would become the way around the gate"

    # render_recall keeps an OPTIONAL audience, but must then refuse to do
    # semantic recall at all rather than assuming staff.
    src = inspect.getsource(CM.render_recall)
    assert "if (topic and audience)" in src, \
        "render_recall runs semantic recall without an explicit audience"


def test_visibility_recheck_covers_every_customer_visible_source():
    """Any source that can appear in a customer result set must have an
    authoritative re-check. A new customer-visible source added without one
    would be dropped (fail-closed) — this makes that omission loud instead."""
    customer_capable = {name for name, spec in CI.SOURCES.items()
                        if "'customer'" in spec["sql"]}
    missing = customer_capable - set(CI._VISIBILITY_RECHECK)
    assert not missing, (
        f"sources can be customer-visible but have no visibility re-check, so "
        f"their results are silently dropped on the customer path: {missing}")


def test_status_reports_stale_model_rows(indexed):
    s = CI.status()
    assert s["ok"] and s["total"] > 0
    assert s["model"] == E.MODEL and s["dims"] == E.DIMS
    assert "on_stale_model" in s


# ===========================================================================
# The behaviour change that motivated all of it
# ===========================================================================

def test_recall_relevant_is_scoped_and_audience_gated(indexed):
    """customer_memory gained a RELEVANCE path beside its recency path. It must
    pass the audience straight through — the memory layer is not allowed to be a
    way around the content index's gate."""
    from app.core import customer_memory as CM
    hits = CM.recall_relevant("contact", indexed, "pricing questions",
                              audience=CI.CUSTOMER)
    assert all(h["visibility"] == CI.CUSTOMER for h in hits)


def test_recall_still_works_without_a_topic(indexed):
    """No topic = the original recency behaviour, unchanged. The semantic path
    is additive; it must never break the block an agent already relies on."""
    from app.core import customer_memory as CM
    assert isinstance(CM.render_recall("contact", indexed), str)
