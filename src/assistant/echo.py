"""Echo gate: let the user interrupt the assistant without headphones.

While the assistant speaks, its own voice leaks from the speaker into the microphone. If that is sent to Gemini,
Gemini hears "someone speaking", interrupts itself and starts over. But we know exactly what the speaker plays, so
we know how loud the echo should be: the gate learns the ratio between the mic level and the playback level during
the first moments of a reply (when only echo is present) and afterwards forwards the mic only when it is clearly
louder than that expected echo, meaning the user is speaking over the assistant.

With headphones the echo is about zero, so any speech above `speech_rms` passes: nothing is lost.
"""
from __future__ import annotations

from collections import deque


class EchoGate:
    MIN_REF = 150.0  # playback quieter than this (16-bit RMS) is treated as silence

    def __init__(self, speech_rms: float = 500.0, margin: float = 2.0, hold_s: float = 0.4, tail_s: float = 0.6,
                 delay_s: float = 0.0, calibration_frames: int = 5):
        self.speech_rms = speech_rms  # the mic must exceed this to count as speech at all
        self.margin = margin  # ...and exceed the expected echo by this factor (2.0 = about +6 dB)
        self.hold_s = hold_s  # once the user is detected, keep forwarding this long so the whole phrase gets through
        self.tail_s = tail_s  # keep gating this long after the playback ends (reverb)
        self.delay_s = delay_s  # how long after being written the sound is actually heard (network, speaker buffer)
        self._calibration_frames = calibration_frames
        self._hist: deque[tuple[float, float]] = deque()
        self._smooth = 0.0  # playback level with instant attack and a short decay
        self._last_active = float("-inf")
        self._cal: list[float] = []
        self.echo_ratio: float | None = None  # mic level / playback level when only echo is present
        self._hold_until = float("-inf")

    def allow(self, mic_rms: float, ref_rms: float, now: float) -> bool:
        """True if this mic frame should be sent to Gemini. `ref_rms` is the level being written to the speaker now."""
        self._hist.append((now, ref_rms))
        cutoff = now - self.delay_s
        while len(self._hist) > 1 and self._hist[1][0] <= cutoff:
            self._hist.popleft()
        ref = self._hist[0][1] if self._hist[0][0] <= cutoff else 0.0  # what the room is hearing right now
        self._smooth = max(ref, self._smooth * 0.85)
        if ref > self.MIN_REF:
            self._last_active = now
        if self._smooth <= self.MIN_REF and now - self._last_active >= self.tail_s:
            self._cal = []
            return True  # nothing is playing: the mic is just the mic
        if self.echo_ratio is None:  # still learning the echo: treat these frames as echo
            if self._smooth > self.MIN_REF:
                self._cal.append(mic_rms / self._smooth)
                if len(self._cal) >= self._calibration_frames:
                    self.echo_ratio = sorted(self._cal)[len(self._cal) // 2]
            return False
        threshold = max(self.speech_rms, self.echo_ratio * self._smooth * self.margin)
        loud = mic_rms > threshold
        if loud:
            self._hold_until = now + self.hold_s
        allowed = loud or now < self._hold_until
        if not allowed and self._smooth > self.MIN_REF:
            ratio = mic_rms / self._smooth
            # adapt slowly to changes in volume or position, but fast downwards: if the user was already talking while
            # the echo level was learned, the estimate is too high and must come down as soon as it is quiet
            weight = 0.5 if ratio < 0.7 * self.echo_ratio else 0.03
            self.echo_ratio = (1 - weight) * self.echo_ratio + weight * ratio
        return allowed
