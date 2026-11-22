"""Files on disk: naming, missing-file scan, relocate, folder import, trash."""

import logging
import os
import re
import unicodedata
from pathlib import Path

from send2trash import send2trash

from . import db, tags
from .errors import AppError

log = logging.getLogger("djcrate.library")

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_NAME_BYTES = 200


def safe_filename(title: str, artists: str) -> str:
    """`Title - Artist1, Artist2` with characters macOS/USB filesystems dislike removed."""
    def clean(s: str) -> str:
        s = unicodedata.normalize("NFC", s or "")
        s = _BAD_CHARS.sub(" ", s)
        return re.sub(r"\s+", " ", s).strip()

    title, artists = clean(title), clean(artists)
    name = f"{title} - {artists}" if artists else title
    name = name.strip(" .") or "Untitled"
    while len(name.encode("utf-8")) > MAX_NAME_BYTES:
        name = name[:-1]
    return name.rstrip(" .")


def unique_path(folder: Path, stem: str, ext: str = ".mp3") -> Path:
    candidate = folder / f"{stem}{ext}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({n}){ext}"
        n += 1
    return candidate


def trash(path: Path) -> None:
    if path.exists():
        send2trash(str(path))


# ---------------------------------------------------------------- missing files

def rescan(state) -> dict:
    """Mark downloaded tracks whose file is gone as missing, and un-mark ones
    that came back. Like Rekordbox's red "!"."""
    conn = state.conn()
    try:
        rows = conn.execute(
            "SELECT id, file_rel_path, download_status FROM tracks WHERE file_rel_path IS NOT NULL AND download_status IN ('done', 'missing')"
        ).fetchall()
        newly_missing = found_again = 0
        with db.tx(conn):
            for r in rows:
                exists = state.abs_path(r["file_rel_path"]).is_file()
                if not exists and r["download_status"] == "done":
                    conn.execute("UPDATE tracks SET download_status = 'missing' WHERE id = ?", (r["id"],))
                    newly_missing += 1
                elif exists and r["download_status"] == "missing":
                    conn.execute("UPDATE tracks SET download_status = 'done' WHERE id = ?", (r["id"],))
                    found_again += 1
        total = conn.execute("SELECT count(*) FROM tracks WHERE download_status = 'missing'").fetchone()[0]
    finally:
        conn.close()
    return {"missing": total, "newly_missing": newly_missing, "found_again": found_again}


def relocate(state, conn, track_id: int, new_path: Path) -> dict:
    new_path = Path(new_path).expanduser()
    if not new_path.is_file():
        raise AppError("not_found", "That file doesn't exist.")
    if new_path.suffix.lower() != ".mp3":
        raise AppError("not_mp3", "Pick an MP3 file.")
    dur = tags.duration_ms(new_path)
    conn.execute(
        "UPDATE tracks SET file_rel_path = ?, file_duration_ms = ?, download_status = 'done', updated_at = datetime('now') WHERE id = ?",
        (state.rel_path(new_path), dur, track_id),
    )
    return db.get_track(conn, track_id)


def auto_relocate_preview(state, conn, folder: Path, track_ids: list[int] | None) -> list[dict]:
    """Find files with the same name as missing tracks. Nothing is changed here;
    the UI shows the matches and you confirm."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise AppError("not_found", "That folder doesn't exist.")
    q = "SELECT id, title, artists, file_rel_path FROM tracks WHERE download_status = 'missing' AND file_rel_path IS NOT NULL"
    params: list = []
    if track_ids:
        q += f" AND id IN ({','.join('?' for _ in track_ids)})"
        params = track_ids
    missing = conn.execute(q, params).fetchall()
    if not missing:
        return []
    wanted = {Path(r["file_rel_path"]).name.lower(): r for r in missing}
    matches: dict[int, dict] = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            r = wanted.get(f.lower())
            if r and r["id"] not in matches:
                matches[r["id"]] = {"track_id": r["id"], "title": r["title"], "artists": r["artists"],
                                    "old_path": r["file_rel_path"], "new_path": str(Path(root) / f)}
    return list(matches.values())


# ---------------------------------------------------------------- folder import

def import_folder(state, conn, folder: Path) -> dict:
    """Add MP3s you already have as downloaded tracks (deduped). Files stay where
    they are; anything unreadable is skipped and listed."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise AppError("not_found", "That folder doesn't exist.")
    imported, existing, skipped = [], 0, []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if f.startswith("."):
                continue
            p = Path(root) / f
            if p.suffix.lower() != ".mp3":
                skipped.append({"file": str(p), "reason": "Not an MP3"})
                continue
            try:
                meta = tags.read_tags(p)
            except Exception as e:
                skipped.append({"file": str(p), "reason": f"Unreadable ({type(e).__name__})"})
                continue
            rel = state.rel_path(p)
            same_file = conn.execute("SELECT id FROM tracks WHERE file_rel_path = ?", (rel,)).fetchone()
            if same_file:
                imported.append(same_file["id"])
                existing += 1
                continue
            tid, created = db.upsert_track(conn, {**meta, "is_manual": 1})
            row = conn.execute("SELECT file_rel_path, download_status FROM tracks WHERE id = ?", (tid,)).fetchone()
            if created or not row["file_rel_path"] or row["download_status"] == "missing":
                conn.execute(
                    "UPDATE tracks SET file_rel_path = ?, file_duration_ms = ?, download_status = 'done' WHERE id = ?",
                    (rel, meta["file_duration_ms"], tid),
                )
            if not created:
                existing += 1
            imported.append(tid)
    return {"track_ids": imported, "imported": len(imported) - existing, "already_known": existing, "skipped": skipped}
