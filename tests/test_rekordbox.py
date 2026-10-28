import json
import os
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from djcrate import db, rekordbox


def test_to_camelot_all_24_names_and_forms():
    for code, name in rekordbox.CAMELOT.items():
        assert rekordbox.to_camelot(name) == code
        assert rekordbox.to_camelot(code) == code
    assert rekordbox.to_camelot("08A") == "8A"
    assert rekordbox.to_camelot("A#m") == "3A" == rekordbox.to_camelot("Bbm")
    assert rekordbox.to_camelot("F♯") == "2B"
    assert rekordbox.to_camelot("Cmaj") == "8B" and rekordbox.to_camelot("Amin") == "8A"
    assert rekordbox.to_camelot("C#") == "3B" and rekordbox.to_camelot("G#m") == "1A"
    for junk in ("", None, "13A", "0A", "H", "xyz", "8C"):
        assert rekordbox.to_camelot(junk) is None


def test_location_roundtrip():
    for name in ["a b.mp3", "x#y&z.mp3", "Beyoncé – Halo (100%).mp3", "q?=+.mp3"]:
        p = f"/Users/dj/My Music/{name}"
        assert rekordbox.location_to_path(rekordbox.path_to_location(p)) == p
    assert rekordbox.location_to_path("http://x/y.mp3") is None


def add_song(lib, title, rel=None, status="done", notes="", artists="Artist"):
    rel = rel or f"{title}.mp3"
    p = lib.library / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    conn = lib.conn()
    try:
        cur = conn.execute(
            "INSERT INTO tracks (title, artists, file_rel_path, download_status, notes, duration_ms) VALUES (?,?,?,?,?,180000)",
            (title, artists, rel, status, notes))
        return cur.lastrowid
    finally:
        conn.close()


def write_xml(path, tracks):
    body = "".join(
        f'<TRACK TrackID="{i}" Name="n" Location="{rekordbox.path_to_location(loc)}" AverageBpm="{bpm}" Tonality="{key}"/>'
        for i, (loc, bpm, key) in enumerate(tracks, 1))
    Path(path).write_text(f'<?xml version="1.0"?><DJ_PLAYLISTS><COLLECTION Entries="{len(tracks)}">{body}</COLLECTION>'
                          '<PLAYLISTS><NODE Type="0" Name="ROOT"><NODE Type="1" Name="p" KeyType="0"><TRACK Key="1"/></NODE></NODE></PLAYLISTS>'
                          '</DJ_PLAYLISTS>', encoding="utf-8")


def row(lib, tid):
    conn = lib.conn()
    try:
        return conn.execute("SELECT bpm, musical_key FROM tracks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()


def test_import_matches_paths_and_leaves_others(lib, tmp_path):
    a = add_song(lib, "Space Song")
    nfd = unicodedata.normalize("NFD", "Café Del Mar.mp3")
    b = add_song(lib, "Café", rel=unicodedata.normalize("NFC", "Café Del Mar.mp3"))
    c = add_song(lib, "Untouched")
    d = add_song(lib, "Zero")
    conn = lib.conn()
    conn.execute("UPDATE tracks SET bpm = 99, musical_key = '1A' WHERE id = ?", (c,))
    xml = tmp_path / "rb.xml"
    write_xml(xml, [(lib.library / "Space Song.mp3", "93.50", "Am"), (lib.library / nfd, "128.00", "8B"),
                    (lib.library / "Zero.mp3", "0.00", "Fm"), ("/elsewhere/not-mine.mp3", "120", "Cm")])
    s = rekordbox.import_xml(conn, xml)
    assert s == {"matched": 3, "total": 4, "in_xml": 4}
    assert tuple(row(lib, a)) == (93.5, "8A") and tuple(row(lib, b)) == (128.0, "8B")
    assert tuple(row(lib, c)) == (99, "1A")  # not in the XML
    assert tuple(row(lib, d)) == (None, "4A")  # BPM 0.00 means unknown


def test_import_moved_library_tail_match(lib, tmp_path):
    a = add_song(lib, "Moved", rel="Artist/Moved.mp3")
    xml = tmp_path / "rb.xml"
    write_xml(xml, [("/Old Mac/Music/Artist/Moved.mp3", "100", "Dm")])
    rekordbox.import_xml(lib.conn(), xml)
    assert tuple(row(lib, a)) == (100.0, "7A")


def test_check_once_mtime_and_broken_xml(lib, tmp_path):
    a = add_song(lib, "Song")
    xml = tmp_path / "rb.xml"
    write_xml(xml, [(lib.library / "Song.mp3", "100", "Am")])
    conn = lib.conn()
    db.set_setting(conn, "rekordbox_xml", str(xml))
    rekordbox.check_once(lib)
    assert row(lib, a)["bpm"] == 100
    conn.execute("UPDATE tracks SET bpm = 1 WHERE id = ?", (a,))
    rekordbox.check_once(lib)  # same mtime: nothing happens
    assert row(lib, a)["bpm"] == 1
    write_xml(xml, [(lib.library / "Song.mp3", "101", "Am")])
    os.utime(xml, (xml.stat().st_atime, xml.stat().st_mtime + 5))
    rekordbox.check_once(lib)
    assert row(lib, a)["bpm"] == 101
    saved = db.get_setting(conn, "rekordbox_xml_mtime")
    xml.write_text("<DJ_PLAYLISTS><COLLECTION>")  # half-written
    os.utime(xml, (xml.stat().st_atime, xml.stat().st_mtime + 5))
    rekordbox.check_once(lib)
    assert db.get_setting(conn, "rekordbox_xml_mtime") == saved  # will retry
    assert "couldn't be read" in json.loads(db.get_setting(conn, "rekordbox_import"))["error"]


def test_api_xml_flow(client, lib, tmp_path):
    a = add_song(lib, "Song")
    assert client.put("/api/rekordbox/xml", json={"path": str(tmp_path / "nope.xml")}).status_code == 400
    bad = tmp_path / "bad.xml"
    bad.write_text("not xml")
    assert client.put("/api/rekordbox/xml", json={"path": str(bad)}).status_code == 400
    xml = tmp_path / "rb.xml"
    write_xml(xml, [(lib.library / "Song.mp3", "90", "Gm")])
    r = client.put("/api/rekordbox/xml", json={"path": str(xml)}).json()
    assert r["matched"] == 1 and r["path"] == str(xml)
    assert client.get("/api/status").json()["rekordbox"]["matched"] == 1
    t = next(x for x in client.get("/api/tracks?crate=smart:all").json()["tracks"] if x["id"] == a)
    assert t["bpm"] == 90 and t["musical_key"] == "6A"
    client.delete("/api/rekordbox/xml")
    assert client.get("/api/status").json()["rekordbox"]["path"] is None
    assert row(lib, a)["bpm"] == 90  # values stay
    assert client.post("/api/rekordbox/import").status_code == 400


def test_migration_v6_to_v7_keeps_tracks(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    db.migrate(conn)
    conn.execute("ALTER TABLE tracks DROP COLUMN bpm")
    conn.execute("ALTER TABLE tracks DROP COLUMN musical_key")
    conn.execute("UPDATE schema_version SET version = 6")
    conn.execute("INSERT INTO tracks (title) VALUES ('keep me')")
    db.migrate(conn)
    assert tuple(conn.execute("SELECT title, bpm, musical_key FROM tracks").fetchone()) == ("keep me", None, None)


# ------------------------------------------------------------ export

def build(lib):
    conn = lib.conn()
    try:
        data, summary = rekordbox.build_export(lib, conn)
    finally:
        conn.close()
    return ET.fromstring(data), summary


def test_export_structure(client, lib):
    a = add_song(lib, "Alpha", notes="loop me", artists="X")
    b = add_song(lib, "Bravo & Co #1", rel="sub dir/Bravo & Co #1.mp3")
    c = add_song(lib, "Charlie")
    pending = add_song(lib, "Pending", status="none")
    removed = add_song(lib, "Removed")
    conn = lib.conn()
    conn.execute("UPDATE tracks SET bpm = 120, musical_key = '8A' WHERE id = ?", (a,))
    folder = client.post("/api/crates", json={"name": "House", "kind": "folder"}).json()["id"]
    warm = client.post("/api/crates", json={"name": "Warmup", "parent_id": folder}).json()["id"]
    peak = client.post("/api/crates", json={"name": "Peak"}).json()["id"]
    client.post(f"/api/crates/{warm}/tracks", json={"ids": [b, a, pending]})
    client.post(f"/api/crates/{peak}/tracks", json={"ids": [c, removed]})
    conn.execute("UPDATE crate_tracks SET removed_from_source = 1 WHERE track_id = ?", (removed,))
    sid = client.post("/api/sets", json={"name": "Friday"}).json()["id"]
    client.put(f"/api/sets/{sid}/board", json={"nodes": [
        {"id": -1, "kind": "song", "track_id": c, "x": 0}, {"id": -2, "kind": "text", "text": "mic", "x": 300},
        {"id": -3, "kind": "song", "track_id": a, "x": 600}, {"id": -4, "kind": "song", "track_id": b, "x": 600, "y": 300},
    ], "edges": [{"from_node": -1, "to_node": -2, "main": True}, {"from_node": -2, "to_node": -3, "main": True},
                 {"from_node": -2, "to_node": -4}]})

    root, summary = build(lib)
    assert summary == {"playlists": 3, "songs": 4, "skipped": 1}  # Pending has no file yet
    tracks = {t.get("Name"): t for t in root.find("COLLECTION")}
    assert set(tracks) == {"Alpha", "Bravo & Co #1", "Charlie", "Removed"}
    assert tracks["Alpha"].get("Comments") == "loop me" and tracks["Alpha"].get("Kind") == "MP3 File"
    for t in tracks.values():  # never BPM, key, grids or cues
        assert not ({"AverageBpm", "Tonality"} & set(t.attrib))
    assert root.find(".//TEMPO") is None and root.find(".//POSITION_MARK") is None
    assert rekordbox.location_to_path(tracks["Bravo & Co #1"].get("Location")) == str(lib.library / "sub dir/Bravo & Co #1.mp3")
    name = {t.get("TrackID"): t.get("Name") for t in tracks.values()}

    top = root.find("PLAYLISTS/NODE/NODE")
    assert top.get("Name") == "DJ Crate"
    crates, sets = list(top)
    house, peak_n = list(crates)
    assert house.get("Type") == "0" and house[0].get("Name") == "Warmup"
    assert [name[t.get("Key")] for t in house[0]] == ["Bravo & Co #1", "Alpha"]  # crate order, Pending left out
    assert [name[t.get("Key")] for t in peak_n] == ["Charlie"]
    assert [name[t.get("Key")] for t in sets[0]] == ["Charlie", "Alpha"]  # main path only, no text, no branch


def test_write_export_only_when_changed(client, lib):
    add_song(lib, "Alpha")
    conn = lib.conn()
    s = rekordbox.write_export(lib, conn)
    p = rekordbox.export_path(lib)
    assert p.exists() and s["songs"] == 1 and s["error"] is None
    os.utime(p, (1_000_000, 1_000_000))
    rekordbox.write_export(lib, conn)
    assert p.stat().st_mtime == 1_000_000  # unchanged: not rewritten
    client.post("/api/crates", json={"name": "New"})
    rekordbox.write_export(lib, conn)
    assert p.stat().st_mtime != 1_000_000


def test_export_toggle_api_and_watcher(client, lib):
    add_song(lib, "Alpha")
    p = rekordbox.export_path(lib)
    assert not p.exists()
    r = client.put("/api/rekordbox/export", json={"on": True}).json()["export"]
    assert r["on"] and r["songs"] == 1 and p.exists()
    p.unlink()
    rekordbox.check_once(lib)  # watcher restores it
    assert p.exists()
    assert client.put("/api/rekordbox/export", json={"on": False}).json()["export"]["on"] is False
    p.unlink()
    rekordbox.check_once(lib)
    assert not p.exists()


def test_pick_endpoint_uses_finder_picker(client, monkeypatch):
    from djcrate import tools
    seen = {}
    monkeypatch.setattr(tools, "pick_path", lambda kind, ext: seen.update(kind=kind, ext=ext) or "/x/rb.xml")
    assert client.post("/api/pick", json={"kind": "file", "extensions": ["xml", "a;b"]}).json() == {"path": "/x/rb.xml"}
    assert seen == {"kind": "file", "ext": ("xml",)}
