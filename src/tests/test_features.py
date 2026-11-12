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


# --- wake detector confirmation -----------------------------------------------------------------------------------

class FakeRecognizer:
    """Scripted Vosk stand-in: each frame is ('partial', text) or ('final', text); FinalResult returns `forced`."""

    def __init__(self, script, forced=None):
        self.script, self.forced, self.i, self.resets = list(script), list(forced or []), -1, 0
        self.current = ("partial", "")

    def AcceptWaveform(self, pcm):
        self.i += 1
        self.current = self.script[self.i] if self.i < len(self.script) else ("partial", "")
        return self.current[0] == "final"

    def PartialResult(self):
        import json
        return json.dumps({"partial": self.current[1]})

    def Result(self):
        import json
        return json.dumps({"text": self.current[1]})

    def FinalResult(self):
        import json
        return json.dumps({"text": self.forced.pop(0) if self.forced else ""})

    def Reset(self):
        self.resets += 1


def detector(script, forced=None, confirm_ms=200):
    from assistant.wake import WakeDetector

    d = WakeDetector.__new__(WakeDetector)  # skip loading a Vosk model
    d._phrases, d._rate, d.confirm_ms = ("hey gemini", "hey assistant"), 16000, confirm_ms
    d._rec, d.last_text, d._pending, d._waited_ms = FakeRecognizer(script, forced), "", False, 0.0
    return d


FRAME = bytes(1280)  # 40 ms


def run_frames(d, n):
    return [d.feed(FRAME) for _ in range(n)]


def test_wake_fires_only_after_the_confirmation_delay():
    d = detector([("partial", ""), ("partial", "hey gemini")], forced=["hey gemini"])
    out = run_frames(d, 12)
    assert out.index("hey gemini") == 1 + 5  # the partial match at frame 1, confirmed 200 ms (5 frames) later
    assert out.count("hey gemini") == 1


def test_wake_is_dropped_when_the_final_result_does_not_confirm_it():
    d = detector([("partial", "[unk] hey assistant")], forced=["[unk]"])
    assert run_frames(d, 6) == [None] * 6  # the match at frame 0, rejected 200 ms later (frame 5)
    assert d.last_text == "[unk]"  # the log shows what the confirming final result heard
    assert run_frames(d, 6) == [None] * 6


def test_detector_keeps_listening_after_a_rejected_match():
    d = detector([("partial", "hey assistant")] + [("partial", "")] * 6 + [("partial", "hey gemini")], forced=["[unk]", "hey gemini"])
    out = run_frames(d, 20)
    assert [x for x in out if x] == ["hey gemini"]


def test_confirm_ms_zero_fires_on_the_first_partial_match():
    d = detector([("partial", "hey gemini")], confirm_ms=0)
    assert d.feed(FRAME) == "hey gemini"


def test_a_final_result_that_matches_fires_at_once():
    d = detector([("final", "[unk] hey assistant")])
    assert d.feed(FRAME) == "hey assistant"


def test_reset_clears_a_pending_confirmation():
    d = detector([("partial", "hey gemini")], forced=["hey gemini"])
    d.feed(FRAME)
    assert d._pending
    d.reset()
    assert not d._pending


def test_a_confirmed_phrase_may_be_followed_by_other_speech():
    for final in ("hey gemini [unk]", "hey gemini [unk] [unk]", "[unk] hey assistant [unk]"):
        d = detector([("partial", "hey gemini")], forced=[final])
        assert [x for x in run_frames(d, 8) if x], final
    d = detector([("partial", "hey gemini")], forced=["[unk] hey [unk]"])  # no full phrase: still rejected
    assert not [x for x in run_frames(d, 8) if x]


# --- echo protection ---------------------------------------------------------------------------------------------

def _frames_sent(cfg, busy_for, mic_seconds=4.0):  # more audio than the session lasts, or the end is silence too
    """Run a session where the speaker is 'busy' for `busy_for` s from the start; return which mic frames were silent."""
    import time as t

    class Speaker(FakeSpeaker):
        def __init__(self):
            super().__init__()
            self.busy_until = t.monotonic() + busy_for

    session = FakeSession([])
    mic = FakeMic(b"\x05\x00" * int(16000 * mic_seconds), speed=1)
    conv = LiveConversation(cfg, factory_for(session), mic, Speaker(), log=lambda s: None)
    asyncio.run(conv.run())
    return [not any(kw["audio"].data) for kw in session.sent]


def test_mic_stays_muted_for_the_tail_after_the_speaker_stops():
    cfg = replace(CFG, idle_timeout=2.0, mute_mic_while_speaking=True, mute_tail_ms=600)
    silent = _frames_sent(cfg, busy_for=0.4)
    assert 0.9 <= silent.index(False) * 0.04 <= 1.2  # 0.4 s of playback + 0.6 s tail, give or take a frame or two
    assert not silent[-1]  # and the mic is open again afterwards


def test_no_muting_at_all_when_it_is_not_enabled():
    cfg = replace(CFG, idle_timeout=2.0, mute_mic_while_speaking=False, mute_tail_ms=600)
    assert not any(_frames_sent(cfg, busy_for=0.5))


def test_zero_tail_unmutes_as_soon_as_the_speaker_stops():
    cfg = replace(CFG, idle_timeout=2.0, mute_mic_while_speaking=True, mute_tail_ms=0)
    assert 0.3 <= _frames_sent(cfg, busy_for=0.4).index(False) * 0.04 <= 0.6


def test_dropped_mic_frames_are_reported():
    class SlowMic:  # produces frames much faster than the (blocked) sender can take them
        def read(self):
            return b"\x01\x00" * 640

    class BlockedSession(FakeSession):
        async def send_realtime_input(self, **kw):
            await asyncio.sleep(5)  # the network is stuck

    lines = []
    conv = LiveConversation(replace(CFG, idle_timeout=0.8), factory_for(BlockedSession([])), SlowMic(), FakeSpeaker(),
                            log=lines.append)
    asyncio.run(conv.run())
    assert any(l.startswith("[Warn] Dropped") for l in lines)


# --- echo gate: interrupting without headphones ---------------------------------------------------------------------

def run_gate(gate, frames, start=0.0, dt=0.04):
    """frames: (mic_rms, playback_rms) per 40 ms frame. Returns the list of allow() results."""
    return [gate.allow(mic, ref, start + i * dt) for i, (mic, ref) in enumerate(frames)]


def test_gate_passes_everything_when_nothing_is_playing():
    from assistant.echo import EchoGate

    assert all(run_gate(EchoGate(), [(30, 0)] * 10 + [(2000, 0)] * 10))


def test_gate_blocks_pure_echo_but_lets_the_user_through():
    from assistant.echo import EchoGate

    gate = EchoGate()
    echo_only = [(900, 3000)] * 20                 # the mic hears 30% of what the speaker plays
    user_over = [(900 + 4000, 3000)] * 15          # ...then the user speaks over it
    out = run_gate(gate, echo_only + user_over)
    assert not any(out[:20])                       # echo (including the calibration frames) is never sent
    assert all(out[20:])                            # the user is
    assert 0.25 < gate.echo_ratio < 0.35


def test_gate_keeps_blocking_echo_when_the_volume_changes():
    from assistant.echo import EchoGate

    gate = EchoGate()
    frames = [(900, 3000)] * 10 + [(1800, 6000)] * 10 + [(450, 1500)] * 10  # same 30% ratio at three volumes
    assert not any(run_gate(gate, frames))


def test_gate_with_headphones_lets_normal_speech_through():
    from assistant.echo import EchoGate

    gate = EchoGate()
    out = run_gate(gate, [(15, 3000)] * 12 + [(1500, 3000)] * 6)  # no echo at all, then speech
    assert not any(out[:5]) and all(out[12:])                      # only the first frames (learning) are lost


def test_gate_ignores_a_loud_room_that_is_not_speech_over_echo():
    from assistant.echo import EchoGate

    gate = EchoGate(speech_rms=500)
    assert not any(run_gate(gate, [(300, 3000)] * 20))  # a mic level below the speech floor is never forwarded


def test_gate_holds_open_for_the_whole_phrase():
    from assistant.echo import EchoGate

    gate = EchoGate(hold_s=0.4)
    out = run_gate(gate, [(900, 3000)] * 10 + [(5000, 3000)] + [(900, 3000)] * 5 + [(900, 3000)] * 10)
    assert out[10] and all(out[11:15])      # kept open for ~0.4 s after the user's last loud frame
    assert not any(out[22:])                # then closed again: it is echo


def test_gate_compensates_for_playback_delay():
    from assistant.echo import EchoGate

    gate = EchoGate(delay_s=0.5)
    # playback starts at frame 0, but the room only hears it (and the mic only picks it up) from frame 13 on
    frames = [(20, 3000)] * 13 + [(900, 3000)] * 20
    out = run_gate(gate, frames)
    assert not any(out[13:])                # delayed echo is recognised as echo, not as the user


def test_gate_stops_gating_after_the_tail():
    from assistant.echo import EchoGate

    gate = EchoGate(tail_s=0.6)
    out = run_gate(gate, [(900, 3000)] * 10 + [(700, 0)] * 30)  # playback ends; leftover reverb, then quiet room
    assert not out[11]                       # still within the tail: not forwarded
    assert all(out[-5:])                     # long after: the mic is open again


def _constant_mic(level, seconds, echo_first=0.6, echo_level=900):
    """Only echo for the first `echo_first` s (the gate learns it), then `level`."""
    from array import array

    pcm = array("h", [echo_level] * int(16000 * echo_first) + [level] * int(16000 * (seconds - echo_first)))
    return FakeMic(pcm.tobytes(), speed=1)


def _gate_session(mic_level, speaker_level=3000, busy_for=2.2, mic_seconds=3.0):
    import time as t

    class Speaker(FakeSpeaker):
        def __init__(self):
            super().__init__()
            self.busy_until = t.monotonic() + busy_for
            self.level_value = speaker_level

    session = FakeSession([])
    cfg = replace(CFG, idle_timeout=2.5, echo_mode="gate", speech_rms=500, mute_tail_ms=600)
    conv = LiveConversation(cfg, factory_for(session), _constant_mic(mic_level, mic_seconds), Speaker(), log=lambda s: None)
    asyncio.run(conv.run())
    return [bool(any(kw["audio"].data)) for kw in session.sent]


def test_session_does_not_forward_the_assistants_own_echo():
    sent = _gate_session(mic_level=900)           # 30% of 3000: just echo
    assert not any(sent[: int(2.0 / 0.04)])       # nothing leaked while the assistant was speaking


def test_session_forwards_the_user_speaking_over_the_assistant():
    sent = _gate_session(mic_level=5000)          # much louder than the echo would be
    during = sent[: int(2.0 / 0.04)]
    assert not any(during[: int(0.5 / 0.04)])     # the echo-only start is not sent
    tail = during[int(0.8 / 0.04):]
    assert sum(tail) > 0.8 * len(tail)            # the user speaking over it is: Gemini can be interrupted


def test_gate_recovers_when_the_user_was_talking_while_it_learned_the_echo():
    from assistant.echo import EchoGate

    gate = EchoGate()
    out = run_gate(gate, [(5000, 3000)] * 10 + [(900, 3000)] * 10 + [(5000 + 900, 3000)] * 6)
    assert gate.echo_ratio is not None and gate.echo_ratio < 0.5   # came down from 1.67 to the real ~0.3
    assert all(out[-6:])                                           # so the user is heard afterwards


# --- chime when the conversation ends -------------------------------------------------------------------------------

def _run_once_until_idle(cfg):
    from assistant.audio import generate_chime  # noqa: F401

    class Detector:
        def reset(self): pass
        def feed(self, frame): return "hey gemini"

    speaker = FakeSpeaker()
    app.run_once(cfg, FakeMic(b"", speed=50), speaker, Detector(), factory_for(FakeSession([])), log=lambda s: None)
    return speaker


def test_falling_chime_plays_when_the_conversation_ends_after_the_idle_timeout():
    from assistant.audio import generate_chime

    speaker = _run_once_until_idle(replace(CFG, greetings={"sv": "", "en": ""}))
    assert speaker.audio.endswith(generate_chime(descending=True))
    assert speaker.audio.startswith(generate_chime())  # the rising wake chime came first


def test_no_idle_chime_when_it_is_turned_off():
    from assistant.audio import generate_chime

    speaker = _run_once_until_idle(replace(CFG, greetings={"sv": "", "en": ""}, idle_chime=False))
    assert generate_chime(descending=True) not in speaker.audio


# --- system prompt: the family --------------------------------------------------------------------------------------

def test_system_prompt_knows_the_family():
    from assistant.config import SYSTEM_PROMPT

    for name in ("Kay", "Rebecka", "Morris", "Milton", "Virgin Wiberg"):
        assert name in SYSTEM_PROMPT
    assert "child" in SYSTEM_PROMPT and "go_to_sleep" in SYSTEM_PROMPT and "set_timer" in SYSTEM_PROMPT


def test_family_context_can_be_replaced_or_removed(tmp_path, monkeypatch):
    from assistant.config import BASE_PROMPT, build_system_prompt, load_config

    assert build_system_prompt("") == BASE_PROMPT
    assert build_system_prompt("  We are the Test family.  ").endswith("We are the Test family.")
    env = tmp_path / ".env"
    env.write_text("")  # nothing in the file: every setting comes from the (monkeypatched) environment, which is undone afterwards
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("FAMILY_CONTEXT", "We are the Test family.")
    cfg = load_config(env)
    assert "Test family" in cfg.system_prompt and "Morris" not in cfg.system_prompt
    monkeypatch.setenv("FAMILY_CONTEXT", "")
    assert load_config(env).system_prompt == BASE_PROMPT
    monkeypatch.delenv("FAMILY_CONTEXT")
    assert "Morris" in load_config(env).system_prompt  # unset: the built-in family


# --- Gemini says the wake greeting ----------------------------------------------------------------------------------

def _wake_once(cfg, session):
    class Detector:
        def reset(self): pass
        def feed(self, frame): return "hey gemini"

    speaker = FakeSpeaker()
    logs = []
    app.run_once(replace(cfg, idle_timeout=0.5), FakeMic(b"\x05\x00" * 4000, speed=50), speaker, Detector(),
                 factory_for(session), log=logs.append)
    return speaker, logs


def test_gemini_greeting_text_follows_the_wake_phrase():
    from assistant import feedback

    cfg = replace(CFG, greeting_map={"hey gemini": "en", "hey assistant": "sv"})
    assert feedback.gemini_greeting_text(cfg, "Hey Gemini") == "Yes?"
    assert feedback.gemini_greeting_text(cfg, "hey assistant") == "Ja?"
    assert feedback.gemini_greeting_text(cfg, "hey something") is None
    assert feedback.gemini_greeting_text(replace(cfg, greeting_source="robot"), "hey gemini") is None
    assert feedback.gemini_greeting_text(replace(cfg, gemini_greetings={"sv": "", "en": ""}), "hey gemini") is None


def test_wake_prompts_gemini_for_the_greeting_and_plays_no_robot_voice():
    session = FakeSession([])
    speaker, _ = _wake_once(CFG, session)
    texts = [kw["text"] for kw in session.sent if "text" in kw]
    assert len(texts) == 1 and texts[0].startswith("[wake]") and '"Yes?"' in texts[0]
    assert not any(c.startswith(b"en:") for c in speaker.chunks)  # no espeak greeting


def test_no_greeting_prompt_when_the_greeting_is_off_or_robot():
    for source in ("off", "robot"):
        session = FakeSession([])
        _wake_once(replace(CFG, greeting_source=source), session)
        assert not [kw for kw in session.sent if "text" in kw]


def test_a_failed_greeting_prompt_does_not_end_the_conversation():
    class Session(FakeSession):
        async def send_realtime_input(self, **kw):
            if "text" in kw:
                raise RuntimeError("text input refused")
            await super().send_realtime_input(**kw)

    session = Session([])
    _, logs = _wake_once(CFG, session)
    assert any("Could not ask Gemini for the greeting" in line for line in logs)
    assert session.sent  # the microphone audio still flowed


def test_mic_stays_open_until_the_gemini_greeting_starts_and_is_muted_while_it_plays():
    import time

    from types import SimpleNamespace as NS

    class Speaker(FakeSpeaker):
        def write(self, pcm):
            super().write(pcm)
            self.busy_until = time.monotonic() + 0.6

    audio = NS(inline_data=NS(data=b"\x01\x00" * 100))
    msg = NS(server_content=NS(model_turn=NS(parts=[audio]), turn_complete=True, interrupted=False,
                               input_transcription=None, output_transcription=None), tool_call=None, go_away=None)
    session = FakeSession([[(0.4, msg)]])  # the greeting audio arrives 0.4 s after connecting
    speaker = Speaker()

    class Detector:
        def reset(self): pass
        def feed(self, frame): return "hey gemini"

    app.run_once(replace(CFG, idle_timeout=1.5, mute_tail_ms=0), FakeMic(b"\x05\x00" * 100000, speed=1), speaker,
                 Detector(), factory_for(session), log=lambda s: None)
    silent = [not any(kw["audio"].data) for kw in session.sent if "audio" in kw]
    assert not silent[0]  # open while waiting for the greeting to arrive
    assert any(silent)  # muted while the greeting played
    assert not silent[-1]  # open again afterwards


# --- language rule and the ready chime --------------------------------------------------------------------------------

def test_prompt_limits_the_assistant_to_swedish_and_english():
    from assistant.config import SYSTEM_PROMPT

    assert "only two languages: Swedish and English" in SYSTEM_PROMPT
    assert "Never reply in any other language" in SYSTEM_PROMPT
    assert "Jag förstår bara svenska och engelska" in SYSTEM_PROMPT and "I only understand Swedish and English" in SYSTEM_PROMPT
    assert "assume Swedish or English" in SYSTEM_PROMPT  # imperfect recognition must not make it nag in Swedish
    assert "Morris" in SYSTEM_PROMPT  # the family context is still there


def test_ready_chime_is_distinct_from_the_wake_and_sleep_chimes():
    from assistant.audio import generate_chime, generate_ready_chime

    ready = generate_ready_chime()
    assert len(ready) % 2 == 0 and len(ready) > len(generate_chime()) * 2
    assert ready not in (generate_chime(), generate_chime(descending=True))
    assert max(abs(int.from_bytes(ready[i:i + 2], "little", signed=True)) for i in range(0, len(ready), 2)) < 32000  # no clipping


def _forever_until_stopped(cfg, speaker):
    class Mic:
        def read(self):
            raise KeyboardInterrupt

    class Detector:
        def reset(self): pass
        def feed(self, frame): return None

    logs = []
    app.run_forever(cfg, Mic(), speaker, Detector(), lambda: None, logs.append)  # Mic.read() ends the loop at once
    return logs


def test_ready_chime_plays_once_at_startup():
    from assistant.audio import generate_ready_chime

    speaker = FakeSpeaker()
    logs = _forever_until_stopped(CFG, speaker)
    assert speaker.audio.startswith(generate_ready_chime()) and "[Status] Ready" in logs


def test_no_ready_chime_when_turned_off_and_a_broken_speaker_does_not_stop_startup():
    speaker = FakeSpeaker()
    _forever_until_stopped(replace(CFG, ready_chime=False), speaker)
    assert speaker.audio == b""

    class Broken(FakeSpeaker):
        def play_blocking(self, pcm):
            raise OSError("no speaker")

    logs = _forever_until_stopped(CFG, Broken())
    assert any("Could not play the ready chime" in line for line in logs)
