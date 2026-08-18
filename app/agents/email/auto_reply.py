"""Autonomous inbound email classifier and auto-reply composer for EmailAgent.

Flow:
  1. Classifier determines intent from sender/subject/body.
  2. For auto-reply-eligible intents the LLM composes a contextual reply.
  3. Reply is sent via SMTP and logged to audit_log.

Intent categories:
  general_inquiry  — greetings, "are you open?", generic questions  → AUTO-REPLY
  support_request  — specific product/service help needed            → AUTO-REPLY
  spam / no_reply  — automated, bounce, noreply senders             → SKIP
  complaint        — escalate (future: alert to relevant agent)      → SKIP for now
  unknown          — insufficient signal                             → SKIP
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Senders we never reply to ─────────────────────────────────────────────────
_SKIP_SENDER_PATTERNS = re.compile(
    r'(no.?reply|noreply|mailer.daemon|postmaster|bounce|auto.?reply'
    r'|donotreply|do.not.reply|support\+|notifications?\+)',
    re.IGNORECASE,
)

# ── Per-sender reply rate-limit: max 1 auto-reply per sender per hour ─────────
_replied_this_hour: Dict[str, float] = {}   # sender_email → last_reply_epoch
_RATE_LIMIT_SECS = 3600


def _extract_email_addr(raw: str) -> str:
    """Pull bare email from 'Display Name <email@domain>'."""
    m = re.search(r'<([^>]+)>', raw)
    return (m.group(1) if m else raw).strip().lower()


def _extract_first_name(raw: str) -> str:
    """Best-effort first name from display name or local part."""
    m = re.match(r'^"?([A-Za-z]+)', raw.strip())
    if m:
        return m.group(1)
    local = _extract_email_addr(raw).split('@')[0]
    return local.split('.')[0].capitalize() if local else 'there'


def _is_our_own_mail(sender: str, own_address: str) -> bool:
    """Is this message from US, under any hostname the mail system uses?

    THE LOOP THIS CLOSES. The check was `sender == own_address.lower()`, an
    exact match against EMAIL_ADDRESS (info@agentorc.ca). Every outbound message
    BCCs the archive mailbox, and the server delivers those copies with the
    From rewritten to the transport host — info@MAIL.agentorc.ca. One character
    of difference, so the guard missed it and the poller answered our own
    transactional mail: 37 auto-replies to our own payment reminders and order
    confirmations, found 2026-08-17.

    Only the per-sender hour rate-limit stopped it going further, and that lives
    in a module-level dict — it resets on every restart and is per-replica, so
    it is a backstop, not a guard.

    Matching on the LOCAL PART plus our own domain (or any subdomain of it)
    recognises the same mailbox however the transport labels it.
    """
    sender = (sender or '').lower()
    own = (own_address or '').lower()
    if not sender or not own:
        return False
    if sender == own:
        return True
    s_local, _, s_domain = sender.partition('@')
    o_local, _, o_domain = own.partition('@')
    if not (s_local and o_local and s_domain and o_domain):
        return False
    return s_local == o_local and (s_domain == o_domain
                                   or s_domain.endswith('.' + o_domain))


def should_skip(email: Dict[str, Any], own_address: str) -> Optional[str]:
    """Return a skip reason string, or None if the email should be processed."""
    sender = _extract_email_addr(email.get('from', ''))

    if not sender:
        return 'own address'
    if _is_our_own_mail(sender, own_address):
        return f'own address ({sender})'

    # RFC 3834: an automatic responder must not answer automatic mail. Honouring
    # these headers stops a loop with ANY correct responder, not just our own —
    # a vacation autoresponder on the far end would otherwise ping-pong with us
    # until one side's rate limit happened to bite.
    auto = str(email.get('auto_submitted') or '').strip().lower()
    if auto and auto != 'no':
        return f'Auto-Submitted: {auto}'
    prec = str(email.get('precedence') or '').strip().lower()
    if prec in ('bulk', 'auto_reply', 'junk', 'list'):
        return f'Precedence: {prec}'

    if _SKIP_SENDER_PATTERNS.search(sender):
        return f'skip-sender pattern matched ({sender})'

    # Rate-limit: one auto-reply per sender per hour
    last = _replied_this_hour.get(sender, 0)
    if time.time() - last < _RATE_LIMIT_SECS:
        return f'rate-limited ({sender})'

    return None


def classify_intent(email: Dict[str, Any]) -> str:
    """Rule-based intent classifier — fast, no LLM call needed for common cases."""
    subject = (email.get('subject') or '').lower()
    body    = (email.get('body_text') or email.get('preview') or '').lower()
    text    = subject + ' ' + body

    # Complaint signals
    if any(w in text for w in ('complaint', 'unhappy', 'terrible', 'refund', 'angry', 'lawsuit')):
        return 'complaint'

    # Unsubscribe
    if any(w in text for w in ('unsubscribe', 'remove me', 'opt out', 'opt-out')):
        return 'unsubscribe'

    # Support / product questions
    if any(w in text for w in ('help', 'support', 'issue', 'problem', 'bug', 'error',
                                'how do i', 'how to', 'question', 'invoice', 'billing',
                                'account', 'password', 'login', 'sign in', 'access')):
        return 'support_request'

    # General inquiry / greetings
    if any(w in text for w in ('hello', 'hi ', 'hey ', 'good morning', 'good afternoon',
                                'good evening', 'are you open', 'open?', 'contact',
                                'information', 'inquiry', 'enquiry', 'interested',
                                'learn more', 'pricing', 'demo', 'trial', 'about')):
        return 'general_inquiry'

    return 'unknown'


def compose_reply(email: Dict[str, Any], intent: str) -> Optional[Dict[str, str]]:
    """
    Compose an auto-reply using the LLM.
    Returns {'subject': ..., 'body_html': ..., 'body_text': ...} or None on failure.
    """
    from app.core.graph_utils import _get_llm

    sender_raw  = email.get('from', '')
    first_name  = _extract_first_name(sender_raw)
    orig_subject = email.get('subject', '(no subject)')
    orig_body    = (email.get('body_text') or email.get('preview') or '').strip()[:600]

    # PII minimization: the customer's raw text goes to the LLM masked
    # (emails/phones/card-like runs) — kill switch PII_MASK_ENABLED. The KB
    # retrieval below intentionally uses the RAW text (internal DB search).
    try:
        from app.core import privacy
        masked_subject = privacy.mask(orig_subject)
        masked_body = privacy.mask(orig_body)
    except Exception:
        masked_subject, masked_body = orig_subject, orig_body

    intent_guidance = {
        'general_inquiry': (
            "The sender has a general question or greeting. "
            "Confirm we are active and open. Briefly explain what Conscestra CRM / Agentorc.ca does "
            "(AI-powered CRM with 12 cooperating AI Agents, fully auditable, built in Canada). "
            "Invite them to ask any specific questions."
        ),
        'support_request': (
            "The sender needs help or has a support question. "
            "Acknowledge their request warmly, let them know a team member will follow up, "
            "and provide our email info@agentorc.ca for direct contact. "
            "Briefly mention our AI Agent platform capabilities."
        ),
    }

    guidance = intent_guidance.get(intent, intent_guidance['general_inquiry'])

    # Context hydration: if the sender is a known contact/lead, give the LLM
    # their compact CRM pack so the reply is personalized — a known customer
    # with an open overdue invoice or a running cadence gets acknowledged as
    # such, not greeted like a stranger. Best-effort; kill switch
    # CONTEXT_HYDRATION_ENABLED.
    crm_block = ""
    try:
        from app.core import context as crm_context
        crm_block = crm_context.render_for_email(_extract_email_addr(sender_raw))
    except Exception as exc:
        logger.debug(f"context hydration skipped for auto-reply: {exc}")

    # Knowledge loop (RAG): ground the reply in APPROVED knowledge-base
    # answers when the message matches one — every resolved case makes the
    # next auto-reply smarter. Best-effort; kill switch KB_RAG_ENABLED.
    kb_block = ""
    try:
        from app.core import knowledge
        # Real subject stays in the query (it's content, unlike the fixed
        # channel labels); a KB miss is logged as an 'email' gap.
        kb_block = knowledge.rag_block(orig_subject, orig_body,
                                       gap_channel="email")
    except Exception as exc:
        logger.debug(f"knowledge retrieval skipped for auto-reply: {exc}")

    system_prompt = (
        "You are the EmailAgent for Conscestra CRM / Agentorc.ca — a Canadian AI orchestration platform. "
        "You write concise, warm, professional email replies on behalf of the Conscestra CRM team. "
        "RULES: Under 150 words. Plain, friendly tone. No jargon. No markdown in the plain-text version. "
        "Always sign as: The Conscestra CRM Team | info@agentorc.ca | agentorc.ca"
    )

    # Multilingual (blindspot #2): reply in the language the customer wrote in
    # (Canadian bilingual first-class). Detected from the RAW body — deterministic,
    # no cost — grounded in the same approved knowledge; kill switch inside.
    try:
        from app.core import language
        system_prompt += language.respond_in(orig_body or orig_subject)
    except Exception as exc:
        logger.debug(f"language directive skipped: {exc}")

    user_prompt = (
        f"Write an auto-reply email.\n\n"
        f"Recipient first name: {first_name}\n"
        f"Original subject: {masked_subject}\n"
        f"Original message snippet:\n{masked_body}\n\n"
        f"Intent: {intent}\n"
        f"Guidance: {guidance}\n\n"
        + (f"Internal CRM context about this sender (use it ONLY to make the tone "
           f"and content appropriate — e.g. thank a long-standing customer, "
           f"acknowledge an ongoing conversation. NEVER quote, reveal, or hint at "
           f"internal scores, churn risk, financial balances, or process names):\n"
           f"{crm_block}\n\n" if crm_block else "")
        + (f"Approved knowledge-base guidance that matches this message — base the "
           f"SUBSTANCE of your reply on it (adapt the wording, keep it accurate):\n"
           f"{kb_block}\n\n" if kb_block else "")
        + f"Return ONLY the email body text (no subject line, no 'Hi' prefix — start with the greeting). "
          f"Keep it under 120 words."
    )

    try:
        llm      = _get_llm(tier="lite")
        response = llm.invoke([
            {'role': 'system',  'content': system_prompt},
            {'role': 'user',    'content': user_prompt},
        ])
        body_text = response.content.strip() if hasattr(response, 'content') else str(response).strip()
    except Exception as exc:
        logger.error(f"LLM compose_reply failed: {exc}", exc_info=True)
        # Fallback template
        body_text = (
            f"Hi {first_name},\n\n"
            "Thank you for reaching out to Conscestra CRM / Agentorc.ca!\n\n"
            "Yes, we are open and active. Conscestra CRM is an AI-powered CRM platform featuring "
            "12 cooperating AI Agents — fully auditable and built in Canada.\n\n"
            "We'd love to help. Please reply to this email with any questions and "
            "a team member will follow up promptly.\n\n"
            "The Conscestra CRM Team\ninfo@agentorc.ca\nhttps://agentorc.ca"
        )

    # Wrap plain text into simple HTML
    html_paragraphs = ''.join(
        f'<p style="margin:0 0 0.75em;">{line}</p>'
        for line in body_text.split('\n') if line.strip()
    )
    body_html = f"""
<html><body style="font-family:Arial,sans-serif;color:#1a202c;max-width:600px;margin:auto;padding:2rem;font-size:0.95rem;line-height:1.6;">
{html_paragraphs}
<p style="margin-top:1.5rem;font-size:0.85rem;color:#718096;">
  <a href="https://agentorc.ca" style="color:#0d9488;">agentorc.ca</a>
</p>
</body></html>
"""

    reply_subject = orig_subject if orig_subject.lower().startswith('re:') \
        else f'Re: {orig_subject}'

    return {
        'subject':   reply_subject,
        'body_text': body_text,
        'body_html': body_html,
    }


_SENT_POS = {"thanks", "thank", "great", "love", "excellent", "happy", "awesome",
             "perfect", "appreciate", "good", "resolved", "wonderful", "fantastic",
             "helpful", "pleased", "amazing", "smooth", "easy", "recommend"}
_SENT_NEG = {"angry", "disappointed", "terrible", "awful", "cancel", "refund",
             "broken", "bug", "error", "unacceptable", "frustrated", "poor", "worst",
             "complaint", "delay", "slow", "fail", "failed", "issue", "problem",
             "unhappy", "wrong", "useless", "disappointing", "ridiculous", "annoyed"}


def score_sentiment(text: str):
    """Lightweight lexicon sentiment → (score in [-1,1], label). No LLM/cost."""
    import re
    words = re.findall(r"[a-z']+", (text or "").lower())
    p = sum(1 for w in words if w in _SENT_POS)
    n = sum(1 for w in words if w in _SENT_NEG)
    if p == 0 and n == 0:
        return 0.0, "neutral"
    score = round((p - n) / (p + n), 3)
    label = "positive" if score > 0.2 else ("negative" if score < -0.2 else "neutral")
    return score, label


def store_inbound_sentiment(email: Dict[str, Any], intent: str = None) -> None:
    """Score + persist an inbound email's sentiment (best-effort)."""
    from app.core.database import get_connection
    try:
        text = (str(email.get("subject", "")) + " " +
                str(email.get("body", "") or email.get("body_text", "") or "")).strip()
        score, label = score_sentiment(text)
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO email_sentiment (from_addr, subject, score, label, intent) "
                "VALUES (%s,%s,%s,%s,%s)",
                (_extract_email_addr(email.get("from", "")), str(email.get("subject", ""))[:300],
                 score, label, intent))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"email sentiment store failed: {exc}")


def _ingest_attachments(email: Dict[str, Any]) -> None:
    """KNOWN sender's document attachments → governed KB article proposals
    (kb_ingest: idempotent per content hash, ≤3 proposals per document per
    pass, human-approved before anything publishes). Unknown senders are
    skipped entirely — their files never reach the LLM. Best-effort,
    backgrounded; a failure never touches the reply path.
    Kill switch: KB_ATTACH_INGEST=0."""
    import os as _os
    if _os.getenv("KB_ATTACH_INGEST", "1").strip().lower() not in (
            "1", "true", "yes", "on"):
        return
    atts = email.get("_attachments") or []
    if not atts:
        return
    try:
        from app.agents.email.inbound_bridge import _resolve_sender_sync
        import re as _re
        m = _re.search(r"<([^>]+)>", email.get("from", ""))
        sender = (m.group(1) if m else email.get("from", "")).strip().lower()
        if not _resolve_sender_sync(sender):
            logger.info(f"[attach] {len(atts)} attachment(s) from unknown "
                        f"sender {sender!r} — not ingested")
            return
    except Exception as exc:
        logger.debug(f"[attach] sender check failed (skip ingest): {exc}")
        return

    def _run():
        from app.core import kb_ingest
        for a in atts:
            try:
                text = kb_ingest.extract_text(a["filename"], a["data"])
                res = kb_ingest.ingest(a["filename"], text, cap=3)
                logger.info(f"[attach] {a['filename']}: "
                            f"{len(res.get('proposed') or [])} proposal(s), "
                            f"{res.get('already_processed', 0)} already known")
            except Exception as exc:
                logger.info(f"[attach] {a['filename']} skipped: {exc}")

    import threading
    threading.Thread(target=_run, daemon=True).start()


def process_inbound_email(email: Dict[str, Any], own_address: str) -> bool:
    """
    Classify, compose, and send an auto-reply for a single inbound email.
    Returns True if a reply was sent, False otherwise.
    """
    from app.agents.email.smtp_imap import send_email
    from app.core.database import get_connection
    import json as _json

    # A message with no parseable sender is not a customer touch (malformed
    # mail, or an error entry that leaked through) — never classify, bridge,
    # or reply to it.
    if not _extract_email_addr(email.get('from', '')):
        logger.debug("Inbound skipped: no parseable sender address")
        return False

    skip_reason = should_skip(email, own_address)
    # Rate-limited senders are still REAL customer touches — only own-address /
    # noreply-pattern mail bypasses the CRM bridge entirely.
    if skip_reason and not skip_reason.startswith('rate-limited'):
        logger.debug(f"Auto-reply skipped: {skip_reason}")
        return False

    intent = classify_intent(email)
    logger.info(f"Inbound email intent={intent!r} from={email.get('from','')!r} subject={email.get('subject','')!r}")

    # Bridge into the CRM: inbound activity on the matched contact/lead +
    # email.received bus event. This is what makes cadence exits ('engaged' /
    # 're-engaged'), campaign reply attribution, and engagement recency real.
    try:
        from app.agents.email.inbound_bridge import record_inbound
        record_inbound(email, intent)
    except Exception as exc:
        logger.warning(f"inbound bridge failed (non-fatal): {exc}")

    # Capture customer-voice sentiment for EVERY inbound email (feeds the
    # executive snapshot) — independent of whether we auto-reply.
    store_inbound_sentiment(email, intent)

    # Zero-manual-data-entry: a KNOWN sender's document attachments flow into
    # the governed KB pipeline (kb_ingest — idempotent by content hash, capped,
    # every article human-approved). Strangers' files are ignored: LLM-cost and
    # spam control. Background — never delays the reply path.
    _ingest_attachments(email)

    if skip_reason:
        logger.debug(f"Auto-reply skipped: {skip_reason}")
        return False

    if intent not in ('general_inquiry', 'support_request'):
        logger.info(f"Intent '{intent}' does not trigger auto-reply.")
        return False

    # Live human takeover (blindspot #1): if a rep currently owns this sender's
    # conversation in the agent console, the AI stands down — it must never talk
    # over the human who took the wheel. Fail-open on any error.
    try:
        from app.core.agent_console import is_human_handled
        if is_human_handled('email', _extract_email_addr(email.get('from', ''))):
            logger.info("Auto-reply suppressed: conversation is human-handled.")
            return False
    except Exception as exc:
        logger.debug(f"human-handled check skipped: {exc}")

    reply = compose_reply(email, intent)
    if not reply:
        return False

    sender_addr = _extract_email_addr(email.get('from', ''))
    result = send_email(
        to=sender_addr,
        subject=reply['subject'],
        body_html=reply['body_html'],
        body_text=reply['body_text'],
        # Declare this as an automatic reply so nothing answers it — the third
        # leg of the loop fix, alongside recognising our own address under any
        # hostname and honouring these same headers on inbound mail.
        auto_replied=True,
    )

    if result.get('success'):
        # Record rate-limit timestamp
        _replied_this_hour[sender_addr] = time.time()
        logger.info(f"Auto-reply sent to {sender_addr} | subject={reply['subject']!r}")

        # Log to audit_log
        try:
            conn = get_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.audit_log (entity, entity_id, action, payload, created_at) "
                    "VALUES ('email', gen_random_uuid(), 'auto_reply_sent', %s::jsonb, now())",
                    (_json.dumps({
                        'to':      sender_addr,
                        'subject': reply['subject'],
                        'intent':  intent,
                        'trigger': 'imap_poller',
                    }),),
                )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"audit_log insert failed: {exc}")

        # Unified customer memory: a KNOWN sender's exchange is remembered
        # (background) so their next contact on ANY channel has this context.
        try:
            from app.agents.email.inbound_bridge import _resolve_sender_sync
            from app.core import customer_memory
            who = _resolve_sender_sync(sender_addr.lower())
            if who:
                customer_memory.remember_later(
                    who["kind"],
                    who.get("account_id") or who.get("lead_id"),
                    "email",
                    f"email:{email.get('message_id') or sender_addr}:{int(time.time())}",
                    f"customer (subject: {email.get('subject', '')}): "
                    f"{str(email.get('body_text') or email.get('preview') or '')[:1500]}\n"
                    f"agent reply: {reply['body_text'][:1000]}")
        except Exception as exc:
            logger.debug(f"memory write skipped for auto-reply: {exc}")

        return True

    logger.warning(f"Auto-reply SMTP failed for {sender_addr}: {result.get('message')}")
    return False
