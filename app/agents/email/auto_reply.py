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


def should_skip(email: Dict[str, Any], own_address: str) -> Optional[str]:
    """Return a skip reason string, or None if the email should be processed."""
    sender = _extract_email_addr(email.get('from', ''))

    if not sender or sender == own_address.lower():
        return 'own address'

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
        kb_block = knowledge.rag_block(orig_subject, orig_body)
    except Exception as exc:
        logger.debug(f"knowledge retrieval skipped for auto-reply: {exc}")

    system_prompt = (
        "You are the EmailAgent for Conscestra CRM / Agentorc.ca — a Canadian AI orchestration platform. "
        "You write concise, warm, professional email replies on behalf of the Conscestra CRM team. "
        "RULES: Under 150 words. Plain, friendly tone. No jargon. No markdown in the plain-text version. "
        "Always sign as: The Conscestra CRM Team | info@agentorc.ca | agentorc.ca"
    )

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
        llm      = _get_llm()
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


def process_inbound_email(email: Dict[str, Any], own_address: str) -> bool:
    """
    Classify, compose, and send an auto-reply for a single inbound email.
    Returns True if a reply was sent, False otherwise.
    """
    from app.agents.email.smtp_imap import send_email
    from app.core.database import get_connection
    import json as _json

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

    if skip_reason:
        logger.debug(f"Auto-reply skipped: {skip_reason}")
        return False

    if intent not in ('general_inquiry', 'support_request'):
        logger.info(f"Intent '{intent}' does not trigger auto-reply.")
        return False

    reply = compose_reply(email, intent)
    if not reply:
        return False

    sender_addr = _extract_email_addr(email.get('from', ''))
    result = send_email(
        to=sender_addr,
        subject=reply['subject'],
        body_html=reply['body_html'],
        body_text=reply['body_text'],
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

        return True

    logger.warning(f"Auto-reply SMTP failed for {sender_addr}: {result.get('message')}")
    return False
