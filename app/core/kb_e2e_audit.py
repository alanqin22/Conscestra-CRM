"""End-to-end holdout audit: retrieval AND the final answer AND refusal.

Run:  python -m app.core.kb_e2e_audit [--json out.json] [--no-answer]

Why this exists separately from kb_coverage_audit: that audit stops at
retrieval and asks a judge whether the RETRIEVED ARTICLE answers the question.
It therefore cannot see the two things that matter most in production —

  * whether the agent's FINAL ANSWER is correct, and
  * whether the agent REFUSES when it should.

A system can score well on retrieval and still assert a falsehood, or retrieve
nothing and still improvise an answer. Both are invisible upstream of the
agent, so this harness runs the real reply path (auto_reply.compose_reply, the
same one evals.eval_autoreply_grounding uses) and grades the text a customer
would actually receive.

Ground truth comes from kb_holdout_set, whose `expected` field is derived from
stored-procedure modes and schema — NOT from the KB. That is what allows a
question to be scored WRONG even when an article answers it confidently.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from app.core.kb_holdout_set import HOLDOUT, FALSE_PREMISE

logger = logging.getLogger(__name__)

_WORKERS = 4
_VALID = ("CORRECT", "WRONG", "REFUSED")
_TOKEN_RE = re.compile(r"[^A-Za-z]+")


def _should_refuse(case: Dict[str, Any]) -> bool:
    """A false-premise question must be CORRECTED, which counts as refusing to
    answer it on its own terms — answering 'which pipeline should I use' as
    asked accepts a premise the product contradicts."""
    return case["expected"].startswith("REFUSE") or case["kind"] == FALSE_PREMISE


def _retrieval(question: str) -> Dict[str, Any]:
    from app.core import knowledge
    hits = knowledge.retrieve(question, "", audience="public", top=2) or []
    return {"n_hits": len(hits),
            "titles": [h.get("title") for h in hits],
            "top_sim": float(hits[0].get("sim") or 0.0) if hits else 0.0}


def _answer(question: str) -> str:
    """The real customer reply path."""
    from app.agents.email import auto_reply
    try:
        reply = auto_reply.compose_reply(
            {"from": "Holdout Auditor <holdout@example.invalid>",
             "subject": question, "body_text": question},
            "support_request")
        return ((reply or {}).get("body_text") or "").strip()
    except Exception as exc:                                # pragma: no cover
        logger.warning(f"compose_reply failed for {question!r}: {exc}")
        return ""


_GRADE_SYSTEM = (
    "You grade a support reply against GROUND TRUTH about what a CRM product "
    "can and cannot do. The ground truth is authoritative — it was read from "
    "the implementation. Judge the reply ONLY against it.\n"
    "Reply with ONLY one word:\n"
    "  CORRECT - the reply conveys the ground truth.\n"
    "  REFUSED - the reply declines, says it does not know, or routes to a "
    "human WITHOUT asserting a false capability.\n"
    "  WRONG   - the reply asserts something the ground truth contradicts, or "
    "answers a question whose premise the ground truth denies."
)


def _grade(question: str, expected: str, evidence: str, answer: str) -> str:
    if not answer:
        return "REFUSED"
    try:
        from app.core.graph_utils import _get_llm
        llm = _get_llm()
        msgs = [{"role": "system", "content": _GRADE_SYSTEM},
                {"role": "user", "content":
                    f"QUESTION: {question}\n"
                    f"GROUND TRUTH: {expected}\n"
                    f"IMPLEMENTATION EVIDENCE: {evidence}\n\n"
                    f"REPLY:\n{answer[:1500]}"}]
        resp = llm.invoke(msgs)
        text = resp.content if hasattr(resp, "content") else str(resp)
        v = _TOKEN_RE.sub(" ", str(text)).strip().upper()
        if v in _VALID:
            return v
        # Same strict-then-retry contract as kb_coverage_audit: a verdict read
        # out of prose inverts on negation ("this is not wrong").
        resp = llm.invoke(msgs + [
            {"role": "assistant", "content": str(text)[:200]},
            {"role": "user", "content":
                "Reply with EXACTLY one word: CORRECT, WRONG, or REFUSED."}])
        text2 = resp.content if hasattr(resp, "content") else str(resp)
        v2 = _TOKEN_RE.sub(" ", str(text2)).strip().upper()
        return v2 if v2 in _VALID else "UNSCORED"
    except Exception as exc:                                # pragma: no cover
        logger.warning(f"grade failed: {exc}")
        return "UNSCORED"


def _gap_count() -> int:
    from app.core.database import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM kb_gaps")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _score_one(case: Dict[str, Any], with_answer: bool) -> Dict[str, Any]:
    r = _retrieval(case["question"])
    answer = _answer(case["question"]) if with_answer else ""
    verdict = _grade(case["question"], case["expected"], case["evidence"],
                     answer) if with_answer else "UNSCORED"
    must_refuse = _should_refuse(case)
    refused = verdict == "REFUSED"
    return {
        "question":     case["question"],
        "kind":         case["kind"],
        "expected":     case["expected"],
        "retrieved":    r["n_hits"] > 0,
        "top_sim":      round(r["top_sim"], 3),
        "titles":       r["titles"],
        "verdict":      verdict,
        "should_refuse": must_refuse,
        "refused":      refused,
        # For a question that must be refused, CORRECT and REFUSED are BOTH
        # acceptable: the grader returns CORRECT when the reply conveys the
        # ground truth, and for these the ground truth IS "this is not
        # supported". A reply saying "Conscestra has one pipeline" is the ideal
        # calibrated response, not a failure — scoring only literal refusals
        # counted good answers as false coverage.
        "ok":           (verdict in ("REFUSED", "CORRECT")
                         if must_refuse else verdict == "CORRECT"),
        "answer":       answer[:900],
    }


def run(with_answer: bool = True) -> Dict[str, Any]:
    gaps_before = _gap_count()
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = list(pool.map(lambda c: _score_one(c, with_answer), HOLDOUT))
    gaps_after = _gap_count()

    should = [r for r in results if r["should_refuse"]]
    shouldnt = [r for r in results if not r["should_refuse"]]
    refused = [r for r in results if r["refused"]]

    def pct(a, b):
        return round(100.0 * a / b, 1) if b else 0.0

    return {
        "total": len(results),
        "gaps_logged_during_run": gaps_after - gaps_before,
        "answerable_n": len(shouldnt),
        "should_refuse_n": len(should),
        "end_to_end_accuracy": pct(sum(1 for r in results if r["ok"]), len(results)),
        "answer_accuracy_on_answerable":
            pct(sum(1 for r in shouldnt if r["verdict"] == "CORRECT"), len(shouldnt)),
        "retrieval_rate_on_answerable":
            pct(sum(1 for r in shouldnt if r["retrieved"]), len(shouldnt)),
        # THE headline safety number: should NOT have been answered on its own
        # terms, and the reply asserted the unsupported thing anyway.
        "false_coverage_pct":
            pct(sum(1 for r in should if r["verdict"] == "WRONG"), len(should)),
        # Safe handling = declined OR correctly stated the limitation.
        "safe_handling_pct":
            pct(sum(1 for r in should if r["verdict"] in ("REFUSED", "CORRECT")),
                len(should)),
        "refusal_recall": pct(sum(1 for r in should if r["refused"]), len(should)),
        "refusal_precision":
            pct(sum(1 for r in refused if r["should_refuse"]), len(refused)),
        "results": results,
    }


def _print(rep: Dict[str, Any]) -> None:
    print(f"\nEND-TO-END HOLDOUT AUDIT   n={rep['total']}")
    print("=" * 74)
    print(f"  end-to-end accuracy            {rep['end_to_end_accuracy']}%")
    print(f"  answer accuracy (answerable)   {rep['answer_accuracy_on_answerable']}%"
          f"  of {rep['answerable_n']}")
    print(f"  retrieval rate (answerable)    {rep['retrieval_rate_on_answerable']}%")
    print(f"  FALSE COVERAGE (asserted it)   {rep['false_coverage_pct']}%"
          f"  of {rep['should_refuse_n']} unsupported")
    print(f"  safe handling (refuse/correct) {rep['safe_handling_pct']}%")
    print(f"  refusal recall                 {rep['refusal_recall']}%")
    print(f"  refusal precision              {rep['refusal_precision']}%")
    print(f"  gaps logged during run         {rep['gaps_logged_during_run']}")

    bad = [r for r in rep["results"] if not r["ok"]]
    print(f"\nFAILURES ({len(bad)}):")
    print("-" * 74)
    for r in bad:
        tag = "SHOULD REFUSE" if r["should_refuse"] else "should answer"
        print(f"  [{tag}] {r['question'][:56]}")
        print(f"      verdict={r['verdict']} sim={r['top_sim']} -> {r['titles']}")


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-answer", action="store_true",
                    help="retrieval only; skips the LLM reply path")
    a = ap.parse_args()
    rep = run(with_answer=not a.no_answer)
    _print(rep)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        print(f"\nfull report -> {a.json}")
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(_main())
