"""Main loop: idle on the wake phrase, then run one live conversation, repeat."""
from __future__ import annotations

import asyncio
from typing import Callable

from .audio import generate_chime
from .config import Config
from .session import LiveConversation


def wait_for_wake(mic, detector, log: Callable[[str], None] = print) -> str:
    detector.reset()
    log("[Idle] Say 'Hey Gemini' or 'Hey Assistant' to start")
    while True:
        hit = detector.feed(mic.read())
        if hit:
            return hit


def run_once(cfg: Config, mic, speaker, detector, client_factory, log: Callable[[str], None] = print) -> LiveConversation:
    """One wake -> chime -> conversation cycle. Returns the finished conversation."""
    phrase = wait_for_wake(mic, detector, log)
    log(f"[Active] Heard '{phrase}'")
    speaker.play_blocking(generate_chime())
    conv = LiveConversation(cfg, client_factory, mic, speaker, log)
    asyncio.run(conv.run())  # a fresh event loop (and client) per conversation
    return conv


def run_forever(cfg: Config, mic, speaker, detector, client_factory, log: Callable[[str], None] = print) -> None:
    try:
        while True:
            run_once(cfg, mic, speaker, detector, client_factory, log)
    except KeyboardInterrupt:
        log("\nStopping.")
