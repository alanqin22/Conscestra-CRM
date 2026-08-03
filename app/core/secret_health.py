"""Are the secrets this system depends on actually secrets?

Every finding this module encodes was found by looking, not by being told:

  * MEMORY_SIGNING_KEY sat on a development placeholder while the assertion
    gate, the verification trail and four red-team controls were all built on
    top of it. Nothing anywhere said so.
  * UNSUBSCRIBE_SECRET could silently fall back to a literal string published
    in the repository, making every opt-out link forgeable.
  * A `.env.backup-*` written during rotation held every secret in the project
    and was not matched by `.gitignore`.

The pattern is always the same: a weak secret behaves EXACTLY like a strong one
until someone attacks it. There is no failing test, no error, no log line — the
system is simply not protected, and looks fine. So this reports the state
explicitly, at startup and on demand, rather than waiting to be asked.

It reports; it does not raise. A process that refuses to boot over a weak
development key is a process nobody runs locally, and the check gets deleted.
The one place it does refuse is where refusing is the compliant answer, and that
lives in consent.py, not here.

    python -m app.core.secret_health
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List

# Importing config is what loads .env. Without it this module reads a bare
# environment and reports every secret as "not set" — a check that cries wolf
# on a correctly configured machine is a check that gets ignored, which is the
# precise failure mode it exists to prevent.
from app.core import config as _config          # noqa: F401  (import for effect)

# Substrings that mean "nobody chose this on purpose".
_PLACEHOLDER_MARKERS = ("dev", "test", "change", "placeholder", "example",
                        "sample", "default", "secret", "password", "todo",
                        "xxx", "local", "insecure")

# name -> (what breaks if it is weak, minimum sensible length)
_GUARDED = {
    "MEMORY_SIGNING_KEY": (
        "the assertion gate — anyone holding it can mint a memory the system "
        "will state to a customer as verified fact", 32),
    "UNSUBSCRIBE_SECRET": (
        "CASL opt-out links — a guessable key lets anyone unsubscribe any "
        "address", 24),
    "ADMIN_API_TOKEN": (
        "every admin endpoint", 24),
    "GOV_LINK_SECRET": (
        "governance approval links", 24),
}


def _fingerprint(value: str) -> str:
    """Identify a secret without revealing it, so two deployments can be
    compared and a rotation can be confirmed from a log."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _assess(name: str, why: str, min_len: int) -> Dict[str, Any]:
    raw = (os.getenv(name, "") or "").strip()
    problems: List[str] = []
    if not raw:
        problems.append("not set")
    else:
        if len(raw) < min_len:
            problems.append(f"only {len(raw)} chars (want >= {min_len})")
        low = raw.lower()
        hit = [m for m in _PLACEHOLDER_MARKERS if m in low]
        if hit:
            problems.append(f"looks like a placeholder (contains {hit[0]!r})")
        if len(set(raw)) < 8:
            problems.append("too few distinct characters to be random")
    return {"name": name, "configured": bool(raw),
            "fingerprint": _fingerprint(raw) if raw else None,
            "length": len(raw), "protects": why,
            "problems": problems, "ok": not problems}


def report() -> Dict[str, Any]:
    """Full state. Never contains a secret value — only length and fingerprint."""
    findings = [_assess(n, why, ln) for n, (why, ln) in _GUARDED.items()]

    # Two secrets serving one purpose is a rotation trap: changing either one
    # breaks the other, so in practice neither ever gets rotated.
    shared: List[str] = []
    seen: Dict[str, str] = {}
    for name in _GUARDED:
        v = (os.getenv(name, "") or "").strip()
        if not v:
            continue
        if v in seen:
            shared.append(f"{name} and {seen[v]} are the SAME value")
        seen[v] = name

    return {"secrets": findings,
            "shared_values": shared,
            "weak": [f["name"] for f in findings if not f["ok"]],
            "ok": all(f["ok"] for f in findings) and not shared}


def log_report(logger) -> None:
    """Called once at startup. Silent when everything is well."""
    r = report()
    if r["ok"]:
        logger.info("[secrets] all guarded secrets look configured and strong")
        return
    for f in r["secrets"]:
        if not f["ok"]:
            logger.warning(f"[secrets] {f['name']}: {'; '.join(f['problems'])} "
                           f"— protects {f['protects']}")
    for s in r["shared_values"]:
        logger.warning(f"[secrets] {s} — rotating one silently breaks the other")


if __name__ == "__main__":                                   # pragma: no cover
    import json
    r = report()
    print(json.dumps(r, indent=2))
    raise SystemExit(0 if r["ok"] else 1)
