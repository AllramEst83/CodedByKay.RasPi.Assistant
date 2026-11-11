"""Colored, tagged log lines."""
import io
import re

import pytest

from assistant import log as logmod
from assistant.log import RESET, STYLES, colorize, make_logger


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)


def test_every_known_tag_gets_its_own_style():
    assert len(set(STYLES.values())) == len(STYLES)
    for tag, style in STYLES.items():
        assert colorize(f"[{tag}] x") == f"\x1b[{style}m[{tag}] x{RESET}"


def test_unknown_or_missing_tag_is_left_alone():
    assert colorize("[Whatever] x") == "[Whatever] x"
    assert colorize("plain line") == "plain line"


def test_no_color_when_stream_is_not_a_terminal():
    buf = io.StringIO()
    make_logger(buf)("[Error] boom")
    assert buf.getvalue() == "[Error] boom\n"  # no escape codes, no timestamp: a pipe, file or journald


def test_forced_color_adds_style_and_timestamp():
    buf = io.StringIO()
    make_logger(buf, color=True)("[Active] Heard 'hey gemini'")
    out = buf.getvalue()
    assert f"\x1b[{STYLES['Active']}m[Active] Heard 'hey gemini'{RESET}" in out
    assert re.search(r"\d\d:\d\d:\d\d ", out)  # HH:MM:SS prefix


def test_no_color_env_beats_force(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    make_logger(buf, color=True, timestamps=False)("[Error] boom")
    assert buf.getvalue() == "[Error] boom\n"


def test_force_color_env(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    buf = io.StringIO()
    make_logger(buf, timestamps=False)("[Status] hi")
    assert "\x1b[" in buf.getvalue()


def test_default_logger_writes_to_stdout(capsys):
    logmod._default = None
    logmod.log("[Status] hello")
    assert "[Status] hello" in capsys.readouterr().out
