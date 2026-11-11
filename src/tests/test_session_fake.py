"""LiveConversation against a fake Gemini session: no network, no audio devices."""
import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS

from assistant.audio import FakeMic, FakeSpeaker
from assistant.config import Config
from assistant.session import LiveConversation

CFG = Config(gemini_api_key="test", vosk_model_path="unused", idle_timeout=0.4)


def audio_msg(data=b"\x01\x02" * 100):
    return NS(server_content=NS(model_turn=NS(parts=[NS(inline_data=NS(data=data))])))


def sc(**kw):
    return NS(server_content=NS(**kw))


class FakeSession:
    """receive() ends after each turn, like the real SDK; `script` is a list of turns of (delay, msg)."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.tool_responses = []

    async def send_realtime_input(self, **kw):
        self.sent.append(kw)

    async def send_tool_response(self, **kw):
        self.tool_responses.extend(kw["function_responses"])

    async def receive(self):
        if not self.script:
            await asyncio.sleep(3600)
        for delay, msg in self.script.pop(0):
            await asyncio.sleep(delay)
            yield msg


def factory_for(session):
    @asynccontextmanager
    async def connect(model, config):
        yield session

    return lambda: NS(aio=NS(live=NS(connect=connect)))


def run(session, cfg=CFG, speaker=None):
    speaker = speaker or FakeSpeaker()
    conv = LiveConversation(cfg, factory_for(session), FakeMic(b"\x05\x00" * 4000, speed=50), speaker, log=lambda s: None)
    start = time.monotonic()
    reason = asyncio.run(conv.run())
    return reason, conv, speaker, time.monotonic() - start


def test_reply_audio_is_played_and_session_ends_idle():
    session = FakeSession([[(0.0, audio_msg(b"A" * 50)), (0.0, sc(turn_complete=True))]])
    reason, _, speaker, _ = run(session)
    assert reason == "idle"
    assert speaker.audio == b"A" * 50


def test_mic_audio_is_streamed_as_16k_pcm():
    session = FakeSession([])
    run(session)
    assert session.sent, "no mic audio was sent"
    blob = session.sent[0]["audio"]
    assert blob.mime_type == "audio/pcm;rate=16000"
    assert len(blob.data) == 1280


def test_barge_in_clears_playback():
    session = FakeSession([[(0.0, audio_msg()), (0.05, sc(interrupted=True))]])
    _, _, speaker, _ = run(session)
    assert speaker.cleared == 1


def test_follow_up_turns_are_handled_after_first_turn_completes():
    session = FakeSession([
        [(0.0, audio_msg(b"1" * 10)), (0.0, sc(turn_complete=True))],
        [(0.1, audio_msg(b"2" * 10)), (0.0, sc(turn_complete=True))],
    ])
    _, _, speaker, _ = run(session)
    assert speaker.audio == b"1" * 10 + b"2" * 10


def test_user_speech_extends_the_session():
    # silence would end it at ~0.4s; user transcripts at 0.3s and 0.6s keep it alive until ~1.0s
    session = FakeSession([
        [(0.3, sc(input_transcription=NS(text="hej"))), (0.3, sc(input_transcription=NS(text=" där")))]
    ])
    reason, conv, _, elapsed = run(session)
    assert reason == "idle"
    assert elapsed >= 0.9
    assert "".join(conv.transcript["user"]) == "hej där"


def test_idle_timer_waits_while_speaker_is_busy():
    speaker = FakeSpeaker()
    speaker.busy_until = time.monotonic() + 0.8
    session = FakeSession([])
    _, _, _, elapsed = run(session, speaker=speaker)
    assert elapsed >= 1.1  # 0.8 s playing + 0.4 s idle window


def test_server_go_away_ends_the_session():
    session = FakeSession([[(0.0, NS(go_away=NS(time_left="1s")))]])
    reason, *_ = run(session, cfg=replace(CFG, idle_timeout=30))
    assert reason == "go_away"


def test_connection_error_is_reported_not_raised():
    @asynccontextmanager
    async def boom(model, config):
        raise ConnectionError("no network")
        yield

    conv = LiveConversation(CFG, lambda: NS(aio=NS(live=NS(connect=boom))), FakeMic(), FakeSpeaker(), log=lambda s: None)
    reason = asyncio.run(conv.run())
    assert reason.startswith("error: ConnectionError")


def test_turns_are_logged_as_you_and_gemini_lines():
    session = FakeSession([[
        (0.0, sc(input_transcription=NS(text="Hej "))),
        (0.0, sc(input_transcription=NS(text="där"))),
        (0.0, sc(output_transcription=NS(text="Hej på dig!"))),
        (0.0, sc(turn_complete=True)),
    ]])
    lines = []
    conv = LiveConversation(CFG, factory_for(session), FakeMic(b"\x05\x00" * 4000, speed=50), FakeSpeaker(), log=lines.append)
    asyncio.run(conv.run())
    assert "[You] Hej där" in lines and "[Gemini] Hej på dig!" in lines
    assert lines.index("[You] Hej där") < lines.index("[Gemini] Hej på dig!")


def tool_msg(name, id="call-1"):
    return NS(tool_call=NS(function_calls=[NS(id=id, name=name)]))


def test_go_to_sleep_ends_the_session_after_the_goodbye():
    cfg = replace(CFG, idle_timeout=5.0)  # long: only the tool call can end this quickly
    session = FakeSession([[
        (0.0, tool_msg("go_to_sleep")),
        (0.0, audio_msg(b"A" * 50)),
        (0.05, sc(turn_complete=True)),
    ]])
    reason, _, speaker, elapsed = run(session, cfg)
    assert reason == "sleep"
    assert elapsed < 3
    assert session.tool_responses and session.tool_responses[0].name == "go_to_sleep"
    assert b"A" * 50 in speaker.audio  # the goodbye was played


def test_go_to_sleep_never_hangs_if_the_turn_does_not_complete(monkeypatch):
    import assistant.session as s

    monkeypatch.setattr(s, "SLEEP_MAX_WAIT", 0.3)
    cfg = replace(CFG, idle_timeout=5.0)
    reason, _, _, elapsed = run(FakeSession([[(0.0, tool_msg("go_to_sleep"))]]), cfg)
    assert reason == "sleep" and elapsed < 3


def test_unknown_tool_call_gets_an_error_response_and_the_session_continues():
    session = FakeSession([[(0.0, tool_msg("launch_missiles")), (0.0, sc(turn_complete=True))]])
    reason, _, _, _ = run(session)
    assert reason == "idle"
    assert "error" in session.tool_responses[0].response


def test_live_config_declares_the_sleep_tool_only_when_enabled():
    from assistant.session import build_live_config

    on = build_live_config(replace(CFG, sleep_tool=True, google_search=True)).tools
    assert any(t.function_declarations and t.function_declarations[0].name == "go_to_sleep" for t in on)
    assert any(t.google_search for t in on)
    assert build_live_config(replace(CFG, sleep_tool=False, google_search=False, timer_tool=False)).tools is None
