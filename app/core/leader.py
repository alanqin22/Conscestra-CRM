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
from typing import Callable, List
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


# Callbacks run when a FOLLOWER later becomes leader. Registered by whoever
# owns the background singletons — leader.py deliberately knows nothing about
# schedulers or pollers.
_promote_callbacks: List[Callable[[], None]] = []
# How often a follower re-asks for the lock. Sets the recovery-time objective
# after a leader dies: RTO <= this. 10s meets the stated <=10s objective at a
# cost of one cheap query per follower per interval.
WATCH_INTERVAL = float(os.getenv("HA_WATCH_INTERVAL", "10"))


def on_promotion(fn: Callable[[], None]) -> None:
    """Run `fn` if this process is later promoted to leader.

    Registering after promotion has already happened runs it immediately, so a
    caller never has to reason about ordering."""
    if _state.get("leader"):
        fn()
        return
    _promote_callbacks.append(fn)


def _watch_for_promotion() -> None:
    """A follower keeps asking for the lock, forever.

    WHY THIS EXISTS. The leader held the lock for process life and nothing ever
    asked again. Kill the leader and the lock is released, every survivor stays
    a follower, and the scheduler, IMAP poller and agent-bus stop — with HTTP
    still healthy on every node, so nothing alerts. MEASURED in a chaos test:
    3 workers, leader killed, 0 survivors claimed leadership after 18 seconds.

    The startup retry in begin() closes the ROLLING DEPLOY race. This closes
    the LEADER DEATH case, which the retry cannot: by then the process has been
    a follower for hours.
    """
    global _hold_conn
    from app.core.database import get_connection
    while True:
        time.sleep(WATCH_INTERVAL)
        if _state.get("leader"):
            return
        try:
            conn = get_connection()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
                won = bool(cur.fetchone()[0])
            if not won:
                conn.close()
                continue
            _hold_conn = conn
            _state["leader"] = True
            logger.warning(
                f"[HA] PROMOTED to leader ({_WHO}) — the previous holder is "
                f"gone. Starting background singletons.")
            for fn in list(_promote_callbacks):
                try:
                    fn()
                except Exception as exc:                      # noqa: BLE001
                    logger.error(f"[HA] promotion callback failed: {exc}",
                                 exc_info=True)
            return
        except Exception as exc:                              # noqa: BLE001
            logger.debug(f"[HA] promotion watch: {exc}")


_demote_callbacks: List[Callable[[], None]] = []


def on_demotion(fn: Callable[[], None]) -> None:
    """Run `fn` if this process stops being leader. Registered by whoever owns
    the background singletons, so they can be stopped."""
    _demote_callbacks.append(fn)


def _holds_lock() -> bool:
    """Does THIS process's hold-connection still own the advisory lock?

    Asks the database rather than memory. `_state['leader']` records what we
    decided at startup; it is not evidence about now."""
    if _hold_conn is None:
        return False
    try:
        with _hold_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                        "AND objid = %s AND pid = pg_backend_pid()", (_LOCK_KEY,))
            return cur.fetchone()[0] > 0
    except Exception:                                         # noqa: BLE001
        return False        # a dead connection holds nothing


def _watch_lock_retention() -> None:
    """A leader keeps PROVING it is the leader.

    FOUND IN PRODUCTION 2026-08-05. /health reported role=leader and
    runs_singletons=true while `pg_locks` showed zero holders of the advisory
    key: the hold-connection had been dropped (idle reaping, a blip, a Postgres
    restart) and nothing re-checked. `_watch_for_promotion` cannot cover this —
    its first line returns when `_state['leader']` is true, because it was
    written for followers.

    With one replica the symptom is invisible: the process keeps running the
    singletons and the work still happens. The danger is the free lock. The next
    replica to start — a rolling deploy, a scale-up — acquires it and becomes a
    SECOND leader, and then dunning email goes out twice and meetings are
    double-booked. "At most one leader" was not violated yet; it was violated
    in waiting, which is worse, because nothing was wrong to look at.

    Re-acquire when the lock is merely lost. Step down when someone else holds
    it — two leaders is the outcome this exists to prevent, and staying is the
    one choice that guarantees it."""
    global _hold_conn
    from app.core.database import get_connection
    while True:
        time.sleep(WATCH_INTERVAL)
        if not _state.get("leader"):
            return
        if _holds_lock():
            _state["lock_verified_at"] = time.time()
            continue

        logger.error(f"[HA] LEADER WITHOUT LOCK ({_WHO}) — this process believes "
                     f"it runs the singletons but no longer holds advisory lock "
                     f"{_LOCK_KEY}. Re-acquiring.")
        try:
            if _hold_conn is not None:
                try:
                    _hold_conn.close()
                except Exception:                             # noqa: BLE001
                    pass
                _hold_conn = None
            conn = get_connection()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
                won = bool(cur.fetchone()[0])
            if won:
                _hold_conn = conn
                _state["lock_verified_at"] = time.time()
                logger.warning(f"[HA] lock re-acquired by {_WHO}; leadership "
                               f"is intact and no longer claimed on trust")
                continue
            conn.close()
            # Someone else holds it. Two leaders is the failure mode; step down.
            _state["leader"] = False
            logger.error(f"[HA] DEMOTED ({_WHO}) — another process now holds "
                         f"lock {_LOCK_KEY}. Stopping background singletons to "
                         f"avoid duplicate scheduled work.")
            for fn in list(_demote_callbacks):
                try:
                    fn()
                except Exception as exc:                      # noqa: BLE001
                    logger.error(f"[HA] demotion callback failed: {exc}",
                                 exc_info=True)
            threading.Thread(target=_watch_for_promotion, daemon=True,
                             name="ha-promotion-watch").start()
            return
        except Exception as exc:                              # noqa: BLE001
            logger.error(f"[HA] lock re-acquisition failed: {exc}")


def status() -> dict:
    """What /health should report. `lock_held` is asked of the database.

    The old payload reported `runs_singletons` from memory, which is what let a
    leader with no lock look perfectly healthy for hours."""
    verified = _state.get("lock_verified_at")
    return {
        "role": role(),
        "runs_singletons": is_leader(),
        "lock_held": _holds_lock() if _state.get("leader") else None,
        "lock_verified_secs_ago": (round(time.time() - verified, 1)
                                   if verified else None),
        "lock_key": _LOCK_KEY,
    }


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
                _state["lock_verified_at"] = time.time()
                # Holding a connection is not the same as holding the lock.
                # Verify it for as long as we claim to be leader.
                threading.Thread(target=_watch_lock_retention, daemon=True,
                                 name="ha-lock-retention").start()
                logger.info(f"[HA] LEADER ({_WHO}) — running scheduler / IMAP poller / agent-bus")
            else:
                _state["leader"] = False
                threading.Thread(target=_watch_for_promotion, daemon=True,
                                 name="ha-promotion-watch").start()
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
