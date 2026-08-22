"""Deepgram Flux — conversational turn detection, in shadow.

WHAT THIS IS FOR. The energy VAD ends an utterance after a FIXED
`VOICE_STREAM_SILENCE_MS=900` of silence. That number is the single largest
remaining component of a turn, and it cannot be lowered safely: 650 ms was
tried and cut callers off mid-sentence ("Tell me." for a refund-policy
question). Flux replaces the fixed wait with a trained turn model.

MEASURED against our own 8 kHz mu-law before any of this was written:

    encoding=mulaw&sample_rate=8000   accepted natively — no upsample hop
                                      (16 kHz bought ~50 ms, not worth it)
    event vs type                     THE TRAP. Every message is
                                      {"type":"TurnInfo","event":"..."} —
                                      `type` is the envelope, `event` is the
                                      signal (StartOfTurn | Update |
                                      EndOfTurn). Reading `type or event`
                                      short-circuits on the envelope and hides
                                      EndOfTurn completely. EndOfTurn DOES
                                      fire, once per turn, carrying the whole
                                      transcript plus turn_index, word
                                      timings, and audio_window_start/end.

    end_of_turn_confidence            peaks as the sentence COMPLETES, then
                                      collapses to ~0.01 in the silence after.
                                      That is the signal: the model reacts to
                                      a finished sentence, not to a gap, which
                                      is exactly why it can beat a silence
                                      timer. Measured peaks: 0.635 @ eot=0.5,
                                      0.768 @ 0.7, 0.920 @ 0.9.

    eot_threshold                     SCALES the reported confidence, it is
                                      not just a filter — so the two settings
                                      move together or the gate is never
                                      reached. Values below ~0.5 are rejected
                                      HTTP 400.
    EndOfTurn event                   NEVER OBSERVED, with or without
                                      eot_timeout_ms. The docs sample shows
                                      one; our probes only ever saw TurnInfo.
                                      So this module keys off the CONFIDENCE
                                      and treats the event as a bonus.
    languages                         flux-general-en = English only.
                                      flux-general-multi = en/fr/es, and
                                      **empty for zh**. Any explicit
                                      `language=` param is rejected HTTP 400.

MANDARIN IS NOT SUPPORTED, and that is a permanent split rather than a gap to
close later: `SUPPORTED` below gates it, zh keeps the VAD path, and the
per-language provider ordering already in speech.py is the same idea one layer
down.

SHADOW FIRST. `FLUX_MODE=shadow` (the default when enabled) forwards a copy of
the caller's audio and records when Flux WOULD have ended the turn against
when the VAD actually did. Nothing about the live call changes. Only after
that data says the turn model is faster AND not premature is `FLUX_MODE=serve`
worth considering.

FAIL-SOFT IS THE WHOLE CONTRACT. Every failure path here — no key, bad
handshake, dropped socket, unparseable frame — disables Flux for that call and
leaves the VAD untouched. A speech vendor must never be able to take the phone
line down.

CONFIG (env)
  FLUX_ENABLED        0        master switch
  FLUX_MODE           shadow   shadow | serve   (serve is not wired yet)
  FLUX_MODEL          flux-general-multi
  FLUX_EOT_THRESHOLD  0.9      sent to Flux — SCALES the confidence it reports
  FLUX_EOT_CONF       0.85     fallback gate, only if EndOfTurn never arrives
  DEEPGRAM_API_KEY    ''       same key as the STT provider
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("flux")

try:
    import websockets
except ImportError:                     # pragma: no cover
    websockets = None

# Languages Flux actually transcribes, measured — not the vendor's list.
# zh is deliberately absent: flux-general-multi returns EMPTY for Mandarin.
SUPPORTED = {"en", "fr", "es"}

_URL = "wss://api.deepgram.com/v2/listen"


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def enabled_for(lang: str) -> bool:
    return (_flag("FLUX_ENABLED")
            and websockets is not None
            and (lang or "en") in SUPPORTED
            and bool(os.getenv("DEEPGRAM_API_KEY", "").strip()))


def mode() -> str:
    m = os.getenv("FLUX_MODE", "shadow").strip().lower()
    return m if m in ("shadow", "serve") else "shadow"


_stats: Dict[str, int] = {}


def _bump(k: str, n: int = 1) -> None:
    _stats[k] = _stats.get(k, 0) + n


def stats() -> Dict[str, int]:
    return dict(_stats)


class FluxSession:
    """One Flux WebSocket for one call.

    Frames go in as they arrive from the carrier; turn-ends come out. Audio is
    queued rather than awaited at the send site, so a slow or wedged socket
    costs the caller nothing.
    """

    def __init__(self, call_sid: str, lang: str):
        self.call_sid, self.lang = call_sid, lang
        self.ws = None
        self.ok = False
        self.closed = False
        self._q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(maxsize=200)
        self._tasks: List[asyncio.Task] = []
        # Turn state
        self.transcript = ""
        self.last_conf = 0.0
        self.turn_ended_at: Optional[float] = None
        self.turn_text = ""
        self._turn_len = 0                     # transcript length when called
        self.turn_conf = 0.0                   # confidence AT the crossing
        self.turns: List[Dict[str, Any]] = []  # every turn inside one VAD utterance
        self.conf_trace: List[tuple] = []      # (t, conf) for tuning
        self.started_at = time.time()

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> bool:
        if websockets is None:
            return False
        key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        if not key:
            return False
        model = os.getenv("FLUX_MODEL", "flux-general-multi").strip()
        eot = os.getenv("FLUX_EOT_THRESHOLD", "0.9").strip()
        # NOTE: no `language` parameter. Flux rejects it outright (HTTP 400);
        # the model id is the only language control there is.
        url = (f"{_URL}?model={model}&encoding=mulaw&sample_rate=8000"
               f"&eot_threshold={eot}")
        try:
            self.ws = await asyncio.wait_for(
                websockets.connect(
                    url, additional_headers={"Authorization": f"Token {key}"},
                    open_timeout=6, close_timeout=2, max_queue=64),
                timeout=8)
        except Exception as exc:
            _bump("connect_failed")
            logger.info("[flux] %s connect failed (%s) — VAD path unaffected",
                        self.call_sid[:12], type(exc).__name__)
            return False
        self.ok = True
        _bump("sessions")
        self._tasks = [asyncio.create_task(self._pump()),
                       asyncio.create_task(self._read())]
        return True

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._q.put_nowait(None)
        except asyncio.QueueFull:
            pass
        for t in self._tasks:
            t.cancel()
        if self.ws is not None:
            try:
                await asyncio.wait_for(self.ws.close(), timeout=2)
            except Exception:
                pass

    # ── audio in ────────────────────────────────────────────────────────────
    def feed(self, ulaw: bytes) -> None:
        """Non-blocking by contract. A full queue means Flux is behind, and
        the right response is to DROP the frame: this is a shadow, and a
        shadow that applies back-pressure to a phone call has stopped being
        one."""
        if not self.ok or self.closed:
            return
        try:
            self._q.put_nowait(ulaw)
        except asyncio.QueueFull:
            _bump("frames_dropped")

    async def _pump(self) -> None:
        try:
            while True:
                item = await self._q.get()
                if item is None or self.ws is None:
                    return
                await self.ws.send(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ok = False
            _bump("send_failed")
            logger.info("[flux] %s send stopped (%s)", self.call_sid[:12],
                        type(exc).__name__)

    # ── events out ──────────────────────────────────────────────────────────
    async def _read(self) -> None:
        """Flux's turn signal is `event`, NOT `type`.

        Every message arrives as `{"type": "TurnInfo", "event": "...", ...}` —
        `type` is the envelope and is always "TurnInfo"; `event` carries
        StartOfTurn / Update / EndOfTurn. Reading `type or event` short-
        circuits on the truthy envelope and never sees the signal, which is
        how an earlier version of this module concluded the API emits no
        EndOfTurn at all. It does, on every turn, with the COMPLETE transcript.
        """
        conf_gate = float(os.getenv("FLUX_EOT_CONF", "0.85"))
        try:
            async for raw in self.ws:                    # type: ignore[union-attr]
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                ev = m.get("event") or ""                # NOT m.get("type")
                text = (m.get("transcript") or "").strip()
                conf = float(m.get("end_of_turn_confidence") or 0.0)
                idx = m.get("turn_index")
                # audio_window_end is the turn's end in AUDIO time, which is
                # what a latency comparison wants: wall-clock arrival includes
                # network and buffering we did not cause.
                aw_end = m.get("audio_window_end")
                if text:
                    self.transcript = text
                self.last_conf = conf
                self.conf_trace.append(
                    (round(time.time() - self.started_at, 3), conf, ev))

                if ev == "EndOfTurn":
                    self._commit_turn(text or self.transcript, conf, idx, aw_end)
                elif ev in ("StartOfTurn", "Update"):
                    continue
                elif conf >= conf_gate and text:
                    # Fallback for a stream that somehow reports no EndOfTurn.
                    # Kept deliberately: the confidence signal was all an
                    # earlier version had, and it did work, just less precisely.
                    _bump("turns_by_confidence")
                    self._commit_turn(text, conf, idx, aw_end)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ok = False
            _bump("read_failed")
            logger.info("[flux] %s read stopped (%s)", self.call_sid[:12],
                        type(exc).__name__)

    def _commit_turn(self, text: str, conf: float, idx, aw_end) -> None:
        """Record a turn Flux has declared finished.

        No withdrawal logic here any more. That existed to undo turns guessed
        from a mid-sentence confidence spike; an explicit EndOfTurn is the
        model's own decision and needs no second-guessing — and its transcript
        is the whole turn, not the fragment the confidence gate produced.
        """
        self.turn_ended_at = time.time()
        self.turn_text = text
        self.turn_conf = conf
        self.turns.append({"at": self.turn_ended_at, "text": text,
                           "conf": conf, "index": idx, "audio_end": aw_end})
        _bump("turns")

    # ── what the shadow comparison needs ────────────────────────────────────
    def take_turn(self) -> Optional[Dict[str, Any]]:
        """The turn currently standing, plus every turn seen since the last
        call, then reset. Returns None only when Flux called nothing at all."""
        if self.turn_ended_at is None and not self.turns:
            return None
        if self.turn_ended_at is None:          # last one was withdrawn
            last = self.turns[-1]
            self.turn_ended_at, self.turn_text = last["at"], last["text"]
            self.turn_conf = last["conf"]
        out = {"at": self.turn_ended_at, "text": self.turn_text,
               "conf": self.turn_conf, "n_turns": len(self.turns),
               "all": [t["text"] for t in self.turns],
               "trace": list(self.conf_trace[-40:])}
        self.turn_ended_at, self.turn_text, self._turn_len = None, "", 0
        self.turns.clear()
        self.conf_trace.clear()
        return out
