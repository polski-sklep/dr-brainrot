"""Qbserve: desktop app time on this Mac, read daily from Qbserve's own database.

User request (DECISIONS DR5.3): close the desktop-apps blind spot. Cold Turkey
sees browser tabs only, so native apps (chat, email, games, the Spotify
client...) were invisible on the desktop.

Qbserve keeps `~/Library/Application Support/Qbserve/UserDatabase.sqlite`:

    Apps(_id, bundle, localized_name, ...)
    Activities(_id, title, source, app_id, category_id, ...)
    HistoryTablesIndex(table_name, start_date)           -- one history table per month
    z_HistoryLog_YYYY_M(activity_id, start_time, duration, project_id)
                                                           -- unix seconds; rows <= 60 s, sequential

What is used, and what is deliberately not:

* **App identity and minutes only.** `Activities.title` holds window titles
  and page names; it is never selected. Rows are summed per app bundle per
  local day while reading, and only bundle, display name, day and minutes
  leave this module.
* **Browsers are skipped**, because Cold Turkey measures them per site and
  counting them here too would double every browser minute. A browser is
  recognised from the data: any app that owns a website activity
  (`Activities.source = 222`, observed on 2026-09-17 as the website type).
  There is no hard-coded list to go stale.
* **Qbserve's productivity ratings and categories are never read.** They are
  an automatic judgement of value, the thing DRBR exists not to do. You label
  apps like any other source.

Only finished local days are read, from the later of go-live and
`LOOKBACK_DAYS` ago. The stored evidence is the per-app/day/minutes document
itself (the database is never copied). An unchanged window is a duplicate.

**Absence is not zero.** Qbserve records nothing while it is not running, which
looks exactly like a day without app use. So an import only claims to cover
days up to the local day of Qbserve's newest record (`covered_through`); days
after that stay uncovered ("not received") rather than reading as 0 minutes,
and the daily job warns when that record is more than 36 hours old.
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import clock
import drbr_types as T
import evidence
import labels
from clock import Clock
from config import Settings

PARSER_VERSION = "qbserve-1"
KIND = "qbserve"
LOOKBACK_DAYS = 8

#: Qbserve's activity type for a website visit (observed; see module docstring).
WEBSITE_SOURCE = 222

#: Matched with fullmatch: `$` alone would also accept a trailing newline.
_HISTORY_TABLE = re.compile(r"z_HistoryLog_\d{4}_\d{1,2}")


class QbserveError(evidence.ParseError):
    """The Qbserve database is missing, unreadable, or not the shape observed."""


@dataclass(frozen=True)
class AppDay:
    bundle: str
    name: str
    day: date
    minutes: float


# --- reading ------------------------------------------------------------------------

def _connect(db_path: pathlib.Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise QbserveError("Qbserve's database was not found")
    try:
        return sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise QbserveError(f"could not open Qbserve's database ({type(exc).__name__})") from None


def _check_shape(conn: sqlite3.Connection) -> None:
    need = {
        "Apps": {"_id", "bundle", "localized_name"},
        "Activities": {"_id", "source", "app_id"},
        "HistoryTablesIndex": {"table_name", "start_date"},
    }
    for table, cols in need.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols <= have:
            raise QbserveError(f"Qbserve's {table} table changed shape; nothing was read")


def read_app_minutes(db_path: pathlib.Path, *, first_day: date, last_day: date,
                     tz: str) -> tuple[list[AppDay], dict]:
    """Per-app minutes per local day, browsers excluded. -> (rows, facts about the read)."""
    zone = ZoneInfo(tz)
    start = datetime.combine(first_day, time(0), tzinfo=zone).timestamp()
    end = datetime.combine(last_day + timedelta(days=1), time(0), tzinfo=zone).timestamp()
    conn = _connect(db_path)
    try:
        _check_shape(conn)
        apps = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT _id, bundle, localized_name FROM Apps")}
        browsers = {row[0] for row in conn.execute(
            "SELECT DISTINCT app_id FROM Activities WHERE source = ?", (WEBSITE_SOURCE,))}
        activity_app = {row[0]: row[1] for row in conn.execute("SELECT _id, app_id FROM Activities")}
        tables = [row[0] for row in conn.execute("SELECT table_name FROM HistoryTablesIndex ORDER BY start_date")]
        totals: dict[tuple[str, date], float] = defaultdict(float)
        names: dict[str, str] = {}
        browser_min = 0.0
        latest: float | None = None
        for table in tables:
            if not isinstance(table, str) or not _HISTORY_TABLE.fullmatch(table):
                continue                                   # never interpolate an unexpected name into SQL
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not {"activity_id", "start_time", "duration"} <= cols:
                raise QbserveError(f"Qbserve's {table} changed shape; nothing was read")
            (tmax,) = conn.execute(f"SELECT MAX(start_time) FROM {table}").fetchone()
            if tmax is not None:
                latest = tmax if latest is None else max(latest, tmax)
            rows = conn.execute(
                f"SELECT activity_id, start_time, duration FROM {table} WHERE start_time >= ? AND start_time < ?",
                (start, end))
            for activity_id, started, duration in rows:
                app_id = activity_app.get(activity_id)
                if app_id is None or duration is None or duration < 0:
                    continue
                if app_id in browsers:
                    browser_min += duration / 60.0
                    continue
                bundle, name = apps.get(app_id, (None, None))
                if not bundle:
                    continue
                day = datetime.fromtimestamp(float(started), timezone.utc).astimezone(zone).date()
                totals[(bundle, day)] += duration / 60.0
                names[bundle] = name or bundle
    except sqlite3.Error as exc:
        raise QbserveError(f"could not read Qbserve's database ({type(exc).__name__})") from None
    finally:
        conn.close()
    out = [AppDay(b, names[b], d, m) for (b, d), m in sorted(totals.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    facts = {"browser_apps_skipped": len(browsers), "browser_minutes_skipped": round(browser_min, 2),
             "latest_record_at": clock.iso(datetime.fromtimestamp(latest, timezone.utc)) if latest else None}
    return out, facts


def covered_through(facts: dict, *, last_day: date, tz: str) -> date | None:
    """The last day the read can vouch for: min(last_day, local day of the newest record)."""
    latest = facts.get("latest_record_at")
    if not latest:
        return None
    return min(last_day, T.local_date(latest, tz))


def document(rows: list[AppDay], facts: dict, *, first_day: date, last_day: date, tz: str) -> bytes:
    """The stored evidence: app/day/minutes only, deterministic bytes.

    Holds local dates only, never the exact time of the newest record, so a
    re-read on the same day with nothing new is byte-identical (a duplicate).
    """
    through = covered_through(facts, last_day=last_day, tz=tz)
    return json.dumps({
        "v": 1,
        "first_day": first_day.isoformat(),
        "last_day": last_day.isoformat(),
        "covered_through": through.isoformat() if through else None,
        "browser_apps_skipped": facts["browser_apps_skipped"],
        "apps": [{"bundle": r.bundle, "name": r.name, "day": r.day.isoformat(), "minutes": round(r.minutes, 2)}
                 for r in rows],
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse(data: bytes) -> tuple[date, date | None, list[AppDay]]:
    """-> (first day read, last day covered or None when nothing is covered, rows)."""
    try:
        doc = json.loads(data.decode("utf-8"))
        first = date.fromisoformat(doc["first_day"])
        date.fromisoformat(doc["last_day"])
        last = date.fromisoformat(doc["covered_through"]) if doc["covered_through"] else None
        if last is not None and last < first:
            last = None
        rows = [AppDay(a["bundle"], a["name"], date.fromisoformat(a["day"]), float(a["minutes"]))
                for a in doc["apps"]]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise QbserveError(f"stored Qbserve document is not readable ({type(exc).__name__})") from None
    if any(r.minutes < 0 or r.minutes > 1440 for r in rows):
        raise QbserveError("stored Qbserve document has an impossible daily value")
    return first, last, rows


# --- importing ----------------------------------------------------------------------

def window(clk: Clock, settings: Settings) -> tuple[date, date] | None:
    today = T.local_date(clk.now(), settings.tz)
    last = today - timedelta(days=1)
    first = today - timedelta(days=LOOKBACK_DAYS)
    if settings.go_live:
        first = max(first, T.parse_date(settings.go_live))
    return (first, last) if first <= last else None


def _record(conn, clk, settings, data: bytes, rel: str, sha: str, *, facts: dict,
            replaces: int | None = None) -> evidence.Outcome:
    rec = evidence.begin_import(conn, clk, kind=KIND, parser_version=PARSER_VERSION, raw_path=rel, raw_sha256=sha)
    rec.meta.update({k: v for k, v in facts.items()})
    if replaces is not None:
        rec.meta["reparse_of"] = replaces
    try:
        first, last, rows = parse(data)
        if replaces is not None:
            conn.execute("DELETE FROM measurements WHERE import_id = ?", (replaces,))
        written = 0
        for r in rows:
            source = labels.ensure_source(conn, clk, T.KIND_DESKTOP_APP, r.bundle, r.name)
            evidence.upsert_measurement(
                conn, rec, source_id=source.id, device=T.DEVICE_DESKTOP, provenance=T.PROV_APP_TRACKER,
                period_start=r.day.isoformat(), period_end=r.day.isoformat(), minutes=round(r.minutes, 2),
                item_key=r.bundle.casefold(), item_title=r.name, detail={"bundle": r.bundle},
            )
            written += 1
        # The read covered every day up to Qbserve's newest record, including days
        # with no app use, so the period runs to there, not just to days with rows.
        # Nothing recorded inside the window: evidence only, with a NULL period, so
        # the week reads "not received" instead of 0 minutes.
        if last is None:
            rec.warnings.append("Qbserve recorded nothing in the days read; is it running?")
        status = evidence.finish_import(conn, clk, settings, rec,
                                        period_start=first.isoformat() if last else None,
                                        period_end=last.isoformat() if last else None)
        return evidence.Outcome(rec.id, status, written, tuple(rec.warnings), None,
                                first.isoformat() if last else None, last.isoformat() if last else None)
    except (evidence.ParseError, labels.LabelError, evidence.EvidenceError) as exc:
        evidence.fail_import(conn, rec, str(exc))
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, tuple(rec.warnings), str(exc))


def sync(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> evidence.Outcome | None:
    """Read finished days and import them. None when not configured or nothing to read yet."""
    if settings.qbserve_db is None:
        return None
    span = window(clk, settings)
    if span is None:
        return None
    rows, facts = read_app_minutes(pathlib.Path(settings.qbserve_db), first_day=span[0], last_day=span[1],
                                   tz=settings.tz)
    data = document(rows, facts, first_day=span[0], last_day=span[1], tz=settings.tz)
    rel, sha = evidence.store_raw(settings, KIND, data, ".json")
    dup = evidence.find_import_by_sha(conn, KIND, sha)
    if dup is not None:
        return evidence.Outcome(dup["id"], dup["status"], 0, (), None, dup["period_start"], dup["period_end"],
                                duplicate_of=dup["id"])
    return _record(conn, clk, settings, data, rel, sha, facts=facts)


def reparse(conn: sqlite3.Connection, clk: Clock, settings: Settings, import_id: int) -> evidence.Outcome:
    row = conn.execute("SELECT * FROM imports WHERE id = ? AND kind = ?", (import_id, KIND)).fetchone()
    if row is None:
        raise ValueError(f"no Qbserve import #{import_id}")
    data = evidence.raw_file(settings, row["raw_path"]).read_bytes()
    facts = {k: v for k, v in json.loads(row["meta"] or "{}").items() if k != "reparse_of"}
    return _record(conn, clk, settings, data, row["raw_path"], row["raw_sha256"], facts=facts, replaces=import_id)


def staleness_hours(conn: sqlite3.Connection, clk: Clock) -> float | None:
    """Hours since Qbserve last recorded anything, from the latest import's meta."""
    row = conn.execute("SELECT meta FROM imports WHERE kind = ? AND status != 'failed' ORDER BY id DESC LIMIT 1",
                       (KIND,)).fetchone()
    if row is None:
        return None
    latest = json.loads(row["meta"] or "{}").get("latest_record_at")
    if not latest:
        return None
    return (clk.now() - clock.parse(latest)).total_seconds() / 3600
