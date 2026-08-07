"""Shared LLM factory and graph building utilities for all CRM Agent modules.

Every agent graph imports _get_llm(), _call_ollama_direct(), and the shared
AgentState TypedDict from this module.  Agent-specific nodes (pre_router,
ai_agent, db, format) are wired in each agent's own graph.py.

Adding a new agent
------------------
1. Import AgentState, _get_llm, _call_ollama_direct, build_standard_graph
   in the new agent's graph.py.
2. Define pre_router_node, ai_agent_node, db_node, formatter_node.
3. Call build_standard_graph(nodes_dict) to get a compiled graph.
   Or wire a custom topology if the agent needs non-standard edges.
"""

from __future__ import annotations

import json
import os
import re
import logging
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
try:
    from langchain_ollama import ChatOllama
except ImportError:
    from langchain_community.chat_models import ChatOllama

from .config import get_settings

logger = logging.getLogger(__name__)

# GPT-5 / o-series reasoning models reject any non-default temperature on the
# Chat Completions API (only temperature=1, the default, is accepted) — so
# for these the parameter must be omitted rather than passed as 0.1.
_OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")

# GPT-5 deliberates before it answers, and at the default effort that thinking
# is a large share of the wall clock on the turns we actually serve: short,
# factual, RAG-grounded replies where there is nothing to reason ABOUT because
# the answer is already sitting in the retrieved text. 'minimal' keeps the same
# model and the same grounding and skips the deliberation — the cheapest
# latency win available on the standard tier (voice's Level-0 brain runs on the
# lite tier, gpt-4o-mini, which has no reasoning parameter at all).
#
# Env-overridable, and an EMPTY value omits the parameter entirely, so an
# unsupported value is a config fix rather than a deploy — the same escape
# hatch the voice STT codes use, for the same reason: "documented" is not the
# same as "works on our account".
_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "minimal").strip()


def _openai_chat_kwargs(model: str) -> Dict[str, Any]:
    m = (model or "").lower()
    if m.startswith(_OPENAI_REASONING_PREFIXES):
        # 'minimal' is a GPT-5-family value. The o-series accepts only
        # low/medium/high and rejects 'minimal', so it must never be sent
        # there — hence the narrower prefix test than the one above.
        if _REASONING_EFFORT and m.startswith("gpt-5"):
            return {"reasoning_effort": _REASONING_EFFORT}
        return {}
    return {"temperature": 0.1}


# ============================================================================
# SHARED STATE DEFINITION
# All agent graphs use this same TypedDict — ensures consistent state keys.
# ============================================================================

class AgentState(TypedDict):
    session_id:      str
    chat_input:      dict           # raw chatInput fields forwarded from the request
    user_input:      str            # message string (raw or preprocessed)
    router_action:   bool           # True = pre-router hit → skip AI Agent
    ai_output:       Optional[str]
    parsed_json:     Optional[Dict[str, Any]]
    should_call_api: bool
    db_rows:         Optional[List]
    final_output:    Optional[str]


# ============================================================================
# LLM FACTORY
# ============================================================================

def _get_llm(tier: str = "standard", caller: str = None,
             data_internal: bool = False):
    """Return the configured LLM (OpenAI or Ollama ChatModel), wrapped in the
    usage meter (app/core/llm_meter): every invoke is budget-checked and
    recorded per caller — the fleet's fuel gauge.

    tier="lite" uses LLM_MODEL_LITE when set — for high-volume, low-stakes
    wording (SDR replies, auto-replies, SMS) that doesn't need the flagship
    model. caller defaults to the calling module's name (sdr, planner, …).

    data_internal=True marks a request as carrying INTERNAL-tier knowledge
    (the employee IT/HR agents). It drives U5's provider policy — internal
    content must not reach an external provider merely because the primary had
    an outage, which is the LLM-layer analogue of U2's reach_invariant."""
    import os as _os
    import sys as _sys
    settings = get_settings()
    model = settings.llm_model
    if tier == "lite":
        model = _os.getenv("LLM_MODEL_LITE", "").strip() or model
    if caller is None:
        try:
            caller = _sys._getframe(1).f_globals.get(
                "__name__", "unknown").rsplit(".", 1)[-1]
        except Exception:
            caller = "unknown"
    if settings.llm_provider == "openai":
        logger.info(f"Using OpenAI LLM: {model} (caller={caller}, tier={tier})")
        inner = ChatOpenAI(
            api_key=settings.openai_api_key,
            model=model,
            **_openai_chat_kwargs(model),
        )
    else:
        logger.info(f"Using Ollama LLM: {model} (caller={caller}, tier={tier})")
        inner = ChatOllama(
            base_url=settings.ollama_base_url,
            model=model,
            temperature=0.1,
        )
    try:
        from app.core.llm_meter import MeteredLLM
        return MeteredLLM(inner, caller=caller, model=model, tier=tier,
                          provider=settings.llm_provider,
                          alt_factory=_alt_client_factory(tier),
                          data_internal=data_internal)
    except Exception as exc:                  # the meter must never break the fleet
        logger.warning(f"LLM meter unavailable — returning unmetered model: {exc}")
        return inner


def _alt_client_factory(tier: str):
    """Build the LAZY constructor for an alternate provider (U5).

    Returns None when failover is off or unconfigured, which makes MeteredLLM
    behave exactly as it did before U5. The alternate client is only ever
    constructed if the primary actually fails — a healthy request pays nothing
    for the existence of a failover path."""
    try:
        from app.core import llm_router
        if not llm_router.ENABLED:
            return None
    except Exception:
        return None

    def _build(provider: str, model: str, timeout: float):
        if provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI
            import os as _o
            return ChatGoogleGenerativeAI(
                model=model, google_api_key=_o.getenv("GOOGLE_API_KEY", ""),
                temperature=0.1, timeout=timeout, max_retries=0)
        if provider == "ollama":
            s = get_settings()
            return ChatOllama(base_url=s.ollama_base_url, model=model,
                              temperature=0.1)
        if provider == "openai":
            s = get_settings()
            return ChatOpenAI(api_key=s.openai_api_key, model=model,
                              timeout=timeout, max_retries=0,
                              **_openai_chat_kwargs(model))
        raise RuntimeError(f"no client builder for provider '{provider}'")

    return _build


def _call_ollama_direct(system_prompt: str, messages: list) -> str:
    """
    Direct Ollama /api/chat call — bypasses ChatOllama which can silently
    drop the ``thinking`` field from reasoning models (Qwen / gpt-oss /
    DeepSeek-R1).

    Falls back to the ``thinking`` field if ``content`` is empty, matching
    the pattern needed for chain-of-thought models that place their JSON
    answer inside <think> blocks.
    """
    import httpx
    settings = get_settings()

    payload = {
        "model":    settings.ollama_model,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "stream":   False,
        "options":  {"temperature": 0.1},
    }

    resp = httpx.post(
        f"{settings.ollama_base_url}/api/chat",
        json=payload,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()

    msg      = data.get("message", {})
    content  = msg.get("content",  "") or ""
    thinking = msg.get("thinking", "") or ""

    logger.info(f"Ollama direct — content: {len(content)} chars, thinking: {len(thinking)} chars")

    if content.strip():
        return content
    if thinking.strip():
        logger.info("Content empty — using thinking field")
        return thinking
    logger.warning("Ollama returned empty content AND empty thinking")
    return ""


# ============================================================================
# JSON PARSER UTILITIES  (shared by all parse_output_node implementations)
# ============================================================================

def extract_json_objects(text: str) -> list:
    """
    Stack-based extractor — handles nested JSON objects correctly.
    Mirrors the n8n Parse AI Output extractJsonObjects() function.
    """
    results = []
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                results.append(text[start: i + 1])
                start = -1
    return results


def parse_ai_json(ai_output: str) -> Optional[Dict[str, Any]]:
    """
    Extract the last valid JSON object containing a ``mode`` key from the
    AI output string.

    Strategy (mirrors n8n Parse AI Output Code node):
      1. Stack-based extraction — walk from END, pick last valid JSON with mode
      2. Markdown code block fallback
      3. Last {...} block regex fallback

    Returns the parsed dict, or None if no valid JSON with mode was found.
    """
    if not ai_output:
        return None

    # 1. Stack-based
    candidates = extract_json_objects(ai_output)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
            if parsed.get("mode"):
                logger.info(f"Parsed JSON via stack-extractor: mode={parsed['mode']}")
                return parsed
        except json.JSONDecodeError:
            pass

    # 2. Markdown code block
    md_match = re.search(r'```json\s*([\s\S]*?)```', ai_output)
    if md_match:
        try:
            parsed = json.loads(md_match.group(1).strip())
            if parsed.get("mode"):
                logger.info(f"Parsed JSON via markdown block: mode={parsed['mode']}")
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. Last {...} block regex
    last_match = re.search(r'(\{[^{}]*\})\s*$', ai_output)
    if last_match:
        try:
            parsed = json.loads(last_match.group(1))
            if parsed.get("mode"):
                logger.info(f"Parsed JSON via last-brace regex: mode={parsed['mode']}")
                return parsed
        except json.JSONDecodeError:
            pass

    # 4. JSON found but without a mode key — infer mode from known field names
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
            if parsed.get("mode"):
                continue  # already handled above
            inferred: Optional[str] = None
            if "search" in parsed:
                inferred = "list"
            elif any(k in parsed for k in ("contact_id", "account_id", "lead_id",
                                            "opportunity_id", "order_id", "product_id")):
                inferred = "get"
            elif any(k in parsed for k in ("firstName", "first_name",
                                            "lastName", "last_name")):
                # Name-only fields with no contact details → search query, not create
                has_details = any(k in parsed for k in ("email", "phone", "role",
                                                         "status", "billing_address"))
                if has_details:
                    inferred = "create"
                else:
                    inferred = "list"
                    # Synthesise a search term from the name parts
                    first = parsed.get("firstName") or parsed.get("first_name", "")
                    last  = parsed.get("lastName")  or parsed.get("last_name",  "")
                    name  = f"{first} {last}".strip()
                    if name:
                        parsed["search"] = name
            elif any(k in parsed for k in ("email", "account_name", "company")):
                inferred = "create"
            if inferred:
                parsed["mode"] = inferred
                logger.info(f"Inferred mode '{inferred}' from JSON fields (no mode key): {list(parsed.keys())}")
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    # 5. [MODE:xxx] text marker fallback — model emitted a tag instead of JSON
    mode_tag = re.search(r'\[MODE:(\w+)\]', ai_output, re.IGNORECASE)
    if mode_tag:
        mode = mode_tag.group(1).lower()
        params: Dict[str, Any] = {"mode": mode}
        # Try to pick up a search term that appears near the marker
        search_m = re.search(r'search[:\s]+["\']?([^"\'\[\]\n,]+)["\']?', ai_output, re.IGNORECASE)
        if search_m:
            params["search"] = search_m.group(1).strip()
        logger.info(f"Parsed mode via [MODE:xxx] marker fallback: {params}")
        return params

    logger.info("No valid JSON with mode found — conversational response")
    return None


# ============================================================================
# STANDARD GRAPH BUILDER
# Wires the canonical 5-node topology shared by all current agents.
# Agents with non-standard topology should build their own graph instead.
# ============================================================================

def build_standard_graph(
    pre_router_node,
    ai_agent_node,
    parse_output_node,
    db_node,
    formatter_node,
    graph_label: str = "CRM Agent",
) -> object:
    """
    Build and compile the standard LangGraph topology:

        pre_router ─┬─[direct_db]──→ db ──→ format → END
                    └─[ai_agent]──→ ai_agent → parse ─┬─[call_db]──→ db → format → END
                                                       └─[skip_db]──→ format → END

    Parameters
    ----------
    pre_router_node   : callable(state) → state
    ai_agent_node     : callable(state) → state
    parse_output_node : callable(state) → state
    db_node           : callable(state) → state
    formatter_node    : callable(state) → state
    graph_label       : Human-readable name for log messages.
    """
    logger.info(f"Building {graph_label} LangGraph...")

    def _route_after_pre_router(state):
        if state.get("router_action"):
            return "direct_db"
        return "ai_agent"

    def _route_after_parse(state):
        if state.get("should_call_api"):
            return "call_db"
        return "skip_db"

    graph = StateGraph(AgentState)
    graph.add_node("pre_router", pre_router_node)
    graph.add_node("ai_agent",   ai_agent_node)
    graph.add_node("parse",      parse_output_node)
    graph.add_node("db",         db_node)
    graph.add_node("format",     formatter_node)

    graph.set_entry_point("pre_router")
    graph.add_conditional_edges(
        "pre_router",
        _route_after_pre_router,
        {"direct_db": "db", "ai_agent": "ai_agent"},
    )
    graph.add_edge("ai_agent", "parse")
    graph.add_conditional_edges(
        "parse",
        _route_after_parse,
        {"call_db": "db", "skip_db": "format"},
    )
    graph.add_edge("db",     "format")
    graph.add_edge("format", END)

    logger.info(f"{graph_label} LangGraph built successfully")
    return graph.compile()


# ============================================================================
# EXTENDED-STATE GRAPH BUILDER (for agents with extra state keys e.g. Orders)
# Same topology as build_standard_graph but accepts a custom state_schema.
# ============================================================================

def build_graph_with_schema(
    state_schema,
    pre_router_node,
    ai_agent_node,
    parse_output_node,
    db_node,
    formatter_node,
    graph_label: str = "CRM Agent",
) -> object:
    """
    Same canonical 5-node topology as build_standard_graph but the caller
    supplies the TypedDict class used for StateGraph(schema).

    Use this when your agent needs extra state keys beyond the base AgentState
    (e.g. the Orders agent adds body, current_message, raw_message, params,
    format_result).
    """
    logger.info(f"Building {graph_label} LangGraph (custom schema)...")

    def _route_after_pre_router(state):
        return "direct_db" if state.get("router_action") else "ai_agent"

    def _route_after_parse(state):
        return "call_db" if state.get("should_call_api") else "skip_db"

    graph = StateGraph(state_schema)
    graph.add_node("pre_router", pre_router_node)
    graph.add_node("ai_agent",   ai_agent_node)
    graph.add_node("parse",      parse_output_node)
    graph.add_node("db",         db_node)
    graph.add_node("format",     formatter_node)

    graph.set_entry_point("pre_router")
    graph.add_conditional_edges(
        "pre_router", _route_after_pre_router,
        {"direct_db": "db", "ai_agent": "ai_agent"},
    )
    graph.add_edge("ai_agent", "parse")
    graph.add_conditional_edges(
        "parse", _route_after_parse,
        {"call_db": "db", "skip_db": "format"},
    )
    graph.add_edge("db",     "format")
    graph.add_edge("format", END)

    logger.info(f"{graph_label} LangGraph built successfully")
    return graph.compile()
