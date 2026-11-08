# Projektplan & Implementation: Gemini Live Voice Assistant med Wake Word på Raspberry Pi 5

Den här guiden visar hur du bygger en intelligent röstassistent på Raspberry Pi 5 som aktiveras av kommandot **"Hey Gemini"**, kommunicerar via **Gemini Live API** och konfigureras helt via PuTTY.

---

## 1. Systemarkitektur med Wake Word

För att assistenten inte ska strömma ljud till Google dygnet runt använder vi en tvåstegsarkitektur:

```
[Mikrofon]
    │
    ▼
[STEG 1: Viloläge - Lokalt Wake Word (Picovoice Porcupine)]
    │  - Körs 100% lokalt på Pi 5 (< 2% CPU)
    │  - Noll nätverkstrafik, full integritet
    │
    ├─► Hörs inte: Fortsätt lyssna lokalt...
    │
    └─► "Hey Gemini" detekterat!
            │
            ├─► Spela upp bekräftelseton ("Ding!")
            │
            ▼
[STEG 2: Aktiv session - Gemini Live API (WebSocket WSS)]
    │  - Strömmar 16 kHz PCM till Google
    │  - Tar emot 24 kHz PCM svar i realtid
    │  - Stöd för "Barge-in" (avbryt assistenten när du vill)
    │  - 8 sekunders tystnad efter svar ──► Stäng session & återgå till Steg 1
```

---

## 2. Förbered "Hey Gemini" Wake Word

Vi använder **Picovoice Porcupine**, industristandarden för lokala wake words på ARM/Raspberry Pi. Det är kostnadsfritt för personligt bruk och låter dig generera en skräddarsydd modell för just frasen *"Hey Gemini"*.

### Steg 2.1: Skapa nyckel och ladda ner modellen
1. Gå till [console.picovoice.ai](https://console.picovoice.ai/) och skapa ett gratis konto.
2. Kopiera din personliga **AccessKey** från översikten.
3. Klicka på **Porcupine** i menyn $\rightarrow$ fliken **Wake Words**.
4. Skriv in `Hey Gemini` som fras och välj språk `English` (fungerar utmärkt med svensk/engelsk brytning).
5. Välj plattform: **Raspberry Pi** (Cortex-A76 / 64-bit).
6. Klicka på **Download** för att ladda ner filen (den heter något i stil med `Hey-Gemini_en_raspberry-pi_v3_0_0.ppn`).

### Steg 2.2: För över `.ppn`-filen till din Raspberry Pi
Du kan föra över filen från din PC till din Pi med verktyg som **WinSCP** (använder samma IP och inloggning som PuTTY) eller via `pscp` i Windows kommandotolk:
```cmd
pscp "C:\sökväg\till\Hey-Gemini_en_raspberry-pi_v3_0_0.ppn" pi@RASPBERRY_PI_IP:/home/pi/gemini-assistant/hey_gemini.ppn
```
*(Döp gärna om filen till `hey_gemini.ppn` på din Pi för enkelhetens skull).*

---

## 3. Förberedelser & Anslutning via PuTTY

### Steg 3.1: Anslut med PuTTY
1. Starta din Pi 5 med mikrofon och högtalare inkopplade.
2. Öppna **PuTTY**, ange din Raspberry Pi:s IP-adress och anslut via SSH på port `22`.
3. Logga in med dina användaruppgifter.

### Steg 3.2: Installera systempaket och ljudberoenden
Kör följande kommandon i PuTTY:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv portaudio19-dev libasound2-dev alsa-utils git
```

### Steg 3.3: Kontrollera mikrofon och högtalare
```bash
# Identifiera inspelningsenheter
arecord -l

# Identifiera uppspelningsenheter
aplay -l

# Testa inspelning i 3 sekunder och spela upp
arecord -d 3 -f cd test.wav && aplay test.wav
rm test.wav
```
*(Justera volym och mikrofonkänslighet med kommandot `alsamixer` vid behov).*

---

## 4. Konfigurera Python-miljön

Skapa projektmappen och installera nödvändiga bibliotek i en virtuell miljö:

```bash
mkdir -p ~/gemini-assistant
cd ~/gemini-assistant

# Skapa virtuell miljö
python3 -m venv venv
source venv/bin/activate

# Installera beroenden
pip install --upgrade pip
pip install google-genai pvporcupine pyaudio python-dotenv
```

### Skapa `.env`-filen med dina API-nycklar
Skapa konfigurationsfilen:
```bash
nano .env
```
Klistra in och fyll i dina nycklar:
```ini
GEMINI_API_KEY="DIN_GEMINI_API_NYCKEL_HÄR"
PICOVOICE_ACCESS_KEY="DIN_PICOVOICE_ACCESS_KEY_HÄR"
WAKE_WORD_PATH="/home/pi/gemini-assistant/hey_gemini.ppn"
```
Spara med `Ctrl+O`, tryck `Enter`, och stäng med `Ctrl+X`. Sätt säkra rättigheter:
```bash
chmod 600 .env
```

---

## 5. Komplett Källkod (`assistant.py`)

Skapa huvudskriptet:
```bash
nano assistant.py
```

Klistra in källkoden nedan:

```python
import asyncio
import math
import os
import struct
import sys
import time
import pvporcupine
import pyaudio
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# --- Miljövariabler & Inställningar ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PICOVOICE_KEY = os.getenv("PICOVOICE_ACCESS_KEY")
WAKE_WORD_PATH = os.getenv("WAKE_WORD_PATH", "hey_gemini.ppn")

if not GEMINI_API_KEY or not PICOVOICE_KEY:
    print("Fel: Säkerställ att både GEMINI_API_KEY och PICOVOICE_ACCESS_KEY finns i .env!")
    sys.exit(1)

# Ljudspecifikationer
AUDIO_FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000     # Både Porcupine och Gemini kräver 16 kHz indata
OUTPUT_RATE = 24000    # Gemini returnerar 24 kHz utdata
LIVE_CHUNK_SIZE = 1024
SILENCE_TIMEOUT = 7.0  # Sekunder av inaktivitet innan assistenten återgår till sömn

MODEL_NAME = "gemini-2.0-flash-exp"

SYSTEM_PROMPT = """
Du är en intelligent, hjälpsam och trevlig röstassistent på en Raspberry Pi 5.
Regler för dina svar:
1. Svara alltid på naturlig svenska såvida inte användaren pratar engelska.
2. Håll svaren korta, koncisa och muntliga (1-3 meningar) eftersom de läses upp direkt i högtalare.
3. Använd aldrig markdown som asterisker (*), punktlistor eller tabeller.
"""

def generate_chime():
    """Genererar en mjuk tvåtons-pling i 24 kHz PCM utan externa ljudfiler."""
    tones = [(523.25, 0.08), (659.25, 0.12)] # C5 följt av E5
    sample_rate = OUTPUT_RATE
    audio = bytearray()
    
    for freq, duration in tones:
        num_samples = int(sample_rate * duration)
        for i in range(num_samples):
            envelope = math.sin(math.pi * (i / num_samples)) # Mjuk fade in/out
            val = int(32767.0 * 0.25 * envelope * math.sin(2.0 * math.pi * freq * (i / sample_rate)))
            audio.extend(struct.pack("<h", val))
    return bytes(audio)

CHIME_SOUND = generate_chime()

class GeminiAssistantWithWakeWord:
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.porcupine = None
        self.client = genai.Client(
            api_key=GEMINI_API_KEY, 
            http_options={'api_version': 'v1alpha'}
        )
        self.output_stream = None
        self.init_porcupine()

    def init_porcupine(self):
        if not os.path.exists(WAKE_WORD_PATH):
            print(f"Varning: Wake word-filen '{WAKE_WORD_PATH}' hittades inte!")
            print("Kontrollera att du laddat upp hey_gemini.ppn till mappen.")
            sys.exit(1)

        self.porcupine = pvporcupine.create(
            access_key=PICOVOICE_KEY,
            keyword_paths=[WAKE_WORD_PATH],
            sensitivities=[0.5]  # Känslighet mellan 0.0 och 1.0
        )

    def play_chime(self):
        """Spelar upp bekräftelseljudet i högtalaren."""
        try:
            stream = self.p.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=OUTPUT_RATE,
                output=True
            )
            stream.write(CHIME_SOUND)
            stream.stop_stream()
            stream.close()
        except Exception as e:
            print(f"Kunde inte spela upp chime: {e}")

    def listen_for_wake_word(self):
        """Lyssnar lokalt efter 'Hey Gemini' i ett synkront block."""
        porcupine_stream = self.p.open(
            rate=self.porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=self.porcupine.frame_length
        )

        print("\n[Viloläge] Säg 'Hey Gemini' för att aktivera...")
        try:
            while True:
                pcm = porcupine_stream.read(self.porcupine.frame_length, exception_on_overflow=False)
                pcm_unpacked = struct.unpack_from("h" * self.porcupine.frame_length, pcm)
                result = self.porcupine.process(pcm_unpacked)
                if result >= 0:
                    print("\n[Aktiv] 'Hey Gemini' uppfattat!")
                    return True
        finally:
            porcupine_stream.stop_stream()
            porcupine_stream.close()

    async def run_live_session(self):
        """Kör en aktiv Gemini Live API WebSocket-session."""
        self.play_chime()

        # Öppna mikrofon och högtalare för Live API
        input_stream = self.p.open(
            format=AUDIO_FORMAT,
            channels=CHANNELS,
            rate=INPUT_RATE,
            input=True,
            frames_per_buffer=LIVE_CHUNK_SIZE
        )

        self.output_stream = self.p.open(
            format=AUDIO_FORMAT,
            channels=CHANNELS,
            rate=OUTPUT_RATE,
            output=True,
            frames_per_buffer=LIVE_CHUNK_SIZE
        )

        audio_in_queue = asyncio.Queue()
        last_activity = time.time()
        session_active = True

        def mic_worker(loop):
            while session_active:
                try:
                    data = input_stream.read(LIVE_CHUNK_SIZE, exception_on_overflow=False)
                    loop.call_soon_threadsafe(audio_in_queue.put_nowait, data)
                except Exception:
                    break

        loop = asyncio.get_running_loop()
        import threading
        mic_thread = threading.Thread(target=mic_worker, args=(loop,), daemon=True)
        mic_thread.start()

        async def send_audio(session):
            nonlocal last_activity
            while session_active:
                data = await audio_in_queue.get()
                await session.send(input={"data": data, "mime_type": "audio/pcm"}, end_of_turn=False)

        async def receive_audio(session):
            nonlocal last_activity
            async for response in session.receive():
                server_content = response.server_content
                if server_content is not None:
                    if server_content.model_turn is not None:
                        for part in server_content.model_turn.parts:
                            if part.text:
                                print(part.text, end="", flush=True)
                            if part.inline_data:
                                last_activity = time.time()
                                await loop.run_in_executor(
                                    None, 
                                    self.output_stream.write, 
                                    part.inline_data.data
                                )
                    if server_content.turn_complete:
                        last_activity = time.time()
                    if server_content.interrupted:
                        print("\n[Avbruten]")

        async def inactivity_monitor():
            nonlocal session_active
            while session_active:
                await asyncio.sleep(0.5)
                # Om modellen varit tyst i mer än SILENCE_TIMEOUT sekunder
                if (time.time() - last_activity) > SILENCE_TIMEOUT:
                    print(f"\n[Timeout] Ingen aktivitet på {SILENCE_TIMEOUT}s. Återgår till viloläge.")
                    session_active = False
                    break

        config = types.LiveConnectConfig(
            response_modalities=[types.LiveServerContentModality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
                )
            ),
            system_instruction=types.Content(parts=[types.Part.from_text(SYSTEM_PROMPT)])
        )

        try:
            print("[Status] Öppnar Live-kanal...")
            async with self.client.aio.live.connect(model=MODEL_NAME, config=config) as session:
                print("[Status] Prata fritt med Gemini...")
                send_task = asyncio.create_task(send_audio(session))
                receive_task = asyncio.create_task(receive_audio(session))
                monitor_task = asyncio.create_task(inactivity_monitor())

                # Vänta tills sessionen avslutas pga inaktivitet
                await monitor_task
                send_task.cancel()
                receive_task.cancel()
        except Exception as e:
            print(f"\n[Fel i Live-session]: {e}")
        finally:
            session_active = False
            input_stream.stop_stream()
            input_stream.close()
            self.output_stream.stop_stream()
            self.output_stream.close()

    def main_loop(self):
        """Huvudloop som växlar mellan Wake Word och aktiv Gemini-session."""
        try:
            while True:
                # 1. Vänta lokalt på wake word
                if self.listen_for_wake_word():
                    # 2. Kör aktiv session med Gemini
                    asyncio.run(self.run_live_session())
        except KeyboardInterrupt:
            print("\nAvslutar assistenten...")
        finally:
            if self.porcupine:
                self.porcupine.delete()
            self.p.terminate()

if __name__ == "__main__":
    app = GeminiAssistantWithWakeWord()
    app.main_loop()
```

Spara med `Ctrl+O`, `Enter`, och `Ctrl+X`.

---

## 6. Provkörning via PuTTY

Starta assistenten i din aktiverade virtuella miljö:

```bash
cd ~/gemini-assistant
source venv/bin/activate
python assistant.py
```

### Så här testar du:
1. I PuTTY visas texten:
   ```text
   [Viloläge] Säg 'Hey Gemini' för att aktivera...
   ```
2. Säg tydligt: **"Hey Gemini"**.
3. Du hör en snabb bekräftelseton (**"Ding!"**) ur högtalaren, och texten `[Aktiv] 'Hey Gemini' uppfattat!` visas.
4. Ställ din fråga direkt (t.ex. *"Vad är jordens omkrets?"*).
5. Gemini svarar direkt via röst.
6. Om du vill ställa en följdfråga kan du göra det inom 7 sekunder utan att behöva säga "Hey Gemini" igen.
7. Om du är tyst i 7 sekunder ser du `[Timeout] Återgår till viloläge` och assistenten väntar lugnt på nästa "Hey Gemini".

---

## 7. Finjustering & Tips

* **Känslighet för Wake Word:** I koden är `sensitivities=[0.5]`. Om den inte reagerar så lätt kan du höja till `0.6` eller `0.7`. Om den reagerar på falsklarm (t.ex. från TV:n), sänk till `0.35`–`0.4`.
* **Byt röst:** Under `prebuilt_voice_config` kan du välja mellan: `Puck`, `Charon`, `Kore`, `Fenrir` eller `Aoede`.
* **Timeout-tid:** Ändra `SILENCE_TIMEOUT = 7.0` till önskat antal sekunder om du vill ha längre eller kortare tid på dig att ställa följdfrågor innan assistenten somnar.