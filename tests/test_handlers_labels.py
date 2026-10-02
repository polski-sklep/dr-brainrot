"""handlers_labels.py: the Monday label digest and /labels (CONTRACTS Part II §8, §19).

Since DR5.6 nothing here labels anything. What is tested is the cadence (DR5.19:
Monday only, and only with an hour undecided — folded into the import
reminders when there are any), the content of that one message, and that no
button or callback survived the move to the dashboard.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

import clock
import config
import db
import drbr_types as T
import evidence
import handlers_labels as H
import labels

OWNER = 1
STRANGER = 666


# --- doubles ----------------------------------------------------------------

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data, uid=OWNER):
        self.data = data
        self.from_user = FakeUser(uid)
        self.answers: list[str | None] = []
        self.edits: list[str] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class FakeUpdate:
    def __init__(self, *, uid=OWNER, query=None):
        self.effective_user = FakeUser(uid)
        self.callback_query = query
        self.message = FakeMessage()


class AsyncBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class SyncBot:
    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


class FakeContext:
    def __init__(self, bot_data, bot=None):
        self.bot_data = bot_data
        self.bot = bot or AsyncBot()


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw", telegram_user_id=OWNER)


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


@pytest.fixture
def ctx(conn, clk, settings):
    return FakeContext({"conn": conn, "clk": clk, "settings": settings})


def _observed(conn, clk, settings, name, day="2026-09-06", minutes=3.0):
    s = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, name)
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=s.id, device="iphone", provenance=T.PROV_SCREENSHOT,
                                period_start=day, period_end=day, minutes=minutes)
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=day)
    return s


def _started(conn, clk, settings, *names):
    """Diagnostic starts 2026-09-06 via the first measurement; sources observed."""
    return [_observed(conn, clk, settings, n) for n in names]


# --- registration -----------------------------------------------------------

def test_register_adds_the_command_and_no_callback_handler():
    class App:
        def __init__(self):
            self.handlers = []

        def add_handler(self, h):
            self.handlers.append(h)

    app = App()
    H.register(app)
    assert [h for h in app.handlers if isinstance(h, CallbackQueryHandler)] == []
    cmd = [h for h in app.handlers if isinstance(h, CommandHandler)]
    assert len(cmd) == 1 and "labels" in cmd[0].commands


def test_no_label_callback_survives_the_move_to_the_dashboard():
    """The `d:lbl:` namespace is gone: no handler, no data builder, no prompt."""
    for name in ("on_label_callback", "callback_data", "render_prompt", "send_label_batch", "CALLBACK_PREFIX"):
        assert not hasattr(H, name), f"{name} should have been removed with in-Telegram labelling"
    source = pathlib.Path(H.__file__).read_text()
    assert "d:lbl:" not in source
    assert "InlineKeyboard" not in source


# --- the digest -------------------------------------------------------------

def test_digest_nothing_before_the_diagnostic_started(conn, clk, settings):
    labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "Notes")
    bot = SyncBot()
    assert H.send_label_digest(conn, clk, settings, bot) == [] and bot.sent == []


MONDAY = "2026-09-14T08:00:00+00:00"           # 10:00 Warsaw, eight days after 09-06


def test_digest_waits_seven_days(conn, clk, settings):
    _observed(conn, clk, settings, "Notes", minutes=90.0)
    clk.set("2026-09-07T08:00:00+00:00")          # a Monday, but only day 1 of the diagnostic
    bot = SyncBot()
    assert H.send_label_digest(conn, clk, settings, bot) == []
    clk.set(MONDAY)                               # the first Monday after seven days
    assert len(H.send_label_digest(conn, clk, settings, bot)) == 1


def test_digest_names_the_count_the_minutes_the_group_and_the_link(conn, clk, settings):
    _observed(conn, clk, settings, "Notes", minutes=200.0)
    _observed(conn, clk, settings, "Maps", minutes=140.0)
    clk.set(MONDAY)
    bot = SyncBot()
    assert H.send_label_digest(conn, clk, settings, bot) == \
        ["label digest: 2 unlabelled source(s), 340 min"]
    (text,) = bot.texts()
    assert "2 sources (340 min) are unlabelled, mostly iphone apps." in text
    assert f"http://127.0.0.1:{settings.dashboard_port}/labels" in text
    assert "This is the only reminder about it this week." in text
    assert bot.sent[0].get("reply_markup") is None
    assert [tuple(r) for r in conn.execute("SELECT kind, period_start, sent_on FROM prompts_sent")] == \
        [("labels", "2026-09-13", "2026-09-14")]


def test_digest_speaks_only_on_mondays_and_once_each(conn, clk, settings):
    """One predictable morning a week, not a three-day clock drifting across it."""
    _observed(conn, clk, settings, "Notes", minutes=90.0)
    clk.set(MONDAY)
    bot = SyncBot()
    assert len(H.send_label_digest(conn, clk, settings, bot)) == 1
    assert H.send_label_digest(conn, clk, settings, bot) == []        # the same Monday again
    for _ in range(6):                                                 # Tuesday .. Sunday
        clk.advance(days=1)
        assert H.send_label_digest(conn, clk, settings, bot) == []
    clk.advance(days=1)                                                # the next Monday
    assert len(H.send_label_digest(conn, clk, settings, bot)) == 1
    assert len(bot.sent) == 2


@pytest.mark.parametrize("minutes,speaks", [(59.0, False), (60.0, True)])
def test_digest_needs_an_hour_undecided(conn, clk, settings, minutes, speaks):
    """34 minutes spread over 50 sources was not worth a message; an hour is."""
    _observed(conn, clk, settings, "Notes", minutes=minutes)
    clk.set(MONDAY)
    assert bool(H.send_label_digest(conn, clk, settings, SyncBot())) is speaks
    assert H.DIGEST_MIN_MINUTES == 60.0


def test_digest_says_nothing_when_everything_is_labelled(conn, clk, settings):
    (s,) = _started(conn, clk, settings, "Notes")
    labels.set_source_label(conn, clk, s.id, T.WASTEFUL)
    assert H.send_label_digest(conn, clk, settings, SyncBot()) == []
    assert conn.execute("SELECT COUNT(*) FROM prompts_sent").fetchone()[0] == 0


def test_digest_nothing_after_the_diagnostic_ended(conn, clk, settings):
    _started(conn, clk, settings, "Notes")
    clk.set("2026-11-01T10:00:00+00:00")         # 8 weeks from 09-06 end 10-31
    bot = SyncBot()
    assert H.send_label_digest(conn, clk, settings, bot) == [] and bot.sent == []


def test_digest_counts_only_sources_with_evidence(conn, clk, settings):
    _observed(conn, clk, settings, "Notes", minutes=75.0)
    labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "NeverSeen")     # identity only
    conn.commit()
    clk.set(MONDAY)
    bot = SyncBot()
    assert H.send_label_digest(conn, clk, settings, bot) == ["label digest: 1 unlabelled source(s), 75 min"]


def test_on_a_monday_with_reminders_the_note_rides_on_the_last_one(conn, clk, settings):
    """One fewer message: no separate digest when import reminders are going out."""
    import jobs

    _observed(conn, clk, settings, "Notes", minutes=90.0)
    clk.set(MONDAY)                               # the week of 6-12 Sep is now due
    bot = SyncBot()
    sent = jobs._prompts_with_digest(conn, clk, settings, bot)
    texts = bot.texts()
    assert texts and len(texts) == len(sent) - 1  # every reminder, and no extra message
    assert texts[-1].endswith(
        f"Also: 1 source (90 min) is unlabelled, mostly iphone apps. "
        f"Label them in the dashboard: http://127.0.0.1:{settings.dashboard_port}/labels")
    assert not any("Also:" in t for t in texts[:-1])
    # Recorded as sent, so the standalone step that follows stays quiet.
    assert H.send_label_digest(conn, clk, settings, bot) == []
    assert len(bot.texts()) == len(texts)


# --- /labels ----------------------------------------------------------------

def test_labels_command_replies_with_the_count_and_a_link_and_no_buttons(conn, clk, settings, ctx):
    _observed(conn, clk, settings, "Notes", minutes=12.0)
    update = FakeUpdate()
    asyncio.run(H.cmd_labels(update, ctx))
    (reply,) = update.message.replies
    assert "1 source (12 min) is unlabelled" in reply
    assert f"http://127.0.0.1:{settings.dashboard_port}/labels" in reply
    assert ctx.bot.sent == []


def test_labels_command_does_not_consume_the_monday_digest(conn, clk, settings, ctx):
    """Asking is not being told: /labels must not silence the next digest."""
    _observed(conn, clk, settings, "Notes", minutes=90.0)
    clk.set(MONDAY)
    asyncio.run(H.cmd_labels(FakeUpdate(), ctx))
    assert conn.execute("SELECT COUNT(*) FROM prompts_sent").fetchone()[0] == 0
    assert len(H.send_label_digest(conn, clk, settings, SyncBot())) == 1


def test_labels_command_reply_does_not_talk_about_reminders(conn, clk, settings, ctx):
    _observed(conn, clk, settings, "Notes", minutes=12.0)
    update = FakeUpdate()
    asyncio.run(H.cmd_labels(update, ctx))
    (reply,) = update.message.replies
    assert "reminder" not in reply and "Also:" not in reply


def test_labels_command_when_nothing_waits(conn, clk, settings, ctx):
    (s,) = _started(conn, clk, settings, "Notes")
    labels.set_source_label(conn, clk, s.id, T.TOOL)
    update = FakeUpdate()
    asyncio.run(H.cmd_labels(update, ctx))
    assert update.message.replies == [H.NOTHING_WAITING]


def test_labels_command_explains_the_gate(conn, clk, settings, ctx):
    update = FakeUpdate()
    asyncio.run(H.cmd_labels(update, ctx))
    assert "after the first 7 days" in update.message.replies[0]
    assert ctx.bot.sent == []


def test_labels_command_unauthorised_is_silent(conn, clk, settings, ctx):
    _started(conn, clk, settings, "Notes")
    update = FakeUpdate(uid=STRANGER)
    asyncio.run(H.cmd_labels(update, ctx))
    assert update.message.replies == [] and ctx.bot.sent == []

