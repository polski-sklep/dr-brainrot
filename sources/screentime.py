"""iPhone Screen Time: weekly screenshots -> per-app minutes (CONTRACTS Part II §6.2).

There is no programmatic route to Screen Time data (brief §3), so the user
sends the scrolled per-device week view as screenshots and Apple Vision reads
them on the Mac (DR0.4). OCR is the least trustworthy provenance in DRBR, so
this module is built to under-claim:

* **Only a complete past week writes minutes** (DR1.13). "This Week", an
  all-devices view, mixed weeks or a missing week header are stored as
  evidence with a warning saying what to resend, and write nothing.
* **Counts are never minutes.** Pickups, First Used After Pickup and
  Notifications show bare integers; `parse_duration` only accepts values with
  a unit, and rows are only taken from the Most Used section.
* **Nothing is invented.** A name without a duration, a duration without a
  name, or the same app read with two different durations is a warning, and
  the app (if named) is stored as `unreadable` with NULL minutes.
* **The raw images are kept** (manifest + images via `evidence.store_raw`) so a
  better parser can `reparse` them without asking the user again.

Layout facts below were measured on the real screenshots' Vision output
(NOTES-B.md), not assumed: an app row is two observations, the name at
x≈0.18–0.20 and its duration one line (≈0.026) lower; section headers sit at
x≈0.08; a sticky header (device name, week label) covers the top ≈0.17 of every
scrolled page, and rows sliding under it are blurred and partly read.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import clock
import drbr_types as T
import evidence
import labels
from clock import Clock
from config import Settings
from sources.ocr_vision import TextObservation

PARSER_VERSION = "screentime-1"
KIND = "screentime"


class ScreenTimeParseError(evidence.ParseError):
    """The images are not a readable Screen Time capture at all."""


# --- geometry (normalised, top-left origin; measured on the real captures) ---

#: Everything above this line is the sticky header: device name, week label and
#: the blurred rows scrolling underneath them. Rows are never read from here.
HEADER_ZONE = 0.175
#: Section headers ("Most Used", "Pickups", ...) and "Show More" start here.
SECTION_X_MAX = 0.15
#: App names start at x≈0.18–0.20; app icons (which OCR as stray letters) sit left of 0.15.
NAME_X = (0.15, 0.32)
#: Durations move right with the usage bar; chevrons/hourglasses sit at x≥0.85.
VALUE_X = (0.15, 0.85)
#: A duration sits one text line below its name.
PAIR_DY = (0.010, 0.045)
#: Summary: category names sit this far above "Total Screen Time".
CATEGORY_WINDOW = (0.03, 0.10)

# Section identities.
SUMMARY, LIMITS, MOST_USED, PICKUPS, FIRST_USED, NOTIFICATIONS = (
    "summary", "limits", "most_used", "pickups", "first_used", "notifications")
COUNTS, OTHER, MIXED, UNKNOWN = "counts", "other", "mixed", "unknown"

_SECTION_HEADERS = {
    "screen time": SUMMARY,
    "limits": LIMITS,
    "most used": MOST_USED,
    "pickups": PICKUPS,
    "first used after pickup": FIRST_USED,
    "notifications": NOTIFICATIONS,
}
_SHOW_MORE = "show more"

#: UI text that is never an app name, and whose presence marks a segment as
#: summary/count content rather than an app list.
_UI_TEXT = {
    "daily average", "total screen time", "most pickups", "total pickups", "show more",
    "show categories", "show apps", "week", "day", "this week", "last week", "screen time",
    "limits", "most used", "pickups", "first used after pickup", "notifications",
}
_UI_PATTERNS = (re.compile(r"from last week", re.I), re.compile(r"^updated\b", re.I))

# --- small text helpers -------------------------------------------------------

_LEADING_JUNK = re.compile(r"^[^\w]+")
_DURATION_RE = re.compile(
    r"(?:(?P<h>\d{1,3})\s*(?:hrs|hr|h)(?![a-z])\s*)?"
    r"(?:(?P<m>\d{1,3})\s*(?:mins|min|m)(?![a-z])\s*)?"
    r"(?:(?P<s>\d{1,2})\s*(?:secs|sec|s)(?![a-z]))?"
)
_COUNT_RE = re.compile(r"\d{1,6}")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _LEADING_JUNK.sub("", text or "")).strip()


def _key(name: str) -> str:
    return _clean(name).casefold()


def parse_duration(text: str) -> float | None:
    """Screen Time duration text -> minutes, or None if it is not a duration.

    '1h 17m' -> 77, '23m' -> 23, '53s' -> 0.8833. A bare number is a count and
    returns None: counts must never become minutes.

    OCR confusions observed on the real captures are undone only where they
    cannot change which number is meant: a leading usage-bar bullet ('• 11m'),
    'I'/'l' read for the digit 1 directly before a unit or digit ('Im' -> 1m),
    an upper-case unit ('4S'), and a stray 'l' after a final unit ('17ml').
    """
    s = _LEADING_JUNK.sub("", text or "").strip().lower()
    if not s:
        return None
    s = re.sub(r"(?<=[hms])[l|]$", "", s)
    s = re.sub(r"(?<![a-z])[il|](?=\s*[\dhms](?![a-z]{2}))", "1", s)
    s = re.sub(r"(?<=\d)o|o(?=\d)", "0", s)
    m = _DURATION_RE.fullmatch(s)
    if not m or not any(m.group(g) for g in ("h", "m", "s")):
        return None
    h, mi, sec = (int(m.group(g)) if m.group(g) else None for g in ("h", "m", "s"))
    if h is not None and mi is not None and mi >= 60:
        return None
    if sec is not None and sec >= 60:
        return None
    return (h or 0) * 60 + (mi or 0) + (sec or 0) / 60


def _is_count(text: str) -> bool:
    return bool(_COUNT_RE.fullmatch(_LEADING_JUNK.sub("", text or "").strip()))


def _is_ui(text: str) -> bool:
    c = _key(text)
    return c in _UI_TEXT or any(p.search(text or "") for p in _UI_PATTERNS)


def _cx(o: TextObservation) -> float:
    return o.x + o.w / 2


# --- week labels ----------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH = r"[A-Za-z]{3,9}"
_RANGE_MD = re.compile(
    rf"(?P<m1>{_MONTH})\.?\s*(?P<d1>\d{{1,2}})\s*[-–—]\s*(?:(?P<m2>{_MONTH})\.?\s*)?(?P<d2>\d{{1,2}})(?:,?\s*(?P<y>\d{{4}}))?")
_RANGE_DM = re.compile(
    rf"(?P<d1>\d{{1,2}})(?:\s*(?P<m1>{_MONTH})\.?)?\s*[-–—]\s*(?P<d2>\d{{1,2}})\s*(?P<m2>{_MONTH})\.?(?:,?\s*(?P<y>\d{{4}}))?")


def _month(token: str | None) -> int | None:
    if not token:
        return None
    return _MONTHS.get(token[:3].casefold())


def _week_label(text: str) -> str | None:
    """Normalised week label if this text is one, else None."""
    c = _clean(text).casefold()
    if c in ("this week", "last week"):
        return c
    if _RANGE_MD.fullmatch(_clean(text)) or _RANGE_DM.fullmatch(_clean(text)):
        return _clean(text)
    return None


def _resolve_range(label: str, received: date) -> tuple[date, date] | None:
    """An explicit 'Sep 6 – 12' style header -> (first, last) day, year inferred.

    The year is the one that puts the range at or before the receive date; a
    week header can only show the past or the present.
    """
    m = _RANGE_MD.fullmatch(label) or _RANGE_DM.fullmatch(label)
    if not m:
        return None
    m1, m2 = _month(m.group("m1")), _month(m.group("m2"))
    m1, m2 = m1 or m2, m2 or m1
    if m1 is None or m2 is None:
        return None
    d1, d2 = int(m.group("d1")), int(m.group("d2"))
    years = [int(m.group("y"))] if m.group("y") else [received.year, received.year - 1]
    for year in years:
        try:
            end = date(year, m2, d2)
            start = date(year if (m2, d2) >= (m1, d1) else year - 1, m1, d1)
        except ValueError:
            return None
        if m.group("y") or start <= received:
            return start, end
    return None


# --- segmentation -----------------------------------------------------------------

@dataclass
class _Segment:
    page: int
    section: str                 # declared header section, or inferred
    declared: bool
    top: float
    bottom: float
    terminator: str              # 'show_more' | 'header:<section>' | 'page_end'
    obs: list[TextObservation]
    header: TextObservation | None = None


def _segments(page_index: int, page: list[TextObservation]) -> list[_Segment]:
    """Split a page at section headers and 'Show More' into ordered segments.

    Pages have no reliable relation to the page before them: the user taps
    "Show More", which expands a list the previous screenshot showed closed. So
    a segment without a visible header is classified by what it contains, never
    by the previous page.
    """
    body = sorted((o for o in page if o.y >= HEADER_ZONE), key=lambda o: (o.y, o.x))
    markers: list[tuple[TextObservation, str]] = []
    for o in body:
        if o.x < SECTION_X_MAX:
            c = _key(o.text)
            if c in _SECTION_HEADERS:
                markers.append((o, "header:" + _SECTION_HEADERS[c]))
            elif c == _SHOW_MORE:
                markers.append((o, "show_more"))
    segs: list[_Segment] = []
    top, section, declared, header = HEADER_ZONE, None, False, None
    for marker, kind in markers + [(None, "page_end")]:
        bottom = marker.y if marker is not None else 1.01
        seg_obs = [o for o in body if top <= o.y < bottom and o is not header and o is not marker]
        segs.append(_Segment(page_index, section or "", declared, top, bottom, kind, seg_obs, header))
        if marker is not None:
            top = marker.y + 0.001
            if kind.startswith("header:"):
                section, declared, header = kind.split(":", 1)[1], True, marker
            else:
                section, declared, header = None, False, None
    for s in segs:
        if not s.declared:
            s.section = _infer(s)
    return [s for s in segs if s.obs or s.declared]


def _infer(seg: _Segment) -> str:
    """Classify a headerless segment by its values: durations, counts, or neither."""
    if any(_is_ui(o.text) for o in seg.obs):
        return OTHER
    durations = sum(1 for o in seg.obs if VALUE_X[0] <= o.x < VALUE_X[1] and parse_duration(o.text) is not None)
    counts = sum(1 for o in seg.obs if VALUE_X[0] <= o.x < VALUE_X[1] and _is_count(o.text))
    if durations and counts:
        return MIXED
    if durations:
        return MOST_USED
    if counts:
        return COUNTS
    return UNKNOWN


@dataclass
class _Row:
    page: int
    name: TextObservation | None
    value: TextObservation | None
    minutes: float | None


def _rows(seg: _Segment) -> list[_Row]:
    """Pair each app name with the duration one line below it."""
    names = sorted((o for o in seg.obs
                    if NAME_X[0] <= o.x < NAME_X[1] and _clean(o.text)
                    and parse_duration(o.text) is None and not _is_count(o.text) and not _is_ui(o.text)),
                   key=lambda o: o.y)
    values = [o for o in seg.obs if VALUE_X[0] <= o.x < VALUE_X[1] and parse_duration(o.text) is not None]
    used: set[int] = set()
    rows: list[_Row] = []
    for i, n in enumerate(names):
        limit = names[i + 1].y if i + 1 < len(names) else 2.0
        best = None
        for j, v in enumerate(values):
            dy = v.y - n.y
            if j in used or not (PAIR_DY[0] < dy < PAIR_DY[1]) or v.y >= limit:
                continue
            if best is None or dy < values[best].y - n.y:
                best = j
        if best is None:
            rows.append(_Row(seg.page, n, None, None))
        else:
            used.add(best)
            rows.append(_Row(seg.page, n, values[best], parse_duration(values[best].text)))
    for j, v in enumerate(values):
        if j not in used:
            rows.append(_Row(seg.page, None, v, parse_duration(v.text)))
    return rows


def _unnamed_is_edge(seg: _Segment, value: TextObservation) -> bool:
    """A duration whose name would sit above the segment top (under the header or a heading)."""
    return value.y - PAIR_DY[1] < seg.top


def _categories_mode(pages: list[list[TextObservation]]) -> bool:
    """True if Most Used was switched to show categories instead of apps."""
    shows_apps = shows_categories = False
    for page in pages:
        for o in page:
            c = _key(o.text)
            if c == "show apps":
                shows_apps = True
            elif c == "show categories":
                shows_categories = True
    return shows_apps and not shows_categories


def find_unnamed_rows(pages: list[list[TextObservation]]) -> list[tuple[int, TextObservation]]:
    """Most Used durations with no readable name, where the name should be visible.

    The ingest re-reads just the name area of these rows (`recognize_region`):
    on the real captures Vision's full-page pass dropped a one-letter app name.
    """
    out = []
    for i, page in enumerate(pages):
        for seg in _segments(i, page):
            if seg.section != MOST_USED:
                continue
            for r in _rows(seg):
                if r.name is None and r.value is not None and not _unnamed_is_edge(seg, r.value):
                    out.append((i, r.value))
    return out


#: Crops tried when re-reading an unnamed row's name area, relative to the
#: duration's top y. Two crops that must agree: a single glyph read on one
#: crop and not the other is not trusted.
RECOVERY_CROPS = ((0.17, -0.040, 0.28, 0.038), (0.17, -0.035, 0.28, 0.030))


def recover_unnamed_rows(pages: list[list[TextObservation]], images: list[bytes],
                         region_ocr: Callable) -> tuple[list[list[TextObservation]], list[str]]:
    """Add names re-read from crops for unnamed Most Used rows. -> (pages, notes).

    A name is added only when every crop that returned a name-like text agrees
    on it. Anything else leaves the row unnamed, and the parse warns about it.
    """
    pages = [list(p) for p in pages]
    notes: list[str] = []
    for page_index, value in find_unnamed_rows(pages):
        reads: list[TextObservation] = []
        try:
            for x, dy, w, h in RECOVERY_CROPS:
                for o in region_ocr(images[page_index], x, value.y + dy, w, h):
                    if _clean(o.text) and parse_duration(o.text) is None and not _is_count(o.text):
                        reads.append(o)
        except Exception as exc:  # noqa: BLE001 — a failed re-read just leaves the row unnamed
            notes.append(f"re-reading a row on screenshot {page_index + 1} failed: {type(exc).__name__}")
            continue
        texts = {_key(o.text) for o in reads}
        if len(texts) == 1:
            best = reads[0]
            pages[page_index].append(TextObservation(_clean(best.text), best.confidence, best.x, best.y,
                                                     best.w, best.h, "region"))
    return pages, notes


# --- the parse --------------------------------------------------------------------------

@dataclass(frozen=True)
class AppRow:
    name: str
    minutes: float | None          # None when unreadable or conflicting
    status: str                    # 'ok' | 'unreadable'
    raw: tuple[str, ...]           # the OCR text of every reading of this row
    confidence: float | None       # lowest confidence among the readings used
    pages: tuple[int, ...]         # 1-based screenshot numbers
    recovered: bool = False        # name came from a region re-read


@dataclass
class ScreenTimeParse:
    device_name: str | None
    week_label: str | None
    period_start: str | None
    period_end: str | None
    apps: list[tuple[str, float]]
    categories: list[tuple[str, float]]
    total_minutes: float | None
    warnings: list[str]
    is_full_past_week: bool
    app_rows: list[AppRow] = field(default_factory=list)
    daily_average_minutes: float | None = None
    meta: dict = field(default_factory=dict)


RESEND_HOWTO = ("On the iPhone open Settings → Screen Time → this iPhone (not All Devices), choose Week, "
                "tap ‹ once so the header reads \"Last Week\", then screenshot the whole page from the top "
                "(tap Show More under Most Used until it disappears) and send all the screenshots together.")


def parse_observations(pages: list[list[TextObservation]], *, received_at: str, tz: str) -> ScreenTimeParse:
    """Recorded OCR pages -> what the screenshots say, with every doubt as a warning.

    Pure: no database, no OCR. `received_at` (UTC ISO) anchors "Last Week".
    Raises ScreenTimeParseError only when the images show no Screen Time
    screen at all; everything short of that is a warning.
    """
    if not pages:
        raise ScreenTimeParseError("no screenshots to read")
    warnings: list[str] = []
    meta: dict = {"pages": len(pages)}
    received = T.local_date(received_at, tz)

    # Header: device title and week label (sticky, repeated on every page).
    titles: Counter[str] = Counter()
    week_labels: Counter[str] = Counter()
    all_devices = False
    for page in pages:
        for o in page:
            if o.y >= 0.2:
                continue
            label = _week_label(o.text)
            if label:
                week_labels[label] += 1
                continue
            if re.fullmatch(r"all devices", _clean(o.text), re.I):
                all_devices = True
            if 0.04 <= o.y <= 0.12 and abs(_cx(o) - 0.5) < 0.08 and not re.search(r"\d{1,2}:\d{2}", o.text):
                titles[_clean(o.text)] += 1
    device_name = titles.most_common(1)[0][0] if titles else None
    if len(titles) > 1:
        meta["device_name_readings"] = sorted(titles)

    segs = [s for i, p in enumerate(pages) for s in _segments(i, p)]
    has_markers = bool(week_labels or device_name or any(s.declared for s in segs)
                       or any(_key(o.text) == "total screen time" for p in pages for o in p))
    if not has_markers:
        raise ScreenTimeParseError(
            "these images don't look like Screen Time screenshots (no week header, device name or sections found)")

    # Which week?
    period: tuple[date, date] | None = None
    week_label = None
    week_ok = False
    if len(week_labels) > 1:
        warnings.append("The screenshots show different weeks (" + ", ".join(sorted(week_labels)) +
                        "). Nothing was recorded. " + RESEND_HOWTO)
    elif not week_labels:
        warnings.append("No week header (\"Last Week\") was readable, so the week is unknown. Nothing was "
                        "recorded. Scroll at least once so the header shows. " + RESEND_HOWTO)
    else:
        week_label = next(iter(week_labels))
        this_sunday = T.week_sunday(received)
        if week_label == "this week":
            warnings.append("These screenshots show This Week, which isn't over yet. Nothing was recorded. "
                            + RESEND_HOWTO)
        elif week_label == "last week":
            period = (this_sunday - timedelta(days=7), this_sunday - timedelta(days=1))
            week_ok = True
        else:
            rng = _resolve_range(week_label, received)
            if rng is None:
                warnings.append(f"The week header {week_label!r} could not be read as dates. Nothing was recorded. "
                                + RESEND_HOWTO)
            elif rng[0].weekday() != 6 or (rng[1] - rng[0]).days != 6:
                warnings.append(f"The header {week_label!r} is not a Sunday–Saturday week. Nothing was recorded. "
                                + RESEND_HOWTO)
            elif rng[1] >= this_sunday:
                warnings.append(f"The week {week_label!r} isn't over yet. Nothing was recorded. " + RESEND_HOWTO)
            else:
                period, week_ok = rng, True

    per_device = device_name is not None and not all_devices
    if all_devices:
        warnings.append("These screenshots show All Devices, which would count the Mac twice (Cold Turkey "
                        "measures it). Nothing was recorded. " + RESEND_HOWTO)
    elif device_name is None:
        warnings.append("No device name was readable at the top, so this can't be confirmed as the iPhone's own "
                        "view. Nothing was recorded. " + RESEND_HOWTO)

    # Summary: categories and total, anchored on "Total Screen Time".
    categories: dict[str, list[tuple[str, float, float]]] = {}
    totals: set[float] = set()
    daily_avgs: set[float] = set()
    saw_total = False
    for page in pages:
        for t in page:
            if _key(t.text) == "daily average":
                for v in page:
                    if 0.01 < v.y - t.y < 0.05 and abs(v.x - t.x) < 0.03 and parse_duration(v.text) is not None:
                        daily_avgs.add(round(parse_duration(v.text), 4))
            if _key(t.text) != "total screen time" or t.y < HEADER_ZONE:
                continue
            saw_total = True
            for v in page:
                if abs(v.y - t.y) < 0.012 and v.x > 0.5 and parse_duration(v.text) is not None:
                    totals.add(round(parse_duration(v.text), 4))
            window = [o for o in page if CATEGORY_WINDOW[0] < t.y - o.y < CATEGORY_WINDOW[1]]
            cat_names = [o for o in window if len(_clean(o.text)) >= 3 and parse_duration(o.text) is None
                         and not _is_count(o.text) and not _is_ui(o.text)]
            cat_values = [o for o in page if parse_duration(o.text) is not None]
            paired: set[int] = set()
            for n in cat_names:
                match = [v for v in cat_values if 0.01 < v.y - n.y < 0.04 and abs(v.x - n.x) < 0.03]
                if len(match) != 1:
                    warnings.append(f"The category {_clean(n.text)!r} has no readable total; it was not recorded.")
                    continue
                paired.add(id(match[0]))
                categories.setdefault(_clean(n.text), []).append(
                    (match[0].text, parse_duration(match[0].text), n.confidence))
            for v in cat_values:
                if CATEGORY_WINDOW[0] < t.y - v.y < CATEGORY_WINDOW[1] and id(v) not in paired and v.x < 0.95:
                    warnings.append(f"A category total {v.text!r} has no readable category name; it was not recorded.")
    if not saw_total:
        warnings.append("The summary at the top (category totals, Total Screen Time) wasn't in the screenshots, "
                        "so category totals are missing. Start the screenshots at the top of the page.")
    if len(totals) > 1:
        warnings.append("Total Screen Time was read with different values; the total was not recorded.")
    total_minutes = next(iter(totals)) if len(totals) == 1 else None
    category_list: list[tuple[str, float]] = []
    for name, readings in categories.items():
        values = {round(m, 4) for _, m, _ in readings}
        if len(values) > 1:
            warnings.append(f"The category {name!r} was read with different totals "
                            f"({', '.join(sorted({r for r, _, _ in readings}))}); it was not recorded.")
            continue
        category_list.append((name, readings[0][1]))

    # Most Used.
    apps_as_categories = _categories_mode(pages)
    readings: dict[str, list[_Row]] = {}
    display: dict[str, str] = {}
    unnamed = 0
    complete: bool | None = None
    saw_most_used = False
    for seg in segs:
        if seg.section == MIXED:
            warnings.append(f"Part of screenshot {seg.page + 1} mixes durations and counts and couldn't be "
                            "assigned to a section; it was ignored.")
            continue
        if seg.section != MOST_USED:
            if seg.section == UNKNOWN and seg.obs:
                meta["unclassified_segments"] = meta.get("unclassified_segments", 0) + 1
            continue
        saw_most_used = True
        if seg.terminator == "show_more":
            complete = False
        elif seg.terminator.startswith("header:"):
            complete = True
        if apps_as_categories:
            continue
        for r in _rows(seg):
            if r.name is None:
                if _unnamed_is_edge(seg, r.value):
                    meta["edge_rows"] = meta.get("edge_rows", 0) + 1
                else:
                    unnamed += 1
                    warnings.append(f"A Most Used row on screenshot {r.page + 1} showing {r.value.text!r} has no "
                                    "readable app name; its minutes were not recorded.")
                continue
            k = _key(r.name.text)
            display.setdefault(k, _clean(r.name.text))
            readings.setdefault(k, []).append(r)

    if apps_as_categories:
        warnings.append("Most Used is showing categories, not apps, so no app minutes were read. Tap "
                        "\"Show Apps\" under Most Used and resend.")
    elif not saw_most_used:
        warnings.append("No Most Used list was found, so no app minutes were read. " + RESEND_HOWTO)
    elif complete is False:
        warnings.append("The Most Used list ends in \"Show More\", so apps below it are missing. Tap Show More "
                        "until it disappears and resend.")
    elif complete is None:
        warnings.append("The end of the Most Used list wasn't in the screenshots, so apps below it may be "
                        "missing. Keep scrolling until the Pickups section shows.")

    app_rows: list[AppRow] = []
    for k, rows in readings.items():
        named = display[k]
        with_value = [r for r in rows if r.value is not None]
        raw = tuple(dict.fromkeys(f"{r.name.text} | {r.value.text}" if r.value else r.name.text for r in rows))
        pages_seen = tuple(sorted({r.page + 1 for r in rows}))
        recovered = any(r.name.origin == "region" for r in rows)
        if not with_value:
            warnings.append(f"The app {named!r} has no readable duration; it is stored as unreadable, not zero.")
            app_rows.append(AppRow(named, None, "unreadable", raw, None, pages_seen, recovered))
            continue
        values = {round(r.minutes, 4) for r in with_value}
        conf = min(min(r.name.confidence, r.value.confidence) for r in with_value)
        if len(values) > 1:
            shown = ", ".join(sorted({r.value.text for r in with_value}))
            warnings.append(f"The app {named!r} was read with different durations ({shown}); it is stored as "
                            "unreadable rather than guessed.")
            app_rows.append(AppRow(named, None, "unreadable", raw, conf, pages_seen, recovered))
            continue
        app_rows.append(AppRow(named, with_value[0].minutes, "ok", raw, conf, pages_seen, recovered))

    meta.update({
        "device_name": device_name, "week_label": week_label, "apps_read": len(app_rows),
        "apps_unreadable": sum(1 for a in app_rows if a.status != "ok"), "unnamed_rows": unnamed,
        "most_used_complete": complete, "total_minutes": total_minutes,
        "apps_sum_minutes": round(sum(a.minutes for a in app_rows if a.minutes is not None), 4),
        "recovered_names": sorted(a.name for a in app_rows if a.recovered),
    })
    daily_average = next(iter(daily_avgs)) if len(daily_avgs) == 1 else None
    full = bool(week_ok and per_device and period is not None)
    return ScreenTimeParse(
        device_name=device_name,
        week_label=week_label,
        period_start=T.iso_date(period[0]) if period else None,
        period_end=T.iso_date(period[1]) if period else None,
        apps=[(a.name, a.minutes) for a in app_rows if a.minutes is not None],
        categories=category_list,
        total_minutes=total_minutes,
        warnings=list(dict.fromkeys(warnings)),
        is_full_past_week=full,
        app_rows=app_rows,
        daily_average_minutes=daily_average,
        meta=meta,
    )


# --- ingest -------------------------------------------------------------------------------

def _ext(name: str) -> str:
    name = (name or "").strip().lower()
    if "." in name:
        name = name.rsplit(".", 1)[1]
    name = re.sub(r"[^a-z0-9]", "", name)
    return "." + (name or "img")


def _images_key(shas: list[str], received_at: str, tz: str) -> str:
    """Identity of 'these images, received in this week'.

    The manifest includes received_at, so resending identical screenshots a
    minute later gives a new manifest sha. What "Last Week" means only changes
    with the receive week, so that is what the duplicate check compares.
    """
    week = T.week_sunday(T.local_date(received_at, tz)).isoformat()
    return hashlib.sha256(("|".join(shas) + "|" + week).encode()).hexdigest()


def _find_duplicate(conn: sqlite3.Connection, manifest_sha: str, images_key: str) -> sqlite3.Row | None:
    row = evidence.find_import_by_sha(conn, KIND, manifest_sha)
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM imports WHERE kind = ? AND status != 'failed' AND json_extract(meta, '$.images_key') = ? "
        "ORDER BY id DESC LIMIT 1", (KIND, images_key)).fetchone()


def _default_ocr():
    from sources import ocr_vision  # noqa: PLC0415

    return ocr_vision.recognize, ocr_vision.recognize_region


def upsert_category(conn: sqlite3.Connection, rec: evidence.ImportRecord, *, category: str,
                    period_start: str, period_end: str, minutes: float) -> None:
    """One Screen Time category total. Does not commit (finish_import does)."""
    conn.execute(
        "INSERT INTO category_measurements (import_id, category, period_start, period_end, minutes) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(category, period_start, period_end) DO UPDATE SET "
        "import_id = excluded.import_id, minutes = excluded.minutes",
        (rec.id, category, period_start, period_end, minutes),
    )


def ingest_batch(conn: sqlite3.Connection, clk: Clock, settings: Settings, images: list[tuple[bytes, str]], *,
                 received_at: str, ocr: Callable | None = None, region_ocr: Callable | None = None) -> evidence.Outcome:
    """One scrolled capture (several screenshots) -> one import.

    `images` are (bytes, filename-or-extension) in scroll order. `ocr` and
    `region_ocr` default to Apple Vision; tests inject recorded observations.
    """
    if not images:
        rec = evidence.begin_import(conn, clk, kind=KIND, parser_version=PARSER_VERSION)
        evidence.fail_import(conn, rec, "no images were received")
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error="no images were received")
    stored = [evidence.store_raw(settings, KIND, data, _ext(name)) for data, name in images]
    manifest = {
        "schema": 1,
        "images": [{"path": rel, "sha256": sha} for rel, sha in stored],
        "received_at": received_at,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=1).encode()
    rel, sha = evidence.store_raw(settings, KIND, manifest_bytes, ".json")
    key = _images_key([s for _, s in stored], received_at, settings.tz)
    dup = _find_duplicate(conn, sha, key)
    if dup is not None:
        return evidence.Outcome(dup["id"], dup["status"], 0, (), None, dup["period_start"], dup["period_end"],
                                duplicate_of=dup["id"])
    return _run(conn, clk, settings, rel, sha, [data for data, _ in images], received_at, key,
                ocr=ocr, region_ocr=region_ocr, replaces=None)


def reparse(conn: sqlite3.Connection, clk: Clock, settings: Settings, import_id: int, *,
            ocr: Callable | None = None, region_ocr: Callable | None = None) -> evidence.Outcome:
    """Re-read the stored images of an earlier import into a new import.

    The new import's facts replace the old import's: rows the old parse wrote
    and the new one no longer finds are removed, in the same transaction.
    """
    row = conn.execute("SELECT * FROM imports WHERE id = ? AND kind = ?", (import_id, KIND)).fetchone()
    if row is None:
        raise ValueError(f"no Screen Time import #{import_id}")
    try:
        manifest = json.loads(evidence.raw_file(settings, row["raw_path"]).read_bytes())
        images = [evidence.raw_file(settings, i["path"]).read_bytes() for i in manifest["images"]]
        received_at = manifest["received_at"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        rec = evidence.begin_import(conn, clk, kind=KIND, parser_version=PARSER_VERSION,
                                    raw_path=row["raw_path"], raw_sha256=row["raw_sha256"])
        msg = f"stored evidence for import #{import_id} could not be read: {type(exc).__name__}"
        evidence.fail_import(conn, rec, msg)
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error=msg)
    key = _images_key([i["sha256"] for i in manifest["images"]], received_at, settings.tz)
    return _run(conn, clk, settings, row["raw_path"], row["raw_sha256"], images, received_at, key,
                ocr=ocr, region_ocr=region_ocr, replaces=import_id)


def _run(conn, clk, settings, raw_path, raw_sha, images, received_at, images_key, *, ocr, region_ocr, replaces):
    if ocr is None or region_ocr is None:
        default_ocr, default_region = _default_ocr()
        ocr, region_ocr = ocr or default_ocr, region_ocr or default_region
    rec = evidence.begin_import(conn, clk, kind=KIND, parser_version=PARSER_VERSION,
                                raw_path=raw_path, raw_sha256=raw_sha)
    rec.meta.update({"images_key": images_key, "image_count": len(images), "received_at": received_at})
    if replaces is not None:
        rec.meta["reparse_of"] = replaces

    try:
        pages = [list(ocr(data)) for data in images]
    except Exception as exc:  # noqa: BLE001 — any OCR failure is a failed import, raw kept
        msg = f"text recognition failed ({type(exc).__name__}: {exc}); the screenshots are saved and can be re-read"
        evidence.fail_import(conn, rec, msg)
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error=msg)

    pages, notes = recover_unnamed_rows(pages, images, region_ocr)
    if notes:
        rec.meta["recovery_notes"] = notes
    try:
        parsed = parse_observations(pages, received_at=received_at, tz=settings.tz)
    except ScreenTimeParseError as exc:
        evidence.fail_import(conn, rec, str(exc))
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error=str(exc))

    rec.warnings.extend(parsed.warnings)
    rec.meta.update(parsed.meta)
    written = 0
    try:
        if replaces is not None:
            conn.execute("DELETE FROM measurements WHERE import_id = ?", (replaces,))
            conn.execute("DELETE FROM category_measurements WHERE import_id = ?", (replaces,))
        if parsed.is_full_past_week and not parsed.app_rows:
            # A complete past week, but no Most Used row was read at all (the
            # list was missing, showed categories, or no row had a name). The
            # warnings already say what to resend. Recording the week's period
            # would mark it covered and report a clean zero for the iPhone, so
            # this stays evidence only: the prompt keeps asking, and nothing
            # reads the structure failure as "no usage".
            rec.warnings.append("No app rows were read, so this capture was kept as evidence only and does "
                                "not count as Screen Time for " + f"{parsed.period_start} – {parsed.period_end}.")
            status = evidence.finish_import(conn, clk, settings, rec, period_start=None, period_end=None,
                                            status=T.IMPORT_PARTIAL)
            return evidence.Outcome(rec.id, status, 0, tuple(rec.warnings))
        if parsed.is_full_past_week:
            for app in parsed.app_rows:
                source = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, app.name)
                evidence.upsert_measurement(
                    conn, rec, source_id=source.id, device=T.DEVICE_IPHONE, provenance=T.PROV_SCREENSHOT,
                    period_start=parsed.period_start, period_end=parsed.period_end,
                    minutes=app.minutes, status=app.status, item_title=app.name,
                    detail={"raw": list(app.raw), "confidence": app.confidence, "screenshots": list(app.pages),
                            "name_reread_from_crop": app.recovered},
                )
                written += 1
            for name, minutes in parsed.categories:
                upsert_category(conn, rec, category=name, period_start=parsed.period_start,
                                period_end=parsed.period_end, minutes=minutes)
            rec.meta["categories_written"] = len(parsed.categories)
            status = evidence.finish_import(conn, clk, settings, rec,
                                            period_start=parsed.period_start, period_end=parsed.period_end)
            return evidence.Outcome(rec.id, status, written, tuple(rec.warnings), None,
                                    parsed.period_start, parsed.period_end)
        # Evidence only: no period, so it neither covers a week nor starts the diagnostic.
        status = evidence.finish_import(conn, clk, settings, rec, period_start=None, period_end=None,
                                        status=T.IMPORT_PARTIAL)
        return evidence.Outcome(rec.id, status, 0, tuple(rec.warnings))
    except (evidence.ParseError, labels.LabelError, evidence.EvidenceError) as exc:
        evidence.fail_import(conn, rec, f"could not record the parse: {exc}")
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, tuple(rec.warnings), f"could not record the parse: {exc}")
