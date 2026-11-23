"""Tell the user when a new version of DJ Crate is out.

At most once a day it asks GitHub for the latest release (one small request, no personal data)
and compares it with the running version. It never downloads or installs anything: the app shows
a banner with a link to the release page. Any failure (offline, rate-limited) is silent.
Turn it off in Settings (`update_check` in config.json).
"""

import logging
import re
import threading
import time

import httpx

from . import __version__, config, tools

log = logging.getLogger("djcrate.updates")

REPO = "Adriatogi/DJ-Crate"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY_S = 24 * 3600
FIRST_CHECK_DELAY_S = 10
STABLE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

_lock = threading.Lock()


def _key(v: str) -> tuple:
    return tools._version_key(v)


def enabled() -> bool:
    return config.load_config().get("update_check", True) is not False


def set_enabled(on: bool) -> None:
    config.update_config(update_check=bool(on))


def cached() -> dict:
    """What the UI needs, without touching the network."""
    info = config.load_config().get("update") or {}
    latest = info.get("latest")
    return {
        "enabled": enabled(),
        "current": __version__,
        "latest": latest,
        "url": info.get("url") or RELEASES_URL,
        "available": bool(latest) and _key(latest) > _key(__version__),
        "checked_at": info.get("checked_at"),
        "install": "app" if config.BUNDLED else "git",
    }


def check(force: bool = False, client: httpx.Client | None = None) -> dict:
    """Ask GitHub for the latest release (unless checked in the last day). Never raises."""
    with _lock:
        info = dict(config.load_config().get("update") or {})
        if not force and (not enabled() or time.time() - (info.get("checked_at") or 0) < CHECK_EVERY_S):
            return cached()
        if not tools.online():
            return {**cached(), "offline": True}
        headers = {"Accept": "application/vnd.github+json", "User-Agent": f"DJ-Crate/{__version__}"}
        if info.get("etag") and info.get("latest"):
            headers["If-None-Match"] = info["etag"]
        try:
            c = client or httpx.Client(timeout=5)
            try:
                r = c.get(API_URL, headers=headers)
            finally:
                if client is None:
                    c.close()
            if r.status_code == 200:
                data = r.json()
                tag = str(data.get("tag_name") or "")
                if STABLE_TAG.match(tag) and not data.get("prerelease") and not data.get("draft"):
                    info.update(latest=tag.lstrip("v"), url=data.get("html_url") or RELEASES_URL)
                info["etag"] = r.headers.get("etag")
            elif r.status_code == 404:          # no releases yet
                info.update(latest=None, etag=None)
            elif r.status_code != 304:          # 304: nothing new since last time
                log.info("Update check: GitHub answered %s", r.status_code)
                return cached()
            info["checked_at"] = time.time()
            config.update_config(update=info)
        except Exception as e:
            log.info("Update check failed (ignored): %s", e)
        return cached()


def _loop():
    time.sleep(FIRST_CHECK_DELAY_S)
    while True:
        check()
        time.sleep(3600)   # check() itself only goes to GitHub once a day


def start():
    threading.Thread(target=_loop, name="updates", daemon=True).start()
