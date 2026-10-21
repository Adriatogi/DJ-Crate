import time

import httpx
import pytest

from djcrate import db, spotify

CID = "abc123"


@pytest.fixture
def fake_http(monkeypatch):
    """Route the Spotify client's HTTP through a handler we control."""
    calls = []
    handlers = {}

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        for (method, path), fn in handlers.items():
            if request.method == method and request.url.path == path:
                return fn(request)
        return httpx.Response(404, json={"error": {"status": 404, "message": "nope"}})

    real = httpx.Client
    monkeypatch.setattr(spotify.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))
    return handlers, calls


def tokens(expired=False):
    spotify.save_tokens(CID, {"access_token": "old", "refresh_token": "r1",
                              "expires_at": time.time() + (-10 if expired else 3600)})


def test_expired_token_refreshes_silently(fake_http):
    handlers, calls = fake_http
    tokens(expired=True)
    handlers[("POST", "/api/token")] = lambda r: httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    handlers[("GET", "/v1/me")] = lambda r: httpx.Response(200, json={"id": "me", "display_name": "Me"}) \
        if r.headers["Authorization"] == "Bearer new" else httpx.Response(401)
    with spotify.Client(CID) as c:
        assert c.get("/me")["id"] == "me"
    saved = spotify.load_tokens(CID)
    assert saved["access_token"] == "new" and saved["refresh_token"] == "r1"  # refresh token kept


def test_revoked_refresh_logs_out_cleanly(fake_http):
    handlers, _ = fake_http
    tokens(expired=True)
    handlers[("POST", "/api/token")] = lambda r: httpx.Response(400, json={"error": "invalid_grant"})
    with pytest.raises(spotify.SpotifyError) as e:
        with spotify.Client(CID) as c:
            c.get("/me")
    assert e.value.code == "revoked" and "crates are safe" in e.value.message
    assert spotify.load_tokens(CID) is None


def test_not_owned_and_editorial_playlists_explained(fake_http):
    handlers, _ = fake_http
    tokens()
    handlers[("GET", "/v1/playlists/aaaaaaaaaaaaaaaaaaaaaa")] = lambda r: httpx.Response(403, json={"error": {"message": "Forbidden"}})
    with spotify.Client(CID) as c:
        with pytest.raises(spotify.SpotifyError) as e:
            spotify.fetch_playlist(c, "aaaaaaaaaaaaaaaaaaaaaa")
        assert e.value.code == "not_owned"
        with pytest.raises(spotify.SpotifyError) as e:
            spotify.fetch_playlist(c, "37i9dQZF1DXcBWIGoYBM5M")
        assert e.value.code == "editorial"


def test_rate_limit_respects_retry_after(fake_http, monkeypatch):
    handlers, calls = fake_http
    tokens()
    slept = []
    monkeypatch.setattr(spotify.time, "sleep", lambda s: slept.append(s))
    responses = iter([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={"id": "me"})])
    handlers[("GET", "/v1/me")] = lambda r: next(responses)
    with spotify.Client(CID) as c:
        assert c.get("/me")["id"] == "me"
    assert slept == [3]


def test_fetch_playlist_items_skips_local_and_episodes(fake_http):
    handlers, _ = fake_http
    tokens()
    pid = "bbbbbbbbbbbbbbbbbbbbbb"
    track = {"id": "t1", "type": "track", "name": "Strobe", "duration_ms": 600000, "artists": [{"name": "deadmau5"}],
             "album": {"name": "For Lack", "release_date": "2009-09-22", "images": [{"url": "http://img"}]},
             "external_ids": {"isrc": "CA1"}, "external_urls": {"spotify": "http://sp"}}
    handlers[("GET", f"/v1/playlists/{pid}")] = lambda r: httpx.Response(200, json={"name": "My Mix"})
    handlers[("GET", f"/v1/playlists/{pid}/items")] = lambda r: httpx.Response(200, json={"total": 4, "next": None, "items": [
        {"item": track},
        {"track": {**track, "id": "t2", "name": "Old field name"}},  # deprecated `track` key still handled
        {"is_local": True, "item": {"name": "My bootleg", "is_local": True}},
        {"item": {"type": "episode", "id": "e1", "name": "Podcast"}},
    ]})
    with spotify.Client(CID) as c:
        data = spotify.fetch_playlist(c, f"https://open.spotify.com/playlist/{pid}")
    assert data["name"] == "My Mix"
    assert [t["title"] for t in data["tracks"]] == ["Strobe", "Old field name"]
    assert data["tracks"][0] == {"spotify_id": "t1", "isrc": "CA1", "title": "Strobe", "artists": "deadmau5",
                                 "album": "For Lack", "release_date": "2009-09-22", "duration_ms": 600000,
                                 "cover_url": "http://img", "spotify_url": "http://sp"}
    assert [s["reason"] for s in data["skipped"]] == ["Local file on Spotify (not streamable)", "Podcast episode"]


def test_import_and_resync_through_api(client, lib, monkeypatch):
    conn = lib.conn()
    db.set_setting(conn, "spotify_client_id", CID)
    tokens()
    songs = [{"spotify_id": f"s{i}", "title": f"Song {i}", "artists": "A", "duration_ms": 1000} for i in range(3)]
    payload = {"playlist_id": "cccccccccccccccccccccc", "name": "Gig", "tracks": songs, "skipped": [{"title": "x", "reason": "Local file"}]}
    monkeypatch.setattr(spotify, "fetch_playlist", lambda c, ref: payload)
    r = client.post("/api/spotify/import", json={"ref": "cccccccccccccccccccccc"}).json()
    assert r["imported"] == 3 and len(r["skipped"]) == 1
    crate = r["crate_id"]
    first = client.get(f"/api/tracks?view=crate:{crate}").json()["tracks"][0]
    client.patch(f"/api/tracks/{first['id']}", json={"notes": "keep me", "source_url": "https://youtu.be/x"})
    # Song 0 removed on Spotify, Song 3 added
    payload["tracks"] = songs[1:] + [{"spotify_id": "s3", "title": "Song 3", "artists": "A"}]
    r = client.post(f"/api/crates/{crate}/resync").json()
    assert (r["added"], r["removed"]) == (1, 1)
    tracks = client.get(f"/api/tracks?view=crate:{crate}").json()["tracks"]
    gone = [t for t in tracks if t["removed_from_source"]]
    assert len(tracks) == 4 and gone[0]["notes"] == "keep me" and gone[0]["source_url"] == "https://youtu.be/x"
    # Importing the same playlist again re-syncs instead of duplicating
    again = client.post("/api/spotify/import", json={"ref": "cccccccccccccccccccccc"}).json()
    assert again["already_imported"] and again["added"] == 0


def test_status_shows_disconnected_without_tokens(client, lib):
    conn = lib.conn()
    db.set_setting(conn, "spotify_client_id", "zzz999")
    s = client.get("/api/status").json()["spotify"]
    assert s["client_id_set"] and not s["connected"]
