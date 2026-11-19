import pytest
import serial

import modem
from modem import Modem, ModemError


class FakePort:
    """Replies to each written line from a script: {line: [reply lines]}."""

    def __init__(self, script: dict[str, list[str]]):
        self.script = script
        self.written: list[str] = []
        self.pending = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        line = data.decode().rstrip("\n")
        self.written.append(line)
        for reply in self.script.get(line, []):
            self.pending += reply.encode() + b"\r\n"
        return len(data)

    def read(self, size: int) -> bytes:
        out, self.pending = self.pending[:size], self.pending[size:]
        return out

    def close(self) -> None:
        self.closed = True


def make(script):
    port = FakePort(script)
    return Modem("/dev/fake", opener=lambda path: port), port


@pytest.fixture(autouse=True)
def fast_timeout(monkeypatch):
    monkeypatch.setattr(modem, "REPLY_TIMEOUT", 0.2)


def test_nec_formats_command_and_accepts_ok():
    m, port = make({"nec 6DD2 FB04": ["ok"]})
    m.nec(0x6DD2, 0xFB04)
    assert port.written == ["nec 6DD2 FB04"]


def test_nec_pads_hex():
    m, port = make({"nec 00D2 0005": ["ok"]})
    m.nec(0xD2, 0x5)
    assert port.written == ["nec 00D2 0005"]


def test_ping_returns_version():
    m, _ = make({"ping": ["pong amp-ir-serial 1"]})
    assert m.ping() == "amp-ir-serial 1"


def test_rx_lines_are_skipped_and_logged(caplog):
    m, _ = make({"nec 6DD2 FA05": ["rx nec 6DD2 FD02", "ok"]})
    with caplog.at_level("INFO"):
        m.nec(0x6DD2, 0xFA05)
    assert "heard nec 6DD2 FD02" in caplog.text


def test_err_reply_raises():
    m, _ = make({"nec 6DD2 FB04": ["err addr and cmd must be 4 hex digits"]})
    with pytest.raises(ModemError, match="rejected"):
        m.nec(0x6DD2, 0xFB04)


def test_no_reply_raises():
    m, _ = make({})
    with pytest.raises(ModemError, match="no reply"):
        m.nec(0x6DD2, 0xFB04)


def test_reboot_mid_request_raises():
    m, _ = make({"nec 6DD2 FB04": ["ready amp-ir-serial 1"]})
    with pytest.raises(ModemError, match="restarted"):
        m.nec(0x6DD2, 0xFB04)


def test_unexpected_reply_raises():
    m, _ = make({"ping": ["ok"]})
    with pytest.raises(ModemError, match="unexpected"):
        m.ping()


def test_okay_is_not_ok():
    m, _ = make({"nec 6DD2 FB04": ["okay"]})
    with pytest.raises(ModemError, match="unexpected"):
        m.nec(0x6DD2, 0xFB04)


def test_out_of_range_rejected_before_sending():
    m, port = make({})
    with pytest.raises(ValueError):
        m.nec(0x10000, 0)
    assert port.written == []


def test_serial_error_closes_and_next_request_reopens():
    opened = []

    def opener(path):
        if not opened:
            opened.append("fail")
            raise serial.SerialException("could not open port")
        port = FakePort({"ping": ["pong amp-ir-serial 1"]})
        opened.append(port)
        return port

    m = Modem("/dev/fake", opener=opener)
    with pytest.raises(ModemError, match="could not open"):
        m.ping()
    assert m.ping() == "amp-ir-serial 1"


def test_write_failure_drops_port():
    class Broken(FakePort):
        def write(self, data):
            raise OSError(5, "Input/output error")

    port = Broken({})
    m = Modem("/dev/fake", opener=lambda path: port)
    with pytest.raises(ModemError, match="Input/output"):
        m.ping()
    assert port.closed
