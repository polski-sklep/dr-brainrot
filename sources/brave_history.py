"""The phone browser, broken into the sites inside it (DECISIONS DR5.13).

Screen Time reports a phone browser as one opaque block — "Brave, 163 min" —
with no way to tell whether that was Reddit or a bank. Brave Sync carries the
phone's visits to this Mac's own history database, where they can be read
locally, so the block can be attributed to hostnames.

Two facts shape everything here.

**Synced visits carry no duration.** Every row that arrives from another device
has `visit_duration = 0`: sync says *that* a page was opened, never for how
long. So the minutes still come from Screen Time and are apportioned across the
hostnames by the gap between consecutive visits, capped at `IDLE_CAP_MIN` so a
phone left on a page overnight does not swallow the week. That is an estimate,
and `T.PROV_BROWSER_SYNC` exists to say so on the page rather than passing it
off as something measured.

**A synced visit is identifiable.** Chromium records the originating device in
`visits.originator_cache_guid`, empty for visits made on this Mac. Only rows
with a non-empty guid are read, so desktop browsing — already measured by Cold
Turkey — is never double-counted here.

Privacy, as with Cold Turkey (DR5.1): the `urls` table holds full URLs with
paths and query strings. Every row is reduced to its hostname *in memory, while
reading*, and nothing finer leaves the function — not into the database, not
into raw evidence, not into a log or an exception message. The stored evidence
is the hostname/day document, never the URLs. Brave's own history file is read
through a copy and never written to.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

import dr_brainrot_types as T
import evidence
import labels
from clock import Clock
from config import Settings
from sources.coldturkey_local import hostname

#: Where Brave keeps it on macOS. Only ever used via DR_BRAINROT_BRAVE_HISTORY_DB.
DEFAULT_DB = (pathlib.Path.home() / "Library" / "Application Support" / "BraveSoftware"
              / "Brave-Browser" / "Default" / "History")

#: The app Screen Time reports the phone browser as, and whose minutes this
#: module attributes. `T.COVERED_ELSEWHERE` excludes it for the same reason.
PHONE_BROWSER_KEY = "brave"

#: Longest gap between two visits still counted as time spent on the first.
#: Chosen as "you have probably stopped looking": long enough for an article,
#: short enough that a phone left unlocked on a page does not dominate a week.
#: Only relative weights matter — the total always comes from Screen Time — so
#: this sets how strongly a long read outweighs a bounce, not the sum.
IDLE_CAP_MIN = 10.0

PARSER_VERSION = "brave-history-1"
FILENAME = "phone-browser-visits.json"

#: Chromium timestamps are microseconds since 1601-01-01 UTC.
_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class BraveHistoryError(evidence.ParseError):
    """The history database is missing, unreadable, or not the shape observed."""


def _to_utc(chromium_micros: int) -> datetime:
    return _EPOCH + timedelta(microseconds=int(chromium_micros))


def read_phone_visits(db_path: pathlib.Path, *, first_day: date, last_day: date,
                      tz: str) -> list[tuple[date, str, datetime]]:
    """(local day, hostname, instant) for each visit synced FROM another device.

    Sorted by instant. Visits made on this Mac are excluded: they have an empty
    `originator_cache_guid`, and Cold Turkey already measures them.

    Brave holds the file open with WAL, so it is copied and the copy is read.
    Reading the original read-only can fail outright while Brave is running,
    and would miss anything still in the write-ahead log.
    """
    if not db_path.exists():
        raise BraveHistoryError("the browser's history database was not found")
    zone = ZoneInfo(tz)
    start = datetime.combine(first_day, time(0), tzinfo=zone)
    end = datetime.combine(last_day + timedelta(days=1), time(0), tzinfo=zone)
    out: list[tuple[date, str, datetime]] = []
    with tempfile.TemporaryDirectory(prefix="dr-brainrot-history-") as tmp:
        copy = pathlib.Path(tmp) / "History"
        try:
            shutil.copy2(db_path, copy)
            for suffix in ("-wal", "-shm"):
                beside = db_path.with_name(db_path.name + suffix)
                if beside.exists():
                    shutil.copy2(beside, copy.with_name(copy.name + suffix))
        except OSError as exc:
            raise BraveHistoryError(
                f"could not copy the browser's history ({type(exc).__name__})") from None
        try:
            conn = sqlite3.connect(copy, timeout=10)
        except sqlite3.Error as exc:
            raise BraveHistoryError(
                f"could not open the browser's history ({type(exc).__name__})") from None
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(visits)")}
            if not {"visit_time", "url", "originator_cache_guid"} <= cols:
                raise BraveHistoryError(
                    "the history database has no originator_cache_guid column, so a visit from "
                    "the phone cannot be told from one made here; nothing was read")
            rows = conn.execute(
                """
                SELECT v.visit_time, u.url
                  FROM visits v JOIN urls u ON u.id = v.url
                 WHERE COALESCE(v.originator_cache_guid, '') <> ''
                 ORDER BY v.visit_time
                """
            )
            for micros, url in rows:
                host = hostname(url)          # the only identity kept, in memory
                if host is None:
                    continue
                when = _to_utc(micros)
                if not (start <= when < end):
                    continue
                out.append((when.astimezone(zone).date(), host, when))
        except sqlite3.Error as exc:
            raise BraveHistoryError(
                f"could not read the browser's history ({type(exc).__name__})") from None
        finally:
            conn.close()
    return out


def dwell_weights(visits: list[tuple[date, str, datetime]]) -> dict[tuple[date, str], float]:
    """(local day, hostname) -> estimated seconds, from the gaps between visits.

    Each visit is credited with the time until the next one, capped at
    `IDLE_CAP_MIN`. The final visit has no successor, so it takes the median of
    the others — the least surprising guess available, and better than the cap,
    which would make whatever you happened to open last the biggest thing in
    the week.
    """
    if not visits:
        return {}
    cap = IDLE_CAP_MIN * 60.0
    gaps: list[float] = []
    for (_, _, this), (_, _, nxt) in zip(visits, visits[1:]):
        gaps.append(min((nxt - this).total_seconds(), cap))
    last = median(gaps) if gaps else cap
    weights: dict[tuple[date, str], float] = defaultdict(float)
    for (day, host, _), seconds in zip(visits, [*gaps, last]):
        weights[(day, host)] += max(0.0, seconds)
    return dict(weights)


def apportion(weights: dict[tuple[date, str], float], minutes: float) -> dict[str, float]:
    """Share `minutes` across hostnames in proportion to their weight.

    The total is Screen Time's and is preserved exactly: the shares are what is
    estimated, never the sum. An all-zero weighting (every visit at the same
    instant) splits evenly rather than dividing by zero.
    """
    by_host: dict[str, float] = defaultdict(float)
    for (_, host), seconds in weights.items():
        by_host[host] += seconds
    if not by_host:
        return {}
    total = sum(by_host.values())
    if total <= 0:
        share = minutes / len(by_host)
        return {host: share for host in sorted(by_host)}
    return {host: minutes * seconds / total for host, seconds in sorted(by_host.items())}


def visit_document(visits: list[tuple[date, str, datetime]]) -> bytes:
    """The evidence kept: hostname and local day only, deterministic bytes.

    Deliberately not the URLs. As with Cold Turkey (DR5.1), the full-URL table
    stays where the browser keeps it and is never copied into this project.
    """
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for day, host, _ in visits:
        counts[(T.iso_date(day), host)] += 1
    doc = [{"day": day, "host": host, "visits": n}
           for (day, host), n in sorted(counts.items())]
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _browser_blocks(conn: sqlite3.Connection, first_day: date,
                    last_day: date) -> list[sqlite3.Row]:
    """Screen Time's phone-browser figures whose period ends inside the window."""
    return conn.execute(
        """
        SELECT m.period_start, m.period_end, m.minutes
          FROM measurements m JOIN sources s ON s.id = m.source_id
         WHERE s.kind = ? AND s.source_key = ? AND m.provenance = ?
           AND m.status = 'ok' AND m.minutes IS NOT NULL
           AND m.period_end >= ? AND m.period_end <= ?
         ORDER BY m.period_start
        """,
        (T.KIND_IPHONE_APP, PHONE_BROWSER_KEY, T.PROV_SCREENSHOT,
         T.iso_date(first_day), T.iso_date(last_day)),
    ).fetchall()


def window(clk: Clock, settings: Settings, lookback_days: int = 35) -> tuple[date, date]:
    """The span of Screen Time periods to attribute: recent, and never pre-go-live."""
    today = T.local_date(clk.now(), settings.tz)
    first = today - timedelta(days=lookback_days)
    if settings.go_live:
        first = max(first, T.parse_date(settings.go_live))
    return first, today


def sync(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> evidence.Outcome | None:
    """Attribute each phone-browser block to the sites inside it. None when there are none.

    One import per run, covering every Screen Time period in the window, and
    authoritative for each: a period's previous site figures are cleared before
    its new ones are written. A period with a block but no synced visits is
    recorded as a warning rather than silently dropped: its minutes are excluded
    as covered here, so the gap has to be visible (`report.blind_spots` says so).
    """
    db_path = getattr(settings, "brave_history_db", None)
    if db_path is None:
        return None
    first_day, last_day = window(clk, settings)
    blocks = _browser_blocks(conn, first_day, last_day)
    if not blocks:
        return None

    visits = read_phone_visits(pathlib.Path(db_path), first_day=first_day,
                               last_day=last_day, tz=settings.tz)
    rel, sha = evidence.store_raw(settings, "brave_sync", visit_document(visits), "json")
    rec = evidence.begin_import(conn, clk, kind="brave_sync", parser_version=PARSER_VERSION,
                                raw_path=rel, raw_sha256=sha)
    try:
        attributed = 0
        for block in blocks:
            span_start = T.parse_date(block["period_start"])
            span_end = T.parse_date(block["period_end"])
            inside = [v for v in visits if span_start <= v[0] <= span_end]
            shares = apportion(dwell_weights(inside), float(block["minutes"]))
            # This import is authoritative for its period: clear what a previous
            # run wrote before writing again. Replacing on the natural key is not
            # enough, because the key can change — a site that stops appearing in
            # history, or one whose hostname now folds differently (`m.youtube.com`
            # to `youtube.com`), would otherwise linger for ever beside its
            # replacement and be counted twice.
            conn.execute(
                "DELETE FROM measurements WHERE provenance = ? AND period_start = ? "
                "AND period_end = ?",
                (T.PROV_BROWSER_SYNC, block["period_start"], block["period_end"]))
            if not shares:
                rec.warnings.append(
                    f"no synced visits for {block['period_start']}..{block['period_end']}, so "
                    f"{round(float(block['minutes']))} min of phone browsing stayed unattributed")
                continue
            for host, minutes in shares.items():
                kind, key = labels.normalise_key(T.KIND_WEB_DOMAIN, host)
                source = labels.ensure_source(conn, clk, kind, key, host)
                evidence.upsert_measurement(
                    conn, rec, source_id=source.id, device=T.DEVICE_IPHONE,
                    provenance=T.PROV_BROWSER_SYNC, period_start=block["period_start"],
                    period_end=block["period_end"], minutes=round(minutes, 2))
                attributed += 1
    except Exception as exc:                      # noqa: BLE001 — the import records why and re-raises
        evidence.fail_import(conn, rec, f"{type(exc).__name__}: {exc}")
        raise
    status = evidence.finish_import(conn, clk, settings, rec,
                                    period_start=T.iso_date(first_day),
                                    period_end=T.iso_date(last_day))
    return evidence.Outcome(import_id=rec.id, status=status, measurements_written=attributed,
                            warnings=tuple(rec.warnings),
                            period_start=T.iso_date(first_day), period_end=T.iso_date(last_day))
