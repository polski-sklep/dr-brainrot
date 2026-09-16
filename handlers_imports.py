"""Telegram handlers for DRBR evidence imports, and the weekly import prompts.

CONTRACTS Part II §8. Owned by Agent B (Screen Time, Cold Turkey).

* Photos and image documents -> one Screen Time batch per burst of images.
* JSON documents -> Cold Turkey ingest.
* `send_due_prompts` -> the Monday-onwards nudges for last week's evidence.

Every reply states the Outcome honestly: how many facts were written, every
warning, or why nothing was and what to send instead. No callback buttons are
used, so the reserved `d:imp:` namespace stays empty.

Batching. Telegram delivers an album as separate updates that share a
`media_group_id`, and caps an album at 10 items; the real Screen Time capture
is exactly 10 screenshots, so a longer one arrives as two albums. Images are
therefore collected per chat and processed once the chat has been quiet for
`BATCH_QUIET_SECONDS`: an album, several albums sent back to back, or photos
sent one by one all become one capture. This install of python-telegram-bot
has no JobQueue (the optional extra is not installed), so the debounce is an
asyncio task. Its sleep is injectable, so tests release a batch explicitly
instead of waiting in real time.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Awaitable, Callable

import clock
import drbr_types as T
import evidence
import triage
from clock import Clock
from config import Settings

log = logging.getLogger("drbr.imports")

CALLBACK_PREFIX = "d:imp:"
BATCHER_KEY = "imports_batcher"
BATCH_QUIET_SECONDS = 4.0

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".webp", ".tif", ".tiff"}


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------

@dataclass
class _Pending:
    images: list[tuple[bytes, str]] = field(default_factory=list)
    received_at: str | None = None
    media_groups: set[str] = field(default_factory=set)
    message: object = None            # the latest message, replied to when the batch is processed


class ImageBatcher:
    """Collect images per key; process each burst once after a quiet period.

    `sleep` is awaited for the quiet period and may be replaced by anything
    awaitable (tests pass an event-gated coroutine). A new image during the
    wait restarts it; once processing has started, new images open a new batch.
    """

    def __init__(self, delay: float = BATCH_QUIET_SECONDS,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep) -> None:
        self.delay = delay
        self.sleep = sleep
        self._pending: dict[object, _Pending] = {}
        self._waiting: dict[object, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()

    async def add(self, key, image: tuple[bytes, str], *, received_at: str, media_group_id: str | None,
                  message, process: Callable[[_Pending], Awaitable[None]]) -> None:
        batch = self._pending.setdefault(key, _Pending())
        batch.images.append(image)
        batch.received_at = batch.received_at or received_at
        if media_group_id:
            batch.media_groups.add(str(media_group_id))
        batch.message = message
        old = self._waiting.pop(key, None)
        if old is not None:
            old.cancel()
        task = asyncio.get_running_loop().create_task(self._later(key, process))
        self._waiting[key] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _later(self, key, process) -> None:
        await self.sleep(self.delay)
        # No await between here and pop: a new image can't slip into this batch.
        self._waiting.pop(key, None)
        batch = self._pending.pop(key, None)
        if batch is not None and batch.images:
            await process(batch)

    async def drain(self) -> None:
        """Wait for every scheduled batch to finish (tests; graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def pending_count(self, key) -> int:
        batch = self._pending.get(key)
        return len(batch.images) if batch else 0


def _batcher(context) -> ImageBatcher:
    data = context.bot_data
    if BATCHER_KEY not in data:
        data[BATCHER_KEY] = ImageBatcher()
    return data[BATCHER_KEY]


# --------------------------------------------------------------------------
# Reply rendering
# --------------------------------------------------------------------------

def _period(outcome: evidence.Outcome) -> str:
    if outcome.period_start and outcome.period_end and outcome.period_start != outcome.period_end:
        return f" for {outcome.period_start} – {outcome.period_end}"
    if outcome.period_start:
        return f" for {outcome.period_start}"
    return ""


def render_outcome(what: str, outcome: evidence.Outcome, *, unit: str) -> str:
    """The honest reply: duplicate, failure + what to resend, or counts + every warning."""
    if outcome.duplicate_of is not None:
        return (f"{what}: already imported (import #{outcome.duplicate_of}, {outcome.status}). "
                "Nothing new was written.")
    if outcome.status == T.IMPORT_FAILED:
        return (f"{what}: import failed, nothing was recorded.\nReason: {outcome.error or 'unknown'}\n"
                "The file is saved and can be re-read later; send a corrected capture if the reason says so.")
    lines = [f"{what}: {outcome.measurements_written} {unit} recorded{_period(outcome)} "
             f"({'complete' if outcome.status == T.IMPORT_OK else 'partial'})."]
    if outcome.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"• {w}" for w in outcome.warnings)
    return "\n".join(lines)


async def _reply(message, text: str) -> None:
    if message is None:
        return
    # Telegram caps a message at 4096 characters; warnings can be long.
    for i in range(0, len(text), 4000):
        await message.reply_text(text[i:i + 4000])


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

def _deps(context):
    data = context.bot_data
    return data.get("settings"), data.get("conn"), data.get("clk")


def _received_at(message, clk: Clock) -> str:
    when = getattr(message, "date", None)
    return clock.iso(when) if when is not None else clock.iso(clk.now())


async def _process_screentime(context, batch: _Pending) -> None:
    from sources import screentime  # noqa: PLC0415

    settings, conn, clk = _deps(context)
    ingest = context.bot_data.get("screentime_ingest", screentime.ingest_batch)
    try:
        outcome = ingest(conn, clk, settings, batch.images, received_at=batch.received_at)
    except Exception:  # noqa: BLE001 — a crash must still be reported, never swallowed
        log.exception("screen time import crashed")
        await _reply(batch.message, "Screen Time: the import crashed before it finished. The screenshots were "
                                    "saved; nothing was recorded. The log has the details.")
        return
    text = render_outcome(f"Screen Time ({len(batch.images)} screenshot{'s' if len(batch.images) != 1 else ''})",
                          outcome, unit="app figures")
    await _reply(batch.message, text)


async def _download(file_like) -> bytes:
    tg_file = await file_like.get_file()
    return bytes(await tg_file.download_as_bytearray())


def _is_image_document(document) -> bool:
    mime = (getattr(document, "mime_type", None) or "").lower()
    name = (getattr(document, "file_name", None) or "").lower()
    return mime.startswith("image/") or any(name.endswith(ext) for ext in _IMAGE_EXTS)


def _is_json_document(document) -> bool:
    mime = (getattr(document, "mime_type", None) or "").lower()
    name = (getattr(document, "file_name", None) or "").lower()
    return name.endswith(".json") or mime in ("application/json", "text/json")


async def on_image(update, context) -> None:
    """A photo or an image file: add it to this chat's Screen Time batch."""
    message = update.effective_message
    settings, conn, clk = _deps(context)
    if message is None or clk is None:
        return
    if getattr(message, "photo", None):
        data, name = await _download(message.photo[-1]), "photo.jpg"   # largest size
    elif getattr(message, "document", None) is not None and _is_image_document(message.document):
        data, name = await _download(message.document), message.document.file_name or "image"
    else:
        return
    chat_id = getattr(getattr(message, "chat", None), "id", None) or getattr(update.effective_user, "id", None)

    async def process(batch: _Pending) -> None:
        await _process_screentime(context, batch)

    await _batcher(context).add(chat_id, (data, name), received_at=_received_at(message, clk),
                                media_group_id=getattr(message, "media_group_id", None),
                                message=message, process=process)


async def on_json_document(update, context) -> None:
    """A JSON file: a Cold Turkey export."""
    from sources import coldturkey  # noqa: PLC0415

    message = update.effective_message
    settings, conn, clk = _deps(context)
    document = getattr(message, "document", None)
    if document is None or not _is_json_document(document):
        return
    data = await _download(document)
    ingest = context.bot_data.get("coldturkey_ingest", coldturkey.ingest)
    try:
        outcome = ingest(conn, clk, settings, data, filename=document.file_name)
    except Exception:  # noqa: BLE001
        log.exception("cold turkey import crashed")
        await _reply(message, "Cold Turkey: the import crashed before it finished. The file was saved; nothing "
                              "was recorded. The log has the details.")
        return
    await _reply(message, render_outcome("Cold Turkey", outcome, unit="domain-day figures"))


def register(app) -> None:
    """Add this module's handlers to the PTB Application (called from bot.build_app)."""
    from telegram.ext import MessageHandler, filters  # noqa: PLC0415

    import bot  # noqa: PLC0415 — bot imports this module lazily; no cycle at import time

    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, bot.authorised_only(on_image)))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("json") | filters.Document.MimeType("application/json"),
        bot.authorised_only(on_json_document)))


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

PROMPT_KINDS = ("screentime", "coldturkey", "youtube")


def prompt_text(kind: str, week: T.MeasurementWeek) -> str:
    span = f"Sun {week.sunday:%d %b} – Sat {week.saturday:%d %b}"
    if kind == "screentime":
        return (f"Screen Time for last week ({span}) hasn't arrived.\n"
                "On the iPhone: Settings → Screen Time → tap this iPhone (the per-device view, not All Devices) → "
                "Week → tap ‹ once so the header reads \"Last Week\". Screenshot the whole page from the top, "
                "scrolling down, and tap Show More under Most Used until it disappears. Send all the screenshots "
                "here together (as photos or as files).")
    if kind == "coldturkey":
        return (f"Cold Turkey for last week ({span}) hasn't arrived.\n"
                "On the desktop: Cold Turkey Blocker → Statistics → the Websites usage (time) view, not "
                f"\"Websites Blocked\" → show the week {span} → Export → send the JSON file here. "
                "Export after Saturday has ended; days not yet over are not recorded. Each machine only knows its "
                "own usage, so export from every desktop you use.")
    if kind == "youtube":
        return (f"YouTube history for last week ({span}) hasn't been synced.\n"
                "On the desktop browser: open youtube.com/feed/history while signed in, click the DRBR extension "
                "and press Sync. It scrolls back to the last sync by itself.")
    raise ValueError(f"no prompt for {kind!r}")


def _covered(conn: sqlite3.Connection, kind: str, week: T.MeasurementWeek, tz: str) -> bool:
    """Is there an ok/partial import that covers this week?

    Screen Time and Cold Turkey imports carry the days they recorded, so the
    import's period must span the whole Sunday..Saturday. A YouTube sync covers
    everything watched since the previous sync, so any successful sync received
    after the week ended covers it.
    """
    if kind == "youtube":
        rows = conn.execute("SELECT received_at FROM imports WHERE kind = 'youtube_history' "
                            "AND status IN ('ok', 'partial')").fetchall()
        return any(T.local_date(r["received_at"], tz) > week.saturday for r in rows)
    return conn.execute(
        "SELECT 1 FROM imports WHERE kind = ? AND status IN ('ok', 'partial') "
        "AND period_start IS NOT NULL AND period_start <= ? AND period_end >= ? LIMIT 1",
        (kind, week.start, week.end),
    ).fetchone() is not None


def send_due_prompts(conn: sqlite3.Connection, clk: Clock, settings: Settings, bot) -> list[str]:
    """Nudge once per day, from the Monday after the week, for each missing import.

    The week asked about is the most recent complete Sunday..Saturday. Before
    the diagnostic has started this still prompts, because the first import is
    what starts it; after it has ended nothing is sent.
    """
    if evidence.diagnostic(conn, clk, settings).ended:
        return []
    today = T.local_date(clk.now(), settings.tz)
    week = T.measurement_week(T.week_sunday(today) - timedelta(days=7))
    if today < week.saturday + timedelta(days=2):      # Sunday: the Monday after hasn't come yet
        return []
    sent: list[str] = []
    for kind in PROMPT_KINDS:
        if _covered(conn, kind, week, settings.tz):
            continue
        already = conn.execute("SELECT 1 FROM prompts_sent WHERE kind = ? AND period_start = ? AND sent_on = ?",
                               (kind, week.start, today.isoformat())).fetchone()
        if already:
            continue
        triage.send(bot, settings, prompt_text(kind, week))
        conn.execute("INSERT OR IGNORE INTO prompts_sent (kind, period_start, sent_on, sent_at) VALUES (?, ?, ?, ?)",
                     (kind, week.start, today.isoformat(), clock.iso(clk.now())))
        conn.commit()
        sent.append(f"{kind} prompt for week {week.start}")
    return sent
