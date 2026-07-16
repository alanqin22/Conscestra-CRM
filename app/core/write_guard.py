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

# Channel-level read-only flag — deliberately SEPARATE from the role above.
#
# A caller that hands work to an agent over the in-process ASGI transport
# cannot constrain it via _role: require_data_access re-stamps the role on
# every request, overwriting the caller's value long before the agent reaches
# the database. This flag is set by the channel and never touched by the
# request path, so it survives the hop.
#
# It is enforced in execute_sp by opening the connection in a PostgreSQL
# read-only transaction. That is the real guarantee: WRITE_MODES is a blocklist
# and is known to be incomplete (log_call, add_note, create_task, checkout,
# bulk_adjust_stock … all write but are absent), so a public channel must not
# depend on it. The database refuses the write whatever the mode is called.
_readonly_channel: ContextVar[Optional[str]] = ContextVar(
    "readonly_channel", default=None)

# Customer scope — a possession-verified CALLER (voice OTP), not a staff user.
#
# Deliberately harsher than the read-only channel: a verified customer may see
# only their OWN rows, but every stored procedure is CRM-wide by construction
# (sp_accounts lists all accounts, sp_orders all orders …). There is no way to
# bolt a row filter onto an arbitrary SP call from out here, so the choke
# point FAILS CLOSED instead: while this scope is set, execute_sp refuses ALL
# SP access. The customer tier answers exclusively through explicitly
# account-scoped parameterized queries (app/core/voice_support.py) that run in
# a read-only transaction and inject the account_id from THIS scope — never
# from anything the caller said. Writes are never executed on this channel;
# they become governance proposals.
_customer_scope: ContextVar[Optional[dict]] = ContextVar(
    "customer_scope", default=None)


def set_customer_scope(scope: Optional[dict]) -> None:
    """Mark this context as a verified-customer channel. `scope` carries the
    verified identity ({'account_id':…, 'contact_id':…}); None clears it.
    Inherited by everything awaited from here — including any agent call a
    routing bug might reach, which execute_sp then refuses (fail-closed)."""
    _customer_scope.set(scope)


def customer_scope() -> Optional[dict]:
    return _customer_scope.get()


def set_readonly_channel(name: Optional[str]) -> None:
    """Mark this context as a read-only channel (e.g. 'sms'). Inherited by
    everything awaited from here, including in-process agent calls."""
    _readonly_channel.set(name)


def readonly_channel() -> Optional[str]:
    return _readonly_channel.get()


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
    from app.core.auth_dep import API_AUTH_ENABLED, WRITE_MODES, WRITE_ROLES

    # Read-only channel: refuse a recognised write early so the caller gets a
    # clean message instead of a database error. This is a courtesy check, NOT
    # the guarantee — WRITE_MODES misses several real write modes. Anything it
    # lets past still hits the read-only transaction opened in execute_sp.
    chan = _readonly_channel.get()
    if chan:
        m = _MODE_RE.search(query or "")
        if m and m.group(1).lower() in WRITE_MODES:
            raise WritePermissionError(
                f"Read-only channel ({chan}): create, update and delete are "
                "not permitted here.",
                http_status=403,
            )

    role = _role.get()
    if role is None:
        return  # system / background context — never gated
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
