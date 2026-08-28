"""Where does the control count come from? Derived, never asserted.

    python -m scripts.control_inventory          # human-readable
    python -m scripts.control_inventory --json   # machine-readable

WHY THIS EXISTS. Stage 3 reported "193 CI-enforced controls". That number was
read off a pytest summary line and retyped into a report -- which is how
`ci.yml` came to claim "1039 tests" and "1088 tests" and "200 tables", all
three wrong, all three believed. A governance metric that is maintained by hand
becomes false the first time nobody updates it, and it stays believed because
it is specific.

So the number is COMPUTED from the repository: the gate's own control list is
the single source, pytest is asked what it actually collects, and the result is
partitioned into what CI enforces versus what it explicitly does not.

THE PARTITION IS THE POINT. "193 controls verified" is true and misleading if a
reader takes it to include production privilege separation or retrieval
quality. Those are real controls, verified elsewhere, and this tool reports
them in their own bucket rather than letting them be absorbed into one
flattering total.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _collect(paths) -> dict:
    """Ask pytest what it actually collects. Not a grep for `def test_`:
    parametrised cases multiply, skipif does not reduce collection, and a
    file that fails to import collects nothing at all -- which a grep would
    happily count as full coverage."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", *paths],
        cwd=str(ROOT), capture_output=True, text=True)
    ids = [ln.strip() for ln in proc.stdout.splitlines()
           if "::" in ln and not ln.startswith(("=", "-", "<"))]
    per_file: dict = {}
    for i in ids:
        per_file.setdefault(i.split("::")[0].replace("\\", "/"), []).append(i)
    return {"count": len(ids), "per_file": per_file, "ok": proc.returncode == 0,
            "stderr": proc.stderr[-400:] if proc.returncode else ""}


def _tracked_anywhere(paths) -> list:
    """Which of these paths would a CLEAN CLONE receive?

    Asking the parent repository alone is wrong now and answers "none". These
    artefacts live in the `governance` submodule, and a parent tracks a
    submodule as a GITLINK, not as files -- `git ls-files governance/tests/x.py`
    is empty even though a clean clone with --recurse-submodules gets the file.

    Reported as untracked, that emptiness said "the control count describes
    this machine rather than the repository" about artefacts that are, in fact,
    fully version-controlled. A governance tool that cries wolf gets muted, so
    the question is asked of the repository that actually owns each path:

      * a path inside a submodule counts as tracked when the PARENT records
        the gitlink (so a clone knows to fetch it) AND the SUBMODULE's index
        carries the file. Either half alone is not enough -- a gitlink to a
        repo missing the file still yields nothing on disk.
      * anything else is asked of the parent, as before.
    """
    from app.core.artifact_paths import REPO_ROOT
    import collections

    def _git(cwd, args):
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True).stdout

    # Group by owning repository so each gets one call rather than one per path.
    by_repo = collections.defaultdict(list)
    for p in paths:
        head = p.split("/", 1)[0]
        link = _git(REPO_ROOT, ["ls-files", "-s", head])
        if link.startswith("160000") and "/" in p:
            by_repo[head].append(p.split("/", 1)[1])
        else:
            by_repo[""].append(p)

    found = []
    for repo, inner in by_repo.items():
        root = REPO_ROOT / repo if repo else REPO_ROOT
        got = set(_git(root, ["ls-files", *inner]).split())
        found += [(f"{repo}/{i}" if repo else i) for i in inner if i in got]
    return found


def build() -> dict:
    from scripts.verify_gate import CONTROL_TESTS, NOT_GATED_HERE
    from app.core.artifact_paths import BASELINE, TESTS_DIR, rel
    baseline_rel, tests_rel = rel(BASELINE), rel(TESTS_DIR)

    gate = _collect(CONTROL_TESTS)
    whole = _collect([tests_rel])

    tracked = _tracked_anywhere([*CONTROL_TESTS, baseline_rel])

    return {
        "ci_enforced": {
            "count": gate["count"],
            "collected_ok": gate["ok"],
            "source": "scripts.verify_gate.CONTROL_TESTS",
            "enforced_by": "python -m scripts.verify_gate (fail-closed, "
                           "non-zero exit on any stage that fails OR does not run)",
            "files": {f: len(v) for f, v in sorted(gate["per_file"].items())},
        },
        "local_only": {
            "count": max(whole["count"] - gate["count"], 0),
            "why": "reads ambient database rows; fails on a freshly built "
                   "database for reasons unrelated to the controls",
            "enforced_by": "pytest, run by a person",
        },
        "not_ci_enforced": [{"control": n, "reason": w} for n, w in NOT_GATED_HERE],
        # THE CLAIM THIS TOOL EXISTS TO REFUSE. A count is only CI-enforced if
        # CI can obtain the files that produce it. Untracked artefacts pass on
        # a developer's machine and do not exist in a clean checkout.
        "reproducible_from_a_clean_checkout": {
            "tracked": sorted(tracked),
            "untracked": sorted(set(CONTROL_TESTS + [baseline_rel])
                                - set(tracked)),
        },
        "totals": {"collected_by_pytest": whole["count"]},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    inv = build()
    if args.json:
        print(json.dumps(inv, indent=2))
        return 0

    ci = inv["ci_enforced"]
    print("=" * 68)
    print("  CONSCESTRA CONTROL INVENTORY  (derived, not asserted)")
    print("=" * 68)
    print(f"\n  {ci['count']:>5}  CI-ENFORCED controls")
    print(f"         source: {ci['source']}")
    print(f"         gate  : {ci['enforced_by']}")
    for f, n in ci["files"].items():
        print(f"           {n:>4}  {f}")
    print(f"\n  {inv['local_only']['count']:>5}  LOCAL-ONLY tests "
          f"(not gate-viable)")
    print(f"         why: {inv['local_only']['why']}")
    print(f"\n         NOT CI-ENFORCED, by declaration:")
    for e in inv["not_ci_enforced"]:
        print(f"           - {e['control']}")

    un = inv["reproducible_from_a_clean_checkout"]["untracked"]
    print("\n" + "-" * 68)
    if un:
        print(f"  ** THE {ci['count']} IS NOT YET REPRODUCIBLE FROM A CLEAN "
              f"CHECKOUT **")
        print(f"     {len(un)} required artefact(s) are untracked, so a fresh")
        print(f"     clone cannot run the gate at all:")
        for u in un:
            print(f"       {u}")
        print("     Until these are governed by version control, the number")
        print("     describes this machine rather than the repository.")
        return 1
    print(f"  The {ci['count']} is reproducible from a clean checkout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
