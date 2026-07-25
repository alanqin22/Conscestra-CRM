"""Industry Starter Packs (Platform blindspot P5).

Salesforce is horizontal at its core but sells industry products. Conscestra
stays horizontal — the CRM is identical — and adapts to a vertical by SEEDING
content on the substrate we already have. A starter pack is a one-click bundle of:

    Custom fields (P3)   — the vertical's own data fields
    Knowledge (KB)       — the vertical's policies/FAQ, grounding the agents
    Objectives           — the vertical's goals (on our registered metrics)
    A custom agent (#3)  — a vertical Q&A assistant, grounded in that KB

Nothing here is a new schema or new code path — every piece REUSES an existing
writer: `custom_fields.create_def`, `knowledge.publish` (idempotent via
source_ref), `objectives.create` (registered metrics only), `custom_agents.upsert`.
So a pack is pure data, applied idempotently and fully removable.

CONFIG: INDUSTRY_PACKS_ENABLED (default 1).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("industry_packs")

ENABLED = os.getenv("INDUSTRY_PACKS_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")


def _kb(title, problem, answer, keywords):
    return {"title": title, "problem": problem, "answer": answer, "keywords": keywords}


def _obj(name, metric, direction, target, play=None, horizon_days=90):
    return {"name": name, "metric": metric, "direction": direction,
            "target_value": target, "play": play, "horizon_days": horizon_days}


def _cf(entity, key, label, ftype, options=None):
    return {"entity": entity, "field_key": key, "label": label,
            "field_type": ftype, "options": options or []}


# ============================================================================
# PACK REGISTRY (pure data)
# ============================================================================
PACKS: Dict[str, Dict[str, Any]] = {

    "saas": {
        "label": "SaaS / Software",
        "description": "Subscription software: plan tiers, MRR, renewals, churn & conversion goals, a success assistant.",
        "custom_fields": [
            _cf("accounts", "plan_tier", "Plan Tier", "select", ["Free", "Pro", "Enterprise"]),
            _cf("accounts", "mrr", "MRR", "number"),
            _cf("accounts", "renewal_date", "Renewal Date", "date"),
            _cf("accounts", "seats", "Seats", "number"),
            _cf("accounts", "health_score", "Health", "select", ["Green", "Yellow", "Red"]),
        ],
        "kb_articles": [
            _kb("Pricing & Plans", "How much does it cost / what plans are there?",
                "We offer three plans. Free includes core features for a single user. Pro adds "
                "team collaboration, integrations and priority support, billed monthly or annually. "
                "Enterprise adds SSO, advanced security, a dedicated success manager and a custom "
                "contract. Annual billing saves roughly two months versus monthly.",
                ["pricing", "plans", "cost", "tier"]),
            _kb("Security & Compliance", "Is my data secure? Do you have SOC 2?",
                "Data is encrypted in transit and at rest. Access is role-based and audit-logged. "
                "We maintain least-privilege access controls and support SSO on Enterprise. Our "
                "Trust Center documents our current control inventory and data-residency posture; "
                "a security questionnaire and DPA are available for Enterprise customers.",
                ["security", "soc2", "compliance", "privacy", "sso"]),
            _kb("Onboarding & Setup", "How do I get started / onboard my team?",
                "Start by importing your accounts and contacts, then invite your team and assign "
                "roles. Connect email so the assistant can classify and route messages. Most teams "
                "are live within a day; Enterprise onboarding includes a guided implementation and "
                "a success plan tied to your goals.",
                ["onboarding", "setup", "getting started", "implementation"]),
            _kb("Cancellation & Renewal", "How do renewals and cancellation work?",
                "Subscriptions renew automatically at the end of each term. You can change or cancel "
                "before the renewal date from billing settings; access continues until the end of the "
                "paid term. Annual plans can be prorated on upgrade. Reach out before renewal to "
                "discuss expansion or a plan change.",
                ["cancel", "renewal", "billing", "refund"]),
        ],
        "objectives": [
            _obj("Grow recurring revenue (30d)", "revenue_30d", "up", None),
            _obj("Reduce customer churn", "high_churn_accounts", "down", None, play="churn_campaign"),
            _obj("Improve trial→paid conversion", "lead_conversion_rate_30d", "up", None, play="leads"),
        ],
        "custom_agent": {
            "slug": "saas-success", "display_name": "SaaS Success Assistant",
            "description": "Answers pricing, security, onboarding and renewal questions.",
            "instructions": "You are a friendly customer-success assistant for a SaaS product. "
                            "Answer questions about pricing, plans, security/compliance, onboarding "
                            "and renewals using ONLY the approved knowledge base. Be concise and "
                            "helpful; when unsure, offer to connect a human.",
            "scope": "external", "kb_audience": "public",
            "examples": ["What plans do you offer?", "Are you SOC 2 compliant?",
                         "How do I onboard my team?", "How do renewals work?"],
        },
    },

    "real_estate": {
        "label": "Real Estate",
        "description": "Brokerage / agent: buyer leads with budget & property type, listing fields, a buyer concierge.",
        "custom_fields": [
            _cf("leads", "budget", "Budget", "number"),
            _cf("leads", "property_type", "Property Type", "select", ["Residential", "Commercial", "Land"]),
            _cf("leads", "preferred_area", "Preferred Area", "text"),
            _cf("leads", "pre_approved", "Mortgage Pre-Approved", "bool"),
            _cf("opportunities", "mls_number", "MLS #", "text"),
            _cf("opportunities", "closing_date", "Closing Date", "date"),
            _cf("opportunities", "listing_price", "Listing Price", "number"),
        ],
        "kb_articles": [
            _kb("The Buying Process", "What are the steps to buy a home?",
                "The typical path is: get pre-approved for a mortgage, define your budget and must-haves, "
                "tour listings, make an offer, complete inspection and financing conditions, then close. "
                "Your agent coordinates each step and negotiates on your behalf. Timelines vary by market "
                "but most closings complete within 30–60 days of an accepted offer.",
                ["buying", "process", "steps", "home"]),
            _kb("Mortgage Pre-Approval", "How do I get pre-approved for a mortgage?",
                "Pre-approval means a lender has reviewed your income, credit and down payment and confirmed "
                "how much you can borrow. It makes your offers stronger. Bring recent pay stubs, tax "
                "documents and a credit check. Pre-approval is typically valid for 90–120 days.",
                ["mortgage", "pre-approval", "financing", "lender"]),
            _kb("Closing Costs & Commission", "What fees are involved in a purchase?",
                "Beyond the purchase price, budget for closing costs — land transfer tax, legal fees, title "
                "insurance and inspection. Agent commission is customarily paid by the seller. Ask your agent "
                "for an estimate specific to your price range and location before you make an offer.",
                ["closing costs", "commission", "fees", "taxes"]),
        ],
        "objectives": [
            _obj("Work new buyer leads faster", "new_lead_backlog", "down", None, play="leads"),
            _obj("Improve lead→client conversion", "lead_conversion_rate_30d", "up", None, play="leads"),
        ],
        "custom_agent": {
            "slug": "real-estate-concierge", "display_name": "Real Estate Concierge",
            "description": "Answers buyer questions about the process, financing and closing.",
            "instructions": "You are a helpful real-estate concierge for prospective buyers. Explain the "
                            "buying process, mortgage pre-approval and closing costs using ONLY the approved "
                            "knowledge base. Be warm and clear; offer to connect an agent for specifics.",
            "scope": "external", "kb_audience": "public",
            "examples": ["How do I start buying a home?", "How does mortgage pre-approval work?",
                         "What are closing costs?"],
        },
    },

    "music_school": {
        "label": "Music / Piano School",
        "description": "Studio: students with instrument, RCM level, teacher & exam date; enrollment goals; a school assistant.",
        "custom_fields": [
            _cf("contacts", "instrument", "Instrument", "select", ["Piano", "Violin", "Guitar", "Voice", "Cello", "Flute"]),
            _cf("contacts", "rcm_level", "RCM Level", "select",
                ["Prep", "Level 1", "Level 2", "Level 3", "Level 4", "Level 5",
                 "Level 6", "Level 7", "Level 8", "Level 9", "Level 10", "ARCT"]),
            _cf("contacts", "teacher", "Teacher", "text"),
            _cf("contacts", "lesson_day", "Lesson Day", "select", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]),
            _cf("contacts", "exam_date", "Next Exam", "date"),
        ],
        "kb_articles": [
            _kb("Lessons & Tuition", "How do lessons and tuition work?",
                "Lessons are weekly, private and scheduled with an assigned teacher. Tuition is billed "
                "monthly and covers the term's lessons. A make-up lesson is offered for absences given "
                "24 hours' notice; missed lessons without notice are not refunded. Instruments and books "
                "are the student's responsibility unless otherwise arranged.",
                ["tuition", "lessons", "schedule", "make-up", "policy"]),
            _kb("RCM Exams & Levels", "How do RCM levels and exams work?",
                "Students progress through Royal Conservatory (RCM) levels from Preparatory through Level 10 "
                "and the ARCT diploma. Each level has repertoire, technique, ear-training and theory "
                "requirements. Practical exams are held in set sessions each year; your teacher recommends "
                "when a student is ready and registers them. Theory co-requisites apply from Level 5 up.",
                ["rcm", "exam", "level", "royal conservatory", "grade"]),
            _kb("Enrollment & Trial Lessons", "How do I enroll or book a trial lesson?",
                "New students start with a trial lesson so we can assess level and match a teacher. To enroll, "
                "share the student's name, instrument, any prior experience and preferred lesson days. We then "
                "confirm a teacher and a weekly time slot. Enrollment is confirmed once the first month's "
                "tuition is arranged.",
                ["enroll", "trial", "register", "sign up", "new student"]),
            _kb("Recitals & Practice", "What about recitals and practice expectations?",
                "We hold student recitals twice a year to build performance confidence; participation is "
                "encouraged but optional. Consistent daily practice — even 15–30 minutes for beginners — drives "
                "progress far more than lesson length. Your teacher sets weekly practice goals appropriate to "
                "the level.",
                ["recital", "practice", "performance"]),
        ],
        "objectives": [
            _obj("Grow enrollment (convert inquiries)", "lead_conversion_rate_30d", "up", None, play="leads"),
            _obj("Reduce student attrition", "high_churn_accounts", "down", None, play="churn_campaign"),
        ],
        "custom_agent": {
            "slug": "music-school-assistant", "display_name": "Music School Assistant",
            "description": "Answers enrollment, lesson, tuition and RCM exam questions.",
            "instructions": "You are a warm assistant for a music school. Answer questions about enrolling, "
                            "trial lessons, tuition, lesson policies and RCM exams/levels using ONLY the "
                            "approved knowledge base. Be encouraging; offer to book a trial lesson or connect "
                            "a human for scheduling.",
            "scope": "external", "kb_audience": "public",
            "examples": ["How do I book a trial lesson?", "How does tuition work?",
                         "How do RCM exams work?", "What instruments do you teach?"],
        },
    },
}


# ============================================================================
# APPLY / STATUS / REMOVE
# ============================================================================

def list_packs() -> Dict[str, Any]:
    return {"enabled": ENABLED, "packs": [
        {"id": pid, "label": p["label"], "description": p["description"],
         "counts": {"custom_fields": len(p["custom_fields"]),
                    "kb_articles": len(p["kb_articles"]),
                    "objectives": len(p["objectives"]),
                    "custom_agent": 1 if p.get("custom_agent") else 0}}
        for pid, p in PACKS.items()]}


def _obj_exists(name: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM business_objectives WHERE name=%s AND status='active' LIMIT 1", (name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def apply(pack_id: str) -> Dict[str, Any]:
    if not ENABLED:
        return {"ok": False, "error": "industry packs are disabled"}
    pack = PACKS.get(pack_id)
    if not pack:
        return {"ok": False, "error": f"unknown pack '{pack_id}'. Valid: {', '.join(PACKS)}"}

    from app.core import custom_fields, knowledge, objectives, custom_agents

    result = {"custom_fields": 0, "kb_articles": {"created": 0, "skipped": 0},
              "objectives": {"created": 0, "skipped": 0}, "custom_agent": None, "errors": []}

    # 1) Custom fields (P3) — idempotent upsert.
    for f in pack["custom_fields"]:
        try:
            custom_fields.create_def(f)
            result["custom_fields"] += 1
        except Exception as exc:
            result["errors"].append(f"custom_field {f['field_key']}: {str(exc)[:120]}")

    # 2) KB articles — idempotent via a pack-scoped source_ref.
    for i, art in enumerate(pack["kb_articles"]):
        try:
            r = knowledge.publish({**art, "source": "industry_pack",
                                   "source_ref": f"pack:{pack_id}:{i}"},
                                  created_by=f"pack:{pack_id}")
            result["kb_articles"]["created" if r.get("ok") else "skipped"] += 1
        except Exception as exc:
            result["errors"].append(f"kb {i}: {str(exc)[:120]}")

    # 3) Objectives — dedupe by name; best-effort per item.
    for o in pack["objectives"]:
        try:
            if _obj_exists(o["name"]):
                result["objectives"]["skipped"] += 1
                continue
            spec = objectives.ObjectiveCreate(
                name=o["name"], metric=o["metric"], direction=o["direction"],
                target_value=o["target_value"] if o["target_value"] is not None else _default_target(o),
                horizon_days=o.get("horizon_days", 90), play=o.get("play"))
            objectives.create(spec, created_by=f"pack:{pack_id}")
            result["objectives"]["created"] += 1
        except Exception as exc:
            result["errors"].append(f"objective {o['name']}: {str(exc)[:120]}")

    # 4) Custom agent — idempotent upsert.
    if pack.get("custom_agent"):
        try:
            r = custom_agents.upsert({**pack["custom_agent"], "created_by": f"pack:{pack_id}"})
            result["custom_agent"] = r.get("slug") or pack["custom_agent"]["slug"]
        except Exception as exc:
            result["errors"].append(f"custom_agent: {str(exc)[:120]}")

    logger.info(f"[industry_packs] applied '{pack_id}': {result}")
    return {"ok": True, "pack": pack_id, "label": pack["label"], "applied": result,
            "note": f"{pack['label']} pack applied — custom fields, {result['kb_articles']['created']} "
                    f"new KB article(s), objectives and the assistant are live. "
                    f"Open setup.html custom fields or ask the analytics agent to group by a new field."}


def _default_target(o: Dict[str, Any]) -> float:
    """Sensible target when a pack leaves it None: nudge the current metric.
    'up' → current×1.2 (min 1); 'down' → current×0.7."""
    from app.core import objectives
    cur = objectives.metric_value(o["metric"]) or 0
    if o["direction"] == "up":
        return round(max(cur * 1.2, 1), 2)
    return round(cur * 0.7, 2)


def status(pack_id: str) -> Dict[str, Any]:
    pack = PACKS.get(pack_id)
    if not pack:
        return {"error": f"unknown pack '{pack_id}'"}
    from app.core import custom_fields
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            defs = 0
            for f in pack["custom_fields"]:
                cur.execute("SELECT 1 FROM custom_field_defs WHERE entity=%s AND field_key=%s AND is_active",
                            (f["entity"], f["field_key"]))
                defs += 1 if cur.fetchone() else 0
            cur.execute("SELECT count(*) FROM knowledge_articles WHERE source_ref LIKE %s AND status='active'",
                        (f"pack:{pack_id}:%",))
            kb = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM business_objectives WHERE created_by=%s AND status='active'",
                        (f"pack:{pack_id}",))
            objs = cur.fetchone()[0]
            agent = 0
            if pack.get("custom_agent"):
                cur.execute("SELECT 1 FROM custom_agents WHERE slug=%s AND enabled",
                            (pack["custom_agent"]["slug"],))
                agent = 1 if cur.fetchone() else 0
    finally:
        conn.close()
    return {"pack": pack_id, "applied": bool(defs or kb or objs or agent),
            "custom_fields": defs, "kb_articles": kb, "objectives": objs, "custom_agent": agent}


def remove(pack_id: str) -> Dict[str, Any]:
    pack = PACKS.get(pack_id)
    if not pack:
        return {"ok": False, "error": f"unknown pack '{pack_id}'"}
    conn = get_connection()
    counts = {"custom_fields": 0, "kb_articles": 0, "objectives": 0, "custom_agent": 0}
    try:
        with conn.cursor() as cur:
            for f in pack["custom_fields"]:
                cur.execute("UPDATE custom_field_defs SET is_active=false WHERE entity=%s AND field_key=%s",
                            (f["entity"], f["field_key"]))
                counts["custom_fields"] += cur.rowcount
            cur.execute("UPDATE knowledge_articles SET status='retired' WHERE source_ref LIKE %s",
                        (f"pack:{pack_id}:%",))
            counts["kb_articles"] = cur.rowcount
            cur.execute("UPDATE business_objectives SET status='archived' WHERE created_by=%s",
                        (f"pack:{pack_id}",))
            counts["objectives"] = cur.rowcount
            if pack.get("custom_agent"):
                cur.execute("UPDATE custom_agents SET enabled=false WHERE slug=%s",
                            (pack["custom_agent"]["slug"],))
                counts["custom_agent"] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "pack": pack_id, "removed": counts,
            "note": "Pack deactivated (custom-field values and KB are retained but inactive)."}


# ============================================================================
# Router (admin)
# ============================================================================
router = APIRouter(tags=["industry-packs"])


@router.get("/industry/packs")
def industry_packs():
    return list_packs()


@router.get("/industry/packs/{pack_id}/status")
def industry_pack_status(pack_id: str):
    return status(pack_id)


@router.post("/industry/packs/{pack_id}/apply")
def industry_pack_apply(pack_id: str):
    return apply(pack_id)


@router.post("/industry/packs/{pack_id}/remove")
def industry_pack_remove(pack_id: str):
    return remove(pack_id)
