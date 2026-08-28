"""Does the remote actually hold what the local repository holds?

    python -m scripts.verify_artifact_sync --repo D:/tmp/gov_stage \
        --remote alanqin22/Conscestra-CRM-governance --ref main

WHY THIS EXISTS. Governance artefacts now live in a second repository that CI
clones as a submodule. If that mirror is missing a file, the gate does not fail
loudly -- it collects fewer controls and still reports success, which is the
Stage 3 defect wearing a new hat. So the two file sets have to be compared, and
the comparison itself has to be trustworthy.

IT WAS NOT. The first version of this check was an inline snippet that read
`git ls-files` and split the output on whitespace. Two files in sql/ are named
`addresses table.sql` and `leads table.sql`. Splitting on whitespace turned each
into two paths that do not exist, so the check reported 3 files MISSING from
GitHub and 2 files EXTRA on it. Nothing was missing. Nothing was extra. The
push had been byte-perfect, and the commit SHA already proved it.

That is the failure being corrected, and it is worth stating precisely: a
detector that invents a defect is not safer than one that overlooks a defect.
Both mean the output cannot be acted on without re-deriving it by hand, which
is the same as having no detector. A governance check that cries wolf gets
muted, and a muted check is indistinguishable from a skipped one.

THE FIX, and why `-z` rather than smarter parsing. `git ls-files -z` emits
NUL-terminated paths. NUL is the one byte a POSIX or Windows filename cannot
contain, so the parse is unambiguous by construction rather than by escaping
cleverness. It also turns off git path quoting: without `-z`, a path holding a
non-ASCII byte comes back wrapped in double quotes with octal escapes, which a
line-based reader treats as a filename that does not exist.

SYMMETRY. Both directions come from one set difference, so `missing` and
`extra` cannot disagree about what they were computed from. An earlier drift
checker here (R-18) was made WORSE by filtering one side only; the diff grew
from 17 to 21. Asymmetry is how a comparison starts describing its own filter
instead of the data.

AND IT NAMES ITS OWN BUG. If the tokenization defect is ever reintroduced --
here, or in any tool feeding this one -- the residue has a signature: a
"missing" path that is a whitespace-delimited fragment of an "extra" path.
`tokenization_signature()` looks for exactly that and reports it as A BUG IN
THE VERIFIER rather than as drift in the data. A tool that recognises its own
characteristic failure is the difference between a false alarm and a false
alarm somebody can act on.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# enumeration
# --------------------------------------------------------------------------

def tracked_files(repo: Path) -> Set[str]:
    """Every path in the index, NUL-delimited.

    `-z` is load-bearing twice over: it makes the delimiter a byte that cannot
    occur inside a filename, AND it suppresses the octal-escaped quoting git
    otherwise applies to non-ASCII paths. Do not "simplify" this to
    `.stdout.split()` or `.splitlines()`; both have shipped bugs here.
    """
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(repo),
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed in {repo}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    # Split the BYTES on b"\0" and decode each path separately: a malformed
    # byte sequence then corrupts at most one path, instead of resynchronising
    # the whole stream onto the wrong boundaries.
    return {p.decode("utf-8", "surrogateescape")
            for p in proc.stdout.split(b"\0") if p}


def remote_files(remote: str, ref: str, gh: str) -> Tuple[Set[str], str]:
    """The blob paths in a GitHub tree, plus the commit SHA they came from.

    Blobs only: `tree` entries are directories and `commit` entries are
    submodule gitlinks, neither of which is a file either side can hold.
    """
    sha = _gh(gh, ["api", f"repos/{remote}/commits/{ref}", "--jq", ".sha"]).strip()
    tree = json.loads(_gh(gh, ["api", f"repos/{remote}/git/trees/{sha}?recursive=1"]))
    if tree.get("truncated"):
        # Fail closed. A truncated tree yields a SHORT remote set, so every
        # absent path would be reported as missing -- a false alarm of exactly
        # the kind this module exists to prevent.
        raise RuntimeError(
            f"GitHub truncated the tree for {remote}@{sha[:7]}; the remote file "
            f"set would be incomplete and every absent path reported as "
            f"missing. Refusing to compare against a partial listing.")
    return ({e["path"] for e in tree["tree"] if e["type"] == "blob"}, sha)


def _gh(gh: str, args: List[str]) -> str:
    proc = subprocess.run([gh, *args], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()}")
    return proc.stdout


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def _fragments(path: str) -> Set[str]:
    """Every run of consecutive whitespace-delimited tokens in `path`.

    These are precisely the strings a naive `.split()` reader could mistake for
    filenames. `"sql/addresses table.sql"` yields `"sql/addresses"` and
    `"table.sql"` -- the two phantom paths from the original false alarm.
    """
    tok = path.split()
    if len(tok) < 2:
        return set()
    return {" ".join(tok[i:j])
            for i in range(len(tok))
            for j in range(i + 1, len(tok) + 1)} - {path}


def tokenization_signature(missing: Iterable[str],
                           extra: Iterable[str]) -> List[str]:
    """Differences that are the residue of whitespace splitting, not real drift.

    The signature is specific: a path reported MISSING that is a token-fragment
    of a path reported EXTRA. Real drift does not look like this -- a genuinely
    absent file is not a substring of a genuinely present one.
    """
    frag_of = {}
    for e in extra:
        for f in _fragments(e):
            frag_of.setdefault(f, e)
    return sorted(f"{m!r} is a whitespace fragment of {frag_of[m]!r}"
                  for m in missing if m in frag_of)


@dataclass
class SyncReport:
    local_count: int
    remote_count: int
    missing: List[str] = field(default_factory=list)   # local, absent remotely
    extra: List[str] = field(default_factory=list)     # remote, absent locally
    verifier_bugs: List[str] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not self.missing and not self.extra

    @property
    def trustworthy(self) -> bool:
        """False when the differences bear the tokenization signature: the
        report is then describing the reader, not the repositories."""
        return not self.verifier_bugs


def compare(local: Set[str], remote: Set[str]) -> SyncReport:
    """Symmetric by construction: both directions from the same two sets.

    No per-side filtering here, deliberately. Filtering one side and not the
    other is how the R-18 drift checker came to report 21 differences where
    there had been 17 -- it had begun measuring its own exclusion list.
    """
    missing = sorted(local - remote)
    extra = sorted(remote - local)
    return SyncReport(
        local_count=len(local), remote_count=len(remote),
        missing=missing, extra=extra,
        verifier_bugs=tokenization_signature(missing, extra),
    )


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def _render(rep: SyncReport, label_l: str, label_r: str) -> None:
    print(f"  {'':12} {label_l:>10} {label_r:>10}")
    print(f"  {'files':12} {rep.local_count:>10} {rep.remote_count:>10}")
    if rep.verifier_bugs:
        print("\n  ** THIS REPORT IS NOT TRUSTWORTHY -- VERIFIER BUG **")
        print("     The differences below bear the whitespace-tokenization")
        print("     signature: a reader split a path on spaces. Fix the reader")
        print("     (`git ls-files -z`). Do not act on the diff.")
        for b in rep.verifier_bugs:
            print(f"       {b}")
        return
    if rep.in_sync:
        print(f"\n  IN SYNC: {rep.local_count} paths, identical on both sides.")
        return
    for lbl, paths in ((f"MISSING from {label_r}", rep.missing),
                       (f"EXTRA on {label_r}", rep.extra)):
        if paths:
            print(f"\n  {lbl} ({len(paths)}):")
            for p in paths[:25]:
                print(f"    {p}")
            if len(paths) > 25:
                print(f"    ... and {len(paths) - 25} more")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--repo", default=str(ROOT), help="local git repository")
    ap.add_argument("--remote", required=True, help="owner/name on GitHub")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--gh", default=r"C:\Program Files\GitHub CLI\gh.exe")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    local = tracked_files(Path(a.repo))
    remote, sha = remote_files(a.remote, a.ref, a.gh)
    rep = compare(local, remote)

    if a.json:
        print(json.dumps({"remote_sha": sha, "in_sync": rep.in_sync,
                          "trustworthy": rep.trustworthy,
                          "local": rep.local_count, "remote": rep.remote_count,
                          "missing": rep.missing, "extra": rep.extra,
                          "verifier_bugs": rep.verifier_bugs}, indent=2))
    else:
        print("=" * 64)
        print(f"  ARTEFACT SYNC  {a.repo}  ->  {a.remote}@{sha[:7]}")
        print("=" * 64)
        _render(rep, "local", "GitHub")

    # A verifier bug exits non-zero too: an untrustworthy report must never be
    # mistaken for a clean one.
    return 0 if (rep.in_sync and rep.trustworthy) else 1


if __name__ == "__main__":
    sys.exit(main())
