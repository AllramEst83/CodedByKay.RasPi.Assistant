"""One active Gemini Live conversation: stream mic up, play replies, handle barge-in and idle."""
from __future__ import annotations

import asyncio
import threading
import time
from array import array
from typing import Any, Callable

from google import genai
from google.genai import types

from .config import INPUT_RATE, Config
from .log import log as default_log


def make_client(cfg: Config):
    return genai.Client(api_key=cfg.gemini_api_key, http_options=types.HttpOptions(api_version=cfg.api_version))


def build_live_config(cfg: Config) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=cfg.system_prompt,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.voice))
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # Google Search grounding runs on Google's side; the model decides when a question needs the web.
        tools=[types.Tool(google_search=types.GoogleSearch())] if cfg.google_search else None,
    )


def _rms(pcm: bytes) -> float:
    """Root-mean-square level of 16-bit little-endian mono PCM."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


class IdleTimer:
    """Expires after `timeout` seconds without speech from either party.

    While the speaker is still playing, the timer keeps resetting, so the countdown
    starts only when the reply has finished.
    """

    def __init__(self, timeout: float, clock: Callable[[], float] = time.monotonic):
        self.timeout = timeout
        self._clock = clock
        self._last = clock()

    def touch(self) -> None:
        self._last = self._clock()

    def expired(self, busy: bool = False) -> bool:
        if busy:
            self.touch()
            return False
        return self._clock() - self._last > self.timeout


class LiveConversation:
    def __init__(self, cfg: Config, client_factory: Callable[[], Any], mic, speaker,
                 log: Callable[[str], None] = default_log, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self._client_factory = client_factory
        self.mic = mic
        self.speaker = speaker
        self.log = log
        self._clock = clock
        self.transcript: dict[str, list[str]] = {"user": [], "assistant": []}
        self.last_error: BaseException | None = None  # set when run() ends with "error: ..."
        self._turn: dict[str, list[str]] = {"user": [], "assistant": []}  # text of the turn in progress, for the log

    def _flush_turn(self) -> None:
        """Log what was said in the finished (or interrupted) turn."""
        for who, tag in (("user", "You"), ("assistant", "Gemini")):
            text = "".join(self._turn[who]).strip()
            if text:
                self.log(f"[{tag}] {text}")
            self._turn[who] = []

    async def run(self) -> str:
        """Run until idle timeout or error. Returns 'idle', 'go_away' or 'error: ...'."""
        timer = IdleTimer(self.cfg.idle_timeout, self._clock)
        loop = asyncio.get_running_loop()
        audio_q: asyncio.Queue = asyncio.Queue(maxsize=100)
        stop = threading.Event()

        def put(data: bytes) -> None:
            try:
                audio_q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # drop rather than lag behind real time

        def mic_worker() -> None:
            while not stop.is_set():
                try:
                    data = self.mic.read()
                    loop.call_soon_threadsafe(put, data)
                except Exception:
                    break

        thread = threading.Thread(target=mic_worker, daemon=True)
        thread.start()
        reason = "idle"
        try:
            client = self._client_factory()
            async with client.aio.live.connect(model=self.cfg.model, config=build_live_config(self.cfg)) as session:
                self.log("[Status] Live session open")
                tasks = [
                    asyncio.create_task(self._send(session, audio_q, timer)),
                    asyncio.create_task(self._receive(session, timer)),
                    asyncio.create_task(self._monitor(timer)),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for t in done:
                    if t.exception():
                        raise t.exception()
                    reason = t.result()
        except Exception as e:  # connection, auth and quota errors end the session, not the program
            self.last_error = e
            reason = f"error: {type(e).__name__}: {e}"
            self.log(f"[Error] {reason}")
            self.speaker.clear()
        finally:
            stop.set()
            await loop.run_in_executor(None, thread.join, 1.0)
        self._flush_turn()  # anything said in a turn that never completed
        self.log(f"[Status] Live session closed ({reason})")
        return reason

    async def _send(self, session, audio_q: asyncio.Queue, timer: IdleTimer) -> str:
        while True:
            data = await audio_q.get()
            if self.cfg.mute_mic_while_speaking and self.speaker.is_busy():
                data = bytes(len(data))  # keep the stream flowing but silent
            elif _rms(data) > self.cfg.speech_rms:
                timer.touch()  # local voice check: Gemini's transcript arrives too late to hold the timer open
            await session.send_realtime_input(
                audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={INPUT_RATE}")
            )

    async def _receive(self, session, timer: IdleTimer) -> str:
        while True:
            # receive() ends after each completed turn, so loop for the next one
            async for msg in session.receive():
                if getattr(msg, "go_away", None):
                    self.log("[Status] Server asked to end the session")
                    return "go_away"
                sc = getattr(msg, "server_content", None)
                if sc is not None:
                    self._handle(sc, timer)
            await asyncio.sleep(0.01)

    def _handle(self, sc, timer: IdleTimer) -> None:
        if getattr(sc, "interrupted", False):
            self.speaker.clear()  # barge-in: stop playback immediately
            timer.touch()
            self._flush_turn()
            self.log("[Interrupted]")
        model_turn = getattr(sc, "model_turn", None)
        if model_turn is not None:
            for part in model_turn.parts or []:
                data = getattr(getattr(part, "inline_data", None), "data", None)
                if data:
                    self.speaker.write(data)
                    timer.touch()
        user = getattr(sc, "input_transcription", None)
        if user is not None and getattr(user, "text", None):
            self.transcript["user"].append(user.text)
            self._turn["user"].append(user.text)
            timer.touch()  # the user is speaking
        bot = getattr(sc, "output_transcription", None)
        if bot is not None and getattr(bot, "text", None):
            self.transcript["assistant"].append(bot.text)
            self._turn["assistant"].append(bot.text)
        if getattr(sc, "turn_complete", False):
            timer.touch()
            self._flush_turn()

    async def _monitor(self, timer: IdleTimer) -> str:
        while True:
            await asyncio.sleep(0.1)
            if timer.expired(busy=self.speaker.is_busy()):
                self.log(f"[Timeout] No speech for {self.cfg.idle_timeout:g}s, returning to idle")
                return "idle"
