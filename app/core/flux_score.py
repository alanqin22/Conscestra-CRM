"""Did Flux's turn model beat the fixed 900 ms wait?

    python -m app.core.flux_score
    python -m app.core.flux_score --lang fr

TWO QUESTIONS, AND THE SECOND ONE DECIDES IT.

  1. Is it FASTER?    delta_ms = how much sooner Flux called the turn.
  2. Is it RIGHT?     a turn called early truncates the caller mid-sentence.

Speed alone is not a result. `VOICE_STREAM_SILENCE_MS` was 650 once; it was
raised to 900 precisely because the faster setting cut people off, and a
caller asking about the refund policy reached the brain as "Tell me." A turn
model that is 400 ms faster and truncates one utterance in twenty is WORSE
than the fixed wait, because a truncation costs a whole extra turn to repair
and the caller experiences it as not being listened to.

So the gate is: median saving worth having, AND truncation rate at or near
zero, AND Flux actually produced a turn for nearly every utterance. A missing
turn (flux_ms NULL) means the VAD carried the call — harmless in shadow,
fatal in serve mode.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, List, Optional

from app.core.database import get_connection

MIN_SAMPLE = 30
MAX_TRUNCATION = 0.02          # 1 in 50 is already too many on a support line


def _rows(days: int, lang: Optional[str]) -> List[tuple]:
    sql = ("SELECT lang, utter_ms, vad_ms, flux_ms, delta_ms, flux_conf, "
           "vad_text, flux_text, truncated FROM voice_flux_turn "
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


def _pct(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def analyse(days: int = 14, lang: Optional[str] = None) -> Dict[str, object]:
    rows = _rows(days, lang)
    by: Dict[str, Dict[str, object]] = {}
    for lg, utter, vad, flux, delta, conf, vtext, ftext, trunc in rows:
        d = by.setdefault(lg, {"n": 0, "deltas": [], "missing": 0,
                               "truncated": 0, "confs": [], "examples": []})
        d["n"] += 1                                    # type: ignore[operator]
        if flux is None or delta is None:
            d["missing"] += 1                          # type: ignore[operator]
        else:
            d["deltas"].append(float(delta))           # type: ignore[union-attr]
            d["confs"].append(float(conf or 0))        # type: ignore[union-attr]
        if trunc:
            d["truncated"] += 1                        # type: ignore[operator]
            if len(d["examples"]) < 3:                 # type: ignore[arg-type]
                d["examples"].append((vtext, ftext))   # type: ignore[union-attr]
    return {"by": by, "n": len(rows), "days": days}


def verdict(lg: str, d: Dict[str, object]) -> str:
    n = int(d["n"])                                    # type: ignore[arg-type]
    deltas: List[float] = d["deltas"]                  # type: ignore[assignment]
    if n < MIN_SAMPLE:
        return f"HOLD — {n} turns, need >={MIN_SAMPLE}"
    miss = int(d["missing"]) / n                       # type: ignore[arg-type]
    if miss > 0.10:
        return f"NO — Flux produced no turn on {miss*100:.0f}% of utterances"
    trunc = int(d["truncated"]) / n                    # type: ignore[arg-type]
    if trunc > MAX_TRUNCATION:
        return (f"NO — truncated {trunc*100:.1f}% (>{MAX_TRUNCATION*100:.0f}%). "
                f"Speed does not pay for cut-off callers.")
    med = _pct(deltas, 0.5)
    if med < 150:
        return f"MARGINAL — median saving only {med:.0f} ms; not worth the rewrite"
    return f"YES — median {med:.0f} ms sooner, no truncation. Consider serve mode."


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Score the Flux turn shadow.")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--lang")
    a = ap.parse_args()

    res = analyse(a.days, a.lang)
    if not res["n"]:
        print(f"No shadow turns in the last {a.days} days.\n"
              "Apply sql/voice_flux_turn.sql, set FLUX_ENABLED=1 "
              "(FLUX_MODE=shadow), and call in en, fr or es.\n"
              "Mandarin is unsupported by Flux and never opens a session.")
        return

    by: Dict[str, Dict[str, object]] = res["by"]       # type: ignore[assignment]
    print(f"turns: {res['n']}   window: {res['days']}d\n")
    print(f"{'lang':5} {'n':>5} {'no turn':>8} {'trunc':>7} "
          f"{'p50 saved':>10} {'p90 saved':>10} {'conf':>6}   verdict")
    for lg in sorted(by):
        d = by[lg]
        deltas: List[float] = d["deltas"]              # type: ignore[assignment]
        confs: List[float] = d["confs"]                # type: ignore[assignment]
        print(f"{lg:5} {int(d['n']):>5} {int(d['missing']):>8} "
              f"{int(d['truncated']):>7} {_pct(deltas,0.5):>9.0f}m "
              f"{_pct(deltas,0.9):>9.0f}m {_pct(confs,0.5):>6.2f}   "
              f"{verdict(lg, d)}")

    for lg in sorted(by):
        ex = by[lg]["examples"]                        # type: ignore[index]
        if ex:
            print(f"\ntruncations in {lg} — Flux called the turn early:")
            for v, f in ex:                            # type: ignore[misc]
                print(f"   VAD  : {v!r}")
                print(f"   FLUX : {f!r}")
    print("\nA turn called early costs a whole extra exchange to repair. "
          "Truncation beats speed.")


if __name__ == "__main__":
    main()
