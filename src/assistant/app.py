"""Main loop: idle on the wake phrase, then run one live conversation, repeat."""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from . import feedback
from .audio import generate_chime, generate_ready_chime
from .config import FRAME_MS, Config
from .log import log as default_log
from .session import LiveConversation
from .timers import TimerManager


def wait_for_wake(mic, detector, log: Callable[[str], None] = default_log) -> str:
    detector.reset()
    log("[Idle] Say 'Hey Gemini' or 'Hey Assistant' to start")
    while True:
        hit = detector.feed(mic.read())
        if hit:
            return hit


def wake_test(mic, detector, log: Callable[[str], None] = default_log, minutes: float | None = None,
              clock: Callable[[], float] = time.monotonic, report_every: float = 300.0) -> int:
    """Listen for the wake phrases only: no Gemini, no speaker, no cost. Logs each trigger and returns the count.

    Leave it running while normal talk or the TV is on, to measure false triggers. Ends on Ctrl+C, end of audio,
    or after `minutes`.
    """
    start = last_report = clock()
    count = 0
    detector.reset()
    log("[Status] Wake test: listening for the wake phrases only (no Gemini calls). Ctrl+C to stop.")
    try:
        while minutes is None or clock() - start < minutes * 60:
            hit = detector.feed(mic.read())
            if hit:
                count += 1
                log(f"[Active] #{count} heard '{hit}' at {_mmss(clock() - start)}{_heard_as(detector, hit)}")
                detector.reset()
            if clock() - last_report >= report_every:
                last_report = clock()
                log(f"[Idle] {count} trigger(s) so far in {_mmss(clock() - start)}")
    except (KeyboardInterrupt, EOFError):
        pass
    elapsed = max(clock() - start, 1e-9)
    log(f"[Status] Wake test done: {count} trigger(s) in {elapsed / 60:.1f} min ({count / (elapsed / 60):.2f} per minute)")
    return count


def _heard_as(detector, phrase: str) -> str:
    """' (heard as "[unk] hey assistant")' when the recognizer's text was more than the bare phrase."""
    text = getattr(detector, "last_text", "")
    return f" (heard as \"{text}\")" if text and text != phrase else ""


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def run_once(cfg: Config, mic, speaker, detector, client_factory, log: Callable[[str], None] = default_log,
             timers=None) -> LiveConversation:
    """One wake -> chime -> conversation cycle. Returns the finished conversation."""
    phrase = wait_for_wake(mic, detector, log)
    log(f"[Active] Heard '{phrase}'{_heard_as(detector, phrase)}")
    speaker.play_blocking(generate_chime())
    greeting = feedback.greeting_audio(cfg, phrase) if cfg.greeting_source == "robot" else None
    if greeting:
        speaker.write(greeting)  # not blocking: it plays while the Live session connects
    for _ in range(cfg.wake_skip_ms // FRAME_MS):
        mic.read()  # drop the tail of the wake phrase (and any stale buffered audio) so Gemini never hears it
    conv = LiveConversation(cfg, client_factory, mic, speaker, log, timers=timers)
    conv.mute_mic_during_greeting = bool(greeting)  # the greeting must not echo into the session
    conv.greeting = feedback.gemini_greeting_text(cfg, phrase)  # or Gemini says it itself, once connected
    reason = asyncio.run(conv.run())  # a fresh event loop (and client) per conversation
    if reason == "sleep":
        speaker.play_blocking(generate_chime(descending=True))
        log("[Status] Going to sleep")
    elif reason in ("idle", "go_away") and cfg.idle_chime:
        speaker.play_blocking(generate_chime(descending=True))  # the same falling chime: the conversation is over
    if reason.startswith("error") and conv.last_error is not None:
        feedback.announce(feedback.classify(conv.last_error), speaker, cfg.feedback_lang, cfg.error_voice, log)
    return conv


def run_forever(cfg: Config, mic, speaker, detector, client_factory, log: Callable[[str], None] = default_log) -> None:
    feedback.prewarm_greetings(cfg)
    timers = TimerManager(speaker, cfg, log)  # shared by all conversations: a timer outlives the chat that set it
    if cfg.ready_chime:
        try:
            speaker.play_blocking(generate_ready_chime())  # once per start: "I am up and listening"
            log("[Status] Ready")
        except Exception as e:  # noqa: BLE001  a missing speaker must not stop the assistant from listening
            log(f"[Warn] Could not play the ready chime: {type(e).__name__}: {e}")
    try:
        while True:
            try:
                run_once(cfg, mic, speaker, detector, client_factory, log, timers)
            except EOFError:
                raise  # audio input closed (remote-audio bridge disconnected): stop instead of retrying
            except Exception as e:  # keep the assistant alive; tell the user instead of dying silently
                log(f"[Error] {type(e).__name__}: {e}")
                try:
                    feedback.announce(feedback.classify(e), speaker, cfg.feedback_lang, cfg.error_voice, log)
                except Exception:
                    pass  # the speaker itself may be what failed
                time.sleep(3)  # avoid a tight error loop, e.g. an unplugged mic
    except KeyboardInterrupt:
        log("[Status] Stopping")
