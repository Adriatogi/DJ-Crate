def mk(client, title, artists="A"):
    return client.post("/api/tracks", json={"title": title, "artists": artists}).json()["track"]["id"]


def test_mix_crud_direction_and_undo(client):
    a, b, c = mk(client, "Shangri-La"), mk(client, "Elevators", "OutKast"), mk(client, "Acapella")
    assert client.post("/api/mixes", json={"in_ids": [a], "out_ids": [], "note": "x"}).status_code == 400
    assert client.post("/api/mixes", json={"in_ids": [a], "out_ids": [a], "note": "x"}).status_code == 400
    m = client.post("/api/mixes", json={"in_ids": [a, c], "out_ids": [b], "note": "loop x8 at end, reverb, slow down"}).json()
    assert [s["track_id"] for s in m["ins"]] == [a, c] and m["outs"][0]["song"]["title"] == "Elevators"
    assert [x["id"] for x in client.get(f"/api/mixes/from/{a}").json()["mixes"]] == [m["id"]]
    assert client.get(f"/api/mixes/from/{b}").json()["mixes"] == []  # one-way
    client.patch(f"/api/mixes/{m['id']}", json={"note": "new note"})
    snap = client.delete(f"/api/mixes/{m['id']}").json()["snapshot"]
    assert client.get(f"/api/mixes/from/{a}").json()["mixes"] == []
    back = client.post("/api/mixes/restore", json={"snapshot": snap}).json()
    assert back["note"] == "new note" and len(back["ins"]) == 2


def test_board_depth_columns_and_samples(client):
    a, b, c, d, s = (mk(client, n) for n in ["Center", "Next", "Prev", "Next2", "Sampled"])
    client.post("/api/mixes", json={"in_ids": [a], "out_ids": [b], "note": "a→b"})
    client.post("/api/mixes", json={"in_ids": [c], "out_ids": [a], "note": "c→a"})
    client.post("/api/mixes", json={"in_ids": [b], "out_ids": [d], "note": "b→d"})
    client.post(f"/api/tracks/{a}/samples", json={"sample_track_id": s, "text": "drum break"})
    client.post(f"/api/tracks/{a}/samples", json={"text": "vocal from an old soul record"})
    one = client.get(f"/api/cutting/board/{a}?depth=1").json()
    cols = {x["title"]: (x["hop"], x["col"]) for x in one["songs"]}
    assert cols == {"Center": (0, 0), "Next": (1, 1), "Prev": (1, -1), "Sampled": (1, 0)}
    assert sorted(m["note"] for m in one["mixes"]) == ["a→b", "c→a"]  # b→d is beyond depth 1
    assert len(one["samples"]) == 1 and len(one["center"]["samples"]) == 2
    two = client.get(f"/api/cutting/board/{a}?depth=2").json()
    assert {x["title"]: x["col"] for x in two["songs"]}["Next2"] == 2 and len(two["mixes"]) == 3
    sampled = client.get(f"/api/cutting/board/{s}").json()["center"]
    assert sampled["sampled_by"][0]["song"]["title"] == "Center"
    client.put(f"/api/cutting/board/{a}/layout", json={"positions": [{"kind": "song", "id": b, "x": 10, "y": 20}]})
    assert client.get(f"/api/cutting/board/{a}").json()["layout"][f"song:{b}"] == {"x": 10.0, "y": 20.0}


def test_labels_survive_track_delete(client, monkeypatch):
    monkeypatch.setattr("djcrate.library.send2trash", lambda p: None)
    a, b = mk(client, "Keep"), mk(client, "Gone", "X")
    m = client.post("/api/mixes", json={"in_ids": [a], "out_ids": [b], "note": "n"}).json()
    client.post(f"/api/tracks/{a}/samples", json={"sample_track_id": b})
    client.post("/api/tracks/delete", json={"ids": [b]})
    mix = client.get(f"/api/mixes/from/{a}").json()["mixes"][0]
    assert mix["outs"][0]["track_id"] is None and mix["outs"][0]["label"] == "Gone — X"
    sample = client.get(f"/api/cutting/board/{a}").json()["center"]["samples"][0]
    assert sample["sample_track_id"] is None and sample["sample_label"] == "Gone — X"


def test_cutting_song_list_counts(client):
    a, b = mk(client, "A"), mk(client, "B")
    mk(client, "Lonely")
    client.post("/api/mixes", json={"in_ids": [a], "out_ids": [b], "note": "n"})
    songs = {s["title"]: s for s in client.get("/api/cutting/songs").json()["songs"]}
    assert songs["A"]["mixes"] == 1 and songs["Lonely"]["mixes"] == 0
    assert [s["title"] for s in client.get("/api/cutting/songs?connected=1").json()["songs"]] == ["A", "B"]
