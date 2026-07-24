"""HA leader election for background singletons (blindspot #7).

The app runs three IN-PROCESS background singletons: the APScheduler daily jobs,
the IMAP auto-reply poller, and the agent-bus consumer. On a SINGLE process
that's fine. But horizontal scaling — the only way to add request throughput —
means running several web workers or replicas, and then EACH one would fire the
scheduler, poll the mailbox, and drain the event bus: duplicate dunning emails,
double-booked meetings, racing writes.

Leader election makes that safe with a Postgres SESSION-LEVEL advisory lock:
exactly one process cluster-wide holds it and runs the singletons; every other
process serves HTTP only. The lock rides a dedicated connection held open for the
process's life; if the leader dies, its session ends and Postgres releases the
lock automatically, so a restarted/again-elected process can take over. No new
infrastructure — the same Postgres the app already uses.

This module intentionally does the SIMPLE, safe thing: a synchronous election at
startup. Zero-gap automatic failover (a live follower promoting itself the moment
the leader dies, without a restart) is a documented follow-up — it needs the
singleton-start logic refactored into a restartable callback.

CONFIG (env)
  HA_LEADER_ELECTION  1       0 → always leader (single-process dev; unchanged behavior)
  HA_LOCK_KEY         871123   advisory-lock key (any bigint, shared cluster-wide)
"""

from __future__ import annotations

import logging
import os
import socket
import threading

logger = logging.getLogger("leader")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("HA_LEADER_ELECTION", "1")
try:
    _LOCK_KEY = int(os.getenv("HA_LOCK_KEY", "871123"))
except ValueError:
    _LOCK_KEY = 871123

_state = {"leader": False, "elected": False}
_hold_conn = None            # dedicated connection holding the advisory lock
_lock = threading.Lock()
_WHO = f"{socket.gethostname()}:{os.getpid()}"


def begin() -> bool:
    """Elect this process. Returns True if it is the cluster leader (should run
    the background singletons), False if it is a follower (HTTP only). Idempotent;
    safe if the DB is briefly unreachable (fails OPEN to leader so a single-process
    deploy never silently stops its schedulers)."""
    global _hold_conn
    with _lock:
        if _state["elected"]:
            return _state["leader"]
        _state["elected"] = True
        if not ENABLED:
            _state["leader"] = True
            logger.info("[HA] leader election disabled — this process runs singletons (standalone)")
            return True
        try:
            from app.core.database import get_connection
            conn = get_connection()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
                won = bool(cur.fetchone()[0])
            if won:
                _hold_conn = conn          # keep open → hold the lock for process life
                _state["leader"] = True
                logger.info(f"[HA] LEADER ({_WHO}) — running scheduler / IMAP poller / agent-bus")
            else:
                conn.close()
                _state["leader"] = False
                logger.info(f"[HA] follower ({_WHO}) — another process holds the singleton lock")
            return _state["leader"]
        except Exception as exc:
            # DB not reachable at election time: fail OPEN (leader) so a normal
            # single-process deploy never silently loses its background jobs.
            logger.warning(f"[HA] election failed ({exc}); assuming leader (fail-open)")
            _state["leader"] = True
            return True


def is_leader() -> bool:
    return _state["leader"]


def role() -> str:
    if not ENABLED:
        return "standalone"
    if not _state["elected"]:
        return "unelected"
    return "leader" if _state["leader"] else "follower"


def release() -> None:
    """Release the advisory lock on shutdown (also released automatically when the
    connection/session ends)."""
    global _hold_conn
    with _lock:
        if _hold_conn is not None:
            try:
                with _hold_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
                _hold_conn.close()
            except Exception:
                pass
            _hold_conn = None
        _state["leader"] = False
