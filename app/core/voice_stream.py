"""Real-time voice — carrier media streams with streaming STT/TTS.

The turn-based <Gather> loop works but feels like an IVR: every exchange
pays a carrier round-trip plus a fixed end-of-speech timeout. This module
upgrades the TRANSPORT only — the brains (voice_support's tier ladder,
sdr's state machine), the signature-verified webhooks, the OTP flow and the
customer-scope security model are all unchanged:

    carrier ──<Connect><Stream>──►  WS /voice/stream/{line}?sid&frm&t
        8 kHz μ-law frames in  ──►  VAD (energy, ~900 ms end-of-speech)
        utterance WAV          ──►  STT   Azure REST (short-audio) →
                                          OpenAI Whisper fallback
        text                   ──►  the SAME brain (support | sdr)
        reply text             ──►  TTS   Azure REST (native 8 kHz μ-law) →
                                          OpenAI TTS (PCM → resample) fallback
        μ-law frames out       ◄──  chunked, cancellable → BARGE-IN: caller
                                    speech during playback sends "clear"
        DTMF events            ──►  the same keypad verify flow (OTP)
        our own voice back     ──►  ECHO, two layers: correlated against
                                    what we sent (~240 ms, stops the
                                    self-interrupt) and, if that misses,
                                    caught again on the transcript

WS AUTH: carriers do not sign the WebSocket connect, so the TwiML we return
embeds an HMAC token binding (line, CallSid, From) — minted only inside the
signature-verified inbound webhook. No valid token, no stream.

PROTOCOL: Twilio Media Streams JSON (connected/start/media/dtmf/mark/stop;
outbound media/mark/clear). Telnyx TeXML streaming is Twilio-compatible on
these events; stream ids are read from either spelling and echoed back.

ON/OFF: VOICE_STREAM_ENABLED=0 (default) → inbound webhooks keep the Gather
loop exactly as before. Flip to 1 (with a public base URL) to upgrade both
lines; flip back any time.

CONFIG (env)
  VOICE_STREAM_ENABLED      0     use media streams instead of <Gather>
  VOICE_STREAM_PUBLIC_BASE  ''    public https base for the wss URL
                                  (falls back to TELNYX_PUBLIC_BASE/APP_URL)
  VOICE_STREAM_VAD_RMS      300   speech energy threshold (16-bit RMS)
  VOICE_STREAM_SILENCE_MS   900   end-of-utterance silence
  VOICE_STREAM_PLAY_LEAD    0.85  outbound pacing, as a fraction of real time
  VOICE_STREAM_BIDI_MODE    rtp   Telnyx <Stream> playback format (rtp | mp3)
  VOICE_STREAM_BIDI_CODEC   PCMU  playback codec (rtp mode only)
  VOICE_TTS_VOICE           en-US-JennyNeural   Azure neural voice (English)
  VOICE_TTS_VOICE_FR/ZH/ES/DE     per-language Azure voices
  -- echo, caught early (audio) --
  VOICE_STREAM_ECHO_PROBE_MS      240  window correlated before interrupting
  VOICE_STREAM_ECHO_CORR          0.70 correlation above which it is our echo
  VOICE_STREAM_ECHO_MAX_DELAY_MS  800  furthest back the echo can have left us
  -- echo, caught late (transcript backstop) --
  VOICE_STREAM_ECHO_TAIL_MS   400  how long after playback echo can arrive
  VOICE_STREAM_ECHO_OVERLAP   0.7  share of heard words that must be ours
  VOICE_STREAM_ECHO_MIN_WORDS 4    below this, overlap proves nothing

LANGUAGE: recognition locale and TTS voice switch TOGETHER (_AZURE_BY_LANG),
and the choice is pushed into the BRAIN's session too, so the words, the voice
that speaks them and the recogniser that hears the reply all agree. The caller
picks with the keypad — accepted at any point in the call, not just during the
opening menu, because DTMF still works when the recogniser is on the wrong
language, which is precisely when they need it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as _hmac
import io
import json
import logging
import os
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core import flux_stream, speech

logger = logging.getLogger("voice_stream")

# audioop is stdlib through 3.12; on 3.13+ the audioop-lts package provides
# the same module (requirements.txt). Its absence must degrade THIS feature,
# never crash the whole app at import — main.py imports this module
# unconditionally.
try:
    import numpy as _np
except ImportError:                     # pragma: no cover
    _np = None                          # acoustic echo check degrades off

try:
    import audioop
except ImportError:                     # pragma: no cover
    audioop = None
    logger.warning("[stream] audioop unavailable (Python 3.13+ without "
                   "audioop-lts installed) — real-time voice disabled; "
                   "the Gather loop keeps working")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLED = _flag("VOICE_STREAM_ENABLED") and audioop is not None
VAD_RMS = int(os.getenv("VOICE_STREAM_VAD_RMS", "300"))
# End-of-utterance silence. 650 ms cut people off mid-sentence: natural speech
# has pauses ("tell me … the refund policy", "can you, uh, …"), and every pause
# longer than this ended the utterance and started a new one. The evidence was
# in the transcripts — a caller asking about the refund policy reached the
# brain as "Tell me." Raising it costs a little response latency on every turn
# and buys back the second half of the question, which is a good trade.
SILENCE_MS = int(os.getenv("VOICE_STREAM_SILENCE_MS", "900"))
TTS_VOICE = os.getenv("VOICE_TTS_VOICE", "en-US-JennyNeural").strip()

# ── Language, on the streaming path ─────────────────────────────────────────
# The <Gather> path switches recognition language and TTS voice together; this
# path did neither — STT was pinned to en-US and TTS to one English voice — so
# turning streaming on silently made a four-language line English-only. Both
# halves move together here for the same reason they do there: an English
# recogniser on Mandarin audio returns confident nonsense.
#
# The pair table moved to app/core/speech.py so the vendors and the language
# registry have one home. `_AZURE_BY_LANG` stays as the name this module's
# language-switching logic already uses — membership in it is still what
# decides whether a language is offered at all.
_AZURE_BY_LANG = speech.LANG_TABLE
_azure_pair = speech.azure_pair


# ── Echo of our own voice ───────────────────────────────────────────────────
# How long after playback ends echo can still arrive (carrier jitter buffer +
# handset latency). Beyond this the line is quiet and anything heard is the
# caller.
ECHO_TAIL_MS = int(os.getenv("VOICE_STREAM_ECHO_TAIL_MS", "400"))
# Fraction of the heard words that must also be words we just said.
ECHO_OVERLAP = float(os.getenv("VOICE_STREAM_ECHO_OVERLAP", "0.7"))
# Below this many words, overlap is not evidence — "yes" and "okay" appear in
# our own sentences constantly and are also the commonest real answers.
ECHO_MIN_WORDS = int(os.getenv("VOICE_STREAM_ECHO_MIN_WORDS", "4"))


# ── Acoustic echo: catching it BEFORE the barge-in, not after ───────────────
# is_echo() above reads the TRANSCRIPT, so it can only act once the utterance
# is over — by then we have already cancelled our own playback and the caller
# has heard us cut ourselves off mid-sentence. This catches the same audio
# ~240 ms in, while it is still only a candidate.
#
# WHY NO LEVEL THRESHOLD. The obvious fix is "ignore quiet audio while
# speaking", which needs a loudness multiplier nobody can pick without a
# handset in hand — set it too high and a softly-spoken caller can never
# interrupt. Unnecessary: WE KNOW EXACTLY WHAT WE SENT, so the probe can be
# correlated against the reference, and normalised correlation does not care
# how loud the echo is. MEASURED across attenuations 0.6 -> 0.06, the score
# moves 0.979 -> 0.975. Amplitude is simply not a variable.
#
# WHY WAVEFORM. Three signatures were measured on synthetic-but-realistic echo
# (delay, attenuation, band-limiting, speaker clipping, mu-law round trip):
#     envelope  echo>=0.994  other<=0.969   gap 0.025  — all speech envelopes
#                                                        are syllable bumps
#     spectral  echo>=0.880  other<=0.764   gap 0.117
#     waveform  echo>=0.970  other<=0.435   gap 0.535  <- chosen
# Double-talk (our echo plus the caller speaking over it) scores 0.42-0.44, so
# a genuine barge-in still reads as speech, which is the point.
#
# THE DELAY BAND IS LOAD-BEARING. Correlating the probe against the WHOLE
# reference scores unrelated speech at 0.98 — a 240 ms window against several
# seconds offers hundreds of alignments and one of them always fits. Only the
# slice that could physically be echoing right now is a candidate, which we
# know because we know our own playback position.
#
# HOW IT FAILS. Measured against line noise: at ~12 dB SNR the score falls to
# 0.32-0.39 and echo is MISSED. That is the safe direction — a miss is exactly
# today's behaviour (barge in, then let is_echo catch the transcript), whereas
# a false positive would swallow a real interruption. Nonlinear speaker
# distortion (tanh drive 1->8) and 40 ms of clock drift barely move it.
ECHO_PROBE_MS = int(os.getenv("VOICE_STREAM_ECHO_PROBE_MS", "240"))
ECHO_CORR = float(os.getenv("VOICE_STREAM_ECHO_CORR", "0.70"))
ECHO_MAX_DELAY_MS = int(os.getenv("VOICE_STREAM_ECHO_MAX_DELAY_MS", "800"))


def echo_correlation(probe_pcm: bytes, ref_pcm: bytes, pos_samples: int) -> float:
    """Peak normalised cross-correlation of the probe against the slice of our
    own recently-sent audio that could be echoing now. 0.0 when undecidable."""
    if _np is None or not probe_pcm or not ref_pcm:
        return 0.0
    n = len(probe_pcm) // 2
    if n < 320:                                   # < 40 ms is not evidence
        return 0.0
    lo = max(0, pos_samples - ECHO_MAX_DELAY_MS * 8 - n)
    hi = min(len(ref_pcm) // 2, pos_samples + n)
    if hi - lo < n + 8:
        return 0.0
    x = _np.frombuffer(probe_pcm, dtype="<i2").astype(_np.float32)
    y = _np.frombuffer(ref_pcm[lo * 2:hi * 2], dtype="<i2").astype(_np.float32)
    x = x - x.mean()
    xn = float(_np.linalg.norm(x))
    if xn < 1e-6:
        return 0.0
    x = x / xn
    corr = _np.correlate(y, x, mode="valid")
    # Norm of every candidate window, from prefix sums — the alternative is a
    # Python loop over ~5000 offsets on the audio path.
    y64 = y.astype(_np.float64)
    csq = _np.concatenate([[0.0], _np.cumsum(y64 ** 2)])
    csm = _np.concatenate([[0.0], _np.cumsum(y64)])
    wsq = csq[n:] - csq[:-n]
    wsm = csm[n:] - csm[:-n]
    denom = _np.sqrt(_np.maximum(wsq - wsm ** 2 / n, 1e-9))
    m = min(len(corr), len(denom))
    if m <= 0:
        return 0.0
    return float(_np.max(_np.abs(corr[:m] / denom[:m])))


# ── Flux shadow persistence ─────────────────────────────────────────────────
_flux_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flux-log")
_FLUX_TABLE_OK: Optional[bool] = None


def _write_flux_turn(row) -> None:
    global _FLUX_TABLE_OK
    if _FLUX_TABLE_OK is False:
        logger.info("[flux] turn lang=%s vad=%sms flux=%sms delta=%sms conf=%.2f",
                    row[2], row[4], row[5], row[6], row[7])
        return
    conn = None
    try:
        from app.core.database import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO voice_flux_turn
                 (call_sid, line, lang, utter_ms, vad_ms, flux_ms, delta_ms,
                  flux_conf, vad_text, flux_text, truncated, n_turns,
                  flux_turns)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", row)
        conn.commit()
        _FLUX_TABLE_OK = True
    except Exception as exc:
        if _FLUX_TABLE_OK is None:
            logger.info("[flux] voice_flux_turn unavailable (%s) — comparisons "
                        "go to the log; apply sql/voice_flux_turn.sql", exc)
        _FLUX_TABLE_OK = False
    finally:
        if conn is not None:
            try:
                conn.close()      # pooled checkout — close() returns the slot
            except Exception:
                pass


# ── Probe telemetry ─────────────────────────────────────────────────────────
# Off the audio path (thread pool, never awaited) for the same reason the STT
# shadow is: a live call must not wait on a database to decide anything.
_probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="echo-log")
_PROBE_TABLE_OK: Optional[bool] = None
_LAST_PROBE_ID: Dict[str, int] = {}


def _log_probe(call, decision: str, corr: float, pos: int,
               probe: bytes, voiced_ms: int) -> None:
    if not _flag("VOICE_ECHO_PROBE_LOG", "1"):
        return
    try:
        p_rms = audioop.rms(probe, 2) if (audioop and probe) else 0
        lo = max(0, pos * 2 - len(probe))
        ref = call.ref_pcm[lo:lo + len(probe)] if call.ref_pcm else b""
        r_rms = audioop.rms(ref, 2) if (audioop and ref) else 0
    except Exception:
        p_rms = r_rms = 0
    row = (call.call_sid, call.line, call.lang, float(corr), ECHO_CORR,
           decision, int(len(probe) / 16), int(pos / 8), int(p_rms),
           int(r_rms), int(voiced_ms))
    try:
        _probe_pool.submit(_write_probe, row)
    except RuntimeError:
        pass


def _write_probe(row) -> None:
    global _PROBE_TABLE_OK
    if _PROBE_TABLE_OK is False:
        logger.info("[stream] probe %s corr=%.3f pos=%sms rms=%s/%s", row[5],
                    row[3], row[7], row[8], row[9])
        return
    conn = None
    try:
        from app.core.database import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO voice_echo_probe
                 (call_sid, line, lang, corr, threshold, decision, probe_ms,
                  play_pos_ms, probe_rms, ref_rms, voiced_ms)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING probe_id""", row)
        _LAST_PROBE_ID[row[0]] = cur.fetchone()[0]
        conn.commit()
        _PROBE_TABLE_OK = True
    except Exception as exc:
        if _PROBE_TABLE_OK is None:
            logger.info("[stream] voice_echo_probe unavailable (%s) — probe "
                        "scores go to the log; apply "
                        "sql/voice_echo_probe.sql to tune from them", exc)
        _PROBE_TABLE_OK = False
    finally:
        if conn is not None:
            try:
                conn.close()      # pooled checkout — close() returns the slot
            except Exception:
                pass


def _label_probe(call_sid: str, heard: str) -> None:
    """Attach the transcript to the probe that produced it.

    This is the label that makes the data self-supervising. A probe we called
    SPEECH whose audio yielded no words was neither speech nor echo — it was
    noise, and noise that interrupts the agent is what makes a caller repeat
    themselves.
    """
    pid = _LAST_PROBE_ID.pop(call_sid, None)
    if pid is None or _PROBE_TABLE_OK is not True:
        return

    def _w():
        conn = None
        try:
            from app.core.database import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("UPDATE voice_echo_probe SET transcript_empty=%s, "
                        "heard=%s WHERE probe_id=%s",
                        (not bool(heard), (heard or "")[:300], pid))
            conn.commit()
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    try:
        _probe_pool.submit(_w)
    except RuntimeError:
        pass


def _echo_norm(s: str) -> str:
    """Lowercase words only — punctuation and casing differ between the text we
    handed to TTS and the text STT returns for that very same audio."""
    return " ".join(re.findall(r"[a-z0-9一-鿿]+", (s or "").lower()))


def _echo_overlap(heard: str, said: str) -> float:
    """Fraction of the heard words that appear in what we said."""
    hw = _echo_norm(heard).split()
    sw = set(_echo_norm(said).split())
    if not hw or not sw:
        return 0.0
    return sum(1 for w in hw if w in sw) / len(hw)


def is_echo(heard: str, said_recent: str, utter_started: float,
            play_end: float) -> bool:
    """Is this utterance our own audio coming back, rather than the caller?

    On a speakerphone the handset plays our reply into its own microphone and
    the carrier returns it. The VAD cannot tell that from speech, so the line
    hears itself, interrupts itself, and answers itself.

    TWO conditions, and BOTH are required — either alone is wrong on a real
    call:

      TIMING   the utterance began while we were still speaking (or inside the
               echo tail). Overlap ALONE would misread the commonest good turn
               on this line: we ask "do you want to cancel order 4471?" and the
               caller answers "cancel order 4471" — every word of which we just
               said. A real answer arrives AFTER playback ends; echo cannot.
      OVERLAP  most of the heard words are words we just said. Timing ALONE
               would discard every genuine barge-in, which is the whole point
               of this transport (and what the urgency proxy counts).

    Heard during our own speech AND made of our own words = echo.
    """
    if not heard or not said_recent:
        return False
    if utter_started > play_end + ECHO_TAIL_MS / 1000.0:
        return False
    if len(_echo_norm(heard).split()) < ECHO_MIN_WORDS:
        return False
    return _echo_overlap(heard, said_recent) >= ECHO_OVERLAP



# The menu WORDING comes from sdr._LANG_MENU_TEXT, the same table the <Gather>
# path speaks, so the two transports cannot drift into telling callers to press
# different keys — or saying it differently in one place and not the other.
# This module kept its own terse copy for a while to save a couple of seconds
# at call open; the saving was not worth a second source of truth, and the
# shortened Chinese ("中文，3。") read as clipped rather than brief.
# Each option is spoken by ITS OWN language's voice: an English engine reading
# "中文服务，请按 3" is unintelligible to the one caller who needs that option.
_RETRY = {
    "en": "Sorry, I didn't catch that. Could you say it again?",
    "fr": "Désolé, je n'ai pas bien entendu. Pouvez-vous répéter ?",
    "zh": "抱歉，我没有听清楚。您可以再说一遍吗？",
    "es": "Perdón, no le entendí. ¿Puede repetirlo?",
    "de": "Entschuldigung, das habe ich nicht verstanden.",
}
_SWITCHED = {
    "en": "Great — how can I help you today?",
    "fr": "Parfait — comment puis-je vous aider ?",
    "zh": "好的，请问有什么可以帮您？",
    "es": "Perfecto — ¿en qué puedo ayudarle?",
    "de": "Gut — wie kann ich Ihnen helfen?",
}
# Synthesized once per process, not per call: four TTS round trips at the top
# of every call would hand back the latency this transport exists to remove.
_MENU_AUDIO: Optional[bytes] = None


def menu_audio() -> bytes:
    """μ-law of the four options, each in its own voice, concatenated.

    Concatenation is valid because every clip is the same format (8 kHz mono
    μ-law), so the bytes simply play in sequence."""
    global _MENU_AUDIO
    if _MENU_AUDIO is None:
        from app.core.sdr import _LANG_MENU_ORDER, _LANG_MENU_TEXT
        parts = []
        for digit in _LANG_MENU_ORDER:
            code, text = _LANG_MENU_TEXT[digit]
            clip = synthesize(text, code)
            if clip:
                parts.append(clip)
        _MENU_AUDIO = b"".join(parts)
        logger.info(f"[stream] language menu cached ({len(_MENU_AUDIO)} bytes, "
                    f"{len(_MENU_AUDIO) / 8000:.1f}s)")
    return _MENU_AUDIO

# Telnyx <Stream> playback format. These must describe the bytes synthesize()
# returns; a mismatch is SILENT — the carrier drops the frames and the caller
# just hears nothing. Overridable so a codec change is config, not a deploy.
BIDI_MODE = os.getenv("VOICE_STREAM_BIDI_MODE", "rtp").strip()
BIDI_CODEC = os.getenv("VOICE_STREAM_BIDI_CODEC", "PCMU").strip()
BIDI_RATE = os.getenv("VOICE_STREAM_BIDI_RATE", "8000").strip()

_FRAME_MS = 20                # carrier frames are 20 ms of 8 kHz μ-law
_MAX_UTTER_MS = 25_000        # hard cap per utterance
_MIN_UTTER_MS = 240           # shorter than this = noise blip, not speech
_START_FRAMES = 2             # voiced frames before "speech started"
_CHUNK_BYTES = 3200           # 400 ms per outbound media message
_CHUNK_SECS = _CHUNK_BYTES / 8000.0        # μ-law 8 kHz = 8000 bytes/second
# Fraction of real time to sleep between chunks. <1 keeps a small lead so the
# carrier never starves; the smaller the lead, the less audio is buffered
# ahead and the sooner a barge-in actually goes quiet.
_PLAY_LEAD = float(os.getenv("VOICE_STREAM_PLAY_LEAD", "0.85"))
_STT_TIMEOUT = 20.0
_TTS_TIMEOUT = 20.0

_LINES = ("support", "sdr")

# Transport-level counters only. Per-provider STT/TTS counts moved to
# speech._stats, which keys them by provider name so a new vendor appears in
# the status endpoint without this dict having to learn about it.
_stats = {"calls": 0, "utterances": 0, "barge_ins": 0, "urgent_calls": 0,
          "echo_suppressed": 0, "echo_prevented": 0,
          "resumed_after_noise": 0}

# Urgency proxy thresholds — deterministic prosody stand-ins (no audio ML):
# repeated barge-ins or fast sustained speech read as elevated urgency.
URGENCY_BARGE_MIN = int(os.getenv("VOICE_URGENCY_BARGE_MIN", "2"))
URGENCY_WPM = int(os.getenv("VOICE_URGENCY_WPM", "185"))
URGENCY_MIN_WORDS = int(os.getenv("VOICE_URGENCY_MIN_WORDS", "12"))


def _public_ws_base() -> str:
    base = (os.getenv("VOICE_STREAM_PUBLIC_BASE")
            or os.getenv("TELNYX_PUBLIC_BASE") or os.getenv("APP_URL") or "").strip()
    if not base:
        return ""
    return base.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")


# ============================================================================
# WS AUTH — HMAC minted in the signature-verified webhook binds the params
# ============================================================================

def _secret() -> bytes:
    return (os.getenv("TELNYX_API_KEY") or os.getenv("TWILIO_AUTH_TOKEN")
            or os.getenv("ADMIN_API_TOKEN") or "voice-stream").encode()


def stream_token(line: str, call_sid: str, from_number: str) -> str:
    return _hmac.new(_secret(), f"{line}|{call_sid}|{from_number}".encode(),
                     hashlib.sha256).hexdigest()[:32]


def _token_ok(line: str, call_sid: str, from_number: str, token: str) -> bool:
    return _hmac.compare_digest(stream_token(line, call_sid, from_number),
                                token or "")


def stream_twiml(line: str, call_sid: str, from_number: str) -> Optional[str]:
    """The <Connect><Stream> fragment for an inbound webhook — or None when
    the upgrade is off/unconfigured, so callers fall back to <Gather>."""
    if not ENABLED or line not in _LINES:
        return None
    base = _public_ws_base()
    if not base:
        return None
    from urllib.parse import quote

    from app.core.telephony import _provider, _twiml_escape
    url = (f"{base}/voice/stream/{line}?sid={quote(call_sid)}"
           f"&frm={quote(from_number)}"
           f"&t={stream_token(line, call_sid, from_number)}")
    # ── Bidirectional playback ──────────────────────────────────────────────
    # Telnyx will happily fork the caller's audio TO us with no extra
    # attributes, which is why inbound worked (speech arrived, STT ran) while
    # the caller heard nothing at all: bidirectionalMode defaults to "mp3", and
    # we send raw base64 PCMU μ-law. Telnyx discarded every outbound frame as
    # malformed MP3 — silently, because a stream that plays nothing is not an
    # error from the carrier's point of view. "rtp" is the mode that matches
    # what synthesize() actually produces (PCMU, 8 kHz — both Telnyx defaults,
    # sent explicitly so a default change cannot silence the line again).
    #
    # Telnyx-only: these attributes are not part of Twilio's <Stream>, so they
    # are emitted only for the provider that defines them.
    extra = ""
    if _provider() == "telnyx":
        extra = (f' bidirectionalMode="{BIDI_MODE}"'
                 f' bidirectionalCodec="{BIDI_CODEC}"'
                 f' bidirectionalSamplingRate="{BIDI_RATE}"')

    # XML-escape the URL before it goes into an ATTRIBUTE. A query string has
    # a bare '&' between parameters, and a bare '&' is not legal XML — so this
    # document did not parse AT ALL, and the carrier rejected the whole
    # response rather than dialling the stream. That is indistinguishable, from
    # the outside, from "the carrier cannot reach the server": the call fails
    # the moment streaming is enabled and works again the moment it is turned
    # off, because the <Gather> fallback path builds no query string.
    return f'<Connect><Stream url="{_twiml_escape(url)}"{extra}/></Connect>'


# ============================================================================
# AUDIO — μ-law 8 kHz ⇄ PCM16, WAV framing, resample
# ============================================================================

def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    return audioop.ulaw2lin(ulaw, 2)


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    return audioop.lin2ulaw(pcm, 2)


def frame_rms(ulaw_frame: bytes) -> int:
    try:
        return audioop.rms(ulaw_to_pcm16(ulaw_frame), 2)
    except Exception:
        return 0


def wav_from_pcm16(pcm: bytes, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    buf.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
    buf.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    buf.write(b"data" + struct.pack("<I", len(pcm)) + pcm)
    return buf.getvalue()


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    out, _ = audioop.ratecv(pcm, 2, 1, src_rate, dst_rate, None)
    return out


# ============================================================================
# STT / TTS — delegated to the provider-pluggable seam
# ============================================================================
# These were four vendor-hardwired functions. They now live in app/core/speech.py
# behind an ordered provider list, so which vendor hears the caller and which
# one answers is a Railway variable rather than a deploy. The two wrappers below
# keep this module's existing call sites and their PCM-in / μ-law-out contract
# unchanged — the seam takes WAV, and building the WAV header is this
# transport's job because it is this transport that knows the audio is 8 kHz.


def transcribe(pcm16_8k: bytes, lang: str = "en", tag: str = "") -> str:
    """Utterance PCM → text ('' when nothing recognizable). Never raises.

    `tag` is the call id, under which the shadow recogniser's reading of the
    same audio is filed. The brain reads it back for proper nouns, where the
    two vendors diverge most — see speech.alternates.
    """
    return speech.stt(wav_from_pcm16(pcm16_8k), lang, tag=tag)


def synthesize(text: str, lang: str = "en") -> bytes:
    """Reply text → 8 kHz μ-law (b'' on total failure — the call then just
    stays silent for that turn rather than dying)."""
    return speech.tts(text, lang)


# ============================================================================
# BRAINS — the same two lines, addressed by name
# ============================================================================

async def _brain_greet(line: str, call_sid: str, from_number: str) -> Tuple[str, str]:
    if line == "support":
        from app.core import voice_support as vs
        if not vs.ENABLED:
            return ("The phone assistant is offline. Goodbye.", "hangup")
        return vs.greet_call(call_sid, from_number), "speech"
    from app.core import sdr
    if not sdr.VOICE_ENABLED:
        return ("The voice assistant is offline. Goodbye.", "hangup")
    return ("Hi! You've reached the Conscestra C R M assistant. "
            "I can answer questions and book you a meeting with our team. "
            "How can I help you today?", "speech")


async def _brain_turn(line: str, call_sid: str, heard: str,
                      from_number: str = "") -> Tuple[str, str]:
    if line == "support":
        from app.core import voice_support as vs
        return await vs.take_turn(call_sid, heard)
    from app.core import sdr
    res = await asyncio.to_thread(sdr.converse, f"voice-{call_sid}", heard,
                                  "voice", from_number or None)
    return res["reply"], ("hangup" if res.get("done") else "speech")


async def _brain_digits(line: str, call_sid: str, digits: str) -> Tuple[str, str]:
    if line == "support":
        from app.core import voice_support as vs
        return await vs.take_digits(call_sid, digits)
    return "", "speech"        # the SDR line has no keypad flow


def _call_urgency(n_barge: int, words: int, speech_ms: float) -> Optional[str]:
    """Why this call reads as urgent, or None. Word count gates the wpm check
    so a single short exclamation can't trip it."""
    if n_barge >= URGENCY_BARGE_MIN:
        return f"caller interrupted the assistant {n_barge} times"
    if words >= URGENCY_MIN_WORDS and speech_ms > 0:
        wpm = words / (speech_ms / 60000.0)
        if wpm >= URGENCY_WPM:
            return f"fast sustained speech (~{int(wpm)} wpm)"
    return None


def _post_urgency(line: str, from_number: str, reason: str) -> None:
    """Elevated urgency → a blackboard signal on the resolved caller, the same
    channel the negative_sentiment signal uses — so the AI 360 summary and the
    supervisor's detectors pick it up with zero extra wiring. Anonymous callers
    have no entity to signal on and are skipped."""
    try:
        from app.core import blackboard, identity
        ident = identity.resolve("voice", from_number)
        if not (ident.resolved and ident.scope == "external" and ident.party_id):
            return
        blackboard.post(ident.party_type, ident.party_id, "voice_stream",
                        "voice_urgency",
                        note=f"Elevated urgency on a {line} call: {reason}.",
                        severity="medium", ttl_hours=7 * 24)
        _stats["urgent_calls"] += 1
        logger.info(f"[stream] voice_urgency posted for "
                    f"{ident.party_type} {ident.party_id[:8]}: {reason}")
    except Exception as exc:
        logger.debug(f"[stream] urgency signal skipped: {exc}")


def _brain_hangup(line: str, call_sid: str) -> None:
    """Caller hung up mid-conversation — close the support transcript."""
    if line != "support":
        return
    try:
        from app.core import voice_support as vs
        sess = vs._CALLS.get(call_sid)
        if sess and sess.get("transcript"):
            vs._close_call(sess, "caller hung up")   # idempotent — no-op if closed
    except Exception as exc:
        logger.debug(f"[stream] hangup close skipped: {exc}")


# ============================================================================
# THE STREAM — one WS per call
# ============================================================================

class _Call:
    """Per-call stream state: VAD segmentation, DTMF buffer, playback task."""

    def __init__(self, ws: WebSocket, line: str, call_sid: str,
                 from_number: str):
        self.ws, self.line = ws, line
        self.call_sid, self.from_number = call_sid, from_number
        self.stream_id = ""
        self.sid_key = "streamSid"          # echo the carrier's spelling
        self.utter = bytearray()            # PCM16 of the current utterance
        self.voiced = 0                     # consecutive voiced frames
        self.silent = 0                     # consecutive silent frames
        self.in_speech = False
        self.mode = "speech"                # speech | digits | hangup
        self.digits = ""
        self.player: Optional[asyncio.Task] = None
        self.worker: Optional[asyncio.Task] = None
        self.closing = False
        self.n_barge = 0                    # this call's barge-in count
        self.epoch = 0                      # bumped whenever the caller speaks
                                            # — invalidates in-flight synthesis
        self.lang = "en"                    # recognition + voice move together
        self.pending = bytearray()          # speech heard while busy (never
                                            # dropped — see _finish_utterance)
        self.call_control_id = ""           # carrier handle for a live transfer
        self.words = 0                      # transcribed words heard
        self.speech_ms = 0.0                # caller speech duration
        # ── echo state ──────────────────────────────────────────────────────
        # On a speakerphone the caller's handset plays our audio into its own
        # microphone and the carrier sends it straight back. The VAD cannot
        # tell that from speech, so the line hears itself: a live call
        # transcribed 4.5 s of OUR greeting as the caller saying "Thanks for
        # calling Conscestra. How can I help you today?" — which the brain then
        # tried to answer. These two fields are what let _is_echo tell the
        # difference; see it for why BOTH are needed.
        self.said_recent = ""               # normalised text of our last reply
        self.play_end = 0.0                 # when that reply finishes playing
        self.utter_started = 0.0            # when the current utterance began
        self.ref_pcm = b""                  # PCM16 of the audio we are playing
        self.play_t0 = 0.0                  # when that playback started
        self.probe = bytearray()            # candidate speech, not yet judged
        self.probing = False
        self.said_raw = ""                  # last reply, verbatim
        self.interrupted_reply = ""         # a reply cut short, resumable
        self.flux = None                    # Deepgram Flux turn shadow
        self.flux_tried = False

    # ── outbound audio ──────────────────────────────────────────────────────
    async def _stop_player(self) -> None:
        """Cancel the current playback and WAIT for it to actually stop.

        Cancelling without awaiting is not enough: the task keeps running until
        the event loop next schedules it, and if a second player is created in
        the meantime BOTH loops send media frames into the same stream. The
        carrier plays exactly what it receives, so the caller hears two voices
        talking over each other."""
        p = self.player
        self.player = None
        if p and not p.done():
            p.cancel()
            try:
                await p
            except (asyncio.CancelledError, Exception):
                pass

    async def say(self, text: str, then_hangup: bool = False,
                  append: bytes = b"") -> None:
        # Snapshot the interrupt epoch BEFORE synthesis, which is slow enough
        # (a network round trip) for the caller to start a new sentence inside
        # it. Without this the reply to the PREVIOUS utterance is played on top
        # of the reply to the current one — the barge-in cancelled a player
        # that had not been created yet, so it cancelled nothing.
        epoch = self.epoch
        # Speak the language of the WORDS, not the language of the question.
        # Detection had been reading the caller's utterance only, so "can you
        # speak Chinese?" — asked in English — left the voice on English while
        # the model answered in Mandarin, and an English engine reading Han
        # characters is exactly the half-switch this module exists to prevent.
        # language.detect needs real signal (two Han characters, or two scoring
        # function words), so a product name or a stray accent cannot flip it.
        self._adopt_reply_lang(text)
        audio = await asyncio.to_thread(synthesize, text, self.lang)
        if epoch != self.epoch:
            logger.info(f"[stream] dropped a stale reply for "
                        f"{self.call_sid[:12]} (caller spoke while it was "
                        f"being synthesized)")
            return
        if not audio:
            logger.warning(f"[stream] no TTS audio for {self.call_sid[:12]}")
            if then_hangup:
                await self._close()
            return
        if append:
            audio += append          # same format (8 kHz mono mu-law) -> concat
        await self._stop_player()       # never two players on one stream
        # Remember what we are about to say, and until when, so an utterance
        # that is really our own audio coming back can be recognised as such.
        self.said_recent = _echo_norm(text)
        self.said_raw = text
        self.play_end = time.time() + len(audio) / 8000.0
        # The reference signal for echo_correlation. Decoded once per reply,
        # not per frame.
        self.ref_pcm = ulaw_to_pcm16(audio)
        self.play_t0 = time.time()
        self.player = asyncio.create_task(self._play(audio, then_hangup))

    async def _play(self, ulaw: bytes, then_hangup: bool) -> None:
        try:
            for i in range(0, len(ulaw), _CHUNK_BYTES):
                await self.ws.send_text(json.dumps({
                    "event": "media", self.sid_key: self.stream_id,
                    "media": {"payload":
                              base64.b64encode(ulaw[i:i + _CHUNK_BYTES]).decode()}}))
                # Pace just under real time. This used to push a 400 ms chunk
                # every 200 ms — 2x real time — on the theory that running
                # ahead kept barge-in responsive. It does the opposite: every
                # chunk sent early is a chunk sitting in the CARRIER's buffer,
                # and cancelling our sender cannot un-play what the carrier has
                # already queued. Telnyx documents no "clear" verb (that is a
                # Twilio convention), so buffered audio is audio the caller
                # WILL hear after interrupting. A small lead keeps the stream
                # from starving; anything more just makes barge-in worse.
                await asyncio.sleep(_CHUNK_SECS * _PLAY_LEAD)
            await self.ws.send_text(json.dumps({
                "event": "mark", self.sid_key: self.stream_id,
                "mark": {"name": "eot"}}))
            if then_hangup:
                self.closing = True
                await asyncio.sleep(len(ulaw) / 8000 + 1.0)  # let it drain
                await self._close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[stream] play ended: {exc}")

    async def _barge_in(self) -> None:
        if self.player and not self.player.done():
            # What we were part-way through saying, in case whatever
            # interrupted turns out to have been noise.
            self.interrupted_reply = self.said_raw
            self.player.cancel()
            _stats["barge_ins"] += 1
            self.n_barge += 1
            try:
                await self.ws.send_text(json.dumps(
                    {"event": "clear", self.sid_key: self.stream_id}))
            except Exception:
                pass

    async def _close(self) -> None:
        if self.flux is not None:
            try:
                await self.flux.close()
            except Exception:
                pass
            self.flux = None
        try:
            await self.ws.close()
        except Exception:
            pass

    # ── inbound handling ────────────────────────────────────────────────────
    async def on_media(self, payload_b64: str) -> None:
        if self.closing:
            return
        try:
            ulaw = base64.b64decode(payload_b64)
        except Exception:
            return
        rms = frame_rms(ulaw)

        # Mirror the caller's audio to the Flux turn shadow. Fire-and-forget:
        # feed() is non-blocking and drops frames rather than ever making the
        # carrier wait. Started lazily on the first frame so a call in an
        # unsupported language (zh) never opens a socket at all.
        if self.flux is not None:
            self.flux.feed(ulaw)
        elif not self.flux_tried and flux_stream.enabled_for(self.lang):
            self.flux_tried = True
            asyncio.create_task(self._start_flux())

        # ── probe before interrupting ourselves ─────────────────────────────
        # While our own audio is playing, energy on the line is as likely to be
        # that audio coming back off a speakerphone as it is to be the caller.
        # Buffer a contiguous window and CORRELATE it against what we sent
        # before deciding. Nothing is lost either way: the buffered frames
        # become the head of the utterance when it turns out to be speech.
        #
        # Buffering continues through SILENT frames too. Gating it on voiced
        # frames only looked tidier and was a trap: real speech has micro-pauses
        # ("stop — wait — I need a person"), every pause reset the buffer, the
        # window never filled, and because this branch returns early the normal
        # start-of-speech path never ran either. A caller who paused could not
        # interrupt us at all — a worse bug than the echo. A contiguous window
        # is also what the correlation wants, since the reference contains the
        # same pauses.
        if self.probing:
            self.probe.extend(ulaw_to_pcm16(ulaw))
            if len(self.probe) >= ECHO_PROBE_MS * 16:
                await self._settle_probe()
            return

        if rms >= VAD_RMS:
            self.voiced += 1
            self.silent = 0
            if (not self.in_speech and self.voiced >= _START_FRAMES
                    and self._echo_check_live()):
                self.probing = True
                self.probe.clear()
                self.probe.extend(ulaw_to_pcm16(ulaw))
                return
            if not self.in_speech and self.voiced >= _START_FRAMES:
                self.in_speech = True
                self.utter_started = time.time()
                # Bump the epoch on EVERY speech start, not only when something
                # is playing: a reply may be mid-synthesis with no player yet,
                # and that reply is already stale the moment the caller starts
                # a new sentence. _barge_in only handles audio already flowing.
                self.epoch += 1
                await self._barge_in()          # caller talked over us
            if self.in_speech:
                self.utter.extend(ulaw_to_pcm16(ulaw))
        else:
            self.voiced = 0
            if self.in_speech:
                self.silent += 1
                self.utter.extend(ulaw_to_pcm16(ulaw))
                if self.silent * _FRAME_MS >= SILENCE_MS:
                    await self._finish_utterance()
        if self.in_speech and len(self.utter) / 16 >= _MAX_UTTER_MS:
            await self._finish_utterance()

    async def _start_flux(self) -> None:
        """Open the shadow. Any failure disables Flux for this call only."""
        try:
            sess = flux_stream.FluxSession(self.call_sid, self.lang)
            if await sess.start():
                self.flux = sess
                logger.info("[stream] %s flux shadow open (%s)",
                            self.call_sid[:12], self.lang)
        except Exception as exc:
            logger.info("[stream] %s flux shadow unavailable (%s)",
                        self.call_sid[:12], type(exc).__name__)

    def _record_flux_turn(self, heard: str, utter_ms: float) -> None:
        """Compare the shadow's turn-end against the one the VAD just made.

        Called at the moment the VAD closes an utterance, which by
        construction is SILENCE_MS after the caller stopped. Flux's timestamp
        is absolute, so the difference is how much sooner it knew.
        """
        if self.flux is None:
            return
        turn = self.flux.take_turn()
        vad_ms = SILENCE_MS
        if turn is None:
            # Flux said nothing. This is the result that would sink the idea,
            # so it is recorded as loudly as a win: flux_ms NULL.
            flux_ms = None
            delta = None
            ftext = ""
            conf = self.flux.last_conf
            n_turns = 0
            all_turns = ""
        else:
            # The VAD declared the end SILENCE_MS after the last speech frame;
            # Flux's turn landed at `at`. Positive delta = Flux got there first.
            ago_ms = max(0.0, (time.time() - turn["at"]) * 1000)
            flux_ms = int(max(0.0, vad_ms - ago_ms))
            delta = int(ago_ms)
            ftext = turn["text"]
            conf = turn["conf"]
            n_turns = int(turn.get("n_turns") or 1)
            all_turns = " | ".join(turn.get("all") or [])
        a, b = len(_echo_norm(heard).split()), len(_echo_norm(ftext).split())
        truncated = bool(ftext) and a > 2 and b < a * 0.7
        _flux_pool.submit(_write_flux_turn, (
            self.call_sid, self.line, self.lang, int(utter_ms), int(vad_ms),
            flux_ms, delta, float(conf or 0.0), heard[:300], ftext[:300],
            truncated, n_turns, all_turns[:600]))

    def _echo_check_live(self) -> bool:
        """Is an echo check both possible and worth doing right now?

        Only while our own audio is actually playing. Once playback is over
        there is nothing to echo, and every frame is the caller's.
        """
        return bool(
            _np is not None
            and self.player and not self.player.done()
            and self.ref_pcm
            and time.time() <= self.play_end + ECHO_TAIL_MS / 1000.0
        )

    async def _settle_probe(self) -> None:
        """Decide what the buffered probe was, and commit to it.

        Whichever way this goes, the probe audio is accounted for: kept as the
        head of the utterance, or discarded as our own echo. It is never
        silently dropped, because the one unacceptable outcome here is losing
        the first quarter-second of a caller interrupting us.
        """
        probe = bytes(self.probe)
        self.probe.clear()
        self.probing = False
        # A window that went quiet almost immediately was a blip — a cough, a
        # door, a keypad click — not the start of a sentence. Neither echo nor
        # speech: drop it and keep listening, rather than interrupting
        # ourselves over a noise.
        pos = int(max(0.0, time.time() - self.play_t0) * 8000)
        voiced_ms = 0
        if audioop is not None and probe:
            voiced_ms = sum(
                _FRAME_MS for i in range(0, len(probe) - 159, 160)
                if audioop.rms(probe[i:i + 160], 2) >= VAD_RMS)
            if voiced_ms < _START_FRAMES * _FRAME_MS * 2:
                _log_probe(self, "blip", 0.0, pos, probe, voiced_ms)
                self.voiced = 0
                return
        score = echo_correlation(probe, self.ref_pcm, pos)
        # EVERY decision is recorded, not only the ones that fire. A log
        # censored at the threshold cannot show where the threshold should be:
        # it has no false negatives in it by construction. See
        # sql/voice_echo_probe.sql and app/core/echo_tune.py.
        _log_probe(self, "echo" if score >= ECHO_CORR else "speech",
                   score, pos, probe, voiced_ms)
        if score >= ECHO_CORR:
            _stats["echo_prevented"] += 1
            logger.info(f"[stream] {self.call_sid[:12]} ignored our own audio "
                        f"on the line (corr {score:.2f}) — kept speaking")
            self.voiced = 0          # do NOT interrupt ourselves
            return
        # Real speech over our playback: this is the barge-in the transport
        # exists for. Interrupt, and keep the probe as the start of what they
        # said — those 240 ms hold the beginning of their sentence.
        self.in_speech = True
        self.utter_started = time.time() - ECHO_PROBE_MS / 1000.0
        self.utter.extend(probe)
        self.epoch += 1
        await self._barge_in()

    async def _finish_utterance(self) -> None:
        pcm = bytes(self.utter)
        self.utter.clear()
        self.in_speech = False
        self.silent = self.voiced = 0
        if len(pcm) / 16 < _MIN_UTTER_MS:       # 16 bytes per ms at 8 kHz s16
            return
        if self.worker and not self.worker.done():
            # Do NOT discard what the caller just said. A natural mid-sentence
            # pause ends one utterance and starts another, and dropping the
            # second half is why "tell me the refund policy" reached the brain
            # as "Tell me." — the agent answered the fragment it got, the
            # caller heard a reply that ignored the question, and the words
            # carrying the actual question were never transcribed at all.
            self.pending.extend(pcm)
            # The reply being composed answers only the fragment, so abandon
            # it: one question deserves one answer, not a confused reply to
            # half a sentence followed by the real one. Bumping the epoch also
            # stops any already-synthesized audio for it from being played.
            self.epoch += 1
            self.worker.cancel()
            logger.info(f"[stream] {self.call_sid[:12]}: sentence continued "
                        f"after a pause — re-asking with the whole thing")
            return
        if self.pending:                        # continuation of a split
            pcm = bytes(self.pending) + pcm     # sentence — rejoin the halves
            self.pending.clear()
        _stats["utterances"] += 1
        self.worker = asyncio.create_task(self._respond(pcm))

    async def _respond(self, pcm: bytes) -> None:
        delivered = False
        try:
            delivered = await self._respond_once(pcm)
        finally:
            # Speech that arrived while this reply was in flight is a real
            # question waiting for an answer, not noise. Drain it here, once
            # the caller has stopped talking, so a split sentence still gets
            # answered instead of vanishing. If this turn was ABANDONED
            # mid-flight (a continuation arrived), its audio never got an
            # answer — so put it back in front of the continuation and ask
            # the whole sentence as one.
            if self.pending and not self.in_speech and not self.closing:
                head = b"" if delivered else pcm
                held = head + bytes(self.pending)
                self.pending.clear()
                self.worker = asyncio.create_task(self._respond(held))

    async def _respond_once(self, pcm: bytes) -> bool:
        """True when a reply was actually handed to the caller."""
        t0 = time.time()
        heard = await asyncio.to_thread(transcribe, pcm, self.lang,
                                        self.call_sid)
        _label_probe(self.call_sid, heard)
        try:
            self._record_flux_turn(heard, len(pcm) / 16)
        except Exception as exc:      # a shadow must never break a live turn
            logger.debug("[flux] comparison skipped: %s", exc)
        if not heard:
            # NOISE, not speech. A live call showed the cost: 1142 ms of home-
            # phone noise passed the VAD mid-answer, cancelled the reply, and
            # transcribed to nothing — so the caller was told "sorry, I didn't
            # catch that" about a sentence they never said, and had to ask
            # their whole question again.
            #
            # If we cut ourselves off to listen to that, apologising is the
            # wrong move twice over: it blames the caller for silence, and it
            # throws away the answer they were already being given. Resume the
            # reply instead. Only when we did NOT interrupt anything is "I
            # didn't catch that" the truthful thing to say.
            if self.interrupted_reply:
                resume, self.interrupted_reply = self.interrupted_reply, ""
                _stats["resumed_after_noise"] += 1
                logger.info(f"[stream] {self.call_sid[:12]} noise cut our reply "
                            f"and said nothing — resuming it")
                await self.say(resume)
                return True
            await self.say(_RETRY.get(self.lang, _RETRY["en"]))
            return True
        self.interrupted_reply = ""
        if is_echo(heard, self.said_recent, self.utter_started, self.play_end):
            # Our own voice, returned by a speakerphone. Say NOTHING: a reply
            # here would be answering ourselves, and the "sorry, I didn't catch
            # that" line would be just as wrong — the caller has not spoken yet
            # and would be told they were unintelligible.
            _stats["echo_suppressed"] += 1
            self.n_barge = max(0, self.n_barge - 1)   # not a real interruption
            logger.info(f"[stream] {self.call_sid[:12]} suppressed echo of our "
                        f"own audio: {heard[:60]!r}")
            # TRUE, even though nothing was said back. The flag does not mean
            # "we replied", it decides whether this audio is RE-ASKED in front
            # of whatever the caller says next. Returning False would splice
            # our own echoed voice onto the start of their real sentence and
            # transcribe the two as one. Echo is the only input here that must
            # be discarded outright rather than retried.
            return True
        self.speech_ms += len(pcm) / 16     # 16 bytes per ms at 8 kHz s16
        self.words += len(heard.split())
        logger.info(f"[stream] {self.line} {self.call_sid[:12]} heard "
                    f"({time.time() - t0:.1f}s): {heard[:80]!r}")
        say, nxt = await _brain_turn(self.line, self.call_sid, heard,
                                     self.from_number)
        # The brain may have decided the language FROM the caller's words
        # (voice_support._note_lang / sdr._call_lang), which the keypad never
        # touched. Adopt it before speaking, or the reply comes back in French
        # and is read aloud by the English voice — the half-switch this module
        # exists to prevent, arriving from the brain's side instead of ours.
        self._adopt_brain_lang()
        if nxt == "dial":
            return await self._transfer_to_human()
        self.mode = nxt
        await self.say(say, then_hangup=(nxt == "hangup"))
        return True

    async def _transfer_to_human(self) -> bool:
        """Hand the live call to a person via Call Control.

        The brain decided a human is warranted; <Dial> is not available on this
        transport, so the carrier API does it instead. A FAILED transfer must
        not drop the caller — it degrades to the same tracked callback the
        Gather path produces when nobody answers. Hanging up on someone who
        just asked for help is the one outcome worse than never offering."""
        from app.core import telephony, voice_support as vs
        say_line = vs._CONNECTING.get(self.lang, vs._CONNECTING["en"])
        await self.say(say_line)
        # Let "connecting you now" actually reach the caller before the carrier
        # tears this leg down. ~14 characters per second of speech.
        await asyncio.sleep(min(len(say_line) / 14.0, 6.0))
        res = await asyncio.to_thread(
            telephony.transfer_call, self.call_control_id,
            vs.transfer_number(), vs._transfer_caller_id(),
            vs.TRANSFER_TIMEOUT)
        if res.get("ok"):
            logger.info(f"[stream] call {self.call_sid[:12]} transferred to a "
                        f"human ({res.get('to')})")
            self.closing = True
            return True
        logger.warning(f"[stream] transfer failed for {self.call_sid[:12]}: "
                       f"{res.get('error')} — taking a message instead")
        window = vs.transfer_window()
        apology = vs.no_answer_message(self.lang, window)
        try:
            vs.open_callback_obligation(
                conversation_id=None, handle=self.from_number, channel="voice",
                heard=f"transfer failed ({res.get('error')})", window=window)
        except Exception as exc:
            logger.error(f"[stream] could not record the callback: {exc}")
        await self.say(apology, then_hangup=True)
        return True

    def _adopt_reply_lang(self, text: str) -> None:
        """Switch to the language we are about to SPEAK, if it is one we serve.

        Moves BOTH halves via set_lang: if the assistant just answered in
        Mandarin, the caller's next sentence will very likely be Mandarin too,
        and leaving the recogniser on English would make their reply
        unintelligible one turn later."""
        try:
            from app.core import language
            code = language.detect(text or "")
        except Exception:
            return
        if code and code != self.lang and code in _AZURE_BY_LANG:
            logger.info(f"[stream] call {self.call_sid[:12]} replying in "
                        f"{code} — switching voice and recogniser to match")
            self.set_lang(code)

    def _adopt_brain_lang(self) -> None:
        try:
            if self.line == "support":
                from app.core import voice_support as vs
                code = (vs._CALLS.get(self.call_sid) or {}).get("lang")
            else:
                from app.core.sdr import _VOICE_LANG
                code = _VOICE_LANG.get(self.call_sid)
        except Exception:
            return
        if code and code != self.lang and code in _AZURE_BY_LANG:
            # set_lang() would write straight back to the brain; harmless, but
            # this direction is brain -> transport, so only move our half.
            self.lang = code
            logger.info(f"[stream] call {self.call_sid[:12]} adopting language "
                        f"{code} detected from speech")

    def set_lang(self, code: str) -> None:
        """Pin this call's language and tell the BRAIN about it too.

        The brain composes the words (voice_support reads sess['lang'] to pick
        its fixed lines and the reply-language directive) while this module
        speaks and hears them. Setting one without the other gives a Mandarin
        answer in an English voice, or the reverse."""
        if code not in _AZURE_BY_LANG or code == self.lang:
            return
        self.lang = code
        try:
            if self.line == "support":
                from app.core import voice_support as vs
                sess = vs._CALLS.get(self.call_sid)
                if sess is not None:
                    sess["lang"] = code
            else:
                from app.core.sdr import set_call_lang
                set_call_lang(self.call_sid, code)
        except Exception as exc:
            logger.debug(f"[stream] brain language sync skipped: {exc}")
        logger.info(f"[stream] call {self.call_sid[:12]} language → {code}")

    async def on_dtmf(self, digit: str) -> None:
        # A language keypress is accepted at ANY time, not only during the
        # opening menu: DTMF is signalling, so it works even when the caller is
        # stuck behind a recogniser committed to the wrong language — which is
        # exactly when they need it. Checked BEFORE the OTP branch would
        # swallow it, and only outside digit entry so an OTP containing 1-4 is
        # never mistaken for a language choice.
        if self.mode != "digits" and digit:
            from app.core.sdr import _LANG_MENU
            code = _LANG_MENU.get(digit.strip()[:1])
            if code:
                self.set_lang(code)
                await self.say(_SWITCHED.get(code, _SWITCHED["en"]))
                return
        if self.mode != "digits" or not digit:
            return
        self.digits += digit.strip()[:1]
        if len(self.digits) < 6 and "#" not in self.digits:
            return
        digits, self.digits = self.digits.replace("#", ""), ""
        say, nxt = await _brain_digits(self.line, self.call_sid, digits)
        self.mode = nxt
        await self.say(say, then_hangup=(nxt == "hangup"))


router_ws = APIRouter()


@router_ws.websocket("/voice/stream/{line}")
async def voice_stream_ws(ws: WebSocket, line: str, sid: str = "",
                          frm: str = "", t: str = ""):
    """One carrier media stream = one call. The HMAC token (minted in the
    signature-verified inbound webhook) is the authorization."""
    if line not in _LINES or not _token_ok(line, sid, frm, t):
        await ws.close(code=4403)
        return
    await ws.accept()
    _stats["calls"] += 1
    # Open the vendor TLS connections while the carrier is still setting up
    # the stream, so the greeting is not the request that pays for them.
    speech.prewarm()
    call = _Call(ws, line, sid, frm)
    logger.info(f"[stream] {line} call {sid[:12]} connected ({frm or '?'})")
    greeted = False
    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            event = data.get("event")
            if event == "start":
                start = data.get("start") or {}
                call.stream_id = (data.get("streamSid") or data.get("stream_id")
                                  or start.get("streamSid")
                                  or start.get("stream_id") or "")
                call.sid_key = ("stream_id" if ("stream_id" in data
                                                or "stream_id" in start)
                                else "streamSid")
                # The only place the carrier hands us a handle on the LIVE
                # call. Without it this transport cannot transfer to a human
                # at all: <Dial> answers a webhook, and there is no webhook
                # left to answer once the media stream is open.
                call.call_control_id = (start.get("call_control_id")
                                        or data.get("call_control_id") or "")
                if not greeted:
                    greeted = True
                    say, nxt = await _brain_greet(line, sid, frm)
                    # The keypad language menu, which the <Gather> path plays
                    # and this one silently skipped — the inbound webhook
                    # returns the <Stream> before it ever reaches the menu, so
                    # switching transports quietly turned a four-language line
                    # into an English-only one. Appended as pre-rendered audio
                    # rather than a second turn, so it costs no extra round trip.
                    # Offered on every tier, staff included: the greeting names
                    # the four languages, so suppressing the menu for one tier
                    # would advertise a choice that tier cannot make.
                    menu = b"" if nxt == "hangup" \
                        else await asyncio.to_thread(menu_audio)
                    await call.say(say, then_hangup=(nxt == "hangup"),
                                   append=menu)
            elif event == "media":
                await call.on_media((data.get("media") or {}).get("payload", ""))
            elif event == "dtmf":
                await call.on_dtmf(str((data.get("dtmf") or {}).get("digit", "")))
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning(f"[stream] {line} call {sid[:12]} error: {exc}")
    finally:
        for task in (call.player, call.worker):
            if task and not task.done():
                task.cancel()
        if not call.closing:
            _brain_hangup(line, sid)
        reason = _call_urgency(call.n_barge, call.words, call.speech_ms)
        if reason:
            await asyncio.to_thread(_post_urgency, line, frm, reason)
        logger.info(f"[stream] {line} call {sid[:12]} ended")


# ============================================================================
# Admin status
# ============================================================================

router = APIRouter(tags=["voice-stream"])


@router.get("/voice-stream/status")
def voice_stream_status():
    """Transport config + who is actually serving speech.

    `speech` reports the ORDER per language rather than a single winner,
    because "azure is serving" and "azure is first of three, with deepgram
    shadowing" are different operational facts and the second one is the one
    you need while a migration is in flight.
    """
    return {"enabled": ENABLED, "ws_base": _public_ws_base() or None,
            "vad_rms": VAD_RMS, "silence_ms": SILENCE_MS,
            "tts_voice": TTS_VOICE,
            "speech": speech.status(),
            "flux": {"enabled": flux_stream.enabled_for("en"),
                     "mode": flux_stream.mode(),
                     "supported": sorted(flux_stream.SUPPORTED),
                     **flux_stream.stats()},
            "stats": {**_stats, **speech.stats()}}
