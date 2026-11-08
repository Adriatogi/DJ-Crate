from djcrate import db, setboard


def mk(client, lib, title, artists="A", ms=180000):
    t = client.post("/api/tracks", json={"title": title, "artists": artists}).json()["track"]
    lib.conn().execute("UPDATE tracks SET duration_ms = ? WHERE id = ?", (ms, t["id"]))
    return t["id"]


def board(client, sid, nodes, edges):
    r = client.put(f"/api/sets/{sid}/board", json={"nodes": nodes, "edges": edges})
    assert r.status_code == 200, r.text
    return r.json()


def test_main_path_branches_times_and_export(client, lib):
    a, b, c, d = (mk(client, lib, n, ms=ms) for n, ms in [("A", 183000), ("B", 120000), ("C", 200000), ("D", 90000)])
    sid = client.post("/api/sets", json={"name": "Friday"}).json()["id"]
    data = board(client, sid, [
        {"id": -1, "kind": "song", "track_id": a, "x": 0, "length_ms": 119000, "cue_time": "drop 1:04"},
        {"id": -2, "kind": "mix", "text": "loop x8 + reverb", "x": 300},
        {"id": -3, "kind": "song", "track_id": b, "x": 600},
        {"id": -4, "kind": "text", "text": "mic break", "x": 900},
        {"id": -5, "kind": "song", "track_id": c, "x": 1200},
        {"id": -6, "kind": "mix", "text": "if they're hype", "x": 900, "y": 300},
        {"id": -7, "kind": "song", "track_id": d, "x": 1200, "y": 300},
    ], [
        {"from_node": -1, "to_node": -2, "main": True}, {"from_node": -2, "to_node": -3, "main": True},
        {"from_node": -3, "to_node": -4, "main": True}, {"from_node": -4, "to_node": -5, "main": True},
        {"from_node": -3, "to_node": -6}, {"from_node": -6, "to_node": -7},
    ])
    path = data["main_path"]
    assert [r["title"] or r["text"] for r in path] == ["A", "B", "mic break", "C"]
    assert path[0]["transition"]["text"] == "loop x8 + reverb" and path[0]["played_ms"] == 119000
    assert [r["start_ms"] for r in path if r["kind"] == "song"] == [0, 119000, 239000]
    assert data["total_ms"] == 119000 + 120000 + 200000
    text = client.get(f"/api/sets/{sid}/export").json()["text"]
    assert "plays 1:59 of 3:03" in text and "Transition: loop x8 + reverb" in text and "— mic break —" in text
    assert "ALTERNATIVES" in text and "From B:" in text and "D" in text and "if they're hype" in text


def test_ids_are_stable_and_list_edits_write_to_nodes(client, lib):
    a, b = mk(client, lib, "A"), mk(client, lib, "B")
    sid = client.post("/api/sets", json={"name": "S"}).json()["id"]
    data = board(client, sid, [{"id": -1, "kind": "song", "track_id": a}, {"id": -2, "kind": "song", "track_id": b, "x": 300}],
                 [{"from_node": -1, "to_node": -2, "main": True}])
    first = data["idmap"]["-1"]
    again = board(client, sid, data["nodes"], data["edges"])
    assert again["idmap"][str(first)] == first  # saving again keeps ids
    r = client.patch(f"/api/sets/{sid}/nodes/{first}", json={"cue_time": "1:04", "length_ms": 60000}).json()
    assert r["main_path"][0]["cue_time"] == "1:04" and r["main_path"][0]["played_ms"] == 60000
    r = client.post(f"/api/sets/{sid}/transition", json={"after_node": first, "text": "echo out"}).json()
    assert r["main_path"][0]["transition"]["text"] == "echo out" and [x["title"] for x in r["main_path"]] == ["A", "B"]


def test_save_mix_links_and_edits_update_everywhere(client, lib):
    a, b = mk(client, lib, "Shangri-La"), mk(client, lib, "Elevators")
    sid = client.post("/api/sets", json={"name": "S"}).json()["id"]
    data = board(client, sid, [{"id": -1, "kind": "song", "track_id": a}, {"id": -2, "kind": "mix", "text": "loop x8"},
                               {"id": -3, "kind": "song", "track_id": b}],
                 [{"from_node": -1, "to_node": -2, "main": True}, {"from_node": -2, "to_node": -3, "main": True}])
    mix_node = data["idmap"]["-2"]
    data = client.post(f"/api/sets/{sid}/nodes/{mix_node}/save_mix").json()
    node = next(n for n in data["nodes"] if n["id"] == mix_node)
    assert node["saved"] and node["mix_id"]
    client.patch(f"/api/sets/{sid}/nodes/{mix_node}", json={"text": "loop x8 with reverb"})
    assert client.get(f"/api/mixes/from/{a}").json()["mixes"][0]["note"] == "loop x8 with reverb"
    # list view: a direct song→song pair with a saved mix gets ✓
    sid2 = client.post("/api/sets", json={"name": "T"}).json()["id"]
    d2 = board(client, sid2, [{"id": -1, "kind": "song", "track_id": a}, {"id": -2, "kind": "song", "track_id": b}],
               [{"from_node": -1, "to_node": -2, "main": True}])
    assert d2["main_path"][0]["saved_mix_id"] == node["mix_id"]


def test_delete_set_undo_and_deleted_song_keeps_label(client, lib, monkeypatch):
    monkeypatch.setattr("djcrate.library.send2trash", lambda p: None)
    a = mk(client, lib, "Gone", "X")
    sid = client.post("/api/sets", json={"name": "S"}).json()["id"]
    board(client, sid, [{"id": -1, "kind": "song", "track_id": a}], [])
    client.post("/api/tracks/delete", json={"ids": [a]})
    row = client.get(f"/api/sets/{sid}").json()["main_path"][0]
    assert row["track_id"] is None and row["label"] == "Gone — X"
    snap = client.delete(f"/api/sets/{sid}").json()["snapshot"]
    new_id = client.post("/api/sets/restore", json={"snapshot": snap}).json()["id"]
    assert client.get(f"/api/sets/{new_id}").json()["main_path"][0]["label"] == "Gone — X"


def test_v3_set_items_convert_to_board(tmp_path):
    import sqlite3
    from djcrate.state import AppState
    lib = tmp_path / "Old"
    (lib / ".djcrate").mkdir(parents=True)
    conn = sqlite3.connect(lib / ".djcrate" / "djcrate.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for migration in db.MIGRATIONS[:3]:
        for stmt in db._statements(migration):
            conn.execute(stmt)
    conn.execute("INSERT INTO schema_version VALUES (3)")
    conn.execute("INSERT INTO tracks (id, title, artists, duration_ms) VALUES (1, 'A', 'x', 183000), (2, 'B', 'y', 100000)")
    conn.execute("INSERT INTO sets (id, name) VALUES (1, 'Old set')")
    conn.executemany("INSERT INTO set_items (set_id, position, track_id, label, free_text, cue_time, transition_note, length_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (1, 0, 1, "A — x", None, "1:04", "loop out", 119000),
        (1, 1, None, "", "mic break", "", "", None),
        (1, 2, 2, "B — y", None, "", "", None),
    ])
    conn.commit()
    conn.close()
    st = AppState()
    st.open(lib)
    assert st.status == "ok", st.error
    data = setboard.get_board(st.conn(), 1)
    path = data["main_path"]
    assert [r["title"] or r["text"] for r in path] == ["A", "mic break", "B"]
    assert path[0]["cue_time"] == "1:04" and path[0]["played_ms"] == 119000 and path[0]["transition"]["text"] == "loop out"
    st.release_lock()
