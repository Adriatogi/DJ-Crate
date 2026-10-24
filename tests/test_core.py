from djcrate import db
from djcrate.downloader import classify_error, valid_link
from djcrate.library import safe_filename, unique_path
from djcrate.spotify import parse_playlist_ref, plan_resync
from djcrate.yt_search import mix_flags


def test_safe_filename_title_then_all_artists():
    assert safe_filename("Strobe", "deadmau5") == "Strobe - deadmau5"
    assert safe_filename("Song (feat. B)", "A, B") == "Song (feat. B) - A, B"
    assert safe_filename("AC/DC: Live?", "X") == "AC DC Live - X"
    assert safe_filename("", "") == "Untitled"
    assert len(safe_filename("a" * 400, "b").encode()) <= 200


def test_unique_path(tmp_path):
    (tmp_path / "Song - A.mp3").write_bytes(b"x")
    assert unique_path(tmp_path, "Song - A").name == "Song - A (2).mp3"


def test_dedup_spotify_id_then_isrc_then_name(lib):
    conn = lib.conn()
    a, created = db.upsert_track(conn, {"spotify_id": "sp1", "isrc": "usabc", "title": "Strobe", "artists": "deadmau5"})
    assert created
    assert db.upsert_track(conn, {"spotify_id": "sp1", "title": "whatever"}) == (a, False)
    # Same ISRC, different Spotify id (re-release) → same track
    assert db.upsert_track(conn, {"spotify_id": "sp2", "isrc": "USABC", "title": "Strobe (Remastered)", "artists": "deadmau5"}) == (a, False)
    # Name match, accents/case/punctuation ignored
    assert db.upsert_track(conn, {"title": "STROBE!", "artists": "Deadmau5"}) == (a, False)
    b, created = db.upsert_track(conn, {"title": "Ghosts n Stuff", "artists": "deadmau5"})
    assert created and b != a


def test_import_never_overwrites_user_edits(lib):
    conn = lib.conn()
    tid, _ = db.upsert_track(conn, {"spotify_id": "sp1", "title": "Strobe", "artists": "deadmau5"})
    conn.execute("UPDATE tracks SET title = 'My Title', notes = 'n', ready = 1 WHERE id = ?", (tid,))
    db.upsert_track(conn, {"spotify_id": "sp1", "title": "Strobe", "artists": "deadmau5", "album": "For Lack"})
    t = db.get_track(conn, tid)
    assert (t["title"], t["notes"], t["ready"], t["album"]) == ("My Title", "n", True, "For Lack")


def test_plan_resync_flags_never_deletes():
    current = {1: False, 2: False, 3: True}
    plan = plan_resync(current, [2, 3, 4, 4])
    assert plan == {"add": [4], "removed": [1], "restored": [3]}
    assert plan_resync({1: False}, [1]) == {"add": [], "removed": [], "restored": []}


def test_parse_playlist_ref():
    pid = "37i9dQZF1DXcBWIGoYBM5M"
    assert parse_playlist_ref(f"https://open.spotify.com/playlist/{pid}?si=abc") == pid
    assert parse_playlist_ref(f"https://open.spotify.com/intl-es/playlist/{pid}") == pid
    assert parse_playlist_ref(f"spotify:playlist:{pid}") == pid
    assert parse_playlist_ref("liked") == "liked"


def test_valid_link():
    assert valid_link("https://www.youtube.com/watch?v=abc")
    assert valid_link("https://youtu.be/abc")
    assert valid_link("https://music.youtube.com/watch?v=abc")
    assert valid_link("https://soundcloud.com/a/b")
    assert valid_link("https://artist.bandcamp.com/track/x")
    assert not valid_link("https://evil.com/youtube.com")
    assert not valid_link("youtube.com/watch?v=abc")


def test_classify_error_real_messages():
    cases = {
        "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader": "unavailable",
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video": "unavailable",
        "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.": "needs_cookies",
        "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies": "needs_cookies",
        "WARNING: [youtube] nsig extraction failed: Some formats may be missing": "ytdlp_outdated",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden": "forbidden",
        "ERROR: [youtube] abc: Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided>": "transient",
        "ERROR: [download] Got error: Read timed out.": "transient",
        "ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path": "ffmpeg_missing",
        "OSError: [Errno 28] No space left on device": "disk_full",
        "ERROR: Unsupported URL: https://example.com": "invalid_url",
        "something new and weird": "unknown",
    }
    for text, code in cases.items():
        assert classify_error(text) == code, text


def test_mix_flags():
    assert mix_flags("Strobe", "deadmau5 - Strobe (Radio Edit)") == ["Says “Radio Edit” — not in the Spotify title"]
    assert mix_flags("Strobe - Extended Mix", "deadmau5 - Strobe") == ["Missing “Extended Mix”"]
    assert mix_flags("Strobe - Extended Mix", "deadmau5 - Strobe (Extended Mix)") == []
    assert mix_flags("Song", "Song (Official Audio)") == []
