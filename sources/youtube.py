"""YouTube: watch-history page data -> per-video minutes, via the Data API.

Phase 2, Agent A. Implements CONTRACTS.md Part II §6.1.

There is no API for watch history. The desktop history page, however, carries
its own data as JSON (`ytInitialData` on load, `/youtubei/v1/browse`
continuation responses while scrolling). The browser extension ships those
objects verbatim; everything is parsed here, server-side, so the parser is
tested against a sanitised copy of the real page rather than against a guess
at the DOM (DECISIONS DR1.12).

What the page gives, and what it does not:

* A **date group header** per section ("Today", "Monday", "Sep 9"). No time of
  day, no number of sittings. Neither is fabricated.
* A **progress value** per regular video: the integer `startPercent` that
  sizes the red overlay. It is a property of the *video* (the account's current
  resume position), not of one viewing: a video listed under three dates shows
  the same value three times. So a regular video is measured once per payload,
  under its newest date; the brief accepts that rewatches undercount.
* **Shorts** carry no progress at all. They are assumed fully watched and
  measured at their true Data API duration, never at an assumed average.
* No durations worth trusting (the badge text is display-only), so duration
  comes from the Data API `videos.list` (`contentDetails.duration`).

Everything that cannot be read is stored as NULL with a status and a warning,
never as zero (DECISIONS DR1.22). Structure that is not there at all is a
`YouTubeParseError` and a failed import, never an empty week.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Iterator

import httpx

import clock
import drbr_types as T
import evidence
import labels
from clock import Clock
from config import Settings
from drbr_types import VideoInfo, VideoLookupError

__all__ = [
    "PARSER_VERSION",
    "YouTubeParseError",
    "WatchEntry",
    "ParsedHistory",
    "parse_pages",
    "resolve_date_header",
    "parse_iso8601_duration",
    "extract_initial_data",
    "DataApiClient",
    "FakeYouTubeApi",
    "ingest",
    "reparse",
    "current_cursor",
    "current_cursor_date",
]

PARSER_VERSION = "youtube-1"
PAYLOAD_SCHEMA = 1
IMPORT_KIND = "youtube_history"

CURSOR_KEY = "youtube_cursor"
#: The watch date the cursor video was listed under. Lets a *rewatched* cursor
#: video (which reappears at the top under a newer date) be told apart from
#: the real stopping point. See `_apply_cursor`.
CURSOR_DATE_KEY = "youtube_cursor_date"

#: Used only when the Data API no longer returns a video (removed, private) and
#: the page named no single channel for it. The measurement still needs a
#: source row; inventing a channel would be worse than admitting there is none.
UNKNOWN_CHANNEL_KEY = "unknown"
UNKNOWN_CHANNEL_NAME = "Unknown channel (video unavailable in the Data API)"

log = logging.getLogger("drbr.youtube")


class YouTubeParseError(evidence.ParseError):
    """The history payload does not have the structure this parser reads."""


# --- parsing ----------------------------------------------------------------

@dataclass(frozen=True)
class WatchEntry:
    """One appearance of a video on the history page, in page order.

    `percent` is None for Shorts (they render no progress) and for a regular
    video whose progress element is absent. `resume_s` is the page's
    `watchEndpoint.startTimeSeconds` when present: recorded as evidence only
    (see NOTES-A.md, "startPercent has a 10% floor"), never used for minutes
    under the current contract.
    """

    video_id: str
    title: str
    channel_id: str | None
    channel_name: str | None
    watch_date: str
    is_short: bool
    percent: float | None
    order: int
    resume_s: int | None = None
    live_badge: bool = False


@dataclass(frozen=True)
class ParsedHistory:
    entries: list[WatchEntry]
    warnings: list[str] = field(default_factory=list)


_WEEKDAYS = {name: i for i, name in enumerate(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_MONTH_DAY_RE = re.compile(r"^([a-z]+)\.?\s+(\d{1,2})(?:,\s*(\d{4}))?$")


def resolve_date_header(text: str, captured_local: date) -> date:
    """A section header -> the local calendar day it names.

    Relative headers are relative to the capture instant in `settings.tz`
    (the caller converts). Rules, matching what the real page renders:

    * "Today", "Yesterday".
    * A weekday name is the most recent *past* occurrence within the last
      seven days, never today: on a Wednesday, "Monday" is two days back and
      "Wednesday" would be seven. (The real page switches to "Sep 9" at seven
      days, so that last case is defensive.)
    * "Sep 9" without a year is the most recent such date not after the
      capture day; "Aug 30, 2025" is taken as written.

    Anything else raises `YouTubeParseError`: an unrecognised header (another
    UI language, a new format) must fail the import, not silently misdate
    a week of viewing.
    """
    raw = " ".join((text or "").split())
    s = raw.casefold()
    if s == "today":
        return captured_local
    if s == "yesterday":
        return captured_local - timedelta(days=1)
    if s in _WEEKDAYS:
        back = (captured_local.weekday() - _WEEKDAYS[s]) % 7 or 7
        return captured_local - timedelta(days=back)
    m = _MONTH_DAY_RE.match(s)
    if m and m.group(1) in _MONTHS:
        month, day = _MONTHS[m.group(1)], int(m.group(2))
        if m.group(3):
            try:
                return date(int(m.group(3)), month, day)
            except ValueError:
                raise YouTubeParseError(f"date header {raw!r} is not a real date") from None
        for year in range(captured_local.year, captured_local.year - 9, -1):
            try:
                candidate = date(year, month, day)
            except ValueError:      # Feb 29 in a non-leap year: look further back
                continue
            if candidate <= captured_local:
                return candidate
        raise YouTubeParseError(f"date header {raw!r} is not a real date")
    raise YouTubeParseError(
        f"unrecognised date header {raw!r}; the history page format (or its language) "
        "has changed, so this capture cannot be dated"
    )


def _text(node: Any) -> str | None:
    """YouTube's three text shapes: {'simpleText'}, {'runs': [...]}, {'content'}."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return None
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"]
    if isinstance(node.get("content"), str):
        return node["content"]
    runs = node.get("runs")
    if isinstance(runs, list):
        parts = [r.get("text") for r in runs if isinstance(r, dict) and isinstance(r.get("text"), str)]
        if parts:
            return "".join(parts)
    return None


def _find(node: Any, key: str) -> Iterator[Any]:
    """Every value stored under `key`, depth-first in document order."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _find(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find(v, key)


def _sections(node: Any) -> Iterator[dict]:
    """`itemSectionRenderer` objects in document order, wherever they sit.

    Walking generically rather than down a fixed path is what lets the initial
    page (`contents.twoColumnBrowseResultsRenderer...sectionListRenderer`) and
    a scroll continuation (`onResponseReceivedActions[].appendContinuationItemsAction
    .continuationItems`) parse with the same code, and keeps working when
    YouTube re-nests the wrappers.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "itemSectionRenderer" and isinstance(v, dict):
                yield v
            else:
                yield from _sections(v)
    elif isinstance(node, list):
        for v in node:
            yield from _sections(v)


def _dig(node: Any, *path: str | int) -> Any:
    for step in path:
        if isinstance(step, int):
            if not isinstance(node, list) or len(node) <= step:
                return None
        elif not isinstance(node, dict):
            return None
        node = node[step] if isinstance(step, int) else node.get(step)
    return node


def _percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _lockup_entry(lv: dict, *, watch_date: str, order: int, where: str,
                  warnings: list[str]) -> WatchEntry | None:
    content_type = lv.get("contentType")
    video_id = lv.get("contentId")
    if content_type != "LOCKUP_CONTENT_TYPE_VIDEO":
        warnings.append(f"{where}: skipped a non-video history item ({content_type!r}); it is not measured")
        return None
    if not isinstance(video_id, str) or not video_id:
        warnings.append(f"{where}: a video item has no contentId; it could not be measured")
        return None

    meta = _dig(lv, "metadata", "lockupMetadataViewModel") or {}
    title = _text(meta.get("title")) or ""

    # Channel: a single avatar links to one channel; a collaboration renders an
    # avatar stack naming several. The page's channel is only corroborating
    # evidence (the source is keyed by the API's channel id), so an ambiguous
    # stack records None rather than picking one.
    channel_id = None
    image = meta.get("image") or {}
    if "decoratedAvatarViewModel" in image:
        ids = [b.get("browseId") for b in _find(image["decoratedAvatarViewModel"], "browseEndpoint")
               if isinstance(b, dict)]
        ids = [i for i in ids if isinstance(i, str)]
        channel_id = ids[0] if len(set(ids)) == 1 else None
    rows = _dig(meta, "metadata", "contentMetadataViewModel", "metadataRows") or []
    channel_name = _text(_dig(rows, 0, "metadataParts", 0, "text"))

    bars = [b for b in _find(lv.get("contentImage"), "thumbnailOverlayProgressBarViewModel")]
    percent = None
    if bars:
        percent = _percent(bars[0].get("startPercent") if isinstance(bars[0], dict) else None)
        if percent is None:
            warnings.append(f"{where}: video {video_id} has a progress bar with no readable startPercent")

    live = any(isinstance(b, dict) and b.get("badgeStyle") == "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"
               for b in _find(lv.get("contentImage"), "thumbnailBadgeViewModel"))

    resume = None
    for we in _find(lv.get("rendererContext"), "watchEndpoint"):
        if isinstance(we, dict) and we.get("videoId") == video_id:
            st = we.get("startTimeSeconds")
            resume = st if isinstance(st, int) and not isinstance(st, bool) else None
            break

    return WatchEntry(video_id, title, channel_id, channel_name, watch_date, False, percent, order,
                      resume, live)


def _short_entry(sv: dict, *, watch_date: str, order: int, where: str,
                 warnings: list[str]) -> WatchEntry | None:
    video_id = _dig(sv, "onTap", "innertubeCommand", "reelWatchEndpoint", "videoId")
    if not isinstance(video_id, str) or not video_id:
        warnings.append(f"{where}: a Short has no reelWatchEndpoint.videoId; it could not be measured")
        return None
    title = _text(_dig(sv, "overlayMetadata", "primaryText")) or ""
    return WatchEntry(video_id, title, None, None, watch_date, True, None, order)


def parse_pages(pages: list[dict], *, captured_at: str, tz: str) -> ParsedHistory:
    """Every video appearance in the captured pages, newest first.

    `pages` are `ytInitialData` and/or browse continuation responses, in the
    order they were received. Nothing here consults the cursor or the API;
    that is `ingest`'s job, so this function stays a pure reading of the page.

    Raises `YouTubeParseError` for: no pages; a page that is not an object; a
    page with zero history sections; a section without a resolvable header.
    Individual unreadable items become warnings, never silent drops.
    """
    if not isinstance(pages, list) or not pages:
        raise YouTubeParseError(
            "the capture contains no pages: the extension found no ytInitialData and no "
            "history responses, so there is nothing to measure (not an empty week)"
        )
    try:
        captured_local = T.local_date(captured_at, tz)
    except (TypeError, ValueError) as exc:
        raise YouTubeParseError(f"captured_at {captured_at!r} is not an ISO instant") from exc

    entries: list[WatchEntry] = []
    warnings: list[str] = []
    order = 0
    for pn, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            raise YouTubeParseError(f"page {pn} is not a JSON object")
        sections = list(_sections(page))
        if not sections:
            raise YouTubeParseError(
                f"page {pn} contains no itemSectionRenderer history sections; the page "
                "structure has changed or the wrong response was captured"
            )
        for sn, section in enumerate(sections, start=1):
            where = f"page {pn} section {sn}"
            header = _text(_dig(section, "header", "itemSectionHeaderRenderer", "title"))
            if header is None:
                raise YouTubeParseError(f"{where} has no date header; its videos cannot be dated")
            watch_date = T.iso_date(resolve_date_header(header, captured_local))
            where = f"{where} ({header})"
            items = section.get("contents")
            if not isinstance(items, list):
                raise YouTubeParseError(f"{where} has no contents list")
            for item in items:
                if not isinstance(item, dict):
                    warnings.append(f"{where}: skipped a non-object item")
                    continue
                if "lockupViewModel" in item:
                    e = _lockup_entry(item["lockupViewModel"], watch_date=watch_date, order=order,
                                      where=where, warnings=warnings)
                    if e is not None:
                        entries.append(e)
                        order += 1
                elif "reelShelfRenderer" in item:
                    shelf_items = _dig(item, "reelShelfRenderer", "items")
                    if not isinstance(shelf_items, list):
                        warnings.append(f"{where}: a Shorts shelf has no items list")
                        continue
                    for si in shelf_items:
                        sv = si.get("shortsLockupViewModel") if isinstance(si, dict) else None
                        if not isinstance(sv, dict):
                            warnings.append(f"{where}: skipped an unrecognised Shorts shelf item")
                            continue
                        e = _short_entry(sv, watch_date=watch_date, order=order, where=where,
                                         warnings=warnings)
                        if e is not None:
                            entries.append(e)
                            order += 1
                elif "continuationItemRenderer" in item:
                    continue
                else:
                    kind = next(iter(item), "?")
                    warnings.append(f"{where}: skipped an unrecognised history item {kind!r}; "
                                    "it is not measured")
    return ParsedHistory(entries, warnings)


_INITIAL_DATA_RE = re.compile(r"(?:var\s+|window\[\"|window\.)ytInitialData\"?\]?\s*=\s*")


def extract_initial_data(html: str) -> dict:
    """`ytInitialData` from a saved history page's HTML (manual import, local checks)."""
    m = _INITIAL_DATA_RE.search(html)
    if not m:
        raise YouTubeParseError("no ytInitialData assignment in the HTML")
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError as exc:
        raise YouTubeParseError(f"ytInitialData is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise YouTubeParseError("ytInitialData is not an object")
    return obj


# --- YouTube Data API -------------------------------------------------------

_DURATION_RE = re.compile(
    r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)


def parse_iso8601_duration(value: Any) -> int | None:
    """'PT1H2M3S' -> 3723 seconds. None when there is no usable duration.

    'P0D' is what the API reports for live and upcoming streams. It is not a
    zero-length video, so it maps to None ("duration_missing"), never to 0.
    """
    if not isinstance(value, str):
        return None
    m = _DURATION_RE.match(value.strip())
    if not m or value.strip() in ("P", "PT"):
        return None
    w, d, h, mi, s = m.groups()
    total = (int(w or 0) * 7 * 86400 + int(d or 0) * 86400 + int(h or 0) * 3600
             + int(mi or 0) * 60 + round(float(s or 0)))
    return total or None


MAX_IDS_PER_REQUEST = 50


def _chunk(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class DataApiClient:
    """YouTube Data API v3 `videos.list`, key-authenticated.

    The key travels in the `X-Goog-Api-Key` header, not the `key=` query
    parameter: httpx logs request URLs at INFO, and a URL-borne key would end
    up in any log that level reaches. Error messages are built from status
    codes and Google's error *reason* only, never from the request.

    `transport` is injected so tests use `httpx.MockTransport`.
    """

    API_URL = "https://www.googleapis.com/youtube/v3/videos"

    def __init__(self, settings: Settings, *, transport: Any = None) -> None:
        self.settings = settings
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DataApiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _redact(self, text: str) -> str:
        key = self.settings.youtube_api_key
        return text.replace(key, "***") if key else text

    def videos(self, video_ids: Iterable[str]) -> dict[str, VideoInfo]:
        """-> {video_id: VideoInfo}. Ids the API does not return are simply absent.

        Raises `VideoLookupError` when the API could not be asked or refused
        (network, auth, quota, malformed response): that is a dependency
        failure, not a missing video, and the caller must not treat it as one.
        """
        wanted = list(dict.fromkeys(v for v in video_ids if v))
        if not wanted:
            return {}
        if not self.settings.youtube_api_key:
            raise VideoLookupError("YOUTUBE_API_KEY is not configured")
        out: dict[str, VideoInfo] = {}
        for batch in _chunk(wanted, MAX_IDS_PER_REQUEST):
            payload = self._get(batch)
            items = payload.get("items")
            if not isinstance(items, list):
                raise VideoLookupError("YouTube Data API response has no 'items' list")
            for item in items:
                info = _video_from_json(item)
                if info is not None:
                    out[info.video_id] = info
        return out

    def video_info(self, video_id: str) -> VideoInfo | None:
        return self.videos([video_id]).get(video_id)

    def _get(self, batch: list[str]) -> dict:
        try:
            resp = self._client.get(
                self.API_URL,
                params={"part": "contentDetails,snippet", "id": ",".join(batch),
                        "maxResults": str(MAX_IDS_PER_REQUEST)},
                headers={"X-Goog-Api-Key": self.settings.youtube_api_key,
                         "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise VideoLookupError(
                f"YouTube Data API request failed ({type(exc).__name__}: {self._redact(str(exc))[:200]})"
            ) from None
        if resp.status_code != 200:
            reason = _error_reason(resp)
            what = "quota exhausted" if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded") \
                else "refused the request"
            raise VideoLookupError(
                f"YouTube Data API {what} (HTTP {resp.status_code}{': ' + reason if reason else ''})"
            )
        try:
            payload = resp.json()
        except ValueError:
            raise VideoLookupError("YouTube Data API returned non-JSON") from None
        if not isinstance(payload, dict):
            raise VideoLookupError("YouTube Data API returned an unexpected JSON shape")
        return payload


def _error_reason(resp: httpx.Response) -> str | None:
    try:
        err = resp.json().get("error") or {}
        errors = err.get("errors") or []
        reason = errors[0].get("reason") if errors else None
        return re.sub(r"[^A-Za-z0-9_.-]", "", str(reason))[:60] if reason else None
    except Exception:
        return None


def _video_from_json(item: Any) -> VideoInfo | None:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        return None
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    raw = details.get("duration")
    duration = parse_iso8601_duration(raw)
    if raw is not None and duration is None and raw != "P0D":
        log.warning("unparseable duration %r for video %s", raw, item["id"])
    return VideoInfo(
        video_id=item["id"],
        title=str(snippet.get("title") or ""),
        channel_id=str(snippet.get("channelId") or ""),
        channel_title=str(snippet.get("channelTitle") or ""),
        duration_s=duration,
    )


class FakeYouTubeApi:
    """Same surface as `DataApiClient`, driven by a dict of VideoInfo. No I/O.

    Set `error` to make every call raise it (quota, outage). `calls` records
    each batch asked for.
    """

    def __init__(self, videos: dict[str, VideoInfo] | Iterable[VideoInfo] | None = None, *,
                 error: Exception | None = None) -> None:
        if videos is None:
            videos = {}
        if not isinstance(videos, dict):
            videos = {v.video_id: v for v in videos}
        self.videos_by_id: dict[str, VideoInfo] = dict(videos)
        self.error = error
        self.calls: list[list[str]] = []

    def add(self, info: VideoInfo) -> None:
        self.videos_by_id[info.video_id] = info

    def videos(self, video_ids: Iterable[str]) -> dict[str, VideoInfo]:
        wanted = list(dict.fromkeys(video_ids))
        self.calls.append(wanted)
        if self.error is not None:
            raise self.error
        return {v: self.videos_by_id[v] for v in wanted if v in self.videos_by_id}

    def video_info(self, video_id: str) -> VideoInfo | None:
        return self.videos([video_id]).get(video_id)

    def close(self) -> None:
        return None


# --- ingest -----------------------------------------------------------------

def current_cursor(conn: sqlite3.Connection) -> str | None:
    """Newest video id of the last ok/partial import: where the next sync stops."""
    return evidence.get_meta(conn, CURSOR_KEY)


def current_cursor_date(conn: sqlite3.Connection) -> str | None:
    return evidence.get_meta(conn, CURSOR_DATE_KEY)


@dataclass(frozen=True)
class _Payload:
    captured_at: str
    extension_version: str | None
    cursor_reached: bool | None
    pages: list


def _decode_payload(data: bytes) -> _Payload:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise YouTubeParseError(f"payload is not UTF-8 JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise YouTubeParseError("payload is not a JSON object")
    if obj.get("schema") != PAYLOAD_SCHEMA:
        raise YouTubeParseError(f"unsupported payload schema {obj.get('schema')!r} (expected {PAYLOAD_SCHEMA})")
    captured_at = obj.get("captured_at")
    if not isinstance(captured_at, str):
        raise YouTubeParseError("payload has no captured_at")
    try:
        clock.parse(captured_at)
    except ValueError:
        raise YouTubeParseError(f"captured_at {captured_at!r} is not an ISO instant") from None
    pages = obj.get("pages")
    if not isinstance(pages, list):
        raise YouTubeParseError("payload has no pages list")
    reached = obj.get("cursor_reached")
    return _Payload(captured_at, obj.get("extension_version") if isinstance(obj.get("extension_version"), str) else None,
                    reached if isinstance(reached, bool) else None, pages)


def _apply_cursor(entries: list[WatchEntry], cursor: str | None,
                  cursor_date: str | None) -> tuple[list[WatchEntry], bool, list[str]]:
    """Drop the cursor video and everything listed after it (older).

    -> (new entries, cursor found, warnings). The cursor is matched on video id
    *and* on being listed no later than the date it was stored with: history
    lists a video again under each day it is watched, so a rewatched cursor
    video reappears at the top, and matching on the id alone would throw the
    whole week away.
    """
    if cursor is None:
        return list(entries), False, []
    warnings: list[str] = []
    for i, e in enumerate(entries):
        if e.video_id != cursor:
            continue
        if cursor_date is not None and e.watch_date > cursor_date:
            continue
        return entries[:i], True, warnings
    if any(e.video_id == cursor for e in entries):
        warnings.append(
            f"the previous sync's newest video ({cursor}) appears only under a later date "
            f"(rewatched), not under {cursor_date}; history between {cursor_date} and the "
            "oldest captured day may be missing"
        )
    return list(entries), False, warnings


def _collapse(entries: list[WatchEntry]) -> list[tuple[WatchEntry, list[str]]]:
    """One measurable fact per video per payload (regular) or per day (Shorts).

    A regular video's progress value is the account's current position for
    that video, repeated under every date it is listed. Counting it once per
    listing would multiply one position into several viewings, so it is
    counted once, under its newest date, and the other dates are recorded in
    the detail. A Short has no position; each day it is listed is a separate
    viewing assumed complete, so Shorts collapse only within one day.

    -> [(kept entry, other dates it was listed under)] in page order.
    """
    kept: dict[tuple, int] = {}
    out: list[tuple[WatchEntry, list[str]]] = []
    for e in entries:
        key = (e.video_id, e.watch_date) if e.is_short else (e.video_id,)
        if key in kept:
            others = out[kept[key]][1]
            if e.watch_date != out[kept[key]][0].watch_date and e.watch_date not in others:
                others.append(e.watch_date)
            continue
        kept[key] = len(out)
        out.append((e, []))
    return out


def _ingest_bytes(conn: sqlite3.Connection, clk: Clock, settings: Settings, data: bytes, api: Any, *,
                  rel: str, sha: str, cursor: str | None, cursor_date: str | None,
                  advance_cursor: bool) -> evidence.Outcome:
    rec = evidence.begin_import(conn, clk, kind=IMPORT_KIND, parser_version=PARSER_VERSION,
                                raw_path=rel, raw_sha256=sha)
    rec.meta.update({"cursor_applied": cursor, "cursor_date_applied": cursor_date})
    try:
        payload = _decode_payload(data)
        rec.meta.update({"captured_at": payload.captured_at,
                         "extension_version": payload.extension_version,
                         "pages": len(payload.pages)})
        parsed = parse_pages(payload.pages, captured_at=payload.captured_at, tz=settings.tz)
        rec.warnings.extend(parsed.warnings)
        if not parsed.entries:
            raise YouTubeParseError(
                "the history sections contain no readable videos; refusing to record an empty week"
            )
        newest = parsed.entries[0]
        fresh, found, cursor_warnings = _apply_cursor(parsed.entries, cursor, cursor_date)
        rec.warnings.extend(cursor_warnings)
        if cursor is not None and not found and not cursor_warnings:
            rec.warnings.append(
                f"the previous sync's newest video ({cursor}) was not reached"
                + (" (the extension stopped at its page cap or ran out of pages)"
                   if payload.cursor_reached is False else "")
                + f"; history older than {parsed.entries[-1].watch_date} may be missing"
            )
        facts = _collapse(fresh)
        rec.meta.update({"appearances": len(parsed.entries), "new_appearances": len(fresh),
                         "cursor_found": found, "newest_video": newest.video_id,
                         "newest_date": newest.watch_date})

        infos = api.videos([e.video_id for e, _ in facts]) if facts else {}

        written = 0
        dates: list[str] = []
        for e, other_dates in facts:
            info = infos.get(e.video_id)
            written += _write_fact(conn, clk, rec, e, info, other_dates)
            dates.append(e.watch_date)
        period_start = min(dates) if dates else None
        period_end = max(dates) if dates else None
        rec.meta["measurements"] = written
    except (evidence.ParseError, VideoLookupError) as exc:
        evidence.fail_import(conn, rec, str(exc))
        log.warning("youtube import %s failed: %s", rec.id, exc)
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, tuple(rec.warnings), str(exc))

    status = evidence.finish_import(conn, clk, settings, rec, period_start=period_start, period_end=period_end)
    # Never move the cursor backwards: an old capture posted late must not make
    # the next sync re-walk (and re-skip) history it already covered.
    if advance_cursor and (cursor_date is None or newest.watch_date >= cursor_date):
        evidence.set_meta(conn, CURSOR_KEY, newest.video_id)
        evidence.set_meta(conn, CURSOR_DATE_KEY, newest.watch_date)
    return evidence.Outcome(rec.id, status, written, tuple(rec.warnings), None, period_start, period_end)


def _write_fact(conn: sqlite3.Connection, clk: Clock, rec: evidence.ImportRecord, e: WatchEntry,
                info: VideoInfo | None, other_dates: list[str]) -> int:
    duration_s = info.duration_s if info is not None else None
    if info is not None and info.channel_id:
        source = labels.ensure_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, info.channel_id,
                                      info.channel_title or info.channel_id)
    elif e.channel_id:
        source = labels.ensure_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, e.channel_id,
                                      e.channel_name or e.channel_id)
    else:
        source = labels.ensure_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, UNKNOWN_CHANNEL_KEY,
                                      UNKNOWN_CHANNEL_NAME)

    label = f"{'Short' if e.is_short else 'video'} {e.video_id} ({e.watch_date})"
    status, minutes = "ok", None
    if info is None:
        status = "duration_missing"
        rec.warnings.append(f"{label}: the Data API returned nothing for it (removed or private); "
                            "duration unknown, not measured")
    elif e.is_short:
        if duration_s is None:
            status = "duration_missing"
            rec.warnings.append(f"{label}: the Data API gave no duration; not measured")
        else:
            minutes = duration_s / 60
    elif e.percent is None:
        status = "progress_missing"
        rec.warnings.append(f"{label}: no progress value on the page"
                            + (" (listed as LIVE)" if e.live_badge else "") + "; not measured")
    elif not 0 <= e.percent <= 100:
        status = "unreadable"
        rec.warnings.append(f"{label}: progress {e.percent!r} is outside 0..100; not measured")
    elif duration_s is None:
        status = "duration_missing"
        rec.warnings.append(f"{label}: the Data API gave no duration (live or upcoming stream?); not measured")
    else:
        minutes = e.percent / 100 * duration_s / 60

    detail = {
        "percent": e.percent,
        "duration_s": duration_s,
        "is_short": e.is_short,
        "assumed_full": e.is_short,
        "page_channel_id": e.channel_id,
        "resume_s": e.resume_s,
        "live_badge": e.live_badge,
        "also_listed_on": other_dates,
        "page_order": e.order,
    }
    evidence.upsert_measurement(
        conn, rec, source_id=source.id, device=T.DEVICE_ACCOUNT, provenance=T.PROV_PAGE,
        period_start=e.watch_date, period_end=e.watch_date,
        minutes=minutes if status == "ok" else None, status=status,
        item_key=e.video_id, item_title=(info.title if info is not None and info.title else e.title) or None,
        detail=detail,
    )
    return 1


def ingest(conn: sqlite3.Connection, clk: Clock, settings: Settings, payload: bytes, api: Any) -> evidence.Outcome:
    """Store, dedupe, parse, look up durations, write facts, move the cursor.

    Follows CONTRACTS §4 exactly: raw bytes first; identical bytes return the
    earlier import as `duplicate_of`; any parse or API failure records a
    failed import (raw kept for `reparse`) and leaves the cursor where it was.
    """
    rel, sha = evidence.store_raw(settings, IMPORT_KIND, payload, ".json")
    prior = evidence.find_import_by_sha(conn, IMPORT_KIND, sha)
    if prior is not None:
        return evidence.Outcome(prior["id"], prior["status"], 0, (), None,
                                prior["period_start"], prior["period_end"], duplicate_of=prior["id"])
    return _ingest_bytes(conn, clk, settings, payload, api, rel=rel, sha=sha,
                         cursor=current_cursor(conn), cursor_date=current_cursor_date(conn),
                         advance_cursor=True)


def reparse(conn: sqlite3.Connection, clk: Clock, settings: Settings, import_id: int, api: Any) -> evidence.Outcome:
    """Re-run an earlier import from its stored raw bytes, as a new import row.

    The cursor that applied to the original import is applied again (the
    stored cursor has usually moved on since, and would skip everything). The
    global cursor only advances if nothing has moved it since that import,
    so re-parsing an old week never rewinds a newer sync.
    """
    row = conn.execute("SELECT * FROM imports WHERE id = ? AND kind = ?", (import_id, IMPORT_KIND)).fetchone()
    if row is None:
        return evidence.Outcome(None, T.IMPORT_FAILED, 0, (), f"no youtube_history import #{import_id}")
    if not row["raw_path"]:
        return evidence.Outcome(None, T.IMPORT_FAILED, 0, (), f"import #{import_id} has no raw evidence")
    path = evidence.raw_file(settings, row["raw_path"])
    try:
        data = path.read_bytes()
    except OSError as exc:
        return evidence.Outcome(None, T.IMPORT_FAILED, 0, (),
                                f"raw evidence for import #{import_id} is unreadable: {exc}")
    meta = json.loads(row["meta"] or "{}")
    cursor, cursor_date = meta.get("cursor_applied"), meta.get("cursor_date_applied")
    unchanged = current_cursor(conn) == cursor and current_cursor_date(conn) == cursor_date
    return _ingest_bytes(conn, clk, settings, data, api, rel=row["raw_path"], sha=row["raw_sha256"],
                         cursor=cursor, cursor_date=cursor_date, advance_cursor=unchanged)
