"""Workflow engine — status reporting only.

THIS MODULE NO LONGER CLAIMS WORK, deliberately.

It began as a partitioned second consumer of `event_queue`, taking only event
types agent_bus had no handler for. That design was defeated before it ever ran:
this deployment sets AGENT_BUS_CATCHALL=1, which makes agent_bus claim ANY
pending type rather than just its registered ones. Two consumers on one queue,
both using FOR UPDATE SKIP LOCKED, means whichever polls first wins and the
other silently never sees the event — starvation, invisible in both logs.

The fix was to stop competing. Under Option C the engine is CHAINED into
agent_bus instead: `agent_bus.handle_default` calls
`workflow_run_rules_for_event(event_uuid)`, a pure function of an event that
never reads or writes `event_queue`. agent_bus remains the sole queue consumer,
so there is no partition to drift and no queue state to contend over.

A claim loop here would reintroduce exactly the race Option C removed, so the
loop is gone rather than merely disabled — a disabled loop is one env var away
from being a bug again.

The engine is driven by agent_bus's existing consumer loop and needs no
scheduler job of its own. What remains here is observability.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from app.core.database import get_connection

ENABLED = os.getenv("WORKFLOW_ENGINE_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on")


def status() -> Dict[str, Any]:
    """What the engine owns, and how it has actually been doing. Read-only.

    `runs_last_7d` is the metric that matters: before Phase 9 an unconditional
    UPDATE overwrote 'failed' with 'completed' after the exception handler had
    already run, so 5,186 of 5,186 historical runs read 'completed' while every
    step beneath them read 'failed'. `failed_with_error` exists to make a
    recurrence visible immediately rather than 188 days later."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT workflow_owned_event_types()")
            types: List[str] = list(cur.fetchone()[0] or [])

            cur.execute("SELECT count(*) FROM workflow_rules WHERE is_enabled")
            enabled_rules = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM workflow_rules")
            total_rules = cur.fetchone()[0]

            cur.execute("""SELECT status, count(*) FROM workflow_runs
                            WHERE started_at > now() - interval '7 days'
                            GROUP BY 1""")
            recent = dict(cur.fetchall())

            # A run marked completed while carrying an error is the signature of
            # the status-overwrite defect. This must stay 0.
            cur.execute("""SELECT count(*) FROM workflow_runs
                            WHERE status = 'completed' AND error_message IS NOT NULL
                              AND started_at > now() - interval '7 days'""")
            liars = cur.fetchone()[0]

            cur.execute("""SELECT count(*) FROM event_types
                            WHERE event_type = ANY(%s) AND queue_enabled""", (types,))
            queued_types = cur.fetchone()[0]
    finally:
        conn.close()

    return {
        "enabled": ENABLED,
        "driver": "agent_bus.handle_default (chained; no separate worker)",
        "owned_event_types": types,
        "owned_types_reaching_the_queue": queued_types,
        "enabled_rules": enabled_rules,
        "disabled_rules": total_rules - enabled_rules,
        "runs_last_7d": recent,
        "completed_but_carrying_an_error": liars,
        "health": "ok" if liars == 0 else "STATUS OVERWRITE HAS RECURRED",
    }
