"""Auto-fill links: for the songs in a view that need a link, use the top Find result
that is fully green (length within CLOSE_MS of Spotify's and no version warnings like
"Remix", "Live" or "Clean"). Songs without one stay in Needs Link for you to pick.

Runs only when you click it, one search at a time (saved Find results are reused),
pauses when YouTube rate-limits, never touches a song that already has a link, and
never downloads: filled songs get an "Auto" marker until you download or edit them.
"""

import logging
import threading
import time

from . import db, yt_search
from .errors import AppError

log = logging.getLogger("djcrate.autofill")

GAP_S = 2.0   # between two real YouTube searches (saved results need no gap)

# A song that needs a link: no link, and nothing downloaded for it.
NEEDS_LINK = "coalesce(t.source_url, '') = '' AND t.download_status NOT IN ('done', 'missing')"


def pick(results: list[dict]) -> dict | None:
    """The top fully green result, or None if none is."""
    return next((r for r in results if r.get("close") and not r.get("flags")), None)


class AutoFiller:
    def __init__(self, state, on_fill=None):
        self.state = state
        self.on_fill = on_fill            # called after a link is filled (wakes the clean check)
        self.lock = threading.Lock()
        self.wake_event = threading.Event()
        self.queue: list[int] = []
        self.run: dict | None = None      # the current (or last) run
        self.paused_until = 0.0
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="autofill", daemon=True)
        self.thread.start()

    # ------------------------------------------------------------ control

    def begin(self, track_ids: list[int], view: str) -> dict:
        with self.lock:
            if self.run and self.run["running"]:
                raise AppError("autofill_busy", "Auto-fill is already running. Stop it first, or wait for it to finish.", 409)
            self.queue = list(dict.fromkeys(track_ids))
            self.run = {"running": bool(self.queue), "view": view, "total": len(self.queue), "done": 0, "filled": 0,
                        "unsure": 0, "failed": 0, "started_at": time.time(), "finished_at": None if self.queue else time.time(),
                        "stopped": False}
        self.wake_event.set()
        return self.status()

    def stop(self) -> dict:
        with self.lock:
            self.queue = []
            if self.run and self.run["running"]:
                self.run.update(running=False, stopped=True, finished_at=time.time())
        return self.status()

    def status(self) -> dict | None:
        with self.lock:
            if not self.run:
                return None
            return {**self.run, "paused_until": self.paused_until if time.time() < self.paused_until else None}

    # ------------------------------------------------------------ work

    def step(self) -> bool:
        """Handle the next song. True if it did something, False if idle or paused."""
        if time.time() < self.paused_until or self.state.status != "ok" or self.state.read_only:
            return False
        with self.lock:
            if not self.queue:
                return False
            tid = self.queue[0]
        conn = self.state.conn()
        searched = False
        try:
            row = conn.execute(f"SELECT t.* FROM tracks t WHERE t.id = ? AND {NEEDS_LINK} AND {db.ACTIVE}", (tid,)).fetchone()
            outcome = "skipped"
            if row:
                track = dict(row)
                searched = yt_search.cached(conn, track) is None
                try:
                    found = pick(yt_search.search(track, conn)["results"])
                except yt_search.RateLimited:
                    self.paused_until = time.time() + yt_search.RATE_LIMIT_PAUSE_S
                    log.warning("Auto-fill paused for %ss (YouTube is rate-limiting searches)", yt_search.RATE_LIMIT_PAUSE_S)
                    return False   # same song again after the pause
                except AppError as e:
                    log.info("Auto-fill couldn't search %s: %s", tid, e.message)
                    found, outcome = None, "failed"
                if found:
                    with db.tx(conn):
                        cur = conn.execute(
                            "UPDATE tracks SET source_url = ?, link_auto = 1, updated_at = datetime('now') "
                            "WHERE id = ? AND coalesce(source_url, '') = ''", (found["url"], tid))
                    outcome = "filled" if cur.rowcount else "skipped"
                    if cur.rowcount:
                        log.info("Auto-filled %s – %s: %s", track["artists"], track["title"], found["url"])
                        if self.on_fill:
                            self.on_fill()
                elif outcome != "failed":
                    outcome = "unsure"
        finally:
            conn.close()
        with self.lock:
            if self.queue and self.queue[0] == tid:
                self.queue.pop(0)
            if self.run and self.run["running"]:
                self.run["done"] += 1
                if outcome in ("filled", "unsure", "failed"):
                    self.run[outcome] += 1
                if not self.queue:
                    self.run.update(running=False, finished_at=time.time())
        if searched:
            time.sleep(GAP_S)
        return True

    def _loop(self):
        while True:
            try:
                if self.step():
                    continue
            except Exception:
                log.exception("Auto-fill error")
                with self.lock:   # don't get stuck on one song
                    if self.queue:
                        self.queue.pop(0)
            self.wake_event.wait(5)
            self.wake_event.clear()
