"""Real-time voice — carrier media streams with streaming STT/TTS.

The turn-based <Gather> loop works but feels like an IVR: every exchange
pays a carrier round-trip plus a fixed end-of-speech timeout. This module
upgrades the TRANSPORT only — the brains (voice_support's tier ladder,
sdr's state machine), the signature-verified webhooks, the OTP flow and the
customer-scope security model are all unchanged:

    carrier ──<Connect><Stream>──►  WS /voice/stream/{line}?sid&frm&t
        8 kHz μ-law frames in  ──►  VAD (energy, ~650 ms end-of-speech)
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
  VOICE_STREAM_SILENCE_MS   650   end-of-utterance silence
  VOICE_TTS_VOICE           en-US-JennyNeural   Azure neural voice
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
SILENCE_MS = int(os.getenv("VOICE_STREAM_SILENCE_MS", "650"))
TTS_VOICE = os.getenv("VOICE_TTS_VOICE", "en-US-JennyNeural").strip()

_FRAME_MS = 20                # carrier frames are 20 ms of 8 kHz μ-law
_MAX_UTTER_MS = 25_000        # hard cap per utterance
_MIN_UTTER_MS = 240           # shorter than this = noise blip, not speech
_START_FRAMES = 2             # voiced frames before "speech started"
_CHUNK_BYTES = 3200           # 400 ms per outbound media message
_STT_TIMEOUT = 20.0
_TTS_TIMEOUT = 20.0

_LINES = ("support", "sdr")

_stats = {"calls": 0, "utterances": 0, "stt_azure": 0, "stt_whisper": 0,
          "stt_failures": 0, "tts_azure": 0, "tts_openai": 0, "barge_ins": 0}


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
    url = (f"{base}/voice/stream/{line}?sid={quote(call_sid)}"
           f"&frm={quote(from_number)}"
           f"&t={stream_token(line, call_sid, from_number)}")
    return f'<Connect><Stream url="{url}"/></Connect>'


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


def _stt_azure(wav: bytes) -> Optional[str]:
    key, region = _azure_key_region()
    if not key:
        return None
    r = httpx.post(
        f"https://{region}.stt.speech.microsoft.com/speech/recognition/"
        "conversation/cognitiveservices/v1",
        params={"language": "en-US", "format": "simple"},
        headers={"Ocp-Apim-Subscription-Key": key,
                 "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=8000"},
        content=wav, timeout=_STT_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("RecognitionStatus") != "Success":
        return ""
    return (j.get("DisplayText") or "").strip()


def _stt_whisper(wav: bytes) -> Optional[str]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    r = httpx.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        data={"model": "whisper-1", "language": "en"},
        files={"file": ("utterance.wav", wav, "audio/wav")},
        timeout=_STT_TIMEOUT)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def transcribe(pcm16_8k: bytes) -> str:
    """Utterance PCM → text ('' when nothing recognizable). Azure first,
    Whisper fallback — mirrors the ddgs→Tavily pattern: never raises."""
    wav = wav_from_pcm16(pcm16_8k)
    try:
        text = _stt_azure(wav)
        if text is not None:
            _stats["stt_azure"] += 1
            return text
    except Exception as exc:
        logger.warning(f"[stream] azure STT failed: {exc}")
    try:
        text = _stt_whisper(wav)
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

def _tts_azure(text: str) -> Optional[bytes]:
    key, region = _azure_key_region()
    if not key:
        return None
    ssml = (f'<speak version="1.0" xml:lang="en-US">'
            f'<voice name="{TTS_VOICE}">'
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


def synthesize(text: str) -> bytes:
    """Reply text → 8 kHz μ-law (b'' on total failure — the call then just
    stays silent for that turn rather than dying)."""
    text = (text or "").strip()
    if not text:
        return b""
    try:
        audio = _tts_azure(text)
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


async def _brain_turn(line: str, call_sid: str, heard: str) -> Tuple[str, str]:
    if line == "support":
        from app.core import voice_support as vs
        return await vs.take_turn(call_sid, heard)
    from app.core import sdr
    res = await asyncio.to_thread(sdr.converse, f"voice-{call_sid}", heard,
                                  "voice")
    return res["reply"], ("hangup" if res.get("done") else "speech")


async def _brain_digits(line: str, call_sid: str, digits: str) -> Tuple[str, str]:
    if line == "support":
        from app.core import voice_support as vs
        return await vs.take_digits(call_sid, digits)
    return "", "speech"        # the SDR line has no keypad flow


def _brain_hangup(line: str, call_sid: str) -> None:
    """Caller hung up mid-conversation — close the support transcript."""
    if line != "support":
        return
    try:
        from app.core import voice_support as vs
        sess = vs._CALLS.get(call_sid)
        if sess and sess.get("transcript") and not sess.get("_closed"):
            sess["_closed"] = True
            vs._close_call(sess, "caller hung up")
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

    # ── outbound audio ──────────────────────────────────────────────────────
    async def say(self, text: str, then_hangup: bool = False) -> None:
        audio = await asyncio.to_thread(synthesize, text)
        if not audio:
            logger.warning(f"[stream] no TTS audio for {self.call_sid[:12]}")
            if then_hangup:
                await self._close()
            return
        self.player = asyncio.create_task(self._play(audio, then_hangup))

    async def _play(self, ulaw: bytes, then_hangup: bool) -> None:
        try:
            for i in range(0, len(ulaw), _CHUNK_BYTES):
                await self.ws.send_text(json.dumps({
                    "event": "media", self.sid_key: self.stream_id,
                    "media": {"payload":
                              base64.b64encode(ulaw[i:i + _CHUNK_BYTES]).decode()}}))
                # slightly faster than real time; the pause keeps barge-in
                # cancellation responsive without starving the carrier buffer
                await asyncio.sleep(0.2)
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
            return                              # one utterance at a time
        _stats["utterances"] += 1
        self.worker = asyncio.create_task(self._respond(pcm))

    async def _respond(self, pcm: bytes) -> None:
        t0 = time.time()
        heard = await asyncio.to_thread(transcribe, pcm)
        if not heard:
            await self.say("Sorry, I didn't catch that. Could you say it again?")
            return
        logger.info(f"[stream] {self.line} {self.call_sid[:12]} heard "
                    f"({time.time() - t0:.1f}s): {heard[:80]!r}")
        say, nxt = await _brain_turn(self.line, self.call_sid, heard)
        self.mode = nxt
        await self.say(say, then_hangup=(nxt == "hangup"))

    async def on_dtmf(self, digit: str) -> None:
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
                    await call.say(say, then_hangup=(nxt == "hangup"))
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
