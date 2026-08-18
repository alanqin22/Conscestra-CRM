"""Stage 3 cutover: structured intent becomes authoritative for enabled modules.

WHAT CHANGES
For a module named in STRUCTURED_INTENT, a resolved StructuredIntent is carried
to the module in `chatInput.structuredIntent` and consumed by its pre-router
BEFORE any heuristic runs. The noun-phrase rules and the module LLM never see
the request, so neither can substitute a different operation.

WHAT DOES NOT CHANGE
Everything downstream. The intent becomes the same `parsed_json` the pre-router
would have produced, and then flows through the module's existing
sql_builder -> write_guard -> execute_sp -> formatter path. No second execution
route, and — the part that matters — no second authorization surface. A cutover
that reached the database by a new path would have to re-earn every permission
check that path skipped; this one inherits them because it joins the pipeline at
the same seam the old router did.

WHY A FLAG PER MODULE
STRUCTURED_INTENT is a comma-separated allowlist, not a boolean. Stage 3 ships
contacts, accounts and leads; the other ten modules keep today's behaviour
untouched. Rollback is removing a name from the variable — no redeploy, no
schema change, no data migration.

FAILURE MODES ARE EXPLICIT, NEVER SUBSTITUTIONS
    MissingTarget        -> ask which records; do not run a discovery mode and
                            present it as the answer
    UnsupportedOperation -> say the object does not support it; do not redirect
                            to an object that does
Both are the whole point. Phase 6 measured "Merge these duplicate contacts."
returning a duplicates report, and Phase 8 put operation preservation at 30%.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ENV = "STRUCTURED_INTENT"

# Orchestrator route -> the object the resolver should assume when the message
# names none. "Archive it" carries no record type; the route already decided.
ROUTE_OBJECT = {
    "/contact-chat": "contact", "/account-chat": "account",
    "/lead-chat": "lead", "/opportunity-chat": "opportunity",
    "/order-chat": "order", "/accounting-chat": "invoice",
    "/prod-chat": "product",
}

# object -> the parameter name each module's sql_builder expects for an id.
# The resolver emits a neutral `recordId`; this renames it at the boundary so
# the module's existing validation applies unchanged.
_ID_PARAM = {
    "contact": "contactId", "account": "accountId", "lead": "leadId",
    "opportunity": "opportunityId", "order": "orderId", "invoice": "invoiceId",
    "product": "productId",
}


def _env_value() -> str:
    """Read the flag, falling back to the project .env file.

    os.getenv alone was not enough: the running server could not see the
    variable even though config.py calls load_dotenv() at import, because the
    process had already imported config before the flag was appended and
    nothing re-read the file. The failure mode is the dangerous one — the
    cutover simply never engages, every request keeps working, and the only
    symptom is that the fix appears to do nothing.
    So: environment first (deployment wins), project .env second.
    """
    raw = os.getenv(_ENV)
    if raw:
        return raw
    try:
        from pathlib import Path
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{_ENV}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as exc:                                # pragma: no cover
        logger.debug(f"[cutover] .env read failed: {exc}")
    return ""


def enabled_modules() -> set:
    raw = _env_value()
    return {m.strip().lower() for m in raw.split(",") if m.strip()}


def is_enabled(obj: str) -> bool:
    """Accepts either spelling. The flag reads naturally as module names
    ("contacts,accounts,leads") while objects are singular ("contact"), and a
    mismatch there fails CLOSED — the cutover silently never engages, which is
    the hardest kind of flag bug to notice because everything keeps working."""
    if not obj:
        return False
    mods = enabled_modules()
    if "all" in mods:
        return True
    o = obj.lower()
    return o in mods or f"{o}s" in mods or o.rstrip("s") in mods


def to_params(intent) -> Dict[str, Any]:
    """StructuredIntent -> the params dict the module's sql_builder validates."""
    params: Dict[str, Any] = {"mode": intent.operation}
    for key, value in (intent.parameters or {}).items():
        if key == "recordId":
            params[_ID_PARAM.get(intent.object, "recordId")] = value
        else:
            params[key] = value
    return params


def resolve_for_route(message: str, path: str) -> Optional[Dict[str, Any]]:
    """Resolve an intent for a route, or return an explicit refusal payload.

    Returns one of:
        {"kind": "intent",  "params": {...}, "object":…, "operation":…}
        {"kind": "ask",     "output": "…"}     — target missing
        {"kind": "refuse",  "output": "…"}     — object does not support it
        None                                    — not ours; route as today
    """
    from app.core.operation_resolver import (resolve_traced,
                                             OUTCOME_BYPASSED,
                                             OUTCOME_MATCHED,
                                             OUTCOME_MISSING_TARGET,
                                             OUTCOME_UNSUPPORTED)
    obj_hint = ROUTE_OBJECT.get(path)
    if not is_enabled(obj_hint or ""):
        return {"kind": "bypassed", "outcome": OUTCOME_BYPASSED,
                "object": obj_hint, "operation": None}
    payload, outcome = resolve_traced(message, object_hint=obj_hint)

    if outcome == OUTCOME_MISSING_TARGET:
        logger.info(f"[cutover] {payload.operation} on {payload.object}: no "
                    f"target -> ask")
        return {"kind": "ask", "outcome": outcome, "object": payload.object,
                "operation": payload.operation,
                "output": (
                    f"I can {payload.operation} {payload.object}s, but I need "
                    f"to know which — {payload.needs}. Nothing has been "
                    f"changed. Tell me the record and I'll run it.")}

    if outcome == OUTCOME_UNSUPPORTED:
        supported = ", ".join(f"{s}s" for s in (payload.supported or [])) \
            or "no record type"
        logger.info(f"[cutover] {payload.object} does not support "
                    f"{payload.operation}")
        return {"kind": "refuse", "outcome": outcome, "object": payload.object,
                "operation": payload.operation,
                "output": (
                    f"Conscestra does not support {payload.operation} for "
                    f"{payload.object}s — it is available for {supported}. I "
                    f"have not applied it to any other record type on your "
                    f"behalf.")}

    intent = payload if outcome == OUTCOME_MATCHED else None
    if intent is None:
        # Declined — and now it says WHY. This is the distinction whose absence
        # made the Stage 3 attribution wrong.
        return {"kind": "declined", "outcome": outcome,
                "object": obj_hint, "operation": None}
    logger.info(f"[cutover] AUTHORITATIVE {intent.object}.{intent.operation} "
                f"params={list(intent.parameters)}")
    return {"kind": "intent", "outcome": outcome, "object": intent.object,
            "operation": intent.operation, "params": to_params(intent)}
