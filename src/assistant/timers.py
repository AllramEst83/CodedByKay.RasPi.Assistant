"""Voice timers. Gemini sets them through tools; they live in the app, so they outlive the conversation that made them.

When a timer ends, an alarm tone plays three times, followed by a spoken message (robot voice, needs espeak-ng).
Timers are kept in memory only: a restart or reboot forgets them.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from . import feedback
from .config import Config
from .log import log as default_log

MAX_TIMERS = 10
MAX_SECONDS = 24 * 3600
SET_TIMER, CANCEL_TIMERS, LIST_TIMERS = "set_timer", "cancel_timers", "list_timers"


class TimerManager:
    def __init__(self, speaker, cfg: Config, log: Callable[[str], None] = default_log,
                 clock: Callable[[], float] = time.monotonic):
        self._speaker, self._cfg, self._log, self._clock = speaker, cfg, log, clock
        self._lock = threading.Lock()
        self._timers: dict[int, dict] = {}
        self._next_id = 1

    def set(self, seconds, label: str = "") -> dict:
        """Start a timer. Returns a dict for the tool response: {"result": ...} or {"error": ...}."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return {"error": "seconds must be a number"}
        if not 0 < seconds <= MAX_SECONDS:
            return {"error": f"seconds must be between 1 and {MAX_SECONDS}"}
        label = (label or "").strip()
        with self._lock:
            if len(self._timers) >= MAX_TIMERS:
                return {"error": f"at most {MAX_TIMERS} timers at once"}
            timer_id = self._next_id
            self._next_id += 1
            t = threading.Timer(seconds, self._fire, args=(timer_id,))
            t.daemon = True
            self._timers[timer_id] = {"label": label, "due": self._clock() + seconds, "thread": t}
            t.start()
        self._log(f"[Timer] #{timer_id} set for {_fmt(seconds)}" + (f" ({label})" if label else ""))
        return {"result": f"timer {timer_id} set for {int(seconds)} seconds", "id": timer_id}

    def cancel_all(self) -> int:
        with self._lock:
            pending, self._timers = self._timers, {}
        for t in pending.values():
            t["thread"].cancel()
        if pending:
            self._log(f"[Timer] cancelled {len(pending)} timer(s)")
        return len(pending)

    def list(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            return [{"id": i, "label": t["label"], "seconds_left": max(0, round(t["due"] - now))}
                    for i, t in sorted(self._timers.items())]

    def _fire(self, timer_id: int) -> None:
        with self._lock:
            t = self._timers.pop(timer_id, None)
        if t is None:  # cancelled just as it fired
            return
        self._log(f"[Timer] #{timer_id} done" + (f" ({t['label']})" if t["label"] else ""))
        self._ring(t["label"])

    def _ring(self, label: str) -> None:
        tone = feedback.tone_pattern("timer")
        for _ in range(3):
            self._speaker.play_blocking(tone)
            time.sleep(0.35)
        lang = self._cfg.feedback_lang
        sv = lang == "sv"
        text = ((f"Timern {label} är klar." if label else "Timern är klar.") if sv
                else (f"Timer {label} done." if label else "Timer done."))
        if self._cfg.error_voice:
            speech = feedback.speak(text, lang)
            if speech:
                self._speaker.play_blocking(speech)


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return " ".join(p for p in (f"{h} h" if h else "", f"{m} min" if m else "", f"{s} s" if s or not (h or m) else "") if p)
