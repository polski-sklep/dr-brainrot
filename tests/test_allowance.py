"""Tests for allowance.py and the label-aware gate — DRBR Phase 2, Agent D.

No network, frozen clocks, a fresh migrated database per test. Measurements
are written through `evidence` exactly as a source module would, and labels
through `labels`, so the gate is tested against the contracts rather than
against private shortcuts.

The matrix at the top is the important part: every combination of
{item override worthwhile / wasteful / none} x {source label worthwhile /
wasteful / unlabelled} x {spotify, youtube} is run through promotion cost,
refusal, lock enforcement, recorded overage and alerts. The rule under test is short —
override beats source beats the wasteful default, and only wasteful spends —
and the matrix is what proves no path quietly disagrees with it.
"""
from __future__ import annotations

import pathlib
import sys
from datetime import date

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import allowance  # noqa: E402
import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import drbr_types as T  # noqa: E402
import evidence  # noqa: E402
import labels  # noqa: E402
import poll  # noqa: E402

MIN = 60_000

# Europe/Warsaw is UTC+2 in September 2026: Sunday 18:00 local is 16:00Z.
WEEK = "2026-09-06T16:00:00+00:00"          # allowance week opening Sun 6 Sep 18:00
NEXT_WEEK = "2026-09-13T16:00:00+00:00"
WED = "2026-09-09T10:00:00+00:00"
CAPTURED = "2026-09-01T10:00:00+00:00"      # a week earlier: locks long lifted

PROVENANCE_KIND = {
    T.PROV_PAGE: "youtube_history",
    T.PROV_SCREENSHOT: "screentime",
    T.PROV_EXPORT: "coldturkey",
}


# ==========================================================================
# Shared helpers (test_report.py imports these)
# ==========================================================================

class FakeBot:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_message(self, **kwargs):
        self.texts.append(kwargs["text"])


def make_settings(tmp_path, **over):
    base = dict(tz="Europe/Warsaw", weekly_allowance_min=180, raw_dir=tmp_path / "raw")
    base.update(over)
    return config.test_settings(**base)


def open_db(settings):
    conn = db.connect(settings)
    db.init_db(conn)
    db.migrate(conn)
    return conn


def label_source(conn, clk, kind, key, label, display=None):
    """Create (if needed) and label a source. label=None leaves it unlabelled."""
    source = labels.ensure_source(conn, clk, kind, key, display)
    conn.commit()
    if label is not None:
        source = labels.set_source_label(conn, clk, source.id, label)
    return source


def spotify_item(conn, clk, settings, sid, *, show="A Show", minutes=60, state="queued",
                 override=None):
    db.upsert_episode(conn, sid, f"Episode {sid[:4]}", show, None, None, minutes * MIN)
    item = db.capture(conn, clk, settings, spotify_id=sid, why_note="because")
    if override is not None:
        labels.set_item_override(conn, clk, T.item_ref("spotify", sid), override)
        conn.commit()
    if state == "queued" and item.state == "locked":
        item = db.set_state(conn, clk, item.id, "queued")
    return item


def youtube_item(conn, clk, settings, video_id, *, channel="UCchannel0000000000000", channel_title="Chan",
                 minutes=60, state="queued", override=None):
    key = T.queue_key_for_youtube(video_id)
    db.upsert_episode(conn, key, f"Video {video_id}", channel_title, None, None, minutes * MIN,
                      platform="youtube", source_key=channel)
    item = db.capture(conn, clk, settings, spotify_id=key, why_note="because",
                      platform="youtube", source_key=channel)
    if override is not None:
        labels.set_item_override(conn, clk, key, override)
        conn.commit()
    if state == "queued" and item.state == "locked":
        item = db.set_state(conn, clk, item.id, "queued")
    return item


def listen(conn, clk, sid, minutes):
    """Add `minutes` of clamped listening at clk.now()."""
    prev = conn.execute(
        "SELECT position_ms FROM listening WHERE spotify_id = ? ORDER BY polled_at DESC, id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    position = (int(prev["position_ms"]) if prev else 0) + minutes * MIN
    db.record_listening(conn, clk, sid, position, minutes * MIN, False)


def measure(conn, clk, settings, *, kind, key, day, minutes, device=None, provenance=None,
            status="ok", item_key="", display=None, period_end=None, item_title=None):
    """Write one measurement through evidence, as a source module would."""
    source = labels.ensure_source(conn, clk, kind, key, display)
    conn.commit()
    if provenance is None:
        provenance = {T.KIND_YOUTUBE_CHANNEL: T.PROV_PAGE, T.KIND_IPHONE_APP: T.PROV_SCREENSHOT,
                      T.KIND_WEB_DOMAIN: T.PROV_EXPORT}[kind]
    if device is None:
        device = {T.PROV_PAGE: T.DEVICE_ACCOUNT, T.PROV_SCREENSHOT: T.DEVICE_IPHONE,
                  T.PROV_EXPORT: T.DEVICE_DESKTOP}[provenance]
    end = period_end or day
    rec = evidence.begin_import(conn, clk, kind=PROVENANCE_KIND[provenance], parser_version="test-1")
    evidence.upsert_measurement(
        conn, rec, source_id=source.id, device=device, provenance=provenance,
        period_start=day, period_end=end, minutes=minutes, status=status,
        item_key=item_key, item_title=item_title,
    )
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=end)
    return labels.get_source(conn, source.id)


def week_row(conn, week_start):
    row = conn.execute("SELECT * FROM weeks WHERE week_start = ?", (week_start,)).fetchone()
    return db._to_week(row) if row else None


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
    return clock.FrozenClock(CAPTURED)


# ==========================================================================
# The label x gate matrix
# ==========================================================================

OVERRIDES = [T.WORTHWHILE, T.WASTEFUL, None]
SOURCE_LABELS = [T.WORTHWHILE, T.WASTEFUL, None]
PLATFORMS = ["spotify", "youtube"]
MATRIX = [(o, s, p) for o in OVERRIDES for s in SOURCE_LABELS for p in PLATFORMS]

SID = "MatrixEpisode000000000"
VID = "Mtrx_Video1"
CHANNEL = "UCmatrixChannel0000000"


def expected(override, source_label):
    if override is not None:
        return override, T.BASIS_ITEM_OVERRIDE
    if source_label is not None:
        return source_label, T.BASIS_SOURCE_LABEL
    return T.WASTEFUL, T.BASIS_DEFAULT


def matrix_item(conn, clk, settings, override, source_label, platform, *, state="queued", minutes=60):
    if platform == "spotify":
        label_source(conn, clk, T.KIND_PODCAST_SHOW, "Matrix Show", source_label)
        return spotify_item(conn, clk, settings, SID, show="Matrix Show", minutes=minutes,
                            state=state, override=override)
    label_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, CHANNEL, source_label, "Matrix Channel")
    return youtube_item(conn, clk, settings, VID, channel=CHANNEL, channel_title="Matrix Channel",
                        minutes=minutes, state=state, override=override)


def consume(conn, clk, settings, item, platform, minutes):
    """Consumption of the matrix item inside the current allowance week."""
    if platform == "spotify":
        listen(conn, clk, item.spotify_id, minutes)
    else:
        day = T.iso_date(T.local_date(clk.now(), settings.tz))
        measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key=CHANNEL, day=day,
                minutes=float(minutes), item_key=VID)


@pytest.mark.parametrize("override,source_label,platform", MATRIX)
def test_item_label_follows_override_then_source_then_default(conn, clk, settings,
                                                              override, source_label, platform):
    item = matrix_item(conn, clk, settings, override, source_label, platform)
    resolved = allowance.item_label(conn, db.get_item(conn, item.id))
    assert (resolved.label, resolved.basis) == expected(override, source_label)


@pytest.mark.parametrize("override,source_label,platform", MATRIX)
def test_promotion_cost_is_zero_only_for_worthwhile(conn, clk, settings,
                                                    override, source_label, platform):
    item = matrix_item(conn, clk, settings, override, source_label, platform, minutes=60)
    cost, resolved = allowance.promotion_cost(conn, db.get_item(conn, item.id))
    label, _ = expected(override, source_label)
    assert resolved.label == label
    assert cost == (0 if label == T.WORTHWHILE else 60)


@pytest.mark.parametrize("override,source_label,platform", MATRIX)
def test_refusal_applies_to_wasteful_only(conn, clk, settings, override, source_label, platform):
    item = matrix_item(conn, clk, settings, override, source_label, platform, minutes=60)
    clk.set(WED)
    db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET promoted_min = 150 WHERE week_start = ?", (WEEK,))  # 30 left
    conn.commit()
    label, _ = expected(override, source_label)

    if label == T.WORTHWHILE:
        promoted = db.promote(conn, clk, settings, item.id)
        assert promoted.state == "promoted"
        assert week_row(conn, WEEK).promoted_min == 150          # spends nothing
    else:
        with pytest.raises(db.AllowanceExceeded) as excinfo:
            db.promote(conn, clk, settings, item.id)
        assert excinfo.value.overage_min == 30
        assert db.get_item(conn, item.id).state == "queued"      # nothing written
        assert week_row(conn, WEEK).promoted_min == 150


@pytest.mark.parametrize("override,source_label,platform", MATRIX)
def test_the_lock_holds_for_every_label(conn, clk, settings, override, source_label, platform):
    clk.set(WED)
    item = matrix_item(conn, clk, settings, override, source_label, platform, state="locked")
    assert item.state == "locked"
    clk.advance(hours=1)
    with pytest.raises(db.IllegalTransition):
        db.promote(conn, clk, settings, item.id)
    assert db.get_item(conn, item.id).state == "locked"
    assert db.get_item(conn, item.id).promoted_at is None


@pytest.mark.parametrize("override,source_label,platform", MATRIX)
def test_overage_comes_only_from_wasteful_consumption(conn, clk, settings,
                                                      override, source_label, platform):
    item = matrix_item(conn, clk, settings, override, source_label, platform, minutes=300)
    clk.set(WED)
    db.current_week(conn, clk, settings)
    consume(conn, clk, settings, item, platform, 240)

    clk.set(NEXT_WEEK)
    closed, opened = db.roll_over_week(conn, clk, settings)
    label, _ = expected(override, source_label)
    assert closed.week_start == WEEK
    assert closed.overage_min == (60 if label == T.WASTEFUL else 0)
    assert week_row(conn, WEEK).overage_min == closed.overage_min      # persisted, not just returned
    # Recorded, never carried (DR5.2): the next week starts whole for every label.
    assert opened.debt_min == 0
    assert opened.effective_allowance_min == settings.weekly_allowance_min


@pytest.mark.parametrize("override,source_label,platform", MATRIX)
def test_alerts_come_only_from_wasteful_consumption(conn, clk, settings,
                                                    override, source_label, platform):
    item = matrix_item(conn, clk, settings, override, source_label, platform, minutes=300)
    clk.set(WED)
    db.current_week(conn, clk, settings)
    consume(conn, clk, settings, item, platform, 200)

    bot = FakeBot()
    poll.check_alerts(conn, clk, settings, bot)
    label, _ = expected(override, source_label)
    if label == T.WASTEFUL:
        assert len(bot.texts) == 2
        assert any("Over the allowance: 200 wasteful content minutes used across sources" in t for t in bot.texts)
    else:
        assert bot.texts == []


# ==========================================================================
# Pooling across sources
# ==========================================================================

def test_pooled_minutes_across_every_source_in_one_week(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    ep = spotify_item(conn, clk, settings, "PooledEpisode000000000", show="Pod")
    listen(conn, clk, ep.spotify_id, 30)                                           # podcasts, wasteful

    label_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, "UCgood", T.WORTHWHILE, "Good")
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCbad", day="2026-09-07",
            minutes=20.0, item_key="aaaaaaaaaaa")                                  # youtube, wasteful
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCgood", day="2026-09-08",
            minutes=10.0, item_key="bbbbbbbbbbb")                                  # youtube, worthwhile
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCbad", day="2026-09-08",
            minutes=None, status="progress_missing", item_key="ccccccccccc")       # unmeasured
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-06",
            period_end="2026-09-12", minutes=50.0)                                 # iphone, wasteful
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="X", day="2026-09-06",
            period_end="2026-09-12", minutes=25.0)                                 # x (pre-seeded)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="YouTube", day="2026-09-06",
            period_end="2026-09-12", minutes=40.0)                                 # covered
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-09",
            minutes=15.0)                                                          # desktop, wasteful
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="x.com", day="2026-09-09",
            minutes=5.0)                                                           # x
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="youtube.com", day="2026-09-10",
            minutes=12.0)                                                          # covered

    pooled = allowance.pooled_minutes(conn, settings, WEEK)

    assert pooled.wasteful_min == pytest.approx(30 + 20 + 50 + 25 + 15 + 5)
    assert pooled.worthwhile_min == pytest.approx(10)
    assert pooled.covered_excluded_min == pytest.approx(52)
    assert pooled.unmeasured_items == 1
    g = pooled.by_group
    assert g[T.GROUP_PODCASTS].wasteful_min == pytest.approx(30)
    assert (g[T.GROUP_YOUTUBE].wasteful_min, g[T.GROUP_YOUTUBE].worthwhile_min) == (20, 10)
    assert g[T.GROUP_YOUTUBE].unmeasured_items == 1
    assert g[T.GROUP_IPHONE].wasteful_min == pytest.approx(50)
    assert g[T.GROUP_IPHONE].covered_excluded_min == pytest.approx(40)
    assert g[T.GROUP_X].wasteful_min == pytest.approx(30)
    assert g[T.GROUP_DESKTOP].wasteful_min == pytest.approx(15)
    assert g[T.GROUP_DESKTOP].covered_excluded_min == pytest.approx(12)

    # And the allowance reads it: 145 wasteful against 180 is "approaching".
    bot = FakeBot()
    poll.check_alerts(conn, clk, settings, bot)
    assert bot.texts == ["145 of 180 wasteful content minutes used this week across sources. 35 min left."]


def test_covered_elsewhere_rows_never_reach_allowance_or_overage(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="YouTube", day="2026-09-06",
            period_end="2026-09-12", minutes=900.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="open.spotify.com", day="2026-09-08",
            minutes=500.0)
    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    assert pooled.wasteful_min == 0 and pooled.worthwhile_min == 0
    assert pooled.covered_excluded_min == pytest.approx(1400)

    bot = FakeBot()
    poll.check_alerts(conn, clk, settings, bot)
    assert bot.texts == []
    clk.set(NEXT_WEEK)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.overage_min == 0
    assert opened.debt_min == 0


def test_worthwhile_consumption_is_counted_but_never_charged(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    label_source(conn, clk, T.KIND_IPHONE_APP, "Kindle", T.WORTHWHILE)
    label_source(conn, clk, T.KIND_PODCAST_SHOW, "Lectures", T.WORTHWHILE)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Kindle", day="2026-09-06",
            period_end="2026-09-12", minutes=500.0)
    ep = spotify_item(conn, clk, settings, "LectureEpisode00000000", show="Lectures", minutes=400)
    listen(conn, clk, ep.spotify_id, 300)

    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    assert pooled.wasteful_min == 0
    assert pooled.worthwhile_min == pytest.approx(800)

    bot = FakeBot()
    poll.check_alerts(conn, clk, settings, bot)
    assert bot.texts == []
    clk.set(NEXT_WEEK)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.overage_min == 0
    assert opened.debt_min == 0
    # POP's listened_min keeps its meaning: raw Spotify minutes, all labels.
    assert week_row(conn, WEEK).listened_min == 300


def test_unmeasured_rows_are_never_zero_and_never_minutes(conn, clk, settings):
    clk.set(WED)
    for status in ("progress_missing", "duration_missing", "unreadable"):
        measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCx", day="2026-09-07",
                minutes=None, status=status, item_key=f"{status[:11]:_<11}")
    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    assert pooled.wasteful_min == 0
    assert pooled.unmeasured_items == 3


# ==========================================================================
# Week attribution
# ==========================================================================

def test_a_sunday_before_1800_day_is_charged_to_the_week_opening_that_evening(conn, clk, settings):
    """Days are whole: Sunday's measurements go to the week opening at 18:00 that
    Sunday (DR1.7), while a Spotify poll at Sunday noon stays instant-attributed."""
    clk.set("2026-09-13T10:00:00+00:00")       # Sunday 12:00 Warsaw
    ep = spotify_item(conn, clk, settings, "SundayEpisode000000000", show="Pod")
    listen(conn, clk, ep.spotify_id, 40)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-13", minutes=25.0)

    before = allowance.pooled_minutes(conn, settings, WEEK)
    after = allowance.pooled_minutes(conn, settings, NEXT_WEEK)
    assert before.wasteful_min == pytest.approx(40)      # Spotify: the instant
    assert after.wasteful_min == pytest.approx(25)       # the day: next allowance week
    assert T.allowance_week_start_for_day(date(2026, 9, 13), settings.tz) == NEXT_WEEK


def test_week_attribution_across_the_dst_boundary(conn, clk, settings):
    """Warsaw leaves DST on Sunday 25 Oct 2026: that evening's 18:00 is 17:00Z, not 16:00Z."""
    before_dst = "2026-10-18T16:00:00+00:00"
    after_dst = "2026-10-25T17:00:00+00:00"
    clk.set("2026-10-20T10:00:00+00:00")
    ep = spotify_item(conn, clk, settings, "DstEpisode000000000000", show="Pod", minutes=600)

    clk.set("2026-10-25T16:30:00+00:00")       # 17:30 CET: still the old week
    listen(conn, clk, ep.spotify_id, 10)
    clk.set("2026-10-25T17:00:00+00:00")       # 18:00 CET: the new week
    listen(conn, clk, ep.spotify_id, 20)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-10-24", minutes=3.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-10-25", minutes=7.0)

    assert allowance.week_end(settings, before_dst) == after_dst
    assert allowance.pooled_minutes(conn, settings, before_dst).wasteful_min == pytest.approx(13)
    assert allowance.pooled_minutes(conn, settings, after_dst).wasteful_min == pytest.approx(27)


def test_rollover_overage_across_the_dst_boundary(conn, clk, settings):
    clk.set("2026-10-20T10:00:00+00:00")
    db.current_week(conn, clk, settings)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-10-18",
            period_end="2026-10-24", minutes=250.0)
    clk.set("2026-10-25T17:00:00+00:00")
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.week_start == "2026-10-18T16:00:00+00:00"
    assert opened.week_start == "2026-10-25T17:00:00+00:00"
    assert closed.overage_min == 70                        # 250 - 180, on the closed week
    assert opened.debt_min == 0
    assert opened.effective_allowance_min == 180


# ==========================================================================
# Overage: recorded, never carried (DECISIONS DR5.2)
# ==========================================================================

def test_overage_for_is_unclamped_and_floors_at_zero():
    week = db.Week(WEEK, allowance_min=180, debt_min=0, promoted_min=0, listened_min=0)
    pooled = lambda m: allowance.PooledMinutes(wasteful_min=m, worthwhile_min=0.0)  # noqa: E731
    assert allowance.overage_for(week, pooled(100)) == 0
    assert allowance.overage_for(week, pooled(180)) == 0
    assert allowance.overage_for(week, pooled(240)) == 60
    assert allowance.overage_for(week, pooled(5000)) == 4820          # no clamp at the allowance
    # Judged against the week's own allowance, never an "effective" one.
    legacy = db.Week(WEEK, allowance_min=180, debt_min=60, promoted_min=0, listened_min=0)
    assert allowance.overage_for(legacy, pooled(150)) == 0
    assert allowance.overage_for(legacy, pooled(240)) == 60


def test_debt_for_is_gone():
    assert not hasattr(allowance, "debt_for")


def test_whole_minutes_matches_pops_rounding():
    for ms in (0, 1, 29_999, 30_000, 30_001, 89_999, 90_000, 90_001, 7 * MIN + 12_345):
        assert allowance.whole_minutes(ms / MIN) == db.ms_to_min(ms)


def test_pooled_overage_is_recorded_unclamped_at_rollover(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-09-06",
            period_end="2026-09-12", minutes=2000.0)
    clk.set(NEXT_WEEK)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.overage_min == 1820                        # 2000 - 180, not clamped at 180
    assert opened.debt_min == 0
    assert opened.effective_allowance_min == 180             # a blown week never zeroes the next


def test_a_late_import_re_records_overage(conn, clk, settings):
    """Screen Time arrives Monday, after Sunday's rollover recorded an overage of 0."""
    clk.set(WED)
    db.current_week(conn, clk, settings)
    ep = spotify_item(conn, clk, settings, "LateEpisode00000000000", show="Pod", minutes=300)
    listen(conn, clk, ep.spotify_id, 150)

    clk.set(NEXT_WEEK)
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.overage_min == 0
    assert opened.debt_min == 0

    clk.set("2026-09-14T08:00:00+00:00")        # Monday: last week's screenshot arrives
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-06",
            period_end="2026-09-12", minutes=100.0)
    assert week_row(conn, WEEK).overage_min == 70            # 250 - 180, on the week it happened
    assert week_row(conn, NEXT_WEEK).debt_min == 0           # nothing carried
    assert week_row(conn, NEXT_WEEK).overage_min == 0        # the current week, so far
    assert week_row(conn, NEXT_WEEK).effective_allowance_min == 180


def test_a_late_import_never_cascades_through_later_weeks(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    clk.set(NEXT_WEEK)
    db.roll_over_week(conn, clk, settings)
    clk.set("2026-09-20T16:00:00+00:00")
    db.roll_over_week(conn, clk, settings)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-13",
            period_end="2026-09-19", minutes=150.0)                # week 2: 150 vs 180, not over
    assert week_row(conn, NEXT_WEEK).overage_min == 0

    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-09-06",
            period_end="2026-09-12", minutes=280.0)                # week 1 blown by 100
    assert week_row(conn, WEEK).overage_min == 100
    # Week 1's overage does not shrink week 2: 150 against its full 180 is still not over.
    assert week_row(conn, NEXT_WEEK).overage_min == 0
    assert week_row(conn, NEXT_WEEK).debt_min == 0
    assert week_row(conn, NEXT_WEEK).effective_allowance_min == 180
    assert week_row(conn, "2026-09-20T16:00:00+00:00").debt_min == 0
    assert week_row(conn, "2026-09-20T16:00:00+00:00").effective_allowance_min == 180


def test_overage_is_judged_per_week_independently(conn, clk, settings):
    """Two weeks both over: each records its own full overage; neither reduces the other."""
    clk.set(WED)
    db.current_week(conn, clk, settings)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-09-06",
            period_end="2026-09-12", minutes=300.0)                # week 1: 120 over
    clk.set(NEXT_WEEK)
    db.roll_over_week(conn, clk, settings)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-09-13",
            period_end="2026-09-19", minutes=200.0)                # week 2: 20 over its own 180
    clk.set("2026-09-20T16:00:00+00:00")
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.week_start == NEXT_WEEK
    assert closed.overage_min == 20                                # not 140 (compounded)
    assert week_row(conn, WEEK).overage_min == 120
    assert opened.debt_min == 0 and opened.effective_allowance_min == 180


def test_on_measurements_changed_records_overage_and_zeroes_legacy_debt(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="TikTok", day="2026-09-06",
            period_end="2026-09-12", minutes=240.0)
    clk.set(NEXT_WEEK)
    db.roll_over_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET debt_min = 45, overage_min = NULL")   # stale state
    conn.commit()
    allowance.on_measurements_changed(conn, clk, settings)
    assert week_row(conn, WEEK).overage_min == 60
    assert week_row(conn, NEXT_WEEK).overage_min == 0
    assert [r["debt_min"] for r in conn.execute("SELECT debt_min FROM weeks")] == [0, 0]


def test_on_measurements_changed_is_idempotent_and_never_touches_allowances(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    clk.set(NEXT_WEEK)
    db.roll_over_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET allowance_min = 360 WHERE week_start = ?", (NEXT_WEEK,))
    conn.commit()
    allowances = [(r["week_start"], r["allowance_min"])
                  for r in conn.execute("SELECT * FROM weeks ORDER BY week_start")]
    allowance.on_measurements_changed(conn, clk, settings)      # first run records overage
    before = conn.execute("SELECT * FROM weeks ORDER BY week_start").fetchall()
    allowance.on_measurements_changed(conn, clk, settings)
    after = conn.execute("SELECT * FROM weeks ORDER BY week_start").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert [(r["week_start"], r["allowance_min"]) for r in after] == allowances
    assert all(r["overage_min"] is not None for r in after)


# ==========================================================================
# Gated versus ungated
# ==========================================================================

def test_spotify_consumption_is_gated_only_after_promotion(conn, clk, settings):
    item = spotify_item(conn, clk, settings, "GatedEpisode0000000000", show="Pod", minutes=150)
    clk.set("2026-09-07T08:00:00+00:00")
    db.current_week(conn, clk, settings)
    listen(conn, clk, item.spotify_id, 20)                      # before promotion: ungated
    clk.set("2026-09-08T08:00:00+00:00")
    db.promote(conn, clk, settings, item.id)
    listen(conn, clk, item.spotify_id, 30)                      # same instant: gated
    clk.set("2026-09-09T08:00:00+00:00")
    listen(conn, clk, item.spotify_id, 15)                      # after: gated

    split = allowance.gated_split(conn, settings, WEEK)[T.GROUP_PODCASTS]
    assert split.gated_min == pytest.approx(45)
    assert split.ungated_min == pytest.approx(20)
    assert split.wasteful_ungated_min == pytest.approx(20)


def test_youtube_consumption_is_gated_from_the_promotion_day(conn, clk, settings):
    item = youtube_item(conn, clk, settings, "GatedVideo1", channel="UCgated", minutes=90)
    clk.set("2026-09-08T08:00:00+00:00")                        # Tuesday
    db.promote(conn, clk, settings, item.id)
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCgated", day="2026-09-07",
            minutes=10.0, item_key="GatedVideo1")               # Monday: before
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCgated", day="2026-09-08",
            minutes=25.0, item_key="GatedVideo1")               # the promotion day
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCgated", day="2026-09-09",
            minutes=5.0, item_key="OtherVideo1")                # never captured

    split = allowance.gated_split(conn, settings, WEEK)[T.GROUP_YOUTUBE]
    assert split.gated_min == pytest.approx(25)
    assert split.ungated_min == pytest.approx(15)


def test_screen_time_and_cold_turkey_are_always_ungated(conn, clk, settings):
    clk.set(WED)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, "arxiv.org", T.WORTHWHILE)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-06",
            period_end="2026-09-12", minutes=60.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="arxiv.org", day="2026-09-08", minutes=40.0)
    split = allowance.gated_split(conn, settings, WEEK)
    assert split[T.GROUP_IPHONE] == allowance.GatedSplit(0.0, 60.0, 60.0)
    assert split[T.GROUP_DESKTOP] == allowance.GatedSplit(0.0, 40.0, 0.0)   # worthwhile: not wasteful


# ==========================================================================
# Labels are never cached
# ==========================================================================

def test_relabelling_a_source_changes_the_next_read_immediately(conn, clk, settings):
    item = spotify_item(conn, clk, settings, "RelabelEpisode00000000", show="Flip", minutes=60)
    source = label_source(conn, clk, T.KIND_PODCAST_SHOW, "Flip", T.WASTEFUL)
    assert allowance.promotion_cost(conn, item)[0] == 60
    labels.set_source_label(conn, clk, source.id, T.WORTHWHILE)
    assert allowance.promotion_cost(conn, item)[0] == 0


def test_reads_never_create_sources(conn, clk, settings):
    clk.set(WED)
    db.upsert_episode(conn, "NoSourceEpisode0000000", "t", "Brand New Show", None, None, MIN)
    db.record_listening(conn, clk, "NoSourceEpisode0000000", MIN, MIN, False)
    before = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    allowance.pooled_minutes(conn, settings, WEEK)
    allowance.gated_split(conn, settings, WEEK)
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == before


def test_ensure_item_source_records_identity_without_a_label(conn, clk, settings):
    yt = youtube_item(conn, clk, settings, "IdentityVid", channel="UCident", channel_title="Ident")
    ep = spotify_item(conn, clk, settings, "IdentityEpisode0000000", show="  Ident  Show ")
    a = allowance.ensure_item_source(conn, clk, db.get_item(conn, yt.id))
    b = allowance.ensure_item_source(conn, clk, db.get_item(conn, ep.id))
    assert (a.kind, a.source_key, a.label) == (T.KIND_YOUTUBE_CHANNEL, "UCident", None)
    assert (b.kind, b.source_key, b.label) == (T.KIND_PODCAST_SHOW, "ident show", None)


# ==========================================================================
# Tool: not content, never charged, shown separately (DECISIONS DR5.5)
# ==========================================================================

TOOL_MATRIX = [(o, p) for o in OVERRIDES for p in PLATFORMS]


def expected_with_tool_source(override):
    """An override beats a tool source; otherwise the source's tool label stands."""
    return (override, T.BASIS_ITEM_OVERRIDE) if override is not None else (T.TOOL, T.BASIS_SOURCE_LABEL)


@pytest.mark.parametrize("override,platform", TOOL_MATRIX)
def test_tool_source_item_label_cost_and_refusal(conn, clk, settings, override, platform):
    item = matrix_item(conn, clk, settings, override, T.TOOL, platform, minutes=60)
    label, basis = expected_with_tool_source(override)
    resolved = allowance.item_label(conn, db.get_item(conn, item.id))
    assert (resolved.label, resolved.basis) == (label, basis)
    cost, _ = allowance.promotion_cost(conn, db.get_item(conn, item.id))
    assert cost == (60 if label == T.WASTEFUL else 0)

    clk.set(WED)
    db.current_week(conn, clk, settings)
    conn.execute("UPDATE weeks SET promoted_min = 150 WHERE week_start = ?", (WEEK,))  # 30 left
    conn.commit()
    if label == T.WASTEFUL:
        with pytest.raises(db.AllowanceExceeded):
            db.promote(conn, clk, settings, item.id)
        assert db.get_item(conn, item.id).state == "queued"
    else:
        assert db.promote(conn, clk, settings, item.id).state == "promoted"
        assert week_row(conn, WEEK).promoted_min == 150          # spends nothing


@pytest.mark.parametrize("override,platform", TOOL_MATRIX)
def test_tool_source_consumption_charges_nothing_unless_overridden_wasteful(conn, clk, settings,
                                                                          override, platform):
    item = matrix_item(conn, clk, settings, override, T.TOOL, platform, minutes=300)
    clk.set(WED)
    db.current_week(conn, clk, settings)
    consume(conn, clk, settings, item, platform, 240)
    label, _ = expected_with_tool_source(override)

    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    assert pooled.wasteful_min == pytest.approx(240 if label == T.WASTEFUL else 0)
    assert pooled.worthwhile_min == pytest.approx(240 if label == T.WORTHWHILE else 0)
    assert pooled.tool_min == pytest.approx(240 if label == T.TOOL else 0)
    split = allowance.gated_split(conn, settings, WEEK)[T.GROUP_PODCASTS if platform == "spotify" else T.GROUP_YOUTUBE]
    assert split.ungated_min == pytest.approx(0 if label == T.TOOL else 240)

    bot = FakeBot()
    poll.check_alerts(conn, clk, settings, bot)
    assert (len(bot.texts) == 2) == (label == T.WASTEFUL)
    clk.set(NEXT_WEEK)
    closed, _ = db.roll_over_week(conn, clk, settings)
    assert closed.overage_min == (60 if label == T.WASTEFUL else 0)


def test_tool_minutes_are_pooled_apart_from_wasteful_and_worthwhile(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, "amazon.pl", T.TOOL)
    label_source(conn, clk, T.KIND_IPHONE_APP, "Mail", T.TOOL)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="amazon.pl", day="2026-09-08", minutes=70.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-08", minutes=20.0)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Mail", day="2026-09-06",
            period_end="2026-09-12", minutes=30.0)                         # OCR, tool
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-06",
            period_end="2026-09-12", minutes=10.0)                         # OCR, wasteful

    pooled = allowance.pooled_minutes(conn, settings, WEEK)
    assert pooled.wasteful_min == pytest.approx(30)
    assert pooled.worthwhile_min == 0
    assert pooled.tool_min == pytest.approx(100)
    assert pooled.by_group[T.GROUP_DESKTOP] == allowance.GroupMinutes(20.0, 0.0, 0.0, 0, 70.0)
    assert pooled.by_group[T.GROUP_IPHONE] == allowance.GroupMinutes(10.0, 0.0, 0.0, 0, 30.0)
    # The OCR share describes content minutes only.
    assert (pooled.screenshot_ocr_min, pooled.screenshot_ocr_wasteful_min) == (10.0, 10.0)

    split = allowance.gated_split(conn, settings, WEEK)
    assert split[T.GROUP_DESKTOP] == allowance.GatedSplit(0.0, 20.0, 20.0)
    assert split[T.GROUP_IPHONE] == allowance.GatedSplit(0.0, 10.0, 10.0)


def test_relabelling_a_source_tool_re_records_overage(conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    src = measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="amazon.pl", day="2026-09-08", minutes=200.0)
    allowance.on_measurements_changed(conn, clk, settings)
    assert week_row(conn, WEEK).overage_min == 20                   # unlabelled -> wasteful: 200 vs 180
    labels.set_source_label(conn, clk, src.id, T.TOOL)
    allowance.on_measurements_changed(conn, clk, settings)
    assert week_row(conn, WEEK).overage_min == 0
    labels.set_source_label(conn, clk, src.id, T.WASTEFUL)          # browsed it for entertainment after all
    allowance.on_measurements_changed(conn, clk, settings)
    assert week_row(conn, WEEK).overage_min == 20
