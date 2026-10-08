# CONTRACTS

**Part I** (below, unchanged) is POP's original contract. **Part II**, at the
end of this file, is DRBR's. Where Part II changes a Part I behaviour it says
so explicitly; otherwise Part I still holds.

---

# Part I — POP

Phase 1 output. Every module in Phases 2 and 3 implements exactly these
signatures and imports other modules only through them. Do not widen a
signature without updating this file first.

Shared modules already written in Phase 1 and owned by nobody in Phase 2:
`config.py`, `clock.py`, `schema.sql`.

---

## 0. Conventions

- **Timestamps.** Every stored timestamp is `clock.iso(dt)` — ISO-8601 UTC with
  an explicit `+00:00`. Fixed width, so SQL string comparison is chronological
  comparison. Parse with `clock.parse(s)`. Never store naive local time.
- **Time is injected.** Anything that needs "now" takes a `clock.Clock`. Tests
  pass `clock.FrozenClock`. No module calls `datetime.now()` directly.
- **Minutes vs milliseconds.** Spotify speaks milliseconds; the allowance
  speaks minutes. Convert at the boundary with `ms_to_min()` (round half up)
  and keep `_ms` / `_min` suffixes honest.
- **No live calls off the happy path.** `spotify.py` is the only module that
  performs HTTP. Everything else takes a `SpotifyClient`-shaped object, so
  tests inject `FakeSpotify`.

```python
def ms_to_min(ms: int) -> int:      # db.py owns the canonical implementation
    return (ms + 29_999) // 60_000  # nearest minute, ties DOWN (30.000s remainder -> 0)
```

The tie direction is deliberate and pinned by a test. It only differs from
ties-up at an exact 30.000-second remainder, and cross-module agreement on what
a minute is matters far more here than which way that one case falls.

---

## 1. The state machine

`queue.state` is the funnel. Six states, three of them terminal.

| state | terminal | meaning |
|---|---|---|
| `locked` | no | captured, inside the 48h lock, invisible to triage |
| `queued` | no | lock lifted, eligible for triage |
| `promoted` | no | in the playable slate, cost charged against the allowance |
| `dropped` | **yes** | rejected at triage — soft deleted |
| `expired` | **yes** | lifespan or deadline ran out — soft deleted |
| `played` | **yes** | `fully_played` observed |

### Legal transitions

```
locked   → queued      lock expired (lift_expired_locks)
locked   → promoted    deadline falls inside the lock, so the item is lock-exempt
locked   → dropped     user dropped a deadline-surfaced item
locked   → expired     deadline passed while still locked
locked   → played      listened out of band before the lock lifted

queued   → promoted    triage promotion, allocation gate permitting
queued   → dropped     triage drop
queued   → expired     lifespan exhausted, or deadline passed
queued   → played      listened out of band without being promoted

promoted → queued      weekly lapse at reset (not fully played)
promoted → dropped     user dropped a promoted item
promoted → expired     deadline passed while promoted
promoted → played      fully_played observed
```

Everything else is illegal, **including any transition out of a terminal
state**. `db.set_state()` raises `IllegalTransition` rather than silently
passing. A no-op transition to the same state also raises: it always means a
caller lost track of what it was doing.

```python
ACTIVE_STATES  = ("locked", "queued", "promoted")
TERMINAL_STATES = ("dropped", "expired", "played")
REMOVED_STATES = ("dropped", "expired")   # soft-deleted: hidden from every default query
```

### Soft delete

`dropped` and `expired` rows stay in the database forever and are excluded from
**every** default query. The only way to see them is `include_removed=True`,
which exists solely for the stats path. `played` is terminal but is *not*
soft-deleted — it is the success outcome and appears in stats normally, though
it never appears in triage.

There are no `DELETE` statements anywhere in this codebase. Phase 4 audits for
this.

---

## 2. `db.py` — Agent A

```python
class IllegalTransition(Exception): ...
class AllowanceExceeded(Exception):
    """Raised by promote() when the gate refuses. Carries the overage."""
    def __init__(self, message: str, overage_min: int, remaining_min: int, cost_min: int): ...

@dataclass(frozen=True)
class QueueItem:
    id: int
    spotify_id: str
    title: str
    show: str
    duration_ms: int
    why_note: str
    captured_at: str
    deadline: str | None
    state: str
    cycles_seen: int
    still_holds: int | None
    promoted_at: str | None
    resolved_at: str | None

@dataclass(frozen=True)
class Week:
    week_start: str
    allowance_min: int
    debt_min: int
    promoted_min: int
    listened_min: int
    @property
    def effective_allowance_min(self) -> int: ...   # allowance_min - debt_min, floored at 0
    @property
    def remaining_min(self) -> int: ...             # effective_allowance_min - promoted_min
```

### Connection and lifecycle

```python
def connect(settings: Settings) -> sqlite3.Connection
def init_db(conn) -> None                      # applies schema.sql; idempotent
def migrate(conn) -> None                      # no-ops at schema_version 1
```

Rows come back as `sqlite3.Row`. `foreign_keys` is ON.

### Capture

```python
def upsert_episode(conn, spotify_id: str, title: str, show: str,
                   description: str | None, release_date: str | None,
                   duration_ms: int) -> None

def capture(conn, clk: Clock, settings: Settings, *, spotify_id: str,
            why_note: str, deadline: str | None = None) -> QueueItem
```

`capture()` **requires** a non-empty `why_note` and raises `ValueError` on
empty or whitespace-only input. There is no code path that writes a `queue` row
without one. This is why the bot holds the capture in memory until the reply
arrives — see §4.

Initial state is `locked`, unless a `deadline` falls inside the lock window, in
which case the item is created `queued` (deadline exemption, per the brief).

### Reading

Every one of these excludes `REMOVED_STATES` unless told otherwise.

```python
def get_item(conn, item_id: int) -> QueueItem | None
def active_items(conn) -> list[QueueItem]                       # locked+queued+promoted
def triage_items(conn, clk, settings) -> list[QueueItem]        # queued only, lock already lifted
def promoted_items(conn) -> list[QueueItem]
def polling_targets(conn) -> list[str]                          # distinct spotify_ids worth polling
def all_items(conn, *, include_removed: bool = False) -> list[QueueItem]
```

`polling_targets()` returns episodes in `locked`, `queued` or `promoted`. Out-of-band
listening on a queued item is exactly the behaviour the brief wants visible, so
polling is not restricted to the promoted slate.

### State changes

```python
def set_state(conn, clk, item_id: int, new_state: str) -> QueueItem
def lift_expired_locks(conn, clk, settings) -> list[QueueItem]   # locked → queued
def record_still_holds(conn, item_id: int, answer: bool) -> QueueItem
def bump_cycle(conn, item_id: int) -> QueueItem                  # cycles_seen += 1
```

`set_state()` stamps `resolved_at` when entering a terminal state and
`promoted_at` on the **first** promotion only. `promoted_at` is never cleared,
including on lapse — it is the evidence for funnel stage 2.

### Weeks, allowance, debt

```python
def current_week(conn, clk, settings) -> Week          # creates the row if absent
def promotion_cost_min(conn, item: QueueItem) -> int   # remaining, not full duration
def promote(conn, clk, settings, item_id: int) -> QueueItem
def roll_over_week(conn, clk, settings) -> tuple[Week, Week]   # (closed, opened)
def record_listening(conn, clk, spotify_id: str, position_ms: int,
                     delta_ms: int, fully_played: bool | None) -> None
```

**`promotion_cost_min`** is the episode's *remaining* minutes:
`ms_to_min(max(0, duration_ms - last_known_position_ms))`. An item already half
heard costs half. Charging full duration for a re-promotion would overstate the
commitment and make the gate lie.

**`promote()` is the allocation gate.** It computes the cost, and if
`week.promoted_min + cost > week.effective_allowance_min` it raises
`AllowanceExceeded` **without changing any state**, carrying the overage in
minutes so the bot can say by how much. Otherwise it transitions
`queued → promoted` and adds the cost to `weeks.promoted_min`.

**`roll_over_week()`** closes the *outgoing* week and opens the one containing
`now`:
1. Lapse every `promoted` item that is not `played` back to `queued`.
2. `debt = clamp(listened_min - effective_allowance_min, 0, allowance_min)`.
3. Create the `weeks` row for the week containing `now`, with that debt and a
   fresh allowance from config.

**Which week gets closed, and why it is not `current_week()`.** Triage fires
*at* Sunday 18:00, and `clock.week_start_for()` treats exactly 18:00 as opening
a new week. So at the moment triage runs, `current_week()` is a week zero
seconds old with nothing in it. Closing that would compute every debt against an
empty week, debt would always be 0, and the mechanism would be dead code that
passes its tests. `roll_over_week()` therefore closes **the most recent week row
that opened before the week containing `now`**. It is idempotent across repeated
triage runs in the same week.

Debt is driven by **listened** minutes, not promoted minutes: the gate already
makes it impossible to over-promote, so the only way to exceed the allowance is
to actually listen past it. The clamp at `allowance_min` stops one blown week
from zeroing out several later ones.

### Stats

```python
@dataclass(frozen=True)
class FunnelStats:
    captured: int
    survived_lock: int
    promoted: int
    played: int
    fully_played: int
    survived_rate: float | None      # None when the denominator is 0
    promoted_rate: float | None
    played_rate: float | None
    fully_played_rate: float | None
    median_days_to_drop: float | None
    still_holds_yes_rate: float | None
    still_holds_answered: int

def funnel_stats(conn, clk, settings, *, since: str | None = None) -> FunnelStats
```

This is the **only** function permitted to read `REMOVED_STATES`.

- `captured`: all items whose lock window has closed (items still inside their
  lock are undetermined and excluded from every denominator).
- `survived_lock`: captured items that were not resolved (`dropped`/`expired`)
  before `captured_at + lock_hours`.
- `promoted`: `promoted_at IS NOT NULL`.
- `played`: any `listening` row for that episode with `delta_ms > 0`.
- `fully_played`: `state = 'played'`.
- `median_days_to_drop`: median of `resolved_at - captured_at` over
  `state = 'dropped'`.
- `still_holds_yes_rate`: `sum(still_holds) / count(still_holds IS NOT NULL)`.

`since=None` means all time; `/stats` also calls it with the `week_start` of
eight weeks ago.

---

## 3. `spotify.py` — Agent B

```python
class SpotifyError(Exception): ...
class InvalidEpisodeRef(ValueError): ...       # message is user-facing

@dataclass(frozen=True)
class Episode:
    spotify_id: str
    title: str
    show: str
    description: str | None
    release_date: str | None
    duration_ms: int
    resume_position_ms: int
    fully_played: bool
```

### Parsing

```python
EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")

def parse_episode_ref(text: str) -> str
```

Accepts exactly two forms and returns the bare 22-character base62 id:

- `https://open.spotify.com/episode/{id}` with any query string, which is
  stripped, and with an optional locale segment (`/intl-pt/episode/{id}`)
- `spotify:episode:{id}`

Anything else raises `InvalidEpisodeRef` with a message the bot shows verbatim.
A Spotify link that is a track, show, album or playlist gets a message naming
what it actually is — silent rejection is forbidden by the brief.

### Auth and fetch

```python
class SpotifyClient:
    def __init__(self, settings: Settings, *, transport=None): ...
    def access_token(self) -> str          # refreshes on first use and on expiry, transparently
    def get_episode(self, spotify_id: str) -> Episode
    def get_episodes(self, ids: list[str]) -> list[Episode]   # batched, ≤50 per request
```

Auth is refresh-token only; the access token is cached in memory with its
expiry and renewed transparently. A 401 triggers exactly one retry after a
forced refresh, then raises. `description` is HTML-stripped before it is
returned.

`resume_position_ms` comes from `resume_point.resume_position_ms` and
`fully_played` from `resume_point.fully_played`. If `resume_point` is absent —
which means the token lost the scope — that is a `SpotifyError`, not a silent
zero. Reading a missing scope as "listened to nothing" is the one failure this
system cannot afford to swallow.

### The clamp

```python
def clamped_delta(previous_position_ms: int, current_position_ms: int) -> int:
    return max(0, current_position_ms - previous_position_ms)
```

`resume_point` is a **position, not a total**. Scrubbing backward or
relistening drops it, and an unclamped delta goes negative, silently crediting
time that was not earned back. Agent B writes unit tests for four cases as it
goes: forward listening, backward scrub, relisten from zero, and no change.

### Test double

Agent B also ships `FakeSpotify` with the same surface, driven by a dict of
fixtures. Agents C and the Phase 3/4 work import that, never the real client.

```python
class FakeSpotify:
    def __init__(self, episodes: dict[str, Episode]): ...
    def set_position(self, spotify_id: str, position_ms: int,
                     fully_played: bool = False) -> None
```

---

## 4. `bot.py` — Agent C

```python
def build_app(settings: Settings, conn, clk, spotify) -> Application
def authorised_only(handler)                   # decorator
```

`spotify` is any object with the `SpotifyClient` surface. The bot never
constructs one itself, so tests inject `FakeSpotify`.

### Authorisation

Every update is checked against `settings.telegram_user_id`. Anything else is
dropped with **no reply at all** — not an error message, not a log line the
sender can observe. Per the brief: reject everything else silently.

### Capture conversation

State lives in `context.user_data`, **not** in the database:

1. User sends a link. `parse_episode_ref` runs. On failure the bot replies with
   the rejection message and the conversation ends.
2. Bot fetches metadata, holds `(spotify_id, metadata)` in `user_data`, and
   asks **"Why?"**.
3. **Nothing is written to the database yet.** If the user never replies, no
   row is ever created. This is what makes the note mandatory rather than
   optional cleanup after the fact.
4. The reply becomes `why_note`. An optional trailing deadline is parsed out of
   it. Only now does `db.capture()` run.
5. Bot confirms with **title, show and duration**, so the time cost is visible
   at the moment of capture, plus the lock expiry.

A second link sent mid-conversation replaces the pending capture rather than
queueing behind it.

### Deadline parsing

```python
def parse_deadline(text: str, clk, tz: str) -> tuple[str, str | None]
```

Returns `(why_note_without_the_deadline, deadline_iso_or_None)`. Recognises a
trailing `by <date>` / `before <date>` in a small set of forms: `by friday`,
`by 3 sep`, `by 2026-09-03`, `by tomorrow`. Anything unrecognised is left as
part of the note — a misparse must never eat the user's words. Deadlines
resolve to 18:00 local on the named day.

### Inline buttons

Callback data is `f"{action}:{item_id}"` with action in
`{"promote", "drop", "holds_yes", "holds_no"}`.

- `promote` → `db.promote()`. On `AllowanceExceeded` the button does **not**
  promote; it answers with the overage: *"That would put you 25 min over. 40 min
  left this week."*
- `drop` → `db.set_state(..., "dropped")`, a soft delete.
- `holds_yes` / `holds_no` → `db.record_still_holds()`. This is recorded
  independently of promote/drop — it is the cleanest signal in the system and
  is never inferred from the other buttons.

Callbacks on an item already in a terminal state answer "already resolved"
rather than raising.

### Commands

```
/start   one-line explanation
/queue   the current unlocked queue, read-only
/stats   the single funnel message (implementation in triage.py, §5)
```

---

## 5. `triage.py` and `poll.py` — Phase 3

```python
# triage.py
def run_triage(conn, clk, settings, spotify, bot) -> None
def render_item(item: QueueItem, week: Week, warning: bool) -> tuple[str, InlineKeyboardMarkup]
def render_deadline_notice(item: QueueItem, week: Week) -> tuple[str, InlineKeyboardMarkup]
def render_stats(conn, clk, settings) -> str

# poll.py
def run_poll(conn, clk, settings, spotify, bot) -> None
def surface_deadlines(conn, clk, settings, bot) -> None
def check_alerts(conn, clk, settings, bot) -> None
```

`run_triage()` executes in this order, and the order matters:

1. `roll_over_week()` — lapse unlistened promotions, compute debt, open the week.
2. `lift_expired_locks()` — `locked → queued`.
3. Expire anything at or past its lifespan (`cycles_seen >= lifespan_cycles`)
   and anything whose deadline has passed, both as soft deletes.
4. `bump_cycle()` on each survivor, then post it with title, show, duration,
   why-note and cycle age. `cycles_seen == lifespan_cycles` after the bump gets
   the "last cycle" warning.
5. Post the week's header: allowance, debt carried, remaining.

`run_poll()` calls `lift_expired_locks()`, fetches positions for
`polling_targets()`, computes `clamped_delta` per episode, writes `listening`
rows, updates `weeks.listened_min`, flips `fully_played` episodes to the
`played` state, and then calls `surface_deadlines()` followed by
`check_alerts()`.

`surface_deadlines()` posts each `queued` item whose deadline falls at or
before `clock.next_week_start(now)` — i.e. one the weekly triage would expire
rather than reach — using `render_deadline_notice()`, which carries triage's
own keyboard and callback payloads. Once per item, ever, tracked in
`deadline_notices`. This is the brief's "an item with a deadline surfaces
before it expires regardless of where the weekly cycle sits"; see D5.1.

`check_alerts()` sends at most one `approaching` (≥80% of effective allowance)
and one `exceeded` (>100%) message per week, tracked in `alerts_sent`.

Both send before writing their bookkeeping row, so a Telegram outage is retried
by tomorrow's poll rather than swallowed (D3.5).

Crontab lines go in `README.md`. They are not installed.


---

# Part II — DRBR

Phase 1 output for the DRBR extension (`drbr-build-brief.md`). Binding on every
Phase 2 agent. **No agent changes a shared contract privately.** If a contract
must change, stop, write the change here centrally, and apply it to every
affected component. Agents record everything else in `NOTES-<agent>.md` (now `docs/build-notes/`) for the
integration pass.

Governing rule for all of it: **the user labels, the system counts.** No
heuristic may infer value (dwell time, length, session length, engagement,
folder, completion, duration, or anything else). If you are tempted, don't,
and write the temptation down in your notes.

## §0 File ownership (Phase 2)

No two agents modify the same file. Paths not listed are reserved for the
integration pass and must not be edited in Phase 2.

| Agent | Owns (may create/modify) |
|---|---|
| **A — YouTube** | `sources/youtube.py`, `ingest_api.py`, `extension/**`, `tests/test_youtube*.py`, `tests/test_ingest_api.py`, `tests/fixtures/youtube/**`, `NOTES-A.md` |
| **B — Screen Time + Cold Turkey** | `sources/screentime.py`, `sources/ocr_vision.py`, `sources/coldturkey.py`, `handlers_imports.py`, `tests/test_screentime*.py`, `tests/test_coldturkey*.py`, `tests/test_handlers_imports.py`, `tests/fixtures/screentime/**`, `tests/fixtures/coldturkey/**`, `NOTES-B.md` |
| **C — Vault + labels** | `sources/vault.py`, `labels.py`, `handlers_labels.py`, `tests/test_vault*.py`, `tests/test_labels*.py`, `tests/test_handlers_labels.py`, `tests/fixtures/vault/**`, `NOTES-C.md`; **and** the separate repo `/Users/Jacob/Projects/obsidian-ingest` on a branch `drbr-capture-id` |
| **D — Gate + reporting** | `allowance.py`, `report.py`, `db.py`, `bot.py`, `poll.py`, `triage.py`, `tests/test_allowance*.py`, `tests/test_report*.py`, `tests/test_db.py`, `tests/test_bot.py`, `tests/test_poll.py`, `tests/test_triage.py`, `tests/test_integration.py`, `NOTES-D.md` |
| **Integration only** | `schema.sql`, `migrations/**`, `config.py`, `clock.py`, `drbr_types.py`, `evidence.py`, `conftest.py`, `check_env.py`, `jobs.py`, `dashboard/**`, `drbr` (launcher), `launchd/**`, `requirements.txt`, `.env*`, `README.md`, `CONTRACTS.md`, `DECISIONS.md`, `EXISTING.md`, `REVIEW.md`, `tests/test_drbr_*.py` |

Agents run tests with `/Users/Jacob/Projects/drbr/.venv/bin/python -m pytest -q`
from their own worktree root. Real personal inputs are read-only at
`/Users/Jacob/Projects/drbr/inputs/` (gitignored; never copy them into the
repo unsanitised).

## §1 Conventions

- **Instants:** ISO-8601 UTC via `clock.iso()`, as in POP.
- **Local days:** `'YYYY-MM-DD'` in `settings.tz` (`POP_TZ`, `Europe/Warsaw`). Convert only with `drbr_types.local_date`.
- **Measurement week:** local Sunday..Saturday (`drbr_types.measurement_week`). It is charged to the POP allowance week opening **18:00 on that same Sunday** (`drbr_types.allowance_week_start_for_day`). A local day is never split between two allowance weeks. Spotify deltas keep POP's instant-based attribution (`clock.week_start_for(polled_at)`).
- **Time is injected:** every function that needs "now" takes a `Clock`. Tests use `clock.FrozenClock`.
- **Settings:** `config.Settings` gains `youtube_api_key`, `vault_path`, `vault_inbox`, `capture_outbox`, `raw_dir`, `diagnostic_weeks`, `dashboard_port`, `ingest_token` (defaults keep `config.test_settings()` valid). Tests pass `raw_dir=tmp_path/...` and `vault_path=tmp_path/...` explicitly.
- **No network in tests**, enforced by `conftest.py`. Every external client is injected.
- **Secrets** never appear in logs, exceptions shown to the user, fixtures, or notes.

## §2 Schema (migration `0002_drbr.sql`, schema_version 2)

Authoritative DDL is `migrations/0002_drbr.sql`; semantics:

| Table | Purpose / invariants |
|---|---|
| `episodes` + `platform` ('spotify' default / 'youtube'), `source_key` | Queue item metadata. A YouTube row has `spotify_id = 'youtube:<videoId>'`, `show` = channel title, `source_key` = channel id, `duration_ms` from the Data API. |
| `sources` | Labelled things. `UNIQUE(kind, source_key)`, key normalised by `labels.normalise_key`. `label` NULL = unlabelled. X pre-seeded wasteful. |
| `item_labels` | Per-item override, `item_ref` = `'spotify:<id>'`/`'youtube:<id>'`, origin `'capture'`. |
| `imports` | One row per ingestion attempt, including failures. `raw_path` relative to `settings.raw_dir`. |
| `measurements` | Non-Spotify minutes. Natural key `(provenance, source_id, item_key, period_start, period_end)`; later import replaces the fact. `minutes IS NULL` ⇔ `status != 'ok'`. |
| `category_measurements` | Screen Time category totals (reported only; not labelled, not gated). |
| `vault_captures` | Obsidian captures from the outbox, identity = `capture_id`. |
| `vault_observations` | One row per capture per scan; latest scan per capture is current. |
| `prompts_sent` | Dedup bookkeeping for bot prompts (like POP's `alerts_sent`). |

## §3 Canonical constants — `drbr_types.py`

Labels `WASTEFUL`/`WORTHWHILE`, `DEFAULT_LABEL = WASTEFUL`; bases
`BASIS_ITEM_OVERRIDE` > `BASIS_SOURCE_LABEL` > `BASIS_DEFAULT`; source kinds
`podcast_show | youtube_channel | iphone_app | web_domain | x`; report groups
`podcasts | youtube | x | iphone_apps | desktop_web` (+ keeper-only `web`,
`other`); provenance `api | page_extraction | screenshot_ocr | manual_export |
vault_scan | user_label` with `PROVENANCE_TEXT`; devices `iphone | desktop |
account`; measurement statuses `ok | progress_missing | duration_missing |
unreadable`; import kinds `youtube_history | screentime | coldturkey |
vault_scan`; `COVERED_ELSEWHERE` (double-count exclusions);
`VideoInfo(video_id, title, channel_id, channel_title, duration_s|None)`;
`VideoLookupError`; `item_ref()`; week helpers.

## §4 Evidence — `evidence.py`

```python
store_raw(settings, kind, data: bytes, ext) -> (rel_path, sha256)      # content-addressed, 0600, idempotent
raw_file(settings, rel_path) -> Path
find_import_by_sha(conn, kind, sha) -> Row | None                        # latest non-failed
begin_import(conn, clk, *, kind, parser_version, raw_path=None, raw_sha256=None) -> ImportRecord   # row starts 'failed'
upsert_measurement(conn, rec, *, source_id, device, provenance, period_start, period_end,
                   minutes, status='ok', item_key='', item_title=None, detail=None) -> None       # no commit
finish_import(conn, clk, settings, rec, *, period_start, period_end, status=None) -> 'ok'|'partial'
fail_import(conn, rec, error: str) -> None                              # rolls back uncommitted writes
get_meta / set_meta / ensure_diagnostic_start(conn, first_data_date)
diagnostic(conn, clk, settings) -> Diagnostic(started, start, end, days_remaining, week_number, ended)
class ParseError(Exception)          # base for every source parse error
class Outcome(import_id, status, measurements_written, warnings, error, period_start, period_end, duplicate_of)
```

**Every ingest function has this shape and these semantics:**

1. Store the raw bytes first (`store_raw`).
2. If `find_import_by_sha` returns a prior non-failed import, return `Outcome(..., duplicate_of=that_id)` and write nothing. Re-parsing is explicit (`reparse`).
3. `begin_import`, parse, `upsert_measurement` per fact, `finish_import`.
4. Any `ParseError` (or a dependency failure such as `VideoLookupError`) → `fail_import(conn, rec, message)` and return a failed `Outcome`. Never raise to a handler for bad evidence; never write zeros for what could not be read.
5. `rec.warnings` for anything partially unreadable ⇒ status `partial`.
6. `finish_import` fixes the diagnostic start (first import with a period) and calls `allowance.on_measurements_changed`.

Each source module exposes `PARSER_VERSION: str` and
`reparse(conn, clk, settings, import_id, ...) -> Outcome`, which reads the raw
file again and writes a **new** import row.

## §5 Labels — `labels.py` (Agent C extends; signatures fixed)

```python
normalise_key(kind, raw) -> (kind, key)       # X app / x.com / twitter.com fold into ('x','x')
ensure_source(conn, clk, kind, raw_key, display_name=None) -> Source           # no commit
get_source(conn, source_id) -> Source | None
is_covered_elsewhere(source) -> str | None
set_source_label(conn, clk, source_id, label) -> Source                         # commits
set_item_override(conn, clk, item_ref, label) -> None                           # no commit
parse_override_token(text) -> (note, label | None)    # '#worthwhile|#worth|#wasteful|#waste'
item_override(conn, item_ref) -> str | None
resolve(conn, *, source_id, item_ref=None) -> ResolvedLabel(label, basis)       # THE resolver
# Agent C adds:
sources_needing_label(conn, clk, settings, *, limit=8) -> list[Source]
mark_prompted(conn, clk, source_ids) -> None
list_sources(conn, *, kind=None, label=None | 'unlabelled') -> list[Source]
```

Precedence is exactly: item override → source label → wasteful default.
`sources_needing_label` returns only unlabelled, not-covered sources that have
at least one measurement, listening row (via a queue episode) or vault capture,
and were not prompted in the last 7 days; oldest `first_seen_at` first.

## §6 Sources

### §6.1 YouTube — `sources/youtube.py`, `ingest_api.py`, `extension/` (Agent A)

The real saved history page (`inputs/Watch history - YouTube.html`) carries the
data in `ytInitialData`; scrolling fetches more via POST `/youtubei/v1/browse`
continuation JSON. The extension captures those JSON objects **from the page's
own data** and posts them; parsing happens server-side in Python so it is
testable against the real fixture.

```python
PARSER_VERSION = "youtube-1"
class YouTubeParseError(evidence.ParseError)
@dataclass WatchEntry(video_id, title, channel_id|None, channel_name|None, watch_date: str,
                      is_short: bool, percent: float|None, order: int)
parse_pages(pages: list[dict], *, captured_at: str, tz: str) -> ParsedHistory(entries, warnings)
resolve_date_header(text: str, captured_local: date) -> date      # 'Today','Yesterday',weekday,'Sep 9','Aug 30, 2025'
class DataApiClient(settings, *, transport=None):                 # httpx, videos.list part=contentDetails,snippet, 50 ids/call
    videos(video_ids) -> dict[str, VideoInfo]                     # missing ids simply absent; raises VideoLookupError
    video_info(video_id) -> VideoInfo | None
class FakeYouTubeApi                                              # test double, same surface
ingest(conn, clk, settings, payload: bytes, api) -> Outcome
reparse(conn, clk, settings, import_id, api) -> Outcome
current_cursor(conn) -> str | None                                # meta 'youtube_cursor'
```

Payload (JSON bytes) from the extension:
`{"schema": 1, "captured_at": "<UTC ISO>", "extension_version": "...", "cursor": "<videoId>|null", "cursor_reached": bool, "pages": [<ytInitialData or browse response>, ...]}`

Rules:
- Walk the JSON generically for `itemSectionRenderer` sections; date = the section header. A section whose header cannot be resolved, or a page with zero sections, is a `YouTubeParseError` — loud, never an empty week.
- Regular videos: `lockupViewModel` (contentType VIDEO), percent from `thumbnailOverlayProgressBarViewModel.startPercent`. Absent progress on a regular video → measurement `status='progress_missing'`, `minutes=None`, and a warning. Never dropped, never 0.
- Shorts: `shortsLockupViewModel` inside `reelShelfRenderer`. **Assumed fully watched**: `minutes = duration_s/60` from the Data API, `detail.assumed_full = true`. Never an assumed average length.
- `minutes = percent/100 * duration_s/60`. Missing API duration → `status='duration_missing'`.
- Source = `youtube_channel` keyed by the API's `channel_id` (display = channel title); device `account`; provenance `page_extraction`; `item_key` = video id; period = the watch date (one day).
- Entries at or after the stored cursor (the cursor video and everything older) are skipped. On `ok`/`partial`, the cursor becomes the newest video id of this payload. A failed import never moves the cursor.
- `VideoLookupError` → failed import (raw kept; `reparse` later).
- No time of day, no sittings, no rewatch correction are fabricated.

`ingest_api.py`: `make_blueprint(*, get_conn, clk, settings, api_factory) -> flask.Blueprint` with
`GET /api/youtube/cursor` and `POST /api/youtube/history`. Both require header
`X-DRBR-Token == settings.ingest_token` (constant-time compare), else 401.
Body limit 25 MB. Responds JSON with the `Outcome` fields. Mounted by the
dashboard app (Phase 3) on `127.0.0.1:settings.dashboard_port`.

`extension/`: Manifest V3, unpacked, works in Chrome and Brave. Content script
on `https://www.youtube.com/feed/history*` reads the page's initial data and
the browse responses (MAIN-world fetch/XHR observation), a popup button runs
the weekly sync: get cursor → scroll until the cursor video id has been seen,
there is no further continuation, or a page cap → POST payload → show the
Outcome (including failures, prominently). A missing `ytInitialData` or zero
sections is reported as a failure in the popup, never as "0 videos".

### §6.2 Screen Time — `sources/screentime.py`, `sources/ocr_vision.py` (Agent B)

Real evidence: `inputs/IMG_5223..5232.PNG`, one scrolled sequence on the
iPhone's per-device view. Sections: summary (daily average, top-3 category
totals, total), Limits, **Most Used** (app + duration like `1h 17m`, `23m`,
`53s`), Pickups (counts), First Used After Pickup (counts), Notifications
(counts). Header shows the device name and a week label.

```python
# ocr_vision.py — the only module importing pyobjc
@dataclass TextObservation(text, confidence, x, y, w, h)   # normalised 0..1, origin top-left
recognize(image_bytes) -> list[TextObservation]             # Apple Vision VNRecognizeTextRequest, accurate, on-device

# screentime.py
PARSER_VERSION = "screentime-1"
class ScreenTimeParseError(evidence.ParseError)
parse_duration(text) -> float | None                        # minutes; '1h 17m'->77, '53s'->0.883; None if not a duration
parse_observations(pages: list[list[TextObservation]], *, received_at: str, tz: str) -> ScreenTimeParse(
    device_name, week_label, period_start|None, period_end|None, apps: list[(name, minutes)],
    categories: list[(name, minutes)], total_minutes|None, warnings, is_full_past_week: bool)
ingest_batch(conn, clk, settings, images: list[tuple[bytes, str]], *, received_at, ocr=recognize) -> Outcome
reparse(conn, clk, settings, import_id, *, ocr=recognize) -> Outcome
```

Rules:
- Raw: each image via `store_raw('screentime', ...)`, plus a JSON manifest (image rel paths in order, received_at) stored the same way; `imports.raw_path` = the manifest. Duplicate detection uses the manifest sha.
- Only per-app minutes from **Most Used** and category totals from the summary are measurements. Counts (pickups, notifications) are ignored and never read as minutes.
- Overlapping screenshots repeat rows: dedupe by app name; the same app with two different durations is a warning, not a guess.
- A row whose name or duration cannot be read → warning (import `partial`); nothing invented.
- **Only a complete past week writes measurements** (`is_full_past_week`: label "Last Week" or an explicit date range entirely before `received_at`'s week). "This Week" or an all-devices view is stored as evidence with a warning explaining what to resend, status `partial`, zero measurements.
- Measurement: source `iphone_app` via `labels.ensure_source` (X folds automatically), device `iphone`, provenance `screenshot_ocr`, period = that Sunday..Saturday. `detail` holds the raw OCR text for the row and the confidence.
- Tests never call Vision: they feed recorded `TextObservation` fixtures. Committed fixtures are sanitised (see §12). A local-only test may run real OCR on `inputs/` and must `skip` when the files are absent.

### §6.3 Cold Turkey — `sources/coldturkey.py` (Agent B)

Real evidence: `inputs/Cold Turkey Blocker Website Statistics.json` —
`[{"label": "<domain>", "data": [[<UTC-midnight epoch ms>, <minutes float>], ...]}, ...]`.
The sibling "Websites Blocked" export is a *count* chart and must be rejected.

```python
PARSER_VERSION = "coldturkey-1"
class ColdTurkeyParseError(evidence.ParseError)
parse(data: bytes) -> ColdTurkeyParse(rows: list[(label, date, minutes)], days: list[date], warnings)
ingest(conn, clk, settings, data: bytes, *, filename: str | None = None) -> Outcome
reparse(conn, clk, settings, import_id) -> Outcome
```

Rules: day = the UTC calendar date of the timestamp (Cold Turkey encodes local
days at UTC midnight). Minutes are taken as given (float). Source `web_domain`
(X folds), device `desktop`, provenance `manual_export`, period = that day.
Only fields observed in the real file are assumed. Malformed/missing/
unexpected shapes raise `ColdTurkeyParseError`; duplicated (label, day) pairs
are a warning with a documented rule. Import `meta` records the listed domain
count and the note that unlisted domains are unmeasured.

### §6.4 Vault — `sources/vault.py` + obsidian-ingest (Agent C)

**Outbox line** (JSONL, appended by obsidian-ingest to `DRBR_CAPTURE_OUTBOX`, one object per line):
`{"v": 1, "capture_id": "<uuid4 hex, 32 chars>", "captured_at": "<UTC ISO>", "capture_type": "<ingest input type/format, verbatim>", "url": str|null, "title": str, "author": str|null, "rel_path": "<path relative to vault>", "body_sha256": "<hex>"}`

**Frontmatter key:** `drbr_capture_id: <capture_id>` in the note's first frontmatter block.

**Body hash** (identical in both repos, shared test vector): the text after the
first frontmatter block's closing `---` line, `.strip()`ped, UTF-8, SHA-256.

```python
PARSER_VERSION = "vault-1"
FRONTMATTER_KEY = "drbr_capture_id"
body_sha256(note_text: str) -> str
read_outbox(conn, clk, settings) -> OutboxResult(new: int, malformed: list[str])     # idempotent by capture_id
scan(conn, clk, settings) -> Outcome        # kind 'vault_scan', raw_path None, meta = counts
latest_outcomes(conn) -> dict[capture_id, VaultOutcome(outcome, rel_path, edited, copies, scanned_at)]
keeper_group_for(capture_type) -> str       # x-post/x-article -> x; youtube -> youtube; article -> web; else other
```

Rules:
- Identity is `capture_id` only. Paths are recorded, never matched on.
- Outcomes, exactly: `unprocessed` (every copy under `settings.vault_inbox/`), `kept` (a copy anywhere else in the vault), `excluded` (no file carries the id, or only in `.trash/`). Excluded never counts as failure and leaves the denominator.
- `edited` = current body hash ≠ `body_sha256` at capture. Secondary flag only.
- Skip `.obsidian/`, `.git/`, `.trash/` when looking for kept/unprocessed copies.
- Two files with one id: `copies=2`, outcome by the rule above, plus a warning.
- obsidian-ingest (branch `drbr-capture-id`): generate the id and append the outbox line **before** writing the note (a logged capture whose note never appears is excluded, which is harmless; a note the log never heard of would be invisible); write the id into frontmatter; outbox failure is logged loudly and the bot reply says the DRBR log failed. Add tests there (pytest, no network). Do **not** restart its launchd agent; integration does.

### §6.5 Spotify

Unchanged except where Part II §7 says. Podcast source = `podcast_show` keyed by
normalised `episodes.show`.

## §7 Gate and allowance — `allowance.py`, `db.py`, `poll.py`, `triage.py`, `bot.py` (Agent D)

```python
item_label(conn, item: db.QueueItem) -> labels.ResolvedLabel
    # source: podcast_show(show) for spotify rows, youtube_channel(source_key) for youtube rows;
    # item_ref: spotify:<id> / youtube:<videoId>
promotion_cost(conn, item) -> tuple[int, labels.ResolvedLabel]    # worthwhile -> 0
pooled_minutes(conn, settings, week_start: str) -> PooledMinutes(
    wasteful_min: float, worthwhile_min: float, by_group: dict[str, GroupMinutes],
    covered_excluded_min: float, unmeasured_items: int)
gated_split(conn, settings, week_start: str) -> dict[group, GatedSplit(gated_min, ungated_min, wasteful_ungated_min)]
on_measurements_changed(conn, clk, settings) -> None     # recompute debt_min of every week that has a predecessor
debt_for(closed: db.Week, pooled: PooledMinutes) -> int  # clamp(wasteful - effective, 0, allowance)
```

Behaviour:
- **Lock stays intact for every item**, worthwhile or wasteful. Triage flow unchanged.
- **Worthwhile items never spend the allowance**: promotion cost 0, no refusal, and their consumption is excluded from allowance, debt and alerts.
- **Wasteful** (including unlabelled-default) promotions go through POP's gate exactly as before.
- **Pooled consumption** for an allowance week = wasteful Spotify minutes (clamped deltas in the week window, labelled per episode) + wasteful `measurements` minutes (status ok, not `COVERED_ELSEWHERE`) whose day maps to the week. `weeks.listened_min` keeps its POP meaning (raw Spotify minutes).
- **Debt** at rollover = `clamp(pooled wasteful − effective allowance, 0, allowance_min)`. Late imports recompute it (`on_measurements_changed`). Alerts in `poll.check_alerts` compare pooled wasteful minutes.
- **Gated** consumption: Spotify delta whose queue item was promoted at or before the poll instant; YouTube measurement whose `youtube:<id>` queue item was promoted on or before that local day. Everything else (all Screen Time, Cold Turkey, unpromoted YouTube/Spotify) is ungated.
- **YouTube capture:** a YouTube link sent to the POP bot captures into the queue through `bot_data["youtube"]` (any object with `video_info(video_id) -> VideoInfo | None`, raising `VideoLookupError`). `duration_s is None` refuses capture with a message. YouTube rows are excluded from Spotify polling and never auto-`played`.
- **Override at capture:** the why-note goes through `labels.parse_override_token` (and POP's deadline parse). An override is stored with `labels.set_item_override`. The confirmation shows the resolved label and basis. The triage post shows the label and "costs 0 min" for worthwhile.
- Labels are always read through `labels.resolve` via `allowance.item_label`; nothing caches a label.

## §8 Telegram integration hooks

- `bot.build_app` registers POP handlers, then calls `handlers_imports.register(app)` and `handlers_labels.register(app)`.
- Callback data prefix `d:` is reserved for DRBR. `d:imp:...` (B), `d:lbl:...` (C). POP's handler ignores `d:`; malformed non-`d:` data is still answered "Unrecognised button.". Data ≤ 64 bytes.
- Extension modules must not add a plain TEXT handler. Photos, image documents and JSON documents belong to B; label commands/callbacks to C; links and notes to D (`bot.py`).
- Every handler is wrapped in `bot.authorised_only` and reads `settings`, `conn`, `clk` from `context.bot_data`.
- Jobs call, and never duplicate, these functions (all return a list of sent-message summaries, send via `triage.send`, dedupe via `prompts_sent`, and do nothing once `evidence.diagnostic(...).ended`):
  - B: `handlers_imports.send_due_prompts(conn, clk, settings, bot) -> list[str]` — for the most recent complete measurement week, prompt once per day (from the Monday after it) for each of screentime / coldturkey / youtube that has no ok/partial import covering it.
  - C: `handlers_labels.send_label_batch(conn, clk, settings, bot) -> list[str]` — only once the diagnostic has run ≥ 7 days; one message per source with two buttons.
- User-facing replies for imports state the Outcome honestly: counts written, warnings, or the failure reason and what to resend.

## §9 Reporting — `report.py` (Agent D)

All functions are read-only and take `(conn, clk, settings, ...)`.

```python
diagnostic_banner(...) -> evidence.Diagnostic
weekly_totals(..., week_start: str) -> list[SourceTotal(group, provenance: str, device, minutes, unmeasured_items, covered_excluded_min, import_status)]
  import_status: None (API-polled) | 'ok' | 'partial' | 'not_due' | 'missing' | 'so_far' | 'incomplete'
  The last four are two pairs: nothing arrived ('not_due' before the week is asked
  for, 'missing' after) and partial coverage ('so_far' before, 'incomplete' after).
  Slugs, not prose: the template maps them to words and to a CSS class (DR5.10).
top_sources(..., week_start, limit=8) -> list[SourceLine(source_id, display_name, group,
                                       minutes, label, reflex_min, provenances)]
  The week's content BY NAME, biggest first. label is None when undecided — never guessed.
  Tool sources never appear. Same exclusions as pooled_minutes, so list and totals agree.
PooledMinutes/GroupMinutes/WeekSplit gain unclassified_min and .charged_min
  (= wasteful + unclassified). Reported headline reads wasteful_min; the allowance,
  overage and alerts read charged_min (DR5.11).
week_is_due(clk, settings, mweek) -> bool   # the Sunday after the week has come (DR5.24; was Monday)
due_on(mweek) -> date                       # that Sunday: the day checklist.send_due asks
everything_in(totals) -> bool               # every expected source covers the week
label_split(..., weeks: int = 8) -> list[WeekSplit(week_start, wasteful_min, worthwhile_min)]
allowance_status(...) -> AllowanceStatus(week_start, allowance_min, debt_min, effective_min, spent_wasteful_min, remaining_min, promoted_min)
gated_vs_ungated(..., week_start: str) -> list[GatedRow(group, gated_min, ungated_min, share_ungated|None)]
cost_per_keeper(..., since: str | None = None) -> list[KeeperRow(group, minutes, keepers, minutes_per_keeper|None, provenances, blind_spots)]   # ranked; None price sorts last
funnels(...) -> Funnels(pop: db.FunnelStats, vault: VaultFunnel(captured, unprocessed, kept, excluded, edited_kept))
blind_spots(...) -> list[str]
records(..., *, group=None, label=None, start=None, end=None, limit=500) -> list[RecordRow]
```

- Cost per keeper = group minutes (all labels, excluding covered duplicates) ÷ vault captures from that group currently `kept`. Zero keepers → `minutes_per_keeper=None` ("no keepers yet"), never infinity or zero.
- Vault figures are **counts**. No function converts a capture into minutes.
- Every row carries provenance; screenshot-derived rows keep `screenshot_ocr` all the way to the UI.
- `blind_spots` includes at least: podcast listening outside the queue is invisible; Cold Turkey lists only its top domains; only one desktop machine is exported; YouTube rewatches undercount and dates have no time of day; YouTube progress-missing items; Screen Time weeks not yet received.
- No targets, scores or streaks.

## §10 Dashboard (Phase 3, integration)

Flask + HTMX, server-rendered, bound to `127.0.0.1:settings.dashboard_port`,
mounts `ingest_api.make_blueprint`. Consumes only `report.py`, `labels.py`
(label manager toggle via `set_source_label`) and `evidence.diagnostic`.

## §11 Jobs (integration)

`jobs.py daily` (launchd, daily): `vault.read_outbox`, `vault.scan`,
`handlers_imports.send_due_prompts`, `handlers_labels.send_label_batch`,
diagnostic-end notice. POP's `poll.py`/`triage.py` schedules are unchanged.

## §12 Fixtures and personal data

- Committed fixtures are derived from the real inputs **and sanitised**: titles, channel names, video ids, app names beyond what the parser needs, domains, device name and exact minutes are replaced with synthetic values while the structure the parser depends on is kept byte-for-byte in shape.
- For Screen Time, fixtures are recorded OCR observations (JSON), sanitised the same way; images are not committed.
- A test may read `/Users/Jacob/Projects/drbr/inputs/` only as an optional local check that `pytest.skip`s when absent, and it must not print personal content.
- Nothing from the vault is committed.

## §13 Amendments made at integration (Phase 2.5)

- §1 Settings: `go_live: str` (`DRBR_GO_LIVE`, local date). `evidence.finish_import` never fixes `diagnostic_start` before it. (DR2.2)
- §6.1: minutes rule for partial regular videos prefers `resume_s` (DR2.1); one fact per regular video per sync under its newest date, one per distinct day for Shorts (DR2.3); entries before `settings.go_live` are skipped and counted in meta (DR2.2); cursor also stores `youtube_cursor_date`, and `GET /api/youtube/cursor` returns `{cursor, cursor_date}` (DR2.4); a failed import answers HTTP 422 with the Outcome body; the key is sent as the `X-Goog-Api-Key` header; `PARSER_VERSION = "youtube-2"`.
- §3: `drbr_types.UNKNOWN_YOUTUBE_CHANNEL_KEY`; §5 `sources_needing_label` excludes it (DR2.5).
- §6.4: `scan` records a failed import when `settings.vault_path` is unset or missing (DR2.6). `/labels` sends immediately, ignoring the once-per-day `prompts_sent` gate but keeping the 7-day and per-source cooldowns.
- §6.2 (Agent B, accepted): `ocr_vision.recognize_region` and a `region_ocr=` parameter on `ingest_batch`/`reparse`; `TextObservation.origin`; batches are per chat, not per album; the duplicate check matches identical images in the same week; `reparse` replaces the previous import's rows; "This Week"/all-devices imports have NULL periods (DR1.13, DR2.8).
- §6.3: days on or after the export date are skipped with a warning (DR2.9). Measurements carry `item_key` = the raw domain, so X from `x.com` and `twitter.com` never overwrite each other; **readers sum per source**.
- §7/§9 (Agent D, accepted): `QueueItem.platform`/`source_key` (defaulted); `triage.render_item(..., label=None)` and `render_deadline_notice(..., label=None)`; `bot.build_app(..., youtube=None)`; `report.window_start(conn, clk, settings)`; `allowance.ensure_item_source`, `whole_minutes`, `week_end`. Week coverage: a YouTube week is covered by any ok/partial sync received after that Saturday; others by period overlap.
- §8: after any label change (Telegram or dashboard) the caller runs `allowance.on_measurements_changed` (DR2.7). `handlers_imports.send_due_prompts` never asks about a week ending before `settings.go_live`.
- §11: `jobs.run_daily(conn, clk, settings, bot) -> list[failed step names]`; each step is isolated and any failure makes `jobs.py daily` exit 1.

## §14 Amendments made at verification (Phase 4)

All additive and defaulted; every earlier call site stays valid. Reasons and tests in REVIEW.md Part II.

- §7 `allowance.pooled_minutes` and `gated_split` skip measurement rows whose `period_end` is before `settings.go_live` (DR4.1). Spotify listening is unaffected. `PooledMinutes` gains `screenshot_ocr_min` and `screenshot_ocr_wasteful_min`; `GatedSplit` gains `provenances: frozenset` (excluded from equality).
- §9 `SourceTotal` gains `import_id` (the import covering that week, not the latest of its kind) and `covered_reasons: tuple[(reason, minutes)]`. `GatedRow` gains `wasteful_ungated_min` and `provenances`. `WeekSplit` gains `screenshot_ocr_min`; `AllowanceStatus` gains `spent_screenshot_ocr_min`. New: `report.latest_imports(conn, clk, settings) -> list[ImportSummary]` and `report.import_detail(conn, clk, settings, import_id) -> ImportSummary | None`. The dashboard reads imports only through these.
- §6.4 `vault.scan` resolves `settings.vault_inbox` to its on-disk spelling and **fails** when the inbox folder is missing while any capture is logged (a warning only when there are none) (DR4.2).
- §6.2 A complete past week whose parse yields no app rows is evidence only: NULL period, status `partial`, nothing written, and a reparse in that state deletes nothing.
- §6.1 `EvidenceError`/`LabelError` during YouTube ingest fail the import like a `ParseError`.
- Part I §5 `triage.render_item`, `render_deadline_notice` and `render_week_header` take keyword `tz=None`; callers with `Settings` pass `settings.tz`.
- §8 The bot's capture confirmation distinguishes an override typed in this note from one stored by an earlier capture of the same item.

## §15 Post-build amendment — automated Cold Turkey (DR5.1)

- `config.Settings.coldturkey_db: Path | None` (`DRBR_COLDTURKEY_DB`); None = manual exports only.
- `sources/coldturkey_local.py`: `hostname(url) -> str | None`; `read_host_minutes(db_path, *, first_day, last_day, tz) -> dict[(date, host), minutes]` (read-only, raises `ColdTurkeyLocalError`); `export_document(totals) -> bytes`; `window(clk, settings) -> (first, last) | None`; `sync(conn, clk, settings) -> Outcome | None`.
- `coldturkey.ingest(..., origin="export" | "local_db")`; import `meta.origin`, measurement `detail.origin`; `reparse` keeps the origin.
- `jobs.run_daily` step `coldturkey_local` runs before `import_prompts`.
- `handlers_imports.prompt_text(kind, week, *, automated_coldturkey=False)`.
- `check_env.py` step 8 checks the database is readable (counts only).

## §16 Post-build amendment — no debt carry-over (DR5.2)

- Migration `0003_no_debt.sql`, schema_version 3: `weeks.overage_min INTEGER NULL CHECK >= 0`; `debt_min` zeroed.
- `db.Week.overage_min: int | None`; `effective_allowance_min` = `allowance_min` (ignores `debt_min`).
- `db.roll_over_week` records `closed.overage_min` and opens the new week with `debt_min = 0`.
- `allowance.debt_for` removed → `allowance.overage_for(week, pooled) -> int`; `on_measurements_changed` re-records `overage_min` for every week.
- `report.AllowanceStatus.debt_min` → `overage_min`; `report.WeekSplit` gains `allowance_min`, `overage_min`.
- `triage.render_week_header(..., last_week_overage_min=None)`.

## §17 Post-build amendment — desktop apps from Qbserve (DR5.3)

- Migration `0004_desktop_apps.sql`, schema_version 4: rebuilds `sources`, `imports`, `measurements`, `prompts_sent` with widened CHECKs (`kind` + `desktop_app`; `provenance` + `app_tracker`; import `kind` + `qbserve`; prompt `kind` + `qbserve_stale`). Columns, constraints, indexes and rows are unchanged.
- `db.migrate` runs each step with `PRAGMA foreign_keys=OFF` (set before `BEGIN`), runs `PRAGMA foreign_key_check` before committing, and raises `db.MigrationError` (whole step rolled back) on any violation. Foreign keys are back ON afterwards.
- `drbr_types`: `KIND_DESKTOP_APP = "desktop_app"`, `GROUP_DESKTOP_APPS = "desktop_apps"` (in `REPORT_GROUPS`; `report_group(KIND_DESKTOP_APP)`), `PROV_APP_TRACKER = "app_tracker"` (in `PROVENANCES`, between export and vault), `IMPORT_KINDS` + `"qbserve"`, `COVERED_ELSEWHERE[("desktop_app", "com.spotify.client")]`.
- `labels.normalise_key("desktop_app", bundle)` → bundle casefolded; known Mac X bundles → `("x", "x")`.
- `config.Settings.qbserve_db: Path | None` (`DRBR_QBSERVE_DB`); None = desktop apps not measured.
- `evidence.upsert_measurement` accepts `PROV_APP_TRACKER`.
- `sources/qbserve.py`:
  - `read_app_minutes(db_path, *, first_day, last_day, tz) -> (list[AppDay], facts)` is read-only and raises `QbserveError`;
  - `document(rows, facts, *, first_day, last_day, tz) -> bytes` and `parse(data) -> (first, covered_through | None, rows)`;
  - `window(clk, settings)`, `sync(conn, clk, settings) -> Outcome | None`, `reparse(conn, clk, settings, import_id)`;
  - `staleness_hours(conn, clk) -> float | None`.

  An import's period ends at the local day of Qbserve's newest record, and is NULL when nothing in the window was recorded.
- `jobs.run_daily` step `qbserve` runs after `coldturkey_local` and before `import_prompts`. While `staleness_hours > 36` and the diagnostic hasn't ended, it sends one Telegram warning per local day.
- `report`:
  - `IMPORT_KIND_FOR_PROVENANCE[PROV_APP_TRACKER] = "qbserve"`;
  - an `EXPECTED_WEEKLY` row for Qbserve, applied only while `settings.qbserve_db` is set;
  - `GROUP_BLIND_SPOTS[GROUP_DESKTOP_APPS]`;
  - `QBSERVE_STALE_HOURS = 36`, and `blind_spots` adds a staleness / never-imported line.
- Dashboard: group name "Desktop apps", badge `("app tracker", "apptracker")` with its own CSS rule, and import kind name "Qbserve".
- `reparse.py` dispatches `qbserve`. `check_env.py` step 9 checks the database shape and prints counts and timestamps only.

## §18 Post-build amendment — the tool source label (DR5.5)

- Migration `0005_tool_label.sql`, schema_version 5:
  - rebuilds `sources` with `label CHECK (label IN ('wasteful','worthwhile','tool'))`; every column, row, id, `UNIQUE (kind, source_key)` and inbound foreign key is unchanged;
  - `item_labels.label` is **not** widened;
  - data step: `label='tool'`, `labelled_at` = migration time (UTC, `YYYY-MM-DDTHH:MM:SS+00:00`) for `kind='web_domain' AND source_key IN ('127.0.0.1','localhost')`.
- `drbr_types`: `TOOL = "tool"`, `SOURCE_LABELS = (WASTEFUL, WORTHWHILE, TOOL)`. `LABELS` is unchanged (item overrides).
- `labels`:
  - `set_source_label` and `list_sources(label=)` accept `tool`;
  - `set_item_override` and `parse_override_token` stay binary (no `#tool`);
  - `resolve` precedence is unchanged: override, then source label (which may now be `tool`, basis `source_label`), then the wasteful default. `ResolvedLabel.label` may be `tool` only via a source label;
  - `sources_needing_label`: tool counts as labelled.
- `allowance`:
  - `GroupMinutes.tool_min` and `PooledMinutes.tool_min` (defaulted 0.0). Tool minutes are in neither `wasteful_min` nor `worthwhile_min`, nor in `screenshot_ocr_min`/`screenshot_ocr_wasteful_min`;
  - `gated_split` skips tool minutes;
  - `promotion_cost` is 0 for `tool` (and `db.promote` never refuses it);
  - `overage_for` and `poll.check_alerts` are unchanged in code: they read `wasteful_min`, which excludes tool.
- `report`:
  - `SourceTotal.minutes` counts content labels only; new `SourceTotal.tool_min`;
  - `WeekSplit.tool_min`;
  - `allowance_status` and `cost_per_keeper` exclude tool minutes;
  - `records(label=)` accepts `tool`;
  - `TOOL_SCOPE_LINE` is the first `blind_spots` line.
- Dashboard:
  - `POST /labels/<id>` accepts `label=tool` (HTMX and plain POST, same `Sec-Fetch-Site` refusal) and still calls `allowance.on_measurements_changed`;
  - `/labels?label=tool` and `/records?label=tool`;
  - `TOOL_TEXT = "tool → not content, not counted"`;
  - Overview: the scope line, a tool column in weekly totals and in the per-week table.
- `handlers_labels`: callback `d:lbl:<w|v|t>:<id>`; third button `TOOL_BUTTON = "Tool (not content)"`; `CONTENT_QUESTION` in the prompt; the edited message reads "labelled tool — not counted".
- `triage.label_line` appends `TOOL_COST` for tool. `bot`'s capture confirmation says a tool-source item costs 0 and how to override it.
- Alert and header text: "wasteful content minutes" (`poll._approaching_text`, `_exceeded_text`, `triage.render_week_header`).

## §19 Post-build amendment — the UX overhaul (DR5.6)

Telegram keeps captures, Sunday triage, allowance alerts and the Monday import
reminder. Everything else is the dashboard's.

### `report.py` (new; all read-only, all `(conn, clk, settings)`)

```python
needs_attention(conn, clk, settings) -> list[NeedsRow]     # rows needing the user first, then automatic
provenance_map(conn, clk, settings) -> list[ProvenanceRow] # "where each number comes from"
needs_your_eye(conn, clk, settings) -> list[EyeRow]        # OCR rows a person can resolve, newest first

@dataclass(frozen=True)
class NeedsRow:
    group: str            # T.GROUP_* incl. the new T.GROUP_VAULT
    label: str            # "iPhone apps", "Desktop web", "Vault (Obsidian)" … (report.GROUP_TEXT)
    mode: str             # report.MODE_AUTOMATIC | report.MODE_MANUAL
    schedule_text: str    # "every morning 09:30" | "every morning 09:17" | "weekly, you run it" |
                          # "continuously while it runs, read every morning 09:30"
    last_data_at: str | None   # UTC ISO instant of the newest data
    covered: bool         # is any part of the CURRENT week covered
    state: str            # report.STATE_OK | STATE_WAITING | STATE_MISSING | STATE_STALE | STATE_ATTENTION
    action_text: str      # "" when the user owes nothing — sort/count on this, not on state
    detail: str           # one sentence: why

@dataclass(frozen=True)
class ProvenanceRow:
    group: str; label: str; measures: str; tool: str
    provenance: str            # T.PROV_*
    provenance_text: str       # T.PROVENANCE_TEXT[provenance]
    mode: str; schedule_text: str
    limits: tuple[str, ...]    # reuses report.GROUP_BLIND_SPOTS
    folds_into: str | None = None   # T.GROUP_X on both X rows

@dataclass(frozen=True)
class EyeRow:
    measurement_id: int; group: str; source: str
    day: str; period_end: str          # local dates
    status: str; provenance: str       # always T.PROV_SCREENSHOT for now
    what_was_read: tuple[str, ...]     # detail.raw, verbatim ("Facebook | 2h 27m")
    options_min: tuple[float, ...]     # every duration the evidence contains, ascending
    suggested_min: float | None        # only when exactly one candidate; never invented
    detail: str
```

Constants the dashboard may render: `report.GROUP_TEXT`, `MODE_AUTOMATIC`,
`MODE_MANUAL`, `STATE_*`, `NEEDS_STATES`, `SCHEDULE_DAILY_JOB`,
`SCHEDULE_SPOTIFY_POLL`, `SCHEDULE_MANUAL_WEEKLY`, `SCHEDULE_CONTINUOUS`,
`STALE_HOURS`, `DIAGNOSTIC_ENDED_NOTE`, `X_FOLD_NOTE`, `EYE_PROVENANCES`.
`needs_attention` rows for the three groups covering X are not separate; X's
two collection paths appear only in `provenance_map`.

### `labels.py` (new)

```python
label_queue(conn, clk, settings, *, limit=None) -> list[QueueRow]   # by minutes DESC, then first_seen_at, id
bulk_set_label(conn, clk, source_ids, label, *, settings) -> int    # commits once; runs on_measurements_changed
unlabelled_summary(conn, clk, settings) -> Summary

@dataclass(frozen=True)
class QueueRow:
    source_id: int; kind: str; group: str; display_name: str
    minutes: float                    # measured minutes in the diagnostic window (0.0, never None)
    provenances: tuple[str, ...]      # T.PROV_* seen for this source
    seen_in: tuple[str, ...]          # the same, in words (labels.PROVENANCE_SEEN)
    first_seen_at: str

@dataclass(frozen=True)
class Summary:
    count: int; minutes: float; top_group: str | None
```

`bulk_set_label` takes `settings` keyword-only because it re-records every
week's overage itself; ids that no longer exist are skipped, not raised, and
the return value is the number of rows actually changed. `sources_needing_label`
is unchanged and still used by the Qbserve tests; `label_queue` shares its
exclusions (covered elsewhere, the unknown-YouTube-channel bucket, no evidence)
but has no per-source cooldown.

### `sources/screentime.py` (new)

```python
correct_measurement(conn, clk, settings, measurement_id, minutes) -> None   # commits; ValueError subclass on refusal
class CorrectionError(ValueError)
MAX_CORRECTION_MIN = 1440
```

Sets `minutes`, `status='ok'` and appends to `detail.corrections`
(`{at, by: "person", minutes, previous_minutes, previous_status}`) with
`detail.corrected_by_person = True`; `detail.raw` is untouched and provenance
stays `screenshot_ocr`. Refuses a non-number, NaN/inf, a negative value, one
over `MAX_CORRECTION_MIN`, an unknown id, or a non-`screenshot_ocr` row. Then
runs `allowance.on_measurements_changed`.

Measurement `detail` gains `readings: list[float]` (every duration the row was
read as) and `unreadable_reason: 'conflicting_durations' | 'no_duration' | null`.
Screen Time import `meta` gains `unnamed_row_texts: list[str]`.

### `handlers_labels.py` (replaced)

```python
send_label_digest(conn, clk, settings, bot) -> list[str]   # one summary line, or []
digest_text(summary: labels.Summary, settings) -> str
labels_url(settings) -> str                                # http://127.0.0.1:<dashboard_port>/labels
cmd_labels(update, context)                                # /labels: the same sentence, no buttons
register(app)                                              # CommandHandler only
DIGEST_EVERY = timedelta(days=3); LABEL_PROMPTS_AFTER = timedelta(days=7); PROMPT_KIND = "labels"
NOTHING_WAITING; CONTENT_QUESTION
```

Removed: `send_label_batch`, `render_prompt`, `on_label_callback`,
`callback_data`, `CALLBACK_PREFIX`, `TOOL_BUTTON`, `_keyboard`, `_seen_in`,
and the `d:lbl:` namespace entirely. The digest's 3-day gate reads
`MAX(sent_on) FROM prompts_sent WHERE kind='labels'`; `/labels` writes no
`prompts_sent` row.

### `handlers_imports.py`

`prompt_text(kind, week, *, automated_coldturkey=False)` is unchanged in shape
and rewritten in content: what is missing, `MANUAL_LINE`, numbered steps
(`SCREENTIME_STEPS`, `YOUTUBE_STEPS`, `COLDTURKEY_STEPS`) and
`AUTOMATIC_TRAILER`. `send_due_prompts` skips `coldturkey` while
`report.needs_attention` reports `GROUP_DESKTOP` as automatic and `ok`.

### `jobs.py`

```python
run_sync(conn, clk, settings, bot=None) -> dict[str, StepResult]   # SYNC_STEPS, in order
@dataclass(frozen=True)
class StepResult: name: str; ok: bool; summary: str; error: str | None = None
SYNC_STEPS = ("read_outbox", "vault_scan", "coldturkey_local", "qbserve")
```

`run_daily` = `run_sync` + `import_prompts`, `label_digest` (renamed from
`label_batch`), `diagnostic_end`; it still returns the names of failed steps.
Without a `bot`, the Qbserve staleness warning is skipped and does not consume
that day's slot. **The HTTP endpoint for "Sync now" is not built here** —
`dashboard/app.py` is owned elsewhere.

### `drbr_types.py`

`GROUP_VAULT = "vault"`. Not in `REPORT_GROUPS`, not a `report_group()` result:
the vault produces counts, never minutes. It exists so the attention and
provenance tables can name it.

## §20 Post-build amendment — the weekly insights summary (DR5.8)

A written summary at the top of the Overview, generated once a week from the
figures `report.py` already computes. Aggregates only leave this Mac.

### `insights.py` (new)

```python
build_payload(conn, clk, settings) -> dict          # aggregate-only facts; read-only, deterministic
write_summary(conn, clk, settings, *, client=None, force=False) -> Insight   # never raises
fallback_summary(payload: dict) -> str              # DRBR's own sentences, computed locally
latest(conn, clk, settings) -> Insight | None       # the newest stored row, or None

@dataclass(frozen=True)
class Insight:
    summary: str                  # the paragraph to print
    created_at: str               # UTC ISO instant (clock.iso)
    model: str | None             # None IS the fallback — nothing else to branch on
    week_start: str               # the allowance week it was generated in, UTC ISO
    fallback_reason: str | None   # printable, verbatim; None exactly when a model wrote `summary`
    based_on_through: str | None  # LOCAL date YYYY-MM-DD in settings.tz; None when no week is complete

MODEL = "claude-opus-5"; MAX_TOKENS = 400; PAYLOAD_SCHEMA = 1; WEEKS = 8
STATUS_OK = "ok"; STATUS_FAILED = "failed"        # the stored column; nothing renders it
REASON_NO_KEY; REASON_TOO_LITTLE; REASON_CALL_FAILED; REASON_UNKNOWN
SYSTEM_PROMPT; USER_PROMPT_HEADER; user_prompt(payload) -> str
class SummaryUnusable(RuntimeError)
```

**The `insight` context key** (what the design session renders). The Overview's
`render_template("overview.html", …)` call carries `insight`, appended after
`flash` and never reordered. It is an `Insight` or `None`.

- `insight is None` — nothing has been generated yet. Render no panel.
- `insight.model is None` — DRBR wrote the sentences itself. **This, and only
  this, is the fallback test**; there is no `is_fallback` and no `status` on the
  dataclass. Print `insight.fallback_reason` beside the summary: it is the real
  reason in words ("No ANTHROPIC_API_KEY is set, so DRBR wrote these sentences
  from its own figures.", "API call failed: RuntimeError: …", "Too little data:
  …"), never a code, and is non-None exactly when `model` is None.
- `insight.model` set (`"claude-opus-5"`) — a model wrote it;
  `fallback_reason` is None.
- `insight.summary` is one paragraph of plain text: 3–5 short sentences, no
  markdown, no headings, no lists. Render it as text, not as markup.
- `insight.created_at` is a UTC instant; the `local_dt` filter formats it.
- `insight.based_on_through` is a **local date** (`YYYY-MM-DD`, `settings.tz`) —
  the Saturday of the last complete measurement week. Directly comparable to
  `report.window_start(...)`, to a `T.MeasurementWeek.end` and to
  `T.iso_date(T.local_date(clk.now(), tz))`, which is what lets the panel say
  the summary was written before later data arrived. It is never an instant and
  is never derived from `week_start`.

`POST /insights/refresh` → endpoint `refresh_insight`. Same-origin only (the
standing `Sec-Fetch-Site` guard), 303 to the Overview with `?msg=` =
`insight-written` (level `ok`) or `insight-fallback` (level `warn`). Both codes
are in `FLASH_TEXT`.

### The payload, field by field

Aggregate-only (DR5.8). Deterministic, JSON-serialisable, every float rounded
to 1 place, lists in fixed order.

```
schema, generated_for_week_start, timezone, based_on_through, complete_weeks,
weekly_allowance_min
diagnostic: {started, ended, start, end, week_number, total_weeks}
weeks[]:    week_start, days {from, to}, wasteful_min, worthwhile_min, tool_min,
            screenshot_ocr_min, allowance_min, overage_min, reflex_wasteful_min,
            reflex_share_of_wasteful, unmeasured_items,
            change_from_previous_week {wasteful_min, wasteful_pct, worthwhile_min,
                                       worthwhile_pct, tool_min} | null,
            by_group[]: group, wasteful_min, worthwhile_min, tool_min, gated_min,
                        ungated_min, share_ungated, wasteful_ungated_min,
                        unmeasured_items, covered_excluded_min
            coverage_gaps[]: one sentence per weekly import that never arrived
cost_per_keeper[]: group, minutes, keepers, minutes_per_keeper
funnels: pop {captured, survived_lock, promoted, played, fully_played, *_rate},
         vault {captured, unprocessed, kept, excluded, edited_kept}
```

`group` is always one of the fixed `T.REPORT_GROUPS` constants (plus
`GROUP_WEB` / `GROUP_OTHER` in `cost_per_keeper`), never a source key. The only
prose is `coverage_gaps`, assembled from `report.GROUP_TEXT` and
`report.EXPECTED_WEEKLY`; stored import warnings and errors are never copied.
No source name, item title, note title, path, URL or video id can reach it.

### `db.py` / migrations

`SCHEMA_VERSION = 6`. `migrations/0006_insights.sql` adds `insights (id,
created_at, model, week_start, summary, payload, status CHECK IN ('ok','failed'),
error, input_tokens, output_tokens)` plus `idx_insights_week`. Additive: no table
is rebuilt.

### `jobs.py`

`run_daily` gains an `insights` step after `diagnostic_end`. It is **not** in
`SYNC_STEPS` and **not** added to the returned failed-step list.

### `config.py`

`Settings.anthropic_api_key: str = ""` (`ANTHROPIC_API_KEY`, in `_KEYS`).
Optional; empty means the panel shows DRBR's computed sentences.

### `check_env.py`

`check_anthropic(env)`, step 10 of 10 (`STEPS = 10`). Shape only
(`ANTHROPIC_SHAPE`), never a live call; unset is an `OK`, not a failure. The key
is in `SECRET_KEYS`, so it is only ever printed masked.

## §21 Post-build amendment — the Sunday session (DR5.24–DR5.27)

### `checklist.py` (new, DR5.24)

The only thing that asks the user for anything. Read-only apart from two
`meta` keys; no table changes, no migration.

```python
CHECKLIST_KEY = "checklist_week"            # week (Sunday, ISO) whose Sunday list was dealt with
NUDGE_KEY = "checklist_nudge_week"          # week whose one reminder was decided
LABELS_WORTH_A_MESSAGE_MIN = 60.0           # labels alone send only from an hour undecided (DR5.19's bar)
MISSING = "missing"; CUT_OFF = "cut_off"; IN = "in"
SCREENTIME_TODO; SCREENTIME_AGAIN; YOUTUBE_TODO; COLDTURKEY_TODO ({span});
COLDTURKEY_FAILING; COLDTURKEY_MANUAL; AUTOMATIC_LINE ({what}); LAST_REMINDER ({url})

url(settings, path) -> str                  # http://127.0.0.1:<dashboard_port><path>
asking_week(conn, clk, settings) -> T.MeasurementWeek | None
    # the week just finished (Sun..Sat ending on or before yesterday); None if it
    # ended before go-live or starts after the diagnostic's last day. The final
    # week is still asked for after the diagnostic has ended.
@dataclass(frozen=True)
class WeekNeeds: week; screentime: str; youtube: str; coldturkey: str | None;
                 readings: int; unlabelled: labels.Summary; final: bool
    evidence_missing -> bool   # screentime != IN or youtube != IN or coldturkey == MISSING
    owed -> bool               # evidence_missing or readings > 0 or unlabelled.minutes >= 60
week_needs(conn, clk, settings, week) -> WeekNeeds     # read-only
render(needs, settings, *, nudge: bool, late: bool = False) -> str
send_due(conn, clk, settings, bot) -> list[str]        # e.g. "sunday checklist for week 2026-10-04: screentime, youtube, labels"
```

* Screen Time: `report._covering_import`; the latest covering import with
  `meta.most_used_complete is False` is `CUT_OFF`; `None`/absent is `IN`.
* YouTube: `report._covering_import` (a sync received after the Saturday).
* Cold Turkey: `None` while `report.needs_attention` reports `GROUP_DESKTOP`
  automatic and `ok`; otherwise an ok/partial import whose period spans the
  whole week is `IN`, else `MISSING`.
* Readings: `len(report.needs_your_eye(...))`. Labels: `labels.unlabelled_summary`.
* `send_due`, by the local day of the run (W = `asking_week`):
  Sunday — the list if `owed` and `checklist_week != W`; `checklist_week = W`
  after a successful send, or at once when nothing is owed. Monday — if
  `checklist_nudge_week != W`: the evidence-only reminder when
  `evidence_missing`, or, when Sunday's list never went out, the whole list
  (`late=True`) when `owed`. Tue..Sat — the reminder only when
  `checklist_week == W` and `checklist_nudge_week != W`. Otherwise nothing.
  `checklist_nudge_week = W` after a successful send, or when nothing is to be
  sent. Sends through `triage.send`; a failed send raises and writes no marker.
* The order of steps is fixed: Screen Time, YouTube, Cold Turkey, readings,
  labels. The reminder carries evidence steps only.

### `handlers_imports.py`

Removed: `PROMPT_KINDS`, `AUTOMATIC_TRAILER`, `SCREENTIME_STEPS`,
`YOUTUBE_STEPS`, `COLDTURKEY_STEPS`, `MANUAL_LINE`, `prompt_text`, `_covered`,
`_self_feeding` (now `checklist._self_feeding`), `send_due_prompts`. The Screen
Time reply gains "N readings couldn't be read for sure. Settle them here:
<url>/#needs-your-eye" when rows of the import's own period are waiting.

### `handlers_labels.py`

Removed: `DIGEST_WEEKDAY`, `DIGEST_MIN_MINUTES`, `PROMPT_KIND`,
`_last_digest_day`, `due_digest`, `record_digest`, `send_label_digest`.
`digest_text(summary, settings) -> str` is the `/labels` reply only. `/labels`
still answers after the diagnostic ends while `checklist.asking_week` is not
None.

### `jobs.py`

`run_daily` = `run_sync` + `checklist`, `diagnostic_end` (+ `insights`, not
counted as a failure). `send_end_notice` waits while the final week's evidence
is missing, until `final.saturday + 3 days`; its last sentence is "Nothing
more will be asked for."

### `triage.py`

`NOTHING_LINE` is removed. `run_triage` posts nothing at all for an empty
queue: the week header goes out only when at least one item was posted. Steps
1–3 and the marker are unchanged.

### `report.py`

`due_on(mweek) = mweek.saturday + 1 day`; `week_is_due` reads it (§9).
`_weekly_manual_state` uses `week_is_due`. `prompts_sent` no longer receives
`screentime`, `youtube`, `coldturkey` or `labels` rows; old rows stay and the
CHECK constraint is untouched.
