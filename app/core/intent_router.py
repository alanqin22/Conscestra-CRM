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

    @property
    def label(self) -> str:
        return (f"llm:{self.agent}({self.confidence:.2f})"
                if self.via != "keyword" else "keyword")


# telemetry — how the two routers behave relative to each other
_stats = {"llm": 0, "keyword": 0, "cache": 0, "disagreements": 0,
          "llm_failures": 0}
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


def _classify_llm(message: str) -> Optional[Tuple[str, float]]:
    """(agent, confidence) or None on any failure — the caller falls back."""
    try:
        from app.core.graph_utils import _get_llm
        catalog = "\n".join(f"- {name}: {desc}"
                            for name, (_ep, desc) in AGENTS.items())
        resp = _get_llm(tier="lite").invoke([
            {"role": "system", "content":
                "Route ONE CRM user message to the single specialist agent "
                "that owns the answer. Route by what the user NEEDS, not by "
                "surface words — 'my account balance' is money (accounting), "
                "not the account record.\n\nAgents:\n" + catalog +
                "\n\nReply ONLY JSON: {\"agent\": \"<name from the list>\", "
                "\"confidence\": <0.0-1.0>}. Ambiguous or none fit → "
                "{\"agent\": \"" + _DEFAULT_AGENT + "\", \"confidence\": 0.3}."},
            {"role": "user", "content": (message or "")[:300]},
        ])
        text = resp.content if hasattr(resp, "content") else str(resp)
        m = _JSON_RE.search(text)
        d = json.loads(m.group(0)) if m else None
        agent = str((d or {}).get("agent") or "").strip().lower()
        conf = float((d or {}).get("confidence") or 0)
        if agent not in AGENTS:
            logger.info(f"[intent] LLM named unknown agent {agent!r} — fallback")
            return None
        return agent, max(0.0, min(conf, 1.0))
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
        return RouteDecision(hit.endpoint, hit.agent, "cache", hit.confidence)

    try:
        llm = _classify_llm(msg)
    except Exception as exc:                     # pragma: no cover — belt
        logger.warning(f"[intent] classifier raised ({exc}) — keyword fallback")
        llm = None
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

    agent, conf = llm
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
    return {"enabled": ENABLED, "llm_min": LLM_MIN, "cached": len(_cache),
            "stats": dict(_stats),
            "agents": {n: ep for n, (ep, _d) in AGENTS.items()}}
