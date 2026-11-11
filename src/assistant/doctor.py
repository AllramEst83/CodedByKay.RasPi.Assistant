"""--doctor: check everything the assistant needs and print one colored Pass / Warn / Fail line per item.

Run on the Pi:  python -m assistant --doctor   (add --skip-audio or --skip-api to leave those checks out)
The API check opens one short Gemini Live session (a few tokens), so it costs next to nothing.
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import stat
import time
from pathlib import Path
from typing import Callable

from . import feedback
from .config import BASE_DIR, INPUT_RATE, Config
from .log import log as default_log

PASS, WARN, FAIL = "Pass", "Warn", "Fail"
Result = tuple[str, str]  # (status, message)


def check_env_file(path: Path) -> Result:
    if not path.exists():
        return FAIL, f"{path} not found (copy .env.example to .env and add GEMINI_API_KEY)"
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return WARN, f"{path.name} can be read by other users (mode {mode:o}); run: chmod 600 {path}"
    return PASS, f"{path.name} found, permissions {mode:o}"


def check_key(cfg: Config) -> Result:
    # never print the key itself, only that it exists
    return PASS, f"API key present ({len(cfg.gemini_api_key)} characters)"


def check_vosk(cfg: Config) -> Result:
    from .wake import WakeDetector

    if not Path(cfg.vosk_model_path).is_dir():
        return FAIL, f"Vosk model not found at {cfg.vosk_model_path}"
    t0 = time.monotonic()
    detector = WakeDetector(cfg.vosk_model_path, cfg.wake_phrases)
    load = time.monotonic() - t0
    t0 = time.monotonic()
    for _ in range(25):  # one second of silence
        detector.feed(bytes(1280))
    speed = 1.0 / max(time.monotonic() - t0, 1e-6)
    return PASS, f"model loaded in {load:.1f} s, phrases: {', '.join(cfg.wake_phrases)} ({speed:.0f}x real time)"


def check_espeak(cfg: Config) -> Result:
    exe = shutil.which("espeak-ng")
    if not exe:
        return WARN, "espeak-ng not installed: error messages, the greeting and timers are tones only (sudo apt install -y espeak-ng)"
    return PASS, "espeak-ng installed (greeting, error messages and timer messages are spoken)"


def check_network() -> Result:
    host = "generativelanguage.googleapis.com"
    try:
        t0 = time.monotonic()
        with socket.create_connection((host, 443), timeout=5):
            pass
    except OSError as e:
        return FAIL, f"cannot reach {host}: {e}"
    return PASS, f"{host} reachable ({(time.monotonic() - t0) * 1000:.0f} ms)"


def check_devices() -> Result:
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        ins, outs = [], []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0 and "hdmi" not in d["name"].lower() and "default" not in d["name"].lower():
                ins.append(d["name"])
            if d["maxOutputChannels"] > 0 and "hdmi" not in d["name"].lower() and "default" not in d["name"].lower():
                outs.append(d["name"])
    finally:
        pa.terminate()
    if not ins:
        return FAIL, "no microphone found (plug in the USB mic; see: python -m assistant --list-devices)"
    if not outs:
        return WARN, f"microphone found ({ins[0]}) but only HDMI or default outputs; no USB speaker/sound card"
    return PASS, f"{len(ins)} input device(s), {len(outs)} output device(s)"


def check_mic(cfg: Config) -> Result:
    from .audio import PyAudioMic, _rms_bytes

    try:
        mic = PyAudioMic(cfg.audio_in_device)
    except (OSError, RuntimeError) as e:
        return FAIL, f"cannot open a microphone ({e}). Plug in the USB mic; check: python -m assistant --list-devices"
    try:
        levels = [_rms_bytes(mic.read()) for _ in range(40)]  # about 1.6 s
    finally:
        mic.close()
    peak = max(levels)
    msg = f"{mic.device_name}, opened at {mic._rate} Hz (converted to {INPUT_RATE} Hz), level peak {peak:.0f}"
    if peak < 5:
        return WARN, msg + ": silent. Is the mic muted or the wrong device? Set AUDIO_IN_DEVICE"
    return PASS, msg + " (speak to see it rise)"


def check_speaker(cfg: Config) -> Result:
    from .audio import PyAudioSpeaker, generate_chime

    try:
        speaker = PyAudioSpeaker(cfg.audio_out_device)
    except (OSError, RuntimeError) as e:
        return FAIL, f"cannot open a speaker ({e}). Plug in the USB sound card; check: python -m assistant --list-devices"
    try:
        speaker.play_blocking(generate_chime())
        speaker.play_blocking(generate_chime(descending=True))
    finally:
        speaker.close()
    return PASS, f"{speaker.device_name}, opened at {speaker._rate} Hz: played a rising and a falling chime (did you hear them?)"


async def _live_probe(cfg: Config) -> str:
    from .session import build_live_config, make_client

    t0 = time.monotonic()
    audio = 0
    async with make_client(cfg).aio.live.connect(model=cfg.model, config=build_live_config(cfg)) as s:
        await s.send_realtime_input(text="Say OK.")
        async for msg in s.receive():
            sc = getattr(msg, "server_content", None)
            if sc is None:
                continue
            for part in (sc.model_turn.parts if sc.model_turn else []) or []:
                if getattr(getattr(part, "inline_data", None), "data", None):
                    audio += len(part.inline_data.data)
            if getattr(sc, "turn_complete", False):
                break
    if not audio:
        raise RuntimeError("the session opened but no audio came back")
    return f"{audio / 48000:.1f} s of reply audio in {time.monotonic() - t0:.1f} s"


def check_live(cfg: Config) -> Result:
    try:
        detail = asyncio.run(asyncio.wait_for(_live_probe(cfg), 30))
    except Exception as e:
        kind = feedback.classify(e)
        hint = {"credits": "credits are out: top up the account", "rate_limit": "quota or rate limit",
                "auth": "key rejected or no access: check GEMINI_API_KEY", "network": "network problem",
                "server": "Gemini is overloaded: try again"}.get(kind, "see the error")
        return FAIL, f"{type(e).__name__}: {str(e)[:160]} ({hint})"
    tools = [n for n, on in (("Google Search", cfg.google_search), ("go to sleep", cfg.sleep_tool),
                             ("timers", cfg.timer_tool)) if on]
    return PASS, f"{cfg.model} answered: {detail}; tools: {', '.join(tools) or 'none'}"


def run_doctor(cfg: Config, log: Callable[[str], None] = default_log, skip_audio: bool = False,
               skip_api: bool = False, env_path: Path | None = None) -> int:
    """Run all checks. Returns the number of failures."""
    checks: list[tuple[str, Callable[[], Result]]] = [
        (".env file", lambda: check_env_file(env_path or BASE_DIR / ".env")),
        ("API key", lambda: check_key(cfg)),
        ("Wake word (Vosk)", lambda: check_vosk(cfg)),
        ("Spoken messages", lambda: check_espeak(cfg)),
        ("Network", check_network),
    ]
    if not skip_audio:
        checks += [("Audio devices", check_devices), ("Microphone", lambda: check_mic(cfg)),
                   ("Speaker", lambda: check_speaker(cfg))]
    if not skip_api:
        checks.append(("Gemini Live", lambda: check_live(cfg)))

    log("[Status] Checking the assistant...")
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for name, fn in checks:
        try:
            status, message = fn()
        except Exception as e:
            status, message = FAIL, f"{type(e).__name__}: {str(e)[:200]}"
        counts[status] += 1
        log(f"[{status}] {name}: {message}")
    log(f"[Status] {counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failed")
    return counts[FAIL]
