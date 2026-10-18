# POP

A queue for podcasts you meant to listen to. Send a Spotify episode to a Telegram bot, write one line about why, and it sits locked. Once a week you triage what is left against a time allowance. A daily poll reads Spotify's resume position and records what actually got played.

Personal tool. One user. Telegram is the whole interface.

The output is the decay curve:

1. captured → survived the lock (how much of the impulse was momentary)
2. survived → promoted at triage (how much survives a calm reading)
3. promoted → actually played (how much was wanted versus imagined)

The program is POP. This repository is `dr-brainrot`.

## Stack

Python 3.12, the version named in `pop-build-brief.md`. Nothing in the repo pins it.

- `python-telegram-bot` — Telegram Bot API, imported as `telegram` in `bot.py` and `triage.py`. Long polling.
- `httpx` — Spotify HTTP. `spotify.py` is the only module that performs it.
- SQLite via the stdlib. `db.connect()` opens `POP_DB_PATH` and `db.init_db()` applies `schema.sql`.
- `pytest` — the suite in `tests/`.

`auth_spotify.py` and `check_env.py` use the stdlib only. There is no `requirements` file. The interface is the bot; persistence is one SQLite file.

## Layout

```
bot.py              capture, /queue, /stats, triage buttons
poll.py             daily job: resume deltas, lock lifts, deadline posts, alerts
triage.py           Sunday job: week rollover, expiry, the queue post, /stats text
db.py               state machine, allowance gate, soft delete, funnel stats
spotify.py          episode links, refresh-token auth, resume_point, the clamp
config.py           .env loading
clock.py            UTC storage and the Sunday 18:00 week boundary
schema.sql
auth_spotify.py     one-time browser consent; writes the refresh token into .env
check_env.py        shape, file mode, and live Spotify and Telegram checks
conftest.py         blocks network and real-.env reads for every test
tests/
launchd/            macOS agents for the bot, the daily poll, and Sunday triage
data/               SQLite database; gitignored
logs/               runtime logs; gitignored
```

`CONTRACTS.md` is the interface between these modules. `DECISIONS.md` records choices the brief left open. `REVIEW.md` is the audit against the brief. `pop-build-brief.md` is the spec.

`com.jacob.pop.build.plist` and `run_build.sh` are the one-shot unattended build. `.build-complete` makes `run_build.sh` exit. The running bot is the three agents in `launchd/`. `bot-logo.png` sits in the repo root; no module loads it.

## Setup

Python 3.12. A Spotify app, and a Telegram bot from @BotFather.

```bash
python3.12 -m venv .venv
.venv/bin/pip install python-telegram-bot httpx pytest
```

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored. `check_env.py` requires mode `0600`:

```bash
cp .env.example .env
chmod 600 .env
```

**Spotify.** Create an app at <https://developer.spotify.com/dashboard> and register the redirect URI written in `.env.example`. `auth_spotify.py` reads `SPOTIFY_REDIRECT_URI`; if it is empty, the script uses `http://127.0.0.1:8888/callback`. Put the client id and secret in `.env`.

**Telegram.** `/newbot` to @BotFather, then put the token in `.env`. Put your numeric user id in `TELEGRAM_USER_ID`. Every other sender is dropped with no reply. Press Start in the bot chat once. A bot cannot open a conversation on its own, so without that it cannot deliver the Sunday triage post or an allowance alert.

On a machine with a browser:

```bash
python3 auth_spotify.py
```

That runs Spotify's Authorization Code flow and writes `SPOTIFY_REFRESH_TOKEN` into `.env`. It refuses to store a token that lacks the scope `user-read-playback-position`. Without that scope every `resume_point` is null and listening time reads as zero. Later runs use the refresh token; the browser step is once.

```bash
python3 check_env.py
python3 check_env.py --ping
```

Exit 0 means every required variable is present and shaped correctly, `.env` is `0600`, `.gitignore` covers `.env`, `data/`, `__pycache__/`, and `*.session`, and both credentials authenticate. `--ping` sends one message to `TELEGRAM_USER_ID`. A 403 on that send means Start has not been pressed.

## Configuration

`config.load()` reads `.env` at the repo root, then lets the process environment override the names in `config._KEYS`. The template is `.env.example`. Code defaults below apply only when the variable is absent.

| Name | Read by | Purpose |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | `config.py`, `auth_spotify.py`, `check_env.py` | Spotify app client id |
| `SPOTIFY_CLIENT_SECRET` | `config.py`, `auth_spotify.py`, `check_env.py` | Spotify app secret |
| `SPOTIFY_REFRESH_TOKEN` | `config.py`, `check_env.py`; written by `auth_spotify.py` | refresh token for later runs |
| `SPOTIFY_REDIRECT_URI` | `auth_spotify.py`, `check_env.py` | loopback callback for the one-time consent. Stays out of `Settings` |
| `TELEGRAM_BOT_TOKEN` | `config.py`, `check_env.py` | Bot API token |
| `TELEGRAM_USER_ID` | `config.py`, `check_env.py` | the one user id the bot answers |
| `TELEGRAM_BOT_USERNAME` | `check_env.py` only, optional | when set, must match the username `getMe` returns |
| `WEEKLY_ALLOWANCE_MIN` | `config.py` | minutes that may be promoted in a week. Default 180 |
| `POP_TZ` | `config.py` | zone of the Sunday 18:00 week boundary. Default `Europe/Lisbon` |
| `LOCK_HOURS` | `config.py` | how long a capture stays locked. Default 48 |
| `LIFESPAN_CYCLES` | `config.py` | triage cycles before expiry. Default 2 |
| `POP_DB_PATH` | `config.py` | SQLite path. Default `data/pop.db`, resolved from the repo root |

`.env.example` sets `POP_TZ` to `Europe/Warsaw`. The fallback inside `config.load()`, and the comments in `clock.py`, still say `Europe/Lisbon`.

`TZ` is refused. It is absent from `config._KEYS`, so a cron or launchd environment exporting `TZ` cannot move the week boundary.

## Running

The launchd plists call `.venv/bin/python`. From a shell:

```bash
.venv/bin/python bot.py
```

`bot.py` long-polls. It opens no inbound port.

In the chat:

- Send `https://open.spotify.com/episode/<id>` or `spotify:episode:<id>` (`play.spotify.com` is accepted too). The bot replies with title and duration and asks why. The row is created when the reply arrives. A trailing `by <date>` or `before <date>` — ISO date, weekday, today, tomorrow, or a day and month — becomes a deadline at 18:00 in `POP_TZ`. Text it does not recognise stays in the note.
- `/start` explains the lock and the allowance.
- `/queue` lists unlocked items. Read only.
- `/stats` prints the funnel for all time and the last 8 weeks, plus median days to drop and the still-holds yes rate.
- Sunday triage, and any queued item whose deadline falls before the next Sunday, arrive with Promote, Drop, and Still holds? Yes / No.

States are `locked`, `queued`, `promoted`, `dropped`, `expired`, and `played`. `dropped` and `expired` remain in the database and stay out of the normal lists. A deadline inside the lock starts the item as `queued`. Promote charges the episode's remaining minutes against this week's allowance after debt, and refuses the click when that would go over. Unplayed promotions return to the queue at the next week boundary. With the default lifespan, the second triage warns and the third removes the item. A deadline item stays until the deadline passes.

Two other processes, meant to be scheduled:

```bash
.venv/bin/python poll.py
.venv/bin/python triage.py
```

`poll.py` reads `resume_point`, writes a clamped delta (a backward move credits zero minutes), lifts locks whose time is up, posts queued items whose deadline falls before the next triage, and sends at most one approaching alert and one exceeded alert per week. Approaching is 80% of the effective allowance. Listening past the allowance becomes debt on the next week, capped at one week's allowance.

`triage.py` closes the week, lifts locks, expires what is due, and posts the queue. Positions stay in `poll.py`; the Sunday post still goes out if Spotify is down. A second run in the same week sees `last_triage_week` in `meta` and returns.

Telegram allows one `getUpdates` poller per token. Stop a manually started `bot.py` before starting another, or both fail with a getUpdates conflict.

## Tests

```bash
.venv/bin/pytest -q
```

488 tests. `conftest.py` makes name resolution, socket connects, both httpx transports, and any read of the real `.env` raise. Spotify and Telegram are faked. `auth_spotify.py` and `check_env.py` are the live calls, and the suite does not run them.

## Scheduling

The week boundary is Sunday 18:00 in `POP_TZ`, stored as UTC (`clock.py`). launchd fires in the Mac's system local time, so the triage hour and `POP_TZ` have to be the same moment. Firing early closes the wrong week in `roll_over_week()`.

The agents in `launchd/` hardcode `/Users/Jacob/Projects/POP`:

| job | schedule | program |
|---|---|---|
| `com.jacob.pop.bot` | always on, `KeepAlive`, restart throttle 30s | `bot.py` |
| `com.jacob.pop.poll` | every day at 09:17 | `poll.py` |
| `com.jacob.pop.triage` | Sunday 18:00 (`Weekday` 0) | `triage.py` |

Logs are `logs/bot.log`, `logs/poll.log`, and `logs/triage.log` under that path. The poll and triage plist comments say the system zone and `POP_TZ` are both `Europe/Warsaw`. That is the change recorded in `DECISIONS.md` D5.11: the plist hour moved from 19 to 18 so launchd lands on POP's own boundary. `com.jacob.pop.build.plist` and `run_build.sh` still say the machine zone is `Europe/Lisbon`. D5.9 says that statement was wrong.

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
for j in bot poll triage; do
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jacob.pop.$j.plist
done
```

Run one immediately: `launchctl kickstart -p gui/$(id -u)/com.jacob.pop.poll`. Stop one: `launchctl bootout gui/$(id -u)/com.jacob.pop.triage`.

`pop-build-brief.md` says the other host is a Hetzner VPS, and D5.9 says cron belongs there because that machine does not sleep. The repo has no crontab, systemd unit, Dockerfile, or deploy script, and it records no install path. The two jobs are `poll.py` once a day and `triage.py` at Sunday 18:00 in `POP_TZ`. `CRON_TZ` would decide when the daemon fires; `POP_TZ` is what POP believes. D5.6 says the VPS supervisor has to keep a single `bot.py`.

The brief also says that VPS runs Pulse, which uses Telethon. POP uses the Bot API and its own token. `.gitignore` ignores `*.session` so Pulse's session files stay out of this repo.

## Known limits

The brief lists these and tells the build to accept them. The code does:

- Scrubbing forward counts as listening, because the position moved.
- Playback above 1x undercounts real minutes against elapsed time.
- Completion is Spotify's `fully_played` flag, not a position past the duration. Trailing credits mean episodes rarely reach the end.
- Playback that never entered the queue is invisible. The brief's v2 sweep over followed shows is unbuilt. Implausibly low numbers are the signal that listening is happening outside the queue.

Also stated in the tree:

- `db.migrate()` returns immediately while `schema_version` is 1. Added tables arrive through `CREATE TABLE IF NOT EXISTS` in `init_db`. `REVIEW.md` notes that an added column would need a real migration.
- Deadline lines in the triage post and the mid-week deadline notice call `clock.fmt_local` with no zone argument, so they render in that function's default, `Europe/Lisbon`. Capture confirmations and `/queue` pass `POP_TZ`.
- Spotify's batch `GET /episodes?ids=` returns 403 for this app's token. `get_episodes()` falls back to one request per episode (`DECISIONS.md` D5.8).
