"""Which modules may write to the database WITHOUT passing execute_sp.

    A boundary you cannot enumerate is a boundary you cannot review.

WHY THIS FILE EXISTS (R-03)

An architecture review claimed Conscestra has "a single typed chokepoint where
an agent's intent becomes a business mutation." A red-team pass overturned it.
There are three write boundaries, not one:

    a2a._dispatch_inner   capability dispatch — registry, allowed_callers,
                          principal, params_schema, confidence gate, HITL
                          floor, outcome classification
    database.execute_sp   stored-procedure calls — guard_query,
                          readonly_context, customer-scope fail-closed,
                          forbidden-procedure list
    get_connection()      ~204 direct DML sites across 75 modules, guarded by
                          NOTHING. It resolves tenancy, borrows from the pool
                          and sets the encoding. That is all it does.

The third is not a defect in itself. `get_connection` is used by the scheduler,
the agent bus, governance execution and the migrations, all of which MUST be
able to write; putting a role check inside it would fail closed on exactly the
callers that need it open, and would duplicate `execute_sp`'s boundary rather
than strengthen it. Every public module that writes directly already carries a
real control — an HMAC-signed link, an email or SMS OTP, a provider signature,
a status predicate evaluated inside the UPDATE.

WHAT WAS ACTUALLY MISSING is the ability to say so. Nothing enumerated the set,
so nobody could review it, measure its coverage, or notice it growing. This
file is that enumeration, and `release_guard` checks it, so a NEW module that
starts writing directly has to be declared rather than merely noticed.

THE NUMBER THAT MATTERS. Of 75 modules, 64 are reachable only through an
admin-gated router or not through HTTP at all. **Eleven can be reached without
authentication**, and those eleven are the ones worth a reviewer's attention.
They are listed first, individually, with the control that protects them.

ADVISORY FIRST, DELIBERATELY. This ships reporting drift rather than blocking
startup. A first-run false positive that stops a deployed environment is
precisely how a good control gets switched off — and the detector is a static
scan, so it will have false positives before it has none. Promote it to
blocking once the declared set has proven stable, the same way the write-mode
coverage check earned its place.

WHEN THIS FAILS: you added a direct `cur.execute("INSERT/UPDATE/DELETE ...")`
to a module that is not declared here. Ask one question:

    Can this write be reached by someone who has not authenticated?

  * **No** → add it under `ADMIN_GATED` or `BACKGROUND` with its category.
  * **Yes** → add it under `PUBLIC_SURFACE` with the specific control that
    protects it. If you cannot name one, that is the finding, not the file.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import warnings
import re
from typing import Any, Dict, List, Set

logger = logging.getLogger("write_call_sites")

_ROOT = pathlib.Path(__file__).resolve().parents[2]

# DML against a real table. Deliberately narrow: `UPDATE <table> SET`, not any
# occurrence of the word, so prose in a docstring is not a write.
_DML = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+[a-z_][a-z_0-9.]*\s+SET|DELETE\s+FROM)\b",
    re.IGNORECASE)


# ============================================================================
# PUBLIC SURFACE — reachable without authentication. Review these.
# ============================================================================
# Each entry names the control that stands in for authentication. These are the
# eleven modules where "no uniform boundary" actually costs something, because
# the compensating control is per-path and can only be verified by reading it.
PUBLIC_SURFACE: Dict[str, str] = {
    "app/agents/auth/router.py":
        "sign-up / sign-in / password reset. Writes auth_credentials and "
        "auth_sessions by definition — it is the thing that creates identity, "
        "so it cannot be behind identity. Guarded by bcrypt, OTP email and "
        "rate limits.",
    "app/core/order_status.py":
        "self-service order status and cancellation. VIEW needs an unforgeable "
        "HMAC link; the CANCEL needs an email OTP to the address on file plus "
        "a status predicate inside the UPDATE itself. Writes "
        "order_cancel_verifications and delegates the mutation to "
        "voice_support.cancel_order_sp.",
    "app/core/consent.py":
        "CASL unsubscribe. Writes email_suppression from a signed link — the "
        "HMAC is the authorization, exactly as it is for the governance "
        "one-click endpoint. An unsubscribe must work without a login.",
    "app/agents/executives/router.py":
        "self-gates: the router object carries require_admin on every route, "
        "so it reads as public at include_router and is not. Declared so the "
        "next reader does not have to re-derive that.",
    "app/core/governance.py":
        "one-click approve/reject from the routed-approval email. PUBLIC "
        "because the HMAC token IS the authorization; the rest of the module "
        "is admin-gated. Also the module that executes approved actions.",
    "app/core/telephony.py":
        "inbound SMS and voice webhooks. Provider signature verification "
        "(Telnyx public key); writes activities and conversation rows. Runs "
        "under a read-only CHANNEL for SP access, which is why its direct "
        "writes are here rather than through execute_sp.",
    "app/core/voice_support.py":
        "the support line. Tiered: KB-only, operator numbers, or an "
        "OTP-verified customer under write_guard.customer_scope, which "
        "fail-closes ALL stored-procedure access. Its writes are explicitly "
        "scoped queries plus cancel_order_sp, whose predicate is the rule.",
    "app/core/voice_stream.py":
        "media-stream websocket for the same line; same verification, writes "
        "transcript and turn rows.",
    "app/core/sdr.py":
        "store chat and the embedded widget. Anonymous by design — a visitor "
        "has no account. Writes conversations, escalations and OTP rows; "
        "external scope restricts it to the public KB tier (reach_invariant).",
    "app/core/booking.py":
        "meeting booking from a public link; writes activities for the slot it "
        "confirms.",
    "app/core/integrations.py":
        "calendar ICS feed and ERP CSV. Feed access is token-gated "
        "(CALENDAR_FEED_TOKEN, which release_guard refuses to start without).",
}

# ============================================================================
# ADMIN-GATED — every HTTP route into these carries require_admin.
# ============================================================================
ADMIN_GATED: Set[str] = {
    "app/core/a2a.py", "app/core/agent_bus.py", "app/core/agent_capabilities.py",
    "app/core/agent_console.py", "app/core/agent_versions.py",
    "app/core/blackboard.py", "app/core/ceo_briefing.py",
    "app/core/content_index.py", "app/core/conversations.py",
    "app/core/custom_agents.py", "app/core/custom_fields.py",
    "app/core/customer_memory.py", "app/core/data_quality.py",
    "app/core/demo.py", "app/core/deploy_state.py", "app/core/embed.py",
    "app/core/escalation.py", "app/core/evals.py",
    "app/core/executive_intelligence.py", "app/core/identity.py",
    "app/core/identity_links.py", "app/core/industry_packs.py",
    "app/core/intelligence.py", "app/core/kb_ingest.py",
    "app/core/knowledge.py", "app/core/learning.py", "app/core/lifecycle.py",
    "app/core/llm_meter.py", "app/core/marketing.py", "app/core/mcp_client.py",
    "app/core/memory_assurance.py", "app/core/memory_consolidation.py",
    "app/core/memory_observability.py", "app/core/notification_triage.py",
    "app/core/objectives.py", "app/core/pipeline_hygiene.py",
    "app/core/retention.py", "app/core/scoring.py", "app/core/semantic.py",
    "app/core/sequences.py", "app/core/shadow_eval.py",
    "app/core/staff_email.py", "app/core/tuning.py",
}

# ============================================================================
# BACKGROUND — no HTTP surface of their own. Reached by the scheduler, the
# agent bus, a governed capability, or another module in one of the lists above.
# ============================================================================
BACKGROUND: Set[str] = {
    "app/agents/email/auto_reply.py", "app/agents/email/graph.py",
    "app/agents/email/inbound_bridge.py", "app/agents/email/structured.py",
    "app/core/assignable.py", "app/core/cases.py", "app/core/critic.py",
    "app/core/dsar.py", "app/core/dsar_api.py", "app/core/enrichment.py",
    "app/core/grounding.py", "app/core/history.py", "app/core/job_ledger.py",
    "app/core/memory.py", "app/core/memory_eval.py",
    "app/core/order_notifications.py", "app/core/promotions.py",
    "app/core/quotes.py", "app/core/routing.py", "app/core/speech.py",
    "app/core/stt_shadow_score.py",
}


def declared() -> Set[str]:
    return set(PUBLIC_SURFACE) | ADMIN_GATED | BACKGROUND


# ============================================================================
# DETECTION
# ============================================================================

def _string_args(call: ast.Call) -> List[str]:
    """Every string constant inside this call — covers f-strings and
    concatenation, which is how most of these queries are actually built."""
    return [n.value for n in ast.walk(call)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def detect(root: pathlib.Path = None) -> Dict[str, int]:
    """module path -> number of direct-DML call sites found in it.

    Matches `<anything>.execute(<sql containing DML>)`. That is deliberately
    shallow: a query assembled entirely from variables is invisible here, so
    this measures DECLARED-SET DRIFT, not the absence of a bypass. Saying so is
    the difference between a control and a claim.
    """
    root = root or _ROOT
    found: Dict[str, int] = {}
    for path in sorted((root / "app").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            # Parsing OTHER PEOPLE'S source emits their warnings as ours.
            # `app/core/kb_resolver_devset.py` carries an invalid `\w` escape,
            # so every run of this check printed a SyntaxWarning that looked
            # like it came from the check. A control that adds noise on every
            # startup is a control people learn to scroll past — the same
            # crying-wolf failure the write-mode scanner had. Their warning is
            # theirs to fix; it is not this scanner's to broadcast.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8",
                                                errors="ignore"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        n = 0
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                    and any(_DML.search(s) for s in _string_args(node))):
                n += 1
        if n:
            found[rel] = n
    return found


def audit(root: pathlib.Path = None) -> Dict[str, Any]:
    """Compare what writes directly against what is declared to."""
    found = detect(root)
    dec = declared()
    undeclared = sorted(set(found) - dec)
    stale = sorted(dec - set(found))
    return {
        "modules_writing_directly": len(found),
        "call_sites": sum(found.values()),
        "declared": len(dec),
        "public_surface": len(PUBLIC_SURFACE),
        "undeclared": undeclared,
        "stale_declarations": stale,
        "ok": not undeclared,
    }
