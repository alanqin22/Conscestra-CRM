"""Where the governance artefacts live. One answer, not thirteen.

    from app.core.artifact_paths import SQL_DIR, BASELINE, REPO_ROOT

WHY THIS EXISTS. `tests/`, `sql/`, `sp/` and `schema/` moved into a private
submodule at `governance/`, because the public repository cannot carry the full
schema and CI cannot verify anything without it. Thirteen call sites had each
computed those locations independently. Thirteen independent copies of one fact
is the definition of a value that will drift, and the drift would be silent:
a stale path yields "file not found", which several of these sites treat as
"nothing to check".

THE DEPTH TRAP, which is the real reason this is a module and not a constant.
Every one of those sites found its root by counting directory levels:

    ROOT = Path(__file__).resolve().parents[1]

From `tests/foo.py` that was the repository. From `governance/tests/foo.py` it
is `governance/` -- a directory with no `app/`, no `scripts/`, no `.github/`.
Worse, the two roots that used to be the same directory are now DIFFERENT
directories: code and workflows live at the repository root, artefacts live one
level down. A count cannot distinguish them; only a marker can.

So `REPO_ROOT` is found by walking up until a directory holds both `app/core`
and `scripts` -- a shape `governance/` does not have and cannot accidentally
acquire. Nesting the checkout deeper, or running from a worktree, no longer
changes the answer.

NO FALLBACK TO THE OLD LOCATIONS, deliberately. It would be easy to try
`governance/sql` and quietly use `sql/` when the submodule is absent. That is
precisely the failure this whole effort exists to remove: a verification that
did not really happen, reporting success. An uninitialised submodule must be
loud and immediate, so `require()` says what is wrong and the exact command
that fixes it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

__all__ = [
    "REPO_ROOT", "GOVERNANCE", "SQL_DIR", "SP_DIR", "SCHEMA_DIR", "TESTS_DIR",
    "BASELINE", "SUBMODULE_NAME", "available", "require", "rel",
    "PIN_FILE", "governance_pin",
]

SUBMODULE_NAME = "governance"

# The marker: a directory that contains BOTH of these is the repository root.
# `governance/` contains neither, so the walk cannot stop early.
_MARKERS = ("app/core", "scripts")


def _find_repo_root(start: Path) -> Path:
    for cand in (start, *start.parents):
        if all((cand / m).is_dir() for m in _MARKERS):
            return cand
    # Falling back to a guess here would hand every caller a plausible-looking
    # wrong path. Better to fail at import with the reason.
    raise RuntimeError(
        f"cannot locate the repository root above {start}: no ancestor "
        f"contains all of {_MARKERS}. artifact_paths must be imported from "
        f"inside a checkout.")


REPO_ROOT: Path = _find_repo_root(Path(__file__).resolve())

GOVERNANCE: Path = REPO_ROOT / SUBMODULE_NAME
SQL_DIR: Path = GOVERNANCE / "sql"
SP_DIR: Path = GOVERNANCE / "sp"
SCHEMA_DIR: Path = GOVERNANCE / "schema"
TESTS_DIR: Path = GOVERNANCE / "tests"
BASELINE: Path = SCHEMA_DIR / "00_base_schema.sql"


def available() -> bool:
    """Is the governance checkout present, as opposed to merely expected?

    Directory EXISTENCE proves nothing — a failed fetch leaves an empty
    directory behind, which is why this asks about contents. That was true of
    `git submodule add` and is equally true of a clone that failed partway.
    """
    return SQL_DIR.is_dir() and SCHEMA_DIR.is_dir()


# ---------------------------------------------------------------------------
# The pin replaced the submodule gitlink.
#
# governance/ WAS a submodule. It showed as a gitlink entry in the PUBLIC
# repository's file listing, which read as though the private schema and tests
# were published there — they never were, a gitlink is a commit id — but the
# appearance was the objection, and it is now cloned explicitly instead.
#
# The gitlink was doing one job besides fetching, and .governance-pin keeps it:
# recording WHICH governance commit a given public commit was verified against.
# Several tools decided "is this path version-controlled?" by looking for mode
# 160000 in the parent's index. With the gitlink gone that test answers "no"
# for every governance path, and each of those tools would report a fully
# version-controlled artefact as untracked — a false alarm, which by its own
# reasoning is how a governance tool gets muted. They ask here instead.
# ---------------------------------------------------------------------------

PIN_FILE: Path = REPO_ROOT / ".governance-pin"


def governance_pin() -> str | None:
    """The pinned governance commit sha, or None if nothing is pinned.

    Returns None rather than raising: callers use this to decide which
    repository owns a path, and a missing pin means "ask the parent", which is
    the correct answer for every non-governance path anyway.
    """
    try:
        if not PIN_FILE.is_file():
            return None
        for line in reversed(PIN_FILE.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                return line
            return None
    except OSError:
        return None
    return None


def require(*paths: Path) -> None:
    """Raise with an actionable message if the artefacts are not present.

    Called at the top of anything that reads them, so a missing submodule
    surfaces as one clear error rather than as a scatter of empty results that
    each look like "nothing to do".
    """
    if not available():
        raise FileNotFoundError(
            f"the `{SUBMODULE_NAME}` submodule is not checked out, so the "
            f"schema baseline, migrations and tests are unavailable.\n"
            f"  expected: {GOVERNANCE}\n"
            f"  fix     : git submodule update --init --recursive\n"
            f"  in CI   : actions/checkout with `submodules: recursive` AND a "
            f"token that can read the private governance repository "
            f"(GITHUB_TOKEN cannot).")
    for p in paths:
        if not p.exists():
            raise FileNotFoundError(
                f"{p} is missing from {SUBMODULE_NAME}/. The checkout is "
                f"present, so this is drift rather than a "
                f"setup problem: run "
                f"`python -m scripts.verify_artifact_sync --repo {GOVERNANCE} "
                f"--remote <owner>/<repo>` to see what else differs.")


def rel(path: Path) -> str:
    """Repo-root-relative POSIX path, for `git ls-files` and pytest arguments.

    Both take paths relative to the repository, and both are given them on
    Windows, where a backslash in a pytest node id or a git pathspec silently
    matches nothing.
    """
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def control_test_paths(names: Iterable[str]) -> list:
    """Bare test filenames -> paths pytest and git both accept."""
    return [rel(TESTS_DIR / n) for n in names]
