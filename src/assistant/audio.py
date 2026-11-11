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
from array import array

from .config import FRAME_MS, INPUT_FRAME_BYTES, INPUT_RATE, OUTPUT_RATE


def _rms_bytes(pcm: bytes) -> float:
    """Root-mean-square level of 16-bit little-endian mono PCM (0 for empty input)."""
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    return (sum(s * s for s in a) / len(a)) ** 0.5 if a else 0.0


def pyaudio_mod():
    import pyaudio  # imported lazily: only the Pi has PortAudio

    return pyaudio


def generate_chime(rate: int = OUTPUT_RATE, descending: bool = False) -> bytes:
    """Soft two-tone chime, no audio files needed: C5 then E5 to wake, E5 then G4 (falling) to go to sleep."""
    out = bytearray()
    tones = ((659.25, 0.08), (392.0, 0.18)) if descending else ((523.25, 0.08), (659.25, 0.12))
    for freq, dur in tones:
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


def pick_usb_device(devices, want_input: bool) -> int | None:
    """First USB device with the needed direction, skipping HDMI. `devices` is (index, name, max_in, max_out) tuples.

    The Pi 5 has HDMI audio outputs and no onboard mic; without this, the system default can be HDMI (silence).
    """
    channels = 2 if want_input else 3
    for d in devices:
        name = d[1].lower()
        if d[channels] > 0 and "usb" in name and "hdmi" not in name:
            return d[0]
    return None


def _choose_device(pa, spec: str | None, want_input: bool) -> tuple[int | None, str]:
    """Resolve a device setting to (index, display name).

    `spec` is an index, part of a name, "default" (the system default), or empty (the first USB device,
    else the system default).
    """
    key = "maxInputChannels" if want_input else "maxOutputChannels"
    spec = (spec or "").strip()
    index: int | None
    if spec.lower() == "default":
        index = None
    elif spec.isdigit():
        index = int(spec)
    elif spec:
        index = next((i for i in range(pa.get_device_count())
                      if (d := pa.get_device_info_by_index(i))[key] > 0 and spec.lower() in d["name"].lower()), None)
        if index is None:
            raise RuntimeError(f"No {'input' if want_input else 'output'} audio device matching {spec!r}")
    else:
        devices = []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            devices.append((i, d["name"], d["maxInputChannels"], d["maxOutputChannels"]))
        index = pick_usb_device(devices, want_input)
    try:
        info = (pa.get_device_info_by_index(index) if index is not None else
                pa.get_default_input_device_info() if want_input else pa.get_default_output_device_info())
        name = info["name"] + ("" if index is not None else " (system default)")
    except OSError:
        name = "none available"
    return index, name


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


def resample_pcm16(pcm: bytes, src: int, dst: int) -> bytes:
    """Resample 16-bit mono PCM. Averages blocks first when downsampling (a cheap low-pass), then interpolates."""
    if src == dst or not pcm:
        return pcm
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return b""
    if src >= 2 * dst:  # e.g. 48 kHz -> 16 kHz: average k samples so high frequencies do not alias
        k = src // dst
        a = array("h", (sum(a[i:i + k]) // k for i in range(0, len(a) - k + 1, k)))
        src = src / k
        if not a:
            return b""
    if src == dst:
        return a.tobytes()
    n_out = int(len(a) * dst / src)
    out = array("h")
    last = len(a) - 1
    for i in range(n_out):
        pos = i * src / dst
        j = min(int(pos), last)
        nxt = a[min(j + 1, last)]
        out.append(int(a[j] + (nxt - a[j]) * (pos - j)))
    return out.tobytes()


def _open_stream(pa, want_rate: int, device_index: int | None, is_input: bool, frames_per_buffer: int | None = None):
    """Open a mono stream at want_rate if the device allows it, else at its native rate, else as stereo.

    Returns (stream, actual_rate, channels). USB sound cards often support only 44.1/48 kHz and/or stereo.
    """
    import pyaudio

    info = (pa.get_device_info_by_index(device_index) if device_index is not None else
            (pa.get_default_input_device_info() if is_input else pa.get_default_output_device_info()))
    rates = [want_rate]
    native = int(info["defaultSampleRate"])
    for r in (native, 48000, 44100):
        if r not in rates:
            rates.append(r)
    last_error: Exception | None = None
    for channels in (1, 2):
        for rate in rates:
            kwargs = dict(format=pyaudio.paInt16, channels=channels, rate=rate)
            if is_input:
                kwargs.update(input=True, input_device_index=device_index)
            else:
                kwargs.update(output=True, output_device_index=device_index)
            if frames_per_buffer:
                kwargs["frames_per_buffer"] = frames_per_buffer * rate // want_rate
            try:
                return pa.open(**kwargs), rate, channels
            except OSError as e:
                last_error = e
    raise OSError(f"Could not open {info['name']!r} for {'input' if is_input else 'output'} "
                  f"(tried rates {rates}, mono and stereo): {last_error}")


class PyAudioMic:
    """Reads 16 kHz mono frames, converting from whatever rate and channel count the device needs."""

    def __init__(self, device: str | None = None):
        self._pa = pyaudio_mod().PyAudio()
        self._chunk = INPUT_FRAME_BYTES // 2
        index, self.device_name = _choose_device(self._pa, device, True)
        self._stream, self._rate, self._channels = _open_stream(self._pa, INPUT_RATE, index, True, self._chunk)
        self._native_chunk = self._chunk * self._rate // INPUT_RATE

    def read(self) -> bytes:
        data = self._stream.read(self._native_chunk, exception_on_overflow=False)
        if self._channels == 2:  # keep the left channel
            a = array("h")
            a.frombytes(data)
            data = a[0::2].tobytes()
        data = resample_pcm16(data, self._rate, INPUT_RATE)
        return data.ljust(INPUT_FRAME_BYTES, b"\x00")[:INPUT_FRAME_BYTES]  # exactly one frame

    def close(self) -> None:
        self._stream.stop_stream()
        self._stream.close()
        self._pa.terminate()


class PyAudioSpeaker:
    """Plays queued 24 kHz mono PCM in 50 ms pieces so clear() can cut playback quickly.

    The device may run at another rate or as stereo; conversion happens in write().
    """

    def __init__(self, device: str | None = None):
        self._pa = pyaudio_mod().PyAudio()
        index, self.device_name = _choose_device(self._pa, device, False)
        self._stream, self._rate, self._channels = _open_stream(self._pa, OUTPUT_RATE, index, False)
        self._start_worker()

    def _start_worker(self) -> None:
        self._piece = self._rate * 2 * self._channels * 50 // 1000  # bytes in 50 ms of device audio
        self._piece -= self._piece % (2 * self._channels)
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._gen = 0
        self._pending = 0
        self._closed = False
        threading.Thread(target=self._run, daemon=True).start()

    def _to_device(self, pcm: bytes) -> bytes:
        pcm = resample_pcm16(pcm, OUTPUT_RATE, self._rate)
        if self._channels == 2:  # duplicate mono into both channels
            a = array("h")
            a.frombytes(pcm[: len(pcm) // 2 * 2])
            st = array("h", bytes(len(a) * 4))
            st[0::2] = a
            st[1::2] = a
            pcm = st.tobytes()
        return pcm

    def write(self, pcm: bytes) -> None:
        pcm = self._to_device(pcm)
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
            for i in range(0, len(pcm), self._piece):
                piece = pcm[i:i + self._piece]
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


class StdioMic:
    """Mic audio arrives on stdin as 16 kHz mono 16-bit PCM (e.g. streamed from the PC over SSH)."""

    def __init__(self, stream=None):
        import sys

        self._in = stream or sys.stdin.buffer

    def read(self) -> bytes:
        data = b""
        while len(data) < INPUT_FRAME_BYTES:
            chunk = self._in.read(INPUT_FRAME_BYTES - len(data))
            if not chunk:
                raise EOFError("audio input closed")
            data += chunk
        return data

    def close(self) -> None:
        pass


class _PacedSink:
    """Writes PCM to a stream at real-time speed, so the speaker's queue still reflects what has been heard."""

    def __init__(self, out, rate: int, lead: float = 0.25):
        # `lead` seconds are sent ahead of real time, giving the far end a cushion against network jitter
        self._out, self._rate, self._lead = out, rate, lead
        self._t0: float | None = None
        self._sent = 0.0  # seconds of audio sent since _t0

    def write(self, piece: bytes) -> None:
        now = time.monotonic()
        if self._t0 is None or now - self._t0 > self._sent + 0.5:  # the stream went idle: restart the clock
            self._t0, self._sent = now, 0.0
        self._out.write(piece)
        self._out.flush()
        self._sent += len(piece) / 2 / self._rate
        time.sleep(max(0.0, self._t0 + self._sent - self._lead - time.monotonic()))

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


class _NoPortAudio:
    def terminate(self) -> None:
        pass


class StdioSpeaker(PyAudioSpeaker):
    """Reply audio goes to stdout as 24 kHz mono 16-bit PCM. Paced in real time so barge-in (clear) still works."""

    def __init__(self, out=None):
        import sys

        self._pa = _NoPortAudio()
        self._rate, self._channels = OUTPUT_RATE, 1
        self._stream = _PacedSink(out or sys.stdout.buffer, OUTPUT_RATE)
        self._start_worker()


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
