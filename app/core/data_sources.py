"""Source freshness — what "as of" is actually allowed to claim (finding #8).

A briefing can be internally consistent (one snapshot, one moment) and still be
misleading, because the database is not the business. If the accounting sync
last succeeded three days ago, "revenue as of 14:00" describes a 14:00 view of
three-day-old facts. Nothing recorded that, so nothing could say it.

    data_sources ──▶ freshness() ──▶ as_of_qualifier() ──▶ briefings / answers

This module owns the read side of that registry plus the small write helpers an
ingestion path calls (`begin`, `succeed`, `fail`, `reject`). It moves no data
itself — a connector framework is a separate piece of work — but every source
that is added can immediately be held to a freshness SLA, and any answer can ask
whether its inputs are current.

`stale` means measured against the source's own declared SLA, not a global
guess: a nightly accounting export is not late at 10am, and a real-time webhook
is late after minutes. A source with no SLA is reported as `unknown`, never as
fresh — an unmeasured source is not a current one.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("data_sources")

OK, PARTIAL, FAILED, RUNNING, NEVER = "ok", "partial", "failed", "running", "never_run"


def _rows(sql: str, args: tuple = ()) -> List[tuple]:
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[data_sources] read skipped: {exc}")
        return []


def _exec(sql: str, args: tuple = ()) -> bool:
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[data_sources] write skipped: {exc}")
        return False


# ── Write side: what an ingestion run reports ────────────────────────────────

def register(source_key: str, label: str, kind: str = "import",
             entity: Optional[str] = None,
             freshness_sla_minutes: Optional[int] = None) -> bool:
    """Declare a source. Idempotent — re-registering updates its metadata but
    never resets its watermark or sync history."""
    return _exec(
        """INSERT INTO data_sources (source_key, label, kind, entity,
                                     freshness_sla_minutes)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (source_key) DO UPDATE SET
             label=EXCLUDED.label, kind=EXCLUDED.kind, entity=EXCLUDED.entity,
             freshness_sla_minutes=EXCLUDED.freshness_sla_minutes,
             updated_at=now()""",
        (source_key, label, kind, entity, freshness_sla_minutes))


def begin(source_key: str) -> bool:
    return _exec("UPDATE data_sources SET last_status='running', "
                 "last_sync_at=now(), updated_at=now() WHERE source_key=%s",
                 (source_key,))


def succeed(source_key: str, watermark: Optional[str] = None,
            seen: int = 0, written: int = 0, rejected: int = 0) -> bool:
    """Record a completed run. `partial` when rows were rejected — a run that
    dropped records is not a success, and a freshness check that treats it as
    one hides the gap."""
    status = PARTIAL if rejected else OK
    return _exec(
        """UPDATE data_sources
              SET last_status=%s, last_success_at=now(), last_sync_at=now(),
                  last_error=NULL,
                  watermark=COALESCE(%s, watermark),
                  rows_seen=rows_seen+%s, rows_written=rows_written+%s,
                  rows_rejected=rows_rejected+%s, updated_at=now()
            WHERE source_key=%s""",
        (status, watermark, seen, written, rejected, source_key))


def fail(source_key: str, error: str) -> bool:
    return _exec("UPDATE data_sources SET last_status='failed', last_sync_at=now(), "
                 "last_error=%s, updated_at=now() WHERE source_key=%s",
                 (str(error)[:500], source_key))


def reject(source_key: str, batch_ref: str, row_number: int,
           reason: str, payload: Optional[Dict[str, Any]] = None) -> bool:
    """Persist a row an import refused.

    Rejected rows used to be returned in an HTTP response and then lost, which
    makes a partial import unfixable: you know 40 rows failed and cannot see
    which. Durable rejects can be corrected and re-run."""
    return _exec(
        """INSERT INTO data_source_rejects
             (source_key, batch_ref, row_number, reason, payload)
           VALUES (%s,%s,%s,%s,%s::jsonb)""",
        (source_key, batch_ref, row_number, str(reason)[:300],
         json.dumps(payload or {})))


def watermark(source_key: str) -> Optional[str]:
    """The high-water mark to resume from. This is what makes a sync
    INCREMENTAL rather than a full re-read every time."""
    r = _rows("SELECT watermark FROM data_sources WHERE source_key=%s", (source_key,))
    return r[0][0] if r else None


# ── Read side: how stale is the picture? ─────────────────────────────────────

def freshness() -> Dict[str, Any]:
    """Per-source staleness, measured against each source's OWN SLA."""
    rows = _rows(
        """SELECT source_key, label, kind, entity, last_status, last_success_at,
                  freshness_sla_minutes, enabled, rows_rejected
             FROM data_sources ORDER BY source_key""")
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for key, label, kind, entity, status, last_ok, sla, enabled, rejected in rows:
        age_min = ((now - last_ok).total_seconds() / 60.0) if last_ok else None
        if not enabled:
            state = "disabled"
        elif last_ok is None:
            state = "never_synced"
        elif sla is None:
            # No declared expectation — we know when it last ran, not whether
            # that is acceptable. Unmeasured is not the same as current.
            state = "unknown"
        else:
            state = "stale" if age_min > sla else "fresh"
        out.append({"source_key": key, "label": label, "kind": kind,
                    "entity": entity, "status": status, "state": state,
                    "last_success_at": last_ok.isoformat() if last_ok else None,
                    "age_minutes": round(age_min, 1) if age_min is not None else None,
                    "sla_minutes": sla, "rows_rejected": int(rejected or 0)})
    stale = [s for s in out if s["state"] in ("stale", "never_synced")]
    return {"sources": out, "stale_count": len(stale),
            "all_fresh": bool(out) and not stale}


def as_of_qualifier(source_keys: Optional[List[str]] = None) -> Optional[str]:
    """The sentence an answer must carry when its inputs are not current.

    `source_keys` restricts the check to the systems that actually feed the
    figure being reported (see metrics.Metric.sources).

    Returns None when everything with a declared SLA is inside it. This is the
    difference between "the database as of X" and "the business as of X" — a
    briefing that cannot say which is claiming the stronger one by default."""
    f = freshness()
    problems = [s for s in f["sources"] if s["state"] in ("stale", "never_synced")]
    # TARGETED, not global. Warning about a stale accounting sync on a figure
    # accounting does not feed trains people to ignore the warning — which is
    # worse than showing none. With no keys given the caveat stays global,
    # because "we do not know what fed this" is itself a reason to qualify.
    if source_keys is not None:
        wanted = set(source_keys)
        problems = [s for s in problems if s["source_key"] in wanted]
    if not problems:
        return None
    parts = []
    for s in problems[:4]:
        if s["state"] == "never_synced":
            parts.append(f"{s['label']} has never synced")
        else:
            hrs = (s["age_minutes"] or 0) / 60.0
            parts.append(f"{s['label']} last synced {hrs:.1f}h ago "
                         f"(SLA {s['sla_minutes']}m)")
    return ("Figures may not reflect the latest source data — "
            + "; ".join(parts) + ".")


router = APIRouter(tags=["data-sources"])


@router.get("/data-sources")
def data_sources_list():
    return freshness()


@router.get("/data-sources/qualifier")
def data_sources_qualifier():
    """The staleness sentence, or null when every source is inside its SLA."""
    return {"qualifier": as_of_qualifier()}


@router.get("/data-sources/{source_key}/rejects")
def data_source_rejects(source_key: str, limit: int = 50):
    rows = _rows(
        """SELECT batch_ref, row_number, reason, payload, created_at
             FROM data_source_rejects WHERE source_key=%s
            ORDER BY created_at DESC LIMIT %s""", (source_key, int(limit)))
    return {"source_key": source_key,
            "rejects": [{"batch_ref": r[0], "row_number": r[1], "reason": r[2],
                         "payload": r[3], "at": r[4].isoformat()} for r in rows]}
