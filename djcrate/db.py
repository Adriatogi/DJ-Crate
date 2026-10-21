"""SQLite schema, migrations and shared query helpers.

One connection per request/thread (sqlite connections are cheap). WAL mode so
the download workers and the API can read/write at the same time.
"""

import contextlib
import logging
import re
import sqlite3
import unicodedata
from pathlib import Path

log = logging.getLogger("djcrate.db")

LENGTH_TOLERANCE_MS = 5000

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE tracks (
        id               INTEGER PRIMARY KEY,
        spotify_id       TEXT UNIQUE,
        isrc             TEXT,
        title            TEXT NOT NULL,
        artists          TEXT NOT NULL DEFAULT '',
        album            TEXT NOT NULL DEFAULT '',
        release_date     TEXT NOT NULL DEFAULT '',
        duration_ms      INTEGER,
        cover_url        TEXT,
        spotify_url      TEXT,
        source_url       TEXT,
        file_rel_path    TEXT,
        file_duration_ms INTEGER,
        download_status  TEXT NOT NULL DEFAULT 'none',
        error_code       TEXT,
        error_msg        TEXT,
        tag_error        TEXT,
        edited           INTEGER NOT NULL DEFAULT 0,
        cleaned          INTEGER NOT NULL DEFAULT 0,
        verified         INTEGER NOT NULL DEFAULT 0,
        notes            TEXT NOT NULL DEFAULT '',
        is_manual        INTEGER NOT NULL DEFAULT 0,
        norm_key         TEXT NOT NULL DEFAULT '',
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_tracks_isrc ON tracks(isrc);
    CREATE INDEX idx_tracks_norm ON tracks(norm_key);
    CREATE INDEX idx_tracks_source ON tracks(source_url);

    CREATE TABLE crates (
        id                  INTEGER PRIMARY KEY,
        name                TEXT NOT NULL,
        parent_id           INTEGER REFERENCES crates(id) ON DELETE CASCADE,
        kind                TEXT NOT NULL CHECK (kind IN ('folder', 'crate', 'spotify')),
        position            INTEGER NOT NULL DEFAULT 0,
        spotify_playlist_id TEXT,
        last_synced         TEXT,
        created_at          TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE crate_tracks (
        crate_id            INTEGER NOT NULL REFERENCES crates(id) ON DELETE CASCADE,
        track_id            INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        position            INTEGER NOT NULL DEFAULT 0,
        removed_from_source INTEGER NOT NULL DEFAULT 0,
        added_at            TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (crate_id, track_id)
    );
    CREATE INDEX idx_crate_tracks_track ON crate_tracks(track_id);

    CREATE TABLE sets (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        notes      TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE set_items (
        id              INTEGER PRIMARY KEY,
        set_id          INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
        position        INTEGER NOT NULL DEFAULT 0,
        track_id        INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
        label           TEXT NOT NULL DEFAULT '',
        free_text       TEXT,
        cue_time        TEXT NOT NULL DEFAULT '',
        transition_note TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE jobs (
        id              INTEGER PRIMARY KEY,
        track_id        INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL DEFAULT 'download',
        status          TEXT NOT NULL DEFAULT 'queued',
        replace_file    INTEGER NOT NULL DEFAULT 0,
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        progress        REAL NOT NULL DEFAULT 0,
        stage           TEXT NOT NULL DEFAULT '',
        error_code      TEXT,
        error_msg       TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_jobs_status ON jobs(status);
    """,
    # v2: Set Setter — how long a song actually plays in the set (mix in late / out early)
    """
    ALTER TABLE set_items ADD COLUMN length_ms INTEGER
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)

CHECK_VERSION_SQL = (
    "(t.download_status = 'done' AND t.edited = 0 AND t.duration_ms IS NOT NULL "
    f"AND t.file_duration_ms IS NOT NULL AND abs(t.file_duration_ms - t.duration_ms) > {LENGTH_TOLERANCE_MS})"
)

SMART_CRATES: dict[str, tuple[str, str]] = {
    "all": ("All Tracks", "1"),
    "needs_link": ("Needs Link", "(coalesce(t.source_url, '') = '' AND t.download_status NOT IN ('done', 'missing'))"),
    "ready": ("Ready to Download", "(coalesce(t.source_url, '') <> '' AND t.download_status IN ('none', 'error'))"),
    "downloaded": ("Downloaded", "t.download_status = 'done'"),
    "cleaned": ("Cleaned", "t.cleaned = 1"),
    "verified": ("Verified", "t.verified = 1"),
    "check_version": ("Check Version", CHECK_VERSION_SQL),
    "missing": ("Missing Files", "t.download_status = 'missing'"),
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """Explicit write transaction: all or nothing."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else 0


def migrate(conn: sqlite3.Connection, backup_fn=None) -> None:
    """Apply pending migrations, each in its own transaction.

    `backup_fn(version)` is called before migrating an existing database, so a
    failed migration never leaves you without a copy.
    """
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"This library was created by a newer DJ Crate (schema v{version}, this app knows v{SCHEMA_VERSION}). Update the app."
        )
    if version == SCHEMA_VERSION:
        return
    if version > 0 and backup_fn:
        backup_fn(version)
    for target in range(version + 1, SCHEMA_VERSION + 1):
        log.info("Migrating database to v%d", target)
        # executescript() commits implicitly, so run statements one by one in a real transaction.
        statements = [s.strip() for s in MIGRATIONS[target - 1].split(";") if s.strip()]
        with tx(conn):
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (target,))


def integrity_ok(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return row is not None and row[0] == "ok"
    except sqlite3.DatabaseError:
        return False


# ---------------------------------------------------------------- settings

def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------- tracks

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_key(title: str, artists: str) -> str:
    return f"{normalize(artists)}|{normalize(title)}"


def find_existing_track(conn, *, spotify_id=None, isrc=None, title="", artists=""):
    """Dedup: spotify_id first, then ISRC, then normalized artist+title."""
    if spotify_id:
        row = conn.execute("SELECT * FROM tracks WHERE spotify_id = ?", (spotify_id,)).fetchone()
        if row:
            return row
    if isrc:
        row = conn.execute("SELECT * FROM tracks WHERE isrc = ? ORDER BY id LIMIT 1", (isrc.upper(),)).fetchone()
        if row:
            return row
    key = norm_key(title, artists)
    if key != "|":
        row = conn.execute("SELECT * FROM tracks WHERE norm_key = ? ORDER BY id LIMIT 1", (key,)).fetchone()
        if row:
            return row
    return None


TRACK_FIELDS = (
    "spotify_id", "isrc", "title", "artists", "album", "release_date", "duration_ms",
    "cover_url", "spotify_url", "source_url", "file_rel_path", "file_duration_ms",
    "download_status", "is_manual",
)


def upsert_track(conn, meta: dict) -> tuple[int, bool]:
    """Insert a track or return the existing one. Returns (track_id, created).

    Existing rows only get *empty* fields filled in, so your edits, links,
    checkmarks and notes are never overwritten by an import.
    """
    if meta.get("isrc"):
        meta["isrc"] = meta["isrc"].upper()
    existing = find_existing_track(
        conn, spotify_id=meta.get("spotify_id"), isrc=meta.get("isrc"),
        title=meta.get("title", ""), artists=meta.get("artists", ""),
    )
    if existing:
        fills = {}
        for field in ("isrc", "album", "release_date", "duration_ms", "cover_url", "spotify_url"):
            if meta.get(field) and not existing[field]:
                fills[field] = meta[field]
        if meta.get("spotify_id") and not existing["spotify_id"]:
            taken = conn.execute("SELECT 1 FROM tracks WHERE spotify_id = ?", (meta["spotify_id"],)).fetchone()
            if not taken:
                fills["spotify_id"] = meta["spotify_id"]
        if fills:
            sets = ", ".join(f"{k} = ?" for k in fills)
            conn.execute(f"UPDATE tracks SET {sets}, updated_at = datetime('now') WHERE id = ?", (*fills.values(), existing["id"]))
        return existing["id"], False
    cols = [f for f in TRACK_FIELDS if meta.get(f) is not None]
    vals = [meta[f] for f in cols]
    cols.append("norm_key")
    vals.append(norm_key(meta.get("title", ""), meta.get("artists", "")))
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO tracks ({', '.join(cols)}) VALUES ({placeholders})", vals)
    return cur.lastrowid, True


def track_to_dict(row) -> dict:
    d = dict(row)
    for flag in ("edited", "cleaned", "verified", "is_manual"):
        d[flag] = bool(d.get(flag))
    d["check_version"] = bool(
        d["download_status"] == "done" and not d["edited"] and d["duration_ms"] and d["file_duration_ms"]
        and abs(d["file_duration_ms"] - d["duration_ms"]) > LENGTH_TOLERANCE_MS
    )
    if "removed_from_source" in d:
        d["removed_from_source"] = bool(d["removed_from_source"])
    return d


def get_track(conn, track_id: int):
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return track_to_dict(row) if row else None


def add_to_crate(conn, crate_id: int, track_ids) -> int:
    row = conn.execute("SELECT coalesce(max(position), -1) FROM crate_tracks WHERE crate_id = ?", (crate_id,)).fetchone()
    pos = row[0] + 1
    added = 0
    for tid in track_ids:
        cur = conn.execute(
            "INSERT OR IGNORE INTO crate_tracks (crate_id, track_id, position) VALUES (?, ?, ?)",
            (crate_id, tid, pos),
        )
        if cur.rowcount:
            pos += 1
            added += 1
    return added


def crate_descendants(conn, crate_id: int) -> list[int]:
    rows = conn.execute(
        """WITH RECURSIVE sub(id) AS (
               SELECT id FROM crates WHERE id = ?
               UNION ALL SELECT c.id FROM crates c JOIN sub ON c.parent_id = sub.id)
           SELECT id FROM sub""",
        (crate_id,),
    ).fetchall()
    return [r[0] for r in rows]
