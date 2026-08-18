"""Golden question set — the KB coverage benchmark.

WHY THIS EXISTS ALONGSIDE eval_suite.py
---------------------------------------
`eval_suite.generate_cases()` derives its cases FROM the articles: it
paraphrases each article's `problem` and checks the article comes back. That
measures retrieval recall over content we already hold, and it is genuinely
useful — but it cannot fail for a question nobody wrote an article about,
because every case it produces has an answer by construction.

This set is authored INDEPENDENTLY of the KB, from what a CRM user would
actually ask. That inversion is the point: it can be wrong in the two ways the
article-derived suite cannot.

    GAP            nothing relevant comes back — we don't know
    FALSE_COVERAGE something confident comes back that does NOT answer it

FALSE_COVERAGE is the dangerous class. A system that answers "How do I merge
duplicate contacts?" with the article on merging duplicate ACCOUNTS has not
failed loudly — it has failed quietly, in a way the user may act on. That is
worse than an honest miss, so the audit scores it separately and never folds it
into a coverage percentage.

THE FIVE LAYERS
---------------
Superficially similar questions should retrieve DIFFERENT articles:

    concept        "What is an account?"
    product        "How do I create an account?"
    procedural     "How do I email a customer after an order ships?"
    troubleshoot   "Why didn't the shipment email get sent?"
    ai             "Why couldn't the AI answer my customer's question?"

Tagging each question by layer lets the audit report where coverage is thin by
KIND, not just by topic — a KB can be rich in product steps and empty of the
troubleshooting questions people actually arrive with.

`must_cover` (optional): substrings a correct answer has to touch. It exists to
stop a judge accepting a fluent answer that omits the decisive fact — the
accepted-vs-sent distinction being the clearest example.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Layers
CONCEPT, PRODUCT, PROCEDURAL, TROUBLESHOOT, AI = (
    "concept", "product", "procedural", "troubleshoot", "ai")


def _q(question: str, category: str, layer: str,
       must_cover: List[str] | None = None) -> Dict[str, Any]:
    return {"question": question, "category": category, "layer": layer,
            "must_cover": must_cover or []}


GOLDEN: List[Dict[str, Any]] = [

    # ── 1. Getting Started ──────────────────────────────────────────────────
    _q("What is Conscestra CRM and what can I use it for?", "getting_started", CONCEPT),
    _q("How do I get started with Conscestra?", "getting_started", PRODUCT),
    _q("How do I set up my company details?", "getting_started", PRODUCT),
    _q("How do I invite team members to the CRM?", "getting_started", PRODUCT),
    _q("How do I configure my user profile?", "getting_started", PRODUCT),
    _q("How do I set user roles and permissions?", "getting_started", PRODUCT),
    _q("How do I import my existing contacts?", "getting_started", PROCEDURAL),
    _q("How do I import accounts or companies?", "getting_started", PROCEDURAL),
    _q("How do I connect my email account to the CRM?", "getting_started", PROCEDURAL),
    _q("How do I connect my calendar?", "getting_started", PROCEDURAL),
    _q("How do I configure notifications?", "getting_started", PRODUCT),
    _q("How do I customize the CRM for my business?", "getting_started", PRODUCT),

    # ── 2. Contacts & People ────────────────────────────────────────────────
    _q("How do I create a contact?", "contacts", PRODUCT),
    _q("How do I edit a contact?", "contacts", PRODUCT),
    _q("How do I delete a contact?", "contacts", PRODUCT),
    _q("How do I search for a contact?", "contacts", PRODUCT),
    _q("How do I find duplicate contacts?", "contacts", PROCEDURAL),
    _q("How do I merge duplicate contacts?", "contacts", PROCEDURAL),
    _q("How do I associate a contact with an account?", "contacts", PROCEDURAL),
    _q("Can one contact belong to more than one account?", "contacts", CONCEPT),
    _q("How do I assign a contact to a salesperson?", "contacts", PRODUCT),
    _q("How do I add notes to a contact?", "contacts", PRODUCT),
    _q("How do I view a contact's activity history?", "contacts", PRODUCT),
    _q("How do I see every email and interaction with a contact?", "contacts", PRODUCT,
       ["One Customer Memory"]),
    _q("How do I add custom fields to a contact?", "contacts", PRODUCT),
    _q("How do I export my contacts?", "contacts", PROCEDURAL),
    _q("Why are duplicate contacts appearing?", "contacts", TROUBLESHOOT),

    # ── 3. Accounts / Companies ─────────────────────────────────────────────
    _q("What is an account in Conscestra?", "accounts", CONCEPT),
    _q("What is the difference between an account and a contact?", "accounts", CONCEPT,
       ["account", "contact"]),
    _q("How do I create an account?", "accounts", PRODUCT),
    _q("How do I edit an account?", "accounts", PRODUCT),
    _q("How do I assign an account owner?", "accounts", PRODUCT),
    _q("How do I link contacts to an account?", "accounts", PROCEDURAL),
    _q("How do I view an account's activity?", "accounts", PRODUCT),
    _q("How do I track opportunities for an account?", "accounts", PROCEDURAL),
    _q("How do I merge duplicate accounts?", "accounts", PROCEDURAL),
    _q("How do I archive an account?", "accounts", PRODUCT),
    _q("Why can't I associate this contact with an account?", "accounts", TROUBLESHOOT),

    # ── 4. Leads ────────────────────────────────────────────────────────────
    _q("What is a lead?", "leads", CONCEPT),
    _q("How do I create a lead?", "leads", PRODUCT),
    _q("How do I qualify a lead?", "leads", PROCEDURAL),
    _q("How do I convert a lead?", "leads", PROCEDURAL),
    _q("What happens when I convert a lead?", "leads", CONCEPT),
    _q("How do I assign leads to a rep?", "leads", PRODUCT),
    _q("How do I change a lead's status?", "leads", PRODUCT),
    _q("How do I track where my leads came from?", "leads", PROCEDURAL),
    _q("How do I prioritize my leads?", "leads", PROCEDURAL),
    _q("How do I find inactive leads?", "leads", PROCEDURAL),
    _q("How do I prevent duplicate leads?", "leads", PROCEDURAL),
    _q("Does a new lead get a welcome email automatically?", "leads", CONCEPT),

    # ── 5. Opportunities / Deals ────────────────────────────────────────────
    _q("What is an opportunity?", "opportunities", CONCEPT),
    _q("How do I create an opportunity?", "opportunities", PRODUCT),
    _q("How do I move an opportunity through the pipeline?", "opportunities", PROCEDURAL),
    _q("What are pipeline stages?", "opportunities", CONCEPT),
    _q("How do I assign an opportunity to someone?", "opportunities", PRODUCT),
    _q("How do I change the expected close date?", "opportunities", PRODUCT),
    _q("How do I record the value of an opportunity?", "opportunities", PRODUCT),
    _q("How do I mark an opportunity as won?", "opportunities", PRODUCT),
    _q("How do I mark an opportunity as lost?", "opportunities", PRODUCT),
    _q("How do I record why we lost a deal?", "opportunities", PROCEDURAL),
    _q("How do I forecast sales?", "opportunities", PROCEDURAL),
    _q("How do I view my pipeline?", "opportunities", PRODUCT),
    _q("Can I create more than one pipeline?", "opportunities", CONCEPT),
    _q("How is win rate calculated?", "opportunities", CONCEPT, ["win rate"]),
    _q("Why does my pipeline total differ from my forecast?", "opportunities", TROUBLESHOOT),

    # ── 6. Activities & Communication ───────────────────────────────────────
    _q("How do I log a call?", "activities", PRODUCT),
    _q("How do I log a meeting?", "activities", PRODUCT),
    _q("How do I create a task?", "activities", PRODUCT),
    _q("How do I assign a task to a colleague?", "activities", PRODUCT),
    _q("How do I schedule a follow-up?", "activities", PROCEDURAL),
    _q("How do I send an email from the CRM?", "activities", PROCEDURAL),
    _q("How do I see a customer's full communication history?", "activities", PRODUCT),
    _q("How do I add a note to a record?", "activities", PRODUCT),
    _q("How do I create a reminder?", "activities", PRODUCT),
    _q("How do I automate follow-up emails?", "activities", PROCEDURAL),
    _q("Why didn't my task get created?", "activities", TROUBLESHOOT),

    # ── 7. Email & Messaging ────────────────────────────────────────────────
    _q("How does email synchronization work?", "email", CONCEPT),
    _q("How do I send an email to a customer?", "email", PROCEDURAL),
    _q("How do I use an email template?", "email", PROCEDURAL),
    _q("How do I create an email template?", "email", PROCEDURAL),
    _q("How do I personalize an email with the customer's details?", "email", PROCEDURAL),
    _q("How do I automatically send an email after an event?", "email", PROCEDURAL),
    _q("How do I know whether an email was accepted by the provider?", "email", CONCEPT,
       ["accepted"]),
    _q("What is the difference between attempted, accepted, sent and delivered?",
       "email", CONCEPT, ["accepted", "delivered"]),
    _q("What happens when an email fails to send?", "email", TROUBLESHOOT),
    _q("How do I handle bounced emails?", "email", TROUBLESHOOT),
    _q("Why was my email not sent?", "email", TROUBLESHOOT),
    _q("Why did a customer get the same email twice?", "email", TROUBLESHOOT),
    _q("Does the customer get an email when their order ships?", "email", CONCEPT),
    _q("Do I need consent before emailing a customer?", "email", CONCEPT, ["consent"]),

    # ── 8. Automation & Workflows ───────────────────────────────────────────
    _q("What is a workflow?", "automation", CONCEPT),
    _q("How do I create a workflow?", "automation", PRODUCT),
    _q("What can trigger a workflow?", "automation", CONCEPT),
    _q("What actions can a workflow perform?", "automation", CONCEPT),
    _q("How do I send an automatic email from a workflow?", "automation", PROCEDURAL),
    _q("How do I create a task automatically?", "automation", PROCEDURAL),
    _q("How do I update a record automatically?", "automation", PROCEDURAL),
    _q("How do I add conditions to a workflow?", "automation", PROCEDURAL),
    _q("How do I test a workflow before turning it on?", "automation", PROCEDURAL),
    _q("How do I turn a workflow on or off?", "automation", PRODUCT),
    _q("How do I see workflow execution history?", "automation", PRODUCT),
    _q("Why didn't my workflow run?", "automation", TROUBLESHOOT),
    _q("Why did an automation run more than once?", "automation", TROUBLESHOOT),
    _q("What is an event in Conscestra?", "automation", CONCEPT),

    # ── 9. AI Assistant / AI Agents ─────────────────────────────────────────
    _q("What can the AI assistant do?", "ai_agents", AI),
    _q("How does the AI assistant access my CRM information?", "ai_agents", AI),
    _q("What information is the AI allowed to use?", "ai_agents", AI),
    _q("How do I ask the AI about a customer?", "ai_agents", AI),
    _q("Can the AI summarize a customer's history?", "ai_agents", AI),
    _q("Can the AI draft an email for me?", "ai_agents", AI),
    _q("Can the AI create a task?", "ai_agents", AI),
    _q("Can the AI update CRM records?", "ai_agents", AI, ["approv"]),
    _q("Which AI actions require my approval?", "ai_agents", AI, ["approv"]),
    _q("How does the AI handle a question it doesn't know?", "ai_agents", AI),
    _q("What happens when the AI cannot answer?", "ai_agents", AI),
    _q("How do I correct a wrong AI answer?", "ai_agents", AI),
    _q("How does the knowledge base affect the AI's answers?", "ai_agents", AI),
    _q("How do I know whether an answer came from the knowledge base?", "ai_agents", AI),
    _q("Can I build my own AI agent without coding?", "ai_agents", AI),
    _q("Why did the AI give me an incorrect answer?", "ai_agents", TROUBLESHOOT),

    # ── 10. Knowledge Base ──────────────────────────────────────────────────
    _q("What is the knowledge base?", "knowledge_base", CONCEPT),
    _q("How do I add a knowledge base article?", "knowledge_base", PRODUCT),
    _q("How do I edit a knowledge base article?", "knowledge_base", PRODUCT),
    _q("What information should go into the knowledge base?", "knowledge_base", CONCEPT),
    _q("How does the AI search the knowledge base?", "knowledge_base", CONCEPT),
    _q("How are relevant articles chosen?", "knowledge_base", CONCEPT),
    _q("What happens when no relevant article exists?", "knowledge_base", CONCEPT),
    _q("How do I report a missing or incorrect answer?", "knowledge_base", PROCEDURAL),
    _q("How do I find gaps in the knowledge base?", "knowledge_base", PROCEDURAL),
    _q("How do I test knowledge base retrieval?", "knowledge_base", PROCEDURAL),
    _q("Who can see internal knowledge base articles?", "knowledge_base", CONCEPT,
       ["internal", "public"]),
    _q("Why didn't the CRM find the right knowledge base article?",
       "knowledge_base", TROUBLESHOOT),

    # ── 11. Reporting & Analytics ───────────────────────────────────────────
    _q("What reports are available?", "reporting", PRODUCT),
    _q("How do I run a report?", "reporting", PRODUCT),
    _q("How do I filter a report?", "reporting", PRODUCT),
    _q("How do I export a report?", "reporting", PROCEDURAL),
    _q("How do I view sales performance?", "reporting", PROCEDURAL),
    _q("How do I view pipeline performance?", "reporting", PROCEDURAL),
    _q("How do I measure conversion rates?", "reporting", PROCEDURAL),
    _q("How do I see my accounts receivable?", "reporting", PROCEDURAL),
    _q("How do I create a dashboard?", "reporting", PRODUCT),
    _q("Why is my dashboard showing different numbers than my report?",
       "reporting", TROUBLESHOOT, ["metric"]),

    # ── 12. Administration & Security ───────────────────────────────────────
    _q("How do I manage users?", "administration", PRODUCT),
    _q("How do permissions work?", "administration", CONCEPT),
    _q("Who can view customer information?", "administration", CONCEPT),
    _q("Who can edit customer information?", "administration", CONCEPT),
    _q("How do I deactivate a user?", "administration", PRODUCT),
    _q("How do I transfer a departing user's accounts?", "administration", PROCEDURAL),
    _q("How is my CRM data protected?", "administration", CONCEPT),
    _q("How is user activity audited?", "administration", CONCEPT, ["audit"]),
    _q("How do I view the audit log?", "administration", PRODUCT),
    _q("How long is my data retained?", "administration", CONCEPT),
    _q("How do I export all of my organization's data?", "administration", PROCEDURAL),
    _q("How do I delete a customer's personal data on request?",
       "administration", PROCEDURAL, ["DSAR"]),

    # ── 13. Troubleshooting ─────────────────────────────────────────────────
    _q("Why can't I log in?", "troubleshooting", TROUBLESHOOT),
    _q("Why can't I see a customer record?", "troubleshooting", TROUBLESHOOT),
    _q("Why can't I edit this record?", "troubleshooting", TROUBLESHOOT),
    _q("Why is the AI assistant not responding?", "troubleshooting", TROUBLESHOOT),
    _q("Why is some of my data missing?", "troubleshooting", TROUBLESHOOT),
    _q("Why can't I import my contacts?", "troubleshooting", TROUBLESHOOT),
    _q("Why didn't the customer receive the order confirmation?",
       "troubleshooting", TROUBLESHOOT),
    _q("Why is an invoice still showing unpaid after payment?",
       "troubleshooting", TROUBLESHOOT),
    _q("Why did my scheduled job not run?", "troubleshooting", TROUBLESHOOT),
]


CATEGORIES = sorted({c["category"] for c in GOLDEN})
LAYERS = (CONCEPT, PRODUCT, PROCEDURAL, TROUBLESHOOT, AI)


def by_category() -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for case in GOLDEN:
        out.setdefault(case["category"], []).append(case)
    return out


def summary() -> Dict[str, Any]:
    cats: Dict[str, int] = {}
    layers: Dict[str, int] = {}
    for c in GOLDEN:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
        layers[c["layer"]] = layers.get(c["layer"], 0) + 1
    return {"total": len(GOLDEN), "by_category": cats, "by_layer": layers}
