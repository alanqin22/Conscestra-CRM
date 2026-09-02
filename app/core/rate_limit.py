"""In-process rate limiting for the auth endpoints (security hardening #4).

Protects /auth/signin (and password-reset requests) from brute-force and
credential-stuffing without any new dependency or infrastructure:

  • per-IP sliding window   — caps total sign-in attempts from one address
  • per-identifier lockout  — caps FAILED attempts against one account, so a
    distributed attack on a single mailbox is still throttled; a successful
    sign-in clears the account's failure window

Same staged philosophy as the rest of the security work: on by default with
generous limits (a human retyping a password never hits them), and can be
disabled with AUTH_RATE_LIMIT=0.

In-process state (like the OTP store in the auth router) — per-instance on a
multi-worker deployment, which only makes limits more generous, never stricter.

CONFIG (env)
  AUTH_RATE_LIMIT            1     0 = disable all auth rate limiting
  AUTH_MAX_ATTEMPTS_PER_IP   20    sign-in attempts allowed per IP per window
  AUTH_MAX_FAILS_PER_USER    3     failed sign-ins per identifier per window
  AUTH_RATE_WINDOW_SECONDS   900   window length (15 min)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Deque, Dict

from fastapi import Request

logger = logging.getLogger("rate_limit")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


AUTH_RATE_LIMIT = os.getenv("AUTH_RATE_LIMIT", "1").strip().lower() not in ("0", "false", "off", "no")
MAX_ATTEMPTS_PER_IP = _int_env("AUTH_MAX_ATTEMPTS_PER_IP", 20)
MAX_FAILS_PER_USER = _int_env("AUTH_MAX_FAILS_PER_USER", 3)
WINDOW_SECONDS = _int_env("AUTH_RATE_WINDOW_SECONDS", 900)

# Logged from main.py at startup (this module imports before logging is
# configured, so logging here would be swallowed).
POSTURE = (
    f"[security] auth rate limiting: {'ON' if AUTH_RATE_LIMIT else 'OFF'} "
    f"(per-IP {MAX_ATTEMPTS_PER_IP}/{WINDOW_SECONDS}s, "
    f"per-account fails {MAX_FAILS_PER_USER}/{WINDOW_SECONDS}s)"
)


class SlidingWindowLimiter:
    """Thread-safe sliding-window event counter keyed by an arbitrary string."""

    # Purge idle keys once the table grows past this, so an attacker rotating
    # identifiers can't grow memory without bound.
    _PURGE_THRESHOLD = 10_000

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window = window_seconds
        self._events: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, dq: Deque[float], now: float) -> None:
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()

    def _purge_idle(self, now: float) -> None:
        if len(self._events) < self._PURGE_THRESHOLD:
            return
        for key in [k for k, dq in self._events.items()
                    if not dq or dq[-1] <= now - self.window]:
            del self._events[key]

    def record(self, key: str) -> int:
        """Record one event for the key; return the count now in the window."""
        now = time.monotonic()
        with self._lock:
            self._purge_idle(now)
            dq = self._events.setdefault(key, deque())
            self._prune(dq, now)
            dq.append(now)
            return len(dq)

    def count(self, key: str) -> int:
        """Return the current in-window count without recording an event."""
        now = time.monotonic()
        with self._lock:
            dq = self._events.get(key)
            if not dq:
                return 0
            self._prune(dq, now)
            return len(dq)

    def is_limited(self, key: str) -> bool:
        return self.count(key) >= self.max_events

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


# Shared limiter instances (module-level singletons, like the OTP store).
signin_ip_attempts = SlidingWindowLimiter(MAX_ATTEMPTS_PER_IP, WINDOW_SECONDS)
signin_user_fails = SlidingWindowLimiter(MAX_FAILS_PER_USER, WINDOW_SECONDS)
reset_requests = SlidingWindowLimiter(5, 3600)  # password-reset: 5/identifier/hour
# Planner EXECUTION (plan: … confirm) — runs reads + queues governance proposals,
# so throttle per-IP to bound queue-flooding + LLM cost even for authorized callers.
plan_exec_ip = SlidingWindowLimiter(_int_env("PLAN_EXEC_MAX_PER_HOUR", 20), 3600)

# ANONYMOUS CRM READS under the public-read posture.
#
# public-read is a deliberate product decision (2026-08-31): a prospective
# client must see the CRM working without a login. It is not a defect and is
# not to be "fixed". But it left the entire read surface with NO control at
# all, so the corpus was enumerable at whatever rate the host would serve —
# 118 accounts, 129 contacts, 2,193 orders and the AR position, page by page,
# to anyone who knew the hostname.
#
# Rate limiting is the control that fits the decision instead of reversing it.
# A demonstration is a human reading pages; enumeration is a script walking
# them. Only the second notices a ceiling.
#
# SIZED FOR THE DEMO, NOT FOR THE ATTACKER. A management page can fan out to
# several agent calls per interaction, and a prospect clicking around briskly
# is still nowhere near 240 requests in five minutes. That ceiling caps a
# scraper at ~48/min — slow enough that a full walk of every module takes long
# enough to notice, generous enough that no real visitor will ever see it.
#
# NOT APPLIED TO SIGNED-IN CALLERS: a session identifies who is asking, and
# staff legitimately drive far more traffic than a visitor. This limiter exists
# because the caller is anonymous, so it ends where anonymity does.
PUBLIC_READ_LIMIT_ENABLED = os.getenv(
    "PUBLIC_READ_RATE_LIMIT", "1").strip().lower() not in ("0", "false", "no", "off")
PUBLIC_READ_MAX_PER_IP = _int_env("PUBLIC_READ_MAX_PER_IP", 240)
PUBLIC_READ_WINDOW_SECONDS = _int_env("PUBLIC_READ_WINDOW_SECONDS", 300)
public_read_ip = SlidingWindowLimiter(PUBLIC_READ_MAX_PER_IP,
                                      PUBLIC_READ_WINDOW_SECONDS)


def client_ip(request: Request) -> str:
    """Best-effort client address; honours the proxy's X-Forwarded-For (Railway
    fronts the app with a proxy, so the socket peer is the proxy itself)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
