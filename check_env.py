#!/usr/bin/env python3
"""Validates .env is complete and every external dependency dr-brainrot needs works.

    .venv/bin/python check_env.py           # validate + live auth checks
    .venv/bin/python check_env.py --ping    # also send a test message to TELEGRAM_USER_ID

Exit 0 = every check passed.
Exit 1 = at least one check FAILED (bad or missing credential, missing path).
Exit 3 = nothing failed, but a provider was UNAVAILABLE (network down, 5xx,
         rate limit) so a credential could not be proven either way.
Secrets are never printed; only lengths and prefixes.

Run with the project venv: the Apple Vision check imports pyobjc from it.
"""
from __future__ import annotations

import base64
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = pathlib.Path(__file__).with_name(".env")
REQUIRED_SCOPE = "user-read-playback-position"

REQUIRED = {
    "SPOTIFY_CLIENT_ID":      r"[0-9a-f]{32}",
    "SPOTIFY_CLIENT_SECRET":  r"[0-9a-f]{32}",
    "SPOTIFY_REFRESH_TOKEN":  r"\S{20,}",
    "SPOTIFY_REDIRECT_URI":   r"https?://\S+",
    "TELEGRAM_BOT_TOKEN":     r"\d{6,12}:[A-Za-z0-9_-]{30,}",
    "TELEGRAM_USER_ID":       r"\d{5,15}",
    "WEEKLY_ALLOWANCE_MIN":   r"\d+",
    "DR_BRAINROT_TZ":                 r"\S+/\S+",
    "LOCK_HOURS":             r"\d+",
    "LIFESPAN_CYCLES":        r"\d+",
    "DR_BRAINROT_DB_PATH":            r"\S+",
    "YOUTUBE_API_KEY":        r"AIza[0-9A-Za-z_-]{35}",
    "DR_BRAINROT_VAULT_PATH":        r"/\S.*",
    "DR_BRAINROT_VAULT_INBOX":       r"[^/].*",
    "DR_BRAINROT_CAPTURE_OUTBOX":    r"\S+",
    "DR_BRAINROT_RAW_DIR":           r"\S+",
    "DR_BRAINROT_DIAGNOSTIC_WEEKS":  r"[1-9]\d*",
    "DR_BRAINROT_DASHBOARD_PORT":    r"\d{4,5}",
    "DR_BRAINROT_GO_LIVE":           r"\d{4}-\d{2}-\d{2}",
    "DR_BRAINROT_INGEST_TOKEN":      r"[A-Za-z0-9_-]{32,}",
}
SECRET_KEYS = {
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REFRESH_TOKEN", "TELEGRAM_BOT_TOKEN",
    "YOUTUBE_API_KEY", "DR_BRAINROT_INGEST_TOKEN", "ANTHROPIC_API_KEY",
}

#: Deliberately not in REQUIRED: the weekly summary (DR5.8) is the only thing
#: that uses it, and without it the Overview falls back to dr-brainrot's own computed
#: sentences. Missing is a fine state, not a failure.
ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
ANTHROPIC_SHAPE = r"sk-ant-[A-Za-z0-9_-]{20,}"

STEPS = 10

failures: list[str] = []
unavailable_list: list[str] = []


def mask(key: str, val: str) -> str:
    return f"{val[:4]}…[{len(val)}]" if key in SECRET_KEYS else val


def ok(msg: str) -> None:
    print(f"  OK    {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def unavailable(msg: str) -> None:
    """The provider could not be reached; the credential is unproven, not bad."""
    print(f"  UNAVAILABLE  {msg}")
    unavailable_list.append(msg)


def http_problem(what: str, code: int, body: str) -> None:
    """Classify an HTTP error: 5xx and 429 are the provider, anything else is us."""
    if code >= 500 or code == 429:
        unavailable(f"{what} HTTP {code} (provider side): {body[:200]}")
    else:
        fail(f"{what} HTTP {code}: {body[:300]}")


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        print(f"FAIL  {ENV_PATH} does not exist.")
        raise SystemExit(2)
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def check_presence(env: dict[str, str]) -> None:
    print(f"\n[1/{STEPS}] .env completeness and shape")
    for key, pattern in REQUIRED.items():
        val = env.get(key, "")
        if not val:
            fail(f"{key} is missing or empty")
        elif not re.fullmatch(pattern, val):
            fail(f"{key} does not match expected shape /{pattern}/ (got {mask(key, val)})")
        else:
            ok(f"{key} = {mask(key, val)}")


def check_permissions() -> None:
    print(f"\n[2/{STEPS}] file hygiene")
    mode = ENV_PATH.stat().st_mode & 0o777
    (ok if mode == 0o600 else fail)(f".env mode is {oct(mode)}" + ("" if mode == 0o600 else " (want 0o600)"))
    gi = ENV_PATH.with_name(".gitignore")
    if not gi.exists():
        fail(".gitignore missing")
        return
    body = gi.read_text()
    for pat in (".env", "data/", "__pycache__/", "*.session", "inputs/"):
        (ok if pat in body else fail)(f".gitignore covers {pat}")


def check_telegram(env: dict[str, str], ping: bool) -> None:
    print(f"\n[3/{STEPS}] Telegram — live getMe")
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        fail("no token to test")
        return
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=20
        ) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        http_problem("getMe", e.code, e.read().decode(errors="replace").replace(token, "<TOKEN>"))
        return
    except urllib.error.URLError as e:
        unavailable(f"getMe unreachable: {e.reason}")
        return
    if not data.get("ok"):
        fail(f"getMe returned ok=false: {data}")
        return
    me = data["result"]
    ok(f"authenticated as @{me.get('username')} (id {me.get('id')})")
    declared = env.get("TELEGRAM_BOT_USERNAME")
    if declared and declared != me.get("username"):
        fail(f"TELEGRAM_BOT_USERNAME={declared} but token belongs to @{me.get('username')}")

    if ping:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=urllib.parse.urlencode({
                    "chat_id": env["TELEGRAM_USER_ID"],
                    "text": "check_env.py ping. Credentials work. Nothing else is running yet.",
                }).encode(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                pong = json.load(r)
            if pong.get("ok"):
                ok(f"test message delivered to {env['TELEGRAM_USER_ID']}")
            else:
                fail(f"sendMessage returned: {pong}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            fail(f"sendMessage HTTP {e.code}: {body[:200]} "
                 "(if 403, press Start in the bot chat first)")


def check_spotify(env: dict[str, str]) -> None:
    print(f"\n[4/{STEPS}] Spotify — live refresh-token exchange")
    cid, csec = env.get("SPOTIFY_CLIENT_ID", ""), env.get("SPOTIFY_CLIENT_SECRET", "")
    refresh = env.get("SPOTIFY_REFRESH_TOKEN", "")
    if not (cid and csec and refresh):
        fail("client id/secret/refresh token incomplete — run auth_spotify.py")
        return
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh,
        }).encode(),
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        http_problem("refresh", e.code, e.read().decode(errors="replace"))
        return
    except urllib.error.URLError as e:
        unavailable(f"Spotify unreachable: {e.reason}")
        return

    tok = payload.get("access_token")
    if not tok:
        fail(f"no access_token returned; keys={sorted(payload)}")
        return
    ok(f"access token minted ({len(tok)} chars, expires_in={payload.get('expires_in')}s)")

    granted = payload.get("scope", "").split()
    if REQUIRED_SCOPE in granted:
        ok(f"scope '{REQUIRED_SCOPE}' present — resume_point is readable")
    else:
        fail(f"scope '{REQUIRED_SCOPE}' NOT granted (got {granted}); "
             "measurement would silently read null. Re-run auth_spotify.py")


def check_youtube(env: dict[str, str]) -> None:
    print(f"\n[5/{STEPS}] YouTube Data API — one videos.list request (1 quota unit)")
    key = env.get("YOUTUBE_API_KEY", "")
    if not key:
        fail("no YOUTUBE_API_KEY to test")
        return
    # The key goes in a header, as in sources/youtube.py: a URL-borne key ends
    # up in any traceback, proxy log or error message that echoes the URL.
    req = urllib.request.Request(
        "https://www.googleapis.com/youtube/v3/videos?part=contentDetails&id=dQw4w9WgXcQ",
        headers={"X-Goog-Api-Key": key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace").replace(key, "<KEY>")
        if e.code == 403 and "quotaExceeded" in body:
            unavailable("YouTube quota exhausted for today; key not proven")
        else:
            http_problem("videos.list", e.code, body)
        return
    except urllib.error.URLError as e:
        unavailable(f"YouTube unreachable: {e.reason}")
        return
    items = data.get("items") or []
    duration = items[0].get("contentDetails", {}).get("duration") if items else None
    if duration:
        ok(f"key accepted; contentDetails.duration readable ({duration})")
    else:
        fail(f"key accepted but no duration returned: keys={sorted(data)}")


def check_paths(env: dict[str, str]) -> None:
    print(f"\n[6/{STEPS}] local paths — vault, inbox, outbox, raw evidence")
    root = ENV_PATH.parent

    def resolve(value: str) -> pathlib.Path:
        path = pathlib.Path(value).expanduser()
        return path if path.is_absolute() else root / path

    vault = pathlib.Path(env.get("DR_BRAINROT_VAULT_PATH", "")).expanduser()
    if not env.get("DR_BRAINROT_VAULT_PATH"):
        fail("DR_BRAINROT_VAULT_PATH unset")
        return
    if not (vault / ".obsidian").is_dir():
        fail("DR_BRAINROT_VAULT_PATH is not an Obsidian vault (no .obsidian/ folder)")
    else:
        ok("vault found (path not printed)")
    inbox = env.get("DR_BRAINROT_VAULT_INBOX", "")
    (ok if (vault / inbox).is_dir() else fail)(f"inbox folder '{inbox}' exists in the vault")
    for key in ("DR_BRAINROT_CAPTURE_OUTBOX", "DR_BRAINROT_RAW_DIR"):
        target = resolve(env.get(key, ""))
        parent = target.parent if key == "DR_BRAINROT_CAPTURE_OUTBOX" else target
        try:
            parent.mkdir(parents=True, exist_ok=True)
            probe = parent / ".check_env_probe"
            probe.write_text("")
            probe.unlink()
            ok(f"{key} location is writable")
        except OSError as e:
            fail(f"{key} location not writable: {e.strerror}")


def check_vision() -> None:
    print(f"\n[7/{STEPS}] Apple Vision OCR — on-device text recognition")
    if sys.platform != "darwin":
        fail("Screen Time parsing needs macOS (Apple Vision); this host is not a Mac")
        return
    try:
        import Vision  # noqa: F401,PLC0415
        import Quartz  # noqa: F401,PLC0415
    except ImportError:
        fail("pyobjc Vision/Quartz not importable — run with .venv/bin/python, "
             "or pip install -r requirements.txt")
        return
    ok("Vision and Quartz frameworks importable (no network involved)")


def check_coldturkey(env: dict[str, str]) -> None:
    print(f"\n[8/{STEPS}] Cold Turkey statistics — read-only, counts only (no URLs printed)")
    path = env.get("DR_BRAINROT_COLDTURKEY_DB", "")
    if not path:
        ok("DR_BRAINROT_COLDTURKEY_DB unset: Cold Turkey arrives by manual export")
        return
    import sqlite3  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415

    db_path = pathlib.Path(path)
    if not db_path.exists():
        fail("DR_BRAINROT_COLDTURKEY_DB does not exist (is Cold Turkey Blocker installed?)")
        return
    try:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True, timeout=10)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(stats)")}
            if not {"date", "domain", "seconds"} <= cols:
                fail("Cold Turkey stats table lacks date/domain/seconds; its format changed")
                return
            (latest,) = conn.execute("SELECT MAX(date) FROM stats").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        fail(f"Cold Turkey statistics not readable ({type(e).__name__})")
        return
    ok("Cold Turkey statistics readable" + ("" if latest else " (no rows yet)"))


def check_qbserve(env: dict[str, str]) -> None:
    print(f"\n[9/{STEPS}] Qbserve — read-only, counts and timestamps only (no titles printed)")
    path = env.get("DR_BRAINROT_QBSERVE_DB", "")
    if not path:
        ok("DR_BRAINROT_QBSERVE_DB unset: desktop apps not measured")
        return
    import datetime as dt  # noqa: PLC0415
    import sqlite3  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415

    db_path = pathlib.Path(path).expanduser()
    if not db_path.exists():
        fail("DR_BRAINROT_QBSERVE_DB does not exist (is Qbserve installed and has it run?)")
        return
    need = {"Apps": {"_id", "bundle", "localized_name"}, "Activities": {"_id", "source", "app_id"},
            "HistoryTablesIndex": {"table_name", "start_date"}}
    try:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True, timeout=10)
        try:
            for table, cols in need.items():
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if not cols <= have:
                    fail(f"Qbserve's {table} table changed shape; the reader would refuse it")
                    return
            (apps,) = conn.execute("SELECT COUNT(*) FROM Apps").fetchone()
            tables = [r[0] for r in conn.execute("SELECT table_name FROM HistoryTablesIndex")]
            latest = None
            for table in tables:
                if not isinstance(table, str) or not re.fullmatch(r"z_HistoryLog_\d{4}_\d{1,2}", table):
                    continue
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if not {"activity_id", "start_time", "duration"} <= have:
                    fail(f"Qbserve's {table} changed shape; the reader would refuse it")
                    return
                (tmax,) = conn.execute(f"SELECT MAX(start_time) FROM {table}").fetchone()
                if tmax is not None:
                    latest = tmax if latest is None else max(latest, tmax)
        finally:
            conn.close()
    except sqlite3.Error as e:
        fail(f"Qbserve database not readable ({type(e).__name__})")
        return
    if latest is None:
        ok(f"Qbserve database readable: {apps} apps, {len(tables)} history table(s), no records yet")
        return
    newest = dt.datetime.fromtimestamp(float(latest), dt.timezone.utc)
    ok(f"Qbserve database readable: {apps} apps, {len(tables)} history table(s), newest record "
       f"{newest.isoformat(timespec='minutes')}")
    age_h = (dt.datetime.now(dt.timezone.utc) - newest).total_seconds() / 3600
    if age_h > 36:
        print(f"  WARN  Qbserve's newest record is {age_h:.0f} h old: is Qbserve running? "
              "The com.jacob.dr-brainrot.qbserve agent reopens it within 10 minutes (not a failure)")


def check_anthropic(env: dict[str, str]) -> None:
    """Shape only. No live call: this script must never spend money or leave a
    trace on a provider, and the weekly summary proves the key by itself."""
    print(f"\n[10/{STEPS}] Anthropic — optional, weekly summary only (shape check, no API call)")
    val = env.get(ANTHROPIC_KEY, "")
    if not val:
        ok(f"{ANTHROPIC_KEY} unset: the Overview's weekly summary shows dr-brainrot's own computed "
           "sentences instead (optional; remove the key to turn the generated summary off)")
        return
    if not re.fullmatch(ANTHROPIC_SHAPE, val):
        fail(f"{ANTHROPIC_KEY} does not match expected shape /{ANTHROPIC_SHAPE}/ "
             f"(got {mask(ANTHROPIC_KEY, val)}); unset it to turn the weekly summary off")
        return
    ok(f"{ANTHROPIC_KEY} = {mask(ANTHROPIC_KEY, val)} (shape only; one summary a week, "
       "aggregates only, never source names)")


def main() -> int:
    ping = "--ping" in sys.argv
    print("dr-brainrot — environment and credential check")
    env = load_env()
    check_presence(env)
    check_permissions()
    check_telegram(env, ping)
    check_spotify(env)
    check_youtube(env)
    check_paths(env)
    check_vision()
    check_coldturkey(env)
    check_qbserve(env)
    check_anthropic(env)

    print("\n" + "=" * 60)
    if failures:
        print(f"RESULT: FAILED — {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        for u in unavailable_list:
            print(f"  - (unavailable) {u}")
        print("Do not proceed until the failures are resolved.")
        return 1
    if unavailable_list:
        print(f"RESULT: UNPROVEN — no failures, but {len(unavailable_list)} provider(s) unavailable")
        for u in unavailable_list:
            print(f"  - {u}")
        print("Credentials are not known to be bad. Re-run when the provider is reachable.")
        return 3
    print("RESULT: PASSED — .env is complete and every dependency works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
