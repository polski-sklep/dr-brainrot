"""allowance.py — the label-aware pooled allowance. DRBR Phase 2, Agent D.

Implements CONTRACTS.md Part II §7. POP's gate (db.promote), rollover
(db.roll_over_week) and alerts (poll.check_alerts) call into this module for
the two things DRBR changes about them: *what an item is labelled* and *which
minutes count against the allowance*.

Five rules this module exists to enforce.

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

4. **Tool is not content (DR5.5).** DRBR measures brain-rot content only. A
   source labelled `tool` (a shop, email, banking, DRBR's own dashboard) is
   out of scope: its minutes go to `tool_min` and never into wasteful,
   worthwhile, the allowance, overage, alerts or the gated split. They are
   still counted and shown, so "tool" cannot become a place to hide content.

5. **NULL is not zero.** A measurement without minutes (progress or duration
   missing, unreadable) adds nothing to any total and is counted in
   `unmeasured_items` instead, so the gap stays visible (DR1.22).
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

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
    unclassified_min: float = 0.0  # no label yet (DR5.11): counted nowhere, shown everywhere
    covered_excluded_min: float = 0.0
    unmeasured_items: int = 0
    tool_min: float = 0.0          # not content (DR5.5): shown, never charged or valued

    @property
    def charged_min(self) -> float:
        """This group's decided plus undecided minutes — what the allowance counted."""
        return self.wasteful_min + self.unclassified_min


@dataclass(frozen=True)
class PooledMinutes:
    """Everything consumed in one allowance week, split by label.

    `wasteful_min` is the only figure the allowance, debt and alerts read, and
    since DR5.11 it holds only what the user has actually called wasteful.
    `worthwhile_min` is measured and reported, never charged. `unclassified_min`
    is time on sources with no label yet: it charges nothing, because a number
    that falls when you label rather than when you consume less is not
    measuring consumption. `tool_min` is time on sources labelled tool (not
    content, DR5.5). Every minute lands in exactly one of the four.
    """

    wasteful_min: float
    worthwhile_min: float
    unclassified_min: float = 0.0
    by_group: dict[str, GroupMinutes] = field(default_factory=dict)
    covered_excluded_min: float = 0.0
    unmeasured_items: int = 0
    #: The parsed-screenshot share of the figures above (all labels, and the
    #: wasteful part). Carried so an aggregate that mixes OCR with API figures
    #: can say how much of it is lower-confidence instead of flattening it.
    screenshot_ocr_min: float = 0.0
    screenshot_ocr_wasteful_min: float = 0.0
    tool_min: float = 0.0

    @property
    def charged_min(self) -> float:
        """What the allowance, overage and alerts count: decided plus undecided.

        The reported headline is `wasteful_min` alone, because a figure that
        falls when you label rather than when you consume less is measuring the
        labelling. The allowance is a different instrument. It is a rail, and a
        rail that ignores everything it cannot identify is not a rail: in the
        week of 13 Sep the undecided minutes (312) outweighed the decided ones
        (251), so charging only the decided would have taken a week that was
        200 minutes over and called it comfortably inside (DR5.11).
        """
        return self.wasteful_min + self.unclassified_min


@dataclass(frozen=True)
class GatedSplit:
    """Consumption that passed through the queue versus consumption that bypassed it.

    Both figures include every content label (wasteful and worthwhile); tool
    minutes are not content and are left out (DR5.5). `wasteful_ungated_min` is the subset of
    `ungated_min` that was wasteful — the brief's "reflex rather than choice".
    """

    gated_min: float = 0.0
    ungated_min: float = 0.0
    wasteful_ungated_min: float = 0.0
    #: Where the counted minutes came from, so a mixed group (X: OCR and
    #: export) can say so. Collected in the same pass as the minutes.
    provenances: frozenset = field(default=frozenset(), compare=False)


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

    Worthwhile costs 0 and is therefore never refused. So does tool (only
    possible if the item's source was labelled tool; not content, never charged,
    DR5.5), unless an item override says otherwise. Wasteful — including
    an unlabelled source resolving to the default — costs POP's remaining
    minutes, `db.promotion_cost_min`, unchanged.
    """
    resolved = item_label(conn, item)
    if resolved.label in (T.WORTHWHILE, T.TOOL):
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


def _field(resolved: labels.ResolvedLabel) -> str:
    """Which bucket a resolved label counts in.

    The resolver still answers `wasteful` for an unlabelled source, because
    that is what the gate and the gate's warning need: an unrecognised thing
    is treated as brain rot until the user says otherwise. Reporting asks a
    different question — what has the user actually decided? — so it reads the
    basis, not the label, and keeps the undecided minutes apart (DR5.11).
    """
    if resolved.basis == T.BASIS_DEFAULT:
        return "unclassified_min"
    return f"{resolved.label}_min"


def pooled_minutes(conn: sqlite3.Connection, settings: Settings, week_start: str) -> PooledMinutes:
    """Every minute consumed in one allowance week, split wasteful/worthwhile/tool. Read-only."""
    groups: dict[str, dict] = {}
    ocr = {"all": 0.0, "wasteful": 0.0}

    def bucket(group: str) -> dict:
        return groups.setdefault(group, {"wasteful_min": 0.0, "worthwhile_min": 0.0,
                                         "unclassified_min": 0.0,
                                         "covered_excluded_min": 0.0, "unmeasured_items": 0,
                                         "tool_min": 0.0})

    for row in _spotify_rows(conn, settings, week_start):
        minutes = int(row["delta_ms"]) / MIN_MS
        bucket(T.GROUP_PODCASTS)[_field(spotify_label(conn, row))] += minutes

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
        resolved = measurement_label(conn, row)
        b[_field(resolved)] += float(row["minutes"])
        if resolved.label == T.TOOL:
            continue            # not content: kept out of the OCR share of content minutes too
        if row["provenance"] == T.PROV_SCREENSHOT:
            ocr["all"] += float(row["minutes"])
            if _field(resolved) == "wasteful_min":
                ocr["wasteful"] += float(row["minutes"])

    by_group = {g: GroupMinutes(**v) for g, v in sorted(groups.items())}
    return PooledMinutes(
        wasteful_min=sum(g.wasteful_min for g in by_group.values()),
        worthwhile_min=sum(g.worthwhile_min for g in by_group.values()),
        unclassified_min=sum(g.unclassified_min for g in by_group.values()),
        by_group=by_group,
        covered_excluded_min=sum(g.covered_excluded_min for g in by_group.values()),
        unmeasured_items=sum(g.unmeasured_items for g in by_group.values()),
        screenshot_ocr_min=ocr["all"],
        screenshot_ocr_wasteful_min=ocr["wasteful"],
        tool_min=sum(g.tool_min for g in by_group.values()),
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
    Covered-elsewhere rows, unmeasured rows and tool minutes (not content,
    DR5.5) contribute nothing.
    """
    out = {g: {"gated_min": 0.0, "ungated_min": 0.0, "wasteful_ungated_min": 0.0, "provenances": set()}
           for g in T.REPORT_GROUPS}

    for row in _spotify_rows(conn, settings, week_start):
        minutes = int(row["delta_ms"]) / MIN_MS
        label = spotify_label(conn, row).label
        if label == T.TOOL:
            continue
        promoted = conn.execute(
            "SELECT 1 FROM queue WHERE spotify_id = ? AND promoted_at IS NOT NULL "
            "AND promoted_at <= ? LIMIT 1",
            (row["spotify_id"], row["polled_at"]),
        ).fetchone()
        _add_split(out[T.GROUP_PODCASTS], minutes, promoted is not None, label)
        out[T.GROUP_PODCASTS]["provenances"].add(T.PROV_API)

    go_live = getattr(settings, "go_live", "") or ""
    for row in _measurement_rows(conn, settings, week_start):
        if row["status"] != "ok" or T.COVERED_ELSEWHERE.get((row["kind"], row["source_key"])):
            continue
        if go_live and row["period_end"] < go_live:
            continue            # same rule as pooled_minutes: pre-go-live evidence is not diagnostic data
        label = measurement_label(conn, row).label
        if label == T.TOOL:
            continue
        group = T.report_group(row["kind"], row["device"])
        gated = False
        if row["kind"] == T.KIND_YOUTUBE_CHANNEL and row["item_key"]:
            gated = _youtube_promoted_by(conn, settings, row["item_key"], row["period_start"])
        _add_split(out[group], float(row["minutes"]), gated, label)
        out[group]["provenances"].add(row["provenance"])

    return {g: GatedSplit(**{**v, "provenances": frozenset(v["provenances"])}) for g, v in out.items()}


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
# Overage: recorded, never carried (DECISIONS DR5.2)
# --------------------------------------------------------------------------

def overage_for(week: db.Week, pooled: PooledMinutes) -> int:
    """max(0, pooled wasteful − the week's allowance), in whole minutes.

    Only wasteful content minutes count; tool minutes never do (DR5.5).

    The user's rule: how far a week goes over is recorded, in full and
    unclamped, and never reduces any other week's allowance. This replaces
    POP's carried debt (D1.5/D1.6).
    """
    return max(0, whole_minutes(pooled.charged_min) - week.allowance_min)


def on_measurements_changed(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> None:
    """Re-record `overage_min` for every week row. Commits.

    Screen Time, Cold Turkey and YouTube evidence arrives after Sunday's
    rollover, and labels can change later, so every week's overage is
    recomputed from current evidence and labels. The current week's figure is
    "so far". Each week is judged against its own snapshotted allowance only;
    no week's figure touches another's. `debt_min` is held at 0 (DR5.2).
    """
    for row in conn.execute("SELECT * FROM weeks ORDER BY week_start").fetchall():
        week = db._to_week(row)
        overage = overage_for(week, pooled_minutes(conn, settings, week.week_start))
        if overage != week.overage_min or week.debt_min != 0:
            conn.execute("UPDATE weeks SET overage_min = ?, debt_min = 0 WHERE week_start = ?",
                         (overage, week.week_start))
    conn.commit()
