"""Can each signal actually fail? Plant the defect; expect red.

A control that is never exercised is a belief, not a control. Every defect this
codebase has produced was of one shape: a signal that could not fail. The health
endpoint returned "healthy" without touching the database. The regression gate
recomputed the formula it was guarding, so mutating the product moved nothing.
"deletion log armed" asked whether a row existed in pg_trigger, and a DISABLED
trigger is still a row. Each reported success while the thing it described was
broken, and none was caught by reading the code.

So this runs the experiment: break what a signal claims to detect, and check
that it turns red.

    python -m scripts.mutation_audit            # every mutation
    python -m scripts.mutation_audit --list     # names only, run nothing

WHY -B AND A CLEARED CACHE, WHICH IS NOT OPTIONAL
-------------------------------------------------
The first sweep run by hand reported two false MISSES. Mutating

    _W_DAYS, _W_SOURCES, _W_WORDINGS = 0.40, 0.25, 0.35
 -> _W_DAYS, _W_SOURCES, _W_WORDINGS = 0.50, 0.25, 0.25

produces a file of the SAME BYTE LENGTH, and the write landed inside the same
mtime second as the original. CPython validates a cached .pyc on (mtime, size),
so both matched and the stale bytecode was reused: the subprocess imported the
ORIGINAL code and the harness concluded the gate had missed a real regression.

The failure direction matters. A mutation harness fooled by caching reports
"this signal cannot fail" when it can — it manufactures alarming false findings
— and, with the mutation inverted, would just as happily report "this signal is
fine" about one that is broken. It lies in whichever direction the cache
happens to favour, which is the one thing a measuring instrument may not do.

Three defences, all cheap:
  * -B and PYTHONDONTWRITEBYTECODE=1  — the child writes no cache
  * __pycache__ removed before every run — no cache to read
  * mtime advanced past the granularity — even a stale check would miss
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import config as _config          # noqa: E402,F401  (loads .env)

CONSOLIDATION = ROOT / "app" / "core" / "memory_consolidation.py"


# ── Running a signal so the result is about the CODE, not a cache ────────────

def _clear_bytecode() -> None:
    for cache in ROOT.joinpath("app").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for cache in ROOT.joinpath("scripts").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def run_signal(module: str) -> int:
    """Exit code of a signal, guaranteed to reflect the source on disk."""
    _clear_bytecode()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-B", "-m", module],
                          cwd=str(ROOT), capture_output=True, text=True, env=env)
    return proc.returncode


class SourceMutation:
    """Replace a literal in a source file, restore it whatever happens."""

    def __init__(self, path: Path, old: str, new: str):
        self.path, self.old, self.new = path, old, new
        self._backup: Optional[str] = None

    def __enter__(self):
        self._backup = self.path.read_text(encoding="utf-8")
        if self.old not in self._backup:
            raise AssertionError(
                f"anchor not present in {self.path.name}: {self.old!r}\n"
                "A mutation whose anchor has moved silently tests nothing.")
        self.path.write_text(self._backup.replace(self.old, self.new, 1),
                             encoding="utf-8")
        # Same-length replacements land inside one mtime second and a stale
        # .pyc validates on (mtime, size). Move past the granularity.
        time.sleep(1.1)
        return self

    def __exit__(self, *exc):
        self.path.write_text(self._backup, encoding="utf-8")
        time.sleep(1.1)
        assert self.path.read_text(encoding="utf-8") == self._backup
        return False


def verify_mutation_is_live(path: Path, old: str, new: str, probe: str) -> bool:
    """Prove the mutated source is what actually executes.

    Without this the whole harness is unfalsifiable: every result would be
    consistent with 'the mutation never ran'."""
    with SourceMutation(path, old, new):
        _clear_bytecode()
        env = dict(os.environ); env["PYTHONDONTWRITEBYTECODE"] = "1"
        got = subprocess.run([sys.executable, "-B", "-c", probe], cwd=str(ROOT),
                             capture_output=True, text=True, env=env).stdout.strip()
    _clear_bytecode()
    env = dict(os.environ); env["PYTHONDONTWRITEBYTECODE"] = "1"
    base = subprocess.run([sys.executable, "-B", "-c", probe], cwd=str(ROOT),
                          capture_output=True, text=True, env=env).stdout.strip()
    return got != base


# ── The mutations ────────────────────────────────────────────────────────────
# (name, signal module, file, old, new). Each breaks something a signal claims.

MUTATIONS: List[Tuple[str, str, Path, str, str]] = [
    ("certainty floor moved", "app.core.memory_bench", CONSOLIDATION,
     "CERTAINTY_FLOOR = 0.35", "CERTAINTY_FLOOR = 0.40"),
    ("certainty span moved", "app.core.memory_bench", CONSOLIDATION,
     "CERTAINTY_SPAN  = 0.60", "CERTAINTY_SPAN  = 0.55"),
    ("certainty cap raised", "app.core.memory_bench", CONSOLIDATION,
     "CERTAINTY_CAP   = 0.95", "CERTAINTY_CAP   = 0.99"),
    ("trust weights reweighted", "app.core.memory_bench", CONSOLIDATION,
     "_W_DAYS, _W_SOURCES, _W_WORDINGS = 0.40, 0.25, 0.35",
     "_W_DAYS, _W_SOURCES, _W_WORDINGS = 0.50, 0.25, 0.25"),
    # eval.determinism re-consolidates an entity twice and compares evidence
    # hashes. Non-determinism there silently un-verifies human approvals,
    # because evidence_hash is what invalidates a verification. It cannot be
    # probed by planting DATA — only by making the code non-deterministic — so
    # it lives here rather than in the observability audit.
    ("evidence hash order-dependent", "app.core.memory_bench", CONSOLIDATION,
     "def _evidence_hash(", "def _evidence_hash_unsorted("),
    ("one wording starts corroborating", "app.core.memory_bench", CONSOLIDATION,
     "+ _W_WORDINGS * min(1.0, max(0, n_templates - 1) / 2.0))",
     "+ _W_WORDINGS * min(1.0, max(0, n_templates) / 2.0))"),
]


def main() -> int:
    if "--list" in sys.argv:
        for name, module, *_ in MUTATIONS:
            print(f"  {name:36} -> {module}")
        return 0

    print("MUTATION AUDIT — planting each defect and checking the signal fails\n")

    # The harness itself must be shown to work before its results mean anything.
    live = verify_mutation_is_live(
        CONSOLIDATION, "CERTAINTY_FLOOR = 0.35", "CERTAINTY_FLOOR = 0.40",
        "from app.core import memory_consolidation as M;"
        "print(M.certainty_from_breadth(0.0))")
    print(f"  harness self-check — mutated source is what executes: {live}")
    if not live:
        print("\n  ABORTING. The mutation did not reach the interpreter, so every\n"
              "  result below would be indistinguishable from 'signal cannot\n"
              "  fail'. Check bytecode caching before trusting any of this.")
        return 2
    print()

    missed = []
    for name, module, path, old, new in MUTATIONS:
        try:
            with SourceMutation(path, old, new):
                code = run_signal(module)
        except AssertionError as exc:
            print(f"  {'ANCHOR LOST':13} {name}\n                {exc}")
            missed.append(name)
            continue
        caught = code != 0
        print(f"  {'CAUGHT' if caught else '*** MISSED ***':13} {name}")
        if not caught:
            missed.append(name)

    baseline = run_signal("app.core.memory_bench")
    print(f"\n  negative control (unmutated) exits {baseline} — must be 0")
    if baseline != 0:
        print("  the suite does not pass unmutated; every CAUGHT above is "
              "meaningless because the signal was already red.")
        return 2

    if missed:
        print(f"\n  {len(missed)} signal(s) could not detect their own defect:")
        for m in missed:
            print(f"    - {m}")
        return 1
    print(f"\n  all {len(MUTATIONS)} mutations detected")
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
