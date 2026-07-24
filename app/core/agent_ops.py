"""Agent-Program Operations Analytics — the AI fleet AS A SERVICE OPERATION.

Blindspot #4 (Agentforce parity). We already measure BUSINESS outcomes (pipeline,
AR) and the AI's internal learning report card. What was missing is the scorecard
an evaluator of an AI-agent PLATFORM opens first: how much work does the fleet
actually contain, how often does it escalate to a human, how do customers feel,
and what does a resolved conversation cost? Every number here is assembled from
data we ALREADY collect — the conversation spine ([conversations] + the
agent_console takeover flags), the distilled cross-channel sentiment
(interaction_memories, the same CSAT proxy the CEO briefing uses), and the LLM
meter ([llm_meter]). No new capture, no new cost.

  metrics(days) →
    volume            conversations touched in the window, by channel
    containment_rate  closed conversations the AI resolved with NO human takeover
    escalation_rate   conversations that were EVER handed to a human
    awaiting_human    open conversations sitting in a human's hands right now
    resolution        avg messages + avg hours to close
    csat_proxy        % non-negative distilled conversations (voice→email→chat)
    cost              LLM spend in the window and cost per conversation
    daily             per-day conversations vs escalations (for the trend chart)

Read-only. Admin-gated at the router. Degrades cleanly when the takeover
migration (sql/agent_console.sql) or the memory tables are absent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("agent_ops")


def _has_column(cur, table: str, column: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name=%s", (table, column))
    return cur.fetchone() is not None


def _has_table(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
    return bool(cur.fetchone()[0])


def _pct(numer: float, denom: float) -> Optional[float]:
    return round(100.0 * numer / denom, 1) if denom else None


def metrics(days: int = 30) -> Dict[str, Any]:
    days = max(1, min(days, 365))
    out: Dict[str, Any] = {"window_days": days}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if not _has_table(cur, "conversations"):
                return {**out, "error": "conversations table not found "
                        "(apply sql/unified_comms_conversations.sql)"}
            has_esc = _has_column(cur, "conversations", "escalated")
            has_handling = _has_column(cur, "conversations", "handling")

            # ── Volume + resolution over EXTERNAL conversations in the window ──
            esc_expr = "COUNT(*) FILTER (WHERE escalated)" if has_esc else "NULL"
            cur.execute(
                f"""SELECT
                        COUNT(*),
                        COUNT(*) FILTER (WHERE status='closed'),
                        COUNT(*) FILTER (WHERE status='open'),
                        {esc_expr},
                        AVG(message_count) FILTER (WHERE status='closed'),
                        AVG(EXTRACT(EPOCH FROM (updated_at - created_at))/3600.0)
                            FILTER (WHERE status='closed')
                    FROM conversations
                    WHERE scope='external'
                      AND last_message_at > now() - make_interval(days => %s)""",
                (days,))
            total, closed, open_, escalated, avg_msgs, avg_hours = cur.fetchone()
            total = int(total or 0)
            closed = int(closed or 0)
            open_ = int(open_ or 0)

            # closed conversations the AI resolved without a human = contained
            contained = None
            if has_esc:
                cur.execute(
                    """SELECT COUNT(*) FROM conversations
                       WHERE scope='external' AND status='closed'
                         AND NOT escalated
                         AND last_message_at > now() - make_interval(days => %s)""",
                    (days,))
                contained = int(cur.fetchone()[0] or 0)

            # currently in a human's hands
            awaiting_human = None
            if has_handling:
                cur.execute(
                    """SELECT COUNT(*) FROM conversations
                       WHERE scope='external' AND status='open'
                         AND handling='human'""")
                awaiting_human = int(cur.fetchone()[0] or 0)

            # ── By channel ──
            cur.execute(
                """SELECT channel, COUNT(*) FROM conversations
                   WHERE scope='external'
                     AND last_message_at > now() - make_interval(days => %s)
                   GROUP BY channel ORDER BY 2 DESC""", (days,))
            by_channel = [{"channel": c or "unknown", "count": int(n)}
                          for c, n in cur.fetchall()]

            # ── Daily trend: conversations vs escalations ──
            esc_day = ("COUNT(*) FILTER (WHERE escalated)" if has_esc else "0")
            cur.execute(
                f"""SELECT date_trunc('day', last_message_at)::date AS d,
                           COUNT(*), {esc_day}
                    FROM conversations
                    WHERE scope='external'
                      AND last_message_at > now() - make_interval(days => %s)
                    GROUP BY d ORDER BY d""", (days,))
            daily = [{"date": d.isoformat(), "conversations": int(n),
                      "escalations": int(e or 0)} for d, n, e in cur.fetchall()]

            # ── CSAT proxy — distilled cross-channel sentiment (same source as
            #    the CEO briefing): % of resolved conversations NOT negative. ──
            csat = None
            csat_n = 0
            if _has_table(cur, "interaction_memories"):
                try:
                    cur.execute(
                        """SELECT COUNT(*) FILTER (WHERE sentiment <> 'negative'),
                                  COUNT(*) FILTER (WHERE sentiment IS NOT NULL)
                           FROM interaction_memories
                           WHERE created_at > now() - make_interval(days => %s)""",
                        (days,))
                    nn, tot = cur.fetchone()
                    csat_n = int(tot or 0)
                    csat = _pct(int(nn or 0), csat_n)
                except Exception:
                    conn.rollback()
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[agent_ops] metrics failed: {exc}")
        return {**out, "error": str(exc)[:200]}
    finally:
        conn.close()

    # ── LLM spend (metered) + cost per conversation ──
    llm_cost = llm_calls = None
    try:
        from app.core import llm_meter
        s = llm_meter.usage_summary(days)
        if not s.get("error"):
            llm_cost = s["totals"].get("est_cost_usd")
            llm_calls = s["totals"].get("calls")
    except Exception as exc:
        logger.debug(f"[agent_ops] llm spend skipped: {exc}")

    out.update({
        "volume": {
            "total": total, "closed": closed, "open": open_,
            "escalated": (int(escalated) if escalated is not None else None),
            "by_channel": by_channel,
        },
        "containment_rate": _pct(contained, closed) if contained is not None else None,
        "escalation_rate": (_pct(int(escalated), total)
                            if escalated is not None else None),
        "awaiting_human": awaiting_human,
        "resolution": {
            "avg_messages": round(float(avg_msgs), 1) if avg_msgs else None,
            "avg_hours_to_close": round(float(avg_hours), 1) if avg_hours else None,
        },
        "csat_proxy_pct": csat,
        "csat_sample": csat_n,
        "cost": {
            "llm_usd": llm_cost, "llm_calls": llm_calls,
            "per_conversation_usd": (round(llm_cost / total, 4)
                                     if llm_cost is not None and total else None),
        },
        "daily": daily,
        "migration_applied": bool(escalated is not None),
    })
    return out


router = APIRouter(tags=["agent-ops"])


@router.get("/ops/agent-metrics")
def ops_agent_metrics(days: int = 30):
    return metrics(days)
