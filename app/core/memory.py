"""Session conversation memory — shared across all CRM Agent modules.

Mirrors the n8n 'Simple Memory2' node (memoryBufferWindow keyed by sessionId),
now DURABLE (audit item #3): the in-process deque stays the fast path, and
every saved turn is written through to `agent_session_memory` (one row per
session, the whole trimmed window as jsonb). On a cache miss — a restart, or
another uvicorn worker — get_history reloads the window from the DB, so a chat
session survives both.

Degrades: table missing / DB down → in-process only, exactly the old
behavior. A memory failure must never break a chat turn.

Design
------
All agents share a single store keyed by session_id. Sessions from different
agents are namespaced automatically because each agent passes its own
session_id format (e.g. ``accounts-<uuid>`` vs ``contacts-<uuid>``).

Retention: rows idle longer than SESSION_MEMORY_TTL_DAYS are pruned
opportunistically (1-in-50 saves), so the table stays a few KB per active
session and needs no scheduled job.

CONFIG (env)
  MEMORY_WINDOW_SIZE       5   turns kept per session (0 disables memory)
  SESSION_MEMORY_DB        1   write-through persistence kill switch
  SESSION_MEMORY_TTL_DAYS  7   idle sessions older than this are pruned
"""

import json
import logging
import os
import random
from collections import deque
from typing import Any, Dict, List

from .config import get_settings

logger = logging.getLogger(__name__)


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


DB_ENABLED = _flag("SESSION_MEMORY_DB", "1")
TTL_DAYS = int(os.getenv("SESSION_MEMORY_TTL_DAYS", "7"))

# { session_id: deque([{role, content}, ...]) }
# Each pair of user + assistant messages = one "turn".
# Window cap = memory_window_size * 2 individual messages.
_store: Dict[str, deque] = {}


def _max_len() -> int:
    return max(0, get_settings().memory_window_size) * 2


def _get_deque(session_id: str) -> deque:
    if session_id not in _store:
        max_len = _max_len()
        _store[session_id] = deque(maxlen=max_len if max_len > 0 else None)
    return _store[session_id]


# ── DB write-through (best-effort — never raises into the chat path) ─────────

def _db_load(session_id: str) -> List[Dict[str, Any]]:
    if not DB_ENABLED:
        return []
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT messages FROM agent_session_memory "
                            "WHERE session_id=%s", (session_id,))
                row = cur.fetchone()
            msgs = row[0] if row else []
            return msgs if isinstance(msgs, list) else json.loads(msgs)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"Memory DB load skipped (table applied?): {exc}")
        return []


def _db_save(session_id: str, messages: List[Dict[str, Any]]) -> None:
    if not DB_ENABLED:
        return
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO agent_session_memory (session_id, messages)
                       VALUES (%s, %s::jsonb)
                       ON CONFLICT (session_id) DO UPDATE SET
                         messages=EXCLUDED.messages, updated_at=now()""",
                    (session_id, json.dumps(messages)))
                if TTL_DAYS > 0 and random.random() < 0.02:
                    cur.execute("DELETE FROM agent_session_memory WHERE "
                                "updated_at < now() - make_interval(days => %s)",
                                (TTL_DAYS,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"Memory DB save skipped (table applied?): {exc}")


def _db_delete(session_id: str) -> None:
    if not DB_ENABLED:
        return
    try:
        from app.core.database import get_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_session_memory "
                            "WHERE session_id=%s", (session_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"Memory DB delete skipped: {exc}")


# ── Public API ────────────────────────────────────────────────────────────────

def get_history(session_id: str) -> List[Dict[str, Any]]:
    """
    Return conversation history for session_id as a list of message dicts:
        [{"role": "user", "content": "..."}, {"role": "assistant", ...}, ...]
    Oldest first; ready to prepend before the current user message.
    Returns [] when memory is disabled (window_size == 0) or session is new.
    A cache miss (restart / other worker) reloads the window from the DB.
    """
    if get_settings().memory_window_size <= 0:
        return []

    dq = _store.get(session_id)
    if dq is None:
        saved = _db_load(session_id)
        if not saved:
            return []
        dq = _get_deque(session_id)
        dq.extend(saved)          # deque maxlen keeps only the window tail
        logger.debug(f"Memory rehydrated from DB — session={session_id!r}, "
                     f"{len(dq)} messages")

    history = list(dq)
    logger.debug(f"Memory get_history — session={session_id!r}, "
                 f"{len(history)} messages")
    return history


def save_turn(session_id: str, user_message: str, assistant_message: str) -> None:
    """
    Persist one turn (user + assistant) to the rolling window — in process AND
    written through to the DB. Oldest turns are evicted automatically once
    maxlen is reached. No-op when memory is disabled (window_size == 0).
    """
    if get_settings().memory_window_size <= 0:
        return

    dq = _get_deque(session_id)
    dq.append({"role": "user",      "content": user_message})
    dq.append({"role": "assistant", "content": assistant_message})
    _db_save(session_id, list(dq))

    logger.debug(
        f"Memory save_turn — session={session_id!r}, "
        f"window now {len(dq)}/{dq.maxlen} messages"
    )


def clear_session(session_id: str) -> None:
    """Remove all history for a session (useful for testing / manual resets)."""
    if session_id in _store:
        del _store[session_id]
        logger.info(f"Memory cleared for session={session_id!r}")
    _db_delete(session_id)


def active_sessions() -> List[str]:
    """Session IDs with history in THIS process (the DB may hold more)."""
    return list(_store.keys())
