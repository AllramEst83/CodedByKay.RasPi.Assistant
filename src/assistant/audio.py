"""Audio I/O behind small interfaces so real and simulated devices are swappable.

Mic:     read() -> one frame of 16 kHz mono 16-bit PCM (blocking)
Speaker: write(pcm), clear() (barge-in), is_busy(), play_blocking(pcm), close()
"""
from __future__ import annotations

import math
import queue
import struct
import threading
import time
import wave

from .config import FRAME_MS, INPUT_FRAME_BYTES, INPUT_RATE, OUTPUT_RATE


def generate_chime(rate: int = OUTPUT_RATE) -> bytes:
    """Soft two-tone chime (C5 then E5), no audio files needed."""
    out = bytearray()
    for freq, dur in ((523.25, 0.08), (659.25, 0.12)):
        n = int(rate * dur)
        for i in range(n):
            env = math.sin(math.pi * i / n)
            out += struct.pack("<h", int(32767 * 0.25 * env * math.sin(2 * math.pi * freq * i / rate)))
    return bytes(out)


def read_wav_pcm16(path, expect_rate: int) -> bytes:
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, expect_rate):
            raise ValueError(f"{path}: need mono 16-bit {expect_rate} Hz, got "
                             f"{w.getnchannels()}ch/{w.getsampwidth() * 8}bit/{w.getframerate()}Hz")
        return w.readframes(w.getnframes())


def write_wav_pcm16(path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _find_device(pa, spec: str | None, want_input: bool) -> int | None:
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    key = "maxInputChannels" if want_input else "maxOutputChannels"
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info[key] > 0 and spec.lower() in info["name"].lower():
            return i
    raise RuntimeError(f"No {'input' if want_input else 'output'} audio device matching {spec!r}")


def list_devices() -> str:
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        lines = []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            lines.append(f"{i}: {d['name']} (in={d['maxInputChannels']}, out={d['maxOutputChannels']})")
        return "\n".join(lines) or "(no audio devices found)"
    finally:
        pa.terminate()


class PyAudioMic:
    def __init__(self, device: str | None = None):
        import pyaudio

        self._pa = pyaudio.PyAudio()
        self._chunk = INPUT_FRAME_BYTES // 2
        self._stream = self._pa.open(
            format=pyaudio.paInt16, channels=1, rate=INPUT_RATE, input=True,
            input_device_index=_find_device(self._pa, device, True),
            frames_per_buffer=self._chunk,
        )

    def read(self) -> bytes:
        return self._stream.read(self._chunk, exception_on_overflow=False)

    def close(self) -> None:
        self._stream.stop_stream()
        self._stream.close()
        self._pa.terminate()


class PyAudioSpeaker:
    """Plays queued PCM in 50 ms pieces so clear() can cut playback quickly."""

    PIECE = OUTPUT_RATE * 2 * 50 // 1000

    def __init__(self, device: str | None = None):
        import pyaudio

        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=pyaudio.paInt16, channels=1, rate=OUTPUT_RATE, output=True,
            output_device_index=_find_device(self._pa, device, False),
        )
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._gen = 0
        self._pending = 0
        self._closed = False
        threading.Thread(target=self._run, daemon=True).start()

    def write(self, pcm: bytes) -> None:
        with self._lock:
            gen = self._gen
            self._pending += len(pcm)
        self._q.put((gen, pcm))

    def _run(self) -> None:
        while not self._closed:
            try:
                gen, pcm = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            for i in range(0, len(pcm), self.PIECE):
                piece = pcm[i:i + self.PIECE]
                with self._lock:
                    if gen != self._gen:
                        break
                self._stream.write(piece)
                with self._lock:
                    if gen == self._gen:
                        self._pending -= len(piece)

    def clear(self) -> None:
        with self._lock:
            self._gen += 1
            self._pending = 0
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def is_busy(self) -> bool:
        with self._lock:
            return self._pending > 0

    def play_blocking(self, pcm: bytes, timeout: float = 10.0) -> None:
        self.write(pcm)
        deadline = time.monotonic() + timeout
        while self.is_busy() and time.monotonic() < deadline:
            time.sleep(0.02)

    def close(self) -> None:
        self._closed = True
        self._stream.stop_stream()
        self._stream.close()
        self._pa.terminate()


class FakeMic:
    """Plays a PCM buffer frame by frame, then endless silence. speed>1 runs faster than real time."""

    def __init__(self, pcm: bytes = b"", speed: float = 1.0):
        self._pcm = pcm
        self._pos = 0
        self._delay = FRAME_MS / 1000 / speed

    def read(self) -> bytes:
        time.sleep(self._delay)
        chunk = self._pcm[self._pos:self._pos + INPUT_FRAME_BYTES]
        self._pos += INPUT_FRAME_BYTES
        return chunk + b"\0" * (INPUT_FRAME_BYTES - len(chunk))

    def close(self) -> None:
        pass


class FakeSpeaker:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.cleared = 0
        self.busy_until = 0.0

    @property
    def audio(self) -> bytes:
        return b"".join(self.chunks)

    def write(self, pcm: bytes) -> None:
        self.chunks.append(pcm)

    def clear(self) -> None:
        self.cleared += 1

    def is_busy(self) -> bool:
        return time.monotonic() < self.busy_until

    def play_blocking(self, pcm: bytes, timeout: float = 10.0) -> None:
        self.write(pcm)

    def close(self) -> None:
        pass
