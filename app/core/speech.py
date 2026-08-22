"""Provider-pluggable speech layer — one seam for every STT/TTS vendor.

WHY THIS EXISTS. Speech is ~62% of the cost of a phone call (Azure STT +
Azure TTS against a ~$0.06 call), and it was reached through four functions
hardwired to two vendors. Every future price move meant a code change and a
deploy. This module makes the vendor a config value, the way LLM_PROVIDER /
LLM_ALT_PROVIDER already do for the brain.

    voice_stream ──► speech.stt(pcm, lang)  ──► ordered provider list
                 ──► speech.tts(text, lang) ──► ordered provider list

TWO MECHANISMS, DELIBERATELY SEPARATE. They look similar and are not:

  ORDER   (VOICE_STT_PROVIDER)  decides who SERVES. The list is a fallback
          chain: the next provider is tried only when the previous one
          raises or returns nothing.
  SHADOW  (VOICE_STT_SHADOW)    decides who additionally RUNS AND IS LOGGED,
          off the critical path, result discarded.

The distinction is the whole point of the shadow. A fallback list invokes its
second entry only when the first THROWS, and Azure Speech rarely throws — so
ordering `azure,deepgram` would send Deepgram a handful of utterances a month
and produce no evidence at all. Shadow mode runs both on EVERY utterance and
writes the pair, which is what a promotion decision actually needs. It costs
one extra STT call per utterance (~$0.0055/call on Deepgram) and zero added
latency, because the shadow is submitted to a thread pool and never awaited.

PER-LANGUAGE ORDER. `VOICE_STT_PROVIDER_ZH` overrides `VOICE_STT_PROVIDER`
for Mandarin alone. This is not a nicety: English passing a recogniser
comparison tells you nothing about Mandarin, and the honest end state may be
Deepgram for en/fr and Azure for zh forever. A per-language order makes that
a config outcome rather than a blocked migration.

NOTHING CHANGES BY DEFAULT. The default orders are `azure,whisper` and
`azure,openai` with shadow unset — byte-identical behaviour to the code this
replaced. Providers are added dormant and promoted on evidence.

CONFIG (env)
  VOICE_STT_PROVIDER      azure,whisper   ordered serving chain
  VOICE_TTS_PROVIDER      azure,openai    ordered serving chain
  VOICE_STT_PROVIDER_<L>  ''              per-language override (EN/FR/ZH/ES/DE)
  VOICE_TTS_PROVIDER_<L>  ''              per-language override
  VOICE_STT_SHADOW        ''              provider to dual-run and log
  VOICE_STT_SHADOW_RATE   1.0             fraction of utterances to shadow
  DEEPGRAM_API_KEY        ''              enables the deepgram provider
  DEEPGRAM_STT_MODEL      nova-3          Deepgram model id
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("speech")

# Same fail-soft import as voice_stream: audioop is stdlib through 3.12 and the
# audioop-lts package on 3.13+. Its absence must disable a provider, not crash
# the app at import.
try:
    import audioop
except ImportError:                     # pragma: no cover
    audioop = None

STT_TIMEOUT = float(os.getenv("VOICE_STT_TIMEOUT", "20"))
TTS_TIMEOUT = float(os.getenv("VOICE_TTS_TIMEOUT", "20"))

# ── One pooled HTTP client for every vendor ─────────────────────────────────
# MEASURED: a fresh connection per request costs ~313 ms of DNS + TCP + TLS
# before a single byte of audio moves — Azure STT p50 834 ms cold against
# 521 ms on a warm connection. A turn makes two of these calls (STT then TTS)
# and a third when a shadow is running, so per-request connections were
# burning most of a second per turn on handshakes alone.
#
# KEEPALIVE_EXPIRY IS THE LOAD-BEARING SETTING. httpx defaults it to 5 s,
# which is shorter than the gap between two turns of a phone conversation —
# the pool would drop the connection during the caller's own sentence and pay
# the handshake again on every single turn, making the pool decorative. 300 s
# outlives any call.
#
# httpx.Client is thread-safe, which matters because the shadow runs on a
# worker thread and shares this pool.
_HTTP: Optional[httpx.Client] = None


def _http() -> httpx.Client:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client(
            timeout=max(STT_TIMEOUT, TTS_TIMEOUT),
            limits=httpx.Limits(max_keepalive_connections=8,
                                max_connections=16,
                                keepalive_expiry=300.0),
        )
    return _HTTP

# ── Language table ──────────────────────────────────────────────────────────
# The canonical set of languages the voice channel speaks, and what each
# vendor needs to be told. Membership in this dict is what the transports use
# to decide whether a language is offered at all, so adding a row here is how
# a language is added — not by setting an env var alone.
#
# `locale` is the BCP-47 tag Azure wants for both recognition and SSML;
# `azure_voice` is its neural voice; `iso` is the bare ISO-639-1 code that
# Whisper and Deepgram take instead. They are NOT interchangeable: passing a
# locale where an ISO code is expected is silently accepted by some vendors
# and then ignored, which is the failure mode that once made a four-language
# line English-only.
LANG_TABLE: Dict[str, Dict[str, str]] = {
    "en": {"locale": "en-US",
           "azure_voice": os.getenv("VOICE_TTS_VOICE", "en-US-JennyNeural").strip()
                          or "en-US-JennyNeural",
           "iso": "en"},
    "fr": {"locale": "fr-CA",
           "azure_voice": os.getenv("VOICE_TTS_VOICE_FR", "fr-CA-SylvieNeural"),
           "iso": "fr"},
    "zh": {"locale": "zh-CN",
           "azure_voice": os.getenv("VOICE_TTS_VOICE_ZH", "zh-CN-XiaoxiaoNeural"),
           "iso": "zh"},
    "es": {"locale": "es-MX",
           "azure_voice": os.getenv("VOICE_TTS_VOICE_ES", "es-MX-DaliaNeural"),
           "iso": "es"},
    "de": {"locale": "de-DE",
           "azure_voice": os.getenv("VOICE_TTS_VOICE_DE", "de-DE-KatjaNeural"),
           "iso": "de"},
}


def azure_pair(lang: str) -> Tuple[str, str]:
    """(recognition locale, neural voice) — the pair that must move together."""
    row = LANG_TABLE.get(lang or "en", LANG_TABLE["en"])
    return (row["locale"], row["azure_voice"])


def iso(lang: str) -> str:
    return LANG_TABLE.get(lang or "en", LANG_TABLE["en"])["iso"]


# ── Stats ───────────────────────────────────────────────────────────────────
# Keyed by provider rather than hardcoded per vendor, so a new provider shows
# up in /voice-stream/status without touching the status endpoint. The cost
# model in the assessment should become observable here, not stay assumed.
_stats: Dict[str, int] = {}


def _bump(key: str, n: int = 1) -> None:
    _stats[key] = _stats.get(key, 0) + n


def stats() -> Dict[str, int]:
    return dict(_stats)


# ══════════════════════════════════════════════════════════════════════════
# STT providers.  Contract: (wav_bytes, lang) -> str | None
#   str   — a transcript, INCLUDING '' for "audio held no recognisable words"
#   None  — this provider is not configured; try the next one
# The '' / None distinction is load-bearing: '' is a real answer that ends the
# chain (the caller said nothing intelligible), None means "not my turn".
# ══════════════════════════════════════════════════════════════════════════

def _azure_key_region() -> Tuple[str, str]:
    from app.core.config import get_settings
    s = get_settings()
    return (getattr(s, "azure_speech_key", "") or "",
            getattr(s, "azure_speech_region", "") or "eastus")


def _stt_azure(wav: bytes, lang: str = "en") -> Optional[str]:
    key, region = _azure_key_region()
    if not key:
        return None
    r = _http().post(
        f"https://{region}.stt.speech.microsoft.com/speech/recognition/"
        "conversation/cognitiveservices/v1",
        params={"language": azure_pair(lang)[0], "format": "simple"},
        headers={"Ocp-Apim-Subscription-Key": key,
                 "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=8000"},
        content=wav, timeout=STT_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("RecognitionStatus") != "Success":
        return ""
    return (j.get("DisplayText") or "").strip()


def _stt_whisper(wav: bytes, lang: str = "en") -> Optional[str]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    r = _http().post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        data={"model": "whisper-1", "language": iso(lang)},
        files={"file": ("utterance.wav", wav, "audio/wav")},
        timeout=STT_TIMEOUT)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def _stt_deepgram(wav: bytes, lang: str = "en") -> Optional[str]:
    """Deepgram Nova-3, pre-recorded endpoint.

    PRE-RECORDED, NOT STREAMING, and that is a saving not a compromise: the
    VAD upstream has already cut the audio at an utterance boundary, so there
    is no partial-result benefit left to buy. The pre-recorded tier is
    ~$0.0043-0.0052/min against ~$0.0048-0.0058 streaming, and roughly a
    third of Azure either way.

    `smart_format` gives punctuation and number formatting, which matters
    because the transcript is read by the brain and stored as the customer's
    words in conversation_messages.
    """
    key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    if not key:
        return None
    r = _http().post(
        "https://api.deepgram.com/v1/listen",
        params={"model": os.getenv("DEEPGRAM_STT_MODEL", "nova-3").strip(),
                "language": iso(lang),
                "smart_format": "true"},
        headers={"Authorization": f"Token {key}",
                 "Content-Type": "audio/wav"},
        content=wav, timeout=STT_TIMEOUT)
    r.raise_for_status()
    chans = (r.json().get("results") or {}).get("channels") or []
    if not chans:
        return ""
    alts = chans[0].get("alternatives") or []
    return (alts[0].get("transcript") or "").strip() if alts else ""


STT_PROVIDERS: Dict[str, Callable[[bytes, str], Optional[str]]] = {
    "azure": _stt_azure,
    "whisper": _stt_whisper,
    "deepgram": _stt_deepgram,
}


# ══════════════════════════════════════════════════════════════════════════
# TTS providers.  Contract: (text, lang) -> bytes | None
# EVERY provider must return 8 kHz mono μ-law — the format the carrier's media
# stream plays. A provider that returns anything else converts BEFORE
# returning; a mismatch here is silent, because the carrier simply drops the
# frames and the caller hears nothing at all.
# ══════════════════════════════════════════════════════════════════════════

# ── Speaking style ──────────────────────────────────────────────────────────
# Azure neural voices carry STYLES — cheerful, excited, friendly,
# customerservice — and we were using none of them, so the line spoke in each
# voice's flat default. That is a tone change available for free on the voice
# already configured; it needs no new vendor, no new voice, and no re-testing
# of the language pairing.
#
# NOT EVERY VOICE HAS THEM, and the gap is uneven in a way that matters here:
# en JennyNeural and zh XiaoxiaoNeural have rich style lists, es DaliaNeural
# has `cheerful`, and **fr-CA has none at all** — not Sylvie, not any other
# fr-CA female voice. So style is per-language and simply absent for French
# rather than faked with a different accent, because a Parisian voice on a
# Canadian support line is a market decision, not a tone tweak.
#
# `styledegree` scales intensity (0.01–2). Above ~1.3 the delivery starts to
# sound performed rather than warm, which on a support call reads as insincere
# — the caller is often mildly annoyed already.
_TTS_STYLES = {
    "en": os.getenv("VOICE_TTS_STYLE_EN", "friendly").strip(),
    "fr": os.getenv("VOICE_TTS_STYLE_FR", "").strip(),      # unsupported
    "zh": os.getenv("VOICE_TTS_STYLE_ZH", "friendly").strip(),
    "es": os.getenv("VOICE_TTS_STYLE_ES", "cheerful").strip(),
    "de": os.getenv("VOICE_TTS_STYLE_DE", "").strip(),
}
STYLE_DEGREE = os.getenv("VOICE_TTS_STYLE_DEGREE", "1.0").strip()


def _style_for(lang: str) -> str:
    return _TTS_STYLES.get(lang or "en", "")


def _tts_azure(text: str, lang: str = "en") -> Optional[bytes]:
    key, region = _azure_key_region()
    if not key:
        return None
    locale, voice = azure_pair(lang)
    body = (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
    style = _style_for(lang)
    if style:
        # mstts is a SEPARATE namespace and must be declared on <speak>, or the
        # whole document is rejected — the style tag is not part of plain SSML.
        body = (f'<mstts:express-as style="{style}" '
                f'styledegree="{STYLE_DEGREE}">{body}</mstts:express-as>')
    ssml = ('<speak version="1.0" '
            'xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" '
            f'xml:lang="{locale}"><voice name="{voice}">'
            + body + "</voice></speak>")
    r = _http().post(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        headers={"Ocp-Apim-Subscription-Key": key,
                 "Content-Type": "application/ssml+xml",
                 # Azure emits carrier-ready μ-law natively. No alternative
                 # vendor does, so every other provider pays a resample that
                 # this one does not — worth remembering when comparing their
                 # per-character prices.
                 "X-Microsoft-OutputFormat": "raw-8khz-8bit-mono-mulaw",
                 "User-Agent": "conscestra-voice"},
        content=ssml.encode("utf-8"), timeout=TTS_TIMEOUT)
    if r.status_code >= 400 and style:
        # A voice without this style rejects the document. Losing the tone is
        # acceptable; losing the caller's answer is not — retry plain.
        logger.info("[speech] %s does not support style %r — speaking plain",
                    voice, style)
        plain = ('<speak version="1.0" '
                 'xmlns="http://www.w3.org/2001/10/synthesis" '
                 f'xml:lang="{locale}"><voice name="{voice}">'
                 + (text.replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;")) + "</voice></speak>")
        r = _http().post(
            f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={"Ocp-Apim-Subscription-Key": key,
                     "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": "raw-8khz-8bit-mono-mulaw",
                     "User-Agent": "conscestra-voice"},
            content=plain.encode("utf-8"), timeout=TTS_TIMEOUT)
    r.raise_for_status()
    return r.content or None


def _tts_openai(text: str, lang: str = "en") -> Optional[bytes]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    from app.core.voice_stream import pcm16_to_ulaw, resample_pcm16
    r = _http().post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "tts-1", "voice": "alloy", "input": text[:4000],
              "response_format": "pcm"},           # 24 kHz s16le mono
        timeout=TTS_TIMEOUT)
    r.raise_for_status()
    return pcm16_to_ulaw(resample_pcm16(r.content, 24000, 8000))


# ── Telnyx TTS ──────────────────────────────────────────────────────────────
# MEASURED 2026-08-21 against the live account, not read from documentation:
#
#   endpoint   POST /v2/text-to-speech/speech      (the bare /v2/text-to-speech
#                                                   path returns 404)
#   returns    audio/wav — RIFF, PCM, 16000 Hz, 1ch, 16-bit, ALWAYS
#   overrides  none. `voice_settings` {format|encoding|sample_rate} were all
#              accepted and SILENTLY IGNORED — every variant came back
#              byte-identical (54254 B). There is no way to ask for μ-law.
#
# So this provider owns the conversion: strip the WAV header, resample
# 16k → 8k, encode μ-law. Same path the OpenAI fallback already walks from
# 24 kHz, and the reason the seam's contract is "return μ-law or None".
#
# LANGUAGE COVERAGE IS THE CATCH, and it decides how much this is worth:
#   en-US   Bayan + KokoroTTS families  → the cheap tier exists
#   es-ES   KokoroTTS exists, but es-MX (our locale) is Ultra-only
#   fr-CA   Ultra ONLY (11 voices)
#   zh      Ultra ONLY (10 voices; note the bare `zh` code, not zh-CN)
# Telnyx list pricing puts Ultra at ~$32/1M characters against Azure Neural's
# $16/1M — so on fr and zh this vendor is TWICE the price of the incumbent.
# A language with no entry in the map below returns None and falls through to
# Azure, which is the correct outcome, not a gap to fill later.
#
# STILL UNVERIFIED: which family maps to which billing tier. The voices API
# does not expose price and the pricing page names tiers ("Telnyx TTS",
# "Telnyx HD Voices", "Telnyx Ultra"), not families. Confirm Bayan bills at
# the $3/1M tier by synthesising a known character count and reading the
# usage report BEFORE ordering this provider first anywhere.
#
# 2026-08-17: Telnyx DELETED the Telnyx.Natural, Telnyx.NaturalHD and Rime
# families outright — the live voices API now returns zero of each. Two things
# follow. First, this map is unaffected, because it names Bayan/KokoroTTS and
# never those; being cautious about an unverified tier is what kept a vendor's
# deprecation from becoming an outage here. Second, the vendor's own
# recommended replacement is Telnyx.Ultra at ~$32/1M — DOUBLE Azure Neural's
# $16/1M. A migration note that says "switch to X" is a pricing decision
# wearing a maintenance notice, and following it would have raised the bill on
# the very component this provider exists to make cheaper.
# 2026-08-22, after Telnyx deleted the Natural/NaturalHD/Rime families: the
# surviving cheap tier that covers ALL FOUR of our languages is Inworld Mini
# (~$5.50/1M characters against Azure Neural's $16/1M). Bayan is English-only
# and KokoroTTS has no Mandarin, which is why neither could serve this line on
# its own. Ultra covers everything and costs ~$32/1M — DOUBLE the incumbent,
# and it is the migration Telnyx's own deprecation notice recommends.
_TELNYX_VOICES = {
    "en": os.getenv("VOICE_TTS_TELNYX_EN", "Inworld.Mini.Ashley").strip(),
    "fr": os.getenv("VOICE_TTS_TELNYX_FR", "Inworld.Mini.Alain").strip(),
    "zh": os.getenv("VOICE_TTS_TELNYX_ZH", "Inworld.Mini.Mei").strip(),
    "es": os.getenv("VOICE_TTS_TELNYX_ES", "Inworld.Mini.Diego").strip(),
    "de": os.getenv("VOICE_TTS_TELNYX_DE", "").strip(),
}


def _wav_pcm16(wav: bytes) -> Tuple[bytes, int]:
    """(PCM16 samples, sample rate) from a RIFF/WAVE container.

    Walks the chunk list rather than assuming a 44-byte header: Telnyx returns
    a streaming-style RIFF whose size field is 0xFFFFFFFF, and extra chunks
    before `data` are legal. Raises on anything that is not 16-bit PCM, so a
    silent format change becomes a logged failure and a fallback rather than
    noise on a live call.
    """
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE payload")
    rate = fmt = bits = ch = 0
    i = 12
    while i + 8 <= len(wav):
        cid, sz = wav[i:i + 4], struct.unpack("<I", wav[i + 4:i + 8])[0]
        body = i + 8
        if cid == b"fmt ":
            fmt, ch, rate, _br, _ba, bits = struct.unpack(
                "<HHIIHH", wav[body:body + 16])
        elif cid == b"data":
            if fmt != 1 or bits != 16:
                raise ValueError(f"expected 16-bit PCM, got fmt={fmt} bits={bits}")
            end = len(wav) if sz in (0, 0xFFFFFFFF) else min(len(wav), body + sz)
            pcm = wav[body:end]
            if ch == 2:
                pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
            return pcm, rate
        i = body + sz + (sz % 2)
    raise ValueError("no data chunk")


def _tts_telnyx(text: str, lang: str = "en") -> Optional[bytes]:
    key = os.getenv("TELNYX_API_KEY", "").strip()
    voice = _TELNYX_VOICES.get(lang or "en", "")
    if not key or not voice:
        return None                      # no voice for this language → next
    # ASK FOR MU-LAW DIRECTLY. Left to itself this endpoint returns whatever
    # the underlying vendor prefers — WAV for Telnyx.Bayan, but MP3 for
    # Inworld and Ultra, which we cannot decode without pulling in ffmpeg. The
    # `voice_settings.encoding` parameter is undocumented on the request page;
    # its valid values came out of a 422 body: MP3, LINEAR16, OGG_OPUS, ALAW,
    # MULAW, FLAC, PCM, WAV. MULAW at 8000 Hz is exactly what the carrier
    # plays, so this path costs no conversion at all — the same native-format
    # advantage Azure has.
    #
    # `sample_rate` is REQUIRED alongside MULAW; without it the API answers
    # HTTP 400 "failed to produce text to speech", which reads like an outage
    # rather than a missing field.
    body = {"text": text[:4000], "voice": voice,
            "output_type": "binary_output",
            "voice_settings": {"encoding": "MULAW", "sample_rate": 8000}}
    r = _http().post(
        "https://api.telnyx.com/v2/text-to-speech/speech",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json=body, timeout=TTS_TIMEOUT)
    if r.status_code == 200 and (r.headers.get("content-type") or "").startswith(
            ("audio/basic", "audio/mulaw", "audio/x-mulaw")):
        return r.content or None
    # Fall back to WAV and convert, for any voice family that refuses MULAW.
    body["voice_settings"] = {"encoding": "WAV", "sample_rate": 8000}
    r = _http().post(
        "https://api.telnyx.com/v2/text-to-speech/speech",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json=body, timeout=TTS_TIMEOUT)
    r.raise_for_status()
    from app.core.voice_stream import pcm16_to_ulaw, resample_pcm16
    pcm, rate = _wav_pcm16(r.content)
    if rate != 8000:
        pcm = resample_pcm16(pcm, rate, 8000)
    return pcm16_to_ulaw(pcm)


TTS_PROVIDERS: Dict[str, Callable[[str, str], Optional[bytes]]] = {
    "azure": _tts_azure,
    "telnyx": _tts_telnyx,
    "openai": _tts_openai,
}


# ══════════════════════════════════════════════════════════════════════════
# Dispatch
# ══════════════════════════════════════════════════════════════════════════

_DEFAULT_STT_ORDER = "azure,whisper"
_DEFAULT_TTS_ORDER = "azure,openai"


def _order(kind: str, lang: str, registry: Dict[str, Callable],
           default: str) -> List[str]:
    """Provider names to try, most-preferred first.

    A per-language variable wins over the global one so Phase-3 promotion can
    happen one language at a time. Unknown names are dropped with a warning
    rather than raising: a typo in a Railway variable must not take the phone
    line down, it must fall through to the next provider.
    """
    var = f"VOICE_{kind}_PROVIDER"
    raw = (os.getenv(f"{var}_{(lang or 'en').upper()}", "").strip()
           or os.getenv(var, "").strip()
           or default)
    names, seen = [], set()
    for n in (p.strip().lower() for p in raw.split(",")):
        if not n or n in seen:
            continue
        seen.add(n)
        if n in registry:
            names.append(n)
        else:
            logger.warning("[speech] unknown %s provider %r — ignored", kind, n)
    return names or [p for p in default.split(",") if p in registry]


def stt(wav: bytes, lang: str = "en", tag: str = "") -> str:
    """Utterance WAV → transcript. Never raises; '' when nothing recognised.

    Returns '' rather than raising on total failure because the caller's next
    move is to re-prompt the caller, and a dead line is worse than a "sorry,
    could you say that again".
    """
    result, served_by = "", None
    for name in _order("STT", lang, STT_PROVIDERS, _DEFAULT_STT_ORDER):
        t0 = time.time()
        try:
            text = STT_PROVIDERS[name](wav, lang)
        except Exception as exc:
            _bump(f"stt_{name}_error")
            logger.warning("[speech] stt %s failed: %s", name, exc)
            continue
        if text is None:                    # not configured — not a failure
            continue
        _bump(f"stt_{name}")
        _bump(f"stt_{name}_ms", int((time.time() - t0) * 1000))
        result, served_by = text, name
        break
    if served_by is None:
        _bump("stt_failures")
    _maybe_shadow(wav, lang, result, served_by, tag)
    return result


def tts(text: str, lang: str = "en") -> bytes:
    """Reply text → 8 kHz μ-law. b'' on total failure, so the turn goes silent
    rather than the call dying."""
    text = (text or "").strip()
    if not text:
        return b""
    for name in _order("TTS", lang, TTS_PROVIDERS, _DEFAULT_TTS_ORDER):
        t0 = time.time()
        try:
            audio = TTS_PROVIDERS[name](text, lang)
        except Exception as exc:
            _bump(f"tts_{name}_error")
            logger.warning("[speech] tts %s failed: %s", name, exc)
            continue
        if audio:
            _bump(f"tts_{name}")
            _bump(f"tts_{name}_ms", int((time.time() - t0) * 1000))
            _bump("tts_chars", len(text))
            return audio
    _bump("tts_failures")
    return b""


# ══════════════════════════════════════════════════════════════════════════
# Shadow — dual-run, log, discard
# ══════════════════════════════════════════════════════════════════════════

# Small and bounded on purpose. If shadow work ever backs up behind live
# calls, the right outcome is to DROP shadow samples, never to queue them and
# grow memory on a long call. Two workers is enough for one utterance per
# caller per ~3 seconds across a realistic concurrent-call count.
_shadow_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stt-shadow")


def _shadow_enabled() -> Tuple[Optional[str], float]:
    name = os.getenv("VOICE_STT_SHADOW", "").strip().lower()
    if not name or name not in STT_PROVIDERS:
        return (None, 0.0)
    try:
        rate = float(os.getenv("VOICE_STT_SHADOW_RATE", "1.0"))
    except ValueError:
        rate = 1.0
    return (name, max(0.0, min(1.0, rate)))


def _maybe_shadow(wav: bytes, lang: str, served_text: str,
                  served_by: Optional[str], tag: str = "") -> None:
    """Submit the comparison run. Returns immediately — the caller is a live
    phone call and must not wait for this."""
    name, rate = _shadow_enabled()
    if not name or name == served_by or not served_by:
        return
    if rate < 1.0 and random.random() > rate:
        return
    try:
        _shadow_pool.submit(_run_shadow, wav, lang, served_text, served_by,
                            name, tag)
    except RuntimeError:                    # pool shutting down at exit
        pass


def _run_shadow(wav: bytes, lang: str, served_text: str,
                served_by: str, name: str, tag: str = "") -> None:
    t0 = time.time()
    try:
        text = STT_PROVIDERS[name](wav, lang)
    except Exception as exc:
        _bump(f"shadow_{name}_error")
        logger.warning("[speech] shadow %s failed: %s", name, exc)
        return
    if text is None:
        return
    ms = int((time.time() - t0) * 1000)
    _record_alternate(tag, text)
    _bump(f"shadow_{name}")
    if text.strip() == (served_text or "").strip():
        _bump("shadow_agree")
    else:
        _bump("shadow_differ")
    _persist_shadow(lang, served_by, served_text, name, text, ms, len(wav))


_SHADOW_TABLE_OK: Optional[bool] = None


def _persist_shadow(lang: str, served_by: str, served_text: str,
                    shadow_by: str, shadow_text: str, ms: int,
                    wav_bytes: int) -> None:
    """Write the pair for later scoring.

    Degrades to a structured log line when the table is absent, so shadow mode
    can be switched on before its migration is applied without losing the
    early samples entirely. Applying sql/voice_stt_shadow.sql upgrades it to
    queryable rows — the table is NOT in REQUIRED_MIGRATIONS because this
    feature must never make a deploy fail a readiness check.
    """
    global _SHADOW_TABLE_OK
    # A stable handle for the utterance that stores NO audio. The channel has
    # never retained voice recordings and this feature must not become the
    # reason it starts — the scorer only ever needs to group and de-duplicate
    # pairs, which a digest of the utterance's shape does just as well.
    audio_key = hashlib.sha256(
        f"{lang}|{wav_bytes}|{served_text}".encode("utf-8")).hexdigest()[:16]

    if _SHADOW_TABLE_OK is not False:
        conn = None
        try:
            from app.core.database import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO voice_stt_shadow
                     (lang, served_by, served_text, shadow_by, shadow_text,
                      shadow_ms, audio_ms, audio_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lang, served_by, served_text, shadow_by, shadow_text,
                 ms, int(wav_bytes / 16), audio_key))
            conn.commit()
            _SHADOW_TABLE_OK = True
            return
        except Exception as exc:
            if _SHADOW_TABLE_OK is None:
                logger.info("[speech] shadow table unavailable (%s) — "
                            "logging pairs instead; apply "
                            "sql/voice_stt_shadow.sql to store them", exc)
            _SHADOW_TABLE_OK = False
        finally:
            # get_connection() hands out a POOLED checkout holding a semaphore
            # slot; close() is what returns it. Omitting this drains the pool
            # one shadow write at a time and eventually blocks every caller —
            # including the live phone line this feature promises not to touch.
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    logger.info("[speech] shadow lang=%s %s=%r %s=%r ms=%d",
                lang, served_by, served_text, shadow_by, shadow_text, ms)


def prewarm(lang: str = "en") -> None:
    """Open the vendor connections before the caller needs them.

    Pooling only helps from the SECOND request onward — the first still pays
    DNS + TCP + TLS. On a phone call that first request is the greeting, which
    is the worst possible place to spend 300 ms. This runs at WS connect, on a
    worker thread, so the handshake overlaps the carrier's own stream setup
    instead of landing in front of the greeting.

    Cheapest possible warmers: a token mint for Azure (no audio, no STT
    charge) and Deepgram's own free `/v1/projects`. Deliberately NOT a real
    synthesis or transcription — a prewarm that bills per call is a prewarm
    nobody leaves switched on. Failures are ignored; this is an optimisation,
    and a cold connection still works.
    """
    def _warm() -> None:
        key, region = _azure_key_region()
        if key:
            try:
                _http().post(
                    f"https://{region}.api.cognitive.microsoft.com"
                    "/sts/v1.0/issueToken",
                    headers={"Ocp-Apim-Subscription-Key": key,
                             "Content-Length": "0"}, timeout=5.0)
            except Exception:
                pass
            # STT and TTS are different hosts, so the token mint above warms
            # neither of them — each host needs its own connection in the pool.
            for host in (f"https://{region}.stt.speech.microsoft.com",
                         f"https://{region}.tts.speech.microsoft.com"):
                try:
                    _http().get(host, timeout=5.0)
                except Exception:
                    pass
        if os.getenv("DEEPGRAM_API_KEY", "").strip():
            try:
                _http().get("https://api.deepgram.com/v1/projects",
                            headers={"Authorization":
                                     f"Token {os.getenv('DEEPGRAM_API_KEY').strip()}"},
                            timeout=5.0)
            except Exception:
                pass

    try:
        _shadow_pool.submit(_warm)
    except RuntimeError:
        pass


# ── Alternate transcripts, from the shadow we already pay for ───────────────
# A proper noun spoken in another language is where recognisers diverge most.
# MEASURED on a live Mandarin call: the caller said the surname "Graham" to a
# zh-CN recogniser. Azure returned "Greyhound" (soundex G653, no match);
# Deepgram returned "greyham" (G650, MATCHES). The verification failed on a
# transcription error, not on anything the caller did.
#
# The shadow already transcribes every utterance a second time, so the better
# reading exists — it was simply being logged and thrown away. This keeps the
# last few per call so a caller-supplied proper noun can be checked against
# BOTH readings.
#
# THIS IS NOT A LOOSER MATCH. The comparison downstream is unchanged; it just
# gets a second, independent transcript of the SAME audio. An attacker still
# has to say something that sounds like the real name — two transcripts of a
# wrong name are still two wrong names.
_ALT_MAX = 64
_alternates: Dict[str, List[str]] = {}


def _record_alternate(tag: str, text: str) -> None:
    if not tag or not text:
        return
    if len(_alternates) > _ALT_MAX:          # bounded: calls end, tags do not
        for k in list(_alternates)[:_ALT_MAX // 2]:
            _alternates.pop(k, None)
    lst = _alternates.setdefault(tag, [])
    if text not in lst:
        lst.append(text)
    del lst[:-4]


def alternates(tag: str, wait_ms: int = 0) -> List[str]:
    """Other recognisers' readings of this call's recent utterances.

    `wait_ms` briefly gives the shadow time to land — it runs off the critical
    path, so on a fast turn the caller's words can reach the brain before the
    second transcript does. Only worth spending where a proper noun is being
    checked; everywhere else, take what is already there.
    """
    deadline = time.time() + wait_ms / 1000.0
    while True:
        got = list(_alternates.get(tag) or [])
        if got or time.time() >= deadline:
            return got
        time.sleep(0.05)


def status() -> Dict[str, object]:
    """Which providers are configured and which would serve — for the admin
    status endpoint. Reports the ORDER, not just the winner, because 'azure is
    serving' and 'azure is first in a list of three' are different facts."""
    shadow, rate = _shadow_enabled()
    return {
        "stt_order": {l: _order("STT", l, STT_PROVIDERS, _DEFAULT_STT_ORDER)
                      for l in LANG_TABLE},
        "tts_order": {l: _order("TTS", l, TTS_PROVIDERS, _DEFAULT_TTS_ORDER)
                      for l in LANG_TABLE},
        "shadow": shadow,
        "shadow_rate": rate if shadow else 0.0,
        "configured": {
            "azure": bool(_azure_key_region()[0]),
            "whisper": bool(os.getenv("OPENAI_API_KEY", "").strip()),
            "openai": bool(os.getenv("OPENAI_API_KEY", "").strip()),
            "deepgram": bool(os.getenv("DEEPGRAM_API_KEY", "").strip()),
        },
    }
