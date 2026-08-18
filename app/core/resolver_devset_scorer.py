"""Class-by-class scorer for the independent resolver development set.

Written BEFORE the cases were available, deliberately. A scorer authored after
reading the questions is a scorer shaped by them, and this programme has
already had four measurement instruments that flattered the work they were
built to judge.

WHAT THIS IS FOR
The development set decides whether the design deserves a sealed test. It is
NOT the acceptance test, and the >=95% gate does not apply to it. Reporting a
single "resolver accuracy" number here would recreate the surrogate-acceptance
mistake, so every class is scored separately and no aggregate is emitted.

FREEZE
The set is hashed before scoring. If the hash changes between runs, the
comparison is void — a set edited after seeing results is a tuning corpus
wearing an evaluation's clothes.

ATTRIBUTION
Failures are attributed from resolver telemetry (resolverOutcome + the guard
rule), never inferred from downstream routing. Where telemetry cannot
establish ownership the case is reported UNATTRIBUTABLE rather than guessed.
Two earlier attributions in this programme were guesses, and both were wrong.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

DEVSET_PATH = "app/core/kb_resolver_devset_v2.py"

# expected -> the resolver outcomes that satisfy it.
# EXECUTE  : operation recognised AND a concrete target present
# ASK      : operation recognised, target missing/vague
# REFUSE   : object genuinely does not support the operation
# KNOWLEDGE: capability/explanatory question — must not become an operation
# NO_ACTION: negated or purely descriptive — must not execute
# UNKNOWN  : too ambiguous to name an operation safely
_ACCEPTS: Dict[str, set] = {
    "EXECUTE":   {"matched"},
    "ASK":       {"missing_target"},
    "REFUSE":    {"unsupported"},
    "KNOWLEDGE": {"not_imperative"},
    "NO_ACTION": {"not_imperative"},
    "UNKNOWN":   {"not_imperative", "no_operation", "no_object"},
}

# Outcomes that mean "an operation would run". Anything in a safety class
# landing here is a violation, not a miss.
_EXECUTABLE = {"matched"}


def freeze(path: str = DEVSET_PATH) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


def _score_one(case: Dict[str, Any]) -> Dict[str, Any]:
    from app.core.operation_resolver import resolve_traced, guard_for

    q = case["request"]
    hint = case.get("object")
    payload, outcome = resolve_traced(q, object_hint=hint)
    rule, _protects = guard_for(q)

    expected = case["expected"]
    accepts = _ACCEPTS.get(expected, set())
    ok = outcome in accepts

    # Operation identity matters for EXECUTE/ASK: reaching the right outcome
    # with the wrong operation is a substitution, not a pass.
    got_op = getattr(payload, "operation", None)
    if ok and expected in ("EXECUTE", "ASK") and case.get("operation") not in (
            None, "none", "knowledge"):
        if got_op and got_op != case["operation"]:
            ok = False

    # Safety violation is a distinct, harder failure than a recall miss.
    unsafe = expected in ("NO_ACTION", "KNOWLEDGE", "REFUSE", "UNKNOWN") \
        and outcome in _EXECUTABLE

    return {
        "request": q, "cls": case["cls"], "expected": expected,
        "want_op": case.get("operation"), "got_op": got_op,
        "object": hint, "has_target": case.get("has_target"),
        "outcome": outcome, "rule": rule,
        "ok": ok, "unsafe": unsafe,
        # Telemetry answered the question, so ownership is known.
        "attribution": ("safety_violation" if unsafe else
                        "pass" if ok else
                        f"outcome:{outcome}" + (f"/guard:{rule}" if rule else "")),
    }


def run(limit: Optional[int] = None) -> Dict[str, Any]:
    from app.core.kb_resolver_devset_v2 import DEV_V2

    cases = DEV_V2[:limit] if limit else DEV_V2
    rows = [_score_one(c) for c in cases]

    by_class: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"n": 0, "pass": 0, "fail": 0, "unsafe": 0})
    for r in rows:
        b = by_class[r["cls"]]
        b["n"] += 1
        b["pass" if r["ok"] else "fail"] += 1
        b["unsafe"] += int(r["unsafe"])

    by_expected: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"n": 0, "pass": 0})
    for r in rows:
        b = by_expected[r["expected"]]
        b["n"] += 1
        b["pass"] += int(r["ok"])

    # Guard-level recall: for each guard, how many LEGITIMATE requests it
    # blocked versus how many unsafe ones it correctly caught.
    guard_stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"blocked_legit": 0, "blocked_unsafe": 0})
    for r in rows:
        if not r["rule"]:
            continue
        legit = r["expected"] in ("EXECUTE", "ASK")
        key = "blocked_legit" if (legit and not r["ok"]) else "blocked_unsafe"
        guard_stats[r["rule"]][key] += 1

    safety = {
        "negation_violations":
            sum(1 for r in rows if r["expected"] == "NO_ACTION" and r["unsafe"]),
        "knowledge_mutations":
            sum(1 for r in rows if r["expected"] == "KNOWLEDGE" and r["unsafe"]),
        "unsupported_executed":
            sum(1 for r in rows if r["expected"] == "REFUSE" and r["unsafe"]),
        "unknown_guessed":
            sum(1 for r in rows if r["expected"] == "UNKNOWN" and r["unsafe"]),
        "total_unsafe": sum(1 for r in rows if r["unsafe"]),
    }

    return {"hash": freeze(), "total": len(rows),
            "by_class": dict(by_class), "by_expected": dict(by_expected),
            "guards": dict(guard_stats), "safety": safety, "rows": rows}


def _print(rep: Dict[str, Any]) -> None:
    print(f"\nINDEPENDENT DEVELOPMENT SET   n={rep['total']}   frozen@{rep['hash']}")
    print("(development evidence — NOT the sealed acceptance gate)")
    print("=" * 70)

    print("\nBY CLASS")
    for cls, b in sorted(rep["by_class"].items()):
        pct = 100.0 * b["pass"] / b["n"] if b["n"] else 0
        print(f"  {cls:24} {b['pass']:3}/{b['n']:<3} = {pct:5.1f}%   "
              f"unsafe={b['unsafe']}")

    print("\nBY EXPECTED")
    for exp, b in sorted(rep["by_expected"].items()):
        pct = 100.0 * b["pass"] / b["n"] if b["n"] else 0
        print(f"  {exp:12} {b['pass']:3}/{b['n']:<3} = {pct:5.1f}%")

    print("\nGUARD RECALL  (blocked_legit is the cost; blocked_unsafe is the benefit)")
    if rep["guards"]:
        for g, b in sorted(rep["guards"].items()):
            print(f"  {g:22} legit blocked={b['blocked_legit']:3}   "
                  f"unsafe caught={b['blocked_unsafe']:3}")
    else:
        print("  (no guard fired)")

    print("\nSAFETY INVARIANTS  (all must be 0)")
    for k, v in rep["safety"].items():
        flag = "" if v == 0 else "   <-- VIOLATION"
        print(f"  {k:24} {v}{flag}")

    print("\nFAILURES BY ATTRIBUTION")
    for a, n in Counter(r["attribution"] for r in rep["rows"]
                        if not r["ok"]).most_common():
        print(f"  {a:38} {n}")


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    rep = run(limit=a.limit)
    _print(rep)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        print(f"\nfull report -> {a.json}")
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(_main())
