"""Use this PC's microphone and speakers as the Pi's, over SSH. For testing before the Pi has its own audio gear.

    pip install sounddevice
    python tools/pc_audio_bridge.py                      # talk to the assistant through the PC mic/speakers
    python tools/pc_audio_bridge.py --list-devices
    python tools/pc_audio_bridge.py --from-wav in.wav --to-wav out.wav   # no hardware: stream a 16 kHz mono wav

The Pi runs `python -m assistant --remote-audio`: 16 kHz mono PCM goes up its stdin, 24 kHz mono PCM comes back on
stdout, and its log lines come back on stderr (shown here). Use headphones: speaker sound picked up by the mic
makes the assistant hear itself. Ctrl+C stops it.
"""
from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

MIC_RATE, OUT_RATE, FRAME = 16000, 24000, 640  # 40 ms of 16 kHz samples


def ssh_command(args) -> list[str]:
    cmd = ["ssh", "-o", "BatchMode=yes"]
    if args.key:
        cmd += ["-i", str(Path(args.key).expanduser()), "-o", "IdentitiesOnly=yes"]
    return cmd + [args.host, "cd ~/gemini-assistant && ./venv/bin/python -m assistant --remote-audio"]


def pump_wav_in(path: str, proc, stop: threading.Event) -> None:
    """Send a wav as if it were a live mic: real-time pace, then silence until stopped."""
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="gemini-pi")
    ap.add_argument("--key", default="~/.ssh/id_ed25519_claude", help="SSH private key ('' to use your default)")
    ap.add_argument("--in-device", help="PC microphone: index or name substring")
    ap.add_argument("--out-device", help="PC speaker: index or name substring")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--from-wav", help="stream this 16 kHz mono wav instead of the mic")
    ap.add_argument("--to-wav", help="save the reply audio to this wav instead of playing it")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0

    def dev(spec):
        return int(spec) if spec and spec.isdigit() else spec

    proc = subprocess.Popen(ssh_command(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
    stop = threading.Event()
    reply = bytearray()
    out_q: queue.Queue = queue.Queue()
    streams = []

    if args.from_wav:
        threading.Thread(target=pump_wav_in, args=(args.from_wav, proc, stop), daemon=True).start()
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
    if not args.to_wav:
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
            if speaker is not None:
                speaker.write(data)

    for s in streams:
        s.start()
    threading.Thread(target=reader, daemon=True).start()
    print("Bridge running. Say 'Hey Gemini' or 'Hey Assistant'. Ctrl+C to stop.", file=sys.stderr)
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
        print(f"Saved {len(reply) / OUT_RATE / 2:.1f}s of reply audio to {args.to_wav}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
