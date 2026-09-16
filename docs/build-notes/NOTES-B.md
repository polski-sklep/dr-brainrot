# NOTES-B — Screen Time + Cold Turkey (Phase 2, Agent B)

## Contract-change proposals (none applied to shared files)

Everything below is implemented *inside my own files* as a backward-compatible
extension of CONTRACTS Part II. The integrator should fold these into
CONTRACTS.md, or tell me to back them out.

1. **`ocr_vision.recognize_region(image_bytes, x, y, w, h)`** and a
   `region_ocr=` keyword on `screentime.ingest_batch` / `reparse` (default:
   Vision). Reason: on the real screenshots Vision's full-page pass never
   returned the one-letter app name **"X"** — the one app the brief pre-labels —
   while reading its duration fine. The same row cropped alone reads "X". Without
   this, iPhone X minutes would be a permanent warning. A name is added only when
   both crops in `RECOVERY_CROPS` agree; the measurement's `detail` records
   `name_reread_from_crop: true` and Vision's (low, 0.3) confidence.
2. **`TextObservation.origin: str = "page"`** (`"region"` for crop re-reads), plus
   `to_dict` / `from_dict` for fixtures. Defaulted, so the contract constructor
   still works.
3. **`ScreenTimeParse` gains** `app_rows: list[AppRow]` (name, minutes|None,
   status, raw texts, confidence, screenshots, recovered),
   `daily_average_minutes`, and `meta`. The contract fields are unchanged; `apps`
   lists only readable apps.
4. **`ocr=None` instead of `ocr=recognize`** as the default. Same behaviour, but
   resolved at call time so tests can guard Vision by monkeypatching the module.
5. **Screen Time duplicate detection** uses the manifest sha as specified *and*
   `meta.images_key` = sha256(ordered image shas + local Sunday of the receive
   week). The manifest carries `received_at` (as the contract says), so on its own
   the manifest sha never matches a resend a minute later. "Last Week" only
   changes meaning when the receive week changes, so identical images within the
   same week are `duplicate_of`; in a later week they are new evidence.
6. **Reparse replaces facts by deleting the old import's rows** (`measurements` and
   `category_measurements` with `import_id = old`) in the new import's transaction
   before writing. Plain upsert would leave behind apps the new parse no longer
   finds. Rows already replaced by a *later* import aren't touched. Plain ingests
   only upsert. (A second capture of the same week that lacks the summary must
   not wipe the first capture's categories.)
7. **Evidence-only Screen Time imports finish with `period_start=None`**
   ("This Week", all devices, unknown week). So they neither start the diagnostic
   clock (DR0.6) nor count as covering a week for prompts.
8. **Cold Turkey `item_key`**: `''` for ordinary domains (an aggregate, per
   contract), but the unfolded domain (`x.com`, `twitter.com`) for rows folded into
   the X source. Otherwise x.com and twitter.com on the same day collide on the
   natural key and one silently overwrites the other. **Agent D / report must sum
   measurements per source, not assume one row per (source, day).**
9. **Cold Turkey days on or after the export's local date are not recorded**
   (warning ⇒ `partial`). The real export, made on Wednesday, runs to Saturday
   with zeros for days that hadn't happened. A zero for an unfinished day isn't a
   measurement. `reparse` judges this against the original `received_at` (kept in
   meta).

## What the real Vision output looked like (no personal values)

10 screenshots, 1170×2532, one scrolled per-device "This Week" sequence. I used the
accurate level, with language correction both on and off.

- **Rows are two observations.** The app name is at x≈0.18–0.20. The duration is one
  line lower (dy≈0.026) and its x moves right with the usage bar (0.21–0.71).
  App icons OCR as stray tokens at x<0.15 ("M", "in", "R", "31", a logo word).
  Chevrons and limit hourglasses read as `>`, `8>`, `&>` at x≈0.86–0.89.
- **Section headers** ("Most Used", "Pickups", "First Used After Pickup",
  "Notifications", "Limits", "Screen Time") and "Show More" are at x≈0.08.
  "Show Categories" sits at the right of the Most Used header line.
- **Sticky header** on every scrolled page: device name centred at y≈0.07. The week
  label **"This Week"** is centred at y≈0.15. Page 1 has no week label, only the
  "Week"/"Day" segmented control. Rows under the header blur. Usually they are
  not read at all. Sometimes a blurred name is read with no duration (y 0.09–0.11).
  So nothing above y=0.175 is used for rows.
- **Summary** (page 1): category names in one row, durations directly below at the
  same x. "Total Screen Time" has its value on the same line at the right.
  "Daily Average" has its duration below it. Chart axis labels read as "6h", "avg",
  "0" and weekday letters.
- **Limits** rows put the value on the same line ("15 min >", "1hr >"). They never
  pair as Most Used rows.
- **Counts** (pickups, notifications, first-used) are bare integers, sometimes
  with junk prefixes ("• 31", "022", ": 70"). Small grey notification counts next
  to a red dot were often not read at all.
- **Duration quirks seen:** a leading bullet from the bar ("• 11m"), "Im" for 1m,
  "4S" for 4s, "• 17ml". `parse_duration` handles exactly these. A bare number is
  never a duration.
- **Name quirks seen:** "Linkedln" (language correction off; "LinkedIn" with it
  on), and "lelegram" for a T-initial name in both modes, while a crop of the
  same row reads it correctly. A **one-letter name was never returned** by the
  full-page pass (see proposal 1).
- Vision confidence was 1.0 for almost everything, including misreads, so it is
  recorded but can't tell good readings from bad ones. Crop re-reads report 0.3.
- **The user tapped "Show More"**, so a page can show list rows the previous page
  showed collapsed. Page order doesn't predict the section. A headerless segment is
  therefore classified by its contents: only durations → Most Used; only counts →
  ignored; both → warning; neither → ignored and counted in meta.
- **Language correction: ON** (decision). It changed no duration token, fixed
  "Linkedln", and made the device-name reading consistent across pages. With it
  off, two different device-name readings appeared.
- Parsing the real pages (label edited to Last Week) gives 38 named apps, 3
  categories and a total. The one unnamed row is recovered as X by the crop
  re-read. The local-only test `DRBR_REAL_OCR=1 pytest tests/test_screentime.py -k real`
  checks this against `inputs/`. It prints nothing.

## Screen Time decisions

- **Week:** a label equal to "Last Week" is the Sunday..Saturday before the receive
  week (`received_at` in `settings.tz`). An explicit range header ("Sep 6 – 12",
  "30 Aug – 5 Sep", optional year) is accepted if it is a Sunday..Saturday week that
  ended before the receive week. The year is inferred as the one not after the
  receive date. "This Week", mixed labels across pages, an unreadable range or no
  label → evidence only, with a warning that includes the resend procedure.
  **Unobserved:** the real "Last Week" and date-range wording (I only have This
  Week). Both are handled.
- **Per-device check:** a centred title in the header band is required, and
  "All Devices" anywhere in the header refuses the capture. **Unobserved:** what the
  all-devices screen really says. The `all_devices.json` fixture is a labelled
  synthetic variant. No title at all also refuses, which covers an all-devices
  screen without a device title.
- **received_at** is the Telegram message date. Screenshots taken on Saturday
  night and sent after midnight on Sunday would be read as the wrong "Last Week".
  Photos lose EXIF when compressed, so there is no capture time to use. A named
  limitation, and the prompt tells the user to capture on Monday.
- **Unreadable handling:** a name with no duration on any page, or one app with two
  different durations → `status='unreadable'`, `minutes=NULL` measurement plus a
  warning. A duration with no name → warning only (there's no source to attach).
  A duration at the top edge whose name would sit under the header is counted in
  `meta.edge_rows`, with no warning.
- **Completeness of Most Used:** if the last Most Used segment ends in "Show More"
  → warning to expand it. If a following section header is never seen → warning
  that the end wasn't captured. If Most Used shows categories ("Show Apps") → no app
  rows and a warning.
- **Summary missing** ("Total Screen Time" not seen) → warning, no categories.
  Total minutes, daily average and the sum of app minutes go to import `meta` only.
  No "other apps = total − sum" figure is computed.
- **Parse failure** (`ScreenTimeParseError` → failed import) only when no Screen
  Time marker exists at all (no device title, week label, section header or total).

## Cold Turkey decisions

- **Strict shape:** top-level list; each series has exactly `label` and `data`.
  **Extra keys are rejected**: a new key such as a unit could change what the
  numbers mean, and "only fields observed are assumed". Data points must be
  `[int epoch ms at exactly UTC midnight, number 0..1440]`. Booleans, strings,
  NaN, non-midnight timestamps and empty lists are rejected.
- **Blocked-count export:** a label containing whitespace isn't a domain. If it
  contains "blocked", the message names the chart and says to export Websites
  usage instead.
- **Duplicate (domain, day) rule:** the key is the domain after www/scheme
  normalisation and before X folding. Identical values → kept once, with a
  warning. Different values → one `unreadable` NULL fact, with a warning. Neither
  summed nor picked.
- `file:` (local files opened in the browser) appears in the real export. It is
  stored as a `web_domain` source named `file:`.
- `meta`: `listed_domain_count`, `unlisted_domains` note (unmeasured, not zero),
  `days_in_file`, `days_not_recorded_unfinished`, `filename`, `received_at`,
  `machine` note.

## Telegram

- `register(app)`: `filters.PHOTO | filters.Document.IMAGE` → `on_image`;
  `.json` extension or `application/json` MIME → `on_json_document`. Both are
  wrapped in `bot.authorised_only`. There are no callbacks, so the `d:imp:`
  namespace is reserved but unused. (The Phase 1 stub docstring said `d:import:`;
  the contract says `d:imp:`.)
- **This install has no JobQueue** (`app.job_queue is None`, PTB 22.8 without the
  extra). Batching is an asyncio debounce task: `ImageBatcher(delay, sleep)`, kept in
  `bot_data["imports_batcher"]`.
- **Batching deviates slightly from "per media group"**: batches are keyed per chat,
  and each new image restarts a 4 s quiet window. Telegram caps albums at 10, and
  the real capture is exactly 10 screenshots. A longer capture arrives as two
  albums; keyed per media group, it would become two partial imports of the same
  week. Media group ids are still collected.
- Tests inject `bot_data["screentime_ingest"]` / `["coldturkey_ingest"]`, and an
  event-gated `sleep`.
- **OCR runs synchronously inside the event loop.** Real timing: about 2–3 s for 10
  images, including crop re-reads. That's acceptable for a one-user bot, but it
  blocks other updates meanwhile. Moving it to a thread would need a
  per-thread SQLite connection (`check_same_thread`).
- `send_due_prompts`: the target is the most recent complete Sunday..Saturday.
  Prompts go out once per kind per day, from the Monday after the week. Nothing is
  sent on the Sunday itself, so a week older than the most recent is never
  prompted again. That follows the contract. Screen Time and Cold Turkey count as
  covered when an ok/partial import's period spans the whole week. **YouTube** counts
  as covered when any ok/partial `youtube_history` import was received after the
  week's Saturday, because its period is watch dates, not a week (Agent A should
  confirm). Deduped via `prompts_sent`. Nothing is sent after
  `diagnostic().ended`. Prompts still go out before the diagnostic starts.

## Temptations rejected (the user labels, the system counts)

- Fuzzy-matching a misread Most Used name ("lelegram") against a correctly read
  name in the Notifications list. It would be right here, but it's a guess.
  Misreads become their own source for the user to label.
- Estimating a missing duration from the usage-bar length.
- Recording "total − sum of apps" as unattributed minutes.
- Summing or picking between duplicated Cold Turkey values.
- Treating Screen Time categories as labels (DR1.18); they're stored, never gated.

## Integrator to-dos

- Fold proposals 1–9 into CONTRACTS.md, and add DR entries (language correction
  on, crop re-read, strict Cold Turkey shape, unfinished days, per-chat batching).
- Agent D: sum `measurements` per source (X from Cold Turkey can have two rows per
  day), and treat `status='unreadable'` rows as unmeasured items.
- Agent A: confirm how a `youtube_history` import should count as covering a week
  (my `_covered` uses `received_at`).
- `jobs.py daily` calls `handlers_imports.send_due_prompts(conn, clk, settings, bot)`
  with a `triage.SyncBot`.
- Consider moving OCR off the event loop (see Telegram).
