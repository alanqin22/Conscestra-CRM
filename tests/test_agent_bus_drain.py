"""Regression tests for the agent-bus draining bug (2026-07-25).

THE BUG
    `start_agent_bus()` set the eligibility cutoff to `now()` on every boot, so
    any event emitted while the process was DOWN became permanently ineligible.
    It was never claimed, never retried, never failed — it sat 'pending' with
    attempts=0 forever. Found with 50 such events aged up to 13 days, and it
    recurred on every restart, in production as much as locally.

WHAT THESE TESTS PIN DOWN
    1. the downtime gap is caught up after a restart      (the fix)
    2. a never-consumed queue still does NOT mass-replay  (the protection kept)
    3. the catch-up window is bounded                     (blast radius)
    4. an explicit backfill override still wins           (operator control)
    5. events stranded behind the cutoff are REPORTED     (no silent loss)
    6. the full lifecycle drains: create → claim → dispatch → completed
    7. a failing handler retries with backoff, then fails (nothing stuck pending)

These run against the local Postgres and clean up after themselves. They use a
synthetic event type with an in-test handler, so no business side effect (and
notably no AUTOSEND email) can fire.

    python -m pytest tests/test_agent_bus_drain.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DB_DSN", "postgresql://postgres:aria@localhost:5434/crmdb")

from app.core import agent_bus as B          # noqa: E402
from app.core.database import get_connection  # noqa: E402

PROBE_TYPE = "test.bus_probe"


# ---------------------------------------------------------------- helpers ---

def _emit(created_at=None, etype=PROBE_TYPE) -> str:
    """Insert an event AND its queue row directly.

    We write the queue row ourselves because the DB's enqueue trigger only fires
    for registered event types; the point here is the CONSUMER's behaviour, not
    the registry."""
    eid = str(uuid.uuid4())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO events (event_uuid, event_type, entity_type,
                                       entity_uuid, payload, created_at, source_system)
                   VALUES (%s::uuid, %s, 'system', %s::uuid, %s::jsonb,
                           COALESCE(%s, now()), 'pytest')""",
                (eid, etype, eid, json.dumps({"context": {"probe": True}}), created_at))
            cur.execute(
                """INSERT INTO event_queue (event_uuid, status, created_at)
                   VALUES (%s::uuid, 'pending', COALESCE(%s, now()))
                   ON CONFLICT (event_uuid) DO NOTHING""", (eid, created_at))
        conn.commit()
        return eid
    finally:
        conn.close()


def _state(event_uuid: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT status, COALESCE(attempts,0), last_error,
                                  next_attempt_at
                           FROM event_queue WHERE event_uuid=%s::uuid""",
                        (event_uuid,))
            return cur.fetchone()
    finally:
        conn.close()


def _cleanup(ids):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM event_queue WHERE event_uuid = ANY(%s::uuid[])", (ids,))
            cur.execute("DELETE FROM events WHERE event_uuid = ANY(%s::uuid[])", (ids,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def bus():
    """Isolate the consumer: only our probe type is claimable, no catchall, and
    every piece of global state is restored afterwards."""
    saved = (dict(B.HANDLERS), B.CATCHALL, B._CUTOFF, B.BACKFILL_MIN,
             B.MAX_CATCHUP_HOURS, B.BATCH)
    created: list = []

    async def ok_handler(ev):
        return {"status": "ok", "probe": True}

    B.HANDLERS.clear()
    B.HANDLERS[PROBE_TYPE] = ok_handler
    B.CATCHALL = False
    yield created
    _cleanup(created)
    (handlers, B.CATCHALL, B._CUTOFF, B.BACKFILL_MIN,
     B.MAX_CATCHUP_HOURS, B.BATCH) = saved
    B.HANDLERS.clear()
    B.HANDLERS.update(handlers)


# ------------------------------------------------------------ the cutoff ---

def test_resume_cutoff_catches_up_the_downtime_gap(bus, monkeypatch):
    """THE FIX: a restart resumes from the last-settled watermark, so events
    emitted while the process was down are still eligible."""
    watermark = datetime.now(timezone.utc) - timedelta(minutes=45)
    monkeypatch.setattr(B, "_last_activity_sync", lambda: watermark)
    monkeypatch.setattr(B, "BACKFILL_MIN", 0)

    cutoff = B._resume_cutoff()
    assert cutoff == watermark, "must resume at the watermark, not at now()"
    # An event emitted 30 min ago — i.e. DURING the downtime — is now eligible.
    assert cutoff < datetime.now(timezone.utc) - timedelta(minutes=30)


def test_resume_cutoff_is_bounded_by_max_catchup(bus, monkeypatch):
    """A consumer that has been off for a month reaches back a day, not a
    month — the boot cutoff's original anti-mass-replay purpose is preserved."""
    ancient = datetime.now(timezone.utc) - timedelta(days=30)
    monkeypatch.setattr(B, "_last_activity_sync", lambda: ancient)
    monkeypatch.setattr(B, "BACKFILL_MIN", 0)
    monkeypatch.setattr(B, "MAX_CATCHUP_HOURS", 24)

    cutoff = B._resume_cutoff()
    floor = datetime.now(timezone.utc) - timedelta(hours=24)
    assert cutoff > ancient, "must not reach back 30 days"
    assert abs((cutoff - floor).total_seconds()) < 60


def test_never_consumed_queue_does_not_mass_replay(bus, monkeypatch):
    """A fresh install with a large historical queue must NOT replay it by
    surprise: with no watermark we keep the conservative 'start at now'."""
    monkeypatch.setattr(B, "_last_activity_sync", lambda: None)
    monkeypatch.setattr(B, "BACKFILL_MIN", 0)

    cutoff = B._resume_cutoff()
    assert abs((cutoff - datetime.now(timezone.utc)).total_seconds()) < 5


def test_explicit_backfill_override_wins(bus, monkeypatch):
    monkeypatch.setattr(B, "_last_activity_sync", lambda: datetime.now(timezone.utc))
    monkeypatch.setattr(B, "BACKFILL_MIN", 120)

    cutoff = B._resume_cutoff()
    expected = datetime.now(timezone.utc) - timedelta(minutes=120)
    assert abs((cutoff - expected).total_seconds()) < 5


# ------------------------------------------------------------ the drain ----

def test_event_drains_end_to_end(bus):
    """create → claim → dispatch → completed, with the cutoff behind it."""
    eid = _emit(); bus.append(eid)
    assert _state(eid)[0] == "pending"

    B._CUTOFF = datetime.now(timezone.utc) - timedelta(minutes=5)
    summary = asyncio.run(B.run_once())

    assert summary["claimed"] == 1
    status, attempts, err, _ = _state(eid)
    assert status == "completed"
    assert attempts == 1
    assert err is None


def test_event_emitted_during_downtime_drains_after_restart(bus, monkeypatch):
    """THE REGRESSION. An event emitted 6 hours ago (while the app was down)
    used to be stranded forever by a now() cutoff; with the resume watermark it
    is picked up on the next boot."""
    six_hours_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    eid = _emit(created_at=six_hours_ago); bus.append(eid)

    # The consumer was last alive 8 hours ago, then the process died.
    monkeypatch.setattr(B, "_last_activity_sync",
                        lambda: datetime.now(timezone.utc) - timedelta(hours=8))
    monkeypatch.setattr(B, "BACKFILL_MIN", 0)
    monkeypatch.setattr(B, "MAX_CATCHUP_HOURS", 24)

    B._CUTOFF = B._resume_cutoff()
    summary = asyncio.run(B.run_once())

    assert summary["claimed"] == 1, "the downtime event must be claimed"
    assert _state(eid)[0] == "completed"


def test_events_behind_the_cutoff_are_reported_not_silent(bus):
    """Anything the cutoff still excludes must be COUNTED, so the decision to
    drain or discard is made by a person instead of by silence."""
    old = _emit(created_at=datetime.now(timezone.utc) - timedelta(days=13))
    bus.append(old)

    B._CUTOFF = datetime.now(timezone.utc) - timedelta(hours=1)
    assert asyncio.run(B.run_once())["claimed"] == 0      # correctly skipped
    assert _state(old)[1] == 0                            # never even attempted

    report = B.orphaned_sync(B._CUTOFF)
    assert report["orphaned"] >= 1
    assert PROBE_TYPE in report["by_type"]
    assert "drain" in report["note"]


def test_drain_backlog_processes_stranded_events(bus):
    """The deliberate escape hatch still works and settles old events."""
    old = _emit(created_at=datetime.now(timezone.utc) - timedelta(days=13))
    bus.append(old)
    B._CUTOFF = datetime.now(timezone.utc) - timedelta(hours=1)

    asyncio.run(B.drain_backlog(max_total=5, since_days=365))

    assert _state(old)[0] == "completed"
    # the live cutoff is restored afterwards, not left wide open
    assert B._CUTOFF > datetime.now(timezone.utc) - timedelta(hours=2)


# ------------------------------------------------------ failure handling ---

def test_handler_failure_retries_with_backoff_then_fails(bus):
    """A raising handler must never leave an event stuck: it goes back to
    pending with a future next_attempt_at, and lands on 'failed' at the cap —
    it is not silently swallowed."""
    async def boom(ev):
        raise RuntimeError("handler exploded")
    B.HANDLERS[PROBE_TYPE] = boom

    eid = _emit(); bus.append(eid)
    B._CUTOFF = datetime.now(timezone.utc) - timedelta(minutes=5)

    asyncio.run(B.run_once())
    status, attempts, err, next_at = _state(eid)
    assert status == "pending", "a failure must return it to the queue"
    assert attempts == 1
    assert "exploded" in (err or "")
    assert next_at > datetime.now(timezone.utc), "backoff must delay the retry"

    # The lock must be fully released, so next_attempt_at (not the 5-minute
    # stale-lock guard) is what governs when the retry may run.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT locked_by, locked_at FROM event_queue "
                        "WHERE event_uuid=%s::uuid", (eid,))
            locked_by, locked_at = cur.fetchone()
    finally:
        conn.close()
    assert locked_by is None and locked_at is None, \
        "a released event must clear locked_at too, or backoff is overridden"

    # At the attempt cap it becomes 'failed' rather than retrying forever.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE event_queue
                           SET attempts=%s, next_attempt_at=now()-interval '1 minute'
                           WHERE event_uuid=%s::uuid""", (B.MAX_ATTEMPTS - 1, eid))
        conn.commit()
    finally:
        conn.close()

    asyncio.run(B.run_once())
    assert _state(eid)[0] == "failed"
