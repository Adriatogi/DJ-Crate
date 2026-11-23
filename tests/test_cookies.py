import json
import subprocess

import pytest

from djcrate import cookies, db


@pytest.fixture(autouse=True)
def clean():
    cookies.forget()
    yield
    cookies.forget()


def fake_run(monkeypatch, stdout="", stderr="", rc=0, write=True):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if write and rc == 0:
            open(cmd[5], "w").write("# Netscape HTTP Cookie File\n")
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    monkeypatch.setattr(cookies.subprocess, "run", run)
    return calls


def ok_line(count=5, signed_in=True):
    return "DJCOOKIES " + json.dumps({"count": count, "signed_in": signed_in}) + "\n"


def test_export_keeps_a_private_file(monkeypatch):
    calls = fake_run(monkeypatch, stdout=ok_line())
    out = cookies.export("chrome", "Profile 1")
    f = cookies.cookie_file("chrome", "Profile 1")
    assert out["ok"] and f.exists() and oct(f.stat().st_mode & 0o777) == "0o600"
    assert calls[0][3:5] == ["chrome", "Profile 1"]


@pytest.mark.parametrize("stderr,code", [
    ("WARNING: cannot decrypt v10 cookies: no key found", "keychain"),
    ("ERROR: could not find chrome cookies database in \"/x\"", "not_found"),
    ("PermissionError: [Errno 1] Operation not permitted: '/Users/x/Library/Cookies/Cookies.binarycookies'", "safari_access"),
    ("sqlite3.OperationalError: database is locked", "browser_open"),
    ("something else", "failed"),
])
def test_failures_are_explained(monkeypatch, stderr, code):
    fake_run(monkeypatch, stderr=stderr, rc=1)
    with pytest.raises(cookies.CookieError) as e:
        cookies.export("chrome")
    assert e.value.code == code and e.value.message and stderr in e.value.output
    assert not cookies.cookie_file("chrome").exists()


def test_keychain_denied_still_exits_ok_but_is_caught(monkeypatch):
    # yt-dlp only warns when it can't decrypt, then saves the few unencrypted cookies.
    fake_run(monkeypatch, stdout=ok_line(2, signed_in=False), stderr="WARNING: cannot decrypt v10 cookies: no key found")
    with pytest.raises(cookies.CookieError) as e:
        cookies.export("chrome")
    assert e.value.code == "keychain" and "Always Allow" in e.value.message


def test_not_signed_in(monkeypatch):
    fake_run(monkeypatch, stdout=ok_line(3, signed_in=False))
    with pytest.raises(cookies.CookieError) as e:
        cookies.export("firefox")
    assert e.value.code == "not_signed_in" and "Firefox" in e.value.message


def test_reads_the_browser_once_then_reuses_until_stale(monkeypatch, tmp_path):
    calls = fake_run(monkeypatch, stdout=ok_line())
    for _ in range(3):
        cookies.copy_for_job("chrome", "", tmp_path / "c.txt")
    assert len(calls) == 1
    cookies.mark_stale("chrome")
    cookies.copy_for_job("chrome", "", tmp_path / "c.txt")
    assert len(calls) == 2
    monkeypatch.setitem(cookies._exported, ("chrome", ""), 0)   # hours old
    cookies.copy_for_job("chrome", "", tmp_path / "c.txt")
    assert len(calls) == 3


def test_a_failure_isnt_retried_for_every_queued_song(monkeypatch, tmp_path):
    calls = fake_run(monkeypatch, stderr="WARNING: cannot decrypt v10 cookies: no key found", rc=1)
    for _ in range(5):
        with pytest.raises(cookies.CookieError) as e:
            cookies.copy_for_job("chrome", "", tmp_path / "c.txt")
        assert e.value.code == "keychain"
    assert len(calls) == 1               # one Keychain prompt, not five
    with pytest.raises(cookies.CookieError):
        cookies.export("chrome")         # Test tries again
    assert len(calls) == 2


def test_turning_cookies_off_mid_read_keeps_nothing(monkeypatch):
    def run(cmd, **kw):
        open(cmd[5], "w").write("# Netscape HTTP Cookie File\n")
        cookies.forget()                 # user turns cookies off while the browser is being read
        return subprocess.CompletedProcess(cmd, 0, ok_line(), "")
    monkeypatch.setattr(cookies.subprocess, "run", run)
    with pytest.raises(cookies.CookieError) as e:
        cookies.export("chrome")
    assert e.value.code == "off"
    assert not cookies.cookie_file("chrome").exists()
    assert not list(cookies.cookie_file("chrome").parent.glob("*")) if cookies.cookie_file("chrome").parent.exists() else True


def test_export_script_passes_a_logger_that_prints():
    # yt-dlp's default cookie logger is silent, which hid "no key found" (Keychain refused).
    assert "Logger()" in cookies._EXPORT_SCRIPT
    out = subprocess.run([__import__("sys").executable, "-c",
                          cookies._EXPORT_SCRIPT.split("browser, profile, path")[0] + "Logger().warning('cannot decrypt v10 cookies: no key found', only_once=True)"],
                         capture_output=True, text=True)
    assert "no key found" in out.stderr, out.stderr


def test_profiles_from_local_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    for d in ("Default", "Profile 1", "Guest Profile"):
        (root / d).mkdir(parents=True)
    (root / "Default" / "Cookies").write_text("")
    (root / "Profile 1" / "Network").mkdir()
    (root / "Profile 1" / "Network" / "Cookies").write_text("")
    (root / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "DJ"}}}}))
    assert cookies.profiles("chrome") == [{"id": "Default", "name": "Default"}, {"id": "Profile 1", "name": "DJ"}]
    assert cookies.profiles("safari") == []


def test_real_export_script_runs_with_yt_dlp():
    """The script really runs against yt-dlp (a profile that doesn't exist: no browser or Keychain touched)."""
    with pytest.raises(cookies.CookieError) as e:
        cookies.export("chrome", "/nonexistent/djcrate-test-profile")
    assert e.value.code == "not_found", e.value.output


def test_settings_and_test_route(client, lib, monkeypatch):
    fake_run(monkeypatch, stdout=ok_line())
    assert client.post("/api/cookies/test").json()["error"]["code"] == "cookies_off"
    client.patch("/api/settings", json={"cookies_browser": "chrome"})
    r = client.post("/api/cookies/test").json()
    assert r["ok"] and "Chrome" in r["message"]
    assert client.patch("/api/settings", json={"cookies_profile": "Nope"}).status_code == 400
    s = client.get("/api/settings").json()
    assert s["cookies_browser"] == "chrome" and "cookie_profiles" in s
    client.patch("/api/settings", json={"cookies_browser": ""})
    assert not cookies.cookie_file("chrome").exists()


def test_failed_test_route_returns_the_reason(client, lib, monkeypatch):
    fake_run(monkeypatch, stderr="WARNING: cannot decrypt v10 cookies: no key found", rc=1)
    client.patch("/api/settings", json={"cookies_browser": "chrome"})
    r = client.post("/api/cookies/test").json()
    assert r["ok"] is False and r["code"] == "keychain" and "no key found" in r["output"]
