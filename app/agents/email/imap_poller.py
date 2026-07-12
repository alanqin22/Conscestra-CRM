"""Background IMAP poller — checks inbox every 5 minutes and auto-replies to eligible emails."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECS = 300  # 5 minutes


class ImapPoller:
    """Daemon thread that polls IMAP and dispatches auto-replies."""

    def __init__(self, own_address: str, interval: int = _POLL_INTERVAL_SECS):
        self._own_address = own_address
        self._interval    = interval
        self._stop_event  = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="imap-poller")
        self._thread.start()
        logger.info(f"ImapPoller started (interval={self._interval}s, own={self._own_address})")

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info("ImapPoller stopped.")

    def _run(self) -> None:
        # Stagger first poll by 15 s so the app finishes startup first
        if self._stop_event.wait(15):
            return
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                logger.error(f"ImapPoller unhandled error: {exc}", exc_info=True)
            self._stop_event.wait(self._interval)

    def _poll_once(self) -> None:
        from app.agents.email.smtp_imap import fetch_inbox
        from app.agents.email.auto_reply import process_inbound_email

        try:
            emails = fetch_inbox(unseen_only=True)
        except Exception as exc:
            logger.warning(f"ImapPoller fetch_inbox failed: {exc}")
            return

        # fetch_inbox signals failure as [{'error': …}] (kept that way for the
        # inbox UI) — those are NOT messages; processing one would feed a junk
        # dict into the auto-reply pipeline.
        emails = [e for e in emails if not e.get("error")]
        if not emails:
            return

        logger.info(f"ImapPoller: {len(emails)} unseen email(s) fetched.")
        replied = 0
        for email in emails:
            try:
                if process_inbound_email(email, self._own_address):
                    replied += 1
            except Exception as exc:
                logger.error(f"ImapPoller process_inbound_email error: {exc}", exc_info=True)

        if replied:
            logger.info(f"ImapPoller: {replied} auto-reply/replies sent.")


# Module-level singleton — created lazily in start_poller()
_poller: Optional[ImapPoller] = None


def start_poller(own_address: str, interval: int = _POLL_INTERVAL_SECS) -> ImapPoller:
    global _poller
    _poller = ImapPoller(own_address=own_address, interval=interval)
    _poller.start()
    return _poller


def stop_poller() -> None:
    global _poller
    if _poller:
        _poller.stop()
        _poller = None
