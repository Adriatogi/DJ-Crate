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
