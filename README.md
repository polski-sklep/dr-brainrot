# DRBR — Doctor Brain Rot

An eight-week diagnostic of **brain-rot content**: how much content you
consume across Spotify podcasts, YouTube, iPhone apps, desktop websites, desktop
apps, X and audiobooks, and how much of it is brain rot. It is not a screen-time or
productivity tracker: shopping, email, banking and other errands are out of
scope. You label content sources wasteful or worthwhile, and anything that
isn't content at all as **tool** (not counted). The system counts content
minutes, gates only the wasteful ones through POP's deliberate queue, checks
whether captured material was ever processed in Obsidian, and reports it on a
local dashboard.

DRBR absorbs **POP**, the podcast queue: capture instead of play, a why-note, a
48-hour lock, weekly triage against an allowance. POP's behaviour is unchanged
except where labels now decide what counts. Personal tool, single user, never
productised. **It has an end date.** After eight weeks it stops and asks you
to name the decision it should change, or shut it down.

> The user labels. The system counts.

| Document | What it is |
|---|---|
| `drbr-build-brief.md` | The product brief (authority) |
| `EXISTING.md` | POP as it actually was before DRBR, plus where the brief was wrong about it |
| `CONTRACTS.md` | Part I: POP's module contracts. Part II: DRBR's, including amendments |
| `DECISIONS.md` | Every choice made where the brief was silent (POP `D*`, DRBR `DR*`) |
| `REVIEW.md` | Independent audits: Part I POP, Part II DRBR |
| `docs/build-notes/` | The Phase 2 agents' notes on real-data findings |

---

## Prerequisites

- macOS (Apple Vision reads the Screen Time screenshots on-device; launchd runs everything).
- Python 3.12 (`brew install python@3.12`).
- Chrome or Brave, signed in to YouTube, for the history extension.
- Cold Turkey Blocker on the desktop.
- The Obsidian vault on this Mac, and the `obsidian-ingest` bot (`~/Projects/obsidian-ingest`, merged with the capture-id change).
- A Telegram bot token, a Spotify app, and a YouTube Data API key (below).

## Installation

```bash
cd ~/Projects/drbr
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # then fill it in, see Environment
```

## Migration and setup

The database is `data/pop.db` (SQLite, gitignored). `schema.sql` is the
version-1 POP baseline. Numbered files in `migrations/` bring it forward, one
transaction per step (`db.migrate`), and every process runs this at start. The
upgrade from a pre-DRBR POP database is additive: no POP row is rewritten
(`tests/test_drbr_migration.py`, `tests/test_drbr_e2e.py`). Migration 0004
rebuilds four DRBR tables to widen their CHECK constraints, copying every row;
it runs with foreign keys off and refuses to commit if a foreign key check
fails. Migration 0005 rebuilds `sources` the same way so a source can be
labelled `tool`, and labels DRBR's own dashboard host (`127.0.0.1` /
`localhost`) tool; nothing else changes.

**Back up before any upgrade:**

```bash
sqlite3 data/pop.db ".backup data/pop-$(date +%Y%m%d).db"
```

## Environment

All configuration is in `.env`. `.env.example` lists every key with placeholders.

| Key | Meaning |
|---|---|
| `SPOTIFY_*`, `TELEGRAM_*` | POP credentials, unchanged (see below) |
| `WEEKLY_ALLOWANCE_MIN` | Pooled **wasteful content** minutes per week across every source (360); tool minutes never count |
| `POP_TZ` | The app's timezone (`Europe/Warsaw`). Week = Sunday 18:00 → Sunday 18:00 |
| `LOCK_HOURS`, `LIFESPAN_CYCLES` | POP's lock and queue lifespan |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key, used only for `videos.list` (durations, channels) |
| `DRBR_VAULT_PATH` | Absolute path to the Obsidian vault. Never committed |
| `DRBR_VAULT_INBOX` | Folder whose notes count as unprocessed (`01-Inbox`, including subfolders) |
| `DRBR_CAPTURE_OUTBOX` | JSONL file obsidian-ingest appends captures to (`data/vault_captures.jsonl`) |
| `DRBR_RAW_DIR` | Raw evidence kept for re-parsing (`data/raw`) |
| `DRBR_COLDTURKEY_DB` | Cold Turkey's local statistics database, read daily (hostnames only). Empty = manual exports |
| `DRBR_QBSERVE_DB` | Qbserve's database, read daily for desktop app time (per app per day; no window titles). Empty = desktop apps not measured |
| `DRBR_AUDIBLE_AUTH` | **Optional.** The Audible sign-in `audible_login.py` saves (default `data/audible_auth.json`, beside `audible_store.json`). Not there = audiobooks not measured |
| `DRBR_GO_LIVE` | First local date that counts (a Sunday). Earlier history is evidence only |
| `DRBR_DIAGNOSTIC_WEEKS` | Length of the diagnostic (8) |
| `DRBR_DASHBOARD_PORT` | Dashboard and extension ingest port on 127.0.0.1 (8750) |
| `DRBR_INGEST_TOKEN` | Shared secret the extension sends. `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `ANTHROPIC_API_KEY` | **Optional.** Only the Overview's weekly summary uses it (one Claude Opus 5 call a week, aggregates only). Empty = DRBR's own computed sentences |

**Spotify:** create an app at developer.spotify.com, with redirect URI exactly
`http://127.0.0.1:8888/callback`. Then run `.venv/bin/python auth_spotify.py`
once and grant "View your listening position in podcasts".

**YouTube:** console.cloud.google.com → project → enable **YouTube Data API
v3** → Credentials → API key, restricted to that API. One sync costs a few
quota units out of 10,000 a day.

## Credential verification

```bash
.venv/bin/python check_env.py          # add --ping to send yourself a Telegram test message
```

Checks every key's shape, `.env` permissions, `.gitignore` coverage, live
Telegram/Spotify/YouTube auth, the vault and inbox paths, that the outbox and
raw-evidence locations are writable, that Apple Vision is importable, and
that the Cold Turkey and Qbserve databases (when configured) are readable.
Exit codes: 0 passed, 1 failed, 3 unproven (a provider was unreachable,
credentials not known to be bad).

## Start everything

```bash
./drbr start
```

This one command validates the environment (it refuses to start on a
failure), migrates the database, installs and loads every launchd agent,
makes sure obsidian-ingest is loaded, and waits until the dashboard answers.
The other subcommands are `./drbr status`, `./drbr stop` and `./drbr restart`.

| Agent | Schedule | What |
|---|---|---|
| `com.jacob.pop.bot` | always on | Telegram: capture, triage buttons, screenshot/export imports, labels |
| `com.jacob.pop.poll` | daily 09:17 | Spotify positions, lock lifts, deadline posts, allowance alerts |
| `com.jacob.pop.triage` | Sunday 18:00 | Week rollover (records last week's overage), expiry, the triage post, only when the queue has something |
| `com.jacob.drbr.jobs` | daily 09:30 | Outbox read, vault scan, Cold Turkey read, Qbserve read, phone browser read, Audible read; then, as separate steps, Qbserve's stale warning, the Sunday check-in and its one Monday reminder (or the whole list late, on the first morning after a missed Sunday and Monday), end notice |
| `com.jacob.drbr.dashboard` | always on | `http://127.0.0.1:8750/` plus the extension's ingest API |
| `com.jacob.drbr.qbserve` | every 10 min | Reopens Qbserve in the background if it isn't running; never touches its data (DR5.27) |

Telegram being unreachable never stops a data step. Each scheduled run reads
and records first and sends after; a message that can't go is logged, the run
exits 1, and the log says which message didn't go (DR5.25). The bot token is
scrubbed from every log line, tracebacks included.

launchd, not cron, because macOS cron skips runs missed while the Mac sleeps.
launchd catches up at the next wake. The system timezone and `POP_TZ` must
agree, or triage closes the wrong week (see POP's DECISIONS on timezone).
There is no VPS: everything runs on this Mac, which is also where the vault
and browser are (DR0.3).

## Telegram

One bot (POP's), commands `/start`, `/queue`, `/stats`, `/labels`. Only
`TELEGRAM_USER_ID` is answered; everyone else is silently ignored. Press
**Start** in the chat once, or the bot can never message you first.

- **Capture:** send a Spotify episode link **or a YouTube link**. The bot shows
  the duration and asks why. Your reply is the why-note. Nothing is stored
  until you reply.
  - Deadline: end the note with `by friday` / `before 3 oct`.
  - Label override for this one item: put `#worthwhile` (`#worth`) or
    `#wasteful` (`#waste`) anywhere in the note.
  - The confirmation shows the resolved label and where it came from.
- **Triage** (Sunday 18:00): each unlocked item comes with Promote / Drop /
  Still holds?, then the week's header. A wasteful promotion costs its
  remaining minutes. A worthwhile one costs 0, but still waits out the lock
  (DR1.3). An empty queue posts nothing; the week still rolls over. It also
  rolls over when Telegram can't be reached: an item whose post didn't go
  keeps its cycle and comes back next Sunday, and `/queue` still lists it.
- **Imports:** send the Screen Time screenshots (and a Cold Turkey JSON export only if its automatic read fails)
  straight to the bot (below). It replies with what it stored, what it couldn't
  read, or why it failed.
- **The Sunday check-in** (09:30): one message listing what the week just
  finished (Sun–Sat) still needs from you, numbered, always in this order:
  Screen Time "Last Week" screenshots, the YouTube sync, any readings to
  settle, then labelling new sources on the dashboard (last, because the first
  two bring new sources). What already arrived is listed as "Already in". When
  everything is in, nothing is sent. If evidence is still missing on Monday,
  one reminder follows, and nothing more for that week; the dashboard keeps
  showing what's missing. If the Mac was asleep (or Telegram down) on Sunday
  and Monday, the whole list comes once, on the first morning it runs. A
  Screen Time capture that stopped at "Show More" is asked for again, once,
  also for an older week while the iPhone still has it (about four weeks).
  Cold Turkey appears only if its automatic read fails. (DR5.24)
- **Labels** happen on the dashboard's Labels page, never in the chat;
  `/labels` replies with how many are waiting and the link. Unlabelled sources
  count as wasteful. X is fixed wasteful. The `#` override tokens stay
  wasteful/worthwhile only: a captured item is content.

## Labels

DRBR measures **brain-rot content only**. Label each source by asking: is this
*content* I consume? If yes, is it brain rot (**wasteful**) or not
(**worthwhile**)? If I only go there to get a task done — buy, pay, book, log
in, organise, check the weather — it's a **tool** and isn't counted.

- **Tool is a scope setting, not a middle score.** Tool minutes never count as
  wasteful or worthwhile, never charge the allowance, never trigger alerts or
  overage, and are left out of cost per keeper and the gated split.
- **Tool is still shown.** The dashboard prints tool minutes per week and per
  source group beside the content figures, so a label can't hide consumption.
- **Relabel when the use changes.** amazon.pl is a tool while it's errands;
  relabel it wasteful if you browse it for entertainment. Every relabel
  re-records each week's overage immediately.
- **Item overrides stay binary.** `#wasteful` / `#worthwhile` on a captured item
  beats a tool source; there is no `#tool`.
- **One automatic tool label:** migration 0005 labels DRBR's own dashboard host
  (`127.0.0.1` / `localhost` web domains) tool, because it is the measuring
  instrument. Nothing else is labelled for you.
- Unlabelled sources still count as wasteful until you decide (DR5.5).

## Chrome extension (YouTube)

Load `extension/` unpacked (Chrome `chrome://extensions` or Brave
`brave://extensions` → Developer mode → Load unpacked). In its **Backend
settings**, set the URL `http://127.0.0.1:8750` and the ingest token from
`.env`. Full details are in `extension/README.md`.

## YouTube ingestion behaviour

Weekly: click **Run weekly sync** with the dashboard running and the history
tab visible. The extension scrolls `youtube.com/feed/history` back to the
previous sync's newest video (the cursor) and posts the page's own data to
the backend. The backend parses it (`sources/youtube.py`):

- **Minutes, regular videos:** the saved resume position, or else the
  progress percentage, multiplied by the true duration from the Data API. The
  bar never shows under 10%, so the position wins when present (DR2.1).
- **Shorts:** assumed fully watched, using the Shorts' real API duration,
  never an average.
- **Account-level:** phone viewing appears here too, so Screen Time's YouTube
  app minutes are excluded from totals (DR1.9).
- **Dates:** from the page's day headings. There is no time of day and no
  sittings. A rewatch overwrites position, so rewatches undercount.
- **Missing data:** a video with no progress, or no API duration, is recorded
  as **unmeasured** with a warning, never as zero.
- **Structure changes:** if YouTube changes its page structure the import
  **fails loudly** and the extension shows **SYNC FAILED**.

## Screen Time weekly procedure

Sunday morning, as step 1 of the check-in (if it hasn't arrived by Monday the
bot reminds you once; Screen Time keeps about four weeks, so a late capture
loses nothing):

1. **Settings → Screen Time → See All App & Website Activity.** Select this
   **iPhone**, not all devices (Share Across Devices would double-count the Mac).
2. Choose **Week**, then tap **back one week**. The header must read **Last
   Week**, not "This Week".
3. Screenshot the summary, then scroll and screenshot the whole **Most Used**
   list, tapping **Show More** until it ends. Overlap between screenshots is fine.
4. Send all the screenshots to the bot, as photos or files, in one go.

It is read by Apple Vision **on this Mac**, and nothing is uploaded. Only
per-app minutes and the top three category totals are used; pickups and
notification counts are ignored. A "This Week" or all-devices capture is kept
as evidence but writes no minutes, and the bot tells you what to resend. Rows
it couldn't read for sure are left unmeasured, and the reply links to
"Readings to settle" on the dashboard.
Misread app names are kept as read, and each becomes a source you label. On
the dashboard, screenshot figures are marked **parsed screenshot, lower
confidence**.

## Cold Turkey (automatic)

Nothing to do weekly. Every day, `jobs.py daily` reads Cold Turkey's own
statistics on this Mac (`DRBR_COLDTURKEY_DB`), read-only:

- **Hostnames only.** Cold Turkey stores full URLs, search terms included.
  They are reduced to hostnames while reading, and nothing finer is stored
  anywhere (DR5.1).
- **Finished days only.** It reads from go-live up to yesterday, and re-reads
  the last 8 days so late corrections land.
- **Every visited domain.** A manual export lists only its chart's top 5.
- **This Mac only.** Cold Turkey doesn't sync between machines.

**If the automatic read fails,** the daily job logs it (`logs/jobs.log`) and
the Sunday check-in asks for a manual export. Manual export: **Cold Turkey →
Statistics → Websites** usage view (time, not "Websites Blocked") →
**Export** → send the JSON to the bot. Unfinished days are dropped, and the
blocked-count export is rejected. Leaving `DRBR_COLDTURKEY_DB` empty turns the
automatic read off.

## Qbserve (automatic)

Nothing to do weekly. Qbserve has to be running to record, so the
`com.jacob.drbr.qbserve` agent checks every 10 minutes (and at login) and
reopens it hidden in the background if it isn't running. It only runs
`pgrep` and `open`, never touches Qbserve's data, and logs one line to
`logs/qbserve.log` each time it reopens it (DR5.27). Every day, `jobs.py daily`
reads Qbserve's database on this Mac (`DRBR_QBSERVE_DB`, usually
`~/Library/Application Support/Qbserve/UserDatabase.sqlite`), read-only
(DR5.3):

- **Apps and minutes only.** Time is summed per app per day. Window titles
  and page names are never read, so they can't be stored anywhere.
- **Your labels, not Qbserve's.** Its productivity ratings and categories are
  never read. New apps are offered for labelling like any other source.
- **Browsers are skipped.** Any app that shows websites in Qbserve is treated
  as a browser. Cold Turkey already measures browsers per site.
- **Double counts excluded.** The Spotify app is covered by Spotify polling.
  The X Mac app counts under X.
- **Finished days only**, re-reading the last 8, like Cold Turkey.

**If Qbserve isn't running,** nothing is recorded, and DRBR won't treat that
as zero. Days after Qbserve's last record stay "not received". Cold Turkey is
the witness for whether that matters: when it saw at least 10 minutes of
browsing on this Mac on days after the day of Qbserve's last record, the Mac
was in use and Qbserve wasn't recording. Any browsing at all counts too once
that record is over 36 hours old. Then the bot sends one warning a day and the
dashboard lists it under blind spots. An idle Mac (no browsing since) never
warns, not even back from a trip. When Cold Turkey isn't read automatically,
or hasn't been read for 36 hours, the old rule applies instead: a last record
over 36 hours old.

**To quit Qbserve on purpose,** stop the keeper first, or it reopens Qbserve
within 10 minutes: `launchctl bootout gui/$(id -u)/com.jacob.drbr.qbserve`
(`./drbr stop` stops it too; `./drbr start` brings it back).

If the database can't be read, the daily job logs a failed `qbserve` step.
Re-parse stored evidence with `reparse.py <import_id>`. Leaving
`DRBR_QBSERVE_DB` empty turns desktop app measurement off.

## Audible (automatic, optional)

Audiobooks are measured but never counted as brain rot (DR5.28). Nothing to do
weekly. Sign in once, yourself, in Terminal:

```bash
cd ~/Projects/drbr && .venv/bin/python audible_login.py
```

It opens Amazon's sign-in page; your password and two-step code go to Amazon
only. It saves `data/audible_auth.json` (readable by you only, never
committed) and `data/audible_store.json`, the store that holds your books.
Every morning `jobs.py daily` then reads, read-only:

- **Audible's own daily listening totals**, in wall-clock minutes, across every
  device on the account. The first read goes back to go-live; after that the
  last 8 days are re-read, so offline listening lands. A day with no listening
  in a read is 0; a day no read covered is unmeasured.
- **Where every book is**, to split each day across the books whose place
  moved since the morning before. The total is measured; the split is
  estimated ("split estimated" on the page). Days before the first read, or
  with no book moving, are "Audible (book not known)".
- **Every book is worthwhile**, labelled when first seen, as your standing
  decision. Relabel one on the Labels page and your label wins.

Screen Time's Audible app is covered elsewhere: it only sees the app while the
screen is on. The client library is unofficial (mkb79's `audible`), so a
change at Amazon can break the read: it then fails loudly and the days stay
unmeasured. If Audible ever refuses the sign-in, the dashboard and the Sunday
check-in say so with the command to sign in again:

```bash
cd ~/Projects/drbr && .venv/bin/python audible_login.py --again
```

**To sign out,** remove the device it registered from your Amazon account's
devices, then delete `data/audible_auth.json` and `data/audible_store.json`.
Audiobooks then stop being measured, and nothing asks about them.

## Obsidian / vault procedure

Nothing to do weekly. obsidian-ingest writes a `drbr_capture_id` into every new
note's frontmatter and appends one line to `DRBR_CAPTURE_OUTBOX` **before**
writing the note. The daily job reads the outbox, then scans the vault by
capture ID, never by path, so renaming or moving a note keeps its identity:

- **unprocessed:** every copy is still under `01-Inbox/`;
- **kept:** it is anywhere else;
- **excluded:** deleted or only in `.trash/`. Excluded notes leave the
  denominator and never count as failures.

Notes from before DRBR, and notes without an ID, are not tracked. "Edited
after capture" compares body hashes (frontmatter excluded) and is a secondary
flag only. Vault numbers are **counts**, never minutes.

If obsidian-ingest's reply says **"DRBR capture log failed"**, the note was
filed but DRBR can't track it. Check that `DRBR_CAPTURE_OUTBOX` is set in
obsidian-ingest's `.env` and that `drbr/data/` exists.

## Dashboard access

`http://127.0.0.1:8750/`, on this Mac only. It binds loopback and rejects
cross-site posts. Every page shows the countdown to the end date.

- **Overview:**
  - the **weekly summary** — three to five plain sentences reading the figures
    below it (see "Weekly summary");
  - weekly totals by source, with provenance badges (API / page data / parsed
    screenshot / manual export);
  - a standing line: brain-rot content only, tools excluded;
  - wasteful vs worthwhile content per week, with tool minutes in their own column (never in the bars);
  - allowance spent, remaining, and how far each week went over (recorded, never carried into the next week);
  - the **reflex** figure: wasteful content minutes that never passed through the queue;
  - gated vs ungated per group;
  - cost per keeper, ranked;
  - funnels, blind spots, and the latest import per kind with warnings.
- **Labels:** the content rule at the top, then every source with inline
  wasteful / worthwhile / tool buttons and a filter for each. Tool reads
  "tool → not content, not counted". Changing a label re-records each week's
  overage immediately.
- **Records:** the raw measurements, filterable by group, label and date.

No targets, scores or streaks, by design.

## Weekly summary

The Overview opens with three to five plain sentences reading the week's
figures — "wasteful content fell 38% week on week; 90% of it bypassed the
queue" — written by Claude Opus 5 from the numbers DRBR already computed.
DECISIONS DR5.8 has the reasoning; the short version:

**What it is.** One generated paragraph a week, describing measured patterns.
It does not say whether anything was worth your time, does not suggest
relabelling anything, and gives no advice — the system counts, you label. Every
number in it has to come from the figures it was given, and those figures are
stored beside it so any sentence can be checked.

**Cadence.** The daily job writes it once per allowance week, after the week has
rolled over, so it always summarises **complete** weeks; the current, unfinished
week is left out. Six mornings out of seven the step is a single database read.
The **Rewrite** button on the panel rewrites it on demand.

**When it is out of date.** The paragraph never changes by itself. When it no
longer matches the page, a chip above it says why, naming one reason, checked
in this order:

1. "a newer week has finished since this was written": a later week is complete.
2. "new data has arrived since this was written": something you send arrived
   after it was written, for the summarised week or the week its headline
   compares it with. That means Screen Time screenshots, a YouTube sync
   (including the first sync after that week, even with no watches from it), a
   Cold Turkey export sent by hand, a reading you settled, or podcast listening
   still inside that week. "After" includes the seconds while it was being
   written. The automatic morning reads (Cold Turkey's own statistics,
   Qbserve, the browser sync) count only when catching up after a gap: none
   had run since the week ended when the summary was written. Otherwise they
   re-read the last eight days every morning, and would mark every summary out
   of date for a week. The vault never counts.
3. "you have relabelled since — these figures no longer match": a label under
   the summarised figures changed.

Press **Rewrite** to bring it up to date.

**What is sent.** Aggregates only — report *group* names (podcasts, YouTube, X,
iPhone apps, desktop web, desktop apps), minutes by label per group per week,
week-on-week changes, allowance and overage, the reflex share, cost per keeper,
funnel counts, and which weeks' evidence never arrived. **No show, channel, app,
domain, video, note title or path ever leaves this Mac**, and nothing from the
vault does. A test seeds the database with distinctive names and fails if any
string in the payload is not a date, a number or one of DRBR's own constants.

**Cost.** One Claude Opus 5 call a week: a few thousand input tokens and at most
400 output tokens, so a few cents a month.

**Turning it off.** Delete or empty `ANTHROPIC_API_KEY` in `.env`. Nothing is
called and nothing is sent; the panel shows DRBR's own computed sentences with
the reason printed beside them ("No ANTHROPIC_API_KEY is set…"). The same
fallback covers a failed call, and an honest "not enough data yet" before the
first week is complete. `check_env.py` treats the key as optional, checks its
shape only, and never makes a live call.

## Backup and recovery

**What to back up:**
- `data/pop.db` (use `sqlite3 ... ".backup"`, not `cp`, while agents run);
- `data/raw/` (all original evidence);
- `data/vault_captures.jsonl`;
- `.env`.

`data/` and `.env` are gitignored. Pre-DRBR backups from the 2026-09-16
migration are in `~/Projects/drbr-backup-20260916/`.

**Restore:**
1. `./drbr stop`
2. Copy the database back.
3. `./drbr start`. Migrations re-apply if needed.

**Lost a parse or improved a parser?** Nothing needs resending; see Troubleshooting.

## Weekly routine

| When | Do |
|---|---|
| Any time | Capture instead of play: send links to the POP bot, with a why and optionally `#worthwhile` |
| Any time | Capture reading/watching material via obsidian-ingest as usual; process notes out of `01-Inbox` when they earn it |
| Sunday morning | The check-in, in its order: Screen Time "Last Week" screenshots, the extension's YouTube sync, any readings to settle, then label new sources on the dashboard (content → wasteful or worthwhile; errands → tool). Everything else is automatic |
| Sunday 18:00 | Triage the queue post in Telegram, when there is one |
| Weekly | Glance at the dashboard: reflex share, cost per keeper |
| Week 8 | Name the decision, or shut DRBR down (`./drbr stop`) |

## Troubleshooting parse failures

Every import is recorded, including failures, with its warnings and error, and
the original bytes are kept.

```bash
.venv/bin/python reparse.py list              # recent imports, status, warnings
.venv/bin/python reparse.py list screentime   # or coldturkey / youtube_history
.venv/bin/python reparse.py 42                # re-parse import #42 from its stored evidence
```

- **YouTube "SYNC FAILED" / no sections:** YouTube changed its page data. Fix
  `sources/youtube.py` against a freshly saved history page, then `reparse.py`
  the failed imports. The cursor didn't move, so nothing is lost. **Quota
  exhausted:** re-parse tomorrow.
- **Screen Time "no minutes written":** check the header said Last Week and the
  view was the iPhone, not all devices. Unreadable rows are listed as
  warnings. After changing the parser, re-parse.
- **Cold Turkey rejected:** you exported the blocked-count chart. Export the
  Websites usage statistics instead.
- **Vault scan failed:** `DRBR_VAULT_PATH` or `DRBR_VAULT_INBOX` is wrong,
  missing, or has the wrong letter case. The scan refuses rather than count
  every capture as kept.
- **Anything else:** look in `logs/` (`bot.log`, `jobs.log`, `dashboard.log`,
  `poll.log`, `triage.log`, `qbserve.log`, `check_env.last`) and run `./drbr status`.

## Tests

```bash
.venv/bin/pytest -q
```

No test touches the network or runs Apple Vision; `conftest.py` blocks both
and blocks reading the real `.env`. An optional local check against your real
screenshots runs only with `DRBR_REAL_OCR=1` and skips when `inputs/` is
absent. obsidian-ingest's tests run from its own directory with the same venv.

## Files

```
bot.py poll.py triage.py db.py spotify.py clock.py config.py   POP, label-aware (CONTRACTS Part I + II §7)
drbr_types.py evidence.py labels.py allowance.py report.py     shared DRBR core
sources/youtube.py screentime.py ocr_vision.py coldturkey.py coldturkey_local.py qbserve.py vault.py
sources/brave_history.py audible.py                             phone browser by site (DR5.13), audiobooks (DR5.28)
audible_login.py                                                one-time Audible sign-in, run by you (DR5.28)
insights.py                                                     the Overview's weekly written summary (DR5.8)
checklist.py                                                    the Sunday check-in and its one reminder (DR5.24)
handlers_imports.py handlers_labels.py                          Telegram extensions
ingest_api.py dashboard/ extension/                             local API, dashboard, browser extension
jobs.py reparse.py check_env.py drbr                            daily job, re-parse CLI, env check, launcher
schema.sql migrations/                                          v1 baseline + numbered migrations
launchd/                                                        agent definitions installed by ./drbr start
tests/                                                          unit, migration, integration, end-to-end
data/ inputs/ logs/                                             gitignored
```

## Known distortions and blind spots

Accepted deliberately, and stated on the dashboard rather than absorbed:

- **Scope:** content only. Sources you label tool are excluded from every
  content figure and shown separately; a mislabelled tool would hide content,
  which is why tool minutes are always on the page.
- **Podcasts:** listening that never went through the queue is invisible (POP
  polls only captured episodes, DR1.11).
- **Spotify accuracy:** scrubbing forward reads as listening, and playback
  above 1x undercounts. Completion is Spotify's `fully_played` flag.
- **YouTube:** rewatches overwrite position and undercount; there is no time of
  day; the bar floor is handled by using the resume position.
- **Cold Turkey:** this Mac's browsers only; hostnames, not pages.
- **Desktop apps (Qbserve):** this Mac only, and only while Qbserve is running;
  browsers are left to Cold Turkey; no keeper path (vault captures aren't
  attributed to apps).
- **Audiobooks (Audible):** worthwhile by your standing decision, so never
  charged. The day's total is Audible's own; its split across books is
  estimated from how far each book's place moved, and every day before the
  first read is "book not known". The client is unofficial and can break. No
  keeper path.
- **Screen Time:** OCR, lower confidence than the API figures; only full past
  weeks count; YouTube and Spotify app minutes are excluded as double counts;
  Spotify music is out of scope.
- **X:** one label, wasteful, with no sub-categories by design. Value taken from X
  shows up as vault keepers, not minutes.
