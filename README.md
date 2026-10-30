# Dr. Brainrot

An eight-week diagnostic of **brain-rot content**. It measures how much content
you take in across podcasts, YouTube, iPhone apps, the phone browser, desktop
websites, desktop apps, X and audiobooks, and how much of that is brain rot.
It shows the result on a local dashboard.

It is not a screen-time or productivity tracker. Shopping, email, banking,
booking and other errands are out of scope. You label every source:

- **wasteful**: content, and brain rot;
- **worthwhile**: content, and not brain rot;
- **tool**: not content at all. It is shown, never counted.

> You label. The system counts.

It started as a **deliberate podcast queue**, which it still contains. You
capture an episode instead of playing it, write why you want it, wait out a
48-hour lock, then choose what to hear at a weekly triage against a time
allowance.

It is a personal tool with a single user, running on one Mac, and it will
never be a product. **It has an end date.** After eight weeks it stops, and
you either name the decision it should change or shut it down.

---

## What it measures

| Source | How it is read | Cadence | Your effort |
|---|---|---|---|
| Spotify podcasts | Spotify Web API playback positions for captured episodes | daily 09:17 | capture in Telegram |
| YouTube (all devices) | The watch-history page's own data via a browser extension, with durations from the YouTube Data API | weekly | one click |
| iPhone apps | Screen Time "Last Week" screenshots, read on the Mac with Apple Vision OCR | weekly | send screenshots |
| Phone browser, by site | Brave Sync's phone visits in this Mac's Brave history. The minutes come from Screen Time and are split across sites | weekly, with Screen Time | none |
| Desktop websites | Cold Turkey's local statistics database, reduced to hostnames | daily | none |
| Desktop apps | Qbserve's local database, per app per day (browsers skipped) | daily | none |
| X | Desktop x.com through Cold Turkey and the iPhone app through Screen Time, combined into one source that is always wasteful | as above | none |
| Audiobooks (optional) | Audible's own daily listening totals (unofficial client). Always worthwhile unless you relabel a book | daily | sign in once |
| Obsidian vault | Whether captured notes were ever processed out of the inbox. These are counts, never minutes | daily | none |

Every figure keeps its **provenance**: API, page data, parsed screenshot,
browser sync, or manual export. A value that couldn't be read is stored as
**unmeasured**, never as zero. All raw evidence is kept, so any import can be
re-parsed after a parser fix without resending anything.

## What you see

**Dashboard**: `http://127.0.0.1:8750/`. It is served on this Mac only and
shows the countdown to the end date on every page.

- **Overview**:
  - **Summary.** Three to five plain sentences reading the last complete week.
    Claude writes them once a week from aggregates only (see
    [Weekly summary](#weekly-summary)).
  - **The brain.** A drawing beside the summary that grows mould as the week's
    brain rot nears your own peak week. Past 85% of the peak, mushrooms appear.
    The peak is compared like for like, so a week where a sensor was added
    doesn't become the worst week forever.
  - **Against the week before**: *Better than the week before* and *Try next
    week*. These are computed only from what both weeks measured the same way,
    and anything set aside is named.
  - **This week so far.** A part-week pace against the same weekdays last week,
    using only the sources that report every day.
  - **Where the week went**, week by week, as wasteful / worthwhile /
    undecided. Tool minutes get their own column.
  - **Readings to settle** (OCR values it couldn't read for sure), what isn't
    counted yet, and blind spots.
  - **Sync now**: runs every automatic read immediately instead of waiting for
    09:30.
- **Labels**: every source with wasteful / worthwhile / tool buttons. The
  biggest undecided source comes first, and bulk labelling is supported.
  Changing a label re-records each week's figures immediately.
- **Records**: the detail. Weekly totals by source with provenance, imports per
  week, allowance and weeks over it, **reflex** (wasteful minutes you opened
  without queueing first), queued vs opened straight away, **cost per
  keeper** (minutes spent for each vault note you kept), and the queue and
  vault funnels.

There are no scores or streaks.

**Telegram** (one bot, which answers only your user ID):

- **Capture**: send a Spotify episode or YouTube link. The bot shows the
  duration and asks why, and nothing is stored until you reply. End the note
  with `by friday` or `before 3 oct` to set a deadline. Put `#worthwhile` /
  `#wasteful` anywhere in the note to override the label for that one item.
- **Triage**, Sunday 18:00: each unlocked item comes with **Promote / Drop /
  Still holds?** buttons. A wasteful promotion costs its remaining minutes
  against the allowance. A worthwhile one costs nothing but still waits out
  the lock. An empty queue posts nothing.
- **Imports**: send the Screen Time screenshots, or a Cold Turkey JSON export
  if its automatic read ever fails. The reply says what was stored and what
  couldn't be read.
- **Sunday check-in**, 09:30: one numbered message listing what the finished
  week still needs (screenshots, YouTube sync, readings to settle, labels).
  One reminder follows on Monday if anything is missing. When everything is
  in, nothing is sent.
- **Label nudge**: Monday only, and only when enough minutes are undecided to
  matter. Labelling itself happens on the dashboard, never in the chat.
- **Allowance alerts**: at most one of each kind per week.
- Commands: `/start`, `/queue`, `/stats`, `/labels`.

## How it counts

- **Labels decide everything.** The question for each source: is this content
  I consume? If yes, is it brain rot? If you only go there to get something
  done (buy, pay, book, organise, check the weather), it's a tool.
- **Tool is scope, not a middle score.** Tool minutes never count, never
  charge the allowance and never trigger alerts. They are still printed beside
  the content figures, so a label can't hide consumption. Relabel when the use
  changes: a shop is a tool for errands and wasteful for browsing.
- **Unlabelled is undecided.** The headline wasteful figure counts only what
  you've called wasteful. Undecided minutes are shown beside it. The allowance
  and the queue gate still treat undecided as wasteful until you decide.
- **Allowance**: a pooled 360 minutes per week of wasteful plus undecided
  content, across every source. The week runs Sunday 18:00 to Sunday 18:00.
  Going over is recorded, not carried into the next week.
- **Double counts are excluded, not deleted.** YouTube's own history covers
  every device, so Screen Time's YouTube app is dropped. Spotify polling
  covers the Spotify app. X is measured twice and counted once.
- **Comparisons are like for like.** Week-on-week figures, the brain and the
  part-week pace use only channels both spans measured. A week with a new
  sensor can't look like a relapse.

## Weekly routine

| When | Do |
|---|---|
| Any time | Capture instead of play: send links to the bot with a why |
| Any time | Capture reading material via obsidian-ingest as usual; move notes out of `01-Inbox` when they earn it |
| Sunday morning | The check-in: Screen Time screenshots, the YouTube sync, readings to settle, then label new sources on the dashboard |
| Sunday 18:00 | Triage the queue post, when there is one |
| Week 8 | Name the decision, or shut it down (`./dr-brainrot stop`) |

Everything else is automatic.

---

## Setup

### Prerequisites

- macOS. Apple Vision reads the screenshots on-device, and launchd runs
  everything. There is no server: the vault, the browsers and the database
  all live on this Mac.
- Python 3.12 (`brew install python@3.12`).
- Chrome or Brave, signed in to YouTube, for the history extension.
- Brave on the phone and the Mac with **History** sync on (for the phone
  browser split).
- Cold Turkey Blocker and Qbserve on the Mac.
- The Obsidian vault and the `obsidian-ingest` bot (`~/Projects/obsidian-ingest`).
- A Telegram bot token, a Spotify app and a YouTube Data API key.

### Install

```bash
cd ~/Projects/dr-brainrot
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # then fill it in
```

### Environment

All configuration is in `.env`, which is gitignored.

| Key | Meaning |
|---|---|
| `SPOTIFY_*`, `TELEGRAM_*` | Credentials (see below). Only `TELEGRAM_USER_ID` is ever answered |
| `WEEKLY_ALLOWANCE_MIN` | Pooled wasteful-content minutes per week (360) |
| `DR_BRAINROT_TZ` | App timezone (`Europe/Warsaw`). Must match the system timezone, or triage closes the wrong week |
| `LOCK_HOURS`, `LIFESPAN_CYCLES` | Queue lock (48) and how many triage cycles an item survives |
| `DR_BRAINROT_DB_PATH` | SQLite database (`data/dr-brainrot.db`) |
| `YOUTUBE_API_KEY` | YouTube Data API v3, used only for durations and channels |
| `DR_BRAINROT_VAULT_PATH`, `DR_BRAINROT_VAULT_INBOX` | The vault, and the folder whose notes count as unprocessed (`01-Inbox`) |
| `DR_BRAINROT_CAPTURE_OUTBOX` | JSONL file obsidian-ingest appends captures to |
| `DR_BRAINROT_RAW_DIR` | Raw evidence kept for re-parsing (`data/raw`) |
| `DR_BRAINROT_COLDTURKEY_DB` | Cold Turkey's statistics database. Empty means manual exports |
| `DR_BRAINROT_QBSERVE_DB` | Qbserve's `UserDatabase.sqlite`. Empty means desktop apps aren't measured |
| `DR_BRAINROT_BRAVE_HISTORY_DB` | This Mac's Brave `History` file. Empty means the phone browser stays one unsplit block. *Not yet in `.env.example`* |
| `DR_BRAINROT_AUDIBLE_AUTH` | Optional. The sign-in `audible_login.py` saves (`data/audible_auth.json`). Empty or missing means audiobooks aren't measured |
| `DR_BRAINROT_GO_LIVE` | First counted date, a Sunday (`2026-09-13`). Earlier history is evidence only |
| `DR_BRAINROT_DIAGNOSTIC_WEEKS` | Length of the diagnostic (8) |
| `DR_BRAINROT_DASHBOARD_PORT` | Dashboard and extension ingest port on 127.0.0.1 (8750) |
| `DR_BRAINROT_INGEST_TOKEN` | Shared secret for the extension: `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `ANTHROPIC_API_KEY` | Optional. Used only for the weekly summary. Empty means dr-brainrot's own computed sentences |

### Credentials

- **Spotify**: create an app at developer.spotify.com with the redirect URI
  `http://127.0.0.1:8888/callback`. Then run `.venv/bin/python auth_spotify.py`
  once and grant *View your listening position in podcasts*.
- **Telegram**: `/newbot` with @BotFather, then press **Start** in the bot's
  chat once so it can message you first.
- **YouTube**: in console.cloud.google.com, enable **YouTube Data API v3**,
  create an API key, and restrict the key to that API. A sync costs a few
  quota units out of 10,000 a day.
- **Audible** (optional): run `.venv/bin/python audible_login.py` yourself in
  Terminal. Your password and two-step code go only to Amazon. Use `--again`
  if Audible ever refuses the saved sign-in. To sign out, remove the device
  from your Amazon account and delete `data/audible_auth.json` and
  `data/audible_store.json`.

Then check everything:

```bash
.venv/bin/python check_env.py          # add --ping for a Telegram test message
```

It checks key shapes, `.env` permissions, `.gitignore` coverage, live
Telegram, Spotify and YouTube auth, the vault paths, writable data locations,
Apple Vision, and the Cold Turkey and Qbserve databases. Exit codes: 0 passed,
1 failed, 3 unproven (a provider was unreachable).

### Browser extension (YouTube)

Load `extension/` unpacked (`chrome://extensions` or `brave://extensions`,
then **Developer mode**, then **Load unpacked**). In **Backend settings**, set
`http://127.0.0.1:8750` and the ingest token. Each week, click **Run weekly
sync**. It scrolls your watch history back to the last sync and posts the
page's data to the backend. If YouTube changes its page structure, the sync
fails loudly instead of recording zeros. Details are in
[`extension/README.md`](extension/README.md).

### Screen Time screenshots

Sunday morning, as part of the check-in:

1. **Settings → Screen Time → See All App & Website Activity.** Choose this
   **iPhone**, not all devices.
2. Choose **Week** and go **back one week**. The header must read **Last Week**.
3. Screenshot the summary and the whole **Most Used** list, tapping **Show
   More** until it ends. Overlapping screenshots are fine.
4. Send them all to the bot in one go.

The OCR runs on this Mac, and nothing is uploaded. A "This Week" or
all-devices capture is kept but writes no minutes, and the bot tells you what
to resend. Screen Time keeps about four weeks, so a late capture loses nothing.

## Start everything

```bash
./dr-brainrot start      # validate .env, migrate, install and load every agent, wait for the dashboard
./dr-brainrot status
./dr-brainrot restart
./dr-brainrot stop       # obsidian-ingest is left alone
```

| launchd agent | When | What |
|---|---|---|
| `com.jacob.dr-brainrot.bot` | always on | Telegram: capture, triage, imports |
| `com.jacob.dr-brainrot.dashboard` | always on | Dashboard and extension ingest API |
| `com.jacob.dr-brainrot.poll` | daily 09:17 | Spotify positions, lock lifts, deadlines, allowance alerts |
| `com.jacob.dr-brainrot.jobs` | daily 09:30 | All automatic reads, then the weekly summary, Sunday check-in, Monday reminder, Qbserve warning and end notice |
| `com.jacob.dr-brainrot.nightly` | daily 00:15 | Automatic reads only (outbox, vault, Cold Turkey, Qbserve, phone browser, Audible), so each finished day appears just after midnight. Sends nothing |
| `com.jacob.dr-brainrot.triage` | Sunday 18:00 | Week rollover, expiry, the triage post |
| `com.jacob.dr-brainrot.qbserve` | every 10 min | Reopens Qbserve in the background if it isn't running |

launchd is used instead of cron because launchd catches up on runs missed
while the Mac slept. Every scheduled run reads and records data first and
sends Telegram messages after. A Telegram outage never stops a read. The bot
token is scrubbed from every log line.

**After pulling or merging new code, run `./dr-brainrot restart`.** The bot and
dashboard keep the code they started with, but the scheduled jobs run whatever
is in the working tree. Old code meeting new kinds of rows fails. Every
process also applies pending migrations at start, so the 09:30 job will
migrate the live database on its own. To restart just the two always-on
agents and confirm they started after the merge:

```bash
launchctl kickstart -k gui/$(id -u)/com.jacob.dr-brainrot.dashboard
launchctl kickstart -k gui/$(id -u)/com.jacob.dr-brainrot.bot
ps -o lstart= -p "$(pgrep -f Projects/dr-brainrot/bot.py)"
ps -o lstart= -p "$(pgrep -f 'dashboard.app')"
```

To quit Qbserve on purpose, stop its keeper first
(`launchctl bootout gui/$(id -u)/com.jacob.dr-brainrot.qbserve`), or it is
reopened within ten minutes.

## Weekly summary

Once per finished week, the 09:30 job asks Claude (`claude-opus-5`) for three
to five sentences describing that week's measured patterns. It gives no
advice and no praise, and every number in it must come from the figures it was
given. Those figures are stored beside it so any sentence can be checked. The
wins and tips on the Overview come from dr-brainrot's own computation, not the
model.

- **What is sent**: aggregates only. Group names, minutes by label per group
  per week, changes, allowance, reflex share, cost per keeper, funnel counts
  and missing-evidence weeks. **No show, channel, app, domain, video, note
  title or path leaves the Mac.** A test fails if anything in the payload is
  not a date, a number or one of dr-brainrot's own constants.
- **When it is out of date**: the paragraph never changes on its own. A chip
  says why it no longer matches the page: a newer week has finished, data
  arrived after it was written, or you relabelled. **Rewrite** regenerates it.
- **Cost**: one call a week, a few cents a month.
- **Off**: empty `ANTHROPIC_API_KEY`. Nothing is sent, and the panel shows
  computed sentences with the reason beside them.

## Privacy

- Everything runs and is stored on this Mac. The dashboard binds 127.0.0.1 and
  rejects cross-site posts.
- Cold Turkey and Brave history store full URLs. Both are reduced to hostnames
  in memory while reading, and nothing finer is stored, logged or kept as
  evidence.
- Qbserve: apps and minutes only. Window titles, and Qbserve's own ratings,
  are never read.
- Every source database (Cold Turkey, Qbserve, Brave) is opened read-only.
  Brave's is read through a copy.
- Vault: notes are tracked by a capture ID that obsidian-ingest writes into
  their frontmatter, never by path or title. Notes from before go-live aren't
  tracked.
- The only things that leave the Mac are the provider API calls themselves
  (Spotify, YouTube, Telegram, Audible) and the optional aggregate-only
  summary call.

## Data, backup and recovery

The database is `data/dr-brainrot.db` (SQLite). `schema.sql` is the original
queue baseline, and the numbered files in `migrations/` bring it forward, one
transaction per step. Back up the following:

- `data/dr-brainrot.db`, using `sqlite3 data/dr-brainrot.db ".backup data/backup-$(date +%Y%m%d).db"`
  (not `cp`) while agents run;
- `data/raw/` (all original evidence);
- `data/vault_captures.jsonl`;
- `.env`.

To restore: `./dr-brainrot stop`, copy the database back, `./dr-brainrot start`.

## Troubleshooting

Every import, failed or not, is recorded with its warnings and original bytes.

```bash
.venv/bin/python reparse.py list              # recent imports, status, warnings
.venv/bin/python reparse.py list screentime   # or coldturkey / youtube_history
.venv/bin/python reparse.py 42                # re-parse import #42 from its stored evidence
```

- **YouTube "SYNC FAILED"**: YouTube changed its page data. Fix
  `sources/youtube.py` against a freshly saved history page, then re-parse.
  The cursor didn't move, so nothing is lost.
- **Screen Time "no minutes written"**: the header wasn't *Last Week*, or the
  view was all devices.
- **Cold Turkey export rejected**: you exported the blocked-count chart. Export
  the Websites usage view.
- **Vault scan failed**: the vault or inbox path is wrong, including letter
  case. The scan refuses rather than count every capture as kept.
- **obsidian-ingest says "dr-brainrot capture log failed"**:
  `DR_BRAINROT_CAPTURE_OUTBOX` isn't set in obsidian-ingest's `.env`, or
  `data/` doesn't exist.
- **Anything else**: check `logs/` (`bot.log`, `jobs.log`, `dashboard.log`,
  `poll.log`, `triage.log`, `qbserve.log`) and run `./dr-brainrot status`.

## Known distortions and blind spots

These are accepted deliberately and stated on the dashboard.

- **Podcasts**: only captured episodes are polled, so listening that bypassed
  the queue is invisible. Scrubbing forward reads as listening, and playback
  above 1x undercounts.
- **YouTube**: a rewatch overwrites the saved position and undercounts. Dates
  come from day headings, with no time of day. Shorts are assumed fully
  watched.
- **Screen Time**: OCR is lower confidence than the APIs, and only full past
  weeks count.
- **Phone browser**: synced visits carry no duration, so the split across
  sites is an estimate from gaps between visits.
- **Cold Turkey and Qbserve**: this Mac only. Qbserve records only while it
  runs, and days it missed show as not received. It warns only when Cold
  Turkey shows the Mac was in use.
- **Audible**: the day's total is Audible's own, and the split across books is
  estimated. The client is unofficial and can break, in which case it fails
  loudly.
- **X**: one source, wasteful, with no sub-categories. Value from X shows up
  as vault keepers, not minutes.
- **Scope**: a source mislabelled as a tool would hide content, which is why
  tool minutes are always on the page.

## Tests

```bash
.venv/bin/pytest -q
```

No test touches the network or runs Apple Vision. `conftest.py` blocks both,
and blocks reading the real `.env`. An optional check against real screenshots
runs only with `DR_BRAINROT_REAL_OCR=1`.

## Layout

```
bot.py poll.py triage.py spotify.py      the podcast queue, label-aware
db.py clock.py config.py schema.sql      storage, week boundaries, settings
migrations/                              numbered schema migrations
dr_brainrot_types.py evidence.py         shared types, raw-evidence store
labels.py allowance.py report.py         labels, the pooled allowance, reporting
progress.py pace.py insights.py          week-on-week, part-week pace, weekly summary
checklist.py                             Sunday check-in and Monday reminder
handlers_imports.py handlers_labels.py   Telegram imports and label nudge
sources/                                 youtube, screentime + ocr_vision, brave_history,
                                         coldturkey(_local), qbserve, audible, vault
ingest_api.py dashboard/ extension/      local API, Flask + HTMX dashboard, MV3 extension
jobs.py reparse.py check_env.py          daily and nightly reads, re-parse CLI, env check
auth_spotify.py audible_login.py         one-time sign-ins
dr-brainrot launchd/                     launcher and agent definitions
tests/                                   unit, migration, integration, end-to-end
```

## Further reading

| Document | What it is |
|---|---|
| [`dr-brainrot-build-brief.md`](dr-brainrot-build-brief.md) | The original product brief |
| [`DECISIONS.md`](DECISIONS.md) | Every design choice and the reasoning behind it (the queue's `D*`, dr-brainrot's `DR*`) |
| [`CONTRACTS.md`](CONTRACTS.md) | Module interfaces |
| [`REVIEW.md`](REVIEW.md) | Independent audits |
| [`EXISTING.md`](EXISTING.md) | The podcast queue as it was before dr-brainrot |
| [`docs/`](docs/) | Dashboard design notes and build notes |
