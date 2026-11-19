import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from steam import Steam, SteamError


class _Api(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str, bytes]] = []
    launch_reply = {"ok": True, "message": "Launching game 620…"}

    def log_message(self, *args):
        pass

    def _reply(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.seen.append(("GET", self.path, self.headers.get("X-Bix-Launch-Token", ""), b""))
        if self.path.startswith("/steam/art/"):
            if self.path == "/steam/art/620/cover":
                self.send_response(200)
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"\xff\xd8ab")
                return
            return self._reply(404, {"error": "no cached art"})
        if self.headers.get("X-Bix-Launch-Token") != "t" * 32:
            return self._reply(403, {"error": "forbidden"})
        self._reply(200, {"games": [{"appid": "620", "name": "Portal 2"}], "running": {"appid": "620", "name": "Portal 2"}})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.seen.append(("POST", self.path, self.headers.get("X-Bix-Launch-Token", ""), body))
        self._reply(200, self.launch_reply)


@pytest.fixture
def api(tmp_path):
    _Api.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tok = tmp_path / "token"
    tok.write_text("t" * 32 + "\n")
    yield f"http://127.0.0.1:{srv.server_port}", tok
    srv.shutdown()


def test_games_sends_launch_token(api):
    base, tok = api
    assert Steam(base, str(tok)).library() == {
        "games": [{"appid": "620", "name": "Portal 2"}], "running": {"appid": "620", "name": "Portal 2"}}
    assert _Api.seen[0][:3] == ("GET", "/steam/games", "t" * 32)


def test_launch_posts_appid(api):
    base, tok = api
    Steam(base, str(tok)).launch("620")
    method, path, _, body = _Api.seen[0]
    assert (method, path, json.loads(body)) == ("POST", "/steam/launch", {"appid": "620"})


def test_launch_refusal_raises(api, monkeypatch):
    base, tok = api
    monkeypatch.setattr(_Api, "launch_reply", {"ok": False, "error": "not an installed game"})
    with pytest.raises(SteamError, match="not an installed game"):
        Steam(base, str(tok)).launch("99")


def test_wrong_token_is_an_error(api, tmp_path):
    base, _ = api
    bad = tmp_path / "bad"
    bad.write_text("nope")
    with pytest.raises(SteamError, match="HTTP 403"):
        Steam(base, str(bad)).library()


def test_missing_token_file_is_an_error(api, tmp_path):
    base, _ = api
    with pytest.raises(SteamError, match="token unreadable"):
        Steam(base, str(tmp_path / "missing")).library()


def test_non_numeric_appid_never_sent(api):
    base, tok = api
    with pytest.raises(SteamError, match="bad app ID"):
        Steam(base, str(tok)).launch("620; rm -rf")
    assert _Api.seen == []


def test_quit_game_posts(api):
    base, tok = api
    Steam(base, str(tok)).quit_game()
    assert _Api.seen[0][:2] == ("POST", "/steam/quit-game")


def test_art_bytes_and_missing(api):
    base, tok = api
    s = Steam(base, str(tok))
    assert s.art("620", "cover") == b"\xff\xd8ab"
    assert s.art("400", "wide") is None
