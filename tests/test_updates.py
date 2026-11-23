import json
import subprocess
import sys
import time

import httpx
import pytest

from djcrate import __version__, config, tools, updates


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    cfg = config.load_config()
    cfg.pop("update", None)
    cfg.pop("update_check", None)
    config.save_config(cfg)
    monkeypatch.setattr(tools, "online", lambda *a, **k: True)
    yield


def fake(status=200, body=None, etag='"abc"', calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json=body if body is not None else {}, headers={"etag": etag} if etag else {})
    return httpx.Client(transport=httpx.MockTransport(handler))


def release(tag, **kw):
    return {"tag_name": tag, "html_url": f"https://github.com/Adriatogi/DJ-Crate/releases/tag/{tag}", **kw}


def test_newer_release_is_available():
    out = updates.check(force=True, client=fake(body=release("v99.0.0")))
    assert out["available"] and out["latest"] == "99.0.0"
    assert out["url"].endswith("/v99.0.0")
    assert updates.cached()["available"]


def test_same_or_older_release_is_not_available():
    assert not updates.check(force=True, client=fake(body=release(f"v{__version__}")))["available"]
    assert not updates.check(force=True, client=fake(body=release("v0.0.1")))["available"]


def test_version_compare_is_numeric():
    assert updates._key("1.10.0") > updates._key("1.9.9")


@pytest.mark.parametrize("rel", [release("v99.0.0-rc1"), release("nightly"), release("v99.0.0", prerelease=True),
                                 release("v99.0.0", draft=True)])
def test_odd_and_prerelease_tags_are_ignored(rel):
    assert not updates.check(force=True, client=fake(body=rel))["available"]


def test_checks_github_at_most_once_a_day():
    calls = []
    updates.check(force=True, client=fake(body=release("v99.0.0"), calls=calls))
    updates.check(client=fake(body=release("v99.0.0"), calls=calls))
    assert len(calls) == 1
    cfg = config.load_config()
    cfg["update"]["checked_at"] = time.time() - updates.CHECK_EVERY_S - 1
    config.save_config(cfg)
    updates.check(client=fake(body=release("v99.0.0"), calls=calls))
    assert len(calls) == 2


def test_sends_etag_and_keeps_result_on_304():
    updates.check(force=True, client=fake(body=release("v99.0.0")))
    calls = []
    out = updates.check(force=True, client=fake(status=304, calls=calls))
    assert calls[0].headers["if-none-match"] == '"abc"'
    assert out["available"] and out["latest"] == "99.0.0"


def test_errors_are_silent_and_keep_the_last_answer():
    updates.check(force=True, client=fake(body=release("v99.0.0")))

    def boom(request):
        raise httpx.ConnectError("offline")
    out = updates.check(force=True, client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert out["latest"] == "99.0.0"
    assert updates.check(force=True, client=fake(status=403))["latest"] == "99.0.0"


def test_no_releases_yet():
    assert updates.check(force=True, client=fake(status=404, etag=None))["latest"] is None


def test_turned_off_means_no_request():
    updates.set_enabled(False)
    calls = []
    out = updates.check(client=fake(body=release("v99.0.0"), calls=calls))
    assert calls == [] and out["enabled"] is False


def test_offline_means_no_request(monkeypatch):
    monkeypatch.setattr(tools, "online", lambda *a, **k: False)
    calls = []
    updates.check(force=True, client=fake(body=release("v99.0.0"), calls=calls))
    assert calls == []


def test_status_and_settings_report_updates(client):
    st = client.get("/api/status").json()
    assert st["install"] == "git" and st["translocated"] is False
    assert st["update"]["current"] == __version__
    assert client.get("/api/settings").json()["update_check"] is True
    client.patch("/api/settings", json={"update_check": False})
    assert client.get("/api/settings").json()["update_check"] is False
    assert client.get("/api/status").json()["update"]["enabled"] is False


# ---------------------------------------------------------------- running from the app

def _probe(env, code):
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=str(config.REPO_DIR))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_bundle_detection_and_library_default(tmp_path, monkeypatch):
    import os
    code = ("import json; from djcrate import config, tools; print(json.dumps({'bundled': config.BUNDLED, "
            "'libs': str(config.LIBRARIES_DIR), 'trans': config.TRANSLOCATED, 'path': tools.SEARCH_PATH}))")
    env = {k: v for k, v in os.environ.items() if k not in ("DJCRATE_BUNDLE", "DJCRATE_LIBRARIES_DIR")}
    src = _probe(env, code)
    assert src["bundled"] is False and src["libs"] == str(config.REPO_DIR / "Libraries")

    app = tmp_path / "DJ Crate.app"
    app_out = _probe({**env, "DJCRATE_BUNDLE": str(app)}, code)
    assert app_out["bundled"] is True and app_out["trans"] is False
    assert app_out["libs"].endswith("/Music/DJ Crate")
    assert app_out["path"].split(":")[0] == str(app / "Contents" / "Resources" / "bin")

    moved = _probe({**env, "DJCRATE_BUNDLE": "/private/var/folders/x/AppTranslocation/ABC/d/DJ Crate.app"}, code)
    assert moved["trans"] is True


def test_onboarding_progress_and_dismiss(client, lib):
    from djcrate import db
    ob = client.get("/api/status").json()["onboarding"]
    assert ob == {"asked": False, "dismissed": False, "skipped_rekordbox": False, "fresh": True, "playlists": 0, "linked": 0, "downloaded": 0}
    # "No thanks" in the popup
    ob = client.post("/api/onboarding", json={"asked": True, "dismissed": True}).json()
    assert ob["asked"] and ob["dismissed"]
    # Settings → About → Setup guide brings it back
    assert client.post("/api/onboarding", json={"dismissed": False}).json()["dismissed"] is False
    conn = lib.conn()
    with db.tx(conn):
        conn.execute("INSERT INTO tracks (title, artists, source_url) VALUES ('a', 'b', 'https://youtu.be/x')")
    assert client.get("/api/status").json()["onboarding"]["linked"] == 1


def test_library_already_in_use_is_not_asked(client, lib):
    from djcrate import db
    conn = lib.conn()
    with db.tx(conn):
        conn.execute("INSERT INTO tracks (title, artists) VALUES ('a', 'b')")
    ob = client.get("/api/status").json()["onboarding"]
    assert ob["fresh"] is False and ob["asked"] is True and ob["dismissed"] is True


def test_fake_update_shows_the_banner_without_github(monkeypatch):
    monkeypatch.setenv("DJCRATE_FAKE_UPDATE", "9.9.9")
    calls = []
    out = updates.check(force=True, client=fake(body=release("v99.0.0"), calls=calls))
    assert calls == [] and out["available"] and out["latest"] == "9.9.9"
    monkeypatch.setenv("DJCRATE_FAKE_UPDATE", "0.0.1")
    assert not updates.cached()["available"]


def test_skip_rekordbox_step(client, lib):
    assert client.post("/api/onboarding", json={"skipped_rekordbox": True}).json()["skipped_rekordbox"] is True


def test_source_install_gets_the_exact_update_command(client, lib):
    up = client.get("/api/status").json()["update"]
    assert up["install"] == "git" and up["command"].endswith("scripts/update.sh")
