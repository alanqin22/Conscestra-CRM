"""Premise firewall for the CRM-user path.

WHY THIS IS DETERMINISTIC AND NOT AN LLM CALL
The failure it exists to stop is a model inventing operational detail — "the
account clean-up job runs nightly at 02:30 AM server local time." Asking
another model to catch that reintroduces exactly the faculty that produced it,
and every audit so far has found the LLM graders wrong in both directions
(5/15 false-WRONG in Phase 2; 2 false-SEVERE in Phase 3). A rule that reads
`kb_capability_truth` cannot hallucinate a schedule, is inspectable in CI, adds
no latency, and fails closed.

WHAT IT DOES NOT DO
It does not answer questions and it does not route. It returns a correction
when a message asserts something the implementation contradicts, and None
otherwise. Anything it does not recognise flows on untouched to the existing
orchestrator — a firewall that swallowed ambiguous traffic would break the task
routing this path exists for, which is the explicit non-goal.

THE THREE INVARIANTS (Phase 3 established each as a real, measured failure)
    a capability existing      ≠  it runs automatically
    it running automatically   ≠  it ran in THIS case
    it applies to contacts     ≠  it applies to orders
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.core import kb_capability_truth as TRUTH

# ── vocabulary ──────────────────────────────────────────────────────────────

# Words that assert something runs on its own. "automatic" alone is enough:
# the invariant is that NO record-changing capability here is automatic.
_AUTOMATION_RE = re.compile(
    # auto-\w+ generally, not an enumerated list: "auto-converts" escaped the
    # first version because only merge/clean/dedup were listed.
    r"\b(automatic(ally)?|auto-\w+|nightly|overnight|"
    r"every night|each night|every evening|scheduled|schedule|background|"
    r"cron|batch job|nightly job|clean-?up job|daily job|runs? on its own|"
    r"by itself|on a schedule)\b", re.I)

# Asking WHEN/HOW OFTEN — the shape that invites an invented time.
_WHEN_RE = re.compile(
    r"\b(what time|when does|when did|how often|how frequently|what schedule|"
    r"which hour|at what time)\b", re.I)

# Past-tense assertion that it already happened.
_HISTORY_RE = re.compile(
    r"\b(why (did|was|were|has|have)|who (merged|archived|deleted|changed|"
    r"converted)|what caused|which (rule|job|process) (merged|deleted|archived)"
    r"|after the .{0,24}(ran|completed))\b", re.I)

# An explicit instruction to DO something — must not be intercepted as a
# question. Imperative mood, or "please <verb>".
_ACTION_RE = re.compile(
    r"^\s*(please\s+)?(merge|archive|delete|restore|convert|qualify|"
    r"disqualify|score|create|update|add|remove|void|send|assign|stop|"
    r"disable|turn off)\b", re.I)

_OBJECTS = {
    "contact": r"contacts?", "account": r"accounts?", "lead": r"leads?",
    "opportunity": r"opportunit(y|ies)|deals?", "order": r"orders?",
    "invoice": r"invoices?", "product": r"products?",
}

_CAPABILITIES = {
    "merge": r"merg\w*|combin\w*|de-?duplicat\w*|dedup\w*|duplicate clean-?up|"
             r"duplicate cleanup",
    "duplicates": r"duplicates?\s+(finder|detection|report|check|tool)",
    "archive": r"archiv\w*",
}

# Maintenance processes that DO NOT EXIST in any form — there is no scheduled
# job in app/main.py or the scheduler that cleans up, purges or syncs records.
# Held apart from _CAPABILITIES because there is no capability to reason about:
# the whole premise is invented, so any automation claim about one is false
# regardless of object.
#
# This is what stops the measured P0 — "the account clean-up job runs nightly at
# 02:30 AM". The Phase 3 wording was "account cleanup", which shares no words
# with "duplicate", so a capability-keyed rule could never have caught it.
# Matching the PROCESS NOUN rather than the phrasing is what makes this
# semantic instead of a string block on "02:30".
_PHANTOM_PROCESS_RE = re.compile(
    r"\b(clean-?up|cleanup|housekeeping|purge|tidy-?up|maintenance (job|run|"
    r"process|task)|sync (job|process)|reconciliation job|nightly (job|run|"
    r"process|task)|batch (job|run|process))\b", re.I)

# Capabilities that no object performs automatically. Kept explicit rather than
# derived, so that if a future capability DOES become automatic, the truth model
# is what changes and this list is what fails the test.
_NEVER_AUTOMATIC = ("merge", "duplicates", "archive")

# (regex, kb_capability_truth key, correction). The truth row is consulted at
# match time, so removing a row when a feature ships disables the rule with it.
_ABSENT_FEATURES = [
    (r"\b(loss[- ]reason|lost[- ]reason|reason (we |for )?(lost|losing))\b.*"
     r"(dropdown|picklist|field|option|list)|"
     r"(dropdown|picklist|field)\b.*\bloss[- ]reason\b",
     "loss_reason_field",
     "There is no loss-reason field, dropdown or picklist on opportunities in "
     "Conscestra — so it has no options to configure and none to change. "
     "Record why a deal was lost in the opportunity description or as a note "
     "activity before setting the stage to closed lost. Keeping that wording "
     "consistent is what makes the reasons readable in aggregate later."),

    # "second sales pipeline" — allow qualifying words between the determiner
    # and the noun, which the first version did not.
    (r"\b(second|another|additional|multiple|other|new)\s+(\w+\s+){0,2}pipelines?\b|"
     r"\bpipelines?\b.*\b(second|another|additional|multiple)\b",
     "multiple_pipelines",
     "Conscestra has one sales pipeline with a single set of stages — there is "
     "no second or additional pipeline, so there is none to configure or "
     "troubleshoot. To look at a subset of deals separately, filter the "
     "pipeline by owner, account or amount."),

    (r"\bsaved dashboards?\b|\b(my|custom|own)\s+dashboard\s+(layout|widgets)\b|"
     r"\bdashboard\s+(layout|widgets)\b",
     "saved_dashboards",
     "Conscestra does not have user-created saved dashboards — there is no "
     "layout or widget arrangement to save. The home dashboard provides fixed "
     "KPI cards and the Analytics module holds the reports; for a recurring "
     "view, the executive briefing arrives daily by email."),

    (r"\bpermission (matrix|grid|editor)\b|\bper-?record (access|permission)\b|"
     r"\bread-?only access to (just |only )?(one|a single)\b",
     "permissions_editor",
     "Conscestra has no permission matrix or per-record access control — "
     "access is decided by the employee's role, and there is no editor for "
     "granting rights on an individual record. If someone needs different "
     "access, that is a change of role."),

    (r"\bmulti-?currenc|\bin euros\b.*\bdollars\b|\bcurrency (setting|field)\b",
     "multi_currency",
     "Conscestra does not support multi-currency invoicing — invoices carry no "
     "currency field, so there is no setting to configure."),

    (r"\bterritor|\bquota\s+(target|rule|assignment)|\bquotas\b",
     "territory_quota",
     "Conscestra has no territory management or quota feature — there are no "
     "territory rules or per-rep quota targets to set. Ownership on accounts, "
     "contacts and opportunities is the routing mechanism."),
]


def _objects_in(text: str) -> List[str]:
    return [obj for obj, pat in _OBJECTS.items()
            if re.search(rf"\b({pat})\b", text, re.I)]


def _capabilities_in(text: str) -> List[str]:
    return [cap for cap, pat in _CAPABILITIES.items()
            if re.search(rf"\b({pat})\b", text, re.I)]


def _supported_objects(cap: str) -> List[str]:
    return TRUTH.objects_supporting(cap)


def _fmt_list(items: List[str]) -> str:
    items = [f"{i}s" for i in items]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def check(message: str) -> Optional[Dict[str, Any]]:
    """Return a correction dict, or None to let the message route normally.

    dict: {rule, correction, capability, objects, is_action}
    """
    text = (message or "").strip()
    if not text:
        return None

    objs = _objects_in(text)
    is_action = bool(_ACTION_RE.match(text))
    asserts_auto = bool(_AUTOMATION_RE.search(text))
    asks_when = bool(_WHEN_RE.search(text))
    asserts_history = bool(_HISTORY_RE.search(text))

    # ── RULE 0: a maintenance process that does not exist at all ────────────
    # Runs before capability matching, because the invented process usually has
    # no capability word in it ("account clean-up", not "duplicate merge").
    if _PHANTOM_PROCESS_RE.search(text) and (asserts_auto or asks_when
                                             or asserts_history):
        obj_txt = f" for {objs[0]}s" if objs else ""
        return {
            "rule": "phantom_process",
            "capability": None, "objects": objs, "is_action": is_action,
            "correction": (
                f"Conscestra runs no scheduled clean-up, purge, housekeeping or "
                f"maintenance job{obj_txt} — there is no such process, so it has "
                f"no run time, frequency or history. I will not give you a "
                f"schedule for something that does not exist. What does run "
                f"automatically is limited to: enrichment of new leads, workflow "
                f"rules raising tasks and notifications, order lifecycle emails, "
                f"invoice reminders and executive briefings. Nothing merges, "
                f"archives or deletes records on its own."),
        }

    # ── RULE 0b: automation asserted over ANY record-changing verb ──────────
    # Generalises rule 2 beyond merge/archive. The truth model says no
    # record-changing capability is automatic — creating, deleting,
    # disqualifying, converting and assigning are all user-invoked — so an
    # automation word attached to any of them is false regardless of which
    # capability keyword happens to appear. Phase 4 measured three distinct
    # escapes from the narrower rule: "what caused Conscestra to automatically
    # CREATE this opportunity" (which opened a create form), "which rule
    # automatically DELETED these contacts", and "why were my leads
    # automatically DISQUALIFIED overnight".
    _changing = re.search(
        r"\b(creat\w+|delet\w+|disqualif\w+|convert\w+|qualif\w+|assign\w+|"
        r"updat\w+|chang\w+|archiv\w+|merg\w+|remov\w+)\b", text, re.I)
    if _changing and (asserts_auto or asks_when or asserts_history) \
            and not is_action:
        verb = _changing.group(1).lower()
        return {
            "rule": "false_automation",
            "capability": None, "objects": objs, "is_action": False,
            "correction": (
                f"Nothing in Conscestra {verb} records automatically — there is "
                f"no rule, job or schedule that does. Record changes happen "
                f"because a person, or an agent acting on someone's explicit "
                f"request, asked for them, and AI-proposed changes above the "
                f"approval thresholds wait in the approval queue rather than "
                f"applying themselves. If a record changed and nobody recalls "
                f"doing it, the audit history is where to establish who and "
                f"when; I cannot attribute it to an automatic process, because "
                f"none exists. The only things that happen without being asked "
                f"are additive: enrichment of new leads, workflow rules raising "
                f"tasks and notifications, order lifecycle emails, invoice "
                f"reminders and executive briefings."),
        }

    # ── RULE 0c: a feature that does not exist in any form ──────────────────
    # Distinct from a wrong OBJECT (rule 1): here the feature itself is absent
    # for every object, so there is nothing to redirect to. Measured escapes:
    # "why does the loss-reason dropdown only have five options?" produced an
    # invented picklist ("your org's loss_reason picklist is configured with
    # exactly five active entries"), and "why is my second pipeline not showing
    # deals?" produced a pipeline report that tacitly accepted a second
    # pipeline. Both presuppose a field the schema does not have.
    #
    # Each entry is anchored to a kb_capability_truth row whose supported=False,
    # so a feature that later ships is removed from the truth model and this
    # rule stops firing for it — one place to change, not two.
    for pattern, truth_key, correction in _ABSENT_FEATURES:
        if re.search(pattern, text, re.I):
            row = TRUTH.get("system", truth_key) or TRUTH.get("opportunity", truth_key)
            if row is not None and not row["supported"]:
                return {
                    "rule": "absent_feature",
                    "capability": truth_key, "objects": objs,
                    "is_action": is_action, "correction": correction,
                }

    caps = _capabilities_in(text)
    if not caps:
        return None                      # nothing this firewall knows about
    cap = caps[0]

    supported = _supported_objects(cap)

    # ── RULE 1: wrong object ────────────────────────────────────────────────
    # Checked FIRST and for actions too. "Merge these duplicate orders" must be
    # corrected, not executed against some other object.
    for obj in objs:
        row = TRUTH.get(obj, cap)
        if row is not None and not row["supported"]:
            return {
                "rule": "wrong_object",
                "capability": cap, "objects": [obj], "is_action": is_action,
                "correction": (
                    f"Conscestra does not support {cap} for {obj}s. "
                    f"{cap.capitalize()} is available for {_fmt_list(supported)} "
                    f"only — a duplicate {obj} is cancelled or deleted rather "
                    f"than combined. I have not applied it to any other record "
                    f"type on your behalf."),
            }

    # ── RULE 2: false automation / schedule / history ───────────────────────
    if cap in _NEVER_AUTOMATIC and (asserts_auto or asks_when or asserts_history):
        obj_txt = f" for {objs[0]}s" if objs else ""
        parts = [f"There is no automatic, nightly, scheduled or background "
                 f"{cap}{obj_txt} in Conscestra, so there is no such process, "
                 f"schedule or run time."]
        if asserts_history:
            parts.append(
                "Nothing runs on its own, so if records were changed it was "
                "done by a person or by an agent acting on someone's explicit "
                "request — the audit history is where to establish who and "
                "when. I cannot attribute it to a routine job, because none "
                "exists.")
        if supported:
            parts.append(
                f"{cap.capitalize()} itself IS supported for "
                f"{_fmt_list(supported)}, but only when it is explicitly "
                f"requested.")
        return {
            "rule": ("false_history" if asserts_history
                     else "false_schedule" if asks_when else "false_automation"),
            "capability": cap, "objects": objs, "is_action": is_action,
            "correction": " ".join(parts),
        }

    # ── RULE 3: false capability denial ─────────────────────────────────────
    # "Why can't Conscestra merge contacts?" — the premise is that it cannot.
    if re.search(r"\b(why (can'?t|cannot|does ?n'?t|won'?t)|"
                 r"is there no|there is no)\b", text, re.I):
        for obj in objs:
            row = TRUTH.get(obj, cap)
            if row is not None and row["supported"]:
                return {
                    "rule": "false_denial",
                    "capability": cap, "objects": [obj], "is_action": is_action,
                    "correction": (
                        f"Conscestra does support {cap} for {obj}s — it is "
                        f"available whenever you ask for it. It simply does not "
                        f"run on its own."),
                }

    return None


def as_answer(result: Dict[str, Any]) -> str:
    """Render a correction as the user-facing response."""
    return result["correction"]
