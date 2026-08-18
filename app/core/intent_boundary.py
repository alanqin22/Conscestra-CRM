"""Intent boundary for the CRM-user path — knowledge vs action.

THE DISTINCTION THIS EXISTS TO MAKE

    "Can I merge duplicate contacts?"    -> KNOWLEDGE  (what is true)
    "How do I merge duplicate contacts?" -> KNOWLEDGE  (still asking, not doing)
    "Merge these duplicate contacts."    -> ACTION     (do it)
    "Show me duplicate contacts."        -> LOOKUP     (fetch data)
    "Give me the contact report."        -> REPORT

"How do I…" is the trap. It contains an action verb and reads imperative to a
keyword router, which is why `_route_single()` sent it to the module agent that
performs the operation. It is a request for instructions, not for execution.

WHY CLASSIFICATION IS CONSERVATIVE
Misrouting an ACTION to knowledge is worse than the reverse: the user asked for
work to be done and instead gets prose, which is a functional regression on a
path whose job is execution. Misrouting a KNOWLEDGE question to the module path
merely restores today's behaviour. So the predicate answers KNOWLEDGE only on
positive evidence of a question, and defaults to the existing router otherwise.

MIXED INTENT
"Does Conscestra merge duplicates automatically? If not, show me the
duplicates." is both. It is classified MIXED: the knowledge half is answered
(and any false premise corrected) FIRST, then the operational half continues
down the normal controlled path. The knowledge answer never executes anything
itself.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

KNOWLEDGE, ACTION, LOOKUP, REPORT, MIXED, UNKNOWN = (
    "knowledge", "action", "lookup", "report", "mixed", "unknown")

# ── knowledge markers ───────────────────────────────────────────────────────
# Interrogatives about capability, behaviour or procedure.
_KNOWLEDGE_RE = re.compile(
    r"^\s*(can|could|does|do|is|are|will|would|should|may|must)\s+(i|we|you|it|"
    r"the\s+\w+|conscestra|the\s+system)\b"
    r"|^\s*how\s+(do|does|can|would|should)\s+(i|we|you|it|the)\b"
    r"|^\s*what\s+(is|are|does|do|happens?|makes?)\b"
    r"|^\s*(why|when)\s+(is|are|does|do|would|should)\b"
    r"|^\s*(tell me|explain|describe)\b"
    r"|\bis it possible\b|\bam i able to\b|\bwhat'?s the difference\b",
    re.I)

# ── action markers ──────────────────────────────────────────────────────────
# Imperative mood, or an explicit demand naming a target.
_ACTION_RE = re.compile(
    r"^\s*(please\s+)?(merge|archive|restore|delete|remove|convert|qualify|"
    r"disqualify|score|create|add|update|change|assign|void|send|schedule|"
    r"log|complete|reopen|stop|disable|enable|set)\b", re.I)

# ── data-fetch markers ──────────────────────────────────────────────────────
_LOOKUP_RE = re.compile(
    r"^\s*(show|list|find|get|search|display|give me|pull up|look up|"
    r"who is|which)\b", re.I)

_REPORT_RE = re.compile(
    r"\b(report|dashboard|summary|analytics|forecast|breakdown|"
    r"pipeline (summary|by stage)|kpi|pulse)\b", re.I)

# A knowledge question and an instruction joined together.
_CONJUNCTION_RE = re.compile(
    r"\b(and (then )?(please )?(merge|archive|delete|create|show|list|do it)"
    r"|if (so|not),? (please )?(merge|archive|delete|show|list)"
    r"|then (merge|archive|delete|show|list))\b", re.I)


def classify(message: str) -> Dict[str, Any]:
    """Return {intent, knowledge_part, action_part, why}.

    Only `knowledge` and `mixed` may be answered from the KB. Everything else —
    including UNKNOWN — continues to the existing orchestrator untouched.
    """
    text = (message or "").strip()
    if not text:
        return {"intent": UNKNOWN, "knowledge_part": None,
                "action_part": None, "why": "empty"}

    is_question = bool(_KNOWLEDGE_RE.search(text)) or text.rstrip().endswith("?")
    is_action = bool(_ACTION_RE.match(text))
    is_lookup = bool(_LOOKUP_RE.match(text))
    is_report = bool(_REPORT_RE.search(text))

    # MIXED first: "Can I merge these, and please merge them now."
    if is_question and _CONJUNCTION_RE.search(text):
        return {"intent": MIXED, "knowledge_part": text, "action_part": text,
                "why": "question joined to an instruction"}

    # An imperative wins over a trailing question mark — "Merge these
    # contacts?" is still a request to merge.
    if is_action:
        return {"intent": ACTION, "knowledge_part": None, "action_part": text,
                "why": "imperative verb in first position"}

    if is_lookup:
        # "Show me the executive dashboard" is a report; "show me duplicate
        # contacts" is a lookup. Both are operational, neither is knowledge.
        return {"intent": REPORT if is_report else LOOKUP,
                "knowledge_part": None, "action_part": text,
                "why": "data-fetch verb in first position"}

    if is_report and not is_question:
        return {"intent": REPORT, "knowledge_part": None, "action_part": text,
                "why": "report noun without an interrogative"}

    if is_question:
        return {"intent": KNOWLEDGE, "knowledge_part": text,
                "action_part": None, "why": "interrogative form"}

    # No positive evidence — do NOT claim it for knowledge.
    return {"intent": UNKNOWN, "knowledge_part": None, "action_part": text,
            "why": "no interrogative or imperative marker"}


def is_knowledge_question(message: str) -> bool:
    return classify(message)["intent"] in (KNOWLEDGE, MIXED)
