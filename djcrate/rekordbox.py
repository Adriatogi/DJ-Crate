"""Talking to Rekordbox through its official XML files, never its database.

Two files, one per direction:
  * `rekordbox.xml`, exported by Rekordbox: DJ Crate reads BPM and key from it.
  * `DJ Crate for Rekordbox.xml`, written by DJ Crate: crates and sets as
    playlists, plus notes as each song's Comments. Rekordbox matches songs by
    file path, so nothing is duplicated. BPM, key, grids and cues are never sent.
"""

import json
import logging
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from . import __version__, db, setboard
from .state import state

log = logging.getLogger("djcrate.rekordbox")

EXPORT_NAME = "DJ Crate for Rekordbox.xml"
WATCH_SECONDS = 10

CAMELOT = {
    "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "Dbm",
    "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}
_PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_BY_KEY = {}  # (pitch class, minor?) -> camelot
for _code, _name in CAMELOT.items():
    _minor = _name.endswith("m")
    _root = _name[:-1] if _minor else _name
    _pc = (_PITCH[_root[0]] + {"#": 1, "b": -1, "": 0}[_root[1:]]) % 12
    _BY_KEY[(_pc, _minor)] = _code


def to_camelot(value) -> str | None:
    """Camelot code from either notation ('8A', '08A', 'Am', 'Bbm', 'F♯', 'C maj'); None if unknown."""
    text = (value or "").strip().replace("♯", "#").replace("♭", "b")
    m = re.fullmatch(r"0?(\d{1,2})\s*([ABab])", text)
    if m:
        code = f"{int(m.group(1))}{m.group(2).upper()}"
        return code if code in CAMELOT else None
    m = re.fullmatch(r"([A-Ga-g])([#b]?)\s*(m|min|minor|maj|major|M)?", text)
    if not m:
        return None
    root, acc, quality = m.groups()
    minor = quality in ("m", "min", "minor")  # "M", "maj", "major" or nothing is major
    pc = (_PITCH[root.upper()] + {"#": 1, "b": -1, "": 0}[acc]) % 12
    return _BY_KEY.get((pc, minor))


def key_name(camelot: str | None) -> str | None:
    return CAMELOT.get(camelot or "")


# ---------------------------------------------------------------- paths

def location_to_path(location: str) -> str | None:
    if not location:
        return None
    u = urlparse(location)
    if u.scheme and u.scheme != "file":
        return None
    path = unquote(u.path if u.scheme else location)
    return path or None


def path_to_location(path) -> str:
    return "file://localhost" + quote(str(path), safe="/")


def _norm(path) -> str:
    return unicodedata.normalize("NFC", str(path)).casefold()


# ---------------------------------------------------------------- Rekordbox -> DJ Crate

def parse(path):
    """Yield (absolute path, bpm or None, camelot or None) for each collection track."""
    for _, el in ET.iterparse(str(path), events=("end",)):
        if el.tag != "TRACK":
            continue
        if "Location" in el.attrib:  # skip the playlist-node TRACK refs, which only have Key
            p = location_to_path(el.get("Location", ""))
            if p:
                try:
                    bpm = float(el.get("AverageBpm") or 0) or None
                except ValueError:
                    bpm = None
                yield p, bpm, to_camelot(el.get("Tonality"))
        el.clear()


def _read_xml(path) -> list[tuple]:
    return list(parse(path))


def import_xml(conn, xml_path) -> dict:
    """Copy BPM and key onto the matching library tracks. Songs not in the XML keep theirs."""
    entries = _read_xml(xml_path)
    by_path = {_norm(p): (bpm, key) for p, bpm, key in entries}
    tails = [(_norm(p), (bpm, key)) for p, bpm, key in entries]
    rows = conn.execute("SELECT id, file_rel_path FROM tracks WHERE file_rel_path IS NOT NULL AND file_rel_path <> ''").fetchall()
    matched = 0
    with db.tx(conn):
        for r in rows:
            found = by_path.get(_norm(state.abs_path(r["file_rel_path"])))
            if found is None:  # library moved since the export: match on the path inside it
                tail = "/" + _norm(r["file_rel_path"]).lstrip("/")
                found = next((v for p, v in tails if p.endswith(tail)), None)
            if found is None:
                continue
            bpm, key = found
            conn.execute("UPDATE tracks SET bpm = coalesce(?, bpm), musical_key = coalesce(?, musical_key) WHERE id = ?",
                         (bpm, key, r["id"]))
            matched += 1
    return {"matched": matched, "total": len(rows), "in_xml": len(entries)}


def run_import(conn) -> dict:
    """Import now and remember the outcome. Raises nothing: problems land in `error`."""
    path = db.get_setting(conn, "rekordbox_xml")
    summary = {"at": time.time(), "matched": 0, "total": 0, "in_xml": 0, "error": None}
    try:
        mtime = os.stat(path).st_mtime
        summary.update(import_xml(conn, path))
        db.set_setting(conn, "rekordbox_xml_mtime", str(mtime))
    except FileNotFoundError:
        summary["error"] = "That Rekordbox XML file isn't there anymore."
    except ET.ParseError:
        summary["error"] = "The Rekordbox XML couldn't be read (still being written?). Will retry."
    except Exception as e:  # read-only library, locked database…
        log.exception("Rekordbox import failed")
        summary["error"] = str(e)
    db.set_setting(conn, "rekordbox_import", json.dumps(summary))
    return summary


def check_once(st=None) -> None:
    st = st or state
    if st.status != "ok" or st.read_only:
        return
    conn = st.conn()
    try:
        path = db.get_setting(conn, "rekordbox_xml")
        if path:
            try:
                mtime = str(os.stat(path).st_mtime)
            except OSError:
                mtime = None
            if mtime is not None and mtime != db.get_setting(conn, "rekordbox_xml_mtime"):
                run_import(conn)
        if db.get_setting(conn, "rekordbox_export_on") == "1":
            write_export(st, conn)
    finally:
        conn.close()


def watch_loop() -> None:
    while True:
        try:
            check_once()
        except Exception:
            log.exception("Rekordbox watcher failed")
        time.sleep(WATCH_SECONDS)


# ---------------------------------------------------------------- DJ Crate -> Rekordbox

def _tree(conn, parent_id, ids: dict, skipped: list, counts: dict) -> list[ET.Element]:
    out = []
    q = "SELECT * FROM crates WHERE parent_id IS ? ORDER BY position, id"
    for c in conn.execute(q, (parent_id,)).fetchall():
        if c["kind"] == "folder":
            kids = _tree(conn, c["id"], ids, skipped, counts)
            node = ET.Element("NODE", Type="0", Name=c["name"], Count=str(len(kids)))
            node.extend(kids)
        else:
            rows = conn.execute(
                """SELECT t.id FROM crate_tracks ct JOIN tracks t ON t.id = ct.track_id
                   WHERE ct.crate_id = ? AND ct.removed_from_source = 0 ORDER BY ct.position""", (c["id"],)).fetchall()
            keys = [ids[r["id"]] for r in rows if r["id"] in ids]
            skipped.extend(r["id"] for r in rows if r["id"] not in ids)
            node = _playlist(c["name"], keys)
            counts["playlists"] += 1
        out.append(node)
    return out


def _playlist(name: str, track_ids: list) -> ET.Element:
    node = ET.Element("NODE", Type="1", Name=name, KeyType="0", Entries=str(len(track_ids)))
    for tid in track_ids:
        ET.SubElement(node, "TRACK", Key=str(tid))
    return node


def build_export(st, conn) -> tuple[bytes, dict]:
    """The whole `DJ Crate for Rekordbox.xml`: downloaded songs, crates, and sets."""
    tracks = conn.execute("SELECT * FROM tracks WHERE download_status = 'done' ORDER BY id").fetchall()
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="DJ Crate", Version=__version__, Company="")
    coll = ET.SubElement(root, "COLLECTION")
    ids = {}
    for t in tracks:
        p = st.abs_path(t["file_rel_path"])
        if p is None or not p.exists():
            continue
        ids[t["id"]] = t["id"]
        a = {"TrackID": str(t["id"]), "Name": t["title"], "Artist": t["artists"], "Album": t["album"],
             "Comments": t["notes"] or "", "Kind": "MP3 File", "Location": path_to_location(p)}
        year = (t["release_date"] or "")[:4]
        if year.isdigit():
            a["Year"] = year
        ms = t["file_duration_ms"] or t["duration_ms"]
        if ms:
            a["TotalTime"] = str(round(ms / 1000))
        ET.SubElement(coll, "TRACK", a)
    coll.set("Entries", str(len(ids)))

    counts, skipped = {"playlists": 0}, []
    crates = ET.Element("NODE", Type="0", Name="Crates")
    kids = _tree(conn, None, ids, skipped, counts)
    crates.set("Count", str(len(kids)))
    crates.extend(kids)

    sets = ET.Element("NODE", Type="0", Name="Sets")
    n_sets = 0
    for s in conn.execute("SELECT id, name FROM sets ORDER BY name COLLATE NOCASE, id").fetchall():
        path = setboard.get_board(conn, s["id"])["main_path"]
        songs = [r["track_id"] for r in path if r["kind"] == "song" and r["track_id"]]
        skipped.extend(t for t in songs if t not in ids)
        sets.append(_playlist(s["name"], [t for t in songs if t in ids]))
        counts["playlists"] += 1
        n_sets += 1
    sets.set("Count", str(n_sets))

    top = ET.Element("NODE", Type="0", Name="DJ Crate", Count="2")
    top.extend([crates, sets])
    playlists = ET.SubElement(root, "PLAYLISTS")
    r = ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count="1")
    r.append(top)

    ET.indent(root)
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml, {"playlists": counts["playlists"], "songs": len(ids), "skipped": len(set(skipped))}


def export_path(st=None) -> Path:
    return (st or state).library / EXPORT_NAME


def write_export(st, conn) -> dict:
    """Rewrite the file only if it changed. Problems land in the summary, not as exceptions."""
    try:
        data, summary = build_export(st, conn)
        path = export_path(st)
        if not path.exists() or path.read_bytes() != data:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            summary["at"] = time.time()
        else:
            prev = json.loads(db.get_setting(conn, "rekordbox_export", "{}") or "{}")
            summary["at"] = prev.get("at") or time.time()
        summary["error"] = None
    except Exception as e:
        log.exception("Rekordbox export failed")
        prev = json.loads(db.get_setting(conn, "rekordbox_export", "{}") or "{}")
        summary = {**prev, "error": f"Couldn't write {EXPORT_NAME}: {e}"}
    db.set_setting(conn, "rekordbox_export", json.dumps(summary))
    return summary


def status(conn) -> dict:
    imp = json.loads(db.get_setting(conn, "rekordbox_import", "{}") or "{}")
    exp = json.loads(db.get_setting(conn, "rekordbox_export", "{}") or "{}")
    return {
        "path": db.get_setting(conn, "rekordbox_xml") or None,
        "at": imp.get("at"), "matched": imp.get("matched", 0), "total": imp.get("total", 0),
        "in_xml": imp.get("in_xml", 0), "error": imp.get("error"),
        "export": {"on": db.get_setting(conn, "rekordbox_export_on") == "1", "path": str(export_path()),
                   "at": exp.get("at"), "playlists": exp.get("playlists", 0), "songs": exp.get("songs", 0),
                   "skipped": exp.get("skipped", 0), "error": exp.get("error")},
    }
