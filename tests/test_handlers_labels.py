"""handlers_labels.py: the 3-day label digest and /labels (CONTRACTS Part II §8, §19).

Since DR5.6 nothing here labels anything. What is tested is the cadence (at
most one message every three days), the content of that one message, and that
no button or callback survived the move to the dashboard.
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


def test_digest_waits_seven_days(conn, clk, settings):
    _started(conn, clk, settings, "Notes")
    clk.set("2026-09-12T20:00:00+00:00")      # local 2026-09-12: day 6 after 09-06
    bot = SyncBot()
    assert H.send_label_digest(conn, clk, settings, bot) == []
    clk.set("2026-09-12T22:30:00+00:00")      # local 2026-09-13 00:30 Warsaw: 7 days
    assert len(H.send_label_digest(conn, clk, settings, bot)) == 1


def test_digest_names_the_count_the_minutes_the_group_and_the_link(conn, clk, settings):
    _observed(conn, clk, settings, "Notes", minutes=200.0)
    _observed(conn, clk, settings, "Maps", minutes=140.0)
    bot = SyncBot()
    assert H.send_label_digest(conn, clk, settings, bot) == \
        ["label digest: 2 unlabelled source(s), 340 min"]
    (text,) = bot.texts()
    assert "2 sources (340 min) are unlabelled, mostly iphone apps." in text
    assert f"http://127.0.0.1:{settings.dashboard_port}/labels" in text
    assert bot.sent[0].get("reply_markup") is None
    assert [tuple(r) for r in conn.execute("SELECT kind, period_start, sent_on FROM prompts_sent")] == \
        [("labels", "2026-09-13", "2026-09-16")]


def test_digest_is_silent_for_three_days_then_speaks_again(conn, clk, settings):
    _started(conn, clk, settings, "Notes")
    bot = SyncBot()
    assert len(H.send_label_digest(conn, clk, settings, bot)) == 1
    _observed(conn, clk, settings, "Maps")          # a new source the same day changes nothing
    for _ in range(2):                              # 17th, 18th: silent
        clk.advance(days=1)
        assert H.send_label_digest(conn, clk, settings, bot) == []
    clk.advance(days=1)                             # the 19th: three days after the 16th
    assert len(H.send_label_digest(conn, clk, settings, bot)) == 1
    clk.advance(days=1)                             # and silent again straight away
    assert H.send_label_digest(conn, clk, settings, bot) == []
    assert len(bot.sent) == 2


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
    _started(conn, clk, settings, "Notes")
    labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "NeverSeen")     # identity only
    conn.commit()
    bot = SyncBot()
    assert H.send_label_digest(conn, clk, settings, bot) == ["label digest: 1 unlabelled source(s), 3 min"]


# --- /labels ----------------------------------------------------------------

def test_labels_command_replies_with_the_count_and_a_link_and_no_buttons(conn, clk, settings, ctx):
    _observed(conn, clk, settings, "Notes", minutes=12.0)
    update = FakeUpdate()
    asyncio.run(H.cmd_labels(update, ctx))
    (reply,) = update.message.replies
    assert "1 source (12 min) is unlabelled" in reply
    assert f"http://127.0.0.1:{settings.dashboard_port}/labels" in reply
    assert ctx.bot.sent == []


def test_labels_command_does_not_consume_the_three_day_slot(conn, clk, settings, ctx):
    """Asking is not being told: /labels must not silence the next digest."""
    _started(conn, clk, settings, "Notes")
    asyncio.run(H.cmd_labels(FakeUpdate(), ctx))
    assert conn.execute("SELECT COUNT(*) FROM prompts_sent").fetchone()[0] == 0
    assert len(H.send_label_digest(conn, clk, settings, SyncBot())) == 1


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

