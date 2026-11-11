---
id: SPEC-gemini-voice-assistant
companions: [pi-setup.md, stack.md]
sources: [../../../docs/plan.md]
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Gemini Voice Assistant on Raspberry Pi 5

## Why

A vision to realize: Kay wants a hands-free, voice-only Gemini assistant on a Raspberry Pi 5 they own, woken by a spoken wake word. A local wake word keeps audio off the network until the user asks for it, so privacy is the design anchor. The Pi is a repurposed old Docker web server that must be reset to a clean state first.

## Capabilities

- **CAP-1**
  - **intent:** The device listens only locally for "Hey Gemini" or "Hey Assistant" while idle, using offline Vosk speech recognition.
  - **success:** A network capture while idle shows zero audio egress; each phrase, spoken in normal voice at about 2 m with a Swedish accent, triggers activation; false activations from ordinary TV or conversation are rare enough to tolerate (target to be measured, see Open Questions).
- **CAP-2**
  - **intent:** The user hears a confirmation chime on wake-word detection before the live session opens.
  - **success:** Chime plays on every detection, generated in code with no asset files.
- **CAP-3**
  - **intent:** After activation the user speaks and hears Gemini's spoken reply in real time.
  - **success:** A spoken question is answered aloud, with first reply audio within about 2 s of the end of speech on home broadband.
- **CAP-4**
  - **intent:** The user can interrupt the assistant mid-reply (barge-in).
  - **success:** Speaking over a reply stops playback and the new utterance is answered.
- **CAP-5**
  - **intent:** Follow-ups need no wake word while the session is active; after 8 s without speech from either party the session closes and the device returns to idle. Saying "go to sleep" closes it at once (Gemini calls a `go_to_sleep` tool, says a short goodbye, a falling chime plays).
  - **success:** A follow-up within 8 s works without the wake word, and user speech restarts the 8 s window; after 8 s of silence the log shows the session closed and the wake word is required again.
- **CAP-6**
  - **intent:** Replies are short, spoken-style and markdown-free, Swedish by default and English when the user speaks English.
  - **success:** Sampled replies are 1–3 sentences, contain no markdown, and follow the language rule.
- **CAP-7**
  - **intent:** The Pi is set up and operated entirely headless over SSH from the Windows PC.
  - **success:** From a freshly flashed Pi, a running assistant is reached using only SSH, with no monitor or keyboard on the Pi.
- **CAP-8**
  - **intent:** The Pi is returned to a known-clean state, discarding the old Docker web-server setup.
  - **success:** After reflash no Docker, containers or old user data remain, and the Pi boots to an SSH-reachable fresh OS.

## Constraints

- Audio hardware is required: the Pi 5 has no 3.5 mm jack and no mic, so a USB mic and a wired speaker (USB speakerphone, USB headset, or a 3.5 mm speaker via a USB sound adapter) must be chosen; Bluetooth is out of scope for now; the source plan omits this.
- Power is the official Raspberry Pi 27 W USB-C supply (EU, 5.1 V/5 A, confirmed by photo); keep it, since weaker supplies limit USB current to audio devices.
- No third-party account or vendor key for wake-word detection: it runs on-device with Vosk and a local model, restricted to a small phrase grammar. Phrase words must exist in the model vocabulary ("gemini", "assistant" do; "gimini" does not).
- Wake-word detection must leave the Pi fast enough to respond; Vosk uses more CPU than a dedicated engine, so idle CPU must be measured.
- The mic has a single owner at a time: the wake-word stream closes before the live session opens and vice versa; both need 16 kHz mono 16-bit PCM.
- The network is needed only while a session is active; API keys live in a `chmod 600` `.env` on the Pi and are never committed.
- Modern Raspberry Pi OS has no preset `pi` user; the username is chosen in Imager, and all `/home/pi/...` paths and the `pscp` target in the source plan must match it.
- The Pi (Raspberry Pi 5 8 GB, built-in Wi-Fi) reaches the PC through the LAN, not a direct cable: Wi-Fi configured at flash time, Ethernet as fallback; see `pi-setup.md`.
- The code in the source plan is a proposal; its SDK calls and model name must be verified against the current `google-genai` SDK (see `stack.md`).

## Non-goals

- No screen or UI.
- No multi-room or multi-user support.
- No custom tool use beyond `go_to_sleep`, and no smart-home control. Google Search grounding (built into the Live API, `GOOGLE_SEARCH`) is allowed so answers can use current facts.
- No Docker or web-server role for the Pi.
- No cloud-based wake-word detection.
- No vendor-hosted or account-bound wake-word engine (e.g. Picovoice).
- No mobile app.

## Success signal

From a freshly flashed Pi, Kay says "Hey Gemini", hears the chime, asks a question and hears a spoken Swedish answer, asks a follow-up without the wake word, and after silence the device returns to idle.

## Assumptions

- Personal, home, non-commercial use only; Swedish primary language, English when the user speaks English; the PC runs Windows 11.
- The Pi is reflashed from scratch (Raspberry Pi OS Lite 64-bit), so nothing from the old Docker setup is kept.
- Headless SSH is the setup route (PuTTY, or Windows' built-in `ssh`).
- Chosen defaults, adjustable: 8 s idle timeout, reset by speech from either party.
- Audio hardware is the cheapest class-compliant USB mic and speaker (or one USB speakerphone) bought in Sweden.

## Open Questions

- Which USB mic and speaker (or speakerphone) will be bought? Confirm ALSA recognises it and captures at 16 kHz.
- Vosk false-trigger rate is still unmeasured (one false trigger in four test clips, a small sample); if too high, tighten the grammar or fall back to a dedicated engine. Idle CPU is measured and fine: about 5% of one core, 56 °C, no throttling, 203 MB (Pi 5 8 GB, 2026-11-11).
