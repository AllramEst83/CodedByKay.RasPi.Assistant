"""Wake detector tests. Negative cases are offline; positive cases need fixtures (tools/make_fixtures.py)."""
import random
import struct
from pathlib import Path

import pytest

from assistant.audio import FakeMic, read_wav_pcm16
from assistant.config import BASE_DIR, FRAME_MS, INPUT_RATE
from assistant.wake import WakeDetector

MODEL = BASE_DIR / "models" / "vosk-model-small-en-us-0.15"
FIXTURES = Path(__file__).parent / "fixtures"
PHRASES = ("hey gemini", "hey assistant")

pytestmark = pytest.mark.skipif(not MODEL.is_dir(), reason="Vosk model not installed")


def detect(detector, pcm, tail_s=1.0):
    mic = FakeMic(pcm + b"\0" * int(INPUT_RATE * 2 * tail_s), speed=1000)
    for _ in range(len(pcm) // 1280 + int(tail_s * 1000 / FRAME_MS) + 1):
        hit = detector.feed(mic.read())
        if hit:
            return hit
    return None


def test_silence_does_not_trigger():
    assert detect(WakeDetector(MODEL, PHRASES), b"\0" * INPUT_RATE * 2 * 20) is None


def test_noise_does_not_trigger():
    rnd = random.Random(1)
    noise = struct.pack("<" + "h" * (INPUT_RATE * 15), *(rnd.randint(-3000, 3000) for _ in range(INPUT_RATE * 15)))
    assert detect(WakeDetector(MODEL, PHRASES), noise) is None


@pytest.mark.integration
@pytest.mark.parametrize("name,expected", [("hey_gemini", "hey gemini"), ("hey_assistant", "hey assistant")])
def test_spoken_phrase_triggers(name, expected):
    path = FIXTURES / f"{name}.wav"
    if not path.exists():
        pytest.skip("run tools/make_fixtures.py first")
    assert detect(WakeDetector(MODEL, PHRASES), read_wav_pcm16(path, INPUT_RATE)) == expected


@pytest.mark.integration
def test_question_does_not_trigger():
    path = FIXTURES / "question_en.wav"
    if not path.exists():
        pytest.skip("run tools/make_fixtures.py first")
    assert detect(WakeDetector(MODEL, PHRASES), read_wav_pcm16(path, INPUT_RATE)) is None
