"""Watch /health and email when it stops being true.

WHY THIS EXISTS
---------------
Background jobs stopped for ten days in July 2026 and nobody noticed, because
nothing about that failure is visible from outside: the API answered every
request correctly the whole time. Every runbook in docs/ opens with "check
/health", and until now nothing checked it.

WHY IT DOES NOT IMPORT THE APPLICATION
--------------------------------------
`app/agents/email/smtp_imap.py` already sends mail, and reusing it would be the
DRY choice everywhere else in this codebase. Not here. An alerting path that
imports the system it monitors shares that system's failure modes — a bad
migration, a broken import, an exhausted connection pool would take down the
thing meant to tell you about it. This talks SMTP directly and imports nothing
from `app/` except the .env loader.

WHAT IT CHECKS
--------------
  transport      /health answers at all, within the timeout
  http status    200 (the endpoint returns 503 when the database is unreachable)
  database       database.ok is true
  db role        database.connected_as is the expected app role, not a superuser
  leader         ha.role is not 'unelected' (an election completed at all)
  coverage       the answering process is AUTHORITATIVE about the scheduled
                 controls: it holds the state, its evidence QUERY SUCCEEDED,
                 and the resulting assessment is present and fresh
  scheduler      scheduler.last_tick is fresher than HEALTH_MAX_TICK_AGE_MIN

It does NOT verify that exactly one process claims to run the singletons. One
probe reaches one worker and cannot see the others; the earlier wording here
described a check that was never implemented.

The scheduler tick is the one that matters most and the one a generic uptime
service cannot see. `running: true` only means the object exists; a scheduler
that is running and never firing looks identical in every other field.

COVERAGE IS NOT THE SAME AS HEALTH
----------------------------------
Scheduler state lives in the LEADER's memory, so a follower answers 200 with
`scheduler: {running: null, jobs: 0, last_tick: null}` and nothing to assess.
Measured over 2,989 checks: 899 (30%) reached a follower, and 829 of those were
recorded HEALTHY on no scheduler evidence at all. One follower-only run lasted
3h23m and reported healthy throughout.

So this checker separates "I looked and the controls are fine" from "I could not
obtain evidence". The second is NOT healthy. It re-polls first, because 16% of
follower runs are isolated and a second probe reaches the leader; if authority
is still not obtained it says so as a problem.

AND AN EMPTY RESULT IS NOT THE SAME AS A SUCCESSFUL ONE. `_last_success_map()`
used to return {} when its query FAILED -- the same value a successful query
over an empty table returns -- so a database failure was published as
`overdue_jobs: []` with a fresh timestamp and satisfied every condition above.
The backend now publishes `evidence_complete`, and this checker requires it to
be exactly true.

NOISE CONTROL
-------------
Emails on TRANSITION (ok -> bad, bad -> ok) and once per HEALTH_REPEAT_HOURS
while a problem persists. A checker that mails every 15 minutes trains you to
filter it, which is the same as not having one.

    python -m scripts.health_watch                    # check once, email on change
    python -m scripts.health_watch --url https://x/health
    python -m scripts.health_watch --test-email       # prove the alert path works
    python -m scripts.health_watch --force            # email regardless of state
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import config as _config          # noqa: E402,F401  (loads .env only)

STATE = Path(os.getenv("HEALTH_STATE_FILE", str(ROOT / "backups" / "health_state.json")))
TIMEOUT = float(os.getenv("HEALTH_TIMEOUT_S", "20"))
MAX_TICK_AGE_MIN = float(os.getenv("HEALTH_MAX_TICK_AGE_MIN", "120"))
REPEAT_HOURS = float(os.getenv("HEALTH_REPEAT_HOURS", "12"))
EXPECT_ROLE = os.getenv("HEALTH_EXPECT_DB_ROLE", "crm_app").strip()

# Confirm before alerting. A single probe cannot distinguish "the service is
# down" from "the service is restarting", and this one has been paging for the
# latter: on 2026-08-07 it caught a 502 at 03:30:01 for a process that was
# serving again by 03:30:33 — a 32-second window. An alert that fires for
# something already fixed by the time you read it is how a monitor teaches you
# to ignore it.
CONFIRM_RETRIES = int(os.getenv("HEALTH_CONFIRM_RETRIES", "2"))
CONFIRM_DELAY_S = float(os.getenv("HEALTH_CONFIRM_DELAY_S", "45"))

# Blips are suppressed, not forgotten. Restarts that keep happening are a real
# problem even when each one self-heals in under a minute, so they are counted
# and reported once the rate stops looking like noise.
BLIP_WINDOW_HOURS = float(os.getenv("HEALTH_BLIP_WINDOW_HOURS", "24"))
BLIP_ALERT_COUNT = int(os.getenv("HEALTH_BLIP_ALERT_COUNT", "3"))

# This script only knows what happened while it was running. It is a scheduled
# task on a workstation that sleeps, so "no alert" has two meanings — nothing
# was wrong, or nobody looked. The gap between checks is the only thing that
# tells them apart, and it is reported rather than assumed away.
MAX_GAP_MIN = float(os.getenv("HEALTH_MAX_GAP_MIN", "45"))

# The overdue-job audit runs hourly; 150 min allows one missed pass plus slack
# before its cached answer is treated as unusable.
MAX_AUDIT_AGE_MIN = float(os.getenv("HEALTH_MAX_AUDIT_AGE_MIN", "150"))

# The newest backup that actually contains data, in hours.
#
# This check exists because its absence let a backup outage run for sixteen
# nights while this monitor reported healthy every thirty minutes. It was not
# wrong: backup freshness was simply outside everything it looked at. The
# nightly dump ran, failed at pg_dump, and left a dated zero-byte file, so the
# directory kept looking like a working backup from the outside.
#
# Size is the check, not presence. A zero-byte dump is the exact artifact a
# failed run produces, so a check for "a recent file" would have passed
# throughout the outage. The default allows one missed night, because the
# machine taking the backup is not on continuously.
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(ROOT / "backups")))
MAX_BACKUP_AGE_H = float(os.getenv("HEALTH_MAX_BACKUP_AGE_H", "48"))
ALERT_ON_GAP = os.getenv("HEALTH_ALERT_ON_GAP", "0").strip().lower() in (
    "1", "true", "yes", "on")


# ── the checks ───────────────────────────────────────────────────────────────

def _age_minutes(ts: Any) -> Optional[float]:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:                                           # noqa: BLE001
        return None


def check(url: str) -> Tuple[List[str], Dict[str, Any]]:
    """Return (problems, facts). Empty problems means healthy."""
    problems: List[str] = []
    facts: Dict[str, Any] = {"url": url,
                             "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "health_watch"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        code, body = exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                                    # noqa: BLE001
        # Unreachable is the loudest possible result and must never be silent.
        problems.append(f"UNREACHABLE: {type(exc).__name__}: {exc}")
        facts["reachable"] = False
        return problems, facts

    facts["reachable"] = True
    facts["http_status"] = code
    if code != 200:
        problems.append(f"HTTP {code} (503 means the database query failed)")

    try:
        h = json.loads(body)
    except Exception:                                           # noqa: BLE001
        problems.append("response was not JSON — is this the right URL?")
        return problems, facts

    db = h.get("database") or {}
    facts["db_ok"] = db.get("ok")
    facts["connected_as"] = db.get("connected_as")
    if db.get("ok") is False:
        problems.append(f"database not answering: {str(db.get('error'))[:120]}")
    if EXPECT_ROLE and db.get("connected_as") and db["connected_as"] != EXPECT_ROLE:
        # Not a crash — a silently weaker security posture, which is worse
        # because nothing else will report it.
        problems.append(f"app connected as '{db['connected_as']}', expected "
                        f"'{EXPECT_ROLE}' — privilege separation may be off")

    ha = h.get("ha") or {}
    role = ha.get("role")
    facts["ha_role"] = role
    if role == "unelected":
        problems.append("leader election never completed (ha.role=unelected)")

    sched = h.get("scheduler") or {}
    facts["scheduler_running"] = sched.get("running")
    facts["scheduler_jobs"] = sched.get("jobs")
    facts["last_tick"] = sched.get("last_tick")
    age = _age_minutes(sched.get("last_tick"))
    facts["last_tick_age_min"] = round(age, 1) if age is not None else None

    started_age = _age_minutes(sched.get("started_at"))
    facts["started_min_ago"] = round(started_age, 1) if started_age is not None else None

    # IS THIS RESPONSE AUTHORITATIVE ABOUT THE SCHEDULED CONTROLS?
    #
    # FOUR SEPARATE CONDITIONS, deliberately not collapsed into one. An identity
    # test alone would accept a leader whose audit cache is empty or three hours
    # stale, and `running: true` says the scheduler OBJECT exists -- not that its
    # assessment of the work is present, let alone current.
    #
    #   identity    only a leader or a standalone process holds scheduler state
    #   running     the scheduler object exists in that process
    #   present     it has an overdue assessment at all (absent on a freshly
    #               started process, and on any backend predating the ledger)
    #   fresh       that assessment is newer than MAX_AUDIT_AGE_MIN. A frozen
    #               cache reports `[]` forever, and an absence of bad news that
    #               cannot go stale is not evidence.
    #
    # `unelected` fails the identity test as well as raising its own problem
    # above: a process that never won an election holds no state either.
    _audit_age = _age_minutes(sched.get("overdue_checked_at"))
    _complete = sched.get("evidence_complete")
    coverage = {
        "identity": role in ("leader", "standalone"),
        "running": sched.get("running") is True,
        # DID THE EVIDENCE QUERY ACTUALLY SUCCEED? This is not inferable from
        # the shape of the result: an empty `overdue` list is produced both by a
        # healthy system and by a read that failed, and before the backend
        # published this field a failed read was reported as `overdue_jobs: []`
        # with a fresh timestamp -- a database failure presented as proof that
        # every scheduled job was fine.
        #
        # `is True` AND NOT `is not False`. A missing field means this backend
        # cannot say whether its query succeeded, and an unanswerable question
        # is not an affirmative answer. Backward compatibility does not get to
        # weaken the invariant; version skew is reported below under its own
        # name and remains non-authoritative until the contract is available.
        "query_ok": _complete is True,
        "assessment_present": sched.get("overdue_jobs") is not None,
        "assessment_fresh": (_audit_age is not None
                             and _audit_age <= MAX_AUDIT_AGE_MIN),
    }
    authoritative = all(coverage.values())
    facts["scheduler_evidence"] = ("authoritative" if authoritative else
                                   "INCOMPLETE: missing "
                                   + ", ".join(k for k, v in coverage.items()
                                               if not v))
    facts["scheduler_evidence_authoritative"] = authoritative
    # WHY THE EVIDENCE IS INCOMPLETE -- reported WITHOUT inferring a cause the
    # payload does not establish.
    #
    # The authority decision is unchanged: every state below is non-authoritative
    # and `query_ok` already said so. This only fixes the explanation, because an
    # explanation that names the wrong cause sends an operator to the wrong fix.
    #
    # THE CASE THAT FORCED THIS. A FOLLOWER publishes evidence_complete false
    # with evidence_never_succeeded true and NO evidence_error -- it never ran an
    # assessment, because it is not the leader. Calling that "the query did not
    # succeed" invents a database fault out of ordinary HA routing. Observed live
    # on 2026-10-12, five consecutive follower probes before a leader answered.
    #
    # AND THE CONVERSE: an absent evidence_error does NOT imply a query failed.
    # A payload that claims incomplete evidence without saying why gets a neutral
    # diagnostic rather than a guess.
    _err = sched.get("evidence_error")
    _never = bool(sched.get("evidence_never_succeeded"))
    if _complete is None:
        # Too old to say, rather than new enough to say it failed.
        facts["evidence_contract"] = (
            "unavailable — this backend does not publish evidence_complete")
    elif _complete is False and _err:
        facts["evidence_contract"] = (
            "incomplete — the backend reports the assessment failed: "
            + str(_err)
            + (" (and none has ever succeeded here)" if _never else ""))
    elif _complete is False and _never:
        facts["evidence_contract"] = (
            "no assessment has been made in this process — expected on an HA "
            "follower, which holds no scheduler state")
    elif _complete is False:
        facts["evidence_contract"] = (
            "incomplete — the backend reports its evidence is not complete and "
            "does not say why")
    if sched.get("evidence_refresh_failed_at"):
        facts["evidence_refresh_failed_at"] = sched["evidence_refresh_failed_at"]

    # Gated on IDENTITY, not on full authority: a leader with a stale or absent
    # assessment must still reach the checks below, because that is where the
    # staleness is reported. `None` is no longer admitted here -- it used to be,
    # and every check then no-opped silently on the missing fields, which is a
    # bypass wearing the costume of a check.
    if coverage["identity"]:
        if sched.get("running") is False:
            problems.append("scheduler is not running on the leader")

        # `running` is a flag set once at startup; `alive` is the scheduler
        # object answering for itself. If APScheduler's thread dies, the flag
        # stays true forever — so reading only `running` would have let the
        # new live signal be published and ignored.
        alive = sched.get("alive")
        if alive is not None:
            facts["scheduler_alive"] = alive
            if alive is False:
                problems.append(
                    "scheduler.alive=false — APScheduler has stopped inside a "
                    "process that still reports running=true; no job will fire "
                    "until this process is restarted")
        if age is None and sched.get("running"):
            # A freshly restarted scheduler has not ticked YET, and most jobs
            # here are nightly — after a 03:30 deploy the first fires at 22:00.
            # Alerting on that pages after every deploy and teaches the reader
            # to ignore this check, which is worse than not having it.
            if started_age is None or started_age > MAX_TICK_AGE_MIN:
                problems.append(
                    f"scheduler has not ticked since it started "
                    f"{'?' if started_age is None else f'{started_age:.0f} min'} "
                    f"ago (threshold {MAX_TICK_AGE_MIN:.0f} min)")
        elif age is not None and age > MAX_TICK_AGE_MIN:
            problems.append(f"scheduler last ticked {age:.0f} min ago "
                            f"(threshold {MAX_TICK_AGE_MIN:.0f}) — background "
                            f"jobs are not running")
        # last_tick says a job fired. It does not say WHICH jobs did not.
        # Every field above was true throughout the period when the nightly
        # order pipeline advanced nothing, because the scheduler was healthy
        # and the work still was not happening. This is the only check that
        # looks at the work.
        # The overdue list is a CACHE, refreshed hourly by job_ledger_audit. If
        # that job is itself among the casualties the cache freezes, and a
        # frozen cache reports `[]` — "nothing overdue" — indefinitely. An
        # absence of bad news that cannot go stale is not evidence.
        audit_age = _age_minutes(sched.get("overdue_checked_at"))
        if audit_age is not None:
            facts["overdue_checked_min_ago"] = round(audit_age, 1)
            if audit_age > MAX_AUDIT_AGE_MIN:
                problems.append(
                    f"the overdue-job audit last ran {audit_age:.0f} min ago "
                    f"(threshold {MAX_AUDIT_AGE_MIN:.0f}) — scheduler."
                    f"overdue_jobs is stale and cannot be trusted")

        # A JOB WITH NO RECORDED RUN AT ALL. audit() has always computed this
        # and /health used to discard it, which is part of how a failed evidence
        # query read as a clean result: every job lands in `unknown` and
        # `overdue` comes back empty.
        #
        # WORDED AS "CANNOT ESTABLISH", not "unhealthy". An unassessed job is
        # not evidence that the job is failing; it is evidence that this check
        # cannot say either way. Only `overdue_jobs` and `failed_jobs_24h`
        # assert that something is actually wrong.
        unknown = sched.get("unknown_jobs")
        if unknown is None:
            facts["unknown_jobs"] = "not reported by this backend"
        else:
            facts["unknown_jobs"] = len(unknown)
            if unknown:
                named = ", ".join(str(u) for u in unknown[:4])
                more = f" (+{len(unknown) - 4} more)" if len(unknown) > 4 else ""
                problems.append(
                    f"{len(unknown)} scheduled job(s) have no recorded run at "
                    f"all: {named}{more}. This does not say they are unhealthy "
                    f"-- it says their health cannot be established from the "
                    f"job ledger.")

        overdue = sched.get("overdue_jobs")
        if overdue is None:
            # Version skew, not health: a backend that predates the ledger has
            # no opinion on this. Saying "0 overdue" would claim a coverage
            # that does not exist, which is the failure mode this whole check
            # was added to remove.
            facts["overdue_jobs"] = "not reported by this backend"
            overdue = []
        else:
            facts["overdue_jobs"] = len(overdue)
        if overdue:
            named = ", ".join(
                f"{o.get('job')} ({o.get('late_min')} min late)"
                for o in overdue[:4])
            more = f" (+{len(overdue) - 4} more)" if len(overdue) > 4 else ""
            problems.append(
                f"{len(overdue)} scheduled job(s) missed their last due time: "
                f"{named}{more}")
        # Repairs are reported, not alerted on: the work did happen. A job that
        # has to be caught up EVERY night is a restart problem wearing a
        # success costume, and the count is what makes that visible to someone
        # whose monitoring was asleep when it occurred.
        repaired = sched.get("repaired_24h") or []
        if repaired:
            facts["repaired_24h"] = ", ".join(
                f"{r.get('job')}@{r.get('at')}" for r in repaired[:4])

        failed = sched.get("failed_jobs_24h") or []
        facts["failed_jobs_24h"] = len(failed)
        if failed:
            named = ", ".join(f"{f.get('job')} ({f.get('status')})"
                              for f in failed[:4])
            problems.append(
                f"{len(failed)} scheduled job(s) raised in the last 24h: {named}")

        if sched.get("ledger"):
            # The safety net that re-runs jobs a restart skipped is not
            # installed. Nothing is broken yet, and nothing will report it
            # when something is.
            problems.append(f"job-run ledger {sched['ledger']}")
    elif role == "follower":
        # A follower not running the scheduler is correct, and this branch used
        # to end here as a NOTE -- which is how 829 checks were recorded healthy
        # on no evidence. The note stays for the reader; `authoritative` above is
        # what stops it being read as a clean assessment, and check_confirmed
        # escalates it once re-polling is exhausted.
        facts["note"] = ("this process is a follower; scheduler state not "
                         "assessed. Confirm some process reports leader.")
    else:
        # `unknown` means leader.status() raised inside /health -- a fault, not a
        # quiet state -- and an absent `ha` means a backend predating HA
        # reporting. Both used to skip the scheduler checks without comment.
        facts["note"] = ("ha.role=%r: this process does not hold scheduler "
                         "state, so none was assessed." % (role,))
    return problems, facts


def check_backups(directory: Path = None,
                  max_age_h: float = None) -> Tuple[List[str], Dict[str, Any]]:
    """Report whether a backup exists that is both recent and non-empty.

    Kept separate from main() so it can be exercised directly. A monitoring
    check that can only be run by running the monitor is a check nobody tests,
    and this one was added precisely because an untested gap stayed open for
    sixteen nights.
    """
    directory = BACKUP_DIR if directory is None else directory
    max_age_h = MAX_BACKUP_AGE_H if max_age_h is None else max_age_h
    problems: List[str] = []
    facts: Dict[str, Any] = {}
    try:
        present = list(directory.glob("railway-*.dump"))
        dumps = [f for f in present if f.stat().st_size > 0]
    except OSError as exc:
        return ([f"could not read {directory}: {type(exc).__name__}: {exc}"],
                facts)
    empty = len(present) - len(dumps)
    if empty:
        facts["empty_dump_files"] = empty
    if not dumps:
        facts["newest_backup"] = "NONE"
        problems.append(
            f"no backup containing data exists in {directory}. "
            + (f"{empty} dump file(s) are present but empty, which is what a "
               f"failed run leaves behind."
               if empty else "The directory holds no dumps at all."))
        return problems, facts
    newest = max(dumps, key=lambda f: f.stat().st_mtime)
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    facts["newest_backup"] = (f"{newest.name} "
                              f"({newest.stat().st_size / 1e6:.0f} MB, "
                              f"{age_h:.0f}h old)")
    if age_h > max_age_h:
        problems.append(
            f"newest usable backup is {age_h / 24:.1f} days old "
            f"({newest.name}), threshold {max_age_h / 24:.1f} days. Recovery "
            f"would lose everything written since. "
            + (f"{empty} zero-byte dump file(s) are present, so the nightly "
               f"job is running and failing."
               if empty else
               "Check that the nightly backup task still exists."))
    return problems, facts


def check_confirmed(url: str) -> Tuple[List[str], Dict[str, Any], int]:
    """check(), but a failure has to survive a re-check -- and MISSING EVIDENCE
    is re-polled too.

    Returns (problems, facts, attempts). The LAST attempt wins: if the retry
    comes back clean the service is treated as healthy and the flap is recorded
    by the caller instead of mailed.

    TWO REASONS TO RE-POLL, and they are not the same thing. A problem may be a
    flap. Missing authority is a ROUTING outcome: scheduler state lives in the
    leader and 30% of probes reach a follower holding none, so a second probe
    reaches the leader for the 16% of follower runs that are isolated.

    AND THEN IT FAILS CLOSED. If authority is still not obtained, the absence is
    recorded as a problem, because "I could not check" is not "nothing is wrong"
    -- the substitution this function exists to prevent.

    `first_attempt_problems` is carried so the caller can tell a recovered
    SERVICE flap from a re-poll that only ever chased routing. Counting the
    second as a blip would make ~30% of checks look like restarts and trip the
    repeated-restart alert on a healthy service.
    """
    problems, facts = check(url)
    first_problems = len(problems)
    attempts = 1
    while attempts <= CONFIRM_RETRIES and (
            problems or not facts.get("scheduler_evidence_authoritative")):
        why = ("%d problem(s)" % len(problems) if problems else
               "no authoritative scheduler evidence (ha.role=%r)"
               % (facts.get("ha_role"),))
        print(f"  {why} on attempt {attempts} — re-checking in "
              f"{CONFIRM_DELAY_S:.0f}s before alerting")
        time.sleep(CONFIRM_DELAY_S)
        problems, facts = check(url)
        attempts += 1
    facts["attempts"] = attempts
    facts["first_attempt_problems"] = first_problems
    if not facts.get("scheduler_evidence_authoritative"):
        problems.append(
            f"NO AUTHORITATIVE SCHEDULER EVIDENCE after {attempts} attempt(s) "
            f"(ha.role={facts.get('ha_role')!r}; "
            f"{facts.get('scheduler_evidence')}). Scheduled-control coverage is "
            f"NOT established by this check: a governance audit or a nightly job "
            f"could be failing and this check could not have seen it. That is "
            f"not a healthy result.")
    return problems, facts, attempts


# ── alerting ─────────────────────────────────────────────────────────────────

def _send(subject: str, text: str) -> bool:
    host = os.getenv("EMAIL_SMTP_HOST", "mail.agentorc.ca")
    port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
    addr = os.getenv("EMAIL_ADDRESS", "info@agentorc.ca")
    pwd = os.getenv("EMAIL_PASSWORD", "")
    to = os.getenv("HEALTH_ALERT_TO", "").strip() or addr
    if not pwd:
        print("EMAIL_PASSWORD not set — cannot send", file=sys.stderr)
        return False
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"Conscestra Health <{addr}>"
    msg["To"] = to
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(addr, pwd)
            s.sendmail(addr, [a.strip() for a in to.split(",")], msg.as_string())
        print(f"  alert sent to {to}")
        return True
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ALERT SEND FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:                                           # noqa: BLE001
        return {}


def _save_state(d: Dict[str, Any]) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception as exc:                                    # noqa: BLE001
        print(f"  could not persist state: {exc}", file=sys.stderr)


class _Tee:
    """Write to stdout AND the log file.

    The scheduled task used to be `cmd /c ... >> health.log`, and cmd exists in
    that command ONLY to perform the redirection — which costs a console window
    flashing on the desktop every fifteen minutes. Owning the log here means the
    task can run under pythonw.exe, which has no console at all.

    It is also the better shape: a script whose record of what it did depends on
    how it happened to be invoked has no record when someone invokes it
    differently."""

    def __init__(self, stream, path: Path):
        self._stream, self._path = stream, path

    def write(self, text: str) -> int:
        try:
            self._stream.write(text)
        except Exception:                                       # noqa: BLE001
            pass                       # no console under pythonw.exe
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:                                       # noqa: BLE001
            pass                       # a failed log must not fail the check
        return len(text)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:                                       # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Alert when /health stops being true")
    ap.add_argument("--url", default=os.getenv("HEALTH_URL", "").strip())
    ap.add_argument("--force", action="store_true", help="email regardless of state")
    ap.add_argument("--no-log", action="store_true",
                    help="do not append to the log file (it is written by "
                         "default so the scheduled task needs no shell "
                         "redirection, and therefore no console window)")
    ap.add_argument("--test-email", action="store_true",
                    help="send a test alert and exit — proves the path works "
                         "BEFORE you need it")
    a = ap.parse_args()

    if not a.no_log:
        log = Path(os.getenv("HEALTH_LOG_FILE",
                             str(ROOT / "backups" / "health.log")))
        sys.stdout = _Tee(sys.stdout, log)          # type: ignore[assignment]
        sys.stderr = _Tee(sys.stderr, log)          # type: ignore[assignment]

    if a.test_email:
        ok = _send("Conscestra health watch — TEST",
                   "This is a test of the health alert path.\n\n"
                   "If you are reading this, alerts can reach you. That is the "
                   "only thing this proves; it says nothing about the system's "
                   "health.\n")
        return 0 if ok else 1

    url = a.url
    if not url:
        print("no URL. Pass --url or set HEALTH_URL "
              "(e.g. https://<app>.up.railway.app/health)", file=sys.stderr)
        return 2

    problems, facts, attempts = check_confirmed(url)
    now = datetime.now(timezone.utc)
    state = _load_state()

    # ── how long was nobody watching? ────────────────────────────────────────
    gap = _age_minutes(state.get("checked_at"))
    gap_note = None
    if gap is not None:
        facts["since_last_check_min"] = round(gap, 1)
        if gap > MAX_GAP_MIN:
            gap_note = (f"MONITORING GAP: no check ran for {gap:.0f} minutes "
                        f"before this one (threshold {MAX_GAP_MIN:.0f}). "
                        f"Nothing is known about that window.")
            print(f"  {gap_note}")
            if ALERT_ON_GAP:
                problems.append(gap_note)

    # ── transient failures: suppressed, counted, and eventually reported ─────
    blips = [b for b in state.get("blips", [])
             if (_age_minutes(b) or 1e9) <= BLIP_WINDOW_HOURS * 60]
    # ONLY A SERVICE FLAP COUNTS. A re-poll driven purely by missing authority
    # is a routing outcome, not a restart, and ~30% of checks now re-poll for
    # that reason -- counting those would trip the repeated-restart alert on a
    # perfectly healthy service.
    if attempts > 1 and not problems and facts.get("first_attempt_problems"):
        blips.append(now.isoformat())
        print(f"  transient: recovered by attempt {attempts} — not alerting")
    facts["blips_24h"] = len(blips)
    if len(blips) >= BLIP_ALERT_COUNT:
        problems.append(
            f"{len(blips)} transient failures in the last "
            f"{BLIP_WINDOW_HOURS:.0f}h — each recovered within "
            f"{CONFIRM_DELAY_S:.0f}s, so the service is restarting repeatedly "
            f"rather than being down. Check the platform's restart history.")

    # ── is there a recent backup that contains anything? ────────────────────
    backup_problems, backup_facts = check_backups()
    facts.update(backup_facts)
    problems.extend(backup_problems)

    healthy = not problems

    print(f"health check {url}")
    for k, v in facts.items():
        print(f"  {k:22} {v}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("\n  healthy")

    was_healthy = state.get("healthy")
    last_alert = _age_minutes(state.get("last_alert_at"))
    transition = (was_healthy is not None) and (was_healthy != healthy)
    stale_alert = (last_alert is None) or (last_alert > REPEAT_HOURS * 60)

    should = a.force or (not healthy and (transition or stale_alert)) or \
             (healthy and transition)
    if should:
        if healthy:
            subj = "Conscestra: RECOVERED"
            body = f"{url} is healthy again at {now.isoformat()}.\n\n"
        else:
            subj = f"Conscestra: {len(problems)} problem(s)"
            body = ("Problems detected:\n\n"
                    + "\n".join(f"  - {p}" for p in problems)
                    + "\n\nRunbooks:\n"
                      "  docs/runbook_leader_failure.md      (scheduler / leader)\n"
                      "  docs/runbook_incident_escalation.md (triage, 72h clock)\n"
                      "  docs/runbook_restore.md             (data loss)\n\n")
        if gap_note:
            # Carried on every email, not only gap alerts: the reader needs to
            # know the preceding silence was unobserved rather than quiet.
            body += gap_note + "\n\n"
        body += "Facts:\n" + "\n".join(f"  {k}: {v}" for k, v in facts.items())
        if _send(subj, body):
            state["last_alert_at"] = now.isoformat()

    state["healthy"] = healthy
    state["checked_at"] = now.isoformat()
    state["problems"] = problems
    state["blips"] = blips[-50:]
    # Additive to the EXISTING store, not a second one. Without these the
    # monitor's own history can say "healthy" without saying whether it ever had
    # the evidence to justify it -- which is how this blind spot stayed
    # invisible across 2,989 checks.
    state["ha_role"] = facts.get("ha_role")
    state["scheduler_evidence"] = facts.get("scheduler_evidence")
    _save_state(state)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
