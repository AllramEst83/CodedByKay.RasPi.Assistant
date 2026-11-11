"""python -m assistant [--doctor | --wake-test | --simulate IN.wav | --list-devices | --remote-audio]"""
from __future__ import annotations

import argparse
import sys
import time
from functools import partial

from . import app
from .audio import (FakeMic, FakeSpeaker, PyAudioMic, PyAudioSpeaker, StdioMic, StdioSpeaker, list_devices,
                    read_wav_pcm16, write_wav_pcm16)
from .config import INPUT_RATE, OUTPUT_RATE, ConfigError, load_config
from .log import log, make_logger
from .session import make_client
from .timers import TimerManager
from .wake import WakeDetector


def _wait_for_timers(timers: TimerManager, max_wait: float = 40.0) -> None:
    """--simulate only: stay alive until pending timers have gone off, so the test can hear them."""
    deadline = time.monotonic() + max_wait
    if timers.list():
        log(f"[Status] Waiting for {len(timers.list())} timer(s) (up to {max_wait:.0f} s)...")
    while timers.list() and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(3.0)  # the alarm tone and message are still playing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="assistant")
    ap.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    ap.add_argument("--doctor", action="store_true", help="check config, wake word, audio, network and the Gemini connection")
    ap.add_argument("--skip-audio", action="store_true", help="with --doctor: skip the microphone and speaker checks")
    ap.add_argument("--skip-api", action="store_true", help="with --doctor: skip the Gemini connection check")
    ap.add_argument("--wake-test", action="store_true",
                    help="listen for the wake phrases only (no Gemini calls) and count triggers")
    ap.add_argument("--minutes", type=float, help="with --wake-test: stop after this many minutes")
    ap.add_argument("--simulate", metavar="IN.wav", help="run once using a 16 kHz mono wav as the microphone")
    ap.add_argument("--out", metavar="OUT.wav", default="/tmp/sim_out.wav", help="where --simulate saves the reply audio")
    ap.add_argument("--play", action="store_true", help="with --simulate: play the reply on the real speaker")
    ap.add_argument("--remote-audio", action="store_true",
                    help="use PCM on stdin/stdout as mic/speaker (run through tools/pc_audio_bridge.py on the PC)")
    args = ap.parse_args(argv)

    if args.list_devices:
        print(list_devices())
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        make_logger(sys.stderr)(f"[Error] Config: {e}")
        return 1
    if args.doctor:
        from .doctor import run_doctor

        return 1 if run_doctor(cfg, log, skip_audio=args.skip_audio, skip_api=args.skip_api) else 0

    detector = WakeDetector(cfg.vosk_model_path, cfg.wake_phrases)
    factory = partial(make_client, cfg)

    if args.wake_test:
        if args.remote_audio:  # the PC bridge supplies the mic; logs go back over stderr
            app.wake_test(StdioMic(), detector, make_logger(sys.stderr, color=True), args.minutes)
        else:
            mic = PyAudioMic(cfg.audio_in_device)
            log(f"[Status] Microphone: {mic.device_name} ({mic._rate} Hz)")
            try:
                app.wake_test(mic, detector, log, args.minutes)
            finally:
                mic.close()
        return 0

    if args.simulate:
        mic = FakeMic(read_wav_pcm16(args.simulate, INPUT_RATE))
        if args.play:  # real speaker, so the reply can be heard; nothing is saved
            speaker = PyAudioSpeaker(cfg.audio_out_device)
            try:
                timers = TimerManager(speaker, cfg)
                app.run_once(cfg, mic, speaker, detector, factory, timers=timers)
                _wait_for_timers(timers)
            finally:
                speaker.close()
            log("[Status] Played the reply on the speaker")
        else:
            speaker = FakeSpeaker()
            timers = TimerManager(speaker, cfg)
            app.run_once(cfg, mic, speaker, detector, factory, timers=timers)
            _wait_for_timers(timers)
            write_wav_pcm16(args.out, speaker.audio, OUTPUT_RATE)
            log(f"[Status] Saved {len(speaker.audio) / (OUTPUT_RATE * 2):.1f}s of reply audio to {args.out}")
        return 0  # what was said is already in the log as [You] / [Gemini] lines

    if args.remote_audio:  # PC bridge: mic PCM on stdin, reply PCM on stdout, logs on stderr (colored: a PC terminal shows them)
        bridge_log = make_logger(sys.stderr, color=True)
        mic, speaker = StdioMic(), StdioSpeaker()
        try:
            app.run_forever(cfg, mic, speaker, detector, factory, bridge_log)
        except EOFError:
            bridge_log("[Status] PC audio bridge disconnected")
        finally:
            speaker.close()
        return 0

    mic = PyAudioMic(cfg.audio_in_device)
    speaker = PyAudioSpeaker(cfg.audio_out_device)
    log(f"[Status] Microphone: {mic.device_name} ({mic._rate} Hz)")
    log(f"[Status] Speaker:    {speaker.device_name} ({speaker._rate} Hz)")
    try:
        app.run_forever(cfg, mic, speaker, detector, factory, log)
    finally:
        mic.close()
        speaker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
