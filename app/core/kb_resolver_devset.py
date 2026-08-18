"""DEVELOPMENT set for the operation resolver — NOT an acceptance set.

Authored by the implementer, deliberately, and therefore worthless as evidence
of generalisation. It exists to establish semantic boundaries before tuning, so
that the sealed acceptance holdout measures a design rather than a memory of
its own failures. Any score from this file is a development signal only and
must never be reported as operation preservation.

WHY IT EXISTS AT ALL
Attribution of the 14 in-scope v2 failures showed the working hypothesis —
"the verb must be first" — explained only 2 of them:

    R1 lexical gap        2   "un-archive" (hyphen defeats `unarchiv\w*`)
    R1 wrong object       1   "Convert X into an account and an opportunity"
                              picks *opportunity*; convert belongs to leads
    R1 verb position      2   "…, merge them" / "…, merge em"
    R3/R4 no object hint  3   "archive it" resolves correctly WHEN given the
                              route hint — it failed because the orchestrator
                              routed it to a module outside the cutover
    R4 module read-mode   6   "give me the timeline on this account" wanted
                              `timeline`, module chose `list`. The resolver
                              never sees these: they are reads, and it declines
                              reads on purpose.

So 6 of 14 are not resolver defects, and fixing verb extraction alone would
have moved the score by two questions while appearing to address the class.

SAFETY IS THE HARDER HALF
Raising recall on imperatives risks reading ordinary language as an action
request. The NEGATIVE and INFORMATIONAL blocks below are the counterweight:
they must never resolve to an executable intent, and they are checked in the
same pass as the recall cases so a recall gain that costs safety is visible
immediately rather than at acceptance.
"""

from __future__ import annotations
from typing import Any, Dict, List

# expected: ("op", have_target) | "ASK" | "NONE" | "REFUSE"
DEV: List[Dict[str, Any]] = [

    # ── verb-first (already working; regression guard) ──────────────────────
    {"q": "Merge these contacts.", "hint": "contact", "want": "ASK"},
    {"q": "Archive this contact.", "hint": "contact", "want": "ASK"},
    {"q": "Merge contacts with email a.b@x.ca", "hint": "contact", "want": ("merge", True)},

    # ── C1 lexical gaps ─────────────────────────────────────────────────────
    {"q": "un-archive these please", "hint": "contact", "want": "ASK"},
    {"q": "Un-delete that contact", "hint": "contact", "want": "ASK"},
    {"q": "put this account away", "hint": "account", "want": "ASK"},
    {"q": "bring it back", "hint": "contact", "want": "ASK"},
    {"q": "consolidate these two accounts", "hint": "account", "want": "ASK"},

    # ── C2/C3 non-initial verbs ─────────────────────────────────────────────
    {"q": "there's a bunch of dupes in here, merge them", "hint": "contact", "want": "ASK"},
    {"q": "these two are the same company, merge em", "hint": "account", "want": "ASK"},
    {"q": "I need these contacts merged.", "hint": "contact", "want": "ASK"},
    {"q": "These two accounts should be merged.", "hint": "account", "want": "ASK"},
    {"q": "Could you archive this contact for me?", "hint": "contact", "want": "ASK"},
    {"q": "we archived this by mistake, can you bring it back?", "hint": "contact", "want": "ASK"},

    # ── wrong-object selection ──────────────────────────────────────────────
    # convert belongs to leads; the sentence names its DESTINATIONS.
    {"q": "Convert Mason Reid (c3c5467e-b5a8-4dd0-b83c-6e1ab9f2099a) into an "
          "account and an opportunity", "hint": "lead", "want": ("convert", True)},
    {"q": "turn this lead into a customer", "hint": "lead", "want": "ASK"},

    # ── unsupported must still refuse ───────────────────────────────────────
    {"q": "merge these duplicate orders", "hint": "order", "want": "REFUSE"},
    {"q": "archive these closed-lost deals", "hint": "opportunity", "want": "REFUSE"},

    # ── NEGATION — must never execute ───────────────────────────────────────
    {"q": "Don't merge these contacts.", "hint": "contact", "want": "NONE"},
    {"q": "I don't want these accounts merged.", "hint": "account", "want": "NONE"},
    {"q": "Never archive this contact.", "hint": "contact", "want": "NONE"},
    {"q": "These should not be deleted.", "hint": "contact", "want": "NONE"},
    {"q": "do not restore that one", "hint": "contact", "want": "NONE"},

    # ── INFORMATIONAL — operation MENTIONED, not REQUESTED ──────────────────
    {"q": "Can I merge duplicate contacts?", "hint": "contact", "want": "NONE"},
    {"q": "How does contact merging work?", "hint": "contact", "want": "NONE"},
    {"q": "Why were these contacts archived?", "hint": "contact", "want": "NONE"},
    {"q": "Which contacts were merged?", "hint": "contact", "want": "NONE"},
    {"q": "What happens when I restore a contact?", "hint": "contact", "want": "NONE"},
    {"q": "Tell me about contact archiving.", "hint": "contact", "want": "NONE"},
    {"q": "Can contacts be merged?", "hint": "contact", "want": "NONE"},

    # ── READS — the resolver must decline; modules own these ────────────────
    {"q": "Show me the duplicate contacts.", "hint": "contact", "want": "NONE"},
    {"q": "give me the timeline on this account", "hint": "account", "want": "NONE"},
    {"q": "can you find the duplicate contacts?", "hint": "contact", "want": "NONE"},
    {"q": "List my leads", "hint": "lead", "want": "NONE"},

    # ── AMBIGUOUS — no safe operation; must not guess ───────────────────────
    {"q": "Take care of these duplicates.", "hint": "contact", "want": "NONE"},
    {"q": "Do something with these duplicates.", "hint": "contact", "want": "NONE"},
    {"q": "Fix this account.", "hint": "account", "want": "NONE"},
    {"q": "Handle the archived records.", "hint": "contact", "want": "NONE"},
]
