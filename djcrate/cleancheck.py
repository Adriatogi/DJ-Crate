"""Background clean-version checks: works through songs with a YouTube link and
asks YouTube Music whether that exact video is the explicit one (see ytmusic.py).

A check belongs to the link it was made for (`clean_url`), so changing a song's
link (or swapping to the explicit version) makes it due again automatically.
Results only ever *flag* a song; swapping is always your click.
"""

import json
import logging
import threading
import time

from . import db, ytmusic

log = logging.getLogger("djcrate.cleancheck")

PAUSE_S = 300            # back off this long when YouTube Music is busy or unreachable
IDLE_S = 10              # how often to look for new work when there is nothing to do
UNKNOWN_RETRY_S = 86400  # try "couldn't check" songs again after a day

# Songs with a YouTube link that haven't been checked for that link yet (or the check came back unknown a while ago).
DUE = """
FROM tracks t
WHERE (t.source_url LIKE '%youtube.com/%' OR t.source_url LIKE '%youtu.be/%')
  AND t.trashed_at IS NULL AND t.hidden_at IS NULL
  AND (t.clean_url IS NULL OR t.clean_url <> t.source_url
       OR coalesce(json_extract(t.clean_check, '$.v'), 0) < {rules}
       OR (json_extract(t.clean_check, '$.result') = 'unknown' AND t.clean_checked_at < ?))
""".format(rules=ytmusic.RULES)


def due_count(conn) -> int:
    return conn.execute("SELECT count(*) " + DUE, (time.time() - UNKNOWN_RETRY_S,)).fetchone()[0]


def store(conn, track: dict, result: dict) -> None:
    """Save a check for the link it was made for."""
    with db.tx(conn):
        conn.execute(
            "UPDATE tracks SET clean_check = ?, clean_checked_at = ?, clean_url = ? WHERE id = ?",
            (json.dumps(result), result.get("checked_at", time.time()), track["source_url"], track["id"]))


def check_track(conn, track: dict, cl=None) -> dict:
    """Check one song now and save it. Raises ytmusic.YTMError if YouTube Music is unreachable."""
    result = ytmusic.check(track, cl or ytmusic.interactive_client())
    store(conn, track, result)
    log.info("Clean check: %s – %s → %s", track.get("artists"), track.get("title"), result["result"])
    return result


def status(conn, checker=None) -> dict:
    return {
        "on": db.get_setting(conn, "clean_check_on", "1") == "1",
        "pending": due_count(conn),
        "flagged": conn.execute(f"SELECT count(*) FROM tracks t WHERE {db.ACTIVE} AND " + db.CLEAN_SQL).fetchone()[0],
        "checked": conn.execute("SELECT count(*) FROM tracks WHERE clean_url IS NOT NULL AND clean_url = source_url").fetchone()[0],
        "linked": conn.execute("SELECT count(*) FROM tracks WHERE source_url LIKE '%youtube.com/%' OR source_url LIKE '%youtu.be/%'").fetchone()[0],
        "songs": conn.execute("SELECT count(*) FROM tracks").fetchone()[0],
        "last_flag": checker.last_flag if checker else None,
        "paused_until": checker.paused_until if checker and checker.paused else None,
    }


class Checker:
    def __init__(self, state, client_factory=ytmusic.Client):
        self.state = state
        self.client_factory = client_factory
        self.client = None
        self.paused_until = 0.0
        self.last_flag = None   # {track_id, title, artists, at}: the newest song found to be a clean version
        self.wake_event = threading.Event()
        self.thread = None

    @property
    def paused(self) -> bool:
        return time.time() < self.paused_until

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="clean-check", daemon=True)
        self.thread.start()

    def wake(self):
        self.wake_event.set()

    def pause(self):
        self.paused_until = time.time() + PAUSE_S
        log.warning("Clean checks paused for %ss", PAUSE_S)

    def step(self) -> bool:
        """Check one song. True if it did (or tried) work, False if idle."""
        if self.state.status != "ok" or self.state.read_only or self.paused:
            return False
        conn = self.state.conn()
        try:
            if db.get_setting(conn, "clean_check_on", "1") != "1":
                return False
            # Newest changes first, so a song you just downloaded is checked right away.
            row = conn.execute("SELECT t.* " + DUE + " ORDER BY t.updated_at DESC, t.id DESC LIMIT 1",
                               (time.time() - UNKNOWN_RETRY_S,)).fetchone()
            if not row:
                return False
            track = dict(row)
            self.client = self.client or self.client_factory()
            try:
                result = check_track(conn, track, self.client)
            except ytmusic.YTMError as e:
                log.info("Clean check deferred: %s", e)
                self.pause()
                return False
            if result["result"] == "clean":
                self.last_flag = {"track_id": track["id"], "title": track["title"], "artists": track["artists"], "at": time.time()}
            return True
        finally:
            conn.close()

    def _loop(self):
        while True:
            try:
                if self.step():
                    continue  # the client itself spaces requests out
            except Exception:
                log.exception("Clean check error")
            self.wake_event.wait(IDLE_S)
            self.wake_event.clear()
