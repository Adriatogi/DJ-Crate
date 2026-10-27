"""FastAPI routes. Every error goes out as {"error": {"code", "message", ...}}
so the UI can always show something useful."""

import datetime as dt
import logging
import os
import traceback
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, backup, cutting, db, genius, libraries, library, rekordbox, setboard, spotify, tags, tools, yt_search
from .config import DEFAULT_LIBRARY, LOG_DIR, get_port, update_config
from .downloader import DownloadManager, valid_link
from .errors import AppError
from .state import state

log = logging.getLogger("djcrate.api")

WEB_DIR = Path(__file__).parent / "web"
COOKIE_BROWSERS = ("", "safari", "chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi")

app = FastAPI(title="DJ Crate", docs_url=None, redoc_url=None)
downloads = DownloadManager(state)
search_ahead = yt_search.Prefetcher(state)
hooks: dict[str, Any] = {"focus": None}

# Routes that must keep working in read-only mode / without a library.
READ_ONLY_ALLOWED = (
    "/api/library/", "/api/reveal", "/api/rekordbox/export/reveal", "/api/open_url", "/api/focus", "/api/tools/", "/api/spotify/login",
    "/api/spotify/cancel_login", "/api/spotify/logout", "/api/backup/export", "/api/errors/clear",
)


# ---------------------------------------------------------------- plumbing

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message, **exc.extra}}, status_code=exc.status)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    state.record_error(f"{request.method} {request.url.path} failed", "".join(traceback.format_exception(exc))[-3000:])
    return JSONResponse(
        {"error": {"code": "internal", "message": "Something went wrong — details in Activity."}}, status_code=500
    )


@app.middleware("http")
async def guards(request: Request, call_next):
    path = request.url.path
    if (
        state.read_only
        and request.method in ("POST", "PATCH", "PUT", "DELETE")
        and path.startswith("/api/")
        and not path.startswith(READ_ONLY_ALLOWED)
    ):
        return JSONResponse(
            {"error": {"code": "read_only", "message": "This library is open read-only. Take over to make changes."}},
            status_code=423,
        )
    response = await call_next(request)
    if not path.startswith("/api/tracks/") or not path.endswith("/audio"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_conn():
    conn = state.conn()
    try:
        yield conn
    finally:
        conn.close()


def placeholders(items) -> str:
    return ",".join("?" for _ in items)


# ---------------------------------------------------------------- status

@app.get("/api/ping")
def ping():
    return {"app": "djcrate", "version": __version__, "pid": os.getpid()}


@app.post("/api/focus")
def focus():
    if hooks.get("focus"):
        hooks["focus"]()
    return {"ok": True}


@app.get("/api/status")
def status():
    spotify.check_login_timeout()
    lib = {
        "status": state.status, "path": str(state.library) if state.library else None, "error": state.error,
        "read_only": state.read_only, "lock_info": state.lock_info, "host": state.host,
        "default_path": str(DEFAULT_LIBRARY), "name": None, "ask_on_launch": libraries.ask_on_launch(),
    }
    if state.status == "ok" and not state.library_reachable():
        state.status = lib["status"] = "library_missing"
    out = {
        "version": __version__,
        "library": lib,
        "tools": {"ffmpeg": tools.ffmpeg_dir() is not None, "ytdlp": tools.ytdlp_version(), "js": tools.js_runtime()},
        "spotify": {"client_id_set": False, "connected": False, "login": dict(spotify.login_result),
                    "progress": spotify.progress.get("text", "")},
        "queue": {"paused_reason": downloads.paused_reason, "queued": 0, "running": 0, "failed": 0,
                  "interrupted": 0, "outdated": 0},
        "errors_count": len(state.app_errors),
        "search": {"paused_until": search_ahead.paused_until if search_ahead.paused else None},
        "genius": {"configured": False},
    }
    if state.status in ("db_corrupt", "migration_failed"):
        out["backups"] = backup.list_backups(state)
    if state.status == "ok":
        conn = state.conn()
        try:
            client_id = db.get_setting(conn, "spotify_client_id", "")
            lib["name"] = db.get_setting(conn, "library_name") or (state.library.name if state.library else None)
            out["spotify"]["client_id_set"] = bool(client_id)
            out["spotify"]["connected"] = spotify_connected(client_id, db.get_setting(conn, "library_id"))
            out["genius"]["configured"] = genius.configured(db.get_setting(conn, "library_id"))
            out["rekordbox"] = rekordbox.status(conn)
            for row in conn.execute("SELECT status, count(*) n FROM jobs WHERE status IN ('queued','running','failed','interrupted') GROUP BY status"):
                out["queue"][row["status"]] = row["n"]
            out["queue"]["outdated"] = conn.execute(
                "SELECT count(*) FROM jobs WHERE status = 'failed' AND error_code = 'ytdlp_outdated'").fetchone()[0]
            if not out["queue"]["queued"]:
                downloads.paused_reason = None
                out["queue"]["paused_reason"] = None
        finally:
            conn.close()
    return out


_connected_cache: dict[tuple, bool] = {}


def spotify_connected(client_id: str, owner: str | None) -> bool:
    if not client_id:
        return False
    key = (client_id, owner)
    if key not in _connected_cache:
        _connected_cache[key] = spotify.load_tokens(client_id, owner) is not None
    return _connected_cache[key]


def _on_library_opened():
    # A different library means a different Spotify login.
    _connected_cache.clear()
    genius._configured.clear()
    spotify._me_cache.clear()
    spotify.cancel_login()


state.on_opened.append(_on_library_opened)


# ---------------------------------------------------------------- library lifecycle

class NameBody(BaseModel):
    name: str


class PathBody(BaseModel):
    path: str
    create: bool = False


@app.post("/api/library/choose")
def choose_library(body: PathBody):
    p = Path(body.path).expanduser()
    if not p.exists():
        if not body.create:
            raise AppError("not_found", "That folder doesn't exist.")
        p.mkdir(parents=True)
    if not p.is_dir():
        raise AppError("not_a_folder", "Pick a folder, not a file.")
    state.open(p)
    return status()


@app.post("/api/library/default")
def default_library():
    DEFAULT_LIBRARY.mkdir(parents=True, exist_ok=True)
    state.open(DEFAULT_LIBRARY)
    return status()


@app.get("/api/libraries")
def list_libraries():
    current = state.library if state.status == "ok" else None
    return {"libraries": libraries.describe(current), "ask_on_launch": libraries.ask_on_launch(),
            "libraries_dir": str(libraries.LIBRARIES_DIR)}


@app.post("/api/library/new")
def new_library(body: NameBody):
    folder = libraries.new_library_path(body.name)
    state.open(folder)
    if state.status == "ok":
        conn = state.conn()
        try:
            db.set_setting(conn, "library_name", body.name.strip())
        finally:
            conn.close()
    return status()


class ForgetBody(BaseModel):
    path: str


@app.post("/api/library/forget")
def forget_library(body: ForgetBody):
    """Remove from the list only — never deletes the folder."""
    libraries.forget(body.path)
    return list_libraries()


class LibraryPrefs(BaseModel):
    ask_on_launch: bool


@app.post("/api/library/prefs")
def library_prefs(body: LibraryPrefs):
    libraries.set_ask_on_launch(body.ask_on_launch)
    return {"ask_on_launch": libraries.ask_on_launch()}


@app.post("/api/library/switch")
def switch_library():
    if state.status == "ok":
        conn = state.conn()
        try:
            busy = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('running', 'queued')").fetchone()[0]
        finally:
            conn.close()
        if busy:
            raise AppError("busy", f"{busy} download{'s are' if busy != 1 else ' is'} in progress. Let them finish (or Cancel all in Activity), then switch.")
    state.close()
    _on_library_opened()
    return status()


@app.post("/api/library/retry")
def retry_library():
    if state.library:
        state.open(state.library, read_only=state.read_only)
    return status()


@app.post("/api/library/take_over")
def take_over():
    state.open(state.library, take_over=True)
    return status()


@app.post("/api/library/open_read_only")
def open_read_only():
    state.open(state.library, read_only=True)
    return status()


@app.post("/api/library/restore")
def restore(body: NameBody):
    backup.restore_backup(state, body.name)
    state.open(state.library)
    return status()


@app.post("/api/library/rescan")
def rescan():
    return library.rescan(state)


class RelocateBody(BaseModel):
    track_id: int
    path: str


@app.post("/api/library/relocate")
def relocate(body: RelocateBody, conn=Depends(get_conn)):
    return library.relocate(state, conn, body.track_id, Path(body.path))


class AutoRelocateBody(BaseModel):
    folder: str | None = None
    track_ids: list[int] | None = None


@app.post("/api/library/auto_relocate/preview")
def auto_relocate_preview(body: AutoRelocateBody, conn=Depends(get_conn)):
    folder = Path(body.folder) if body.folder else state.library
    return {"matches": library.auto_relocate_preview(state, conn, folder, body.track_ids)}


class Match(BaseModel):
    track_id: int
    new_path: str


class ApplyBody(BaseModel):
    matches: list[Match]


@app.post("/api/library/auto_relocate/apply")
def auto_relocate_apply(body: ApplyBody, conn=Depends(get_conn)):
    done = 0
    with db.tx(conn):
        for m in body.matches:
            library.relocate(state, conn, m.track_id, Path(m.new_path))
            done += 1
    return {"relinked": done}


class ImportFolderBody(BaseModel):
    path: str
    parent_id: int | None = None


@app.post("/api/library/import_folder")
def import_folder(body: ImportFolderBody, conn=Depends(get_conn)):
    folder = Path(body.path).expanduser()
    with db.tx(conn):
        result = library.import_folder(state, conn, folder)
        crate_id = None
        if result["track_ids"]:
            crate_id = _create_crate(conn, folder.name or "Imported", "crate", body.parent_id)
            db.add_to_crate(conn, crate_id, result["track_ids"])
    result.pop("track_ids")
    return {**result, "crate_id": crate_id}


# ---------------------------------------------------------------- sidebar & crates

@app.get("/api/sidebar")
def sidebar(conn=Depends(get_conn)):
    smart = []
    for key, (label, cond) in db.SMART_CRATES.items():
        n = conn.execute(f"SELECT count(*) FROM tracks t WHERE {cond}").fetchone()[0]
        smart.append({"key": key, "name": label, "count": n})
    crates = [dict(r) for r in conn.execute("SELECT * FROM crates ORDER BY position, id")]
    members: dict[int, dict[int, tuple]] = {}
    for r in conn.execute(
        "SELECT ct.crate_id, ct.track_id, t.download_status = 'done' d, t.ready, t.broken "
        "FROM crate_tracks ct JOIN tracks t ON t.id = ct.track_id"
    ):
        members.setdefault(r[0], {})[r[1]] = (r[2], r[3], r[4])
    children: dict[int | None, list[dict]] = {}
    for c in crates:
        children.setdefault(c["parent_id"], []).append(c)

    def build(c):
        kids = [build(k) for k in children.get(c["id"], [])]
        tracks = dict(members.get(c["id"], {}))
        for k in kids:
            tracks.update(k.pop("_tracks"))
        flags = list(tracks.values())
        return {
            **c, "children": kids, "_tracks": tracks,
            "stats": {"total": len(flags), "downloaded": sum(f[0] for f in flags),
                      "ready": sum(f[1] for f in flags), "broken": sum(f[2] for f in flags)},
        }

    tree = [build(c) for c in children.get(None, [])]

    def strip(nodes):
        for n in nodes:
            n.pop("_tracks", None)
            strip(n["children"])

    strip(tree)
    return {"smart": smart, "tree": tree}


def _create_crate(conn, name: str, kind: str, parent_id: int | None, spotify_playlist_id: str | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise AppError("bad_name", "Give it a name.")
    if parent_id is not None:
        parent = conn.execute("SELECT kind FROM crates WHERE id = ?", (parent_id,)).fetchone()
        if not parent or parent["kind"] != "folder":
            raise AppError("bad_parent", "Crates can only go inside folders.")
    pos = conn.execute("SELECT coalesce(max(position), -1) + 1 FROM crates WHERE parent_id IS ?", (parent_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO crates (name, kind, parent_id, position, spotify_playlist_id) VALUES (?, ?, ?, ?, ?)",
        (name, kind, parent_id, pos, spotify_playlist_id),
    )
    return cur.lastrowid


class CrateBody(BaseModel):
    name: str
    kind: str = "crate"
    parent_id: int | None = None


@app.post("/api/crates")
def create_crate(body: CrateBody, conn=Depends(get_conn)):
    if body.kind not in ("folder", "crate"):
        raise AppError("bad_kind", "Unknown kind.")
    with db.tx(conn):
        cid = _create_crate(conn, body.name, body.kind, body.parent_id)
    return {"id": cid}


class CratePatch(BaseModel):
    name: str | None = None
    move: bool = False
    parent_id: int | None = None
    index: int | None = None


@app.patch("/api/crates/{crate_id}")
def update_crate(crate_id: int, body: CratePatch, conn=Depends(get_conn)):
    crate = conn.execute("SELECT * FROM crates WHERE id = ?", (crate_id,)).fetchone()
    if not crate:
        raise AppError("not_found", "That crate doesn't exist anymore.", 404)
    with db.tx(conn):
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise AppError("bad_name", "Give it a name.")
            conn.execute("UPDATE crates SET name = ? WHERE id = ?", (name, crate_id))
        if body.move:
            parent_id = body.parent_id
            if parent_id is not None:
                if parent_id in db.crate_descendants(conn, crate_id):
                    raise AppError("bad_move", "A folder can't go inside itself.")
                parent = conn.execute("SELECT kind FROM crates WHERE id = ?", (parent_id,)).fetchone()
                if not parent or parent["kind"] != "folder":
                    raise AppError("bad_parent", "You can only drop into folders.")
            siblings = [r["id"] for r in conn.execute(
                "SELECT id FROM crates WHERE parent_id IS ? AND id <> ? ORDER BY position, id", (parent_id, crate_id))]
            index = len(siblings) if body.index is None else max(0, min(body.index, len(siblings)))
            siblings.insert(index, crate_id)
            conn.execute("UPDATE crates SET parent_id = ? WHERE id = ?", (parent_id, crate_id))
            for pos, sid in enumerate(siblings):
                conn.execute("UPDATE crates SET position = ? WHERE id = ?", (pos, sid))
    return {"ok": True}


@app.delete("/api/crates/{crate_id}")
def delete_crate(crate_id: int, conn=Depends(get_conn)):
    with db.tx(conn):
        conn.execute("DELETE FROM crates WHERE id = ?", (crate_id,))
    return {"ok": True}


class IdsBody(BaseModel):
    ids: list[int]


@app.post("/api/crates/{crate_id}/tracks")
def add_tracks_to_crate(crate_id: int, body: IdsBody, conn=Depends(get_conn)):
    crate = conn.execute("SELECT kind FROM crates WHERE id = ?", (crate_id,)).fetchone()
    if not crate:
        raise AppError("not_found", "That crate doesn't exist anymore.", 404)
    if crate["kind"] == "folder":
        raise AppError("is_folder", "Drop songs onto a crate, not a folder.")
    with db.tx(conn):
        added = db.add_to_crate(conn, crate_id, body.ids)
    return {"added": added, "already": len(body.ids) - added}


@app.post("/api/crates/{crate_id}/tracks/remove")
def remove_tracks_from_crate(crate_id: int, body: IdsBody, conn=Depends(get_conn)):
    """Only removes membership. Returns the removed rows so the UI can Undo."""
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM crate_tracks WHERE crate_id = ? AND track_id IN ({placeholders(body.ids)})", (crate_id, *body.ids))]
    with db.tx(conn):
        conn.execute(f"DELETE FROM crate_tracks WHERE crate_id = ? AND track_id IN ({placeholders(body.ids)})", (crate_id, *body.ids))
    return {"removed": rows}


class RestoreRows(BaseModel):
    rows: list[dict]


@app.post("/api/crates/{crate_id}/tracks/restore")
def restore_tracks_to_crate(crate_id: int, body: RestoreRows, conn=Depends(get_conn)):
    with db.tx(conn):
        for r in body.rows:
            conn.execute(
                "INSERT OR IGNORE INTO crate_tracks (crate_id, track_id, position, removed_from_source, added_at) VALUES (?, ?, ?, ?, ?)",
                (crate_id, r["track_id"], r.get("position", 0), r.get("removed_from_source", 0), r.get("added_at") or dt.datetime.now().isoformat()),
            )
    return {"ok": True}


# ---------------------------------------------------------------- tracks

JOB_JOIN = """
LEFT JOIN (SELECT track_id, id job_id, status job_status, progress job_progress, stage job_stage, error_msg job_error
           FROM jobs WHERE id IN (SELECT max(id) FROM jobs WHERE status IN ('queued','running','interrupted','failed') GROUP BY track_id)
          ) j ON j.track_id = t.id
"""


@app.get("/api/tracks")
def list_tracks(view: str = "smart:all", q: str = "", conn=Depends(get_conn)):
    params: list = []
    search = ""
    if q.strip():
        like = f"%{q.strip()}%"
        search = " AND (t.title LIKE ? OR t.artists LIKE ? OR t.album LIKE ? OR t.notes LIKE ?)"
        params_search = [like] * 4
    else:
        params_search = []
    if view.startswith("crate:"):
        crate_id = int(view.split(":", 1)[1])
        crate = conn.execute("SELECT * FROM crates WHERE id = ?", (crate_id,)).fetchone()
        if not crate:
            raise AppError("not_found", "That crate doesn't exist anymore.", 404)
        if crate["kind"] == "folder":
            ids = db.crate_descendants(conn, crate_id)
            sql = (f"SELECT t.*, j.*, 0 removed_from_source FROM tracks t {JOB_JOIN} WHERE t.id IN "
                   f"(SELECT track_id FROM crate_tracks WHERE crate_id IN ({placeholders(ids)})){search} "
                   "ORDER BY t.title COLLATE NOCASE")
            params = [*ids, *params_search]
        else:
            sql = (f"SELECT t.*, j.*, ct.removed_from_source, ct.position FROM crate_tracks ct JOIN tracks t ON t.id = ct.track_id "
                   f"{JOB_JOIN} WHERE ct.crate_id = ?{search} ORDER BY ct.removed_from_source, ct.position")
            params = [crate_id, *params_search]
    else:
        key = view.split(":", 1)[1] if ":" in view else "all"
        if key not in db.SMART_CRATES:
            raise AppError("not_found", "Unknown view.", 404)
        cond = db.SMART_CRATES[key][1]
        sql = f"SELECT t.*, j.* FROM tracks t {JOB_JOIN} WHERE {cond}{search} ORDER BY t.title COLLATE NOCASE, t.artists COLLATE NOCASE"
        params = params_search
    return {"tracks": [db.track_to_dict(r) for r in conn.execute(sql, params)]}


class NewTrack(BaseModel):
    title: str
    artists: str = ""
    album: str = ""
    source_url: str = ""
    crate_id: int | None = None


@app.post("/api/tracks")
def add_track(body: NewTrack, conn=Depends(get_conn)):
    title = body.title.strip()
    if not title:
        raise AppError("bad_title", "A song needs a title.")
    url = body.source_url.strip()
    if url and not valid_link(url):
        raise AppError("invalid_url", "That isn't a YouTube/SoundCloud/Bandcamp link.")
    with db.tx(conn):
        tid, created = db.upsert_track(conn, {"title": title, "artists": body.artists.strip(), "album": body.album.strip(), "is_manual": 1})
        if url:
            conn.execute("UPDATE tracks SET source_url = coalesce(nullif(source_url, ''), ?) WHERE id = ?", (url, tid))
        if body.crate_id:
            db.add_to_crate(conn, body.crate_id, [tid])
    return {"track": db.get_track(conn, tid), "created": created}


class TrackPatch(BaseModel):
    title: str | None = None
    artists: str | None = None
    album: str | None = None
    release_date: str | None = None
    notes: str | None = None
    source_url: str | None = None
    edited: bool | None = None
    ready: bool | None = None
    broken: bool | None = None


TAG_FIELDS = ("title", "artists", "album", "release_date", "notes")


@app.patch("/api/tracks/{track_id}")
def update_track(track_id: int, body: TrackPatch, conn=Depends(get_conn)):
    track = db.get_track(conn, track_id)
    if not track:
        raise AppError("not_found", "That song doesn't exist anymore.", 404)
    changes = body.model_dump(exclude_none=True)
    warnings = []
    if "title" in changes and not changes["title"].strip():
        raise AppError("bad_title", "A song needs a title.")
    if "source_url" in changes:
        url = changes["source_url"].strip()
        if url and not valid_link(url):
            raise AppError("invalid_url", "Not a YouTube/SoundCloud/Bandcamp link.")
        changes["source_url"] = url or None
        if url:
            dupe = conn.execute("SELECT title, artists FROM tracks WHERE source_url = ? AND id <> ?", (url, track_id)).fetchone()
            if dupe:
                warnings.append(f"This link is already used by “{dupe['title']} – {dupe['artists']}”. Kept anyway.")
    for k in ("edited", "ready", "broken"):
        if k in changes:
            changes[k] = int(changes[k])
    if not changes:
        return {"track": track, "warnings": warnings}
    with db.tx(conn):
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE tracks SET {sets}, updated_at = datetime('now') WHERE id = ?", (*changes.values(), track_id))
        if "title" in changes or "artists" in changes:
            t = conn.execute("SELECT title, artists FROM tracks WHERE id = ?", (track_id,)).fetchone()
            conn.execute("UPDATE tracks SET norm_key = ? WHERE id = ?", (db.norm_key(t["title"], t["artists"]), track_id))
        if "edited" in changes and track["file_rel_path"]:
            # Re-check the length against the file as it is now.
            p = state.abs_path(track["file_rel_path"])
            if p and p.is_file():
                conn.execute("UPDATE tracks SET file_duration_ms = ? WHERE id = ?", (tags.duration_ms(p), track_id))
    updated = db.get_track(conn, track_id)
    if any(k in changes for k in TAG_FIELDS):
        warn = _write_file_tags(conn, updated, fetch_cover=False)
        if warn:
            warnings.append(warn)
        updated = db.get_track(conn, track_id)
    return {"track": updated, "warnings": warnings}


def _write_file_tags(conn, track: dict, fetch_cover: bool) -> str | None:
    p = state.abs_path(track.get("file_rel_path"))
    if not p or not p.is_file() or track["download_status"] != "done":
        return None
    try:
        cover, mime = downloads._cover(track.get("cover_url")) if fetch_cover else (None, "image/jpeg")
        tags.write_tags(p, track, cover, mime)
        conn.execute("UPDATE tracks SET tag_error = NULL WHERE id = ?", (track["id"],))
        return None
    except Exception as e:
        log.exception("Tag write failed for track %s", track["id"])
        msg = f"Tags couldn't be written: {e}"
        conn.execute("UPDATE tracks SET tag_error = ? WHERE id = ?", (msg, track["id"]))
        return msg


@app.post("/api/tracks/{track_id}/retag")
def retag(track_id: int, conn=Depends(get_conn)):
    track = db.get_track(conn, track_id)
    if not track:
        raise AppError("not_found", "That song doesn't exist anymore.", 404)
    warn = _write_file_tags(conn, track, fetch_cover=True)
    if warn:
        raise AppError("tag_failed", warn)
    return {"track": db.get_track(conn, track_id)}


class BulkBody(BaseModel):
    ids: list[int]
    field: str
    value: bool


@app.post("/api/tracks/bulk")
def bulk_update(body: BulkBody, conn=Depends(get_conn)):
    if body.field not in ("edited", "ready", "broken"):
        raise AppError("bad_field", "Unknown field.")
    with db.tx(conn):
        conn.execute(
            f"UPDATE tracks SET {body.field} = ?, updated_at = datetime('now') WHERE id IN ({placeholders(body.ids)})",
            (int(body.value), *body.ids),
        )
    return {"updated": len(body.ids)}


@app.post("/api/tracks/delete")
def delete_tracks(body: IdsBody, conn=Depends(get_conn)):
    """Delete from library: MP3s go to the macOS Trash, rows are removed.
    Set Setter rows keep their label so your set notes still read right."""
    rows = conn.execute(f"SELECT id, title, artists, file_rel_path FROM tracks WHERE id IN ({placeholders(body.ids)})", body.ids).fetchall()
    for job in conn.execute(f"SELECT id FROM jobs WHERE track_id IN ({placeholders(body.ids)}) AND status IN ('queued','running','interrupted')", body.ids):
        downloads.cancel(conn, job["id"])
    trashed, failed, failed_ids = 0, [], set()
    for r in rows:
        p = state.abs_path(r["file_rel_path"])
        if p and p.exists():
            try:
                library.trash(p)
                trashed += 1
            except Exception as e:
                log.exception("Trash failed for %s", p)
                failed.append({"title": r["title"], "reason": str(e)})
                failed_ids.add(r["id"])
    with db.tx(conn):
        for r in rows:
            if r["id"] in failed_ids:
                continue
            conn.execute("UPDATE set_items SET label = ? WHERE track_id = ? AND label = ''", (f"{r['title']} — {r['artists']}", r["id"]))
            conn.execute("DELETE FROM tracks WHERE id = ?", (r["id"],))
    return {"deleted": len(rows) - len(failed), "trashed_files": trashed, "failed": failed}


@app.get("/api/tracks/{track_id}/audio")
def track_audio(track_id: int, conn=Depends(get_conn)):
    row = conn.execute("SELECT file_rel_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
    p = state.abs_path(row["file_rel_path"]) if row else None
    if not p or not p.is_file():
        raise AppError("missing_file", "The audio file is missing — Relocate it.", 404)
    return FileResponse(p, media_type="audio/mpeg")


@app.get("/api/tracks/{track_id}/yt_search")
def track_yt_search(track_id: int, refresh: bool = False, conn=Depends(get_conn)):
    track = db.get_track(conn, track_id)
    if not track:
        raise AppError("not_found", "That song doesn't exist anymore.", 404)
    try:
        return yt_search.search(track, conn, refresh=refresh)
    except yt_search.RateLimited:
        search_ahead.pause()  # if YouTube is pushing back, don't add background searches on top
        raise


def _search_ahead_setting(conn) -> int:
    try:
        return int(db.get_setting(conn, "search_ahead", yt_search.AHEAD_DEFAULT))
    except (TypeError, ValueError):
        return yt_search.AHEAD_DEFAULT


@app.post("/api/yt_search/ahead")
def yt_search_ahead(body: IdsBody, conn=Depends(get_conn)):
    """The next songs in on-screen order; searched quietly in the background."""
    return {"queued": search_ahead.request(body.ids, _search_ahead_setting(conn)),
            "paused_until": search_ahead.paused_until if search_ahead.paused else None}


# ---------------------------------------------------------------- downloads / jobs

class DownloadBody(BaseModel):
    ids: list[int]
    replace: bool = False


@app.post("/api/downloads")
def start_downloads(body: DownloadBody, conn=Depends(get_conn)):
    return downloads.enqueue(conn, body.ids, replace=body.replace)


class ViewBody(BaseModel):
    view: str


@app.post("/api/downloads/all")
def download_all(body: ViewBody, conn=Depends(get_conn)):
    tracks = list_tracks(body.view, "", conn)["tracks"]
    ids = [t["id"] for t in tracks if (t["source_url"] or "") and t["download_status"] in ("none", "error")]
    if not ids:
        return {"queued": 0, "skipped": []}
    return downloads.enqueue(conn, ids)


@app.get("/api/jobs")
def jobs(conn=Depends(get_conn)):
    def fetch(statuses, limit=200):
        return [dict(r) for r in conn.execute(
            f"SELECT j.*, t.title, t.artists FROM jobs j JOIN tracks t ON t.id = j.track_id "
            f"WHERE j.status IN ({placeholders(statuses)}) ORDER BY j.id LIMIT ?", (*statuses, limit))]
    return {"active": fetch(("running", "queued")), "interrupted": fetch(("interrupted",)), "failed": fetch(("failed",)),
            "app_errors": list(state.app_errors), "paused_reason": downloads.paused_reason}


class JobIds(BaseModel):
    ids: list[int] | None = None


@app.post("/api/jobs/retry")
def retry_jobs(body: JobIds, conn=Depends(get_conn)):
    return {"retried": downloads.retry(conn, body.ids)}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, conn=Depends(get_conn)):
    downloads.cancel(conn, job_id)
    return {"ok": True}


@app.post("/api/jobs/cancel_all")
def cancel_all(conn=Depends(get_conn)):
    downloads.cancel_all(conn)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/dismiss")
def dismiss_job(job_id: int, conn=Depends(get_conn)):
    downloads.dismiss(conn, job_id)
    return {"ok": True}


class ResumeBody(BaseModel):
    resume: bool


@app.post("/api/jobs/interrupted")
def interrupted(body: ResumeBody, conn=Depends(get_conn)):
    return {"count": downloads.resume_interrupted(conn, body.resume)}


@app.post("/api/errors/clear")
def clear_errors():
    state.app_errors.clear()
    return {"ok": True}


# ---------------------------------------------------------------- spotify

def _client_id(conn) -> str:
    return db.get_setting(conn, "spotify_client_id", "")


def _owner(conn) -> str | None:
    return db.get_setting(conn, "library_id")


@app.post("/api/spotify/login")
def spotify_login(conn=Depends(get_conn)):
    url = spotify.start_login(_client_id(conn), get_port(), _owner(conn))
    tools.open_url(url)
    return {"url": url, "redirect_uri": spotify.redirect_uri(get_port())}


@app.post("/api/spotify/cancel_login")
def spotify_cancel_login():
    spotify.cancel_login()
    return {"ok": True}


@app.get("/callback", response_class=HTMLResponse)
def spotify_callback(request: Request, code: str | None = None, error: str | None = None):
    ok, message = spotify.finish_login(code, request.query_params.get("state"), error)
    _connected_cache.clear()
    color = "#1db954" if ok else "#ff5c5c"
    return f"""<!doctype html><meta charset=utf-8><title>DJ Crate</title>
<body style="background:#111;color:#eee;font:16px -apple-system,sans-serif;display:grid;place-items:center;height:90vh">
<div style="text-align:center"><div style="font-size:48px;color:{color}">{'✓' if ok else '!'}</div><p>{message}</p></div>"""


@app.post("/api/spotify/logout")
def spotify_logout(conn=Depends(get_conn)):
    spotify.clear_tokens(_client_id(conn), _owner(conn))
    _connected_cache.clear()
    spotify.cancel_login()
    return {"ok": True}


def _spotify_client(conn) -> spotify.Client:
    try:
        return spotify.Client(_client_id(conn), _owner(conn))
    except spotify.SpotifyError:
        _connected_cache.clear()
        raise


def _on_spotify_error(e: spotify.SpotifyError):
    if e.code in ("revoked", "not_logged_in"):
        _connected_cache.clear()


@app.get("/api/spotify/playlists")
def spotify_playlists(conn=Depends(get_conn)):
    try:
        with _spotify_client(conn) as client:
            return {"me": client.me(), "playlists": spotify.my_playlists(client)}
    except spotify.SpotifyError as e:
        _on_spotify_error(e)
        raise


class ImportBody(BaseModel):
    ref: str
    parent_id: int | None = None


@app.post("/api/spotify/import")
def spotify_import(body: ImportBody, conn=Depends(get_conn)):
    pid = spotify.parse_playlist_ref(body.ref)
    existing = conn.execute("SELECT id FROM crates WHERE spotify_playlist_id = ?", (pid,)).fetchone()
    if existing:
        result = resync(existing["id"], conn)
        return {**result, "crate_id": existing["id"], "already_imported": True}
    try:
        with _spotify_client(conn) as client:
            data = spotify.fetch_playlist(client, pid)
    except spotify.SpotifyError as e:
        _on_spotify_error(e)
        raise
    with db.tx(conn):
        crate_id = _create_crate(conn, data["name"], "spotify", body.parent_id, data["playlist_id"])
        ids, new = [], 0
        for meta in data["tracks"]:
            tid, created = db.upsert_track(conn, meta)
            ids.append(tid)
            new += created
        db.add_to_crate(conn, crate_id, ids)
        conn.execute("UPDATE crates SET last_synced = datetime('now') WHERE id = ?", (crate_id,))
    return {"crate_id": crate_id, "name": data["name"], "imported": len(set(ids)), "new_tracks": new,
            "already_known": len(set(ids)) - new, "skipped": data["skipped"]}


@app.post("/api/crates/{crate_id}/resync")
def resync(crate_id: int, conn=Depends(get_conn)):
    crate = conn.execute("SELECT * FROM crates WHERE id = ?", (crate_id,)).fetchone()
    if not crate or not crate["spotify_playlist_id"]:
        raise AppError("not_spotify", "Only crates imported from Spotify can re-sync.")
    try:
        with _spotify_client(conn) as client:
            data = spotify.fetch_playlist(client, crate["spotify_playlist_id"])
    except spotify.SpotifyError as e:
        _on_spotify_error(e)
        raise
    with db.tx(conn):
        fetched = [db.upsert_track(conn, meta)[0] for meta in data["tracks"]]
        current = {r["track_id"]: bool(r["removed_from_source"]) for r in conn.execute(
            "SELECT track_id, removed_from_source FROM crate_tracks WHERE crate_id = ?", (crate_id,))}
        plan = spotify.plan_resync(current, fetched)
        db.add_to_crate(conn, crate_id, plan["add"])
        for tid in plan["removed"]:
            conn.execute("UPDATE crate_tracks SET removed_from_source = 1 WHERE crate_id = ? AND track_id = ?", (crate_id, tid))
        for tid in plan["restored"]:
            conn.execute("UPDATE crate_tracks SET removed_from_source = 0 WHERE crate_id = ? AND track_id = ?", (crate_id, tid))
        conn.execute("UPDATE crates SET last_synced = datetime('now') WHERE id = ?", (crate_id,))
    return {"crate_id": crate_id, "name": crate["name"], "added": len(plan["add"]), "removed": len(plan["removed"]),
            "restored": len(plan["restored"]), "skipped": data["skipped"]}


# ---------------------------------------------------------------- sets (boards)

@app.get("/api/sets")
def list_sets(conn=Depends(get_conn)):
    return {"sets": [dict(r) for r in conn.execute(
        "SELECT s.*, (SELECT count(*) FROM set_nodes n WHERE n.set_id = s.id AND n.kind = 'song') items "
        "FROM sets s ORDER BY s.updated_at DESC")]}


@app.post("/api/sets")
def create_set(body: NameBody, conn=Depends(get_conn)):
    name = body.name.strip() or "Untitled set"
    with db.tx(conn):
        cur = conn.execute("INSERT INTO sets (name) VALUES (?)", (name,))
    return {"id": cur.lastrowid}


@app.get("/api/sets/{set_id}")
def get_set(set_id: int, conn=Depends(get_conn)):
    return setboard.get_board(conn, set_id)


class SetPatch(BaseModel):
    name: str | None = None
    notes: str | None = None


@app.patch("/api/sets/{set_id}")
def update_set(set_id: int, body: SetPatch, conn=Depends(get_conn)):
    changes = body.model_dump(exclude_none=True)
    if "name" in changes:
        changes["name"] = changes["name"].strip() or "Untitled set"
    if changes:
        with db.tx(conn):
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE sets SET {sets}, updated_at = datetime('now') WHERE id = ?", (*changes.values(), set_id))
    return {"ok": True}


class SetBoardBody(BaseModel):
    nodes: list[dict]
    edges: list[dict]


@app.put("/api/sets/{set_id}/board")
def save_set_board(set_id: int, body: SetBoardBody, conn=Depends(get_conn)):
    return setboard.replace_board(conn, set_id, body.nodes, body.edges)


class SetNodePatch(BaseModel):
    cue_time: str | None = None
    length_ms: int | None = None
    text: str | None = None
    clear_length: bool = False


@app.patch("/api/sets/{set_id}/nodes/{node_id}")
def patch_set_node(set_id: int, node_id: int, body: SetNodePatch, conn=Depends(get_conn)):
    changes = body.model_dump(exclude_none=True)
    if changes.pop("clear_length", False):
        changes["length_ms"] = None
    return setboard.patch_node(conn, set_id, node_id, changes)


class TransitionBody(BaseModel):
    after_node: int
    text: str


@app.post("/api/sets/{set_id}/transition")
def add_set_transition(set_id: int, body: TransitionBody, conn=Depends(get_conn)):
    return setboard.add_transition(conn, set_id, body.after_node, body.text)


@app.post("/api/sets/{set_id}/nodes/{node_id}/save_mix")
def save_set_node_as_mix(set_id: int, node_id: int, conn=Depends(get_conn)):
    return setboard.save_mix(conn, set_id, node_id)


class PairBody(BaseModel):
    a_node: int
    b_node: int
    text: str = ""


@app.post("/api/sets/{set_id}/save_pair")
def save_set_pair(set_id: int, body: PairBody, conn=Depends(get_conn)):
    return setboard.save_pair_as_mix(conn, set_id, body.a_node, body.b_node, body.text)


@app.delete("/api/sets/{set_id}")
def delete_set(set_id: int, conn=Depends(get_conn)):
    """Returns a snapshot so the UI can Undo."""
    snap = setboard.snapshot(conn, set_id)
    with db.tx(conn):
        conn.execute("DELETE FROM sets WHERE id = ?", (set_id,))
    return {"snapshot": snap}


class SetSnapshot(BaseModel):
    snapshot: dict


@app.post("/api/sets/restore")
def restore_set(body: SetSnapshot, conn=Depends(get_conn)):
    return {"id": setboard.restore(conn, body.snapshot)}


@app.get("/api/sets/{set_id}/export")
def export_set(set_id: int, conn=Depends(get_conn)):
    return {"text": setboard.set_text(setboard.get_board(conn, set_id))}


@app.post("/api/sets/{set_id}/export_file")
def export_set_file(set_id: int, conn=Depends(get_conn)):
    data = setboard.get_board(conn, set_id)
    downloads_dir = Path.home() / "Downloads"
    downloads_dir.mkdir(exist_ok=True)
    dest = library.unique_path(downloads_dir, library.safe_filename(data["set"]["name"], "") or "Set", ".txt")
    dest.write_text(setboard.set_text(data))
    tools.reveal(dest)
    return {"path": str(dest)}


# ---------------------------------------------------------------- cutting (mixes, samples, song boards)

@app.get("/api/cutting/songs")
def cutting_songs(q: str = "", connected: bool = False, conn=Depends(get_conn)):
    return {"songs": cutting.cutting_songs(conn, q, connected)}


@app.get("/api/cutting/board/{track_id}")
def cutting_board(track_id: int, depth: int = 1, conn=Depends(get_conn)):
    return cutting.board(conn, track_id, depth)


class LayoutBody(BaseModel):
    positions: list[dict]


@app.put("/api/cutting/board/{track_id}/layout")
def cutting_layout(track_id: int, body: LayoutBody, conn=Depends(get_conn)):
    cutting.save_layout(conn, track_id, body.positions)
    return {"ok": True}


@app.delete("/api/cutting/board/{track_id}/layout")
def cutting_layout_reset(track_id: int, conn=Depends(get_conn)):
    cutting.reset_layout(conn, track_id)
    return {"ok": True}


class MixBody(BaseModel):
    in_ids: list[int]
    out_ids: list[int]
    note: str = ""


@app.post("/api/mixes")
def create_mix(body: MixBody, conn=Depends(get_conn)):
    return cutting.create_mix(conn, body.in_ids, body.out_ids, body.note)


class MixPatch(BaseModel):
    note: str | None = None
    in_ids: list[int] | None = None
    out_ids: list[int] | None = None


@app.patch("/api/mixes/{mix_id}")
def update_mix(mix_id: int, body: MixPatch, conn=Depends(get_conn)):
    return cutting.update_mix(conn, mix_id, body.note, body.in_ids, body.out_ids)


@app.delete("/api/mixes/{mix_id}")
def delete_mix(mix_id: int, conn=Depends(get_conn)):
    return {"snapshot": cutting.delete_mix(conn, mix_id)}


class MixSnapshot(BaseModel):
    snapshot: dict


@app.post("/api/mixes/restore")
def restore_mix(body: MixSnapshot, conn=Depends(get_conn)):
    return cutting.restore_mix(conn, body.snapshot)


@app.get("/api/mixes/from/{track_id}")
def mixes_from(track_id: int, conn=Depends(get_conn)):
    return {"mixes": cutting.mixes_from(conn, track_id)}


class SampleBody(BaseModel):
    sample_track_id: int | None = None
    text: str = ""


@app.post("/api/tracks/{track_id}/samples")
def add_sample(track_id: int, body: SampleBody, conn=Depends(get_conn)):
    return cutting.add_sample(conn, track_id, body.sample_track_id, body.text)


class SamplePatch(BaseModel):
    text: str | None = None
    sample_track_id: int | None = None
    unlink: bool = False


@app.patch("/api/samples/{sample_id}")
def update_sample(sample_id: int, body: SamplePatch, conn=Depends(get_conn)):
    link = None if body.unlink else (body.sample_track_id if body.sample_track_id is not None else cutting.UNSET)
    cutting.update_sample(conn, sample_id, body.text, link)
    return {"ok": True}


@app.get("/api/tracks/{track_id}/samples")
def get_samples(track_id: int, conn=Depends(get_conn)):
    return cutting.samples_of(conn, track_id)


# ---------------------------------------------------------------- rekordbox (BPM & key in, playlists out)

class XmlBody(BaseModel):
    path: str


class ExportBody(BaseModel):
    on: bool


@app.put("/api/rekordbox/xml")
def rekordbox_set_xml(body: XmlBody, conn=Depends(get_conn)):
    p = Path(body.path).expanduser()
    if not p.is_file():
        raise AppError("not_found", "That file doesn't exist.", 400)
    try:
        rekordbox._read_xml(p)
    except Exception:
        raise AppError("bad_xml", "That doesn't look like a Rekordbox XML export (File → Export Collection in xml format).", 400)
    db.set_setting(conn, "rekordbox_xml", str(p))
    rekordbox.run_import(conn)
    return rekordbox.status(conn)


@app.post("/api/rekordbox/import")
def rekordbox_import(conn=Depends(get_conn)):
    if not db.get_setting(conn, "rekordbox_xml"):
        raise AppError("no_xml", "Choose your Rekordbox XML file first.", 400)
    rekordbox.run_import(conn)
    return rekordbox.status(conn)


@app.delete("/api/rekordbox/xml")
def rekordbox_forget_xml(conn=Depends(get_conn)):
    with db.tx(conn):
        conn.execute("DELETE FROM settings WHERE key IN ('rekordbox_xml', 'rekordbox_xml_mtime', 'rekordbox_import')")
    return rekordbox.status(conn)


@app.put("/api/rekordbox/export")
def rekordbox_export(body: ExportBody, conn=Depends(get_conn)):
    db.set_setting(conn, "rekordbox_export_on", "1" if body.on else "0")
    if body.on:
        rekordbox.write_export(state, conn)
    return rekordbox.status(conn)


@app.post("/api/rekordbox/export/reveal")
def rekordbox_export_reveal(conn=Depends(get_conn)):
    p = rekordbox.export_path()
    if not p.exists():
        rekordbox.write_export(state, conn)
    tools.reveal(p)
    return {"ok": True}


# ---------------------------------------------------------------- genius (sample lookup)

@app.get("/api/tracks/{track_id}/genius")
def genius_suggestions(track_id: int, refresh: bool = False, genius_id: int | None = None, conn=Depends(get_conn)):
    owner = _owner(conn)
    return genius.suggestions(conn, lambda: genius.Client(genius.load_token(owner)), track_id,
                              refresh=refresh, genius_id=genius_id)


class GeniusApplyBody(BaseModel):
    items: list[dict]


@app.post("/api/tracks/{track_id}/genius/apply")
def genius_apply(track_id: int, body: GeniusApplyBody, conn=Depends(get_conn)):
    return {"added": genius.apply(conn, track_id, body.items), **cutting.samples_of(conn, track_id)}


class TokenBody(BaseModel):
    token: str


@app.put("/api/genius/token")
def genius_token(body: TokenBody, conn=Depends(get_conn)):
    token = body.token.strip()
    if not token:
        raise AppError("bad_token", "Paste the access token from your Genius API client.")
    genius.check_token(token)
    genius.save_token(_owner(conn), token)
    return {"ok": True}


@app.delete("/api/genius/token")
def genius_logout(conn=Depends(get_conn)):
    genius.clear_token(_owner(conn))
    return {"ok": True}


@app.delete("/api/samples/{sample_id}")
def delete_sample(sample_id: int, conn=Depends(get_conn)):
    cutting.delete_sample(conn, sample_id)
    return {"ok": True}


# ---------------------------------------------------------------- settings & tools

@app.get("/api/settings")
def get_settings(conn=Depends(get_conn)):
    return {
        "spotify_client_id": _client_id(conn),
        "redirect_uri": spotify.redirect_uri(get_port()),
        "port": get_port(),
        "cookies_browser": db.get_setting(conn, "cookies_browser", ""),
        "cookie_browsers": list(COOKIE_BROWSERS),
        "library_path": str(state.library),
        "library_name": db.get_setting(conn, "library_name") or state.library.name,
        "ask_on_launch": libraries.ask_on_launch(),
        "log_dir": str(LOG_DIR),
        "ffmpeg": tools.ffmpeg_dir(),
        "ytdlp": tools.ytdlp_version(),
        "search_ahead": _search_ahead_setting(conn),
        "search_ahead_max": yt_search.AHEAD_MAX,
        "js_runtime": tools.js_runtime(),
        "js_runtime_path": tools.which(tools.js_runtime()) if tools.js_runtime() else None,
        "backups": backup.list_backups(state),
    }


class SettingsPatch(BaseModel):
    library_name: str | None = None
    search_ahead: int | None = None
    spotify_client_id: str | None = None
    cookies_browser: str | None = None
    port: int | None = None


@app.patch("/api/settings")
def patch_settings(body: SettingsPatch, conn=Depends(get_conn)):
    notes = []
    with db.tx(conn):
        if body.search_ahead is not None:
            if not 0 <= body.search_ahead <= yt_search.AHEAD_MAX:
                raise AppError("too_many", f"{yt_search.AHEAD_MAX} max: searching further ahead makes YouTube rate-limit you "
                               "(it can block downloads too).")
            db.set_setting(conn, "search_ahead", str(body.search_ahead))
        if body.library_name is not None:
            if not body.library_name.strip():
                raise AppError("bad_name", "Give the library a name.")
            db.set_setting(conn, "library_name", body.library_name.strip())
        if body.spotify_client_id is not None:
            cid = body.spotify_client_id.strip()
            if cid and not cid.isalnum():
                raise AppError("bad_client_id", "A Client ID is 32 letters and numbers — copy it from your Spotify app's dashboard.")
            db.set_setting(conn, "spotify_client_id", cid)
            _connected_cache.clear()
        if body.cookies_browser is not None:
            if body.cookies_browser not in COOKIE_BROWSERS:
                raise AppError("bad_browser", "Unknown browser.")
            db.set_setting(conn, "cookies_browser", body.cookies_browser)
    if body.port is not None and body.port != get_port():
        if not 1024 <= body.port <= 65535:
            raise AppError("bad_port", "Pick a port between 1024 and 65535.")
        update_config(port=body.port)
        notes.append(f"Restart DJ Crate to use port {body.port}, and add {spotify.redirect_uri(body.port)} as a "
                     "redirect URI in your Spotify dashboard.")
    return {"settings": get_settings(conn), "notes": notes}


@app.post("/api/tools/update_ytdlp")
def update_ytdlp():
    result = tools.update_ytdlp()
    if result["ok"] and state.status == "ok":
        conn = state.conn()
        try:
            ids = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status = 'failed' AND error_code IN ('ytdlp_outdated', 'forbidden')")]
            result["retried"] = downloads.retry(conn, ids) if ids else 0
        finally:
            conn.close()
    return result


@app.post("/api/tools/recheck")
def recheck_tools():
    tools.ytdlp_version(refresh=True)
    downloads.paused_reason = None
    downloads.wake()
    return {"ffmpeg": tools.ffmpeg_dir(), "ytdlp": tools.ytdlp_version()}


# ---------------------------------------------------------------- backups

class ExportBody(BaseModel):
    include_music: bool = False


@app.post("/api/backup/export")
def export_backup(body: ExportBody):
    downloads_dir = Path.home() / "Downloads"
    dest = library.unique_path(downloads_dir, f"DJ Crate Export {dt.datetime.now():%Y-%m-%d %H%M}", ".zip")
    result = backup.export_zip(state, dest, body.include_music)
    tools.reveal(dest)
    return result


@app.post("/api/backup/import")
def import_backup(body: PathBody, conn=Depends(get_conn)):
    busy = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('running', 'queued')").fetchone()[0]
    if busy:
        raise AppError("busy", "Downloads are in progress. Wait for them to finish or Cancel all, then import.")
    conn.close()
    result = backup.import_zip(state, Path(body.path).expanduser())
    state.open(state.library)
    return {**result, "status": status()}


@app.post("/api/backup/now")
def backup_now():
    if state.status != "ok":
        raise AppError("library_unavailable", "The library isn't open.", 409)
    dest = state.backup_dir / f"manual-{dt.datetime.now():%Y-%m-%d-%H%M%S}.db"
    backup._snapshot(state.db_path, dest)
    return {"name": dest.name}


# ---------------------------------------------------------------- misc

class RevealBody(BaseModel):
    track_id: int | None = None
    target: str | None = None  # "library" | "logs"


@app.post("/api/reveal")
def reveal(body: RevealBody):
    if body.track_id:
        conn = state.conn()
        try:
            row = conn.execute("SELECT file_rel_path FROM tracks WHERE id = ?", (body.track_id,)).fetchone()
        finally:
            conn.close()
        p = state.abs_path(row["file_rel_path"]) if row else None
        if not p or not p.exists():
            raise AppError("missing_file", "That file is missing — Relocate it.", 404)
        tools.reveal(p)
    elif body.target == "logs":
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        tools.reveal(LOG_DIR)
    elif body.target == "library" and state.library_reachable():
        tools.reveal(state.library)
    return {"ok": True}


class UrlBody(BaseModel):
    url: str


@app.post("/api/open_url")
def open_url(body: UrlBody):
    if not body.url.startswith(("https://", "http://")):
        raise AppError("bad_url", "Only web links can be opened.")
    tools.open_url(body.url)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
