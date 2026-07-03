"""Contact Pre-Router - Python equivalent of n8n Pre Router v3.2.

ARCHITECTURAL PRINCIPLE:
  Deterministic SQL operations -> ALWAYS routed directly (no AI)
  Ambiguous / NL queries       -> AI Agent fallback ONLY

v3.2: Added NL shortcut patterns to catch sendRequest() button messages
      that previously fell through to the AI Agent / Ollama.
v3.1: compact() helper strips None values before routing.
v3.0: Complete rewrite with direct routes for all sp_contacts modes.

ROUTING TABLE:
  "list contacts:"       -> list
  "search contacts:"     -> list (alias)
  "get contact:"         -> get_details
  "get details:"         -> get_details (alias)
  "create contact:"      -> create
  "update contact:"      -> update
  "contact activities:"  -> activities
  "contact duplicates:"  -> duplicates
  "contact summary:"     -> summary
  "merge contacts:"      -> merge
  "send verification:"   -> send_verification
  "verify email:"        -> verify_email

  NL shortcuts (v3.2):
  "send verification email..." -> send_verification (extracts UUID)
  "show all contacts" etc.     -> list
  "archived contacts" etc.     -> list (deletedOnly=True)
  "duplicate contacts" etc.    -> duplicates

  All other messages -> Passthru -> AI Agent
"""

import re
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)


# ===========================================================================
# HELPERS
# ===========================================================================

def _val(v: Any) -> Optional[Any]:
    """Coerce empty string / None -> None."""
    if v is None or v == '':
        return None
    return v


def _compact(d: dict) -> dict:
    """Strip None values - prevents overriding SP DEFAULT params with NULL."""
    return {k: v for k, v in d.items() if v is not None}


def _extract_uuid(s: str) -> Optional[str]:
    """Return the first UUID found in a string, or None."""
    m = UUID_RE.search(str(s or ''))
    return m.group(0) if m else None


def _build_passthru_message(raw: str, chat_input: dict) -> str:
    """Append structured chatInput fields to the message."""
    current = raw
    if chat_input.get('pageSize') is not None:
        current += f", page size {chat_input['pageSize']}"
    if chat_input.get('pageNumber') is not None:
        current += f", page number {chat_input['pageNumber']}"
    return current.strip()


# ===========================================================================
# ROUTER
# ===========================================================================

def route_request(message: str, chat_input: dict) -> Dict[str, Any]:
    """
    Inspect the incoming message and return a routing decision dict.

    Routed:   { "router_action": True,  "params": { "mode": "...", ... } }
    Passthru: { "router_action": False, "current_message": "<string>" }
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

    # ── Deterministic AI-summary route ───────────────────────────────────────
    # "AI summary for <name>" / "Summarize <name>" → mode=ai_summary. Requires
    # a concrete person name so aggregate phrasings are never hijacked.
    _ais_m = re.match(
        r'^(?:ai\s+summary\s+(?:of|for)\s+|summari[sz]e\s+(?:contact\s+)?|brief\s+me\s+on\s+)'
        r'["\']?([A-Za-z][A-Za-z\-\'. ]{2,50}?)["\']?\s*$',
        raw, re.IGNORECASE)
    if _ais_m:
        _nm = _ais_m.group(1).strip()
        _generic = {'contact', 'contacts', 'all contacts', 'the contact',
                    'this contact', 'summary', 'statistics'}
        if _nm.lower() not in _generic and ' by ' not in _nm.lower():
            logger.info(f'-> ROUTED: ai_summary (deterministic) contact={_nm!r}')
            return {'router_action': True,
                    'params': {'mode': 'ai_summary', 'contactName': _nm}}

    logger.info('=== Contact Pre-Router v3.2 ===')
    logger.info(f'Message: {raw[:120]}')

    def routed(params: dict) -> dict:
        logger.info(
            f'-> ROUTED: mode={params.get("mode")} | {str(params)[:200]}'
        )
        return {'router_action': True, 'params': params}

    def passthru() -> dict:
        current = _build_passthru_message(raw, chat_input)
        logger.info(f'-> PASSTHRU: AI Agent | currentMessage: {current[:120]}')
        return {'router_action': False, 'current_message': current}

    # ── Web-intent guard — "search the web…", "look up … online" must reach
    # the AI agent (mode=web_search), not the deterministic search regexes.
    if msg and re.search(r'\b(?:web|internet|online)\b', msg):
        logger.info('-> PASSTHRU: web-intent message (AI agent handles web_search)')
        return passthru()

    # ── Executive questions (CEO / CFO / VP bank) ─────────────────────────────
    # Interrogative phrasings route to the shared executive Q&A layer with the
    # decision-grade format. Imperative commands ("Show active contacts",
    # "Find contacts named Lila") keep their deterministic routes below.
    _is_exec_q = raw.rstrip().endswith('?') or bool(re.match(
        r'^(?:are|what|which|how|do|does|where|when|who|why|if)\b|^show\s+audit',
        msg))
    if _is_exec_q:
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

    # -- List Contacts ---------------------------------------------------------
    if msg.startswith('list contacts:'):
        return routed(_compact({
            'mode':           'list',
            'pageNumber':     _val(chat_input.get('pageNumber')),
            'pageSize':       _val(chat_input.get('pageSize')),
            'search':         _val(chat_input.get('search')),
            'status':         _val(chat_input.get('status')),
            'accountId':      _val(chat_input.get('accountId')),
            'ownerId':        _val(chat_input.get('ownerId')),
            'dateFrom':       _val(chat_input.get('dateFrom')),
            'dateTo':         _val(chat_input.get('dateTo')),
            'deletedOnly':    True if chat_input.get('deletedOnly') is True else None,
            'includeDeleted': True if chat_input.get('includeDeleted') is True else None,
        }))

    # -- Search Contacts (legacy typeahead alias) ------------------------------
    if msg.startswith('search contacts:'):
        query = raw[len('search contacts:'):].strip()
        if len(query) >= 2:
            # Detect "role X" or "X role" → use p_role filter (partial LIKE match)
            _rsm = re.match(r'^role\s+(.+)$', query, re.IGNORECASE)
            if not _rsm:
                _rsm = re.match(r'^(.+?)\s+role$', query, re.IGNORECASE)
            if _rsm:
                role_val = _rsm.group(1).strip()
                logger.info(f'[search_contacts->role] role filter: {role_val}')
                return routed(_compact({'mode': 'list', 'role': role_val}))
            return routed(_compact({
                'mode':       'list',
                'search':     query,
                'pageSize':   _val(chat_input.get('pageSize')),
                'pageNumber': _val(chat_input.get('pageNumber')),
            }))
        return passthru()

    # -- Get Contact Detail ----------------------------------------------------
    if msg.startswith('get contact:') or msg.startswith('get details:'):
        contact_id      = _val(chat_input.get('contactId')) or _extract_uuid(raw)
        email           = _val(chat_input.get('email'))
        incl_deleted    = True if chat_input.get('includeDeleted') is True else None
        if contact_id or email:
            logger.info(f'[get_details] contactId={contact_id} email={email}')
            return routed(_compact({'mode': 'get_details', 'contactId': contact_id, 'email': email, 'includeDeleted': incl_deleted}))
        return passthru()

    # -- Create Contact --------------------------------------------------------
    if msg.startswith('create contact:'):
        if not chat_input.get('firstName') and not chat_input.get('lastName'):
            logger.warning('[create] Missing firstName and lastName - passthru')
            return passthru()
        return routed(_compact({
            'mode':            'create',
            'firstName':       _val(chat_input.get('firstName')),
            'lastName':        _val(chat_input.get('lastName')),
            'email':           _val(chat_input.get('email')),
            'phone':           _val(chat_input.get('phone')),
            'role':            _val(chat_input.get('role')),
            'status':          _val(chat_input.get('status')),
            'accountId':       _val(chat_input.get('accountId')),
            'ownerId':         _val(chat_input.get('ownerId')),
            'createdBy':       _val(chat_input.get('createdBy')),
            'isCustomer':      chat_input.get('isCustomer')      if isinstance(chat_input.get('isCustomer'), bool)      else None,
            'isEmailVerified': chat_input.get('isEmailVerified') if isinstance(chat_input.get('isEmailVerified'), bool) else None,
            'billingAddress':  chat_input.get('billingAddress')  or None,
            'shippingAddress': chat_input.get('shippingAddress') or None,
        }))

    # -- Update Contact --------------------------------------------------------
    if msg.startswith('update contact:'):
        contact_id = _val(chat_input.get('contactId')) or _extract_uuid(raw)
        if not contact_id:
            logger.warning('[update] Missing contactId - passthru')
            return passthru()
        return routed(_compact({
            'mode':            'update',
            'contactId':       contact_id,
            'firstName':       _val(chat_input.get('firstName')),
            'lastName':        _val(chat_input.get('lastName')),
            'email':           _val(chat_input.get('email')),
            'phone':           _val(chat_input.get('phone')),
            'role':            _val(chat_input.get('role')),
            'status':          _val(chat_input.get('status')),
            'accountId':       _val(chat_input.get('accountId')),
            'ownerId':         _val(chat_input.get('ownerId')),
            'updatedBy':       _val(chat_input.get('updatedBy')),
            'isCustomer':      chat_input.get('isCustomer')      if isinstance(chat_input.get('isCustomer'), bool)      else None,
            'isEmailVerified': chat_input.get('isEmailVerified') if isinstance(chat_input.get('isEmailVerified'), bool) else None,
            'billingAddress':  chat_input.get('billingAddress')  or None,
            'shippingAddress': chat_input.get('shippingAddress') or None,
        }))

    # -- Contact Activities ----------------------------------------------------
    if msg.startswith('contact activities:'):
        contact_id = _val(chat_input.get('contactId')) or _extract_uuid(raw)
        email      = _val(chat_input.get('email'))
        if contact_id or email:
            return routed(_compact({'mode': 'activities', 'contactId': contact_id, 'email': email}))
        return passthru()

    # -- Archive Contact -------------------------------------------------------
    if msg.startswith('archive contact:'):
        contact_id = _val(chat_input.get('contactId')) or _extract_uuid(raw)
        if contact_id:
            return routed(_compact({'mode': 'archive', 'contactId': contact_id}))
        return passthru()

    # -- Restore Contact -------------------------------------------------------
    if msg.startswith('restore contact:'):
        contact_id = _val(chat_input.get('contactId')) or _extract_uuid(raw)
        if contact_id:
            return routed(_compact({'mode': 'restore', 'contactId': contact_id}))
        return passthru()

    # -- Find Duplicates -------------------------------------------------------
    if msg.startswith('contact duplicates:'):
        return routed({'mode': 'duplicates'})

    # -- Summary / Statistics --------------------------------------------------
    if msg.startswith('contact summary:'):
        return routed({'mode': 'summary'})

    # -- Merge Contacts --------------------------------------------------------
    if msg.startswith('merge contacts:'):
        operation = _val(chat_input.get('operation'))
        email     = _val(chat_input.get('email'))
        phone     = _val(chat_input.get('phone'))
        if operation and (email or phone):
            return routed(_compact({'mode': 'merge', 'operation': operation, 'email': email, 'phone': phone}))
        return passthru()

    # -- Send Email Verification -----------------------------------------------
    if msg.startswith('send verification:'):
        contact_id          = _val(chat_input.get('contactId')) or _extract_uuid(raw)
        email               = _val(chat_input.get('email'))
        token_expires_hours = _val(chat_input.get('tokenExpiresHours'))
        if contact_id or email:
            logger.info(f'[send_verification] contactId={contact_id} email={email}')
            return routed(_compact({
                'mode':              'send_verification',
                'contactId':         contact_id,
                'email':             email,
                'tokenExpiresHours': token_expires_hours,
            }))
        logger.warning('[send_verification] no identifier - passthru')
        return passthru()

    # -- Verify Email Token ----------------------------------------------------
    if msg.startswith('verify email:'):
        token      = _val(chat_input.get('token'))
        contact_id = _val(chat_input.get('contactId')) or _extract_uuid(raw)
        if token:
            logger.info(f'[verify_email] token={str(token)[:8]}...')
            return routed(_compact({'mode': 'verify_email', 'token': token, 'contactId': contact_id}))
        logger.warning('[verify_email] no token - passthru')
        return passthru()

    # ===========================================================================
    # NL SHORTCUTS (v3.2) -- safety net for any remaining sendRequest() NL calls
    # These catch natural-language button messages before they reach Ollama.
    # ===========================================================================

    # "Send verification email to contact ID {UUID}"
    if 'send verification email' in msg or 'verification email' in msg:
        contact_id = _val(chat_input.get('contactId')) or _extract_uuid(raw)
        email      = _val(chat_input.get('email'))
        if contact_id or email:
            logger.info(f'[NL->send_verification] contactId={contact_id} email={email}')
            return routed(_compact({
                'mode':      'send_verification',
                'contactId': contact_id,
                'email':     email,
            }))
        logger.warning('[NL->send_verification] no identifier - passthru')
        return passthru()

    # "Show all contacts" / "View contacts" / "list all contacts"
    # Anchored to end-of-string so "show contacts with role X" is NOT caught here.
    if re.match(r'^(?:show|view|list|display)\s+(?:all\s+)?contacts?\s*$', msg):
        logger.info('[NL->list] all contacts')
        return routed({'mode': 'list'})

    # "Show archived contacts"
    if 'archived contacts' in msg or 'show archived' in msg or 'view archived' in msg:
        logger.info('[NL->list] archived contacts')
        return routed({'mode': 'list', 'deletedOnly': True})

    # "Show duplicate contacts for Chen" — SP can't filter duplicates by name;
    # route to list search so the user sees matching contacts instead.
    _dupes_for_m = re.match(
        r'^(?:show|find|check|display)?\s*(?:duplicate\s+contacts?|duplicates)\s+for\s+(.+)$',
        raw, re.IGNORECASE,
    )
    if _dupes_for_m:
        term = _dupes_for_m.group(1).strip()
        if len(term) >= 2:
            logger.info(f'[NL->search] duplicates-for: searching contacts named {term!r}')
            return routed(_compact({'mode': 'list', 'search': term}))

    # "Show duplicate contacts report" / "Check duplicates"
    if any(p in msg for p in (
        'duplicate contacts', 'duplicates report', 'check duplicates',
        'find duplicates', 'show duplicates',
    )):
        logger.info('[NL->duplicates]')
        return routed({'mode': 'duplicates'})

    # "Show contact statistics" / "Contact statistics/summary"
    if any(p in msg for p in (
        'contact statistics', 'contact summary', 'contact stats',
        'show statistics', 'show contact stats',
    )):
        logger.info('[NL->summary]')
        return routed({'mode': 'summary'})

    # NOTE: sp_contacts list mode filters by p_role using an EXACT (trimmed,
    # case-insensitive) match — LOWER(TRIM(role)) = LOWER(TRIM(p_role)). Pass a
    # full role string (e.g. "VP Sales", "Sales Manager"); partial words like
    # "Manager" won't match. Free-text/partial role lookups should use p_search.

    # "Find contacts at/for/in X" — company name lookup.
    # Searching by company/account name requires the AI to resolve the accountId first;
    # sp_contacts p_search only matches contact-level fields (name, email, phone).
    # Route "at/in/from <company>" as passthru so the AI can handle it properly.
    # "named/with name" queries search the contact's own name directly.
    _contacts_named_m = re.match(
        r'^(?:find|search|show|list)\s+contacts?\s+'
        r'(?:named?|with\s+name)\s+(.+)$',
        raw, re.IGNORECASE
    )
    if _contacts_named_m:
        term = _contacts_named_m.group(1).strip()
        if len(term) >= 2:
            logger.info(f'[NL->search] contacts named: {term}')
            return routed(_compact({'mode': 'list', 'search': term}))

    # "Search by gmail.com email" / "Search contacts with gmail.com email"
    # "Find contacts with gmail.com email"
    _email_domain_m = re.match(
        r'^(?:find|search|show|list)(?:\s+contacts?)?\s+'
        r'(?:by|with|having|using)\s+(?:email\s+)?([A-Za-z0-9._%-]+\.[a-z]{2,})\s*(?:email)?',
        raw, re.IGNORECASE
    )
    if _email_domain_m:
        term = _email_domain_m.group(1).strip()
        if len(term) >= 3:
            logger.info(f'[NL->search] email domain: {term}')
            return routed(_compact({'mode': 'list', 'search': term}))

    # "Show activity timeline for X" / "Activities for X"
    # Route directly to activities mode so the timeline is shown immediately.
    _activities_m = re.match(
        r'^(?:show|get|view|display)?\s*(?:activity\s+timeline|activities|recent\s+activities?|email\s+activity)\s+'
        r'(?:for|of|about)\s+(.+)$',
        raw, re.IGNORECASE
    )
    if _activities_m:
        term = _activities_m.group(1).strip()
        parts = term.split()
        if len(parts) >= 2:
            logger.info(f'[NL->activities] activities for full name: {term}')
            return routed(_compact({'mode': 'activities', 'firstName': parts[0], 'lastName': ' '.join(parts[1:])}))
        if len(term) >= 2:
            logger.info(f'[NL->search] activities for partial name: {term}')
            return routed(_compact({'mode': 'list', 'search': term}))

    # "Show details for X" / "Get details for X" / "Contact details for X"
    # Full name (2+ words) → get_details exact match; partial → list search.
    _details_m = re.match(
        r'^(?:show|get|view|display|contact)\s+(?:full\s+)?(?:details?|info|profile|record)\s+'
        r'(?:for|of|about)\s+(.+)$',
        raw, re.IGNORECASE
    )
    if _details_m:
        term = _details_m.group(1).strip()
        parts = term.split()
        if len(parts) >= 2:
            logger.info(f'[NL->get_details] details for full name: {term}')
            return routed(_compact({'mode': 'get_details', 'firstName': parts[0], 'lastName': ' '.join(parts[1:])}))
        if len(term) >= 2:
            logger.info(f'[NL->search] details for partial name: {term}')
            return routed(_compact({'mode': 'list', 'search': term}))

    # "Show unverified contacts" / "List unverified contacts"
    if any(p in msg for p in ('unverified contacts', 'not verified', 'unverified email')):
        logger.info('[NL->list] unverified contacts filter')
        return routed(_compact({'mode': 'list', 'isEmailVerified': False}))

    # "Show verified contacts" / "List verified contacts" (guard against 'unverified')
    if 'unverified' not in msg and any(p in msg for p in ('verified contacts', 'verified email')):
        logger.info('[NL->list] verified contacts filter')
        return routed(_compact({'mode': 'list', 'isEmailVerified': True}))

    # "Inactive contacts" / "List inactive contacts"
    if any(p in msg for p in ('inactive contacts', 'list inactive', 'show inactive contacts')):
        logger.info('[NL->list] inactive contacts')
        return routed(_compact({'mode': 'list', 'status': 'inactive'}))

    # "Active contacts" / "Show active contacts"
    if any(p in msg for p in ('active contacts', 'show active', 'list active contacts')):
        logger.info('[NL->list] active contacts')
        return routed(_compact({'mode': 'list', 'status': 'active'}))

    # "Show contacts with status inactive/active" — explicit status= phrase
    _status_m = re.search(r'\bwith\s+(?:the\s+)?status\s+(active|inactive)\b', msg)
    if _status_m:
        status_val = _status_m.group(1).lower()
        logger.info(f'[NL->list] with-status filter: {status_val}')
        return routed(_compact({'mode': 'list', 'status': status_val}))

    # "Contacts with no account" / "Contacts without an account"
    if any(p in msg for p in ('no account', 'without account', 'without an account', 'contacts per account')):
        logger.info('[NL->summary] contacts per account query')
        return routed({'mode': 'summary'})

    # "Contacts with role Manager" / "Show contacts with role Director"
    _role_m = re.search(
        r'\bwith\s+(?:the\s+)?role\s+([A-Za-z][A-Za-z\s\-]{0,39}?)\s*$',
        raw, re.IGNORECASE
    )
    if _role_m:
        role_val = _role_m.group(1).strip()
        if role_val:
            logger.info(f'[NL->list] role filter: {role_val}')
            return routed(_compact({'mode': 'list', 'role': role_val}))

    # "Show Manager contacts" / "List Director contacts" / "Find VP contacts"
    _STATUS_WORDS = {'all', 'active', 'inactive', 'archived', 'unverified', 'duplicate', 'new', 'deleted'}
    _role_prefix_m = re.match(
        r'^(?:show|list|find|display|get)\s+([A-Za-z][A-Za-z\s\-]{0,39}?)\s+contacts?\s*$',
        raw, re.IGNORECASE
    )
    if _role_prefix_m:
        role_val = _role_prefix_m.group(1).strip()
        if role_val.lower() not in _STATUS_WORDS and len(role_val) >= 2:
            logger.info(f'[NL->list] role prefix filter: {role_val}')
            return routed(_compact({'mode': 'list', 'role': role_val}))

    # "Archive contact Tom Williams" — archive by name: look up first; SP requires UUID
    _archive_m = re.match(
        r'^(?:archive|soft.?delete|deactivate)\s+(?:contact\s+)?(.+)$',
        raw, re.IGNORECASE,
    )
    if _archive_m:
        term = _archive_m.group(1).strip()
        parts = term.split()
        if len(parts) >= 2:
            logger.info(f'[NL->get_details] archive intent for: {term!r}')
            return routed(_compact({'mode': 'get_details', 'firstName': parts[0], 'lastName': ' '.join(parts[1:])}))
        if len(term) >= 2:
            logger.info(f'[NL->search] archive intent partial: {term!r}')
            return routed(_compact({'mode': 'list', 'search': term}))

    # "Restore archived contact Sarah Brown" — list archived contacts filtered by name
    _restore_m = re.match(
        r'^(?:restore|unarchive|reactivate)\s+(?:archived\s+)?(?:contact\s+)?(.+)$',
        raw, re.IGNORECASE,
    )
    if _restore_m:
        term = _restore_m.group(1).strip()
        if len(term) >= 2:
            logger.info(f'[NL->list] restore intent for: {term!r}')
            return routed(_compact({'mode': 'list', 'deletedOnly': True, 'search': term}))

    # ── Vague UI-form intents → emit a marker so the frontend opens the
    # inline form instead of bouncing to the AI for a list of required
    # fields. Each detector skips itself when the user already supplied a
    # UUID (so the AI/SP can act directly).
    # IMPORTANT: placed BEFORE _find_m and bare-name fallback so chip phrases
    # like "Create a new contact" / "Edit contact information" don't get caught
    # by the broad name-search patterns below.
    _has_uuid = bool(_extract_uuid(raw))

    # Create contact (no UUID).
    if not _has_uuid and (
        re.search(r'\b(create|new|add|make)\b.*\bcontact', msg)
        or re.search(r'\bcreate\s+or\s+update\s+contact', msg)
        or re.match(r'^\s*(create|new|add)\s+contact', msg)
    ):
        return routed({'mode': 'show_contact_form'})

    # Update/edit contact (no UUID) — opens the contact search bar.
    # Catches "update phone/email/address/name for [person]" as well as
    # the explicit "update/edit contact" phrases.
    # Extracts the contact name (if present) so the form can pre-populate its search.
    if not _has_uuid and (
        re.search(r'\b(update|edit)\b.*\bcontact', msg) or
        re.search(r'\b(update|change|edit)\b.*\b(phone|email|address|name|role|status)\b', msg)
    ):
        _for_name_m = re.search(r'\bfor\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*$', raw)
        params = {'mode': 'show_contact_update_form'}
        if _for_name_m:
            params['prefillSearch'] = _for_name_m.group(1).strip()
            logger.info(f'[NL->show_contact_update_form] prefillSearch={params["prefillSearch"]!r}')
        return routed(params)

    # "find Steven" / "search for Emily" / "look up John Smith" etc.
    # Catches voice queries that include an action verb before the name.
    # NOTE: placed after the more-specific patterns above so "find contacts at X"
    # and "show details for X" are already handled before reaching here.
    _find_m = re.match(
        r'^(?:find|search(?:\s+for)?|look(?:\s+up)?(?:\s+for)?|'
        r'show(?:\s+me)?|get|display|pull\s+up)\s+(.+)$',
        raw, re.IGNORECASE
    )
    if _find_m:
        term = _find_m.group(1).strip()
        # Skip if term looks like a multi-word instruction: starts OR ends with
        # "contacts", contains a detail/profile prefix, or contains "role"/"with"
        # (e.g. "Manager contacts" or "contacts with role Manager").
        _skip_terms = re.match(
            r'^(?:all\s+)?contacts?|details?\s+for|info\s+(?:for|about)|'
            r'profile\s+(?:for|of)|.*\brole\b|.*\s+contacts?\s*$',
            term, re.IGNORECASE
        )
        if not _skip_terms and len(term) >= 2:
            logger.info(f'[NL->search] verb+name: {term}')
            return routed(_compact({'mode': 'list', 'search': term}))

    # Bare name — voice STT outputs just "Steven" or "John Smith" with no verb.
    # Match 1–4 words made of letters/apostrophes/hyphens; skip reserved words
    # and any multi-word phrase containing prepositions/command words (which
    # would indicate a structured query, not a name).
    _RESERVED = {
        'contacts', 'contact', 'list', 'all', 'show', 'find', 'search',
        'view', 'archive', 'archived', 'duplicate', 'duplicates', 'merge',
        'activities', 'summary', 'statistics', 'report', 'home', 'back',
        'create', 'add', 'update', 'edit', 'new', 'make',
    }
    _STOP_WORDS = {'with', 'role', 'for', 'at', 'in', 'by', 'from', 'and', 'the', 'of', 'or'}
    _msg_words   = set(msg.split())
    _first_word  = msg.split()[0] if msg else ''
    if re.match(r"^[A-Za-z][A-Za-z'\-]{1,}(?:\s+[A-Za-z][A-Za-z'\-]*){0,3}$", raw) \
            and msg not in _RESERVED \
            and _first_word not in _RESERVED \
            and not _msg_words.intersection(_STOP_WORDS):
        logger.info(f'[NL->search] bare name: {raw}')
        return routed(_compact({'mode': 'list', 'search': raw}))

    # -- No match -- AI Agent handles ------------------------------------------
    return passthru()
