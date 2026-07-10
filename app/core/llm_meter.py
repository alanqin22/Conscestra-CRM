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
            estimated: bool, ok: bool, latency_ms: int) -> None:
    total = int(p_tok) + int(c_tok)
    key = (caller, date.today().isoformat())
    _SPEND[key] = _SPEND.get(key, 0) + total
    if not ENABLED:
        return
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO llm_usage (caller, model, tier, prompt_tokens,
                         completion_tokens, total_tokens, estimated, ok, latency_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (caller, model, tier, int(p_tok), int(c_tok), total,
                     estimated, ok, int(latency_ms)))
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
    budget-checked and metered; everything else passes straight through."""

    def __init__(self, inner: Any, caller: str, model: str, tier: str = "standard"):
        self._inner = inner
        self.caller = caller
        self.model = model
        self.tier = tier

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _done(self, t0: float, messages: Any, resp: Any, ok: bool) -> None:
        out = getattr(resp, "content", "") if resp is not None else ""
        p_tok, c_tok, est = _tokens_from(resp, messages, str(out)) \
            if resp is not None else (max(len(str(messages)) // 4, 1), 0, True)
        _record(self.caller, self.model, self.tier, p_tok, c_tok, est, ok,
                int((time.time() - t0) * 1000))

    def invoke(self, input: Any, *args: Any, **kwargs: Any) -> Any:
        check_budget(self.caller)
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
        t0 = time.time()
        try:
            resp = await self._inner.ainvoke(input, *args, **kwargs)
        except Exception:
            self._done(t0, input, None, ok=False)
            raise
        self._done(t0, input, resp, ok=True)
        return resp


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
