import time

import pytest

from djcrate import db, downloader, tags
from djcrate.downloader import DownloadManager, _Failure


@pytest.fixture
def dm(lib, monkeypatch):
    monkeypatch.setattr("djcrate.tools.ffmpeg_dir", lambda: "/opt/homebrew/bin")
    monkeypatch.setattr("djcrate.tools.online", lambda max_age=10: True)
    return DownloadManager(lib)


def add_track(lib, **meta):
    conn = lib.conn()
    tid, _ = db.upsert_track(conn, {"title": "Strobe", "artists": "deadmau5", "duration_ms": 3000, **meta})
    conn.execute("UPDATE tracks SET source_url = 'https://www.youtube.com/watch?v=abc' WHERE id = ?", (tid,))
    return conn, tid


def run_one(dm):
    job = dm._claim()
    assert job, f"nothing claimed (paused={dm.paused_reason})"
    dm._run(job)
    return job


def test_success_moves_tagged_file_into_library(dm, lib, make_mp3, monkeypatch):
    conn, tid = add_track(lib, album="Album")
    monkeypatch.setattr(dm, "_fetch", lambda job_id, url, tmp, cookies: make_mp3(tmp / "audio.mp3"))
    dm.enqueue(conn, [tid])
    run_one(dm)
    t = db.get_track(conn, tid)
    assert t["download_status"] == "done" and t["file_rel_path"] == "Strobe - deadmau5.mp3"
    assert tags.read_tags(lib.library / "Strobe - deadmau5.mp3")["album"] == "Album"
    assert not t["check_version"]  # 3s file vs 3s on Spotify
    assert list(lib.tmp_dir.iterdir()) == []


def test_failure_leaves_no_partial_file(dm, lib, make_mp3, monkeypatch):
    conn, tid = add_track(lib)

    def fetch(job_id, url, tmp, cookies):
        (tmp / "audio.webm.part").write_bytes(b"half")
        raise _Failure("unavailable", "Video unavailable")

    monkeypatch.setattr(dm, "_fetch", fetch)
    dm.enqueue(conn, [tid])
    run_one(dm)
    t = db.get_track(conn, tid)
    assert t["download_status"] == "error" and "paste a new link" in t["error_msg"]
    assert [p for p in lib.library.iterdir() if p.name != ".djcrate"] == []
    assert list(lib.tmp_dir.iterdir()) == []
    job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "failed" and job["error_code"] == "unavailable"


def test_transient_errors_retry_with_backoff_then_403_means_outdated(dm, lib, monkeypatch):
    conn, tid = add_track(lib)
    monkeypatch.setattr(dm, "_fetch", lambda *a: (_ for _ in ()).throw(_Failure("forbidden", "HTTP Error 403")))
    monkeypatch.setattr(downloader.threading, "Timer", lambda *a: type("T", (), {"start": lambda self: None})())
    dm.enqueue(conn, [tid])
    for attempt in range(1, 5):
        conn.execute("UPDATE jobs SET next_attempt_at = 0")
        run_one(dm)
        job = conn.execute("SELECT * FROM jobs").fetchone()
        if attempt <= 3:
            assert job["status"] == "queued" and job["attempts"] == attempt
    assert job["status"] == "failed" and job["error_code"] == "ytdlp_outdated"


def test_redownload_keeps_same_path_and_trashes_old(dm, lib, make_mp3, monkeypatch):
    trashed = []
    monkeypatch.setattr("djcrate.library.send2trash", lambda p: (trashed.append(p), __import__("os").remove(p)))
    conn, tid = add_track(lib)
    old = make_mp3(lib.library / "Custom Name.mp3", seconds=2)
    conn.execute("UPDATE tracks SET file_rel_path = 'Custom Name.mp3', download_status = 'done' WHERE id = ?", (tid,))
    monkeypatch.setattr(dm, "_fetch", lambda job_id, url, tmp, cookies: make_mp3(tmp / "audio.mp3", seconds=3))
    dm.enqueue(conn, [tid], replace=True)
    run_one(dm)
    assert trashed == [str(old)]
    t = db.get_track(conn, tid)
    assert t["file_rel_path"] == "Custom Name.mp3" and abs(t["file_duration_ms"] - 3000) < 200


def test_queue_pauses_when_offline_or_disk_full(dm, lib, monkeypatch):
    conn, tid = add_track(lib)
    dm.enqueue(conn, [tid])
    monkeypatch.setattr("djcrate.tools.online", lambda max_age=10: False)
    assert dm._claim() is None and dm.paused_reason == "offline"
    monkeypatch.setattr("djcrate.tools.online", lambda max_age=10: True)
    monkeypatch.setattr(downloader.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    assert dm._claim() is None and dm.paused_reason == "disk_full"


def test_interrupted_downloads_are_recovered_on_open(dm, lib):
    conn, tid = add_track(lib)
    dm.enqueue(conn, [tid])
    dm._claim()  # now "running"
    (lib.tmp_dir / "1").mkdir()
    (lib.tmp_dir / "1" / "audio.part").write_bytes(b"x")
    lib.open(lib.library)  # simulate relaunch after a crash
    assert lib.interrupted_jobs == 1
    assert list(lib.tmp_dir.iterdir()) == []
    conn = lib.conn()
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "interrupted"
    dm.resume_interrupted(conn, resume=True)
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "queued"


def test_cancel_queued_and_no_link(dm, lib):
    conn, tid = add_track(lib)
    conn.execute("UPDATE tracks SET source_url = NULL WHERE id = ?", (tid,))
    assert dm.enqueue(conn, [tid])["skipped"][0]["reason"] == "No link"
    conn.execute("UPDATE tracks SET source_url = 'https://youtu.be/x' WHERE id = ?", (tid,))
    dm.enqueue(conn, [tid])
    assert dm.enqueue(conn, [tid])["skipped"][0]["reason"] == "Already queued"
    job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]
    dm.cancel(conn, job_id)
    assert db.get_track(conn, tid)["download_status"] == "none"



def test_convert_is_constant_bitrate_192(dm, tmp_path):
    """CBR, not VBR: VBR MP3s can make cues drift between Rekordbox and CDJs."""
    import subprocess
    from djcrate import tools
    from mutagen.mp3 import MP3, BitrateMode
    ffdir = tools.ffmpeg_dir() or pytest.skip("ffmpeg not installed")
    src = tmp_path / "source.wav"
    subprocess.run([f"{ffdir}/ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    str(src)], check=True)
    dm._set_job = lambda *a, **k: None
    info = MP3(dm._convert(1, src, tmp_path / "audio.mp3", ffdir)).info
    assert abs(info.bitrate - 192000) < 1000 and info.bitrate_mode != BitrateMode.VBR


def _job(conn, tid, status, progress=0.0, stage=""):
    return conn.execute("INSERT INTO jobs (track_id, status, progress, stage) VALUES (?, ?, ?, ?)", (tid, status, progress, stage)).lastrowid


def test_overall_progress_counts_the_current_run(dm, lib):
    conn, tid = add_track(lib)
    _job(conn, tid, "done")                       # from an earlier run: not counted
    assert dm.batch(conn) is None and dm.last_batch is None
    first = _job(conn, tid, "running", 0.5, "downloading")
    _job(conn, tid, "queued")
    b = dm.batch(conn)
    assert (b["total"], b["done"], b["queued"]) == (2, 0, 1) and b["fraction"] == 0.25
    assert b["running"][0]["title"] == "Strobe" and b["running"][0]["progress"] == 0.5
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (first,))
    _job(conn, tid, "queued")                     # added mid-way: the total grows
    b = dm.batch(conn)
    assert (b["total"], b["done"]) == (3, 1) and round(b["fraction"], 3) == 0.333
    conn.execute("UPDATE jobs SET status = 'running', stage = 'converting' WHERE status = 'queued' AND id = (SELECT min(id) FROM jobs WHERE status = 'queued')")
    assert dm.batch(conn)["fraction"] == round(2 / 3, 4)   # converting counts as downloaded
    conn.execute("UPDATE jobs SET status = 'done' WHERE status = 'running'")
    conn.execute("UPDATE jobs SET status = 'failed' WHERE status = 'queued'")
    assert dm.batch(conn) is None
    assert {k: dm.last_batch[k] for k in ("total", "done", "failed")} == {"total": 3, "done": 2, "failed": 1}


def test_overall_progress_includes_retried_old_jobs(dm, lib):
    conn, tid = add_track(lib)
    old = _job(conn, tid, "failed")
    _job(conn, tid, "queued")
    conn.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (old,))   # retried
    assert dm.batch(conn)["total"] == 2
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (old,))
    assert dm.batch(conn)["total"] == 2 and dm.batch(conn)["done"] == 1     # still counted once done


def test_status_reports_overall_progress(client, lib):
    from djcrate.api import downloads
    downloads.batch_start_id, downloads.last_batch, downloads.batch_old_ids = None, None, set()
    conn, tid = add_track(lib)
    _job(conn, tid, "queued")
    q = client.get("/api/status").json()["queue"]
    assert q["batch"]["total"] == 1 and q["last_batch"] is None


def test_no_formats_is_not_called_outdated(monkeypatch):
    assert downloader.classify_error("ERROR: [youtube] x: Requested format is not available. Use --list-formats") == "no_formats"
    monkeypatch.setattr("djcrate.tools.js_runtime", lambda: "deno")
    assert "different link" in downloader._no_formats_message(["ERROR: Requested format is not available"], "")
    assert "cookies" in downloader._no_formats_message([], "chrome")
    monkeypatch.setattr("djcrate.tools.js_runtime", lambda: None)
    assert "brew install deno" in downloader._no_formats_message([], "")


def test_no_formats_with_cookies_tries_once_without(dm, lib, make_mp3, monkeypatch):
    conn, tid = add_track(lib)
    db.set_setting(conn, "cookies_browser", "chrome")
    fake_cookies(monkeypatch, lib)
    tried = []

    def fetch(job_id, url, tmp, cookies):
        tried.append(cookies)
        if cookies:
            raise _Failure("no_formats", "Requested format is not available")
        return make_mp3(tmp / "audio.mp3")

    monkeypatch.setattr(dm, "_fetch", fetch)
    dm.enqueue(conn, [tid])
    run_one(dm)
    assert tried[0].endswith("cookies.txt") and tried[1] == "" and db.get_track(conn, tid)["download_status"] == "done"


def fake_cookies(monkeypatch, lib, fail=None):
    """Pretend the browser's cookies were read (or fail to be) without touching a real browser."""
    from djcrate import cookies
    src = lib.library / "fake-cookies.txt"
    src.write_text("# Netscape HTTP Cookie File\n")
    calls = []

    def copy_for_job(browser, profile, dest):
        calls.append((browser, profile))
        if fail:
            raise cookies.CookieError(fail, cookies.MESSAGES[fail].format(name="Chrome"), "WARNING: cannot decrypt v10 cookies: no key found")
        dest.write_text(src.read_text())
        return str(dest)
    monkeypatch.setattr(cookies, "copy_for_job", copy_for_job)
    return calls


def test_downloads_use_a_copy_of_the_cookies_not_the_browser(dm, lib, make_mp3, monkeypatch):
    conn, tid = add_track(lib)
    db.set_setting(conn, "cookies_browser", "chrome")
    db.set_setting(conn, "cookies_profile", "Profile 1")
    calls = fake_cookies(monkeypatch, lib)
    seen = []

    def fetch(job_id, url, tmp, cookies):
        seen.append(cookies)
        assert (tmp / "cookies.txt").exists()
        return make_mp3(tmp / "audio.mp3")
    monkeypatch.setattr(dm, "_fetch", fetch)
    dm.enqueue(conn, [tid])
    run_one(dm)
    assert calls == [("chrome", "Profile 1")] and seen[0].endswith("cookies.txt")


def test_cookie_read_failure_explains_itself(dm, lib, monkeypatch):
    conn, tid = add_track(lib)
    db.set_setting(conn, "cookies_browser", "chrome")
    fake_cookies(monkeypatch, lib, fail="keychain")
    monkeypatch.setattr(dm, "_fetch", lambda *a: pytest.fail("shouldn't download without the cookies"))
    dm.enqueue(conn, [tid])
    job = run_one(dm)
    row = conn.execute("SELECT error_code, error_msg, error_detail FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["error_code"] == "cookies_failed" and "Always Allow" in row["error_msg"]
    assert "no key found" in row["error_detail"]


def test_still_asked_to_sign_in_rereads_cookies_next_time(dm, lib, monkeypatch):
    from djcrate import cookies
    conn, tid = add_track(lib)
    db.set_setting(conn, "cookies_browser", "chrome")
    fake_cookies(monkeypatch, lib)
    stale = []
    monkeypatch.setattr(cookies, "mark_stale", lambda b, p="": stale.append(b))

    def fetch(job_id, url, tmp, c):
        raise _Failure("needs_cookies", "Sign in to confirm you're not a bot")
    monkeypatch.setattr(dm, "_fetch", fetch)
    dm.enqueue(conn, [tid])
    job = run_one(dm)
    msg = conn.execute("SELECT error_msg FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0]
    assert stale == ["chrome"] and "even with your browser's cookies" in msg


def test_failed_download_keeps_what_ytdlp_said(dm, lib, monkeypatch):
    conn, tid = add_track(lib)
    out = "WARNING: [youtube] No supported JavaScript runtime could be found\nERROR: [youtube] abc: Requested format is not available"

    def fetch(job_id, url, tmp, cookies):
        raise _Failure("no_formats", "Requested format is not available", "No audio.", out)

    monkeypatch.setattr(dm, "_fetch", fetch)
    dm.enqueue(conn, [tid])
    job = run_one(dm)
    row = conn.execute("SELECT error_detail FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["error_detail"] == out
    assert downloader._tool_output(["[download] 10%", "WARNING: w", "ERROR: e"]) == "WARNING: w\nERROR: e"
    dm.retry(conn, [job["id"]])
    assert conn.execute("SELECT error_detail FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0] is None
