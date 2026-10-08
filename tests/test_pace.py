"""This week so far against the same point last week (DR5.29).

Finished days only, the same weekdays in both weeks, and only on channels both
spans actually read. The regression worth naming: the first draft looked up
covered-elsewhere by exact key and counted Qbserve's YouTube pseudo-app, so
95 minutes of YouTube in the browser on 6 Oct read as a 208% jump.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import allowance  # noqa: E402
import clock  # noqa: E402
import drbr_types as T  # noqa: E402
import evidence  # noqa: E402
import labels  # noqa: E402
import pace  # noqa: E402
from tests.test_allowance import label_source, make_settings, open_db  # noqa: E402

TZ = "Europe/Warsaw"
WED = "2026-10-07T08:00:00+00:00"           # Wednesday: Sun, Mon and Tue have finished
SITE, APP = T.KIND_WEB_DOMAIN, T.KIND_DESKTOP_APP


@pytest.fixture
def clk():
    return clock.FrozenClock(WED)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, db_path=tmp_path / "drbr.db", tz=TZ, go_live="2026-09-13")


@pytest.fixture
def conn(settings):
    c = open_db(settings)
    evidence.ensure_diagnostic_start(c, "2026-09-27")
    yield c
    c.close()


def read(conn, clk, settings, kind, provenance, device, first, last, rows):
    """One import covering first..last, with per-day rows {(day, key): minutes}."""
    rec = evidence.begin_import(conn, clk, kind=kind, parser_version="t")
    for (day, key), minutes in rows.items():
        src_kind = APP if provenance == T.PROV_APP_TRACKER else (
            T.KIND_YOUTUBE_CHANNEL if provenance == T.PROV_PAGE else SITE)
        src = labels.ensure_source(conn, clk, src_kind, key, key)
        evidence.upsert_measurement(conn, rec, source_id=src.id, device=device,
                                    provenance=provenance, period_start=day, period_end=day,
                                    minutes=minutes, status="ok" if minutes is not None else "unreadable")
    evidence.finish_import(conn, clk, settings, rec, period_start=first, period_end=last)


def mac_web(conn, clk, settings, first, last, rows):
    read(conn, clk, settings, "coldturkey", T.PROV_EXPORT, T.DEVICE_DESKTOP, first, last, rows)


def mac_apps(conn, clk, settings, first, last, rows):
    read(conn, clk, settings, "qbserve", T.PROV_APP_TRACKER, T.DEVICE_DESKTOP, first, last, rows)


def both_weeks_read(conn, clk, settings, *, last=None, this=None):
    """Cold Turkey and Qbserve read every day of both spans."""
    mac_web(conn, clk, settings, "2026-09-27", "2026-10-06", {**(last or {}), **(this or {})})
    mac_apps(conn, clk, settings, "2026-09-27", "2026-10-06", {})


# ==========================================================================

def test_it_compares_the_finished_days_with_the_same_weekdays_last_week(conn, clk, settings):
    both_weeks_read(conn, clk, settings,
                    last={("2026-09-27", "reddit.com"): 30, ("2026-09-29", "reddit.com"): 30,
                          ("2026-09-30", "reddit.com"): 500},      # Wednesday: not yet comparable
                    this={("2026-10-04", "reddit.com"): 10, ("2026-10-06", "reddit.com"): 20})
    label_source(conn, clk, SITE, "reddit.com", T.WASTEFUL)

    p = pace.pace(conn, clk, settings)
    assert (p.first_day, p.last_day, p.days) == ("2026-10-04", "2026-10-06", 3)
    assert (p.this_week_min, p.last_week_min) == (30, 60)      # last Wednesday's 500 is not in it
    assert p.direction == "down"
    assert p.text == "So far this week (Sun–Tue): 30 min, against 60 min by Tuesday last week — down 50%."


def test_nothing_on_a_sunday(conn, settings):
    assert pace.pace(conn, clock.FrozenClock("2026-10-04T10:00:00+00:00"), settings) is None


def test_a_channel_that_missed_a_day_is_left_out_not_read_as_zero(conn, clk, settings):
    mac_web(conn, clk, settings, "2026-09-27", "2026-10-06",
            {("2026-09-27", "reddit.com"): 40, ("2026-10-04", "reddit.com"): 40})
    # Qbserve read last week but not this week at all.
    mac_apps(conn, clk, settings, "2026-09-27", "2026-09-29", {("2026-09-28", "Game"): 90})
    label_source(conn, clk, SITE, "reddit.com", T.WASTEFUL)
    label_source(conn, clk, APP, "Game", T.WASTEFUL)

    p = pace.pace(conn, clk, settings)
    assert "Mac apps, a day wasn't read" in p.left_out
    assert (p.this_week_min, p.last_week_min) == (40, 40)        # the game's 90 is not a fall


def test_qbserves_youtube_pseudo_app_is_not_counted_twice(conn, clk, settings):
    """The regression: YouTube in the browser is YouTube history's to count."""
    both_weeks_read(conn, clk, settings)
    mac_apps(conn, clk, settings, "2026-09-27", "2026-10-06",
             {("2026-10-06", "com.qotoqot.qbserve.youtube"): 95})
    label_source(conn, clk, APP, "com.qotoqot.qbserve.youtube", T.WASTEFUL)
    assert pace.pace(conn, clk, settings).this_week_min == 0


def test_youtube_counts_only_once_it_has_been_synced_since_the_span(conn, clk, settings):
    both_weeks_read(conn, clk, settings)
    clk.set("2026-10-05T08:00:00+00:00")                          # synced Monday morning
    read(conn, clk, settings, "youtube_history", T.PROV_PAGE, T.DEVICE_ACCOUNT, "2026-09-27",
         "2026-10-05", {("2026-10-04", "UCchan"): 25})
    clk.set(WED)
    p = pace.pace(conn, clk, settings)
    assert any(x.startswith("YouTube history, not synced") and "run the extension" in x
               for x in p.left_out)

    read(conn, clk, settings, "youtube_history", T.PROV_PAGE, T.DEVICE_ACCOUNT, "2026-09-27",
         "2026-10-06", {("2026-10-04", "UCchan"): 25, ("2026-09-27", "UCchan"): 10})
    p = pace.pace(conn, clk, settings)
    assert "YouTube history" in p.counted
    assert (p.this_week_min, p.last_week_min) == (25, 10)        # undecided still counts


def test_an_unreadable_reading_takes_its_channel_out(conn, clk, settings):
    both_weeks_read(conn, clk, settings, this={("2026-10-05", "mystery.example"): None})
    p = pace.pace(conn, clk, settings)
    assert "the Mac's browser, a reading couldn't be measured" in p.left_out


def test_weekly_channels_are_named_as_joining_later(conn, clk, settings):
    both_weeks_read(conn, clk, settings)
    p = pace.pace(conn, clk, settings)
    assert p.joins_later == ("Screen Time", "the phone browser by site")
    assert p.basis.endswith("Screen Time and the phone browser by site join once the week is in.")


@pytest.mark.parametrize("this,last,direction,tail", [
    (40, 30, "level", "level with 30 min by Tuesday last week."),
    (90, 30, "up", "against 30 min by Tuesday last week — up 200%."),
    (20, 0, "up", "against nothing by Tuesday last week."),
])
def test_the_sentence_reads_honestly_either_way(conn, clk, settings, this, last, direction, tail):
    both_weeks_read(conn, clk, settings,
                    last={("2026-09-27", "reddit.com"): last} if last else {},
                    this={("2026-10-04", "reddit.com"): this})
    label_source(conn, clk, SITE, "reddit.com", T.WASTEFUL)
    p = pace.pace(conn, clk, settings)
    assert p.direction == direction and p.text.endswith(tail)


def test_a_single_finished_day_names_just_that_day(conn, settings):
    monday = clock.FrozenClock("2026-10-05T08:00:00+00:00")
    mac_web(conn, monday, settings, "2026-09-27", "2026-10-04",
            {("2026-10-04", "reddit.com"): 10})
    label_source(conn, monday, SITE, "reddit.com", T.WASTEFUL)
    assert pace.pace(conn, monday, settings).text.startswith("So far this week (Sun): 10 min")


def test_its_daily_sums_agree_with_the_weeks_own_figure(conn, settings):
    """Same rules as pooled_minutes, read per day: on a Saturday, Sun-Fri is the week."""
    saturday = clock.FrozenClock("2026-10-10T08:00:00+00:00")
    rows = {(f"2026-10-0{d}", host): m for d, host, m in
            [(4, "reddit.com", 12), (6, "x.com", 30), (8, "youtube.com", 50), (9, "news.example", 7)]}
    mac_web(conn, saturday, settings, "2026-09-27", "2026-10-09", rows)
    mac_apps(conn, saturday, settings, "2026-09-27", "2026-10-09", {})
    label_source(conn, saturday, SITE, "reddit.com", T.WASTEFUL)
    label_source(conn, saturday, SITE, "news.example", T.TOOL)

    p = pace.pace(conn, saturday, settings)
    week = T.allowance_week_start_for_day(T.parse_date("2026-10-04"), TZ)
    pooled = allowance.pooled_minutes(conn, settings, week)
    assert p.this_week_min == pytest.approx(pooled.charged_by_provenance.get(T.PROV_EXPORT, 0.0))
    assert p.this_week_min == pytest.approx(12 + 30)   # youtube.com covered elsewhere, news a tool
