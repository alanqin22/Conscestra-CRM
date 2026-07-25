"""Data Access Context — the security context every governed READ passes through.

P0 "Trusted Semantic Core", step 3. The write path is governed (auth → RBAC →
tenant → SP), but the AGGREGATE READ paths (/metrics, /analytics/explore) reach
the base tables directly with only soft-delete guards — so a non-admin session,
or an anonymous caller under API_PUBLIC_READ, could read company-wide win rate
and revenue. This module is the one place read authorization is decided.

SCOPE OF THIS STEP (per the 2026-07-24 policy decision):
  • Cross-record analytics is ADMIN/authorized-role ONLY. Everyone else is denied
    (the `require_analytics_access` dependency raises 403). This closes the leak.
  • Rep→owner and customer→own-account row scoping are DEFERRED (no rep-role
    sessions exist yet — auth_sessions carry contact/account identity + role, not
    owner_id). The machinery below (`DataAccessContext.scope_predicate`) is built
    and tested but NOT wired, so flipping a role to 'own_account'/'own_records'
    later is a config change, not new code.

WHY A GATE, NOT A PREDICATE, TODAY: with only 'admin' and customer sessions in
play, the sole outcomes are see-all (admin) or see-nothing (everyone else). A
row predicate only becomes meaningful once a role sits BETWEEN those — a rep who
sees a subset. Until that role + its subject (owner_id on the session) exists,
the honest enforcement is allow/deny. `scope_predicate` is the seam for when it
does; `metrics.compute/compare(extra_where=...)` already accept its output.

TRUST BOUNDARY: internal/system callers (the supervisor loop, briefings) invoke
metrics.compute/compare with NO context — that is BY DESIGN a trusted SYSTEM
context with full visibility. Enforcement lives at the HTTP boundary (the router
dependency), where an untrusted caller is present.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("access")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _roles_env(name: str, default: str) -> set:
    return {r.strip().lower() for r in os.getenv(name, default).split(",") if r.strip()}


# Roles allowed to run cross-record aggregate analytics. Default: admin only.
# Extendable without code (e.g. add an internal 'analyst'/'manager' role here).
ANALYTICS_ROLES = _roles_env("ANALYTICS_FULL_ACCESS_ROLES", "admin")


# Visibility tiers a context can carry. Only 'all' and 'none' are reachable today;
# the scoped tiers are the config-ready seam (see module docstring).
VIS_ALL = "all"            # see every row (admin / internal analyst / system)
VIS_OWN_ACCOUNT = "own_account"   # customer → only their account's rows
VIS_OWN_RECORDS = "own_records"   # rep → only rows they own (owner_id)
VIS_NONE = "none"          # not permitted to run analytics


# Per-entity columns the scoped tiers would filter on (config-ready map). Aliases
# match the metric/explore base clauses ("opportunities o", "orders ord", ...).
_ACCOUNT_COL = {"opportunities": "o.account_id", "orders": "ord.account_id",
                "accounts": "account_id"}
_OWNER_COL = {"opportunities": "o.owner_id", "orders": "ord.owner_id",
              "accounts": "owner_id", "leads": "l.owner_id"}


@dataclass
class DataAccessContext:
    role: str = "anonymous"
    tenant_id: str = "default"
    account_id: Optional[str] = None
    contact_id: Optional[str] = None
    owner_id: Optional[str] = None       # not populated today (no rep sessions)
    visibility: str = VIS_NONE

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_session(cls, sess: Optional[Dict[str, Any]]) -> "DataAccessContext":
        """Build the context from an auth_sessions row (or None for anonymous).
        Visibility today: ANALYTICS_ROLES → all; everyone else → none."""
        sess = sess or {}
        role = str(sess.get("role") or "anonymous").strip().lower()
        vis = VIS_ALL if role in ANALYTICS_ROLES else VIS_NONE
        return cls(
            role=role,
            tenant_id=str(sess.get("tenant_id") or "default"),
            account_id=sess.get("account_id"),
            contact_id=sess.get("contact_id"),
            owner_id=sess.get("owner_id"),   # present only once sessions carry it
            visibility=vis,
        )

    # ── policy ──────────────────────────────────────────────────────────────
    def may_run_analytics(self) -> bool:
        return self.visibility != VIS_NONE

    def scope_predicate(self, entity: str) -> Optional[str]:
        """The row-scoping SQL predicate for `entity` under this context, or None
        for unrestricted (VIS_ALL / system). CONFIG-READY: only VIS_ALL is
        reachable today, so this returns None in practice — but the scoped
        branches are implemented so enabling them later is a policy flip.

        The id is validated as a UUID before interpolation (session data is
        trusted, but we never place an unvalidated value in SQL). A future
        production wiring should bind it as a parameter instead."""
        if self.visibility in (VIS_ALL, VIS_NONE):
            return None
        if self.visibility == VIS_OWN_ACCOUNT:
            col, ident = _ACCOUNT_COL.get(entity), self.account_id
        elif self.visibility == VIS_OWN_RECORDS:
            col, ident = _OWNER_COL.get(entity), self.owner_id
        else:
            return None
        if not col or not ident:
            # No scoping column for this entity, or no subject id → fail CLOSED
            # (a scoped context that can't be scoped must see nothing, never all).
            return "false"
        if not _UUID_RE.match(str(ident)):
            raise ValueError(f"access scope id is not a uuid: {ident!r}")
        return f"{col} = '{ident}'"


# ============================================================================
# FastAPI dependency — the HTTP-boundary gate. require_data_access (the _DATA
# gate) runs first and stamps request.state.session; we read it here.
# ============================================================================

def context_from_request(request: Request) -> DataAccessContext:
    sess = getattr(getattr(request, "state", None), "session", None)
    return DataAccessContext.from_session(sess)


async def require_analytics_access(request: Request) -> DataAccessContext:
    """Gate cross-record analytics to authorized roles. Raises 403 otherwise.
    Returns the context so an endpoint MAY use ctx.scope_predicate(...) once
    scoped tiers are enabled (today it is always unrestricted for the callers
    that pass)."""
    ctx = context_from_request(request)
    if not ctx.may_run_analytics():
        raise HTTPException(
            status_code=403,
            detail=("Aggregate analytics (metrics / explore) is restricted to "
                    "authorized roles. Individual records remain available "
                    "through their normal governed endpoints."))
    return ctx
