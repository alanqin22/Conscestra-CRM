"""LLM Provider Failover — U5 (round-2 blindspots, 2026-07-25).

WHAT THIS IS FOR
    `llm_provider` was a static, process-wide choice with no alternate. All 39
    `_get_llm` call sites wrap their invoke in a try/except that drops to a
    deterministic (scripted, non-LLM) fallback, so an OpenAI outage never
    errored — it silently collapsed the QUALITY of every agent at once, leaving
    only `llm_usage.ok=false` rows that nothing read until U3.

    So U5 is about CONTINUITY OF INTELLIGENCE during a vendor incident, not
    crash prevention. This module is the policy-aware provider graph that
    decides — deterministically, and before every attempt — whether a given
    request may go to a given provider, and which transient failures earn a
    second opinion from a different vendor.

WHAT IT IS NOT
    Not a retry layer. The OpenAI SDK already retries twice internally; a
    router-level retry against the same provider would multiply latency without
    adding availability. Exactly ONE router attempt per provider.

THE THREE RULES THAT SHAPE EVERYTHING HERE
  1. An exception earns a failover only if it is PROVIDER-SIDE *and* the request
     was not semantically accepted. Our bugs, refusals and budget outcomes are
     not permission to re-send a customer's data to a second vendor.
  2. Policy is evaluated on the DATA CLASS of the request, never on the failure.
     Internal-tier content (IT/HR agents) has no external hop at all — the
     LLM-layer analogue of U2's `reach_invariant`, and equally unforceable.
  3. A provider that is CONFIGURED is not a provider that WORKS. Usability is
     proven by a real generation, never by a catalogue lookup — see health().

CURRENT DEPLOYMENT (2026-07-25)
    Primary OpenAI (gpt-4o-mini) · alternate Google Gemini
    (gemini-3.5-flash-lite: measured 3/3 parse_ai_json compatibility, p50 784ms).
    **The Google key is on the FREE tier**, where content may be used to improve
    Google's products — so `LLM_FAILOVER_ENABLED=0` AND the policy gate refuses
    customer/internal data to a free-tier provider. Two independent locks, on
    purpose: a feature flag protects against forgetting, a policy gate protects
    against a deliberate flip that missed the tier upgrade.

Design + rationale: docs/llm_provider_failover_design.md
Requires sql/llm_usage_failover.sql for per-attempt telemetry.

CONFIG (env) — see the design doc for the full table
  LLM_FAILOVER_ENABLED     0  master switch (OFF until the key is paid-tier)
  LLM_ALT_PROVIDER    gemini  remote alternate
  LLM_ALT_MODEL / _LITE       gemini-3.5-flash-lite (pin GA, never `-latest`)
  LLM_ALT_TIER          free  free|paid — free BLOCKS customer/internal data
  LLM_LOCAL_FALLBACK_ENABLED 0  opt-in Ollama third hop
  LLM_INTERNAL_STRICT      0  1 = internal-tier callers may ONLY use local
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("llm_router")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


ENABLED = _flag("LLM_FAILOVER_ENABLED", "0")
ALT_PROVIDER = os.getenv("LLM_ALT_PROVIDER", "gemini").strip().lower()
ALT_TIER = os.getenv("LLM_ALT_TIER", "free").strip().lower()      # free | paid
# Explicit, informed acknowledgement that a FREE-tier alternate may train on
# this deployment's content. Exists so a synthetic/dev database can exercise
# failover WITHOUT anyone having to write the lie `LLM_ALT_TIER=paid` — a lie
# that would silently follow the config to an environment holding real customer
# data. Loud in the logs and in U3 readiness so it can never become an
# unnoticed production posture.
ALT_TIER_TRAINING_ACK = _flag("LLM_ALT_TIER_TRAINING_ACK", "0")
LOCAL_ENABLED = _flag("LLM_LOCAL_FALLBACK_ENABLED", "0")
INTERNAL_STRICT = _flag("LLM_INTERNAL_STRICT", "0")
ALLOW_EGRESS_FAILOVER = _flag("LLM_ALLOW_EGRESS_FAILOVER", "0")
REQUIRE_ZERO_RETENTION = _flag("LLM_REQUIRE_ZERO_RETENTION", "0")
EXTERNAL_ALLOWED = _flag("LLM_EXTERNAL_ALLOWED", "1")
MIN_ATTEMPT_SECS = _int("LLM_MIN_ATTEMPT_SECS", 6)
HEALTH_TTL_SECS = _int("LLM_HEALTH_TTL_SECS", 900)


# ============================================================================
# DATA CLASSES — policy is decided by what the request CONTAINS
# ============================================================================

INTERNAL_SENSITIVE = "INTERNAL_SENSITIVE"
CUSTOMER_EXTERNAL = "CUSTOMER_EXTERNAL"
BUSINESS_INTERNAL = "BUSINESS_INTERNAL"

# Callers whose prompts carry INTERNAL-tier knowledge (employee IT/HR content).
# Kept as a set of caller names because `caller` is what the meter already
# derives from the calling module — no new plumbing through 39 call sites.
_INTERNAL_CALLERS = {
    c.strip() for c in os.getenv(
        "LLM_LOCAL_ONLY_CALLERS",
        "it-service,people-hr,custom_agents_internal").split(",") if c.strip()
}

_CUSTOMER_CALLERS = {
    "sdr", "auto_reply", "store", "store_catalog", "telephony", "voice_support",
    "custom_agents", "embed", "agent_console", "transports", "promotions",
}


def classify(caller: str, internal: bool = False) -> str:
    """Which data class is this request? `internal=True` is passed explicitly by
    callers that KNOW they are handling internal-tier content (custom_agents
    with scope='internal'); everything else is inferred from the caller name."""
    c = (caller or "").strip().lower()
    if internal or c in _INTERNAL_CALLERS:
        return INTERNAL_SENSITIVE
    if c in _CUSTOMER_CALLERS:
        return CUSTOMER_EXTERNAL
    return BUSINESS_INTERNAL


# ============================================================================
# FAILURE TAXONOMY — an exception is not automatically a reason to fail over
# ============================================================================

# A — transient, provider-side, request not accepted → MAY fail over
NETWORK, TIMEOUT, PROVIDER_5XX, OVERLOADED, RATE_LIMIT = (
    "NETWORK", "TIMEOUT", "PROVIDER_5XX", "OVERLOADED", "RATE_LIMIT")
# B — our fault, or the alternate would fail identically → NEVER fail over
AUTH, BAD_REQUEST, NOT_FOUND, CONTENT_POLICY, CONTEXT_LENGTH, APP_ERROR = (
    "AUTH", "BAD_REQUEST", "NOT_FOUND", "CONTENT_POLICY", "CONTEXT_LENGTH",
    "APP_ERROR")
# C — decisions, not failures
BUDGET_EXHAUSTED, POLICY_FORBIDDEN, TIER_UNAVAILABLE, CAPABILITY_MISMATCH = (
    "BUDGET_EXHAUSTED", "POLICY_FORBIDDEN", "TIER_UNAVAILABLE",
    "CAPABILITY_MISMATCH")

FAILOVER_ELIGIBLE = {NETWORK, TIMEOUT, PROVIDER_5XX, OVERLOADED, RATE_LIMIT}

_STATUS_MAP = {
    400: BAD_REQUEST, 401: AUTH, 403: AUTH, 404: NOT_FOUND,
    413: CONTEXT_LENGTH, 422: BAD_REQUEST, 429: RATE_LIMIT,
    500: PROVIDER_5XX, 502: PROVIDER_5XX, 503: OVERLOADED,
    504: TIMEOUT, 529: OVERLOADED,
}

# Message matching is the LAST resort — brittle and locale-dependent — so it is
# only consulted when the exception carries neither a type nor a status we know.
_MSG_HINTS = (
    (re.compile(r"content[_ ]policy|safety|refus|blocked by", re.I), CONTENT_POLICY),
    (re.compile(r"context length|too many tokens|maximum context", re.I), CONTEXT_LENGTH),
    (re.compile(r"timed? ?out|deadline exceeded", re.I), TIMEOUT),
    (re.compile(r"rate.?limit|quota|resource_exhausted|too many requests", re.I), RATE_LIMIT),
    (re.compile(r"unauthenticated|invalid.{0,12}(api.?key|credential)|permission denied", re.I), AUTH),
    (re.compile(r"connect|dns|unreachable|ssl|network", re.I), NETWORK),
    (re.compile(r"overload|unavailable", re.I), OVERLOADED),
)


def classify_failure(exc: BaseException) -> str:
    """Map an exception to a failure class. Type first, status second, message
    LAST — message text is the least reliable signal and is only reached when
    nothing structured is available."""
    name = type(exc).__name__
    # Our own control-flow signals.
    if name == "LLMBudgetExceeded":
        return BUDGET_EXHAUSTED
    if isinstance(exc, (TypeError, KeyError, AttributeError, IndexError,
                        NotImplementedError, AssertionError)):
        return APP_ERROR
    # Provider SDK exception names (OpenAI + Anthropic share this vocabulary).
    by_name = {
        "APITimeoutError": TIMEOUT, "Timeout": TIMEOUT, "ReadTimeout": TIMEOUT,
        "ConnectTimeout": NETWORK, "APIConnectionError": NETWORK,
        "ConnectError": NETWORK, "ServiceUnavailable": OVERLOADED,
        "InternalServerError": PROVIDER_5XX, "RateLimitError": RATE_LIMIT,
        "AuthenticationError": AUTH, "PermissionDeniedError": AUTH,
        "BadRequestError": BAD_REQUEST, "UnprocessableEntityError": BAD_REQUEST,
        "NotFoundError": NOT_FOUND,
    }
    if name in by_name:
        return by_name[name]
    status = (getattr(exc, "status_code", None) or getattr(exc, "code", None)
              or getattr(getattr(exc, "response", None), "status_code", None))
    if isinstance(status, int) and status in _STATUS_MAP:
        return _STATUS_MAP[status]
    text = str(exc)[:400]
    for rx, cls in _MSG_HINTS:
        if rx.search(text):
            return cls
    return PROVIDER_5XX      # unknown provider-side error: treat as transient


# ============================================================================
# REDACTION — telemetry must never carry prompt or customer content
# ============================================================================

_SCRUB = (
    (re.compile(r"\b(sk|ek|whsec|ghp|gsk)[-_][A-Za-z0-9_\-]{8,}"), "<key>"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I), "Bearer <token>"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{10,}"), "<key>"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b\d{7,}\b"), "<num>"),
)


def redact(text: str, limit: int = 120) -> str:
    """Exception class + status + a short, scrubbed message. Provider errors can
    echo request content, so this runs on EVERY string that reaches the log or
    the usage table."""
    out = str(text or "")
    for rx, sub in _SCRUB:
        out = rx.sub(sub, out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit]


# ============================================================================
# PROVIDER REGISTRY — providers are NOT interchangeable, so say how they differ
# ============================================================================

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "kind": "remote", "vendor": "OpenAI", "egress": True,
        "env_key": "OPENAI_API_KEY",
        "caps": {"connect": 5, "interactive": 12, "async": 30, "background": 60},
        "profile": {"json_in_prose": "high", "relative_cost": 1.0,
                    "style": "baseline"},
    },
    "gemini": {
        "kind": "remote", "vendor": "Google", "egress": True,
        "env_key": "GOOGLE_API_KEY",
        "caps": {"connect": 5, "interactive": 15, "async": 25, "background": 45},
        "profile": {"json_in_prose": "high (measured 3/3 on parse_ai_json)",
                    "relative_cost": 0.5,
                    "style": "differs — different length calibration",
                    "measured_p50_ms": 784},
    },
    "ollama": {
        "kind": "local", "vendor": "self-hosted", "egress": False,
        "env_key": None,
        # Local models are legitimately slow: connect fast-fails, generation doesn't.
        "caps": {"connect": 2, "interactive": 0, "async": 0, "background": 120},
        "profile": {"json_in_prose": "medium", "relative_cost": 0.0,
                    "style": "differs markedly", "relative_latency": 6.0},
    },
}

# Ordered provider graph per data class. NOT one linear chain — the whole point.
_GRAPH = {
    CUSTOMER_EXTERNAL: ["primary", "alt", "local"],
    BUSINESS_INTERNAL: ["primary", "alt", "local"],
    # Local is PREFERRED for internal-tier content, but the remote hops are
    # still listed so `may_send` — not the graph — decides whether they are
    # permitted. A local-only graph silently bypassed LLM_INTERNAL_STRICT and
    # left the IT/HR agents with NO provider at all when Ollama wasn't
    # provisioned ("no provider was permitted"), which is a worse outcome than
    # the legacy behaviour it was meant to preserve. With strict mode ON the
    # gate refuses the remote hops; with it OFF they work exactly as before U5.
    INTERNAL_SENSITIVE: ["local", "primary", "alt"],
}

LATENCY_CLASS = {
    "interactive": _int("LLM_DEADLINE_INTERACTIVE", 30),
    "async": _int("LLM_DEADLINE_ASYNC", 75),
    "background": _int("LLM_DEADLINE_BACKGROUND", 240),
}

_INTERACTIVE_CALLERS = {"sdr", "store", "store_catalog", "custom_agents",
                        "embed", "agent_console", "voice_support", "voice_stream"}
_ASYNC_CALLERS = {"auto_reply", "telephony", "transports", "email"}


def latency_class(caller: str) -> str:
    c = (caller or "").lower()
    if c in _INTERACTIVE_CALLERS:
        return "interactive"
    if c in _ASYNC_CALLERS:
        return "async"
    return "background"


# ============================================================================
# POLICY GATE — "may THIS request go to THIS provider?"
# ============================================================================

def may_send(data_class: str, provider: str) -> Tuple[bool, str]:
    """Answered BEFORE every attempt, including the first. A denial is a
    decision (POLICY_FORBIDDEN), not an error."""
    spec = PROVIDERS.get(provider)
    if not spec:
        return False, f"unknown provider '{provider}'"
    external = bool(spec.get("egress"))

    # 1. Internal-tier content never leaves the deployment. This is the LLM-layer
    #    analogue of U2's reach_invariant and is deliberately unforceable: an
    #    outage is not a reason to export employee IT/HR knowledge.
    if data_class == INTERNAL_SENSITIVE and external:
        if INTERNAL_STRICT:
            return False, ("internal-tier content may not be sent to an external "
                           "provider (LLM_INTERNAL_STRICT=1)")
        # Default OFF so enabling U5 does not silently change TODAY's behaviour:
        # these callers already run on OpenAI as their primary. Flipping strict
        # mode on is a deliberate migration once a local model is provisioned.
        logger.debug("[llm_router] internal caller on external provider "
                     "(LLM_INTERNAL_STRICT=0 — permitted, legacy behaviour)")

    if external:
        # 2. A FREE-tier provider may be used to train on our prompts. Customer
        #    and internal data must never reach one, whatever the feature flag
        #    says — a flag protects against forgetting, this protects against a
        #    deliberate flip that missed the tier upgrade.
        if provider == ALT_PROVIDER and ALT_TIER != "paid" \
                and not ALT_TIER_TRAINING_ACK:
            if data_class in (CUSTOMER_EXTERNAL, INTERNAL_SENSITIVE):
                return False, (f"{provider} is configured as a FREE tier, whose "
                               "content may be used to train the provider's "
                               "models — customer/internal data is refused "
                               "(set LLM_ALT_TIER=paid once upgraded, or "
                               "LLM_ALT_TIER_TRAINING_ACK=1 for a synthetic "
                               "dataset you accept being trained on)")
        # 3. Residency.
        if not EXTERNAL_ALLOWED:
            return False, "external providers disabled (LLM_EXTERNAL_ALLOWED=0)"
        region = os.getenv("DATA_REGION", "").strip().lower()
        if region.startswith("ca") and not _flag("LLM_EXTERNAL_REGION_OK", "0"):
            return False, (f"DATA_REGION={region} requires an in-region "
                           "provider; this one is not attested")
        # 4. Zero retention.
        if REQUIRE_ZERO_RETENTION and not _flag(
                f"LLM_ZDR_{provider.upper()}", "0"):
            return False, f"{provider} has no attested zero-retention agreement"

    # 5. Local→external egress is opt-in (preserved for an Ollama-primary future).
    primary = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if external and PROVIDERS.get(primary, {}).get("kind") == "local" \
            and not ALLOW_EGRESS_FAILOVER:
        return False, ("local→external failover is disabled "
                       "(LLM_ALLOW_EGRESS_FAILOVER=0)")
    return True, "allowed"


# ============================================================================
# HEALTH — configured is not the same as usable
# ============================================================================

_health_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def health(provider: str, model: Optional[str] = None,
           force: bool = False) -> Dict[str, Any]:
    """Is this provider+model actually usable RIGHT NOW?

    Proven by a minimal real generation, never by a catalogue lookup. Found the
    hard way 2026-07-25: `gemini-2.5-flash` and `gemini-2.5-flash-lite` are both
    LISTED by Google's models endpoint and both 404 on generateContent with our
    key — a list-membership check would have passed them and still failed at
    runtime, which is the same failure mode as the misconfigured `gpt-oss:20b`
    Ollama model. Cached with a TTL; never runs in the hot request path."""
    key = f"{provider}:{model or ''}"
    now = time.time()
    if not force:
        hit = _health_cache.get(key)
        if hit and now - hit[0] < HEALTH_TTL_SECS:
            return hit[1]

    spec = PROVIDERS.get(provider) or {}
    res: Dict[str, Any] = {"provider": provider, "model": model,
                           "reachable": False, "usable": False,
                           "checked_at": now, "detail": ""}
    env_key = spec.get("env_key")
    if env_key and not os.getenv(env_key):
        res["detail"] = f"{env_key} not set"
        _health_cache[key] = (now, res)
        return res
    try:
        import httpx
        if provider == "gemini":
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent")
            r = httpx.post(url, headers={"x-goog-api-key": os.getenv("GOOGLE_API_KEY", "")},
                           json={"contents": [{"role": "user",
                                               "parts": [{"text": "ping"}]}],
                                 "generationConfig": {"maxOutputTokens": 8}},
                           timeout=15)
            res["reachable"] = True
            res["usable"] = (r.status_code == 200)
            res["detail"] = ("ok" if r.status_code == 200
                             else f"HTTP {r.status_code}: {redact(r.text, 80)}")
        elif provider == "ollama":
            base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
            r = httpx.get(f"{base}/api/tags", timeout=5)
            res["reachable"] = (r.status_code == 200)
            names = [m.get("name", "") for m in (r.json().get("models") or [])] \
                if r.status_code == 200 else []
            res["usable"] = bool(model) and any(
                n == model or n.split(":")[0] == str(model).split(":")[0]
                for n in names)
            res["detail"] = ("ok" if res["usable"] else
                             f"model '{model}' not installed; have: {names[:4]}")
        elif provider == "openai":
            # The primary proves itself with live traffic; a synthetic probe here
            # would spend money to learn what the next real call learns for free.
            res["reachable"] = res["usable"] = bool(os.getenv("OPENAI_API_KEY"))
            res["detail"] = "key present (primary proven by live traffic)"
        else:
            res["detail"] = "no probe implemented"
    except Exception as exc:
        res["detail"] = f"{type(exc).__name__}: {redact(exc, 80)}"
    _health_cache[key] = (now, res)
    return res


def alt_model_for(tier: str) -> str:
    return (os.getenv("LLM_ALT_MODEL_LITE", "gemini-3.5-flash-lite")
            if tier == "lite"
            else os.getenv("LLM_ALT_MODEL", "gemini-3.5-flash-lite"))


def local_model_for(tier: str) -> str:
    return (os.getenv("LLM_LOCAL_MODEL_LITE", "") or
            os.getenv("LLM_LOCAL_MODEL", "")) if tier == "lite" \
        else os.getenv("LLM_LOCAL_MODEL", "")


def readiness() -> Dict[str, Any]:
    """Failover readiness for U3 Platform Health — surfaces a misconfigured
    target BEFORE an outage rather than during one."""
    out: Dict[str, Any] = {"enabled": ENABLED, "alt_provider": ALT_PROVIDER,
                           "alt_tier": ALT_TIER,
                           "training_ack": ALT_TIER_TRAINING_ACK, "targets": []}
    if ALT_TIER != "paid" and ALT_TIER_TRAINING_ACK:
        out["warning"] = (
            f"{ALT_PROVIDER} is a FREE tier and content MAY BE USED FOR "
            "TRAINING — accepted via LLM_ALT_TIER_TRAINING_ACK=1. This is only "
            "appropriate for synthetic/dev data. Do NOT carry this setting to "
            "an environment holding real customer data.")
    if not ENABLED:
        out["note"] = ("failover disabled (LLM_FAILOVER_ENABLED=0)"
                       + ("; alternate is FREE tier — upgrade before enabling"
                          if ALT_TIER != "paid" else ""))
        return out
    for tier in ("standard", "lite"):
        m = alt_model_for(tier)
        h = health(ALT_PROVIDER, m)
        out["targets"].append({"tier": tier, "provider": ALT_PROVIDER,
                               "model": m, "usable": h["usable"],
                               "detail": h["detail"]})
    if LOCAL_ENABLED:
        m = local_model_for("standard")
        h = health("ollama", m)
        out["targets"].append({"tier": "local", "provider": "ollama",
                               "model": m or "(unset)", "usable": h["usable"],
                               "detail": h["detail"]})
    out["all_usable"] = all(t["usable"] for t in out["targets"]) if out["targets"] else False
    return out


# ============================================================================
# THE ROUTER
# ============================================================================

class _NormalizedResponse:
    """A response whose `.content` is a plain string, with everything else
    (usage_metadata, response_metadata, id, …) passed straight through."""

    __slots__ = ("_inner", "content")

    def __init__(self, inner: Any, content: str):
        self._inner = inner
        self.content = content

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __repr__(self) -> str:                       # pragma: no cover
        return f"<NormalizedResponse {self.content[:40]!r}>"


def normalize_response(resp: Any) -> Any:
    """Make every provider's response look like the one all 39 call sites expect.

    THE BUG THIS FIXES (found live 2026-07-25, and it would have made U5 useless):
    every caller does `(resp.content if hasattr(resp,'content') else str(resp)).strip()`.
    OpenAI and Ollama return `.content` as a str, but Gemini returns a LIST of
    content blocks — `[{'type':'text','text':'…','extras':{…}}]` — so `.strip()`
    raises AttributeError, the caller's own `except` swallows it, and the reply
    silently drops to the scripted deterministic fallback. Failover would have
    reported success at the router level while the customer still got the
    degraded answer U5 exists to prevent.

    Idempotent: a string `.content` is returned untouched."""
    content = getattr(resp, "content", None)
    if content is None or isinstance(content, str):
        return resp
    text = ""
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                # Only TEXT blocks become the reply. Thinking/signature/extras
                # are provider bookkeeping and must never reach a customer.
                if b.get("type") in (None, "text") and b.get("text"):
                    parts.append(str(b["text"]))
            else:
                t = getattr(b, "text", None)
                if t:
                    parts.append(str(t))
        text = "".join(parts)
    if not text:
        # LangChain exposes a .text helper on some versions; last resort only.
        t = getattr(resp, "text", None)
        text = (t() if callable(t) else t) or ""
        if not isinstance(text, str):
            text = str(text)
    return _NormalizedResponse(resp, text)


class Attempt:
    """One provider attempt's telemetry."""

    def __init__(self, n: int, provider: str, model: str):
        self.n, self.provider, self.model = n, provider, model
        self.ok = False
        self.failure_class: Optional[str] = None
        self.failure_reason: Optional[str] = None
        self.policy_reason: Optional[str] = None
        self.latency_ms = 0


class RouteResult:
    def __init__(self, logical_id: str):
        self.logical_id = logical_id
        self.attempts: List[Attempt] = []
        self.response: Any = None
        self.outcome = "fallback"
        self.error: Optional[BaseException] = None
        self.total_latency_ms = 0

    @property
    def failed_over(self) -> bool:
        return self.outcome == "failed_over_ok"


def route(caller: str, tier: str, data_class: str,
          invoke: Callable[[str, str, float], Any],
          primary_provider: str, primary_model: str) -> RouteResult:
    """Drive one LOGICAL REQUEST across the allowed provider graph.

    `invoke(provider, model, timeout) -> response` is supplied by the caller so
    this module stays free of provider SDKs and is trivially testable with fakes.
    Budget is NOT checked here — llm_meter decides that once, before the router
    is entered, so a failover can never be a second bite at a spent budget.
    """
    res = RouteResult(str(uuid.uuid4()))
    lc = latency_class(caller)
    deadline = LATENCY_CLASS[lc]
    started = time.time()

    hops: List[Tuple[str, str]] = []
    for slot in _GRAPH.get(data_class, [CUSTOMER_EXTERNAL]):
        if slot == "primary":
            hops.append((primary_provider, primary_model))
        elif slot == "alt" and ENABLED:
            hops.append((ALT_PROVIDER, alt_model_for(tier)))
        elif slot == "local" and (LOCAL_ENABLED or data_class == INTERNAL_SENSITIVE):
            lm = local_model_for(tier)
            if lm:
                hops.append(("ollama", lm))

    n = 0
    for provider, model in hops:
        n += 1
        att = Attempt(n, provider, model)

        allowed, why = may_send(data_class, provider)
        if not allowed:
            att.failure_class, att.policy_reason = POLICY_FORBIDDEN, why
            res.attempts.append(att)
            logger.info(f"[llm_router] {provider} refused for {data_class}: {why}")
            continue

        if n > 1:      # the primary is proven by live traffic; alternates are not
            h = health(provider, model)
            if not h["usable"]:
                att.failure_class = TIER_UNAVAILABLE
                att.failure_reason = redact(h["detail"], 80)
                res.attempts.append(att)
                logger.warning(f"[llm_router] {provider}/{model} unusable — "
                               f"skipped: {h['detail'][:80]}")
                continue

        remaining = deadline - (time.time() - started)
        cap = PROVIDERS.get(provider, {}).get("caps", {}).get(lc, 0) or 0
        if cap <= 0 or remaining < MIN_ATTEMPT_SECS:
            att.failure_class = TIER_UNAVAILABLE
            att.policy_reason = (f"no time left ({remaining:.1f}s < "
                                 f"{MIN_ATTEMPT_SECS}s)" if cap > 0 else
                                 f"{provider} not permitted on the {lc} path")
            res.attempts.append(att)
            continue
        timeout = min(cap, remaining)

        t0 = time.time()
        try:
            # Normalize BEFORE marking success: every provider must hand the
            # caller the same shape, or a "successful" failover still lands in
            # the caller's deterministic fallback.
            att_resp = normalize_response(invoke(provider, model, timeout))
            att.ok = True
            att.latency_ms = int((time.time() - t0) * 1000)
            res.attempts.append(att)
            res.response = att_resp
            res.outcome = "failed_over_ok" if n > 1 else "ok"
            res.total_latency_ms = int((time.time() - started) * 1000)
            if n > 1:
                logger.warning(
                    f"[llm_router] FAILED OVER {primary_provider}→{provider} "
                    f"for caller={caller} ({data_class}) after "
                    f"{res.attempts[0].failure_class}; total "
                    f"{res.total_latency_ms}ms")
            return res
        except BaseException as exc:            # noqa: BLE001 — classified below
            att.latency_ms = int((time.time() - t0) * 1000)
            att.failure_class = classify_failure(exc)
            att.failure_reason = f"{type(exc).__name__}: {redact(exc)}"
            res.attempts.append(att)
            res.error = exc
            if att.failure_class not in FAILOVER_ELIGIBLE:
                # Class B/C: our fault, a refusal, or a decision. Re-raise now —
                # sending the same request to a second vendor would either fail
                # identically or launder a policy refusal.
                res.outcome = ("budget" if att.failure_class == BUDGET_EXHAUSTED
                               else "fallback")
                res.total_latency_ms = int((time.time() - started) * 1000)
                # Attach the partial route so the meter still records every
                # attempt — a failed attempt spent tokens and belongs in cost.
                try:
                    exc.route_result = res       # type: ignore[attr-defined]
                except Exception:
                    pass
                raise
            logger.info(f"[llm_router] {provider} attempt {n} failed "
                        f"({att.failure_class}) — trying next provider")

    res.total_latency_ms = int((time.time() - started) * 1000)
    res.outcome = "policy" if all(
        a.failure_class == POLICY_FORBIDDEN for a in res.attempts) and res.attempts \
        else "fallback"
    exc: BaseException = res.error or RuntimeError(
        "no provider was permitted for this request: "
        + "; ".join(f"{a.provider}={a.policy_reason or a.failure_class}"
                    for a in res.attempts))
    try:
        exc.route_result = res                   # type: ignore[attr-defined]
    except Exception:
        pass
    raise exc


router = None       # set below, after status() is defined


def status() -> Dict[str, Any]:
    return {
        "enabled": ENABLED, "alt_provider": ALT_PROVIDER, "alt_tier": ALT_TIER,
        "local_enabled": LOCAL_ENABLED, "internal_strict": INTERNAL_STRICT,
        "egress_failover": ALLOW_EGRESS_FAILOVER,
        "deadlines": LATENCY_CLASS,
        "providers": {k: {"kind": v["kind"], "vendor": v["vendor"],
                          "caps": v["caps"], "profile": v["profile"]}
                      for k, v in PROVIDERS.items()},
        "readiness": readiness(),
    }


# ============================================================================
# Router (admin) — pre-incident inspection
# ============================================================================

from fastapi import APIRouter                                   # noqa: E402

router = APIRouter(tags=["llm-providers"])


@router.get("/llm/providers")
def api_providers():
    """Provider posture + failover readiness. The point is to be able to answer
    'would failover actually work right now?' BEFORE an outage."""
    return status()


@router.post("/llm/providers/health")
def api_health(body: Dict[str, Any]):
    """Force a fresh usability probe (bypasses the TTL cache). A real minimal
    generation — listing a model is not proof it works."""
    b = body or {}
    provider = str(b.get("provider") or ALT_PROVIDER)
    model = b.get("model") or alt_model_for(str(b.get("tier") or "standard"))
    return health(provider, model, force=True)
