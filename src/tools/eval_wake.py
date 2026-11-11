"""Compare wake-word detection strategies on the corpus from tools/make_wake_corpus.py.

Run on the Pi:  python tools/eval_wake.py [CORPUS_FOLDER_NAME] [-v]   (default folder: wake_corpus)
For each strategy: false triggers on the neg_* clips (must be 0), hits on the pos_* clips (should be all),
extra delay compared with the current strategy, and speed (x real time; idle listening needs well above 1).
"""
import json
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vosk import KaldiRecognizer, Model, SetLogLevel  # noqa: E402

from assistant.config import load_config  # noqa: E402

FRAME = 1280  # 40 ms of 16 kHz 16-bit mono
LEAD, TAIL = bytes(16000), bytes(48000)  # 0.5 s of silence before, 1.5 s after (lets the recognizer finalize)
PHRASES = ("hey gemini", "hey assistant")
DECOYS = ["hey", "assist", "assistance", "a system", "system", "hey system", "gem", "gemstone", "jim", "hey jim",
          "heater", "he is", "hey there", "hey google", "hey alexa", "hey siri", "hay", "he", "who", "huge"]


def match(text: str):
    text = text.strip().lower()
    return next((p for p in PHRASES if text == p or text.endswith(" " + p)), None)


def frames(pcm: bytes):
    data = LEAD + pcm + TAIL
    for i in range(0, len(data) - FRAME + 1, FRAME):
        yield i / 32000, data[i:i + FRAME]


class Strategy:
    name = ""
    note = ""

    def __init__(self, model):
        self.model = model

    def run(self, pcm: bytes):
        """Return (trigger_time_s, heard_text) or (None, '')."""
        raise NotImplementedError


class Current(Strategy):
    name = "A current: grammar, trigger on partial"

    def run(self, pcm):
        rec = KaldiRecognizer(self.model, 16000, json.dumps(list(PHRASES) + ["[unk]"]))
        for t, f in frames(pcm):
            text = json.loads(rec.Result())["text"] if rec.AcceptWaveform(f) else json.loads(rec.PartialResult())["partial"]
            if match(text):
                return t, text
        return None, ""


class FinalConf(Strategy):
    def __init__(self, model, min_conf):
        super().__init__(model)
        self.min_conf = min_conf
        self.name = f"B grammar, final result only, word confidence >= {min_conf}"

    def run(self, pcm):
        rec = KaldiRecognizer(self.model, 16000, json.dumps(list(PHRASES) + ["[unk]"]))
        rec.SetWords(True)
        for t, f in frames(pcm):
            if rec.AcceptWaveform(f):
                j = json.loads(rec.Result())
                p = match(j.get("text", ""))
                if p:
                    words = j.get("result", [])[-len(p.split()):]
                    if all(w["conf"] >= self.min_conf for w in words):
                        return t, j["text"]
        return None, ""


class Confirmed(Strategy):
    """Fast partial match, then keep listening `wait` seconds, force a final result, and accept only if it still matches.

    Vosk's own end-of-speech delay cannot be shortened from Python (no endpointer API in 0.3.45), so this makes one.
    """

    def __init__(self, model, wait, decoys=False):
        super().__init__(model)
        self.wait, self.decoys = wait, decoys
        self.name = f"G grammar{' + decoys' if decoys else ''}, partial match then forced final result after {wait:g} s"

    def run(self, pcm):
        grammar = list(PHRASES) + (DECOYS if self.decoys else []) + ["[unk]"]
        rec = KaldiRecognizer(self.model, 16000, json.dumps(grammar))
        pending_since = None
        for t, f in frames(pcm):
            finished = rec.AcceptWaveform(f)
            if pending_since is None:
                text = json.loads(rec.Result())["text"] if finished else json.loads(rec.PartialResult())["partial"]
                if match(text):
                    if finished:
                        return t, text
                    pending_since = t
                continue
            if finished or t - pending_since >= self.wait:
                text = json.loads(rec.Result() if finished else rec.FinalResult()).get("text", "")
                if match(text):
                    return t, text
                pending_since = None  # rejected: the recognizer was reset by the final result; keep listening
        return None, ""


class Free(Strategy):
    name = "C free decoding (no grammar), trigger on partial"

    def run(self, pcm):
        rec = KaldiRecognizer(self.model, 16000)
        for t, f in frames(pcm):
            text = json.loads(rec.Result())["text"] if rec.AcceptWaveform(f) else json.loads(rec.PartialResult())["partial"]
            if match(text):
                return t, text
        return None, ""


class Decoys(Strategy):
    name = "D grammar + look-alike decoy phrases, trigger on partial"

    def run(self, pcm):
        rec = KaldiRecognizer(self.model, 16000, json.dumps(list(PHRASES) + DECOYS + ["[unk]"]))
        for t, f in frames(pcm):
            text = json.loads(rec.Result())["text"] if rec.AcceptWaveform(f) else json.loads(rec.PartialResult())["partial"]
            if match(text):
                return t, text
        return None, ""


class Verified(Strategy):
    """Grammar partial trigger (fast), then a free-decoding second opinion on the last 2 s before accepting."""

    def __init__(self, model, window=2.0):
        super().__init__(model)
        self.window = window
        self.name = f"E grammar trigger + free-decoding check of the last {window:g} s"

    def run(self, pcm):
        rec = KaldiRecognizer(self.model, 16000, json.dumps(list(PHRASES) + ["[unk]"]))
        seen = bytearray()
        for t, f in frames(pcm):
            seen += f
            text = json.loads(rec.Result())["text"] if rec.AcceptWaveform(f) else json.loads(rec.PartialResult())["partial"]
            p = match(text)
            if p:
                tail = bytes(seen[-int(self.window * 32000):]) + bytes(12800)  # + 0.4 s silence to finalize
                check = KaldiRecognizer(self.model, 16000)
                for i in range(0, len(tail) - FRAME + 1, FRAME):
                    check.AcceptWaveform(tail[i:i + FRAME])
                heard = json.loads(check.FinalResult()).get("text", "")
                if match(heard):
                    return t, f"{text}  | check: {heard}"
                rec.Reset()  # rejected: keep listening
        return None, ""


def load_clips(folder: Path):
    clips = []
    for p in sorted(folder.glob("*.wav")):
        with wave.open(str(p)) as w:
            pcm = w.readframes(w.getnframes())
        label = p.with_suffix(".txt").read_text(encoding="utf-8").strip().split("\t")
        clips.append((p.stem, p.stem.startswith("pos"), pcm, label[-1], label[0]))
    return clips


def main() -> None:
    verbose = "-v" in sys.argv
    SetLogLevel(-1)
    cfg = load_config()
    args = [a for a in sys.argv[1:] if not a.startswith("-")] or ["wake_corpus"]
    base = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    clips = [c for name in args for c in load_clips(base / name)]
    pos = [c for c in clips if c[1]]
    neg = [c for c in clips if not c[1]]
    print(f"{len(neg)} clips that must not wake, {len(pos)} that must\n")
    model = Model(str(cfg.vosk_model_path))
    strategies = [Current(model), Decoys(model), FinalConf(model, 0.0),
                  Confirmed(model, 0.2), Confirmed(model, 0.35), Confirmed(model, 0.5),
                  Confirmed(model, 0.2, decoys=True), Confirmed(model, 0.35, decoys=True)]
    if "--all" in sys.argv:
        strategies += [FinalConf(model, 0.8), FinalConf(model, 0.95), Free(model), Verified(model)]
    base_times = {}
    for s in strategies:
        t0 = time.monotonic()
        false = [(n, text, heard) for n, is_pos, pcm, text, voice in neg if (r := s.run(pcm))[0] is not None
                 for heard in [r[1]]]
        hits, misses, delays = 0, [], []
        for n, is_pos, pcm, text, voice in pos:
            t, heard = s.run(pcm)
            if t is None:
                misses.append((n, voice, text))
            else:
                hits += 1
                if isinstance(s, Current):
                    base_times[n] = t
                elif n in base_times:
                    delays.append(t - base_times[n])
        audio = sum((len(c[2]) + len(LEAD) + len(TAIL)) / 32000 for c in clips) * 2  # neg and pos were each run once
        speed = audio / (time.monotonic() - t0)
        delay = f", +{sum(delays) / len(delays):.2f} s slower than A on average" if delays else ""
        print(f"{s.name}\n   false triggers: {len(false)}/{len(neg)}   hits: {hits}/{len(pos)}{delay}   speed: {speed:.0f}x real time")
        if verbose or len(false) <= 6:
            for n, text, heard in false:
                print(f"      FALSE  {text!r}  ->  {heard!r}")
        for n, voice, text in misses:
            print(f"      MISSED {n} ({voice}) {text!r}")
        print()


if __name__ == "__main__":
    main()
