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
import time
import threading

logger = logging.getLogger("leader")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("HA_LEADER_ELECTION", "1")

# How long a losing process keeps trying before accepting follower for life.
# Sized for a rolling deploy: the outgoing container releases its lock when it
# exits, typically within seconds. Long enough to outlast that, short enough
# not to delay startup on a genuine multi-instance deployment.
ELECTION_RETRY_SECONDS = float(os.getenv("HA_ELECTION_RETRY_SECONDS", "45"))
ELECTION_RETRY_INTERVAL = float(os.getenv("HA_ELECTION_RETRY_INTERVAL", "3"))
try:
    _LOCK_KEY = int(os.getenv("HA_LOCK_KEY", "871123"))
except ValueError:
    _LOCK_KEY = 871123

_state = {"leader": False, "elected": False}
_hold_conn = None            # dedicated connection holding the advisory lock
_lock = threading.Lock()
_WHO = f"{socket.gethostname()}:{os.getpid()}"


def _worker_count() -> int:
    """How many application processes are expected to exist.

    WEB_CONCURRENCY is the variable both uvicorn and gunicorn read, and Railway
    sets it. Unset means one process, which is the deployment this code was
    written for and the one where failing open is right."""
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        try:
            n = int(os.getenv(var, "") or 0)
            if n > 0:
                return n
        except ValueError:
            continue
    return 1


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
            if not won:
                # THE DEPLOY RACE THAT SILENTLY STOPPED BACKGROUND WORK FOR 10 DAYS.
                #
                # During a rolling deploy the NEW container starts while the OLD
                # one still holds the lock. A single try loses, this process
                # accepts follower for life, the old container then exits, and
                # the lock is orphaned — nobody runs the scheduler, IMAP poller
                # or agent-bus again until someone restarts by hand. Measured on
                # production: last scheduled run 2026-07-24, discovered 2026-08-04.
                #
                # The old container leaves within seconds, so retrying briefly
                # converts the race into a non-event. This does NOT cover a
                # leader that dies later in life — that needs promotion of a
                # running follower, which is a separate change.
                conn.close()
                deadline = time.monotonic() + ELECTION_RETRY_SECONDS
                attempt = 0
                while time.monotonic() < deadline:
                    time.sleep(ELECTION_RETRY_INTERVAL)
                    attempt += 1
                    try:
                        conn = get_connection()
                        conn.autocommit = True
                        with conn.cursor() as cur:
                            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
                            won = bool(cur.fetchone()[0])
                        if won:
                            logger.info(f"[HA] acquired leadership on retry "
                                        f"{attempt} after {_WHO} lost the first "
                                        f"attempt — the previous holder exited")
                            break
                        conn.close()
                    except Exception as exc:                  # noqa: BLE001
                        logger.debug(f"[HA] retry {attempt} failed: {exc}")

            if won:
                _hold_conn = conn          # keep open → hold the lock for process life
                _state["leader"] = True
                logger.info(f"[HA] LEADER ({_WHO}) — running scheduler / IMAP poller / agent-bus")
            else:
                _state["leader"] = False
                logger.warning(
                    f"[HA] follower ({_WHO}) — another process held the singleton "
                    f"lock for the whole {ELECTION_RETRY_SECONDS:g}s election "
                    f"window. If this is a single-instance deploy, background "
                    f"work is NOT running: check for an orphaned holder of "
                    f"advisory lock {_LOCK_KEY}.")
            return _state["leader"]
        except Exception as exc:
            # FAIL-OPEN IS CORRECT FOR ONE PROCESS AND DANGEROUS FOR SEVERAL.
            #
            # A single-process deploy that cannot reach the database at startup
            # should still run its schedulers once the database returns —
            # silently losing every background job is the worse failure.
            #
            # Under `uvicorn --workers N` that reasoning inverts. MEASURED: with
            # the database unreachable, four processes each assumed leadership —
            # four schedulers, four IMAP pollers, four agent-bus consumers.
            # That is duplicate dunning emails, duplicate reminders and
            # duplicate consolidations, caused by a transient blip during a
            # rolling restart when all workers elect at once.
            #
            # So the tie-break depends on how many processes exist. WEB_CONCURRENCY
            # is the variable uvicorn and gunicorn both honour; when it says more
            # than one, an unreachable database means FOLLOWER. A cluster that
            # briefly runs no background jobs recovers on the next restart. A
            # cluster that sends every email four times does not.
            multi = _worker_count() > 1
            _state["leader"] = not multi
            logger.warning(
                f"[HA] election failed ({exc}); "
                + (f"assuming FOLLOWER — WEB_CONCURRENCY={_worker_count()} means "
                   f"several processes would each fail open and duplicate every "
                   f"background job"
                   if multi else
                   "assuming leader (fail-open, single process)"))
            return _state["leader"]


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
