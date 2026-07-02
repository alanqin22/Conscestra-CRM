"""Server-Sent Events push for notifications — replaces frontend polling.

GET /notifications/stream
    Long-lived SSE connection. Emits:
      event: hello         once on connect — {"unread": N}
      event: notification  for each NEW notifications row (channel != agent_inbox)
      event: ping          every ~25s keep-alive

    Client (EventSource) reconnects automatically; `since` (ISO timestamp)
    lets a reconnecting client backfill missed rows.

Implementation: 3-second DB delta polling server-side (single cheap indexed
query per tick per connection) — a pragmatic push layer that needs no
LISTEN/NOTIFY plumbing and works unchanged on Railway.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.core.database import get_connection

logger = logging.getLogger("notify_stream")

router = APIRouter(tags=["notifications-stream"])


def _secs(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


# SSE_POLL_SECS — how often the server checks the DB for new rows (this is
#   the knob that costs database work; one indexed query per tick per client).
# SSE_PING_SECS — keep-alive heartbeat on the open connection (no DB work;
#   only matters for proxies that drop idle connections). Quiet dev DB: 3600.
#   For real business set SSE_PING_SECS=25 so connections survive proxies.
_POLL_SECS = _secs("SSE_POLL_SECS", 3.0)
_PING_EVERY = _secs("SSE_PING_SECS", 25.0)


def _unread_count() -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM notifications "
                        "WHERE read_at IS NULL AND channel <> 'agent_inbox'")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _new_since(since: datetime) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT notification_uuid::text, title, body, channel, status, "
                "       created_at "
                "FROM notifications "
                "WHERE created_at > %s AND channel <> 'agent_inbox' "
                "ORDER BY created_at LIMIT 20", (since,))
            return cur.fetchall()
    finally:
        conn.close()


@router.get("/notifications/stream")
async def notifications_stream(request: Request, since: Optional[str] = None):
    try:
        watermark = (datetime.fromisoformat(since) if since
                     else datetime.now(timezone.utc))
    except ValueError:
        watermark = datetime.now(timezone.utc)

    async def gen() -> AsyncIterator[dict]:
        nonlocal watermark
        try:
            unread = await asyncio.to_thread(_unread_count)
        except Exception as exc:
            logger.warning(f"[sse] hello unread failed: {exc}")
            unread = 0
        yield {"event": "hello", "data": json.dumps({"unread": unread})}

        last_ping = asyncio.get_event_loop().time()
        while True:
            if await request.is_disconnected():
                break
            try:
                rows = await asyncio.to_thread(_new_since, watermark)
            except Exception as exc:
                logger.warning(f"[sse] delta query failed: {exc}")
                rows = []
            for r in rows:
                watermark = max(watermark, r[5])
                yield {
                    "event": "notification",
                    "id": r[0],
                    "data": json.dumps({
                        "id": r[0], "title": r[1], "body": (r[2] or "")[:300],
                        "channel": r[3], "status": r[4],
                        "created_at": r[5].isoformat(),
                    }),
                }
            now = asyncio.get_event_loop().time()
            if now - last_ping >= _PING_EVERY:
                last_ping = now
                yield {"event": "ping",
                       "data": json.dumps({"ts": datetime.now(timezone.utc).isoformat()})}
            await asyncio.sleep(_POLL_SECS)

    return EventSourceResponse(gen())
