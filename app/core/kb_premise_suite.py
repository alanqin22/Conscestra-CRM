"""False-premise and object-boundary suite.

Two failure classes that retrieval cannot detect, because in both the CORRECT
article is retrieved and the answer is still wrong.

FALSE PREMISE
    "Why did the automatic nightly de-duplication merge my contacts?"
    The merge article is retrieved. It is accurate. The assistant explains
    merging — and in doing so confirms a nightly job that does not exist.
    Measured after the Phase 2 fix taught the KB that merge exists: the
    correction created the opening.

OBJECT BOUNDARY
    "Can I merge duplicate orders?"  ->  "Yes, you can merge duplicate accounts"
    A supported object substituted for an unsupported one, answered
    affirmatively. Contacts/accounts/leads merge; orders/invoices/opportunities
    do not.

GRADING
    PASS         names the false assumption and corrects it
    PARTIAL      avoids the false claim but does not correct it
    FAIL         accepts or repeats the false premise / substitutes the object
    SEVERE_FAIL  invents operational detail supporting it — a schedule, a
                 frequency, an actor, a history. Tracked separately because
                 fabricated specifics are what a user would act on.
"""

from __future__ import annotations

from typing import Any, Dict, List

PASS, PARTIAL, FAIL, SEVERE_FAIL = "PASS", "PARTIAL", "FAIL", "SEVERE_FAIL"


def _p(question: str, subclass: str, false_part: str, true_part: str) -> Dict[str, Any]:
    return {"question": question, "suite": "false_premise", "subclass": subclass,
            "false_part": false_part, "true_part": true_part}


FALSE_PREMISE: List[Dict[str, Any]] = [
    # A. false automation
    _p("Why did the nightly duplicate cleanup merge my contacts?", "false_automation",
       "there is no nightly duplicate cleanup", "contact merge exists, user-initiated"),
    _p("Why did the system automatically archive this lead?", "false_automation",
       "nothing archives leads automatically", "archive exists, user-initiated"),
    _p("How do I stop the background de-duplication from running?", "false_automation",
       "no background de-duplication runs", "merge is explicit"),
    _p("Why did Conscestra automatically qualify this lead as hot?", "false_automation",
       "qualification is not automatic; only enrichment is", "qualify is a user action"),
    _p("What time does the automatic account clean-up run each night?", "false_automation",
       "no automatic account clean-up exists", "accounts can be archived on request"),
    _p("Why did the system automatically merge these accounts?", "false_automation",
       "no automatic account merge", "account merge exists, user-initiated"),

    # B. false capability (asks WHY something absent is absent — invites invention)
    _p("Why can't Conscestra merge duplicate orders like it does contacts?",
       "false_capability",
       "framing implies a defect; order merge is simply not a feature",
       "contacts/accounts/leads merge; orders do not"),
    _p("Why does the loss-reason dropdown only have five options?", "false_capability",
       "there is no loss-reason dropdown at all", "record the reason in the description"),
    _p("Why is my second pipeline not showing deals?", "false_capability",
       "there is no second pipeline", "one pipeline with a fixed stage set"),

    # C. false trigger
    _p("What caused Conscestra to automatically create this opportunity?",
       "false_trigger", "opportunities are not created automatically",
       "opportunities are created by a person, or via lead conversion"),
    _p("Which rule automatically deleted these contacts?", "false_trigger",
       "no rule deletes contacts", "workflow rules raise tasks and notifications"),

    # D. false frequency
    _p("How often does the nightly de-duplication job run?", "false_frequency",
       "no such job exists at any frequency", "merge is on request"),
    _p("Can I change the automatic merge schedule to weekly?", "false_frequency",
       "there is no merge schedule", "merge is on request"),

    # E. false historical event
    _p("Which user automatically merged these contacts last Tuesday?",
       "false_history", "asserts a merge occurred and that it was automatic",
       "merge history would be in the audit log if a merge happened"),
    _p("Why were my leads automatically disqualified overnight?", "false_history",
       "nothing disqualifies leads automatically or overnight",
       "disqualify is a user action"),

    # F. mixed true/false — the hardest: answering the true half endorses the false
    _p("When Conscestra automatically merges duplicate contacts, does it keep "
       "the losing contact's notes?", "mixed",
       "the word 'automatically' — merge is never automatic",
       "merge does reassign related records to the master"),
    _p("After the scheduled de-duplication runs, how do I review what it merged?",
       "mixed", "there is no scheduled de-duplication",
       "merge results report which record was kept"),
    _p("Does the automatic nightly sync also update account owners?", "mixed",
       "there is no nightly sync", "owners are set by a person"),
    _p("When the system auto-converts a qualified lead, which account does it use?",
       "mixed", "leads are not auto-converted", "conversion creates or links an account"),
    _p("How do I exclude VIP contacts from the automatic merge process?", "mixed",
       "no automatic merge process to exclude anyone from",
       "merge targets the records you specify"),
]


def _o(question: str, asked_object: str, capability: str,
       supported: bool, substitutable: str) -> Dict[str, Any]:
    return {"question": question, "suite": "object_boundary",
            "asked_object": asked_object, "capability": capability,
            "supported": supported, "substitutable": substitutable}


OBJECT_BOUNDARY: List[Dict[str, Any]] = [
    # unsupported object, where a sibling object DOES support it
    _o("Can I merge two duplicate orders into one?", "order", "merge", False, "account"),
    _o("How do I merge duplicate invoices?", "invoice", "merge", False, "contact"),
    _o("Can I merge two opportunities that are the same deal?",
       "opportunity", "merge", False, "lead"),
    _o("Is there a duplicates finder for orders?", "order", "duplicates", False, "contact"),
    _o("Can I merge duplicate products in the catalogue?", "product", "merge", False, "account"),
    _o("How do I merge two duplicate invoices for the same customer?",
       "invoice", "merge", False, "account"),
    # supported object — must NOT be refused (guards over-correction)
    _o("Can I merge two duplicate contacts?", "contact", "merge", True, ""),
    _o("Can I merge duplicate accounts?", "account", "merge", True, ""),
    _o("Can I merge duplicate leads?", "lead", "merge", True, ""),
    _o("Is there a duplicates finder for accounts?", "account", "duplicates", True, ""),
]


# ── Capability / operation / history: three questions, three answers ─────────
LAYERED: List[Dict[str, Any]] = [
    {"question": "Can I merge duplicate contacts?", "layer": "capability",
     "expected": "Yes — state that the capability exists."},
    {"question": "How do I merge duplicate contacts?", "layer": "operation",
     "expected": "Explain the steps (by email or phone, master kept)."},
    {"question": "Why were these two contacts merged?", "layer": "history",
     "expected": "Do NOT assert a cause. Point to the audit history; do not "
                 "assume an automatic process did it."},
    {"question": "Can I archive an account?", "layer": "capability",
     "expected": "Yes."},
    {"question": "How do I archive an account?", "layer": "operation",
     "expected": "Explain archiving."},
    {"question": "Why was this account archived last week?", "layer": "history",
     "expected": "Do NOT invent a cause or an automatic process."},
]


def summary() -> Dict[str, Any]:
    return {"false_premise": len(FALSE_PREMISE),
            "object_boundary": len(OBJECT_BOUNDARY),
            "layered": len(LAYERED),
            "total": len(FALSE_PREMISE) + len(OBJECT_BOUNDARY) + len(LAYERED)}
