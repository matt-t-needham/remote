import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dizquetv import Dizque, DizqueError

M3U = """#EXTM3U url-tvg="http://192.168.0.47:8000/api/xmltv.xml"
#EXTINF:0 tvg-id="2" CUID="2" tvg-chno="2" tvg-name="RAGE" tvg-logo="http://localhost:8000/images/uploads/Ragelogo.webp" group-title="dizqueTV",RAGE
http://192.168.0.47:8000/video?channel=2
#EXTINF:0 tvg-id="1" CUID="1" tvg-chno="1" tvg-name="The Simpsons" tvg-logo="http://localhost:8000/images/uploads/simpsons.png" group-title="dizqueTV",The Simpsons
http://192.168.0.47:8000/video?channel=1
#EXTINF:0 tvg-id="3" CUID="3" tvg-chno="3" tvg-name="Sneaky" tvg-logo="http://evil.example/etc/passwd" group-title="dizqueTV",Sneaky
http://192.168.0.47:8000/video?channel=3
"""


class _Dz(BaseHTTPRequestHandler):
    seen: list[str] = []

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append(self.path)
        if self.path == "/api/channels.m3u":
            return self._send(200, M3U.encode(), "audio/x-mpegurl")
        if self.path == "/images/uploads/Ragelogo.webp":
            return self._send(200, b"RIFFwebp", "image/webp")
        return self._send(404, b"nope", "text/plain")


@pytest.fixture
def dz():
    _Dz.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Dz)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Dizque(f"http://127.0.0.1:{srv.server_port}")
    srv.shutdown()


def test_channels_parsed_in_number_order(dz):
    assert [(c["number"], c["name"]) for c in dz.channels()] == [(1, "The Simpsons"), (2, "RAGE"), (3, "Sneaky")]


def test_icon_fetched_from_the_real_host(dz):
    assert dz.icon(2) == (b"RIFFwebp", "image/webp")
    assert "/images/uploads/Ragelogo.webp" in _Dz.seen


def test_icon_outside_images_is_never_fetched(dz):
    assert dz.icon(3) is None
    assert not any("passwd" in p for p in _Dz.seen)


def test_missing_icon_file_is_an_error(dz):
    with pytest.raises(DizqueError, match="HTTP 404"):
        dz.icon(1)


def test_stream_url(dz):
    assert dz.stream_url(2).endswith("/video?channel=2")


def test_unreachable_is_an_error():
    with pytest.raises(DizqueError):
        Dizque("http://127.0.0.1:9").channels()
