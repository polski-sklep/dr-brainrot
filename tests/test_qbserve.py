"""Qbserve: desktop app time on this Mac, per app per local day, browsers skipped (DR5.3).

Every test builds a synthetic Qbserve database with the observed schema in
tmp_path. No test reads the real Qbserve file: `settings.qbserve_db` is unset
by default and only ever pointed at tmp_path here.
"""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from datetime import date, datetime, timezone

import pytest

import allowance
import clock
import config
import db
import drbr_types as T
import evidence
import jobs
import labels
import reparse
import report
from dashboard import app as dashboard_app
from sources import qbserve as qb

SECRET = "zz-private-window-title"
TZ = "Europe/Warsaw"

SCHEMA = """
CREATE TABLE Categories(_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE COLLATE NOCASE,display_name TEXT UNIQUE COLLATE NOCASE,productivity TEXT NOT NULL COLLATE NOCASE,can_be_idle INTEGER NOT NULL,is_default INTEGER NOT NULL, 'is_hidden' INTEGER NOT NULL DEFAULT 0);
CREATE TABLE Apps(_id INTEGER PRIMARY KEY,bundle TEXT NOT NULL UNIQUE COLLATE NOCASE,localized_name TEXT NOT NULL COLLATE NOCASE,category_id INTEGER NOT NULL REFERENCES Categories(_id),track_windows INTEGER NOT NULL,show_windows_as_activities INTEGER NOT NULL,is_ignored INTEGER NOT NULL,latest_use INTEGER NOT NULL);
CREATE TABLE Activities(_id INTEGER PRIMARY KEY,title TEXT NOT NULL COLLATE NOCASE,source INTEGER NOT NULL,app_id INTEGER NOT NULL REFERENCES Apps(_id),category_id INTEGER NOT NULL REFERENCES Categories(_id),auto_category INTEGER NOT NULL,is_ignored INTEGER NOT NULL,latest_use INTEGER NOT NULL);
CREATE TABLE HistoryTablesIndex(_id INTEGER PRIMARY KEY,table_name TEXT NOT NULL COLLATE NOCASE,start_date INTEGER NOT NULL);
"""

HISTORY = ("CREATE TABLE {name}(_id INTEGER PRIMARY KEY,activity_id INTEGER NOT NULL REFERENCES Activities(_id),"
           "start_time INTEGER NOT NULL,duration INTEGER NOT NULL,project_id INTEGER REFERENCES Projects(_id))")

APPS = [
    (1, "com.apple.Safari", "Safari"),
    (2, "com.tinyspeck.slackmacgap", "Slack"),
    (3, "com.spotify.client", "Spotify"),
    (4, "maccatalyst.com.atebits.Tweetie2", "X"),
    (5, "org.example.UnknownBrowser", "Unlisted Browser"),     # in no list anywhere: detected by data
    (6, "com.apple.mail", "Mail"),
]

# (id, title, source, app_id, category_id)
ACTIVITIES = [
    (1, "Safari window", 111, 1, 1),
    (2, f"https://secret.example/{SECRET}", 222, 1, 2),
    (3, f"Slack | {SECRET}", 101, 2, 1),
    (4, "Spotify", 101, 3, 2),
    (5, "Home / X", 101, 4, 2),
    (6, f"unlisted page {SECRET}", 222, 5, 2),
    (7, "unlisted browser window", 111, 5, 1),
    (8, f"Inbox {SECRET}", 111, 6, 1),
]

# (utc iso, activity_id, seconds). Rows <= 60 s, as Qbserve writes them.
ROWS = [
    ("2026-09-14T08:00:00", 3, 60), ("2026-09-14T08:01:00", 3, 60), ("2026-09-14T08:02:00", 3, 30),  # Slack 2.5
    ("2026-09-14T09:00:00", 1, 60), ("2026-09-14T09:01:00", 2, 60),                               # Safari: skipped
    ("2026-09-14T10:00:00", 7, 60), ("2026-09-14T10:01:00", 6, 60),                               # unlisted browser
    # Warsaw is UTC+2: 21:59Z is 23:59 on the 14th, 22:00Z is 00:00 on the 15th.
    ("2026-09-14T21:59:00", 8, 60),
    ("2026-09-14T22:00:00", 8, 60), ("2026-09-14T22:01:00", 8, 30),
    ("2026-09-15T12:00:00", 4, 60), ("2026-09-15T12:01:00", 4, 60),                               # Spotify 2.0
    ("2026-09-15T12:05:00", 5, 60),                                                               # X 1.0
    ("2026-09-16T08:00:00", 3, 60),                                                               # today: never read
    ("2026-09-10T12:00:00", 3, 60),                                                               # before go-live
]


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def make_qb_db(path, rows=ROWS, *, productivity=("productive", "distracting"), categories=None,
               extra_tables=()):
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    c.executemany("INSERT INTO Categories VALUES (?, ?, ?, ?, 1, 0, 0)",
                  [(1, "work", "Work", productivity[0]), (2, "fun", "Fun", productivity[1])])
    c.executemany("INSERT INTO Apps VALUES (?, ?, ?, 1, 1, 0, 0, 0)", APPS)
    cat = categories or {}
    c.executemany("INSERT INTO Activities VALUES (?, ?, ?, ?, ?, 1, 0, 0)",
                  [(i, t, s, a, cat.get(i, k)) for i, t, s, a, k in ACTIVITIES])
    c.execute(HISTORY.format(name="z_HistoryLog_2026_9"))
    c.execute("INSERT INTO HistoryTablesIndex (table_name, start_date) VALUES ('z_HistoryLog_2026_9', ?)",
              (_epoch("2026-09-01T00:00:00"),))
    c.executemany("INSERT INTO z_HistoryLog_2026_9 (activity_id, start_time, duration) VALUES (?, ?, ?)",
                  [(a, _epoch(t), s) for t, a, s in rows])
    for name in extra_tables:
        c.execute(f'CREATE TABLE "{name}" (activity_id INTEGER, start_time INTEGER, duration INTEGER)')
        c.execute(f'INSERT INTO "{name}" VALUES (3, ?, 3600)', (_epoch("2026-09-14T12:00:00"),))
        c.execute("INSERT INTO HistoryTablesIndex (table_name, start_date) VALUES (?, 0)", (name,))
    c.commit()
    c.close()
    return path


def add_rows(path, rows):
    c = sqlite3.connect(str(path))
    c.executemany("INSERT INTO z_HistoryLog_2026_9 (activity_id, start_time, duration) VALUES (?, ?, ?)",
                  [(a, _epoch(t), s) for t, a, s in rows])
    c.commit()
    c.close()


@pytest.fixture
def qb_db(tmp_path):
    return make_qb_db(tmp_path / "UserDatabase.sqlite")


@pytest.fixture
def settings(tmp_path, qb_db):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "01-Inbox").mkdir()
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-09-13", qbserve_db=qb_db,
                                vault_path=vault, capture_outbox=tmp_path / "o.jsonl",
                                db_path=tmp_path / "drbr.db")


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


class Bot:
    def __init__(self):
        self.sent = []

    def send_message(self, **kw):
        self.sent.append(kw)


def facts(conn):
    return {(r["kind"], r["source_key"], r["item_key"], r["period_start"]): r["minutes"] for r in conn.execute(
        "SELECT s.kind, s.source_key, m.item_key, m.period_start, m.minutes FROM measurements m "
        "JOIN sources s ON s.id = m.source_id WHERE m.provenance = 'app_tracker'")}


# --- reading ------------------------------------------------------------------------

def test_read_sums_per_app_per_local_day_and_skips_browsers(qb_db):
    rows, info = qb.read_app_minutes(qb_db, first_day=date(2026, 9, 13), last_day=date(2026, 9, 15), tz=TZ)
    got = {(r.bundle, r.day): round(r.minutes, 4) for r in rows}
    assert got == {
        ("com.tinyspeck.slackmacgap", date(2026, 9, 14)): 2.5,
        ("com.apple.mail", date(2026, 9, 14)): 1.0,              # 23:59 Warsaw
        ("com.apple.mail", date(2026, 9, 15)): 1.5,              # 00:00 Warsaw, not the UTC day
        ("com.spotify.client", date(2026, 9, 15)): 2.0,
        ("maccatalyst.com.atebits.Tweetie2", date(2026, 9, 15)): 1.0,
    }
    # Both browsers skipped: Safari, and an app no list names, both via source 222.
    assert not any(b in ("com.apple.Safari", "org.example.UnknownBrowser") for b, _ in got)
    assert info["browser_apps_skipped"] == 2
    assert info["browser_minutes_skipped"] == 4.0
    assert info["latest_record_at"] == "2026-09-16T08:00:00+00:00"


def test_never_selects_titles_productivity_or_categories(qb_db, monkeypatch):
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced(*args, **kwargs):
        c = real_connect(*args, **kwargs)
        c.set_trace_callback(statements.append)
        return c

    monkeypatch.setattr(qb.sqlite3, "connect", traced)
    qb.read_app_minutes(qb_db, first_day=date(2026, 9, 13), last_day=date(2026, 9, 15), tz=TZ)
    assert statements
    sql = "\n".join(statements).lower()
    assert "title" not in sql and "productivity" not in sql and "categor" not in sql


def test_productivity_and_categories_change_nothing(tmp_path):
    a = make_qb_db(tmp_path / "a.sqlite")
    b = make_qb_db(tmp_path / "b.sqlite", productivity=("distracting", "very productive"),
                   categories={3: 2, 8: 2, 5: 1, 4: 1})
    span = dict(first_day=date(2026, 9, 13), last_day=date(2026, 9, 15), tz=TZ)
    ra, fa = qb.read_app_minutes(a, **span)
    rb, fb = qb.read_app_minutes(b, **span)
    assert ra == rb and fa == fb
    assert qb.document(ra, fa, first_day=date(2026, 9, 13), last_day=date(2026, 9, 15), tz=TZ) == \
        qb.document(rb, fb, first_day=date(2026, 9, 13), last_day=date(2026, 9, 15), tz=TZ)


def test_missing_database_or_changed_shape_raises(tmp_path):
    span = dict(first_day=date(2026, 9, 13), last_day=date(2026, 9, 15), tz=TZ)
    with pytest.raises(qb.QbserveError):
        qb.read_app_minutes(tmp_path / "nope.sqlite", **span)

    no_bundle = tmp_path / "apps.sqlite"
    c = sqlite3.connect(str(no_bundle))
    c.executescript(SCHEMA.replace("bundle TEXT NOT NULL UNIQUE COLLATE NOCASE,", "ident TEXT,"))
    c.close()
    with pytest.raises(qb.QbserveError, match="Apps"):
        qb.read_app_minutes(no_bundle, **span)

    history = make_qb_db(tmp_path / "hist.sqlite")
    c = sqlite3.connect(str(history))
    c.execute("DROP TABLE z_HistoryLog_2026_9")
    c.execute("CREATE TABLE z_HistoryLog_2026_9 (activity_id INTEGER, began INTEGER, secs INTEGER)")
    c.commit()
    c.close()
    with pytest.raises(qb.QbserveError, match="changed shape"):
        qb.read_app_minutes(history, **span)

    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"this is not a database" * 100)
    with pytest.raises(qb.QbserveError):
        qb.read_app_minutes(garbage, **span)


def test_unexpected_history_table_names_are_ignored_not_interpolated(tmp_path):
    evil = ["Apps; DROP TABLE Apps; --", "z_HistoryLog_2026_9\n", "Evil"]
    path = make_qb_db(tmp_path / "evil.sqlite", extra_tables=evil)
    rows, _ = qb.read_app_minutes(path, first_day=date(2026, 9, 13), last_day=date(2026, 9, 15), tz=TZ)
    slack = {r.day: r.minutes for r in rows if r.bundle == "com.tinyspeck.slackmacgap"}
    assert slack == {date(2026, 9, 14): 2.5}                    # the 60-minute rows in those tables never counted
    c = sqlite3.connect(str(path))
    assert c.execute("SELECT COUNT(*) FROM Apps").fetchone()[0] == len(APPS)
    c.close()


# --- window and sync --------------------------------------------------------------------

def test_window_excludes_today_and_clamps_to_go_live(clk, settings):
    assert qb.window(clk, settings) == (date(2026, 9, 13), date(2026, 9, 15))
    clk.set("2026-09-30T10:00:00+00:00")
    assert qb.window(clk, settings) == (date(2026, 9, 22), date(2026, 9, 29))
    clk.set("2026-09-13T08:00:00+00:00")
    assert qb.window(clk, settings) is None


def test_sync_imports_app_minutes_and_never_stores_titles(conn, clk, settings):
    out = qb.sync(conn, clk, settings)
    assert out.status == "ok" and out.measurements_written == 5
    # Nothing was recorded on the 13th, so coverage starts at the 14th rather than claiming a zero.
    assert (out.period_start, out.period_end) == ("2026-09-14", "2026-09-15")
    got = facts(conn)
    assert got == {
        ("desktop_app", "com.tinyspeck.slackmacgap", "com.tinyspeck.slackmacgap", "2026-09-14"): 2.5,
        ("desktop_app", "com.apple.mail", "com.apple.mail", "2026-09-14"): 1.0,
        ("desktop_app", "com.apple.mail", "com.apple.mail", "2026-09-15"): 1.5,
        ("desktop_app", "com.spotify.client", "com.spotify.client", "2026-09-15"): 2.0,
        ("x", "x", "maccatalyst.com.atebits.tweetie2", "2026-09-15"): 1.0,
    }
    assert not any(day in ("2026-09-16", "2026-09-10") for *_, day in got)       # today, pre-go-live
    assert {r[0] for r in conn.execute("SELECT DISTINCT device FROM measurements")} == {T.DEVICE_DESKTOP}

    imp = conn.execute("SELECT * FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    assert imp["kind"] == "qbserve"
    raw = evidence.raw_file(settings, imp["raw_path"]).read_text()
    dump = "\n".join(conn.iterdump())
    for text in (raw, dump):
        assert SECRET not in text and "secret.example" not in text and "Inbox" not in text
        assert "productive" not in text and "distracting" not in text
        assert "com.apple.Safari".casefold() not in text.casefold()
    assert json.loads(imp["meta"])["latest_record_at"] == "2026-09-16T08:00:00+00:00"


def test_x_desktop_app_folds_into_the_x_source(conn, clk, settings):
    qb.sync(conn, clk, settings)
    x = conn.execute("SELECT s.kind, s.source_key, s.label FROM measurements m JOIN sources s "
                     "ON s.id = m.source_id WHERE m.item_key = 'maccatalyst.com.atebits.tweetie2'").fetchone()
    assert tuple(x) == ("x", "x", "wasteful")
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE kind = 'desktop_app' AND source_key LIKE '%tweetie%'"
                        ).fetchone()[0] == 0
    for bundle in ("maccatalyst.com.atebits.Tweetie2", "MACCATALYST.COM.ATEBITS.TWEETIE2",
                   "com.twitter.twitter-mac"):
        assert labels.normalise_key(T.KIND_DESKTOP_APP, bundle) == (T.KIND_X, "x")
    assert labels.normalise_key(T.KIND_DESKTOP_APP, " Com.Tinyspeck.SlackMacGap ") == \
        (T.KIND_DESKTOP_APP, "com.tinyspeck.slackmacgap")
    assert T.report_group(T.KIND_DESKTOP_APP) == T.GROUP_DESKTOP_APPS


def test_unchanged_is_a_duplicate_and_changed_minutes_replace_facts(conn, clk, settings, qb_db):
    first = qb.sync(conn, clk, settings)
    again = qb.sync(conn, clk, settings)
    assert again.duplicate_of == first.import_id
    add_rows(qb_db, [("2026-09-14T15:00:00", 3, 30)])
    third = qb.sync(conn, clk, settings)
    assert third.duplicate_of is None and third.import_id != first.import_id
    rows = conn.execute("SELECT minutes, import_id FROM measurements WHERE item_key = 'com.tinyspeck.slackmacgap' "
                        "AND period_start = '2026-09-14'").fetchall()
    assert [(r["minutes"], r["import_id"]) for r in rows] == [(3.0, third.import_id)]


def test_reparse_rewrites_the_same_facts_from_stored_evidence(conn, clk, settings, qb_db):
    first = qb.sync(conn, clk, settings)
    before = facts(conn)
    qb_db.unlink()                                             # reparse never needs the Qbserve database
    out = reparse.reparse_import(conn, clk, settings, first.import_id)
    assert out.status == "ok" and out.import_id != first.import_id and out.measurements_written == 5
    assert facts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM measurements WHERE import_id = ?", (first.import_id,)).fetchone()[0] == 0
    meta = json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?", (out.import_id,)).fetchone()["meta"])
    assert meta["reparse_of"] == first.import_id and meta["latest_record_at"] == "2026-09-16T08:00:00+00:00"


def test_unconfigured_never_reads_anything(conn, clk, tmp_path):
    plain = config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-09-13")
    assert plain.qbserve_db is None
    assert qb.sync(conn, clk, plain) is None
    assert jobs._qbserve(conn, clk, plain, Bot()).startswith("not configured")
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_days_after_qbserve_stopped_are_uncovered_not_zero(conn, settings, tmp_path):
    stopped = make_qb_db(tmp_path / "stopped.sqlite", rows=[("2026-09-14T08:00:00", 3, 60)])
    s = config.test_settings(**{**settings.__dict__, "qbserve_db": stopped})
    clk = clock.FrozenClock("2026-09-17T10:00:00+00:00")
    out = qb.sync(conn, clk, s)
    assert (out.period_start, out.period_end) == ("2026-09-14", "2026-09-14")

    silent = make_qb_db(tmp_path / "silent.sqlite", rows=[("2026-09-01T08:00:00", 3, 60)])
    s2 = config.test_settings(**{**settings.__dict__, "qbserve_db": silent})
    out = qb.sync(conn, clk, s2)
    assert out.status == "partial" and out.measurements_written == 0
    assert (out.period_start, out.period_end) == (None, None)


def test_staleness_hours(conn, clk, settings):
    assert qb.staleness_hours(conn, clk) is None
    qb.sync(conn, clk, settings)
    assert qb.staleness_hours(conn, clk) == pytest.approx(2.0)
    clk.set("2026-09-18T20:00:00+00:00")
    assert qb.staleness_hours(conn, clk) == pytest.approx(60.0)


# --- labels and double counting ------------------------------------------------------------

def test_labels_offer_desktop_apps_but_not_the_covered_spotify_client(conn, clk, settings):
    qb.sync(conn, clk, settings)
    offered = {(s.kind, s.source_key) for s in labels.sources_needing_label(conn, clk, settings, limit=50)}
    assert (T.KIND_DESKTOP_APP, "com.tinyspeck.slackmacgap") in offered
    assert (T.KIND_DESKTOP_APP, "com.apple.mail") in offered
    assert (T.KIND_DESKTOP_APP, "com.spotify.client") not in offered
    assert labels.list_sources(conn, kind=T.KIND_DESKTOP_APP)


def test_spotify_client_is_covered_elsewhere_and_excluded_from_pooled_minutes(conn, clk, settings):
    qb.sync(conn, clk, settings)
    week = T.allowance_week_start_for_day(date(2026, 9, 13), TZ)
    pooled = allowance.pooled_minutes(conn, settings, week)
    apps = pooled.by_group[T.GROUP_DESKTOP_APPS]
    assert apps.wasteful_min == pytest.approx(5.0)            # Slack 2.5 + Mail 2.5, unlabelled
    assert apps.covered_excluded_min == pytest.approx(2.0)    # the Spotify client
    assert pooled.by_group[T.GROUP_X].wasteful_min == pytest.approx(1.0)
    assert pooled.wasteful_min == pytest.approx(6.0)

    slack = labels.ensure_source(conn, clk, T.KIND_DESKTOP_APP, "com.tinyspeck.slackmacgap")
    labels.set_source_label(conn, clk, slack.id, T.WORTHWHILE)
    pooled = allowance.pooled_minutes(conn, settings, week)
    assert pooled.by_group[T.GROUP_DESKTOP_APPS].wasteful_min == pytest.approx(2.5)
    assert pooled.by_group[T.GROUP_DESKTOP_APPS].worthwhile_min == pytest.approx(2.5)

    split = allowance.gated_split(conn, settings, week)[T.GROUP_DESKTOP_APPS]
    assert split.gated_min == 0 and split.ungated_min == pytest.approx(5.0)
    assert split.provenances == frozenset({T.PROV_APP_TRACKER})


# --- daily job, report, dashboard ---------------------------------------------------------

def test_end_to_end_through_the_daily_job(conn, clk, settings):
    bot = Bot()
    failed = jobs.run_daily(conn, clk, settings, bot)
    assert "qbserve" not in failed
    assert conn.execute("SELECT COUNT(*) FROM imports WHERE kind = 'qbserve' AND status = 'ok'").fetchone()[0] == 1
    assert not any("Qbserve" in m["text"] for m in bot.sent)             # fresh: no warning

    week = T.allowance_week_start_for_day(date(2026, 9, 13), TZ)
    totals = report.weekly_totals(conn, clk, settings, week)
    apps = [t for t in totals if t.group == T.GROUP_DESKTOP_APPS]
    assert len(apps) == 1
    assert (apps[0].provenance, apps[0].device, apps[0].import_status) == (T.PROV_APP_TRACKER, "desktop", "ok")
    assert apps[0].minutes == pytest.approx(5.0) and apps[0].covered_excluded_min == pytest.approx(2.0)
    x = [t for t in totals if t.group == T.GROUP_X and t.provenance == T.PROV_APP_TRACKER]
    assert x and x[0].minutes == pytest.approx(1.0)

    gated = {r.group: r for r in report.gated_vs_ungated(conn, clk, settings, week)}
    assert gated[T.GROUP_DESKTOP_APPS].share_ungated == 1.0
    keepers = {k.group: k for k in report.cost_per_keeper(conn, clk, settings)}
    assert keepers[T.GROUP_DESKTOP_APPS].minutes == pytest.approx(5.0)
    assert keepers[T.GROUP_DESKTOP_APPS].provenances == (T.PROV_APP_TRACKER,)
    assert any("Qbserve" in b for b in keepers[T.GROUP_DESKTOP_APPS].blind_spots)
    recs = report.records(conn, clk, settings, group=T.GROUP_DESKTOP_APPS)
    assert {r.provenance for r in recs} == {T.PROV_APP_TRACKER} and len(recs) == 4
    assert [r.covered_elsewhere for r in recs if r.source == "Spotify"] == [
        "Spotify polling (podcasts only; music is out of scope)"]
    assert not any("Qbserve" in line for line in report.blind_spots(conn, clk, settings))
    split = report.label_split(conn, clk, settings)
    assert split[-1].wasteful_min == pytest.approx(6.0)

    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    app.config["TESTING"] = True
    client = app.test_client()
    html = client.get("/?week=2026-09-13").get_data(as_text=True)
    assert 'class="prov prov-apptracker"' in html and "Desktop apps" in html
    assert SECRET not in html
    records = client.get("/records?group=desktop_apps").get_data(as_text=True)
    assert 'class="prov prov-apptracker"' in records and "Slack" in records
    css = pathlib.Path(dashboard_app.__file__).parent / "static" / "dashboard.css"
    assert re.search(r"\.prov-apptracker\s*\{[^}]+\}", css.read_text())


def test_stale_qbserve_warns_once_a_day_until_the_diagnostic_ends(conn, clk, settings):
    jobs.run_daily(conn, clk, settings, Bot())                            # latest record 2026-09-16 08:00Z
    later = clock.FrozenClock("2026-09-18T09:00:00+00:00")                # 49 h later, nothing new recorded
    bot = Bot()
    assert "qbserve" not in jobs.run_daily(conn, later, settings, bot)
    warnings = [m["text"] for m in bot.sent if "Qbserve" in m["text"]]
    assert len(warnings) == 1
    assert "hasn't recorded anything since Wed 16 Sep 10:00" in warnings[0]
    assert "Desktop app time is missing" in warnings[0] and "open at login" in warnings[0]

    bot = Bot()
    jobs.run_daily(conn, later, settings, bot)                            # same day: not again
    assert not any("Qbserve" in m["text"] for m in bot.sent)
    assert any("Qbserve has not recorded since" in s for s in report.blind_spots(conn, later, settings))

    bot = Bot()
    jobs.run_daily(conn, clock.FrozenClock("2026-09-19T09:00:00+00:00"), settings, bot)
    assert len([m for m in bot.sent if "Qbserve" in m["text"]]) == 1      # next day: once more

    ended = clock.FrozenClock("2026-11-20T09:00:00+00:00")
    bot = Bot()
    jobs._qbserve_stale_warning(conn, ended, settings, bot)
    assert not bot.sent


def test_unreadable_database_fails_the_step_and_still_warns_when_stale(conn, clk, settings, tmp_path):
    jobs.run_daily(conn, clk, settings, Bot())
    gone = config.test_settings(**{**settings.__dict__, "qbserve_db": tmp_path / "gone.sqlite"})
    bot = Bot()
    failed = jobs.run_daily(conn, clock.FrozenClock("2026-09-18T09:00:00+00:00"), gone, bot)
    assert "qbserve" in failed
    assert len([m for m in bot.sent if "Qbserve" in m["text"]]) == 1


def test_blind_spots_when_configured_but_never_imported(conn, clk, settings, tmp_path):
    assert any("no desktop app time has been imported" in s for s in report.blind_spots(conn, clk, settings))
    plain = config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-09-13")
    assert not any("Qbserve" in s for s in report.blind_spots(conn, clk, plain))
    # Unconfigured, no week expects a Qbserve import; configured, a missing one says so.
    week = T.allowance_week_start_for_day(date(2026, 9, 13), TZ)
    assert not [t for t in report.weekly_totals(conn, clk, plain, week) if t.group == T.GROUP_DESKTOP_APPS]
    missing = [t for t in report.weekly_totals(conn, clk, settings, week) if t.group == T.GROUP_DESKTOP_APPS]
    assert [(t.minutes, t.import_status) for t in missing] == [(None, report.IMPORT_MISSING)]


def test_new_vocabulary_is_registered():
    assert T.KIND_DESKTOP_APP in T.SOURCE_KINDS and T.GROUP_DESKTOP_APPS in T.REPORT_GROUPS
    assert T.PROV_APP_TRACKER in T.PROVENANCES and "qbserve" in T.IMPORT_KINDS
    assert T.PROVENANCE_TEXT[T.PROV_APP_TRACKER] == "App tracker (Qbserve, this Mac)"
    assert report.IMPORT_KIND_FOR_PROVENANCE[T.PROV_APP_TRACKER] == "qbserve"
    assert T.COVERED_ELSEWHERE[(T.KIND_DESKTOP_APP, "com.spotify.client")]
    assert dashboard_app.PROVENANCE_BADGES[T.PROV_APP_TRACKER][1] == "apptracker"
    assert len({b[1] for b in dashboard_app.PROVENANCE_BADGES.values()}) == len(dashboard_app.PROVENANCE_BADGES)


# --- check_env step 9 ----------------------------------------------------------------------

def _check(env, capsys):
    import check_env

    check_env.failures.clear()
    check_env.check_qbserve(env)
    out = capsys.readouterr().out
    failed = list(check_env.failures)
    check_env.failures.clear()
    return out, failed


def test_check_env_qbserve_step(tmp_path, qb_db, capsys, monkeypatch):
    out, failed = _check({}, capsys)
    assert "[9/10]" in out and "desktop apps not measured" in out and not failed

    out, failed = _check({"DRBR_QBSERVE_DB": str(tmp_path / "nope.sqlite")}, capsys)
    assert failed and "does not exist" in out

    bad = tmp_path / "bad.sqlite"
    c = sqlite3.connect(str(bad))
    c.execute("CREATE TABLE Apps (_id INTEGER, name TEXT)")
    c.close()
    out, failed = _check({"DRBR_QBSERVE_DB": str(bad)}, capsys)
    assert failed and "changed shape" in out

    out, failed = _check({"DRBR_QBSERVE_DB": str(qb_db)}, capsys)
    assert not failed and "OK" in out and f"{len(APPS)} apps" in out and "2026-09-16T08:00+00:00" in out
    assert SECRET not in out and "Slack" not in out and "Inbox" not in out

    old = make_qb_db(tmp_path / "old.sqlite", rows=[("2026-01-05T08:00:00", 3, 60)])
    out, failed = _check({"DRBR_QBSERVE_DB": str(old)}, capsys)
    assert not failed and "WARN" in out and "open at login" in out    # stale is a note, never a failure


def test_days_before_qbserve_resumed_are_uncovered_not_zero(conn, settings, tmp_path):
    """The live case: Qbserve closed for days, then reopened. The gap is not zero use."""
    resumed = make_qb_db(tmp_path / "resumed.sqlite", rows=[
        ("2026-08-31T08:00:00", 3, 60),                      # before the gap
        ("2026-09-16T08:00:00", 3, 60), ("2026-09-16T08:01:00", 1, 60),   # reopened on the 16th
    ])
    s = config.test_settings(**{**settings.__dict__, "qbserve_db": resumed})
    out = qb.sync(conn, clock.FrozenClock("2026-09-17T10:00:00+00:00"), s)
    assert (out.period_start, out.period_end) == ("2026-09-16", "2026-09-16")
    assert out.measurements_written == 1


def test_a_window_with_no_records_is_uncovered_even_if_qbserve_recorded_later(conn, settings, tmp_path):
    """Live 2026-09-17: nothing 13-16 Sep, first record the morning of the 17th."""
    later = make_qb_db(tmp_path / "later.sqlite", rows=[("2026-09-17T09:00:00", 3, 60)])
    s = config.test_settings(**{**settings.__dict__, "qbserve_db": later})
    out = qb.sync(conn, clock.FrozenClock("2026-09-17T12:00:00+00:00"), s)
    assert out.status == "partial" and out.measurements_written == 0
    assert (out.period_start, out.period_end) == (None, None)
