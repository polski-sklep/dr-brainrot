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

#: Said at the end of every reminder, because the user's complaint was not that
#: one prompt was unclear but that he could not tell what he owed at all:
#: "I don't know how and when I'm supposed to be submitting sources like X
#: desktop data, desktop web, any of it" (DR5.6). Naming what is automatic is
#: what makes the two manual asks legible as the whole of the list.
AUTOMATIC_TRAILER = (
    "Everything else collects itself every morning at 09:30: desktop websites (Cold Turkey), "
    "Mac apps (Qbserve), podcasts (Spotify) and your vault. These are the only sources that "
    "can't be fetched automatically.\n"
    "Labelling is not done here any more — it's in the dashboard."
)

#: The two mistakes that actually happened with real captures: the Most Used
#: list left ending in "Show More" (so the apps below it were silently missing),
#: and screenshots sent in two goes minutes apart (read as two captures, one of
#: them an incomplete week). Both are called out as steps, not as warnings
#: after the fact.
SCREENTIME_STEPS = (
    "On the iPhone:\n"
    "1. Settings → Screen Time → tap this iPhone (the per-device view, not All Devices).\n"
    "2. Choose Week, then tap ‹ once so the header reads \"Last Week\".\n"
    "3. Under Most Used, tap Show More until it disappears. If it's still there, every app below "
    "it is missing from the week.\n"
    "4. Screenshot the whole page from the top, scrolling down.\n"
    "5. Send all the screenshots here in one go — one burst, as photos or as files. Sending some "
    "now and the rest later makes two separate captures, and neither is a whole week."
)

YOUTUBE_STEPS = (
    "On the desktop browser:\n"
    "1. Open youtube.com/feed/history, signed in.\n"
    "2. Click the YouTube history extension and press Sync. It scrolls back to the last sync by itself, so "
    "there is nothing to scroll or select."
)

COLDTURKEY_STEPS = (
    "On the desktop:\n"
    "1. Cold Turkey Blocker → Statistics → the Websites usage (time) view, not \"Websites Blocked\".\n"
    "2. Show the week {span}, then Export, and send the JSON file here.\n"
    "3. Export after Saturday has ended: days not yet over are not recorded. Each machine only "
    "knows its own usage, so export from every desktop you use."
)

MANUAL_LINE = "This is one of the two sources you have to send by hand."


def prompt_text(kind: str, week: T.MeasurementWeek, *, automated_coldturkey: bool = False) -> str:
    """The Monday reminder for one missing manual source.

    Since DR5.6 this is the *only* import reminder DRBR sends, so each message
    has to carry the whole picture on its own: what is missing, that this
    source is manual, the exact steps, and that nothing else needs the user.
    """
    span = f"Sun {week.sunday:%d %b} – Sat {week.saturday:%d %b}"
    if kind == "screentime":
        return (f"Screen Time for last week ({span}) hasn't arrived.\n{MANUAL_LINE}\n\n"
                f"{SCREENTIME_STEPS}\n\n{AUTOMATIC_TRAILER}")
    if kind == "coldturkey":
        head = (f"Cold Turkey for last week ({span}) hasn't arrived.\n"
                + ("Cold Turkey's statistics on this Mac are normally read automatically every morning, "
                   "so this means that automatic read failed. Check logs/jobs.log; exporting by hand "
                   "covers the week meanwhile."
                   if automated_coldturkey else
                   "DRBR_COLDTURKEY_DB is unset, so this Mac is not read automatically and the export "
                   "is manual."))
        return f"{head}\n\n{COLDTURKEY_STEPS.format(span=span)}\n\n{AUTOMATIC_TRAILER}"
    if kind == "youtube":
        return (f"YouTube history for last week ({span}) hasn't been synced.\n{MANUAL_LINE}\n\n"
                f"{YOUTUBE_STEPS}\n\n{AUTOMATIC_TRAILER}")
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


def _self_feeding(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> set[str]:
    """Report groups that are currently feeding themselves, so nothing is asked for them.

    Cold Turkey became automatic at DR5.1 and Qbserve is automatic by nature,
    so asking for them by hand every Monday was asking for work DRBR was
    already doing. The prompt now survives only as a *fallback*: unconfigured,
    or configured and failing. `report.needs_attention` is the one place that
    judges whether an automatic source is healthy, and this reads its verdict
    rather than keeping a second opinion.
    """
    import report  # noqa: PLC0415 — report imports allowance; lazy keeps the import graph one-way

    return {row.group for row in report.needs_attention(conn, clk, settings)
            if row.mode == report.MODE_AUTOMATIC and row.state == report.STATE_OK}


def send_due_prompts(conn: sqlite3.Connection, clk: Clock, settings: Settings, bot,
                     trailer: str | None = None) -> list[str]:
    """Nudge once per day, from the Monday after the week, for each missing manual import.

    The week asked about is the most recent complete Sunday..Saturday. Before
    the diagnostic has started this still prompts, because the first import is
    what starts it; after it has ended nothing is sent.

    Since DR5.6 this is the only import reminder there is, and it asks only for
    what the user actually has to do: a source DRBR reads for itself, and is
    reading successfully, is never in the rotation.
    """
    if evidence.diagnostic(conn, clk, settings).ended:
        return []
    today = T.local_date(clk.now(), settings.tz)
    week = T.measurement_week(T.week_sunday(today) - timedelta(days=7))
    if today < week.saturday + timedelta(days=2):      # Sunday: the Monday after hasn't come yet
        return []
    if settings.go_live and week.end < settings.go_live:
        return []                                     # a week before DRBR existed is not asked for (DR2.2)
    sent: list[str] = []
    healthy = _self_feeding(conn, clk, settings)
    due: list[str] = []
    for kind in PROMPT_KINDS:
        if _covered(conn, kind, week, settings.tz):
            continue
        if kind == "coldturkey" and T.GROUP_DESKTOP in healthy:
            continue
        already = conn.execute("SELECT 1 FROM prompts_sent WHERE kind = ? AND period_start = ? AND sent_on = ?",
                               (kind, week.start, today.isoformat())).fetchone()
        if already:
            continue
        due.append(kind)
    for i, kind in enumerate(due):
        text = prompt_text(kind, week, automated_coldturkey=settings.coldturkey_db is not None)
        if trailer and i == len(due) - 1:
            # One more note rides on the last reminder rather than arriving as
            # a message of its own (DR5.19: the Monday label digest).
            text = f"{text}\n\n{trailer}"
        triage.send(bot, settings, text)
        conn.execute("INSERT OR IGNORE INTO prompts_sent (kind, period_start, sent_on, sent_at) VALUES (?, ?, ?, ?)",
                     (kind, week.start, today.isoformat(), clock.iso(clk.now())))
        conn.commit()
        sent.append(f"{kind} prompt for week {week.start}")
    return sent
