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
lists every domain (29–30 hostnames a day in the first week), where the export listed 5.

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

### DR5.2 — Going over the allowance is recorded, never carried as debt
User decision (2026-09-17): "This notion of the carryover of debt needs to go.
I need it recorded. How much I go over, I don't want to carry debt over."
This retires POP's D1.5/D1.6 (debt from listening, clamped at one allowance)
and DRBR's DR1.4/DR1.5 debt recompute.

- **Recorded:** `weeks.overage_min` (migration 0003) = max(0, pooled wasteful
  minutes − that week's own allowance), in whole minutes and **unclamped**. The
  current week's figure is "so far".
- **Re-recorded:** late imports and relabels update it through
  `allowance.on_measurements_changed`, which now updates every week
  independently. No week's figure affects another.
- **Never carried:** every week opens with the full allowance.
  `weeks.debt_min` stays in POP's schema but was zeroed by migration 0003 and
  is ignored by `Week.effective_allowance_min`, so even a stray write can't
  shrink the gate.
- **Unchanged:** the gate still refuses a wasteful promotion that exceeds what
  is left this week, and alerts still fire at 80% and on crossing. The
  "exceeded" message now says the overage is recorded and next week starts
  full.
- **Where it shows:**
  - dashboard: "Over the allowance so far" and a "Weeks over the allowance" table;
  - Sunday triage header: "Last week went N min over (recorded, not carried)".
- **Live data:** the one existing non-zero debt (21 min on the week of 13 Sep)
  was zeroed by the migration. That week's overage is recomputed from evidence.
- **Stale comments:** `schema.sql`'s comments still describe the debt formula.
  That file is the frozen v1 baseline and is deliberately not edited.

### DR5.3 — Desktop app time is read from Qbserve, per app per day, browsers skipped
User request (2026-09-17): the user asked how Qbserve could improve DRBR and
approved adding desktop apps. Cold Turkey sees this Mac's browsers only
(DR5.1), so native apps (chat, mail, games, the Spotify and X clients) were a
blind spot. Qbserve, already running on this Mac, keeps its tracking in a
local SQLite database (`~/Library/Application Support/Qbserve/UserDatabase.sqlite`).

**What is read** (`sources/qbserve.py`, read-only):
- `Apps(_id, bundle, localized_name)`, `Activities(_id, source, app_id)`, and
  the monthly history tables listed in `HistoryTablesIndex`
  (`activity_id, start_time, duration`).
- Rows are summed per app bundle per local day (`POP_TZ`) while reading. Only
  bundle, display name, day and minutes leave the module.
- A history table name is used only if it fully matches `z_HistoryLog_YYYY_M`;
  anything else in the index is ignored, never interpolated into SQL.

**What is never read:**
- `Activities.title`: window titles and page names. It is never selected, so it
  can't reach the database, raw evidence or logs.
- Qbserve's productivity ratings and categories. They are an automatic judgement
  of value, which is exactly what DRBR refuses to make (DR1.18). Apps are
  labelled by the user like every other source.

**Browsers are detected from the data.** Any app that owns a website activity
(`Activities.source = 222`) is a browser, and all its time is skipped. The user
checked the real data: website activities belong almost entirely to browsers.
There is no hard-coded browser list to go stale.

**Double counting (DR1.9):**
- Browsers: skipped here, because Cold Turkey measures them per site.
- The Spotify client (`com.spotify.client`) is `COVERED_ELSEWHERE` by Spotify
  polling, like the iPhone app and the web player. It is stored and shown, but
  never pooled.
- X clients (`com.twitter.twitter-mac`, `maccatalyst.com.atebits.Tweetie2`,
  matched case-insensitively) fold into the one X source. Each is its own fact
  (item key = bundle), and none overlaps Cold Turkey's x.com because a native
  client isn't a browser.

**Mechanics:**
- New vocabulary: source kind `desktop_app` (key = bundle id, casefolded),
  report group `desktop_apps`, provenance `app_tracker` ("App tracker (Qbserve,
  this Mac)"), import kind `qbserve`. Migration 0004 rebuilds `sources`,
  `imports`, `measurements` and `prompts_sent` to widen their CHECKs.
- `jobs.py daily` runs `qbserve.sync` after the Cold Turkey read. Like DR5.1, it
  reads finished days only, from the later of go-live and 8 days ago. An
  unchanged window is a duplicate, and changed minutes replace facts on their
  natural key. The stored evidence is the app/day/minutes document; the
  database is never copied.
- Desktop app minutes are always ungated. They count in pooled wasteful minutes
  by label, like every other measurement.
- `DRBR_QBSERVE_DB` unset means desktop apps are not measured, and no weekly
  row expects them.

**Absence is not zero.** Qbserve records nothing while it isn't running, which
looks like a day without app use. So an import covers days only up to the local
day of Qbserve's newest record. Later days stay "not received" instead of
reading as 0 minutes. Stale-data handling:
- **Daily warning:** while that record is more than 36 hours old, the daily job
  sends one Telegram message a day (deduped by `prompts_sent` kind
  `qbserve_stale`). It says since when nothing was recorded, that desktop app
  time is missing until Qbserve runs, and to set Qbserve to open at login. The
  warning stops once the diagnostic ends.
- **Dashboard:** `blind_spots` says the same when the latest Qbserve import is
  more than 36 hours old, or when none exists while Qbserve is configured.

### DR5.4 — The week opening 13 Sep was corrected to the 360-minute allowance
User decision (2026-09-17): "set this week to 360". That week's row was opened
with POP's old 180-minute snapshot before the user chose 360 in Phase 0, so
it showed 127 min over. It is the first diagnostic week, and the only row
changed: `weeks.allowance_min` went 180 → 360 for `2026-09-13T16:00:00+00:00`,
then overage was re-recorded. Earlier pre-diagnostic weeks keep 180. A backup
was taken first (`drbr-backup-20260916/pop-pre-360.db`).

### DR5.5 — "Tool": sources that aren't content are out of scope
User framing (2026-09-17), which governs this decision:

> "Let's ensure we do not lose sight of the fact that this tool is specifically for brain rot content, not for how much time I'm wasting on Amazon or websites tagged wasteful. This is purely a content brain-rotting exercise. Do not lose sight of that."

The user asked how amazon.pl should be labelled. Since Cold Turkey's automatic
read (DR5.1) and Qbserve (DR5.3), DRBR sees many sources that are not content at
all: shopping, email, todo apps, Notion, Apple ID, weather, banking, DRBR's own
dashboard, most desktop apps. Forcing each into wasteful or worthwhile would
turn a content diagnostic into a screen-time tracker. The user approved a third
source label, **tool** = "not content; out of scope".

**Why this doesn't reopen the brief's "no neutral".** The brief rejected a
middle *value* band ("a middle band would not be applied consistently").
The worry was an inconsistently applied neutral *score* on content. Tool is not
a point between wasteful and worthwhile. It answers an earlier question, "is
this content at all?", which is a question about scope. Content is still
labelled strictly binary.

**The rule** (the same words on the Labels page and in the Telegram prompt):
DRBR measures brain-rot content only. Label each source by asking: is this
*content* I consume? If yes, is it brain rot (wasteful) or not (worthwhile)? If
I only go there to get a task done — buy, pay, book, log in, organise, check
the weather — it's a tool and isn't counted.

**What tool does:**
- `labels.resolve` returns `tool` (basis `source_label`) for a tool source.
- Tool minutes are kept out of wasteful and worthwhile totals, the label split,
  the allowance, overage, alerts, the gated split and cost per keeper. A queue
  item whose source is tool costs 0 to promote.
- **Shown, never hidden.** Tool minutes are reported on their own:
  `PooledMinutes.tool_min`, `GroupMinutes.tool_min`, `WeekSplit.tool_min` and
  `SourceTotal.tool_min`, a tool column in the dashboard's weekly totals and
  per-week table (never stacked into the content bars), and a standing blind
  spot line. A tool label can't become a place to hide consumption.
- Unlabelled still defaults to wasteful. Tool counts as labelled, so a tool
  source is never prompted again.

**Item overrides stay binary.** `item_labels` keeps its two-value CHECK, and
there is no `#tool` token. A captured queue item is content by definition, so
an override on it (`#wasteful` / `#worthwhile`) still beats a tool source.

**The only automatic tool label.** Migration 0005 labels the `web_domain`
sources `127.0.0.1` and `localhost` tool, because DRBR's own dashboard is the
measuring instrument, not content (user-approved). It labels only rows that
already exist when it runs, with `labelled_at` = the migration time. Nothing
else is labelled for the user.

**amazon.pl:** tool while it's errands. Relabel it wasteful if you browse it
for entertainment. A relabel re-records every week's overage at once.

**Mechanics:**
- Migration `0005_tool_label.sql` (schema_version 5) rebuilds `sources` so
  `label` allows `tool`, the same way as 0004 (foreign keys off,
  `foreign_key_check` before commit, whole-step rollback).
- `drbr_types.TOOL` and `SOURCE_LABELS`. `LABELS` stays the two value labels.
- Dashboard: third button, "tool" filter, and the scope line on the Overview.
  Telegram: third button, callback `d:lbl:t:<id>`.
- **Wording.** User-facing text now says "wasteful content minutes" where it said
  "min used … (wasteful only)" or "min wasteful": poll alerts, Sunday triage
  header, `/start`, dashboard headings, README.

### DR5.6 — The UX overhaul: Telegram stops asking, the dashboard starts telling
User framing (2026-09-22), which governs this decision:

> "I haven't been prompted in any meaningful way to put data in: I don't know
> how and when I'm supposed to be submitting sources like X desktop data,
> desktop web, any of it. This needs a holistic improvement. I don't want the
> Telegram bot pinging me to tag content. It can ping me once, maximum every 3
> days, saying there is content to be tagged in the desktop app, but I don't
> want to be doing it in the Telegram app itself."

Two separate failures, with one cause. DRBR had grown five sources with three
different collection modes (automatic daily, automatic continuous, manual
weekly) and never said anywhere which was which. And labelling — the one act
the whole diagnostic depends on — was happening in the medium worst suited to
it: one message per source, each a decision made without seeing the minutes,
the provenance, or the other nineteen sources.

**What Telegram keeps.** Captures, Sunday triage, allowance alerts, and the
Monday reminder for the two sources DRBR genuinely cannot fetch (Screen Time,
YouTube). That is the whole list. Everything else is a page.

**The 3-day label digest.** `handlers_labels.send_label_digest` replaces
`send_label_batch`. One message, not one per source; at most one per three
local days (the user's own number), dedup through `prompts_sent` kind `labels`
on `MAX(sent_on)` rather than "already today"; nothing before the diagnostic
has run 7 days and nothing after it ends. The text names the count, the
minutes and the biggest group, and links to the dashboard. `/labels` answers
with the same sentence and never consumes the three-day slot — asking is not
being told. The `d:lbl:` callback namespace, its handler, its keyboard and
`render_prompt` are deleted, not deprecated: a handler for buttons nothing
sends would only make `bot.build_app`'s wiring lie.

**Labelling is ordered by minutes now.** The bot asked oldest-first
deliberately: a *prompt* ordered by size would have told the user which
sources DRBR thought mattered. A queue the user opens himself is a different
act — he is choosing where to spend attention — so `labels.label_queue` sorts
by measured minutes in the diagnostic window, biggest decision first. The
exclusions are unchanged and are exclusions of *fact*, not of prompting
policy: covered-elsewhere sources, the unknown-YouTube-channel bucket, and
anything never actually observed. The system still never suggests a label.
`labels.bulk_set_label` writes twenty decisions in one transaction and
re-records every week's overage once (DR2.7).

**"What needs you" and "where each number comes from".**
`report.needs_attention` answers the user's question directly, one row per
source group: automatic or manual, when it runs, when it last produced
anything, whether the current week is covered, and — only when true — what he
has to do. For a weekly manual source the honest question is about the last
*complete* week, because the current one cannot be sent yet; `waiting` and
`missing` are therefore different states. `report.provenance_map` is the
standing table of what each number is, which tool made it, at which provenance
tier, and what it cannot see (reusing `GROUP_BLIND_SPOTS`, so the caveats
cannot drift from the ones on cost per keeper). It says in full that **X is
measured twice and counted once**: desktop x.com through Cold Turkey, the X
iPhone app through Screen Time, folded into one source by `normalise_key` —
the single most confusing thing in DRBR, now stated rather than discovered.

**The Monday reminder is now the only import reminder**, so each message
carries the whole picture: what is missing, that this source is manual, the
numbered steps, and that everything else collects itself. Screen Time's names
the two mistakes that actually happened with real captures — the Most Used
list left ending in "Show More" (every app below it silently missing) and
screenshots sent in two goes (read as two captures, neither a whole week).
Cold Turkey and Qbserve leave the rotation while they are feeding themselves:
the prompt survives only as a fallback, when unconfigured or when the
automatic read is failing, judged by `report.needs_attention` rather than by a
second opinion kept in `handlers_imports`.

**A person may correct an OCR row.** Real case: Facebook was read as both
"2h 27m" and "2h 37m" on two screenshots, so ~150 minutes were stored
`unreadable` with NULL minutes; another row's name was unreadable and 81
minutes were dropped. Refusing to guess is right; leaving it there is not,
because the user can settle it in two seconds by looking at the screenshot.
`report.needs_your_eye` lists those rows with **what was actually read**, and
`screentime.correct_measurement` stores the answer: minutes set, status back
to `ok`, and `detail.corrections` recording that a *person* supplied it, with
the original reading kept. Guard rails:

- Only `screenshot_ocr` rows are offered. A YouTube video with no duration
  from the API has no number a person could supply either, so offering it
  would invite an invented figure.
- `options_min` holds only durations the evidence itself contains (the two
  conflicting readings). `suggested_min` is filled only when there is exactly
  one candidate — with two, choosing is the user's job and inventing a third
  is nobody's.
- Negative or over 1440 minutes (one day) is refused; a second correction
  appends rather than overwriting the first.
- Provenance stays `screenshot_ocr`. The figure still came from a screenshot;
  only the arbitration was human, and a human arbitration is not an API.
- The parser now stores `detail.readings` and `detail.unreadable_reason` so
  this needs no re-OCR, and keeps an unnamed row's raw text in the import's
  meta (`unnamed_row_texts`) — the minutes are real and only the name was
  lost, and there is no measurement to attach it to.

**`jobs.run_sync`.** The data-fetching half of the daily run (outbox, vault
scan, Cold Turkey, Qbserve) is now callable on its own and returns a
`StepResult` per step, so a dashboard "Sync now" does exactly what the morning
run does and shows exactly what the log would have said. `run_daily` calls it
rather than keeping a second copy of the order. Without a bot it sends
nothing, and skipping the Qbserve staleness warning does not consume that
day's slot.

### DR5.7 — The dashboard opens on the current week, and never before go-live
The overview defaulted to the last complete week while the allowance section
always described the current one, so two different weeks sat side by side with
nothing saying which was which. In week 1 the default was also a week *before*
`DRBR_GO_LIVE`, i.e. outside the diagnostic. Now: the default is the current
week; a `?week=` earlier than the diagnostic window clamps forward; a future
week clamps back; and the "previous week" link disappears in the first week
rather than leading nowhere. `test_overview_defaults_to_the_last_complete_week...`
was renamed and repointed with this change, in the same commit, because it
stated the old behaviour as the intent.

Also added here, the routes behind DR5.6: `POST /sync` (run every self-fetching
source now), `GET /labels/queue` (biggest undecided source first, its own
template so `/labels` keeps its table as the page's first `<tbody>`),
`POST /labels/bulk`, and `POST /measurements/<id>/fix` (a person settles a
reading OCR could not). Flash messages travel as whitelisted `?msg=` codes
rather than a Flask session, so the app still needs no secret key.

### DR5.8 — The Overview opens with a written summary, generated weekly from DRBR's own figures
User request (2026-09-22), which governs this decision:

> "I'd like 'overview' to be a summary based on LLM findings parsed findings (Opus 5) on my activity, e.g. 'brainrot activity is decreasing but reflex content consumption remains an issue' or whatever"

The Overview shows ten panels of figures and leaves the reading of them to the
user. Once a week, DRBR now writes the reading down: three to five plain
sentences at the top of the page, generated by Claude Opus 5 from the numbers
DRBR already computed.

**Why this does not breach "never infer value" (DR1.22, DR5.5, DR5.6).** The
rule DRBR is built on is that *the system counts and the person labels*. A
generated paragraph would breach it if it decided what was worth the time. This
one does not, for four reasons that are enforced rather than hoped for:

1. **It describes measured figures.** Every input is a number `report.py`
   already produces and already shows on the page. The system prompt says every
   number must come from the payload, that nothing may be invented or
   estimated, and that only two numbers both present in the payload may be
   compared.
2. **It judges nothing.** The prompt forbids saying whether any content was
   worth the time, forbids suggesting a different label for anything, and
   forbids advice, recommendations, encouragement, praise, criticism and
   moralising. "Wasteful" and "worthwhile" remain the user's own labels, used
   as the names of two measured quantities and nothing more.
3. **It stores what it was based on.** The exact payload is written to the
   `insights` row beside the summary, so any sentence can be checked against
   the figures it came from. A summary whose numbers do not appear in its own
   payload is visibly wrong, not merely disputable.
4. **It falls back to computed sentences.** With no key, too little data or a
   failed call, `insights.fallback_summary` writes the same kind of paragraph
   arithmetically ("Wasteful content fell 38% week on week, from 307 min to
   190 min."), and is explicit when there is not enough data to speak of a
   trend rather than manufacturing one. The panel is never empty and never
   guesses.

It also changes nothing. `insights.py` writes one table, `insights`; no stored
figure, label, week or measurement is touched by generating or refreshing a
summary.

**The privacy choice: aggregates only (chosen by the user, option (a)).**
Nothing that identifies a source or a piece of content leaves this Mac. The
payload may contain report **group** names (podcasts, YouTube, X, iPhone apps,
desktop web, desktop apps), minutes by label per group per week, week-on-week
changes, allowance and overage, reflex (ungated wasteful) minutes and share,
gated vs ungated per group, cost per keeper per group, the POP and vault funnel
counts, and per-week coverage gaps. It never contains a podcast show, a YouTube
channel, an app name, a domain, a Mac app bundle id, an item title, a video id,
a note title, a path, or anything at all from the vault — and never a stored
import warning, which quotes real rows. The group keys are the fixed
`T.REPORT_GROUPS` constants, not source keys. The only prose the payload carries
is the coverage-gap sentence, assembled from `report.GROUP_TEXT` and
`report.EXPECTED_WEEKLY`. `tests/test_insights.py` seeds a database with source
names nobody would write by accident, walks every string leaf of the built
payload, and fails on anything that is not a date, an instant, a known constant
or that one assembled sentence.

**Cadence and cost.** One call a week. `jobs.py daily` runs the `insights` step
every morning; `write_summary` returns the existing row unless the week has
rolled over, so six mornings out of seven it is a single SELECT. The week
summarised is always a *complete* one — the current, unfinished week is dropped
before the payload is built, because half a week read as a trend would be a lie
every Monday. `max_tokens` is 400, thinking is off at `effort: "low"` (the
answer is three to five sentences read off a JSON document; reasoning tokens
would eat the budget the summary needs), and the payload is a few thousand
tokens: a few cents a month at Opus 5 rates. `POST /insights/refresh` on the
Overview rewrites it on demand with `force=True`.

**Turning it off.** Remove `ANTHROPIC_API_KEY` from `.env`. Nothing is called,
nothing is sent, and the panel shows DRBR's own computed sentences with the
reason printed beside them. `check_env.py` reports the key as optional and
checks its shape only; it never makes a live call.

**Failure is never the caller's problem.** `write_summary` never raises. No key,
too little data, a network error, a refusal, an empty or truncated answer: each
stores `status='failed'` with the printable reason and returns the fallback. The
`insights` step of `run_daily` is deliberately **not** counted as a failed step —
a missing paragraph is not a data failure and must not turn the morning launchd
log red next to real ones.

**The view branches on one field.** `Insight.model is None` **is** the fallback:
a model run always names its model, DRBR's own sentences never do. There is no
second boolean that could disagree with it. `Insight.fallback_reason` carries
the reason verbatim and printable ("No ANTHROPIC_API_KEY is set…", "API call
failed: …", "Too little data: …"), because every other failure on this dashboard
prints its real error and this panel is not going to be the one showing a status
code. The `status` column stays in the table for storage and querying only.
`Insight.based_on_through` is a **local date** (`YYYY-MM-DD` in `settings.tz`),
the Saturday of the last complete measurement week — not an instant, not derived
from `week_start` — so it compares directly with `report.window_start` and a
measurement week's end and the panel can say when it was written relative to the
data.

**The key.** It is read into `Settings.anthropic_api_key` and goes nowhere else:
not into a log, a payload, a fixture, an exception message or the database.
`insights._scrub` removes it from any error text before that text reaches the
row or the log, and a test asserts it.

**Mechanics:** migration `0006_insights.sql` (schema_version 6) adds the
`insights` table; additive only, so no rebuild. `insights.py` holds
`build_payload`, `write_summary`, `fallback_summary`, `latest` and the `Insight`
dataclass. `jobs.run_daily` gains the `insights` step (not `run_sync`: a "Sync
now" press must not spend money). `dashboard/app.py` passes `insight` to the
Overview and adds `POST /insights/refresh`. Tests never call the API: the suite
blocks the network and every test injects a fake client or runs the no-key path.

### DR5.9 — A fallback summary is retried the next morning; a written one is not

**22 September 2026.** DR5.8 wrote the summary at most once per allowance week
and counted a `failed` row as written, so that a missing key or a broken
provider could not turn the daily job into a daily API call. A real failure
showed that rule to be the wrong shape. Three generations ran within eight
minutes: the first succeeded, the second came back `400 credit balance is too
low`, the third succeeded again. Nothing had been fixed in between — the
balance was simply brushing the floor.

Under the old rule, if that middle attempt had been the morning's only one, the
Overview would have shown DRBR's own computed sentences for a further seven
days, and only a person noticing the amber line and pressing refresh would have
recovered it. A summary that reads "no model involved" for a week because of a
four-minute outage is not honest reporting, it is a stuck flag.

**So the skip condition is now the status, not the existence, of the row.** A
row with `status='ok'` closes the week. A fallback row does not, and the next
daily run tries again.

The original cost worry does not survive contact with the code: the no-key and
too-little-data paths both return before a client is built, so they were never
capable of spending anything, retried daily or not. What remains is a provider
that is actually down or an account that is actually empty — one rejected call
a morning, which bills nothing and stops on its own the day it is fixed.

**Mechanics:** one condition in `insights.write_summary`, plus
`test_a_fallback_week_is_retried_by_the_next_run`. `force` is unchanged, and so
is the refresh button. `latest()` already reads the newest row, so a successful
retry supersedes the fallback with no further change.

### DR5.10 — A week that has not been asked for is "not due yet", not "missing"

**22 September 2026.** The Import column had two words for absent evidence,
`missing` and `incomplete`, and used them from the first hour of a week. On
Tuesday the 22nd, looking at a week that was two days old, Jacob asked: "It
says 'missing and incomplete,' but do I owe anything yet?"

He did not. DRBR asks for manual evidence on the Monday *after* a measurement
week ends, and never asks for a week in progress. iPhone apps read `missing`
because Screen Time is a weekly screenshot that cannot exist yet, and would
have read `missing` every day of every week by construction. YouTube read
`incomplete` because page data covered two days of a seven-day week that had
only had two days. Both were describing a schedule working correctly in the
vocabulary of a fault.

DR5.6 had offered this change and it was declined in favour of the blunter
wording, on the reasonable ground that it never lets you forget a source
exists. Meeting it in practice reversed the judgement, which is the right way
round: the question the page provoked was the argument for changing it.

**So the four absent-evidence states are now two pairs**, the same fact about
coverage worded by whether the week has been asked for: `not_due`/`missing` for
nothing arrived, `so_far`/`incomplete` for coverage that does not span the
week. The threshold is `report.week_is_due`, read from the two rules
`handlers_imports.send_due_prompts` already prompts on — the Monday after the
week, and never a week that ended before go-live (DR2.2). Sharing one rule is
the point: a page that says "missing" on a day DRBR would not ask is the bug
being fixed, and two copies of the threshold would let it return.

`week.due_on` carries that Monday to the page, because the label alone still
does not answer the question asked — the date does. `everything_in` gives a
finished, fully covered week a positive state to report instead of silence.

**Values stay slugs, not prose.** The Import cell renders
`class="import-{{ t.import_status }}"`, so "not due yet" would have become
three CSS classes. The backend decides which state holds; the template owns the
words and the styling, and neither new state should be amber — neither is a
problem, and amber is what prompted the question.

**Mechanics:** `report.py` gains `IMPORT_NOT_DUE`, `IMPORT_SO_FAR`,
`IMPORT_AWAITED`, `due_on`, `week_is_due` and `everything_in`; `weekly_totals`
decides per week. `WeekChoice` gains `due` and `due_on`. `dashboard/app.py`
passes `everything_in`. The wording lives in `overview.html`, which belongs to
the UI session and was handed over rather than edited from here.

### DR5.11 — Unlabelled is undecided, not wasteful

**22 September 2026.** Asked to assess the whole thing, Jacob said the numbers
were over-indexed and that the core wasteful-versus-worthwhile question had
ended up at the bottom. Reading the rows behind the headline turned that from a
layout complaint into a measurement one.

The week of 13 Sep reported 495 wasteful minutes. It was made of 205 minutes on
sources he had actually called wasteful and 290 on sources nobody had labelled
at all — and the two largest of those were **Brave, 163 min**, a phone browser
with no visibility inside it, and **`file:`, 82 min**, local files opened from
his own disk. Of the twenty largest sources that week, two were labelled
brain-rot content. The default "unlabelled counts as wasteful" was written when
we assumed most measured time would be content; 1,437 of 2,073 minutes were
tools, so the assumption was simply wrong.

The deeper fault is that the headline could not be falsified. It fell when he
labelled and rose when evidence arrived — never because he watched less, which
is the only movement the diagnostic exists to detect.

**So the headline counts only what the person has decided.** `wasteful_min` is
labelled-wasteful alone; the undecided minutes are `unclassified_min`, reported
beside it rather than folded into it. Nothing is hidden and nothing is dropped.

**The allowance still counts both**, as `charged_min`. An allowance is a rail,
and a rail that ignores everything it cannot identify is not a rail: charging
only the decided minutes would have taken a week that was 135 minutes over and
called it comfortably inside. `charged_min` reproduces the old figure exactly,
so no recorded overage changes and no history is rewritten.

**The gate is unchanged too, and deliberately.** `labels.resolve` still answers
`wasteful` for an unlabelled source, because a gate is a decision point where an
unrecognised thing should be treated as brain rot until the person says
otherwise. Reporting is a measurement, where it should not. The two readings are
separated by basis, not by a second resolver: `allowance._field` reads
`BASIS_DEFAULT` and nothing else changed about precedence.

Alert and triage copy stopped calling the charged figure "wasteful content
minutes" — it now holds undecided minutes too, and the old wording would have
asserted a judgement the person had not made.

**Also in this pass: `report.top_sources`.** The Reflex section listed "Desktop
web 49, Desktop apps 5.6" — the pipe the evidence arrived through, not the thing
consumed. Nobody watches desktop web; they watch VICE Vault, streamseast.ws, X.
The names were in the database and no surface used them. `top_sources` returns
the week by name, biggest first, with the label (None when undecided, never
guessed), the reflex share per row and the provenances. Tool sources never
appear. It reuses `pooled_minutes`' exclusions so the list and the totals cannot
drift apart, and a test asserts exactly that.

### DR5.12 — Records becomes the detail page; the Overview sheds, nothing is deleted

**22 September 2026.** The Overview restructure moves six sections off the front
page. Jacob confirmed the destination explicitly: they move to Records, they are
not removed. The instrumentation earned its place in the product; it had not
earned the first screen.

So `records_page` now renders from exactly the values `overview()` computes —
`week`, `totals`, `allowance`, `gated`, `keepers`, `funnels`, `imports`,
`week_imports`, `everything_in` — rather than recomputing them with its own
filters. One source per figure means a number cannot say one thing on the
Overview and another on Records.

Records keeps its own `?group=`, `?label=`, `?start=`, `?end=` filters for the
row table, and gains `?week=` for the weekly surfaces, defaulting exactly as the
Overview does (DR5.7).

Sequencing matters and was agreed with the UI session: the context lands first,
the sections are added to Records and removed from the Overview in one commit
after. There is never a window in which a figure exists nowhere.

### DR5.13 — The phone browser is measured by the sites inside it

**23 September 2026.** Screen Time reports a phone browser as one opaque block.
Brave was 163 minutes in the week of 13 September — the single largest source
that week, larger than YouTube, X and every desktop site — and nothing could say
whether it was Reddit or a bank. Counting it as brain rot asserted something
nobody knew; excluding it would have hidden the biggest thing on the page.

Brave Sync carries the phone's visits to this Mac, where they can be read
locally. Jacob enabled History sync on both devices on 23 September and the
first four visits arrived within minutes, carrying a non-empty
`visits.originator_cache_guid` — the originating device — so a phone visit is
cleanly separable from one made here. That also settles a documentation
contradiction: Brave's Sync v2 wiki lists history as an iOS type and its 2021
iOS announcement says bookmarks only. The wiki is right.

**Synced visits carry no duration.** Every arriving row has
`visit_duration = 0`: sync says *that* a page was opened, never for how long.
So the minutes still come from Screen Time, and the sites come from history.

**The minutes are apportioned by dwell, and that is an estimate.** Each visit is
credited with the gap to the next, capped at `IDLE_CAP_MIN` (10 minutes) so a
phone left unlocked on a page cannot swallow a week; the last visit takes the
median of the others rather than the cap, which would make whatever was opened
last the largest thing in the week. The shares are then scaled so they sum to
Screen Time's figure exactly: **the total is measured, only its division is
estimated.** `browser_sync` exists as its own provenance so the page can say
which of the two a number is, rather than passing an estimate off as a reading.

Jacob chose this over composition-without-minutes. The alternative was strictly
more honest and practically useless: minutes are the currency of every other
surface, so an unattributed block can never be labelled, never charged, and
never compared.

**Double counting is prevented by the mechanism already there.**
`(iphone_app, "brave")` joins `COVERED_ELSEWHERE`, exactly as the iPhone YouTube
app does. Its minutes stay visible in the "covered elsewhere" column, so an
exclusion still cannot hide them, and `report.blind_spots` names any period
whose block was excluded while no synced history covered it — the one case
where minutes would otherwise leave the week without a word.

**Privacy.** The `urls` table holds full URLs with paths and query strings.
Every row is reduced to its hostname in memory while reading, exactly as Cold
Turkey is (DR5.1); nothing finer reaches the database, the stored evidence, a
log or an exception message, and a test seeds distinctive paths and asserts
none survives. Brave's own file is copied before reading and never written to.
The cost, stated to Jacob before he enabled it: history now leaves the phone,
end-to-end encrypted, through Brave's servers. It is the first thing in this
system that leaves the device at all. Everything after that stays local.

**Mechanics:** migration `0007_phone_browser.sql` (schema_version 7) widens
`measurements.provenance` with `browser_sync` and `imports.kind` with
`brave_sync`. `sources/brave_history.py` holds the read, the dwell weighting,
the apportionment and the import. `report_group(web_domain, iphone)` is the
phone group, not desktop web, so the sites land where the block they replace
was. `jobs.run_sync` gains a `phone_browser` step, configured by
`DRBR_BRAVE_HISTORY_DB`; unset, the phone browser stays one block and nothing
changes.

### DR5.14 — The scheduled job applies whatever is in the working tree

**23 September 2026.** Twice now a migration reached the live database before it
was committed, and both times it was blamed on a dashboard restart. The second
time that was wrong, and finding out why matters more than the attribution.

`migrations/0007_phone_browser.sql` appeared at 09:24:46. The launchd job
`com.jacob.drbr.jobs` runs at 09:30 daily, and `logs/jobs.log` has it starting
at 09:30:04 — four minutes later. `jobs.py` calls `db.migrate` at start, so that
run applied 0007. The log line proves it was running uncommitted code, not just
an uncommitted schema: `phone_browser: no phone browser figures to attribute`, a
step that existed only in the working tree at that moment.

So the vector is not a restart at all. **A scheduled job runs the working tree,
whatever state it is in, and nobody triggers it.** A pre-restart check cannot
catch it because there is no restart to hang the check on. Between 09:30 and the
commit, the live database was migrated by half-finished work and the daily
import ran through a source module that had no tests yet.

No harm either time — the database was backed up, `integrity_check` and
`foreign_key_check` came back clean, and the live schema was byte-identical to a
freshly migrated one. The exposure is what matters: a migration that was wrong,
or a source module mid-edit, would have reached real data unattended.

**The window is wider than 09:30.** launchd runs a *missed* calendar job at the
next wake, so a tree left dirty overnight on a sleeping Mac is exposed at
breakfast rather than at half past nine. "Not across 09:30" is therefore the
wrong shape of rule.

**Working rule while two sessions share a tree:** a migration is never left
uncommitted when you stop working. Either commit it or move it aside. The same goes for a
source module wired into `jobs.SYNC_STEPS` — adding the step is what arms it,
so the step goes in last, after the module it calls is finished and tested.

Recorded rather than fixed. Making the job run committed code only — a separate
checkout, or refusing to migrate from a dirty tree — is a real change to how
this is deployed, and worth doing deliberately rather than in passing.

### DR5.15 — `m.` is the same site, and an import owns its period

**29 September 2026.** The first week with both a Screen Time block and synced
phone history attributed 164 minutes across 17 sites, and immediately showed two
defects that only phone data could reveal.

**`m.youtube.com` was a source of its own.** `normalise_key` folded `www.` but
not `m.`, and desktop evidence never carries a mobile front door, so it had
never mattered. It matters now: phone YouTube arrived as `m.youtube.com`, which
is not the `youtube.com` key in `COVERED_ELSEWHERE`, so 24 minutes of YouTube
watched in the phone browser were counted here *and* in account-level YouTube
history. `m.reddit.com` would likewise have carried a label separate from
`reddit.com`. Both front doors now fold to the site, and only the exact prefix:
`maps.google.com` and `media.example.com` are untouched.

**An import that replaces on the natural key cannot replace a key that
changed.** Re-running after the fold wrote `youtube.com` and left the old
`m.youtube.com` row beside it — two rows, one period, both counted. The same
hole existed for any site that simply stopped appearing in history: its figure
would have persisted for ever. So `brave_history.sync` is now authoritative for
each period and clears that period's own figures before writing them again.

Both are the same shape of bug: a source's *identity* changing under a fact that
was keyed on it. Worth remembering the next time a normalisation rule is
widened — the rule change is the easy half, and the facts already written under
the old key are the half that bites.

The `m.youtube.com` row remains in `sources` with no measurements and no
captures, reachable from nothing and absent from the label queue. It is left
there: deleting rows from a live database to tidy an identity table is a worse
habit than an inert row.

### DR5.16 — A dead sync must not disable the button that restarts it

**29 September 2026.** YouTube history for the week of 20–26 September was the
only gap on the page. Jacob opened the extension, saw *"Sync running… Scrolling
history back to the last sync"*, and asked whether it was working. It was not,
and had not been for a week.

The server log told the story. On 16 and 20 September the extension fetched the
cursor and posted history. On **22 September it fetched the cursor at 11:57:13
and never posted anything**. Nothing at all since — including while the popup
said a sync was running.

`background.js` writes `status: "running"` to `chrome.storage.local` before it
starts, and only a completion or a caught failure ever changes it. A run that
simply stops — the tab switched away, the browser quit, the machine asleep —
leaves that record in place for ever. `popup.js` then rendered it on open:
"Sync running…", with `$("sync").disabled = run.status === "running"`. **The
stale record disabled the only control that could have recovered it.** A single
interrupted run locked the extension out of syncing for seven days, while
displaying a message that said the opposite.

It also read *"Last update: 22/09/2026, 11:57:13"* — `finishedAt || updatedAt`,
so an abandoned run's last heartbeat was presented as an update. That is why the
failure went unnoticed rather than being obvious the next morning.

So the popup now treats a "running" record with no heartbeat for fifteen minutes
as **interrupted**: it says so in the failed styling, says the tab has to stay
visible, says nothing was lost, and re-enables the button. A finish is labelled
"Last completed"; an abandoned run is labelled "Last activity (this run did not
complete)".

**Nothing was lost, and that is by design rather than luck.** The watermark is
the backend's — `GET /api/youtube/cursor` reads it from imported data — so it
only advances when history actually arrives. It still read `2026-09-20`
throughout, so the next successful run covers the whole gap. Had the extension
kept its own cursor, the failed run would have skipped 20–22 September
permanently.

The general shape, worth carrying: **state that gates recovery must not be
written by the thing that can die.** A status owned by the process is fine for
display; using it to disable the restart turns one transient failure into a
permanent one.

### DR5.17 — Highlight what improved, suggest what to try — but only like for like

**30 September 2026.** Until now the system only described. The summary was
told to give "no advice, no recommendations, no encouragement, no praise", and
the Overview said there was nothing to hit. Jacob asked for the opposite: point
out real improvements concretely, and offer tips for the coming weeks based on
what he actually did. That reverses a founding rule, and it is his to reverse.

**What made this hard was not the tips. It was "improvement".** Before writing
a word of praise, the two complete weeks were checked, and they had not been
measured the same way. Three distortions, all live in his data that day:

1. **A channel only one week had.** From 20 September the phone browser was
   broken into sites (DR5.13); the week before, its minutes were set aside.
   Counted time went 220 → 344: "up 56%", largely because more was seen.
2. **A reading that could not be read.** Facebook on 13–19 September was read
   as 2h 27m *or* 2h 37m and stored as unreadable. The first version of this
   module reported "Facebook rose from nothing to 56 min" — when it had fallen
   by about ninety. NULL is not zero (DR1.22), and a comparison that forgets
   it inverts the finding it is supposed to report.
3. **A cut-off Screen Time list.** Both weeks' Most Used lists stopped before
   their end (`most_used_complete` false/unknown), so an app below the cut is
   absent, and absent reads as zero.

Handled properly, the headline did not merely shrink — **it changed sign.**
Like for like, the week of 20 September was *down* 28% (181 → 131 min), where
the naive comparison said up 56%.

**So the rule: a source is compared only on channels both weeks had, and only
where both weeks actually hold a value.** `allowance.pooled_minutes` now splits
counted minutes by source and provenance and records every unmeasured reading,
so there is still one aggregator and one definition of a minute. A value is
*unknown* — never zero — when its reading was unmeasured, or when it is a phone
app absent from a Screen Time list not known to be complete. Other channels
list everything they see, so an absence there is a real zero. Anything set
aside is named on the page, by source where naming it lets him fix it
("Facebook — couldn't be read the week before" is one click from becoming his
biggest win).

**Wins** are only ever improvements: a named source at least halving and by at
least 15 min, the allowance overage falling, the reflex share falling by 15
points. An allowance win is claimed only when the later week measured at least
as much and holds no undercount of its own — less over *despite* seeing more
cannot be an artefact. Nothing is invented when nothing improved; the verdict
states direction honestly either way.

**Tips** address only sources he labelled wasteful — never undecided ones —
and name the lever that exists for the surface it was on: the queue for
YouTube and podcasts, Cold Turkey on the Mac, a Screen Time app limit on the
phone, a limit on Brave itself for phone-browser sites (per-site limits do not
reach Brave), both for X. "Halving these would have brought the week inside the
allowance" is promised only when the week is fully known; otherwise the tip
states the arithmetic. At most three of each: he said the page made him feel he
was drowning, and a list of eleven tips is the same problem wearing advice.

**The summary model gets the comparison, never the findings.** Its payload
gains a `comparison` block — direction, like-for-like before/after, the channel
names set aside and *counts* of unreadable and below-the-cut sources — and
nothing else: the wins and tips are all names and never leave the Mac (DR5.8
stands). The prompt now says to state a like-for-like fall plainly and first,
"as a fact, not a compliment", and never to present raw weekly totals as a
change in behaviour. It still gives no advice: the named tips beside it are
more precise than anything it could say without names. The privacy test that
allows only known vocabulary into the payload was extended to enumerate the two
new vocabularies (direction words, channel names), not loosened; a mutation
check confirms reintroducing the Facebook bug turns three tests red.

**Mechanics:** `progress.py` (`progress()`, `Progress`, `Finding`);
`PooledMinutes.charged_by_provenance`, `.charged_by_source`,
`.unmeasured_sources`; `SourceLine.by_provenance`; `insights.PAYLOAD_SCHEMA` 2
with `comparison`; the fallback sentences use it too; the Overview context
gains `progress`. Rendering is the UI session's.

### DR5.18 — A summary goes stale when its labels change, not only when a week ends

**30 September 2026.** Moving the summary to the top of the Overview put it
directly above the like-for-like panel, and they disagreed: "181 to 131" in the
paragraph, "177 to 119" beneath it, both claiming like for like. The UI session
checked before blaming the code, which was right — the two agree on what
comparable means. What happened was ordinary use: the paragraph was written at
08:19 and forty-three sources were bulk-labelled tool at 11:26. No data arrived.
The labels moved both ends of the comparison.

`insight_is_stale` only asked whether a newer week had finished (DR5.9). That
misses the common case: labelling is the thing Jacob does most and the thing
that moves every figure. So `insight_staleness` returns a *reason*:

* `newer_week` — a later week has finished; it outranks the other, being the
  more urgent news;
* `relabelled` — a source label or per-item override that feeds the summarised
  figures changed after the paragraph was written.

"Feeds" is deliberate: a source with a measurement on or before the last
summarised day, any podcast show (its minutes come from listening, not
measurements), or any per-item override. Labelling something first seen this
week does not stale a summary about last week. The evidence already existed —
`sources.labelled_at`, `item_labels.set_at` — so there is no new state, and a
bulk action needs no "mark stale" step: the check derives it on every render.

It only says so. Regenerating on every labelling session would be an API call
per sitting; the Rewrite button is the person's. `insight_stale` keeps its
original meaning so the sentence already on the page stays true until the
template learns to word the second reason.

### DR5.19 — The label nudge comes on Monday, and only when it is worth it

**2 October 2026.** DR5.6 set the label digest to "at most once every three
days", Jacob's own number. It kept to it — 25 Sep, 29 Sep, 2 Oct — and on
Friday 2 October it interrupted his morning to say that 50 sources, 34 minutes
in all, were unlabelled: under a minute each, across the whole diagnostic. He
asked whether that should be happening on a Friday. It was doing what it was
told; what it was told was the wrong rule.

Two changes, both his choice:

**Monday only.** A three-day clock drifts across the week and lands wherever it
lands. Monday is already when he hears from this system — the Screen Time and
YouTube reminders for the week just ended — so labelling joins that one
predictable morning.

**Only when an hour or more is undecided.** The figure is cumulative over the
diagnostic, so the bar reads "an hour of your measured time is still
undecided". Below it the undecided time cannot move the headline enough to be
worth a message.

**And it rides on the reminders rather than arriving beside them.** When import
reminders go out that Monday, the note is appended to the last one ("Also: …")
and recorded as sent, so the standalone step finds nothing to do; only on a
Monday with no reminders does it go out alone. `/labels` gets its own wording,
because a reply to a question he just asked should not talk about when the next
reminder comes.

The Qbserve warning that arrived beside it on the same Friday was right and is
unchanged: Qbserve had stopped on Wednesday afternoon while the Mac was still in
use (Cold Turkey logged 36 minutes on Thursday), so Thursday's desktop-app time
is lost — Qbserve records live and cannot fill a gap afterwards. The warning
already tells him to set it to open at login.

### DR5.20 — The brain beside the summary grows mould against your own peak week

**4 October 2026.** Jacob asked for the wordmark's brain, large, beside the
summary, growing more mould the more brain rot a week held — "full brain rot"
being his own peak week. It is the first picture on the dashboard that means
something, so three choices had to be made about what it measures.

**The peak is like for like, not the biggest raw total.** By the raw charged
total the peak is 20–26 Sep (330 min), and the brain would be fully covered
today. But 20–26 Sep is simply the first week the phone browser was measured
by site (DR5.13): a total that grows when a sensor is added would make the
first well-instrumented week the "worst" forever, and no later week could
ever look better against it. Instead each finished week is compared with the
one before exactly as the progress panel compares them (`progress._compare`,
DR5.17), and the comparisons are chained: I(w) = I(w−1) × after / before. The
peak is the highest I; the drawing is the shown week's I over it. Today that
puts the peak at 13–19 Sep and 20–26 Sep at 0.67 of it.

**A thin overlap is never chained.** What both weeks measured must hold at
least a quarter of *each* week's brain rot (`MIN_LINK_SHARE`). The progress
panel shows a thin pair once; chained, a 7-minute overlap (a week whose Screen
Time and Cold Turkey export both went missing) would multiply every later
week by 21 / 7.4 and set a peak that stood for the rest of the diagnostic.
Today's real pairs hold 36–91% of their weeks. A thin pair cuts the run, and
so does a week before with no brain rot at all; weeks before a cut are left
out and the page says so. When the shown week itself is cut, the page says
which: too little measured the same way (grey, not drawn), or nothing the
week before to scale against — and two clean weeks in a row are a clean brain.

**It describes the last finished week, never the running one** — the same
week as the progress panel beneath it, so the two can never disagree about
which week they mean.

**It is drawn in tenths, with the exact share printed beside it.** The
like-for-like figure is honest to roughly ±15 points once coverage is allowed
for, so it is drawn at the nearest tenth (seven of ten today) and the tenths
are never named. Beside it go the exact share and both weeks' minutes when the
peak is the week before, or the newest week-on-week comparisons when it is
chained. Any brain rot at all draws at least the mark's own
three specks; zero draws a clean brain; a week that couldn't be compared, or
no finished week at all, draws a grey dashed outline — absence is not zero. A
new peak is drawn full and the words say how far over it went — but only past
the progress panel's own level rule, so the lead row never announces a record
for a change the panel below calls level; a smaller rise reads "100%, a little
above it". Settling the
Facebook reading (2h 27m or 2h 37m) moves today's drawing from seven to five.

**Mould is coloured, here only.** §1 of the design doc says no good/bad colour
for consumption; this component reverses it at his request and nowhere else.
Mould starts in the mark's own speck green and greys as it ages, with a pale
frayed rim and a stain on the tissue round it, so it reads as mould and not
as the `--win` green of a week that went well. No red, no score, no named
levels, and the figures are always printed beside the drawing.

`progress.py` gains `Link`, `PeakChain`, `chain_index` and `peak_chain`,
additively: they call `_week_described`, `_load` and `_compare` and change
none of them. The session that owns `progress.py` was not running when this
landed; nothing it wrote was edited.

**"N sources set aside" counted lines, not sources** — in the progress panel
too, since DR5.17. A whole channel is one line in that list and can hold
dozens of sources, and "2 phone apps below the end of a cut-off list" is one
line for two. Both panels now say what was left out in the units it has: one
channel by name, several by count, and sources counted apart ("the phone
browser by site and 3 sources set aside").

### DR5.21 — The brain gets friendlier, then more detailed

**4 October 2026.** On the first version (DR5.20) Jacob said: "Make it a
little more visually cute/animated; less medical, the rot really looks
troublesome." Shown four directions — a face on the brain, mould critters, a
moss-and-toadstool garden, a lighter-touch polish — he chose the last:
"rounder, friendlier shapes with subtle motion and no faces".

- Mould became soft cartoon clouds that ripen to a pale mint. The spoilt-tissue
  tint, the stains round colonies, the dark spores and the turbulence filters
  are gone: they read as a lesion, which is the "medical" he meant.
- It moves a little: colonies grow in from the specks outward when the page
  loads, take three slow breaths, and a few motes drift up twice; then it is
  still. Not a loop: SVG motion is repainted on the main thread every frame,
  and an endless breath cost about half a core for as long as the Overview was
  open and visible. All of it is opt-in
  under `prefers-reduced-motion: no-preference`, on screen only; the still
  drawing is the finished one. The brain and undecided time never move.

Then: "the brain itself looks sloppy or lazily designed. I want to see more
details on the brain." The large brain was the 21px icon scaled up — an
outline, a midline and four arcs. It is now drawn in full: grooves, lobes and
soft light, built round the mark's own outline, midline and folds (now its
deepest grooves, tested against the mark's), so it is still the mark. The
21px mark and the tab icon are unchanged.

This amends DR5.20's "Mould is coloured, here only": mould no longer greys as
it ages under a frayed rim and a stain; it softens to a pale mint under a pale
fuzz. That gives up DR5.20's reason for the greying: the mint tiers and the
fuzz now sit in the same hue band as `--win` and the worthwhile series (a young
puff's #B8E5CB against `--win-bg-solid` #BFE6CE). Mould and a good week are told
apart by shape and placement, not hue — soft clouds on the brain inside #rot,
never a bar, panel or chip — accepted because the greyed, stained mould was the
"medical" look he asked to lose. The rest of DR5.20 stands: no red, no score, no named levels, and
the figures always printed beside the drawing.

### DR5.22 — Past 85% of the peak week, the mould grows mushrooms

**7 October 2026.** Jacob asked: "Once I start crossing the 85% brainrot levels
add little mushrooms appearing on the brain too, which grow and become larger
(although nothing crazy) with every percentage point I go beyond." Three
designs were drawn and judged: caramel toadstools, brain-pink toadstools and
mint ones in the mould's own green. The caramel ones won: they read as
mushrooms at 240px, and their run from 85% visibly fills in point by point.
Mint on mint vanished; pink caps with white dots read as a softened red
toadstool.

**What it draws.** From 85% of the peak week the mould fruits. Little caramel
toadstools, with a cocoa outline as fine as the mould's ink, cream spots and
stems, and light from the upper left, come up out of older colonies, the mark's
three specks first, each through a low mound of ripe mould at its foot.
A new one comes up every two to four points (85, 87, 90, 93, 96, 100, 104) and
every one already up is a little bigger at each whole point. Each grows
fastest just after it comes up, so the first is bigger at 86 than at 85, and
those already up gain at least .065 brain units of cap between them at every
point (about a pixel at 320px). Caps gain spots as they grow, two to five.

**Where it stops: 110%, the page's own new-peak line** (100 +
`progress.LEVEL_PCT`). There the page says "New peak week" and the mushrooms
are full grown; 125% or 300% draws the same seven. The drawing never claims
more than the words beside it, and a week a little over the peak (100–109%,
printed "100%, a little above it") still grows them a little. Nothing crazy:
full grown, the biggest cap is 2.4 of the brain's 17.1 units across and all
seven cover about 7% of its area, over mould already there, so the level still
reads.

**By the whole point, which DR5.20 called false precision.** The mould stays in
tenths. The mushrooms follow the point because that is what he asked for; a
point is kept to about a pixel, so a noise-sized move barely shows, and the
arrivals carry the run. The share beside the brain now rounds half up
(`whole_pct`, `drawn_step`'s own rule), so it and the mushrooms always count the
same point: 84.5% used to print "84%" beside the first mushroom. Only the share
changed; the step lines ("down 33%") keep `_pct`.

**Only on brain rot.** A mushroom stands only on a colony drawn as brain rot.
Undecided time takes the youngest colonies first and the mushrooms stand on
older ones (six of seven on colonies from the first three steps), so the two
rarely meet; when they do, that mushroom stays down and the
alt counts only those drawn.

**Said once, only while drawn.** The key beside the brain adds "From 85% of it,
mushrooms sprout too, growing a little with each point up to a new peak." and
the alt counts them ("Four little mushrooms sprout from the mould, as they do
from 85% of your peak week, a little bigger with each point."; at 110% "Seven
mushrooms sprout from the mould, fully grown."). This names one level, 85%,
his own number, and only once it is crossed; there is still no score, and the
tenths stay unnamed.

Motion as DR5.21: once the mould has grown in, the mushrooms come up one by one
from their feet (a stretch, a squash, one small wobble) and are still. Opt-in,
transform only, once; reduced motion and print draw them full grown. No red:
the caps and their outline sit at hue 25–42, tested against the danger red.

### DR5.23 — Qbserve's website pseudo-apps are measured elsewhere

**8 October 2026.** Qbserve files some websites it recognises in a browser tab
under pseudo-apps of its own: `com.qotoqot.qbserve.youtube` ("YouTube"),
`com.qotoqot.qbserve.reddit`. The Qbserve reader skips browsers because Cold
Turkey measures them per site, but these pseudo-apps are not browsers, so they
came through as desktop apps — and YouTube watched in a browser was charged
twice: once as the "YouTube" app, once from YouTube history. 155 minutes
between 29 Sep and 6 Oct; it put the week of 27 Sep at 383 minutes (23 over)
instead of 362.5 (2.5 over), up 17% instead of 13%, and 13–19 Sep over the
allowance. Reddit's pseudo-app was labelled tool, so it had not counted yet.

`drbr_types.covered_elsewhere()` now answers what `COVERED_ELSEWHERE` answered,
plus a rule: every `com.qotoqot.qbserve.*` desktop app is already measured —
YouTube by its history, any other site by the Mac's browser per site — except
Qbserve's manual-activity wrapper, which is time entered by hand. A rule, not
a list, so a site Qbserve starts recognising later is never double counted
either. Like every covered-elsewhere source, the minutes stay visible in the
"covered elsewhere" column and are never charged. Applied at read time, so
every past week is corrected without reimporting.

### DR5.24 — One Sunday check-in asks for everything, and reminds once

**8 October 2026.** Jacob said what he disliked about putting data in: "the
disparate/broken up aspect" of it. He wanted "one concrete time to enter all
inputs". He had a point. Requests reached him five ways, on different days:

* Monday import reminders that repeated daily, then silently gave up on Sunday;
* a separate label nudge (DR5.19, Monday);
* the Qbserve warning;
* the Sunday 18:00 triage post, every week, even with nothing to triage;
* readings to settle, which only the Overview mentioned.

Since 20 Sep he entered something on 11 different days. On 29 Sep four
messages arrived before 10:00. He chose **Sunday morning** for one session.

**One list, Sunday 09:30.** The daily job sends one message for the week just
finished (Sun–Sat, ending yesterday). It numbers what is still to do, always in
this order:

1. Screen Time "Last Week" screenshots, tapping Show More until it disappears;
2. the YouTube sync from the extension;
3. Cold Turkey, only when its automatic read is failing or not set up. An
   export counts as in when it arrived after the week ended and overlaps it,
   as the dashboard counts it: its period runs from the first to the last
   day with browsing, so an export of a week with a quiet Sunday starts on
   Monday;
4. readings to settle, only when there are any;
5. labelling new sources, last, because the steps before it bring new ones.

What already arrived is listed after it as "Already in". It is plain text, no
buttons, and never names the product.

**When everything is in, nothing is sent.** By 09:30 on a Sunday that can
only mean he already did the session that morning, and the bot answered each
upload at the time. A "nothing to do" message would be the filler he objected
to. The log records the decision, and the dashboard says "Everything expected
for this week has arrived". Labels alone send a message only from an hour
undecided (DR5.19's bar, kept). Below that they ride along when something else
is owed.

**One reminder, then silence.** On Monday, if evidence for that week is still
missing, one reminder lists just that and ends "That's the only reminder."
It used to say "for this week", which, sent in the new week, read as the week
now running. Nothing more is sent for the week; the dashboard keeps showing
what is missing. Labels and readings never cause a Monday message.

**A capture cut off at "Show More" is asked for once more.** Its Most Used
list stopped at "Show More" (`most_used_complete` false), so the apps below
it are missing. Last week's is asked for again, with its own wording, on
Sunday and Monday. An older week's is too, while the iPhone still has it:
Screen Time keeps about four weeks, so up to three taps of ‹ from This Week
(`SCREENTIME_WEEKS_BACK`). The step names the dates and the taps. Imports
#80 (27 Sep – 3 Oct) and #24 (13 – 19 Sep) were cut off before the check-in
existed. On Sun 11 Oct #80's week is two taps back and is asked for, in that
list and in Monday's reminder. #24's is four back, past saving, and is not.
Each cut-off week gets one check-in and its reminder, never more, like
everything else: `checklist_recapture` records which check-in asked for it.
"Already in" then says "for last week", and a reminder with only an older
week left opens "Still needed:" rather than "Last week … still needs".
A capture where the end of the list was never seen (NULL, like #49) counts as
in; the import reply already warned about it.

**State is three `meta` keys, no migration.** `checklist_week` is the week
whose Sunday list was dealt with. `checklist_nudge_week` is the week whose
reminder (or late list) was decided. `checklist_recapture` maps each cut-off
week asked for again to the check-in that asked. `prompts_sent`'s CHECK
constraint would have needed the table rebuilt for a new kind. Each key is
written only after a successful send (or when there was nothing to send), so:

* Telegram down on Sunday: Monday sends the whole list instead;
* the Mac off on Monday: the reminder goes on the first day it runs that week;
* the Mac asleep on Sunday and Monday (a long weekend away), or Telegram down
  both mornings: the whole list goes once, late, on the first day it runs
  that week, and the log says it went late. The old Monday reminders repeated
  until the evidence came; dropping the week would have been a regression;
* merged on a Friday: nothing goes out mid-week, because no week has ever
  been dealt with yet. The log says the week went unasked.

**The final week is asked for.** The old reminders stopped once the
diagnostic ended. They also never ran on a Sunday, so the last week, Sun 1 –
Sat 7 Nov, would never have been asked for. `checklist.asking_week` still asks
for a week that started on or before the last day. The end notice now waits
until that week owes nothing, or until the Tuesday after it, whichever comes
first, and never goes in the run that just sent a check-in. Owing includes an
hour of labels and readings to settle, not only evidence: the notice tells him
to judge cost per keeper, and those move it. It ends "Nothing more will be
asked for." `/labels` keeps answering while the final week is being asked for.

**The due day moved to Sunday.** DR5.10's rule is that the page says
"missing" on the same day the system asks. So `report.due_on` is now the
Sunday after the week. The Import column reads "asked for on Sunday …", and
the progress panel and the brain describe the just-finished week from Sunday,
setting aside Screen Time and YouTube like for like until they arrive. A side
benefit: imports sent on Sunday land before the 18:00 rollover, so the closed
week's overage is recorded complete the first time.

**The triage post is skipped when the queue is empty.** The rollover, lock
lift, expiry and marker still run.

**The Screen Time reply points at its readings.** At 09:30 the week's
screenshots have not arrived, so the list cannot know about their readings.
When an import leaves rows to settle, its own reply says "2 readings couldn't
be read for sure. Settle them here: …/#needs-your-eye", while he is still at
it.

Gone: `handlers_imports.send_due_prompts` and its texts, the DR5.19 label
digest, and "Nothing to triage this week". `prompts_sent` keeps its old rows
and still records the Qbserve warning and the end notice. The bot keeps the
old Screen Time reply until it is restarted.

### DR5.25 — Data first, Telegram after: an outage never stops a read

**8 October 2026.** Jacob approved "keep reading data when Telegram is
unreachable", third in his order for the Sunday session.

On 27–28 Sep the Mac had no internet. `jobs.log`, `poll.log` and `triage.log`
all end in the same traceback: `httpx.ConnectError: [Errno 8] nodename nor
servname provided`, raised from `triage.SyncBot.__init__`, which asked
Telegram who the bot was before anything else ran. So each scheduled run died
before its first data step:

* the morning job read no vault, Cold Turkey or Qbserve that day;
* the poll recorded no listening;
* Sunday's 18:00 triage never rolled the week over.

None of those steps needs Telegram. Only the messages do.

**`SyncBot` opens nothing until it sends.** Building it is free; the first
`send_message` connects. A failed connection fails that send only, and the
next send starts afresh. Closing a bot that never connected does nothing, and
closing never raises.

**Every entry point reads first and sends after.**

* **Morning job:** every read runs first (it already did). The Qbserve
  warning is now its own step after them, instead of riding inside the
  Qbserve read, so `run_sync` (and the dashboard's Sync now) sends nothing at
  all. Each send is its own step: a failure fails that step, writes no marker,
  and is retried on a later run (the Sunday list then goes on Monday, DR5.24).
* **Poll:** the bookkeeping (week, lock lift, sources), then three isolated
  steps: positions, deadline posts, alerts. A Spotify outage no longer costs
  the deadline post either.
* **Triage:** the rollover, lock lift and expiry always run, then the week is
  marked triaged, then the items are posted. An item's cycle is bumped only
  after its post went out (send-then-record, like D3.5), and the post shows
  the cycle it is about to reach. So in an outage no item ages without being
  seen: it keeps its cycle and comes back next Sunday.

**The triage marker moved before the posts.** It used to be written last, so
a run that died was retried. Bumps now happen during the posts, so a retry
after a partial failure would bump the items that did go out a second time.
With the marker first, a retry is a no-op. The price: a post lost to an outage
is not re-sent that week. `/queue` still lists the items, and they return the
next Sunday at the same cycle. If a re-send is ever wanted, the poll could
re-post when the week's posts are recorded as missing.

**A failure is loud, never silent.** `triage.try_send` logs one ERROR line
saying which message didn't go and why, with the bot token scrubbed (an
`InvalidToken` message contains it). Each run exits 1 when anything failed,
and a successful triage now logs one line too. `triage.log` has not been
written since 27 Sep, so there was no way to tell whether later Sundays ran.

**The token is scrubbed from every log line.** The morning job's and the
poll's send steps now catch a failed send and log it with its traceback, so a
revoked token would have been written in full to `jobs.log` and `poll.log`
(the old crash path leaked it the same way). Every entry point now loads its
settings first and installs a logging filter on the root handlers
(`bot.scrub_token_from_logs`, via `_silence_token_logging(token)`). It
replaces the token with `<TOKEN>` in the message, the traceback and the stack
of every record, whichever module logged it. The tracebacks stay: they are
how a failure gets diagnosed.

`bot.py` is unchanged: it is not scheduled, and launchd's KeepAlive restarts
it until the network is back.

### DR5.26 — A summary also goes stale when the week's data arrives after it

**8 October 2026.** Jacob approved "mark the summary as out of date when new
data lands after it's written", fourth in his order for the Sunday session.

Summary #8 was written by the 09:30 job on Mon 5 Oct, at 07:30:11Z, from
figures through Sat 3 Oct. The YouTube sync (import #85, watches 29 Sep – 4
Oct) landed at 07:30:59Z, 48 seconds later. For the rest of the week the
Overview kept saying the week was down 19.8% and that YouTube never arrived,
beside panels that had the YouTube minutes. Neither reason from DR5.18 fired:
no newer week had finished, and nothing had been relabelled.

So `insight_staleness` gains a third reason, `new_data`, with its own chip:
"new data has arrived since this was written". It holds when, after the
summary was written:

* an import he **sends** arrived for the summarised week, or for the week
  its headline compares it with: Screen Time, a YouTube sync, or a Cold
  Turkey export made by hand, whose period overlaps either. Summary #8 says
  "fell from 288 minutes to 230", and the 288 is the week before's figure: a
  cut-off capture of that week re-sent because the check-in asks for it
  (DR5.24) changes the sentence. A YouTube sync also counts when it is the
  first to cover the summarised week at all, whatever its watch dates,
  because then the paragraph named the gap ("never arrived") and this sync
  fills it;
* a reading for either week was settled (`detail.corrections[].at`);
* listening was heard inside the week's allowance week. This is almost never
  true, but it is right for a Rewrite pressed on a Sunday morning, before the
  week closes at 18:00;
* an automatic read caught up after a gap (below).

**"Written" means when its figures were read.** The summary's `created_at`
used to be stamped when the answer came back. The call takes seconds (5 Oct:
the last send at 09:30:06, the row at 09:30:11), and an import is stamped when
it starts, so one that started during the call was in neither the summary nor
the check. `created_at` is now stamped just before the payload is built.

**The automatic reads count only when catching up.** Cold Turkey's own
statistics, Qbserve, the browser sync and the vault re-read their last eight
days every morning. On the live data they overlapped summary #8's week on
each of the following mornings (imports #87–#93), so a plain "any import
after the summary that covers its week" rule would show the chip almost
every day, and it would mean nothing. That is the trap `insight_is_stale`'s
own note warns about (DR5.9). But a healthy read always comes before the
summary: the morning job reads first and writes after, on the Monday after
the week. So an automatic read counts only when none of its kind had arrived
between the end of the week and the summary: the read was failing (Full Disk
Access, say), the summary went without that source's last days, and this
read brings them. Once fixed, the page shows a healthy read, so nothing else
would say the paragraph is out of date. The vault never counts: its scans
have no period.

**Order:** `newer_week`, then `new_data`, then `relabelled`, and only the
first that holds is named. A finished week replaces the whole paragraph. New
data can make a sentence in it false. A relabel reclassifies figures that were
right when they were written. Checked against a copy of the live database,
summary #8 now says "new data"; summaries #5–#7 still say "a newer week".
Judged as of their own week, #5–#7 would now say "new data" rather than
"relabelled": a reading for 13 – 19 Sep, the week they compare with, was
settled on 7 Oct. The catch-up rule fires for none of #4–#8.

It is derived on every render, like DR5.18: no new state and no migration.
The chip only says so; Rewrite stays the person's choice. The dashboard needs
a restart to show it.

### DR5.27 — Qbserve is kept running, and only warns when the Mac was in use

**8 October 2026.** Jacob approved "keep Qbserve running", fifth in his order
for the Sunday session.

Qbserve stopped on Wed 30 Sep at 14:41 and recorded nothing until he reopened
it on Fri 2 Oct at 09:42. Cold Turkey saw 35.6 minutes of browsing on Thursday,
so the Mac was in use, and Thursday's desktop-app time is lost: Qbserve records
live and cannot fill a gap afterwards. The Mac had not restarted, so the old
advice, "set it to open at login", would not have helped.

**A keeper agent.** `launchd/com.jacob.drbr.qbserve.plist` runs at login and
every 10 minutes. If no process is named exactly `Qbserve`, it opens it hidden
and in the background (`open -g -j -a Qbserve`) and logs one line to
`logs/qbserve.log`. It runs only `pgrep` and `open`: no code from this
repository (so DR5.14's working-tree risk doesn't apply), and it never reads or
writes Qbserve's data. It is in `./drbr`'s agent list, so `start`, `stop` and
`status` include it. The price: quitting Qbserve on purpose is undone within 10
minutes unless the agent is stopped first, which the README and the plist say
how to do.

**The warning asks Cold Turkey whether the Mac was in use.** The old rule was a
timer: warn when Qbserve's newest record is over 36 hours old. That cannot tell
a stopped Qbserve from an idle Mac. On 29 Sep it warned about a record from Sun
27 Sep 12:12, but Cold Turkey saw nothing on the 28th, and by 10:12 that
morning Qbserve was recording again. The Mac had simply not been used.

So Cold Turkey is the witness (`report.qbserve_silence`). Qbserve's newest
record falls on day D. If Cold Turkey recorded at least 10 minutes of browsing
on this Mac on days after D, the Mac was in use and Qbserve wasn't recording.
Every Cold Turkey minute counts, whatever its label, because this is about
activity, not content. Less browsing than that is still use, so once the
record is over 36 hours old it warns too: days of desktop apps with a few
minutes of browsing are what Qbserve exists to measure, and the timer alone
used to warn about them. Only no browsing at all, an idle Mac, never warns.
Without a witness (Cold Turkey isn't read automatically, or hasn't been read
for 36 hours), the timer applies as before.

**A read that finds nothing is still a witness.** Cold Turkey's read records
no import when its eight days hold no browsing, or nothing changed since
yesterday. Back from a trip of eight days or more, the witness used to look
stale, and the timer told an idle Mac that Qbserve had stopped: the false
alarm this change set out to remove. Every successful read now stamps
`coldturkey_local_read_at` in `meta`, and freshness is judged from that (or
the newest import, for reads made before the key existed). Replayed against
copies of the live database:

* 29 Sep: no warning. The timer would have warned.
* 1 Oct: no warning. Thursday wasn't over yet.
* 2 Oct 09:30: the warning goes, as it actually did, now saying why: "Qbserve
  hasn't recorded anything since Wed 30 Sep 14:41, though this Mac has been in
  use since (Cold Turkey saw 36 min of browsing)." It ends "Open Qbserve and
  check it's recording."

The dashboard follows the same rule: the blind-spots line and the Desktop apps
row in "what needs you". An unconfigured Qbserve never warns.

**Accepted limits.** Detection is by day, so a stop is caught the morning after
the first day of use, about as early as the timer. Reading Cold Turkey's
per-minute rows would catch it a day sooner, but that means a second read of a
database that holds full URLs, and two rules. The witness sees browsers only:
Mac use with no browsing at all goes unnoticed, however long. If Qbserve is
ever uninstalled, the keeper logs one error line every 10 minutes.

The agent is not installed by this change. Merging adds the file; `./drbr
start` (or `cp` and `launchctl bootstrap`) loads it.

### DR5.28 — Audiobooks are measured, and never counted as brain rot

**8 October 2026.** Asked "Do you want Audible in at all?", Jacob answered:
"Yes, I want it; albeit not considered as brainrot". He then signed in himself
with `audible_login.py` and reported back: "Ok it says uk, 114 titles".

**What is measured.** Audible's own listening total for each day: wall-clock
minutes, across every device signed in to the account, screen on or off. The
answer lists only days with listening, so a day inside a successful read with
no entry is a real zero, and a day no successful read covered is unmeasured,
never zero. The first read reaches back to go-live; after that every morning
re-reads the last eight days, because listening done offline reaches Audible
when the phone next syncs. After a gap (the Mac asleep, Audible unreachable)
the next read starts the day after the last one covered, since Audible keeps
its history. One request covers at most 30 days; requests start 28 days apart,
because the probe proved a request returns its start day and at least the 27
after it, and never showed whether the 30th is included.

A figure that can't be used for its own day (in another unit, over 24 hours,
or listed twice with different totals) costs only that day: it gets an
explicit unmeasured row and a warning, and the rest of the read stands, as
"partial". Review found that failing the whole read instead failed every
later one too, since each reaches back to the first uncovered day, while the
words promised the next morning would work. Only an answer of a new shape (no
list of totals, an entry with no readable date) still fails whole, and its
words no longer promise that the next morning fixes it.

A finished week counts as covered only once a read reached its Saturday. An
import overlapping it is not enough: reads through Wednesday, then three
failed mornings, used to leave the week "ok" with Thursday to Saturday
unmeasured. The week now says "incomplete" until a read gets there. Cold
Turkey's and Qbserve's daily reads still count by overlap.

Screen Time saw the Audible app for 2 and 7 minutes in the weeks of 20 and 27
September; Audible counted 144 and 273 minutes. Screen Time only sees the app
while the screen is on, so `(iphone_app, "audible")` joins
`COVERED_ELSEWHERE` (DR1.9, DR5.23). Its minutes stay visible in that column.

**Which book.** Each morning's read also notes where every book in the library
is. A day's listening, which is Audible's measured total, is split across the
books whose place moved between that day's first read and the next day's first
read, in proportion to how far each moved. If no book moved in that time, the
listening must have happened that morning before the read, so the window
before that read is used instead. If no book moved there either, or the day
came before the first read, the day goes to "Audible (book not known)".

The total is measured and only the split is estimated, so these rows carry the
neutral "split estimated" chip, as the phone browser's do (DR5.13). A place
moves in book time and he listens at 1.25x, so movement is only ever a
weight: it divides the measured total and never becomes minutes itself. A book
newly finished counts from where it was to its end; the reset to the start
that Audible does after a finish is never movement, nor is any place that
went back. Later reads on the same day (Sync now) are kept but never become
boundaries, so a day's split doesn't depend on when the button was pressed.
Because a later read can move a day from "book not known" to a book, every
read owns its window and rewrites it whole (DR5.15).

Accepted limits: marking a book finished without hearing it, or a large skip
ahead, counts as movement and can take most of that day's split (the day's
total never changes). Listening before the morning read is credited to the
wrong book when a different book also moved after the read. A gap with no
reads splits every day in it by the same proportions. Titles past the first
1,000, or outside the library, can't be matched. Everything before the first
read is "book not known": the whole backfill.

**Why worthwhile, and how.** An audiobook is content, so it is not a tool
(DR5.5); by his decision it is not brain rot. Each book is a source of its own
(kind `audiobook`, key the ASIN, named by its title), labelled worthwhile the
moment it is first seen. That is recorded as his label (basis `source_label`,
and each import's `meta.standing_label` cites this decision), not as a default
guess. A label he later sets on one book wins: the standing label is written
only when the source is created (`labels.ensure_source(label_on_create=)`), and
an existing row is never touched. "Audible (book not known)" is labelled the
same way and, unlike YouTube's unknown channel, may be relabelled, because
everything in it is audiobook listening. This is the second label the system
writes by itself, after DR5.5's dashboard host, and the first he asked for as a
standing rule. The effect: worthwhile rises; wasteful, undecided, charged
minutes, the allowance and every week's overage do not move.

The same listening could reach two other channels: the web player at
`audible.co.uk` or `audible.com` in the Mac's browser (Cold Turkey), and the
iPhone app run on the Mac (Qbserve, `com.audible.iphone`). None of these is a
source yet: the only Audible source on the live database is Screen Time's app.
An earlier draft of this entry called the site "a tool by his labels", but no
such label exists, and a first visit would have arrived unlabelled, charged as
undecided beside the audiobook minutes it duplicates. All three are therefore
covered elsewhere, as Spotify's site and Mac app are. Time in the store goes
with them; shopping for a book is not content either.

**The client is unofficial.** mkb79's `audible` library (pinned at 0.12.0)
talks to Audible the way the app does. It is not an interface Amazon
supports, and it can stop working whenever Amazon changes something; the read
then fails loudly and the days stay unmeasured. Signing in registered a device
on his Amazon account: removing that device there and deleting
`data/audible_auth.json` signs it out. The file holds that device's key and
tokens. It is chmod 600, never committed (`data/` is gitignored), never
logged, and never copied into evidence; errors carry only an exception's
class name. When the library refreshes a token, the file is written back
atomically at 0600. The calls are read-only: daily stats and the library.
Audible decides where its day boundary falls. `data/audible_store.json` names
the store that holds the books (`uk`). The library's warning about its slower
crypto fallback is suppressed: it is about the library, not the figures.

**Optional, and quiet.** Without a sign-in the morning step says "not set up"
and succeeds. Audiobooks then have no coverage row, no blind spot, no line in
the Sunday check-in, and no import is owed. A failed read is recorded as a
failed import, fails its own step in the log, and adds one plain line to the
blind spots naming the days now unmeasured. Only a refused sign-in asks
anything of him: "what needs you" carries the action, and the Sunday list,
when it goes anyway, gains one un-numbered line with the exact command, `cd
~/Projects/drbr && .venv/bin/python audible_login.py --again`. It is never a
reason to send. Audible being unreachable asks nothing: the next morning
tries again.

Signed out (or signed in but never read), the record browser shows no
Audible import as missing and no 0-minute Audiobooks row in the reflex and
cost-per-keeper tables: a zero there would claim nothing was heard. Figures
already read stay. Days given whole to "book not known" carry no "split
estimated" chip, because nothing was divided. Reparsing a read is not a read:
it neither clears a refused sign-in nor freshens "Read on …"; both go by the
time Audible was actually read.

Audiobooks count in this week's pace (DR5.29) like the other daily reads. They
add nothing while worthwhile; a book he relabels wasteful counts there as it
does in the week.

**Going live.** The bot and the dashboard keep the code they started with
until restarted, and the bot's old `report_group` raises on `audiobook`: once
the first read has written rows, every Screen Time or Cold Turkey import the
old bot takes is saved but its week is not recomputed, and the reply wrongly
says nothing was recorded. So right after the merge, before Sync now or the
next 00:15 or 09:30 run, both are restarted (`launchctl kickstart -k` on
`com.jacob.drbr.dashboard` and `com.jacob.pop.bot`, README "Start
everything"), and the bot's start time is checked to be after the merge.

If he signs out, Screen Time's Audible app stays covered elsewhere. Its
minutes stay visible in that column, and the source is labelled tool anyway.

**Mechanics.** Migration `0008_audiobooks.sql` (schema_version 8) widens
`sources.kind` with `audiobook`, `measurements.provenance` with
`audible_stats` and `imports.kind` with `audible`, by the rebuild of 0007.
`drbr_types` gains the kind, the Audiobooks group (device `account`), the
provenance ("Audible's own listening stats") and the import kind.
`sources/audible.py` holds the read, the evidence document (the stats answers
verbatim, the library reduced to titles and places), the split, the import and
`reparse`. `jobs.SYNC_STEPS` ends with `audible`: last, as the one read that
needs the internet, and adding it is what arms it (DR5.14). The dashboard
shows a `listening stats` badge of its own. `DRBR_AUDIBLE_AUTH` overrides where
the sign-in is (default `data/audible_auth.json`); set but empty, it turns
audiobooks off.

**What the first read changes.** Replayed against a copy of the live database
with the probe's figures, the first read writes 703.46 minutes for 13 September
to 8 October, all "book not known": 202.56, 144.26, 273.16 and 83.48 (so far)
by week. Screen Time's Audible app (2 and 7 minutes, labelled tool) moves from
tool to covered elsewhere. Worthwhile rises by those amounts; wasteful,
undecided, charged and every overage are unchanged. Because the read fills in
weeks the Overview's summary already described, that summary counts as
written before new data (DR5.26) until it is rewritten. Later morning reads
don't.

### DR5.29 — A nightly refresh, and this week's pace against the same point last week

**8 October 2026.** Jacob asked for the figures to move during the week, not
only once it is finished: "so I can see the progress over the course of the
week", knowing some sources are weekly.

**The refresh.** `jobs.py sync` runs every automatic read and sends nothing —
no Telegram, no reminders, no summary — under a new agent,
`com.jacob.drbr.nightly`, at 00:15. Cold Turkey and Qbserve deliberately read
finished days only, so the morning job could not show a day until 09:30 the
next morning; just after midnight is the first moment the day just ended is
whole. launchd runs a missed calendar job at the next wake, so on a Mac asleep
overnight it runs on waking; the reads are idempotent, so running twice before
09:30 costs nothing. It runs the working tree like the morning job (DR5.14).

**The pace.** `pace.pace()` compares this week's finished days — Sunday to
yesterday — with the same weekdays last week, under the same rule as the
weekly comparison: only what was measured the same way in both spans.

* **Daily channels only.** The Mac's browser (Cold Turkey) and Mac apps
  (Qbserve) read every finished day; podcast listening is polled. YouTube
  history is recorded per watch day but arrives only when the extension is
  run, so it counts once a sync has happened since the span ended, and is
  otherwise named with "run the extension to include it". Screen Time and the
  phone browser arrive as one figure per week; they cannot be cut at Tuesday,
  so they are named as joining once the week is in.
* **A channel is counted only if every day of both spans was read**, and not
  if any reading in them could not be measured. Left out, it is named with its
  reason rather than read as zero.
* **Never today**, for the same reason the readers skip it, and nothing on a
  Sunday, when no day of the week has finished.

It is computed per local day with the same rules as `pooled_minutes` —
covered-elsewhere, tool and pre-go-live excluded, undecided charged — and a
test pins a Sunday-to-Friday span to the week's own figure.

**The first draft double-counted, and the numbers said so.** On real data it
reported "up 208%": 150 minutes against 49. Breaking it down by day put 95
minutes on Tuesday in a "YouTube" desktop app — Qbserve's pseudo-app for
YouTube in a browser tab, already counted by YouTube history. DR5.23 had fixed
exactly this, with `T.covered_elsewhere()`; `pace.py` had used the old
exact-key dictionary. With the rule, the week read 39 against 30 — level. A
test now fails if the dictionary lookup comes back. The weekly totals were never
affected.

Residual, stated: a day Qbserve was quit part-way through still counts as read,
because its read of that day succeeded. The keeper (DR5.27) is what prevents it.

**Mechanics:** `jobs.main` gains `sync`; `launchd/com.jacob.drbr.nightly.plist`;
`./drbr`'s AGENTS list and the README's agent table; `pace.py` (`Pace`,
`pace()`); `pace` is the last argument of the Overview's render call.
Rendering it in the week-summary strip is the UI session's.
