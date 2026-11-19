"""dizquetv on the HX100G: its channel list, their thumbnails, and stream URLs.

The Shield plays a channel's stream in its built-in player (shield.play_stream). Thumbnails
are fetched here and passed to the browser, which can't reach the LAN when the
page is used away from home; only the icon each channel lists, and only from
dizquetv's own /images/ path, so this is no open proxy.
"""
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("remote.dizquetv")

TIMEOUT = 5.0
_ATTR = re.compile(r'([a-z-]+)="([^"]*)"')


class DizqueError(Exception):
    pass


class Dizque:
    def __init__(self, base_url: str):
        self._base = base_url.rstrip("/")

    def channels(self) -> list[dict]:
        """[{number, name, icon}] in channel order, from dizquetv's own M3U."""
        text = self._get("/api/channels.m3u").decode("utf-8", "replace")
        out = []
        for line in text.splitlines():
            if not line.startswith("#EXTINF"):
                continue
            attrs = dict(_ATTR.findall(line))
            number = attrs.get("tvg-chno", "")
            if not number.isdigit():
                continue
            name = attrs.get("tvg-name") or line.rsplit(",", 1)[-1].strip() or f"Channel {number}"
            out.append({"number": int(number), "name": name, "icon": attrs.get("tvg-logo", "")})
        out.sort(key=lambda c: c["number"])
        return out

    def stream_url(self, number: int) -> str:
        return f"{self._base}/video?channel={number}"

    def icon(self, number: int) -> tuple[bytes, str] | None:
        """The channel's thumbnail (bytes, content type), or None if it has none."""
        ch = next((c for c in self.channels() if c["number"] == number), None)
        if ch is None or not ch["icon"]:
            return None
        # dizquetv writes icons as http://localhost:8000/images/...; fetch the
        # same path from the real host, and nothing outside /images/.
        path = urllib.parse.urlsplit(ch["icon"]).path
        if not path.startswith("/images/") or ".." in path:
            log.warning("channel %d icon outside /images/, not fetched: %s", number, ch["icon"])
            return None
        body, ctype = self._get_typed(path)
        if not ctype.startswith("image/"):
            raise DizqueError(f"channel {number} icon isn't an image ({ctype})")
        return body, ctype

    def _get(self, path: str) -> bytes:
        return self._get_typed(path)[0]

    def _get_typed(self, path: str) -> tuple[bytes, str]:
        try:
            with urllib.request.urlopen(self._base + path, timeout=TIMEOUT) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise DizqueError(f"dizquetv GET {path}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise DizqueError(f"dizquetv GET {path}: {type(e).__name__}: {e}") from e
