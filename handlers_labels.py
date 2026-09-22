"""Telegram's one line about labelling: a digest, at most every third day.

CONTRACTS.md Part II §8, §19. Labelling itself lives on the dashboard now
(DECISIONS DR5.6). The user said it plainly:

    "I don't want the Telegram bot pinging me to tag content. It can ping me
    once, maximum every 3 days, saying there is content to be tagged in the
    desktop app, but I don't want to be doing it in the Telegram app itself."

So this module no longer renders a source, a keyboard or a callback, and the
label-callback namespace it used is gone entirely. Deciding a label happens on
a page where the minutes, the provenance and the other nineteen sources are
visible at once, which is a better place to make twenty decisions than a chat
is. What survives in Telegram is the *fact* that a decision is waiting, said
once, with a link.

The gate is three days rather than a day, and it is on the digest as a whole
rather than per source: one message every third day is a reminder, one message
per source per day is the nagging the user objected to.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from telegram.ext import CommandHandler

import bot
import clock
import drbr_types as T
import evidence
import labels
import report
import triage
from clock import Clock
from config import Settings

log = logging.getLogger("drbr.labels")

#: The diagnostic must have run this long before labels are mentioned at all:
#: "after the first week of collection".
LABEL_PROMPTS_AFTER = timedelta(days=7)
#: At most one digest per this many local days (DR5.6, the user's own number).
DIGEST_EVERY = timedelta(days=3)
PROMPT_KIND = "labels"

#: The labelling rule, in the same words as the dashboard's Labels page. Kept
#: here because the digest links to that page and must not describe it
#: differently; the page itself is where it is answered.
CONTENT_QUESTION = (
    "DRBR measures brain-rot content only. Is this content you consume? "
    "If yes, is it brain rot (wasteful) or not (worthwhile)? "
    "If you only go there to get a task done — buy, pay, book, log in, organise, "
    "check the weather — it's a tool and isn't counted."
)

NOTHING_WAITING = "No sources are waiting for a label."


def labels_url(settings: Settings) -> str:
    """The dashboard page the digest points at. The only place labelling happens."""
    return f"http://127.0.0.1:{settings.dashboard_port}/labels"


# --------------------------------------------------------------------------
# The digest
# --------------------------------------------------------------------------

def _gate(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> str | None:
    """Why nothing may be said about labels now, or None if something may."""
    diag = evidence.diagnostic(conn, clk, settings)
    if not diag.started:
        return "Labels are requested after the first 7 days of data; no data has arrived yet."
    if diag.ended:
        return "The diagnostic has ended; labels are no longer requested."
    today = T.local_date(clk.now(), settings.tz)
    if today - T.parse_date(diag.start) < LABEL_PROMPTS_AFTER:  # type: ignore[arg-type]
        return "Labels are requested after the first 7 days of data."
    return None


def digest_text(summary: labels.Summary, settings: Settings) -> str:
    """The whole message: how many, how much, where the weight is, and the link.

    Minutes are stated because they are what makes the decision worth making,
    and the biggest group is named for the same reason. Neither is a judgement
    about any source: the system still never suggests a label.
    """
    minutes = f"{round(summary.minutes):,}".replace(",", " ")
    where = ""
    if summary.top_group is not None:
        where = f", mostly {report.GROUP_TEXT.get(summary.top_group, summary.top_group).lower()}"
    one = summary.count == 1
    return (f"{summary.count} source{'' if one else 's'} ({minutes} min) "
            f"{'is' if one else 'are'} unlabelled{where}.\n\n"
            f"Label them in the dashboard: {labels_url(settings)}\n"
            "Nothing to do here — this is the only message you'll get about it for three days.")


def _last_digest_day(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(sent_on) FROM prompts_sent WHERE kind = ?", (PROMPT_KIND,)).fetchone()
    return row[0] if row else None


def send_label_digest(conn: sqlite3.Connection, clk: Clock, settings: Settings, bot_like) -> list[str]:
    """Daily job: at most one message every three days, only when something is unlabelled.

    Returns one summary line if a message was sent, otherwise nothing. Silent
    before the diagnostic has run 7 days, after it has ended, within three
    local days of the last digest, and whenever every source with evidence has
    been decided.
    """
    if _gate(conn, clk, settings) is not None:
        return []
    summary = labels.unlabelled_summary(conn, clk, settings)
    if summary.count == 0:
        return []
    today = T.local_date(clk.now(), settings.tz)
    last = _last_digest_day(conn)
    if last is not None and today - T.parse_date(last) < DIGEST_EVERY:
        return []
    triage.send(bot_like, settings, digest_text(summary, settings))
    week = T.measurement_week(today).start
    conn.execute(
        "INSERT OR IGNORE INTO prompts_sent (kind, period_start, sent_on, sent_at) VALUES (?, ?, ?, ?)",
        (PROMPT_KIND, week, T.iso_date(today), clock.iso(clk.now())),
    )
    conn.commit()
    return [f"label digest: {summary.count} unlabelled source(s), {round(summary.minutes)} min"]


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

def _deps(context):
    data = context.bot_data
    return data["conn"], data["clk"], data["settings"]


@bot.authorised_only
async def cmd_labels(update, context) -> None:
    """/labels — how many are waiting, and the link. It never labels anything here."""
    conn, clk, settings = _deps(context)
    reason = _gate(conn, clk, settings)
    if reason is not None:
        await update.message.reply_text(reason)
        return
    summary = labels.unlabelled_summary(conn, clk, settings)
    if summary.count == 0:
        await update.message.reply_text(NOTHING_WAITING)
        return
    await update.message.reply_text(digest_text(summary, settings))


def register(app) -> None:
    """Add this module's handlers to the PTB Application (called by bot.build_app).

    One command and no callback handler: with in-Telegram labelling gone,
    nothing in this module answers a button, and registering a handler for a
    namespace no message uses would only make the wiring lie.
    """
    app.add_handler(CommandHandler("labels", cmd_labels))
