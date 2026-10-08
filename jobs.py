"""jobs.py — DRBR's daily entry point, alongside POP's poll.py and triage.py.

    .venv/bin/python jobs.py daily

Integration module (CONTRACTS.md Part II §11). It calls, and never duplicates,
the functions each source owns:

1. `vault.read_outbox` then `vault.scan` — new obsidian-ingest captures first,
   so a note captured yesterday is already in the denominator when scanned.
2. `coldturkey_local.sync` — this Mac's Cold Turkey statistics, hostnames only
   (DR5.1), before the check-in so a week it covers is never asked for.
3. `qbserve.sync` — desktop app minutes from Qbserve, browsers skipped
   (DR5.3); then `brave_history.sync`, the phone browser's sites (DR5.13).
4. The Qbserve warning — one Telegram message a day while Qbserve has not
   recorded for more than 36 hours. Its own step, after every read, so it
   still goes out when today's Qbserve read failed: an earlier import is
   still the last word.
5. `checklist.send_due` — the Sunday check-in for the week just finished
   (Screen Time, YouTube, Cold Turkey only as a fallback, readings to settle,
   labels), and its one Monday reminder while evidence is still missing
   (DR5.24). The only thing that asks the user for anything.
6. One diagnostic-end notice, ever, once the final week's evidence is in (or
   from the Tuesday after it).
7. `insights.write_summary` — the Overview's weekly written summary (DR5.8),
   which writes at most one row a week and never fails the run.

Steps 1-3 are the *data-fetching* half and are also callable on their own as
`run_sync`, so a dashboard "Sync now" button can do exactly what the morning
run does without sending anything.

Each step is isolated: a vault failure must not cost the user their Sunday
check-in, and a Telegram outage must not stop the scan. Every failure is logged
and makes the process exit non-zero, so launchd's log shows it; none is
swallowed into a success.

**Reads first, sends after, and Telegram is best-effort** (DR5.25). On
27–28 Sep the Mac had no internet and this job died connecting to Telegram
before reading anything. Now nothing touches Telegram until a step sends; a
failed send fails only its own step, writes no marker, and is retried on a
later run.
"""
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import checklist
import clock
import db
import drbr_types as T
import evidence
import insights
from clock import Clock
from config import Settings
from sources import brave_history, coldturkey_local, qbserve, vault
from triage import SyncBot, send

log = logging.getLogger("drbr.jobs")

END_KIND = "diagnostic_end"
QBSERVE_STALE_KIND = "qbserve_stale"
QBSERVE_STALE_HOURS = 36


@dataclass(frozen=True)
class StepResult:
    """What one isolated step did, or why it did nothing.

    `summary` is the same sentence the log line carries, so a dashboard can
    show the user exactly what the morning run would have written to
    logs/jobs.log rather than a bare tick.
    """

    name: str
    ok: bool
    summary: str
    error: str | None = None


#: The data-fetching steps, in dependency order: captures are read before the
#: scan that decides their outcome, and this Mac's own sources before anything
#: that asks the user for a week they might already cover.
SYNC_STEPS = ("read_outbox", "vault_scan", "coldturkey_local", "qbserve", "phone_browser")


def _run_step(conn, name: str, fn: Callable[[], object]) -> StepResult:
    """Run one step in isolation: a failure is recorded, logged, never raised."""
    try:
        result = fn()
        log.info("%s: %s", name, result)
        return StepResult(name, True, str(result))
    except Exception as exc:  # noqa: BLE001 — isolate steps; reported to the caller, never swallowed
        conn.rollback()
        log.exception("%s failed", name)
        return StepResult(name, False, f"{name} failed", f"{type(exc).__name__}: {exc}")


def run_sync(conn, clk: Clock, settings: Settings) -> dict[str, StepResult]:
    """Fetch everything DRBR can fetch by itself. -> one StepResult per step.

    The half of the daily run that only *reads* sources: the capture outbox,
    the vault scan, Cold Turkey's own statistics, Qbserve and the phone
    browser. It sends nothing at all, so it is safe to run on demand — a
    dashboard "Sync now" button is exactly this function — and `run_daily`
    calls it rather than keeping a second copy of the order. Nothing here can
    wait on Telegram (DR5.25).
    """
    return {
        "read_outbox": _run_step(conn, "read_outbox", lambda: _outbox(conn, clk, settings)),
        "vault_scan": _run_step(conn, "vault_scan", lambda: _scan(conn, clk, settings)),
        "coldturkey_local": _run_step(conn, "coldturkey_local", lambda: _coldturkey(conn, clk, settings)),
        "qbserve": _run_step(conn, "qbserve", lambda: _qbserve(conn, clk, settings)),
        "phone_browser": _run_step(conn, "phone_browser", lambda: _phone_browser(conn, clk, settings)),
    }


def run_daily(conn, clk: Clock, settings: Settings, bot) -> list[str]:
    """Run every daily step. Returns the names of steps that failed.

    `insights` is deliberately *not* in that list. It is commentary on figures
    DRBR already has, it depends on a third party, and it is optional (no key,
    no call). A summary that could not be written is recorded in the `insights`
    table and logged; making the morning job exit non-zero over it would turn a
    missing paragraph into a red launchd log next to real data failures.

    Every read comes before every send (DR5.25). The sends are their own
    steps, so Telegram being unreachable fails those steps and nothing else.
    """
    results = run_sync(conn, clk, settings)
    failed = [name for name, result in results.items() if not result.ok]
    for name, fn in (
        # After every read, and whether or not today's Qbserve read worked.
        ("qbserve_warning", lambda: _qbserve_stale_warning(conn, clk, settings, bot)),
        ("checklist", lambda: checklist.send_due(conn, clk, settings, bot)),
        ("diagnostic_end", lambda: send_end_notice(conn, clk, settings, bot)),
    ):
        if not _run_step(conn, name, fn).ok:
            failed.append(name)
    _run_step(conn, "insights", lambda: _insights(conn, clk, settings))
    return failed


def _insights(conn, clk: Clock, settings: Settings) -> str:
    """The weekly Overview summary (DR5.8). At most one row a week; never raises.

    `write_summary` skips when this week already has a row, so calling it every
    morning costs one SELECT six days out of seven. Only the status is logged:
    the summary itself belongs on the dashboard, not in logs/jobs.log.
    """
    insight = insights.write_summary(conn, clk, settings)
    if insight.model is None:
        return f"week {insight.week_start}: computed sentences — {insight.fallback_reason}"
    return f"week {insight.week_start}: written by {insight.model}"


def _phone_browser(conn, clk: Clock, settings: Settings) -> str:
    """Attribute the phone browser's block to the sites inside it (DR5.13).

    Runs after the Screen Time import it depends on, and is a no-op until both
    a browser block and a configured history database exist.
    """
    outcome = brave_history.sync(conn, clk, settings)
    if outcome is None:
        return "no phone browser figures to attribute"
    for warning in outcome.warnings:
        log.warning("phone browser: %s", warning)
    return f"import {outcome.import_id} {outcome.status}: {outcome.measurements_written} site figure(s)"


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


def _qbserve(conn, clk: Clock, settings: Settings) -> str:
    """Read Qbserve's database. Reads only: the warning is its own step."""
    if settings.qbserve_db is None:
        return "not configured (DRBR_QBSERVE_DB unset): desktop apps not measured"
    outcome = qbserve.sync(conn, clk, settings)
    if outcome is None:
        return "no finished days to read yet"
    if outcome.duplicate_of is not None:
        return f"unchanged since import {outcome.duplicate_of}"
    for warning in outcome.warnings:
        log.warning("qbserve: %s", warning)
    if outcome.status == "failed":
        raise RuntimeError(f"qbserve import failed: {outcome.error}")
    return f"import {outcome.import_id} {outcome.status}, {outcome.measurements_written} app-day figures"


def _qbserve_stale_warning(conn, clk: Clock, settings: Settings, bot) -> str:
    """One Telegram message a day while Qbserve has not recorded for over 36 hours (DR5.3).

    The day's `prompts_sent` row is written only after the send, so a failed
    send is tried again by the next run.
    """
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
        "Desktop app time isn't being measured until it runs again. "
        "Open Qbserve, and set it to open at login so this doesn't recur."
    ))
    conn.execute(
        "INSERT OR IGNORE INTO prompts_sent (kind, period_start, sent_on, sent_at) VALUES (?, ?, ?, ?)",
        (QBSERVE_STALE_KIND, today, today, clock.iso(clk.now())),
    )
    conn.commit()
    return "qbserve stale; warning sent"


def send_end_notice(conn, clk: Clock, settings: Settings, bot) -> str:
    """The diagnostic has an end date, and the tool says so, once.

    Not before the final week's evidence is in: on the Sunday after the last
    day the check-in still asks for that week (DR5.24), and "everything has
    stopped" beside it would contradict it. It waits until nothing is missing
    for that week, or until the Tuesday after it, whichever comes first.
    """
    diag = evidence.diagnostic(conn, clk, settings)
    if not diag.ended:
        return "diagnostic running"
    if conn.execute("SELECT 1 FROM prompts_sent WHERE kind = ?", (END_KIND,)).fetchone():
        return "end notice already sent"
    final = T.measurement_week(T.parse_date(diag.end))  # type: ignore[arg-type]
    today = T.local_date(clk.now(), settings.tz)
    if (today < final.saturday + timedelta(days=3)
            and checklist.week_needs(conn, clk, settings, final).evidence_missing):
        return "end notice waits for the final week's evidence"
    send(bot, settings, (
        f"The diagnostic ended on {diag.end}, after {settings.diagnostic_weeks} weeks.\n\n"
        "Open the dashboard, look at cost per keeper and the ungated share, and name the "
        "one decision this should change. If there isn't one, switch the whole thing off. "
        "Nothing more will be asked for."
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
    telegram_bot = SyncBot(settings.telegram_bot_token)    # opens nothing until it sends
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
