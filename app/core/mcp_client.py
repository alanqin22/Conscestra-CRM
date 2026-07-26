"""MCP Client — U6 (round-2 blindspots, 2026-07-25).

THE ASYMMETRY
    `app/mcp_server.py` exposes 7 tools, so we are already a good MCP citizen
    *inbound*. Nothing of ours can CONSUME an external MCP server, so the
    fleet's only reach past our own database is `web_tools.py`. Every new
    integration is custom connector code, custom auth and a custom permission
    model for us — and a config row for a platform with an MCP gateway.

WHY THIS RIDES U4 INSTEAD OF INVENTING A SECOND PERMISSION MODEL
    An external MCP tool is simply another thing an agent might be allowed to
    do. Giving it a parallel grant system would mean two authorization models
    to keep in sync, and the weaker one would eventually be the one that leaks.
    So a registered tool is PROJECTED into `agent_capability_catalog` as
    `mcp:<server>.<tool>` and inherits U4 wholesale — an agent must be granted
    it, cannot invent it, cannot widen its own grant, and scope governs reach.

THE ONE RULE MCP NEEDS THAT LOCAL CAPABILITIES DON'T
    Calling an external MCP server SENDS OUR DATA TO A THIRD PARTY. That is
    egress, exactly like a prompt to an LLM provider, so U5's data-class gate
    applies: internal-tier content must not leave. And because tool code is
    something we cannot audit, tools default to requiring human approval —
    "we do not know what this does" is an argument for a person, not against
    recording it.

    Three independent gates, each of which alone is sufficient to refuse:
      1. server registered AND enabled (an allowlist, not discovery)
      2. tool enabled individually (listing a tool is not permission)
      3. U4 grant + U5-style data-class egress check

Requires sql/mcp_servers.sql. See [agent_capabilities] (U4 — the authorization
model this reuses), [llm_router] (U5 — the egress rule this mirrors).

CONFIG (env)
  MCP_CLIENT_ENABLED   1   kill switch
  MCP_CALL_TIMEOUT     20  default per-call timeout (server rows may override)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("mcp_client")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("MCP_CLIENT_ENABLED", "1")
DEFAULT_TIMEOUT = int(os.getenv("MCP_CALL_TIMEOUT", "20"))

CAP_PREFIX = "mcp:"          # capability id namespace: mcp:<server>.<tool>


def capability_id(server: str, tool: str) -> str:
    return f"{CAP_PREFIX}{server}.{tool}"


def split_capability(cap: str) -> Optional[Tuple[str, str]]:
    if not cap.startswith(CAP_PREFIX):
        return None
    rest = cap[len(CAP_PREFIX):]
    if "." not in rest:
        return None
    server, tool = rest.split(".", 1)
    return server, tool


# ============================================================================
# Registry
# ============================================================================

def register_server(name: str, url: str, label: str = "",
                    auth_env_var: Optional[str] = None,
                    max_scope: str = "internal",
                    allow_internal_data: bool = False,
                    timeout_secs: int = DEFAULT_TIMEOUT,
                    registered_by: str = "admin",
                    note: str = "") -> Dict[str, Any]:
    """Add an external MCP server to the allowlist. DISABLED on creation — a
    server becomes callable only when an admin enables it, after seeing what
    tools it actually offers."""
    if not ENABLED:
        return {"ok": False, "error": "mcp client disabled"}
    if not name or not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "name and an http(s) url are required"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_servers (name, label, url, auth_env_var,
                       max_scope, allow_internal_data, timeout_secs,
                       registered_by, note, enabled)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,false)
                   ON CONFLICT (name) DO UPDATE SET
                     label=EXCLUDED.label, url=EXCLUDED.url,
                     auth_env_var=EXCLUDED.auth_env_var,
                     max_scope=EXCLUDED.max_scope,
                     allow_internal_data=EXCLUDED.allow_internal_data,
                     timeout_secs=EXCLUDED.timeout_secs,
                     note=EXCLUDED.note, updated_at=now()
                   RETURNING name""", (name, label, url, auth_env_var,
                                       max_scope, allow_internal_data,
                                       int(timeout_secs), registered_by, note))
            saved = cur.fetchone()[0]
        conn.commit()
        logger.info(f"[mcp] registered server '{saved}' (disabled until enabled)")
        return {"ok": True, "server": saved, "enabled": False,
                "next": "discover its tools, then enable the server and the "
                        "individual tools you want"}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"{str(exc)[:180]} "
                "(apply sql/mcp_servers.sql?)"}
    finally:
        conn.close()


def list_servers() -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.name, s.label, s.url, s.enabled, s.max_scope,
                          s.allow_internal_data, s.auth_env_var, s.timeout_secs,
                          s.last_probe_ok, s.last_probe_detail,
                          count(t.tool_id) FILTER (WHERE t.enabled) AS tools_enabled,
                          count(t.tool_id) AS tools_known
                   FROM mcp_servers s
                   LEFT JOIN mcp_tools t ON t.server_name = s.name
                   GROUP BY s.name, s.label, s.url, s.enabled, s.max_scope,
                            s.allow_internal_data, s.auth_env_var, s.timeout_secs,
                            s.last_probe_ok, s.last_probe_detail
                   ORDER BY s.name""")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            # Never leak the credential itself — only whether one is configured.
            for r in rows:
                r["auth_configured"] = bool(r.pop("auth_env_var", None)
                                            and os.getenv(r.get("auth_env_var") or ""))
            return {"ok": True, "servers": rows}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"{str(exc)[:180]} (apply sql/mcp_servers.sql?)"}
    finally:
        conn.close()


def _server(name: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT name,url,transport,auth_env_var,enabled,max_scope,
                          allow_internal_data,timeout_secs
                   FROM mcp_servers WHERE name=%s""", (name,))
            r = cur.fetchone()
            if not r:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, r))
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def set_server_enabled(name: str, enabled: bool) -> Dict[str, Any]:
    return _flip("mcp_servers", "name", name, enabled)


def set_tool_enabled(server: str, tool: str, enabled: bool,
                     requires_approval: Optional[bool] = None) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if requires_approval is None:
                cur.execute("""UPDATE mcp_tools SET enabled=%s, updated_at=now()
                               WHERE server_name=%s AND tool_name=%s
                               RETURNING tool_name""", (enabled, server, tool))
            else:
                cur.execute("""UPDATE mcp_tools SET enabled=%s,
                                 requires_approval=%s, updated_at=now()
                               WHERE server_name=%s AND tool_name=%s
                               RETURNING tool_name""",
                            (enabled, requires_approval, server, tool))
            ok = cur.fetchone() is not None
        conn.commit()
        if ok:
            _sync_catalog(server)
        return {"ok": ok, "server": server, "tool": tool, "enabled": enabled}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)[:180]}
    finally:
        conn.close()


def _flip(table: str, key: str, val: str, enabled: bool) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET enabled=%s, updated_at=now() "
                        f"WHERE {key}=%s RETURNING {key}", (enabled, val))
            ok = cur.fetchone() is not None
        conn.commit()
        if ok and table == "mcp_servers":
            _sync_catalog(val)
        return {"ok": ok, key: val, "enabled": enabled}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)[:180]}
    finally:
        conn.close()


# ============================================================================
# Transport — one place that talks MCP
# ============================================================================

async def _with_session(srv: Dict[str, Any], fn):
    """Open a short-lived MCP session and run `fn(session)`. Deliberately not
    pooled: an external server is untrusted infrastructure, and a per-call
    session keeps a hung peer from holding resources."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {}
    env = srv.get("auth_env_var")
    if env and os.getenv(env):
        headers["Authorization"] = f"Bearer {os.getenv(env)}"
    timeout = int(srv.get("timeout_secs") or DEFAULT_TIMEOUT)

    async with streamablehttp_client(srv["url"], headers=headers or None) as (
            read, write, _):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout)
            return await asyncio.wait_for(fn(session), timeout=timeout)


def discover(name: str) -> Dict[str, Any]:
    """List a server's tools and record them — DISABLED. Discovery is a read,
    not a grant: knowing a tool exists is not permission to call it."""
    if not ENABLED:
        return {"ok": False, "error": "mcp client disabled"}
    srv = _server(name)
    if not srv:
        return {"ok": False, "error": f"server '{name}' is not registered"}

    async def _list(session):
        return await session.list_tools()

    t0 = time.time()
    try:
        res = asyncio.run(_with_session(srv, _list))
        tools = [{"name": t.name,
                  "description": (t.description or "")[:800],
                  "input_schema": getattr(t, "inputSchema", None) or {}}
                 for t in getattr(res, "tools", [])]
        detail = f"{len(tools)} tools"
        ok = True
    except Exception as exc:
        tools, ok = [], False
        detail = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning(f"[mcp] discover '{name}' failed: {detail}")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE mcp_servers SET last_probed_at=now(),
                             last_probe_ok=%s, last_probe_detail=%s
                           WHERE name=%s""", (ok, detail[:300], name))
            for t in tools:
                cur.execute(
                    """INSERT INTO mcp_tools (server_name, tool_name,
                          description, input_schema)
                       VALUES (%s,%s,%s,%s::jsonb)
                       ON CONFLICT (server_name, tool_name) DO UPDATE SET
                         description=EXCLUDED.description,
                         input_schema=EXCLUDED.input_schema, updated_at=now()""",
                    (name, t["name"], t["description"],
                     json.dumps(t["input_schema"])[:8000]))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[mcp] recording tools failed: {exc}")
    finally:
        conn.close()

    _sync_catalog(name)
    return {"ok": ok, "server": name, "tools": tools,
            "latency_ms": int((time.time() - t0) * 1000), "detail": detail,
            "note": "tools are recorded DISABLED — enable individually"}


# ============================================================================
# Projection into U4's catalogue — one authorization model, not two
# ============================================================================

def _sync_catalog(server: str) -> None:
    """Project this server's ENABLED tools into `agent_capability_catalog` so
    U4 governs them exactly like a native capability. Disabled tools and
    disabled servers are removed, so turning something off actually revokes it
    rather than leaving a stale grantable row behind."""
    srv = _server(server)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT tool_name, description, enabled,
                                  requires_approval
                           FROM mcp_tools WHERE server_name=%s""", (server,))
            rows = cur.fetchall()
            for tool, desc, t_enabled, needs_ok in rows:
                cap = capability_id(server, tool)
                live = bool(srv and srv.get("enabled") and t_enabled)
                if not live:
                    # Revoke: drop grants first (FK), then the catalogue row.
                    cur.execute("DELETE FROM agent_capability_grants "
                                "WHERE capability=%s", (cap,))
                    cur.execute("DELETE FROM agent_capability_catalog "
                                "WHERE capability=%s", (cap,))
                    continue
                cur.execute(
                    """INSERT INTO agent_capability_catalog
                         (capability, kind, label, description, grantable,
                          requires_approval, max_scope)
                       VALUES (%s,'write',%s,%s,true,%s,%s)
                       ON CONFLICT (capability) DO UPDATE SET
                         label=EXCLUDED.label, description=EXCLUDED.description,
                         grantable=EXCLUDED.grantable,
                         requires_approval=EXCLUDED.requires_approval,
                         max_scope=EXCLUDED.max_scope, updated_at=now()""",
                    (cap, f"{server}: {tool}", (desc or "")[:500],
                     bool(needs_ok), (srv or {}).get("max_scope", "internal")))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[mcp] catalog sync skipped for '{server}': {exc}")
    finally:
        conn.close()


# ============================================================================
# Invocation
# ============================================================================

def may_call(server: str, data_class: str) -> Tuple[bool, str]:
    """Egress gate — the rule MCP needs that a local capability does not.

    Calling an external MCP server sends our data to a third party, so the same
    reach rule U5 applies to LLM providers applies here."""
    srv = _server(server)
    if not srv:
        return False, f"server '{server}' is not registered"
    if not srv["enabled"]:
        return False, f"server '{server}' is registered but not enabled"
    if data_class == "INTERNAL_SENSITIVE" and not srv["allow_internal_data"]:
        return False, (f"'{server}' is a third party and is not approved for "
                       "internal-tier content")
    return True, "allowed"


def call_tool(server: str, tool: str, params: Dict[str, Any],
              caller: str = "system",
              data_class: str = "BUSINESS_INTERNAL") -> Dict[str, Any]:
    """Invoke one external MCP tool. Three gates, each sufficient to refuse:
    server enabled, tool enabled, egress permitted."""
    t0 = time.time()
    if not ENABLED:
        return {"ok": False, "outcome": "refused", "reason": "mcp client disabled"}

    allowed, why = may_call(server, data_class)
    if not allowed:
        _audit(server, tool, caller, data_class, "refused", why, params, 0)
        return {"ok": False, "outcome": "refused", "reason": why}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT enabled FROM mcp_tools
                           WHERE server_name=%s AND tool_name=%s""",
                        (server, tool))
            row = cur.fetchone()
    except Exception:
        conn.rollback(); row = None
    finally:
        conn.close()
    if not row or not row[0]:
        why = f"tool '{tool}' is not enabled on '{server}'"
        _audit(server, tool, caller, data_class, "refused", why, params, 0)
        return {"ok": False, "outcome": "refused", "reason": why}

    srv = _server(server)

    async def _call(session):
        return await session.call_tool(tool, params or {})

    try:
        res = asyncio.run(_with_session(srv, _call))
        out = []
        for blk in getattr(res, "content", []) or []:
            txt = getattr(blk, "text", None)
            if txt:
                out.append(str(txt))
        text = "\n".join(out)[:4000]
        ms = int((time.time() - t0) * 1000)
        _audit(server, tool, caller, data_class, "executed", None, params, ms)
        return {"ok": True, "outcome": "executed", "content": text,
                "is_error": bool(getattr(res, "isError", False)),
                "latency_ms": ms}
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        reason = f"{type(exc).__name__}: {str(exc)[:140]}"
        _audit(server, tool, caller, data_class, "error", reason, params, ms)
        logger.warning(f"[mcp] {server}.{tool} failed: {reason}")
        return {"ok": False, "outcome": "error", "reason": reason}


def _audit(server: str, tool: str, caller: str, data_class: str, outcome: str,
           reason: Optional[str], params: Dict[str, Any], ms: int) -> None:
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
                """INSERT INTO mcp_calls (server_name, tool_name, caller,
                     data_class, outcome, refusal_reason, params, latency_ms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (server, tool, caller[:120], data_class, outcome, reason,
                 json.dumps(safe)[:4000], ms))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def status() -> Dict[str, Any]:
    srv = list_servers()
    return {"enabled": ENABLED,
            "servers": srv.get("servers", []) if srv.get("ok") else [],
            "error": None if srv.get("ok") else srv.get("error"),
            "note": ("we both SERVE MCP (app/mcp_server.py, 7 tools) and now "
                     "CONSUME it; consumed tools are governed by U4 grants")}


# ============================================================================
# Router (admin)
# ============================================================================

router = APIRouter(tags=["mcp-client"])


@router.get("/mcp/servers")
def api_list():
    return status()


@router.post("/mcp/servers")
def api_register(body: Dict[str, Any]):
    b = body or {}
    return register_server(
        name=str(b.get("name") or ""), url=str(b.get("url") or ""),
        label=str(b.get("label") or ""), auth_env_var=b.get("auth_env_var"),
        max_scope=str(b.get("max_scope") or "internal"),
        allow_internal_data=bool(b.get("allow_internal_data")),
        timeout_secs=int(b.get("timeout_secs") or DEFAULT_TIMEOUT),
        registered_by=str(b.get("registered_by") or "admin"),
        note=str(b.get("note") or ""))


@router.post("/mcp/servers/{name}/discover")
def api_discover(name: str):
    return discover(name)


@router.post("/mcp/servers/{name}/enabled")
def api_server_enabled(name: str, body: Dict[str, Any]):
    return set_server_enabled(name, bool((body or {}).get("enabled", True)))


@router.post("/mcp/servers/{name}/tools/{tool}/enabled")
def api_tool_enabled(name: str, tool: str, body: Dict[str, Any]):
    b = body or {}
    ra = b.get("requires_approval")
    return set_tool_enabled(name, tool, bool(b.get("enabled", True)),
                            None if ra is None else bool(ra))


@router.post("/mcp/call")
def api_call(body: Dict[str, Any]):
    b = body or {}
    return call_tool(str(b.get("server") or ""), str(b.get("tool") or ""),
                     b.get("params") or {},
                     caller=str(b.get("caller") or "admin"),
                     data_class=str(b.get("data_class") or "BUSINESS_INTERNAL"))


@router.get("/mcp/calls")
def api_calls(limit: int = 50):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT server_name,tool_name,caller,data_class,
                                  outcome,refusal_reason,latency_ms,created_at
                           FROM mcp_calls ORDER BY created_at DESC LIMIT %s""",
                        (max(1, min(limit, 200)),))
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                d["created_at"] = d["created_at"].isoformat()
                rows.append(d)
            return {"ok": True, "calls": rows}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)[:180]}
    finally:
        conn.close()
