import pytest
from mutagen.id3 import COMM, ID3

from djcrate import db, genres, rekordbox, tags
from djcrate.errors import AppError


def track(lib, title, artists="A", **cols):
    conn = lib.conn()
    try:
        tid, _ = db.upsert_track(conn, {"title": title, "artists": artists, "is_manual": 1})
        for k, v in cols.items():
            conn.execute(f"UPDATE tracks SET {k} = ? WHERE id = ?", (v, tid))
        return tid
    finally:
        conn.close()


def with_file(lib, make_mp3, tid, name):
    mp3 = make_mp3(lib.library / f"{name}.mp3")
    conn = lib.conn()
    conn.execute("UPDATE tracks SET file_rel_path = ?, download_status = 'done' WHERE id = ?", (lib.rel_path(mp3), tid))
    conn.close()
    return mp3


def mk(client, name, parent=None):
    return client.post("/api/genres", json={"name": name, "parent_id": parent}).json()["id"]


# ------------------------------------------------------------ names

def test_norm_and_split():
    assert genres.norm("  Hip-Hop ") == genres.norm("hip   hop") == "hip hop"
    assert genres.norm("Café") == "cafe"
    assert genres.split_names("Hip-Hop/Rap; Soul, hip hop") == ["Hip-Hop", "Rap", "Soul"]
    assert genres.split_names(None) == []


# ------------------------------------------------------------ the rack

def test_create_rejects_duplicates_and_slashes(client):
    house = mk(client, "House")
    r = client.post("/api/genres", json={"name": "house"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "genre_exists" and r.json()["error"]["existing_id"] == house
    assert client.post("/api/genres", json={"name": "R&B/Soul"}).status_code == 400
    assert client.post("/api/genres", json={"name": "  "}).status_code == 400


def test_aliases_resolve_and_block_duplicates(client, lib):
    hh = mk(client, "Hip Hop")
    assert client.post(f"/api/genres/{hh}/aliases", json={"name": "Hip-Hop/Rap"}).status_code == 400  # slash
    assert client.post(f"/api/genres/{hh}/aliases", json={"name": "rap"}).status_code == 200
    conn = lib.conn()
    assert genres.resolve(conn, "RAP") == hh and genres.resolve(conn, "hip-hop") == hh
    assert genres.resolve(conn, "polka") is None
    conn.close()
    house = mk(client, "House")
    assert client.post(f"/api/genres/{house}/aliases", json={"name": "Rap"}).status_code == 409
    assert client.post("/api/genres", json={"name": "rap"}).status_code == 409  # alias names are taken too


def test_rename_collision_and_old_name_stays_as_alias(client, lib):
    a, b = mk(client, "Deep House"), mk(client, "Tech House")
    r = client.patch(f"/api/genres/{b}", json={"name": "deep house"})
    assert r.status_code == 409 and r.json()["error"]["existing_id"] == a
    assert client.patch(f"/api/genres/{b}", json={"name": "Techno House"}).status_code == 200
    conn = lib.conn()
    assert genres.resolve(conn, "Tech House") == b
    conn.close()


def test_move_refuses_cycles_and_delete_moves_children_up(client):
    house = mk(client, "House")
    deep = mk(client, "Deep House", house)
    sub = mk(client, "Dub House", deep)
    assert client.patch(f"/api/genres/{house}", json={"move": True, "parent_id": sub}).status_code == 400
    assert client.delete(f"/api/genres/{deep}").status_code == 200
    tree = client.get("/api/genres").json()["tree"]
    assert [n["name"] for n in tree] == ["House"]
    assert [c["name"] for c in tree[0]["children"]] == ["Dub House"]


def test_merge_moves_songs_aliases_and_children(client, lib):
    a, b = mk(client, "Hip Hop"), mk(client, "Hiphop")
    child = mk(client, "Boom Bap", b)
    t1, t2 = track(lib, "One"), track(lib, "Two")
    client.put(f"/api/tracks/{t1}/genres", json={"genre_ids": [a, b]})
    client.put(f"/api/tracks/{t2}/genres", json={"genre_ids": [b]})
    assert client.post(f"/api/genres/{b}/merge", json={"into": a}).status_code == 200
    conn = lib.conn()
    assert genres.resolve(conn, "hiphop") == a  # old name is now an alias
    assert {r[0] for r in conn.execute("SELECT track_id FROM track_genres WHERE genre_id = ?", (a,))} == {t1, t2}
    assert conn.execute("SELECT parent_id FROM genres WHERE id = ?", (child,)).fetchone()[0] == a
    conn.close()
    assert client.post(f"/api/genres/{a}/merge", json={"into": child}).status_code == 400  # into its own sub-genre


def test_tree_counts_include_sub_genres(client, lib):
    house = mk(client, "House")
    deep = mk(client, "Deep House", house)
    t1, t2, t3 = track(lib, "One"), track(lib, "Two"), track(lib, "Three")
    client.put(f"/api/tracks/{t1}/genres", json={"genre_ids": [house]})
    client.put(f"/api/tracks/{t2}/genres", json={"genre_ids": [deep]})
    data = client.get("/api/genres").json()
    node = data["tree"][0]
    assert (node["own"], node["count"]) == (1, 2) and node["children"][0]["count"] == 1
    assert data["unsorted"] == 1  # t3


# ------------------------------------------------------------ songs + the MP3 tag

def test_tag_string_is_parent_first_without_repeats(client, lib):
    house = mk(client, "House")
    deep = mk(client, "Deep House", house)
    ama = mk(client, "Amapiano")
    conn = lib.conn()
    got = genres.for_tracks(conn, [])
    assert got == {}
    t = track(lib, "One")
    conn.close()
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": [deep, house, ama]})
    tr = client.get("/api/tracks?view=smart:all").json()["tracks"][0]
    assert tr["genre_tag"] == "House / Deep House / Amapiano"
    assert [g["name"] for g in tr["genres"]] == ["Deep House", "House", "Amapiano"]


def test_setting_genres_writes_and_clears_the_mp3_tag_but_not_the_comment(client, lib, make_mp3):
    t = track(lib, "Song")
    mp3 = with_file(lib, make_mp3, t, "Song")
    id3 = ID3(mp3)
    id3.add(COMM(encoding=3, lang="eng", desc="", text="mine"))
    id3.save(mp3, v2_version=3)
    house = mk(client, "House")
    deep = mk(client, "Deep House", house)
    r = client.put(f"/api/tracks/{t}/genres", json={"genre_ids": [deep]}).json()
    assert r["retagged"] == 1 and str(ID3(mp3)["TCON"].text[0]) == "House / Deep House"
    assert tags.read_tags(mp3)["genre"] == "House / Deep House"
    assert str(ID3(mp3).getall("COMM")[0].text[0]) == "mine"
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": []})
    assert "TCON" not in ID3(mp3)


def test_file_genre_is_left_alone_when_the_song_has_no_rack_genres(client, lib, make_mp3):
    t = track(lib, "Song")
    mp3 = with_file(lib, make_mp3, t, "Song")
    id3 = ID3(mp3)
    from mutagen.id3 import TCON
    id3.add(TCON(encoding=3, text="Techno"))
    id3.save(mp3, v2_version=3)
    client.patch(f"/api/tracks/{t}", json={"title": "Song 2"})  # an ordinary retag
    assert str(ID3(mp3)["TCON"].text[0]) == "Techno"


def test_rename_and_merge_retag_files(client, lib, make_mp3):
    t = track(lib, "Song")
    mp3 = with_file(lib, make_mp3, t, "Song")
    house = mk(client, "House")
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": [house]})
    client.patch(f"/api/genres/{house}", json={"name": "House Music"})
    assert str(ID3(mp3)["TCON"].text[0]) == "House Music"
    other = mk(client, "Dance")
    client.post(f"/api/genres/{house}/merge", json={"into": other})
    assert str(ID3(mp3)["TCON"].text[0]) == "Dance"


def test_bulk_add_remove_is_idempotent(client, lib):
    a, b = track(lib, "A"), track(lib, "B")
    g = mk(client, "House")
    assert client.post("/api/tracks/genres", json={"ids": [a, b], "genre_id": g, "action": "add"}).status_code == 200
    client.post("/api/tracks/genres", json={"ids": [a, b], "genre_id": g, "action": "add"})
    conn = lib.conn()
    assert conn.execute("SELECT count(*) FROM track_genres").fetchone()[0] == 2
    conn.close()
    client.post("/api/tracks/genres", json={"ids": [a], "genre_id": g, "action": "remove"})
    conn = lib.conn()
    assert conn.execute("SELECT count(*) FROM track_genres").fetchone()[0] == 1
    conn.close()
    assert client.post("/api/tracks/genres", json={"ids": [a], "genre_id": g, "action": "nope"}).status_code == 400


# ------------------------------------------------------------ views, artists, search

def test_genre_views_artists_and_search(client, lib):
    house = mk(client, "House")
    deep = mk(client, "Deep House", house)
    t1 = track(lib, "One", "Mike, Tony")
    t2 = track(lib, "Two", "Mike")
    t3 = track(lib, "Three", "Zed")
    client.put(f"/api/tracks/{t1}/genres", json={"genre_ids": [house]})
    client.put(f"/api/tracks/{t2}/genres", json={"genre_ids": [deep]})
    ids = lambda url: sorted(t["id"] for t in client.get(url).json()["tracks"])
    assert ids(f"/api/tracks?view=genre:{house}") == sorted([t1, t2])
    assert ids(f"/api/tracks?view=genre:{house}&sub=0") == [t1]
    assert ids("/api/tracks?view=genre:none") == [t3]
    assert ids(f"/api/tracks?view=genre:{house}&artist=tony") == [t1]
    assert ids("/api/tracks?view=smart:all&q=deep") == [t2]  # search finds genre names
    art = client.get(f"/api/genre_artists?view=genre:{house}").json()
    assert art["songs"] == 2 and art["artists"][0] == {"name": "Mike", "count": 2}
    assert {a["name"] for a in art["artists"]} == {"Mike", "Tony"}
    assert client.get("/api/tracks?view=genre:9999").status_code == 404


# ------------------------------------------------------------ Rekordbox / MP3 genre names

def test_rekordbox_genre_is_stored_and_warns_when_different(client, lib, tmp_path):
    t = track(lib, "Song", file_rel_path="a.mp3", download_status="done")
    xml = tmp_path / "rb.xml"
    loc = rekordbox.path_to_location(str(lib.library / "a.mp3"))
    xml.write_text(f'<DJ_PLAYLISTS><COLLECTION><TRACK TrackID="1" Location="{loc}" Genre="Hip-Hop/Rap"/></COLLECTION></DJ_PLAYLISTS>')
    conn = lib.conn()
    assert rekordbox.import_xml(conn, xml)["genres"] == 1
    conn.close()
    tr = client.get("/api/tracks?view=smart:all").json()["tracks"][0]
    assert tr["rekordbox_genre"] == "Hip-Hop/Rap" and tr["genres"] == [] and not tr["rekordbox_genre_differs"]
    hh = mk(client, "Hip Hop")
    client.post(f"/api/genres/{hh}/aliases", json={"name": "Hip-Hop"})
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": [hh]})
    assert client.get("/api/tracks?view=smart:all").json()["tracks"][0]["rekordbox_genre_differs"]  # "Rap" isn't known
    client.post(f"/api/genres/{hh}/aliases", json={"name": "Rap"})
    assert not client.get("/api/tracks?view=smart:all").json()["tracks"][0]["rekordbox_genre_differs"]
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": []})
    conn = lib.conn()  # DJ Crate's own genres were never touched by the import
    assert conn.execute("SELECT count(*) FROM track_genres").fetchone()[0] == 0
    conn.close()


def test_bring_in_genres_lists_then_applies_only_to_songs_without_genres(client, lib):
    a = track(lib, "A", rekordbox_genre="Hip-Hop/Rap")
    b = track(lib, "B", file_genre="House / Deep House")
    c = track(lib, "C", rekordbox_genre="Hip-Hop/Rap")
    hh, house = mk(client, "Hip Hop"), mk(client, "House")
    deep = mk(client, "Deep House", house)
    client.post(f"/api/genres/{hh}/aliases", json={"name": "Hip-Hop"})
    client.post("/api/genres/ignore", json={"name": "Rap"})
    client.put(f"/api/tracks/{c}/genres", json={"genre_ids": [house]})  # already has one
    items = {i["name"]: i for i in client.get("/api/genres/incoming").json()["items"]}
    assert items["Hip-Hop"]["genre_id"] == hh and items["Hip-Hop"]["songs"] == 2 and items["Hip-Hop"]["unsorted"] == 1
    assert items["Rap"]["ignored"] and items["Rap"]["genre_id"] is None
    assert items["Deep House"]["path"] == ["House", "Deep House"]
    r = client.post("/api/genres/incoming/apply").json()
    assert r["assigned"] == 2
    got = {t["id"]: t for t in client.get("/api/tracks?view=smart:all").json()["tracks"]}
    assert [g["id"] for g in got[a]["genres"]] == [hh]
    assert [g["id"] for g in got[b]["genres"]] == [deep]  # the more specific one only
    assert [g["id"] for g in got[c]["genres"]] == [house]  # untouched


def test_folder_import_keeps_the_files_genre(client, lib, make_mp3, tmp_path):
    from mutagen.id3 import TCON
    folder = tmp_path / "in"
    mp3 = make_mp3(folder / "Song - Art.mp3")
    from mutagen.id3 import ID3NoHeaderError
    try:
        id3 = ID3(mp3)
    except ID3NoHeaderError:
        id3 = ID3()
    id3.add(TCON(encoding=3, text="Techno"))
    id3.save(mp3, v2_version=3)
    client.post("/api/library/import_folder", json={"path": str(folder)})
    tr = client.get("/api/tracks?view=smart:all").json()["tracks"][0]
    assert tr["file_genre"] == "Techno" and tr["genres"] == []


# ------------------------------------------------------------ live genre crates

def crate_ids(client, crate_id):
    return sorted(t["id"] for t in client.get(f"/api/tracks?view=crate:{crate_id}").json()["tracks"])


def test_genre_crate_follows_the_genre(client, lib):
    house = mk(client, "House")
    deep = mk(client, "Deep House", house)
    t1, t2, t3 = track(lib, "One"), track(lib, "Two"), track(lib, "Three")
    client.put(f"/api/tracks/{t1}/genres", json={"genre_ids": [house]})
    cid = client.post(f"/api/genres/{house}/crate", json={"parent_id": None}).json()["crate_id"]
    assert client.post(f"/api/genres/{house}/crate", json={}).json()["crate_id"] == cid  # no second crate
    assert crate_ids(client, cid) == [t1]
    client.put(f"/api/tracks/{t2}/genres", json={"genre_ids": [deep]})  # sub-genre songs count
    assert crate_ids(client, cid) == sorted([t1, t2])
    client.put(f"/api/tracks/{t1}/genres", json={"genre_ids": []})
    assert crate_ids(client, cid) == [t2]
    # by hand: refused
    for call in (lambda: client.post(f"/api/crates/{cid}/tracks", json={"ids": [t3]}),
                 lambda: client.post(f"/api/crates/{cid}/tracks/remove", json={"ids": [t2]}),
                 lambda: client.post(f"/api/crates/{cid}/tracks/restore", json={"rows": []})):
        r = call()
        assert r.status_code == 409 and r.json()["error"]["code"] == "genre_crate"
    sidebar = {c["id"]: c for c in client.get("/api/sidebar").json()["tree"]}
    assert sidebar[cid]["genre_id"] == house


def test_deleting_the_genre_leaves_a_normal_crate_and_unfollow_works(client, lib):
    g1, g2 = mk(client, "House"), mk(client, "Techno")
    t = track(lib, "One")
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": [g1]})
    c1 = client.post(f"/api/genres/{g1}/crate", json={}).json()["crate_id"]
    c2 = client.post(f"/api/genres/{g2}/crate", json={}).json()["crate_id"]
    client.delete(f"/api/genres/{g1}")
    assert crate_ids(client, c1) == [t]  # a normal crate now, with the songs it had
    assert client.post(f"/api/crates/{c1}/tracks", json={"ids": [t]}).status_code == 200  # and editable
    client.post(f"/api/crates/{c2}/unfollow")
    assert client.post(f"/api/crates/{c2}/tracks", json={"ids": [t]}).status_code == 200


def test_genre_crates_are_in_the_rekordbox_export(client, lib, make_mp3):
    from tests.test_rekordbox import build
    t = track(lib, "Song")
    with_file(lib, make_mp3, t, "Song")
    house = mk(client, "House")
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": [house]})
    client.post(f"/api/genres/{house}/crate", json={})
    root, _ = build(lib)
    assert any(n.get("Name") == "House" and n.get("Type") == "1" for n in root.iter("NODE"))


def test_guard_helper_unknown_crate_is_fine(lib):
    conn = lib.conn()
    genres.guard_not_genre_crate(conn, 12345)  # no such crate: nothing to guard
    conn.close()
    with pytest.raises(AppError):
        genres.get(lib.conn(), 99)


def test_suggestions_from_rekordbox_and_file_resolve_through_aliases(client, lib):
    t = track(lib, "Song", rekordbox_genre="Hip-Hop/Rap", file_genre="Techno")
    hh = mk(client, "Hip Hop")
    client.post(f"/api/genres/{hh}/aliases", json={"name": "Hip-Hop"})
    client.post("/api/genres/ignore", json={"name": "Techno"})
    sug = client.get("/api/tracks?view=smart:all").json()["tracks"][0]["genre_suggestions"]
    assert [(s["label"], s["genre_id"], s["source"]) for s in sug] == [("Hip Hop", hh, "Rekordbox"), ("Rap", None, "Rekordbox")]
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": [hh]})
    assert client.get("/api/tracks?view=smart:all").json()["tracks"][0]["genre_suggestions"] == []


def test_download_all_works_on_a_genre_view(client, lib):
    t = track(lib, "One", source_url="https://www.youtube.com/watch?v=abcdefghijk")
    g = mk(client, "House")
    client.put(f"/api/tracks/{t}/genres", json={"genre_ids": [g]})
    r = client.post("/api/downloads/all", json={"view": f"genre:{g}"})
    assert r.status_code == 200


# ------------------------------------------------ MusicBrainz suggestions

from djcrate import genre_lookup  # noqa: E402


class FakeClient:
    """Answers like MusicBrainz did for the real songs we measured."""

    def __init__(self, recordings=None, genres=None, artists=None, search=None):
        self.calls = []
        self.recordings, self.genres, self.artists, self.search = recordings or {}, genres or {}, artists or {}, search or []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path.startswith("/isrc/"):
            rid = self.recordings.get(path.split("/")[-1])
            return {"recordings": [{"id": rid}]} if rid else {"recordings": []}
        if path == "/recording":
            return {"recordings": self.search}
        if path.startswith("/recording/"):
            rid = path.split("/")[-1]
            return {"genres": self.genres.get(rid, []), "artist-credit": [{"artist": {"id": a}} for a in self.artists.get(rid, [])]}
        if path.startswith("/artist/"):
            return {"genres": self.ARTIST_GENRES.get(path.split("/")[-1], [])}

    ARTIST_GENRES = {"A1": [{"name": "hip hop", "count": 3}, {"name": "drumless hip hop", "count": 1}], "A2": [{"name": "r&b", "count": 2}]}


def test_display_name():
    assert genre_lookup.display_name("hip hop") == "Hip Hop" and genre_lookup.display_name("r&b") == "R&B"


def test_lookup_song_genres_first_then_artist_genres(lib):
    conn = lib.conn()
    c = FakeClient(recordings={"ISRC1": "R1"}, genres={"R1": [{"name": "gangsta rap", "count": 1}, {"name": "hip hop", "count": 4}]},
                   artists={"R1": ["A1", "A2"]})
    status, items = genre_lookup.lookup(c, conn, {"isrc": "isrc1", "title": "x", "artists": "y"})
    assert status == "found"
    assert items == [{"name": "Hip Hop", "level": "song"}, {"name": "Gangsta Rap", "level": "song"},
                     {"name": "Drumless Hip Hop", "level": "artist"}, {"name": "R&B", "level": "artist"}]  # no repeat of hip hop
    n_artist_calls = sum(1 for p, _ in c.calls if p.startswith("/artist/"))
    genre_lookup.lookup(c, conn, {"isrc": "ISRC1", "title": "x", "artists": "y"})
    assert sum(1 for p, _ in c.calls if p.startswith("/artist/")) == n_artist_calls  # artists are cached
    conn.close()


def test_lookup_without_isrc_needs_a_confident_match(lib):
    conn = lib.conn()
    weak = FakeClient(search=[{"id": "R9", "score": 80}])
    assert genre_lookup.lookup(weak, conn, {"title": "Glide (feat. X)", "artists": "Hoodboi, Tkay"})[0] == "not_in_musicbrainz"
    path, params = weak.calls[0]
    assert path == "/recording" and 'recording:"Glide"' in params["query"] and 'artist:"Hoodboi"' in params["query"]
    strong = FakeClient(search=[{"id": "R9", "score": 100}], genres={"R9": [{"name": "house", "count": 1}]})
    assert genre_lookup.lookup(strong, conn, {"title": "Glide", "artists": "Hoodboi"}) == ("found", [{"name": "House", "level": "song"}])
    # an ISRC that MusicBrainz doesn't know is final: no guessing by text
    unknown = FakeClient()
    assert genre_lookup.lookup(unknown, conn, {"isrc": "NOPE", "title": "x", "artists": "y"})[0] == "not_in_musicbrainz"
    assert all(p != "/recording" for p, _ in unknown.calls)
    conn.close()


def test_finder_looks_up_songs_without_genres_and_chips_appear(client, lib):
    a = track(lib, "Alpha", isrc="ISRC1")
    b = track(lib, "Beta", rekordbox_genre="Techno")        # already has a genre to go on: skipped
    c = track(lib, "Gamma", isrc="ISRC2")
    g = mk(client, "Hip Hop")
    c_fake = FakeClient(recordings={"ISRC1": "R1"}, genres={"R1": [{"name": "hip hop", "count": 1}]}, artists={"R1": ["A1"]})
    finder = genre_lookup.Finder(lib, client_factory=lambda: c_fake)
    assert finder.step() and finder.step() and not finder.step()  # a, c, then nothing left
    tracks = {t["id"]: t for t in client.get("/api/tracks?view=smart:all").json()["tracks"]}
    assert [(s["label"], s["genre_id"], s["level"]) for s in tracks[a]["genre_suggestions"]] == [
        ("Hip Hop", g, "song"), ("Drumless Hip Hop", None, "artist")]
    assert [s["source"] for s in tracks[a]["genre_suggestions"]] == ["MusicBrainz", "MusicBrainz"]
    assert tracks[c]["genre_suggestions"] == []  # not in MusicBrainz
    assert [s["label"] for s in tracks[b]["genre_suggestions"]] == ["Techno"]
    st = client.get("/api/status").json()["genres"]
    assert st["looked_up"] == 2 and st["found"] == 1 and st["pending"] == 0
    # once a song has a rack genre its suggestions go away
    client.put(f"/api/tracks/{a}/genres", json={"genre_ids": [g]})
    assert {t["id"]: t for t in client.get("/api/tracks?view=smart:all").json()["tracks"]}[a]["genre_suggestions"] == []


def test_finder_pauses_when_musicbrainz_is_busy_and_respects_the_switch(client, lib):
    track(lib, "Alpha", isrc="ISRC1")

    class Busy:
        def get(self, *a, **k):
            raise genre_lookup.MBError("503")

    finder = genre_lookup.Finder(lib, client_factory=Busy)
    assert not finder.step() and finder.paused
    conn = lib.conn()
    assert genre_lookup.status(conn, finder)["paused_until"]
    conn.close()
    fresh = genre_lookup.Finder(lib, client_factory=lambda: FakeClient())
    client.put("/api/genres/lookup", json={"on": False})
    assert not fresh.step() and client.get("/api/status").json()["genres"]["on"] is False
    client.put("/api/genres/lookup", json={"on": True})
    lib.read_only = True
    assert not fresh.step()  # never writes into a read-only library
    lib.read_only = False


# ------------------------------------------------ Genre Rack cards + MusicBrainz's genre list

def test_tree_nodes_carry_top_artists_and_covers_for_the_cards(client, lib):
    house = mk(client, "House")
    deep = mk(client, "Deep House", house)
    a = track(lib, "One", "Mike, Tony", cover_url="http://c/1.jpg")
    b = track(lib, "Two", "Mike", cover_url="http://c/2.jpg")
    client.put(f"/api/tracks/{a}/genres", json={"genre_ids": [house]})
    client.put(f"/api/tracks/{b}/genres", json={"genre_ids": [deep]})
    node = client.get("/api/genres").json()["tree"][0]
    assert node["top_artists"][0] == "Mike" and set(node["top_artists"]) == {"Mike", "Tony"}
    assert sorted(node["covers"]) == ["http://c/1.jpg", "http://c/2.jpg"]
    assert node["children"][0]["top_artists"] == ["Mike"]


def test_musicbrainz_list_shows_only_genres_your_songs_need(client, lib):
    a, b, c = track(lib, "A"), track(lib, "B"), track(lib, "C")
    import json, time
    conn = lib.conn()
    put = lambda tid, items: conn.execute("INSERT INTO genre_suggest VALUES (?, ?, 'found', ?)", (tid, json.dumps(items), time.time()))
    put(a, [{"name": "Pop", "level": "artist"}, {"name": "Hip Hop", "level": "song"}])
    put(b, [{"name": "Pop", "level": "artist"}, {"name": "Techno", "level": "song"}])
    put(c, [{"name": "Pop", "level": "artist"}])
    conn.close()
    hh = mk(client, "Hip-Hop")                                  # already in the rack: not needed
    client.post("/api/genres/ignore", json={"name": "Techno"})   # dismissed: not needed
    r = client.get("/api/genres/musicbrainz").json()
    assert r["mode"] == "needed" and r["genres"] == [{"name": "Pop", "songs": 3, "level": "artist"}]
    client.put(f"/api/tracks/{c}/genres", json={"genre_ids": [hh]})   # c is sorted now: no longer counts
    assert client.get("/api/genres/musicbrainz").json()["genres"][0]["songs"] == 2


def test_musicbrainz_search_covers_the_whole_list_cached_once(client, lib, monkeypatch):
    calls = []
    monkeypatch.setattr(genre_lookup, "fetch_genre_list", lambda: calls.append(1) or ["acid house", "afro house", "amapiano", "techno"])
    house = mk(client, "Afro-House")
    r = client.get("/api/genres/musicbrainz?q=house").json()
    assert r["mode"] == "search" and r["total"] == 4 and r["error"] is None
    assert {g["name"]: g["genre_id"] for g in r["genres"]} == {"acid house": None, "afro house": house}
    client.get("/api/genres/musicbrainz?q=techno")
    assert len(calls) == 1  # fetched once, then kept in the library


def test_musicbrainz_genre_list_offline_without_cache_says_so(client, monkeypatch):
    def boom():
        raise genre_lookup.MBError("no network")
    monkeypatch.setattr(genre_lookup, "fetch_genre_list", boom)
    r = client.get("/api/genres/musicbrainz?q=house").json()
    assert r["total"] == 0 and "Couldn't reach MusicBrainz" in r["error"]


def test_others_view_lists_songs_not_in_the_genre(client, lib):
    house = mk(client, "House")
    deep = mk(client, "Deep House", house)
    a, b, c = track(lib, "A"), track(lib, "B"), track(lib, "C")
    client.put(f"/api/tracks/{a}/genres", json={"genre_ids": [house]})
    client.put(f"/api/tracks/{b}/genres", json={"genre_ids": [deep]})
    ids = lambda url: sorted(t["id"] for t in client.get(url).json()["tracks"])
    assert ids(f"/api/tracks?view=genre:{house}&others=1") == [c]            # sub-genre songs count as "in"
    assert ids(f"/api/tracks?view=genre:{house}&sub=0&others=1") == sorted([b, c])
    empty = mk(client, "Techno")
    assert ids(f"/api/tracks?view=genre:{empty}&others=1") == sorted([a, b, c])


def test_others_can_hide_songs_that_already_have_a_genre(client, lib):
    house, techno = mk(client, "House"), mk(client, "Techno")
    a, b, c = track(lib, "A"), track(lib, "B"), track(lib, "C")
    client.put(f"/api/tracks/{a}/genres", json={"genre_ids": [house]})
    client.put(f"/api/tracks/{b}/genres", json={"genre_ids": [techno]})
    ids = lambda url: sorted(t["id"] for t in client.get(url).json()["tracks"])
    assert ids(f"/api/tracks?view=genre:{house}&others=1") == sorted([b, c])
    assert ids(f"/api/tracks?view=genre:{house}&others=1&nogenre=1") == [c]          # B has Techno: hidden
    assert ids(f"/api/tracks?view=genre:{house}&others=1&nogenre=1&q=C") == [c]
    assert ids(f"/api/tracks?view=genre:{house}&nogenre=1") == [a]                   # only meaningful with others=1


def test_others_can_be_limited_to_songs_in_another_genre(client, lib):
    house, techno, deep = mk(client, "House"), mk(client, "Techno"), None
    deep = mk(client, "Deep Techno", techno)
    a, b, c, d = track(lib, "A"), track(lib, "B"), track(lib, "C"), track(lib, "D")
    client.put(f"/api/tracks/{b}/genres", json={"genre_ids": [techno]})
    client.put(f"/api/tracks/{c}/genres", json={"genre_ids": [deep]})
    ids = lambda url: sorted(t["id"] for t in client.get(url).json()["tracks"])
    assert ids(f"/api/tracks?view=genre:{house}&others=1&ingenre={techno}") == sorted([b, c])   # sub-genre songs count
    assert ids(f"/api/tracks?view=genre:{house}&others=1&ingenre={deep}") == [c]
    assert ids(f"/api/tracks?view=genre:{techno}&others=1&ingenre={techno}") == []              # already in the target genre
