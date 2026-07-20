"""Channel Adapters — the Unified Communication Layer's translation edge.

    raw channel event  ──adapter──▶  InboundMessage  ──▶  conversations.ingest()

Each adapter turns a channel's native inbound (Telnyx SMS, SMTP email, a Slack
event, a WhatsApp webhook) into the ONE common envelope, so the conversation
store and the Orchestrator never need channel specifics. This is the layer that
lets "every channel become an interface" while the relationship stays the
intelligence.

CONTRACT — every capture_* is:
  • BEST-EFFORT — it must NEVER change or break the channel's existing behavior.
    A conversation-store failure is swallowed and logged; the channel's own
    logic (activity logging, customer_memory, auto-reply) is untouched.
  • FLAG-GATED — CONV_CAPTURE_ENABLED (default 1) is a single kill switch.
  • ADDITIVE — it only ADDS the unified cross-channel thread; nothing else
    changes.

WIRED (Phase 2): inbound SMS (telephony._bridge_inbound_sms) and inbound email
(email.inbound_bridge).
WIRED (Phase 2b): voice — a support call threads as ONE transcript message at
call end (voice_support._close_call); SDR calls thread per turn, both
directions (sdr.converse, Gather + media-stream transports) — and webchat
(sdr.converse, both directions).
Phase 3 transports live in transports.py — capture_whatsapp (external) and
capture_slack / capture_teams (internal), credential-gated.

CONFIG (env)
  CONV_CAPTURE_ENABLED  1   master kill switch for conversation capture
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("channel_adapters")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("CONV_CAPTURE_ENABLED", "1")

_EMAIL_ANGLE_RE = re.compile(r"<([^>]+)>")


def _email_addr(sender: str) -> str:
    """'Name <a@b.c>' or 'a@b.c' → 'a@b.c' (lowercased)."""
    m = _EMAIL_ANGLE_RE.search(sender or "")
    return (m.group(1) if m else (sender or "")).strip().lower()


def capture(channel: str, handle: str, body: str, direction: str = "inbound",
            external_ref: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            prior_handle: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Normalize + thread one message. Best-effort — returns the ingest result,
    or None when disabled or on any failure (never raises)."""
    if not ENABLED:
        return None
    try:
        from app.core.conversations import InboundMessage, ingest
        return ingest(InboundMessage(
            channel=channel, handle=handle or "", body=body or "",
            direction=direction, external_ref=external_ref,
            metadata=metadata or {}, prior_handle=prior_handle))
    except Exception as exc:
        logger.debug(f"[adapter] capture {channel} failed (non-fatal): {exc}")
        return None


# ── External channels (customer ↔ business) ──────────────────────────────────

def capture_sms(sender_e164: str, body: str, direction: str = "inbound",
                external_ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return capture("sms", sender_e164, body, direction, external_ref)


def capture_email(sender: str, subject: Optional[str], body_text: str,
                  direction: str = "inbound",
                  external_ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
    body = f"{subject}\n\n{body_text}".strip() if subject else (body_text or "")
    return capture("email", _email_addr(sender), body, direction, external_ref,
                   {"subject": subject} if subject else None)


def capture_webchat(handle: Optional[str], body: str,
                    session_id: Optional[str] = None,
                    direction: str = "inbound") -> Optional[Dict[str, Any]]:
    # A typed email identifies the visitor; otherwise thread by the session id.
    # Once the email appears, the session key rides along as prior_handle so
    # the visitor's anonymous thread merges into their identified one.
    h = handle or (f"session:{session_id}" if session_id else "")
    prior = f"session:{session_id}" if (handle and session_id) else None
    return capture("webchat", h, body, direction, external_ref=session_id,
                   prior_handle=prior)


def capture_voice(caller_e164: str, body: str, direction: str = "inbound",
                  external_ref: Optional[str] = None,
                  metadata: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return capture("voice", caller_e164, body, direction, external_ref, metadata)


def capture_whatsapp(sender_e164: str, body: str, direction: str = "inbound",
                     external_ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return capture("whatsapp", sender_e164, body, direction, external_ref)


# ── Internal channels (employee ↔ business intelligence) ─────────────────────

def capture_slack(user_id: str, body: str, direction: str = "inbound",
                  external_ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return capture("slack", user_id, body, direction, external_ref)


def capture_teams(user_id: str, body: str, direction: str = "inbound",
                  external_ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return capture("teams", user_id, body, direction, external_ref)
