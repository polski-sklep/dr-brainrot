"""The Sunday check-in: one message a week saying what the week just finished
still needs from the user, and at most one reminder after it (DR5.24).

The user put the problem plainly: he hated "the disparate/broken up aspect" of
entering inputs and wanted "one concrete time to enter all inputs". Requests
used to reach him five ways on different days — Monday import reminders that
repeated for a few days and then silently gave up, a separate label nudge, the
Qbserve warning, the Sunday 18:00 triage post, and readings to settle that
only the Overview mentioned. He chose Sunday morning, so this module owns the
whole ask:

* **Sunday** (the 09:30 daily job): one numbered list, always in the same
  order — Screen Time, YouTube, (Cold Turkey, only as a fallback), readings to
  settle, labels. Labels come last because the steps before them bring new
  sources. Things already received are listed as "Already in". When nothing
  is owed, nothing is sent: by 09:30 on a Sunday that can only mean he has
  already done the session, and the bot answered each upload at the time.
* **Monday**: one reminder, only while evidence for that week is still missing
  (a cut-off Screen Time capture counts as missing). Then nothing more for
  that week; the dashboard keeps showing what is missing.

Two `meta` keys hold the state, so no table changes: `checklist_week` is the
week whose Sunday list was dealt with, `checklist_nudge_week` the week whose
one reminder was decided. Both are written only after a successful send (or
when there was nothing to send), so a Telegram failure is retried on the next
run rather than lost.

Read-only apart from those two keys. Nothing here imports `checklist` back.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

import drbr_types as T
import evidence
import labels
import report
import triage
from clock import Clock
from config import Settings

log = logging.getLogger("drbr.checklist")

CHECKLIST_KEY = "checklist_week"
NUDGE_KEY = "checklist_nudge_week"

#: Undecided label minutes that are worth a message on their own (DR5.19's bar,
#: moved here from handlers_labels). The figure is cumulative over the
#: diagnostic. Below it, labels ride along when something else is owed but
#: never cause a message by themselves.
LABELS_WORTH_A_MESSAGE_MIN = 60.0

#: What one piece of the week's evidence looks like.
MISSING = "missing"
CUT_OFF = "cut_off"     # Screen Time arrived, but the capture stopped at "Show More"
IN = "in"

#: The two mistakes that actually happened with real captures — the Most Used
#: list left ending in "Show More", and screenshots sent in two goes — are
#: steps here, not warnings after the fact.
SCREENTIME_TODO = (
    "Screen Time. On the iPhone: Settings → Screen Time → this iPhone (not All Devices) → Week, then tap ‹ "
    "once so it says \"Last Week\". Under Most Used, tap Show More until it disappears. Screenshot the whole "
    "page from the top and send them all here in one go."
)
SCREENTIME_AGAIN = (
    "Screen Time, once more. The screenshots stopped at \"Show More\", so the apps below it are missing. "
    "Under Most Used, tap Show More until it disappears, then send the whole page here in one go."
)
YOUTUBE_TODO = ("YouTube. On the computer, open youtube.com/feed/history, click the YouTube history extension "
                "and press Sync.")
COLDTURKEY_TODO = ("Cold Turkey Blocker → Statistics → Websites usage (time) → {span} → Export, and send the "
                   "JSON file here.")
COLDTURKEY_FAILING = "Cold Turkey. This Mac's statistics couldn't be read this week, so export them by hand: "
COLDTURKEY_MANUAL = "Cold Turkey. Export last week by hand: "
#: Said whenever evidence is asked for, because the original complaint was not
#: knowing what he owed at all (DR5.6): naming what is automatic is what makes
#: the list legible as the whole of it.
AUTOMATIC_LINE = "Everything else ({what}) collects itself."
LAST_REMINDER = "That's the only reminder for this week. The dashboard keeps showing what's missing: {url}"

#: How each evidence step is named when the labels line refers back to it.
_BRINGS = {"screentime": ("the screenshots", "add", "bring"),
           "youtube": ("the sync", "adds", "brings"),
           "coldturkey": ("the export", "adds", "brings")}
_EVIDENCE = ("screentime", "youtube", "coldturkey")
_NAMES = {"screentime": "Screen Time", "youtube": "YouTube", "coldturkey": "Cold Turkey"}


def url(settings: Settings, path: str) -> str:
    """A link into the local dashboard."""
    return f"http://127.0.0.1:{settings.dashboard_port}{path}"


def asking_week(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> T.MeasurementWeek | None:
    """The week just finished, Sun..Sat ending on or before yesterday, if it may be asked for.

    Never a week that ended before go-live (DR2.2), never one that starts after
    the diagnostic's last day. The diagnostic's final week is still asked for
    on the Sunday after it, although the diagnostic has ended by then: without
    that, the last week of evidence would never be requested at all. Before the
    diagnostic has started, the week is asked for as usual, because the first
    import is what starts it.
    """
    today = T.local_date(clk.now(), settings.tz)
    week = T.measurement_week(T.week_sunday(today) - timedelta(days=7))
    if settings.go_live and week.end < settings.go_live:
        return None
    diag = evidence.diagnostic(conn, clk, settings)
    if diag.started and diag.end is not None and week.start > diag.end:
        return None
    return week


@dataclass(frozen=True)
class WeekNeeds:
    """What one finished week still needs from the user. Read-only.

    `coldturkey` is None while Cold Turkey reads itself successfully: it is
    then not part of the list at all.
    """

    week: T.MeasurementWeek
    screentime: str                 # MISSING | CUT_OFF | IN
    youtube: str                    # MISSING | IN
    coldturkey: str | None          # MISSING | IN | None (automatic and healthy)
    readings: int                   # Screen Time readings waiting to be settled
    unlabelled: labels.Summary
    final: bool                     # the diagnostic's last week

    @property
    def evidence_missing(self) -> bool:
        return self.screentime != IN or self.youtube != IN or self.coldturkey == MISSING

    @property
    def owed(self) -> bool:
        return (self.evidence_missing or self.readings > 0
                or self.unlabelled.minutes >= LABELS_WORTH_A_MESSAGE_MIN)


def _self_feeding(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> set[str]:
    """Report groups currently feeding themselves, so nothing is asked for them.

    Cold Turkey became automatic at DR5.1, so asking for it by hand every week
    would be asking for work already being done. It survives only as a
    *fallback*: unconfigured, or configured and failing. `report.needs_attention`
    is the one place that judges whether an automatic source is healthy, and
    this reads its verdict rather than keeping a second opinion.
    """
    return {row.group for row in report.needs_attention(conn, clk, settings)
            if row.mode == report.MODE_AUTOMATIC and row.state == report.STATE_OK}


def _coldturkey_spans(conn: sqlite3.Connection, week: T.MeasurementWeek) -> bool:
    """Is there an ok/partial Cold Turkey import whose period spans the whole week?

    Stricter than the report's overlap rule on purpose: the fallback is asked
    for when the automatic read is failing, and a read that stopped mid-week
    has not covered it.
    """
    return conn.execute(
        "SELECT 1 FROM imports WHERE kind = 'coldturkey' AND status IN ('ok', 'partial') "
        "AND period_start IS NOT NULL AND period_start <= ? AND period_end >= ? LIMIT 1",
        (week.start, week.end),
    ).fetchone() is not None


def week_needs(conn: sqlite3.Connection, clk: Clock, settings: Settings, week: T.MeasurementWeek) -> WeekNeeds:
    """Work out what `week` still needs. Sends nothing, writes nothing."""
    st = report._covering_import(conn, settings, "screentime", week)
    if st is None:
        screentime = MISSING
    else:
        try:
            meta = json.loads(st["meta"] or "{}")
        except ValueError:
            meta = {}
        # Only an explicit False is "cut off". A capture whose end of list was
        # never seen (None) was already flagged in the import reply.
        screentime = CUT_OFF if meta.get("most_used_complete") is False else IN
    youtube = IN if report._covering_import(conn, settings, "youtube_history", week) is not None else MISSING
    coldturkey = None
    if T.GROUP_DESKTOP not in _self_feeding(conn, clk, settings):
        coldturkey = IN if _coldturkey_spans(conn, week) else MISSING
    diag = evidence.diagnostic(conn, clk, settings)
    final = bool(diag.started and diag.end is not None and week.start <= diag.end <= week.end)
    return WeekNeeds(week, screentime, youtube, coldturkey,
                     len(report.needs_your_eye(conn, clk, settings)),
                     labels.unlabelled_summary(conn, clk, settings), final)


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------

def _span(week: T.MeasurementWeek) -> str:
    """"Sun 4 – Sat 10 Oct", or "Sun 27 Sep – Sat 3 Oct" across a month."""
    sun, sat = week.sunday, week.saturday
    if sun.month == sat.month:
        return f"Sun {sun.day} – Sat {sat.day} {sat:%b}"
    return f"Sun {sun.day} {sun:%b} – Sat {sat.day} {sat:%b}"


def _evidence_todo(needs: WeekNeeds, settings: Settings) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if needs.screentime == MISSING:
        out.append(("screentime", SCREENTIME_TODO))
    elif needs.screentime == CUT_OFF:
        out.append(("screentime", SCREENTIME_AGAIN))
    if needs.youtube == MISSING:
        out.append(("youtube", YOUTUBE_TODO))
    if needs.coldturkey == MISSING:
        lead = COLDTURKEY_FAILING if settings.coldturkey_db is not None else COLDTURKEY_MANUAL
        out.append(("coldturkey", lead + COLDTURKEY_TODO.format(span=_span(needs.week))))
    return out


def _labels_item(needs: WeekNeeds, settings: Settings, steps: list[tuple[int, str]]) -> str | None:
    """The labels step, or None when there is nothing to label and nothing will bring any.

    `steps` are the (number, key) of the evidence steps above it: they bring
    new sources, which is why labelling comes last.
    """
    link = url(settings, "/labels/queue")
    count = needs.unlabelled.count
    if not steps:
        subject = add = bring = ""
    elif len(steps) == 1:
        subject, add, bring = _BRINGS[steps[0][1]]
    elif count:
        numbers = [str(n) for n, _ in steps]
        subject, add, bring = f"steps {', '.join(numbers[:-1])} and {numbers[-1]}", "add", "bring"
    else:
        subject, add, bring = "the steps above", "add", "bring"
    if count == 0:
        return f"Label any new sources {subject} {bring}: {link}" if steps else None
    minutes = f"{round(needs.unlabelled.minutes):,}".replace(",", " ")
    waiting = f"{count} {'is' if count == 1 else 'are'} waiting ({minutes} min)"
    if steps:
        waiting += f", and {subject} usually {add} a few"
    return f"Label new sources. {waiting}: {link}"


def _todo(needs: WeekNeeds, settings: Settings, *, nudge: bool) -> list[tuple[str, str]]:
    """(key, text) of every numbered step, in the fixed order."""
    out = _evidence_todo(needs, settings)
    if nudge:
        return out          # the reminder is about evidence only
    steps = [(n, key) for n, (key, _) in enumerate(out, 1)]
    if needs.readings:
        n = needs.readings
        out.append(("readings", f"Settle {n} reading{'' if n == 1 else 's'} the screenshots couldn't read "
                                f"for sure: {url(settings, '/#needs-your-eye')}"))
    item = _labels_item(needs, settings, steps)
    if item is not None:
        out.append(("labels", item))
    return out


def _automatic(needs: WeekNeeds, settings: Settings) -> str:
    what = ["Mac apps"] if settings.qbserve_db is not None else []
    if needs.coldturkey is None:
        what.append("websites")
    what += ["podcasts", "your vault"]
    return AUTOMATIC_LINE.format(what=", ".join(what))


def render(needs: WeekNeeds, settings: Settings, *, nudge: bool, late: bool = False) -> str:
    """The message. Plain text; only what is still to do is numbered.

    `nudge`: the one reminder after a Sunday list that went out — evidence
    only. `late`: the full list, sent after Sunday because Sunday's never went
    out (the Mac was off, or Telegram was down); no reminder follows it.
    """
    todo = _todo(needs, settings, nudge=nudge)
    span = _span(needs.week)
    if nudge:
        who = "The final week" if needs.final else "Last week"
        head = f"{who}, {span}, still needs{' these, in this order' if len(todo) > 1 else ''}:"
    else:
        if needs.final:
            head = f"The diagnostic has ended. One last check-in, for its final week, {span}."
        else:
            head = f"{'Check-in' if late else 'Sunday check-in'} for last week, {span}."
        if len(todo) > 1:
            head += " In this order:"
    parts = [head] + [f"{n}. {text}" for n, (_, text) in enumerate(todo, 1)]
    tail = []
    done = [_NAMES[k] for k in _EVIDENCE if getattr(needs, k) == IN]
    if done:
        tail.append(f"Already in: {', '.join(done)}.")
    if needs.evidence_missing:
        if not nudge:
            tail.append(_automatic(needs, settings))
        if nudge or late:
            tail.append(LAST_REMINDER.format(url=url(settings, f"/?week={needs.week.start}#coverage")))
    if tail:
        parts.append("\n".join(tail))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def _keys(needs: WeekNeeds, settings: Settings, *, nudge: bool) -> str:
    return ", ".join(key for key, _ in _todo(needs, settings, nudge=nudge))


def send_due(conn: sqlite3.Connection, clk: Clock, settings: Settings, bot) -> list[str]:
    """Daily job: the Sunday list, or the one reminder, when due. -> what was sent.

    | Day of the run | Sends                                                     |
    |----------------|-----------------------------------------------------------|
    | Sunday         | the list, if anything is owed                             |
    | Monday         | the reminder, if evidence is still missing; or the whole  |
    |                | list, if Sunday's never went out and anything is owed     |
    | Tue..Sat       | the reminder, only if Sunday's list went out and Monday's |
    |                | reminder was never decided (the Mac was off on Monday)    |

    Nothing is ever repeated for a week, and nothing goes out mid-week for a
    week whose Sunday list never went out: merging this on a Friday sends
    nothing off-schedule. Markers are written after a successful send, or when
    there was nothing to send.
    """
    week = asking_week(conn, clk, settings)
    if week is None:
        return []
    today = T.local_date(clk.now(), settings.tz)
    sunday = week.saturday + timedelta(days=1)
    checklist_sent = evidence.get_meta(conn, CHECKLIST_KEY) == week.start
    if today == sunday:
        if checklist_sent:
            return []
        needs = week_needs(conn, clk, settings, week)
        if not needs.owed:
            log.info("week %s is all in; nothing sent", week.start)
            evidence.set_meta(conn, CHECKLIST_KEY, week.start)
            return []
        triage.send(bot, settings, render(needs, settings, nudge=False))
        evidence.set_meta(conn, CHECKLIST_KEY, week.start)
        return [f"sunday checklist for week {week.start}: {_keys(needs, settings, nudge=False)}"]

    if evidence.get_meta(conn, NUDGE_KEY) == week.start:
        return []
    if not checklist_sent and today != sunday + timedelta(days=1):
        return []           # mid-week, and Sunday's list never went out: nothing off-schedule
    needs = week_needs(conn, clk, settings, week)
    late = not checklist_sent
    if not (needs.owed if late else needs.evidence_missing):
        log.info("week %s: nothing still missing; no reminder", week.start)
        evidence.set_meta(conn, NUDGE_KEY, week.start)
        return []
    triage.send(bot, settings, render(needs, settings, nudge=not late, late=late))
    evidence.set_meta(conn, NUDGE_KEY, week.start)
    what = "checklist" if late else "nudge"
    return [f"{today:%A}".lower() + f" {what} for week {week.start}: {_keys(needs, settings, nudge=not late)}"]
