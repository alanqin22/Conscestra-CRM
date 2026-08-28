"""The gate. One command that runs the whole verification path, fail-closed.

    python -m scripts.verify_gate            # provision, verify, tear down
    python -m scripts.verify_gate --keep     # leave the scratch database behind

WHY THIS EXISTS. Every control this codebase has is enforced by somebody
remembering to type `pytest`. CI runs 222 imports and a frozen benchmark; its
integration job is written but skipped, because `if: … has_base_schema ==
'true'` is false and A SKIPPED JOB DOES NOT FAIL A WORKFLOW. So the strongest
claims in the system are guarded by tests that a green tick says nothing about.

This is the sequence that has to run for that tick to mean something:

    provision   a fresh, empty database
    baseline    schema/00_base_schema.sql applied to it
    migrations  the declared chain, verified complete
    seed        the knowledge base, which the controls read
    controls    the security + detector suite, against THAT database

`evals` was a stage here and is not one now: eval_suite measures retrieval
quality, which is a property of an EMBEDDED corpus rather than of the schema,
so on a fresh build it fails for a reason unrelated to the code under test.
It is declared in NOT_GATED_HERE and printed on every run instead.

THE ONE RULE THAT MAKES IT A GATE: a stage that cannot run is a FAILURE, never
a skip. That is the entire defect being corrected — an absent verification
reported as success. Every early return below is non-zero.

WHAT IT DELIBERATELY DOES NOT VERIFY. The baseline is dumped with
--no-privileges, because CI has no `auth_owner` or `crm_app` roles to grant to
and including them would make the apply fail. Grant-based controls and
privilege separation are therefore NOT covered here and remain post-deployment
checks (scripts/postdeploy_verify.py). Saying so is the point: a gate that
implies coverage it does not have is the failure mode this replaces.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.artifact_paths import (                    # noqa: E402
    BASELINE, SQL_DIR, control_test_paths)

# The tests that run WITHOUT seed data. Measured, not guessed: each of these
# passes against a database built from the baseline alone (92 passed, 1 skipped
# on 2026-08-27). The wider suite reads ambient rows and would fail on an empty
# database for reasons that have nothing to do with the controls — so a gate
# built on it would be red for the wrong cause and get switched off.
CONTROL_TESTS: List[str] = control_test_paths([
    "test_readonly_role_enforcement.py",   # anonymous cannot write
    "test_capability_integrity.py",        # the 5 class detectors
    "test_approved_execution_params.py",   # R-11
    "test_irreversible_write_contracts.py",  # R-09 + R-12
    "test_write_call_sites.py",            # R-03 direct-write surface
    "test_schema_drift_rules.py",          # R-17/R-18 drift checker
    "test_profile_change_audit.py",        # R-13
    "test_u4_action_execution.py",         # R-14
    "test_sql_disposition_governance.py",  # SQL corpus classification
    "test_baseline_provenance.py",         # the baseline is truthful
    "test_workflow_cannot_skip_verification.py",  # the job cannot be skipped
    "test_artifact_sync_verifier.py",      # the sync check cannot cry wolf
])


# CHECKS THIS GATE DELIBERATELY DOES NOT MAKE, each with its reason and where
# it IS made instead. DECLARED rather than omitted: a gate that quietly covers
# less than a reader assumes is the same defect as a skipped job reported as
# success -- it just fails later, and with more confidence behind it.
NOT_GATED_HERE = [
    ("grants / privilege separation",
     "the baseline is dumped --no-privileges because CI has no auth_owner or "
     "crm_app role to grant to; including them would fail the apply. "
     "Verified post-deploy by scripts/postdeploy_verify.py."),
    ("eval_suite retrieval battery",
     "it measures RETRIEVAL QUALITY, a property of an EMBEDDED corpus rather "
     "than of the schema. Seeding gives article text but no vectors, so "
     "hybrid retrieval degrades to keyword-only and scores below threshold -- "
     "which would measure nothing about the code under test. Embedding in CI "
     "needs an API key, network and spend, and is not deterministic. Run it "
     "where the corpus is embedded: `python -m app.core.eval_suite` "
     "(measured local: 1,260 cases, accuracy 0.9897, GATE PASSED)."),
]


class Stage:
    def __init__(self, name: str, detail: str = ""):
        self.name, self.detail, self.ok, self.ran = name, detail, False, False
        self.secs = 0.0


def _psql() -> Optional[str]:
    exe = shutil.which("psql")
    if exe:
        return exe
    for v in ("18", "17", "16", "15"):
        p = Path(rf"C:\Program Files\PostgreSQL\{v}\bin\psql.exe")
        if p.exists():
            return str(p)
    return None


def _admin_dsn(dsn: str) -> str:
    """Same server, `postgres` database — where CREATE DATABASE is issued."""
    return dsn.rsplit("/", 1)[0] + "/postgres"


def _run(cmd: List[str], env: Optional[dict] = None,
         cwd: Optional[str] = None) -> Tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True,
                       env=env or os.environ.copy(), cwd=cwd or str(ROOT))
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _ci_count(by) -> str:
    """The controls count, reported with its scope attached.

    "184 controls verified" is true and misleading: grants and the retrieval
    evals are outside this gate, so an unqualified number invites a reader to
    believe production privileges were checked too. The count is printed as
    CI-ENFORCED, immediately above the list of what is not.
    """
    d = by["controls"].detail
    for tok in d.replace(",", " ").split():
        if tok.isdigit():
            return tok
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="do not drop the scratch database")
    ap.add_argument("--dsn", default=os.getenv("GATE_DSN") or
                    os.getenv("DATABASE_URL") or os.getenv("DB_DSN"),
                    help="a server to provision the scratch database on")
    args = ap.parse_args()

    print("=" * 66)
    print("  CONSCESTRA VERIFICATION GATE")
    print("=" * 66)

    stages = [Stage("provision"), Stage("baseline"), Stage("migrations"),
              Stage("seed"), Stage("controls")]
    by = {s.name: s for s in stages}
    scratch = f"gate_{uuid.uuid4().hex[:10]}"
    scratch_dsn = ""

    def report(code: int) -> int:
        print("\n" + "-" * 66)
        for s in stages:
            mark = "PASS" if s.ok else ("FAIL" if s.ran else "DID NOT RUN")
            print(f"  {s.name:11s} {mark:12s} {s.secs:6.1f}s  {s.detail}")
        print("-" * 66)
        # A stage that never ran is a failure. That sentence is the gate.
        unrun = [s.name for s in stages if not s.ran]
        if unrun:
            print(f"  VERDICT: FAIL — these never executed: {', '.join(unrun)}")
            print("           An absent verification is not a passing one.")
            return code or 1
        if any(not s.ok for s in stages):
            print("  VERDICT: FAIL")
            return code or 1
        print("  VERDICT: PASS -- every stage ran and held")
        print()
        print(f"  {_ci_count(by)} CI-ENFORCED CONTROLS (this gate alone). "
              f"NOT a total-coverage figure --")
        print("  the following are governed elsewhere, not here:")
        print()
        for name, why in NOT_GATED_HERE:
            print(f"  NOT GATED HERE: {name}")
            for i in range(0, len(why), 64):
                print(f"      {why[i:i+64]}")
        return 0

    # ---- provision ---------------------------------------------------------
    t = time.time()
    psql = _psql()
    if not psql:
        by["provision"].ran = True
        by["provision"].detail = "psql not found on PATH"
        return report(1)
    if not args.dsn:
        by["provision"].ran = True
        by["provision"].detail = "no DSN (set GATE_DSN or DATABASE_URL)"
        return report(1)
    if not BASELINE.is_file():
        by["provision"].ran = True
        by["provision"].detail = f"baseline missing: {BASELINE}"
        return report(1)

    by["provision"].ran = True
    admin = _admin_dsn(args.dsn)
    rc, out = _run([psql, admin, "-q", "-c", f'CREATE DATABASE "{scratch}";'])
    by["provision"].secs = time.time() - t
    if rc != 0:
        by["provision"].detail = f"CREATE DATABASE failed: {out.strip()[:120]}"
        return report(1)
    scratch_dsn = args.dsn.rsplit("/", 1)[0] + "/" + scratch
    by["provision"].ok = True
    by["provision"].detail = f"scratch db {scratch}"
    print(f"  provisioned {scratch}")

    try:
        # ---- baseline ------------------------------------------------------
        t = time.time()
        by["baseline"].ran = True
        rc, out = _run([psql, scratch_dsn, "--file", str(BASELINE),
                        "--single-transaction", "--set", "ON_ERROR_STOP=1"])
        by["baseline"].secs = time.time() - t
        if rc != 0:
            err = next((l for l in out.splitlines() if "ERROR" in l), out[:120])
            by["baseline"].detail = err.strip()[:120]
            return report(1)
        by["baseline"].ok = True
        by["baseline"].detail = f"{BASELINE.name} applied"
        print(f"  baseline applied")

        env = os.environ.copy()
        env["DATABASE_URL"] = scratch_dsn
        env["CRM_REQUIRE_DB"] = "1"          # env-shaped skips become failures

        # ---- migrations ----------------------------------------------------
        t = time.time()
        by["migrations"].ran = True
        rc, out = _run([sys.executable, "-m", "scripts.migrate", "--check"], env)
        by["migrations"].secs = time.time() - t
        by["migrations"].ok = rc == 0
        by["migrations"].detail = ("declared chain complete" if rc == 0
                                   else out.strip().splitlines()[-1][:120]
                                   if out.strip() else "migrate --check failed")
        print(f"  migrations: {'ok' if rc == 0 else 'FAILED'}")

        # ---- seed ----------------------------------------------------------
        # Content, not schema — and required, because the eval stage measures
        # RETRIEVAL. An empty knowledge base yields zero cases, and a suite
        # that evaluates nothing exits 0 while proving nothing. Seeding it is
        # what makes that stage capable of failing, which is the only reason
        # to run it.
        t = time.time()
        by["seed"].ran = True
        seed = SQL_DIR / "seed_kb_articles.sql"
        if not seed.is_file():
            by["seed"].detail = f"missing: {seed.name}"
            return report(1)
        rc, out = _run([psql, scratch_dsn, "--file", str(seed),
                        "--single-transaction", "--set", "ON_ERROR_STOP=1"])
        by["seed"].secs = time.time() - t
        by["seed"].ok = rc == 0
        by["seed"].detail = ("knowledge base seeded" if rc == 0
                             else next((l for l in out.splitlines()
                                        if "ERROR" in l), out[:110]).strip()[:110])
        print(f"  seed: {'ok' if rc == 0 else 'FAILED'}")

        # ---- controls ------------------------------------------------------
        t = time.time()
        by["controls"].ran = True
        rc, out = _run([sys.executable, "-m", "pytest", "-q",
                        "-p", "no:cacheprovider", *CONTROL_TESTS], env)
        by["controls"].secs = time.time() - t
        tail = [l for l in out.splitlines() if "passed" in l or "failed" in l]
        by["controls"].ok = rc == 0
        by["controls"].detail = (tail[-1][:110] if tail else "no result line")
        if rc != 0:
            print("  controls: FAILED")
            for l in out.splitlines():
                if l.startswith("FAILED"):
                    print(f"    {l[:110]}")
        else:
            print(f"  controls: {by['controls'].detail}")

        return report(0)
    finally:
        if args.keep:
            print(f"\n  scratch database kept: {scratch}")
        else:
            _run([psql, admin, "-q", "-c",
                  f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE);'])


if __name__ == "__main__":
    sys.exit(main())
