"""Nvidia Shield over network ADB: wake, sleep, and the remote's keys.

The container has its own ADB key (not matt's), mounted read-only; the Shield
approved it once on screen. Wake and sleep connect, act, check the Shield's
power state actually changed, and disconnect. Remote keys (d-pad, OK, Back)
and typed text reuse one open connection so presses don't each pay for a new handshake; a
failed press drops it and the next press reconnects.
"""
import json
import logging
import shlex
import re
import threading
import time
from dataclasses import dataclass

from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import (
    AdbConnectionError,
    AdbTimeoutError,
    DeviceAuthError,
    InvalidResponseError,
    TcpTimeoutException,
)

log = logging.getLogger("remote.shield")

ADB_ERRORS = (
    AdbConnectionError, AdbTimeoutError, DeviceAuthError,
    InvalidResponseError, TcpTimeoutException, OSError,
)
SETTLE_TIMEOUT = 5.0  # seconds for the Shield to report the new power state

# The Shield's built-in video player plays dizquetv's MPEG-TS with sound from
# the start; VLC dropped the audio until the next file in the stream ("PCR
# called too late", "discarded audio buffer"; 2026-10-30).
PLAYER = "com.android.gallery3d/.app.MovieActivity"

# The remote's keys: the only keyevents the page can send.
KEYS = {
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER",
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "play-pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
    "next": "KEYCODE_MEDIA_NEXT",
    "rewind": "KEYCODE_MEDIA_REWIND",
    "fast-forward": "KEYCODE_MEDIA_FAST_FORWARD",
}

# PlaybackState action bits (android.media.session.PlaybackState) behind each
# media key, in the order the page shows them. play-pause: any of PAUSE,
# PLAY, PLAY_PAUSE.
MEDIA_CONTROLS = (
    ("previous", 16),       # SKIP_TO_PREVIOUS
    ("rewind", 8),
    ("play-pause", 2 | 4 | 512),
    ("fast-forward", 64),
    ("next", 32),           # SKIP_TO_NEXT
)


@dataclass
class Media:
    title: str | None
    subtitle: str | None
    controls: list[str]


@dataclass
class Session:
    package: str
    actions: int = 0
    title: str | None = None
    subtitle: str | None = None


@dataclass
class Now:
    """What the TV shows. When it's asleep nothing else was asked."""
    awake: bool
    app: str | None = None
    media: Media | None = None


def front_app(focus: str) -> str | None:
    """The package of `dumpsys window`'s mCurrentFocus line; None when the
    focus is null or a window that isn't an app's (e.g. StatusBar)."""
    m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^\s/}]+)/", focus)
    return m.group(1) if m else None


def _description(text: str) -> tuple[str | None, str | None]:
    """Title and subtitle from MediaDescription's toString, which is always
    "title, subtitle, description" with "null" for missing parts. Split from
    the right, so a title's own commas stay in the title."""
    parts = [None if p == "null" else p for p in text.rsplit(", ", 2)]
    title = parts[0] if parts else None
    subtitle = parts[1] if len(parts) > 1 else None
    return title, subtitle


def parse_sessions(dump: str) -> tuple[list[Session], str | None]:
    """The sessions in `dumpsys media_session`, and the package of its media
    button session (where a media key goes when no app takes it)."""
    sessions: list[Session] = []
    button = None
    for raw in dump.splitlines():
        line = raw.strip()
        if line.startswith("Media button session is "):
            target = line.removeprefix("Media button session is ")
            button = target.split("/", 1)[0] if "/" in target else None
        elif line.startswith("Audio playback"):
            break
        elif line.startswith("package="):
            sessions.append(Session(line.removeprefix("package=")))
        elif not sessions:
            continue
        elif line.startswith("state=PlaybackState"):
            m = re.search(r"\bactions=(\d+)", line)
            sessions[-1].actions = int(m.group(1)) if m else 0
        elif line.startswith("metadata:") and "description=" in line:
            sessions[-1].title, sessions[-1].subtitle = _description(line.split("description=", 1)[1])
    return sessions, button


def controls(actions: int) -> list[str]:
    return [name for name, bits in MEDIA_CONTROLS if actions & bits]


def choose_media(dump: str, app: str | None) -> Media | None:
    """The front app's session if it has one, else the media button session's.
    PlaybackState's state isn't used: stale sessions still claim to play."""
    sessions, button = parse_sessions(dump)
    by_package = {}
    for s in sessions:
        by_package.setdefault(s.package, s)  # the stack lists the newest first
    chosen = by_package.get(app) if app else None
    if chosen is None and button:
        chosen = by_package.get(button)
    if chosen is None:
        return None
    return Media(chosen.title, chosen.subtitle, controls(chosen.actions))


ENTER = "KEYCODE_ENTER"
KEYBOARD_TIMEOUT = 1.5  # seconds for the on-screen keyboard to close after Back
KEYBOARD_SETTLE = 0.4   # after Enter, before checking whether the app reopened it
MAX_TEXT = 200

# Phone keyboards substitute typographic characters; `input text` only types
# ASCII, so these become their plain forms rather than being refused.
_PLAIN = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
})


def _input_text(text: str) -> str:
    """`input text`'s argument for `text`: plain printable ASCII, with spaces
    written as %s (its only escape; a literal "%s" types a space)."""
    plain = text.translate(_PLAIN).strip()
    if not plain:
        raise ValueError("nothing to type")
    if len(plain) > MAX_TEXT:
        raise ValueError(f"too long: {len(plain)} characters, the limit is {MAX_TEXT}")
    bad = sorted({c for c in plain if not " " <= c <= "~"})
    if bad:
        raise ValueError("the TV can't type " + " ".join(bad))
    return plain.replace(" ", "%s")


class ShieldError(Exception):
    pass


class Refused(ValueError):
    """The app received the command and said no; its reason is the message."""


# Watch (our Materialious fork) answers `am broadcast`s to its remote receiver:
# result=1 with the player state as JSON, 3 with a short reason, 0 when
# nothing received it (the app isn't running, or a version without one).
WATCH = "computer.bix.materialious"
WATCH_ACTION = "computer.bix.materialious.REMOTE"
WATCH_SETTINGS = ("speed", "quality")
BROADCAST_OK, BROADCAST_REFUSED, BROADCAST_UNHEARD = 1, 3, 0


def _broadcast(cmd: str, value: str | None = None) -> str:
    """The `am broadcast` for one Watch remote command; value shell-quoted."""
    out = f"am broadcast -a {WATCH_ACTION} -p {WATCH} --es cmd {cmd}"
    return out if value is None else f"{out} --es value {shlex.quote(value)}"


def parse_broadcast(out: str) -> tuple[int, str | None]:
    """The result code and data of `am broadcast`'s output, whose last line is
    `Broadcast completed: result=<code>[, data="<data>"]`. am escapes nothing
    in data, so it runs from the first `data="` to the last `"`."""
    at = out.rfind("Broadcast completed: result=")
    m = re.match(r"Broadcast completed: result=(-?\d+)", out[at:]) if at >= 0 else None
    if m is None:
        raise ShieldError(f"am broadcast: no result in {out[-200:]!r}")
    tail = out[at:]
    start = tail.find('data="')
    if start < 0:
        return int(m.group(1)), None
    start += len('data="')
    end = tail.rfind('"')
    return int(m.group(1)), tail[start:end] if end >= start else None


def watch_state(data: str | None) -> dict:
    """Watch's state JSON, checked: {"player": False}, or the player's speed,
    speeds, quality ("auto" or a height) and qualities (heights)."""
    try:
        state = json.loads(data or "")
    except ValueError:
        raise ShieldError(f"Watch sent unreadable state: {(data or '')[:200]!r}") from None
    if not isinstance(state, dict) or not isinstance(state.get("player"), bool):
        raise ShieldError(f"Watch sent unexpected state: {(data or '')[:200]!r}")
    if not state["player"]:
        return {"player": False}
    speed, speeds = state.get("speed"), state.get("speeds")
    quality, qualities = state.get("quality"), state.get("qualities")

    def number(x) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    if not (number(speed) and isinstance(speeds, list) and all(number(s) for s in speeds)
            and (quality == "auto" or isinstance(quality, int) and not isinstance(quality, bool))
            and isinstance(qualities, list)
            and all(isinstance(q, int) and not isinstance(q, bool) for q in qualities)):
        raise ShieldError(f"Watch sent unexpected player state: {data[:200]!r}")
    return {"player": True, "speed": speed, "speeds": speeds, "quality": quality, "qualities": qualities}


class Shield:
    def __init__(self, addr: str, key_path: str):
        host, _, port = addr.partition(":")
        self._host = host
        self._port = int(port or 5555)
        self._key_path = key_path
        self._key_dev: AdbDeviceTcp | None = None
        self._key_lock = threading.Lock()

    def key(self, name: str) -> None:
        """Send one remote key over the kept-open connection."""
        code = KEYS.get(name)
        if code is None:
            raise ShieldError(f"no such key {name!r}")
        self._input(f"input keyevent {code}")

    def type_text(self, text: str) -> None:
        """Type into whatever field has focus on the TV, all at once, then press
        Enter (starts the search in most apps), leaving the on-screen keyboard
        closed. Raises ValueError for text the Shield's `input text` can't type.

        The Shield's keyboard (Leanback) takes Enter as a press on its own
        focused key while it's on screen, which in Plex is the mic: voice
        search instead of a search (2026-11-18). So it's closed first, and Enter
        goes to the field itself. Back is only sent while the keyboard is
        showing, where all it does is close it."""
        self._input(f"input text {shlex.quote(_input_text(text))}")
        if not self._close_keyboard():
            raise ShieldError("typed, but the TV's keyboard didn't close, so Enter wasn't sent")
        self._input(f"input keyevent {ENTER}")
        # Some apps open the keyboard again on Enter.
        time.sleep(KEYBOARD_SETTLE)
        if not self._close_keyboard():
            raise ShieldError("typed and pressed Enter, but the TV's keyboard didn't close")

    def now(self) -> Now:
        """What's on the TV: whether it's on, the front app, and that app's
        (or the media button's) session. A few quick dumpsys reads."""
        state = self._shell("dumpsys power | grep -m1 mWakefulness=").removeprefix("mWakefulness=")
        if state != "Awake":
            return Now(awake=False)
        app = front_app(self._shell("dumpsys window | grep -m1 mCurrentFocus"))
        return Now(awake=True, app=app, media=choose_media(self._shell("dumpsys media_session"), app))

    def watch_player(self) -> dict | None:
        """Watch's player state (see watch_state); None when nothing answered:
        Watch isn't running, or this version has no remote receiver."""
        code, data = parse_broadcast(self._shell(_broadcast("state")))
        if code == BROADCAST_UNHEARD:
            return None
        if code != BROADCAST_OK:
            raise ShieldError(f"Watch didn't give its state (result={code}): {data or 'no reason'}")
        return watch_state(data)

    def watch_player_set(self, kind: str, value: str) -> None:
        """Set Watch's playback speed ("1.25") or quality ("auto", "1080").
        Raises Refused with Watch's reason when it says no (no video playing,
        a value it doesn't offer)."""
        if kind not in WATCH_SETTINGS:
            raise ShieldError(f"no such Watch setting {kind!r}")
        code, data = parse_broadcast(self._shell(_broadcast(kind, value)))
        if code == BROADCAST_REFUSED:
            raise Refused(data or "Watch refused")
        if code == BROADCAST_UNHEARD:
            raise ShieldError("Watch isn't running")
        if code != BROADCAST_OK:
            raise ShieldError(f"Watch didn't take the {kind} (result={code}): {data or 'no reason'}")

    def _keyboard_shown(self) -> bool:
        return "mInputShown=true" in self._input("dumpsys input_method | grep -m1 mInputShown=")

    def _close_keyboard(self) -> bool:
        """Close the on-screen keyboard if it's showing; False if it stays."""
        if not self._keyboard_shown():
            return True
        self._input("input keyevent KEYCODE_BACK")
        deadline = time.monotonic() + KEYBOARD_TIMEOUT
        while self._keyboard_shown():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True

    def _input(self, command: str) -> str:
        """Run a shell command over the kept-open connection and return its
        output. `input` prints nothing on success and an exception on failure."""
        out = self._shell(command)
        if "Exception" in out or "Error" in out:
            raise ShieldError(f"{command.split(' ', 2)[1]} refused: {out[-200:]}")
        return out

    def _shell(self, command: str) -> str:
        """Run a shell command over the kept-open connection; its output
        unchecked (a dumpsys can contain any title, "Error" included)."""
        with self._key_lock:
            try:
                if self._key_dev is None:
                    self._key_dev = self._open()
                out = str(self._key_dev.shell(command)).strip()
            except ADB_ERRORS as e:
                dev, self._key_dev = self._key_dev, None
                if dev is not None:
                    dev.close()
                raise ShieldError(f"ADB to {self._host}:{self._port}: {type(e).__name__}: {e}") from e
        return out

    def play_stream(self, url: str) -> None:
        """Open a stream in the built-in player (dizquetv's MPEG-TS)."""
        def act(dev: AdbDeviceTcp) -> str:
            return str(dev.shell(f"am start -W -n {PLAYER} -a android.intent.action.VIEW -d '{url}' -t video/mp2t"))
        out = self._run(act)
        if "Status: ok" not in out:
            raise ShieldError(f"player didn't start: {out.strip()[-200:]}")

    def stop_player(self) -> None:
        """Close the player (and VLC, used before), so the stream ends."""
        self._run(lambda dev: dev.shell(f"am force-stop {PLAYER.split('/')[0]}; am force-stop org.videolan.vlc"))

    def is_awake(self) -> bool:
        return self._run(self._wakefulness) == "Awake"

    def wake(self) -> None:
        self._set_power("KEYCODE_WAKEUP", "Awake")

    def sleep(self) -> None:
        # Dozing is screen-off with a doze dream running; the Shield usually
        # moves on to Asleep within the settle time but sometimes stays
        # (2026-10-31, 2026-11-11). Either way it's off.
        self._set_power("KEYCODE_SLEEP", "Asleep", "Dozing")

    def pair(self, wait_s: float = 60.0) -> str:
        """First connection: the Shield shows "Allow USB debugging?" on the TV
        and this waits for someone to accept it."""
        return self._run(lambda dev: self._wakefulness(dev), auth_timeout_s=wait_s)

    def _set_power(self, keycode: str, *want: str) -> None:
        def act(dev: AdbDeviceTcp) -> str:
            # Already there: send nothing. A WAKEUP to an awake Shield still
            # makes it claim the TV (Text View On + Active Source); switching
            # TV -> Play that way put the TV into standby (2026-10-30).
            state = self._wakefulness(dev)
            if state in want:
                return state
            dev.shell(f"input keyevent {keycode}")
            deadline = time.monotonic() + SETTLE_TIMEOUT
            state = self._wakefulness(dev)
            while state not in want and time.monotonic() < deadline:
                time.sleep(0.3)
                state = self._wakefulness(dev)
            return state

        state = self._run(act)
        if state not in want:
            raise ShieldError(f"sent {keycode} but the Shield reports {state}, not {' or '.join(want)}")

    @staticmethod
    def _wakefulness(dev: AdbDeviceTcp) -> str:
        out = dev.shell("dumpsys power | grep -m1 mWakefulness=")
        return str(out).strip().removeprefix("mWakefulness=") or "unknown"

    def _open(self, auth_timeout_s: float = 1.0) -> AdbDeviceTcp:
        """A connected device; ADB errors propagate (callers wrap them)."""
        try:
            with open(self._key_path) as f:
                priv = f.read()
            with open(self._key_path + ".pub") as f:
                pub = f.read()
        except OSError as e:
            raise ShieldError(f"ADB key unreadable: {e}") from e
        dev = AdbDeviceTcp(self._host, self._port, default_transport_timeout_s=5.0)
        try:
            dev.connect(rsa_keys=[PythonRSASigner(pub, priv)], auth_timeout_s=auth_timeout_s)
        except BaseException:
            dev.close()
            raise
        return dev

    def _run(self, action, auth_timeout_s: float = 1.0):
        try:
            dev = self._open(auth_timeout_s)
        except ADB_ERRORS as e:
            raise ShieldError(f"ADB to {self._host}:{self._port}: {type(e).__name__}: {e}") from e
        try:
            return action(dev)
        except ADB_ERRORS as e:
            raise ShieldError(f"ADB to {self._host}:{self._port}: {type(e).__name__}: {e}") from e
        finally:
            dev.close()
