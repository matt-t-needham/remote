# av_control

Stack: Arduino (ESP32) firmware + Python FastAPI. The `remote` container owns
the serial port and serves named commands (on/off/inputs/volume) behind
Access.

Onkyo TX-SR3100 control over IR. Codes, wiring, placement and test history
are in `README.md`; this file is how to work on it.

## Layout

- `firmware/`: Arduino (PlatformIO) serial IR modem for the M5Stack ATOM Lite.
  It knows nothing about the amp: `nec <addr> <cmd>` → `ok`/`err`, `ping` →
  `pong`, and it reports frames it hears as `rx nec …`. Protocol is documented
  at the top of `firmware/src/main.cpp`. Matt may rewrite it in Rust; keep the
  line protocol identical so the service doesn't change.
- `service/`: the `remote` container (FastAPI). `commands.py` is the only place
  that knows amp codes. `modem.py` waits for every reply: a send that wasn't
  acknowledged raises, never returns quietly.
- `service/shield.py` + `scenes.py`: the Shield over network ADB (wake/sleep,
  keys, `type_text` = `input text` + Enter) and the TV / GAME scenes, plus
  `tv on|off` (Shield wake/sleep only; the amp is untouched). The ADB key is the container's own, in
  `~/.config/bix/remote-adb/` (mounted read-only). To re-approve it (e.g. after
  the Shield is reset): wake the Shield, put the amp on BD/DVD, run
  `docker compose exec -T remote python -c "import config, shield;
  print(shield.Shield(config.SHIELD_ADDR, config.ADB_KEY).pair(90))"` and
  accept the prompt on the TV.
- `service/apps.py`: the one table of Shield apps (package → label, launch
  component, search intent, and page extras as typed objects: `key`,
  `dialog`, `search`, `action`, `channel`). The page renders extras by type and
  only ever names a key, action, package or direction; every shell string is
  built server-side. Entries marked `# untested` haven't been tried on the TV.
  The main page polls `GET /api/now`
  (`Shield.now()`: power, front app, `dumpsys media_session`) and shows the
  front app, what's playing and the media keys its session advertises above
  the base controls. A session's play state is stale, so it's never shown.
- `service/steam.py`: Play a game. Lists installed games and launches one
  through the host's deploy-api with a launch-only token
  (`~/.config/bix/remote-launch-token`, mounted :ro); `scenes.play` launches
  first, then runs GAME (deploy-api holds the launch until HDMI connects), and
  `quit_game` closes the running game. deploy-api refuses that token on every other route
  (bix-infra CLAUDE.md, Security): never give this container the dashboard's
  deploy token.
- `amp-ir.yaml`: the ESPHome config used for capture and testing on
  2026-10-29. Not what's flashed now; kept for capturing codes with
  `dump: all` if ever needed.

## Commands

- Tests: `cd service && docker build --target test .` (the compose build runs
  them too).
- Firmware build: `./pio.sh run`; flash: `docker compose stop remote` (from
  `bix-infra/`), then `sg dialout -c "./pio.sh run -t upload"`, then
  `docker compose up -d remote`. PlatformIO's toolchain lives in `.pio-core/`.
- Deploy the service: from `bix-infra/`, `docker compose build remote && docker
  compose up -d remote`; check `curl -s http://127.0.0.1:3016/healthz` (pings
  the ESP).

## Gotchas

- One process owns the serial port (the service opens it exclusively). Stop
  `remote` before flashing or using any other serial tool.
- The emitter's range is a few cm: it's taped to the amp's IR window. "Sent ok"
  means the light went out, not that the amp acted; IR has no feedback.
- `On`/`Off` are discrete and safe to repeat; the remote's own Power is a toggle
  and isn't exposed.
- HDMI connector status on the HX100G is only an "amp went off" signal, not "amp
  is on" (README, task 7).
- Every request needs a valid Access JWT (`ACCESS_AUD` in compose), so
  `curl 127.0.0.1:3016/api/...` gets 403, including from inside the container.
  Test IR through remote.bix.computer in a logged-in browser. The Shield alone
  can be driven with `docker exec apps-remote-1 python` + `shield.Shield` (ADB
  needs no port), but a second `Modem` can't open the serial port while the
  service holds it. Never re-add `ACCESS_DEV_BYPASS` for a test.
- The Shield's address (192.168.0.36) is hard-coded in compose; if its DHCP
  lease changes, scenes fail with an ADB error until SHIELD_ADDR is updated.
- If the ESP is replugged, the container keeps a stale device node: healthz goes
  503 and the healthcheck unhealthy until `docker compose up -d --force-recreate remote`.

## Deployment

Port 3016: remote (remote.bix.computer) — living-room remote, Onkyo IR control
for now (`av_control/service`); owns the ATOM Lite's USB serial port
(`devices:` passthrough, `group_add` dialout); own `remote` network
(172.30.16.0/24, egress for Access keys and the Shield's network ADB at
192.168.0.36:5555 using its own key, `~/.config/bix/remote-adb`, mounted :ro);
Access JWT checked in-app on every request (`ACCESS_AUD` in compose; the dev
bypass was removed); Play a game reaches deploy-api (172.17.0.1:9100) with the
launch-only token only (see `bix-infra/SECURITY.md`).
