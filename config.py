"""Configuration, loaded from .env once.

Phase 1 contract module. Owned by no Phase 2 agent; imported by all of them.
Stdlib only — no python-dotenv dependency for something this small.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"


def _parse_env(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass(frozen=True)
class Settings:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_refresh_token: str
    telegram_bot_token: str
    telegram_user_id: int
    weekly_allowance_min: int
    tz: str
    lock_hours: int
    lifespan_cycles: int
    db_path: pathlib.Path
    # --- DRBR (Part II). Defaults keep every POP call site and test valid. ---
    youtube_api_key: str = ""
    vault_path: pathlib.Path | None = None
    vault_inbox: str = "01-Inbox"
    capture_outbox: pathlib.Path | None = None
    raw_dir: pathlib.Path = pathlib.Path(":memory-raw:")
    diagnostic_weeks: int = 8
    dashboard_port: int = 8750
    ingest_token: str = ""
    coldturkey_db: pathlib.Path | None = None   # Cold Turkey's local browser statistics; None = manual export only
    qbserve_db: pathlib.Path | None = None      # Qbserve's database (desktop app time, DR5.3); None = not measured
    go_live: str = ""              # local date DRBR measurement starts; earlier data is not counted
    #: Optional. Only the weekly insights summary uses it (DR5.8); empty means the
    #: Overview shows DRBR's own computed sentences instead. Never logged, never stored.
    anthropic_api_key: str = ""

    @property
    def lock_seconds(self) -> int:
        return self.lock_hours * 3600


def load(env_path: pathlib.Path | None = None, **overrides) -> Settings:
    """Build Settings from .env, then os.environ, then explicit overrides.

    Tests pass overrides directly and never touch the real .env.
    """
    raw = dict(_parse_env(env_path or ENV_PATH))
    raw.update({k: v for k, v in os.environ.items() if k in _KEYS})

    def get(key: str, default: str = "") -> str:
        return str(overrides.get(key.lower(), raw.get(key, default)))

    db = get("POP_DB_PATH", "data/pop.db")
    db_path = pathlib.Path(db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    return Settings(
        spotify_client_id=get("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=get("SPOTIFY_CLIENT_SECRET"),
        spotify_refresh_token=get("SPOTIFY_REFRESH_TOKEN"),
        telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
        telegram_user_id=int(get("TELEGRAM_USER_ID", "0") or 0),
        weekly_allowance_min=int(get("WEEKLY_ALLOWANCE_MIN", "180")),
        # POP_TZ, never TZ. `TZ` is the POSIX *system* timezone variable, so a
        # cron or systemd unit exporting `TZ=UTC` would silently move the
        # triage boundary off Lisbon local time — the exact drift D1.3 exists
        # to prevent, arriving through config instead of the arithmetic. It is
        # absent from `_KEYS` as well, so it cannot reach Settings at all.
        tz=get("POP_TZ", "Europe/Lisbon"),
        lock_hours=int(get("LOCK_HOURS", "48")),
        lifespan_cycles=int(get("LIFESPAN_CYCLES", "2")),
        db_path=db_path,
        youtube_api_key=get("YOUTUBE_API_KEY"),
        vault_path=_path_or_none(get("DRBR_VAULT_PATH")),
        vault_inbox=get("DRBR_VAULT_INBOX", "01-Inbox"),
        capture_outbox=_resolve(get("DRBR_CAPTURE_OUTBOX", "data/vault_captures.jsonl")),
        raw_dir=_resolve(get("DRBR_RAW_DIR", "data/raw")),
        diagnostic_weeks=int(get("DRBR_DIAGNOSTIC_WEEKS", "8")),
        dashboard_port=int(get("DRBR_DASHBOARD_PORT", "8750")),
        ingest_token=get("DRBR_INGEST_TOKEN"),
        go_live=get("DRBR_GO_LIVE"),
        coldturkey_db=_path_or_none(get("DRBR_COLDTURKEY_DB")),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        qbserve_db=_path_or_none(get("DRBR_QBSERVE_DB")),
    )


def _resolve(value: str) -> pathlib.Path:
    """Relative paths are relative to the repo root, as POP_DB_PATH is."""
    path = pathlib.Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _path_or_none(value: str) -> pathlib.Path | None:
    return pathlib.Path(value).expanduser() if value else None


#: Environment variables allowed to override `.env`. Deliberately does **not**
#: contain `TZ`: that name belongs to POSIX, not to this application, and any
#: ambient `TZ` must be ignored here (D5.2). POP's own setting is `POP_TZ`.
_KEYS = {
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID", "WEEKLY_ALLOWANCE_MIN",
    "POP_TZ", "LOCK_HOURS", "LIFESPAN_CYCLES", "POP_DB_PATH",
    "YOUTUBE_API_KEY", "DRBR_VAULT_PATH", "DRBR_VAULT_INBOX", "DRBR_CAPTURE_OUTBOX",
    "DRBR_RAW_DIR", "DRBR_DIAGNOSTIC_WEEKS", "DRBR_DASHBOARD_PORT", "DRBR_INGEST_TOKEN",
    "DRBR_GO_LIVE", "DRBR_COLDTURKEY_DB", "DRBR_QBSERVE_DB", "ANTHROPIC_API_KEY",
}


def test_settings(**overrides) -> Settings:
    """Settings with dummy credentials, for tests. Never reads .env."""
    base = dict(
        spotify_client_id="test-client-id",
        spotify_client_secret="test-client-secret",
        spotify_refresh_token="test-refresh-token",
        telegram_bot_token="0000000000:TEST-TOKEN-NOT-REAL-DO-NOT-USE-EVER",
        telegram_user_id=1,
        weekly_allowance_min=180,
        tz="Europe/Lisbon",
        lock_hours=48,
        lifespan_cycles=2,
        db_path=pathlib.Path(":memory:"),
        youtube_api_key="test-youtube-key",
        ingest_token="test-ingest-token",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]
