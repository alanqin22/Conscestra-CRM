"""Blind action holdout — authored by an independent evaluator."""
from typing import Any, Dict, List

ACTIONS: List[Dict[str, Any]] = [
    # ------------------------------------------------------------------
    # CONTACT OPERATIONS (12)
    # ------------------------------------------------------------------
    {"request": "We've got two records for j.torres@northwind.example floating around. Merge them and keep the older one as the master.",
     "object": "contact",
     "operation": "merge",
     "executable": True,
     "evidence": "sp_contacts p_mode 'merge' (p_operation by_email/by_phone, keeps oldest created_at as master)"},

    {"request": "Archive contact 3f2a1b7c-9d44-4e18-8b02-5c6d7e8f9a01 — she left the company last Friday.",
     "object": "contact",
     "operation": "archive",
     "executable": True,
     "evidence": "sp_contacts p_mode 'archive' (requires p_contact_id, sets contacts.is_deleted = TRUE)"},

    {"request": "I archived Priya Raman by mistake last week. Can you restore her contact record?",
     "object": "contact",
     "operation": "restore",
     "executable": True,
     "evidence": "sp_contacts p_mode 'restore' (requires p_contact_id, sets contacts.is_deleted = FALSE)"},

    {"request": "show me duplicate contacts",
     "object": "contact",
     "operation": "duplicates",
     "executable": True,
     "evidence": "sp_contacts p_mode 'duplicates'"},

    {"request": "Add a new contact: Dana Whitfield, dana.whitfield@brightpath.example, 416-555-0142, role Procurement Manager.",
     "object": "contact",
     "operation": "create",
     "executable": True,
     "evidence": "sp_contacts p_mode 'create'"},

    {"request": "Update Marcus Lee's phone to 905-555-0188.",
     "object": "contact",
     "operation": "update",
     "executable": True,
     "evidence": "sp_contacts p_mode 'update'"},

    {"request": "Pull up the full record for Alicia Fernandez — I want everything, addresses included.",
     "object": "contact",
     "operation": "get_details",
     "executable": True,
     "evidence": "sp_contacts p_mode 'get_details'"},

    {"request": "list all contacts at Northwind Logistics",
     "object": "contact",
     "operation": "list",
     "executable": True,
     "evidence": "sp_contacts p_mode 'list'"},

    {"request": "Sam Okafor says he never received the confirmation email. Send the verification link to sam.okafor@harborline.example again.",
     "object": "contact",
     "operation": "send_verification",
     "executable": True,
     "evidence": "sp_contacts p_mode 'send_verification' (p_token, p_token_expires_hours)"},

    {"request": "Here's the token from the link he clicked: a7c91e44d2b8. Mark that contact's email as verified.",
     "object": "contact",
     "operation": "verify_email",
     "executable": True,
     "evidence": "sp_contacts p_mode 'verify_email' (contacts.is_email_verified, email_verification_token)"},

    {"request": "Give me a summary of our contact base — how many total, how many verified, how many are customers.",
     "object": "contact",
     "operation": "summary",
     "executable": True,
     "evidence": "sp_contacts p_mode 'summary'"},

    {"request": "What activities do we have logged against Rebecca Hughes?",
     "object": "contact",
     "operation": "activities",
     "executable": True,
     "evidence": "sp_contacts p_mode 'activities'"},

    # ------------------------------------------------------------------
    # ACCOUNT OPERATIONS (10)
    # ------------------------------------------------------------------
    {"request": "Somebody created Cascade Manufacturing twice. Merge the two accounts into one.",
     "object": "account",
     "operation": "merge",
     "executable": True,
     "evidence": "sp_accounts p_mode 'merge'"},

    {"request": "Find duplicate accounts.",
     "object": "account",
     "operation": "duplicates",
     "executable": True,
     "evidence": "sp_accounts p_mode 'duplicates'"},

    {"request": "Bluewater Foods went under. Archive the account so it stops showing up in my lists.",
     "object": "account",
     "operation": "archive",
     "executable": True,
     "evidence": "sp_accounts p_mode 'archive' (accounts.is_deleted = TRUE)"},

    {"request": "Ridgeline Systems came back to us. Restore that account — I deleted it back in March.",
     "object": "account",
     "operation": "restore",
     "executable": True,
     "evidence": "sp_accounts p_mode 'restore' (accounts.is_deleted = FALSE)"},

    {"request": "What's the financial picture on Vertex Industrial? Invoiced, paid, and what's still outstanding.",
     "object": "account",
     "operation": "financials",
     "executable": True,
     "evidence": "sp_accounts p_mode 'financials'"},

    {"request": "Show me the full timeline for Halcyon Retail.",
     "object": "account",
     "operation": "timeline",
     "executable": True,
     "evidence": "sp_accounts p_mode 'timeline'"},

    {"request": "open account 8c11d2e0-4a5f-4b3c-9e77-1a2b3c4d5e6f",
     "object": "account",
     "operation": "get",
     "executable": True,
     "evidence": "sp_accounts p_mode 'get'"},

    {"request": "list accounts in the manufacturing industry",
     "object": "account",
     "operation": "list",
     "executable": True,
     "evidence": "sp_accounts p_mode 'list'"},

    {"request": "Which accounts does Elena Vasquez own?",
     "object": "account",
     "operation": "list_owner",
     "executable": True,
     "evidence": "sp_accounts p_mode 'list_owner' (accounts.owner_id)"},

    {"request": "Change Northwind Logistics' website to https://northwind-logistics.example and set the industry to Transportation.",
     "object": "account",
     "operation": "update",
     "executable": True,
     "evidence": "sp_accounts p_mode 'update'"},

    # ------------------------------------------------------------------
    # LEAD OPERATIONS (10)
    # ------------------------------------------------------------------
    {"request": "Tomas Berg signed. Convert his lead into an account and an opportunity.",
     "object": "lead",
     "operation": "convert",
     "executable": True,
     "evidence": "sp_leads p_mode 'convert' (sets leads.converted, converted_account_id/contact_id/opportunity_id)"},

    {"request": "Budget's confirmed and we're talking to the decision maker — qualify lead 5d9e77a1-3b2c-4f80-9a11-0c8d7e6f5a44.",
     "object": "lead",
     "operation": "qualify",
     "executable": True,
     "evidence": "sp_leads p_mode 'qualify' (leads.status = 'qualified', qualification_reason)"},

    {"request": "disqualify this lead — wrong region, no budget",
     "object": "lead",
     "operation": "disqualify",
     "executable": True,
     "evidence": "sp_leads p_mode 'disqualify' (leads.status = 'disqualified', disqualification_reason)"},

    {"request": "Re-score Hannah Kim's lead, her company info was just updated.",
     "object": "lead",
     "operation": "score",
     "executable": True,
     "evidence": "sp_leads p_mode 'score' (leads.score, score_updated_at)"},

    {"request": "Orion Freight came in three separate times off the same campaign. Merge those leads into a single record.",
     "object": "lead",
     "operation": "merge",
     "executable": True,
     "evidence": "sp_leads p_mode 'merge' (leads.merged_into_lead_id)"},

    {"request": "Are there duplicate leads in here?",
     "object": "lead",
     "operation": "duplicates",
     "executable": True,
     "evidence": "sp_leads p_mode 'duplicates' (leads.dedupe_group_id, dedupe_confidence)"},

    {"request": "Kyle Nguyen has been cold since the 2024 trade show. Archive that lead.",
     "object": "lead",
     "operation": "archive",
     "executable": True,
     "evidence": "sp_leads p_mode 'archive' (leads.is_deleted, deleted_at)"},

    {"request": "I deleted Marcus Ibrahim's lead too early — he just replied. Restore it.",
     "object": "lead",
     "operation": "restore",
     "executable": True,
     "evidence": "sp_leads p_mode 'restore'"},

    {"request": "Show me the lead pipeline broken out by status.",
     "object": "lead",
     "operation": "pipeline",
     "executable": True,
     "evidence": "sp_leads p_mode 'pipeline'"},

    {"request": "New lead please: Sofia Marchetti at Delta Pack Solutions, sofia.m@deltapack.example, source is the March webinar.",
     "object": "lead",
     "operation": "create",
     "executable": True,
     "evidence": "sp_leads p_mode 'create'"},

    # ------------------------------------------------------------------
    # OPPORTUNITY OPERATIONS (8)
    # ------------------------------------------------------------------
    {"request": "Add 12 units of SKU RTX-400 to the Vertex Q3 Expansion opportunity.",
     "object": "opportunity",
     "operation": "add_product",
     "executable": True,
     "evidence": "sp_opportunities p_mode 'add_product' (p_product_id, p_quantity, p_selling_price)"},

    {"request": "Take the RTX-400 line off Vertex Q3 Expansion — customer dropped it from scope.",
     "object": "opportunity",
     "operation": "remove_product",
     "executable": True,
     "evidence": "sp_opportunities p_mode 'remove_product' (p_opp_product_id)"},

    {"request": "Bump the RTX-400 line on Vertex Q3 Expansion from 12 units up to 20.",
     "object": "opportunity",
     "operation": "update_product",
     "executable": True,
     "evidence": "sp_opportunities p_mode 'update_product'"},

    {"request": "Delete the opportunity called 'Test Deal - ignore'. I created it by accident.",
     "object": "opportunity",
     "operation": "delete",
     "executable": True,
     "evidence": "sp_opportunities p_mode 'delete'"},

    {"request": "Move Halcyon Retail Renewal to Negotiation and set probability to 70.",
     "object": "opportunity",
     "operation": "update",
     "executable": True,
     "evidence": "sp_opportunities p_mode 'update' (opportunities.stage, probability)"},

    {"request": "what's the forecast for this quarter",
     "object": "opportunity",
     "operation": "forecast",
     "executable": True,
     "evidence": "sp_opportunities p_mode 'forecast'"},

    {"request": "How accurate have our forecasts actually been?",
     "object": "opportunity",
     "operation": "forecast_accuracy",
     "executable": True,
     "evidence": "sp_opportunities p_mode 'forecast_accuracy'"},

    {"request": "Show me the pipeline by stage.",
     "object": "opportunity",
     "operation": "pipeline",
     "executable": True,
     "evidence": "sp_opportunities p_mode 'pipeline'"},

    # ------------------------------------------------------------------
    # ORDER / ACCOUNTING OPERATIONS (8)
    # ------------------------------------------------------------------
    {"request": "Create an order for Cascade Manufacturing: 5 of SKU BLT-220, wholesale pricing.",
     "object": "order",
     "operation": "create",
     "executable": True,
     "evidence": "sp_orders p_mode 'create'"},

    {"request": "Set order ORD-10490 to shipped.",
     "object": "order",
     "operation": "update",
     "executable": True,
     "evidence": "sp_orders p_mode 'update' (orders.status)"},

    {"request": "ORD-10517 is a duplicate of 10516. Delete it.",
     "object": "order",
     "operation": "delete",
     "executable": True,
     "evidence": "sp_orders p_mode 'delete' (orders.deleted_at, p_force_hard_delete)"},

    {"request": "Which orders are sitting there ready to be invoiced for Halcyon Retail?",
     "object": "order",
     "operation": "get_invoiceable_orders",
     "executable": True,
     "evidence": "sp_accounting p_mode 'get_invoiceable_orders'"},

    {"request": "Generate an invoice covering orders ORD-10422 and ORD-10423 for Vertex Industrial, net 30 terms.",
     "object": "invoice",
     "operation": "generate_invoice",
     "executable": True,
     "evidence": "sp_accounting p_mode 'generate_invoice' (p_order_ids uuid[], p_due_date)"},

    {"request": "Record a $4,250 payment against invoice INV-2026-0188 — came in by wire, ref WT-88213.",
     "object": "invoice",
     "operation": "record_payment",
     "executable": True,
     "evidence": "sp_accounting p_mode 'record_payment' (p_amount, p_payment_method, p_transaction_reference)"},

    {"request": "INV-2026-0201 went out to the wrong account. Void it.",
     "object": "invoice",
     "operation": "void_invoice",
     "executable": True,
     "evidence": "sp_accounting p_mode 'void_invoice' (invoices.status, cancelled_at)"},

    {"request": "Pull every invoice we've issued to Vertex Industrial.",
     "object": "invoice",
     "operation": "list_invoices_for_account",
     "executable": True,
     "evidence": "sp_accounting p_mode 'list_invoices_for_account'"},

    # ------------------------------------------------------------------
    # NEAR-NEIGHBOUR OPERATION PAIRS (6 requests = 3 minimal-diff pairs)
    # Each pair differs ONLY in the requested operation.
    # ------------------------------------------------------------------
    # Pair 1 — accounts: duplicates (read) vs merge (write)
    {"request": "Show me the duplicate accounts for Cascade Manufacturing.",
     "object": "account",
     "operation": "duplicates",
     "executable": True,
     "evidence": "sp_accounts p_mode 'duplicates' — read-only; must NOT resolve to 'merge'"},

    {"request": "Merge the duplicate accounts for Cascade Manufacturing.",
     "object": "account",
     "operation": "merge",
     "executable": True,
     "evidence": "sp_accounts p_mode 'merge' — write; must NOT resolve to 'duplicates'"},

    # Pair 2 — leads: qualify vs disqualify (opposite outcomes, same record)
    {"request": "Qualify lead 7a3c05be-1f92-4d6a-b8e3-2c4f9a1d7e50.",
     "object": "lead",
     "operation": "qualify",
     "executable": True,
     "evidence": "sp_leads p_mode 'qualify' — leads.status = 'qualified'"},

    {"request": "Disqualify lead 7a3c05be-1f92-4d6a-b8e3-2c4f9a1d7e50.",
     "object": "lead",
     "operation": "disqualify",
     "executable": True,
     "evidence": "sp_leads p_mode 'disqualify' — leads.status = 'disqualified'"},

    # Pair 3 — contacts: archive vs restore (exact inverses)
    {"request": "Archive the contact record for Devon Marsh.",
     "object": "contact",
     "operation": "archive",
     "executable": True,
     "evidence": "sp_contacts p_mode 'archive' — is_deleted TRUE; inverse of 'restore'"},

    {"request": "Restore the contact record for Devon Marsh.",
     "object": "contact",
     "operation": "restore",
     "executable": True,
     "evidence": "sp_contacts p_mode 'restore' — is_deleted FALSE; inverse of 'archive'"},

    # ------------------------------------------------------------------
    # INFORMATIONAL / ACTION CONTRASTS (6 requests = 3 contrasts)
    # The question must be answered, not executed. The imperative must execute.
    # ------------------------------------------------------------------
    {"request": "Can I archive a contact in Conscestra, or does that delete them permanently?",
     "object": "contact",
     "operation": "knowledge",
     "executable": False,
     "evidence": "informational — asks about capability; sp_contacts 'archive' is a soft delete (is_deleted) and no hard-delete mode exists"},

    {"request": "Archive the contact for Yusuf Demir.",
     "object": "contact",
     "operation": "archive",
     "executable": True,
     "evidence": "sp_contacts p_mode 'archive'"},

    {"request": "How does merging accounts work here — what happens to the contacts and orders on the losing record?",
     "object": "account",
     "operation": "knowledge",
     "executable": False,
     "evidence": "informational — explains sp_accounts p_mode 'merge' semantics, does not invoke it"},

    {"request": "Merge the duplicate accounts for Orion Freight.",
     "object": "account",
     "operation": "merge",
     "executable": True,
     "evidence": "sp_accounts p_mode 'merge'"},

    {"request": "What actually happens when I convert a lead? Does it always create an opportunity?",
     "object": "lead",
     "operation": "knowledge",
     "executable": False,
     "evidence": "informational — describes sp_leads p_mode 'convert' behaviour (converted_account_id / converted_contact_id / converted_opportunity_id) without executing it"},

    {"request": "Convert Amara Osei's lead to an account.",
     "object": "lead",
     "operation": "convert",
     "executable": True,
     "evidence": "sp_leads p_mode 'convert'"},
]
