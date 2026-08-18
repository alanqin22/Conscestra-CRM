"""Grounded answering for CRM-user informational questions.

CONTRACT
    message (already classified KNOWLEDGE or MIXED)
        -> premise firewall          (correct a false assumption first)
        -> knowledge.retrieve()      (hybrid, with the answerability gate)
        -> grounded answer from the retrieved articles ONLY
        -> or refuse + log a gap

THE ANTI-REGRESSION RULE
On a retrieval miss this returns a REFUSAL. It must never hand the question
back to the module agent, because that is precisely the hole Phase 3 exposed:

    KB found nothing -> executive agent -> answered from model knowledge
                                        -> "the account clean-up job runs
                                            nightly at 02:30 AM"

Falling through converts an honest "we don't have that documented" into a
confident fabrication. A refusal is recoverable — it logs a gap, and the gap
list is what tells someone which article to write. A fabrication is not
recoverable, because nobody knows it happened.

WHY THE ANSWER IS GENERATED, NOT PASTED
Articles are written for a question, not for THIS question, so pasting the
top article answers a neighbouring question with full confidence — the Phase 2
false-coverage failure. The model is given the retrieved articles and told it
may use nothing else; that is grounding, not generation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_GROUND_SYSTEM = (
    "You answer questions about the Conscestra CRM product for its OWN USERS "
    "(staff), using ONLY the approved knowledge supplied below.\n"
    "RULES:\n"
    "1. Use only the supplied knowledge. If it does not contain the answer, "
    "say so plainly — never fill the gap from general CRM experience.\n"
    "2. A capability existing does NOT mean it runs automatically, on a "
    "schedule, or that it happened in any particular case. Never state a "
    "frequency, run time, schedule or history that the knowledge does not "
    "establish.\n"
    "3. Answer for the RECORD TYPE the user named. If the knowledge covers a "
    "different record type, say the asked-about type is not covered rather "
    "than substituting.\n"
    "4. You are explaining, not doing. Never claim to have performed an "
    "action.\n"
    "5. Be direct and brief — 2-5 sentences. Lead with the answer."
)


def _refusal(topic: str) -> str:
    return (
        f"I don't have approved knowledge covering {topic}, so I'd rather say "
        f"that than guess — an invented answer about how the product behaves is "
        f"worse than none. I've logged this as a knowledge gap so it can be "
        f"documented. If you need it now, ask a colleague who administers "
        f"Conscestra, or rephrase in case it's written up differently."
    )


def answer(message: str, audience: str = "public") -> Optional[Dict[str, Any]]:
    """Grounded answer, or a refusal. None only if knowledge routing is off."""
    from app.core import knowledge, premise_firewall

    # 1. Premise first. A false assumption must be corrected before any
    #    retrieval, or the retrieved article becomes evidence FOR it.
    pf = premise_firewall.check(message)
    if pf:
        return {"mode": f"premise_correction:{pf['rule']}",
                "output": premise_firewall.as_answer(pf),
                "grounded": True, "source": "capability_truth",
                "articles": [], "gap_logged": False}

    # 2. Retrieve. `retrieve()` applies the answerability gate, so a weak match
    #    returns [] rather than reaching the model as approved knowledge.
    try:
        hits: List[Dict[str, Any]] = knowledge.retrieve(
            message, "", audience=audience, top=3) or []
    except Exception as exc:                                # pragma: no cover
        logger.warning(f"[knowledge_route] retrieval failed: {exc}")
        hits = []

    if not hits:
        try:
            knowledge.log_gap("crm_user", message)
        except Exception as exc:                            # pragma: no cover
            logger.warning(f"[knowledge_route] gap log failed: {exc}")
        return {"mode": "knowledge_refusal", "output": _refusal("that"),
                "grounded": True, "source": "refusal",
                "articles": [], "gap_logged": True}

    # 3. Ground the answer in what came back — and nothing else.
    block = "\n\n".join(
        f"Q: {h.get('title','')}\nA: {str(h.get('answer',''))[:700]}"
        for h in hits)
    try:
        from app.core.graph_utils import _get_llm
        resp = _get_llm().invoke([
            {"role": "system", "content": _GROUND_SYSTEM},
            {"role": "user",
             "content": f"APPROVED KNOWLEDGE:\n{block}\n\nQUESTION: {message}"},
        ])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
    except Exception as exc:                                # pragma: no cover
        logger.warning(f"[knowledge_route] generation failed: {exc}")
        # Degrade to the article itself rather than to the module agent: a
        # slightly off-target approved answer still beats an ungrounded one.
        text = str(hits[0].get("answer") or "")

    if not text:
        return {"mode": "knowledge_refusal", "output": _refusal("that"),
                "grounded": True, "source": "refusal", "articles": [],
                "gap_logged": False}

    return {"mode": "knowledge_answer", "output": text, "grounded": True,
            "source": "kb", "articles": [h.get("title") for h in hits],
            "gap_logged": False}
