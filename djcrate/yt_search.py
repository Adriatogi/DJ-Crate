"""'Find YouTube link': top results with length match and mix-label warnings.
It only suggests; you always pick the link."""

import json
import logging
import re
import subprocess
from urllib.parse import quote_plus

from . import tools
from .downloader import classify_error, friendly
from .errors import AppError

log = logging.getLogger("djcrate.yt_search")

CLOSE_MS = 3000
RESULTS = 5

# Version words that mean "a different recording/edit than the one on Spotify".
MIX_LABELS = [
    "extended mix", "extended", "radio edit", "club mix", "original mix", "remix", "vip", "bootleg", "edit",
    "live", "acoustic", "instrumental", "acapella", "a cappella", "sped up", "slowed", "nightcore", "reverb",
    "8d", "cover", "karaoke", "bass boosted", "remastered", "remaster", "clean", "dirty", "music video",
]


def _labels(text: str) -> set[str]:
    low = (text or "").lower()
    found = set()
    for label in sorted(MIX_LABELS, key=len, reverse=True):
        pattern = r"\b" + re.escape(label) + r"\b"
        if re.search(pattern, low) and not any(label in f for f in found):
            found.add(label)
    return found


def mix_flags(spotify_title: str, result_title: str) -> list[str]:
    want, got = _labels(spotify_title), _labels(result_title)
    flags = [f"Says “{l.title()}” — not in the Spotify title" for l in sorted(got - want)]
    flags += [f"Missing “{l.title()}”" for l in sorted(want - got)]
    return flags


def query_for(track: dict) -> str:
    return f"{track.get('artists', '')} - {track.get('title', '')}".strip(" -")


def search_url(track: dict) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(query_for(track))


def search(track: dict) -> dict:
    q = query_for(track)
    cmd = [*tools.ytdlp_cmd(), "--flat-playlist", "-J", "--no-warnings", "--socket-timeout", "20", f"ytsearch{RESULTS}:{q}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise AppError("search_failed", "YouTube search timed out.", 502, search_url=search_url(track))
    if out.returncode != 0:
        code = classify_error(out.stderr)
        log.warning("Search failed (%s): %s", code, out.stderr[-500:])
        msg = friendly(code) if code != "unknown" else "YouTube search failed."
        raise AppError("search_failed", msg, 502, search_url=search_url(track), reason=code)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        raise AppError("search_failed", "YouTube search returned something unexpected.", 502, search_url=search_url(track))
    target = track.get("duration_ms")
    results = []
    for e in data.get("entries") or []:
        if not e or not e.get("id"):
            continue
        dur_ms = int(e["duration"] * 1000) if e.get("duration") else None
        diff = (dur_ms - target) if (dur_ms is not None and target) else None
        results.append({
            "url": e.get("url") if str(e.get("url", "")).startswith("http") else f"https://www.youtube.com/watch?v={e['id']}",
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration_ms": dur_ms,
            "diff_ms": diff,
            "close": diff is not None and abs(diff) <= CLOSE_MS,
            "flags": mix_flags(track.get("title", ""), e.get("title") or ""),
        })
    return {"query": q, "search_url": search_url(track), "results": results}
