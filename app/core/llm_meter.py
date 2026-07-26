"""LLM cost metering, budgets, and tiering (round-3 improvement #2).

The fleet has ~20 LLM call sites (11 chat agents, AI summaries, SDR,
auto-reply, knowledge miner, planner, marketing drafts, SMS replies) and —
until now — no fuel gauge. Everything goes through the shared factory
(graph_utils._get_llm), which wraps the model in MeteredLLM:

    meter      every call → one llm_usage row (caller, model/tier, tokens —
               exact from the provider when available, estimated otherwise —
               latency, success). GET /llm/usage aggregates it; the CEO
               briefing carries the spend line (cost-per-day, per agent).
    budgets    optional per-caller DAILY token caps. When a caller is over
               budget the call raises LLMBudgetExceeded BEFORE spending —
               and every call site in the platform already degrades
               gracefully (deterministic script replies, template drafts,
               skip-and-log), so a blown budget never breaks a flow, it
               just switches it to the free path.
    tiering    _get_llm(tier="lite") uses LLM_MODEL_LITE when set — the
               60-word SDR replies don't need the planner's model.

Deterministic, best-effort everywhere: a missing llm_usage table degrades
to in-process counters; the meter itself failing never blocks the call.

CONFIG (env)
  LLM_METER_ENABLED       1     record usage rows (kill switch)
  LLM_DAILY_TOKEN_BUDGET  0     default per-caller daily cap (0 = unlimited)
  LLM_BUDGET_<CALLER>     —     per-caller override, e.g. LLM_BUDGET_SDR=50000
  LLM_MODEL_LITE          —     cheaper model for tier="lite" callers
  LLM_COST_PER_1K_IN      0.00015   USD estimate per 1K prompt tokens
  LLM_COST_PER_1K_OUT     0.0006    USD estimate per 1K completion tokens
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import date
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter

logger = logging.getLogger("llm_meter")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("LLM_METER_ENABLED", "1")

_COST_IN = float(os.getenv("LLM_COST_PER_1K_IN", "0.00015"))
_COST_OUT = float(os.getenv("LLM_COST_PER_1K_OUT", "0.0006"))

# in-process running spend per (caller, date) — seeded from the DB once per
# day per caller so budgets survive restarts; increments live in memory
_SPEND: Dict[Tuple[str, str], int] = {}
_SEEDED: set = set()


class LLMBudgetExceeded(RuntimeError):
    """Raised BEFORE calling the model when the caller's daily budget is
    spent. Call sites treat it like any LLM failure → deterministic fallback."""


def budget_for(caller: str) -> int:
    """Daily token budget for a caller (0 = unlimited)."""
    per = os.getenv(f"LLM_BUDGET_{caller.upper().replace('-', '_')}", "").strip()
    if per:
        try:
            return int(per)
        except ValueError:
            pass
    try:
        return int(os.getenv("LLM_DAILY_TOKEN_BUDGET", "0"))
    except ValueError:
        return 0


def _seed_spend(caller: str, day: str) -> None:
    key = (caller, day)
    if key in _SEEDED:
        return
    _SEEDED.add(key)
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(SUM(total_tokens),0) FROM llm_usage "
                            "WHERE caller=%s AND at::date=CURRENT_DATE", (caller,))
                _SPEND[key] = int(cur.fetchone()[0]) + _SPEND.get(key, 0)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[llm_meter] spend seed skipped: {exc}")


def spent_today(caller: str) -> int:
    day = date.today().isoformat()
    _seed_spend(caller, day)
    return _SPEND.get((caller, day), 0)


def check_budget(caller: str) -> None:
    budget = budget_for(caller)
    if budget > 0 and spent_today(caller) >= budget:
        raise LLMBudgetExceeded(
            f"'{caller}' has spent its daily LLM budget "
            f"({spent_today(caller)}/{budget} tokens) — deterministic "
            f"fallbacks apply until midnight UTC")


def _record(caller: str, model: str, tier: str, p_tok: int, c_tok: int,
            estimated: bool, ok: bool, latency_ms: int,
            route: Optional[Dict[str, Any]] = None) -> None:
    """Record ONE PROVIDER ATTEMPT. `route` carries the U5 logical-request
    context; when absent (every pre-U5 caller) the row is written as its own
    single-attempt logical request, so existing queries keep their meaning."""
    total = int(p_tok) + int(c_tok)
    key = (caller, date.today().isoformat())
    _SPEND[key] = _SPEND.get(key, 0) + total
    if not ENABLED:
        return
    r = route or {}
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO llm_usage (caller, model, tier, prompt_tokens,
                         completion_tokens, total_tokens, estimated, ok, latency_ms,
                         logical_request_id, attempt_number, is_final,
                         requested_provider, selected_provider, data_class,
                         failover, failure_class, failure_reason, policy_reason,
                         attempt_latency_ms, total_latency_ms, outcome,
                         deterministic_fallback)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               COALESCE(%s::uuid, gen_random_uuid()),
                               COALESCE(%s,1), COALESCE(%s,true),
                               %s,%s,%s, COALESCE(%s,false), %s,%s,%s,
                               COALESCE(%s,%s), %s,%s, COALESCE(%s,false))""",
                    (caller, model, tier, int(p_tok), int(c_tok), total,
                     estimated, ok, int(latency_ms),
                     r.get("logical_request_id"), r.get("attempt_number"),
                     r.get("is_final"), r.get("requested_provider"),
                     r.get("selected_provider"), r.get("data_class"),
                     r.get("failover"), r.get("failure_class"),
                     r.get("failure_reason"), r.get("policy_reason"),
                     r.get("attempt_latency_ms"), int(latency_ms),
                     r.get("total_latency_ms"), r.get("outcome"),
                     r.get("deterministic_fallback")))
                if random.random() < 0.01:      # opportunistic 90-day GC
                    cur.execute("DELETE FROM llm_usage "
                                "WHERE at < now() - interval '90 days'")
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[llm_meter] usage insert skipped (table missing?): {exc}")


def _tokens_from(resp: Any, messages: Any, out_text: str) -> Tuple[int, int, bool]:
    """(prompt, completion, estimated) — provider counts when available."""
    um = getattr(resp, "usage_metadata", None)
    if isinstance(um, dict) and um.get("input_tokens") is not None:
        return int(um.get("input_tokens", 0)), int(um.get("output_tokens", 0)), False
    rm = getattr(resp, "response_metadata", None) or {}
    tu = rm.get("token_usage") or {}
    if tu.get("prompt_tokens") is not None:
        return int(tu.get("prompt_tokens", 0)), int(tu.get("completion_tokens", 0)), False
    p_chars = len(str(messages))
    return max(p_chars // 4, 1), max(len(out_text or "") // 4, 1), True


class MeteredLLM:
    """Transparent proxy over a LangChain chat model: invoke/ainvoke are
    budget-checked and metered; everything else passes straight through.

    U5: when failover is enabled this is also the PROVIDER ROUTER. The order is
    load-bearing — budget is decided ONCE, before any provider is contacted, so
    a failover can never become a second bite at a budget that is already spent
    (`one logical request = one budget decision`). Everything after that is the
    router's business, and a caller's `except` still sees a provider exception,
    so all 39 deterministic fallbacks behave exactly as before."""

    def __init__(self, inner: Any, caller: str, model: str, tier: str = "standard",
                 provider: Optional[str] = None,
                 alt_factory: Optional[Any] = None,
                 data_internal: bool = False):
        self._inner = inner
        self.caller = caller
        self.model = model
        self.tier = tier
        self.provider = provider or "openai"
        # Lazily builds a client for an alternate provider. None = no failover
        # possible, which is exactly today's behaviour.
        self._alt_factory = alt_factory
        self._data_internal = data_internal

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _done(self, t0: float, messages: Any, resp: Any, ok: bool,
              route: Optional[Dict[str, Any]] = None,
              model: Optional[str] = None) -> None:
        out = getattr(resp, "content", "") if resp is not None else ""
        p_tok, c_tok, est = _tokens_from(resp, messages, str(out)) \
            if resp is not None else (max(len(str(messages)) // 4, 1), 0, True)
        _record(self.caller, model or self.model, self.tier, p_tok, c_tok, est,
                ok, int((time.time() - t0) * 1000), route=route)

    # -- routed path ---------------------------------------------------------

    def _router(self):
        """The router module, or None when U5 is off/unavailable. Failing to
        load it must degrade to today's single-provider behaviour, never break
        the fleet."""
        try:
            from app.core import llm_router
            if not llm_router.ENABLED or self._alt_factory is None:
                return None
            return llm_router
        except Exception as exc:
            logger.debug(f"[llm_meter] router unavailable: {exc}")
            return None

    def _run_routed(self, R, messages: Any, args, kwargs, is_async: bool):
        data_class = R.classify(self.caller, internal=self._data_internal)

        def _invoke(provider: str, model: str, timeout: float):
            client = (self._inner if provider == self.provider
                      else self._alt_factory(provider, model, timeout))
            return client.invoke(messages, *args, **kwargs)

        result = None
        try:
            result = R.route(self.caller, self.tier, data_class, _invoke,
                             self.provider, self.model)
            return result.response
        except BaseException as exc:
            # route() raises with the partial result attached so a failed
            # logical request is still fully recorded (a failed attempt spent
            # tokens and must still appear in cost).
            result = getattr(exc, "route_result", None)
            raise
        finally:
            if result is not None:
                self._record_route(result, messages, data_class)

    def _record_route(self, result, messages: Any, data_class: str) -> None:
        n = len(result.attempts)
        for i, a in enumerate(result.attempts):
            final = (i == n - 1)
            base = {
                "logical_request_id": result.logical_id,
                "attempt_number": a.n, "is_final": final,
                "requested_provider": self.provider,
                "selected_provider": a.provider if a.ok else None,
                "data_class": data_class,
                "failover": a.n > 1,
                "failure_class": a.failure_class,
                "failure_reason": a.failure_reason,
                "policy_reason": a.policy_reason,
                "attempt_latency_ms": a.latency_ms,
                "total_latency_ms": result.total_latency_ms if final else None,
                "outcome": result.outcome if final else None,
                "deterministic_fallback": (final and not a.ok),
            }
            resp = result.response if a.ok else None
            try:
                self._done(time.time() - (a.latency_ms / 1000.0), messages,
                           resp, a.ok, route=base, model=a.model)
            except Exception as exc:
                logger.debug(f"[llm_meter] attempt record skipped: {exc}")

    # -- public surface (signatures unchanged) --------------------------------

    def invoke(self, input: Any, *args: Any, **kwargs: Any) -> Any:
        check_budget(self.caller)          # ONE budget decision, before any provider
        R = self._router()
        if R is not None:
            return self._run_routed(R, input, args, kwargs, is_async=False)
        t0 = time.time()
        try:
            resp = self._inner.invoke(input, *args, **kwargs)
        except Exception:
            self._done(t0, input, None, ok=False)
            raise
        self._done(t0, input, resp, ok=True)
        return resp

    async def ainvoke(self, input: Any, *args: Any, **kwargs: Any) -> Any:
        check_budget(self.caller)
        R = self._router()
        if R is not None:
            import asyncio
            return await asyncio.to_thread(
                self._run_routed, R, input, args, kwargs, False)
        t0 = time.time()
        try:
            resp = await self._inner.ainvoke(input, *args, **kwargs)
        except Exception:
            self._done(t0, input, None, ok=False)
            raise
        self._done(t0, input, resp, ok=True)
        return resp


# Backwards-compatible alias — the router IS the metered proxy (U5 design §12).
ProviderRouter = MeteredLLM


# ============================================================================
# REPORTING
# ============================================================================

def usage_summary(days: int = 7) -> Dict[str, Any]:
    out: Dict[str, Any] = {"window_days": days, "callers": {}, "totals": {}}
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT caller,
                              count(*) AS calls,
                              count(*) FILTER (WHERE NOT ok) AS failures,
                              COALESCE(SUM(prompt_tokens),0),
                              COALESCE(SUM(completion_tokens),0),
                              COALESCE(AVG(latency_ms),0)::int,
                              bool_or(estimated)
                       FROM llm_usage
                       WHERE at > now() - make_interval(days => %s)
                       GROUP BY caller ORDER BY 4+5 DESC""", (days,))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[llm_meter] summary skipped: {exc}")
        return {**out, "error": "llm_usage unavailable (migration applied?)"}
    t_calls = t_in = t_out = 0
    for caller, calls, fails, p_tok, c_tok, lat, any_est in rows:
        cost = (p_tok / 1000) * _COST_IN + (c_tok / 1000) * _COST_OUT
        budget = budget_for(caller)
        out["callers"][caller] = {
            "calls": calls, "failures": fails,
            "prompt_tokens": int(p_tok), "completion_tokens": int(c_tok),
            "est_cost_usd": round(cost, 4), "avg_latency_ms": lat,
            "some_estimated": bool(any_est),
            "budget_today": budget or None,
            "spent_today": spent_today(caller) if budget else None,
        }
        t_calls += calls; t_in += p_tok; t_out += c_tok
    out["totals"] = {
        "calls": t_calls, "prompt_tokens": int(t_in),
        "completion_tokens": int(t_out),
        "est_cost_usd": round((t_in / 1000) * _COST_IN
                              + (t_out / 1000) * _COST_OUT, 4)}
    return out


def spend_lines(days: int = 1) -> list:
    """Compact human lines for the CEO briefing ('is the AI worth its fuel?')."""
    s = usage_summary(days)
    if s.get("error") or not s["totals"].get("calls"):
        return []
    t = s["totals"]
    lines = [f"AI spend ({days}d): {t['calls']} calls · "
             f"{(t['prompt_tokens'] + t['completion_tokens']):,} tokens · "
             f"≈${t['est_cost_usd']:.2f}"]
    top = sorted(s["callers"].items(),
                 key=lambda kv: -kv[1]["est_cost_usd"])[:3]
    lines.append("top spenders: " + " · ".join(
        f"{c} ${v['est_cost_usd']:.2f}" for c, v in top))
    over = [c for c, v in s["callers"].items()
            if v.get("budget_today") and v["spent_today"] >= v["budget_today"]]
    if over:
        lines.append(f"⚠ over daily budget (deterministic fallbacks active): "
                     f"{', '.join(over)}")
    return lines


router = APIRouter(tags=["llm"])


@router.get("/llm/usage")
def llm_usage(days: int = 7):
    return usage_summary(days)
