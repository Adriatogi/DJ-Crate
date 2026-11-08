"""Genre suggestions from MusicBrainz, looked up slowly in the background.

These are only ever *suggestions*: they show up as faint chips in a song's Genre
cell and nothing is applied until you click one. Songs that already have a
Rekordbox or MP3 genre are skipped (those are shown as suggestions already).

MusicBrainz asks for at most one request per second and a descriptive
User-Agent, so the client waits between calls and backs off for a few minutes
whenever the service pushes back.
"""

import json
import logging
import re
import threading
import time

import httpx

from . import __version__, db

log = logging.getLogger("djcrate.genre_lookup")

MB = "https://musicbrainz.org/ws/2"
USER_AGENT = f"DJCrate/{__version__} ( https://github.com/Adriatogi/DJ-Crate )"
GAP_S = 1.1              # between any two requests
PAUSE_S = 300            # back off this long when MusicBrainz is busy or unreachable
IDLE_S = 15              # how often to check for new songs when there is nothing to do
REFRESH_DAYS = 90        # look a song up again after this long
ARTIST_CACHE_DAYS = 60
MAX_ITEMS = 8
MIN_SCORE = 95           # for the artist+title search used when a song has no ISRC

SHOUTY = {"edm", "uk", "us", "idm", "dnb", "rnb", "ebm", "r&b"}


class MBError(Exception):
    """The service is busy or unreachable — try again later."""


def display_name(name: str) -> str:
    """'hip hop' -> 'Hip Hop', 'r&b' -> 'R&B'."""
    return " ".join(w.upper() if w in SHOUTY else w[:1].upper() + w[1:] for w in (name or "").strip().split())


class Client:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._last = 0.0

    def get(self, path: str, **params):
        wait = GAP_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            r = self.http.get(MB + path, params={**params, "fmt": "json"})
        except httpx.HTTPError as e:
            raise MBError(str(e))
        if r.status_code in (429, 503) or r.status_code >= 500:
            raise MBError(f"MusicBrainz answered {r.status_code}")
        if r.status_code >= 400:
            return None  # not found / bad request for this song: nothing to learn
        return r.json()


# ---------------------------------------------------------------- MusicBrainz's own genre list

def fetch_genre_list() -> list[str]:
    """All of MusicBrainz's genre names (about 2,200, flat A–Z, no hierarchy)."""
    try:
        r = httpx.get(MB + "/genre/all", params={"fmt": "txt"}, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise MBError(str(e))
    return [line.strip() for line in r.text.splitlines() if line.strip()]


def genre_list(conn) -> tuple[list[str], float | None, str | None]:
    """-> (names, fetched_at, error). Cached in the library; refreshed after a month. Works offline once cached."""
    cached = db.get_setting(conn, "mb_genre_list")
    at = float(db.get_setting(conn, "mb_genre_list_at", "0") or 0)
    names = json.loads(cached) if cached else []
    if names and time.time() - at < 30 * 86400:
        return names, at, None
    try:
        fresh = fetch_genre_list()
    except MBError as e:
        return names, (at or None), (None if names else f"Couldn't reach MusicBrainz ({e}).")
    db.set_setting(conn, "mb_genre_list", json.dumps(fresh))
    db.set_setting(conn, "mb_genre_list_at", str(time.time()))
    return fresh, time.time(), None


# ---------------------------------------------------------------- one song

def _lucene(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _recording_id(client: Client, track: dict) -> str | None:
    isrc = (track.get("isrc") or "").strip().upper()
    if isrc:
        data = client.get(f"/isrc/{isrc}")
        recs = (data or {}).get("recordings") or []
        if recs:
            return recs[0]["id"]
        return None  # the ISRC is exact; a text search would only guess
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", track.get("title") or "").strip()
    artist = (track.get("artists") or "").split(",")[0].strip()
    if not title or not artist:
        return None
    data = client.get("/recording", query=f'recording:"{_lucene(title)}" AND artist:"{_lucene(artist)}"', limit=3)
    for r in (data or {}).get("recordings") or []:
        if r.get("score", 0) >= MIN_SCORE:
            return r["id"]
    return None


def _artist_genres(client: Client, conn, mbid: str) -> list[str]:
    row = conn.execute("SELECT genres_json, fetched_at FROM mb_artist_genres WHERE mbid = ?", (mbid,)).fetchone()
    if row and time.time() - row["fetched_at"] < ARTIST_CACHE_DAYS * 86400:
        return json.loads(row["genres_json"])
    data = client.get(f"/artist/{mbid}", inc="genres") or {}
    names = [g["name"] for g in sorted(data.get("genres") or [], key=lambda g: -g.get("count", 0)) if g.get("name")]
    conn.execute(
        "INSERT INTO mb_artist_genres (mbid, genres_json, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(mbid) DO UPDATE SET genres_json = excluded.genres_json, fetched_at = excluded.fetched_at",
        (mbid, json.dumps(names), time.time()))
    return names


def lookup(client: Client, conn, track: dict) -> tuple[str, list[dict]]:
    """-> (status, items). status: found | none | not_in_musicbrainz.
    items: [{name, level: 'song'|'artist'}], the song's own genres first."""
    rec = _recording_id(client, track)
    if not rec:
        return "not_in_musicbrainz", []
    data = client.get(f"/recording/{rec}", inc="genres+artist-credits") or {}
    items, seen = [], set()

    def add(name, level):
        key = name.strip().lower()
        if key and key not in seen and len(items) < MAX_ITEMS:
            seen.add(key)
            items.append({"name": display_name(name), "level": level})

    for g in sorted(data.get("genres") or [], key=lambda g: -g.get("count", 0)):
        add(g.get("name", ""), "song")
    for credit in (data.get("artist-credit") or [])[:2]:
        mbid = (credit.get("artist") or {}).get("id")
        if mbid:
            for name in _artist_genres(client, conn, mbid):
                add(name, "artist")
    return ("found" if items else "none"), items


# ---------------------------------------------------------------- the background worker

# Songs worth looking up: no rack genre, no genre from Rekordbox/MP3 to go on, and not looked up recently.
ELIGIBLE = """
FROM tracks t
LEFT JOIN genre_suggest gs ON gs.track_id = t.id
WHERE NOT EXISTS (SELECT 1 FROM track_genres g WHERE g.track_id = t.id)
  AND coalesce(t.rekordbox_genre, '') = '' AND coalesce(t.file_genre, '') = ''
  AND (gs.track_id IS NULL OR gs.fetched_at < ?)
"""


def status(conn, finder=None) -> dict:
    stale = time.time() - REFRESH_DAYS * 86400
    return {
        "on": db.get_setting(conn, "genre_lookup_on", "1") == "1",
        "pending": conn.execute("SELECT count(*) " + ELIGIBLE, (stale,)).fetchone()[0],
        "looked_up": conn.execute("SELECT count(*) FROM genre_suggest").fetchone()[0],
        "found": conn.execute("SELECT count(*) FROM genre_suggest WHERE status = 'found'").fetchone()[0],
        "paused_until": finder.paused_until if finder and finder.paused else None,
    }


class Finder:
    def __init__(self, state, client_factory=Client):
        self.state = state
        self.client_factory = client_factory
        self.client = None
        self.paused_until = 0.0
        self.thread = None

    @property
    def paused(self) -> bool:
        return time.time() < self.paused_until

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="genre-lookup", daemon=True)
        self.thread.start()

    def pause(self):
        self.paused_until = time.time() + PAUSE_S
        log.warning("Genre lookups paused for %ss", PAUSE_S)

    def step(self) -> bool:
        """Look up one song. True if it did (or tried) work, False if idle."""
        if self.state.status != "ok" or self.state.read_only or self.paused:
            return False
        conn = self.state.conn()
        try:
            if db.get_setting(conn, "genre_lookup_on", "1") != "1":
                return False
            row = conn.execute("SELECT t.* " + ELIGIBLE + " ORDER BY t.id DESC LIMIT 1", (time.time() - REFRESH_DAYS * 86400,)).fetchone()
            if not row:
                return False
            self.client = self.client or self.client_factory()
            try:
                status_, items = lookup(self.client, conn, dict(row))
            except MBError as e:
                log.info("MusicBrainz lookup deferred: %s", e)
                self.pause()
                return False
            with db.tx(conn):
                conn.execute(
                    "INSERT INTO genre_suggest (track_id, items_json, status, fetched_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(track_id) DO UPDATE SET items_json = excluded.items_json, status = excluded.status, "
                    "fetched_at = excluded.fetched_at",
                    (row["id"], json.dumps(items), status_, time.time()))
            log.info("Genre lookup: %s – %s → %s", row["artists"], row["title"], status_)
            return True
        finally:
            conn.close()

    def _loop(self):
        while True:
            try:
                if self.step():
                    continue  # the client itself spaces requests out
            except Exception:
                log.exception("Genre lookup error")
            time.sleep(IDLE_S)
