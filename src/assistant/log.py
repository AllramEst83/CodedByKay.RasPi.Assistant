"""Tagged, colored log lines: "[Tag] message" is colored by its tag so events stand out at a glance.

Color is on when the stream is a terminal, or when forced (the PC bridge, where the log travels over SSH).
It is off in files, pipes and journald (where raw escape codes would show as noise) and whenever NO_COLOR is set;
FORCE_COLOR=1 turns it on anyway.
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Callable

RESET = "\x1b[0m"
DIM = "\x1b[2m"

# tag -> ANSI SGR style
STYLES: dict[str, str] = {
    "Idle": "2",              # dim: waiting for the wake phrase
    "Active": "1;32",         # bold green: wake phrase heard
    "Status": "36",           # cyan: session and system state
    "You": "1;94",            # bold blue: what you said
    "Gemini": "1;95",         # bold magenta: what Gemini said
    "Interrupted": "1;33",    # bold yellow: barge-in
    "Timeout": "33",          # yellow: idle timeout
    "Feedback": "1;30;43",    # black on yellow: an error was announced out loud
    "Error": "1;97;41",       # white on red: something failed
    "Timer": "1;36",          # bold cyan: a voice timer was set, cancelled or went off
    "Pass": "32",             # green: a --doctor check passed
    "Warn": "93",             # bright yellow: a --doctor check passed with a caveat
    "Fail": "91",             # bright red: a --doctor check failed
}
_TAG = re.compile(r"^\s*\[(\w+)\]")


def colorize(line: str) -> str:
    """Wrap a whole line in the style of its leading [Tag]; lines without a known tag are left alone."""
    m = _TAG.match(line)
    style = STYLES.get(m.group(1)) if m else None
    return f"\x1b[{style}m{line}{RESET}" if style else line


def use_color(stream, force: bool | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if force is not None:
        return force
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def make_logger(stream=None, color: bool | None = None, timestamps: bool | None = None) -> Callable[[str], None]:
    """A log(line) function writing to `stream` (default stdout). Timestamps follow color unless set."""
    stream = stream or sys.stdout
    on = use_color(stream, color)
    stamp = on if timestamps is None else timestamps

    def log(line: str) -> None:
        text = str(line)
        prefix = time.strftime("%H:%M:%S ") if stamp else ""
        if on:
            prefix = f"{DIM}{prefix}{RESET}" if prefix else ""
            text = colorize(text)
        print(prefix + text, file=stream, flush=True)

    return log


_default: Callable[[str], None] | None = None


def log(line: str) -> None:
    """Default logger: stdout, colored when it is a terminal."""
    global _default
    if _default is None:
        _default = make_logger()
    _default(line)
