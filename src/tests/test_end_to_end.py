"""Real Gemini Live round trips with spoken fixtures standing in for the microphone. Needs network + API key."""
import asyncio
from functools import partial
from pathlib import Path

import pytest

from assistant import app
from assistant.audio import FakeMic, FakeSpeaker, read_wav_pcm16
from assistant.config import BASE_DIR, INPUT_RATE, ConfigError, load_config
from assistant.session import LiveConversation, make_client
from assistant.wake import WakeDetector

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.integration


def load(name):
    path = FIXTURES / f"{name}.wav"
    if not path.exists():
        pytest.skip("run tools/make_fixtures.py first")
    return read_wav_pcm16(path, INPUT_RATE)


@pytest.fixture(scope="module")
def cfg():
    try:
        return load_config()
    except ConfigError as e:
        pytest.skip(str(e))


def test_swedish_question_gets_spoken_swedish_answer(cfg):
    speaker = FakeSpeaker()
    mic = FakeMic(load("question_sv"))
    conv = LiveConversation(cfg, partial(make_client, cfg), mic, speaker, log=lambda s: None)
    assert asyncio.run(conv.run()) == "idle"
    assert len(speaker.audio) > 24000 * 2, "expected at least a second of reply audio"
    assert "".join(conv.transcript["assistant"]).strip()


def test_wake_then_question_full_cycle(cfg):
    pcm = load("hey_gemini") + b"\0" * 19200 + load("question_en")
    speaker = FakeSpeaker()
    detector = WakeDetector(cfg.vosk_model_path, cfg.wake_phrases)
    conv = app.run_once(cfg, FakeMic(pcm), speaker, detector, partial(make_client, cfg), log=lambda s: None)
    assert len(speaker.audio) > 24000 * 2
    assert "".join(conv.transcript["assistant"]).strip()
