"""U6 (MCP client) + Voice: regression tests.

No network, no phone calls, no external MCP server — the transport is stubbed
so the SAFETY properties are what get pinned down:

  MCP  — three independent gates (allowlist / tool enablement / egress), and
         disabling something actually REVOKES it rather than leaving a stale
         grantable row behind.
  Voice — recognition language and TTS voice always switch TOGETHER, detection
         is sticky per call, and a human takeover actually stands the AI down.

    python -m pytest tests/test_voice_and_mcp.py -v
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DB_DSN", "postgresql://postgres:aria@localhost:5434/crmdb")

from app.core import mcp_client as M        # noqa: E402
from app.core import voice_support as V     # noqa: E402


# ================================================================== MCP ====

def test_01_capability_id_roundtrip():
    cap = M.capability_id("linear", "create_issue")
    assert cap == "mcp:linear.create_issue"
    assert M.split_capability(cap) == ("linear", "create_issue")


def test_02_non_mcp_capability_is_not_claimed():
    """A native capability must never be mistaken for an MCP one."""
    assert M.split_capability("accounts.query") is None
    assert M.split_capability("mcp:no-dot-here") is None


def test_03_unregistered_server_is_refused():
    """GATE 1 — an allowlist, not discovery. Knowing a URL is not permission."""
    r = M.call_tool("never-registered", "anything", {})
    assert r["outcome"] == "refused"
    assert "not registered" in r["reason"]


def test_04_egress_gate_refuses_internal_tier(monkeypatch):
    """GATE 3 — calling a third party is egress, so U5's data-class rule
    applies: internal-tier content must not leave."""
    monkeypatch.setattr(M, "_server", lambda n: {
        "name": n, "url": "https://x/mcp", "enabled": True,
        "allow_internal_data": False, "max_scope": "internal",
        "timeout_secs": 5, "auth_env_var": None, "transport": "streamable_http"})
    ok, why = M.may_call("x", "INTERNAL_SENSITIVE")
    assert ok is False and "internal-tier" in why
    ok2, _ = M.may_call("x", "BUSINESS_INTERNAL")
    assert ok2 is True


def test_05_disabled_server_refused_even_when_registered(monkeypatch):
    monkeypatch.setattr(M, "_server", lambda n: {
        "name": n, "url": "https://x/mcp", "enabled": False,
        "allow_internal_data": True, "max_scope": "internal",
        "timeout_secs": 5, "auth_env_var": None, "transport": "streamable_http"})
    ok, why = M.may_call("x", "BUSINESS_INTERNAL")
    assert ok is False and "not enabled" in why


def test_06_internal_data_allowed_only_when_opted_in(monkeypatch):
    monkeypatch.setattr(M, "_server", lambda n: {
        "name": n, "url": "https://x/mcp", "enabled": True,
        "allow_internal_data": True, "max_scope": "internal",
        "timeout_secs": 5, "auth_env_var": None, "transport": "streamable_http"})
    ok, _ = M.may_call("x", "INTERNAL_SENSITIVE")
    assert ok is True, "explicit opt-in must be honoured"


def test_07_credentials_are_never_returned(monkeypatch):
    """A server listing exposes WHETHER auth is configured, never the secret."""
    monkeypatch.setenv("SOME_MCP_TOKEN", "super-secret-value")
    out = M.list_servers()
    if out.get("ok"):
        blob = str(out["servers"])
        assert "super-secret-value" not in blob
        assert "auth_env_var" not in blob, "env var NAME should not leak either"


# ================================================================ VOICE ====

@pytest.mark.parametrize("lang,recog,voice", [
    ("en", "en-US", "alice"),
    ("fr", "fr-CA", "Polly.Chantal"),
    ("es", "es-US", "Polly.Penelope"),
    ("de", "de-DE", "Polly.Marlene"),
])
def test_08_stt_and_tts_switch_together(lang, recog, voice):
    """The rule that makes voice i18n work at all: switching the TTS voice
    without the recognition language means English STT on French speech, which
    produces garbage the agent then answers confidently. Both, or neither."""
    say = V._say("test", lang)
    gather = V._gather_speech(say, lang)
    assert f'voice="{voice}"' in say
    assert f'language="{recog}"' in gather


def test_09_language_detection_is_sticky_per_call():
    """One ambiguous utterance must not flip the voice mid-call — being wrong
    once is far less jarring than switching accents halfway through."""
    sess = {}
    assert V._note_lang(sess, "Bonjour, je voudrais parler de ma facture") == "fr"
    assert V._note_lang(sess, "ok") == "fr"
    assert V._note_lang(sess, "yes please") == "fr"


def test_10_unknown_language_falls_back_to_english():
    sess = {}
    lang = V._note_lang(sess, "こんにちは")      # unsupported
    assert lang in V._VOICE_BY_LANG
    assert lang == "en"


def test_11_multilingual_kill_switch(monkeypatch):
    monkeypatch.setattr(V, "VOICE_MULTILINGUAL", False)
    assert V._lang_of({"lang": "fr"}) == "en"
    assert V._note_lang({}, "Bonjour tout le monde") == "en"


def test_12_next_twiml_carries_the_language_end_to_end():
    body = V._next_twiml("Votre solde est de 250 dollars.", "speech", "fr").body.decode()
    assert 'language="fr-CA"' in body
    assert "Polly.Chantal" in body
    assert "solde" in body


def test_13_hangup_and_digits_paths_are_localized():
    hang = V._next_twiml("Au revoir.", "hangup", "fr").body.decode()
    assert "Polly.Chantal" in hang and "<Hangup/>" in hang
    digits = V._next_twiml("Entrez votre code.", "digits", "fr").body.decode()
    assert "Polly.Chantal" in digits
    assert 'input="dtmf"' in digits, "keypad entry must survive localization"


def test_14_hold_music_does_not_hand_the_turn_back_immediately():
    """Standing down for a human must actually pause. A bare <Redirect> would
    return the turn to the AI on the next callback, which is the behaviour
    takeover exists to stop."""
    hold = V._hold_music()
    assert "<Pause" in hold
    assert "Redirect" in hold


def test_15_english_remains_the_default_shape():
    """Regression: the pre-existing English path must be byte-compatible with
    what the carrier already accepts."""
    body = V._next_twiml("Thanks for calling.", "speech", "en").body.decode()
    assert 'voice="alice"' in body
    assert 'language="en-US"' in body


# ====================================================== MANDARIN (zh) ======
# The multilingual feature (#2) was Latin-script only: a stopword+diacritic
# scorer is structurally blind to a language that doesn't space its words, so
# Chinese scored zero on every set and fell through to English. A Chinese
# customer silently got English replies on EVERY channel, text included.

from app.core import language as L      # noqa: E402


@pytest.mark.parametrize("text,expected,label", [
    ("你好，我想查询我的订单状态", "zh", "Simplified"),
    ("請問你們送貨到安大略省嗎？", "zh", "Traditional"),
    ("我的发票有问题", "zh", "short Simplified"),
    ("Bonjour, je voudrais parler de ma facture", "fr", "French unaffected"),
    ("Hello, I have a question about my order", "en", "English unaffected"),
])
def test_16_chinese_is_detected(text, expected, label):
    assert L.detect(text) == expected, label


def test_17_single_han_character_does_not_flip_the_language():
    """A pasted product name or brand must not switch the whole conversation —
    the same conservatism the Latin scorer already applies."""
    assert L.detect("I ordered the 中 model last week") == "en"


def test_18_japanese_and_korean_are_not_mistaken_for_chinese():
    """Japanese uses Han characters too, so 'has Han' is NOT 'is Chinese'.
    Kana and Hangul are the disambiguators, and both are checked before the Han
    threshold because Korean often contains no Han at all."""
    assert L._script_language("こんにちは、注文について") == "ja"
    assert L._script_language("안녕하세요 주문 확인해주세요") == "ko"
    assert L._script_language("订单状态查询") == "zh"


def test_19_unsupported_script_falls_back_to_english():
    """Detecting a script we cannot SERVE must not promise support we lack."""
    assert L.detect("こんにちは、注文について") == "en"
    assert L.detect("안녕하세요 주문 확인해주세요") == "en"


def test_20_chinese_reply_directive_handles_character_sets():
    d = L.directive("zh")
    assert "Mandarin Chinese" in d
    # Simplified vs Traditional is not a style choice — answering a Traditional
    # writer in Simplified reads as wrong.
    assert "繁體" in d and "简体" in d


def test_21_mandarin_voice_pairs_stt_and_tts():
    say = V._say("您的余额是250美元。", "zh")
    gather = V._gather_speech(say, "zh")
    assert 'voice="Polly.Zhiyu"' in say      # Telnyx accepts Polly.VoiceId
    assert 'language="zh-CN"' in gather
    body = V._next_twiml("您的余额是250美元。", "speech", "zh").body.decode()
    assert "余额" in body, "Chinese must survive XML escaping"


def test_22_recognition_codes_are_env_overridable(monkeypatch):
    """Telnyx does not publish its <Gather language> value list, so an
    unverified code must be fixable by CONFIG, not by a deploy — the same
    lesson as the missing Ollama model and the listed-but-404 Gemini models."""
    import importlib
    monkeypatch.setenv("VOICE_STT_ZH", "cmn-CN")
    importlib.reload(V)
    try:
        assert V._VOICE_BY_LANG["zh"][0] == "cmn-CN"
        assert V._VOICE_BY_LANG["zh"][1] == "Polly.Zhiyu", "TTS unaffected"
    finally:
        monkeypatch.delenv("VOICE_STT_ZH", raising=False)
        importlib.reload(V)


def test_23_mandarin_is_sticky_per_call():
    sess = {}
    assert V._note_lang(sess, "你好，我想查询我的订单") == "zh"
    assert V._note_lang(sess, "ok") == "zh"


# ============================ HALF-SWITCH GUARD (env override safety) ======
# The env-override hooks made it possible to change the TTS voice WITHOUT the
# recognition language (or vice versa) — reintroducing the exact failure voice
# i18n exists to prevent: English STT on Mandarin audio yields garbage the
# agent then answers confidently. A mismatched pair is refused.

def _reload_voice(monkeypatch, **env):
    import importlib
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(V)
    return V._VOICE_BY_LANG["zh"]


def test_24_tts_only_override_is_refused(monkeypatch):
    pair = _reload_voice(monkeypatch, VOICE_TTS_ZH="Polly.Chantal")   # French
    assert pair == ("zh-CN", "Polly.Zhiyu"), "safe default pair must be restored"


def test_25_stt_only_override_is_refused(monkeypatch):
    pair = _reload_voice(monkeypatch, VOICE_STT_ZH="en-US")
    assert pair == ("zh-CN", "Polly.Zhiyu")


def test_26_same_language_variants_are_accepted(monkeypatch):
    """The guard must not block the overrides it exists to enable. `cmn-CN` is
    the ISO 639-3 code for Mandarin — the one Amazon Polly uses for Zhiyu — and
    `zh-TW` is Mandarin as spoken in Taiwan."""
    assert _reload_voice(monkeypatch, VOICE_STT_ZH="cmn-CN")[0] == "cmn-CN"
    monkeypatch.delenv("VOICE_STT_ZH", raising=False)
    assert _reload_voice(monkeypatch, VOICE_STT_ZH="zh-TW")[0] == "zh-TW"


def test_27_cantonese_is_not_accepted_as_mandarin(monkeypatch):
    """yue (Cantonese) is a DIFFERENT language, not a dialect toggle. Accepting
    it would silently answer Mandarin callers in Cantonese."""
    assert _reload_voice(monkeypatch, VOICE_STT_ZH="yue-HK") == ("zh-CN", "Polly.Zhiyu")


def test_28_defaults_restored_when_overrides_cleared(monkeypatch):
    import importlib
    for k in ("VOICE_STT_ZH", "VOICE_TTS_ZH"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(V)
    assert V._VOICE_BY_LANG["zh"] == ("zh-CN", "Polly.Zhiyu")


# ================================ SDR VOICE LINE (the one callers reach) ===
# The multilingual work originally landed only in voice_support.py, but the
# configured Telnyx webhook points at /sdr/voice/inbound — a different module
# with its own TwiML builders that were still hard-coded to en-US/alice.

from app.core import sdr as SDR      # noqa: E402


@pytest.mark.parametrize("lang,stt,tts", [
    ("en", "en-US", "alice"),
    ("fr", "fr-CA", "Polly.Chantal"),
    ("es", "es-US", "Polly.Penelope"),
    ("zh", "zh-CN", "Polly.Zhiyu"),
])
def test_29_sdr_line_pairs_stt_and_tts(lang, stt, tts):
    say = SDR._say("test", lang)
    gather = SDR._gather(say, lang)
    assert f'voice="{tts}"' in say
    assert f'language="{stt}"' in gather


def test_30_sdr_shares_one_language_table_with_support():
    """Both lines must resolve to the SAME pair, including the guard that
    refuses a half-switched override — two tables would drift."""
    assert SDR._voice_pair("zh") == V._VOICE_BY_LANG["zh"]
    assert SDR._voice_pair("fr") == V._VOICE_BY_LANG["fr"]


def test_31_sdr_language_is_sticky_and_per_call():
    SDR._VOICE_LANG.clear()
    assert SDR._call_lang("CALL_A", "你好，我想了解你们的产品") == "zh"
    assert SDR._call_lang("CALL_A", "ok") == "zh"
    # A different call must not inherit it.
    assert SDR._call_lang("CALL_B", "Bonjour, je cherche un devis") == "fr"
    SDR._VOICE_LANG.clear()


def test_32_sdr_greeting_stays_english_before_the_caller_speaks():
    """Language cannot be detected before there is speech, so the greeting is
    English — and must keep its original TwiML shape."""
    SDR._VOICE_LANG.clear()
    assert SDR._call_lang("CALL_C") == "en"
    g = SDR._gather(SDR._say("Thanks for calling.", "en"), "en")
    assert 'voice="alice"' in g and 'language="en-US"' in g


# ===================== VOICE LANGUAGE SELECTION (the chicken-and-egg fix) ===
# Text detection cannot work on a phone call: <Gather> commits to ONE
# recognition language, so Mandarin spoken into an en-US recogniser returns
# English-ish text. Running a detector over THAT can never yield `zh` — you
# cannot detect a language from a transcript produced by the wrong recogniser.
# Confirmed on a real call: "my name is Alex I want to speak Chinese" came back
# as English. The caller therefore DECLARES the language by keypad, which no
# recogniser can garble.

def test_33_greeting_offers_the_language_menu():
    g = SDR._gather(SDR._say("Hi!") + SDR.lang_menu_twiml())
    assert "中文服务，请按 3" in g, "the Chinese option must be spoken in Chinese"
    assert "Pour le français, appuyez sur 2" in g
    assert 'input="speech dtmf"' in g and 'numDigits="1"' in g


def test_34_english_caller_path_is_unchanged():
    """An English caller simply talks — no menu interaction required."""
    g = SDR._gather(SDR._say("How can I help?", "en"), "en")
    assert 'voice="alice"' in g and 'language="en-US"' in g


def test_35_english_transcript_does_not_pin_the_call():
    """An `en` result from an en-US recogniser is not evidence of anything — it
    is the only thing that recogniser can produce. Pinning on it would lock a
    Mandarin caller into English before they ever reached the menu."""
    SDR._VOICE_LANG.clear()
    garbled = "my name is Alex I want to speak Chinese"   # real en-US STT output
    assert SDR._call_lang("CALL_X", garbled) == "en"
    assert "CALL_X" not in SDR._VOICE_LANG, "must not be pinned"


def test_36_keypad_selection_switches_both_halves():
    SDR._VOICE_LANG.clear()
    assert SDR.set_call_lang("CALL_Y", "zh") == "zh"
    body = SDR._twiml(SDR._gather(SDR._say("好的", "zh"), "zh")).body.decode()
    assert "Polly.Zhiyu" in body
    assert 'language="zh-CN"' in body
    SDR._VOICE_LANG.clear()


def test_37_menu_digits_map_to_supported_languages():
    for digit, code in SDR._LANG_MENU.items():
        assert digit.isdigit()
        assert code in V._VOICE_BY_LANG, f"digit {digit} maps to unsupported {code}"


def test_38_non_english_detection_is_still_honoured_when_it_appears():
    """If a recogniser DOES return Han characters, take the hint — the menu is
    the reliable path, not the only one."""
    SDR._VOICE_LANG.clear()
    assert SDR._call_lang("CALL_Z", "你好，我想了解你们的产品和价格") == "zh"
    SDR._VOICE_LANG.clear()


# ===================== MENU ORDER (spoken order must match routing) ========
# A caller presses what they HEARD. If the spoken menu and the digit routing
# ever drift apart, the caller gets a language they did not ask for — and the
# one most affected is the one who cannot read the code to check.

def test_39_menu_order_is_en_fr_zh_es():
    assert SDR._LANG_MENU == {"1": "en", "2": "fr", "3": "zh", "4": "es"}
    assert SDR._LANG_MENU_ORDER == ["1", "2", "3", "4"]


def test_40_spoken_menu_matches_routing_exactly():
    """The guard that makes drift impossible: every spoken option must route to
    the language it names."""
    for digit, (code, _text) in SDR._LANG_MENU_TEXT.items():
        assert SDR._LANG_MENU[digit] == code, (
            f"option {digit} is spoken as {code} but routes to "
            f"{SDR._LANG_MENU[digit]}")


def test_41_each_option_is_spoken_in_its_own_voice():
    """A single English <Say> containing '中文服务，请按 3' is read by an English
    TTS engine and is unintelligible — the caller who most needs that option is
    the one who cannot understand it. One <Say> per option, own voice."""
    import re
    menu = SDR.lang_menu_twiml()
    pairs = re.findall(r'<Say voice="([^"]+)">([^<]*)</Say>', menu)
    assert len(pairs) == 4
    voices = [v for v, _ in pairs]
    assert voices == ["alice", "Polly.Chantal", "Polly.Zhiyu", "Polly.Penelope"]
    # the Chinese option is voiced by the Mandarin speaker, not by alice
    zh = [t for v, t in pairs if v == "Polly.Zhiyu"][0]
    assert "请按 3" in zh


def test_42_menu_digits_all_resolve_to_a_usable_pair():
    for digit, code in SDR._LANG_MENU.items():
        stt, tts = SDR._voice_pair(code)
        assert stt and tts
        assert SDR._voice_pair(code) == V._VOICE_BY_LANG[code]


def test_43_menu_is_omitted_when_multilingual_is_off(monkeypatch):
    monkeypatch.setattr(V, "VOICE_MULTILINGUAL", False)
    assert SDR._call_lang("MONO_1", "你好我想了解产品") == "en"
