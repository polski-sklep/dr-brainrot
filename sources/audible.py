"""Audiobooks: Audible's own daily listening totals, split across books (DECISIONS DR5.28).

His decision, 8 Oct 2026: "Yes, I want it; albeit not considered as brainrot".
Audiobooks are content, so they are measured; they are not brain rot, so each
book is a source labelled worthwhile when it is first seen, as his standing
decision. A label he later sets on one book wins.

What is read, once a day, by the morning job (read-only calls only):

* **`1.0/stats/aggregates`** — Audible's own listening total per day, in
  milliseconds of wall-clock listening, across every device on the account.
  It lists only days with listening, so a day missing from a successful read
  is a real zero. At most 30 days per request (`CHUNK_DAYS`).
* **`1.0/library`** — every title's place: time remaining, whether and when it
  was finished. Reduced at once to ASIN, title and those positions; nothing
  else is kept.

**The total is measured; the split across books is estimated.** Each morning's
read notes where every book is. A day's listening, which is Audible's measured
total, is split across the books whose place moved between that day's first
read and the next day's first read, in proportion to how far each moved. If no
book moved in that time, the listening must have happened that morning before
the read, so the window before that read is used instead. If no book moved
there either, or the day came before the first read, the day goes to
"Audible (book not known)". Positions move in book time and he listens at
1.25×, so a position change is only ever a *weight*: it divides the measured
total and never becomes minutes itself (`attribute`).

**Absence is not zero.** A read that succeeds vouches for every day in its
window (`imports.period_start..period_end`): a day there with no listening is
0. A read that fails writes nothing and deletes nothing, so the days after the
last successful read stay unmeasured. The next read reaches back over them
(`window`): Audible keeps its history, so a gap is always filled.

**An import owns its period** (DR5.15). Attribution can move a day from "book
not known" to a real book once a later read exists, so every read deletes its
whole window's figures and writes them again.

**Secrets.** The sign-in (`data/audible_auth.json`, written by
`audible_login.py`) holds a device key and tokens. It is never logged, never
copied into evidence, never put in an error: errors carry the exception's
class name only. When the client refreshes a token, the file is rewritten
atomically at 0600. The client is mkb79's unofficial `audible` library, not an
interface Amazon supports; when it breaks, the read fails loudly and the days
stay unmeasured.

Not signed in (no sign-in file, or no store file) is a choice, not a fault:
`sync` returns None and nothing anywhere asks about it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import pathlib
import sqlite3
import warnings
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import clock
import drbr_types as T
import evidence
import labels
from clock import Clock
from config import Settings

log = logging.getLogger("drbr.audible")
# The library logs file names at INFO and whole responses at DEBUG. Neither
# belongs in logs/jobs.log.
logging.getLogger("audible").setLevel(logging.WARNING)

PARSER_VERSION = "audible-1"
KIND = "audible"

#: Re-read every morning, like Cold Turkey and Qbserve: listening done offline
#: reaches Audible when the phone next syncs, sometimes days later.
LOOKBACK_DAYS = 8
#: The most days one stats request may cover (`daily_listening_interval_duration`).
CHUNK_DAYS = 30
#: How far apart consecutive requests start. The probe of 8 Oct proved a
#: request returns its start day and at least the 27 days after it; whether
#: day 30 is the last or one past it was never seen. Starting each request 28
#: days after the last leaves no day outside every request either way. A day
#: two requests both return is the same figure; the larger is kept.
CHUNK_STEP = 28

STATS_PATH = "1.0/stats/aggregates"
LIBRARY_PATH = "1.0/library"
LIBRARY_GROUPS = "product_desc,listening_status,percent_complete,is_finished"
LIBRARY_LIMIT = 1000
#: The stores `audible_login.py` can find the books in.
COUNTRY_CODES = ("uk", "de", "us", "fr", "it", "es", "ca", "au", "in", "jp", "br")

BOOK_NOT_KNOWN = "Audible (book not known)"
#: DR5.28: his decision for every audiobook, written as his label when a book is
#: first seen. Not a default guess.
STANDING_LABEL = T.WORTHWHILE
STANDING_LABEL_NOTE = "worthwhile: your standing decision for audiobooks (DR5.28)"
#: `meta` key: the last read that reached Audible, whatever it found
#: (as DR5.27): an unchanged re-read records no import but still counts.
READ_AT_KEY = "audible_read_at"
SIGN_IN_COMMAND = "cd ~/Projects/drbr && .venv/bin/python audible_login.py --again"
MAX_DAY_MS = 86_400_000

NO_LIBRARY_WARNING = ("Where each book is couldn't be read this time, so days that needed it are listed "
                      "as book not known until the next read.")
TRUNCATED_WARNING = ("The library returned 1,000 titles, the most one read asks for; books past them "
                     "can't be matched, and their days are listed as book not known.")

#: Why a day went to "book not known".
WHY_BEFORE_FIRST_READ = "before_first_read"
WHY_NO_READ_AFTER = "no_read_after_yet"
WHY_NOTHING_MOVED = "nothing_moved"

#: How a day's figure was split.
BASIS_ONE_BOOK = "one_book"
BASIS_SPLIT = "split"
BASIS_NOT_KNOWN = "book_not_known"

#: Failure kinds, in `imports.meta.failure`.
FAIL_SIGN_IN = "sign_in"
FAIL_UNREACHABLE = "unreachable"
FAIL_UNEXPECTED = "unexpected"


class AudibleError(evidence.ParseError):
    """A stored document or a response is not the shape observed."""


class AudibleReadError(Exception):
    """Audible could not be read. `message` is plain words and a class name, never the exception text."""

    def __init__(self, failure: str, message: str):
        super().__init__(message)
        self.failure = failure
        self.message = message


@dataclass(frozen=True)
class Setup:
    auth_path: pathlib.Path
    country_code: str


@dataclass(frozen=True)
class Book:
    """One title's place in the library, as one read saw it."""

    asin: str
    title: str | None
    remaining_s: int | None
    percent: float | None
    finished: bool | None
    finished_at: str | None
    runtime_s: int | None


@dataclass(frozen=True)
class Snapshot:
    """Where every book was at one read."""

    read_at: str
    day: date                  # the local day of the read
    books: Mapping[str, Book]


@dataclass(frozen=True)
class Share:
    """One book's part of one day's listening, or the day given to "book not known"."""

    asin: str | None           # None: book not known
    title: str | None
    minutes: float
    basis: str                 # BASIS_*
    content_s: int             # book seconds this book's place moved in the window used
    between: tuple[str, str] | None
    why: str | None            # WHY_* when the book is not known
    weight: float = 1.0        # this book's part of the day, 0..1


@dataclass(frozen=True)
class Document:
    store: str
    first: date
    last: date
    days: dict[date, float]                # ms, only days with listening
    library: tuple[Book, ...] | None
    library_problem: str | None = None
    library_truncated: bool = False


# --- set-up -------------------------------------------------------------------------

def store_file(auth_path: pathlib.Path) -> pathlib.Path:
    """The file naming the store that holds the books, beside the sign-in (as `audible_login`)."""
    return pathlib.Path(auth_path).with_name("audible_store.json")


def setup(settings: Settings) -> Setup | None:
    """The sign-in, or None when audiobooks are not set up (optional, never a fault).

    Reads only the small store file and checks the sign-in file exists; the
    client library is not imported here, so the dashboard can ask cheaply.
    """
    auth = getattr(settings, "audible_auth", None)
    if auth is None:
        return None
    auth = pathlib.Path(auth)
    if not auth.is_file():
        return None
    try:
        doc = json.loads(store_file(auth).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    country = doc.get("country_code") if isinstance(doc, dict) else None
    if country not in COUNTRY_CODES:
        return None
    return Setup(auth, country)


# --- which days ---------------------------------------------------------------------

def covered_through(conn: sqlite3.Connection, *, before: str | None = None) -> date | None:
    """The last day any successful read vouched for; with `before`, only reads received before it."""
    sql = "SELECT MAX(period_end) FROM imports WHERE kind = ? AND status IN ('ok', 'partial')"
    params: tuple = (KIND,)
    if before is not None:
        sql += " AND received_at < ?"
        params += (before,)
    (last,) = conn.execute(sql, params).fetchone()
    return T.parse_date(last) if last else None


def window(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> tuple[date, date] | None:
    """(first, last) finished local days to read, or None when there are none yet.

    The last `LOOKBACK_DAYS`, ending yesterday. The first read reaches back to
    go-live; a read after a gap reaches back to the day after the last one
    covered. Never a day before go-live. Only reads from before today decide
    it, so every read on one day asks for the same days, and a Sync now that
    finds nothing new is a duplicate even on the day of the first read.
    """
    today = T.local_date(clk.now(), settings.tz)
    last = today - timedelta(days=1)
    first = today - timedelta(days=LOOKBACK_DAYS)
    midnight = clock.iso(datetime.combine(today, time(0), tzinfo=ZoneInfo(settings.tz)))
    covered = covered_through(conn, before=midnight)
    if covered is None:
        if settings.go_live:
            first = T.parse_date(settings.go_live)
    elif covered + timedelta(days=1) < first:
        first = covered + timedelta(days=1)
    if settings.go_live:
        first = max(first, T.parse_date(settings.go_live))
    return (first, last) if first <= last else None


def chunk_starts(first: date, last: date) -> list[date]:
    """Start days of the stats requests that together return every day in first..last."""
    out = []
    start = first
    while start <= last:
        out.append(start)
        start += timedelta(days=CHUNK_STEP)
    return out


# --- reading ------------------------------------------------------------------------

_SIGN_IN = {"Unauthorized", "NoRefreshToken", "AuthFlowError", "FileEncryptionError"}
_UNREACHABLE = {"NetworkError", "NotResponding", "ServerError", "RatelimitError", "TransportError",
                "TimeoutException", "ConnectionError", "TimeoutError"}


def classify(exc: BaseException) -> tuple[str, str]:
    """-> (failure kind, plain message). By class name, so no import of the library is needed.

    The message names the class only: an exception's text can carry a URL,
    a header or a token, and it never leaves this function.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    name = type(exc).__name__
    if names & _SIGN_IN:
        kind = FAIL_SIGN_IN
    elif "HTTPStatusError" in names:
        # Raised raw only by a token refresh: Amazon refusing the refresh token
        # means the sign-in is gone; its servers failing means try again.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        kind = FAIL_SIGN_IN if status in (400, 401, 403) else FAIL_UNREACHABLE
    elif names & _UNREACHABLE:
        kind = FAIL_UNREACHABLE
    else:
        kind = FAIL_UNEXPECTED
    return kind, failure_message(kind, name)


def failure_message(kind: str, name: str) -> str:
    if kind == FAIL_SIGN_IN:
        return f"Audible no longer accepts the sign-in ({name}). Sign in again: {SIGN_IN_COMMAND}"
    if kind == FAIL_UNREACHABLE:
        return f"Audible couldn't be reached ({name}). The next morning's read tries again."
    return f"Audible's answer wasn't what was expected ({name}); nothing was read."


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _book(item) -> Book | None:
    """One library item reduced to its identity and place. None when it has no ASIN."""
    if not isinstance(item, dict):
        return None
    asin = item.get("asin")
    if not isinstance(asin, str) or not asin.strip():
        return None
    status = item.get("listening_status")
    status = status if isinstance(status, dict) else {}
    title = item.get("title")
    remaining = _number(status.get("time_remaining_seconds"))
    percent = _number(status.get("percent_complete", item.get("percent_complete")))
    finished = status.get("is_finished", item.get("is_finished"))
    finished_at = status.get("finished_at_timestamp")
    runtime = _number(item.get("runtime_length_min"))
    return Book(
        asin=asin.strip(),
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        remaining_s=int(remaining) if remaining is not None and remaining >= 0 else None,
        percent=percent,
        finished=finished if isinstance(finished, bool) else None,
        finished_at=finished_at if isinstance(finished_at, str) and finished_at else None,
        runtime_s=int(round(runtime * 60)) if runtime is not None and runtime > 0 else None,
    )


def reduce_library(response) -> tuple[tuple[Book, ...], bool]:
    """-> (books sorted by ASIN, whether the read hit the title limit)."""
    if not isinstance(response, dict) or not isinstance(response.get("items"), list):
        raise AudibleError("the library answer has no list of items")
    items = response["items"]
    books = {b.asin: b for b in (_book(i) for i in items) if b is not None}
    return tuple(books[a] for a in sorted(books)), len(items) >= LIBRARY_LIMIT


def fetch(client, first: date, last: date) -> tuple[list[dict], tuple[Book, ...] | None, str | None, bool]:
    """Ask Audible for first..last and for where every book is.

    -> (stats responses verbatim, books or None, why the library failed, truncated).
    Raises AudibleReadError when a stats request fails: without the totals
    nothing can be recorded. A library failure only costs attribution.
    """
    stats = []
    for start in chunk_starts(first, last):
        try:
            response = client.get(STATS_PATH, daily_listening_interval_duration=str(CHUNK_DAYS),
                                  daily_listening_interval_start_date=start.isoformat(), store="Audible")
            json.dumps(response)                       # stored verbatim, so it must be plain JSON
        except Exception as exc:  # noqa: BLE001 — classified by name, never passed on
            raise AudibleReadError(*classify(exc)) from None
        stats.append({"start": start.isoformat(), "duration": CHUNK_DAYS, "response": response})
    try:
        books, truncated = reduce_library(
            client.get(LIBRARY_PATH, num_results=LIBRARY_LIMIT, response_groups=LIBRARY_GROUPS))
    except Exception as exc:  # noqa: BLE001 — the totals still stand; only the split waits
        return stats, None, classify(exc)[0], False
    return stats, books, None, truncated


def document(stats: list[dict], books: tuple[Book, ...] | None, *, store: str, first: date, last: date,
             library_problem: str | None = None, truncated: bool = False) -> bytes:
    """The stored evidence, deterministic bytes.

    The stats responses verbatim, and the library reduced to titles and
    places. No time of day, so an unchanged re-read on the same day is
    byte-identical (a duplicate). No sign-in data of any kind.
    """
    return json.dumps({
        "v": 1,
        "store": store,
        "first_day": first.isoformat(),
        "last_day": last.isoformat(),
        "stats": stats,
        "library": None if books is None else [
            {"asin": b.asin, "title": b.title, "remaining_s": b.remaining_s, "percent": b.percent,
             "finished": b.finished, "finished_at": b.finished_at, "runtime_s": b.runtime_s}
            for b in books],
        "library_problem": library_problem,
        "library_truncated": truncated,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _day_ms(response, first: date, last: date) -> dict[date, float]:
    if not isinstance(response, dict) or not isinstance(response.get("aggregated_daily_listening_stats"), list):
        raise AudibleError("a stats answer has no list of daily totals")
    out: dict[date, float] = {}
    for entry in response["aggregated_daily_listening_stats"]:
        if not isinstance(entry, dict):
            raise AudibleError("a daily total is not an object")
        if entry.get("unit") != "Milliseconds":
            raise AudibleError("a daily total is not in milliseconds")
        try:
            day = date.fromisoformat(str(entry.get("interval_identifier")))
        except ValueError:
            raise AudibleError("a daily total has no readable date") from None
        ms = _number(entry.get("aggregated_sum"))
        if ms is None or ms < 0 or ms > MAX_DAY_MS:
            raise AudibleError(f"the total for {day} is not a possible day's listening")
        if day in out:
            raise AudibleError(f"{day} is listed twice in one answer")
        if first <= day <= last:          # today's partial figure, and anything outside, are dropped
            out[day] = ms
    return out


def parse(data: bytes) -> Document:
    try:
        doc = json.loads(data.decode("utf-8"))
        store = doc["store"]
        first = date.fromisoformat(doc["first_day"])
        last = date.fromisoformat(doc["last_day"])
        chunks = doc["stats"]
        library = doc["library"]
        problem = doc.get("library_problem")
        truncated = bool(doc.get("library_truncated", False))
        if doc.get("v") != 1 or not isinstance(store, str) or not isinstance(chunks, list) or first > last:
            raise ValueError
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise AudibleError("the stored Audible document is not readable") from None
    days: dict[date, float] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise AudibleError("the stored Audible document is not readable")
        for day, ms in _day_ms(chunk.get("response"), first, last).items():
            days[day] = max(ms, days.get(day, 0.0))
    books = None
    if library is not None:
        if not isinstance(library, list):
            raise AudibleError("the stored library is not readable")
        try:
            books = tuple(Book(b["asin"], b["title"], b["remaining_s"], b["percent"], b["finished"],
                               b["finished_at"], b["runtime_s"]) for b in library)
        except (KeyError, TypeError):
            raise AudibleError("the stored library is not readable") from None
    return Document(store, first, last, dict(sorted(days.items())), books, problem, truncated)


# --- which book ---------------------------------------------------------------------

def content_moved(a: Snapshot, b: Snapshot) -> dict[str, int]:
    """Book seconds each title's place moved from read `a` to read `b`. Only titles that moved.

    A place that went back (a restart, a reset after finishing) is never
    movement. A title newly finished moved from where it was to its end, plus
    anything heard after it reset to the start. A title not seen in `a` is
    taken to start at its beginning.
    """
    out: dict[str, int] = {}
    for asin, now in b.books.items():
        before = a.books.get(asin)
        start = before.remaining_s if before is not None else now.runtime_s
        if start is None:
            continue
        newly_finished = now.finished_at is not None and now.finished_at != (
            before.finished_at if before is not None else None)
        if newly_finished:
            again = (max(0, now.runtime_s - now.remaining_s)
                     if now.runtime_s is not None and now.remaining_s is not None else 0)
            moved = start + again
        elif now.remaining_s is None:
            continue
        else:
            moved = max(0, start - now.remaining_s)
        if moved > 0:
            out[asin] = int(moved)
    return out


def first_reads(snapshots: list[Snapshot]) -> list[Snapshot]:
    """The earliest read of each local day, oldest first.

    Later reads the same day (Sync now) are kept as evidence but never become
    boundaries, so a day's split does not depend on when Sync now was pressed.
    """
    by_day: dict[date, Snapshot] = {}
    for snap in sorted(snapshots, key=lambda s: clock.parse(s.read_at)):
        by_day.setdefault(snap.day, snap)
    return [by_day[d] for d in sorted(by_day)]


def split_minutes(total_min: float, weights: Mapping[str, int]) -> dict[str, float]:
    """`total_min` shared by weight, to 2 dp, summing exactly to the rounded total.

    The rounding residual goes to the largest share (ties: the first ASIN).
    """
    total = round(total_min, 2)
    whole = sum(weights.values())
    raw = {asin: total_min * w / whole for asin, w in sorted(weights.items())}
    shares = {asin: round(v, 2) for asin, v in raw.items()}
    residual = round(total - sum(shares.values()), 2)
    if residual:
        biggest = min(raw, key=lambda asin: (-raw[asin], asin))
        shares[biggest] = max(0.0, round(shares[biggest] + residual, 2))
    return shares


def attribute(day: date, total_min: float, reads: list[Snapshot]) -> list[Share]:
    """Split one day's measured total across books. See the module docstring for the rule.

    `reads` is `first_reads(...)`: one per local day, oldest first.
    """
    a_index = max((i for i, r in enumerate(reads) if r.day <= day), default=None)
    b = next((r for r in reads if r.day > day), None)

    def not_known(why: str, between=None) -> list[Share]:
        return [Share(None, None, round(total_min, 2), BASIS_NOT_KNOWN, 0, between, why)]

    if a_index is None:
        return not_known(WHY_BEFORE_FIRST_READ)
    a = reads[a_index]
    if b is None:
        return not_known(WHY_NO_READ_AFTER)
    moved, between = content_moved(a, b), (a.read_at, b.read_at)
    titles = {asin: book.title for asin, book in b.books.items()}
    if not moved and a.day == day and a_index > 0:
        # Nothing moved after the day's own read, so the listening came before it.
        earlier = reads[a_index - 1]
        moved, between = content_moved(earlier, a), (earlier.read_at, a.read_at)
        titles = {asin: book.title for asin, book in a.books.items()}
    if not moved:
        return not_known(WHY_NOTHING_MOVED, between)
    whole = sum(moved.values())
    basis = BASIS_ONE_BOOK if len(moved) == 1 else BASIS_SPLIT
    return [Share(asin, titles.get(asin), minutes, basis, moved[asin], between, None,
                  round(moved[asin] / whole, 4))
            for asin, minutes in split_minutes(total_min, moved).items()]


# --- importing ----------------------------------------------------------------------

def _snapshot(doc: Document, read_at: str, tz: str) -> Snapshot | None:
    if doc.library is None:
        return None
    return Snapshot(read_at, T.local_date(read_at, tz), {b.asin: b for b in doc.library})


def prior_snapshots(conn: sqlite3.Connection, settings: Settings, *, exclude: tuple[int, ...] = ()) -> list[Snapshot]:
    """Every library read stored by a successful import, except `exclude`.

    The read's time is the one the import recorded (`meta.read_at`), not when
    the row was written. A stored file that is gone or unreadable is skipped:
    it only costs that read's part in the split.
    """
    out = []
    for row in conn.execute("SELECT id, raw_path, received_at, meta FROM imports WHERE kind = ? "
                            "AND status IN ('ok', 'partial') AND raw_path IS NOT NULL ORDER BY id", (KIND,)):
        if row["id"] in exclude:
            continue
        try:
            doc = parse(evidence.raw_file(settings, row["raw_path"]).read_bytes())
            meta = json.loads(row["meta"] or "{}")
        except (OSError, ValueError, AudibleError):
            continue
        snap = _snapshot(doc, meta.get("read_at") or row["received_at"], settings.tz)
        if snap is not None:
            out.append(snap)
    return out


def _days_owned_by_newer(conn: sqlite3.Connection, import_id: int, first: date, last: date) -> set[date]:
    """Days in first..last that a later successful read covers. A reparse leaves them alone."""
    owned: set[date] = set()
    for row in conn.execute("SELECT period_start, period_end FROM imports WHERE kind = ? AND id > ? "
                            "AND status IN ('ok', 'partial') AND period_start IS NOT NULL",
                            (KIND, import_id)):
        day = max(first, T.parse_date(row["period_start"]))
        end = min(last, T.parse_date(row["period_end"]))
        while day <= end:
            owned.add(day)
            day += timedelta(days=1)
    return owned


def _record(conn: sqlite3.Connection, clk: Clock, settings: Settings, data: bytes, rel: str, sha: str, *,
            read_at: str, replaces: int | None = None) -> evidence.Outcome:
    rec = evidence.begin_import(conn, clk, kind=KIND, parser_version=PARSER_VERSION, raw_path=rel, raw_sha256=sha)
    rec.meta.update({"read_at": read_at, "standing_label": STANDING_LABEL_NOTE})
    if replaces is not None:
        rec.meta["reparse_of"] = replaces
    try:
        doc = parse(data)
        rec.meta["store"] = doc.store
        if doc.library_problem:
            rec.meta["library_problem"] = doc.library_problem
        this = _snapshot(doc, read_at, settings.tz)
        exclude = (rec.id,) if replaces is None else (rec.id, replaces)
        reads = first_reads(prior_snapshots(conn, settings, exclude=exclude) + ([this] if this else []))
        skip = _days_owned_by_newer(conn, replaces, doc.first, doc.last) if replaces is not None else set()

        # This read owns its window (DR5.15): a day attributed to "book not known"
        # yesterday may belong to a book today, and must not linger beside it.
        day = doc.first
        while day <= doc.last:
            if day not in skip:
                conn.execute("DELETE FROM measurements WHERE provenance = ? AND period_start = ? "
                             "AND period_end = ?", (T.PROV_AUDIBLE_STATS, day.isoformat(), day.isoformat()))
            day += timedelta(days=1)

        written, new_books = 0, []
        for day, ms in doc.days.items():
            if ms <= 0 or day in skip:
                continue
            total = ms / 60000.0
            for share in attribute(day, total, reads):
                if share.minutes <= 0:
                    continue
                key = share.asin or T.UNKNOWN_AUDIOBOOK_KEY
                existed = conn.execute("SELECT 1 FROM sources WHERE kind = ? AND source_key = ?",
                                       (T.KIND_AUDIOBOOK, key)).fetchone()
                source = labels.ensure_source(conn, clk, T.KIND_AUDIOBOOK, key,
                                              share.title or share.asin or BOOK_NOT_KNOWN,
                                              label_on_create=STANDING_LABEL)
                if existed is None:
                    new_books.append(key)
                evidence.upsert_measurement(
                    conn, rec, source_id=source.id, device=T.DEVICE_ACCOUNT, provenance=T.PROV_AUDIBLE_STATS,
                    period_start=day.isoformat(), period_end=day.isoformat(), minutes=share.minutes,
                    detail={"basis": share.basis, "day_total_min": round(total, 2), "share": share.weight,
                            "content_s_moved": share.content_s,
                            "between": list(share.between) if share.between else None, "why": share.why})
                written += 1
        if new_books:
            rec.meta["new_books"] = new_books
        if doc.library is None:
            rec.warnings.append(NO_LIBRARY_WARNING)
        elif doc.library_truncated:
            rec.warnings.append(TRUNCATED_WARNING)
        # Every day first..last is vouched for: a day with no figure there is a real zero.
        status = evidence.finish_import(conn, clk, settings, rec, period_start=doc.first.isoformat(),
                                        period_end=doc.last.isoformat())
        return evidence.Outcome(rec.id, status, written, tuple(rec.warnings), None,
                                doc.first.isoformat(), doc.last.isoformat())
    except (AudibleError, labels.LabelError, evidence.EvidenceError) as exc:
        rec.meta["failure"] = FAIL_UNEXPECTED
        evidence.fail_import(conn, rec, str(exc))
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, tuple(rec.warnings), str(exc))


def _failed(conn: sqlite3.Connection, clk: Clock, exc: AudibleReadError, *, read_at: str,
            first: date, last: date, store: str) -> evidence.Outcome:
    """Record a read that never reached the figures. Nothing is deleted, so nothing reads as zero."""
    rec = evidence.begin_import(conn, clk, kind=KIND, parser_version=PARSER_VERSION)
    rec.meta.update({"failure": exc.failure, "read_at": read_at, "first_day": first.isoformat(),
                     "last_day": last.isoformat(), "store": store})
    evidence.fail_import(conn, rec, exc.message)
    return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, (), exc.message)


def sync(conn: sqlite3.Connection, clk: Clock, settings: Settings, *, client=None) -> evidence.Outcome | None:
    """Read Audible and import what it says. None when not set up or nothing is due yet.

    `client` is injected by tests; otherwise the real client is built from the
    saved sign-in. A failed read is recorded as a failed import and returned,
    never raised. Every read that reaches Audible stamps `READ_AT_KEY`,
    including one that changed nothing.
    """
    found = setup(settings)
    if found is None:
        return None
    span = window(conn, clk, settings)
    if span is None:
        return None
    first, last = span
    read_at = clock.iso(clk.now())
    try:
        if client is not None:
            stats, books, problem, truncated = fetch(client, first, last)
        else:
            with _real_client(found) as real:
                stats, books, problem, truncated = fetch(real, first, last)
    except AudibleReadError as exc:
        return _failed(conn, clk, exc, read_at=read_at, first=first, last=last, store=found.country_code)
    evidence.set_meta(conn, READ_AT_KEY, read_at)
    data = document(stats, books, store=found.country_code, first=first, last=last,
                    library_problem=problem, truncated=truncated)
    rel, sha = evidence.store_raw(settings, KIND, data, ".json")
    dup = evidence.find_import_by_sha(conn, KIND, sha)
    if dup is not None:
        return evidence.Outcome(dup["id"], dup["status"], 0, (), None, dup["period_start"], dup["period_end"],
                                duplicate_of=dup["id"])
    return _record(conn, clk, settings, data, rel, sha, read_at=read_at)


def reparse(conn: sqlite3.Connection, clk: Clock, settings: Settings, import_id: int) -> evidence.Outcome:
    """Rebuild an import's figures from its stored evidence, as of its own read.

    Days a later read covers keep that read's figures: an older document must
    not overwrite a newer total.
    """
    row = conn.execute("SELECT * FROM imports WHERE id = ? AND kind = ?", (import_id, KIND)).fetchone()
    if row is None or not row["raw_path"]:
        raise ValueError(f"no Audible import #{import_id} with stored evidence")
    data = evidence.raw_file(settings, row["raw_path"]).read_bytes()
    meta = json.loads(row["meta"] or "{}")
    return _record(conn, clk, settings, data, row["raw_path"], row["raw_sha256"],
                   read_at=meta.get("read_at") or row["received_at"], replaces=import_id)


# --- the real client ----------------------------------------------------------------

def _fingerprint(auth) -> str:
    """A digest of the sign-in's contents, to notice a refresh without keeping a copy."""
    return hashlib.sha256(json.dumps(auth.to_dict(), sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _save_auth(auth, path: pathlib.Path) -> None:
    """Write a refreshed sign-in back, atomically, readable by him only.

    The file is created at 0600 before anything is written to it, so the
    tokens are never on disk with wider permissions, and swapped into place in
    one rename. A failure leaves the old file and no temporary one behind.
    """
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        os.chmod(tmp, 0o600)
        auth.to_file(tmp, encryption=False, set_default=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception as exc:  # noqa: BLE001 — the old sign-in still works; the next read refreshes again
        with contextlib.suppress(OSError):
            tmp.unlink()
        log.warning("the refreshed Audible sign-in couldn't be saved (%s); the next read refreshes it again",
                    type(exc).__name__)


@contextlib.contextmanager
def _real_client(found: Setup) -> Iterator[object]:
    """The library's client for the store with the books, from the saved sign-in.

    Its warnings (legacy crypto, SHA-1) are suppressed for the whole read:
    they are about the library, not the figures. A sign-in the client
    refreshed while reading is written back, also when the read then failed.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            import audible  # noqa: PLC0415 — only a real read needs it
        except ImportError as exc:
            raise AudibleReadError(FAIL_UNEXPECTED, failure_message(FAIL_UNEXPECTED, type(exc).__name__)) from None
        try:
            auth = audible.Authenticator.from_file(found.auth_path)
        except Exception as exc:  # noqa: BLE001 — a sign-in that can't be loaded is a sign-in to redo
            raise AudibleReadError(
                FAIL_SIGN_IN, f"The saved Audible sign-in couldn't be read ({type(exc).__name__}). "
                              f"Sign in again: {SIGN_IN_COMMAND}") from None
        before = _fingerprint(auth)
        try:
            client = audible.Client(auth=auth, country_code=found.country_code)
        except Exception as exc:  # noqa: BLE001
            raise AudibleReadError(*classify(exc)) from None
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                client.close()
            if _fingerprint(auth) != before:
                _save_auth(auth, found.auth_path)
