"""Daily database backups, pre-migration backups, export/import zips.

These cover DJ Crate's own data only. Rekordbox backs itself up separately.
"""

import datetime as dt
import json
import logging
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from . import __version__, db
from .errors import AppError

log = logging.getLogger("djcrate.backup")

KEEP_DAILY = 14
MANIFEST = "djcrate-manifest.json"


def _snapshot(src_db: Path, dest: Path) -> None:
    """Consistent copy even while the app is writing (sqlite backup API)."""
    src = sqlite3.connect(src_db)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def list_backups(state) -> list[dict]:
    if not state.library_reachable() or not state.backup_dir.is_dir():
        return []
    items = []
    for p in sorted(state.backup_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append({
            "name": p.name,
            "size": p.stat().st_size,
            "modified": dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return items


def daily_backup(state) -> Path | None:
    if state.status != "ok" or state.read_only or not state.db_path.exists():
        return None
    dest = state.backup_dir / f"daily-{dt.date.today().isoformat()}.db"
    if dest.exists():
        return None
    _snapshot(state.db_path, dest)
    dailies = sorted(state.backup_dir.glob("daily-*.db"))
    for old in dailies[:-KEEP_DAILY]:
        old.unlink(missing_ok=True)
    log.info("Daily backup written: %s", dest.name)
    return dest


def premigration_backup(state, from_version: int) -> Path:
    dest = state.backup_dir / f"pre-migration-v{from_version}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    _snapshot(state.db_path, dest)
    log.info("Pre-migration backup written: %s", dest.name)
    return dest


def restore_backup(state, name: str) -> None:
    src = (state.backup_dir / name).resolve()
    if src.parent != state.backup_dir.resolve() or not src.exists():
        raise AppError("not_found", "That backup doesn't exist.", 404)
    check = sqlite3.connect(src)
    try:
        if not db.integrity_ok(check):
            raise AppError("backup_corrupt", "That backup is damaged too. Pick an older one.")
    finally:
        check.close()
    if state.db_path.exists():
        state.db_path.rename(state.db_path.with_name(f"djcrate.db.damaged-{time.strftime('%Y%m%d-%H%M%S')}"))
    for suffix in ("-wal", "-shm"):
        Path(str(state.db_path) + suffix).unlink(missing_ok=True)
    _snapshot(src, state.db_path)
    log.info("Restored database from %s", name)


def export_zip(state, dest: Path, include_music: bool) -> dict:
    conn = state.conn()
    try:
        files = []
        if include_music:
            for row in conn.execute("SELECT file_rel_path FROM tracks WHERE file_rel_path IS NOT NULL"):
                p = state.abs_path(row[0])
                if p and p.is_file() and not Path(row[0]).is_absolute():
                    files.append((p, row[0]))
    finally:
        conn.close()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "djcrate.db"
        _snapshot(state.db_path, snap)
        manifest = {"app": "djcrate", "app_version": __version__, "schema": db.SCHEMA_VERSION,
                    "created": dt.datetime.now().isoformat(timespec="seconds"), "music_files": len(files)}
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST, json.dumps(manifest, indent=2))
            zf.write(snap, "djcrate.db")
            for p, rel in files:
                zf.write(p, f"music/{rel}", compress_type=zipfile.ZIP_STORED)
    return {"path": str(dest), "music_files": len(files)}


def import_zip(state, src: Path) -> dict:
    """Replace this library's DJ Crate data with an export. Music files in the
    zip are extracted into the library, never overwriting existing files."""
    try:
        zf = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as e:
        raise AppError("bad_zip", f"That isn't a DJ Crate export ({e}).")
    with zf:
        names = set(zf.namelist())
        if MANIFEST not in names or "djcrate.db" not in names:
            raise AppError("bad_zip", "That zip isn't a DJ Crate export.")
        manifest = json.loads(zf.read(MANIFEST))
        if manifest.get("schema", 0) > db.SCHEMA_VERSION:
            raise AppError("too_new", "That export came from a newer DJ Crate. Update the app first.")
        with tempfile.TemporaryDirectory() as tmp:
            extracted = Path(zf.extract("djcrate.db", tmp))
            check = sqlite3.connect(extracted)
            try:
                if not db.integrity_ok(check):
                    raise AppError("backup_corrupt", "The database inside that export is damaged.")
            finally:
                check.close()
            if state.db_path.exists():
                _snapshot(state.db_path, state.backup_dir / f"before-import-{time.strftime('%Y%m%d-%H%M%S')}.db")
                state.db_path.unlink()
            for suffix in ("-wal", "-shm"):
                Path(str(state.db_path) + suffix).unlink(missing_ok=True)
            _snapshot(extracted, state.db_path)
        extracted_music = skipped = 0
        lib = state.library.resolve()
        for name in names:
            if not name.startswith("music/") or name.endswith("/"):
                continue
            target = (lib / name[len("music/"):]).resolve()
            if lib not in target.parents:
                continue  # never write outside the library
            if target.exists():
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as fsrc, open(target, "wb") as fdst:
                while chunk := fsrc.read(1 << 20):
                    fdst.write(chunk)
            extracted_music += 1
    return {"music_extracted": extracted_music, "music_skipped": skipped}
