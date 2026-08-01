"""U5 — LLM provider failover: adversarial + regression tests.

Everything here uses INJECTED FAKE PROVIDERS. No test in this file makes a real
provider call, so the suite is deterministic, free, and CI-safe.

The properties being pinned down are the ones that would be dangerous to get
wrong silently:
  • a transient provider failure earns a second opinion;
  • our own bugs, refusals and budget outcomes NEVER do;
  • internal-tier content never egresses;
  • a FREE-tier provider never receives customer data;
  • telemetry can still distinguish a logical request from a provider attempt.

    python -m pytest tests/test_llm_failover.py -v
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("DB_DSN", "postgresql://postgres:aria@localhost:5434/crmdb")

from app.core import llm_router as R          # noqa: E402


# ------------------------------------------------------------------ fakes ---

class FakeResp:
    def __init__(self, text="ok"):
        self.content = text


class Boom(Exception):
    """Provider exception with a settable status, like the real SDK errors."""

    def __init__(self, msg="boom", status=None, name=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status
        if name:
            self.__class__ = type(name, (Boom,), {})


def make_invoke(script):
    """script: {provider: 'ok' | Exception}. Records the call order."""
    calls = []

    def _invoke(provider, model, timeout):
        calls.append((provider, model, round(timeout)))
        outcome = script.get(provider, "ok")
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResp(f"{provider}:{outcome}")

    _invoke.calls = calls
    return _invoke


@pytest.fixture
def env(monkeypatch):
    """Baseline: failover ON, alternate PAID, local off — then each test varies
    only what it is about."""
    monkeypatch.setattr(R, "ENABLED", True)
    monkeypatch.setattr(R, "ALT_PROVIDER", "gemini")
    monkeypatch.setattr(R, "ALT_TIER", "paid")
    # Pinned OFF so the suite asserts DEFAULT behaviour regardless of whatever
    # the developer's local .env happens to set. A test that silently inherits
    # local config stops testing the shipped default.
    monkeypatch.setattr(R, "ALT_TIER_TRAINING_ACK", False)
    monkeypatch.setattr(R, "LOCAL_ENABLED", False)
    monkeypatch.setattr(R, "INTERNAL_STRICT", False)
    monkeypatch.setattr(R, "ALLOW_EGRESS_FAILOVER", False)
    monkeypatch.setattr(R, "REQUIRE_ZERO_RETENTION", False)
    monkeypatch.setattr(R, "EXTERNAL_ALLOWED", True)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("DATA_REGION", "")
    monkeypatch.setenv("LLM_ALT_MODEL_LITE", "gemini-3.5-flash-lite")
    monkeypatch.setenv("LLM_ALT_MODEL", "gemini-3.5-flash-lite")
    # Alternates are health-gated; make them usable unless a test says otherwise.
    monkeypatch.setattr(R, "health", lambda p, m=None, force=False: {
        "provider": p, "model": m, "reachable": True, "usable": True,
        "checked_at": time.time(), "detail": "ok"})
    return monkeypatch


def run(invoke, caller="sdr", tier="lite", data_class=R.CUSTOMER_EXTERNAL):
    return R.route(caller, tier, data_class, invoke, "openai", "gpt-4o-mini")


# ------------------------------------------------------------- happy path ---

def test_01_primary_succeeds(env):
    inv = make_invoke({"openai": "ok"})
    res = run(inv)
    assert res.outcome == "ok"
    assert len(res.attempts) == 1 and res.attempts[0].ok
    assert [c[0] for c in inv.calls] == ["openai"], "alternate must not be called"


def test_02_transient_5xx_fails_over(env):
    inv = make_invoke({"openai": Boom("upstream", status=500)})
    res = run(inv)
    assert res.outcome == "failed_over_ok"
    assert res.failed_over
    assert [c[0] for c in inv.calls] == ["openai", "gemini"]
    assert res.attempts[0].failure_class == R.PROVIDER_5XX
    assert res.attempts[1].ok


def test_03_primary_timeout_fails_over(env):
    inv = make_invoke({"openai": Boom("timed out", name="APITimeoutError")})
    res = run(inv)
    assert res.attempts[0].failure_class == R.TIMEOUT
    assert res.outcome == "failed_over_ok"


def test_04_rate_limit_fails_over(env):
    inv = make_invoke({"openai": Boom("429 rate limit", status=429)})
    res = run(inv)
    assert res.attempts[0].failure_class == R.RATE_LIMIT
    assert res.outcome == "failed_over_ok"


def test_05_local_third_hop_when_enabled(env):
    env.setattr(R, "LOCAL_ENABLED", True)
    env.setenv("LLM_LOCAL_MODEL", "gemma4:31b")
    env.setenv("LLM_LOCAL_MODEL_LITE", "gemma4:31b")
    inv = make_invoke({"openai": Boom(status=503), "gemini": Boom(status=500)})
    res = run(inv, caller="planner")          # background class: local allowed
    assert [c[0] for c in inv.calls] == ["openai", "gemini", "ollama"]
    assert res.outcome == "failed_over_ok"


def test_06_all_providers_fail_raises_for_caller_fallback(env):
    inv = make_invoke({"openai": Boom(status=500), "gemini": Boom(status=503)})
    with pytest.raises(Exception) as ei:
        run(inv)
    # The caller's existing try/except sees a provider exception, so all 39
    # deterministic fallbacks behave exactly as before U5.
    assert getattr(ei.value, "route_result", None) is not None
    assert len(ei.value.route_result.attempts) == 2


# ------------------------------------------------- must NEVER fail over ---

@pytest.mark.parametrize("exc,cls", [
    (Boom("bad key", status=401), R.AUTH),
    (Boom("forbidden", status=403), R.AUTH),
    (Boom("malformed", status=400), R.BAD_REQUEST),
    (Boom("unknown model", status=404), R.NOT_FOUND),
    (Boom("blocked by content policy"), R.CONTENT_POLICY),
    (Boom("maximum context length exceeded"), R.CONTEXT_LENGTH),
])
def test_07_class_b_never_fails_over(env, exc, cls):
    inv = make_invoke({"openai": exc})
    with pytest.raises(Exception):
        run(inv)
    assert [c[0] for c in inv.calls] == ["openai"], \
        f"{cls} must not reach a second provider"
    assert inv.calls and len(inv.calls) == 1


def test_08_content_policy_refusal_is_not_laundered(env):
    """A refusal re-sent to another vendor to get a different answer is policy
    laundering. It must stop at the first provider."""
    inv = make_invoke({"openai": Boom("Request blocked by content policy")})
    with pytest.raises(Exception) as ei:
        run(inv)
    assert ei.value.route_result.attempts[0].failure_class == R.CONTENT_POLICY
    assert len(inv.calls) == 1


def test_09_app_error_surfaces_not_masked(env):
    inv = make_invoke({"openai": TypeError("our bug")})
    with pytest.raises(TypeError):
        run(inv)
    assert len(inv.calls) == 1


def test_10_budget_exhaustion_is_not_a_provider_failure(env):
    from app.core.llm_meter import LLMBudgetExceeded
    assert R.classify_failure(LLMBudgetExceeded("over")) == R.BUDGET_EXHAUSTED
    assert R.BUDGET_EXHAUSTED not in R.FAILOVER_ELIGIBLE


# ----------------------------------------------------------- policy gate ---

def test_11_internal_content_never_egresses(env):
    env.setattr(R, "INTERNAL_STRICT", True)
    env.setattr(R, "LOCAL_ENABLED", True)
    env.setenv("LLM_LOCAL_MODEL", "gemma4:31b")
    inv = make_invoke({"ollama": "ok"})
    res = R.route("it-service", "lite", R.INTERNAL_SENSITIVE, inv,
                  "openai", "gpt-4o-mini")
    providers = [c[0] for c in inv.calls]
    assert providers == ["ollama"], "internal-tier must never reach a remote provider"
    assert res.outcome == "ok"


def test_12_free_tier_provider_refuses_customer_data(env):
    """The live condition: the Google key is FREE tier, where content may be
    used to train. A feature flag protects against forgetting; this protects
    against a deliberate flip that missed the tier upgrade."""
    env.setattr(R, "ALT_TIER", "free")
    ok, why = R.may_send(R.CUSTOMER_EXTERNAL, "gemini")
    assert ok is False and "FREE tier" in why

    inv = make_invoke({"openai": Boom(status=500)})
    with pytest.raises(Exception) as ei:
        run(inv)
    assert [c[0] for c in inv.calls] == ["openai"], "no customer data to a free tier"
    assert ei.value.route_result.attempts[1].failure_class == R.POLICY_FORBIDDEN


def test_13_free_tier_still_allowed_for_business_internal(env):
    """Our own operational prompts (briefings, planning) are not customer data,
    so a free tier is a defensible choice for them — the gate is about WHAT the
    request contains, not about the failure."""
    env.setattr(R, "ALT_TIER", "free")
    ok, _ = R.may_send(R.BUSINESS_INTERNAL, "gemini")
    assert ok is True


def test_14_egress_failover_disabled_by_default(env):
    """A local-primary deployment must not silently export data on an outage."""
    env.setenv("LLM_PROVIDER", "ollama")
    env.setattr(R, "ALLOW_EGRESS_FAILOVER", False)
    ok, why = R.may_send(R.BUSINESS_INTERNAL, "gemini")
    assert ok is False and "egress" in why.lower()


def test_15_residency_blocks_external(env):
    env.setenv("DATA_REGION", "canada-central")
    ok, why = R.may_send(R.BUSINESS_INTERNAL, "gemini")
    assert ok is False and "DATA_REGION" in why


# ---------------------------------------------- health, timeouts, telemetry --

def test_16_unusable_model_detected_before_outage(env):
    """The gemini-2.5-flash case: LISTED by the provider, 404 on use. It must be
    skipped by health, not discovered by burning the deadline."""
    env.setattr(R, "health", lambda p, m=None, force=False: {
        "provider": p, "model": m, "reachable": True, "usable": (p != "gemini"),
        "checked_at": time.time(), "detail": "HTTP 404: model not found"})
    inv = make_invoke({"openai": Boom(status=500)})
    with pytest.raises(Exception) as ei:
        run(inv)
    assert [c[0] for c in inv.calls] == ["openai"], "must not call an unusable model"
    assert ei.value.route_result.attempts[1].failure_class == R.TIER_UNAVAILABLE


def test_17_primary_cap_leaves_room_for_alternate(env):
    """The v2 bug this pins shut: interactive deadline 30s with a 30s primary
    cap meant a primary timeout ate the whole budget and failover was
    unreachable. The primary cap must be <= 40% of the deadline."""
    for cls, deadline in R.LATENCY_CLASS.items():
        cap = R.PROVIDERS["openai"]["caps"].get(cls, 0)
        if cap:
            assert cap <= deadline * 0.4 + 0.01, (
                f"{cls}: primary cap {cap}s vs deadline {deadline}s leaves no "
                "room for an alternate")


def test_18_provider_specific_timeouts(env):
    inv = make_invoke({"openai": Boom(status=500)})
    run(inv, caller="sdr")                       # interactive
    caps = dict((c[0], c[2]) for c in inv.calls)
    assert caps["openai"] <= 12
    assert caps["gemini"] <= 15


def test_19_local_never_on_interactive_path(env):
    """A 31B local model cannot answer inside a 30s interactive budget behind a
    failed primary — so it is not offered there."""
    env.setattr(R, "LOCAL_ENABLED", True)
    env.setenv("LLM_LOCAL_MODEL_LITE", "gemma4:31b")
    inv = make_invoke({"openai": Boom(status=500), "gemini": Boom(status=500)})
    with pytest.raises(Exception):
        run(inv, caller="sdr")                   # interactive
    assert "ollama" not in [c[0] for c in inv.calls]


def test_20_logical_request_and_attempt_telemetry(env):
    inv = make_invoke({"openai": Boom(status=500)})
    res = run(inv)
    assert res.logical_id
    assert [a.n for a in res.attempts] == [1, 2]
    # `failover` is DERIVED from attempt_number (attempt 1 is never a failover),
    # not stored twice — one source of truth for the telemetry writer.
    assert [a.n > 1 for a in res.attempts] == [False, True]
    assert res.total_latency_ms >= 0
    # exactly one attempt succeeded, and it is the last
    assert sum(1 for a in res.attempts if a.ok) == 1 and res.attempts[-1].ok


def test_21_redaction_scrubs_secrets_and_pii(env):
    dirty = ("AuthenticationError: key sk-abcdef0123456789 for bob@acme.com "
             "failed, account 12345678901, Bearer eyJhbGciOiJIUzI1")
    clean = R.redact(dirty, limit=300)
    for leak in ("sk-abcdef0123456789", "bob@acme.com", "12345678901",
                 "eyJhbGciOiJIUzI1"):
        assert leak not in clean, f"{leak} leaked into telemetry"
    assert "<key>" in clean and "<email>" in clean


def test_22_failure_reason_never_carries_prompt(env):
    prompt = "Customer Jane Doe asked about invoice INV-99887766 for $42,000"
    inv = make_invoke({"openai": Boom(f"400 bad request: {prompt}", status=400)})
    with pytest.raises(Exception) as ei:
        run(inv)
    reason = ei.value.route_result.attempts[0].failure_reason or ""
    assert len(reason) <= 160
    assert "99887766" not in reason              # long digit runs scrubbed


def test_23_disabled_router_is_a_noop(env):
    """With U5 off the graph is primary-only — today's exact behaviour."""
    env.setattr(R, "ENABLED", False)
    inv = make_invoke({"openai": Boom(status=500)})
    with pytest.raises(Exception):
        run(inv)
    assert [c[0] for c in inv.calls] == ["openai"]


# ------------------------------------------- response-shape normalization ---
# Found live 2026-07-25: Gemini returns .content as a LIST of content blocks
# while OpenAI/Ollama return a str. Every one of the 39 call sites does
# `resp.content.strip()`, so without normalization a "successful" failover
# still lands in the caller's deterministic fallback — U5 would look like it
# worked while buying nothing.

class ListContentResp:
    """A Gemini-shaped response."""

    def __init__(self, blocks):
        self.content = blocks
        self.usage_metadata = {"input_tokens": 6, "output_tokens": 1}


def test_24_list_content_is_normalized_to_string():
    r = R.normalize_response(ListContentResp(
        [{"type": "text", "text": "HELLO", "extras": {"signature": "abc"}}]))
    assert isinstance(r.content, str)
    assert r.content == "HELLO"
    assert r.content.strip() == "HELLO"           # what all 39 call sites do


def test_25_normalization_drops_non_text_blocks():
    """Thinking/signature blocks are provider bookkeeping and must never reach
    a customer."""
    r = R.normalize_response(ListContentResp([
        {"type": "thinking", "thinking": "internal deliberation"},
        {"type": "text", "text": "Yes, we ship to Ontario."},
    ]))
    assert r.content == "Yes, we ship to Ontario."
    assert "deliberation" not in r.content


def test_26_normalization_preserves_usage_metadata():
    """Token accounting must survive normalization or cost telemetry breaks."""
    r = R.normalize_response(ListContentResp([{"type": "text", "text": "hi"}]))
    assert r.usage_metadata["input_tokens"] == 6


def test_27_normalization_is_idempotent_for_str_content():
    orig = FakeResp("already a string")
    assert R.normalize_response(orig) is orig      # untouched, not re-wrapped


def test_28_routed_success_returns_string_content(env):
    """End-to-end: a failover whose alternate returns Gemini-shaped content must
    still hand the caller a .strip()-able string."""
    def inv(provider, model, timeout):
        if provider == "openai":
            raise Boom(status=503)
        return ListContentResp([{"type": "text", "text": "FAILOVER OK"}])
    res = run(inv)
    assert res.outcome == "failed_over_ok"
    assert res.response.content.strip() == "FAILOVER OK"


def test_29_training_ack_unlocks_free_tier_explicitly(env):
    """A synthetic dataset can opt IN to a free tier — but only via an explicit
    acknowledgement, never by mislabelling the tier as paid. `ALT_TIER` keeps
    telling the truth so the setting cannot quietly become a production posture."""
    env.setattr(R, "ALT_TIER", "free")
    env.setattr(R, "ALT_TIER_TRAINING_ACK", False)
    ok, why = R.may_send(R.CUSTOMER_EXTERNAL, "gemini")
    assert ok is False and "FREE tier" in why          # default: refuse

    env.setattr(R, "ALT_TIER_TRAINING_ACK", True)
    ok, _ = R.may_send(R.CUSTOMER_EXTERNAL, "gemini")
    assert ok is True                                   # acknowledged: allowed
    assert R.ALT_TIER == "free", "the tier must still report the truth"


def test_30_training_ack_is_surfaced_as_a_warning(env):
    env.setattr(R, "ALT_TIER", "free")
    env.setattr(R, "ALT_TIER_TRAINING_ACK", True)
    rd = R.readiness()
    assert "TRAINING" in (rd.get("warning") or "").upper(), \
        "an accepted training risk must be loudly visible, not silent"


def test_31_internal_agents_keep_working_without_a_local_model(env):
    """REGRESSION (found live 2026-07-25 by an end-to-end U4 test): the
    INTERNAL_SENSITIVE graph was local-only, so with Ollama unprovisioned the
    IT/HR agents got 'no provider was permitted' and every reply collapsed to
    the scripted fallback. LLM_INTERNAL_STRICT=0 must preserve the pre-U5
    behaviour — the GATE decides, not the graph."""
    env.setattr(R, "INTERNAL_STRICT", False)
    env.setattr(R, "LOCAL_ENABLED", False)          # no local model provisioned
    inv = make_invoke({"openai": "ok"})
    res = R.route("it-service", "lite", R.INTERNAL_SENSITIVE, inv,
                  "openai", "gpt-4o-mini")
    assert res.outcome == "ok"
    assert [c[0] for c in inv.calls] == ["openai"]


def test_32_strict_mode_still_blocks_remote_for_internal(env):
    """...and with strict mode ON the same request is refused everywhere
    remote, so the invariant is intact when it is asked for."""
    env.setattr(R, "INTERNAL_STRICT", True)
    env.setattr(R, "LOCAL_ENABLED", False)
    inv = make_invoke({"openai": "ok"})
    with pytest.raises(Exception) as ei:
        R.route("it-service", "lite", R.INTERNAL_SENSITIVE, inv,
                "openai", "gpt-4o-mini")
    assert inv.calls == [], "internal content must not reach a remote provider"
    assert all(a.failure_class == R.POLICY_FORBIDDEN
               for a in ei.value.route_result.attempts)
