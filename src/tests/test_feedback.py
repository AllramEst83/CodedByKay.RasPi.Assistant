"""Error classification and audible feedback. No network, no audio devices."""
import asyncio
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest
from google.genai import errors

from assistant import feedback
from assistant.audio import FakeMic, FakeSpeaker, resample_pcm16
from assistant.config import Config
from assistant.session import LiveConversation


def api_error(cls, code, status, message):
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


@pytest.mark.parametrize("exc, kind", [
    (api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED", "Your prepayment credits are depleted."), "credits"),
    (api_error(errors.ClientError, 402, "", "Payment required"), "credits"),
    (api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED",
               "You exceeded your current quota, please check your plan and billing details."), "rate_limit"),
    (api_error(errors.ClientError, 403, "PERMISSION_DENIED", "API key not valid"), "auth"),
    (api_error(errors.ClientError, 400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."), "auth"),
    (api_error(errors.ServerError, 503, "UNAVAILABLE", "The model is overloaded."), "server"),
    (api_error(errors.ServerError, 500, "INTERNAL", "Internal error"), "server"),
    (RuntimeError("received 1011 (internal error) Resource has been exhausted (e.g. check quota)."), "rate_limit"),
    (RuntimeError("received 1008 (policy violation) caller does not have permission"), "auth"),
    (RuntimeError("received 1011 (internal error) The service is currently unavailable."), "server"),
    (socket.gaierror(-3, "Temporary failure in name resolution"), "network"),
    (ConnectionRefusedError(), "network"),
    (TimeoutError(), "network"),
    (ValueError("something odd"), "unknown"),
])
def test_classify(exc, kind):
    assert feedback.classify(exc) == kind


def test_classify_accepts_reason_strings():
    assert feedback.classify("error: ClientError: 429 RESOURCE_EXHAUSTED. prepayment credits are depleted") == "credits"


def test_every_kind_has_a_distinct_tone_and_both_languages():
    tones = {k: feedback.tone_pattern(k) for k in feedback.KINDS}
    assert all(len(t) > 1000 for t in tones.values())
    assert len(set(tones.values())) == len(tones)
    for _, texts in feedback.KINDS.values():
        assert texts["sv"] and texts["en"]


def test_resample_changes_length_proportionally():
    pcm = b"\x10\x00" * 2205  # 0.1 s at 22.05 kHz
    out = resample_pcm16(pcm, 22050, 24000)
    assert len(out) // 2 == 2400


def test_announce_plays_tone_even_without_espeak(monkeypatch):
    monkeypatch.setattr(feedback, "speak", lambda text, lang="sv", rate=24000: None)
    speaker = FakeSpeaker()
    feedback.announce("credits", speaker, "sv", True, log=lambda s: None)
    assert len(speaker.audio) == len(feedback.tone_pattern("credits"))


def test_announce_speaks_after_tone_when_voice_available(monkeypatch):
    monkeypatch.setattr(feedback, "speak", lambda text, lang="sv", rate=24000: b"\x07\x00" * 100)
    speaker = FakeSpeaker()
    feedback.announce("auth", speaker, "en", True, log=lambda s: None)
    assert len(speaker.audio) == len(feedback.tone_pattern("auth")) + 200


def test_connect_failure_sets_last_error_that_classifies_as_credits():
    err = api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED", "Your prepayment credits are depleted.")

    @asynccontextmanager
    async def connect(model, config):
        raise err
        yield  # pragma: no cover

    factory = lambda: NS(aio=NS(live=NS(connect=connect)))
    cfg = Config(gemini_api_key="test", vosk_model_path="unused", idle_timeout=0.4)
    conv = LiveConversation(cfg, factory, FakeMic(b"\x00\x00" * 2000, speed=50), FakeSpeaker(), log=lambda s: None)
    reason = asyncio.run(conv.run())
    assert reason.startswith("error")
    assert feedback.classify(conv.last_error) == "credits"


def test_resample_48k_to_16k_keeps_a_tone_and_length():
    import math
    from array import array

    tone = array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(4800)))  # 0.1 s
    out = array("h")
    out.frombytes(resample_pcm16(tone.tobytes(), 48000, 16000))
    assert len(out) == 1600
    assert max(out) > 6000  # amplitude survives the low-pass


def test_resample_24k_to_48k_doubles_length():
    assert len(resample_pcm16(bytes(2400), 24000, 48000)) == 4800


# --- wake greeting -----------------------------------------------------------------------------------------------

def _cfg(**kw):
    return Config(gemini_api_key="test", vosk_model_path="unused", idle_timeout=0.5, **kw)


@pytest.fixture
def fake_espeak(monkeypatch):
    feedback.speak_cached.cache_clear()
    monkeypatch.setattr(feedback, "speak", lambda text, lang="sv", rate=24000: f"{lang}:{text}".encode())
    yield
    feedback.speak_cached.cache_clear()


def test_greeting_language_follows_the_wake_phrase(fake_espeak):
    cfg = _cfg(greetings={"sv": "Nu är jag vaken", "en": "Now I am awake"}, greeting_map={"hey gemini": "sv", "hey assistant": "en"})
    assert feedback.greeting_audio(cfg, "hey gemini") == b"sv:Nu \xc3\xa4r jag vaken"
    assert feedback.greeting_audio(cfg, "Hey Assistant") == b"en:Now I am awake"
    assert feedback.greeting_audio(cfg, "hey something else") is None


def test_empty_greeting_text_turns_it_off(fake_espeak):
    cfg = _cfg(greetings={"sv": "", "en": "Yes?"}, greeting_map={"hey gemini": "sv", "hey assistant": "en"})
    assert feedback.greeting_audio(cfg, "hey gemini") is None
    assert feedback.greeting_audio(cfg, "hey assistant") is not None


def test_default_greeting_is_online_for_both_wake_phrases(fake_espeak):
    cfg = _cfg()
    assert feedback.greeting_audio(cfg, "hey gemini") == b"en:Online"
    assert feedback.greeting_audio(cfg, "hey assistant") == b"en:Online"


def test_greeting_is_none_without_espeak(monkeypatch):
    feedback.speak_cached.cache_clear()
    monkeypatch.setattr(feedback, "speak", lambda text, lang="sv", rate=24000: None)
    assert feedback.greeting_audio(_cfg(), "hey gemini") is None
    feedback.speak_cached.cache_clear()


def test_greeting_is_rendered_once(monkeypatch):
    feedback.speak_cached.cache_clear()
    calls = []
    monkeypatch.setattr(feedback, "speak", lambda text, lang="sv", rate=24000: calls.append(text) or b"x")
    for _ in range(3):
        feedback.greeting_audio(_cfg(), "hey gemini")
    assert calls == ["Online"]
    feedback.speak_cached.cache_clear()


def test_parse_map():
    from assistant.config import _parse_map

    assert _parse_map("Hey Gemini:sv, hey assistant : en,junk") == {"hey gemini": "sv", "hey assistant": "en"}


def test_run_once_plays_chime_then_greeting_and_mutes_the_mic_until_it_ends(fake_espeak):
    import time

    from assistant import app
    from test_session_fake import FakeSession, factory_for

    class Speaker(FakeSpeaker):
        def write(self, pcm):
            super().write(pcm)
            if pcm.startswith(b"en:"):
                self.busy_until = time.monotonic() + 1.0  # the greeting takes a second to play

    class Detector:
        def reset(self): pass
        def feed(self, frame): return "hey gemini"

    session = FakeSession([])
    speaker = Speaker()
    mic = FakeMic(b"\x05\x00" * 40000, speed=1)
    app.run_once(_cfg(), mic, speaker, Detector(), factory_for(session), log=lambda s: None)

    assert speaker.chunks[1] == b"en:Online"  # right after the chime
    frames = [kw["audio"].data for kw in session.sent]
    silent = [not any(f) for f in frames]
    assert silent[0] and not silent[-1]  # muted at first, open once the greeting ended
    assert silent.index(False) > 0 and all(not s for s in silent[silent.index(False):])


def test_trim_silence_keeps_the_speech_and_a_short_pad():
    from array import array

    quiet, loud = [0] * 2400, [5000, -5000] * 100  # 0.1 s of silence at 24 kHz, then speech, then silence
    pcm = array("h", quiet + loud + quiet).tobytes()
    out = array("h")
    out.frombytes(feedback._trim_silence(pcm, 24000))
    pad = 24000 * 30 // 1000
    assert len(out) == len(loud) + 2 * pad
    assert feedback._trim_silence(array("h", quiet).tobytes(), 24000) == array("h", quiet).tobytes()  # all silence: untouched
