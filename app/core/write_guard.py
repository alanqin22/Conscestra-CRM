"""In-agent write guard (security roadmap #3 — closes the NL-write gap).

The HTTP gate (auth_dep.require_data_access) classifies write-vs-read from the
*structured* `mode` in the request body, so a free-text command typed into the AI
bar ("create a lead", "delete invoice 5") slips through as a read. This guard sits
at the universal DB choke point — execute_sp() — and inspects the agent's RESOLVED
intent (the `p_mode`/`p_action` baked into the SQL by the sql_builder). That intent
exists only AFTER the agent has parsed the NL, so it catches what the HTTP layer
cannot.

Mechanism: require_data_access stamps the caller's role onto a request-scoped
ContextVar; execute_sp calls guard_query() before running any SQL. Outside a request
(role None = scheduler / agent-bus / system) it is a no-op, so background automation
is never blocked.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Optional

# Per-request caller role. None = no request context (system/background) → allowed.
_role: ContextVar[Optional[str]] = ContextVar("request_role", default=None)


def set_request_role(role: Optional[str]) -> None:
    _role.set(role)


def current_role() -> Optional[str]:
    return _role.get()


class WritePermissionError(Exception):
    """Raised when a read-only caller attempts a write SP (no partial write occurs
    — the guard runs before the SQL executes). Agents' db_node re-raise it past
    their generic -500 handler so the router can answer with the real HTTP status:
    401 for anonymous (the frontend auth shim opens the sign-in modal) or 403 for
    a signed-in read-only viewer."""

    def __init__(self, message: str, http_status: int = 403):
        super().__init__(message)
        self.http_status = http_status


# Pull the resolved operation from `sp_x(p_mode := 'create' …)` / `p_action := …`.
_MODE_RE = re.compile(r"p_(?:mode|action)\s*:?=\s*'([a-z_]+)'", re.IGNORECASE)


def guard_query(query: str) -> None:
    """Raise WritePermissionError if the current request's role may not run this
    write SP. No-op when auth is off, outside a request (role None), or for a
    write-capable role. Reads policy from auth_dep (lazy import avoids a cycle)."""
    role = _role.get()
    if role is None:
        return  # system / background context — never gated
    from app.core.auth_dep import API_AUTH_ENABLED, WRITE_MODES, WRITE_ROLES
    if not API_AUTH_ENABLED or role in WRITE_ROLES:
        return
    m = _MODE_RE.search(query or "")
    if m and m.group(1).lower() in WRITE_MODES:
        if role == "anonymous":
            raise WritePermissionError(
                "Please sign in to create, update, or delete records.",
                http_status=401,
            )
        raise WritePermissionError(
            "Read-only access: only Admin or authorized users may create, update, "
            "or delete records. Please sign in with a writer account to make changes.",
            http_status=403,
        )
