"""Guards for provenance on enriched / AI-generated fields (audit finding #4).

`enrichment.apply_to_lead` wrote industry, employee_band, revenue_band and
website onto leads from Apollo / PDL / a web scrape — or, with no provider key
configured, from `_stub()`, which FABRICATES deterministic pseudo-firmographics
from a hash of the domain and attached a 0.60-0.95 "confidence" to them. Nothing
recorded which. Meanwhile semantic_model exposes employee_band and revenue_band
as Explore DIMENSIONS, so segmentation ran on values that might be invented and
were indistinguishable from values a person typed.

The point of these tests is not that a source is recorded — it is that an
UNTRUSTWORTHY source stays visibly untrustworthy all the way to the read side.

    python -m pytest tests/test_provenance_enrichment.py -v
"""

from __future__ import annotations

import pytest

from app.core import enrichment as E
from app.core import provenance


# ===========================================================================
# Source → envelope mapping  (no DB, no network)
# ===========================================================================

@pytest.mark.parametrize("source,expected_kind", [
    ("apollo", provenance.EXTERNAL),
    ("pdl", provenance.EXTERNAL),
    ("generic", provenance.EXTERNAL),
    ("web", provenance.AI),
    ("stub", provenance.COMPUTED),
])
def test_source_maps_to_the_shared_vocabulary(source, expected_kind):
    p = E.provenance_for({"source": source})
    assert p.source_type == expected_kind
    assert p.source_id == f"enrichment:{source}"


def test_unknown_provider_is_low_trust():
    """A source we don't recognise must not inherit a confident default."""
    p = E.provenance_for({"source": "mystery-vendor", "confidence": 0.99})
    assert p.source_type == provenance.UNKNOWN
    assert p.confidence <= 0.3


@pytest.mark.parametrize("source,reliability", [
    ("apollo", 0.90), ("pdl", 0.85), ("generic", 0.75), ("web", 0.55),
])
def test_reliability_is_not_negotiable_by_the_provider(source, reliability):
    """A vendor's stated match quality is CERTAINTY about one value. It can
    never raise how much we trust the SOURCE — otherwise any provider could
    promote itself to human-grade by reporting 0.99."""
    for claimed in (0.1, 0.5, 0.99, 1.0):
        p = E.provenance_for({"source": source, "confidence": claimed})
        assert p.reliability == reliability
        assert p.certainty == claimed


@pytest.mark.parametrize("source", ["apollo", "pdl", "generic", "web", "stub"])
def test_composite_never_exceeds_source_reliability(source):
    """confidence = reliability × certainty, so it is bounded by reliability.
    A third-party guess cannot reach human-entered trust however sure it is."""
    p = E.provenance_for({"source": source, "confidence": 1.0})
    assert p.confidence <= p.reliability + 1e-9


def test_certainty_is_honoured_as_given():
    """The provider's own number is not discarded — it scales the composite."""
    p = E.provenance_for({"source": "apollo", "confidence": 0.4})
    assert p.certainty == 0.4
    assert p.confidence == pytest.approx(0.90 * 0.4, abs=1e-6)


def test_stub_is_certain_but_unreliable():
    """THE case that proves the split. The stub returns the same answer every
    time (certainty 1.0) while being disconnected from the real company
    (reliability 0.15). One number cannot express 'perfectly repeatable and
    completely made up'."""
    p = E.provenance_for(E._stub("acme.com", "Acme"))
    assert p.certainty == 1.0
    assert p.reliability <= 0.2
    assert p.confidence <= 0.2


def test_stub_is_marked_synthetic_and_low_confidence():
    """The stub INVENTS values. Its old 0.60-0.95 confidence asserted
    trustworthiness for fabricated data, which is worse than none — every
    downstream consumer read it as real match quality."""
    d = E._stub("example.com", "Example Co")
    assert d["synthetic"] is True
    assert d["confidence"] == E.STUB_CONFIDENCE <= 0.2
    p = E.provenance_for(d)
    assert p.source_type == provenance.COMPUTED
    assert p.confidence <= 0.2


def test_stub_values_are_deterministic_but_not_observed():
    """Stable per domain (so demos are reproducible) yet still invented — the
    stability is exactly what makes them look real."""
    a = E._stub("acme.com", "Acme")
    b = E._stub("acme.com", "Acme")
    assert a["industry"] == b["industry"] and a["employee_band"] == b["employee_band"]
    assert E._stub("other.com", "Other")["industry"] is not None


# ===========================================================================
# The write path
# ===========================================================================

def _conn():
    try:
        from app.core.database import get_connection
        return get_connection()
    except Exception:
        pytest.skip("no database reachable")


def _has_confidence_column(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='leads' AND column_name='confidence'")
        return cur.fetchone() is not None


def test_apply_to_lead_stamps_provenance():
    """Provenance must travel WITH the values. Without it an Apollo match, a
    web-scraped guess and a fabricated stub are the same string in the same
    column."""
    conn = _conn()
    try:
        if not _has_confidence_column(conn):
            pytest.skip("run sql/provenance_enrichment.sql")
        with conn.cursor() as cur:
            cur.execute("SELECT lead_id FROM leads WHERE deleted_at IS NULL LIMIT 1")
            row = cur.fetchone()
            if not row:
                pytest.skip("no leads")
            lid = str(row[0])
            cur.execute("SELECT industry, employee_band, revenue_band, website, "
                        "city, province, source_type, source_id, confidence "
                        "FROM leads WHERE lead_id=%s::uuid", (lid,))
            before = cur.fetchone()
            cur.execute("UPDATE leads SET industry=NULL, employee_band=NULL, "
                        "revenue_band=NULL, source_type=NULL, confidence=NULL "
                        "WHERE lead_id=%s::uuid", (lid,))
            conn.commit()

        E.apply_to_lead(lid, E._stub("probe-example.com", "Probe Co"))

        with conn.cursor() as cur:
            cur.execute("SELECT employee_band, source_type, source_id, confidence "
                        "FROM leads WHERE lead_id=%s::uuid", (lid,))
            band, st, sid, conf = cur.fetchone()
            assert band, "enrichment wrote no value at all"
            assert st == provenance.COMPUTED, f"source_type not recorded: {st}"
            assert sid == "enrichment:stub"
            assert conf is not None and float(conf) <= 0.2, \
                "fabricated firmographics are not marked low-confidence"
    finally:
        with conn.cursor() as cur:      # restore the row
            cur.execute("""UPDATE leads SET industry=%s, employee_band=%s,
                             revenue_band=%s, website=%s, city=%s, province=%s,
                             source_type=%s, source_id=%s, confidence=%s
                           WHERE lead_id=%s::uuid""", (*before, lid))
            conn.commit()
        conn.close()


def test_source_type_vocabulary_is_constrained():
    """Unconstrained free text drifts into 'apollo' / 'Apollo' / 'api' and stops
    being comparable across tables, which defeats a shared envelope."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname='leads_source_type_check'")
            if not cur.fetchone():
                pytest.skip("run sql/provenance_enrichment.sql")
            cur.execute("SELECT lead_id FROM leads WHERE deleted_at IS NULL LIMIT 1")
            lid = str(cur.fetchone()[0])
            with pytest.raises(Exception):
                cur.execute("UPDATE leads SET source_type='apollo' "
                            "WHERE lead_id=%s::uuid", (lid,))
            conn.rollback()
    finally:
        conn.close()


# ===========================================================================
# The read side — provenance only matters if something consumes it
# ===========================================================================

def test_readiness_scores_provenance():
    """An envelope nothing reads is decoration. Readiness must score BOTH
    'is a source recorded' and 'is it trustworthy'."""
    from app.core import data_readiness as DR
    rep = DR.report()
    dims = rep.get("dimensions") or {}
    assert "provenance" in dims
    assert isinstance(dims["provenance"], (int, float))


def test_readiness_warns_about_synthesized_firmographics():
    """The check that matters: a recorded source at confidence 0.15 is not a
    smaller version of a real one, and the caveat must say so."""
    from app.core import data_readiness as DR
    keys = {c["key"] for c in DR.CHECKS} if hasattr(DR, "CHECKS") else set()
    if not keys:
        import inspect
        src = inspect.getsource(DR)
        assert "lead_synthetic_firmographics" in src
        return
    assert "lead_synthetic_firmographics" in keys
