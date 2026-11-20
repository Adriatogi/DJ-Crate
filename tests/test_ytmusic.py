"""Clean-version check: YouTube Music's explicit badge (responses shaped like the real ones)."""

import json
import subprocess

import pytest

from djcrate import cleancheck, db, yt_search, ytmusic

CLEAN, EXPL, OTHER = "UE10URP4d5I", "v54io9f0Wfk", "C4Fc4WJv5J0"
E_BADGE = [{"musicInlineBadgeRenderer": {"icon": {"iconType": "MUSIC_EXPLICIT_BADGE"}}}]


def nav(page):
    return {"browseEndpoint": {"browseEndpointContextSupportedConfigs": {"browseEndpointContextMusicConfig": {"pageType": page}}}}


def byline(artist, album=None, tail=None):
    runs = [{"text": artist, "navigationEndpoint": nav("MUSIC_PAGE_TYPE_ARTIST" if album else "MUSIC_PAGE_TYPE_USER_CHANNEL")}]
    if album:
        runs += [{"text": " • "}, {"text": album, "navigationEndpoint": nav("MUSIC_PAGE_TYPE_ALBUM")}]
    if tail:
        runs += [{"text": " • "}, {"text": tail}]
    return {"runs": runs}


def next_resp(vid, title, artist, album, length, explicit):
    panel = {"videoId": vid, "title": {"runs": [{"text": title}]}, "longBylineText": byline(artist, album, "2015"),
             "lengthText": {"runs": [{"text": length}]}}
    if explicit:
        panel["badges"] = E_BADGE
    return {"contents": {"x": [{"playlistPanelVideoRenderer": panel}]}}


def song_row(vid, title, artist, album, length, explicit):
    item = {"playlistItemData": {"videoId": vid}, "flexColumns": [
        {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": title}]}}},
        {"musicResponsiveListItemFlexColumnRenderer": {"text": byline(artist, album, length)}},
    ]}
    if explicit:
        item["badges"] = E_BADGE
    return {"musicResponsiveListItemRenderer": item}


VIDEOS = {
    CLEAN: next_resp(CLEAN, "The Percocet & Stripper Joint", "Future", "DS2 (Deluxe)", "2:30", False),
    EXPL: next_resp(EXPL, "The Percocet & Stripper Joint", "Future", "DS2 (Deluxe)", "2:29", True),
    OTHER: next_resp(OTHER, "Some Upload", "SomeChannel", None, "3:10", False),
}
SEARCH = {"contents": [
    song_row(EXPL, "The Percocet & Stripper Joint", "Future", "DS2 (Deluxe)", "2:29", True),
    song_row(CLEAN, "The Percocet & Stripper Joint", "Future", "DS2 (Deluxe)", "2:30", False),
    song_row("zzzzzzzzzzz", "News or Something", "Future", "Purple Reign", "3:34", True),
]}


@pytest.fixture
def ytm(monkeypatch):
    calls = []

    def fake(self, endpoint, body):
        calls.append(endpoint)
        if endpoint == "next":
            return VIDEOS.get(body["videoId"], {})
        return SEARCH
    monkeypatch.setattr(ytmusic.Client, "post", fake)
    return calls


def test_video_id_parsing():
    assert ytmusic.video_id(f"https://www.youtube.com/watch?v={CLEAN}&list=RD{CLEAN}&start_radio=1") == CLEAN
    assert ytmusic.video_id(f"https://youtu.be/{CLEAN}?t=3") == CLEAN
    assert ytmusic.video_id(f"https://music.youtube.com/watch?v={CLEAN}") == CLEAN
    assert ytmusic.video_id(f"https://www.youtube.com/shorts/{CLEAN}") == CLEAN
    assert ytmusic.video_id("https://soundcloud.com/a/b") is None
    assert ytmusic.video_id("https://www.youtube.com/watch?v=short") is None


def test_parse_video_and_search():
    v = ytmusic.parse_video(VIDEOS[EXPL], EXPL)
    assert v == {"video_id": EXPL, "title": "The Percocet & Stripper Joint", "artists": "Future", "album": "DS2 (Deluxe)",
                 "duration_ms": 149000, "explicit": True, "official": True}
    assert ytmusic.parse_video(VIDEOS[OTHER], OTHER)["official"] is False
    rows = ytmusic.parse_search(SEARCH)
    assert [(r["video_id"], r["explicit"], r["duration_ms"]) for r in rows] == [(EXPL, True, 149000), (CLEAN, False, 150000), ("zzzzzzzzzzz", True, 214000)]
    # Unexpected shapes never crash, they just find nothing.
    assert ytmusic.parse_video({"weird": [1, None, "x"]}, CLEAN) is None
    assert ytmusic.parse_search({"contents": [{"musicResponsiveListItemRenderer": {"flexColumns": None}}]}) == []


def test_check_results(ytm):
    url = lambda v: f"https://www.youtube.com/watch?v={v}"
    clean = ytmusic.check({"source_url": url(CLEAN), "title": "The Percocet & Stripper Joint", "artists": "Future"})
    assert clean["result"] == "clean" and clean["twin"]["video_id"] == EXPL and clean["twin"]["album"] == "DS2 (Deluxe)"
    assert ytmusic.check({"source_url": url(EXPL)})["result"] == "explicit"
    assert ytmusic.check({"source_url": url(OTHER)})["result"] == "unofficial"   # a user upload: never flagged
    assert ytmusic.check({"source_url": url("aaaaaaaaaaa")})["result"] == "unknown"
    assert ytmusic.check({"source_url": "https://soundcloud.com/x/y"})["result"] == "unknown"


def test_same_song_rules():
    a = {"title": "Good Life", "artists": "Kanye West, T-Pain"}
    assert ytmusic.same_song(a, {"title": "Good Life (feat. T-Pain)", "artists": "Kanye West", "duration_ms": 207000}, 207000)
    assert not ytmusic.same_song(a, {"title": "Good Life", "artists": "Kanye West", "duration_ms": 214000}, 207000)  # 7 s off
    assert not ytmusic.same_song(a, {"title": "Good Life Remix", "artists": "Kanye West", "duration_ms": 207000}, 207000)
    # Different artist spelling ("$uede" vs "MoneySign Suede") is OK only when the length is very close.
    b = {"title": "Back To The Bag", "artists": "MoneySign Suede"}
    assert ytmusic.same_song(b, {"title": "BACK TO THE BAG", "artists": "$uede", "duration_ms": 156000}, 155000)
    assert not ytmusic.same_song(b, {"title": "BACK TO THE BAG", "artists": "$uede", "duration_ms": 159000}, 155000)


def test_unreachable_raises(monkeypatch):
    with pytest.raises(ytmusic.YTMError):  # conftest keeps YouTube Music "offline"
        ytmusic.check({"source_url": f"https://youtu.be/{CLEAN}"})


def add(client, url, title="The Percocet & Stripper Joint"):
    return client.post("/api/tracks", json={"title": title, "artists": "Future", "source_url": url}).json()["track"]["id"]


def test_checker_flags_swap_and_keep(client, lib, ytm):
    tid = add(client, f"https://www.youtube.com/watch?v={CLEAN}&list=RD{CLEAN}")
    checker = cleancheck.Checker(lib)
    assert checker.step() is True and checker.last_flag["track_id"] == tid
    assert checker.step() is False  # nothing else due
    t = client.get(f"/api/tracks/{tid}").json()["track"]
    assert t["clean_flag"] and t["clean"]["twin"]["video_id"] == EXPL and "clean_check" not in t
    crate = client.get("/api/tracks?view=smart:clean_versions").json()["tracks"]
    assert [x["id"] for x in crate] == [tid]
    smart = {s["key"]: s["count"] for s in client.get("/api/sidebar").json()["smart"]}
    assert smart["clean_versions"] == 1

    # Keep: no longer flagged, undo brings it back.
    assert client.put(f"/api/tracks/{tid}/clean_keep", json={"keep": True}).json()["track"]["clean_flag"] is False
    assert client.get("/api/tracks?view=smart:clean_versions").json()["tracks"] == []
    assert client.put(f"/api/tracks/{tid}/clean_keep", json={"keep": False}).json()["track"]["clean_flag"] is True

    # Swap: the link becomes the explicit copy, recorded as explicit, nothing left to check.
    r = client.post("/api/tracks/swap_explicit", json={"ids": [tid]}).json()
    assert r["swapped"] == 1 and r["queued"] == 0  # not downloaded yet, so nothing to re-download
    t = r["tracks"][0]
    assert t["source_url"] == ytmusic.watch_url(EXPL) and t["clean"]["result"] == "explicit" and not t["clean_flag"]
    assert checker.step() is False
    assert client.post("/api/tracks/swap_explicit", json={"ids": [tid]}).json()["swapped"] == 0  # not flagged anymore


def test_swap_redownloads_downloaded_songs(client, lib, ytm):
    tid = add(client, f"https://youtu.be/{CLEAN}")
    lib.conn().execute("UPDATE tracks SET download_status = 'done', file_rel_path = 'x.mp3' WHERE id = ?", (tid,))
    cleancheck.Checker(lib).step()
    r = client.post("/api/tracks/swap_explicit", json={"ids": [tid]}).json()
    assert r["queued"] == 1
    job = lib.conn().execute("SELECT replace_file FROM jobs WHERE track_id = ?", (tid,)).fetchone()
    assert job["replace_file"] == 1


def test_link_change_makes_it_due_again_and_resets_keep(client, lib, ytm):
    tid = add(client, f"https://youtu.be/{CLEAN}")
    checker = cleancheck.Checker(lib)
    checker.step()
    client.put(f"/api/tracks/{tid}/clean_keep", json={"keep": True})
    client.patch(f"/api/tracks/{tid}", json={"source_url": f"https://www.youtube.com/watch?v={OTHER}"})
    t = client.get(f"/api/tracks/{tid}").json()["track"]
    assert t["clean"] is None and not t["clean_flag"]  # the old check was for the old link
    assert checker.step() is True
    t = client.get(f"/api/tracks/{tid}").json()["track"]
    assert t["clean"]["result"] == "unofficial" and t["clean_keep"] is False


def test_unreachable_pauses_checker(client, lib):
    add(client, f"https://youtu.be/{CLEAN}")
    checker = cleancheck.Checker(lib)
    assert checker.step() is False and checker.paused
    assert cleancheck.due_count(lib.conn()) == 1  # still due, tried again later


def test_turning_it_off(client, lib, ytm):
    add(client, f"https://youtu.be/{CLEAN}")
    s = client.put("/api/clean/check", json={"on": False}).json()
    assert s["on"] is False and s["pending"] == 1
    assert cleancheck.Checker(lib).step() is False
    assert client.get("/api/status").json()["clean"]["on"] is False


def test_check_now_route(client, ytm):
    tid = add(client, f"https://youtu.be/{CLEAN}")
    assert client.post(f"/api/tracks/{tid}/cleancheck").json()["track"]["clean_flag"] is True
    other = client.post("/api/tracks", json={"title": "X", "source_url": "https://soundcloud.com/a/b"}).json()["track"]["id"]
    assert client.post(f"/api/tracks/{other}/cleancheck").json()["error"]["code"] == "not_youtube"


def test_find_marks_clean_copies(client, ytm, monkeypatch):
    entries = {"entries": [{"id": CLEAN, "title": "The Percocet & Stripper Joint", "channel": "Future", "duration": 150},
                           {"id": EXPL, "title": "The Percocet & Stripper Joint", "channel": "Future", "duration": 149}]}
    monkeypatch.setattr(yt_search.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(entries), ""))
    tid = client.post("/api/tracks", json={"title": "The Percocet & Stripper Joint", "artists": "Future"}).json()["track"]["id"]
    res = client.get(f"/api/tracks/{tid}/yt_search").json()["results"]
    assert ytm == [] and len(res) == 2  # Find itself never waits for YouTube Music
    clean = client.get(f"/api/tracks/{tid}/yt_search/clean").json()["clean"]
    assert clean == {res[0]["url"]: {"video_id": EXPL, "title": "The Percocet & Stripper Joint", "album": "DS2 (Deluxe)", "duration_ms": 149000}}


def test_find_still_works_when_youtube_music_is_down(client, monkeypatch):
    entries = {"entries": [{"id": CLEAN, "title": "Song", "channel": "c", "duration": 150}]}
    monkeypatch.setattr(yt_search.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(entries), ""))
    tid = client.post("/api/tracks", json={"title": "Song", "artists": "A"}).json()["track"]["id"]
    res = client.get(f"/api/tracks/{tid}/yt_search").json()["results"]
    assert len(res) == 1 and "clean_of" not in res[0]
    assert client.get(f"/api/tracks/{tid}/yt_search/clean").json() == {"clean": {}}


def test_migration_adds_columns(lib):
    cols = {r["name"] for r in lib.conn().execute("PRAGMA table_info(tracks)")}
    assert {"clean_url", "clean_check", "clean_checked_at", "clean_keep"} <= cols
    assert db.SCHEMA_VERSION == 10


def test_user_uploads_are_never_flagged(client, lib, ytm, monkeypatch):
    # A user upload the same length as the official explicit copy: not flagged, but the official copy is offered.
    VIDEOS["uploadupload"[:11]] = next_resp("uploadupload"[:11], "The Percocet & Stripper Joint", "SomeChannel", None, "2:29", False)
    vid = "uploadupload"[:11]
    r = ytmusic.check({"source_url": f"https://youtu.be/{vid}", "title": "The Percocet & Stripper Joint", "artists": "Future"})
    assert r["result"] == "unofficial" and r["twin"]["video_id"] == EXPL
    tid = add(client, f"https://youtu.be/{vid}")
    cleancheck.Checker(lib).step()
    t = client.get(f"/api/tracks/{tid}").json()["track"]
    assert t["clean"]["result"] == "unofficial" and not t["clean_flag"]
    assert client.get("/api/tracks?view=smart:clean_versions").json()["tracks"] == []
    out = client.post("/api/tracks/swap_explicit", json={"ids": [tid]}).json()  # you can still choose the official copy
    assert out["swapped"] == 1 and out["tracks"][0]["source_url"] == ytmusic.watch_url(EXPL)


def test_old_results_are_checked_again_under_new_rules(client, lib, ytm):
    tid = add(client, f"https://youtu.be/{CLEAN}")
    conn = lib.conn()
    conn.execute("UPDATE tracks SET clean_url = source_url, clean_check = ?, clean_checked_at = 1 WHERE id = ?",
                 (json.dumps({"result": "clean", "official": False, "twin": {"video_id": EXPL}}), tid))
    assert cleancheck.due_count(conn) == 1
    cleancheck.Checker(lib).step()
    assert cleancheck.due_count(conn) == 0
    assert client.get(f"/api/tracks/{tid}").json()["track"]["clean"]["v"] == ytmusic.RULES


def test_status_counts(client, lib, ytm):
    add(client, f"https://youtu.be/{CLEAN}")
    client.post("/api/tracks", json={"title": "No link yet"})
    s = client.get("/api/status").json()["clean"]
    assert (s["linked"], s["songs"], s["checked"]) == (1, 2, 0)


def test_search_ahead_preloads_youtube_music(client, lib, ytm, monkeypatch):
    entries = {"entries": [{"id": CLEAN, "title": "The Percocet & Stripper Joint", "channel": "Future", "duration": 150}]}
    monkeypatch.setattr(yt_search.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(entries), ""))
    tid = client.post("/api/tracks", json={"title": "The Percocet & Stripper Joint", "artists": "Future"}).json()["track"]["id"]
    pre = yt_search.Prefetcher(lib)
    pre.request([tid], 3)
    assert pre.step() is True
    assert ytm == ["search"]  # YouTube Music answered during search-ahead...
    client.get(f"/api/tracks/{tid}/yt_search")
    clean = client.get(f"/api/tracks/{tid}/yt_search/clean").json()["clean"]
    assert ytm == ["search"] and list(clean.values())[0]["video_id"] == EXPL  # ...so marking needed no new request
