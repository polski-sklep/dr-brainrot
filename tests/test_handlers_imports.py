"""handlers_imports: Telegram routing, album batching, honest replies, weekly prompts.

No network and no real Telegram objects: updates, messages and files are
fakes, as in tests/test_bot.py. Batches are released through an injected
sleep gate, so nothing waits in real time.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import datetime, timezone

import pytest

import bot
import clock
import config
import db
import evidence
import handlers_imports as hi
from sources import coldturkey, ocr_vision

AUTHORISED = 1
INTRUDER = 999
TZ = "Europe/Warsaw"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def vision_is_off_limits(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a unit test reached Apple Vision")
    monkeypatch.setattr(ocr_vision, "recognize", boom)
    monkeypatch.setattr(ocr_vision, "recognize_region", boom)


# --- fakes ---------------------------------------------------------------------------

class FakeTgFile:
    def __init__(self, data: bytes):
        self.data = data

    async def download_as_bytearray(self):
        return bytearray(self.data)


class FakeFileRef:
    def __init__(self, data: bytes, file_name=None, mime_type=None):
        self.data, self.file_name, self.mime_type = data, file_name, mime_type

    async def get_file(self):
        return FakeTgFile(self.data)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    def __init__(self, *, photo=None, document=None, media_group_id=None, date=None, chat_id=AUTHORISED):
        self.photo = photo or []
        self.document = document
        self.media_group_id = media_group_id
        self.date = date or datetime(2026, 9, 16, 10, 11, tzinfo=timezone.utc)
        self.chat = FakeChat(chat_id)
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, message, uid=AUTHORISED):
        self.effective_user = FakeUser(uid)
        self.effective_message = message
        self.message = message


class FakeContext:
    def __init__(self, bot_data):
        self.bot_data = bot_data


class Gate:
    """An injectable 'sleep' that returns only when the test opens it."""

    def __init__(self):
        self.event = None

    async def sleep(self, _delay):
        if self.event is None:
            self.event = asyncio.Event()
        await self.event.wait()

    def open(self):
        if self.event is None:
            self.event = asyncio.Event()
        self.event.set()


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)


def photo_message(data: bytes, group="album-1", **kw):
    return FakeMessage(photo=[FakeFileRef(b"thumb"), FakeFileRef(data)], media_group_id=group, **kw)


# --- fixtures ----------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", telegram_user_id=AUTHORISED)


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-16T10:12:00+00:00")


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


class RecordingIngest:
    def __init__(self, outcome):
        self.calls = []
        self.outcome = outcome

    def __call__(self, conn, clk, settings, images, *, received_at):
        self.calls.append((images, received_at))
        return self.outcome


def make_ctx(settings, conn, clk, **extra):
    gate = Gate()
    data = {"settings": settings, "conn": conn, "clk": clk,
            hi.BATCHER_KEY: hi.ImageBatcher(delay=4.0, sleep=gate.sleep)}
    data.update(extra)
    return FakeContext(data), gate


OK = evidence.Outcome(7, "ok", 39, (), None, "2026-09-06", "2026-09-12")


# --- batching and routing ----------------------------------------------------------------------

def test_album_is_processed_as_one_batch(settings, conn, clk):
    ingest = RecordingIngest(OK)
    ctx, gate = make_ctx(settings, conn, clk, screentime_ingest=ingest)
    handler = bot.authorised_only(hi.on_image)
    messages = [photo_message(f"shot-{i}".encode()) for i in range(3)]

    async def scenario():
        for m in messages:
            await handler(FakeUpdate(m), ctx)
        await asyncio.sleep(0)
        assert ingest.calls == []                      # still waiting for the album to finish
        assert ctx.bot_data[hi.BATCHER_KEY].pending_count(AUTHORISED) == 3
        gate.open()
        await ctx.bot_data[hi.BATCHER_KEY].drain()

    asyncio.run(scenario())
    assert len(ingest.calls) == 1
    images, received_at = ingest.calls[0]
    assert [d for d, _ in images] == [b"shot-0", b"shot-1", b"shot-2"]     # largest photo size, in order
    assert received_at == "2026-09-16T10:11:00+00:00"
    assert messages[-1].replies and "39 app figures recorded for 2026-09-06 – 2026-09-12" in messages[-1].replies[0]
    assert messages[0].replies == []


def test_back_to_back_albums_and_image_documents_join_one_batch(settings, conn, clk):
    ingest = RecordingIngest(OK)
    ctx, gate = make_ctx(settings, conn, clk, screentime_ingest=ingest)
    msgs = [photo_message(b"a", group="g1"), photo_message(b"b", group="g2"),
            FakeMessage(document=FakeFileRef(b"c", "IMG_1.PNG", "image/png"))]

    async def scenario():
        for m in msgs:
            await hi.on_image(FakeUpdate(m), ctx)
        gate.open()
        await ctx.bot_data[hi.BATCHER_KEY].drain()

    asyncio.run(scenario())
    assert len(ingest.calls) == 1
    assert [(d, n) for d, n in ingest.calls[0][0]] == [(b"a", "photo.jpg"), (b"b", "photo.jpg"), (b"c", "IMG_1.PNG")]


def test_unauthorised_images_are_ignored_silently(settings, conn, clk):
    ingest = RecordingIngest(OK)
    ctx, gate = make_ctx(settings, conn, clk, screentime_ingest=ingest)
    msg = photo_message(b"x")

    async def scenario():
        await bot.authorised_only(hi.on_image)(FakeUpdate(msg, uid=INTRUDER), ctx)
        gate.open()
        await ctx.bot_data[hi.BATCHER_KEY].drain()

    asyncio.run(scenario())
    assert ingest.calls == [] and msg.replies == []
    assert ctx.bot_data[hi.BATCHER_KEY].pending_count(AUTHORISED) == 0


def test_unauthorised_json_is_ignored_silently(settings, conn, clk):
    msg = FakeMessage(document=FakeFileRef((FIXTURES / "coldturkey" / "website_statistics.json").read_bytes(),
                                           "export.json", "application/json"))
    asyncio.run(bot.authorised_only(hi.on_json_document)(FakeUpdate(msg, uid=INTRUDER), make_ctx(settings, conn, clk)[0]))
    assert msg.replies == []
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_json_document_goes_to_cold_turkey_and_replies_honestly(settings, conn):
    monday = clock.FrozenClock("2026-09-21T07:00:00+00:00")
    ctx, _ = make_ctx(settings, conn, monday)
    data = (FIXTURES / "coldturkey" / "website_statistics.json").read_bytes()
    msg = FakeMessage(document=FakeFileRef(data, "Cold Turkey Blocker Website Statistics.json", "application/json"))
    asyncio.run(bot.authorised_only(hi.on_json_document)(FakeUpdate(msg), ctx))
    assert msg.replies == ["Cold Turkey: 35 domain-day figures recorded for 2026-09-13 – 2026-09-19 (complete)."]
    row = conn.execute("SELECT kind, status FROM imports").fetchone()
    assert (row["kind"], row["status"]) == ("coldturkey", "ok")

    again = FakeMessage(document=FakeFileRef(data, "copy.json", "application/octet-stream"))
    asyncio.run(hi.on_json_document(FakeUpdate(again), ctx))
    assert "already imported" in again.replies[0]


def test_blocked_count_json_reply_says_what_to_send(settings, conn, clk):
    ctx, _ = make_ctx(settings, conn, clk)
    msg = FakeMessage(document=FakeFileRef((FIXTURES / "coldturkey" / "websites_blocked.json").read_bytes(),
                                           "Cold Turkey Blocker Statistics.json", "application/json"))
    asyncio.run(hi.on_json_document(FakeUpdate(msg), ctx))
    assert "import failed, nothing was recorded" in msg.replies[0]
    assert "Export the Websites usage statistics instead" in msg.replies[0]


def test_screentime_end_to_end_through_the_handler(settings, conn, clk):
    """The real ingest with recorded OCR: a This Week capture is kept, writes nothing, and says why."""
    from functools import partial

    from sources import screentime
    from sources.ocr_vision import TextObservation as TO

    pages = [[TO.from_dict(o) for o in p]
             for p in json.loads((FIXTURES / "screentime" / "this_week.json").read_text())["pages"]]
    by_bytes = {f"shot-{i}".encode(): p for i, p in enumerate(pages)}
    ingest = partial(screentime.ingest_batch, ocr=lambda b: by_bytes[b], region_ocr=lambda *a: [])
    ctx, gate = make_ctx(settings, conn, clk, screentime_ingest=ingest)
    msgs = [photo_message(f"shot-{i}".encode()) for i in range(10)]

    async def scenario():
        for m in msgs:
            await hi.on_image(FakeUpdate(m), ctx)
        gate.open()
        await ctx.bot_data[hi.BATCHER_KEY].drain()

    asyncio.run(scenario())
    reply = msgs[-1].replies[0]
    assert reply.startswith("Screen Time (10 screenshots): 0 app figures recorded (partial).")
    assert "This Week" in reply and "Last Week" in reply
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


def test_crash_in_ingest_is_reported_not_swallowed(settings, conn, clk):
    def crash(*a, **k):
        raise RuntimeError("bug")
    ctx, gate = make_ctx(settings, conn, clk, screentime_ingest=crash)
    msg = photo_message(b"x")

    async def scenario():
        await hi.on_image(FakeUpdate(msg), ctx)
        gate.open()
        await ctx.bot_data[hi.BATCHER_KEY].drain()

    asyncio.run(scenario())
    assert "crashed" in msg.replies[0] and "nothing was recorded" in msg.replies[0]


# --- reply text ------------------------------------------------------------------------------

def test_render_outcome_ok_partial_failed_duplicate():
    assert hi.render_outcome("Screen Time", OK, unit="app figures") == (
        "Screen Time: 39 app figures recorded for 2026-09-06 – 2026-09-12 (complete).")
    partial = evidence.Outcome(8, "partial", 38, ("A row had no name.",), None, "2026-09-06", "2026-09-12")
    text = hi.render_outcome("Screen Time", partial, unit="app figures")
    assert "(partial)" in text and "Warnings:\n• A row had no name." in text
    failed = evidence.Outcome(9, "failed", 0, (), "text recognition failed (RuntimeError: x)")
    text = hi.render_outcome("Screen Time", failed, unit="app figures")
    assert "import failed, nothing was recorded" in text and "RuntimeError" in text
    dup = evidence.Outcome(7, "ok", 0, (), None, duplicate_of=7)
    assert "already imported (import #7" in hi.render_outcome("Cold Turkey", dup, unit="x")


def test_register_adds_photo_document_and_json_handlers():
    from telegram.ext import Application

    app = Application.builder().token("0000000000:TEST-TOKEN-NOT-REAL-DO-NOT-USE-EVER").build()
    hi.register(app)
    handlers = app.handlers[0]
    assert len(handlers) == 2
    assert all(h.callback.__wrapped__ in (hi.on_image, hi.on_json_document) for h in handlers)


# --- prompts ---------------------------------------------------------------------------------------

def test_prompts_from_monday_once_per_day(settings, conn):
    fake = FakeBot()
    monday = clock.FrozenClock("2026-09-14T07:00:00+00:00")
    sent = hi.send_due_prompts(conn, monday, settings, fake)
    assert sent == ["screentime prompt for week 2026-09-06", "coldturkey prompt for week 2026-09-06",
                    "youtube prompt for week 2026-09-06"]
    assert fake.sent[0]["chat_id"] == AUTHORISED
    assert "Last Week" in fake.sent[0]["text"] and "not All Devices" in fake.sent[0]["text"]
    assert "Export" in fake.sent[1]["text"] and "Websites Blocked" in fake.sent[1]["text"]
    assert hi.send_due_prompts(conn, monday, settings, fake) == []            # deduped the same day
    monday.advance(days=1)
    assert len(hi.send_due_prompts(conn, monday, settings, fake)) == 3        # again the next day
    assert conn.execute("SELECT COUNT(*) FROM prompts_sent").fetchone()[0] == 6


def test_no_prompt_on_the_sunday_the_week_ends(settings, conn):
    sunday = clock.FrozenClock("2026-09-13T09:00:00+00:00")
    assert hi.send_due_prompts(conn, sunday, settings, FakeBot()) == []


def test_covered_week_is_not_prompted(settings, conn):
    monday = clock.FrozenClock("2026-09-21T07:00:00+00:00")
    coldturkey.ingest(conn, monday, settings, (FIXTURES / "coldturkey" / "website_statistics.json").read_bytes())
    rec = evidence.begin_import(conn, monday, kind="youtube_history", parser_version="t")
    evidence.finish_import(conn, monday, settings, rec, period_start="2026-09-15", period_end="2026-09-18")
    sent = hi.send_due_prompts(conn, monday, settings, FakeBot())
    assert sent == ["screentime prompt for week 2026-09-13"]


def test_partial_this_week_capture_does_not_cover_the_week(settings, conn):
    monday = clock.FrozenClock("2026-09-21T07:00:00+00:00")
    rec = evidence.begin_import(conn, monday, kind="screentime", parser_version="t")
    evidence.finish_import(conn, monday, settings, rec, period_start=None, period_end=None, status="partial")
    assert "screentime prompt for week 2026-09-13" in hi.send_due_prompts(conn, monday, settings, FakeBot())


def test_prompts_stop_after_the_diagnostic_ends(settings, conn):
    evidence.ensure_diagnostic_start(conn, "2026-07-01")
    after_end = clock.FrozenClock("2026-09-14T07:00:00+00:00")     # 8 weeks from 1 Jul ended 25 Aug
    assert evidence.diagnostic(conn, after_end, settings).ended
    fake = FakeBot()
    assert hi.send_due_prompts(conn, after_end, settings, fake) == [] and fake.sent == []


def test_prompts_before_the_diagnostic_has_started(settings, conn):
    monday = clock.FrozenClock("2026-09-14T07:00:00+00:00")
    assert not evidence.diagnostic(conn, monday, settings).started
    assert len(hi.send_due_prompts(conn, monday, settings, FakeBot())) == 3
