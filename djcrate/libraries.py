"""Several libraries on one Mac (e.g. two people sharing DJ Crate).

Each library is a self-contained folder: its own music, crates, notes, sets
and Spotify login. config.json remembers which ones you've opened, which one
was last, and whether to ask on launch.
"""

import datetime as dt
import sqlite3
from pathlib import Path

from .config import LIBRARIES_DIR, load_config, update_config
from .errors import AppError
from .library import safe_filename
from .state import DJ_DIR_NAME

MAX_RECENT = 20


def _recent() -> list[dict]:
    cfg = load_config()
    items = list(cfg.get("libraries") or [])
    # Older configs only had library_path.
    if not items and cfg.get("library_path"):
        items = [{"path": cfg["library_path"], "last_opened": None}]
    return items


def remember(path: Path) -> None:
    path = str(Path(path).expanduser())
    items = [i for i in _recent() if i["path"] != path]
    items.insert(0, {"path": path, "last_opened": dt.datetime.now().isoformat(timespec="seconds")})
    update_config(libraries=items[:MAX_RECENT], library_path=path)


def forget(path: str) -> None:
    items = [i for i in _recent() if i["path"] != path]
    cfg = update_config(libraries=items)
    if cfg.get("library_path") == path:
        update_config(library_path=items[0]["path"] if items else None)


def ask_on_launch() -> bool:
    return bool(load_config().get("ask_on_launch", True))


def set_ask_on_launch(value: bool) -> None:
    update_config(ask_on_launch=bool(value))


def last_library() -> Path | None:
    p = load_config().get("library_path")
    return Path(p) if p else None


def _peek(path: Path) -> tuple[str | None, int | None]:
    """Name + song count without opening (or locking) the library."""
    db_path = path / DJ_DIR_NAME / "djcrate.db"
    if not db_path.exists():
        return None, 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = 'library_name'").fetchone()
            count = conn.execute("SELECT count(*) FROM tracks").fetchone()[0]
            return (row[0] if row and row[0] else None), count
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None, None


def describe(current: Path | None) -> list[dict]:
    out = []
    for item in _recent():
        p = Path(item["path"])
        exists = p.is_dir()
        name, tracks = _peek(p) if exists else (None, None)
        out.append({
            "path": str(p), "name": name or p.name, "exists": exists, "tracks": tracks,
            "last_opened": item.get("last_opened"), "current": current is not None and p == current,
        })
    return out


def new_library_path(name: str) -> Path:
    name = (name or "").strip()
    if not name:
        raise AppError("bad_name", "Give the library a name, e.g. your name.")
    folder = LIBRARIES_DIR / safe_filename(name, "")
    if folder.exists() and any(folder.iterdir()):
        raise AppError("exists", f"There's already a library folder called “{folder.name}”. Pick another name, or open it with Open a folder….")
    folder.mkdir(parents=True, exist_ok=True)
    return folder
