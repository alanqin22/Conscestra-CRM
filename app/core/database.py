"""Shared database connectivity for all CRM Agent modules.

Design
------
execute_sp(query)
    Generic executor for any stored procedure.  The query is a fully-built
    SQL string (e.g. SELECT sp_accounts(...) AS result) produced by the
    agent-specific sql_builder module.

    The column alias in the SELECT must always be ``result``.  Agent
    sql_builders already emit ``AS result``, so existing queries work
    unchanged.

Adding a new agent
------------------
No changes needed here.  The new agent's sql_builder emits a query ending
with ``... AS result;`` and calls execute_sp() directly.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from .config import get_settings

logger = logging.getLogger(__name__)


# ── Connection factory ────────────────────────────────────────────────────────

def get_connection():
    """Return a raw psycopg2 connection for the current tenant.

    Tenancy (P4 Phase 0): the connection's (dsn, schema) come from
    `tenancy.resolve()`. With MULTI_TENANT_ENABLED=0 (default, the entire product
    today) this is ALWAYS (DB_DSN, 'public') → behaviourally identical to before.
    In multi-tenant mode a per-tenant schema is applied via the `search_path`
    connection option, so it is (re)established on EVERY connection (never a stale
    or pooled search_path); the schema identifier is validated inside
    `tenancy.resolve()` before it reaches here — never raw-interpolated.

    Forces client_encoding=UTF8 so multi-byte characters (e.g. 'é' in 'Québec',
    en-dashes) round-trip correctly regardless of what the remote PostgreSQL
    server negotiates by default.
    """
    from app.core import tenancy
    dsn, schema = tenancy.resolve()
    if schema and schema != "public":
        conn = psycopg2.connect(dsn, options=f"-c search_path={schema},public")
    else:
        conn = psycopg2.connect(dsn)
    conn.set_client_encoding('UTF8')
    return conn


# ── Generic SP executor ───────────────────────────────────────────────────────

def execute_sp(query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Execute any stored-procedure query and return a list of row dicts.

    The query must alias its return value as ``result``::

        SELECT sp_accounts(p_mode := 'list') AS result;
        SELECT sp_contacts(p_mode := 'list') AS result;

    The ``result`` column value is parsed from JSONB/str → dict where
    possible.  Agents receive the same structure they produced when
    running standalone.

    Parameters
    ----------
    query  : Fully-formed SQL string produced by an agent's sql_builder.
    params : Optional dict of psycopg2 named parameters (rarely needed since
             queries are pre-built, but kept for future flexibility).

    Returns
    -------
    List of row dicts, each with a ``result`` key containing the SP response.
    """
    logger.info("Executing SP query")
    logger.debug(f"SQL: {query[:300]}...")

    # Block read-only callers from write SPs (catches NL-driven writes the HTTP
    # gate misses). No-op outside a request / when auth is off — see write_guard.
    from app.core.write_guard import (WritePermissionError, customer_scope,
                                      guard_query, readonly_channel)

    # Verified-customer channel: FAIL CLOSED. SPs are CRM-wide by construction
    # and cannot be row-scoped from here, so a customer-scoped context gets no
    # SP access at all — its answers come only from the explicitly
    # account-scoped queries in voice_support (see write_guard.customer_scope).
    if customer_scope() is not None:
        raise WritePermissionError(
            "Customer-verified channel: general CRM queries are not available "
            "here — only your own account's information can be looked up.",
            http_status=403)

    guard_query(query)
    _chan = readonly_channel()

    try:
        conn = get_connection()
        try:
            # A read-only channel (public SMS) runs inside a PostgreSQL
            # read-only transaction, so any write inside the SP is refused by
            # the database itself. guard_query's mode blocklist is incomplete
            # by construction; this does not depend on the mode's name.
            if _chan:
                conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()

                results = []
                for row in rows:
                    row_dict = dict(row)
                    # Parse any JSONB / string columns into Python dicts
                    for key in row_dict:
                        val = row_dict[key]
                        if isinstance(val, str):
                            try:
                                row_dict[key] = json.loads(val)
                            except (json.JSONDecodeError, TypeError):
                                pass
                    results.append(row_dict)

                conn.commit()
                logger.info(f"SP executed successfully — {len(results)} rows returned")
                return results

        finally:
            conn.close()

    except psycopg2.errors.ReadOnlySqlTransaction as e:
        # The SP tried to write on a read-only channel. Nothing was written —
        # PostgreSQL rejected it — so surface it as the permission error the
        # agents already know how to re-raise past their -500 handler.
        logger.warning(f"[write_guard] {_chan} channel blocked a write: {e}")
        raise WritePermissionError(
            f"Read-only channel ({_chan}): create, update and delete are not "
            "permitted here.", http_status=403) from e
    except psycopg2.Error as e:
        logger.error(f"Database error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error executing SP: {e}")
        raise


# ── Health check ──────────────────────────────────────────────────────────────

def test_connection() -> bool:
    """Verify the DB is reachable.  Returns True on success."""
    try:
        conn = get_connection()
        conn.close()
        logger.info("Database connection test successful")
        return True
    except Exception as e:
        logger.error(f"Database connection test failed: {e}")
        return False
