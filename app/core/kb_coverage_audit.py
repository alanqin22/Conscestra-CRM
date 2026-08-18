"""KB coverage audit — scores the golden set against live retrieval.

Run:  python -m app.core.kb_coverage_audit [--audience public|internal|all]
                                           [--limit N] [--json out.json]

FOUR VERDICTS, and why they are not one number
----------------------------------------------
    ANSWERED        retrieval returned an article that answers the question
    PARTIAL         related and useful, but does not fully answer it
    GAP             nothing relevant came back — an honest "we don't know"
    FALSE_COVERAGE  something came back CONFIDENTLY that does not answer it

Coverage% deliberately counts ANSWERED only, and FALSE_COVERAGE is reported
separately rather than being folded in as a near-miss. A GAP is a system
admitting ignorance; FALSE_COVERAGE is a system asserting something wrong with
the same confidence it asserts something right. Averaging them together would
let a KB improve its score by guessing — the precise failure this audit exists
to prevent.

WHY THE JUDGE IS NOT SIMILARITY
-------------------------------
The retriever already ranked these hits by similarity, so scoring them by
similarity again would only ask "did the ranker rank?" — it cannot see that
"How do I merge duplicate CONTACTS" was answered with the article on merging
duplicate ACCOUNTS, which scores highly on every lexical and embedding measure
and is still the wrong answer. Judging answerhood needs a reader.

`must_cover` is applied AFTER the judge and can only downgrade. A fluent answer
that omits the decisive fact (accepted vs sent, consent before emailing) is
exactly what a language judge is prone to wave through.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from app.core.kb_golden_set import GOLDEN, LAYERS

logger = logging.getLogger(__name__)

ANSWERED, PARTIAL, GAP, FALSE_COVERAGE, UNSCORED = (
    "ANSWERED", "PARTIAL", "GAP", "FALSE_COVERAGE", "UNSCORED")

# UNSCORED = the judge did not return a verdict. Reported, never guessed: it is
# the instrument's own uncertainty, and hiding it inside PARTIAL would make the
# audit do exactly what it criticises the KB for.
_VERDICTS = (ANSWERED, PARTIAL, GAP, FALSE_COVERAGE, UNSCORED)

# Judge concurrency. Retrieval already parallelises internally; this bounds the
# number of simultaneous LLM round trips.
_WORKERS = 6


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _retrieve(question: str, audience: Optional[str], top: int) -> List[Dict[str, Any]]:
    """Live hybrid retrieval. Side-effect free — `retrieve()` is documented as
    pure precisely so evaluation does not inflate the `uses` counters it is
    trying to measure."""
    from app.core import knowledge
    try:
        return knowledge.retrieve(question, "", audience=audience, top=top) or []
    except Exception as exc:                                # pragma: no cover
        logger.warning(f"retrieval failed for {question!r}: {exc}")
        return []


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You grade whether a retrieved knowledge-base article ANSWERS a user's "
    "question about a CRM product. You are strict about topic identity: an "
    "article about a DIFFERENT but similar object (accounts vs contacts, leads "
    "vs opportunities), or about the same object at a different layer (what it "
    "IS vs how to DO it), does not answer the question. "
    "Reply with ONLY one word: ANSWERED, PARTIAL, or WRONG.\n"
    "  ANSWERED - a user reading this article gets what they asked for.\n"
    "  PARTIAL  - genuinely related and helps, but leaves the question open.\n"
    "  WRONG    - about something else; acting on it would mislead."
)

_FIRST_WORD_RE = re.compile(r"[^A-Za-z]+")
_VALID_JUDGEMENTS = ("ANSWERED", "PARTIAL", "WRONG")


def _extract_verdict(text: Any) -> Optional[str]:
    """Pull the verdict out of a judge response.

    STRICT: the whole response, stripped of punctuation, must BE one of the
    three words. Returns None for anything else.

    Two looser versions were tried and both inverted verdicts, in opposite
    directions, on the same negation:

      * searching the whole response  — "this is not answered by the article"
        contains ANSWERED and scored as a pass.
      * accepting the first OR last word — "the article is not wrong" ends in
        WRONG, so 14 questions answered by the article of their exact title
        were scored FALSE_COVERAGE.

    Both failures came from reading a verdict out of prose that was never a
    verdict. The prompt asks for one word; anything else is the judge not
    answering the question, and is reported as unscored rather than guessed.
    A measuring instrument that fills in its own gaps is worse than one that
    admits them — which is precisely the property this audit tests the KB for.
    """
    cleaned = _FIRST_WORD_RE.sub(" ", str(text or "")).strip().upper()
    return cleaned if cleaned in _VALID_JUDGEMENTS else None


def _judge(question: str, hits: List[Dict[str, Any]]) -> str:
    """ANSWERED / PARTIAL / WRONG for the retrieved set."""
    if not hits:
        return "WRONG"
    try:
        from app.core.graph_utils import _get_llm
        joined = "\n\n".join(
            f"ARTICLE {i + 1}: {h.get('title', '')}\n{str(h.get('answer', ''))[:700]}"
            for i, h in enumerate(hits[:2]))
        prompt = f"QUESTION: {question}\n\n{joined}"
        llm = _get_llm()
        resp = llm.invoke([
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ])
        text = resp.content if hasattr(resp, "content") else str(resp)
        verdict = _extract_verdict(text)
        if verdict:
            return verdict

        # The strict extractor refuses prose, which left ~21% of the set
        # unscored on the first pass — the model explains itself despite being
        # asked not to. One retry that restates the constraint recovers most of
        # them. Retrying is honest in a way that loosening the parser was not:
        # it gets a real verdict rather than inferring one from a sentence that
        # never contained a judgement.
        resp = llm.invoke([
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": str(text)[:200]},
            {"role": "user", "content":
                "Reply with EXACTLY one word and nothing else: "
                "ANSWERED, PARTIAL, or WRONG."},
        ])
        text2 = resp.content if hasattr(resp, "content") else str(resp)
        return _extract_verdict(text2) or "UNSCORED"
    except Exception as exc:                                # pragma: no cover
        logger.warning(f"judge failed for {question!r}: {exc}")
        return "UNSCORED"


def _apply_must_cover(verdict: str, case: Dict[str, Any],
                      hits: List[Dict[str, Any]]) -> tuple:
    """Downgrade-only check for decisive facts. Returns (verdict, missing)."""
    required = case.get("must_cover") or []
    if not required or verdict != ANSWERED:
        return verdict, []
    blob = " ".join(f"{h.get('title', '')} {h.get('answer', '')}" for h in hits).lower()
    missing = [t for t in required if t.lower() not in blob]
    return (PARTIAL if missing else ANSWERED), missing


def _score_one(case: Dict[str, Any], audience: Optional[str],
               top: int) -> Dict[str, Any]:
    hits = _retrieve(case["question"], audience, top)
    if not hits:
        verdict, missing = GAP, []
    else:
        raw = _judge(case["question"], hits)
        verdict = {"ANSWERED": ANSWERED,
                   "PARTIAL": PARTIAL,
                   "WRONG": FALSE_COVERAGE,
                   "UNSCORED": UNSCORED}[raw]
        verdict, missing = _apply_must_cover(verdict, case, hits)

    return {
        "question":  case["question"],
        "category":  case["category"],
        "layer":     case["layer"],
        "verdict":   verdict,
        "top_title": hits[0].get("title") if hits else None,
        "n_hits":    len(hits),
        "missing_required": missing,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(audience: Optional[str] = None, top: int = 2,
        limit: Optional[int] = None) -> Dict[str, Any]:
    """Score the golden set. audience=None sees every tier — so a GAP under
    None is a real absence, not an audience restriction."""
    cases = GOLDEN[:limit] if limit else GOLDEN

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = list(pool.map(lambda c: _score_one(c, audience, top), cases))

    def _tally(key: str) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for r in results:
            bucket = out.setdefault(r[key], {v: 0 for v in _VERDICTS})
            bucket[r["verdict"]] += 1
        return out

    totals = {v: sum(1 for r in results if r["verdict"] == v) for v in _VERDICTS}
    n = len(results) or 1

    return {
        "audience":       audience or "all",
        "total":          len(results),
        "totals":         totals,
        "coverage_pct":   round(100.0 * totals[ANSWERED] / n, 1),
        # Reported on its own. This is the number that must go DOWN; it is the
        # only one a KB can improve by guessing.
        "false_coverage_pct": round(100.0 * totals[FALSE_COVERAGE] / n, 1),
        "honest_miss_pct":    round(100.0 * totals[GAP] / n, 1),
        "by_category":    _tally("category"),
        "by_layer":       _tally("layer"),
        "results":        results,
    }


def _print_report(rep: Dict[str, Any]) -> None:
    t = rep["totals"]
    print(f"\nKB COVERAGE AUDIT  —  audience={rep['audience']}  n={rep['total']}")
    print("=" * 72)
    print(f"  ANSWERED        {t[ANSWERED]:4}   ({rep['coverage_pct']}%)")
    print(f"  PARTIAL         {t[PARTIAL]:4}")
    print(f"  GAP             {t[GAP]:4}   ({rep['honest_miss_pct']}%  honest 'we don't know')")
    print(f"  FALSE_COVERAGE  {t[FALSE_COVERAGE]:4}   ({rep['false_coverage_pct']}%  <-- confidently wrong)")
    print(f"  UNSCORED        {t[UNSCORED]:4}   (judge returned no verdict)")

    print("\nBY CATEGORY" + " " * 12 + "ANSW  PART   GAP  FALSE")
    print("-" * 72)
    for cat in sorted(rep["by_category"],
                      key=lambda c: rep["by_category"][c][ANSWERED]):
        b = rep["by_category"][cat]
        print(f"  {cat:22} {b[ANSWERED]:4}  {b[PARTIAL]:4}  {b[GAP]:4}  {b[FALSE_COVERAGE]:5}")

    print("\nBY LAYER" + " " * 15 + "ANSW  PART   GAP  FALSE")
    print("-" * 72)
    for layer in LAYERS:
        b = rep["by_layer"].get(layer)
        if b:
            print(f"  {layer:22} {b[ANSWERED]:4}  {b[PARTIAL]:4}  {b[GAP]:4}  {b[FALSE_COVERAGE]:5}")

    false_hits = [r for r in rep["results"] if r["verdict"] == FALSE_COVERAGE]
    if false_hits:
        print(f"\nFALSE COVERAGE — answered confidently with the wrong article ({len(false_hits)}):")
        print("-" * 72)
        for r in false_hits:
            print(f"  Q: {r['question']}")
            print(f"     -> {r['top_title']}")

    gaps = [r for r in rep["results"] if r["verdict"] == GAP]
    if gaps:
        print(f"\nGAPS — nothing retrieved ({len(gaps)}):")
        print("-" * 72)
        for r in gaps:
            print(f"  [{r['category']:16}/{r['layer']:12}] {r['question']}")


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audience", default="all",
                    choices=("public", "internal", "all"))
    ap.add_argument("--top", type=int, default=2)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", default=None, help="write the full report here")
    args = ap.parse_args()

    rep = run(audience=None if args.audience == "all" else args.audience,
              top=args.top, limit=args.limit)
    _print_report(rep)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        print(f"\nfull report -> {args.json}")
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(_main())
