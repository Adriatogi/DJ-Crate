import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# Keep tests away from the real ~/Library config, logs and Keychain.
_TMP = Path(tempfile.mkdtemp(prefix="djcrate-tests-"))
os.environ["DJCRATE_APP_SUPPORT"] = str(_TMP / "support")
os.environ["DJCRATE_LOG_DIR"] = str(_TMP / "logs")

import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            import keyring.errors
            raise keyring.errors.PasswordDeleteError()
        del self.store[(service, username)]


keyring.set_keyring(MemoryKeyring())


@pytest.fixture
def lib(tmp_path):
    from djcrate.state import state
    from djcrate import api
    api._connected_cache.clear()
    (tmp_path / "Library").mkdir()
    state.open(tmp_path / "Library")
    assert state.status == "ok", state.error
    yield state
    state.release_lock()
    state.read_only = False


@pytest.fixture
def client(lib):
    from fastapi.testclient import TestClient
    from djcrate.api import app
    return TestClient(app)


@pytest.fixture
def make_mp3(tmp_path):
    if not shutil.which("ffmpeg", path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"):
        pytest.skip("ffmpeg not installed")
    ffmpeg = shutil.which("ffmpeg", path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin")

    def make(path: Path, seconds: float = 3.0) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-b:a", "128k", str(path)], check=True)
        return path

    return make
