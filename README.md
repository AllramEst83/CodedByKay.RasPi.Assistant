# Gemini voice assistant for Raspberry Pi 5

A private, hands-free voice assistant that runs on a Raspberry Pi 5, in the spirit of a Google Nest, built on
the Gemini Live API. Say **"Hey Gemini"** or **"Hey Assistant"**, hear a chime and a short "Online", and talk.
It answers aloud in the language you speak (Swedish or English), can search the web, sets timers, and goes
back to sleep when you say **"go to sleep"** or after 8 seconds of silence.

```
 mic ──► Vosk (offline wake phrase, on the Pi) ──► chime + "Online"
                                                        │
 speaker ◄── Gemini Live (WebSocket, streaming audio) ◄─┘   search · timers · go to sleep
```

The Pi sends **no audio to the internet until you say the wake phrase.** Wake-word detection is offline
(Vosk, no vendor account or key). The network is used only while a conversation is open.

## Features

- Offline wake phrases "Hey Gemini" / "Hey Assistant", very fast, about 5% of one CPU core while idle
- Real-time spoken conversation with Gemini Live (model `gemini-3.8-live`); interrupt it by speaking over it
- Answers in the language of your question; short, spoken-style replies
- Follow-up questions without the wake word; the session closes after 8 s of silence
- **"Go to sleep"** ends the conversation immediately, with a falling chime
- Google Search grounding for current facts (weather, news, prices)
- Voice timers: "Sätt en timer på fem minuter"; they ring on the device even after the conversation ends
- Audible error feedback in a retro robot voice: running out of credits, bad key, no internet, overloaded
- Colored, tagged logs; `--doctor` self-check; wake-word-only test mode; use your PC's mic and speakers over SSH

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 5 (8 GB), official 27 W USB-C power supply | the Pi 5 has no 3.5 mm jack and no mic |
| USB microphone | any class-compliant USB mic |
| Speaker | a powered speaker through a USB sound card (3.5 mm), a USB speaker, or a USB speakerphone |
| microSD card, network (Wi-Fi or Ethernet) | headless: no monitor or keyboard needed |

Bluetooth speakers are out of scope. USB audio devices are detected automatically (HDMI is skipped), and
devices that only run at 44.1/48 kHz or in stereo are handled by converting in code.

## Set it up

Full headless setup (flashing, SSH, Wi-Fi) is in
[`_bmad-output/specs/spec-gemini-voice-assistant/pi-setup.md`](_bmad-output/specs/spec-gemini-voice-assistant/pi-setup.md).
In short, on a Pi with Raspberry Pi OS Lite 64-bit:

```bash
sudo apt install -y python3-venv portaudio19-dev libasound2-dev alsa-utils espeak-ng
mkdir -p ~/gemini-assistant && cd ~/gemini-assistant        # copy the contents of src/ here (see below)
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
mkdir -p models && cd models                                  # the small English Vosk model, about 40 MB
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip && unzip vosk-model-small-en-us-0.15.zip && cd ..
cp .env.example .env && chmod 600 .env && nano .env           # set GEMINI_API_KEY
./venv/bin/python -m assistant --doctor                        # check everything
./venv/bin/python -m assistant                                 # run it
```

From the PC, copy the code to the Pi (it never contains `.env`):

```
scp -r src/. gemini-pi:gemini-assistant/
```

Get a Gemini API key at [Google AI Studio](https://aistudio.google.com/). Live audio needs a billing-enabled
project; set a spending limit there.

## Use it

| Say | What happens |
|---|---|
| "Hey Gemini" / "Hey Assistant" | chime, "Online", then speak your question |
| a follow-up within 8 s | answered without the wake phrase |
| "Go to sleep" | goodbye, falling chime, back to idle |
| "Set a timer for ten minutes" | confirmed aloud; rings later with a tone and a spoken message |
| a question about now ("How warm is it in Stockholm?") | Gemini searches the web |

Everything is configurable in `.env` (voice, timeout, greeting, which tools are on, and more). The full table of
settings, service commands, log colors and troubleshooting is in **[`docs/COMMANDS.md`](docs/COMMANDS.md)**.

### Autostart on boot

```
./deploy/install-service.sh          # on the Pi; needs sudo
sudo journalctl -u gemini-assistant -f
```

### Try it before the Pi has a mic and speaker

`python src/tools/pc_audio_bridge.py` (PC, `pip install sounddevice`) streams your PC's microphone to the Pi
over SSH and plays the replies on your PC speakers. Add `--wake-test` to only count wake-phrase triggers,
with no Gemini calls and no cost, for example while the TV is on.

## Commands at a glance

```
python -m assistant                  # run the assistant
python -m assistant --doctor         # check config, wake word, audio, network and the Gemini connection
python -m assistant --wake-test      # listen for the wake phrases only (no Gemini calls)
python -m assistant --list-devices   # audio devices the Pi sees
python -m assistant --simulate IN.wav [--play]   # run once with a 16 kHz mono wav as the microphone
```

## Develop and test

Code is written on a PC in `src/` and runs on the Pi. Tests need no network or audio devices:

```
./venv/bin/python -m pytest -m "not integration" -q     # on the Pi: offline tests
./venv/bin/python -m pytest -m integration -q           # real Gemini calls (uses credits)
```

Spoken test clips are generated with Gemini itself (`src/tools/make_fixtures.py`). The design decisions and the
spec this was built from are in `_bmad-output/specs/spec-gemini-voice-assistant/` (`SPEC.md`, `stack.md`,
`stories.yaml`); [`CLAUDE.md`](CLAUDE.md) describes the architecture and the verified Gemini Live details.

## Status

Built and tested without real audio hardware: wake word, Live conversations in Swedish and English, search,
timers, sleep, error feedback, PC bridge. The wake word is confirmed before it fires, because the English model
can mishear Swedish speech as "hey assistant" (12 of 81 look-alike test sentences falsely woke it; now 1).
Still to verify on the Pi's own mic and speaker: the false-trigger rate with real speech, speaker echo, and barge-in.

Personal, non-commercial home project.
