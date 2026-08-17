"""SMTP and IMAP utilities for EmailAgent.

Credentials are loaded from environment variables:
  EMAIL_ADDRESS       — info@agentorc.ca
  EMAIL_PASSWORD      — SMTP/IMAP password
  EMAIL_SMTP_HOST     — mail.agentorc.ca
  EMAIL_SMTP_PORT     — 465
  EMAIL_IMAP_HOST     — mail.agentorc.ca
  EMAIL_IMAP_PORT     — 993
"""

from __future__ import annotations

import email as email_lib
import imaplib
import logging
import os
import smtplib
import socket
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

def _cfg(key: str, default: str = '') -> str:
    """Read env var at call time so Railway vars are always current."""
    return os.environ.get(key, default)

EMAIL_ADDRESS  = os.environ.get('EMAIL_ADDRESS',   'info@agentorc.ca')
BCC_ADDRESS    = os.environ.get('EMAIL_BCC',        'info@agentorc.ca')

def _email_address() -> str:  return os.environ.get('EMAIL_ADDRESS',   'info@agentorc.ca')
def _email_password() -> str:  return os.environ.get('EMAIL_PASSWORD',  '')
def _smtp_host()     -> str:  return os.environ.get('EMAIL_SMTP_HOST', 'mail.agentorc.ca')
def _smtp_port()     -> int:  return int(os.environ.get('EMAIL_SMTP_PORT', '465'))
def _imap_host()     -> str:  return os.environ.get('EMAIL_IMAP_HOST', 'mail.agentorc.ca')
def _imap_port()     -> int:  return int(os.environ.get('EMAIL_IMAP_PORT', '993'))
def _bcc_address()   -> str:  return os.environ.get('EMAIL_BCC',        'info@agentorc.ca')


def _clean(value: str) -> str:
    """Strip all whitespace including non-breaking spaces."""
    return ''.join(ch for ch in value if not ch.isspace()) if value else value


def _send_via_resend(
    to: str,
    subject: str,
    body_html: str,
    body_text: str,
    from_addr: str,
    from_name: str,
    bcc_addr: str,
    auto_replied: bool = False,
) -> Dict[str, Any]:
    """Send via Resend API using the requests library (avoids Cloudflare bot detection)."""
    import requests as _requests

    api_key     = _clean(os.environ.get('RESEND_API_KEY', ''))
    resend_from = _clean(os.environ.get('RESEND_FROM', from_addr))
    to          = _clean(to)
    logger.info(f"[Resend] from={resend_from!r} to={to!r} key_prefix={api_key[:8] if api_key else 'MISSING'}...")

    payload: Dict[str, Any] = {
        'from':    f'{from_name} <{resend_from}>',
        'to':      [to],
        'subject': subject,
        'html':    body_html,
        'text':    body_text,
    }
    if bcc_addr:
        payload['bcc'] = [_clean(bcc_addr)]
    if auto_replied:
        # RFC 3834 on the Resend path too. Railway sends via Resend, so omitting
        # this here would leave the loop guard working on local (SMTP) and not
        # in production — the same split-by-environment trap as the SQL/Python
        # deploy seam.
        payload['headers'] = {'Auto-Submitted': 'auto-replied',
                              'Precedence': 'auto_reply'}

    resp = _requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {api_key}'},
        json=payload,
        timeout=15,
    )
    logger.info(f"[Resend] HTTP {resp.status_code} — body: {resp.text[:300]}")
    # A 4xx/5xx raises here and is caught by send_email, which returns
    # success=False. Status is never inferred from "the call returned".
    resp.raise_for_status()
    body = resp.json()
    logger.info(f"[Resend] OK id={body.get('id')} → {to} | subject={subject!r}")
    # The message id is the provider's own identifier for the message it took
    # responsibility for. Callers that must distinguish 'accepted' from
    # 'we called a function' have nothing else to go on, so it is returned
    # rather than logged and discarded. Note the wording: ACCEPTED for
    # transmission. Resend issuing an id is not evidence of delivery.
    return {'success': True, 'message': f'Email accepted by Resend for {to}',
            'to': to, 'subject': subject,
            'provider': 'resend',
            'provider_message_id': body.get('id'),
            'provider_status': resp.status_code}



def send_email(
    to: str,
    subject: str,
    body_html: str,
    body_text: str,
    from_name: str = 'Conscestra CRM Team',
    bcc: Optional[str] = None,
    commercial: bool = False,
    auto_replied: bool = False,
) -> Dict[str, Any]:
    """Send an email. Uses Resend API when RESEND_API_KEY is set, otherwise SMTP.

    commercial=True marks a CASL commercial electronic message: the recipient
    is checked against the email_suppression opt-out list (send is skipped if
    unsubscribed) and the compliance footer (sender identification +
    unsubscribe link) is appended. Transactional callers leave the default."""
    # Outbound guard (guardrail 3): the last deterministic wall before a
    # customer sees the message — toxic tone, binding promises, leaked
    # internals, over-cap discounts. Applies to EVERY path through here.
    try:
        from app.core.outbound_guard import screen
        # Pass the RECIPIENT, not an audience: the guard resolves policy from
        # database identity. Channel is transport, not audience — this one send
        # path carries both customer mail and internal executive briefings, and
        # screening them under identical tone rules is what blocked the CEO
        # briefing (sent=0, failed=1).
        g = screen(f"{subject or ''}\n{body_text or body_html or ''}", "email",
                   recipient=to)
        if not g["ok"]:
            return {'success': False, 'blocked': True, 'to': to,
                    'message': "blocked by outbound guard: "
                               + "; ".join(g["violations"])}
    except ImportError:
        pass

    if commercial:
        # ONE predicate for every channel (Axis 6 V1). This used to call
        # consent.guard_outbound directly, which was email-specific logic
        # living beside SMS logic that did not exist — the shape that let SMS
        # ship with no consent at all. Email now asks the same question
        # send_sms and place_call ask, of the same store.
        #
        # guard_outbound still runs, for the half that IS email-specific: the
        # CASL footer and its signed unsubscribe link. Consent DECIDES; the
        # footer is formatting.
        from app.core import consent
        if not consent.allows("email", to, commercial=True):
            logger.info(f"[send_email] SKIPPED commercial email to {to!r} "
                        f"(consent: {consent.state('email', to)})")
            return {'success': False, 'skipped': 'unsubscribed', 'to': to,
                    'message': f'{to} has unsubscribed from commercial email'}
        _allowed, body_html, body_text = consent.guard_outbound(
            to, body_html, body_text)

    addr     = _email_address()
    password = _email_password()
    host     = _smtp_host()
    port     = _smtp_port()
    bcc_addr = bcc or _bcc_address()

    resend_key = os.environ.get('RESEND_API_KEY', '')
    logger.info(f"[send_email] to={to!r} | RESEND_API_KEY={'SET' if resend_key else 'NOT SET'} | smtp={host}:{port}")

    # Prefer Resend API for reliable delivery on cloud platforms (Railway)
    if resend_key:
        logger.info(f"[send_email] → using Resend API path")
        try:
            return _send_via_resend(to, subject, body_html, body_text, addr,
                                    from_name, bcc_addr, auto_replied)
        except Exception as e:
            logger.error(f"[send_email] Resend API error: {e}", exc_info=True)
            return {'success': False, 'provider': 'resend', 'to': to,
                    'message': str(e)}

    logger.info(f"[send_email] → using SMTP path (host={host} port={port})")
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'{from_name} <{addr}>'
        msg['To']      = to
        if auto_replied:
            # RFC 3834. Marks this message as machine-generated so no
            # autoresponder — including our own IMAP poller reading the BCC
            # archive copy — treats it as a human asking for a reply.
            msg['Auto-Submitted'] = 'auto-replied'
            msg['Precedence'] = 'auto_reply'
        if bcc_addr:
            msg['Bcc'] = bcc_addr

        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        msg.attach(MIMEText(body_html, 'html',  'utf-8'))

        context    = ssl.create_default_context()
        recipients = [to] + ([bcc_addr] if bcc_addr else [])
        raw        = msg.as_string()

        # sendmail() returns the recipients the server REFUSED. It raises only
        # when EVERY recipient is refused, so a partial refusal used to be
        # discarded — and if the refused one was the customer while the BCC
        # archive was accepted, this returned success for an email nobody got.
        refused: Dict[str, Any] = {}
        try:
            logger.debug(f"[send_email] Trying SMTP_SSL {host}:{port} timeout=15s")
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
                server.login(addr, password)
                refused = server.sendmail(addr, recipients, raw) or {}
            logger.info(f"[send_email] Email accepted by SMTP (SSL) → {to} | subject={subject!r}")
        except smtplib.SMTPException as smtp_err:
            logger.warning(f"[send_email] SMTP_SSL failed: {smtp_err} — retrying STARTTLS on port 587")
            with smtplib.SMTP(host, 587, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(addr, password)
                refused = server.sendmail(addr, recipients, raw) or {}
            logger.info(f"[send_email] Email accepted by SMTP (STARTTLS) → {to} | subject={subject!r}")

        if to in refused:
            logger.error(f"[send_email] SMTP REFUSED the recipient {to}: {refused[to]}")
            return {'success': False, 'to': to, 'provider': 'smtp',
                    'message': f'SMTP refused {to}: {refused[to]}'}

        # 'accepted', not 'sent': the SMTP server took the message (250). SMTP
        # issues no message id, so provider_message_id stays absent — the
        # evidence here is the absence of a refusal, which is weaker than
        # Resend's id and is described as such rather than upgraded.
        return {'success': True, 'message': f'Email accepted by SMTP for {to}',
                'to': to, 'subject': subject, 'provider': 'smtp',
                'provider_message_id': None}

    except Exception as e:
        logger.error(f"[send_email] SMTP error: {e}", exc_info=True)
        return {'success': False, 'provider': 'smtp', 'to': to,
                'message': str(e)}


def fetch_inbox(limit: int = 20, unseen_only: bool = False) -> List[Dict[str, Any]]:
    """Fetch emails from IMAP inbox. Returns list of email dicts."""
    try:
        context = ssl.create_default_context()
        with imaplib.IMAP4_SSL(_imap_host(), _imap_port(), ssl_context=context) as imap:
            imap.login(_email_address(), _email_password())
            imap.select('INBOX')

            criterion = 'UNSEEN' if unseen_only else 'ALL'
            _, msg_nums = imap.search(None, criterion)
            if not msg_nums or not msg_nums[0]:
                return []

            ids = msg_nums[0].split()
            ids = ids[-limit:]  # most recent N

            emails = []
            for num in reversed(ids):
                _, data = imap.fetch(num, '(RFC822)')
                if not data or not data[0]:
                    continue
                raw = data[0][1] if isinstance(data[0], tuple) else None
                if not raw:
                    continue
                parsed = email_lib.message_from_bytes(raw)
                emails.append(_parse_email(parsed))

        logger.info(f"IMAP fetched {len(emails)} emails (limit={limit})")
        return emails

    except (socket.gaierror, TimeoutError, ConnectionError, OSError) as e:
        # Transient network/DNS trouble — the next poll will retry; one calm
        # line, no traceback spam.
        logger.warning(f"IMAP fetch skipped (transient network/DNS): {e}")
        return [{'error': str(e)}]
    except Exception as e:
        logger.error(f"IMAP fetch error: {e}", exc_info=True)
        return [{'error': str(e)}]


def search_inbox(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Search IMAP inbox by subject/body keyword."""
    try:
        context = ssl.create_default_context()
        with imaplib.IMAP4_SSL(_imap_host(), _imap_port(), ssl_context=context) as imap:
            imap.login(_email_address(), _email_password())
            imap.select('INBOX')

            safe_query = query.replace('"', '')
            criterion = f'(OR SUBJECT "{safe_query}" BODY "{safe_query}")'
            _, msg_nums = imap.search(None, criterion)
            if not msg_nums or not msg_nums[0]:
                return []

            ids = msg_nums[0].split()
            ids = ids[-limit:]

            emails = []
            for num in reversed(ids):
                _, data = imap.fetch(num, '(RFC822)')
                if not data or not data[0]:
                    continue
                raw = data[0][1] if isinstance(data[0], tuple) else None
                if not raw:
                    continue
                parsed = email_lib.message_from_bytes(raw)
                emails.append(_parse_email(parsed))

        return emails

    except Exception as e:
        logger.error(f"IMAP search error: {e}", exc_info=True)
        return [{'error': str(e)}]


def _decode_header(value: str) -> str:
    """Decode MIME-encoded header values (=?UTF-8?B?...?= etc.)."""
    if not value:
        return ''
    parts = []
    for chunk, charset in email_lib.header.decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or 'utf-8', errors='replace'))
        else:
            parts.append(chunk)
    return ' '.join(parts)


def _parse_email(msg) -> Dict[str, Any]:
    subject  = _decode_header(msg.get('Subject', '(no subject)'))
    from_    = _decode_header(msg.get('From', ''))
    to_      = _decode_header(msg.get('To', ''))
    date_    = msg.get('Date', '')
    msg_id   = msg.get('Message-ID', '')
    # RFC 3834 loop-prevention headers. Extracted here so should_skip() can
    # decline to answer automatic mail — without them an autoresponder on the
    # far end and ours ping-pong until a rate limit happens to bite.
    auto_sub = (msg.get('Auto-Submitted', '') or '').strip()
    precedence = (msg.get('Precedence', '') or '').strip()

    body_text = ''
    body_html = ''
    attachments = []          # document attachments (pdf/txt/md/csv/html) —
                              # consumed by the KB ingestion hook; capped so a
                              # mailbox bomb can't balloon memory

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/plain' and not body_text:
                try:
                    body_text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or 'utf-8', errors='replace')
                except Exception:
                    pass
            elif ct == 'text/html' and not body_html:
                try:
                    body_html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or 'utf-8', errors='replace')
                except Exception:
                    pass
            fn = part.get_filename()
            if (fn and part.get_content_disposition() == 'attachment'
                    and len(attachments) < 3):
                low = fn.lower()
                if low.endswith(('.pdf', '.txt', '.md', '.markdown', '.csv',
                                 '.html', '.htm')):
                    try:
                        data = part.get_payload(decode=True) or b''
                        if 0 < len(data) <= 8 * 1024 * 1024:
                            attachments.append({'filename': _decode_header(fn),
                                                'data': data})
                    except Exception:
                        pass
    else:
        ct = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or 'utf-8', errors='replace')
            if ct == 'text/html':
                body_html = payload
            else:
                body_text = payload
        except Exception:
            body_text = ''

    return {
        'subject':    subject,
        'from':       from_,
        'to':         to_,
        'date':       date_,
        'message_id': msg_id,
        'auto_submitted': auto_sub,
        'precedence':     precedence,
        'preview':    body_text[:200].strip(),
        'body_text':  body_text,
        'body_html':  body_html,
        '_attachments': attachments,   # internal — carries bytes, never serialize
    }
