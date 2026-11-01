"""Guest demo policy — the controls around the public "Try the demo" session.

The guest demo is an approved marketing posture on a DEMO dataset (owner
attestation, 2026-11-01). Read-only was already guaranteed by write_guard: a
viewer runs inside a PostgreSQL read-only transaction. What this module adds is
the rest of the boundary a public, no-password entry point needs:

  • REACH      deny-by-default route allowlist for guest requests
  • COST       per-session and global daily caps on AI (natural-language) calls
  • ACTION     no outbound or acting tool (web, email, SMS, calls, MCP) for a guest
  • VOLUME     a ceiling on concurrent guest sessions, and the anonymous read
               limit applied to guests as well
  • LIFETIME   short sessions, re-validated on every request
  • POSTURE    the release guard refuses a deployed start with the guest flag on
               unless the dataset is attested as demo data

GUEST MARKING. A guest request is marked on a ContextVar, which is never reset
to False within a request. The orchestrator calls module agents in-process
without forwarding the bearer token, so the inner call looks anonymous; the
inherited marker keeps the guest restrictions in force across that hop.

COUNTERS. The AI caps use the in-process sliding-window limiter. On a
multi-worker deployment each worker counts separately, so the effective global
ceiling is GUEST_AI_PER_DAY × workers. The concurrent-session ceiling is read
from auth_sessions and holds across workers.

CONFIG (env)
  GUEST_DEMO_ENABLED            0     1 = the guest endpoint is on
  GUEST_DEMO_IDENTIFIER         ''    the viewer credential guest sessions use
  GUEST_DEMO_DATASET_ATTESTED   0     1 = the connected dataset is demo data
  GUEST_SESSION_MINUTES         15    guest session lifetime
  GUEST_MAX_CONCURRENT          20    live guest sessions allowed at once
  GUEST_AI_PER_SESSION          20    AI calls per guest session
  GUEST_AI_PER_DAY              200   AI calls per day across all guests
  TURNSTILE_SECRET_KEY          ''    set = a Cloudflare Turnstile token is required
"""
from __future__ import annotations

import hashlib
import logging
import os
from contextvars import ContextVar
from typing import Optional

from fastapi import HTTPException, Request

from app.core.rate_limit import SlidingWindowLimiter

logger = logging.getLogger("guest_demo")


def _flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


SESSION_MINUTES = _int_env("GUEST_SESSION_MINUTES", 15)
MAX_CONCURRENT = _int_env("GUEST_MAX_CONCURRENT", 20)
AI_PER_SESSION = _int_env("GUEST_AI_PER_SESSION", 20)
AI_PER_DAY = _int_env("GUEST_AI_PER_DAY", 200)


def enabled() -> bool:
    return _flag("GUEST_DEMO_ENABLED")


def identifier() -> str:
    """The configured guest identifier, or '' when the feature is off."""
    return os.getenv("GUEST_DEMO_IDENTIFIER", "").strip().lower() if enabled() else ""


def dataset_attested() -> bool:
    return _flag("GUEST_DEMO_DATASET_ATTESTED")


def turnstile_secret() -> str:
    return os.getenv("TURNSTILE_SECRET_KEY", "").strip()


def is_guest_identifier(ident: Optional[str]) -> bool:
    """True when `ident` is the configured guest credential (feature on or off).

    Deliberately independent of GUEST_DEMO_ENABLED: password sign-in and
    password reset must refuse the guest identifier even while the feature is
    off, so the credential can never be used as an ordinary account."""
    configured = os.getenv("GUEST_DEMO_IDENTIFIER", "").strip().lower()
    return bool(configured) and (ident or "").strip().lower() == configured


def is_guest_session(sess: Optional[dict]) -> bool:
    return bool(sess) and is_guest_identifier(sess.get("identifier"))


# ---------------------------------------------------------------------------
# Request marking
# ---------------------------------------------------------------------------
_is_guest: ContextVar[bool] = ContextVar("guest_request", default=False)
_ai_counted: ContextVar[bool] = ContextVar("guest_ai_counted", default=False)


def mark_guest() -> None:
    _is_guest.set(True)


def is_guest_request() -> bool:
    return _is_guest.get()


def refuse_if_guest(operation: str) -> bool:
    """True (and logged) when the current request is a guest's. Outbound and
    acting chokepoints call this and decline the operation."""
    if not _is_guest.get():
        return False
    logger.warning(f"[guest] refused outbound/acting operation: {operation}")
    return True


# ---------------------------------------------------------------------------
# Reach: deny-by-default route allowlist
# ---------------------------------------------------------------------------
# Core CRM modules only. Each `*-chat` endpoint serves both the list/search
# screens (structured `mode`, no LLM) and natural-language chat (LLM, capped
# below). Not listed, and therefore refused: notifications, voice, the
# notification stream, analytics "act", case routing administration, metrics
# and explore, and every admin or governance router.
ALLOWED_PATHS = frozenset({
    "/account-chat", "/contact-chat", "/prod-chat",
    "/order-chat", "/orders-chat", "/activity-chat", "/case-chat",
    "/opportunity-chat", "/accounting-chat", "/lead-chat", "/leads-chat",
    "/analytics-chat", "/orchestrator-chat",
    "/account-health", "/contact-health", "/product-health", "/order-health",
    "/activity-health", "/opportunity-health", "/accounting-health",
    "/lead-health", "/analytics-health", "/orchestrator-health",
})


def path_allowed(path: str) -> bool:
    return path.rstrip("/") in ALLOWED_PATHS


def is_ai_call(method: str, path: str, mode: Optional[str]) -> bool:
    """A POST to a chat endpoint without a structured mode reaches the LLM.

    Counted conservatively: a request that carries no mode is treated as an AI
    call even if the agent graph later answers it without one."""
    return method.upper() == "POST" and path.rstrip("/").endswith("-chat") and not mode


# ---------------------------------------------------------------------------
# Cost: AI caps
# ---------------------------------------------------------------------------
ai_per_session = SlidingWindowLimiter(AI_PER_SESSION, max(SESSION_MINUTES, 1) * 60)
ai_per_day = SlidingWindowLimiter(AI_PER_DAY, 86400)


def _session_key(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def enforce(request: Request, mode: Optional[str], token: str) -> None:
    """Apply the guest boundary to one request. Raises HTTPException to refuse."""
    path = request.url.path
    if not path_allowed(path):
        logger.warning(f"[guest] route refused: {request.method} {path}")
        raise HTTPException(status_code=403,
                            detail="This area is not available in the guest demo.")

    if not is_ai_call(request.method, path, mode) or _ai_counted.get():
        return
    _ai_counted.set(True)

    if ai_per_day.count("all") >= AI_PER_DAY:
        logger.warning("[guest] daily AI cap reached")
        raise HTTPException(
            status_code=429,
            detail="The guest demo's AI assistant has reached today's limit. "
                   "Please try again tomorrow, or sign in for full access.")
    key = _session_key(token)
    if ai_per_session.count(key) >= AI_PER_SESSION:
        logger.warning("[guest] per-session AI cap reached")
        raise HTTPException(
            status_code=429,
            detail="This guest session has used its AI questions. "
                   "Start a new guest session or sign in for full access.")
    ai_per_session.record(key)
    ai_per_day.record("all")
    logger.info(f"[guest] AI call {request.method} {path}")


# ---------------------------------------------------------------------------
# Volume: Cloudflare Turnstile on session creation
# ---------------------------------------------------------------------------
_TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def verify_turnstile(token: Optional[str], ip: str) -> bool:
    """True when Turnstile is not configured, or the token verifies.

    Fails closed once a secret is configured: an unreachable verifier refuses
    the session rather than skipping the check."""
    secret = turnstile_secret()
    if not secret:
        return True
    if not token:
        return False
    try:
        import httpx
        resp = httpx.post(_TURNSTILE_URL, timeout=10,
                          data={"secret": secret, "response": token, "remoteip": ip})
        return bool(resp.json().get("success"))
    except Exception as exc:
        logger.warning(f"[guest] turnstile verification unavailable: {exc}")
        return False
