"""The phone browser broken into sites (DR5.13): reading, dwell, apportionment, privacy.

The privacy test is the important one. It seeds the fake history with paths and
query strings nobody would write by accident and asserts that not one of them
survives into the stored evidence or the database.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import dr_brainrot_types as T  # noqa: E402
import evidence  # noqa: E402
import labels  # noqa: E402
import report  # noqa: E402
from sources import brave_history  # noqa: E402
from tests.test_allowance import make_settings, measure, open_db  # noqa: E402

TZ = "Europe/Warsaw"
WEEK = "2026-09-13T16:00:00+00:00"          # allowance week opening Sun 13 Sep 18:00
PHONE = "phoneGUID=="                        # a non-empty originator: not this Mac
_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

#: Paths and queries that must never leave the browser's own file.
SECRET_PATHS = {
    "query": "?q=zornblatt+embarrassing+symptom",
    "path": "/r/kwarglepuff/comments/hidden-thing",
    "token": "?session=BLORPTANGLE-PRIVATE-TOKEN",
}


def _micros(when: datetime) -> int:
    return int((when - _EPOCH).total_seconds() * 1_000_000)


def history_db(path: pathlib.Path, rows: list[tuple[str, datetime, str]]) -> pathlib.Path:
    """A minimal Chromium history file. `rows` are (url, instant, originator)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER,
                             visit_duration INTEGER DEFAULT 0, originator_cache_guid TEXT);
        """
    )
    for i, (url, when, origin) in enumerate(rows, start=1):
        conn.execute("INSERT INTO urls (id, url) VALUES (?, ?)", (i, url))
        conn.execute(
            "INSERT INTO visits (url, visit_time, visit_duration, originator_cache_guid) "
            "VALUES (?, ?, 0, ?)", (i, _micros(when), origin))
    conn.commit()
    conn.close()
    return path


def at(day: str, hh: int, mm: int = 0) -> datetime:
    """A local instant on a day, as UTC."""
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").replace(
        tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc)


@pytest.fixture
def clk():
    import clock
    return clock.FrozenClock("2026-09-23T08:00:00+00:00")     # the Wednesday after the week


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, db_path=tmp_path / "dr-brainrot.db", tz=TZ, go_live="2026-09-13",
                         weekly_allowance_min=360)


@pytest.fixture
def conn(settings):
    c = open_db(settings)
    yield c
    c.close()


# ==========================================================================
# read_phone_visits
# ==========================================================================

def test_only_visits_from_another_device_are_read(tmp_path):
    db = history_db(tmp_path / "History", [
        ("https://www.reddit.com/r/x", at("2026-09-15", 9), PHONE),
        ("https://news.example.com/a", at("2026-09-15", 10), ""),        # made on this Mac
        ("https://news.example.com/b", at("2026-09-15", 11), None),      # older rows: NULL
        ("https://x.com/home", at("2026-09-16", 8), PHONE),
    ])
    got = brave_history.read_phone_visits(db, first_day=date(2026, 9, 13),
                                          last_day=date(2026, 9, 19), tz=TZ)
    assert [(d.isoformat(), h) for d, h, _ in got] == [
        ("2026-09-15", "www.reddit.com"), ("2026-09-16", "x.com")]


def test_visits_outside_the_window_are_left_out(tmp_path):
    db = history_db(tmp_path / "History", [
        ("https://a.example/1", at("2026-09-12", 23), PHONE),            # the day before
        ("https://b.example/2", at("2026-09-13", 0, 5), PHONE),          # just inside
        ("https://c.example/3", at("2026-09-20", 0, 5), PHONE),          # just outside
    ])
    got = brave_history.read_phone_visits(db, first_day=date(2026, 9, 13),
                                          last_day=date(2026, 9, 19), tz=TZ)
    assert [h for _, h, _ in got] == ["b.example"]


def test_a_history_without_the_originator_column_is_refused(tmp_path):
    db = tmp_path / "History"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT);"
                    "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);")
    c.commit()
    c.close()
    with pytest.raises(brave_history.BraveHistoryError, match="originator_cache_guid"):
        brave_history.read_phone_visits(db, first_day=date(2026, 9, 13),
                                        last_day=date(2026, 9, 19), tz=TZ)


def test_a_missing_history_is_refused_by_name(tmp_path):
    with pytest.raises(brave_history.BraveHistoryError, match="was not found"):
        brave_history.read_phone_visits(tmp_path / "nope", first_day=date(2026, 9, 13),
                                        last_day=date(2026, 9, 19), tz=TZ)


def test_reading_never_writes_to_the_browsers_own_file(tmp_path):
    db = history_db(tmp_path / "History", [("https://a.example/1", at("2026-09-15", 9), PHONE)])
    before = (db.stat().st_mtime_ns, db.read_bytes())
    brave_history.read_phone_visits(db, first_day=date(2026, 9, 13),
                                    last_day=date(2026, 9, 19), tz=TZ)
    assert (db.stat().st_mtime_ns, db.read_bytes()) == before


# ==========================================================================
# dwell and apportionment
# ==========================================================================

def _visits(*pairs):
    return [(at(day, hh, mm).astimezone().date(), host, at(day, hh, mm))
            for day, hh, mm, host in pairs]


def test_dwell_credits_each_visit_with_the_gap_to_the_next():
    v = _visits(("2026-09-15", 9, 0, "slow.example"),     # 9:00 -> 9:04 = 4 min
                ("2026-09-15", 9, 4, "fast.example"),     # 9:04 -> 9:05 = 1 min
                ("2026-09-15", 9, 5, "last.example"))
    w = brave_history.dwell_weights(v)
    by_host = {host: secs for (_, host), secs in w.items()}
    assert by_host["slow.example"] == pytest.approx(240)
    assert by_host["fast.example"] == pytest.approx(60)
    # The final visit has no successor and takes the median of the others.
    assert by_host["last.example"] == pytest.approx(150)


def test_a_long_gap_is_capped_so_an_idle_phone_cannot_swallow_the_week():
    v = _visits(("2026-09-15", 9, 0, "left.example"),     # eight hours later
                ("2026-09-15", 17, 0, "next.example"))
    w = brave_history.dwell_weights(v)
    by_host = {host: secs for (_, host), secs in w.items()}
    assert by_host["left.example"] == pytest.approx(brave_history.IDLE_CAP_MIN * 60)


def test_apportionment_preserves_the_screen_time_total_exactly():
    v = _visits(("2026-09-15", 9, 0, "big.example"),      # 5 min
                ("2026-09-15", 9, 5, "small.example"),    # 1 min
                ("2026-09-15", 9, 6, "big.example"))      # median of the two = 3 min
    shares = brave_history.apportion(brave_history.dwell_weights(v), 163.0)
    assert sum(shares.values()) == pytest.approx(163.0)
    assert shares["big.example"] > shares["small.example"]


def test_identical_instants_split_evenly_rather_than_dividing_by_zero():
    v = _visits(("2026-09-15", 9, 0, "a.example"), ("2026-09-15", 9, 0, "b.example"))
    weights = {k: 0.0 for k in brave_history.dwell_weights(v)}
    assert brave_history.apportion(weights, 10.0) == {"a.example": 5.0, "b.example": 5.0}


def test_no_visits_apportions_nothing():
    assert brave_history.apportion({}, 163.0) == {}
    assert brave_history.dwell_weights([]) == {}


# ==========================================================================
# sync: the whole path, and privacy
# ==========================================================================

def _browser_block(conn, clk, settings, *, minutes=163.0, start="2026-09-13", end="2026-09-19"):
    """Screen Time's one opaque figure for the phone browser."""
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Brave", day=start,
            period_end=end, minutes=minutes, device=T.DEVICE_IPHONE,
            provenance=T.PROV_SCREENSHOT, display="Brave")


def _configured(settings, db):
    return config.test_settings(**{**settings.__dict__, "brave_history_db": db})


def test_sync_splits_the_block_across_the_sites_inside_it(conn, clk, settings, tmp_path):
    _browser_block(conn, clk, settings)
    db = history_db(tmp_path / "History", [
        (f"https://www.reddit.com{SECRET_PATHS['path']}", at("2026-09-15", 9, 0), PHONE),
        (f"https://x.com/search{SECRET_PATHS['query']}", at("2026-09-15", 9, 5), PHONE),
        (f"https://www.reddit.com/r/two{SECRET_PATHS['token']}", at("2026-09-15", 9, 6), PHONE),
    ])
    outcome = brave_history.sync(conn, clk, _configured(settings, db))

    assert outcome.status == "ok" and outcome.measurements_written == 2
    rows = conn.execute(
        "SELECT s.source_key, s.kind, m.device, m.provenance, m.minutes, m.period_start, m.period_end "
        "FROM measurements m JOIN sources s ON s.id = m.source_id "
        "WHERE m.provenance = ? ORDER BY m.minutes DESC", (T.PROV_BROWSER_SYNC,)).fetchall()
    assert [r["source_key"] for r in rows] == ["reddit.com", "x"]        # x.com folds into X
    assert all(r["device"] == T.DEVICE_IPHONE for r in rows)
    assert all((r["period_start"], r["period_end"]) == ("2026-09-13", "2026-09-19") for r in rows)
    # Screen Time's total is preserved: the shares are the estimate, not the sum.
    assert sum(r["minutes"] for r in rows) == pytest.approx(163.0, abs=0.02)


def test_no_url_path_or_query_survives_into_evidence_or_the_database(conn, clk, settings, tmp_path):
    _browser_block(conn, clk, settings)
    db = history_db(tmp_path / "History", [
        (f"https://www.reddit.com{SECRET_PATHS['path']}", at("2026-09-15", 9, 0), PHONE),
        (f"https://x.com/search{SECRET_PATHS['query']}", at("2026-09-15", 9, 5), PHONE),
        (f"https://bank.example/pay{SECRET_PATHS['token']}", at("2026-09-15", 9, 6), PHONE),
    ])
    brave_history.sync(conn, clk, _configured(settings, db))

    stored = "\n".join(
        str(r[0]) for r in conn.execute(
            "SELECT source_key FROM sources UNION ALL SELECT display_name FROM sources "
            "UNION ALL SELECT detail FROM measurements UNION ALL SELECT raw_path FROM imports "
            "UNION ALL SELECT COALESCE(warnings,'') FROM imports"))
    raw = "\n".join(p.read_text() for p in pathlib.Path(settings.raw_dir).rglob("*") if p.is_file())
    for secret in SECRET_PATHS.values():
        assert secret not in stored, f"{secret} reached the database"
        assert secret not in raw, f"{secret} reached the stored evidence"
    assert "reddit.com" in raw                       # the hostname is kept, and only that


def test_a_period_with_no_synced_visits_is_warned_about_not_silently_dropped(
        conn, clk, settings, tmp_path):
    _browser_block(conn, clk, settings, minutes=163.0)
    db = history_db(tmp_path / "History", [
        ("https://late.example/1", at("2026-09-25", 9), PHONE),          # a later week
    ])
    outcome = brave_history.sync(conn, clk, _configured(settings, db))
    assert outcome.status == "partial" and outcome.measurements_written == 0
    assert any("stayed unattributed" in w and "163 min" in w for w in outcome.warnings)


def test_sync_is_a_no_op_without_a_configured_history(conn, clk, settings):
    _browser_block(conn, clk, settings)
    assert brave_history.sync(conn, clk, settings) is None


def test_sync_is_a_no_op_before_any_browser_block_exists(conn, clk, settings, tmp_path):
    db = history_db(tmp_path / "History", [("https://a.example/1", at("2026-09-15", 9), PHONE)])
    assert brave_history.sync(conn, clk, _configured(settings, db)) is None


# ==========================================================================
# the week does not double-count
# ==========================================================================

def test_the_block_is_excluded_so_its_minutes_are_not_counted_twice(conn, clk, settings, tmp_path):
    _browser_block(conn, clk, settings, minutes=100.0)
    db = history_db(tmp_path / "History", [
        ("https://www.reddit.com/a", at("2026-09-15", 9, 0), PHONE),
        ("https://www.reddit.com/b", at("2026-09-15", 9, 5), PHONE),
    ])
    brave_history.sync(conn, clk, _configured(settings, db))

    import allowance
    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    # The 100 minutes are counted once, through the site, and the block itself
    # is reported as covered elsewhere rather than dropped.
    assert pooled.charged_min == pytest.approx(100.0)
    assert pooled.by_group[T.GROUP_IPHONE].covered_excluded_min == pytest.approx(100.0)
    assert T.COVERED_ELSEWHERE[(T.KIND_IPHONE_APP, "brave")] == "phone browser history (per site)"


def test_the_sites_land_in_the_phone_group_not_desktop_web(conn, clk, settings, tmp_path):
    _browser_block(conn, clk, settings, minutes=50.0)
    db = history_db(tmp_path / "History", [
        ("https://www.reddit.com/a", at("2026-09-15", 9, 0), PHONE),
        ("https://www.reddit.com/b", at("2026-09-15", 9, 5), PHONE),
    ])
    brave_history.sync(conn, clk, _configured(settings, db))
    named = {r.display_name: r for r in report.top_sources(conn, clk, settings, WEEK, limit=None)}
    assert named["www.reddit.com"].group == T.GROUP_IPHONE
    assert named["www.reddit.com"].minutes == pytest.approx(50.0)


def test_an_unattributed_block_is_named_in_blind_spots(conn, clk, settings, tmp_path):
    """Excluded minutes with nothing in their place must not leave the week silently."""
    _browser_block(conn, clk, settings, minutes=163.0)
    spots = report.blind_spots(conn, clk, settings)
    assert any("163 min for 2026-09-13 to 2026-09-19" in s and "not attributed" in s for s in spots)

    db = history_db(tmp_path / "History", [
        ("https://www.reddit.com/a", at("2026-09-15", 9, 0), PHONE),
        ("https://www.reddit.com/b", at("2026-09-15", 9, 5), PHONE),
    ])
    brave_history.sync(conn, clk, _configured(settings, db))
    assert not any("not attributed" in s for s in report.blind_spots(conn, clk, settings))


def test_the_new_provenance_is_described_rather_than_shown_as_a_slug():
    """Every provenance the page can render needs words, or the badge shows the slug."""
    assert T.PROV_BROWSER_SYNC in T.PROVENANCES
    assert set(T.PROVENANCE_TEXT) == set(T.PROVENANCES)
    text = T.PROVENANCE_TEXT[T.PROV_BROWSER_SYNC]
    assert "measured" in text and "estimated" in text     # names which half is which


def test_a_rerun_replaces_the_period_rather_than_accumulating(conn, clk, settings, tmp_path):
    """A site that stops appearing, or folds to a different key, must not linger."""
    _browser_block(conn, clk, settings, minutes=100.0)
    first = history_db(tmp_path / "H1", [
        ("https://gone.example/a", at("2026-09-15", 9, 0), PHONE),
        ("https://gone.example/b", at("2026-09-15", 9, 5), PHONE),
    ])
    brave_history.sync(conn, clk, _configured(settings, first))
    assert {r[0] for r in conn.execute(
        "SELECT s.source_key FROM measurements m JOIN sources s ON s.id = m.source_id "
        "WHERE m.provenance = ?", (T.PROV_BROWSER_SYNC,))} == {"gone.example"}

    second = history_db(tmp_path / "H2", [
        ("https://stayed.example/a", at("2026-09-16", 9, 0), PHONE),
        ("https://stayed.example/b", at("2026-09-16", 9, 5), PHONE),
    ])
    brave_history.sync(conn, clk, _configured(settings, second))
    rows = conn.execute(
        "SELECT s.source_key, m.minutes FROM measurements m JOIN sources s ON s.id = m.source_id "
        "WHERE m.provenance = ?", (T.PROV_BROWSER_SYNC,)).fetchall()
    assert {r["source_key"] for r in rows} == {"stayed.example"}       # not both
    assert sum(r["minutes"] for r in rows) == pytest.approx(100.0)


def test_the_mobile_front_door_is_the_same_site(conn, clk, settings, tmp_path):
    """m.youtube.com is YouTube, so it inherits YouTube's covered-elsewhere rule."""
    assert labels.normalise_key(T.KIND_WEB_DOMAIN, "m.youtube.com") == (T.KIND_WEB_DOMAIN, "youtube.com")
    assert labels.normalise_key(T.KIND_WEB_DOMAIN, "m.reddit.com") == (T.KIND_WEB_DOMAIN, "reddit.com")
    # Only the exact prefix: a site that merely starts with m is left alone.
    assert labels.normalise_key(T.KIND_WEB_DOMAIN, "maps.google.com") == (T.KIND_WEB_DOMAIN, "maps.google.com")

    _browser_block(conn, clk, settings, minutes=60.0)
    db = history_db(tmp_path / "History", [
        ("https://m.youtube.com/watch", at("2026-09-15", 9, 0), PHONE),
        ("https://news.example/a", at("2026-09-15", 9, 5), PHONE),
    ])
    brave_history.sync(conn, clk, _configured(settings, db))
    import allowance
    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    # The YouTube half is excluded as covered by account-level history, not
    # counted a second time here.
    assert pooled.by_group[T.GROUP_IPHONE].covered_excluded_min > 0


def test_phone_sites_name_their_import_rather_than_claiming_to_be_polled(
        conn, clk, settings, tmp_path):
    """Without the mapping the row reads "polled via API", which it is not."""
    assert report.IMPORT_KIND_FOR_PROVENANCE[T.PROV_BROWSER_SYNC] == "brave_sync"
    _browser_block(conn, clk, settings, minutes=40.0)
    db = history_db(tmp_path / "History", [
        ("https://a.example/1", at("2026-09-15", 9, 0), PHONE),
        ("https://a.example/2", at("2026-09-15", 9, 5), PHONE),
    ])
    out = brave_history.sync(conn, clk, _configured(settings, db))
    row = next(t for t in report.weekly_totals(conn, clk, settings, WEEK)
               if t.provenance == T.PROV_BROWSER_SYNC)
    assert row.import_status is not None and row.import_id == out.import_id
