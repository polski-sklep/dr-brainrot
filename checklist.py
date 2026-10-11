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
* **A cut-off capture of an older week** (Most Used stopped at "Show More")
  is asked for once more, in one check-in and its reminder, while the iPhone
  still has that week (`SCREENTIME_WEEKS_BACK`).
* **Missed Sunday and Monday** (the Mac asleep, or Telegram down both
  mornings): the whole list goes, late, on the first day this week the job
  runs. Only before the check-in has ever run (merged mid-week) does a week
  go unasked, and the log says so.

Three `meta` keys hold the state, so no table changes: `checklist_week` is the
week whose Sunday list was dealt with, `checklist_nudge_week` the week whose
one reminder was decided, `checklist_recapture` which cut-off weeks were asked
for once more, and in which check-in. All are written only after a successful
send (or when there was nothing to send), so a Telegram failure is retried on
the next run rather than lost.

Read-only apart from those keys. Nothing here imports `checklist` back.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

import dr_brainrot_types as T
import evidence
import labels
import report
import triage
from clock import Clock
from config import Settings

log = logging.getLogger("dr-brainrot.checklist")

CHECKLIST_KEY = "checklist_week"
NUDGE_KEY = "checklist_nudge_week"
RECAPTURE_KEY = "checklist_recapture"

#: How far back the iPhone's Screen Time goes, in taps of ‹ from This Week:
#: this week and the three before it ("about four weeks", README). A cut-off
#: capture of a week further back can't be taken again, so it is never asked for.
SCREENTIME_WEEKS_BACK = 3

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
#: An older week, cut off the same way, while the iPhone still has it.
SCREENTIME_OLDER = (
    "Screen Time for {span}, once more: those screenshots stopped at \"Show More\". In Week, tap ‹ {taps} "
    "to reach that week. Tap Show More until it disappears, then send the whole page here in one go."
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
#: No "this week": the reminder is about last week, sent in the new one.
LAST_REMINDER = "That's the only reminder. The dashboard keeps showing what's missing: {url}"
#: The one Audible line there ever is (DR5.28), un-numbered and never a reason
#: to send: audiobooks are optional and read themselves. Said only when Audible
#: refused the sign-in, because then nothing but him can fix it.
AUDIBLE_SIGNIN_LINE = ("Audiobooks aren't being read: the Audible sign-in needs redoing. When you have a "
                       "minute, in Terminal: cd ~/Projects/dr-brainrot && .venv/bin/python audible_login.py --again")

#: How each evidence step is named when the labels line refers back to it.
_BRINGS = {"screentime": ("the screenshots", "add", "bring"),
           "recapture": ("the screenshots", "add", "bring"),
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
    then not part of the list at all. `recapture` holds older weeks whose
    Screen Time capture was cut off and can still be taken again.
    `audible_signin` is true when Audible refused the saved sign-in: the list
    then says so in one un-numbered line, but it is never owed (DR5.28).
    """

    week: T.MeasurementWeek
    screentime: str                 # MISSING | CUT_OFF | IN
    youtube: str                    # MISSING | IN
    coldturkey: str | None          # MISSING | IN | None (automatic and healthy)
    readings: int                   # Screen Time readings waiting to be settled
    unlabelled: labels.Summary
    final: bool                     # the diagnostic's last week
    recapture: tuple[T.MeasurementWeek, ...] = ()
    audible_signin: bool = False    # the Audible sign-in needs redoing; mentioned, never owed

    @property
    def evidence_missing(self) -> bool:
        return (self.screentime != IN or self.youtube != IN or self.coldturkey == MISSING
                or bool(self.recapture))

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


def _coldturkey_in(conn: sqlite3.Connection, settings: Settings, week: T.MeasurementWeek) -> bool:
    """Is there an ok/partial Cold Turkey import, received after the week ended, that overlaps it?

    Overlap, as the dashboard counts it (`report._covering_import`): an
    import's period runs from its first to its last day *with browsing*, so an
    export of Sun–Sat with a quiet Sunday starts on Monday, and a rule that
    wanted the whole week spanned asked for it again after it was sent.
    Received after the Saturday, because the fallback is asked for when the
    automatic read is failing, and a read made mid-week has not covered the
    rest of it. A read after the week covers all of it (eight days back).
    """
    after_saturday = report._local_midnight(week.saturday + timedelta(days=1), settings.tz)
    return conn.execute(
        "SELECT 1 FROM imports WHERE kind = 'coldturkey' AND status IN ('ok', 'partial') "
        "AND received_at >= ? AND period_start IS NOT NULL AND period_end IS NOT NULL "
        "AND period_start <= ? AND period_end >= ? LIMIT 1",
        (after_saturday, week.end, week.start),
    ).fetchone() is not None


def _screentime(conn: sqlite3.Connection, settings: Settings, week: T.MeasurementWeek) -> str:
    """MISSING, CUT_OFF or IN, from the latest Screen Time import covering `week`."""
    st = report._covering_import(conn, settings, "screentime", week)
    if st is None:
        return MISSING
    # Only an explicit False is "cut off". A capture whose end of list was
    # never seen (None) was already flagged in the import reply, and one cut
    # off below a tiny app hides nothing worth asking for (DR5.30).
    return CUT_OFF if report.screentime_list_complete(conn, st) is False else IN


def _asked_again(conn: sqlite3.Connection) -> dict[str, str]:
    """{cut-off week start: the asking week whose check-in asked for it once more}."""
    try:
        value = json.loads(evidence.get_meta(conn, RECAPTURE_KEY) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _taps_back(clk: Clock, settings: Settings, week: T.MeasurementWeek) -> int:
    """Taps of ‹ from This Week on the iPhone to reach `week`."""
    this_week = T.week_sunday(T.local_date(clk.now(), settings.tz))
    return (this_week - week.sunday).days // 7


def _recapture(conn: sqlite3.Connection, clk: Clock, settings: Settings,
               week: T.MeasurementWeek) -> tuple[T.MeasurementWeek, ...]:
    """Older weeks to ask for once more: cut off, still on the iPhone, not asked for before.

    "Before" means in another week's check-in. Each cut-off week gets one
    Sunday list and its one reminder, like everything else: imports #24 and
    #80 were cut off before the check-in existed, and nothing asked for them
    again. A week further back than the iPhone keeps is past saving.
    """
    floor = report.window_start(conn, clk, settings)
    asked = _asked_again(conn)
    out = []
    older = week.sunday - timedelta(days=7)
    while _taps_back(clk, settings, T.measurement_week(older)) <= SCREENTIME_WEEKS_BACK:
        candidate = T.measurement_week(older)
        if floor and candidate.end < floor:
            break
        if (asked.get(candidate.start) in (None, week.start)
                and _screentime(conn, settings, candidate) == CUT_OFF):
            out.append(candidate)
        older -= timedelta(days=7)
    return tuple(reversed(out))


def week_needs(conn: sqlite3.Connection, clk: Clock, settings: Settings, week: T.MeasurementWeek) -> WeekNeeds:
    """Work out what `week` still needs. Sends nothing, writes nothing."""
    screentime = _screentime(conn, settings, week)
    youtube = IN if report._covering_import(conn, settings, "youtube_history", week) is not None else MISSING
    coldturkey = None
    if T.GROUP_DESKTOP not in _self_feeding(conn, clk, settings):
        coldturkey = IN if _coldturkey_in(conn, settings, week) else MISSING
    diag = evidence.diagnostic(conn, clk, settings)
    final = bool(diag.started and diag.end is not None and week.start <= diag.end <= week.end)
    return WeekNeeds(week, screentime, youtube, coldturkey,
                     len(report.needs_your_eye(conn, clk, settings)),
                     labels.unlabelled_summary(conn, clk, settings), final,
                     _recapture(conn, clk, settings, week),
                     report.audible_needs_signin(conn, settings))


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------

def _span(week: T.MeasurementWeek) -> str:
    """"Sun 4 – Sat 10 Oct", or "Sun 27 Sep – Sat 3 Oct" across a month."""
    sun, sat = week.sunday, week.saturday
    if sun.month == sat.month:
        return f"Sun {sun.day} – Sat {sat.day} {sat:%b}"
    return f"Sun {sun.day} {sun:%b} – Sat {sat.day} {sat:%b}"


_TAPS = {2: "twice", 3: "three times"}


def _evidence_todo(needs: WeekNeeds, settings: Settings) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if needs.screentime == MISSING:
        out.append(("screentime", SCREENTIME_TODO))
    elif needs.screentime == CUT_OFF:
        out.append(("screentime", SCREENTIME_AGAIN))
    for older in needs.recapture:
        # Counted from the asking week, which is one tap back by definition.
        taps = (needs.week.sunday - older.sunday).days // 7 + 1
        out.append((f"recapture {older.start}",
                    SCREENTIME_OLDER.format(span=_span(older), taps=_TAPS.get(taps, f"{taps} times"))))
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
        subject, add, bring = _BRINGS[steps[0][1].split()[0]]
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
    what.append("podcasts")
    if report.expects("audible", settings) and not needs.audible_signin:
        what.append("audiobooks")
    what.append("your vault")
    return AUTOMATIC_LINE.format(what=", ".join(what))


def render(needs: WeekNeeds, settings: Settings, *, nudge: bool, late: bool = False) -> str:
    """The message. Plain text; only what is still to do is numbered.

    `nudge`: the one reminder after a Sunday list that went out — evidence
    only. `late`: the full list, sent after Sunday because Sunday's never went
    out (the Mac was off, or Telegram was down); no reminder follows it.
    """
    todo = _todo(needs, settings, nudge=nudge)
    span = _span(needs.week)
    order = " these, in this order" if len(todo) > 1 else ""
    if nudge and all(key.startswith("recapture") for key, _ in todo):
        head = f"Still needed{',' + order if order else ''}:"     # last week is in; each step names its week
    elif nudge:
        who = "The final week" if needs.final else "Last week"
        head = f"{who}, {span}, still needs{order}:"
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
        # With an older week in the list, say which week these are in for.
        tail.append(f"Already in{' for last week' if needs.recapture else ''}: {', '.join(done)}.")
    if needs.evidence_missing and not nudge:
        tail.append(_automatic(needs, settings))
    if needs.audible_signin and not nudge:
        tail.append(AUDIBLE_SIGNIN_LINE)
    if needs.evidence_missing and (nudge or late):
        tail.append(LAST_REMINDER.format(url=url(settings, f"/?week={needs.week.start}#coverage")))
    if tail:
        parts.append("\n".join(tail))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def _keys(needs: WeekNeeds, settings: Settings, *, nudge: bool) -> str:
    return ", ".join(key for key, _ in _todo(needs, settings, nudge=nudge))


def _has_run(conn: sqlite3.Connection) -> bool:
    """Has the check-in dealt with any week yet? False only before its first Sunday."""
    return evidence.get_meta(conn, CHECKLIST_KEY) is not None or evidence.get_meta(conn, NUDGE_KEY) is not None


def _send(conn: sqlite3.Connection, settings: Settings, bot, needs: WeekNeeds, text: str) -> None:
    """Send, then remember which cut-off captures this check-in asked for once more."""
    triage.send(bot, settings, text)
    asked = _asked_again(conn)
    again = [w.start for w in needs.recapture]
    if needs.screentime == CUT_OFF:
        again.append(needs.week.start)
    if not again:
        return
    for start in again:
        asked[start] = needs.week.start
    # Only weeks the iPhone may still have are worth remembering.
    oldest = T.iso_date(needs.week.sunday - timedelta(days=7 * SCREENTIME_WEEKS_BACK))
    evidence.set_meta(conn, RECAPTURE_KEY, json.dumps({k: v for k, v in sorted(asked.items()) if k >= oldest}))


def send_due(conn: sqlite3.Connection, clk: Clock, settings: Settings, bot) -> list[str]:
    """Daily job: the Sunday list, or the one reminder, when due. -> what was sent.

    | Day of the run | Sends                                                      |
    |----------------|------------------------------------------------------------|
    | Sunday         | the list, if anything is owed                              |
    | Monday..Sat    | the reminder, if Sunday's list went out and evidence is    |
    |                | still missing; or the whole list, late, if Sunday's never  |
    |                | went out (the Mac was asleep, or Telegram down) and        |
    |                | anything is owed                                           |

    Only one of those ever goes after Sunday, on the first day the job runs:
    nothing is repeated for a week. The one exception to "late" is Tuesday to
    Saturday before the check-in has ever run: merging this on a Friday sends
    nothing off-schedule, and the log says the week went unasked. Markers are
    written after a successful send, or when there was nothing to send.
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
        _send(conn, settings, bot, needs, render(needs, settings, nudge=False))
        evidence.set_meta(conn, CHECKLIST_KEY, week.start)
        return [f"sunday checklist for week {week.start}: {_keys(needs, settings, nudge=False)}"]

    if evidence.get_meta(conn, NUDGE_KEY) == week.start:
        return []
    monday = sunday + timedelta(days=1)
    if not checklist_sent and today != monday and not _has_run(conn):
        log.info("week %s: the check-in has never run, and it is %s; not asked off-schedule",
                 week.start, f"{today:%A}")
        return []
    needs = week_needs(conn, clk, settings, week)
    late = not checklist_sent
    if not (needs.owed if late else needs.evidence_missing):
        log.info("week %s: nothing still missing; no %s", week.start, "late list" if late else "reminder")
        evidence.set_meta(conn, NUDGE_KEY, week.start)
        return []
    if late and today != monday:
        log.warning("week %s: Sunday's and Monday's runs didn't send the check-in; sending it late",
                    week.start)
    _send(conn, settings, bot, needs, render(needs, settings, nudge=not late, late=late))
    evidence.set_meta(conn, NUDGE_KEY, week.start)
    what = "checklist" if late else "nudge"
    return [f"{today:%A}".lower() + f" {what} for week {week.start}: {_keys(needs, settings, nudge=not late)}"]
