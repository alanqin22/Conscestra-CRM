"""Cases pre-router — C1 Step 5.

Deterministic routes that must never depend on an LLM: the UI's direct modes,
and a few unambiguous phrasings. Anything else falls through to the model.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# Read modes, plus the WRITE modes the Case Management UI clicks.
#
# A button must be deterministic — routing a click through the LLM would make
# "resolve this case" a probabilistic act. But a UI write must not become a
# SECOND write path either, so these land on exactly the same
# execute() -> app/core/cases.py chain the model's actions take. The model is
# bypassed; the domain layer, state machine and history are not.
#
# Note this is MODE routing only: it fires on an explicit body["mode"] from the
# UI. Message/phrase routing below stays READ-ONLY, so a write can still never
# be inferred from a keyword.
# THE CONTRACT: a direct mode is an explicit, server-recognised OPERATION —
# never a generic write capability. There is deliberately no "write", "mutate",
# "execute_action" or "raw_sql" escape hatch, and adding one would defeat every
# guarantee below.
#
#   mode          declared input fields              domain method
#   ------------  ---------------------------------  ---------------------------
#   list          status, priority, owner_email,      cases (read)
#                 limit
#   queue         limit                               cases (read)
#   unowned       limit                               cases (read)
#   get           caseId                              cases (read)
#   history       caseId                              cases.history()
#   owners        —                                   owners (read)
#   transition    caseId, toStatus                    cases.transition()
#   assign        caseId, ownerEmail                  cases.resolve_owner+assign()
#   priority      caseId, priority                    cases.set_priority()
#   comment       caseId, body, internal              cases.comment()
#
# Params are BUILT FIELD BY FIELD below — the request body is never forwarded
# wholesale, so an unexpected field cannot reach the domain layer.
READ_MODES = ("list", "get", "history", "queue", "unowned", "owners")
WRITE_MODES = ("transition", "assign", "priority", "comment")
DIRECT_MODES = READ_MODES + WRITE_MODES

# The exact fields each mode may contribute. Anything else in the body is
# ignored by construction (it is never read), which is the documented contract.
MODE_FIELDS = {
    "list":       ("status", "priority", "owner_email", "limit"),
    "queue":      ("limit",),
    "unowned":    ("limit",),
    "get":        ("caseId",),
    "history":    ("caseId",),
    "owners":     (),
    "transition": ("caseId", "toStatus"),
    "assign":     ("caseId", "ownerEmail"),
    "priority":   ("caseId", "priority"),
    "comment":    ("caseId", "body", "internal"),
}


def is_known_mode(mode: Optional[str]) -> bool:
    """A supplied mode must be one we recognise. Unknown modes FAIL CLOSED at
    the endpoint rather than falling through to the model — otherwise
    `{"mode": "write", "message": "..."}` would quietly become an LLM turn and
    the mode would look honoured when it was ignored."""
    return (mode or "").strip().lower() in DIRECT_MODES

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Read-only phrasings only. A WRITE is never inferred from a keyword — moving a
# case through its lifecycle is consequential enough to deserve the model's
# reading of the whole sentence, or an explicit UI mode.
_PHRASES = (
    (re.compile(r"\b(case\s+)?queue\b|\bopen cases\b|\blive cases\b", re.I),
     "case_queue"),
    (re.compile(r"\bunowned\b|\bunassigned\b|\bno owner\b", re.I), "unowned"),
    (re.compile(r"\bhistory\b|\bwho changed\b|\baudit\b", re.I), "case_history"),
)


def route(body: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(action, params) for a deterministic route, or None to use the model."""
    body = body or {}
    mode = str(body.get("mode") or "").strip().lower()
    if mode in DIRECT_MODES:
        if mode == "list":
            return "list_cases", {k: body.get(k) for k in
                                  ("status", "priority", "owner_email", "limit")}
        if mode == "unowned":
            return "list_cases", {"unowned": True, "limit": body.get("limit")}
        if mode == "queue":
            return "case_queue", {"limit": body.get("limit")}
        if mode == "get":
            return "get_case", {"case_id": body.get("caseId") or body.get("case_id")}
        if mode == "history":
            return "case_history", {"case_id": body.get("caseId")
                                    or body.get("case_id")}
        if mode == "owners":
            return "list_owners", {}

        # ── write modes ────────────────────────────────────────────────────
        cid = body.get("caseId") or body.get("case_id")
        actor = body.get("agent") or "console"
        if mode == "transition":
            return "transition", {"case_id": cid,
                                  "to_status": body.get("toStatus"),
                                  "actor": actor}
        if mode == "assign":
            # ownerEmail, never a name and never "agent": resolve_owner() maps
            # it to a validated owners.owner_id or refuses.
            return "assign", {"case_id": cid,
                              "owner_email": body.get("ownerEmail"),
                              "actor": actor}
        if mode == "priority":
            return "set_priority", {"case_id": cid,
                                    "priority": body.get("priority"),
                                    "actor": actor}
        if mode == "comment":
            return "add_comment", {"case_id": cid,
                                   "body": body.get("body"),
                                   "internal": bool(body.get("internal")),
                                   "actor": actor}

    msg = str(body.get("message") or "")
    if not msg.strip():
        return None
    found = _UUID.search(msg)
    for rx, action in _PHRASES:
        if rx.search(msg):
            if action == "unowned":
                return "list_cases", {"unowned": True}
            if action == "case_history":
                # History needs a specific case; without an id let the model
                # work out which case is meant.
                return ("case_history", {"case_id": found.group(0)}) if found \
                    else None
            return action, {}
    return None
