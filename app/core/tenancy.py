"""Tenant-routing seam (Platform blindspot P4 — Phase 0).

Conscestra is a SINGLE-ORGANIZATION product today; full SaaS multi-tenancy is
DEFERRED. This module is the minimal, zero-behaviour-change seam that keeps the
door open: it resolves the current request's tenant to a (dsn, schema) pair that
`database.get_connection` applies. With `MULTI_TENANT_ENABLED=0` (default) it
always resolves to today's exact DSN + `public` schema — so nothing changes.

RECOMMENDED future model (ratified 2026-07-24): **schema-per-tenant** by default
(one Postgres, `search_path` switch), **database-per-tenant** for enterprise /
data-residency tenants (a `dsn` override in the registry). Both are served by the
one routing point here — because ALL 360 data-access sites funnel through
`get_connection`, tenancy attaches in exactly one place with NO changes to
business tables or stored procedures.

SCOPE (do not over-claim): this is the ROUTING mechanism, not a hard SaaS
isolation boundary. It proves a request reaches the right schema and a write is
invisible across schemas. Background-worker / cache / rate-limit / billing / full
application isolation are later phases (see docs/multi_tenancy.md). Hard isolation
against untrusted tenants favours database-per-tenant and/or RLS.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg2

logger = logging.getLogger("tenancy")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


MULTI_TENANT_ENABLED = _flag("MULTI_TENANT_ENABLED", "0")
DEFAULT_TENANT_ID = "default"

# Postgres identifier safety: a schema name we will place in a connection's
# search_path MUST match this before it is ever used. Never raw-interpolate.
_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Request-scoped current tenant (same pattern as write_guard's role context).
_tenant_ctx: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "tenant_id", default=None)


class TenantError(RuntimeError):
    """Raised when a tenant id cannot be resolved in multi-tenant mode
    (fail-closed — we never silently fall back to the default tenant)."""


# ── Request context ──────────────────────────────────────────────────────────

def set_tenant(tenant_id: Optional[str]):
    return _tenant_ctx.set(tenant_id or None)


def current_tenant() -> Optional[str]:
    return _tenant_ctx.get()


def reset(token) -> None:
    try:
        _tenant_ctx.reset(token)
    except (ValueError, LookupError):
        pass


# ── Registry (control-plane; lives in the DEFAULT db/public schema) ──────────
# Cached briefly so resolve() doesn't hit the DB on every connection when
# multi-tenant is ON. When OFF, resolve() short-circuits with NO DB access.
_CACHE_TTL = 30.0
_cache: Dict[str, Tuple[float, Optional[Dict[str, str]]]] = {}


def _default_dsn() -> str:
    from app.core.config import get_settings
    return get_settings().db_dsn


def _lookup(tenant_id: str) -> Optional[Dict[str, str]]:
    """Read one tenant row from the control-plane registry. Connects DIRECTLY to
    the default DSN + public (NOT via the tenant-aware get_connection) to avoid
    recursion. Tolerates the table being absent (returns None)."""
    now = time.time()
    hit = _cache.get(tenant_id)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    row: Optional[Dict[str, str]] = None
    try:
        conn = psycopg2.connect(_default_dsn())
        try:
            conn.set_client_encoding("UTF8")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tenant_id, dsn, schema_name, status FROM tenants "
                    "WHERE tenant_id=%s", (tenant_id,))
                r = cur.fetchone()
                if r:
                    row = {"tenant_id": r[0], "dsn": r[1],
                           "schema_name": r[2], "status": r[3]}
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[tenancy] registry lookup skipped ({tenant_id}): {exc}")
        row = None
    _cache[tenant_id] = (now, row)
    return row


def resolve(tenant_id: Optional[str] = None) -> Tuple[str, str]:
    """Return (dsn, schema) for the effective tenant.

    • MULTI_TENANT_ENABLED=0 → ALWAYS (default DSN, 'public'). No DB access, no
      behaviour change. This is the entire product today.
    • Otherwise: the explicit `tenant_id`, else the request-context tenant, else
      the named DEFAULT tenant. The default resolves to (default DSN, 'public')
      (optionally overridden by its registry row). An UNKNOWN or INACTIVE tenant
      FAILS CLOSED (raises) — never an implicit fallback to default.
    """
    if not MULTI_TENANT_ENABLED:
        return _default_dsn(), "public"

    tid = tenant_id or current_tenant() or DEFAULT_TENANT_ID
    row = _lookup(tid)

    if tid == DEFAULT_TENANT_ID:
        if row and row.get("status") == "active":
            return row.get("dsn") or _default_dsn(), _valid_schema(row.get("schema_name"))
        return _default_dsn(), "public"

    if not row or row.get("status") != "active":
        raise TenantError(f"unknown or inactive tenant '{tid}'")
    return row.get("dsn") or _default_dsn(), _valid_schema(row.get("schema_name"))


def _valid_schema(schema: Optional[str]) -> str:
    schema = (schema or "public").strip()
    if schema == "public":
        return "public"
    if not _SCHEMA_RE.match(schema):
        raise TenantError(f"invalid schema name '{schema}' (must match {_SCHEMA_RE.pattern})")
    return schema


# ============================================================================
# BACKGROUND / SCHEDULED WORK (blind spot #9)
# ----------------------------------------------------------------------------
# The interactive path resolves a tenant from the request. Background jobs have
# NO request, so `current_tenant()` is None and every scheduled job silently ran
# against the DEFAULT tenant only — proactive intelligence would go dark for
# every other tenant with no error. These primitives make the tenant an explicit,
# mandatory boundary for scheduled work.
#
# A background job must NOT impersonate a user: `tenant_context()` installs an
# explicit SYSTEM actor (actor_type='system' + the job name) so every action it
# takes is attributable.
#
# With MULTI_TENANT_ENABLED=0 this is a NO-OP by construction: active_tenants()
# returns the single default tenant, so for_each_tenant() runs exactly one
# iteration — identical behaviour to today.
# ============================================================================

# Request/job-scoped actor. Background work is 'system'; interactive work leaves
# this unset (the auth session is the actor).
_actor_ctx: "contextvars.ContextVar[Optional[Dict[str, str]]]" = contextvars.ContextVar(
    "actor", default=None)


def current_actor() -> Optional[Dict[str, str]]:
    """The active SYSTEM actor ({actor_type, job, tenant_id}), or None for
    interactive requests. Audit writers can attribute background actions with it."""
    return _actor_ctx.get()


def active_tenants() -> List[str]:
    """Tenant ids scheduled work must iterate. Single-org (the product today) →
    exactly [DEFAULT_TENANT_ID]. In multi-tenant mode → every ACTIVE registry row
    (falling back to the default tenant if the registry is unreadable, so a
    registry outage degrades to today's behaviour rather than doing nothing)."""
    if not MULTI_TENANT_ENABLED:
        return [DEFAULT_TENANT_ID]
    try:
        conn = psycopg2.connect(_default_dsn())
        try:
            conn.set_client_encoding("UTF8")
            with conn.cursor() as cur:
                cur.execute("SELECT tenant_id FROM tenants WHERE status='active' "
                            "ORDER BY tenant_id")
                ids = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[tenancy] active_tenants lookup failed, using default: {exc}")
        return [DEFAULT_TENANT_ID]
    if DEFAULT_TENANT_ID not in ids:
        ids.insert(0, DEFAULT_TENANT_ID)
    return ids


@contextlib.contextmanager
def tenant_context(tenant_id: Optional[str], job: str = "scheduled"):
    """Run a block as the SYSTEM actor for one tenant. Both the tenant and the
    actor are reset on exit, even on error."""
    t_token = set_tenant(tenant_id)
    a_token = _actor_ctx.set({"actor_type": "system", "job": job,
                              "tenant_id": tenant_id or DEFAULT_TENANT_ID})
    try:
        yield
    finally:
        try:
            _actor_ctx.reset(a_token)
        except (ValueError, LookupError):
            pass
        reset(t_token)


def for_each_tenant(fn: Callable[[], Any], job: str) -> Dict[str, Any]:
    """Run `fn` once per active tenant inside its own tenant+system context.
    One tenant failing never stops the others. Returns
    {tenants, results:{tenant: result}, errors:{tenant: msg}}.

    Single-tenant deployments get one iteration and the SAME result shape their
    caller had before (see `result` for convenience)."""
    tenants = active_tenants()
    results: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for tid in tenants:
        try:
            with tenant_context(tid, job=job):
                results[tid] = fn()
        except Exception as exc:
            logger.error(f"[tenancy] job '{job}' failed for tenant '{tid}': {exc}",
                         exc_info=True)
            errors[tid] = str(exc)[:200]
    out: Dict[str, Any] = {"job": job, "tenants": tenants,
                           "results": results, "errors": errors}
    if len(tenants) == 1:
        out["result"] = results.get(tenants[0])
    return out


def status() -> Dict[str, object]:
    return {"multi_tenant_enabled": MULTI_TENANT_ENABLED,
            "default_tenant": DEFAULT_TENANT_ID,
            "current_tenant": current_tenant(),
            "current_actor": current_actor(),
            "active_tenants": active_tenants()}
