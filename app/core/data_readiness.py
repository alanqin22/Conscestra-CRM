"""Data Readiness — is this data good ENOUGH to decide on? (P1, quality scoring).

`readiness.py` answers "does data EXIST?" (counts > 0). `data_quality.py` answers
"what specific problems exist?" (problem counts + governed fixes). Neither answers
the question an AGENT needs before it trusts a number: *how reliable is the data
under this answer, and what does it undermine?*

This module is that scoring layer — it does NOT re-detect problems from scratch;
it profiles the SAME core entities across the classic quality dimensions and
turns them into scores a human (and an agent) can act on:

    completeness · validity · uniqueness · integrity · freshness

  report()   → overall score + per-entity scores + per-dimension scores +
               a "blocks which decision" reliability map
  caveats()  → the plain-language qualifiers an agent prepends to an answer,
               e.g. "11% of accounts may be duplicates" — so the AI can say
               "revenue reporting is reliable; segmentation is moderate."

Every probe is READ-ONLY and defensive: a missing table/column degrades that one
check to `no_data` and never raises.

PROVENANCE IS NOW SCORED, not merely reported (it was deferred when this module
was written, pending the envelope). Two questions, not one:
  • is a source RECORDED?  — an unsourced value can't be judged at all;
  • is the source TRUSTWORTHY? — enrichment falls back to fabricated stub
    firmographics, and Explore segments on employee_band / revenue_band as
    dimensions. A recorded source at confidence 0.15 is not a smaller version of
    a real one; it is invented data wearing a label, and the score must say so.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection
from app.core.data_quality import _E164   # reuse the E.164 pattern (single source)

logger = logging.getLogger("data_readiness")

# Below this pass-rate a check is worth warning about (caveat + decision drag).
WARN_BELOW = float(os.getenv("DQ_WARN_BELOW", "0.90"))
FRESH_DAYS = int(os.getenv("DQ_FRESH_DAYS", "30"))   # open deals stale beyond this
_EMAIL_RE = r"^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$"

DIMENSIONS = ["completeness", "validity", "consistency", "uniqueness",
              "integrity", "freshness", "provenance"]


# ── Soft-delete guards per entity (differ across tables) ─────────────────────
_ALIVE = {
    "accounts": "COALESCE(is_deleted,false)=false",
    "contacts": "COALESCE(is_deleted,false)=false",
    "leads":    "COALESCE(is_deleted,false)=false",
    "orders":   "deleted_at IS NULL",
    # opportunities have no soft-delete column — count all (matches semantic_model)
}


def _check(entity, key, dim, label, sql, caveat=None) -> Dict[str, Any]:
    return {"entity": entity, "key": key, "dimension": dim, "label": label,
            "sql": sql, "caveat": caveat}


# ============================================================================
# THE CHECKS — each SQL returns (good_count, total_count). rate = good/total.
# ============================================================================
CHECKS: List[Dict[str, Any]] = [
    # ── accounts ──────────────────────────────────────────────────────────
    _check("accounts", "acc_owner", "completeness", "Accounts have an owner",
           "SELECT count(*) FILTER (WHERE owner_id IS NOT NULL), count(*) "
           "FROM accounts WHERE COALESCE(is_deleted,false)=false",
           "{bad_pct}% of accounts are ownerless"),
    _check("accounts", "acc_industry", "completeness", "Accounts have an industry",
           "SELECT count(*) FILTER (WHERE COALESCE(industry,'')<>''), count(*) "
           "FROM accounts WHERE COALESCE(is_deleted,false)=false",
           "{bad_pct}% of accounts have no industry (weakens segmentation)"),
    _check("accounts", "acc_email_valid", "validity", "Account emails well-formed",
           f"SELECT count(*) FILTER (WHERE email ~ '{_EMAIL_RE}'), "
           "count(*) FILTER (WHERE COALESCE(email,'')<>'') "
           "FROM accounts WHERE COALESCE(is_deleted,false)=false",
           "{bad} account email(s) are malformed"),
    _check("accounts", "acc_name_unique", "uniqueness", "Account names are unique",
           "WITH d AS (SELECT lower(trim(account_name)) k, count(*) c FROM accounts "
           "WHERE COALESCE(is_deleted,false)=false GROUP BY 1) "
           "SELECT COALESCE(sum(CASE WHEN c=1 THEN 1 ELSE 0 END),0)::int, "
           "COALESCE(sum(c),0)::int FROM d",
           "{bad_pct}% of accounts may be duplicates"),

    # ── contacts ──────────────────────────────────────────────────────────
    _check("contacts", "con_reachable", "completeness", "Contacts are reachable (email or phone)",
           "SELECT count(*) FILTER (WHERE COALESCE(email,'')<>'' OR COALESCE(phone,'')<>''), count(*) "
           "FROM contacts WHERE COALESCE(is_deleted,false)=false",
           "{bad_pct}% of contacts have no email or phone"),
    _check("contacts", "con_email_valid", "validity", "Contact emails well-formed",
           f"SELECT count(*) FILTER (WHERE email ~ '{_EMAIL_RE}'), "
           "count(*) FILTER (WHERE COALESCE(email,'')<>'') "
           "FROM contacts WHERE COALESCE(is_deleted,false)=false",
           "{bad} contact email(s) are malformed"),
    _check("contacts", "con_phone_valid", "validity", "Contact phones normalized (E.164)",
           f"SELECT count(*) FILTER (WHERE phone ~ '{_E164}'), "
           "count(*) FILTER (WHERE COALESCE(phone,'')<>'') "
           "FROM contacts WHERE COALESCE(is_deleted,false)=false",
           "{bad_pct}% of contact phones are not normalized"),
    _check("contacts", "con_account", "integrity", "Contacts link to an existing account",
           "SELECT count(*) FILTER (WHERE a.account_id IS NOT NULL), count(*) "
           "FROM contacts c LEFT JOIN accounts a ON a.account_id=c.account_id "
           "WHERE COALESCE(c.is_deleted,false)=false AND c.account_id IS NOT NULL",
           "{bad} contact(s) reference a missing account"),

    # ── leads ─────────────────────────────────────────────────────────────
    _check("leads", "lead_reachable", "completeness", "Leads are reachable (email or phone)",
           "SELECT count(*) FILTER (WHERE COALESCE(email,'')<>'' OR COALESCE(phone,'')<>''), count(*) "
           "FROM leads WHERE COALESCE(is_deleted,false)=false",
           "{bad_pct}% of leads have no email or phone"),
    _check("leads", "lead_source", "completeness", "Leads have a source",
           "SELECT count(*) FILTER (WHERE COALESCE(source,'')<>''), count(*) "
           "FROM leads WHERE COALESCE(is_deleted,false)=false",
           "{bad_pct}% of leads have no source (weakens attribution)"),
    _check("leads", "lead_email_valid", "validity", "Lead emails well-formed",
           f"SELECT count(*) FILTER (WHERE email ~ '{_EMAIL_RE}'), "
           "count(*) FILTER (WHERE COALESCE(email,'')<>'') "
           "FROM leads WHERE COALESCE(is_deleted,false)=false",
           "{bad} lead email(s) are malformed"),

    # ── opportunities ─────────────────────────────────────────────────────
    _check("opportunities", "opp_owner", "completeness", "Opportunities have an owner",
           "SELECT count(*) FILTER (WHERE owner_id IS NOT NULL), count(*) FROM opportunities",
           "{bad_pct}% of opportunities are unowned (blocks rep accountability)"),
    _check("opportunities", "opp_amount", "completeness", "Opportunities have an amount",
           "SELECT count(*) FILTER (WHERE amount IS NOT NULL AND amount>0), count(*) FROM opportunities",
           "{bad_pct}% of opportunities have no amount (skews pipeline value)"),
    _check("opportunities", "opp_close_date", "completeness", "Opportunities have a close date",
           "SELECT count(*) FILTER (WHERE close_date IS NOT NULL), count(*) FROM opportunities",
           "{bad_pct}% of opportunities have no close date (skews forecast timing)"),
    _check("opportunities", "opp_account", "integrity", "Opportunities link to an existing account",
           "SELECT count(*) FILTER (WHERE a.account_id IS NOT NULL), count(*) "
           "FROM opportunities o LEFT JOIN accounts a ON a.account_id=o.account_id",
           "{bad} opportunity(ies) reference a missing account"),
    _check("opportunities", "opp_freshness", "freshness", "Open deals updated recently",
           f"SELECT count(*) FILTER (WHERE updated_at >= now()-interval '{FRESH_DAYS} days'), "
           "count(*) FROM opportunities WHERE status='open'",
           f"{{bad_pct}}% of open deals are stale (>{FRESH_DAYS} days untouched)"),

    # ── orders ────────────────────────────────────────────────────────────
    # WHY OWNERSHIP IS MEASURED HERE NOW. This dimension covered acc_owner and
    # opp_owner — the two entities whose ownership is healthy (97% / 87%) — and
    # not orders, which sat at 3% (58 of 1,888) and had been at zero for every
    # order created since 6 January 2026. Neither order-creation path
    # (sp_orders p_mode='create', fn_generate_orders) writes owner_id at all,
    # so this is a WRITE-PATH REGRESSION, not gradual data decay.
    #
    # It went unseen for seven months because the readiness report measured the
    # entities that were fine. A completeness dimension that skips an entity
    # cannot report that entity degrading — and "Rep accountability" scored
    # well while the largest transactional table had no reps on it.
    #
    # NOTE FOR WHOEVER FIXES THE WRITE PATH: do NOT backfill owner_id from
    # accounts.owner_id. All 58 genuinely-owned orders have an owner that
    # DIFFERS from their account's owner, so order ownership is independent by
    # design; deriving it would fabricate 1,809 ownership facts and quietly
    # reassign accountability. Who owns an order is a product decision.
    _check("orders", "ord_owner", "completeness", "Orders have an owner",
           "SELECT count(*) FILTER (WHERE owner_id IS NOT NULL), count(*) "
           "FROM orders WHERE deleted_at IS NULL",
           "{bad_pct}% of orders are unowned (blocks rep accountability and "
           "any owner-scoped read policy)"),
    _check("orders", "ord_amount", "completeness", "Orders have a total amount",
           "SELECT count(*) FILTER (WHERE total_amount IS NOT NULL AND total_amount>0), count(*) "
           "FROM orders WHERE deleted_at IS NULL",
           "{bad_pct}% of orders have no total amount"),
    _check("orders", "ord_account", "integrity", "Orders link to an existing account",
           "SELECT count(*) FILTER (WHERE a.account_id IS NOT NULL), count(*) "
           "FROM orders ord LEFT JOIN accounts a ON a.account_id=ord.account_id "
           "WHERE ord.deleted_at IS NULL",
           "{bad} order(s) reference a missing account"),
    _check("orders", "ord_line_product", "integrity", "Order lines link to an existing product",
           "SELECT count(*) FILTER (WHERE p.product_id IS NOT NULL), count(*) "
           "FROM order_items oi LEFT JOIN products p ON p.product_id=oi.product_id",
           "{bad} order line(s) reference a missing product"),

    # ── cases ─────────────────────────────────────────────────────────────
    # DELIBERATELY CONDITIONAL. Raw case ownership is 26% (183/704), but that
    # number is not a defect: 487 cases are status='new', and an untriaged
    # case sitting in the queue with no owner is the CORRECT state. A flat
    # ownership check here would report a 74% failure that nobody should act
    # on — and a check that fires on normal operation is one whose next true
    # finding gets waved through.
    #
    # What is genuinely wrong is a case being WORKED with nobody working it.
    # Measured: 48 of 69 'in_progress' cases have no owner.
    _check("cases", "case_owner_active", "completeness",
           "Cases being worked have an owner",
           "SELECT count(*) FILTER (WHERE owner_id IS NOT NULL), count(*) "
           "FROM cases WHERE status IN ('in_progress','waiting')",
           "{bad} case(s) are in progress with no owner (nobody is "
           "accountable for them)"),

    # ── OWNERSHIP INTEGRITY — does the owner actually EXIST? ───────────────
    # COMPLETENESS ASKS "IS IT SET"; INTEGRITY ASKS "DOES IT RESOLVE", and
    # reading one as the other is how an audit reported "activities 100%
    # owned" about a column where 2,304 of 11,553 values point at an owner
    # that is not in the `owners` table.
    #
    # `orders`, `activities`, `leads` and `cases` carry owner_id with NO
    # FOREIGN KEY (accounts, contacts, opportunities and invoices have one),
    # so nothing has ever prevented a value that resolves to nobody. Every one
    # of the 58 populated `orders.owner_id` values is a single id that does
    # not exist — which is why they all differ from their account's owner, and
    # why that difference is not evidence of an independent ownership design.
    #
    # This matters beyond tidiness: owner_id is the subject of the deferred
    # row-level read policy. A predicate joining `owners` would silently
    # exclude these rows from everyone; one that omits the join would include
    # them for everyone. Both are wrong and neither is visible.
    _check("orders", "ord_owner_valid", "integrity",
           "Order owners exist in the owners table",
           "SELECT count(*) FILTER (WHERE w.owner_id IS NOT NULL), count(*) "
           "FROM orders o LEFT JOIN owners w ON w.owner_id=o.owner_id "
           "WHERE o.deleted_at IS NULL AND o.owner_id IS NOT NULL",
           "{bad} order(s) name an owner that does not exist"),
    _check("activities", "act_owner_valid", "integrity",
           "Activity owners exist in the owners table",
           "SELECT count(*) FILTER (WHERE w.owner_id IS NOT NULL), count(*) "
           "FROM activities a LEFT JOIN owners w ON w.owner_id=a.owner_id "
           "WHERE a.owner_id IS NOT NULL",
           "{bad} activity/activities name an owner that does not exist"),

    # ── CONSISTENCY — fields that must AGREE with each other ───────────────
    # Distinct from completeness (a value exists) and validity (it's well-formed):
    # these catch values that are individually fine but mutually contradictory —
    # the class of defect that makes two correct-looking reports disagree.
    _check("opportunities", "opp_status_stage", "consistency",
           "Closed deals have a matching closed stage",
           "SELECT count(*) FILTER (WHERE stage IN ('closed_won','closed_lost','closed_paid')), "
           "count(*) FROM opportunities WHERE status IN ('closed_won','closed_lost')",
           "{bad} closed deal(s) still carry an open stage (status and stage disagree)"),
    _check("opportunities", "opp_decided_at", "consistency",
           "Decided deals have a decision date",
           "SELECT count(*) FILTER (WHERE decided_at IS NOT NULL), count(*) "
           "FROM opportunities WHERE status IN ('closed_won','closed_lost')",
           "{bad_pct}% of decided deals have no decision date (breaks period-over-period metrics)"),
    _check("opportunities", "opp_date_order", "consistency",
           "Close date is not before the created date",
           "SELECT count(*) FILTER (WHERE close_date >= created_at::date), count(*) "
           "FROM opportunities WHERE close_date IS NOT NULL",
           "{bad} opportunity(ies) close before they were created (impossible timeline)"),
    _check("invoices", "inv_balance", "consistency",
           "Invoice balance never exceeds its total",
           "SELECT count(*) FILTER (WHERE COALESCE(balance_due,0) <= COALESCE(total_amount,0)), "
           "count(*) FROM invoices WHERE COALESCE(is_deleted,false)=false",
           "{bad} invoice(s) owe more than they bill"),

    # ── PROVENANCE — do we know WHERE values came from? ────────────────────
    # Scored only once the envelope is migrated; on a pre-provenance schema the
    # query fails and the check degrades to no_data (not a false 0%).
    _check("custom_fields", "cfv_provenance", "provenance",
           "Custom-field values record their source",
           "SELECT count(*) FILTER (WHERE source_type IS NOT NULL "
           "AND source_type <> 'unknown'), count(*) FROM custom_field_values",
           "{bad_pct}% of custom-field values have no recorded source (can't judge their trust)"),
    _check("leads", "lead_provenance", "provenance",
           "Enriched leads record where their firmographics came from",
           "SELECT count(*) FILTER (WHERE source_type IS NOT NULL "
           "AND source_type <> 'unknown'), count(*) FROM leads "
           "WHERE COALESCE(is_deleted,false)=false "
           "AND COALESCE(industry,employee_band,revenue_band) IS NOT NULL",
           "{bad_pct}% of leads carrying firmographics don't record where they came from"),
    # The value of the envelope is not that a source EXISTS — it is that a
    # low-trust source is visible. Enrichment falls back to `_stub()`, which
    # fabricates deterministic pseudo-firmographics from a hash of the domain,
    # and semantic_model exposes employee_band / revenue_band as Explore
    # DIMENSIONS. Segmenting on invented values is only safe if someone can see
    # that is what is happening.
    _check("leads", "lead_synthetic_firmographics", "provenance",
           "Firmographics come from an observed source, not a synthesized one",
           "SELECT count(*) FILTER (WHERE COALESCE(confidence,1) > 0.2), count(*) "
           "FROM leads WHERE COALESCE(is_deleted,false)=false "
           "AND COALESCE(industry,employee_band,revenue_band) IS NOT NULL",
           "{bad_pct}% of enriched leads carry SYNTHESIZED firmographics "
           "(confidence <= 0.2) — don't segment or forecast on them"),
    _check("accounts", "acc_provenance", "provenance",
           "Accounts record how they entered the CRM",
           "SELECT count(*) FILTER (WHERE source_type IS NOT NULL "
           "AND source_type <> 'unknown'), count(*) FROM accounts "
           "WHERE COALESCE(is_deleted,false)=false",
           "{bad_pct}% of accounts have no recorded origin"),
    _check("contacts", "con_provenance", "provenance",
           "Contacts record how they entered the CRM",
           "SELECT count(*) FILTER (WHERE source_type IS NOT NULL "
           "AND source_type <> 'unknown'), count(*) FROM contacts "
           "WHERE COALESCE(is_deleted,false)=false",
           "{bad_pct}% of contacts have no recorded origin"),
]


# ── Decision reliability map: which checks each business decision leans on ────
DECISIONS: List[Dict[str, Any]] = [
    {"name": "Revenue reporting", "checks": ["opp_amount", "opp_account", "ord_amount", "ord_account", "ord_line_product"]},
    {"name": "Pipeline & forecast", "checks": ["opp_amount", "opp_close_date", "opp_freshness"]},
    # ord_owner / case_owner_active are here because this decision was scoring
    # on the two entities whose ownership is healthy while the largest
    # transactional table had no reps on it at all. A decision-reliability map
    # that omits the weakest input reports confidence it has not earned.
    {"name": "Rep accountability",
     "checks": ["opp_owner", "acc_owner", "ord_owner", "case_owner_active"]},
    {"name": "Customer segmentation", "checks": ["acc_name_unique", "acc_industry", "con_account"]},
    {"name": "Email outreach", "checks": ["con_reachable", "con_email_valid", "lead_email_valid"]},
    {"name": "Contactability (calls/SMS)", "checks": ["con_reachable", "con_phone_valid", "lead_reachable"]},
]


# ============================================================================
# Execution + scoring
# ============================================================================

def _good_total(sql: str) -> Tuple[Optional[int], Optional[int]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            r = cur.fetchone()
            if not r:
                return None, None
            g, t = r[0], r[1]
            return (int(g) if g is not None else 0, int(t) if t is not None else 0)
    except Exception as exc:
        conn.rollback()
        logger.debug(f"[dq-score] check skipped: {exc}")
        return None, None
    finally:
        conn.close()


def _run_checks() -> List[Dict[str, Any]]:
    out = []
    for c in CHECKS:
        good, total = _good_total(c["sql"])
        if total is None:
            status, rate = "no_data", None
        elif total == 0:
            status, rate = "no_data", None
        else:
            rate = good / total
            status = "ok" if rate >= WARN_BELOW else "warn"
        out.append({**{k: c[k] for k in ("entity", "key", "dimension", "label", "caveat")},
                    "good": good, "total": total, "rate": rate, "status": status})
    return out


def _score(rate: Optional[float]) -> Optional[int]:
    return None if rate is None else round(100 * rate)


def _avg_rate(results: List[Dict[str, Any]]) -> Optional[float]:
    rates = [r["rate"] for r in results if r["rate"] is not None]
    return sum(rates) / len(rates) if rates else None


def _grade(score: Optional[int]) -> str:
    if score is None:
        return "unknown"
    return ("strong" if score >= 90 else "good" if score >= 75
            else "fair" if score >= 60 else "weak")


def _tier(score: Optional[int]) -> str:
    if score is None:
        return "unknown"
    return "high" if score >= 90 else "moderate" if score >= 75 else "low"


def report() -> Dict[str, Any]:
    results = _run_checks()
    by_key = {r["key"]: r for r in results}

    # per-entity
    entities: Dict[str, Any] = {}
    for r in results:
        entities.setdefault(r["entity"], []).append(r)
    entity_scores = {
        e: {"score": _score(_avg_rate(rs)),
            "checks": [{"key": r["key"], "label": r["label"], "dimension": r["dimension"],
                        "good": r["good"], "total": r["total"], "score": _score(r["rate"]),
                        "status": r["status"]} for r in rs]}
        for e, rs in entities.items()}

    # per-dimension
    dim_scores: Dict[str, Any] = {}
    for d in DIMENSIONS:
        dim_scores[d] = _score(_avg_rate([r for r in results if r["dimension"] == d]))
    # provenance is now driven by its own CHECKS (custom fields + record origin),
    # like every other dimension — no special-case override.

    # overall = mean of entity scores that have data
    es = [v["score"] for v in entity_scores.values() if v["score"] is not None]
    overall = round(sum(es) / len(es)) if es else None

    # decisions: reliability = mean of dependent checks; limiting = weakest check
    decisions = []
    for d in DECISIONS:
        rs = [by_key[k] for k in d["checks"] if k in by_key and by_key[k]["rate"] is not None]
        if not rs:
            decisions.append({"name": d["name"], "reliability": None, "tier": "unknown", "limiting": None})
            continue
        rel = _score(sum(r["rate"] for r in rs) / len(rs))
        weak = min(rs, key=lambda r: r["rate"])
        decisions.append({
            "name": d["name"], "reliability": rel, "tier": _tier(rel),
            "limiting": (weak["label"] if weak["rate"] < WARN_BELOW else None)})

    return {
        "overall_score": overall, "grade": _grade(overall),
        "entities": entity_scores, "dimensions": dim_scores,
        "decisions": decisions, "caveats": _caveats(results),
        "warn_below_pct": round(WARN_BELOW * 100),
        "note": ("Data-readiness scores 0–100 across completeness, validity, "
                 "uniqueness, integrity and freshness. Provenance is not yet "
                 "tracked (next P1 step)."),
    }


def _caveats(results: List[Dict[str, Any]]) -> List[str]:
    """Plain-language qualifiers for warn-level checks — what an agent should say
    out loud before trusting a related number."""
    lines = []
    for r in results:
        if r["status"] == "warn" and r.get("caveat") and r["rate"] is not None:
            bad = (r["total"] or 0) - (r["good"] or 0)
            bad_pct = round(100 * (1 - r["rate"]))
            try:
                lines.append(r["caveat"].format(bad=bad, bad_pct=bad_pct))
            except (KeyError, IndexError):
                lines.append(r["caveat"])
    return lines


def caveats() -> List[str]:
    """Just the qualifier lines (agent-facing helper)."""
    return _caveats(_run_checks())


# ============================================================================
# Router (admin — same posture as readiness.py)
# ============================================================================
router = APIRouter(tags=["data-readiness"])


@router.get("/data-readiness/report")
def data_readiness_report():
    """Scored data readiness across quality dimensions + decision reliability."""
    return report()


@router.get("/data-readiness/caveats")
def data_readiness_caveats():
    """The plain-language reliability qualifiers an agent prepends to answers."""
    return {"caveats": caveats()}
