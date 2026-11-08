import httpx
import pytest

from djcrate import cutting, db, genius


def mk(client, title, artists="A"):
    return client.post("/api/tracks", json={"title": title, "artists": artists}).json()["track"]["id"]


def gsong(id, title, artist, **kw):
    return {"id": id, "title": title, "artist_names": artist, "primary_artist": {"name": artist.split(" & ")[0]},
            "url": f"https://genius.com/{id}", "release_date_components": {"year": kw.get("year", 1999)},
            "song_relationships": kw.get("rels", [])}


STRONGER = gsong(525, "Stronger", "Kanye West", rels=[
    {"relationship_type": "samples", "songs": [gsong(1, "Harder, Better, Faster, Stronger", "Daft Punk"),
                                               gsong(2, "Cold Sweat", "James Brown")]},
    {"relationship_type": "sampled_in", "songs": [gsong(3, "Remix Thing", "DJ X"), gsong(4, "Not Mine", "Nobody")]},
    {"relationship_type": "interpolates", "songs": [gsong(5, "Some Hook", "Old Group")]},
    {"relationship_type": "cover_of", "songs": [gsong(6, "Ignored", "Ignored")]},
])


@pytest.fixture
def fake_genius(monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.headers.get("authorization") != "Bearer good":
            return httpx.Response(401, json={})
        if req.url.path == "/search":
            return httpx.Response(200, json={"response": {"hits": [
                {"type": "song", "result": gsong(9, "Stronger", "Some Cover Band")},
                {"type": "song", "result": gsong(525, "Stronger", "Kanye West")},
            ]}})
        if req.url.path == "/songs/525":
            return httpx.Response(200, json={"response": {"song": STRONGER}})
        if req.url.path == "/songs/9":
            return httpx.Response(200, json={"response": {"song": gsong(9, "Stronger", "Some Cover Band")}})
        return httpx.Response(404, json={})

    real = httpx.Client
    monkeypatch.setattr(genius.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return calls


def test_title_and_artist_matching():
    assert cutting.clean_title("Stronger - Remastered 2011") == "stronger"
    assert cutting.clean_title("Song (feat. Someone)") == "song"
    assert cutting.clean_title("Song (Remix)") == "song remix"
    assert cutting.artist_matches("Kanye West, Daft Punk", "Kanye West")
    assert cutting.artist_matches("Simon, Garfunkel", "Simon & Garfunkel")
    assert not cutting.artist_matches("Kanye West", "Daft Punk")


def test_token_saved_per_library_and_checked(client, fake_genius):
    assert client.get("/api/status").json()["genius"]["configured"] is False
    r = client.put("/api/genius/token", json={"token": "bad"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "genius_auth"
    assert client.put("/api/genius/token", json={"token": "good"}).status_code == 200
    assert client.get("/api/status").json()["genius"]["configured"] is True
    client.delete("/api/genius/token")
    assert client.get("/api/status").json()["genius"]["configured"] is False


def test_suggestions_link_library_songs_and_cache(client, fake_genius):
    me = mk(client, "Stronger - Album Version", "Kanye West")
    daft = mk(client, "Harder, Better, Faster, Stronger", "Daft Punk")
    remixer = mk(client, "Remix Thing", "DJ X")
    r = client.get(f"/api/tracks/{me}/genius")
    assert r.json()["error"]["code"] == "genius_not_set_up"
    client.put("/api/genius/token", json={"token": "good"})
    s = client.get(f"/api/tracks/{me}/genius").json()
    assert s["genius"]["id"] == 525  # skips the cover band
    items = {i["title"]: i for i in s["items"]}
    assert "Ignored" not in items
    assert items["Harder, Better, Faster, Stronger"]["library_song"]["id"] == daft
    assert items["Cold Sweat"]["library_song"] is None
    assert items["Remix Thing"]["library_song"]["id"] == remixer
    assert items["Not Mine"]["library_song"] is None

    n = len(fake_genius)
    client.get(f"/api/tracks/{me}/genius")
    assert len(fake_genius) == n  # cached
    client.get(f"/api/tracks/{me}/genius?refresh=1")
    assert len(fake_genius) == n + 2

    added = client.post(f"/api/tracks/{me}/genius/apply", json={"items": s["items"]}).json()
    assert added["added"] == 5  # daft (linked), cold sweat + some hook (text), remix thing (other way), not mine (text)
    by = {x["sample_label"]: x for x in added["samples"]}
    assert by["Harder, Better, Faster, Stronger — Daft Punk"]["sample_track_id"] == daft
    assert by["Cold Sweat — James Brown"]["sample_track_id"] is None and by["Cold Sweat — James Brown"]["url"]
    assert by["Some Hook — Old Group"]["relation"] == "interpolation"
    remix_board = client.get(f"/api/cutting/board/{remixer}").json()["center"]
    assert remix_board["samples"][0]["sample_track_id"] == me
    by_labels = {(x["song"] or {}).get("title") or x["sample_label"] for x in added["sampled_by"]}
    assert by_labels == {"Remix Thing", "Not Mine — Nobody"}
    # Second time round nothing is added twice and everything shows as already added.
    again = client.get(f"/api/tracks/{me}/genius").json()
    assert all(i["already_added"] for i in again["items"])
    assert client.post(f"/api/tracks/{me}/genius/apply", json={"items": s["items"]}).json()["added"] == 0


def test_wrong_song_repick(client, fake_genius):
    me = mk(client, "Stronger", "Kanye West")
    client.put("/api/genius/token", json={"token": "good"})
    client.get(f"/api/tracks/{me}/genius")
    s = client.get(f"/api/tracks/{me}/genius?genius_id=9").json()
    assert s["genius"]["id"] == 9 and s["items"] == [] and len(s["alternatives"]) == 2
    assert client.get(f"/api/tracks/{me}/genius").json()["genius"]["id"] == 9  # remembered


def test_link_text_sample_later(client):
    me = mk(client, "Track", "Me")
    client.post(f"/api/tracks/{me}/samples", json={"text": "drum break"})
    from djcrate.state import state
    conn = state.conn()
    cutting.add_sample(conn, me, None, "", label="Cold Sweat — James Brown", url="https://genius.com/2")
    conn.close()
    samples = client.get(f"/api/tracks/{me}/samples").json()["samples"]
    cold = next(x for x in samples if x["sample_label"])
    assert cold["match"] is None
    js = mk(client, "Cold Sweat - Pt. 1 - Remastered", "James Brown")
    js2 = mk(client, "Cold Sweat", "James Brown, The Famous Flames")
    cold = next(x for x in client.get(f"/api/tracks/{me}/samples").json()["samples"] if x["sample_label"])
    assert cold["match"]["id"] == js2  # "Pt. 1" is a different title
    client.patch(f"/api/samples/{cold['id']}", json={"sample_track_id": js})
    cold = next(x for x in client.get(f"/api/tracks/{me}/samples").json()["samples"] if x["id"] == cold["id"])
    assert cold["sample_track_id"] == js and cold["sample_label"].startswith("Cold Sweat - Pt. 1")
    client.patch(f"/api/samples/{cold['id']}", json={"unlink": True})
    cold = next(x for x in client.get(f"/api/tracks/{me}/samples").json()["samples"] if x["id"] == cold["id"])
    assert cold["sample_track_id"] is None and cold["sample_label"]
    assert client.patch(f"/api/samples/{cold['id']}", json={"sample_track_id": me}).status_code == 400


def test_v4_to_v5_keeps_samples(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    v5 = db.SCHEMA_VERSION
    db.SCHEMA_VERSION = 4
    try:
        db.migrate(conn)
    finally:
        db.SCHEMA_VERSION = v5
    conn.execute("INSERT INTO tracks (title, artists) VALUES ('A', 'B')")
    conn.execute("INSERT INTO samples (track_id, text) VALUES (1, 'break')")
    db.migrate(conn)
    row = conn.execute("SELECT * FROM samples").fetchone()
    assert row["text"] == "break" and row["url"] == "" and row["relation"] == "sample"


def test_sampled_in_text_links_once_in_library(client):
    me = mk(client, "Classic", "Old Band")
    from djcrate.state import state
    conn = state.conn()
    cutting.add_sample(conn, me, None, "", label="New Hit — Rapper", direction="in")
    conn.close()
    side = client.get(f"/api/tracks/{me}/samples").json()
    assert side["samples"] == [] and side["sampled_by"][0]["sample_label"] == "New Hit — Rapper"
    assert client.get(f"/api/cutting/board/{me}").json()["samples"] == []  # text only, no board line
    hit = mk(client, "New Hit", "Rapper, Singer")
    row = client.get(f"/api/tracks/{me}/samples").json()["sampled_by"][0]
    assert row["match"]["id"] == hit
    client.patch(f"/api/samples/{row['id']}", json={"sample_track_id": hit})
    side = client.get(f"/api/tracks/{me}/samples").json()
    assert side["sampled_by"][0]["song"]["id"] == hit and side["sampled_by"][0]["track_id"] == hit
    assert client.get(f"/api/tracks/{hit}/samples").json()["samples"][0]["sample_track_id"] == me
    assert len(client.get(f"/api/cutting/board/{me}").json()["samples"]) == 1
