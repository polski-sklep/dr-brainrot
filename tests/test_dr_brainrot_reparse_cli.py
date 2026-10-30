"""reparse.py: routing by import kind and listing, without network or Vision."""
from __future__ import annotations

import pathlib

import pytest

import clock
import config
import db
import reparse
from sources import coldturkey

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "coldturkey" / "website_statistics.json"


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw")


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-20T10:00:00+00:00")


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


def test_reparse_routes_by_kind_and_writes_a_new_import(conn, clk, settings):
    first = coldturkey.ingest(conn, clk, settings, FIXTURE.read_bytes(), filename="x.json")
    assert first.status in ("ok", "partial")
    again = reparse.reparse_import(conn, clk, settings, first.import_id)
    assert again.import_id != first.import_id and again.status == first.status
    assert any(line.startswith(f"#{again.import_id}") for line in reparse.list_imports(conn, "coldturkey"))


def test_unknown_import_and_vault_scans_are_refused(conn, clk, settings):
    with pytest.raises(ValueError):
        reparse.reparse_import(conn, clk, settings, 999)
    conn.execute("INSERT INTO imports (kind, received_at, parser_version, status) "
                 "VALUES ('vault_scan', '2026-09-20T10:00:00+00:00', 'vault-1', 'ok')")
    vid = conn.execute("SELECT MAX(id) FROM imports").fetchone()[0]
    with pytest.raises(ValueError, match="no raw evidence"):
        reparse.reparse_import(conn, clk, settings, vid)


def test_youtube_route_is_injectable_so_tests_never_build_a_real_client(conn, clk, settings):
    conn.execute("INSERT INTO imports (kind, received_at, parser_version, status) "
                 "VALUES ('youtube_history', '2026-09-20T10:00:00+00:00', 'youtube-2', 'ok')")
    vid = conn.execute("SELECT MAX(id) FROM imports").fetchone()[0]
    seen = []
    reparse.reparse_import(conn, clk, settings, vid,
                           dispatch=lambda kind, s: (lambda *a: seen.append(kind) or "outcome"))
    assert seen == ["youtube_history"]
