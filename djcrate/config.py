"""App-level config that lives outside the library folder.

The only thing stored here is where the library is (plus the port). Everything
else lives inside `<Library>/.djcrate/` so the library folder is portable.
"""

import json
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from . import APP_NAME

APP_SUPPORT_DIR = Path(os.environ.get("DJCRATE_APP_SUPPORT", Path.home() / "Library" / "Application Support" / APP_NAME))
LOG_DIR = Path(os.environ.get("DJCRATE_LOG_DIR", Path.home() / "Library" / "Logs" / APP_NAME))
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"

REPO_DIR = Path(__file__).resolve().parent.parent


def find_bundle() -> Path | None:
    """The DJ Crate.app we're running from (the downloadable app), or None for a source checkout.
    The app's launcher sets DJCRATE_BUNDLE; otherwise look for our Python inside an .app."""
    env = os.environ.get("DJCRATE_BUNDLE")
    if env:
        return Path(env)
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app" and exe.is_relative_to(parent / "Contents" / "Resources" / "python"):
            return parent
    return None


BUNDLE_DIR = find_bundle()
BUNDLED = BUNDLE_DIR is not None
# macOS runs a just-downloaded app from a random read-only copy until it's moved to Applications.
TRANSLOCATED = BUNDLED and "/AppTranslocation/" in str(BUNDLE_DIR)

# A source checkout keeps libraries next to the code; the app keeps them in ~/Music
# (outside the app, so replacing the app with a new version never touches them).
LIBRARIES_DIR = Path(os.environ.get(
    "DJCRATE_LIBRARIES_DIR", Path.home() / "Music" / APP_NAME if BUNDLED else REPO_DIR / "Libraries"))
DEFAULT_LIBRARY = LIBRARIES_DIR / "My Library"
DEFAULT_PORT = 8765

log = logging.getLogger("djcrate")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.exception("config.json unreadable; starting with defaults")
        return {}


_write_lock = threading.RLock()   # the update check and Settings can both write config.json


def save_config(cfg: dict) -> None:
    with _write_lock:
        APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_name(f"config.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        tmp.replace(CONFIG_PATH)


def update_config(**changes) -> dict:
    with _write_lock:
        cfg = load_config()
        cfg.update(changes)
        save_config(cfg)
        return cfg


def get_port() -> int:
    try:
        return int(load_config().get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("djcrate")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "djcrate.log", maxBytes=2_000_000, backupCount=5)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
