"""Regressions for defects found by ADVERSARIAL ATTACK, not by review.

Six review rounds each found a real defect that a green suite and mutation tests
missed. These tests exist because the corresponding bug shipped:

  * verification bound to evidence, not to the approved WORDING
  * a trust signal that was secretly a constant
  * an attribution heuristic applied at scale without validation
  * a PII store created after the registry built to prevent exactly that
  * concurrent consolidation with no lock
  * one signature promoting unlimited claims

    python -m pytest tests/test_memory_safety_case.py -v
"""

from __future__ import annotations

import pytest

from app.core import lifecycle
from app.core import memory_assurance as MA
from app.core import memory_consolidation as MC
from app.core.content_index import actor_for


@pytest.fixture
def conn():
    try:
        from app.core.database import get_connection
        c = get_connection()
    except Exception:
        pytest.skip("no database reachable")
    try:
        yield c
    finally:
        c.close()


# ===========================================================================
# 1. Every PII store must be registered for erasure
# ===========================================================================

def test_verification_records_are_erasable():
    """FOURTH instance of this bug class. memory_verifications stores
    `statement_shown` — the exact claim ABOUT A PERSON that a human approved —
    and was in no erasure plan. Being append-only made it easier to argue it
    should survive; that argument does not survive an erasure request."""
    assert "memory_verifications" in lifecycle.DERIVED_PII_STORES
    for entity in ("contacts", "accounts"):
        sats = lifecycle.PLANS[entity]["satellites"]
        hit = [s for s in sats if s["table"] == "memory_verifications"]
        assert hit, f"{entity} erasure does not delete memory_verifications"
        assert hit[0]["action"] == lifecycle.DELETE


def test_verifications_are_deleted_before_their_parent():
    """Ordering matters: memory_verifications is reachable only VIA
    customer_memories, so deleting the parent first orphans the child and the
    lookup finds nothing to erase."""
    sats = [s["table"] for s in lifecycle.PLANS["contacts"]["satellites"]]
    assert sats.index("memory_verifications") < sats.index("customer_memories")


def test_every_pii_store_declares_how_it_is_reached():
    for store, spec in lifecycle.DERIVED_PII_STORES.items():
        assert spec.get("why"), f"{store} has no stated reason"
        assert spec.get("regenerated_by"), f"{store} declares no regenerator"


# ===========================================================================
# 2. A trust signal must not be secretly a constant
# ===========================================================================

def test_reliability_join_resolves(conn):
    """`reliability = weakest evidence link` was documented, tested around, and
    had NEVER ONCE EXECUTED: the lookup joined leads.lead_id to a contact_id,
    which can never match, so every memory stored the 0.70 default. A trust
    signal that is secretly a constant is worse than none - it looks earned.

    Behavioural, not source-matching: an earlier version of this test grepped
    the function body and matched the FIX'S OWN COMMENT quoting the broken join.
    A test that reads comments is testing prose."""
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM content_embeddings ce
                         JOIN contacts ct ON ct.contact_id = ce.contact_id""")
        by_contact = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM content_embeddings ce
                         JOIN leads l ON l.lead_id = ce.contact_id""")
        by_lead = cur.fetchone()[0]
    assert by_contact > 0, "no indexed rows resolve to a contact"
    assert by_lead == 0, ("contact_id resolves against leads - the original "
                          "join was not as impossible as diagnosed; re-check")


def test_weakest_evidence_link_propagates(conn):
    """End to end: a low-confidence source must drag the memory's reliability
    down, or the 'weakest link' rule is decoration."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""SELECT contact_id FROM content_embeddings
                        WHERE contact_id IS NOT NULL
                        GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""")
        r = cur.fetchone()
        if not r:
            pytest.skip("no indexed contacts")
        cid = str(r[0])
        cur.execute("SELECT confidence FROM contacts WHERE contact_id=%s::uuid", (cid,))
        restore = cur.fetchone()[0]
        cur.execute("UPDATE contacts SET confidence=0.25 WHERE contact_id=%s::uuid", (cid,))
        cur.execute("DELETE FROM customer_memories WHERE entity_id=%s::uuid", (cid,))
    try:
        MC.consolidate_entity("contact", cid)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT reliability FROM customer_memories "
                        "WHERE entity_id=%s::uuid", (cid,))
            vals = [float(x[0]) for x in cur.fetchall() if x[0] is not None]
        if not vals:
            pytest.skip("no memories derived for this contact")
        assert max(vals) <= 0.3, f"low-confidence evidence did not lower reliability: {vals}"
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE contacts SET confidence=%s WHERE contact_id=%s::uuid",
                        (restore, cid))
            cur.execute("DELETE FROM customer_memories WHERE entity_id=%s::uuid", (cid,))


# ===========================================================================
# 3. Attribution must be validated, not assumed
# ===========================================================================

def test_actor_heuristic_meets_its_precision_bar(conn):
    """The withdrawn activity-type rule scored 64.3% precision when it committed
    — barely better than guessing between two classes — and had been applied to
    5,595 records and reported as a coverage win. Held-out validation against
    rows that DO carry an observed direction is the standing bar."""
    with conn.cursor() as cur:
        # EXCLUDE internal work items. `direction` is demonstrably arbitrary for
        # them — "Order shipped - follow up with customer", "Welcome / Account
        # created" and "First contact / Intro" each appear in this data labelled
        # BOTH inbound and outbound. Measuring a rule against a field that
        # carries two labels for one logical event measures noise, which is how
        # a "53.3% precision" figure was produced and acted on.
        cur.execute("""SELECT ce.snippet, ce.direction, ce.source_type, a.type
                         FROM content_embeddings ce
                         LEFT JOIN activities a
                           ON ce.source_type='activity'
                          AND a.activity_id::text = ce.source_id
                        WHERE ce.direction IS NOT NULL
                          AND lower(COALESCE(a.type,'')) NOT IN
                              ('task','note','todo','reminder')""")
        rows = cur.fetchall()
    if len(rows) < 50:
        pytest.skip("not enough ground-truth rows")

    truth = {"inbound": {"customer_said", "customer_did"},
             "outbound": {"company_did"}}
    agree = disagree = 0
    for snippet, direction, source_type, atype in rows:
        guess = actor_for(None, source_type, snippet or "", atype)  # direction withheld
        if guess == "unknown":
            continue
        if guess in truth.get(direction, set()):
            agree += 1
        else:
            disagree += 1
    decided = agree + disagree
    if decided < 20:
        pytest.skip("heuristic abstains too often to measure here")
    precision = agree / decided
    assert precision >= 0.85, (
        f"actor heuristic precision {precision:.1%} on held-out rows. An "
        f"unattributed memory says less; a wrongly attributed one says "
        f"something FALSE about a person.")


def test_withdrawn_rule_stays_withdrawn():
    """Reinstating it requires a hand-labelled sample and >=95% precision."""
    from app.core.content_index import _COMPANY_ACTIVITY_TYPES
    assert not _COMPANY_ACTIVITY_TYPES, \
        "the 64%-precision activity-type rule is back without validation"


# ===========================================================================
# 4. Concurrency
# ===========================================================================

def test_consolidation_takes_an_advisory_lock():
    """consolidate_entity is API-reachable from any replica while the
    leader-gated job also runs. Two passes interleaving means one rebuilds
    themes while the other links memories the first is about to delete."""
    import inspect
    src = inspect.getsource(MC.consolidate_entity)
    assert "pg_try_advisory_xact_lock" in src
    assert "skipped" in src, "a losing caller must return, not queue"


# ===========================================================================
# 5. One signature must not be enough for a costly claim
# ===========================================================================

def test_high_consequence_topics_require_two_approvers():
    assert MC.REQUIRED_APPROVALS >= 2
    for topic in ("billing", "pricing"):
        assert topic in MC.HIGH_CONSEQUENCE_TOPICS, \
            f"'{topic}' can create a financial obligation and needs dual approval"


def test_same_person_cannot_be_both_approvers():
    """A second signature from one person is not a second opinion."""
    import inspect
    src = inspect.getsource(MC.verify)
    assert "_distinct_approvers" in src
    assert "DISTINCT performed_by" in inspect.getsource(MC._distinct_approvers)


# ===========================================================================
# 6. Verification binds to the approved CLAIM
# ===========================================================================

def test_claim_hash_binds_statement_and_evidence():
    a = MC.claim_hash("Raised billing 2 times.", "ev")
    assert a != MC.claim_hash("Customer agreed to a refund.", "ev")
    assert a != MC.claim_hash("Raised billing 2 times.", "ev2")


# ===========================================================================
# Internal work items — a SEMANTIC rule, not a statistical one
# ===========================================================================

@pytest.mark.parametrize("atype", ["task", "note", "todo", "reminder"])
def test_internal_work_items_are_our_action(atype):
    """A task/note/todo/reminder is something WE created and own. The actor is
    company_did by definition of the record type — there is nothing to
    estimate, which is why this rule needs no precision bar."""
    from app.core.content_index import actor_for
    assert actor_for(None, "activity", "Follow up with the customer", atype)         == "company_did"


def test_work_item_rule_ignores_direction():
    """`direction` is arbitrary for work items in this data: "Order shipped -
    follow up with customer", "Welcome / Account created" and "First contact /
    Intro" each appear labelled BOTH inbound and outbound. Letting it win would
    reintroduce the arbitrary labelling this rule exists to bypass."""
    from app.core.content_index import actor_for
    for direction in ("inbound", "outbound", None):
        assert actor_for(direction, "activity", "Invoice 35 issued", "task")             == "company_did"


def test_text_cues_still_outrank_the_type_rule():
    """A work item RECORDING what a customer said is still about the customer,
    and a third party mentioned in one is still a third party. The type rule is
    a fallback, not an override."""
    from app.core.content_index import actor_for
    assert actor_for(None, "activity",
                     "Customer said the invoice was wrong.", "task") == "customer_said"
    assert actor_for(None, "activity",
                     "Carrier reported a delay in transit.", "task") == "third_party_did"


def test_work_items_never_claim_the_customer_acted(conn):
    """The point of the whole exercise. 73.4% of the memory corpus is
    type='task' — our own to-do list. Before this rule those records produced
    statements like "Raised billing 25 times", attributing OUR actions to the
    customer. They must now read "We contacted them about ..."."""
    with conn.cursor() as cur:
        cur.execute("""SELECT cm.statement, cm.evidence
                         FROM customer_memories cm
                        WHERE cm.statement LIKE 'Raised %'""")
        rows = cur.fetchall()
    if not rows:
        pytest.skip("no customer-attributed memories in this dataset")
    with conn.cursor() as cur:
        for statement, evidence in rows:
            ids = [(e["source_type"], e["source_id"]) for e in (evidence or [])
                   if e.get("source_id")]
            if not ids:
                continue
            cur.execute("""SELECT count(*) FROM content_embeddings ce
                             LEFT JOIN activities a
                               ON ce.source_type='activity'
                              AND a.activity_id::text = ce.source_id
                            WHERE (ce.source_type, ce.source_id) IN %s
                              AND lower(COALESCE(a.type,'')) IN
                                  ('task','note','todo','reminder')""", (tuple(ids),))
            work_items = cur.fetchone()[0]
            assert work_items == 0, (
                f"{statement!r} claims the customer acted, but {work_items} of "
                f"its evidence records are our own internal work items")


# ===========================================================================
# uuid[] handling, topic tie-breaks, cluster determinism, reconstruction
# ===========================================================================

def test_uuid_arrays_decode_as_lists(conn):
    """psycopg2 returns a `uuid[]` as the literal string '{...}' unless the uuid
    type is registered. Iterating that yields CHARACTERS, so an empty array
    reads as ['{','}'] — truthy. This caused three separate live bugs before the
    cause was found: every memory falsely flagged "contradicted", cross-linking
    wrote character fragments, and reconstruction crashed on
    'invalid input syntax for type uuid: "{"'."""
    import app.core.database  # noqa: F401 — import registers the type
    with conn.cursor() as cur:
        cur.execute("SELECT ARRAY[gen_random_uuid()]")
        v = cur.fetchone()[0]
        assert isinstance(v, list), f"uuid[] came back as {type(v).__name__}"
        cur.execute("SELECT ARRAY[]::uuid[]")
        assert cur.fetchone()[0] == []


def test_topic_ties_break_toward_the_stricter_policy():
    """`topic` decides the decay class AND how many humans must approve, so a
    tie between two topics is a tie between two SAFETY POLICIES. It used to
    break on -ord(name[0]) — the earlier letter won, so approval policy was
    settled by the alphabet."""
    for snippet in ("renewal contract shipped delivery tracking",
                    "invoice charge shipped delivery",
                    "demo proposal price quote"):
        topic = MC._topic_for([snippet])
        assert MC.required_approvals_for(topic) >= 2, (
            f"{snippet!r} -> '{topic}' needs only "
            f"{MC.required_approvals_for(topic)} approver(s); a tie must resolve "
            f"to the stricter policy")


def test_topic_strictness_matches_the_policy_table(conn):
    """The static tie-break rank must agree with the DB policy it stands in for,
    or ties resolve toward a strictness the system does not actually apply."""
    for topic, rank in MC._TOPIC_STRICTNESS.items():
        actual = MC.required_approvals_for(topic)
        assert actual >= rank, (
            f"'{topic}' ranked {rank} for tie-breaks but the policy table "
            f"requires {actual}")


def test_cluster_input_order_is_total(conn):
    """`ORDER BY occurred_at DESC` alone is not deterministic — one contact has
    162 timestamps shared by more than one record, and _cluster() attaches each
    record to the FIRST seed it is near. A reordering produces different
    clusters, a different evidence_hash, and silently invalidates any human
    verification of those memories."""
    import inspect
    src = inspect.getsource(MC._load_records)
    order = [ln for ln in src.splitlines() if "ORDER BY" in ln][0]
    assert "source_id" in order, (
        "cluster input order has no total tiebreaker: " + order.strip())


def test_reconstruction_flags_memories_rewritten_after_the_utterance(conn):
    """A reconstruction that quietly presented today's memory as what was
    believed then would be worse than none — it would launder drift into
    evidence."""
    import uuid as _uuid

    from app.core import grounding
    MA.ensure_tables()
    with conn.cursor() as cur:
        cur.execute("SELECT memory_id::text, entity_id::text, statement "
                    "FROM customer_memories LIMIT 1")
        row = cur.fetchone()
    if not row:
        pytest.skip("no memories")
    mid, eid, original = row
    play = str(_uuid.uuid4())
    tok = grounding.set_correlation_id(play)
    try:
        MA.record_utterance("probe", audience="customer", entity_type="contact",
                            entity_id=eid, channel="pytest", memory_ids=[mid])
    finally:
        grounding.reset_correlation_id(tok)
    try:
        with conn.cursor() as cur:
            # now() is the TRANSACTION start time in Postgres, so on a
            # connection with an open transaction it can predate the utterance
            # written moments earlier on another connection. Use an
            # unambiguously later stamp — the drift this asserts is real, the
            # sub-second race is not what is under test.
            cur.execute("UPDATE customer_memories SET statement=%s, "
                        "updated_at=now() + interval '1 hour' "
                        "WHERE memory_id=%s::uuid", ("rewritten", mid))
        conn.commit()   # reconstruct() reads on its own connection
        r = MA.reconstruct(correlation_id=play)
        assert r["ok"]
        m = r["utterances"][0]["memories_in_context"][0]
        assert m["rewritten_since_utterance"] is True
        assert any("REWRITTEN" in c for c in r["utterances"][0]["caveats"])
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE customer_memories SET statement=%s WHERE memory_id=%s::uuid",
                        (original, mid))
            cur.execute("DELETE FROM agent_utterances WHERE channel='pytest'")
        conn.commit()


def test_reconstruction_reports_erased_memories(conn):
    """"The claim we relied on no longer exists" is itself the answer to some
    disputes, and must not read as "no memory was involved"."""
    import uuid as _uuid

    MA.ensure_tables()
    ghost = str(_uuid.uuid4())
    play = str(_uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO agent_utterances
                         (correlation_id, audience, channel, text, memory_ids)
                       VALUES (%s,'customer','pytest','probe',ARRAY[%s]::uuid[])""",
                    (play, ghost))
    conn.commit()       # reconstruct() reads on its own connection
    try:
        r = MA.reconstruct(correlation_id=play)
        m = r["utterances"][0]["memories_in_context"][0]
        assert m["state"] == "erased_or_deleted"
        assert any("erased" in c for c in r["utterances"][0]["caveats"])
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_utterances WHERE channel='pytest'")
        conn.commit()


# ===========================================================================
# 8. A query bound must not be reported as a fact about a person
# ===========================================================================

def test_clipped_window_never_asserts_an_exact_count_or_a_start_date():
    """MAX_RECORDS is a feasibility bound. It was being rendered as history: on
    the largest contact here (762 records) the "returns" theme read "came up 22
    times between 2025-12-19 and 2026-01-09" — 2025-12-19 is where the LIMIT
    fell, not when returns started (true history began 2025-11-28). A `truncated`
    column recorded this, and nobody reading a sentence reads a column."""
    import datetime as d
    a = d.datetime(2025, 12, 19, tzinfo=d.timezone.utc)
    b = d.datetime(2026, 1, 9, tzinfo=d.timezone.utc)

    honest = MC._statement("returns", 22, a, b, None, True)
    assert "at least 22" in honest, "clipped count must be a floor, not a count"
    assert "between" not in honest, "a clipped window has no known beginning"
    assert "not examined" in honest, "the reader must be told history was cut"

    exact = MC._statement("returns", 22, a, b, None, False)
    assert "at least" not in exact and "between" in exact, (
        "an unclipped window must keep its precision - the hedge is not free, "
        "it is the correct claim only when something was actually excluded")


def test_truncation_is_measured_not_inferred_from_row_count():
    """The old flag was `len(records) >= MAX_RECORDS`, which reports truncation
    for a customer holding exactly MAX_RECORDS records with nothing excluded.
    The flag now requires evidence that older history EXISTS."""
    import inspect
    src = inspect.getsource(MC._load_records)
    assert "min(occurred_at)" in src, (
        "clipping must be established against the unbounded minimum")


def test_generator_identity_tracks_the_words_it_produces(monkeypatch):
    """`v1` was hand-maintained, so changing the statement templates left two
    materially different sentences claiming one generator identity. Behavioural
    on purpose: it must move when the WORDS move."""
    before = MC._wording_fingerprint()
    assert before == MC._wording_fingerprint(), "fingerprint is not stable"

    real = MC._statement
    monkeypatch.setattr(MC, "_statement",
                        lambda *a, **k: real(*a, **k) + " (reworded)")
    assert MC._wording_fingerprint() != before, (
        "the statement wording changed and the generator identity did not")


def test_previous_generator_rows_are_retired_not_left_active(conn):
    """A generator change does not collide on the ON CONFLICT key and the
    same-generator sweep filters them out, so the previous generator's rows
    survived indefinitely, still status='active', still served by recall -
    presenting superseded wording as current."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""SELECT entity_id::text FROM customer_memories
                        WHERE entity_type='contact' GROUP BY 1 LIMIT 1""")
        r = cur.fetchone()
    if not r:
        pytest.skip("no consolidated contact")
    eid = r[0]
    with conn.cursor() as cur:      # leftovers from an earlier run collide on
        cur.execute("DELETE FROM customer_memories WHERE generator='stale/v0'")
    MC.consolidate_entity("contact", eid)
    with conn.cursor() as cur:      # the (entity,topic,kind,generator) claim key
        # Only the CURRENT generator's rows: real entities accumulate rows
        # under several historical generators, so a blanket relabel collides on
        # the claim key.
        cur.execute("""UPDATE customer_memories SET generator='stale/v0'
                        WHERE entity_id=%s::uuid AND kind=%s
                          AND generator=%s""", (eid, MC.THEME, MC.GENERATOR))
    out = MC.consolidate_entity("contact", eid)
    # Either mechanism is correct — judged rows are retired, unjudged ones are
    # discarded. What must never happen is that they stay ACTIVE.
    assert out.get("retired", 0) + out.get("discarded", 0) > 0,         "previous-generator rows were neither retired nor discarded"
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM customer_memories
                        WHERE entity_id=%s::uuid AND generator='stale/v0'
                          AND status='active'""", (eid,))
        assert cur.fetchone()[0] == 0, "stale-generator rows are still active"
        cur.execute("DELETE FROM customer_memories WHERE generator='stale/v0'")


def test_wording_fingerprint_is_sensitive_to_every_branch(monkeypatch):
    """A fingerprint that misses a branch is worse than none: it certifies that
    wording did not change while it did. Ten source-level mutations (one per
    rendered clause) were caught 10/10 when this was built; this pins the
    per-branch sensitivity that made that possible, by perturbing ONE actor or
    ONE clipped state at a time and requiring the identity to move."""
    base = MC._wording_fingerprint()
    real = MC._statement

    for actor in ("customer_said", "customer_did", "company_did",
                  "third_party_did", None):
        def only(a=actor):
            def f(topic, n, first, last, actor_=None, clipped=False):
                out = real(topic, n, first, last, actor_, clipped)
                return out + "!" if actor_ == a else out
            return f
        monkeypatch.setattr(MC, "_statement", only())
        assert MC._wording_fingerprint() != base, (
            f"wording for actor={actor} can change without moving the generator")

    for state in (True, False):
        def only(s=state):
            def f(topic, n, first, last, actor_=None, clipped=False):
                out = real(topic, n, first, last, actor_, clipped)
                return out + "!" if clipped is s else out
            return f
        monkeypatch.setattr(MC, "_statement", only())
        assert MC._wording_fingerprint() != base, (
            f"wording for clipped={state} can change without moving the generator")


# ===========================================================================
# 9. The append-only audit trail could not be erased — silently
# ===========================================================================

def _probe_verification(cur, statement="probe claim"):
    cur.execute("SELECT memory_id::text, entity_type, entity_id::text "
                "  FROM customer_memories LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    mid, et, eid = row
    cur.execute("""INSERT INTO memory_verifications
          (memory_id, action, actor_confirmed, evidence_hash, evidence_shown,
           statement_shown, performed_by, role, entity_type, entity_id)
          VALUES (%s::uuid,'verified',true,'h',1,%s,'pytest','admin',%s,%s::uuid)""",
                (mid, statement, et, eid))
    return mid, et, eid


def test_verification_trail_can_actually_be_erased(conn):
    """The sanctioned erasure path was a NO-OP for months. `ON DELETE ... DO
    INSTEAD NOTHING` is applied at query-rewrite time and SECURITY DEFINER does
    not exempt it, so the DELETE inside erase_memory_verifications() was
    discarded: it reported 0 deleted and the row survived. Every erasure
    completed "successfully" while statement_shown stayed on disk.

    The old test asserted the erasure PLAN contained this table. A plan is not
    a deletion."""
    conn.autocommit = True
    with conn.cursor() as cur:
        got = _probe_verification(cur)
        if not got:
            pytest.skip("no memories")
        _, et, eid = got
        cur.execute("SELECT count(*) FROM memory_verifications "
                    " WHERE entity_id=%s::uuid AND performed_by='pytest'", (eid,))
        assert cur.fetchone()[0] >= 1
        cur.execute("SELECT erase_verifications_for_entity(%s,%s::uuid)", (et, eid))
        assert cur.fetchone()[0] >= 1, "erasure reported deleting nothing"
        cur.execute("SELECT count(*) FROM memory_verifications WHERE entity_id=%s::uuid",
                    (eid,))
        assert cur.fetchone()[0] == 0, "rows survived a completed erasure"


def test_append_only_refuses_loudly_not_silently(conn):
    """DO INSTEAD NOTHING failed silently in BOTH directions: it hid the broken
    erasure, and a forger's UPDATE also 'succeeded' from the client's view."""
    conn.autocommit = True
    with conn.cursor() as cur:
        if not _probe_verification(cur, "forgery target"):
            pytest.skip("no memories")
    with conn.cursor() as cur:
        with pytest.raises(Exception) as e:
            cur.execute("UPDATE memory_verifications SET performed_by='mallory' "
                        " WHERE performed_by='pytest'")
        assert "append-only" in str(e.value)
    with conn.cursor() as cur:
        # Even the cleanup must use the sanctioned path — a plain DELETE here is
        # refused, which is the whole point.
        cur.execute("""SELECT erase_verifications_for_entity(entity_type, entity_id)
                         FROM memory_verifications WHERE performed_by='pytest'""")


def test_verification_is_reachable_after_its_memory_is_swept(conn):
    """The erasure joined through customer_memories, so a verification whose
    memory had been deleted was unreachable BY CONSTRUCTION — 10 of 10 rows in
    this database were exactly that. Consolidation sweeps any memory with
    verified_by IS NULL, and re-derivation clears verified_by whenever the
    evidence hash moves: verify, re-derive, lose the topic, and the memory is
    swept while its verification rows remain. Ordinary path, unerasable PII."""
    conn.autocommit = True
    with conn.cursor() as cur:
        got = _probe_verification(cur, "orphan probe")
        if not got:
            pytest.skip("no memories")
        mid, et, eid = got
        # The parent disappears; the trail must still be locatable by person.
        cur.execute("""SELECT count(*) FROM memory_verifications
                        WHERE entity_id=%s::uuid AND memory_id=%s::uuid""", (eid, mid))
        assert cur.fetchone()[0] >= 1, "verification does not carry its entity"
        cur.execute("SELECT erase_verifications_for_entity(%s,%s::uuid)", (et, eid))
        assert cur.fetchone()[0] >= 1


def test_no_unattributable_verification_rows_accumulate(conn):
    """A row with no entity can never be erased on request and never shown to
    the person it describes. Holding PII you cannot locate is worse than not
    holding it."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_verifications WHERE entity_id IS NULL")
        assert cur.fetchone()[0] == 0


def test_erasure_verifies_the_outcome_not_the_rowcount(conn):
    """`if cur.rowcount:` cannot tell "nothing matched" from "the statement was
    silently discarded" — which is precisely how this shipped. Erasure now
    re-reads every DELETE satellite."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id::text FROM customer_memories "
                    " WHERE entity_type='contact' LIMIT 1")
        r = cur.fetchone()
        if not r:
            pytest.skip("no contact memories")
        left = lifecycle._survivors_after_erase(
            cur, lifecycle.PLANS["contacts"], "contacts", r[0])
        assert left.get("customer_memories", 0) > 0, (
            "the post-condition cannot see rows that are demonstrably present, "
            "so it would not catch a silently-discarded delete either")


def test_erase_sp_end_to_end_removes_the_verification_trail(conn, monkeypatch):
    """END TO END, through erase_sp — not through the SQL function.

    This test exists because unit-testing the function was not enough: the
    `SET LOCAL app.memory_audit_erase` that erasure depends on was first added
    to `preview()` (the read-only path) instead of `erase_sp`. Every direct test
    of the SQL function still passed, and a real erasure still raised. Only
    driving the actual entry point caught it.

    Runs inside a transaction that is always rolled back, so no record is
    really erased."""
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SELECT entity_id::text FROM customer_memories "
                    " WHERE entity_type='contact' LIMIT 1")
        r = cur.fetchone()
        if not r:
            pytest.skip("no contact memories")
        eid = r[0]
        cur.execute("""INSERT INTO memory_verifications
              (memory_id,action,actor_confirmed,evidence_hash,evidence_shown,
               statement_shown,performed_by,role,entity_type,entity_id)
              SELECT memory_id,'verified',true,'h',1,'e2e probe','pytest','admin',
                     entity_type,entity_id
                FROM customer_memories WHERE entity_id=%s::uuid LIMIT 1""", (eid,))

    class _Held:                      # hand erase_sp OUR transaction
        def __init__(self, real): self._r = real
        def __getattr__(self, n): return getattr(self._r, n)
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(lifecycle, "get_connection", lambda: _Held(conn))
    try:
        out = lifecycle.erase_sp({"entity": "contacts", "record_id": eid,
                                  "confirm": True, "requested_by": "pytest",
                                  "basis": "test"})
        assert out.get("ok"), out
        assert (out.get("deleted") or {}).get("memory_verifications", 0) >= 1, (
            "erasure did not remove the verification trail")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memory_verifications "
                        " WHERE entity_id=%s::uuid", (eid,))
            assert cur.fetchone()[0] == 0
    finally:
        conn.rollback()


# ===========================================================================
# 10. Uncertainty must fail in ONE direction, not two
# ===========================================================================

def test_every_topic_has_an_explicit_decay_class():
    """`_TOPIC_DECAY` named 4 of the 10 topics in `_TOPICS`; the rest inherited
    EPISODIC from a dict default — a 180-day assertion lifetime assigned by
    omission. Adding a topic must not silently grant it one."""
    vocab = {name for name, _ in MC._TOPICS}
    missing = vocab - set(MC._TOPIC_DECAY)
    assert not missing, f"topics with no declared decay class: {sorted(missing)}"


def test_unclassifiable_claims_decay_fastest_not_middling():
    """The same uncertainty was treated in opposite directions: an unknown topic
    needs the STRICTEST approval, and got the MIDDLE decay tier. `general` is
    12.6% of this corpus and reaches it precisely when the classifier could not
    characterise the text - which is the case for keeping it shortest, not
    longest."""
    assert MC.decay_class_for("general") == MC.VOLATILE
    assert MC.decay_class_for("a topic that does not exist") == MC.VOLATILE
    half = {MC.VOLATILE: 30, MC.EPISODIC: 180, MC.STABLE: 1460}
    assert half[MC.decay_class_for("nonexistent")] == min(half.values()), (
        "the fallback decay class is not the shortest-lived one")


def test_a_quoted_price_is_not_treated_as_a_six_month_fact():
    """pricing was absent from the map and inherited 180 days."""
    assert MC.decay_class_for("pricing") == MC.VOLATILE


def test_unjudged_superseded_memories_are_discarded_not_hoarded(conn):
    """Retiring every previous-generator row bounds nothing. Two derivation
    changes in one afternoon produced 270 retired rows against 135 live ones,
    all 270 with no verification history — PII-bearing assertions kept for an
    audit with nothing to audit. Only a claim a human actually ruled on has
    history worth keeping."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""SELECT entity_id::text FROM customer_memories
                        WHERE entity_type='contact' AND kind=%s LIMIT 1""", (MC.THEME,))
        r = cur.fetchone()
        if not r:
            pytest.skip("no themes")
        eid = r[0]
        cur.execute("""UPDATE customer_memories SET generator='stale/v0'
                        WHERE entity_id=%s::uuid AND kind=%s AND generator=%s""",
                    (eid, MC.THEME, MC.GENERATOR))
        if cur.rowcount == 0:
            pytest.skip("nothing to retire")
    out = MC.consolidate_entity("contact", eid)
    assert out.get("discarded", 0) > 0, "unjudged churn was kept"
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM customer_memories
                        WHERE entity_id=%s::uuid AND generator='stale/v0'""", (eid,))
        assert cur.fetchone()[0] == 0


def test_a_judged_memory_is_retired_rather_than_deleted(conn):
    """The other half of the same rule: if a person ruled on the claim, the row
    survives as 'superseded' so their judgement still has a subject."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""SELECT memory_id::text, entity_id::text FROM customer_memories
                        WHERE entity_type='contact' AND kind=%s LIMIT 1""", (MC.THEME,))
        r = cur.fetchone()
        if not r:
            pytest.skip("no themes")
        mid, eid = r
        cur.execute("""INSERT INTO memory_verifications
              (memory_id,action,actor_confirmed,evidence_hash,evidence_shown,
               statement_shown,performed_by,role,entity_type,entity_id)
              VALUES (%s::uuid,'rejected',true,'h',1,'judged','pytest','admin',
                      'contact',%s::uuid)""", (mid, eid))
        cur.execute("""UPDATE customer_memories SET generator='stale/v0'
                        WHERE memory_id=%s::uuid""", (mid,))
    try:
        MC.consolidate_entity("contact", eid)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM customer_memories WHERE memory_id=%s::uuid",
                        (mid,))
            row = cur.fetchone()
            assert row, "a judged memory was deleted; the ruling lost its subject"
            assert row[0] == "superseded"
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT erase_verifications_for_entity('contact',%s::uuid)", (eid,))
            cur.execute("DELETE FROM customer_memories WHERE generator='stale/v0'")


# ===========================================================================
# 11. The review surface must apply the SAME gate as enforcement
# ===========================================================================

def test_explain_and_recall_return_the_same_verdict(conn):
    """explain() is what a reviewer reads before approving a claim, and it
    computed a STRICTLY WEAKER gate than recall enforces: it passed
    `verification_expires_at=None` as a literal and omitted the claim hash and
    signature, so three conditions could not fire there at all —

        statement changed since it was verified
        verification signature invalid or unsigned   (the control a DB writer
        verification expired                          cannot satisfy)

    Two implementations of one rule, on the screen built for that judgement."""
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT entity_type, entity_id::text
                         FROM customer_memories WHERE status='active' LIMIT 10""")
        ents = cur.fetchall()
    if not ents:
        pytest.skip("no memories")
    compared = 0
    for etype, eid in ents:
        for m in MC.recall(etype, eid, audience="internal", limit=20):
            e = MC.explain(m["memory_id"])
            if not e.get("ok"):
                continue
            compared += 1
            assert sorted(m.get("assertion_blockers") or []) == \
                   sorted(e.get("assertion_blockers") or []), (
                f"explain and recall disagree on {m['memory_id']}")
    assert compared, "nothing was actually compared"


def test_gate_fails_closed_when_a_safety_input_is_missing():
    """Each of these checks required its input to be PRESENT before it could
    fire, so a caller that omitted one got a pass. Not knowing whether the
    statement still matches what a human approved is a reason to refuse."""
    base = dict(verified_by="alan", verified_actor=True,
                verification_expires_at=None, kind=MC.FACT,
                visibility=MC.CUSTOMER, actor="customer_said",
                contradicts=[], conflict_severity=None, evidence_missing=0,
                truncated=False, effective_certainty=0.9,
                verified_claim_hash="abc", current_claim_hash="abc",
                signature_ok=True)
    assert MC._assertable(**base)
    assert not MC._assertable(**dict(base, signature_ok=None)), \
        "an uncomputed signature was treated as a valid one"
    assert not MC._assertable(**dict(base, verified_claim_hash=None)), \
        "a missing claim hash was treated as a matching one"
    assert not MC._assertable(**dict(base, current_claim_hash=None)), \
        "an uncomputable current hash was treated as a match"


def test_gate_inputs_is_the_only_assembly_point():
    """recall() built the gate's inputs twice verbatim and ran the whole gate
    twice per memory; explain() built a third, weaker set. One assembly."""
    import inspect
    src = inspect.getsource(MC.recall) + inspect.getsource(MC.explain)
    assert src.count("gate_inputs(") == 2, (
        "a caller is assembling gate inputs by hand again")
    assert "verification_expires_at=None" not in src, (
        "expiry is hardcoded off somewhere in the gate path")


# ===========================================================================
# 12. Defect CLASS: irreversible, un-provenanced mutation
# ===========================================================================

def test_every_deletion_is_recoverable_without_anyone_remembering(conn):
    """Provenance as a DISCIPLINE failed twice — once in a 5,657-row backfill,
    then again in the very remediation that created `data_repairs` to prevent
    it, which deleted 270 rows with none. No threshold could have caught the
    second: it ran as 59 statements of 2-5 rows each. Nothing about the SIZE of
    a statement identifies a repair.

    So no code participates in producing provenance any more."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO customer_memories
            (entity_type,entity_id,kind,statement,topic,occurrences,evidence,
             evidence_count,evidence_hash,source_type,reliability,certainty,
             generator,visibility,last_observed_at)
            VALUES ('contact',gen_random_uuid(),'theme','undo probe','seed',1,
                    '[]'::jsonb,0,'undo-probe','ai',0.5,0.5,'pytest/undo',
                    'internal',now()) RETURNING memory_id::text""")
        mid = cur.fetchone()[0]
        cur.execute("DELETE FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
        cur.execute("SELECT repair_key FROM governed_deletions WHERE row_pk=%s", (mid,))
        row = cur.fetchone()
        assert row and row[0] == "undeclared", "a plain delete left no image"
        cur.execute("SELECT restore_governed_deletion('undeclared')")
        cur.execute("SELECT count(*) FROM customer_memories WHERE memory_id=%s::uuid",
                    (mid,))
        assert cur.fetchone()[0] == 1, "the image was not mechanically restorable"
        cur.execute("SET app.repair_key='pytest:cleanup'")
        cur.execute("DELETE FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
        cur.execute("RESET app.repair_key")
        cur.execute("DELETE FROM governed_deletions WHERE row_pk=%s", (mid,))


def test_an_erasure_leaves_nothing_to_restore(conn):
    """The one deletion that must NOT be recoverable. An undo log that survives
    a GDPR erasure is a mechanically restorable copy of exactly what was
    erased."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO customer_memories
            (entity_type,entity_id,kind,statement,topic,occurrences,evidence,
             evidence_count,evidence_hash,source_type,reliability,certainty,
             generator,visibility,last_observed_at)
            VALUES ('contact',gen_random_uuid(),'theme','erase probe','seed',1,
                    '[]'::jsonb,0,'erase-probe','ai',0.5,0.5,'pytest/erase',
                    'internal',now()) RETURNING memory_id::text""")
        mid = cur.fetchone()[0]
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SET LOCAL app.erasure = 'on'")
        cur.execute("DELETE FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
        cur.execute("COMMIT")
        cur.execute("SELECT count(*) FROM governed_deletions WHERE row_pk=%s", (mid,))
        assert cur.fetchone()[0] == 0, "an erased row is still restorable"


def test_signing_key_can_be_rotated(monkeypatch):
    """A single unlabelled key made rotation impossible: changing it invalidated
    every verification at once, indistinguishably from forgery. So it was never
    rotated, and hardening increased the dependence on a placeholder."""
    monkeypatch.setattr(MC, "_SIGNING_KEY", "new-secret")
    monkeypatch.setattr(MC, "_SIGNING_KEY_ID", "k2")
    monkeypatch.setenv("MEMORY_SIGNING_KEYS_OLD", "k1:old-secret")

    fresh = MC.signature_for("m", "fp", "alice")
    assert fresh.startswith("k2:")
    assert MC.signature_valid("m", "fp", "alice", fresh)

    prior = "k1:" + MC._digest("old-secret", "m", "fp", "alice")
    assert MC.signature_valid("m", "fp", "alice", prior), \
        "a signature from the previous key must survive rotation"

    legacy = MC._digest("new-secret", "m", "fp", "alice")   # unprefixed
    assert MC.signature_valid("m", "fp", "alice", legacy)

    assert not MC.signature_valid("m", "fp", "alice", "k9:" + "0" * 64), \
        "an unknown key id must fail closed, not be assumed valid"
    assert not MC.signature_valid("m", "fp", "mallory", fresh)


# ===========================================================================
# 13. A "forward-only" constraint must not freeze history against erasure
# ===========================================================================

def test_legacy_rows_can_still_be_anonymised_by_an_erasure(conn):
    """`CHECK ... NOT VALID` does not grandfather existing rows. It skips the
    initial scan; every later INSERT **or UPDATE** of the row is checked. So
    132 legacy activities became unmodifiable, and erase_sp — which ANONYMISES
    activities by nulling the personal FK — raised CheckViolation for
    48 of 291 entities (16.5%). One GDPR request in six would have failed
    part-way through, after the satellite deletions had already run."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""SELECT activity_id::text, contact_id::text FROM activities
                        WHERE direction IS NULL
                          AND lower(COALESCE(type,'')) IN ('email','call','meeting')
                          AND contact_id IS NOT NULL LIMIT 1""")
        row = cur.fetchone()
        if not row:
            pytest.skip("no legacy rows without direction")
        aid, cid = row
        try:
            cur.execute("UPDATE activities SET contact_id=NULL "
                        " WHERE activity_id=%s::uuid", (aid,))
        finally:
            cur.execute("UPDATE activities SET contact_id=%s::uuid "
                        " WHERE activity_id=%s::uuid", (cid, aid))


def test_a_new_interaction_still_requires_direction(conn):
    """The rule itself is unchanged — only its blast radius. Scoping the
    trigger to `UPDATE OF direction, type` must not have quietly removed it."""
    conn.autocommit = True
    with conn.cursor() as cur:
        with pytest.raises(Exception) as e:
            cur.execute("""INSERT INTO activities (type,status,subject,
                             related_type,related_id)
                           VALUES ('call','open','pytest-probe','account',
                                   gen_random_uuid())""")
        assert "direction is required" in str(e.value)


def test_a_task_never_requires_direction(conn):
    """A task is not a communication and has no direction. Three rounds were
    spent trying to fill those NULLs before reading the stored procedures."""
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO activities (type,status,subject,related_type,
                         related_id)
                       VALUES ('task','open','pytest-task-probe','account',
                               gen_random_uuid())""")
        cur.execute("SET app.repair_key='pytest:direction-probe'")
        cur.execute("DELETE FROM activities WHERE subject='pytest-task-probe'")
        cur.execute("RESET app.repair_key")
