"""The mic and speaker survive being unplugged: no spinning on a dead handle, automatic reconnect. A fake PortAudio."""
import os
import time
from types import SimpleNamespace

import pytest

from assistant import audio, doctor
from assistant.config import INPUT_FRAME_BYTES, Config


class FakeStream:
    def __init__(self, on_read=None, on_write=None):
        self.on_read, self.on_write = on_read, on_write
        self.written = []
        self.closed = False

    def read(self, frames, exception_on_overflow=False):
        return self.on_read(frames)

    def write(self, data):
        if self.on_write:
            self.on_write(len(self.written))
        self.written.append(data)

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


def install_fake_audio(monkeypatch, streams):
    """PyAudio.open() hands out `streams` in order (an Exception instance is raised instead)."""
    state = SimpleNamespace(opened=0, streams=list(streams))

    class FakePA:
        def get_device_count(self):
            return 1

        def get_device_info_by_index(self, i):
            return {"name": "USB Test Device: Audio (hw:9,0)", "maxInputChannels": 1, "maxOutputChannels": 2,
                    "defaultSampleRate": 16000.0}

        def get_default_input_device_info(self):
            return self.get_device_info_by_index(0)

        get_default_output_device_info = get_default_input_device_info

        def open(self, **kwargs):
            state.opened += 1
            item = state.streams.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def terminate(self):
            pass

    monkeypatch.setattr(audio, "pyaudio_mod", lambda: SimpleNamespace(PyAudio=FakePA))
    return state


def healthy(frames):
    time.sleep(0.04)  # a real stream blocks for the length of the frame
    return bytes(frames * 2)


def test_mic_reopens_after_a_read_error(monkeypatch):
    calls = []

    def flaky(frames):
        calls.append(1)
        if len(calls) >= 3:
            raise OSError("Unanswered call to Pa_ReadStream (-9985)")
        return healthy(frames)

    state = install_fake_audio(monkeypatch, [FakeStream(flaky), FakeStream(healthy)])
    log = []
    mic = audio.PyAudioMic(log=log.append)
    frames = [mic.read() for _ in range(40)]
    assert all(len(f) == INPUT_FRAME_BYTES for f in frames)  # the caller always gets a proper frame
    assert state.opened == 2
    assert any("Microphone lost" in l for l in log) and any("Microphone reconnected" in l for l in log)


def test_mic_notices_a_dead_handle_that_returns_instantly(monkeypatch):
    dead = FakeStream(lambda frames: bytes(frames * 2))  # no blocking at all: the signature of an unplugged device
    state = install_fake_audio(monkeypatch, [dead, FakeStream(healthy)])
    log = []
    mic = audio.PyAudioMic(log=log.append)
    t0 = time.monotonic()
    for _ in range(90):
        mic.read()
    assert any("faster than real time" in l for l in log)
    assert state.opened == 2
    assert time.monotonic() - t0 > 1.0  # and it no longer spins: it waits at the real-time pace while the device is gone


def test_mic_recovers_when_the_stream_stalls_and_never_returns(monkeypatch):
    """A USB re-enumeration leaves a blocking read hanging forever: no error, no frames."""
    import threading

    block = threading.Event()
    stalled = FakeStream(on_read=lambda frames: block.wait(30) or bytes(frames * 2))
    state = install_fake_audio(monkeypatch, [stalled, FakeStream(on_read=healthy)])
    monkeypatch.setattr(audio.PyAudioMic, "STALL_SECONDS", 0.3)
    logs = []
    mic = audio.PyAudioMic(log=logs.append)
    start = time.monotonic()
    frames = []
    while time.monotonic() - start < 3 and not any("reconnected" in line for line in logs):
        frames.append(mic.read())
    block.set()  # let the abandoned reader thread end
    assert any("Microphone lost" in line and "no audio" in line for line in logs)
    assert any("reconnected" in line for line in logs) and state.opened == 2
    assert all(len(f) == INPUT_FRAME_BYTES for f in frames)
    assert len(mic.read()) == INPUT_FRAME_BYTES


def test_mic_keeps_retrying_while_the_device_is_missing(monkeypatch):
    broken = FakeStream(lambda frames: (_ for _ in ()).throw(OSError("gone")))
    state = install_fake_audio(monkeypatch, [broken] + [OSError("no device")] * 8 + [FakeStream(healthy)])
    log = []
    mic = audio.PyAudioMic(log=log.append)
    for _ in range(60):  # about 2.5 s: the first retry fails (all rates x mono/stereo), the next one succeeds
        assert len(mic.read()) == INPUT_FRAME_BYTES
    assert any("not available yet" in l for l in log)
    assert any("reconnected" in l for l in log)


def test_a_healthy_mic_is_left_alone(monkeypatch):
    state = install_fake_audio(monkeypatch, [FakeStream(healthy)])
    log = []
    mic = audio.PyAudioMic(log=log.append)
    for _ in range(60):  # several pacing windows
        mic.read()
    assert state.opened == 1 and log == []


def test_speaker_reopens_when_it_is_unplugged_mid_sentence(monkeypatch):
    def fail_on_third(n):
        if n == 2:
            raise OSError("Output underflowed / device gone")

    first, second = FakeStream(on_write=fail_on_third), FakeStream()
    state = install_fake_audio(monkeypatch, [first, second])
    log = []
    speaker = audio.PyAudioSpeaker(log=log.append)
    speaker.write(bytes(24000))  # 0.25 s: several 50 ms pieces
    deadline = time.monotonic() + 3
    while speaker.is_busy() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not speaker.is_busy()  # is_busy() did not get stuck on the lost device
    assert any("Speaker lost" in l for l in log) and any("Speaker reconnected" in l for l in log)
    speaker.write(bytes(4800))  # and the new device works
    time.sleep(0.4)
    assert second.written
    speaker.close()


def test_speaker_gives_up_cleanly_when_the_device_never_returns(monkeypatch):
    def always_fail(n):
        raise OSError("gone")

    install_fake_audio(monkeypatch, [FakeStream(on_write=always_fail)] + [OSError("no device")] * 40)
    monkeypatch.setattr(audio.PyAudioSpeaker, "REOPEN_SECONDS", 0.3)
    speaker = audio.PyAudioSpeaker(log=lambda s: None)
    speaker.write(bytes(9600))
    deadline = time.monotonic() + 6
    while speaker.is_busy() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not speaker.is_busy()
    speaker.close()


def test_quiet_stderr_hides_c_library_noise_and_restores_it(capfd):
    with audio.quiet_stderr():
        os.write(2, b"ALSA lib pcm.c: Unknown PCM cards.pcm.rear\n")
    assert capfd.readouterr().err == ""
    os.write(2, b"visible again\n")
    assert "visible again" in capfd.readouterr().err


def test_stdio_speaker_does_not_try_to_reopen_a_device():
    class BrokenPipe:
        def write(self, data):
            raise BrokenPipeError

        def flush(self):
            pass

    speaker = audio.StdioSpeaker(out=BrokenPipe())
    speaker.write(bytes(4800))
    time.sleep(0.5)
    assert not speaker.is_busy()  # the pipe broke: the queue was dropped, nothing hangs
    speaker.close()


def test_doctor_warns_when_the_service_holds_the_audio_devices(monkeypatch):
    def fake_run(state):
        return lambda *a, **k: SimpleNamespace(stdout=state + "\n")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run("active"))
    status, message = doctor.check_service()
    assert status == doctor.WARN and "sudo systemctl stop gemini-assistant" in message

    monkeypatch.setattr(doctor.subprocess, "run", fake_run("inactive"))
    assert doctor.check_service()[0] == doctor.PASS

    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(doctor.subprocess, "run", missing)
    assert doctor.check_service()[0] == doctor.PASS
