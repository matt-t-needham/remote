import pytest
from adb_shell.exceptions import TcpTimeoutException

import shield
from shield import Shield, ShieldError


class FakeDevice:
    """Answers dumpsys with the next state from `states` (last one repeats)."""

    def __init__(self, states, connect_error=None):
        self.states = list(states)
        self.connect_error = connect_error
        self.commands = []
        self.closed = False

    def __call__(self, host, port, default_transport_timeout_s=None):
        self.addr = (host, port)
        return self

    def connect(self, rsa_keys, auth_timeout_s):
        self.auth_timeout_s = auth_timeout_s
        self.connects = getattr(self, "connects", 0) + 1
        self.closed = False
        if self.connect_error:
            raise self.connect_error

    def shell(self, cmd):
        if getattr(self, "shell_error", None):
            err, self.shell_error = self.shell_error, None
            raise err
        self.commands.append(cmd)
        if cmd.startswith("dumpsys"):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return f"  mWakefulness={state}\n"
        return ""

    def close(self):
        self.closed = True


@pytest.fixture
def key(tmp_path, monkeypatch):
    (tmp_path / "adbkey").write_text("priv")
    (tmp_path / "adbkey.pub").write_text("pub")
    monkeypatch.setattr(shield, "PythonRSASigner", lambda pub, priv: (pub, priv))
    monkeypatch.setattr(shield, "SETTLE_TIMEOUT", 0.5)
    monkeypatch.setattr(shield.time, "sleep", lambda s: None)
    return str(tmp_path / "adbkey")


def use(monkeypatch, dev):
    monkeypatch.setattr(shield, "AdbDeviceTcp", dev)
    return dev


def test_wake_waits_for_awake(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Asleep", "Asleep", "Awake"]))
    Shield("192.168.0.36:5555", key).wake()
    assert dev.addr == ("192.168.0.36", 5555)
    assert "input keyevent KEYCODE_WAKEUP" in dev.commands
    assert dev.closed


def test_wake_when_already_awake_sends_no_key(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake"]))
    Shield("192.168.0.36:5555", key).wake()
    assert not any(c.startswith("input") for c in dev.commands)


def test_sleep_when_already_asleep_sends_no_key(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Asleep"]))
    Shield("192.168.0.36:5555", key).sleep()
    assert not any(c.startswith("input") for c in dev.commands)


def test_sleep_that_settles_in_dozing_succeeds(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake", "Dozing"]))
    Shield("192.168.0.36:5555", key).sleep()
    assert "input keyevent KEYCODE_SLEEP" in dev.commands


def test_sleep_when_already_dozing_sends_no_key(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Dozing"]))
    Shield("192.168.0.36:5555", key).sleep()
    assert not any(c.startswith("input") for c in dev.commands)


def test_wake_from_dozing_sends_wakeup(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Dozing", "Awake"]))
    Shield("192.168.0.36:5555", key).wake()
    assert "input keyevent KEYCODE_WAKEUP" in dev.commands


def test_sleep_that_never_takes_raises(key, monkeypatch):
    use(monkeypatch, FakeDevice(["Awake"]))
    with pytest.raises(ShieldError, match="reports Awake, not Asleep"):
        Shield("192.168.0.36:5555", key).sleep()


def test_connection_failure_is_shield_error(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake"], connect_error=TcpTimeoutException("timed out")))
    with pytest.raises(ShieldError, match="TcpTimeoutException"):
        Shield("192.168.0.36:5555", key).wake()
    assert dev.closed


def test_missing_key_is_shield_error(tmp_path):
    with pytest.raises(ShieldError, match="key unreadable"):
        Shield("192.168.0.36:5555", str(tmp_path / "nope")).wake()


def test_normal_calls_dont_wait_for_approval_but_pair_does(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake"]))
    s = Shield("192.168.0.36", key)
    s.wake()
    assert dev.auth_timeout_s == 1.0
    assert s.pair(wait_s=60) == "Awake"
    assert dev.auth_timeout_s == 60


def test_keys_reuse_one_connection(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake"]))
    s = Shield("192.168.0.36:5555", key)
    s.key("up")
    s.key("select")
    s.key("back")
    assert dev.commands == ["input keyevent KEYCODE_DPAD_UP", "input keyevent KEYCODE_DPAD_CENTER",
                            "input keyevent KEYCODE_BACK"]
    assert dev.connects == 1 and not dev.closed


def test_failed_key_drops_connection_and_next_press_reconnects(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake"]))
    s = Shield("192.168.0.36:5555", key)
    s.key("left")
    dev.shell_error = TcpTimeoutException("timed out")
    with pytest.raises(ShieldError, match="TcpTimeoutException"):
        s.key("left")
    assert dev.closed
    s.key("right")
    assert dev.connects == 2
    assert dev.commands[-1] == "input keyevent KEYCODE_DPAD_RIGHT"


def test_unknown_key_is_refused_without_connecting(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake"]))
    with pytest.raises(ShieldError, match="no such key"):
        Shield("192.168.0.36:5555", key).key("power")
    assert getattr(dev, "connects", 0) == 0


def test_play_stream_opens_the_player(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake"]))
    dev.shell = lambda cmd: (dev.commands.append(cmd), "Status: ok\nComplete")[1]
    Shield("192.168.0.36:5555", key).play_stream("http://h:8000/video?channel=2")
    assert dev.commands == ["am start -W -n com.android.gallery3d/.app.MovieActivity -a android.intent.action.VIEW "
                            "-d 'http://h:8000/video?channel=2' -t video/mp2t"]


def test_play_stream_failure_raises(key, monkeypatch):
    dev = use(monkeypatch, FakeDevice(["Awake"]))
    dev.shell = lambda cmd: "Error: Activity not started"
    with pytest.raises(ShieldError, match="player didn't start"):
        Shield("192.168.0.36:5555", key).play_stream("http://h:8000/video?channel=2")



class ImeDevice(FakeDevice):
    """A Shield with an on-screen keyboard: `ime` lists whether it's showing at
    each check (the last repeats); Back closes it unless `stuck`."""

    def __init__(self, ime, stuck=False, reopen=False):
        super().__init__(["Awake"])
        self.ime = list(ime)
        self.stuck = stuck
        self.reopen = reopen

    def shell(self, cmd):
        if cmd.startswith("dumpsys input_method"):
            if getattr(self, "shell_error", None):
                err, self.shell_error = self.shell_error, None
                raise err
            self.commands.append("check")
            shown = self.ime.pop(0) if len(self.ime) > 1 else self.ime[0]
            return f"  mShowRequested=false mInputShown={'true' if shown else 'false'}\n"
        out = super().shell(cmd)
        if cmd == "input keyevent KEYCODE_BACK" and not self.stuck:
            self.ime = [False]
        if cmd == "input keyevent KEYCODE_ENTER" and self.reopen:
            self.reopen, self.ime = False, [True]
        return out


def test_type_text_quotes_and_presses_enter(key, monkeypatch):
    dev = use(monkeypatch, ImeDevice([False]))
    sh = Shield("192.168.0.36:5555", key)
    sh.type_text("it's 2 > 1")
    assert dev.commands == [
        """input text 'it'"'"'s%s2%s>%s1'""",
        "check",
        "input keyevent KEYCODE_ENTER",
        "check",
    ]
    assert dev.connects == 1  # one kept-open connection throughout


def test_type_text_closes_keyboard_before_enter(key, monkeypatch):
    dev = use(monkeypatch, ImeDevice([True]))
    Shield("192.168.0.36:5555", key).type_text("dune")
    assert dev.commands == [
        "input text dune",
        "check", "input keyevent KEYCODE_BACK", "check",
        "input keyevent KEYCODE_ENTER",
        "check",
    ]


def test_type_text_closes_keyboard_reopened_by_enter(key, monkeypatch):
    dev = use(monkeypatch, ImeDevice([False], reopen=True))
    Shield("192.168.0.36:5555", key).type_text("dune")
    assert dev.commands[-4:] == [
        "input keyevent KEYCODE_ENTER",
        "check", "input keyevent KEYCODE_BACK", "check",
    ]


def test_type_text_keyboard_that_wont_close_skips_enter(key, monkeypatch):
    monkeypatch.setattr(shield, "KEYBOARD_TIMEOUT", 0.05)
    dev = use(monkeypatch, ImeDevice([True], stuck=True))
    with pytest.raises(ShieldError, match="Enter wasn't sent"):
        Shield("192.168.0.36:5555", key).type_text("dune")
    assert "input keyevent KEYCODE_ENTER" not in dev.commands
    assert dev.commands.count("input keyevent KEYCODE_BACK") == 1  # never a second Back


def test_type_text_plain_word_and_smart_quotes(key, monkeypatch):
    dev = use(monkeypatch, ImeDevice([False]))
    Shield("192.168.0.36:5555", key).type_text("  Ocean\u2019s Eleven \u2014 2001 ")
    assert dev.commands[0] == """input text 'Ocean'"'"'s%sEleven%s-%s2001'"""


@pytest.mark.parametrize("text, match", [
    ("", "nothing"), ("   ", "nothing"), ("x" * 201, "too long"),
    ("naïve", "can't type ï"), ("a\nb", "can't type"),
])
def test_type_text_refuses_before_connecting(key, monkeypatch, text, match):
    dev = use(monkeypatch, FakeDevice(["Awake"]))
    with pytest.raises(ValueError, match=match):
        Shield("192.168.0.36:5555", key).type_text(text)
    assert dev.commands == []


def test_type_text_exception_output_is_error_and_no_enter(key, monkeypatch):
    class Refusing(FakeDevice):
        def shell(self, cmd):
            super().shell(cmd)
            return "java.lang.IllegalArgumentException: bad" if cmd.startswith("input text") else ""

    dev = use(monkeypatch, Refusing(["Awake"]))
    with pytest.raises(ShieldError, match="text refused"):
        Shield("192.168.0.36:5555", key).type_text("dune")
    assert dev.commands == ["input text dune"]


def test_type_text_adb_failure_drops_connection(key, monkeypatch):
    dev = use(monkeypatch, ImeDevice([False]))
    sh = Shield("192.168.0.36:5555", key)
    dev.shell_error = TcpTimeoutException("timed out")
    with pytest.raises(ShieldError, match="TcpTimeoutException"):
        sh.type_text("dune")
    assert dev.closed
    sh.type_text("dune")
    assert dev.connects == 2


# `dumpsys media_session` on the Shield, 2026-11-18 (abridged).
MEDIA_SESSIONS = """\
  Media button session is computer.bix.materialious/capacitor-music-controls-media-session (userId=0)
  Sessions Stack - have 2 sessions:
    capacitor-music-controls-media-session computer.bix.materialious/capacitor-music-controls-media-session (userId=0)
      ownerPid=32717, ownerUid=10114, userId=0
      package=computer.bix.materialious
      active=true
      state=PlaybackState {state=3, position=0, buffered position=0, speed=1.0, updated=1908088229, actions=3586, custom actions=[], active item id=-1, error=null}
      metadata:size=6, description=ADHD: a cultural analysis, Meditations for the anxious mind, null
      queueTitle=null, size=0

    androidx.media3.session.id. com.spotify.tv.android/androidx.media3.session.id. (userId=0)
      package=com.spotify.tv.android
      active=true
      state=PlaybackState {state=3, position=365, buffered position=0, speed=1.1, updated=1907840613, actions=7057354, custom actions=[Action:mName='Seek 15 seconds back, ...], active item id=0, error=null}
      metadata:size=21, description=Chapter 48, The Girls of Good Fortune: A Novel, null

Audio playback (lastly played comes first)
"""
WATCH = shield.Media("ADHD: a cultural analysis", "Meditations for the anxious mind", ["play-pause"])
SPOTIFY = shield.Media("Chapter 48", "The Girls of Good Fortune: A Novel", ["rewind", "play-pause", "fast-forward"])


@pytest.mark.parametrize("app, media", [
    ("com.spotify.tv.android", SPOTIFY),        # the front app's own session
    ("com.spocky.projengmenu", WATCH),          # no session: the media button's
    (None, WATCH),
])
def test_media_is_front_apps_session_else_media_buttons(app, media):
    assert shield.choose_media(MEDIA_SESSIONS, app) == media


def test_no_media_without_front_or_button_session():
    no_button = MEDIA_SESSIONS.replace("Media button session is computer.bix.materialious/capacitor-music-controls-media-session",
                                       "Media button session is null")
    assert shield.choose_media(no_button, "com.plexapp.android") is None


@pytest.mark.parametrize("description, title, subtitle", [
    ("Chapter 48, Part 2, The Girls of Good Fortune, null", "Chapter 48, Part 2", "The Girls of Good Fortune"),
    ("Dune, null, null", "Dune", None),
    ("null, null, null", None, None),
])
def test_description_title_keeps_its_commas_and_drops_nulls(description, title, subtitle):
    dump = MEDIA_SESSIONS.replace("ADHD: a cultural analysis, Meditations for the anxious mind, null", description)
    media = shield.choose_media(dump, "computer.bix.materialious")
    assert (media.title, media.subtitle) == (title, subtitle)


def test_controls_in_display_order():
    assert shield.controls(16 | 8 | 4 | 64 | 32 | 1) == ["previous", "rewind", "play-pause", "fast-forward", "next"]
    assert shield.controls(1) == []  # STOP alone has no button


@pytest.mark.parametrize("focus, app", [
    ("  mCurrentFocus=Window{fe6544a u0 computer.bix.materialious/us.materialio.app.MainActivity}",
     "computer.bix.materialious"),
    ("  mCurrentFocus=Window{3c1d0e2 u0 StatusBar}", None),
    ("  mCurrentFocus=null", None),
])
def test_front_app(focus, app):
    assert shield.front_app(focus) == app


class NowDevice(FakeDevice):
    def __init__(self, power, title="ADHD: a cultural analysis"):
        super().__init__([power])
        self.title = title

    def shell(self, cmd):
        out = super().shell(cmd)
        if cmd.startswith("dumpsys window"):
            return "  mCurrentFocus=Window{fe6544a u0 computer.bix.materialious/us.materialio.app.MainActivity}\n"
        if cmd == "dumpsys media_session":
            return MEDIA_SESSIONS.replace("ADHD: a cultural analysis", self.title)
        return out


def test_now_asleep_asks_nothing_else(key, monkeypatch):
    dev = use(monkeypatch, NowDevice("Dozing"))
    assert Shield("192.168.0.36", key).now() == shield.Now(awake=False)
    assert len(dev.commands) == 1


def test_now_awake_reads_app_and_media_on_the_kept_connection(key, monkeypatch):
    # A title saying "Error" is just a title, not a refused command.
    dev = use(monkeypatch, NowDevice("Awake", title="Error 404: a documentary"))
    now = Shield("192.168.0.36", key).now()
    assert now.awake and now.app == "computer.bix.materialious"
    assert now.media == shield.Media("Error 404: a documentary", "Meditations for the anxious mind", ["play-pause"])
    assert dev.connects == 1 and not dev.closed
