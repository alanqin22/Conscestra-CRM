"""Phase 3 — scientific evaluation of Customer Memory.

Eleven review rounds established that a green test suite says nothing about
whether the memory layer is ACCURATE. Tests assert that code does what it was
written to do. None of them ask whether topic classification is right, whether
a cluster is a real theme, or whether `confidence` predicts anything.

TWO KINDS OF METRIC, KEPT APART ON PURPOSE.

  INTRINSIC — computable from the database alone, today. Determinism, evidence
  resolvability, internal consistency, attribution against observed direction.
  These are real numbers and they are reported as such.

  EXTRINSIC — require a human label: topic precision, cluster validity,
  usefulness. There is NO way to compute these without labels, and this module
  will not invent one. It emits the labelling task and reports
  `insufficient_labels` until enough exist. A suite that quietly substitutes a
  proxy for the thing it cannot measure is how "coverage" got reported where
  precision was the question.

THRESHOLDS ARE DECLARED BEFORE THE RUN, in `THRESHOLDS` below, so that a
failing metric cannot be reinterpreted after the fact.

    python -m app.core.memory_eval                     # run what is runnable
    python -m app.core.memory_eval --labels --out t.json  # emit the task
    python -m app.core.memory_eval --review t.json --by NAME   # label it
    python -m app.core.memory_eval --status            # how far from a result
"""

from __future__ import annotations

import json
import logging
import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.database import get_connection

logger = logging.getLogger("memory_eval")

# ── Pass/fail bars, declared in advance ─────────────────────────────────────
# Set from what the system must achieve to be trusted, not from what it
# currently scores. Any change here is a code change someone reviews.
THRESHOLDS: Dict[str, float] = {
    # A rerun over unchanged evidence must produce identical memories, or a
    # human verification can be invalidated by nothing but a re-run.
    "determinism": 1.00,
    # Evidence pointers that no longer resolve mean the claim's support is gone.
    "evidence_resolvable": 0.95,
    # `occurrences` must equal the distinct occasions actually cited.
    "count_consistency": 1.00,
    # Attribution, measured only against records carrying an OBSERVED
    # direction. 0.90 because a wrong actor states something false about a
    # person — the standard that got the 64.3% heuristic withdrawn.
    "actor_accuracy": 0.90,
    # PRECISION ALONE IS GAMEABLE: abstaining from every record scores 1.000.
    # That is not hypothetical — fixing a third-party false positive raised
    # precision from 0.8955 to 1.000 while abstentions rose by exactly the 14
    # records that had been wrong. The errors were removed, not corrected, and
    # the headline number improved for it. Coverage is now scored too, so the
    # degenerate strategy fails the suite instead of topping it.
    # 1 in 20. A rule that commits on less than that is not doing work,
    # whatever its precision. Chosen as a "is this rule doing anything"
    # floor rather than from the observed value — DISCLOSED, because it was
    # set after seeing 0.076 and that ordering is exactly the bias this
    # project keeps finding in itself. It is not a quality bar; the quality
    # bar is `actor_accuracy`, and PRODUCTION coverage is reported separately
    # below because production passes `direction` and this metric withholds it.
    "actor_coverage": 0.05,
    # Extrinsic. Enforced once labels exist.
    "topic_precision": 0.85,
    "cluster_precision": 0.80,
    "reviewer_agreement_kappa": 0.60,
}

# Minimum labelled items before an extrinsic metric is reported at all.
# Below this the confidence interval is wider than the quantity of interest.
MIN_LABELS = 100

# Which labelling instrument is current. Labels from an older one are kept as
# evidence ABOUT the instrument but never used as ground truth.
#
#   v1  evidence shown as {source_type, source_id, on_date} — ids and dates —
#       while asking "is the topic right FOR THIS EVIDENCE?". The evidence was
#       never shown. 240 labels, kappa -0.026 on topic: worse than chance.
#   v2  shows the indexed TEXT of every cited record.
INSTRUMENT_VERSION = 2
_CURRENT = f"instrument_version = {INSTRUMENT_VERSION}"

# Rows this suite must ignore. The first run measured 7 `count_consistency`
# failures that were all pytest fixtures ("Competing claim.", evidence_count=0)
# left in the shared database by tests. Evaluating a production metric over a
# corpus contaminated with synthetic rows measures the fixtures, not the
# system — the same ambient-state problem that let a test hardcode
# entity_type='contact' and pass for months.
#
# No `%` in this predicate ON PURPOSE. psycopg2 treats `%` as a parameter
# placeholder, so `LIKE 'pytest/%'` raises "tuple index out of range" in a
# parameterised query — and escaping it as `%%` then breaks the one query here
# that passes no parameters. left() sidesteps both.
EXCLUDED_GENERATORS = ("left(generator, 7) <> 'pytest/' "
                       "AND left(generator, 6) <> 'probe/'")


def wilson(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval. Used instead of the normal approximation because
    these proportions sit near 0 or 1 with small n, where the normal interval
    produces bounds outside [0, 1] and reads as more certain than it is."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def cohens_kappa(a: List[str], b: List[str]) -> Optional[float]:
    """Agreement beyond chance between two reviewers.

    Raw agreement is misleading when one label dominates: two reviewers who
    both answer 'accepted' 95% of the time agree 90% of the time by accident."""
    if not a or len(a) != len(b):
        return None
    n = len(a)
    labels = sorted(set(a) | set(b))
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    expected = sum((a.count(l) / n) * (b.count(l) / n) for l in labels)
    if expected >= 1.0:
        return None
    return (observed - expected) / (1 - expected)


# ============================================================================
# INTRINSIC — real numbers, no labels needed
# ============================================================================

def measure_determinism(sample: int = 60) -> Dict[str, Any]:
    """Re-consolidate an entity twice; the evidence hashes must be identical.

    Non-determinism here is not cosmetic: evidence_hash is what invalidates a
    human verification, so a reordering silently un-verifies claims a person
    approved."""
    from app.core import memory_consolidation as MC
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT DISTINCT entity_type, entity_id::text
                             FROM customer_memories
                            WHERE status='active' AND {EXCLUDED_GENERATORS}
                            LIMIT %s""", (int(sample),))
            ents = cur.fetchall()
    finally:
        conn.close()
    if not ents:
        return {"metric": "determinism", "n": 0, "status": "no_data"}

    stable = 0
    for etype, eid in ents:
        snaps = []
        for _ in range(2):
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    recs, _ = MC._load_records(cur, etype, eid)
                    idxs = MC._cluster(recs)
                    snaps.append([
                        MC._evidence_hash(
                            [f"{recs[i]['source_type']}:{recs[i]['source_id']}"
                             for i in idx])
                        for idx in idxs])
            finally:
                conn.close()
        if snaps[0] == snaps[1]:
            stable += 1
    return _score("determinism", stable, len(ents))


def measure_evidence_resolvable(sample: int = 300) -> Dict[str, Any]:
    """Do the records a memory cites still exist?

    A memory whose evidence has been deleted still asserts its count. This was
    a proven hole: retention expired a source and the claim kept its number."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # DETERMINISTIC SAMPLE, not a random one.
            #
            # `ORDER BY random()` made this metric un-trendable: two runs
            # against identical data drew different rows, so a fall from 1.00
            # to 0.98 could be a regression or could be the sample. Measured —
            # consecutive reads reported n=2700 then n=2614 with nothing
            # changed. A number that moves on its own cannot be alerted on, and
            # an alert nobody can trust is one nobody reads.
            #
            # Hashing the primary key gives a stable pseudo-random subset: the
            # same rows every run, and a DIFFERENT subset only when the corpus
            # itself changes — which is the one time the sample should move.
            cur.execute(f"""SELECT evidence FROM customer_memories
                            WHERE status='active' AND evidence_count > 0
                              AND {EXCLUDED_GENERATORS}
                            ORDER BY md5(memory_id::text) LIMIT %s""",
                        (int(sample),))
            rows = cur.fetchall()
            total = resolved = 0
            for (evidence,) in rows:
                items = evidence if isinstance(evidence, list) else json.loads(evidence or "[]")
                for it in items:
                    total += 1
                    cur.execute("""SELECT 1 FROM content_embeddings
                                    WHERE source_type=%s AND source_id=%s LIMIT 1""",
                                (it.get("source_type"), it.get("source_id")))
                    if cur.fetchone():
                        resolved += 1
    finally:
        conn.close()
    return _score("evidence_resolvable", resolved, total)


def measure_count_consistency(sample: int = 300) -> Dict[str, Any]:
    """`occurrences` is asserted in the sentence a human reads. It must equal
    the number of distinct occasions actually cited — the 385-vs-22 incident
    was exactly this quantity being wrong."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Deterministic sample — see measure_evidence_resolvable.
            cur.execute(f"""SELECT occurrences, evidence_count, truncated
                             FROM customer_memories
                            WHERE status='active' AND {EXCLUDED_GENERATORS}
                            ORDER BY md5(memory_id::text) LIMIT %s""",
                        (int(sample),))
            rows = cur.fetchall()
    finally:
        conn.close()
    # evidence is capped at MAX_EVIDENCE, so equality is only required when the
    # cap was not reached and the window was not clipped.
    from app.core.memory_consolidation import MAX_EVIDENCE
    checked = ok = 0
    for occ, ev_count, trunc in rows:
        if trunc or ev_count >= MAX_EVIDENCE:
            continue
        checked += 1
        if occ == ev_count:
            ok += 1
    return _score("count_consistency", ok, checked)


def measure_actor_accuracy() -> Dict[str, Any]:
    """Attribution against records that carry an OBSERVED direction.

    Internal work items are excluded: `direction` is demonstrably arbitrary for
    them in this data — the same subject appears labelled both inbound and
    outbound — and measuring against a field with two labels for one event
    measures noise. That mistake produced a 53.3% figure that was acted on."""
    from app.core.content_index import actor_for
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT ce.snippet, ce.direction, ce.source_type, a.type
                             FROM content_embeddings ce
                             LEFT JOIN activities a
                               ON ce.source_type='activity'
                              AND a.activity_id::text = ce.source_id
                            WHERE ce.direction IS NOT NULL
                              AND lower(COALESCE(a.type,'')) NOT IN
                                  ('task','note','todo','reminder')""")
            rows = cur.fetchall()
    finally:
        conn.close()
    truth = {"inbound": {"customer_said", "customer_did"},
             "outbound": {"company_did"}}
    agree = judged = 0
    for snippet, direction, stype, atype in rows:
        guess = actor_for(None, stype, snippet or "", atype)   # direction withheld
        if guess in ("unknown", None):
            continue
        judged += 1
        if guess in truth.get(direction, set()):
            agree += 1
    out = _score("actor_accuracy", agree, judged)
    out["abstained"] = len(rows) - judged
    out["ground_truth_rows"] = len(rows)
    out["note"] = ("precision over records the rule COMMITS on. Abstention is "
                   "the correct behaviour when unsure — an unattributed memory "
                   "says less, a wrongly attributed one says something false "
                   "about a person — but precision alone rewards abstaining "
                   "from everything, so coverage is scored beside it.")
    # Deliberately measured with `direction` WITHHELD, so this is the harder
    # counterfactual: can the actor be recovered from text and schema alone?
    # Production passes direction, so live coverage is far higher.
    out["coverage"] = _score("actor_coverage", judged, len(rows))
    return out


def measure_production_attribution() -> Dict[str, Any]:
    """What fraction of INDEXED records actually carry an actor.

    `actor_accuracy` withholds `direction` on purpose, to test whether the text
    and schema cues alone can recover the actor. Production does not withhold
    it, so the counterfactual's 7.6% coverage would badly understate the live
    system. This is the operational number."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE actor IS NOT NULL
                                                     AND actor <> 'unknown'),
                                  count(*)
                             FROM content_embeddings""")
            attributed, total = cur.fetchone()
    finally:
        conn.close()
    out = _score("production_attribution", attributed or 0, total or 0)
    out["note"] = ("stored attribution across the live index, with direction "
                   "available as it is in production")
    return out


def _score(metric: str, hits: int, n: int) -> Dict[str, Any]:
    """Score a proportion against its declared bar.

    TWO TESTS, because one does not fit both cases — and using the wrong one
    was a bug in this file's first run:

      bar < 1  judge the Wilson LOWER bound. Passing on a point estimate whose
               interval straddles the bar is how a small sample gets promoted
               into a claim.

      bar == 1 judge observed failures. A lower confidence bound is ALWAYS
               below 1 at finite n, so `lo >= 1.0` can never hold — the first
               run reported determinism as FAILING at a measured value of
               1.000 with zero failures. For a zero-defect bar the meaningful
               statistic is the rule-of-three upper bound on the failure rate,
               reported here so a small n reads as weak evidence rather than
               as a pass.
    """
    if n == 0:
        return {"metric": metric, "n": 0, "status": "no_data"}
    value = hits / n
    lo, hi = wilson(hits, n)
    bar = THRESHOLDS.get(metric)
    out = {"metric": metric, "n": n, "value": round(value, 4),
           "ci95": [round(lo, 4), round(hi, 4)], "threshold": bar}
    if bar is None:
        out["status"] = "reported"
    elif bar >= 1.0:
        failures = n - hits
        out["failures"] = failures
        # Rule of three: with 0 failures in n trials the true rate is <= ~3/n
        # at 95% confidence.
        out["max_failure_rate_95"] = round(3.0 / n, 4) if failures == 0 else None
        out["status"] = "pass" if failures == 0 else "fail"
    else:
        out["status"] = "pass" if lo >= bar else "fail"
    return out


# ============================================================================
# EXTRINSIC — needs human labels; will not be faked
# ============================================================================

def _with_text(evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach the indexed snippet to each evidence pointer.

    Without this the labelling question is unanswerable: a reviewer cannot say
    whether `billing` is the right topic for records they have not read."""
    if not evidence:
        return evidence
    keys = [(e.get("source_type"), e.get("source_id")) for e in evidence]
    text: Dict[tuple, str] = {}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT source_type, source_id, snippet
                         FROM content_embeddings
                        WHERE (source_type, source_id) IN (
                              SELECT unnest(%s::text[]), unnest(%s::text[]))""",
                    ([k[0] for k in keys], [k[1] for k in keys]))
                for st, sid, sn in cur.fetchall():
                    text[(st, sid)] = (sn or "")[:300]
        finally:
            conn.close()
    except Exception as exc:                             # noqa: BLE001
        logger.warning(f"[eval] evidence text unavailable: {exc}")
    out = []
    for e in evidence:
        item = dict(e)
        item["text"] = text.get((e.get("source_type"), e.get("source_id")))
        out.append(item)
    return out


def labelling_task(n: int = 120, seed: int = 20260801) -> Dict[str, Any]:
    """A stratified sample of memories for a human to label.

    Stratified by actor and topic so rare classes are represented; a uniform
    sample of this corpus would be 92% company_did and say nothing about the
    customer-voice classes that matter most."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""SELECT memory_id::text, entity_type, entity_id::text,
                                  topic, actor, statement, occurrences, evidence
                             FROM customer_memories
                            WHERE status='active' AND {EXCLUDED_GENERATORS}""")
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return {"ok": False, "reason": "no memories"}

    strata: Dict[Tuple[str, str], List[Any]] = {}
    for r in rows:
        strata.setdefault((r[4] or "unknown", r[3]), []).append(r)
    rng = random.Random(seed)
    per = max(1, n // max(1, len(strata)))
    picked: List[Any] = []
    leftover: List[Any] = []
    for key in sorted(strata):
        group = strata[key][:]
        rng.shuffle(group)
        picked.extend(group[:per])
        leftover.extend(group[per:])
    # TOP UP. Small strata cannot supply `per` items, so the stratified pass
    # alone returned 75 for a request of 120 — and MIN_LABELS is 100, so the
    # tool was asking for more labels than one task could ever provide. Rare
    # classes are already represented by the pass above; the remainder is
    # filled from the pool so the sample actually reaches the size requested.
    rng.shuffle(leftover)
    picked.extend(leftover[:max(0, n - len(picked))])
    rng.shuffle(picked)

    return {
        "ok": True, "seed": seed, "strata": len(strata), "items": [
            {"memory_id": r[0], "entity": f"{r[1]}:{r[2]}",
             "statement": r[5], "occurrences": r[6],
             # WITH TEXT. The first instrument shipped evidence as
             # {source_type, source_id, on_date} — UUIDs and dates — and then
             # asked "is the assigned topic right FOR THIS EVIDENCE?". The
             # evidence was never shown. Two reviewers labelled 120 items each
             # under that question and produced kappa = -0.026 on topic: raw
             # agreement 94%, agreement beyond chance NONE, because both were
             # guessing from the sentence and both defaulted to yes.
             "evidence": _with_text(
                 r[7] if isinstance(r[7], list) else json.loads(r[7] or "[]")),
             # FILL THESE IN: true / false / null. Nothing is pre-filled —
             # showing the model's answer beside the question is how a reviewer
             # ends up confirming rather than judging. null stays distinct from
             # false: "not answered" must never count as "wrong".
             "answers": {
                 "topic_correct": None,     # is the assigned topic right?
                 "actor_correct": None,     # is the actor right?
                 "cluster_coherent": None,  # do the cited records describe ONE theme?
                 "useful": None,            # would this change what a rep does?
             }}
            for r in picked[:n]],
        "instructions": (
            "Judge each statement against its evidence only. The topic and "
            "actor the system assigned are omitted deliberately. Set each "
            "`answers` field to true or false (leave null if you cannot tell), "
            "save the file, then run:  python -m app.core.memory_eval "
            "--ingest FILE --by YOUR_NAME . Generate with --out FILE "
            "rather than shell redirection: PowerShell writes UTF-16 and "
            "the file will not read back. TWO reviewers should label the "
            "SAME sample (same seed) or inter-reviewer agreement cannot be "
            "computed and every precision figure rests on one opinion."),
    }


def _read_json(path: str) -> Dict[str, Any]:
    """Read a task file whatever the shell wrote it as.

    `python -m app.core.memory_eval --labels > task.json` in Windows
    PowerShell 5.1 writes **UTF-16 LE with a BOM**, so a plain
    open(..., encoding="utf-8") fails on the very first byte:

        UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0

    The instructions told the user to use `>`, so handling what `>` produces is
    this tool's job, not theirs. BOMs are checked explicitly rather than guessed
    at, and utf-8 is tried last because a UTF-16 file often decodes as utf-8
    without raising — it just produces text full of NUL characters, which would
    fail later and much less legibly."""
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    elif b"\x00" in raw[:64]:
        # UTF-16 written with no BOM (PowerShell's Out-File -Encoding unicode
        # on some hosts). ASCII JSON in UTF-16 is every other byte NUL.
        text = raw.decode("utf-16-le" if raw[1:2] == b"\x00" else "utf-16-be")
    else:
        text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{path} is not valid JSON ({exc}).\n"
            "If you edited it by hand, check that every `answers` value is "
            "true, false or null — not yes/no and not quoted.") from exc


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    """Write UTF-8 ourselves, so the shell's default encoding never applies."""
    Path(path).write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")


def record_labels(task, labelled_by: str) -> Dict[str, Any]:
    """Store a filled-in labelling task.

    Re-labelling by the SAME person overwrites; a DIFFERENT person adds a
    second independent judgement, which is what makes kappa computable."""
    labelled_by = (labelled_by or "").strip()
    if not labelled_by:
        return {"ok": False,
                "error": "--by is required: an anonymous label cannot be "
                         "checked against a second reviewer, which is the point"}
    items = task.get("items") or []
    if not items:
        return {"ok": False, "error": "no items in task"}

    fields = ("topic_correct", "actor_correct", "cluster_coherent", "useful")
    skipped = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for it in items:
                ans = it.get("answers") or {}
                vals = [ans.get(f) for f in fields]
                if all(v is None for v in vals):
                    skipped += 1          # untouched item, not a judgement
                    continue
                cur.execute(
                    """INSERT INTO memory_eval_labels
                         (memory_id, labelled_by, topic_correct, actor_correct,
                          cluster_coherent, useful, note, statement_shown,
                          instrument_version)
                       VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (memory_id, labelled_by) DO UPDATE SET
                         instrument_version=EXCLUDED.instrument_version,
                         topic_correct=EXCLUDED.topic_correct,
                         actor_correct=EXCLUDED.actor_correct,
                         cluster_coherent=EXCLUDED.cluster_coherent,
                         useful=EXCLUDED.useful,
                         note=EXCLUDED.note,
                         statement_shown=EXCLUDED.statement_shown,
                         created_at=now()""",
                    (it["memory_id"], labelled_by, vals[0], vals[1], vals[2],
                     vals[3], (it.get("note") or "")[:500] or None,
                     (it.get("statement") or "")[:1000], INSTRUMENT_VERSION))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        conn.close()
    return {"ok": True, "labelled_by": labelled_by,
            "stored": len(items) - skipped, "skipped_unanswered": skipped}


def label_status() -> Dict[str, Any]:
    """How far off is a computable extrinsic result?"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.memory_eval_labels')")
            if not cur.fetchone()[0]:
                return {"labels": 0, "required": MIN_LABELS, "reviewers": 0,
                        "double_labelled": 0,
                        "next_step": "python -m scripts.migrate"}
            cur.execute(f"""SELECT count(*), count(DISTINCT labelled_by),
                                   count(DISTINCT memory_id)
                              FROM memory_eval_labels WHERE {_CURRENT}""")
            n, reviewers, distinct = cur.fetchone()
            cur.execute(f"""SELECT count(*) FROM (
                              SELECT memory_id FROM memory_eval_labels
                               WHERE {_CURRENT}
                               GROUP BY 1 HAVING count(DISTINCT labelled_by) > 1) x""")
            both = cur.fetchone()[0]
    finally:
        conn.close()
    return {"labels": n, "distinct_memories": distinct, "reviewers": reviewers,
            "double_labelled": both, "required": MIN_LABELS,
            "kappa_computable": both > 0,
            "note": None if both else
                    "no item has two reviewers, so inter-reviewer agreement "
                    "cannot be computed and every precision figure below would "
                    "rest on one person's opinion"}


# ── Interactive labelling ───────────────────────────────────────────────────

_QUESTIONS = (
    ("topic_correct",    "Is the assigned TOPIC right for this evidence?"),
    ("actor_correct",    "Is the ACTOR right (who did it)?"),
    ("cluster_coherent", "Do the cited records describe ONE theme?"),
    ("useful",           "Would this change what a rep does?"),
)


def format_item(item: Dict[str, Any], index: int, total: int) -> str:
    """Render one item for judgement.

    Shows the evidence DATES and sources, because the claim being judged is a
    dated, counted assertion and the dates are most of what makes it right or
    wrong. Deliberately does NOT show the assigned topic or actor: a reviewer
    shown the answer confirms it rather than judging it."""
    ev = item.get("evidence") or []
    kinds = sorted({e.get("source_type", "?") for e in ev})
    lines = [f"\n[{index}/{total}]  {item['entity']}",
             f"  STATEMENT: {item['statement']}",
             f"  claims {item['occurrences']} occurrence(s); cites "
             f"{len(ev)} record(s) from {', '.join(kinds) or '(none)'}",
             "  EVIDENCE:"]
    # The records themselves, not just their ids. Showing at most six keeps the
    # item readable; the count above says how many were actually cited.
    for e in ev[:6]:
        when = e.get("on_date") or "(no date)"
        body = (e.get("text") or "(text unavailable)").replace("\n", " ")
        lines.append(f"    {when}  {body[:110]}")
    if len(ev) > 6:
        lines.append(f"    ... and {len(ev) - 6} more")
    return "\n".join(lines)


def _pending(items: List[Dict[str, Any]], labelled_by: str) -> List[int]:
    """Indices not yet labelled BY THIS REVIEWER, so a session can resume."""
    done: set = set()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT memory_id::text FROM memory_eval_labels "
                            " WHERE labelled_by=%s", (labelled_by,))
                done = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception:                                   # noqa: BLE001
        pass
    return [i for i, it in enumerate(items) if it["memory_id"] not in done]


def _save_one(item: Dict[str, Any], labelled_by: str) -> Dict[str, Any]:
    return record_labels({"items": [item]}, labelled_by)


def review_cli(path: str, labelled_by: str) -> int:
    """Walk the task one item at a time, saving after each answer.

    Saving per item rather than at the end is the point: 120 items is a long
    sitting, and work already done must survive closing the window."""
    task = _read_json(path)
    items = task.get("items") or []
    if not items:
        print("no items in task")
        return 1
    pending = _pending(items, labelled_by)
    if not pending:
        print(f"{labelled_by} has already labelled all {len(items)} items.")
        return 0

    print(f"\n{len(pending)} of {len(items)} items left for '{labelled_by}'.")
    print("y = yes   n = no   u = unsure (left null)   s = skip item   q = quit\n"
          "Judge the statement against the evidence shown. The topic and actor\n"
          "the system assigned are hidden on purpose.")

    saved = 0
    for n, idx in enumerate(pending, 1):
        item = items[idx]
        print(format_item(item, n, len(pending)))
        answers: Dict[str, Any] = {}
        quit_now = skip = False
        for field, question in _QUESTIONS:
            while True:
                try:
                    reply = input(f"    {question} [y/n/u/s/q] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    reply = "q"
                if reply in ("y", "n", "u", "s", "q"):
                    break
                print("    please answer y, n, u, s or q")
            if reply == "q":
                quit_now = True
                break
            if reply == "s":
                skip = True
                break
            answers[field] = {"y": True, "n": False, "u": None}[reply]
        if quit_now:
            break
        if skip or all(v is None for v in answers.values()):
            continue
        item["answers"] = answers
        out = _save_one(item, labelled_by)
        if out.get("ok"):
            saved += 1
        else:
            print(f"    NOT SAVED: {out.get('error')}")

    print(f"\nsaved {saved} item(s) as '{labelled_by}'.")
    st = label_status()
    print(f"labels: {st['labels']} / {st['required']} required; "
          f"reviewers: {st['reviewers']}; double-labelled: {st['double_labelled']}")
    if not st["kappa_computable"]:
        print("A SECOND reviewer must label the same file, or agreement cannot "
              "be measured and every precision figure is one opinion.")
    return 0


# ── Is the GROUND TRUTH trustworthy? ────────────────────────────────────────
#
# The system already flags a human verifier who approves >95% of what they see
# as a rubber-stamping signal (`memory_assurance.safety_metrics.bias_flag`).
# The labels that GRADE the system had no such check, so the first real
# labelling session produced:
#
#     topic_correct     98.2% yes     topic_precision  0.982  PASS
#     actor_correct     97.2% yes     cluster_precision 0.973  PASS
#
# and the suite would have banked a PASS from 110 labels by one reviewer with
# nothing double-labelled. Among them, "General came up 2 times." — the
# catch-all topic, NO dates, two occurrences — was marked "would change what a
# rep does" six times, and 21 `general` memories were marked topic-correct
# where `general` is precisely what the classifier assigns when NO keyword
# matched.
#
# A measuring instrument with the same failure mode as the thing it measures
# reports the thing's quality as its own. So the ground truth is now graded
# before it is used, and an unreliable label set BLOCKS the result rather than
# producing a flattering one.

# Above this, near-total agreement stops being evidence about the system and
# starts being evidence about the reviewer.
RUBBER_STAMP_RATE = 0.95
RUBBER_STAMP_MIN = 20


def label_quality() -> Dict[str, Any]:
    """Grade the labels themselves. Returns blockers, not a score."""
    fields = ("topic_correct", "actor_correct", "cluster_coherent", "useful")
    out: Dict[str, Any] = {"per_field": {}, "blockers": [], "warnings": []}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for f in fields:
                cur.execute(f"""SELECT count(*) FILTER (WHERE {f} IS TRUE),
                                       count(*) FILTER (WHERE {f} IS FALSE)
                                  FROM memory_eval_labels WHERE {_CURRENT}""")
                yes, no = cur.fetchone()
                decided = (yes or 0) + (no or 0)
                rate = (yes / decided) if decided else None
                out["per_field"][f] = {"yes": yes, "no": no,
                                       "yes_rate": round(rate, 4) if rate else None}
                if decided >= RUBBER_STAMP_MIN and rate is not None \
                        and rate > RUBBER_STAMP_RATE:
                    out["blockers"].append(
                        f"{f}: {rate:.1%} 'yes' over {decided} decisions — at "
                        f"this rate the labels cannot distinguish a good system "
                        f"from default agreement")

            cur.execute(f"SELECT count(DISTINCT labelled_by) FROM memory_eval_labels "
                        f"WHERE {_CURRENT}")
            reviewers = cur.fetchone()[0]
            cur.execute("""SELECT count(*) FROM (
                             SELECT memory_id FROM memory_eval_labels
                              GROUP BY 1 HAVING count(DISTINCT labelled_by) > 1) x""")
            double = cur.fetchone()[0]
            out["reviewers"], out["double_labelled"] = reviewers, double
            if double == 0:
                out["blockers"].append(
                    "no item has two reviewers — agreement is unmeasurable, so "
                    "every figure is one person's opinion and cannot be "
                    "distinguished from a consistent mistake")

            # Two checks that need no second opinion: a label that contradicts
            # what the record plainly says.
            cur.execute(f"""SELECT count(*) FROM memory_eval_labels l
                             JOIN customer_memories m ON m.memory_id=l.memory_id
                            WHERE m.first_observed_at IS NULL AND l.useful IS TRUE
                              AND l.{_CURRENT}""")
            dateless_useful = cur.fetchone()[0]
            if dateless_useful:
                out["warnings"].append(
                    f"{dateless_useful} memory/memories with NO dates were "
                    f"marked as changing what a rep does")

            cur.execute(f"""SELECT count(*) FROM memory_eval_labels l
                             JOIN customer_memories m ON m.memory_id=l.memory_id
                            WHERE m.topic='general' AND l.topic_correct IS TRUE
                              AND l.{_CURRENT}""")
            general_ok = cur.fetchone()[0]
            if general_ok:
                out["warnings"].append(
                    f"{general_ok} memory/memories on the catch-all topic "
                    f"'general' were marked topic-correct; 'general' is what "
                    f"the classifier assigns when no keyword matched")
    finally:
        conn.close()
    out["reliable"] = not out["blockers"]
    return out


# ── Is this corpus worth labelling at all? ──────────────────────────────────
#
# Extrinsic validation was attempted on 2026-08-01 and abandoned on evidence.
# Two reviewers labelled 120 items each; the corpus turned out to be seed data:
#
#     36 contacts on example.com          RFC 2606 reserved — unreachable by
#                                         specification, not by accident
#     1,129 activities are ONE template   "Order shipped - follow up with
#                                         customer" (642 + 487, differing only
#                                         by en-dash vs hyphen)
#
# Every `delivery` theme under review was that template repeated. Labelling it
# measures the SEED GENERATOR, not the memory system, and no amount of reviewer
# discipline fixes that — the ground truth would be about fixtures.
#
# So the suite says "deferred" rather than "insufficient_labels", which reads
# like an outstanding task someone should get round to. It is not: it is
# blocked on data that does not exist yet.

SYNTHETIC_DOMAINS = ("example.com", "example.org", "example.net", "examples.com",
                     "test.com", "invalid", "localhost")

# Below this share of distinct subjects, the corpus is template-generated
# rather than observed.
MIN_SUBJECT_DIVERSITY = 0.75


def corpus_realism() -> Dict[str, Any]:
    """Decide whether the corpus can support a judgement about the system.

    Detected rather than configured: a flag someone must remember to set is a
    flag that will be wrong."""
    out: Dict[str, Any] = {"signals": [], "synthetic": False}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE split_part(email,'@',2)
                                                        = ANY(%s)), count(*)
                             FROM contacts WHERE email IS NOT NULL""",
                        (list(SYNTHETIC_DOMAINS),))
            reserved, with_email = cur.fetchone()
            out["contacts_with_email"] = with_email
            out["reserved_domain_contacts"] = reserved
            if with_email and reserved:
                out["signals"].append(
                    f"{reserved} of {with_email} contact emails use a reserved "
                    f"documentation domain — nothing can be sent to them")

            cur.execute("""SELECT count(DISTINCT left(subject, 46)), count(*)
                             FROM activities WHERE subject IS NOT NULL""")
            distinct, total = cur.fetchone()
            diversity = (distinct / total) if total else 1.0
            out["subject_diversity"] = round(diversity, 4)
            cur.execute("""SELECT left(subject,46), count(*) FROM activities
                            WHERE subject IS NOT NULL
                            GROUP BY 1 ORDER BY 2 DESC LIMIT 1""")
            top = cur.fetchone()
            if top:
                out["most_repeated"] = {"subject": top[0], "count": top[1]}
                if total and top[1] / total > 0.05:
                    out["signals"].append(
                        f"one subject template accounts for {top[1]} of {total} "
                        f"activities ({top[1]/total:.0%})")
            if diversity < MIN_SUBJECT_DIVERSITY:
                out["signals"].append(
                    f"only {diversity:.0%} of activity subjects are distinct; "
                    f"the corpus is template-generated")
    finally:
        conn.close()
    out["synthetic"] = bool(out["signals"])
    return out


def extrinsic_metrics() -> Dict[str, Any]:
    """Topic precision, cluster precision, usefulness and reviewer agreement.

    Returns `insufficient_labels` until MIN_LABELS exist. It does NOT
    substitute a proxy: there is no way to compute topic precision without
    someone judging topics, and reporting a related-but-different number as
    though it answered the question is the single most common failure this
    project has made."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.memory_eval_labels')")
            if not cur.fetchone()[0]:
                return {"status": "insufficient_labels", "labels": 0,
                        "required": MIN_LABELS,
                        "next_step": "python -m app.core.memory_eval --labels"}
            cur.execute(f"SELECT count(*) FROM memory_eval_labels WHERE {_CURRENT}")
            n = cur.fetchone()[0]
    finally:
        conn.close()
    # Before asking for more labels, check that labelling this corpus could
    # answer the question. It cannot answer it about seed data.
    realism = corpus_realism()
    if realism["synthetic"] and n < MIN_LABELS:
        return {"status": "deferred_synthetic_corpus",
                "labels": n, "required": MIN_LABELS,
                "corpus": realism,
                "why": "labelling this corpus would measure the seed-data "
                       "generator, not the memory system",
                "next_step": "re-run once the database holds real customer "
                             "records; the harness and thresholds are ready"}
    if n < MIN_LABELS:
        return {"status": "insufficient_labels", "labels": n,
                "required": MIN_LABELS,
                "next_step": "python -m app.core.memory_eval --labels --out t.json"}

    # GRADE THE GROUND TRUTH FIRST. Computing precision from labels that cannot
    # discriminate produces a number that measures the reviewer, and reports it
    # as a property of the system.
    quality = label_quality()
    if not quality["reliable"]:
        return {"status": "labels_unreliable", "labels": n,
                "label_quality": quality,
                "why": "precision computed from these labels would describe "
                       "the labelling, not the system",
                "next_step": "a SECOND reviewer must label the same sample "
                             "independently: --review t.json --by other_name"}
    out = _extrinsic_from_labels()
    out["label_quality"] = quality
    return out


def _extrinsic_from_labels() -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            out: Dict[str, Any] = {"status": "measured"}
            for field, metric in (("topic_correct", "topic_precision"),
                                  ("cluster_coherent", "cluster_precision"),
                                  ("useful", "usefulness")):
                cur.execute(f"""SELECT count(*) FILTER (WHERE {field}), count(*)
                                  FROM memory_eval_labels
                                 WHERE {field} IS NOT NULL AND {_CURRENT}""")
                hits, n = cur.fetchone()
                out[metric] = _score(metric, hits or 0, n or 0)
            # Agreement on the topic judgement, across reviewer pairs.
            cur.execute("""SELECT a.topic_correct::text, b.topic_correct::text
                             FROM memory_eval_labels a
                             JOIN memory_eval_labels b
                               ON a.memory_id = b.memory_id
                              AND a.labelled_by < b.labelled_by
                            WHERE a.topic_correct IS NOT NULL
                              AND b.topic_correct IS NOT NULL
                              AND a.instrument_version = {v}
                              AND b.instrument_version = {v}""".format(
                            v=INSTRUMENT_VERSION))
            pairs = cur.fetchall()
            if pairs:
                k = cohens_kappa([p[0] for p in pairs], [p[1] for p in pairs])
                out["reviewer_agreement_kappa"] = {
                    "value": round(k, 3) if k is not None else None,
                    "n": len(pairs),
                    "threshold": THRESHOLDS["reviewer_agreement_kappa"]}
            else:
                out["reviewer_agreement_kappa"] = {
                    "value": None, "n": 0,
                    "note": "no memory was labelled by two reviewers, so "
                            "agreement is unmeasured and every precision "
                            "figure above rests on one person's judgement"}
    finally:
        conn.close()
    return out


def run_all() -> Dict[str, Any]:
    intrinsic = [measure_determinism(), measure_evidence_resolvable(),
                 measure_count_consistency(), measure_actor_accuracy(),
                 measure_production_attribution()]
    ext = extrinsic_metrics()
    # A nested coverage score must be able to fail the run; otherwise the
    # gameable half of the metric is reported but not enforced.
    flat = []
    for m in intrinsic:
        flat.append(m)
        if isinstance(m.get("coverage"), dict):
            flat.append(m["coverage"])
    failed = [m["metric"] for m in flat if m.get("status") == "fail"]
    return {"intrinsic": intrinsic, "extrinsic": ext,
            "failed": failed,
            # `labels_unreliable` is INCOMPLETE, never PASS: the system may well
            # be fine, but this evidence cannot show it.
            "verdict": "FAIL" if failed else
                       ("INCOMPLETE" if ext.get("status") != "measured" else "PASS")}


if __name__ == "__main__":                                   # pragma: no cover
    import sys
    def _arg(flag, default=None):
        return (sys.argv[sys.argv.index(flag) + 1]
                if flag in sys.argv and len(sys.argv) > sys.argv.index(flag) + 1
                else default)

    if "--review" in sys.argv:
        raise SystemExit(review_cli(_arg("--review"), _arg("--by", "")))
    if "--ingest" in sys.argv:
        print(json.dumps(record_labels(_read_json(_arg("--ingest")),
                                       _arg("--by", "")), indent=2))
    elif "--corpus" in sys.argv:
        print(json.dumps(corpus_realism(), indent=2, default=str))
    elif "--label-quality" in sys.argv:
        print(json.dumps(label_quality(), indent=2, default=str))
    elif "--status" in sys.argv:
        print(json.dumps(label_status(), indent=2, default=str))
    elif "--labels" in sys.argv:
        task = labelling_task(n=int(_arg("--n", "120")),
                              seed=int(_arg("--seed", "20260801")))
        out = _arg("--out")
        if out:
            # Preferred: WE write the file, in UTF-8. Shell redirection is what
            # produced the UTF-16 task file that could not be read back.
            _write_json(out, task)
            print(f"wrote {out} ({len(task.get('items') or [])} items, UTF-8)\n"
                  f"Fill each `answers` field with true / false / null, then:\n"
                  f"    python -m app.core.memory_eval --ingest {out} --by YOUR_NAME")
        else:
            print(json.dumps(task, indent=2, default=str))
    else:
        print(json.dumps(run_all(), indent=2, default=str))
