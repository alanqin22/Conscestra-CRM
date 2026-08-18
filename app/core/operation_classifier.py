"""LLM operation classifier — an experiment, behind a flag, measured side by side.

WHY THIS EXISTS AFTER FIVE ROUNDS OF ARGUING AGAINST MODELS HERE
The deterministic resolver was the right call for SAFETY and has been
vindicated: zero unsafe outcomes across every independently authored set built
to break it. It is the wrong tool for BREADTH. Recall plateaus in the thirties
because the failures stopped being defects and became the open-endedness of
language — "needs merging", "would you mind archiving", "roll those together",
"take it out of circulation". Each round enumerates more forms; the next
independent author writes forms nobody enumerated. A pattern list cannot
enumerate every way to ask, for the same reason a stop-list could not enumerate
every word that is not a name.

WHY THIS IS NOT THE PHASE 6 DEFECT RETURNING
Phase 6's failure was an UNCONSTRAINED model reading prose and choosing a mode —
"I think this sounds like duplicates". Every property that made that dangerous
is removed here, and none of them depends on the model behaving:

  * The guards run BEFORE this. Negation, capability questions, past-tense
    description, reads and ambiguity never reach the model at all.
  * The model may only emit a mode from SP_MODES[object]. StructuredIntent's
    __post_init__ raises on anything else, so an invented operation is
    unrepresentable rather than unlikely.
  * kb_capability_truth still refuses unsupported pairings.
  * A target the model did not find raises MissingTarget. It is never allowed
    to invent one — the failure that killed the case-insensitive name matcher.
  * resolverOutcome telemetry is unchanged, so attribution stays honest.

The model is therefore doing exactly one job: mapping prose to a member of a
closed set, or declining. It has no vocabulary for substitution, no ability to
name an operation that does not exist, and no path to a write the guards did
not already permit.

FAILURE IS A DECLINE, NEVER A GUESS
Any error — timeout, unparseable reply, a mode outside the enum — returns
no_operation. The deterministic resolver remains the fallback, so the worst
case is today's behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ENABLED = os.getenv("OPERATION_CLASSIFIER", "0").strip().lower() in (
    "1", "true", "yes", "on")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _prompt(objects_and_modes: str) -> str:
    return (
        "You map a CRM user's message to ONE operation from a fixed list, or "
        "decline.\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"object": "<contact|account|lead>", "operation": "<exact mode from '
        'the list>", "target": "<the id, email, phone or full record name the '
        'user named, or null>"}\n'
        'Decline with {"object": null, "operation": null, "target": null}.\n\n'
        "RULES\n"
        "1. `operation` MUST be copied exactly from the list for that object. "
        "Never invent one, never approximate. If what the user wants is not "
        "on the list, decline.\n"
        "2. Use the record type the USER named. If they say account, do not "
        "answer with lead, even when only leads support the operation — "
        "decline instead and let the caller refuse.\n"
        "3. `target` is only what the user actually wrote. If they said "
        '"these", "them", "it" or named nobody, target is null. Never infer '
        "who they meant. A null target is correct and useful.\n"
        "4. Decline for anything that is not a request to perform an "
        "operation: questions about what is possible, descriptions of what "
        "already happened, requests to show or list, and anything negated.\n"
        "5. Do not explain. JSON only.\n\n"
        f"AVAILABLE OPERATIONS\n{objects_and_modes}"
    )


def _catalogue() -> str:
    from app.core.operation_resolver import SP_MODES, FORM_OWNED
    lines = []
    for obj in ("contact", "account", "lead"):
        modes = sorted(m for m in SP_MODES.get(obj, set())
                       if m not in FORM_OWNED)
        lines.append(f"  {obj}: {', '.join(modes)}")
    return "\n".join(lines)


def classify(message: str, object_hint: Optional[str] = None
             ) -> Tuple[Optional[Dict[str, Any]], str]:
    """({object, operation, target}, reason) or (None, reason).

    Never raises. `reason` is for telemetry, not for control flow.
    """
    if not ENABLED:
        return None, "disabled"
    try:
        from app.core.graph_utils import _get_llm
        resp = _get_llm().invoke([
            {"role": "system", "content": _prompt(_catalogue())},
            {"role": "user", "content": message},
        ])
        text = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as exc:                                # pragma: no cover
        logger.warning(f"[classifier] call failed: {exc}")
        return None, "error"

    m = _JSON_RE.search(str(text))
    if not m:
        return None, "unparseable"
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None, "unparseable"

    obj = (data.get("object") or object_hint or "").strip().lower() or None
    op = (data.get("operation") or "").strip().lower() or None
    target = data.get("target")
    if not obj or not op:
        return None, "declined"

    # The enum is enforced here as well as in StructuredIntent. Checking twice
    # is deliberate: this is the boundary where a model's output becomes a
    # database operation, and it should be impossible to widen that by editing
    # one file.
    from app.core.operation_resolver import SP_MODES, FORM_OWNED
    if op in FORM_OWNED:
        return None, "form_owned"
    if op not in SP_MODES.get(obj, set()):
        logger.info(f"[classifier] rejected out-of-enum {obj}.{op}")
        return None, "out_of_enum"

    return {"object": obj, "operation": op,
            "target": str(target).strip() if target else None}, "matched"
