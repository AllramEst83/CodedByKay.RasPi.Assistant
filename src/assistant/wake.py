"""Offline wake-phrase spotting with Vosk, restricted to a tiny phrase grammar."""
from __future__ import annotations

import json
from pathlib import Path


class WakeDetector:
    def __init__(self, model_path: str | Path, phrases: tuple[str, ...], rate: int = 16000):
        from vosk import KaldiRecognizer, Model, SetLogLevel

        if not Path(model_path).is_dir():
            raise FileNotFoundError(f"Vosk model not found at {model_path}")
        SetLogLevel(-1)
        self._phrases = tuple(p.lower() for p in phrases)
        self.last_text = ""
        self._rec = KaldiRecognizer(
            Model(str(model_path)), rate, json.dumps(list(self._phrases) + ["[unk]"])
        )

    def feed(self, pcm: bytes) -> str | None:
        """Feed one PCM frame; return the matched phrase, or None."""
        if self._rec.AcceptWaveform(pcm):
            text = json.loads(self._rec.Result()).get("text", "")
        else:
            text = json.loads(self._rec.PartialResult()).get("partial", "")
        hit = self._match(text)
        self.last_text = text  # what the recognizer heard, e.g. "[unk] hey assistant": shown in the log to debug false triggers
        if hit:
            self._rec.Reset()
        return hit

    def reset(self) -> None:
        self._rec.Reset()

    def _match(self, text: str) -> str | None:
        text = text.strip().lower()
        for p in self._phrases:
            if text == p or text.endswith(" " + p):
                return p
        return None
