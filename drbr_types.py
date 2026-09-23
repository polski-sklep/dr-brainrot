"""Shared DRBR vocabulary: enums, source grouping, and day/week semantics.

Phase 1 contract module (CONTRACTS.md Part II). Owned by no Phase 2 agent;
imported by all of them. Stdlib only. Changing anything here is a contract
change and happens centrally, never inside a source module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import clock

# --- labels -----------------------------------------------------------------

WASTEFUL = "wasteful"
WORTHWHILE = "worthwhile"
#: The two *value* labels for content. Per-item overrides use only these: a
#: queue item is content by definition (DECISIONS DR5.5).
LABELS = (WASTEFUL, WORTHWHILE)

#: A source label meaning "not content; out of scope" (DECISIONS DR5.5). DRBR
#: measures brain-rot content only. A source the user only goes to to get a task
#: done (shopping, email, banking, a todo app) is a tool: its minutes are never
#: wasteful or worthwhile, never charge the allowance, and are shown separately.
TOOL = "tool"
#: What a *source* may be labelled.
SOURCE_LABELS = (WASTEFUL, WORTHWHILE, TOOL)

#: Unlabelled resolves to this. Brief: "It errs honestly."
DEFAULT_LABEL = WASTEFUL

#: How a resolved label was arrived at, in precedence order.
BASIS_ITEM_OVERRIDE = "item_override"
BASIS_SOURCE_LABEL = "source_label"
BASIS_DEFAULT = "default_unlabelled"

# --- sources ----------------------------------------------------------------

KIND_PODCAST_SHOW = "podcast_show"
KIND_YOUTUBE_CHANNEL = "youtube_channel"
KIND_IPHONE_APP = "iphone_app"
KIND_WEB_DOMAIN = "web_domain"
KIND_X = "x"
KIND_DESKTOP_APP = "desktop_app"      # a native Mac app, from Qbserve (DR5.3); key = bundle id, casefolded
SOURCE_KINDS = (KIND_PODCAST_SHOW, KIND_YOUTUBE_CHANNEL, KIND_IPHONE_APP, KIND_WEB_DOMAIN, KIND_X, KIND_DESKTOP_APP)

#: Keeper / reporting groups: the rows of the cost-per-keeper table.
GROUP_PODCASTS = "podcasts"
GROUP_YOUTUBE = "youtube"
GROUP_X = "x"
GROUP_IPHONE = "iphone_apps"
GROUP_DESKTOP = "desktop_web"
GROUP_DESKTOP_APPS = "desktop_apps"   # native Mac apps (Qbserve, DR5.3); browsers stay in desktop_web
GROUP_WEB = "web"          # vault keepers from articles; has no minutes source
GROUP_OTHER = "other"      # vault keepers with no attributable source
#: Not a minutes group: the Obsidian vault produces counts only. Named here so the
#: "where does each number come from" and "what needs you" tables (DR5.6) can list it
#: beside the measured groups without inventing a string per caller.
GROUP_VAULT = "vault"
REPORT_GROUPS = (GROUP_PODCASTS, GROUP_YOUTUBE, GROUP_X, GROUP_IPHONE, GROUP_DESKTOP, GROUP_DESKTOP_APPS)
KEEPER_GROUPS = ("x", "youtube", "podcasts", "web", "other")

# --- provenance -------------------------------------------------------------

#: Where a number came from. Ordered from most to least direct. The dashboard
#: renders each distinctly; nothing may present a lower tier as a higher one.
PROV_API = "api"                         # Spotify resume_point polling
PROV_PAGE = "page_extraction"            # YouTube history page data x Data API duration
PROV_SCREENSHOT = "screenshot_ocr"       # Screen Time via on-device Apple Vision OCR
PROV_EXPORT = "manual_export"            # Cold Turkey statistics (local database read or JSON export)
PROV_APP_TRACKER = "app_tracker"         # Qbserve's per-app time on this Mac (DR5.3)
PROV_BROWSER_SYNC = "browser_sync"        # the phone browser's own synced history, minutes apportioned (DR5.13)
PROV_VAULT = "vault_scan"                # Obsidian capture-ID scan (counts, never minutes)
PROV_LABEL = "user_label"                # explicit user labels
PROVENANCES = (PROV_API, PROV_PAGE, PROV_SCREENSHOT, PROV_EXPORT, PROV_APP_TRACKER,
               PROV_BROWSER_SYNC, PROV_VAULT, PROV_LABEL)

PROVENANCE_TEXT = {
    PROV_API: "API (Spotify resume position)",
    PROV_PAGE: "Page data (YouTube history) x API duration",
    PROV_SCREENSHOT: "Parsed screenshot (OCR) — lower confidence",
    PROV_EXPORT: "Cold Turkey statistics (this Mac)",
    PROV_APP_TRACKER: "App tracker (Qbserve, this Mac)",
    PROV_VAULT: "Vault scan (count, not minutes)",
    PROV_LABEL: "Your label",
}

DEVICE_IPHONE = "iphone"
DEVICE_DESKTOP = "desktop"
DEVICE_ACCOUNT = "account"   # account-level: YouTube history spans every device

MEASUREMENT_STATUSES = ("ok", "progress_missing", "duration_missing", "unreadable")

IMPORT_KINDS = ("youtube_history", "screentime", "coldturkey", "vault_scan", "qbserve", "brave_sync")
IMPORT_OK, IMPORT_PARTIAL, IMPORT_FAILED = "ok", "partial", "failed"

# --- double counting --------------------------------------------------------

#: Aggregate rows that a more direct source already measures. They are stored
#: and displayed, but excluded from pooled totals, allowance and cost per
#: keeper, or the same minutes would be counted twice (DECISIONS DR1.9).
#: Keys are (kind, normalised source_key).
COVERED_ELSEWHERE: dict[tuple[str, str], str] = {
    (KIND_IPHONE_APP, "youtube"): "YouTube history (account-level)",
    (KIND_WEB_DOMAIN, "youtube.com"): "YouTube history (account-level)",
    (KIND_IPHONE_APP, "spotify"): "Spotify polling (podcasts only; music is out of scope)",
    (KIND_WEB_DOMAIN, "open.spotify.com"): "Spotify polling (podcasts only; music is out of scope)",
    (KIND_DESKTOP_APP, "com.spotify.client"): "Spotify polling (podcasts only; music is out of scope)",
    # The phone browser is measured by the sites inside it, not as one block
    # (DR5.13). Like the entries above, the minutes stay visible in the
    # "covered elsewhere" column, so an exclusion can never hide them.
    (KIND_IPHONE_APP, "brave"): "phone browser history (per site)",
}


def report_group(kind: str, device: str | None = None) -> str:
    """The cost-per-keeper row a source's minutes belong to."""
    if kind == KIND_X:
        return GROUP_X
    if kind == KIND_PODCAST_SHOW:
        return GROUP_PODCASTS
    if kind == KIND_YOUTUBE_CHANNEL:
        return GROUP_YOUTUBE
    if kind == KIND_IPHONE_APP:
        return GROUP_IPHONE
    if kind == KIND_WEB_DOMAIN:
        # A hostname read from the phone's own browser history is phone time,
        # and belongs where the block it replaces was (DR5.13).
        return GROUP_IPHONE if device == DEVICE_IPHONE else GROUP_DESKTOP
    if kind == KIND_DESKTOP_APP:
        return GROUP_DESKTOP_APPS
    raise ValueError(f"unknown source kind {kind!r}")


#: Placeholder YouTube source for videos neither the page nor the Data API
#: attributes to one channel. Its minutes are real; it is never offered for a
#: label, because a label on "unknown" would apply to unrelated channels.
UNKNOWN_YOUTUBE_CHANNEL_KEY = "unknown"

# --- YouTube video facts ------------------------------------------------------

@dataclass(frozen=True)
class VideoInfo:
    """What the YouTube Data API says about one video (videos.list).

    Produced by the YouTube source module; consumed by the bot's YouTube
    capture path. duration_s is None only when the API returned no duration
    (live streams, removed videos) — never guessed.
    """

    video_id: str
    title: str
    channel_id: str
    channel_title: str
    duration_s: int | None


class VideoLookupError(Exception):
    """The API could not be asked (network, quota, auth). Never a missing video."""


# --- item references --------------------------------------------------------

def item_ref(platform: str, native_id: str) -> str:
    """'spotify:<episodeId>' / 'youtube:<videoId>'. The key for item overrides."""
    if platform not in ("spotify", "youtube"):
        raise ValueError(f"no item refs for platform {platform!r}")
    if not native_id or ":" in native_id:
        raise ValueError(f"bad native id {native_id!r}")
    return f"{platform}:{native_id}"


def queue_key_for_youtube(video_id: str) -> str:
    """What `episodes.spotify_id` holds for a YouTube row. Same shape as item_ref."""
    return item_ref("youtube", video_id)


# --- days and weeks ---------------------------------------------------------
#
# Two kinds of time exist in DRBR and they must not be confused.
#
# 1. Instants (UTC ISO strings) — Spotify polls, captures, imports.
# 2. Local calendar days (YYYY-MM-DD in POP_TZ) — every weekly/daily source:
#    Screen Time weeks, Cold Turkey days, YouTube date groups.
#
# A *measurement week* is local Sunday..Saturday, because that is what Screen
# Time and Cold Turkey report. It maps onto the POP *allowance week* that opens
# at 18:00 on that same Sunday (DECISIONS DR1.7).

def local_date(instant: datetime | str, tz: str) -> date:
    dt = clock.parse(instant) if isinstance(instant, str) else instant
    return clock._as_utc(dt).astimezone(ZoneInfo(tz)).date()


def iso_date(d: date) -> str:
    return d.isoformat()


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def week_sunday(d: date) -> date:
    """The Sunday opening the local Sunday..Saturday week containing `d`."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


@dataclass(frozen=True)
class MeasurementWeek:
    sunday: date

    @property
    def saturday(self) -> date:
        return self.sunday + timedelta(days=6)

    @property
    def start(self) -> str:
        return iso_date(self.sunday)

    @property
    def end(self) -> str:
        return iso_date(self.saturday)

    def days(self) -> list[date]:
        return [self.sunday + timedelta(days=i) for i in range(7)]


def measurement_week(d: date) -> MeasurementWeek:
    return MeasurementWeek(week_sunday(d))


def allowance_week_start_for_day(d: date, tz: str) -> str:
    """POP `weeks.week_start` (UTC ISO) that a local calendar day is charged to.

    The allowance week opening at 18:00 on the Sunday of that day's
    measurement week. A day is never split across two allowance weeks.
    """
    sunday = week_sunday(d)
    opening = datetime.combine(sunday, time(clock.TRIAGE_HOUR), tzinfo=ZoneInfo(tz))
    return clock.iso(opening)


def measurement_week_for_allowance_week(week_start: str, tz: str) -> MeasurementWeek:
    """Inverse of allowance_week_start_for_day."""
    return measurement_week(local_date(week_start, tz))
