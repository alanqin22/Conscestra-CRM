"""Bounded goal→plan orchestration (advanced improvement #4).

The reference vision's "advanced reasoning engine", built the Conscestra way:
given a NOVEL business goal in plain language, the planner drafts a multi-step
plan — but every step must be a REGISTERED A2A capability, plans are hard-
capped, reads and writes are treated completely differently, and nothing
outbound happens without a human:

    draft      LLM sees the goal + the capability manifest (intent, kind,
               description) and returns a JSON plan. Anything else — unknown
               intents, too many steps, too many writes — fails validation;
               the planner never improvises around its vocabulary.
    execute    READ steps run immediately through a2a.dispatch (structured,
               deterministic, side-effect free) and their results are
               attached to the plan.
    propose    WRITE steps are NEVER executed — each becomes a governance
               proposal (critic-reviewed, routed to the right executive,
               one-click decidable, undoable), tagged with the plan's goal
               and correlation id so the whole play is auditable end to end
               in action_approvals.

This is the same trade the rest of the platform makes: the LLM supplies
judgment, deterministic rails supply safety. A plan is a *suggestion of
registered moves*, not a new code path.

BOUNDS
  MAX_STEPS   6    a plan longer than this fails validation
  MAX_WRITES  2    at most this many write steps per plan
"""

from __future__ import annotations

import json
import logging
import re
import uuid as _uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

logger = logging.getLogger("planner")

MAX_STEPS = 6
MAX_WRITES = 2

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# ============================================================================
# CAPABILITY VOCABULARY
# ============================================================================

def _vocabulary() -> Dict[str, Dict[str, Any]]:
    """intent → {kind, description, required} for every capability.

    `required` was missing here, and production paid for it. The drafter was
    handed a capability list with no parameter contract, so it planned
    `comms.select_channel` with no party_id; validate_plan checked only that
    `params` was an object; and the omission surfaced four layers away as a
    PostgreSQL uuid error. The capability already declares what it needs --
    this makes that declaration visible to the two places that can act on it:
    the prompt that drafts the plan, and the validator that accepts it."""
    from app.core.a2a import CAPABILITIES
    return {c.intent: {"kind": c.kind, "description": c.description,
                       "required": list(c.params_schema[0])
                                   if c.params_schema else []}
            for c in CAPABILITIES.values()}


def _manifest_lines() -> str:
    """The capability list the drafter sees.

    Required parameters are named inline. Telling the model what a
    capability NEEDS is what stops it planning a call that cannot be
    executed -- validation alone would only reject the plan afterwards."""
    vocab = _vocabulary()
    out = []
    for intent, v in sorted(vocab.items()):
        req = (f" REQUIRES params: {', '.join(v['required'])}."
               if v["required"] else "")
        out.append(f"- {intent} [{v['kind'].upper()}]:{req} "
                   f"{v['description'][:140]}")
    return "\n".join(out)


# ============================================================================
# VALIDATE — the safety wall (mirrors playbook validation)
# ============================================================================

def _cap(key: str, default: int) -> int:
    """Live policy cap (governance_policies row) with the code bound as
    fallback — the bounds are tunable data, never absent."""
    try:
        from app.core import governance
        return int(governance.policy_value(key, default))
    except Exception:
        return default


def validate_plan(plan: Any) -> List[str]:
    """All the reasons this plan is unusable ([] = valid)."""
    errs: List[str] = []
    steps = (plan or {}).get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not steps:
        return ["plan.steps must be a non-empty list"]
    max_steps = _cap("planner.max_steps", MAX_STEPS)
    if len(steps) > max_steps:
        errs.append(f"{len(steps)} steps exceeds the MAX_STEPS bound ({max_steps})")
    vocab = _vocabulary()
    writes = 0
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            errs.append(f"step {i}: not an object")
            continue
        intent = s.get("intent")
        if intent not in vocab:
            errs.append(f"step {i}: unknown capability {intent!r} — the planner "
                        f"may only use registered intents")
            continue
        if not isinstance(s.get("params", {}), dict):
            errs.append(f"step {i}: params must be an object")
        # A MISSING REQUIRED PARAM IS DELIBERATELY *NOT* AN ERROR HERE.
        #
        # An earlier version of this fix made it one, and that was wrong in
        # a way worth recording: validate_plan rejects the WHOLE plan, so a
        # single under-specified step killed four good ones. This wall is
        # for problems that make a plan untrustworthy as a whole -- a
        # hallucinated intent, a write count over the safety bound. A step
        # that merely lacks an identifier is a LOCAL defect, and dropping
        # just that step (see draft_plan) is both safer and more useful.
        if vocab[intent]["kind"] == "write":
            writes += 1
    max_writes = _cap("planner.max_writes", MAX_WRITES)
    if writes > max_writes:
        errs.append(f"{writes} write steps exceeds the MAX_WRITES bound "
                    f"({max_writes}) — split the goal")
    return errs


# ============================================================================
# DRAFT — the LLM's only job
# ============================================================================

def _draft_llm(goal: str) -> Optional[Dict[str, Any]]:
    """goal → {"steps":[{intent, params, why}], "summary"} or None on failure."""
    try:
        from app.core.graph_utils import _get_llm
        llm = _get_llm()
        resp = llm.invoke([
            {"role": "system", "content":
                "You are the planning brain of Conscestra CRM's Orchestrator. "
                "You decompose a business goal into a SHORT plan of registered "
                "agent capabilities. RULES: use ONLY intents from the provided "
                "list, verbatim; at most "
                f"{_cap('planner.max_steps', MAX_STEPS)} steps and "
                f"{_cap('planner.max_writes', MAX_WRITES)} WRITE steps; put READ "
                "steps before WRITE steps; params must be simple JSON values "
                "the capability description implies. NEVER invent entity IDs, "
                "names, or placeholder values (no 'x', '<id>', example names) — "
                "if you don't have a real, concrete identifier, OMIT that param "
                "and prefer a discovery capability (a list/summary) that finds "
                "the records itself. Only include a param you can fill with a "
                "real value taken from the goal. If the goal cannot be served "
                'with these capabilities, return {"error": "<why>"}.'},
            {"role": "user", "content":
                f"GOAL: {goal}\n\nREGISTERED CAPABILITIES:\n{_manifest_lines()}\n\n"
                'Return ONLY JSON: {"summary": "<one line>", "steps": '
                '[{"intent": "<registered intent>", "params": {…}, '
                '"why": "<short reason>"}]}'},
        ])
        text = resp.content if hasattr(resp, "content") else str(resp)
        m = _JSON_RE.search(text)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:
        logger.warning(f"[planner] LLM draft failed: {exc}")
        return None


# ============================================================================
# PARAM SANITIZATION — strip invented placeholder values before they reach an
# agent. The draft LLM sometimes fills an ENTITY param with a value it doesn't
# actually know ("account": "x", "<id>", an example name); that junk then
# reaches the owning agent and errors (e.g. "Invalid UUID: x"), wasting the
# step. We drop such values so the step runs on clean input (a discovery /
# general answer) instead. Conservative by design: only OBVIOUS placeholders
# are removed — real filter values (status="open", scoreMin=70) are untouched.
# ============================================================================

_PLACEHOLDER_TOKENS = {
    "", "x", "y", "z", "xx", "xxx", "tbd", "todo", "n/a", "na", "none", "null",
    "nil", "example", "placeholder", "string", "value", "unknown", "sample",
    "<id>", "<uuid>", "<name>", "<account>", "?", "-", "--", "...",
    "account name", "the account", "company name", "entity id", "id here",
    "your account", "account_id", "entity_id", "lead_id", "contact_id",
}

_TEMPLATE_RE = re.compile(r"^\s*(?:<.*>|\{\{.*\}\}|\[.*\])\s*$")

# Capabilities that are meaningless without a concrete identifier/param. A step
# using one with none of them (after sanitization) is DROPPED, so the plan never
# dispatches work it can't fill:
#   • READS  — the owning agent would turn the empty request into a placeholder
#     id and error on it (e.g. account_balance → "Invalid UUID: x").
#   • WRITES — an under-specified write (e.g. email.send_payment_reminder with no
#     recipient) would otherwise queue a governance proposal that CANNOT execute
#     on approval, filling the queue with dead actions. For aggregate goals the
#     planner should propose a bulk/segment capability (supervisor.emit_dunning,
#     campaign.winback) that needs no per-record params — those are unaffected.
# Discovery reads (summaries/lists) and param-free writes are untouched. intent →
# the param aliases that satisfy the requirement (any ONE is enough).
# HAND-MAINTAINED, and deliberately "at least one of": these capabilities
# accept alternative identifiers (an account OR an entity OR a lead), which
# a flat required-list cannot express.
#
# IT IS NO LONGER THE ONLY SOURCE. This dict silently drifted from the
# capability declarations: comms.select_channel declares party_id required
# via params_schema and was simply never added here, so the drop mechanism
# below -- which worked correctly -- never learned the step was
# under-specified. The step was planned, dispatched, and died on a
# PostgreSQL uuid cast.
#
# `draft_plan` now applies TWO rules rather than merging them into one, and
# the difference is semantic rather than cosmetic: this dict means "at least
# one of these", because these capabilities accept alternative identifiers,
# while a declared `params_schema` means "all of these". Collapsing them into
# a single merged list would have to pick one meaning and would be wrong for
# whichever source it did not pick. A capability that declares its own schema
# is therefore covered the moment it declares, without anyone remembering
# this file — but by the second rule, not by an entry here.
_ALTERNATIVE_IDS: Dict[str, tuple] = {
    # reads
    "accounting.account_balance": ("account", "account_id", "accountId"),
    "account.context":            ("account_id", "entity_id"),
    "crm.context":                ("entity_id", "account_id", "lead_id", "contact_id"),
    "leads.enrich":               ("lead_id", "leadId", "company", "domain", "email"),
    # writes that cannot execute without their specifics
    "email.send_payment_reminder": ("to", "invoice_number", "invoice_id"),
    "sms.send":                    ("to",),
    "quote.generate":              ("account_id", "items"),
    "meeting.book":                ("entity_id", "lead_id", "contact_id"),
    "contact.update_profile":      ("contact_id",),
    "tuning.adjust":               ("param",),
    "kb.publish":                  ("title",),
    "scoring.activate":            ("version",),
}


def _is_placeholder_value(v: Any) -> bool:
    """True when a param value is an obvious invented placeholder rather than a
    real value the goal supplied. Numbers/booleans and concrete strings pass."""
    if v is None:
        return True
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return False
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _PLACEHOLDER_TOKENS or _TEMPLATE_RE.match(s):
            return True
        return len(s) == 1 and not s.isdigit()   # bare single letter like "x"
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _sanitize_params(params: Any):
    """Drop placeholder-valued params. Returns (clean_params, [stripped_keys])."""
    if not isinstance(params, dict):
        return {}, []
    clean, stripped = {}, []
    for k, val in params.items():
        (stripped.append(k) if _is_placeholder_value(val) else clean.update({k: val}))
    return clean, stripped


def draft_plan(goal: str) -> Dict[str, Any]:
    """Draft + validate — no side effects (the A2A `crm.plan` capability)."""
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "errors": ["goal is required"]}
    plan = _draft_llm(goal)
    if not plan:
        return {"ok": False, "errors": ["the planner could not draft a plan "
                                        "(LLM unavailable or unparseable)"]}
    if plan.get("error"):
        return {"ok": False, "errors": [f"planner declined: {plan['error']}"]}
    errs = validate_plan(plan)
    vocab = _vocabulary()
    steps = []
    stripped_all: Dict[str, List[str]] = {}
    dropped: List[str] = []
    for s in plan.get("steps", []):
        if not isinstance(s, dict):
            continue
        intent = s.get("intent")
        clean_params, stripped = _sanitize_params(s.get("params") or {})
        if stripped:
            stripped_all[str(intent)] = stripped
        # Two rules, each matching the semantics of its source.
        #   alternatives  -> at least one of them must be present
        #   declared schema -> ALL of them must be present
        # Blank values never reach here: _sanitize_params already strips an
        # empty string as a placeholder, so absence is the only shape.
        need = _ALTERNATIVE_IDS.get(intent)
        if need and not any(k in clean_params for k in need):
            dropped.append(str(intent))
            continue
        declared = vocab.get(intent, {}).get("required") or []
        if declared and not all(k in clean_params for k in declared):
            dropped.append(str(intent))
            continue
        steps.append({"intent": intent,
                      "kind": vocab.get(intent, {}).get("kind"),
                      "params": clean_params,
                      "why": str(s.get("why") or "")[:200]})
    if stripped_all:
        logger.info(f"[planner] stripped placeholder params: {stripped_all}")
    if dropped:
        logger.info(f"[planner] dropped under-specified steps: {dropped}")
    return {"ok": not errs, "goal": goal,
            "summary": str(plan.get("summary") or "")[:300],
            "steps": steps, "errors": errs or None}


# ============================================================================
# EXECUTE — reads run, writes queue
# ============================================================================

async def run_plan(goal: str, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Draft (unless a validated plan is supplied), execute the READ steps,
    and PROPOSE every WRITE step through governance. Returns the full trace."""
    from app.core import governance
    from app.core.a2a import A2ARequest, dispatch

    drafted = plan or draft_plan(goal)
    if not drafted.get("ok"):
        return drafted
    if plan is not None:
        errs = validate_plan({"steps": drafted.get("steps")})
        if errs:
            return {"ok": False, "goal": goal, "errors": errs}

    cid = str(_uuid.uuid4())
    trace: List[Dict[str, Any]] = []
    proposed: List[Dict[str, Any]] = []

    # READ steps are independent (v1 params never reference prior outputs) —
    # fan them out CONCURRENTLY; WRITE steps queue sequentially after.
    import asyncio as _aio

    async def _read(i: int, intent: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from app.core.a2a import Principal
            res = await dispatch(A2ARequest(
                intent=intent, from_agent="planner", params=params,
                # Reads do not require a principal — the write gate does. Named
                # here anyway so the trace can attribute a plan's read fan-out,
                # which is otherwise a burst of dispatches with no initiator.
                principal=Principal.service("planner"),
                correlation_id=cid))
            return {"step": i, "intent": intent, "kind": "read", "ok": res.ok,
                    "output": (res.output or "")[:500], "error": res.error}
        except Exception as exc:
            return {"step": i, "intent": intent, "kind": "read",
                    "ok": False, "error": str(exc)[:200]}

    reads = [(i, s) for i, s in enumerate(drafted["steps"], 1)
             if s.get("kind") == "read"]
    read_results = await _aio.gather(
        *[_read(i, s["intent"], dict(s.get("params") or {})) for i, s in reads])
    trace.extend(read_results)

    for i, step in enumerate(drafted["steps"], 1):
        if step.get("kind") == "read":
            continue
        # WRITE — never executed by the planner: queue for human approval.
        intent, params = step["intent"], dict(step.get("params") or {})
        params["plan_goal"] = goal
        params["plan_correlation_id"] = cid
        aid = governance.propose(intent, "planner", params,
                                 confidence=0.55, severity="medium")
        proposed.append({"step": i, "intent": intent, "approval_uuid": aid,
                         "why": step.get("why")})
        trace.append({"step": i, "intent": intent, "kind": "write",
                      "ok": True, "queued_approval": aid})
    trace.sort(key=lambda t: t["step"])

    logger.info(f"[planner] goal={goal[:80]!r} cid={cid[:8]} "
                f"steps={len(trace)} proposed={len(proposed)}")
    return {"ok": True, "goal": goal, "summary": drafted.get("summary"),
            "correlation_id": cid, "trace": trace,
            "proposed_approvals": proposed,
            "note": ("write steps are queued in the governance approval queue "
                     "(critic-reviewed) — nothing outbound has happened"
                     if proposed else "read-only plan — fully executed")}


# ============================================================================
# Admin endpoint
# ============================================================================

router = APIRouter(tags=["planner"])


@router.post("/planner/plan")
async def planner_plan(body: Dict[str, Any]):
    """Draft a plan for a goal; execute=true also runs it (reads execute,
    writes queue for approval). Default is a side-effect-free preview."""
    goal = str((body or {}).get("goal") or "")
    if not (body or {}).get("execute"):
        import asyncio
        return await asyncio.to_thread(draft_plan, goal)
    return await run_plan(goal)
