"""Automated Cold Turkey: reading this Mac's statistics database, hostnames only.

Every test builds a synthetic `data-browser.db` with the observed schema. No
test reads the real Cold Turkey file: `settings.coldturkey_db` is unset by
default and only ever pointed at tmp_path here.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

import clock
import config
import db
import checklist
import evidence
import jobs
import labels
import report
import dr_brainrot_types as T
from sources import coldturkey, coldturkey_local as ctl

SECRET = "zz-private-search-term"


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


def make_ct_db(path, rows):
    """rows: (utc iso minute, full url, seconds). Schema as observed in Cold Turkey."""
    c = sqlite3.connect(str(path))
    c.execute('CREATE TABLE "stats" ("date" REAL NOT NULL, "domain" TEXT NOT NULL, "seconds" REAL NOT NULL, '
              '"user" TEXT NOT NULL DEFAULT (\'Unknown\'), PRIMARY KEY ("date", "domain"))')
    c.executemany("INSERT INTO stats (date, domain, seconds, user) VALUES (?, ?, ?, 'u')",
                  [(_epoch(t), u, s) for t, u, s in rows])
    c.commit()
    c.close()
    return path


ROWS = [
    ("2026-09-14T08:00:00", f"https://www.google.com/search?q={SECRET}&sca=1", 60),
    ("2026-09-14T08:01:00", "https://google.com/maps/place/somewhere/@1,2,3z", 30),
    ("2026-09-14T09:00:00", "https://mail.google.com/mail/u/0/#inbox/abc", 45),
    ("2026-09-14T10:00:00", f"file:///users/someone/desktop/{SECRET}.pdf", 60),
    ("2026-09-14T11:00:00", "https://x.com/someone/status/1", 120),
    ("2026-09-14T11:05:00", "https://twitter.com/home", 60),
    # 22:30 UTC on the 14th is 00:30 on the 15th in Warsaw (CEST, UTC+2).
    ("2026-09-14T22:30:00", "https://eips.ethereum.org/EIPS/eip-7579", 60),
    ("2026-09-15T12:00:00", "https://user:pw@example.org:8443/path", 30),
    ("2026-09-16T12:00:00", "https://google.com/search?q=today", 600),       # today: never read
    ("2026-09-10T12:00:00", "https://google.com/search?q=before-go-live", 600),
]


@pytest.fixture
def ct_db(tmp_path):
    return make_ct_db(tmp_path / "data-browser.db", ROWS)


@pytest.fixture
def settings(tmp_path, ct_db):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "01-Inbox").mkdir()
    return config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw", go_live="2026-09-13",
                                coldturkey_db=ct_db, vault_path=vault, capture_outbox=tmp_path / "o.jsonl")


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-16T10:00:00+00:00")     # Wednesday, local 12:00


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


# --- hostnames --------------------------------------------------------------

@pytest.mark.parametrize("url,host", [
    ("https://www.Google.com/search?q=secret", "www.google.com"),
    ("http://mail.google.com/mail/u/0/#inbox", "mail.google.com"),
    ("file:///Users/me/Desktop/private.pdf", "file:"),
    ("https://user:pw@example.org:8443/x", "example.org"),
    ("example.com/no-scheme?x=1", "example.com"),
    ("", None),
    ("   ", None),
    ("https:// spaced host/", None),
])
def test_hostname_keeps_only_the_host(url, host):
    assert ctl.hostname(url) == host


# --- reading ----------------------------------------------------------------

def test_read_sums_per_local_day_and_host_and_drops_everything_finer(ct_db):
    totals = ctl.read_host_minutes(ct_db, first_day=date(2026, 9, 13), last_day=date(2026, 9, 15),
                                   tz="Europe/Warsaw")
    assert totals == {
        (date(2026, 9, 14), "google.com"): 1.5,          # www + maps folded into one host total
        (date(2026, 9, 14), "mail.google.com"): 0.75,
        (date(2026, 9, 14), "file:"): 1.0,
        (date(2026, 9, 14), "x.com"): 2.0,
        (date(2026, 9, 14), "twitter.com"): 1.0,
        (date(2026, 9, 15), "eips.ethereum.org"): 1.0,   # Warsaw day, not UTC day
        (date(2026, 9, 15), "example.org"): 0.5,
    }
    assert not any(SECRET in host for _, host in totals)


def test_missing_database_and_changed_schema_fail_loudly(tmp_path):
    with pytest.raises(ctl.ColdTurkeyLocalError):
        ctl.read_host_minutes(tmp_path / "nope.db", first_day=date(2026, 9, 13), last_day=date(2026, 9, 14),
                              tz="Europe/Warsaw")
    bad = tmp_path / "bad.db"
    c = sqlite3.connect(str(bad))
    c.execute("CREATE TABLE stats (day TEXT, url TEXT, secs REAL)")
    c.close()
    with pytest.raises(ctl.ColdTurkeyLocalError, match="format changed"):
        ctl.read_host_minutes(bad, first_day=date(2026, 9, 13), last_day=date(2026, 9, 14), tz="Europe/Warsaw")


def test_document_round_trips_through_the_export_parser():
    doc = ctl.export_document({(date(2026, 9, 14), "google.com"): 1.5, (date(2026, 9, 15), "x.com"): 2.0})
    parsed = coldturkey.parse(doc)
    assert sorted((label, d, m) for label, d, m in parsed.rows) == [
        ("google.com", date(2026, 9, 14), 1.5), ("x.com", date(2026, 9, 15), 2.0)]
    assert ctl.export_document({(date(2026, 9, 14), "google.com"): 1.5}) == \
        ctl.export_document({(date(2026, 9, 14), "google.com"): 1.5})


# --- window -----------------------------------------------------------------

def test_window_is_finished_days_from_go_live(clk, settings):
    assert ctl.window(clk, settings) == (date(2026, 9, 13), date(2026, 9, 15))
    clk.set("2026-09-30T10:00:00+00:00")
    assert ctl.window(clk, settings) == (date(2026, 9, 22), date(2026, 9, 29))
    clk.set("2026-09-13T08:00:00+00:00")
    assert ctl.window(clk, settings) is None


# --- sync -------------------------------------------------------------------

def test_sync_imports_hostname_minutes_and_nothing_personal(conn, clk, settings):
    out = ctl.sync(conn, clk, settings)
    assert out.status == "ok" and out.measurements_written == 7
    assert (out.period_start, out.period_end) == ("2026-09-14", "2026-09-15")
    rows = {(r["item_title"], r["period_start"]): r["minutes"] for r in conn.execute(
        "SELECT item_title, period_start, minutes FROM measurements")}
    assert rows[("google.com", "2026-09-14")] == 1.5
    assert ("google.com", "2026-09-16") not in rows          # today not read
    assert ("google.com", "2026-09-10") not in rows          # before go-live not read

    imp = conn.execute("SELECT * FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    meta = json.loads(imp["meta"])
    assert meta["origin"] == "local_db" and meta["filename"] == ctl.FILENAME

    # Privacy: no URL fragment anywhere dr-brainrot stores things.
    raw = evidence.raw_file(settings, imp["raw_path"]).read_text()
    assert SECRET not in raw and "?" not in raw and "/search" not in raw and "/users/" not in raw
    dump = "\n".join(conn.iterdump())
    assert SECRET not in dump and "/search" not in dump and "maps/place" not in dump


def test_x_hosts_fold_into_the_x_source_as_separate_facts(conn, clk, settings):
    ctl.sync(conn, clk, settings)
    x = conn.execute("SELECT m.item_key, m.minutes FROM measurements m JOIN sources s ON s.id = m.source_id "
                     "WHERE s.kind = 'x' ORDER BY m.item_key").fetchall()
    assert [tuple(r) for r in x] == [("twitter.com", 1.0), ("x.com", 2.0)]


def test_unchanged_day_is_a_duplicate_and_new_minutes_replace_facts(conn, clk, settings, ct_db):
    first = ctl.sync(conn, clk, settings)
    again = ctl.sync(conn, clk, settings)
    assert again.duplicate_of == first.import_id
    c = sqlite3.connect(str(ct_db))
    c.execute("INSERT INTO stats VALUES (?, 'https://google.com/late', 30, 'u')", (_epoch("2026-09-14T15:00:00"),))
    c.commit()
    c.close()
    third = ctl.sync(conn, clk, settings)
    assert third.duplicate_of is None and third.import_id != first.import_id
    (minutes,) = conn.execute("SELECT minutes FROM measurements WHERE item_title='google.com' "
                              "AND period_start='2026-09-14'").fetchone()
    assert minutes == 2.0
    assert conn.execute("SELECT COUNT(*) FROM measurements WHERE item_title='google.com' "
                        "AND period_start='2026-09-14'").fetchone()[0] == 1


def test_replaces_an_earlier_manual_export_fact(conn, clk, settings):
    manual = json.dumps([{"label": "google.com", "data": [[1789344000000, 9.99]]}]).encode()
    assert coldturkey.ingest(conn, clk, settings, manual, filename="manual.json").status == "ok"
    ctl.sync(conn, clk, settings)
    rows = conn.execute("SELECT minutes, detail FROM measurements WHERE item_title='google.com' "
                        "AND period_start='2026-09-14'").fetchall()
    assert len(rows) == 1 and rows[0]["minutes"] == 1.5
    assert json.loads(rows[0]["detail"])["origin"] == "local_db"


def test_unconfigured_never_reads_anything(conn, clk, tmp_path):
    plain = config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw", go_live="2026-09-13")
    assert plain.coldturkey_db is None
    assert ctl.sync(conn, clk, plain) is None
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


# --- daily job, check-in, report ----------------------------------------------

class Bot:
    def __init__(self):
        self.sent = []

    def send_message(self, **kw):
        self.sent.append(kw)


def test_daily_job_reads_before_the_check_in_so_a_covered_week_is_not_asked_for(conn, settings, ct_db):
    c = sqlite3.connect(str(ct_db))
    c.executemany("INSERT INTO stats VALUES (?, 'https://example.net/', 60, 'u')",
                  [(_epoch(f"2026-09-{d}T12:00:00"),) for d in (13, 15, 17, 19)])
    c.commit()
    c.close()
    sunday = clock.FrozenClock("2026-09-20T08:00:00+00:00")       # the Sunday check-in (DR5.24)
    bot = Bot()
    failed = jobs.run_daily(conn, sunday, settings, bot)
    assert "coldturkey_local" not in failed
    (text,) = [m["text"] for m in bot.sent]
    assert text.startswith("Sunday check-in for last week, Sun 13 – Sat 19 Sep.")
    assert "Cold Turkey" not in text
    assert "1. Screen Time." in text


def test_daily_job_reports_an_unreadable_database_and_still_asks(conn, tmp_path, settings):
    broken = config.test_settings(**{**settings.__dict__, "coldturkey_db": tmp_path / "gone.db"})
    bot = Bot()
    failed = jobs.run_daily(conn, clock.FrozenClock("2026-09-20T08:00:00+00:00"), broken, bot)
    assert "coldturkey_local" in failed
    (text,) = [m["text"] for m in bot.sent]
    assert "3. Cold Turkey. This Mac's statistics couldn't be read this week, so export them by hand:" in text


def test_blind_spots_follow_how_cold_turkey_minutes_arrived(conn, clk, settings):
    ctl.sync(conn, clk, settings)
    text = "\n".join(report.blind_spots(conn, clk, settings))
    assert "top sites" not in text
    manual = json.dumps([{"label": "a.example", "data": [[1789344000000, 1.0]]}]).encode()
    coldturkey.ingest(conn, clk, settings, manual, filename="manual.json")
    text = "\n".join(report.blind_spots(conn, clk, settings))
    assert "top sites" in text


def test_check_in_mentions_the_failing_read_only_when_configured(settings):
    needs = checklist.WeekNeeds(T.measurement_week(date(2026, 9, 13)), checklist.IN, checklist.IN,
                                checklist.MISSING, 0, labels.Summary(0, 0.0, None), False)
    plain = config.test_settings(tz="Europe/Warsaw")
    assert "couldn't be read" not in checklist.render(needs, plain, nudge=False)
    assert "Export last week by hand" in checklist.render(needs, plain, nudge=False)
    assert "couldn't be read this week" in checklist.render(needs, settings, nudge=False)
