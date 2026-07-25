"""AI Account Summary — a decision-grade, one-click 360 synthesis.

Gathers the account's real CRM facts (profile, revenue, pipeline, AR,
activity recency) PLUS the shared agent blackboard (ar_risk / hot_lead /
dunning_hold signals other agents posted), then has the LLM write a short
executive summary with recommended next actions.

Fail-safe: if the LLM is unavailable the deterministic fact sheet is
returned unchanged, so the mode always answers.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.core.database import get_connection

logger = logging.getLogger(__name__)


def _rows(cur, sql: str, params=None) -> List[tuple]:
    cur.execute(sql, params or ())
    return cur.fetchall()


def _one(cur, sql: str, params=None):
    cur.execute(sql, params or ())
    return cur.fetchone() or ()


def _money(n) -> str:
    try:
        return "${:,.0f}".format(float(n or 0))
    except Exception:
        return "$0"


def _gather(account_id: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            acct = _one(cur,
                "SELECT account_name, type, industry, status, email, phone, "
                "       created_at::date FROM accounts WHERE account_id=%s",
                (account_id,))
            rev = _one(cur,
                "SELECT COUNT(*), COALESCE(SUM(total_amount),0), MAX(order_date::date) "
                "FROM orders WHERE account_id=%s AND deleted_at IS NULL", (account_id,))
            opps = _rows(cur,
                "SELECT name, stage, COALESCE(amount,0), COALESCE(probability,0), close_date "
                "FROM opportunities WHERE account_id=%s AND status='open' "
                "ORDER BY amount DESC NULLS LAST LIMIT 5", (account_id,))
            ar = _one(cur,
                "SELECT COUNT(*), COALESCE(SUM(computed_balance_due),0), "
                "       MAX(CURRENT_DATE - due_date::date) "
                "FROM accounting_invoice_pipeline "
                "WHERE account_id=%s AND payment_status IN ('unpaid','partial') "
                "  AND due_date::date < CURRENT_DATE", (account_id,))
            acts = _one(cur,
                "SELECT COUNT(*) FILTER (WHERE status='open' AND due_at < now()), "
                "       MAX(created_at)::date "
                "FROM activities WHERE account_id=%s", (account_id,))
            contacts = _one(cur,
                "SELECT COUNT(*) FROM contacts WHERE account_id=%s", (account_id,))
            # Persisted customer-intelligence profile (nightly scorer) —
            # best-effort: tolerate the table not existing yet.
            try:
                intel = _one(cur,
                    "SELECT churn_risk, churn_band, ltv, order_recency_days, "
                    "       typical_gap_days, next_purchase_due, preferred_channel, "
                    "       computed_at::date, preferred_hour, interests, "
                    "       sentiment_score, sentiment_label "
                    "FROM account_intelligence WHERE account_id=%s", (account_id,))
            except Exception:
                conn.rollback()
                intel = ()
        # Admin-defined custom fields (P3) — so the 360 reflects the customer's
        # own data model, not just the fixed schema. Best-effort.
        try:
            from app.core import custom_fields
            custom = custom_fields.get_values_labeled("accounts", account_id)
        except Exception:
            custom = []
        return {"acct": acct, "rev": rev, "opps": opps, "ar": ar,
                "acts": acts, "contacts": contacts, "intel": intel, "custom": custom}
    finally:
        conn.close()


def _fact_sheet(d: Dict[str, Any], notes: List[Dict[str, Any]]) -> str:
    a = d["acct"]
    lines = [
        f"Account: {a[0]} — {a[1] or 'n/a'} / {a[2] or 'n/a'} / status {a[3] or 'n/a'}, "
        f"customer since {a[6]}",
        f"Contacts on file: {d['contacts'][0] if d['contacts'] else 0}",
        f"Orders: {d['rev'][0]} totalling {_money(d['rev'][1])}"
        + (f", last order {d['rev'][2]}" if d['rev'][2] else ", never ordered"),
        f"Overdue AR: {d['ar'][0]} invoice(s), {_money(d['ar'][1])}"
        + (f", oldest {d['ar'][2]}d past due" if d['ar'][0] else ""),
        f"Overdue activities: {d['acts'][0] or 0}"
        + (f", last touch {d['acts'][1]}" if d['acts'][1] else ""),
    ]
    intel = d.get("intel") or ()
    if intel:
        (risk, band, ltv, rec, gap, next_due, chan, as_of,
         pref_hour, interests, senti_score, senti_label) = intel
        lines.append(
            f"Intelligence profile (nightly scorer, as of {as_of}): "
            f"churn risk {float(risk):.2f} ({band}) — {rec}d since last order vs "
            f"typical {gap}d gap; LTV {_money(ltv)}"
            + (f"; prefers {chan}" if chan else "")
            + (f", typically engages around {int(pref_hour):02d}:00 ET"
               if pref_hour is not None else "")
            + (f"; next purchase expected ~{next_due}" if next_due else ""))
        if interests:
            lines.append(f"Buys mostly: {', '.join(interests)}")
        if senti_label:
            lines.append(f"Customer-voice sentiment (90d): {senti_label} "
                         f"({float(senti_score):+.2f})")
    if d.get("custom"):
        lines.append("Custom fields: " + ", ".join(
            f"{f['label']}: {f['value']}" for f in d["custom"]))
    if d["opps"]:
        lines.append("Open opportunities:")
        for o in d["opps"]:
            lines.append(f"  - {o[0]} — {_money(o[2])} at {int(o[3])}% ({o[1]}, closes {o[4]})")
    else:
        lines.append("Open opportunities: none")
    if notes:
        lines.append("Live agent signals (shared blackboard):")
        for n in notes:
            lines.append(f"  - [{n['severity'] or 'info'}] {n['author_agent']}/{n['topic']}: {n['note']}")
    return "\n".join(lines)


def build_account_ai_summary(account_id: str) -> str:
    """Markdown summary card body: '### 🧠 <name> — AI Account Summary' + body."""
    d = _gather(account_id)
    if not d["acct"]:
        return "### 🧠 AI Account Summary\nAccount not found."

    try:
        from app.core import blackboard
        notes = blackboard.read("account", str(account_id))
    except Exception as exc:
        logger.warning(f"[ai_summary] blackboard read failed: {exc}")
        notes = []

    facts = _fact_sheet(d, notes)
    name = d["acct"][0]
    header = f"### 🧠 {name} — AI Account Summary"

    prompt = (
        "You are a CRM account strategist. Using ONLY the facts below, write a "
        "concise account summary in markdown with EXACTLY these four bold "
        "section labels:\n"
        "**Snapshot** — 1-2 sentences: who they are, relationship size.\n"
        "**Momentum** — 1-2 sentences: orders/pipeline trajectory.\n"
        "**Risks** — bullet list of concrete risks derived ONLY from the facts "
        "above (never copy this instruction); say 'None material.' if none.\n"
        "**Next actions** — 2-3 numbered, specific actions with owners implied.\n"
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
