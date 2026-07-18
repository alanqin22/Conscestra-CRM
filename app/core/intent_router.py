"""Orchestrator v2 — LLM intent routing with the keyword router as fallback.

The v1 keyword router (orchestrator `_route_single`) is fast and free, but
brittle at the seams: "What is my ACCOUNT BALANCE?" contains 'account' so it
lands on the Accounts agent, which asks "which account?" — when the answer
lives with Accounting. This module puts a small LLM classifier in front:

    route(message) → RouteDecision(endpoint, agent, via, confidence)

    1. CACHE     — normalized repeats are free (SMS follow-ups, retries).
    2. LLM       — lite tier, one strict-JSON call over the agent catalog.
                   Trusted only when the answer names a real agent AND
                   confidence ≥ INTENT_LLM_MIN.
    3. KEYWORD   — v1 `_route_single` answers whenever the LLM is disabled,
                   fails, hallucinates an agent, or hedges. Routing must
                   never break because an LLM had a bad day.

Disagreements between LLM and keyword are logged and counted — that delta is
the learning signal for tuning the catalog descriptions.

CONSUMERS: orchestrator single-agent delegation, the SMS + voice operator
tiers, and the KB gap miner. Deterministic fast paths (web search, pulse,
executive bank, symphonies, A2A `route:` handles) stay ahead of this and are
untouched.

CONFIG (env)
  INTENT_ROUTER_ENABLED  1     LLM classification on/off (off = pure keyword)
  INTENT_LLM_MIN         0.55  trust the LLM at/above this confidence
  INTENT_PLAN_ROUTING    0     route confident multi-step GOALS to the planner
  INTENT_PLAN_MIN        0.70  trust a 'plan' label at/above this confidence
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger("intent_router")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("INTENT_ROUTER_ENABLED", "1")
LLM_MIN = float(os.getenv("INTENT_LLM_MIN", "0.55"))

# Step 4 (dark by default): route a confident MULTI-STEP GOAL to the bounded
# planner instead of a single agent. When off, route() behaves EXACTLY as
# before — the plan instruction isn't even added to the classifier prompt.
PLAN_ROUTING = _flag("INTENT_PLAN_ROUTING", "0")
PLAN_MIN = float(os.getenv("INTENT_PLAN_MIN", "0.70"))

_CACHE_MAX = 500

# The routable catalog: agent name → (endpoint, what it owns). Descriptions
# are the classifier's whole world — tune THEM, not the prompt, when a
# message routes wrong.
AGENTS: Dict[str, Tuple[str, str]] = {
    "accounts": ("/account-chat",
                 "customer companies/organizations: profiles, status, "
                 "industry, revenue tier, account records themselves"),
    "contacts": ("/contact-chat",
                 "individual people at customer accounts: names, emails, "
                 "phone numbers, roles, who works where"),
    "leads": ("/lead-chat",
              "prospects not yet customers: lead scores, sources, "
              "qualification, ratings, conversion"),
    "opportunities": ("/opportunity-chat",
                      "deals and the sales pipeline: stages, deal amounts, "
                      "close dates, win rates"),
    "orders": ("/order-chat",
               "placed orders and fulfillment: order status, shipping, "
               "deliveries, monthly sales summaries"),
    "products": ("/prod-chat",
                 "the product catalog and inventory: items, pricing, "
                 "stock levels, low stock"),
    "activities": ("/activity-chat",
                   "tasks, meetings, calls, follow-ups, schedules, notes — "
                   "and the default for unclear requests"),
    "accounting": ("/accounting-chat",
                   "money: invoices, payments, balances owed, accounts "
                   "receivable, revenue, financial summaries"),
    "analytics": ("/analytics-chat",
                  "cross-module analysis and reports: cash flow, forecasts, "
                  "dashboards, trends, owner/source breakdowns"),
    "notifications": ("/notifications-chat",
                      "system alerts and unread notifications"),
    "email": ("/email-chat",
              "composing and sending emails, outreach, the shared inbox"),
}

_DEFAULT_AGENT = "activities"
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class RouteDecision:
    endpoint: str
    agent: str
    via: str            # 'llm' | 'keyword' | 'cache'
    confidence: float
    kind: str = "single_agent"   # 'single_agent' | 'plan'

    @property
    def label(self) -> str:
        if self.kind == "plan":
            return f"plan({self.confidence:.2f})"
        return (f"llm:{self.agent}({self.confidence:.2f})"
                if self.via != "keyword" else "keyword")


# telemetry — how the two routers behave relative to each other
_stats = {"llm": 0, "keyword": 0, "cache": 0, "disagreements": 0,
          "llm_failures": 0, "plan": 0}
_cache: Dict[str, RouteDecision] = {}
_cache_lock = threading.Lock()


def _keyword_endpoint(message: str) -> str:
    from app.agents.orchestrator.router import _route_single
    return _route_single((message or "").lower())


def _agent_for_endpoint(endpoint: str) -> str:
    for name, (ep, _d) in AGENTS.items():
        if ep == endpoint:
            return name
    return _DEFAULT_AGENT


def _classify_llm(message: str) -> Optional[Tuple[str, float, str]]:
    """(agent, confidence, kind) or None on any failure — the caller falls back.
    `kind` is always 'single_agent' unless PLAN_ROUTING is on AND the classifier
    labels the message a multi-step goal ('plan'). The plan instruction/field are
    only added to the prompt when PLAN_ROUTING is on, so with the flag off this
    returns exactly what it did before (kind always 'single_agent')."""
    try:
        from app.core.graph_utils import _get_llm
        catalog = "\n".join(f"- {name}: {desc}"
                            for name, (_ep, desc) in AGENTS.items())
        plan_rule = (
            "\n\nOR, if the message is a MULTI-STEP GOAL that needs several "
            "capabilities coordinated (e.g. 'recover our overdue receivables AND "
            "re-engage the slipped deals this week') rather than a single lookup "
            "or one action, set kind='plan' — a single question or one action is "
            "kind='single_agent'." if PLAN_ROUTING else "")
        kind_field = ", \"kind\": \"single_agent\"|\"plan\"" if PLAN_ROUTING else ""
        resp = _get_llm(tier="lite").invoke([
            {"role": "system", "content":
                "Route ONE CRM user message to the single specialist agent "
                "that owns the answer. Route by what the user NEEDS, not by "
                "surface words — 'my account balance' is money (accounting), "
                "not the account record.\n\nAgents:\n" + catalog + plan_rule +
                "\n\nReply ONLY JSON: {\"agent\": \"<name from the list>\", "
                "\"confidence\": <0.0-1.0>" + kind_field + "}. Ambiguous or none "
                "fit → {\"agent\": \"" + _DEFAULT_AGENT + "\", \"confidence\": 0.3}."},
            {"role": "user", "content": (message or "")[:300]},
        ])
        text = resp.content if hasattr(resp, "content") else str(resp)
        m = _JSON_RE.search(text)
        d = json.loads(m.group(0)) if m else None
        agent = str((d or {}).get("agent") or "").strip().lower()
        conf = float((d or {}).get("confidence") or 0)
        kind = str((d or {}).get("kind") or "single_agent").strip().lower()
        if kind not in ("single_agent", "plan"):
            kind = "single_agent"
        if agent not in AGENTS:
            logger.info(f"[intent] LLM named unknown agent {agent!r} — fallback")
            return None
        return agent, max(0.0, min(conf, 1.0)), kind
    except Exception as exc:
        logger.warning(f"[intent] LLM classify failed (keyword fallback): {exc}")
        return None


def route(message: str) -> RouteDecision:
    """Synchronous routing decision (LLM → keyword fallback). Call from a
    thread (asyncio.to_thread) inside async handlers — the LLM leg blocks."""
    msg = (message or "").strip()
    kw_endpoint = _keyword_endpoint(msg)

    if not ENABLED or not msg:
        _stats["keyword"] += 1
        return RouteDecision(kw_endpoint, _agent_for_endpoint(kw_endpoint),
                             "keyword", 0.0)

    key = re.sub(r"\s+", " ", msg.lower())[:200]
    with _cache_lock:
        hit = _cache.get(key)
    if hit:
        _stats["cache"] += 1
        return RouteDecision(hit.endpoint, hit.agent, "cache", hit.confidence,
                             kind=hit.kind)

    try:
        llm = _classify_llm(msg)
    except Exception as exc:                     # pragma: no cover — belt
        logger.warning(f"[intent] classifier raised ({exc}) — keyword fallback")
        llm = None

    # Plan routing (dark unless INTENT_PLAN_ROUTING): a confident multi-step
    # goal is handed to the bounded planner, not a single agent. The endpoint is
    # moot for a plan decision (the caller dispatches the planner) — keep the
    # keyword endpoint as a harmless fallback. Cached like any other decision.
    if (PLAN_ROUTING and llm is not None and llm[0] in AGENTS
            and llm[2] == "plan" and llm[1] >= PLAN_MIN):
        _stats["plan"] += 1
        logger.info(f"[intent] LLM labelled a plan (@{llm[1]:.2f}) for {msg[:60]!r}")
        decision = RouteDecision(kw_endpoint, "planner", "llm", llm[1], kind="plan")
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[key] = decision
        return decision

    # Belt and braces: whatever the classifier returned, only a KNOWN agent
    # at/above the confidence floor is trusted — route() must never raise.
    if llm is None or llm[0] not in AGENTS or llm[1] < LLM_MIN:
        if llm is None or llm[0] not in AGENTS:
            _stats["llm_failures"] += 1
        else:
            logger.info(f"[intent] LLM hedged ({llm[0]} @{llm[1]:.2f} < "
                        f"{LLM_MIN}) — keyword fallback")
        _stats["keyword"] += 1
        return RouteDecision(kw_endpoint, _agent_for_endpoint(kw_endpoint),
                             "keyword", 0.0)

    agent, conf, _kind = llm
    decision = RouteDecision(AGENTS[agent][0], agent, "llm", conf)
    if decision.endpoint != kw_endpoint:
        _stats["disagreements"] += 1
        logger.info(f"[intent] LLM overrode keyword: {kw_endpoint} → "
                    f"{decision.endpoint} ({conf:.2f}) for {msg[:60]!r}")
    _stats["llm"] += 1
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()          # simple + bounded beats clever here
        _cache[key] = decision
    return decision


async def aroute(message: str) -> RouteDecision:
    """Async wrapper — runs the (possibly LLM-blocking) route() off-loop."""
    import asyncio
    return await asyncio.to_thread(route, message)


# ============================================================================
# Admin status
# ============================================================================

from fastapi import APIRouter          # noqa: E402  (router tail, house style)

router = APIRouter(tags=["intent-router"])


@router.get("/intent-router/status")
def intent_router_status():
    return {"enabled": ENABLED, "llm_min": LLM_MIN,
            "plan_routing": PLAN_ROUTING, "plan_min": PLAN_MIN,
            "cached": len(_cache), "stats": dict(_stats),
            "agents": {n: ep for n, (ep, _d) in AGENTS.items()}}
