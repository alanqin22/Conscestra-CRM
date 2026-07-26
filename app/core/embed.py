"""Distributable Widget SDK — a drop-in chat widget for anyone's website.

Blindspot #6, the packaged DELIVERY surface Agentforce sells (a web SDK + in-app
messaging customers deploy on THEIR properties). Our agent brains already exist
(the external custom agents of blindspot #3); this wraps one in a single-tag,
origin-scoped, embeddable widget:

    <script src="https://<host>/widget.js" data-embed-key="ek_live_..."></script>

That's the whole integration. The script renders a floating chat bubble that
talks to a PUBLIC, key-scoped endpoint here, which forwards to the mapped agent's
grounded/safe-by-default runtime. Nothing new about the intelligence — just the
distribution.

SECURITY MODEL
  • A key maps to exactly ONE agent, which MUST be `external` scope — an internal
    (employee) agent can never be exposed through an embed key.
  • CORS is echoed ONLY to the key's allowed_origins (empty = any, for dev). The
    key is public by design (it ships in client HTML); origin-scoping + the
    agent's own safe-by-default runtime (no tools, no CRM, guard-screened) are
    what contain it. It cannot read data or take actions — it answers questions.
  • Per-(key, IP) rate limiting.

Requires sql/embed_keys.sql + sql/custom_agents.sql. Kill switch EMBED_ENABLED.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Response

from app.core.database import get_connection

logger = logging.getLogger("embed")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("EMBED_ENABLED", "1")


def _norm_origin(o: str) -> str:
    return (o or "").strip().rstrip("/").lower()


# ============================================================================
# Key CRUD (admin)
# ============================================================================

def _rows(cur) -> List[Dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        for k in ("created_at", "updated_at"):
            if d.get(k):
                d[k] = d[k].isoformat()
        out.append(d)
    return out


def list_keys() -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT embed_key, agent_slug, label, allowed_origins, title, "
                "color, greeting, enabled, rate_limit, created_at "
                "FROM embed_keys ORDER BY created_at DESC")
            return {"ok": True, "keys": _rows(cur)}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"{str(exc)[:160]} (apply sql/embed_keys.sql?)"}
    finally:
        conn.close()


def get_key(embed_key: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT embed_key, agent_slug, label, allowed_origins, title, "
                "color, greeting, enabled, rate_limit FROM embed_keys "
                "WHERE embed_key=%s", (embed_key,))
            rows = _rows(cur)
            return rows[0] if rows else None
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def create_key(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Mint a new embed key for an EXTERNAL custom agent."""
    spec = spec or {}
    agent_slug = str(spec.get("agent_slug") or "").strip()
    if not agent_slug:
        return {"ok": False, "error": "agent_slug is required"}
    # The mapped agent must exist and be EXTERNAL — never expose an internal agent.
    try:
        from app.core import custom_agents
        agent = custom_agents.get_agent(agent_slug)
    except Exception:
        agent = None
    if not agent:
        return {"ok": False, "error": f"custom agent '{agent_slug}' not found"}
    if agent.get("scope") != "external":
        return {"ok": False, "error": "only EXTERNAL (customer-facing) agents can "
                "be embedded — this agent is internal"}
    origins = spec.get("allowed_origins") or []
    if isinstance(origins, str):
        origins = [o.strip() for o in origins.replace("\n", ",").split(",") if o.strip()]
    origins = [_norm_origin(o) for o in origins][:20]
    key = "ek_" + secrets.token_urlsafe(24)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO embed_keys
                     (embed_key, agent_slug, label, allowed_origins, title, color,
                      greeting, enabled, rate_limit, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (key, agent_slug, str(spec.get("label") or agent["display_name"])[:120],
                 origins, str(spec.get("title") or "Chat with us")[:80],
                 str(spec.get("color") or "#0d9488")[:16],
                 (str(spec.get("greeting"))[:300] if spec.get("greeting") else None),
                 bool(spec.get("enabled", True)),
                 int(spec.get("rate_limit") or 30),
                 str(spec.get("created_by") or "admin")))
        conn.commit()
        logger.info(f"[embed] minted key for '{agent_slug}' ({key[:12]}…)")
        return {"ok": True, "embed_key": key, "agent_slug": agent_slug}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"{str(exc)[:160]} (apply sql/embed_keys.sql?)"}
    finally:
        conn.close()


def update_key(embed_key: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    spec = spec or {}
    origins = spec.get("allowed_origins")
    if isinstance(origins, str):
        origins = [o.strip() for o in origins.replace("\n", ",").split(",") if o.strip()]
    if origins is not None:
        origins = [_norm_origin(o) for o in origins][:20]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE embed_keys SET
                     label=COALESCE(%s,label),
                     allowed_origins=COALESCE(%s,allowed_origins),
                     title=COALESCE(%s,title), color=COALESCE(%s,color),
                     greeting=COALESCE(%s,greeting),
                     enabled=COALESCE(%s,enabled),
                     rate_limit=COALESCE(%s,rate_limit), updated_at=now()
                   WHERE embed_key=%s RETURNING embed_key""",
                (spec.get("label"), origins, spec.get("title"), spec.get("color"),
                 spec.get("greeting"),
                 spec.get("enabled") if "enabled" in spec else None,
                 spec.get("rate_limit"), embed_key))
            ok = cur.fetchone() is not None
        conn.commit()
        return {"ok": ok, "embed_key": embed_key}
    finally:
        conn.close()


def delete_key(embed_key: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM embed_keys WHERE embed_key=%s RETURNING embed_key",
                        (embed_key,))
            ok = cur.fetchone() is not None
        conn.commit()
        return {"ok": ok, "embed_key": embed_key}
    finally:
        conn.close()


# ============================================================================
# Origin / CORS + rate limiting
# ============================================================================

def _origin_allowed(key: Dict[str, Any], origin: str) -> bool:
    allow = key.get("allowed_origins") or []
    if not allow:
        return True   # empty allowlist = any origin (dev). Production keys list sites.
    return _norm_origin(origin) in allow


def _cors_headers(key: Dict[str, Any], origin: str) -> Dict[str, str]:
    """Echo CORS to an allowed origin only. No credentials (the key is public,
    there are no cookies), so we can safely reflect the specific origin."""
    if origin and _origin_allowed(key, origin):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
            "Vary": "Origin",
        }
    return {}


_rate: Dict[str, List[float]] = {}


def _rate_ok(key: str, ip: str, limit: int) -> bool:
    bucket = f"{key}:{ip}"
    now = time.time()
    hits = [t for t in _rate.get(bucket, []) if now - t < 600]
    if len(hits) >= max(1, limit):
        _rate[bucket] = hits
        return False
    hits.append(now)
    _rate[bucket] = hits
    return True


# ============================================================================
# Routers
# ============================================================================

admin_router = APIRouter(tags=["embed-admin"])
public_router = APIRouter(tags=["embed"])


@admin_router.get("/embed/keys")
def api_list():
    return list_keys()


@admin_router.post("/embed/keys")
def api_create(body: Dict[str, Any]):
    return create_key(body)


@admin_router.post("/embed/keys/{embed_key}")
def api_update(embed_key: str, body: Dict[str, Any]):
    return update_key(embed_key, body)


@admin_router.delete("/embed/keys/{embed_key}")
def api_delete(embed_key: str):
    return delete_key(embed_key)


def _json(payload: Dict[str, Any], headers: Dict[str, str], status: int = 200) -> Response:
    import json as _j
    return Response(_j.dumps(payload), status_code=status,
                    media_type="application/json", headers=headers)


@public_router.options("/embed/v1/{embed_key}/{rest:path}")
async def embed_preflight(embed_key: str, rest: str, request: Request):
    key = get_key(embed_key)
    origin = request.headers.get("origin", "")
    headers = _cors_headers(key, origin) if key else {}
    return Response(status_code=204, headers=headers)


@public_router.get("/embed/v1/{embed_key}/config")
async def embed_config(embed_key: str, request: Request):
    origin = request.headers.get("origin", "")
    key = get_key(embed_key)
    if not key or not key.get("enabled"):
        return _json({"ok": False, "error": "unknown or disabled key"}, {}, 404)
    if not _origin_allowed(key, origin):
        return _json({"ok": False, "error": "origin not allowed for this key"},
                     {}, 403)
    agent = None
    try:
        from app.core import custom_agents
        agent = custom_agents.get_agent(key["agent_slug"])
    except Exception:
        pass
    examples = (agent.get("examples") if agent else []) or []
    return _json({"ok": True, "title": key["title"], "color": key["color"],
                  "greeting": key.get("greeting"), "examples": examples,
                  "agent": key["agent_slug"]}, _cors_headers(key, origin))


@public_router.post("/embed/v1/{embed_key}/chat")
async def embed_chat(embed_key: str, request: Request):
    if not ENABLED:
        return _json({"ok": False, "error": "embedding disabled"}, {}, 503)
    origin = request.headers.get("origin", "")
    key = get_key(embed_key)
    if not key or not key.get("enabled"):
        return _json({"ok": False, "error": "unknown or disabled key"}, {}, 404)
    cors = _cors_headers(key, origin)
    if not _origin_allowed(key, origin):
        return _json({"ok": False, "error": "origin not allowed for this key"}, {}, 403)
    ip = request.client.host if request.client else "?"
    if not _rate_ok(embed_key, ip, key.get("rate_limit", 30)):
        return _json({"ok": False, "error": "too many messages — slow down"},
                     cors, 429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    history = body.get("history") if isinstance(body.get("history"), list) else None
    # Forward to the mapped agent. custom_agents.run re-checks it exists +
    # enabled; we additionally guarantee external scope at key-creation time.
    # The widget's per-browser session id threads the visitor's turns into ONE
    # conversation, which is what makes an escalation (U1) followable — without
    # it a visitor asking for a human leaves nothing a rep can pick up.
    sid = str(body.get("session_id") or "")[:64] or None
    handle = str(body.get("email") or "").strip()[:200] or None
    from app.core import custom_agents
    res = custom_agents.run(key["agent_slug"], str(body.get("message") or ""),
                            history, session_id=sid, handle=handle,
                            source=f"embed:{embed_key}")
    return _json(res, cors)


@public_router.get("/widget.js")
async def widget_js():
    return Response(_WIDGET_JS, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300"})


@admin_router.get("/embed-status")
def embed_status():
    has = False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.embed_keys') IS NOT NULL")
            has = bool(cur.fetchone()[0])
    except Exception:
        conn.rollback()
    finally:
        conn.close()
    return {"enabled": ENABLED, "table": has}


# ============================================================================
# The widget loader — one self-contained script, no dependencies
# ============================================================================

_WIDGET_JS = r"""
(function () {
  var s = document.currentScript;
  var KEY = s && s.getAttribute('data-embed-key');
  if (!KEY) { console.error('[conscestra-widget] missing data-embed-key'); return; }
  var BASE = (s && s.getAttribute('data-api-base')) ||
             (s && s.src ? new URL(s.src).origin : '');
  var esc = function (x) { return String(x == null ? '' : x)
      .replace(/[<>&"]/g, function (c) { return {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]; }); };
  var cfg = { title: 'Chat with us', color: '#0d9488', greeting: null, examples: [] };
  var history = [], open = false, booted = false;
  // Stable per-browser id so a visitor's turns thread into ONE conversation a
  // human can pick up (U1). Survives reloads; falls back to memory in private
  // mode. Not an identifier of the person — just of the chat thread.
  var SID = (function () {
    var k = 'cw_sid_' + KEY;
    try { var v = localStorage.getItem(k);
          if (!v) { v = 'w' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
                    localStorage.setItem(k, v); }
          return v; }
    catch (e) { return 'w' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); }
  })();

  function el(tag, css, html) { var e = document.createElement(tag); if (css) e.style.cssText = css; if (html != null) e.innerHTML = html; return e; }

  var root = el('div', 'position:fixed;right:20px;bottom:20px;z-index:2147483000;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif');
  var bubble = el('button', 'width:60px;height:60px;border-radius:50%;border:0;cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.28);color:#fff;font-size:26px;display:flex;align-items:center;justify-content:center', '💬');
  var panel = el('div', 'position:absolute;right:0;bottom:74px;width:360px;max-width:calc(100vw - 40px);height:520px;max-height:calc(100vh - 120px);background:#fff;border-radius:16px;box-shadow:0 18px 60px rgba(0,0,0,.32);display:none;flex-direction:column;overflow:hidden');
  root.appendChild(panel); root.appendChild(bubble);

  function paint() {
    bubble.style.background = cfg.color;
    panel.innerHTML =
      '<div style="background:' + esc(cfg.color) + ';color:#fff;padding:14px 16px;font-weight:700;font-size:15px;display:flex;justify-content:space-between;align-items:center">' +
        '<span>' + esc(cfg.title) + '</span><span id="cw-x" style="cursor:pointer;opacity:.85;font-weight:400">✕</span></div>' +
      '<div id="cw-thread" style="flex:1;overflow-y:auto;padding:14px;background:#f8fafc;display:flex;flex-direction:column;gap:8px"></div>' +
      '<div id="cw-chips" style="padding:0 12px 6px;display:flex;gap:6px;flex-wrap:wrap"></div>' +
      '<div style="display:flex;gap:8px;padding:10px 12px;border-top:1px solid #e2e8f0">' +
        '<input id="cw-in" placeholder="Type a message…" style="flex:1;border:1px solid #e2e8f0;border-radius:9px;padding:9px 11px;font:inherit;font-size:14px;outline:none">' +
        '<button id="cw-send" style="border:0;border-radius:9px;background:' + esc(cfg.color) + ';color:#fff;padding:0 14px;font-weight:700;cursor:pointer">Send</button></div>';
    panel.querySelector('#cw-x').onclick = toggle;
    panel.querySelector('#cw-send').onclick = send;
    panel.querySelector('#cw-in').addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
    var chips = panel.querySelector('#cw-chips');
    chips.innerHTML = (cfg.examples || []).slice(0, 3).map(function (q) {
      return '<span class="cw-chip" style="font-size:12px;border:1px solid #e2e8f0;border-radius:999px;padding:5px 10px;cursor:pointer;background:#fff">' + esc(q) + '</span>'; }).join('');
    chips.querySelectorAll('.cw-chip').forEach(function (c) { c.onclick = function () { send(c.textContent); }; });
    var th = panel.querySelector('#cw-thread');
    if (cfg.greeting && !history.length) add(th, cfg.greeting, 'a');
    history.forEach(function (m) { add(th, m.content, m.role === 'user' ? 'u' : 'a'); });
  }
  function add(th, text, who) {
    var m = el('div', 'max-width:82%;padding:8px 11px;border-radius:12px;font-size:14px;line-height:1.45;white-space:pre-wrap;' +
      (who === 'u' ? 'align-self:flex-end;background:' + cfg.color + ';color:#fff' : 'align-self:flex-start;background:#fff;border:1px solid #e2e8f0;color:#0f172a'), esc(text));
    th.appendChild(m); th.scrollTop = th.scrollHeight; return m;
  }
  function send(preset) {
    var inp = panel.querySelector('#cw-in'); var msg = (preset || inp.value).trim(); if (!msg) return;
    inp.value = ''; var th = panel.querySelector('#cw-thread');
    add(th, msg, 'u');
    var typing = add(th, '…', 'a');
    fetch(BASE + '/embed/v1/' + KEY + '/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg, history: history, session_id: SID }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { typing.innerHTML = esc(d && d.ok ? d.reply : (d && d.error) || 'Something went wrong.'); th.scrollTop = th.scrollHeight;
        if (d && d.ok) { history.push({ role: 'user', content: msg }); history.push({ role: 'assistant', content: d.reply }); } })
      .catch(function () { typing.textContent = 'Connection error.'; });
  }
  function toggle() { open = !open; panel.style.display = open ? 'flex' : 'none';
    if (open) { paint(); setTimeout(function () { var i = panel.querySelector('#cw-in'); if (i) i.focus(); }, 50); } }
  bubble.onclick = function () { if (!booted) { booted = true;
      fetch(BASE + '/embed/v1/' + KEY + '/config').then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok) cfg = { title: d.title, color: d.color, greeting: d.greeting, examples: d.examples || [] }; })
        .catch(function () {}).finally(toggle); } else { toggle(); } };

  (document.body || document.documentElement).appendChild(root);
})();
"""
