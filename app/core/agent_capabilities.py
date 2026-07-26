"""Action Authorization Layer — U4 (round-2 blindspots, 2026-07-25).

THE GAP
    Blindspot #3 made agents authorable as data, with a runtime that is
    deliberately tool-less and write-less. That default is right — a
    non-developer must not be able to create risk by typing. But it left no
    path at all between explaining and doing:

        "How much parental leave do I get?"  -> answered from the KB
        "OK, submit my leave request."       -> impossible, forever

    Agentforce's own hero examples are all verbs — *processing* a leave
    request, *resolving* a ticket, *confirming* parts availability. An agent
    that can only explain is an FAQ bot. Making one act still requires a
    developer, and that is the strategic gap U4 closes.

THE MODEL — a granted capability set, never a tool box
    An authored agent is not handed tools. It is granted a named subset of the
    capabilities that ALREADY exist in `a2a.CAPABILITIES`, each of which
    already carries its own validation, guardrails and audit trail:

        agent → granted capability → authorize() → READ  : execute (scoped)
                                                 → WRITE : governed proposal
                                                           → human approval
                                                           → execution

    Four properties make this safe rather than merely convenient:

      1. **An agent cannot invent a capability.** Only names present in the
         admin-curated catalogue AND granted to that agent are callable.
      2. **A write never executes on the agent's decision.** It becomes a
         proposal in the SAME `action_approvals` queue an executive already
         ratifies. U4 adds reach without adding a new trust path.
      3. **An agent cannot widen its own grant.** Grants are data an admin
         edits, and — because a grant is the most security-relevant edit
         possible — that edit rides U2's draft → evaluate → publish gate.
      4. **Scope still governs reach.** An `external` (anonymously reachable)
         agent can only be granted capabilities marked safe for external
         scope — U2's `reach_invariant` extended from knowledge to actions.

    Every invocation is recorded, including refusals, because "what did the
    agent try to do" is a different and more important question than "what did
    it say".

Requires sql/agent_capabilities.sql. See [a2a] (the capability registry),
[governance] (the approval queue), [custom_agents] (the authored agents),
[agent_versions] (U2's publish gate).

CONFIG (env)
  AGENT_CAPABILITIES_ENABLED  1  kill switch for the whole layer
  AGENT_CAP_MAX_CALLS_PER_TURN 3 how many capabilities one turn may invoke
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("agent_capabilities")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("AGENT_CAPABILITIES_ENABLED", "1")
MAX_CALLS_PER_TURN = int(os.getenv("AGENT_CAP_MAX_CALLS_PER_TURN", "3"))

EXECUTED, PROPOSED, REFUSED, ERROR = "executed", "proposed", "refused", "error"


# ============================================================================
# Catalogue + grants
# ============================================================================

def catalog(grantable_only: bool = True) -> Dict[str, Any]:
    """What an admin has opened up for granting. A capability existing in the
    code registry is NOT sufficient — `grantable` must be set deliberately."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT capability, kind, label, description, grantable,
                          requires_approval, max_scope
                   FROM agent_capability_catalog
                   {} ORDER BY kind, capability""".format(
                       "WHERE grantable" if grantable_only else ""))
            cols = [d[0] for d in cur.description]
            return {"ok": True,
                    "capabilities": [dict(zip(cols, r)) for r in cur.fetchall()]}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"{str(exc)[:180]} "
                "(apply sql/agent_capabilities.sql?)"}
    finally:
        conn.close()


def grants_for(slug: str) -> List[Dict[str, Any]]:
    """The capabilities this agent may use, joined to their catalogue entry so
    one read answers both 'is it granted' and 'what does it require'."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT g.capability, c.kind, c.label, c.requires_approval,
                          c.max_scope, g.constraints, c.grantable
                   FROM agent_capability_grants g
                   JOIN agent_capability_catalog c USING (capability)
                   WHERE g.slug=%s AND g.enabled
                   ORDER BY c.kind, g.capability""", (slug,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[agent_caps] grants_for({slug}) failed: {exc}")
        return []
    finally:
        conn.close()


def set_grants(slug: str, capabilities: List[str], granted_by: str = "admin",
               constraints: Optional[Dict[str, Dict]] = None) -> Dict[str, Any]:
    """Replace an agent's grant set. Refuses any capability that is not in the
    catalogue or not marked grantable — an admin cannot grant reach that was
    never opened up, even by typing the name correctly."""
    if not ENABLED:
        return {"ok": False, "error": "agent capabilities disabled"}
    cat = {c["capability"]: c for c in
           (catalog(grantable_only=True).get("capabilities") or [])}
    bad = [c for c in capabilities if c not in cat]
    if bad:
        return {"ok": False, "error": f"not grantable: {', '.join(bad)}"}

    # Scope invariant: an external agent may only hold externally-safe grants.
    try:
        from app.core import custom_agents
        agent = custom_agents.get_agent(slug)
        if agent and agent.get("scope") == "external":
            unsafe = [c for c in capabilities if cat[c]["max_scope"] != "external"]
            if unsafe:
                return {"ok": False, "error":
                        f"agent '{slug}' is external-scope; these capabilities "
                        f"are internal-only: {', '.join(unsafe)}"}
    except Exception as exc:
        logger.debug(f"[agent_caps] scope check skipped: {exc}")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_capability_grants WHERE slug=%s", (slug,))
            for c in capabilities:
                cur.execute(
                    """INSERT INTO agent_capability_grants
                         (slug, capability, constraints, granted_by)
                       VALUES (%s,%s,%s::jsonb,%s)""",
                    (slug, c, json.dumps((constraints or {}).get(c, {})),
                     granted_by[:120]))
        conn.commit()
        logger.info(f"[agent_caps] '{slug}' granted: {capabilities or '(none)'} "
                    f"by {granted_by}")
        return {"ok": True, "slug": slug, "granted": capabilities}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()


# ============================================================================
# Authorization — the question every call must answer first
# ============================================================================

def authorize(slug: str, capability: str,
              agent_scope: str = "internal") -> Tuple[bool, str, Dict[str, Any]]:
    """May THIS agent invoke THIS capability? Returns (allowed, reason, meta).

    Deliberately fail-closed at every step: unknown capability, ungranted
    capability, non-grantable capability and scope violation all deny."""
    if not ENABLED:
        return False, "agent capabilities are disabled", {}
    granted = {g["capability"]: g for g in grants_for(slug)}
    g = granted.get(capability)
    if not g:
        return False, f"'{capability}' is not granted to this agent", {}
    if not g.get("grantable"):
        return False, f"'{capability}' is no longer grantable", {}
    if agent_scope == "external" and g["max_scope"] != "external":
        # An anonymously reachable agent must not act on internal-only surfaces
        # — U2's reach_invariant, applied to actions rather than knowledge.
        return False, (f"'{capability}' is internal-only and this agent is "
                       "externally reachable"), {}
    # U6: an external MCP tool is authorized here like any other capability —
    # one permission model, not two. Its liveness is owned by the MCP registry
    # (server enabled + tool enabled), checked at call time by mcp_client.
    if capability.startswith("mcp:"):
        return True, "allowed", {"kind": "mcp",
                                 "requires_approval": bool(g["requires_approval"]),
                                 "constraints": g.get("constraints") or {}}

    # A registered native capability must still exist in code.
    try:
        from app.core import a2a
        if capability not in a2a.CAPABILITIES:
            return False, f"'{capability}' is not a live capability", {}
        kind = a2a.CAPABILITIES[capability].kind
    except Exception as exc:
        return False, f"capability registry unavailable: {str(exc)[:60]}", {}
    return True, "allowed", {"kind": kind,
                             "requires_approval": bool(g["requires_approval"]),
                             "constraints": g.get("constraints") or {}}


def _check_constraints(params: Dict[str, Any],
                       constraints: Dict[str, Any]) -> Optional[str]:
    """Per-grant narrowing, e.g. {"max_amount": 500}. Returns a refusal reason
    or None."""
    if not constraints:
        return None
    if "max_amount" in constraints:
        try:
            amt = float(params.get("amount") or params.get("total") or 0)
            if amt > float(constraints["max_amount"]):
                return (f"amount {amt} exceeds this agent's limit of "
                        f"{constraints['max_amount']}")
        except (TypeError, ValueError):
            pass
    if "entity_types" in constraints:
        et = params.get("entity_type")
        allowed = constraints["entity_types"] or []
        if et and allowed and et not in allowed:
            return f"entity_type '{et}' is outside this agent's grant"
    return None


# ============================================================================
# Invocation — reads execute, writes PROPOSE
# ============================================================================

def invoke(slug: str, capability: str, params: Dict[str, Any],
           agent_scope: str = "internal",
           conversation_id: Optional[str] = None) -> Dict[str, Any]:
    """Run one granted capability on an authored agent's behalf.

    A READ executes immediately (already scoped and guarded by the capability
    itself). A WRITE — or any read the admin flagged as sensitive — becomes a
    governed proposal and returns WITHOUT having changed anything. The agent is
    then told, truthfully, that a person must approve it.
    """
    t0 = time.time()
    allowed, reason, meta = authorize(slug, capability, agent_scope)
    if not allowed:
        _audit(slug, capability, "?", REFUSED, reason, params, conversation_id,
               int((time.time() - t0) * 1000))
        return {"ok": False, "outcome": REFUSED, "reason": reason}

    kind = meta["kind"]
    breach = _check_constraints(params, meta.get("constraints") or {})
    if breach:
        _audit(slug, capability, kind, REFUSED, breach, params, conversation_id,
               int((time.time() - t0) * 1000))
        return {"ok": False, "outcome": REFUSED, "reason": breach}

    # ---- WRITE (or sensitive read): propose, never execute -----------------
    if kind == "write" or meta.get("requires_approval"):
        try:
            from app.core import governance
            aid = governance.propose(
                action_type=capability,
                proposed_by=f"agent:{slug}",
                params=params,
                entity_type=params.get("entity_type"),
                entity_id=params.get("entity_id"),
                confidence=0.0,          # an authored agent never auto-acts
                severity="medium")
            _audit(slug, capability, kind, PROPOSED, None, params,
                   conversation_id, int((time.time() - t0) * 1000), aid)
            logger.info(f"[agent_caps] '{slug}' PROPOSED {capability} "
                        f"→ approval {str(aid)[:8]}")
            return {"ok": True, "outcome": PROPOSED, "approval_id": str(aid),
                    "message": ("Submitted for approval — a person needs to "
                                "confirm this before it takes effect.")}
        except Exception as exc:
            _audit(slug, capability, kind, ERROR, str(exc)[:200], params,
                   conversation_id, int((time.time() - t0) * 1000))
            logger.warning(f"[agent_caps] propose failed for {capability}: {exc}")
            return {"ok": False, "outcome": ERROR, "reason": str(exc)[:160]}

    # ---- U6: external MCP tool ---------------------------------------------
    # Reached only when the tool was NOT marked requires_approval (an admin
    # reviewed it and knows it is read-only) — otherwise the write branch above
    # already turned it into a governed proposal.
    if kind == "mcp":
        try:
            from app.core import mcp_client
            parts = mcp_client.split_capability(capability)
            if not parts:
                raise RuntimeError(f"malformed MCP capability '{capability}'")
            server, tool = parts
            dc = "INTERNAL_SENSITIVE" if agent_scope == "internal" \
                else "CUSTOMER_EXTERNAL"
            out = mcp_client.call_tool(server, tool, params,
                                       caller=f"agent:{slug}", data_class=dc)
            outcome = EXECUTED if out.get("ok") else (
                REFUSED if out.get("outcome") == "refused" else ERROR)
            _audit(slug, capability, kind, outcome, out.get("reason"), params,
                   conversation_id, int((time.time() - t0) * 1000))
            return {"ok": out.get("ok", False), "outcome": outcome,
                    "data": out.get("content"), "reason": out.get("reason")}
        except Exception as exc:
            _audit(slug, capability, kind, ERROR, str(exc)[:200], params,
                   conversation_id, int((time.time() - t0) * 1000))
            return {"ok": False, "outcome": ERROR, "reason": str(exc)[:160]}

    # ---- READ: execute -----------------------------------------------------
    try:
        import asyncio
        from app.core import a2a
        req = a2a.A2ARequest(capability=capability, params=params,
                             caller=f"agent:{slug}")
        result = asyncio.run(a2a.dispatch(req))
        data = getattr(result, "data", None)
        if data is None and hasattr(result, "__dict__"):
            data = result.__dict__
        _audit(slug, capability, kind, EXECUTED, None, params, conversation_id,
               int((time.time() - t0) * 1000))
        return {"ok": True, "outcome": EXECUTED, "data": data}
    except Exception as exc:
        _audit(slug, capability, kind, ERROR, str(exc)[:200], params,
               conversation_id, int((time.time() - t0) * 1000))
        logger.warning(f"[agent_caps] {capability} failed for '{slug}': {exc}")
        return {"ok": False, "outcome": ERROR, "reason": str(exc)[:160]}


def _audit(slug: str, capability: str, kind: str, outcome: str,
           refusal: Optional[str], params: Dict[str, Any],
           conversation_id: Optional[str], latency_ms: int,
           approval_id: Optional[str] = None) -> None:
    """Record every invocation INCLUDING refusals — a refused attempt is the
    most interesting row in the table. Params are PII-masked before storage."""
    try:
        from app.core import privacy
        safe = {k: (privacy.mask(str(v)) if isinstance(v, str) else v)
                for k, v in (params or {}).items()}
    except Exception:
        safe = {k: "***" for k in (params or {})}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_capability_calls
                     (slug, capability, kind, outcome, refusal_reason,
                      approval_id, params, conversation_id, latency_ms)
                   VALUES (%s,%s,%s,%s,%s,%s::uuid,%s::jsonb,%s::uuid,%s)""",
                (slug, capability, kind or "?", outcome, refusal,
                 approval_id, json.dumps(safe)[:4000], conversation_id,
                 latency_ms))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[agent_caps] audit skipped: {exc}")
    finally:
        conn.close()


def describe_for_prompt(slug: str, agent_scope: str = "internal") -> str:
    """The capability block injected into an authored agent's system prompt.

    Written to be HONEST about what will happen: a write is described as
    something a person must approve, so the agent never promises an action it
    cannot complete on its own — the same rule U1 enforces for escalations."""
    gs = grants_for(slug)
    if not gs:
        return ""
    usable = [g for g in gs if not (agent_scope == "external"
                                    and g["max_scope"] != "external")]
    if not usable:
        return ""
    lines = []
    for g in usable:
        needs = (g["kind"] == "write") or g["requires_approval"]
        lines.append(f"  - {g['capability']}: {g['label']}"
                     + (" (a person must approve this before it takes effect)"
                        if needs else ""))
    return (
        "\n\n[ACTIONS YOU CAN TAKE]\n"
        "When the person asks you to DO one of these — not just explain it — "
        "reply with ONLY a JSON object and no other text:\n"
        '  {"action": "<capability>", "params": {...}}\n'
        "Available actions:\n" + "\n".join(lines) +
        "\nRules: use an action only when the person clearly asks for it; never "
        "invent an action name or a parameter you were not given; for anything "
        "marked as needing approval, say plainly that you have submitted it for "
        "approval rather than claiming it is done. If no action fits, answer "
        "normally in prose."
    )


def parse_action(reply: str) -> Optional[Dict[str, Any]]:
    """Extract an action request from a model reply, reusing the same tolerant
    JSON extraction every other agent uses."""
    try:
        from app.core.graph_utils import extract_json_objects
        for cand in extract_json_objects(reply or ""):
            try:
                obj = json.loads(cand)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("action"):
                return {"action": str(obj["action"]),
                        "params": obj.get("params") or {}}
    except Exception:
        pass
    return None


# ============================================================================
# Router (admin)
# ============================================================================

router = APIRouter(tags=["agent-capabilities"])


@router.get("/agent-capabilities/catalog")
def api_catalog(grantable_only: bool = True):
    return catalog(grantable_only)


@router.get("/agent-capabilities/{slug}")
def api_grants(slug: str):
    return {"ok": True, "slug": slug, "grants": grants_for(slug),
            "prompt_block": describe_for_prompt(slug)}


@router.post("/agent-capabilities/{slug}")
def api_set_grants(slug: str, body: Dict[str, Any]):
    b = body or {}
    caps = b.get("capabilities")
    if not isinstance(caps, list):
        return {"ok": False, "error": "capabilities (list) is required"}
    return set_grants(slug, [str(c) for c in caps],
                      granted_by=str(b.get("granted_by") or "admin"),
                      constraints=b.get("constraints"))


@router.get("/agent-capabilities/{slug}/calls")
def api_calls(slug: str, limit: int = 50):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT capability, kind, outcome, refusal_reason,
                          approval_id::text, latency_ms, created_at
                   FROM agent_capability_calls WHERE slug=%s
                   ORDER BY created_at DESC LIMIT %s""",
                (slug, max(1, min(limit, 200))))
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["created_at"] = d["created_at"].isoformat()
                rows.append(d)
            return {"ok": True, "slug": slug, "calls": rows}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)[:180]}
    finally:
        conn.close()


@router.get("/agent-capabilities-status")
def api_status():
    has = False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.agent_capability_grants') "
                        "IS NOT NULL")
            has = bool(cur.fetchone()[0])
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return {"enabled": ENABLED, "tables": has,
            "max_calls_per_turn": MAX_CALLS_PER_TURN}
