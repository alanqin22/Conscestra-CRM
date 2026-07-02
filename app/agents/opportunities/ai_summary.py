"""AI Deal Summary — decision-grade one-click synthesis of an opportunity.

Same pattern as app/agents/accounts/ai_summary.py: real CRM facts (deal
economics, timing, account context, activity coverage) + shared blackboard
signals, LLM-synthesized with a deterministic fact-sheet fallback.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.core.database import get_connection

logger = logging.getLogger(__name__)


def _money(n) -> str:
    try:
        return "${:,.0f}".format(float(n or 0))
    except Exception:
        return "$0"


def resolve_opportunity(name: str) -> Tuple[Optional[str], Optional[str]]:
    """Deal name → opportunity_id (exact then substring; open deals win ties
    only when the match is still unambiguous). Returns (id, error)."""
    nm = (name or "").strip()
    if not nm:
        return None, "no opportunity name given"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for sql, param in (
                ("SELECT opportunity_id::text FROM opportunities "
                 "WHERE LOWER(TRIM(name)) = LOWER(%s) LIMIT 2", nm),
                ("SELECT opportunity_id::text FROM opportunities "
                 "WHERE name ILIKE %s LIMIT 2", f"%{nm}%"),
            ):
                cur.execute(sql, (param,))
                rows = cur.fetchall()
                if len(rows) == 1:
                    return rows[0][0], None
                if len(rows) > 1:
                    return None, f"'{nm}' matches multiple opportunities"
        return None, f"no opportunity found matching '{nm}'"
    finally:
        conn.close()


def _gather(opportunity_id: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT o.name, o.stage, o.status, COALESCE(o.amount,0), "
                "       COALESCE(o.probability,0), o.close_date, o.lead_source, "
                "       o.created_at::date, o.margin_pct, o.margin_health, "
                "       o.account_id::text, COALESCE(a.account_name,'(no account)'), "
                "       (o.close_date - CURRENT_DATE) AS days_to_close "
                "FROM opportunities o LEFT JOIN accounts a ON a.account_id=o.account_id "
                "WHERE o.opportunity_id=%s", (opportunity_id,))
            o = cur.fetchone() or ()
            cur.execute(
                "SELECT COUNT(*), MAX(created_at)::date FROM activities "
                "WHERE related_type='opportunity' AND related_id=%s", (opportunity_id,))
            acts = cur.fetchone() or ()
            acct = ()
            if o and o[10]:
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE status='open'), "
                    "       COALESCE(SUM(amount) FILTER (WHERE status='open'),0), "
                    "       COUNT(*) FILTER (WHERE status='closed_won') "
                    "FROM opportunities WHERE account_id=%s", (o[10],))
                acct = cur.fetchone() or ()
        return {"o": o, "acts": acts, "acct": acct}
    finally:
        conn.close()


def _fact_sheet(d: Dict[str, Any], notes: List[Dict[str, Any]]) -> str:
    o, acts, acct = d["o"], d["acts"], d["acct"]
    weighted = float(o[3]) * float(o[4]) / 100.0
    timing = (f"{o[12]} days to close" if (o[12] or 0) >= 0
              else f"{abs(o[12])} days PAST its close date")
    # margin_pct is stored as a fraction (0.59 = 59%)
    margin = ""
    if o[8] is not None:
        pct = float(o[8]) * 100 if float(o[8]) <= 1 else float(o[8])
        margin = f", margin {pct:.0f}%" + (f" ({o[9]})" if o[9] else "")
    lines = [
        f"Deal: {o[0]} — {o[1]} stage, status {o[2]}, created {o[7]}, source {o[6] or 'n/a'}",
        f"Value: {_money(o[3])} at {int(o[4])}% probability (weighted {_money(weighted)})"
        + margin,
        f"Timing: closes {o[5]} — {timing}",
        f"Account: {o[11]}"
        + (f" — {acct[0]} open deals worth {_money(acct[1])}, {acct[2]} won historically"
           if acct else ""),
        f"Activity coverage: {acts[0]} activities logged on this deal"
        + (f", last touch {acts[1]}" if acts[1] else " — NO touches recorded"),
    ]
    if notes:
        lines.append("Live agent signals (shared blackboard):")
        for n in notes:
            lines.append(f"  - [{n['severity'] or 'info'}] {n['author_agent']}/{n['topic']}: {n['note']}")
    return "\n".join(lines)


def build_opportunity_ai_summary(opportunity_id: str) -> str:
    d = _gather(opportunity_id)
    if not d["o"]:
        return "### 🧠 AI Deal Summary\nOpportunity not found."

    notes: List[Dict[str, Any]] = []
    try:
        from app.core import blackboard
        notes = blackboard.read("opportunity", str(opportunity_id))
        if d["o"][10]:
            notes += blackboard.read("account", str(d["o"][10]))
    except Exception as exc:
        logger.warning(f"[ai_summary] blackboard read failed: {exc}")

    facts = _fact_sheet(d, notes)
    header = f"### 🧠 {d['o'][0]} — AI Deal Summary"

    prompt = (
        "You are a B2B deal strategist. Using ONLY the facts below, write a "
        "concise deal summary in markdown with EXACTLY these four bold "
        "section labels:\n"
        "**Snapshot** — 1-2 sentences: the deal, its value and stage.\n"
        "**Deal health** — 1-2 sentences: probability, margin, timing, coverage.\n"
        "**Risks** — bullet list of concrete risks derived ONLY from the facts "
        "above (never copy this instruction); say 'None material.' if none.\n"
        "**Next actions** — 2-3 numbered, specific actions to advance or "
        "rescue the deal.\n"
        "Mention agent signals when present. No preamble, no invented numbers."
    )
    try:
        from app.core.graph_utils import _get_llm
        llm = _get_llm()
        resp = llm.invoke([{"role": "system", "content": prompt},
                           {"role": "user", "content": facts}])
        body = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        if body:
            return f"{header}\n{body}"
    except Exception as exc:
        logger.error(f"[ai_summary] LLM synthesis failed, returning facts: {exc}")

    return f"{header}\n" + "\n".join(f"- {ln}" for ln in facts.split("\n"))
