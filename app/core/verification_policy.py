"""Verification throughput — how a human-gated memory system survives scale.

THE PROBLEM THE ARCHITECTURE DID NOT ADDRESS. Every assertable memory required
a human approval, and high-consequence topics required two. 60 customers
produced 138 memories. Linear extrapolation to 10,000 customers is ~23,000
memories, each needing an evidence review the workflow deliberately makes
non-trivial — roughly a thousand person-hours per full pass, recurring whenever
evidence changes. That is not a product; it is a labelling operation, and the
predictable outcome is that someone lowers the assertion floor to make the
backlog go away, recreating the original risk with better paperwork.

    memory ──▶ classify()  ──▶ tier
                                ├─ AUTO      structurally safe, never customer-assertable
                                ├─ SAMPLED   low-consequence: verify a sample, trust the class
                                └─ FULL      high-consequence or high-value: every one, dual approval

THE INSIGHT: not every memory needs the same assurance, because not every memory
carries the same consequence. A `company_did` resolution note used only for
internal routing cannot harm a customer no matter how wrong it is. A billing
claim stated to that customer can. Spending identical human effort on both is
what makes the system unshippable.

WHAT AUTO-VERIFICATION IS NOT: it never produces a customer-assertable fact.
`AUTO` memories are marked machine-checked and remain internal — the assertion
gate still requires `verified_by`, and nothing here sets it. Auto-tiering buys
throughput on the INTERNAL path only. Any design where a machine promotes its
own inference to customer-assertable is the circularity this project rejected.

SAMPLING gives a statistical, not per-item, guarantee: verify n of N and the
observed error rate bounds the class's error rate. That is a weaker promise than
per-item review and it is stated as such — `assurance: "sampled"` travels with
every memory verified this way.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("verification_policy")

AUTO, SAMPLED, FULL = "auto", "sampled", "full"

# Sampling parameters. `SAMPLE_RATE` is the fraction of a low-consequence class
# a human reviews; `SAMPLE_MIN` stops a small class being "sampled" by looking
# at one item, which would be a guarantee about nothing.
SAMPLE_RATE = float(os.getenv("VERIFY_SAMPLE_RATE", "0.10"))
SAMPLE_MIN = int(os.getenv("VERIFY_SAMPLE_MIN", "20"))
# Above this observed error rate a sampled class loses its trust and every
# member falls back to FULL review.
SAMPLE_MAX_ERROR = float(os.getenv("VERIFY_SAMPLE_MAX_ERROR", "0.05"))

# Minutes a careful evidence review actually takes. Used only for capacity
# planning — a number that is visibly a guess beats an unstated assumption.
MINUTES_PER_REVIEW = float(os.getenv("VERIFY_MINUTES_PER_REVIEW", "2.0"))

# Occasions needed before an internal-only, our-own-action memory is trusted
# without review. Set above the MIN_CLUSTER floor so a bare two-record theme
# still gets sampled.
AUTO_MIN_OCCURRENCES = int(os.getenv("VERIFY_AUTO_MIN_OCCURRENCES", "4"))


def _account_value_tier(cur, entity_type: str, entity_id: str) -> str:
    """high | standard. Revenue is the crude proxy: a wrong statement to the
    largest account costs more than the same statement to the smallest."""
    try:
        cur.execute(
            """SELECT COALESCE(SUM(o.amount), 0)
                 FROM opportunities o
                 LEFT JOIN contacts c ON c.account_id = o.account_id
                WHERE (o.account_id = %s::uuid OR c.contact_id = %s::uuid)
                  AND o.status = 'closed_won'""",
            (entity_id, entity_id))
        won = float(cur.fetchone()[0] or 0)
    except Exception as exc:
        cur.connection.rollback()
        logger.warning(f"[verify-policy] account value lookup failed for "
                       f"{entity_id[:8]}: {exc}")
        return "high"           # unknown value => treat as high. Fail expensive.
    # PERCENTILE, not an absolute figure. A fixed $50k threshold marked 45 of
    # 60 accounts "high value" on this book — which is not a tier, it is a
    # relabelling of everything. "High value" has to mean high RELATIVE to your
    # own customer base, or the tier carries no information and the throughput
    # model saves nothing.
    return "high" if won >= _high_value_threshold(cur) else "standard"


_HV_THRESHOLD: Optional[float] = None


def _high_value_threshold(cur) -> float:
    """Revenue at the VERIFY_HIGH_VALUE_PCTL percentile of won revenue per
    account. Computed once per process — the book does not move hourly."""
    global _HV_THRESHOLD
    if _HV_THRESHOLD is not None:
        return _HV_THRESHOLD
    pctl = float(os.getenv("VERIFY_HIGH_VALUE_PCTL", "0.80"))
    try:
        cur.execute(
            """SELECT percentile_cont(%s) WITHIN GROUP (ORDER BY total)
                 FROM (SELECT account_id, SUM(amount) AS total
                         FROM opportunities WHERE status='closed_won'
                        GROUP BY account_id) t""", (pctl,))
        row = cur.fetchone()
        _HV_THRESHOLD = float(row[0]) if row and row[0] is not None else 0.0
    except Exception as exc:
        cur.connection.rollback()
        logger.warning(f"[verify-policy] high-value threshold failed: {exc}")
        _HV_THRESHOLD = 0.0        # 0 => everything is "high". Fail expensive.
    return _HV_THRESHOLD


def classify(memory: Dict[str, Any], value_tier: str = "standard") -> Tuple[str, str]:
    """(tier, reason) for one memory.

    Order matters: the most restrictive condition that applies wins, so a
    high-consequence topic on a small account is still FULL."""
    topic = (memory.get("topic") or "").lower()
    visibility = memory.get("visibility")
    actor = memory.get("actor")
    truncated = bool(memory.get("truncated"))
    independent = int(memory.get("independent_sources") or 0)
    conflicts = memory.get("contradicts") or []

    # Anything a customer could ever be told gets human eyes, always.
    try:
        from app.core.memory_consolidation import required_approvals_for
        needs_two = required_approvals_for(topic) >= 2
    except Exception:
        needs_two = True
    if needs_two:
        return FULL, f"'{topic}' is high-consequence ({'dual approval' if needs_two else ''})"
    if value_tier == "high":
        return FULL, "high-value account — a wrong statement here is expensive"
    if conflicts:
        return FULL, "unresolved conflict needs a human decision"

    # AUTO: structurally incapable of reaching a customer, and corroborated.
    # These never get verified_by, so they remain un-assertable; they simply
    # stop consuming review capacity.
    #
    # Corroboration is OCCURRENCES, not source diversity. The first version
    # required >=2 distinct source types and fired on ZERO memories, because
    # clustering is done on embedding similarity and same-source records are
    # more similar to each other — clusters are homogeneous by construction.
    # Requiring heterogeneity from a process that produces homogeneity is a
    # criterion that can never be met. For an internal-only memory the harm
    # ceiling is low enough that repeated observation is adequate corroboration.
    occurrences = int(memory.get("occurrences") or 0)
    if (visibility != "customer" and actor == "company_did" and not truncated
            and (independent >= 2 or occurrences >= AUTO_MIN_OCCURRENCES)):
        return AUTO, (f"internal-only, our own action, {occurrences} occasions "
                      f"— cannot reach a customer")

    return SAMPLED, "low-consequence and customer-visible — sampled review"


def plan(limit: int = 2000) -> Dict[str, Any]:
    """The capacity picture: what needs review, by tier, and what it costs.

    This is the number the architecture was missing. It converts "verification
    does not scale" from an opinion into a staffing figure."""
    out: Dict[str, Any] = {"ok": True, "tiers": {AUTO: 0, SAMPLED: 0, FULL: 0},
                           "reasons": {}, "unverified": 0}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT memory_id::text, entity_type, entity_id::text, topic,
                              visibility, actor, truncated, independent_sources,
                              ARRAY(SELECT unnest(contradicts)::text), verified_by,
                              occurrences
                         FROM customer_memories
                        WHERE status='active' AND superseded_by IS NULL
                        LIMIT %s""", (int(limit),))
                rows = cur.fetchall()
                value_cache: Dict[str, str] = {}
                for (mid, etype, eid, topic, vis, actor, trunc, indep,
                     contradicts, vby, occurrences) in rows:
                    if vby:
                        continue
                    out["unverified"] += 1
                    key = f"{etype}:{eid}"
                    if key not in value_cache:
                        value_cache[key] = _account_value_tier(cur, etype, eid)
                    tier, reason = classify(
                        {"topic": topic, "visibility": vis, "actor": actor,
                         "truncated": trunc, "independent_sources": indep,
                         "contradicts": contradicts,
                         "occurrences": occurrences}, value_cache[key])
                    out["tiers"][tier] += 1
                    out["reasons"].setdefault(tier, {})
                    out["reasons"][tier][reason] = out["reasons"][tier].get(reason, 0) + 1
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[verify-policy] plan failed: {exc}")
        return {"ok": False, "error": str(exc)[:200]}

    full = out["tiers"][FULL]
    sampled = out["tiers"][SAMPLED]
    sample_n = max(SAMPLE_MIN, math.ceil(sampled * SAMPLE_RATE)) if sampled else 0
    sample_n = min(sample_n, sampled)
    # FULL is dual-approval, so two humans per item.
    reviews = full * 2 + sample_n
    out["review_load"] = {
        "full_reviews": full * 2,
        "sampled_reviews": sample_n,
        "auto_no_review": out["tiers"][AUTO],
        "total_reviews": reviews,
        "person_hours": round(reviews * MINUTES_PER_REVIEW / 60.0, 1),
        "without_tiering_person_hours": round(
            out["unverified"] * 2 * MINUTES_PER_REVIEW / 60.0, 1),
    }
    saved = (out["review_load"]["without_tiering_person_hours"]
             - out["review_load"]["person_hours"])
    out["review_load"]["hours_saved_by_tiering"] = round(saved, 1)
    return out


def sample_for_review(topic: Optional[str] = None,
                      limit: int = 0) -> List[Dict[str, Any]]:
    """The SAMPLED tier's work queue — a deterministic pseudo-random sample.

    Deterministic (hash of memory_id) so the same memories are drawn on every
    call: a reviewer who returns tomorrow continues the same sample rather than
    facing a fresh one, and the sample cannot be reshuffled until it contains
    something convenient."""
    n = int(limit or SAMPLE_MIN)
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT memory_id::text, entity_type, entity_id::text,
                              statement, topic, occurrences, evidence_count
                         FROM customer_memories
                        WHERE status='active' AND verified_by IS NULL
                          AND visibility='customer'
                          AND (%s IS NULL OR topic = %s)
                        ORDER BY ('x' || substr(md5(memory_id::text), 1, 8))::bit(32)::int
                        LIMIT %s""", (topic, topic, n))
                return [{"memory_id": r[0], "entity_type": r[1], "entity_id": r[2],
                         "statement": r[3], "topic": r[4], "occurrences": r[5],
                         "evidence_count": r[6]} for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[verify-policy] sample failed: {exc}")
        return []


def sampled_class_health(topic: str) -> Dict[str, Any]:
    """Does this sampled class still deserve its trust?

    Sampling promises a statistical bound, not a per-item guarantee. If reviewed
    members of a class are being REJECTED above SAMPLE_MAX_ERROR, the bound is
    broken and the class must fall back to full review."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT count(*) FILTER (WHERE v.action='verified'),
                              count(*) FILTER (WHERE v.action='rejected')
                         FROM customer_memories cm
                         JOIN memory_verifications v ON v.memory_id = cm.memory_id
                        WHERE cm.topic = %s""", (topic,))
                approved, rejected = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[verify-policy] class health failed for '{topic}': {exc}")
        return {"ok": False, "error": str(exc)[:200]}

    judged = (approved or 0) + (rejected or 0)
    error = (rejected / judged) if judged else None
    return {"ok": True, "topic": topic, "judged": judged,
            "approved": approved, "rejected": rejected,
            "error_rate": round(error, 3) if error is not None else None,
            "sample_sufficient": judged >= SAMPLE_MIN,
            "trusted": bool(judged >= SAMPLE_MIN and error is not None
                            and error <= SAMPLE_MAX_ERROR),
            "note": ("insufficient sample — this class has no statistical bound "
                     "and every member needs review"
                     if judged < SAMPLE_MIN else
                     f"error {error:.1%} vs max {SAMPLE_MAX_ERROR:.0%}")}


router = APIRouter(tags=["verification-policy"])


@router.get("/verification/plan")
def verification_plan(limit: int = 2000):
    """Review capacity required, by tier — the staffing number."""
    return plan(limit)


@router.get("/verification/queue")
def verification_queue(topic: Optional[str] = None, limit: int = 0):
    return {"sample": sample_for_review(topic, limit)}


@router.get("/verification/class-health/{topic}")
def verification_class_health(topic: str):
    return sampled_class_health(topic)
