"""The currently open library: path, database, lock, and its health.

Everything that touches the library asks `state` for a connection, so when the
library is missing / locked by another Mac / corrupt, every route fails the same
clear way and the UI can show the right dialog.
"""

import collections
import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import backup, db
from .config import update_config
from .errors import AppError

log = logging.getLogger("djcrate.state")

DJ_DIR_NAME = ".djcrate"


def computer_name() -> str:
    try:
        out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.node() or "this Mac"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AppState:
    def __init__(self):
        self.library: Path | None = None
        self.status = "needs_library"
        self.error = ""
        self.read_only = False
        self.lock_info: dict | None = None
        self.interrupted_jobs = 0
        self.host = computer_name()
        self.app_errors: collections.deque = collections.deque(maxlen=50)
        self._lock = threading.RLock()
        self.on_opened = []  # callbacks run after a library opens successfully

    # ------------------------------------------------------------ paths

    @property
    def dj_dir(self) -> Path:
        return self.library / DJ_DIR_NAME

    @property
    def db_path(self) -> Path:
        return self.dj_dir / "djcrate.db"

    @property
    def tmp_dir(self) -> Path:
        return self.dj_dir / "tmp"

    @property
    def backup_dir(self) -> Path:
        return self.dj_dir / "backups"

    @property
    def lock_path(self) -> Path:
        return self.dj_dir / "lock.json"

    def abs_path(self, rel: str | None) -> Path | None:
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else self.library / p

    def rel_path(self, p: Path) -> str:
        """Paths inside the library are stored relative so the folder can move."""
        try:
            return str(Path(p).resolve().relative_to(self.library.resolve()))
        except ValueError:
            return str(Path(p).resolve())

    def library_reachable(self) -> bool:
        return self.library is not None and self.library.is_dir()

    # ------------------------------------------------------------ open/close

    def open(self, path: Path, *, take_over=False, read_only=False) -> None:
        with self._lock:
            self.release_lock()
            path = Path(path).expanduser()
            self.library = path
            self.read_only = read_only
            self.lock_info = None
            self.error = ""
            update_config(library_path=str(path))
            if not path.is_dir():
                self.status = "library_missing"
                return
            new_library = not (path / DJ_DIR_NAME / "djcrate.db").exists()
            try:
                self.dj_dir.mkdir(exist_ok=True)
                self.tmp_dir.mkdir(exist_ok=True)
                self.backup_dir.mkdir(exist_ok=True)
            except OSError as e:
                self.status = "library_missing"
                self.error = f"Can't write to the library folder: {e}"
                return

            if not read_only and not take_over:
                other = self._foreign_lock()
                if other:
                    self.status = "lock_conflict"
                    self.lock_info = other
                    return

            if not new_library:
                conn = db.connect(self.db_path)
                try:
                    ok = db.integrity_ok(conn)
                finally:
                    conn.close()
                if not ok:
                    self.status = "db_corrupt"
                    self.error = "The DJ Crate database in this library is damaged."
                    return

            conn = db.connect(self.db_path)
            try:
                db.migrate(conn, backup_fn=lambda v: backup.premigration_backup(self, v))
            except Exception as e:
                log.exception("Migration failed")
                self.status = "migration_failed"
                self.error = str(e)
                return
            finally:
                conn.close()

            if not read_only:
                self._write_lock()
                self._recover_interrupted()
            self.status = "ok"
            log.info("Opened library %s (read_only=%s)", path, read_only)
        for cb in self.on_opened:
            try:
                cb()
            except Exception:
                log.exception("on_opened callback failed")

    def _foreign_lock(self) -> dict | None:
        try:
            info = json.loads(self.lock_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if info.get("host") == self.host:
            if info.get("pid") == os.getpid() or not pid_alive(int(info.get("pid", 0))):
                return None  # ours, or stale from a crash
        return info

    def _write_lock(self) -> None:
        info = {"host": self.host, "pid": os.getpid(), "since": time.strftime("%Y-%m-%d %H:%M")}
        try:
            self.lock_path.write_text(json.dumps(info))
        except OSError:
            log.exception("Couldn't write lock file")

    def release_lock(self) -> None:
        if self.library is None or self.read_only:
            return
        try:
            info = json.loads(self.lock_path.read_text())
            if info.get("host") == self.host and info.get("pid") == os.getpid():
                self.lock_path.unlink()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def _recover_interrupted(self) -> None:
        """After a crash/quit mid-download: clear temp files, park running jobs."""
        try:
            for child in self.tmp_dir.iterdir():
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        except OSError:
            log.exception("Couldn't clean tmp dir")
        conn = db.connect(self.db_path)
        try:
            with db.tx(conn):
                cur = conn.execute(
                    "UPDATE jobs SET status = 'interrupted', progress = 0, stage = '' WHERE status = 'running'"
                )
                conn.execute(
                    "UPDATE tracks SET download_status = 'queued' WHERE id IN (SELECT track_id FROM jobs WHERE status = 'interrupted')"
                )
            self.interrupted_jobs = conn.execute("SELECT count(*) FROM jobs WHERE status = 'interrupted'").fetchone()[0]
            if cur.rowcount:
                log.info("%d downloads were interrupted", cur.rowcount)
        finally:
            conn.close()

    # ------------------------------------------------------------ access

    def conn(self):
        if self.status != "ok":
            raise AppError("library_unavailable", "The library isn't open.", 409)
        if not self.library_reachable():
            self.status = "library_missing"
            raise AppError("library_missing", "Library not found — reconnect the drive or choose the folder.", 409)
        return db.connect(self.db_path)

    def record_error(self, message: str, detail: str = "") -> None:
        self.app_errors.appendleft({"time": time.strftime("%H:%M:%S"), "message": message, "detail": detail})


state = AppState()
