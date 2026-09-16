"""report.py — funnels, allowance status, cost per keeper. DRBR Phase 2, Agent D.

Implements CONTRACTS.md Part II §9. The dashboard (Phase 3) renders these and
computes nothing of its own.

Five rules this module exists to enforce.

1. **Read-only.** Every function takes `(conn, clk, settings, ...)` and issues
   SELECTs only. Nothing here creates a week row, a source, or a label. A
   report that wrote while it read could change the figure it was reporting.

2. **Provenance travels with every number.** Every row names where its
   minutes came from (`drbr_types.PROV_*`). A parsed screenshot stays
   `screenshot_ocr` all the way to the page; it is never merged into a row
   that would let it borrow an API figure's confidence.

3. **The vault is counts, never minutes.** No function converts a capture
   into reading time. Cost per keeper divides *consumption* minutes by a
   *count* of kept captures; the capture itself contributes no minutes.

4. **No targets, scores or streaks.** Trend and comparison only. Cost per
   keeper is ranked, never judged against a threshold, and a group with no
   keepers has no price (`None`), never infinity and never zero.

5. **Gaps are stated, not absorbed.** Unmeasured items are counted,
   covered-elsewhere minutes are shown beside the totals they were kept out
   of, and `blind_spots` names what the system cannot see.

Labels are read through `labels.resolve` via `allowance` on every call, like
everywhere else. Nothing is cached.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import allowance
import clock
import db
import drbr_types as T
import evidence
from clock import Clock
from config import Settings

MIN_MS = allowance.MIN_MS

#: Which import kind produces each measurement provenance.
IMPORT_KIND_FOR_PROVENANCE = {
    T.PROV_PAGE: "youtube_history",
    T.PROV_SCREENSHOT: "screentime",
    T.PROV_EXPORT: "coldturkey",
}

#: The rows a week *should* have from weekly evidence, so an absent import
#: shows up as a row saying so rather than as nothing at all.
EXPECTED_WEEKLY = (
    ("youtube_history", T.GROUP_YOUTUBE, T.PROV_PAGE, T.DEVICE_ACCOUNT, "YouTube history"),
    ("screentime", T.GROUP_IPHONE, T.PROV_SCREENSHOT, T.DEVICE_IPHONE, "Screen Time"),
    ("coldturkey", T.GROUP_DESKTOP, T.PROV_EXPORT, T.DEVICE_DESKTOP, "Cold Turkey"),
)

IMPORT_MISSING = "missing"

#: Standing per-group caveats shown on the cost-per-keeper table.
GROUP_BLIND_SPOTS: dict[str, tuple[str, ...]] = {
    T.GROUP_PODCASTS: (
        "Only captured episodes are polled; podcast listening outside the queue is invisible.",
    ),
    T.GROUP_YOUTUBE: (
        "Rewatches overwrite progress and undercount; dates have no time of day.",
    ),
    T.GROUP_X: (
        "Minutes come from Screen Time (OCR) and Cold Turkey (top domains only); "
        "keepers from vault captures of X posts and articles.",
    ),
    T.GROUP_IPHONE: (
        "Parsed from screenshots (OCR), lower confidence.",
        "No keeper path: vault captures are not attributed to iPhone apps.",
    ),
    T.GROUP_DESKTOP: (
        "Cold Turkey lists only its top domains; only one desktop machine is exported.",
        "No keeper path: vault captures are not attributed to web domains.",
    ),
    T.GROUP_WEB: ("No minutes source: articles are counted as keepers only.",),
    T.GROUP_OTHER: ("No minutes source: captures with no attributable source.",),
}


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceTotal:
    """One (group, provenance, device) row of a week's consumption.

    `minutes` counts every label, excludes covered-elsewhere rows (shown in
    `covered_excluded_min`) and is `None` when the week's evidence for this
    row never arrived — `import_status == 'missing'` — which is not zero.
    `import_status` is `None` for API-polled Spotify, which has no imports.
    """

    group: str
    provenance: str
    device: str
    minutes: float | None
    unmeasured_items: int
    covered_excluded_min: float
    import_status: str | None


@dataclass(frozen=True)
class WeekSplit:
    week_start: str
    wasteful_min: float
    worthwhile_min: float


@dataclass(frozen=True)
class AllowanceStatus:
    """The current allowance week.

    `spent_wasteful_min` is pooled wasteful consumption across sources and
    `remaining_min` is what is left of the effective allowance after it —
    consumption, the figure debt is computed from. `promoted_min` is POP's
    promoted commitment, reported alongside and never mixed in.
    """

    week_start: str
    allowance_min: int
    debt_min: int
    effective_min: int
    spent_wasteful_min: float
    remaining_min: int
    promoted_min: int


@dataclass(frozen=True)
class GatedRow:
    group: str
    gated_min: float
    ungated_min: float
    share_ungated: float | None       # None when nothing was consumed


@dataclass(frozen=True)
class KeeperRow:
    group: str
    minutes: float | None             # None: the group has no minutes source at all
    keepers: int
    minutes_per_keeper: float | None  # None: no keepers yet (or no minutes source)
    provenances: tuple[str, ...]
    blind_spots: tuple[str, ...]


@dataclass(frozen=True)
class VaultFunnel:
    """Counts only. `captured` is the denominator: unprocessed + kept.

    `excluded` (no file carries the id, or only in .trash) is reported but
    never in the denominator. Captures not yet seen by any scan are not
    counted anywhere until a scan decides their outcome.
    """

    captured: int
    unprocessed: int
    kept: int
    excluded: int
    edited_kept: int


@dataclass(frozen=True)
class Funnels:
    pop: db.FunnelStats
    vault: VaultFunnel


@dataclass(frozen=True)
class RecordRow:
    """One raw consumption fact, as the record browser shows it."""

    record_type: str                  # 'listening' | 'measurement'
    day: str                          # local date
    group: str
    source_kind: str
    source: str                       # display name
    item: str | None
    minutes: float | None
    status: str
    label: str
    label_basis: str
    provenance: str
    device: str
    covered_elsewhere: str | None
    import_id: int | None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _current_week_start(clk: Clock, settings: Settings) -> str:
    return clock.iso(clock.week_start_for(clk.now(), settings.tz))


def _previous_week_start(week_start: str, settings: Settings) -> str:
    return clock.iso(clock.week_start_for(clock.parse(week_start) - timedelta(seconds=1), settings.tz))


def _local_midnight(d: date, tz: str) -> str:
    return clock.iso(datetime.combine(d, time(0), tzinfo=ZoneInfo(tz)))


def _since_bounds(since: str | None, tz: str) -> tuple[str | None, str | None]:
    """-> (instant, local date) for a `since` given as either form."""
    if since is None:
        return None, None
    if len(since) == 10:
        return _local_midnight(T.parse_date(since), tz), since
    return clock.iso(clock.parse(since)), T.iso_date(T.local_date(since, tz))


def _covering_import(conn: sqlite3.Connection, kind: str, start: str, end: str) -> sqlite3.Row | None:
    """The latest ok/partial import of `kind` whose period overlaps [start, end]."""
    return conn.execute(
        "SELECT * FROM imports WHERE kind = ? AND status IN ('ok','partial') "
        "AND period_start IS NOT NULL AND period_start <= ? AND period_end >= ? "
        "ORDER BY id DESC LIMIT 1",
        (kind, end, start),
    ).fetchone()


def _last_complete_measurement_week(clk: Clock, settings: Settings) -> T.MeasurementWeek:
    today = T.local_date(clk.now(), settings.tz)
    return T.measurement_week(today - timedelta(days=7))


def _latest_vault_outcomes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The latest observation per capture, joined to its capture."""
    return conn.execute(
        """
        SELECT c.capture_id, c.keeper_group, c.captured_at, o.outcome, o.edited
          FROM vault_captures c
          JOIN vault_observations o ON o.id = (
                SELECT id FROM vault_observations
                 WHERE capture_id = c.capture_id
                 ORDER BY scanned_at DESC, id DESC LIMIT 1)
        """
    ).fetchall()


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def diagnostic_banner(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> evidence.Diagnostic:
    """The countdown shown on every page."""
    return evidence.diagnostic(conn, clk, settings)


def weekly_totals(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                  week_start: str) -> list[SourceTotal]:
    """Consumption in one allowance week, one row per (group, provenance, device)."""
    acc: dict[tuple[str, str, str], dict] = {}

    def row(key):
        return acc.setdefault(key, {"minutes": 0.0, "unmeasured_items": 0, "covered_excluded_min": 0.0})

    for r in allowance._spotify_rows(conn, settings, week_start):
        row((T.GROUP_PODCASTS, T.PROV_API, T.DEVICE_ACCOUNT))["minutes"] += int(r["delta_ms"]) / MIN_MS

    for r in allowance._measurement_rows(conn, settings, week_start):
        b = row((T.report_group(r["kind"], r["device"]), r["provenance"], r["device"]))
        if T.COVERED_ELSEWHERE.get((r["kind"], r["source_key"])):
            if r["status"] == "ok":
                b["covered_excluded_min"] += float(r["minutes"])
        elif r["status"] != "ok":
            b["unmeasured_items"] += 1
        else:
            b["minutes"] += float(r["minutes"])

    mweek = T.measurement_week_for_allowance_week(week_start, settings.tz)
    out: list[SourceTotal] = []
    for (group, provenance, device), v in acc.items():
        status = None
        kind = IMPORT_KIND_FOR_PROVENANCE.get(provenance)
        if kind is not None:
            imp = _covering_import(conn, kind, mweek.start, mweek.end)
            status = imp["status"] if imp else IMPORT_MISSING
        out.append(SourceTotal(group, provenance, device, v["minutes"], v["unmeasured_items"],
                               v["covered_excluded_min"], status))

    present = {(t.group, t.provenance, t.device) for t in out}
    for kind, group, provenance, device, _name in EXPECTED_WEEKLY:
        if (group, provenance, device) in present:
            continue
        imp = _covering_import(conn, kind, mweek.start, mweek.end)
        if imp is None:
            out.append(SourceTotal(group, provenance, device, None, 0, 0.0, IMPORT_MISSING))
        else:
            # Evidence arrived and simply held no rows for this week.
            out.append(SourceTotal(group, provenance, device, 0.0, 0, 0.0, imp["status"]))

    order = {g: i for i, g in enumerate(T.REPORT_GROUPS)}
    prov_order = {p: i for i, p in enumerate(T.PROVENANCES)}
    out.sort(key=lambda t: (order.get(t.group, 99), prov_order.get(t.provenance, 99), t.device))
    return out


def label_split(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                weeks: int = 8) -> list[WeekSplit]:
    """Wasteful vs worthwhile per allowance week, oldest first, ending with the current week."""
    starts = [_current_week_start(clk, settings)]
    for _ in range(max(0, weeks - 1)):
        starts.append(_previous_week_start(starts[-1], settings))
    out = []
    for start in reversed(starts):
        pooled = allowance.pooled_minutes(conn, settings, start)
        out.append(WeekSplit(start, pooled.wasteful_min, pooled.worthwhile_min))
    return out


def allowance_status(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> AllowanceStatus:
    """Spent, remaining and debt carried for the week containing now.

    Read-only: if no process has opened this week's row yet, the week is
    described as rollover would open it (fresh allowance, debt from the
    previous row's pooled wasteful minutes) without writing it.
    """
    start = _current_week_start(clk, settings)
    row = conn.execute("SELECT * FROM weeks WHERE week_start = ?", (start,)).fetchone()
    if row is not None:
        week = db._to_week(row)
    else:
        prior = conn.execute(
            "SELECT * FROM weeks WHERE week_start < ? ORDER BY week_start DESC LIMIT 1", (start,)
        ).fetchone()
        debt = 0
        if prior is not None:
            closed = db._to_week(prior)
            debt = allowance.debt_for(closed, allowance.pooled_minutes(conn, settings, closed.week_start))
        week = db.Week(start, settings.weekly_allowance_min, debt, 0, 0)

    spent = allowance.pooled_minutes(conn, settings, start).wasteful_min
    effective = week.effective_allowance_min
    return AllowanceStatus(
        week_start=start,
        allowance_min=week.allowance_min,
        debt_min=week.debt_min,
        effective_min=effective,
        spent_wasteful_min=spent,
        remaining_min=max(0, effective - allowance.whole_minutes(spent)),
        promoted_min=week.promoted_min,
    )


def gated_vs_ungated(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                     week_start: str) -> list[GatedRow]:
    """How much consumption passed through the queue versus bypassed it, per group."""
    split = allowance.gated_split(conn, settings, week_start)
    out = []
    for group in T.REPORT_GROUPS:
        s = split[group]
        total = s.gated_min + s.ungated_min
        out.append(GatedRow(group, s.gated_min, s.ungated_min,
                            (s.ungated_min / total) if total > 0 else None))
    return out


def cost_per_keeper(conn: sqlite3.Connection, clk: Clock, settings: Settings, *,
                    since: str | None = None) -> list[KeeperRow]:
    """Minutes per kept vault capture, one row per group, cheapest first, no price last.

    Minutes are every label's consumption (excluding covered-elsewhere rows),
    because a keeper is what the time *yielded*, whatever it was labelled.
    Keepers are captures whose latest scan outcome is `kept`. `since` is a
    local date or an instant; `None` means all time.
    """
    since_instant, since_day = _since_bounds(since, settings.tz)

    minutes: dict[str, float] = {g: 0.0 for g in T.REPORT_GROUPS}
    provenances: dict[str, set[str]] = {g: set() for g in T.REPORT_GROUPS}

    sql = ("SELECT l.delta_ms FROM listening l JOIN episodes e ON e.spotify_id = l.spotify_id "
           "WHERE l.delta_ms > 0 AND e.platform = 'spotify'")
    params: list = []
    if since_instant is not None:
        sql += " AND l.polled_at >= ?"
        params.append(since_instant)
    for r in conn.execute(sql, params).fetchall():
        minutes[T.GROUP_PODCASTS] += int(r["delta_ms"]) / MIN_MS
        provenances[T.GROUP_PODCASTS].add(T.PROV_API)

    sql = ("SELECT m.minutes, m.provenance, m.device, s.kind, s.source_key FROM measurements m "
           "JOIN sources s ON s.id = m.source_id WHERE m.status = 'ok'")
    params = []
    if since_day is not None:
        sql += " AND m.period_start >= ?"
        params.append(since_day)
    for r in conn.execute(sql, params).fetchall():
        if T.COVERED_ELSEWHERE.get((r["kind"], r["source_key"])):
            continue
        group = T.report_group(r["kind"], r["device"])
        minutes[group] += float(r["minutes"])
        provenances[group].add(r["provenance"])

    keepers: dict[str, int] = {}
    for r in _latest_vault_outcomes(conn):
        if r["outcome"] != "kept":
            continue
        if since_instant is not None and r["captured_at"] < since_instant:
            continue
        keepers[r["keeper_group"]] = keepers.get(r["keeper_group"], 0) + 1

    prov_order = {p: i for i, p in enumerate(T.PROVENANCES)}
    rows: list[KeeperRow] = []
    groups = list(T.REPORT_GROUPS) + [g for g in (T.GROUP_WEB, T.GROUP_OTHER) if keepers.get(g)]
    for group in groups:
        has_minutes_source = group in T.REPORT_GROUPS
        mins = minutes[group] if has_minutes_source else None
        kept = keepers.get(group, 0)
        provs = set(provenances.get(group, set()))
        if group in T.KEEPER_GROUPS:
            provs.add(T.PROV_VAULT)
        price = (mins / kept) if (mins is not None and kept > 0) else None
        rows.append(KeeperRow(
            group=group,
            minutes=mins,
            keepers=kept,
            minutes_per_keeper=price,
            provenances=tuple(sorted(provs, key=lambda p: prov_order.get(p, 99))),
            blind_spots=GROUP_BLIND_SPOTS.get(group, ()),
        ))

    group_order = {g: i for i, g in enumerate(groups)}
    rows.sort(key=lambda r: (r.minutes_per_keeper is None,
                             r.minutes_per_keeper if r.minutes_per_keeper is not None else 0.0,
                             group_order[r.group]))
    return rows


def funnels(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> Funnels:
    """POP's capture funnel, and capture → kept in the vault (excluded out of the denominator)."""
    unprocessed = kept = excluded = edited_kept = 0
    for r in _latest_vault_outcomes(conn):
        if r["outcome"] == "kept":
            kept += 1
            if r["edited"]:
                edited_kept += 1
        elif r["outcome"] == "unprocessed":
            unprocessed += 1
        else:
            excluded += 1
    return Funnels(
        pop=db.funnel_stats(conn, clk, settings),
        vault=VaultFunnel(unprocessed + kept, unprocessed, kept, excluded, edited_kept),
    )


def blind_spots(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[str]:
    """What this diagnostic cannot see, stated rather than silently absorbed."""
    out = [
        "Podcast listening outside the queue is invisible: only captured episodes are polled.",
        "Cold Turkey lists only its top domains; unlisted domains are unmeasured, not zero.",
        "Only one desktop machine is exported to Cold Turkey; a second machine would be unmeasured.",
        "YouTube rewatches overwrite progress and undercount; history dates have no time of day or sittings.",
        "Screen Time figures are parsed from screenshots (OCR) and carry lower confidence than API figures.",
        "iPhone YouTube/Spotify app and youtube.com/open.spotify.com minutes are excluded as covered "
        "elsewhere; Spotify music is not measured at all.",
        "Vault captures are counts, never minutes: reading time is not measured.",
    ]

    missing = conn.execute(
        "SELECT m.status AS status, COUNT(*) AS n FROM measurements m JOIN sources s ON s.id = m.source_id "
        "WHERE s.kind = 'youtube_channel' AND m.status != 'ok' GROUP BY m.status"
    ).fetchall()
    for r in missing:
        what = {"progress_missing": "no readable progress",
                "duration_missing": "no duration from the API"}.get(r["status"], "unreadable data")
        out.append(f"{r['n']} YouTube video(s) with {what} are left out of minutes (not counted as zero).")

    last = _last_complete_measurement_week(clk, settings)
    diag = evidence.diagnostic(conn, clk, settings)
    first = T.measurement_week(T.parse_date(diag.start)).sunday if diag.started else last.sunday
    names = {kind: name for kind, _g, _p, _d, name in EXPECTED_WEEKLY}
    sunday = first
    while sunday <= last.sunday:
        week = T.measurement_week(sunday)
        for kind in ("screentime", "coldturkey", "youtube_history"):
            if _covering_import(conn, kind, week.start, week.end) is None:
                out.append(f"{names[kind]} for the week {week.start} to {week.end} has not been received.")
        sunday += timedelta(days=7)
    return out


def records(conn: sqlite3.Connection, clk: Clock, settings: Settings, *, group: str | None = None,
            label: str | None = None, start: str | None = None, end: str | None = None,
            limit: int = 500) -> list[RecordRow]:
    """Raw consumption facts, newest first, filterable by group, label and local date range.

    Covered-elsewhere rows are included and say so; unmeasured rows are
    included with `minutes=None`. Vault captures are not minute records and
    appear in `funnels` instead.
    """
    if group is not None and group not in T.REPORT_GROUPS:
        raise ValueError(f"unknown group {group!r}")
    if label is not None and label not in T.LABELS:
        raise ValueError(f"unknown label {label!r}")
    tz = settings.tz
    out: list[RecordRow] = []

    if group in (None, T.GROUP_PODCASTS):
        sql = """
            SELECT l.id, l.spotify_id, l.polled_at, l.delta_ms, e.title, e.show, e.platform, e.source_key
              FROM listening l JOIN episodes e ON e.spotify_id = l.spotify_id
             WHERE l.delta_ms > 0 AND e.platform = 'spotify'
        """
        params: list = []
        if start is not None:
            sql += " AND l.polled_at >= ?"
            params.append(_local_midnight(T.parse_date(start), tz))
        if end is not None:
            sql += " AND l.polled_at < ?"
            params.append(_local_midnight(T.parse_date(end) + timedelta(days=1), tz))
        for r in conn.execute(sql, params).fetchall():
            resolved = allowance.spotify_label(conn, r)
            out.append(RecordRow(
                record_type="listening", day=T.iso_date(T.local_date(r["polled_at"], tz)),
                group=T.GROUP_PODCASTS, source_kind=T.KIND_PODCAST_SHOW, source=r["show"],
                item=r["title"], minutes=int(r["delta_ms"]) / MIN_MS, status="ok",
                label=resolved.label, label_basis=resolved.basis, provenance=T.PROV_API,
                device=T.DEVICE_ACCOUNT, covered_elsewhere=None, import_id=None,
            ))

    sql = """
        SELECT m.*, s.kind AS kind, s.source_key AS source_key, s.display_name AS display_name
          FROM measurements m JOIN sources s ON s.id = m.source_id WHERE 1 = 1
    """
    params = []
    if start is not None:
        sql += " AND m.period_start >= ?"
        params.append(start)
    if end is not None:
        sql += " AND m.period_start <= ?"
        params.append(end)
    for r in conn.execute(sql, params).fetchall():
        g = T.report_group(r["kind"], r["device"])
        if group is not None and g != group:
            continue
        resolved = allowance.measurement_label(conn, r)
        out.append(RecordRow(
            record_type="measurement", day=r["period_start"], group=g, source_kind=r["kind"],
            source=r["display_name"], item=r["item_title"] or (r["item_key"] or None),
            minutes=None if r["minutes"] is None else float(r["minutes"]), status=r["status"],
            label=resolved.label, label_basis=resolved.basis, provenance=r["provenance"],
            device=r["device"], covered_elsewhere=T.COVERED_ELSEWHERE.get((r["kind"], r["source_key"])),
            import_id=r["import_id"],
        ))

    if label is not None:
        out = [r for r in out if r.label == label]
    out.sort(key=lambda r: (r.day, r.provenance, r.source, r.item or ""), reverse=True)
    return out[:limit]
