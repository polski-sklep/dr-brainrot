# DECISIONS

Choices made where the brief was silent, with reasoning.

Phases 1–5 append to this file. Everything below is from Phase 0 / 0.5.

---

## Phase 0 — credentials and scaffolding

### D0.1 — Redirect URI is `http://127.0.0.1:8888/callback`, not `localhost`

The brief says "one-time browser consent" but does not name a redirect URI.
Spotify no longer accepts `http://localhost:...` on newly created apps; plain
HTTP is permitted only for explicit loopback IP literals. `127.0.0.1` is
therefore the only form that both works and avoids standing up TLS for a
one-time local callback. Port 8888 was free.

### D0.2 — `auth_spotify.py` and `check_env.py` are stdlib-only and self-contained

They duplicate a small `.env` parser rather than importing a shared config
module. Two reasons: Phase 0 must run before any virtualenv or dependency
install exists, and a shared helper written in Phase 0 would pre-empt the
Phase 1 contracts and collide with the module ownership boundaries in Phase 2.
The duplication is ~15 lines and is deliberately not refactored away.

### D0.3 — `write_env_value` replaces the key's line rather than blindly appending

The prompt said "appends `SPOTIFY_REFRESH_TOKEN` to `.env`". The seeded `.env`
already contains an empty `SPOTIFY_REFRESH_TOKEN=` placeholder, so a blind
append would produce two definitions of the same key and the resolved value
would depend on parser order. Replace-or-append is the same behaviour for the
append case and correct for the placeholder case.

### D0.4 — `auth_spotify.py` hard-fails when the required scope is not granted

Not requested. Added because it is the one failure mode in this system that is
invisible in production: a token minted without `user-read-playback-position`
authenticates fine, and every `resume_point` reads `null`. The measurement
layer would then report that the user listens to nothing, which is
indistinguishable from a real result and would be interpreted per the brief's
own note as "the system is being bypassed". Failing loudly at consent time is
the only cheap place to catch it. Same check is repeated in `check_env.py`.

### D0.5 — `check_env.py` validates shape as well as presence

The prompt required presence plus live authentication. Shape regexes were added
because a value can be present, wrong, and still produce a confusing downstream
failure — a bot token pasted into an `api_hash`-shaped field being the classic
case. Cheap, and it localises the error.

### D0.6 — `--ping` is opt-in, not default

`check_env.py` could confirm the bot can actually reach the user by sending a
message. It is behind a flag and off by default: the prompt asked for
authentication checks, not for sending messages, and a credential validator
that emits Telegram traffic on every run is a surprising side effect. The
underlying concern is real — a bot cannot initiate a conversation until the
user presses Start — and that was handled interactively instead.

### D0.7 — `TELEGRAM_BOT_USERNAME` added to `.env`

Not in the brief's config list. Stored because it is needed for README setup
instructions, and because `check_env.py` can then assert that the token
actually belongs to the bot the docs name. Non-secret, no per-item friction.

### D0.8 — Live API calls made during Phase 0

The brief's rule is no live Spotify or Telegram calls in development or
testing. Phase 0 made exactly three, all of them credential validation
explicitly required by the build prompt, none of them in a code path the build
will exercise:

1. Spotify authorisation-code exchange (`auth_spotify.py`) — the sanctioned
   one-time consent.
2. Spotify refresh-token exchange (`check_env.py`) — required proof the
   refresh token authenticates.
3. Telegram `getMe` (`check_env.py`) — required proof the bot token
   authenticates.

`getUpdates` was *not* called: the user supplied their numeric ID directly.
Phases 1–5 make no live calls at all.

### D0.9 — Local Python is 3.14.5, brief specifies 3.12

Left alone. Nothing in Phase 0 uses 3.13+ syntax and the target is the Hetzner
VPS, not this Mac. Phases 1–5 should stay inside the 3.12 feature set so the
VPS runtime is not accidentally raised.

---

## Phase 0.5 — scheduling the unattended build

### D0.10 — `/schedule` could not be used; a local launchd job is the substitute

**This is a deviation from an explicit instruction and needs your sign-off.**

The instruction was to use `/schedule`. `/schedule` creates *cloud* routines:
each run is an isolated sandbox in Anthropic's infrastructure with its own git
checkout. It cannot satisfy the requirement as written, for two independent
reasons:

- It has no access to the local filesystem, so it cannot "re-read
  `pop-build-brief.md` and this prompt from the project directory at run time".
  Inlining both documents into the routine prompt would work around the read,
  but not the write.
- `/Users/Jacob/Projects/POP` is not a git repository, so there is no source
  for the sandbox to clone and no mechanism to return the deliverables. The
  build would complete in a cloud sandbox and be unrecoverable.

`CronCreate` (the local scheduler available in-session) was also rejected: its
jobs are session-only and in-memory, and fire only while the REPL is idle. A
job three days out would not survive to Saturday.

The chosen mechanism is a launchd user agent (`com.jacob.pop.build.plist`)
running `run_build.sh`, which invokes `claude -p` in the project directory. It
meets the actual requirement: fires Saturday 09:00 local, on this machine, with
the project directory present and readable, and deliverables land where they
belong.

### D0.11 — The build prompt was persisted to `BUILD-PROMPT.md`

The scheduled run starts with no memory of the originating conversation, and
the instruction requires it to re-read "this prompt" at run time. The prompt
only existed in conversation, so it was written to disk verbatim in substance,
with a status header recording that Phase 0 is complete and must not be redone.

### D0.12 — `run_build.sh` re-runs `check_env.py` as a precondition and aborts on failure

Directly from the instruction that credentials working is a hard precondition.
The check is cheap and the failure mode it guards against — a build that
consumes the whole window on expired credentials — is expensive.

### D0.13 — The job is one-shot by construction

`StartCalendarInterval` in launchd is inherently recurring (every Saturday).
Since this is a build, not a routine, `run_build.sh` writes a `.build-complete`
sentinel on success, refuses to run if that sentinel exists, and calls
`launchctl bootout` on itself at the end. Belt and braces, because a build
agent re-running unattended over finished work is a bad outcome.

### D0.14 — `--permission-mode bypassPermissions` — armed by the user, 2026-08-26

An unattended agent must not stall on a permission prompt at 09:00 on a
Saturday with nobody at the keyboard, so the runner uses
`--permission-mode bypassPermissions`. This grants the build agent unprompted
tool use for the duration of the run.

I wrote the files but did **not** install or arm the launchd job myself. Two of
my attempts to do so were blocked by the permission classifier, correctly: this
is a security decision that belongs to you, not a detail I should install
quietly while you are reading something else.

Resolution: the user reviewed the script and armed the job manually on
2026-08-26 at 11:03 WEST. Verified state — calendar descriptor
`Minute 0 / Hour 9 / Weekday 6`, `watching = 1`, installed plist byte-identical
to the source in the repo, `run_build.sh` mode 0755, no `.build-complete`
sentinel present.

### D0.15 — The scheduled run fired and failed; the build was run interactively instead

For the record, since it changes how the build actually happened.

The launchd job fired on time on 2026-08-29 at 09:00:04 and died four seconds
later. `check_env.py` passed every check. The build agent did not start:

```
--- launching build agent ---
Not logged in · Please run /login
```

launchd spawns a bare session that does not inherit the interactive Claude Code
login, so `claude -p` had no credentials of its own. The script behaved
correctly on failure: it wrote no `.build-complete` sentinel, exited 1, and
booted the job out of launchd. Nothing was built and nothing was corrupted.

A second defect is visible in the same log: it stamps `CEST`, not `WEST`.
launchd resolved a different timezone than the login shell, so the job actually
fired at 08:00 Lisbon, not 09:00. Both defects have the same root cause — the
launchd environment is not the login environment — and both are recorded in
`README.md` as a caveat against re-arming that job without fixing it.

Phases 1–5 were subsequently run interactively on 2026-08-30, at the user's
instruction, in a session that does have auth.

---

## Phase 1 — contracts

### D1.1 — `config.py` and `clock.py` are Phase 1 modules owned by no Phase 2 agent

The brief's file list is `bot.py`, `spotify.py`, `poll.py`, `triage.py`,
`schema.sql`. Three parallel agents all need configuration loading and time
arithmetic. Had each written its own, the build would have ended with three
incompatible notions of "now" and of when a week starts — precisely the
non-composition Phase 1 exists to prevent. Both are contract surface, so both
were written in Phase 1 and declared off-limits to Phase 2.

### D1.2 — All timestamps are ISO-8601 UTC strings

The brief's schema says `TEXT` without specifying a format. Chosen:
`clock.iso()` output, UTC with an explicit `+00:00`. Fixed width, so SQL string
comparison is chronological comparison and no date functions are needed in
queries. Local time is produced only for display.

### D1.3 — Week boundaries are computed in local time, never as 7×24h

Lisbon observes DST. The week containing 2026-10-25 is 169 UTC hours long, not
168. Adding seven days in UTC would silently shift triage to 17:00 or 19:00
local for half the year. `next_week_start()` does the arithmetic in the local
zone and converts back. Verified against the October transition.

### D1.4 — Time is injected everywhere via a `Clock` protocol

Nothing calls `datetime.now()` directly. Phase 4 has to drive a full lifecycle —
capture, 48h lock expiry, week rollover, two triage cycles — against a frozen
clock, and that is impossible if any module reads the wall clock on its own.

### D1.5 — Debt is driven by listened minutes, not promoted minutes

The brief says "minutes over the allowance are deducted from next week's
allowance" without saying which minutes. The allocation gate makes it
impossible for promoted minutes to exceed the allowance in the first place, so
if debt were computed from promotions it would always be zero and the mechanism
would be dead code. The only way to exceed the allowance is to actually listen
past it, which POP explicitly cannot prevent. So debt is
`listened_min - effective_allowance_min`. This also makes the debt mechanism
consistent with the alerts, which fire on listened minutes.

### D1.6 — Debt is clamped at one full allowance

Not in the brief. Without a cap, one badly blown week can zero out the next
several, and the tool becomes a thing that only ever says no — which kills it by
the brief's own "dies from admin burden" logic. Clamping at `allowance_min`
keeps the effective allowance at or above zero, so the correction is "mildly
costly" as intended rather than compounding.

### D1.7 — Promotion cost is remaining minutes, not full duration

An item can be promoted, partly heard, lapse at reset, and be promoted again.
Charging its full duration the second time would overstate the commitment and
make the gate refuse promotions that cost the user nothing like that much.
`promotion_cost_min()` charges `duration - last_known_position`, floored at 0.

### D1.8 — All promoted-but-not-played items lapse, not just wholly unlistened ones

The brief says "unlistened promoted items return to the queue at reset". Taken
literally, a promoted item with one minute of listening would persist forever,
which is exactly the ever-growing playable backlog the rule exists to prevent.
Read as describing the common case rather than a precise filter, so everything
that is not `played` lapses. D1.7 is what makes this fair: a half-heard item
re-promotes at half price.

### D1.9 — `cycles_seen` never resets, including across a promotion

The brief does not say what happens to the two-cycle lifespan when an item is
promoted and then lapses. If promotion reset the counter, an item could
ping-pong between promoted and queued indefinitely and never expire. Lifespan is
therefore total age in the system, not age since last promotion.

### D1.10 — Deadline items are exempt from lifespan expiry until their deadline passes

The brief says a deadline item "surfaces before it expires regardless of where
the weekly cycle sits" but does not say how deadlines interact with the
two-cycle lifespan. Expiring an item the day before the event it was captured
for would defeat the only reason the deadline field exists. After the deadline
passes unpromoted, it expires as a soft delete.

### D1.11 — `played` is terminal but is not a soft delete

The brief's soft-delete rule names "dropped and expired". `played` is also
terminal, but it is the success outcome and must remain visible in stats as the
numerator of funnel stage 3. It is excluded from triage by being terminal, not
by being hidden.

### D1.12 — Out-of-band listening on a non-promoted item is recorded honestly

`polling_targets()` covers `locked`, `queued` and `promoted`, not just the
promoted slate, and `queued → played` and `locked → played` are legal
transitions. Listening to something you never promoted is precisely the
behaviour the brief wants visible. It does not corrupt funnel stage 3, whose
denominator is `promoted_at IS NOT NULL`.

### D1.13 — An `alerts_sent` table was added

Not in the brief's data model. `poll.py` runs daily and the allowance stays
crossed once crossed, so without bookkeeping the bot would send the same
"exceeded" message every day for the rest of the week. Adds no per-item input,
so it does not violate the friction constraint.

### D1.14 — A no-op state transition raises

`set_state(item, current_state)` is illegal rather than a silent success. It
always means a caller has lost track of what it was doing, and the brief's
"never overwrite a terminal state" is easier to guarantee when the machine is
strict about everything.

---

## Phase 2 — parallel implementation

Three agents built `db.py`, `spotify.py` and `bot.py` concurrently against the
Phase 1 contract. Each reported the places the contract was silent or wrong.
Choices below are theirs unless marked otherwise.

### D2.1 — `roll_over_week()` closes the *preceding* week, not the current one

**A defect in the Phase 1 contract, caught by Agent A.** Triage fires *at*
Sunday 18:00, and `clock.week_start_for()` treats exactly 18:00 as opening a new
week. So at the instant triage runs, `current_week()` is a week zero seconds
old. Closing it would have computed every debt against an empty week: debt would
always be 0, the mechanism would be dead code, and every test asserting "no debt
in a normal week" would have passed. `roll_over_week()` closes the most recent
week row that opened before the week containing `now`, and is idempotent across
repeated triage runs. `CONTRACTS.md` §2 was corrected.

### D2.2 — `parse_episode_ref` extracts a link from surrounding text

**Overruled Agent B's initial choice.** It first implemented strict matching:
the whole message had to be exactly a link or URI. That is the literal reading
of "accept both forms", but sharing an episode from Spotify's iOS share sheet
can prepend the title and show name, and the strict parser bounced it. The brief
says this tool "dies from admin burden long before it dies from bad design", and
forcing the user back to re-copy a link they already shared is exactly that.
Extracting a valid link out of prose is not "silently ignoring malformed input"
— every genuinely malformed case still gets its clear rejection message. A bare
22-character id with no URL or URI around it stays rejected: too easy to match
an arbitrary word.

### D2.3 — Missing `resume_point` raises rather than defaulting to zero

The single most important defensive choice in `spotify.py`. An absent
`resume_point` means the token lost the `user-read-playback-position` scope.
Defaulting it to 0 would report that the user listens to nothing — which is
indistinguishable from a real result, and which the brief itself says should be
read as "the system is being bypassed". It raises `SpotifyError` naming the
scope.

### D2.4 — `ms_to_min` ties round down, and the contract prose was corrected

`(ms + 29_999) // 60_000` rounds a 30.000-second remainder to 0, which is ties
*down*, not the "half up" the contract prose claimed. Agent A kept the formula
bit-identical and flagged the contradiction rather than silently changing
behaviour. Cross-module agreement on what a minute is matters more than which
way one boundary case falls, so the prose was fixed instead of the code.

### D2.5 — No database write happens until the why-note arrives, in either table

Agent C deferred `upsert_episode` as well as `capture` to the note-arrival path.
The contract only required that no `queue` row exist. Deferring both means an
abandoned capture leaves no trace anywhere, which is a cleaner reading of the
brief's "the item is not stored until the reply arrives".

### D2.6 — A failed Spotify link mid-conversation is never swallowed as the note

Agent C's addition. Without it, pasting a track link while the bot is waiting
for a why-note would silently record that URL as the user's stated reason. It is
rejected instead, and the pending capture is kept rather than discarded.

### D2.7 — A note that parses down to only a date is refused

"by friday" as the entire why-note leaves nothing behind once the deadline is
extracted. Rather than store an empty note — which no code path permits — or
discard the capture, the bot asks again and keeps the pending item.

### D2.8 — Deadline parsing rolls forward only underspecified forms

Fully specified forms (ISO dates, `today`, `tomorrow`) are taken literally even
if the resulting 18:00 is already past. Only underspecified ones (a bare
weekday, a day and month with no year) roll forward to the next future
occurrence. The alternative silently moves a date the user actually named.

### D2.9 — `promote()` on a terminal item raises `IllegalTransition`, not `AllowanceExceeded`

Legality is checked before budget. A dropped item should not come back with a
message about how many minutes are left this week. Neither path writes anything.

### D2.10 — `promotion_cost_min` reads the most recent position, not the highest ever seen

A backward scrub genuinely means there is more left to hear, so the cost of
re-promoting rises again. Consistent with D1.7.

### D2.11 — `record_listening` recomputes `listened_min` rather than incrementing it

Agent A's choice. Incrementing accumulates per-poll rounding error across a
week; recomputing `SUM(delta_ms)` over the week window cannot drift. The poll is
attributed to the newest week row at or before `polled_at`.

### D2.12 — `/queue` shows the unlocked queue only, not promoted items

Agent C's reading of "the current unlocked queue, read-only". Promoted items are
in the playable slate and live in Spotify at that point, not in the queue.

### D2.13 — Callbacks answer with a toast and do not edit the message

Agent C deliberately does not rewrite message text or keyboards, leaving that
rendering to Phase 3. A consequence worth knowing: the still-holds buttons stay
live after a promote, which is correct, since still-holds is recorded
independently of the promote/drop decision and is the cleanest signal in the
system.

### D2.14 — A 429 from Spotify raises without automatic backoff

The contract only specified the 401 refresh-and-retry. A daily cron can simply
fail and run again tomorrow; a retry loop inside a cron job is a good way to
turn one rate limit into a sustained one.

### D2.15 — Tests make a real socket attempt an immediate failure

Agent B added an autouse fixture monkeypatching httpx's transports to raise. The
build rule was "no live API calls"; this makes a violation fail loudly in
milliseconds rather than hang, which is the failure mode the build prompt
specifically warned about.

---

## Phase 3 — orchestration

### D3.1 — Triage idempotence uses a `meta` key, not a new table

Cron retries happen, and steps 3–4 of `run_triage()` are not naturally
repeatable: a second run in the same week would bump every `cycles_seen` again
and expire a whole generation of items a week early. The completed week is
recorded under `last_triage_week` in the existing generic `meta` k/v table,
written last so a crashed run retries rather than skips. This mirrors how
`alerts_sent` protects `poll.py` and needed no schema change.

### D3.2 — `run_triage()` deliberately does not call Spotify

The contract mandates a `spotify` parameter but never says what for. Positions
are `poll.py`'s job. The Sunday post is the single moment the whole system
exists for, and making it dependent on a Spotify call would let an API outage
silently cancel triage for a week. The parameter is accepted and unused, and
the docstring says why.

### D3.3 — Both `approaching` and `exceeded` can fire from the same poll

If one day's listening crosses 80% and 100% at once, both alerts send, once
each, ever. Suppressing the first would mean writing an `alerts_sent` row for a
message that was never sent, which would then suppress a genuine alert later.

### D3.4 — No `approaching` alert when debt has consumed the whole allowance

When `effective_allowance_min == 0`, "80% of nothing" is zero, so the alert
would fire every single day at zero minutes listened. Only the crossing is
reported in that case.

### D3.5 — Alerts send before `alerts_sent` is written

Deliberate ordering. A Telegram outage leaves the alert unrecorded and
tomorrow's cron retries it. The reverse order would mark an undelivered alert as
sent and lose it permanently — and the alerts are the control loop, which the
brief names as the main reason the interface is a bot.

### D3.6 — Identical consecutive observations write no `listening` row

If an episode's position and `fully_played` are byte-identical to the previous
poll, no row is written. `weeks.listened_min` is recomputed as a sum of deltas
so no number changes; this only stops a locked episode nobody is listening to
accreting a zero-delta row every day for weeks.

### D3.7 — No `parse_mode` on bot messages

Episode titles containing `_` or `*` would break a Markdown parse or silently
swallow part of the title. Plain text is correct here: the messages carry a
why-note and a duration, not formatting.

### D3.8 — The week header is posted last

Contract §5 numbers it step 5 and says the order matters. A header-first message
reads more naturally and this is worth revisiting, but the contract was followed
literally rather than silently reordered.

### D3.9 — Promoting an item on its final cycle does not buy it another cycle

Consequence of D1.9 (`cycles_seen` never resets) plus the rollover-before-expiry
ordering: an item promoted on its last cycle and then not listened to lapses
back to `queued` and is expired in the same triage run.

This looks harsh and is correct. An item that survived two calm readings, was
promoted, and still was not played is the exact definition of funnel stage 3
failing — wanted in the imagination, not in fact. Keeping it alive because it
was promoted would let anything survive indefinitely by being re-promoted, which
is the ever-growing backlog the lifespan rule exists to prevent. Re-capturing it
costs one message.

---

## Phase 4 — verification

Six defects were found and fixed; the full evidence is in `REVIEW.md`. The
choices below are the ones where the fix was not obvious, or where it touched
something an earlier phase had settled.

### D4.1 — The capture path seeds a zero-delta listening baseline

`resume_point` is a position, and `poll._record` reads "no previous
observation" as position 0. Nothing recorded the position an episode was
already at when it was captured, so the first poll of a part-heard episode
credited every pre-capture minute to the current week — feeding the debt
mechanism with listening that may have happened months earlier — and
`promotion_cost_min()` charged the full duration for an episode already half
heard, which is precisely the case D1.7 says should cost half. Measured: a
58-minute episode with 21 minutes already heard cost 58 to promote, and a poll
in which nothing was listened to credited 21 minutes.

`bot._handle_note` now writes one `db.record_listening(..., delta_ms=0)` after
a successful capture when the episode's position is non-zero. The position is
recorded; none of it is credited.

**It is in `bot.py` rather than `db.py` deliberately.** `db.capture()` cannot
know the position without a signature change, and `CONTRACTS.md` forbids
widening a signature without amending the contract first. `bot.py` is the only
capture path in the system, so the invariant holds today. The better long-term
home is `db.capture(..., position_ms)`, which would additionally let `db`
compare the seed against existing listening history — as written, re-capturing
an episode that is already being polled discards at most one day of real
listening. That is a bounded, rare cost against an unbounded, silent one.

### D4.2 — `why_note` gained a CHECK constraint; existing databases would need a migration

`delta_ms >= 0` was backed by a CHECK; the why-note, the other invariant of the
same kind, was only `NOT NULL`, and raw SQL could insert `'   '`. The
constraint is `TRIM(why_note, ' ' || char(9) || char(10) || char(13)) <> ''` —
the explicit character set is required because SQLite's one-argument `TRIM`
strips spaces only and still accepted `"\t\n "`.

`schema.sql` uses `CREATE TABLE IF NOT EXISTS`, so this applies to newly
created databases only. `data/` is empty, so there is nothing to migrate. If a
database is created before this ships, `migrate()` needs a version 2 that
rebuilds the table.

### D4.3 — `promote()` re-checks the lock; the transition table is untouched

The brief says locked items cannot be promoted, and `promote()` enforced only
the transition table and the budget, so it would happily promote an item inside
its 48h. `LEGAL_TRANSITIONS["locked"]` includes `"promoted"`, justified in
`CONTRACTS.md` §1 as the deadline-exemption case — but a deadline-exempt item
is created `queued`, not `locked`, so that justification never applies and the
entry only permitted the thing the brief forbids.

The entry was left in place and the check added to `promote()` instead. The
state machine is contract surface and shared with `set_state`; the lock is a
policy about *how an item may enter the playable slate*, which is what
`promote()` is for. No live path could reach the hole today — nothing posts a
button for a locked item — so this is enforcement of a stated invariant, not a
bug fix for observed behaviour.

### D4.4 — The last-cycle warning asks "would this expire next triage?"

`_should_expire` exempts deadline items from lifespan expiry until the deadline
passes (D1.10). The warning flag did not, so a deadline item two months out was
told at every triage from cycle 2 onward that it would expire at the next one,
while `_should_expire` kept it alive indefinitely. `_will_expire_next_cycle()`
now asks the question the message actually claims to answer, which for a
deadline item is `deadline <= next_week_start(now)` and for everything else is
the unchanged lifespan test. This makes the warning fire, correctly, in the week
a deadline actually falls in — behaviour the previous code never produced.

### D4.5 — The no-live-calls rule moved from one test module to a root conftest

D2.15's autouse fixture lives in `tests/test_spotify.py`, the one module that
could not have made a live call anyway. The four modules that drive `bot.py`,
`poll.py` and `triage.py` had no guard, and nothing stopped a test reading the
real `.env`. A root `conftest.py` now blocks DNS, socket connects and both httpx
transports, and raises on any read of the real `.env`, for every test in the
repository. The suite was already hermetic; it simply had no proof.

`socket.socket` itself is not blocked: asyncio builds a self-pipe with
`socket.socketpair()` on every event loop, and `triage.send()` legitimately
drives a coroutine-returning bot double through `asyncio.run()`. Blocking the
constructor fails 52 tests that never go near a network. The reason is written
into the conftest so nobody tightens it back.

### D4.6 — Funnel stage 3's numerators are restricted to promoted items

`funnel_stats` divided "any item whose episode has listening" by "items that
were promoted", so out-of-band listening on a never-promoted item entered a
numerator whose denominator excludes it and `/stats` could render
`played 200% (2/1)`. The brief defines stage 3 as "promoted → actually played",
so both `played` and `fully_played` are now restricted to
`promoted_at IS NOT NULL`.

This makes D1.12's claim true rather than reversing it: out-of-band listening is
still recorded honestly in `listening` and still charges the week's allowance,
which is the behaviour D1.12 exists to protect. What it no longer does is
inflate the one number the system is built to produce.

### D4.7 — Two problems are reported and left unfixed

Both are in `REVIEW.md` with evidence.

1. **A deadline item is never surfaced between triages.** The brief says a
   deadline item "surfaces before it expires regardless of where the weekly
   cycle sits". The only mechanism implemented is the lock exemption, which
   changes a state the user never sees: an item captured Monday for a Tuesday
   event is `queued` immediately, is never posted, and is soft-deleted at the
   following Sunday's triage for having passed its deadline. Every deadline
   falling between two Sundays behaves this way. The fix is for `poll.py` — the
   only daily process — to post imminent-deadline items with
   `alerts_sent`-style bookkeeping. That is new user-facing behaviour outside
   `CONTRACTS.md` §5, and Phase 4 was told not to add features. **This is the
   largest gap between the brief and the build and it needs a decision.**

2. **`TZ` is read from the process environment.** `config.load()` overlays
   `os.environ` for every key in `_KEYS`, which includes `TZ` — a standard
   POSIX variable meaning the *system* timezone. A cron environment exporting
   `TZ=UTC` would silently move triage to Sunday 18:00 UTC, an hour off for half
   the year, which is exactly what D1.3 was written to prevent, arriving through
   the config layer instead of the arithmetic. The fix is to rename the key to
   `POP_TZ`, which touches `.env` on the VPS and so belongs to its owner.

---

## Phase 5 — post-audit fixes

Both problems D4.7 reported and left unfixed were escalated and decided. The
numbering continues the file's convention: a new phase, entries from `.1`.

### D5.1 — Deadline items are surfaced by the daily poll, because the brief requires it

**This is not a new feature. It is an unimplemented line of the brief.**

`pop-build-brief.md:79-81`: "An item with a deadline surfaces before it expires
regardless of where the weekly cycle sits. It still requires a note and still
counts against the allowance." The word is *surfaces* — the item is shown to
the user. What was built was the lock exemption alone (`db.py:281-291`,
`db.py:316`): a deadline inside the 48h window makes the item `queued` at
capture instead of `locked`. `queued` is a database state. The user never sees
it. The only thing that turns `queued` into a message is the weekly triage
post, so an item captured Monday for a Tuesday event was shown to nobody and
was soft-deleted the following Sunday for having passed its deadline
(`triage.py:242`). Every deadline falling between two Sundays behaved that way,
which is most deadlines, which made the whole field inert.

D4.7 called this "the largest gap between the brief and the build" and declined
to close it on the grounds that a new message path is a feature. That was the
right call for Phase 4, whose brief was verification. It is the wrong call
standing: a requirement of the brief that produces no observable behaviour is a
defect, not a deferred enhancement, and the fix delivers the behaviour the
brief already specified rather than inventing any.

`poll.py` is the only daily process, so delivery goes there
(`poll.py:208-249`).

**Why this does not violate the constraints it looks like it might.**

*Friction* (`pop-build-brief.md:313-316`, "every field added is friction the
user must survive weekly"). `deadline_notices` (`schema.sql:94-105`) is dedup
bookkeeping keyed by `queue_id`, the same idea as `alerts_sent` under D1.13. It
asks the user for nothing. No field was added to `queue`.

*No privileged content category* (`pop-build-brief.md:38-41`, and the deadline
paragraph's own "without creating a privileged content category"). Surfacing is
delivery and nothing else. The item needed a note to exist (`db.py:302-304`);
promotion runs through `db.promote()` and is refused by the allocation gate at
the same threshold as anything else — there is no bypass and none was added;
and being surfaced writes no state at all, so `state`, `cycles_seen`,
`promoted_at` and `resolved_at` are exactly what they were. The funnel numbers
are untouched by this path. What changes is that a decision the user was never
asked to make is now asked.

**Ordering and dedup, both inherited rather than invented.**

`db.lift_expired_locks()` moved into `run_poll` (`poll.py:112`), before
surfacing. A lock is time arithmetic against `captured_at`; lifting it daily is
strictly more accurate than lifting it weekly, and it is *required* here,
because an item captured Monday with a Thursday deadline is `locked` until
Wednesday and would otherwise never be eligible to surface at all. The call
only moves rows whose lock has already expired, so `run_triage` making it again
(`triage.py:297`) changes nothing. `run_triage`'s documented step order is
unchanged.

The send precedes the `deadline_notices` write (`poll.py:248-249`), which is
D3.5 applied to a second message kind: a Telegram outage must leave the notice
unrecorded so tomorrow's poll retries it. For the same reason nothing filters
on `deadline >= now` (`poll.py:228-232`). Filtering would mean an outage on the
deadline day silently consumed the one notice an item ever gets, which is the
exact failure D3.5 exists to prevent.

**The boundary is `<=`, not `<`.** `run_triage` expires on `deadline <= now`
(`triage.py:242`), so an item whose deadline lands exactly on the triage
instant is expired by that triage rather than posted by it. A strict `<` in
`surface_deadlines` would leave that single case reproducing the original bug.
The comparison mirrors `_will_expire_next_cycle` (`triage.py:256`), which was
written to ask this same question under D4.4.

**The renderer is triage's own.** `render_deadline_notice`
(`triage.py:176-195`) calls `render_item` verbatim and prepends one lead line.
Same fields in the same order, and the same keyboard with the same
`f"{action}:{item_id}"` payloads, so `bot.on_callback` needs no knowledge that
this path exists and cannot drift from it. The lead line is the one addition:
a post arriving on a Tuesday carrying triage buttons is otherwise
unexplainable, and an unexplained message is how a personal tool starts getting
ignored. `warning=False`, because the last-cycle warning is a lifespan
statement and a deadline item is lifespan-exempt (D1.10).

### D5.2 — The timezone setting is `POP_TZ`, and `TZ` is refused outright

`TZ` is the POSIX system-timezone variable. `config.load()` overlaid
`os.environ` for every key in `_KEYS`, and `TZ` was one of them, so any cron
entry, systemd unit or shell profile exporting `TZ=UTC` would have moved
`Settings.tz` and with it the Sunday 18:00 boundary — an hour off for half the
year. That boundary defines the week: the allowance window, the debt
calculation, `_should_expire`'s deadline comparison, every `week_start_for`
call. D1.3 was written to keep exactly this drift out of the arithmetic; it
arrived through the config layer instead. The build had already been bitten
once from the other direction, by a launchd job stamped CEST rather than WEST
(`README.md:105-109`).

The setting is now `POP_TZ`, defaulting to `"Europe/Lisbon"`
(`config.py:70-75`).

**Renaming the read was not sufficient on its own.** `TZ` is removed from
`_KEYS` entirely (`config.py:82-89`), so no ambient value under that name can
reach `Settings` by any path — not as an override, and not as a fallback when
`POP_TZ` is absent. `tests/test_config.py` asserts `"TZ" not in config._KEYS`
structurally, so a later edit to that set cannot quietly re-add it.

An existing `.env` still carrying `TZ=` now fails `check_env.py`'s presence
check (`check_env.py:32`) rather than silently working under the POSIX name.
That noise is the point: a config key that stops being read should say so.

`CRON_TZ=Europe/Lisbon` in `README.md`'s crontab block is deliberately
untouched. It controls when the daemon fires the jobs, which is a genuinely
different concern from what POP believes its own timezone to be, and both are
needed. `README.md:111-120` now says so explicitly, because the two being
confusable is how this defect gets reintroduced.

`config.py` had no test module of its own before this, which is how a config
key nobody had reason to look at reached production. It has one now.

### D5.3 — `.build-complete` written by hand

`run_build.sh` writes this sentinel on a successful unattended run and refuses
to start when it exists. The build ran interactively instead (D0.15), so the
sentinel was written manually. Without it, re-arming the launchd job — which is
a one-line command still sitting in `README.md` and the transcript — would set a
build agent loose over finished work with `bypassPermissions`.

### D5.4 — `run_build.sh` and the launchd plist were kept, not deleted

They document how the unattended build was meant to run, and the log in `logs/`
is the evidence for D0.15. Anyone re-arming that job must first fix the two
defects recorded in D0.15: the launchd session has no Claude Code login, and it
resolves a different timezone than the login shell.

### D5.5 — httpx logging silenced because it printed the bot token on every call

Found by starting the bot, not by any test. httpx logs each request at INFO
including the full URL, and the Telegram Bot API puts the token **in the path**:

    INFO:httpx:HTTP Request: POST https://api.telegram.org/bot<TOKEN>/getMe

A long-polling bot calls `getUpdates` every few seconds forever, so at INFO the
token lands in `logs/bot.log` thousands of times a day — a file that gets
tailed, copied to the VPS, and picked up by any log shipper. `logs/` being
gitignored does not help with any of that.

`bot._silence_token_logging()` raises `httpx`, `httpcore` and `telegram.request`
to WARNING, and all three entry points call it. Real failures still surface;
the URLs do not. Verified by 25 seconds of live polling producing an empty log.

No test could have caught this: it is a property of the production logging
configuration, which the suite deliberately never executes.

### D5.6 — Only one bot instance may run at a time

Telegram allows exactly one `getUpdates` poller per token. A second instance
makes both fail with `Conflict: terminated by other getUpdates request`, and
neither serves the user. This surfaced during startup when an orphaned process
survived a pattern-based `pkill` — its command line showed the resolved
interpreter path, not `.venv/bin/python`. Recorded in `README.md`, and it is why
the VPS deployment should use a supervisor that guarantees a single instance
rather than a bare nohup.

### D5.7 — An error handler was added, at the user's request

PTB warned `No error handlers are registered` at startup. Without one, an
unhandled exception in a handler is logged and the update is dropped: from the
user's side the bot simply ignores them, which is **indistinguishable from the
deliberate silence an unauthorised sender gets**. That ambiguity is the actual
problem — the silent-rejection rule is load-bearing, and a crash must not be
able to impersonate it.

`bot.on_error` therefore has three properties, each of which is tested:

1. **The reply is a fixed string, never the exception text.** Exception
   messages can carry request URLs, and Telegram puts the token in the URL
   path. Echoing `context.error` to the chat would undo D5.5.
2. **The logged traceback is scrubbed** of the token via `bot._scrub()`, for
   the same reason: D5.5 closed the happy path, and the error path must not
   reopen it.
3. **Unauthorised users still observe nothing**, even when their update is what
   crashed the handler. The gate holds on the failure path too.

It also cannot raise: a failure inside the error handler would be
unhandleable, so the delivery attempt is wrapped and logged.

### D5.8 — `get_episodes()` falls back to one request per episode on a 403

Found by running `poll.py` in production, not by any test. Probed against the
live API on 2026-08-30 with the real token:

| request | result |
|---|---|
| `GET /episodes/{id}` | 200 |
| `GET /episodes/{id}?market=PT` | 200 |
| `GET /episodes?ids=` | **403** |
| `GET /episodes?ids=&market=PT` | **403** |

Same token, same episode, same minute. `market` is not the cause. This is a
Spotify-side restriction on the several-episodes endpoint that the client
cannot fix by asking differently — and it made every daily poll fail outright,
which would have meant zero measurement.

The brief already budgets for the fix: *"Cost is one API call per queued
episode."* `get_episodes()` now catches `SpotifyForbidden` on the batch call
and falls back to sequential single fetches, latching a flag so a queue of 30
episodes costs one wasted request per process rather than thirty. The fallback
mirrors the batch contract — unavailable ids are skipped, so the result can be
shorter than the request and callers must key by `spotify_id`.

`SpotifyScopeError` was added and deliberately escapes the fallback's per-id
error tolerance. Skipping an episode because it 404s is fine; skipping one
because the token lost `user-read-playback-position` would reintroduce exactly
the silent "you listened to nothing" failure the whole module is built to
prevent.

### D5.9 — launchd, not cron, and the schedule is in Warsaw time

Two corrections, one of them mine.

**cron is wrong on a laptop.** macOS cron does not catch up on missed runs. If
the Mac is asleep at the triage hour on a Sunday, triage never runs that week:
the week never rolls over, debt is never computed, promotions never lapse, and
the cycle stalls silently. launchd runs a missed `StartCalendarInterval` job at
the next wake. Cron remains correct on the always-on Hetzner VPS, and those
lines stay in `README.md` for that deployment.

**This Mac is on Europe/Warsaw, not Europe/Lisbon.** I asserted the opposite in
Phase 0.5, and that error is the real explanation for the `CEST` stamp in the
failed build log (D0.15) — launchd was reporting correctly.

Both schedulers fire in system local time, so triage is scheduled at **19:00
Warsaw = 18:00 Lisbon**. Lisbon and Warsaw share EU DST dates, so that offset is
a constant +1h; verified across 400 days and both switchovers, zero mismatches.
Scheduling `Hour 18` would have fired an hour *before* POP's own week boundary
and closed the wrong week every Sunday.

Unresolved and the user's call: POP is configured `POP_TZ=Europe/Lisbon`
because the brief specifies Sunday 18:00 Lisbon, but the machine is in Warsaw.
If triage should happen at 18:00 *local* instead, set `POP_TZ=Europe/Warsaw`
and change the plist to `Hour 18`.

### D5.10 — The bot runs under launchd with KeepAlive

It was previously running from an interactive shell and would have died with
that session. `com.jacob.pop.bot.plist` restarts it on crash and across
reboots, with `ThrottleInterval` 30 so a crash-loop cannot spin. Telegram
permits exactly one `getUpdates` poller per token, so any manually started
instance must be stopped before bootstrapping this (D5.6).

The stale `com.jacob.pop.build.plist` was also removed from
`~/Library/LaunchAgents`. Agents there load at login, so it would have re-armed
itself and fired again the next Saturday. The `.build-complete` sentinel would
have aborted the run safely, but the job should not exist at all. The source
file stays in the project as the record for D0.15.

### D5.11 — `POP_TZ` changed to Europe/Warsaw at the user's instruction

Supersedes the Lisbon setting in D5.9 and departs from the brief, which
specifies "Sunday 18:00 Europe/Lisbon". The user is in Warsaw; triage at 19:00
local to satisfy a Lisbon clock would be an hour later than the brief's actual
intent, which is early Sunday evening while the week is still being thought
about.

Three things moved together, and all three must stay in step:

1. `.env`: `POP_TZ=Europe/Warsaw`.
2. `com.jacob.pop.triage.plist`: `Hour` 19 → 18. The system timezone and
   `POP_TZ` now agree, so launchd fires exactly on POP's own week boundary
   rather than being offset against it.
3. The stored `weeks` row was **realigned, not deleted**: its key moved from
   `2026-08-23T17:00:00+00:00` (Sunday 18:00 Lisbon) to
   `2026-08-23T16:00:00+00:00` (Sunday 18:00 Warsaw) by `UPDATE`. It was
   all-zero, so no accounting could be misattributed. Left alone it would have
   been an orphaned key matching no boundary the code can now compute — inert,
   but the kind of debris that makes a later bug hard to read.

Side effect worth knowing: the Warsaw boundary for 30 August had already passed
when the switch was made, so the change moved POP into a new week immediately.
Nothing was lost — the outgoing week was empty.

The bot was restarted because `POP_TZ` is read once at process start; the
running instance still held Lisbon.

**If the user moves timezone again**, all three of the above must change
together, and the plist `Hour` must be offset if the system zone and `POP_TZ`
diverge. Firing early is the failure that closes the wrong week.

---

# Part II — DRBR

Decisions for the DRBR extension (`drbr-build-brief.md`). Numbered `DR<phase>.<n>`
so they never collide with POP's `D` numbers above. Phase 0 answers came from
the user on 2026-09-16; everything after Phase 0 was decided without asking,
per the build prompt.

## Phase 0 — dependencies (user answers and consequences)

### DR0.1 — `drbr/` is the master folder; POP was moved into it
User decision. POP's launchd agents were stopped, `data/pop.db` and both `.env`
files were backed up to `/Users/Jacob/Projects/drbr-backup-20260916/` (integrity
check `ok`), POP's files and `.git` were moved into `drbr/`, the venv was
rebuilt (venvs are not relocatable), the three plists and `run_build.sh` were
repointed, and the agents were re-bootstrapped. 488 tests and `check_env.py`
passed before the first DRBR change. Work happens on git branch `drbr`.

### DR0.2 — POP stays flat at the repo root, not in `drbr/pop/`
The brief's layout shows `pop/`. POP's 46 intra-module imports are flat
(`import db`), `config.ROOT`/`.env`/`data/` resolve from the module's own
directory, and tests monkeypatch module objects by name. A subpackage would
mean rewriting all of that for no behavioural gain and at real risk to a
working system. New DRBR code follows the brief's names (`sources/`,
`labels.py`, `report.py`, `dashboard/`, `extension/`).

### DR0.3 — The vault is scanned in place; no local agent
The brief's fork assumed a VPS. There is no VPS: POP runs on the Mac, the vault
is a local git repo at `<vault path>` (obsidian-ingest's own default), and the browser extension runs on
the same machine. "Server-side scanning" and "local" are the same host.

### DR0.4 — Screen Time OCR uses Apple Vision on-device
User decision ("privacy is priority"). No image leaves the Mac. The cost is
lower accuracy than a large vision model, mitigated by keeping every raw image
for re-parsing and by marking all screenshot figures `screenshot_ocr`.

### DR0.5 — Pooled wasteful allowance is 360 minutes
User decision (6 hours across every source). `.env` `WEEKLY_ALLOWANCE_MIN=360`.
The currently open POP week keeps its 180-minute snapshot, as POP designed;
the next week opens at 360.

### DR0.6 — End date: 8 weeks from the first valid data date
Default accepted. "First valid data date" = `period_start` of the first
successful (ok/partial) non-vault import. Stored once in `meta.diagnostic_start`
and never moved, even if an earlier week is imported later, so the countdown
cannot jump.

### DR0.7 — Inbox is everything under `01-Inbox/`, as it is today
User decision. Any tracked note whose copies are all under `01-Inbox/`
(including `Read/`, `Read/X Bookmarks/`, `YoutubeTranscripts/`) is unprocessed;
anywhere else is kept. Only notes carrying a DRBR capture id are tracked, so
everything already in the vault is outside the measurement.

### DR0.8 — Cold Turkey usage comes from the website-statistics export
The first export was the "Websites Blocked" count chart and carries no time.
The second (`Website Statistics`) carries per-domain minutes per day. It lists
only 5 domains, most likely the chart's top N. Unlisted domains are reported as
unmeasured, never as zero. One desktop machine is assumed; a second machine is
a named blind spot.

### DR0.9 — Live calls made in Phase 0
YouTube `videos.list` twice (manual validation, then `check_env.py`); Telegram
`getMe` and the Spotify refresh exchange twice each (baseline before the move,
`check_env.py` after). No other live call was made; none is reachable from a
test.

### DR0.10 — `check_env.py` separates "failed" from "unavailable"
5xx, 429, quota exhaustion and unreachable hosts are UNAVAILABLE (exit 3):
the credential is unproven, not bad. Everything else is FAILED (exit 1). The
check now covers YouTube, the vault and inbox paths, outbox and raw-evidence
writability, and Apple Vision importability.

## Phase 1 — architecture and contracts

### DR1.1 — Numbered, transactional migrations; `schema.sql` frozen at v1
`schema.sql` stays the version-1 baseline, so a fresh database and an upgraded
one run the identical path (asserted by test). Each `migrations/NNNN_*.sql`
runs in one transaction with its version bump; a failure rolls the whole step
back and it retries whole on the next start. Migration 0002 is additive: no POP
row is rewritten, and new columns default to pre-DRBR behaviour. Verified
against a copy of the real database (row counts unchanged, integrity ok).

### DR1.2 — YouTube captures reuse the POP queue via `episodes.platform`
The brief pools the allowance across "a wasteful podcast and a wasteful YouTube
save", and spending it "requires the existing POP path". So YouTube saves go
through POP's queue. `queue.spotify_id` is `NOT NULL REFERENCES episodes`, and
SQLite cannot relax that without a table rebuild. So YouTube rows live in
`episodes` with `platform='youtube'` and key `youtube:<videoId>`. The column
name is now slightly wrong, but every POP query, state transition and test is
untouched.

### DR1.3 — The lock stays on for worthwhile items; only the allowance is conditional
The brief says "lock and allocation ceiling become conditional on the label"
and "worthwhile content is unrestricted and uncounted". The build prompt says
"POP's existing lock behaviour remains intact for worthwhile items" and lists
"worthwhile items accidentally bypassing the existing gate" as a defect. They
agree on the allowance and conflict on the lock. The prompt is the more
specific instruction and was written after the brief, so worthwhile items keep
the 48h lock and triage but cost 0 minutes and never count toward debt or
alerts. "Unrestricted" is read as "no minute ceiling".

### DR1.4 — Debt comes from pooled wasteful consumption
POP's debt was driven by listened minutes (D1.5). DRBR keeps that principle and
widens it: debt = pooled **wasteful** minutes across Spotify, YouTube, Screen
Time and Cold Turkey minus the effective allowance, clamped as before (D1.6).
`weeks.listened_min` keeps its POP meaning (raw Spotify minutes) so no POP
reading of it changes.

### DR1.5 — Late imports recompute debt
Screen Time, Cold Turkey and YouTube arrive after the week closes, while POP's
rollover runs Sunday 18:00. `evidence.finish_import` calls
`allowance.on_measurements_changed`, which recomputes `debt_min` for weeks
whose predecessor's minutes changed. A debt can therefore rise on Monday. The
alternative, ignoring late minutes, would make the pooled allowance a
Spotify-only allowance in practice.

### DR1.6 — Per-item override is a hashtag in the why-note
`#worthwhile`/`#worth` or `#wasteful`/`#waste` anywhere in the note. The brief
says the override "is the same act as writing the why-note" — one message, one
decision. A button after capture would be a second decision. Conflicting tokens
are not guessed between.

### DR1.7 — Weekly data maps Sunday..Saturday → the allowance week opening that Sunday 18:00
Screen Time and Cold Turkey report local Sunday–Saturday weeks. POP's allowance
week runs Sunday 18:00 → Sunday 18:00. Mapping the calendar week to the
allowance week that opens on its Sunday overlaps by 6¼ of 7 days, and a day is
never split. The residual (Sunday before 18:00 charged a few hours "late") is
accepted and documented rather than apportioned by guesswork.

### DR1.8 — X is one source, pre-seeded wasteful
The brief fixes X's label. `labels.normalise_key` folds the iPhone app "X",
`x.com` and `twitter.com` into a single `('x','x')` source so the label is
applied once and X's minutes are one row in cost per keeper.

### DR1.9 — Double-counted surfaces are excluded from totals, not deleted
YouTube history is account-level, so Screen Time's "YouTube" app and Cold
Turkey's youtube.com would count the same viewing twice; Spotify's app and web
player overlap POP polling. Those rows are stored and shown with the reason,
but excluded from pooled minutes, allowance and cost per keeper. Spotify music
is outside the brief's scope and is therefore not measured anywhere, which is
stated as such.

### DR1.10 — Cost per keeper is ranked per group, not per individual source
Keepers attach to a group (X, YouTube, web) through the capture type, but a
vault capture carries no reliable channel id or show, and Screen Time can't
split an app by content. So the ranked table is one row per group (podcasts,
YouTube, X, iPhone apps, desktop web). Groups with no keeper path show minutes
with "no keepers yet", never a price.

### DR1.11 — Podcast impulse listening stays a named blind spot
POP polls only captured episodes, so listening that never touched the queue is
invisible. A followed-shows sweep would fix it, but the brief says "No change"
for Spotify beyond conditional gating. It's reported as a blind spot, not
silently absorbed or estimated.

### DR1.12 — YouTube progress is read from the page's own data, parsed in Python
The real saved page carries `thumbnailOverlayProgressBarViewModel.startPercent`
in `ytInitialData`; scrolled pages arrive as browse JSON. The same value
drives the red overlay, so this satisfies "readable as a value, not estimated
from pixels", and it survives CSS/DOM reshuffles better than element
selectors. The extension ships raw JSON; the parser runs server-side against
the real fixture. Missing structure is a loud failure.

### DR1.13 — Screen Time: only complete past weeks become measurements
The real screenshots show "This Week" (partial). Storing a partial week under
a week key would later collide with, or double-count against, the full week.
Partial or all-devices captures are kept as evidence with a warning saying
what to send, and write no minutes.

### DR1.14 — YouTube queue items never auto-transition to `played`
POP's `played` is Spotify's own `fully_played` flag. YouTube offers no
such flag. Declaring "played" at some watched percentage would be an inferred
threshold. YouTube items lapse and expire through POP's normal rules. Their
consumption still appears in gated/ungated minutes and the "played" funnel
stage counts measured watching after promotion.

### DR1.15 — obsidian-ingest talks to DRBR through a JSONL outbox
The two bots run from different repos and venvs. An append-only JSONL file
couples them by a documented line format instead of shared code or a shared
database connection, and DRBR ingests it idempotently by `capture_id`.

### DR1.16 — The outbox line is written before the note
If the log succeeds and the note write fails, the capture is `excluded` at
scan, which is harmless. If the note were written first and the log failed,
the note would carry an id DRBR never heard of and would be silently missing
from the denominator.

### DR1.17 — "Edited after capture" compares body hashes, not mtimes
mtime changes on moves, syncs and git checkouts. A hash of the body (frontmatter
excluded, so Obsidian property edits like `read_status` don't count) changes
only when the content does.

### DR1.18 — Temptations rejected
- Marking a YouTube video "played" or "valuable" above some watch percentage (DR1.14).
- Treating Screen Time categories ("Social", "Productivity") as labels.
- Using mtime or file size to judge engagement (DR1.17).
- Ranking by items-per-hour (the brief chose cost per keeper for stability).
- Estimating reading minutes for vault captures.

### DR1.19 — Extension ingest shares the dashboard's 127.0.0.1 port, behind a token
One local Flask process serves the dashboard and `/api/youtube/*`. A random
`DRBR_INGEST_TOKEN` is required on the API so no other local page can post
fake history.

### DR1.20 — DRBR callback data uses the reserved `d:` prefix
POP's callback handler caught every button. It is now registered with a
pattern that excludes `d:`, so POP's "Unrecognised button." behaviour for
garbage data is unchanged and DRBR namespaces can't collide with
`promote:`/`drop:`.

### DR1.21 — Duplicate evidence is detected by content hash; re-parsing is explicit
Sending the same file twice returns the earlier import (`duplicate_of`) and
writes nothing. `reparse` re-reads the stored raw bytes and creates a new
import row, whose facts replace the old ones on their natural key.

### DR1.22 — An unmeasured value is NULL with a status, never zero
Enforced three times: the `measurements` CHECK constraint, `evidence.upsert_measurement`, and the report layer counting `unmeasured_items` separately.

## Phase 2.5 — integration decisions

### DR2.1 — YouTube minutes use the saved resume position when the page carries one
Found by Agent A in the real page: `startPercent` never renders below 10, and
17 of the 22 videos at exactly 10% carry `watchEndpoint.startTimeSeconds` with a
true position of 0.3–7.6%. Using the percentage would count 57 s into a
47-minute video as 4.7 min. For partial videos with a saved position,
`minutes = min(resume_s, duration_s)/60`; otherwise the percentage formula;
Shorts unchanged. This reads a stored value, the YouTube equivalent of Spotify's
`resume_point`, so nothing is inferred. `detail.minutes_basis` records which
rule applied. `PARSER_VERSION` is now `youtube-2`.

### DR2.2 — Measurement starts at a fixed go-live date (`DRBR_GO_LIVE=2026-09-13`)
The first YouTube sync reaches about 3.5 weeks back from the initial page alone,
which would have set `diagnostic_start` to 24 August and burned weeks of the
eight-week window before DRBR existed. Go-live is the Sunday of the week DRBR
was built, so this week's Cold Turkey export counts. YouTube history before
go-live is kept as raw evidence and counted in import meta
(`skipped_before_go_live`), but not measured. `finish_import` never sets
the diagnostic start earlier than go-live.

### DR2.3 — A regular YouTube video is one fact per sync, under its newest listing date
History relists a video under every day it was watched, each time with the same
current position. Writing that position under every date would turn one
position into several viewings. One fact per sync goes under the newest date,
with the other dates in `detail.also_listed_on`. A Short is one fact per
distinct day, since each day is a separate complete viewing. This consistently
undercounts rewatches, which the brief accepts.

### DR2.4 — The YouTube cursor carries a date
When the cursor video is rewatched it reappears at the top. An id-only cursor
would then stop the next sync immediately and drop a week. A match needs the
same id and a listing date ≤ the stored cursor date. The cursor never moves
backwards, and `reparse` reapplies the cursor recorded in the original import.

### DR2.5 — The "unknown channel" placeholder is never offered for a label
A video neither the page nor the API attributes to a channel goes under
`youtube_channel/unknown`. Labelling that placeholder would apply to unrelated
videos, so `sources_needing_label` skips it. Its minutes still count as
unlabelled, which means wasteful.

### DR2.6 — A vault scan fails when the vault root is missing
Agent C's addition. Otherwise an unmounted or misconfigured vault would
mark every capture `excluded` and silently empty the denominator.

### DR2.7 — Relabelling a source recomputes carried debt immediately
Debt is computed from labels as they are now (Agent D). Without a trigger, a
relabel would change past debt only when the next import happened to arrive.
The Telegram label callback calls `allowance.on_measurements_changed` at once;
the dashboard toggle does the same. Verified on a copy of the live database
before the live restart: recomputing all four existing weeks reproduced the
stored debt exactly.

### DR2.8 — Screen Time ingestion details (Agent B, accepted)
- **Batching:** one batch per chat. An album holds at most 10 images, and a full scrolled capture already hits 10, so batching by album would split one week into two partial imports.
- **Rows with no name:** a row whose name OCR could not read (Vision never reads the one-letter "X" on a full-page pass) gets a second, cropped re-read. The name is accepted only when two crops agree, and the measurement is flagged as re-read.
- **Misreads:** misread names ("lelegram") are kept as read and become their own sources. Correcting them would be a guess, and labelling makes a misread harmless.
- **Confidence:** Vision reports 1.0 even for misreads, so confidence gates nothing.
- **Duplicates and reparse:** a resend is detected by the same images arriving in the same week. The manifest can't be used for this, because it carries `received_at`. `reparse` replaces the previous import's rows, so apps a better parse no longer finds don't linger.

### DR2.9 — Cold Turkey days on or after the export date are not recorded
The real export, made on a Wednesday, runs to Saturday with zeros. Those zeros
are days that hadn't happened yet, not measured zero usage, so they are dropped
with a warning and the import is `partial`.

### DR2.10 — Report windows start at the diagnostic start
`report.window_start` = `diagnostic_start` once set, else `settings.go_live`.
Queue captures from before it drop out of the dashboard funnels. POP's own
`/stats` in Telegram still reports all time.

## Phase 4 — verification decisions

### DR4.1 — Evidence dated before go-live never charges the allowance
DR2.2 skipped pre-go-live YouTube history at ingest, but Cold Turkey and Screen
Time still wrote those days and `pooled_minutes` charged them. A first export
reaching back past go-live would have handed week 1 a debt built from days
before DRBR existed. The rows are still stored and shown in weekly totals
(they are evidence); only the allowance, debt and gated split ignore them.
Spotify is POP's own allowance, older than DRBR, and is unaffected.

### DR4.2 — A vault scan without its inbox fails
Without the inbox folder every note is "outside the inbox", so every capture
would be recorded kept and cost per keeper would fall. On macOS a wrong-case
`DRBR_VAULT_INBOX` passed the old `is_dir()` check and matched no path, with
the same result. The inbox is resolved to its on-disk spelling, and a missing
one fails the scan when captures exist (same reasoning as DR2.6).

### DR4.3 — Screen Time OCR stays on the bot's event loop (accepted)
Measured at 2–3 s for a 10-image capture, once a week, in a one-user bot whose
updates are handled one at a time anyway. Moving Vision to a worker thread
would put pyobjc on a non-main thread (autorelease pools, framework thread
rules) on a path the automated suite cannot exercise, because Vision is
blocked in tests. Not worth that risk for a few seconds a week.

### DR4.4 — A relabel does not refund minutes already promoted (accepted)
`weeks.promoted_min` records what was committed at the moment of promotion,
under the label that applied then. It only caps further promotions in the
same week and resets at rollover; debt, alerts and reports read consumption
through the current labels and do change at once (DR2.7). A refund would need
per-promotion cost records, which POP never stored, to undo a decision that
was correctly priced when it was made.

## Post-build — user-requested changes

### DR5.1 — Cold Turkey is read automatically from this Mac's own statistics, hostnames only
User request (2026-09-16): "Can we automate cold turkey?" The brief assumed no
automated route. In fact Cold Turkey Blocker keeps its statistics in a local
SQLite file (`/Library/Application Support/Cold Turkey/data-browser.db`,
table `stats(date, domain, seconds, user)`, one row per minute). Checked
against the user's manual export: summed per hostname per local day, it
reproduced every finished day's figure to the hundredth of a minute. It also
lists every domain (76–187 a day in the first week), where the export listed 5.

**Privacy.** The `domain` column actually holds full URLs, search queries
included. The user approved a **hostname-only** version. Rows are reduced
to hostnames in memory while reading, and nothing finer is stored anywhere: not
in the database, not in raw evidence, not in logs. This deliberately narrows
"preserve raw evidence": the stored evidence is the hostname/day/minutes
document, in Cold Turkey's own export shape. The full-URL table is never copied.

**Mechanics:**
- `jobs.py daily` calls `sources/coldturkey_local.sync` before sending prompts.
- It reads finished local days only (never today), from the later of go-live and 8 days ago, so late corrections replace facts on their natural key. An unchanged window is a duplicate import.
- It feeds the existing `coldturkey.ingest`, so every tested rule still applies, with `origin="local_db"`.
- Local day = `POP_TZ`. The first week had no activity near midnight, so it could not tell local days from UTC days; local matches the rest of DRBR.
- Provenance keeps the `manual_export` key, so these facts replace any earlier export facts for the same domain and day. The display text is now "Cold Turkey statistics (this Mac)" and `detail.origin` says which route.
- Desktop app time (`data-helper.db`) is not read. The brief scopes Cold Turkey to websites.

**Fallback.** If the database can't be read, the job step fails loudly, the
week stays uncovered, and the Monday reminder asks for a manual export,
saying the automatic read failed. Manual exports sent to the bot still work.
`DRBR_COLDTURKEY_DB` unset means manual exports only. Tests never read the real file.

**Blind spots updated.** "Top domains only" is claimed only while any
manual-export import exists. "This Mac only" always stands.
