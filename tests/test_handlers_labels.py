"""handlers_labels.py: label buttons, /labels, and the daily batch (CONTRACTS Part II §8)."""
from __future__ import annotations

import asyncio

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


def _observed(conn, clk, settings, name, day="2026-09-06"):
    s = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, name)
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=s.id, device="iphone", provenance=T.PROV_SCREENSHOT,
                                period_start=day, period_end=day, minutes=3.0)
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=day)
    return s


# --- registration -----------------------------------------------------------

def test_register_adds_callback_and_command():
    class App:
        def __init__(self):
            self.handlers = []

        def add_handler(self, h):
            self.handlers.append(h)

    app = App()
    H.register(app)
    cb = [h for h in app.handlers if isinstance(h, CallbackQueryHandler)]
    cmd = [h for h in app.handlers if isinstance(h, CommandHandler)]
    assert len(cb) == 1 and cb[0].pattern.pattern == "^d:lbl:"
    assert len(cmd) == 1 and "labels" in cmd[0].commands


def test_callback_data_fits_telegram_limit():
    data = H.callback_data(T.WORTHWHILE, 10**17)
    assert data.startswith("d:lbl:v:") and len(data.encode()) <= 64


# --- callback ---------------------------------------------------------------

@pytest.mark.parametrize("code,label", [("w", T.WASTEFUL), ("v", T.WORTHWHILE)])
def test_callback_sets_label_and_edits_message(conn, clk, settings, ctx, code, label):
    s = _observed(conn, clk, settings, "Notes")
    q = FakeQuery(f"d:lbl:{code}:{s.id}")
    asyncio.run(H.on_label_callback(FakeUpdate(query=q), ctx))
    assert labels.get_source(conn, s.id).label == label
    assert q.answers == [f"Labelled {label}."]
    assert q.edits == [f"Notes (iPhone app): labelled {label}."]
    assert labels.resolve(conn, source_id=s.id).basis == T.BASIS_SOURCE_LABEL


def test_callback_can_change_a_label(conn, clk, settings, ctx):
    s = _observed(conn, clk, settings, "Notes")
    for code in ("w", "v"):
        asyncio.run(H.on_label_callback(FakeUpdate(query=FakeQuery(f"d:lbl:{code}:{s.id}")), ctx))
    assert labels.get_source(conn, s.id).label == T.WORTHWHILE


def test_stale_source_id_is_answered_not_raised(ctx, conn):
    q = FakeQuery("d:lbl:w:424242")
    asyncio.run(H.on_label_callback(FakeUpdate(query=q), ctx))
    assert q.answers == [H.STALE] and q.edits == []


@pytest.mark.parametrize("data", ["d:lbl:x:1", "d:lbl:w:", "d:lbl:w:abc", "d:lbl:", "d:lbl:w:1:extra"])
def test_malformed_callback_is_answered(ctx, data):
    q = FakeQuery(data)
    asyncio.run(H.on_label_callback(FakeUpdate(query=q), ctx))
    assert q.answers == [H.UNRECOGNISED]


def test_unauthorised_callback_is_silently_dropped(conn, clk, settings, ctx):
    s = _observed(conn, clk, settings, "Notes")
    q = FakeQuery(f"d:lbl:v:{s.id}", uid=STRANGER)
    asyncio.run(H.on_label_callback(FakeUpdate(uid=STRANGER, query=q), ctx))
    assert q.answers == [] and q.edits == []
    assert labels.get_source(conn, s.id).label is None


def test_edit_failure_does_not_lose_the_label(conn, clk, settings, ctx):
    s = _observed(conn, clk, settings, "Notes")

    class BrokenEdit(FakeQuery):
        async def edit_message_text(self, text, **kwargs):
            raise RuntimeError("message is too old")

    q = BrokenEdit(f"d:lbl:v:{s.id}")
    asyncio.run(H.on_label_callback(FakeUpdate(query=q), ctx))
    assert labels.get_source(conn, s.id).label == T.WORTHWHILE


# --- send_label_batch gating ------------------------------------------------

def _started(conn, clk, settings, *names):
    """Diagnostic starts 2026-09-06 via the first measurement; sources observed."""
    return [_observed(conn, clk, settings, n) for n in names]


def test_batch_nothing_before_diagnostic_started(conn, clk, settings):
    labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "Notes")
    bot = SyncBot()
    assert H.send_label_batch(conn, clk, settings, bot) == [] and bot.sent == []


def test_batch_waits_seven_days(conn, clk, settings):
    _started(conn, clk, settings, "Notes")
    clk.set("2026-09-12T20:00:00+00:00")      # local 2026-09-12: day 6 after 09-06
    bot = SyncBot()
    assert H.send_label_batch(conn, clk, settings, bot) == []
    clk.set("2026-09-12T22:30:00+00:00")      # local 2026-09-13 00:30 Warsaw: 7 days
    assert len(H.send_label_batch(conn, clk, settings, bot)) == 1


def test_batch_one_message_per_source_with_two_buttons(conn, clk, settings):
    a, b = _started(conn, clk, settings, "Notes", "Maps")
    bot = SyncBot()
    sent = H.send_label_batch(conn, clk, settings, bot)
    assert len(sent) == 2 and len(bot.sent) == 2
    first = bot.sent[0]
    assert first["chat_id"] == OWNER
    assert "Notes" in first["text"] and "iPhone app" in first["text"] and "Screen Time screenshots" in first["text"]
    buttons = first["reply_markup"].inline_keyboard[0]
    assert [(x.text, x.callback_data) for x in buttons] == \
        [("Wasteful", f"d:lbl:w:{a.id}"), ("Worthwhile", f"d:lbl:v:{a.id}")]
    assert labels.get_source(conn, a.id).prompted_at == "2026-09-16T10:00:00+00:00"
    assert labels.get_source(conn, b.id).prompted_at == "2026-09-16T10:00:00+00:00"
    assert [tuple(r) for r in conn.execute("SELECT kind, period_start, sent_on FROM prompts_sent")] == \
        [("labels", "2026-09-13", "2026-09-16")]


def test_batch_dedupes_within_a_day(conn, clk, settings):
    _started(conn, clk, settings, "Notes")
    bot = SyncBot()
    assert len(H.send_label_batch(conn, clk, settings, bot)) == 1
    _observed(conn, clk, settings, "Maps")        # new source the same day
    assert H.send_label_batch(conn, clk, settings, bot) == []
    clk.advance(days=1)
    assert [m.split()[-1] for m in H.send_label_batch(conn, clk, settings, bot)] == ["Maps"]


def test_batch_nothing_after_diagnostic_ended(conn, clk, settings):
    _started(conn, clk, settings, "Notes")
    clk.set("2026-11-01T10:00:00+00:00")         # 8 weeks from 09-06 end 10-31
    bot = SyncBot()
    assert H.send_label_batch(conn, clk, settings, bot) == [] and bot.sent == []


def test_batch_with_nothing_to_ask_records_no_prompt(conn, clk, settings):
    (s,) = _started(conn, clk, settings, "Notes")
    labels.set_source_label(conn, clk, s.id, T.WASTEFUL)
    assert H.send_label_batch(conn, clk, settings, SyncBot()) == []
    assert conn.execute("SELECT COUNT(*) FROM prompts_sent").fetchone()[0] == 0


def test_seen_in_lists_vault_and_queue(conn, clk, settings):
    show = labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Synthetic Show")
    conn.execute("INSERT INTO episodes (spotify_id, title, show, description, release_date, duration_ms) "
                 "VALUES ('ep', 't', 'synthetic show', '', '2026-09-01', 1)")
    dom = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "example.org")
    for cid in ("a" * 32, "b" * 32):
        conn.execute("INSERT INTO vault_captures VALUES (?, '2026-09-15T08:00:00+00:00', 'article', 'web', ?, "
                     "NULL, 't', 'x.md', ?, '2026-09-15T08:00:00+00:00')", (cid, dom.id, "0" * 64))
    assert H._seen_in(conn, show) == "your POP queue"
    assert H._seen_in(conn, dom) == "2 vault captures"


# --- /labels ----------------------------------------------------------------

def test_labels_command_sends_next_batch_even_after_the_daily_one(conn, clk, settings, ctx):
    _started(conn, clk, settings, "Notes")
    assert len(H.send_label_batch(conn, clk, settings, SyncBot())) == 1
    _observed(conn, clk, settings, "Maps")
    update = FakeUpdate()
    asyncio.run(H.cmd_labels(update, ctx))
    assert [m["text"].splitlines()[1] for m in ctx.bot.sent] == ["Maps"]
    assert update.message.replies == []
    # Already prompted: nothing further now.
    asyncio.run(H.cmd_labels(update, ctx))
    assert update.message.replies == ["No sources are waiting for a label."]


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
    assert labels.list_sources(conn, label="unlabelled")[0].prompted_at is None
