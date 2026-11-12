---
id: SPEC-look-tool
status: planned (not built)
parent: SPEC.md (CAP-10)
---

# Look on demand: the `look` tool

Kay owns a USB webcam. This file is the design for letting the assistant see, written down so it can be built later without re-deriving it.

## Behaviour

1. The user asks something that needs sight: "Hey Gemini, what's this?", "what do I have in my hand?", "read this label", "vad står det här?".
2. Gemini calls the `look` tool (declared like `set_timer` and `go_to_sleep`, answered in `LiveConversation._answer_tools`).
3. The Pi grabs **one** frame from the webcam, scales it down, encodes it as JPEG and sends it to the session.
4. Gemini describes what it sees, reads text or identifies things, in the user's language (Swedish or English) in one to three spoken sentences.
5. The camera is not used at any other time. There is no continuous video.

Why one frame on demand and not a video stream: it costs far less, avoids the roughly two-minute cap on audio-plus-video sessions, and keeps the camera off almost all the time, which matters in a family home.

## Tool declaration

- `look`, no required arguments. Optional `question` (string): what to look for, so the model can say "the label on the bottle". Description tells the model to call it whenever the answer depends on seeing something, never to guess about what is in front of the camera, and to say so if the picture is dark or unclear.
- Declared only when `LOOK_TOOL=true` **and** a camera was found at startup, so the model never offers a tool that cannot work.

## Capturing

- A new module `camera.py`: `grab_jpeg(device, max_side=1024, quality=80) -> bytes`. USB webcams are UVC and need no driver; use OpenCV (`cv2.VideoCapture`) or `v4l2-ctl`/`ffmpeg` if OpenCV is too heavy for the Pi. Open the device, discard the first few frames (auto-exposure settles), keep one, close the device straight away.
- Blocking capture runs in an executor so the event loop (audio sending) never stalls. Timeout about 3 s.
- `FakeCamera` for tests and `--simulate`, like `FakeMic`/`FakeSpeaker`.

## Sending the image to Gemini

This is the open question. Try in order and keep what `gemini-3.8-live` accepts:

1. Return the picture inside the tool response, if the API allows image parts in a `FunctionResponse`.
2. `send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))` right after answering the tool call with `{"result": "ok, the picture follows"}`.
3. `send_client_content(turns=Content(role="user", parts=[Part.from_bytes(data=jpeg, mime_type="image/jpeg")]), turn_complete=False)`.

A throwaway probe script (like `/tmp/live_greet.py` for the greeting) settles this in a couple of minutes once the webcam is plugged in. Record the answer in `stack.md`.

## Configuration (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `LOOK_TOOL` | `true` | declare the tool when a camera is present |
| `CAMERA_DEVICE` | empty = first video device | index (`0`) or path (`/dev/video0`) |
| `CAMERA_MAX_SIDE` | `1024` | longest side of the JPEG in pixels |

`--doctor` gains a camera check (found, can grab a frame, frame size) and `--list-devices` lists video devices.

## Privacy and safety (a family home with children)

- Capture happens only inside a `look` tool call, and every capture is logged (`[Look] Captured 640x480 JPEG, 38 kB`) so it is visible in the journal.
- Frames are held in memory only, never written to disk, and sent only to Gemini.
- If the user did not ask for it, Gemini must not call the tool. The system prompt says so.
- A webcam with a hardware shutter or an LED is preferred; the LED shows when it is in use.
- Child-appropriate answers still apply to what it describes.

## Failure handling

| Case | Behaviour |
|---|---|
| No webcam at startup | tool not declared; if the user asks anyway, Gemini says it has no camera |
| Camera busy or unplugged mid-run | tool returns `{"error": "camera unavailable"}`; Gemini says so briefly; the assistant keeps running |
| Frame is black or very dark | tool returns a note; Gemini tells the user it is too dark |
| Capture takes over 3 s | timeout, same as unavailable |
| API refuses images | logged as `[Warn]`, tool returns an error, conversation continues |

## Tests (offline)

- The tool is declared only with a camera and `LOOK_TOOL=true`.
- A `look` call captures once, sends one image through the chosen call and answers the tool call.
- Camera errors and timeouts return an error result without ending the session.
- Nothing is captured when the tool is not called; every capture logs one line.
- The JPEG is scaled to `CAMERA_MAX_SIDE`.

## Acceptance

With the webcam plugged in, "Hey Gemini, what is this?" while holding up an object gets a correct spoken description within about 4 s of the end of the question; the journal shows exactly one `[Look]` line per request; unplugging the camera gives a spoken "I cannot see right now" and nothing crashes.

## Out of scope for the first version

Continuous video, motion detection, recognising who is in front of the camera, saving pictures, a Pi Camera Module (a later `camera.py` backend).
