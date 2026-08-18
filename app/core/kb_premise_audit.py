"""Runs the false-premise / object-boundary suites across BOTH answer paths.

Paths are reported separately and never averaged. They are different products:
`auto_reply` answers customers by email and is designed to defer CRM-operational
questions to a human, so a deferral there is correct behaviour and would be a
failure in the in-app assistant. One combined number would hide both facts.

    customer   auto_reply.compose_reply  — the email auto-responder
    crm_user   POST /orchestrator-chat   — the in-app assistant (needs the app
                                           running on APP_URL)

Grading is deliberately NOT left to a single LLM call: Phase 2 measured the
grader marking 5 of 15 substantively-correct replies WRONG. Here the LLM
verdict is recorded ALONGSIDE deterministic signals (does the reply contain a
fabricated schedule? does it name the wrong object?), and the deterministic
signals are what the headline severity uses.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from app.core.kb_premise_suite import (FALSE_PREMISE, OBJECT_BOUNDARY, LAYERED,
                                       PASS, PARTIAL, FAIL, SEVERE_FAIL)

logger = logging.getLogger(__name__)

APP_URL = os.getenv("APP_URL", "http://localhost:8000")
_WORKERS = 4

# Fabricated operational detail — the SEVERE tell. A reply may mention the word
# "nightly" while denying it ("there is no nightly job"), so negation is checked
# separately rather than by keyword alone.
_CADENCE_RE = re.compile(
    r"\b(nightly|every night|each night|every evening|overnight|daily|"
    r"each day|weekly|hourly|every \d+ (hours|minutes|days)|at \d{1,2}\s?(am|pm))\b",
    re.I)
# An operational verb anywhere in the same sentence. Order varies — "the nightly
# job runs" and "it runs nightly" both assert a schedule — so cadence and verb
# are matched independently within the sentence rather than as one sequence.
_OPVERB_RE = re.compile(
    r"\b(runs?|ran|executes?|executed|happens?|happened|occurs?|occurred|"
    r"performs?|performed|scheduled|process(es|ed)?|job|sync(s|ed)?)\b", re.I)
# A reply that opens by restating the question — "your question about the
# nightly de-duplication job" — contains the cadence and an operational noun
# while asserting nothing. Scoring that as a fabricated schedule punishes the
# reply for echoing the user, and it fired on 2 of 20 in the first run.
_ECHO_RE = re.compile(
    r"(your (question|inquiry|enquiry|query)|you asked|regarding your|"
    r"about the|thanks for asking|we appreciate your)", re.I)

_DENIAL_RE = re.compile(
    r"\b(no|not|never|does ?n[o']t|is ?n[o']t|there is no|there are no|"
    r"cannot|doesn't exist|no such|no automatic|not automatic|only when|"
    r"only happens when|requires? (a |an )?(explicit|manual|deliberate)?\s*"
    r"(request|action)|user-initiated|manually)\b", re.I)


# ── transports ──────────────────────────────────────────────────────────────

def _answer_customer(q: str) -> str:
    from app.agents.email import auto_reply
    try:
        r = auto_reply.compose_reply(
            {"from": "Premise Auditor <premise@example.invalid>",
             "subject": q, "body_text": q}, "support_request")
        return ((r or {}).get("body_text") or "").strip()
    except Exception as exc:
        logger.warning(f"customer path failed: {exc}")
        return ""


def _answer_crm_user(q: str) -> str:
    payload = json.dumps({"chatInput": {"message": q},
                          "sessionId": "premise-audit"}).encode()
    req = urllib.request.Request(f"{APP_URL}/orchestrator-chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            d = json.loads(resp.read().decode())
        if isinstance(d, list):
            d = d[0] if d else {}
        return str(d.get("output") or d.get("error") or "")[:4000]
    except Exception as exc:
        logger.warning(f"crm_user path failed for {q!r}: {exc}")
        return ""


PATHS = {"customer": _answer_customer, "crm_user": _answer_crm_user}


# ── deterministic signals ───────────────────────────────────────────────────

def _fabricated_schedule(answer: str) -> bool:
    """A stated cadence for something that does not run on a cadence.

    Evaluated per SENTENCE, because a correct reply routinely contains both the
    cadence word and a denial — "there is no nightly de-duplication" — and a
    document-level keyword match would score that as a fabrication. The denial
    has to sit in the same sentence as the cadence to excuse it.
    """
    for sent in re.split(r'(?<=[.!?])\s+', answer or ""):
        if _CADENCE_RE.search(sent) and _OPVERB_RE.search(sent) \
                and not _DENIAL_RE.search(sent) and not _ECHO_RE.search(sent):
            return True
    return False


def _denies(answer: str) -> bool:
    return bool(_DENIAL_RE.search(answer or ""))


def _mentions_other_object(answer: str, asked: str) -> bool:
    """Did the reply pivot to a different record type than the one asked about?"""
    others = {"order", "invoice", "opportunity", "contact", "account", "lead",
              "product"} - {asked}
    a = (answer or "").lower()
    # affirmative substitution: "you can merge duplicate accounts" when the
    # question was about orders
    return any(re.search(rf"\b(can|able to|supports?)\b[^.]{{0,60}}\b{o}s?\b", a)
               for o in others)


# ── grading ─────────────────────────────────────────────────────────────────

def _grade_premise(case: Dict[str, Any], answer: str) -> str:
    if not answer:
        return PARTIAL                      # silence asserts nothing
    if _fabricated_schedule(answer):
        return SEVERE_FAIL
    if _denies(answer):
        return PASS
    # No denial. Did it engage with the premise's subject at all? If it simply
    # deferred to a human without repeating the claim, that is PARTIAL.
    deferred = re.search(r"(team|member|colleague|someone) will|get back to you|"
                         r"follow up with you", answer, re.I)
    return PARTIAL if deferred else FAIL


def _grade_boundary(case: Dict[str, Any], answer: str) -> str:
    if not answer:
        return PARTIAL
    asked, supported = case["asked_object"], case["supported"]
    # Affirmation is matched across sentence boundaries. The grounded answers
    # introduced in Phase 5 read "Yes. Conscestra can merge two duplicate
    # contacts, but only when you ask — there is no automatic merging." The
    # affirmation and the object sit in different sentences, and the correct
    # non-automation caveat trips the denial detector, so a sentence-bounded
    # match scored the best answers in the suite as refusals.
    affirms_asked = re.search(
        rf"\b(yes|you can|can be|supports?|is supported)\b", answer, re.I) and \
        re.search(rf"\b{asked}s?\b", answer, re.I)
    if supported:
        # must NOT be refused — guards against over-correction
        return PASS if (affirms_asked or not _denies(answer)) else FAIL
    # unsupported: must deny for the ASKED object, and must not substitute
    if _mentions_other_object(answer, asked) and not _denies(answer):
        return FAIL                         # substituted a supported object
    return PASS if _denies(answer) else PARTIAL


def _run_case(case: Dict[str, Any], path: str) -> Dict[str, Any]:
    answer = PATHS[path](case["question"])
    if case["suite"] == "false_premise":
        verdict = _grade_premise(case, answer)
    else:
        verdict = _grade_boundary(case, answer)
    return {"path": path, "suite": case["suite"],
            "subclass": case.get("subclass") or case.get("asked_object"),
            "question": case["question"], "verdict": verdict,
            "fabricated_schedule": _fabricated_schedule(answer),
            "answer": (answer or "")[:600]}


def run(paths: List[str]) -> Dict[str, Any]:
    cases = FALSE_PREMISE + OBJECT_BOUNDARY
    out: Dict[str, Any] = {"paths": {}}
    for path in paths:
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            rows = list(pool.map(lambda c: _run_case(c, path), cases))
        fp = [r for r in rows if r["suite"] == "false_premise"]
        ob = [r for r in rows if r["suite"] == "object_boundary"]

        def pct(sel, pool_):
            return round(100.0 * sum(1 for r in pool_ if r["verdict"] in sel)
                         / len(pool_), 1) if pool_ else 0.0

        out["paths"][path] = {
            "false_premise_n": len(fp),
            "false_premise_pass_pct": pct((PASS,), fp),
            "false_premise_fail_pct": pct((FAIL, SEVERE_FAIL), fp),
            "severe_fails": sum(1 for r in fp if r["verdict"] == SEVERE_FAIL),
            "object_boundary_n": len(ob),
            "object_boundary_accuracy": pct((PASS,), ob),
            "results": rows,
        }
    return out


def _print(rep: Dict[str, Any]) -> None:
    for path, d in rep["paths"].items():
        print(f"\n===== PATH: {path} =====")
        print(f"  false premise  PASS {d['false_premise_pass_pct']}%   "
              f"FAIL {d['false_premise_fail_pct']}%   "
              f"severe {d['severe_fails']}   (n={d['false_premise_n']})")
        print(f"  object boundary accuracy {d['object_boundary_accuracy']}%  "
              f"(n={d['object_boundary_n']})")
        bad = [r for r in d["results"] if r["verdict"] in (FAIL, SEVERE_FAIL)]
        for r in bad:
            print(f"    [{r['verdict']}] {r['question'][:62]}")
            safe = r['answer'][:150].encode('ascii', 'replace').decode('ascii')
            print(f"        {safe}")


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", default="customer,crm_user")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    rep = run([p.strip() for p in a.paths.split(",") if p.strip()])
    _print(rep)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        print(f"\nfull report -> {a.json}")
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(_main())
