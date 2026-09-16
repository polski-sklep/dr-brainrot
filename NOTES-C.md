# NOTES-C — Vault + labels (Phase 2, Agent C)

## Contract proposals (not applied; implemented as close as possible)

1. **`vault_captures` has no `author` column.** §6.4's outbox line carries
   `author`; `read_outbox` validates it (string or null) but cannot store it.
   Proposal: add `author TEXT` in a later migration if reporting ever needs it.
   Nothing in §9 does today, so this is low priority.
2. **`OutboxResult` cannot say "outbox file missing".** A missing file returns
   `OutboxResult(0, [])`, identical to "no new captures". Proposal: add
   `missing: bool = False` so `jobs.py daily` can warn when
   `DRBR_CAPTURE_OUTBOX` points at nothing for days.
3. **Callback prefix in the Phase 1 stubs.** `handlers_labels.py`'s stub
   docstring said `d:label:`; §8 and the build prompt say `d:lbl:`. Implemented
   `d:lbl:` (contract wins). The `handlers_imports.py` stub similarly says
   `d:import:` vs §8's `d:imp:` — Agent B's call.
4. **`scan` fails on a missing vault root** (not stated in §6.4). Otherwise an
   unmounted or misconfigured vault would mark every capture `excluded` and
   silently shrink the denominator. Proposal: write this into §6.4.
5. **`/labels` vs the daily dedupe.** `send_label_batch`'s signature is fixed,
   so `/labels` calls an internal `_send_batch(..., once_per_day=False)`: same
   gates (diagnostic ≥ 7 days, not ended) and same 7-day per-source cooldown,
   but not blocked by today's `prompts_sent` row. It records that row too, so
   the daily job does not send again the same day.

## Assumptions and decisions

### Vault (`sources/vault.py`)
- **Frontmatter block** = first line (after an optional BOM) is `---`, closed
  by the next line that is `---`; trailing whitespace on fence lines ignored;
  `\r\n` tolerated. No opening fence or no closing fence: the whole text is
  body and there is no id. Same rule in obsidian-ingest's `body_sha256`.
- **Shared hash vector:** `tests/fixtures/vault/body_hash_vectors.json` here,
  byte-identical copy at `obsidian-ingest/tests/body_hash_vectors.json`. Each
  repo tests its own implementation against it; each also has a test asserting
  the other copy is byte-identical (skips when the other checkout is absent).
- **Id reader:** only a top-level `drbr_capture_id:` line in the first block.
  Quotes and a trailing ` # comment` tolerated, value lower-cased. A value that
  isn't 32 hex chars is a warning (`malformed_ids` in meta), not tracked.
- **Notes with an id the outbox never logged** are counted (`unlogged_ids`)
  and warned about, not tracked. They are the symptom of an outbox failure.
- **Skipped dirs:** `.obsidian`, `.git`, `.trash` are pruned at any depth.
  A note only in `.trash/` is therefore `excluded`, same as deleted — no
  separate "trashed" state.
- **Duplicates:** `copies` = number of non-trash files carrying the id. If any
  copy is outside the inbox, outcome `kept`, `rel_path` = alphabetically first
  kept copy, `edited` = any kept copy's body differs. Otherwise `unprocessed`
  over all copies. One warning per duplicated id.
- **Inbox match** is path-prefix on whole segments (`01-Inbox` matches
  `01-Inbox/Read/x.md`, not `01-Inbox-old/x.md`); a trailing slash in
  `DRBR_VAULT_INBOX` is ignored. A missing inbox folder is a warning (partial).
- **Non-UTF-8 note:** decoded with replacement characters plus a warning;
  unreadable files are warnings. The scan never aborts on one bad file.
- **Import row:** kind `vault_scan`, `raw_path` NULL, periods NULL (so the
  diagnostic start is unaffected), meta = `files_scanned, captures,
  unprocessed, kept, excluded, edited, duplicate_ids, unlogged_ids,
  malformed_ids`. Warnings make it `partial`.
- **Outbox validation:** `v == 1`; `capture_id` 32 lowercase hex;
  `captured_at` ISO with a timezone (`Z` accepted, stored via `clock.iso`);
  `capture_type`, `rel_path` non-empty strings; `rel_path` relative with no
  `..`; `title` string; `url`/`author` string or null; `body_sha256` 64 hex.
  Malformed lines are reported as `line N: reason`, never with content.
  A later line reusing a known `capture_id` with different data is reported
  and ignored (first record stands). Exact repeats are silent.
- **Attribution (explicit data only):** `x-post`/`x-article` → the seeded X
  source via `ensure_source('web_domain','x.com')`. `article` → `web_domain` of
  the URL host (normalised, so `www.` stripped; X hosts would fold into X).
  `youtube` → no source (the outbox line has no channel id). Others → none.
- `latest_outcomes` picks the newest observation by `(scanned_at, id)`, so two
  scans at the same frozen instant still resolve to the later one.

### Labels (`labels.py`)
- All Phase 1 signatures and behaviour unchanged. Note the existing rule that
  an item override short-circuits before the source lookup, so
  `resolve(source_id=<unknown>, item_ref=<overridden>)` returns the override
  rather than raising; pinned by a test.
- `sources_needing_label` evidence = any `measurements` row, any
  `vault_captures.source_id`, or a `listening` row for an episode whose
  normalised show (Spotify) / `source_key` (YouTube) matches. POP's zero-delta
  baseline listening row written at capture counts, so a captured podcast show
  becomes promptable. Episodes with no listening row do not count.
- Cooldown: excluded iff `prompted_at > now − 7 days` (exactly 7 days → asked
  again). Order `first_seen_at, id`. `limit` applied after filtering.
  `settings` is unused (fixed signature).
- `mark_prompted` commits (the message has already gone out).
- `list_sources(label='unlabelled')` filters `label IS NULL`; order
  `kind, display_name (NOCASE), id`; unknown kind/label raise `LabelError`.

### Telegram (`handlers_labels.py`)
- Gate: diagnostic started, not ended, and local today − start ≥ 7 days.
- Dedupe key in `prompts_sent`: `('labels', <measurement-week Sunday>, <today>)`,
  i.e. at most one daily-job batch per local day; row written only when at
  least one prompt went out.
- Each prompt: display name, kind, "seen in" (distinct measurement provenances
  as source names, vault capture count, "your POP queue"), and a reminder that
  unlabelled counts as wasteful. Buttons `Wasteful` / `Worthwhile`. Sources are
  marked prompted one at a time after each send.
- `/labels` collects messages via a sync collector, then awaits
  `context.bot.send_message` for each (`triage.send` can't run inside the
  handler's event loop). Consequence: sources are marked prompted just before
  the real send; if Telegram fails mid-way, those sources wait 7 days.
- Callback: malformed data → "Unrecognised button."; unknown source id →
  "That source no longer exists."; success → answer + edit the message to
  `<name> (<kind>): labelled <label>.` (buttons removed). Pressing again can
  change the label. An edit failure is logged; the label is already stored.

## obsidian-ingest (branch `drbr-capture-id`, worktree `/Users/Jacob/Projects/obsidian-ingest-drbr`)

- `write_note(title, body, dest_folder, frontmatter, capture_type="unknown")`
  now returns `(path, log_error|None)`: computes the final de-duplicated path,
  generates `uuid4().hex`, appends `drbr_capture_id` as the **last**
  frontmatter key, renders the content, appends the outbox line (fsync), and
  only then writes the note. `body_sha256` is computed from the rendered
  content, so it matches what DRBR will hash.
- `commit_plan(plan, input_type=None)` returns `WROTE: <path>` plus, on log
  failure, a second line `DRBR_LOG_FAILED: <reason>`. `ingest` passes its
  `input_type`. `bot.py` reads the first line for the filename and appends a
  warning to the reply if the second line is present. Proposals
  (`action != write`) get no id and no line.
- **capture_type mapping:**

  | classified input | note `format` | `capture_type` | DRBR keeper group |
  |---|---|---|---|
  | tweet (short) | x-post | `x-post` | x |
  | tweet (long-form/article) | x-article | `x-article` | x |
  | youtube | youtube | `youtube` | youtube |
  | article | article | `article` | web |
  | text | — | `text` | other |
  | curriculum (only with a section) | — | `curriculum` | other |
  | screenshot | proposal only | — | — |

- Outbox line: `captured_at` is UTC to the second (`+00:00`); `url`/`author`
  empty strings become `null`; `title` is the plan title before filename
  sanitising; `rel_path` is POSIX, relative to `VAULT_PATH`.
- `DRBR_CAPTURE_OUTBOX` is read from `os.environ` at call time (`~` expanded),
  so the `.env` loaded by `bot.py`/`processor.py` applies. The outbox's parent
  directory is not created: a wrong path fails loudly instead of silently
  logging somewhere nobody reads.
- Pre-existing, not changed: `VAULT_PATH` is read when `vault_write` is
  imported, which in `bot.py` happens *before* `load_dotenv`, so a
  `VAULT_PATH` set only in `.env` is ignored (default `<vault path>` (obsidian-ingest's own default) is
  used). Running `python vault_write.py` still writes demo notes into the real
  vault, and now also appends outbox lines if the env var is set.
- Tests: `tests/test_capture_id.py` + `tests/conftest.py` (autouse: VAULT_PATH
  → tmp, `DRBR_CAPTURE_OUTBOX` unset). Stdlib only. The venv there has no
  pytest; they run with `/Users/Jacob/Projects/drbr/.venv/bin/python -m pytest tests`.
  The reply-formatting change in `bot.py` is untested (importing it needs
  dotenv/telegram and a token).
- Added `.env.example` (placeholders only; there was none).

## Temptations recorded and rejected

- Treating folders other than the inbox differently (e.g. "Archive" = not
  really kept). Anywhere outside the inbox is kept, full stop.
- Using the size of a body edit (word delta) as a strength of engagement, or
  promoting `edited` to a primary signal.
- A distinct "trashed" outcome vs "deleted". Both are excluded.
- Resolving a YouTube capture's channel via the Data API or the transcript
  metadata to attribute it to a source. Only explicit outbox data is used.
- Guessing a source for `text` captures from URLs inside the pasted text.
- Ordering label prompts by minutes used, so heavy sources get asked first.
  Order is first-seen only.
- Suggesting a default button or pre-selecting "wasteful" for sources that
  look like entertainment.
- Matching a vault note to a capture by title/filename when the id is missing.

## Integration to-dos

1. Merge the drbr worktree branch (commit listed in the final report).
2. In `/Users/Jacob/Projects/obsidian-ingest` (the live checkout): merge branch
   `drbr-capture-id`, add to its real `.env`:
   `DRBR_CAPTURE_OUTBOX=/Users/Jacob/Projects/drbr/data/vault_captures.jsonl`,
   make sure `/Users/Jacob/Projects/drbr/data/` exists, then restart
   `com.jacob.obsidian-ingest`. Send one test capture and confirm a line lands
   in the outbox and the note carries `drbr_capture_id`.
3. Remove the worktree afterwards:
   `git -C /Users/Jacob/Projects/obsidian-ingest worktree remove /Users/Jacob/Projects/obsidian-ingest-drbr`.
4. Set `DRBR_VAULT_PATH=<absolute vault path>` in drbr's `.env`
   (default `vault_path` is None, so `scan` fails loudly until it is set).
5. `jobs.py daily`: `vault.read_outbox` then `vault.scan` (in that order, or
   fresh captures show as `unlogged_ids`), then `handlers_labels.send_label_batch`.
   Surface `OutboxResult.malformed` and scan warnings somewhere visible.
6. Decide on contract proposals 1, 2 and 4 above.
7. Article captures create `web_domain` sources, which share identity with
   Cold Turkey domains (correct) but render as "Website (desktop)" in label
   prompts; report grouping for them (`web` vs `desktop_web`) is Agent D's.
8. Optionally add `pytest` to obsidian-ingest's venv so its tests run there.
