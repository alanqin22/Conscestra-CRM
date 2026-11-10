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

WHERE AN AI QUESTION IS COUNTED. The caps are charged at the model call, not
at the HTTP edge. The edge cannot tell from the request shape whether an agent
will reach the model: an agent skips the model only for `routerAction` together
with `mode`, some agents have no such short-circuit, and the orchestrator
ignores both. Counting at the edge by "no mode" let any guest request carrying
a `mode` reach the model uncounted. Every model call goes through the shared
factory's MeteredLLM, which calls charge_llm_call() before contacting a
provider. A guest request is charged one AI question at its first model call,
and may make at most GUEST_LLM_CALLS_PER_REQUEST model calls in total, which
bounds orchestrator fan-out. The edge still refuses an evident AI request early
once a cap is spent, so the guest sees a clear 429 instead of an agent error.

CONFIG (env)
  GUEST_DEMO_ENABLED            0     1 = the guest endpoint is on
  GUEST_DEMO_IDENTIFIER         ''    the viewer credential guest sessions use
  GUEST_DEMO_DATASET_ATTESTED   0     1 = the connected dataset is demo data
  GUEST_SESSION_MINUTES         15    guest session lifetime
  GUEST_MAX_CONCURRENT          20    live guest sessions allowed at once
  GUEST_AI_PER_SESSION          20    AI questions per guest session
  GUEST_AI_PER_DAY              200   AI questions per day across all guests
  GUEST_LLM_CALLS_PER_REQUEST   8     model calls one guest request may make
  TURNSTILE_SECRET_KEY          ''    set = a Cloudflare Turnstile token is required
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from contextvars import ContextVar
from typing import Optional

from fastapi import HTTPException, Request

from app.core.llm_meter import LLMBudgetExceeded
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
LLM_CALLS_PER_REQUEST = _int_env("GUEST_LLM_CALLS_PER_REQUEST", 8)


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


class _GuestTicket:
    """The AI accounting for one guest request.

    Held on a ContextVar as a mutable object, so that every copy of the
    request's context (worker threads, the orchestrator's in-process agent
    calls) shares the same counts. A ContextVar holding a plain value would
    give each thread its own copy and charge the request once per thread."""

    __slots__ = ("key", "charged", "calls", "lock")

    def __init__(self, key: str) -> None:
        self.key = key
        self.charged = False
        self.calls = 0
        self.lock = threading.Lock()


_ticket: ContextVar[Optional[_GuestTicket]] = ContextVar("guest_ai_ticket", default=None)

# count() followed by record() on two limiters is not atomic; this lock makes
# the cap check and the charge one step across concurrent guest requests.
_charge_lock = threading.Lock()


class GuestAILimitReached(LLMBudgetExceeded):
    """A guest request was refused a model call. Raised before any provider
    is contacted; call sites already degrade on LLMBudgetExceeded."""


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
    """A POST to a chat endpoint without a structured mode is evidently an AI
    question. Used only for the early refusal in enforce(); a request carrying
    a mode can still reach the model, and charge_llm_call() counts it there."""
    return method.upper() == "POST" and path.rstrip("/").endswith("-chat") and not mode


# ---------------------------------------------------------------------------
# Cost: AI caps
# ---------------------------------------------------------------------------
ai_per_session = SlidingWindowLimiter(AI_PER_SESSION, max(SESSION_MINUTES, 1) * 60)
ai_per_day = SlidingWindowLimiter(AI_PER_DAY, 86400)

_DAY_CAP_MESSAGE = ("The guest demo's AI assistant has reached today's limit. "
                    "Please try again tomorrow, or sign in for full access.")
_SESSION_CAP_MESSAGE = ("This guest session has used its AI questions. "
                        "Start a new guest session or sign in for full access.")


def _session_key(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _spent_cap_message(key: str) -> Optional[str]:
    if ai_per_day.count("all") >= AI_PER_DAY:
        return _DAY_CAP_MESSAGE
    if ai_per_session.count(key) >= AI_PER_SESSION:
        return _SESSION_CAP_MESSAGE
    return None


def enforce(request: Request, mode: Optional[str], token: str) -> None:
    """Apply the guest boundary to one request. Raises HTTPException to refuse.

    Opens the request's AI ticket. An in-process hop from the orchestrator
    carries no bearer token and inherits the outer ticket, so the whole
    request is charged to the guest session that made it."""
    path = request.url.path
    if not path_allowed(path):
        logger.warning(f"[guest] route refused: {request.method} {path}")
        raise HTTPException(status_code=403,
                            detail="This area is not available in the guest demo.")

    ticket = _ticket.get()
    if ticket is None:
        ticket = _GuestTicket(_session_key(token))
        _ticket.set(ticket)

    if is_ai_call(request.method, path, mode) and not ticket.charged:
        message = _spent_cap_message(ticket.key)
        if message:
            logger.warning(f"[guest] AI cap reached: {request.method} {path}")
            raise HTTPException(status_code=429, detail=message)


def charge_llm_call(caller: str) -> None:
    """Gate one model call. Called by MeteredLLM before any provider is
    contacted; a no-op outside guest requests.

    The first model call of a guest request charges one AI question against
    the session and daily caps. Every call counts against the per-request
    ceiling. A guest request without a ticket did not pass enforce() and is
    refused rather than served uncounted."""
    if not _is_guest.get():
        return
    ticket = _ticket.get()
    if ticket is None:
        logger.error(f"[guest] model call without a guest ticket refused: caller={caller}")
        raise GuestAILimitReached("Guest AI is unavailable for this request.")
    with ticket.lock:
        if ticket.calls >= LLM_CALLS_PER_REQUEST:
            logger.warning(f"[guest] per-request model-call ceiling reached: caller={caller}")
            raise GuestAILimitReached("This guest request has reached its AI limit.")
        if not ticket.charged:
            with _charge_lock:
                message = _spent_cap_message(ticket.key)
                if message:
                    logger.warning(f"[guest] AI cap reached at model call: caller={caller}")
                    raise GuestAILimitReached(message)
                ai_per_session.record(ticket.key)
                ai_per_day.record("all")
            ticket.charged = True
            logger.info(f"[guest] AI question charged: caller={caller}")
        ticket.calls += 1


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
