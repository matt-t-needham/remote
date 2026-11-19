"""Scenes: several devices in one request. Every step runs even if an earlier
one failed (the amp is still worth switching if the Shield didn't answer), and
the result lists each step so a partial failure can't look like success.

The TV is never controlled directly. It comes on when the Shield wakes (CEC
one-touch play), which is the only thing that can turn it on, and it has gone
to standby when the Shield sleeps (4 of 5 tries on 2026-10-30).

The amp's power and input must be settled *before* the Shield wakes the TV. If
the amp powers up or changes input while the TV is still starting (~9 s), the
TV puts everything back to standby a few seconds later (ARC keeps the amp on
the CEC bus). So both on-scenes send the amp's codes first and wake the Shield
last, back to back: the amp took an input code 0.15 s after On. The TV then
asks the amp for the Shield's input, which the amp ignores with its CEC off.
The Shield stays awake in GAME: sleeping it right after the wake left the TV
cycling on and off.

Switching to Play while the TV is already on (Shield awake, the TV's active
input): the TV goes to standby whenever the Shield stops being its picture,
whether the amp switches away or the Shield sleeps (2026-10-30, both tried).
That can't be avoided from here, so Play lets it happen and recovers: after
GAME it waits for the Shield to follow the TV into standby, then wakes it,
which brings the TV straight back on GAME (the cold path). The screen blinks
off for a few seconds.
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable

from commands import COMMANDS
from modem import Modem, ModemError
from shield import Shield, ShieldError
from steam import Steam, SteamError

log = logging.getLogger("remote.scenes")

SWITCH_SETTLE = 6.0  # seconds to wait for the TV (and the Shield) to drop to standby after GAME
STANDBY_SETTLE = 4.0  # then let the TV finish turning off; a wake mid-transition is ignored


@dataclass
class Step:
    name: str
    ok: bool
    error: str | None = None


def _amp(modem: Modem, name: str) -> Callable[[], None]:
    cmd = COMMANDS[name]
    return lambda: modem.nec(cmd.address, cmd.command)


def _plan(scene: str, modem: Modem, shield: Shield) -> list[tuple[str, Callable[[], None]]]:
    if scene == "tv":
        # The player closes too, so TV after a channel lands on the home screen.
        return [
            ("amp on", _amp(modem, "on")),
            ("amp input BD/DVD", _amp(modem, "shield")),
            ("player stop", shield.stop_player),
            ("shield wake", shield.wake),
        ]
    if scene == "game":
        return [
            ("amp on", _amp(modem, "on")),
            ("amp input GAME", _amp(modem, "game")),
            ("shield wake", shield.wake),
        ]
    if scene == "off":
        return [
            # Stop the player first so dizquetv's stream closes, not just pauses.
            ("player stop", shield.stop_player),
            ("shield sleep", shield.sleep),
            ("amp off", _amp(modem, "off")),
        ]
    raise KeyError(scene)


SCENES = ("tv", "game", "off")


def run(scene: str, modem: Modem, shield: Shield) -> list[Step]:
    return _run(scene, _plan(scene, modem, shield))


TV_STATES = ("on", "off")


def tv(state: str, shield: Shield) -> list[Step]:
    """The TV on its own, through the Shield (the only way to reach it): waking
    the Shield turns it on over CEC, and it follows the Shield to standby. The
    amp is left as it is; changing the amp while the TV starts can make the TV
    cycle, which the TV scene's ordering avoids."""
    if state == "on":
        plan = [("shield wake", shield.wake)]
    elif state == "off":
        plan = [("player stop", shield.stop_player), ("shield sleep", shield.sleep)]
    else:
        raise KeyError(state)
    return _run(f"tv {state}", plan)


def watch(number: int, url: str, modem: Modem, shield: Shield) -> list[Step]:
    """A dizquetv channel: the TV steps, then its stream in the Shield's
    built-in player (VLC lost the audio until the next file; see shield.py)."""
    plan = [
        ("amp on", _amp(modem, "on")),
        ("amp input BD/DVD", _amp(modem, "shield")),
        ("shield wake", shield.wake),
        (f"play channel {number}", lambda: shield.play_stream(url)),
    ]
    return _run(f"watch {number}", plan)


def switch_channel(number: int, url: str, shield: Shield) -> list[Step]:
    """Channel up/down/last from the channel player: the TV is already on and
    on the Shield, so only the player is restarted on the new stream."""
    plan = [
        ("player stop", shield.stop_player),
        (f"play channel {number}", lambda: shield.play_stream(url)),
    ]
    return _run(f"channel {number}", plan)


def play(appid: str, modem: Modem, shield: Shield, steam: Steam) -> list[Step]:
    """Launch the game first (it takes longest, and the HTPC is always on), then
    the GAME scene brings the amp, Shield and TV up while it loads."""
    was_on = {"tv": False}

    def check() -> None:
        was_on["tv"] = shield.is_awake()

    def wake() -> None:
        if was_on["tv"]:
            deadline = time.monotonic() + SWITCH_SETTLE
            while time.monotonic() < deadline and shield.is_awake():
                time.sleep(0.5)
            time.sleep(STANDBY_SETTLE)
        shield.wake()

    plan = [
        ("steam launch", lambda: steam.launch(appid)),
        ("shield state", check),
        ("amp on", _amp(modem, "on")),
        ("amp input GAME", _amp(modem, "game")),
        ("shield wake", wake),
    ]
    return _run(f"play {appid}", plan)


def _run(scene: str, plan: list[tuple[str, Callable[[], None]]]) -> list[Step]:
    steps = []
    for name, action in plan:
        try:
            action()
            steps.append(Step(name, True))
        except (ModemError, ShieldError, SteamError) as e:
            log.error("scene %s: %s FAILED: %s", scene, name, e)
            steps.append(Step(name, False, str(e)))
    return steps
