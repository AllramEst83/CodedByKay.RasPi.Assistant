"""Generate a wake-word test corpus with Gemini Live (text in, speech out): sentences that must NOT wake the
assistant (tricky Swedish and English look-alikes) and wake phrases that must, in several voices.

Run on the Pi:  python tools/make_wake_corpus.py      -> tests/fixtures/wake_corpus/{neg,pos}_*.wav (16 kHz mono)
Then:           python tools/eval_wake.py

The voices are synthetic, so this is a proxy for real speech: use it to compare detection strategies, and confirm
with real speech (`python -m assistant --wake-test`).
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

NEGATIVE = [  # must not wake
    "Vad heter huvudstaden i Frankrike?", "Vad heter huvudstaden i Sverige?", "Hur många invånare har Stockholm?",
    "Jag tänker laga middag ikväll.", "Kan du hjälpa mig med en sak?", "Vad blir det för väder imorgon?",
    "Hej, hur mår du idag?", "Heter du Gemini eller något annat?", "Det här är en bra assistent.",
    "Jag glömde nycklarna hemma.", "Vem vann matchen igår?", "Vad heter den där staden i Norge?",
    "Hejdå, vi ses imorgon.", "Han heter Erik och hon heter Anna.", "Kan du stänga fönstret, det är kallt.",
    "Jag ska åka till Göteborg på fredag.", "Vilken tid börjar filmen?", "Hallå, är du där?",
    "Vad kostar en biljett till Malmö?", "Titta, där är en stor hund.", "Hur lång tid tar det att koka potatis?",
    "Det heter inte så, det heter huvudstad.", "Vad har du gjort idag?", "Jag behöver en ny telefon.",
    "Spela lite musik, tack.", "Vad heter huvudstaden i Danmark, och vad heter hovedstaden i Norge?",
    "What is the capital of France?", "I think the system is working fine.", "Hey, how are you doing today?",
    "Can you assist me with this?", "He is a good assistant manager.", "Let's go to the gym tomorrow.",
    "The weather is nice today.", "Hey Google, what time is it?", "I need a new system for my computer.",
    "Hey Alexa, turn on the lights.", "That is a gemini constellation.", "She said hey and left.",
    "Did you hear what the assistant said?", "We should meet at the station.", "The geminis are born in June.",
]
HELDOUT_NEGATIVE = [  # a second set, written after the first results: for checking that tuning did not overfit
    "Vad heter huvudstaden i Italien?", "Huvudstaden heter Helsingfors.", "Hur heter din assistent?",
    "Vad heter staden där du bor?", "Heter hon Maria eller Marie?", "Han heter Gustav och bor i Stockholm.",
    "Vad heter huvudstaden i Tyskland, och när grundades den?", "Hejsan, vad heter du egentligen?",
    "Det är ett bra system för att hålla ordning.", "Kan du assistera mig med läxorna?",
    "Vi hade en lång och trevlig middag igår.", "Jag heter Kay och jag bor i Sverige.", "Hej hej, vad händer?",
    "Gemini är ett stjärntecken, tvillingarna.", "Det var en riktigt bra film, tycker jag.",
    "Vad ska vi äta till middag idag?", "Hon sa hej och gick därifrån.", "Ska vi ta en promenad nu?",
    "Vilken färg har huset på gatan?", "Hur stor är den största staden i Europa?",
    "What is the largest city in Europe?", "He said hello and walked away.", "I need some assistance with this.",
    "The system is down again, can you check?", "Hey there, long time no see.", "Hey Siri, set an alarm.",
    "What is your name, and where do you live?", "This is a great assistant for the kitchen.",
    "Okay Google, play some music.", "Hey Jim, are you coming tonight?", "It is hot in here, open the window.",
    "My cousin's name is Jemima.", "Tell me a story about a gentle giant.", "Hey, what's up with the weather?",
    "A gem of a place, honestly.", "Please assist the next customer.", "The hay is stored in the barn.",
    "He has a system for everything.", "Hey you, over there!", "Is the station far from here?",
]
POSITIVE = ["Hey Gemini.", "Hey Assistant.", "Okej, hey Gemini.", "Hello, hey Assistant."]  # must wake
FOLLOWED = [  # wake phrase straight into a question, no pause: must wake (the phrase is followed by speech)
    "Hey Gemini, vad är klockan?", "Hey Assistant, what is the weather like today?",
    "Hey Gemini vad heter huvudstaden i Norge?", "Hey Assistant, set a timer for five minutes.",
]
VOICES = ["Kore", "Puck", "Charon", "Aoede"]
PROMPT = ("You are a text-to-speech engine. The user message contains a quoted text. Say exactly the text inside "
          "the quotes, word for word, naturally, and nothing else. Do not answer it or comment on it.")


async def speak(client, cfg, text: str, voice: str) -> bytes:
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"], system_instruction=PROMPT,
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
    )
    pcm = bytearray()
    async with client.aio.live.connect(model=cfg.model, config=config) as s:
        await s.send_realtime_input(text=f'Say: "{text}"')
        async for m in s.receive():
            sc = m.server_content
            if sc is None:
                continue
            for p in (sc.model_turn.parts if sc.model_turn else []):
                if p.inline_data and p.inline_data.data:
                    pcm += p.inline_data.data
            if sc.turn_complete:
                break
    return bytes(pcm)


def to_16k(pcm24: bytes) -> bytes:
    x = np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
    return np.clip(resample_poly(x, INPUT_RATE, OUTPUT_RATE), -32768, 32767).astype(np.int16).tobytes()


async def main() -> None:
    cfg = load_config()
    heldout = "heldout" in sys.argv  # python tools/make_wake_corpus.py heldout
    followed = "followed" in sys.argv  # python tools/make_wake_corpus.py followed
    out = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / (
        "wake_corpus_followed" if followed else "wake_corpus_heldout" if heldout else "wake_corpus")
    out.mkdir(parents=True, exist_ok=True)
    client = make_client(cfg)
    jobs = [("neg", i, t, VOICES[(i + (2 if heldout else 0)) % len(VOICES)])
            for i, t in enumerate(HELDOUT_NEGATIVE if heldout else NEGATIVE)]
    if followed:
        jobs = []
    jobs += [("pos", i * len(VOICES) + j, t, v) for i, t in enumerate(FOLLOWED if followed else POSITIVE)
             for j, v in enumerate(VOICES)]
    sem = asyncio.Semaphore(3)  # a few at a time

    async def one(kind, i, text, voice):
        async with sem:
            for attempt in range(3):
                try:
                    pcm = await speak(client, cfg, text, voice)
                    if pcm:
                        write_wav_pcm16(out / f"{kind}_{i:02d}.wav", to_16k(pcm), INPUT_RATE)
                        (out / f"{kind}_{i:02d}.txt").write_text(f"{voice}\t{text}\n", encoding="utf-8")
                        return
                except Exception as e:  # noqa: BLE001
                    print(f"retry {kind}_{i:02d}: {type(e).__name__}: {str(e)[:80]}")
                    await asyncio.sleep(1.5)
            print(f"FAILED {kind}_{i:02d} {text!r}")

    await asyncio.gather(*(one(*j) for j in jobs))
    print(f"wrote {len(list(out.glob('*.wav')))} clips to {out}")


if __name__ == "__main__":
    asyncio.run(main())
