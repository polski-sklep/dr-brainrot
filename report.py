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

import json
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

IMPORT_MISSING = "missing"          # no rows and no import covering the week
IMPORT_INCOMPLETE = "incomplete"    # rows exist, but no import covers the whole week yet

#: Standing per-group caveats shown on the cost-per-keeper table.
GROUP_BLIND_SPOTS: dict[str, tuple[str, ...]] = {
    T.GROUP_PODCASTS: (
        "Only captured episodes are polled; podcast listening outside the queue is invisible.",
    ),
    T.GROUP_YOUTUBE: (
        "Rewatches overwrite progress and undercount; dates have no time of day.",
    ),
    T.GROUP_X: (
        "Minutes come from Screen Time (OCR) and Cold Turkey (this Mac's browser only); "
        "keepers from vault captures of X posts and articles.",
    ),
    T.GROUP_IPHONE: (
        "Parsed from screenshots (OCR), lower confidence.",
        "No keeper path: vault captures are not attributed to iPhone apps.",
    ),
    T.GROUP_DESKTOP: (
        "Cold Turkey covers this Mac's browsers only; a manual export lists only its top domains.",
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

    `import_id` is the import whose status is reported — the one covering this
    week, not merely the latest of its kind — so a "partial" links to the
    warnings that made *this* week partial. `covered_reasons` says why each
    excluded minute was excluded, computed from the same rows as
    `covered_excluded_min` so the two can never disagree.
    """

    group: str
    provenance: str
    device: str
    minutes: float | None
    unmeasured_items: int
    covered_excluded_min: float
    import_status: str | None
    import_id: int | None = None
    covered_reasons: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class WeekSplit:
    """One bar of the wasteful/worthwhile chart.

    `screenshot_ocr_min` is how much of the bar came from parsed screenshots,
    so a chart that stacks OCR minutes on API minutes can say so.
    """

    week_start: str
    wasteful_min: float
    worthwhile_min: float
    screenshot_ocr_min: float = 0.0


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
    spent_screenshot_ocr_min: float = 0.0     # the parsed-screenshot part of spent_wasteful_min


@dataclass(frozen=True)
class GatedRow:
    """Queue-gated vs ungated consumption for one group.

    `wasteful_ungated_min` is the brief's "reflex rather than choice" figure:
    wasteful consumption that never passed through the queue. It is a subset
    of `ungated_min` and is carried through so the dashboard can surface it.
    `provenances` names where the group's counted minutes came from this week.
    """

    group: str
    gated_min: float
    ungated_min: float
    share_ungated: float | None       # None when nothing was consumed
    wasteful_ungated_min: float = 0.0
    provenances: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportSummary:
    """The latest stored import of one kind, read back verbatim. `id` None: none yet."""

    kind: str
    id: int | None = None
    status: str | None = None
    received_at: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    parser_version: str | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()


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


def _covering_import(conn: sqlite3.Connection, settings: Settings, kind: str,
                     week: T.MeasurementWeek) -> sqlite3.Row | None:
    """The latest ok/partial import of `kind` that covers a measurement week.

    * YouTube history: import periods are watch-date ranges, not weeks, and a
      sync walks back to the previous cursor. So any ok/partial sync
      *received after* the week's Saturday (local) covers that week.
    * Screen Time / Cold Turkey: the import's period overlaps the week. A
      "This Week" or all-devices Screen Time capture has a NULL period and
      writes nothing, so it never counts as coverage. A Cold Turkey export
      that skipped days on/after its export date is 'partial' and still
      covers the days it holds.
    """
    if kind == "youtube_history":
        after_saturday = _local_midnight(week.saturday + timedelta(days=1), settings.tz)
        return conn.execute(
            "SELECT * FROM imports WHERE kind = ? AND status IN ('ok','partial') "
            "AND received_at >= ? ORDER BY id DESC LIMIT 1",
            (kind, after_saturday),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM imports WHERE kind = ? AND status IN ('ok','partial') "
        "AND period_start IS NOT NULL AND period_end IS NOT NULL "
        "AND period_start <= ? AND period_end >= ? "
        "ORDER BY id DESC LIMIT 1",
        (kind, week.end, week.start),
    ).fetchone()


def window_start(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> str | None:
    """The local date report windows start at, or None for "all data".

    Data before the diagnostic start is not diagnostic data. The start is
    `evidence.diagnostic(...).start` once set; before the first import it
    falls back to `settings.go_live` when configured (read defensively: the
    field is new on the integration branch).
    """
    diag = evidence.diagnostic(conn, clk, settings)
    if diag.started and diag.start:
        return diag.start
    return getattr(settings, "go_live", "") or None


def _clamp_start(requested: str | None, floor: str | None) -> str | None:
    """The later of a requested local start date and the diagnostic window start."""
    if floor is None:
        return requested
    if requested is None:
        return floor
    return max(requested, floor)


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
        return acc.setdefault(key, {"minutes": 0.0, "unmeasured_items": 0, "covered_excluded_min": 0.0,
                                    "reasons": {}})

    for r in allowance._spotify_rows(conn, settings, week_start):
        row((T.GROUP_PODCASTS, T.PROV_API, T.DEVICE_ACCOUNT))["minutes"] += int(r["delta_ms"]) / MIN_MS

    for r in allowance._measurement_rows(conn, settings, week_start):
        b = row((T.report_group(r["kind"], r["device"]), r["provenance"], r["device"]))
        reason = T.COVERED_ELSEWHERE.get((r["kind"], r["source_key"]))
        if reason:
            if r["status"] == "ok":
                b["covered_excluded_min"] += float(r["minutes"])
                b["reasons"][reason] = b["reasons"].get(reason, 0.0) + float(r["minutes"])
        elif r["status"] != "ok":
            b["unmeasured_items"] += 1
        else:
            b["minutes"] += float(r["minutes"])

    mweek = T.measurement_week_for_allowance_week(week_start, settings.tz)
    out: list[SourceTotal] = []
    for (group, provenance, device), v in acc.items():
        status = import_id = None
        kind = IMPORT_KIND_FOR_PROVENANCE.get(provenance)
        if kind is not None:
            imp = _covering_import(conn, settings, kind, mweek)
            # Rows exist but no import covers the whole week yet (e.g. a
            # YouTube sync made mid-week): the figure is honest but incomplete.
            status = imp["status"] if imp else IMPORT_INCOMPLETE
            import_id = imp["id"] if imp else None
        out.append(SourceTotal(group, provenance, device, v["minutes"], v["unmeasured_items"],
                               v["covered_excluded_min"], status, import_id,
                               tuple(sorted(v["reasons"].items()))))

    present = {(t.group, t.provenance, t.device) for t in out}
    for kind, group, provenance, device, _name in EXPECTED_WEEKLY:
        if (group, provenance, device) in present:
            continue
        imp = _covering_import(conn, settings, kind, mweek)
        if imp is None:
            out.append(SourceTotal(group, provenance, device, None, 0, 0.0, IMPORT_MISSING))
        else:
            # Evidence arrived and simply held no rows for this week.
            out.append(SourceTotal(group, provenance, device, 0.0, 0, 0.0, imp["status"], imp["id"]))

    order = {g: i for i, g in enumerate(T.REPORT_GROUPS)}
    prov_order = {p: i for i, p in enumerate(T.PROVENANCES)}
    out.sort(key=lambda t: (order.get(t.group, 99), prov_order.get(t.provenance, 99), t.device))
    return out


def label_split(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                weeks: int = 8) -> list[WeekSplit]:
    """Wasteful vs worthwhile per allowance week, oldest first, ending with the current week.

    Weeks that end before the diagnostic window start are left out: they are
    not diagnostic data, and showing them as empty bars would read as zero.
    """
    floor = window_start(conn, clk, settings)
    starts = [_current_week_start(clk, settings)]
    for _ in range(max(0, weeks - 1)):
        starts.append(_previous_week_start(starts[-1], settings))
    out = []
    for start in reversed(starts):
        if floor is not None and T.measurement_week_for_allowance_week(start, settings.tz).end < floor:
            continue
        pooled = allowance.pooled_minutes(conn, settings, start)
        out.append(WeekSplit(start, pooled.wasteful_min, pooled.worthwhile_min, pooled.screenshot_ocr_min))
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

    pooled = allowance.pooled_minutes(conn, settings, start)
    spent = pooled.wasteful_min
    effective = week.effective_allowance_min
    return AllowanceStatus(
        week_start=start,
        allowance_min=week.allowance_min,
        debt_min=week.debt_min,
        effective_min=effective,
        spent_wasteful_min=spent,
        remaining_min=max(0, effective - allowance.whole_minutes(spent)),
        promoted_min=week.promoted_min,
        spent_screenshot_ocr_min=pooled.screenshot_ocr_wasteful_min,
    )


def gated_vs_ungated(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                     week_start: str) -> list[GatedRow]:
    """How much consumption passed through the queue versus bypassed it, per group."""
    split = allowance.gated_split(conn, settings, week_start)
    prov_order = {p: i for i, p in enumerate(T.PROVENANCES)}
    out = []
    for group in T.REPORT_GROUPS:
        s = split[group]
        total = s.gated_min + s.ungated_min
        out.append(GatedRow(group, s.gated_min, s.ungated_min,
                            (s.ungated_min / total) if total > 0 else None,
                            s.wasteful_ungated_min,
                            tuple(sorted(s.provenances, key=lambda p: prov_order.get(p, 99)))))
    return out


def latest_imports(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[ImportSummary]:
    """The most recent import of every kind (in `IMPORT_KINDS` order), failures included.

    Read-only and verbatim: status, error and warnings exactly as stored, so a
    failed import is as visible on the dashboard as it was in the bot reply.
    """
    out: list[ImportSummary] = []
    for kind in T.IMPORT_KINDS:
        row = conn.execute("SELECT * FROM imports WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        if row is None:
            out.append(ImportSummary(kind))
            continue
        out.append(ImportSummary(kind, row["id"], row["status"], row["received_at"], row["period_start"],
                                 row["period_end"], row["parser_version"], row["error"],
                                 _stored_warnings(row["warnings"])))
    return out


def _stored_warnings(raw: str | None) -> tuple[str, ...]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return ("(stored warnings could not be read)",)
    if not isinstance(value, list):
        return ("(stored warnings could not be read)",)
    return tuple(str(w) for w in value)


def import_detail(conn: sqlite3.Connection, clk: Clock, settings: Settings, import_id: int) -> ImportSummary | None:
    """One stored import by id, verbatim (what a week's "partial" links to)."""
    row = conn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
    if row is None:
        return None
    return ImportSummary(row["kind"], row["id"], row["status"], row["received_at"], row["period_start"],
                         row["period_end"], row["parser_version"], row["error"], _stored_warnings(row["warnings"]))


def cost_per_keeper(conn: sqlite3.Connection, clk: Clock, settings: Settings, *,
                    since: str | None = None) -> list[KeeperRow]:
    """Minutes per kept vault capture, one row per group, cheapest first, no price last.

    Minutes are every label's consumption (excluding covered-elsewhere rows),
    because a keeper is what the time *yielded*, whatever it was labelled.
    Keepers are captures whose latest scan outcome is `kept`. `since` is a
    local date or an instant; `None` means the whole diagnostic window, and an
    earlier `since` is clamped to the window start. A measurement counts when
    its period ends on or after the start (a Screen Time week that opens the
    window counts whole; it cannot be split).
    """
    floor = window_start(conn, clk, settings)
    if since is not None and len(since) != 10:
        since = T.iso_date(T.local_date(since, settings.tz))
    since_instant, since_day = _since_bounds(_clamp_start(since, floor), settings.tz)

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
        sql += " AND m.period_end >= ?"
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
    """POP's capture funnel, and capture → kept in the vault (excluded out of the denominator).

    Both count captures made from the diagnostic window start onwards.
    """
    floor = window_start(conn, clk, settings)
    since = _local_midnight(T.parse_date(floor), settings.tz) if floor else None
    unprocessed = kept = excluded = edited_kept = 0
    for r in _latest_vault_outcomes(conn):
        if since is not None and r["captured_at"] < since:
            continue
        if r["outcome"] == "kept":
            kept += 1
            if r["edited"]:
                edited_kept += 1
        elif r["outcome"] == "unprocessed":
            unprocessed += 1
        else:
            excluded += 1
    return Funnels(
        pop=db.funnel_stats(conn, clk, settings, since=since),
        vault=VaultFunnel(unprocessed + kept, unprocessed, kept, excluded, edited_kept),
    )


def blind_spots(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[str]:
    """What this diagnostic cannot see, stated rather than silently absorbed."""
    out = [
        "Podcast listening outside the queue is invisible: only captured episodes are polled.",
        *_coldturkey_blind_spots(conn),
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
    floor = window_start(conn, clk, settings)
    first = T.measurement_week(T.parse_date(floor)).sunday if floor else last.sunday
    names = {kind: name for kind, _g, _p, _d, name in EXPECTED_WEEKLY}
    sunday = first
    while sunday <= last.sunday:
        week = T.measurement_week(sunday)
        for kind in ("screentime", "coldturkey", "youtube_history"):
            if _covering_import(conn, settings, kind, week) is None:
                out.append(f"{names[kind]} for the week {week.start} to {week.end} has not been received.")
        sunday += timedelta(days=7)
    return out


def _coldturkey_blind_spots(conn: sqlite3.Connection) -> list[str]:
    """Cold Turkey's caveat depends on how its minutes arrived (DECISIONS DR5.1).

    The daily read of this Mac's own statistics lists every domain; a manual
    export lists only the chart's top domains. Claim the smaller blind spot
    only when every Cold Turkey import that wrote minutes came from the
    automatic read.
    """
    origins = {
        (json.loads(r["meta"] or "{}").get("origin") or "export")
        for r in conn.execute("SELECT meta FROM imports WHERE kind = 'coldturkey' AND status != 'failed'")
    }
    spots = ["Cold Turkey measures this Mac only; Cold Turkey does not sync, so another computer would be "
             "unmeasured."]
    if origins == {"local_db"}:
        spots.append("Cold Turkey minutes are read from this Mac's own statistics (hostnames only), so every "
                     "visited domain is listed.")
    else:
        spots.append("Cold Turkey manual exports list only the chart's top domains; unlisted domains are "
                     "unmeasured, not zero.")
    return spots


def records(conn: sqlite3.Connection, clk: Clock, settings: Settings, *, group: str | None = None,
            label: str | None = None, start: str | None = None, end: str | None = None,
            limit: int = 500) -> list[RecordRow]:
    """Raw consumption facts, newest first, filterable by group, label and local date range.

    Covered-elsewhere rows are included and say so; unmeasured rows are
    included with `minutes=None`. Vault captures are not minute records and
    appear in `funnels` instead. Several rows may share a source and day (X
    from Cold Turkey is one row per domain); each is its own record.

    `start` is clamped to the diagnostic window start. A measurement matches
    the date range when its period overlaps it, so a Screen Time week shows
    up for any day inside it.
    """
    if group is not None and group not in T.REPORT_GROUPS:
        raise ValueError(f"unknown group {group!r}")
    if label is not None and label not in T.LABELS:
        raise ValueError(f"unknown label {label!r}")
    tz = settings.tz
    start = _clamp_start(start, window_start(conn, clk, settings))
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
        sql += " AND m.period_end >= ?"
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
