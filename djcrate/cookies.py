"""YouTube cookies from your browser, read once instead of on every download.

yt-dlp's --cookies-from-browser reads the browser's whole cookie store each time it runs. For
Chrome (and Brave, Edge…) that means asking the macOS Keychain for "Chrome Safe Storage" from a
new process on every download, two at a time, so the Keychain prompt piles up, gets denied, and
yt-dlp quietly carries on without cookies ("cannot decrypt … no key found"). YouTube then asks
you to sign in, even though cookies are on.

So DJ Crate reads them once (one Keychain prompt: choose Always Allow), keeps only YouTube and
Google cookies in a private file in Application Support, and hands each download a copy with
--cookies. They're read again every few hours, or when YouTube still asks to sign in.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, tools

log = logging.getLogger("djcrate.cookies")

MAX_AGE_S = 6 * 3600
EXPORT_TIMEOUT_S = 180   # long enough to answer the Keychain prompt
KEEP_DOMAINS = ("youtube.com", "google.com", "googlevideo.com", "youtu.be")
SIGNED_IN_NAMES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PSID", "LOGIN_INFO", "SID")

# Chromium-based browsers keep profiles in these folders (under ~/Library/Application Support).
PROFILE_DIRS = {
    "chrome": "Google/Chrome",
    "brave": "BraveSoftware/Brave-Browser",
    "edge": "Microsoft Edge",
    "chromium": "Chromium",
    "vivaldi": "Vivaldi",
}

_lock = threading.Lock()
_exported: dict[tuple, float] = {}   # (browser, profile) -> when read in this run
_stale: set[tuple] = set()
_failed: dict[tuple, "CookieError"] = {}   # last failure per (browser, profile), until Test or a settings change
_generation = 0                            # bumped by forget(), so a read in progress is thrown away

MESSAGES = {
    "keychain": ("macOS didn't let DJ Crate read {name}'s cookies. Click Test, and when macOS asks for "
                 "“{name} Safe Storage”, enter your Mac password and choose Always Allow."),
    "not_found": ("Couldn't find {name}'s cookies. Is {name} installed and opened at least once? "
                  "If you use several profiles, pick the one signed in to YouTube."),
    "safari_access": ("Safari's cookies need Full Disk Access: System Settings → Privacy & Security → Full Disk Access, "
                      "turn on DJ Crate (or Terminal, if you run DJ Crate from there), then reopen DJ Crate."),
    "not_signed_in": "Got {name}'s cookies, but you aren't signed in to YouTube there. Sign in at youtube.com in {name}, then click Test.",
    "browser_open": "{name} is locking its cookies. Quit {name} completely, then click Test.",
    "failed": "Couldn't read {name}'s cookies.",
    "off": "Cookies were turned off.",
}


class CookieError(Exception):
    def __init__(self, code: str, message: str, output: str = ""):
        super().__init__(message)
        self.code, self.message, self.output = code, message, output


def browser_name(browser: str) -> str:
    return {"edge": "Edge", "chromium": "Chromium"}.get(browser, browser[:1].upper() + browser[1:])


def profiles(browser: str) -> list[dict]:
    """The profiles of a Chromium-based browser that have cookies: [{id, name}]. [] if none or not applicable."""
    sub = PROFILE_DIRS.get(browser)
    if not sub:
        return []
    root = Path.home() / "Library" / "Application Support" / sub
    try:
        cache = json.loads((root / "Local State").read_text()).get("profile", {}).get("info_cache", {})
    except (OSError, ValueError):
        cache = {}
    out = []
    for d in sorted(root.glob("*/")):
        if (d / "Cookies").exists() or (d / "Network" / "Cookies").exists():
            out.append({"id": d.name, "name": (cache.get(d.name) or {}).get("name") or d.name})
    return out


def cookie_file(browser: str, profile: str = "") -> Path:
    safe = "".join(ch if ch.isalnum() else "_" for ch in f"{browser}_{profile}" if ch)
    return config.APP_SUPPORT_DIR / "cookies" / f"{safe}.txt"


# Runs in a separate Python (the same yt-dlp the downloads use): read the browser's cookies, keep YouTube's.
_EXPORT_SCRIPT = r"""
import json, sys
from yt_dlp.cookies import extract_cookies_from_browser, YoutubeDLCookieJar, YDLLogger

class Logger(YDLLogger):   # yt-dlp's default one prints nothing without a YoutubeDL; we need its warnings
    def warning(self, message, only_once=False):
        print("WARNING: " + message, file=sys.stderr)
    def error(self, message):
        print("ERROR: " + message, file=sys.stderr)

browser, profile, path, keep, names = sys.argv[1], sys.argv[2] or None, sys.argv[3], tuple(sys.argv[4].split(",")), set(sys.argv[5].split(","))
jar = extract_cookies_from_browser(browser, profile, Logger())
out = YoutubeDLCookieJar(path)
for c in jar:
    d = c.domain.lstrip(".")
    if any(d == k or d.endswith("." + k) for k in keep):
        out.set_cookie(c)
out.save(ignore_discard=True, ignore_expires=True)
print("DJCOOKIES " + json.dumps({"count": len(out), "signed_in": any(c.name in names for c in out)}))
"""


def _classify(text: str) -> str:
    low = text.lower()
    if "no key found" in low or "cannot decrypt" in low or "safe storage" in low or ("keychain" in low and "fail" in low):
        return "keychain"
    if "operation not permitted" in low or "permission denied" in low:
        return "safari_access"
    if ("could not find" in low and "cookie" in low) or "no such file" in low:
        return "not_found"
    if "database is locked" in low or "could not copy" in low:
        return "browser_open"
    return "failed"


def export(browser: str, profile: str = "") -> dict:
    """Read the browser's YouTube cookies into our file now (Settings → Test). Raises CookieError."""
    with _lock:
        _failed.pop((browser, profile), None)   # an explicit Test always tries again
        return _export(browser, profile)


def _export(browser: str, profile: str) -> dict:
    """Call with _lock held (one read at a time, so at most one Keychain prompt)."""
    key = (browser, profile)
    gen = _generation
    name = browser_name(browser)
    dest = cookie_file(browser, profile)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    tmp = dest.with_name(f"{dest.stem}.{os.getpid()}.{threading.get_ident()}.new")
    cmd = [sys.executable, "-c", tools.ytdlp_python_prefix() + _EXPORT_SCRIPT,
           browser, profile, str(tmp), ",".join(KEEP_DOMAINS), ",".join(SIGNED_IN_NAMES)]
    log.info("Reading YouTube cookies from %s%s", name, f" ({profile})" if profile else "")
    try:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=EXPORT_TIMEOUT_S,
                                 env={**os.environ, "PATH": tools.SEARCH_PATH})
        except subprocess.TimeoutExpired:
            _fail(key, CookieError("keychain", MESSAGES["keychain"].format(name=name), "Timed out waiting (Keychain prompt not answered?)"))
        text = (out.stdout or "") + (out.stderr or "")
        output = "\n".join(l for l in text.splitlines() if l.strip() and not l.startswith("DJCOOKIES"))[-2000:]
        result = next((json.loads(l[10:]) for l in (out.stdout or "").splitlines() if l.startswith("DJCOOKIES ")), None)
        if out.returncode != 0 or result is None:
            code = _classify(text)
            log.warning("Couldn't read %s cookies (%s): %s", name, code, output[-300:])
            _fail(key, CookieError(code, MESSAGES[code].format(name=name), output))
        if not result["signed_in"]:
            # A Keychain refusal drops the encrypted cookies but still exits fine: check the warning first.
            code = "keychain" if _classify(text) == "keychain" else "not_signed_in"
            _fail(key, CookieError(code, MESSAGES[code].format(name=name), output))
        if gen != _generation:   # cookies were turned off (or changed) meanwhile: don't keep these
            raise CookieError("off", MESSAGES["off"])
        try:
            os.chmod(tmp, 0o600)
            tmp.replace(dest)
        except OSError as e:
            raise CookieError("off" if gen != _generation else "failed", MESSAGES["failed"].format(name=name), str(e))
    finally:
        tmp.unlink(missing_ok=True)
    _exported[key] = time.time()
    _stale.discard(key)
    _failed.pop(key, None)
    return {"ok": True, "count": result["count"], "message": f"Signed in to YouTube in {name}: DJ Crate can use it.", "output": output}


def _fail(key, err: CookieError):
    _failed[key] = err     # don't ask again for every queued song; Test or a settings change retries
    raise err


def copy_for_job(browser: str, profile: str, dest: Path) -> str:
    """A fresh-enough copy of the cookies for one download (yt-dlp writes cookies back when it exits).
    The browser is read at most every few hours; after a failure, downloads fail fast until you Test again."""
    key = (browser, profile)
    with _lock:
        if key in _failed:
            raise _failed[key]
        f = cookie_file(browser, profile)
        fresh = f.exists() and key in _exported and time.time() - _exported[key] < MAX_AGE_S and key not in _stale
        if not fresh:
            _export(browser, profile)
        try:
            shutil.copyfile(f, dest)
        except OSError as e:   # turned off just now
            raise CookieError("off", MESSAGES["off"], str(e))
    os.chmod(dest, 0o600)
    return str(dest)


def mark_stale(browser: str, profile: str = "") -> None:
    """YouTube still asked to sign in: read the browser again before the next download."""
    with _lock:
        _stale.add((browser, profile))


def forget() -> None:
    """Cookies turned off or changed: delete the saved ones (and discard any read in progress)."""
    # No lock: a read in progress may be waiting on a Keychain prompt. Bumping the generation makes
    # it throw its result away instead of saving it.
    global _generation
    _generation += 1
    _exported.clear()
    _stale.clear()
    _failed.clear()
    shutil.rmtree(config.APP_SUPPORT_DIR / "cookies", ignore_errors=True)
