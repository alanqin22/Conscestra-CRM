"""Freeze a benchmark corpus from live data.

WHY FROZEN. A regression gate reading live data fires on every reindex and
stays silent on real code regressions — worse than no gate, because it trains
people to ignore it. Measured during one afternoon's work, with no intentional
benchmark activity: the index moved 7278 -> 8049 -> 7278 -> 7394 records, themes
757 -> 853 -> 863 -> 848, and `production_attribution` drifted 0.9312 -> 0.9243
-> 0.9340 purely from data changes.

So the benchmark runs against a pinned snapshot. Movement then means CODE moved.

WHY A FILE AND NOT A TABLE. The fixture carries its own embeddings, so the
benchmark needs no database at all — which is the difference between running in
CI and not. CI currently executes 185 database-free tests out of 1088; this
gives it the derivation pipeline end to end.

    python -m scripts.make_benchmark --out benchmarks/corpus.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import embeddings as E            # noqa: E402
from app.core.database import get_connection    # noqa: E402

# Entities with enough records to cluster, few enough to stay a fixture.
MIN_RECORDS, MAX_RECORDS = 8, 60
DEFAULT_ENTITIES = 40

# STRATIFY BY SOURCE, OR THE GATE HAS A BLIND SPOT.
#
# The first corpus took contacts with 8-60 records and came out 100% `activity`
# — because the non-activity sources sit on a handful of contacts far outside
# that band (case_comment: ONE contact with 480 records; case: one with 120).
#
# The consequence was not cosmetic. `content_index._KNOWN_SPEAKER_SOURCES`
# fires only on case / case_comment / conversation_message, and it is the fix
# that corrected all 14 wrong third-party attributions. The regression gate
# could not execute it: break that branch and CI stays green.
#
# So every source type present in the index must appear in the fixture, and a
# per-entity record cap keeps a 480-record contact from dominating it.
MIN_PER_SOURCE = 2
MAX_RECORDS_PER_ENTITY = 60


def build(n_entities: int = DEFAULT_ENTITIES, seed: int = 20260801) -> dict:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Deterministic pick: hash-ordered, not random(), so re-running
            # against the same database reproduces the same fixture.
            cur.execute(
                """SELECT contact_id::text
                     FROM content_embeddings
                    WHERE contact_id IS NOT NULL
                    GROUP BY 1
                   HAVING count(*) BETWEEN %s AND %s
                    ORDER BY md5(contact_id::text || %s)
                    LIMIT %s""",
                (MIN_RECORDS, MAX_RECORDS, str(seed), int(n_entities)))
            ids = [r[0] for r in cur.fetchall()]

            # Then guarantee every source type is represented, whatever the
            # record-count band would have excluded.
            cur.execute("""SELECT DISTINCT source_type FROM content_embeddings
                            WHERE contact_id IS NOT NULL""")
            for (st,) in cur.fetchall():
                cur.execute(
                    """SELECT contact_id::text FROM content_embeddings
                        WHERE source_type = %s AND contact_id IS NOT NULL
                        GROUP BY 1
                        ORDER BY count(*) DESC, md5(contact_id::text || %s)
                        LIMIT %s""", (st, str(seed), MIN_PER_SOURCE))
                for (eid,) in cur.fetchall():
                    if eid not in ids:
                        ids.append(eid)
            ids.sort()          # stable order regardless of how they were found

            entities = []
            for eid in ids:
                # CAP PER (entity, source_type), not per entity. A flat
                # per-entity cap taking the most recent rows silently dropped
                # the very source an entity had been selected FOR: the
                # 480-record case_comment contact also has activities, so its
                # newest 60 were all activity and `case_comment` vanished from
                # the fixture again.
                cur.execute(
                    """SELECT source_type, source_id, embedding, dims, snippet,
                              visibility, occurred_at, direction, actor,
                              speech_act, parent_key, activity_type
                         FROM (
                           SELECT ce.source_type, ce.source_id, ce.embedding,
                                  ce.dims, ce.snippet, ce.visibility,
                                  ce.occurred_at, ce.direction, ce.actor,
                                  ce.speech_act, ce.parent_key, a.type
                                      AS activity_type,
                                  row_number() OVER (
                                      PARTITION BY ce.source_type
                                      ORDER BY ce.occurred_at DESC NULLS LAST,
                                               ce.source_id, ce.chunk_ix) AS rn
                             FROM content_embeddings ce
                             LEFT JOIN activities a
                               ON ce.source_type='activity'
                              AND a.activity_id::text = ce.source_id
                            WHERE ce.contact_id = %s::uuid
                              AND ce.model = %s AND ce.dims = %s) x
                        WHERE rn <= %s
                        ORDER BY occurred_at DESC NULLS LAST,
                                 source_type, source_id""",
                    (eid, E.MODEL, E.DIMS, MAX_RECORDS_PER_ENTITY))
                recs = []
                for (st, sid, blob, dims, snip, vis, occ, direction, actor,
                     act, parent, atype) in cur.fetchall():
                    recs.append({
                        "source_type": st, "source_id": sid,
                        # base64 of the raw float32 buffer — exact, not a
                        # re-encoded approximation.
                        "embedding": base64.b64encode(bytes(blob)).decode(),
                        "dims": dims, "snippet": snip, "visibility": vis,
                        "occurred_at": occ.isoformat() if occ else None,
                        "direction": direction, "actor": actor,
                        "speech_act": act, "parent_key": parent,
                        "activity_type": atype,
                    })
                if recs:
                    entities.append({"entity_type": "contact", "entity_id": eid,
                                     "records": recs})
    finally:
        conn.close()

    payload = {
        "schema": 1,
        "seed": seed,
        "model": E.MODEL,
        "dims": E.DIMS,
        "entities": entities,
        "record_count": sum(len(e["records"]) for e in entities),
    }
    # A checksum over the INPUT, so a fixture edit is visible. The baseline
    # records which corpus it was measured against; comparing results across
    # different corpora would be comparing nothing.
    payload["corpus_id"] = hashlib.sha256(
        json.dumps(entities, sort_keys=True).encode()).hexdigest()[:16]

    # IS THIS SAFE TO PUBLISH? The fixture stores VERBATIM indexed text and real
    # entity uuids. That is fine today because the corpus is seed data — order
    # templates and invoice numbers. It stops being fine the moment this script
    # is re-run against real customers, and NOTHING about the run would look
    # different: same command, same file size, same green CI, real sentences in
    # a public repository.
    #
    # So the realism verdict is stamped into the payload and the caller decides
    # the filename. Detected, not configured — a flag someone must remember to
    # set is a flag that will be wrong.
    from app.core.memory_eval import corpus_realism
    try:
        payload["realism"] = corpus_realism()
    except Exception as exc:                                  # noqa: BLE001
        payload["realism"] = {"synthetic": False, "signals": [],
                              "error": str(exc)[:120],
                              "note": "could not verify; treated as REAL"}
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmarks/corpus.json.gz")
    ap.add_argument("--entities", type=int, default=DEFAULT_ENTITIES)
    ap.add_argument("--allow-real", action="store_true",
                    help="freeze a corpus from data that is NOT synthetic. The "
                         "output contains verbatim customer text and must never "
                         "be committed; name it benchmarks/corpus-real.json.gz, "
                         "which .gitignore excludes.")
    args = ap.parse_args()

    payload = build(args.entities)
    real = not payload.get("realism", {}).get("synthetic", False)
    out = Path(args.out)
    if real and not args.allow_real:
        raise SystemExit(
            "REFUSING to freeze a corpus from non-synthetic data.\n"
            "  signals: " + "; ".join(payload.get("realism", {}).get("signals")
                                      or ["corpus does not look like seed data"])
            + "\n\nThis fixture stores verbatim indexed text and real entity "
              "uuids, and benchmarks/corpus.json.gz is COMMITTED to a public "
              "repository.\nIf you mean to build a private real-data corpus:\n"
              "    python -m scripts.make_benchmark --allow-real "
              "--out benchmarks/corpus-real.json.gz")
    if real and "corpus-real" not in out.name:
        raise SystemExit(
            f"a real-data corpus must be named benchmarks/corpus-real*.json.gz "
            f"(gitignored), not {out.name}")
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=1).encode("utf-8")
    if out.suffix == ".gz":
        import gzip
        out.write_bytes(gzip.compress(body, 6))
    else:
        out.write_bytes(body)
    size = out.stat().st_size / 1024 / 1024
    print(f"wrote {out}  corpus_id={payload['corpus_id']}  "
          f"{len(payload['entities'])} entities, "
          f"{payload['record_count']} records, {size:.1f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
