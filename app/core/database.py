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
import os
import threading
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor, register_uuid

from .config import get_settings

logger = logging.getLogger(__name__)

# Teach psycopg2 the uuid type ONCE, process-wide.
#
# Without this, a `uuid[]` column comes back as the literal string '{...}' —
# not a list. Iterating it yields CHARACTERS, so an empty array reads as
# ['{', '}'], which is truthy. That produced three separate live bugs before
# the cause was found:
#   customer_memories.contradicts  every memory falsely reported "contradicted
#                                  by another memory" — a flag firing on
#                                  everything, which trains readers to ignore it
#   agent_utterances.memory_ids    point-in-time reconstruction crashed with
#                                  'invalid input syntax for type uuid: "{"'
#   _link_contradictions           cross-linking wrote character fragments
#
# Each was patched at the call site with an explicit ::text cast. Three ad-hoc
# fixes for one missing registration is a systemic problem, so it is fixed here
# instead. The per-site casts remain valid and harmless.
register_uuid()


# ── Connection factory ────────────────────────────────────────────────────────

class _Pooled:
    """A pooled connection that behaves exactly like a raw one.

    `close()` returns it to the pool instead of closing it, so the ~100 existing
    `conn = get_connection() ... conn.close()` call sites are unchanged. That is
    the whole point: connection lifetime was managed correctly everywhere
    already — only the factory was wrong.

    `reset()` before return is what makes reuse safe: it rolls back, and clears
    autocommit, isolation level and the read-only flag that `execute_sp` sets
    for read-only channels. Returning a connection that is still marked
    read-only would fail the next writer for no visible reason.
    """

    __slots__ = ("_conn", "_pool", "_done", "_slot")

    def __init__(self, conn, pool, slot=None):
        self._conn, self._pool, self._done = conn, pool, False
        # The semaphore slot this checkout holds. Released exactly once, in
        # close(), whatever else happens — a leaked slot is a permanent
        # reduction in pool capacity and looks like a slow leak in throughput.
        self._slot = slot

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in _Pooled.__slots__:
            object.__setattr__(self, name, value)
        else:                       # e.g. conn.autocommit = True
            setattr(self._conn, name, value)

    def close(self):
        if self._done:
            return
        self._done = True
        try:
            try:
                self._conn.reset()
            except Exception:       # poisoned — drop it, do not hand it on
                try:
                    self._pool.putconn(self._conn, close=True)
                except Exception:
                    pass
                return
            try:
                self._pool.putconn(self._conn)
            except Exception:
                pass
        finally:
            if self._slot is not None:
                try:
                    self._slot.release()
                except ValueError:
                    pass            # already released; never double-count


_POOLS: Dict[tuple, Any] = {}
_POOL_LOCK = threading.Lock()

POOL_ENABLED = os.getenv("DB_POOL_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("DB_POOL_MAX", "16"))

# How long a caller waits for a pooled connection before giving up.
#
# EXHAUSTION AND UNAVAILABILITY ARE DIFFERENT FAILURES, and the first version
# of this module treated them the same: any exception from getconn() fell
# through to a direct psycopg2.connect(). Measured at 32 concurrent against
# POOL_MAX=16, sixteen requests logged "pool unavailable, falling back direct"
# and opened their own connections — with no errors, which is precisely the
# problem. Under sustained load that turns a queueing limit into
# max_connections pressure on PostgreSQL, taking down every other service on
# the same database rather than slowing one endpoint.
#
# A pool that cannot be BUILT (bad DSN, server down at startup) still falls
# back, because refusing to start helps nobody. A pool that is merely BUSY now
# makes the caller wait, and then fail honestly.
POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "10"))

# Bounds concurrent checkouts. psycopg2's getconn() raises immediately when the
# pool is at maxconn rather than waiting, so the queueing has to live here.
_POOL_SLOTS: Dict[tuple, Any] = {}


class PoolExhausted(RuntimeError):
    """Every pooled connection is busy and the wait expired.

    Raised instead of silently opening an unpooled connection. A caller seeing
    this is a capacity signal; a database refusing all connections is an
    outage, and the whole point of the bound is to keep the first from becoming
    the second."""


def _pool_for(dsn: str, schema: str):
    """One pool per (dsn, schema). Created lazily, under a lock."""
    key = (dsn, schema)
    pool = _POOLS.get(key)
    if pool is not None:
        return pool
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            # Same application_name as the direct path below. There are TWO
            # connect sites in this module and almost every connection comes
            # from this one — patching only the fallback left every real
            # session anonymous in pg_stat_activity while the change looked
            # done.
            kw = {"application_name": (os.getenv("PG_APPLICATION_NAME")
                                       or "conscestra-crm")[:63]}
            if schema and schema != "public":
                kw["options"] = f"-c search_path={schema},public"
            pool = psycopg2.pool.ThreadedConnectionPool(
                POOL_MIN, POOL_MAX, dsn, **kw)
            _POOLS[key] = pool
            _POOL_SLOTS[key] = threading.BoundedSemaphore(POOL_MAX)
    return pool


def pool_utilisation() -> Dict[str, Any]:
    """How close this PROCESS is to its connection ceiling.

    Pools are per process. `uvicorn --workers 4` means 4 x POOL_MAX against a
    server-wide `max_connections` that other services also draw on — the
    multiplication is easy to forget and expensive to discover, because
    exhaustion arrives as every service failing at once rather than this one
    slowing down.

    `in_use` is derived from the semaphore, which is the same thing the bound
    is enforced with, so this cannot drift from the limit it reports."""
    out: Dict[str, Any] = {"pool_max": POOL_MAX, "pools": len(_POOLS),
                           "timeout_s": POOL_TIMEOUT, "in_use": None}
    slots = list(_POOL_SLOTS.values())
    if slots:
        # BoundedSemaphore has no public counter; _value is the free count.
        free = sum(getattr(s, "_value", 0) for s in slots)
        capacity = POOL_MAX * len(slots)
        out["in_use"] = capacity - free
        out["utilisation"] = round((capacity - free) / capacity, 4) if capacity else None
        out["process_ceiling"] = capacity
        out["cluster_ceiling_hint"] = (
            f"x WEB_CONCURRENCY workers; check against the server's "
            f"max_connections before raising either")
    return out


def close_all_pools() -> None:
    """Release every pooled connection. For shutdown hooks and test teardown."""
    with _POOL_LOCK:
        for pool in _POOLS.values():
            try:
                pool.closeall()
            except Exception:
                pass
        _POOLS.clear()


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

    if POOL_ENABLED:
        # Measured before pooling: consolidation opened one connection per
        # entity at ~121 ms each — 3.4 hours for a 100k-contact pass, almost
        # all of it TCP + TLS + auth rather than work.
        # Building the pool and borrowing from it fail for different reasons
        # and must be handled differently — see POOL_TIMEOUT.
        try:
            pool = _pool_for(dsn, schema)
            slots = _POOL_SLOTS[(dsn, schema)]
        except Exception as exc:
            # Cannot be BUILT: bad DSN, server unreachable at startup. Falling
            # back keeps the app serving, and there is no pool to overwhelm.
            logger.warning(f"[db] pool unavailable, falling back direct: {exc}")
        else:
            # BUSY: wait for a slot rather than opening an unpooled connection.
            if not slots.acquire(timeout=POOL_TIMEOUT):
                raise PoolExhausted(
                    f"all {POOL_MAX} pooled connections busy for "
                    f"{POOL_TIMEOUT:g}s. Raise DB_POOL_MAX, shed load, or find "
                    f"the caller holding connections — opening more would move "
                    f"the failure onto the database itself.")
            try:
                # VALIDATE ON CHECKOUT. A pooled connection whose server has
                # restarted is not "closed" — psycopg2 only finds out when it
                # tries to use it. After a database restart or failover the pool
                # therefore hands out corpses, and each one fails a real user
                # request before being discarded on return: up to POOL_MAX
                # failures, multiplied by every worker. Observed in production
                # after a Postgres restart — /health reported
                # "SSL SYSCALL error: EOF detected" and recovered only once the
                # dead connections had each broken something.
                #
                # A round trip costs ~0.03 ms against a ~65 ms search. Paying it
                # to convert a user-visible error into a silent retry is not a
                # close call.
                raw = None
                for _ in range(max(2, POOL_MAX // 4)):
                    raw = pool.getconn()
                    try:
                        raw.set_client_encoding('UTF8')
                        with raw.cursor() as _c:
                            _c.execute("SELECT 1")
                        # END THE PROBE'S TRANSACTION.
                        #
                        # psycopg2 is not autocommit by default, so the probe
                        # leaves the connection IN a transaction. Roughly a
                        # hundred call sites then do `conn.autocommit = True`,
                        # which psycopg2 refuses inside one — the validation
                        # would hand back a connection that fails on its first
                        # real use, which is precisely the failure it exists to
                        # prevent.
                        raw.rollback()
                        break
                    except Exception:
                        # Dead. Drop it rather than return it to the pool, and
                        # take the next one.
                        try:
                            pool.putconn(raw, close=True)
                        except Exception:
                            pass
                        raw = None
                if raw is None:
                    raise PoolExhausted(
                        "every pooled connection failed validation — the "
                        "database is unreachable or was restarted and the pool "
                        "could not be refreshed")
                return _Pooled(raw, pool, slots)
            except Exception:
                slots.release()
                raise

    # application_name is forensics, not decoration.
    #
    # A breach tabletop on 2026-08-06 asked "who is connected as postgres right
    # now?" against production and got six sessions back. pgAdmin identified
    # itself, Railway's data browser tagged its queries, and the connections
    # opened by our own scripts showed `application_name` empty — which is
    # exactly what an intruder's session looks like. The one query an operator
    # runs first in an incident could not separate our tooling from an attacker.
    #
    # Costs nothing and is visible in pg_stat_activity for the life of the
    # session. APP_NAME overrides it so a script can say what it is.
    app_name = (os.getenv("PG_APPLICATION_NAME") or "conscestra-crm")[:63]
    if schema and schema != "public":
        conn = psycopg2.connect(dsn, application_name=app_name,
                                options=f"-c search_path={schema},public")
    else:
        conn = psycopg2.connect(dsn, application_name=app_name)
    conn.set_client_encoding('UTF8')
    return conn


def ensure_table(cur, table: str, ddl: str) -> bool:
    """Create a table lazily, tolerating a role that is not allowed to.

    Three modules create their tables at first use with
    `CREATE TABLE IF NOT EXISTS`. Under the non-superuser `crm_app` role that
    statement fails even when the table ALREADY EXISTS — PostgreSQL checks
    CREATE permission on the schema before the IF NOT EXISTS short-circuit, so
    the answer is `permission denied for schema public`, not a quiet no-op.

    Worse, a failed statement POISONS the surrounding transaction: every
    subsequent query in the same block fails too, so a startup helper that
    "just tries" would take the whole request down with it.

    A SAVEPOINT contains the failure. If the table is already there — the normal
    case, because migrations now declare these — the caller proceeds. If it is
    genuinely missing and we cannot create it, that is a real deployment fault
    and the error is raised rather than swallowed.
    """
    cur.execute("SAVEPOINT ensure_table")
    try:
        cur.execute(ddl)
        cur.execute("RELEASE SAVEPOINT ensure_table")
        return True
    except Exception as exc:                              # noqa: BLE001
        cur.execute("ROLLBACK TO SAVEPOINT ensure_table")
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is not None:
            logger.debug(f"[db] {table} exists; lazy CREATE not permitted "
                         f"for this role ({str(exc).splitlines()[0][:80]})")
            return False
        raise


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
