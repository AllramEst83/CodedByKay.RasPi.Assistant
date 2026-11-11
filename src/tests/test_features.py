"""USB device choice, wake-only test mode, voice timers (and their Gemini tools), and --doctor. No network, no audio."""
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from assistant import app, doctor, feedback
from assistant.audio import FakeMic, FakeSpeaker, _choose_device, pick_usb_device
from assistant.config import Config
from assistant.session import LiveConversation, build_live_config
from assistant.timers import MAX_TIMERS, TimerManager
from test_session_fake import FakeSession, factory_for, sc

CFG = Config(gemini_api_key="test", vosk_model_path="unused", idle_timeout=0.4, error_voice=False)

# --- USB device choice ---------------------------------------------------------------------------------------------

DEVICES = [  # (index, name, max_in, max_out) as PortAudio lists them on the Pi
    (0, "vc4-hdmi-0: MAI PCM i2s-hifi-0 (hw:0,0)", 0, 2),
    (1, "vc4-hdmi-1: MAI PCM i2s-hifi-0 (hw:1,0)", 0, 2),
    (2, "USB PnP Sound Device: USB Audio (hw:2,0)", 1, 0),
    (3, "Wireless Audio: USB Audio (hw:3,0)", 0, 2),
    (4, "sysdefault", 0, 128),
    (5, "default", 128, 128),
]


def test_pick_usb_prefers_usb_and_skips_hdmi_and_defaults():
    assert pick_usb_device(DEVICES, want_input=True) == 2
    assert pick_usb_device(DEVICES, want_input=False) == 3


def test_pick_usb_is_none_without_usb_devices():
    assert pick_usb_device([d for d in DEVICES if "USB" not in d[1]], want_input=False) is None


class FakePA:
    def __init__(self, devices=DEVICES):
        self.devices = devices

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, i):
        d = self.devices[i]
        return {"name": d[1], "maxInputChannels": d[2], "maxOutputChannels": d[3]}

    def get_default_input_device_info(self):
        return {"name": "default"}

    def get_default_output_device_info(self):
        return {"name": "vc4-hdmi-0"}


def test_choose_device_auto_names_the_usb_device():
    idx, name = _choose_device(FakePA(), None, want_input=False)
    assert idx == 3 and "Wireless Audio" in name


def test_choose_device_by_index_by_name_and_default():
    assert _choose_device(FakePA(), "1", False)[0] == 1
    assert _choose_device(FakePA(), "usb pnp", True)[0] == 2
    idx, name = _choose_device(FakePA(), "default", False)
    assert idx is None and "system default" in name


def test_choose_device_unknown_name_raises():
    with pytest.raises(RuntimeError, match="No output audio device"):
        _choose_device(FakePA(), "nonexistent", False)


def test_choose_device_falls_back_to_system_default_without_usb():
    idx, name = _choose_device(FakePA([d for d in DEVICES if "USB" not in d[1]]), None, False)
    assert idx is None and "system default" in name


# --- wake-only test mode -------------------------------------------------------------------------------------------

class ScriptedDetector:
    """Triggers on the given (1-based) frame numbers."""

    def __init__(self, hits):
        self.hits, self.n, self.resets = dict(hits), 0, 0

    def reset(self):
        self.resets += 1

    def feed(self, frame):
        self.n += 1
        return self.hits.get(self.n)


def test_wake_test_counts_triggers_and_stops_after_the_time_limit():
    ticks = iter(range(10_000))
    lines = []
    n = app.wake_test(FakeMic(b"", speed=1000), ScriptedDetector({5: "hey gemini", 9: "hey assistant"}),
                      lines.append, minutes=0.5, clock=lambda: float(next(ticks)))  # 30 "seconds" = 30 clock ticks
    assert n == 2
    assert any("#1" in l and "hey gemini" in l for l in lines)
    assert any("#2" in l and "hey assistant" in l for l in lines)
    assert "Wake test done: 2 trigger(s)" in lines[-1]


def test_wake_test_ends_cleanly_when_the_audio_stream_closes():
    class Mic:
        def read(self):
            raise EOFError

    lines = []
    assert app.wake_test(Mic(), ScriptedDetector({}), lines.append) == 0
    assert "Wake test done: 0 trigger(s)" in lines[-1]


# --- voice timers ----------------------------------------------------------------------------------------------------

def test_timer_rings_three_times_when_it_ends():
    speaker, lines = FakeSpeaker(), []
    tm = TimerManager(speaker, CFG, lines.append)
    assert tm.set(0.05, "pasta")["result"].startswith("timer 1 set")
    assert tm.list()[0]["label"] == "pasta"
    time.sleep(1.5)
    assert tm.list() == []
    assert speaker.audio == feedback.tone_pattern("timer") * 3  # error_voice is off: tones only
    assert any("#1 done" in l and "pasta" in l for l in lines)


def test_timer_message_is_spoken_when_voice_is_on(monkeypatch):
    spoken = []
    monkeypatch.setattr(feedback, "speak", lambda text, lang="sv", rate=24000, speed=150: spoken.append(text) or b"S")
    tm = TimerManager(FakeSpeaker(), replace(CFG, error_voice=True, feedback_lang="sv"), lambda s: None)
    tm.set(0.05, "pasta")
    time.sleep(1.5)
    assert spoken == ["Timern pasta är klar."]


def test_cancel_all_stops_pending_timers():
    speaker = FakeSpeaker()
    tm = TimerManager(speaker, CFG, lambda s: None)
    tm.set(0.3)
    tm.set(0.3)
    assert tm.cancel_all() == 2
    time.sleep(0.8)
    assert speaker.audio == b"" and tm.list() == []


def test_timer_validation():
    tm = TimerManager(FakeSpeaker(), CFG, lambda s: None)
    assert "error" in tm.set("soon")
    assert "error" in tm.set(0)
    assert "error" in tm.set(-5)
    assert "error" in tm.set(10 ** 9)
    for _ in range(MAX_TIMERS):
        assert "result" in tm.set(60)
    assert "error" in tm.set(60)
    tm.cancel_all()


def test_seconds_left_counts_down():
    now = [100.0]
    tm = TimerManager(FakeSpeaker(), CFG, lambda s: None, clock=lambda: now[0])
    tm.set(90)
    now[0] = 130.0
    assert tm.list()[0]["seconds_left"] == 60
    tm.cancel_all()


# --- timers through Gemini's tools ---------------------------------------------------------------------------------

def tool_call(name, args=None, id="c1"):
    return NS(tool_call=NS(function_calls=[NS(id=id, name=name, args=args or {})]))


def run_session(session, timers):
    conv = LiveConversation(CFG, factory_for(session), FakeMic(b"\x05\x00" * 4000, speed=50), FakeSpeaker(),
                            log=lambda s: None, timers=timers)
    return asyncio.run(conv.run())


def test_gemini_sets_lists_and_cancels_timers():
    tm = TimerManager(FakeSpeaker(), CFG, lambda s: None)
    session = FakeSession([[
        (0.0, tool_call("set_timer", {"seconds": 300, "label": "ägg"}, "a")),
        (0.0, tool_call("list_timers", None, "b")),
        (0.0, tool_call("cancel_timers", None, "c")),
        (0.0, sc(turn_complete=True)),
    ]])
    assert run_session(session, tm) == "idle"
    by_id = {r.id: r.response for r in session.tool_responses}
    assert by_id["a"]["result"].startswith("timer 1 set for 300")
    assert by_id["b"]["timers"][0]["label"] == "ägg"
    assert by_id["c"]["result"] == "cancelled 1 timer(s)"
    assert tm.list() == []


def test_timer_tool_without_a_manager_returns_an_error_not_a_crash():
    session = FakeSession([[(0.0, tool_call("set_timer", {"seconds": 5})), (0.0, sc(turn_complete=True))]])
    assert run_session(session, None) == "idle"
    assert "error" in session.tool_responses[0].response


def test_live_config_declares_the_timer_tools_only_when_enabled():
    def names(cfg):
        return {d.name for t in (build_live_config(cfg).tools or []) for d in (t.function_declarations or [])}

    assert {"set_timer", "cancel_timers", "list_timers", "go_to_sleep"} <= names(CFG)
    assert "set_timer" not in names(replace(CFG, timer_tool=False))
    set_timer = next(d for t in build_live_config(CFG).tools for d in (t.function_declarations or [])
                     if d.name == "set_timer")
    assert "seconds" in set_timer.parameters.properties and set_timer.parameters.required == ["seconds"]


# --- doctor ------------------------------------------------------------------------------------------------------

def test_env_file_check(tmp_path):
    env = tmp_path / ".env"
    assert doctor.check_env_file(env)[0] == doctor.FAIL
    env.write_text("GEMINI_API_KEY=x\n")
    env.chmod(0o644)
    assert doctor.check_env_file(env)[0] == doctor.WARN
    env.chmod(0o600)
    assert doctor.check_env_file(env)[0] == doctor.PASS


def test_doctor_never_prints_the_key():
    status, message = doctor.check_key(replace(CFG, gemini_api_key="AQ.SECRET-VALUE"))
    assert status == doctor.PASS and "SECRET" not in message and "15 characters" in message


def test_run_doctor_reports_each_check_and_counts_failures(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=x\n")
    env.chmod(0o600)
    monkeypatch.setattr(doctor, "check_vosk", lambda cfg: (doctor.PASS, "ok"))
    monkeypatch.setattr(doctor, "check_espeak", lambda cfg: (doctor.WARN, "missing"))
    monkeypatch.setattr(doctor, "check_network", lambda: (_ for _ in ()).throw(OSError("no route")))  # a crashing check
    lines = []
    failures = doctor.run_doctor(CFG, lines.append, skip_audio=True, skip_api=True, env_path=env)
    assert failures == 1
    text = "\n".join(lines)
    assert "[Pass] .env file" in text and "[Pass] Wake word (Vosk): ok" in text
    assert "[Warn] Spoken messages: missing" in text
    assert "[Fail] Network: OSError: no route" in text
    assert "2 passed" not in text and "3 passed, 1 warning(s), 1 failed" in lines[-1]
