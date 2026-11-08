import json
import subprocess
import time

import pytest

from djcrate import yt_search
from djcrate.downloader import classify_error

ENTRIES = {"entries": [{"id": "abc", "title": "Song (Radio Edit)", "channel": "ch", "duration": 180}]}


class Runs:
    def __init__(self, monkeypatch, result=None):
        self.calls = 0
        self.result = result or subprocess.CompletedProcess([], 0, json.dumps(ENTRIES), "")
        monkeypatch.setattr(yt_search.subprocess, "run", self)

    def __call__(self, *a, **k):
        self.calls += 1
        return self.result


def track(client, title="Song", **kw):
    t = client.post("/api/tracks", json={"title": title, "artists": "A", **kw}).json()["track"]
    return t["id"]


def test_cache_hit_rename_and_refresh(client, monkeypatch):
    runs = Runs(monkeypatch)
    tid = track(client)
    first = client.get(f"/api/tracks/{tid}/yt_search").json()
    assert runs.calls == 1 and first["cached_at"] is None and first["results"][0]["flags"]
    second = client.get(f"/api/tracks/{tid}/yt_search").json()
    assert runs.calls == 1 and second["cached_at"]  # instant: no new search
    client.patch(f"/api/tracks/{tid}", json={"title": "Renamed"})
    client.get(f"/api/tracks/{tid}/yt_search")
    assert runs.calls == 2  # different query → searched again
    client.get(f"/api/tracks/{tid}/yt_search?refresh=1")
    assert runs.calls == 3


def test_stale_cache_searches_again(client, lib, monkeypatch):
    runs = Runs(monkeypatch)
    tid = track(client)
    client.get(f"/api/tracks/{tid}/yt_search")
    lib.conn().execute("UPDATE yt_search_cache SET fetched_at = ?", (time.time() - 8 * 86400,))
    client.get(f"/api/tracks/{tid}/yt_search")
    assert runs.calls == 2


def test_search_ahead_caps_skips_and_pauses(client, lib, monkeypatch):
    from djcrate.api import search_ahead
    search_ahead.paused_until = 0
    runs = Runs(monkeypatch)
    ids = [track(client, f"S{i}") for i in range(6)]
    linked = track(client, "Linked", source_url="https://youtu.be/x")
    r = client.post("/api/yt_search/ahead", json={"ids": [linked, *ids]}).json()
    assert r["queued"] == 3  # default
    while search_ahead.step():
        pass
    assert runs.calls == 2  # the linked one was skipped
    runs.result = subprocess.CompletedProcess([], 1, "", "ERROR: HTTP Error 429: Too Many Requests")
    client.post("/api/yt_search/ahead", json={"ids": ids[2:]})
    search_ahead.step()
    assert search_ahead.paused and client.get("/api/status").json()["search"]["paused_until"]
    assert client.post("/api/yt_search/ahead", json={"ids": ids}).json()["queued"] == 0
    search_ahead.paused_until = 0


def test_own_search_rate_limited_message(client, monkeypatch):
    from djcrate.api import search_ahead
    Runs(monkeypatch, subprocess.CompletedProcess([], 1, "", "ERROR: Sign in to confirm you're not a bot"))
    tid = track(client)
    r = client.get(f"/api/tracks/{tid}/yt_search")
    assert r.status_code == 429 and "rate-limiting" in r.json()["error"]["message"]
    assert search_ahead.paused
    search_ahead.paused_until = 0


def test_search_ahead_setting_limits(client):
    assert client.patch("/api/settings", json={"search_ahead": 11}).status_code == 400
    assert "rate-limit" in client.patch("/api/settings", json={"search_ahead": 11}).json()["error"]["message"]
    assert client.patch("/api/settings", json={"search_ahead": 10}).json()["settings"]["search_ahead"] == 10
    assert client.patch("/api/settings", json={"search_ahead": 0}).json()["settings"]["search_ahead"] == 0


def test_rate_limit_classification():
    assert classify_error("ERROR: HTTP Error 429: Too Many Requests") == "rate_limited"
    assert yt_search.rate_limited("ERROR: Sign in to confirm you’re not a bot")
