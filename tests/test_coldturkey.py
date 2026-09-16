"""Cold Turkey website-statistics export: parse and ingest (CONTRACTS Part II §6.3).

`fixtures/coldturkey/website_statistics.json` has the real export's exact shape
(5 series, `label` + `data`, UTC-midnight epoch ms, int and float minutes, a
`file:` label) with synthetic domains and values. `websites_blocked.json` has
the shape of the real "Websites Blocked" count export.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pytest

import clock
import config
import db
import evidence
from sources import coldturkey as ct

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "coldturkey"
USAGE = (FIXTURES / "website_statistics.json").read_bytes()
BLOCKED = (FIXTURES / "websites_blocked.json").read_bytes()
TZ = "Europe/Warsaw"
SUN = 1789257600000          # 2026-09-13T00:00:00Z
DAY = 86_400_000


def doc(*series) -> bytes:
    return json.dumps(list(series)).encode()


def series(label, *values, start=SUN):
    return {"label": label, "data": [[start + i * DAY, v] for i, v in enumerate(values)]}


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw")


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-21T07:00:00+00:00")     # Monday after the week


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


def rows(conn):
    return conn.execute("SELECT m.*, s.kind, s.source_key FROM measurements m JOIN sources s ON s.id = m.source_id "
                        "ORDER BY m.period_start, s.source_key, m.item_key").fetchall()


# --- parse ---------------------------------------------------------------------------

def test_parse_real_shaped_export():
    p = ct.parse(USAGE)
    assert len(p.rows) == 35
    assert p.days == [date(2026, 9, 13 + i) for i in range(7)]
    assert p.warnings == []
    assert p.listed_domains == 5
    assert ("file:", date(2026, 9, 13), 4.5) in p.rows
    assert ("example.org", date(2026, 9, 14), 9.0) in p.rows


def test_utc_midnight_is_that_calendar_date_not_shifted_to_local():
    # 2026-09-13T00:00Z is 02:00 in Warsaw and still the 13th; a west-of-UTC reading would say the 12th.
    p = ct.parse(doc(series("example.org", 5)))
    assert p.rows == [("example.org", date(2026, 9, 13), 5.0)]


def test_blocked_count_export_is_rejected_with_a_clear_message():
    with pytest.raises(ct.ColdTurkeyParseError, match="counts blocks, not time"):
        ct.parse(BLOCKED)


@pytest.mark.parametrize("data,match", [
    (b"{not json", "not valid JSON"),
    (b"\xff\xfe", "not valid JSON"),
    (b'{"label": "x.com"}', "list of chart series"),
    (b"[]", "no domains"),
    (b"[1]", "not an object"),
    (json.dumps([{"label": "a.com"}]).encode(), "missing data"),
    (json.dumps([{"data": [[SUN, 1]]}]).encode(), "missing label"),
    (json.dumps([{"label": "a.com", "data": []}]).encode(), "no data points"),
    (json.dumps([{"label": "", "data": [[SUN, 1]]}]).encode(), "no domain label"),
    (json.dumps([{"label": "a.com", "data": [[SUN]]}]).encode(), "not \\[timestamp, minutes\\]"),
    (json.dumps([{"label": "a.com", "data": [[SUN, "12"]]}]).encode(), "non-numeric"),
    (json.dumps([{"label": "a.com", "data": [[SUN, None]]}]).encode(), "non-numeric"),
    (json.dumps([{"label": "a.com", "data": [[SUN, True]]}]).encode(), "non-numeric"),
    (json.dumps([{"label": "a.com", "data": [[str(SUN), 1]]}]).encode(), "non-integer timestamp"),
    (json.dumps([{"label": "a.com", "data": [[SUN, -1]]}]).encode(), "impossible"),
    (json.dumps([{"label": "a.com", "data": [[SUN, 2000]]}]).encode(), "impossible"),
    (json.dumps([{"label": "a.com", "data": [[SUN + 3_600_000, 1]]}]).encode(), "not a day boundary"),
    (json.dumps([{"label": "Total Time", "data": [[SUN, 1]]}]).encode(), "not a website domain"),
])
def test_malformed_exports_raise(data, match):
    with pytest.raises(ct.ColdTurkeyParseError, match=match):
        ct.parse(data)


def test_unexpected_fields_are_rejected_not_assumed():
    with pytest.raises(ct.ColdTurkeyParseError, match="never seen \\(unit\\)"):
        ct.parse(doc({**series("a.com", 1), "unit": "hours"}))


def test_identical_duplicate_is_kept_once_with_a_warning():
    p = ct.parse(doc(series("a.com", 3, 4), series("www.a.com", 3)))
    assert [r for r in p.rows if r[1] == date(2026, 9, 13)] == [("a.com", date(2026, 9, 13), 3.0)]
    assert len(p.warnings) == 1 and "kept once" in p.warnings[0]
    assert p.listed_domains == 1


def test_conflicting_duplicate_is_unreadable_not_guessed():
    p = ct.parse(doc(series("a.com", 3), series("a.com", 5)))
    assert p.rows == [("a.com", date(2026, 9, 13), None)]
    assert "conflicting values" in p.warnings[0]


def test_x_domains_are_distinct_facts():
    p = ct.parse(doc(series("x.com", 3), series("twitter.com", 5)))
    assert sorted(p.rows) == [("twitter.com", date(2026, 9, 13), 5.0), ("x.com", date(2026, 9, 13), 3.0)]
    assert p.warnings == [] and p.listed_domains == 2


# --- ingest ----------------------------------------------------------------------------

def test_ingest_full_past_week(conn, clk, settings):
    out = ct.ingest(conn, clk, settings, USAGE, filename="Cold Turkey Blocker Website Statistics.json")
    assert (out.status, out.measurements_written) == ("ok", 35)
    assert (out.period_start, out.period_end) == ("2026-09-13", "2026-09-19")
    got = rows(conn)
    assert {(r["device"], r["provenance"], r["status"]) for r in got} == {("desktop", "manual_export", "ok")}
    assert all(r["period_start"] == r["period_end"] for r in got)
    zero = [r for r in got if r["source_key"] == "news.example.com" and r["period_start"] == "2026-09-14"]
    assert zero[0]["minutes"] == 0.0                       # a finished day's listed zero is a measurement
    meta = json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?", (out.import_id,)).fetchone()["meta"])
    assert meta["listed_domain_count"] == 5
    assert "unmeasured" in meta["unlisted_domains"]
    assert meta["filename"] == "Cold Turkey Blocker Website Statistics.json"
    assert evidence.get_meta(conn, "diagnostic_start") == "2026-09-13"


def test_x_com_folds_into_the_x_source(conn, clk, settings):
    ct.ingest(conn, clk, settings, doc(series("x.com", 3), series("twitter.com", 5)))
    got = rows(conn)
    assert {(r["kind"], r["source_key"], r["item_key"], r["minutes"]) for r in got} == {
        ("x", "x", "twitter.com", 5.0), ("x", "x", "x.com", 3.0)}


def test_days_not_yet_over_are_not_recorded(conn, settings):
    wednesday = clock.FrozenClock("2026-09-16T10:39:00+00:00")
    out = ct.ingest(conn, wednesday, settings, USAGE)
    assert out.status == "partial"
    assert out.measurements_written == 15                    # Sun, Mon, Tue x 5 domains
    assert (out.period_start, out.period_end) == ("2026-09-13", "2026-09-15")
    assert any("hadn't finished" in w for w in out.warnings)
    assert max(r["period_start"] for r in rows(conn)) == "2026-09-15"


def test_conflicting_duplicate_is_stored_null_with_status(conn, clk, settings):
    out = ct.ingest(conn, clk, settings, doc(series("a.com", 3), series("a.com", 5)))
    assert out.status == "partial"
    (r,) = rows(conn)
    assert (r["minutes"], r["status"]) == (None, "unreadable")


def test_blocked_export_is_a_failed_import_with_raw_kept(conn, clk, settings):
    out = ct.ingest(conn, clk, settings, BLOCKED, filename="Cold Turkey Blocker Statistics.json")
    assert out.status == "failed" and "counts blocks" in out.error
    row = conn.execute("SELECT * FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    assert row["status"] == "failed"
    assert evidence.raw_file(settings, row["raw_path"]).read_bytes() == BLOCKED
    assert rows(conn) == []


def test_duplicate_file_returns_duplicate_of(conn, clk, settings):
    first = ct.ingest(conn, clk, settings, USAGE)
    again = ct.ingest(conn, clk, settings, USAGE)
    assert again.duplicate_of == first.import_id
    assert again.measurements_written == 0
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


def test_reparse_writes_a_new_import_using_the_original_receive_time(conn, settings):
    wednesday = clock.FrozenClock("2026-09-16T10:39:00+00:00")
    first = ct.ingest(conn, wednesday, settings, USAGE)
    later = clock.FrozenClock("2026-09-25T10:00:00+00:00")
    out = ct.reparse(conn, later, settings, first.import_id)
    assert out.import_id != first.import_id
    assert out.measurements_written == 15                    # still judged against the Wednesday export
    assert {r["import_id"] for r in rows(conn)} == {out.import_id}


REAL = pathlib.Path("/Users/Jacob/Projects/drbr/inputs")


def test_real_exports_local_only():
    usage, blocked = REAL / "Cold Turkey Blocker Website Statistics.json", REAL / "Cold Turkey Blocker Statistics.json"
    if not (usage.exists() and blocked.exists()):
        pytest.skip("real Cold Turkey exports are local-only")
    p = ct.parse(usage.read_bytes())
    assert p.listed_domains == 5 and len(p.days) == 7 and p.warnings == []
    with pytest.raises(ct.ColdTurkeyParseError):
        ct.parse(blocked.read_bytes())
