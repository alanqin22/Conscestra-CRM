"""Intelligent Channel Selection — the Orchestrator's "what's the best way to
accomplish this objective?" decision (Unified Communication Layer, Phase 4).

A traditional UC system asks "which channel receives this message?". This asks
the AI-native question: given an OBJECTIVE and a PARTY, what is the best
communication ACTION? The vision's formula, made deterministic:

    Intent + Identity + Relationship + Urgency + Channel Preference
           + Business Context + Authorization  =  Best Communication Action

    select(objective, party) -> {channel, action, reason, requires_verification,
                                 ranked fallbacks}

INPUTS, grounded in real state:
  • Objective spec — the vision's table encoded (urgent→voice/sms, proposal→
    email, quick update→whatsapp, internal alert→slack/teams, sensitive→verified
    channel, …).
  • Reachability — which channels we can actually use: contact email/phone,
    employee email, and any linked channel_identities (whatsapp/slack/teams).
  • Channel preference — LEARNED from the Unified Conversation Object: the
    channel this party most recently used. The relationship informs the channel.
  • Authorization — sensitive/financial/OTP objectives require a VERIFIED channel;
    if none is verified the decision says so (requires_verification) instead of
    leaking to an unverified one.

Deterministic by design (rules over the formula) — no LLM per decision. Exposed
as the A2A read capability `comms.select_channel`, so the planner/supervisor can
ask it while composing a play (ties into [[project_conductor]]).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection
from app.core.identity import INTERNAL_CHANNELS

logger = logging.getLogger("channel_selector")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("CHANNEL_SELECT_ENABLED", "1")

# Real-time channels — favored when the objective is urgent.
REALTIME = {"voice", "sms", "whatsapp", "slack", "teams"}

# channel → the action/capability that performs it.
CHANNEL_ACTION = {
    "email": "email.send", "internal_email": "email.send",
    "sms": "sms.send", "whatsapp": "whatsapp.send", "voice": "voice.call",
    "slack": "slack.post", "teams": "teams.post",
}

# The vision's "situation → intelligent action" table, encoded.
#   prefer: ordered channel affinity · sensitive/requires_verified: authorization
#   urgency: default urgency · use_preference: let the learned channel win ties
DEFAULT_SPEC = {"scope": "external", "prefer": ["email", "sms"], "use_preference": True}
OBJECTIVES: Dict[str, Dict[str, Any]] = {
    "urgent_issue":         {"scope": "external", "urgency": "urgent",
                             "prefer": ["voice", "sms", "whatsapp"]},
    "payment_reminder":     {"scope": "external", "sensitive": True,
                             "prefer": ["email", "sms"], "use_preference": True},
    "proposal":             {"scope": "external", "prefer": ["email"]},
    "quick_update":         {"scope": "external", "prefer": ["whatsapp", "sms"],
                             "use_preference": True},
    "appointment_reminder": {"scope": "external", "prefer": ["sms", "email"],
                             "use_preference": True},
    "otp":                  {"scope": "external", "sensitive": True,
                             "requires_verified": True, "prefer": ["sms", "email"]},
    "sensitive_financial":  {"scope": "external", "sensitive": True,
                             "requires_verified": True, "prefer": ["email", "voice"]},
    "routine_reminder":     {"scope": "external", "prefer": ["email", "sms"],
                             "use_preference": True},
    "internal_alert":       {"scope": "internal", "urgency": "high",
                             "prefer": ["slack", "teams"]},
    "internal_briefing":    {"scope": "internal",
                             "prefer": ["slack", "teams", "internal_email"]},
}


def _scope_of(channel: str) -> str:
    return "internal" if channel in INTERNAL_CHANNELS else "external"


# ============================================================================
# Grounded inputs
# ============================================================================

def _reachability(party_type: str, party_id: str, scope: str) -> Dict[str, Dict[str, bool]]:
    """channel → {reachable, verified} for this party, from real state."""
    out: Dict[str, Dict[str, bool]] = {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if scope == "external" and party_type == "contact":
                cur.execute("SELECT email, COALESCE(is_email_verified,false), phone "
                            "FROM contacts WHERE contact_id=%s::uuid", (party_id,))
                r = cur.fetchone()
                if r:
                    email, ev, phone = r
                    if email:
                        out["email"] = {"reachable": True, "verified": bool(ev)}
                    if phone:
                        out["sms"] = {"reachable": True, "verified": False}
                        out["voice"] = {"reachable": True, "verified": False}
                        out["whatsapp"] = {"reachable": True, "verified": False}
            elif scope == "internal" and party_type == "employee":
                cur.execute("SELECT email FROM owners WHERE owner_id=%s::uuid", (party_id,))
                r = cur.fetchone()
                if r and r[0]:
                    out["internal_email"] = {"reachable": True, "verified": True}
            # Linked channel_identities upgrade reachability + verified (the only
            # source for slack/teams; also verifies whatsapp once OTP-linked).
            try:
                cur.execute("SELECT channel, verified FROM channel_identities "
                            "WHERE party_type=%s AND party_id=%s::uuid",
                            (party_type, party_id))
                for ch, ver in cur.fetchall():
                    info = out.get(ch, {"reachable": True, "verified": False})
                    info["reachable"] = True
                    info["verified"] = info["verified"] or bool(ver)
                    out[ch] = info
            except Exception:
                conn.rollback()
        return out
    finally:
        conn.close()


def _learned_preference(party_type: str, party_id: str) -> Optional[str]:
    """The channel this party most recently used — the relationship's own signal,
    read from the Unified Conversation Object."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT channel FROM conversations "
                        "WHERE party_type=%s AND party_id=%s::uuid "
                        "ORDER BY last_message_at DESC LIMIT 1", (party_type, party_id))
            r = cur.fetchone()
            return r[0] if r else None
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


# ============================================================================
# Decision
# ============================================================================

def select(objective: str, party_type: str, party_id: str,
           urgency: Optional[str] = None,
           sensitive: Optional[bool] = None) -> Dict[str, Any]:
    """Best communication action for an objective + party. Deterministic."""
    if not ENABLED:
        return {"ok": False, "error": "channel selection disabled"}
    spec = OBJECTIVES.get((objective or "").strip().lower(), DEFAULT_SPEC)
    scope = spec.get("scope", "external")
    urg = (urgency or spec.get("urgency") or "normal").lower()
    sens = spec.get("sensitive", False) if sensitive is None else bool(sensitive)
    needs_verified = bool(spec.get("requires_verified") or sens)

    reach = _reachability(party_type, party_id, scope)
    pref = _learned_preference(party_type, party_id)
    urgent = urg in ("urgent", "high")

    def score_channel(ch: str, base: float) -> float:
        info = reach.get(ch)
        if not info or not info.get("reachable"):
            return -1.0
        s = base
        if ch == pref and spec.get("use_preference"):
            s += 0.25                                   # relationship preference
        if urgent and ch in REALTIME:
            s += 0.20                                   # urgency → real-time
        if needs_verified:
            s += 0.30 if info.get("verified") else -0.25  # authorization
        return s

    scored: List[Dict[str, Any]] = []
    seen = set()
    for i, ch in enumerate(spec.get("prefer", [])):
        s = score_channel(ch, 1.0 - i * 0.15)
        if s >= 0:
            scored.append({"channel": ch, "score": round(s, 3),
                           "verified": reach[ch]["verified"]})
            seen.add(ch)
    # Reachable fallbacks not named by the objective (lower base), same scope.
    for ch, info in reach.items():
        if ch not in seen and info.get("reachable") and _scope_of(ch) == scope:
            s = score_channel(ch, 0.30)
            if s >= 0:
                scored.append({"channel": ch, "score": round(s, 3),
                               "verified": info["verified"]})
    scored.sort(key=lambda x: -x["score"])

    if not scored:
        return {"ok": False, "objective": objective, "scope": scope,
                "reason": f"no reachable {scope} channel for this party",
                "requires_verification": needs_verified,
                "party": {"party_type": party_type, "party_id": party_id}}

    best = scored[0]
    ch = best["channel"]
    requires_verification = bool(needs_verified and not best["verified"])
    reasons = [f"objective '{objective}' favors {'/'.join(spec.get('prefer', [])) or 'any'}"]
    if urgent and ch in REALTIME:
        reasons.append(f"urgent → real-time ({ch})")
    if ch == pref:
        reasons.append(f"{ch} is the party's most recent channel")
    if needs_verified:
        reasons.append("verified channel required" if requires_verification
                       else f"{ch} is verified")
    return {
        "ok": True, "objective": objective, "scope": scope, "urgency": urg,
        "channel": ch, "action": CHANNEL_ACTION.get(ch),
        "confidence": round(min(0.99, max(0.3, best["score"])), 3),
        "requires_verification": requires_verification,
        "reason": "; ".join(reasons),
        "preferred_channel": pref,
        "ranked": scored,
        "party": {"party_type": party_type, "party_id": party_id},
    }


# ============================================================================
# Admin endpoint
# ============================================================================

router = APIRouter(tags=["channel-selector"])


@router.post("/comms/select-channel")
def comms_select_channel(body: Dict[str, Any]):
    b = body or {}
    return select(str(b.get("objective") or "quick_update"),
                  str(b.get("party_type") or "contact"),
                  str(b.get("party_id") or ""),
                  urgency=b.get("urgency"), sensitive=b.get("sensitive"))


@router.get("/comms/objectives")
def comms_objectives():
    return {"enabled": ENABLED,
            "objectives": {k: v.get("prefer") for k, v in OBJECTIVES.items()}}
