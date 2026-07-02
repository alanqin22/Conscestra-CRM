"""AI Contact Summary — decision-grade one-click synthesis of a contact.

Same pattern as app/agents/accounts/ai_summary.py: gather real CRM facts
(profile, account affiliation, engagement recency, open pipeline on their
account) + shared blackboard signals, then LLM-synthesize. Fail-safe: the
deterministic fact sheet is returned when the LLM is unavailable.
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


def resolve_contact(name: str) -> Tuple[Optional[str], Optional[str]]:
    """Full-name → contact_id. Exact (case-insensitive) first, then word-ILIKE;
    only unambiguous matches are accepted. Returns (contact_id, error)."""
    nm = (name or "").strip()
    if not nm:
        return None, "no contact name given"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT contact_id::text FROM contacts "
                "WHERE LOWER(TRIM(first_name || ' ' || last_name)) = LOWER(%s) "
                "  AND (is_deleted IS NULL OR is_deleted = false) LIMIT 2", (nm,))
            rows = cur.fetchall()
            if len(rows) == 1:
                return rows[0][0], None
            if len(rows) > 1:
                return None, f"'{nm}' matches multiple contacts"
            words = [w for w in nm.split() if len(w) >= 2]
            if words:
                conds = " AND ".join(
                    f"LOWER(first_name || ' ' || last_name) LIKE %s" for _ in words)
                cur.execute(
                    f"SELECT contact_id::text FROM contacts WHERE {conds} "
                    "  AND (is_deleted IS NULL OR is_deleted = false) LIMIT 2",
                    tuple(f"%{w.lower()}%" for w in words))
                rows = cur.fetchall()
                if len(rows) == 1:
                    return rows[0][0], None
                if len(rows) > 1:
                    return None, f"'{nm}' matches multiple contacts"
        return None, f"no contact found matching '{nm}'"
    finally:
        conn.close()


def _gather(contact_id: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.first_name || ' ' || c.last_name, c.role, c.status, c.email, "
                "       c.phone, c.is_customer, c.created_at::date, "
                "       c.account_id::text, COALESCE(a.account_name,'(no account)') "
                "FROM contacts c LEFT JOIN accounts a ON a.account_id = c.account_id "
                "WHERE c.contact_id=%s", (contact_id,))
            c = cur.fetchone() or ()
            cur.execute(
                "SELECT COUNT(*), "
                "       COUNT(*) FILTER (WHERE status='open' AND due_at < now()), "
                "       MAX(created_at)::date, "
                "       COUNT(*) FILTER (WHERE created_at >= now() - interval '30 days') "
                "FROM activities WHERE contact_id=%s", (contact_id,))
            acts = cur.fetchone() or ()
            opps = []
            if c and c[7]:
                cur.execute(
                    "SELECT name, stage, COALESCE(amount,0), close_date "
                    "FROM opportunities WHERE account_id=%s AND status='open' "
                    "ORDER BY amount DESC NULLS LAST LIMIT 3", (c[7],))
                opps = cur.fetchall()
        return {"c": c, "acts": acts, "opps": opps}
    finally:
        conn.close()


def _fact_sheet(d: Dict[str, Any], notes: List[Dict[str, Any]]) -> str:
    c, acts = d["c"], d["acts"]
    lines = [
        f"Contact: {c[0]} — {c[1] or 'role n/a'}, status {c[2] or 'n/a'}, "
        f"{'customer' if c[5] else 'not yet a customer'}, on file since {c[6]}",
        f"Account: {c[8]}",
        f"Reachable at: {c[3] or 'no email'} / {c[4] or 'no phone'}",
        f"Engagement: {acts[0]} activities total, {acts[3]} in the last 30 days, "
        f"{acts[1]} overdue open" + (f", last touch {acts[2]}" if acts[2] else ""),
    ]
    if d["opps"]:
        lines.append("Open pipeline on their account:")
        for o in d["opps"]:
            lines.append(f"  - {o[0]} — {_money(o[2])} ({o[1]}, closes {o[3]})")
    else:
        lines.append("Open pipeline on their account: none")
    if notes:
        lines.append("Live agent signals (shared blackboard):")
        for n in notes:
            lines.append(f"  - [{n['severity'] or 'info'}] {n['author_agent']}/{n['topic']}: {n['note']}")
    return "\n".join(lines)


def build_contact_ai_summary(contact_id: str) -> str:
    d = _gather(contact_id)
    if not d["c"]:
        return "### 🧠 AI Contact Summary\nContact not found."

    notes: List[Dict[str, Any]] = []
    try:
        from app.core import blackboard
        notes = blackboard.read("contact", str(contact_id))
        if d["c"][7]:  # account-level signals matter for the relationship too
            notes += blackboard.read("account", str(d["c"][7]))
    except Exception as exc:
        logger.warning(f"[ai_summary] blackboard read failed: {exc}")

    facts = _fact_sheet(d, notes)
    header = f"### 🧠 {d['c'][0]} — AI Contact Summary"

    prompt = (
        "You are a CRM relationship strategist. Using ONLY the facts below, write "
        "a concise contact summary in markdown with EXACTLY these four bold "
        "section labels:\n"
        "**Snapshot** — 1-2 sentences: who they are and their account.\n"
        "**Engagement** — 1-2 sentences: how active the relationship is.\n"
        "**Risks** — bullet list of concrete risks derived ONLY from the facts "
        "above (never copy this instruction); say 'None material.' if none.\n"
        "**Next actions** — 2-3 numbered, specific actions.\n"
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
