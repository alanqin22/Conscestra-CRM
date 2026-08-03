"""Verify a DEPLOYED environment. Run this after every Railway deploy.

WHY THIS EXISTS. CI runs 222 module imports and the memory benchmark. It runs
no tests, no invariants and no red team, because `tests/` and `sql/` live
outside the repository by policy — so a runner cannot build the schema and
cannot execute a single database control. Every safety property this system
claims is therefore verified only where the schema and the secrets actually
exist, which is the deployed environment itself.

A green CI tick has never been evidence about the database. This is.

    python -m scripts.postdeploy_verify                  # uses DB_DSN/DATABASE_URL
    python -m scripts.postdeploy_verify --target railway # uses RAILWAY_DB_URL

WHAT IT RUNS
  secret_health      are the guarded secrets real, strong and distinct
  verify_invariants  the DB-layer controls, asserted in SQL against live schema
  red_team           attacks executed, not enumerated

EXIT CODE is what a deploy pipeline should gate on: 0 means every control was
exercised and held. Anything else means a control is missing, disabled, or was
never installed on this database.

WRITES: verify_invariants and red_team both MUTATE rows — they plant probes,
attack them, and revert. Both check their own residue. That is deliberate: a
control tested only by reading catalogs is a control tested against its
description rather than its behaviour, which is how three separate broken
controls survived here for months.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Importing config is what loads .env. Any entry point that reads os.getenv
# before this sees a bare environment and concludes nothing is configured —
# which for a verification tool means reporting a false problem, or worse,
# verifying a database it did not mean to.
from app.core import config as _config          # noqa: E402,F401


def _target_dsn(argv) -> tuple[str, str]:
    """Return (label, dsn). Railway is opt-in and explicit — never a default."""
    if "--target" in argv:
        i = argv.index("--target")
        name = argv[i + 1] if i + 1 < len(argv) else ""
        if name == "railway":
            dsn = (os.getenv("RAILWAY_DB_URL") or "").strip()
            if not dsn:
                raise SystemExit("RAILWAY_DB_URL is not set")
            if "sslmode" not in dsn.lower():
                # 'prefer' silently falls back to plaintext over a public proxy.
                raise SystemExit(
                    "RAILWAY_DB_URL has no sslmode. libpq defaults to 'prefer', "
                    "which downgrades to an unencrypted connection without "
                    "telling you. Append ?sslmode=require.")
            return "railway", dsn
        raise SystemExit(f"unknown target {name!r} (known: railway)")
    dsn = (os.getenv("DATABASE_URL") or os.getenv("DB_DSN") or "").strip()
    if not dsn:
        raise SystemExit("no DATABASE_URL or DB_DSN configured")
    return "configured DSN", dsn


def _run(label: str, module: str, env: dict) -> tuple[str, int, str]:
    proc = subprocess.run([sys.executable, "-m", module], cwd=str(ROOT),
                          env=env, capture_output=True, text=True)
    return label, proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    label, dsn = _target_dsn(sys.argv)

    env = dict(os.environ)
    # Both variables are set, because different modules read different ones and
    # a half-applied override would silently verify the WRONG database — the
    # single worst outcome for a tool whose entire job is to tell the truth
    # about which database is safe.
    env["DB_DSN"] = dsn
    env["DATABASE_URL"] = dsn

    user = dsn.split("//", 1)[-1].split(":", 1)[0] if "//" in dsn else "?"
    host = dsn.split("@")[-1].split("/")[0] if "@" in dsn else "?"
    print(f"post-deploy verification — target: {label}")
    print(f"  connecting as '{user}' to {host}\n")

    stages = [("secrets", "app.core.secret_health"),
              ("invariants", "scripts.verify_invariants"),
              ("red team", "scripts.red_team")]

    failures = []
    for name, module in stages:
        stage, code, output = _run(name, module, env)
        ok = code == 0
        print(f"  {'PASS' if ok else 'FAIL'}  {stage}")
        if not ok:
            failures.append(stage)
            for line in output.strip().splitlines()[-14:]:
                print(f"        {line}")
        print()

    if failures:
        print(f"FAILED: {', '.join(failures)}")
        print("A deployed environment failing any of these is not safe to serve "
              "customer-facing claims. Do not roll forward.")
        return 1
    print("all post-deploy checks passed")
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
