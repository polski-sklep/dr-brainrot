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
#: The digest goes out on one predictable morning — Monday, inside the import
#: reminders when there are any — rather than drifting across the week on a
#: three-day clock (DR5.19). A label nudge arriving on a Friday about 34 minutes
#: spread over 50 sources was the case that changed it.
DIGEST_WEEKDAY = 0                 # Monday
#: ...and only when the undecided time is big enough to change what the page
#: says. The figure is cumulative over the diagnostic, so this reads "an hour
#: of your measured time is still undecided", not "an hour this week".
DIGEST_MIN_MINUTES = 60.0
PROMPT_KIND = "labels"

#: The labelling rule, in the same words as the dashboard's Labels page. Kept
#: here because the digest links to that page and must not describe it
#: differently; the page itself is where it is answered.
CONTENT_QUESTION = (
    "Only brain-rot content is measured. Is this content you consume? "
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


def digest_text(summary: labels.Summary, settings: Settings, *, mode: str = "message") -> str:
    """The note itself: how many, how much, where the weight is, and the link.

    Minutes are stated because they are what makes the decision worth making,
    and the biggest group is named for the same reason. Neither is a judgement
    about any source: the system still never suggests a label.

    `mode`: "message" stands alone; "folded" rides at the end of a Monday import
    reminder (DR5.19); "reply" answers /labels, which was asked for and so
    should not say anything about when the next reminder comes.
    """
    minutes = f"{round(summary.minutes):,}".replace(",", " ")
    where = ""
    if summary.top_group is not None:
        where = f", mostly {report.GROUP_TEXT.get(summary.top_group, summary.top_group).lower()}"
    one = summary.count == 1
    what = (f"{summary.count} source{'' if one else 's'} ({minutes} min) "
            f"{'is' if one else 'are'} unlabelled{where}.")
    if mode == "folded":
        return f"Also: {what} Label them in the dashboard: {labels_url(settings)}"
    text = f"{what}\n\nLabel them in the dashboard: {labels_url(settings)}"
    if mode == "reply":
        return text
    return text + "\nThis is the only reminder about it this week."


def _last_digest_day(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(sent_on) FROM prompts_sent WHERE kind = ?", (PROMPT_KIND,)).fetchone()
    return row[0] if row else None


def due_digest(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> labels.Summary | None:
    """The undecided summary when a digest is due today, else None. Sends nothing.

    Due only on a Monday, only when at least `DIGEST_MIN_MINUTES` are undecided,
    never before the diagnostic has run seven days or after it ends, and at most
    once that day.
    """
    if _gate(conn, clk, settings) is not None:
        return None
    today = T.local_date(clk.now(), settings.tz)
    if today.weekday() != DIGEST_WEEKDAY:
        return None
    summary = labels.unlabelled_summary(conn, clk, settings)
    if summary.count == 0 or summary.minutes < DIGEST_MIN_MINUTES:
        return None
    if _last_digest_day(conn) == T.iso_date(today):
        return None
    return summary


def record_digest(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> None:
    """Note that today's digest went out, however it went (alone or folded)."""
    today = T.local_date(clk.now(), settings.tz)
    conn.execute(
        "INSERT OR IGNORE INTO prompts_sent (kind, period_start, sent_on, sent_at) VALUES (?, ?, ?, ?)",
        (PROMPT_KIND, T.measurement_week(today).start, T.iso_date(today), clock.iso(clk.now())),
    )
    conn.commit()


def send_label_digest(conn: sqlite3.Connection, clk: Clock, settings: Settings, bot_like) -> list[str]:
    """Daily job: send the digest on its own when it is due and was not folded in.

    On a Monday with import reminders, `jobs` folds the note into the last of
    them and records it, so this finds it already sent and stays silent.
    """
    summary = due_digest(conn, clk, settings)
    if summary is None:
        return []
    triage.send(bot_like, settings, digest_text(summary, settings))
    record_digest(conn, clk, settings)
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
    await update.message.reply_text(digest_text(summary, settings, mode="reply"))


def register(app) -> None:
    """Add this module's handlers to the PTB Application (called by bot.build_app).

    One command and no callback handler: with in-Telegram labelling gone,
    nothing in this module answers a button, and registering a handler for a
    namespace no message uses would only make the wiring lie.
    """
    app.add_handler(CommandHandler("labels", cmd_labels))
