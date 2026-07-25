"""Shared API authentication/authorization dependencies (security hardening #1).

Two FastAPI dependencies enforce identity at the edge of the API:

  require_admin    Protects the privileged COMMAND endpoints (agent-bus,
                   supervisor, notification-triage, governance, a2a, blackboard)
                   that can trigger mass mutations. Enforced whenever
                   ADMIN_API_TOKEN is configured — recommended in EVERY deployed
                   environment. Callers present the secret via the 'X-Admin-Token'
                   header (or 'Authorization: Bearer <token>'). The web frontend
                   never calls these endpoints, so enabling this NEVER breaks the
                   UI. Fails closed (403) once a token is set; constant-time compare.

  require_session  Protects the CRM DATA endpoints (the *-chat APIs). Validates a
                   user session issued by /auth/signin. GATED by API_AUTH_ENABLED
                   (default 0) so the current frontend keeps working until it sends
                   the session token — then flip the flag to enforce. Staged
                   rollout, mirroring the rest of this codebase.

CONFIG (env)
  ADMIN_API_TOKEN   ''   shared secret for admin/command endpoints (set a strong value)
  API_AUTH_ENABLED   0   1 = enforce user sessions on data endpoints (after the
                         frontend is updated to send 'Authorization: Bearer <token>')
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("auth_dep")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "").strip()
# Roles allowed to write. viewer + anonymous are read-only.
WRITE_ROLES = {"admin", "member"}

# Two ways to choose the data-endpoint posture, in priority order:
#   1) API_SECURITY_MODE — a single switch (recommended; foolproof):
#        open        → no auth (anyone read + write)
#        public-read → anyone READS; create/update/delete need an Admin/writer login
#        locked      → every data call requires a login (full lockdown)
#   2) Otherwise fall back to the individual flags API_AUTH_ENABLED / API_PUBLIC_READ.
_MODE = os.getenv("API_SECURITY_MODE", "").strip().lower().replace("_", "-")
if _MODE in ("open", "off"):
    API_AUTH_ENABLED, API_PUBLIC_READ = False, False
elif _MODE in ("public-read", "publicread", "read"):
    API_AUTH_ENABLED, API_PUBLIC_READ = True, True
elif _MODE in ("locked", "lockdown", "full", "strict"):
    API_AUTH_ENABLED, API_PUBLIC_READ = True, False
else:
    API_AUTH_ENABLED = _flag("API_AUTH_ENABLED")
    API_PUBLIC_READ = _flag("API_PUBLIC_READ")

_POSTURE = ("open" if not API_AUTH_ENABLED else
            "public-read" if API_PUBLIC_READ else "locked")
# Public name — surfaced via /auth-health so the frontend shim can adapt its
# sign-in notice (locked → "Back to Home"; public-read → "Continue browsing").
SECURITY_POSTURE = _POSTURE
logger.info(f"[security] data-endpoint posture: {_POSTURE} "
            f"(API_AUTH_ENABLED={int(API_AUTH_ENABLED)}, API_PUBLIC_READ={int(API_PUBLIC_READ)})")

# Surface the posture once at import (startup) rather than per request.
if not ADMIN_API_TOKEN:
    logger.warning(
        "[security] ADMIN_API_TOKEN is unset — admin/command endpoints "
        "(/agent-bus, /supervisor, /notif-triage, /governance, /a2a, /blackboard) "
        "are NOT protected. Set ADMIN_API_TOKEN to lock them down."
    )
if not API_AUTH_ENABLED:
    logger.info(
        "[security] API_AUTH_ENABLED=0 — data (*-chat) endpoints accept "
        "unauthenticated calls (staged rollout; enable once the frontend sends "
        "the session token)."
    )


def _bearer(request: Request) -> Optional[str]:
    """Extract a token from 'Authorization: Bearer <t>' or 'X-Session-Token'."""
    h = request.headers.get("authorization") or ""
    if h[:7].lower() == "bearer ":
        return h[7:].strip()
    return request.headers.get("x-session-token")


# Write operations a read-only ('viewer') role may NOT perform. Keyed off the
# explicit 'mode'/'action' in the request body (the structured create/update/
# delete forms). Default-deny by listing writes; unknown/absent modes (NL reads,
# list/get/report/forecast) are allowed.
# ⚠ When adding a NEW write mode to any agent, add it here too — otherwise both
# the HTTP gate and the deep write guard treat it as a read, and an ANONYMOUS
# caller can run it from the NL bar. Two traps this list has fallen into before:
#   • WRAPPER MODES — sp_activities rewrites log_call/add_note/create_task/
#     schedule_meeting/log_email → create and reopen → update INSIDE the
#     function. The guard only ever sees the pre-rewrite name, so the wrapper
#     must be listed BY NAME; matching 'create' is not enough.
#   • p_action VERBS — sp_orders branches on p_action (add_item, soft_delete,
#     update_header, …), which the guard reads alongside p_mode.
# Coverage test: `python scratch/test_write_modes.py` re-derives the write modes
# from sp/*.sql and fails on anything missing here. Run it after touching an SP.
WRITE_MODES = {
    "create", "update", "delete", "edit", "save", "remove", "add",
    "change_stage", "close_won", "close_lost", "convert", "qualify", "disqualify",
    "merge", "archive", "restore", "assign", "reassign", "snooze", "complete",
    "cancel", "approve", "reject", "send",
    "add_product", "update_product", "remove_product", "update_stock", "adjust",
    "show_lead_form", "show_lead_update_form",  # forms that lead to writes
    # Forms for the other entities — same reason as the lead forms above: the
    # form is the entry to a write, so a read-only caller is sent to sign in
    # rather than shown a form whose save would fail. (show_low_stock_form and
    # show_price_history_form are NOT here — those open read-only reports.)
    "show_account_form", "show_account_update_form",
    "show_contact_form", "show_contact_update_form",
    "show_opportunity_form", "show_opportunity_update_form",
    "show_opportunity_add_product_form", "show_opportunity_update_product_form",
    "show_order_form", "show_product_form", "show_bulk_stock_form",
    # WRAPPER MODES — sp_activities rewrites each of these into create/update
    # INSIDE the function ("WRAPPER MODES → CREATE / UPDATE"). The guard only
    # sees the pre-rewrite name in the SQL text, so they must be listed by name
    # or they read as harmless. 'complete' was already here; these were missed.
    "log_call", "log_email", "schedule_meeting", "create_task", "add_note",
    "reopen",
    # p_action verbs on sp_orders (the guard reads p_action as well as p_mode);
    # each writes orders / order_items / audit_log. 'restore' was already here.
    "add_item", "batch_update", "change_status", "hard_delete", "remove_item",
    "soft_delete", "update_header",
    # Direct writes: sp_store INSERTs order_items; sp_products UPDATEs products.
    "checkout_add_items", "bulk_adjust_stock",
    # Module-specific write modes (same names in the request body and in the
    # SQL's p_mode, so both the HTTP gate and the deep write guard catch them):
    "generate_invoice", "record_payment", "void_invoice",      # accounting
    "score",                                                   # leads
    "send_email", "send_template",                             # email
    "mark_read", "mark_unread", "mark_all_read",               # notifications
    "mark_all_unread", "click",
    "advance_statuses",                                        # orders
    "send_verification", "verify_email",                       # contacts
}


async def require_admin(request: Request) -> bool:
    """Gate privileged command endpoints.

    Accepts EITHER a machine/ops token (ADMIN_API_TOKEN via 'X-Admin-Token' or
    bearer) OR a logged-in session whose role is 'admin'. Fails closed (403) once
    any protection is configured; bypasses (with a startup warning) only when both
    ADMIN_API_TOKEN is unset AND API_AUTH_ENABLED=0 (local dev)."""
    # 1. machine/ops shared secret
    if ADMIN_API_TOKEN:
        provided = request.headers.get("x-admin-token") or _bearer(request) or ""
        if secrets.compare_digest(provided, ADMIN_API_TOKEN):
            return True
    # 2. human admin via session — accepted regardless of the data-endpoint
    #    posture (admin-only modules like Email must work in every mode).
    token = _bearer(request)
    if token:
        from app.agents.auth.router import get_session
        sess = get_session(token)
        if sess and sess.get("role") == "admin":
            request.state.session = sess
            return True
    # 3. dev bypass only when nothing is configured
    if not ADMIN_API_TOKEN and not API_AUTH_ENABLED:
        return True
    raise HTTPException(status_code=403, detail="Admin authorization required")


async def require_session(request: Request) -> Optional[Dict[str, Any]]:
    """Validate a CRM user session on data endpoints.

    No-op unless API_AUTH_ENABLED=1 (staged rollout). When enabled, requires a
    valid, non-expired session token from /auth/signin or returns 401. Stashes the
    session on request.state for downstream role checks (e.g. require_write)."""
    if not API_AUTH_ENABLED:
        return None
    # Lazy import avoids any import-order coupling with the auth router.
    from app.agents.auth.router import get_session
    token = _bearer(request)
    sess = get_session(token) if token else None
    if not sess:
        raise HTTPException(status_code=401, detail="Authentication required")
    request.state.session = sess
    return sess


async def _request_mode(request: Request) -> Optional[str]:
    """Best-effort extract of the operation 'mode'/'action' from a JSON body.
    Returns None for non-JSON / bodyless requests (treated as a read)."""
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    ci = body.get("chatInput") if isinstance(body.get("chatInput"), dict) else {}
    mode = body.get("mode") or body.get("action") or ci.get("mode") or ci.get("action")
    return str(mode).strip().lower() if mode else None


async def require_write(request: Request) -> None:
    """Block 'viewer' (read-only) sessions from write operations. No-op unless
    API_AUTH_ENABLED=1. admin/member pass through; viewer is rejected only when the
    request carries an explicit write mode (the structured create/update/delete
    forms)."""
    if not API_AUTH_ENABLED:
        return
    sess = getattr(request.state, "session", None)
    role = (sess or {}).get("role", "member")
    if role != "viewer":
        return
    mode = await _request_mode(request)
    if mode and mode in WRITE_MODES:
        raise HTTPException(
            status_code=403,
            detail="Read-only (viewer) role: write operations are not permitted.",
        )


async def caller_can_write(request: Request) -> bool:
    """True when the caller may perform WRITE-like actions under the current
    posture. For NL-driven write paths that carry NO structured `mode` — e.g. the
    planner's 'plan: … confirm', which queues governance proposals — and which
    require_data_access therefore reads as a harmless read. Open posture → always
    True; otherwise a valid admin token OR a write-capable (admin/member) session."""
    if not API_AUTH_ENABLED:
        return True
    if ADMIN_API_TOKEN:
        provided = request.headers.get("x-admin-token") or _bearer(request) or ""
        if secrets.compare_digest(provided, ADMIN_API_TOKEN):
            return True
    from app.agents.auth.router import get_session
    sess = get_session(_bearer(request) or "")
    return (sess or {}).get("role", "anonymous") in WRITE_ROLES


async def require_data_access(request: Request) -> Optional[Dict[str, Any]]:
    """Unified data-endpoint gate (the demo-friendly model).

      • API_AUTH_ENABLED=0  → no enforcement (open).
      • API_PUBLIC_READ=1   → anyone may READ; a WRITE (structured mode in
        WRITE_MODES) requires a logged-in session whose role is in WRITE_ROLES
        (admin/member). Anonymous writes → 401 (login); viewer writes → 403.
      • API_PUBLIC_READ=0   → every data call requires a valid session (full
        lockdown), and writes still require a write-capable role.

    Replaces the require_session + require_write pair on the data routers."""
    if not API_AUTH_ENABLED:
        return None

    # Resolve the caller's role up front and stamp it on the request context, so
    # the in-agent write guard (write_guard.guard_query, called from execute_sp)
    # can block NL-driven writes even on requests the HTTP layer reads as a read.
    from app.agents.auth.router import get_session
    from app.core.write_guard import set_request_role
    sess = get_session(_bearer(request) or "")
    role = (sess or {}).get("role", "anonymous")
    set_request_role(role)
    # Tenancy (P4 Phase 0): stamp the caller's tenant into the request context so
    # get_connection routes to the right schema. No-op today (MULTI_TENANT_ENABLED=0
    # → resolve() ignores context and returns the default DSN + public schema).
    from app.core import tenancy
    tenancy.set_tenant((sess or {}).get("tenant_id") or tenancy.DEFAULT_TENANT_ID)
    if sess:
        request.state.session = sess

    mode = await _request_mode(request)
    is_write = bool(mode and mode in WRITE_MODES)

    # Public reads: allow anonymous when nothing needs a session (role already
    # stamped above for the deep write guard).
    if API_PUBLIC_READ and not is_write:
        return None

    # From here a session is required (a structured write, or full-lockdown read).
    if not sess:
        raise HTTPException(status_code=401, detail="Authentication required")
    if is_write and role not in WRITE_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Your role is read-only — only Admin/authorized users may "
                   "create, update, or delete records.",
        )
    return sess
