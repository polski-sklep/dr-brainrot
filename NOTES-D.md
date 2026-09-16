# NOTES-D — Gate + reporting (Agent D, DRBR Phase 2)

Branch `worktree-agent-a803254386310f8af`, based on `f0c456e` (the worktree was
created from `main`; it was reset to `f0c456e` before any work, with no local
changes to lose).

## Contract proposals / concerns (not applied to shared files)

1. **Part I §2 `QueueItem` gained two trailing fields**, `platform: str = "spotify"`
   and `source_key: str | None = None`, filled from `episodes`. Defaults keep
   every POP constructor call valid. §7's `item_label` needs them. Please add
   to CONTRACTS.md §2.
2. **Part I §5 render signatures widened with keyword-only `label=None`**:
   `render_item(item, week, warning, *, label=None)` and
   `render_deadline_notice(item, week, *, label=None)`. These functions take no
   connection, so the resolved label is passed in; `None` renders the POP post
   exactly. `render_week_header(week, *, spent_wasteful_min=None,
   last_week_wasteful_min=None)` likewise.
3. **`bot.build_app(..., *, youtube=None)`**: optional client parked in
   `bot_data["youtube"]`.
4. **`allowance` exposes helpers beyond §7**: `whole_minutes` (POP's
   ties-down rounding for float minutes, used by debt *and* alerts so they
   never disagree), `week_end`, `ensure_item_source` (identity row for a queue
   item's show/channel; never a label), `spotify_label`, `measurement_label`,
   dataclasses `GroupMinutes`, `GatedSplit`. `report.py` also reads
   `allowance._spotify_rows/_measurement_rows` (same owner).
   `GroupMinutes` = `(wasteful_min, worthwhile_min, covered_excluded_min,
   unmeasured_items)` — §7 names the type but not its fields.
5. **§9 type details chosen**: `SourceTotal.minutes` and `KeeperRow.minutes`
   are `float | None` (None = no evidence arrived / no minutes source; never a
   stand-in zero). `RecordRow` fields are defined in `report.py`.
6. **Retroactive relabelling (needs a decision).** `on_measurements_changed`
   recomputes every week's debt from labels *as they are now*. A source
   relabelled worthwhile today therefore silently lowers past debts at the
   next import, but not before. Either (a) `handlers_labels` also calls
   `allowance.on_measurements_changed` after `set_source_label` so it is at
   least consistent and immediate, or (b) debt freezes once the following
   week closes. I implemented the contract as written; (a) is one line for C
   or integration.
7. **Historical weeks on the live DB.** The first DRBR import will run
   `on_measurements_changed` over POP-era `weeks` rows. With every show still
   unlabelled (so wasteful) the recomputed debt equals POP's, *except* where
   week rows have gaps: POP's `listened_min` window ran to the next existing
   week row, while pooled Spotify minutes use the §1 instant attribution
   (`week_start_for(polled_at)`, i.e. exactly seven local days). Integration
   should diff `weeks.debt_min` on a copy of the real DB before and after one
   `on_measurements_changed` call.

## Decisions

- **Gate** (`db.promote`): lock re-check first (unchanged, every label), then
  `allowance.promotion_cost`. Worthwhile: cost 0, the refusal check is skipped
  entirely (not just `+0`), so a worthwhile item promotes even when debt has
  raised `promoted_min` above the effective allowance. Wasteful: POP's path
  byte-for-byte. POP's `promotion_cost_min` is untouched.
- **Pooled minutes** are floats; debt and alerts compare
  `whole_minutes(pooled.wasteful_min)` (ceil(x − 0.5), identical to
  `ms_to_min` for Spotify-only weeks — tested).
- **Reads never write.** `item_label`/`pooled_minutes` look sources up without
  creating them (absent = unlabelled = wasteful default, the same answer).
  Source identity rows are created at bot capture and backfilled daily by
  `poll.run_poll` for active items, so pre-DRBR queue shows can reach the label
  batch (`labels.sources_needing_label` needs a `sources` row).
- **Measurements → week**: by `period_start` through
  `allowance_week_start_for_day` (a Screen Time Sunday..Saturday row maps via
  its Sunday). Covered-elsewhere rows accumulate in `covered_excluded_min`;
  non-ok rows only in `unmeasured_items`.
- **`weeks.listened_min`** keeps POP's meaning (raw Spotify, all labels).
- **Gated Spotify** = a queue row for the episode with `promoted_at <=
  polled_at`. `promoted_at` is never cleared, so listening after a lapse still
  counts as gated (the choice was made). YouTube gated = promoted on/before the
  measurement's local day.
- **`funnel_stats` "played" for YouTube** = an ok `youtube_channel`
  measurement of that video id with `minutes > 0` and `period_start >=` the
  local date of `promoted_at`. No percentage threshold. `fully_played` stays 0
  for YouTube (never auto-played). Spotify path unchanged.
- **Alerts** text: "`N` of `M` min used this week across sources (wasteful
  only). `L` min left." / "Over the allowance: `N` min used across sources
  (wasteful only) against `M` min. `X` min over — that comes off next week."
  Still one of each kind per week via `alerts_sent`.
- **Triage post**: label line directly below the why-note (the note stays
  directly under the duration): `Label: <label> (<basis>)`, plus
  `· costs 0 min — worthwhile`. Header adds "Last week spent N min wasteful
  across sources" and "Spent so far N min wasteful across sources"; "Remaining"
  keeps POP's promotable meaning. `/stats` adds one final line pointing to the
  dashboard.
- **Bot YouTube capture**: strict 11-char id from `youtube.com/watch?…v=`,
  `m.youtube.com`, `youtu.be/`, `/shorts/` (link may sit inside text). A
  YouTube-looking message is always treated as a link attempt, so a bad one is
  never swallowed as a note. Refusals (not a video, not configured, lookup
  error, no such video, `duration_s is None`) reply and write nothing, and
  leave any pending capture alone. The why-note goes through
  `parse_override_token` first, then `parse_deadline`, so tag-before and
  tag-after-deadline both work; a note that is only a tag is refused like a
  bare date. The override is written only after `db.capture` succeeds.
  Confirmation: `Label: <label> (your tag in this note | the source's label |
  source not labelled yet, so wasteful by default)`, plus a "Costs 0 min"
  line for worthwhile. No Spotify-style listening baseline for YouTube.
  `/start` text mentions YouTube and the tags.
- **Report**: `weekly_totals` emits a `minutes=None, import_status='missing'`
  row for YouTube/Screen Time/Cold Turkey when no ok/partial import overlaps
  the measurement week; a present import with no rows is a real `0.0`.
  `allowance_status` never creates the week row: if absent, debt is what
  rollover would carry. `remaining_min` = effective − whole(spent wasteful)
  (consumption); `promoted_min` alongside. `cost_per_keeper` ranks ascending
  (cheapest per keeper first), `None` last; `web`/`other` rows appear only
  when they have keepers, with `minutes=None`. `VaultFunnel.captured` =
  unprocessed + kept (latest scan per capture); never-scanned captures are not
  counted. `blind_spots` lists missing weekly evidence for every measurement
  week from the diagnostic start (or just the last complete week before it
  starts). `records` covers Spotify listening and measurements (vault rows are
  counts and live in `funnels`).

## POP test assertions changed (and why)

All are fixture/setup changes forced by Part II; no POP behaviour assertion
was weakened.

1. `tests/test_poll.py` — five tests (`check_alerts_is_safe…`,
   `zero_effective_allowance…`, `exactly_eighty_percent…` (twice),
   `alerts_go_to_the_authorised_user`, `failed_send_leaves_the_alert…`;
   six statements) hand-set `UPDATE weeks SET listened_min = N`. Alerts now read pooled wasteful
   minutes computed from evidence (§7), so each now calls a `use_minutes(conn,
   clk, N)` helper that records equivalent listening on an unlabelled filler
   episode. Thresholds and expected messages unchanged (the assertions check
   "Over the allowance", "150", "13 min over", which the new text keeps).
   Note: `alerts_go_to_the_authorised_user` had become vacuous (no alert sent,
   `all([])`) before this fix.
2. `tests/test_integration.py::test_debt_shrinks_the_gate_not_just_the_report`
   hand-set `listened_min = 260`; now records 260 min of listening on episode B
   (not A, whose position would change A's promotion cost). Debt 80 unchanged.
3. `tests/test_bot.py` `ctx` fixture: `"conn": object()` → a real in-memory
   schema connection built from `schema.sql` + `migrations/` (no db.py
   import). FakeDB still handles all queue writes; the capture path now
   resolves the label and records the source, which needs the label tables.
   No assertion changed; `test_full_capture_writes_only_after_the_note` still
   sees exactly `["upsert_episode", "capture"]` on FakeDB.

## Temptations recorded (rejected)

- Declaring a YouTube queue item `played` above some watch percentage, or
  using watched minutes ≥ duration as completion (DR1.14).
- Treating a promoted-then-lapsed Spotify item's later listening as ungated
  because the promotion "expired" — that would be inferring intent.
- Defaulting unknown YouTube durations to an average, or letting capture
  proceed at cost 0 when the duration is unknown.
- Filling `SourceTotal.minutes` with 0 when an import is missing, or giving
  no-keeper groups an "infinite" or very large price so they sort to the top.
- Caching resolved labels per episode across calls in `pooled_minutes`
  (only a per-row fresh `labels.resolve` is used).
- Pro-rating a Sunday-before-18:00 day between two allowance weeks.
- Using `still_holds` or promotion as a proxy label for worthwhile.

## Integration to-dos

- **Construct the YouTube client in `bot.main`** — done defensively:
  `_youtube_client(settings)` imports `sources.youtube.DataApiClient` when
  `YOUTUBE_API_KEY` is set, else YouTube capture replies "not set up". Verify
  `DataApiClient.video_info` raises `drbr_types.VideoLookupError` (not a
  generic httpx error) on network/quota failures; the bot catches only that,
  anything else reaches `on_error`.
- **`jobs.py`** does not need anything from D; `poll.py`/`triage.py` schedules
  are unchanged.
- **Dashboard** consumes `report.*` only; `report.cost_per_keeper(since=...)`
  accepts a local date or an instant.
- Resolve concern 6 (relabel → debt recompute) with Agent C / `handlers_labels`.
- Run concern 7's diff on a copy of `data/pop.db` before enabling imports.
- Update CONTRACTS.md with concerns 1–5.
