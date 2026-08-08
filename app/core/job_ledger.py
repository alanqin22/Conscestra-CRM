"""Record which scheduled jobs ran, and re-run the ones a restart skipped.

WHY THIS EXISTS
---------------
APScheduler is configured here with the default in-memory jobstore. Every
process start recomputes each job's next_run_time from "now", so a fire time
that elapsed while the process was down is simply never run. `misfire_grace_time`
does not help: it forgives a LIVE scheduler that ran late, not a scheduler that
was not alive.

Nothing reports that. After the restart /health says leader, running, 32 jobs,
last_tick fresh — all true, and all compatible with the night's work never
having happened. Railway restarted this service at 03:30 UTC on two consecutive
nights; the nightly batch runs 21:45–23:45, and a restart landing there would
have skipped it silently.

WHAT IT DOES
------------
  instrument()   wraps every job's callable to write an outcome row
  catch_up()     at startup, compares each cron job's last successful run to its
                 previous fire time and re-runs what was missed
  audit()        the same comparison without running anything — surfaced on
                 /health so a job that quietly stopped firing is visible

WHY NOT A PERSISTENT APSCHEDULER JOBSTORE
-----------------------------------------
The obvious alternative is SQLAlchemyJobStore, which stores next_run_time and
would let APScheduler handle misfires itself. Two things rule it out here. The
schedules are defined in code and added with replace_existing=True, which
rewrites next_run_time from now on every boot — the stored overdue time is
destroyed by the very call that would need it. And under HA leader election the
FOLLOWERS also build the scheduler (so they can start it on promotion), which
with a shared store means several processes mutating one set of job rows.

This ledger is additive instead: APScheduler keeps owning the schedule, and the
ledger owns the question of whether the work happened.

NO RUNTIME DDL
--------------
crm_app has USAGE but not CREATE on public. Every function here degrades to a
no-op and logs once if `scheduled_job_runs` is absent — see sql/job_ledger.sql.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("job_ledger")

# How far back a missed fire time may be and still be worth running. A nightly
# job missed by two hours should run; one missed by three days should not — its
# window has passed and running it now would act on a stale view of the world.
CATCHUP_GRACE_MIN = float(os.getenv("JOB_CATCHUP_GRACE_MIN", "360"))    # 6h

# A cap, not a schedule. If a dozen jobs are overdue, the cause is systemic
# (a long outage) and firing all of them at once turns a recovery into a
# thundering herd against the same database the app is trying to serve from.
CATCHUP_MAX_JOBS = int(os.getenv("JOB_CATCHUP_MAX_JOBS", "6"))

# Seconds between catch-up runs, so they queue behind each other rather than
# landing together.
CATCHUP_SPACING_S = float(os.getenv("JOB_CATCHUP_SPACING_S", "20"))

PRUNE_DAYS = int(os.getenv("JOB_LEDGER_PRUNE_DAYS", "90"))
PRUNE_MAX = int(os.getenv("JOB_LEDGER_PRUNE_MAX", "20000"))

_HOST = socket.gethostname()[:60]

# Set once when the table turns out to be missing, so the warning is logged a
# single time instead of on every job execution.
_disabled = False
_disabled_reason = ""
_disabled_until = 0.0

# How long the "table is missing" latch holds before the next write re-probes.
DISABLED_RETRY_S = float(os.getenv("JOB_LEDGER_RETRY_S", "900"))   # 15 min

# Last audit result, published on /health.
_last_audit: Dict[str, Any] = {}
_audit_lock = threading.Lock()

# The UNWRAPPED callables, kept by instrument(). Catch-up must invoke these
# rather than job.func: after instrumentation job.func records an 'ok' row of
# its own, so a caught-up job wrote BOTH 'ok' and 'caught_up' — and the 'ok'
# claims the run happened on schedule, which is the one thing the ledger exists
# to disprove. Measured before the fix: one catch-up, three rows.
_originals: Dict[str, Callable[[], Any]] = {}

# Catch-up is single-flight. A process demoted and re-promoted twice would
# otherwise have two threads replaying the same jobs against each other.
_catchup_lock = threading.Lock()


def _table_missing(exc: Exception) -> bool:
    """psycopg2 raises UndefinedTable for a missing relation."""
    return getattr(exc, "pgcode", None) == "42P01" or \
        "scheduled_job_runs" in str(exc) and "does not exist" in str(exc)


def _disable(reason: str) -> None:
    global _disabled, _disabled_reason, _disabled_until
    if not _disabled:
        logger.error(
            "[JobLedger] DISABLED — %s. Scheduled-job outcomes are not being "
            "recorded and a restart will silently skip whatever was due while "
            "the process was down. Apply sql/job_ledger.sql.", reason)
    _disabled, _disabled_reason = True, reason
    _disabled_until = time.monotonic() + DISABLED_RETRY_S


def available() -> bool:
    """Whether the ledger is usable, RE-PROBING periodically.

    The latch used to be permanent: apply the migration to a running service
    and the ledger stayed off until someone restarted it — so the deploy order
    that is meant to be forgiving (migration first, code second) had a
    silently unforgiving reverse. Expiring the latch lets the next write find
    the table and heal without a restart.
    """
    global _disabled
    if _disabled and time.monotonic() >= _disabled_until:
        _disabled = False           # optimistic re-probe; a failure re-latches
        logger.info("[JobLedger] re-probing after %s", _disabled_reason)
    return not _disabled


# Personal data must not accumulate here. `detail` carries exception text, and
# an exception from a mail or contact job can quote an address or a phone
# number verbatim — putting personal data into a table DSAR does not export
# (it has no subject column, correctly) and retention did not govern. Scrubbed
# on the way IN, because a scrub applied on the way out leaves the original
# sitting in the database, which is the part that matters.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Phone matching is deliberately conservative, because over-redaction here is
# not a safe default — it destroys the identifiers that are the entire reason
# to keep `detail`. A first attempt turned "SO-2026-101730" into "SO-[phone]",
# i.e. it scrubbed away exactly the fact a reader needs.
#
# Two guards do the work: the candidate may not be glued to a word character
# or hyphen (which excludes SO-/INV- document numbers and the tail of a UUID),
# and the DIGIT COUNT must fall in the range a real phone number occupies.
# ISO timestamps survive both — `2026-08-06` is eight digits — and catch-up
# details are full of them.
# Bare spaces are NOT separators. Allowing them let a candidate run from a date
# into the following time — "2026-08-01 00:30:00" reached ten digits and was
# redacted — and the telephony code emits E.164 (`+14165550123`) anyway, so the
# space bought nothing. Bracketed North American form gets its own alternative
# because it cannot survive without one.
_PHONE_CAND = re.compile(
    r"(?<![\w-])(?:"
    r"\(\d{3}\)[\s.-]?\d{3}[\s.-]?\d{4}"      # (416) 555-0123
    r"|\+?\d[\d\-.()]{7,}\d"                   # +14165550123 / 416-555-0123
    r")(?![\w-])")


def _phone_sub(m: "re.Match[str]") -> str:
    digits = sum(c.isdigit() for c in m.group(0))
    return "[phone]" if 10 <= digits <= 15 else m.group(0)


def scrub(text: Optional[str]) -> Optional[str]:
    """Redact personal data from a ledger `detail` before it is stored."""
    if not text:
        return text
    t = _EMAIL_RE.sub("[email]", str(text))
    t = _PHONE_CAND.sub(_phone_sub, t)
    return t


# ── writing ──────────────────────────────────────────────────────────────────

def record(job_id: str, status: str, started: _dt.datetime,
           detail: Optional[str] = None) -> None:
    """Append one outcome row. Never raises — a ledger that can break the job
    it is observing is worse than no ledger."""
    if not available():
        return
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scheduled_job_runs "
                    "  (job_id, started_at, finished_at, status, detail, host) "
                    "VALUES (%s, %s, now(), %s, %s, %s)",
                    (job_id, started, status,
                     scrub(str(detail)[:500]) if detail else None, _HOST))
            conn.commit()
        finally:
            conn.close()          # returns it to the pool; NOT a contextmanager
    except Exception as exc:                                    # noqa: BLE001
        if _table_missing(exc):
            _disable("table scheduled_job_runs does not exist")
        else:
            logger.warning("[JobLedger] could not record %s/%s: %s",
                           job_id, status, str(exc).splitlines()[0][:160])


def _last_success_map() -> Dict[str, _dt.datetime]:
    """One query for every job, rather than one query per job."""
    if not available():
        return {}
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT ON (job_id) job_id, started_at "
                    "FROM scheduled_job_runs "
                    "WHERE status IN ('ok', 'caught_up', 'baseline') "
                    "ORDER BY job_id, started_at DESC")
                return {r[0]: r[1] for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception as exc:                                    # noqa: BLE001
        if _table_missing(exc):
            _disable("table scheduled_job_runs does not exist")
        else:
            logger.warning("[JobLedger] last-success query failed: %s",
                           str(exc).splitlines()[0][:160])
        return {}


def _recent_notable(hours: int = 24) -> Dict[str, List[Dict[str, Any]]]:
    """Repairs and failures from the last day.

    Without this an overnight miss that catch_up quietly fixed leaves no trace
    a morning observer can see: `overdue` is empty again precisely BECAUSE the
    repair worked. Self-healing that reports nothing is indistinguishable from
    nothing having happened, and the difference matters — a job being caught up
    every night means something is restarting every night.
    """
    out: Dict[str, List[Dict[str, Any]]] = {"repairs": [], "failures": []}
    if _disabled:
        return out
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT job_id, status, started_at, detail "
                    "FROM scheduled_job_runs "
                    "WHERE status IN ('caught_up','catch_up_failed','error') "
                    "  AND started_at > now() - make_interval(hours => %s) "
                    "ORDER BY started_at DESC LIMIT 25", (hours,))
                for job_id, status, started, detail in cur.fetchall():
                    row = {"job": job_id, "status": status,
                           "at": started.isoformat(timespec="seconds")}
                    if detail:
                        row["detail"] = str(detail)[:160]
                    (out["repairs"] if status == "caught_up"
                     else out["failures"]).append(row)
        finally:
            conn.close()
    except Exception as exc:                                    # noqa: BLE001
        if _table_missing(exc):
            _disable("table scheduled_job_runs does not exist")
    return out


def prune() -> int:
    if _disabled:
        return 0
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # BOUNDED, matching the house pattern in retention.py. An
                # unbounded DELETE on a table nobody has looked at for a year
                # takes a long lock on the path that runs at startup, i.e. at
                # exactly the moment the process is least able to afford one.
                cur.execute(
                    "DELETE FROM scheduled_job_runs WHERE ctid IN ("
                    "  SELECT ctid FROM scheduled_job_runs "
                    "   WHERE started_at < now() - make_interval(days => %s) "
                    "   LIMIT %s)",
                    (PRUNE_DAYS, PRUNE_MAX))
                n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return n or 0
    except Exception:                                           # noqa: BLE001
        return 0


# ── instrumenting the scheduler ──────────────────────────────────────────────

def _wrap(job_id: str, fn: Callable[[], Any]) -> Callable[[], Any]:
    def _runner() -> Any:
        started = _dt.datetime.now(_dt.timezone.utc)
        try:
            out = fn()
        except Exception as exc:                                # noqa: BLE001
            # Re-raise: APScheduler's own error listener still needs to see it,
            # and app.state.scheduler_last_error is fed from that event.
            record(job_id, "error", started,
                   f"{type(exc).__name__}: {exc}")
            raise
        record(job_id, "ok", started)
        return out
    _runner.__name__ = getattr(fn, "__name__", job_id)
    _runner.__doc__ = getattr(fn, "__doc__", None)
    return _runner


def instrument(scheduler) -> int:
    """Replace every job's callable with one that records its outcome.

    Done in one pass over the built scheduler rather than at each of the 32
    add_job call sites: a rule enforced in one place cannot be forgotten by the
    33rd job, and the schedule definitions stay readable.
    """
    if _disabled:
        return 0
    n = 0
    for job in scheduler.get_jobs():
        try:
            if job.id in _originals:
                continue                    # already wrapped; never double-wrap
            _originals[job.id] = job.func
            scheduler.modify_job(job.id, func=_wrap(job.id, job.func))
            n += 1
        except Exception as exc:                                # noqa: BLE001
            logger.warning("[JobLedger] could not instrument %s: %s",
                           job.id, str(exc).splitlines()[0][:120])
    logger.info("[JobLedger] instrumented %d job(s)", n)
    return n


# ── did it run? ──────────────────────────────────────────────────────────────

def previous_fire_time(trigger, now: _dt.datetime,
                       lookback_hours: int = 960) -> Optional[_dt.datetime]:
    """The most recent fire time at or before `now`.

    APScheduler triggers only walk forwards, so this steps forward from a bound
    in the past and keeps the last value that is still <= now. Bounded by
    iteration count as well as by time, so a misconfigured seconds-level trigger
    cannot spin here.

    The 40-day default is what makes MONTHLY and WEEKLY jobs auditable: with a
    3-day lookback `capture_forecast_snapshot` (1st of the month) and the Monday
    passes have no previous fire time in range, come back None, and are silently
    treated as never-due — the exact blind spot this module exists to remove.
    Hitting the iteration cap fails safe: `prev` ends up too EARLY, which can
    only mask an overdue job, never invent one.
    """
    t = now - _dt.timedelta(hours=lookback_hours)
    prev = None
    for _ in range(5000):
        try:
            nxt = trigger.get_next_fire_time(None, t)
        except Exception:                                       # noqa: BLE001
            return None
        if nxt is None or nxt > now:
            break
        prev = nxt
        t = nxt + _dt.timedelta(seconds=1)
    return prev


def _demoted() -> bool:
    """True only when this process AFFIRMATIVELY knows it is a follower.

    Deliberately not `not is_leader()`: that is also true before the election
    has run and in tests that never elect, and refusing to repair a missed job
    because leadership is merely unknown would reintroduce the silence.
    """
    try:
        from app.core import leader
        return leader.role() == "follower"
    except Exception:                                           # noqa: BLE001
        return False


def _cron_jobs(scheduler) -> List[Any]:
    """Only calendar jobs are catch-up candidates.

    An IntervalTrigger job that was missed will fire again within its own
    interval — 20 minutes for the content index, an hour for memory distillation
    — so re-running it at boot buys nothing and risks doubling the work.
    """
    from apscheduler.triggers.cron import CronTrigger
    return [j for j in scheduler.get_jobs()
            if isinstance(j.trigger, CronTrigger)]


def audit(scheduler) -> Dict[str, Any]:
    """Which cron jobs have not run for their most recent scheduled time.

    Read-only. This is the check that would have caught the original silent
    stall: it asks whether the WORK happened, where every other signal only
    reports that the machinery exists.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    out: Dict[str, Any] = {"checked_at": now.isoformat(timespec="seconds"),
                           "overdue": [], "unknown": [], "ledger": "ok"}
    if _disabled:
        out["ledger"] = _disabled_reason or "disabled"
        return out

    last = _last_success_map()
    for job in _cron_jobs(scheduler):
        prev = previous_fire_time(job.trigger, now)
        if prev is None:
            continue                       # never due yet (e.g. monthly, day 1)
        seen = last.get(job.id)
        if seen is None:
            out["unknown"].append(job.id)
            continue
        if seen < prev:
            out["overdue"].append({
                "job": job.id,
                "due": prev.isoformat(timespec="seconds"),
                "last_ran": seen.isoformat(timespec="seconds"),
                "late_min": round((now - prev).total_seconds() / 60.0, 1),
            })
    out["overdue_count"] = len(out["overdue"])
    out.update(_recent_notable())
    with _audit_lock:
        _last_audit.clear()
        _last_audit.update(out)
    return out


def last_audit() -> Dict[str, Any]:
    with _audit_lock:
        return dict(_last_audit)


# ── catch-up ─────────────────────────────────────────────────────────────────

def _catch_up_blocking(scheduler) -> Dict[str, Any]:
    now = _dt.datetime.now(_dt.timezone.utc)
    result: Dict[str, Any] = {"ran": [], "skipped": [], "seeded": [],
                              "too_old": []}
    if _disabled:
        return result

    last = _last_success_map()
    candidates = []
    for job in _cron_jobs(scheduler):
        prev = previous_fire_time(job.trigger, now)
        if prev is None:
            continue
        seen = last.get(job.id)
        if seen is None:
            # First sighting. Recording a baseline INSTEAD of running is the
            # important half: on a first deploy every job would otherwise look
            # missed and all 28 would fire at once.
            record(job.id, "baseline", now, "first sighting — not run")
            result["seeded"].append(job.id)
            continue
        if seen >= prev:
            continue
        late_min = (now - prev).total_seconds() / 60.0
        if late_min > CATCHUP_GRACE_MIN:
            result["too_old"].append({"job": job.id,
                                      "late_min": round(late_min, 1)})
            continue
        candidates.append((prev, job, late_min))

    # Oldest scheduled time first, so a catch-up batch replays in the order the
    # schedule intended (the 22:00 pipeline advance before the 22:15 seed).
    candidates.sort(key=lambda c: c[0])

    for i, (prev, job, late_min) in enumerate(candidates):
        if i >= CATCHUP_MAX_JOBS:
            result["skipped"].append(job.id)
            continue
        if i:
            time.sleep(CATCHUP_SPACING_S)
        # Re-checked EVERY iteration, not once at the top. The loop sleeps
        # between jobs, and a process can lose the advisory lock inside that
        # window — carrying on would run the nightly batch a second time
        # alongside the process that legitimately took over.
        if _demoted():
            result["skipped"].append(job.id)
            logger.error("[JobLedger] catch-up ABORTED at %s — this process is "
                         "no longer the leader", job.id)
            break
        logger.warning(
            "[JobLedger] catch-up: %s was due %s (%.0f min ago) and did not "
            "run — running it now", job.id, prev.isoformat(timespec="seconds"),
            late_min)
        started = _dt.datetime.now(_dt.timezone.utc)
        try:
            _originals.get(job.id, job.func)()
            record(job.id, "caught_up", started,
                   f"missed fire time {prev.isoformat(timespec='seconds')}")
            result["ran"].append(job.id)
        except Exception as exc:                                # noqa: BLE001
            record(job.id, "catch_up_failed", started,
                   f"{type(exc).__name__}: {exc}")
            logger.error("[JobLedger] catch-up of %s failed: %s", job.id, exc,
                         exc_info=True)

    if result["ran"] or result["skipped"] or result["too_old"]:
        logger.warning("[JobLedger] catch-up complete — ran=%s skipped=%s "
                       "too_old=%s", result["ran"], result["skipped"],
                       [t["job"] for t in result["too_old"]])
    else:
        logger.info("[JobLedger] catch-up: nothing was missed (%d job(s) "
                    "seeded)", len(result["seeded"]))
    prune()
    audit(scheduler)
    return result


def catch_up_async(scheduler) -> None:
    """Run the catch-up off the startup path.

    Startup must not block on it: a caught-up nightly batch can take minutes,
    and holding the lifespan open that long means the platform's health check
    fails and restarts the very process that is repairing the miss.

    LEADER ONLY — the caller is responsible for that, the same as for starting
    the scheduler at all.
    """
    def _guarded() -> None:
        if not _catchup_lock.acquire(blocking=False):
            logger.info("[JobLedger] catch-up already in progress — skipping")
            return
        try:
            _catch_up_blocking(scheduler)
        finally:
            _catchup_lock.release()

    t = threading.Thread(target=_guarded, name="job-ledger-catchup", daemon=True)
    t.start()
