"""YouTube Music's explicit (E) badge: is the video you downloaded the clean version?

Labels put an album on YouTube Music twice when it has a clean edit: once with
the E badge and once without. Testing five real clean/explicit pairs showed the
audio alone can't tell them apart (a few muted words look like normal pauses),
but the badge did every time. So for a song's YouTube link we ask:

  * does this exact video have the E badge?  -> explicit, nothing to show
  * it's an official album track without the badge, and YouTube Music has a
    badged copy of the same song (same title, same length)? -> clean: offer to swap
  * the video isn't an official YouTube Music track (a user upload or music
    video)                                     -> unofficial: never flagged, since
                                                  YouTube Music can't say which it is
  * no badged copy anywhere                    -> the song has no explicit version
  * anything unexpected                        -> unknown (never a false "clean")

This reads YouTube Music's public web API (the same one its website uses). It's
not an official API, so the parser is defensive: if the format changes, the
result is "unknown".
"""

import logging
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx

from . import cutting, db

log = logging.getLogger("djcrate.ytmusic")

BASE = "https://music.youtube.com/youtubei/v1"
CONTEXT = {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20241106.01.00", "hl": "en"}}
HEADERS = {"Content-Type": "application/json", "Origin": "https://music.youtube.com", "User-Agent": "Mozilla/5.0"}
SONGS_FILTER = "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"   # search "Songs" only
EXPLICIT = "MUSIC_EXPLICIT_BADGE"
GAP_S = 1.5              # between any two requests
LENGTH_TOLERANCE_MS = 5000
CLOSE_LENGTH_MS = 2000   # when the artist names differ (e.g. "$uede" vs "MoneySign Suede"), the length must be this close
SEARCH_CACHE_S = 7 * 86400
RULES = 2   # bump when the check changes, so every song is checked again under the new rules

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class YTMError(Exception):
    """YouTube Music is unreachable or pushing back: try again later."""


def video_id(url: str | None) -> str | None:
    """The 11-character id of a YouTube / YouTube Music link, else None."""
    if not url:
        return None
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    vid = None
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [None])[0]
        else:
            m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", u.path)
            vid = m.group(1) if m else None
    return vid if vid and _VIDEO_ID.match(vid) else None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


# ---------------------------------------------------------------- parsing (defensive: missing keys -> None)

def _walk(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, key)


def _runs(node) -> list[dict]:
    return [r for r in ((node or {}).get("runs") or []) if isinstance(r, dict)]


def _text(node) -> str:
    return "".join(r.get("text", "") for r in _runs(node))


def _page_type(run: dict) -> str:
    return (((run.get("navigationEndpoint") or {}).get("browseEndpoint") or {})
            .get("browseEndpointContextSupportedConfigs", {}).get("browseEndpointContextMusicConfig", {}).get("pageType", ""))


def _length_ms(text: str) -> int | None:
    m = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*", text or "")
    if not m:
        return None
    h, mins, s = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return ((h * 60 + mins) * 60 + s) * 1000


def _byline(runs: list[dict]) -> dict:
    """'Future • DS2 (Deluxe) • 2:29' -> artists, album, length. Uses link types when present."""
    artists = [r["text"] for r in runs if _page_type(r) == "MUSIC_PAGE_TYPE_ARTIST" and r.get("text")]
    album = next((r["text"] for r in runs if _page_type(r) == "MUSIC_PAGE_TYPE_ALBUM"), None)
    parts = [p.strip() for p in "".join(r.get("text", "") for r in runs).split("•")]
    if not artists and parts and parts[0]:
        artists = [parts[0]]
    length = next((_length_ms(p) for p in reversed(parts) if _length_ms(p)), None)
    return {"artists": ", ".join(artists), "album": album, "duration_ms": length}


def _explicit(node) -> bool:
    for badges in _walk(node, "badges"):
        for b in badges if isinstance(badges, list) else []:
            if isinstance(b, dict) and ((b.get("musicInlineBadgeRenderer") or {}).get("icon") or {}).get("iconType") == EXPLICIT:
                return True
    return False


def parse_video(data: dict, vid: str) -> dict | None:
    """The `next` response for one video -> {video_id, title, artists, album, duration_ms, explicit, official}."""
    panel = next((p for p in _walk(data, "playlistPanelVideoRenderer") if isinstance(p, dict) and p.get("videoId") == vid), None)
    if not panel:
        return None
    info = _byline(_runs(panel.get("longBylineText")))
    length = _length_ms(_text(panel.get("lengthText")))
    return {
        "video_id": vid,
        "title": _text(panel.get("title")),
        "artists": info["artists"],
        "album": info["album"],
        "duration_ms": length or info["duration_ms"],
        "explicit": _explicit({"badges": panel.get("badges") or []}),
        "official": bool(info["album"]),   # a YouTube Music album track, not a user upload
    }


def parse_search(data: dict) -> list[dict]:
    """The songs-search response -> [{video_id, title, artists, album, duration_ms, explicit}]."""
    out = []
    for item in _walk(data, "musicResponsiveListItemRenderer"):
        if not isinstance(item, dict):
            continue
        cols = [c.get("musicResponsiveListItemFlexColumnRenderer", {}).get("text") for c in item.get("flexColumns") or [] if isinstance(c, dict)]
        vid = (item.get("playlistItemData") or {}).get("videoId") or next(_walk(cols[:1], "videoId"), None)
        if not vid or len(cols) < 2:
            continue
        info = _byline(_runs(cols[1]))
        out.append({"video_id": vid, "title": _text(cols[0]), **info, "explicit": _explicit({"badges": item.get("badges") or []})})
    return out


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=20, headers=HEADERS)
        self._last = 0.0
        self._lock = threading.Lock()

    def post(self, endpoint: str, body: dict) -> dict:
        with self._lock:
            wait = GAP_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.post(f"{BASE}/{endpoint}?prettyPrint=false", json={"context": CONTEXT, **body})
            except httpx.HTTPError as e:
                raise YTMError(str(e))
        if r.status_code == 429 or r.status_code >= 500:
            raise YTMError(f"YouTube Music answered {r.status_code}")
        if r.status_code >= 400:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def video(self, vid: str) -> dict | None:
        return parse_video(self.post("next", {"videoId": vid, "isAudioOnly": True}), vid)

    def search_songs(self, query: str) -> list[dict]:
        return parse_search(self.post("search", {"query": query, "params": SONGS_FILTER}))


_client: Client | None = None
_search_cache: dict[str, tuple[float, list[dict]]] = {}


def client() -> Client:
    global _client
    _client = _client or Client()
    return _client


_interactive: Client | None = None


def interactive_client() -> Client:
    """A separate client for things you're waiting on (Find, Check now), so they never queue behind the background checker."""
    global _interactive
    _interactive = _interactive or Client()
    return _interactive


def search_songs(query: str, cl: Client | None = None) -> list[dict]:
    key = db.normalize(query)
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < SEARCH_CACHE_S:
        return hit[1]
    rows = (cl or client()).search_songs(query)
    _search_cache[key] = (time.time(), rows)
    return rows


# ---------------------------------------------------------------- matching

def _artist_set(text: str) -> set[str]:
    parts = re.split(r",|&| x | feat\.? | ft\.? | with ", (text or "").lower())
    return {db.normalize(p) for p in parts if db.normalize(p)}


def same_song(a: dict, b: dict, length_ms: int | None) -> bool:
    """Is search row `b` the same song as `a` (title + artist + length)?"""
    if not cutting.clean_title(a.get("title")) or cutting.clean_title(a.get("title")) != cutting.clean_title(b.get("title")):
        return False
    if length_ms is None or b.get("duration_ms") is None:
        return False
    diff = abs(b["duration_ms"] - length_ms)
    if diff > LENGTH_TOLERANCE_MS:
        return False
    shared = _artist_set(a.get("artists")) & _artist_set(b.get("artists"))
    return bool(shared) or diff <= CLOSE_LENGTH_MS


def check(track: dict, cl: Client | None = None) -> dict:
    """-> {result, video_id, twin?, official, checked_at, v}. result: explicit | clean | unofficial | no_explicit | unknown.
    Raises YTMError when YouTube Music is unreachable (the caller tries again later)."""
    cl = cl or client()
    now = time.time()
    vid = video_id(track.get("source_url"))
    if not vid:
        return {"result": "unknown", "reason": "not_youtube", "checked_at": now, "v": RULES}
    me = cl.video(vid)
    if not me:
        return {"result": "unknown", "reason": "not_found", "video_id": vid, "checked_at": now, "v": RULES}
    base = {"video_id": vid, "official": me["official"], "checked_at": now, "v": RULES}
    if me["explicit"]:
        return {"result": "explicit", **base}
    length = me["duration_ms"] or track.get("file_duration_ms") or track.get("duration_ms")
    title = track.get("title") or me["title"]
    artists = track.get("artists") or me["artists"]
    rows = search_songs(find_query({"title": title, "artists": artists}), cl)
    want = {"title": title, "artists": artists}
    twin = next((r for r in rows if r["explicit"] and r["video_id"] != vid and same_song(want, r, length)), None)
    if not twin and me["title"] and db.normalize(me["title"]) != db.normalize(title):
        want = {"title": me["title"], "artists": me["artists"] or artists}
        twin = next((r for r in rows if r["explicit"] and r["video_id"] != vid and same_song(want, r, length)), None)
    twin_info = {k: twin[k] for k in ("video_id", "title", "artists", "album", "duration_ms")} if twin else None
    if not me["official"]:
        # A user upload or music video: YouTube Music can't say which version it is, so never flag it.
        # If an official explicit copy exists, the song card offers it.
        return {"result": "unofficial", **base, **({"twin": twin_info} if twin_info else {})}
    if twin:
        # An official album track without the E badge, and an E copy of the same song exists: the clean edit.
        return {"result": "clean", **base, "twin": twin_info}
    return {"result": "no_explicit", **base}


def find_query(track: dict) -> str:
    """The YouTube Music songs search for a track (shared by the check, Find and search-ahead, so one cached answer serves all three)."""
    first_artist = (track.get("artists") or "").split(",")[0].strip()
    return f"{first_artist} {cutting.clean_title(track.get('title')) or track.get('title') or ''}".strip()


def annotate_results(track: dict, results: list[dict], cl: Client | None = None) -> list[dict]:
    """Find results: mark the ones that are the clean copy of a song YouTube Music has as explicit.
    One songs search; failures leave the results unmarked."""
    try:
        rows = search_songs(find_query(track), cl)
    except YTMError as e:
        log.info("Couldn't mark clean versions in Find: %s", e)
        return results
    by_id = {r["video_id"]: r for r in rows}
    out = []
    for res in results:
        mine = by_id.get(video_id(res.get("url")))
        twin = None
        if mine and not mine["explicit"]:
            twin = next((r for r in rows if r["explicit"] and r["video_id"] != mine["video_id"] and same_song(mine, r, mine["duration_ms"])), None)
        out.append({**res, "clean_of": ({k: twin[k] for k in ("video_id", "title", "album", "duration_ms")} if twin else None)})
    return out
