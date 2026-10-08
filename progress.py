"""progress.py — what got better, and what to try next week (DECISIONS DR5.17).

Until DR5.17 this system only described: the summary was told to give no
advice and no praise, and the Overview said there was nothing to hit. Jacob
asked for the opposite on 30 September — point out real improvements, and offer
concrete tips for the weeks ahead based on what he actually did. This module is
that, under one rule that everything else here serves:

**Compare only what was measured the same way in both weeks.** Three things
make a naive week-on-week comparison lie, and all three were live in his first
two weeks:

1. *A channel only one week had.* From 20 September the phone browser was
   broken into sites; the week before, its minutes were set aside. Counted time
   went 220 -> 344 minutes — "up 56%" — mostly because more was *seen*.
2. *A reading that could not be read.* Facebook on 13-19 September was read as
   either 2h 27m or 2h 37m and stored as unreadable. NULL is not zero, but a
   comparison that forgets that reports Facebook as "rose from nothing to 56
   min" — when it had in fact fallen by about ninety.
3. *A cut-off Screen Time list.* When the Most Used list is not captured to
   its end, apps below the cut are simply absent, and absent reads as zero.

So each source is compared only on channels both weeks had, and only where
both weeks actually hold a value. Anything else is set aside, and the page is
told what was set aside and why — by name where naming it lets him fix it.

Everything here is computed on this Mac and names real sources. None of it is
sent to the summary model, whose payload stays aggregates only (DR5.8).

Nothing here judges content. Tips address only sources *he* labelled wasteful,
name the lever that actually exists for the surface it was on, and never
suggest how anything should be labelled.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Sequence

import allowance
import drbr_types as T
import report
from clock import Clock
from config import Settings

# --- thresholds ---------------------------------------------------------------
# Deliberately conservative: a claim of improvement that turns out to be noise
# costs more trust than a real one left unsaid.

#: A week-on-week change smaller than this, in either unit, reads as "level".
LEVEL_PCT = 10.0
LEVEL_MIN = 15.0
#: A named source counts as improved when it at least halves and by this much.
SOURCE_DROP_SHARE = 0.5
SOURCE_DROP_MIN = 15.0
#: The reflex share must move this many points, on at least this much wasteful time.
REFLEX_POINTS = 15.0
REFLEX_FLOOR_MIN = 30.0
#: A source must be at least this big to be the subject of a tip.
TIP_SOURCE_MIN = 20.0
TIP_RISE_MIN = 20.0
#: The three biggest wasteful sources must be at least this share of it.
CONCENTRATION_SHARE = 0.5
#: Undecided time worth a tip: this share of counted time, and this many minutes.
UNDECIDED_SHARE = 0.25
UNDECIDED_MIN = 30.0

MAX_WINS = 3
MAX_TIPS = 3

#: What each measurement channel is called on the page.
CHANNEL_NAMES = {
    T.PROV_API: "podcast listening",
    T.PROV_PAGE: "YouTube history",
    T.PROV_SCREENSHOT: "Screen Time",
    T.PROV_EXPORT: "the Mac's browser",
    T.PROV_APP_TRACKER: "Mac apps",
    T.PROV_BROWSER_SYNC: "the phone browser by site",
    T.PROV_AUDIBLE_STATS: "audiobook listening",
}

_BRAIN_ROT = (T.WASTEFUL, None)          # labelled wasteful, or not decided yet


@dataclass(frozen=True)
class Finding:
    """One sentence for the page, and the source it is about when there is one."""

    kind: str
    text: str
    source_id: int | None = None


@dataclass(frozen=True)
class Progress:
    """The latest finished week against the one before, like for like."""

    week_start: str                    # allowance week start of the week described
    week_sunday: str
    week_saturday: str
    previous_start: str | None         # None when there is no week before it to compare
    direction: str | None              # "down" | "up" | "level" | None (nothing to compare)
    verdict: str                       # one plain sentence, either direction
    before_min: float | None           # brain-rot minutes both weeks measured the same way
    after_min: float | None
    set_aside: tuple[str, ...]         # what the comparison left out, and why, in plain words
    wins: tuple[Finding, ...]
    tips: tuple[Finding, ...]
    # The same set-aside, without names, for the summary model (DR5.8): channel
    # names are this module's fixed vocabulary, and sources are only counted.
    channels_set_aside: tuple[str, ...] = ()
    unreadable_sources: int = 0
    below_cut_sources: int = 0

    @property
    def like_for_like(self) -> bool:
        return self.previous_start is not None and not self.set_aside


@dataclass(frozen=True)
class _Week:
    start: str
    pooled: allowance.PooledMinutes
    lines: dict                        # source key -> report.SourceLine
    channels: frozenset                # provenances that measured the week
    screenshot_complete: bool          # Screen Time's Most Used list captured to its end
    split: report.WeekSplit | None

    def known(self, key: tuple, prov: str) -> float | None:
        """Brain-rot minutes for a source on a channel, or None when unknown.

        Unknown, never zero: a reading that could not be measured, or a phone
        app absent from a Screen Time list that was cut off before its end.
        """
        if (key, prov) in self.pooled.unmeasured_sources:
            return None
        value = self.pooled.charged_by_source.get(key, {}).get(prov)
        if value is not None:
            return value
        if prov == T.PROV_SCREENSHOT and not self.screenshot_complete:
            return None
        return 0.0

    @property
    def fully_known(self) -> bool:
        """Nothing counted this week is an undercount of what happened."""
        return (not self.pooled.unmeasured_sources
                and (self.screenshot_complete or T.PROV_SCREENSHOT not in self.channels))


# --- helpers ------------------------------------------------------------------

def _m(value: float) -> str:
    """Minutes as the page prints them (`dashboard.app.fmt_min`; a test pins them equal)."""
    if value == 0:
        return "0 min"
    if value < 10:
        return f"{value:.1f} min"
    return f"{value:,.0f} min"


def _names(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _key(line: report.SourceLine) -> tuple:
    return ("id", line.source_id) if line.source_id is not None else ("name", line.display_name)


def _lever(line: report.SourceLine) -> str:
    """The control that actually exists for the surface this was on, as a sentence.

    Only levers that are real for this setup: the queue takes podcasts and
    YouTube; Cold Turkey is on this Mac; Screen Time limits apps on the phone,
    but not individual sites inside Brave.
    """
    if line.group == T.GROUP_X:
        return ("It is the same source on the phone and the Mac, so it takes both: a Screen Time "
                "limit on the app and a Cold Turkey block on x.com.")
    if line.group in (T.GROUP_YOUTUBE, T.GROUP_PODCASTS):
        return ("Capturing it to the queue instead of opening it is the lever — the lock is what "
                "turns a reflex into a choice.")
    if T.PROV_BROWSER_SYNC in line.provenances:
        return ("It was in the phone browser, where per-site limits don't reach, so a Screen Time "
                "limit on Brave itself is the lever.")
    if line.group == T.GROUP_IPHONE:
        return "A Screen Time app limit on the phone is the direct lever."
    if line.group == T.GROUP_DESKTOP:
        return "A Cold Turkey block on this Mac takes the reflex out of it."
    if line.group == T.GROUP_DESKTOP_APPS:
        return "Blocking the app on this Mac for part of the day takes the reflex out of it."
    return ""


def _week_described(clk: Clock, settings: Settings) -> T.MeasurementWeek:
    """The latest finished week whose evidence has been asked for.

    Since DR5.24 that is the week just finished from the Sunday after it, the
    morning the check-in asks for it; until its Screen Time and YouTube arrive,
    the comparison sets them aside like for like. The fallback to the week
    before covers only a week that ended before go-live, which is never asked
    for (DR2.2).
    """
    week = report._last_complete_measurement_week(clk, settings)
    if not report.week_is_due(clk, settings, week):
        week = T.measurement_week(week.sunday - timedelta(days=7))
    return week


def _load(conn, clk, settings, mweek: T.MeasurementWeek, splits: dict) -> _Week:
    start = T.allowance_week_start_for_day(mweek.sunday, settings.tz)
    totals = report.weekly_totals(conn, clk, settings, start)
    channels = {T.PROV_API}            # podcasts are polled continuously, never "missing"
    for t in totals:
        if t.import_status in (T.IMPORT_OK, T.IMPORT_PARTIAL):
            channels.add(t.provenance)
    imp = report._covering_import(conn, settings, "screentime", mweek)
    meta = json.loads(imp["meta"] or "{}") if imp is not None else {}
    lines = {_key(line): line
             for line in report.top_sources(conn, clk, settings, start, limit=None)}
    return _Week(start, allowance.pooled_minutes(conn, settings, start), lines,
                 frozenset(channels), bool(meta.get("most_used_complete")), splits.get(start))


def _name(conn, key: tuple, *weeks: _Week) -> str:
    for w in weeks:
        if key in w.lines:
            return w.lines[key].display_name
    if key[0] == "id":
        row = conn.execute("SELECT display_name FROM sources WHERE id = ?", (key[1],)).fetchone()
        if row is not None:
            return row["display_name"]
    return str(key[1])


# --- the comparison -----------------------------------------------------------

@dataclass(frozen=True)
class _Pair:
    before: float
    after: float
    per_source: dict                   # key -> (before, after), comparable sources only
    set_aside: tuple[str, ...]
    channels: tuple[str, ...]
    unreadable: int
    below_cut: int


def _compare(conn, now: _Week, prev: _Week) -> _Pair:
    shared = now.channels & prev.channels
    notes: list[str] = []

    carried = set(now.pooled.charged_by_provenance) | set(prev.pooled.charged_by_provenance)
    channels: list[str] = []
    for prov in sorted((now.channels ^ prev.channels) & carried):
        which = "the week before" if prov in prev.channels else "this week"
        channels.append(CHANNEL_NAMES.get(prov, prov))
        notes.append(f"{CHANNEL_NAMES.get(prov, prov)} — only {which} had it")

    keys = (set(now.pooled.charged_by_source) | set(prev.pooled.charged_by_source)
            | {k for k, _ in now.pooled.unmeasured_sources}
            | {k for k, _ in prev.pooled.unmeasured_sources})
    per_source: dict = {}
    unreadable: list[str] = []
    below_cut = 0
    for key in sorted(keys, key=repr):
        touched = (set(now.pooled.charged_by_source.get(key, {}))
                   | set(prev.pooled.charged_by_source.get(key, {}))
                   | {p for k, p in now.pooled.unmeasured_sources if k == key}
                   | {p for k, p in prev.pooled.unmeasured_sources if k == key}) & shared
        if not touched:
            continue
        a = [prev.known(key, p) for p in touched]
        b = [now.known(key, p) for p in touched]
        if None in a or None in b:
            bad = [p for p in touched
                   if (key, p) in prev.pooled.unmeasured_sources
                   or (key, p) in now.pooled.unmeasured_sources]
            if bad:
                when = ("the week before" if any((key, p) in prev.pooled.unmeasured_sources
                                                 for p in bad) else "this week")
                unreadable.append(f"{_name(conn, key, now, prev)} — couldn't be read {when}")
            else:
                below_cut += 1
            continue
        per_source[key] = (sum(a), sum(b))

    notes.extend(unreadable)
    if below_cut:
        notes.append(f"{below_cut} phone app{'s' if below_cut != 1 else ''} below the end of a "
                     "cut-off Screen Time list")
    before = sum(a for a, _ in per_source.values())
    after = sum(b for _, b in per_source.values())
    return _Pair(before, after, per_source, tuple(notes), tuple(channels), len(unreadable),
                 below_cut)


def _verdict(before: float, after: float, caveated: bool) -> tuple[str, str]:
    lfl = ", like for like" if caveated else ""
    diff = after - before
    if before <= 0:
        if after <= 0:
            return "level", "Nothing was counted in either week."
        return "up", f"Up from nothing to {_m(after)} on the week before{lfl}."
    pct = diff / before * 100
    if abs(pct) < LEVEL_PCT or abs(diff) < LEVEL_MIN:
        return "level", f"Level with the week before{lfl}: {_m(before)} then {_m(after)}."
    word = "Down" if diff < 0 else "Up"
    return ("down" if diff < 0 else "up",
            f"{word} {abs(pct):.0f}% on the week before{lfl}: {_m(before)} to {_m(after)}.")


# --- wins ---------------------------------------------------------------------

def _reflex(conn, clk, settings, week: _Week) -> tuple[float, float]:
    rows = report.gated_vs_ungated(conn, clk, settings, week.start)
    return week.pooled.wasteful_min, sum(r.wasteful_ungated_min for r in rows)


def _wins(conn, clk, settings, now: _Week, prev: _Week, pair: _Pair) -> list[Finding]:
    wins: list[Finding] = []

    # The allowance is charged on everything measured, not only on what both
    # weeks share. Less over is only robust when the later week measured at
    # least as much and holds no undercount of its own.
    if now.channels >= prev.channels and now.fully_known and now.split and prev.split:
        before, after = prev.split.overage_min, now.split.overage_min
        if before > 0 and after == 0:
            wins.append(Finding("allowance", f"You finished inside the {now.split.allowance_min} "
                                             f"min allowance, after {before} min over the week "
                                             "before."))
        elif before > 0 and before - after >= LEVEL_MIN:
            wins.append(Finding("allowance", f"Over the allowance by {after} min, down from "
                                             f"{before} min the week before."))

    drops = []
    for key, (a, b) in pair.per_source.items():
        if a - b >= SOURCE_DROP_MIN and b <= a * (1 - SOURCE_DROP_SHARE):
            drops.append((a - b, key, a, b))
    drops.sort(key=lambda d: (-d[0], _name(conn, d[1], now, prev)))
    for _, key, a, b in drops[:2]:
        to = "nothing" if b == 0 else _m(b)
        wins.append(Finding("source_down", f"{_name(conn, key, now, prev)} fell from {_m(a)} "
                                           f"to {to}.", key[1] if key[0] == "id" else None))

    pw, pr = _reflex(conn, clk, settings, prev)
    nw, nr = _reflex(conn, clk, settings, now)
    if pw >= REFLEX_FLOOR_MIN and nw >= REFLEX_FLOOR_MIN:
        before, after = pr / pw * 100, nr / nw * 100
        if before - after >= REFLEX_POINTS:
            wins.append(Finding("reflex_down", f"Less of it was reflex: {before:.0f}% of wasteful "
                                               f"time was opened without queueing first, down to "
                                               f"{after:.0f}%."))
    return wins[:MAX_WINS]


# --- tips ---------------------------------------------------------------------

def _tips(conn, now: _Week, prev: _Week | None, pair: _Pair | None) -> list[Finding]:
    tips: list[Finding] = []
    named: set[tuple] = set()
    wasteful = sorted((line for line in now.lines.values() if line.label == T.WASTEFUL),
                      key=lambda line: (-line.minutes, line.display_name))

    # 1. The biggest thing he called wasteful, and the lever for where it was.
    if wasteful and wasteful[0].minutes >= TIP_SOURCE_MIN:
        top = wasteful[0]
        reflex = ", all of it opened on reflex" if top.all_reflex else ""
        tips.append(Finding("lever", f"{top.display_name} was your biggest source of brain rot: "
                                     f"{_m(top.minutes)}{reflex}. {_lever(top)}", top.source_id))
        named.add(_key(top))

    # 2. What grew most — only where both weeks hold a real value.
    if pair is not None:
        rises = [(b - a, key, a, b) for key, (a, b) in pair.per_source.items()
                 if key not in named and b - a >= TIP_RISE_MIN]
        rises.sort(key=lambda r: (-r[0], _name(conn, r[1], now, prev)))
        if rises:
            _, key, a, b = rises[0]
            start = "from nothing" if a == 0 else f"from {_m(a)}"
            lever = _lever(now.lines[key]) if key in now.lines else ""
            tips.append(Finding("riser", f"{_name(conn, key, now, prev)} rose {start} to "
                                         f"{_m(b)}. {lever}".rstrip(),
                                key[1] if key[0] == "id" else None))
            named.add(key)

    # 3. When a few sources are most of it, cutting those is the plan.
    total_wasteful = sum(line.minutes for line in wasteful)
    over = now.split.overage_min if now.split is not None else 0
    top3 = wasteful[:3]
    if over > 0 and len(top3) == 3 and total_wasteful > 0:
        share = sum(line.minutes for line in top3) / total_wasteful
        if share >= CONCENTRATION_SHARE:
            half = sum(line.minutes for line in top3) / 2
            if half >= over and now.fully_known:
                effect = "brought the week inside the allowance"
            else:
                effect = f"taken {_m(half)} off a week that was {over} min over"
            tips.append(Finding("concentration",
                                f"{_names([line.display_name for line in top3])} were "
                                f"{share * 100:.0f}% of your wasteful time. Halving just those "
                                f"would have {effect}."))

    # 4. Undecided time blurs every figure above; deciding the big ones sharpens them.
    undecided = sorted((line for line in now.lines.values() if line.label is None),
                       key=lambda line: (-line.minutes, line.display_name))
    charged = now.pooled.charged_min
    if (now.pooled.unclassified_min >= UNDECIDED_MIN and charged > 0
            and now.pooled.unclassified_min / charged >= UNDECIDED_SHARE and undecided):
        biggest = undecided[:3]
        cover = sum(line.minutes for line in biggest) / now.pooled.unclassified_min * 100
        tips.append(Finding("undecided",
                            f"{_m(now.pooled.unclassified_min)} of the week's {_m(charged)} "
                            f"counted is still undecided, and "
                            f"{_names([line.display_name for line in biggest])} are {cover:.0f}% "
                            "of it. Deciding those makes next week's comparison mean more."))
    return tips[:MAX_TIPS]


# --- the whole thing ----------------------------------------------------------

def progress(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> Progress | None:
    """What improved in the latest finished week, and what to try next. Read-only.

    None before any week of the diagnostic has finished and been asked for.
    """
    floor = report.window_start(conn, clk, settings)
    if floor is None:
        return None
    week = _week_described(clk, settings)
    if week.end < floor:
        return None
    splits = {s.week_start: s for s in report.label_split(conn, clk, settings)}
    now = _load(conn, clk, settings, week, splits)

    prev_week = T.measurement_week(week.sunday - timedelta(days=7))
    if prev_week.end < floor:
        return Progress(now.start, week.start, week.end, None, None,
                        "This is the first week measured, so there is no week before it to "
                        "compare with yet.", None, now.pooled.charged_min, (), (),
                        tuple(_tips(conn, now, None, None)))

    prev = _load(conn, clk, settings, prev_week, splits)
    pair = _compare(conn, now, prev)
    direction, verdict = _verdict(pair.before, pair.after, bool(pair.set_aside))
    return Progress(now.start, week.start, week.end, prev.start, direction, verdict,
                    pair.before, pair.after, pair.set_aside,
                    tuple(_wins(conn, clk, settings, now, prev, pair)),
                    tuple(_tips(conn, now, prev, pair)),
                    pair.channels, pair.unreadable, pair.below_cut)


# --- the brain against your peak week (DR5.20) -----------------------------------
# Additive: it calls _week_described, _load and _compare and changes none of them,
# so every step it chains is exactly a pair the progress panel would show.

@dataclass(frozen=True)
class Link:
    """One finished week against the week before it, exactly as the progress panel compares them."""

    week_start: str                # allowance week start of the later week
    sunday: str                    # its measurement week, ISO dates
    saturday: str
    before_min: float              # the earlier week, on what both weeks measured
    after_min: float               # this week, on the same basis
    undecided_after_min: float     # of after_min: sources with no label yet
    channels_set_aside: tuple[str, ...]  # whole channels only one of the two weeks measured
    sources_set_aside: int         # sources left out: unreadable, or below a cut-off list
    unreadable: int                # of those, readings that couldn't be read

    @property
    def direction(self) -> str:
        """`_verdict`'s own rule, so a step reads exactly as the panel would read it."""
        diff = self.after_min - self.before_min
        if self.before_min <= 0:
            return "level" if self.after_min <= 0 else "up"
        if abs(diff / self.before_min * 100) < LEVEL_PCT or abs(diff) < LEVEL_MIN:
            return "level"
        return "down" if diff < 0 else "up"

    @property
    def change(self) -> float | None:
        """|after / before - 1|, or None when there was nothing before."""
        return abs(self.after_min / self.before_min - 1) if self.before_min > 0 else None


@dataclass(frozen=True)
class PeakChain:
    """The described week against your peak week, one like-for-like week at a time."""

    state: str                     # "drawn" | "first_week" | "nothing_before" | "not_comparable"
    week_start: str                # == Progress.week_start
    week_sunday: str
    week_saturday: str
    ratio: float | None            # I(shown) / highest I before it. Above 1 is a new peak.
    peak_start: str | None         # allowance week start of the peak week (the previous one, on a new peak)
    peak_sunday: str | None
    peak_saturday: str | None
    steps: tuple[Link, ...]        # peak week -> shown week, oldest first; the last is the panel's pair
    segment: tuple[str, ...]       # allowance week starts the ratio was chained across
    since_sunday: str | None       # set when the chain restarts after the first finished week
    unreadable: int = 0            # unreadable readings on any link that decides the ratio,
                                   # including the one that cut the run: settling one can move it
    from_sunday: str | None = None # first measurement week those links touch
    cut: str | None = None         # why weeks before since_sunday are left out:
                                   # "thin" (too little measured the same way) | "zero"


def chain_index(pairs: Sequence[tuple[float, float]]) -> tuple[int, int, float] | None:
    """Where the last of len(pairs)+1 weeks stands against the highest week before it.

    `pairs[i]` is (before, after) comparing week i+1 with week i, like for like,
    oldest first. Returns (segment_start, peak, ratio): the first week of the
    unbroken run of comparisons ending at the last week, the earliest week in
    that run with the highest chained index, and the last week's index over it.
    A link with nothing comparable before it (before == 0) cannot be crossed,
    nor can a week whose chained index fell to zero carry it on: either bounds
    the run instead of blanking it. None when the last week has no comparable
    week behind it at all.
    """
    n = len(pairs)
    start = n
    while start > 0:
        before, after = pairs[start - 1]
        # A link is crossed only with something before it to scale by, and a
        # week whose index fell to zero cannot carry the chain further back.
        if before <= 0 or (start < n and after <= 0):
            break
        start -= 1
    if start == n:
        return None
    index = [1.0]
    for before, after in pairs[start:]:
        index.append(index[-1] * after / before)
    earlier = index[:-1]
    top = max(earlier)                       # >= 1: the run's first week is 1
    # Float noise must not break a tie (7/25 * 25/7 is 1.0000000000000002): the
    # earliest of equal weeks is the peak, and a week equal to it is level.
    peak = next(i for i, x in enumerate(earlier) if math.isclose(x, top, rel_tol=1e-9))
    ratio = index[-1] / top
    return start, start + peak, 1.0 if math.isclose(ratio, 1.0, rel_tol=1e-9) else ratio


#: What both weeks measured must hold at least this share of EACH week's brain
#: rot for the pair to be chained. The progress panel shows a thin pair once;
#: chained, a 7-minute overlap would multiply every later week by after/before
#: and could set a peak that stands for the rest of the diagnostic. Today's
#: real pairs hold 36-91% of their weeks.
MIN_LINK_SHARE = 0.25


def peak_chain(conn, clk, settings) -> PeakChain | None:
    """The described week against your peak week, like for like. Read-only.

    None exactly when `progress()` is None. Every link is `_compare` on two
    adjacent finished weeks, so the last link is the progress panel's own pair
    and each step moves with it.
    """
    floor = report.window_start(conn, clk, settings)
    if floor is None:
        return None
    shown = _week_described(clk, settings)
    if shown.end < floor:
        return None
    weeks = [shown]
    while True:
        earlier = T.measurement_week(weeks[0].sunday - timedelta(days=7))
        if earlier.end < floor:
            break
        weeks.insert(0, earlier)
    splits = {s.week_start: s for s in report.label_split(conn, clk, settings)}
    loaded = [_load(conn, clk, settings, w, splits) for w in weeks]
    links: list[Link] = []
    for i in range(1, len(weeks)):
        now, prev = loaded[i], loaded[i - 1]
        pair = _compare(conn, now, prev)
        undecided = sum(after for key, (_, after) in pair.per_source.items()
                        if key in now.lines and now.lines[key].label is None)
        links.append(Link(now.start, weeks[i].start, weeks[i].end, pair.before, pair.after,
                          undecided, pair.channels, pair.unreadable + pair.below_cut,
                          pair.unreadable))
    # A thin pair is cut exactly like one with nothing before it (before == 0).
    thin = [l.before_min < MIN_LINK_SHARE * loaded[i].pooled.charged_min
            or l.after_min < MIN_LINK_SHARE * loaded[i + 1].pooled.charged_min
            for i, l in enumerate(links)]
    base = dict(week_start=loaded[-1].start, week_sunday=shown.start, week_saturday=shown.end)
    placed = chain_index([(0.0 if t else l.before_min, l.after_min) for l, t in zip(links, thin)])
    if placed is None:
        # Not thin, so before == 0 on a real overlap: the week before had no brain
        # rot at all (a share of nothing is never thin), which is not a mismatch.
        state = ("first_week" if not links else "not_comparable" if thin[-1]
                 else "nothing_before")
        return PeakChain(state, ratio=None, peak_start=None, peak_sunday=None, peak_saturday=None,
                         steps=tuple(links[-1:]), segment=(), since_sunday=None,
                         unreadable=sum(l.unreadable for l in links[-1:]),
                         from_sunday=weeks[-2].start if links else None, **base)
    start, peak, ratio = placed
    return PeakChain("drawn", ratio=ratio, peak_start=loaded[peak].start,
                     peak_sunday=weeks[peak].start, peak_saturday=weeks[peak].end,
                     steps=tuple(links[peak:]),
                     segment=tuple(w.start for w in loaded[start:]),
                     since_sunday=weeks[start].start if start > 0 else None,
                     unreadable=sum(l.unreadable for l in links[max(start - 1, 0):]),
                     from_sunday=weeks[max(start - 1, 0)].start,
                     cut=("thin" if thin[start - 1] else "zero") if start > 0 else None, **base)
