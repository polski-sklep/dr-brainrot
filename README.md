# DRBR — Doctor Brain Rot

An eight-week diagnostic of where attention actually goes: Spotify podcasts,
YouTube, iPhone apps, desktop websites, and X. You label sources wasteful or
worthwhile. The system counts minutes, gates only the wasteful ones through
POP's deliberate queue, checks whether captured material was ever processed in
Obsidian, and reports it on a local dashboard.

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
(`tests/test_drbr_migration.py`, `tests/test_drbr_e2e.py`).

**Back up before any upgrade:**

```bash
sqlite3 data/pop.db ".backup data/pop-$(date +%Y%m%d).db"
```

## Environment

All configuration is in `.env`. `.env.example` lists every key with placeholders.

| Key | Meaning |
|---|---|
| `SPOTIFY_*`, `TELEGRAM_*` | POP credentials, unchanged (see below) |
| `WEEKLY_ALLOWANCE_MIN` | Pooled **wasteful** minutes per week across every source (360) |
| `POP_TZ` | The app's timezone (`Europe/Warsaw`). Week = Sunday 18:00 → Sunday 18:00 |
| `LOCK_HOURS`, `LIFESPAN_CYCLES` | POP's lock and queue lifespan |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key, used only for `videos.list` (durations, channels) |
| `DRBR_VAULT_PATH` | Absolute path to the Obsidian vault. Never committed |
| `DRBR_VAULT_INBOX` | Folder whose notes count as unprocessed (`01-Inbox`, including subfolders) |
| `DRBR_CAPTURE_OUTBOX` | JSONL file obsidian-ingest appends captures to (`data/vault_captures.jsonl`) |
| `DRBR_RAW_DIR` | Raw evidence kept for re-parsing (`data/raw`) |
| `DRBR_GO_LIVE` | First local date that counts (a Sunday). Earlier history is evidence only |
| `DRBR_DIAGNOSTIC_WEEKS` | Length of the diagnostic (8) |
| `DRBR_DASHBOARD_PORT` | Dashboard and extension ingest port on 127.0.0.1 (8750) |
| `DRBR_INGEST_TOKEN` | Shared secret the extension sends. `python -c "import secrets;print(secrets.token_urlsafe(32))"` |

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
raw-evidence locations are writable, and that Apple Vision is importable.
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
| `com.jacob.pop.triage` | Sunday 18:00 | Week rollover, debt, expiry, the triage post |
| `com.jacob.drbr.jobs` | daily 09:30 | Outbox read, vault scan, weekly import prompts, label batches, end notice |
| `com.jacob.drbr.dashboard` | always on | `http://127.0.0.1:8750/` plus the extension's ingest API |

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
  Still holds?. A wasteful promotion costs its remaining minutes. A
  worthwhile one costs 0, but still waits out the lock (DR1.3).
- **Imports:** send the Screen Time screenshots and the Cold Turkey JSON
  straight to the bot (below). It replies with what it stored, what it couldn't
  read, or why it failed.
- **Labels:** after the first week, newly seen sources arrive one per message
  with **Wasteful / Worthwhile** buttons. `/labels` asks for the next batch
  now. Unlabelled sources count as wasteful. X is fixed wasteful.

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

Monday or later (the bot reminds you daily until it arrives; Screen Time keeps
about four weeks, so a late capture loses nothing):

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
as evidence but writes no minutes, and the bot tells you what to resend.
Misread app names are kept as read, and each becomes a source you label. On
the dashboard, screenshot figures are marked **parsed screenshot, lower
confidence**.

## Cold Turkey import procedure

Weekly: **Cold Turkey → Statistics → Websites** usage view (time, not
"Websites Blocked") → **Export** → send the JSON file to the bot.

- Minutes are per domain per day.
- Days that haven't finished yet are dropped (the export fills them with zeros).
- Cold Turkey exports only the domains its chart lists, so unlisted sites are
  a stated blind spot, not zero.
- The blocked-count export is rejected.
- One desktop machine is assumed. A second machine that isn't exported is a
  blind spot.

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
  - weekly totals by source, with provenance badges (API / page data / parsed
    screenshot / manual export);
  - wasteful vs worthwhile per week;
  - allowance spent, remaining and debt;
  - the **reflex** figure: wasteful minutes that never passed through the queue;
  - gated vs ungated per group;
  - cost per keeper, ranked;
  - funnels, blind spots, and the latest import per kind with warnings.
- **Labels:** every source with an inline wasteful/worthwhile toggle.
  Changing a label recomputes carried debt immediately.
- **Records:** the raw measurements, filterable by group, label and date.

No targets, scores or streaks, by design.

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
| Sunday 18:00 | Triage the queue post in Telegram |
| Monday+ | Send Screen Time "Last Week" screenshots and the Cold Turkey export; run the extension's weekly sync |
| When asked | Label new sources (buttons) |
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
  `poll.log`, `triage.log`, `check_env.last`) and run `./drbr status`.

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
sources/youtube.py screentime.py ocr_vision.py coldturkey.py vault.py
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

- **Podcasts:** listening that never went through the queue is invisible (POP
  polls only captured episodes, DR1.11).
- **Spotify accuracy:** scrubbing forward reads as listening, and playback
  above 1x undercounts. Completion is Spotify's `fully_played` flag.
- **YouTube:** rewatches overwrite position and undercount; there is no time of
  day; the bar floor is handled by using the resume position.
- **Cold Turkey:** only the chart's listed domains; one machine.
- **Screen Time:** OCR, lower confidence than the API figures; only full past
  weeks count; YouTube and Spotify app minutes are excluded as double counts;
  Spotify music is out of scope.
- **X:** one label, wasteful, with no sub-categories by design. Value taken from X
  shows up as vault keepers, not minutes.
