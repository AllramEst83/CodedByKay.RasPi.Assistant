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

Log lines are tagged and colored (in a terminal and in the audio bridge; not in `journalctl`, where escape codes would show as noise, and not when `NO_COLOR` is set; `FORCE_COLOR=1` forces color):

| Tag | Color | Meaning |
|---|---|---|
| `[Idle]` | dim grey | waiting for the wake phrase |
| `[Active]` | bold green | wake phrase heard |
| `[Status]` | cyan | session opened or closed, system messages |
| `[You]` | bold blue | what you said (Gemini's transcript of it) |
| `[Gemini]` | bold magenta | what Gemini answered |
| `[Interrupted]` | bold yellow | you spoke over a reply (barge-in) |
| `[Timeout]` | yellow | the 8 s idle timeout ended the session |
| `[Feedback]` | black on yellow | an error was announced out loud |
| `[Error]` | white on red | something failed, with the error text |
| `[Timer]` | bold cyan | a voice timer was set, cancelled or went off |
| `[Pass]` `[Warn]` `[Fail]` | green, yellow, red | results of `--doctor`; `[Warn] Dropped N mic frame(s)` means the connection was too slow and part of your speech was lost |

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

## Check it and measure it

```
[Pi]  ./venv/bin/python -m assistant --doctor                 # one Pass / Warn / Fail line per check
[Pi]  ./venv/bin/python -m assistant --doctor --skip-audio    # leave out the mic and speaker checks
[Pi]  ./venv/bin/python -m assistant --doctor --skip-api      # leave out the Gemini connection (no tokens used)
```

`--doctor` checks the `.env` file and its permissions, that the API key exists (never printed), the Vosk wake
model, espeak-ng, the network, the audio devices, the mic level, a test chime on the speaker, and a short
Gemini Live session (a few tokens). Run it first when something does not work, and on the day the hardware arrives.

**Wake-word false-trigger test** (no Gemini calls, no cost). Leave it running while you talk normally or the TV
is on; every `[Active]` line is a trigger, and the text it heard is shown, for example
`(heard as "[unk] hey assistant")`:

```
[Pi]  ./venv/bin/python -m assistant --wake-test --minutes 10   # with the Pi's own mic
[PC]  python src/tools/pc_audio_bridge.py --wake-test            # with the PC's mic, over SSH
```

At the end it prints the number of triggers and triggers per minute. Anything that was not you saying a wake
phrase is a false trigger.

**Tuning the wake word.** The English model can mishear other speech as a wake phrase ("heter huvudstaden" as
"hey assistant"), so a match is confirmed before it fires (`WAKE_CONFIRM_MS`). To compare strategies on a corpus of
synthetic look-alike sentences (this costs a little Gemini TTS):

```
[Pi]  ./venv/bin/python tools/make_wake_corpus.py ; ./venv/bin/python tools/make_wake_corpus.py heldout ; ./venv/bin/python tools/make_wake_corpus.py followed
[Pi]  ./venv/bin/python tools/eval_wake.py wake_corpus wake_corpus_heldout     # false triggers, hits and delay per strategy
```

Measured so far: without confirmation 12 of 81 look-alike sentences woke the detector; with the default 200 ms, 1
did, and all real wake clips (alone, and followed straight by a question) still worked, at about +0.25 s delay.
Synthetic voices are only a proxy: judge with your own voice using `--wake-test`.

**Which audio device is used:** with `AUDIO_IN_DEVICE` / `AUDIO_OUT_DEVICE` empty, the first USB device is chosen
(HDMI is skipped), and the log says which one at startup (`[Status] Microphone: ...`). If you have two USB devices
(for example a USB mic and a USB sound card that also has a mic input), set the variable to part of the device
name, as shown by `--list-devices`. `default` forces the system default.

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

**Echo: the assistant hearing itself.** If sound from the PC speakers reaches the microphone, Gemini takes its own
answer for you speaking, interrupts itself and starts the answer again (you see `[You]` lines with fragments of the
answer, such as "grand theft", followed by `[Interrupted]`). By default the bridge therefore makes the Pi ignore the
mic while it speaks (plus `MUTE_TAIL_MS`, 600 ms, afterwards); the downside is that you cannot interrupt it. Check
your setup, then choose:

```
python src/tools/pc_audio_bridge.py --echo-check      # plays a quiet tone; [Pass] = the mic does not hear the speaker
python src/tools/pc_audio_bridge.py --headphones      # only if the check passes: the mic stays open, so you can interrupt
```

The bridge prints which microphone and speaker it opened. If they are not your headset, run `--list-devices` and
pass `--in-device` and `--out-device` (a number or part of the name). On the Pi itself the same protection is
`MUTE_MIC_WHILE_SPEAKING=true` in `.env`.

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
| `AUDIO_IN_DEVICE`, `AUDIO_OUT_DEVICE` | first USB device | device index or part of its name (see `--list-devices`); `default` = the system default |
| `MUTE_TAIL_MS` | `600` | while muting (greeting, or `MUTE_MIC_WHILE_SPEAKING`), keep ignoring the mic this long after the speaker stops (room reverb, network and playback delay) |
| `MUTE_MIC_WHILE_SPEAKING` | `false` | set `true` if the assistant interrupts itself because of echo |
| `SPEECH_RMS` | `500` | mic level that counts as speech for the idle timer; raise it if room noise keeps sessions open |
| `GREETING_EN`, `GREETING_SV` | `Online`, `Ja?` | robot-voice greeting after the wake chime (needs `espeak-ng`); empty text turns that language's greeting off |
| `GREETING_SPEED` | `115` | greeting speed in words per minute; lower is slower (error messages use 150) |
| `GREETING_MAP` | `hey gemini:en,hey assistant:en` | which wake phrase gets which greeting language |
| `TIMER_TOOL` | `true` | voice timers ("sätt en timer på fem minuter"); they ring with a tone and a spoken message, are kept in memory only (a restart forgets them), at most 10 at once; `false` turns it off |
| `SLEEP_TOOL` | `true` | saying "go to sleep" (or "gå och lägg dig") ends the conversation at once with a goodbye and a falling chime; `false` turns it off |
| `GOOGLE_SEARCH` | `true` | let Gemini search the web for current facts (weather, news, prices); `false` turns it off |
| `WAKE_CONFIRM_MS` | `200` | after a wake match, listen this many ms longer and require a confirming final result; fewer false triggers for a small delay. `0` = off (fastest, most false triggers); `500` = strictest |
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
