"""The Onkyo TX-SR3100's IR codes (NEC, ESPHome byte order). Captured and
tested on the amp 2026-10-29; see ../README.md."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    label: str
    address: int
    command: int
    steppable: bool = False  # may be sent several times in one request


COMMANDS: dict[str, Command] = {
    # Discrete codes: each is ignored when the amp is already in that state.
    "on": Command("On", 0x6DD2, 0xFB04),
    "off": Command("Off", 0x6CD2, 0xB847),
    "game": Command("Game (HX100G)", 0x6DD2, 0xF20D),
    "shield": Command("BD/DVD (Shield)", 0x6CD2, 0x738C),
    "volume-up": Command("Volume up", 0x6DD2, 0xFD02, steppable=True),
    "volume-down": Command("Volume down", 0x6DD2, 0xFC03, steppable=True),
    "mute": Command("Mute", 0x6DD2, 0xFA05),
}

MAX_STEPS = 5     # cap on volume steps per request
STEP_GAP = 0.4    # seconds between repeated frames; 0.5 s tested fine
