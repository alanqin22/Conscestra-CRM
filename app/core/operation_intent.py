"""Operation extraction for CRM-user imperatives.

THE DEFECT THIS ADDRESSES (Phase 6, measured)

    "Merge these duplicate contacts."  -> mode=duplicates
    "Archive these contacts."          -> mode=get_details  (firstName='these',
                                                             lastName='contacts.')
    "Restore these contacts."          -> mode=list

The module pre-routers recognise operations only in a colon-prefixed protocol
form ("merge contacts:", "archive contact:"). Free-form requests fall through to
heuristics that match NOUN PHRASES and never consult the leading VERB, so
"merge these duplicate contacts" matched the substring 'duplicate contacts' and
became a duplicates search. "Archive these contacts" was parsed as a person
named "these contacts".

None of these produced a false success — the system did the wrong thing and
reported the wrong thing honestly — but a request to MERGE that silently
returns a SEARCH has not been served.

THE RULE
    operation identified + target identified  -> route to that operation
    operation identified + NO target          -> do NOT substitute; hand to the
                                                 agent so it can ask
Never let a noun-phrase heuristic outrank an explicit verb.

WHY "no target -> passthrough" RATHER THAN A GUESS
The pre-router contract is binary: a concrete {mode, params} or passthrough to
the module's LLM agent. There is no "ask the user" mode. The agents already ask
when passed through — the accounting agent replies "please provide the invoice
UUID" for a void with no id. Passthrough therefore produces the honest outcome
the substitution prevented, without inventing a new response channel.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Imperative operation verbs -> the SP p_mode they must reach.
_OPERATIONS = {
    "merge":       r"merge|combine|de-?duplicate|dedupe",
    "archive":     r"archive",
    "restore":     r"restore|unarchive|un-?delete",
    "delete":      r"delete",
    "convert":     r"convert",
    "qualify":     r"qualify",
    "disqualify":  r"disqualify",
    "score":       r"score",
}

# Discovery verbs. Present so "show me duplicate contacts" is NOT treated as an
# operation request — it genuinely IS a search, and must keep working.
_DISCOVERY_RE = re.compile(
    r"^\s*(show|list|find|get|search|display|give me|check|view)\b", re.I)

# Evidence that the user named WHICH records to act on.
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
# A capitalised personal name, but not a sentence-initial capital.
_NAME_RE = re.compile(r"(?<!^)\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?\b")
# Vague plural referents that look like targets but identify nothing.
_VAGUE_RE = re.compile(
    r"^\s*(these|those|them|the|my|all|any|duplicate|dupes?)\b", re.I)


def extract(message: str) -> Optional[Dict[str, Any]]:
    """Return {operation, has_target, target} for an imperative, else None.

    None means "not an operation request" — the caller keeps its existing
    behaviour untouched.
    """
    text = (message or "").strip()
    if not text or _DISCOVERY_RE.match(text):
        return None

    op = None
    for name, pat in _OPERATIONS.items():
        # The verb must lead the request. "How do I merge…" and "Can I merge…"
        # are questions, already claimed by the knowledge boundary upstream;
        # requiring first position keeps this from second-guessing that.
        if re.match(rf"^\s*(please\s+)?({pat})\b", text, re.I):
            op = name
            break
    if op is None:
        return None

    target = None
    for rx in (_UUID_RE, _EMAIL_RE, _PHONE_RE):
        m = rx.search(text)
        if m:
            target = m.group(0).strip()
            break
    if target is None:
        # A proper noun counts, but "these contacts" does not.
        rest = re.sub(rf"^\s*(please\s+)?({_OPERATIONS[op]})\b", "", text,
                      flags=re.I).strip()
        if not _VAGUE_RE.match(rest):
            m = _NAME_RE.search(text)
            if m:
                target = m.group(0).strip()

    return {"operation": op, "has_target": target is not None, "target": target}
