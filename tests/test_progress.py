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
import dr_brainrot_types as T  # noqa: E402
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
    return make_settings(tmp_path, db_path=tmp_path / "dr-brainrot.db", tz=TZ, go_live="2026-09-13",
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
    tight = make_settings(tmp_path, db_path=tmp_path / "dr-brainrot.db", tz=TZ, go_live="2026-09-13",
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


def test_on_a_sunday_it_describes_the_week_just_asked_for(settings):
    """Sunday 4 Oct: the check-in asks for 27 Sep - 3 Oct that morning, so that is the week (DR5.24)."""
    saturday = clock.FrozenClock("2026-10-03T10:00:00+00:00")
    assert progress._week_described(saturday, settings).start == B[0]
    sunday = clock.FrozenClock("2026-10-04T10:00:00+00:00")
    assert progress._week_described(sunday, settings).start == "2026-09-27"
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



# ==========================================================================
# On the page, and the loop Jacob will actually use
# ==========================================================================

def _page(settings, clk, path="/"):
    import db
    from dashboard import app as dashboard_app
    client = dashboard_app.create_app(settings, clk, lambda: db.connect(settings)).test_client()
    return client, client.get(path).get_data(as_text=True)


def _section(html, section_id):
    import re
    m = re.search(rf'<section id="{section_id}">(.*?)</section>', html, re.S)
    assert m, f"section {section_id} missing"
    return m.group(1)


def test_the_overview_shows_the_verdict_what_improved_and_what_to_try(conn, clk, settings):
    web(conn, clk, settings, "2026-09-15", {"reddit.com": 60, "news.example": 10})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 20, "news.example": 10})
    wasteful(conn, clk, (SITE, "reddit.com"))

    _client, html = _page(settings, clk)
    part = _section(html, "progress")
    assert "Week of Sun 2026-09-20 – Sat 2026-09-26, against the week before" in part
    assert "Down 57% on the week before: 70 min to 30 min." in part
    assert "Better than the week before" in part and "reddit.com fell from 60 min to 20 min." in part
    assert "Try next week" in part and "reddit.com was your biggest source of brain rot" in part
    # Wins have a green of their own (--win). They must never borrow the
    # pipeline's: --ok means a file arrived, and the two would be confusable.
    assert "state-ok" not in part and "import-ok" not in part


def test_settling_an_unreadable_reading_on_the_page_turns_it_into_a_win(conn, clk, settings):
    """The whole loop: unreadable -> set aside -> settled by hand -> compared."""
    screen_time(conn, clk, settings, A, {"Instagram": 40}, unreadable=("Facebook",))
    screen_time(conn, clk, settings, B, {"Instagram": 40, "Facebook": 56})
    wasteful(conn, clk, (APP, "Instagram"), (APP, "Facebook"))

    before = progress.progress(conn, clk, settings)
    assert "Facebook — couldn't be read the week before" in before.set_aside

    client, html = _page(settings, clk)
    eye = _section(html, "needs-your-eye")
    assert "Facebook" in eye
    import re
    action = re.search(r'<form method="post" action="(/measurements/\d+/fix)"', eye).group(1)

    resp = client.post(action, data={"minutes": "150"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 303 and "msg=fixed" in resp.headers["Location"]

    after = progress.progress(conn, clk, settings)
    assert after.set_aside == () and after.like_for_like
    assert "Facebook fell from 150 min to 56 min." in [w.text for w in after.wins]
    assert after.direction == "down"
    # Settled, it is no longer asked about.
    assert 'id="needs-your-eye"' not in _page(settings, clk)[1]

# --- helpers -------------------------------------------------------------------

Finding = progress.Finding


def _plain(findings):
    """Findings without their source ids, for comparing text."""
    return [Finding(f.kind, f.text) for f in findings]


# ==========================================================================
# The brain against your peak week (DR5.20): peak_chain
# ==========================================================================

@pytest.mark.parametrize("pairs,expected", [
    ([], None),                                      # the first week: nothing behind it
    ([(0.0, 5.0)], None),                            # nothing comparable with the week before
    ([(177.07, 118.58)], (0, 0, 118.58 / 177.07)),   # 20 Sep against 13 Sep, as the panel says
    ([(177.07, 118.58), (207.82, 168.97)], (0, 0, 118.58 / 177.07 * 168.97 / 207.82)),
    ([(100.0, 120.0)], (0, 0, 1.2)),                 # a new peak: above 1, capped only when drawn
    ([(100.0, 50.0), (40.0, 80.0)], (0, 0, 1.0)),    # a tie goes to the earliest week
    ([(10.0, 5.0), (0.0, 3.0), (4.0, 8.0)], (2, 2, 2.0)),   # an old break bounds the run, never blanks it
    ([(10.0, 0.0), (5.0, 5.0)], (1, 1, 1.0)),        # a week at zero cannot carry the chain back
    ([(10.0, 0.0)], (0, 0, 0.0)),                    # nothing on what both measured: a clean brain
    ([(25.0, 7.0), (7.0, 25.0)], (0, 0, 1.0)),       # 7/25 * 25/7 is a tie, not a new peak
    ([(25.0, 7.0), (7.0, 25.0), (25.0, 20.0)], (0, 0, 0.8)),  # ...so the peak stays the earliest
])
def test_the_chain_index_moves_with_each_like_for_like_step(pairs, expected):
    got = progress.chain_index(pairs)
    if expected is None:
        assert got is None
    else:
        assert got[:2] == expected[:2] and got[2] == pytest.approx(expected[2])


def test_the_brain_chains_exactly_the_pair_the_panel_shows(conn, clk, settings):
    web(conn, clk, settings, "2026-09-15", {"reddit.com": 60, "news.example": 10})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 20, "news.example": 10})
    wasteful(conn, clk, (SITE, "reddit.com"))

    p = progress.progress(conn, clk, settings)
    ch = progress.peak_chain(conn, clk, settings)
    assert ch.state == "drawn" and ch.week_start == p.week_start
    assert (ch.week_sunday, ch.peak_sunday) == (B[0], A[0])
    (step,) = ch.steps
    assert (step.before_min, step.after_min) == (p.before_min, p.after_min) == (70, 30)
    assert ch.ratio == pytest.approx(30 / 70)
    # news.example has no label: charged, inside the comparison, and counted apart.
    assert step.undecided_after_min == 10
    assert step.direction == p.direction == "down"


def test_an_unreadable_reading_is_left_out_of_the_peak_and_flagged(conn, clk, settings):
    screen_time(conn, clk, settings, A, {"Instagram": 40}, unreadable=("Facebook",))
    screen_time(conn, clk, settings, B, {"Instagram": 40, "Facebook": 56})
    wasteful(conn, clk, (APP, "Instagram"), (APP, "Facebook"))

    ch = progress.peak_chain(conn, clk, settings)
    (step,) = ch.steps
    assert (step.before_min, step.after_min) == (40, 40)      # never "rose from nothing"
    assert step.unreadable == 1 and step.sources_set_aside == 1 and step.channels_set_aside == ()
    assert ch.unreadable == 1                     # settling it can move the drawing


def test_the_brain_is_none_exactly_when_the_panel_is(settings, tmp_path):
    c = open_db(make_settings(tmp_path, db_path=tmp_path / "empty.db", tz=TZ,
                              go_live="2026-10-30", weekly_allowance_min=360))
    early = clock.FrozenClock("2026-09-16T10:00:00+00:00")
    assert progress.progress(c, early, settings) is None
    assert progress.peak_chain(c, early, settings) is None
    c.close()


def test_the_first_finished_week_sets_the_benchmark(conn, clk, tmp_path):
    late = make_settings(tmp_path, db_path=tmp_path / "late.db", tz=TZ, go_live="2026-09-20",
                         weekly_allowance_min=360)
    c = open_db(late)
    evidence.ensure_diagnostic_start(c, B[0])
    web(c, clk, late, "2026-09-22", {"reddit.com": 30})
    ch = progress.peak_chain(c, clk, late)
    assert ch.state == "first_week" and ch.ratio is None and ch.steps == ()
    c.close()


def test_the_running_week_never_joins_the_chain(conn, clk, settings):
    """Saturday 3 Oct: the brain still describes 20-26 Sep, like the panel, not the open week."""
    web(conn, clk, settings, "2026-09-15", {"reddit.com": 60})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 20})
    web(conn, clk, settings, "2026-09-29", {"reddit.com": 900})          # 27 Sep: not over yet
    wasteful(conn, clk, (SITE, "reddit.com"))
    saturday = clock.FrozenClock("2026-10-03T10:00:00+00:00")
    ch = progress.peak_chain(conn, saturday, settings)
    assert ch.week_sunday == B[0] and len(ch.steps) == 1
    assert ch.ratio == pytest.approx(20 / 60)


def test_the_overview_draws_the_brain_beside_the_summary_naming_both_weeks(conn, clk, settings):
    import re
    web(conn, clk, settings, "2026-09-15", {"reddit.com": 60, "news.example": 10})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 20, "news.example": 10})
    wasteful(conn, clk, (SITE, "reddit.com"))

    _client, html = _page(settings, clk)
    part = _section(html, "rot")
    text = " ".join(re.sub(r"<[^>]+>", " ", part).split())
    assert "Week of Sun 2026-09-20 – Sat 2026-09-26, against your peak week" in text
    assert "43% of your peak week" in text
    assert "This week 30 min" in text and "Peak week, Sun 2026-09-13 70 min" in text
    assert "Only what both weeks measured: 30 min of this week's" in text
    assert "10 min of those 30 min is still undecided" in text
    label = re.search(r'<svg class="rot-brain[^"]*"[^>]*role="img" aria-label="([^"]+)"', part).group(1)
    assert "30 min" in label and "70 min" in label and "Sun 2026-09-13" in label
    # Drawn and lit; undecided time is drawn apart and never inside a moving group.
    assert 'class="rot-light"' in part and "rot-blank" not in part
    undecided = re.search(r'<g class="rot-undecided"[^>]*>(.*?)</g>', part, re.S).group(1)
    assert "<path" in undecided and "rot-b" not in undecided
    # Its own section, a sibling: never inside the summary or the panel.
    assert html.count('<section id="rot">') == 1
    assert 'id="rot"' not in _section(html, "progress")
    assert html.index('<section id="rot">') < html.index('<section id="progress">')


def test_a_thin_overlap_is_never_chained(conn, clk, settings):
    """Phone time missing from the week described: what both weeks measured is a
    sliver of the week before, and chaining it would multiply every later week."""
    screen_time(conn, clk, settings, A, {"Instagram": 200})
    web(conn, clk, settings, "2026-09-15", {"reddit.com": 10})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 20})
    wasteful(conn, clk, (APP, "Instagram"), (SITE, "reddit.com"))

    ch = progress.peak_chain(conn, clk, settings)
    assert ch.state == "not_comparable" and ch.ratio is None
    (step,) = ch.steps
    assert (step.before_min, step.after_min) == (10, 20)
    assert step.channels_set_aside == ("Screen Time",)


def test_a_clean_week_before_is_nothing_to_scale_against_not_a_mismatch(conn, clk, settings):
    web(conn, clk, settings, "2026-09-15", {"news.example": 10})
    web(conn, clk, settings, "2026-09-22", {"news.example": 10, "reddit.com": 30})
    label_source(conn, clk, SITE, "news.example", T.WORTHWHILE)
    wasteful(conn, clk, (SITE, "reddit.com"))

    ch = progress.peak_chain(conn, clk, settings)
    assert ch.state == "nothing_before" and ch.steps[-1].after_min == 30


def test_a_cut_says_why_weeks_before_it_are_left_out(conn, clk, settings):
    sunday = clock.FrozenClock("2026-10-07T10:00:00+00:00")       # shows 27 Sep - 3 Oct
    screen_time(conn, clk, settings, A, {"Instagram": 200})
    web(conn, clk, settings, "2026-09-15", {"reddit.com": 10})
    web(conn, clk, settings, "2026-09-22", {"reddit.com": 40})
    web(conn, clk, settings, "2026-09-29", {"reddit.com": 30})
    wasteful(conn, clk, (APP, "Instagram"), (SITE, "reddit.com"))

    ch = progress.peak_chain(conn, sunday, settings)
    assert ch.state == "drawn" and ch.since_sunday == B[0] and ch.cut == "thin"
    assert ch.from_sunday == A[0] and ch.ratio == pytest.approx(30 / 40)


def test_two_clean_weeks_are_a_clean_brain(conn, clk, settings):
    web(conn, clk, settings, "2026-09-15", {"news.example": 10})
    web(conn, clk, settings, "2026-09-22", {"news.example": 12})
    label_source(conn, clk, SITE, "news.example", T.WORTHWHILE)

    ch = progress.peak_chain(conn, clk, settings)
    assert ch.state == "nothing_before" and ch.steps[-1].after_min == 0
