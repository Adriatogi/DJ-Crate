"""Auto-fill links: the top fully green Find result, or leave the song for review."""

from djcrate import autofill, db, yt_search
from djcrate.autofill import AutoFiller

GREEN = {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "close": True, "flags": []}
GREEN2 = {"url": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "close": True, "flags": []}
FLAGGED = {"url": "https://www.youtube.com/watch?v=ccccccccccc", "close": True, "flags": ["Says “Remix”"]}
OFF = {"url": "https://www.youtube.com/watch?v=ddddddddddd", "close": False, "flags": []}


def test_pick_takes_the_top_fully_green_result():
    assert autofill.pick([OFF, FLAGGED, GREEN, GREEN2]) == GREEN
    assert autofill.pick([OFF, FLAGGED]) is None and autofill.pick([]) is None


def add(lib, title, url=None, status="none"):
    conn = lib.conn()
    try:
        return conn.execute("INSERT INTO tracks (title, artists, source_url, download_status, duration_ms) VALUES (?, 'A', ?, ?, 180000)",
                            (title, url, status)).lastrowid
    finally:
        conn.close()


def run_all(f):
    while f.step():
        pass


def test_fills_green_leaves_unsure_and_never_overwrites(client, lib, monkeypatch):
    monkeypatch.setattr(autofill, "GAP_S", 0)
    sure, unsure, linked = add(lib, "Sure"), add(lib, "Unsure"), add(lib, "Has link", url="https://youtu.be/mine0000000")
    answers = {"Sure": [OFF, GREEN, GREEN2], "Unsure": [FLAGGED, OFF], "Has link": [GREEN]}
    monkeypatch.setattr(yt_search, "search", lambda track, conn=None, refresh=False: {"results": answers[track["title"]]})
    woke = []
    f = AutoFiller(lib, on_fill=lambda: woke.append(1))
    monkeypatch.setattr("djcrate.api.auto_filler", f)
    st = client.post("/api/autofill", json={"view": "smart:needs_link"}).json()
    assert st["running"] and st["total"] == 2                     # only songs that need a link
    run_all(f)
    st = f.status()
    assert (st["running"], st["done"], st["filled"], st["unsure"]) == (False, 2, 1, 1)
    t = {x["id"]: x for x in client.get("/api/tracks").json()["tracks"]}
    assert t[sure]["source_url"] == GREEN["url"] and t[sure]["link_auto"] == 1
    assert not t[unsure]["source_url"]                            # stays for you
    assert t[linked]["source_url"] == "https://youtu.be/mine0000000" and not t[linked]["link_auto"]
    assert woke == [1]
    assert {x["id"] for x in client.get("/api/tracks?view=smart:needs_link").json()["tracks"]} == {unsure}
    # Editing the link yourself drops the Auto marker.
    client.patch(f"/api/tracks/{sure}", json={"source_url": GREEN2["url"]})
    assert client.get(f"/api/tracks/{sure}").json()["track"]["link_auto"] == 0


def test_rate_limit_pauses_and_retries_the_same_song(lib, monkeypatch):
    monkeypatch.setattr(autofill, "GAP_S", 0)
    tid = add(lib, "Song")
    calls = []

    def search(track, conn=None, refresh=False):
        calls.append(track["id"])
        if len(calls) == 1:
            raise yt_search.RateLimited("rate_limited", "slow down", 429)
        return {"results": [GREEN]}

    monkeypatch.setattr(yt_search, "search", search)
    f = AutoFiller(lib)
    f.begin([tid], "smart:needs_link")
    assert f.step() is False and f.status()["paused_until"]       # paused, song still queued
    f.paused_until = 0
    run_all(f)
    assert calls == [tid, tid] and f.status()["filled"] == 1


def test_busy_and_stop(lib, monkeypatch):
    f = AutoFiller(lib)
    a, b = add(lib, "A"), add(lib, "B")
    f.begin([a, b], "smart:needs_link")
    try:
        f.begin([a], "smart:needs_link")
        assert False, "should refuse a second run"
    except Exception as e:
        assert getattr(e, "code", "") == "autofill_busy"
    st = f.stop()
    assert not st["running"] and st["stopped"] and f.step() is False


def test_live_performances_are_not_green():
    assert yt_search.mix_flags("We Outside", "Flagboy Giz - We Outside | A COLORS SHOW")
    assert yt_search.mix_flags("Song", "Artist: Tiny Desk Concert")
    assert not yt_search.mix_flags("Glide", "Hoodboi - Glide feat. Tkay Maidza (Official Audio)")


def test_official_upload_wins_when_several_are_green():
    track = {"artists": "03 Greedo, Maxo Kream", "title": "Wit the Plug"}
    fan = {"url": "u1", "close": True, "flags": [], "title": "03 Greedo - Wit The Plug", "channel": "RapFan"}
    official_title = {"url": "u2", "close": True, "flags": [], "title": "Wit the Plug (Official Audio)", "channel": "Some Label"}
    own = {"url": "u3", "close": True, "flags": [], "title": "Wit the Plug", "channel": "03 Greedo"}
    topic = {"url": "u4", "close": True, "flags": [], "title": "Wit the Plug", "channel": "03 Greedo - Topic"}
    assert autofill.pick([fan, official_title, own, topic], track)["url"] == "u4"
    assert autofill.pick([fan, official_title, own], track)["url"] == "u3"
    assert autofill.pick([fan, official_title], track)["url"] == "u2"
    assert autofill.pick([fan], track)["url"] == "u1"            # a re-upload is fine when it's all there is


def test_same_length_is_not_enough():
    t = {"artists": "Draft Day", "title": "LUV & DRUGS", "duration_ms": 145000}
    raw = [{"url": "a", "title": "GIFT & CURSE", "channel": "Draft Day", "duration_ms": 147000},
           {"url": "b", "title": "Draft Day - LUV & DRUGS (Official Video)", "channel": "Draft Day", "duration_ms": 146000},
           {"url": "c", "title": "LUV & DRUGS", "channel": "", "duration_ms": 145000},
           {"url": "d", "title": "Helluva Price ( Dance Video )", "channel": "x", "duration_ms": 145000}]
    res = yt_search._annotate(t, raw)
    assert [bool(r["flags"]) for r in res] == [True, False, True, True]  # other song / ok / no artist / dance edit
    assert autofill.pick(res, t)["url"] == "b"


def test_undo_takes_back_auto_links_only(client, lib):
    auto, mine, done = add(lib, "Auto"), add(lib, "Mine", url="https://youtu.be/mine0000000"), add(lib, "Done", status="done")
    conn = lib.conn()
    conn.execute("UPDATE tracks SET source_url = 'https://youtu.be/auto0000000', link_auto = 1 WHERE id IN (?, ?)", (auto, done))
    conn.close()
    assert client.post("/api/autofill/undo", json={"view": "smart:all"}).json()["undone"] == 1
    t = {x["id"]: x for x in client.get("/api/tracks").json()["tracks"]}
    assert not t[auto]["source_url"] and not t[auto]["link_auto"]
    assert t[mine]["source_url"] == "https://youtu.be/mine0000000"
    assert t[done]["source_url"] == "https://youtu.be/auto0000000"     # downloaded: never touched
    assert auto in {x["id"] for x in client.get("/api/tracks?view=smart:needs_link").json()["tracks"]}
