"""Spotify: PKCE login, token refresh, playlist / Liked Songs fetch, re-sync diff.

Each user registers their own Spotify developer app and pastes its Client ID
(no secret needed with PKCE). Tokens live in the macOS Keychain, not in the
library, so a copied library never carries your login.

Notes on the 2026 Web API: playlist contents come from /playlists/{id}/items
and each entry's track is under `item` (`track` is deprecated); contents are
only returned for playlists you own or collaborate on.
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
import keyring
import keyring.errors

from . import APP_NAME
from .errors import AppError

log = logging.getLogger("djcrate.spotify")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
LOGIN_TIMEOUT = 300
PAGE = 50

_pending: dict[str, dict] = {}
_lock = threading.Lock()
login_result: dict = {"state": "idle"}  # idle | waiting | ok | cancelled | error | timeout
progress: dict = {"text": ""}
_me_cache: dict = {}


class SpotifyError(AppError):
    pass


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}/callback"


# ---------------------------------------------------------------- tokens

def _key(client_id: str, owner: str | None) -> str:
    # Keyed per library too, so two people sharing a Mac (and maybe a Spotify
    # developer app) each stay logged in to their own account.
    return f"spotify:{client_id}:{owner}" if owner else f"spotify:{client_id}"


def load_tokens(client_id: str, owner: str | None = None) -> dict | None:
    if not client_id:
        return None
    try:
        raw = keyring.get_password(APP_NAME, _key(client_id, owner))
        if not raw and owner:
            # Logins saved before libraries had their own key: adopt them once.
            raw = keyring.get_password(APP_NAME, _key(client_id, None))
            if raw:
                keyring.set_password(APP_NAME, _key(client_id, owner), raw)
                _delete(_key(client_id, None))
    except keyring.errors.KeyringError:
        log.exception("Keychain read failed")
        return None
    return json.loads(raw) if raw else None


def save_tokens(client_id: str, tokens: dict, owner: str | None = None) -> None:
    keyring.set_password(APP_NAME, _key(client_id, owner), json.dumps(tokens))


def _delete(key: str) -> None:
    try:
        keyring.delete_password(APP_NAME, key)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError:
        log.exception("Keychain delete failed")


def clear_tokens(client_id: str, owner: str | None = None) -> None:
    _me_cache.clear()
    _delete(_key(client_id, owner))


def _store_token_response(client_id: str, data: dict, old: dict | None = None, owner: str | None = None) -> dict:
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or (old or {}).get("refresh_token"),
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
    }
    save_tokens(client_id, tokens, owner)
    return tokens


# ---------------------------------------------------------------- login (PKCE)

def start_login(client_id: str, port: int, owner: str | None = None) -> str:
    if not client_id:
        raise SpotifyError("no_client_id", "Add your Spotify Client ID in Settings first.")
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    with _lock:
        _pending.clear()
        _pending[state] = {"verifier": verifier, "client_id": client_id, "port": port, "owner": owner, "created": time.time()}
        login_result.clear()
        login_result.update(state="waiting", started=time.time())
    return AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "scope": SCOPES,
        "redirect_uri": redirect_uri(port),
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })


def check_login_timeout() -> None:
    with _lock:
        if login_result.get("state") == "waiting" and time.time() - login_result.get("started", 0) > LOGIN_TIMEOUT:
            _pending.clear()
            login_result.update(state="timeout", message="Login timed out. Try again.")


def cancel_login() -> None:
    with _lock:
        _pending.clear()
        login_result.clear()
        login_result.update(state="idle")


def finish_login(code: str | None, state: str | None, error: str | None) -> tuple[bool, str]:
    """Handle the browser redirect. Returns (ok, message for the browser tab)."""
    with _lock:
        pending = _pending.pop(state or "", None)
    if error:
        msg = "Login cancelled." if error == "access_denied" else f"Spotify said: {error}"
        login_result.update(state="cancelled" if error == "access_denied" else "error", message=msg)
        return False, msg
    if not pending or time.time() - pending["created"] > LOGIN_TIMEOUT:
        login_result.update(state="error", message="That login link expired. Click Log in again.")
        return False, "That login link expired. Go back to DJ Crate and click Log in again."
    try:
        r = httpx.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(pending["port"]),
            "client_id": pending["client_id"],
            "code_verifier": pending["verifier"],
        }, timeout=20)
    except httpx.HTTPError as e:
        login_result.update(state="error", message=f"Couldn't reach Spotify ({type(e).__name__}).")
        return False, "Couldn't reach Spotify. Check your connection and try again."
    if r.status_code != 200:
        body = _json(r)
        desc = body.get("error_description") or body.get("error") or r.text[:200]
        if "redirect" in desc.lower():
            msg = f"Redirect URI mismatch. In your Spotify dashboard add exactly: {redirect_uri(pending['port'])}"
        elif body.get("error") == "invalid_client":
            msg = "Spotify rejected the Client ID. Double-check it in Settings."
        else:
            msg = f"Spotify login failed: {desc}"
        login_result.update(state="error", message=msg)
        return False, msg
    _store_token_response(pending["client_id"], r.json(), owner=pending.get("owner"))
    _me_cache.clear()
    login_result.update(state="ok", message="Connected to Spotify.")
    return True, "Connected! You can close this tab and go back to DJ Crate."


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------- API client

class Client:
    def __init__(self, client_id: str, owner: str | None = None):
        if not client_id:
            raise SpotifyError("no_client_id", "Add your Spotify Client ID in Settings first.")
        self.client_id = client_id
        self.owner = owner
        self.tokens = load_tokens(client_id, owner)
        if not self.tokens:
            raise SpotifyError("not_logged_in", "Log in to Spotify first.", 401)
        self.http = httpx.Client(timeout=20)

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _refresh(self):
        try:
            r = self.http.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": self.tokens.get("refresh_token"),
                "client_id": self.client_id,
            })
        except httpx.HTTPError:
            raise SpotifyError("offline", "Can't reach Spotify — check your internet connection.", 503)
        if r.status_code in (400, 401):
            log.warning("Spotify refresh rejected: %s", r.text[:200])
            clear_tokens(self.client_id, self.owner)
            raise SpotifyError("revoked", "You've been logged out of Spotify. Log in again — your crates are safe.", 401)
        if r.status_code != 200:
            raise SpotifyError("spotify_down", f"Spotify had a problem ({r.status_code}). Try again shortly.", 502)
        self.tokens = _store_token_response(self.client_id, r.json(), self.tokens, self.owner)

    def get(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else API + path
        refreshed = False
        for attempt in range(6):
            if self.tokens["expires_at"] - 60 < time.time():
                self._refresh()
                refreshed = True
            try:
                r = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self.tokens['access_token']}"})
            except httpx.HTTPError:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise SpotifyError("offline", "Can't reach Spotify — check your internet connection.", 503)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and not refreshed:
                self._refresh()
                refreshed = True
                continue
            if r.status_code == 401:
                clear_tokens(self.client_id, self.owner)
                raise SpotifyError("revoked", "You've been logged out of Spotify. Log in again — your crates are safe.", 401)
            if r.status_code == 429:
                wait = min(int(r.headers.get("Retry-After", "5") or 5), 60)
                log.info("Spotify rate limit; waiting %ss", wait)
                progress["text"] = f"Spotify asked us to slow down — waiting {wait}s…"
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            err = _json(r).get("error")
            msg = err.get("message", "") if isinstance(err, dict) else str(err or "")
            if r.status_code == 403 and "registered" in msg.lower():
                raise SpotifyError("not_registered",
                                   "Your Spotify account isn't on this developer app's user list. Add your Spotify email under "
                                   "User Management in your Spotify developer dashboard.", 403)
            raise SpotifyError(f"http_{r.status_code}", msg or f"Spotify error {r.status_code}", r.status_code)
        raise SpotifyError("rate_limited", "Spotify is rate limiting us. Try again in a minute.", 429)

    def paged(self, path: str, label: str):
        url, params, seen = path, {"limit": PAGE}, 0
        while url:
            data = self.get(url, params)
            items = data.get("items") or []
            seen += len(items)
            total = data.get("total")
            progress["text"] = f"Fetching {label} {seen}/{total}…" if total else f"Fetching {label} {seen}…"
            yield from items
            url, params = data.get("next"), None

    def me(self) -> dict:
        if not _me_cache:
            data = self.get("/me")
            _me_cache.update(id=data.get("id"), name=data.get("display_name") or data.get("id"))
        return dict(_me_cache)


# ---------------------------------------------------------------- playlists

PLAYLIST_RE = re.compile(r"(?:open\.spotify\.com/(?:[a-z-]+/)?playlist/|spotify:playlist:)([A-Za-z0-9]{22})")


def parse_playlist_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if ref == "liked":
        return "liked"
    m = PLAYLIST_RE.search(ref)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", ref):
        return ref
    raise SpotifyError("bad_playlist_url", "That doesn't look like a Spotify playlist link. Copy it via Share → Copy link to playlist.")


def parse_track(t: dict) -> dict:
    album = t.get("album") or {}
    images = album.get("images") or []
    return {
        "spotify_id": t.get("id"),
        "isrc": (t.get("external_ids") or {}).get("isrc"),
        "title": t.get("name") or "",
        "artists": ", ".join(a.get("name", "") for a in t.get("artists") or [] if a.get("name")),
        "album": album.get("name") or "",
        "release_date": album.get("release_date") or "",
        "duration_ms": t.get("duration_ms"),
        "cover_url": images[0]["url"] if images else None,
        "spotify_url": (t.get("external_urls") or {}).get("spotify"),
    }


def _collect(entries) -> tuple[list[dict], list[dict]]:
    tracks, skipped = [], []
    for e in entries:
        obj = e.get("item") or e.get("track")
        if e.get("is_local") or (obj and obj.get("is_local")):
            name = (obj or {}).get("name") or "Local file"
            skipped.append({"title": name, "reason": "Local file on Spotify (not streamable)"})
            continue
        if not obj:
            skipped.append({"title": "Unknown", "reason": "Unavailable on Spotify"})
            continue
        if obj.get("type") and obj["type"] != "track":
            skipped.append({"title": obj.get("name") or "Episode", "reason": "Podcast episode"})
            continue
        if not obj.get("id"):
            skipped.append({"title": obj.get("name") or "Unknown", "reason": "Unavailable on Spotify"})
            continue
        tracks.append(parse_track(obj))
    return tracks, skipped


def fetch_playlist(client: Client, ref: str) -> dict:
    """Everything is fetched before anything is written, so a network failure
    halfway never leaves a half-imported crate."""
    pid = parse_playlist_ref(ref)
    progress["text"] = "Contacting Spotify…"
    try:
        if pid == "liked":
            tracks, skipped = _collect(client.paged("/me/tracks", "Liked Songs"))
            return {"playlist_id": "liked", "name": "Liked Songs", "tracks": tracks, "skipped": skipped}
        meta = client.get(f"/playlists/{pid}", {"fields": "name,owner(id,display_name),collaborative"})
        tracks, skipped = _collect(client.paged(f"/playlists/{pid}/items", "songs"))
        return {"playlist_id": pid, "name": meta.get("name") or "Spotify playlist", "tracks": tracks, "skipped": skipped}
    except SpotifyError as e:
        if e.code in ("http_403", "http_404"):
            if pid.startswith("37i9"):
                raise SpotifyError("editorial", "Spotify blocks its own editorial playlists for apps. Copy its songs into a "
                                   "playlist of your own and import that.", 403)
            raise SpotifyError("not_owned", "Spotify only lets apps read playlists you own or collaborate on. Add these songs "
                               "to a playlist of your own (select all → Add to playlist) and import that.", 403)
        raise
    finally:
        progress["text"] = ""


def my_playlists(client: Client) -> list[dict]:
    me = client.me()
    out = []
    try:
        for p in client.paged("/me/playlists", "playlists"):
            if not p:
                continue
            owner = (p.get("owner") or {}).get("id")
            count = (p.get("items") or p.get("tracks") or {}).get("total")
            images = p.get("images") or []
            out.append({
                "id": p["id"], "name": p.get("name") or "", "count": count,
                "image": images[-1]["url"] if images else None,
                "owner": (p.get("owner") or {}).get("display_name") or owner,
                "readable": owner == me.get("id") or bool(p.get("collaborative")),
            })
    finally:
        progress["text"] = ""
    return out


def plan_resync(current: dict[int, bool], fetched: list[int]) -> dict:
    """current: {track_id: removed_from_source}; fetched: track ids now on Spotify.
    Nothing is ever deleted: gone songs are only flagged."""
    fetched_set = set(fetched)
    add, seen = [], set()
    for tid in fetched:
        if tid not in current and tid not in seen:
            add.append(tid)
            seen.add(tid)
    removed = [tid for tid, was_removed in current.items() if tid not in fetched_set and not was_removed]
    restored = [tid for tid, was_removed in current.items() if tid in fetched_set and was_removed]
    return {"add": add, "removed": removed, "restored": restored}
