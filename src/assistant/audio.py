"""Audio I/O behind small interfaces so real and simulated devices are swappable.

Mic:     read() -> one frame of 16 kHz mono 16-bit PCM (blocking)
Speaker: write(pcm), clear() (barge-in), is_busy(), play_blocking(pcm), close()
"""
from __future__ import annotations

import contextlib
import math
import os
import queue
import struct
import threading
import time
import wave
from array import array

from .config import FRAME_MS, INPUT_FRAME_BYTES, INPUT_RATE, OUTPUT_RATE


@contextlib.contextmanager
def quiet_stderr():
    """Silence the C libraries' stderr chatter (ALSA, JACK and PortAudio probe messages) while audio devices are opened."""
    try:
        saved, null = os.dup(2), os.open(os.devnull, os.O_WRONLY)
    except OSError:
        yield
        return
    try:
        os.dup2(null, 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(null)


def _close_quietly(stream, pa) -> None:
    for fn in (getattr(stream, "stop_stream", None), getattr(stream, "close", None), getattr(pa, "terminate", None)):
        try:
            if fn:
                fn()
        except Exception:  # noqa: BLE001  the device may already be gone
            pass


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


def generate_ready_chime(rate: int = OUTPUT_RATE) -> bytes:
    """Played once when the assistant has booted and is waiting for the wake phrase: a brighter, longer rising arpeggio
    (C5 E5 G5 C6, the last note held and fading) that cannot be mistaken for the wake chime (two short notes)."""
    out = bytearray()
    for freq, dur, gain in ((523.25, 0.11, 0.22), (659.25, 0.11, 0.22), (783.99, 0.11, 0.22), (1046.5, 0.45, 0.22)):
        n = int(rate * dur)
        for i in range(n):
            env = math.sin(math.pi * i / n) if dur < 0.2 else math.sin(math.pi * i / n) ** 0.5 * (1 - i / n)
            tone = math.sin(2 * math.pi * freq * i / rate) + 0.3 * math.sin(4 * math.pi * freq * i / rate)  # a little shimmer
            out += struct.pack("<h", int(32767 * gain * env * tone / 1.3))
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

    with quiet_stderr():
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
    """Reads 16 kHz mono frames, converting from whatever rate and channel count the device needs.

    If the device disappears (unplugged, USB hiccup) the mic notices, returns silence at real-time pace so the app
    stays alive and idle, and reopens the device as soon as it is back. Two signs of a lost device: a read error, or
    frames arriving much faster than real time (a dead handle that returns at once). A third: the stream stalls and
    never returns (the USB device re-enumerated under it), which a blocking read cannot report, so the reads happen
    in a helper thread and read() gives up on it after STALL_SECONDS.
    """

    WINDOW = 25  # frames (1 s of audio) per pacing check
    STALL_SECONDS = 1.5  # no frame for this long: the stream is dead

    def __init__(self, device: str | None = None, log=None):
        self._device = device
        self._log = log or (lambda line: None)
        self._chunk = INPUT_FRAME_BYTES // 2
        self._dead = False
        self._next_try = 0.0
        self._waiting_logged = False
        self._open()

    def _open(self) -> None:
        with quiet_stderr():
            pa = pyaudio_mod().PyAudio()
            try:
                index, name = _choose_device(pa, self._device, True)
                stream, rate, channels = _open_stream(pa, INPUT_RATE, index, True, self._chunk)
            except Exception:
                pa.terminate()
                raise
        self._pa, self._stream, self._rate, self._channels, self.device_name = pa, stream, rate, channels, name
        self._native_chunk = self._chunk * rate // INPUT_RATE
        self._win_start, self._win_frames, self._fast = time.monotonic(), 0, 0
        self._start_reader()

    def _start_reader(self) -> None:
        self._gen = getattr(self, "_gen", 0) + 1
        gen, stream, n = self._gen, self._stream, self._native_chunk
        q: queue.Queue = queue.Queue(maxsize=10)  # a short backlog; the oldest frame is dropped if the consumer lags
        self._q = q

        def offer(item) -> None:
            try:
                q.put_nowait(item)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass

        def run() -> None:
            while self._gen == gen:
                try:
                    data = stream.read(n, exception_on_overflow=False)
                except Exception as e:  # noqa: BLE001
                    offer(e)
                    return
                offer(data)

        threading.Thread(target=run, daemon=True, name="mic-reader").start()

    def _abandon_stream(self) -> None:
        """Stop using the current stream. Closing runs in the background: a stalled device can block the close too."""
        self._gen += 1
        threading.Thread(target=_close_quietly, args=(self._stream, self._pa), daemon=True).start()

    def read(self) -> bytes:
        if self._dead:
            return self._silence_and_retry()
        try:
            data = self._q.get(timeout=self.STALL_SECONDS)
        except queue.Empty:
            return self._lost(f"no audio for {self.STALL_SECONDS:g} s")
        if isinstance(data, Exception):
            return self._lost(f"read failed: {data}")
        # a healthy stream blocks about 40 ms per frame; many frames in far less time mean the handle is dead
        self._win_frames += 1
        if self._win_frames >= self.WINDOW:
            now = time.monotonic()
            self._fast = self._fast + 1 if now - self._win_start < 0.5 * self.WINDOW * 0.04 else 0
            self._win_start, self._win_frames = now, 0
            if self._fast >= 2:
                return self._lost("audio arriving faster than real time")
        if self._channels == 2:  # keep the left channel
            a = array("h")
            a.frombytes(data)
            data = a[0::2].tobytes()
        data = resample_pcm16(data, self._rate, INPUT_RATE)
        return data.ljust(INPUT_FRAME_BYTES, b"\x00")[:INPUT_FRAME_BYTES]  # exactly one frame

    def _lost(self, why: str) -> bytes:
        self._log(f"[Warn] Microphone lost ({why}); waiting for it to come back")
        self._abandon_stream()
        self._dead, self._next_try, self._waiting_logged = True, time.monotonic() + 0.5, False
        return self._silence_and_retry()

    def _silence_and_retry(self) -> bytes:
        time.sleep(0.04)  # keep the real-time pace so nothing spins
        if time.monotonic() >= self._next_try:
            try:
                self._open()
            except Exception as e:  # noqa: BLE001
                self._next_try = time.monotonic() + 1.0
                if not self._waiting_logged:
                    self._log(f"[Warn] Microphone not available yet ({e}); retrying every second")
                    self._waiting_logged = True
            else:
                self._dead = False
                self._log(f"[Status] Microphone reconnected: {self.device_name} ({self._rate} Hz)")
        return bytes(INPUT_FRAME_BYTES)

    def close(self) -> None:
        if not self._dead:
            self._abandon_stream()


class PyAudioSpeaker:
    """Plays queued 24 kHz mono PCM in 50 ms pieces so clear() can cut playback quickly.

    The device may run at another rate or as stereo; conversion happens in write(). If the device disappears, the
    queue is dropped and the device is reopened when it is back (the app keeps running).
    """

    playback_delay = 0.05  # seconds between writing a piece and hearing it (the device buffer)
    REOPEN_SECONDS = 30  # how long to wait for a lost device before giving up on the audio that was queued

    def __init__(self, device: str | None = None, log=None):
        self._device = device
        self._log = log or (lambda line: None)
        self._open_device()
        self._start_worker()

    def _open_device(self) -> None:
        with quiet_stderr():
            pa = pyaudio_mod().PyAudio()
            try:
                index, name = _choose_device(pa, self._device, False)
                stream, rate, channels = _open_stream(pa, OUTPUT_RATE, index, False)
            except Exception:
                pa.terminate()
                raise
        self._pa, self._stream, self._rate, self._channels, self.device_name = pa, stream, rate, channels, name
        self._set_piece()

    def _set_piece(self) -> None:
        self._piece = self._rate * 2 * self._channels * 50 // 1000  # bytes in 50 ms of device audio
        self._piece -= self._piece % (2 * self._channels)

    def _start_worker(self) -> None:
        self._set_piece()
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._gen = 0
        self._pending = 0
        self._level, self._level_at = 0.0, 0.0  # loudness of the piece being played, for the echo gate
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
                self._level, self._level_at = _rms_bytes(piece), time.monotonic()
                try:
                    self._stream.write(piece)
                except (OSError, ValueError):
                    self.clear()  # nothing can be played now; do not leave is_busy() stuck
                    self._reopen()
                    break
                with self._lock:
                    if gen == self._gen:
                        self._pending -= len(piece)

    def _reopen(self) -> bool:
        """After a write failure: wait for the device to come back and reopen it. Returns whether it did."""
        self._log("[Warn] Speaker lost; waiting for it to come back")
        _close_quietly(self._stream, self._pa)
        deadline = time.monotonic() + self.REOPEN_SECONDS
        while not self._closed and time.monotonic() < deadline:
            try:
                self._open_device()
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
                continue
            self._log(f"[Status] Speaker reconnected: {self.device_name} ({self._rate} Hz)")
            return True
        self.clear()  # gave up: drop whatever was queued meanwhile
        return False

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

    def level(self) -> float:
        """Loudness (16-bit RMS) of what is being played right now; 0 when nothing is playing."""
        return self._level if self.is_busy() and time.monotonic() - self._level_at < 0.3 else 0.0

    def play_blocking(self, pcm: bytes, timeout: float = 10.0) -> None:
        self.write(pcm)
        deadline = time.monotonic() + timeout
        while self.is_busy() and time.monotonic() < deadline:
            time.sleep(0.02)

    def close(self) -> None:
        self._closed = True
        _close_quietly(self._stream, self._pa)


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

    playback_delay = 0.6  # the sink runs 0.25 s ahead, then the PC's output buffer and the network add more

    def __init__(self, out=None):
        import sys

        self._pa = _NoPortAudio()
        self._rate, self._channels = OUTPUT_RATE, 1
        self._stream = _PacedSink(out or sys.stdout.buffer, OUTPUT_RATE)
        self._start_worker()

    def _reopen(self) -> bool:
        self.clear()  # a broken pipe means the PC bridge went away: there is nothing to reopen
        return False


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
        self.level_value = 0.0  # tests set this to what the "speaker" is playing
        self.playback_delay = 0.0

    @property
    def audio(self) -> bytes:
        return b"".join(self.chunks)

    def write(self, pcm: bytes) -> None:
        self.chunks.append(pcm)

    def clear(self) -> None:
        self.cleared += 1

    def is_busy(self) -> bool:
        return time.monotonic() < self.busy_until

    def level(self) -> float:
        return self.level_value if self.is_busy() else 0.0

    def play_blocking(self, pcm: bytes, timeout: float = 10.0) -> None:
        self.write(pcm)

    def close(self) -> None:
        pass
