import shlex

import pytest
from fastapi.testclient import TestClient

import apps
import auth
import commands
import config
import web
from modem import ModemError
import scenes
import shield as shield_mod
from shield import ShieldError
from steam import SteamError
from dizquetv import DizqueError


class FakeModem:
    def __init__(self, fail_after: int | None = None):
        self.sent: list[tuple[int, int]] = []
        self.fail_after = fail_after
        self.healthy = True

    def ping(self) -> str:
        if not self.healthy:
            raise ModemError("no reply to 'ping' within 2.0s")
        return "amp-ir-serial 1"

    def nec(self, address: int, command: int) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ModemError("no reply")
        self.sent.append((address, command))


class FakeShield:
    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def wake(self) -> None:
        self._do("wake")

    def sleep(self) -> None:
        self._do("sleep")

    def key(self, name: str) -> None:
        self._do("key " + name)

    def play_stream(self, url: str) -> None:
        self._do("play " + url)

    def type_text(self, text: str) -> None:
        shield_mod._input_text(text)  # same validation as the real Shield
        self._do("type " + text)

    def stop_player(self) -> None:
        self._do("stop_player")

    def now(self):
        self._do("now")
        return getattr(self, "state", shield_mod.Now(awake=False))

    def watch_player(self):
        self._do("watch_player")
        if isinstance(getattr(self, "player", None), Exception):
            raise self.player
        return getattr(self, "player", None)

    def watch_player_set(self, kind: str, value: str) -> None:
        self._do(f"watch {kind} {value}")
        if getattr(self, "refuse", None):
            raise shield_mod.Refused(self.refuse)

    def front(self):
        self._do("front")
        return getattr(self, "front_pkg", None)

    def start(self, command: str) -> None:
        self._do("start " + command)
        if getattr(self, "start_error", None):
            raise ShieldError(self.start_error)

    def has_session(self, package: str) -> bool:
        self._do("has_session " + package)
        return getattr(self, "session", False)

    def broadcast(self, action: str, package: str) -> int:
        self._do(f"broadcast {action} {package}")
        return 0

    def is_awake(self) -> bool:
        self._do("is_awake")
        return getattr(self, "awake", False)

    def _do(self, what: str) -> None:
        if self.fail:
            raise ShieldError("ADB to 192.168.0.36:5555: TcpTimeoutException: timed out")
        self.calls.append(what)


class FakeDizque:
    def __init__(self, fail: bool = False):
        self.fail = fail

    def channels(self) -> list[dict]:
        if self.fail:
            raise DizqueError("dizquetv GET /api/channels.m3u: ConnectionRefusedError")
        return [{"number": 1, "name": "The Simpsons", "icon": "x"}, {"number": 2, "name": "RAGE", "icon": "y"}]

    def stream_url(self, number: int) -> str:
        return f"http://dz:8000/video?channel={number}"

    def icon(self, number: int):
        if self.fail:
            raise DizqueError("dizquetv GET /images/x: HTTP 500")
        return (b"RIFFwebp", "image/webp") if number == 2 else None


class FakeSteam:
    def __init__(self, fail: bool = False):
        self.launched: list[str] = []
        self.fail = fail

    def library(self) -> dict:
        if self.fail:
            raise SteamError("deploy-api GET /steam/games: HTTP 403")
        return {"games": [{"appid": "620", "name": "Portal 2"}], "running": None}

    def art(self, appid: str, kind: str) -> bytes | None:
        if self.fail:
            raise SteamError("deploy-api GET /steam/art: HTTP 403")
        return b"\xff\xd8jpeg" if appid == "620" else None

    def quit_game(self) -> str:
        if self.fail:
            raise SteamError("quit refused: no game running")
        self.quits = getattr(self, "quits", 0) + 1
        return "Quit game 620 (closed 1 window(s))"

    def launch(self, appid: str) -> None:
        if self.fail:
            raise SteamError(f"launch of {appid} refused: not an installed game")
        self.launched.append(appid)


@pytest.fixture
def modem():
    return FakeModem()


@pytest.fixture
def shield():
    return FakeShield()


@pytest.fixture
def client(modem, shield, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    monkeypatch.setattr(commands, "STEP_GAP", 0)
    monkeypatch.setattr(web, "STEP_GAP", 0)
    return TestClient(web.create_app(modem, shield, FakeSteam(), FakeDizque()))


def test_on_sends_discrete_on(client, modem):
    r = client.post("/api/on")
    assert r.status_code == 200
    assert r.json() == {"sent": "on", "steps": 1}
    assert modem.sent == [(0x6DD2, 0xFB04)]


def test_volume_steps(client, modem):
    r = client.post("/api/volume-up?steps=3")
    assert r.status_code == 200
    assert modem.sent == [(0x6DD2, 0xFD02)] * 3


def test_steps_capped(client, modem):
    assert client.post(f"/api/volume-up?steps={commands.MAX_STEPS + 1}").status_code == 422
    assert modem.sent == []


def test_power_cannot_be_repeated(client, modem):
    assert client.post("/api/off?steps=2").status_code == 400
    assert modem.sent == []


def test_unknown_command(client, modem):
    assert client.post("/api/self-destruct").status_code == 404


def test_modem_failure_is_an_error_not_success(client, caplog):
    # `client` only for its Access patch; this test needs its own failing modem.
    failing = FakeModem(fail_after=1)
    c = TestClient(web.create_app(failing, FakeShield(), FakeSteam(), FakeDizque()))
    r = c.post("/api/volume-down?steps=3")
    assert r.status_code == 502
    assert "IR modem" in r.json()["detail"]
    assert "volume-down FAILED after 1 of 3 frames" in caplog.text


def test_healthz_reports_modem(client, modem):
    assert client.get("/healthz").json() == {"ok": True, "modem": "amp-ir-serial 1"}
    modem.healthy = False
    assert client.get("/healthz").status_code == 503


def test_cross_site_post_refused(client, modem):
    r = client.post("/api/on", headers={"sec-fetch-site": "same-site"})
    assert r.status_code == 403
    r = client.post("/api/on", headers={"origin": "https://evil.bix.computer"})
    assert r.status_code == 403
    assert modem.sent == []


def test_own_origin_allowed(client, modem):
    r = client.post("/api/on", headers={"origin": config.PUBLIC_ORIGIN, "sec-fetch-site": "same-origin"})
    assert r.status_code == 200


def test_no_access_jwt_refused(modem, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_AUD", "some-aud")
    c = TestClient(web.create_app(modem, FakeShield(), FakeSteam(), FakeDizque()))
    assert c.post("/api/on").status_code == 403
    assert c.get("/").status_code == 403
    assert modem.sent == []


def test_unset_audience_fails_closed(modem, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_AUD", "")
    c = TestClient(web.create_app(modem, FakeShield(), FakeSteam(), FakeDizque()))
    r = c.post("/api/on", headers={"cf-access-jwt-assertion": "anything"})
    assert r.status_code == 403


def test_forged_jwt_refused(modem, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_AUD", "some-aud")
    c = TestClient(web.create_app(modem, FakeShield(), FakeSteam(), FakeDizque()))
    r = c.post("/api/on", headers={"cf-access-jwt-assertion": "not.a.jwt"})
    assert r.status_code == 403
    assert modem.sent == []


def test_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    # Volume and mute on the main page; amp power and inputs on the Devices page.
    for name in ("volume-down", "volume-up", "mute", "on", "off", "shield", "game"):
        assert f'data-cmd="{name}"' in r.text
    assert 'data-cmd="mute" data-hold="350"' in r.text
    for k in ("up", "down", "left", "right", "select", "back", "home"):
        assert f'data-key="{k}"' in r.text
    assert 'id="open-remote"' in r.text
    for name in ("tv", "off"):
        assert f'data-scene="{name}"' in r.text
    assert 'id="play"' in r.text and 'fetch("/api/games")' in r.text
    assert 'id="open-channels"' in r.text and 'fetch("/api/channels")' in r.text
    # Volume Up and Down only: queued sends, and a tap is one step.
    assert r.text.count(' data-repeat data-repeat-delay="300" data-inflight="2">') == 2
    assert 'id="open-devices"' in r.text
    # The app's controls show on the main page and above the Remote Control d-pad.
    assert 'id="now"' in r.text and 'id="now-remote"' in r.text
    for state in ("on", "off"):
        assert f'data-tv="{state}"' in r.text
    assert 'id="open-keyboard"' in r.text
    assert f'maxlength="{shield_mod.MAX_TEXT}"' in r.text
    assert "id=\"quit-game\"" in r.text
    # On the TV: polled, with a label for every media control and a renderer
    # for every extra the server can name.
    assert 'id="context"' in r.text and 'fetch("/api/now")' in r.text
    for name, _ in shield_mod.MEDIA_CONTROLS:
        assert f'"{name}": "' in r.text
    for kind in apps.EXTRA_TYPES:
        assert f"    {kind}: (x, b" in r.text
    for name in apps.DIALOGS:
        assert f"    {name}: {{ open:" in r.text
    # Apps launcher in the base block's last row, three across.
    assert 'class="row three"' in r.text and 'id="open-apps"' in r.text
    assert 'fetch("/api/apps")' in r.text and '"/api/launch/"' in r.text
    assert 'fetch(searching ? "/api/search" : "/api/text"' in r.text
    # Watch's player settings, one row per setting the server takes.
    assert 'fetch("/api/watch-player/" + kind' in r.text
    for kind in shield_mod.WATCH_SETTINGS:
        assert f'choices("{kind}", ' in r.text


ON, GAME, BD = (0x6DD2, 0xFB04), (0x6DD2, 0xF20D), (0x6CD2, 0x738C)
OFF = (0x6CD2, 0xB847)


def test_tv_scene(client, modem, shield):
    r = client.post("/api/scene/tv")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["steps"]] == ["amp on", "amp input BD/DVD", "player stop", "shield wake"]
    assert shield.calls == ["stop_player", "wake"]
    assert modem.sent == [ON, BD]


def test_game_scene(client, modem, shield):
    r = client.post("/api/scene/game")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["steps"]] == ["amp on", "amp input GAME", "shield wake"]
    assert shield.calls == ["wake"]
    assert modem.sent == [ON, GAME]


def test_off_scene_sleeps_shield_before_amp_off(client, modem, shield):
    r = client.post("/api/scene/off")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["steps"]] == ["player stop", "shield sleep", "amp off"]
    assert shield.calls == ["stop_player", "sleep"]
    assert modem.sent == [OFF]


def test_scene_shield_failure_still_switches_amp_but_reports_502(modem, monkeypatch, caplog):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, FakeShield(fail=True), FakeSteam(), FakeDizque()))
    r = c.post("/api/scene/tv")
    assert r.status_code == 502
    body = r.json()
    assert body["detail"] == "player stop, shield wake failed"
    assert [(s["name"], s["ok"]) for s in body["steps"]] == [
        ("amp on", True), ("amp input BD/DVD", True), ("player stop", False), ("shield wake", False)]
    assert modem.sent == [ON, BD]
    assert "scene tv: shield wake FAILED" in caplog.text


def test_scene_modem_failure_reports_502(shield, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(FakeModem(fail_after=0), shield, FakeSteam(), FakeDizque()))
    r = c.post("/api/scene/game")
    assert r.status_code == 502
    assert r.json()["detail"] == "amp on, amp input GAME failed"


def test_unknown_scene(client):
    assert client.post("/api/scene/party").status_code == 404


def test_scene_needs_access(modem, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_AUD", "some-aud")
    c = TestClient(web.create_app(modem, FakeShield(), FakeSteam(), FakeDizque()))
    assert c.post("/api/scene/tv").status_code == 403
    assert modem.sent == []


def test_dev_bypass_allows_without_jwt(modem, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_DEV_BYPASS", True)
    monkeypatch.setattr(config, "ACCESS_AUD", "")
    c = TestClient(web.create_app(modem, FakeShield(), FakeSteam(), FakeDizque()))
    assert c.post("/api/on").status_code == 200
    assert modem.sent == [(0x6DD2, 0xFB04)]


def test_play_launches_game_first_then_game_scene(modem, shield, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    steam = FakeSteam()
    c = TestClient(web.create_app(modem, shield, steam, FakeDizque()))
    r = c.post("/api/play/620")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["steps"]] == ["steam launch", "shield state", "amp on", "amp input GAME", "shield wake"]
    assert steam.launched == ["620"]
    assert modem.sent == [ON, GAME]
    # From cold (Shield asleep): wake straight away, no waiting.
    assert shield.calls == ["is_awake", "wake"]


def test_play_from_tv_waits_for_standby_then_wakes(modem, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    monkeypatch.setattr(scenes.time, "sleep", lambda s: None)
    sh = FakeShield()
    sh.awake = True
    polls = {"n": 0}
    real = sh.is_awake

    def is_awake():
        # Awake at the start, then the TV's standby takes the Shield down.
        polls["n"] += 1
        sh.awake = polls["n"] < 3
        return real()

    sh.is_awake = is_awake
    c = TestClient(web.create_app(modem, sh, FakeSteam(), FakeDizque()))
    r = c.post("/api/play/620")
    assert r.status_code == 200
    assert sh.calls == ["is_awake", "is_awake", "is_awake", "wake"]
    assert modem.sent == [ON, GAME]


def test_play_launch_failure_still_runs_game_scene_but_reports_502(modem, shield, monkeypatch, caplog):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, shield, FakeSteam(fail=True), FakeDizque()))
    r = c.post("/api/play/620")
    assert r.status_code == 502
    assert r.json()["detail"] == "steam launch failed"
    assert modem.sent == [ON, GAME]
    assert "scene play 620: steam launch FAILED" in caplog.text


def test_play_rejects_non_numeric_appid(client):
    assert client.post("/api/play/620;rm").status_code == 400


def test_games_lists_names_and_ids(client):
    r = client.get("/api/games")
    assert r.status_code == 200
    assert r.json() == {"games": [{"appid": "620", "name": "Portal 2"}], "running": None}


def test_quit_game(modem, shield, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    steam = FakeSteam()
    c = TestClient(web.create_app(modem, shield, steam, FakeDizque()))
    r = c.post("/api/quit-game")
    assert r.status_code == 200 and r.json()["quit"] is True
    assert steam.quits == 1


def test_quit_game_failure_is_502(modem, shield, monkeypatch, caplog):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, shield, FakeSteam(fail=True), FakeDizque()))
    assert c.post("/api/quit-game").status_code == 502
    assert "quit game FAILED" in caplog.text


def test_games_failure_is_502(modem, shield, monkeypatch, caplog):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, shield, FakeSteam(fail=True), FakeDizque()))
    r = c.get("/api/games")
    assert r.status_code == 502
    assert "games list FAILED" in caplog.text


def test_play_needs_access(modem, shield, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: False)
    c = TestClient(web.create_app(modem, shield, FakeSteam(), FakeDizque()))
    assert c.post("/api/play/620").status_code == 403
    assert c.post("/api/quit-game").status_code == 403
    assert c.get("/api/games").status_code == 403


def test_remote_keys(client, shield):
    for k in ("up", "down", "left", "right", "select", "back", "home"):
        assert client.post(f"/api/key/{k}").status_code == 200
    assert shield.calls == ["key up", "key down", "key left", "key right", "key select", "key back", "key home"]


def test_unknown_key_is_404(client, shield):
    assert client.post("/api/key/power").status_code == 404
    assert shield.calls == []


def test_key_failure_is_502(modem, monkeypatch, caplog):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, FakeShield(fail=True), FakeSteam(), FakeDizque()))
    assert c.post("/api/key/up").status_code == 502
    assert "key up FAILED" in caplog.text


def test_art_is_proxied_and_cached(client):
    r = client.get("/api/art/620/cover")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age" in r.headers["cache-control"]


def test_missing_art_is_404(client):
    assert client.get("/api/art/400/wide").status_code == 404


def test_bad_art_request_is_404(client):
    assert client.get("/api/art/620/logo").status_code == 404


def test_watch_channel_runs_tv_steps_then_player(client, modem, shield):
    r = client.post("/api/watch/2")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["steps"]] == ["amp on", "amp input BD/DVD", "shield wake", "play channel 2"]
    assert modem.sent == [ON, BD]
    assert shield.calls == ["wake", "play http://dz:8000/video?channel=2"]


def test_watch_unknown_channel_is_404(client, modem):
    assert client.post("/api/watch/9").status_code == 404
    assert modem.sent == []


def test_channels_list_and_art(client):
    assert client.get("/api/channels").json() == {"channels": [{"number": 1, "name": "The Simpsons"}, {"number": 2, "name": "RAGE"}]}
    r = client.get("/api/channel-art/2")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert client.get("/api/channel-art/1").status_code == 404


def test_channels_failure_is_502(modem, shield, monkeypatch, caplog):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, shield, FakeSteam(), FakeDizque(fail=True)))
    assert c.get("/api/channels").status_code == 502
    assert c.post("/api/watch/2").status_code == 502
    assert "channel list FAILED" in caplog.text


def test_tv_on_wakes_shield_only(client, modem, shield):
    r = client.post("/api/tv/on")
    assert r.status_code == 200
    assert r.json()["tv"] == "on"
    assert shield.calls == ["wake"]
    assert modem.sent == []


def test_tv_off_stops_player_then_sleeps(client, modem, shield):
    r = client.post("/api/tv/off")
    assert r.status_code == 200
    assert shield.calls == ["stop_player", "sleep"]
    assert modem.sent == []


def test_tv_unknown_state(client, shield):
    assert client.post("/api/tv/toggle").status_code == 404
    assert shield.calls == []


def test_tv_failure_is_502_with_steps(modem, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, FakeShield(fail=True), FakeSteam(), FakeDizque()))
    r = c.post("/api/tv/off")
    assert r.status_code == 502
    assert [s["ok"] for s in r.json()["steps"]] == [False, False]


def test_text_typed(client, shield, caplog):
    with caplog.at_level("INFO"):
        r = client.post("/api/text", json={"text": "blade runner"})
    assert r.status_code == 200
    assert r.json() == {"typed": 12}
    assert shield.calls == ["type blade runner"]
    assert "blade runner" not in caplog.text  # searches stay out of the log


def test_text_the_tv_cant_type_is_400(client, shield):
    r = client.post("/api/text", json={"text": "café"})
    assert r.status_code == 400
    assert "é" in r.json()["detail"]
    assert shield.calls == []


def test_text_empty_is_400(client, shield):
    assert client.post("/api/text", json={"text": "   "}).status_code == 400
    assert shield.calls == []


def test_text_shield_failure_is_502(modem, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, FakeShield(fail=True), FakeSteam(), FakeDizque()))
    assert c.post("/api/text", json={"text": "dune"}).status_code == 502


def test_text_cross_site_refused(client, shield):
    r = client.post("/api/text", json={"text": "dune"}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert shield.calls == []


def test_media_keys(client, shield):
    for k in ("play-pause", "previous", "next", "rewind", "fast-forward"):
        assert client.post(f"/api/key/{k}").status_code == 200
    assert shield.calls == ["key play-pause", "key previous", "key next", "key rewind", "key fast-forward"]


def test_now_playing(client, shield):
    shield.state = shield_mod.Now(True, "com.spotify.tv.android",
                                  shield_mod.Media("Chapter 48", None, ["rewind", "play-pause"]))
    assert client.get("/api/now").json() == {
        "awake": True,
        "app": {"package": "com.spotify.tv.android", "label": "Spotify"},
        "media": {"title": "Chapter 48", "subtitle": None, "controls": ["rewind", "play-pause"]},
        "extras": [{"type": "search", "label": "Search"}],
        "player": None,
    }
    assert "watch_player" not in shield.calls  # only Watch is asked


def test_now_unknown_app_and_asleep(client, shield):
    shield.state = shield_mod.Now(True, "com.example.thing", None)
    assert client.get("/api/now").json() == {
        "awake": True, "app": {"package": "com.example.thing", "label": "com.example.thing"},
        "media": None, "extras": [], "player": None}
    shield.state = shield_mod.Now(False)
    assert client.get("/api/now").json() == {"awake": False, "app": None, "media": None, "extras": [], "player": None}


def test_now_shield_failure_is_502(modem, monkeypatch, caplog):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, FakeShield(fail=True), FakeSteam(), FakeDizque()))
    r = c.get("/api/now")
    assert r.status_code == 502 and "TcpTimeoutException" in r.json()["detail"]
    assert "FAILED" in caplog.text


def test_now_needs_access(modem, shield, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: False)
    c = TestClient(web.create_app(modem, shield, FakeSteam(), FakeDizque()))
    assert c.get("/api/now").status_code == 403
    assert shield.calls == []


def test_app_extras_are_ones_the_page_renders():
    media = {name for name, _ in shield_mod.MEDIA_CONTROLS}
    for package, app in apps.APPS.items():
        for x in app.extras:
            assert x.type in apps.EXTRA_TYPES, (package, x)
            if x.type == "key":
                assert x.arg in shield_mod.KEYS, (package, x)
            elif x.type == "dialog":
                assert x.arg in apps.DIALOGS, (package, x)
            elif x.type == "action":
                assert x.arg in apps.ACTIONS, (package, x)
                assert apps.ACTIONS[x.arg].package == package, (package, x)
            elif x.type == "channel":
                assert x.arg in apps.CHANNEL_DIRS, (package, x)
            else:
                assert x.arg is None, (package, x)
        # Search is offered exactly where the app has a search mechanism.
        assert (apps.SEARCH in app.extras) == (app.search is not None), package
    for name, act in apps.ACTIONS.items():
        assert act.same_as is None or act.same_as in media, name


def test_newpipe_actions_match_v0_28_8_source():
    # NotificationConstants.java: BASE_ACTION + ".player.MainPlayer.X" doubles it.
    assert apps.ACTIONS["newpipe-repeat"].action == "org.schabi.newpipe.player.MainPlayer..player.MainPlayer.REPEAT"
    assert apps.ACTIONS["newpipe-close"].action == "org.schabi.newpipe.player.MainPlayer.CLOSE"


def test_now_extras_for_vlc(client, shield):
    shield.state = shield_mod.Now(True, "org.videolan.vlc", shield_mod.Media("Film", None, ["rewind"]))
    extras = client.get("/api/now").json()["extras"]
    assert extras[0] == {"type": "key", "key": "rewind", "label": "Back 10 s"}
    assert {"type": "key", "key": "audio-delay-up", "label": "+ 50 ms", "group": "Audio delay"} in extras
    assert [x["key"] for x in extras] == [
        "rewind", "fast-forward", "captions", "resize",
        "audio-delay-down", "audio-delay-up", "subtitle-delay-down", "subtitle-delay-up"]


def test_now_extras_for_channel_player_and_newpipe(client, shield):
    shield.state = shield_mod.Now(True, "com.android.gallery3d", None)
    assert client.get("/api/now").json()["extras"] == [
        {"type": "dialog", "name": "channels", "label": "Channels"},
        {"type": "channel", "dir": "down", "label": "Ch \u2212", "group": "Channel"},
        {"type": "channel", "dir": "up", "label": "Ch +", "group": "Channel"},
        {"type": "channel", "dir": "last", "label": "Last", "group": "Channel"},
    ]
    shield.state = shield_mod.Now(True, "org.schabi.newpipe", None)
    extras = client.get("/api/now").json()["extras"]
    assert {"type": "action", "name": "newpipe-rewind", "label": "Rewind",
            "group": "Player", "same_as": "rewind"} in extras
    assert {"type": "action", "name": "newpipe-shuffle", "label": "Shuffle", "group": "Player"} in extras


def test_apps_lists_launchable_only(client):
    listed = client.get("/api/apps").json()
    assert {"package": "org.videolan.vlc", "label": "VLC"} in listed
    packages = [a["package"] for a in listed]
    assert "com.android.gallery3d" not in packages and "com.spocky.projengmenu" not in packages
    assert len(packages) == 7


def test_launch_runs_allowlisted_component(client, shield):
    r = client.post("/api/launch/org.videolan.vlc")
    assert r.status_code == 200
    assert shield.calls == ["start am start -n org.videolan.vlc/.StartActivity"]


def test_launch_unknown_or_unlaunchable_is_404(client, shield):
    assert client.post("/api/launch/com.evil;reboot").status_code == 404
    assert client.post("/api/launch/com.android.gallery3d").status_code == 404
    assert shield.calls == []


def test_launch_error_is_502(client, shield, caplog):
    shield.start_error = "am start refused: Error: Activity class does not exist."
    assert client.post("/api/launch/com.wbd.stream").status_code == 502
    assert "launch com.wbd.stream FAILED" in caplog.text


def test_launch_cross_site_refused(client, shield):
    assert client.post("/api/launch/org.videolan.vlc", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert shield.calls == []


@pytest.mark.parametrize("current,direction,want", [
    (None, "up", 1), (None, "down", 7), (99, "up", 1),
    (1, "up", 2), (2, "down", 1), (7, "up", 1), (1, "down", 7),
])
def test_next_channel_wraps(current, direction, want):
    assert web.next_channel([1, 2, 7], current, direction) == want


def test_channel_up_from_unknown_then_last(client, shield, modem):
    r = client.post("/api/channel/up")
    assert r.status_code == 200 and r.json()["channel"] == 1
    assert [s["name"] for s in r.json()["steps"]] == ["player stop", "play channel 1"]
    assert shield.calls == ["stop_player", "play http://dz:8000/video?channel=1"]
    assert modem.sent == []  # no amp steps
    assert client.post("/api/channel/up").json()["channel"] == 2
    assert client.post("/api/channel/up").json()["channel"] == 1  # wraps
    assert client.post("/api/channel/last").json()["channel"] == 2
    assert client.post("/api/channel/last").json()["channel"] == 1


def test_channel_last_without_previous_is_409(client, shield):
    r = client.post("/api/channel/last")
    assert r.status_code == 409 and "previous" in r.json()["detail"]
    client.post("/api/watch/2")
    assert client.post("/api/channel/last").status_code == 409  # one channel, no previous
    assert client.post("/api/channel/down").json()["channel"] == 1  # from the watched channel
    assert client.post("/api/channel/last").json()["channel"] == 2


def test_failed_switch_keeps_the_current_channel(modem, monkeypatch):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    sh = FakeShield()
    c = TestClient(web.create_app(modem, sh, FakeSteam(), FakeDizque()))
    assert c.post("/api/channel/up").json()["channel"] == 1
    sh.fail = True
    assert c.post("/api/channel/up").status_code == 502
    sh.fail = False
    assert c.post("/api/channel/up").json()["channel"] == 2  # still from 1
    assert c.post("/api/channel/last").json()["channel"] == 1


def test_unknown_channel_direction_is_404(client, shield):
    assert client.post("/api/channel/sideways").status_code == 404
    assert shield.calls == []


def test_search_newpipe_shares_quoted_text(client, shield, caplog):
    shield.front_pkg = "org.schabi.newpipe"
    with caplog.at_level("INFO"):
        r = client.post("/api/search", json={"text": "'; reboot"})
    assert r.status_code == 200 and r.json() == {"searched": "NewPipe"}
    assert shield.calls[-1] == ("start am start -n org.schabi.newpipe/.RouterActivity -a android.intent.action.SEND"
                                " -t text/plain --es android.intent.extra.TEXT ''\"'\"'; reboot'")
    assert "reboot" not in caplog.text  # searches stay out of the log


@pytest.mark.parametrize("package,url", [
    ("com.spotify.tv.android", "spotify:search:%27%3B%20reboot%20caf%C3%A9"),
    ("com.apple.atve.androidtv.appletv", "https://tv.apple.com/search?term=%27%3B%20reboot%20caf%C3%A9"),
    ("com.wbd.stream", "https://play.max.com/search?q=%27%3B%20reboot%20caf%C3%A9"),
])
def test_search_url_apps_encode_the_query(client, shield, package, url):
    shield.front_pkg = package
    assert client.post("/api/search", json={"text": "  '; reboot café "}).status_code == 200
    # A URL with ? or & is shell-quoted as a whole.
    assert shield.calls[-1] == f"start am start -a android.intent.action.VIEW -d {shlex.quote(url)} -p {package}"


def test_search_unicode_reaches_intent_unchanged(client, shield):
    shield.front_pkg = "org.schabi.newpipe"
    assert client.post("/api/search", json={"text": "café 東京"}).status_code == 200
    assert shield.calls[-1].endswith("--es android.intent.extra.TEXT 'café 東京'")


def test_search_refuses_control_characters_and_long_text(client, shield):
    shield.front_pkg = "org.schabi.newpipe"
    assert client.post("/api/search", json={"text": "a\nreboot"}).status_code == 400
    assert client.post("/api/search", json={"text": "a" * 201}).status_code == 400
    assert client.post("/api/search", json={"text": "a" * 200}).status_code == 200
    assert client.post("/api/search", json={"text": "  "}).status_code == 400


def test_search_without_mechanism_is_404(client, shield):
    shield.front_pkg = "com.plexapp.android"
    assert client.post("/api/search", json={"text": "dune"}).status_code == 404
    shield.front_pkg = None
    assert client.post("/api/search", json={"text": "dune"}).status_code == 404
    assert not any(c.startswith("start") for c in shield.calls)


def test_app_action_needs_player_open(client, shield):
    r = client.post("/api/app-action/newpipe-repeat")
    assert r.status_code == 409 and r.json()["detail"] == "NewPipe's player isn't open"
    assert not any(c.startswith("broadcast") for c in shield.calls)
    shield.session = True
    assert client.post("/api/app-action/newpipe-repeat").status_code == 200
    assert shield.calls[-1] == ("broadcast org.schabi.newpipe.player.MainPlayer..player.MainPlayer.REPEAT"
                                " org.schabi.newpipe")


def test_unknown_app_action_is_404(client, shield):
    assert client.post("/api/app-action/reboot").status_code == 404
    assert shield.calls == []


PLAYER = {"speed": 1.25, "speeds": [0.5, 1, 1.25, 2], "quality": "auto", "qualities": [1080, 720]}


def test_now_includes_watch_player(client, shield):
    shield.state = shield_mod.Now(True, shield_mod.WATCH, None)
    shield.player = {"player": True, **PLAYER}
    assert client.get("/api/now").json()["player"] == PLAYER
    shield.player = {"player": False}  # Watch open, no video
    assert client.get("/api/now").json()["player"] is None
    shield.player = None  # Watch without a receiver
    assert client.get("/api/now").json()["player"] is None


def test_now_survives_watch_player_failure(client, shield, caplog):
    shield.state = shield_mod.Now(True, shield_mod.WATCH, shield_mod.Media("Dune", None, ["play-pause"]))
    shield.player = ShieldError("Watch sent unreadable state: 'x'")
    r = client.get("/api/now")
    assert r.status_code == 200
    assert r.json()["player"] is None and r.json()["media"]["title"] == "Dune"
    assert [rec.levelname for rec in caplog.records] == ["WARNING"]
    assert "unreadable state" in caplog.text


def test_watch_player_set(client, shield, caplog):
    with caplog.at_level("INFO"):
        r = client.post("/api/watch-player/speed?value=1.25")
    assert r.status_code == 200
    assert r.json() == {"kind": "speed", "value": "1.25"}
    assert shield.calls == ["watch speed 1.25"]
    assert "Watch speed set to 1.25" in caplog.text


def test_watch_player_unknown_setting_is_404(client, shield):
    assert client.post("/api/watch-player/volume?value=3").status_code == 404
    assert shield.calls == []


def test_watch_player_refused_is_400_with_reason(client, shield):
    shield.refuse = "no video playing"
    r = client.post("/api/watch-player/quality?value=1080")
    assert r.status_code == 400 and r.json()["detail"] == "no video playing"


def test_watch_player_shield_failure_is_502(modem, monkeypatch, caplog):
    monkeypatch.setattr(auth, "access_verified", lambda request: True)
    c = TestClient(web.create_app(modem, FakeShield(fail=True), FakeSteam(), FakeDizque()))
    assert c.post("/api/watch-player/quality?value=auto").status_code == 502
    assert "Watch quality 'auto' FAILED" in caplog.text


def test_watch_player_cross_site_refused(client, shield):
    r = client.post("/api/watch-player/speed?value=2", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert shield.calls == []
