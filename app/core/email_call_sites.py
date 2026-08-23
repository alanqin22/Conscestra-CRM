"""Who is allowed to call `send_email`, and why.

    A control enforced only by a test nobody is obliged to run is a habit,
    not a control.

WHY THIS FILE IS IN `app/` AND NOT `tests/`

`app/core/staff_email.py` carries a strong guarantee: it reaches a sender from
exactly one function, `_deliver`, and that function is unreachable except
through claim → acquire → mark_attempted. An adversarial audit asked the
sharper question:

    Can a future developer bypass the email-governance layer simply by calling
    send_email() directly?

They could. The staff-email guard is MODULE-SCOPED — it stops that module
growing a second sender; it does nothing about a NEW module emailing an
employee directly, skipping tier, recipient resolution, preference, budget and
ledger entirely.

The obvious fix is a test. It is not sufficient here: `.github/workflows/ci.yml`
states plainly that "the tests/ directory is also outside this repository by
policy, so CI cannot run any test at all." A test-only guard would be enforced
by whoever remembers to run pytest.

So the allowlist lives here, in committed code, and is enforced twice:

  * `release_guard.enforce()` runs it at startup and REFUSES to start a
    deployed environment with an undeclared sender. Same doctrine as every
    other control in that module — "a warning in a log nobody reads is the same
    outcome as no control at all."
  * `tests/test_email_sender_allowlist.py` reads the SAME list, for fast
    feedback while developing.

WHAT TO DO WHEN THIS FAILS

You added a `send_email` call. Ask one question:

    Can this email reach a member of staff — anyone in `assignable_identity`,
    or the role mailbox?

  * **Yes** → do not add yourself here. Route it through
    `app.core.staff_email`, which decides tier, resolves the recipient from
    explicit membership, honours preference, spends from the attention budget
    and records a ledger row. That is the whole point of this file existing.
  * **No — it is customer mail, OTP, or a booking confirmation** → add an entry
    below with a one-line reason. The reason is the deliverable: it is what a
    reviewer reads when deciding whether the answer to the question above was
    honest.

THE LIST IS DELIBERATELY BORING TO EDIT. Making a bypass *impossible* would
mean putting a database read in the hot path of every send, including customer
mail, and `send_email` cannot know whether THIS PARTICULAR send is governed —
only who the recipient is. Making it *deliberate* is the achievable goal, and
this is what deliberate looks like.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger("email_call_sites")

# Functions that may call a sender directly. (module path, function name).
#
# Ordered by what the mail is FOR, because that is the axis a reviewer cares
# about. Every entry answers: can this reach staff, and if so why is it not
# going through staff_email?
AUTHORIZED_SENDERS: Dict[Tuple[str, str], str] = {
    # ── Customer mail. Recipients are contacts, gated by is_email_verified. ──
    ("app/agents/email/graph.py", "db_node"):
        "email agent — customer send_email/send_template mode",
    ("app/agents/email/graph.py", "_handle_send_template"):
        "email agent — templated customer mail",
    ("app/agents/email/router.py", "_send_contact_notification"):
        "website contact form → info@ notification + customer acknowledgement",
    ("app/agents/email/router.py", "email_test"):
        "admin-gated SMTP smoke test",
    ("app/agents/email/structured.py", "send_payment_reminder_sp"):
        "dunning notice to a verified customer contact (A2A structured path)",
    ("app/agents/email/auto_reply.py", "process_inbound_email"):
        "auto-reply to an inbound customer email",
    ("app/core/order_notifications.py", "notify"):
        "order lifecycle mail to the customer, behind its own ledger",
    ("app/core/booking.py", "book"):
        "booking confirmation to the person who booked",
    ("app/core/quotes.py", "generate_quote_sp"):
        "quote document to a customer contact",
    ("app/core/marketing.py", "launch_campaign"):
        "CASL-gated commercial campaign; consent checked in send_email",
    ("app/core/voice_support.py", "_send_payment_summary"):
        "payment summary to the verified caller",
    ("app/core/sdr.py", "_send_otp_email"):
        "one-time code to a customer proving control of their address",
    ("app/core/agent_console.py", "send_reply"):
        "a human rep's reply to the customer, on the customer's channel",

    # ── Account/security mail. Recipients are CRM users, not work assignees. ──
    ("app/agents/auth/router.py", "_send_otp_email"):
        "sign-in one-time code — an authentication step, not a work notice",
    ("app/agents/admin_users/router.py", "send_reset"):
        "password reset link — same",

    # ── Module-level bindings. Not calls — but importable by anything, which
    #    makes the module itself an email capability. Declared so the chain
    #    "A re-exports the sender, B imports it from A" is cut at A.
    ("app/agents/email/graph.py", "<re-export:send_email>"):
        "email agent binds the sender at module scope for its own send modes; "
        "an importer of app.agents.email.graph.send_email would be invisible "
        "to a call-site scan, so the binding declares itself",
    ("app/agents/auth/router.py", "<re-export:send_email>"):
        "auth router binds at module scope for the OTP path; same reason",
    ("app/agents/admin_users/router.py", "<re-export:send_email>"):
        "admin-users router binds at module scope for the reset path; same reason",

    # ── Staff mail. THESE ARE THE ONES THAT MATTER. ──────────────────────────
    ("app/core/staff_email.py", "_deliver"):
        "THE governed staff path. Tier, recipient, preference, budget, ledger. "
        "Anything emailing staff belongs here, not beside it",
    ("app/core/escalation.py", "_email_escalation"):
        "exception escalation to the ROLE MAILBOX. Predates staff_email; "
        "Stage 3 wrapped it in the ledger rather than moving it, so behaviour "
        "for an already-shipped path did not change",
    ("app/core/governance.py", "route_approval"):
        "routed approval to an executive. Same as above — ledger-wrapped in "
        "Stage 3, not relocated",
    ("app/core/governance.py", "renotify_pending"):
        "human-triggered re-send of a pending approval with a better template. "
        "DELIBERATELY not ledger-wrapped: this is somebody explicitly asking "
        "for a duplicate, the one case where suppressing one is wrong",
    ("app/core/ceo_briefing.py", "send_briefing"):
        "daily executive briefing. Predates this design and has its own "
        "recipient model (executives.briefing_hour); a candidate to fold into "
        "staff_email once the digest has proven itself",
}

_SENDERS = {"send_email", "_send_via_resend"}

# The module that DEFINES the sender. `send_email` dispatches to
# `_send_via_resend` internally, which is the transport choosing itself — not a
# caller reaching past the governance layer. Excluding it by path rather than by
# function name, so a new helper added inside smtp_imap is still exempt while a
# new caller anywhere else is not.
_SENDER_MODULE = "app/agents/email/smtp_imap.py"

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _sender_names(tree: ast.AST) -> Set[str]:
    """Every local name in this module bound to a sender.

    THE HOLE THIS CLOSES. The first version matched only on the CALLED NAME,
    so an adversarial audit walked straight past it:

        from app.agents.email.smtp_imap import send_email as mail
        mail(to="sarah.johnson@emp.agentorc.ca", ...)      # invisible

    That is not an attack, it is ordinary style — `as _send` is exactly what
    somebody writes to avoid a name clash. Three of six bypass classes got
    through the name-only check; this is the fix for two of them.
    """
    names = set(_SENDERS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name in _SENDERS:
                    names.add(a.asname or a.name)
    return names


def _reexports(tree: ast.AST) -> Set[str]:
    """Sender names bound at MODULE level — i.e. re-exported.

    The third bypass class, and the subtlest: module A does
    `from …smtp_imap import send_email` at module scope; module B does
    `from A import send_email; send_email(...)`. B's call is invisible if the
    scanner only knows A's own aliases, and following the binding transitively
    is a whole-program analysis this does not want to be.

    MODULE SCOPE IS THE WHOLE TEST. A binding inside a function body is
    private — nothing can import it — and is already covered by the call site
    that contains it. A binding at module scope is importable by any other
    module, which makes that module an email capability whether or not it ever
    calls the sender itself. So it declares itself, and the chain is cut at the
    source rather than chased.

    Cheap in practice: three modules in this repository bind at module level.
    """
    bound = set()
    for node in tree.body:                       # module level ONLY
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name in _SENDERS:
                    bound.add(a.asname or a.name)
    return bound


def scan(root: pathlib.Path | None = None) -> Set[Tuple[str, str]]:
    """Every (module, function) under `app/` that calls a sender.

    AST, not grep: a substring search reports docstrings that merely MENTION
    send_email — `marketing.py` has one — and a test that fails on its subject
    being described is a test people learn to weaken.

    KNOWN BOUNDARY — documented rather than papered over. A static scan cannot
    see a sender reached through fully dynamic dispatch:

        getattr(importlib.import_module("…smtp_imap"), "send_email")(…)

    That is not a plausible accident; it is deliberate evasion, and no static
    control stops a developer determined to evade it. `_dynamic_suspects()`
    flags the naive literal form so the common case is still noisy. The
    guarantee this module offers is precise: **a sender reached by name,
    alias, attribute, wrapper or re-export is declared or the process refuses
    to start.**
    """
    base = pathlib.Path(root) if root else _ROOT
    found: Set[Tuple[str, str]] = set()
    # Parsing every module surfaces warnings that belong to those modules, not
    # to this check — `kb_resolver_devset.py` has a non-raw '\w' that emits a
    # SyntaxWarning. Reporting somebody else's lint on every boot trains people
    # to ignore this control's output, which is the opposite of the point.
    import warnings
    for path in sorted((base / "app").rglob("*.py")):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception as exc:            # unparseable file is CI's problem
            logger.debug(f"[email_call_sites] skipped {path}: {exc}")
            continue
        rel = path.relative_to(base).as_posix()
        if rel == _SENDER_MODULE:
            continue

        senders = _sender_names(tree)          # canonical names + local aliases
        called: Set[str] = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = (node.func.attr if isinstance(node.func, ast.Attribute)
                            else getattr(node.func, "id", ""))
                    if name in senders:
                        called.add(name)
                        found.add((rel, fn.name))

        # A module-level binding is importable by anything, which makes this
        # module an email capability in its own right. Reported under a
        # synthetic function name so it lands in the same undeclared list
        # rather than in a second mechanism nobody reads.
        for name in _reexports(tree):
            found.add((rel, f"<re-export:{name}>"))

        for fn_name in _dynamic_suspects(tree):
            found.add((rel, f"<dynamic:{fn_name}>"))
    return found


def _dynamic_suspects(tree: ast.AST) -> Set[str]:
    """Functions that name a sender in a `getattr(...)` string literal.

    A heuristic, and honestly labelled as one: it catches
    `getattr(mod, "send_email")` and nothing cleverer. Its value is that the
    naive form of the one bypass a static scan cannot close is still noisy,
    rather than silently permitted.
    """
    out: Set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "getattr"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value in _SENDERS:
                        out.add(fn.name)
    return out


def audit(root: pathlib.Path | None = None) -> Dict[str, Any]:
    """Compare reality against the allowlist, both directions.

    STALE ENTRIES MATTER TOO. An allowlist that still names a call site somebody
    deleted is an allowlist nobody is maintaining, and an unmaintained
    allowlist is the thing a future bypass hides in.
    """
    actual = scan(root)
    declared = set(AUTHORIZED_SENDERS)
    undeclared = sorted(actual - declared)
    stale = sorted(declared - actual)
    return {"ok": not undeclared, "found": len(actual),
            "declared": len(declared),
            "undeclared": undeclared, "stale": stale}


def check() -> Dict[str, Any]:
    """release_guard-shaped result. BLOCKING: an undeclared sender in a
    deployed environment means a path that may email staff without passing
    through the governance layer, and we cannot tell which from here."""
    try:
        rep = audit()
    except Exception as exc:                                   # pragma: no cover
        return {"control": "email_call_sites", "ok": False,
                "severity": "advisory",
                "message": f"could not scan for send_email call sites: {exc}"}

    if rep["undeclared"]:
        listed = "; ".join(f"{m}::{f}()" for m, f in rep["undeclared"])
        return {
            "control": "email_call_sites", "ok": False, "severity": "blocking",
            "message": (f"{len(rep['undeclared'])} undeclared send_email call "
                        f"site(s): {listed}. If it can reach staff, route it "
                        f"through app.core.staff_email; if it is customer mail, "
                        f"declare it in app/core/email_call_sites.py with a "
                        f"reason.")}

    if rep["stale"]:
        listed = "; ".join(f"{m}::{f}()" for m, f in rep["stale"])
        return {"control": "email_call_sites", "ok": True,
                "severity": "advisory",
                "message": (f"allowlist names {len(rep['stale'])} call site(s) "
                            f"that no longer exist: {listed}. Remove them — a "
                            f"stale allowlist is an unmaintained one.")}

    return {"control": "email_call_sites", "ok": True, "severity": "ok",
            "message": (f"{rep['found']} send_email call sites, all declared; "
                        f"staff mail is governed by app.core.staff_email")}


def report() -> List[str]:
    """Human-readable inventory, for a reviewer rather than a machine."""
    rep = audit()
    lines = [f"send_email call sites: {rep['found']} found, "
             f"{rep['declared']} declared"]
    for site in sorted(scan()):
        why = AUTHORIZED_SENDERS.get(site, "*** UNDECLARED ***")
        lines.append(f"  {site[0]}::{site[1]}()  — {why}")
    return lines
