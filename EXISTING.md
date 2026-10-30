# EXISTING.md — The podcast queue as it actually is, before dr-brainrot

Phase 1 reconnaissance, 2026-09-16. Read from source, not from any brief.
Where `dr-brainrot-build-brief.md` assumed something different, the code wins and the
difference is listed in [Brief discrepancies](#brief-discrepancies).

Source read in full: `db.py`, `schema.sql`, `bot.py`, `poll.py`, `triage.py`,
`config.py`, `clock.py`, `conftest.py`, `check_env.py`, the launchd plists,
`README.md`, the queue's `CONTRACTS.md`/`DECISIONS.md` (headings plus the sections
cited below), and `spotify.py` (surface and auth). The related capture bot at
`/Users/Jacob/Projects/obsidian-ingest` was read too (`bot.py`,
`ingest_url.py`, `vault_write.py`, `processor.py`).

---

## Runtime and deployment

| Aspect | Reality |
|---|---|
| Host | **The user's Mac**, not a VPS. The README documents a VPS cron variant, but nothing runs on one. |
| Python | 3.12.14 (Homebrew), venv at `.venv/`. The queue's own deps: `python-telegram-bot` 22.8, `httpx`, `pytest`, `pytest-asyncio`. |
| Location | Was the original podcast-queue directory under `/Users/Jacob/Projects/`; moved into `/Users/Jacob/Projects/dr-brainrot` on 2026-09-16, flat at the repo root, git history kept (branch `dr-brainrot`). |
| Processes | Three launchd user agents in `launchd/`, installed in `~/Library/LaunchAgents/`. |
| `com.jacob.dr-brainrot.bot` | `bot.py`, `KeepAlive`, long polling (no inbound port, no webhook). |
| `com.jacob.dr-brainrot.poll` | `poll.py`, daily 09:17 local. |
| `com.jacob.dr-brainrot.triage` | `triage.py`, Sunday 18:00 local. |
| Ports | None. The queue binds nothing. |
| Logs | `logs/{bot,poll,triage}.log` (gitignored). httpx logging raised to WARNING so the bot token never reaches a log. |
| Env loading | `config.load()` parses `.env` (stdlib, no dotenv), then overlays allow-listed `os.environ` keys (`_KEYS`), then explicit overrides. `TZ` is deliberately ignored; the app reads `DR_BRAINROT_TZ` (currently `Europe/Warsaw`). Relative paths resolve against the repo root. |
| Test guards | `conftest.py` blocks DNS, socket connects and both httpx transports, and raises if any test reads the real `.env`. |

## Database

**SQLite**, WAL, foreign keys ON per connection. File `data/dr-brainrot.db`
(gitignored). Timestamps are ISO-8601 UTC strings with `+00:00`, so string
order is chronological order.

**Migration mechanism before dr-brainrot:** `db.init_db()` runs `schema.sql`
(idempotent `CREATE ... IF NOT EXISTS`) on every process start, then
`db.migrate()`, which was a no-op at `meta.schema_version = '1'`. There was no
migrations directory.

### Tables

| Table | Columns (type) | Keys / constraints | Written by | Read by |
|---|---|---|---|---|
| `episodes` | `spotify_id` TEXT, `title` TEXT, `show` TEXT, `description` TEXT, `release_date` TEXT, `duration_ms` INTEGER | PK `spotify_id` | `db.upsert_episode` (bot capture) | `db._ITEM_SELECT` join |
| `queue` | `id` INTEGER, `spotify_id` TEXT, `why_note` TEXT, `captured_at` TEXT, `deadline` TEXT, `state` TEXT, `cycles_seen` INTEGER, `still_holds` INTEGER, `promoted_at` TEXT, `resolved_at` TEXT | PK `id`; FK `spotify_id → episodes`; CHECK non-blank `why_note`; CHECK `state` in 6 states; idx on state, spotify_id, captured_at | `db.capture` (only INSERT), `db.set_state`, `db.record_still_holds`, `db.bump_cycle` | bot, poll, triage, `funnel_stats` |
| `listening` | `id`, `spotify_id`, `polled_at`, `position_ms`, `delta_ms`, `fully_played` | PK `id`; FK episodes; CHECK `delta_ms >= 0`; idx (spotify_id, polled_at), (polled_at) | `db.record_listening` (poll; bot seeds a zero-delta baseline at capture) | poll `_last_observation`, `promotion_cost_min`, `funnel_stats`, week re-total |
| `weeks` | `week_start` TEXT, `allowance_min`, `debt_min`, `promoted_min`, `listened_min` INTEGER | PK `week_start` (UTC instant of Sunday 18:00 local) | `_ensure_week`, `promote`, `roll_over_week`, `record_listening` | poll alerts, triage header, gate |
| `alerts_sent` | `week_start`, `kind` ('approaching'/'exceeded'), `sent_at` | PK (week_start, kind) | `poll.check_alerts` | same |
| `deadline_notices` | `queue_id`, `sent_at` | PK `queue_id`, FK queue | `poll.surface_deadlines` | same |
| `meta` | `key`, `value` | PK `key`; holds `schema_version`, `last_triage_week` | `migrate`, `triage._mark_triaged` | same |

There are **no DELETE statements** anywhere. `dropped`/`expired` are soft
deletes, visible only to `funnel_stats` and `get_item` by id.

## Queue state machine

States: `locked`, `queued`, `promoted` (active); `dropped`, `expired`,
`played` (terminal). Legal transitions are data (`db.LEGAL_TRANSITIONS`);
anything else, including same-state, raises `IllegalTransition`.

| From → To | Trigger | Where |
|---|---|---|
| (none) → `locked` | why-note reply after a link | `bot._handle_note` → `db.capture` |
| (none) → `queued` | capture whose deadline falls inside the 48h lock | `db.capture` (`_deadline_exempt`) |
| `locked` → `queued` | 48h elapsed | `db.lift_expired_locks`, called daily by poll and Sunday by triage |
| `queued` → `promoted` | Promote button, gate allows | `bot.on_callback` → `db.promote` |
| `locked` → `promoted` | only for a deadline-exempt item (lock re-checked in `promote`) | same |
| `queued`/`locked`/`promoted` → `dropped` | Drop button | `bot.on_callback` → `db.set_state` |
| `queued` → `expired` | `cycles_seen >= LIFESPAN_CYCLES` (non-deadline) or deadline passed | `triage.run_triage` step 3 |
| `promoted` → `queued` | weekly lapse of everything not played | `db.roll_over_week` step 1 |
| active → `played` | Spotify `fully_played` flag true | `poll._mark_played` |

Stamps: `promoted_at` on first promotion only, never cleared; `resolved_at` on
first entry to a terminal state.

**Lock:** `clock.is_locked(captured_at, now, LOCK_HOURS=48)`. Locked items are
not in `triage_items` and `promote` refuses them unless deadline-exempt.

**Allowance and gate** (`db.promote`): cost = remaining minutes
(`duration - last polled position`). Refuses with `AllowanceExceeded` if
`promoted_min + cost > effective_allowance`, where
`effective = max(0, allowance_min - debt_min)`. Nothing is written on refusal.

**Spend:** `weeks.promoted_min += cost` at promotion.

**Reset/rollover** (`db.roll_over_week`, triage step 1): lapse promotions;
debt for the new week = `clamp(closed.listened_min - closed.effective, 0,
closed.allowance_min)`; new week row gets fresh `allowance_min` from config.

**Debt driver:** listened minutes (sum of clamped `delta_ms` in the week
window, recomputed on every `record_listening`), not promoted minutes.

**Failure/retry:** alerts and deadline notices send first, record second, so a
Telegram failure retries tomorrow. Triage records `last_triage_week` last and
returns early on a repeat run. Spotify missing `resume_point` raises (scope
lost) rather than reading as zero. `poll` keys results by id, never by index.

## Telegram bot (`bot.py`)

- **Entry:** `bot.main()` → `build_app(settings, conn, clk, SpotifyClient)` → `run_polling`.
- **Auth:** `authorised_only` decorator drops every update not from `TELEGRAM_USER_ID`, silently.
- **Commands:** `/start`, `/queue` (read-only unlocked queue), `/stats` (delegates to `triage.render_stats`).
- **Message routing:** one `MessageHandler(TEXT & ~COMMAND)` → `on_message`. A Spotify-looking link starts a capture; otherwise text is the why-note for the pending capture.
- **Capture:** link → `spotify.get_episode` → pending capture parked in `context.user_data` (no DB write) → why-note reply → `parse_deadline` strips a trailing "by/before <date>" → `db.upsert_episode` + `db.capture` → zero-delta baseline `record_listening` if already partly heard.
- **Callbacks:** one `CallbackQueryHandler(on_callback)` for everything; data `action:item_id` with actions `promote|drop|holds_yes|holds_no`; anything else is answered "Unrecognised button."
- **Persistence boundary:** `db` module only; `conn`, `clk`, `settings`, `spotify` live in `app.bot_data`. Tests inject fakes there.
- **Where dr-brainrot hooks belong:** new capture types (YouTube link, label override) in `_handle_link`/`_handle_note`; new commands and photo/document handlers as separate handlers registered in `build_app`; inline labelling as its own callback namespace, which requires narrowing `on_callback`'s catch-all.
- **Errors:** `on_error` logs a token-scrubbed traceback and tells the authorised user a fixed failure string.

## Spotify (`spotify.py`)

- **Auth:** client id/secret + long-lived refresh token (from `auth_spotify.py`, one-time consent with scope `user-read-playback-position`). Access token minted on demand and cached with expiry; 401 invalidates and retries once.
- **Polling:** `poll.run_poll` asks `get_episodes(polling_targets)` for every locked/queued/promoted episode (batched, 50 per call, falls back to single fetch on errors). Results keyed by id.
- **`resume_point`:** `resume_position_ms` and `fully_played` per episode; a missing `resume_point` raises `SpotifyScopeError`.
- **Persistence:** `record_listening` with `clamped_delta = max(0, current - previous)`; unchanged observations are skipped.
- **Errors:** `SpotifyError`/`SpotifyForbidden`/`SpotifyScopeError`; `InvalidEpisodeRef` (a `ValueError`) for bad links, shown verbatim to the user.

## Gate and allowance: call sites affected by label-conditional gating

| Concern | Location |
|---|---|
| Lock applied | `db.capture` (state), `clock.is_locked`, `db.triage_items`, `db.promote` lock re-check |
| Eligibility checked | `db.promote` (`promoted_min + cost > effective`) |
| Allowance spent | `db.promote` (`UPDATE weeks SET promoted_min = ...`) |
| Cost computed | `db.promotion_cost_min` |
| Allowance reset | `db.roll_over_week` (new row), `db.current_week` / `_ensure_week` (first touch) |
| Debt created/carried | `db.roll_over_week` from `listened_min`; `weeks.listened_min` maintained by `db.record_listening` |
| Consumption alerts | `poll.check_alerts` (reads `listened_min` vs effective) |
| User-facing numbers | `bot.on_callback` (AllowanceExceeded text), `triage.render_item` / `render_week_header` (remaining), `poll._approaching_text`/`_exceeded_text` |
| Funnel | `db.funnel_stats` (reads `listening` for "played") |

## The other bot: obsidian-ingest

- Separate repo `/Users/Jacob/Projects/obsidian-ingest`, separate venv and token, launchd `com.jacob.obsidian-ingest` (KeepAlive), **no tests**.
- `bot.py` debounces text bursts (3s), then `ingest_url.ingest(raw)` classifies (tweet, x-article, YouTube, article, text, curriculum), extracts, optionally cleans an article via the Anthropic API, and `vault_write.route_note` + `write_note` writes `<VAULT_PATH>/<folder>/<title>.md` with rendered frontmatter.
- `VAULT_PATH` defaults to `<vault path>` (obsidian-ingest's own default). Almost everything lands in `01-Inbox/Read/...`.
- No capture id, no log of what was written other than `bot.log`.

## Brief discrepancies

| # | Brief assumed | Reality | Consequence for dr-brainrot |
|---|---|---|---|
| 1 | Backend on a VPS, reached over Tailscale | Everything runs on the Mac under launchd | Vault scan and extension ingest run locally; no sync agent (DR0.3). Dashboard on 127.0.0.1 is reachable on the Mac itself. |
| 2 | One existing Telegram bot that also writes Obsidian notes | Two bots: the podcast queue's and obsidian-ingest (vault writer) | Capture IDs are added to obsidian-ingest. The queue, screenshots, exports and labels go through the queue's bot. Neither bot is replaced. |
| 3 | Vault may need syncing to the server | Local git repo at `<vault path>` (obsidian-ingest's own default) | Scanned in place. |
| 4 | Ungated/impulse consumption is measured for every source | The queue polls only **captured** episodes; podcast listening that never touched the queue is invisible | Reported as a named blind spot for podcasts; not silently absorbed. |
| 5 | "Existing lock and allocation ceiling" is a simple minute cap | Cap is on *promotion* (remaining minutes); *debt* comes from *listened* minutes, clamped at one allowance | dr-brainrot keeps both mechanisms and makes each label-aware. |
| 6 | Weekly timing is generic | Week = Sunday 18:00 `Europe/Warsaw`, DST-correct; the original docs still mention Lisbon in places | dr-brainrot maps its Sunday–Saturday data weeks onto this boundary (DR1.7). |
| 7 | The queue stores a podcast show identity | Only the show **name** is stored | Podcast sources are keyed by normalised show name. |
| 8 | A migration mechanism exists | Hook only (no-op), no migrations dir | Added `migrations/NNNN_*.sql`, transactional per step. |
| 9 | Repo layout `dr-brainrot/pop/` | The queue's modules import each other flat and resolve `.env`/data from their own directory | The queue kept flat at the repo root (DR0.2). |
| 10 | Allowance 180 min | User set 360 min pooled | `WEEKLY_ALLOWANCE_MIN=360`. |
| 11 | YouTube progress read from the rendered DOM overlay | The saved page carries the same value as `thumbnailOverlayProgressBarViewModel.startPercent` inside `ytInitialData`; later pages arrive as `/youtubei/v1/browse` continuation JSON | Parser reads the page data, not pixels and not the rendered DOM (DR1.12). |
| 12 | Cold Turkey export gives per-site usage | The real export lists only the chart's top domains (5 here), minutes as floats per UTC-midnight day | Unlisted domains are a stated blind spot. |
| 13 | One Screen Time screenshot | A scrolled sequence (10 images): Most Used (app minutes), Pickups, Notifications, and only the top 3 categories | Parser takes a batch; per-category data limited to what's shown. |
| 14 | The queue's catch-all callback handler is an extension point | It answers every callback, so a second namespace needed narrowing | dr-brainrot reserves the `d:` prefix. |
| 15 | `played` is a generic completion signal | It is Spotify's `fully_played` flag; YouTube has no equivalent | YouTube queue items never auto-transition to `played` (DR1.14). |
