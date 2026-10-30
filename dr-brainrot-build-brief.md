# Build brief: dr-brainrot (Doctor Brain Rot)

An eight-week diagnostic that measures where attention actually goes across
Spotify, YouTube, iPhone, desktop, and X, classifies it as wasteful or
worthwhile, gates only the wasteful, and reports the results on a desktop
dashboard.

**This is a delta spec against the existing podcast-queue codebase, not a new build.**
The podcast queue is built and working. dr-brainrot absorbs it. Read the queue's source before
designing anything.

Personal tool. Single user. Never productised.

---

## Read this first

**The podcast queue already exists.** Its Telegram bot, SQLite layer, Spotify auth,
`resume_point` poller, capture flow, why-note gate, 48h lock, weekly triage and
allowance accounting are all implemented. dr-brainrot extends them. Phase 1 is
reconnaissance of what is actually there, because this brief was written
without sight of the final queue code and its assumptions may be wrong.

**This is a diagnostic with an end date, not a product.** Eight weeks from
first data, then it stops and the user either names a decision it should change
or shuts it down. Build the countdown in. Do not build for indefinite
operation.

**Measurement is the job. Inference is not.** Several tempting features were
explicitly considered and rejected during design: dwell-time scoring on X,
character-count heuristics, session-length classification, and five-point
value scales. Each was rejected for the same reason, that it produces a
confident number with no ground truth to correct it. If you find yourself
adding a heuristic that guesses at value, stop. The user labels. The system
counts.

**Do not ask the user questions after Phase 0.** Everything needed from them is
front-loaded. Where this brief is silent, choose and document in
`DECISIONS.md`.

---

## What gets measured, and how

Five sources. They differ enormously in how clean the data is. Do not paper
over that in the reporting.

### 1. Spotify podcasts (automatic, exists in the queue)

Already built. `resume_point` polled daily, clamped delta
(`max(0, current - previous)`), `fully_played` for completion. No change except
that the gate becomes conditional on the item's label.

### 2. YouTube (browser extension, weekly)

There is no API route. The Data API has had no watch-history endpoint since
~2016, there is no OAuth scope for history, and Google Takeout records only
that a video was watched and when, with no duration, completion percentage or
resume position.

The route that works: the desktop watch-history page renders a red progress
overlay per partially watched video, sized proportionally, so the watched
fraction is readable as a value from the DOM rather than estimated from pixels.
Multiply by true duration from the Data API (`contentDetails.duration`, ISO
8601) for minutes watched.

**Critical property: YouTube history is account-level, not device-level.**
Videos watched on the phone appear on the desktop history page with their
progress bars. This is the only route by which mobile viewing becomes visible
at all.

Implementation notes:
- Chrome extension, runs in a logged-in session. It cannot run headless on the
  VPS. Posts results to the backend.
- Weekly run. Scroll back until reaching the stored cursor (newest video ID
  from the previous run), then stop. Store the new cursor.
- Date comes from the page's date group headers. Time of day and number of
  sittings are not available. Do not fabricate them.
- Rewatches overwrite position, so a video watched twice in one week
  undercounts. Accepted.
- **Shorts: assume fully watched.** They loop and may render no progress
  element. Fetch true duration from the Data API rather than assuming an
  average — Shorts now run up to three minutes and an assumed 40s would
  understate badly. Never let missing progress elements silently drop Shorts
  from totals.
- Selectors will break. YouTube reshuffles its DOM. Write the extraction so a
  selector failure is loud and logged, never a silent zero.

### 3. iPhone Screen Time (weekly screenshot, vision-parsed)

No programmatic route exists and none will. Apple Developer Technical Support
has confirmed the sandboxing is deliberate: the `DeviceActivityReport`
extension runs read-only and cannot export data, over the network or to the
containing app. App Groups, shared UserDefaults, shared containers, CloudKit
and Darwin notifications all fail.

So: **Monday morning, the bot prompts for a screenshot.** The user taps back
one week in Screen Time (the week runs Sunday to Saturday, so Monday with a
one-week back-step gives a complete seven days with no partial day at either
end) and sends the image. A vision model parses per-app and per-category
minutes into the database.

- Bot re-prompts daily until received. Screen Time retains roughly four weeks,
  so a late capture loses nothing.
- **Capture the per-device iPhone view, not the all-devices total.** Share
  Across Devices is on, so the combined total would include the Mac and
  double-count against Cold Turkey.
- Parse defensively. Store the raw image alongside the parse so a bad parse can
  be re-run later.

### 4. Cold Turkey (weekly JSON export, manual)

Statistics export to JSON from the app's own UI. Everything is stored locally,
nothing is collected by the vendor, and **there is no sync between desktops**.
No API, no CLI, no automated pull.

- Weekly manual export, sent to the bot.
- Per-machine. If the user works across two machines, either both export or the
  gap is recorded as a known blind spot in the report.
- Phase 0 collects a sample export. Write the parser against the real file, not
  against an assumption about its shape.

### 5. Obsidian vault (weekly scan)

Measures whether captured material was ever processed.

- **The existing Telegram bot writes a capture ID into the note's frontmatter
  at creation.** This requires extending that bot.
- dr-brainrot scans the vault weekly, locates each capture ID, and records the folder
  the note currently sits in.
- **Track by capture ID, never by path or filename.** Renames are common and
  happen precisely on the notes that mattered enough to develop. Path-based
  tracking fails hardest on exactly the notes worth following.
- Two outcomes plus an exclusion: still in the inbox is unprocessed, anywhere
  else is kept, and anything the bot logged that no longer exists in the vault
  drops out of the denominator entirely. Deletions are excluded rather than
  counted as failures, because a deleted file is indistinguishable from one
  that never existed and counting it as failure would penalise healthy tidying.
- Secondary flag: was the note edited after capture. Moved-and-edited is
  engagement, moved-and-untouched is housekeeping. Secondary only, never a
  replacement for the folder signal.

---

## Classification

**Binary. Wasteful or worthwhile. No neutral, no five-point scale.** A middle
band would not be applied consistently across eight weeks and inconsistent
labels produce noise that looks like data.

**Labels attach to the source, not the item.** A YouTube channel, a podcast
show, an app. Every item inherits its source's label. Per-item labelling would
be hundreds of decisions a week and that workload is what kills these tools.

**Per-item override at capture.** Some episodes of a given show are
substantial and others are not. The override is the same act as writing the
why-note, so it is one decision, not two.

**Unlabelled sources default to wasteful.** It errs honestly and it creates
pressure to label a source the first time it appears rather than letting an
unlabelled pile accumulate.

**Labels are bootstrapped from data, not seeded by hand up front.** After the
first week of collection, the bot presents newly-seen sources in batches with
inline buttons. No spreadsheet homework before the system has anything to show.

**X is a single app-level label: wasteful.** No sub-categorisation. Screen Time
reports at app granularity and Cold Turkey at domain granularity, so neither
can distinguish research from scrolling. Dwell-time and character-count proxies
were considered and rejected: dwell correlates with outrage rather than value,
and length on X is heavily gamed. The X Articles distinction was also rejected,
on volume (a low-adoption Premium feature) and on device coverage (visible only
on desktop, where almost none of the usage happens). Value extracted from X is
captured into Obsidian and counted there instead.

---

## The gate

**The allowance applies only to items labelled wasteful. Worthwhile content is
unrestricted and uncounted.**

This is the one behaviour-changing mechanism in the system and it is confined
to exactly the behaviour being limited. The queue's existing lock and allocation
ceiling become conditional on the label.

- One pooled weekly allowance across all sources, not per-source. Forces the
  tradeoff between a wasteful podcast and a wasteful YouTube save.
  (Author's decision where the brief was silent. Default 180 minutes, config
  value.)
- Spending it requires the existing queue path: capture rather than play, one
  line saying why, wait out the lock, then choose at triage against everything
  else competing.
- **Impulse consumption bypasses the gate entirely and is measured but
  ungated.** This is intended. The residual, consumption that never passed
  through the queue, is the honest measure of how much is reflex rather than
  choice, and it is probably the most revealing number the system produces.
  Surface it prominently.

---

## Reporting

### Cost per keeper

Minutes spent on a source divided by items from that source that survived to
"kept" in the vault. Expressed as a price: "two hours yielding two keepers is
60 minutes per keeper."

Use this framing rather than items-per-hour. It stays stable at low volume
where a ratio swings wildly, and it makes cross-source comparison obvious.

**Rank sources against each other.** X at 60 minutes per keeper, podcasts at
90, YouTube at 200, tells the user where to cut. This cross-source ranking may
turn out to be the entire point of the diagnostic.

**Set no targets.** The moment there is a number to hit, it gets hit by saving
more, and the metric dies. Trend and comparison only.

### Funnels

- Capture → survived lock → promoted → played (inherited from the queue)
- Capture → kept in vault (excluding vanished notes)
- Gated consumption vs ungated consumption, per source

### Honesty requirements

- Never present a parsed screenshot figure with the same confidence as an API
  figure. Mark data provenance per source in the UI.
- Never convert Obsidian captures into estimated reading minutes. There is no
  resume position for a markdown note. X-derived worthwhile time is a count,
  not minutes. Report it as a count.
- Where a source has a known blind spot (second machine not exported, rewatch
  undercounting), state it in the report rather than silently absorbing it.

---

## Dashboard

Desktop. Local only, bound to `127.0.0.1`, reached over Tailscale. Flask plus
HTMX plus server-rendered HTML. No SPA, no build step, no npm.

Deliberately basic until the concept proves out. Ship these and nothing more:

- **Countdown to the end date.** Days remaining in the eight weeks, on every
  page. This is a diagnostic and the UI should say so.
- **Weekly totals by source**, with provenance marked (API / parsed screenshot
  / manual export).
- **Wasteful vs worthwhile split**, weekly, as a simple stacked bar.
- **Allowance status**: spent, remaining, debt carried.
- **Gated vs ungated**: how much consumption passed through the queue versus
  bypassed it.
- **Cost per keeper table**, one row per source, ranked.
- **Source label manager**: list of known sources, their label, inline toggle.
  This is the one interactive surface that earns its place, because labelling
  is ongoing.
- **Raw record browser**: filter by source, date, label.

### What not to build

No user accounts. No dark mode toggle. No settings page. No mobile layout. No
real-time updates. No export button until something needs exporting. No
gamification, no streaks, no scores. Every hour spent on dashboard polish is an
hour not spent finding out whether the measurement is even true.

---

## Architecture

```
Spotify API ────────┐
Telegram bot ───────┤  (screenshots, Cold Turkey JSON, captures)
Chrome extension ───┼──►  dr-brainrot backend (VPS)  ──►  SQLite
Obsidian vault ─────┘                              │
                                                   └──► Flask dashboard
```

**Open architectural fork, resolved in Phase 0.** The vault scan and the
browser extension both need local access. If the vault is reachable from the
VPS (synced via git, Syncthing or similar), everything runs server-side. If
not, build a small local agent that posts vault state to the VPS weekly. Phase
0 determines which.

Repo layout, extending the queue's:

```
dr-brainrot/
├── pop/                    existing, absorbed
├── sources/
│   ├── spotify.py          exists in the queue, reused
│   ├── youtube.py          extension payload ingestion
│   ├── screentime.py       vision parse of weekly screenshot
│   ├── coldturkey.py       JSON export parser
│   └── vault.py            Obsidian scan by capture ID
├── labels.py               source labels, inheritance, overrides
├── report.py               funnels, cost per keeper
├── dashboard/
│   ├── app.py
│   └── templates/
├── extension/              Chrome extension, unpacked
├── schema.sql
└── data/                   gitignored
```

---

## Constraints

- Extend the existing bot rather than building a second one. Two capture paths
  means one gets abandoned, and it will be the newer one.
- Secrets in `.env`, gitignored. Never commit the vault path or its contents.
- Vision parsing runs on a personal screenshot containing app names and usage.
  Keep it local to the user's own infrastructure. Do not send it anywhere it
  does not need to go.
- Cost: vision parse is one image per week. Trivial. If the bill is noticeable,
  something is wrong.
