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

DRBR measures brain-rot content only (DECISIONS DR5.5). Minutes on sources
labelled `tool` (not content) are kept out of wasteful/worthwhile totals, the
label split, allowance status and cost per keeper, and are reported on their
own (`WeekSplit.tool_min`, `SourceTotal.tool_min`) so the label can never hide
consumption.
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
    T.PROV_APP_TRACKER: "qbserve",
    T.PROV_BROWSER_SYNC: "brave_sync",
    T.PROV_AUDIBLE_STATS: "audible",
}

#: The rows a week *should* have from weekly evidence, so an absent import
#: shows up as a row saying so rather than as nothing at all.
EXPECTED_WEEKLY = (
    ("youtube_history", T.GROUP_YOUTUBE, T.PROV_PAGE, T.DEVICE_ACCOUNT, "YouTube history"),
    ("screentime", T.GROUP_IPHONE, T.PROV_SCREENSHOT, T.DEVICE_IPHONE, "Screen Time"),
    ("coldturkey", T.GROUP_DESKTOP, T.PROV_EXPORT, T.DEVICE_DESKTOP, "Cold Turkey"),
    # Expected only while DRBR_QBSERVE_DB is configured (see weekly_totals): unconfigured,
    # desktop apps are deliberately unmeasured, not an import the user owes.
    ("qbserve", T.GROUP_DESKTOP_APPS, T.PROV_APP_TRACKER, T.DEVICE_DESKTOP, "Qbserve"),
    # Expected only while he is signed in to Audible (DR5.28): signed out,
    # audiobooks are deliberately unmeasured, an option and not a gap.
    ("audible", T.GROUP_AUDIOBOOKS, T.PROV_AUDIBLE_STATS, T.DEVICE_ACCOUNT, "Audible"),
)


def expects(kind: str, settings: Settings) -> bool:
    """Is an import of `kind` expected at all, given how this Mac is set up?

    Qbserve only while `DRBR_QBSERVE_DB` is configured, Audible only while he
    is signed in (DR5.28). Without them those groups are unmeasured by choice,
    so their absence is not an import he owes. Every other kind always is.
    """
    if kind == "qbserve":
        return getattr(settings, "qbserve_db", None) is not None
    if kind == "audible":
        from sources import audible  # noqa: PLC0415 — lazy keeps report's imports light

        return audible.setup(settings) is not None
    return True


#: Qbserve (DR5.3) is read daily. A read older than this means the read is not
#: happening; a newest record older than this is the stopped-Qbserve rule when
#: there is no witness to say whether the Mac was in use (`qbserve_silence`).
QBSERVE_STALE_HOURS = 36

#: Minutes of browsing Cold Turkey must have seen after Qbserve's newest record
#: before Qbserve counts as stopped while the Mac was in use (DR5.27).
QBSERVE_WITNESS_MIN = 10.0

#: What the Import column says about a week's evidence. Four of these are two
#: pairs: the same fact about coverage, worded by whether the week has been
#: asked for yet (DR5.10). Values are slugs, not prose — the template maps them
#: to words and to a CSS class, so they must stay space-free.
IMPORT_MISSING = "missing"          # asked for, and nothing has arrived
IMPORT_NOT_DUE = "not_due"          # nothing has arrived, and nothing is owed yet
IMPORT_INCOMPLETE = "incomplete"    # asked for, but no import covers the whole week
IMPORT_SO_FAR = "so_far"            # covers part of a week that is still running


#: The four states that mean a week's evidence is not all in yet.
IMPORT_AWAITED = (IMPORT_MISSING, IMPORT_NOT_DUE, IMPORT_INCOMPLETE, IMPORT_SO_FAR)


def everything_in(totals) -> bool:
    """Does every expected source cover the whole week? (`ok`/`partial` both do.)

    `partial` counts: it means an import arrived and parsed with warnings, not
    that the week is uncovered. The unreadable rows it stands for are already
    reported as unmeasured items, in their own column.
    """
    return not any(t.import_status in IMPORT_AWAITED for t in totals)


def due_on(mweek: T.MeasurementWeek) -> date:
    """The Sunday after the week: the day its manual evidence is first asked for (DR5.24)."""
    return mweek.saturday + timedelta(days=1)


def week_is_due(clk: Clock, settings: Settings, mweek: T.MeasurementWeek) -> bool:
    """Has the Sunday after it arrived? Before it, absence is a schedule, not a gap.

    This is the day `checklist.send_due` asks on, read from the same two rules,
    so the word on the page and the day of the ask cannot drift apart (DR5.10).
    A week that ended before go-live is never asked for at all (DR2.2), so it
    is never due.
    """
    if settings.go_live and mweek.end < settings.go_live:
        return False
    return T.local_date(clk.now(), settings.tz) >= due_on(mweek)

#: Standing per-group caveats shown on the cost-per-keeper table.
GROUP_BLIND_SPOTS: dict[str, tuple[str, ...]] = {
    T.GROUP_PODCASTS: (
        "Only captured episodes are polled; podcast listening outside the queue is invisible.",
    ),
    T.GROUP_YOUTUBE: (
        "Rewatches overwrite progress and undercount; dates have no time of day.",
    ),
    T.GROUP_X: (
        "Minutes come from Screen Time (OCR), Cold Turkey (this Mac's browser only) and Qbserve "
        "(a Mac X app, when configured); "
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
    T.GROUP_DESKTOP_APPS: (
        "Measured only while Qbserve is running on this Mac.",
        "Browsers are left out here: Cold Turkey measures them per site instead (Desktop web).",
        "No keeper path: vault captures are not attributed to desktop apps.",
    ),
    T.GROUP_AUDIOBOOKS: (
        "Labelled worthwhile by your standing decision: counted as content, never charged as brain rot.",
        "The day's total is Audible's own; its split across books is estimated from how far each "
        "book's place moved.",
        "No keeper path: vault captures are not attributed to audiobooks.",
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

    `minutes` counts every content label (wasteful and worthwhile), excludes
    tool minutes (shown in `tool_min`, DR5.5) and covered-elsewhere rows (shown in
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
    tool_min: float = 0.0             # sources labelled tool: not content, shown, never counted (DR5.5)


@dataclass(frozen=True)
class WeekSplit:
    """One bar of the wasteful/worthwhile chart.

    `screenshot_ocr_min` is how much of the bar came from parsed screenshots,
    so a chart that stacks OCR minutes on API minutes can say so. `tool_min`
    is time on sources labelled tool (not content, DR5.5): in neither bar
    segment, shown beside them.
    """

    week_start: str
    wasteful_min: float
    worthwhile_min: float
    screenshot_ocr_min: float = 0.0
    allowance_min: int | None = None    # that week's allowance (its snapshot, or config if never opened)
    overage_min: int = 0                # recorded, never carried (DR5.2); "so far" for the current week
    tool_min: float = 0.0               # not content (DR5.5): shown, never in the bars or the allowance
    unclassified_min: float = 0.0       # no label yet (DR5.11): charged, but never called brain rot

    @property
    def charged_min(self) -> float:
        """Decided plus undecided — what the allowance counted for this week."""
        return self.wasteful_min + self.unclassified_min


@dataclass(frozen=True)
class AllowanceStatus:
    """The current allowance week.

    `spent_wasteful_min` is pooled wasteful consumption across sources and
    `remaining_min` is what is left of the allowance after it. `overage_min` is
    how far past the allowance it has gone: recorded, never carried into the
    next week (DR5.2). `promoted_min` is POP's promoted commitment, reported
    alongside and never mixed in.
    """

    week_start: str
    allowance_min: int
    overage_min: int
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
    source_key: str | None = None     # so the page can link a site or channel to its own page


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
                                    "reasons": {}, "tool_min": 0.0})

    for r in allowance._spotify_rows(conn, settings, week_start):
        b = row((T.GROUP_PODCASTS, T.PROV_API, T.DEVICE_ACCOUNT))
        field = "tool_min" if allowance.spotify_label(conn, r).label == T.TOOL else "minutes"
        b[field] += int(r["delta_ms"]) / MIN_MS

    for r in allowance._measurement_rows(conn, settings, week_start):
        b = row((T.report_group(r["kind"], r["device"]), r["provenance"], r["device"]))
        reason = T.covered_elsewhere(r["kind"], r["source_key"])
        if reason:
            if r["status"] == "ok":
                b["covered_excluded_min"] += float(r["minutes"])
                b["reasons"][reason] = b["reasons"].get(reason, 0.0) + float(r["minutes"])
        elif r["status"] != "ok":
            b["unmeasured_items"] += 1
        elif allowance.measurement_label(conn, r).label == T.TOOL:
            b["tool_min"] += float(r["minutes"])
        else:
            b["minutes"] += float(r["minutes"])

    mweek = T.measurement_week_for_allowance_week(week_start, settings.tz)
    due = week_is_due(clk, settings, mweek)
    out: list[SourceTotal] = []
    for (group, provenance, device), v in acc.items():
        status = import_id = None
        kind = IMPORT_KIND_FOR_PROVENANCE.get(provenance)
        if kind is not None:
            imp = _covering_import(conn, settings, kind, mweek)
            # Rows exist but no import covers the whole week yet (e.g. a
            # YouTube sync made mid-week): the figure is honest but partial.
            # Only once the week has been asked for is that a gap.
            status = imp["status"] if imp else (IMPORT_INCOMPLETE if due else IMPORT_SO_FAR)
            import_id = imp["id"] if imp else None
        out.append(SourceTotal(group, provenance, device, v["minutes"], v["unmeasured_items"],
                               v["covered_excluded_min"], status, import_id,
                               tuple(sorted(v["reasons"].items())), v["tool_min"]))

    present = {(t.group, t.provenance, t.device) for t in out}
    for kind, group, provenance, device, _name in EXPECTED_WEEKLY:
        if (group, provenance, device) in present:
            continue
        if not expects(kind, settings):
            continue
        imp = _covering_import(conn, settings, kind, mweek)
        if imp is None:
            out.append(SourceTotal(group, provenance, device, None, 0, 0.0,
                                   IMPORT_MISSING if due else IMPORT_NOT_DUE))
        else:
            # Evidence arrived and simply held no rows for this week.
            out.append(SourceTotal(group, provenance, device, 0.0, 0, 0.0, imp["status"], imp["id"]))

    order = {g: i for i, g in enumerate(T.REPORT_GROUPS)}
    prov_order = {p: i for i, p in enumerate(T.PROVENANCES)}
    out.sort(key=lambda t: (order.get(t.group, 99), prov_order.get(t.provenance, 99), t.device))
    return out


@dataclass(frozen=True)
class SourceLine:
    """One named thing consumed in a week: a show, a channel, an app, a site.

    Groups and provenances answer "how do we know this"; this answers "what was
    it". `label` is None when nobody has decided yet — never guessed, so the
    row can offer the decision instead of asserting one.
    """

    source_id: int | None
    display_name: str
    group: str
    minutes: float
    label: str | None
    reflex_min: float                   # of `minutes`, how much bypassed the queue
    provenances: frozenset = frozenset()
    #: `minutes` split by the provenance that measured it, sorted, so a source
    #: can be compared across weeks only on channels both weeks had (DR5.17).
    by_provenance: tuple = ()
    #: What the source is, so the page can link a site or channel to its own page.
    kind: str | None = None
    source_key: str | None = None

    @property
    def all_reflex(self) -> bool:
        return self.minutes > 0 and self.reflex_min >= self.minutes - 1e-9


def top_sources(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                week_start: str, limit: int | None = 8) -> list[SourceLine]:
    """The week's content by name, biggest first. Tool sources never appear.

    The same rows `pooled_minutes` counts, under the same exclusions — covered
    elsewhere, unmeasured, pre-go-live — so this list and the week's totals
    cannot disagree. `limit=None` returns every source.
    """
    acc: dict[tuple, dict] = {}

    def line(key, *, name: str, group: str, source_id: int | None) -> dict:
        return acc.setdefault(key, {"display_name": name, "group": group, "source_id": source_id,
                                    "minutes": 0.0, "reflex_min": 0.0, "label": None,
                                    "provenances": set(), "by_prov": {}})

    def add(bucket: dict, minutes: float, resolved, gated: bool, provenance: str) -> None:
        bucket["minutes"] += minutes
        if not gated:
            bucket["reflex_min"] += minutes
        bucket["label"] = None if resolved.basis == T.BASIS_DEFAULT else resolved.label
        bucket["provenances"].add(provenance)
        bucket["by_prov"][provenance] = bucket["by_prov"].get(provenance, 0.0) + minutes

    for row in allowance._spotify_rows(conn, settings, week_start):
        resolved = allowance.spotify_label(conn, row)
        if resolved.label == T.TOOL:
            continue
        show = (row["show"] or "").strip() or "(unknown show)"
        promoted = conn.execute(
            "SELECT 1 FROM queue WHERE spotify_id = ? AND promoted_at IS NOT NULL "
            "AND promoted_at <= ? LIMIT 1", (row["spotify_id"], row["polled_at"])).fetchone()
        b = line((T.KIND_PODCAST_SHOW, show), name=show, group=T.GROUP_PODCASTS,
                 source_id=allowance._find_source_id(conn, T.KIND_PODCAST_SHOW, show))
        add(b, int(row["delta_ms"]) / allowance.MIN_MS, resolved, promoted is not None, T.PROV_API)

    go_live = getattr(settings, "go_live", "") or ""
    for row in allowance._measurement_rows(conn, settings, week_start):
        if row["status"] != "ok" or T.covered_elsewhere(row["kind"], row["source_key"]):
            continue
        if go_live and row["period_end"] < go_live:
            continue
        resolved = allowance.measurement_label(conn, row)
        if resolved.label == T.TOOL:
            continue
        gated = bool(row["kind"] == T.KIND_YOUTUBE_CHANNEL and row["item_key"]
                     and allowance._youtube_promoted_by(conn, settings, row["item_key"],
                                                        row["period_start"]))
        name = (row["display_name"] or row["source_key"] or "").strip() or "(unnamed)"
        b = line((row["kind"], row["source_key"]), name=name,
                 group=T.report_group(row["kind"], row["device"]), source_id=row["source_id"])
        add(b, float(row["minutes"]), resolved, gated, row["provenance"])

    out = [SourceLine(v["source_id"], v["display_name"], v["group"], v["minutes"], v["label"],
                      v["reflex_min"], frozenset(v["provenances"]),
                      tuple(sorted(v["by_prov"].items())), kind=k[0], source_key=k[1])
           for k, v in acc.items() if v["minutes"] > 0]
    # Biggest first; name breaks ties so two equal rows never swap between runs.
    out.sort(key=lambda r: (-r.minutes, r.display_name))
    return out if limit is None else out[:limit]


def label_split(conn: sqlite3.Connection, clk: Clock, settings: Settings,
                weeks: int = 8) -> list[WeekSplit]:
    """Wasteful vs worthwhile content per allowance week, oldest first, ending with the current week.

    Tool minutes (not content, DR5.5) are in neither figure; each week carries
    them as `tool_min` so they stay visible. Sources with no label yet are in
    neither either, and carried as `unclassified_min` (DR5.11): the bars show
    what the user has decided, and `charged_min` is what the allowance counted.

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
        row = conn.execute("SELECT allowance_min FROM weeks WHERE week_start = ?", (start,)).fetchone()
        allowed = row["allowance_min"] if row else settings.weekly_allowance_min
        over = allowance.overage_for(db.Week(start, allowed, 0, 0, 0), pooled)
        out.append(WeekSplit(start, pooled.wasteful_min, pooled.worthwhile_min, pooled.screenshot_ocr_min,
                             allowed, over, pooled.tool_min, pooled.unclassified_min))
    return out


def allowance_status(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> AllowanceStatus:
    """Spent, remaining and overage so far for the week containing now.

    Read-only: if no process has opened this week's row yet, the week is
    described as rollover would open it (the full allowance from config)
    without writing it. Nothing from any earlier week reduces it (DR5.2).
    """
    start = _current_week_start(clk, settings)
    row = conn.execute("SELECT * FROM weeks WHERE week_start = ?", (start,)).fetchone()
    if row is not None:
        week = db._to_week(row)
    else:
        week = db.Week(start, settings.weekly_allowance_min, 0, 0, 0)

    pooled = allowance.pooled_minutes(conn, settings, start)
    spent = pooled.charged_min
    effective = week.effective_allowance_min
    return AllowanceStatus(
        week_start=start,
        allowance_min=week.allowance_min,
        overage_min=allowance.overage_for(week, pooled),
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

    Minutes are every content label's consumption (excluding covered-elsewhere
    rows), because a keeper is what the time *yielded*, whatever it was
    labelled. Tool minutes are not content and are left out (DR5.5).
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

    sql = ("SELECT l.delta_ms, l.spotify_id, e.show FROM listening l "
           "JOIN episodes e ON e.spotify_id = l.spotify_id "
           "WHERE l.delta_ms > 0 AND e.platform = 'spotify'")
    params: list = []
    if since_instant is not None:
        sql += " AND l.polled_at >= ?"
        params.append(since_instant)
    for r in conn.execute(sql, params).fetchall():
        if allowance.spotify_label(conn, r).label == T.TOOL:
            continue
        minutes[T.GROUP_PODCASTS] += int(r["delta_ms"]) / MIN_MS
        provenances[T.GROUP_PODCASTS].add(T.PROV_API)

    sql = ("SELECT m.minutes, m.provenance, m.device, m.source_id, m.item_key, s.kind, s.source_key "
           "FROM measurements m "
           "JOIN sources s ON s.id = m.source_id WHERE m.status = 'ok'")
    params = []
    if since_day is not None:
        sql += " AND m.period_end >= ?"
        params.append(since_day)
    for r in conn.execute(sql, params).fetchall():
        if T.covered_elsewhere(r["kind"], r["source_key"]):
            continue
        if allowance.measurement_label(conn, r).label == T.TOOL:
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


#: Standing first line of `blind_spots` (DR5.5).
TOOL_SCOPE_LINE = ("Content only; sources you mark as tool (shopping, email, utilities…) are "
                   "excluded and shown separately.")


def _unattributed_phone_browsing(conn: sqlite3.Connection) -> list[str]:
    """Phone-browser blocks whose minutes are excluded but never attributed (DR5.13).

    The browser is excluded from the week because the sites inside it are
    measured instead. When no synced history covers that period, nothing takes
    its place, and those minutes would leave the week without a word. They are
    still shown in the "covered elsewhere" column; this says why they are not
    anywhere else.
    """
    rows = conn.execute(
        """
        SELECT b.period_start AS period_start, b.period_end AS period_end, b.minutes AS minutes
          FROM measurements b JOIN sources s ON s.id = b.source_id
         WHERE s.kind = ? AND s.source_key = ? AND b.status = 'ok' AND b.minutes IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM measurements m
                WHERE m.provenance = ? AND m.period_start = b.period_start
                  AND m.period_end = b.period_end)
         ORDER BY b.period_start
        """,
        (T.KIND_IPHONE_APP, "brave", T.PROV_BROWSER_SYNC),
    ).fetchall()
    return [
        f"The phone browser's {round(float(r['minutes'])):,} min for {r['period_start']} to "
        f"{r['period_end']} are not attributed to any site: no synced browser history covers that "
        "week, so they are excluded from the week's totals rather than counted blind."
        for r in rows
    ]


def blind_spots(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[str]:
    """What this diagnostic cannot see, stated rather than silently absorbed."""
    out = [
        TOOL_SCOPE_LINE,
        "Podcast listening outside the queue is invisible: only captured episodes are polled.",
        *_coldturkey_blind_spots(conn),
        "YouTube rewatches overwrite progress and undercount; history dates have no time of day or sittings.",
        "Screen Time figures are parsed from screenshots (OCR) and carry lower confidence than API figures.",
        "iPhone YouTube/Spotify app and youtube.com/open.spotify.com minutes are excluded as covered "
        "elsewhere; Spotify music is not measured at all.",
        "Vault captures are counts, never minutes: reading time is not measured.",
    ]

    out.extend(_unattributed_phone_browsing(conn))

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
    out.extend(_qbserve_blind_spots(conn, clk, settings))
    out.extend(_audible_blind_spots(conn, clk, settings))
    return out


@dataclass(frozen=True)
class QbserveSilence:
    """Qbserve has stopped recording while it should have been (DR5.27).

    `witnessed_min` is the browsing Cold Turkey saw on this Mac after the day
    of Qbserve's newest record; None when there was no witness and the
    36-hour timer decided instead.
    """

    latest_record_at: str
    witnessed_min: float | None


def _coldturkey_witness(conn: sqlite3.Connection, clk: Clock, settings: Settings, *,
                        after: date) -> float | None:
    """Browser minutes Cold Turkey recorded on this Mac on days after `after`.

    None when there is no witness: Cold Turkey isn't read automatically, or
    it hasn't been read successfully in the last `QBSERVE_STALE_HOURS`.
    "Read" includes a read that found no browsing at all, or nothing new
    (`coldturkey_local.READ_AT_KEY`): back from a trip, an eight-day window
    with no browsing is the witness that the Mac was idle, not a missing one.
    Every Cold Turkey minute counts, whatever its label: this is activity on
    the Mac, not content. Only finished days are ever recorded.
    """
    from sources import coldturkey_local  # noqa: PLC0415 — the key's owner; lazy keeps report's imports light

    if getattr(settings, "coldturkey_db", None) is None:
        return None
    row = conn.execute(
        "SELECT received_at FROM imports WHERE kind = 'coldturkey' AND status IN ('ok', 'partial') "
        "AND (CASE WHEN json_valid(meta) THEN json_extract(meta, '$.origin') END) = 'local_db' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    # The import's time too: reads from before the key existed left only that.
    reads = [t for t in (evidence.get_meta(conn, coldturkey_local.READ_AT_KEY),
                         row["received_at"] if row is not None else None) if t]
    hours = min((_hours_since(clk, t) for t in reads), default=None)
    if hours is None or hours > QBSERVE_STALE_HOURS:
        return None
    (minutes,) = conn.execute(
        "SELECT COALESCE(SUM(minutes), 0) FROM measurements "
        "WHERE provenance = ? AND status = 'ok' AND period_start > ?",
        (T.PROV_EXPORT, after.isoformat())).fetchone()
    return float(minutes)


def qbserve_silence(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> QbserveSilence | None:
    """Has Qbserve stopped recording while this Mac was in use? (DR5.27)

    Cold Turkey is the witness. Qbserve's newest record falls on local day D;
    if Cold Turkey saw at least `QBSERVE_WITNESS_MIN` of browsing on days
    after D, the Mac was used and Qbserve was not recording. An idle Mac
    (no browsing after D) is not a stopped Qbserve. Less browsing than that is
    still use, so once the newest record is more than `QBSERVE_STALE_HOURS`
    old it warns too. With no witness, that timer alone decides, the rule
    before DR5.27. None when Qbserve isn't configured or has no record.
    """
    if getattr(settings, "qbserve_db", None) is None:
        return None
    latest = _qbserve_latest_record(conn)
    if not latest:
        return None
    witnessed = _coldturkey_witness(conn, clk, settings, after=T.local_date(latest, settings.tz))
    hours = _hours_since(clk, latest)
    timer = hours is not None and hours > QBSERVE_STALE_HOURS
    if witnessed is None:
        return QbserveSilence(latest, None) if timer else None
    # Some browsing is still use. Under the bar it can't warn early, but the
    # timer stays as a backstop: days of desktop apps with a few minutes of
    # browsing (what Qbserve exists to measure) must not go unwarned. Only
    # no browsing at all, an idle Mac, never warns.
    if witnessed >= QBSERVE_WITNESS_MIN or (witnessed > 0 and timer):
        return QbserveSilence(latest, witnessed)
    return None


def _witness_clause(silence: QbserveSilence) -> str:
    """The "though this Mac was in use" clause when Cold Turkey saw it; empty when the timer decided."""
    if silence.witnessed_min is None:
        return ""
    return f", though this Mac was in use (Cold Turkey saw {round(silence.witnessed_min)} min of browsing)"


def _qbserve_blind_spots(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[str]:
    """Desktop app time is only as current as Qbserve's own recording (DR5.3).

    Stale when the latest usable Qbserve import was received more than
    `QBSERVE_STALE_HOURS` ago (the daily read is not happening), or Qbserve
    has stopped recording while the Mac was in use (`qbserve_silence`). No
    import at all while `settings.qbserve_db` is configured says so too.
    """
    row = conn.execute("SELECT received_at, meta FROM imports WHERE kind = 'qbserve' AND status != 'failed' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        if getattr(settings, "qbserve_db", None) is None:
            return []
        return ["Qbserve is configured but no desktop app time has been imported yet; desktop apps are "
                "unmeasured, not zero."]
    now = clk.now()
    try:
        latest = json.loads(row["meta"] or "{}").get("latest_record_at")
    except ValueError:
        latest = None
    received_h = (now - clock.parse(row["received_at"])).total_seconds() / 3600
    if received_h > QBSERVE_STALE_HOURS:
        return [f"Desktop app time was last read from Qbserve on {clock.fmt_local(row['received_at'], settings.tz)}; "
                "later app use is unmeasured, not zero."]
    if not latest:
        return ["Qbserve has not recorded since the start of the read; desktop app time after that is "
                "unmeasured, not zero."]
    silence = qbserve_silence(conn, clk, settings)
    if silence is not None:
        return [f"Qbserve has not recorded since {clock.fmt_local(silence.latest_record_at, settings.tz)}"
                f"{_witness_clause(silence)}; desktop app time after that is unmeasured, not zero."]
    return []


@dataclass(frozen=True)
class AudibleFailure:
    """The newest read of Audible failed, and nothing has reached Audible since (DR5.28).

    `kind` is `sources.audible.FAIL_*`: the sign-in needs redoing, Audible
    couldn't be reached, or its answer changed shape. `covered_through` is the
    last day a successful read vouched for; days after it are unmeasured.
    """

    kind: str
    at: str
    error: str
    covered_through: str | None


def _audible_last_read(conn: sqlite3.Connection) -> str | None:
    """When a read last reached Audible: its stamp, or the newest successful import."""
    from sources import audible  # noqa: PLC0415 — the key's owner

    row = _latest_import_row(conn, "audible")
    reads = [t for t in (evidence.get_meta(conn, audible.READ_AT_KEY),
                         row["received_at"] if row is not None else None) if t]
    return max(reads, key=clock.parse) if reads else None


def audible_failure(conn: sqlite3.Connection) -> AudibleFailure | None:
    """The failed read that is the last word on audiobooks, or None.

    A failure stops counting once any later read reaches Audible, even one
    that found nothing new and so recorded no import (`READ_AT_KEY`).
    """
    failed = _latest_import_row(conn, "audible", include_failed=True)
    if failed is None or failed["status"] != T.IMPORT_FAILED:
        return None
    last_read = _audible_last_read(conn)
    if last_read is not None and clock.parse(last_read) > clock.parse(failed["received_at"]):
        return None
    try:
        kind = json.loads(failed["meta"] or "{}").get("failure") or "unexpected"
    except ValueError:
        kind = "unexpected"
    (covered,) = conn.execute("SELECT MAX(period_end) FROM imports WHERE kind = 'audible' "
                              "AND status IN ('ok', 'partial')").fetchone()
    return AudibleFailure(kind, failed["received_at"], failed["error"] or "", covered)


def audible_needs_signin(conn: sqlite3.Connection, settings: Settings) -> bool:
    """Is the Audible sign-in the thing to redo? The only Audible ask there ever is."""
    if not expects("audible", settings):
        return False
    failure = audible_failure(conn)
    return failure is not None and failure.kind == "sign_in"


def _audible_blind_spots(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[str]:
    """One plain line while audiobooks aren't being read, otherwise nothing (DR5.28).

    Nothing at all when he isn't signed in (optional, so never a nag) or
    before the first read. A failed read says what failed and which days are
    unmeasured; a sign-in to redo carries the exact command.
    """
    from sources import audible  # noqa: PLC0415 — the command's owner

    if not expects("audible", settings):
        return []
    failure = audible_failure(conn)
    if failure is not None:
        when = clock.fmt_local(failure.at, settings.tz)
        after = (f"days after {failure.covered_through} are unmeasured, not zero" if failure.covered_through
                 else "audiobook listening is unmeasured, not zero")
        if failure.kind == "sign_in":
            return [f"Audiobook listening couldn't be read on {when}: the Audible sign-in needs redoing "
                    f"({audible.SIGN_IN_COMMAND}). {after[0].upper()}{after[1:]}."]
        return [f"Audible's listening stats couldn't be read on {when}; {after}. "
                "The next morning's read tries again."]
    last = _audible_last_read(conn)
    hours = _hours_since(clk, last)
    if hours is not None and hours > STALE_HOURS:
        return [f"Audible's listening stats were last read on {clock.fmt_local(last, settings.tz)}; "
                "later days are unmeasured, not zero."]
    return []


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
    if label is not None and label not in T.SOURCE_LABELS:
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
            device=r["device"], covered_elsewhere=T.covered_elsewhere(r["kind"], r["source_key"]),
            import_id=r["import_id"], source_key=r["source_key"],
        ))

    if label is not None:
        out = [r for r in out if r.label == label]
    out.sort(key=lambda r: (r.day, r.provenance, r.source, r.item or ""), reverse=True)
    return out[:limit]


# --------------------------------------------------------------------------
# What needs the user (DR5.6)
# --------------------------------------------------------------------------
#
# The user's complaint was that nothing told him how or when to put data in:
# "I haven't been prompted in any meaningful way to put data in". Telegram now
# keeps only captures, Sunday triage, allowance alerts and the Sunday check-in
# (DR5.24); everything else is answered here, as data, and rendered by the
# dashboard. These functions therefore say, per source group,
# what the state of the current week is, whether the user owes an action, and
# in one sentence why — never a score, never a nag.

MODE_AUTOMATIC = "automatic"
MODE_MANUAL = "manual"

#: The state of one source group for the current week.
STATE_OK = "ok"                # nothing to do; the data is arriving
STATE_WAITING = "waiting"      # nothing to do *yet* (the week isn't over, or nothing seen yet)
STATE_MISSING = "missing"      # evidence that is due has not arrived
STATE_STALE = "stale"          # an automatic source stopped producing
STATE_ATTENTION = "attention"  # the last attempt failed, or a person must resolve something
NEEDS_STATES = (STATE_MISSING, STATE_STALE, STATE_ATTENTION)

#: Human names, the same words the dashboard uses for groups.
GROUP_TEXT = {
    T.GROUP_PODCASTS: "Podcasts",
    T.GROUP_YOUTUBE: "YouTube",
    T.GROUP_X: "X",
    T.GROUP_IPHONE: "iPhone apps",
    T.GROUP_DESKTOP: "Desktop web",
    T.GROUP_DESKTOP_APPS: "Desktop apps",
    T.GROUP_AUDIOBOOKS: "Audiobooks",
    T.GROUP_VAULT: "Vault (Obsidian)",
}

#: When each source actually produces data. Plain language, no cron syntax:
#: the daily job runs 09:30 local and POP's Spotify poll 09:17 (launchd/).
SCHEDULE_DAILY_JOB = "every morning 09:30"
SCHEDULE_SPOTIFY_POLL = "every morning 09:17"
SCHEDULE_MANUAL_WEEKLY = "weekly, you run it"
SCHEDULE_CONTINUOUS = "continuously while it runs, read every morning 09:30"

#: How long an automatic source may produce nothing before it is stale. The
#: same threshold the Qbserve warning uses, for the same reason: one missed
#: daily run is not a failure, two is.
STALE_HOURS = QBSERVE_STALE_HOURS

#: Appended to every row once the diagnostic is over. Nothing is owed then.
DIAGNOSTIC_ENDED_NOTE = "The diagnostic has ended, so nothing more is asked for."

#: The order groups are considered in, before the "needs you first" sort.
ATTENTION_ORDER = (T.GROUP_IPHONE, T.GROUP_YOUTUBE, T.GROUP_DESKTOP, T.GROUP_DESKTOP_APPS,
                   T.GROUP_PODCASTS, T.GROUP_AUDIOBOOKS, T.GROUP_VAULT)


@dataclass(frozen=True)
class NeedsRow:
    """One source group's standing, for the dashboard's "what needs you" panel.

    `covered` answers only "is there evidence covering any part of the current
    week"; `state` answers the useful question, which for a weekly manual
    source is about the last *complete* week (the current one cannot be sent
    yet). `action_text` is empty when the user owes nothing, so a caller can
    sort and count by "is there an action" without interpreting `state`.
    """

    group: str
    label: str
    mode: str                  # MODE_AUTOMATIC | MODE_MANUAL
    schedule_text: str
    last_data_at: str | None   # UTC ISO instant of the newest data, or None
    covered: bool
    state: str
    action_text: str
    detail: str


@dataclass(frozen=True)
class ProvenanceRow:
    """One line of "where each number comes from".

    `folds_into` is set when this path's minutes are stored under a different
    source than its own name suggests — the two X paths (DR1.9/§5) — so the
    table can say so instead of the user discovering it from a total.
    """

    group: str
    label: str
    measures: str
    tool: str
    provenance: str
    provenance_text: str
    mode: str
    schedule_text: str
    limits: tuple[str, ...]
    folds_into: str | None = None


#: Said in full on the provenance table, because "X" being one row while two
#: different tools measure it is the single most confusing thing in DRBR.
X_FOLD_NOTE = ("X is measured twice and counted once: desktop x.com/twitter.com through Cold Turkey, "
               "the X iPhone app through Screen Time. Both are folded into one X source, so you label X "
               "once and the minutes add up rather than competing.")


def _latest_import_row(conn: sqlite3.Connection, kind: str, *, include_failed: bool = False) -> sqlite3.Row | None:
    sql = "SELECT * FROM imports WHERE kind = ?"
    if not include_failed:
        sql += " AND status != 'failed'"
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", (kind,)).fetchone()


def _hours_since(clk: Clock, instant: str | None) -> float | None:
    if not instant:
        return None
    return (clk.now() - clock.parse(instant)).total_seconds() / 3600


def _received_in_week(conn: sqlite3.Connection, settings: Settings, kind: str,
                      week: T.MeasurementWeek) -> bool:
    """Any usable import of `kind` received on or after this week's Sunday.

    The coverage test for sources whose imports carry no week (a YouTube sync
    walks back to its cursor; a vault scan is a moment, not a period).
    """
    since = _local_midnight(week.sunday, settings.tz)
    return conn.execute(
        "SELECT 1 FROM imports WHERE kind = ? AND status IN ('ok','partial') AND received_at >= ? LIMIT 1",
        (kind, since),
    ).fetchone() is not None


def _weekly_manual_state(conn: sqlite3.Connection, clk: Clock, settings: Settings, kind: str, *,
                         today: date, action: str) -> tuple[str, str, str]:
    """-> (state, action_text, detail) for a source the user sends once a week.

    The question is never about the current week — it isn't over — but about
    the most recent complete Sunday..Saturday, and only from the Sunday after
    it, which is exactly when `checklist.send_due` asks (DR5.24). A week
    ending before `settings.go_live` is never owed (DR2.2).
    """
    week = T.measurement_week(T.week_sunday(today) - timedelta(days=7))
    span = f"{week.start} to {week.end}"
    if _covering_import(conn, settings, kind, week) is not None:
        return STATE_OK, "", f"Last week ({span}) has arrived."
    if settings.go_live and week.end < settings.go_live:
        return STATE_OK, "", f"The week {span} ended before measuring started."
    if not week_is_due(clk, settings, week):
        return STATE_WAITING, "", f"Last week ({span}) can be sent from Sunday."
    return STATE_MISSING, action, f"Nothing has arrived for last week ({span})."


def _automatic_state(conn: sqlite3.Connection, clk: Clock, kind: str, *, what: str, tz: str,
                     never_action: str, stale_action: str) -> tuple[str, str, str, str | None]:
    """-> (state, action_text, detail, last_data_at) for a source DRBR reads itself.

    Answers "is the read happening". Whether the tool itself is still
    recording is a separate question, asked only of Qbserve
    (`qbserve_silence`, in `needs_attention`).
    """
    latest_any = _latest_import_row(conn, kind, include_failed=True)
    row = _latest_import_row(conn, kind)
    if row is None:
        error = (latest_any["error"] if latest_any is not None else None)
        if latest_any is not None:
            return (STATE_ATTENTION, never_action,
                    f"Every attempt to read {what} has failed. Last error: {error or 'unknown'}.", None)
        return STATE_MISSING, never_action, f"{what} has never been read.", None
    last = row["received_at"]
    if latest_any is not None and latest_any["status"] == T.IMPORT_FAILED and latest_any["id"] > row["id"]:
        return (STATE_ATTENTION, stale_action,
                f"The last read of {what} failed: {latest_any['error'] or 'unknown'}. "
                f"The newest usable figures are from {clock.fmt_local(last, tz)}.", last)
    hours = _hours_since(clk, last)
    if hours is not None and hours > STALE_HOURS:
        return (STATE_STALE, stale_action,
                f"{what} was last read on {clock.fmt_local(last, tz)}; later use is unmeasured, not zero.", last)
    return STATE_OK, "", f"Read on {clock.fmt_local(last, tz)}.", last


def _qbserve_latest_record(conn: sqlite3.Connection) -> str | None:
    row = _latest_import_row(conn, "qbserve")
    if row is None:
        return None
    try:
        return json.loads(row["meta"] or "{}").get("latest_record_at")
    except ValueError:
        return None


SCREENTIME_ACTION = ("Send last week's Screen Time screenshots to the bot: Settings → Screen Time → this "
                     "iPhone → Week → ‹ once (\"Last Week\"), tap Show More until it disappears, and send "
                     "the whole scroll in one go.")
YOUTUBE_ACTION = ("Open youtube.com/feed/history in the desktop browser, click the YouTube history extension and "
                  "press Sync.")
COLDTURKEY_EXPORT_ACTION = ("Cold Turkey Blocker → Statistics → Websites usage (time) → last week → Export, "
                            "and send the JSON file to the bot.")
COLDTURKEY_READ_ACTION = ("Cold Turkey's own statistics are read on this Mac; check logs/jobs.log, and "
                          "export last week by hand meanwhile.")
QBSERVE_ACTION = "Open Qbserve and check it's recording; it is reopened automatically every 10 minutes."
QBSERVE_UNCONFIGURED_ACTION = ("Desktop app time is not measured: set DRBR_QBSERVE_DB in .env and let the "
                               "next daily run pick it up.")
VAULT_ACTION = "Check DRBR_VAULT_PATH and logs/jobs.log: the vault has not been scanned."
AUDIBLE_SIGNIN_ACTION = ("Sign in to Audible again, in Terminal: "
                         "cd ~/Projects/drbr && .venv/bin/python audible_login.py --again")
AUDIBLE_NOT_SET_UP = "Not set up, so audiobook listening isn't measured. That's optional."


def _audible_state(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> tuple[str, str, str, str | None]:
    """-> (state, action_text, detail, last_data_at) for audiobooks, once he is signed in (DR5.28).

    The only action ever asked of him is the sign-in, and only when Audible
    refused it. Audible being unreachable is shown but owes nothing: the next
    morning's read tries again by itself.
    """
    tz = settings.tz
    last = _audible_last_read(conn)
    failure = audible_failure(conn)
    if failure is not None:
        through = (f" Figures run to {failure.covered_through}; later days are unmeasured, not zero."
                   if failure.covered_through else "")
        return (STATE_ATTENTION, AUDIBLE_SIGNIN_ACTION if failure.kind == "sign_in" else "",
                f"The last read of Audible's listening stats failed: {failure.error}{through}", last)
    if last is None:
        return STATE_WAITING, "", "Audible's listening stats haven't been read yet; the morning job reads them.", None
    hours = _hours_since(clk, last)
    if hours is not None and hours > STALE_HOURS:
        return (STATE_STALE, "", f"Audible's listening stats were last read on {clock.fmt_local(last, tz)}; "
                                 "later listening is unmeasured, not zero.", last)
    return STATE_OK, "", f"Read on {clock.fmt_local(last, tz)}.", last


def needs_attention(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[NeedsRow]:
    """One row per source group for the current week, rows needing the user first.

    This is the answer to "how and when am I supposed to be submitting
    sources": every group says whether it feeds itself, when it does so, when
    it last produced anything, and what — if anything — the user has to do.
    Read-only, like everything else here.
    """
    tz = settings.tz
    today = T.local_date(clk.now(), tz)
    this_week = T.measurement_week(today)
    ended = evidence.diagnostic(conn, clk, settings).ended
    rows: list[NeedsRow] = []

    # --- Podcasts: POP's Spotify poll, already running for the queue.
    last_poll = conn.execute("SELECT MAX(polled_at) FROM listening WHERE delta_ms > 0").fetchone()[0]
    if last_poll is None:
        podcast_state, podcast_detail = STATE_WAITING, ("Nothing has been polled yet. Only episodes you "
                                                        "capture are measured.")
    else:
        podcast_state, podcast_detail = STATE_OK, (f"Last poll {clock.fmt_local(last_poll, tz)}. Only episodes "
                                                   "you capture are measured.")
    rows.append(NeedsRow(T.GROUP_PODCASTS, GROUP_TEXT[T.GROUP_PODCASTS], MODE_AUTOMATIC, SCHEDULE_SPOTIFY_POLL,
                         last_poll, last_poll is not None and T.local_date(last_poll, tz) >= this_week.sunday,
                         podcast_state, "", podcast_detail))

    # --- YouTube: the extension, run by hand.
    yt_import = _latest_import_row(conn, "youtube_history")
    state, action, detail = _weekly_manual_state(conn, clk, settings, "youtube_history", today=today,
                                                 action=YOUTUBE_ACTION)
    rows.append(NeedsRow(T.GROUP_YOUTUBE, GROUP_TEXT[T.GROUP_YOUTUBE], MODE_MANUAL, SCHEDULE_MANUAL_WEEKLY,
                         yt_import["received_at"] if yt_import else None,
                         _received_in_week(conn, settings, "youtube_history", this_week),
                         state, action, detail))

    # --- iPhone apps: Screen Time screenshots, sent by hand.
    st_import = _latest_import_row(conn, "screentime")
    state, action, detail = _weekly_manual_state(conn, clk, settings, "screentime", today=today,
                                                 action=SCREENTIME_ACTION)
    eye = len(needs_your_eye(conn, clk, settings))
    if eye:
        state = STATE_ATTENTION if state == STATE_OK else state
        action = action or "Check the rows the screenshots could not be read for and set the right minutes."
        detail = f"{detail} {eye} row(s) could not be read and are waiting for your eye."
    rows.append(NeedsRow(T.GROUP_IPHONE, GROUP_TEXT[T.GROUP_IPHONE], MODE_MANUAL, SCHEDULE_MANUAL_WEEKLY,
                         st_import["received_at"] if st_import else None,
                         _covering_import(conn, settings, "screentime", this_week) is not None,
                         state, action, detail))

    # --- Desktop web: automatic since DR5.1 unless the database isn't configured.
    ct_covered = _covering_import(conn, settings, "coldturkey", this_week) is not None
    ct_import = _latest_import_row(conn, "coldturkey")
    if getattr(settings, "coldturkey_db", None) is not None:
        state, action, detail, last = _automatic_state(
            conn, clk, "coldturkey", what="Cold Turkey's statistics", tz=tz,
            never_action=COLDTURKEY_READ_ACTION, stale_action=COLDTURKEY_READ_ACTION)
        rows.append(NeedsRow(T.GROUP_DESKTOP, GROUP_TEXT[T.GROUP_DESKTOP], MODE_AUTOMATIC, SCHEDULE_DAILY_JOB,
                             last, ct_covered, state, action, detail))
    else:
        state, action, detail = _weekly_manual_state(conn, clk, settings, "coldturkey", today=today,
                                                     action=COLDTURKEY_EXPORT_ACTION)
        rows.append(NeedsRow(T.GROUP_DESKTOP, GROUP_TEXT[T.GROUP_DESKTOP], MODE_MANUAL, SCHEDULE_MANUAL_WEEKLY,
                             ct_import["received_at"] if ct_import else None, ct_covered, state, action,
                             f"{detail} DRBR_COLDTURKEY_DB is unset, so this Mac is not read automatically."))

    # --- Desktop apps: Qbserve, or nothing at all.
    qb_covered = _covering_import(conn, settings, "qbserve", this_week) is not None
    if getattr(settings, "qbserve_db", None) is None:
        rows.append(NeedsRow(T.GROUP_DESKTOP_APPS, GROUP_TEXT[T.GROUP_DESKTOP_APPS], MODE_AUTOMATIC,
                             SCHEDULE_CONTINUOUS, None, qb_covered, STATE_MISSING,
                             QBSERVE_UNCONFIGURED_ACTION,
                             "Qbserve is not configured, so native Mac app time is unmeasured, not zero."))
    else:
        state, action, detail, last = _automatic_state(
            conn, clk, "qbserve", what="Qbserve", tz=tz, never_action=QBSERVE_ACTION,
            stale_action=QBSERVE_ACTION)
        # The read is happening, but is Qbserve recording? Only while the Mac is in use (DR5.27).
        silence = qbserve_silence(conn, clk, settings) if state == STATE_OK else None
        if silence is not None:
            state, action = STATE_STALE, QBSERVE_ACTION
            detail = (f"The read is current but Qbserve has recorded nothing since "
                      f"{clock.fmt_local(silence.latest_record_at, tz)}{_witness_clause(silence)}.")
        rows.append(NeedsRow(T.GROUP_DESKTOP_APPS, GROUP_TEXT[T.GROUP_DESKTOP_APPS], MODE_AUTOMATIC,
                             SCHEDULE_CONTINUOUS, last, qb_covered, state, action, detail))

    # --- Audiobooks: Audible's own listening stats, read every morning once he is signed in.
    ab_covered = _covering_import(conn, settings, "audible", this_week) is not None
    if not expects("audible", settings):
        rows.append(NeedsRow(T.GROUP_AUDIOBOOKS, GROUP_TEXT[T.GROUP_AUDIOBOOKS], MODE_AUTOMATIC,
                             SCHEDULE_DAILY_JOB, None, ab_covered, STATE_WAITING, "", AUDIBLE_NOT_SET_UP))
    else:
        state, action, detail, last = _audible_state(conn, clk, settings)
        rows.append(NeedsRow(T.GROUP_AUDIOBOOKS, GROUP_TEXT[T.GROUP_AUDIOBOOKS], MODE_AUTOMATIC,
                             SCHEDULE_DAILY_JOB, last, ab_covered, state, action, detail))

    # --- Vault: scanned by the same daily job; counts, never minutes.
    state, action, detail, last = _automatic_state(
        conn, clk, "vault_scan", what="the Obsidian vault", tz=tz,
        never_action=VAULT_ACTION, stale_action=VAULT_ACTION)
    rows.append(NeedsRow(T.GROUP_VAULT, GROUP_TEXT[T.GROUP_VAULT], MODE_AUTOMATIC, SCHEDULE_DAILY_JOB, last,
                         _received_in_week(conn, settings, "vault_scan", this_week), state, action,
                         f"{detail} Captures are counted, never turned into minutes."))

    if ended:
        rows = [NeedsRow(r.group, r.label, r.mode, r.schedule_text, r.last_data_at, r.covered, r.state, "",
                         f"{r.detail} {DIAGNOSTIC_ENDED_NOTE}") for r in rows]

    order = {g: i for i, g in enumerate(ATTENTION_ORDER)}
    rows.sort(key=lambda r: (r.action_text == "", r.mode == MODE_AUTOMATIC, order.get(r.group, 99)))
    return rows


def provenance_map(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[ProvenanceRow]:
    """Where each number on the dashboard comes from, and what it cannot see.

    Static except for the settings that change how a source is fed
    (`coldturkey_db`, `qbserve_db`, the Audible sign-in); `conn` and `clk` are part of the report
    signature convention. Limits reuse `GROUP_BLIND_SPOTS` rather than
    restating them, so the caveats on this table and on cost per keeper can
    never drift apart.
    """
    ct_auto = getattr(settings, "coldturkey_db", None) is not None
    qb_on = getattr(settings, "qbserve_db", None) is not None
    ab_on = expects("audible", settings)
    rows = [
        ProvenanceRow(
            T.GROUP_PODCASTS, GROUP_TEXT[T.GROUP_PODCASTS],
            "Minutes listened to episodes you captured into the queue.",
            "Spotify (resume position, polled)", T.PROV_API, T.PROVENANCE_TEXT[T.PROV_API],
            MODE_AUTOMATIC, SCHEDULE_SPOTIFY_POLL, GROUP_BLIND_SPOTS[T.GROUP_PODCASTS]),
        ProvenanceRow(
            T.GROUP_YOUTUBE, GROUP_TEXT[T.GROUP_YOUTUBE],
            "Minutes watched per channel, across every device signed into the account.",
            "Browser extension (watch history) × YouTube Data API duration",
            T.PROV_PAGE, T.PROVENANCE_TEXT[T.PROV_PAGE], MODE_MANUAL, SCHEDULE_MANUAL_WEEKLY,
            GROUP_BLIND_SPOTS[T.GROUP_YOUTUBE]),
        ProvenanceRow(
            T.GROUP_X, "X — desktop (x.com)", "Minutes on x.com and twitter.com in this Mac's browsers.",
            "Cold Turkey Blocker statistics", T.PROV_EXPORT, T.PROVENANCE_TEXT[T.PROV_EXPORT],
            MODE_AUTOMATIC if ct_auto else MODE_MANUAL,
            SCHEDULE_DAILY_JOB if ct_auto else SCHEDULE_MANUAL_WEEKLY,
            (X_FOLD_NOTE,) + GROUP_BLIND_SPOTS[T.GROUP_X], folds_into=T.GROUP_X),
        ProvenanceRow(
            T.GROUP_X, "X — iPhone app", "Minutes in the X app on the iPhone.",
            "Screen Time screenshots (Apple Vision OCR on this Mac)", T.PROV_SCREENSHOT,
            T.PROVENANCE_TEXT[T.PROV_SCREENSHOT], MODE_MANUAL, SCHEDULE_MANUAL_WEEKLY,
            (X_FOLD_NOTE,) + GROUP_BLIND_SPOTS[T.GROUP_X], folds_into=T.GROUP_X),
        ProvenanceRow(
            T.GROUP_X, "X — Mac app", "Minutes in a native X client on this Mac, when one is used.",
            "Qbserve", T.PROV_APP_TRACKER, T.PROVENANCE_TEXT[T.PROV_APP_TRACKER],
            MODE_AUTOMATIC, SCHEDULE_CONTINUOUS,
            (X_FOLD_NOTE,) + GROUP_BLIND_SPOTS[T.GROUP_X]
            + (() if qb_on else ("Qbserve is not configured, so this path measures nothing.",)),
            folds_into=T.GROUP_X),
        ProvenanceRow(
            T.GROUP_IPHONE, GROUP_TEXT[T.GROUP_IPHONE], "Per-app minutes for one complete past week.",
            "Screen Time screenshots (Apple Vision OCR on this Mac)", T.PROV_SCREENSHOT,
            T.PROVENANCE_TEXT[T.PROV_SCREENSHOT], MODE_MANUAL, SCHEDULE_MANUAL_WEEKLY,
            GROUP_BLIND_SPOTS[T.GROUP_IPHONE]),
        ProvenanceRow(
            T.GROUP_DESKTOP, GROUP_TEXT[T.GROUP_DESKTOP], "Minutes per website in this Mac's browsers.",
            "Cold Turkey Blocker statistics" + ("" if ct_auto else " (manual export)"),
            T.PROV_EXPORT, T.PROVENANCE_TEXT[T.PROV_EXPORT],
            MODE_AUTOMATIC if ct_auto else MODE_MANUAL,
            SCHEDULE_DAILY_JOB if ct_auto else SCHEDULE_MANUAL_WEEKLY,
            GROUP_BLIND_SPOTS[T.GROUP_DESKTOP]),
        ProvenanceRow(
            T.GROUP_DESKTOP_APPS, GROUP_TEXT[T.GROUP_DESKTOP_APPS],
            "Minutes per native Mac app; browsers are left to Desktop web.",
            "Qbserve", T.PROV_APP_TRACKER, T.PROVENANCE_TEXT[T.PROV_APP_TRACKER],
            MODE_AUTOMATIC, SCHEDULE_CONTINUOUS,
            GROUP_BLIND_SPOTS[T.GROUP_DESKTOP_APPS]
            + (() if qb_on else ("Qbserve is not configured, so desktop apps are unmeasured, not zero.",))),
        ProvenanceRow(
            T.GROUP_AUDIOBOOKS, GROUP_TEXT[T.GROUP_AUDIOBOOKS],
            "Minutes listened to audiobooks, across every device signed in to your Audible account.",
            "Audible's own daily listening stats, split across books by how far each one's place moved",
            T.PROV_AUDIBLE_STATS, T.PROVENANCE_TEXT[T.PROV_AUDIBLE_STATS], MODE_AUTOMATIC, SCHEDULE_DAILY_JOB,
            GROUP_BLIND_SPOTS[T.GROUP_AUDIOBOOKS] + (() if ab_on else (AUDIBLE_NOT_SET_UP,))),
        ProvenanceRow(
            T.GROUP_VAULT, GROUP_TEXT[T.GROUP_VAULT],
            "Counts of captures kept, edited or dropped. Never minutes.",
            "Obsidian vault scan (capture ids)", T.PROV_VAULT, T.PROVENANCE_TEXT[T.PROV_VAULT],
            MODE_AUTOMATIC, SCHEDULE_DAILY_JOB,
            ("Vault captures are counts, never minutes: reading time is not measured.",)),
    ]
    return rows


# --------------------------------------------------------------------------
# Rows a person has to resolve (DR5.6)
# --------------------------------------------------------------------------
#
# Real case: Facebook was read as both "2h 27m" and "2h 37m" on two
# screenshots, so ~150 minutes were stored as `unreadable` with NULL minutes
# rather than guessed. That is the right thing for a parser to do and the
# wrong place to leave it: the user can see which is right in two seconds.
# Only OCR rows appear here — a YouTube video with no duration from the API
# has no number a person could supply either, so offering it would invite an
# invented figure.

#: Provenances whose failures a person can actually resolve.
EYE_PROVENANCES = (T.PROV_SCREENSHOT,)


@dataclass(frozen=True)
class EyeRow:
    """One measurement the parser refused to guess at, with what it did read.

    `options_min` holds every duration the evidence itself contains, so the
    dashboard can offer the two conflicting readings as buttons.
    `suggested_min` is filled only when the evidence contains exactly one
    candidate; with two conflicting readings there is no suggestion, because
    choosing between them is the user's job and inventing a third is nobody's.
    """

    measurement_id: int
    group: str
    source: str
    day: str
    period_end: str
    status: str
    provenance: str
    what_was_read: tuple[str, ...]
    options_min: tuple[float, ...]
    suggested_min: float | None
    detail: str


def _eye_readings(raw: tuple[str, ...], stored: object) -> tuple[float, ...]:
    """Durations present in the stored evidence, newest format first.

    `detail.readings` is written by the parser from the values it actually
    matched. Rows stored before that existed only have `detail.raw` ("Facebook
    | 2h 27m"), so those are re-read with the parser's own duration grammar —
    lazily imported, because `report` must stay importable without the source
    modules' dependencies.
    """
    if isinstance(stored, list):
        out = [float(v) for v in stored if isinstance(v, (int, float))]
        if out:
            return tuple(sorted(dict.fromkeys(out)))
    from sources import screentime  # noqa: PLC0415 — see docstring

    found: list[float] = []
    for text in raw:
        for part in str(text).split("|"):
            value = screentime.parse_duration(part)
            if value is not None:
                found.append(round(value, 4))
    return tuple(sorted(dict.fromkeys(found)))


def needs_your_eye(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> list[EyeRow]:
    """Measurements in the diagnostic window a person could resolve, newest first.

    Read-only. `screentime.correct_measurement` is what acts on a row.
    """
    floor = window_start(conn, clk, settings)
    sql = ("SELECT m.*, s.kind AS kind, s.display_name AS display_name FROM measurements m "
           "JOIN sources s ON s.id = m.source_id WHERE m.status != 'ok' AND m.provenance IN (%s)"
           % ",".join("?" * len(EYE_PROVENANCES)))
    params: list = list(EYE_PROVENANCES)
    if floor is not None:
        sql += " AND m.period_end >= ?"
        params.append(floor)
    out: list[EyeRow] = []
    for r in conn.execute(sql, params).fetchall():
        try:
            detail = json.loads(r["detail"] or "{}")
        except ValueError:
            detail = {}
        raw = tuple(str(x) for x in detail.get("raw", []) if str(x).strip())
        options = _eye_readings(raw, detail.get("readings"))
        reason = detail.get("unreadable_reason")
        if reason == "conflicting_durations" or len(options) > 1:
            text = (f"{r['display_name']} was read with more than one duration "
                    f"({', '.join(raw) or 'no readable text'}), so nothing was recorded rather than guessed.")
        elif not options:
            text = (f"{r['display_name']} has no readable duration in the screenshots, so nothing was "
                    "recorded rather than guessed.")
        else:
            text = f"{r['display_name']} was read once, with low confidence."
        out.append(EyeRow(
            measurement_id=r["id"], group=T.report_group(r["kind"], r["device"]), source=r["display_name"],
            day=r["period_start"], period_end=r["period_end"], status=r["status"], provenance=r["provenance"],
            what_was_read=raw, options_min=options,
            suggested_min=options[0] if len(options) == 1 else None, detail=text,
        ))
    out.sort(key=lambda e: (e.day, e.source), reverse=True)
    return out
