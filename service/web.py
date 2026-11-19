"""remote — the living-room remote: the Onkyo TX-SR3100 over IR, through the ATOM Lite serial IR modem."""
import logging
import shlex
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

import apps
import auth
import config
import scenes
from commands import COMMANDS, MAX_STEPS, STEP_GAP
from modem import Modem, ModemError
from shield import KEYS, WATCH, WATCH_SETTINGS, Refused, Shield, ShieldError
from steam import Steam, SteamError
from dizquetv import Dizque, DizqueError

log = logging.getLogger("remote")
STATIC = Path(__file__).parent / "static"


def setup_logging() -> None:
    logging.Formatter.converter = time.gmtime
    fmt = logging.Formatter("%(asctime)s %(levelname)s remote %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(config.LOG_DIR / "app.log", maxBytes=10_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


class TypedText(BaseModel):
    text: str


def next_channel(numbers: list[int], current: int | None, direction: str) -> int:
    """Channel up/down through `numbers` (channel order), wrapping around.
    From an unknown current channel, up is the first and down the last."""
    if current not in numbers:
        return numbers[0] if direction == "up" else numbers[-1]
    i = numbers.index(current) + (1 if direction == "up" else -1)
    return numbers[i % len(numbers)]


def create_app(modem: Modem, shield: Shield, steam: Steam, dizque: Dizque) -> FastAPI:
    app = FastAPI(title="remote", docs_url=None, redoc_url=None, openapi_url=None)
    # The channel playing and the one before it, for channel up/down/last.
    # Set by /api/watch and /api/channel when the stream started; lost on restart.
    tuned: dict[str, int | None] = {"current": None, "previous": None}
    tuning = threading.Lock()

    def _tuned_to(number: int, steps: list[scenes.Step]) -> None:
        if steps and steps[-1].ok and tuned["current"] != number:
            tuned["previous"], tuned["current"] = tuned["current"], number

    @app.get("/healthz")
    def healthz():
        """The modem answers ping: the container can reach the ESP over USB."""
        try:
            version = modem.ping()
        except ModemError as e:
            log.error("health check FAILED: %s", e)
            raise HTTPException(status_code=503, detail="IR modem not answering") from None
        return {"ok": True, "modem": version}

    @app.get("/", dependencies=[Depends(auth.require_access)])
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/commands", dependencies=[Depends(auth.require_access)])
    def list_commands():
        return {name: {"label": c.label, "steppable": c.steppable} for name, c in COMMANDS.items()}

    @app.post("/api/scene/{scene}", dependencies=[Depends(auth.require_control)])
    def scene(scene: str):
        if scene not in scenes.SCENES:
            raise HTTPException(status_code=404, detail="no such scene")
        return _scene_reply(f"scene {scene}", scenes.run(scene, modem, shield), {"scene": scene})

    def _scene_reply(label: str, steps: list[scenes.Step], body: dict):
        body = {**body, "steps": [s.__dict__ for s in steps]}
        if not all(s.ok for s in steps):
            failed = ", ".join(s.name for s in steps if not s.ok)
            return JSONResponse(status_code=502, content={**body, "detail": f"{failed} failed"})
        log.info("%s done", label)
        return body

    @app.post("/api/key/{name}", dependencies=[Depends(auth.require_control)])
    def key(name: str):
        if name not in KEYS:
            raise HTTPException(status_code=404, detail="no such key")
        try:
            shield.key(name)
        except ShieldError as e:
            log.error("key %s FAILED: %s", name, e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        return {"key": name}

    @app.get("/api/now", dependencies=[Depends(auth.require_access)])
    def now():
        # Polled every few seconds while the page is open: only failures log.
        try:
            state = shield.now()
        except ShieldError as e:
            log.error("reading what's on the TV FAILED: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        entry = apps.lookup(state.app) if state.app else None
        media = state.media
        return {
            "awake": state.awake,
            "app": {"package": state.app, "label": entry.label} if entry else None,
            "media": {"title": media.title, "subtitle": media.subtitle, "controls": media.controls} if media else None,
            "extras": [x.as_json() for x in entry.extras] if entry else [],
            "player": _watch_player() if state.app == WATCH else None,
        }

    def _watch_player() -> dict | None:
        """Watch's speed and quality choices while its player is open. Extra
        detail: a failure leaves them off rather than failing /api/now."""
        try:
            player = shield.watch_player()
        except ShieldError as e:
            log.warning("couldn't read Watch's player state: %s", e)
            return None
        if not player or not player["player"]:
            return None
        return {k: player[k] for k in ("speed", "speeds", "quality", "qualities")}

    @app.post("/api/watch-player/{kind}", dependencies=[Depends(auth.require_control)])
    def watch_player(kind: str, value: str = Query(..., min_length=1, max_length=16)):
        if kind not in WATCH_SETTINGS:
            raise HTTPException(status_code=404, detail="no such Watch setting")
        try:
            shield.watch_player_set(kind, value)
        except Refused as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except ShieldError as e:
            log.error("Watch %s %r FAILED: %s", kind, value, e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        log.info("Watch %s set to %s", kind, value)
        return {"kind": kind, "value": value}

    @app.post("/api/tv/{state}", dependencies=[Depends(auth.require_control)])
    def tv(state: str):
        if state not in scenes.TV_STATES:
            raise HTTPException(status_code=404, detail="no such TV state")
        return _scene_reply(f"tv {state}", scenes.tv(state, shield), {"tv": state})

    @app.post("/api/text", dependencies=[Depends(auth.require_control)])
    def text(body: TypedText):
        # The text itself isn't logged: it's whatever Matt searched for.
        try:
            shield.type_text(body.text)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        except ShieldError as e:
            log.error("typing %d characters FAILED: %s", len(body.text), e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        log.info("typed %d characters and Enter", len(body.text))
        return {"typed": len(body.text)}

    @app.get("/api/channels", dependencies=[Depends(auth.require_access)])
    def channels():
        try:
            return {"channels": [{"number": c["number"], "name": c["name"]} for c in dizque.channels()]}
        except DizqueError as e:
            log.error("channel list FAILED: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from None

    @app.get("/api/channel-art/{number}", dependencies=[Depends(auth.require_access)])
    def channel_art(number: int):
        try:
            icon = dizque.icon(number)
        except DizqueError as e:
            log.error("channel %d thumbnail FAILED: %s", number, e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        if icon is None:
            # No thumbnail: the tile shows the name only.
            raise HTTPException(status_code=404, detail="no thumbnail")
        body, ctype = icon
        return Response(body, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/watch/{number}", dependencies=[Depends(auth.require_control)])
    def watch(number: int):
        try:
            known = {c["number"] for c in dizque.channels()}
        except DizqueError as e:
            log.error("channel list FAILED: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        if number not in known:
            raise HTTPException(status_code=404, detail="no such channel")
        with tuning:
            steps = scenes.watch(number, dizque.stream_url(number), modem, shield)
            _tuned_to(number, steps)
        return _scene_reply(f"watch {number}", steps, {"watch": number})

    @app.post("/api/channel/{direction}", dependencies=[Depends(auth.require_control)])
    def channel_step(direction: str):
        """Channel up/down (channel order, wrapping) or back to the last one,
        in the channel player: no amp or wake steps."""
        if direction not in apps.CHANNEL_DIRS:
            raise HTTPException(status_code=404, detail="no such channel direction")
        with tuning:
            if direction == "last":
                number = tuned["previous"]
                if number is None:
                    raise HTTPException(status_code=409, detail="no previous channel yet")
            else:
                try:
                    numbers = [c["number"] for c in dizque.channels()]
                except DizqueError as e:
                    log.error("channel list FAILED: %s", e)
                    raise HTTPException(status_code=502, detail=str(e)) from None
                if not numbers:
                    raise HTTPException(status_code=409, detail="dizquetv has no channels")
                number = next_channel(numbers, tuned["current"], direction)
            steps = scenes.switch_channel(number, dizque.stream_url(number), shield)
            _tuned_to(number, steps)
        return _scene_reply(f"channel {direction} to {number}", steps, {"channel": number})

    @app.get("/api/apps", dependencies=[Depends(auth.require_access)])
    def app_list():
        return apps.launchable()

    @app.post("/api/launch/{package}", dependencies=[Depends(auth.require_control)])
    def launch(package: str):
        entry = apps.APPS.get(package)
        if entry is None or entry.launch is None:
            raise HTTPException(status_code=404, detail="no such app")
        try:
            shield.start(f"am start -n {shlex.quote(entry.launch)}")
        except ShieldError as e:
            log.error("launch %s FAILED: %s", package, e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        log.info("launched %s", package)
        return {"launched": package}

    @app.post("/api/search", dependencies=[Depends(auth.require_control)])
    def search(body: TypedText):
        # The query itself isn't logged: it's whatever Matt searched for.
        try:
            query = apps.search_text(body.text)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        try:
            package = shield.front()
        except ShieldError as e:
            log.error("search: reading the front app FAILED: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        entry = apps.lookup(package) if package else None
        if entry is None or entry.search is None:
            raise HTTPException(status_code=404, detail="the app on the TV has no search")
        try:
            shield.start(entry.search(query))
        except ShieldError as e:
            log.error("search in %s FAILED: %s", package, e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        log.info("searched %s for %d characters", package, len(query))
        return {"searched": entry.label}

    @app.post("/api/app-action/{name}", dependencies=[Depends(auth.require_control)])
    def app_action(name: str):
        act = apps.ACTIONS.get(name)
        if act is None:
            raise HTTPException(status_code=404, detail="no such action")
        label = apps.lookup(act.package).label
        try:
            # The receiver lives in the app's player, which has a media
            # session while it's open; am's result can't tell (apps.py).
            if not shield.has_session(act.package):
                raise HTTPException(status_code=409, detail=f"{label}'s player isn't open")
            shield.broadcast(act.action, act.package)
        except ShieldError as e:
            log.error("app action %s FAILED: %s", name, e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        log.info("app action %s sent", name)
        return {"action": name}

    @app.get("/api/games", dependencies=[Depends(auth.require_access)])
    def games():
        try:
            return steam.library()
        except SteamError as e:
            log.error("games list FAILED: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from None

    @app.get("/api/art/{appid}/{kind}", dependencies=[Depends(auth.require_access)])
    def art(appid: str, kind: str):
        if not appid.isdigit() or kind not in ("cover", "wide"):
            raise HTTPException(status_code=404, detail="no such art")
        try:
            image = steam.art(appid, kind)
        except SteamError as e:
            log.error("art for %s FAILED: %s", appid, e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        if image is None:
            # Not every game has every image cached; the tile shows the title only.
            raise HTTPException(status_code=404, detail="no cached art")
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/quit-game", dependencies=[Depends(auth.require_control)])
    def quit_game():
        try:
            message = steam.quit_game()
        except SteamError as e:
            log.error("quit game FAILED: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from None
        log.info("quit game: %s", message)
        return {"quit": True, "message": message}

    @app.post("/api/play/{appid}", dependencies=[Depends(auth.require_control)])
    def play(appid: str):
        if not appid.isdigit():
            raise HTTPException(status_code=400, detail="bad app ID")
        return _scene_reply(f"play {appid}", scenes.play(appid, modem, shield, steam), {"play": appid})

    @app.post("/api/{name}", dependencies=[Depends(auth.require_control)])
    def send(name: str, steps: int = Query(1, ge=1, le=MAX_STEPS)):
        cmd = COMMANDS.get(name)
        if cmd is None:
            raise HTTPException(status_code=404, detail="no such command")
        if steps > 1 and not cmd.steppable:
            raise HTTPException(status_code=400, detail=f"{name} can't be repeated")
        sent = 0
        try:
            for _ in range(steps):
                if sent:
                    time.sleep(STEP_GAP)
                modem.nec(cmd.address, cmd.command)
                sent += 1
        except ModemError as e:
            log.error("%s FAILED after %d of %d frames: %s", name, sent, steps, e)
            raise HTTPException(status_code=502, detail=f"IR modem: {e}") from None
        log.info("sent %s x%d", name, steps)
        return {"sent": name, "steps": steps}

    return app


def main_app() -> FastAPI:
    setup_logging()
    if config.ACCESS_DEV_BYPASS:
        log.warning("ACCESS_DEV_BYPASS is on: every request is treated as logged in")
    return create_app(Modem(config.SERIAL_PORT), Shield(config.SHIELD_ADDR, config.ADB_KEY),
                      Steam(config.STEAM_API, config.LAUNCH_TOKEN), Dizque(config.DIZQUETV_URL))
