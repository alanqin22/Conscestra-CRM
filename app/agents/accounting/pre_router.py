"""Accounting Pre-Router — Python equivalent of n8n pre-router v3.4.

Mirrors the n8n 'pre router' Code node (v3.4).

OVERVIEW
  Inspects every incoming message and either:
    • ROUTES directly to Build SQL Query (router_action = True)  — deterministic
    • Passes through to the AI Agent   (router_action = False)   — free-text NL

  DESIGN PRINCIPLE: Every SP mode that accepts deterministic parameters
  MUST be handled here. The AI Agent is invoked ONLY as a last resort for
  free-form natural-language queries that cannot be structured here.

KEY TRIGGERS (all produce router_action = True)
  "account search:"                            → account_search
  "account balance:"                           → account_balance
  "Search accounts by name <query>"            → account_search
  "List invoices for account <uuid>"           → list_invoices_for_account
  "Get invoiceable orders for account <uuid>"  → get_invoiceable_orders
  "list employees active"                      → list_employee
  "list filtered:"                             → list_invoices | list_payments | accounting_summary
  "generate invoice:"                          → generate_invoice
  "record payment:"                            → record_payment
  "void invoice:"                              → void_invoice
  "get invoice 360:"                           → get_invoice_360       (v3.4)
  "get payment 360:"                           → get_payment_360       (v3.4)
  All other messages                           → Passthru → AI Agent

CHANGELOG
  v3.4 — Added "get invoice 360:" → get_invoice_360
              and "get payment 360:" → get_payment_360 direct routes.
         All 14 SP modes now have a deterministic path; AI Agent is pure fallback.
  v3.3 — Added home-page Google-style Account Search Bar routes:
           "account search:" → account_search (searchText from chatInput)
           "account balance:" → account_balance (accountId from chatInput)
         Updated list_filtered to support account_summary → accounting_summary.
  v3.2 — Added AccountingFilterBar direct route:
           "list filtered:" → list_invoices | list_payments
  v3.1 — Added four UI-lookup direct routes:
           "Search accounts by name <query>"           → account_search
           "List invoices for account <uuid>"          → list_invoices_for_account
           "Get invoiceable orders for account <uuid>" → get_invoiceable_orders
           "list employees active"                     → list_employee
  v3.0 — Added direct routes for generate_invoice / record_payment / void_invoice.
"""

import re
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

UUID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)


# ============================================================================
# HELPERS
# ============================================================================

def _extract_uuid(s: str) -> Optional[str]:
    """Return the first UUID found in *s*, or None."""
    m = UUID_PATTERN.search(str(s))
    return m.group(0) if m else None


def _val(v: Any) -> Any:
    """Coerce empty string / None to None; pass everything else through."""
    if v is None or v == '':
        return None
    return v


def _build_passthru_message(raw: str, chat_input: dict) -> str:
    """
    Build the normalised AI Agent message by appending any structured
    chatInput fields — mirrors the n8n passthru() helper and the v6
    preprocessor.py logic.
    """
    current = raw
    if chat_input.get('city'):
        current += f", city {chat_input['city']}"
    if chat_input.get('pageSize') is not None:
        current += f", page size {chat_input['pageSize']}"
    if chat_input.get('pageNumber') is not None:
        current += f", page number {chat_input['pageNumber']}"
    if chat_input.get('customerId'):
        current += f", customer ID {chat_input['customerId']}"
    return current.strip()


# ============================================================================
# MAIN ROUTER
# ============================================================================

def route_request(message: str, chat_input: dict) -> Dict[str, Any]:
    """
    Inspect the incoming message and return a routing decision dict:

    Routed (deterministic):
        {
            "router_action": True,
            "params": { "mode": "...", ... }   ← fed directly into sql_builder
        }

    Passthru (AI Agent handles):
        {
            "router_action": False,
            "current_message": "<preprocessed message string>"
        }

    Parameters
    ----------
    message     : Raw message string from chatInput.message
    chat_input  : Full chatInput dict (all structured fields)
    """
    raw = (message or '').strip()
    msg = raw.lower()

    # ── Deterministic web-search route ───────────────────────────────────────
    # Explicit "search the web…" phrasings go straight to mode=web_search so
    # they never depend on the AI agent's JSON discipline (with session history
    # it sometimes replies "I can only assist with CRM queries" instead of
    # emitting the web_search JSON).
    _web_m = re.match(
        r'^(?:please\s+)?(?:can\s+you\s+)?(?:search|browse|look\s*up|google)\s+'
        r'(?:on\s+|in\s+)?the\s+(?:web|internet)\s*(?:for|about|:)?\s*(.*)$',
        raw, re.IGNORECASE)
    if _web_m:
        _q = (_web_m.group(1) or '').strip().rstrip('?.!') or raw
        logger.info(f'-> ROUTED: web_search (deterministic) query={_q[:80]!r}')
        return {'router_action': True, 'params': {'mode': 'web_search', 'query': _q}}

    logger.info('=== Accounting Pre-Router v3.4 ===')
    logger.info(f'Message: {raw}')

    # ── routerAction short-circuit (v3.1) ───────────────────────────────────
    # HTML direct-SP calls send routerAction=True + mode in chatInput with no
    # message text.  Detect this here before message-pattern matching so we
    # never fall through to the AI Agent (which needs Ollama / OpenAI).
    _SKIP = {'routerAction', 'message', 'sessionId', 'chatInput',
             'originalBody', 'webhookUrl', 'executionMode',
             'currentMessage', 'chatHistory'}
    if chat_input.get('routerAction') and chat_input.get('mode'):
        _params = {k: v for k, v in chat_input.items()
                   if k not in _SKIP and v is not None}
        logger.info(f'→ routerAction SHORT-CIRCUIT: mode={_params.get("mode")}')
        return {'router_action': True, 'params': _params}

    def routed(params: dict) -> dict:
        logger.info(f'→ ROUTED: mode={params.get("mode")} params={str(params)[:200]}')
        return {'router_action': True, 'params': params}

    def passthru() -> dict:
        current_message = _build_passthru_message(raw, chat_input)
        logger.info(f'→ PASSTHRU: AI Agent | currentMessage: {current_message}')
        return {'router_action': False, 'current_message': current_message}

    # ── Home-Page Account Search Bar: account_search  (v3.3) ─────────────────
    #   Source: _homeSearchAccounts() — message prefix "account search:"
    #   chatInput.searchText = typed query (2+ chars)
    if msg.startswith('account search:'):
        query = (chat_input.get('searchText') or '').strip()
        if len(query) >= 2:
            logger.info(f'[account_search / home bar] searchText: {query}')
            return routed({'mode': 'account_search', 'searchText': query})
        logger.warning(f'[account_search / home bar] query too short ("{query}") — passthru')
        return passthru()

    # ── Home-Page Account Search Bar: account_balance  (v3.3) ────────────────
    #   Source: _homeSelectAccount() — message prefix "account balance:"
    #   chatInput.accountId = selected account UUID
    if msg.startswith('account balance:'):
        account_id = _val(chat_input.get('accountId')) or _extract_uuid(raw)
        if account_id:
            logger.info(f'[account_balance / home bar] accountId: {account_id}')
            return routed({'mode': 'account_balance', 'accountId': account_id})
        logger.warning('[account_balance / home bar] no accountId — falling through to AI Agent')
        # fall through to passthru below

    # ── Account Search  (v3.1) ────────────────────────────────────────────────
    #   Source: _giSearchAccounts() / _sharedSearchAccounts()
    #   Message: "Search accounts by name <query>"
    if msg.startswith('search accounts by name '):
        query = raw[len('search accounts by name '):].strip()
        if len(query) >= 2:
            logger.info(f'[account_search] query: {query}')
            return routed({'mode': 'account_search', 'search': query})
        logger.warning(f'[account_search] query too short ("{query}") — falling through')

    # ── List Invoices for Account  (v3.1) ─────────────────────────────────────
    #   Source: _loadInvoicesForAccount()
    #   Message: "List invoices for account <uuid>"
    if msg.startswith('list invoices for account '):
        account_id = _extract_uuid(raw)
        if account_id:
            logger.info(f'[list_invoices_for_account] accountId: {account_id}')
            return routed({'mode': 'list_invoices_for_account', 'accountId': account_id})
        logger.warning('[list_invoices_for_account] no UUID found — falling through')

    # ── Get Invoiceable Orders for Account  (v3.1) ────────────────────────────
    #   Source: _giLoadInvoiceableOrders()
    #   Message: "Get invoiceable orders for account <uuid>"
    if msg.startswith('get invoiceable orders for account '):
        account_id = _extract_uuid(raw)
        if account_id:
            logger.info(f'[get_invoiceable_orders] accountId: {account_id}')
            return routed({'mode': 'get_invoiceable_orders', 'accountId': account_id})
        logger.warning('[get_invoiceable_orders] no UUID found — falling through')

    # ── List Employees  (v3.1) ────────────────────────────────────────────────
    #   Source: _loadEmployeesFromAgent()
    #   Message: "list employees active" (exact, case-insensitive)
    if msg == 'list employees active':
        logger.info('[list_employee] direct route')
        return routed({'mode': 'list_employee'})

    # ── AccountingFilterBar / Search Bar: List Filtered  (v3.5) ──────────────
    #   Source: applyFilters() and sendDirectToAcctAgent()
    #   Message prefix: "list filtered:"
    #   chatInput: listingType, startDate, endDate, search (optional)
    if msg.startswith('list filtered:'):
        listing_type = (chat_input.get('listingType') or 'invoice').lower().strip()
        if listing_type == 'payment':
            mode = 'list_payments'
        elif listing_type == 'account_summary':
            mode = 'accounting_summary'
        else:
            mode = 'list_invoices'
        search_term = _val(chat_input.get('search'))
        page_number = chat_input.get('pageNumber')
        page_size   = chat_input.get('pageSize')
        logger.info(
            f'[list_filtered] listingType: {listing_type} | mode: {mode} | '
            f'search: {search_term!r} | page: {page_number} | size: {page_size}'
        )
        params: dict = {
            'mode':       mode,
            'startDate':  _val(chat_input.get('startDate')),
            'endDate':    _val(chat_input.get('endDate')),
        }
        if search_term:
            params['search'] = search_term
        if page_number is not None:
            params['pageNumber'] = page_number
        if page_size is not None:
            params['pageSize'] = page_size
        return routed(params)

    # ── Generate Invoice  (v3.0) ──────────────────────────────────────────────
    #   Source: submitGenerateInvoice()
    #   Message prefix: "generate invoice:"
    if msg.startswith('generate invoice:'):
        if not chat_input.get('accountId') or not chat_input.get('orderIds'):
            logger.warning('[generate_invoice] Missing required fields — passthru to AI Agent')
            return passthru()
        logger.info(
            f'[generate_invoice] accountId: {chat_input.get("accountId")} '
            f'| orderIds: {chat_input.get("orderIds")} '
            f'| contactId: {chat_input.get("contactId")} '
            f'| employeeUuid: {chat_input.get("employeeUuid")}'
        )
        return routed({
            'mode':           'generate_invoice',
            'accountId':      _val(chat_input.get('accountId')),
            'orderIds':       _val(chat_input.get('orderIds')),
            'contactId':      _val(chat_input.get('contactId')),        # v8.7 / v3.4
            'invoiceNumber':  _val(chat_input.get('invoiceNumber')),
            'invoiceType':    _val(chat_input.get('invoiceType')),
            'dueDate':        _val(chat_input.get('dueDate')),
            'currency':       _val(chat_input.get('currency')) or 'CAD',
            'taxRate':        _val(chat_input.get('taxRate')),
            'discountAmount': _val(chat_input.get('discountAmount')),
            'shippingAmount': _val(chat_input.get('shippingAmount')),
            'adjustment':     _val(chat_input.get('adjustment')),
            'ownerId':        _val(chat_input.get('ownerId')),
            'employeeUuid':   _val(chat_input.get('employeeUuid')),
            'notes':          _val(chat_input.get('notes')),
        })

    # ── Record Payment  (v3.0) ────────────────────────────────────────────────
    #   Source: submitRecordPayment()
    #   Message prefix: "record payment:"
    if msg.startswith('record payment:'):
        if not chat_input.get('invoiceId') or not chat_input.get('amount'):
            logger.warning('[record_payment] Missing required fields — passthru to AI Agent')
            return passthru()
        logger.info(
            f'[record_payment] invoiceId: {chat_input.get("invoiceId")} '
            f'| amount: {chat_input.get("amount")} '
            f'| method: {chat_input.get("paymentMethod")} '
            f'| employeeUuid: {chat_input.get("employeeUuid")}'
        )
        return routed({
            'mode':                 'record_payment',
            'invoiceId':            _val(chat_input.get('invoiceId')),
            'amount':               _val(chat_input.get('amount')),
            'paymentMethod':        _val(chat_input.get('paymentMethod')),
            'paymentSource':        _val(chat_input.get('paymentSource')),
            'currency':             _val(chat_input.get('currency')) or 'CAD',
            'transactionReference': _val(chat_input.get('transactionReference')),
            'ownerId':              _val(chat_input.get('ownerId')),
            'employeeUuid':         _val(chat_input.get('employeeUuid')),
            'notes':                _val(chat_input.get('notes')),
        })

    # ── Void Invoice  (v3.0) ──────────────────────────────────────────────────
    #   Source: submitVoidInvoice()
    #   Message prefix: "void invoice:"
    if msg.startswith('void invoice:'):
        if not chat_input.get('invoiceId'):
            logger.warning('[void_invoice] Missing invoiceId — passthru to AI Agent')
            return passthru()
        logger.info(
            f'[void_invoice] invoiceId: {chat_input.get("invoiceId")} '
            f'| employeeUuid: {chat_input.get("employeeUuid")}'
        )
        return routed({
            'mode':         'void_invoice',
            'invoiceId':    _val(chat_input.get('invoiceId')),
            'employeeUuid': _val(chat_input.get('employeeUuid')),
            'notes':        _val(chat_input.get('notes')),
        })

    # ── Get Invoice 360  (v3.4) ───────────────────────────────────────────────
    #   Source: actions.getInvoice360() / "View Invoice" buttons
    #   Message prefix: "get invoice 360:"
    #   chatInput.invoiceId = invoice UUID
    if msg.startswith('get invoice 360:'):
        invoice_id = _val(chat_input.get('invoiceId')) or _extract_uuid(raw)
        if invoice_id:
            logger.info(f'[get_invoice_360] invoiceId: {invoice_id}')
            return routed({'mode': 'get_invoice_360', 'invoiceId': invoice_id})
        logger.warning('[get_invoice_360] no invoiceId found — falling through to AI Agent')

    # ── Get Payment 360  (v3.4) ───────────────────────────────────────────────
    #   Source: actions.getPayment360() / "View Payment" buttons
    #   Message prefix: "get payment 360:"
    #   chatInput.paymentId = payment UUID
    if msg.startswith('get payment 360:'):
        payment_id = _val(chat_input.get('paymentId')) or _extract_uuid(raw)
        if payment_id:
            logger.info(f'[get_payment_360] paymentId: {payment_id}')
            return routed({'mode': 'get_payment_360', 'paymentId': payment_id})
        logger.warning('[get_payment_360] no paymentId found — falling through to AI Agent')

    # ── Vague UI-form intents → emit a marker so the frontend opens the
    # inline form instead of bouncing to the AI for a list of required
    # fields. Each detector skips itself when a UUID is present.
    _has_uuid = bool(_extract_uuid(raw))
    if not _has_uuid:
        # Pre-fill the form's account search when the chip names a customer,
        # e.g. "Generate invoice for Bob Brown" → the form opens pre-searched
        # on "Bob Brown" instead of blank. (Form is account-first: the user
        # then picks the order / invoice.)
        _pf_m = re.search(
            r'\b(?:for|of)\s+([A-Z][a-zA-Z]+(?:[-\s][A-Z][a-zA-Z]+)+)\s*$', raw)
        _pf_account = _pf_m.group(1).strip() if _pf_m else None

        def _form(form_mode: str) -> dict:
            p = {'mode': form_mode}
            if _pf_account:
                p['prefillAccount'] = _pf_account
            return routed(p)

        if re.search(r'\b(generate|create|new|issue|make)\b.*\binvoice', msg):
            return _form('show_invoice_form')
        if re.search(r'\b(record|log|enter|add)\b.*\bpayment', msg):
            return _form('show_payment_form')
        if re.search(r'\bvoid\b.*\binvoice', msg) \
           or re.search(r'\b(cancel|nullify)\b.*\binvoice', msg):
            return _form('show_void_invoice_form')

    # ── Executive finance questions (CEO / CFO / VP bank) ────────────────────
    # Shares the orchestrator's executive Q&A layer: deterministic sections
    # from sp_orchestrator('executive') with a one-line decision headline.
    # Checked before NL listing so e.g. "weighted forecast vs commit" or
    # "discounting exposure" aren't misread as invoice/payment list queries.
    try:
        from app.agents.orchestrator.executive import match_exec_question
        _exec = match_exec_question(raw)
    except Exception:
        _exec = None
    if _exec:
        _sections, _note = _exec
        logger.info(f'[executive] sections={_sections}')
        return routed({'mode': 'executive_question',
                       'sections': _sections, 'note': _note})

    # ── Invoice-number lookup → full 360 view ────────────────────────────────
    # Catches "Get invoice INV-000620", "Get invoice 360 for INV-000780",
    # "show invoice inv 620", etc.  The db_node resolves invoiceNumber →
    # invoice_id (via a list_invoices search) then runs get_invoice_360.
    m_invnum = re.search(r'\binv[-\s]?(\d{3,})\b', msg)
    if m_invnum and re.search(r'\binvoice\b', msg):
        inv_number = f"INV-{m_invnum.group(1)}"
        if '360' in msg or re.search(r'\b(?:get|show|find|open|view|display|detail|details)\b', msg):
            logger.info(f'[invoice-number-360] invoiceNumber: {inv_number}')
            return routed({'mode': 'get_invoice_360', 'invoiceNumber': inv_number})
        logger.info(f'[invoice-number-list] search: {inv_number}')
        return routed({'mode': 'list_invoices', 'search': inv_number})

    # ── Natural-language invoice / payment listing with optional date context ──
    # Catches phrases like:
    #   "show me invoices this month"        "list all invoices"
    #   "show invoices for this month"       "show invoices today"
    #   "display payments this week"         "show overdue invoices"
    #   "show me all invoices"               "list payments last month"
    #   "list unpaid invoices"               "list invoices page 2"
    #
    # Date expressions supported:
    #   today / this week / this month / last month / this quarter /
    #   last quarter / this year / last year / last N days
    #   If no date mentioned → list all (no date filter)
    _is_invoice_query = bool(re.search(
        r'\b(?:show|list|display|get|find|give)(?:\s+me)?(?:\s+all)?\s+(?:all\s+)?invoices?\b'
        r'|\binvoices?\s+(?:for\s+)?(?:this|last)\s+\w+'
        r'|\binvoices?\s+today\b'
        r'|\boverdue\s+invoices?\b'
        r'|\b(?:unpaid|paid|open|outstanding)\s+invoices?\b',
        msg, re.IGNORECASE
    ))
    _is_payment_query = bool(re.search(
        r'\b(?:show|list|display|get|find|give)(?:\s+me)?(?:\s+all)?\s+(?:all\s+)?payments?\b'
        r'|\bpayments?\s+(?:for\s+)?(?:this|last)\s+\w+'
        r'|\bpayments?\s+today\b',
        msg, re.IGNORECASE
    ))
    _is_summary_query = bool(re.search(
        r'\b(?:accounting\s+summary|accounting\s+report|account(?:ing)?\s+overview'
        r'|revenue\s+(?:summary|report))\b',
        msg, re.IGNORECASE
    ))

    if _is_invoice_query or _is_payment_query or _is_summary_query:
        today = date.today()
        start_date: Optional[str] = None
        end_date:   Optional[str] = None

        if re.search(r'\btoday\b', msg, re.IGNORECASE):
            start_date = today.isoformat()
            end_date   = today.isoformat()
        elif re.search(r'\bthis\s+week\b', msg, re.IGNORECASE):
            start_date = (today - timedelta(days=today.weekday())).isoformat()
            end_date   = today.isoformat()
        elif re.search(r'\blast\s+month\b', msg, re.IGNORECASE):
            first_this  = today.replace(day=1)
            last_month_end = first_this - timedelta(days=1)
            start_date  = last_month_end.replace(day=1).isoformat()
            end_date    = last_month_end.isoformat()
        elif re.search(r'\bthis\s+month\b', msg, re.IGNORECASE):
            start_date = today.replace(day=1).isoformat()
            end_date   = today.isoformat()
        elif re.search(r'\bthis\s+quarter\b', msg, re.IGNORECASE):
            q_start_month = ((today.month - 1) // 3) * 3 + 1
            start_date = today.replace(month=q_start_month, day=1).isoformat()
            end_date   = today.isoformat()
        elif re.search(r'\blast\s+quarter\b', msg, re.IGNORECASE):
            q_start_month  = ((today.month - 1) // 3) * 3 + 1
            this_q_start   = today.replace(month=q_start_month, day=1)
            last_q_end     = this_q_start - timedelta(days=1)
            lq_start_month = ((last_q_end.month - 1) // 3) * 3 + 1
            start_date = last_q_end.replace(month=lq_start_month, day=1).isoformat()
            end_date   = last_q_end.isoformat()
        elif re.search(r'\bthis\s+year\b', msg, re.IGNORECASE):
            start_date = today.replace(month=1, day=1).isoformat()
            end_date   = today.isoformat()
        elif re.search(r'\blast\s+year\b', msg, re.IGNORECASE):
            y = today.year - 1
            start_date = f'{y}-01-01'
            end_date   = f'{y}-12-31'
        elif m_days := re.search(r'\blast\s+(\d+)\s+days?\b', msg, re.IGNORECASE):
            n = int(m_days.group(1))
            start_date = (today - timedelta(days=n)).isoformat()
            end_date   = today.isoformat()
        elif m_year := re.search(r'\b(20\d{2})\b', raw):
            y = m_year.group(1)
            start_date = f'{y}-01-01'
            end_date   = f'{y}-12-31'

        # Determine mode
        if _is_summary_query:
            # "Accounting summary for Bob Brown" → per-account balance lookup
            # (the accounting_summary SP mode has no account filter; the
            # account_balance_lookup mode returns balance / invoiced / paid
            # per matching account).
            m_sum_name = re.search(
                r'\b(?:summary|overview|report)\s+(?:for|of)\s+'
                r'([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b',
                raw
            )
            if m_sum_name:
                acct = m_sum_name.group(1).strip()
                logger.info(f'[NL-accounting] summary for account → account_balance_lookup: {acct!r}')
                return routed({'mode': 'account_balance_lookup', 'search': acct})
            nl_mode = 'accounting_summary'
        elif _is_payment_query:
            nl_mode = 'list_payments'
        else:
            nl_mode = 'list_invoices'

        params: Dict[str, Any] = {'mode': nl_mode}
        if start_date is not None:
            params['startDate'] = start_date
        if end_date is not None:
            params['endDate'] = end_date

        # Page navigation: "list invoices page 2" → pageNumber=2
        m_page = re.search(r'\bpage\s+(\d+)\b', msg, re.IGNORECASE)
        if m_page:
            params['pageNumber'] = int(m_page.group(1))

        # Explicit paid / unpaid status filter (overdue handled below).
        if nl_mode == 'list_invoices' and not re.search(r'\boverdue\b', msg, re.IGNORECASE):
            if re.search(r'\b(?:unpaid|outstanding|open)\b', msg, re.IGNORECASE):
                # "unpaid / outstanding / open" = a balance is still owing
                # (payment_status unpaid OR partial). Was 'unpaid', which only
                # matched zero-payment invoices and missed partially-paid ones.
                params['statusFilter'] = 'outstanding'
            elif re.search(r'\bpaid\b', msg, re.IGNORECASE):
                params['statusFilter'] = 'paid'

        # Overdue invoice filter: pass statusFilter='unpaid' so the SP only
        # returns unpaid invoices; Python-side will further filter by due_date.
        _is_overdue = bool(re.search(r'\boverdue\b', msg, re.IGNORECASE))
        if _is_overdue and nl_mode == 'list_invoices':
            # 'overdue' = past due AND still owing. The SP maps statusFilter
            # 'overdue' to payment_status IN ('unpaid','partial') AND
            # due_date < today. (Previously used 'unpaid', which only matched
            # zero-payment invoices and missed partially-paid overdue ones —
            # returned 3 of 166.)
            params['statusFilter'] = 'overdue'

        # Extract optional customer/account name for invoice OR payment queries.
        # Handles:
        #   "find invoice for Emily Brown"     → search = "Emily Brown"
        #   "find invoices from Acme Corp"     → search = "Acme Corp"
        #   "find payment for Bob Brown"       → search = "Bob Brown"
        #   "show payments Bob Brown"          → search = "Bob Brown"
        # Use raw (original case) to detect proper nouns via capitalisation.
        _entity_word = r'(?:invoices?|payments?)'
        _acct_name_m = re.search(
            rf'\b{_entity_word}\s+(?:(?:for|from|by|of)\s+)?'
            r'([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+)',
            raw
        )
        if not _acct_name_m:
            # Fallback: optional preposition, case-insensitive.
            # Exclude matches whose first word is a date/time qualifier.
            _fb = re.search(
                rf'\b{_entity_word}\s+(?:(?:for|from|by|of)\s+)?'
                r'(.+?)(?:\s+(?:this|last|in|on|at|during)\b.*)?$',
                raw, re.IGNORECASE
            )
            if _fb:
                _candidate = _fb.group(1).strip().rstrip('?.,;')
                _first = _candidate.split()[0].lower() if _candidate else ''
                _skip = {'this', 'last', 'today', 'all', 'any', 'a', 'an', 'the',
                         'my', 'our', 'your', 'overdue', 'paid', 'unpaid', 'recent',
                         'summary', 'report', 'page', 'pages', 'open', 'outstanding',
                         'list', 'details', 'detail'}
                if _candidate and _first not in _skip:
                    _acct_name_m = _fb
        if _acct_name_m:
            acct_name = _acct_name_m.group(1).strip().rstrip('?.,;')
            if acct_name:
                params['search'] = acct_name

        logger.info(
            f'[NL-accounting] mode={nl_mode} start={start_date} end={end_date} '
            f'search={params.get("search")!r} | msg={raw[:60]!r}'
        )
        return routed(params)

    # ── All other messages → AI Agent ─────────────────────────────────────────
    return passthru()
