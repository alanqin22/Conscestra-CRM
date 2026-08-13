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
import time
from typing import Any, Dict, List, Optional

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


# ── Audience ────────────────────────────────────────────────────────────────
# CHANNEL IS NOT AUDIENCE. `channel` describes transport; an "email" may go to a
# customer or to our own CFO. screen() previously received only text+channel and
# therefore could not tell the two apart, so every rule — including the
# customer-facing TONE rules — was applied to internal mail. The CEO briefing was
# blocked by _SHOUT because its section headers are legitimately uppercase
# (measured: sent=0, failed=1). A control that blocks legitimate internal traffic
# is a control somebody eventually switches off, so the fix is to give the wall
# the missing information rather than to lower it.
#
# Audience is resolved from DATABASE IDENTITY, never from subject text, sender
# name, or an address pattern:
#     employees                        -> internal   (staff and AI agents)
#     owners that are not contacts     -> internal   (the exec identities)
#     contacts                         -> customer
#     anything unresolved / any error  -> customer   (strict)
_AUD_CUSTOMER, _AUD_INTERNAL = "customer", "internal"
_aud_cache: Dict[str, tuple] = {}
_AUD_TTL = 300.0


def resolve_audience(recipient: Optional[str]) -> str:
    """Authoritative audience resolver. THE normal way to obtain an audience.

    Fails closed: an unknown recipient, a missing table or any error yields
    'customer', which is the stricter policy."""
    if not recipient:
        return _AUD_CUSTOMER
    key = recipient.strip().lower()
    hit = _aud_cache.get(key)
    if hit and (time.time() - hit[1]) < _AUD_TTL:
        return hit[0]
    aud = _AUD_CUSTOMER
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # A contact is a customer even if the same address also appears
                # in `owners` — 42 of 44 owners duplicate a contact, so contact
                # membership must win or customers would be classed internal.
                cur.execute("SELECT 1 FROM contacts WHERE lower(email)=%s LIMIT 1", (key,))
                if cur.fetchone():
                    aud = _AUD_CUSTOMER
                else:
                    cur.execute("SELECT 1 FROM employees WHERE lower(email)=%s LIMIT 1", (key,))
                    if cur.fetchone():
                        aud = _AUD_INTERNAL
                    else:
                        cur.execute("SELECT 1 FROM owners WHERE lower(email)=%s LIMIT 1", (key,))
                        aud = _AUD_INTERNAL if cur.fetchone() else _AUD_CUSTOMER
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[guard] audience unresolved for {key!r} ({exc}); "
                       f"defaulting to customer (strict)")
        return _AUD_CUSTOMER
    _aud_cache[key] = (aud, time.time())
    return aud


def screen(text: str, channel: str = "email",
           recipient: Optional[str] = None,
           audience: Optional[str] = None) -> Dict[str, Any]:
    """{'ok': bool, 'violations': [...], 'audience': str, 'audience_source': str}

    Never raises. Empty text passes (emptiness is the caller's problem).

    Pass `recipient` and let the guard decide the policy — that is the intended
    path, because it gives the guard a FACT (who) rather than a DECISION (which
    policy). `audience` is an explicit override for callers with no recipient
    (the eval harness); it is recorded in the result and logged so an assertion
    of 'internal' is always auditable. See the module docstring on trust."""
    if not ENABLED:
        return {"ok": True, "violations": [], "audience": _AUD_CUSTOMER,
                "audience_source": "disabled"}
    if recipient:
        aud, src = resolve_audience(recipient), "resolved"
    elif audience:
        aud, src = (audience if audience in (_AUD_CUSTOMER, _AUD_INTERNAL)
                    else _AUD_CUSTOMER), "asserted"
        if aud == _AUD_INTERNAL:
            logger.info(f"[guard] audience 'internal' ASSERTED by caller on "
                        f"{channel} with no recipient to verify against")
    else:
        aud, src = _AUD_CUSTOMER, "default"
    customer_facing = aud == _AUD_CUSTOMER

    t = text or ""
    v: List[str] = []
    # ── SAFETY: every audience, no exceptions. 'internal' is a policy
    #    classification, not a safety bypass.
    if _LEAKS.search(t):
        v.append("internal marker leaked")
    if _CARD.search(t):
        v.append("payment-card number in message body")
    # ── TONE / COMMERCIAL / QUALITY: customer-facing only.
    if customer_facing and _TOXIC.search(t):
        v.append("toxic/aggressive language")
    if customer_facing and _BINDING.search(t):
        v.append("legally binding promise")
    if customer_facing and _PLACEHOLDER.search(t):
        v.append("unresolved template placeholder")
    if customer_facing and _SHOUT.search(t):
        v.append("shouting (all-caps run)")
    if customer_facing and (t.count("!!!") >= 2 or "!!!!" in t):
        v.append("excessive punctuation")
    m = _DISCOUNT.search(t) if customer_facing else None
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
        logger.warning(f"[guard] BLOCKED {channel}/{aud} message: "
                       f"{', '.join(v)} — {t[:120]!r}")
    return {"ok": not v, "violations": v, "audience": aud,
            "audience_source": src}


router = APIRouter(tags=["outbound-guard"])


@router.get("/outbound-guard/status")
def guard_status():
    return {"enabled": ENABLED, **_counters}


@router.get("/outbound-guard/test")
def guard_test(text: str, channel: str = "test",
               recipient: str = None, audience: str = None):
    """Dry-run the screen against arbitrary text (admin tooling)."""
    return screen(text, channel, recipient=recipient, audience=audience)
