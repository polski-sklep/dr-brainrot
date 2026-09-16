"""jobs.py — DRBR's daily entry point, alongside POP's poll.py and triage.py.

    .venv/bin/python jobs.py daily

Integration module (CONTRACTS.md Part II §11). It calls, and never duplicates,
the functions each source owns:

1. `vault.read_outbox` then `vault.scan` — new obsidian-ingest captures first,
   so a note captured yesterday is already in the denominator when scanned.
2. `coldturkey_local.sync` — this Mac's Cold Turkey statistics, hostnames only
   (DR5.1), before the prompts so a week it covers is never asked for.
3. `handlers_imports.send_due_prompts` — Screen Time / Cold Turkey / YouTube
   for the last complete week, once per day until each arrives.
4. `handlers_labels.send_label_batch` — newly seen sources, once the
   diagnostic has run a week.
5. One diagnostic-end notice, ever.

Each step is isolated: a vault failure must not cost the user their Monday
prompt, and a Telegram outage must not stop the scan. Every failure is logged
and makes the process exit non-zero, so launchd's log shows it; none is
swallowed into a success.
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import clock
import db
import evidence
import handlers_imports
import handlers_labels
from clock import Clock
from config import Settings
from sources import coldturkey_local, vault
from triage import SyncBot, send

log = logging.getLogger("drbr.jobs")

END_KIND = "diagnostic_end"


def run_daily(conn, clk: Clock, settings: Settings, bot) -> list[str]:
    """Run every daily step. Returns the names of steps that failed."""
    failed: list[str] = []

    def step(name: str, fn: Callable[[], object]) -> None:
        try:
            result = fn()
            log.info("%s: %s", name, result)
        except Exception:  # noqa: BLE001 — isolate steps; reported below, never swallowed
            conn.rollback()
            log.exception("%s failed", name)
            failed.append(name)

    step("read_outbox", lambda: _outbox(conn, clk, settings))
    step("vault_scan", lambda: _scan(conn, clk, settings))
    step("coldturkey_local", lambda: _coldturkey(conn, clk, settings))
    step("import_prompts", lambda: handlers_imports.send_due_prompts(conn, clk, settings, bot))
    step("label_batch", lambda: handlers_labels.send_label_batch(conn, clk, settings, bot))
    step("diagnostic_end", lambda: send_end_notice(conn, clk, settings, bot))
    return failed


def _outbox(conn, clk: Clock, settings: Settings) -> str:
    result = vault.read_outbox(conn, clk, settings)
    for problem in result.malformed:
        log.warning("outbox: %s", problem)
    return f"{result.new} new capture(s), {len(result.malformed)} malformed line(s)"


def _scan(conn, clk: Clock, settings: Settings) -> str:
    outcome = vault.scan(conn, clk, settings)
    for warning in outcome.warnings:
        log.warning("vault scan: %s", warning)
    if outcome.status == "failed":
        raise RuntimeError(f"vault scan failed: {outcome.error}")
    return f"import {outcome.import_id} {outcome.status}"


def _coldturkey(conn, clk: Clock, settings: Settings) -> str:
    if settings.coldturkey_db is None:
        return "not configured (DRBR_COLDTURKEY_DB unset): manual exports only"
    outcome = coldturkey_local.sync(conn, clk, settings)
    if outcome is None:
        return "no finished days to read yet"
    if outcome.duplicate_of is not None:
        return f"unchanged since import {outcome.duplicate_of}"
    for warning in outcome.warnings:
        log.warning("cold turkey: %s", warning)
    if outcome.status == "failed":
        raise RuntimeError(f"cold turkey import failed: {outcome.error}")
    return f"import {outcome.import_id} {outcome.status}, {outcome.measurements_written} domain-day figures"


def send_end_notice(conn, clk: Clock, settings: Settings, bot) -> str:
    """The diagnostic has an end date, and the tool says so, once."""
    diag = evidence.diagnostic(conn, clk, settings)
    if not diag.ended:
        return "diagnostic running"
    if conn.execute("SELECT 1 FROM prompts_sent WHERE kind = ?", (END_KIND,)).fetchone():
        return "end notice already sent"
    send(bot, settings, (
        f"The DRBR diagnostic ended on {diag.end}, after {settings.diagnostic_weeks} weeks.\n\n"
        "Open the dashboard, look at cost per keeper and the ungated share, and name the "
        "one decision this should change. If there isn't one, shut DRBR down. "
        "Prompts and label requests have stopped."
    ))
    today = clock.iso(clk.now())
    conn.execute(
        "INSERT OR IGNORE INTO prompts_sent (kind, period_start, sent_on, sent_at) VALUES (?, ?, ?, ?)",
        (END_KIND, diag.start, diag.end, today),
    )
    conn.commit()
    return "end notice sent"


def main(argv: list[str]) -> int:  # pragma: no cover — production only
    if argv[1:] != ["daily"]:
        print("usage: jobs.py daily", file=sys.stderr)
        return 2
    import config
    from bot import _silence_token_logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _silence_token_logging()
    settings = config.load()
    conn = db.connect(settings)
    db.init_db(conn)
    db.migrate(conn)
    telegram_bot = SyncBot(settings.telegram_bot_token)
    try:
        failed = run_daily(conn, clock.RealClock(), settings, telegram_bot)
    finally:
        telegram_bot.close()
        conn.close()
    if failed:
        log.error("daily job finished with failures: %s", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
