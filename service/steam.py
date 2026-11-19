"""Steam on the HX100G, through the host's deploy-api with a launch-only token.

That token opens three routes and nothing else: the installed games (ID and
name, most recently played first, plus the one running), launching one of
them, and quitting the running game. deploy-api refuses an app ID that isn't
installed, and holds a launch until the PC sees a display. The dashboard's
full token, which can change launch options and so run anything as matt,
never comes near this container.
"""
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("remote.steam")

TIMEOUT = 10.0  # seconds; deploy-api answers from local files
QUIT_TIMEOUT = 40.0  # quit waits up to 15 s for the game to close, then 10 s after SIGTERM
HEADER = "X-Bix-Launch-Token"


class SteamError(Exception):
    pass


class _NotFound(SteamError):
    pass


class Steam:
    def __init__(self, base_url: str, token_path: str):
        self._base = base_url.rstrip("/")
        self._token_path = token_path

    def library(self) -> dict:
        """{"games": [{appid, name}, ...], "running": {appid, name} | None}."""
        body = self._call("GET", "/steam/games")
        games = body.get("games")
        if not isinstance(games, list):
            raise SteamError("deploy-api returned no games list")
        running = body.get("running")
        return {
            "games": [{"appid": str(g["appid"]), "name": str(g["name"])} for g in games],
            "running": {"appid": str(running["appid"]), "name": str(running["name"])} if running else None,
        }

    def art(self, appid: str, kind: str) -> bytes | None:
        """Steam's cached image for a game ("cover" portrait, "wide" hero), or
        None when Steam has none cached."""
        if not appid.isdigit() or kind not in ("cover", "wide"):
            raise SteamError(f"bad art request {appid!r} {kind!r}")
        try:
            return self._fetch("GET", f"/steam/art/{appid}/{kind}", None, TIMEOUT)
        except _NotFound:
            return None

    def quit_game(self) -> str:
        body = self._call("POST", "/steam/quit-game", {}, timeout=QUIT_TIMEOUT)
        if not body.get("ok"):
            raise SteamError(f"quit refused: {body.get('error') or body.get('message') or body}")
        return str(body.get("message", ""))

    def launch(self, appid: str) -> None:
        if not appid.isdigit():
            raise SteamError(f"bad app ID {appid!r}")
        body = self._call("POST", "/steam/launch", {"appid": appid})
        if not body.get("ok"):
            raise SteamError(f"launch of {appid} refused: {body.get('error') or body.get('message') or body}")

    def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = TIMEOUT) -> dict:
        raw = self._fetch(method, path, payload, timeout)
        try:
            return json.loads(raw or b"{}")
        except ValueError as e:
            raise SteamError(f"deploy-api {method} {path}: bad JSON: {e}") from e

    def _fetch(self, method: str, path: str, payload: dict | None, timeout: float) -> bytes:
        try:
            with open(self._token_path) as f:
                token = f.read().strip()
        except OSError as e:
            raise SteamError(f"launch token unreadable: {e}") from e
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method,
                                     headers={HEADER: token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # A refused launch comes back as an error status with a JSON reason.
            try:
                reason = json.loads(e.read() or b"{}").get("error", "")
            except ValueError:
                reason = ""
            cls = _NotFound if e.code == 404 else SteamError
            raise cls(f"deploy-api {method} {path}: HTTP {e.code}" + (f": {reason}" if reason else "")) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SteamError(f"deploy-api {method} {path}: {type(e).__name__}: {e}") from e
