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
