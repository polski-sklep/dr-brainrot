"""handlers_imports: Telegram routing, album batching, honest replies.

Asking for the week's evidence moved to the Sunday check-in (tests/test_checklist.py, DR5.24).

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
from sources import ocr_vision

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


def test_screen_time_reply_points_at_readings_to_settle(settings, conn, clk):
    """DR5.24: at 09:30 the check-in can't know them yet, so the import reply says so."""
    import labels

    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="t")
    for name in ("Facebook", "Lionfish"):
        src = labels.ensure_source(conn, clk, "iphone_app", name)
        evidence.upsert_measurement(conn, rec, source_id=src.id, device="iphone", provenance="screenshot_ocr",
                                    period_start="2026-09-06", period_end="2026-09-12", minutes=None,
                                    status="unreadable", detail={"raw": [f"{name} | 2h 27m", "2h 37m"]})
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-06", period_end="2026-09-12")

    def reply_for(outcome):
        ctx, gate = make_ctx(settings, conn, clk, screentime_ingest=RecordingIngest(outcome))
        msg = photo_message(b"x")

        async def scenario():
            await hi.on_image(FakeUpdate(msg), ctx)
            gate.open()
            await ctx.bot_data[hi.BATCHER_KEY].drain()

        asyncio.run(scenario())
        return msg.replies[0]

    reply = reply_for(OK)
    assert reply.endswith("(complete).\n\n2 readings couldn't be read for sure. Settle them here: "
                          f"http://127.0.0.1:{settings.dashboard_port}/#needs-your-eye")
    # Only this import's own period counts, and never on a duplicate or a failure.
    other = evidence.Outcome(8, "ok", 3, (), None, "2026-09-13", "2026-09-19")
    assert "needs-your-eye" not in reply_for(other)
    assert "needs-your-eye" not in reply_for(evidence.Outcome(7, "ok", 0, (), None, duplicate_of=7))
    assert "needs-your-eye" not in reply_for(evidence.Outcome(9, "failed", 0, (), "boom"))


# --- a network blip while downloading (18 Oct: one screenshot of ten was lost) ------------------

class FlakyFileRef(FakeFileRef):
    """Fails `failures` times with `error`, then downloads."""

    def __init__(self, data, failures, error):
        super().__init__(data)
        self.failures, self.error, self.calls = failures, error, 0

    async def get_file(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return FakeTgFile(self.data)


def test_a_screenshot_lost_to_a_network_blip_is_retried_and_stays_in_order(settings, conn, clk, monkeypatch):
    from telegram.error import NetworkError
    monkeypatch.setattr(hi, "DOWNLOAD_RETRY_S", (0, 0, 0))
    ingest = RecordingIngest(OK)
    ctx, gate = make_ctx(settings, conn, clk, screentime_ingest=ingest)
    flaky = FlakyFileRef(b"b", 2, NetworkError("nodename nor servname provided"))
    msgs = [photo_message(b"a"), FakeMessage(photo=[flaky], media_group_id="album-1"), photo_message(b"c")]

    async def scenario():
        for m in msgs:
            await hi.on_image(FakeUpdate(m), ctx)
        gate.open()
        await ctx.bot_data[hi.BATCHER_KEY].drain()

    asyncio.run(scenario())
    assert flaky.calls == 3
    assert [[d for d, _ in images] for images, _ in ingest.calls] == [[b"a", b"b", b"c"]]


def test_a_bad_request_is_not_retried(monkeypatch):
    from telegram.error import BadRequest
    monkeypatch.setattr(hi, "DOWNLOAD_RETRY_S", (0, 0, 0))
    ref = FlakyFileRef(b"x", 5, BadRequest("file is too big"))
    with pytest.raises(BadRequest):
        asyncio.run(hi._download(ref))
    assert ref.calls == 1


def test_the_batch_waits_for_a_screenshot_still_downloading(settings, conn, clk):
    ingest = RecordingIngest(OK)
    release = asyncio.Event()

    class SlowFileRef(FakeFileRef):
        async def get_file(self):
            await release.wait()
            return FakeTgFile(self.data)

    async def yield_once(_delay):
        await asyncio.sleep(0)

    batcher = hi.ImageBatcher(delay=4.0, sleep=yield_once)
    ctx = FakeContext({"settings": settings, "conn": conn, "clk": clk, hi.BATCHER_KEY: batcher,
                       "screentime_ingest": ingest})

    async def scenario():
        slow = asyncio.create_task(hi.on_image(FakeUpdate(FakeMessage(photo=[SlowFileRef(b"b")])), ctx))
        await asyncio.sleep(0)                                    # its download is now in flight
        await hi.on_image(FakeUpdate(photo_message(b"a")), ctx)
        for _ in range(10):
            await asyncio.sleep(0)
        assert ingest.calls == []                                  # held open for the slow one
        release.set()
        await slow
        await batcher.drain()

    asyncio.run(scenario())
    assert [sorted(d for d, _ in images) for images, _ in ingest.calls] == [[b"a", b"b"]]
