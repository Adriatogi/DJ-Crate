"""Download queue: link → yt-dlp → MP3 (CBR 192k) → tags → into the library.

yt-dlp runs as a subprocess so "Update yt-dlp" takes effect without a restart
and Cancel can kill a download immediately. Files are built in
`.djcrate/tmp/<job>/` and only moved into the library once the audio is good,
so Rekordbox never sees a half-written file.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from . import db, library, tags, tools

log = logging.getLogger("djcrate.downloader")

WORKERS = 2
# Constant bitrate on purpose: VBR MP3s can make cue points and beatgrids drift
# between Rekordbox and CDJs. 192k with a 17 kHz lowpass matches the sound of
# yt-dlp's default MP3 conversion (YouTube's audio is ~130 kbps anyway).
MP3_BITRATE = "192k"
MP3_LOWPASS_HZ = "17000"
MIN_FREE_BYTES = 200 * 1024 * 1024
RETRY_DELAYS = [5, 30, 120]
ACTIVE_STATUSES = ("queued", "running", "interrupted")

ALLOWED_HOSTS = re.compile(
    r"^https?://([a-z0-9-]+\.)*(youtube\.com|youtu\.be|soundcloud\.com|bandcamp\.com)(/|$)", re.I
)

ERROR_MESSAGES = {
    "unavailable": "Video unavailable (removed, private or blocked in your country) — paste a new link.",
    "needs_cookies": "YouTube wants you signed in (age-restricted or bot check). Turn on Settings → Use cookies from browser, then Retry.",
    "ytdlp_outdated": "YouTube changed something and the downloader needs an update. Click Update yt-dlp, then Retry.",
    "no_formats": "YouTube didn't offer any audio to download for this video. Retry later, or paste a different link for this song.",
    "transient": "Network problem while downloading.",
    "rate_limited": "YouTube is rate-limiting you right now — wait a few minutes.",
    "forbidden": "YouTube refused the download (HTTP 403).",
    "ffmpeg_missing": "ffmpeg isn't installed. Run: brew install ffmpeg",
    "disk_full": "Not enough disk space.",
    "invalid_url": "That link isn't a supported YouTube/SoundCloud/Bandcamp link.",
    "no_link": "This song has no link yet — paste one first.",
    "library_missing": "Library folder not found.",
    "unknown": "Download failed.",
}

# Order matters: first match wins.
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("disk_full", ("no space left on device", "errno 28")),
    ("ffmpeg_missing", ("ffmpeg not found", "ffprobe and ffmpeg not found", "ffmpeg is not installed")),
    ("invalid_url", ("is not a valid url", "unsupported url")),
    ("rate_limited", ("http error 429", "too many requests", "rate-limited", "rate limited")),
    ("needs_cookies", (
        "sign in to confirm your age", "age-restricted", "confirm you're not a bot", "confirm you’re not a bot",
        "use --cookies", "cookies-from-browser", "login required", "this video may be inappropriate",
    )),
    ("unavailable", (
        "video unavailable", "private video", "has been removed", "no longer available", "not available in your country",
        "blocked it in your country", "account associated with this video has been terminated", "this video is unavailable",
        "members-only", "join this channel", "http error 404", "this track is not available", "premieres in",
        "this live event will begin",
    )),
    # YouTube handed out no audio formats. Usually not an old yt-dlp: browser cookies (YouTube serves
    # signed-in sessions differently) or a missing JavaScript runtime. See _no_formats_message.
    ("no_formats", ("requested format is not available",)),
    ("ytdlp_outdated", (
        "nsig extraction failed", "signature extraction failed", "unable to extract", "please report this issue",
        "n challenge", "failed to extract any player response",
        "unable to download api page",
    )),
    ("forbidden", ("http error 403",)),
    ("transient", (
        "timed out", "timeout", "connection reset", "temporary failure in name resolution", "nodename nor servname",
        "network is unreachable", "http error 5", "http error 429", "too many requests", "remote end closed",
        "incompleteread", "connection refused", "connection aborted", "errno 54", "errno 61", "errno 60",
        "ssl:", "unable to download webpage", "got error",
    )),
]


def classify_error(text: str) -> str:
    low = (text or "").lower()
    for code, needles in _PATTERNS:
        if any(n in low for n in needles):
            return code
    return "unknown"


def friendly(code: str, detail: str = "") -> str:
    msg = ERROR_MESSAGES.get(code, ERROR_MESSAGES["unknown"])
    if code == "unknown" and detail:
        msg = f"Download failed: {detail[:300]}"
    return msg


def _no_formats_message(lines: list[str], cookies: str) -> str:
    """Why YouTube offered no audio, from yt-dlp's warnings, and what to do."""
    low = "\n".join(lines).lower()
    if "javascript runtime" in low or "js runtime" in low or not tools.js_runtime():
        return ("YouTube didn't offer any audio because no JavaScript runtime was found. "
                "Run: brew install deno, then Re-check in Settings and Retry.")
    if cookies:
        return ("YouTube didn't offer any audio for this video while signed in with your browser cookies "
                "(it was also tried without them). Retry later, or paste a different link.")
    return ERROR_MESSAGES["no_formats"]


def valid_link(url: str) -> bool:
    return bool(ALLOWED_HOSTS.match((url or "").strip()))


def _error_detail(lines: list[str]) -> str:
    errs = [l.strip() for l in lines if "ERROR" in l]
    return (errs[-1] if errs else (lines[-1].strip() if lines else "")).replace("ERROR: ", "")


class _Cancelled(Exception):
    pass


class _Requeue(Exception):
    """Environment problem (disk/offline/library): put the job back, don't count an attempt."""

    def __init__(self, reason):
        self.reason = reason


class DownloadManager:
    def __init__(self, state):
        self.state = state
        self.cond = threading.Condition()
        self.stopping = False
        self.paused_reason: str | None = None
        self.cancelled: set[int] = set()
        self.procs: dict[int, subprocess.Popen] = {}
        self.threads: list[threading.Thread] = []
        self._claim_lock = threading.Lock()
        self.batch_start_id: int | None = None   # first job of the current run of downloads (for the overall progress bar)
        self.last_batch: dict | None = None      # how the last run ended: {total, done, failed, at}
        self.batch_old_ids: set[int] = set()     # older jobs retried during this run (ids below batch_start_id)

    # ------------------------------------------------------------ lifecycle

    def start(self):
        for i in range(WORKERS):
            t = threading.Thread(target=self._loop, name=f"download-{i + 1}", daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stopping = True
        for p in list(self.procs.values()):
            p.terminate()
        self.wake()

    def wake(self):
        with self.cond:
            self.cond.notify_all()

    # ------------------------------------------------------------ overall progress

    def batch(self, conn) -> dict | None:
        """Progress of the current run of downloads: every job since the queue was last empty.
        -> {total, done, failed, running, fraction}, or None when nothing is queued or running."""
        active = conn.execute("SELECT min(id) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0]
        if active is None:
            if self.batch_start_id is not None:
                # The run just ended: remember how it went so the header can say so once.
                done, failed = self._batch_counts(conn)
                if done or failed:
                    self.last_batch = {"total": done + failed, "done": done, "failed": failed, "at": time.time()}
                self.batch_start_id = None
                self.batch_old_ids = set()
            return None
        if self.batch_start_id is None:
            self.batch_start_id = active
        rows = conn.execute(
            "SELECT j.id, j.status, j.progress, j.stage, t.title, t.artists FROM jobs j JOIN tracks t ON t.id = j.track_id "
            "WHERE j.status IN ('queued', 'running') ORDER BY j.id").fetchall()
        self.batch_old_ids |= {r["id"] for r in rows if r["id"] < self.batch_start_id}
        done, failed = self._batch_counts(conn)
        running = [{"title": r["title"], "artists": r["artists"], "stage": r["stage"] or "starting",
                    # converting / tagging come after the download itself, so that song is as good as done
                    "progress": 1.0 if r["stage"] in ("converting", "tagging") else float(r["progress"] or 0)}
                   for r in rows if r["status"] == "running"]
        total = done + failed + len(rows)
        fraction = (done + failed + sum(r["progress"] for r in running)) / total if total else 0.0
        return {"total": total, "done": done, "failed": failed, "queued": len(rows) - len(running),
                "running": running, "fraction": round(min(fraction, 1.0), 4)}

    def _batch_counts(self, conn) -> tuple[int, int]:
        old = sorted(self.batch_old_ids)
        row = conn.execute(
            "SELECT coalesce(sum(status = 'done'), 0), coalesce(sum(status = 'failed'), 0) FROM jobs "
            f"WHERE (id >= ? OR id IN ({','.join('?' * len(old)) or 'NULL'})) AND status IN ('done', 'failed')",
            (self.batch_start_id, *old)).fetchone()
        return int(row[0]), int(row[1])

    # ------------------------------------------------------------ public API

    def enqueue(self, conn, track_ids: list[int], replace: bool = False, swaps: dict | None = None) -> dict:
        """swaps: {track id: (url, on_done)} downloads that link instead; on_done is applied once the file is in."""
        queued, skipped = 0, []
        swaps = swaps or {}
        with db.tx(conn):
            for tid in track_ids:
                t = conn.execute("SELECT id, title, source_url, download_status FROM tracks WHERE id = ?", (tid,)).fetchone()
                if not t:
                    continue
                if not (t["source_url"] or "").strip() and tid not in swaps:
                    skipped.append({"track_id": tid, "title": t["title"], "reason": "No link"})
                    continue
                active = conn.execute(
                    f"SELECT 1 FROM jobs WHERE track_id = ? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                    (tid, *ACTIVE_STATUSES),
                ).fetchone()
                if active:
                    skipped.append({"track_id": tid, "title": t["title"], "reason": "Already queued"})
                    continue
                swap = conn.execute(
                    "SELECT id FROM jobs WHERE track_id = ? AND status = 'failed' AND url IS NOT NULL AND url <> coalesce(?, '') "
                    "ORDER BY id DESC LIMIT 1", (tid, t["source_url"])).fetchone()
                if swap and tid not in swaps:
                    # The song owes a swap to its other version: downloading it means trying that swap again.
                    conn.execute("UPDATE jobs SET status = 'queued', attempts = 0, next_attempt_at = 0, error_code = NULL, error_msg = NULL, "
                                 "progress = 0, stage = '', updated_at = datetime('now') WHERE id = ?", (swap["id"],))
                    conn.execute("UPDATE tracks SET download_status = 'queued', error_code = NULL, error_msg = NULL WHERE id = ?", (tid,))
                    queued += 1
                    continue
                conn.execute("UPDATE jobs SET status = 'dismissed' WHERE track_id = ? AND status = 'failed'", (tid,))
                url, on_done = swaps.get(tid, (None, None))
                conn.execute("INSERT INTO jobs (track_id, replace_file, url, on_done) VALUES (?, ?, ?, ?)",
                             (tid, int(replace or t["download_status"] in ("done", "missing")), url, json.dumps(on_done) if on_done else None))
                conn.execute("UPDATE tracks SET download_status = 'queued', error_code = NULL, error_msg = NULL WHERE id = ?", (tid,))
                queued += 1
        self.wake()
        return {"queued": queued, "skipped": skipped}

    def cancel(self, conn, job_id: int) -> None:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return
        if job["status"] == "running":
            self.cancelled.add(job_id)
            proc = self.procs.get(job_id)
            if proc:
                proc.terminate()
        elif job["status"] in ("queued", "interrupted"):
            with db.tx(conn):
                conn.execute("UPDATE jobs SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?", (job_id,))
                self._reset_track_status(conn, job["track_id"])

    def cancel_all(self, conn) -> None:
        for row in conn.execute("SELECT id FROM jobs WHERE status IN ('queued', 'running', 'interrupted')").fetchall():
            self.cancel(conn, row["id"])

    def retry(self, conn, job_ids: list[int] | None = None) -> int:
        q = "SELECT id, track_id FROM jobs WHERE status = 'failed'"
        params: list = []
        if job_ids:
            q += f" AND id IN ({','.join('?' for _ in job_ids)})"
            params = job_ids
        rows = conn.execute(q, params).fetchall()
        with db.tx(conn):
            for r in rows:
                conn.execute(
                    "UPDATE jobs SET status = 'queued', attempts = 0, next_attempt_at = 0, error_code = NULL, error_msg = NULL, "
                    "progress = 0, stage = '', updated_at = datetime('now') WHERE id = ?", (r["id"],))
                conn.execute("UPDATE tracks SET download_status = 'queued', error_code = NULL, error_msg = NULL WHERE id = ?", (r["track_id"],))
        self.wake()
        return len(rows)

    def resume_interrupted(self, conn, resume: bool) -> int:
        rows = conn.execute("SELECT id, track_id FROM jobs WHERE status = 'interrupted'").fetchall()
        with db.tx(conn):
            for r in rows:
                if resume:
                    conn.execute("UPDATE jobs SET status = 'queued', next_attempt_at = 0 WHERE id = ?", (r["id"],))
                else:
                    conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ?", (r["id"],))
                    self._reset_track_status(conn, r["track_id"])
        self.state.interrupted_jobs = 0
        self.wake()
        return len(rows)

    def dismiss(self, conn, job_id: int) -> None:
        conn.execute("UPDATE jobs SET status = 'dismissed' WHERE id = ? AND status = 'failed'", (job_id,))

    def _reset_track_status(self, conn, track_id: int) -> None:
        t = conn.execute("SELECT file_rel_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
        has_file = bool(t and t["file_rel_path"] and self.state.abs_path(t["file_rel_path"]).is_file())
        conn.execute("UPDATE tracks SET download_status = ? WHERE id = ?", ("done" if has_file else "none", track_id))

    # ------------------------------------------------------------ worker

    def _environment_problem(self) -> str | None:
        if not self.state.library_reachable():
            return "library_missing"
        if not tools.ffmpeg_dir():
            return "ffmpeg_missing"
        try:
            if shutil.disk_usage(self.state.library).free < MIN_FREE_BYTES:
                return "disk_full"
        except OSError:
            return "library_missing"
        if not tools.online():
            return "offline"
        return None

    def _claim(self):
        with self._claim_lock:
            conn = self.state.conn()
            try:
                job = conn.execute(
                    "SELECT * FROM jobs WHERE status = 'queued' AND next_attempt_at <= ? ORDER BY id LIMIT 1", (time.time(),)
                ).fetchone()
                if not job:
                    return None
                problem = self._environment_problem()
                if problem != self.paused_reason:
                    if problem:
                        log.warning("Download queue paused: %s", problem)
                    elif self.paused_reason:
                        log.info("Download queue resumed")
                self.paused_reason = problem
                if problem:
                    return None
                with db.tx(conn):
                    conn.execute(
                        "UPDATE jobs SET status = 'running', progress = 0, stage = 'starting', updated_at = datetime('now') WHERE id = ?",
                        (job["id"],))
                    conn.execute("UPDATE tracks SET download_status = 'downloading' WHERE id = ?", (job["track_id"],))
                return dict(job)
            finally:
                conn.close()

    def _loop(self):
        while not self.stopping:
            with self.cond:
                self.cond.wait(timeout=3)
            if self.stopping:
                return
            st = self.state
            if st.status != "ok" or st.read_only:
                continue
            try:
                while not self.stopping and (job := self._claim()):
                    self._run(job)
            except Exception:
                log.exception("Download worker error")

    def _set_job(self, job_id: int, **fields):
        conn = self.state.conn()
        try:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE jobs SET {sets}, updated_at = datetime('now') WHERE id = ?", (*fields.values(), job_id))
        finally:
            conn.close()

    def _run(self, job: dict):
        job_id = job["id"]
        tmp = self.state.tmp_dir / str(job_id)
        try:
            conn = self.state.conn()
            try:
                track = db.get_track(conn, job["track_id"])
                cookies = db.get_setting(conn, "cookies_browser", "")
            finally:
                conn.close()
            if not track:
                self._set_job(job_id, status="cancelled")
                return
            # A version swap downloads the other copy's link; the song only switches to it once it's in.
            url = (job.get("url") or track["source_url"] or "").strip()
            if not url:
                raise _Failure("no_link", "")
            if not valid_link(url):
                raise _Failure("invalid_url", url)
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            try:
                mp3 = self._fetch(job_id, url, tmp, cookies)
            except _Failure as f:
                if f.code != "no_formats" or not cookies:
                    raise
                # Signed-in sessions sometimes get no formats where a plain request does: try once without cookies.
                log.info("Job %s: no formats with cookies, trying without", job_id)
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True)
                mp3 = self._fetch(job_id, url, tmp, "")
            self._finish(job, track, mp3)
        except _Cancelled:
            log.info("Job %s cancelled", job_id)
            conn = self.state.conn()
            try:
                with db.tx(conn):
                    conn.execute("UPDATE jobs SET status = 'cancelled', stage = '', updated_at = datetime('now') WHERE id = ?", (job_id,))
                    self._reset_track_status(conn, job["track_id"])
            finally:
                conn.close()
        except _Requeue as rq:
            self.paused_reason = rq.reason
            self._set_job(job_id, status="queued", stage="", progress=0, next_attempt_at=time.time() + 10)
            conn = self.state.conn()
            try:
                conn.execute("UPDATE tracks SET download_status = 'queued' WHERE id = ?", (job["track_id"],))
            finally:
                conn.close()
        except _Failure as f:
            self._fail(job, f.code, f.detail, f.message)
        except OSError as e:
            code = "disk_full" if e.errno == 28 else "unknown"
            log.exception("Job %s failed with OSError", job_id)
            self._fail(job, code, str(e))
        except Exception as e:
            log.exception("Job %s crashed", job_id)
            self.state.record_error(f"Download crashed for job {job_id}", repr(e))
            self._fail(job, "unknown", repr(e))
        finally:
            self.cancelled.discard(job_id)
            self.procs.pop(job_id, None)
            shutil.rmtree(tmp, ignore_errors=True)

    def _fetch(self, job_id: int, url: str, tmp: Path, cookies: str) -> Path:
        ffdir = tools.ffmpeg_dir()
        if not ffdir:
            raise _Requeue("ffmpeg_missing")
        cmd = [
            *tools.ytdlp_cmd(), "--no-playlist", "--newline", "--no-mtime", "--no-colors",
            "-f", "bestaudio/best", "--ffmpeg-location", ffdir, "--socket-timeout", "30",
            "--progress-template", "download:DJPROG %(progress._percent_str)s",
            "-o", str(tmp / "source.%(ext)s"),
        ]
        if cookies:
            cmd += ["--cookies-from-browser", cookies]
        cmd += ["--", url]
        log.info("Job %s: downloading %s", job_id, url)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                env={**os.environ, "PATH": tools.SEARCH_PATH})
        self.procs[job_id] = proc
        if job_id in self.cancelled:
            proc.terminate()
        lines: list[str] = []
        last_update = 0.0
        self._set_job(job_id, stage="downloading")
        for line in proc.stdout:
            if line.startswith("DJPROG"):
                m = re.search(r"([\d.]+)%", line)
                if m and time.time() - last_update > 0.5:
                    last_update = time.time()
                    self._set_job(job_id, progress=float(m.group(1)) / 100)
                continue
            lines.append(line.rstrip())
            lines = lines[-40:]
        rc = proc.wait()
        if job_id in self.cancelled:
            raise _Cancelled()
        if rc != 0:
            detail = _error_detail(lines)
            log.warning("Job %s: yt-dlp exit %s: %s", job_id, rc, detail)
            code = classify_error("\n".join(lines))
            if code in ("disk_full", "ffmpeg_missing"):
                raise _Requeue(code)
            raise _Failure(code, detail, _no_formats_message(lines, cookies) if code == "no_formats" else None)
        sources = [p for p in tmp.glob("source.*") if p.suffix not in (".part", ".ytdl")]
        if not sources:
            raise _Failure("unknown", "yt-dlp finished but no audio file was produced: " + _error_detail(lines))
        return self._convert(job_id, sources[0], tmp / "audio.mp3", ffdir)

    def _ffmpeg(self, job_id: int, args: list[str], ffdir: str) -> str:
        proc = subprocess.Popen([str(Path(ffdir) / "ffmpeg"), "-nostdin", "-hide_banner", "-nostats", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs[job_id] = proc
        if job_id in self.cancelled:
            proc.terminate()
        out, _ = proc.communicate()
        if job_id in self.cancelled:
            raise _Cancelled()
        if proc.returncode != 0:
            code = classify_error(out)
            if code == "disk_full":
                raise _Requeue(code)
            raise _Failure("unknown", "Converting to MP3 failed: " + (out.strip().splitlines() or ["?"])[-1])
        return out

    def _convert(self, job_id: int, src: Path, dest: Path, ffdir: str) -> Path:
        self._set_job(job_id, stage="converting", progress=1.0)
        self._ffmpeg(job_id, ["-y", "-i", str(src), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                              "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-cutoff", MP3_LOWPASS_HZ, str(dest)], ffdir)
        return dest

    def _finish(self, job: dict, track: dict, mp3: Path):
        job_id = job["id"]
        self._set_job(job_id, stage="tagging")
        dur = tags.duration_ms(mp3)
        tag_error = None
        try:
            cover, mime = self._cover(track.get("cover_url"))
            tags.write_tags(mp3, track, cover, mime)
        except Exception as e:
            log.exception("Job %s: tagging failed", job_id)
            tag_error = f"Tags couldn't be written: {e}"

        if not self.state.library_reachable():
            raise _Requeue("library_missing")
        old = self.state.abs_path(track.get("file_rel_path"))
        if old:
            # Re-download keeps the same path on purpose: Rekordbox keeps its link to the file.
            dest = old
            if old.exists():
                library.trash(old)
        else:
            dest = library.unique_path(self.state.library, library.safe_filename(track["title"], track["artists"]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(mp3), dest)

        conn = self.state.conn()
        try:
            with db.tx(conn):
                conn.execute(
                    "UPDATE tracks SET file_rel_path = ?, file_duration_ms = ?, download_status = 'done', error_code = NULL, "
                    "error_msg = NULL, tag_error = ?, updated_at = datetime('now') WHERE id = ?",
                    (self.state.rel_path(dest), dur, tag_error, track["id"]),
                )
                if job.get("on_done"):
                    # The new copy is in: now the song points at its link, with its clean/explicit label.
                    done = json.loads(job["on_done"])
                    conn.execute("UPDATE tracks SET source_url = ?, clean_url = ?, clean_check = ?, clean_checked_at = ? WHERE id = ?",
                                 (job["url"], job["url"], json.dumps(done["clean_check"]), done["clean_check"].get("checked_at"), track["id"]))
                conn.execute("UPDATE jobs SET status = 'done', progress = 1, stage = '', updated_at = datetime('now') WHERE id = ?", (job_id,))
        finally:
            conn.close()
        log.info("Job %s: saved %s", job_id, dest.name)

    def _cover(self, url: str | None) -> tuple[bytes | None, str]:
        if not url:
            return None, "image/jpeg"
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            r.raise_for_status()
            return r.content, r.headers.get("content-type", "image/jpeg").split(";")[0]
        except httpx.HTTPError as e:
            log.warning("Cover art fetch failed (%s); saving without artwork", e)
            return None, "image/jpeg"

    def _fail(self, job: dict, code: str, detail: str, message: str | None = None):
        attempts = job["attempts"] + 1
        retryable = code in ("transient", "forbidden", "rate_limited") and attempts <= len(RETRY_DELAYS)
        if code == "forbidden" and not retryable:
            code = "ytdlp_outdated"
        conn = self.state.conn()
        try:
            with db.tx(conn):
                if retryable:
                    delay = RETRY_DELAYS[attempts - 1]
                    log.info("Job %s: %s, retry %d in %ss", job["id"], code, attempts, delay)
                    conn.execute(
                        "UPDATE jobs SET status = 'queued', attempts = ?, next_attempt_at = ?, error_code = ?, error_msg = ?, "
                        "stage = '', progress = 0, updated_at = datetime('now') WHERE id = ?",
                        (attempts, time.time() + delay, code, f"{friendly(code)} Retrying in {delay}s…", job["id"]),
                    )
                    conn.execute("UPDATE tracks SET download_status = 'queued' WHERE id = ?", (job["track_id"],))
                else:
                    msg = message or friendly(code, detail)
                    if job.get("on_done"):
                        msg = f"Swap not done: the file and its link are unchanged. {msg}"
                    conn.execute(
                        "UPDATE jobs SET status = 'failed', attempts = ?, error_code = ?, error_msg = ?, stage = '', "
                        "updated_at = datetime('now') WHERE id = ?",
                        (attempts, code, msg, job["id"]),
                    )
                    t = conn.execute("SELECT file_rel_path FROM tracks WHERE id = ?", (job["track_id"],)).fetchone()
                    has_file = bool(t and t["file_rel_path"] and self.state.abs_path(t["file_rel_path"]).is_file())
                    conn.execute(
                        "UPDATE tracks SET download_status = ?, error_code = ?, error_msg = ? WHERE id = ?",
                        ("done" if has_file else "error", code, msg, job["track_id"]),
                    )
        finally:
            conn.close()
        if retryable:
            threading.Timer(RETRY_DELAYS[attempts - 1] + 0.5, self.wake).start()


class _Failure(Exception):
    def __init__(self, code: str, detail: str, message: str | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.message = message   # a specific explanation, instead of the code's usual one
