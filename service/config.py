import os
from pathlib import Path

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/amp-ir")
# Shield network ADB, and the container's own ADB key (adbkey + adbkey.pub).
SHIELD_ADDR = os.environ.get("SHIELD_ADDR", "192.168.0.36:5555")
ADB_KEY = os.environ.get("ADB_KEY", "/keys/adbkey")
LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))
# dizquetv on the host: the channel list, thumbnails and streams (the Shield
# plays the streams in its built-in player, so this must be an address the Shield can reach).
DIZQUETV_URL = os.environ.get("DIZQUETV_URL", "http://192.168.0.47:8000")
# Steam on the host, via deploy-api's launch-only token (list + launch only).
STEAM_API = os.environ.get("STEAM_API", "http://172.17.0.1:9100")
LAUNCH_TOKEN = os.environ.get("LAUNCH_TOKEN", "/keys-launch/remote-launch-token")

# Cloudflare Access in front; the app checks the JWT itself. Unset ACCESS_AUD
# fails closed: every control request is refused.
ACCESS_TEAM_DOMAIN = os.environ.get("ACCESS_TEAM_DOMAIN", "bixcomputer.cloudflareaccess.com")
ACCESS_AUD = os.environ.get("ACCESS_AUD", "")
# Local testing only, while remote.bix.computer has no tunnel route (the port
# is on 127.0.0.1, out of containers' reach). Logged as a WARNING at startup.
ACCESS_DEV_BYPASS = os.environ.get("ACCESS_DEV_BYPASS", "") == "1"

# The page's own origin. Browser POSTs from anywhere else are refused (CSRF:
# sibling *.bix.computer sites are same-site and carry the Access cookie).
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "https://remote.bix.computer")
