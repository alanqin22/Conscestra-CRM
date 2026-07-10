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
    """intent → {kind, description} for every registered capability."""
    from app.core.a2a import CAPABILITIES
    return {c.intent: {"kind": c.kind, "description": c.description}
            for c in CAPABILITIES.values()}


def _manifest_lines() -> str:
    vocab = _vocabulary()
    return "\n".join(f"- {intent} [{v['kind'].upper()}]: {v['description'][:140]}"
                     for intent, v in sorted(vocab.items()))


# ============================================================================
# VALIDATE — the safety wall (mirrors playbook validation)
# ============================================================================

def validate_plan(plan: Any) -> List[str]:
    """All the reasons this plan is unusable ([] = valid)."""
    errs: List[str] = []
    steps = (plan or {}).get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not steps:
        return ["plan.steps must be a non-empty list"]
    if len(steps) > MAX_STEPS:
        errs.append(f"{len(steps)} steps exceeds the MAX_STEPS bound ({MAX_STEPS})")
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
        if vocab[intent]["kind"] == "write":
            writes += 1
    if writes > MAX_WRITES:
        errs.append(f"{writes} write steps exceeds the MAX_WRITES bound "
                    f"({MAX_WRITES}) — split the goal")
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
                f"{MAX_STEPS} steps and {MAX_WRITES} WRITE steps; put READ "
                "steps before WRITE steps; params must be simple JSON values "
                "the capability description implies; if the goal cannot be "
                'served with these capabilities, return {"error": "<why>"}.'},
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
    for s in plan.get("steps", []):
        if isinstance(s, dict):
            steps.append({"intent": s.get("intent"),
                          "kind": vocab.get(s.get("intent"), {}).get("kind"),
                          "params": s.get("params") or {},
                          "why": str(s.get("why") or "")[:200]})
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
            res = await dispatch(A2ARequest(
                intent=intent, from_agent="planner", params=params,
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
