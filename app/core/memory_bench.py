"""Phase 4 — continuous memory-quality benchmark.

WHAT THIS IS. The derivation pipeline run end to end against a FROZEN corpus,
producing numbers that move only when CODE moves. It is the regression gate:
a build whose clustering, statements, attribution or trust scores differ from
the recorded baseline is reported as a regression before it ships.

WHY FROZEN. Measured over one afternoon with no benchmark activity at all, the
live index moved 7278 -> 8049 -> 7278 -> 7394 records and themes 757 -> 853 ->
863 -> 848; `production_attribution` drifted 0.9312 -> 0.9243 -> 0.9340 purely
from data changes. A gate reading live data fires on every reindex and stays
silent on real code regressions — worse than no gate, because it teaches people
to ignore it.

WHY NO DATABASE. The fixture carries its own embeddings, so this runs anywhere.
CI currently executes 185 database-free tests out of 1088 and cannot touch the
derivation pipeline at all; this gives it the whole thing.

WHAT IT DELIBERATELY DOES NOT COVER. Retrieval accuracy, commitment extraction,
temporal reasoning and reviewer agreement all need human labels, and labels are
blocked on real data (see memory_eval.corpus_realism). Those stay with Phase 3.
A gate that implied it covered them would be worse than one that says it does
not.

    python -m app.core.memory_bench                 # run and compare
    python -m app.core.memory_bench --record        # set the baseline
"""

from __future__ import annotations

import base64
import json
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# .json.gz: the corpus is base64 embeddings, unreadable and undiffable either
# way, and gzip halves what every clone carries forever (3.09 -> 1.38 MiB).
BENCH_DIR = Path(__file__).resolve().parents[2] / "benchmarks"
CORPUS = BENCH_DIR / "corpus.json.gz"

# EVERY corpus present is measured, not just the committed one.
#
# The instinct on real data is to RE-RECORD the baseline. That launders
# regressions: any defect introduced between now and then becomes the new
# reference, silently and permanently. A baseline is a claim that an output is
# correct, and re-recording overwrites the only evidence that it changed.
#
# So corpora ACCUMULATE. `corpus.json.gz` is synthetic and committed — it tests
# CODE, and code does not care that its input is seed data. `corpus-*.json.gz`
# may hold real records, is gitignored, and additionally catches "this breaks on
# real-world text shapes". A change must clear every corpus present.
CORPUS_GLOB = "corpus*.json.gz"
BASELINE = Path(__file__).resolve().parents[2] / "benchmarks" / "baseline.json"

# How far a metric may move before it is a regression.
#
# EXACT-ZERO for the derivation outputs: clustering, statements and evidence
# hashes are deterministic functions of frozen input, so ANY movement is a code
# change and the gate should say so rather than absorb it. Latency gets a real
# tolerance because it measures a machine, not the code.
TOLERANCE: Dict[str, float] = {
    "clusters": 0.0,
    "themes": 0.0,
    "occasions_total": 0.0,
    "statement_digest": 0.0,        # categorical: equal or not
    "evidence_digest": 0.0,
    "actor_distribution": 0.0,
    "single_wording_themes": 0.0,
    "mean_certainty": 0.0005,       # float rounding only
    "mean_distinct_templates": 0.0005,
    "attribution_distribution": 0.0,
    "actor_matrix_digest": 0.0,
    "gate_digest": 0.0,
    "trust_matrix_digest": 0.0,
    "gate_cases_passing": 0.0,
    "gate_blockers_total": 0.0,
}

# REPORTED, NEVER GATING. Latency measures the machine, not the code. The very
# first clean run after recording a baseline exited 1 on nothing but a cold
# start — imports uncached, 110ms baseline, >176ms needed to trip a 60%
# tolerance. On a shared CI runner that fires at random, and a gate that fires
# at random is worse than no gate because it teaches people to ignore it.
#
# It stays in the output: a build that doubles the derivation cost is worth
# seeing. It just does not block, because this harness cannot tell a slow
# runner from slow code.
REPORTED_ONLY = ("latency_ms",)


def corpora() -> List[Path]:
    """Every frozen corpus on disk, committed or not."""
    return sorted(BENCH_DIR.glob(CORPUS_GLOB)) if BENCH_DIR.exists() else []


def load_corpus(path: Path = CORPUS) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"no benchmark corpus at {path}\n"
            "build one:  python -m scripts.make_benchmark --out benchmarks/corpus.json")
    if path.suffix == ".gz":
        import gzip
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def _records(entity: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rehydrate to the shape `_load_records` returns, so the pipeline runs
    unmodified. Decoding here rather than storing floats keeps the fixture
    exact — a re-encoded vector is not the vector that produced the baseline."""
    import datetime as dt
    from app.core import embeddings as E
    from app.core.content_index import actor_for, speech_act
    out = []
    for r in entity["records"]:
        vec = E.decode(base64.b64decode(r["embedding"]), r["dims"])
        if vec is None:
            continue
        occ = r.get("occurred_at")
        out.append({
            "source_type": r["source_type"], "source_id": r["source_id"],
            "vec": vec, "snippet": r.get("snippet") or "",
            "visibility": r.get("visibility") or "internal",
            "occurred_at": dt.datetime.fromisoformat(occ) if occ else None,
            "direction": r.get("direction"),
            # RE-DERIVED, not read back. The fixture stores the actor and
            # speech act that were computed WHEN IT WAS FROZEN; trusting them
            # meant the benchmark never called `actor_for` at all. Breaking
            # `_KNOWN_SPEAKER_SOURCES` — the fix that corrected all 14 wrong
            # third-party attributions — left the gate green.
            #
            # Stratifying the corpus by source was necessary and not
            # sufficient: the branches were present in the DATA and still
            # unreachable in the CODE.
            "actor": actor_for(r.get("direction"), r["source_type"],
                               r.get("snippet") or "", r.get("activity_type")),
            "speech_act": speech_act(r.get("snippet") or ""),
            "parent_key": r.get("parent_key"),
            "activity_type": r.get("activity_type"),
            # Kept for reference: what the live index held at freeze time.
            "actor_at_freeze": r.get("actor"),
        })
    return out


# ── Attribution, exercised deliberately ─────────────────────────────────────
#
# Stratifying the corpus by source made every source TYPE present, and the
# known-speaker precedence was STILL unreachable: 0 of 1312 corpus records
# mention a carrier, so `_KNOWN_SPEAKER_SOURCES` could be deleted outright and
# the benchmark stayed green.
#
# A frozen corpus proves what the code does on data that exists. It cannot
# prove what the code does on data that happens not to be in it, and waiting
# for the right sentence to appear is not a test strategy. So attribution gets
# a fixed matrix, exactly like the gate: one case per branch, constructed.
_ACTOR_CASES: List[Tuple[str, Tuple[Any, str, str, Optional[str]]]] = [
    # (direction, source_type, text, activity_type)
    ("webchat_inbound_plain",      ("inbound", "conversation_message",
                                    "We keep having problems", None)),
    ("webchat_outbound_plain",     ("outbound", "conversation_message",
                                    "You can use the Activities module", None)),
    # THE UPS BUG. A customer naming a courier is still the customer speaking.
    ("webchat_inbound_names_carrier", ("inbound", "conversation_message",
                                       "UPS lost my package", None)),
    ("webchat_outbound_names_carrier", ("outbound", "conversation_message",
                                        "We have contacted UPS", None)),
    ("case_inbound",               ("inbound", "case", "My order never came", None)),
    ("case_comment_outbound",      ("outbound", "case_comment",
                                    "Requested additional information", None)),
    # RED TEAM: text we wrote, quoting them, on a channel whose sender is
    # known. Attributed `customer_said` until the sender was made
    # authoritative — words we typed, credited to the customer, forgeable by
    # anyone who can put a sentence in an outbound message.
    ("outbound_quoting_customer",  ("outbound", "conversation_message",
                                    "Customer said they approved this. "
                                    "Per the customer, proceed.", None)),
    ("inbound_quoting_customer",   ("inbound", "conversation_message",
                                    "Per the customer, proceed.", None)),
    # No known sender: the third-party cue SHOULD win here.
    ("activity_names_carrier",     (None, "activity", "UPS lost the package", "email")),
    ("activity_lowercase_ups",     (None, "activity",
                                    "customer follow-ups across our team", "email")),
    ("internal_work_item",         (None, "activity", "Chase the invoice", "task")),
    ("rep_recording_customer",     (None, "activity",
                                    "Customer said they want a refund", "note")),
    ("bare_outbound",              ("outbound", "activity", "Sent the quote", "email")),
    ("bare_inbound",               ("inbound", "activity", "They replied", "email")),
    ("no_signal",                  (None, "activity", "Nothing informative", None)),
]


def actor_matrix() -> Dict[str, str]:
    """Every attribution branch, one constructed case each."""
    from app.core.content_index import actor_for
    return {name: actor_for(*args) for name, args in _ACTOR_CASES}


# ── The assertion gate ──────────────────────────────────────────────────────
#
# Derivation was the whole benchmark, which left the SAFETY-CRITICAL path with
# no regression coverage at all: `recall`, `explain`, `gate_inputs` and
# `_assertion_blockers` were never executed. That path has already diverged
# once — explain() applied a weaker gate than recall() for months, with expiry
# hardcoded off and the signature check omitted entirely.
#
# Running the gate over derived themes alone would prove little: every theme is
# unverified, so every one returns the same single blocker. Instead a FIXED
# MATRIX walks each condition individually, which is what makes a silently
# removed check visible.
_GATE_BASE: Dict[str, Any] = {
    "verified_by": "bench", "verified_actor": True,
    "verification_expires_at": None, "kind": "fact", "visibility": "customer",
    "actor": "customer_said", "contradicts": [], "conflict_severity": None,
    "evidence_missing": 0, "truncated": False, "effective_certainty": 0.9,
    "verified_claim_hash": "h", "current_claim_hash": "h", "signature_ok": True,
}

# One perturbation per condition the gate is supposed to enforce.
_GATE_CASES: List[Tuple[str, Dict[str, Any]]] = [
    ("clean",              {}),
    ("unverified",         {"verified_by": None}),
    ("actor_unconfirmed",  {"verified_actor": False}),
    ("statement_drifted",  {"current_claim_hash": "other"}),
    ("no_claim_hash",      {"verified_claim_hash": None}),
    ("signature_missing",  {"signature_ok": None}),
    ("signature_invalid",  {"signature_ok": False}),
    ("not_a_fact",         {"kind": "theme"}),
    ("internal_evidence",  {"visibility": "internal"}),
    ("actor_unknown",      {"actor": "unknown"}),
    ("actor_mixed",        {"actor": "mixed"}),
    ("contradicted",       {"contradicts": ["x"]}),
    ("high_conflict",      {"conflict_severity": "high"}),
    ("evidence_gone",      {"evidence_missing": 2}),
    ("truncated_count",    {"truncated": True}),
    ("below_floor",        {"effective_certainty": 0.1}),
]


def gate_matrix() -> Dict[str, Any]:
    """Every gate condition, exercised one at a time.

    Returns the blocker set per case, so a condition that stops firing changes
    the digest even when the pass/fail shape looks unchanged."""
    from app.core import memory_consolidation as MC
    out: Dict[str, List[str]] = {}
    for name, override in _GATE_CASES:
        kw = dict(_GATE_BASE, **override)
        out[name] = sorted(MC._assertion_blockers(**kw))
    return out


def trust_matrix() -> Dict[str, float]:
    """The trust arithmetic probed at its BOUNDARIES, not through the corpus.

    Corpus data covers whatever it happens to contain. Measured: raising the
    certainty CAP from 0.95 to 0.99 moved `mean_certainty` by exactly zero,
    because no cluster in the frozen corpus is broad enough to reach the cap —
    so the rule "a derivation may never assert the confidence only a human
    confers" was unguarded, while the gate reported `ok`.

    Constructed inputs cover the branches data does not: the floor with no
    corroboration, the cap with total corroboration, and each weighting term in
    isolation so a change to one cannot hide behind another.
    """
    from app.core import memory_consolidation as MC
    return {
        # Floor: one day, one source, one wording — nothing corroborates it.
        "floor":        MC.certainty_from_breadth(MC.breadth_of(1, 1, 1)),
        # Cap: saturated on every axis. The value that data never reaches.
        "cap":          MC.certainty_from_breadth(MC.breadth_of(99, 99, 99)),
        # THE CAP WHERE IT ACTUALLY BINDS.
        #
        # FLOOR + SPAN == 0.35 + 0.60 == 0.95 == CAP exactly, so `min()` never
        # selects the cap and changing it alters no output — raising it to 0.99
        # left every metric identical, including the boundary probes above. A
        # backstop that is currently non-binding cannot be observed through any
        # reachable input, which is precisely why it needs an input chosen to
        # make it bind. If someone widens SPAN, this is the number that moves.
        "cap_binds":    MC.certainty_from_breadth(2.0),
        "breadth_zero": MC.breadth_of(0, 0, 0),
        "breadth_full": MC.breadth_of(99, 99, 99),
        # Each term alone, so reweighting cannot be silently compensated.
        "days_only":    MC.breadth_of(99, 0, 0),
        "sources_only": MC.breadth_of(0, 99, 0),
        "wordings_only": MC.breadth_of(0, 0, 99),
        # One wording contributes exactly zero, however often it repeats.
        "one_wording":  MC.breadth_of(0, 0, 1),
        "two_wordings": MC.breadth_of(0, 0, 2),
    }


def run(corpus: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Derive themes for every frozen entity and summarise the result."""
    import hashlib
    from app.core import memory_consolidation as MC

    corpus = corpus or load_corpus()

    # THE EMBEDDING MODEL MUST MATCH.
    #
    # The fixture stores raw float32 vectors. Change EMBED_MODEL and they still
    # DECODE — same dims, same bytes — so every metric keeps passing while
    # production clusters on vectors from a different model entirely. The
    # benchmark would be green and meaningless, which is the worst state a gate
    # can be in.
    from app.core import embeddings as E
    if (corpus.get("model"), corpus.get("dims")) != (E.MODEL, E.DIMS):
        raise SystemExit(
            f"corpus was frozen under {corpus.get('model')}@{corpus.get('dims')} "
            f"but the code now uses {E.MODEL}@{E.DIMS}.\n"
            "The vectors would still decode and every metric would still pass — "
            "against a model you no longer run. Rebuild the corpus deliberately:"
            "\n    python -m scripts.make_benchmark")

    started = time.perf_counter()

    clusters = themes = occasions = single_wording = 0
    certainties: List[float] = []
    templates: List[int] = []
    actors: Dict[str, int] = {}
    statements: List[str] = []
    ev_hashes: List[str] = []

    for entity in corpus["entities"]:
        recs = _records(entity)
        if len(recs) < MC.MIN_CLUSTER:
            continue
        candidates = []
        for idx in MC._cluster(recs):
            if len(idx) < MC.MIN_CLUSTER:
                continue
            clusters += 1
            members = MC._distinct_occasions([recs[i] for i in idx])
            if len(members) < MC.MIN_CLUSTER:
                continue
            candidates.append((MC._distinct_templates(members), len(members),
                               members))
        candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)

        live: List[tuple] = []
        for n_tpl, _n, members in candidates:
            topic = MC._topic_for([m["snippet"] for m in members])
            actor = MC._cluster_actor(members)
            if (topic, actor) in live:
                continue
            live.append((topic, actor))

            dates = [m["occurred_at"] for m in members if m["occurred_at"]]
            first, last = (min(dates), max(dates)) if dates else (None, None)
            stmt = MC._statement(topic, len(members), first, last, actor,
                                 False, single_wording=(n_tpl == 1))
            themes += 1
            occasions += len(members)
            templates.append(n_tpl)
            if n_tpl == 1:
                single_wording += 1
            actors[actor] = actors.get(actor, 0) + 1
            statements.append(stmt)
            ev_hashes.append(MC._evidence_hash(
                [f"{m['source_type']}:{m['source_id']}" for m in members]))

            # Trust without a database: reliability's evidence lookup needs one,
            # so the benchmark measures the BREADTH half — the part the code
            # actually decides.
            days = {m["occurred_at"].date() for m in members if m["occurred_at"]}
            srcs = {m["source_type"] for m in members}
            # CALL the product's arithmetic; never restate it.
            #
            # These two lines were a verbatim copy of the formula in
            # memory_consolidation, literals and all. A gate that recomputes
            # what it is guarding cannot detect a change to it: mutating the
            # certainty floor from 0.35 to 0.40 in the product moved
            # mean_certainty by 0.0000 and the gate reported `ok`.
            breadth = MC.breadth_of(len(days), len(srcs), n_tpl)
            certainties.append(MC.certainty_from_breadth(breadth))

    # THE GATE. Its inputs are pure, so it needs no database and no fixture —
    # only the same code the assertion path runs.
    # Attribution derived from raw fields, digested so any change is visible.
    attribution: Dict[str, int] = {}
    for entity in corpus["entities"]:
        for r in _records(entity):
            attribution[r["actor"]] = attribution.get(r["actor"], 0) + 1

    actors_matrix = actor_matrix()
    gate = gate_matrix()
    trust = trust_matrix()
    gate_pass = [name for name, blockers in gate.items() if not blockers]
    gate_blockers_total = sum(len(b) for b in gate.values())

    elapsed = (time.perf_counter() - started) * 1000

    def digest(items: List[str]) -> str:
        return hashlib.sha256("␟".join(sorted(items)).encode()).hexdigest()[:16]

    return {
        "corpus_id": corpus["corpus_id"],
        "generator": __import__("app.core.memory_consolidation",
                                fromlist=["GENERATOR"]).GENERATOR,
        "metrics": {
            "clusters": clusters,
            "themes": themes,
            "occasions_total": occasions,
            "single_wording_themes": single_wording,
            "mean_certainty": round(statistics.fmean(certainties), 4) if certainties else 0.0,
            "mean_distinct_templates": round(statistics.fmean(templates), 4) if templates else 0.0,
            "actor_distribution": json.dumps(dict(sorted(actors.items()))),
            # The sentences themselves, as one value. A reworded statement is a
            # changed claim about a person even when every count is identical.
            "statement_digest": digest(statements),
            # Evidence selection. Moves if clustering or dedup changes, and it
            # is what invalidates a human verification.
            "evidence_digest": digest(ev_hashes),
            # A condition that silently stops blocking changes this digest.
            "attribution_distribution": json.dumps(dict(sorted(attribution.items()))),
            # One constructed case per attribution branch. Changes when any
            # branch changes, whatever the corpus happens to contain.
            "actor_matrix_digest": digest(
                [f"{k}={v}" for k, v in actors_matrix.items()]),
            "gate_digest": digest([f"{k}={','.join(v)}" for k, v in gate.items()]),
            # The trust arithmetic at its boundaries — covers the cap and each
            # weighting term, which corpus data does not reach.
            "trust_matrix_digest": digest(
                [f"{k}={v}" for k, v in sorted(trust.items())]),
            # Exactly one case ("clean") may pass. If a second appears, some
            # condition stopped firing.
            "gate_cases_passing": len(gate_pass),
            "gate_blockers_total": gate_blockers_total,
            "latency_ms": round(elapsed, 1),
        },
        "trust_detail": trust,
        "gate_detail": gate,
        "actor_detail": actors_matrix,
    }


def _baselines() -> Dict[str, Any]:
    if not BASELINE.exists():
        return {}
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    # Schema 2 keys by corpus_id so several corpora can be gated at once.
    if isinstance(data.get("corpora"), dict):
        return data["corpora"]
    # Schema 1 held ONE result at the top level. Detect it by shape and lift it
    # under its own id — the first version of this read `data` itself as the
    # map, which merged `corpus_id`, `generator` and `metrics` in as if they
    # were corpora. A silent corruption inside the machinery built to stop
    # silent corruption.
    if "metrics" in data and "corpus_id" in data:
        return {data["corpus_id"]: data}
    return {}


def compare(current: Dict[str, Any],
            baseline: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Diff against the recorded baseline. Regressions are named, not summed."""
    if baseline is None:
        stored = _baselines()
        baseline = stored.get(current["corpus_id"])
        if baseline is None:
            return {"status": "no_baseline", "corpus_id": current["corpus_id"],
                    "next_step": "python -m app.core.memory_bench --record "
                                 "--reason 'why this output is correct'"}

    if baseline.get("corpus_id") != current.get("corpus_id"):
        # Comparing results from different corpora compares nothing. This is
        # the failure a live-data gate makes silently, every run.
        return {"status": "corpus_changed",
                "baseline_corpus": baseline.get("corpus_id"),
                "current_corpus": current.get("corpus_id"),
                "why": "the frozen corpus was rebuilt; a baseline measured "
                       "against different input is not a baseline",
                "next_step": "re-record deliberately: --record"}

    changes, regressions, observations = [], [], []
    for name, now in current["metrics"].items():
        was = baseline["metrics"].get(name)
        if name in REPORTED_ONLY:
            if isinstance(now, (int, float)) and isinstance(was, (int, float)) and was:
                observations.append({"metric": name, "baseline": was,
                                     "current": now,
                                     "relative_change": round((now - was) / was, 4)})
            continue
        tol = TOLERANCE.get(name, 0.0)
        if isinstance(now, (int, float)) and isinstance(was, (int, float)):
            if was == 0:
                moved = now != was
                rel = None
            else:
                rel = abs(now - was) / abs(was)
                moved = rel > tol
            if moved:
                entry = {"metric": name, "baseline": was, "current": now,
                         "relative_change": round(rel, 4) if rel is not None else None}
                changes.append(entry)
                regressions.append(entry)
        elif now != was:
            entry = {"metric": name, "baseline": was, "current": now}
            changes.append(entry)
            regressions.append(entry)

    gen_moved = baseline.get("generator") != current.get("generator")
    return {
        "status": "regression" if regressions else "ok",
        "corpus_id": current["corpus_id"],
        "generator_changed": gen_moved,
        # A generator change EXPLAINS statement/evidence movement — it does not
        # excuse it. The build still has to be looked at; it just is not a
        # surprise.
        "note": ("the derivation identity changed, so statement and evidence "
                 "movement is expected — confirm it is the change you meant"
                 if gen_moved else None),
        "changes": changes,
        # Movement worth reading that does not block a build.
        "observations": observations,
    }


def record(current: Dict[str, Any], reason: str = "",
           by: str = "") -> Dict[str, Any]:
    """Pin an output as correct — for ONE corpus, without touching the others.

    A baseline is a claim, and claims in this system carry provenance. Without
    it, `--record` is a one-keystroke way to make a red gate green: the engineer
    who cannot explain a diff can simply adopt it, and the record shows nothing.
    So the reason is required and stored beside the numbers."""
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False,
                "error": "--reason is required. Re-recording adopts the current "
                         "output as correct; if that cannot be explained in a "
                         "sentence, the diff should be investigated instead."}
    import datetime as _dt
    stored = _baselines()
    previous = stored.get(current["corpus_id"])
    entry = dict(current)
    entry["recorded"] = {
        "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "by": by or "unspecified",
        "reason": reason,
        # What this baseline REPLACED, so an adopted regression stays visible.
        "supersedes": (previous or {}).get("metrics"),
    }
    stored[current["corpus_id"]] = entry
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps({"schema": 2, "corpora": stored}, indent=1),
                        encoding="utf-8")
    return {"ok": True, "corpus_id": current["corpus_id"],
            "replaced_a_previous_baseline": previous is not None}


if __name__ == "__main__":                                   # pragma: no cover
    import sys

    def _arg(flag, default=""):
        i = sys.argv.index(flag) if flag in sys.argv else -1
        return sys.argv[i + 1] if 0 <= i < len(sys.argv) - 1 else default

    paths = corpora()
    if not paths:
        raise SystemExit(
            "no frozen corpus in benchmarks/\n"
            "build one:  python -m scripts.make_benchmark")

    failed = False
    out = []
    for path in paths:
        result = run(load_corpus(path))
        if "--record" in sys.argv:
            res = record(result, _arg("--reason"), _arg("--by"))
            if not res.get("ok"):
                raise SystemExit(res["error"])
            out.append({"corpus": path.name, "recorded": res})
            continue
        verdict = compare(result)
        failed = failed or verdict["status"] in ("regression", "corpus_changed")
        out.append({"corpus": path.name, "result": result, "verdict": verdict})
    print(json.dumps(out, indent=2))
    raise SystemExit(1 if failed else 0)
