"""Phase 3 — score the STT shadow, and refuse to pick the winner.

    python -m app.core.stt_shadow_score            # summary, all languages
    python -m app.core.stt_shadow_score --lang zh  # one language
    python -m app.core.stt_shadow_score --review 20  # pairs needing a human

WHAT THIS MEASURES, AND WHAT IT CANNOT. Shadow mode gives two transcripts of
the same audio. It does NOT give a reference transcript, so nothing here is
word-error rate — WER needs a human-verified truth and we have none. What can
be computed honestly is DIVERGENCE: how often the two recognisers disagree,
and by how much. Divergence says where to look; it cannot say who was right.

This is the same discipline as shadow_eval: the pipeline surfaces evidence and
a human sets the verdict. A scorer that auto-promoted on divergence alone
would happily promote a recogniser that is confidently and consistently wrong
— which is precisely the failure mode the voice channel has already been bitten
by, in fr and zh, and the reason those two languages are gates rather than
line items.

HOW TO READ THE OUTPUT
  divergence   fraction of pairs whose transcripts differ after normalising.
               High is a reason to review, not a reason to reject: punctuation
               and number formatting differ constantly between vendors and
               matter to nobody once the brain has read the sentence.
  wdiff        mean word-level edit distance as a fraction of the served
               transcript's length. This is the number that separates "same
               words, different commas" (~0.0) from "heard something else
               entirely" (>0.3).
  empty_delta  pairs where one side heard nothing and the other did. The most
               actionable single signal: a recogniser returning '' on real
               speech makes the caller repeat themselves, which the transcripts
               have shown is what callers hang up over.
  p50/p95 ms   candidate latency. A promotion needs this to beat the incumbent
               at p95, not at the mean — the tail is what a caller feels.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from typing import Dict, List, Optional, Tuple

from app.core.database import get_connection

GATE_LANGS = ("fr", "zh")       # must clear review before any promotion


def _norm(s: Optional[str]) -> List[str]:
    """Words, comparably. Strips the differences that are real but harmless:
    case, punctuation, and unicode width — a vendor emitting full-width
    Chinese punctuation is not a transcription error."""
    s = unicodedata.normalize("NFKC", (s or "")).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    # Chinese has no spaces; compare by character there, by word elsewhere.
    if re.search(r"[一-鿿]", s):
        return [c for c in s if not c.isspace()]
    return s.split()


def _edit(a: List[str], b: List[str]) -> int:
    """Levenshtein over tokens. Iterative two-row — utterances are short, but
    this runs over every pair in the corpus."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def _rows(lang: Optional[str], days: int) -> List[tuple]:
    sql = ("SELECT lang, served_by, served_text, shadow_by, shadow_text, "
           "shadow_ms, verdict FROM voice_stt_shadow "
           "WHERE created_at > now() - make_interval(days => %s)")
    args: list = [days]
    if lang:
        sql += " AND lang = %s"
        args.append(lang)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        conn.close()          # pooled checkout — close() returns the slot


def score(lang: Optional[str] = None, days: int = 30) -> Dict[str, dict]:
    by: Dict[str, dict] = {}
    for lg, served_by, served, shadow_by, shadow, ms, verdict in _rows(lang, days):
        d = by.setdefault(lg, {"pairs": 0, "differ": 0, "wdiff": [], "ms": [],
                               "empty_delta": 0, "reviewed": 0,
                               "served_by": served_by, "shadow_by": shadow_by})
        d["pairs"] += 1
        if verdict:
            d["reviewed"] += 1
        if ms is not None:
            d["ms"].append(ms)
        a, b = _norm(served), _norm(shadow)
        if bool(a) != bool(b):
            d["empty_delta"] += 1
        if a != b:
            d["differ"] += 1
        if a:
            d["wdiff"].append(_edit(a, b) / len(a))
    out: Dict[str, dict] = {}
    for lg, d in by.items():
        ms = sorted(d["ms"])
        out[lg] = {
            "pairs": d["pairs"],
            "served_by": d["served_by"],
            "shadow_by": d["shadow_by"],
            "divergence": round(d["differ"] / d["pairs"], 3) if d["pairs"] else 0.0,
            "wdiff": round(sum(d["wdiff"]) / len(d["wdiff"]), 3) if d["wdiff"] else 0.0,
            "empty_delta": d["empty_delta"],
            "reviewed": d["reviewed"],
            "p50_ms": ms[len(ms) // 2] if ms else None,
            "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))] if ms else None,
        }
    return out


def needs_review(limit: int = 20, lang: Optional[str] = None) -> List[dict]:
    """The pairs a human should look at first: unreviewed, most divergent.

    Ordering by divergence rather than recency is the point — a reviewer's
    time is the scarce resource, and 200 pairs that differ by a comma teach
    nothing that the first one did not.
    """
    scored = []
    sql = ("SELECT shadow_id, lang, served_by, served_text, shadow_by, "
           "shadow_text FROM voice_stt_shadow WHERE verdict IS NULL")
    args: list = []
    if lang:
        sql += " AND lang = %s"
        args.append(lang)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql + " ORDER BY created_at DESC LIMIT 2000", args)
        rows = cur.fetchall()
    finally:
        conn.close()
    for sid, lg, sby, stext, dby, dtext in rows:
        a, b = _norm(stext), _norm(dtext)
        if a == b:
            continue
        scored.append({"shadow_id": sid, "lang": lg,
                       "wdiff": round(_edit(a, b) / len(a), 3) if a else 1.0,
                       sby: stext, dby: dtext})
    scored.sort(key=lambda r: -r["wdiff"])
    return scored[:limit]


def review(shadow_id: int, verdict: str, reviewed_by: str,
           note: str = "") -> dict:
    if verdict not in ("equivalent", "shadow_better", "served_better",
                       "both_wrong"):
        return {"ok": False, "error": f"bad verdict {verdict!r}"}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE voice_stt_shadow SET verdict=%s, reviewed_by=%s, "
                    "reviewed_at=now(), note=%s WHERE shadow_id=%s",
                    (verdict, reviewed_by, note or None, shadow_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "shadow_id": shadow_id, "verdict": verdict}


def _verdict(lg: str, s: dict) -> str:
    """A recommendation, never an action. Promotion stays a human decision and
    stays per-language — see speech._order and VOICE_STT_PROVIDER_<LANG>."""
    if s["pairs"] < 100:
        return f"HOLD — {s['pairs']} pairs, need >=100"
    if s["reviewed"] < 20:
        return f"HOLD — {s['reviewed']} reviewed, need >=20"
    if lg in GATE_LANGS and s["wdiff"] > 0.15:
        return f"HOLD — gate language, wdiff {s['wdiff']} > 0.15"
    if s["empty_delta"] > s["pairs"] * 0.05:
        return f"HOLD — {s['empty_delta']} empty-transcript deltas"
    return "READY for human promotion decision"


def _utf8_stdout() -> None:
    """Windows consoles default to cp1252, which cannot encode Chinese — so
    `--review` crashed on exactly the pairs it exists to show. The gate
    languages are fr and zh; a reviewer tool that dies on zh is no tool at all.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                       # non-reconfigurable stream
        pass


def main() -> None:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="Score the STT shadow corpus.")
    ap.add_argument("--lang")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--review", type=int, metavar="N",
                    help="list N pairs needing human adjudication")
    a = ap.parse_args()

    if a.review:
        rows = needs_review(a.review, a.lang)
        if not rows:
            print("No unreviewed disagreements.")
            return
        for r in rows:
            print(f"\n[{r['shadow_id']}] {r['lang']}  wdiff={r['wdiff']}")
            for k, v in r.items():
                if k not in ("shadow_id", "lang", "wdiff"):
                    print(f"   {k:10} {v!r}")
        return

    res = score(a.lang, a.days)
    if not res:
        print(f"No shadow pairs in the last {a.days} days.\n"
              "Set VOICE_STT_SHADOW=deepgram and apply sql/voice_stt_shadow.sql.")
        return
    print(f"{'lang':5} {'pairs':>6} {'diverge':>8} {'wdiff':>7} "
          f"{'empty':>6} {'rev':>5} {'p50':>6} {'p95':>6}  verdict")
    for lg in sorted(res):
        s = res[lg]
        print(f"{lg:5} {s['pairs']:>6} {s['divergence']:>8} {s['wdiff']:>7} "
              f"{s['empty_delta']:>6} {s['reviewed']:>5} "
              f"{str(s['p50_ms']):>6} {str(s['p95_ms']):>6}  {_verdict(lg, s)}")
    print("\nDivergence is not error. A human sets every verdict; this tool "
          "only says where to look.")


if __name__ == "__main__":
    main()
