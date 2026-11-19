"""Client for the ATOM Lite serial IR modem (protocol in ../firmware/src/main.cpp).

One process owns the port (opened exclusively and kept open); every request
waits for the modem's reply, so a send that didn't happen raises instead of
looking like success.
"""
import logging
import threading
import time
from typing import Callable, Protocol

import serial

log = logging.getLogger("remote.modem")

REPLY_TIMEOUT = 2.0  # an NEC frame takes ~70 ms; the rest is slack


class ModemError(Exception):
    pass


class Port(Protocol):
    def write(self, data: bytes) -> int | None: ...
    def read(self, size: int) -> bytes: ...
    def close(self) -> None: ...


def open_serial(path: str) -> Port:
    s = serial.Serial()
    s.port = path
    s.baudrate = 115200
    s.timeout = 0.05
    s.exclusive = True
    # Opening doesn't reset this board either way (bench-tested 2026-10-29);
    # holding both low keeps it that way should the adapter behave differently.
    s.dtr = False
    s.rts = False
    s.open()
    return s


class Modem:
    def __init__(self, path: str, opener: Callable[[str], Port] = open_serial):
        self._path = path
        self._opener = opener
        self._port: Port | None = None
        self._buf = b""
        self._lock = threading.Lock()

    def ping(self) -> str:
        reply = self._request("ping", ("pong ",))
        return reply.removeprefix("pong ")

    def nec(self, address: int, command: int) -> None:
        if not (0 <= address <= 0xFFFF and 0 <= command <= 0xFFFF):
            raise ValueError("address and command are 16-bit")
        self._request(f"nec {address:04X} {command:04X}", ("ok",))

    def _request(self, line: str, accept: tuple[str, ...]) -> str:
        with self._lock:
            try:
                if self._port is None:
                    self._port = self._opener(self._path)
                    self._buf = b""
                self._port.write(line.encode() + b"\n")
                return self._await_reply(line, accept)
            except (serial.SerialException, OSError) as e:
                # Drop the handle so the next request reopens (e.g. after a replug).
                self._close()
                raise ModemError(f"serial port {self._path}: {e}") from e

    def _await_reply(self, line: str, accept: tuple[str, ...]) -> str:
        deadline = time.monotonic() + REPLY_TIMEOUT
        while True:
            reply = self._readline(deadline)
            if reply is None:
                raise ModemError(f"no reply to {line!r} within {REPLY_TIMEOUT}s")
            if reply.startswith("rx "):
                log.info("heard %s", reply.removeprefix("rx "))
                continue
            if reply.startswith("ready "):
                # The modem rebooted mid-request: whatever we sent is lost.
                raise ModemError(f"modem restarted while handling {line!r}")
            if reply.startswith("err "):
                raise ModemError(f"modem rejected {line!r}: {reply.removeprefix('err ')}")
            # "ok" must match exactly; "pong " is a prefix.
            if any(reply == a or (a.endswith(" ") and reply.startswith(a)) for a in accept):
                return reply
            raise ModemError(f"unexpected reply to {line!r}: {reply!r}")

    def _readline(self, deadline: float) -> str | None:
        assert self._port is not None
        while b"\n" not in self._buf:
            if time.monotonic() >= deadline:
                return None
            self._buf += self._port.read(64)
        raw, self._buf = self._buf.split(b"\n", 1)
        text = raw.decode(errors="replace").strip()
        # Boot noise from the ROM loader is not part of the protocol.
        return text if text else self._readline(deadline)

    def _close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except (serial.SerialException, OSError) as e:
                log.warning("closing %s failed: %s", self._path, e)
            self._port = None
