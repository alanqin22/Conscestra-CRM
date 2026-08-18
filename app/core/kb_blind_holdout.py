"""Blind holdout — authored by an independent evaluator who did not read the KB.

Every entry was derived from the IMPLEMENTATION only: stored-procedure `p_mode`
dispatch tables read out of `pg_proc.prosrc`, `information_schema` columns, and
Python under app/core/ + app/agents/. No `knowledge_articles` row, no
sql/kb_seed_*.sql / sql/kb_fix_*.sql file, and neither kb_golden_set.py nor
kb_holdout_set.py was opened while writing this.

The verified mode matrix this set is built on (from pg_proc.prosrc):

  sp_contacts      list get_details create update send_verification verify_email
                   duplicates merge activities summary archive restore
  sp_accounts      list get create update timeline financials duplicates merge
                   archive restore summary list_owner
  sp_leads         list get create update qualify disqualify convert pipeline
                   duplicates list_employee archive restore score merge
  sp_opportunities list get create update delete add_product update_product
                   remove_product pipeline win_rate forecast_accuracy forecast
                   search_accounts search_products search_opportunities get_owners
  sp_orders        account_search list_employees contact_search get_pricing list
                   get_detail create update delete account_summary
                   category_summary sales_summary get_category get_product
  sp_activities    log_call log_email schedule_meeting create_task add_note
                   complete reopen list get create update delete timeline
                   overdue upcoming summary get_owners
  sp_accounting    list_employee generate_invoice record_payment void_invoice
                   list_invoices list_invoices_for_account get_invoice_360
                   list_payments get_payment_360 account_balance
                   account_balance_lookup account_search get_invoiceable_orders
                   accounting_summary
  sp_cases         list get create update assign escalate add_comment resolve
                   close reopen timeline queue sla_report summary
  sp_products      list get_details add update bulk_adjust_stock
                   inventory_summary low_stock price_history price_matrix
                   product_search list_categories

  merge / duplicates / archive / restore  -> contacts, accounts, leads ONLY
  delete                                  -> opportunities, orders, activities ONLY
  qualify / disqualify / convert / score  -> leads ONLY
  escalate / sla_report / resolve / close -> cases ONLY
  void_invoice / generate_invoice         -> accounting (invoices) ONLY
  price_history / low_stock / stock       -> products ONLY
  send_verification / verify_email        -> contacts ONLY
  forecast / win_rate                     -> opportunities ONLY
"""
from typing import Any, Dict, List

BLIND: List[Dict[str, Any]] = [
    # ================================================================
    # ANSWERABLE (20) — the product genuinely does this
    # ================================================================
    {
        "question": "We ended up with the same person entered twice under one "
                    "account. Is there a way to squash them together into one "
                    "contact record?",
        "kind": "answerable",
        "expected": "Yes — contacts can be merged. The merge groups by email or "
                    "by phone, keeps the OLDEST matching contact as the master, "
                    "and soft-deletes the losing rows (they are marked as merged "
                    "duplicates rather than erased).",
        "evidence": "sp_contacts p_mode='merge'; p_operation must be 'by_email' "
                    "or 'by_phone'; master picked ORDER BY created_at ASC LIMIT 1; "
                    "losers get is_deleted=TRUE, status='duplicate_merged'",
    },
    {
        "question": "Before I clean anything up I'd like a list of which contacts "
                    "actually look like duplicates. Can the system find them for me?",
        "kind": "answerable",
        "expected": "Yes — there is a duplicate-detection report for contacts that "
                    "groups active (non-deleted) contacts by shared email and by "
                    "shared phone and returns each group with its members. It only "
                    "REPORTS; it does not merge anything.",
        "evidence": "sp_contacts p_mode='duplicates' — dup_emails / dup_phones CTEs "
                    "over active_contacts, HAVING COUNT(*) > 1, returns matches array",
    },
    {
        "question": "A customer went dormant and I don't want them cluttering my "
                    "account list, but I might need them back next year. Options?",
        "kind": "answerable",
        "expected": "Archive the account. Archiving is a soft delete (the row stays, "
                    "flagged as deleted) and there is a matching restore that brings "
                    "it back, so nothing is lost.",
        "evidence": "sp_accounts p_mode='archive' and p_mode='restore'; "
                    "accounts.is_deleted column",
    },
    {
        "question": "What happens to a lead we decide isn't a fit — can I mark it "
                    "dead and say why?",
        "kind": "answerable",
        "expected": "Yes — leads can be disqualified, and a reason is stored on the "
                    "lead alongside the status change.",
        "evidence": "sp_leads p_mode='disqualify'; leads.disqualification_reason "
                    "column exists (leads also have qualification_reason / "
                    "qualification_date for the qualify path)",
    },
    {
        "question": "When I convert a lead, what records do I actually end up with?",
        "kind": "answerable",
        "expected": "Three: an Account, a Contact, and an Opportunity (stage "
                    "'prospecting', status 'open'). The lead itself is flagged "
                    "converted and stores pointers back to all three. A lead still "
                    "in 'new' or 'working' is auto-qualified as part of the "
                    "conversion.",
        "evidence": "sp_leads p_mode='convert' — INSERT INTO accounts, contacts and "
                    "opportunities; sets leads.converted / converted_at / "
                    "converted_account_id / converted_contact_id / "
                    "converted_opportunity_id, status='converted'",
    },
    {
        "question": "Do leads get a numeric score, and where does the rating "
                    "(hot/warm/cold) come from?",
        "kind": "answerable",
        "expected": "Yes — scoring a lead computes a score out of 100 plus a rating, "
                    "writes both back onto the lead with a timestamp, and records the "
                    "scoring detail in the audit log. It is an explicit action on a "
                    "specific lead.",
        "evidence": "sp_leads p_mode='score' calls fn_score_lead(p_lead_id) and "
                    "UPDATEs leads.score, leads.rating, leads.score_updated_at; "
                    "audit_log row with action='score'",
    },
    {
        "question": "I need the pipeline broken out by stage for the forecast "
                    "meeting. Can the assistant produce that?",
        "kind": "answerable",
        "expected": "Yes — opportunities support a pipeline-by-stage view, plus "
                    "forecast and forecast-accuracy reporting.",
        "evidence": "sp_opportunities p_mode in ('pipeline','forecast',"
                    "'forecast_accuracy'); opportunities.stage values observed: "
                    "prospecting, qualification, proposal, negotiation, closed_won, "
                    "closed_paid, closed_lost",
    },
    {
        "question": "how do i see our win rate",
        "kind": "answerable",
        "expected": "Opportunities expose a win-rate calculation as a first-class "
                    "report mode.",
        "evidence": "sp_opportunities p_mode='win_rate'",
    },
    {
        "question": "Can I attach specific products with quantities to an "
                    "opportunity, and change them later if the deal shape moves?",
        "kind": "answerable",
        "expected": "Yes — opportunities have line-item product management: add a "
                    "product, update an existing line, and remove a line.",
        "evidence": "sp_opportunities p_mode in ('add_product','update_product',"
                    "'remove_product'); opportunity_products / opportunity_lines "
                    "tables",
    },
    {
        "question": "My manager wants monthly sales numbers and a breakdown by "
                    "product category. Is that built in or do I export to Excel?",
        "kind": "answerable",
        "expected": "Built in — orders provide a sales summary and a per-category "
                    "summary (and a per-account summary) without exporting.",
        "evidence": "sp_orders p_mode in ('sales_summary','category_summary',"
                    "'account_summary')",
    },
    {
        "question": "We shipped three orders to the same customer this month. Can I "
                    "bill them all on one invoice?",
        "kind": "answerable",
        "expected": "Yes — invoice generation takes an account plus a LIST of order "
                    "ids, so several orders can go onto one invoice. The account must "
                    "be active, and orders already linked to a non-cancelled invoice "
                    "are rejected. There is also a lookup that returns which of an "
                    "account's orders are still invoiceable.",
        "evidence": "sp_accounting p_mode='generate_invoice' requires p_account_id "
                    "plus p_order_ids array, validates accounts.status='active' and "
                    "checks invoice_orders for already-invoiced rows; "
                    "p_mode='get_invoiceable_orders' excludes status in "
                    "(invoiced, cancelled, draft) and orders already on a "
                    "non-cancelled invoice",
    },
    {
        "question": "Customer paid half the invoice. Can I log that without marking "
                    "the whole thing paid?",
        "kind": "answerable",
        "expected": "Yes — payments are recorded against an invoice with an amount, "
                    "the invoice's balance due is reduced, and the invoice can sit in "
                    "a partially-paid state rather than jumping straight to paid.",
        "evidence": "sp_accounting p_mode='record_payment' (amount must be > 0, "
                    "reads invoices.balance_due FOR UPDATE); invoices.status values "
                    "observed include 'partial'; payments table",
    },
    {
        "question": "We issued an invoice by mistake. How do I kill it?",
        "kind": "answerable",
        "expected": "Void (cancel) the invoice. Voiding is the supported way to "
                    "retire a wrongly-issued invoice; it is refused if the invoice is "
                    "already cancelled, and refused if confirmed/completed payments "
                    "exist against it.",
        "evidence": "sp_accounting p_mode='void_invoice' — errors -32 on already "
                    "cancelled, and counts payments with status IN "
                    "('confirmed','completed') before proceeding",
    },
    {
        "question": "This support ticket is going nowhere and the customer is "
                    "getting angry. What does escalating actually do to it?",
        "kind": "answerable",
        "expected": "Escalating a case bumps its priority one level (low→medium→"
                    "high→urgent, capping at urgent), moves a case that was still "
                    "'new' into in_progress, and drops an internal comment recording "
                    "the escalation.",
        "evidence": "sp_cases p_mode='escalate' — CASE priority WHEN 'low' THEN "
                    "'medium' ... ELSE 'urgent', status='new' -> 'in_progress', "
                    "INSERT INTO case_comments with is_internal",
    },
    {
        "question": "Is there any reporting on whether we're hitting our response "
                    "targets on support tickets?",
        "kind": "answerable",
        "expected": "Yes — cases have an SLA report, plus a queue view and a case "
                    "timeline. Cases also track first_response_at and resolved_at.",
        "evidence": "sp_cases p_mode in ('sla_report','queue','timeline','summary'); "
                    "cases.first_response_at, cases.resolved_at, cases.closed_at "
                    "columns",
    },
    {
        "question": "Which products are about to run out, and can I bump stock on a "
                    "whole category at once after a delivery lands?",
        "kind": "answerable",
        "expected": "Both are supported — a low-stock report lists products needing "
                    "reorder, and a bulk stock adjustment applies a non-zero delta, "
                    "optionally filtered to one category.",
        "evidence": "sp_products p_mode='low_stock'; p_mode='bulk_adjust_stock' "
                    "(resolves p_category_number/p_category_filter, rejects a NULL "
                    "or 0 p_stock_adjustment); products.stock_quantity column",
    },
    {
        "question": "Customer is arguing we quoted them less last quarter. Can I pull "
                    "up what that item used to cost?",
        "kind": "answerable",
        "expected": "Yes — products have a price history view (and a price matrix "
                    "across price types/tiers).",
        "evidence": "sp_products p_mode in ('price_history','price_matrix'); "
                    "product_pricing + product_pricing_backup tables",
    },
    {
        "question": "I just got off a 20-minute call with a prospect. What's the "
                    "fastest way to get that into the CRM, and can I set the "
                    "follow-up at the same time?",
        "kind": "answerable",
        "expected": "Log the call as an activity — there are dedicated shortcuts for "
                    "logging a call, logging an email, scheduling a meeting, creating "
                    "a task and adding a note. Activities can then be completed (or "
                    "reopened), and there are overdue / upcoming views for the "
                    "follow-ups.",
        "evidence": "sp_activities p_mode in ('log_call','log_email',"
                    "'schedule_meeting','create_task','add_note','complete',"
                    "'reopen','overdue','upcoming'); activities.type values "
                    "observed: call, email, meeting, note, sms, system, task, voip",
    },
    {
        "question": "How do we confirm a contact's email address is real before we "
                    "start emailing them?",
        "kind": "answerable",
        "expected": "Contacts have an email verification flow: send a verification "
                    "message, then verify with the returned token; the contact "
                    "carries a verified flag with an expiring token. Verified status "
                    "is what gates real outbound to that address.",
        "evidence": "sp_contacts p_mode in ('send_verification','verify_email'); "
                    "contacts.is_email_verified, email_verification_token, "
                    "email_verification_token_expires_at columns",
    },
    {
        "question": "We track an 'RCM level' on students that your standard fields "
                    "don't cover. Do I need a developer to add it?",
        "kind": "answerable",
        "expected": "No developer needed — an admin can declare custom fields on the "
                    "core entities without a schema change. They are typed/validated, "
                    "editable from the UI, and are visible to the AI agents and to "
                    "analytics, not just decorative metadata.",
        "evidence": "app/core/custom_fields.py module docstring + custom_field_defs "
                    "(entity, field_key, field_type, options, required) and "
                    "custom_field_values tables; values injected into "
                    "context.hydrate() and entity ai_summary",
    },

    # ================================================================
    # UNANSWERABLE (10) — the product does not do this; refuse
    # ================================================================
    {
        "question": "How do I set up sales territories so that leads in Ontario "
                    "route to the East team and BC goes to the West team?",
        "kind": "unanswerable",
        "expected": "REFUSE: there is no territory management feature. Nothing in the "
                    "product models territories or territory-based routing. Do not "
                    "improvise an answer out of owner assignment or the case routing "
                    "rules — those are not territories.",
        "evidence": "No territory table (information_schema query for table_name "
                    "ILIKE '%territory%' returned none) and no territory module; the "
                    "only ordered-rule router is app/core/routing.py, which is CASE "
                    "routing on priority/origin/subject/account_value and explicitly "
                    "'Recommends; never assigns'",
    },
    {
        "question": "Where do I configure commission plans so reps get 4% on new "
                    "business and 2% on renewals?",
        "kind": "unanswerable",
        "expected": "REFUSE: commission calculation / compensation plans are not a "
                    "feature of this product. There is no place to configure rates or "
                    "produce commission statements.",
        "evidence": "No commission table (information_schema ILIKE '%commission%' "
                    "returned none) and no commission module under app/core/; the "
                    "word appears only as domain vocabulary in "
                    "app/agents/orchestrator/executive.py",
    },
    {
        "question": "Can my team log billable hours against a customer so we can "
                    "invoice the time at month end?",
        "kind": "unanswerable",
        "expected": "REFUSE: there is no time tracking or timesheet capability. "
                    "Activities record that work happened, but they do not capture "
                    "billable duration, rates, or feed hours into invoicing. Invoices "
                    "are generated from ORDERS.",
        "evidence": "grep for 'timesheet' across app/ returns nothing; activities has "
                    "start_at/end_at but no rate/billable/duration-billing column; "
                    "sp_accounting generate_invoice requires p_order_ids",
    },
    {
        "question": "Does the quote go out with an e-signature block so the customer "
                    "can sign it electronically? We use DocuSign today.",
        "kind": "unanswerable",
        "expected": "REFUSE: there is no e-signature capability and no DocuSign (or "
                    "equivalent) integration. Quotes are informational priced offers "
                    "delivered by email or drafted as an owner task; they are not "
                    "signable contracts.",
        "evidence": "grep for 'esignature'/'docusign' across app/ returns nothing; "
                    "app/core/quotes.py docstring: 'Quotes are informational offers, "
                    "not contracts'",
    },
    {
        "question": "We run two warehouses. How do I see stock on hand per location "
                    "and transfer units between them?",
        "kind": "unanswerable",
        "expected": "REFUSE: inventory is tracked as a single stock number per "
                    "product. There is no multi-location / multi-warehouse inventory "
                    "and no stock transfer between locations.",
        "evidence": "products.stock_quantity is one scalar column; no warehouse or "
                    "location table (information_schema ILIKE '%warehouse%' returned "
                    "none); sp_products bulk_adjust_stock filters by category, not "
                    "location",
    },
    {
        "question": "I need to raise a purchase order to our supplier and track what "
                    "we owe them. Where's the vendor module?",
        "kind": "unanswerable",
        "expected": "REFUSE: there is no purchasing/vendor side. The product manages "
                    "customer-facing accounts, orders, invoices and payments "
                    "(receivables) — there are no supplier records or purchase "
                    "orders.",
        "evidence": "No vendor or supplier table (information_schema ILIKE "
                    "'%vendor%' / '%supplier%' returned none); sp_accounting modes "
                    "are all AR-side: generate_invoice, record_payment, "
                    "account_balance",
    },
    {
        "question": "Do you support serial number and lot tracking so I can trace "
                    "which unit went to which customer for a recall?",
        "kind": "unanswerable",
        "expected": "REFUSE: products are tracked by SKU and a stock count only. "
                    "There is no serial-number or lot/batch tracking, so a unit-level "
                    "trace to a customer is not possible.",
        "evidence": "products columns are product_id, product_number, product_name, "
                    "sku, description, currency_code, is_active, stock_quantity, "
                    "category_id ... — no serial/lot/batch column, and no serials "
                    "table",
    },
    {
        "question": "Set up a monthly recurring invoice for our maintenance plan so "
                    "it bills automatically on the 1st.",
        "kind": "unanswerable",
        "expected": "REFUSE: there is no recurring/subscription billing. Every "
                    "invoice is generated explicitly from one or more orders; there "
                    "is no billing schedule, no auto-renewal, and no recurring "
                    "invoice template.",
        "evidence": "sp_accounting generate_invoice is order-driven and requires "
                    "explicit p_order_ids; invoices.invoice_type holds only "
                    "'invoice'; no recurring/schedule/subscription table in the "
                    "158-table public schema",
    },
    {
        "question": "Can you connect to our QuickBooks and keep invoices and "
                    "payments in sync both ways?",
        "kind": "unanswerable",
        "expected": "REFUSE the two-way sync. What exists is a ONE-WAY export: "
                    "QuickBooks/Xero-importable CSV of invoices and payments over a "
                    "date range. There is no OAuth connection and nothing writes back "
                    "into the CRM from the accounting package.",
        "evidence": "app/core/integrations.py — GET /erp/export/invoices.csv and "
                    "/erp/export/payments.csv only; the docstring says the "
                    "external_id/external_source columns mean 'a FUTURE two-way OAuth "
                    "sync can build on this bridge'",
    },
    {
        "question": "Where do I store the signed MSA and get reminded 60 days before "
                    "it comes up for renewal?",
        "kind": "unanswerable",
        "expected": "REFUSE: there is no contract object and no contract lifecycle / "
                    "renewal-reminder feature. Do not substitute quotes (which are "
                    "explicitly not contracts) or opportunity close dates.",
        "evidence": "No contracts table (information_schema ILIKE '%contract%' "
                    "returned none); quotes carry only valid_until (30-day offer "
                    "validity per app/core/quotes.py QUOTE_VALID_DAYS), not a "
                    "contract term",
    },

    # ================================================================
    # FALSE_PREMISE (10) — a real capability, wrongly assumed to be
    # automatic / already-executed
    # ================================================================
    {
        "question": "The nightly dedupe merged two contacts I actually needed kept "
                    "separate. How do I turn the automatic merging off?",
        "kind": "false_premise",
        "expected": "REFUSE the premise: nothing merges contacts automatically, so "
                    "there is no auto-merge to turn off. Duplicate DETECTION is a "
                    "report you run; merging is a separate explicit action, and the "
                    "agent-initiated version is queued as a governed proposal that a "
                    "human approves. Correct the premise, then explain how to inspect "
                    "or undo an approved merge.",
        "evidence": "sp_contacts 'duplicates' only SELECTs; 'merge' is a distinct "
                    "explicit mode; the A2A capability data.merge_contacts is a "
                    "'write' registered in app/core/a2a.py CAPABILITIES described as "
                    "'proposed nightly by the data-quality agent, executes on "
                    "approval' — the nightly dq_propose job queues, it does not merge",
    },
    {
        "question": "Show me the list of phone numbers the data-quality job "
                    "reformatted to E.164 last night.",
        "kind": "false_premise",
        "expected": "REFUSE the premise: the nightly data-quality pass SCANS and "
                    "PROPOSES; it does not rewrite phone numbers. Nothing was "
                    "reformatted unless someone approved the proposal. Point the user "
                    "at the pending approval queue instead.",
        "evidence": "app/main.py schedules _run_dq_propose at 23:20 ET with the "
                    "comment 'fixes queue for approval, nothing mutates directly'; "
                    "a2a capability data.normalize_phones — 'proposed nightly by the "
                    "data-quality agent, executes on approval'",
    },
    {
        "question": "Since leads convert automatically once they cross a score of 80, "
                    "how do I raise that threshold to 90?",
        "kind": "false_premise",
        "expected": "REFUSE the premise: scoring and conversion are unrelated "
                    "actions. A score never triggers a conversion — there is no "
                    "auto-convert threshold to raise. Conversion is an explicit "
                    "action on a specific lead. (A high score can raise a hot-lead "
                    "signal for outreach, which is not conversion.)",
        "evidence": "sp_leads 'score' only UPDATEs leads.score/rating/"
                    "score_updated_at; sp_leads 'convert' is a separate mode "
                    "requiring p_lead_id and creating account+contact+opportunity; "
                    "the nightly job at 22:30 ET is _run_emit_hot_lead_events, which "
                    "emits lead.scored events, not conversions",
    },
    {
        "question": "The system emailed all our overdue customers this morning — can "
                    "I get the list of who received a reminder?",
        "kind": "false_premise",
        "expected": "REFUSE the premise as stated: overdue-invoice reminders are not "
                    "an automatic morning broadcast. The dunning sweep is a nightly "
                    "event emission that is off unless the agent bus is enabled, and "
                    "even then a reminder is only actually transmitted when autosend "
                    "is on and the recipient address is real and verified — otherwise "
                    "it is composed and drafted, not sent. Offer to check what was "
                    "actually drafted vs sent rather than confirming a send.",
        "evidence": "app/main.py _run_emit_overdue_invoice_events at 22:25 ET, "
                    "self-gated ('No-op unless AGENT_BUS_ENABLED=1'); "
                    "app/core/agent_bus.py AUTOSEND = _flag('AGENT_BUS_AUTOSEND') "
                    "default 0, 'Real SMTP only when AGENT_BUS_AUTOSEND=1' and the "
                    "recipient must pass _is_real_email()",
    },
    {
        "question": "Now that the weekly retraining swapped in the new lead scoring "
                    "model, can I see how the old one compared?",
        "kind": "false_premise",
        "expected": "REFUSE the premise: the weekly training run does NOT activate "
                    "anything. It trains a candidate and proposes activation with "
                    "holdout evidence; the new model only becomes active when a human "
                    "approves that proposal. Nothing was swapped in on its own.",
        "evidence": "app/main.py _run_scoring_train Monday 23:30 ET — 'trains a "
                    "candidate and proposes activation through governance, never "
                    "activates'; a2a capability scoring.activate is a write that "
                    "'executes on governance approval, undo restores the previous "
                    "version'; lead_scoring_model table",
    },
    {
        "question": "When a case blows its SLA the system reassigns it to whoever's "
                    "free — where's the log of those automatic reassignments?",
        "kind": "false_premise",
        "expected": "REFUSE the premise: work routing recommends an owner and stops "
                    "there. It never assigns, so there are no automatic "
                    "reassignments to log. Assignment stays an explicit human act "
                    "(and is recorded in field history when it happens).",
        "evidence": "app/core/routing.py docstring: 'Work routing — Recommends; "
                    "never assigns. ... The assignment itself stays ... reached by an "
                    "explicit human act'; sp_cases 'assign' is an explicit mode",
    },
    {
        "question": "I saw the assistant published a help article from that printer "
                    "jam ticket last week. How do I stop it publishing without me?",
        "kind": "false_premise",
        "expected": "REFUSE the premise: the knowledge miner drafts and PROPOSES "
                    "articles; it publishes nothing without an approval, so no "
                    "article went live unattended. There is nothing to switch off on "
                    "that account — explain that publication already requires a human "
                    "approval, and where that queue is.",
        "evidence": "app/main.py _run_kb_draft_pass 23:00 ET — 'Self-gates on "
                    "KB_DRAFT_ENABLED; publishes nothing without an approval'; a2a "
                    "capability kb.publish 'executes on governance approval; undo "
                    "retires the article'",
    },
    {
        "question": "The pipeline hygiene run closed a bunch of my stale "
                    "opportunities overnight. Can I bulk-reopen them?",
        "kind": "false_premise",
        "expected": "REFUSE the premise: the pipeline hygiene pass is dry-run by "
                    "default — it reports what it WOULD do and changes nothing unless "
                    "the apply flag is explicitly enabled (and the feature itself is "
                    "off by default). Nothing was closed overnight, so there is "
                    "nothing to reopen. Offer to show the hygiene findings instead.",
        "evidence": "app/main.py _run_pipeline_hygiene 21:50 ET — 'Self-gates on "
                    "PIPELINE_HYGIENE_ENABLED; dry-run unless "
                    "PIPELINE_HYGIENE_APPLY=1'; app/core/pipeline_hygiene.py "
                    "docstring 'Safe by default'",
    },
    {
        "question": "Great news, the AI booked the demo with the lead who replied "
                    "yesterday. What time did it put on the calendar?",
        "kind": "false_premise",
        "expected": "REFUSE the premise: an automated cadence step does not book "
                    "meetings by itself. Sequence steps only create internal CRM "
                    "records — the 'email' step is a DRAFT task for the owner, with "
                    "no outbound send. Real meeting booking exists as a separate "
                    "governed write capability that has to be invoked and approved, "
                    "and invite emails go out only under autosend to a verified "
                    "address.",
        "evidence": "app/core/sequences.py SAFETY: 'Steps are internal CRM records "
                    "only (activities, blackboard notes) — no SMTP; the step-1 "
                    "\"email\" is a DRAFT task for the owner'; a2a capability "
                    "meeting.book — 'invite emails only under AUTOSEND to verified "
                    "addresses'",
    },
    {
        "question": "Which churn-risk accounts got the win-back campaign the "
                    "supervisor launched this week?",
        "kind": "false_premise",
        "expected": "REFUSE the premise: the supervisor can PROPOSE a win-back "
                    "campaign on a churn spike, but it executes only on governance "
                    "approval, and even then the content is draft-only unless autosend "
                    "is on and the launch is explicitly confirmed. No campaign went "
                    "out on its own. Offer to show the proposal/approval status and "
                    "the default segment (churn_band=high).",
        "evidence": "a2a capability campaign.winback — 'CASL-gated, drafts unless "
                    "AUTOSEND. Proposed by the supervisor on churn spikes; executes "
                    "on governance approval'; app/core/marketing.py 'DRAFT-ONLY "
                    "unless AGENT_BUS_AUTOSEND=1 AND the launch passes confirm=true'",
    },

    # ================================================================
    # OBJECT_BOUNDARY (10) — capability exists, but not on THIS object
    # ================================================================
    {
        "question": "Two of our reps keyed in the same purchase, so we've got "
                    "duplicate orders. How do I merge them into one?",
        "kind": "object_boundary",
        "expected": "REFUSE: orders cannot be merged. Merge exists for contacts, "
                    "accounts and leads only. Do NOT answer about merging contacts or "
                    "accounts instead. For a duplicate order the available action is "
                    "to cancel/delete the extra one.",
        "evidence": "sp_orders modes contain no 'merge' or 'duplicates' (only "
                    "account_search, list_employees, contact_search, get_pricing, "
                    "list, get_detail, create, update, delete, account_summary, "
                    "category_summary, sales_summary, get_category, get_product); "
                    "'merge' exists on sp_contacts, sp_accounts, sp_leads",
    },
    {
        "question": "Can I archive an old opportunity and pull it back out of the "
                    "archive if it revives next quarter?",
        "kind": "object_boundary",
        "expected": "REFUSE: opportunities have no archive/restore pair. Archive and "
                    "restore exist on contacts, accounts and leads. An opportunity "
                    "can be deleted (which marks its status as deleted) or closed via "
                    "its stage, but there is no archive-and-restore round trip. Do "
                    "not answer about archiving an account instead.",
        "evidence": "sp_opportunities modes have 'delete' but no 'archive'/'restore'; "
                    "the delete branch does UPDATE opportunities SET status='deleted'; "
                    "archive/restore appear in sp_contacts, sp_accounts, sp_leads only",
    },
    {
        "question": "Run the duplicate finder over our invoices — I'm sure we've "
                    "double-billed someone.",
        "kind": "object_boundary",
        "expected": "REFUSE: there is no duplicate-detection report for invoices. "
                    "Duplicate detection is available for contacts, accounts and "
                    "leads only. Don't substitute the contact duplicate report. The "
                    "closest real safeguard is that invoice generation rejects orders "
                    "already attached to a non-cancelled invoice.",
        "evidence": "sp_accounting modes contain no 'duplicates'; 'duplicates' "
                    "exists on sp_contacts, sp_accounts, sp_leads; generate_invoice "
                    "counts already-invoiced orders via invoice_orders",
    },
    {
        "question": "This deal is dead in the water — how do I disqualify the "
                    "opportunity and log the reason?",
        "kind": "object_boundary",
        "expected": "REFUSE the disqualify framing: disqualify (with a stored reason) "
                    "is a LEAD action, not an opportunity action. Opportunities are "
                    "not disqualified; they move to a closed stage / are updated. Do "
                    "not silently answer about disqualifying a lead.",
        "evidence": "sp_leads has 'qualify' and 'disqualify' plus "
                    "leads.disqualification_reason; sp_opportunities has neither — "
                    "its modes are list/get/create/update/delete/add_product/"
                    "update_product/remove_product/pipeline/win_rate/forecast*/"
                    "search_*/get_owners; opportunities has no disqualification "
                    "column",
    },
    {
        "question": "One of our contacts just started their own company. Can I "
                    "convert that contact into an account with an opportunity, the "
                    "way conversion works?",
        "kind": "object_boundary",
        "expected": "REFUSE: conversion is a LEAD-only operation. Contacts cannot be "
                    "converted. The account+contact+opportunity conversion path only "
                    "starts from a lead — the user would have to create the new "
                    "records (or a new lead) directly. Do not describe lead "
                    "conversion as though it applied to the contact.",
        "evidence": "sp_leads p_mode='convert'; sp_contacts modes contain no "
                    "'convert' (list, get_details, create, update, "
                    "send_verification, verify_email, duplicates, merge, activities, "
                    "summary, archive, restore)",
    },
    {
        "question": "Can you score our accounts out of 100 and rate them hot/warm/"
                    "cold like you do elsewhere?",
        "kind": "object_boundary",
        "expected": "REFUSE: the 0-100 score plus hot/warm/cold rating is a LEAD "
                    "feature. Accounts have no score or rating field and no scoring "
                    "mode. Accounts do have summary/financials/timeline views, but "
                    "the assistant must not present those as an account score.",
        "evidence": "sp_leads p_mode='score' -> fn_score_lead, leads.score / "
                    "leads.rating / leads.score_updated_at columns; accounts has no "
                    "score or rating column and sp_accounts has no 'score' mode",
    },
    {
        "question": "This overdue invoice needs to be escalated to a manager. How do "
                    "I escalate it?",
        "kind": "object_boundary",
        "expected": "REFUSE the escalate framing: escalate is a CASE operation "
                    "(priority bump + internal comment). Invoices have no escalation "
                    "path. The invoice-side actions are void, record a payment, or "
                    "the overdue/dunning reporting. Do not answer by describing case "
                    "escalation as if it applied to the invoice.",
        "evidence": "sp_cases p_mode='escalate' (+ escalations table); sp_accounting "
                    "has no 'escalate' mode — its modes are generate_invoice, "
                    "record_payment, void_invoice, list_invoices, get_invoice_360, "
                    "list_payments, get_payment_360, account_balance*, "
                    "account_search, get_invoiceable_orders, accounting_summary, "
                    "list_employee",
    },
    {
        "question": "Customer changed their mind after we keyed the order. Can I void "
                    "the order?",
        "kind": "object_boundary",
        "expected": "REFUSE the 'void' framing for orders: voiding is an INVOICE "
                    "operation. Orders are cancelled by updating their status, or "
                    "deleted. The assistant should not describe void_invoice's rules "
                    "(e.g. blocked by confirmed payments) as though they applied to "
                    "the order.",
        "evidence": "sp_accounting p_mode='void_invoice'; sp_orders has no 'void' "
                    "mode — it has update and delete, and orders.status values "
                    "observed include 'cancelled'",
    },
    {
        "question": "Pull the price history for order #10442 so I can see how the "
                    "pricing on it changed over time.",
        "kind": "object_boundary",
        "expected": "REFUSE: price history is a PRODUCT-level report, not an "
                    "order-level one. An order's line prices are what was captured at "
                    "order time; there is no historical price trail on an order. "
                    "Offer product price history for the items instead, but do not "
                    "present it as the order's history.",
        "evidence": "sp_products p_mode in ('price_history','price_matrix') over "
                    "product_pricing; sp_orders has get_pricing (current pricing for "
                    "building an order) but no price_history mode",
    },
    {
        "question": "Send an email verification link to this lead so we know their "
                    "address is good before we start the sequence.",
        "kind": "object_boundary",
        "expected": "REFUSE: email verification (send link, verify token, verified "
                    "flag) exists for CONTACTS only. Leads have no verification flow "
                    "or verified-email flag. Do not redirect to verifying a contact "
                    "as if it were the same record — the lead would have to be "
                    "converted first.",
        "evidence": "sp_contacts p_mode in ('send_verification','verify_email') and "
                    "contacts.is_email_verified / email_verification_token / "
                    "email_verification_token_expires_at; the leads table has no "
                    "is_email_verified column and sp_leads has no verification mode",
    },

    # ================================================================
    # NEAR_NEIGHBOUR (5) — one word away from another question,
    # genuinely different answer
    # ================================================================
    {
        "question": "If I delete an order, is the record actually gone — same as "
                    "deleting an opportunity?",
        "kind": "near_neighbour",
        "expected": "No, they behave differently, and the assistant must not treat "
                    "them as the same. Deleting an OPPORTUNITY is a soft delete: the "
                    "row stays and its status becomes 'deleted'. Deleting an ORDER "
                    "routes into a hard-delete path (and orders also carry a "
                    "deleted_at soft-delete column used elsewhere). Near neighbour of "
                    "'can I delete an opportunity?' — same verb, different outcome.",
        "evidence": "sp_opportunities 'delete' -> UPDATE opportunities SET "
                    "status='deleted'; sp_orders 'delete' -> RETURN sp_orders("
                    "p_mode:='update', p_action:='hard_delete', ..., "
                    "p_force_hard_delete:=p_force_hard_delete); orders.deleted_at "
                    "column",
    },
    {
        "question": "Can I void an invoice the customer has already part-paid?",
        "kind": "near_neighbour",
        "expected": "No — voiding is refused once confirmed or completed payments "
                    "exist against the invoice. This is the opposite answer to the "
                    "near-identical 'can I void an issued invoice with no payments on "
                    "it?', which is yes. (An already-cancelled invoice is also "
                    "refused.)",
        "evidence": "sp_accounting 'void_invoice' — SELECT COUNT(*) ... FROM "
                    "payments WHERE invoice_id = p_invoice_id AND status IN "
                    "('confirmed','completed') AND is_deleted=false, then blocks; "
                    "error -32 when status is already 'cancelled'",
    },
    {
        "question": "When we merge duplicate contacts, is that permanent or can we "
                    "get the other record back?",
        "kind": "near_neighbour",
        "expected": "Recoverable — a merge SOFT-deletes the losing contacts (flagged "
                    "as merged duplicates), and the agent-run merge records every "
                    "move and is undoable. This is deliberately the opposite of the "
                    "near-identical question about honouring an erasure/right-to-be-"
                    "forgotten request, which is IRREVERSIBLE with no undo. The "
                    "assistant must not blur the two.",
        "evidence": "sp_contacts 'merge' sets is_deleted=TRUE, "
                    "status='duplicate_merged' on the losers; a2a data.merge_contacts "
                    "'every move recorded, undoable'; a2a data.erase_record — "
                    "'IRREVERSIBLE — there is no undo'",
    },
    {
        "question": "When I convert a lead for a company we already sell to, does it "
                    "hang the new contact off the existing account?",
        "kind": "near_neighbour",
        "expected": "No — conversion ALWAYS inserts a brand-new account (falling back "
                    "to the person's name when the company field is blank). It does "
                    "not match or attach to an existing account, so converting a lead "
                    "for a current customer creates a duplicate account you then have "
                    "to merge. This differs from the near-identical 'does converting "
                    "a lead create an account?' — yes, but a NEW one, every time.",
        "evidence": "sp_leads 'convert' — unconditional INSERT INTO accounts (... "
                    "gen_random_uuid(), COALESCE(NULLIF(TRIM(company),''), "
                    "first_name||' '||last_name, 'Unknown Account') ...) RETURNING "
                    "account_id; there is no lookup of an existing account before the "
                    "insert",
    },
    {
        "question": "When a duplicate record is physically merged into its primary, "
                    "does the duplicate's login come across too?",
        "kind": "near_neighbour",
        "expected": "No — logins/credentials and derived intelligence are explicitly "
                    "NOT moved by the identity merge; only business and history rows "
                    "are re-pointed to the primary before the duplicate is "
                    "soft-deleted. Near neighbour of 'does the merge move the "
                    "duplicate's activity history?' — which is yes. Worth adding that "
                    "the physical merge is optional because reads already resolve "
                    "through the confirmed link.",
        "evidence": "a2a capability identity.materialize_link — 'the duplicate's "
                    "business/history rows are re-pointed to the primary and it is "
                    "soft-deleted (logins and derived intelligence are never moved). "
                    "One transaction, every move recorded, undoable. Reads already "
                    "resolve through the link, so this is optional'; auth_credentials "
                    "table",
    },
]
