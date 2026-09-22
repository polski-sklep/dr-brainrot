"""Tests for report.py — DRBR Phase 2, Agent D.

Every function in CONTRACTS.md Part II §9, against one migrated database per
test with evidence written the way source modules write it. No network, frozen
clocks.

Two properties are checked beyond the figures themselves: every report
function is read-only (the database is byte-for-byte the same after a full
report pass), and no report function turns a vault capture into minutes.
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import allowance  # noqa: E402
import clock  # noqa: E402
import db  # noqa: E402
import labels  # noqa: E402
import drbr_types as T  # noqa: E402
import evidence  # noqa: E402
import report  # noqa: E402
from tests.test_allowance import (  # noqa: E402
    NEXT_WEEK,
    WED,
    WEEK,
    label_source,
    listen,
    make_settings,
    measure,
    open_db,
    spotify_item,
    youtube_item,
)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def conn(settings):
    c = open_db(settings)
    yield c
    c.close()


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-01T10:00:00+00:00")


_capture_seq = [0]


def vault_capture(conn, clk, *, group, outcome, captured_at="2026-09-08T10:00:00+00:00",
                  edited=0, scans=None):
    """A logged capture plus its scan history (latest last)."""
    _capture_seq[0] += 1
    cid = f"{_capture_seq[0]:032x}"
    conn.execute(
        "INSERT INTO vault_captures (capture_id, captured_at, capture_type, keeper_group, url, title, "
        "initial_rel_path, body_sha256, logged_at) VALUES (?, ?, ?, ?, NULL, 'note', '01-Inbox/n.md', "
        "'00', ?)",
        (cid, captured_at, "x-post", group, captured_at),
    )
    conn.commit()
    for i, (result, was_edited) in enumerate(scans or [(outcome, edited)]):
        rec = evidence.begin_import(conn, clk, kind="vault_scan", parser_version="vault-test")
        conn.execute(
            "INSERT INTO vault_observations (import_id, capture_id, scanned_at, outcome, rel_path, edited, copies) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rec.id, cid, f"2026-09-1{i}T10:00:00+00:00", result,
             None if result == "excluded" else "somewhere/n.md",
             None if result == "excluded" else was_edited,
             0 if result == "excluded" else 1),
        )
        conn.commit()
    return cid


def seed_week(conn, clk, settings):
    """One allowance week with every provenance present."""
    clk.set(WED)
    db.current_week(conn, clk, settings)
    ep = spotify_item(conn, clk, settings, "ReportEpisode000000000", show="Pod", minutes=200)
    listen(conn, clk, ep.spotify_id, 30)
    label_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, "UCgood", T.WORTHWHILE, "Good")
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCgood", day="2026-09-07",
            minutes=40.0, item_key="goodvideo01", item_title="Good video")
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCbad", day="2026-09-08",
            minutes=None, status="progress_missing", item_key="badvideo001")
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-06",
            period_end="2026-09-12", minutes=90.0)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="YouTube", day="2026-09-06",
            period_end="2026-09-12", minutes=33.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="x.com", day="2026-09-09", minutes=12.0)


# ==========================================================================
# weekly_totals
# ==========================================================================

def test_weekly_totals_carry_provenance_device_and_import_status(conn, clk, settings):
    seed_week(conn, clk, settings)
    rows = report.weekly_totals(conn, clk, settings, WEEK)
    by_key = {(r.group, r.provenance, r.device): r for r in rows}

    pod = by_key[(T.GROUP_PODCASTS, T.PROV_API, T.DEVICE_ACCOUNT)]
    assert pod.minutes == pytest.approx(30) and pod.import_status is None

    yt = by_key[(T.GROUP_YOUTUBE, T.PROV_PAGE, T.DEVICE_ACCOUNT)]
    assert yt.minutes == pytest.approx(40)            # worthwhile still counted in totals
    assert yt.unmeasured_items == 1
    assert yt.import_status == report.IMPORT_SO_FAR       # synced mid-week, and the week still runs

    ios = by_key[(T.GROUP_IPHONE, T.PROV_SCREENSHOT, T.DEVICE_IPHONE)]
    assert ios.minutes == pytest.approx(90)           # screenshot stays screenshot
    assert ios.covered_excluded_min == pytest.approx(33)

    x = by_key[(T.GROUP_X, T.PROV_EXPORT, T.DEVICE_DESKTOP)]
    assert x.minutes == pytest.approx(12)

    # No Cold Turkey domain besides X arrived, but the import did: a real zero,
    # not a missing week.
    desktop = by_key[(T.GROUP_DESKTOP, T.PROV_EXPORT, T.DEVICE_DESKTOP)]
    assert desktop.minutes == 0.0 and desktop.import_status == "ok"
    assert all(r.provenance in T.PROVENANCES for r in rows)


def test_youtube_week_is_covered_by_any_sync_received_after_its_saturday(conn, clk, settings):
    clk.set(WED)
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCa", day="2026-09-08",
            minutes=5.0, item_key="aaaaaaaaaaa")
    mweek = T.measurement_week_for_allowance_week(WEEK, settings.tz)
    assert report._covering_import(conn, settings, "youtube_history", mweek) is None

    # Received Sunday after the week, with a watch-date range that is not the week.
    clk.set("2026-09-13T08:00:00+00:00")
    rec = evidence.begin_import(conn, clk, kind="youtube_history", parser_version="t")
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-12", period_end="2026-09-13")
    yt = next(r for r in report.weekly_totals(conn, clk, settings, WEEK) if r.group == T.GROUP_YOUTUBE)
    assert yt.import_status == "ok" and yt.minutes == pytest.approx(5)


def test_a_this_week_screen_time_capture_never_covers_a_week(conn, clk, settings):
    clk.set("2026-09-14T08:00:00+00:00")
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="t")
    rec.warnings.append("This Week view: resend Last Week")
    evidence.finish_import(conn, clk, settings, rec, period_start=None, period_end=None)
    ios = next(r for r in report.weekly_totals(conn, clk, settings, WEEK) if r.group == T.GROUP_IPHONE)
    assert (ios.minutes, ios.import_status) == (None, report.IMPORT_MISSING)
    assert "Screen Time for the week 2026-09-06 to 2026-09-12 has not been received." in \
        report.blind_spots(conn, clk, settings)


def test_x_from_two_domains_on_one_day_is_summed_not_collapsed(conn, clk, settings):
    clk.set(WED)
    x = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "x.com")
    conn.commit()
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t")
    for domain, minutes in (("x.com", 12.5), ("twitter.com", 7.5)):
        evidence.upsert_measurement(conn, rec, source_id=x.id, device="desktop", provenance="manual_export",
                                    period_start="2026-09-08", period_end="2026-09-08",
                                    minutes=minutes, item_key=domain)
    rec.warnings.append("days on or after the export date skipped")
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-06", period_end="2026-09-08")

    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    assert pooled.by_group[T.GROUP_X].charged_min == pytest.approx(20)
    x_rows = [r for r in report.weekly_totals(conn, clk, settings, WEEK) if r.group == T.GROUP_X]
    assert x_rows[0].minutes == pytest.approx(20) and x_rows[0].import_status == "partial"
    assert len(report.records(conn, clk, settings, group=T.GROUP_X)) == 2


def test_unknown_youtube_channel_counts_as_unlabelled_even_if_labelled(conn, clk, settings):
    clk.set(WED)
    label_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, "unknown", T.WORTHWHILE)
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="unknown", day="2026-09-08",
            minutes=30.0, item_key="unknownvid1")
    rows = report.records(conn, clk, settings, group=T.GROUP_YOUTUBE)
    assert (rows[0].label, rows[0].label_basis) == (T.WASTEFUL, T.BASIS_DEFAULT)
    assert allowance.pooled_minutes(conn, settings, WEEK).charged_min == pytest.approx(30)

    # An explicit per-video override at capture still wins.
    labels.set_item_override(conn, clk, "youtube:unknownvid1", T.WORTHWHILE)
    conn.commit()
    assert allowance.pooled_minutes(conn, settings, WEEK).worthwhile_min == pytest.approx(30)


def test_report_windows_start_at_the_diagnostic_start(conn, clk, settings):
    clk.set("2026-08-25T10:00:00+00:00")
    old = spotify_item(conn, clk, settings, "PreLiveEpisode00000000", show="Pod", minutes=300)
    listen(conn, clk, old.spotify_id, 100)                              # before go-live
    vault_capture(conn, clk, group="podcasts", outcome="kept", captured_at="2026-08-25T10:00:00+00:00")
    evidence.ensure_diagnostic_start(conn, "2026-09-06")
    clk.set(WED)
    listen(conn, clk, old.spotify_id, 40)
    vault_capture(conn, clk, group="podcasts", outcome="kept", captured_at=WED)

    assert report.window_start(conn, clk, settings) == "2026-09-06"
    pod = next(r for r in report.cost_per_keeper(conn, clk, settings) if r.group == T.GROUP_PODCASTS)
    assert (pod.minutes, pod.keepers) == (pytest.approx(40), 1)
    early = next(r for r in report.cost_per_keeper(conn, clk, settings, since="2026-08-01")
                 if r.group == T.GROUP_PODCASTS)
    assert early.minutes == pytest.approx(40)                           # clamped, not widened
    assert [s.week_start for s in report.label_split(conn, clk, settings, weeks=4)] == [WEEK]
    assert {r.day for r in report.records(conn, clk, settings)} == {"2026-09-09"}
    f = report.funnels(conn, clk, settings)
    assert f.vault.kept == 1 and f.pop.captured == 0                    # captured before go-live


def test_weekly_totals_mark_missing_evidence_as_none_not_zero(conn, clk, settings):
    clk.set(WED)
    rows = report.weekly_totals(conn, clk, settings, WEEK)
    missing = {(r.group, r.provenance): r for r in rows}
    for key in [(T.GROUP_YOUTUBE, T.PROV_PAGE), (T.GROUP_IPHONE, T.PROV_SCREENSHOT),
                (T.GROUP_DESKTOP, T.PROV_EXPORT)]:
        assert missing[key].minutes is None
        # Wednesday of the week itself: nothing is owed yet (DR5.10).
        assert missing[key].import_status == report.IMPORT_NOT_DUE


def test_absence_is_only_a_gap_once_the_week_has_been_asked_for(conn, clk, settings):
    """The same absence, worded by the schedule. WEEK is Sun 6 - Sat 12 Sep."""
    mweek = T.measurement_week_for_allowance_week(WEEK, settings.tz)
    assert report.due_on(mweek).isoformat() == "2026-09-14"      # the Monday after

    def ios_status():
        rows = report.weekly_totals(conn, clk, settings, WEEK)
        return next(r for r in rows if r.group == T.GROUP_IPHONE).import_status

    clk.set(WED)                                                 # mid-week
    assert ios_status() == report.IMPORT_NOT_DUE
    clk.set("2026-09-13T10:00:00+00:00")                         # Sunday: still not asked
    assert ios_status() == report.IMPORT_NOT_DUE
    clk.set("2026-09-14T06:00:00+00:00")                         # Monday, 08:00 Warsaw
    assert ios_status() == report.IMPORT_MISSING
    assert not report.everything_in(report.weekly_totals(conn, clk, settings, WEEK))


def test_a_week_that_ended_before_go_live_is_never_due(conn, clk, settings):
    """DR2.2: it is never asked for, so its absence is never called a gap."""
    live = dataclasses.replace(settings, go_live="2026-09-06")
    early = T.measurement_week(T.parse_date("2026-08-30"))       # ends Sat 5 Sep
    clk.set("2026-10-01T10:00:00+00:00")                         # long past its Monday
    assert report.week_is_due(clk, live, early) is False
    # The week go-live falls in is asked for as usual.
    assert report.week_is_due(clk, live, T.measurement_week(T.parse_date("2026-09-06"))) is True



# ==========================================================================
# top_sources: the week by name
# ==========================================================================

def test_top_sources_names_the_things_consumed_biggest_first(conn, clk, settings):
    seed_week(conn, clk, settings)
    label_source(conn, clk, T.KIND_IPHONE_APP, "Instagram", T.WASTEFUL)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, "amazon.pl", T.TOOL, "amazon.pl")
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="amazon.pl", day="2026-09-09",
            minutes=300.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="kwargle.example", day="2026-09-09",
            minutes=5.0)                                  # nobody has labelled this one

    rows = report.top_sources(conn, clk, settings, WEEK, limit=None)
    by_name = {r.display_name: r for r in rows}

    # Names, not groups or provenances.
    assert [r.display_name for r in rows][:3] == ["Instagram", "Good", "Pod"]
    assert by_name["Instagram"].minutes == pytest.approx(90) and by_name["Instagram"].group == T.GROUP_IPHONE

    # A tool source is not content and never appears, however large.
    assert "amazon.pl" not in by_name

    # The label is carried, and never invented: x.com has none.
    assert by_name["Good"].label == T.WORTHWHILE
    assert by_name["kwargle.example"].label is None       # never guessed
    assert by_name["Instagram"].label == T.WASTEFUL

    # An unmeasured row contributes no minutes, so it is not a line at all.
    assert all(r.minutes > 0 for r in rows)
    assert "UCbad" not in by_name

    # Nothing here went through the queue.
    assert all(r.all_reflex for r in rows)
    assert by_name["Good"].provenances == frozenset({T.PROV_PAGE})


def test_top_sources_marks_the_promoted_part_as_not_reflex(conn, clk, settings):
    """A YouTube video promoted before it was watched is a choice, not a reflex."""
    clk.set(WED)
    db.current_week(conn, clk, settings)
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCmix", day="2026-09-08",
            minutes=20.0, item_key="planned0001", display="Mixed")
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCmix", day="2026-09-09",
            minutes=30.0, item_key="reflexvid01", display="Mixed")
    clk.set("2026-09-07T08:00:00+00:00")
    youtube_item(conn, clk, settings, "planned0001", channel="UCmix", channel_title="Mixed")
    conn.execute("UPDATE queue SET state = 'promoted', promoted_at = ? WHERE spotify_id = ?",
                 ("2026-09-07T08:00:00+00:00", T.queue_key_for_youtube("planned0001")))
    conn.commit()
    clk.set(WED)

    (row,) = [r for r in report.top_sources(conn, clk, settings, WEEK, limit=None)
              if r.display_name == "Mixed"]
    assert row.minutes == pytest.approx(50)
    assert row.reflex_min == pytest.approx(30)      # only the unpromoted video
    assert row.all_reflex is False


def test_top_sources_agrees_with_the_weeks_content_total(conn, clk, settings):
    """The list and the totals are the same minutes, so they cannot drift apart."""
    seed_week(conn, clk, settings)
    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    listed = sum(r.minutes for r in report.top_sources(conn, clk, settings, WEEK, limit=None))
    assert listed == pytest.approx(pooled.charged_min + pooled.worthwhile_min)

# ==========================================================================
# label_split and allowance_status
# ==========================================================================

def test_label_split_is_weekly_oldest_first(conn, clk, settings):
    evidence.ensure_diagnostic_start(conn, "2026-08-30")
    seed_week(conn, clk, settings)
    clk.set("2026-09-15T10:00:00+00:00")           # the following week
    splits = report.label_split(conn, clk, settings, weeks=4)
    # The week of 23 Aug ends before the diagnostic start and is left out.
    assert [s.week_start for s in splits] == ["2026-08-30T16:00:00+00:00", WEEK, NEXT_WEEK]
    assert splits[1].charged_min == pytest.approx(30 + 90 + 12)
    assert splits[1].worthwhile_min == pytest.approx(40)
    assert (splits[2].wasteful_min, splits[2].worthwhile_min) == (0, 0)


def test_allowance_status_reports_spent_remaining_overage_and_promoted(conn, clk, settings):
    seed_week(conn, clk, settings)
    item = spotify_item(conn, clk, settings, "PromoteEpisode00000000", show="Pod", minutes=25)
    db.promote(conn, clk, settings, item.id)
    status = report.allowance_status(conn, clk, settings)
    assert status.week_start == WEEK
    assert (status.allowance_min, status.overage_min, status.effective_min) == (180, 0, 180)
    assert not hasattr(status, "debt_min")
    assert status.spent_wasteful_min == pytest.approx(132)
    assert status.remaining_min == 48
    assert status.promoted_min == 25


def test_allowance_status_reports_this_weeks_overage_so_far(conn, clk, settings):
    seed_week(conn, clk, settings)                              # 132 wasteful so far
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-09",
            minutes=118.0)                                      # 250 wasteful against 180
    status = report.allowance_status(conn, clk, settings)
    assert status.overage_min == 70
    assert status.remaining_min == 0
    assert status.effective_min == 180


def test_allowance_status_does_not_create_the_week_row(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-08-30",
            period_end="2026-09-05", minutes=230.0)
    db.current_week(conn, clk, settings)                       # the week of 30 Aug exists, 50 over
    clk.set(WED)                                                # next week: no row yet
    status = report.allowance_status(conn, clk, settings)
    # Described as rollover would open it: the full allowance, nothing carried (DR5.2).
    assert status.overage_min == 0                              # nothing spent this week yet
    assert status.effective_min == 180
    assert status.remaining_min == 180
    assert conn.execute("SELECT COUNT(*) FROM weeks WHERE week_start = ?", (WEEK,)).fetchone()[0] == 0


def test_label_split_carries_each_weeks_allowance_and_overage(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-08-30",
            period_end="2026-09-05", minutes=230.0)
    clk.set("2026-09-01T10:00:00+00:00")
    db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET allowance_min = 200")        # a snapshotted allowance wins over config
    conn.commit()
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-09-06",
            period_end="2026-09-12", minutes=190.0)
    clk.set(WED)                                                # no row for this week: config's 180
    by_week = {w.week_start: w for w in report.label_split(conn, clk, settings, weeks=4)}
    first = by_week["2026-08-30T16:00:00+00:00"]
    assert (first.allowance_min, first.overage_min) == (200, 30)
    # Week 2 is judged against its own 180, not reduced by week 1's 30.
    assert (by_week[WEEK].allowance_min, by_week[WEEK].overage_min) == (180, 10)


# ==========================================================================
# gated_vs_ungated
# ==========================================================================

def test_gated_vs_ungated_rows_and_share(conn, clk, settings):
    item = youtube_item(conn, clk, settings, "GatedVideo1", channel="UCg", minutes=60)
    clk.set("2026-09-07T08:00:00+00:00")
    db.promote(conn, clk, settings, item.id)
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCg", day="2026-09-08",
            minutes=30.0, item_key="GatedVideo1")
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCg", day="2026-09-08",
            minutes=10.0, item_key="Impulse0001")
    rows = {r.group: r for r in report.gated_vs_ungated(conn, clk, settings, WEEK)}
    assert [r for r in rows] == list(T.REPORT_GROUPS)
    assert rows[T.GROUP_YOUTUBE].gated_min == pytest.approx(30)
    assert rows[T.GROUP_YOUTUBE].ungated_min == pytest.approx(10)
    assert rows[T.GROUP_YOUTUBE].share_ungated == pytest.approx(0.25)
    assert rows[T.GROUP_PODCASTS].share_ungated is None           # nothing consumed


# ==========================================================================
# cost_per_keeper
# ==========================================================================

def test_cost_per_keeper_ranks_prices_with_none_last(conn, clk, settings):
    clk.set(WED)
    ep = spotify_item(conn, clk, settings, "KeeperEpisode000000000", show="Pod", minutes=200)
    listen(conn, clk, ep.spotify_id, 180)                                  # podcasts 180 / 2 = 90
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="X", day="2026-09-06",
            period_end="2026-09-12", minutes=100.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="twitter.com", day="2026-09-08",
            minutes=20.0)                                                  # x 120 / 2 = 60
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCa", day="2026-09-08",
            minutes=200.0, item_key="aaaaaaaaaaa")                         # youtube 200 / 1
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="YouTube", day="2026-09-06",
            period_end="2026-09-12", minutes=999.0)                        # covered: never priced
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-06",
            period_end="2026-09-12", minutes=70.0)                         # no keeper path

    for _ in range(2):
        vault_capture(conn, clk, group="podcasts", outcome="kept")
        vault_capture(conn, clk, group="x", outcome="kept")
    vault_capture(conn, clk, group="youtube", outcome="kept")
    vault_capture(conn, clk, group="youtube", outcome="unprocessed")
    vault_capture(conn, clk, group="youtube", outcome="excluded")
    vault_capture(conn, clk, group="web", outcome="kept")

    rows = report.cost_per_keeper(conn, clk, settings)
    assert [r.group for r in rows[:3]] == [T.GROUP_X, T.GROUP_PODCASTS, T.GROUP_YOUTUBE]
    by = {r.group: r for r in rows}
    assert by[T.GROUP_X].minutes_per_keeper == pytest.approx(60)
    assert by[T.GROUP_PODCASTS].minutes_per_keeper == pytest.approx(90)
    assert by[T.GROUP_YOUTUBE].minutes_per_keeper == pytest.approx(200)

    ios = by[T.GROUP_IPHONE]
    assert ios.minutes == pytest.approx(70) and ios.keepers == 0
    assert ios.minutes_per_keeper is None                               # never inf, never 0
    assert by[T.GROUP_DESKTOP].minutes_per_keeper is None
    assert by[T.GROUP_WEB].minutes is None and by[T.GROUP_WEB].keepers == 1
    assert by[T.GROUP_WEB].minutes_per_keeper is None
    assert all(r.minutes_per_keeper is None for r in rows[3:])

    assert by[T.GROUP_X].provenances == (T.PROV_SCREENSHOT, T.PROV_EXPORT, T.PROV_VAULT)
    assert by[T.GROUP_PODCASTS].provenances == (T.PROV_API, T.PROV_VAULT)
    assert by[T.GROUP_PODCASTS].blind_spots                              # caveats travel with the row


def test_cost_per_keeper_with_zero_keepers_everywhere(conn, clk, settings):
    clk.set(WED)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-08", minutes=50.0)
    rows = report.cost_per_keeper(conn, clk, settings)
    assert [r.group for r in rows] == list(T.REPORT_GROUPS)
    assert all(r.minutes_per_keeper is None and r.keepers == 0 for r in rows)


def test_cost_per_keeper_since_filters_minutes_and_keepers(conn, clk, settings):
    clk.set("2026-08-25T10:00:00+00:00")
    ep = spotify_item(conn, clk, settings, "SinceEpisode0000000000", show="Pod", minutes=500)
    listen(conn, clk, ep.spotify_id, 100)
    vault_capture(conn, clk, group="podcasts", outcome="kept", captured_at="2026-08-25T10:00:00+00:00")
    clk.set(WED)
    listen(conn, clk, ep.spotify_id, 40)
    vault_capture(conn, clk, group="podcasts", outcome="kept", captured_at=WED)

    everything = {r.group: r for r in report.cost_per_keeper(conn, clk, settings)}
    recent = {r.group: r for r in report.cost_per_keeper(conn, clk, settings, since="2026-09-06")}
    assert everything[T.GROUP_PODCASTS].minutes_per_keeper == pytest.approx(70)
    assert recent[T.GROUP_PODCASTS].minutes == pytest.approx(40)
    assert recent[T.GROUP_PODCASTS].keepers == 1


# ==========================================================================
# funnels
# ==========================================================================

def test_vault_funnel_leaves_excluded_out_of_the_denominator(conn, clk, settings):
    vault_capture(conn, clk, group="x", outcome="kept", edited=1)
    vault_capture(conn, clk, group="x", outcome="kept", edited=0)
    vault_capture(conn, clk, group="web", outcome="unprocessed")
    vault_capture(conn, clk, group="youtube", outcome="excluded")
    # Latest scan wins: once unprocessed, now kept.
    vault_capture(conn, clk, group="web", outcome="kept", scans=[("unprocessed", 0), ("kept", 1)])

    f = report.funnels(conn, clk, settings)
    assert f.vault == report.VaultFunnel(captured=4, unprocessed=1, kept=3, excluded=1, edited_kept=2)
    assert isinstance(f.pop, db.FunnelStats)


def test_pop_funnel_counts_youtube_played_as_measured_watching(conn, clk, settings):
    evidence.ensure_diagnostic_start(conn, "2026-08-30")         # go-live before the captures
    watched = youtube_item(conn, clk, settings, "WatchedVid1", channel="UCw")
    unwatched = youtube_item(conn, clk, settings, "SkippedVid1", channel="UCw")
    early = youtube_item(conn, clk, settings, "EarlyVideo1", channel="UCw")
    clk.set("2026-09-07T08:00:00+00:00")
    for item in (watched, unwatched, early):
        db.promote(conn, clk, settings, item.id)
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCw", day="2026-09-08",
            minutes=12.0, item_key="WatchedVid1")
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCw", day="2026-09-06",
            minutes=12.0, item_key="EarlyVideo1")                # before promotion: not "played"
    stats = report.funnels(conn, clk, settings).pop
    assert stats.promoted == 3
    assert stats.played == 1
    assert stats.fully_played == 0


# ==========================================================================
# blind_spots
# ==========================================================================

def test_blind_spots_list_the_standing_gaps(conn, clk, settings):
    clk.set(WED)
    text = "\n".join(report.blind_spots(conn, clk, settings))
    for needle in ("outside the queue is invisible", "top domains", "this Mac only",
                   "rewatches", "time of day"):
        assert needle in text


def test_blind_spots_name_missing_evidence_for_the_last_complete_week(conn, clk, settings):
    clk.set(WED)                                        # last complete week: 30 Aug .. 5 Sep
    spots = report.blind_spots(conn, clk, settings)
    for name in ("Screen Time", "Cold Turkey", "YouTube history"):
        assert f"{name} for the week 2026-08-30 to 2026-09-05 has not been received." in spots

    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-08-30",
            period_end="2026-09-05", minutes=10.0)
    spots = report.blind_spots(conn, clk, settings)
    assert not any(s.startswith("Screen Time for the week 2026-08-30") for s in spots)
    assert "Cold Turkey for the week 2026-08-30 to 2026-09-05 has not been received." in spots


def test_blind_spots_count_youtube_progress_missing(conn, clk, settings):
    clk.set(WED)
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCm", day="2026-09-07",
                minutes=None, status="progress_missing", item_key=vid)
    spots = report.blind_spots(conn, clk, settings)
    assert any(s.startswith("2 YouTube video(s) with no readable progress") for s in spots)


# ==========================================================================
# records
# ==========================================================================

def test_records_filter_by_group_label_and_date(conn, clk, settings):
    seed_week(conn, clk, settings)
    everything = report.records(conn, clk, settings)
    assert {r.record_type for r in everything} == {"listening", "measurement"}
    assert all(r.provenance in T.PROVENANCES for r in everything)
    assert [r.day for r in everything] == sorted((r.day for r in everything), reverse=True)

    yt = report.records(conn, clk, settings, group=T.GROUP_YOUTUBE)
    assert {r.group for r in yt} == {T.GROUP_YOUTUBE} and len(yt) == 2
    missing = next(r for r in yt if r.status == "progress_missing")
    assert missing.minutes is None

    worthwhile = report.records(conn, clk, settings, label=T.WORTHWHILE)
    assert [r.item for r in worthwhile] == ["Good video"]
    assert worthwhile[0].label_basis == T.BASIS_SOURCE_LABEL

    day = report.records(conn, clk, settings, start="2026-09-09", end="2026-09-09")
    # Period overlap: the Screen Time week (6..12 Sep) contains 9 Sep.
    assert {(r.group, r.day) for r in day} == {(T.GROUP_PODCASTS, "2026-09-09"), (T.GROUP_X, "2026-09-09"),
                                               (T.GROUP_IPHONE, "2026-09-06")}

    covered = [r for r in everything if r.covered_elsewhere]
    assert len(covered) == 1 and covered[0].source == "YouTube"
    assert len(report.records(conn, clk, settings, limit=2)) == 2

    with pytest.raises(ValueError):
        report.records(conn, clk, settings, group="tiktok")


# ==========================================================================
# Properties
# ==========================================================================

def _dump(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in tables}


def _full_report(conn, clk, settings):
    return (
        report.diagnostic_banner(conn, clk, settings),
        report.weekly_totals(conn, clk, settings, WEEK),
        report.label_split(conn, clk, settings),
        report.allowance_status(conn, clk, settings),
        report.gated_vs_ungated(conn, clk, settings, WEEK),
        report.cost_per_keeper(conn, clk, settings),
        report.funnels(conn, clk, settings),
        report.blind_spots(conn, clk, settings),
        report.records(conn, clk, settings),
    )


def test_every_report_function_is_read_only(conn, clk, settings):
    seed_week(conn, clk, settings)
    vault_capture(conn, clk, group="x", outcome="kept")
    clk.set("2026-09-22T10:00:00+00:00")                # a week with no row yet
    before = _dump(conn)
    _full_report(conn, clk, settings)
    assert _dump(conn) == before


def test_no_report_function_converts_vault_captures_into_minutes(conn, clk, settings):
    seed_week(conn, clk, settings)
    baseline = _full_report(conn, clk, settings)
    for group in ("x", "youtube", "podcasts", "web", "other"):
        for outcome in ("kept", "unprocessed", "excluded"):
            vault_capture(conn, clk, group=group, outcome=outcome, captured_at=WED)
    after = _full_report(conn, clk, settings)

    assert after[1] == baseline[1]                       # weekly totals
    assert after[2] == baseline[2]                       # label split
    assert after[3] == baseline[3]                       # allowance status
    assert after[4] == baseline[4]                       # gated vs ungated
    assert after[8] == baseline[8]                       # records
    before_minutes = {r.group: r.minutes for r in baseline[5]}
    for row in after[5]:
        if row.group in before_minutes:
            assert row.minutes == before_minutes[row.group]
        else:
            assert row.minutes is None                   # web/other: keepers, never minutes


# ==========================================================================
# Tool: not content, excluded from every content figure, shown (DR5.5)
# ==========================================================================

def seed_tool_week(conn, clk, settings):
    """A week with wasteful and worthwhile content plus tool time, in several groups."""
    clk.set(WED)
    db.current_week(conn, clk, settings)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, "amazon.pl", T.TOOL)
    label_source(conn, clk, T.KIND_X, "x", T.WASTEFUL)
    label_source(conn, clk, T.KIND_PODCAST_SHOW, "Bank Updates", T.TOOL)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="amazon.pl", day="2026-09-08", minutes=150.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-08", minutes=40.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="x.com", day="2026-09-09", minutes=20.0)
    ep = spotify_item(conn, clk, settings, "ToolEpisode00000000000", show="Bank Updates", minutes=100)
    listen(conn, clk, ep.spotify_id, 25)                                  # podcast from a tool show


def test_weekly_totals_show_tool_minutes_apart_from_content(conn, clk, settings):
    seed_tool_week(conn, clk, settings)
    by_key = {(r.group, r.provenance, r.device): r for r in report.weekly_totals(conn, clk, settings, WEEK)}
    desktop = by_key[(T.GROUP_DESKTOP, T.PROV_EXPORT, T.DEVICE_DESKTOP)]
    assert (desktop.minutes, desktop.tool_min) == (pytest.approx(40), pytest.approx(150))
    pod = by_key[(T.GROUP_PODCASTS, T.PROV_API, T.DEVICE_ACCOUNT)]
    assert (pod.minutes, pod.tool_min) == (0.0, pytest.approx(25))
    x = by_key[(T.GROUP_X, T.PROV_EXPORT, T.DEVICE_DESKTOP)]
    assert (x.minutes, x.tool_min) == (pytest.approx(20), 0.0)


def test_label_split_and_allowance_status_exclude_tool_but_carry_it(conn, clk, settings):
    seed_tool_week(conn, clk, settings)
    (split,) = [s for s in report.label_split(conn, clk, settings) if s.week_start == WEEK]
    assert split.charged_min == pytest.approx(60)                        # reddit 40 + x 20
    assert split.worthwhile_min == 0
    assert split.tool_min == pytest.approx(175)                           # amazon 150 + tool show 25
    assert split.overage_min == 0

    status = report.allowance_status(conn, clk, settings)
    assert status.week_start == WEEK
    assert status.spent_wasteful_min == pytest.approx(60)
    assert status.remaining_min == 120 and status.overage_min == 0


def test_gated_vs_ungated_leaves_tool_out(conn, clk, settings):
    seed_tool_week(conn, clk, settings)
    rows = {r.group: r for r in report.gated_vs_ungated(conn, clk, settings, WEEK)}
    assert rows[T.GROUP_DESKTOP].ungated_min == pytest.approx(40)
    assert rows[T.GROUP_PODCASTS].share_ungated is None                  # only tool listening: nothing consumed


def test_cost_per_keeper_counts_content_minutes_only(conn, clk, settings):
    seed_tool_week(conn, clk, settings)
    vault_capture(conn, clk, group="x", outcome="kept")
    vault_capture(conn, clk, group="podcasts", outcome="kept")
    by = {r.group: r for r in report.cost_per_keeper(conn, clk, settings)}
    assert by[T.GROUP_DESKTOP].minutes == pytest.approx(40)              # amazon.pl's 150 left out
    assert by[T.GROUP_X].minutes_per_keeper == pytest.approx(20)
    assert by[T.GROUP_PODCASTS].minutes == 0.0                           # the tool show's 25 left out


def test_records_filter_accepts_tool(conn, clk, settings):
    seed_tool_week(conn, clk, settings)
    tool = report.records(conn, clk, settings, label=T.TOOL)
    assert sorted(r.source for r in tool) == ["Bank Updates", "amazon.pl"]
    assert all(r.label_basis == T.BASIS_SOURCE_LABEL for r in tool)
    assert all(r.label != T.TOOL for r in report.records(conn, clk, settings, label=T.WASTEFUL))
    with pytest.raises(ValueError):
        report.records(conn, clk, settings, label="neutral")


def test_blind_spots_state_the_content_only_scope(conn, clk, settings):
    clk.set(WED)
    spots = report.blind_spots(conn, clk, settings)
    assert spots[0] == ("Content only; sources you mark as tool (shopping, email, utilities…) "
                        "are excluded and shown separately.")
