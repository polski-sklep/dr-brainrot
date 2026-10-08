"""/labels: how many sources are waiting for a label, and the link.

CONTRACTS.md Part II §8, §19, §21. Labelling itself lives on the dashboard
(DECISIONS DR5.6). The user said it plainly:

    "I don't want the Telegram bot pinging me to tag content. It can ping me
    once, maximum every 3 days, saying there is content to be tagged in the
    desktop app, but I don't want to be doing it in the Telegram app itself."

So this module renders no source, no keyboard and no callback. Deciding a
label happens on a page where the minutes, the provenance and the other
nineteen sources are visible at once.

It no longer sends anything of its own either. The label request is the last
item of the Sunday check-in (`checklist.py`, DR5.24), because the steps before
it bring new sources and because the user asked for one concrete time to enter
everything; a separate Monday label message (DR5.19) was one of the five ways
requests reached him on different days. What is left here is the answer to
`/labels`, asked for and so worded without any talk of reminders.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from telegram.ext import CommandHandler

import bot
import checklist
import drbr_types as T
import evidence
import labels
import report
from clock import Clock
from config import Settings

log = logging.getLogger("drbr.labels")

#: The diagnostic must have run this long before /labels answers with a count:
#: "after the first week of collection".
LABEL_PROMPTS_AFTER = timedelta(days=7)

#: The labelling rule, in the same words as the dashboard's Labels page. Kept
#: here because the reply links to that page and must not describe it
#: differently; the page itself is where it is answered.
CONTENT_QUESTION = (
    "Only brain-rot content is measured. Is this content you consume? "
    "If yes, is it brain rot (wasteful) or not (worthwhile)? "
    "If you only go there to get a task done — buy, pay, book, log in, organise, "
    "check the weather — it's a tool and isn't counted."
)

NOTHING_WAITING = "No sources are waiting for a label."


def labels_url(settings: Settings) -> str:
    """The dashboard page the reply points at. The only place labelling happens."""
    return f"http://127.0.0.1:{settings.dashboard_port}/labels"


def _gate(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> str | None:
    """Why /labels has nothing to say now, or None if it may answer.

    After the diagnostic ends it still answers while its final week is being
    asked for (the Sunday and the days after it, DR5.24), since that check-in
    ends with labelling.
    """
    diag = evidence.diagnostic(conn, clk, settings)
    if not diag.started:
        return "Labels are requested after the first 7 days of data; no data has arrived yet."
    if diag.ended:
        if checklist.asking_week(conn, clk, settings) is None:
            return "The diagnostic has ended; labels are no longer requested."
        return None
    today = T.local_date(clk.now(), settings.tz)
    if today - T.parse_date(diag.start) < LABEL_PROMPTS_AFTER:  # type: ignore[arg-type]
        return "Labels are requested after the first 7 days of data."
    return None


def digest_text(summary: labels.Summary, settings: Settings) -> str:
    """The /labels reply: how many, how much, where the weight is, and the link.

    Minutes are stated because they are what makes the decision worth making,
    and the biggest group is named for the same reason. Neither is a judgement
    about any source: the system still never suggests a label.
    """
    minutes = f"{round(summary.minutes):,}".replace(",", " ")
    where = ""
    if summary.top_group is not None:
        where = f", mostly {report.GROUP_TEXT.get(summary.top_group, summary.top_group).lower()}"
    one = summary.count == 1
    what = (f"{summary.count} source{'' if one else 's'} ({minutes} min) "
            f"{'is' if one else 'are'} unlabelled{where}.")
    return f"{what}\n\nLabel them in the dashboard: {labels_url(settings)}"


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
