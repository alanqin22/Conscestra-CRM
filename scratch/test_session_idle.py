"""Tests for session governance — idle timeout + signout invalidation.

Exercises the real get_session()/_new_session()/_invalidate_session() against
the local DB (no HTTP server needed).  Run: python scratch/test_session_idle.py
"""
import sys

sys.path.insert(0, ".")

from app.agents.auth.router import (
    _IDLE_TIMEOUT_MINUTES, _invalidate_session, _new_session, _token_hash,
    get_session,
)
from app.core.database import get_connection

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}")


def backdate_last_seen(token, minutes):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE auth_sessions SET last_seen_at = now() - make_interval(mins => %s) "
                "WHERE token_hash = %s", (minutes, _token_hash(token)))
        conn.commit()
    finally:
        conn.close()


def last_seen_age_seconds(token):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (now() - last_seen_at)) FROM auth_sessions "
                "WHERE token_hash = %s", (_token_hash(token),))
            row = cur.fetchone()
            return float(row[0]) if row else None
    finally:
        conn.close()


print(f"idle timeout configured: {_IDLE_TIMEOUT_MINUTES} min")
check("idle timeout defaults to 15 min", _IDLE_TIMEOUT_MINUTES == 15)

# 1. fresh session is valid
t1 = _new_session("", "cred-idle-test", "idle-test@nowhere.invalid", role="member")
check("fresh session validates", get_session(t1) is not None)

# 2. idle past the window → invalidated and purged
backdate_last_seen(t1, _IDLE_TIMEOUT_MINUTES + 5)
check("idle session rejected", get_session(t1) is None)
check("idle session row purged", last_seen_age_seconds(t1) is None)

# 3. activity inside the window slides last_seen forward
t2 = _new_session("", "cred-idle-test", "idle-test@nowhere.invalid", role="member")
backdate_last_seen(t2, _IDLE_TIMEOUT_MINUTES - 5)
check("active session survives", get_session(t2) is not None)
age = last_seen_age_seconds(t2)
check("activity slides last_seen forward", age is not None and age < 60,
      f"age={age}")

# 4. signout invalidates immediately
_invalidate_session(t2)
check("signout invalidates the session", get_session(t2) is None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
