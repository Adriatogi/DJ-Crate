"""External tools: ffmpeg, yt-dlp, uv. And a quick online check.

Apps launched from Finder get a bare PATH (/usr/bin:/bin...), so Homebrew and
uv locations are added explicitly.
"""

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("djcrate.tools")

EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"), str(Path.home() / ".cargo" / "bin")]
SEARCH_PATH = os.pathsep.join([os.environ.get("PATH", ""), *EXTRA_PATHS])

_ytdlp_version: str | None = None
_online_cache: tuple[float, bool] = (0.0, True)
_online_lock = threading.Lock()


def which(name: str) -> str | None:
    return shutil.which(name, path=SEARCH_PATH)


def ffmpeg_dir() -> str | None:
    ffmpeg, ffprobe = which("ffmpeg"), which("ffprobe")
    if ffmpeg and ffprobe and Path(ffmpeg).parent == Path(ffprobe).parent:
        return str(Path(ffmpeg).parent)
    return str(Path(ffmpeg).parent) if ffmpeg else None


def ytdlp_cmd() -> list[str]:
    """yt-dlp needs a JavaScript runtime for YouTube (otherwise formats go
    missing). It only looks for deno by default, so point it at node if that's
    what's installed."""
    cmd = [sys.executable, "-m", "yt_dlp"]
    if not which("deno"):
        node = which("node")
        if node:
            cmd += ["--js-runtimes", f"node:{node}"]
    return cmd


def js_runtime() -> str | None:
    return "deno" if which("deno") else "node" if which("node") else None


def ytdlp_version(refresh: bool = False) -> str | None:
    global _ytdlp_version
    if _ytdlp_version is None or refresh:
        try:
            out = subprocess.run([*ytdlp_cmd(), "--version"], capture_output=True, text=True, timeout=30)
            _ytdlp_version = out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            _ytdlp_version = None
    return _ytdlp_version


def update_ytdlp() -> dict:
    """Upgrade yt-dlp inside the app's own environment. If it fails, the old
    version stays installed and keeps working."""
    before = ytdlp_version()
    uv = which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", sys.executable, "--upgrade", "yt-dlp"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
    log.info("Updating yt-dlp: %s", " ".join(cmd))
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env={**os.environ, "PATH": SEARCH_PATH})
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "before": before, "after": before, "output": str(e)}
    after = ytdlp_version(refresh=True)
    ok = out.returncode == 0 and after is not None
    if not ok:
        log.error("yt-dlp update failed: %s", out.stderr[-2000:])
    return {"ok": ok, "before": before, "after": after, "output": (out.stdout + out.stderr)[-2000:]}


def online(max_age: float = 10.0) -> bool:
    global _online_cache
    with _online_lock:
        checked_at, result = _online_cache
        if time.time() - checked_at < max_age:
            return result
        try:
            with socket.create_connection(("www.youtube.com", 443), timeout=3):
                result = True
        except OSError:
            result = False
        _online_cache = (time.time(), result)
        return result


def reveal(path: Path) -> None:
    subprocess.run(["open", "-R", str(path)] if path.is_file() else ["open", str(path)], check=False)


def open_url(url: str) -> None:
    subprocess.run(["open", url], check=False)
