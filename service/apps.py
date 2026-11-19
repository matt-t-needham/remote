"""Apps on the Shield: the one table of what the remote knows about each app.

Per app: the name the page shows, the component the Apps screen launches, how
to search in it (or None), and the extra controls the page offers while it's
in front. A hand-kept list of what's installed on this Shield, meant to grow.

Everything here is allowlisted server-side: the page names an extra, an
action or a package, never a shell string. Extras are rendered generically by
the page from their `type` (EXTRA_TYPES); key extras send a name from
shield.KEYS, action extras a name from ACTIONS, dialog extras open one of
DIALOGS.

Entries marked `# untested` haven't been tried on the TV yet.
"""
import shlex
import urllib.parse
from dataclasses import dataclass
from typing import Callable

EXTRA_TYPES = ("key", "dialog", "search", "action", "channel")
DIALOGS = ("keyboard", "channels")  # the keyboard bubble; the channel list
CHANNEL_DIRS = ("up", "down", "last")


@dataclass(frozen=True)
class Extra:
    """One control the page shows for the front app. `arg` is the key, the
    dialog or action name, or the channel direction, by type. Consecutive
    extras with the same `group` share a caption."""
    type: str
    label: str
    arg: str | None = None
    group: str | None = None

    def as_json(self) -> dict:
        out = {"type": self.type, "label": self.label}
        field = {"key": "key", "dialog": "name", "action": "name", "channel": "dir"}.get(self.type)
        if field:
            out[field] = self.arg
        if self.group:
            out["group"] = self.group
        if self.type == "action":
            same = ACTIONS[self.arg].same_as
            if same:
                out["same_as"] = same
        return out


def key(name: str, label: str, group: str | None = None) -> Extra:
    return Extra("key", label, name, group)


def dialog(name: str, label: str) -> Extra:
    return Extra("dialog", label, name)


def action(name: str, label: str, group: str | None = None) -> Extra:
    return Extra("action", label, name, group)


def channel(direction: str, label: str) -> Extra:
    return Extra("channel", label, direction, "Channel")


SEARCH = Extra("search", "Search")
KEYBOARD = dialog("keyboard", "Keyboard")


@dataclass(frozen=True)
class Action:
    """A broadcast an app's player listens for. `same_as` names the media
    control (shield.MEDIA_CONTROLS) it duplicates, so the page can leave it
    out while the app's session already offers that control."""
    package: str
    action: str
    same_as: str | None = None


# NewPipe v0.28.8 (installed on the Shield) registers these in Player.java's
# setupBroadcastReceiver, from player/notification/NotificationConstants.java:
# BASE_ACTION = App.PACKAGE_NAME + ".player.MainPlayer.", and every action but
# CLOSE appends ".player.MainPlayer.<NAME>" to it again, so the doubled
# segment (and the "..") is real. The receiver never sets a result code, so
# `am broadcast` reports result=0 whether or not a player heard it; a NewPipe
# media session is the sign its player is open.
NEWPIPE = "org.schabi.newpipe"
_NP = NEWPIPE + ".player.MainPlayer."
ACTIONS = {
    "newpipe-play-pause": Action(NEWPIPE, _NP + ".player.MainPlayer.PLAY_PAUSE", "play-pause"),
    "newpipe-previous": Action(NEWPIPE, _NP + ".player.MainPlayer.ACTION_PLAY_PREVIOUS", "previous"),
    "newpipe-next": Action(NEWPIPE, _NP + ".player.MainPlayer.ACTION_PLAY_NEXT", "next"),
    "newpipe-rewind": Action(NEWPIPE, _NP + ".player.MainPlayer.ACTION_FAST_REWIND", "rewind"),
    "newpipe-fast-forward": Action(NEWPIPE, _NP + ".player.MainPlayer.ACTION_FAST_FORWARD", "fast-forward"),
    "newpipe-repeat": Action(NEWPIPE, _NP + ".player.MainPlayer.REPEAT"),
    "newpipe-shuffle": Action(NEWPIPE, _NP + ".player.MainPlayer.ACTION_SHUFFLE"),
    "newpipe-close": Action(NEWPIPE, _NP + "CLOSE"),
}


MAX_SEARCH = 200


def search_text(text: str) -> str:
    """The query for an intent: any UTF-8 (am passes it through), stripped,
    at most MAX_SEARCH characters, no control characters."""
    q = text.strip()
    if not q:
        raise ValueError("nothing to search for")
    if len(q) > MAX_SEARCH:
        raise ValueError(f"too long: {len(q)} characters, the limit is {MAX_SEARCH}")
    if any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in q):
        raise ValueError("control characters can't be searched for")
    return q


def _share_to(component: str) -> Callable[[str], str]:
    """Search by sharing the text to an activity (ACTION_SEND text/plain)."""
    return lambda q: (f"am start -n {component} -a android.intent.action.SEND -t text/plain"
                      f" --es android.intent.extra.TEXT {shlex.quote(q)}")


def _view(package: str, url: str) -> Callable[[str], str]:
    """Search by opening a URL in the app; `url` has one {} for the query."""
    return lambda q: (f"am start -a android.intent.action.VIEW"
                      f" -d {shlex.quote(url.format(urllib.parse.quote(q, safe='')))} -p {package}")


@dataclass(frozen=True)
class App:
    label: str
    extras: tuple[Extra, ...] = ()
    launch: str | None = None  # component for `am start -n`; None: not on the Apps screen
    search: Callable[[str], str] | None = None  # query -> the `am start` command


# Launch components come from a read-only survey of the Shield (2026-11-19).
APPS = {
    "computer.bix.materialious": App(  # Watch, our YouTube app
        "Watch", (KEYBOARD,), "computer.bix.materialious/us.materialio.app.MainActivity"),
    "com.plexapp.android": App(
        "Plex",
        (KEYBOARD,
         key("search", "Search"),            # untested
         key("captions", "Subtitles"),       # untested
         key("rewind", "Back"),              # untested
         key("fast-forward", "Forward")),    # untested
        "com.plexapp.android/tv.plex.app.MainActivity"),
    "org.schabi.newpipe": App(
        "NewPipe",
        (SEARCH,
         action("newpipe-previous", "Previous", "Player"),
         action("newpipe-rewind", "Rewind", "Player"),
         action("newpipe-play-pause", "Play / Pause", "Player"),
         action("newpipe-fast-forward", "Forward", "Player"),
         action("newpipe-next", "Next", "Player"),
         action("newpipe-repeat", "Repeat", "Player"),    # untested
         action("newpipe-shuffle", "Shuffle", "Player"),  # untested
         action("newpipe-close", "Close", "Player")),     # untested
        "org.schabi.newpipe/.MainActivity",
        # RouterActivity's SEND text/plain filter confirmed on the Shield.
        _share_to("org.schabi.newpipe/.RouterActivity")),  # untested
    "com.android.gallery3d": App(  # plays dizquetv channels
        "Channel player",
        (dialog("channels", "Channels"),
         channel("down", "Ch −"), channel("up", "Ch +"), channel("last", "Last"))),
    "com.spotify.tv.android": App(
        "Spotify", (SEARCH,), "com.spotify.tv.android/com.spotify.app.androidtv.MainActivity",
        _view("com.spotify.tv.android", "spotify:search:{}")),  # untested
    "com.wbd.stream": App(
        "HBO Max", (key("captions", "Subtitles"), SEARCH),  # untested
        "com.wbd.stream/com.wbd.beam.BeamActivity",
        _view("com.wbd.stream", "https://play.max.com/search?q={}")),  # untested
    "com.apple.atve.androidtv.appletv": App(
        "Apple TV", (key("captions", "Subtitles"), SEARCH),  # untested
        "com.apple.atve.androidtv.appletv/.MainActivity",
        _view("com.apple.atve.androidtv.appletv", "https://tv.apple.com/search?term={}")),  # untested
    # Keys from VLC 3.7.2's VideoPlayerActivity.onKeyDown: CAPTIONS and
    # MEDIA_AUDIO_TRACK both open the same tracks dialog (audio and
    # subtitles); Z resizes (cycles the scale); J/K and G/H move the audio and
    # subtitle delay by 50 ms; MEDIA_REWIND/FAST_FORWARD seek by the
    # double-tap jump setting, 10 s by default. All untested on the TV.
    "org.videolan.vlc": App(
        "VLC",
        (key("rewind", "Back 10 s"),
         key("fast-forward", "Forward 10 s"),
         key("captions", "Tracks"),
         key("resize", "Resize"),
         key("audio-delay-down", "− 50 ms", "Audio delay"),
         key("audio-delay-up", "+ 50 ms", "Audio delay"),
         key("subtitle-delay-down", "− 50 ms", "Subtitle delay"),
         key("subtitle-delay-up", "+ 50 ms", "Subtitle delay")),
        "org.videolan.vlc/.StartActivity"),
    "com.spocky.projengmenu": App("Home"),  # Projectivy, the launcher
}


def lookup(package: str) -> App:
    """The app's entry; an unknown package is shown by its name, no extras."""
    return APPS.get(package) or App(package)


def launchable() -> list[dict]:
    """The Apps screen: every app with a launch component, in table order."""
    return [{"package": p, "label": a.label} for p, a in APPS.items() if a.launch]
