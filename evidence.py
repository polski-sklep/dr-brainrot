"""Raw evidence, import records, measurement writes, and the diagnostic clock.

Phase 1 contract module (CONTRACTS.md Part II §4). Owned by no Phase 2 agent.
Every source module writes through here, so the three guarantees below hold
for every source by construction rather than by each agent remembering them.

1. **Raw first.** `store_raw()` writes the original bytes to
   `settings.raw_dir/<kind>/<sha256><ext>` before anything is parsed. A parser
   improvement re-runs against that file (`reparse` in each source module);
   the user is never asked for the evidence twice.
2. **Failure is recorded, not swallowed.** An import that fails still gets an
   `imports` row with status 'failed' and the error text. A parse that
   partly worked is 'partial' and carries its warnings.
3. **NULL is not zero.** `upsert_measurement()` refuses `minutes=0` stand-ins
   for unmeasurable values: status and minutes must agree.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

import clock
import dr_brainrot_types as T
from clock import Clock
from config import Settings

DIAGNOSTIC_START_KEY = "diagnostic_start"


class EvidenceError(Exception):
    """Contract violation by a caller (bad status/minutes pairing, unknown kind)."""


class ParseError(Exception):
    """The evidence could not be parsed. Base class for every source's parse error.

    Ingest functions catch it, record a failed import with the message, and
    return an Outcome. It never escapes to a handler as an unhandled crash, and
    it is never converted into an empty or zero result.
    """


@dataclass(frozen=True)
class Outcome:
    """What every ingest/reparse function returns (CONTRACTS.md Part II §4)."""

    import_id: int | None          # None only when nothing could even be recorded
    status: str                    # 'ok' | 'partial' | 'failed'
    measurements_written: int
    warnings: tuple[str, ...] = ()
    error: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    duplicate_of: int | None = None   # set when these exact bytes were already imported


@dataclass
class ImportRecord:
    """An in-progress import. Mutate `warnings`/`meta`, then `finish_import`."""

    id: int
    kind: str
    raw_path: str | None
    raw_sha256: str | None
    parser_version: str
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


# --- raw evidence -----------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_raw(settings: Settings, kind: str, data: bytes, ext: str) -> tuple[str, str]:
    """Write raw bytes content-addressed. -> (relative path, sha256). Idempotent."""
    if kind not in T.IMPORT_KINDS:
        raise EvidenceError(f"unknown import kind {kind!r}")
    if not ext.startswith("."):
        ext = "." + ext
    digest = sha256_bytes(data)
    rel = f"{kind}/{digest}{ext.lower()}"
    path = pathlib.Path(settings.raw_dir) / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        path.chmod(0o600)
    return rel, digest


def raw_file(settings: Settings, rel_path: str) -> pathlib.Path:
    return pathlib.Path(settings.raw_dir) / rel_path


def find_import_by_sha(conn: sqlite3.Connection, kind: str, sha: str) -> sqlite3.Row | None:
    """The most recent non-failed import of these exact bytes, if any."""
    return conn.execute(
        "SELECT * FROM imports WHERE kind = ? AND raw_sha256 = ? AND status != 'failed' "
        "ORDER BY id DESC LIMIT 1",
        (kind, sha),
    ).fetchone()


# --- import lifecycle -------------------------------------------------------

def begin_import(conn: sqlite3.Connection, clk: Clock, *, kind: str, parser_version: str,
                 raw_path: str | None = None, raw_sha256: str | None = None) -> ImportRecord:
    """Open an import row as 'failed'. Only finish_import can upgrade it.

    Starting pessimistic means a crash between begin and finish leaves an
    honest failed record rather than a phantom success.
    """
    if kind not in T.IMPORT_KINDS:
        raise EvidenceError(f"unknown import kind {kind!r}")
    cur = conn.execute(
        "INSERT INTO imports (kind, received_at, raw_path, raw_sha256, parser_version, status, error) "
        "VALUES (?, ?, ?, ?, ?, 'failed', 'import did not finish')",
        (kind, clock.iso(clk.now()), raw_path, raw_sha256, parser_version),
    )
    conn.commit()
    return ImportRecord(int(cur.lastrowid), kind, raw_path, raw_sha256, parser_version)


def finish_import(conn: sqlite3.Connection, clk: Clock, settings: Settings, rec: ImportRecord, *,
                  period_start: str | None, period_end: str | None,
                  status: str | None = None) -> str:
    """Close an import. status defaults to 'partial' if warnings exist, else 'ok'.

    Also fixes the diagnostic start (first valid data) and lets the allowance
    module recompute any debt that late-arriving minutes change.
    """
    final = status or (T.IMPORT_PARTIAL if rec.warnings else T.IMPORT_OK)
    if final not in (T.IMPORT_OK, T.IMPORT_PARTIAL):
        raise EvidenceError("use fail_import for failures")
    conn.execute(
        "UPDATE imports SET status = ?, error = NULL, period_start = ?, period_end = ?, "
        "warnings = ?, meta = ? WHERE id = ?",
        (final, period_start, period_end, json.dumps(rec.warnings), json.dumps(rec.meta, sort_keys=True), rec.id),
    )
    conn.commit()
    if period_start is not None and rec.kind != "vault_scan":
        # Evidence reaching back before go-live cannot start the clock early.
        first = max(period_start, settings.go_live) if settings.go_live else period_start
        if period_end is None or first <= period_end:
            ensure_diagnostic_start(conn, first)
    _after_import(conn, clk, settings)
    return final


def fail_import(conn: sqlite3.Connection, rec: ImportRecord, error: str) -> None:
    """Record a failed import loudly. Measurements already written stay rolled back."""
    conn.rollback()
    conn.execute(
        "UPDATE imports SET status = 'failed', error = ?, warnings = ?, meta = ? WHERE id = ?",
        (error[:2000], json.dumps(rec.warnings), json.dumps(rec.meta, sort_keys=True), rec.id),
    )
    conn.commit()


def _after_import(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> None:
    import allowance  # noqa: PLC0415 — owned by the gate agent; contract hook

    allowance.on_measurements_changed(conn, clk, settings)


# --- measurements -----------------------------------------------------------

def upsert_measurement(conn: sqlite3.Connection, rec: ImportRecord, *, source_id: int, device: str,
                       provenance: str, period_start: str, period_end: str,
                       minutes: float | None, status: str = "ok", item_key: str = "",
                       item_title: str | None = None, detail: dict | None = None) -> None:
    """Insert or replace one fact on its natural key. Does not commit.

    The caller commits via finish_import, so one import is all or nothing.
    """
    if status not in T.MEASUREMENT_STATUSES:
        raise EvidenceError(f"unknown measurement status {status!r}")
    if (status == "ok") != (minutes is not None):
        raise EvidenceError(
            f"status {status!r} with minutes={minutes!r}: an unmeasured value is NULL, "
            "never a zero, and a measured one must say 'ok'"
        )
    if minutes is not None and minutes < 0:
        raise EvidenceError(f"negative minutes {minutes}")
    if provenance not in (T.PROV_PAGE, T.PROV_SCREENSHOT, T.PROV_EXPORT, T.PROV_APP_TRACKER,
                          T.PROV_BROWSER_SYNC, T.PROV_AUDIBLE_STATS):
        raise EvidenceError(f"provenance {provenance!r} does not write measurements")
    T.parse_date(period_start), T.parse_date(period_end)  # validate shape
    conn.execute(
        """
        INSERT INTO measurements (import_id, source_id, device, provenance, item_key, item_title,
                                  period_start, period_end, minutes, status, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provenance, source_id, item_key, period_start, period_end) DO UPDATE SET
            import_id = excluded.import_id, device = excluded.device,
            item_title = excluded.item_title, minutes = excluded.minutes,
            status = excluded.status, detail = excluded.detail
        """,
        (rec.id, source_id, device, provenance, item_key, item_title, period_start, period_end,
         minutes, status, json.dumps(detail or {}, sort_keys=True)),
    )


# --- meta and the diagnostic clock ------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def ensure_diagnostic_start(conn: sqlite3.Connection, first_data_date: str) -> str:
    """Fix the diagnostic start once, at the first valid data. Never moves after."""
    existing = get_meta(conn, DIAGNOSTIC_START_KEY)
    if existing is not None:
        return existing
    T.parse_date(first_data_date)
    set_meta(conn, DIAGNOSTIC_START_KEY, first_data_date)
    return first_data_date


@dataclass(frozen=True)
class Diagnostic:
    started: bool
    start: str | None          # local date
    end: str | None            # local date, inclusive last day
    days_remaining: int | None
    week_number: int | None    # 1..diagnostic_weeks
    ended: bool


def diagnostic(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> Diagnostic:
    start = get_meta(conn, DIAGNOSTIC_START_KEY)
    if start is None:
        return Diagnostic(False, None, None, None, None, False)
    first = T.parse_date(start)
    last = first + timedelta(days=7 * settings.diagnostic_weeks - 1)
    today = T.local_date(clk.now(), settings.tz)
    remaining = (last - today).days
    week = min(settings.diagnostic_weeks, max(1, (today - first).days // 7 + 1))
    return Diagnostic(True, start, T.iso_date(last), max(0, remaining), week, today > last)
