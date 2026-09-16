# NOTES-A — YouTube (Phase 2, Agent A)

Scope: `sources/youtube.py`, `ingest_api.py`, `extension/**`,
`tests/test_youtube.py`, `tests/test_youtube_real_input.py`,
`tests/test_ingest_api.py`, `tests/fixtures/youtube/**`.

Built against CONTRACTS.md Part II §6.1 at f0c456e. (The worktree was first
created from `main` at 822d513; it was reset to f0c456e before any work.)

---

## Contract-change proposals (not applied; for the integration pass)

### P1. `startPercent` has a 10% floor. Use `startTimeSeconds` when present.

Found in the real page. Every regular video's `watchEndpoint` (under
`rendererContext.commandContext.onTap`) can carry `startTimeSeconds`, the
account's resume position in seconds. Compared with the thumbnail badge
duration on the real page:

- 69 progress values: 22 are exactly `10`, 25 are `100`, the rest in between.
- All 17 videos showing `10` that also have `startTimeSeconds` have a true
  position of **0.3%–7.6%** (e.g. 57 s into a 2849 s video shows 10%).
- Above the floor the two agree, and `startPercent` is the true fraction
  rounded down (613/1351 = 45.4 → 45; 277/433 = 63.97 → 63).
- 5 videos show `10` with no `startTimeSeconds`; I don't know what that means.
- Videos at `100` never carry `startTimeSeconds`.

Under the contract formula (`percent/100 × duration`), a video you only
started counts as 10% of its length. On a 47-minute video that is 4.7 min
instead of about 1. **I implemented the contract as written.** Each
measurement's `detail.resume_s` stores `startTimeSeconds`, so a later reparse
can switch formulas without asking for the evidence again. Proposed rule:
`minutes = min(resume_s, duration_s)/60` when `resume_s` is present and
`percent < 100`; otherwise the percent formula. This reads a value already on
the page and involves no inference. If adopted, bump `PARSER_VERSION` to
`youtube-2`. The change is about three lines in `_write_fact`.

### P2. Repeat listings: one fact per regular video per payload.

The contract says "period = the watch date". On the real page, a video appears
under **every** day it was watched: 7 videos repeat, one of them 3 times, some
of them twice on the same day. Each repeat shows **the same** progress value
(the account's current position), so it is state, not a per-viewing reading.
Writing `percent × duration` under each date would turn one position into
several viewings. Implemented:

- **Regular video:** one measurement per payload, under its **newest** date.
  The other dates go in `detail.also_listed_on`. This matches the brief's
  accepted undercount ("Rewatches overwrite position… Accepted").
- **Short:** one measurement per distinct day. A Short has no position, and
  each day it is listed is a separate viewing assumed complete. Several
  listings on the same day collapse to one.

This needs recording centrally (DECISIONS). Known residual: suppose a regular
video was measured on day D1 by one import. If a later *overlapping* import
sees it only under D2, both rows exist. That needs a capture that re-covers
already-synced days, which normally only happens on a first sync or when the
cursor wasn't reached.

### P3. Cursor carries a date (additive).

History relists a video when it is rewatched. If the cursor video is rewatched
it reappears at the top, and a cursor that is only an id would stop the next
sync immediately and drop the whole week. Added meta key
`youtube_cursor_date`, set together with `youtube_cursor`. A cursor match
needs the same id **and** a listing date ≤ the stored date.
`current_cursor(conn)` is unchanged. Also added
`current_cursor_date(conn)`, and `GET /api/youtube/cursor` returns
`{"cursor", "cursor_date"}`. Other rules:

- The cursor never moves backwards (an old capture posted late).
- `reparse` re-applies the cursor recorded in the original import's `meta`
  (`cursor_applied`). The stored cursor has usually moved on and would skip
  everything.
- `reparse` only advances the global cursor if nothing has moved it since that
  import.

### P4. First sync backfills weeks and moves `diagnostic_start` back.

The initial `ytInitialData` alone covers about 3.5 weeks (2026-08-24 to
2026-09-16 on the real page). On the first import (no cursor) every entry is
measured, and `evidence.finish_import` fixes `diagnostic_start` to the
**oldest** watch day. That starts the eight-week clock weeks before the tool
existed. The extension caps first-sync *scrolling* at about 14 days, but the
initial page already goes further back. Options for integration:

- (a) seed the diagnostic start before the first YouTube sync;
- (b) have `ingest` skip entries before `diagnostic_start` or before a
  first-sync horizon;
- (c) accept it as baseline.

I did not choose, because this sets when the diagnostic starts.

### P5. Minor additive details

- `WatchEntry` gains defaulted fields `resume_s` and `live_badge`. Existing
  constructor calls with the contract's 8 fields still work.
- `detail` JSON keys: `percent`, `duration_s`, `is_short`, `assumed_full`,
  `page_channel_id`, `resume_s`, `live_badge`, `also_listed_on`, `page_order`.
- `POST /api/youtube/history` returns **200** for ok/partial/duplicate and
  **422** for a failed import. The body is the Outcome either way.
  Unauthorised is 401 and body over 25 MB is 413; neither records an import.
- Extra helper `extract_initial_data(html)` handles saved pages and the local
  real-input test.
- `DataApiClient` sends the key in the `X-Goog-Api-Key` header, not
  `?key=`. httpx logs full request URLs at INFO, so a key in the URL would
  leak into logs.
- **Unknown channel source.** Sometimes the Data API returns nothing for a
  video (removed or private) and the page names no single channel. That
  happens for Shorts and collaborations. The measurement is then
  `duration_missing` under source `youtube_channel` / key `unknown`
  ("Unknown channel (video unavailable in the Data API)"). **Agent C:**
  `sources_needing_label` will offer this source for labelling unless it is
  filtered out; suggest excluding `source_key = 'unknown'` for
  `youtube_channel`.

---

## Real-data facts verified (inputs/Watch history - YouTube.html)

- `var ytInitialData = {...};` in an inline script. 23 `itemSectionRenderer`
  sections sit under
  `contents.twoColumnBrowseResultsRenderer.tabs[0].tabRenderer.content.sectionListRenderer.contents`,
  followed by one `continuationItemRenderer` (`continuationCommand.token`,
  apiUrl `/youtubei/v1/browse`).
- Headers are `itemSectionHeaderRenderer.title`: `{"runs":[{"text":"Today"}]}`,
  `{"runs":[{"text":"Yesterday"}]}`, then `{"simpleText":"Monday"}` …
  `"Thursday"`, then `"Sep 9"` (exactly 7 days back) and older `"Mon D"`.
  No explicit years on this page.
- Regular items are `lockupViewModel`, 70 of them, all
  `LOCKUP_CONTENT_TYPE_VIDEO`:
  - Video id is `contentId`.
  - Title is `metadata.lockupMetadataViewModel.title.content`.
  - Channel name is `metadataRows[0].metadataParts[0].text.content`.
  - Channel id is in
    `image.decoratedAvatarViewModel...browseEndpoint.browseId` (64 items).
  - 6 items are **collaborations** with `avatarStackViewModel` listing 2
    channels. The page channel is recorded as None there; the API channel is
    authoritative.
- Progress is at
  `contentImage.thumbnailViewModel.overlays[].thumbnailBottomOverlayViewModel.progressBar.thumbnailOverlayProgressBarViewModel.startPercent`.
  69 of 70 items have it. The one without is a **LIVE** stream (badge style
  `THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE`). It is stored as `progress_missing`,
  with the warning saying "listed as LIVE".
- Shorts: 18 `reelShelfRenderer` shelves with 130 `shortsLockupViewModel`
  items in total. The id is `onTap.innertubeCommand.reelWatchEndpoint.videoId`
  and the title is `overlayMetadata.primaryText.content`. There is no
  progress and no plain channel id (`params` appears to encode one in
  protobuf, deliberately not decoded).
- The real page parses with **zero warnings**
  (`tests/test_youtube_real_input.py`, which skips when the file is absent).

## Unverified against real data

- **The continuation envelope.** Only the initial page was provided.
  `tests/fixtures/youtube/history_continuation.json` wraps real, sanitised
  sections in `onResponseReceivedActions[].appendContinuationItemsAction.continuationItems`.
  That is the standard browse continuation shape, but it **was not observed in
  a real capture**. The parser walks JSON generically, so any wrapper that
  still contains `itemSectionRenderer` sections will parse.
- **Missing headers on continuation pages.** If a date group split across
  pages arrives without its header, the import fails loudly ("no date
  header"). The raw file is kept, so a parser fix plus `reparse` recovers it.
  I did not inherit the previous section's date, because that would be a
  guess.
- **Browser time zone.** "Today"/"Monday" are resolved in `settings.tz`
  (Europe/Warsaw), from the page-load instant the extension reports. If the
  browser runs in another zone, relative headers can be off by a day near
  midnight.
- **UI language.** Only English headers are recognised. Anything else fails
  the import, by design.
- **`P0D` means a live or upcoming stream,** per API docs; it maps to
  `duration_missing`, never 0. This was not checked against the live API
  (tests have no network).
- **`X-Goog-Api-Key` header auth.** It is documented for Google APIs but was
  not exercised live.
- **The extension has not been run in a real browser** in this session. The
  MAIN-world collector (stop conditions, cursor-with-date check, first-sync
  horizon, page cap) was exercised in a Node `vm` harness against the
  fixtures; all four cases behaved as intended. Parts that need a real check:
  - the `ytInitialData` accessor trap (there is a fallback that re-parses the
    inline script);
  - fetch/XHR interception of `/youtubei/v1/browse`;
  - scroll-triggered continuation loading;
  - service-worker lifetime during a long scroll.
- **Payload size.** The real initial page is 2.3 MB of compact JSON. The
  default page cap of 8 stays under the 25 MB body limit if continuation pages
  are similar or smaller (unverified).

## Temptations rejected (the user labels, the system counts)

- Reading duration from the thumbnail badge text ("22:31") instead of the
  Data API. It is display text and absent for some items.
- Treating `10%` as "barely started" and scaling it down without
  `startTimeSeconds` (see P1: only the direct value is proposed).
- Summing repeat listings as separate viewings, or dividing a position across
  them (see P2).
- Decoding the channel from a Short's base64 `params`.
- Any "watched" / "played" threshold on percent (DR1.14).
- Guessing a channel for API-unavailable Shorts.
- Inheriting a date for a header-less section.

## Integration notes

**Dashboard (Phase 3).** Mount the blueprint in the Flask app bound to
`127.0.0.1:settings.dashboard_port`:

```python
from clock import RealClock
from sources import youtube
import ingest_api
app.register_blueprint(ingest_api.make_blueprint(
    get_conn=get_request_conn,          # e.g. flask.g-cached db.connect(settings); closed in teardown
    clk=RealClock(), settings=settings,
    api_factory=lambda: youtube.DataApiClient(settings)))
```

- `get_conn()` must return a connection usable on the request thread.
  `db.connect` uses sqlite's default `check_same_thread=True`, and Flask's
  server is threaded, so use a per-request connection. The blueprint never
  closes it.
- `api_factory` is called once per authorised POST, and `close()` is called
  afterwards.
- If the app sets `MAX_CONTENT_LENGTH`, it must be ≥ 25 MB, or Flask rejects
  captures before the blueprint's own limit applies.
- The endpoint is protected by `DRBR_INGEST_TOKEN`. An empty token refuses
  every request, so `check_env.py` should require it, along with
  `YOUTUBE_API_KEY`.

**bot.py (Agent D).**

- Construct the client with
  `context.bot_data["youtube"] = youtube.DataApiClient(settings)`.
- `video_info(video_id)` returns `VideoInfo | None` and raises
  `drbr_types.VideoLookupError`.
- `duration_s` is None for live, upcoming or removed videos. Refuse capture in
  that case, as §7 says.
- Close the client on shutdown.
- Tests use `sources.youtube.FakeYouTubeApi`.

**Re-parsing.** `youtube.reparse(conn, clk, settings, import_id, api)` works
for any `youtube_history` import, including failed ones.

**Prompts (Agent B, `send_due_prompts`).** A YouTube import's
`period_start`/`period_end` are the min/max *watch dates written*, not a full
Sunday..Saturday week. An import with nothing new has period NULL and status
`ok`. "Covers the week" should probably be judged by `received_at` (a sync on
or after the Monday following the week), not by period coverage.

**Report (Agent D) blind spots to surface:**

- `progress_missing` counts (live streams);
- `duration_missing` counts (API unavailable);
- the 10% floor (P1) until it is resolved;
- a partial import whose warning says the cursor wasn't reached (possible
  gap).

**Fixtures.**

- `tests/fixtures/youtube/sanitise_real_page.py` regenerates both fixtures
  from the real page.
- Ids, titles, channel names, URLs and free text are replaced, bulky subtrees
  the parser never reads are dropped, and progress values are set to chosen
  synthetic numbers.
- Before writing, the script fails if any real video id, channel id or title
  from the *whole* page survives.
- It prints only counts.

**Environment.** `.env` needs `YOUTUBE_API_KEY` and `DRBR_INGEST_TOKEN`.
`DRBR_DASHBOARD_PORT` defaults to 8750, which is also the extension's default
backend URL.
