"""What got better, and what to try next (DR5.17).

The first three tests are the reason this module is built the way it is. Each
is a way a week-on-week comparison lies, and each was live in Jacob's real
data on the day this was written: an unreadable Facebook reading that a naive
comparison called "rose from nothing" when it had fallen by ninety minutes; a
Screen Time list cut off before its end; and a channel only one week had.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import clock  # noqa: E402
import drbr_types as T  # noqa: E402
import evidence  # noqa: E402
import insights  # noqa: E402
import labels  # noqa: E402
import progress  # noqa: E402
import report  # noqa: E402
from tests.test_allowance import label_source, make_settings, open_db  # noqa: E402

TZ = "Europe/Warsaw"
A = ("2026-09-13", "2026-09-19")            # the week before
B = ("2026-09-20", "2026-09-26")            # the week described
NOW = "2026-09-30T08:00:00+00:00"           # Wednesday after B: B has been asked for


@pytest.fixture
def clk():
    return clock.FrozenClock(NOW)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, db_path=tmp_path / "drbr.db", tz=TZ, go_live="2026-09-13",
                         weekly_allowance_min=360)


@pytest.fixture
def conn(settings):
    c = open_db(settings)
    evidence.ensure_diagnostic_start(c, A[0])
    yield c
    c.close()


# --- evidence builders ---------------------------------------------------------

def screen_time(conn, clk, settings, week, apps, *, complete=True, unreadable=()):
    """One Screen Time import for a week, with its Most Used list flagged."""
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="t")
    for name, minutes in apps.items():
        src = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, name, name)
        evidence.upsert_measurement(conn, rec, source_id=src.id, device=T.DEVICE_IPHONE,
                                    provenance=T.PROV_SCREENSHOT, period_start=week[0],
                                    period_end=week[1], minutes=minutes)
    for name in unreadable:
        src = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, name, name)
        evidence.upsert_measurement(conn, rec, source_id=src.id, device=T.DEVICE_IPHONE,
                                    provenance=T.PROV_SCREENSHOT, period_start=week[0],
                                    period_end=week[1], minutes=None, status="unreadable")
    rec.meta["most_used_complete"] = complete
    evidence.finish_import(conn, clk, settings, rec, period_start=week[0], period_end=week[1])


def web(conn, clk, settings, day, sites):
    """One Cold Turkey day. Cold Turkey lists every domain, so absence is a real zero."""
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t")
    for host, minutes in sites.items():
        src = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, host, host)
        evidence.upsert_measurement(conn, rec, source_id=src.id, device=T.DEVICE_DESKTOP,
                                    provenance=T.PROV_EXPORT, period_start=day, period_end=day,
                                    minutes=minutes)
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=day)


def wasteful(conn, clk, *pairs):
    for kind, key in pairs:
        label_source(conn, clk, kind, key, T.WASTEFUL)


APP, SITE = T.KIND_IPHONE_APP, T.KIND_WEB_DOMAIN


# ==========================================================================
# The three ways a comparison lies
# ==========================================================================

def test_an_unreadable_reading_is_unknown_never_nothing(conn, clk, settings):
    """The regression. Facebook was unreadable the week before; it did not rise from nothing."""
    screen_time(conn, clk, settings, A, {"Instagram": 40}, unreadable=("Facebook",))
    screen_time(conn, clk, settings, B, {"Instagram": 40, "Facebook": 56})
    wasteful(conn, clk, (APP, "Instagram"), (APP, "Facebook"))

    p = progress.progress(conn, clk, settings)
    assert not any("Facebook" in f.text and "rose" in f.text for f in p.tips)
    assert not any("Facebook" in f.text for f in p.wins)
    assert "Facebook — couldn't be read the week before" in p.set_aside
    assert p.unreadable_sources == 1
    # Its 56 minutes are out of the comparison, not counted as a rise.
    assert (p.before_min, p.after_min) == (40, 40) and p.direction == "level"


def test_an_app_missing_from_a_cut_off_list_is_unknown(conn, clk, settings):
    screen_time(conn, clk, settings, A, {"Instagram": 80}, complete=False)
    screen_time(conn, clk, settings, B, {"Instagram": 80, "Facebook": 56})
    wasteful(conn, clk, (APP, "Instagram"), (APP, "Facebook"))

    p = progress.progress(conn, clk, settings)
    assert not any(f.kind == "riser" for f in p.tips)
    assert p.below_cut_sources == 1
    assert "1 phone app below the end of a cut-off Screen Time list" in p.set_aside


def test_an_app_missing_from_a_complete_list_is_a_real_zero(conn, clk, settings):
    screen_time(conn, clk, settings, A, {"Instagram": 80})
    screen_time(conn, clk, settings, B, {"Instagram": 80, "Facebook": 56})
    wasteful(conn, clk, (APP, "Instagram"), (APP, "Facebook"))

    p = progress.progress(conn, clk, settings)
    assert p.like_for_like
    (riser,) = [f for f in p.tips if f.kind == "riser"]
    assert riser.text == ("Facebook rose from nothing to 56 min. "
                          "A Screen Time app limit on the phone is the direct lever.")


def test_a_channel_only_one_week_had_is_set_aside_and_named(conn, clk, settings):
    screen_time(conn, clk, settings, A, {"Instagram": 80})
    screen_time(conn, clk, settings, B, {"Instagram": 60})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 200})      # only B had Cold Turkey
    wasteful(conn, clk, (APP, "Instagram"), (SITE, "reddit.com"))

    p = progress.progress(conn, clk, settings)
    assert "the Mac's browser — only this week had it" in p.set_aside
    assert p.channels_set_aside == ("the Mac's browser",)
    # reddit's 200 minutes are not a rise: nothing measured it the week before.
    assert (p.before_min, p.after_min) == (80, 60)
    assert p.direction == "down"
    assert p.verdict == "Down 25% on the week before, like for like: 80 min to 60 min."


# ==========================================================================
# Wins
# ==========================================================================

def test_a_source_that_halves_is_a_named_win(conn, clk, settings):
    web(conn, clk, settings, "2026-09-15", {"reddit.com": 60, "news.example": 10})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 20, "news.example": 10})
    wasteful(conn, clk, (SITE, "reddit.com"))

    p = progress.progress(conn, clk, settings)
    assert Finding("source_down", "reddit.com fell from 60 min to 20 min.") in _plain(p.wins)
    assert p.like_for_like and p.direction == "down"


def test_a_source_that_vanished_fell_to_nothing(conn, clk, settings):
    web(conn, clk, settings, "2026-09-15", {"reddit.com": 30, "news.example": 10})
    web(conn, clk, settings, "2026-09-22", {"news.example": 10})
    wasteful(conn, clk, (SITE, "reddit.com"))

    p = progress.progress(conn, clk, settings)
    assert Finding("source_down", "reddit.com fell from 30 min to nothing.") in _plain(p.wins)


@pytest.mark.parametrize("before,after", [(60, 40), (20, 8)])
def test_a_small_drop_is_not_claimed(conn, clk, settings, before, after):
    """Halving is the bar, and fifteen minutes: 60 -> 40 is not half, 20 -> 8 is too small."""
    web(conn, clk, settings, "2026-09-15", {"reddit.com": before})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": after})
    wasteful(conn, clk, (SITE, "reddit.com"))
    assert not [w for w in progress.progress(conn, clk, settings).wins if w.kind == "source_down"]


def test_never_more_than_three_wins_or_tips(conn, clk, settings):
    sites = {f"s{i}.example": 100 for i in range(8)}
    web(conn, clk, settings, "2026-09-15", sites)
    web(conn, clk, settings, "2026-09-22", {"keep.example": 1})
    wasteful(conn, clk, *((SITE, h) for h in sites))
    p = progress.progress(conn, clk, settings)
    assert len(p.wins) <= progress.MAX_WINS == 3 and len(p.tips) <= progress.MAX_TIPS == 3


# ==========================================================================
# Tips
# ==========================================================================

def _line(group, provenances=(), name="Thing"):
    return report.SourceLine(1, name, group, 50.0, T.WASTEFUL, 50.0, frozenset(provenances))


@pytest.mark.parametrize("line,expected", [
    (_line(T.GROUP_X), "a Cold Turkey block on x.com"),
    (_line(T.GROUP_YOUTUBE), "Capturing it to the queue"),
    (_line(T.GROUP_PODCASTS), "Capturing it to the queue"),
    (_line(T.GROUP_IPHONE, {T.PROV_BROWSER_SYNC}), "a Screen Time limit on Brave itself"),
    (_line(T.GROUP_IPHONE, {T.PROV_SCREENSHOT}), "A Screen Time app limit on the phone"),
    (_line(T.GROUP_DESKTOP), "A Cold Turkey block on this Mac"),
    (_line(T.GROUP_DESKTOP_APPS), "Blocking the app on this Mac"),
])
def test_each_tip_names_the_control_that_exists_for_where_it_happened(line, expected):
    assert expected in progress._lever(line)


def test_the_lever_tip_only_ever_addresses_something_labelled_wasteful(conn, clk, settings):
    web(conn, clk, settings, "2026-09-15", {"news.example": 5})
    web(conn, clk, settings, "2026-09-22", {"undecided.example": 100, "reddit.com": 30})
    wasteful(conn, clk, (SITE, "reddit.com"))

    (lever,) = [f for f in progress.progress(conn, clk, settings).tips if f.kind == "lever"]
    assert lever.text.startswith("reddit.com was your biggest source of brain rot: 30 min")
    assert "undecided.example" not in lever.text


@pytest.mark.parametrize("complete,effect", [
    (True, "Halving just those would have brought the week inside the allowance."),
    (False, "Halving just those would have taken 45 min off a week that was 30 min over."),
])
def test_inside_the_allowance_is_only_promised_when_the_week_is_fully_known(
        conn, clk, tmp_path, complete, effect):
    tight = make_settings(tmp_path, db_path=tmp_path / "drbr.db", tz=TZ, go_live="2026-09-13",
                          weekly_allowance_min=60)
    screen_time(conn, clk, tight, A, {"a": 1})
    screen_time(conn, clk, tight, B, {"a": 40, "b": 30, "c": 20}, complete=complete)
    wasteful(conn, clk, (APP, "a"), (APP, "b"), (APP, "c"))

    (tip,) = [f for f in progress.progress(conn, clk, tight).tips if f.kind == "concentration"]
    assert tip.text == f"a, b and c were 100% of your wasteful time. {effect}"


# ==========================================================================
# Which week, and the first one
# ==========================================================================

def test_the_first_week_has_tips_but_nothing_to_compare(conn, clk, tmp_path):
    late = make_settings(tmp_path, db_path=tmp_path / "late.db", tz=TZ, go_live="2026-09-20",
                         weekly_allowance_min=360)
    c = open_db(late)
    evidence.ensure_diagnostic_start(c, B[0])
    web(c, clk, late, "2026-09-22", {"reddit.com": 30})
    label_source(c, clk, SITE, "reddit.com", T.WASTEFUL)

    p = progress.progress(c, clk, late)
    assert p.previous_start is None and p.direction is None and p.wins == ()
    assert "first week measured" in p.verdict
    assert [f.kind for f in p.tips] == ["lever"]
    c.close()


def test_on_a_sunday_it_describes_the_week_already_asked_for(settings):
    """Sunday 4 Oct: 27 Sep - 3 Oct has ended, but its Screen Time is not due until Monday."""
    sunday = clock.FrozenClock("2026-10-04T10:00:00+00:00")
    assert progress._week_described(sunday, settings).start == B[0]
    monday = clock.FrozenClock("2026-10-05T10:00:00+00:00")
    assert progress._week_described(monday, settings).start == "2026-09-27"


def test_minutes_print_exactly_as_the_page_prints_them():
    from dashboard import app as dashboard_app
    for value in (0, 0.4, 9.94, 10, 56.2, 1437.6):
        assert progress._m(value) == dashboard_app.fmt_min(value)


# ==========================================================================
# What crosses to the summary model
# ==========================================================================

def test_the_comparison_reaches_the_model_without_a_single_name(conn, clk, settings):
    screen_time(conn, clk, settings, A, {"Instagram": 40}, unreadable=("Facebook",))
    screen_time(conn, clk, settings, B, {"Instagram": 40, "Facebook": 56})
    wasteful(conn, clk, (APP, "Instagram"), (APP, "Facebook"))

    payload = insights.build_payload(conn, clk, settings)
    cmp = payload["comparison"]
    assert cmp["unreadable_sources"] == 1 and cmp["like_for_like"] is False
    blob = json.dumps(payload)
    assert "Facebook" not in blob and "Instagram" not in blob


def test_the_fallback_describes_the_like_for_like_figure(conn, clk, settings):
    screen_time(conn, clk, settings, A, {"Instagram": 80})
    screen_time(conn, clk, settings, B, {"Instagram": 60})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 200})
    wasteful(conn, clk, (APP, "Instagram"), (SITE, "reddit.com"))

    text = insights.fallback_summary(insights.build_payload(conn, clk, settings))
    assert "Like for like, counted brain-rot time fell 25% week on week, from 80 min to 60 min." in text
    assert "the Mac's browser" in text
    assert "reddit" not in text


# --- helpers -------------------------------------------------------------------

Finding = progress.Finding


def _plain(findings):
    """Findings without their source ids, for comparing text."""
    return [Finding(f.kind, f.text) for f in findings]
