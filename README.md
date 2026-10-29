# Dr. Brainrot

Dr. Brainrot is a single-user Telegram bot that saves Spotify podcast episodes after you explain why you want to hear them. It holds episodes for a configurable lock period, lets you choose what to hear against a weekly time allowance and expires items you leave in the queue. It checks Spotify playback positions to estimate listening and reports how many saved episodes survive the lock, get selected and get played.

## Setup

### 1. Dependencies

Use Python 3.12. Run these commands from the repository directory:

```bash
python3.12 -m venv .venv
.venv/bin/pip install "python-telegram-bot>=21,<23" httpx pytest pytest-asyncio
```

### 2. Credentials and settings

Copy `.env.example` to `.env`, then fill in the existing keys. Keep their names
unchanged. The example includes the credentials, timezone and database path.
The default lock is 48 hours, the weekly allowance is 180 minutes and queued
items have a lifespan of two triage cycles.

```bash
cp .env.example .env
chmod 600 .env
```

`.env` is gitignored.

**Spotify:** create an app at https://developer.spotify.com/dashboard. Register
`http://127.0.0.1:8888/callback` as its redirect URI to match `.env.example`.
Copy the client ID and secret into `.env`.

**Telegram:** send `/newbot` to @BotFather and copy the token into `.env`.
Set `TELEGRAM_USER_ID` to your numeric user ID. The bot silently ignores other
users. Press **Start** in the bot chat so it can send you scheduled messages.

### 3. One-time Spotify consent

Run this on a machine with a browser. If you run the bot elsewhere, transfer
`.env` securely to that machine after consent:

```bash
.venv/bin/python auth_spotify.py
```

Grant **View your listening position in podcasts**, the
`user-read-playback-position` scope. The consent script refuses to save a token
without it. The Spotify client also rejects missing playback-position data
rather than reporting it as zero listening.

### 4. Verify

```bash
.venv/bin/python check_env.py
```

This checks configuration values, `.env` permissions, secret-file exclusions
and live Spotify and Telegram authentication. Exit code 0 means the checks
passed. Add `--ping` to send yourself a test message.

## Running it

Start the bot with long polling. It does not need an inbound port or webhook:

```bash
.venv/bin/python bot.py
```

Send a Spotify episode link, then reply with your reason. Nothing is saved
until that reply arrives. A trailing deadline such as `by friday` or
`before 3 oct` is optional. A deadline inside the lock period waives the lock.

Use `/queue` to see unlocked items and `/stats` to see the capture, lock,
selection and listening figures. Weekly triage offers **Promote**, **Drop**
and **Still holds?** buttons. Promoting an episode reserves its remaining
minutes against the weekly allowance.

| Command | When | What it does |
|---|---|---|
| `.venv/bin/python bot.py` | Always running | Handles capture, triage buttons, `/start`, `/queue` and `/stats` |
| `.venv/bin/python poll.py` | Daily | Reads playback positions, records positive changes, lifts expired locks, surfaces upcoming deadlines and sends allowance alerts |
| `.venv/bin/python triage.py` | Sunday at 18:00 | Closes the previous week, carries listening overage into the next allowance, lifts locks, expires old items and posts the queue |

## Scheduling

Schedule `poll.py` daily and `triage.py` on Sunday at 18:00 in the configured
application timezone. Keep the bot running separately.

The scheduler and Dr. Brainrot must agree on the timezone. The timezone key in
`.env.example` is set to `Europe/Warsaw`. The application ignores the POSIX
`TZ` variable. Running triage before its configured week boundary can close
the wrong week.

### macOS with launchd

Use launchd on a Mac that sleeps. Calendar jobs can run after a missed time
when the Mac wakes; macOS cron does not catch up in the same way.

The three agent definitions in `launchd/` have these schedules:

| Entry point | Schedule |
|---|---|
| `bot.py` | Always running with `KeepAlive` |
| `poll.py` | Daily at 09:17 |
| `triage.py` | Sunday at 18:00 |

The supplied definitions contain machine-specific paths. Copy them first,
then check the interpreter, working directory and log paths in the installed
copies before loading them. Create the log directory if needed. Keep the
existing filenames and labels.

```bash
mkdir -p "$HOME/Library/LaunchAgents" logs
cp launchd/*.plist "$HOME/Library/LaunchAgents/"
```

After checking those paths, load the copied agents and inspect their status:

```bash
for plist in launchd/*.plist; do
  installed="$HOME/Library/LaunchAgents/$(basename "$plist")"
  launchctl bootstrap "gui/$(id -u)" "$installed"
  label=$(/usr/libexec/PlistBuddy -c 'Print :Label' "$installed")
  launchctl print "gui/$(id -u)/$label"
done
```

Run the daily poll immediately using its existing label:

```bash
label=$(/usr/libexec/PlistBuddy -c 'Print :Label' launchd/*poll.plist)
launchctl kickstart -p "gui/$(id -u)/$label"
```

Stop the weekly agent using its existing label:

```bash
label=$(/usr/libexec/PlistBuddy -c 'Print :Label' launchd/*triage.plist)
launchctl bootout "gui/$(id -u)/$label"
```

Stop any manually started bot before loading its agent. Two bot processes
using the same token conflict over Telegram updates.

### An always-on Linux host with cron

Use a cron implementation that supports `CRON_TZ`. Replace
`/path/to/checkout` with your actual repository directory and create its
`logs/` directory before installing these entries with `crontab -e`:

```cron
# Match the application timezone configured in .env.
CRON_TZ=Europe/Warsaw

# Daily playback check and alerts.
17 9 * * * cd /path/to/checkout && .venv/bin/python poll.py >> logs/poll.log 2>&1

# Weekly triage at the Sunday boundary.
0 18 * * 0 cd /path/to/checkout && .venv/bin/python triage.py >> logs/triage.log 2>&1
```

`CRON_TZ` controls the scheduler. It does not set the application's timezone.
Configure both explicitly rather than relying on the login shell.

## Tests

```bash
.venv/bin/pytest -q
```

The suite uses fake Spotify and Telegram clients. `conftest.py` blocks network
access and reads of the real `.env`. The consent and credential-check scripts
make live calls when run separately, as do the bot and scheduled jobs during
normal use.

## Files

| File or directory | Purpose |
|---|---|
| `bot.py` | Telegram capture, queue view, triage buttons and statistics |
| `spotify.py` | Token refresh, episode lookup and playback-position changes |
| `poll.py` | Daily listening checks, deadline messages and allowance alerts |
| `triage.py` | Weekly rollover, expiry, queue messages and statistics formatting |
| `db.py` | Queue state, listening records, weekly accounting and funnel statistics |
| `config.py` | Loads `.env` and configuration values |
| `clock.py` | UTC storage, local week boundaries and daylight-saving handling |
| `schema.sql` | SQLite schema |
| `auth_spotify.py` | Browser consent for Spotify |
| `check_env.py` | Configuration and credential checks |
| `launchd/` | macOS agent definitions |
| `conftest.py` | Test guards against network access and real credentials |
| `tests/` | Automated tests |
| `data/` | Gitignored local data |

`CONTRACTS.md` describes the module interfaces. `DECISIONS.md` records design
choices. `REVIEW.md` records the implementation audit.

## Known distortions

- Moving forward in an episode counts as listening even if you skipped it.
- Playback positions measure progress through the episode, not elapsed
  listening time. At speeds above 1x, recorded progress can exceed time spent
  listening.
- Moving backwards does not subtract recorded minutes. Listening between
  polls can be missed if the position moves backwards before the next check.
- Completion uses Spotify's `fully_played` flag rather than comparing the
  position with the episode's duration.
- Only captured episodes in locked, queued or promoted states are polled.
  Listening to other episodes is invisible. Dr. Brainrot does not scan your
  full listening history or block playback in Spotify.
