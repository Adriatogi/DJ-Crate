"""'Find YouTube link': top results with length match and mix-label warnings.
It only suggests; you always pick the link."""

import json
import logging
import re
import subprocess
import threading
import time
from urllib.parse import quote_plus

from . import tools
from .downloader import classify_error, friendly
from .errors import AppError

log = logging.getLogger("djcrate.yt_search")

CLOSE_MS = 3000
RESULTS = 5
CACHE_DAYS = 7
AHEAD_DEFAULT = 3
AHEAD_MAX = 10
AHEAD_GAP_S = 2.0          # pause between background searches
RATE_LIMIT_PAUSE_S = 180   # stop searching ahead this long after YouTube pushes back

# Version words that mean "a different recording/edit than the one on Spotify".
MIX_LABELS = [
    "extended mix", "extended", "radio edit", "club mix", "original mix", "remix", "vip", "bootleg", "edit",
    "live", "acoustic", "instrumental", "acapella", "a cappella", "sped up", "slowed", "nightcore", "reverb",
    "8d", "cover", "karaoke", "bass boosted", "remastered", "remaster", "clean", "dirty", "music video",
]


def _labels(text: str) -> set[str]:
    low = (text or "").lower()
    found = set()
    for label in sorted(MIX_LABELS, key=len, reverse=True):
        pattern = r"\b" + re.escape(label) + r"\b"
        if re.search(pattern, low) and not any(label in f for f in found):
            found.add(label)
    return found


def mix_flags(spotify_title: str, result_title: str) -> list[str]:
    want, got = _labels(spotify_title), _labels(result_title)
    flags = [f"Says “{l.title()}” — not in the Spotify title" for l in sorted(got - want)]
    flags += [f"Missing “{l.title()}”" for l in sorted(want - got)]
    return flags


def query_for(track: dict) -> str:
    return f"{track.get('artists', '')} - {track.get('title', '')}".strip(" -")


def search_url(track: dict) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(query_for(track))


def rate_limited(stderr: str) -> bool:
    low = (stderr or "").lower()
    return classify_error(stderr) == "rate_limited" or "not a bot" in low or "not a robot" in low


class RateLimited(AppError):
    pass


def _run_search(track: dict) -> list[dict]:
    q = query_for(track)
    cmd = [*tools.ytdlp_cmd(), "--flat-playlist", "-J", "--no-warnings", "--socket-timeout", "20", f"ytsearch{RESULTS}:{q}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise AppError("search_failed", "YouTube search timed out.", 502, search_url=search_url(track))
    if out.returncode != 0:
        if rate_limited(out.stderr):
            log.warning("Search rate-limited: %s", out.stderr[-300:])
            raise RateLimited("rate_limited", "YouTube is rate-limiting searches right now — wait a few minutes, or open the search in your browser.",
                              429, search_url=search_url(track), reason="rate_limited")
        code = classify_error(out.stderr)
        log.warning("Search failed (%s): %s", code, out.stderr[-500:])
        msg = friendly(code) if code != "unknown" else "YouTube search failed."
        raise AppError("search_failed", msg, 502, search_url=search_url(track), reason=code)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        raise AppError("search_failed", "YouTube search returned something unexpected.", 502, search_url=search_url(track))
    results = []
    for e in data.get("entries") or []:
        if not e or not e.get("id"):
            continue
        results.append({
            "url": e.get("url") if str(e.get("url", "")).startswith("http") else f"https://www.youtube.com/watch?v={e['id']}",
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration_ms": int(e["duration"] * 1000) if e.get("duration") else None,
        })
    return results


def _annotate(track: dict, raw: list[dict]) -> list[dict]:
    """Length match + mix-label warnings, computed fresh so they follow the song's current info."""
    target = track.get("duration_ms")
    out = []
    for r in raw:
        diff = (r["duration_ms"] - target) if (r.get("duration_ms") is not None and target) else None
        out.append({**r, "diff_ms": diff, "close": diff is not None and abs(diff) <= CLOSE_MS,
                    "flags": mix_flags(track.get("title", ""), r.get("title") or "")})
    return out


def cached(conn, track: dict) -> tuple[list[dict], float] | None:
    row = conn.execute("SELECT * FROM yt_search_cache WHERE track_id = ?", (track["id"],)).fetchone()
    if not row or row["query"] != query_for(track) or time.time() - row["fetched_at"] > CACHE_DAYS * 86400:
        return None
    return json.loads(row["results_json"]), row["fetched_at"]


def _store(conn, track: dict, raw: list[dict]) -> float:
    now = time.time()
    conn.execute(
        "INSERT INTO yt_search_cache (track_id, query, results_json, fetched_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(track_id) DO UPDATE SET query = excluded.query, results_json = excluded.results_json, fetched_at = excluded.fetched_at",
        (track["id"], query_for(track), json.dumps(raw), now),
    )
    return now


def search(track: dict, conn=None, refresh: bool = False) -> dict:
    """Saved results if we have fresh ones (same artist/title, under a week old), else search."""
    hit = cached(conn, track) if conn is not None and not refresh else None
    if hit:
        raw, fetched_at = hit
    else:
        raw = _run_search(track)
        fetched_at = _store(conn, track, raw) if conn is not None else time.time()
    return {"query": query_for(track), "search_url": search_url(track), "results": _annotate(track, raw),
            "cached_at": fetched_at if hit else None}


class Prefetcher:
    """Searches the next few songs in the background so Find opens instantly.

    One search at a time with a gap between them; skips songs that already have
    a link or saved results; stops for a few minutes if YouTube rate-limits."""

    def __init__(self, state):
        self.state = state
        self.queue: list[int] = []
        self.cond = threading.Condition()
        self.paused_until = 0.0
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="search-ahead", daemon=True)
        self.thread.start()

    @property
    def paused(self) -> bool:
        return time.time() < self.paused_until

    def request(self, track_ids: list[int], limit: int) -> int:
        if self.paused or limit <= 0:
            return 0
        ids = [t for t in track_ids if t][:limit]
        with self.cond:
            self.queue = ids  # newest request wins: you've moved on down the list
            self.cond.notify()
        return len(ids)

    def pause(self):
        self.paused_until = time.time() + RATE_LIMIT_PAUSE_S
        with self.cond:
            self.queue = []
        log.warning("Search-ahead paused for %ss (rate-limited)", RATE_LIMIT_PAUSE_S)

    def step(self) -> bool:
        """Search the next queued song. Returns False when there was nothing to do."""
        with self.cond:
            if not self.queue or self.paused:
                return False
            tid = self.queue.pop(0)
        if self.state.status != "ok":
            return False
        conn = self.state.conn()
        try:
            row = conn.execute("SELECT * FROM tracks WHERE id = ?", (tid,)).fetchone()
            if not row or (row["source_url"] or "").strip():
                return True
            track = dict(row)
            if cached(conn, track):
                return True
            try:
                search(track, conn)
                log.info("Searched ahead: %s", query_for(track))
            except RateLimited:
                self.pause()
            except AppError as e:
                log.info("Search-ahead skipped %s: %s", tid, e.message)
        finally:
            conn.close()
        return True

    def _loop(self):
        while True:
            with self.cond:
                while not self.queue or self.paused:
                    self.cond.wait(timeout=5)
            try:
                if self.step():
                    time.sleep(AHEAD_GAP_S)
            except Exception:
                log.exception("Search-ahead error")
