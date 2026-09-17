"""Telegram handlers for DRBR source labels (CONTRACTS.md Part II §5, §8).

The brief bootstraps labels from data rather than a spreadsheet up front:
after the first week of collection the bot shows each newly seen source with
two buttons, and one tap is the whole decision. The user labels; this module
only asks and records. It never suggests a label and never orders sources by
how much they were used.

Callback data: ``d:lbl:<w|v>:<source_id>`` (w = wasteful, v = worthwhile).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

import bot
import clock
import drbr_types as T
import evidence
import labels
import triage
from clock import Clock
from config import Settings

log = logging.getLogger("drbr.labels")

CALLBACK_PREFIX = "d:lbl:"
_CALLBACK_RE = re.compile(r"^d:lbl:([wv]):(\d{1,18})$")
_CODE_TO_LABEL = {"w": T.WASTEFUL, "v": T.WORTHWHILE}
_LABEL_TO_CODE = {v: k for k, v in _CODE_TO_LABEL.items()}

#: The diagnostic must have run this long before the bot asks for labels:
#: "after the first week of collection".
LABEL_PROMPTS_AFTER = timedelta(days=7)
PROMPT_KIND = "labels"

_KIND_TEXT = {
    T.KIND_PODCAST_SHOW: "Podcast show",
    T.KIND_YOUTUBE_CHANNEL: "YouTube channel",
    T.KIND_IPHONE_APP: "iPhone app",
    T.KIND_WEB_DOMAIN: "Website (desktop)",
    T.KIND_X: "X",
    T.KIND_DESKTOP_APP: "Mac app",
}

_PROVENANCE_SEEN = {
    T.PROV_PAGE: "YouTube history",
    T.PROV_SCREENSHOT: "Screen Time screenshots",
    T.PROV_EXPORT: "Cold Turkey export",
    T.PROV_APP_TRACKER: "Qbserve (Mac apps)",
}

UNRECOGNISED = "Unrecognised button."
STALE = "That source no longer exists."


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def callback_data(label: str, source_id: int) -> str:
    return f"{CALLBACK_PREFIX}{_LABEL_TO_CODE[label]}:{source_id}"


def _keyboard(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Wasteful", callback_data=callback_data(T.WASTEFUL, source_id)),
        InlineKeyboardButton("Worthwhile", callback_data=callback_data(T.WORTHWHILE, source_id)),
    ]])


def _seen_in(conn: sqlite3.Connection, source: labels.Source) -> str:
    """Where this source was observed, from stored rows only — never how much."""
    places = [
        _PROVENANCE_SEEN.get(r["provenance"], r["provenance"])
        for r in conn.execute(
            "SELECT DISTINCT provenance FROM measurements WHERE source_id = ? ORDER BY provenance",
            (source.id,))
    ]
    n = conn.execute("SELECT COUNT(*) FROM vault_captures WHERE source_id = ?", (source.id,)).fetchone()[0]
    if n:
        places.append(f"{n} vault capture{'s' if n != 1 else ''}")
    if source.kind in (T.KIND_PODCAST_SHOW, T.KIND_YOUTUBE_CHANNEL) and _in_queue(conn, source):
        places.append("your POP queue")
    return ", ".join(places) or "unknown"


def _in_queue(conn: sqlite3.Connection, source: labels.Source) -> bool:
    for row in conn.execute("SELECT DISTINCT platform, show, source_key FROM episodes"):
        try:
            if row["platform"] == "youtube":
                key = labels.normalise_key(T.KIND_YOUTUBE_CHANNEL, row["source_key"] or "")
            else:
                key = labels.normalise_key(T.KIND_PODCAST_SHOW, row["show"] or "")
        except labels.LabelError:
            continue
        if key == (source.kind, source.source_key):
            return True
    return False


def render_prompt(conn: sqlite3.Connection, source: labels.Source) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"Label this source?\n"
        f"{source.display_name}\n"
        f"{_KIND_TEXT.get(source.kind, source.kind)} · seen in {_seen_in(conn, source)}\n"
        f"Until you choose, it counts as wasteful."
    )
    return text, _keyboard(source.id)


def _labelled_text(source: labels.Source) -> str:
    return f"{source.display_name} ({_KIND_TEXT.get(source.kind, source.kind)}): labelled {source.label}."


# --------------------------------------------------------------------------
# The batch (job entry point)
# --------------------------------------------------------------------------

def _gate(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> str | None:
    """Why no batch may be sent now, or None if one may."""
    diag = evidence.diagnostic(conn, clk, settings)
    if not diag.started:
        return "Labels are requested after the first 7 days of data; no data has arrived yet."
    if diag.ended:
        return "The diagnostic has ended; labels are no longer requested."
    today = T.local_date(clk.now(), settings.tz)
    if today - T.parse_date(diag.start) < LABEL_PROMPTS_AFTER:  # type: ignore[arg-type]
        return "Labels are requested after the first 7 days of data."
    return None


def _send_batch(conn: sqlite3.Connection, clk: Clock, settings: Settings, bot_like, *,
                once_per_day: bool) -> list[str]:
    if _gate(conn, clk, settings) is not None:
        return []
    today = T.local_date(clk.now(), settings.tz)
    week = T.measurement_week(today).start
    if once_per_day and conn.execute(
        "SELECT 1 FROM prompts_sent WHERE kind = ? AND period_start = ? AND sent_on = ?",
        (PROMPT_KIND, week, T.iso_date(today)),
    ).fetchone():
        return []
    sent: list[str] = []
    for source in labels.sources_needing_label(conn, clk, settings):
        text, markup = render_prompt(conn, source)
        triage.send(bot_like, settings, text, reply_markup=markup)
        # Marked one at a time: if a later send raises, the ones already
        # delivered are not asked about again tomorrow.
        labels.mark_prompted(conn, clk, [source.id])
        sent.append(f"label prompt: {source.kind} {source.display_name}")
    if sent:
        conn.execute(
            "INSERT OR IGNORE INTO prompts_sent (kind, period_start, sent_on, sent_at) VALUES (?, ?, ?, ?)",
            (PROMPT_KIND, week, T.iso_date(today), clock.iso(clk.now())),
        )
        conn.commit()
    return sent


def send_label_batch(conn: sqlite3.Connection, clk: Clock, settings: Settings, bot_like) -> list[str]:
    """Daily job: one message per source needing a label, two buttons each.

    Does nothing before the diagnostic has run 7 days, after it has ended, or
    if a batch already went out today. Returns one summary per message sent.
    """
    return _send_batch(conn, clk, settings, bot_like, once_per_day=True)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

def _deps(context):
    data = context.bot_data
    return data["conn"], data["clk"], data["settings"]


@bot.authorised_only
async def on_label_callback(update, context) -> None:
    query = update.callback_query
    m = _CALLBACK_RE.match(query.data or "")
    if not m:
        await query.answer(UNRECOGNISED)
        return
    conn, clk, settings = _deps(context)
    source_id = int(m.group(2))
    try:
        source = labels.set_source_label(conn, clk, source_id, _CODE_TO_LABEL[m.group(1)])
    except labels.LabelError:
        await query.answer(STALE)
        return
    # Debt is computed from labels as they are now. A relabel changes which
    # past minutes were wasteful, so recompute at once rather than waiting for
    # the next import to trigger it (DECISIONS DR2.7).
    import allowance  # noqa: PLC0415

    allowance.on_measurements_changed(conn, clk, settings)
    await query.answer(f"Labelled {source.label}.")
    try:
        await query.edit_message_text(_labelled_text(source))
    except Exception:  # noqa: BLE001 — the label is stored; a stale message is cosmetic
        log.warning("could not edit label prompt for source %s", source_id)


class _Collector:
    """A bot double for the sync batch code, so the async command can send for real.

    `triage.send` drives a coroutine with asyncio.run, which cannot happen
    inside the running handler loop; collecting first and awaiting after keeps
    one batch implementation for both the job and the command.
    """

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


@bot.authorised_only
async def cmd_labels(update, context) -> None:
    """/labels — send the next batch now rather than waiting for the daily job."""
    conn, clk, settings = _deps(context)
    reason = _gate(conn, clk, settings)
    if reason is not None:
        await update.message.reply_text(reason)
        return
    collector = _Collector()
    sent = _send_batch(conn, clk, settings, collector, once_per_day=False)
    if not sent:
        await update.message.reply_text("No sources are waiting for a label.")
        return
    for kwargs in collector.messages:
        await context.bot.send_message(**kwargs)


def register(app) -> None:
    """Add this module's handlers to the PTB Application (called by bot.build_app)."""
    app.add_handler(CallbackQueryHandler(on_label_callback, pattern=r"^d:lbl:"))
    app.add_handler(CommandHandler("labels", cmd_labels))
