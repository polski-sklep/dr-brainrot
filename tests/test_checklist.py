"""checklist.py: the Sunday check-in and its one Monday reminder (DECISIONS DR5.24).

The user wanted "one concrete time to enter all inputs" and chose Sunday
morning. These tests pin when something is said (Sunday, then at most once
more), what is said (one numbered list in a fixed order, what is already in,
nothing when everything is), and the two edge cases the old reminders got
wrong: a Screen Time capture cut off at "Show More", and the diagnostic's final
week, which used to be never asked for at all.

Evidence goes through `evidence.begin_import`/`finish_import`, so imports and
periods are real. No network: the bot is a recording double.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import checklist
import clock
import config
import db
import drbr_types as T
import evidence
import jobs
import labels

TZ = "Europe/Warsaw"
W = "2026-10-04"                                  # the week asked about: Sun 4 – Sat 10 Oct
SUNDAY = "2026-10-11T07:30:00+00:00"              # 09:30 Warsaw (CEST)
MONDAY = "2026-10-12T07:30:00+00:00"
TUESDAY = "2026-10-13T07:30:00+00:00"
FRIDAY = "2026-10-16T07:30:00+00:00"
LINK = "http://127.0.0.1:8750"


class RecordingBot:
    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(kwargs)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


@pytest.fixture
def settings(tmp_path):
    """Cold Turkey and Qbserve configured, as on the live Mac."""
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-09-13",
                                coldturkey_db=tmp_path / "ct.db", qbserve_db=tmp_path / "qb.db")


@pytest.fixture
def clk():
    return clock.FrozenClock(SUNDAY)


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    evidence.ensure_diagnostic_start(c, "2026-09-13")     # 8 weeks: the last day is Sat 7 Nov
    yield c
    c.close()


# --- evidence helpers --------------------------------------------------------

def _import(conn, clk, settings, kind, start, end, *, meta=None, status=None):
    rec = evidence.begin_import(conn, clk, kind=kind, parser_version="t")
    rec.meta.update(meta or {})
    evidence.finish_import(conn, clk, settings, rec, period_start=start, period_end=end, status=status)
    return rec


def screen_time(conn, clk, settings, sunday=W, *, complete=True):
    week = T.measurement_week(T.parse_date(sunday))
    meta = {} if complete is None else {"most_used_complete": complete}
    return _import(conn, clk, settings, "screentime", week.start, week.end, meta=meta)


def youtube_sync(conn, clk, settings):
    """A sync received now; it covers every week whose Saturday has passed."""
    return _import(conn, clk, settings, "youtube_history", "2026-10-05", "2026-10-10")


def cold_turkey_read(conn, clk, settings):
    """The automatic read of this Mac's statistics, received now: healthy."""
    yesterday = T.iso_date(T.local_date(clk.now(), TZ) - timedelta(days=1))
    return _import(conn, clk, settings, "coldturkey", yesterday, yesterday, meta={"origin": "local_db"})


def unlabelled(conn, clk, settings, count, minutes_each, day="2026-10-06"):
    """`count` desktop apps nobody has labelled yet, `minutes_each` apiece."""
    rec = evidence.begin_import(conn, clk, kind="qbserve", parser_version="t")
    for i in range(count):
        src = labels.ensure_source(conn, clk, T.KIND_DESKTOP_APP, f"com.example.app{i}")
        evidence.upsert_measurement(conn, rec, source_id=src.id, device=T.DEVICE_DESKTOP,
                                    provenance=T.PROV_APP_TRACKER, period_start=day, period_end=day,
                                    minutes=float(minutes_each))
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=day)


def readings(conn, clk, settings, count, sunday="2026-09-27"):
    """`count` Screen Time rows the screenshots couldn't read, from an earlier week."""
    week = T.measurement_week(T.parse_date(sunday))
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="t")
    for i in range(count):
        src = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, f"Unreadable {i}")
        labels.set_source_label(conn, clk, src.id, T.WASTEFUL)    # keeps the label count exact
        evidence.upsert_measurement(conn, rec, source_id=src.id, device=T.DEVICE_IPHONE,
                                    provenance=T.PROV_SCREENSHOT, period_start=week.start,
                                    period_end=week.end, minutes=None, status="unreadable",
                                    detail={"raw": [f"Unreadable {i} | 2h 27m", "2h 37m"]})
    evidence.finish_import(conn, clk, settings, rec, period_start=week.start, period_end=week.end)


def everything_in(conn, clk, settings):
    screen_time(conn, clk, settings)
    youtube_sync(conn, clk, settings)


def run(conn, clk, settings, bot, at=None):
    if at is not None:
        clk.set(at)
        cold_turkey_read(conn, clk, settings)        # the 09:30 run reads Cold Turkey first
    return checklist.send_due(conn, clk, settings, bot)


# --- the Sunday list -----------------------------------------------------------

def test_sunday_with_nothing_in_sends_one_numbered_list_in_the_fixed_order(conn, clk, settings):
    readings(conn, clk, settings, 2)
    unlabelled(conn, clk, settings, 12, 4)
    bot = RecordingBot()
    sent = run(conn, clk, settings, bot, at=SUNDAY)
    assert sent == [f"sunday checklist for week {W}: screentime, youtube, readings, labels"]
    assert bot.texts() == [
        "Sunday check-in for last week, Sun 4 – Sat 10 Oct. In this order:\n\n"
        "1. Screen Time. On the iPhone: Settings → Screen Time → this iPhone (not All Devices) → Week, "
        "then tap ‹ once so it says \"Last Week\". Under Most Used, tap Show More until it disappears. "
        "Screenshot the whole page from the top and send them all here in one go.\n\n"
        "2. YouTube. On the computer, open youtube.com/feed/history, click the YouTube history extension "
        "and press Sync.\n\n"
        f"3. Settle 2 readings the screenshots couldn't read for sure: {LINK}/#needs-your-eye\n\n"
        f"4. Label new sources. 12 are waiting (48 min), and steps 1 and 2 usually add a few: "
        f"{LINK}/labels/queue\n\n"
        "Everything else (Mac apps, websites, podcasts, your vault) collects itself."
    ]
    assert "Already in" not in bot.texts()[0]
    assert bot.sent[0].get("reply_markup") is None
    assert evidence.get_meta(conn, checklist.CHECKLIST_KEY) == W
    assert run(conn, clk, settings, bot) == [] and len(bot.sent) == 1        # the same Sunday again


def test_what_already_arrived_is_listed_after_and_never_numbered(conn, clk, settings):
    clk.set("2026-10-11T06:00:00+00:00")         # the session started before the 09:30 run
    youtube_sync(conn, clk, settings)
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    (text,) = bot.texts()
    assert "1. Screen Time." in text and "YouTube." not in text.split("Already in")[0]
    assert "\n\nAlready in: YouTube.\nEverything else" in text
    assert "In this order" in text               # Screen Time, then labels


def test_a_capture_cut_off_at_show_more_is_asked_for_once_more(conn, clk, settings):
    clk.set("2026-10-11T06:00:00+00:00")
    screen_time(conn, clk, settings, complete=False)
    youtube_sync(conn, clk, settings)
    bot = RecordingBot()
    assert run(conn, clk, settings, bot, at=SUNDAY) == [f"sunday checklist for week {W}: screentime, labels"]
    assert bot.texts() == [
        "Sunday check-in for last week, Sun 4 – Sat 10 Oct. In this order:\n\n"
        "1. Screen Time, once more. The screenshots stopped at \"Show More\", so the apps below it are "
        "missing. Under Most Used, tap Show More until it disappears, then send the whole page here in "
        "one go.\n\n"
        f"2. Label any new sources the screenshots bring: {LINK}/labels/queue\n\n"
        "Already in: YouTube.\n"
        "Everything else (Mac apps, websites, podcasts, your vault) collects itself."
    ]


def test_a_capture_whose_end_of_list_was_never_seen_counts_as_in(conn, clk, settings):
    screen_time(conn, clk, settings, complete=None)
    needs = checklist.week_needs(conn, clk, settings, T.measurement_week(T.parse_date(W)))
    assert needs.screentime == checklist.IN
    screen_time(conn, clk, settings, complete=False)          # a later capture of the same week wins
    needs = checklist.week_needs(conn, clk, settings, T.measurement_week(T.parse_date(W)))
    assert needs.screentime == checklist.CUT_OFF and needs.evidence_missing
    screen_time(conn, clk, settings, complete=True)
    needs = checklist.week_needs(conn, clk, settings, T.measurement_week(T.parse_date(W)))
    assert needs.screentime == checklist.IN


def test_a_this_week_capture_does_not_cover_last_week(conn, clk, settings):
    _import(conn, clk, settings, "screentime", None, None, status="partial")
    needs = checklist.week_needs(conn, clk, settings, T.measurement_week(T.parse_date(W)))
    assert needs.screentime == checklist.MISSING


@pytest.mark.parametrize("count,line", [
    (0, None),
    (1, f"Settle 1 reading the screenshots couldn't read for sure: {LINK}/#needs-your-eye"),
    (3, f"Settle 3 readings the screenshots couldn't read for sure: {LINK}/#needs-your-eye"),
])
def test_readings_to_settle_appear_only_when_there_are_some(conn, clk, settings, count, line):
    if count:
        readings(conn, clk, settings, count)
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    (text,) = bot.texts()
    if line is None:
        assert "needs-your-eye" not in text and "Settle" not in text
    else:
        assert f"3. {line}\n\n" in text


def test_the_labels_line_names_the_count_and_says_more_may_come_only_while_evidence_is_due(conn, clk, settings):
    unlabelled(conn, clk, settings, 1, 5)
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    assert (f"3. Label new sources. 1 is waiting (5 min), and steps 1 and 2 usually add a few: "
            f"{LINK}/labels/queue") in bot.texts()[0]

    # A week with its evidence in: no "add a few", because nothing is left to bring any.
    unlabelled(conn, clk, settings, 31, 2, day="2026-10-07")     # app0 again, plus 30 more
    evidence.set_meta(conn, checklist.CHECKLIST_KEY, "")
    everything_in(conn, clk, settings)
    bot = RecordingBot()
    run(conn, clk, settings, bot)
    assert bot.texts() == [
        "Sunday check-in for last week, Sun 4 – Sat 10 Oct.\n\n"
        f"1. Label new sources. 31 are waiting (67 min): {LINK}/labels/queue\n\n"
        "Already in: Screen Time, YouTube."
    ]


def test_an_hour_undecided_is_worth_a_message_of_its_own(conn, clk, settings):
    """The design's example: evidence all in by 09:30, 74 minutes undecided."""
    clk.set("2026-10-11T06:00:00+00:00")
    everything_in(conn, clk, settings)
    unlabelled(conn, clk, settings, 30, 2)
    unlabelled_one = labels.ensure_source(conn, clk, T.KIND_DESKTOP_APP, "com.example.big")
    rec = evidence.begin_import(conn, clk, kind="qbserve", parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=unlabelled_one.id, device=T.DEVICE_DESKTOP,
                                provenance=T.PROV_APP_TRACKER, period_start="2026-10-08",
                                period_end="2026-10-08", minutes=14.0)
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-10-08", period_end="2026-10-08")
    bot = RecordingBot()
    assert run(conn, clk, settings, bot, at=SUNDAY) == [f"sunday checklist for week {W}: labels"]
    assert bot.texts() == [
        "Sunday check-in for last week, Sun 4 – Sat 10 Oct.\n\n"
        f"1. Label new sources. 31 are waiting (74 min): {LINK}/labels/queue\n\n"
        "Already in: Screen Time, YouTube."
    ]


@pytest.mark.parametrize("minutes,speaks", [(59.0, False), (60.0, True)])
def test_labels_alone_speak_only_from_an_hour(conn, clk, settings, minutes, speaks):
    clk.set("2026-10-11T06:00:00+00:00")
    everything_in(conn, clk, settings)
    unlabelled(conn, clk, settings, 1, minutes)
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    assert len(bot.sent) == int(speaks)
    assert checklist.LABELS_WORTH_A_MESSAGE_MIN == 60.0
    assert evidence.get_meta(conn, checklist.CHECKLIST_KEY) == W     # dealt with either way


def test_everything_in_sends_nothing_and_no_reminder_follows(conn, clk, settings, caplog):
    clk.set("2026-10-11T06:00:00+00:00")
    everything_in(conn, clk, settings)
    bot = RecordingBot()
    with caplog.at_level("INFO", logger="drbr.checklist"):
        assert run(conn, clk, settings, bot, at=SUNDAY) == []
    assert bot.sent == []
    assert f"week {W} is all in; nothing sent" in caplog.text
    assert evidence.get_meta(conn, checklist.CHECKLIST_KEY) == W
    assert run(conn, clk, settings, bot, at=MONDAY) == [] and bot.sent == []
    assert evidence.get_meta(conn, checklist.NUDGE_KEY) == W


# --- the one reminder ------------------------------------------------------------

def test_monday_reminds_once_about_missing_evidence_only(conn, clk, settings):
    readings(conn, clk, settings, 2)
    unlabelled(conn, clk, settings, 12, 4)
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    clk.set("2026-10-11T12:00:00+00:00")
    youtube_sync(conn, clk, settings)                 # done on Sunday; Screen Time forgotten
    assert run(conn, clk, settings, bot, at=MONDAY) == [f"monday nudge for week {W}: screentime"]
    assert bot.texts()[-1] == (
        "Last week, Sun 4 – Sat 10 Oct, still needs:\n\n"
        "1. Screen Time. On the iPhone: Settings → Screen Time → this iPhone (not All Devices) → Week, "
        "then tap ‹ once so it says \"Last Week\". Under Most Used, tap Show More until it disappears. "
        "Screenshot the whole page from the top and send them all here in one go.\n\n"
        "Already in: YouTube.\n"
        "That's the only reminder for this week. The dashboard keeps showing what's missing: "
        f"{LINK}/?week={W}#coverage"
    )
    assert run(conn, clk, settings, bot) == []                       # the same Monday again
    for day in (TUESDAY, FRIDAY, "2026-10-17T07:30:00+00:00"):
        assert run(conn, clk, settings, bot, at=day) == []          # and never again for that week
    assert len(bot.sent) == 2


def test_a_cut_off_capture_is_asked_for_again_on_monday(conn, clk, settings):
    clk.set("2026-10-11T06:00:00+00:00")
    screen_time(conn, clk, settings, complete=False)
    youtube_sync(conn, clk, settings)
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    run(conn, clk, settings, bot, at=MONDAY)
    assert len(bot.sent) == 2 and "1. Screen Time, once more." in bot.texts()[1]
    assert "Label" not in bot.texts()[1]


def test_labels_or_readings_alone_never_cause_a_monday_message(conn, clk, settings):
    clk.set("2026-10-11T06:00:00+00:00")
    everything_in(conn, clk, settings)
    readings(conn, clk, settings, 2)
    unlabelled(conn, clk, settings, 40, 5)
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    assert len(bot.sent) == 1
    assert run(conn, clk, settings, bot, at=MONDAY) == [] and len(bot.sent) == 1


def test_a_failed_sunday_send_writes_no_marker_and_monday_sends_the_whole_list(conn, clk, settings):
    unlabelled(conn, clk, settings, 3, 4)
    with pytest.raises(RuntimeError):
        run(conn, clk, settings, RecordingBot(fail=True), at=SUNDAY)
    assert evidence.get_meta(conn, checklist.CHECKLIST_KEY) is None
    bot = RecordingBot()
    assert run(conn, clk, settings, bot, at=MONDAY) == [
        f"monday checklist for week {W}: screentime, youtube, labels"]
    (text,) = bot.texts()
    assert text.startswith("Check-in for last week, Sun 4 – Sat 10 Oct. In this order:\n\n1. Screen Time.")
    assert f"3. Label new sources. 3 are waiting (12 min)" in text
    assert text.endswith("Everything else (Mac apps, websites, podcasts, your vault) collects itself.\n"
                         "That's the only reminder for this week. The dashboard keeps showing what's "
                         f"missing: {LINK}/?week={W}#coverage")
    assert run(conn, clk, settings, bot, at=TUESDAY) == [] and len(bot.sent) == 1


def test_a_mac_off_on_monday_gets_its_reminder_on_tuesday(conn, clk, settings):
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    assert run(conn, clk, settings, bot, at=TUESDAY) == [f"tuesday nudge for week {W}: screentime, youtube"]
    assert bot.texts()[1].startswith("Last week, Sun 4 – Sat 10 Oct, still needs these, in this order:\n\n")
    assert run(conn, clk, settings, bot, at="2026-10-14T07:30:00+00:00") == []


def test_merging_mid_week_sends_nothing_off_schedule(conn, clk, settings):
    """No markers yet on a Friday: the week before was never asked through this, so nothing goes."""
    screen_time(conn, clk, settings, "2026-09-27", complete=False)      # like the live import #80
    bot = RecordingBot()
    assert run(conn, clk, settings, bot, at="2026-10-09T07:30:00+00:00") == []
    assert bot.sent == [] and evidence.get_meta(conn, checklist.NUDGE_KEY) is None


# --- Cold Turkey, only as a fallback -------------------------------------------------

def test_cold_turkey_is_left_out_while_its_automatic_read_is_healthy(conn, clk, settings):
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    assert "Cold Turkey" not in bot.texts()[0]
    needs = checklist.week_needs(conn, clk, settings, T.measurement_week(T.parse_date(W)))
    assert needs.coldturkey is None


def test_cold_turkey_is_asked_for_by_hand_when_its_read_is_failing(conn, clk, settings):
    clk.set(SUNDAY)
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t")
    evidence.fail_import(conn, rec, "unable to open database file")
    bot = RecordingBot()
    assert checklist.send_due(conn, clk, settings, bot) == [
        f"sunday checklist for week {W}: screentime, youtube, coldturkey, labels"]
    (text,) = bot.texts()
    assert ("3. Cold Turkey. This Mac's statistics couldn't be read this week, so export them by hand: "
            "Cold Turkey Blocker → Statistics → Websites usage (time) → Sun 4 – Sat 10 Oct → Export, and send "
            "the JSON file here.\n\n") in text
    assert "4. Label any new sources the steps above bring" in text
    assert "Everything else (Mac apps, podcasts, your vault) collects itself." in text
    assert "DRBR" not in text


def test_cold_turkey_unconfigured_is_a_plain_manual_export(conn, clk, tmp_path):
    plain = config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-09-13")
    clk.set(SUNDAY)
    bot = RecordingBot()
    checklist.send_due(conn, clk, plain, bot)
    (text,) = bot.texts()
    assert "3. Cold Turkey. Export last week by hand: Cold Turkey Blocker → Statistics" in text
    assert "Everything else (podcasts, your vault) collects itself." in text
    assert "DRBR" not in text and "COLDTURKEY" not in text
    # An export spanning the week is in; one that stopped mid-week is not.
    _import(conn, clk, plain, "coldturkey", "2026-10-04", "2026-10-08")
    needs = checklist.week_needs(conn, clk, plain, T.measurement_week(T.parse_date(W)))
    assert needs.coldturkey == checklist.MISSING
    _import(conn, clk, plain, "coldturkey", "2026-10-04", "2026-10-10")
    needs = checklist.week_needs(conn, clk, plain, T.measurement_week(T.parse_date(W)))
    assert needs.coldturkey == checklist.IN


# --- which weeks are asked for ---------------------------------------------------------

def test_a_week_before_go_live_is_never_asked_for(conn, clk, tmp_path):
    late = config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-10-11")
    bot = RecordingBot()
    clk.set(SUNDAY)
    assert checklist.asking_week(conn, clk, late) is None
    assert checklist.send_due(conn, clk, late, bot) == []
    clk.set(MONDAY)
    assert checklist.send_due(conn, clk, late, bot) == [] and bot.sent == []


def test_before_the_diagnostic_starts_the_week_is_still_asked_for(tmp_path):
    s = config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-09-13")
    c = db.connect(s)
    db.init_db(c)
    db.migrate(c)
    clk = clock.FrozenClock("2026-09-20T07:30:00+00:00")
    assert not evidence.diagnostic(c, clk, s).started
    assert checklist.asking_week(c, clk, s).start == "2026-09-13"
    bot = RecordingBot()
    assert checklist.send_due(c, clk, s, bot) and bot.sent
    c.close()


def test_the_final_week_is_still_asked_for_after_the_diagnostic_ends(conn, clk, settings):
    bot = RecordingBot()
    run(conn, clk, settings, bot, at="2026-11-08T08:30:00+00:00")     # Sun 8 Nov, 09:30 CET
    assert evidence.diagnostic(conn, clk, settings).ended
    (text,) = bot.texts()
    assert text.startswith("The diagnostic has ended. One last check-in, for its final week, "
                           "Sun 1 – Sat 7 Nov. In this order:\n\n1. Screen Time.")
    assert run(conn, clk, settings, bot, at="2026-11-09T08:30:00+00:00") == [
        "monday nudge for week 2026-11-01: screentime, youtube"]
    assert bot.texts()[1].startswith("The final week, Sun 1 – Sat 7 Nov, still needs these, in this order:")
    assert run(conn, clk, settings, bot, at="2026-11-15T08:30:00+00:00") == []
    assert checklist.asking_week(conn, clk, settings) is None
    assert len(bot.sent) == 2


def test_dates_cross_a_month_in_full():
    assert checklist._span(T.measurement_week(T.parse_date("2026-09-27"))) == "Sun 27 Sep – Sat 3 Oct"
    assert checklist._span(T.measurement_week(T.parse_date(W))) == "Sun 4 – Sat 10 Oct"


# --- the end notice ----------------------------------------------------------------

def test_the_end_notice_waits_for_the_final_weeks_evidence(conn, clk, settings):
    bot = RecordingBot()
    clk.set("2026-11-08T08:30:00+00:00")
    assert jobs.send_end_notice(conn, clk, settings, bot) == "end notice waits for the final week's evidence"
    clk.set("2026-11-09T08:30:00+00:00")
    assert jobs.send_end_notice(conn, clk, settings, bot) == "end notice waits for the final week's evidence"
    clk.set("2026-11-10T08:30:00+00:00")                               # Tuesday: regardless
    assert jobs.send_end_notice(conn, clk, settings, bot) == "end notice sent"
    assert jobs.send_end_notice(conn, clk, settings, bot) == "end notice already sent"
    (text,) = bot.texts()
    assert text.endswith("Nothing more will be asked for.") and "Prompts" not in text


def test_the_end_notice_goes_on_monday_once_the_final_week_is_in(conn, clk, settings):
    clk.set("2026-11-08T10:00:00+00:00")
    screen_time(conn, clk, settings, "2026-11-01")
    _import(conn, clk, settings, "youtube_history", "2026-11-02", "2026-11-07")
    clk.set("2026-11-09T08:30:00+00:00")
    cold_turkey_read(conn, clk, settings)
    bot = RecordingBot()
    assert jobs.send_end_notice(conn, clk, settings, bot) == "end notice sent"
    assert len(bot.sent) == 1


def test_markers_live_in_meta_and_no_new_prompt_kind_is_written(conn, clk, settings):
    bot = RecordingBot()
    run(conn, clk, settings, bot, at=SUNDAY)
    run(conn, clk, settings, bot, at=MONDAY)
    assert conn.execute("SELECT COUNT(*) FROM prompts_sent").fetchone()[0] == 0
    stored = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'checklist%'").fetchall())
    assert stored == {"checklist_week": W, "checklist_nudge_week": W}
