"""Use this PC's microphone and speakers as the Pi's, over SSH. For testing before the Pi has its own audio gear.

    pip install sounddevice
    python tools/pc_audio_bridge.py                      # talk to the assistant through the PC mic/speakers
    python tools/pc_audio_bridge.py --list-devices
    python tools/pc_audio_bridge.py --wake-test          # count wake-word triggers only (no Gemini calls, no cost)
    python tools/pc_audio_bridge.py --headphones         # headphones: you can interrupt the assistant by speaking
    python tools/pc_audio_bridge.py --echo-check         # does the mic hear the speaker? (plays a quiet tone)
    python tools/pc_audio_bridge.py --from-wav in.wav --to-wav out.wav   # no hardware: stream a 16 kHz mono wav
    python tools/pc_audio_bridge.py --from-wav in.wav --to-wav out.wav --echo 0.6   # ...with simulated speaker echo

The Pi runs `python -m assistant --remote-audio`: 16 kHz mono PCM goes up its stdin, 24 kHz mono PCM comes back on
stdout, and its log lines come back on stderr (shown here). Speaker sound picked up by the mic makes the
assistant hear itself and interrupt its own answers, so by default the Pi ignores the mic while it is speaking
(you cannot interrupt it). With headphones there is no echo: pass --headphones to allow interrupting. Ctrl+C stops it.
"""
from __future__ import annotations

import argparse
import os
from array import array
import queue
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

MIC_RATE, OUT_RATE, FRAME = 16000, 24000, 640  # 40 ms of 16 kHz samples

if sys.stderr.isatty():
    os.system("")  # makes the classic Windows console understand ANSI colors


def info(msg: str) -> None:
    """The bridge's own messages, in the same style as the Pi's [Status] lines (cyan)."""
    color = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
    print(f"\x1b[36m[Status] {msg}\x1b[0m" if color else f"[Status] {msg}", file=sys.stderr, flush=True)


def ssh_command(args) -> list[str]:
    cmd = ["ssh", "-o", "BatchMode=yes"]
    if args.key:
        cmd += ["-i", str(Path(args.key).expanduser()), "-o", "IdentitiesOnly=yes"]
    mute = "" if args.headphones else "MUTE_MIC_WHILE_SPEAKING=true "  # PC speakers feed back into the PC mic
    remote = f"cd ~/gemini-assistant && {mute}./venv/bin/python -m assistant --remote-audio"
    if args.wake_test:
        remote += " --wake-test"
    return cmd + [args.host, remote]


def to_16k(pcm24: bytes) -> bytes:
    """24 kHz -> 16 kHz by linear interpolation (enough for an echo simulation)."""
    a = array("h")
    a.frombytes(pcm24[: len(pcm24) // 2 * 2])
    out = array("h", (int(a[int(i * 1.5)] + (a[min(int(i * 1.5) + 1, len(a) - 1)] - a[int(i * 1.5)]) * ((i * 1.5) % 1))
                      for i in range(len(a) * 2 // 3)))
    return out.tobytes()


def mix(mic: bytes, echo: bytes, gain: float) -> bytes:
    """mic + gain * echo, clipped to 16 bit."""
    m, e = array("h"), array("h")
    m.frombytes(mic)
    e.frombytes(echo[: len(mic)].ljust(len(mic), bytes(1)))
    return array("h", (max(-32768, min(32767, int(x + gain * y))) for x, y in zip(m, e))).tobytes()


def pump_wav_in(path: str, proc, stop: threading.Event, echo_gain: float = 0.0,
                echo_buf: bytearray | None = None, echo_lock: threading.Lock | None = None) -> None:
    """Send a wav as if it were a live mic: real-time pace, then silence until stopped.

    With echo_gain > 0 the reply audio that has come back from the Pi is mixed into the mic stream at the pace it
    would play, as if the speaker were next to the microphone.
    """
    with wave.open(path, "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, MIC_RATE):
            sys.exit(f"{path}: need 16 kHz mono 16-bit wav")
        data = w.readframes(w.getnframes())
    step = FRAME * 2
    t0 = time.monotonic()
    i = 0
    try:
        while not stop.is_set():
            frame = data[i:i + step].ljust(step, b"\x00")
            proc.stdin.write(frame)
            proc.stdin.flush()
            i += step
            time.sleep(max(0.0, t0 + i / 2 / MIC_RATE - time.monotonic()))
    except (BrokenPipeError, OSError):
        pass


def echo_check(in_device, out_device) -> int:
    """Play a quiet tone and measure whether the microphone picks it up: the cause of the assistant hearing itself."""
    import math

    import sounddevice as sd

    rate, secs, settle = 16000, 1.2, 9600  # settle: skip the first 0.3 s of each recording (device start-up noise)
    tone = array("h", (int(32767 * 0.12 * math.sin(2 * math.pi * 800 * i / rate)) for i in range(int(rate * secs * 2)))).tobytes()

    def rms(buf: bytes) -> float:
        a = array("h")
        a.frombytes(buf[: len(buf) // 2 * 2])
        return (sum(x * x for x in a) / max(len(a), 1)) ** 0.5

    def record(play: bytes) -> bytes:
        """Record the mic for about `secs` seconds while the speaker plays `play` (silence if empty)."""
        captured, pos = bytearray(), [0]

        def callback(indata, outdata, frames, time_info, status):
            captured.extend(bytes(indata))
            n = frames * 2
            outdata[:] = play[pos[0]:pos[0] + n].ljust(n, bytes(1))
            pos[0] += n

        with sd.RawStream(samplerate=rate, channels=1, dtype="int16", device=(in_device, out_device), callback=callback):
            sd.sleep(int((secs + 0.3) * 1000))
        return bytes(captured[settle:])

    names = sd.query_devices(in_device if in_device is not None else sd.default.device[0])["name"],         sd.query_devices(out_device if out_device is not None else sd.default.device[1])["name"]
    info(f"Echo check. Microphone: {names[0]}; speaker: {names[1]}")
    info("Stay quiet for a few seconds: first the room is measured, then a quiet tone plays.")
    base_level = rms(record(b""))
    tone_level = rms(record(tone))
    ratio = tone_level / max(base_level, 1.0)
    info(f"Room level {base_level:.0f}; level while the tone played {tone_level:.0f} ({ratio:.1f}x)")
    if ratio > 2:
        print("[91m[Fail] The microphone hears the speaker. The assistant will hear itself and interrupt its own "
              "answers. Use a headset whose mic is away from the speaker, pick the right devices with "
              "--in-device/--out-device, or keep the default (mic muted while it speaks).[0m", file=sys.stderr)
        return 1
    print("[32m[Pass] The microphone does not hear the speaker: --headphones (interrupting) will work.[0m",
          file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="gemini-pi")
    ap.add_argument("--key", default="~/.ssh/id_ed25519_claude", help="SSH private key ('' to use your default)")
    ap.add_argument("--in-device", help="PC microphone: index or name substring")
    ap.add_argument("--out-device", help="PC speaker: index or name substring")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--echo-check", action="store_true",
                    help="play a quiet tone and check whether the microphone hears it")
    ap.add_argument("--from-wav", help="stream this 16 kHz mono wav instead of the mic")
    ap.add_argument("--to-wav", help="save the reply audio to this wav instead of playing it")
    ap.add_argument("--wake-test", action="store_true",
                    help="only listen for the wake phrases and count triggers: no Gemini calls, no cost, no speaker")
    ap.add_argument("--headphones", action="store_true",
                    help="no speaker echo: the assistant listens while it speaks, so you can interrupt it")
    ap.add_argument("--echo", type=float, default=0.0, metavar="GAIN",
                    help="with --from-wav: mix the reply back into the mic at this level (0.6 is a loud echo)")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0

    def dev(spec):
        return int(spec) if spec and spec.isdigit() else spec

    if args.echo_check:
        return echo_check(dev(args.in_device), dev(args.out_device))

    proc = subprocess.Popen(ssh_command(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
    stop = threading.Event()
    reply = bytearray()
    echo_buf, echo_lock = bytearray(), threading.Lock()
    out_q: queue.Queue = queue.Queue()
    streams = []

    if args.from_wav:
        threading.Thread(target=pump_wav_in, args=(args.from_wav, proc, stop, args.echo, echo_buf, echo_lock),
                         daemon=True).start()
    else:
        import sounddevice as sd

        def on_mic(indata, frames, t, status):
            try:
                out_q.put_nowait(bytes(indata))
            except queue.Full:
                pass

        mic = sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=FRAME,
                                device=dev(args.in_device), callback=on_mic)
        streams.append(mic)

        def writer():
            try:
                while not stop.is_set():
                    proc.stdin.write(out_q.get())
                    proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

        threading.Thread(target=writer, daemon=True).start()

    speaker = None
    if not args.to_wav and not args.wake_test:
        import sounddevice as sd

        speaker = sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16", device=dev(args.out_device),
                                   latency="high")  # bigger buffer: avoids clicks from network jitter
        streams.append(speaker)

    def reader():
        while True:
            data = proc.stdout.read(2400)
            if not data:
                stop.set()
                return
            reply.extend(data)
            if args.echo:
                with echo_lock:
                    echo_buf.extend(data)
            if speaker is not None:
                speaker.write(data)

    for s in streams:
        s.start()
    if not args.from_wav or speaker is not None:
        import sounddevice as sd

        def name(stream):
            return sd.query_devices(stream.device)["name"]

        if not args.from_wav:
            info(f"Microphone: {name(mic)}")
        if speaker is not None:
            info(f"Speaker:    {name(speaker)}")
        if not args.headphones and not args.wake_test:
            info("The assistant ignores the microphone while it speaks (stops it hearing itself). "
                 "With headphones or a headset, add --headphones to be able to interrupt it.")
    threading.Thread(target=reader, daemon=True).start()
    info("Bridge running. Say 'Hey Gemini' or 'Hey Assistant'. Ctrl+C to stop.")
    t0 = time.monotonic()
    try:
        while not stop.is_set() and proc.poll() is None:
            if args.seconds and time.monotonic() - t0 > args.seconds:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    stop.set()
    for s in streams:
        s.stop()
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    if args.to_wav:
        with wave.open(args.to_wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUT_RATE)
            w.writeframes(bytes(reply))
        info(f"Saved {len(reply) / OUT_RATE / 2:.1f}s of reply audio to {args.to_wav}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
