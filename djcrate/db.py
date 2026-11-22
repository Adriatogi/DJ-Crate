"""SQLite schema, migrations and shared query helpers.

One connection per request/thread (sqlite connections are cheap). WAL mode so
the download workers and the API can read/write at the same time.
"""

import contextlib
import json
import logging
import re
import sqlite3
import unicodedata
from pathlib import Path

log = logging.getLogger("djcrate.db")

LENGTH_TOLERANCE_MS = 5000

MIGRATIONS: list = [  # each entry: SQL text, or a function(conn) for conversions
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
    # v3: prep flags simplified — Cleaned/Verified become one "Ready" flag (from
    # Verified), plus "Broken" for songs that need fixing.
    """
    ALTER TABLE tracks RENAME COLUMN verified TO ready;
    ALTER TABLE tracks DROP COLUMN cleaned;
    ALTER TABLE tracks ADD COLUMN broken INTEGER NOT NULL DEFAULT 0
    """,
]

V4_SQL = """
CREATE TABLE mixes (
    id         INTEGER PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE mix_songs (
    mix_id   INTEGER NOT NULL REFERENCES mixes(id) ON DELETE CASCADE,
    track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    label    TEXT NOT NULL DEFAULT '',
    side     TEXT NOT NULL CHECK (side IN ('in', 'out')),
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_mix_songs_track ON mix_songs(track_id);
CREATE INDEX idx_mix_songs_mix ON mix_songs(mix_id);
CREATE TABLE samples (
    id              INTEGER PRIMARY KEY,
    track_id        INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    sample_track_id INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    sample_label    TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    position        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_samples_track ON samples(track_id);
CREATE INDEX idx_samples_sample ON samples(sample_track_id);
CREATE TABLE board_layout (
    board_track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    node_kind      TEXT NOT NULL CHECK (node_kind IN ('song', 'mix')),
    node_id        INTEGER NOT NULL,
    x              REAL NOT NULL,
    y              REAL NOT NULL,
    PRIMARY KEY (board_track_id, node_kind, node_id)
);
CREATE TABLE set_nodes (
    id          INTEGER PRIMARY KEY,
    set_id      INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('song', 'mix', 'text')),
    track_id    INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
    mix_id      INTEGER REFERENCES mixes(id) ON DELETE SET NULL,
    text        TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    cue_time    TEXT NOT NULL DEFAULT '',
    length_ms   INTEGER,
    x           REAL NOT NULL DEFAULT 0,
    y           REAL NOT NULL DEFAULT 0
);
CREATE INDEX idx_set_nodes_set ON set_nodes(set_id);
CREATE TABLE set_edges (
    id        INTEGER PRIMARY KEY,
    set_id    INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
    from_node INTEGER NOT NULL REFERENCES set_nodes(id) ON DELETE CASCADE,
    to_node   INTEGER NOT NULL REFERENCES set_nodes(id) ON DELETE CASCADE,
    main      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_set_edges_set ON set_edges(set_id);
CREATE TABLE yt_search_cache (
    track_id     INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    query        TEXT NOT NULL,
    results_json TEXT NOT NULL,
    fetched_at   REAL NOT NULL
)
"""

SET_X_SONG, SET_X_GAP = 300, 280


def _v4_boards(conn) -> None:
    """Mix graph, samples, boards, search cache — and turn every existing set's
    list into a board whose main path is the old order."""
    for stmt in _statements(V4_SQL):
        conn.execute(stmt)
    for (set_id,) in conn.execute("SELECT id FROM sets").fetchall():
        items = conn.execute("SELECT * FROM set_items WHERE set_id = ? ORDER BY position", (set_id,)).fetchall()
        x, prev = 0.0, None

        def add(kind, **f):
            nonlocal x, prev
            cur = conn.execute(
                "INSERT INTO set_nodes (set_id, kind, track_id, text, label, cue_time, length_ms, x, y) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (set_id, kind, f.get("track_id"), f.get("text", ""), f.get("label", ""), f.get("cue_time", ""), f.get("length_ms"), x),
            )
            if prev is not None:
                conn.execute("INSERT INTO set_edges (set_id, from_node, to_node, main) VALUES (?, ?, ?, 1)", (set_id, prev, cur.lastrowid))
            prev = cur.lastrowid
            x += SET_X_SONG if kind != "mix" else SET_X_GAP

        for it in items:
            if it["free_text"] is not None:
                add("text", text=it["free_text"], cue_time=it["cue_time"])
            else:
                add("song", track_id=it["track_id"], label=it["label"], cue_time=it["cue_time"], length_ms=it["length_ms"])
            if (it["transition_note"] or "").strip():
                add("mix", text=it["transition_note"].strip())


MIGRATIONS.append(_v4_boards)  # v4

MIGRATIONS.append(  # v5: sample lookup (Genius)
    """
    ALTER TABLE samples ADD COLUMN url TEXT NOT NULL DEFAULT '';
    ALTER TABLE samples ADD COLUMN relation TEXT NOT NULL DEFAULT 'sample';
    CREATE TABLE genius_cache (
        track_id   INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
        genius_id  INTEGER,
        match_json TEXT NOT NULL,
        song_json  TEXT,
        fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """
)

MIGRATIONS.append(  # v6: "sampled in" songs you don't have, kept as text on the sampled song
    "ALTER TABLE samples ADD COLUMN direction TEXT NOT NULL DEFAULT 'out';"
)

MIGRATIONS.append(  # v7: BPM and key, read from a Rekordbox XML export
    """
    ALTER TABLE tracks ADD COLUMN bpm REAL;
    ALTER TABLE tracks ADD COLUMN musical_key TEXT
    """
)

MIGRATIONS.append(  # v8: Genre Rack (your own genre vocabulary), live genre crates, genre suggestions
    """
    CREATE TABLE genres (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        norm       TEXT NOT NULL UNIQUE,
        parent_id  INTEGER REFERENCES genres(id),
        position   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE genre_aliases (
        norm     TEXT PRIMARY KEY,
        genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE
    );
    CREATE TABLE genre_ignored (norm TEXT PRIMARY KEY);
    CREATE TABLE track_genres (
        track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
        position INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (track_id, genre_id)
    );
    CREATE INDEX idx_track_genres_genre ON track_genres(genre_id);
    ALTER TABLE tracks ADD COLUMN rekordbox_genre TEXT;
    ALTER TABLE tracks ADD COLUMN file_genre TEXT;
    ALTER TABLE crates ADD COLUMN genre_id INTEGER REFERENCES genres(id) ON DELETE SET NULL;
    CREATE TABLE genre_suggest (
        track_id   INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
        items_json TEXT NOT NULL,
        status     TEXT NOT NULL,
        fetched_at REAL NOT NULL
    );
    CREATE TABLE mb_artist_genres (
        mbid        TEXT PRIMARY KEY,
        genres_json TEXT NOT NULL,
        fetched_at  REAL NOT NULL
    )
    """
)

MIGRATIONS.append(  # v9: the Edited / Ready / Broken flags are gone
    """
    ALTER TABLE tracks DROP COLUMN edited;
    ALTER TABLE tracks DROP COLUMN ready;
    ALTER TABLE tracks DROP COLUMN broken
    """
)

MIGRATIONS.append(  # v10: clean-version check (YouTube Music's explicit badge) for each song's link
    """
    ALTER TABLE tracks ADD COLUMN clean_url TEXT;
    ALTER TABLE tracks ADD COLUMN clean_check TEXT;
    ALTER TABLE tracks ADD COLUMN clean_checked_at REAL;
    ALTER TABLE tracks ADD COLUMN clean_keep INTEGER NOT NULL DEFAULT 0
    """
)

MIGRATIONS.append(  # v11: Rekordbox My Tags (read-only), read from the Comments in a Rekordbox XML export
    """
    ALTER TABLE tracks ADD COLUMN rekordbox_mytags TEXT
    """
)

MIGRATIONS.append(  # v12: Trash and Hidden. Songs already removed on Spotify and in no other crate go to the Trash.
    """
    ALTER TABLE tracks ADD COLUMN trashed_at REAL;
    ALTER TABLE tracks ADD COLUMN trash_reason TEXT;
    ALTER TABLE tracks ADD COLUMN hidden_at REAL;
    UPDATE tracks SET trashed_at = CAST(strftime('%s', 'now') AS REAL),
      trash_reason = (SELECT json_object('kind', 'spotify', 'crate', c.name, 'crate_id', c.id)
                      FROM crate_tracks ct JOIN crates c ON c.id = ct.crate_id
                      WHERE ct.track_id = tracks.id AND ct.removed_from_source = 1 LIMIT 1)
    WHERE EXISTS (SELECT 1 FROM crate_tracks ct WHERE ct.track_id = tracks.id AND ct.removed_from_source = 1)
      AND NOT EXISTS (SELECT 1 FROM crate_tracks ct JOIN crates c ON c.id = ct.crate_id
                      WHERE ct.track_id = tracks.id AND ct.removed_from_source = 0 AND c.genre_id IS NULL)
    """
)

MIGRATIONS.append(  # v13: the Genre Rack is gone for now: its genres, song genres, genre crates and suggestions are deleted.
    # Genres already written into MP3 files are left alone.
    """
    DELETE FROM crates WHERE genre_id IS NOT NULL;
    DROP TABLE track_genres;
    DROP TABLE genre_aliases;
    DROP TABLE genre_ignored;
    DROP TABLE genre_suggest;
    DROP TABLE mb_artist_genres;
    ALTER TABLE crates DROP COLUMN genre_id;
    DROP TABLE genres;
    ALTER TABLE tracks DROP COLUMN rekordbox_genre;
    ALTER TABLE tracks DROP COLUMN file_genre;
    DELETE FROM settings WHERE key IN ('genre_lookup_on', 'mb_genre_list', 'mb_genre_list_at')
    """
)

MIGRATIONS.append(  # v14: a download can be for a different link than the song's (swapping versions):
    # the song only switches to that link, and its clean/explicit label, once the new file is in.
    """
    ALTER TABLE jobs ADD COLUMN url TEXT;
    ALTER TABLE jobs ADD COLUMN on_done TEXT
    """
)

# Repair for a fixed bug: swapping a downloaded song used to switch its link and clean/explicit label before
# the new copy downloaded, so a failed download left the old file labelled as the other version. Those songs
# (swapped, a file, and no download finished since the swap) go back to the link their file came from, and
# their label is cleared so the background check labels them again. Failed jobs for them are dismissed.
STALE_SWAP_SQL = """
    json_extract(t.clean_check, '$.swapped_from') IS NOT NULL
    AND t.download_status = 'done'
    AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.track_id = t.id AND j.status = 'done'
                    AND j.updated_at >= datetime(t.clean_checked_at, 'unixepoch'))
"""


def _repair_stale_swaps(conn) -> None:
    ids = [r[0] for r in conn.execute("SELECT t.id FROM tracks t WHERE " + STALE_SWAP_SQL)]
    for tid in ids:
        conn.execute("UPDATE tracks SET source_url = json_extract(clean_check, '$.swapped_from'), clean_url = NULL, "
                     "clean_check = NULL, clean_checked_at = NULL, updated_at = datetime('now') WHERE id = ?", (tid,))
        conn.execute("UPDATE jobs SET status = 'dismissed' WHERE track_id = ? AND status = 'failed'", (tid,))
    if ids:
        log.info("Put %d swapped songs back to the link their file came from (their new copy never downloaded)", len(ids))


MIGRATIONS.append(_repair_stale_swaps)  # v15


def _from_source(conn) -> None:
    """v16 (main's v10, merged in): remember which songs in a Spotify crate came from the playlist, so
    re-sync doesn't flag songs you added by hand as "Removed on Spotify". Older rows can't be told apart
    exactly: songs with a Spotify ID count as from the playlist, and the wrong flag is cleared from the rest.
    Songs that went to the Trash only because of such a wrong flag come back out of it."""
    if "from_source" in _columns(conn, "crate_tracks"):
        return   # already done: this library came from a version that had it as v10
    conn.execute("ALTER TABLE crate_tracks ADD COLUMN from_source INTEGER NOT NULL DEFAULT 0")
    conn.execute("""UPDATE crate_tracks SET from_source = 1
                    WHERE crate_id IN (SELECT id FROM crates WHERE spotify_playlist_id IS NOT NULL)
                      AND track_id IN (SELECT id FROM tracks WHERE spotify_id IS NOT NULL AND spotify_id != '')""")
    conn.execute("UPDATE crate_tracks SET removed_from_source = 0 WHERE from_source = 0")
    conn.execute("""UPDATE tracks SET trashed_at = NULL, trash_reason = NULL
                    WHERE trashed_at IS NOT NULL AND json_extract(trash_reason, '$.kind') = 'spotify'
                      AND EXISTS (SELECT 1 FROM crate_tracks ct WHERE ct.track_id = tracks.id
                                  AND ct.crate_id = json_extract(tracks.trash_reason, '$.crate_id') AND ct.removed_from_source = 0)""")


MIGRATIONS.append(_from_source)  # v16

MIGRATIONS.append(  # v17: keep what yt-dlp (or ffmpeg) actually said when a download fails, to show it
    "ALTER TABLE jobs ADD COLUMN error_detail TEXT"
)


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _effective_version(conn, version: int) -> int:
    """Two lines of history both had a v10: this branch's (clean-version check) and main's (from_source,
    now v16). A library at main's v10 has from_source but none of v10-v15 here, so it continues from v9."""
    if version == 10 and "from_source" in _columns(conn, "crate_tracks") and "clean_url" not in _columns(conn, "tracks"):
        return 9
    return version

SCHEMA_VERSION = len(MIGRATIONS)

# The song's current link is the clean copy of a song YouTube Music also has as explicit.
CLEAN_SQL = ("(t.clean_url IS NOT NULL AND t.clean_url = t.source_url "
             "AND json_extract(t.clean_check, '$.result') = 'clean')")

CHECK_VERSION_SQL = (
    "(t.download_status = 'done' AND t.duration_ms IS NOT NULL "
    f"AND t.file_duration_ms IS NOT NULL AND abs(t.file_duration_ms - t.duration_ms) > {LENGTH_TOLERANCE_MS})"
)

# Songs that show anywhere: not in the Trash and not hidden. Every view, count and export uses this.
ACTIVE = "(t.trashed_at IS NULL AND t.hidden_at IS NULL)"

# The two bins, listed after the smart crates (they are *not* filtered by ACTIVE).
BINS: dict[str, tuple[str, str]] = {
    "hidden": ("Hidden", "(t.hidden_at IS NOT NULL AND t.trashed_at IS NULL)"),
    "trash": ("Trash", "t.trashed_at IS NOT NULL"),
}

# A swap to the other version whose download failed: the song still has its old file, and the new copy is owed.
FAILED_SWAP_SQL = ("EXISTS (SELECT 1 FROM jobs j WHERE j.track_id = t.id AND j.status = 'failed' "
                   "AND j.url IS NOT NULL AND j.url <> coalesce(t.source_url, ''))")

SMART_CRATES: dict[str, tuple[str, str]] = {
    "all": ("All Tracks", "1"),
    "needs_link": ("Needs Link", "(coalesce(t.source_url, '') = '' AND t.download_status NOT IN ('done', 'missing'))"),
    "to_download": ("Ready to Download", f"((coalesce(t.source_url, '') <> '' AND t.download_status IN ('none', 'error')) OR {FAILED_SWAP_SQL})"),
    "downloaded": ("Downloaded", "t.download_status = 'done'"),
    "check_version": ("Check Version", CHECK_VERSION_SQL),
    "clean_versions": ("Clean Versions", CLEAN_SQL),
    # Songs left loose, e.g. their crate was deleted before the Trash existed: review them, then trash or keep.
    "no_crate": ("Not in a Crate", "NOT EXISTS (SELECT 1 FROM crate_tracks ct WHERE ct.track_id = t.id AND ct.removed_from_source = 0)"),
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
    version = _effective_version(conn, current_version(conn))
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
        step = MIGRATIONS[target - 1]
        # executescript() commits implicitly, so run statements one by one in a real transaction.
        with tx(conn):
            if callable(step):
                step(conn)
            else:
                for stmt in _statements(step):
                    conn.execute(stmt)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (target,))


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


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
    for flag in ("is_manual",):
        d[flag] = bool(d.get(flag))
    d["check_version"] = bool(
        d["download_status"] == "done" and d["duration_ms"] and d["file_duration_ms"]
        and abs(d["file_duration_ms"] - d["duration_ms"]) > LENGTH_TOLERANCE_MS
    )
    if "removed_from_source" in d:
        d["removed_from_source"] = bool(d["removed_from_source"])
    if "clean_check" in d:
        raw = d.pop("clean_check")
        current = bool(raw) and d.get("clean_url") and d.get("clean_url") == d.get("source_url")
        d["clean"] = json.loads(raw) if current else None
        d.pop("clean_keep", None)   # no longer used (labels are neutral, nothing to dismiss)
        d["clean_flag"] = bool(d["clean"] and d["clean"].get("result") == "clean")
    if "trashed_at" in d:
        reason = d.pop("trash_reason", None)
        d["trash"] = {**(json.loads(reason) if reason else {}), "at": d["trashed_at"]} if d["trashed_at"] else None
        d["hidden"] = bool(d.get("hidden_at"))
    if "rekordbox_mytags" in d:
        raw = d.pop("rekordbox_mytags")
        d["mytags"] = json.loads(raw) if raw else []   # read-only: they're edited in Rekordbox
    return d


def get_track(conn, track_id: int):
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if not row:
        return None
    return track_to_dict(row)


def add_to_crate(conn, crate_id: int, track_ids, from_source: bool = False) -> int:
    """from_source: the songs come from the crate's Spotify playlist (import / re-sync)."""
    row = conn.execute("SELECT coalesce(max(position), -1) FROM crate_tracks WHERE crate_id = ?", (crate_id,)).fetchone()
    pos = row[0] + 1
    added = 0
    for tid in track_ids:
        cur = conn.execute(
            "INSERT OR IGNORE INTO crate_tracks (crate_id, track_id, position, from_source) VALUES (?, ?, ?, ?)",
            (crate_id, tid, pos, int(from_source)),
        )
        if cur.rowcount:
            pos += 1
            added += 1
            # A song added to a crate is wanted again: it leaves the Trash.
            conn.execute("UPDATE tracks SET trashed_at = NULL, trash_reason = NULL WHERE id = ? AND trashed_at IS NOT NULL", (tid,))
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
