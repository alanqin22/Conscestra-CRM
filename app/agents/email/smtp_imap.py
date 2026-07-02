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

    resp = _requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {api_key}'},
        json=payload,
        timeout=15,
    )
    logger.info(f"[Resend] HTTP {resp.status_code} — body: {resp.text[:300]}")
    resp.raise_for_status()
    body = resp.json()
    logger.info(f"[Resend] OK id={body.get('id')} → {to} | subject={subject!r}")
    return {'success': True, 'message': f'Email sent to {to}', 'to': to, 'subject': subject}



def send_email(
    to: str,
    subject: str,
    body_html: str,
    body_text: str,
    from_name: str = 'Conscestra CRM Team',
    bcc: Optional[str] = None,
    commercial: bool = False,
) -> Dict[str, Any]:
    """Send an email. Uses Resend API when RESEND_API_KEY is set, otherwise SMTP.

    commercial=True marks a CASL commercial electronic message: the recipient
    is checked against the email_suppression opt-out list (send is skipped if
    unsubscribed) and the compliance footer (sender identification +
    unsubscribe link) is appended. Transactional callers leave the default."""
    if commercial:
        from app.core.consent import guard_outbound
        allowed, body_html, body_text = guard_outbound(to, body_html, body_text)
        if not allowed:
            logger.info(f"[send_email] SKIPPED commercial email to unsubscribed {to!r}")
            return {'success': False, 'skipped': 'unsubscribed', 'to': to,
                    'message': f'{to} has unsubscribed from commercial email'}

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
            return _send_via_resend(to, subject, body_html, body_text, addr, from_name, bcc_addr)
        except Exception as e:
            logger.error(f"[send_email] Resend API error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    logger.info(f"[send_email] → using SMTP path (host={host} port={port})")
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'{from_name} <{addr}>'
        msg['To']      = to
        if bcc_addr:
            msg['Bcc'] = bcc_addr

        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        msg.attach(MIMEText(body_html, 'html',  'utf-8'))

        context    = ssl.create_default_context()
        recipients = [to] + ([bcc_addr] if bcc_addr else [])
        raw        = msg.as_string()

        try:
            logger.debug(f"[send_email] Trying SMTP_SSL {host}:{port} timeout=15s")
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
                server.login(addr, password)
                server.sendmail(addr, recipients, raw)
            logger.info(f"[send_email] Email sent (SSL) → {to} | subject={subject!r}")
        except smtplib.SMTPException as smtp_err:
            logger.warning(f"[send_email] SMTP_SSL failed: {smtp_err} — retrying STARTTLS on port 587")
            with smtplib.SMTP(host, 587, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(addr, password)
                server.sendmail(addr, recipients, raw)
            logger.info(f"[send_email] Email sent (STARTTLS) → {to} | subject={subject!r}")

        return {'success': True, 'message': f'Email sent to {to}', 'to': to, 'subject': subject}

    except Exception as e:
        logger.error(f"[send_email] SMTP error: {e}", exc_info=True)
        return {'success': False, 'message': str(e)}


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

    body_text = ''
    body_html = ''

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
        'preview':    body_text[:200].strip(),
        'body_text':  body_text,
        'body_html':  body_html,
    }
