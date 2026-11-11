"""Main loop: idle on the wake phrase, then run one live conversation, repeat."""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from . import feedback
from .audio import generate_chime
from .config import FRAME_MS, Config
from .log import log as default_log
from .session import LiveConversation


def wait_for_wake(mic, detector, log: Callable[[str], None] = default_log) -> str:
    detector.reset()
    log("[Idle] Say 'Hey Gemini' or 'Hey Assistant' to start")
    while True:
        hit = detector.feed(mic.read())
        if hit:
            return hit


def run_once(cfg: Config, mic, speaker, detector, client_factory, log: Callable[[str], None] = default_log) -> LiveConversation:
    """One wake -> chime -> conversation cycle. Returns the finished conversation."""
    phrase = wait_for_wake(mic, detector, log)
    log(f"[Active] Heard '{phrase}'")
    speaker.play_blocking(generate_chime())
    for _ in range(cfg.wake_skip_ms // FRAME_MS):
        mic.read()  # drop the tail of the wake phrase (and any stale buffered audio) so Gemini never hears it
    conv = LiveConversation(cfg, client_factory, mic, speaker, log)
    reason = asyncio.run(conv.run())  # a fresh event loop (and client) per conversation
    if reason.startswith("error") and conv.last_error is not None:
        feedback.announce(feedback.classify(conv.last_error), speaker, cfg.feedback_lang, cfg.error_voice, log)
    return conv


def run_forever(cfg: Config, mic, speaker, detector, client_factory, log: Callable[[str], None] = default_log) -> None:
    try:
        while True:
            try:
                run_once(cfg, mic, speaker, detector, client_factory, log)
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
