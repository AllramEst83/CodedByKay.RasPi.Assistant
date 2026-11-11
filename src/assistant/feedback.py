"""Audible error feedback: classify an exception, play a tone pattern, optionally speak a short message.

Tones are generated in code (no dependencies), so they always work, even offline or with no credits.
Speech uses the optional `espeak-ng` program (offline, `sudo apt install espeak-ng`); without it only the tones play.
"""
from __future__ import annotations

import io
import math
import re
import shutil
import struct
import subprocess
import wave
from functools import lru_cache
from typing import Callable

from .audio import resample_pcm16
from .config import OUTPUT_RATE
from .log import log as default_log

# kind -> (tone pattern as (Hz, seconds) pairs, spoken text per language)
# Patterns differ in pitch and rhythm so each error is recognisable without the voice.
KINDS: dict[str, tuple[tuple[tuple[float, float], ...], dict[str, str]]] = {
    "credits": (
        ((440.0, 0.18), (349.2, 0.18), (261.6, 0.40)),  # three steps down
        {"sv": "Gemini-krediterna är slut. Fyll på kontot.", "en": "Gemini credits have run out. Please top up."},
    ),
    "rate_limit": (
        ((660.0, 0.10), (0.0, 0.08), (660.0, 0.10), (0.0, 0.08), (660.0, 0.10)),  # three short beeps
        {"sv": "För många förfrågningar. Vänta en stund.", "en": "Too many requests. Wait a moment."},
    ),
    "auth": (
        ((330.0, 0.30), (0.0, 0.08), (330.0, 0.30)),  # two long low beeps
        {"sv": "API-nyckeln fungerar inte.", "en": "The API key is not working."},
    ),
    "network": (
        ((880.0, 0.10), (0.0, 0.05), (587.3, 0.10), (0.0, 0.05), (392.0, 0.25)),  # falling blip
        {"sv": "Ingen internetanslutning.", "en": "No internet connection."},
    ),
    "server": (
        ((300.0, 0.15), (400.0, 0.15), (300.0, 0.15), (400.0, 0.15)),  # wobble
        {"sv": "Gemini är överbelastad. Försök igen strax.", "en": "Gemini is overloaded. Try again shortly."},
    ),
    "unknown": (
        ((300.0, 0.50),),  # one long low tone
        {"sv": "Något gick fel.", "en": "Something went wrong."},
    ),
}

_CREDITS = ("prepayment", "credits are depleted", "outstanding balance")  # not "billing": plain quota errors say "check your plan and billing details"
_RATE = ("resource_exhausted", "resource has been exhausted", "quota", "rate limit", "too many requests")
_AUTH = ("api key", "api_key", "permission", "denied", "unauthenticated", "unauthorized", "leaked")
_SERVER = ("unavailable", "overloaded", "internal", "deadline", "timed out")
_NET = ("name resolution", "temporary failure", "network is unreachable", "connection refused", "no route")


def _code_of(exc: BaseException) -> int | None:
    """HTTP status (SDK APIError) or websocket close code (websockets / 'received 1011 ...' text)."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None and isinstance(getattr(rcvd, "code", None), int):
        return rcvd.code
    m = re.search(r"\b(1008|1011|4\d\d|5\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


def classify(exc: BaseException | str) -> str:
    """Map an exception (or an 'error: ...' reason string) to one of KINDS."""
    text = (str(exc) + " " + type(exc).__name__).lower() if not isinstance(exc, str) else exc.lower()
    code = _code_of(exc) if not isinstance(exc, str) else (_code_of(RuntimeError(exc)))
    if any(k in text for k in _CREDITS) or code == 402:
        return "credits"
    if any(k in text for k in _RATE) or code == 429:
        return "rate_limit"
    if any(k in text for k in _AUTH) or code in (401, 403, 1008):
        return "auth"
    if isinstance(exc, (OSError, TimeoutError)) or any(k in text for k in _NET):
        return "network"
    if any(k in text for k in _SERVER) or code in (408, 1011) or (code is not None and 500 <= code < 600):
        return "server"
    return "unknown"


def tone_pattern(kind: str, rate: int = OUTPUT_RATE) -> bytes:
    """Render the tone pattern for `kind` as 24 kHz mono 16-bit PCM."""
    out = bytearray()
    for freq, dur in KINDS[kind][0]:
        n = int(rate * dur)
        for i in range(n):
            env = math.sin(math.pi * i / n) ** 0.5 if freq else 0.0  # soft edges, no clicks
            out += struct.pack("<h", int(32767 * 0.3 * env * math.sin(2 * math.pi * freq * i / rate)))
    return bytes(out)


def speak(text: str, lang: str = "sv", rate: int = OUTPUT_RATE) -> bytes | None:
    """Render text with espeak-ng to 24 kHz PCM, or None if espeak-ng is missing or fails."""
    exe = shutil.which("espeak-ng")
    if not exe:
        return None
    try:
        wav = subprocess.run([exe, "-v", f"{lang}+f3", "-s", "150", "--stdout", text],  # f3 = female variant
                             capture_output=True, timeout=10, check=True).stdout
        with wave.open(io.BytesIO(wav), "rb") as w:
            src_rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    except (subprocess.SubprocessError, wave.Error, OSError):
        return None
    return resample_pcm16(pcm, src_rate, rate)


@lru_cache(maxsize=32)
def speak_cached(text: str, lang: str) -> bytes | None:
    """speak() remembered per (text, language), so a greeting is rendered once and then plays instantly."""
    return speak(text, lang)


def greeting_audio(cfg, phrase: str) -> bytes | None:
    """The spoken greeting for the wake phrase that was heard, or None (no text set, unknown phrase, or no espeak-ng)."""
    lang = cfg.greeting_map.get(phrase.lower())
    text = cfg.greetings.get(lang, "") if lang else ""
    return speak_cached(text, lang) if text else None


def prewarm_greetings(cfg) -> None:
    """Render every greeting at startup so the first wake is as fast as the rest."""
    for phrase in cfg.greeting_map:
        greeting_audio(cfg, phrase)


def announce(kind: str, speaker, lang: str = "sv", voice: bool = True,
             log: Callable[[str], None] = default_log) -> None:
    """Play the tone pattern for `kind`, then speak the message if espeak-ng is available."""
    kind = kind if kind in KINDS else "unknown"
    text = KINDS[kind][1].get(lang) or KINDS[kind][1]["en"]
    log(f"[Feedback] {kind}: {text}")
    speaker.play_blocking(tone_pattern(kind))
    if voice:
        pcm = speak(text, lang)
        if pcm:
            speaker.play_blocking(pcm)
