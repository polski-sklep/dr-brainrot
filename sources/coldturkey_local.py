"""Cold Turkey, automated: read this Mac's own statistics database daily.

Cold Turkey Blocker keeps browser usage in a local SQLite file,
`/Library/Application Support/Cold Turkey/data-browser.db`, table

    stats(date REAL  -- Unix seconds, one row per minute,
          domain TEXT -- despite the name, the FULL URL incl. query strings,
          seconds REAL, user TEXT)

Summed per hostname per local day, it reproduced the user's manual export
exactly on every finished day (checked 2026-09-16, DECISIONS DR5.1), and it
lists every domain rather than the chart's top five.

**Privacy is the design constraint here.** That column holds search queries,
map links and file paths. This module reduces every row to its hostname *in
memory, while reading*, and nothing finer ever leaves the function: not into
the database, not into raw evidence, not into logs or exceptions. The stored
evidence is the export-shaped hostname/day/minutes document handed to
`coldturkey.ingest`. That is a deliberate exception to "keep the raw
evidence": the full-URL table stays where Cold Turkey keeps it and is never
copied.

Only finished local days are read (never today), from the later of go-live
and `LOOKBACK_DAYS` ago, so a day corrected by Cold Turkey after midnight is
re-read and replaces its facts on their natural key. An unchanged window
produces identical bytes and is recorded as a duplicate, not a new import.
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import drbr_types as T
import evidence
from clock import Clock
from config import Settings
from sources import coldturkey

#: Where Cold Turkey Blocker keeps it on macOS. Only ever used via DRBR_COLDTURKEY_DB.
DEFAULT_DB = pathlib.Path("/Library/Application Support/Cold Turkey/data-browser.db")
LOOKBACK_DAYS = 8
FILENAME = "cold-turkey-local-statistics.json"

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")


class ColdTurkeyLocalError(evidence.ParseError):
    """The statistics database is missing, unreadable, or not the shape observed."""


def hostname(url: str) -> str | None:
    """The only identity kept from a row. None for rows with no usable host.

    `file:` URLs collapse to the single label `file:`, matching Cold Turkey's
    own export, so no local file path survives.
    """
    value = (url or "").strip().lower()
    if not value:
        return None
    if value.startswith("file:"):
        return "file:"
    rest = _SCHEME.sub("", value)
    host = re.split(r"[/?#]", rest, maxsplit=1)[0]
    host = host.rsplit("@", 1)[-1]            # drop any user:password@
    host = host.split(":", 1)[0] if not host.startswith("[") else host
    if not host or re.search(r"\s", host):
        return None
    return host


def read_host_minutes(db_path: pathlib.Path, *, first_day: date, last_day: date,
                      tz: str) -> dict[tuple[date, str], float]:
    """(local day, hostname) -> minutes for first_day..last_day inclusive. Read-only."""
    if not db_path.exists():
        raise ColdTurkeyLocalError("Cold Turkey's statistics database was not found")
    zone = ZoneInfo(tz)
    start = datetime.combine(first_day, time(0), tzinfo=zone).timestamp()
    end = datetime.combine(last_day + timedelta(days=1), time(0), tzinfo=zone).timestamp()
    totals: dict[tuple[date, str], float] = defaultdict(float)
    try:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise ColdTurkeyLocalError(f"could not open Cold Turkey's statistics ({type(exc).__name__})") from None
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stats)")}
        if not {"date", "domain", "seconds"} <= cols:
            raise ColdTurkeyLocalError("Cold Turkey's stats table no longer has date/domain/seconds; "
                                       "the format changed, so nothing was read")
        rows = conn.execute("SELECT date, domain, seconds FROM stats WHERE date >= ? AND date < ?", (start, end))
        keys: dict[str, str | None] = {}
        for when, url, seconds in rows:
            host = hostname(url)
            if host is None or seconds is None or seconds < 0:
                continue
            if host not in keys:
                # The importer's own fact key (www. stripped), so www.example.com and
                # example.com are one total here rather than a duplicate conflict there.
                try:
                    keys[host] = coldturkey.fact_key(host)
                except evidence.ParseError:
                    keys[host] = None
            host = keys[host]
            if host is None:
                continue
            day = datetime.fromtimestamp(float(when), timezone.utc).astimezone(zone).date()
            totals[(day, host)] += float(seconds) / 60.0
    except sqlite3.Error as exc:
        raise ColdTurkeyLocalError(f"could not read Cold Turkey's statistics ({type(exc).__name__})") from None
    finally:
        conn.close()
    return dict(totals)


def export_document(totals: dict[tuple[date, str], float]) -> bytes:
    """Hostname totals in Cold Turkey's own export shape, deterministic bytes.

    Days are encoded as UTC-midnight epoch milliseconds of the local date,
    exactly as the real export encodes them, so `coldturkey.parse` reads them
    back to the same local day.
    """
    by_host: dict[str, list[list]] = defaultdict(list)
    for (day, host), minutes in sorted(totals.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ms = int(datetime.combine(day, time(0), tzinfo=timezone.utc).timestamp() * 1000)
        by_host[host].append([ms, round(minutes, 2)])
    doc = [{"label": host, "data": points} for host, points in sorted(by_host.items())]
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")


def window(clk: Clock, settings: Settings) -> tuple[date, date] | None:
    """(first, last) finished local days to read, or None when there are none yet."""
    today = T.local_date(clk.now(), settings.tz)
    last = today - timedelta(days=1)
    first = today - timedelta(days=LOOKBACK_DAYS)
    if settings.go_live:
        first = max(first, T.parse_date(settings.go_live))
    return (first, last) if first <= last else None


def sync(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> evidence.Outcome | None:
    """Read finished days and import them. None when there is nothing to read.

    Raises ColdTurkeyLocalError when the database cannot be read: the daily
    job reports that as a failed step, and the Monday prompt for a manual
    export still fires because the week stays uncovered.
    """
    if settings.coldturkey_db is None:
        return None          # not configured: manual exports only. Never guess a path (tests must not read it).
    span = window(clk, settings)
    if span is None:
        return None
    totals = read_host_minutes(pathlib.Path(settings.coldturkey_db), first_day=span[0], last_day=span[1],
                               tz=settings.tz)
    if not totals:
        return None
    return coldturkey.ingest(conn, clk, settings, export_document(totals), filename=FILENAME,
                             origin=coldturkey.ORIGIN_LOCAL_DB)

