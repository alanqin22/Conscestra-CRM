"""Structured intent: the single place an operation is determined.

Phase 8 measured operation preservation at 30% on an independent blind holdout.
Every layer above the module agent was correct — premise, intent, object,
authorization — and then the module discarded it. The orchestrator already knew
the object and the operation; it forwarded the raw sentence instead, and each
module re-derived the operation twice: once by noun-phrase heuristics that never
read the verb, once by its own LLM.

Phase 7 fixed the first derivation and the substitution moved to the second.
Two doors, one room. So this does not improve rediscovery — it removes it. A
module handed a StructuredIntent can execute it or raise; it has no vocabulary
for choosing a different one, because it is never handed the prose that would
let it choose. Substitution stops being a defect to detect and becomes a state
that cannot be represented.

DETERMINISTIC, DELIBERATELY
No model in this path. Four separate LLM graders in this programme were wrong
in the direction their author expected, and the failure being fixed is itself a
model inventing operational detail. A rule that reads kb_capability_truth cannot
invent a mode that does not exist.

THE HARD RULE
    operation identified + target identified  -> StructuredIntent
    operation identified + NO target          -> MissingTarget, never a downgrade
Answering an easier question than the one asked is the whole defect.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.core import kb_capability_truth as TRUTH

logger = logging.getLogger(__name__)


class IntentError(Exception):
    """Base for resolution failures. Never swallowed into a fallback mode."""


class MissingTarget(IntentError):
    """The operation is clear; which records to act on is not."""

    def __init__(self, obj: str, operation: str, needs: str):
        self.object, self.operation, self.needs = obj, operation, needs
        super().__init__(f"{operation} on {obj} requires {needs}")


class UnsupportedOperation(IntentError):
    """The pairing does not exist in the implementation."""

    def __init__(self, obj: str, operation: str, supported):
        self.object, self.operation, self.supported = obj, operation, supported
        super().__init__(f"{obj} does not support {operation}")


# ── SP mode inventory, per object ───────────────────────────────────────────
# Extracted from pg_proc IF/ELSIF p_mode dispatch. This is the ONLY set of
# operations that may cross the boundary: a mode absent here cannot be named,
# which is what makes an invented operation unrepresentable rather than merely
# unlikely.
SP_MODES: Dict[str, set] = {
    "contact": {"activities", "archive", "create", "duplicates", "get_details",
                "list", "merge", "restore", "send_verification", "summary",
                "update", "verify_email"},
    "account": {"archive", "create", "duplicates", "financials", "get", "list",
                "list_owner", "merge", "restore", "summary", "timeline",
                "update"},
    "lead": {"archive", "convert", "create", "disqualify", "duplicates", "get",
             "list", "list_employee", "merge", "pipeline", "qualify", "restore",
             "score", "update"},
    "opportunity": {"add_product", "create", "delete", "forecast",
                    "forecast_accuracy", "get", "get_owners", "list",
                    "pipeline", "remove_product", "search_accounts",
                    "search_opportunities", "search_products", "update",
                    "update_product", "win_rate"},
    "order": {"account_search", "account_summary", "category_summary",
              "contact_search", "create", "delete", "get_category",
              "get_detail", "get_pricing", "get_product", "list",
              "list_employees", "sales_summary", "update"},
    "invoice": {"account_balance", "accounting_summary", "generate_invoice",
                "get_invoice_360", "list_invoices", "list_payments",
                "record_payment", "void_invoice"},
    "product": {"add", "bulk_adjust_stock", "get_details", "inventory_summary",
                "list", "list_categories", "low_stock", "price_history",
                "price_matrix", "product_search", "update"},
}

# Which parameter identifies the target, and whether the operation needs one.
# Reads only — list, duplicates, summary — legitimately act on no single record.
REQUIRES_TARGET: Dict[str, str] = {
    "merge":      "the records to merge (email, phone, or ids)",
    "archive":    "which record to archive (id or full name)",
    "restore":    "which record to restore (id or full name)",
    "delete":     "which record to delete (id)",
    "update":     "which record to update (id or full name)",
    "convert":    "which lead to convert (id or full name)",
    "qualify":    "which lead to qualify (id)",
    "disqualify": "which lead to disqualify (id)",
    "score":      "which lead to score (id)",
    "get_details": "which record (id or full name)",
    "void_invoice": "which invoice to void (id)",
    "record_payment": "the invoice and the amount",
}

# Operations the UI FORM PROTOCOL owns. Stage 1 shadow mode showed the resolver
# "disagreeing" with the module on create and update, and the module was right:
# "Add a new contact: Dana Whitfield…" correctly returns show_contact_form, and
# Phase 6 established that marker as a deliberate protocol the client consumes.
# Claiming those would replace a working form flow with a blind write — a
# regression dressed as a fidelity gain.
#
# This is exactly what shadow mode is for: the resolver's own scope was wrong,
# and it cost a log line instead of a production incident.
FORM_OWNED = {"create", "update"}

# Verb -> canonical operation. Order matters: longest/most specific first, so
# "disqualify" is never matched as "qualify".
# Lexical forms per operation. Hyphens are explicit: `unarchiv\w*` does not
# match "un-archive", which cost one v2 question outright.
#
# restore MUST be tested before archive: "un-archive" contains "archive", and a
# first-match table would restore nothing and archive everything.
_VERB_MAP = [
    ("restore",    r"un-?archiv\w*|un-?delet\w*|restor\w*|reinstat\w*|"
                   r"bring\s+(it|them|this|that|these|those)\s+back|"
                   r"put\s+(it|them|this|that)\s+back"),
    ("disqualify", r"disqualif\w*"),
    ("qualify",    r"qualif\w*"),
    ("merge",      r"merg\w*|combin\w*|consolidat\w*|de-?duplicat\w*|dedup\w*"),
    ("archive",    r"archiv\w*|put\s+(\w+\s+){0,3}away"),
    ("convert",    r"convert\w*|turn\s+(\w+\s+){0,3}into"),
    ("score",      r"scor\w*"),
    ("delete",     r"delet\w*|remov\w*"),
    ("create",     r"creat\w*|add\b|new\b"),
    ("update",     r"updat\w*|chang\w*|edit\b|set\b"),
    ("void_invoice", r"void\w*"),
]

# --- Framing: is the operation REQUESTED, or merely MENTIONED? --------------
# Raising recall on non-initial verbs is the dangerous half of this change. The
# guards below run BEFORE any verb is matched, and each exists because a
# sentence that mentions an operation must never become one.
#
# Negation. "Don't merge these" contains merge; acting on it is the worst
# possible reading of a user's words.
_NEGATION_RE = re.compile(
    r"\b(do\s*n[o']?t|don t|never|no need to|rather not|instead of|without|"
    r"should\s*n[o']?t|must\s*n[o']?t|avoid)\b", re.I)

# Capability / explanatory questions. "Can I merge..." asks what is possible;
# "Can you merge..." asks for it to happen. The pronoun carries the whole
# distinction, so they are separated rather than lumped as "interrogative".
_CAPABILITY_Q_RE = re.compile(
    r"^\s*(can|could|may)\s+(i|we|a|an|the|this|these|those|contacts?|"
    r"accounts?|leads?)\b"
    r"|^\s*(how|what|why|when|which|does|do|is|are|tell\s+me|explain)\b"
    r"|\bis\s+it\s+possible\b|\bam\s+i\s+able\s+to\b"
    r"|\bhow\s+does\b|\bwhat\s+happens\b", re.I)

# Past/passive description of something already done -- "Which contacts were
# merged?", "Why were these archived?" -- a report, not an instruction.
_PAST_DESC_RE = re.compile(
    r"\b(were|was|has\s+been|have\s+been|had\s+been|got)\s+\w*"
    r"(merged|archived|restored|deleted|converted|qualified)\b", re.I)

# Reads. The modules own these; the resolver declines them on purpose.
_READ_LEAD_RE = re.compile(
    r"^\s*(please\s+)?(show|list|find|get|search|display|give|pull|look|"
    r"check|view|tell)\b", re.I)

# Vague verbs that name no operation. Guessing one here is exactly the
# substitution this whole programme exists to remove.
_AMBIGUOUS_RE = re.compile(
    r"^\s*(take\s+care\s+of|do\s+something|handle|deal\s+with|sort\s+out|"
    r"fix|clean\s+up|tidy)\b", re.I)

# Request framings that legitimately put the verb later in the sentence.
_REQUEST_FRAME_RE = re.compile(
    r"^\s*(please|could\s+you|can\s+you|would\s+you|i\s+need|i\s+want|"
    r"i'?d\s+like|let'?s|we\s+should|go\s+ahead)\b"
    r"|\bshould\s+be\s+\w+ed\b"
    r"|,\s*(please\s+)?\w+\s+(them|it|these|those|em|all)\b"
    r"|\bcan\s+you\b|\bcould\s+you\b", re.I)

_OBJECT_MAP = [
    ("opportunity", r"opportunit(y|ies)|deals?"),
    ("invoice",     r"invoices?|payments?"),
    ("account",     r"accounts?|compan(y|ies)"),
    ("contact",     r"contacts?|people|persons?"),
    ("lead",        r"leads?|prospects?"),
    ("order",       r"orders?"),
    ("product",     r"products?|sku"),
]

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
_NAME_RE = re.compile(r"(?<!^)\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+\b")
# Referents that look like targets and identify nothing.
_VAGUE_RE = re.compile(
    r"\b(these|those|them|it|the (duplicate|dupe)s?|all|any|my)\b", re.I)
_IMPERATIVE_RE = re.compile(r"^\s*(please\s+)?[a-z]+\b", re.I)


@dataclass(frozen=True)
class StructuredIntent:
    """What the orchestrator determined. Modules execute this, not prose."""
    object: str
    operation: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    source: str = "resolver"
    confidence: float = 1.0

    def __post_init__(self) -> None:
        # Invariant 1 — the mode must exist on that object's SP.
        modes = SP_MODES.get(self.object)
        if modes is None:
            raise UnsupportedOperation(self.object, self.operation, [])
        if self.operation not in modes:
            raise UnsupportedOperation(self.object, self.operation,
                                       sorted(modes))
        # Invariant 2 — capability truth must agree, where it has an opinion.
        # A missing row means "unknown", which stays silent; only an explicit
        # supported=False blocks. Same rule the premise firewall follows.
        row = TRUTH.get(self.object, self.operation)
        if row is not None and not row["supported"]:
            raise UnsupportedOperation(
                self.object, self.operation,
                TRUTH.objects_supporting(self.operation))

    def as_params(self) -> Dict[str, Any]:
        """The dict a module's sql_builder expects."""
        return {"mode": self.operation, **self.parameters}


def _names_only_outputs(text: str, named: str) -> bool:
    """True only when EVERY mention of `named` follows a destination
    preposition, and there is at least one mention.

    "Convert X into an account" names an OUTPUT. "Score the account" names the
    SUBJECT. Only the first may be overridden by the route.

    The `saw_any` guard is not defensive padding. Without it the function
    returned True when it found no mentions at all — vacuously, since a loop
    over nothing satisfies "every" — and that True authorised the route to
    override a stated object. It turned "Score the Harris Construction
    account" into lead.score: scoring a different record type than the one the
    user named, which is the substitution this whole programme exists to stop.
    """
    saw_any = False
    for pat_name, pat in _OBJECT_MAP:
        if pat_name != named:
            continue
        for m in re.finditer(rf"({pat})", text, re.I):
            saw_any = True
            before = text[max(0, m.start() - 24):m.start()].lower()
            if not re.search(r"(into|to|as)\s+(an?\s+)?$", before):
                return False          # this mention is the subject
    return saw_any


def _find_object(text: str, operation: Optional[str] = None) -> Optional[str]:
    """The record type acted ON -- not merely a noun in the sentence.

    "Convert Mason Reid into an account and an opportunity" names three record
    types. Convert belongs to leads; the other two are its OUTPUTS. Taking the
    first noun found returned `opportunity`, which has no convert mode, so a
    valid request was refused as unsupported.

    Given an operation, only objects that actually support it are eligible.
    """
    found = [name for name, pat in _OBJECT_MAP
             if re.search(rf"\b({pat})\b", text, re.I)]
    if not found:
        return None
    if operation:
        eligible = [o for o in found if operation in SP_MODES.get(o, set())]
        if eligible:
            return eligible[0]
    return found[0]


def _find_operation(text: str) -> Optional[str]:
    """The operation the user is ASKING FOR, or None.

    Verb position is no longer required to be first -- that rule explained only
    2 of the 14 in-scope v2 failures -- but every relaxation is paid for by a
    framing guard, because "mentions merge" and "asks to merge" are different
    sentences and only one of them may execute.
    """
    # Each guard is a reason NOT to act, and outranks any verb found later.
    if _NEGATION_RE.search(text):
        return None
    if _CAPABILITY_Q_RE.search(text):
        return None
    if _PAST_DESC_RE.search(text):
        return None
    if _AMBIGUOUS_RE.match(text):
        return None
    if _READ_LEAD_RE.match(text):
        return None

    # Verb in first position -- the unambiguous imperative.
    for name, pat in _VERB_MAP:
        if re.match(rf"^\s*(please\s+)?({pat})\b", text, re.I):
            return name

    # Verb later in the sentence, but only inside a recognised request framing.
    # Without that condition "there's a bunch of dupes in here" and "we already
    # merged those" would read alike.
    if _REQUEST_FRAME_RE.search(text):
        for name, pat in _VERB_MAP:
            if re.search(rf"\b({pat})\b", text, re.I):
                return name
    return None


def _find_target(text: str, operation: str) -> Dict[str, Any]:
    """Identifying parameters, or {} when the user named nothing concrete."""
    params: Dict[str, Any] = {}
    m = _UUID_RE.search(text)
    if m:
        params["recordId"] = m.group(0)
        return params
    m = _EMAIL_RE.search(text)
    if m:
        params["email"] = m.group(0)
        if operation == "merge":
            params["operation"] = "by_email"
        return params
    m = _PHONE_RE.search(text)
    if m:
        params["phone"] = m.group(0).strip()
        if operation == "merge":
            params["operation"] = "by_phone"
        return params
    # A full proper name only — "these contacts" must not qualify.
    stripped = re.sub(r"^\s*(please\s+)?\w+\b", "", text).strip()
    if not _VAGUE_RE.match(stripped):
        m = _NAME_RE.search(text)
        if m:
            params["name"] = m.group(0)
    return params


def resolve(message: str, object_hint: Optional[str] = None
            ) -> Optional[StructuredIntent]:
    """Prose -> StructuredIntent.

    None means "not an operation request" — the caller keeps its existing
    behaviour, which is what protects lookups, reports and conversation.
    Raises MissingTarget / UnsupportedOperation when the operation IS clear but
    cannot be executed as asked; those must reach the user as an explicit
    question or refusal, never as a substituted mode.
    """
    text = (message or "").strip()
    if not text or not _IMPERATIVE_RE.match(text):
        return None

    operation = _find_operation(text)
    if operation is None or operation in FORM_OWNED:
        return None

    named = _find_object(text, operation)
    if named and operation not in SP_MODES.get(named, set())             and object_hint and operation in SP_MODES.get(object_hint, set())             and _names_only_outputs(text, named):
        # The named types are DESTINATIONS of the operation, not its subject:
        # "Convert <lead> into an account and an opportunity". The route knows
        # the subject, so it wins.
        logger.debug(f"[resolver] {named!r} follows a destination preposition; "
                     f"using route object {object_hint!r}")
        named = object_hint
    elif named and operation not in SP_MODES.get(named, set()):
        # The user named the subject and it does not support the operation.
        # REFUSE. Retargeting to whatever the route happened to pick turns
        # "Score the Harris Construction account" into lead.score — silently
        # scoring a different record type than the one named. An earlier
        # version of this function did exactly that; a hint may SUPPLY a
        # missing object, never OVERRIDE a stated one.
        raise UnsupportedOperation(named, operation,
                                   TRUTH.objects_supporting(operation))
    obj = named or object_hint
    if obj is None:
        return None

    # void_invoice is named for its SP mode; the verb is just "void".
    if operation == "void_invoice" and obj != "invoice":
        return None

    if operation not in SP_MODES.get(obj, set()):
        raise UnsupportedOperation(obj, operation,
                                   TRUTH.objects_supporting(operation))

    params = _find_target(text, operation)
    needs = REQUIRES_TARGET.get(operation)
    if needs and not params:
        raise MissingTarget(obj, operation, needs)

    return StructuredIntent(object=obj, operation=operation, parameters=params)


def shadow(message: str, actual_mode: str,
           object_hint: Optional[str] = None) -> Dict[str, Any]:
    """Stage 1: resolve and compare WITHOUT changing behaviour.

    Logged so the agreement rate is measured on real traffic before anything
    is routed through it. Shipping the resolver dark is the difference between
    a migration and a rewrite.
    """
    rec: Dict[str, Any] = {"message": message[:160], "actual_mode": actual_mode,
                           "resolved": None, "outcome": "none"}
    try:
        intent = resolve(message, object_hint)
        if intent is None:
            rec["outcome"] = "not_an_operation"
        else:
            rec["resolved"] = {"object": intent.object,
                               "operation": intent.operation,
                               "parameters": intent.parameters}
            rec["outcome"] = ("agree" if intent.operation == actual_mode
                              else "DISAGREE")
    except MissingTarget as exc:
        rec["outcome"] = "missing_target"
        rec["resolved"] = {"object": exc.object, "operation": exc.operation,
                           "needs": exc.needs}
    except UnsupportedOperation as exc:
        rec["outcome"] = "unsupported"
        rec["resolved"] = {"object": exc.object, "operation": exc.operation}
    except Exception as exc:                                # pragma: no cover
        rec["outcome"] = f"error:{type(exc).__name__}"
    logger.info(f"[intent-shadow] {rec['outcome']} "
                f"actual={actual_mode} resolved={rec['resolved']}")
    return rec
