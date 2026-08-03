"""Phase 5 — AI-specific observability for Customer Memory.

WHAT WAS MISSING. Eight surfaces already measured this system —
`safety_metrics`, `shadow_report`, `calibration`, `roster_health`,
`weekly_report`, `run_all`, `label_status`, `corpus_realism` — and every one
answered only "what is true right now". Nothing was retained, so "memory drift
over time" was not unimplemented, it was UNCOMPUTABLE.

That cost real understanding already. Over one session the live index moved
7278 -> 8049 -> 7278 -> 7394 records and themes 757 -> 853 -> 863 -> 848; every
figure was known only because someone happened to run a query at that instant,
and two days later none of it is recoverable.

THIS MODULE COMPOSES, IT DOES NOT MEASURE. Every number comes from the surface
that already owns it. A second implementation of a metric is how `explain()`
came to apply a weaker gate than `recall()` — same defect, and there is no
reason to invite it back for the sake of a dashboard.

NO ALERT THRESHOLDS. Production metrics are SUPPOSED to move; the useful
question is whether one moved unexpectedly fast, and answering it needs history
that does not exist yet. Inventing a cutoff now would be the third withdrawn
guess in this project. Deltas are reported; a human reads them.

    python -m app.core.memory_observability            # dashboard
    python -m app.core.memory_observability --snapshot # persist a reading
    python -m app.core.memory_observability --drift    # what moved
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter

from app.core.database import get_connection

logger = logging.getLogger("memory_observability")


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _corpus_shape() -> Dict[str, Tuple[Optional[float], Any]]:
    """The only numbers this module reads directly: the shape of the corpus
    itself, which no existing surface reports as a whole."""
    out: Dict[str, Tuple[Optional[float], Any]] = {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM content_embeddings")
            out["corpus.indexed_records"] = (float(cur.fetchone()[0]), None)
            cur.execute("SELECT count(*) FROM customer_memories WHERE status='active'")
            out["corpus.active_themes"] = (float(cur.fetchone()[0]), None)

            cur.execute("""SELECT actor, count(*) FROM customer_memories
                            WHERE status='active' GROUP BY 1""")
            actors = dict(cur.fetchall())
            total = sum(actors.values()) or 1
            out["corpus.customer_voice_share"] = (
                round((actors.get("customer_said", 0)
                       + actors.get("customer_did", 0)) / total, 4),
                {"by_actor": actors})

            cur.execute("""SELECT count(*) FROM customer_memories
                            WHERE status='active' AND distinct_templates = 1""")
            single = cur.fetchone()[0]
            out["corpus.single_wording_share"] = (round(single / total, 4), None)

            cur.execute("""SELECT round(avg(certainty),4), round(avg(reliability),4),
                                  count(*) FILTER (WHERE reliability IS NOT NULL),
                                  count(*)
                             FROM customer_memories WHERE status='active'""")
            cert, rel, measured, tot = cur.fetchone()
            out["trust.mean_certainty"] = (_num(cert), None)
            # Averaged over the MEASURED subset only. A null mean is ambiguous
            # in a dashboard — it reads as an outage — so the share is reported
            # beside it. 0.0 means "reliability is unmeasured across the board",
            # which is a fact about the DATA, not a fault in the reading.
            out["trust.mean_reliability"] = (_num(rel), None)
            out["trust.reliability_measured_share"] = (
                round((measured or 0) / (tot or 1), 4),
                {"measured": measured, "total": tot,
                 "note": None if measured else
                         "no evidence source carries a confidence score; "
                         "reliability is not measured, and is NOT 0.70"})

            cur.execute("""SELECT decay_class, count(*) FROM customer_memories
                            WHERE status='active' GROUP BY 1""")
            out["lifecycle.decay_distribution"] = (None, dict(cur.fetchall()))

            cur.execute("""SELECT count(*) FROM customer_memories
                            WHERE status='active'
                              AND valid_until IS NOT NULL AND valid_until < now()""")
            out["lifecycle.stale_themes"] = (float(cur.fetchone()[0]), None)

            cur.execute("""SELECT count(*) FROM customer_memories
                            WHERE status='active' AND cardinality(contradicts) > 0""")
            out["quality.contradicted_themes"] = (float(cur.fetchone()[0]), None)

            # ASSERTABILITY IS NOT DECIDABLE IN SQL.
            #
            # This metric used to be `kind='fact' AND verified_by IS NOT NULL AND
            # visibility='customer'` — three of the gate's thirteen conditions,
            # re-derived here in a WHERE clause. It could not check the
            # signature (PostgreSQL cannot compute the HMAC), the claim hash,
            # expiry, contradictions, evidence loss or the certainty floor.
            #
            # So a row a database writer forged — which the red team showed the
            # database accepts — counted as ASSERTABLE on the dashboard while
            # recall() refused it forever. The number reported the opposite of
            # the truth precisely when it mattered: under attack.
            #
            # gate_inputs() exists to be the ONLY place the gate's inputs are
            # assembled, and its own docstring records that re-implementing the
            # rule elsewhere is a defect this codebase has already produced four
            # times. This was the fifth. It now calls the gate.
            from app.core.memory_consolidation import (_assertion_blockers,
                                                       gate_inputs)
            cur.execute("""SELECT memory_id, statement, evidence_hash, kind,
                                  visibility, actor, truncated, certainty,
                                  contradicts, conflict_severity, evidence_missing,
                                  verified_by, verified_actor,
                                  verification_expires_at, verified_claim_hash,
                                  verified_signature
                             FROM customer_memories
                            WHERE status='active' AND verified_by IS NOT NULL""")
            cols = [d[0] for d in cur.description]
            candidates = [dict(zip(cols, r)) for r in cur.fetchall()]

            assertable, refused = 0, {}
            for row in candidates:
                cert = row.get("certainty")
                blockers = _assertion_blockers(**gate_inputs(
                    row, effective_cert=float(cert) if cert is not None else None))
                if blockers:
                    for b in blockers:
                        refused[b] = refused.get(b, 0) + 1
                else:
                    assertable += 1

            out["gate.assertable_themes"] = (float(assertable), {
                "candidates": len(candidates),
                "note": "computed by the real gate, not a WHERE clause",
                "refused_by": refused or None})
            # The gap between the two is the interesting number: rows a human
            # marked verified that the gate still will not state.
            out["gate.verified_but_refused"] = (
                float(len(candidates) - assertable), refused or None)

            # The signal that already exists and had no home: undeclared bulk
            # deletion. 270 rows once vanished across 59 innocuous statements.
            #
            # THIS WAS NOT A 24-HOUR NUMBER. `v_governed_deletion_activity`
            # aggregates all history into one row per (table, repair_key,
            # principal), so filtering it on `last_seen > now() - 1 day`
            # selects GROUPS touched recently and then sums their ENTIRE
            # lifetime. Measured: it reported 14581 when the true 24-hour
            # figure was 198 — the all-time total, wearing a 24h label.
            #
            # The consequence is worse than the wrong number. A total is
            # monotonic: it can never fall, so the metric can never say the
            # problem stopped, and a genuine spike of 5000 is invisible
            # against a baseline of 14581. An alert that only ever rises is
            # read once and then ignored.
            #
            # Counted from the rows themselves, with the time filter on the
            # event. Transactions are reported alongside rows because 9034
            # rows in ONE statement and 9034 separate deletions are different
            # incidents, and the row count alone cannot tell them apart.
            cur.execute("""SELECT count(*), count(DISTINCT txid)
                             FROM governed_deletions
                            WHERE repair_key = 'undeclared'
                              AND deleted_at > now() - interval '1 day'""")
            rows, txns = cur.fetchone()
            out["ops.undeclared_deletions_24h"] = (
                float(rows), {"transactions": txns,
                              "note": "rows in the last 24h; transactions "
                                      "distinguishes one bulk delete from many"})
            out["ops.undeclared_deletion_txns_24h"] = (float(txns), None)
    finally:
        conn.close()
    return out


# Each entry: (metric prefix, callable, extractor). The callable is the surface
# that OWNS the number; nothing here recomputes one.
def _compose() -> Dict[str, Tuple[Optional[float], Any]]:
    out: Dict[str, Tuple[Optional[float], Any]] = {}

    def safely(label: str, fn: Callable[[], Any]) -> Optional[Any]:
        """A dashboard must never be the thing that breaks. One surface failing
        leaves a hole in the reading, not an exception in the caller."""
        try:
            return fn()
        except Exception as exc:                       # noqa: BLE001
            logger.warning(f"[observability] {label} unavailable: {exc}")
            out[f"{label}.unavailable"] = (1.0, {"error": str(exc)[:200]})
            return None

    from app.core import memory_assurance as MA
    from app.core import memory_eval as EV
    from app.core import shadow_eval as SE

    sm = safely("safety", lambda: MA.safety_metrics(30))
    if sm:
        for k in ("verified", "rejected", "with_dangling_evidence",
                  "truncated", "unattributed"):
            if k in sm:
                out[f"safety.{k}"] = (_num(sm[k]), None)
        if "by_verifier" in sm:
            out["safety.verifier_bias"] = (None, sm["by_verifier"])

    sh = safely("shadow", lambda: SE.weekly_report(7))
    if sh and sh.get("ok"):
        out["shadow.pairs"] = (_num(sh.get("pairs")), None)
        out["shadow.reviewed"] = (_num(sh.get("reviewed")), None)
        out["shadow.accepted"] = (_num(sh.get("accepted")), None)
        out["shadow.unsafe"] = (_num(sh.get("unsafe")), None)
        # The number a safety report would never volunteer: if memory changed
        # no answer, every other metric looks perfect.
        out["shadow.identical_rate"] = (_num(sh.get("identical_rate")), None)
        out["shadow.autonomy_ready"] = (1.0 if sh.get("autonomy_ready") else 0.0,
                                        {"blockers": sh.get("blockers")})

    ev = safely("eval", EV.run_all)
    if ev:
        for m in ev.get("intrinsic", []):
            if m.get("value") is not None:
                out[f"eval.{m['metric']}"] = (
                    _num(m["value"]), {"n": m.get("n"), "status": m.get("status")})
        out["eval.verdict_ok"] = (1.0 if ev.get("verdict") == "PASS" else 0.0,
                                  {"verdict": ev.get("verdict"),
                                   "extrinsic": ev.get("extrinsic", {}).get("status")})

    ls = safely("labels", EV.label_status)
    if ls:
        out["labels.count"] = (_num(ls.get("labels")), None)
        out["labels.double_labelled"] = (_num(ls.get("double_labelled")), None)

    rh = safely("roster", lambda: MA.roster_health(7))
    if rh:
        for k in ("unreviewed", "reviewers"):
            if k in rh:
                out[f"roster.{k}"] = (_num(rh[k]), None)

    return out


def snapshot(persist: bool = True) -> Dict[str, Any]:
    """Take one reading of everything and, by default, keep it.

    Keeping it is the entire point — a reading nobody stored is why the record
    of this system's own behaviour has holes in it."""
    metrics = _corpus_shape()
    metrics.update(_compose())

    if not persist:
        return {"ok": True, "persisted": False,
                "metrics": {k: v[0] for k, v in metrics.items()},
                "detail": {k: v[1] for k, v in metrics.items() if v[1] is not None}}

    written = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for metric, (value, detail) in sorted(metrics.items()):
                cur.execute(
                    """INSERT INTO memory_metrics_history
                         (metric, value, detail, source)
                       VALUES (%s,%s,%s::jsonb,%s)""",
                    (metric, value,
                     json.dumps(detail, default=str) if detail is not None else None,
                     metric.split(".", 1)[0]))
                written += 1
        conn.commit()
    except Exception as exc:                           # noqa: BLE001
        conn.rollback()
        logger.warning(f"[observability] snapshot not persisted: {exc}")
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        conn.close()
    return {"ok": True, "persisted": True, "metrics_written": written}


def drift(limit: int = 40) -> Dict[str, Any]:
    """What moved, largest relative change first. Reports; does not judge."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT metric, current_value, previous_value, delta,
                          relative_change, measured_at, compared_with
                     FROM v_memory_drift
                    ORDER BY abs(COALESCE(relative_change, 0)) DESC,
                             metric
                    LIMIT %s""", (int(limit),))
            rows = cur.fetchall()
            cur.execute("""SELECT count(DISTINCT metric), count(*),
                                  min(captured_at), max(captured_at)
                             FROM memory_metrics_history""")
            series, points, first, last = cur.fetchone()
    finally:
        conn.close()
    return {
        "ok": True,
        "series": series, "data_points": points,
        "first_reading": first.isoformat() if first else None,
        "latest_reading": last.isoformat() if last else None,
        "moved": [
            {"metric": m, "current": _num(cur_v), "previous": _num(prev),
             "delta": _num(d), "relative_change": _num(rel),
             "measured_at": at.isoformat() if at else None,
             "compared_with": cw.isoformat() if cw else None}
            for m, cur_v, prev, d, rel, at, cw in rows],
        "note": "deltas are reported, not judged — production metrics are "
                "supposed to move, and there is not yet enough history to say "
                "how fast is too fast",
    }


def dashboard() -> Dict[str, Any]:
    """Everything an operator needs, grouped by the question it answers."""
    snap = snapshot(persist=False)
    m = snap["metrics"]

    def grab(prefix: str) -> Dict[str, Any]:
        return {k.split(".", 1)[1]: v for k, v in m.items()
                if k.startswith(prefix + ".")}

    return {
        "ok": True,
        # Is the memory layer producing anything, and about whom?
        "corpus": grab("corpus"),
        # How much do we trust it?
        "trust": grab("trust"),
        # Is it decaying and conflicting as designed?
        "lifecycle": grab("lifecycle"),
        "quality": grab("quality"),
        # Is anything actually assertable to a customer?
        "gate": grab("gate"),
        # Is the accuracy suite passing?
        "eval": grab("eval"),
        # Is there evidence it helps?
        "shadow": grab("shadow"),
        "labels": grab("labels"),
        # Human review load and bias.
        "roster": grab("roster"),
        "safety": grab("safety"),
        # Operational signals with nowhere else to live.
        "ops": grab("ops"),
        "detail": snap.get("detail", {}),
    }


router = APIRouter(tags=["memory-observability"])


@router.get("/memory/observability")
def memory_dashboard():
    """Grouped current readings."""
    return dashboard()


@router.get("/memory/observability/drift")
def memory_drift(limit: int = 40):
    """What moved since the previous reading."""
    return drift(limit)


@router.post("/memory/observability/snapshot")
def memory_snapshot():
    """Persist a reading. Also runs on the daily schedule."""
    return snapshot(persist=True)


if __name__ == "__main__":                                   # pragma: no cover
    import sys
    if "--snapshot" in sys.argv:
        print(json.dumps(snapshot(persist=True), indent=2, default=str))
    elif "--drift" in sys.argv:
        print(json.dumps(drift(), indent=2, default=str))
    else:
        print(json.dumps(dashboard(), indent=2, default=str))
