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
import struct
import time
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("voice_stream")

# audioop is stdlib through 3.12; on 3.13+ the audioop-lts package provides
# the same module (requirements.txt). Its absence must degrade THIS feature,
# never crash the whole app at import — main.py imports this module
# unconditionally.
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
# (recognition locale, Azure neural voice). English keeps JennyNeural, the
# voice already in use, so enabling this changes nothing for English callers.
_AZURE_BY_LANG = {
    "en": ("en-US", TTS_VOICE or "en-US-JennyNeural"),
    "fr": ("fr-CA", os.getenv("VOICE_TTS_VOICE_FR", "fr-CA-SylvieNeural")),
    "zh": ("zh-CN", os.getenv("VOICE_TTS_VOICE_ZH", "zh-CN-XiaoxiaoNeural")),
    "es": ("es-MX", os.getenv("VOICE_TTS_VOICE_ES", "es-MX-DaliaNeural")),
    "de": ("de-DE", os.getenv("VOICE_TTS_VOICE_DE", "de-DE-KatjaNeural")),
}


def _azure_pair(lang: str) -> Tuple[str, str]:
    return _AZURE_BY_LANG.get(lang or "en", _AZURE_BY_LANG["en"])


# The opening keypad menu, and the confirmation after a choice. Each line is
# spoken by ITS OWN language's voice — an English engine reading "中文服务，
# 请按 3" is unintelligible to the one caller who needs that option.
# Deliberately terse. The full-sentence version of these four options ran 10.4
# seconds, on top of an 11-second greeting — 22 seconds before the caller could
# say anything, which gives back exactly the latency this transport buys. Every
# option still names its language in its own language, which is the part that
# has to survive: it is the only cue a caller who speaks no English can use.
_MENU_PROMPT = {
    "en": "English, press 1.",
    "fr": "Français, 2.",
    "zh": "中文，3。",
    "es": "Español, 4.",
}
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
        parts = []
        for code in ("en", "fr", "zh", "es"):
            clip = synthesize(_MENU_PROMPT[code], code)
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

_stats = {"calls": 0, "utterances": 0, "stt_azure": 0, "stt_whisper": 0,
          "stt_failures": 0, "tts_azure": 0, "tts_openai": 0, "barge_ins": 0,
          "urgent_calls": 0}

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
# STT — Azure short-audio REST first (fast, keyed), Whisper fallback
# ============================================================================

def _azure_key_region() -> Tuple[str, str]:
    from app.core.config import get_settings
    s = get_settings()
    return (getattr(s, "azure_speech_key", "") or "",
            getattr(s, "azure_speech_region", "") or "eastus")


def _stt_azure(wav: bytes, lang: str = "en") -> Optional[str]:
    key, region = _azure_key_region()
    if not key:
        return None
    r = httpx.post(
        f"https://{region}.stt.speech.microsoft.com/speech/recognition/"
        "conversation/cognitiveservices/v1",
        params={"language": _azure_pair(lang)[0], "format": "simple"},
        headers={"Ocp-Apim-Subscription-Key": key,
                 "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=8000"},
        content=wav, timeout=_STT_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("RecognitionStatus") != "Success":
        return ""
    return (j.get("DisplayText") or "").strip()


def _stt_whisper(wav: bytes, lang: str = "en") -> Optional[str]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    r = httpx.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        # Was pinned to "en", so the fallback transcriber ALSO could not hear
        # any other language — a second English-only choke point behind the
        # first. Whisper takes a bare ISO-639-1 code, not Azure's locale.
        data={"model": "whisper-1", "language": (lang or "en")[:2]},
        files={"file": ("utterance.wav", wav, "audio/wav")},
        timeout=_STT_TIMEOUT)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def transcribe(pcm16_8k: bytes, lang: str = "en") -> str:
    """Utterance PCM → text ('' when nothing recognizable). Azure first,
    Whisper fallback — mirrors the ddgs→Tavily pattern: never raises."""
    wav = wav_from_pcm16(pcm16_8k)
    try:
        text = _stt_azure(wav, lang)
        if text is not None:
            _stats["stt_azure"] += 1
            return text
    except Exception as exc:
        logger.warning(f"[stream] azure STT failed: {exc}")
    try:
        text = _stt_whisper(wav, lang)
        if text is not None:
            _stats["stt_whisper"] += 1
            return text
    except Exception as exc:
        logger.warning(f"[stream] whisper STT failed: {exc}")
    _stats["stt_failures"] += 1
    return ""


# ============================================================================
# TTS — Azure REST emits 8 kHz μ-law natively; OpenAI PCM fallback
# ============================================================================

def _tts_azure(text: str, lang: str = "en") -> Optional[bytes]:
    key, region = _azure_key_region()
    if not key:
        return None
    locale, voice = _azure_pair(lang)
    ssml = (f'<speak version="1.0" xml:lang="{locale}">'
            f'<voice name="{voice}">'
            + (text.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
            + "</voice></speak>")
    r = httpx.post(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        headers={"Ocp-Apim-Subscription-Key": key,
                 "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "raw-8khz-8bit-mono-mulaw",
                 "User-Agent": "conscestra-voice"},
        content=ssml.encode("utf-8"), timeout=_TTS_TIMEOUT)
    r.raise_for_status()
    return r.content or None


def _tts_openai(text: str) -> Optional[bytes]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    r = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "tts-1", "voice": "alloy", "input": text[:4000],
              "response_format": "pcm"},           # 24 kHz s16le mono
        timeout=_TTS_TIMEOUT)
    r.raise_for_status()
    return pcm16_to_ulaw(resample_pcm16(r.content, 24000, 8000))


def synthesize(text: str, lang: str = "en") -> bytes:
    """Reply text → 8 kHz μ-law (b'' on total failure — the call then just
    stays silent for that turn rather than dying)."""
    text = (text or "").strip()
    if not text:
        return b""
    try:
        audio = _tts_azure(text, lang)
        if audio:
            _stats["tts_azure"] += 1
            return audio
    except Exception as exc:
        logger.warning(f"[stream] azure TTS failed: {exc}")
    try:
        audio = _tts_openai(text)
        if audio:
            _stats["tts_openai"] += 1
            return audio
    except Exception as exc:
        logger.warning(f"[stream] openai TTS failed: {exc}")
    return b""


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
        self.words = 0                      # transcribed words heard
        self.speech_ms = 0.0                # caller speech duration

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
            self.player.cancel()
            _stats["barge_ins"] += 1
            self.n_barge += 1
            try:
                await self.ws.send_text(json.dumps(
                    {"event": "clear", self.sid_key: self.stream_id}))
            except Exception:
                pass

    async def _close(self) -> None:
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
        if rms >= VAD_RMS:
            self.voiced += 1
            self.silent = 0
            if not self.in_speech and self.voiced >= _START_FRAMES:
                self.in_speech = True
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
        heard = await asyncio.to_thread(transcribe, pcm, self.lang)
        if not heard:
            await self.say(_RETRY.get(self.lang, _RETRY["en"]))
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
        self.mode = nxt
        await self.say(say, then_hangup=(nxt == "hangup"))
        return True

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
                if not greeted:
                    greeted = True
                    say, nxt = await _brain_greet(line, sid, frm)
                    # The keypad language menu, which the <Gather> path plays
                    # and this one silently skipped — the inbound webhook
                    # returns the <Stream> before it ever reaches the menu, so
                    # switching transports quietly turned a four-language line
                    # into an English-only one. Appended as pre-rendered audio
                    # rather than a second turn, so it costs no extra round trip.
                    menu = b"" if nxt == "hangup" else \
                        await asyncio.to_thread(menu_audio)
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
    key, region = _azure_key_region()
    return {"enabled": ENABLED, "ws_base": _public_ws_base() or None,
            "vad_rms": VAD_RMS, "silence_ms": SILENCE_MS,
            "tts_voice": TTS_VOICE,
            "stt": ("azure" if key else
                    "whisper" if os.getenv("OPENAI_API_KEY") else "none"),
            "azure_region": region if key else None,
            "stats": dict(_stats)}
