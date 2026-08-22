"""Derive VOICE_STREAM_ECHO_CORR from real calls instead of guessing it.

    python -m app.core.echo_tune                 # distribution + recommendation
    python -m app.core.echo_tune --days 14
    python -m app.core.echo_tune --sweep         # cost of every candidate

WHY A TOOL AND NOT A LOG READ. The log line only ever recorded probes that
CROSSED the threshold, which is censored data: it contains no false negatives
by construction, so no amount of it can tell you the threshold is too high.
`voice_echo_probe` records every decision with its score, which is what makes
the question answerable.

THE LABELS ARE IMPERFECT AND THAT IS THE POINT. There is no ground truth for
"was this echo" on a live call, so the tool uses the outcome the caller
actually felt:

  transcript_empty=TRUE   we cut our own reply short and the audio yielded no
                          words. Neither speech nor echo — noise. This is the
                          case that makes a caller repeat themselves.
  transcript_empty=FALSE  real words came back. Whatever it was, treating it
                          as speech was RIGHT. Raising the threshold past
                          these scores would start swallowing real callers.
  decision='echo'         we suppressed it. Correctness is not observable here
                          — the caller heard us keep talking, which is what we
                          wanted if it was echo and rude if it was not. Watch
                          the rate, not the label.

So the safe band is bounded BELOW by nothing in particular and ABOVE by the
lowest-scoring probe that produced real words. A threshold above that line
starts discarding real speech, which is the one failure this system must not
have.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, List, Optional, Tuple

from app.core.database import get_connection

MIN_SAMPLE = 40          # below this, any recommendation is noise


def _rows(days: int, lang: Optional[str]) -> List[tuple]:
    sql = ("SELECT corr, decision, transcript_empty, lang, probe_rms, "
           "ref_rms, play_pos_ms, heard FROM voice_echo_probe "
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
    speech_real: List[float] = []     # became words — must stay below threshold
    noise: List[float] = []           # interrupted us, said nothing
    echo: List[float] = []            # we suppressed
    unlabelled: List[float] = []
    for corr, dec, empty, *_ in rows:
        c = float(corr or 0.0)
        if dec == "echo":
            echo.append(c)
        elif empty is True:
            noise.append(c)
        elif empty is False:
            speech_real.append(c)
        else:
            unlabelled.append(c)
    return {
        "n": len(rows), "days": days, "lang": lang or "all",
        "speech_real": speech_real, "noise": noise, "echo": echo,
        "unlabelled": unlabelled,
    }


def recommend(a: Dict[str, object]) -> Tuple[Optional[float], str]:
    """A threshold, or None with the reason it cannot be given yet."""
    real: List[float] = a["speech_real"]          # type: ignore[assignment]
    echo: List[float] = a["echo"]                 # type: ignore[assignment]
    n = int(a["n"])                               # type: ignore[arg-type]
    if n < MIN_SAMPLE:
        return None, (f"only {n} probes — need >={MIN_SAMPLE}. Make calls on "
                      f"the handsets that actually misbehave; a threshold from "
                      f"a quiet office line will not survive a speakerphone.")
    if len(real) < 10:
        return None, (f"only {len(real)} probes produced real words. Without "
                      f"them there is no floor, and a threshold with no floor "
                      f"is a guess with extra steps.")
    # DIRECTION MATTERS. The rule is `score >= threshold -> suppress`, so the
    # threshold must sit ABOVE the HIGHEST-scoring real utterance, not below
    # the lowest. Getting this backwards recommends a value the sweep on the
    # same data marks unsafe.
    floor = max(real)                 # anything at or under this is a caller
    ceiling = min(echo) if echo else 1.0
    if floor + 0.02 >= ceiling:
        return None, (
            f"real speech reaches {floor:.2f} and echo starts at "
            f"{ceiling:.2f} — the two overlap, so NO threshold separates them "
            f"on this data. Do not split the difference: that trades swallowed "
            f"callers for suppressed echo. Lean on the transcript backstop, and "
            f"look at whether these are really echo (our words coming back) or "
            f"noise (no words at all) — the `heard` column tells you which.")
    proposed = round(min(floor + 0.05, (floor + ceiling) / 2), 2)
    detail = (f"real speech tops out at {floor:.2f}; "
              + (f"echo starts at {ceiling:.2f}" if echo
                 else "no echo detected yet at the current setting"))
    return proposed, (
        f"{detail}. {proposed:.2f} sits in the gap, so no real utterance in "
        f"this sample would have been swallowed."
        + ("" if echo else " With no echo samples this is the lowest value "
                           "that is still SAFE, not a value shown to work — "
                           "if echo still gets through, the audio layer is not "
                           "the right tool for that handset."))


def sweep(a: Dict[str, object]) -> List[Tuple[float, int, int]]:
    """For each candidate threshold: real utterances lost, noise stopped."""
    real: List[float] = a["speech_real"]          # type: ignore[assignment]
    noise: List[float] = a["noise"]               # type: ignore[assignment]
    out = []
    t = 0.30
    while t <= 0.95001:
        lost = sum(1 for c in real if c >= t)      # real speech we would eat
        caught = sum(1 for c in noise if c >= t)   # noise we would stop
        out.append((round(t, 2), lost, caught))
        t += 0.05
    return out


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Tune the echo threshold.")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--lang")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    a = analyse(args.days, args.lang)
    if not a["n"]:
        print(f"No probes in the last {args.days} days.\n"
              "Apply sql/voice_echo_probe.sql, keep VOICE_ECHO_PROBE_LOG=1, "
              "and place calls from the handsets that misbehave.")
        return

    print(f"probes: {a['n']}   window: {a['days']}d   lang: {a['lang']}\n")
    print(f"{'group':22} {'n':>5} {'p10':>7} {'p50':>7} {'p90':>7} {'min':>7}")
    for name, key in (("became real words", "speech_real"),
                      ("interrupted, silent", "noise"),
                      ("suppressed as echo", "echo"),
                      ("unlabelled", "unlabelled")):
        xs: List[float] = a[key]                  # type: ignore[assignment]
        if not xs:
            print(f"{name:22} {0:>5}       -       -       -       -")
            continue
        print(f"{name:22} {len(xs):>5} {_pct(xs,0.1):>7.3f} {_pct(xs,0.5):>7.3f} "
              f"{_pct(xs,0.9):>7.3f} {min(xs):>7.3f}")

    if args.sweep:
        print(f"\n{'threshold':>10} {'real speech lost':>18} {'noise stopped':>15}")
        for t, lost, caught in sweep(a):
            mark = "  <-- unsafe" if lost else ""
            print(f"{t:>10.2f} {lost:>18} {caught:>15}{mark}")

    val, why = recommend(a)
    print("\n" + "=" * 68)
    if val is None:
        print(f"NO RECOMMENDATION — {why}")
    else:
        print(f"VOICE_STREAM_ECHO_CORR={val}")
        print(why)
    print("Change one thing, then re-measure. Never both at once.")


if __name__ == "__main__":
    main()
