"""ingest_api.py — Agent A. The extension's local HTTP endpoint (CONTRACTS §6.1).

The blueprint is mounted on a throwaway Flask app with an in-memory database
and `FakeYouTubeApi`; nothing binds a socket or reaches the network.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from flask import Flask

import clock
import config
import db
import dr_brainrot_types as T
import ingest_api
from dr_brainrot_types import VideoInfo, VideoLookupError
from sources import youtube
from sources.youtube import FakeYouTubeApi

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "youtube"
TOKEN = "t0k3n-for-tests"


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw", ingest_token=TOKEN)


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


class Factory:
    def __init__(self, api):
        self.api = api
        self.built = 0
        self.closed = 0
        api.close = self._close

    def _close(self):
        self.closed += 1

    def __call__(self):
        self.built += 1
        return self.api


def all_videos():
    initial = json.loads((FIXTURES / "history_initial.json").read_text())
    ids = [e.video_id for e in youtube.parse_pages([initial], captured_at="2026-09-16T10:00:00Z",
                                                    tz="Europe/Warsaw").entries]
    return initial, [VideoInfo(v, f"title {v}", "UCapiChannelAAAAAAAAAAAA", "Channel A", 300) for v in ids]


def make_client(conn, settings, api):
    factory = Factory(api)
    app = Flask(__name__)
    app.register_blueprint(ingest_api.make_blueprint(
        get_conn=lambda: conn, clk=clock.FrozenClock("2026-09-16T10:05:00+00:00"),
        settings=settings, api_factory=factory))
    return app.test_client(), factory


def body(pages):
    return json.dumps({"schema": 1, "captured_at": "2026-09-16T10:00:00Z", "extension_version": "0.1.0",
                       "cursor": None, "cursor_reached": False, "pages": pages}).encode()


@pytest.mark.parametrize("headers", [{}, {"X-Dr-Brainrot-Token": "wrong"}, {"X-Dr-Brainrot-Token": ""},
                                     {"X-Dr-Brainrot-Token": TOKEN + "x"}])
def test_requests_without_the_right_token_are_401(conn, settings, headers):
    client, factory = make_client(conn, settings, FakeYouTubeApi())
    assert client.get("/api/youtube/cursor", headers=headers).status_code == 401
    r = client.post("/api/youtube/history", data=body([]), headers=headers)
    assert r.status_code == 401
    assert factory.built == 0                                    # no quota spent on strangers
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_unset_server_token_refuses_everything(conn, tmp_path):
    s = config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw", ingest_token="")
    client, _ = make_client(conn, s, FakeYouTubeApi())
    assert client.get("/api/youtube/cursor", headers={"X-Dr-Brainrot-Token": ""}).status_code == 401


def test_cursor_get(conn, settings):
    client, _ = make_client(conn, settings, FakeYouTubeApi())
    r = client.get("/api/youtube/cursor", headers={"X-Dr-Brainrot-Token": TOKEN})
    assert r.status_code == 200 and r.get_json() == {"cursor": None, "cursor_date": None}


def test_post_history_ingests_and_moves_cursor(conn, settings):
    initial, videos = all_videos()
    client, factory = make_client(conn, settings, FakeYouTubeApi(videos))
    r = client.post("/api/youtube/history", data=body([initial]),
                    headers={"X-Dr-Brainrot-Token": TOKEN, "Content-Type": "application/json"})
    assert r.status_code == 200
    out = r.get_json()
    assert set(out) == {"import_id", "status", "measurements_written", "warnings", "error",
                        "period_start", "period_end", "duplicate_of"}
    assert out["status"] == "partial" and out["measurements_written"] == 13
    assert out["error"] is None and isinstance(out["warnings"], list) and out["warnings"]
    assert (factory.built, factory.closed) == (1, 1)

    r = client.get("/api/youtube/cursor", headers={"X-Dr-Brainrot-Token": TOKEN})
    assert r.get_json() == {"cursor": "fxVideo0001", "cursor_date": "2026-09-16"}

    again = client.post("/api/youtube/history", data=body([initial]), headers={"X-Dr-Brainrot-Token": TOKEN})
    assert again.status_code == 200 and again.get_json()["duplicate_of"] == out["import_id"]


def test_failed_import_is_422_with_the_reason(conn, settings):
    initial, _ = all_videos()
    client, factory = make_client(conn, settings, FakeYouTubeApi(error=VideoLookupError("quota exhausted")))
    r = client.post("/api/youtube/history", data=body([initial]), headers={"X-Dr-Brainrot-Token": TOKEN})
    assert r.status_code == 422
    out = r.get_json()
    assert out["status"] == "failed" and "quota" in out["error"] and out["measurements_written"] == 0
    assert factory.closed == 1


def test_broken_structure_is_reported_as_failure_not_zero_videos(conn, settings):
    client, _ = make_client(conn, settings, FakeYouTubeApi())
    r = client.post("/api/youtube/history", data=body([{"contents": {}}]), headers={"X-Dr-Brainrot-Token": TOKEN})
    assert r.status_code == 422
    assert "no itemSectionRenderer" in r.get_json()["error"]
    assert conn.execute("SELECT status FROM imports").fetchone()["status"] == T.IMPORT_FAILED


def test_body_over_limit_is_413(conn, settings, monkeypatch):
    monkeypatch.setattr(ingest_api, "MAX_BODY_BYTES", 100)
    client, factory = make_client(conn, settings, FakeYouTubeApi())
    r = client.post("/api/youtube/history", data=b"x" * 101, headers={"X-Dr-Brainrot-Token": TOKEN})
    assert r.status_code == 413
    assert factory.built == 0
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_real_limit_is_25_mb():
    assert ingest_api.MAX_BODY_BYTES == 25 * 1024 * 1024
