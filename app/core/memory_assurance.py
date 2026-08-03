"""Shadow mode, calibration and safety-path observability.

Three panel findings, one module, because they share a data model: all of them
are about MEASURING the safety path rather than asserting it works.

SHADOW MODE (#3). Autonomy has been argued for and never evidenced. Shadow mode
logs what an agent WOULD have said, with the memories that informed it, and
sends nothing. It is the only way to earn the autonomous decision with data —
and it doubles as the missing incident-response trail: after a bad reply,
"which memories were in context, and what did we say" currently has no answer.

CALIBRATION (#4). `confidence = reliability × certainty × decay` has the syntax
of a probability and has never been checked against an outcome. The
`memory_verifications` trail now provides ground truth: a human either approved
a claim or rejected it. If memories scoring 0.8 are approved at 55%, the number
is not a probability and should not be gated on as though it were.

OBSERVABILITY (#7). Nothing measures gate rejections, approval rates, or
per-verifier patterns. Verification bias — a reviewer approving 200 memories on
pattern rather than evidence — is invisible by construction, and a rubber-stamp
rate near 100% is indistinguishable from genuinely good memories.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.core.database import ensure_table, get_connection

logger = logging.getLogger("memory_assurance")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# Shadow mode is ON by default: recording what we WOULD say costs nothing and
# is the prerequisite for ever turning autonomy on.
SHADOW_ENABLED = _flag("MEMORY_SHADOW_MODE", "1")


def ensure_tables() -> bool:
    # A non-owner role cannot CREATE in the schema even when the table is
    # already there. Returning False on that would silently switch SHADOW MODE
    # OFF — the recording that everything downstream is measured from — for a
    # reason that is not a fault. ensure_table checks existence and proceeds.
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                ensure_table(cur, "public.agent_utterances", """
                    CREATE TABLE IF NOT EXISTS public.agent_utterances (
                        utterance_id   bigserial PRIMARY KEY,
                        correlation_id text,
                        channel        text,
                        audience       text NOT NULL,
                        entity_type    text,
                        entity_id      uuid,
                        -- What the agent said, or WOULD have said in shadow mode.
                        text           text NOT NULL,
                        shadow         boolean NOT NULL DEFAULT true,
                        -- Which memories were in context. Pointers, not content:
                        -- the statements live in customer_memories, which is
                        -- erased with the customer.
                        memory_ids     uuid[] NOT NULL DEFAULT '{}',
                        asserted_ids   uuid[] NOT NULL DEFAULT '{}',
                        reviewed       boolean NOT NULL DEFAULT false,
                        review_verdict text,
                        review_note    text,
                        reviewed_by    text,
                        created_at     timestamptz NOT NULL DEFAULT now(),
                        CONSTRAINT agent_utterances_verdict_check
                            CHECK (review_verdict IS NULL OR review_verdict IN
                                   ('correct','false_statement','unsupported','leaked'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_agent_utterances_corr
                        ON public.agent_utterances (correlation_id);
                    CREATE INDEX IF NOT EXISTS idx_agent_utterances_review
                        ON public.agent_utterances (reviewed, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_agent_utterances_entity
                        ON public.agent_utterances (entity_type, entity_id, created_at DESC);
                """)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[assurance] tables unavailable: {exc}")
        return False


# ============================================================================
# SHADOW MODE
# ============================================================================

def record_utterance(text: str, audience: str,
                     entity_type: Optional[str] = None,
                     entity_id: Optional[str] = None,
                     memory_ids: Optional[List[str]] = None,
                     asserted_ids: Optional[List[str]] = None,
                     channel: str = "", shadow: Optional[bool] = None) -> bool:
    """Record what an agent said — or would have said.

    Called at the point of composition, not the point of send, so a message
    blocked by outbound_guard is still recorded: a near-miss is exactly the
    signal shadow mode exists to collect.

    Never raises. An assurance layer that can break the feature it observes gets
    switched off, and then there is no assurance layer."""
    if not SHADOW_ENABLED:
        return False
    try:
        from app.core import grounding
        cid = grounding.correlation_id()
    except Exception:
        cid = None
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO agent_utterances
                         (correlation_id, channel, audience, entity_type, entity_id,
                          text, shadow, memory_ids, asserted_ids)
                       VALUES (%s,%s,%s,%s,%s::uuid,%s,%s,%s::uuid[],%s::uuid[])""",
                    (cid, channel, audience, entity_type, entity_id,
                     (text or "")[:4000],
                     True if shadow is None else bool(shadow),
                     memory_ids or [], asserted_ids or []))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[assurance] utterance not recorded: {exc}")
        return False


def review_utterance(utterance_id: int, verdict: str, reviewed_by: str,
                     note: str = "") -> Dict[str, Any]:
    """A human grades a shadow utterance. This is the ground truth that decides
    whether autonomy is earned."""
    if verdict not in ("correct", "false_statement", "unsupported", "leaked"):
        return {"ok": False, "error": f"unknown verdict '{verdict}'"}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE agent_utterances
                          SET reviewed=true, review_verdict=%s, review_note=%s,
                              reviewed_by=%s
                        WHERE utterance_id=%s RETURNING utterance_id""",
                    (verdict, note[:500], reviewed_by, int(utterance_id)))
                r = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": bool(r), "utterance_id": utterance_id, "verdict": verdict}


def shadow_report(days: int = 30) -> Dict[str, Any]:
    """The evidence an autonomy decision should rest on."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT count(*),
                              count(*) FILTER (WHERE reviewed),
                              count(*) FILTER (WHERE review_verdict='false_statement'),
                              count(*) FILTER (WHERE review_verdict='unsupported'),
                              count(*) FILTER (WHERE review_verdict='leaked'),
                              count(*) FILTER (WHERE cardinality(asserted_ids) > 0)
                         FROM agent_utterances
                        WHERE created_at > now() - (%s || ' days')::interval""",
                    (str(days),))
                total, reviewed, false_st, unsupported, leaked, with_assert = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    harmful = (false_st or 0) + (unsupported or 0) + (leaked or 0)
    return {
        "ok": True, "window_days": days,
        "utterances": total, "reviewed": reviewed,
        "review_coverage": round(reviewed / total, 3) if total else None,
        "false_statements": false_st, "unsupported": unsupported,
        "leaked_internal": leaked,
        "utterances_asserting_a_memory": with_assert,
        "harmful_rate": round(harmful / reviewed, 4) if reviewed else None,
        # Deliberately explicit: an unreviewed shadow log is not evidence.
        "autonomy_ready": bool(
            total >= int(os.getenv("SHADOW_MIN_UTTERANCES", "500"))
            and reviewed >= int(os.getenv("SHADOW_MIN_REVIEWED", "100"))
            and harmful == 0),
        "criteria": "no harmful verdicts, >=500 utterances, >=100 reviewed",
    }


# ============================================================================
# CALIBRATION
# ============================================================================

def calibration(bucket: float = 0.1) -> Dict[str, Any]:
    """Is stated confidence a probability, or just an ordering?

    Ground truth is human judgement from the verification trail: `verified`
    means a person agreed with the claim, `rejected` means they did not. If
    memories at 0.8 are approved at 55%, the number is not a probability, and
    the assertion floor is not what it appears to be.

    Reports buckets with counts so a thin bucket is visibly thin rather than
    silently noisy."""
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    -- EXCLUDE unmeasured reliability, do not score it 0.
                    -- COALESCE(reliability,0) turned "we never measured this"
                    -- into "confidence 0", which is the same fabrication as
                    -- the 1.0 that produced the constant in the first place —
                    -- just in the other direction. A calibration curve built
                    -- from invented zeroes is worse than a shorter one.
                    SELECT width_bucket(cm.reliability * cm.certainty, 0, 1, %s) AS b,
                           count(*) AS n,
                           count(*) FILTER (WHERE v.action='verified')  AS approved,
                           count(*) FILTER (WHERE v.action='rejected')  AS rejected,
                           avg(cm.reliability * cm.certainty) AS mean_conf
                      FROM customer_memories cm
                      JOIN memory_verifications v ON v.memory_id = cm.memory_id
                     WHERE cm.reliability IS NOT NULL AND cm.certainty IS NOT NULL
                     GROUP BY b ORDER BY b""", (int(1 / bucket),))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    buckets = []
    for b, n, approved, rejected, mean_conf in rows:
        judged = (approved or 0) + (rejected or 0)
        buckets.append({
            "bucket": f"{(b - 1) * bucket:.1f}-{b * bucket:.1f}",
            "memories": n, "approved": approved, "rejected": rejected,
            "mean_stated_confidence": round(float(mean_conf or 0), 3),
            "observed_approval_rate": round(approved / judged, 3) if judged else None,
            "thin": judged < 20,
        })
    usable = [b for b in buckets if not b["thin"]]
    return {
        "ok": True, "buckets": buckets,
        "calibrated": None if not usable else all(
            abs((b["observed_approval_rate"] or 0) - b["mean_stated_confidence"]) < 0.15
            for b in usable),
        "note": ("insufficient judged memories to calibrate — this is expected "
                 "until verification has been used in anger"
                 if not usable else "compare stated vs observed per bucket"),
    }


# ============================================================================
# SAFETY-PATH OBSERVABILITY
# ============================================================================

def safety_metrics(days: int = 30) -> Dict[str, Any]:
    """Gate rejections, approval behaviour, and verification-bias signals."""
    out: Dict[str, Any] = {"ok": True, "window_days": days}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT count(*),
                            count(*) FILTER (WHERE kind='fact'),
                            count(*) FILTER (WHERE verified_by IS NOT NULL),
                            count(*) FILTER (WHERE verified_signature IS NOT NULL),
                            count(*) FILTER (WHERE cardinality(contradicts)>0),
                            count(*) FILTER (WHERE evidence_missing>0),
                            count(*) FILTER (WHERE truncated),
                            count(*) FILTER (WHERE actor IN ('unknown','mixed'))
                         FROM customer_memories WHERE status='active'""")
                (total, facts, verified, signed, conflicted, dangling,
                 truncated, unattributed) = cur.fetchone()
                out["memories"] = {
                    "active": total, "facts": facts, "verified": verified,
                    "signed": signed, "with_conflicts": conflicted,
                    "with_dangling_evidence": dangling, "truncated": truncated,
                    "unattributed": unattributed,
                    "unattributed_pct": round(100 * unattributed / total, 1) if total else None,
                }

                # VERIFICATION BIAS. An approval rate near 100% is
                # indistinguishable from rubber-stamping; a reviewer who never
                # rejects is not reviewing. Reported per person because that is
                # where the pattern lives.
                cur.execute("""SELECT performed_by,
                            count(*) FILTER (WHERE action='verified') AS approved,
                            count(*) FILTER (WHERE action='rejected') AS rejected,
                            count(DISTINCT memory_id) AS memories,
                            min(created_at), max(created_at)
                         FROM memory_verifications
                        WHERE created_at > now() - (%s || ' days')::interval
                        GROUP BY performed_by ORDER BY 2 DESC""", (str(days),))
                verifiers = []
                for who, approved, rejected, n, first, last in cur.fetchall():
                    judged = approved + rejected
                    rate = approved / judged if judged else None
                    span = (last - first).total_seconds() if first and last else 0
                    verifiers.append({
                        "verifier": who, "approved": approved,
                        "rejected": rejected, "memories": n,
                        "approval_rate": round(rate, 3) if rate is not None else None,
                        "seconds_per_decision": round(span / judged, 1) if judged > 1 else None,
                        "bias_flag": bool(judged >= 10 and rate is not None and rate > 0.95),
                    })
                out["verifiers"] = verifiers
                out["bias_suspected"] = any(v["bias_flag"] for v in verifiers)
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return out


# ============================================================================
# REVIEW ROSTER
#
# Shadow mode without a staffed roster produces a log nobody reads, and
# `autonomy_ready` stays False forever because `reviewed` never rises. A queue
# needs an owner, an SLA and a measurable burn-down or it is a backlog with
# better branding.
# ============================================================================

REVIEW_SLA_HOURS = float(os.getenv("SHADOW_REVIEW_SLA_HOURS", "48"))
REVIEWER_DAILY_CAP = int(os.getenv("SHADOW_REVIEWER_DAILY_CAP", "40"))


def roster() -> List[str]:
    """Who is on the hook. Empty roster = nobody, and the report says so rather
    than implying the queue is being worked."""
    return [r.strip() for r in
            os.getenv("SHADOW_REVIEWERS", "").split(",") if r.strip()]


def review_queue(reviewer: Optional[str] = None,
                 limit: int = 20) -> Dict[str, Any]:
    """Unreviewed utterances, oldest first, optionally sharded to one reviewer.

    Sharding is deterministic (hash of utterance_id modulo roster size) so two
    reviewers working simultaneously never collide on the same item, and a
    reviewer returning tomorrow sees their own shard rather than a reshuffle."""
    people = roster()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT utterance_id, created_at, audience, channel, text,
                              cardinality(memory_ids), cardinality(asserted_ids),
                              correlation_id
                         FROM agent_utterances
                        WHERE NOT reviewed
                        ORDER BY created_at LIMIT %s""", (int(limit) * 8,))
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[assurance] review queue unavailable: {exc}")
        return {"ok": False, "error": str(exc)[:200]}

    items = []
    for (uid, created, audience, channel, text, n_mem, n_assert, cid) in rows:
        if reviewer and people:
            if people[uid % len(people)] != reviewer:
                continue
        age_h = (_now() - created).total_seconds() / 3600.0
        items.append({
            "utterance_id": uid, "created_at": created.isoformat(),
            "age_hours": round(age_h, 1),
            "breaching_sla": age_h > REVIEW_SLA_HOURS,
            "audience": audience, "channel": channel,
            "text": (text or "")[:300],
            "memories_in_context": n_mem, "memories_asserted": n_assert,
            "correlation_id": cid,
            # An utterance that ASSERTED something is worth more review time
            # than one that merely had memories in context.
            "priority": "high" if n_assert else "normal",
        })
        if len(items) >= int(limit):
            break
    return {"ok": True, "reviewer": reviewer, "roster": people,
            "sla_hours": REVIEW_SLA_HOURS, "queue": items}


def _now():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def roster_health(days: int = 7) -> Dict[str, Any]:
    """Is the queue being worked, and will it ever clear?

    Reports days-to-clear at the OBSERVED rate, not at capacity. A roster that
    exists on paper and reviews nothing produces `days_to_clear: null`, which is
    the honest answer."""
    people = roster()
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT count(*) FILTER (WHERE NOT reviewed),
                                      count(*) FILTER (WHERE NOT reviewed AND
                                        created_at < now() - (%s || ' hours')::interval)
                                 FROM agent_utterances""", (str(REVIEW_SLA_HOURS),))
                pending, breaching = cur.fetchone()
                cur.execute("""SELECT reviewed_by, count(*)
                                 FROM agent_utterances
                                WHERE reviewed
                                  AND created_at > now() - (%s || ' days')::interval
                                GROUP BY 1 ORDER BY 2 DESC""", (str(days),))
                done = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[assurance] roster health unavailable: {exc}")
        return {"ok": False, "error": str(exc)[:200]}

    per_day = sum(n for _w, n in done) / max(days, 1)
    capacity = len(people) * REVIEWER_DAILY_CAP
    return {
        "ok": True, "roster": people, "roster_size": len(people),
        "pending": pending, "breaching_sla": breaching,
        "sla_hours": REVIEW_SLA_HOURS,
        "reviews_per_day_observed": round(per_day, 1),
        "capacity_per_day": capacity,
        "days_to_clear_at_observed_rate": (round(pending / per_day, 1)
                                           if per_day > 0 else None),
        "reviewers": [{"who": w, "reviewed": n} for w, n in done],
        "warning": ("no reviewers configured — SHADOW_REVIEWERS is empty, so "
                    "this queue has no owner and autonomy can never be earned"
                    if not people else
                    "queue is not being worked" if per_day == 0 and pending
                    else None),
    }


# ============================================================================
# POINT-IN-TIME RECONSTRUCTION
#
# `explain(memory_id)` describes what the system believes NOW. A dispute is
# always about what it believed THEN — "your agent told me on 3 June that we had
# agreed a refund". After two consolidation generations the memory has been
# rewritten, and present state answers a different question.
#
# The durable record already existed in three places; nothing stitched them by
# TIME:
#   agent_utterances      what was said, and which memories were in context
#   memory_retrievals     what was retrieved, under which audience
#   memory_verifications  who approved what WORDING, and what they were shown
# ============================================================================

def reconstruct(correlation_id: Optional[str] = None,
                utterance_id: Optional[int] = None) -> Dict[str, Any]:
    """What did the system believe when it said this?

    Answers a dispute from the record rather than from current state. Memories
    that have since been rewritten, rejected or erased are reported as such —
    "the claim we relied on no longer exists" is itself the answer to some
    disputes."""
    if not (correlation_id or utterance_id):
        return {"ok": False, "error": "give a correlation_id or an utterance_id"}
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cols = ("utterance_id, correlation_id, created_at, audience, "
                        "channel, text, shadow, memory_ids, asserted_ids, "
                        "reviewed, review_verdict")
                if utterance_id:
                    cur.execute(f"SELECT {cols} FROM agent_utterances "
                                f"WHERE utterance_id=%s", (int(utterance_id),))
                else:
                    cur.execute(f"SELECT {cols} FROM agent_utterances "
                                f"WHERE correlation_id=%s ORDER BY created_at",
                                (correlation_id,))
                rows = cur.fetchall()
                if not rows:
                    return {"ok": False, "error": "no utterance recorded for that id"}

                out = []
                for (uid, cid, said_at, audience, channel, text, shadow,
                     mem_ids, asserted, reviewed, verdict) in rows:
                    memories = []
                    for mid in (mem_ids or []):
                        cur.execute(
                            "SELECT statement, topic, kind, verified_by, "
                            "       updated_at, status "
                            "  FROM customer_memories WHERE memory_id=%s", (mid,))
                        m = cur.fetchone()
                        if not m:
                            memories.append({
                                "memory_id": str(mid), "state": "erased_or_deleted",
                                "note": "the memory relied on no longer exists"})
                            continue
                        stmt, topic, kind, vby, updated, status = m
                        # The wording a human approved AT OR BEFORE the utterance.
                        cur.execute(
                            "SELECT statement_shown, performed_by, created_at "
                            "  FROM memory_verifications "
                            " WHERE memory_id=%s AND created_at <= %s "
                            " ORDER BY created_at DESC LIMIT 1", (mid, said_at))
                        v = cur.fetchone()
                        memories.append({
                            "memory_id": str(mid), "topic": topic, "kind": kind,
                            "status": status,
                            "statement_now": stmt,
                            "statement_when_approved": v[0] if v else None,
                            "approved_by": v[1] if v else None,
                            "approved_at": v[2].isoformat() if v else None,
                            # Timestamp comparison, so it has a resolution
                            # limit: a rewrite in the same instant as the
                            # utterance, or clock skew between replicas, can
                            # read as "not rewritten". It detects DRIFT over
                            # time, which is what a dispute is about — it is not
                            # a race-free ordering guarantee.
                            "rewritten_since_utterance": bool(updated and updated > said_at),
                            "wording_changed_since_approval": bool(
                                v and v[0] and v[0] != stmt),
                            "verified_now": bool(vby),
                            "asserted_in_this_utterance": mid in (asserted or []),
                        })

                    cur.execute(
                        "SELECT created_at, audience, query, result_count, results "
                        "  FROM memory_retrievals WHERE correlation_id=%s "
                        " ORDER BY created_at", (cid,))
                    retrievals = [{"at": r[0].isoformat(), "audience": r[1],
                                   "query": r[2], "results": r[3],
                                   "sources": r[4]} for r in cur.fetchall()]

                    out.append({
                        "utterance_id": uid, "correlation_id": cid,
                        "said_at": said_at.isoformat(), "audience": audience,
                        "channel": channel, "shadow": shadow, "text": text,
                        "reviewed": reviewed, "review_verdict": verdict,
                        "memories_in_context": memories,
                        "retrievals": retrievals,
                        "caveats": _reconstruction_caveats(memories),
                    })
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(f"[assurance] reconstruction failed: {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "utterances": out}


def _reconstruction_caveats(memories: List[Dict[str, Any]]) -> List[str]:
    """What a reader must know before trusting this reconstruction.

    A reconstruction that quietly presented today's memory as what was believed
    then would be worse than none — it would launder drift into evidence."""
    c: List[str] = []
    gone = [m for m in memories if m.get("state") == "erased_or_deleted"]
    drift = [m for m in memories if m.get("rewritten_since_utterance")]
    reworded = [m for m in memories if m.get("wording_changed_since_approval")]
    if gone:
        c.append(f"{len(gone)} memory/memories relied on have since been erased "
                 f"or deleted — their content cannot be recovered here")
    if drift:
        c.append(f"{len(drift)} memory/memories were REWRITTEN after this was "
                 f"said; `statement_now` is not what the agent saw")
    if reworded:
        c.append(f"{len(reworded)} statement(s) differ from the wording a human "
                 f"approved — see `statement_when_approved`")
    if not memories:
        c.append("no memories were in context for this utterance")
    return c


router = APIRouter(tags=["memory-assurance"])


@router.get("/assurance/reconstruct")
def assurance_reconstruct(correlation_id: Optional[str] = None,
                          utterance_id: Optional[int] = None):
    """What did the system believe when it said this? Answers a dispute from the
    record, not from current state."""
    return reconstruct(correlation_id, utterance_id)


@router.get("/assurance/review-queue")
def assurance_review_queue(reviewer: Optional[str] = None, limit: int = 20):
    """The shadow-mode work queue, optionally sharded to one reviewer."""
    return review_queue(reviewer, limit)


@router.get("/assurance/roster-health")
def assurance_roster_health(days: int = 7):
    """Is the queue owned, worked, and will it clear?"""
    return roster_health(days)


@router.get("/assurance/shadow")
def assurance_shadow(days: int = 30):
    return shadow_report(days)


@router.get("/assurance/calibration")
def assurance_calibration():
    return calibration()


@router.get("/assurance/safety-metrics")
def assurance_safety(days: int = 30):
    return safety_metrics(days)


@router.post("/assurance/utterances/{utterance_id}/review")
def assurance_review(utterance_id: int, body: Dict[str, Any]):
    return review_utterance(utterance_id, str(body.get("verdict") or ""),
                            str(body.get("reviewed_by") or "admin"),
                            str(body.get("note") or ""))
