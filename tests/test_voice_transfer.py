"""Live transfer to a human — hours window, detection, and the TeXML produced.

The rules under test, in order of how badly they fail if wrong:

  1. A caller who asks for a person during business hours reaches a <Dial>.
  2. A caller who asks outside them is TOLD WHEN WE OPEN and leaves behind a
     tracked obligation — never a silent hangup.
  3. A caller asking ABOUT our AI ("人工智能") is not transferred. 人工 alone
     means "human operator", so the naive substring match sends every Chinese
     AI question to a person's cell phone.
  4. The window is wall-clock in a named zone, so it follows EDT/EST itself.
"""
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("VOICE_TRANSFER_NUMBER", "+14168896638")

from app.core import escalation, voice_support as V   # noqa: E402

ET = ZoneInfo("America/Toronto")


@pytest.fixture(autouse=True)
def _pinned_config(monkeypatch):
    """Pin the schedule these tests assert against.

    Without this the suite reads whatever the operator currently has in .env —
    so legitimately widening the window to 1-7 for a live test would 'fail' the
    weekend cases. Tests must assert the LOGIC, not the deployment."""
    monkeypatch.setattr(V, "TRANSFER_DAYS", "1-5")
    monkeypatch.setattr(V, "TRANSFER_ENABLED", True)
    monkeypatch.setenv("VOICE_TRANSFER_START", "08:30")
    monkeypatch.setenv("VOICE_TRANSFER_END", "17:30")
    monkeypatch.setenv("VOICE_TRANSFER_NUMBER", "+14168896638")
    monkeypatch.setattr(V, "TRANSFER_TZ", "America/Toronto")


def _win(when):
    return V.transfer_window(when)


# ── the window ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("when,is_open,why", [
    (datetime(2026, 7, 24, 8, 29, tzinfo=ET), False, "one minute before open"),
    (datetime(2026, 7, 24, 8, 30, tzinfo=ET), True,  "exactly at open"),
    (datetime(2026, 7, 24, 17, 29, tzinfo=ET), True, "last minute of the day"),
    (datetime(2026, 7, 24, 17, 30, tzinfo=ET), False, "exactly at close"),
    (datetime(2026, 7, 25, 12, 0, tzinfo=ET), False, "Saturday"),
    (datetime(2026, 7, 26, 12, 0, tzinfo=ET), False, "Sunday"),
    (datetime(2026, 7, 22, 12, 0, tzinfo=ET), True,  "Wednesday midday"),
])
def test_01_window_boundaries(when, is_open, why):
    assert _win(when)["open"] is is_open, why


def test_02_window_follows_dst():
    """Same wall-clock hour, opposite sides of the DST switch. A UTC-offset
    implementation passes one of these and fails the other."""
    summer = _win(datetime(2026, 7, 22, 9, 0, tzinfo=ET))
    winter = _win(datetime(2026, 1, 14, 9, 0, tzinfo=ET))
    assert summer["open"] and winter["open"]
    assert "EDT" in summer["local_time"] and "EST" in winter["local_time"]


def test_03_closed_reason_is_speakable(monkeypatch):
    w = _win(datetime(2026, 7, 25, 12, 0, tzinfo=ET))
    assert "Saturday" in w["reason"]


def test_04_no_number_configured_is_closed_not_crashed(monkeypatch):
    monkeypatch.setenv("VOICE_TRANSFER_NUMBER", "")
    w = _win(datetime(2026, 7, 22, 12, 0, tzinfo=ET))
    assert w["open"] is False and "no VOICE_TRANSFER_NUMBER" in w["reason"]


def test_05_unparseable_number_degrades_to_message(monkeypatch):
    """A typo must not emit a <Dial> the carrier rejects."""
    monkeypatch.setenv("VOICE_TRANSFER_NUMBER", "call me maybe")
    assert V.transfer_number() == ""
    assert _win(datetime(2026, 7, 22, 12, 0, tzinfo=ET))["open"] is False


def test_06_number_is_normalised_to_e164(monkeypatch):
    monkeypatch.setenv("VOICE_TRANSFER_NUMBER", "416 889 6638")
    assert V.transfer_number() == "+14168896638"


def test_07_disabled_flag_closes_the_window(monkeypatch):
    monkeypatch.setattr(V, "TRANSFER_ENABLED", False)
    assert _win(datetime(2026, 7, 22, 12, 0, tzinfo=ET))["open"] is False


def test_08_malformed_hours_fall_back_not_midnight(monkeypatch):
    """'5:30pm' is not HH:MM. Parsing it as 0:00 would close the line all day."""
    monkeypatch.setenv("VOICE_TRANSFER_END", "5:30pm")
    assert V._hhmm("VOICE_TRANSFER_END", "17:30") == (17, 30)


def test_09_unknown_timezone_falls_back(monkeypatch):
    monkeypatch.setattr(V, "TRANSFER_TZ", "Mars/Olympus_Mons")
    assert _win(datetime(2026, 7, 22, 12, 0, tzinfo=ET))["open"] is True


# ── who counts as asking for a person ────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "I want to talk to a human",
    "can I speak to someone please",
    "transfer me to an agent",
    "put me through to sales",
    "connect me with support",
    "forward me to customer service",
    "get me a manager",
    "is there a real person there",
    "我要转人工",
    "请帮我转接人工客服",
    "我想跟真人说话",
    "quiero hablar con una persona",
    "puede pasarme con un agente",
    "je veux parler à un humain",
])
def test_10_asks_for_a_human(text):
    assert escalation.detect(text) == "customer_requested_human"


@pytest.mark.parametrize("text", [
    "你们的人工智能怎么样",          # "how is your AI?" — contains 人工
    "人工智能可以做什么",
    "tell me about your AI agents",
    "what are your prices",
    "how does the CRM work",
])
def test_11_does_not_transfer_ai_questions(text):
    """人工智能 (artificial intelligence) contains 人工 (human operator).
    Without the negative lookahead, every Chinese question about our own AI
    rings a real phone."""
    assert escalation.detect(text) != "customer_requested_human"


# ── the TeXML ────────────────────────────────────────────────────────────────

def test_12_dial_targets_the_configured_number(monkeypatch):
    monkeypatch.setenv("VOICE_TRANSFER_NUMBER", "416 889 6638")
    xml = V.dial_twiml("en", "/sdr/voice/transfer-result")
    assert "<Number>+14168896638</Number>" in xml


def test_13_dial_has_an_action_so_no_answer_returns_to_us():
    """Without action=, an unanswered ring hangs up on the caller."""
    xml = V.dial_twiml("en", "/sdr/voice/transfer-result")
    assert 'action="/sdr/voice/transfer-result"' in xml
    assert re.search(r'timeout="\d+"', xml)


def test_13b_answer_on_bridge_defers_the_join():
    """Our leg is already answered (we just spoke). Without answerOnBridge the
    carrier joins the legs while the cell is still ringing, which is the window
    transfer audio artifacts live in."""
    assert 'answerOnBridge="true"' in V.dial_twiml("en", "/x")


def test_13c_whisper_announces_the_customer_not_you(monkeypatch):
    """On the whisper leg the carrier reports From=our DID and To=the cell, so
    reading either would read YOUR number back to you. It has to ride the URL."""
    monkeypatch.setattr(V, "TRANSFER_WHISPER", True)
    xml = V.dial_twiml("en", "/x", caller="+14164779298")
    assert "/sdr/voice/whisper?c=" in xml
    assert "%2B14164779298" in xml
    said = V.whisper_twiml("+14164779298")
    assert "9 2 9 8" in said and "6638" not in said


def test_13d_whisper_is_off_by_default():
    assert 'url="' not in V.dial_twiml("en", "/x", caller="+14164779298")


def test_14_caller_id_is_ours_not_the_customers(monkeypatch):
    """Passing the customer's number through is spoofing; carriers reject it."""
    monkeypatch.setenv("VOICE_TRANSFER_CALLER_ID", "+16475550123")
    assert 'callerId="+16475550123"' in V.dial_twiml("en", "/x")


@pytest.mark.parametrize("lang,voice", [
    ("en", "alice"), ("zh", "Polly.Zhiyu"),
])
def test_15_connecting_line_uses_the_callers_voice(lang, voice):
    assert f'voice="{voice}"' in V.dial_twiml(lang, "/x")


@pytest.mark.parametrize("lang", ["en", "fr", "es", "zh"])
def test_16_closed_message_states_the_hours(lang):
    """'Someone will get back to you' with no WHEN is the vague promise that
    made escalations necessary in the first place."""
    msg = V.transfer_message(lang, _win(datetime(2026, 7, 25, 12, 0, tzinfo=ET)))
    assert "8:30" in msg                      # opening time, every language
    assert ("5:30" in msg) or ("17:30" in msg)  # 12h for en/es, 24h for fr/zh
    assert len(msg) > 40


def test_17_english_hours_are_spoken_not_military():
    """TTS reads '17:30' as 'seventeen thirty', which no caller says."""
    msg = V.transfer_message("en", _win(datetime(2026, 7, 25, 12, 0, tzinfo=ET)))
    assert "5:30 PM" in msg and "8:30 AM" in msg


def test_18_no_answer_leads_with_the_apology():
    """The caller just heard 'connecting you now' — opening with business
    hours would sound like we never tried."""
    msg = V.no_answer_message("en", _win(datetime(2026, 7, 22, 12, 0, tzinfo=ET)))
    assert msg.lower().startswith("sorry")


# ── the language menu must respond to the keypad immediately ─────────────────

def test_20_menu_gather_is_dtmf_only():
    """A speech recogniser running during the menu competes with the keypad —
    a 3 pressed while option 4 was still playing was being dropped."""
    from app.core import sdr
    xml = sdr.lang_menu_gather(sdr._say("hi"))
    assert 'input="dtmf"' in xml
    assert "speech" not in xml.split(">")[0]      # not input="speech dtmf"
    assert 'speechTimeout' not in xml


def test_21_menu_options_are_inside_the_gather():
    """Nested prompts are what make a keypress barge in. Options played AFTER
    </Gather> would have to finish before any digit was collected."""
    from app.core import sdr
    xml = sdr.lang_menu_gather(sdr._say("hi"))
    body = xml[xml.index(">") + 1:xml.index("</Gather>")]
    for digit in sdr._LANG_MENU_ORDER:
        assert f"press {digit}" in body.lower() or digit in body


def test_22_no_digit_falls_through_to_speech():
    """A caller who never presses anything must still be able to just talk."""
    from app.core import sdr
    menu = sdr.lang_menu_gather(sdr._say("hi"))
    assert "</Gather>" in menu and menu.rstrip().endswith("</Gather>")


def test_23_obligation_writer_never_raises(monkeypatch):
    """A DB problem must not break the call in progress."""
    import app.core.escalation as E
    monkeypatch.setattr(E, "open", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db down")))
    V.open_callback_obligation(conversation_id=None, handle=None,
                               channel="voice", heard="hi", window={})
