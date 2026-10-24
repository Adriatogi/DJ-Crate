"""App-level config that lives outside the library folder.

The only thing stored here is where the library is (plus the port). Everything
else lives inside `<Library>/.djcrate/` so the library folder is portable.
"""

import json
import logging
import logging.handlers
import os
from pathlib import Path

from . import APP_NAME

APP_SUPPORT_DIR = Path(os.environ.get("DJCRATE_APP_SUPPORT", Path.home() / "Library" / "Application Support" / APP_NAME))
LOG_DIR = Path(os.environ.get("DJCRATE_LOG_DIR", Path.home() / "Library" / "Logs" / APP_NAME))
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"

REPO_DIR = Path(__file__).resolve().parent.parent
# Everything lives inside the DJ-Crate folder: code, the app, and libraries.
LIBRARIES_DIR = Path(os.environ.get("DJCRATE_LIBRARIES_DIR", REPO_DIR / "Libraries"))
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


def save_config(cfg: dict) -> None:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_PATH)


def update_config(**changes) -> dict:
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
