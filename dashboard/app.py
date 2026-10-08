"""dashboard/app.py — the DRBR dashboard. Phase 3, integration.

Implements CONTRACTS.md Part II §10 and the brief's "Dashboard" section: Flask
plus HTMX, server-rendered, bound to 127.0.0.1 only. Run it with

    .venv/bin/python -m dashboard.app

Why it is shaped like this.

1. **It renders; it does not compute.** Every figure comes from `report.py`,
   the label manager writes through `labels.set_source_label`, and the
   countdown is `evidence.diagnostic`. The helpers below only format, group
   rows for display, or add up figures `report` already computed. A number
   the dashboard derived on its own would be a second, untested definition
   of the same fact.

2. **Provenance is never flattened.** Every minute figure is printed next to
   a badge whose visual weight differs by tier, and a parsed-screenshot figure
   also carries a "lower confidence" marker beside the number itself, so it
   can never be read with an API figure's confidence.

3. **Absence is not zero.** A week whose evidence never arrived says "not
   received"; unmeasured items are a count; vault figures are counts and are
   never followed by "min".

4. **No targets, scores or streaks.** Allowance status is plain numbers, the
   stacked bar uses neutral greys rather than good/bad colours, and cost per
   keeper is ranked without any threshold.

5. **One connection per request.** Flask's threaded server handles each
   request on its own thread and sqlite3 refuses cross-thread use, so the
   connection is opened lazily inside the request and closed on teardown.
   The ingest blueprint shares it and never closes it itself.

6. **Content only (DR5.5).** DRBR measures brain-rot content. A source the
   user labels `tool` (not content) is out of every wasteful/worthwhile figure
   `report` produces, and its minutes are printed on their own so the label
   cannot hide anything. The label manager offers it as a third button.

7. **Loopback is not a trust boundary on its own.** Any page open in the
   browser can POST to 127.0.0.1. The ingest API has its token; the label
   toggle refuses requests the browser marks as cross-site
   (`Sec-Fetch-Site`), so a web page cannot relabel sources behind the user's
   back.
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Callable, Sequence

from flask import Flask, abort, g, redirect, render_template, request, url_for
from markupsafe import Markup, escape

import allowance
import clock
import config
import db
import drbr_types as T
import ingest_api
import insights
import jobs
import labels
import pace
import progress
import report
from clock import Clock
from config import Settings
from sources import screentime
from sources.youtube import DataApiClient

log = logging.getLogger("drbr.dashboard")

#: HTMX 2.x from jsdelivr, pinned. The SRI hash was computed from the file
#: served by jsdelivr, checked against unpkg's copy, and against jsdelivr's
#: published sha256 for the same file. Every page works without it.
HTMX_URL = "https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js"
HTMX_SRI = "sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"

#: Leaves the ingest blueprint room to answer an oversized body with its own
#: JSON message (25 MB) before Werkzeug's generic 413 fires.
MAX_CONTENT_LENGTH = ingest_api.MAX_BODY_BYTES + 1024 * 1024

GROUP_NAMES = {
    T.GROUP_PODCASTS: "Podcasts",
    T.GROUP_YOUTUBE: "YouTube",
    T.GROUP_X: "X",
    T.GROUP_IPHONE: "iPhone apps",
    T.GROUP_DESKTOP: "Desktop web",
    T.GROUP_DESKTOP_APPS: "Desktop apps",
    T.GROUP_WEB: "Web articles",
    T.GROUP_OTHER: "Other captures",
}

KIND_NAMES = {
    T.KIND_PODCAST_SHOW: "podcast show",
    T.KIND_YOUTUBE_CHANNEL: "YouTube channel",
    T.KIND_IPHONE_APP: "iPhone app",
    T.KIND_WEB_DOMAIN: "web domain",
    T.KIND_X: "X",
    T.KIND_DESKTOP_APP: "Mac app",
}

IMPORT_KIND_NAMES = {
    "youtube_history": "YouTube history",
    "screentime": "Screen Time",
    "coldturkey": "Cold Turkey",
    "vault_scan": "Vault scan",
    "qbserve": "Qbserve",
}

#: (short text, css tier) per provenance. The long text is T.PROVENANCE_TEXT.
PROVENANCE_BADGES = {
    T.PROV_API: ("API", "api"),
    T.PROV_PAGE: ("page data", "page"),
    T.PROV_SCREENSHOT: ("parsed screenshot", "screenshot"),
    T.PROV_EXPORT: ("manual export", "export"),
    T.PROV_APP_TRACKER: ("app tracker", "apptracker"),
    T.PROV_BROWSER_SYNC: ("phone sync", "browsersync"),
    T.PROV_VAULT: ("vault scan · count", "vault"),
    T.PROV_LABEL: ("your label", "label"),
}

BASIS_TEXT = {
    T.BASIS_ITEM_OVERRIDE: "item override",
    T.BASIS_SOURCE_LABEL: "source label",
    T.BASIS_DEFAULT: "unlabelled → wasteful",
}

#: Shown when a write is refused because it did not come from this dashboard.
#: A form this app served sends `Sec-Fetch-Site: same-origin`; anything else
#: (another localhost port, another site) is refused, since there is no CSRF
#: token to check instead.
WRITE_REFUSED = (
    "This change was refused because the request did not come from this dashboard. "
    "Open http://127.0.0.1:8750/ directly and try again."
)

UNKNOWN_CHANNEL_REASON = (
    "Not toggleable: this is a bucket for videos with no identifiable channel. "
    "A label here would apply to unrelated channels at once, so its minutes "
    "count as unlabelled (wasteful)."
)

#: How a tool label reads wherever a source's label is shown (DR5.5).
TOOL_TEXT = "tool → not content, not counted"

RECORDS_LIMIT = 500


# --------------------------------------------------------------------------
# View models (formatting and grouping only)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Countdown:
    state: str                 # 'not_started' | 'running' | 'ended'
    total_days: int
    weeks: int
    day: int | None = None
    days_left: int | None = None
    week_number: int | None = None
    start: str | None = None
    end: str | None = None
    reason: str | None = None


def countdown(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> Countdown:
    """The header countdown, from `report.diagnostic_banner` and today's local date."""
    diag = report.diagnostic_banner(conn, clk, settings)
    total = 7 * settings.diagnostic_weeks
    today = T.local_date(clk.now(), settings.tz)
    if not diag.started:
        if settings.go_live and today < T.parse_date(settings.go_live):
            reason = (f"Measurement goes live on {settings.go_live}. The {settings.diagnostic_weeks} weeks "
                      "start at the first valid data on or after that date.")
        else:
            reason = ("The weeks start at the first valid data: a Screen Time, Cold Turkey or "
                      "YouTube import. None has arrived yet.")
        return Countdown("not_started", total, settings.diagnostic_weeks, reason=reason)
    if diag.ended:
        return Countdown("ended", total, settings.diagnostic_weeks, start=diag.start, end=diag.end)
    day = (today - T.parse_date(diag.start)).days + 1
    if day < 1:
        return Countdown("not_started", total, settings.diagnostic_weeks, start=diag.start, end=diag.end,
                         reason=f"The first valid data is dated {diag.start}; day 1 is that date.")
    return Countdown("running", total, settings.diagnostic_weeks, day=day, days_left=diag.days_remaining,
                     week_number=diag.week_number, start=diag.start, end=diag.end)


@dataclass(frozen=True)
class WeekChoice:
    week_start: str            # allowance week start, UTC ISO
    sunday: str                # local Sunday of the measurement week
    saturday: str
    is_current: bool
    prev_sunday: str | None    # None when the previous week is before the diagnostic
    next_sunday: str | None    # None when the next week is in the future
    current_sunday: str
    last_complete_sunday: str
    due: bool                  # the Sunday this week's evidence is asked for has come (DR5.24)
    due_on: str                # that Sunday, so the page can name the day


def choose_week(clk: Clock, settings: Settings, raw: str | None,
                floor: str | None = None) -> WeekChoice:
    """`?week=YYYY-MM-DD` (any local day) -> that allowance week; default **this** week.

    The default is the current week, not the last complete one (DR5.7). The
    allowance section has always described the current week, so defaulting the
    rest of the page to the previous one put two different weeks side by side
    with nothing saying so — and before the diagnostic had run a full week, it
    opened on a week outside the diagnostic entirely.

    `floor` is the first local day the diagnostic covers: a week before it
    holds no diagnostic data, so a `?week=` pointing there clamps forward and
    the "previous week" link disappears rather than leading nowhere.
    """
    tz = settings.tz
    current = clock.iso(clock.week_start_for(clk.now(), tz))
    current_m = T.measurement_week_for_allowance_week(current, tz)
    last_m = T.measurement_week(current_m.sunday - timedelta(days=7))
    floor_m = T.measurement_week(T.parse_date(floor)) if floor else None
    if raw:
        try:
            chosen_m = T.measurement_week(T.parse_date(raw))
        except ValueError:
            abort(400, description=f"week must be a date YYYY-MM-DD, got {raw!r}")
    else:
        chosen_m = current_m
    if floor_m is not None and chosen_m.sunday < floor_m.sunday:
        chosen_m = floor_m
    if chosen_m.sunday > current_m.sunday:
        chosen_m = current_m
    week_start = T.allowance_week_start_for_day(chosen_m.sunday, tz)
    prev_sunday = chosen_m.sunday - timedelta(days=7)
    has_prev = floor_m is None or prev_sunday >= floor_m.sunday
    next_sunday = chosen_m.sunday + timedelta(days=7)
    return WeekChoice(
        week_start=week_start,
        sunday=chosen_m.start,
        saturday=chosen_m.end,
        is_current=chosen_m.sunday == current_m.sunday,
        prev_sunday=T.iso_date(prev_sunday) if has_prev else None,
        next_sunday=T.iso_date(next_sunday) if next_sunday <= current_m.sunday else None,
        current_sunday=current_m.start,
        last_complete_sunday=last_m.start,
        due=report.week_is_due(clk, settings, chosen_m),
        due_on=T.iso_date(report.due_on(chosen_m)),
    )


#: Flash messages survive a redirect as `?msg=` codes rather than a Flask
#: session, so the app still needs no secret key. Only these codes render.
FLASH_TEXT = {
    "synced": ("ok", "Sync finished. Everything that can be fetched automatically is up to date."),
    "sync-failed": ("warn", "Sync ran but some sources failed — see the source table below."),
    "labelled": ("ok", "Labels saved. Every week's figures were re-recorded."),
    "fixed": ("ok", "Reading corrected. The week's totals now include it."),
    "insight-written": ("ok", "Summary rewritten from the figures above."),
    "insight-fallback": ("warn", "The summary could not be generated, so the computed "
                                 "sentences are shown instead."),
}


def flash_from(args) -> list[tuple[str, str]]:
    """-> [(level, text)] for the codes in `?msg=`. Unknown codes are ignored."""
    return [FLASH_TEXT[code] for code in args.getlist("msg") if code in FLASH_TEXT]


#: Why a stored summary no longer describes the figures beside it (DR5.18,
#: DR5.26), in the order `insight_staleness` checks them.
STALE_NEWER_WEEK = "newer_week"
STALE_NEW_DATA = "new_data"
STALE_RELABELLED = "relabelled"

#: The imports a person sends. Cold Turkey counts only as a manual export:
#: `meta.origin` other than `local_db`, or none, as `report._coldturkey_blind_spots` reads it.
SENT_BY_HAND = ("screentime", "youtube_history", "coldturkey")
#: The automatic reads that re-read their last eight days every morning and
#: carry a period: Cold Turkey's own statistics (`local_db`), Qbserve, the phone
#: browser. They count only when catching up (`_new_data_since`, rule 4).
READ_DAILY = ("coldturkey", "qbserve", "brave_sync")


def insight_staleness(conn: sqlite3.Connection, insight, last_complete_sunday: str,
                      tz: str) -> str | None:
    """None when the summary still describes the page; otherwise why it doesn't.

    Three reasons, and they want different sentences, so they are returned
    rather than folded into one boolean. They are checked in this order, and
    the first that holds is the one named:

    1. `newer_week` — a later week has finished (`insight_is_stale`). It
       replaces the whole paragraph, so it is the most urgent news.
    2. `new_data` — data for the summarised week, or for the week its
       headline compares it with, arrived after the paragraph's figures were
       read (`_new_data_since`, DR5.26). It can make a sentence in it false:
       summary #8 was written at 07:30:11Z on 5 Oct, the YouTube sync landed
       48 s later, and the paragraph went on saying YouTube never arrived.
    3. `relabelled` — a label that feeds the summarised figures changed after
       the paragraph was written. Labelling is the thing Jacob does most, and
       it moves every figure: forty-three sources bulk-labelled tool three
       hours after a summary was written left it saying "181 to 131" beside a
       panel saying "177 to 119", both claiming like for like, with no data
       having arrived. Staleness per week never sees that. It ranks last
       because it reclassifies figures that were right when they were written.

    Only labels that could have changed the summarised figures count: a source
    with a measurement on or before the last summarised day, any podcast show
    (its minutes come from listening, not measurements), and any per-item
    override. Labelling a source first seen this week does not stale a
    summary about last week. Regenerating is still a person's choice — an API
    call per labelling session would be wrong — so this only says so.
    """
    if insight is None or not insight.based_on_through:
        return None
    if insight_is_stale(insight, last_complete_sunday, tz):
        return STALE_NEWER_WEEK
    written, through = insight.created_at, insight.based_on_through
    if _new_data_since(conn, written, T.measurement_week(T.parse_date(through)), tz):
        return STALE_NEW_DATA
    relabelled = conn.execute(
        """
        SELECT 1 FROM sources s
         WHERE s.labelled_at IS NOT NULL AND s.labelled_at > ?
           AND (s.kind = ?
                OR EXISTS (SELECT 1 FROM measurements m
                            WHERE m.source_id = s.id AND m.period_start <= ?))
        UNION ALL
        SELECT 1 FROM item_labels WHERE set_at > ?
        LIMIT 1
        """,
        (written, T.KIND_PODCAST_SHOW, through, written),
    ).fetchone()
    return STALE_RELABELLED if relabelled else None


#: Is import `i` (or `j`) one of the automatic reads in `READ_DAILY`? Cold
#: Turkey only as its own statistics; an export sent by hand has no origin or another one.
_AUTO = """({a}.kind IN ('qbserve', 'brave_sync')
            OR ({a}.kind = 'coldturkey'
                AND COALESCE(CASE WHEN json_valid({a}.meta) THEN json_extract({a}.meta, '$.origin') END,
                             'export') = 'local_db'))"""


def _new_data_since(conn: sqlite3.Connection, written: str, week: T.MeasurementWeek,
                    tz: str) -> bool:
    """Did data the summary describes arrive after its figures were read (`written`)?

    `week` is the last summarised week. The headline compares it with the
    week before (`payload.comparison.previous_week`: summary #8 says "fell
    from 288 minutes to 230"), so data for either week counts. `written` is
    the summary's `created_at`, stamped before its figures were read, so an
    import landing during the API call counts as after it. Any one of four
    things (DR5.26):

    1. An import a person sends (`SENT_BY_HAND`), ok or partial, received after
       the summary, whose period overlaps either week. Or a YouTube sync that
       is the first to cover the last week at all: no sync had arrived since
       its Saturday when the summary was written, so the paragraph named the
       gap, and this one fills it whatever its watch dates. Imports have no
       finish time; `received_at` is stamped by `evidence.begin_import`.
    2. A reading for either week that a person settled after the summary
       (`detail.corrections[].at`, written by `screentime.correct_measurement`).
    3. Listening heard in the last week's allowance week, polled after the
       summary. Almost never true: it is for a Rewrite pressed on a Sunday
       morning, before that allowance week closes at 18:00.
    4. An automatic read (`READ_DAILY`) catching up: it overlaps the last
       week and arrived after the summary, and no read of its kind had
       arrived since that week ended when the summary was written. The read
       was failing (Full Disk Access, say), so the paragraph went without that
       source's last days, and this read brings them.

    Otherwise the automatic reads are left out by design. They re-read their
    last eight days every morning, so they overlap the summarised week on each
    of the mornings after it is written (imports #87–#93 after summary #8).
    Counting every one would mark every summary out of date for most of a
    week, the trap `insight_is_stale`'s note describes. A healthy read always
    comes before the summary: the morning job reads first and writes after,
    on the Monday after the week, so rule 4 holds only after a gap. The vault
    is left out entirely: its scans have no period.
    """
    after_saturday = report._local_midnight(week.saturday + timedelta(days=1), tz)
    opens = T.allowance_week_start_for_day(week.sunday, tz)
    closes = clock.iso(clock.next_week_start(clock.parse(opens), tz))    # as allowance.week_end
    compared = T.measurement_week(week.sunday - timedelta(days=7))
    start = conn.execute("SELECT value FROM meta WHERE key = 'diagnostic_start'").fetchone()
    if start is not None and compared.end < start["value"]:
        compared = week               # the first week has nothing before it to compare with
    kinds = {f"kind{n}": kind for n, kind in enumerate(SENT_BY_HAND)}
    auto_kinds = {f"auto{n}": kind for n, kind in enumerate(READ_DAILY)}
    found = conn.execute(
        f"""
        SELECT 1 FROM imports i
         WHERE i.kind IN ({", ".join(":" + k for k in kinds)})
           AND i.status IN ('ok', 'partial') AND i.received_at > :written
           AND NOT {_AUTO.format(a="i")}
           AND ((i.period_start <= :end AND i.period_end >= :from)
                OR (i.kind = 'youtube_history' AND i.received_at >= :after_saturday
                    AND NOT EXISTS (SELECT 1 FROM imports y
                                     WHERE y.kind = 'youtube_history'
                                       AND y.status IN ('ok', 'partial')
                                       AND y.received_at >= :after_saturday
                                       AND y.received_at <= :written)))
        UNION ALL
        SELECT 1 FROM imports i
         WHERE i.kind IN ({", ".join(":" + k for k in auto_kinds)}) AND {_AUTO.format(a="i")}
           AND i.status IN ('ok', 'partial') AND i.received_at > :written
           AND i.period_start <= :end AND i.period_end >= :start
           AND NOT EXISTS (SELECT 1 FROM imports j
                            WHERE j.kind = i.kind AND {_AUTO.format(a="j")}
                              AND j.status IN ('ok', 'partial')
                              AND j.received_at >= :after_saturday
                              AND j.received_at <= :written)
        UNION ALL
        SELECT 1 FROM measurements m,
               json_each(CASE WHEN json_valid(m.detail) THEN m.detail ELSE '{{}}' END,
                         '$.corrections') c
         WHERE m.period_start <= :end AND m.period_end >= :from
           AND instr(m.detail, 'corrections') > 0
           AND (CASE WHEN c.type = 'object' THEN json_extract(c.value, '$.at') END) > :written
        UNION ALL
        SELECT 1 FROM listening l
         WHERE l.polled_at > :written AND l.polled_at >= :opens AND l.polled_at < :closes
           AND l.delta_ms > 0
        LIMIT 1
        """,
        {"written": written, "from": compared.start, "start": week.start, "end": week.end,
         "after_saturday": after_saturday, "opens": opens, "closes": closes, **kinds, **auto_kinds},
    ).fetchone()
    return found is not None


def insight_is_stale(insight, last_complete_sunday: str, tz: str) -> bool:
    """Does a completed week exist that is later than the one this summarises?

    Staleness is per **week**, not per import. Cold Turkey, Qbserve and the
    vault are re-read every morning over the last 8 days, so data newer than
    any summary exists within a day of it being written; a data-based test
    would be true six days in seven and mean nothing. A superseded *week* is
    the thing worth telling the reader about.

    The week it **describes** is the one that matters, not the week it was
    written in. `Insight.week_start` is the allowance week of *generation*: a
    summary written on Monday the 21st carries `week_start` of the week
    opening the 20th while describing the week ending the 19th. Testing that
    field fires a week late, leaving a superseded summary presented as current
    for the whole seven days the flag exists to cover. `based_on_through` is
    the last day the figures cover, already a local date, so the week it
    describes comes straight from it.
    """
    if insight is None or not insight.based_on_through:
        return False
    summarised = T.measurement_week(T.parse_date(insight.based_on_through)).sunday
    return summarised < T.parse_date(last_complete_sunday)


def split_bars(splits: list[report.WeekSplit], current_week_start: str, tz: str) -> dict:
    """Geometry for the stacked wasteful/undecided/worthwhile bars. Scale only; no judgement.

    Three bands, not two, since DR5.11. A bar drawn from `wasteful_min` alone
    would leave undecided time out of the geometry entirely — 290 of the 567
    minutes in the week of 13 Sep — so the chart that answers "is it going
    down" would be drawn from half the week, and would appear to have halved
    for a reason that is not behaviour. The undecided band keeps the bar the
    height of its week and shows the share that is still a decision.
    """
    height, bar_w, gap, top, label_h = 168, 44, 30, 26, 42
    peak = max((s.charged_min + s.worthwhile_min for s in splits), default=0.0)
    scale = (height / peak) if peak > 0 else 0.0
    bars = []
    for i, s in enumerate(splits):
        x = gap // 2 + i * (bar_w + gap)
        waste_h = s.wasteful_min * scale
        undec_h = s.unclassified_min * scale
        worth_h = s.worthwhile_min * scale
        base = top + height
        bars.append({
            "x": x,
            "cx": x + bar_w / 2,
            "waste_y": base - waste_h,
            "waste_h": waste_h,
            "undec_y": base - waste_h - undec_h,
            "undec_h": undec_h,
            "worth_y": base - waste_h - undec_h - worth_h,
            "worth_h": worth_h,
            "total_y": base - waste_h - undec_h - worth_h - 6,
            "sunday": T.iso_date(T.measurement_week_for_allowance_week(s.week_start, tz).sunday),
            "current": s.week_start == current_week_start,
            "split": s,
        })
    return {
        "bars": bars,
        "bar_w": bar_w,
        "width": max(1, len(splits)) * (bar_w + gap),
        "height": top + height + label_h,
        "base": top + height,
        "empty": peak == 0,
    }


def week_summary(splits: list[report.WeekSplit], week_start: str) -> report.WeekSplit | None:
    """The chosen week's split, picked out of the list `report.label_split` returned.

    Selection, not computation. The Overview's summary strip prints the same
    figures the bars are drawn from, so there is no second definition of a
    week's wasteful, worthwhile or tool minutes. A week outside the diagnostic
    window has no split, and the strip then prints nothing rather than zero.
    """
    for s in splits:
        if s.week_start == week_start:
            return s
    return None


# The brain against your peak week (#rot). Scale only; no judgement: the ratio
# is progress.peak_chain's, and everything below only selects and words it.

# --- identity: the wordmark's own geometry (base.html reads these too) -------
BRAIN_OUTLINE = ("M12 4.6C9.6 3.3 6.4 4.1 5.4 6.3C3.5 6.9 2.9 9 4 10.6C3.1 12.2 3.8 14.4 5.6 15.1"
                 "C6 17.3 8.3 18.6 10.3 17.9C11 18.9 13 18.9 13.7 17.9C15.7 18.6 18 17.3 18.4 15.1"
                 "C20.2 14.4 20.9 12.2 20 10.6C21.1 9 20.5 6.9 18.6 6.3C17.6 4.1 14.4 3.3 12 4.6Z")
BRAIN_FOLDS = ("M12 4.6V17.9M9 7.4c-1.6.5-2 2.2-1.2 3.2M15 7.4c1.6.5 2 2.2 1.2 3.2"
               "M8.4 13.2c-.9.5-1 1.6-.4 2.3M15.6 13.2c.9.5 1 1.6.4 2.3")
#: Cropped to the outline's box (x 3.45-20.55, y 3.97-18.65) plus stroke room.
BRAIN_VIEWBOX = "2.8 3.3 18.4 16.1"

# --- the large brain's own detail (#rot only) --------------------------------
# The 21px mark keeps only the outline, the four folds above and its three
# specks; the tab icon only the outline and specks. Only the large brain is
# drawn in full. It is the same brain:
# the same outline, midline and four folds (now its deepest grooves), with the
# rest of its grooves, its light and its lobes added round them. Light comes
# from the upper left. Every groove is a filled, tapered ribbon with round ends,
# so it swells and thins like a brush line. Placed by hand once (in brain
# units) and frozen here as literals: nothing is generated at render time.
#: The two hemispheres, each closed along the midline (the bottom lobe split at
#: its own lowest point, 12 18.65), so each gets its own dome of light.
BRAIN_HALVES = ("M12 4.6C9.6 3.3 6.4 4.1 5.4 6.3C3.5 6.9 2.9 9 4 10.6C3.1 12.2 3.8 14.4 5.6 15.1"
                "C6 17.3 8.3 18.6 10.3 17.9C10.65 18.4 11.33 18.65 12 18.65Z",
                "M12 4.6C14.4 3.3 17.6 4.1 18.6 6.3C20.5 6.9 21.1 9 20 10.6C20.9 12.2 20.2 14.4 18.4 15.1"
                "C18 17.3 15.7 18.6 13.7 17.9C13.35 18.4 12.67 18.65 12 18.65Z")
#: A soft pool of light on each hemisphere's upper shoulder.
BRAIN_GLOW = ("M3.9 7.3A3.4 2.6 0 1 1 10.7 7.3A3.4 2.6 0 1 1 3.9 7.3Z",
              "M12.4 6.9A2.9 2.2 0 1 1 18.2 6.9A2.9 2.2 0 1 1 12.4 6.9Z")
#: The deepest grooves: the mark's midline and four folds (the same lines as
#: BRAIN_FOLDS), tapered.
BRAIN_DEEP = ("M8.97 7.31C7.23 7.65 6.71 9.7 7.74 10.65A.08 .08 0 0 0 7.86 10.55"
              "C7.29 9.5 7.57 8.15 9.03 7.49A.09 .09 0 0 0 8.97 7.31ZM14.97 7.49"
              "C16.43 8.15 16.71 9.5 16.14 10.55A.08 .08 0 0 0 16.26 10.65"
              "C17.29 9.7 16.77 7.65 15.03 7.31A.09 .09 0 0 0 14.97 7.49ZM8.36 13.13"
              "C7.31 13.52 7.16 14.92 7.95 15.55A.07 .07 0 0 0 8.05 15.45"
              "C7.64 14.68 7.69 13.88 8.44 13.27A.08 .08 0 0 0 8.36 13.13ZM15.56 13.27"
              "C16.31 13.88 16.36 14.68 15.95 15.45A.07 .07 0 0 0 16.05 15.55"
              "C16.84 14.92 16.69 13.52 15.64 13.13A.08 .08 0 0 0 15.56 13.27ZM11.85 4.5"
              "C11.77 8.95 11.75 13.45 11.87 17.9A.13 .13 0 0 0 12.13 17.9"
              "C12.25 13.45 12.23 8.95 12.15 4.5A.15 .15 0 0 0 11.85 4.5Z")
#: The other grooves: one family on each side, never a mirror.
BRAIN_SULCI = ("M11.58 5.99C11.24 5.77 10.9 5.46 10.48 5.33C10.07 5.21 9.6 5.25 9.23 5.32"
               "C8.85 5.39 8.49 5.56 8.24 5.76C7.96 5.97 7.86 6.24 7.7 6.46A.06 .06 0 0 0 7.8 6.54"
               "C8.01 6.33 8.14 6.08 8.36 5.94C8.61 5.79 8.94 5.64 9.27 5.58"
               "C9.62 5.52 10.06 5.49 10.42 5.57C10.77 5.64 11.13 5.93 11.52 6.11"
               "A.07 .07 0 0 0 11.58 5.99ZM10.75 6.42C10.53 6.69 10.25 6.95 10.1 7.31"
               "C9.95 7.66 9.92 8.08 9.93 8.45C9.93 8.83 9.98 9.24 10.1 9.58"
               "C10.22 9.93 10.42 10.2 10.6 10.48A.06 .06 0 0 0 10.7 10.42"
               "C10.58 10.1 10.38 9.83 10.3 9.52C10.22 9.2 10.17 8.81 10.17 8.45"
               "C10.18 8.09 10.2 7.7 10.3 7.39C10.4 7.08 10.67 6.81 10.85 6.48"
               "A.06 .06 0 0 0 10.75 6.42ZM11.64 8.71C11.47 8.73 11.27 8.71 11.1 8.83"
               "C10.94 8.96 10.88 9.17 10.82 9.33A.04 .04 0 0 0 10.88 9.37"
               "C11.02 9.23 11.1 9.04 11.2 8.97C11.3 8.89 11.49 8.87 11.66 8.79"
               "A.04 .04 0 0 0 11.64 8.71ZM5.29 6.31C5.26 6.62 5.28 6.91 5.21 7.17"
               "C5.15 7.41 4.93 7.68 4.86 8.02C4.8 8.34 4.81 8.71 4.89 8.98"
               "C4.97 9.27 5.16 9.41 5.29 9.59A.07 .07 0 0 0 5.41 9.51C5.31 9.29 5.15 9.13 5.11 8.92"
               "C5.07 8.69 5.08 8.35 5.14 8.08C5.19 7.84 5.41 7.57 5.49 7.23"
               "C5.56 6.92 5.54 6.61 5.55 6.33A.13 .13 0 0 0 5.29 6.31ZM5.05 8.07"
               "C5.26 8.11 5.52 8.22 5.78 8.15C6.03 8.08 6.25 7.86 6.38 7.66"
               "C6.53 7.44 6.55 7.19 6.6 6.97A.05 .05 0 0 0 6.5 6.93C6.39 7.15 6.34 7.39 6.22 7.54"
               "C6.09 7.71 5.89 7.89 5.72 7.95C5.57 8.01 5.31 7.93 5.05 7.93A.07 .07 0 0 0 5.05 8.07Z"
               "M5.47 6.42C5.7 6.33 5.97 6.27 6.22 6.1C6.46 5.94 6.63 5.71 6.79 5.5"
               "A.06 .06 0 0 0 6.71 5.4C6.47 5.56 6.29 5.77 6.08 5.9C5.88 6.02 5.63 6.09 5.37 6.22"
               "A.11 .11 0 0 0 5.47 6.42ZM3.98 10.76C4.35 10.87 4.72 10.92 5.04 11.05"
               "C5.36 11.17 5.66 11.42 6.05 11.56C6.41 11.68 6.8 11.77 7.2 11.78"
               "C7.59 11.78 8.02 11.7 8.39 11.58C8.77 11.44 9.07 11.21 9.39 11.02"
               "A.08 .08 0 0 0 9.31 10.88C8.96 11.02 8.65 11.23 8.31 11.32"
               "C7.96 11.42 7.56 11.48 7.2 11.46C6.85 11.45 6.49 11.36 6.15 11.24"
               "C5.84 11.14 5.54 10.89 5.16 10.75C4.79 10.62 4.41 10.56 4.06 10.48"
               "A.15 .15 0 0 0 3.98 10.76ZM8.57 11.55C8.75 11.72 8.96 11.95 9.22 12.04"
               "C9.49 12.12 9.73 12.03 9.95 12A.05 .05 0 0 0 9.95 11.9C9.7 11.87 9.46 11.93 9.28 11.86"
               "C9.09 11.8 8.88 11.58 8.63 11.45A.06 .06 0 0 0 8.57 11.55ZM4.54 12.21"
               "C4.85 12.29 5.15 12.29 5.41 12.41C5.68 12.53 6.05 12.73 6.2 12.93"
               "C6.35 13.12 6.43 13.45 6.44 13.7C6.46 13.92 6.3 14.16 6.24 14.43"
               "A.06 .06 0 0 0 6.36 14.47C6.46 14.24 6.64 14.01 6.66 13.7"
               "C6.67 13.42 6.61 13.04 6.4 12.77C6.19 12.5 5.8 12.31 5.49 12.19"
               "C5.17 12.08 4.85 12.11 4.56 12.09A.07 .07 0 0 0 4.54 12.21ZM5.6 15.21"
               "C5.89 15.32 6.19 15.36 6.38 15.5C6.55 15.62 6.64 15.97 6.92 16.2"
               "C7.17 16.4 7.51 16.57 7.83 16.63C8.14 16.68 8.5 16.64 8.75 16.5"
               "C9.01 16.36 9.2 16.08 9.29 15.84C9.39 15.57 9.31 15.29 9.31 15.05"
               "A.07 .07 0 0 0 9.19 15.05C9.15 15.31 9.2 15.58 9.11 15.76"
               "C9.02 15.97 8.84 16.21 8.65 16.3C8.45 16.39 8.13 16.42 7.87 16.37"
               "C7.62 16.33 7.3 16.17 7.08 16C6.89 15.86 6.8 15.51 6.52 15.3"
               "C6.26 15.11 5.93 15.08 5.68 14.99A.12 .12 0 0 0 5.6 15.21ZM10.4 17.83"
               "C10.29 17.55 10.09 17.28 10.07 17.09C10.05 16.88 10.13 16.62 10.24 16.47"
               "C10.32 16.33 10.58 16.25 10.78 16.1A.06 .06 0 0 0 10.72 16"
               "C10.52 16.08 10.25 16.12 10.06 16.33C9.9 16.53 9.8 16.83 9.83 17.11"
               "C9.86 17.42 10.08 17.69 10.2 17.93A.11 .11 0 0 0 10.4 17.83ZM11.32 11.9"
               "C11.04 12 10.7 12.07 10.43 12.26C10.18 12.45 9.97 12.73 9.84 13"
               "C9.71 13.27 9.66 13.6 9.65 13.84C9.65 14.11 9.74 14.28 9.79 14.46"
               "A.06 .06 0 0 0 9.91 14.44C9.89 14.22 9.82 14.06 9.85 13.86"
               "C9.87 13.63 9.94 13.33 10.06 13.1C10.18 12.87 10.36 12.61 10.57 12.44"
               "C10.77 12.26 11.1 12.17 11.38 12A.06 .06 0 0 0 11.32 11.9ZM12.49 6.8"
               "C12.79 6.58 13.02 6.29 13.31 6.16C13.6 6.01 13.98 5.92 14.31 5.88"
               "C14.64 5.85 15.03 5.88 15.31 5.96C15.58 6.03 15.79 6.25 16.06 6.4"
               "A.06 .06 0 0 0 16.14 6.3C15.91 6.11 15.72 5.87 15.39 5.74"
               "C15.07 5.62 14.66 5.58 14.29 5.62C13.92 5.65 13.52 5.76 13.19 5.94"
               "C12.86 6.14 12.64 6.45 12.41 6.7A.07 .07 0 0 0 12.49 6.8ZM13.05 6.63"
               "C13.21 6.96 13.44 7.23 13.55 7.54C13.66 7.86 13.72 8.25 13.73 8.6"
               "C13.73 8.92 13.54 9.32 13.54 9.7C13.55 10.06 13.67 10.36 13.74 10.66"
               "A.06 .06 0 0 0 13.86 10.64C13.83 10.31 13.74 10.02 13.76 9.7"
               "C13.77 9.4 13.97 9.01 13.97 8.6C13.98 8.22 13.89 7.8 13.75 7.46"
               "C13.61 7.11 13.36 6.84 13.15 6.57A.06 .06 0 0 0 13.05 6.63ZM14.9 8.64"
               "C14.81 8.87 14.66 9.11 14.65 9.4C14.66 9.69 14.81 9.93 14.91 10.16"
               "A.05 .05 0 0 0 14.99 10.14C14.96 9.87 14.84 9.61 14.85 9.4"
               "C14.84 9.19 14.96 8.93 15 8.66A.05 .05 0 0 0 14.9 8.64ZM18.49 6.23"
               "C18.32 6.38 18.09 6.53 17.93 6.77C17.78 7 17.69 7.28 17.67 7.54"
               "C17.66 7.81 17.76 8.01 17.83 8.21A.07 .07 0 0 0 17.97 8.19"
               "C17.97 7.95 17.89 7.74 17.93 7.56C17.96 7.37 18.06 7.11 18.17 6.93"
               "C18.27 6.76 18.49 6.61 18.67 6.41A.13 .13 0 0 0 18.49 6.23ZM18.56 6.21"
               "C18.3 6.24 17.99 6.22 17.7 6.34C17.42 6.47 17.25 6.71 17.06 6.9A.06 .06 0 0 0 17.14 7"
               "C17.39 6.86 17.58 6.64 17.8 6.56C18.01 6.47 18.3 6.48 18.6 6.43"
               "A.11 .11 0 0 0 18.56 6.21ZM20.02 10.45C19.72 10.28 19.35 10.17 19.2 10"
               "C19.02 9.83 18.9 9.56 18.87 9.34C18.83 9.15 18.97 8.89 19.02 8.62"
               "A.07 .07 0 0 0 18.88 8.58C18.79 8.81 18.62 9.05 18.63 9.36"
               "C18.65 9.64 18.78 9.97 19 10.2C19.25 10.43 19.63 10.53 19.9 10.67"
               "A.12 .12 0 0 0 20.02 10.45ZM19.94 10.48C19.58 10.57 19.2 10.66 18.84 10.8"
               "C18.46 10.95 18.13 11.21 17.85 11.3C17.56 11.4 17.19 11.43 16.96 11.42"
               "C16.76 11.42 16.65 11.3 16.43 11.22A.08 .08 0 0 0 16.37 11.38"
               "C16.51 11.46 16.64 11.63 16.94 11.68C17.21 11.72 17.59 11.7 17.95 11.6"
               "C18.32 11.49 18.65 11.23 18.96 11.1C19.29 10.97 19.66 10.89 20.02 10.76"
               "A.15 .15 0 0 0 19.94 10.48ZM17.39 11.5C17.28 11.74 17.12 12 17.1 12.29"
               "C17.09 12.59 17.22 12.86 17.3 13.11A.05 .05 0 0 0 17.4 13.09"
               "C17.38 12.81 17.28 12.53 17.3 12.31C17.31 12.08 17.45 11.82 17.51 11.54"
               "A.06 .06 0 0 0 17.39 11.5ZM12.72 11.65C12.96 11.84 13.27 11.96 13.42 12.16"
               "C13.58 12.38 13.71 12.72 13.73 13.01C13.75 13.29 13.65 13.67 13.55 13.96"
               "C13.46 14.25 13.28 14.48 13.15 14.77A.06 .06 0 0 0 13.25 14.83"
               "C13.42 14.58 13.62 14.35 13.75 14.04C13.87 13.73 14 13.34 13.97 12.99"
               "C13.94 12.65 13.79 12.28 13.58 12.04C13.36 11.78 13.04 11.69 12.78 11.55"
               "A.06 .06 0 0 0 12.72 11.65ZM18.48 15.01C18.3 14.73 18.03 14.48 17.98 14.27"
               "C17.92 14.03 17.98 13.67 18.07 13.46C18.15 13.26 18.37 13.08 18.56 12.94"
               "C18.73 12.82 18.96 12.78 19.18 12.67A.07 .07 0 0 0 19.12 12.53"
               "C18.91 12.59 18.67 12.61 18.44 12.76C18.23 12.88 17.96 13.06 17.83 13.34"
               "C17.7 13.61 17.63 14.01 17.72 14.33C17.81 14.68 18.11 14.91 18.28 15.15"
               "A.13 .13 0 0 0 18.48 15.01ZM18.3 15C18.04 15.16 17.79 15.29 17.52 15.5"
               "C17.25 15.72 17.02 16.06 16.79 16.19C16.54 16.33 16.17 16.42 15.95 16.43"
               "C15.72 16.43 15.56 16.38 15.43 16.27C15.3 16.16 15.17 15.89 15.15 15.69"
               "C15.11 15.51 15.23 15.26 15.26 15.01A.07 .07 0 0 0 15.14 14.99"
               "C15.07 15.2 14.94 15.44 14.95 15.71C14.98 15.96 15.1 16.26 15.27 16.43"
               "C15.44 16.6 15.68 16.68 15.95 16.67C16.23 16.67 16.61 16.59 16.91 16.41"
               "C17.23 16.23 17.45 15.88 17.68 15.7C17.92 15.51 18.17 15.38 18.42 15.2"
               "A.12 .12 0 0 0 18.3 15ZM13.8 17.93C13.94 17.69 14.12 17.43 14.22 17.14"
               "C14.31 16.86 14.34 16.55 14.31 16.29C14.28 16.02 14.15 15.83 14.06 15.63"
               "A.06 .06 0 0 0 13.94 15.67C13.98 15.91 14.09 16.1 14.09 16.31"
               "C14.1 16.53 14.06 16.82 13.98 17.06C13.91 17.3 13.73 17.55 13.6 17.83"
               "A.11 .11 0 0 0 13.8 17.93Z")
#: The finest grooves: short dimples and forks, one shade lighter.
BRAIN_FINE = ("M6.47 9.42C6.53 9.59 6.65 9.74 6.68 9.87C6.71 9.99 6.65 10.17 6.66 10.35"
              "A.04 .04 0 0 0 6.74 10.35C6.78 10.2 6.86 10.03 6.82 9.83C6.78 9.65 6.64 9.51 6.53 9.38"
              "A.04 .04 0 0 0 6.47 9.42ZM17.47 9.43C17.38 9.56 17.25 9.7 17.23 9.89"
              "C17.21 10.08 17.3 10.25 17.36 10.41A.04 .04 0 0 0 17.44 10.39"
              "C17.43 10.21 17.36 10.03 17.37 9.91C17.38 9.78 17.49 9.64 17.53 9.47"
              "A.04 .04 0 0 0 17.47 9.43ZM4.72 13.37C4.85 13.6 5.09 13.81 5.12 13.97"
              "C5.17 14.11 5.04 14.32 5.01 14.54A.04 .04 0 0 0 5.09 14.56"
              "C5.16 14.38 5.33 14.19 5.28 13.93C5.21 13.69 4.95 13.5 4.78 13.33"
              "A.04 .04 0 0 0 4.72 13.37ZM9.07 6.78C9.24 6.72 9.41 6.56 9.5 6.58"
              "C9.59 6.56 9.76 6.72 9.93 6.78A.04 .04 0 0 0 9.97 6.72C9.84 6.62 9.71 6.44 9.5 6.42"
              "C9.29 6.44 9.16 6.62 9.03 6.72A.04 .04 0 0 0 9.07 6.78ZM12.82 15.87"
              "C12.88 16.04 13.01 16.18 13.03 16.31C13.05 16.44 12.97 16.65 12.96 16.84"
              "A.04 .04 0 0 0 13.04 16.86C13.1 16.69 13.2 16.49 13.17 16.29"
              "C13.14 16.09 12.98 15.96 12.88 15.83A.04 .04 0 0 0 12.82 15.87ZM7 12.58"
              "C7.17 12.63 7.36 12.61 7.47 12.66C7.58 12.71 7.68 12.87 7.83 12.97"
              "A.04 .04 0 0 0 7.87 12.93C7.78 12.8 7.7 12.62 7.53 12.54C7.35 12.46 7.16 12.51 7 12.52"
              "A.04 .04 0 0 0 7 12.58ZM11.27 16.68C11.17 16.79 11.02 16.91 10.98 17.09"
              "C10.96 17.27 11.06 17.42 11.12 17.56A.04 .04 0 0 0 11.18 17.54"
              "C11.17 17.38 11.09 17.21 11.12 17.11C11.13 17.01 11.26 16.87 11.33 16.72"
              "A.04 .04 0 0 0 11.27 16.68ZM8.82 13.79C8.76 13.93 8.67 14.07 8.68 14.26"
              "C8.7 14.44 8.83 14.58 8.92 14.71A.04 .04 0 0 0 8.98 14.69"
              "C8.94 14.52 8.83 14.36 8.82 14.24C8.8 14.13 8.87 13.97 8.88 13.81"
              "A.04 .04 0 0 0 8.82 13.79ZM19.37 7.33C19.28 7.45 19.16 7.57 19.13 7.74"
              "C19.12 7.91 19.21 8.04 19.27 8.16A.04 .04 0 0 0 19.33 8.14"
              "C19.32 7.99 19.25 7.85 19.27 7.76C19.28 7.66 19.38 7.52 19.43 7.37"
              "A.04 .04 0 0 0 19.37 7.33Z")
#: Light along the crest of a few ridges on the lit side.
BRAIN_CREST = ("M8.1 4.78C8.45 4.78 8.79 4.7 9.1 4.7C9.42 4.69 9.75 4.75 10.1 4.73"
               "A.03 .03 0 0 0 10.1 4.67C9.78 4.58 9.45 4.5 9.1 4.5C8.75 4.51 8.42 4.62 8.1 4.72"
               "A.03 .03 0 0 0 8.1 4.78ZM4.37 7.44C4.23 7.7 4.09 7.99 4.05 8.29"
               "C4.02 8.59 4.1 8.85 4.17 9.1A.03 .03 0 0 0 4.23 9.1C4.27 8.82 4.21 8.56 4.25 8.31"
               "C4.28 8.06 4.4 7.76 4.43 7.46A.03 .03 0 0 0 4.37 7.44ZM13.21 5.18"
               "C13.56 5.12 13.9 4.97 14.21 4.94C14.52 4.9 14.86 4.96 15.2 4.93"
               "A.03 .03 0 0 0 15.2 4.87C14.88 4.81 14.55 4.72 14.19 4.76"
               "C13.83 4.81 13.51 4.98 13.19 5.12A.03 .03 0 0 0 13.21 5.18ZM4.28 11.6"
               "C4.18 11.83 4.08 12.09 4.07 12.35C4.07 12.6 4.15 12.8 4.23 13A.02 .02 0 0 0 4.27 13"
               "C4.28 12.77 4.22 12.57 4.23 12.35C4.24 12.14 4.32 11.87 4.32 11.6"
               "A.02 .02 0 0 0 4.28 11.6ZM5.93 15.76C6.03 16.02 6.12 16.31 6.29 16.54"
               "C6.47 16.77 6.71 16.91 6.94 17.07A.02 .02 0 0 0 6.96 17.03"
               "C6.79 16.82 6.56 16.66 6.41 16.46C6.25 16.26 6.14 15.98 5.97 15.74"
               "A.02 .02 0 0 0 5.93 15.76ZM18.94 6.91C19.08 7.11 19.27 7.26 19.39 7.44"
               "C19.52 7.62 19.59 7.85 19.73 8.06A.02 .02 0 0 0 19.77 8.04"
               "C19.71 7.81 19.65 7.57 19.51 7.36C19.36 7.16 19.16 7.02 18.96 6.89"
               "A.02 .02 0 0 0 18.94 6.91Z")
#: The rim: the outline stroked at each of these widths, faintly, inside the
#: clip, so the light rolls off every lobe of it; stacked, it fades inward.
BRAIN_RIM = (3.2, 2.8, 2.4, 2.05, 1.75, 1.45, 1.2, .95)
#: Each groove's lit lip: the groove itself, moved this far toward the lower
#: right (away from the light) and drawn under it, pale.
BRAIN_LIP = ".07 .085"

# --- the mould ----------------------------------------------------------------
#: Drawn in whole tenths of your peak week. The like-for-like figure is honest
#: to roughly +/-15 points (20-26 Sep lies between 61% and 91% of 13-19 Sep
#: once coverage is allowed for), so drawing it to the percent would be false
#: precision. Steps are never named or numbered on the page.
STEPS = 10

#: Where mould grows: (cx, cy, born, r_first, r_full). `born` is the first
#: drawn step a colony exists on. The first three are the icon's three specks,
#: at the icon's coordinates and radii, so the first step IS the mark. The rest
#: were placed once (farthest-point, 2.2 units apart, centres at least 1.15
#: inside the outline) and ordered outward from the specks, alternating
#: hemispheres, so rot spreads from where the mark puts it. Frozen here as
#: literals: nothing is generated at render time.
SEATS = (
    (8.2, 9.1, 1, 1.15, 1.90), (15.4, 12.4, 1, 1.05, 1.80), (10.9, 14.9, 1, 0.90, 1.61),
    (10.2, 7.9, 2, 0.42, 1.35), (13.8, 14.5, 2, 0.42, 1.51), (10.2, 10.3, 2, 0.42, 1.35),
    (12.2, 17.3, 3, 0.42, 1.56), (6.0, 10.9, 3, 0.42, 1.63), (12.4, 12.3, 4, 0.42, 1.51),
    (7.0, 6.1, 4, 0.42, 1.45), (17.2, 14.9, 4, 0.42, 1.45), (8.8, 12.3, 5, 0.42, 1.40),
    (17.8, 10.3, 5, 0.42, 1.40), (7.8, 16.5, 6, 0.42, 1.40), (15.8, 8.9, 6, 0.42, 1.40),
    (4.8, 8.3, 6, 0.42, 1.47), (19.2, 12.3, 7, 0.42, 1.40), (10.0, 5.3, 7, 0.42, 1.51),
    (12.4, 9.3, 8, 0.42, 1.40), (5.6, 13.7, 8, 0.42, 1.47), (15.0, 16.9, 8, 0.42, 1.42),
    (12.2, 6.7, 9, 0.42, 1.35), (19.2, 8.3, 9, 0.42, 1.40), (17.2, 6.3, 10, 0.90, 0.90),
    (14.4, 5.3, 10, 0.90, 0.90),
)
#: Radius follows sqrt of age, so each colony's AREA grows evenly per step and
#: the covered share of the brain tracks the ratio. Measured by rasterising the
#: outline: mould plus its fuzz covers 14%, 28%, 48%, 61% and 86% of what it
#: covers at the peak, at steps 2, 4, 6, 7 and 9; the last step still changes
#: 16% of the brain's area (the mould itself), more than steps 2 to 4 together.
GROWTH = 0.5
GOLDEN_DEG = 137.508
#: A grown colony is a soft cartoon cloud: a ring of round, overlapping bumps
#: joined by round valleys, nothing pointed. Below PUFF_FROM_R it is still a
#: round seedling. Six bumps on small colonies, up to eight on big ones, so
#: each bump stays about one size.
PUFF_FROM_R = 0.7
PUFF_SCALLOPS = (6, 8)
#: How far neighbouring bumps overlap (bump radius over half the gap between
#: bump centres): more is a rounder, shallower cloud.
PUFF_OVERLAP = 1.75
#: The round valley between two bumps, as a share of r.
PUFF_FILLET = 0.15
#: The bump tips reach this far past r and the valleys sit about as far inside
#: it, so a puff covers about what a circle of r would: coverage, and so the
#: level, reads as a circle's would.
PUFF_REACH = 1.07
#: Age tiers. 0: the icon's own flat speck (step 1 only). 1: a seedling, born at
#: the drawn step, a shade paler than the speck. 2: a young puff. 3: a ripe puff, the
#: palest. Ripening softens the colour, so the peak week is a soft mat.
RIPE_FROM_AGE = 4
#: One short curved highlight on each puff from this radius, upper left.
SHINE_FROM_R = 0.6
SHINE_ARC = (198, 246)          # degrees, y down: the upper-left shoulder
SHINE_AT, SHINE_W = 0.6, 0.13   # of r: the arc's radius and its stroke width
#: Drifting motes: one from each puff at least this big, the first few by
#: seat order (so the same week always lets off the same ones). Motion only.
MOTE_FROM_R, MOTE_MAX = 1.2, 4
#: The pale fuzz round a grown puff thickens with its age (stroke width, brain
#: units), so at the peak week the oldest growth knits into one soft mat.
FUZZ_W, FUZZ_PER_AGE = 0.42, 0.06


def _n(x: float) -> str:
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _circle(cx: float, cy: float, r: float) -> str:
    return (f"M{_n(cx - r)} {_n(cy)}a{_n(r)} {_n(r)} 0 1 0 {_n(2 * r)} 0"
            f"a{_n(r)} {_n(r)} 0 1 0 {_n(-2 * r)} 0Z")


def _puff(cx: float, cy: float, r: float, seat: int) -> str:
    """A closed cartoon-cloud outline round (cx, cy): n round bumps on a ring,
    joined by small round valleys, so nothing on it comes to a point.

    Each bump is an arc of a circle (Cj, rb); each valley is a fillet, an arc of
    a little circle touching both neighbouring bumps from outside. The ring
    turns by the seat's golden angle and the bumps wobble a little by seat,
    deterministically, so no two puffs are stamped from one mould.
    """
    n = max(PUFF_SCALLOPS[0], min(PUFF_SCALLOPS[1], round(4.2 + 2 * r)))
    half = math.pi / n
    k = PUFF_OVERLAP * math.sin(half)                  # bump radius over ring radius
    ring = PUFF_REACH * r / (1 + k)
    rot = seat * GOLDEN_DEG * math.pi / 180
    bumps = []
    for j in range(n):
        t = rot + 2 * half * j + 0.16 * half * math.sin(seat * 1.3 + j * 2.1)
        rc = ring * (1 + 0.04 * math.sin(seat * 0.7 + j * 1.9))
        rb = k * ring * (1 + 0.07 * math.sin(seat * 1.1 + j * 2.9))
        bumps.append((cx + rc * math.cos(t), cy + rc * math.sin(t), rb))
    f = PUFF_FILLET * r
    joins = []                                         # (on bump j, on bump j+1)
    for j in range(n):
        (x1, y1, r1), (x2, y2, r2) = bumps[j], bumps[(j + 1) % n]
        a, b = r1 + f, r2 + f
        dx, dy = x2 - x1, y2 - y1
        d = math.hypot(dx, dy)
        along = (a * a - b * b + d * d) / (2 * d)
        h = math.sqrt(max(a * a - along * along, 0.0))
        mx, my = x1 + along * dx / d, y1 + along * dy / d
        cands = [(mx - h * dy / d, my + h * dx / d), (mx + h * dy / d, my - h * dx / d)]
        fx, fy = max(cands, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)   # the outer one
        joins.append(((x1 + r1 * (fx - x1) / a, y1 + r1 * (fy - y1) / a),
                      (x2 + r2 * (fx - x2) / b, y2 + r2 * (fy - y2) / b)))
    d = [f"M{_n(joins[-1][1][0])} {_n(joins[-1][1][1])}"]
    for j in range(n):
        x, y, rb = bumps[j]
        (sx, sy), (ex, ey) = joins[j - 1][1], joins[j][0]
        span = (math.atan2(ey - y, ex - x) - math.atan2(sy - y, sx - x)) % (2 * math.pi)
        d.append(f"A{_n(rb)} {_n(rb)} 0 {int(span > math.pi)} 1 {_n(ex)} {_n(ey)}")
        nx, ny = joins[j][1]
        d.append(f"A{_n(f)} {_n(f)} 0 0 0 {_n(nx)} {_n(ny)}")
    return "".join(d) + "Z"


def _shine(cx: float, cy: float, r: float) -> tuple[str, str]:
    """A short arc on the upper-left shoulder: (stroke path, stroke width)."""
    a1, a2 = (math.radians(a) for a in SHINE_ARC)
    R = SHINE_AT * r
    x1, y1 = cx + R * math.cos(a1), cy + R * math.sin(a1)
    x2, y2 = cx + R * math.cos(a2), cy + R * math.sin(a2)
    return f"M{_n(x1)} {_n(y1)}A{_n(R)} {_n(R)} 0 0 1 {_n(x2)} {_n(y2)}", _n(SHINE_W * r)


@dataclass(frozen=True)
class Colony:
    seat: int
    cx: float
    cy: float
    r: float
    age: int
    d: str                         # its outline: a cloud once grown, else a disc
    disc: str                      # the disc of r, which undecided time is drawn as
    shine: tuple[str, str] | None  # (d, w): a grown puff's highlight


def _colonies_at(step: int) -> tuple[Colony, ...]:
    out = []
    for i, (cx, cy, born, r0, rf) in enumerate(SEATS):
        if step < born:
            continue
        age = step - born
        p = age / (STEPS - born) if born < STEPS else 0.0
        r = r0 + (rf - r0) * p ** GROWTH
        grown = age > 0 and r >= PUFF_FROM_R
        out.append(Colony(i, cx, cy, round(r, 3), age,
                          _puff(cx, cy, r, i) if grown else _circle(cx, cy, r), _circle(cx, cy, r),
                          _shine(cx, cy, r) if grown and r >= SHINE_FROM_R else None))
    return tuple(out)


#: Every step's colonies, computed once at import: a render is selection only.
STEP_COLONIES = tuple(_colonies_at(s) for s in range(STEPS + 1))


def drawn_step(ratio: float | None) -> int | None:
    """The nearest whole tenth, never rounded up past it, and capped at the peak.

    Any brain rot at all draws at least step 1, the icon's three specks: a clean
    brain means zero minutes and nothing else. round(x, 6) first, so float noise
    such as 0.7 * 10 = 7.000000000000001 can never move a step. Half-tenths go
    up (floor(x + 0.5)), not to the even neighbour as Python's round() would.
    """
    if ratio is None:
        return None
    if ratio <= 0:
        return 0
    return min(STEPS, max(1, math.floor(round(ratio * STEPS, 6) + 0.5)))


# --- the mushrooms ------------------------------------------------------------
#: From FRUIT_FROM% of your peak week the mould fruits, at Jacob's request
#: (DR5.22): little caramel toadstools come up out of its older colonies, the
#: mark's three specks first. Every whole point past it makes every one a
#: little bigger, and every few points another comes up, until FRUIT_FULL, the
#: page's own new-peak line (progress.LEVEL_PCT above the peak). There they are
#: full grown and stop, so the drawing never claims more than the words beside
#: it. Drawn by the whole point the page prints, though the figure itself is
#: only good to about +/-15 (see STEPS): the growth is kept small for that.
FRUIT_FROM = 85
FRUIT_FULL = 100 + round(progress.LEVEL_PCT)
#: (born, host seat, foot x, foot y, cap width when born, full grown, tilt in
#: degrees, button), in the order they come up. A toadstool stands about as
#: tall as its cap is wide; a button is the young, rounder kind, beside an
#: older one. Placed once by hand in brain units: three clumps on the mark's
#: three specks and one alone at the upper right, every foot inside its host
#: colony from the point it comes up (older colonies, which undecided time
#: reaches late), clear of the midline, the outline and one another. Frozen
#: here: nothing is generated at render time.
SHROOMS = (
    (85, 0, 8.15, 10.0, 1.3, 2.4, -5, False),
    (87, 1, 15.6, 13.3, 1.15, 2.15, 4, False),
    (90, 5, 9.65, 10.45, .9, 1.4, 10, True),
    (93, 2, 10.7, 15.9, 1.1, 1.95, -4, False),
    (96, 4, 14.25, 13.65, .9, 1.35, -9, True),
    (100, 14, 16.05, 9.75, 1.0, 1.7, 5, False),
    (104, 6, 11.85, 16.35, .85, 1.25, 8, True),
)
#: Each grows fastest just after it comes up and slows toward full size:
#: w = born + (full - born) * (1 - (1 - t) ** FRUIT_EASE), t its share of the
#: way from its own first point to FRUIT_FULL. So the first, alone for two
#: points, grows visibly from the start, and those already up gain at least
#: .065 of cap width between them at every point (about 1px at 320px); near
#: full size the oldest grow by less than a drawn hundredth, the young carry it.
FRUIT_EASE = 1.6
#: Cream spots on a cap: (across the cap, -1..1; up it, 0 rim..1 crown; radius
#: over cap width), the biggest first, so a small cap keeps the biggest. Two
#: sets, alternated, and never where the cap catches the light (upper left).
SPOT_SETS = (
    ((.4, .62, .085), (-.06, .87, .07), (-.62, .3, .06), (.8, .26, .05), (.12, .38, .055)),
    ((.3, .78, .08), (-.18, .46, .065), (.7, .36, .06), (-.68, .2, .045), (.08, .2, .05)),
)
#: How many spots by cap width: (from width, count), so a cap gains spots as it grows.
SPOT_COUNT = ((0, 2), (1.0, 3), (1.4, 4), (1.8, 5))
SPOT_MIN_R = .075               # brain units: a spot never shrinks out of sight
#: A low mound of ripe mould round each foot, in front of the stem, so it comes
#: up through the mould rather than standing on it: three bumps (x, y, r over
#: the cap's width, from the foot), never narrower than MOUND_MIN_W's would be.
MOUND = ((-.21, -.02, .1), (-.01, -.06, .13), (.19, -.025, .105))
MOUND_BASE, MOUND_MIN_W = .04, 1.0


@dataclass(frozen=True)
class Shroom:
    born: int                   # the whole point it comes up at
    seat: int                   # its host colony: drawn only while that is brain rot
    i: int                      # the order it comes up in, which staggers the motion
    x: str                      # its foot, which it rises from and tilts about
    y: str
    w: float                    # cap width, brain units
    tilt: str
    gills: str
    stem: str
    shade: str                  # the cap's shade on the stem
    cap: str
    spots: str
    shine: str                  # one highlight, upper left (a stroke shine_w wide)
    shine_w: str
    mound: str                  # the mould it comes up through


def _ellipse(x: float, y: float, rx: float, ry: float, turn: float = 0.0) -> str:
    c, s = math.cos(math.radians(turn)), math.sin(math.radians(turn))
    x1, y1, x2, y2 = x - rx * c, y - rx * s, x + rx * c, y + rx * s
    a = f"A{_n(rx)} {_n(ry)} {_n(turn)} 1 0"
    return f"M{_n(x1)} {_n(y1)}{a} {_n(x2)} {_n(y2)}{a} {_n(x1)} {_n(y1)}Z"


def _mound(x: float, y: float, w: float) -> str:
    """A low cloud of three round bumps over a flat base just below the foot:
    each bump an arc over its top, meeting the next at their upper crossing."""
    s = max(w, MOUND_MIN_W)
    bumps = [(x + dx * s, y + dy * s, r * s) for dx, dy, r in MOUND]
    base = y + MOUND_BASE * s
    meets = []
    for (x1, y1, r1), (x2, y2, r2) in zip(bumps, bumps[1:]):
        dx, dy = x2 - x1, y2 - y1
        d = math.hypot(dx, dy)
        along = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
        h = math.sqrt(max(r1 * r1 - along * along, 0.0))
        mx, my = x1 + along * dx / d, y1 + along * dy / d
        meets.append(min(((mx - h * dy / d, my + h * dx / d), (mx + h * dy / d, my - h * dx / d)),
                         key=lambda p: p[1]))                        # the upper crossing
    (lx, ly, lr), (rx, ry, rr) = bumps[0], bumps[-1]
    d = [f"M{_n(lx - lr)} {_n(ly)}"]
    for (cx, cy, r), (ex, ey) in zip(bumps, meets + [(rx + rr, ry)]):
        d.append(f"A{_n(r)} {_n(r)} 0 0 1 {_n(ex)} {_n(ey)}")
    d.append(f"Q{_n(rx + rr)} {_n(base)} {_n(rx)} {_n(base)}L{_n(lx)} {_n(base)}"
             f"Q{_n(lx - lr)} {_n(base)} {_n(lx - lr)} {_n(ly)}Z")
    return "".join(d)


def _toadstool(born: int, seat: int, i: int, x: float, y: float, w: float, tilt: int,
               button: bool) -> Shroom:
    """One toadstool standing on (x, y), upright: a domed cap with a little lip,
    the gills just showing under it, a short chubby stem, cream spots, one
    highlight upper left. Symmetric about x below the cap, with nothing below
    y, so the bottom middle of its box is its foot, which it grows from and
    tilts about."""
    R = w / 2                                                   # cap half-width
    rh, ch = (.3 * w, .58 * w) if button else (.4 * w, .5 * w)  # rim height, cap height
    fw, tw = (.23 * w, .17 * w) if button else (.21 * w, .15 * w)  # stem: foot, top half-widths
    rim, top = y - rh, y - rh - ch

    def at(u: float, v: float) -> tuple[float, float]:
        return x + u * R, rim - v * ch

    def P(*pts: tuple[float, float]) -> str:
        return " ".join(f"{_n(px)} {_n(py)}" for px, py in pts)

    st = y - rh - .06 * w                                       # the stem's top, under the cap
    stem = (f"M{P((x - tw, st))}"
            f"C{P((x - tw - .03 * w, y - .6 * rh), (x - fw - .02 * w, y - .25 * rh), (x - fw, y - .06 * w))}"
            f"Q{P((x - fw + .005 * w, y), (x - fw + .07 * w, y))}L{P((x + fw - .07 * w, y))}"
            f"Q{P((x + fw - .005 * w, y), (x + fw, y - .06 * w))}"
            f"C{P((x + fw + .02 * w, y - .25 * rh), (x + tw + .03 * w, y - .6 * rh), (x + tw, st))}Z")
    lift = .7 if button else .62                                # how soon the dome turns over
    cap = (f"M{P((x - R, rim))}C{P((x - R - .01 * w, rim - lift * ch), (x - .56 * R, top), (x, top))}"
           f"C{P((x + .56 * R, top), (x + R + .01 * w, rim - lift * ch), (x + R, rim))}"
           f"Q{P((x + R + .005 * w, rim + .05 * w), (x + R - .08 * w, rim + .045 * w))}"
           f"Q{P((x, rim - .05 * w), (x - R + .08 * w, rim + .045 * w))}"
           f"Q{P((x - R - .005 * w, rim + .05 * w), (x - R, rim))}Z")
    n = next(c for lo, c in reversed(SPOT_COUNT) if w >= lo)
    spots = "".join(_ellipse(*at(u, v), max(s * w, SPOT_MIN_R), .8 * max(s * w, SPOT_MIN_R), 22 * u)
                    for u, v, s in SPOT_SETS[i % 2][:n])
    a, c, b = at(-.74, .36), at(-.7, .82), at(-.28, .88)
    return Shroom(born, seat, i, _n(x), _n(y), round(w, 3), str(tilt),
                  gills=_ellipse(x, rim + .035 * w, .41 * w, .065 * w), stem=stem,
                  shade=_ellipse(x, rim + .05 * w, tw, .07 * w), cap=cap, spots=spots,
                  shine=f"M{P(a)}Q{P(c, b)}", shine_w=_n(.075 * w), mound=_mound(x, y, w))


def _shrooms_at(pct: int) -> tuple[Shroom, ...]:
    out = []
    for i, (born, seat, x, y, w0, w1, tilt, button) in enumerate(SHROOMS):
        if pct < born:
            continue
        t = (pct - born) / (FRUIT_FULL - born)
        w = w0 + (w1 - w0) * (1 - (1 - t) ** FRUIT_EASE)
        out.append(_toadstool(born, seat, i, x, y, w, tilt, button))
    return tuple(sorted(out, key=lambda s: float(s.y)))         # the nearer stands in front


#: Every whole point's toadstools, from FRUIT_FROM to FRUIT_FULL, computed once
#: at import: a render is selection only.
FRUIT_STATES = tuple(_shrooms_at(p) for p in range(FRUIT_FROM, FRUIT_FULL + 1))


def whole_pct(ratio: float) -> int:
    """The ratio in whole points, half-points up, as drawn_step rounds its
    tenths; round(x, 6) first, so float noise such as 0.29 * 100 =
    28.999999999999996 never moves a point."""
    return math.floor(round(ratio * 100, 6) + 0.5)


def fruit_pct(ratio: float | None) -> int | None:
    """The whole point the toadstools are drawn at: None below FRUIT_FROM,
    held at FRUIT_FULL above it."""
    if ratio is None:
        return None
    p = whole_pct(ratio)
    return min(p, FRUIT_FULL) if p >= FRUIT_FROM else None


@dataclass(frozen=True)
class Specimen:
    step: int | None               # 0..10; None draws the blank, grey and unlit
    mould: tuple[dict, ...]        # {ref, d, i, tier, plain, young, w, shine}: i is the seat,
                                   # which staggers the motion; tier 0 is the mark's own flat
                                   # speck (step 1 only), 1 a seedling born this step, a
                                   # shade paler than the speck, 2 a young puff, 3 a ripe one, the palest
    undecided: tuple[dict, ...]    # {d, i}: round, striped, dashed, amber, and still
    spores: tuple[dict, ...]       # {cx, cy, r, i}: motes drifting up off big puffs, motion only
    uid: str
    bloom: tuple[dict, ...] = ()   # {ref, i, w}: grown puffs, under a pale fuzz w wide
    tiers: tuple[int, ...] = ()    # the puff tiers present (1-3), one gradient each
    shrooms: tuple[Shroom, ...] = ()   # from FRUIT_FROM% only, and only on drawn mould
    fruit: int | None = None       # the whole point they are drawn at, while any are
    # The brain itself: the same on every week, blank included (the macro
    # leaves the light off a blank brain).
    outline: str = BRAIN_OUTLINE
    viewbox: str = BRAIN_VIEWBOX
    halves: tuple[str, ...] = BRAIN_HALVES
    rim: tuple[float, ...] = BRAIN_RIM
    glow: tuple[str, ...] = BRAIN_GLOW
    deep: str = BRAIN_DEEP
    sulci: str = BRAIN_SULCI
    fine: str = BRAIN_FINE
    crest: str = BRAIN_CREST
    lip: str = BRAIN_LIP


def specimen(ratio: float | None, undecided_share: float = 0.0, uid: str = "rot") -> Specimen:
    """Geometry for one brain. `ratio` is the shown week over your peak week, like for
    like, uncapped: the mould stops at the peak (drawn_step), the toadstools at FRUIT_FULL.

    `undecided_share` is undecided minutes over the compared minutes of the
    shown week. That share of the mould's area is drawn apart, youngest colonies
    first, a colony counting once at least half of it fits (so a small share
    can draw nothing, and the page then says "too little to draw").
    """
    step = drawn_step(ratio)
    if step is None:
        return Specimen(None, (), (), (), uid)
    cols = STEP_COLONIES[step]
    total = sum(c.r * c.r for c in cols)
    target, taken, undecided = max(0.0, min(1.0, undecided_share)) * total, 0.0, set()
    for c in sorted(cols, key=lambda c: (c.age, -c.seat)):        # youngest first
        a = c.r * c.r
        if taken + a / 2 > target:
            break
        undecided.add(c.seat)
        taken += a
    mould, und, spores = [], [], []
    for c in cols:
        if c.seat in undecided:
            und.append({"d": c.disc, "i": c.seat})
            continue
        # plain: still exactly the mark's own speck (step 1), drawn flat.
        plain = c.age == 0 and SEATS[c.seat][2] == 1
        tier = 0 if plain else 1 if c.age == 0 else 2 if c.age < RIPE_FROM_AGE else 3
        mould.append({"ref": f"{uid}-c{c.seat}", "d": c.d, "i": c.seat, "tier": tier,
                      "plain": plain, "young": c.age == 0,
                      "w": _n(FUZZ_W + FUZZ_PER_AGE * c.age),
                      "shine": {"d": c.shine[0], "w": c.shine[1]} if c.shine else None})
        if c.age > 0 and c.r >= MOTE_FROM_R and len(spores) < MOTE_MAX:   # a grown puff, and big
            turn = math.radians(c.seat * GOLDEN_DEG)
            spores.append({"cx": _n(c.cx + 0.35 * c.r * math.cos(turn)), "cy": _n(c.cy - 0.75 * c.r),
                           "r": _n(0.24 + 0.05 * (c.seat % 3)), "i": len(spores)})
    # The mould fruits only where it is drawn as brain rot: a toadstool whose
    # colony is undecided time stays down, and the rest close up their order.
    fruit = fruit_pct(ratio)
    shrooms = FRUIT_STATES[fruit - FRUIT_FROM] if fruit is not None else ()
    if any(s.seat in undecided for s in shrooms):
        kept = [s for s in shrooms if s.seat not in undecided]
        order = {i: n for n, i in enumerate(sorted(s.i for s in kept))}
        shrooms = tuple(replace(s, i=order[s.i]) for s in kept)
    return Specimen(step, tuple(mould), tuple(und), tuple(spores), uid,
                    shrooms=shrooms, fruit=fruit if shrooms else None,
                    bloom=tuple({"ref": m["ref"], "i": m["i"], "w": m["w"]}
                                for m in mould if not m["young"]),
                    tiers=tuple(sorted({m["tier"] for m in mould if not m["plain"]})))


_COUNT = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine")


def fruit_phrase(n: int, fruit: int) -> str:
    """The toadstools, said in the alt: how many are drawn, and what they mean."""
    one = n == 1
    if fruit >= FRUIT_FULL:
        return (f"{_COUNT[n]} mushroom{'' if one else 's'} sprout{'s' if one else ''} "
                "from the mould, fully grown.")
    return (f"{_COUNT[n]} little mushroom{'' if one else 's'} sprout{'s' if one else ''} from the mould, "
            f"as {'it does' if one else 'they do'} from {FRUIT_FROM}% of your peak week, "
            "a little bigger with each point.")


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


#: A public host name: dotted labels and a letter TLD. Rules out what the web
#: channels also record but nobody can open from here: 127.0.0.1, localhost,
#: browser-extension ids, `file:`.
_HOST = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
#: A YouTube channel id, as the history import stores it.
_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")


def source_url(kind: str | None, key: str | None) -> str | None:
    """Where a source can be looked at: a site's home page, a channel's page, X.

    None for anything with no page to open — apps, podcasts, and any key that
    isn't a plain public host or channel id — so the page never links a guess.
    """
    key = (key or "").strip()
    if kind == T.KIND_WEB_DOMAIN and _HOST.match(key.lower()):
        return f"https://{key.lower()}"
    if kind == T.KIND_YOUTUBE_CHANNEL and _CHANNEL.match(key):
        return f"https://www.youtube.com/channel/{key}"
    if kind == T.KIND_X:
        return "https://x.com"
    return None


def _share(x: float) -> str:
    """A share of the peak week in whole points, half-points up (whole_pct), so
    the number beside the brain and its mushrooms count the same point. Any
    brain rot at all draws mould, so a share above zero never prints as 0%."""
    s = f"{whole_pct(x)}%"
    return "under 1%" if x > 0 and s == "0%" else s


def set_aside_phrase(channels: tuple[str, ...], sources: int) -> str:
    """What a like-for-like comparison left out, counted honestly.

    A whole channel is one line in the set-aside list but may hold dozens of
    sources, so channels and sources are never added together: one channel is
    named, several are counted.
    """
    parts = []
    if len(channels) == 1:
        parts.append(channels[0])
    elif channels:
        parts.append(f"{len(channels)} channels")
    if sources:
        parts.append(f"{sources} source{'' if sources == 1 else 's'}")
    return " and ".join(parts)


#: Where the extension's Sync button is pressed: the one thing that brings
#: YouTube into this week's pace before the week is over.
YOUTUBE_HISTORY_URL = "https://www.youtube.com/feed/history"


def pace_parts(p: pace.Pace) -> dict:
    """pace.Pace, cut for the page: the lead ("So far this week (Sun–Wed)") apart
    from the figures, and the YouTube hint turned into a link. Wording only."""
    lead, sep, rest = p.text.partition(": ")
    left_out = []
    for entry in p.left_out:
        reason, _, hint = entry.partition(" — ")
        sync = entry.startswith("YouTube history") and "run the extension" in hint
        left_out.append({"text": reason if sync else entry, "sync": sync})
    return {"lead": lead if sep else "", "rest": rest if sep else p.text,
            "counted": progress._names(list(p.counted)) if p.counted else "",
            "left_out": left_out,
            "joins_later": progress._names(list(p.joins_later)) if p.joins_later else ""}


def progress_set_aside(p: progress.Progress) -> str:
    """set_aside_phrase where it opens a sentence, as it does in the progress panel."""
    phrase = set_aside_phrase(p.channels_set_aside, p.unreadable_sources + p.below_cut_sources)
    return phrase[:1].upper() + phrase[1:]


def _undecided_min(x: float) -> str:
    """fmt_min, except that a sliver above zero never reads as "0.0 min". It opens
    a sentence wherever it is printed, so it is capitalised."""
    return "Under 0.1 min" if 0 < x < 0.05 else fmt_min(x)


#: Chained steps printed beside the brain, newest first; older ones are counted.
MAX_STEPS_SHOWN = 3

_BLANK = {
    "state": "not_measured", "week_sunday": None, "week_saturday": None, "next_sunday": None,
    "specimen": None, "alt": "", "ratio": None, "pct": None, "is_new_peak": False,
    "over_pct": None, "above_peak": False, "single": False, "after_min": None,
    "before_min": None, "steps": (), "earlier_steps": 0, "peak_sunday": None,
    "peak_saturday": None, "counted_min": None, "week_charged_min": None, "set_aside": "",
    "undecided_min": None, "undecided_text": None, "undecided_drawn": 0,
    "settle_can_move": False, "peak_not_tallest": False, "since_sunday": None, "cut": None,
    "clean_both": False, "fruit_from": None,
}


def rot_view(chain: progress.PeakChain | None, splits: list[report.WeekSplit],
             uid: str = "rot", eye: Sequence[report.EyeRow] = ()) -> dict:
    """Everything #rot prints and draws. Selection and wording only.

    `chain` is progress.peak_chain(...) or None; `splits` is the list
    report.label_split already returned to overview(); `eye` is
    report.needs_your_eye's rows, so the page only points at "Readings to
    settle" when that section is there to point at. Never None, so the
    section renders on an empty database and before any summary exists.
    """
    by_start = {s.week_start: s for s in splits}
    # Every key, always: the template branches on state and never meets an Undefined.
    base = dict(_BLANK)
    if chain is None:
        return base | {"specimen": specimen(None, uid=uid),
                       "alt": "Grey brain outline, nothing drawn: no week has finished with its "
                              "evidence in yet."}
    nxt = (date.fromisoformat(chain.week_sunday) + timedelta(days=7)).isoformat()
    base |= {"state": chain.state, "week_sunday": chain.week_sunday,
             "week_saturday": chain.week_saturday,
             "next_sunday": nxt if any(s.week_start > chain.week_start for s in splits) else None}
    who = f"the week of Sun {chain.week_sunday}"
    if chain.state == "first_week":
        return base | {"specimen": specimen(None, uid=uid),
                       "alt": (f"Grey brain outline, nothing drawn: {who} is the first finished "
                               "week, so it sets the benchmark.")}
    if chain.state == "not_comparable":
        return base | {"specimen": specimen(None, uid=uid),
                       "alt": (f"Grey brain outline, nothing drawn: too little of {who} and the "
                               "week before was measured the same way to compare them.")}
    if chain.state == "nothing_before":
        # Not thin, so the week before had no brain rot at all. If this week had
        # none either, and nothing was left out or unreadable, that is a clean
        # brain, not an absence.
        last = chain.steps[-1]
        clean = (last.after_min <= 0 and not chain.unreadable
                 and not last.channels_set_aside and not last.sources_set_aside)
        return base | {"specimen": specimen(0.0 if clean else None, uid=uid), "clean_both": clean,
                       "counted_min": last.after_min,
                       "set_aside": set_aside_phrase(last.channels_set_aside,
                                                     last.sources_set_aside),
                       "alt": (f"Brain with no mould: no brain rot in {who} or the week before."
                               if clean else
                               f"Grey brain outline, nothing drawn: the week before {who} had no "
                               "brain rot, so there is nothing to scale it against.")}

    last = chain.steps[-1]
    ratio = chain.ratio
    single = len(chain.steps) == 1
    # A new peak only past the progress panel's own level rule, so the lead row
    # never announces a record for a change the panel below calls level.
    new_peak = ratio > 1 and (ratio - 1) * 100 >= progress.LEVEL_PCT and (
        not single or last.direction == "up")
    undecided_share = (last.undecided_after_min / last.after_min) if last.after_min > 0 else 0.0
    # Uncapped: drawn_step caps the mould. Full-grown toadstools mean a new peak,
    # so a rise the page calls level (too few minutes to count) stops a point short.
    sp = specimen(ratio if new_peak else min(ratio, (FRUIT_FULL - 1) / 100), undecided_share, uid)
    newest = list(reversed(chain.steps))
    earlier = max(0, len(newest) - MAX_STEPS_SHOWN)
    first_against = "your previous peak week" if new_peak else "your peak week"
    steps = [{"sunday": l.sunday,
              "against": first_against if l is chain.steps[0] else "the week before",
              "before_min": l.before_min, "after_min": l.after_min,
              "change": ("level" if l.direction == "level"
                         else f"{l.direction} {_pct(l.change)}")} for l in newest[:MAX_STEPS_SHOWN]]
    # The week the page now calls your peak: the shown week itself whenever it is
    # above the old one, called a new peak or not, because chain_index makes it the
    # benchmark from next week either way.
    bench = by_start.get(chain.week_start if ratio > 1 else chain.peak_start)
    charged = by_start[chain.week_start].charged_min if chain.week_start in by_start else None
    # "Tallest bar" means what split_bars draws: every counted minute, worthwhile too.
    bar = lambda s: s.charged_min + s.worthwhile_min            # noqa: E731
    peak_not_tallest = bench is not None and any(bar(s) > bar(bench) for s in splits)

    peak = f"Sun {chain.peak_sunday}"
    if new_peak and single:
        alt = (f"Brain covered in mould: {who} is a new peak week, {fmt_min(last.after_min)} of brain "
               f"rot against {fmt_min(last.before_min)} in your previous peak week of {peak}, "
               "counting only what both weeks measured.")
    elif new_peak:
        alt = (f"Brain covered in mould: {who} is a new peak week, about {_share(ratio - 1)} above "
               f"your previous peak week of {peak}, taking each week against the one before it.")
    elif ratio == 0 and single:
        alt = (f"Brain with no mould: {who} had no brain rot on what it and your peak week of "
               f"{peak} both measured.")
    elif ratio == 0:
        alt = (f"Brain with no mould: {who} had no brain rot on what it and the week before both "
               f"measured, so 0% of your peak week of {peak}.")
    elif single:
        alt = (f"Brain with mould for {_share(min(ratio, 1.0))} of your peak week. Week of Sun "
               f"{chain.week_sunday}: {fmt_min(last.after_min)} of brain rot against "
               f"{fmt_min(last.before_min)} in your peak week of {peak}, counting only what both "
               "weeks measured.")
    else:
        parts = ", ".join(f"Sun {s['sunday']} {s['change']} ({fmt_min(s['before_min'])} to "
                          f"{fmt_min(s['after_min'])})" for s in reversed(steps))
        if earlier:
            parts = f"{earlier} earlier week{'' if earlier == 1 else 's'} from {peak}, then {parts}"
        share = _share(min(ratio, 1.0))
        share = share if share.startswith("under") else f"about {share}"
        alt = (f"Brain with mould for {share} of your peak week, {peak}, "
               f"taking each week against the one before it: {parts}.")
    if sp.shrooms:
        alt += " " + fruit_phrase(len(sp.shrooms), sp.fruit)
    if sp.undecided:
        alt += (f" {_undecided_min(last.undecided_after_min)} of the week's "
                f"{fmt_min(last.after_min)} is undecided time, drawn as dashed patches.")

    return base | {
        "specimen": sp, "alt": alt,
        "ratio": ratio, "pct": _share(min(ratio, 1.0)), "is_new_peak": new_peak,
        "over_pct": _share(ratio - 1) if new_peak else None,
        "above_peak": ratio > 1 and not new_peak,
        "single": single,
        "after_min": last.after_min if single else None,
        "before_min": last.before_min if single else None,
        "steps": tuple(steps), "earlier_steps": earlier,
        "peak_sunday": chain.peak_sunday, "peak_saturday": chain.peak_saturday,
        "counted_min": last.after_min, "week_charged_min": charged,
        "set_aside": set_aside_phrase(last.channels_set_aside, last.sources_set_aside),
        "undecided_min": last.undecided_after_min,
        "undecided_text": _undecided_min(last.undecided_after_min),
        "undecided_drawn": len(sp.undecided),
        # Only a reading in the weeks the ratio was chained across can move it.
        "settle_can_move": bool(chain.unreadable) and any(
            (chain.from_sunday or "") <= r.day <= chain.week_saturday for r in eye),
        "peak_not_tallest": peak_not_tallest,
        "since_sunday": chain.since_sunday, "cut": chain.cut,
        # Said beside the drawing only while mushrooms are on it.
        "fruit_from": FRUIT_FROM if sp.shrooms else None,
    }


def week_imports(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                 totals: list[report.SourceTotal]) -> list[report.ImportSummary]:
    """The imports the week's rows report on, so "partial" links to *its* warnings.

    The latest import of a kind is often a later week's; linking a week's
    partial status there would show the wrong warnings.
    """
    ids = sorted({t.import_id for t in totals if t.import_id is not None})
    return [d for d in (report.import_detail(conn, clk, settings, i) for i in ids) if d is not None]


def reflex_total(gated: list[report.GatedRow]) -> float:
    """Sum of the per-group wasteful ungated minutes `report` computed. Addition only."""
    return sum(r.wasteful_ungated_min for r in gated)


def label_toggleable(source: labels.Source) -> bool:
    return not (source.kind == T.KIND_YOUTUBE_CHANNEL and source.source_key == T.UNKNOWN_YOUTUBE_CHANNEL_KEY)


# --------------------------------------------------------------------------
# Template filters
# --------------------------------------------------------------------------

def fmt_min(value: float | None) -> str:
    if value is None:
        return "not measured"
    if value == 0:
        return "0 min"
    if value < 10:
        return f"{value:.1f} min"
    return f"{value:,.0f} min"


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


#: Quantities inside generated prose: a number with a unit. Bolding these
#: gives the paragraph a spine you can skim without the model having to emit
#: markup, which it must never do — see `emphasise`.
_QUANTITY = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*-?\s*(?:minutes?|mins?\b|percent|hours?|hrs?)|\b\d[\d,]*(?:\.\d+)?\s*%",
    re.IGNORECASE,
)


def emphasise(text: str) -> Markup:
    """Bold the figures in a generated paragraph.

    The text is model output, so it is **escaped first** and the `<strong>`
    tags are added afterwards: nothing the model writes can become markup.
    Only quantities are emphasised — they are the part a reader skims for, and
    they are the one thing in the sentence this app can recognise reliably
    without guessing at the model's phrasing.
    """
    escaped = str(escape(text or ""))
    return Markup(_QUANTITY.sub(lambda m: f"<strong>{m.group(0)}</strong>", escaped))


def _parse_day(raw: str | None, name: str, errors: list[str]) -> str | None:
    if not raw:
        return None
    try:
        return T.iso_date(date.fromisoformat(raw))
    except ValueError:
        errors.append(f"{name} must be a date YYYY-MM-DD; ignored {raw!r}.")
        return None


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------

def create_app(settings: Settings, clk: Clock, get_conn: Callable[[], sqlite3.Connection]) -> Flask:
    """Build the dashboard. `get_conn()` must return a *new* connection each call."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

    def conn() -> sqlite3.Connection:
        if "drbr_conn" not in g:
            g.drbr_conn = get_conn()
        return g.drbr_conn

    @app.teardown_appcontext
    def _close_conn(_exc):
        c = g.pop("drbr_conn", None)
        if c is not None:
            c.close()

    app.register_blueprint(ingest_api.make_blueprint(
        get_conn=conn, clk=clk, settings=settings, api_factory=lambda: DataApiClient(settings),
    ))

    app.jinja_env.filters["min"] = fmt_min
    app.jinja_env.filters["emphasise"] = emphasise
    app.jinja_env.filters["pct"] = fmt_pct
    app.jinja_env.filters["local_dt"] = lambda iso: clock.fmt_local(iso, settings.tz) if iso else ""
    app.jinja_env.globals.update(
        GROUP_NAMES=GROUP_NAMES, KIND_NAMES=KIND_NAMES, IMPORT_KIND_NAMES=IMPORT_KIND_NAMES,
        PROVENANCE_BADGES=PROVENANCE_BADGES, PROVENANCE_TEXT=T.PROVENANCE_TEXT, BASIS_TEXT=BASIS_TEXT,
        PROV_SCREENSHOT=T.PROV_SCREENSHOT, PROV_BROWSER_SYNC=T.PROV_BROWSER_SYNC, HTMX_URL=HTMX_URL, HTMX_SRI=HTMX_SRI,
        UNKNOWN_CHANNEL_REASON=UNKNOWN_CHANNEL_REASON, WASTEFUL=T.WASTEFUL, WORTHWHILE=T.WORTHWHILE,
        TOOL=T.TOOL, TOOL_TEXT=TOOL_TEXT,
        BRAIN={"outline": BRAIN_OUTLINE, "folds": BRAIN_FOLDS},
        progress_set_aside=progress_set_aside, source_url=source_url, pace_parts=pace_parts,
        YOUTUBE_HISTORY_URL=YOUTUBE_HISTORY_URL,
    )

    @app.context_processor
    def _header():
        return {"countdown": countdown(conn(), clk, settings)}

    @app.before_request
    def _refuse_cross_site_writes():
        if request.method == "POST" and not request.path.startswith("/api/"):
            if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                # `same-site` is refused as well as `cross-site`: another page on
                # another localhost port is same-site, and this app has no CSRF
                # token. A form served by this dashboard always sends
                # `same-origin`, so the guard only fires on a write that came
                # from somewhere else.
                abort(403, description=WRITE_REFUSED)

    @app.errorhandler(403)
    def _explain_refusal(exc):
        """Say why, rather than showing a bare 403 the user cannot act on.

        An htmx request gets a bare fragment: the label toggle swaps the
        response into one `<tr>`, and a full document there renders as
        garbage instead of telling the user anything.
        """
        detail = getattr(exc, "description", "") or WRITE_REFUSED
        if request.headers.get("HX-Request") == "true":
            return render_template("_refused_row.html", detail=detail), 403
        return render_template("refused.html", detail=detail), 403

    @app.get("/")
    def overview():
        c = conn()
        floor = report.window_start(c, clk, settings)
        week = choose_week(clk, settings, request.args.get("week"), floor)
        status = report.allowance_status(c, clk, settings)
        splits = report.label_split(c, clk, settings)
        totals = report.weekly_totals(c, clk, settings, week.week_start)
        gated = report.gated_vs_ungated(c, clk, settings, week.week_start)
        return render_template(
            "overview.html",
            week=week,
            totals=totals,
            chart=split_bars(splits, status.week_start, settings.tz),
            week_split=week_summary(splits, week.week_start),
            allowance=status,
            gated=gated,
            reflex_min=reflex_total(gated),
            keepers=report.cost_per_keeper(c, clk, settings),
            funnels=report.funnels(c, clk, settings),
            blind_spots=report.blind_spots(c, clk, settings),
            imports=report.latest_imports(c, clk, settings),
            week_imports=week_imports(c, clk, settings, totals),
            everything_in=report.everything_in(totals),
            top_sources=report.top_sources(c, clk, settings, week.week_start),
            progress=progress.progress(c, clk, settings),
            all_sources_count=len(report.top_sources(c, clk, settings, week.week_start, limit=None)),
            window_start=floor,
            needs=report.needs_attention(c, clk, settings),
            provenance_rows=report.provenance_map(c, clk, settings),
            needs_your_eye=(eye := report.needs_your_eye(c, clk, settings)),
            unlabelled=labels.unlabelled_summary(c, clk, settings),
            flash=flash_from(request.args),
            # Appended, never reordered: the design pass reads this call's
            # argument order. `insight` is an insights.Insight or None.
            insight=(latest_insight := insights.latest(c, clk, settings)),
            insight_stale_reason=insight_staleness(c, latest_insight, week.last_complete_sunday,
                                                   settings.tz),
            rot=rot_view(progress.peak_chain(c, clk, settings), splits, eye=eye),
            pace=pace.pace(c, clk, settings),
        )

    def _label_filters(args) -> tuple[str | None, str | None]:
        kind = args.get("kind") or None
        label = args.get("label") or None
        if kind is not None and kind not in T.SOURCE_KINDS:
            abort(400, description=f"unknown kind {kind!r}")
        if label is not None and label not in (*T.SOURCE_LABELS, labels.UNLABELLED):
            abort(400, description=f"unknown label filter {label!r}")
        return kind, label

    @app.get("/labels")
    def labels_page():
        kind, label = _label_filters(request.args)
        return render_template(
            "labels.html",
            sources=labels.list_sources(conn(), kind=kind, label=label),
            f_kind=kind, f_label=label, kinds=T.SOURCE_KINDS, UNLABELLED=labels.UNLABELLED,
            toggleable=label_toggleable, covered=labels.is_covered_elsewhere,
            unlabelled=labels.unlabelled_summary(conn(), clk, settings),
            flash=flash_from(request.args),
        )

    @app.post("/labels/<int:source_id>")
    def set_label(source_id: int):
        c = conn()
        source = labels.get_source(c, source_id)
        if source is None:
            abort(404, description="no such source")
        if not label_toggleable(source):
            abort(400, description=UNKNOWN_CHANNEL_REASON)
        new_label = request.form.get("label", "")
        if new_label not in T.SOURCE_LABELS:
            abort(400, description=f"label must be one of {', '.join(T.SOURCE_LABELS)}")
        source = labels.set_source_label(c, clk, source_id, new_label)
        # DECISIONS DR2.7/DR5.2: a relabel re-records each week's overage now, not at the next import.
        allowance.on_measurements_changed(c, clk, settings)
        log.info("source %d labelled %s from the dashboard", source_id, new_label)
        if request.headers.get("HX-Request") == "true":
            return render_template("_label_row.html", s=source, toggleable=label_toggleable,
                                   covered=labels.is_covered_elsewhere,
                                   f_kind=request.form.get("f_kind") or None,
                                   f_label=request.form.get("f_label") or None)
        params = {k: v for k, v in (("kind", request.form.get("f_kind")),
                                    ("label", request.form.get("f_label"))) if v}
        return redirect(url_for("labels_page", **params) + f"#source-{source_id}", code=303)

    @app.get("/labels/queue")
    def labels_queue():
        """The decisions that move the numbers most, biggest first (DR5.6).

        Its own URL and its own template: `/labels` keeps its table as the
        page's first `<tbody>`, which the dashboard tests read positionally.
        """
        c = conn()
        return render_template(
            "labels_queue.html",
            rows=labels.label_queue(c, clk, settings),
            unlabelled=labels.unlabelled_summary(c, clk, settings),
            seen_text=labels.PROVENANCE_SEEN,
            flash=flash_from(request.args),
        )

    @app.post("/labels/bulk")
    def labels_bulk():
        """Label a whole selection at once — one transaction, one recompute."""
        new_label = request.form.get("label", "")
        if new_label not in T.SOURCE_LABELS:
            abort(400, description=f"label must be one of {', '.join(T.SOURCE_LABELS)}")
        ids = [int(v) for v in request.form.getlist("source_id") if v.isdigit()]
        if not ids:
            abort(400, description="select at least one source")
        changed = labels.bulk_set_label(conn(), clk, ids, new_label, settings=settings)
        log.info("%d source(s) labelled %s in bulk from the dashboard", changed, new_label)
        return redirect(url_for("labels_queue", msg="labelled"), code=303)

    @app.post("/measurements/<int:measurement_id>/fix")
    def fix_measurement(measurement_id: int):
        """A reading OCR could not settle, settled by the person who was there."""
        raw = (request.form.get("minutes") or "").strip()
        try:
            minutes = float(raw)
        except ValueError:
            abort(400, description=f"minutes must be a number, got {raw!r}")
        try:
            screentime.correct_measurement(conn(), clk, settings, measurement_id, minutes)
        except screentime.CorrectionError as exc:
            abort(400, description=str(exc))
        log.info("measurement %d corrected by hand from the dashboard", measurement_id)
        return redirect(url_for("overview", msg="fixed"), code=303)

    @app.post("/sync")
    def sync_now():
        """Run every source DRBR can fetch by itself, now, instead of at 09:30."""
        results = jobs.run_sync(conn(), clk, settings)
        failed = [r.name for r in results.values() if not r.ok]
        for r in results.values():
            log.info("sync %s: %s", r.name, r.summary if r.ok else r.error)
        code = "sync-failed" if failed else "synced"
        return redirect(url_for("overview", msg=code), code=303)

    @app.post("/insights/refresh")
    def refresh_insight():
        """Rewrite this week's summary now, instead of waiting for tomorrow's job.

        `force=True` because `write_summary` writes at most one row a week; this
        is the one way to ask for another. It never raises: a failure stores
        itself and comes back as DRBR's own computed sentences, which is what
        the warn flash says.
        """
        result = insights.write_summary(conn(), clk, settings, force=True)
        log.info("insight refreshed from the dashboard: %s",
                 result.fallback_reason or f"written by {result.model}")
        code = "insight-written" if result.model else "insight-fallback"
        return redirect(url_for("overview", msg=code), code=303)

    @app.get("/records")
    def records_page():
        errors: list[str] = []
        group = request.args.get("group") or None
        label = request.args.get("label") or None
        if group is not None and group not in T.REPORT_GROUPS:
            errors.append(f"Unknown source group {group!r}; ignored.")
            group = None
        if label is not None and label not in T.SOURCE_LABELS:
            errors.append(f"Unknown label {label!r}; ignored.")
            label = None
        start = _parse_day(request.args.get("start"), "Start", errors)
        end = _parse_day(request.args.get("end"), "End", errors)
        c = conn()
        rows = report.records(c, clk, settings, group=group, label=label, start=start, end=end,
                              limit=RECORDS_LIMIT)
        # Records is the full detail page (DR5.12): the surfaces the Overview
        # sheds render here from exactly the values `overview()` computes, so a
        # figure cannot say one thing on one page and another on the other.
        floor = report.window_start(c, clk, settings)
        week = choose_week(clk, settings, request.args.get("week"), floor)
        totals = report.weekly_totals(c, clk, settings, week.week_start)
        status = report.allowance_status(c, clk, settings)
        splits = report.label_split(c, clk, settings)
        gated = report.gated_vs_ungated(c, clk, settings, week.week_start)
        return render_template(
            "records.html", rows=rows, errors=errors, f_group=group, f_label=label, f_start=start,
            f_end=end, groups=T.REPORT_GROUPS, limit=RECORDS_LIMIT,
            window_start=floor,
            week=week,
            totals=totals,
            allowance=status,
            chart=split_bars(splits, status.week_start, settings.tz),
            reflex_min=reflex_total(gated),
            gated=gated,
            keepers=report.cost_per_keeper(c, clk, settings),
            funnels=report.funnels(c, clk, settings),
            imports=report.latest_imports(c, clk, settings),
            week_imports=week_imports(c, clk, settings, totals),
            everything_in=report.everything_in(totals),
        ), (400 if errors else 200)

    return app


def main() -> None:  # production entry point; tests patch Flask.run
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO. The YouTube key travels in a header,
    # but keep the policy the bot and jobs follow.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = config.load()
    setup = db.connect(settings)
    try:
        db.init_db(setup)
        db.migrate(setup)
    finally:
        setup.close()
    if not settings.ingest_token:
        log.warning("DRBR_INGEST_TOKEN is not set: the extension ingest API will refuse every request")
    app = create_app(settings, clock.RealClock(), lambda: db.connect(settings))
    app.run(host="127.0.0.1", port=settings.dashboard_port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
