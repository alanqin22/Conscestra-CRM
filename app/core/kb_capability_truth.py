"""Capability truth model — derived from implementation, never from the KB.

The Phase 2 audit found articles asserting that capabilities did not exist,
because the check looked for TABLES named '%merge%'. Phase 3 found the mirror
error: an article that correctly says a capability EXISTS, read as meaning it
runs automatically. Both come from treating "capability" as one bit.

It is at least four:

    supported?        does the product do this at all
    automatic?        does it happen without anyone asking
    scheduled?        does it run on a timer
    user_initiated?   does it require an explicit request

"Contact merge is supported" and "contacts get merged nightly" are different
claims, and only the first is true. Keeping the dimensions apart is what lets a
test assert the difference, and what lets CI fail when an article blurs them.

EVIDENCE is a required field. Every row cites the stored-procedure mode, column
or code path it was read from, so a reviewer can re-derive it without trusting
this file. Nothing here may be sourced from knowledge_articles.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# object -> capability -> facts
TRUTH: Dict[str, Dict[str, Dict[str, Any]]] = {}


def _cap(obj: str, cap: str, supported: bool, automatic: bool, scheduled: bool,
         user_initiated: bool, evidence: str, side_effects: str = "") -> None:
    TRUTH.setdefault(obj, {})[cap] = {
        "supported": supported, "automatic": automatic, "scheduled": scheduled,
        "user_initiated": user_initiated, "evidence": evidence,
        "side_effects": side_effects,
    }


# ── MERGE — the capability the audits kept getting wrong in both directions ──
_cap("contact", "merge", True, False, False, True,
     "sp_contacts p_mode 'merge' (by_email|by_phone); app/agents/contacts/"
     "sql_builder.py:218 validates operation; formatter.py:552 renders result",
     "keeps one master, soft-deletes duplicates, reassigns related records/addresses")
_cap("account", "merge", True, False, False, True, "sp_accounts p_mode 'merge'",
     "keeps master, reassigns related records")
_cap("lead", "merge", True, False, False, True, "sp_leads p_mode 'merge'")
_cap("opportunity", "merge", False, False, False, False,
     "sp_opportunities mode list has no 'merge'")
_cap("order", "merge", False, False, False, False,
     "sp_orders mode list has no 'merge'")
_cap("invoice", "merge", False, False, False, False,
     "no sp_invoices merge mode")

# ── DUPLICATE DETECTION — finder exists; nothing merges on its own ──────────
_cap("contact", "duplicates", True, False, False, True,
     "sp_contacts p_mode 'duplicates'; no scheduled job references it")
_cap("account", "duplicates", True, False, False, True, "sp_accounts p_mode 'duplicates'")
_cap("lead", "duplicates", True, False, False, True, "sp_leads p_mode 'duplicates'")
# Absences stated explicitly. A missing ROW means "unknown" and the firewall
# stays silent; only an explicit supported=False lets it correct the user. Two
# boundary questions ("duplicates finder for orders?", "merge duplicate
# products?") fell through to an unrelated dashboard purely because these rows
# did not exist.
_cap("order", "duplicates", False, False, False, False,
     "sp_orders mode list has no 'duplicates'")
_cap("invoice", "duplicates", False, False, False, False,
     "sp_accounting has no duplicates mode")
_cap("opportunity", "duplicates", False, False, False, False,
     "sp_opportunities mode list has no 'duplicates'")
_cap("product", "merge", False, False, False, False,
     "sp_products mode list has no 'merge'")
_cap("product", "duplicates", False, False, False, False,
     "sp_products mode list has no 'duplicates'")

# ── LIFECYCLE ───────────────────────────────────────────────────────────────
_cap("contact", "archive", True, False, False, True, "sp_contacts p_mode 'archive'")
_cap("contact", "restore", True, False, False, True, "sp_contacts p_mode 'restore'")
_cap("account", "archive", True, False, False, True, "sp_accounts p_mode 'archive'")
_cap("lead", "convert", True, False, False, True, "sp_leads p_mode 'convert'")
_cap("lead", "qualify", True, False, False, True,
     "sp_leads p_mode 'qualify' — NOT called by agent_bus.handle_lead_created, "
     "which performs enrichment only")
_cap("lead", "score", True, False, False, True,
     "sp_leads p_mode 'score'; leads.score column — not auto-computed on create")
_cap("lead", "disqualify", True, False, False, True, "sp_leads p_mode 'disqualify'")
_cap("lead", "enrichment", True, True, False, False,
     "agent_bus.handle_lead_created -> enrichment.enrich_company on lead.created",
     "writes firmographics to blackboard at confidence 0.15")

# ── OPPORTUNITY ─────────────────────────────────────────────────────────────
_cap("opportunity", "add_product", True, False, False, True,
     "sp_opportunities p_mode 'add_product'")
_cap("opportunity", "forecast", True, False, False, True,
     "sp_opportunities p_mode 'forecast' / 'forecast_accuracy' / 'win_rate'")
_cap("opportunity", "loss_reason_field", False, False, False, False,
     "no lost_reason/loss_reason column on opportunities")
_cap("opportunity", "multiple_pipelines", False, False, False, False,
     "no pipeline_id/pipeline_name in app/ or schema; single stage enum")

# ── ACTIVITIES ──────────────────────────────────────────────────────────────
_cap("activity", "log_call", True, False, False, True, "sp_activities p_mode 'log_call'")
_cap("activity", "schedule_meeting", True, False, False, True,
     "sp_activities p_mode 'schedule_meeting'")
_cap("activity", "overdue", True, False, False, True, "sp_activities p_mode 'overdue'")
_cap("activity", "create_task", True, True, False, True,
     "sp_activities p_mode 'create_task'; ALSO raised automatically by "
     "workflow_rules on lead.created / opportunity.created / invoice.overdue")

# ── EMAIL ───────────────────────────────────────────────────────────────────
_cap("order", "lifecycle_email", True, True, False, False,
     "order_notifications; created/shipped/delivered on state transition",
     "UNIQUE(order_id,event_type) idempotency")
_cap("invoice", "payment_reminder", True, True, True, False,
     "scheduled dunning job; app/agents/email/structured.py reminder window")
_cap("lead", "welcome_email", True, False, False, True,
     "send_template templateType welcome — NOT sent by lead.created")

# ── PLATFORM-WIDE NEGATIVES ─────────────────────────────────────────────────
_cap("system", "saved_dashboards", False, False, False, False,
     "no dashboard tables; sp_home_index provides fixed KPI cards only")
_cap("system", "reminder_object", False, False, False, False,
     "no %remind% tables; reminders are tasks with due dates")
_cap("system", "permissions_editor", False, False, False, False,
     "no permission/role tables; employees.role is the unit of access")
_cap("system", "nightly_dedupe", False, False, False, False,
     "no scheduled job referencing duplicates/merge in scheduler or main")
_cap("system", "multi_currency", False, False, False, False,
     "no currency column on invoices")
_cap("system", "territory_quota", False, False, False, False,
     "no territory/quota tables or SP modes")
_cap("system", "audit_log", True, True, False, False, "audit_log table, append-only")


def get(obj: str, cap: str) -> Optional[Dict[str, Any]]:
    return TRUTH.get(obj, {}).get(cap)


def supports(obj: str, cap: str) -> bool:
    row = get(obj, cap)
    return bool(row and row["supported"])


def is_automatic(obj: str, cap: str) -> bool:
    row = get(obj, cap)
    return bool(row and row["automatic"])


def objects_supporting(cap: str) -> List[str]:
    return sorted(o for o, caps in TRUTH.items()
                  if caps.get(cap, {}).get("supported"))


def objects_not_supporting(cap: str) -> List[str]:
    return sorted(o for o, caps in TRUTH.items()
                  if cap in caps and not caps[cap]["supported"])


def automatic_capabilities() -> List[str]:
    return sorted(f"{o}.{c}" for o, caps in TRUTH.items()
                  for c, f in caps.items() if f["automatic"])
