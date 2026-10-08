"""Cold Turkey Blocker: website-statistics JSON export -> per-domain daily minutes.

CONTRACTS Part II §6.3, DECISIONS DR0.8. Written against the one real export
(`inputs/Cold Turkey Blocker Website Statistics.json`), whose whole shape is:

    [{"label": "<domain>", "data": [[<UTC-midnight epoch ms>, <minutes>], ...]}, ...]

and nothing else. So this parser is strict: only `label` and `data` are
understood, and a series carrying any other key is rejected rather than
imported under a guess about what the new key means (a "unit" field would
silently change every number). A rejected file keeps its raw bytes and can be
re-parsed once the parser learns the new shape.

What the export does not say is recorded, not filled in:

* It lists only the chart's top domains (5 in the real file). Unlisted domains
  are **unmeasured**, never zero; the import meta says so.
* The export includes days that had not ended yet (the real file, exported on a
  Wednesday, ran to Saturday with zeros). A zero for a day that hadn't finished
  is not a measurement, so days on or after the export's local date are not
  written, and the import says so.
* It is one machine's statistics (no sync between desktops).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

import clock
import dr_brainrot_types as T
import evidence
import labels
from clock import Clock
from config import Settings

PARSER_VERSION = "coldturkey-1"
KIND = "coldturkey"

#: The keys observed in the real export. Anything else is an unknown format.
SERIES_KEYS = frozenset({"label", "data"})

UNLISTED_NOTE = ("Cold Turkey exports only the domains shown in its chart; every other domain is unmeasured "
                 "for these days, not zero.")

#: Where the export-shaped bytes came from. `export` is a file the user sent;
#: `local_db` is the daily read of this Mac's own Cold Turkey statistics
#: (sources/coldturkey_local.py), which lists every domain (DECISIONS DR5.1).
ORIGIN_EXPORT = "export"
ORIGIN_LOCAL_DB = "local_db"
ORIGINS = (ORIGIN_EXPORT, ORIGIN_LOCAL_DB)


class ColdTurkeyParseError(evidence.ParseError):
    """The file is not a Cold Turkey website-usage export this parser understands."""


@dataclass(frozen=True)
class ColdTurkeyParse:
    rows: list[tuple[str, date, float | None]]   # (label, day, minutes); None = duplicate conflict
    days: list[date]
    warnings: list[str]
    listed_domains: int = 0


def parse(data: bytes) -> ColdTurkeyParse:
    """Export bytes -> rows. Raises ColdTurkeyParseError for anything not observed.

    Duplicate rule for the same (normalised domain, day): identical minutes are
    kept once with a warning; different minutes are both dropped and the fact is
    returned as None (stored as unreadable), because picking one or summing
    them would be a guess about why the export repeated itself.
    """
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ColdTurkeyParseError(f"this file is not valid JSON ({exc.__class__.__name__})") from exc
    if not isinstance(doc, list):
        raise ColdTurkeyParseError("expected a list of chart series at the top level of the export")
    if not doc:
        raise ColdTurkeyParseError("the export contains no domains")

    readings: dict[tuple[str, date], list[tuple[str, float]]] = {}
    labels_seen: list[str] = []
    for i, series in enumerate(doc):
        if not isinstance(series, dict):
            raise ColdTurkeyParseError(f"series {i + 1} is not an object")
        missing = SERIES_KEYS - series.keys()
        if missing:
            raise ColdTurkeyParseError(f"series {i + 1} is missing {', '.join(sorted(missing))}")
        extra = set(series.keys()) - SERIES_KEYS
        if extra:
            raise ColdTurkeyParseError(
                f"series {i + 1} has fields this parser has never seen ({', '.join(sorted(extra))}); "
                "not importing numbers whose meaning may have changed")
        label = series["label"]
        if not isinstance(label, str) or not label.strip():
            raise ColdTurkeyParseError(f"series {i + 1} has no domain label")
        label = label.strip()
        if re.search(r"\s", label):
            if "blocked" in label.casefold():
                raise ColdTurkeyParseError(
                    f"this is the {label!r} chart, which counts blocks, not time. Export the Websites usage "
                    "statistics instead")
            raise ColdTurkeyParseError(f"{label!r} is not a website domain; this doesn't look like a website "
                                       "usage export")
        points = series["data"]
        if not isinstance(points, list) or not points:
            raise ColdTurkeyParseError(f"{label!r} has no data points")
        labels_seen.append(label)
        for point in points:
            day, minutes = _point(label, point)
            readings.setdefault((fact_key(label), day), []).append((label, minutes))

    rows: list[tuple[str, date, float | None]] = []
    warnings: list[str] = []
    for (domain_key, day), found in readings.items():
        label = found[0][0]
        if len(found) == 1:
            rows.append((label, day, found[0][1]))
            continue
        values = {m for _, m in found}
        if len(values) == 1:
            warnings.append(f"{label} appears {len(found)} times for {day}; the identical value was kept once.")
            rows.append((label, day, found[0][1]))
        else:
            shown = ", ".join(f"{m:g}" for _, m in found)
            warnings.append(f"{label} has conflicting values for {day} ({shown}); stored as unreadable, not guessed.")
            rows.append((label, day, None))
    rows.sort(key=lambda r: (r[1], r[0]))
    days = sorted({d for _, d, _ in rows})
    return ColdTurkeyParse(rows, days, warnings, len({fact_key(lbl) for lbl in labels_seen}))


def fact_key(label: str) -> str:
    """The domain a fact is about, after www/scheme normalisation but before X folding.

    `labels.normalise_key` folds x.com and twitter.com into one X source; they
    are still two domains in the export, so duplicates and item keys use this.
    """
    try:
        kind, key = labels.normalise_key(T.KIND_WEB_DOMAIN, label)
    except labels.LabelError as exc:
        raise ColdTurkeyParseError(f"{label!r} is not a usable domain") from exc
    if kind != T.KIND_X:
        return key
    raw = re.sub(r"^[a-z]+://", "", label.strip().casefold()).split("/")[0]
    return raw[4:] if raw.startswith("www.") else raw


def _point(label: str, point) -> tuple[date, float]:
    if not isinstance(point, list) or len(point) != 2:
        raise ColdTurkeyParseError(f"{label!r} has a data point that is not [timestamp, minutes]")
    ts, minutes = point
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ColdTurkeyParseError(f"{label!r} has a non-integer timestamp {ts!r}")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes):
        raise ColdTurkeyParseError(f"{label!r} has a non-numeric minutes value {minutes!r}")
    if minutes < 0 or minutes > 1440:
        raise ColdTurkeyParseError(f"{label!r} has an impossible daily value {minutes!r} minutes")
    try:
        instant = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ColdTurkeyParseError(f"{label!r} has an out-of-range timestamp {ts!r}") from exc
    if (instant.hour, instant.minute, instant.second, instant.microsecond) != (0, 0, 0, 0):
        # Observed: every timestamp is a UTC midnight standing for a local day.
        raise ColdTurkeyParseError(f"{label!r} has a timestamp that is not a day boundary ({ts!r})")
    return instant.date(), float(minutes)


# --- ingest --------------------------------------------------------------------------

def ingest(conn: sqlite3.Connection, clk: Clock, settings: Settings, data: bytes, *,
           filename: str | None = None, origin: str = ORIGIN_EXPORT) -> evidence.Outcome:
    """Store the export, then record one fact per listed domain per finished day."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown Cold Turkey origin {origin!r}")
    rel, sha = evidence.store_raw(settings, KIND, data, ".json")
    dup = evidence.find_import_by_sha(conn, KIND, sha)
    if dup is not None:
        return evidence.Outcome(dup["id"], dup["status"], 0, (), None, dup["period_start"], dup["period_end"],
                                duplicate_of=dup["id"])
    return _run(conn, clk, settings, rel, sha, data, filename=filename, received_at=clock.iso(clk.now()),
                origin=origin)


def reparse(conn: sqlite3.Connection, clk: Clock, settings: Settings, import_id: int) -> evidence.Outcome:
    """Re-read an earlier import's stored export into a new import.

    "Days not yet over" is judged against the original receive time, so a
    re-parse never starts recording a day that was incomplete in the file.
    """
    row = conn.execute("SELECT * FROM imports WHERE id = ? AND kind = ?", (import_id, KIND)).fetchone()
    if row is None:
        raise ValueError(f"no Cold Turkey import #{import_id}")
    meta = json.loads(row["meta"] or "{}")
    try:
        data = evidence.raw_file(settings, row["raw_path"]).read_bytes()
    except OSError as exc:
        rec = evidence.begin_import(conn, clk, kind=KIND, parser_version=PARSER_VERSION,
                                    raw_path=row["raw_path"], raw_sha256=row["raw_sha256"])
        msg = f"stored export for import #{import_id} could not be read: {type(exc).__name__}"
        evidence.fail_import(conn, rec, msg)
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error=msg)
    return _run(conn, clk, settings, row["raw_path"], row["raw_sha256"], data, filename=meta.get("filename"),
                received_at=meta.get("received_at") or row["received_at"], replaces=import_id,
                origin=meta.get("origin", ORIGIN_EXPORT))


def _run(conn, clk, settings, rel, sha, data, *, filename, received_at, replaces=None,
         origin=ORIGIN_EXPORT) -> evidence.Outcome:
    rec = evidence.begin_import(conn, clk, kind=KIND, parser_version=PARSER_VERSION, raw_path=rel, raw_sha256=sha)
    rec.meta.update({"filename": filename, "received_at": received_at, "origin": origin})
    if replaces is not None:
        rec.meta["reparse_of"] = replaces
    try:
        parsed = parse(data)
    except ColdTurkeyParseError as exc:
        evidence.fail_import(conn, rec, str(exc))
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error=str(exc))

    today = T.local_date(received_at, settings.tz)
    rec.warnings.extend(parsed.warnings)
    unfinished = sorted({d for _, d, _ in parsed.rows if d >= today})
    if unfinished:
        rec.warnings.append(
            f"{len(unfinished)} day(s) from {unfinished[0]} on hadn't finished when this was exported and were "
            "not recorded. Export again after the week ends.")
    rec.meta.update({
        "listed_domain_count": parsed.listed_domains,
        "unlisted_domains": (UNLISTED_NOTE if origin == ORIGIN_EXPORT
                             else "Read from this Mac's own Cold Turkey statistics: every domain is listed."),
        "days_in_file": [d.isoformat() for d in parsed.days],
        "days_not_recorded_unfinished": [d.isoformat() for d in unfinished],
        "machine": ("one desktop export; other machines are not included" if origin == ORIGIN_EXPORT
                    else "this Mac only; Cold Turkey does not sync between machines"),
    })
    written = 0
    recorded_days: list[date] = []
    try:
        if replaces is not None:
            conn.execute("DELETE FROM measurements WHERE import_id = ?", (replaces,))
        for label, day, minutes in parsed.rows:
            if day >= today:
                continue
            source = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, label)
            # item_key keeps x.com and twitter.com distinct facts under the one X source.
            item_key = fact_key(label) if source.kind == T.KIND_X else ""
            evidence.upsert_measurement(
                conn, rec, source_id=source.id, device=T.DEVICE_DESKTOP, provenance=T.PROV_EXPORT,
                period_start=day.isoformat(), period_end=day.isoformat(),
                minutes=minutes, status="ok" if minutes is not None else "unreadable",
                item_key=item_key, item_title=label, detail={"label": label, "origin": origin},
            )
            written += 1
            recorded_days.append(day)
        start = min(recorded_days).isoformat() if recorded_days else None
        end = max(recorded_days).isoformat() if recorded_days else None
        status = evidence.finish_import(conn, clk, settings, rec, period_start=start, period_end=end)
        return evidence.Outcome(rec.id, status, written, tuple(rec.warnings), None, start, end)
    except (labels.LabelError, evidence.EvidenceError) as exc:
        msg = f"could not record the export: {exc}"
        evidence.fail_import(conn, rec, msg)
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, tuple(rec.warnings), msg)
