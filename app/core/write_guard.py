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
from typing import Any, Dict, Optional

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


def scoped_rows(sql: str, params: Optional[dict] = None) -> list:
    """THE customer-scoped read. One implementation, every channel.

    The account_id / contact_id placeholders are filled FROM THE VERIFIED
    SCOPE, never from caller-supplied values, and the transaction is opened
    READ-ONLY so the database itself refuses a write regardless of what the
    SQL says.

    Extracted from voice_support when the portal became the second consumer.
    A per-channel copy would drift, and the weakest copy would decide what a
    customer can see — so voice, chat, the portal and anything later share
    this exact function.

    Raises PermissionError when no verified scope is present: absence of a
    scope is never treated as permission to read everything."""
    from app.core.database import get_connection
    from psycopg2.extras import RealDictCursor

    scope = customer_scope()
    if not scope or not scope.get("account_id"):
        raise PermissionError("no verified customer scope on this context")
    merged = {**(params or {}), "account_id": scope["account_id"],
              "contact_id": scope.get("contact_id")}
    conn = get_connection()
    try:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, merged)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


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


# ============================================================================
# THE READ-ONLY DECISION — one question, asked once, at the DB chokepoint
# ============================================================================
# WHY THIS EXISTS. `guard_query` decides "may this role run this write?" by
# matching the resolved p_mode against WRITE_MODES — a hand-maintained
# BLOCKLIST that this file already documents as incomplete by construction.
# For a read-only CHANNEL that incompleteness was survivable, because
# execute_sp also opened a PostgreSQL read-only transaction and the database
# refused the write whatever the mode was called.
#
# An ANONYMOUS HTTP caller had no such backstop. `readonly_channel()` is None
# on a web request, so no read-only transaction was opened, and the blocklist
# was the ONLY thing between the open internet and a mutation under the
# `public-read` posture. A write mode missing from the list was an
# authorization bypass, and the list's sole coverage proof was a script in a
# gitignored directory that was not run by the test suite.
#
# So the same guarantee is extended to the caller's ROLE. A role that may not
# write gets the same read-only transaction a read-only channel gets, and the
# blocklist stops being load-bearing: it degrades from "the control" to "a
# courtesy check that produces a friendlier error message earlier".
#
# MEASURED SAFE, not assumed. Every list/get/summary/report mode across the 20
# reachable stored procedures was executed inside a read-only transaction: all
# 20 succeeded, so no read path performs an incidental write that this would
# newly refuse. The only behaviour that changes is a write that was previously
# permitted BECAUSE IT WAS MISSING FROM THE LIST.
#
# Deliberately NOT applied when the role is None. That is the system/background
# context (scheduler, agent-bus, governance execution, the public governance
# email-link router, the store, the portal, telephony) — none of which passes
# through require_data_access, and all of which must be able to write.

def readonly_context() -> Optional[Dict[str, Any]]:
    """Why this context must run inside a PostgreSQL read-only transaction,
    or None when it may write.

    Returns the refusal to raise if the database does reject a write, so the
    caller never has to re-derive who it was talking to:

        {"reason": 'channel'|'role', "subject": <name>,
         "message": <what the caller is told>, "http_status": 401|403}
    """
    chan = _readonly_channel.get()
    if chan:
        return {"reason": "channel", "subject": chan,
                "message": (f"Read-only channel ({chan}): create, update and "
                            f"delete are not permitted here."),
                "http_status": 403}

    role = _role.get()
    if role is None:
        return None                      # system / background — never gated

    from app.core.auth_dep import API_AUTH_ENABLED, WRITE_ROLES
    if not API_AUTH_ENABLED or role in WRITE_ROLES:
        return None

    if role == "anonymous":
        return {"reason": "role", "subject": role,
                "message": "Please sign in to create, update, or delete records.",
                "http_status": 401}
    return {"reason": "role", "subject": role,
            "message": ("Read-only access: only Admin or authorized users may "
                        "create, update, or delete records. Please sign in with "
                        "a writer account to make changes."),
            "http_status": 403}


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


# ============================================================================
# FORBIDDEN PROCEDURES — legacy write paths no role may use
# ============================================================================
# Distinct from WRITE_MODES, which asks "may THIS ROLE write?". These procedures
# are not a permission question at all: they are a SECOND mutation boundary that
# bypasses the governed domain layer entirely, so no role — including admin and
# the system context — may reach them.
#
# sp_cases(): authored January 2026 beside the cases table, never governed. 14
# modes, writes `status` and `owner_id` (close/resolve/reopen/escalate/assign),
# and has NO record_field_history awareness. Executing it would skip the case
# state machine, owner validation and field history in a single call — the
# governed path is app/core/cases.py and there is no legitimate second one.
#
# NOTE ON DEPTH: whether the database-privilege half of this defence is live
# depends on the role the application connects as, and THAT IS NOT SOMETHING
# THIS COMMENT CAN KNOW. It used to try: it stated that the application
# connects as `postgres` — a superuser, whose privilege-check bypass makes a
# REVOKE inert — and concluded "THIS GUARD IS THE ONLY EFFECTIVE CONTROL."
# Privilege separation later shipped and production moved to `crm_app`, and
# the comment did not move with it. It then spent months telling readers to
# discount a defence layer that was working.
#
# The question is now asked of the database at startup by
# `release_guard._check_db_privileges`, which reports the REAL role and
# whether it is a superuser. Read that, not this.
#
# What has not changed: do not weaken this guard on the assumption that the
# database is backstopping it. This is the control that holds in either case.
FORBIDDEN_PROCEDURES = {
    "sp_cases": ("Case mutations must go through app/core/cases.py, which "
                 "enforces the lifecycle state machine, owner validation and "
                 "field history. sp_cases() bypasses all three."),
}

_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(FORBIDDEN_PROCEDURES) + r")\s*\(", re.I)


def guard_query(query: str) -> None:
    """Raise WritePermissionError if the current request's role may not run this
    write SP. No-op when auth is off, outside a request (role None), or for a
    write-capable role. Reads policy from auth_dep (lazy import avoids a cycle)."""
    from app.core.auth_dep import API_AUTH_ENABLED, WRITE_MODES, WRITE_ROLES

    # UNCONDITIONAL, and deliberately FIRST — before the read-only-channel
    # courtesy check and before every role early-return below, because those
    # exempt exactly the callers most able to do damage (system context and
    # write-capable roles). A forbidden legacy path fails VISIBLY: the query is
    # never rewritten and never silently rerouted to the governed layer, so the
    # caller learns their code is wrong instead of appearing to work.
    m = _FORBIDDEN_RE.search(query or "")
    if m:
        proc = m.group(1).lower()
        raise WritePermissionError(
            f"{proc}() is a forbidden legacy write path. "
            f"{FORBIDDEN_PROCEDURES[proc]}",
            http_status=403,
        )

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
