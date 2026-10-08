"""pace.py — this week so far, against the same point last week (DECISIONS DR5.29).

The weekly comparison (`progress.py`) only speaks once a week is finished and
its evidence is in. Jacob wanted to watch the week as it goes: "so I can see
the progress over the course of the week". This is that, under the same rule
as the weekly one — compare only what was measured the same way in both spans.

**Only the sources that report every day can be compared part-way through a
week.** Cold Turkey (the Mac's browser) and Qbserve (Mac apps) read every
finished day on their own, and podcast listening is polled. Screen Time and
the phone browser arrive as one figure per week from Monday's screenshots, and
YouTube only when the extension is run; last week's Screen Time cannot be cut
at Tuesday, and this week's does not exist yet. So the pace figure says which
sources it counts and which join when the week is in, and never pretends the
part-week is the whole week.

The spans are finished days only — Sunday to yesterday this week, against the
same weekdays last week — because the readers never read today: a half-day
would look like a whole one. On a Sunday nothing this week has finished, so
there is no pace yet.

A channel is compared only when an import covered every day of *both* spans;
one that missed a day is left out and named, rather than read as zero.
Residual, stated rather than hidden: a day Qbserve was quit part-way through
still counts as covered, because its read of that day succeeded — the keeper
(DR5.27) is what stops that happening.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

import allowance
import drbr_types as T
import progress
import report
from clock import Clock
from config import Settings

#: Channels whose figures are per day, and the import kind that proves a day
#: was read. Podcast listening is polled continuously, so it has none. YouTube
#: history is recorded per watch day but only arrives when the extension is
#: run, so it counts whenever a sync has happened since the span ended.
DAILY_CHANNELS: dict[str, str | None] = {
    T.PROV_EXPORT: "coldturkey",
    T.PROV_APP_TRACKER: "qbserve",
    T.PROV_API: None,
    T.PROV_PAGE: "youtube_history",
}

#: Weekly channels: one figure per week, so they cannot be cut part-way.
WEEKLY_CHANNELS = (T.PROV_SCREENSHOT, T.PROV_BROWSER_SYNC)


@dataclass(frozen=True)
class Pace:
    """Brain-rot minutes so far this week against the same days last week."""

    first_day: str                 # this week's Sunday, local date
    last_day: str                  # yesterday: the last finished day counted
    days: int                      # finished days so far, 1..6
    this_week_min: float
    last_week_min: float           # the same weekdays of the week before
    direction: str                 # "down" | "up" | "level"
    text: str                      # one sentence, either direction
    counted: tuple[str, ...]       # channel names compared
    left_out: tuple[str, ...]      # daily channels set aside, each with its reason
    joins_later: tuple[str, ...]   # weekly channels, added once the week is in

    @property
    def basis(self) -> str:
        """The second line: what the figure counts, and what it does not yet."""
        parts = [f"Counted on {progress._names(list(self.counted))}."]
        if self.left_out:
            parts.append(f"Left out: {'; '.join(self.left_out)}.")
        if self.joins_later:
            parts.append(f"{progress._names(list(self.joins_later))} join once the week is in.")
        return " ".join(parts)


def _span_read(conn: sqlite3.Connection, settings: Settings, prov: str,
               span: list[date]) -> bool:
    """Was this channel read for every day of the span?

    Daily readers must have covered each day. YouTube history is walked back
    to the last cursor on every sync, so any sync received after the span's
    last day covers all of it — the same rule `report._covering_import` uses.
    """
    kind = DAILY_CHANNELS[prov]
    if kind is None:
        return True
    if prov == T.PROV_PAGE:
        after = report._local_midnight(span[-1] + timedelta(days=1), settings.tz)
        return conn.execute(
            "SELECT 1 FROM imports WHERE kind = ? AND status IN ('ok','partial') "
            "AND received_at >= ? LIMIT 1", (kind, after)).fetchone() is not None
    return all(_day_read(conn, kind, d) for d in span)


def _day_read(conn: sqlite3.Connection, kind: str, day: date) -> bool:
    """Did an import of this kind read the day? Absence of a read is not a zero."""
    iso = T.iso_date(day)
    return conn.execute(
        "SELECT 1 FROM imports WHERE kind = ? AND status IN ('ok','partial') "
        "AND period_start <= ? AND period_end >= ? LIMIT 1",
        (kind, iso, iso),
    ).fetchone() is not None


def _day_charged(conn: sqlite3.Connection, settings: Settings, day: date) -> tuple[dict, set]:
    """({provenance: brain-rot minutes}, {provenance with an unmeasured row}) for one day.

    The same rules `allowance.pooled_minutes` applies — covered-elsewhere and
    tool excluded, undecided counted as charged, pre-go-live ignored — read per
    local day instead of per allowance week. A test pins the two together.
    """
    iso = T.iso_date(day)
    charged: dict[str, float] = {}
    unknown: set[str] = set()
    go_live = getattr(settings, "go_live", "") or ""
    rows = conn.execute(
        """
        SELECT m.*, s.kind AS kind, s.source_key AS source_key, s.display_name AS display_name
          FROM measurements m JOIN sources s ON s.id = m.source_id
         WHERE m.provenance IN (?, ?, ?) AND m.period_start = ? AND m.period_end = ?
        """,
        (T.PROV_EXPORT, T.PROV_APP_TRACKER, T.PROV_PAGE, iso, iso),
    ).fetchall()
    for row in rows:
        if go_live and row["period_end"] < go_live:
            continue
        # The rule, not the dict: Qbserve's website pseudo-apps are covered too
        # (DR5.23), and missing them double-counted 95 min of YouTube on 6 Oct.
        if T.covered_elsewhere(row["kind"], row["source_key"]):
            continue
        if row["status"] != "ok":
            unknown.add(row["provenance"])
            continue
        field = allowance._field(allowance.measurement_label(conn, row))
        if field in ("wasteful_min", "unclassified_min"):
            charged[row["provenance"]] = charged.get(row["provenance"], 0.0) + float(row["minutes"])

    start = report._local_midnight(day, settings.tz)
    end = report._local_midnight(day + timedelta(days=1), settings.tz)
    for row in conn.execute(
        """
        SELECT l.spotify_id AS spotify_id, l.polled_at AS polled_at, l.delta_ms AS delta_ms,
               e.show AS show, e.platform AS platform, e.source_key AS source_key
          FROM listening l JOIN episodes e ON e.spotify_id = l.spotify_id
         WHERE l.polled_at >= ? AND l.polled_at < ? AND l.delta_ms > 0 AND e.platform = 'spotify'
        """,
        (start, end),
    ):
        field = allowance._field(allowance.spotify_label(conn, row))
        if field in ("wasteful_min", "unclassified_min"):
            charged[T.PROV_API] = charged.get(T.PROV_API, 0.0) + int(row["delta_ms"]) / allowance.MIN_MS
    return charged, unknown


def _text(this_week: float, last_week: float, first: date, last: date) -> tuple[str, str]:
    span = first.strftime("%a") if first == last else f"{first.strftime('%a')}–{last.strftime('%a')}"
    by = f"by {last.strftime('%A')} last week"
    head = f"So far this week ({span}): {progress._m(this_week)}"
    if last_week <= 0:
        return ("up" if this_week > 0 else "level",
                f"{head}, against nothing {by}." if this_week > 0
                else f"{head}, the same as {by}.")
    diff = this_week - last_week
    pct = diff / last_week * 100
    if abs(pct) < progress.LEVEL_PCT or abs(diff) < progress.LEVEL_MIN:
        return "level", f"{head}, level with {progress._m(last_week)} {by}."
    word = "down" if diff < 0 else "up"
    return word, f"{head}, against {progress._m(last_week)} {by} — {word} {abs(pct):.0f}%."


def pace(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> Pace | None:
    """This week's finished days against the same weekdays last week. Read-only.

    None on a Sunday (no day of the week has finished yet), before go-live, or
    when last week's matching days are before the diagnostic began.
    """
    today = T.local_date(clk.now(), settings.tz)
    sunday = T.week_sunday(today)
    days = (today - sunday).days
    if days <= 0:
        return None
    this_span = [sunday + timedelta(days=i) for i in range(days)]
    last_span = [d - timedelta(days=7) for d in this_span]
    floor = report.window_start(conn, clk, settings)
    if floor is None or T.iso_date(last_span[0]) < floor:
        return None

    per_day = {d: _day_charged(conn, settings, d) for d in this_span + last_span}
    counted: list[str] = []
    left_out: list[str] = []
    this_total = last_total = 0.0
    for prov in DAILY_CHANNELS:
        name = progress.CHANNEL_NAMES[prov]
        if not (_span_read(conn, settings, prov, this_span)
                and _span_read(conn, settings, prov, last_span)):
            reason = ("not synced since the last day counted — run the extension to include it"
                      if prov == T.PROV_PAGE else "a day wasn't read")
            left_out.append(f"{name}, {reason}")
            continue
        if any(prov in per_day[d][1] for d in this_span + last_span):
            left_out.append(f"{name}, a reading couldn't be measured")
            continue
        counted.append(name)
        this_total += sum(per_day[d][0].get(prov, 0.0) for d in this_span)
        last_total += sum(per_day[d][0].get(prov, 0.0) for d in last_span)
    if not counted:
        return None

    direction, text = _text(this_total, last_total, this_span[0], this_span[-1])
    return Pace(T.iso_date(this_span[0]), T.iso_date(this_span[-1]), days, this_total, last_total,
                direction, text, tuple(counted), tuple(left_out),
                tuple(progress.CHANNEL_NAMES[p] for p in WEEKLY_CHANNELS))
