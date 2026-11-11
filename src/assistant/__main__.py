"""python -m assistant [--simulate IN.wav [--out OUT.wav]] [--list-devices]"""
from __future__ import annotations

import argparse
import sys
from functools import partial

from . import app
from .audio import FakeMic, FakeSpeaker, PyAudioMic, PyAudioSpeaker, list_devices, read_wav_pcm16, write_wav_pcm16
from .config import INPUT_RATE, OUTPUT_RATE, ConfigError, load_config
from .session import make_client
from .wake import WakeDetector


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="assistant")
    ap.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    ap.add_argument("--simulate", metavar="IN.wav", help="run once using a 16 kHz mono wav as the microphone")
    ap.add_argument("--out", metavar="OUT.wav", default="/tmp/sim_out.wav", help="where --simulate saves the reply audio")
    ap.add_argument("--play", action="store_true", help="with --simulate: play the reply on the real speaker")
    args = ap.parse_args(argv)

    if args.list_devices:
        print(list_devices())
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1
    detector = WakeDetector(cfg.vosk_model_path, cfg.wake_phrases)
    factory = partial(make_client, cfg)

    if args.simulate:
        mic = FakeMic(read_wav_pcm16(args.simulate, INPUT_RATE))
        if args.play:  # real speaker, so the reply can be heard; nothing is saved
            speaker = PyAudioSpeaker(cfg.audio_out_device)
            try:
                conv = app.run_once(cfg, mic, speaker, detector, factory)
            finally:
                speaker.close()
            print("Played the reply on the speaker")
        else:
            speaker = FakeSpeaker()
            conv = app.run_once(cfg, mic, speaker, detector, factory)
            write_wav_pcm16(args.out, speaker.audio, OUTPUT_RATE)
            print(f"Saved {len(speaker.audio) / (OUTPUT_RATE * 2):.1f}s of reply audio to {args.out}")
        print("User said:     ", "".join(conv.transcript["user"]))
        print("Assistant said:", "".join(conv.transcript["assistant"]))
        return 0

    mic = PyAudioMic(cfg.audio_in_device)
    speaker = PyAudioSpeaker(cfg.audio_out_device)
    try:
        app.run_forever(cfg, mic, speaker, detector, factory)
    finally:
        mic.close()
        speaker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
