from assistant.session import IdleTimer


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_expires_after_timeout():
    c = Clock()
    timer = IdleTimer(8.0, c)
    c.t = 8.0
    assert not timer.expired()
    c.t = 8.1
    assert timer.expired()


def test_speech_resets_the_window():
    c = Clock()
    timer = IdleTimer(8.0, c)
    c.t = 7.0
    timer.touch()  # user or model spoke
    c.t = 14.0
    assert not timer.expired()
    c.t = 15.1
    assert timer.expired()


def test_countdown_starts_after_playback_finishes():
    c = Clock()
    timer = IdleTimer(8.0, c)
    c.t = 20.0
    assert not timer.expired(busy=True)  # speaker still playing: no expiry, timer reset
    c.t = 27.0
    assert not timer.expired()
    c.t = 28.1
    assert timer.expired()


def test_rms_distinguishes_speech_from_silence():
    from array import array

    from assistant.session import _rms

    quiet = array("h", [10, -10] * 320).tobytes()
    loud = array("h", [3000, -3000] * 320).tobytes()
    assert _rms(quiet) < 500 < _rms(loud)
    assert _rms(b"") == 0.0
