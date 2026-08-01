"""Outbound guard — real-time triage on EVERY outgoing message (guardrail 3).

The critic reviews what agents PROPOSE; this screens what actually LEAVES —
the last deterministic wall before a customer sees it, sitting at the two
universal send choke points (send_email, send_sms) plus the SDR reply
composer. The same trade as write_guard: deterministic rails at the choke
point, so no path — human, agent, or bug — can ship a message that:

    toxic       aggressive/insulting language, shouting (long ALL-CAPS runs),
                excessive punctuation
    binding     legally binding promises the AI must never make ("we
                guarantee", "legally binding", "risk-free", "100% refund")
    off-brand   discounts above brand.max_discount_pct mentioned in prose —
                a message can't promise what a quote can't contain
    leaks       internal markers ([APPROVED KNOWLEDGE BASE], system prompt
                fragments, "as an AI"), unresolved template placeholders
                ({name}, {{var}}), payment-card numbers

Deterministic ONLY — zero LLM calls, zero latency on the send path. A block
returns the violation list to the caller, which degrades the way this
platform always degrades: auto-replies skip (gap logged), SDR falls to the
scripted reply, drafts stay drafts. Nothing is silently rewritten.

CONFIG (env)
  OUTBOUND_GUARD_ENABLED  1   kill switch (screen() passes everything when 0)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

from fastapi import APIRouter

logger = logging.getLogger("outbound_guard")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("OUTBOUND_GUARD_ENABLED", "1")

# Small, curated, and deliberately conservative — this list blocks what should
# NEVER appear in business correspondence, not casual informality.
_TOXIC = re.compile(
    r"\b(idiot|stupid|shut up|screw you|damn you|moron|pathetic|useless "
    r"(?:person|customer)|hate you|worthless)\b", re.IGNORECASE)

_BINDING = re.compile(
    r"\b(we (?:legally )?guarantee|legally binding|100% (?:guaranteed|refund)|"
    r"risk[- ]free|no questions asked refund|lifetime warranty|"
    r"cannot be undersold)\b", re.IGNORECASE)

# UNVERIFIED CUSTOMER-AUTHORED HISTORY / END UNVERIFIED HISTORY fence the
# customer-memory block (customer_memory.UNTRUSTED_OPEN/CLOSE). If either marker
# reaches an outgoing message, the model has echoed its retrieved context back
# at the customer — which both looks broken and confirms to an attacker probing
# with a planted payload that their text is being fed into the prompt.
_LEAKS = re.compile(
    r"(\[APPROVED KNOWLEDGE BASE\]|\[UNVERIFIED CUSTOMER-AUTHORED HISTORY[^\]]*\]|"
    r"\[END UNVERIFIED HISTORY\]|\[redacted-directive\]|"
    r"as an ai\b|system prompt|"
    r"my instructions|<\|.*?\|>)", re.IGNORECASE)

_PLACEHOLDER = re.compile(r"(\{\{[^}]{1,40}\}\}|\{[a-z_]{2,30}\}|"
                          r"<(?:name|placeholder|insert[^>]*)>)", re.IGNORECASE)

_CARD = re.compile(r"\b(?:\d[ -]?){15,16}\b")

_SHOUT = re.compile(r"\b[A-Z]{6,}(?:\s+[A-Z]{4,}){2,}")

_DISCOUNT = re.compile(r"(\d{1,3})\s?%\s*(?:off|discount)", re.IGNORECASE)

_counters: Dict[str, int] = {"screened": 0, "blocked": 0}


def screen(text: str, channel: str = "email") -> Dict[str, Any]:
    """{'ok': bool, 'violations': [...]} — never raises. Empty text passes
    (emptiness is the caller's problem, not a safety issue)."""
    if not ENABLED:
        return {"ok": True, "violations": []}
    t = text or ""
    v: List[str] = []
    if _TOXIC.search(t):
        v.append("toxic/aggressive language")
    if _BINDING.search(t):
        v.append("legally binding promise")
    if _LEAKS.search(t):
        v.append("internal marker leaked")
    if _PLACEHOLDER.search(t):
        v.append("unresolved template placeholder")
    if _CARD.search(t):
        v.append("payment-card number in message body")
    if _SHOUT.search(t):
        v.append("shouting (all-caps run)")
    if t.count("!!!") >= 2 or "!!!!" in t:
        v.append("excessive punctuation")
    m = _DISCOUNT.search(t)
    if m:
        try:
            from app.core import governance
            cap = float(governance.policy_value("brand.max_discount_pct", 15.0))
        except Exception:
            cap = 15.0
        if float(m.group(1)) > cap:
            v.append(f"promises {m.group(1)}% discount above the "
                     f"{cap:.0f}% brand cap")
    _counters["screened"] += 1
    if v:
        _counters["blocked"] += 1
        logger.warning(f"[guard] BLOCKED {channel} message: {', '.join(v)} — "
                       f"{t[:120]!r}")
    return {"ok": not v, "violations": v}


router = APIRouter(tags=["outbound-guard"])


@router.get("/outbound-guard/status")
def guard_status():
    return {"enabled": ENABLED, **_counters}


@router.get("/outbound-guard/test")
def guard_test(text: str, channel: str = "test"):
    """Dry-run the screen against arbitrary text (admin tooling)."""
    return screen(text, channel)
