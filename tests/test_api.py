from djcrate import db, tags


def mk(client, **body):
    r = client.post("/api/tracks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["track"]


def test_crates_folders_and_shared_metadata(client):
    folder = client.post("/api/crates", json={"name": "House", "kind": "folder"}).json()["id"]
    a = client.post("/api/crates", json={"name": "Warmup", "parent_id": folder}).json()["id"]
    b = client.post("/api/crates", json={"name": "Peak"}).json()["id"]
    assert client.post("/api/crates", json={"name": "X", "parent_id": a}).status_code == 400  # not into a crate
    t = mk(client, title="Strobe", artists="deadmau5", crate_id=a)
    client.post(f"/api/crates/{b}/tracks", json={"ids": [t["id"]]})
    # A change in one crate shows in the other: it's the same row.
    client.patch(f"/api/tracks/{t['id']}", json={"notes": "drop at 1:04", "ready": True})
    tb = client.get(f"/api/tracks?view=crate:{b}").json()["tracks"][0]
    assert tb["notes"] == "drop at 1:04" and tb["ready"]
    # Folder view shows its crates' songs
    assert len(client.get(f"/api/tracks?view=crate:{folder}").json()["tracks"]) == 1
    side = client.get("/api/sidebar").json()
    assert side["smart"][0] == {"key": "all", "name": "All Tracks", "count": 1}
    assert side["tree"][0]["stats"]["ready"] == 1
    # Can't move a folder into itself
    assert client.patch(f"/api/crates/{folder}", json={"move": True, "parent_id": folder}).status_code == 400
    # Deleting a crate never deletes songs
    client.delete(f"/api/crates/{b}")
    assert len(client.get("/api/tracks?view=smart:all").json()["tracks"]) == 1


def test_link_validation_and_duplicate_warning(client):
    t1 = mk(client, title="A", artists="x")
    t2 = mk(client, title="B", artists="y")
    r = client.patch(f"/api/tracks/{t1['id']}", json={"source_url": "https://evil.com/a"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_url"
    url = "https://www.youtube.com/watch?v=abc"
    assert client.patch(f"/api/tracks/{t1['id']}", json={"source_url": url}).json()["warnings"] == []
    r = client.patch(f"/api/tracks/{t2['id']}", json={"source_url": url}).json()
    assert "already used" in r["warnings"][0] and r["track"]["source_url"] == url
    smart = {s["key"]: s["count"] for s in client.get("/api/sidebar").json()["smart"]}
    assert smart["to_download"] == 2 and smart["needs_link"] == 0


def test_remove_from_crate_with_undo(client):
    c = client.post("/api/crates", json={"name": "C"}).json()["id"]
    t = mk(client, title="A", crate_id=c)
    removed = client.post(f"/api/crates/{c}/tracks/remove", json={"ids": [t["id"]]}).json()["removed"]
    assert client.get(f"/api/tracks?view=crate:{c}").json()["tracks"] == []
    client.post(f"/api/crates/{c}/tracks/restore", json={"rows": removed})
    assert len(client.get(f"/api/tracks?view=crate:{c}").json()["tracks"]) == 1


def test_edited_bypasses_length_check(client, lib, make_mp3):
    t = mk(client, title="Cut", artists="Me")
    mp3 = make_mp3(lib.library / "Cut - Me.mp3", seconds=3)
    conn = lib.conn()
    conn.execute("UPDATE tracks SET duration_ms = 20000, file_rel_path = ?, file_duration_ms = ?, download_status = 'done' WHERE id = ?",
                 (lib.rel_path(mp3), tags.duration_ms(mp3), t["id"]))
    smart = lambda: {s["key"]: s["count"] for s in client.get("/api/sidebar").json()["smart"]}
    assert smart()["check_version"] == 1
    r = client.patch(f"/api/tracks/{t['id']}", json={"edited": True}).json()["track"]
    assert r["check_version"] is False and smart()["check_version"] == 0
    client.patch(f"/api/tracks/{t['id']}", json={"edited": False})
    assert smart()["check_version"] == 1


def test_metadata_edit_rewrites_tags_not_filename(client, lib, make_mp3):
    t = mk(client, title="Old", artists="Me")
    mp3 = make_mp3(lib.library / "Old - Me.mp3")
    conn = lib.conn()
    conn.execute("UPDATE tracks SET file_rel_path = ?, download_status = 'done' WHERE id = ?", (lib.rel_path(mp3), t["id"]))
    client.patch(f"/api/tracks/{t['id']}", json={"title": "New", "notes": "big drop"})
    assert mp3.exists()  # filename unchanged so Rekordbox keeps its link
    meta = tags.read_tags(mp3)
    assert meta["title"] == "New"
    from mutagen.id3 import ID3
    assert str(ID3(mp3).getall("COMM")[0].text[0]) == "big drop"


def test_missing_files_and_relocate(client, lib, make_mp3):
    t = mk(client, title="Song", artists="A")
    mp3 = make_mp3(lib.library / "Song - A.mp3")
    conn = lib.conn()
    conn.execute("UPDATE tracks SET file_rel_path = ?, download_status = 'done' WHERE id = ?", (lib.rel_path(mp3), t["id"]))
    moved = lib.library / "sub" / "Song - A.mp3"
    moved.parent.mkdir()
    mp3.rename(moved)
    assert client.post("/api/library/rescan").json()["missing"] == 1
    matches = client.post("/api/library/auto_relocate/preview", json={}).json()["matches"]
    assert matches[0]["new_path"] == str(moved)
    assert db.get_track(conn, t["id"])["download_status"] == "missing"  # preview changes nothing
    client.post("/api/library/auto_relocate/apply", json={"matches": matches})
    tr = db.get_track(conn, t["id"])
    assert tr["download_status"] == "done" and tr["file_rel_path"] == "sub/Song - A.mp3"


def test_folder_import(client, lib, make_mp3, tmp_path):
    src = tmp_path / "old"
    make_mp3(src / "One - Artist.mp3")
    (src / "cover.jpg").write_bytes(b"x")
    (src / "broken.mp3").write_bytes(b"not audio")
    r = client.post("/api/library/import_folder", json={"path": str(src)}).json()
    assert r["imported"] == 1 and len(r["skipped"]) == 2
    t = client.get(f"/api/tracks?view=crate:{r['crate_id']}").json()["tracks"][0]
    assert (t["title"], t["artists"], t["download_status"]) == ("One", "Artist", "done")
    again = client.post("/api/library/import_folder", json={"path": str(src)}).json()
    assert again["imported"] == 0 and again["already_known"] == 1




def test_read_only_blocks_writes(client, lib):
    lib.read_only = True
    r = client.post("/api/crates", json={"name": "Nope"})
    assert r.status_code == 423 and r.json()["error"]["code"] == "read_only"
    assert client.get("/api/sidebar").status_code == 200


def test_unexpected_errors_are_reported(client, lib, monkeypatch):
    monkeypatch.setattr("djcrate.setboard.set_text", lambda d: 1 / 0)
    from fastapi.testclient import TestClient
    from djcrate.api import app
    c = TestClient(app, raise_server_exceptions=False)
    sid = c.post("/api/sets", json={"name": "S"}).json()["id"]
    r = c.get(f"/api/sets/{sid}/export")
    assert r.status_code == 500 and "Activity" in r.json()["error"]["message"]
    assert c.get("/api/status").json()["errors_count"] >= 1



def test_upgrade_from_v1_backs_up_first(tmp_path):
    import sqlite3
    from djcrate import db
    from djcrate.state import AppState
    lib = tmp_path / "Old"
    (lib / ".djcrate").mkdir(parents=True)
    conn = sqlite3.connect(lib / ".djcrate" / "djcrate.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for stmt in [s for s in db.MIGRATIONS[0].split(";") if s.strip()]:
        conn.execute(stmt)
    conn.execute("INSERT INTO schema_version VALUES (1)")
    conn.commit()
    conn.close()
    st = AppState()
    st.open(lib)
    assert st.status == "ok"
    assert list((lib / ".djcrate" / "backups").glob("pre-migration-v1-*.db"))
    cols = [r[1] for r in st.conn().execute("PRAGMA table_info(set_items)")]
    assert "length_ms" in cols
    st.release_lock()


def test_multiple_libraries_switch_and_picker(client, lib, tmp_path, monkeypatch):
    from djcrate import libraries
    monkeypatch.setattr(libraries, "LIBRARIES_DIR", tmp_path / "Libraries")
    first = lib.library
    mk(client, title="Mine", artists="A")
    r = client.post("/api/library/new", json={"name": "Sam"}).json()
    assert r["library"]["status"] == "ok" and r["library"]["name"] == "Sam"
    assert client.get("/api/tracks?view=smart:all").json()["tracks"] == []  # Sam's library is empty
    libs = client.get("/api/libraries").json()["libraries"]
    assert [l["name"] for l in libs][:2] == ["Sam", first.name]
    assert next(l for l in libs if l["path"] == str(first))["tracks"] == 1
    # switching goes back to the picker; downloads block it
    assert client.post("/api/library/switch").json()["library"]["status"] == "choose_library"
    assert client.get("/api/sidebar").status_code == 409
    client.post("/api/library/choose", json={"path": str(first)})
    assert [t["title"] for t in client.get("/api/tracks?view=smart:all").json()["tracks"]] == ["Mine"]
    client.post("/api/library/prefs", json={"ask_on_launch": False})
    assert libraries.ask_on_launch() is False and libraries.last_library() == first
    # forgetting removes it from the list but never deletes the folder
    sam = tmp_path / "Libraries" / "Sam"
    client.post("/api/library/forget", json={"path": str(sam)})
    assert sam.is_dir() and all(l["path"] != str(sam) for l in client.get("/api/libraries").json()["libraries"])
    assert client.post("/api/library/new", json={"name": "Sam"}).status_code == 400  # folder already has data


def test_switch_blocked_while_downloading(client, lib):
    t = mk(client, title="A", source_url="https://youtu.be/x")
    client.post("/api/downloads", json={"ids": [t["id"]]})
    r = client.post("/api/library/switch")
    assert r.status_code == 400 and r.json()["error"]["code"] == "busy"


def test_upgrade_turns_verified_into_ready(tmp_path):
    import sqlite3
    from djcrate import db
    from djcrate.state import AppState
    lib = tmp_path / "Old"
    (lib / ".djcrate").mkdir(parents=True)
    conn = sqlite3.connect(lib / ".djcrate" / "djcrate.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for migration in db.MIGRATIONS[:2]:
        for stmt in [x for x in migration.split(";") if x.strip()]:
            conn.execute(stmt)
    conn.execute("INSERT INTO schema_version VALUES (2)")
    conn.execute("INSERT INTO tracks (title, cleaned, verified) VALUES ('A', 1, 1), ('B', 1, 0)")
    conn.commit()
    conn.close()
    st = AppState()
    st.open(lib)
    rows = st.conn().execute("SELECT title, ready, broken FROM tracks ORDER BY title").fetchall()
    assert [tuple(r) for r in rows] == [("A", 1, 0), ("B", 0, 0)]
    st.release_lock()
