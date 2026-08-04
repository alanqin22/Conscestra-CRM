"""Negative controls for every observability metric.

A metric nobody has broken on purpose is a number, not a measurement. Two of the
first eight examined here were wrong in ways no amount of reading would have
shown: `gate.assertable_themes` decided assertability in a WHERE clause and
counted forged rows as assertable, and `ops.undeclared_deletions_24h` reported
14581 when the true 24-hour figure was 198.

Both looked completely plausible on a dashboard.

So this plants a specific defect, re-reads every metric, and reports which ones
moved. Three outcomes matter:

  RESPONDED    the metric moved as expected — it has a negative control
  BLIND        the metric should have moved and did not
  UNWATCHED    no metric anywhere moved — the defect is invisible to monitoring

UNWATCHED is not automatically a bug. `verify_invariants` covers the database
controls and does not need a metric duplicating it. It IS a finding when the
defect is one an operator would only ever learn about from a dashboard.

Every mutation restores itself, and the run re-reads the baseline afterwards to
prove it. A monitoring audit that leaves the corpus altered has corrupted the
thing it measures.

    python -m scripts.observability_audit
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config as _config          # noqa: E402,F401
from app.core.database import get_connection    # noqa: E402

PROBE_GENERATOR = "obsaudit/probe"


def read_metrics() -> Dict[str, Any]:
    """VALUE AND DETAIL, because some metrics carry no value at all.

    `lifecycle.decay_distribution` reports {'volatile': 177, 'episodic': 671}
    with a value of None, and `roster.reviewers` is detail-only too. A diff that
    compared values alone could never see either move, so both were reported as
    having no negative control when in fact they had no observable surface.
    A metric that cannot be compared cannot be monitored."""
    from app.core.memory_observability import snapshot
    snap = snapshot(persist=False)
    metrics, detail = snap["metrics"], snap.get("detail", {})
    return {k: (v, detail.get(k)) for k, v in metrics.items()}


def _delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Tuple]:
    moved = {}
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b is None and a is None:
            continue
        if b != a:
            moved[k] = (b, a)
    return moved


# ── Defects, each planted and undone ────────────────────────────────────────

def _new_theme(cur, **over) -> str:
    cols = {"certainty": 0.5, "visibility": "internal", "kind": "theme",
            "topic": "delivery", "actor": "customer_said"}
    cols.update(over)
    cur.execute(f"""INSERT INTO customer_memories
        (entity_type,entity_id,kind,statement,topic,occurrences,evidence,
         evidence_count,evidence_hash,source_type,certainty,generator,
         visibility,last_observed_at,actor,valid_until,contradicts)
        VALUES ('contact',gen_random_uuid(),%(kind)s,'observability probe',
                %(topic)s,3,'[]'::jsonb,0,
                'obs-'||substr(md5(random()::text),1,10),'ai',%(certainty)s,
                '{PROBE_GENERATOR}',%(visibility)s,now(),%(actor)s,
                %(valid_until)s,%(contradicts)s::uuid[])
        RETURNING memory_id::text""",
        {**cols, "valid_until": over.get("valid_until"),
         "contradicts": over.get("contradicts", [])})
    return cur.fetchone()[0]


def defect_forged_verified(cur) -> List[str]:
    """A database writer marks a theme verified with no valid signature."""
    mid = _new_theme(cur, visibility="customer")
    cur.execute("SELECT statement, evidence_hash, entity_type, entity_id::text "
                "FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
    stmt, evh, et, eid = cur.fetchone()
    cur.execute("SELECT memory_claim_hash(%s,%s)", (stmt, evh))
    claim = cur.fetchone()[0]
    cur.execute("""INSERT INTO memory_verifications
        (memory_id,action,actor_confirmed,evidence_hash,evidence_shown,
         statement_shown,performed_by,role,entity_type,entity_id)
        VALUES (%s::uuid,'verified',true,%s,1,%s,'mallory','admin',%s,%s::uuid)""",
        (mid, evh, stmt, et, eid))
    cur.execute("""UPDATE customer_memories SET kind='fact', verified_by='mallory',
                   verified_actor=true, verified_claim_hash=%s,
                   verified_evidence_hash=%s WHERE memory_id=%s::uuid""",
                (claim, evh, mid))
    return [mid]


def defect_invalid_signature(cur) -> List[str]:
    """Signature PRESENT but not computable from the key — the forgery a
    database writer can actually produce."""
    mids = defect_forged_verified(cur)
    cur.execute("""UPDATE customer_memories
                   SET verified_signature = 'k2:' || repeat('0', 64)
                   WHERE memory_id=%s::uuid""", (mids[0],))
    return mids


def defect_missing_certainty(cur) -> List[str]:
    return [_new_theme(cur, certainty=None)]


def defect_stale_theme(cur) -> List[str]:
    return [_new_theme(cur, valid_until="2020-01-01")]


def defect_contradicted_theme(cur) -> List[str]:
    a = _new_theme(cur)
    b = _new_theme(cur, contradicts=[a])
    return [a, b]


def defect_single_wording_theme(cur) -> List[str]:
    return [_new_theme(cur)]


def defect_undeclared_deletion(cur) -> List[str]:
    """Rows removed with no declared repair key — the bulk-deletion signal."""
    mid = _new_theme(cur)
    cur.execute("DELETE FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
    return []          # already gone; governed_deletions row cleaned by caller


def defect_disabled_deletion_trigger(cur) -> List[str]:
    cur.execute("ALTER TABLE content_embeddings "
                "DISABLE TRIGGER trg_content_embeddings_deletion_log")
    return []


def undo_disabled_deletion_trigger(cur) -> None:
    cur.execute("ALTER TABLE content_embeddings "
                "ENABLE TRIGGER trg_content_embeddings_deletion_log")


_ASSERTABLE_SAVED: Dict[str, Any] = {}


def probe_genuinely_assertable(cur) -> List[str]:
    """A memory that is ACTUALLY assertable — real evidence, real dual approval.

    gate.assertable_themes reads 0.0 in every environment examined, and a metric
    pinned at zero is indistinguishable from a broken one. Proving it can count
    is the only way to know the corrected implementation works.

    A SYNTHETIC theme cannot be used: verify() refuses one whose actor is not
    supported by evidence ("the statement's actor does not match its evidence"),
    which is the verification path doing its job. So this borrows a real
    evidence-backed memory and puts every field back afterwards.
    """
    from app.core import memory_consolidation as MC
    # The subject must be able to PASS the gate, not merely have the most
    # evidence. Selecting by evidence_count alone picked a memory whose
    # certainty was 0.440 — below the assertion floor — so dual approval
    # completed and the metric still read 0, and the probe reported the metric
    # blind when the metric was right. A negative control that depends on
    # ambient data selection is a negative control that fails at random.
    from app.core.memory_consolidation import ASSERT_FLOOR
    cur.execute("""SELECT memory_id::text, visibility, kind, certainty
                     FROM customer_memories
                    WHERE status='active' AND evidence_count > 0
                      AND certainty >= %s
                      AND generator LIKE 'memory_consolidation%%'
                    ORDER BY evidence_count DESC LIMIT 1""", (ASSERT_FLOOR,))
    row = cur.fetchone()
    if not row:
        return []
    mid, vis, kind, cert = row
    _ASSERTABLE_SAVED.update(mid=mid, visibility=vis, kind=kind, certainty=cert)
    cur.execute("UPDATE customer_memories SET visibility='customer' "
                "WHERE memory_id=%s::uuid", (mid,))
    # TWO DISTINCT verifiers: high-consequence topics require dual approval, and
    # one approval leaves the claim pending rather than assertable.
    for who in ("obs-audit-a", "obs-audit-b"):
        pv = MC.verification_preview(mid)
        if pv.get("ok"):
            MC.verify(mid, verified_by=who, role="admin",
                      acknowledged_evidence_hash=pv.get("evidence_hash"),
                      actor_confirmed=True)
    return []


def undo_genuinely_assertable(cur) -> None:
    st = _ASSERTABLE_SAVED
    if not st:
        return
    cur.execute("SET app.repair_key = 'observability-audit:revert'")
    cur.execute("SET app.erasure_reason = 'observability-audit:harness'")
    cur.execute("SELECT erase_memory_verifications(ARRAY[%s]::uuid[])", (st["mid"],))
    cur.execute("""UPDATE customer_memories
                      SET visibility=%s, kind=%s, certainty=%s, verified_by=NULL,
                          verified_actor=false, verified_claim_hash=NULL,
                          verified_signature=NULL, verified_evidence_hash=NULL,
                          verified_at=NULL, verification_expires_at=NULL
                    WHERE memory_id=%s::uuid""",
                (st["visibility"], st["kind"], st["certainty"], st["mid"]))
    cur.execute("RESET app.repair_key")
    st.clear()


def probe_reliability_recorded(cur) -> List[str]:
    """trust.mean_reliability and reliability_measured_share are NULL/0 while no
    evidence source carries a confidence score. Untested at any other value."""
    mid = _new_theme(cur)
    cur.execute("UPDATE customer_memories SET reliability = 0.42 "
                "WHERE memory_id = %s::uuid", (mid,))
    return [mid]


def probe_indexed_record(cur) -> List[str]:
    """corpus.indexed_records counts content_embeddings; never exercised."""
    cur.execute("""INSERT INTO content_embeddings
        (source_type, source_id, content_hash, snippet, visibility, occurred_at,
         embedding, model, dims, contact_id, account_id, party_key)
        SELECT 'activity', 'obsaudit-' || substr(md5(random()::text),1,10),
               md5(random()::text), 'observability probe', 'internal', now(),
               embedding, model, dims, contact_id, account_id, party_key
          FROM content_embeddings LIMIT 1
        RETURNING source_id""")
    row = cur.fetchone()
    return [f"embedding:{row[0]}"] if row else []



_SHADOW_PAIRS: List[str] = []


def probe_shadow_pair_reviewed(cur) -> List[str]:
    """A captured shadow pair, then a human verdict on it.

    Six shadow.* metrics gate the autonomy decision — whether the system may
    ever answer without a human — and none had been shown capable of moving.
    A readiness signal that cannot change is a permanent 'not ready', which is
    safe, and a permanent 'ready' if it ever inverts, which is not.
    """
    from app.core import shadow_eval as SH
    cur.execute("SELECT entity_type, entity_id::text FROM customer_memories "
                "WHERE status='active' LIMIT 1")
    row = cur.fetchone()
    if not row:
        return []
    et, eid = row
    rec = SH.capture_pair(et, eid, "observability audit probe",
                          lambda mems: "probe answer " + ("with" if mems else "without"),
                          audience="internal", channel="observability_audit")
    pid = rec.get("pair_id")
    if not pid:
        return []
    _SHADOW_PAIRS.append(pid)
    # A reviewer name unique to this run. Reusing a name that is already in
    # the roster leaves roster.reviewers unchanged, so the metric looks
    # unguarded when it is really just being handed a value it already has.
    SH.review_pair(pid, "accepted", reviewed_by=f"obs-audit-{uuid.uuid4().hex[:8]}")
    return []


def undo_shadow_pair(cur) -> None:
    if not _SHADOW_PAIRS:
        return
    cur.execute("SET app.repair_key = 'observability-audit:shadow'")
    for pid in _SHADOW_PAIRS:
        # Shadow arms are rows in agent_utterances, not a table of their own.
        cur.execute("DELETE FROM agent_utterances WHERE pair_id = %s::uuid", (pid,))
    cur.execute("RESET app.repair_key")
    _SHADOW_PAIRS.clear()


def probe_shadow_unsafe_and_identical(cur) -> List[str]:
    """A HARMFUL verdict, and a pair whose two arms are identical.

    shadow.unsafe is the metric that must veto autonomy, and
    shadow.identical_rate is the one that says memory changed nothing. Neither
    had ever been observed at a non-constant value, so neither was known to be
    capable of objecting."""
    from app.core import shadow_eval as SH
    cur.execute("SELECT entity_type, entity_id::text FROM customer_memories "
                "WHERE status='active' LIMIT 1")
    row = cur.fetchone()
    if not row:
        return []
    et, eid = row
    # Identical arms: the answer ignores the memory list entirely.
    rec = SH.capture_pair(et, eid, "observability audit unsafe probe",
                          lambda mems: "identical regardless of memory",
                          audience="internal", channel="observability_audit")
    pid = rec.get("pair_id")
    if not pid:
        return []
    _SHADOW_PAIRS.append(pid)
    SH.review_pair(pid, "harmful", reviewed_by="obs-audit")
    return []


def probe_eval_label(cur) -> List[str]:
    """A recorded reviewer label. labels.count and labels.double_labelled feed
    the inter-rater agreement that decides whether any eval number is
    trustworthy; both sat at a constant."""
    cur.execute("""SELECT memory_id::text FROM customer_memories
                    WHERE status='active' LIMIT 1""")
    row = cur.fetchone()
    if not row:
        return []
    for who in ("obs-audit-r1", "obs-audit-r2"):     # two -> double_labelled
        cur.execute("""INSERT INTO memory_eval_labels
              (memory_id, labelled_by, topic_correct, actor_correct,
               cluster_coherent, useful, instrument_version)
              VALUES (%s::uuid, %s, true, true, true, true, 2)
              ON CONFLICT DO NOTHING""", (row[0], who))
    return []


def undo_eval_label(cur) -> None:
    cur.execute("DELETE FROM memory_eval_labels "
                "WHERE labelled_by LIKE 'obs-audit-r%'")


_EVAL_SAVED: Dict[str, Any] = {}


def _first_sampled(cur, extra: str = "") -> Optional[str]:
    """The row the eval sample is GUARANTEED to include.

    The eval measures take `ORDER BY md5(memory_id::text) LIMIT 300` over ~848
    active memories, so a freshly inserted probe row lands in the sample only
    about a third of the time. A probe that works intermittently is worse than
    none: it reports 'no negative control' at random. Mutating the first row in
    that same ordering removes the coin flip."""
    cur.execute(f"""SELECT memory_id::text FROM customer_memories
                     WHERE status='active'
                       AND left(generator, 7) <> 'pytest/'
                       AND left(generator, 6) <> 'probe/' {extra}
                     ORDER BY md5(memory_id::text) LIMIT 1""")
    row = cur.fetchone()
    return row[0] if row else None


def probe_count_inconsistency(cur) -> List[str]:
    """`occurrences` no longer equals the occasions actually cited.

    This is the 385-vs-22 incident in miniature: a sentence a human reads
    asserting a number the evidence does not support."""
    mid = _first_sampled(cur, "AND NOT truncated AND evidence_count > 0")
    if not mid:
        return []
    cur.execute("SELECT occurrences FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
    _EVAL_SAVED["count"] = (mid, cur.fetchone()[0])
    cur.execute("UPDATE customer_memories SET occurrences = occurrences + 97 "
                "WHERE memory_id=%s::uuid", (mid,))
    return []


def undo_count_inconsistency(cur) -> None:
    st = _EVAL_SAVED.pop("count", None)
    if st:
        cur.execute("UPDATE customer_memories SET occurrences=%s "
                    "WHERE memory_id=%s::uuid", (st[1], st[0]))


def probe_unresolvable_evidence(cur) -> List[str]:
    """A memory citing a source that no longer exists — the retention hole that
    let a claim keep its number after its evidence was deleted."""
    mid = _first_sampled(cur, "AND evidence_count > 0")
    if not mid:
        return []
    cur.execute("SELECT evidence FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
    ev = cur.fetchone()[0]
    _EVAL_SAVED["evidence"] = (mid, json.dumps(ev) if not isinstance(ev, str) else ev)
    cur.execute("""UPDATE customer_memories
                      SET evidence = evidence || '[{"source_type":"activity",
                          "source_id":"obsaudit-does-not-exist"}]'::jsonb
                    WHERE memory_id=%s::uuid""", (mid,))
    return []


def undo_unresolvable_evidence(cur) -> None:
    st = _EVAL_SAVED.pop("evidence", None)
    if st:
        cur.execute("UPDATE customer_memories SET evidence=%s::jsonb "
                    "WHERE memory_id=%s::uuid", (st[1], st[0]))


def probe_misattributed_record(cur) -> List[str]:
    """A record whose observed direction contradicts what the rule infers.

    actor_accuracy withholds `direction` from actor_for and then compares, so a
    row carrying outbound direction with customer-speech wording is a genuine
    disagreement — the shape of the 14 wrong third-party attributions."""
    cur.execute("""INSERT INTO content_embeddings
        (source_type, source_id, content_hash, snippet, visibility, occurred_at,
         embedding, model, dims, contact_id, account_id, party_key, direction)
        SELECT 'activity', 'obsaudit-attr-' || substr(md5(random()::text),1,8),
               md5(random()::text),
               'The customer said they would call us back about the invoice.',
               'internal', now(), embedding, model, dims,
               contact_id, account_id, party_key, 'outbound'
          FROM content_embeddings LIMIT 1""")
    return []


def undo_misattributed_record(cur) -> None:
    cur.execute("DELETE FROM content_embeddings "
                "WHERE source_id LIKE 'obsaudit-attr-%'")


def defect_erased_audit_trail(cur) -> List[str]:
    """The sanctioned GDPR path used as a cover-up."""
    mids = defect_forged_verified(cur)
    cur.execute("SET app.erasure_reason = 'observability-audit:probe'")
    cur.execute("SET app.erasure_reason = 'observability-audit:harness'")
    cur.execute("SELECT erase_memory_verifications(ARRAY[%s]::uuid[])", (mids[0],))
    return mids


# name -> (plant, expected metric prefixes, optional explicit undo)
DEFECTS: List[Tuple[str, Callable, Tuple[str, ...], Optional[Callable]]] = [
    ("forged verified row",        defect_forged_verified,
     ("gate.verified_but_refused",), None),
    ("invalid signature",          defect_invalid_signature,
     ("gate.verified_but_refused",), None),
    ("missing certainty",          defect_missing_certainty,
     ("trust.certainty_measured_share",), None),
    ("stale theme past valid_until", defect_stale_theme,
     ("lifecycle.stale_themes",), None),
    ("contradicted theme",         defect_contradicted_theme,
     ("quality.contradicted_themes",), None),
    ("single-wording theme",       defect_single_wording_theme,
     ("corpus.single_wording_share", "corpus.active_themes"), None),
    ("undeclared deletion",        defect_undeclared_deletion,
     ("ops.undeclared_deletions_24h",), None),
    ("deletion trigger disabled",  defect_disabled_deletion_trigger,
     ("ops.deletion_logs_armed",), undo_disabled_deletion_trigger),
    ("audit trail erased",         defect_erased_audit_trail,
     (), None),
    ("a genuinely assertable memory", probe_genuinely_assertable,
     ("gate.assertable_themes",), undo_genuinely_assertable),
    ("reliability actually recorded", probe_reliability_recorded,
     ("trust.mean_reliability", "trust.reliability_measured_share"), None),
    ("a newly indexed record",      probe_indexed_record,
     ("corpus.indexed_records",), None),
    ("a reviewed shadow pair",      probe_shadow_pair_reviewed,
     ("shadow.pairs", "shadow.reviewed", "shadow.accepted"), undo_shadow_pair),
    ("two reviewer labels",         probe_eval_label,
     ("labels.count", "labels.double_labelled"), undo_eval_label),
    ("an unsafe, identical pair",   probe_shadow_unsafe_and_identical,
     ("shadow.unsafe", "shadow.identical_rate"), undo_shadow_pair),
    ("a miscounted memory",         probe_count_inconsistency,
     ("eval.count_consistency",), undo_count_inconsistency),
    ("evidence that no longer exists", probe_unresolvable_evidence,
     ("eval.evidence_resolvable",), undo_unresolvable_evidence),
    ("a misattributed record",      probe_misattributed_record,
     ("eval.actor_accuracy",), undo_misattributed_record),
]


def _cleanup(cur) -> None:
    """Remove every probe row and the trace of removing it."""
    cur.execute("SET app.repair_key = 'observability-audit:cleanup'")
    cur.execute(f"""SELECT memory_id::text FROM customer_memories
                     WHERE generator = '{PROBE_GENERATOR}'""")
    for (mid,) in cur.fetchall():
        # Label it: the register is permanent, so an undeclared harness erasure
        # is indistinguishable from a real subject request, forever.
        cur.execute("SET app.erasure_reason = 'observability-audit:harness'")
        cur.execute("SELECT erase_memory_verifications(ARRAY[%s]::uuid[])", (mid,))
        cur.execute("DELETE FROM customer_memories WHERE memory_id=%s::uuid", (mid,))
    cur.execute("DELETE FROM content_embeddings WHERE snippet = 'observability probe'")
    cur.execute(f"""DELETE FROM governed_deletions
                     WHERE old_row->>'generator' = '{PROBE_GENERATOR}'""")
    # memory_erasure_log is DELIBERATELY not cleaned.
    #
    # The first version of this tried, and the register refused: "the record
    # that an erasure happened is not itself erasable — that is the whole
    # point." That is the control working, including against the auditor, and
    # the harness is what should bend.
    #
    # So each run of the "audit trail erased" probe leaves one register row
    # saying an erasure occurred, attributed to this script. That is the
    # correct outcome: a permanent, attributable record of every erasure,
    # including deliberate ones. It is metadata only — no statement text, no
    # evidence, no personal data.
    cur.execute("RESET app.repair_key")
    cur.execute("RESET app.erasure_reason")


def main() -> int:
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    _cleanup(cur)                       # start from a known state

    # THE AUDIT'S OWN NEGATIVE CONTROL.
    #
    # Read twice, change nothing. Anything that differs is nondeterministic —
    # `eval.count_consistency` and `eval.evidence_resolvable` carry sampled
    # detail that varies per call — and would otherwise be counted as "moved"
    # under every probe, manufacturing negative controls that do not exist.
    #
    # This is the same failure the mutation harness already had with stale
    # bytecode: an instrument reporting more coverage than it measured. It is
    # the direction that feels safe and is not.
    baseline = read_metrics()
    unstable = set(_delta(baseline, read_metrics()))
    if unstable:
        print(f"  UNSTABLE — differ between two identical reads, so they cannot "
              f"serve as a signal at all:")
        for k in sorted(unstable):
            print(f"    {k}")
        print()
    print(f"OBSERVABILITY NEGATIVE CONTROLS — {len(baseline)} metrics\n")

    blind, unwatched = [], []
    # Which METRICS ever move? A defect being detected does not mean every
    # metric has a negative control — most moved because one probe row changed
    # a corpus count. A metric that never moves under ANY planted defect has
    # not been shown capable of failing at all.
    ever_moved: set = set()
    for name, plant, expect, undo in DEFECTS:
        plant(cur)
        after = read_metrics()
        moved = _delta(baseline, after)

        if undo:
            undo(cur)
        _cleanup(cur)

        moved = {k: v for k, v in moved.items() if k not in unstable}
        ever_moved.update(moved)
        responded = [k for k in expect if k in moved]
        if not moved:
            unwatched.append(name)
            verdict = "UNWATCHED"
        elif expect and not responded:
            blind.append((name, expect, sorted(moved)))
            verdict = "*** BLIND ***"
        else:
            verdict = "RESPONDED"
        movers = ", ".join(f"{k} {moved[k][0][0]}->{moved[k][1][0]}"
                           for k in sorted(moved)[:3]) or "nothing moved"
        print(f"  {verdict:14} {name}")
        print(f"                 {movers[:110]}")

    # METRICS BACKED BY AN APPEND-ONLY REGISTER CANNOT BE RESTORED.
    #
    # `ops.undeclared_erasures` counts rows in memory_erasure_log, which refuses
    # DELETE by design — including from this harness, which is the control
    # working. Every audit run legitimately and permanently adds erasure events,
    # so its detail can never return to the baseline.
    #
    # Reporting that as residue would make "restored cleanly: False" the normal
    # outcome, and a warning that is always on is one nobody reads. It is
    # excluded and named, not silently dropped.
    MONOTONIC = {"ops.undeclared_erasures",
                 # 24h-windowed counts of deletions. This audit deletes probe
                 # rows, so it legitimately raises them and they only fall when
                 # the window rolls — never within a run.
                 "ops.undeclared_deletions_24h",
                 "ops.undeclared_deletion_txns_24h"}

    restored = read_metrics()
    # Unstable metrics differ between any two reads by definition; including
    # them would report residue on every clean run and train the reader to
    # ignore the line that matters.
    drift = {k: v for k, v in _delta(baseline, restored).items()
             if k not in unstable and k not in MONOTONIC}
    print(f"\n  restored cleanly: {not drift}"
          + ("" if not drift else f"  LEFTOVER: {sorted(drift)}"))

    # COVERAGE IS PER METRIC, NOT PER DEFECT.
    #
    # Nine defects being detected does not mean 31 metrics are guarded. Most of
    # the movement above is one probe row nudging a corpus count. A metric that
    # never moves under ANY planted defect has not been shown capable of
    # failing, which is the property this whole exercise exists to establish.
    never = sorted(k for k in baseline if k not in ever_moved)
    if never:
        print(f"\n  {len(never)} of {len(baseline)} metric(s) NEVER moved under "
              f"any planted defect —")
        print("  no negative control has been demonstrated for these:")
        for k in never:
            print(f"    {k}")

    if blind:
        print(f"\n  {len(blind)} metric(s) BLIND to the defect they name:")
        for name, expect, moved in blind:
            print(f"    {name}: expected {expect}, moved {moved}")
    if unwatched:
        print(f"\n  {len(unwatched)} defect(s) invisible to ALL metrics:")
        for name in unwatched:
            print(f"    {name}")

    conn.close()
    return 1 if (blind or drift) else 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
