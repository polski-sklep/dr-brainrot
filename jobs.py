"""jobs.py — DRBR's daily entry point, alongside POP's poll.py and triage.py.

    .venv/bin/python jobs.py daily

Integration module (CONTRACTS.md Part II §11). It calls, and never duplicates,
the functions each source owns:

1. `vault.read_outbox` then `vault.scan` — new obsidian-ingest captures first,
   so a note captured yesterday is already in the denominator when scanned.
2. `coldturkey_local.sync` — this Mac's Cold Turkey statistics, hostnames only
   (DR5.1), before the prompts so a week it covers is never asked for.
3. `qbserve.sync` — desktop app minutes from Qbserve, browsers skipped
   (DR5.3), with a once-a-day Telegram warning while Qbserve has not
   recorded for more than 36 hours.
4. `handlers_imports.send_due_prompts` — Screen Time / Cold Turkey / YouTube
   for the last complete week, once per day until each arrives.
5. `handlers_labels.send_label_batch` — newly seen sources, once the
   diagnostic has run a week.
6. One diagnostic-end notice, ever.

Each step is isolated: a vault failure must not cost the user their Monday
prompt, and a Telegram outage must not stop the scan. Every failure is logged
and makes the process exit non-zero, so launchd's log shows it; none is
swallowed into a success.
"""
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable

import clock
import db
import drbr_types as T
import evidence
import handlers_imports
import handlers_labels
from clock import Clock
from config import Settings
from sources import coldturkey_local, qbserve, vault
from triage import SyncBot, send

log = logging.getLogger("drbr.jobs")

END_KIND = "diagnostic_end"
QBSERVE_STALE_KIND = "qbserve_stale"
QBSERVE_STALE_HOURS = 36


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
    step("qbserve", lambda: _qbserve(conn, clk, settings, bot))
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


def _qbserve(conn, clk: Clock, settings: Settings, bot) -> str:
    if settings.qbserve_db is None:
        return "not configured (DRBR_QBSERVE_DB unset): desktop apps not measured"
    try:
        outcome = qbserve.sync(conn, clk, settings)
        if outcome is None:
            result = "no finished days to read yet"
        elif outcome.duplicate_of is not None:
            result = f"unchanged since import {outcome.duplicate_of}"
        else:
            for warning in outcome.warnings:
                log.warning("qbserve: %s", warning)
            if outcome.status == "failed":
                raise RuntimeError(f"qbserve import failed: {outcome.error}")
            result = f"import {outcome.import_id} {outcome.status}, {outcome.measurements_written} app-day figures"
    except Exception:
        # Warn even when this read failed: a stale earlier import is still the last
        # word. Roll back first so the warning's commit can't carry half a write.
        conn.rollback()
        try:
            _qbserve_stale_warning(conn, clk, settings, bot)
        except Exception:  # noqa: BLE001 — the read failure is the error to report
            log.exception("qbserve stale warning failed")
        raise
    return f"{result}; {_qbserve_stale_warning(conn, clk, settings, bot)}"


def _qbserve_stale_warning(conn, clk: Clock, settings: Settings, bot) -> str:
    """One Telegram message a day while Qbserve has not recorded for over 36 hours (DR5.3)."""
    hours = qbserve.staleness_hours(conn, clk)
    if hours is None or hours <= QBSERVE_STALE_HOURS:
        return "qbserve recording is current" if hours is not None else "no qbserve record to judge staleness"
    if evidence.diagnostic(conn, clk, settings).ended:
        return "qbserve stale; diagnostic ended, no warning"
    today = T.iso_date(T.local_date(clk.now(), settings.tz))
    if conn.execute("SELECT 1 FROM prompts_sent WHERE kind = ? AND sent_on = ?",
                    (QBSERVE_STALE_KIND, today)).fetchone():
        return "qbserve stale; warning already sent today"
    row = conn.execute("SELECT meta FROM imports WHERE kind = ? AND status != 'failed' ORDER BY id DESC LIMIT 1",
                       (qbserve.KIND,)).fetchone()
    latest = json.loads(row["meta"] or "{}").get("latest_record_at")
    send(bot, settings, (
        f"Qbserve hasn't recorded anything since {clock.fmt_local(latest, settings.tz)}.\n\n"
        "Desktop app time is missing from DRBR until it runs again. "
        "Open Qbserve, and set it to open at login so this doesn't recur."
    ))
    conn.execute(
        "INSERT OR IGNORE INTO prompts_sent (kind, period_start, sent_on, sent_at) VALUES (?, ?, ?, ?)",
        (QBSERVE_STALE_KIND, today, today, clock.iso(clk.now())),
    )
    conn.commit()
    return "qbserve stale; warning sent"


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
