"""Conscestra CRM — MCP server.

Exposes the CRM's agents and A2A capability registry as MCP tools, so any
MCP-capable AI assistant (Claude Code / Claude Desktop / other clients) can
query and orchestrate the CRM.

Runs as a stdio server that PROXIES the running backend over HTTP — it holds
no business logic of its own. Point it at local dev or Railway via env:

    CRM_API_URL      backend base URL   (default http://localhost:8000)
    CRM_ADMIN_TOKEN  X-Admin-Token for the admin-gated endpoints
                     (falls back to ADMIN_API_TOKEN from .env)

Register (Claude Code):  .mcp.json in the repo root already points here, or
    claude mcp add conscestra-crm -- <venv-python> d:/a/crm_agent/app/mcp_server.py
(.mcp.json itself MUST stay at the repo root — that's where MCP clients
discover project-scope configuration.)

Safety: dispatch_intent defaults to dry_run=True; real writes additionally
pass through the CRM's own governance confidence gate + approval queue.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# .env lives at the repo root (this file is app/mcp_server.py)
load_dotenv(Path(__file__).parent.parent / ".env")

API = (os.getenv("CRM_API_URL", "") or "http://localhost:8000").rstrip("/")
ADMIN_TOKEN = (os.getenv("CRM_ADMIN_TOKEN", "") or os.getenv("ADMIN_API_TOKEN", "")).strip()

mcp = FastMCP(
    "conscestra-crm",
    instructions=(
        "Conscestra CRM — an AI-native CRM. Use ask_agent for natural-language "
        "questions about accounts, contacts, leads, opportunities, orders, "
        "products, activities, analytics or accounting. Use company_pulse for a "
        "cross-module KPI overview, ai_summary for a decision-grade 360 of one "
        "record, and dispatch_intent (dry-run by default) for structured A2A "
        "capability calls."),
)

AGENT_ENDPOINTS = {
    "accounts": "/account-chat", "contacts": "/contact-chat", "leads": "/lead-chat",
    "opportunities": "/opportunity-chat", "orders": "/order-chat",
    "products": "/prod-chat", "activities": "/activity-chat",
    "analytics": "/analytics-chat", "accounting": "/accounting-chat",
    "notifications": "/notifications-chat", "orchestrator": "/orchestrator-chat",
}


def _post(path: str, body: dict, admin: bool = False, timeout: float = 300.0) -> Any:
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["X-Admin-Token"] = ADMIN_TOKEN
    r = httpx.post(f"{API}{path}", json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _get(path: str, admin: bool = False, timeout: float = 60.0) -> Any:
    headers = {"X-Admin-Token": ADMIN_TOKEN} if admin else {}
    r = httpx.get(f"{API}{path}", headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _chat(endpoint: str, message: str) -> str:
    d = _post(endpoint, {"chatInput": {"message": message},
                         "sessionId": "mcp-session"})
    if isinstance(d, list):
        d = d[0] if d else {}
    return str(d.get("output") or d.get("error") or json.dumps(d)[:2000])


@mcp.tool()
def ask_agent(agent: str, message: str) -> str:
    """Ask one of the CRM's AI agents a natural-language question.

    agent: accounts | contacts | leads | opportunities | orders | products |
           activities | analytics | accounting | notifications | orchestrator
    message: the question, e.g. 'Show top 10 accounts by revenue' or
             'Show overdue invoices'. The orchestrator keyword-routes to the
             best agent when you are unsure which one to ask.
    """
    ep = AGENT_ENDPOINTS.get((agent or "").strip().lower())
    if not ep:
        return f"Unknown agent '{agent}'. Choose one of: {', '.join(AGENT_ENDPOINTS)}"
    return _chat(ep, message)


@mcp.tool()
def company_pulse() -> str:
    """Cross-module KPI overview (pipeline, orders, invoices, payments, leads,
    activities, alerts) in one call — the CRM's 'company pulse'."""
    return _chat("/orchestrator-chat", "company pulse")


@mcp.tool()
def ai_summary(entity: str, name: str) -> str:
    """Decision-grade AI 360 summary of one record: snapshot, momentum/health,
    risks (incl. live agent signals), and recommended next actions.

    entity: account | contact | opportunity
    name: the record's name, e.g. 'Costa Retail Group', 'Ethan Wong',
          'New Business - Costa Retail Group'
    """
    ep = {"account": "/account-chat", "contact": "/contact-chat",
          "opportunity": "/opportunity-chat"}.get((entity or "").strip().lower())
    if not ep:
        return "entity must be one of: account, contact, opportunity"
    return _chat(ep, f"AI summary for {name}")


@mcp.tool()
def web_search(query: str) -> str:
    """Live internet answer with cited sources (the CRM's built-in free web
    intelligence: DuckDuckGo search + page fetch + synthesis)."""
    return _chat("/orchestrator-chat", f"Search the web for {query}")


@mcp.tool()
def list_capabilities() -> str:
    """List the A2A capability registry: every structured intent agents (and
    you, via dispatch_intent) can invoke, with owning agent and kind."""
    return json.dumps(_get("/a2a/capabilities", admin=True), indent=2, default=str)


@mcp.tool()
def dispatch_intent(intent: str, params_json: str = "{}",
                    entity_type: Optional[str] = None,
                    entity_id: Optional[str] = None,
                    dry_run: bool = True) -> str:
    """Dispatch a structured A2A intent (see list_capabilities).

    dry_run defaults to True — set dry_run=False only when the user explicitly
    confirms a write. Writes are additionally confidence-gated by the CRM's
    governance layer (may queue for human approval instead of executing).
    """
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as e:
        return f"params_json is not valid JSON: {e}"
    body = {"intent": intent, "from_agent": "mcp", "params": params,
            "dry_run": bool(dry_run)}
    if entity_type and entity_id:
        body["entity_type"] = entity_type
        body["entity_id"] = entity_id
    return json.dumps(_post("/a2a/dispatch", body, admin=True), indent=2, default=str)


@mcp.tool()
def governance_queue() -> str:
    """Agent actions waiting for human approval (the governance queue), plus
    the current gating policy. Review/decide in governance-mgmt.html."""
    status = _get("/governance/status", admin=True)
    queue = _get("/governance/queue", admin=True)
    return json.dumps({"policy": status, "pending": queue.get("pending", [])},
                      indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
