"""Entry point: start the local server and open the app window.

    uv run python -m djcrate.main            # native window
    uv run python -m djcrate.main --browser  # dev: open in your browser instead
    uv run python -m djcrate.main --fresh-start   # pretend it's a brand-new install (throwaway data)
"""

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import uvicorn

from . import APP_NAME, __version__, backup, config, libraries, library, rekordbox, updates
from .api import app, auto_filler, clean_checker, downloads, hooks, search_ahead
from .config import get_port, setup_logging
from .state import state

log = logging.getLogger("djcrate.main")


def probe_existing(port: int) -> bool:
    """Is DJ Crate already running on this port? If so, bring it forward."""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/ping", timeout=1.5)
        if r.status_code == 200 and r.json().get("app") == "djcrate":
            httpx.post(f"http://127.0.0.1:{port}/api/focus", timeout=1.5)
            return True
    except (httpx.HTTPError, ValueError):
        pass
    return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Same option the server uses: a port still cooling down from the last
        # run (TIME_WAIT) is fine to reuse; only a live listener counts as taken.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def backup_loop():
    while True:
        try:
            backup.daily_backup(state)
        except Exception:
            log.exception("Daily backup failed")
        time.sleep(3600)


def on_library_opened():
    try:
        library.rescan(state)
    except Exception:
        log.exception("Startup missing-file scan failed")
    try:
        backup.daily_backup(state)
    except Exception:
        log.exception("Daily backup failed")
    try:
        rekordbox.check_once(state)
    except Exception:
        log.exception("Rekordbox check failed")
    downloads.wake()


class JsApi:
    """Native bits the web UI can't do itself (exposed as window.pywebview.api)."""

    def __init__(self):
        self.window = None

    def _dialog(self, kind, file_types=()):
        import webview
        result = self.window.create_file_dialog(kind, directory=str(state.library or Path.home()), file_types=file_types)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    def pick_folder(self):
        import webview
        return self._dialog(webview.FileDialog.FOLDER)

    def pick_file(self, kind="mp3"):
        import webview
        types = {"mp3": ("MP3 files (*.mp3)",), "zip": ("DJ Crate export (*.zip)",), "xml": ("Rekordbox XML (*.xml)",)}.get(kind, ())
        return self._dialog(webview.FileDialog.OPEN, types)

    def restart(self):
        log.info("Restarting")
        downloads.stop()
        state.release_lock()
        # Same interpreter, same arguments (the app's launcher settings carry over in the environment).
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def macos_branding():
    """Show "DJ Crate" + our icon in the Dock/menu bar instead of "Python"."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle, NSProcessInfo
        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = APP_NAME
        info["CFBundleDisplayName"] = APP_NAME
        NSProcessInfo.processInfo().setProcessName_(APP_NAME)
        icon = Path(__file__).parent / "web" / "icon.png"
        if config.BUNDLED and (config.BUNDLE_DIR / "Contents" / "Resources" / "AppIcon.icns").exists():
            icon = config.BUNDLE_DIR / "Contents" / "Resources" / "AppIcon.icns"
        if icon.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icon)))
    except Exception:
        log.exception("Couldn't set app name/icon (cosmetic only)")


def fresh_start():
    """Restart this process as if DJ Crate had just been installed: settings, logs and new libraries
    go to a new temp folder (Keychain logins are per library, so a new library starts logged out)."""
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="djcrate-fresh-"))
    env = {**os.environ, "DJCRATE_FRESH": str(root), "DJCRATE_APP_SUPPORT": str(root / "Application Support"),
           "DJCRATE_LOG_DIR": str(root / "Logs"), "DJCRATE_LIBRARIES_DIR": str(root / "Libraries")}
    print(f"Fresh start: everything for this run lives in {root}\n(delete it when you're done; nothing else is touched)", flush=True)
    os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], env)


ERROR_PAGE = """<!doctype html><meta charset=utf-8>
<body style="background:#121214;color:#eee;font:15px -apple-system,sans-serif;padding:40px">
<h2 style="color:#ff6b6b">DJ Crate can't start</h2><p>{message}</p></body>"""


def main():
    parser = argparse.ArgumentParser(prog="djcrate")
    parser.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    parser.add_argument("--headless", action="store_true", help="only run the server (no window, no browser), for tests")
    parser.add_argument("--fresh-start", action="store_true",
                        help="pretend this is a brand-new install: throwaway settings and libraries in a temp folder "
                             "(your real libraries, settings and logins are untouched)")
    args = parser.parse_args()
    if args.fresh_start and not os.environ.get("DJCRATE_FRESH"):
        fresh_start()

    setup_logging()
    if config.BUNDLED:
        # The bundled Python can't see the macOS certificate store; use certifi's (yt-dlp inherits it).
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    port = get_port()
    log.info("Starting %s %s on port %d (%s)", APP_NAME, __version__, port,
             f"app at {config.BUNDLE_DIR}" if config.BUNDLED else "source checkout")

    if os.environ.get("DJCRATE_FRESH") and probe_existing(port):
        print(f"DJ Crate is already running on port {port}. Quit it first: the fresh start uses the same port "
              "so Spotify login works.")
        sys.exit(1)
    if probe_existing(port):
        log.info("Already running; focused the existing window")
        if args.headless:
            sys.exit(1)
        if args.browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    if not port_free(port):
        message = (f"Port {port} is in use by another app. Quit that app, or set a different "
                   f"<code>\"port\"</code> in <code>~/Library/Application Support/DJ Crate/config.json</code> "
                   f"and add <code>http://127.0.0.1:&lt;port&gt;/callback</code> as a redirect URI in your Spotify dashboard.")
        log.error("Port %d in use", port)
        if args.browser or args.headless:
            print(message)
            sys.exit(1)
        import webview
        webview.create_window(APP_NAME, html=ERROR_PAGE.format(message=message), width=640, height=300)
        webview.start()
        sys.exit(1)

    state.on_opened.append(on_library_opened)
    # Several people can share DJ Crate: by default it asks which library to
    # open (with "Don't ask again" to always open the last one).
    last = libraries.last_library()
    if last and not libraries.ask_on_launch():
        state.open(last)
    elif libraries.describe(None):
        state.status = "choose_library"
    downloads.start()
    search_ahead.start()
    clean_checker.start()
    auto_filler.start()
    updates.start()
    threading.Thread(target=backup_loop, name="backups", daemon=True).start()
    threading.Thread(target=rekordbox.watch_loop, name="rekordbox", daemon=True).start()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    server_thread = threading.Thread(target=server.run, name="server", daemon=True)
    server_thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/"

    try:
        if args.browser or args.headless:
            if args.browser:
                webbrowser.open(url)
            while server_thread.is_alive():
                server_thread.join(1)
        else:
            import webview
            macos_branding()
            api = JsApi()
            window = webview.create_window(
                APP_NAME, url, js_api=api, width=1440, height=900, min_size=(1000, 640),
                background_color="#121214", text_select=True,
            )
            api.window = window

            def focus():
                window.restore()
                window.show()

            hooks["focus"] = focus
            webview.start()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down")
        downloads.stop()
        server.should_exit = True
        state.release_lock()


if __name__ == "__main__":
    main()
