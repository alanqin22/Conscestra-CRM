"""PII minimization — mask personal identifiers before text leaves for the
LLM (advanced improvement #7, the trust-layer analog).

Customer-authored content (inbound email bodies, support threads) and the
context blocks we inject into prompts can carry emails, phone numbers and
account-number-like digit runs. None of that helps the LLM write a better
reply or article — so it never leaves the building:

    j.smith@acme.com     →  j***@acme.com      (domain kept — tone/context)
    +1 (555) 123-4567    →  ***4567            (last 4 kept — reference)
    4111111111111111     →  ************1111   (card-like runs)

THE BOUNDARY (deliberate):
  • MASKED: customer-authored text going into prompts — auto-reply
    subject/body, knowledge-mining threads — and the rendered CRM context
    blocks (a2a NL dispatch + auto-reply personalization).
  • NOT masked: operational agent commands ("send a payment reminder to
    x@y.com") — the agent needs the field to act, and the send itself is
    already gated by AUTOSEND/consent; and user-typed chat ("find
    bob@acme.com") — masking would break the ask.

Deterministic regex only, idempotent (masked output never re-matches), and
behind PII_MASK_ENABLED (default ON; set 0 to kill instantly). First names
are deliberately kept — personalization is the product.

CONFIG (env)
  PII_MASK_ENABLED   1   kill switch for all masking
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, Tuple

logger = logging.getLogger("privacy")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("PII_MASK_ENABLED", "1")

_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# Card/account-like: 13–19 straight digits (before the phone rule eats them).
_CARD_RE = re.compile(r"\b\d{13,19}\b")
# Phone-like: digit sequences with separators. Applied only when the match
# carries ≥9 digits, so dates (8 digits) and money stay readable.
_PHONE_RE = re.compile(r"(?<![\w*])\+?\d[\d\s().\-]{6,}\d(?![\w*])")


def _mask_email(m: re.Match) -> str:
    return f"{m.group(1)}***{m.group(2)}"


def _mask_card(m: re.Match) -> str:
    s = m.group(0)
    return "*" * (len(s) - 4) + s[-4:]


def _mask_phone(m: re.Match) -> str:
    s = m.group(0)
    digits = re.sub(r"\D", "", s)
    if len(digits) < 9:          # dates, amounts, short ids — leave alone
        return s
    return "***" + digits[-4:]


def mask(text: str) -> str:
    """Mask emails / phone numbers / card-like digit runs. Idempotent;
    passthrough when PII_MASK_ENABLED=0 or text is empty."""
    if not ENABLED or not text:
        return text or ""
    out = _EMAIL_RE.sub(_mask_email, text)
    out = _CARD_RE.sub(_mask_card, out)
    out = _PHONE_RE.sub(_mask_phone, out)
    return out


def mask_report(text: str) -> Tuple[str, Dict[str, int]]:
    """mask() plus counts of what was hidden (for tests/telemetry)."""
    if not text:
        return "", {"emails": 0, "cards": 0, "phones": 0}
    emails = len(_EMAIL_RE.findall(text))
    cards = len(_CARD_RE.findall(text))
    phones = sum(1 for m in _PHONE_RE.finditer(text)
                 if len(re.sub(r"\D", "", m.group(0))) >= 9)
    return mask(text), {"emails": emails, "cards": cards, "phones": phones}
