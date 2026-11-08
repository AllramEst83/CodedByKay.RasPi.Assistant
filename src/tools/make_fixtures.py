"""Generate spoken test clips with Gemini Live (text in, speech out) as 16 kHz mono wavs.

These stand in for a human voice in tests, so they are a proxy, not a substitute for real-mic testing.
Run on the Pi:  python tools/make_fixtures.py
"""
import asyncio
import sys
from pathlib import Path

import numpy as np
from google.genai import types
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant.audio import write_wav_pcm16  # noqa: E402
from assistant.config import INPUT_RATE, OUTPUT_RATE, load_config  # noqa: E402
from assistant.session import make_client  # noqa: E402

CLIPS = {
    "hey_gemini": "Hey Gemini.",
    "hey_assistant": "Hey Assistant.",
    "question_sv": "Vad heter huvudstaden i Frankrike?",
    "question_en": "What is two plus two?",
}
SPEAKER_PROMPT = (
    "You are a text-to-speech engine. The user message contains a quoted text. "
    "Say exactly the text inside the quotes, word for word, in a neutral clear voice, and nothing else. "
    "Do not answer it or comment on it."
)


async def speak(client, cfg, text: str) -> tuple[bytes, str]:
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=SPEAKER_PROMPT,
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    pcm, said = bytearray(), ""
    async with client.aio.live.connect(model=cfg.model, config=config) as s:
        await s.send_realtime_input(text=f'Say: "{text}"')
        async for m in s.receive():
            sc = m.server_content
            if sc is None:
                continue
            for p in (sc.model_turn.parts if sc.model_turn else []):
                if p.inline_data and p.inline_data.data:
                    pcm += p.inline_data.data
            if sc.output_transcription and sc.output_transcription.text:
                said += sc.output_transcription.text
            if sc.turn_complete:
                break
    return bytes(pcm), said


def to_16k(pcm24: bytes) -> bytes:
    x = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    y = resample_poly(x, INPUT_RATE, OUTPUT_RATE)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


async def main() -> None:
    cfg = load_config()
    out = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    out.mkdir(parents=True, exist_ok=True)
    client = make_client(cfg)
    for name, text in CLIPS.items():
        pcm, said = await speak(client, cfg, text)
        write_wav_pcm16(out / f"{name}.wav", to_16k(pcm), INPUT_RATE)
        print(f"{name}: {len(pcm) / (OUTPUT_RATE * 2):.1f}s  asked={text!r}  said={said.strip()!r}")


if __name__ == "__main__":
    asyncio.run(main())
