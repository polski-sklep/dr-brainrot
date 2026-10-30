"""Phase 2.5 integration: the seams between the four Phase 2 components.

Each component is unit-tested on its own branch. These tests exercise only
what integration added or connected: the daily job, relabel-driven overage
re-recording, the go-live floor across modules, and the bot's handler set
with every real extension module installed.
"""
from __future__ import annotations

import asyncio

import pytest

import allowance
import bot
import clock
import config
import db
import dr_brainrot_types as T
import checklist
import evidence
import jobs
import labels
import report


class RecordingBot:
    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(kwargs)


@pytest.fixture
def settings(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "01-Inbox").mkdir()
    return config.test_settings(
        tz="Europe/Warsaw", raw_dir=tmp_path / "raw", vault_path=vault,
        capture_outbox=tmp_path / "outbox.jsonl", go_live="2026-09-13",
        weekly_allowance_min=360,
    )


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-16T10:00:00+00:00")


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


def _minutes(conn, clk, settings, *, kind, key, day, minutes):
    src = labels.ensure_source(conn, clk, kind, key)
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=src.id, device="desktop", provenance=T.PROV_EXPORT,
                                period_start=day, period_end=day, minutes=minutes, item_key=key)
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=day)
    return src


# --- the daily job ----------------------------------------------------------

def test_daily_job_runs_every_step_and_reports_none_failed(conn, clk, settings):
    failed = jobs.run_daily(conn, clk, settings, RecordingBot())
    assert failed == []
    assert conn.execute("SELECT status FROM imports WHERE kind='vault_scan'").fetchone()[0] in ("ok", "partial")


def test_daily_job_isolates_a_failing_step(conn, clk, settings, tmp_path):
    broken = config.test_settings(**{**settings.__dict__, "vault_path": tmp_path / "nope"})
    failed = jobs.run_daily(conn, clk, broken, RecordingBot())
    assert "vault_scan" in failed
    assert "checklist" not in failed


def test_daily_job_reads_everything_when_telegram_is_down(conn, clk, settings):
    """27–28 Sep: no internet, and the morning job died before reading anything (DR5.25).

    Now every read happens and the summary is written. Only the step that
    sends fails, and it records nothing, so Monday sends the whole list.
    """
    clk.set("2026-09-20T08:00:00+00:00")        # Sunday 10:00 in Warsaw: the check-in is due
    failed = jobs.run_daily(conn, clk, settings, RecordingBot(fail=True))
    assert failed == ["checklist"]
    assert conn.execute("SELECT status FROM imports WHERE kind = 'vault_scan'").fetchone()[0] in ("ok", "partial")
    assert evidence.get_meta(conn, checklist.CHECKLIST_KEY) is None
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 1

    clk.set("2026-09-21T08:00:00+00:00")        # Monday, online again
    bot = RecordingBot()
    assert jobs.run_daily(conn, clk, settings, bot) == []
    assert len(bot.sent) == 1
    text = bot.sent[0]["text"]
    assert text.startswith("Check-in for last week, Sun 13 – Sat 19 Sep. In this order:")
    assert "\n\n1. Screen Time. " in text and "\n\n2. YouTube. " in text
    assert evidence.get_meta(conn, checklist.NUDGE_KEY) == "2026-09-13"


def test_no_prompt_for_a_week_before_go_live(conn, clk, settings):
    clk.set("2026-09-13T08:00:00+00:00")        # Sunday: the week just ended, 6–12 Sep, is before go-live
    assert checklist.send_due(conn, clk, settings, RecordingBot()) == []
    clk.set("2026-09-20T08:00:00+00:00")        # the Sunday after the first live week
    bot = RecordingBot()
    sent = checklist.send_due(conn, clk, settings, bot)
    assert sent == ["sunday checklist for week 2026-09-13: screentime, youtube, coldturkey, labels"]
    assert len(bot.sent) == 1


def test_end_notice_is_sent_once_and_only_after_the_end(conn, clk, settings):
    evidence.ensure_diagnostic_start(conn, "2026-09-13")
    b = RecordingBot()
    assert jobs.send_end_notice(conn, clk, settings, b) == "diagnostic running"
    clk.set("2026-11-08T10:00:00+00:00")        # ended, but the final week is still being asked for
    assert jobs.send_end_notice(conn, clk, settings, b) == "end notice waits for the final week"
    clk.set("2026-11-10T10:00:00+00:00")        # the Tuesday after: regardless
    assert jobs.send_end_notice(conn, clk, settings, b) == "end notice sent"
    assert jobs.send_end_notice(conn, clk, settings, b) == "end notice already sent"
    assert len(b.sent) == 1 and "If there isn't one, switch it off" in b.sent[0]["text"]
    assert "Nothing more will be asked for." in b.sent[0]["text"]


# --- relabel re-records overage ------------------------------------------------

def test_relabelling_a_source_re_records_overage_immediately(conn, clk, settings):
    clk.set("2026-09-14T10:00:00+00:00")
    closed = db.current_week(conn, clk, settings)
    src = _minutes(conn, clk, settings, kind="web_domain", key="news.example", day="2026-09-15", minutes=500)
    clk.set("2026-09-20T16:30:00+00:00")        # after Sunday 18:00 Warsaw
    _closed, opened = db.roll_over_week(conn, clk, settings)
    assert _closed.week_start == closed.week_start
    assert _closed.overage_min == 140            # 500 wasteful - 360, recorded on the closed week
    assert opened.debt_min == 0                  # never carried (DR5.2)

    # The relabel happens in the dashboard now (DR5.6); `bulk_set_label` is the
    # one write behind it, and it recomputes every week's overage itself.
    assert labels.bulk_set_label(conn, clk, [src.id], T.WORTHWHILE, settings=settings) == 1
    was = conn.execute("SELECT overage_min, debt_min FROM weeks WHERE week_start = ?",
                       (closed.week_start,)).fetchone()
    assert (was["overage_min"], was["debt_min"]) == (0, 0)       # now worthwhile: never over
    now = conn.execute("SELECT overage_min, debt_min FROM weeks WHERE week_start = ?",
                       (opened.week_start,)).fetchone()
    assert now["debt_min"] == 0


# --- go-live across modules -------------------------------------------------

def test_report_window_falls_back_to_go_live_before_first_import(conn, clk, settings):
    assert report.window_start(conn, clk, settings) == "2026-09-13"
    evidence.ensure_diagnostic_start(conn, "2026-09-14")
    assert report.window_start(conn, clk, settings) == "2026-09-14"


def test_import_reaching_before_go_live_starts_the_clock_at_go_live(conn, clk, settings):
    src = labels.ensure_source(conn, clk, "web_domain", "a.example")
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=src.id, device="desktop", provenance=T.PROV_EXPORT,
                                period_start="2026-09-10", period_end="2026-09-10", minutes=3)
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-10", period_end="2026-09-15")
    assert evidence.get_meta(conn, "diagnostic_start") == "2026-09-13"


# --- bot wiring -------------------------------------------------------------

def test_build_app_installs_every_extension_without_collisions(settings, clk):
    app = bot.build_app(settings, object(), clk, object())
    handlers = [h for group in app.handlers.values() for h in group]
    modules = {h.callback.__module__ for h in handlers}
    assert {"bot", "handlers_imports", "handlers_labels"} <= modules
    from telegram.ext import CallbackQueryHandler, MessageHandler
    text_handlers = [h for h in handlers if isinstance(h, MessageHandler) and h.callback.__module__ != "bot"]
    for h in text_handlers:
        assert "TEXT" not in repr(h.filters).upper() or "DOCUMENT" in repr(h.filters).upper() \
            or "PHOTO" in repr(h.filters).upper()
    queue_cb = [h for h in handlers if isinstance(h, CallbackQueryHandler) and h.callback is bot.on_callback]
    assert queue_cb and queue_cb[0].pattern("d:lbl:w:1") is False and queue_cb[0].pattern("promote:1") is True


def test_label_resolution_is_shared_by_gate_and_report(conn, clk, settings):
    """The gate and the report must read the same label for the same source."""
    db.upsert_episode(conn, "4rOoJ6Egrf8K2IrywzwOMk", "Ep", "Show One", None, None, 3_600_000)
    item = db.capture(conn, clk, settings, spotify_id="4rOoJ6Egrf8K2IrywzwOMk", why_note="because")
    src = labels.ensure_source(conn, clk, "podcast_show", "Show One")
    labels.set_source_label(conn, clk, src.id, "worthwhile")
    assert allowance.item_label(conn, item).label == "worthwhile"
    assert labels.resolve(conn, source_id=src.id, item_ref="spotify:4rOoJ6Egrf8K2IrywzwOMk").label == "worthwhile"


def test_no_telegram_message_names_the_product():
    """The product never names itself in its own copy — on the page or in Telegram.

    The dashboard was cleaned on 22 Sep; Telegram was missed until a Friday-morning
    Qbserve warning said "missing from dr-brainrot". This pins every message constant and
    template the bot sends, so the next one cannot slip through the same gap.
    """
    import pathlib
    import re

    import checklist as checklist_module
    import handlers_imports
    import handlers_labels
    import jobs as jobs_module
    import poll
    import triage

    sources = {
        "handlers_labels.CONTENT_QUESTION": handlers_labels.CONTENT_QUESTION,
        "checklist.AUTOMATIC_LINE": checklist_module.AUTOMATIC_LINE,
        "checklist.SCREENTIME_TODO": checklist_module.SCREENTIME_TODO,
        "checklist.COLDTURKEY_FAILING": checklist_module.COLDTURKEY_FAILING,
        "checklist.LAST_REMINDER": checklist_module.LAST_REMINDER,
    }
    # Message bodies built inline: read them from the source, skipping comments,
    # docstrings and config-key names such as DR_BRAINROT_COLDTURKEY_DB.
    for module in (checklist_module, handlers_imports, handlers_labels, jobs_module, triage, poll):
        text = pathlib.Path(module.__file__).read_text()
        text = re.sub(r'""".*?"""', "", text, flags=re.S)
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            for literal in re.findall(r'f?"((?:[^"\\]|\\.)*)"', code):
                sources[f"{module.__name__}:{n}"] = literal
    offenders = {where: s for where, s in sources.items()
                 if re.search(r"\bDRBR\b(?!_)|Dr\.? ?Brainrot", s)}
    assert not offenders, offenders
