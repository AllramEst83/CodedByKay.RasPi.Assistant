# Commands cheat sheet

Where to run each command: **[Pi]** in an SSH/PuTTY session on the Raspberry Pi, **[PC]** in a terminal on your Windows computer.

The Pi is `gemini-pi` (`gemini-pi.local`), user `allramest`, project folder `~/gemini-assistant`.

## Connect

```
[PC]  ssh allramest@gemini-pi.local        # or: ssh gemini-pi, if you have it in ~/.ssh/config
[Pi]  hostname -I                          # the Pi's IP address (capital I)
```

## The autostart service

Install once (needs sudo, asks for your password). Do this after the mic and speaker are connected:

```
[Pi]  cd ~/gemini-assistant
[Pi]  ./deploy/install-service.sh              # install, enable at boot, start now
[Pi]  ./deploy/install-service.sh --no-start   # install and enable at boot, but do not start yet
```

Daily use:

| What | Command |
|---|---|
| Is it running? | `systemctl status gemini-assistant` |
| Start | `sudo systemctl start gemini-assistant` |
| Stop (until next boot) | `sudo systemctl stop gemini-assistant` |
| Restart (after changing code or `.env`) | `sudo systemctl restart gemini-assistant` |
| Turn autostart on again and start now | `sudo systemctl enable --now gemini-assistant` |
| Turn autostart off and stop now | `sudo systemctl disable --now gemini-assistant` |
| Is autostart on? | `systemctl is-enabled gemini-assistant` |

## Logs

| What | Command |
|---|---|
| Follow the live log | `sudo journalctl -u gemini-assistant -f` |
| Last 100 lines | `sudo journalctl -u gemini-assistant -n 100 --no-pager` |
| Since the last boot | `sudo journalctl -u gemini-assistant -b` |

To leave the live log press **Ctrl+C** (the assistant keeps running). If a pager opens, press **q**.

Useful log lines: `[Idle]` waiting for the wake phrase, `[Active] Heard '...'` wake phrase detected, `[Status] Live session open/closed`, `[Timeout]` idle timeout, `[Feedback] <kind>: ...` an error was announced, `[Error]` the error text.

## Run it by hand (without the service)

Stop the service first. Only one program can own the microphone.

```
[Pi]  sudo systemctl stop gemini-assistant
[Pi]  cd ~/gemini-assistant
[Pi]  ./venv/bin/python -m assistant --list-devices      # what audio devices the Pi sees
[Pi]  ./venv/bin/python -m assistant                     # run with the Pi's own mic and speaker (Ctrl+C stops)
[Pi]  aplay -l ; arecord -l                              # ALSA playback and capture devices
```

Test without a mic, using a 16 kHz mono wav that contains the wake phrase and then a question:

```
[Pi]  ./venv/bin/python -m assistant --simulate /tmp/in.wav --out /tmp/out.wav   # saves the reply as a wav
[Pi]  ./venv/bin/python -m assistant --simulate /tmp/in.wav --play               # plays the reply on the speaker
```

## Use your PC's mic and speakers over SSH (audio bridge)

Use this to test the assistant before the Pi has its own mic and speaker. The PC streams microphone audio to the Pi over SSH; the Pi runs the real assistant and streams the reply audio back to the PC speakers.

**What you need to install (on the PC, once):**

1. **Python 3** on the PC (check with `python --version`).
2. **The Windows OpenSSH client** (included in Windows 10/11; check with `ssh -V`).
3. **The `sounddevice` package:** `pip install sounddevice`
4. **Passwordless SSH from the PC to the Pi.** The script uses the key `~/.ssh/id_ed25519_claude` by default. Use your own key with `--key "C:\Users\you\.ssh\id_ed25519"`, or `--key ""` to use your normal SSH setup.
5. **Nothing extra on the Pi:** `--remote-audio` is built in, and the project must already be deployed to `~/gemini-assistant`.
6. **Headphones**, so the mic does not pick up the speaker and make the assistant hear itself.

**Run it** (in the repo folder on the PC; stop the service on the Pi first):

```
[Pi]  sudo systemctl stop gemini-assistant
[PC]  python src/tools/pc_audio_bridge.py
```

Say "Hey Gemini" or "Hey Assistant" and ask a question. The Pi's log lines appear in the same window. Ctrl+C stops it.

Options:

```
python src/tools/pc_audio_bridge.py --list-devices                 # show the PC's audio devices
python src/tools/pc_audio_bridge.py --in-device 1 --out-device 4   # pick devices by number or name part
python src/tools/pc_audio_bridge.py --host gemini-pi.local         # another host name
python src/tools/pc_audio_bridge.py --from-wav in.wav --to-wav out.wav --seconds 25   # headless test, no mic or speaker
```

If you hear clicks: use headphones, and tell the maintainer where in the reply they happen (start or end, throughout, or while you speak).

## Change settings

Settings live in `~/gemini-assistant/.env` on the Pi (permission `600`, never commit it). Edit with `nano ~/gemini-assistant/.env`, save with **Ctrl+O, Enter**, exit with **Ctrl+X**, then restart the service.

| Setting | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | none | your key (required) |
| `GEMINI_VOICE` | `Kore` | female: Kore, Aoede, Leda, Zephyr; male: Puck, Charon, Fenrir |
| `IDLE_TIMEOUT` | `8` | seconds of silence before the session closes |
| `WAKE_PHRASES` | `hey gemini,hey assistant` | comma-separated; the words must exist in the Vosk model |
| `AUDIO_IN_DEVICE`, `AUDIO_OUT_DEVICE` | system default | device index or part of its name (see `--list-devices`) |
| `MUTE_MIC_WHILE_SPEAKING` | `false` | set `true` if the assistant interrupts itself because of echo |
| `SPEECH_RMS` | `500` | mic level that counts as speech for the idle timer; raise it if room noise keeps sessions open |
| `WAKE_SKIP_MS` | `600` | audio dropped after the chime so the wake phrase's tail is not sent to Gemini |
| `ERROR_VOICE` | `true` | speak error messages (needs `espeak-ng`); tones always play |
| `FEEDBACK_LANG` | `sv` | language of spoken errors: `sv` or `en` |

## Deploy code changes (from the PC to the Pi)

```
[PC]  scp -r src/. gemini-pi:gemini-assistant/        # never contains .env
[Pi]  sudo systemctl restart gemini-assistant         # if the service is running
```

## Run the tests

```
[Pi]  cd ~/gemini-assistant
[Pi]  ./venv/bin/python -m pytest -m "not integration" -q    # offline tests, no network or audio
[Pi]  ./venv/bin/python -m pytest -m integration -q          # real Gemini calls (uses your API key and credits)
```

## Error sounds

When something goes wrong the Pi plays a tone, then speaks a short message (needs `sudo apt install -y espeak-ng`). Examples are in `docs/error-feedback/`.

| Sound | Meaning | Typical cause |
|---|---|---|
| three falling notes | credits depleted | HTTP 402 or 429 "prepayment credits are depleted": top up the account |
| three short beeps | quota or rate limit | HTTP 429 `RESOURCE_EXHAUSTED`: wait a moment |
| two long low beeps | key or access problem | HTTP 403 or close code 1008: check the API key |
| falling blip | no internet | Wi-Fi or DNS problem |
| wobble | Gemini overloaded | HTTP 5xx or close code 1011: try again shortly |
| one long low tone | something else | read the log |

## Quick fixes

| Problem | Try |
|---|---|
| The assistant does not answer | `systemctl status gemini-assistant`, then the live log |
| `Permission denied (publickey)` on SSH | check the key path and that the Pi accepts it |
| No sound | `aplay -l`; set `AUDIO_OUT_DEVICE`; check the speaker's own volume and power |
| It wakes up on ordinary talk | raise the wake threshold by tightening `WAKE_PHRASES`, or tell the maintainer |
| It interrupts itself | set `MUTE_MIC_WHILE_SPEAKING=true` and move the mic away from the speaker |
| The session ends in the middle of a long question | lower `SPEECH_RMS` or raise `IDLE_TIMEOUT` |
| Pi's IP changed | `hostname -I` on the Pi, or use `gemini-pi.local` |
| Reboot or power off the Pi | `sudo reboot` / `sudo shutdown now` (wait for the green light to stop before unplugging) |
