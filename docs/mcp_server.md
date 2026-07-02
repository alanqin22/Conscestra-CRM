# Conscestra CRM — MCP Server

`app/mcp_server.py` exposes the CRM to any MCP-capable AI assistant (Claude Code,
Claude Desktop, etc.) as 7 tools. It is a thin stdio proxy over the running
backend's HTTP API — no business logic of its own.

## Tools

| Tool | What it does | Auth |
|---|---|---|
| `ask_agent(agent, message)` | NL question to any of the 11 agents | public data endpoints |
| `company_pulse()` | Cross-module KPI overview in one call | public |
| `ai_summary(entity, name)` | Decision-grade 360 of an account/contact/opportunity | public |
| `web_search(query)` | Live internet answer with cited sources | public |
| `list_capabilities()` | The A2A intent registry (21 capabilities) | admin token |
| `dispatch_intent(intent, params_json, …, dry_run=True)` | Structured A2A call; **dry-run by default**, real writes still pass the governance gate | admin token |
| `governance_queue()` | Pending human approvals + gating policy | admin token |

## Setup

The repo's `.mcp.json` already registers the server for Claude Code in this
project (venv python + `app/mcp_server.py`, pointed at `http://localhost:8000`).
Restart Claude Code in the repo and approve the project MCP server when
prompted, or add it manually:

```
claude mcp add conscestra-crm -- d:/a/crm_agent/venv/Scripts/python.exe d:/a/crm_agent/app/mcp_server.py
```

For Claude Desktop, add the same command block to
`%APPDATA%/Claude/claude_desktop_config.json` under `mcpServers`.

## Config (env — read from the repo `.env` automatically)

| Var | Default | Meaning |
|---|---|---|
| `CRM_API_URL` | `http://localhost:8000` | backend to proxy (set the Railway URL for prod) |
| `CRM_ADMIN_TOKEN` | falls back to `ADMIN_API_TOKEN` | required for the 3 admin tools |

## Safety model

- `dispatch_intent` defaults to `dry_run=True`; a client must explicitly pass
  `dry_run=False` for a real write.
- Real writes then hit the CRM's own governance layer: high-confidence acts,
  medium queues for human approval (see `governance-mgmt.html`), low skips.
- The three admin tools require the admin token; the NL tools use the same
  public data endpoints as the web frontend (subject to `API_SECURITY_MODE`).
