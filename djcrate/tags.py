"""ID3 tag read/write with mutagen.

Tags are written as ID3v2.3, which Rekordbox and CDJs read most reliably.
Notes go into the comment frame so they show in Rekordbox's Comments column.
"""

import logging
from pathlib import Path

from mutagen.id3 import APIC, COMM, ID3, TALB, TDRC, TIT2, TPE1, TSRC, ID3NoHeaderError
from mutagen.mp3 import MP3

log = logging.getLogger("djcrate.tags")


def write_tags(path: Path, track: dict, cover: bytes | None = None, cover_mime: str = "image/jpeg") -> None:
    """Write our metadata, keeping any other frames already in the file.
    If `cover` is None, existing artwork is left alone."""
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.setall("TIT2", [TIT2(encoding=3, text=track.get("title") or "")])
    tags.setall("TPE1", [TPE1(encoding=3, text=track.get("artists") or "")])
    tags.setall("TALB", [TALB(encoding=3, text=track.get("album") or "")])
    year = (track.get("release_date") or "")[:4]
    if year.isdigit():
        tags.setall("TDRC", [TDRC(encoding=3, text=year)])
    else:
        tags.delall("TDRC")
    if track.get("isrc"):
        tags.setall("TSRC", [TSRC(encoding=3, text=track["isrc"])])
    tags.delall("COMM")
    if track.get("notes"):
        tags.add(COMM(encoding=3, lang="eng", desc="", text=track["notes"]))
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Cover", data=cover))
    tags.save(path, v2_version=3)


def read_tags(path: Path) -> dict:
    """Metadata for folder import. Raises on unreadable/corrupt files."""
    audio = MP3(path)
    tags = audio.tags or {}

    def text(frame):
        f = tags.get(frame) if hasattr(tags, "get") else None
        return str(f.text[0]).strip() if f and getattr(f, "text", None) else ""

    title = text("TIT2")
    artists = text("TPE1")
    if not title:
        # Fall back to our own filename convention: "Title - Artist.mp3"
        stem = path.stem
        if " - " in stem:
            title, _, guess = stem.partition(" - ")
            artists = artists or guess
        else:
            title = stem
    return {
        "title": title,
        "artists": artists,
        "album": text("TALB"),
        "release_date": text("TDRC")[:10],
        "isrc": text("TSRC") or None,
        "file_duration_ms": int(audio.info.length * 1000),
    }


def duration_ms(path: Path) -> int | None:
    try:
        return int(MP3(path).info.length * 1000)
    except Exception:
        log.warning("Couldn't read duration of %s", path)
        return None
