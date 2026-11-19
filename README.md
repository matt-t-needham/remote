# av_control

IR control of the Onkyo TX-SR3100 from the HX100G.

```
phone ──HTTPS (Access)──> remote container ──USB serial──> ATOM Lite ──IR──> Onkyo
```

- **ESP:** M5Stack ATOM Lite (ESP32-PICO-D4) + M5Stack IR Unit on the Grove
  port, running `firmware/` (Arduino): a serial IR modem with no network.
  Protocol at the top of `firmware/src/main.cpp`.
- **Service:** `service/`, the `remote` compose service on 127.0.0.1:3016. Named
  commands only (`POST /api/on|off|game|shield|mute|volume-up|volume-down`,
  volume `?steps=1..5`), scenes (`POST /api/scene/tv|game`), the TV alone
  through the Shield (`POST /api/tv/on|off`), typing on the TV
  (`POST /api/text {"text": …}`: ASCII, ≤200 chars, then Enter), the front
  app's own search (`POST /api/search {"text": …}`, by intent; 404 if it has
  none), launching an app (`GET /api/apps`, `POST /api/launch/<package>`),
  channel up/down/last in the channel player (`POST /api/channel/up|down|last`),
  app player actions (`POST /api/app-action/<name>`), all allowlisted in
  `apps.py`; a phone page at
  `/`, `/healthz` pings the ESP. Held volume keeps two requests queued, so
  frames go back to back at the NEC period (108 ms, padded by IRremoteESP8266),
  the same rate as the amp's own remote; that is the ceiling.
- **Shield:** network ADB at 192.168.0.36:5555 with the container's own key
  (`~/.config/bix/remote-adb/`, shown on the Shield as `unknown@b2ad5ef7654c`,
  approved 2026-10-29). Wake and sleep only; each call checks the Shield's power
  state really changed.
- **Serial port:** `/dev/serial/by-id/usb-Hades2001_M5stack_695268894B-if00-port0`
  (FTDI, kernel `ftdi_sio` driver; matt is in `dialout`). Opening it does not
  reset the board. One owner at a time.
- `amp-ir.yaml` is the ESPHome config used for capture and testing on
  2026-10-29 (stages 1–5 in `captures/`); ESPHome is no longer flashed.
- Working notes: `CLAUDE.md`.

## Wiring and placement

- IR Unit pins (tested): receiver **GPIO32**, emitter **GPIO26**, emitter not
  inverted.
- The emitter has a range of a few cm: it must sit right at the amp's front IR
  window, LED facing in. Currently taped on. Once it faces the amp its receiver
  can't hear the Onkyo remote; unplug it to capture more codes.

## Onkyo codes

NEC, as ESPHome reports them. JP1 notation in brackets (device.subdevice OBC).

| Function | NEC address | command | Source | Tested on the amp |
|---|---|---|---|---|
| Power toggle | 0x6CD2 | 0x34CB | captured (210.108 203) | yes |
| **Power on (discrete)** | 0x6DD2 | 0xFB04 | code file (210.109 4) | yes: wakes it with one frame; ignored when already on |
| **Power off (discrete)** | 0x6CD2 | 0xB847 | code file (210.108 71) | yes: standby; ignored when already off |
| GAME | 0x6DD2 | 0xF20D | captured | yes |
| BD/DVD (Shield) | 0x6CD2 | 0x738C | captured | yes |
| Volume up | 0x6DD2 | 0xFD02 | captured | yes |
| Volume down | 0x6DD2 | 0xFC03 | captured | yes |
| Mute | 0x6DD2 | 0xFA05 | captured | not yet sent since the emitter was placed |

The remote only has the toggle. The discrete codes come from an Onkyo code
file quoted on the JP1 forum
(<http://www.hifi-remote.com/forums/viewtopic.php?p=85412>); there, 210.109/4
reportedly acted as a toggle on a TX-SR501, but on this amp it doesn't.
Volume buttons resend the full frame about every 108 ms while held.

## Scenes

Tested end to end through the service on 2026-10-29. Every step runs even if
one fails; any failure makes the request a 502 that lists each step.

| Scene | Steps | TV |
|---|---|---|
| TV | amp On, BD/DVD, player force-stop (built-in player, and VLC in case it is open; TV after a channel lands on the home screen), Shield wake, no waits | comes on via the Shield's CEC one-touch play |
| GAME | amp On, GAME, Shield wake (stays awake), no waits: TV on ~3 s after the request | comes on via the Shield's CEC one-touch play; the TV asks the amp for the Shield's input, the amp (CEC off) ignores it and the desktop shows |
| Off | player force-stop (built-in player and VLC), Shield sleep, amp Off | usually follows the Shield to standby (4 of 5 tries on 2026-10-30); not guaranteed |
| Channels | list of dizquetv's channels (names and thumbnails from its M3U, thumbnails proxied from its `/images/` only); holding one runs amp On, BD/DVD, Shield wake, then the Shield's built-in player opens `<DIZQUETV_URL>/video?channel=N` (`am start -n com.android.gallery3d/.app.MovieActivity -a VIEW -d … -t video/mp2t`) | as TV. Not VLC: it played the picture but discarded the audio until the next file in the stream ("PCR called too late", "discarded audio buffer"); the built-in player has sound at once (Matt, 2026-10-30). dizquetv's `/m3u8` is a fake playlist (100+ zero-length copies of one stream) and VLC never opens a player for it |
| Play | Steam launch (host deploy-api, launch-only token), then amp On, GAME, Shield wake | as GAME. deploy-api holds the launch until the PC sees the display: it wakes the runtime-suspended GPU (`xrandr --query`) at once and re-probes every 2 s, since a sleeping card never notices the amp/TV coming up. Tested from all off: launched 11 s after the press. Quit game (same slot while a game runs) closes the game's windows; Steam stays up |

The HX100G's desktop came up on GAME both times it was tested that evening
(HDMI reconnected within 8 s of an IR power-on, unlike earlier in the day).

**Amp before Shield (2026-10-30).** The 2026-10-29 tests started with the TV
already on. From a TV in standby, the old order (Shield wake, then amp On)
turned the TV on and then back to standby about 5 s later: the amp powered up
while the TV was still starting. ARC is on, which keeps the amp on the CEC bus
even with its CEC setting off, and ARC stays on for AirPlay audio from the TV.
Each part alone was fine with the TV settled; amp On first, then the Shield,
was tested from TV standby and the TV stayed on. GAME hit the same thing when
it switched input after the wake (TV on, GAME, TV standby 3 s later), so GAME
now selects its input first and wakes the Shield last; tested from standby,
desktop on screen. Don't wake the Shield and then sleep it a few seconds later
to get the TV on: that left the TV cycling on and off.

The amp's own CEC can't power the TV either way (tested with HDMI CEC on:
amp On didn't wake it, amp Off didn't standby it), so it's back to Off; ARC on,
HDMI Standby Through on Auto (keeps the PC's HDMI connected while the amp is
off, so the desktop shows within seconds of the TV coming on).

## CEC and remote settings (2026-10-29)

- TV: Anynet+ on. IP Remote off (keeps the TV off the control path; see the
  TV lockdown notes).
- Shield HDMI-CEC: one-touch play on, set TV input to Shield on, turn TV off
  **off** (else the Shield's 20-min sleep would kill a gaming session), wake
  Shield when TV powers on off.
- Shield remote IR power control: TV discrete; amp toggle (discrete off). The
  IR Unit currently covers the amp's window, so the Shield remote can't reach
  the amp anyway. Switch the amp to discrete if that changes.
- The amp does not send CEC standby when switched off by IR: the TV stays on.

## Amp state via HDMI (task 7)

`/sys/class/drm/card1-HDMI-A-1/status` goes `disconnected` within ~1 s of the
amp entering standby. It does **not** reliably come back when the amp turns on:
after a single discrete On (2026-10-29 19:06) the amp was on but the connector
stayed disconnected for 5+ minutes, and an extra On didn't bring it back. Earlier
it reconnected 30–70 s after power-on for reasons not yet known. So it's a
usable "amp went off" signal, not an "amp is on" signal. With discrete codes,
scenes don't need state anyway.

## Not yet known

- Why HDMI to the HX100G sometimes doesn't come back after power-on, and whether
  selecting GAME brings it back.
- Range and angle limits beyond "a few cm, straight on".
- Other input codes (only GAME and BD/DVD captured).
- Whether the Arduino firmware's IR reaches the amp: its byte order is checked
  against the captured frames, but it hasn't been fired at the amp yet.

## Watching a dizquetv channel on the Shield (tested 2026-10-30)

Plex for Android TV (2026.20.0) takes `plex://` and watch.plex.tv links but
only opens its main screen; no documented link opens a DVR channel, so Plex
isn't used. Instead, an ADB intent opens dizquetv's stream for a channel in
the Shield's built-in player:

    am start -n com.android.gallery3d/.app.MovieActivity -a android.intent.action.VIEW \
      -d 'http://192.168.0.47:8000/video?channel=2' -t video/mp2t

Rage is channel 2 (`/api/channels.m3u` lists them). Tested with the Shield
awake: player resumed, NuPlayer running, dizquetv streaming 1080p50 H.264 + AAC
for 55 s, then `am force-stop com.android.gallery3d` closed the stream
(`Client Closed`). Not seen on screen (nobody home). No VLC or other player
was installed then. VLC (3.7.2) was added later the same day and briefly played
channels, but lost the audio until the next file in the stream, so channels
went back to the built-in player (see the Channels row above). VLC is still
installed; `stop_player` force-stops it too in case it is open.

### Start-up delay (2026-11-14)

The built-in player holds the first frame until the stream's clock (PCR)
reaches its first timestamp (PTS), and dizquetv puts that gap there with its
`concatMuxDelay` setting (`-muxdelay N -muxpreload N` on the outer concat
ffmpeg). At 5 (set on 2026-09-19 for the Plex Android TV app) a fresh stream
had PCR 5.0 s / first PTS 10.04 s: ~5 s of black after dizquetv's 0.67 s to
first byte. Set to 1 (PCR 1.0 s / PTS 2.04 s) on 2026-11-14, then 2 the same
evening (PCR 2.0 s / PTS 4.04 s) because 1 was too short in use (Matt). It
applies per stream, so reopen a channel to pick up a change. The 09-19
symptom it guards against: stalls at track boundaries, worst seen 1.35 s. History in
`bix-infra/todos/rageagain_channel.md`.

Changing it: don't use dizquetv's web UI or `PUT /api/ffmpeg-settings`, which
refuse every ffmpeg settings change on this install; edit the file instead.
Why and how: "Changing DizqueTV" in `rageagain_channel/CLAUDE.md`.

## Switching between TV and Play (tested 2026-10-30)

- **Play → TV** with everything on: the amp goes back to BD/DVD, the Shield
  (still awake) is the picture again. No standby.
- **TV → Play** with everything on: the Samsung goes to standby ~2–3 s after
  the Shield stops being its picture, whether the amp switches away or the
  Shield sleeps (both tried). Play lets it happen and recovers: it notes the
  Shield was awake, sends GAME, waits for the Shield to follow the TV into
  standby, waits 4 s more (a wake while the TV is still turning off is ignored
  and it drops again), then wakes the Shield; the TV comes back on GAME. The
  screen is off for ~7 s; the request takes ~7 s.
- Shield wake/sleep send nothing when it's already in that state: a WAKEUP to
  an awake Shield still makes it claim the TV (Text View On + Active Source).
- Shield remote (d-pad, OK, Back): `input keyevent` over one kept-open ADB
  connection, ~450–560 ms per press, almost all of it Android's `input`
  command starting up. Writing events straight to the input device
  (`sendevent`) would be much faster; not done.

## Game tiles

Steam's own cached art, served by deploy-api to the launch-only token
(`/steam/art/<appid>/cover|wide`, installed games only): `library_600x900`
portrait on phones, `library_hero` on wide screens, falling back through
the header images; a game with none shows its title only.
