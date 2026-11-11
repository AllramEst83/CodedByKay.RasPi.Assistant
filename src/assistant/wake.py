"""Offline wake-phrase spotting with Vosk, restricted to a tiny phrase grammar.

A partial match is fast but noisy (the English model hears "He has a system", "Hey Jim" or the Swedish "heter
huvudstaden" as a wake phrase). So after a partial match the detector listens `confirm_ms` longer, forces a final
result, and only fires if that final result still matches. Measured on synthetic speech (tools/eval_wake.py):
10 of 81 look-alike sentences woke the plain partial detector; with a 200 ms confirmation 1 did, at +0.25 s delay.
"""
from __future__ import annotations

import json
from pathlib import Path


def _without_trailing_unk(text: str) -> str:
    """'hey gemini [unk] [unk]' -> 'hey gemini': speech after the phrase (a question, a breath) is not a reason to reject it."""
    words = text.split()
    while words and words[-1] == "[unk]":
        words.pop()
    return " ".join(words)


class WakeDetector:
    def __init__(self, model_path: str | Path, phrases: tuple[str, ...], rate: int = 16000, confirm_ms: int = 200):
        from vosk import KaldiRecognizer, Model, SetLogLevel

        if not Path(model_path).is_dir():
            raise FileNotFoundError(f"Vosk model not found at {model_path}")
        SetLogLevel(-1)
        self._phrases = tuple(p.lower() for p in phrases)
        self._rec = KaldiRecognizer(
            Model(str(model_path)), rate, json.dumps(list(self._phrases) + ["[unk]"])
        )
        self._rate = rate
        self.confirm_ms = confirm_ms  # 0 = fire on the first partial match (fastest, most false triggers)
        self.last_text = ""  # what the recognizer heard, e.g. "[unk] hey assistant": shown in the log to debug false triggers
        self._pending = False  # a partial match is waiting for its confirming final result
        self._waited_ms = 0.0

    def feed(self, pcm: bytes) -> str | None:
        """Feed one PCM frame; return the matched phrase, or None."""
        finished = self._rec.AcceptWaveform(pcm)
        if not self._pending:
            text = json.loads(self._rec.Result() if finished else self._rec.PartialResult())
            text = text.get("text" if finished else "partial", "")
            self.last_text = text
            hit = self._match(text)
            if hit is None:
                return None
            if finished or self.confirm_ms <= 0:
                self._rec.Reset()
                return hit
            self._pending, self._waited_ms = True, 0.0  # keep listening a little, then check the final result
            return None
        self._waited_ms += len(pcm) / 2 / self._rate * 1000
        if finished or self._waited_ms >= self.confirm_ms:
            final = json.loads(self._rec.Result() if finished else self._rec.FinalResult()).get("text", "")
            self.last_text = final
            self._pending = False
            hit = self._match(_without_trailing_unk(final))  # "hey gemini [unk]": the phrase, then more speech
            if hit is not None:
                self._rec.Reset()
                return hit
        return None  # not confirmed (or still waiting): keep listening

    def reset(self) -> None:
        self._rec.Reset()
        self._pending = False

    def _match(self, text: str) -> str | None:
        text = text.strip().lower()
        for p in self._phrases:
            if text == p or text.endswith(" " + p):
                return p
        return None
