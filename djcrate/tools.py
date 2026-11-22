"""External tools: ffmpeg, yt-dlp, uv. And a quick online check.

Apps launched from Finder get a bare PATH (/usr/bin:/bin...), so Homebrew and
uv locations are added explicitly.
"""

import logging
import os
import re
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


# ---------------------------------------------------------------- yt-dlp
# "Update yt-dlp" installs into DJ Crate's own folder in Application Support, not the project's
# environment: `uv run` resets that environment to uv.lock on every start, which used to undo
# the update. Whichever copy is newer (the updated one or the project's own) is used.

def updated_ytdlp_dir() -> Path:
    from . import config
    return config.APP_SUPPORT_DIR / "yt-dlp"


def _version_key(v: str | None) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", v or ""))


def _updated_version(d: Path | None = None) -> str | None:
    """The version of the updated copy, read from its files (no need to run it)."""
    f = (d or updated_ytdlp_dir()) / "yt_dlp" / "version.py"
    try:
        m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", f.read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def _bundled_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:
        return None


def _ytdlp_base(d: Path | None = None) -> list[str]:
    d = d or updated_ytdlp_dir()
    updated = _updated_version(d)
    if updated and _version_key(updated) >= _version_key(_bundled_version()):
        # Run the updated copy: its folder goes first on the import path.
        return [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(d)!r}); import yt_dlp; yt_dlp.main()"]
    return [sys.executable, "-m", "yt_dlp"]


def ytdlp_cmd() -> list[str]:
    """yt-dlp needs a JavaScript runtime for YouTube (otherwise formats go
    missing). It only looks for deno by default, so point it at node if that's
    what's installed."""
    cmd = _ytdlp_base()
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
    """Install the latest yt-dlp into DJ Crate's own folder (see updated_ytdlp_dir), so it survives
    restarts. It goes into a fresh folder first and only replaces the old one once it runs, so a
    failed update leaves the working version in place."""
    before = ytdlp_version()
    final = updated_ytdlp_dir()
    new = final.with_name("yt-dlp.new")
    shutil.rmtree(new, ignore_errors=True)
    new.parent.mkdir(parents=True, exist_ok=True)
    uv = which("uv")
    if uv:
        cmd = [uv, "pip", "install", "--python", sys.executable, "--target", str(new), "--upgrade", "yt-dlp"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--target", str(new), "--upgrade", "yt-dlp"]
    log.info("Updating yt-dlp: %s", " ".join(cmd))
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env={**os.environ, "PATH": SEARCH_PATH})
    except (OSError, subprocess.SubprocessError) as e:
        shutil.rmtree(new, ignore_errors=True)
        return {"ok": False, "before": before, "after": before, "output": str(e)}
    output = (out.stdout + out.stderr)[-2000:]
    works = False
    if out.returncode == 0 and _updated_version(new):
        try:
            check = subprocess.run([*_ytdlp_base(new), "--version"], capture_output=True, text=True, timeout=60)
            works = check.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    if not works:
        shutil.rmtree(new, ignore_errors=True)
        log.error("yt-dlp update failed: %s", output)
        return {"ok": False, "before": before, "after": ytdlp_version(refresh=True), "output": output}
    old = final.with_name("yt-dlp.old")
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.rename(old)
    new.rename(final)
    shutil.rmtree(old, ignore_errors=True)
    after = ytdlp_version(refresh=True)
    log.info("yt-dlp updated: %s -> %s (in %s)", before, after, final)
    return {"ok": after is not None, "before": before, "after": after, "output": output}


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


def pick_path(kind: str = "file", extensions: tuple = ()) -> str | None:
    """Native macOS Finder picker for when the app runs in a browser tab. None if cancelled."""
    what = "folder" if kind == "folder" else "file"
    types = " of type {%s}" % ", ".join(f'"{e}"' for e in extensions) if extensions and what == "file" else ""
    script = f'POSIX path of (choose {what} with prompt "Choose a {what}"{types})'
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip().rstrip("\n") or None if out.returncode == 0 else None


def open_url(url: str) -> None:
    subprocess.run(["open", url], check=False)
