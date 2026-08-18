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



# ── Resolver outcomes (N2) ──────────────────────────────────────────────────
# Every failure in this programme has been attributed by INFERENCE from the
# final response, and the inference was wrong twice. Stage 3 reported the
# resolver as 2 of 45 failures; probing it directly said 41 of 45. The two are
# observationally identical downstream: when resolve() returns None the cutover
# does not engage and the legacy router answers, so the response looks the same
# whether the resolver declined, was never consulted, or was bypassed.
#
# So the resolver now states which of those happened. These are outcomes of a
# decision, not log levels, and they are asserted in CI — an instrument that
# cannot be tested is the thing that produced the wrong attributions.
OUTCOME_MATCHED       = "matched"          # intent produced
OUTCOME_MISSING_TARGET = "missing_target"  # operation clear, target absent
OUTCOME_UNSUPPORTED   = "unsupported"      # object does not support operation
OUTCOME_NO_OPERATION  = "no_operation"     # no operation verb recognised
OUTCOME_NO_OBJECT     = "no_object"        # operation clear, no object anywhere
OUTCOME_FORM_OWNED    = "form_owned"       # create/update belong to the UI form
OUTCOME_NOT_IMPERATIVE = "not_imperative"  # question, negation, read, ambiguity
OUTCOME_BYPASSED      = "bypassed"         # module not enabled for the cutover
OUTCOME_NOT_REACHED   = "not_reached"      # cutover code never ran

ALL_OUTCOMES = (OUTCOME_MATCHED, OUTCOME_MISSING_TARGET, OUTCOME_UNSUPPORTED,
                OUTCOME_NO_OPERATION, OUTCOME_NO_OBJECT, OUTCOME_FORM_OWNED,
                OUTCOME_NOT_IMPERATIVE, OUTCOME_BYPASSED, OUTCOME_NOT_REACHED)

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
    ("archive",    r"archiv\w*|put\s+(\w+\s+){0,3}away|get\s+rid\s+of"),
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
    # Contracted auxiliaries are listed individually. `do\s*n[o']?t` covers
    # "do not" and "don't" but NOT "didn't", "won't" or "can't" — those are
    # different auxiliaries, not different spellings of the same one, and they
    # matched no guard at all. "we won't be merging these" was refused only
    # because no request-framing rule happened to fire: safety by coincidence,
    # and the next recall change removes the coincidence.
    r"\b("
    r"do\s*n[o']?t|does\s*n[o']?t|did\s*n[o']?t|don t|"
    r"wo\s*n[o']?t|ca\s*n[o']?t|cannot|"
    r"should\s*n[o']?t|would\s*n[o']?t|must\s*n[o']?t|"
    r"is\s*n[o']?t|are\s*n[o']?t|"
    r"never|no need to|rather not|instead of|without|avoid|hold off"
    r")\b", re.I)

# Capability / explanatory questions. "Can I merge..." asks what is possible;
# "Can you merge..." asks for it to happen. The pronoun carries the whole
# distinction, so they are separated rather than lumped as "interrogative".
_CAPABILITY_Q_RE = re.compile(
    r"^\s*(can|could|may)\s+(i|we|a|an|the|this|these|those|contacts?|"
    r"accounts?|leads?)\b(?!\s+(get|have|see|grab|pull)\b)"
    r"|^\s*(how|why|when|which|does|do|is|are|tell\s+me|explain)\b"
    r"|^\s*what\s+(is|are|does|do|happens?|makes?)\b"
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
    r"check|view|tell)\b(?!\s+rid\b)", re.I)

# A write clause joined to a read clause. "Find the duplicates AND MERGE the
# ones I pick" contains an instruction the read guard would otherwise discard.
_TRAILING_WRITE_RE = re.compile(
    r"\b(and|then|,)\s+(please\s+)?(also\s+)?"
    r"(merge|archive|restore|delete|convert|qualify|disqualify|score|"
    r"update|combine|consolidate)\b", re.I)

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
# Two or more adjacent word tokens, CASE-INSENSITIVE. The previous pattern
# required capitals, which cost every request typed the way people actually
# type: "i want isla bennett archived", "hugo mendes should be disqualified".
# Requiring capitals is requiring the user to punctuate before the system will
# identify who they mean.
#
# Case-insensitivity has to be paid for. Capitals were doing the work of
# separating a person's name from an ordinary noun phrase, so _NAME_STOP takes
# that job: any token that is CRM vocabulary — an operation verb, a record
# type, a determiner, a filler word — disqualifies the phrase. "duplicate
# contacts" and "the archived record" are noun phrases; "isla bennett" is not.
_NAME_RE = re.compile(r"(?<!^)\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+)\b")

_NAME_STOP = {
    # operations and their inflections
    "merge", "merged", "merging", "archive", "archived", "archiving",
    "restore", "restored", "restoring", "delete", "deleted", "deleting",
    "convert", "converted", "converting", "qualify", "qualified",
    "qualifying", "disqualify", "disqualified", "score", "scored", "scoring",
    "combine", "combined", "combining", "consolidate", "consolidated",
    "duplicate", "duplicates", "dupes", "dupe", "remove", "removed",
    "update", "updated", "create", "created", "put", "bring", "take", "get",
    # record types
    "contact", "contacts", "account", "accounts", "lead", "leads", "order",
    "orders", "invoice", "invoices", "opportunity", "opportunities",
    "product", "products", "record", "records", "customer", "customers",
    "company", "companies", "person", "people",
    # grammar and filler
    "the", "these", "those", "this", "that", "them", "they", "it", "its",
    "and", "for", "with", "from", "into", "onto", "please", "should", "would",
    "could", "want", "wants", "wanted", "need", "needs", "needed", "have",
    "has", "had", "are", "was", "were", "been", "being", "you", "your", "our",
    "his", "her", "their", "all", "any", "some", "one", "two", "both",
    "before", "after", "today", "tomorrow", "yesterday", "next", "last",
    "week", "month", "year", "friday", "monday", "audit", "mistake", "error",
    "not", "dont", "doesnt", "same", "old", "new", "just", "really", "sec",
    "when", "while", "since", "because", "over", "under", "about",
}
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
        for m in re.finditer(rf"\b({pat})\b", text, re.I):
            saw_any = True
            before = text[max(0, m.start() - 24):m.start()].lower()
            if not re.search(r"\b(into|to|as)\s+(an?\s+)?$", before):
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



# ── Guards, named ───────────────────────────────────────────────────────────
# Each entry is (rule_name, compiled_pattern, protects). The name reaches the
# response, because "not_imperative" in aggregate cannot answer the only
# question that matters when recall is missing: WHICH safety property is
# costing it, and is that trade worth making?
#
# Stage 4B measured 10 legitimate EXECUTE/ASK requests rejected by this set
# without being able to say which member did it.
_GUARDS = [
    ("negation",        _NEGATION_RE,
     "a negated request must never execute"),
    ("capability_question", _CAPABILITY_Q_RE,
     "'can I merge' asks what is possible; answering it by merging is the "
     "worst possible reading"),
    ("past_description", _PAST_DESC_RE,
     "'which contacts were merged' reports history; it must not create it"),
    ("ambiguous_verb",  _AMBIGUOUS_RE,
     "'take care of these' names no operation; guessing one is substitution"),
    ("read_request",    _READ_LEAD_RE,
     "reads belong to the modules; claiming them would replace a working "
     "list with a write"),
]


def guard_for(text: str):
    """(rule_name, protects) for the guard that rejects `text`, else (None, None).

    A guard is skipped when the same sentence also carries an explicit
    instruction. Two shapes made this necessary, and both were the guard
    answering the easier half of a request:

        "Find the duplicates and merge the ones I pick"   read + write
        "...was archived in error and should be restored" description + write

    Rejecting these protected nothing — no negation, no capability question —
    while discarding an instruction the user plainly gave.
    """
    has_trailing_write = bool(_TRAILING_WRITE_RE.search(text))
    for name, rx, protects in _GUARDS:
        hit = rx.match(text) if name in ("ambiguous_verb", "read_request") \
            else rx.search(text)
        if not hit:
            continue
        # Negation is never overridden: "find the dupes and don't merge them"
        # must still refuse. The other guards yield to an explicit instruction.
        if has_trailing_write and name in ("read_request", "past_description"):
            continue
        return name, protects
    return None, None


def _find_operation_traced(text: str) -> "tuple[Optional[str], str, Optional[str]]":
    """(operation, outcome, rule).

    `rule` names the guard responsible when nothing was recognised. Reporting
    the guard rather than inferring it afterwards is the same correction made
    in Stage 4B one level up: a heuristic reconstruction of the cause was
    wrong, and the fixes for different causes point in opposite directions.
    """
    rule, _protects = guard_for(text)
    if rule:
        return None, OUTCOME_NOT_IMPERATIVE, rule
    op = _find_operation(text)
    if op:
        return op, OUTCOME_MATCHED, None
    return None, OUTCOME_NO_OPERATION, None


def _find_operation(text: str) -> Optional[str]:
    """The operation the user is ASKING FOR, or None.

    Verb position is no longer required to be first -- that rule explained only
    2 of the 14 in-scope v2 failures -- but every relaxation is paid for by a
    framing guard, because "mentions merge" and "asks to merge" are different
    sentences and only one of them may execute.
    """
    # Guards run through guard_for(), the single source of truth. They used to
    # be re-implemented inline here, so the same five checks existed twice —
    # and the copies could drift without anything noticing. The symptom that
    # exposed it was a sabotage test that disabled a guard and saw no change:
    # the other copy was still running, which means the test proved nothing
    # about the guard it was aiming at.
    #
    # Each guard is a reason NOT to act, and outranks any verb found later.
    rule, _protects = guard_for(text)
    if rule:
        return None

    # Verb in first position -- the unambiguous imperative.
    for name, pat in _VERB_MAP:
        if re.match(rf"^\s*(please\s+)?({pat})\b", text, re.I):
            return name

    # Verb anywhere in the sentence. The previous version required one of a
    # list of recognised request framings, which is why five phrasing families
    # scored between 0% and 29%: "needs merging", "would you mind archiving",
    # "should be merged" and "these are dupes, merge them" are all ordinary
    # requests wearing frames nobody had enumerated. Enumerating frames is an
    # unbounded job — there is always another way to ask politely.
    #
    # Safety does not come from the frame list. It comes from the guards above,
    # which is the right place for it: negation, capability questions,
    # past-tense description, reads and ambiguity are what separate "mentions
    # merge" from "asks to merge", and every one of them is checked BEFORE this
    # point. A verb that survives all five guards is a request, whatever
    # sentence position it occupies.
    #
    # This is the trade the guard-calibration work in Stage 5 was buying: make
    # the guards precise enough to carry the safety load alone, then stop
    # second-guessing them with a frame whitelist.
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
        name = _first_proper_name(text)
        if name:
            params["name"] = name
    return params


def _first_proper_name(text: str) -> Optional[str]:
    """The first multi-token phrase that reads as a name, or None.

    Every candidate token must be outside _NAME_STOP. A single CRM word
    anywhere in the phrase disqualifies it, because that is the signal capitals
    used to carry: "duplicate contacts" and "archived record" are noun phrases
    describing WHAT, while "isla bennett" and "roy health group" name WHICH.

    Returning None is the safe outcome — it produces MissingTarget, and asking
    which record is always better than acting on a phrase that merely looked
    like a name.
    """
    # REVERTED to requiring capitals, and the reason is worth keeping.
    #
    # A case-insensitive version was built and measured: it read lowercase
    # names correctly ("isla bennett", "hugo mendes", "roy health group") and
    # was rejected anyway. Scanning for runs of non-stop-list tokens also
    # produced targets out of ordinary English — "be possible to", "first
    # place", "is needing", "email basically" — because a stop-list cannot
    # enumerate every word that is not a name. Sixteen ASK cases invented a
    # target, and one capability question became an executable
    # account.restore on a record called "be possible to".
    #
    # Capitals are a weak signal, but they are the user's own assertion that a
    # token is a proper noun. Nothing in the sentence's shape replaces that.
    # Reading lowercase names needs a different KIND of evidence — matching
    # candidate spans against real record names in the database — not a better
    # guess about English. Until then, missing the name and asking which
    # record is the correct outcome.
    m = _NAME_RE.search(text)
    if not m:
        return None
    phrase = m.group(1).strip()
    tokens = [t for t in re.split(r"\s+", phrase) if t]
    if len(tokens) < 2 or any(t.lower().strip("'’-") in _NAME_STOP
                              for t in tokens):
        return None
    return phrase


def _resolve_core(message: str, object_hint: Optional[str] = None
                  ) -> "tuple[Any, str]":
    """Prose -> StructuredIntent.

    None means "not an operation request" — the caller keeps its existing
    behaviour, which is what protects lookups, reports and conversation.
    Raises MissingTarget / UnsupportedOperation when the operation IS clear but
    cannot be executed as asked; those must reach the user as an explicit
    question or refusal, never as a substituted mode.
    """
    text = (message or "").strip()
    if not text or not _IMPERATIVE_RE.match(text):
        return None, OUTCOME_NOT_IMPERATIVE

    operation, why, _rule = _find_operation_traced(text)
    if operation is None:
        return None, why
    if operation in FORM_OWNED:
        return None, OUTCOME_FORM_OWNED

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
        return None, OUTCOME_NO_OBJECT

    # void_invoice is named for its SP mode; the verb is just "void".
    if operation == "void_invoice" and obj != "invoice":
        return None, OUTCOME_NO_OBJECT

    if operation not in SP_MODES.get(obj, set()):
        raise UnsupportedOperation(obj, operation,
                                   TRUTH.objects_supporting(operation))

    params = _find_target(text, operation)
    needs = REQUIRES_TARGET.get(operation)
    if needs and not params:
        raise MissingTarget(obj, operation, needs)

    return (StructuredIntent(object=obj, operation=operation,
                            parameters=params), OUTCOME_MATCHED)


def resolve_traced(message: str, object_hint: Optional[str] = None
                   ) -> "tuple[Any, str]":
    """(payload, outcome) and NEVER raises.

    payload is a StructuredIntent when matched, the MissingTarget /
    UnsupportedOperation instance when the request is understood but cannot be
    executed as asked, else None. Telemetry that throws is missing precisely
    when something went wrong, so the raising core is wrapped rather than
    exposed.
    """
    try:
        return _resolve_core(message, object_hint)
    except MissingTarget as exc:
        return exc, OUTCOME_MISSING_TARGET
    except UnsupportedOperation as exc:
        return exc, OUTCOME_UNSUPPORTED


def resolve(message: str, object_hint: Optional[str] = None
            ) -> Optional[StructuredIntent]:
    """Raising facade, unchanged for existing callers."""
    payload, outcome = resolve_traced(message, object_hint)
    if outcome == OUTCOME_MISSING_TARGET:
        raise payload
    if outcome == OUTCOME_UNSUPPORTED:
        raise payload
    return payload if outcome == OUTCOME_MATCHED else None


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
