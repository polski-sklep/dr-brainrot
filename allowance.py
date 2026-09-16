"""allowance.py — the label-aware pooled allowance. DRBR Phase 2, Agent D.

Implements CONTRACTS.md Part II §7. POP's gate (db.promote), rollover
(db.roll_over_week) and alerts (poll.check_alerts) call into this module for
the two things DRBR changes about them: *what an item is labelled* and *which
minutes count against the allowance*.

Four rules this module exists to enforce.

1. **The user labels, the system counts.** Every label read goes through
   `labels.resolve`, via `item_label` for queue items. Nothing here caches a
   label past a single call, and nothing derives one from duration, watch
   percentage, completion, dwell time or anything else. A source that nobody
   has labelled resolves to wasteful, because that is the documented default,
   not because of anything it did.

2. **Worthwhile never spends the allowance.** Its promotion costs 0 and its
   consumption is excluded from pooled wasteful minutes, debt and alerts. It
   is still *counted* — `PooledMinutes.worthwhile_min` — because "uncounted"
   in the brief means "uncharged", and a diagnostic that forgot what it saw
   would be lying by omission. The lock is not this module's business and is
   untouched for every label (DECISIONS.md DR1.3).

3. **One pooled allowance across sources.** A week's wasteful minutes are
   Spotify's clamped deltas (POP's instant-based week attribution) plus every
   ok `measurements` row whose local day maps to that allowance week
   (`drbr_types.allowance_week_start_for_day`, DR1.7). Rows a better source
   already measures (`COVERED_ELSEWHERE`, DR1.9) are reported separately and
   never pooled, or the same viewing would be charged twice.

4. **NULL is not zero.** A measurement without minutes (progress or duration
   missing, unreadable) adds nothing to any total and is counted in
   `unmeasured_items` instead, so the gap stays visible (DR1.22).
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field, replace

import clock
import db
import drbr_types as T
import labels
from clock import Clock
from config import Settings

MIN_MS = 60_000


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GroupMinutes:
    """One report group's share of an allowance week."""

    wasteful_min: float = 0.0
    worthwhile_min: float = 0.0
    covered_excluded_min: float = 0.0
    unmeasured_items: int = 0


@dataclass(frozen=True)
class PooledMinutes:
    """Everything consumed in one allowance week, split by label.

    `wasteful_min` is the only figure the allowance, debt and alerts read.
    `worthwhile_min` is measured and reported, never charged.
    """

    wasteful_min: float
    worthwhile_min: float
    by_group: dict[str, GroupMinutes] = field(default_factory=dict)
    covered_excluded_min: float = 0.0
    unmeasured_items: int = 0
    #: The parsed-screenshot share of the figures above (all labels, and the
    #: wasteful part). Carried so an aggregate that mixes OCR with API figures
    #: can say how much of it is lower-confidence instead of flattening it.
    screenshot_ocr_min: float = 0.0
    screenshot_ocr_wasteful_min: float = 0.0


@dataclass(frozen=True)
class GatedSplit:
    """Consumption that passed through the queue versus consumption that bypassed it.

    Both figures include every label. `wasteful_ungated_min` is the subset of
    `ungated_min` that was wasteful — the brief's "reflex rather than choice".
    """

    gated_min: float = 0.0
    ungated_min: float = 0.0
    wasteful_ungated_min: float = 0.0


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def whole_minutes(minutes: float) -> int:
    """Float minutes to whole minutes, ties DOWN — POP's `ms_to_min` rule.

    For Spotify-only weeks this reproduces `ms_to_min(sum(delta_ms))` exactly,
    so debt and alerts agree with POP to the minute wherever POP had an
    opinion.
    """
    if minutes <= 0:
        return 0
    return max(0, math.ceil(minutes - 0.5))


def week_end(settings: Settings, week_start: str) -> str:
    """The instant that closes an allowance week. DST-correct via clock."""
    return clock.iso(clock.next_week_start(clock.parse(week_start), settings.tz))


def _item_source_key(item) -> tuple[str, str, str] | None:
    """(kind, raw key, display name) for a queue item's labelled source, or None.

    Spotify rows belong to the podcast show, keyed by the show name POP
    stores (EXISTING.md discrepancy 7). YouTube rows belong to the channel,
    keyed by the channel id captured from the Data API.
    """
    platform = getattr(item, "platform", "spotify") or "spotify"
    if platform == "youtube":
        key = (getattr(item, "source_key", None) or "").strip()
        if not key:
            return None
        return T.KIND_YOUTUBE_CHANNEL, key, (item.show or key)
    show = (getattr(item, "show", None) or "").strip()
    if not show:
        return None
    return T.KIND_PODCAST_SHOW, show, show


def _item_ref_for(item) -> str:
    """The override key. YouTube rows already store 'youtube:<videoId>'."""
    platform = getattr(item, "platform", "spotify") or "spotify"
    if platform == "youtube":
        return item.spotify_id
    return T.item_ref("spotify", item.spotify_id)


def _find_source_id(conn: sqlite3.Connection, kind: str, raw_key: str) -> int | None:
    """Look a source up WITHOUT creating it. Reads must never write.

    An absent source is simply unlabelled, which `labels.resolve` turns into
    the wasteful default — the same answer creating it would have produced.
    """
    try:
        kind, key = labels.normalise_key(kind, raw_key)
    except labels.LabelError:
        return None
    row = conn.execute(
        "SELECT id FROM sources WHERE kind = ? AND source_key = ?", (kind, key)
    ).fetchone()
    return int(row["id"]) if row else None


# --------------------------------------------------------------------------
# Labels for queue items
# --------------------------------------------------------------------------

def item_label(conn: sqlite3.Connection, item) -> labels.ResolvedLabel:
    """The resolved label of one queue item. Always read fresh.

    Source: `podcast_show(show)` for Spotify rows, `youtube_channel(source_key)`
    for YouTube rows. Item ref: `spotify:<id>` / `youtube:<videoId>`.
    Precedence is `labels.resolve`'s and nobody else's.
    """
    found = _item_source_key(item)
    source_id = _find_source_id(conn, found[0], found[1]) if found else None
    return labels.resolve(conn, source_id=source_id, item_ref=_item_ref_for(item))


def ensure_item_source(conn: sqlite3.Connection, clk: Clock, item) -> labels.Source | None:
    """Make sure the item's source exists so the user can be asked to label it.

    Identity only — never a label. Commits. Called at capture and by the
    daily poll (which backfills items captured before DRBR existed); without a
    `sources` row `labels.sources_needing_label` could never offer the show.
    """
    found = _item_source_key(item)
    if found is None:
        return None
    kind, raw, display = found
    source = labels.ensure_source(conn, clk, kind, raw, display)
    conn.commit()
    return source


def promotion_cost(conn: sqlite3.Connection, item) -> tuple[int, labels.ResolvedLabel]:
    """-> (cost in minutes, the label it was decided on).

    Worthwhile costs 0 and is therefore never refused. Wasteful — including
    an unlabelled source resolving to the default — costs POP's remaining
    minutes, `db.promotion_cost_min`, unchanged.
    """
    resolved = item_label(conn, item)
    if resolved.label == T.WORTHWHILE:
        return 0, resolved
    return db.promotion_cost_min(conn, item), resolved


# --------------------------------------------------------------------------
# Pooled consumption
# --------------------------------------------------------------------------

def _spotify_rows(conn: sqlite3.Connection, settings: Settings, week_start: str) -> list[sqlite3.Row]:
    """Positive clamped deltas polled inside the allowance week, with their episode."""
    return conn.execute(
        """
        SELECT l.spotify_id AS spotify_id, l.polled_at AS polled_at, l.delta_ms AS delta_ms,
               e.show AS show, e.platform AS platform, e.source_key AS source_key
          FROM listening l
          JOIN episodes e ON e.spotify_id = l.spotify_id
         WHERE l.polled_at >= ? AND l.polled_at < ? AND l.delta_ms > 0
           AND e.platform = 'spotify'
         ORDER BY l.polled_at, l.id
        """,
        (week_start, week_end(settings, week_start)),
    ).fetchall()


def _measurement_rows(conn: sqlite3.Connection, settings: Settings, week_start: str) -> list[sqlite3.Row]:
    """Measurements whose (local) period_start day is charged to this allowance week."""
    mweek = T.measurement_week_for_allowance_week(week_start, settings.tz)
    rows = conn.execute(
        """
        SELECT m.*, s.kind AS kind, s.source_key AS source_key, s.display_name AS display_name
          FROM measurements m
          JOIN sources s ON s.id = m.source_id
         WHERE m.period_start >= ? AND m.period_start <= ?
         ORDER BY m.period_start, m.id
        """,
        (mweek.start, mweek.end),
    ).fetchall()
    # Belt and braces: the SQL window is the measurement week, and every day in
    # it maps to this allowance week by construction. Re-check with the one
    # canonical mapping so the two can never silently disagree.
    return [
        r for r in rows
        if T.allowance_week_start_for_day(T.parse_date(r["period_start"]), settings.tz) == week_start
    ]


#: The YouTube source for videos whose channel could not be identified
#: (drbr_types.UNKNOWN_YOUTUBE_CHANNEL_KEY on branch drbr). It is a bucket,
#: not a channel, so a label on it would label unrelated channels at once.
UNKNOWN_YOUTUBE_CHANNEL_KEY = getattr(T, "UNKNOWN_YOUTUBE_CHANNEL_KEY", "unknown")


def measurement_label(conn: sqlite3.Connection, row: sqlite3.Row) -> labels.ResolvedLabel:
    """Label for one measurement row. YouTube videos honour a capture-time override.

    Several rows may exist for one source and day (Cold Turkey stores x.com and
    twitter.com under the one X source, keyed by domain); each is labelled and
    summed on its own, never assumed unique.

    The unknown-channel placeholder always resolves as unlabelled (wasteful by
    default): its source label is ignored, though an explicit per-video
    override at capture still applies.
    """
    ref = None
    source_id = row["source_id"]
    if row["kind"] == T.KIND_YOUTUBE_CHANNEL:
        if row["item_key"]:
            ref = T.item_ref("youtube", row["item_key"])
        if row["source_key"] == UNKNOWN_YOUTUBE_CHANNEL_KEY:
            source_id = None
    return labels.resolve(conn, source_id=source_id, item_ref=ref)


def spotify_label(conn: sqlite3.Connection, row: sqlite3.Row) -> labels.ResolvedLabel:
    """Label for one listening row, via its episode's show and item override."""
    source_id = _find_source_id(conn, T.KIND_PODCAST_SHOW, row["show"] or "")
    return labels.resolve(conn, source_id=source_id, item_ref=T.item_ref("spotify", row["spotify_id"]))


def pooled_minutes(conn: sqlite3.Connection, settings: Settings, week_start: str) -> PooledMinutes:
    """Every minute consumed in one allowance week, split wasteful/worthwhile. Read-only."""
    groups: dict[str, dict] = {}
    ocr = {"all": 0.0, "wasteful": 0.0}

    def bucket(group: str) -> dict:
        return groups.setdefault(group, {"wasteful_min": 0.0, "worthwhile_min": 0.0,
                                         "covered_excluded_min": 0.0, "unmeasured_items": 0})

    for row in _spotify_rows(conn, settings, week_start):
        minutes = int(row["delta_ms"]) / MIN_MS
        label = spotify_label(conn, row).label
        bucket(T.GROUP_PODCASTS)[f"{label}_min"] += minutes

    go_live = getattr(settings, "go_live", "") or ""
    for row in _measurement_rows(conn, settings, week_start):
        if go_live and row["period_end"] < go_live:
            # Evidence from before DRBR went live is kept and shown, but it is
            # not diagnostic data and must not charge the allowance: a first
            # Cold Turkey export or "Last Week" screenshot reaching back past
            # go-live would otherwise hand the first diagnostic week a debt
            # built from pre-DRBR days. YouTube already skips these at ingest
            # (DR2.2); this applies the same rule to every DRBR source.
            # Spotify is POP's own allowance and predates DRBR, so it is not
            # affected.
            continue
        group = T.report_group(row["kind"], row["device"])
        b = bucket(group)
        if T.COVERED_ELSEWHERE.get((row["kind"], row["source_key"])):
            if row["status"] == "ok":
                b["covered_excluded_min"] += float(row["minutes"])
            continue
        if row["status"] != "ok":
            b["unmeasured_items"] += 1
            continue
        label = measurement_label(conn, row).label
        b[f"{label}_min"] += float(row["minutes"])
        if row["provenance"] == T.PROV_SCREENSHOT:
            ocr["all"] += float(row["minutes"])
            if label == T.WASTEFUL:
                ocr["wasteful"] += float(row["minutes"])

    by_group = {g: GroupMinutes(**v) for g, v in sorted(groups.items())}
    return PooledMinutes(
        wasteful_min=sum(g.wasteful_min for g in by_group.values()),
        worthwhile_min=sum(g.worthwhile_min for g in by_group.values()),
        by_group=by_group,
        covered_excluded_min=sum(g.covered_excluded_min for g in by_group.values()),
        unmeasured_items=sum(g.unmeasured_items for g in by_group.values()),
        screenshot_ocr_min=ocr["all"],
        screenshot_ocr_wasteful_min=ocr["wasteful"],
    )


def gated_split(conn: sqlite3.Connection, settings: Settings,
                week_start: str) -> dict[str, GatedSplit]:
    """Per report group: queue-gated minutes versus minutes that bypassed the queue.

    Gated, and only this:
      * a Spotify delta whose episode has a queue row promoted at or before the
        poll instant (`promoted_at` is never cleared, so a lapsed promotion
        still counts — the choice was made);
      * a YouTube measurement whose `youtube:<videoId>` queue row was promoted
        on or before that local day.
    Everything else — every Screen Time and Cold Turkey minute, and any
    Spotify/YouTube consumption never promoted first — is ungated.
    Covered-elsewhere rows and unmeasured rows contribute nothing.
    """
    out = {g: {"gated_min": 0.0, "ungated_min": 0.0, "wasteful_ungated_min": 0.0}
           for g in T.REPORT_GROUPS}

    for row in _spotify_rows(conn, settings, week_start):
        minutes = int(row["delta_ms"]) / MIN_MS
        promoted = conn.execute(
            "SELECT 1 FROM queue WHERE spotify_id = ? AND promoted_at IS NOT NULL "
            "AND promoted_at <= ? LIMIT 1",
            (row["spotify_id"], row["polled_at"]),
        ).fetchone()
        _add_split(out[T.GROUP_PODCASTS], minutes, promoted is not None,
                   spotify_label(conn, row).label)

    go_live = getattr(settings, "go_live", "") or ""
    for row in _measurement_rows(conn, settings, week_start):
        if row["status"] != "ok" or T.COVERED_ELSEWHERE.get((row["kind"], row["source_key"])):
            continue
        if go_live and row["period_end"] < go_live:
            continue            # same rule as pooled_minutes: pre-go-live evidence is not diagnostic data
        group = T.report_group(row["kind"], row["device"])
        gated = False
        if row["kind"] == T.KIND_YOUTUBE_CHANNEL and row["item_key"]:
            gated = _youtube_promoted_by(conn, settings, row["item_key"], row["period_start"])
        _add_split(out[group], float(row["minutes"]), gated, measurement_label(conn, row).label)

    return {g: GatedSplit(**v) for g, v in out.items()}


def _add_split(acc: dict, minutes: float, gated: bool, label: str) -> None:
    if gated:
        acc["gated_min"] += minutes
        return
    acc["ungated_min"] += minutes
    if label == T.WASTEFUL:
        acc["wasteful_ungated_min"] += minutes


def _youtube_promoted_by(conn: sqlite3.Connection, settings: Settings, video_id: str, day: str) -> bool:
    rows = conn.execute(
        "SELECT promoted_at FROM queue WHERE spotify_id = ? AND promoted_at IS NOT NULL",
        (T.queue_key_for_youtube(video_id),),
    ).fetchall()
    return any(T.iso_date(T.local_date(r["promoted_at"], settings.tz)) <= day for r in rows)


# --------------------------------------------------------------------------
# Debt
# --------------------------------------------------------------------------

def debt_for(closed: db.Week, pooled: PooledMinutes) -> int:
    """clamp(pooled wasteful − effective allowance, 0, allowance_min).

    POP's D1.5 and D1.6 unchanged in shape; only the minutes widened from
    Spotify listening to pooled wasteful consumption (DR1.4).
    """
    raw = whole_minutes(pooled.wasteful_min) - closed.effective_allowance_min
    return max(0, min(raw, closed.allowance_min))


def on_measurements_changed(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> None:
    """Recompute `debt_min` for every week row that has a predecessor. Commits.

    Screen Time, Cold Turkey and YouTube evidence arrives after Sunday's
    rollover has already fixed a debt (DR1.5). Weeks are walked oldest first
    so a recomputed debt feeds the next week's effective allowance, exactly as
    successive rollovers would have. A week's predecessor is the most recent
    week row before it — the same rule `db.roll_over_week` closes by — and
    allowances already snapshotted in `weeks` are never rewritten.
    """
    rows = conn.execute("SELECT * FROM weeks ORDER BY week_start").fetchall()
    previous: db.Week | None = None
    for row in rows:
        week = db._to_week(row)
        if previous is not None:
            debt = debt_for(previous, pooled_minutes(conn, settings, previous.week_start))
            if debt != week.debt_min:
                conn.execute("UPDATE weeks SET debt_min = ? WHERE week_start = ?",
                             (debt, week.week_start))
                week = replace(week, debt_min=debt)
        previous = week
    conn.commit()
