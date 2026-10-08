"""The UX overhaul: what needs the user, where numbers come from, and fixing OCR by hand.

DECISIONS DR5.6. The user's complaint was that dr-brainrot never told him what it
wanted from him — "I don't know how and when I'm supposed to be submitting
sources like X desktop data, desktop web, any of it" — and that Telegram was
the wrong place to label content. These tests pin the data behind the answer:
`report.needs_attention`, `report.provenance_map`, the dashboard label queue,
`report.needs_your_eye` with `screentime.correct_measurement`, the check-in
wording, and `jobs.run_sync`.

Nothing here renders anything: the dashboard session owns the templates.
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import date

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import allowance  # noqa: E402
import checklist  # noqa: E402
import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import dr_brainrot_types as T  # noqa: E402
import evidence  # noqa: E402
import jobs  # noqa: E402
import labels  # noqa: E402
import report  # noqa: E402
from sources import screentime  # noqa: E402
from tests.test_screentime import edit, fake_ocr, fake_region, images, load  # noqa: E402

TZ = "Europe/Warsaw"
#: Wednesday 16 Sep 2026. Last complete measurement week: Sun 6 .. Sat 12 Sep.
NOW = "2026-09-16T10:00:00+00:00"


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", db_path=tmp_path / "dr-brainrot.db",
                                telegram_user_id=1, go_live="2026-09-06")


@pytest.fixture
def clk():
    return clock.FrozenClock(NOW)


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


def measure(conn, clk, settings, *, kind, key, minutes, day="2026-09-07", end=None,
            provenance=T.PROV_SCREENSHOT, device=T.DEVICE_IPHONE, import_kind="screentime",
            received_at=None):
    """One measurement through the real evidence path, so imports/periods are real."""
    source = labels.ensure_source(conn, clk, kind, key)
    rec = evidence.begin_import(conn, clk, kind=import_kind, parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=source.id, device=device, provenance=provenance,
                                period_start=day, period_end=end or day, minutes=minutes)
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=end or day)
    if received_at is not None:
        conn.execute("UPDATE imports SET received_at = ? WHERE id = ?", (received_at, rec.id))
        conn.commit()
    return source


def row_for(rows, group):
    return next(r for r in rows if r.group == group)


# ==========================================================================
# needs_attention
# ==========================================================================

def test_needs_attention_covers_every_source_group_once(conn, clk, settings):
    rows = report.needs_attention(conn, clk, settings)
    assert {r.group for r in rows} == {T.GROUP_PODCASTS, T.GROUP_YOUTUBE, T.GROUP_IPHONE,
                                       T.GROUP_DESKTOP, T.GROUP_DESKTOP_APPS, T.GROUP_AUDIOBOOKS,
                                       T.GROUP_VAULT}
    assert len(rows) == 7
    assert row_for(rows, T.GROUP_IPHONE).label == "iPhone apps"
    assert row_for(rows, T.GROUP_DESKTOP).label == "Desktop web"


def test_a_manual_week_that_arrived_is_ok_and_asks_for_nothing(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Chatterbox", minutes=38.0,
            day="2026-09-06", end="2026-09-12")
    row = row_for(report.needs_attention(conn, clk, settings), T.GROUP_IPHONE)
    assert (row.mode, row.state, row.action_text) == (report.MODE_MANUAL, report.STATE_OK, "")
    assert "2026-09-06 to 2026-09-12" in row.detail
    assert row.last_data_at is not None


def test_a_manual_week_that_never_arrived_asks_for_it_with_the_steps(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="news.example", minutes=10.0,
            provenance=T.PROV_EXPORT, device=T.DEVICE_DESKTOP, import_kind="coldturkey")
    row = row_for(report.needs_attention(conn, clk, settings), T.GROUP_IPHONE)
    assert (row.mode, row.state) == (report.MODE_MANUAL, report.STATE_MISSING)
    assert "Show More" in row.action_text and "one go" in row.action_text
    assert row.covered is False


def test_a_manual_week_is_owed_from_the_sunday_it_closes(conn, clk, settings):
    """The Sunday check-in asks for it that morning, so the page says so the same day (DR5.24)."""
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Chatterbox", minutes=5.0)
    clk.set("2026-09-12T09:00:00+00:00")             # Saturday 12 Sep: last week ended before go-live
    row = row_for(report.needs_attention(conn, clk, settings), T.GROUP_YOUTUBE)
    assert (row.state, row.action_text) == (report.STATE_OK, "")
    clk.set("2026-09-13T07:00:00+00:00")             # Sunday 13 Sep, 09:00 Warsaw: 6-12 Sep is owed
    row = row_for(report.needs_attention(conn, clk, settings), T.GROUP_YOUTUBE)
    assert (row.state, row.action_text) == (report.STATE_MISSING, report.YOUTUBE_ACTION)
    assert "2026-09-06 to 2026-09-12" in row.detail


def test_qbserve_that_stopped_recording_is_stale_with_one_thing_to_do(conn, tmp_path, clk, settings):
    configured = config.test_settings(**{**settings.__dict__, "qbserve_db": tmp_path / "qbserve.db"})
    measure(conn, clk, settings, kind=T.KIND_DESKTOP_APP, key="com.example.app", minutes=42.0,
            provenance=T.PROV_APP_TRACKER, device=T.DEVICE_DESKTOP, import_kind="qbserve")
    imp = conn.execute("SELECT id FROM imports WHERE kind = 'qbserve'").fetchone()["id"]
    conn.execute("UPDATE imports SET meta = ? WHERE id = ?",
                 (json.dumps({"latest_record_at": "2026-09-14T08:00:00+00:00"}), imp))
    conn.commit()
    row = row_for(report.needs_attention(conn, clk, configured), T.GROUP_DESKTOP_APPS)
    assert (row.mode, row.state) == (report.MODE_AUTOMATIC, report.STATE_STALE)
    assert row.detail == "The read is current but Qbserve has recorded nothing since Mon 14 Sep 10:00."
    assert row.action_text == report.QBSERVE_ACTION and "check it's recording" in row.action_text


def test_qbserve_unconfigured_says_desktop_apps_are_unmeasured_not_zero(conn, clk, settings):
    row = row_for(report.needs_attention(conn, clk, settings), T.GROUP_DESKTOP_APPS)
    assert (row.state, row.last_data_at) == (report.STATE_MISSING, None)
    assert "unmeasured, not zero" in row.detail


def test_an_automatic_source_read_today_is_ok(conn, tmp_path, clk, settings):
    configured = config.test_settings(**{**settings.__dict__, "coldturkey_db": tmp_path / "ct.db"})
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="news.example", minutes=30.0,
            day="2026-09-15", provenance=T.PROV_EXPORT, device=T.DEVICE_DESKTOP, import_kind="coldturkey")
    row = row_for(report.needs_attention(conn, clk, configured), T.GROUP_DESKTOP)
    assert (row.mode, row.state, row.action_text) == (report.MODE_AUTOMATIC, report.STATE_OK, "")
    assert row.schedule_text == report.SCHEDULE_DAILY_JOB


def test_an_automatic_source_not_read_for_two_days_is_stale(conn, tmp_path, clk, settings):
    configured = config.test_settings(**{**settings.__dict__, "coldturkey_db": tmp_path / "ct.db"})
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="news.example", minutes=30.0,
            day="2026-09-13", provenance=T.PROV_EXPORT, device=T.DEVICE_DESKTOP, import_kind="coldturkey",
            received_at="2026-09-13T09:30:00+00:00")
    row = row_for(report.needs_attention(conn, clk, configured), T.GROUP_DESKTOP)
    assert row.state == report.STATE_STALE
    assert "unmeasured, not zero" in row.detail and "logs/jobs.log" in row.action_text


def test_rows_needing_the_user_sort_first(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Chatterbox", minutes=38.0,
            day="2026-09-06", end="2026-09-12")
    rows = report.needs_attention(conn, clk, settings)
    with_action = [r.group for r in rows if r.action_text]
    assert with_action and [r.group for r in rows][:len(with_action)] == with_action
    assert rows[-1].mode == report.MODE_AUTOMATIC


def test_nothing_is_asked_for_after_the_diagnostic_ends(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Chatterbox", minutes=5.0)
    clk.set("2026-12-01T10:00:00+00:00")
    rows = report.needs_attention(conn, clk, settings)
    assert [r.action_text for r in rows] == [""] * len(rows)
    assert all(report.DIAGNOSTIC_ENDED_NOTE in r.detail for r in rows)


# ==========================================================================
# provenance_map
# ==========================================================================

def test_provenance_map_names_both_x_paths_and_says_they_are_one_source(conn, clk, settings):
    rows = report.provenance_map(conn, clk, settings)
    x_rows = [r for r in rows if r.group == T.GROUP_X]
    tools = {r.tool for r in x_rows}
    assert any("Cold Turkey" in t for t in tools), "desktop x.com comes from Cold Turkey"
    assert any("Screen Time" in t for t in tools), "the X phone app comes from Screen Time"
    assert {r.provenance for r in x_rows} >= {T.PROV_EXPORT, T.PROV_SCREENSHOT}
    assert all(r.folds_into == T.GROUP_X for r in x_rows)
    assert all(report.X_FOLD_NOTE in r.limits for r in x_rows)
    assert "measured twice and counted once" in report.X_FOLD_NOTE


def test_provenance_map_reuses_the_cost_per_keeper_blind_spots(conn, clk, settings):
    rows = report.provenance_map(conn, clk, settings)
    iphone = row_for(rows, T.GROUP_IPHONE)
    assert iphone.limits == report.GROUP_BLIND_SPOTS[T.GROUP_IPHONE]
    assert iphone.provenance_text == T.PROVENANCE_TEXT[T.PROV_SCREENSHOT]


def test_provenance_map_follows_the_settings_that_change_who_does_the_work(conn, tmp_path, clk, settings):
    manual = row_for(report.provenance_map(conn, clk, settings), T.GROUP_DESKTOP)
    assert manual.mode == report.MODE_MANUAL
    configured = config.test_settings(**{**settings.__dict__, "coldturkey_db": tmp_path / "ct.db"})
    auto = row_for(report.provenance_map(conn, clk, configured), T.GROUP_DESKTOP)
    assert auto.mode == report.MODE_AUTOMATIC and auto.schedule_text == report.SCHEDULE_DAILY_JOB


def test_provenance_map_says_the_vault_is_counts_not_minutes(conn, clk, settings):
    vault = row_for(report.provenance_map(conn, clk, settings), T.GROUP_VAULT)
    assert "Never minutes" in vault.measures and vault.provenance == T.PROV_VAULT


# ==========================================================================
# the labelling queue
# ==========================================================================

def test_label_queue_is_ordered_by_minutes_biggest_decision_first(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Small", minutes=5.0)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Huge", minutes=500.0)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Middling", minutes=60.0)
    rows = labels.label_queue(conn, clk, settings)
    assert [r.display_name for r in rows] == ["Huge", "Middling", "Small"]
    assert [r.minutes for r in rows] == [500.0, 60.0, 5.0]
    assert rows[0].seen_in == ("Screen Time screenshots",)
    assert rows[0].group == T.GROUP_IPHONE
    assert labels.label_queue(conn, clk, settings, limit=2) == rows[:2]


def test_label_queue_excludes_covered_elsewhere_the_unknown_channel_and_the_unobserved(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Real", minutes=10.0)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="YouTube", minutes=99.0)      # covered elsewhere
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key=T.UNKNOWN_YOUTUBE_CHANNEL_KEY,
            minutes=99.0, provenance=T.PROV_PAGE, device=T.DEVICE_ACCOUNT, import_kind="youtube_history")
    labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "NeverSeen")
    conn.commit()
    assert [r.display_name for r in labels.label_queue(conn, clk, settings)] == ["Real"]


def test_label_queue_leaves_out_what_is_already_decided_including_tool(conn, clk, settings):
    a = measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Shop", minutes=200.0)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Feed", minutes=100.0)
    labels.set_source_label(conn, clk, a.id, T.TOOL)
    assert [r.display_name for r in labels.label_queue(conn, clk, settings)] == ["Feed"]


def test_label_queue_counts_podcast_minutes_from_listening(conn, clk, settings):
    db.upsert_episode(conn, "EpisodeAAAAAAAAAAAAAAA", "Ep", "Deep Science", None, None, 3_600_000)
    conn.execute("INSERT INTO listening (spotify_id, polled_at, position_ms, delta_ms, fully_played) "
                 "VALUES (?, ?, ?, ?, 0)", ("EpisodeAAAAAAAAAAAAAAA", NOW, 1_800_000, 1_800_000))
    labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Deep Science")
    conn.commit()
    (row,) = labels.label_queue(conn, clk, settings)
    assert (row.display_name, row.minutes, row.group) == ("Deep Science", 30.0, T.GROUP_PODCASTS)
    assert row.seen_in == ("Spotify polling",)


def test_unlabelled_summary_counts_minutes_and_names_the_biggest_group(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Feed", minutes=200.0)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Scroll", minutes=140.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="news.example", minutes=50.0,
            provenance=T.PROV_EXPORT, device=T.DEVICE_DESKTOP, import_kind="coldturkey")
    summary = labels.unlabelled_summary(conn, clk, settings)
    assert (summary.count, summary.minutes, summary.top_group) == (3, 390.0, T.GROUP_IPHONE)


def test_unlabelled_summary_is_empty_when_everything_is_decided(conn, clk, settings):
    source = measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Feed", minutes=200.0)
    labels.set_source_label(conn, clk, source.id, T.WASTEFUL)
    assert labels.unlabelled_summary(conn, clk, settings) == labels.Summary(0, 0.0, None)


# ==========================================================================
# bulk_set_label
# ==========================================================================

def test_bulk_set_label_labels_many_at_once_and_re_records_the_overage(conn, clk, settings):
    a = measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Feed", minutes=300.0)
    b = measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Scroll", minutes=200.0)
    week_start = T.allowance_week_start_for_day(date(2026, 9, 7), settings.tz)
    conn.execute("INSERT INTO weeks (week_start, allowance_min, debt_min, promoted_min, listened_min) "
                 "VALUES (?, 360, 0, 0, 0)", (week_start,))
    conn.commit()
    allowance.on_measurements_changed(conn, clk, settings)
    assert conn.execute("SELECT overage_min FROM weeks").fetchone()[0] == 140     # 500 unlabelled -> wasteful

    assert labels.bulk_set_label(conn, clk, [a.id, b.id], T.TOOL, settings=settings) == 2
    assert {labels.get_source(conn, i).label for i in (a.id, b.id)} == {T.TOOL}
    assert conn.execute("SELECT overage_min FROM weeks").fetchone()[0] == 0       # tool never charges


def test_bulk_set_label_validates_the_label_and_skips_ids_that_are_gone(conn, clk, settings):
    source = measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Feed", minutes=10.0)
    with pytest.raises(labels.LabelError):
        labels.bulk_set_label(conn, clk, [source.id], "neutral", settings=settings)
    assert labels.get_source(conn, source.id).label is None
    assert labels.bulk_set_label(conn, clk, [source.id, 424242], T.WASTEFUL, settings=settings) == 1
    assert labels.bulk_set_label(conn, clk, [], T.WASTEFUL, settings=settings) == 0


# ==========================================================================
# needs_your_eye + correct_measurement
# ==========================================================================

def screentime_with_a_conflict(conn, clk, settings):
    """The real parse, with Lionfish read as two different durations on two pages."""
    pages, recovery = load("last_week.json")
    return screentime.ingest_batch(
        conn, clk, settings, images(), received_at=NOW,
        ocr=fake_ocr(edit(pages, "2h 28m", "2h 29m", page=1)), region_ocr=fake_region(recovery))


def test_needs_your_eye_lists_the_conflicting_row_with_both_readings(conn, clk, settings):
    out = screentime_with_a_conflict(conn, clk, settings)
    assert out.status == "partial"
    rows = report.needs_your_eye(conn, clk, settings)
    lionfish = next(r for r in rows if r.source == "Lionfish")
    assert lionfish.status == "unreadable"
    assert lionfish.options_min == (148.0, 149.0)          # 2h 28m and 2h 29m, both from the evidence
    assert lionfish.suggested_min is None                  # never invented, never picked for the user
    assert any("2h 28m" in text for text in lionfish.what_was_read)
    assert "more than one duration" in lionfish.detail
    assert lionfish.group == T.GROUP_IPHONE and lionfish.day == "2026-09-06"


def test_correct_measurement_records_the_persons_figure_and_moves_the_totals(conn, clk, settings):
    screentime_with_a_conflict(conn, clk, settings)
    (eye,) = [r for r in report.needs_your_eye(conn, clk, settings) if r.source == "Lionfish"]
    week_start = T.allowance_week_start_for_day(date(2026, 9, 7), settings.tz)
    before = allowance.pooled_minutes(conn, settings, week_start).charged_min

    screentime.correct_measurement(conn, clk, settings, eye.measurement_id, 149.0)

    row = conn.execute("SELECT * FROM measurements WHERE id = ?", (eye.measurement_id,)).fetchone()
    assert (row["minutes"], row["status"]) == (149.0, "ok")
    detail = json.loads(row["detail"])
    assert detail["corrected_by_person"] is True
    assert detail["corrections"][0]["previous_status"] == "unreadable"
    assert detail["corrections"][0]["previous_minutes"] is None
    assert detail["raw"], "the original OCR reading is kept"
    assert row["provenance"] == T.PROV_SCREENSHOT, "a human arbitration does not upgrade provenance"

    after = allowance.pooled_minutes(conn, settings, week_start).charged_min
    assert after == pytest.approx(before + 149.0)
    assert [r.source for r in report.needs_your_eye(conn, clk, settings) if r.source == "Lionfish"] == []


def test_correct_measurement_refuses_impossible_figures(conn, clk, settings):
    screentime_with_a_conflict(conn, clk, settings)
    (eye,) = [r for r in report.needs_your_eye(conn, clk, settings) if r.source == "Lionfish"]
    for bad in (-1, 1441, float("nan"), "twelve"):
        with pytest.raises(screentime.CorrectionError):
            screentime.correct_measurement(conn, clk, settings, eye.measurement_id, bad)
    row = conn.execute("SELECT minutes, status FROM measurements WHERE id = ?", (eye.measurement_id,)).fetchone()
    assert (row["minutes"], row["status"]) == (None, "unreadable")
    with pytest.raises(screentime.CorrectionError):
        screentime.correct_measurement(conn, clk, settings, 424242, 10)


def test_a_second_correction_keeps_the_first_in_the_record(conn, clk, settings):
    screentime_with_a_conflict(conn, clk, settings)
    (eye,) = [r for r in report.needs_your_eye(conn, clk, settings) if r.source == "Lionfish"]
    screentime.correct_measurement(conn, clk, settings, eye.measurement_id, 148.0)
    screentime.correct_measurement(conn, clk, settings, eye.measurement_id, 149.0)
    detail = json.loads(conn.execute("SELECT detail FROM measurements WHERE id = ?",
                                     (eye.measurement_id,)).fetchone()["detail"])
    assert [c["minutes"] for c in detail["corrections"]] == [148.0, 149.0]
    assert detail["corrections"][1]["previous_minutes"] == 148.0


def test_the_parser_keeps_the_raw_text_of_a_row_whose_name_was_unreadable(conn, clk, settings):
    """No measurement can hold it — it has no source — so the import's meta does."""
    pages, recovery = load("last_week.json")
    out = screentime.ingest_batch(conn, clk, settings, images(), received_at=NOW,
                                  ocr=fake_ocr(pages), region_ocr=lambda *a, **k: [])
    meta = json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?", (out.import_id,)).fetchone()["meta"])
    assert meta["unnamed_rows"] >= 1
    assert meta["unnamed_row_texts"], "the minutes are real; only the name was lost"
    assert all("screenshot" in text for text in meta["unnamed_row_texts"])


def test_needs_your_eye_ignores_failures_a_person_could_not_resolve(conn, clk, settings):
    """A YouTube video with no duration from the API has no number to supply."""
    source = labels.ensure_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, "UCchannelAAAAAAAAAAAAAAA")
    rec = evidence.begin_import(conn, clk, kind="youtube_history", parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=source.id, device=T.DEVICE_ACCOUNT,
                                provenance=T.PROV_PAGE, period_start="2026-09-07", period_end="2026-09-07",
                                minutes=None, status="duration_missing", item_key="vid1")
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-07", period_end="2026-09-07")
    assert report.needs_your_eye(conn, clk, settings) == []


def test_needs_attention_flags_screen_time_rows_waiting_for_a_human(conn, clk, settings):
    screentime_with_a_conflict(conn, clk, settings)
    row = row_for(report.needs_attention(conn, clk, settings), T.GROUP_IPHONE)
    assert "waiting for your eye" in row.detail
    assert row.action_text


# ==========================================================================
# the Sunday check-in's wording (DR5.24; behaviour in tests/test_checklist.py)
# ==========================================================================

def _needs(**over):
    base = dict(week=T.measurement_week(date(2026, 9, 6)), screentime=checklist.MISSING,
                youtube=checklist.MISSING, coldturkey=None, readings=0,
                unlabelled=labels.Summary(0, 0.0, None), final=False)
    base.update(over)
    return checklist.WeekNeeds(**base)


def test_screen_time_step_names_the_two_mistakes_that_actually_happened(settings):
    text = checklist.render(_needs(youtube=checklist.IN), settings, nudge=False)
    assert "Show More until it disappears" in text
    assert "in one go" in text
    assert "not All Devices" in text and '"Last Week"' in text
    assert "Everything else" in text and "collects itself" in text


def test_youtube_step_names_the_steps(settings):
    text = checklist.render(_needs(screentime=checklist.IN), settings, nudge=False)
    assert "youtube.com/feed/history" in text and "press Sync" in text
    assert "Already in: Screen Time." in text


def test_the_cold_turkey_step_is_gone_while_the_automatic_read_is_healthy(conn, tmp_path, clk, settings):
    configured = config.test_settings(**{**settings.__dict__, "coldturkey_db": tmp_path / "ct.db"})
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="news.example", minutes=30.0,
            day="2026-09-15", provenance=T.PROV_EXPORT, device=T.DEVICE_DESKTOP, import_kind="coldturkey")
    needs = checklist.week_needs(conn, clk, configured, T.measurement_week(date(2026, 9, 6)))
    assert needs.coldturkey is None
    assert "Cold Turkey" not in checklist.render(needs, configured, nudge=False)


def test_the_cold_turkey_step_returns_as_a_fallback_when_nothing_is_configured(conn, clk, settings):
    needs = checklist.week_needs(conn, clk, settings, T.measurement_week(date(2026, 9, 6)))
    assert needs.coldturkey == checklist.MISSING
    text = checklist.render(needs, settings, nudge=False)
    assert "Cold Turkey. Export last week by hand:" in text and "Websites usage (time)" in text
    assert "dr-brainrot" not in text


# ==========================================================================
# jobs.run_sync
# ==========================================================================

def test_run_sync_runs_only_the_fetching_steps_and_sends_nothing(conn, clk, settings, tmp_path):
    vault = tmp_path / "vault"
    (vault / "01-Inbox").mkdir(parents=True)
    fetching = config.test_settings(**{**settings.__dict__, "vault_path": vault,
                                       "capture_outbox": tmp_path / "outbox.jsonl"})
    results = jobs.run_sync(conn, clk, fetching)
    assert set(results) == set(jobs.SYNC_STEPS)
    assert all(isinstance(r, jobs.StepResult) for r in results.values())
    assert all(r.ok for r in results.values()), {k: v.error for k, v in results.items()}
    assert "not configured" in results["qbserve"].summary
    # Nothing was said to the user: no bot was given and no prompt row was written.
    assert conn.execute("SELECT COUNT(*) FROM prompts_sent").fetchone()[0] == 0


def test_run_sync_isolates_a_failing_step(conn, clk, settings, tmp_path):
    broken = config.test_settings(**{**settings.__dict__, "vault_path": tmp_path / "not-mounted",
                                     "capture_outbox": tmp_path / "outbox.jsonl"})
    results = jobs.run_sync(conn, clk, broken)
    assert results["vault_scan"].ok is False and results["vault_scan"].error
    assert results["read_outbox"].ok is True
