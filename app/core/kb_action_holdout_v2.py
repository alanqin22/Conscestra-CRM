"""Sealed blind action holdout v2 — authored by an independent evaluator."""
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Ground truth for every `operation` below was read out of the LIVE DATABASE
# (SELECT prosrc FROM pg_proc ...) and reduced to the IF / ELSIF branches that
# actually test p_mode. Verified mode sets:
#
#   sp_contacts      list, get_details, create, update, send_verification,
#                    verify_email, duplicates, merge, activities, summary,
#                    archive, restore                                   (12)
#   sp_accounts      list, get, create, update, timeline, financials,
#                    duplicates, merge, archive, restore, summary,
#                    list_owner                                         (12)
#   sp_leads         list, get, create, update, qualify, disqualify,
#                    convert, pipeline, duplicates, list_employee,
#                    archive, restore, score, merge                     (14)
#   sp_opportunities list, get, create, update, delete, add_product,
#                    update_product, remove_product, pipeline, win_rate,
#                    forecast_accuracy, forecast, search_accounts,
#                    search_products, search_opportunities, get_owners  (16)
#                    >>> NO merge / archive / restore <<<
#   sp_orders        account_search, list_employees, contact_search,
#                    get_pricing, list, get_detail, create, update,
#                    delete, account_summary, category_summary,
#                    sales_summary, get_category, get_product           (14)
#   sp_activities    log_call, log_email, schedule_meeting, create_task,
#                    add_note, complete, reopen, list, get, create,
#                    update, delete, timeline, overdue, upcoming,
#                    summary, get_owners                                (17)
#   sp_accounting    list_employee, generate_invoice, record_payment,
#                    void_invoice, list_invoices, list_invoices_for_account,
#                    get_invoice_360, list_payments, get_payment_360,
#                    account_balance, account_balance_lookup,
#                    account_search, get_invoiceable_orders,
#                    accounting_summary                                 (14)
#   sp_products      list, get_details, add, update, bulk_adjust_stock,
#                    inventory_summary, low_stock, price_history,
#                    price_matrix, product_search, list_categories      (11)
#                    (its own valid_modes ARRAY also advertises
#                     add_price / toggle_active / adjust_stock, but NO
#                     p_mode branch dispatches them — stale list.)
#
# Identifiers used below are real rows sampled from the same database.
#
# NOTE ON CONTROLS: a few no-target rows legitimately expect execution, because
# the operation is a scan/aggregate that needs no record (duplicates, forecast,
# list_invoices). They exist so that "always ask when no id is present" cannot
# score 100% on this set.
# ---------------------------------------------------------------------------

ACTIONS_V2: List[Dict[str, Any]] = [
    # ----- CONTACTS (12) ---------------------------------------------------
    {"request": "Please merge the duplicate contact records for nolan.clarke-8ae3@seed.agentorc.ca into the primary one.",
     "object": "contact",
     "operation": "merge",
     "has_target": True,
     "expected": "execute merge",
     "evidence": "sp_contacts p_mode 'merge'"},

    {"request": "there's a bunch of dupes in here, merge them",
     "object": "contact",
     "operation": "merge",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_contacts p_mode 'merge' (must not fall back to 'duplicates')"},

    {"request": "can you find the duplicate contacts?",
     "object": "contact",
     "operation": "duplicates",
     "has_target": False,
     "expected": "execute duplicates",
     "evidence": "sp_contacts p_mode 'duplicates' - scan mode, no target required"},

    {"request": "archive contact 226c8744-c4cc-44f9-9289-fca5e1f512fb",
     "object": "contact",
     "operation": "archive",
     "has_target": True,
     "expected": "execute archive",
     "evidence": "sp_contacts p_mode 'archive'"},

    {"request": "pull up contact 226c8744-c4cc-44f9-9289-fca5e1f512fb",
     "object": "contact",
     "operation": "get_details",
     "has_target": True,
     "expected": "execute get_details",
     "evidence": "sp_contacts p_mode 'get_details' (near-neighbour of the archive request above)"},

    {"request": "archive it",
     "object": "contact",
     "operation": "archive",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_contacts p_mode 'archive' (must not degrade to 'get_details' or 'list')"},

    {"request": "Restore Diana Lopez's contact record - 940e4818-a418-4bc4-880d-538bcdd2ed46 - we archived her by mistake last week.",
     "object": "contact",
     "operation": "restore",
     "has_target": True,
     "expected": "execute restore",
     "evidence": "sp_contacts p_mode 'restore'"},

    {"request": "un-archive these please",
     "object": "contact",
     "operation": "restore",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_contacts p_mode 'restore' (must not degrade to 'list')"},

    {"request": "send a verification email to mateo.park-a431@seed.agentorc.ca",
     "object": "contact",
     "operation": "send_verification",
     "has_target": True,
     "expected": "execute send_verification",
     "evidence": "sp_contacts p_mode 'send_verification'"},

    {"request": "update their phone number to +1 416 555 0199",
     "object": "contact",
     "operation": "update",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_contacts p_mode 'update'"},

    {"request": "show me every activity logged against Alan Chang (alanc-fa1e@seed.agentorc.ca)",
     "object": "contact",
     "operation": "activities",
     "has_target": True,
     "expected": "execute activities",
     "evidence": "sp_contacts p_mode 'activities'"},

    {"request": "Does Conscestra let me merge two contact records, or do I have to delete one of them?",
     "object": "contact",
     "operation": "knowledge",
     "has_target": False,
     "expected": "answer knowledge",
     "evidence": "informational; the capability it asks about is sp_contacts p_mode 'merge'"},

    # ----- ACCOUNTS (10) ---------------------------------------------------
    {"request": "Merge Bennett Legal Group (d0e81c80-d42e-4d3b-805d-3a6b362f2b9d) into Bennett Foods (32bb1861-d486-486c-9e77-18f2a4c7e9a3).",
     "object": "account",
     "operation": "merge",
     "has_target": True,
     "expected": "execute merge",
     "evidence": "sp_accounts p_mode 'merge'"},

    {"request": "these two are the same company, merge em",
     "object": "account",
     "operation": "merge",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_accounts p_mode 'merge' (must not fall back to 'duplicates')"},

    {"request": "Archive the Horizon Freight Systems account - they went under in June.",
     "object": "account",
     "operation": "archive",
     "has_target": True,
     "expected": "execute archive",
     "evidence": "sp_accounts p_mode 'archive'"},

    {"request": "bring it back, we shouldnt have archived that one",
     "object": "account",
     "operation": "restore",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_accounts p_mode 'restore'"},

    {"request": "what's the financial picture on Zenith Manufacturing",
     "object": "account",
     "operation": "financials",
     "has_target": True,
     "expected": "execute financials",
     "evidence": "sp_accounts p_mode 'financials'"},

    {"request": "give me the timeline on this account",
     "object": "account",
     "operation": "timeline",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_accounts p_mode 'timeline'"},

    {"request": "check whether Martin Retail Group has any duplicate records sitting in the system",
     "object": "account",
     "operation": "duplicates",
     "has_target": True,
     "expected": "execute duplicates",
     "evidence": "sp_accounts p_mode 'duplicates'"},

    {"request": "restore account 8c18b78e-1c52-4659-8e16-b519fdba883f",
     "object": "account",
     "operation": "restore",
     "has_target": True,
     "expected": "execute restore",
     "evidence": "sp_accounts p_mode 'restore'"},

    {"request": "archive them all",
     "object": "account",
     "operation": "archive",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_accounts p_mode 'archive' (must not degrade to 'list')"},

    {"request": "How does account merging work in Conscestra - does it keep the older record or the newer one as the survivor?",
     "object": "account",
     "operation": "knowledge",
     "has_target": False,
     "expected": "answer knowledge",
     "evidence": "informational; the capability it asks about is sp_accounts p_mode 'merge'"},

    # ----- LEADS (10) ------------------------------------------------------
    {"request": "Convert Mason Reid (c3c5467e-b5a8-4dd0-b83c-6e1ab9f2099a) into an account and an opportunity, he signed the SOW this morning.",
     "object": "lead",
     "operation": "convert",
     "has_target": True,
     "expected": "execute convert",
     "evidence": "sp_leads p_mode 'convert'"},

    {"request": "convert them",
     "object": "lead",
     "operation": "convert",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_leads p_mode 'convert' (must not degrade to 'list' or 'pipeline')"},

    {"request": "Qualify the lead for aisha.karim-7a1f@seed.agentorc.ca",
     "object": "lead",
     "operation": "qualify",
     "has_target": True,
     "expected": "execute qualify",
     "evidence": "sp_leads p_mode 'qualify'"},

    {"request": "Disqualify the lead for aisha.karim-7a1f@seed.agentorc.ca - budget fell through.",
     "object": "lead",
     "operation": "disqualify",
     "has_target": True,
     "expected": "execute disqualify",
     "evidence": "sp_leads p_mode 'disqualify' (near-neighbour of the qualify request above)"},

    {"request": "merge the duplicate leads",
     "object": "lead",
     "operation": "merge",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_leads p_mode 'merge' (must not fall back to 'duplicates')"},

    {"request": "are there duplicate leads in here?",
     "object": "lead",
     "operation": "duplicates",
     "has_target": False,
     "expected": "execute duplicates",
     "evidence": "sp_leads p_mode 'duplicates' - scan mode, no target required"},

    {"request": "re-score lead 6b2e48df-815a-414b-8a8d-56137a724daf please",
     "object": "lead",
     "operation": "score",
     "has_target": True,
     "expected": "execute score",
     "evidence": "sp_leads p_mode 'score'"},

    {"request": "archive this one, it's junk",
     "object": "lead",
     "operation": "archive",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_leads p_mode 'archive'"},

    {"request": "Restore the lead record for Hazel Reid (hazel.reid-c9aa@seed.agentorc.ca), I archived it by accident.",
     "object": "lead",
     "operation": "restore",
     "has_target": True,
     "expected": "execute restore",
     "evidence": "sp_leads p_mode 'restore'"},

    {"request": "Can leads be un-archived after they've been archived, or is that permanent?",
     "object": "lead",
     "operation": "knowledge",
     "has_target": False,
     "expected": "answer knowledge",
     "evidence": "informational; the capability it asks about is sp_leads p_mode 'restore'"},

    # ----- OPPORTUNITIES (8) ----------------------------------------------
    {"request": "Remove the Razer BlackWidow V4 line from opportunity 77c9221c-2324-415a-b38e-4c870d04e38b.",
     "object": "opportunity",
     "operation": "remove_product",
     "has_target": True,
     "expected": "execute remove_product",
     "evidence": "sp_opportunities p_mode 'remove_product'"},

    {"request": "Add the Razer BlackWidow V4 to opportunity 77c9221c-2324-415a-b38e-4c870d04e38b.",
     "object": "opportunity",
     "operation": "add_product",
     "has_target": True,
     "expected": "execute add_product",
     "evidence": "sp_opportunities p_mode 'add_product' (near-neighbour of the remove_product request above)"},

    {"request": "delete opportunity 5d7e502e-8b1f-4701-88ca-a4aae6df7f2c, it was created in error",
     "object": "opportunity",
     "operation": "delete",
     "has_target": True,
     "expected": "execute delete",
     "evidence": "sp_opportunities p_mode 'delete'"},

    {"request": "just delete it",
     "object": "opportunity",
     "operation": "delete",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_opportunities p_mode 'delete' (must not degrade to 'get' or 'list')"},

    {"request": "Merge opportunity 28625bab-e43c-4918-97f7-36d0a707839d into 77c9221c-2324-415a-b38e-4c870d04e38b.",
     "object": "opportunity",
     "operation": "merge",
     "has_target": True,
     "expected": "refuse unsupported",
     "evidence": "sp_opportunities has NO 'merge' p_mode branch (16 branches verified in prosrc)"},

    {"request": "Can you archive these closed-lost deals so they stop cluttering up the board?",
     "object": "opportunity",
     "operation": "archive",
     "has_target": False,
     "expected": "refuse unsupported",
     "evidence": "sp_opportunities has NO 'archive' p_mode branch (16 branches verified in prosrc)"},

    {"request": "what's the forecast looking like",
     "object": "opportunity",
     "operation": "forecast",
     "has_target": False,
     "expected": "execute forecast",
     "evidence": "sp_opportunities p_mode 'forecast' - aggregate mode, no target required"},

    {"request": "push the close date on this out by two weeks",
     "object": "opportunity",
     "operation": "update",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_opportunities p_mode 'update'"},

    # ----- ORDERS / INVOICES (8) ------------------------------------------
    {"request": "Void invoice INV-000560 - it was issued against the wrong account.",
     "object": "invoice",
     "operation": "void_invoice",
     "has_target": True,
     "expected": "execute void_invoice",
     "evidence": "sp_accounting p_mode 'void_invoice'"},

    {"request": "Pull up invoice INV-000560.",
     "object": "invoice",
     "operation": "get_invoice_360",
     "has_target": True,
     "expected": "execute get_invoice_360",
     "evidence": "sp_accounting p_mode 'get_invoice_360' (near-neighbour of the void request above)"},

    {"request": "void it",
     "object": "invoice",
     "operation": "void_invoice",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_accounting p_mode 'void_invoice' (must not degrade to 'list_invoices')"},

    {"request": "Record a $1,200 payment against invoice INV-001149 - cheque, recieved today.",
     "object": "invoice",
     "operation": "record_payment",
     "has_target": True,
     "expected": "execute record_payment",
     "evidence": "sp_accounting p_mode 'record_payment'"},

    {"request": "generate invoices for these orders",
     "object": "invoice",
     "operation": "generate_invoice",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_accounting p_mode 'generate_invoice' (must not degrade to 'get_invoiceable_orders')"},

    {"request": "Delete order SO-2026-103800, it got duplicated during the import.",
     "object": "order",
     "operation": "delete",
     "has_target": True,
     "expected": "execute delete",
     "evidence": "sp_orders p_mode 'delete'"},

    {"request": "change the status on this order to shipped",
     "object": "order",
     "operation": "update",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_orders p_mode 'update' (must not degrade to 'get_detail' or 'list')"},

    {"request": "show me the outstanding invoices",
     "object": "invoice",
     "operation": "list_invoices",
     "has_target": False,
     "expected": "execute list_invoices",
     "evidence": "sp_accounting p_mode 'list_invoices' - list mode, no target required"},

    # ----- NEAR-NEIGHBOUR PAIRS (6: three pairs differing only by the verb) -
    {"request": "Mark activity 632df5ac-662b-48d7-96d8-45e1d6a50369 complete.",
     "object": "activity",
     "operation": "complete",
     "has_target": True,
     "expected": "execute complete",
     "evidence": "sp_activities p_mode 'complete' (wrapper mode -> update, p_completed_at set)"},

    {"request": "Reopen activity 632df5ac-662b-48d7-96d8-45e1d6a50369.",
     "object": "activity",
     "operation": "reopen",
     "has_target": True,
     "expected": "execute reopen",
     "evidence": "sp_activities p_mode 'reopen' (wrapper mode -> update, p_completed_at cleared)"},

    {"request": "delete these tasks",
     "object": "activity",
     "operation": "delete",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_activities p_mode 'delete' (must not degrade to 'list')"},

    {"request": "close them off",
     "object": "activity",
     "operation": "complete",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_activities p_mode 'complete' (must not degrade to 'list' or 'overdue')"},

    {"request": "price history on the Nike Women's Air Max 270 Lifestyle Shoe",
     "object": "product",
     "operation": "price_history",
     "has_target": True,
     "expected": "execute price_history",
     "evidence": "sp_products p_mode 'price_history'"},

    {"request": "stock adjustment on the Nike Women's Air Max 270 Lifestyle Shoe - bring it up to 40 units",
     "object": "product",
     "operation": "bulk_adjust_stock",
     "has_target": True,
     "expected": "execute bulk_adjust_stock",
     "evidence": "sp_products p_mode 'bulk_adjust_stock' (near-neighbour of the price_history request above)"},

    # ----- INFORMATIONAL vs IMPERATIVE CONTRASTS (6: three matched pairs) ---
    {"request": "Can Conscestra adjust stock levels for a whole batch of products at once?",
     "object": "product",
     "operation": "knowledge",
     "has_target": False,
     "expected": "answer knowledge",
     "evidence": "informational; the capability it asks about is sp_products p_mode 'bulk_adjust_stock'"},

    {"request": "Adjust the stock levels on these.",
     "object": "product",
     "operation": "bulk_adjust_stock",
     "has_target": False,
     "expected": "ask for target",
     "evidence": "sp_products p_mode 'bulk_adjust_stock' (imperative twin of the capability question above)"},

    {"request": "Is there a way to reopen an activity after somebody has marked it done?",
     "object": "activity",
     "operation": "knowledge",
     "has_target": False,
     "expected": "answer knowledge",
     "evidence": "informational; the capability it asks about is sp_activities p_mode 'reopen'"},

    {"request": "Reopen the 'Order confirmed - invoice generated' task, activity 4c7b1950-4d0d-435b-bec3-671822d01f89.",
     "object": "activity",
     "operation": "reopen",
     "has_target": True,
     "expected": "execute reopen",
     "evidence": "sp_activities p_mode 'reopen' (imperative twin of the capability question above)"},

    {"request": "Where does the price matrix in Conscestra get its numbers from?",
     "object": "product",
     "operation": "knowledge",
     "has_target": False,
     "expected": "answer knowledge",
     "evidence": "informational; the capability it asks about is sp_products p_mode 'price_matrix'"},

    {"request": "Show me the price matrix for the Razer BlackWidow V4 Mechanical Gaming Keyboard.",
     "object": "product",
     "operation": "price_matrix",
     "has_target": True,
     "expected": "execute price_matrix",
     "evidence": "sp_products p_mode 'price_matrix' (imperative twin of the capability question above)"},
]
