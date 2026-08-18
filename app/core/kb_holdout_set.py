"""Independent holdout — authored from the IMPLEMENTATION, not the KB.

WHY A SECOND SET
The 125 seeded articles were written after reading which golden questions
failed, so coverage measured on the golden set is teaching to the test. Any
number from it overstates generalization by an unknown margin.

INDEPENDENCE CLAIM, STATED HONESTLY
These questions were derived from the stored-procedure mode inventory and the
schema — sp_contacts/sp_accounts/sp_leads/sp_opportunities/sp_activities modes,
column presence, table presence — NOT from article titles. The author had
already seen the articles, so this is weaker than a blind second evaluator; it
is independence of DERIVATION, not of knowledge. Treat the resulting figure as
an upper bound, and note where it lands relative to the contaminated one.

What makes it still worth running: the `expected` field below is ground truth
from the implementation, so a question can be scored WRONG even when an article
confidently answers it. The golden set could not do that — it had no notion of
what the product actually does.

CATEGORIES
    answerable      the product does this; a correct answer exists
    unanswerable    the product does not do this; refusal is correct
    boundary        adjacent to a real capability but asking for what is absent
    near_neighbour  a very similar article exists that answers a DIFFERENT thing
    false_premise   the question assumes a capability that does not exist
"""

from __future__ import annotations

from typing import Any, Dict, List

ANSWERABLE, UNANSWERABLE, BOUNDARY, NEAR_NEIGHBOUR, FALSE_PREMISE = (
    "answerable", "unanswerable", "boundary", "near_neighbour", "false_premise")


def _h(question: str, kind: str, expected: str, evidence: str) -> Dict[str, Any]:
    """expected  = what a correct response must convey (or REFUSE).
       evidence  = the implementation fact that makes `expected` true."""
    return {"question": question, "kind": kind,
            "expected": expected, "evidence": evidence}


HOLDOUT: List[Dict[str, Any]] = [

    # ── ANSWERABLE — capability verified present ────────────────────────────
    _h("Can I combine two contact records that are the same person?",
       ANSWERABLE, "Yes — contacts support merge (by email or phone).",
       "sp_contacts mode 'merge'"),
    _h("Is there a way to spot companies entered twice?",
       ANSWERABLE, "Yes — accounts have a duplicates finder.",
       "sp_accounts mode 'duplicates'"),
    _h("Can I mark a lead as not worth pursuing without deleting it?",
       ANSWERABLE, "Yes — leads support disqualify.",
       "sp_leads mode 'disqualify'"),
    _h("How do I record that I phoned a customer?",
       ANSWERABLE, "Log a call activity.",
       "sp_activities mode 'log_call'"),
    _h("How do I book a meeting against a customer record?",
       ANSWERABLE, "Schedule a meeting activity.",
       "sp_activities mode 'schedule_meeting'"),
    _h("Can I see which of my tasks are late?",
       ANSWERABLE, "Yes — overdue activities are listed.",
       "sp_activities mode 'overdue'"),
    _h("Can I add a product line to a deal?",
       ANSWERABLE, "Yes — opportunities support adding products.",
       "sp_opportunities mode 'add_product'"),
    _h("Can the system tell me how accurate my past forecasts were?",
       ANSWERABLE, "Yes — forecast accuracy is reported.",
       "sp_opportunities mode 'forecast_accuracy'"),
    _h("How do I confirm a customer's email address is real?",
       ANSWERABLE, "Send a verification; the contact carries a verified flag.",
       "sp_contacts modes 'send_verification' / 'verify_email'"),
    _h("Can I bring a lead into the customer records properly?",
       ANSWERABLE, "Yes — convert the lead.",
       "sp_leads mode 'convert'"),
    _h("Is there a score on leads to tell me which to work first?",
       ANSWERABLE, "Yes — leads carry a score.",
       "sp_leads mode 'score'; leads.score column"),
    _h("Can I put an account out of the way without losing its history?",
       ANSWERABLE, "Yes — archive it; restore is available.",
       "sp_accounts modes 'archive' / 'restore'"),
    _h("What does the customer receive when their order is on its way?",
       ANSWERABLE, "A shipped notification email.",
       "order_notifications event types; app/core/order_notifications.py"),
    _h("If sending a customer email fails, what is recorded?",
       ANSWERABLE, "A failure with its reason — never a send.",
       "order_notifications.state includes failed/skipped"),
    _h("Who is responsible for chasing this company?",
       ANSWERABLE, "The account owner.",
       "accounts.owner_id"),

    # ── UNANSWERABLE — verified absent ──────────────────────────────────────
    _h("How do I merge two opportunities into one deal?",
       UNANSWERABLE, "REFUSE — opportunities have no merge.",
       "sp_opportunities has no 'merge' mode (contacts/accounts/leads do)"),
    _h("How do I save my own dashboard layout with the widgets I choose?",
       UNANSWERABLE, "REFUSE — no user-created saved dashboards.",
       "no dashboard tables; only sp_home_index KPI cards"),
    _h("How do I set up a second sales pipeline for our services team?",
       UNANSWERABLE, "REFUSE — one pipeline only.",
       "no pipeline_id/pipeline_name anywhere in app/ or schema"),
    _h("Where do I pick the reason we lost, from the dropdown?",
       UNANSWERABLE, "REFUSE — no loss-reason field exists.",
       "opportunities has no lost_reason/loss_reason column"),
    _h("How do I edit a permission matrix for a specific user?",
       UNANSWERABLE, "REFUSE — roles are the unit of access; no permissions editor.",
       "no permission/role tables; employees.role only"),
    _h("How do I invoice a customer in euros as well as dollars?",
       UNANSWERABLE, "REFUSE — no multi-currency support found.",
       "no currency column on invoices"),
    _h("How do I set territory rules and quota targets per rep?",
       UNANSWERABLE, "REFUSE — no territory or quota feature.",
       "no territory/quota tables or SP modes"),
    _h("What is Conscestra's contractual uptime SLA?",
       UNANSWERABLE, "REFUSE — no SLA is published in the KB.",
       "no SLA article or commitment in the corpus"),

    # ── BOUNDARY — adjacent to something real ───────────────────────────────
    _h("Can I merge duplicate orders?",
       BOUNDARY, "REFUSE — merge exists for contacts/accounts/leads, not orders.",
       "sp_orders has no merge mode"),
    _h("Can a contact be linked to two accounts at once?",
       BOUNDARY, "No — contacts.account_id is a single link.",
       "contacts.account_id single FK; no junction table"),
    _h("Can I undo a merge after it has run?",
       BOUNDARY, "Not as one step — duplicates are soft-deleted and links moved.",
       "sp_contacts merge soft-deletes and reassigns"),
    _h("Can I schedule a report to email itself weekly?",
       BOUNDARY, "REFUSE for reports — the executive briefing is the scheduled artefact.",
       "no report scheduling; briefing jobs exist"),

    # ── NEAR NEIGHBOUR — a similar article answers something else ───────────
    _h("How do I archive a contact?",
       NEAR_NEIGHBOUR, "Archive/soft-delete — NOT the merge article.",
       "sp_contacts mode 'archive' is distinct from 'merge'"),
    _h("How do I restore a contact I archived?",
       NEAR_NEIGHBOUR, "Restore — NOT archive, and not delete.",
       "sp_contacts mode 'restore'"),
    _h("What is the difference between archiving and merging a contact?",
       NEAR_NEIGHBOUR, "Archive hides one record; merge folds two into one.",
       "distinct SP modes 'archive' and 'merge'"),
    _h("How do I see an account's invoices and payments?",
       NEAR_NEIGHBOUR, "Account financials — NOT the AR report.",
       "sp_accounts mode 'financials'"),

    # ── FALSE PREMISE — assumes something untrue ────────────────────────────
    _h("Which pipeline should I put this deal in?",
       FALSE_PREMISE, "Correct the premise — there is only one pipeline.",
       "single pipeline"),
    _h("How do I change the loss reason I selected on a closed deal?",
       FALSE_PREMISE, "Correct the premise — no loss-reason field exists.",
       "no loss_reason column"),
    _h("Why did the automatic nightly de-duplication merge my contacts?",
       FALSE_PREMISE, "Correct the premise — nothing merges in the background.",
       "no scheduled dedupe job in scheduler/main"),
    _h("How do I give a user read-only access to just one account?",
       FALSE_PREMISE, "Correct the premise — access is by role, not per record.",
       "role-based only; no per-record ACL"),
    _h("Where do I set the SLA timer on a support case?",
       FALSE_PREMISE, "Correct the premise — no SLA timer feature.",
       "no SLA fields on cases"),
]


def summary() -> Dict[str, Any]:
    by: Dict[str, int] = {}
    for c in HOLDOUT:
        by[c["kind"]] = by.get(c["kind"], 0) + 1
    should_refuse = sum(1 for c in HOLDOUT if c["expected"].startswith("REFUSE")
                        or c["kind"] == FALSE_PREMISE)
    return {"total": len(HOLDOUT), "by_kind": by, "should_refuse": should_refuse}
