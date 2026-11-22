"""Trash and Hidden: two ways to get songs out of sight without losing them.

  * Trash: songs on their way out. Deleting songs, deleting a crate (its songs that
    aren't in another crate) and songs removed from a Spotify playlist (same rule)
    all land here. Nothing is deleted until you empty the Trash; until then a song
    keeps its file, crates and notes, and Restore brings it all back.
  * Hidden: songs you just don't want to see (e.g. ones whose files are gone for good).
    Nothing is ever deleted.

Both vanish from every other view, count, search, download and Rekordbox export
(see db.ACTIVE).
"""

import json
import time

from . import db

# A song still "lives" in a crate if it's in one and wasn't removed on Spotify.
_IN_OTHER_CRATE = """EXISTS (SELECT 1 FROM crate_tracks ct
    WHERE ct.track_id = t.id AND ct.removed_from_source = 0 AND ct.crate_id NOT IN ({skip}))"""


def _ph(items) -> str:
    return ",".join("?" * len(items)) or "NULL"


def trash(conn, ids, reason: dict) -> int:
    """Move songs to the Trash (if they aren't there already). Their downloads are cancelled by the caller."""
    ids = list(ids)
    if not ids:
        return 0
    cur = conn.execute(
        f"UPDATE tracks SET trashed_at = ?, trash_reason = ? WHERE trashed_at IS NULL AND id IN ({_ph(ids)})",
        (time.time(), json.dumps(reason), *ids))
    return cur.rowcount


def restore(conn, ids) -> int:
    ids = list(ids)
    if not ids:
        return 0
    return conn.execute(
        f"UPDATE tracks SET trashed_at = NULL, trash_reason = NULL WHERE trashed_at IS NOT NULL AND id IN ({_ph(ids)})", ids).rowcount


def set_hidden(conn, ids, hidden: bool) -> int:
    ids = list(ids)
    if not ids:
        return 0
    if hidden:
        return conn.execute(f"UPDATE tracks SET hidden_at = ? WHERE hidden_at IS NULL AND id IN ({_ph(ids)})", (time.time(), *ids)).rowcount
    return conn.execute(f"UPDATE tracks SET hidden_at = NULL WHERE hidden_at IS NOT NULL AND id IN ({_ph(ids)})", ids).rowcount


def orphans(conn, candidate_ids, skip_crates) -> list[int]:
    """Of these songs, the ones that would no longer be in any crate once `skip_crates` are gone."""
    ids, skip = list(candidate_ids), list(skip_crates)
    assert skip, "skip_crates can't be empty (NOT IN (NULL) would match nothing)"
    if not ids:
        return []
    q = f"SELECT t.id FROM tracks t WHERE t.id IN ({_ph(ids)}) AND t.trashed_at IS NULL AND NOT {_IN_OTHER_CRATE.format(skip=_ph(skip))}"
    return [r[0] for r in conn.execute(q, (*ids, *skip))]


def before_crate_delete(conn, crate_id: int) -> dict:
    """Trash the songs of a crate (or folder and its crates) that aren't in any other crate. Call before deleting it."""
    crate = conn.execute("SELECT * FROM crates WHERE id = ?", (crate_id,)).fetchone()
    if not crate:
        return {"trashed": 0}
    gone = db.crate_descendants(conn, crate_id)
    songs = [r[0] for r in conn.execute(
        f"SELECT DISTINCT track_id FROM crate_tracks WHERE removed_from_source = 0 AND crate_id IN ({_ph(gone)})", gone)]
    ids = orphans(conn, songs, gone)
    n = trash(conn, ids, {"kind": "crate", "crate": crate["name"]})
    return {"trashed": n, "ids": ids}


def after_spotify_removal(conn, crate_id: int, crate_name: str, removed_ids) -> int:
    """Songs just removed from a Spotify playlist go to the Trash, unless they're in another crate."""
    ids = orphans(conn, removed_ids, [crate_id])
    return trash(conn, ids, {"kind": "spotify", "crate": crate_name, "crate_id": crate_id})


def after_spotify_return(conn, crate_id: int, returned_ids) -> int:
    """Songs back on a Spotify playlist are wanted again, so they leave the Trash, whatever put them
    there (like adding a song to a crate does). Otherwise they'd be invisible in that crate."""
    return restore(conn, returned_ids)


def counts(conn) -> dict:
    return {
        "trash": conn.execute("SELECT count(*) FROM tracks WHERE trashed_at IS NOT NULL").fetchone()[0],
        "hidden": conn.execute("SELECT count(*) FROM tracks WHERE hidden_at IS NOT NULL AND trashed_at IS NULL").fetchone()[0],
    }
