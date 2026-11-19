"""Apps on the Shield: the name the page shows, and the extra controls it
offers while that app is in front.

A hand-kept list of what's installed on this Shield, meant to grow: add an app
when it should have a nicer name or extras. Extras are names the page knows how
to render, from EXTRAS only.
"""
from dataclasses import dataclass

EXTRAS = {"keyboard", "channels"}  # the keyboard bubble; the channel list


@dataclass(frozen=True)
class App:
    label: str
    extras: tuple[str, ...] = ()


APPS = {
    "computer.bix.materialious": App("Watch", ("keyboard",)),  # our YouTube app
    "com.plexapp.android": App("Plex", ("keyboard",)),
    "org.schabi.newpipe": App("NewPipe", ("keyboard",)),
    "com.android.gallery3d": App("Channel player", ("channels",)),  # plays dizquetv channels
    "com.spotify.tv.android": App("Spotify", ("keyboard",)),
    "com.wbd.stream": App("HBO Max", ("keyboard",)),
    "com.apple.atve.androidtv.appletv": App("Apple TV", ("keyboard",)),
    "org.videolan.vlc": App("VLC"),
    "com.spocky.projengmenu": App("Home"),  # Projectivy, the launcher
}


def lookup(package: str) -> App:
    """The app's entry; an unknown package is shown by its name, no extras."""
    return APPS.get(package) or App(package)
