"""Genius: what a song samples (and what sampled it), for the Cutting tab.

WhoSampled has no public API, so suggestions come from Genius' official API:
every song there has `song_relationships` (samples, sampled_in, interpolates,
interpolated_by). Each person makes their own free API client and pastes its
access token in Settings; it's kept in the macOS Keychain per library, like the
Spotify login.

Nothing is added on its own: the UI shows suggestions and you tick the ones to
keep. Songs in your library are linked; the rest are saved as text samples.
"""

import datetime as dt
import json
import logging
import sqlite3

import httpx
import keyring
import keyring.errors

from . import APP_NAME, cutting, db
from .errors import AppError

log = logging.getLogger("djcrate.genius")

API = "https://api.genius.com"
CACHE_DAYS = 30
ALTERNATIVES = 8
# Genius relationship type → how we store it. "Sampled in" ones point the other way.
RELATIONS = {
    "samples": ("sample", "out"),
    "interpolates": ("interpolation", "out"),
    "sampled_in": ("sample", "in"),
    "interpolated_by": ("interpolation", "in"),
}


class GeniusError(AppError):
    pass


# ---------------------------------------------------------------- token

_configured: dict[str | None, bool] = {}


def _key(owner: str | None) -> str:
    return f"genius:{owner}" if owner else "genius"


def load_token(owner: str | None) -> str | None:
    try:
        return keyring.get_password(APP_NAME, _key(owner))
    except keyring.errors.KeyringError:
        log.exception("Keychain read failed")
        return None


def save_token(owner: str | None, token: str) -> None:
    keyring.set_password(APP_NAME, _key(owner), token)
    _configured[owner] = True


def clear_token(owner: str | None) -> None:
    _configured[owner] = False
    try:
        keyring.delete_password(APP_NAME, _key(owner))
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError:
        log.exception("Keychain delete failed")


def configured(owner: str | None) -> bool:
    if owner not in _configured:
        _configured[owner] = bool(load_token(owner))
    return _configured[owner]


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, token: str | None):
        if not token:
            raise GeniusError("genius_not_set_up", "Connect Genius in Settings first (it's free).")
        self.token = token
        self.http = httpx.Client(timeout=15)

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = self.http.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError:
            raise GeniusError("offline", "Can't reach Genius — check your internet connection.", 503)
        if r.status_code == 401:
            raise GeniusError("genius_auth", "Genius rejected your access token — check it in Settings.")
        if r.status_code == 429:
            raise GeniusError("genius_rate_limited", "Genius is getting too many requests from you — wait a minute and try again.", 429)
        if r.status_code == 404:
            raise GeniusError("not_found", "Genius doesn't have that song anymore.", 404)
        if r.status_code != 200:
            raise GeniusError("genius_down", f"Genius had a problem ({r.status_code}). Try again shortly.", 502)
        try:
            return r.json()["response"]
        except (ValueError, KeyError):
            raise GeniusError("genius_down", "Genius sent back something unexpected. Try again shortly.", 502)


def check_token(token: str) -> None:
    with Client(token) as c:
        c.get("/search", {"q": "test"})


# ---------------------------------------------------------------- lookups

def _year(s: dict):
    return (s.get("release_date_components") or {}).get("year")


def _brief(s: dict) -> dict:
    return {
        "id": s.get("id"), "title": s.get("title") or "",
        "artist": s.get("artist_names") or (s.get("primary_artist") or {}).get("name") or "",
        "primary_artist": (s.get("primary_artist") or {}).get("name") or "",
        "url": s.get("url") or "", "year": _year(s), "cover": s.get("song_art_image_thumbnail_url") or "",
    }


def find_song(client: Client, track: dict) -> dict:
    """Best Genius match for a library song, plus a few alternatives to pick from."""
    first_artist = (track.get("artists") or "").split(",")[0].strip()
    title = track.get("title") or ""
    for pattern in (cutting._FEAT, cutting._VERSION, cutting._DASH_VERSION):
        title = pattern.sub("", title)
    hits = client.get("/search", {"q": f"{title} {first_artist}".strip()}).get("hits", [])
    songs = [_brief(h["result"]) for h in hits if h.get("type") == "song" and h.get("result")]
    want = cutting.clean_title(track.get("title"))
    match = next((s for s in songs if cutting.clean_title(s["title"]) == want
                  and (cutting.artist_matches(track.get("artists"), s["primary_artist"])
                       or cutting.artist_matches(track.get("artists"), s["artist"]))), None)
    return {"match": match, "alternatives": songs[:ALTERNATIVES]}


def relationships(client: Client, genius_id: int) -> dict:
    song = client.get(f"/songs/{int(genius_id)}", {"text_format": "plain"}).get("song") or {}
    items = []
    for rel in song.get("song_relationships") or []:
        kind = rel.get("relationship_type") or rel.get("type")
        if kind not in RELATIONS:
            continue
        for s in rel.get("songs") or []:
            items.append({"kind": kind, **_brief(s)})
    return {"song": _brief(song), "items": items}


# ---------------------------------------------------------------- cache

def _cached(conn, track_id: int):
    row = conn.execute("SELECT * FROM genius_cache WHERE track_id = ?", (track_id,)).fetchone()
    if not row:
        return None
    try:
        age = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.datetime.fromisoformat(row["fetched_at"])
    except ValueError:
        return None
    return {"fresh": age < dt.timedelta(days=CACHE_DAYS), "found": json.loads(row["match_json"]),
            "song": json.loads(row["song_json"]) if row["song_json"] else None}


def _store(conn, track_id: int, found: dict, song: dict | None) -> None:
    try:
        with db.tx(conn):
            conn.execute(
                "INSERT INTO genius_cache (track_id, genius_id, match_json, song_json, fetched_at) VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(track_id) DO UPDATE SET genius_id = excluded.genius_id, match_json = excluded.match_json, "
                "song_json = excluded.song_json, fetched_at = excluded.fetched_at",
                (track_id, (found.get("match") or {}).get("id"), json.dumps(found), json.dumps(song) if song else None))
    except sqlite3.OperationalError:
        log.warning("Couldn't cache Genius results (read-only library?)")


# ---------------------------------------------------------------- suggestions

def _already(conn, track_id: int, item: dict, lib_id: int | None) -> bool:
    label = f"{item['title']} — {item['artist']}"
    if RELATIONS[item["kind"]][1] == "in":
        if lib_id and conn.execute(
                "SELECT 1 FROM samples WHERE track_id = ? AND sample_track_id = ?", (lib_id, track_id)).fetchone():
            return True
        return bool(conn.execute(
            "SELECT 1 FROM samples WHERE track_id = ? AND direction = 'in' AND sample_track_id IS NULL "
            "AND ((url <> '' AND url = ?) OR lower(sample_label) = lower(?))", (track_id, item["url"], label)).fetchone())
    return bool(conn.execute(
        "SELECT 1 FROM samples WHERE track_id = ? AND ((? IS NOT NULL AND sample_track_id = ?) "
        "OR (sample_track_id IS NULL AND direction = 'out' AND ((url <> '' AND url = ?) OR lower(sample_label) = lower(?))))",
        (track_id, lib_id, lib_id, item["url"], label)).fetchone())


def suggestions(conn, client_factory, track_id: int, *, refresh: bool = False, genius_id: int | None = None) -> dict:
    """`client_factory()` is only called when Genius actually has to be asked."""
    track = db.get_track(conn, track_id)
    if not track:
        raise AppError("not_found", "That song isn't in your library anymore.", 404)
    cache = _cached(conn, track_id)
    found, song = (cache["found"], cache["song"]) if cache else (None, None)
    if genius_id:
        with client_factory() as c:
            song = relationships(c, genius_id)
        found = {"match": song["song"], "alternatives": (found or {}).get("alternatives", [])}
        _store(conn, track_id, found, song)
    elif refresh or not cache or not cache["fresh"]:
        with client_factory() as c:
            found = find_song(c, track)
            song = relationships(c, found["match"]["id"]) if found["match"] else None
        _store(conn, track_id, found, song)

    out = {"genius": (song or {}).get("song") or found.get("match"), "alternatives": found.get("alternatives", []), "items": []}
    if not song:
        return out
    index = cutting.LibraryIndex(conn)
    lib_ids = {}
    for it in song["items"]:
        lib = index.find(it["title"], it["primary_artist"]) or index.find(it["title"], it["artist"])
        if lib == track_id:
            lib = None
        lib_ids[id(it)] = lib
    info = cutting.briefs(conn, [v for v in lib_ids.values() if v])
    for it in song["items"]:
        lib = lib_ids[id(it)]
        out["items"].append({**it, "library_song": info.get(lib), "already_added": _already(conn, track_id, it, lib)})
    return out


def apply(conn, track_id: int, items: list[dict]) -> int:
    """Save ticked suggestions. Songs in the library are linked, the rest become text."""
    if not db.get_track(conn, track_id):
        raise AppError("not_found", "That song isn't in your library anymore.", 404)
    added = 0
    for it in items:
        kind = it.get("kind")
        if kind not in RELATIONS or not it.get("title"):
            continue
        relation, direction = RELATIONS[kind]
        lib_id = (it.get("library_song") or {}).get("id") or it.get("library_song_id")
        item = {"kind": kind, "title": it["title"], "artist": it.get("artist", ""), "url": it.get("url", "")}
        if _already(conn, track_id, item, lib_id):
            continue
        if lib_id:
            cutting.add_sample(conn, track_id, lib_id, "", url=item["url"], relation=relation, direction=direction)
        else:
            cutting.add_sample(conn, track_id, None, "", label=f"{item['title']} — {item['artist']}".strip(" —"),
                               url=item["url"], relation=relation, direction=direction)
        added += 1
    return added
