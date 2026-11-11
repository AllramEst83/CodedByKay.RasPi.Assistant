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
    "Your replies are spoken aloud. Always reply in the same language as the "
    "user's most recent question and switch when they switch; the user mostly "
    "speaks Swedish or English. If the language is unclear, use Swedish. The "
    "audio may begin with the tail of a wake phrase ('hey gemini', 'hey "
    "assistant') or a stray syllable: ignore it, do not greet, do not reply to "
    "it, and never choose the reply language from it. Answer only the actual "
    "question. Keep answers short and conversational, one to three sentences. "
    "Never use markdown, lists, tables or symbols that are awkward to read aloud. "
    "If, and only if, the user says 'go to sleep', say a very short goodbye in their "
    "language and call go_to_sleep. When the user asks for a timer or alarm, call "
    "set_timer with the time in seconds and confirm in one short sentence; the device "
    "rings by itself. Use cancel_timers and list_timers when asked to cancel or list them."
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
    voice: str = "Kore"  # female; others: Aoede, Leda, Zephyr (female), Puck, Charon, Fenrir (male)
    idle_timeout: float = 8.0  # seconds without speech from either party
    wake_phrases: tuple[str, ...] = ("hey gemini", "hey assistant")
    audio_in_device: str | None = None  # index or name substring
    audio_out_device: str | None = None
    mute_tail_ms: int = 600  # keep ignoring the mic this long after the speaker stops (reverb, speaker and network delay)
    mute_mic_while_speaking: bool = False  # set True if the speaker echoes into the mic
    speech_rms: float = 500.0  # mic level (16-bit RMS) counted as speech for the idle timer; raise if room noise keeps sessions open
    wake_confirm_ms: int = 200  # after a wake-phrase match, listen this long and confirm with a final result (0 = off): fewer false triggers
    wake_skip_ms: int = 600  # audio discarded after the chime so the wake phrase's tail is not sent to Gemini
    google_search: bool = True  # let Gemini search the web for current facts (news, weather, prices, dates)
    timer_tool: bool = True  # "set a timer for 5 minutes": Gemini starts timers that ring on the device
    sleep_tool: bool = True  # saying "go to sleep" ends the conversation at once instead of waiting for the idle timeout
    # Spoken (robot voice) right after the wake chime, chosen by which wake phrase was heard. An empty text turns it off.
    # Keep it to one short word: it should end inside the wake_skip_ms window.
    greetings: dict = field(default_factory=lambda: {"sv": "Ja?", "en": "Online"})
    greeting_map: dict = field(default_factory=lambda: {"hey gemini": "en", "hey assistant": "en"})  # phrase -> language
    greeting_speed: int = 115  # espeak words per minute for the greeting (error messages use 150); lower = slower
    feedback_lang: str = "sv"  # language of spoken error messages: sv or en
    error_voice: bool = True  # speak error messages (needs espeak-ng); tones always play
    system_prompt: str = SYSTEM_PROMPT


def _parse_map(text: str) -> dict[str, str]:
    """'hey gemini:sv,hey assistant:en' -> {'hey gemini': 'sv', 'hey assistant': 'en'}"""
    pairs = (item.rsplit(":", 1) for item in text.split(",") if ":" in item)
    return {phrase.strip().lower(): lang.strip().lower() for phrase, lang in pairs}


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
        voice=os.getenv("GEMINI_VOICE", "Kore"),
        idle_timeout=float(os.getenv("IDLE_TIMEOUT", "8.0")),
        wake_phrases=phrases or Config.wake_phrases,
        audio_in_device=os.getenv("AUDIO_IN_DEVICE") or None,
        audio_out_device=os.getenv("AUDIO_OUT_DEVICE") or None,
        mute_mic_while_speaking=os.getenv("MUTE_MIC_WHILE_SPEAKING", "").lower() in ("1", "true", "yes"),
        mute_tail_ms=int(os.getenv("MUTE_TAIL_MS", "600")),
        speech_rms=float(os.getenv("SPEECH_RMS", "500")),
        wake_confirm_ms=int(os.getenv("WAKE_CONFIRM_MS", "200")),
        wake_skip_ms=int(os.getenv("WAKE_SKIP_MS", "600")),
        google_search=os.getenv("GOOGLE_SEARCH", "true").lower() in ("1", "true", "yes"),
        sleep_tool=os.getenv("SLEEP_TOOL", "true").lower() in ("1", "true", "yes"),
        timer_tool=os.getenv("TIMER_TOOL", "true").lower() in ("1", "true", "yes"),
        greetings={"sv": os.getenv("GREETING_SV", "Ja?").strip(),
                   "en": os.getenv("GREETING_EN", "Online").strip()},
        greeting_map=_parse_map(os.getenv("GREETING_MAP", "hey gemini:en,hey assistant:en")),
        greeting_speed=int(os.getenv("GREETING_SPEED", "115")),
        feedback_lang=os.getenv("FEEDBACK_LANG", "sv").lower(),
        error_voice=os.getenv("ERROR_VOICE", "true").lower() in ("1", "true", "yes"),
    )
