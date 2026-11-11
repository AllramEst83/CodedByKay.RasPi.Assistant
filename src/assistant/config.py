"""Settings and constants. Secrets come from .env, never from code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

INPUT_RATE = 16000  # mic -> Vosk and Gemini
OUTPUT_RATE = 24000  # Gemini -> speaker
FRAME_MS = 40
INPUT_FRAME_BYTES = INPUT_RATE * FRAME_MS // 1000 * 2  # 16-bit mono

SYSTEM_PROMPT = (
    "You are a friendly voice assistant running on a Raspberry Pi 5. "
    "Your replies are spoken aloud. Reply in natural Swedish unless the user "
    "speaks English, then reply in English. Keep answers short and "
    "conversational, one to three sentences. Never use markdown, lists, "
    "tables or symbols that are awkward to read aloud."
)

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    gemini_api_key: str = field(repr=False)  # keep the secret out of logs, tracebacks and test output
    vosk_model_path: Path
    model: str = "gemini-3.8-live"
    api_version: str = "v1beta"  # the Live model gives 404 on the SDK's default version; v1beta and v1alpha work
    voice: str = "Puck"
    idle_timeout: float = 8.0  # seconds without speech from either party
    wake_phrases: tuple[str, ...] = ("hey gemini", "hey assistant")
    audio_in_device: str | None = None  # index or name substring
    audio_out_device: str | None = None
    mute_mic_while_speaking: bool = False  # set True if the speaker echoes into the mic
    speech_rms: float = 500.0  # mic level (16-bit RMS) counted as speech for the idle timer; raise if room noise keeps sessions open
    system_prompt: str = SYSTEM_PROMPT


def load_config(env_path: str | Path | None = None) -> Config:
    load_dotenv(env_path or BASE_DIR / ".env")
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise ConfigError("GEMINI_API_KEY is missing; set it in .env")
    phrases = tuple(
        p.strip().lower()
        for p in os.getenv("WAKE_PHRASES", "hey gemini,hey assistant").split(",")
        if p.strip()
    )
    return Config(
        gemini_api_key=key,
        vosk_model_path=Path(
            os.getenv("VOSK_MODEL_PATH", BASE_DIR / "models" / "vosk-model-small-en-us-0.15")
        ),
        model=os.getenv("GEMINI_MODEL", "gemini-3.8-live"),
        api_version=os.getenv("GEMINI_API_VERSION", "v1beta"),
        voice=os.getenv("GEMINI_VOICE", "Puck"),
        idle_timeout=float(os.getenv("IDLE_TIMEOUT", "8.0")),
        wake_phrases=phrases or Config.wake_phrases,
        audio_in_device=os.getenv("AUDIO_IN_DEVICE") or None,
        audio_out_device=os.getenv("AUDIO_OUT_DEVICE") or None,
        mute_mic_while_speaking=os.getenv("MUTE_MIC_WHILE_SPEAKING", "").lower() in ("1", "true", "yes"),
        speech_rms=float(os.getenv("SPEECH_RMS", "500")),
    )
