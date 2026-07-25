"""Context hydration — agents "born with context" (advanced improvement #1).

Every piece of customer intelligence Conscestra computes — the nightly
profile, blackboard signals, open money, running cadences, pending approvals,
last touches — already exists, but each consumer used to fetch its own slice
(or none). This module assembles ONE compact, deterministic context pack per
entity and injects it wherever an agent starts work, so agents open the
conversation already knowing who they're dealing with:

    hydrate(entity_type, id)  → structured pack (sections best-effort)
    render(pack)              → ≤12-line plain-text block for LLM prompts
    render_for(type, id)      → hydrate+render honoring the kill switch
    render_for_email(sender)  → resolve sender → contact/lead → block

INJECTION POINTS
    • A2A dispatch (NL path): requests carrying an entity get the block
      prepended to the agent message.
    • Email auto-reply: the matched sender's pack personalizes the reply
      (with an explicit instruction never to reveal internal scores).
    • GET /context/{entity_type}/{entity_id} — inspection + frontend use.
    • A2A capability `crm.context` — any agent can ask for a peer-visible
      360 of an entity through the typed protocol.

Read-only, no LLM, every section tolerates its table being absent. The
whole layer is behind CONTEXT_HYDRATION_ENABLED (default ON — it has no
side effects; set 0 to kill instantly).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import execute_sp

logger = logging.getLogger("context")


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("CONTEXT_HYDRATION_ENABLED", "1")

_NOTE_CHARS = 70      # blackboard note preview length in the rendered block
_MAX_SIGNALS = 6


def _rows(sql: str, params=None) -> List[Dict[str, Any]]:
    try:
        return execute_sp(sql, params)
    except Exception as exc:
        logger.debug(f"[context] section skipped: {exc}")
        return []


def _money(v) -> str:
    try:
        return f"${float(v or 0):,.0f}"
    except (TypeError, ValueError):
        return "$0"


# ============================================================================
# HYDRATE — sections are independent and best-effort
# ============================================================================

def _signals(entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
    try:
        from app.core import blackboard
        notes = blackboard.read(entity_type, entity_id)
        return [{"topic": n.get("topic"), "author": n.get("author_agent"),
                 "severity": n.get("severity"),
                 "note": str(n.get("note") or "")[:_NOTE_CHARS]}
                for n in notes[:_MAX_SIGNALS]]
    except Exception as exc:
        logger.debug(f"[context] blackboard skipped: {exc}")
        return []


def _cadences(entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
    return _rows(
        "SELECT playbook, step_no, next_step_at::date::text AS next_step_at "
        "FROM agent_sequences WHERE entity_type=%(t)s AND entity_uuid=%(id)s::uuid "
        "AND status='active'", {"t": entity_type, "id": entity_id})


def _approvals(entity_id: str) -> List[Dict[str, Any]]:
    return _rows(
        "SELECT action_type, critique->>'stance' AS critic FROM action_approvals "
        "WHERE entity_id=%(id)s::uuid AND status='pending' "
        "AND (expires_at IS NULL OR expires_at > now())", {"id": entity_id})


def _touches(col: str, entity_id: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for direction in ("inbound", "outbound"):
        r = _rows(
            f"SELECT type, COALESCE(subject,'') AS subject, "
            f"created_at::date::text AS on_date FROM activities "
            f"WHERE {col}=%(id)s::uuid AND direction=%(d)s "
            f"ORDER BY created_at DESC LIMIT 1",
            {"id": entity_id, "d": direction})
        if r:
            out[direction] = r[0]
    return out


def _hydrate_account(account_id: str) -> Optional[Dict[str, Any]]:
    ident = _rows(
        "SELECT account_name, status, industry FROM accounts "
        "WHERE account_id=%(id)s::uuid AND COALESCE(is_deleted,false)=false",
        {"id": account_id})
    if not ident:
        return None
    pack: Dict[str, Any] = {
        "entity_type": "account", "entity_id": account_id,
        "display": ident[0]["account_name"], "identity": ident[0]}

    prof = _rows(
        "SELECT churn_band, churn_risk::float AS churn_risk, ltv::float AS ltv, "
        "       order_recency_days, typical_gap_days, preferred_channel, "
        "       preferred_hour, interests, sentiment_label, "
        "       next_purchase_due::text AS next_purchase_due, "
        "       open_ar_balance::float AS open_ar_balance, overdue_invoices "
        "FROM account_intelligence WHERE account_id=%(id)s::uuid",
        {"id": account_id})
    if prof:
        pack["profile"] = prof[0]

    open_items: Dict[str, Any] = {}
    r = _rows("SELECT count(*) AS n, COALESCE(SUM(amount),0)::float AS value "
              "FROM opportunities WHERE account_id=%(id)s::uuid AND status='open'",
              {"id": account_id})
    if r and int(r[0].get("n") or 0):
        open_items["open_deals"] = r[0]
    r = _rows("SELECT count(*) AS n, COALESCE(SUM(balance_due),0)::float AS value "
              "FROM invoices WHERE account_id=%(id)s::uuid AND status='overdue' "
              "AND deleted_at IS NULL", {"id": account_id})
    if r and int(r[0].get("n") or 0):
        open_items["overdue_invoices"] = r[0]
    r = _rows("SELECT count(*) AS n FROM activities "
              "WHERE account_id=%(id)s::uuid AND status='open'", {"id": account_id})
    if r and int(r[0].get("n") or 0):
        open_items["open_activities"] = int(r[0]["n"])
    pack["open_items"] = open_items

    pack["signals"] = _signals("account", account_id)
    pack["cadences"] = _cadences("account", account_id)
    pack["pending_approvals"] = _approvals(account_id)
    pack["last_touches"] = _touches("account_id", account_id)
    return pack


def _hydrate_lead(lead_id: str) -> Optional[Dict[str, Any]]:
    ident = _rows(
        "SELECT COALESCE(NULLIF(TRIM(COALESCE(first_name,'')||' '||"
        "COALESCE(last_name,'')),''), company) AS display, company, email, "
        "score, status, rating FROM leads "
        "WHERE lead_id=%(id)s::uuid AND deleted_at IS NULL", {"id": lead_id})
    if not ident:
        return None
    pack: Dict[str, Any] = {
        "entity_type": "lead", "entity_id": lead_id,
        "display": ident[0]["display"], "identity": ident[0]}
    try:
        # Predictive model first (scoring v2, governed activation); fall back
        # to the band-history heuristic when no model is active.
        from app.core import scoring
        pred = scoring.predict_for(lead_id)
        if pred:
            pack["win_probability"] = float(pred["probability"])
        else:
            from app.core import qualification
            wp = qualification.win_probability(int(ident[0].get("score") or 0))
            if wp and wp.get("probability") is not None:
                pack["win_probability"] = float(wp["probability"])
    except Exception as exc:
        logger.debug(f"[context] win probability skipped: {exc}")
    pack["signals"] = _signals("lead", lead_id)
    pack["cadences"] = _cadences("lead", lead_id)
    pack["pending_approvals"] = _approvals(lead_id)
    pack["last_touches"] = _touches("lead_id", lead_id)
    return pack


def _attach_memory(pack: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Unified customer memory: recent cross-channel conversations + open
    commitments join the pack, so every consumer (A2A, email auto-reply,
    AI summaries) knows what was last said to this customer — on ANY
    channel. Best-effort, like every other section."""
    if pack:
        try:
            from app.core import customer_memory
            mem = customer_memory.recall(pack["entity_type"],
                                         pack["entity_id"], limit=3)
            if mem["interactions"] or mem["open_commitments"]:
                pack["recent_interactions"] = mem
        except Exception as exc:
            logger.debug(f"[context] customer memory skipped: {exc}")
    return pack


def hydrate(entity_type: str, entity_id: str) -> Optional[Dict[str, Any]]:
    """Compact context pack for an entity. Contacts resolve to their account.
    None when the entity doesn't exist (or the type is unsupported)."""
    et = (entity_type or "").lower()
    if et == "contact":
        r = _rows("SELECT account_id::text AS account_id FROM contacts "
                  "WHERE contact_id=%(id)s::uuid "
                  "AND COALESCE(is_deleted,false)=false", {"id": entity_id})
        if not (r and r[0].get("account_id")):
            return None
        pack = _hydrate_account(r[0]["account_id"])
        if pack:
            pack["via_contact"] = entity_id
        return _attach_memory(pack)
    if et == "account":
        pack = _hydrate_account(entity_id)
        _attach_custom_fields(pack, "accounts", entity_id)
    elif et == "lead":
        pack = _hydrate_lead(entity_id)
        _attach_custom_fields(pack, "leads", entity_id)
    else:
        return None
    if pack:
        pack["as_of"] = datetime.now(timezone.utc).isoformat()
    return _attach_memory(pack)


def _attach_custom_fields(pack, entity: str, entity_id: str) -> None:
    """Agent-aware custom fields (P3): admin-defined extra fields for this record
    so agents are 'born' knowing them. Best-effort — never breaks hydration."""
    if not pack:
        return
    try:
        from app.core import custom_fields
        cf = custom_fields.get_values_labeled(entity, entity_id)
        if cf:
            pack["custom_fields"] = cf
    except Exception as exc:
        logger.debug(f"[context] custom fields skipped: {exc}")


# ============================================================================
# RENDER — the ≤12-line block agents actually read
# ============================================================================

def render(pack: Optional[Dict[str, Any]]) -> str:
    if not pack:
        return ""
    lines = [f"[CRM CONTEXT — {pack['display']} ({pack['entity_type']})]"]

    p = pack.get("profile")
    if p:
        bits = []
        if p.get("churn_band"):
            bits.append(f"churn {p['churn_band'].upper()} ({p.get('churn_risk', 0):.2f})")
        if p.get("ltv"):
            bits.append(f"LTV {_money(p['ltv'])}")
        if p.get("order_recency_days") is not None:
            gap = p.get("typical_gap_days")
            bits.append(f"{p['order_recency_days']}d since last order"
                        + (f" (typical {gap}d)" if gap else ""))
        if p.get("preferred_channel"):
            hour = p.get("preferred_hour")
            bits.append(f"prefers {p['preferred_channel']}"
                        + (f" ~{hour}:00 ET" if hour is not None else ""))
        if p.get("sentiment_label"):
            bits.append(f"sentiment {p['sentiment_label']}")
        if bits:
            lines.append("Profile: " + " · ".join(bits))
        if p.get("interests"):
            lines.append(f"Buys: {', '.join(list(p['interests'])[:3])}"
                         + (f" · next purchase due {p['next_purchase_due']}"
                            if p.get("next_purchase_due") else ""))

    ident = pack.get("identity") or {}
    if pack["entity_type"] == "lead":
        bits = [f"score {ident.get('score')}" if ident.get("score") is not None else "",
                f"rating {ident.get('rating')}" if ident.get("rating") else "",
                f"status {ident.get('status')}" if ident.get("status") else ""]
        if pack.get("win_probability") is not None:
            bits.append(f"win probability {pack['win_probability']:.0%}")
        bits = [b for b in bits if b]
        if bits:
            lines.append("Lead: " + " · ".join(bits))

    cf = pack.get("custom_fields") or []
    if cf:
        lines.append("Custom: " + " · ".join(
            f"{f['label']}: {f['value']}" for f in cf[:6]))

    sig = pack.get("signals") or []
    if sig:
        lines.append("Signals: " + " · ".join(
            f"{n['topic']} ({n['author']}): \"{n['note']}\"" for n in sig[:3]))

    oi = pack.get("open_items") or {}
    bits = []
    if oi.get("open_deals"):
        bits.append(f"{oi['open_deals']['n']} open deal(s) "
                    f"({_money(oi['open_deals']['value'])})")
    if oi.get("overdue_invoices"):
        bits.append(f"{oi['overdue_invoices']['n']} OVERDUE invoice(s) "
                    f"({_money(oi['overdue_invoices']['value'])})")
    if oi.get("open_activities"):
        bits.append(f"{oi['open_activities']} open activit(ies)")
    if bits:
        lines.append("Open: " + " · ".join(bits))

    cads = pack.get("cadences") or []
    if cads:
        lines.append("Cadences: " + " · ".join(
            f"{c['playbook']} step {c['step_no']} (next {c['next_step_at']})"
            for c in cads))

    aps = pack.get("pending_approvals") or []
    if aps:
        lines.append("Approvals pending: " + " · ".join(
            f"{a['action_type']}" + (f" (critic: {a['critic']})" if a.get("critic") else "")
            for a in aps))

    lt = pack.get("last_touches") or {}
    bits = [f"{d} {t['type']} \"{t['subject'][:40]}\" {t['on_date']}"
            for d, t in lt.items()]
    if bits:
        lines.append("Last touch: " + " · ".join(bits))

    mem = pack.get("recent_interactions") or {}
    for r in (mem.get("interactions") or [])[:2]:
        res = ("resolved" if r.get("resolved") else
               "UNRESOLVED" if r.get("resolved") is False else "")
        lines.append(f"Recent {r['channel']} conversation ({r['on_date']}): "
                     f"{str(r['summary'])[:90]}" + (f" [{res}]" if res else ""))
    if mem.get("open_commitments"):
        lines.append("Owed to customer: " + " · ".join(
            f"{c['what'][:60]} (since {c['since']})"
            for c in mem["open_commitments"][:2]))

    return "\n".join(lines)


def render_for(entity_type: str, entity_id: str) -> str:
    """hydrate+render honoring the kill switch — the one-call injection API.
    The block is PII-masked: it feeds LLM prompts (a2a NL dispatch,
    auto-reply), and emails/phones inside note previews or activity subjects
    add nothing to the model's judgment."""
    if not ENABLED or not entity_id:
        return ""
    try:
        rendered = render(hydrate(entity_type, entity_id))
        try:
            from app.core import privacy
            return privacy.mask(rendered)
        except Exception:
            return rendered
    except Exception as exc:
        logger.debug(f"[context] render_for failed: {exc}")
        return ""


def render_for_email(sender_email: str) -> str:
    """Resolve an inbound sender to their contact(account)/lead and render the
    pack — personalizes the autonomous auto-reply."""
    if not ENABLED or not sender_email:
        return ""
    try:
        from app.agents.email.inbound_bridge import _resolve_sender_sync
        who = _resolve_sender_sync(sender_email.strip().lower())
        if not who:
            return ""
        if who["kind"] == "account":
            return render_for("account", who["account_id"])
        return render_for("lead", who["lead_id"])
    except Exception as exc:
        logger.debug(f"[context] render_for_email failed: {exc}")
        return ""


# ============================================================================
# Admin endpoint
# ============================================================================

router = APIRouter(tags=["context"])


@router.get("/context/{entity_type}/{entity_id}")
def context_get(entity_type: str, entity_id: str):
    pack = hydrate(entity_type, entity_id)
    return {"enabled": ENABLED, "pack": pack, "rendered": render(pack)}
