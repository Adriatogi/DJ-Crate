"""Trash and Hidden."""

from djcrate import db, rekordbox


def add(lib, title, rel=None, status="none"):
    conn = lib.conn()
    try:
        if rel:
            p = lib.library / rel
            p.write_bytes(b"x")
        return conn.execute("INSERT INTO tracks (title, artists, file_rel_path, download_status) VALUES (?, 'A', ?, ?)",
                            (title, rel, status)).lastrowid
    finally:
        conn.close()


def ids(client, view):
    return {t["id"] for t in client.get(f"/api/tracks?view={view}").json()["tracks"]}


def counts(client):
    sb = client.get("/api/sidebar").json()
    return {s["key"]: s["count"] for s in sb["smart"] + sb["bins"]}


def test_trash_hides_everywhere_and_restore(client, lib):
    a, b = add(lib, "Keep"), add(lib, "Bin me")
    crate = client.post("/api/crates", json={"name": "C", "kind": "crate"}).json()["id"]
    client.post(f"/api/crates/{crate}/tracks", json={"ids": [a, b]})
    assert client.post("/api/tracks/trash", json={"ids": [b]}).json()["trashed"] == 1
    assert ids(client, "smart:all") == {a} and ids(client, f"crate:{crate}") == {a}
    assert ids(client, "smart:trash") == {b}
    assert b not in ids(client, "smart:needs_link")
    c = counts(client)
    assert c["all"] == 1 and c["trash"] == 1
    t = client.get("/api/tracks?view=smart:trash").json()["tracks"][0]
    assert t["trash"]["kind"] == "deleted" and t["trash"]["at"]
    # nothing downloads from the Trash
    assert client.post("/api/downloads/all", json={"view": "smart:trash"}).json()["queued"] == 0
    client.post("/api/tracks/restore", json={"ids": [b]})
    assert ids(client, f"crate:{crate}") == {a, b} and not ids(client, "smart:trash")   # back in its crate


def test_delete_crate_trashes_only_songs_in_no_other_crate(client, lib):
    only, shared = add(lib, "Only here"), add(lib, "Also elsewhere")
    c1 = client.post("/api/crates", json={"name": "Gone", "kind": "crate"}).json()["id"]
    c2 = client.post("/api/crates", json={"name": "Stays", "kind": "crate"}).json()["id"]
    client.post(f"/api/crates/{c1}/tracks", json={"ids": [only, shared]})
    client.post(f"/api/crates/{c2}/tracks", json={"ids": [shared]})
    r = client.request("DELETE", f"/api/crates/{c1}").json()
    assert r["trashed"] == 1
    assert ids(client, "smart:trash") == {only} and ids(client, "smart:all") == {shared}
    t = client.get("/api/tracks?view=smart:trash").json()["tracks"][0]
    assert t["trash"] == {**t["trash"], "kind": "crate", "crate": "Gone"}


def test_delete_folder_trashes_songs_of_its_crates(client, lib):
    s = add(lib, "Deep")
    folder = client.post("/api/crates", json={"name": "F", "kind": "folder"}).json()["id"]
    inner = client.post("/api/crates", json={"name": "Inner", "kind": "crate", "parent_id": folder}).json()["id"]
    client.post(f"/api/crates/{inner}/tracks", json={"ids": [s]})
    assert client.request("DELETE", f"/api/crates/{folder}").json()["trashed"] == 1
    assert ids(client, "smart:trash") == {s}


def test_adding_a_trashed_song_to_a_crate_restores_it(client, lib):
    s = add(lib, "Back")
    client.post("/api/tracks/trash", json={"ids": [s]})
    crate = client.post("/api/crates", json={"name": "C", "kind": "crate"}).json()["id"]
    client.post(f"/api/crates/{crate}/tracks", json={"ids": [s]})
    assert ids(client, "smart:all") == {s}


def test_empty_trash_deletes_for_good(client, lib):
    a, b = add(lib, "Gone", rel="Gone.mp3", status="done"), add(lib, "Stay")
    client.post("/api/tracks/trash", json={"ids": [a]})
    trashed = []
    import djcrate.library as library
    orig, library.trash = library.trash, lambda p: trashed.append(p)
    try:
        r = client.post("/api/trash/empty").json()
    finally:
        library.trash = orig
    assert r["deleted"] == 1 and len(trashed) == 1
    assert ids(client, "smart:all") == {b} and not ids(client, "smart:trash")


def test_hidden_vanishes_and_comes_back(client, lib):
    a, b = add(lib, "Visible"), add(lib, "Gone file", status="missing")
    crate = client.post("/api/crates", json={"name": "C", "kind": "crate"}).json()["id"]
    client.post(f"/api/crates/{crate}/tracks", json={"ids": [a, b]})
    assert counts(client)["missing"] == 1
    client.post("/api/tracks/hide", json={"ids": [b]})
    assert ids(client, "smart:all") == {a} and ids(client, f"crate:{crate}") == {a}
    assert not ids(client, "smart:missing") and ids(client, "smart:hidden") == {b}
    c = counts(client)
    assert c["missing"] == 0 and c["hidden"] == 1
    assert client.get("/api/tracks?view=smart:hidden").json()["tracks"][0]["hidden"] is True
    client.post("/api/tracks/hide", json={"ids": [b], "hidden": False})
    assert ids(client, f"crate:{crate}") == {a, b} and counts(client)["hidden"] == 0


def test_rekordbox_export_leaves_out_trash_and_hidden(lib):
    conn = lib.conn()
    a, b, c = add(lib, "In", "In.mp3", "done"), add(lib, "Binned", "Binned.mp3", "done"), add(lib, "Hid", "Hid.mp3", "done")
    conn.execute("UPDATE tracks SET trashed_at = 1 WHERE id = ?", (b,))
    conn.execute("UPDATE tracks SET hidden_at = 1 WHERE id = ?", (c,))
    data, summary = rekordbox.build_export(lib, conn)
    assert summary["songs"] == 1 and b"Binned" not in data and b"Hid" not in data


def test_migration_trashes_songs_already_removed_on_spotify(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "x.db")
    with monkeypatch.context() as m:
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:11])
        m.setattr(db, "SCHEMA_VERSION", 11)
        db.migrate(conn)
    conn.execute("INSERT INTO crates (id, name, kind, position, spotify_playlist_id) VALUES (1, 'Gig', 'spotify', 0, 'p'), (2, 'Mine', 'crate', 1, NULL)")
    conn.execute("INSERT INTO tracks (id, title, spotify_id) VALUES (1, 'removed only', 's1'), (2, 'removed but kept', 's2'), (3, 'normal', 's3')")
    conn.execute("INSERT INTO crate_tracks (crate_id, track_id, position, removed_from_source) VALUES (1,1,0,1), (1,2,1,1), (2,2,0,0), (1,3,2,0)")
    db.migrate(conn)
    trashed = {r[0] for r in conn.execute("SELECT id FROM tracks WHERE trashed_at IS NOT NULL")}
    assert trashed == {1}
    reason = db.track_to_dict(conn.execute("SELECT * FROM tracks WHERE id = 1").fetchone())["trash"]
    assert reason["kind"] == "spotify" and reason["crate"] == "Gig" and reason["crate_id"] == 1


def test_song_back_on_any_playlist_leaves_the_trash(lib):
    """In Spotify crates A and B: removed from A (kept, B has it), removed from B (trashed for B), back on A -> restored."""
    from djcrate import trash
    conn = lib.conn()
    conn.execute("INSERT INTO crates (id, name, kind, position) VALUES (1, 'A', 'spotify', 0), (2, 'B', 'spotify', 1)")
    s = conn.execute("INSERT INTO tracks (title) VALUES ('x')").lastrowid
    conn.execute("INSERT INTO crate_tracks (crate_id, track_id, position, removed_from_source) VALUES (1, ?, 0, 1), (2, ?, 0, 1)", (s, s))
    trash.trash(conn, trash.orphans(conn, [s], [2]), {"kind": "spotify", "crate": "B", "crate_id": 2})
    assert conn.execute("SELECT trashed_at FROM tracks WHERE id = ?", (s,)).fetchone()[0]
    conn.execute("UPDATE crate_tracks SET removed_from_source = 0 WHERE crate_id = 1")
    assert trash.after_spotify_return(conn, 1, [s]) == 1


def test_not_in_a_crate_lists_loose_songs(client, lib):
    loose, kept, removed = add(lib, "Loose"), add(lib, "Kept"), add(lib, "Removed on Spotify")
    crate = client.post("/api/crates", json={"name": "C", "kind": "crate"}).json()["id"]
    client.post(f"/api/crates/{crate}/tracks", json={"ids": [kept, removed]})
    lib.conn().execute("UPDATE crate_tracks SET removed_from_source = 1 WHERE track_id = ?", (removed,))
    assert ids(client, "smart:no_crate") == {loose, removed}
    assert counts(client)["no_crate"] == 2
    client.post("/api/tracks/trash", json={"ids": [loose, removed]})
    assert not ids(client, "smart:no_crate") and ids(client, "smart:all") == {kept}


def test_library_picker_count_skips_trash_and_hidden(lib):
    from djcrate import libraries
    a, b, c = add(lib, "One"), add(lib, "Two"), add(lib, "Three")
    conn = lib.conn()
    conn.execute("UPDATE tracks SET trashed_at = 1 WHERE id = ?", (b,))
    conn.execute("UPDATE tracks SET hidden_at = 1 WHERE id = ?", (c,))
    conn.close()
    assert libraries._peek(lib.library)[1] == 1
