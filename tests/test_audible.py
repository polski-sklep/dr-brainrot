"""Audiobooks: Audible's own daily listening totals, split across books (DR5.28).

His decision, 8 Oct 2026: "Yes, I want it; albeit not considered as brainrot".
So audiobooks are measured, each book is a source labelled worthwhile as his
standing decision, and none of it is ever charged as brain rot.

No test reaches the real Audible client or the network: `conftest.py` puts a
stub in `sys.modules["audible"]` that fails on any use, and every read here is
given a fake client. No test reads the real sign-in or store file.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import pathlib
import re
import stat
import sys
import types
import warnings
from datetime import date, timedelta

import pytest

import allowance
import checklist
import clock
import config
import db
import dr_brainrot_types as T
import evidence
import jobs
import labels
import pace
import reparse
import report
from dashboard import app as dashboard_app
from sources import audible

TZ = "Europe/Warsaw"
CSS = pathlib.Path(dashboard_app.__file__).parent / "static" / "dashboard.css"


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-09-13",
                                weekly_allowance_min=360, db_path=tmp_path / "dr-brainrot.db")


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-10-09T07:30:00+00:00")        # Fri 9 Oct, 09:30 in Warsaw


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


def _screentime_row(conn, clk, settings, app: str, minutes: float, *, start="2026-09-20",
                    end="2026-09-26") -> int:
    """One Screen Time week figure for an iPhone app, as the screenshot reader stores it."""
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="test")
    source = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, app, app)
    evidence.upsert_measurement(conn, rec, source_id=source.id, device=T.DEVICE_IPHONE,
                                provenance=T.PROV_SCREENSHOT, period_start=start, period_end=end,
                                minutes=minutes)
    evidence.finish_import(conn, clk, settings, rec, period_start=start, period_end=end)
    return source.id


# --- vocabulary (commit: "Name audiobooks everywhere") ----------------------------

def test_new_vocabulary_is_registered():
    assert T.KIND_AUDIOBOOK in T.SOURCE_KINDS and T.GROUP_AUDIOBOOKS in T.REPORT_GROUPS
    assert T.GROUP_AUDIOBOOKS not in T.KEEPER_GROUPS                 # no vault path to audiobooks
    assert T.PROV_AUDIBLE_STATS in T.PROVENANCES and "audible" in T.IMPORT_KINDS
    assert T.PROVENANCE_TEXT[T.PROV_AUDIBLE_STATS] == "Audible's own listening stats"
    assert T.report_group(T.KIND_AUDIOBOOK, T.DEVICE_ACCOUNT) == T.GROUP_AUDIOBOOKS
    assert report.IMPORT_KIND_FOR_PROVENANCE[T.PROV_AUDIBLE_STATS] == "audible"
    assert report.GROUP_TEXT[T.GROUP_AUDIOBOOKS] == "Audiobooks"
    assert report.GROUP_BLIND_SPOTS[T.GROUP_AUDIOBOOKS]
    assert dashboard_app.GROUP_NAMES[T.GROUP_AUDIOBOOKS] == "Audiobooks"
    assert dashboard_app.KIND_NAMES[T.KIND_AUDIOBOOK] == "audiobook"
    assert dashboard_app.IMPORT_KIND_NAMES["audible"] == "Audible"
    assert dashboard_app.PROVENANCE_BADGES[T.PROV_AUDIBLE_STATS] == ("listening stats", "audiblestats")
    badges = dashboard_app.PROVENANCE_BADGES.values()
    assert len({b[0] for b in badges}) == len({b[1] for b in badges}) == len(dashboard_app.PROVENANCE_BADGES)
    assert labels.PROVENANCE_SEEN[T.PROV_AUDIBLE_STATS] == "Audible listening stats"


def test_the_badge_has_a_css_rule_of_its_own():
    css = CSS.read_text()
    rules = {m.group(1): m.group(2).strip() for m in re.finditer(r"\.prov-([a-z]+)\s*\{([^}]*)\}", css)}
    assert "audiblestats" in rules
    others = [body for name, body in rules.items() if name != "audiblestats"]
    assert rules["audiblestats"] not in others


def test_an_asin_is_its_own_key_and_never_folds_into_x():
    assert labels.normalise_key(T.KIND_AUDIOBOOK, " B0TEST0001 ") == (T.KIND_AUDIOBOOK, "B0TEST0001")
    assert labels.normalise_key(T.KIND_AUDIOBOOK, T.UNKNOWN_AUDIOBOOK_KEY) == (T.KIND_AUDIOBOOK, "unknown")


def test_a_standing_label_is_written_only_when_the_source_is_created(conn, clk):
    made = labels.ensure_source(conn, clk, T.KIND_AUDIOBOOK, "B0TEST0001", "Test Book One",
                                label_on_create=T.WORTHWHILE)
    assert (made.label, made.labelled_at) == (T.WORTHWHILE, clock.iso(clk.now()))
    assert labels.resolve(conn, source_id=made.id) == labels.ResolvedLabel(T.WORTHWHILE, T.BASIS_SOURCE_LABEL)

    labels.set_source_label(conn, clk, made.id, T.WASTEFUL)          # his later word on one book
    clk.advance(days=1)
    again = labels.ensure_source(conn, clk, T.KIND_AUDIOBOOK, "B0TEST0001", "Test Book One",
                                 label_on_create=T.WORTHWHILE)
    assert again.id == made.id and again.label == T.WASTEFUL         # his label wins

    plain = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "Some App")
    assert plain.label is None and plain.labelled_at is None         # no standing label: nothing written
    with pytest.raises(labels.LabelError):
        labels.ensure_source(conn, clk, T.KIND_AUDIOBOOK, "B0TEST0002", label_on_create="great")


def test_screen_times_audible_app_is_covered_elsewhere_even_labelled_tool(conn, clk, settings):
    assert T.covered_elsewhere(T.KIND_IPHONE_APP, "audible") == (
        "Audible's own listening stats (Screen Time only sees the app while the screen is on)")
    app_id = _screentime_row(conn, clk, settings, "Audible", 2.0)
    _screentime_row(conn, clk, settings, "Instagram", 30.0)
    labels.set_source_label(conn, clk, app_id, T.TOOL)                # as it is on the live database

    week = T.allowance_week_start_for_day(date(2026, 9, 20), TZ)
    iphone = [t for t in report.weekly_totals(conn, clk, settings, week) if t.group == T.GROUP_IPHONE]
    assert len(iphone) == 1
    assert iphone[0].covered_excluded_min == pytest.approx(2.0)
    assert iphone[0].tool_min == 0.0 and iphone[0].minutes == pytest.approx(30.0)
    assert dict(iphone[0].covered_reasons) == {T.covered_elsewhere(T.KIND_IPHONE_APP, "audible"): 2.0}
    pooled = allowance.pooled_minutes(conn, settings, week)
    assert pooled.tool_min == 0.0 and pooled.covered_excluded_min == pytest.approx(2.0)

    labels.set_source_label(conn, clk, app_id, T.WASTEFUL)            # whatever its label, never charged
    assert allowance.pooled_minutes(conn, settings, week).charged_min == pytest.approx(30.0)
    conn.execute("UPDATE sources SET label = NULL WHERE id = ?", (app_id,))
    assert app_id not in {r.source_id for r in labels.label_queue(conn, clk, settings)}


# ==========================================================================
# The reader (commit: "Read Audible's listening stats")
# ==========================================================================

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "audible"
BW, UH, LF = "B0TEST0001", "B0TEST0002", "B0TEST0003"


class NetworkError(Exception):
    """Named like the library's own: classified by name, never imported."""


class Unauthorized(Exception):
    pass


def ms(minutes: float) -> float:
    return minutes * 60_000


def item(asin: str, title: str, runtime_min: int, remaining_s: int | None, *,
         finished_at: str | None = None, is_finished: bool = False) -> dict:
    """A library item with the keys the real one carries for what is read."""
    return {"asin": asin, "title": title, "runtime_length_min": runtime_min, "percent_complete": 0.0,
            "is_finished": is_finished, "content_type": "Product", "format_type": "unabridged",
            "listening_status": {"finished_at_timestamp": finished_at, "is_finished": is_finished,
                                 "percent_complete": 0.0, "time_remaining_seconds": remaining_s}}


def library(bw: int = 40380, uh: int = 31020, lf: int = 46740, *, lf_finished: str | None = None) -> list[dict]:
    return [item(BW, "Test Book One", 673, bw),
            item(UH, "Test Book Two", 517, uh, finished_at="2026-10-05T13:41:39.015Z"),
            item(LF, "Test Book Three", 779, lf, finished_at=lf_finished, is_finished=lf_finished is not None)]


class FakeClient:
    """Answers like the real API: only days with listening, inside the 30 days asked for."""

    def __init__(self, days: dict[str, float], books: list[dict] | None = None, *,
                 stats_error: Exception | None = None, library_error: Exception | None = None):
        self.days, self.books = dict(days), books
        self.stats_error, self.library_error = stats_error, library_error
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == audible.STATS_PATH:
            if self.stats_error is not None:
                raise self.stats_error
            start = date.fromisoformat(params["daily_listening_interval_start_date"])
            end = start + timedelta(days=int(params["daily_listening_interval_duration"]) - 1)
            return {"aggregated_daily_listening_stats": [
                {"aggregated_sum": value, "interval_identifier": day, "unit": "Milliseconds"}
                for day, value in sorted(self.days.items())
                if start <= date.fromisoformat(day) <= end and value > 0]}
        if path == audible.LIBRARY_PATH:
            if self.library_error is not None:
                raise self.library_error
            return {"items": copy.deepcopy(self.books or []), "total_results": len(self.books or [])}
        raise AssertionError(f"unexpected path {path}")

    def stats_starts(self) -> list[str]:
        return [p["daily_listening_interval_start_date"] for path, p in self.calls if path == audible.STATS_PATH]


#: The real daily totals from the probe of 8 Oct (minutes), go-live onwards.
REAL_DAYS = {"2026-09-14": 3391902.0, "2026-09-17": 5189783.0, "2026-09-19": 3571852.0,
             "2026-09-22": 537364.0, "2026-09-26": 8118108.0, "2026-09-28": 8408101.0,
             "2026-09-29": 4028611.0, "2026-10-02": 2129089.0, "2026-10-03": 1824061.0,
             "2026-10-05": 5009025.0,
             # Before go-live: never asked for, never counted.
             "2026-09-08": 1667332.0, "2026-09-11": 2667411.0}


@pytest.fixture
def signed_in(tmp_path, settings):
    """Settings signed in to Audible, with throwaway files in tmp_path (never the real ones)."""
    auth = tmp_path / "data" / "audible_auth.json"
    auth.parent.mkdir()
    auth.write_text(json.dumps({"adp_token": "not-a-real-token"}))
    auth.chmod(0o600)
    audible.store_file(auth).write_text(json.dumps({"country_code": "uk", "titles": 3}))
    return config.test_settings(**{**settings.__dict__, "audible_auth": auth})


def rows(conn, day: str | None = None) -> list[dict]:
    sql = ("SELECT s.source_key, s.display_name, s.label, m.period_start, m.minutes, m.detail, m.device "
           "FROM measurements m JOIN sources s ON s.id = m.source_id WHERE m.provenance = 'audible_stats'")
    params: tuple = ()
    if day:
        sql += " AND m.period_start = ?"
        params = (day,)
    return [dict(r) | {"detail": json.loads(r["detail"])}
            for r in conn.execute(sql + " ORDER BY m.period_start, s.source_key", params)]


def day_totals(conn) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows(conn):
        if r["minutes"] is not None:                                    # an unmeasured day has no figure
            out[r["period_start"]] = round(out.get(r["period_start"], 0.0) + r["minutes"], 2)
    return out


def week_of(day: str) -> str:
    return T.allowance_week_start_for_day(date.fromisoformat(day), TZ)


def audiobook_total(conn, clk, settings, day: str) -> report.SourceTotal | None:
    found = [t for t in report.weekly_totals(conn, clk, settings, week_of(day)) if t.group == T.GROUP_AUDIOBOOKS]
    assert len(found) <= 1
    return found[0] if found else None


# --- set-up ------------------------------------------------------------------------

def test_not_signed_in_reads_nothing_and_asks_for_nothing(conn, clk, settings, tmp_path):
    missing = tmp_path / "nowhere" / "audible_auth.json"
    no_store = tmp_path / "solo" / "audible_auth.json"
    no_store.parent.mkdir()
    no_store.write_text("{}")
    odd_store = tmp_path / "odd" / "audible_auth.json"
    odd_store.parent.mkdir()
    odd_store.write_text("{}")
    audible.store_file(odd_store).write_text(json.dumps({"country_code": "zz"}))
    for path in (None, missing, no_store, odd_store):
        s = config.test_settings(**{**settings.__dict__, "audible_auth": path})
        assert audible.setup(s) is None
        assert audible.sync(conn, clk, s, client=FakeClient(REAL_DAYS, library())) is None
        assert not report.expects("audible", s)
        assert audiobook_total(conn, clk, s, "2026-09-14") is None          # no row: not owed
        assert not any("Audible" in line or "udiobook" in line for line in report.blind_spots(conn, clk, s))
        row = next(r for r in report.needs_attention(conn, clk, s) if r.group == T.GROUP_AUDIOBOOKS)
        assert (row.state, row.action_text, row.detail) == (report.STATE_WAITING, "", report.AUDIBLE_NOT_SET_UP)
        assert not report.audible_needs_signin(conn, s)
        needs = checklist.week_needs(conn, clk, s, T.measurement_week(date(2026, 10, 4)))
        assert "audiobooks" not in checklist._automatic(needs, s) and not needs.audible_signin
    assert conn.execute("SELECT COUNT(*) FROM imports WHERE kind = 'audible'").fetchone()[0] == 0


def test_setup_reads_the_store_beside_the_sign_in(signed_in):
    found = audible.setup(signed_in)
    assert found == audible.Setup(signed_in.audible_auth, "uk")
    assert audible.store_file(found.auth_path).name == "audible_store.json"


# --- which days ------------------------------------------------------------------

def test_requests_overlap_so_no_day_falls_between_them_and_today_is_never_counted(conn, signed_in):
    clk = clock.FrozenClock("2026-11-01T08:30:00+00:00")             # Sun 1 Nov, 09:30 in Warsaw (CET)
    days = {"2026-09-13": ms(10), "2026-10-11": ms(20), "2026-10-12": ms(30), "2026-10-31": ms(40),
            "2026-11-01": ms(99)}                                       # today: never counted
    client = FakeClient(days, library())
    outcome = audible.sync(conn, clk, signed_in, client=client)
    assert (outcome.status, outcome.period_start, outcome.period_end) == ("ok", "2026-09-13", "2026-10-31")
    assert client.stats_starts() == ["2026-09-13", "2026-10-11"]      # 28 days apart: overlapping 30-day asks
    for path, params in client.calls:
        if path == audible.STATS_PATH:
            assert params["daily_listening_interval_duration"] == "30" and params["store"] == "Audible"
    assert day_totals(conn) == {"2026-09-13": 10.0, "2026-10-11": 20.0, "2026-10-12": 30.0, "2026-10-31": 40.0}
    assert audible.chunk_starts(date(2026, 9, 13), date(2026, 9, 13)) == [date(2026, 9, 13)]


def test_window_backfills_from_go_live_then_reads_eight_days_and_catches_up_after_a_gap(conn, signed_in):
    assert audible.window(conn, clock.FrozenClock("2026-09-15T07:30:00+00:00"), signed_in) == (
        date(2026, 9, 13), date(2026, 9, 14))                            # never before go-live
    assert audible.window(conn, clock.FrozenClock("2026-09-13T07:30:00+00:00"), signed_in) is None
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    assert audible.window(conn, clk, signed_in) == (date(2026, 9, 13), date(2026, 10, 8))
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    clk.advance(hours=3)                                                # Sync now, the same day: same days
    assert audible.window(conn, clk, signed_in) == (date(2026, 9, 13), date(2026, 10, 8))
    clk.set("2026-10-10T07:30:00+00:00")
    assert audible.window(conn, clk, signed_in) == (date(2026, 10, 2), date(2026, 10, 9))
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    clk.set("2026-10-22T07:30:00+00:00")                               # twelve days away
    assert audible.window(conn, clk, signed_in) == (date(2026, 10, 10), date(2026, 10, 21))


def test_a_day_missing_from_a_good_read_is_zero_and_an_unread_week_is_unmeasured(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    sept14 = [r for r in rows(conn) if r["period_start"] <= "2026-09-19"]
    assert [r["period_start"] for r in sept14] == ["2026-09-14", "2026-09-17", "2026-09-19"]
    assert all(r["minutes"] > 0 for r in rows(conn))                    # no zero rows: zero is the absence
    week = audiobook_total(conn, clk, signed_in, "2026-09-14")
    assert week.minutes == pytest.approx(202.56) and week.import_status == "ok"
    assert audiobook_total(conn, clk, signed_in, "2026-09-20").minutes == pytest.approx(144.26)
    assert audiobook_total(conn, clk, signed_in, "2026-09-27").minutes == pytest.approx(273.16)
    assert audiobook_total(conn, clk, signed_in, "2026-10-04").minutes == pytest.approx(83.48)
    assert sum(day_totals(conn).values()) == pytest.approx(703.46, abs=0.011)
    assert "2026-09-08" not in day_totals(conn) and "2026-09-11" not in day_totals(conn)   # before go-live

    later = audiobook_total(conn, clk, signed_in, "2026-10-11")       # this week: nothing read yet
    assert (later.minutes, later.import_status) == (None, report.IMPORT_NOT_DUE)
    clk.set("2026-10-19T07:00:00+00:00")
    later = audiobook_total(conn, clk, signed_in, "2026-10-11")
    assert (later.minutes, later.import_status) == (None, report.IMPORT_MISSING)


# --- failures ----------------------------------------------------------------------

def test_an_unreachable_audible_records_a_failure_and_leaves_days_unmeasured(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    before = rows(conn)
    for day in ("2026-10-10", "2026-10-15", "2026-10-19"):
        clk.set(f"{day}T07:30:00+00:00")
        outcome = audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, stats_error=NetworkError("x")))
        assert outcome.status == "failed"
        assert outcome.error == "Audible couldn't be reached (NetworkError). The next morning's read tries again."
    assert rows(conn) == before                                          # nothing deleted, nothing zeroed
    failed = conn.execute("SELECT * FROM imports WHERE kind = 'audible' ORDER BY id DESC").fetchone()
    assert json.loads(failed["meta"])["failure"] == "unreachable"
    assert failed["raw_path"] is None and failed["period_start"] is None

    week = audiobook_total(conn, clk, signed_in, "2026-10-11")
    assert (week.minutes, week.import_status) == (None, report.IMPORT_MISSING)   # unmeasured, never 0
    spots = [s for s in report.blind_spots(conn, clk, signed_in) if "Audible" in s or "udiobook" in s]
    assert spots == ["Audible's listening stats couldn't be read on Mon 19 Oct 09:30; days after 2026-10-08 "
                     "are unmeasured, not zero. The next morning's read tries again."]
    row = next(r for r in report.needs_attention(conn, clk, signed_in) if r.group == T.GROUP_AUDIOBOOKS)
    assert (row.state, row.action_text) == (report.STATE_ATTENTION, "")
    assert "2026-10-08" in row.detail
    assert not report.audible_needs_signin(conn, signed_in)

    clk.set("2026-10-20T07:30:00+00:00")                                # back: the gap is read whole
    outcome = audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS | {"2026-10-12": ms(15)}, library()))
    assert (outcome.status, outcome.period_start, outcome.period_end) == ("ok", "2026-10-09", "2026-10-19")
    assert report.audible_failure(conn) is None
    assert audiobook_total(conn, clk, signed_in, "2026-10-11").minutes == pytest.approx(15.0)


class Doctored(FakeClient):
    """Answers like FakeClient, then lets a test spoil the daily totals of each stats answer."""

    def __init__(self, days, books=None, *, spoil=None, **kw):
        super().__init__(days, books, **kw)
        self.spoil = spoil

    def get(self, path, **params):
        out = super().get(path, **params)
        if path == audible.STATS_PATH and self.spoil is not None:
            out = self.spoil(out)
        return out


def test_an_answer_of_a_new_shape_fails_whole_and_keeps_the_evidence(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    before = rows(conn)

    clk.set("2026-10-10T07:30:00+00:00")
    renamed = Doctored(REAL_DAYS, library(), spoil=lambda out: {"daily_stats": out["aggregated_daily_listening_stats"]})
    outcome = audible.sync(conn, clk, signed_in, client=renamed)
    assert outcome.status == "failed" and "no list of daily totals" in outcome.error
    assert rows(conn) == before                                          # the window's delete rolled back
    failed = conn.execute("SELECT * FROM imports WHERE id = ?", (outcome.import_id,)).fetchone()
    assert json.loads(failed["meta"])["failure"] == "unexpected"
    assert evidence.raw_file(signed_in, failed["raw_path"]).exists()   # kept, for a reparse once fixed
    # A new shape doesn't mend itself overnight, so the words don't promise that it will.
    [spot] = [s for s in report.blind_spots(conn, clk, signed_in) if "Audible" in s]
    assert "The next morning's read tries again." not in spot
    assert "unmeasured, not zero" in spot and "needs updating" in spot


def test_a_refused_sign_in_asks_once_for_the_exact_command(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    clk.set("2026-10-11T07:30:00+00:00")                                # Sunday 11 Oct
    outcome = audible.sync(conn, clk, signed_in, client=FakeClient({}, stats_error=Unauthorized("401 ...")))
    assert outcome.status == "failed" and audible.SIGN_IN_COMMAND in outcome.error
    assert json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?",
                                   (outcome.import_id,)).fetchone()[0])["failure"] == "sign_in"
    assert report.audible_needs_signin(conn, signed_in)
    spots = [s for s in report.blind_spots(conn, clk, signed_in) if "Audible" in s]
    assert len(spots) == 1 and audible.SIGN_IN_COMMAND in spots[0] and "unmeasured, not zero" in spots[0]
    row = next(r for r in report.needs_attention(conn, clk, signed_in) if r.group == T.GROUP_AUDIOBOOKS)
    assert row.state == report.STATE_ATTENTION and audible.SIGN_IN_COMMAND in row.action_text

    week = T.measurement_week(date(2026, 10, 4))
    needs = checklist.week_needs(conn, clk, signed_in, week)
    assert needs.audible_signin and needs.owed                          # owed for Screen Time and YouTube
    text = checklist.render(needs, signed_in, nudge=False)
    assert text.count(checklist.AUDIBLE_SIGNIN_LINE) == 1
    line = next(ln for ln in text.splitlines() if "Audible" in ln)
    assert line == checklist.AUDIBLE_SIGNIN_LINE and not re.match(r"\d+\.", line)   # never a numbered step
    assert audible.SIGN_IN_COMMAND in checklist.AUDIBLE_SIGNIN_LINE
    assert "audiobooks" not in checklist._automatic(needs, signed_in)
    assert checklist.AUDIBLE_SIGNIN_LINE not in checklist.render(needs, signed_in, nudge=True)

    quiet = checklist.WeekNeeds(week, checklist.IN, checklist.IN, None, 0, labels.Summary(0, 0.0, None),
                                False, (), True)
    assert not quiet.owed                                               # never a reason to send by itself


def test_a_healthy_read_lists_audiobooks_among_what_collects_itself(conn, signed_in):
    clk = clock.FrozenClock("2026-10-11T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    needs = checklist.week_needs(conn, clk, signed_in, T.measurement_week(date(2026, 10, 4)))
    assert checklist._automatic(needs, signed_in) == ("Everything else (podcasts, audiobooks, your vault) "
                                                      "collects itself.")
    assert checklist.AUDIBLE_SIGNIN_LINE not in checklist.render(needs, signed_in, nudge=False)
    row = next(r for r in report.needs_attention(conn, clk, signed_in) if r.group == T.GROUP_AUDIOBOOKS)
    assert (row.state, row.action_text) == (report.STATE_OK, "")
    assert row.covered is False                     # Sunday: no day of this week has finished yet
    assert not [s for s in report.blind_spots(conn, clk, signed_in) if "Audible" in s]
    clk.set("2026-10-13T07:30:00+00:00")                                # two mornings with no read
    assert [s for s in report.blind_spots(conn, clk, signed_in) if "Audible" in s] == [
        "Audible's listening stats were last read on Sun 11 Oct 09:30; later days are unmeasured, not zero."]
    row = next(r for r in report.needs_attention(conn, clk, signed_in) if r.group == T.GROUP_AUDIOBOOKS)
    assert (row.state, row.action_text) == (report.STATE_STALE, "")


def test_a_read_that_fails_to_load_the_sign_in_file_is_a_sign_in_to_redo(conn, signed_in, monkeypatch):
    fake = types.ModuleType("audible")

    class Authenticator:
        @staticmethod
        def from_file(path):
            raise ValueError("garbled: not-a-real-token")

    fake.Authenticator = Authenticator
    monkeypatch.setitem(sys.modules, "audible", fake)
    outcome = audible.sync(conn, clock.FrozenClock("2026-10-09T07:30:00+00:00"), signed_in)
    assert outcome.status == "failed" and "not-a-real-token" not in outcome.error
    assert outcome.error == ("The saved Audible sign-in couldn't be read (ValueError). Sign in again: "
                             + audible.SIGN_IN_COMMAND)
    assert report.audible_needs_signin(conn, signed_in)


def test_classify_by_name_never_by_text():
    class HTTPStatusError(Exception):
        def __init__(self, status):
            super().__init__("secret-token-in-a-url")
            self.response = types.SimpleNamespace(status_code=status)

    class ServerError(Exception):
        pass

    class ConnectError(ConnectionError):
        pass

    assert audible.classify(Unauthorized("secret"))[0] == "sign_in"
    assert audible.classify(HTTPStatusError(400))[0] == "sign_in"
    assert audible.classify(HTTPStatusError(502))[0] == "unreachable"
    assert audible.classify(ServerError())[0] == "unreachable"
    assert audible.classify(ConnectError())[0] == "unreachable"
    assert audible.classify(KeyError("x")) == ("unexpected", "Audible's answer wasn't what was expected "
                                                             "(KeyError); nothing was read.")
    for exc in (Unauthorized("secret-token"), HTTPStatusError(401), KeyError("secret-token")):
        assert "secret" not in audible.classify(exc)[1]


# --- duplicates --------------------------------------------------------------------

def test_an_unchanged_read_is_a_duplicate_and_still_counts_as_a_read(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))     # the backfill
    clk.set("2026-10-10T07:30:00+00:00")
    first = audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    clk.advance(minutes=30)
    outcome = audible.sync(conn, clk, signed_in, client=FakeClient({}, stats_error=NetworkError()))
    assert outcome.status == "failed" and report.audible_failure(conn) is not None
    clk.advance(minutes=30)
    again = audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    assert again.duplicate_of == first.import_id
    assert conn.execute("SELECT COUNT(*) FROM imports WHERE kind = 'audible' AND status = 'ok'").fetchone()[0] == 2
    assert evidence.get_meta(conn, audible.READ_AT_KEY) == clock.iso(clk.now())
    assert report.audible_failure(conn) is None                         # a later read reached Audible

    clk.advance(minutes=30)                                             # Sync now, a book moved: not a duplicate
    moved = audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library(bw=40000)))
    assert moved.duplicate_of is None and moved.status == "ok"
    assert day_totals(conn) == pytest.approx({k: round(v / 60000, 2) for k, v in REAL_DAYS.items()
                                              if k >= "2026-09-13"})


# --- which book --------------------------------------------------------------------

def snap(day: str, **remaining) -> audible.Snapshot:
    books = {}
    for asin, value in remaining.items():
        rem, finished_at = value if isinstance(value, tuple) else (value, None)
        books[asin] = audible.Book(asin, asin, rem, 0.0, None, finished_at, 46740 if asin == "LF" else 40380)
    return audible.Snapshot(f"{day}T07:30:00+00:00", date.fromisoformat(day), books)


def test_a_finish_counts_to_the_end_and_a_reset_is_never_movement():
    a = snap("2026-09-29", LF=2400, BW=40380)
    b = snap("2026-09-30", LF=(46740, "2026-09-29T12:55:26.545Z"), BW=40380)
    assert audible.content_moved(a, b) == {"LF": 2400}                  # to the end; the reset ignored
    again = snap("2026-09-30", LF=(46000, "2026-09-29T12:55:26.545Z"))
    assert audible.content_moved(a, again) == {"LF": 2400 + 740}       # and heard again after starting over
    back = snap("2026-10-01", LF=(46740, "2026-09-29T12:55:26.545Z"), BW=40380)
    assert audible.content_moved(b, back) == {}                         # the same finish is not new
    assert audible.content_moved(snap("2026-10-01", BW=30000), snap("2026-10-02", BW=35000)) == {}
    new = audible.Snapshot("2026-10-02T07:30:00+00:00", date(2026, 10, 2),
                           {"NEW": audible.Book("NEW", "New", 900, 0.0, None, None, 1000)})
    assert audible.content_moved(snap("2026-10-01"), new) == {"NEW": 100}    # bought and started between reads


def test_rounding_keeps_every_day_whole():
    shares = audible.split_minutes(10.0, {"C": 1, "A": 1, "B": 1})
    assert sorted(shares.values()) == [3.33, 3.33, 3.34] and shares["A"] == 3.34
    assert round(sum(shares.values()), 2) == 10.0
    assert audible.split_minutes(45.0, {BW: 1800, UH: 1575}) == {BW: 24.0, UH: 21.0}


def test_each_day_goes_to_the_books_that_moved_between_the_mornings(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")                # run 1: the backfill
    days = REAL_DAYS | {"2026-10-09": ms(50), "2026-10-10": ms(45)}
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
    backfill = rows(conn)
    assert {r["source_key"] for r in backfill} == {T.UNKNOWN_AUDIOBOOK_KEY}
    assert {r["display_name"] for r in backfill} == {audible.BOOK_NOT_KNOWN}
    assert {r["detail"]["why"] for r in backfill} == {audible.WHY_BEFORE_FIRST_READ}
    assert {r["detail"]["basis"] for r in backfill} == {audible.BASIS_NOT_KNOWN}
    assert {r["label"] for r in backfill} == {T.WORTHWHILE} and {r["device"] for r in backfill} == {"account"}

    clk.set("2026-10-10T07:30:00+00:00")                                # run 2: one book moved 3,750 s
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=36630)))
    [nine] = rows(conn, "2026-10-09")
    assert (nine["source_key"], nine["display_name"], nine["minutes"]) == (BW, "Test Book One", 50.0)
    assert nine["detail"]["basis"] == audible.BASIS_ONE_BOOK and nine["detail"]["content_s_moved"] == 3750
    assert nine["detail"]["between"] == ["2026-10-09T07:30:00+00:00", "2026-10-10T07:30:00+00:00"]
    assert {r["source_key"] for r in rows(conn, "2026-10-05")} == {T.UNKNOWN_AUDIOBOOK_KEY}  # before the first read

    clk.set("2026-10-11T07:30:00+00:00")                                # run 3: two books moved
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=34830, uh=29445)))
    ten = {r["source_key"]: r for r in rows(conn, "2026-10-10")}
    assert {k: r["minutes"] for k, r in ten.items()} == {BW: 24.0, UH: 21.0}
    assert {r["detail"]["basis"] for r in ten.values()} == {audible.BASIS_SPLIT}
    assert ten[BW]["detail"]["share"] == pytest.approx(0.5333, abs=1e-4)
    assert [r["minutes"] for r in rows(conn, "2026-10-09")] == [50.0]   # the 9th keeps its own window
    totals = day_totals(conn)
    assert totals["2026-10-10"] == 45.0 and totals["2026-10-09"] == 50.0


def test_listening_before_the_morning_read_goes_to_the_book_that_moved_before_it(conn, signed_in):
    clk = clock.FrozenClock("2026-10-11T07:30:00+00:00")
    days = {"2026-10-12": ms(20)}
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
    clk.set("2026-10-12T07:30:00+00:00")                                # 07:40-08:20 local, before this read
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=38880)))
    clk.set("2026-10-13T07:30:00+00:00")                                # nothing moved after it
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=38880)))
    [twelve] = rows(conn, "2026-10-12")
    assert (twelve["source_key"], twelve["minutes"]) == (BW, 20.0)
    assert twelve["detail"]["between"] == ["2026-10-11T07:30:00+00:00", "2026-10-12T07:30:00+00:00"]

    clk.set("2026-10-14T07:30:00+00:00")                                # a day nothing explains
    audible.sync(conn, clk, signed_in, client=FakeClient(days | {"2026-10-13": ms(5)}, library(bw=38880)))
    [thirteen] = rows(conn, "2026-10-13")
    assert (thirteen["source_key"], thirteen["detail"]["why"]) == (T.UNKNOWN_AUDIOBOOK_KEY, audible.WHY_NOTHING_MOVED)


def test_a_later_read_moves_a_day_off_book_not_known_without_leaving_it_twice(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    days = {"2026-10-09": ms(50)}
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
    clk.set("2026-10-10T07:30:00+00:00")                                # the library couldn't be read
    outcome = audible.sync(conn, clk, signed_in, client=FakeClient(days, library_error=NetworkError()))
    assert outcome.status == "partial" and outcome.warnings == (audible.NO_LIBRARY_WARNING,)
    [nine] = rows(conn, "2026-10-09")
    assert (nine["source_key"], nine["detail"]["why"]) == (T.UNKNOWN_AUDIOBOOK_KEY, audible.WHY_NO_READ_AFTER)
    assert audiobook_total(conn, clk, signed_in, "2026-10-09").import_status == "partial"

    clk.set("2026-10-11T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=36630)))
    assert [(r["source_key"], r["minutes"]) for r in rows(conn, "2026-10-09")] == [(BW, 50.0)]


# --- the standing label ------------------------------------------------------------

def test_books_are_worthwhile_as_his_decision_and_never_charged(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    _screentime_row(conn, clk, signed_in, "Instagram", 400.0)
    ig = conn.execute("SELECT id FROM sources WHERE source_key = 'instagram'").fetchone()[0]
    labels.set_source_label(conn, clk, ig, T.WASTEFUL)
    week = week_of("2026-09-20")
    conn.execute("INSERT INTO weeks (week_start, allowance_min, debt_min, promoted_min, listened_min) "
                 "VALUES (?, 360, 0, 0, 0)", (week,))
    conn.commit()
    allowance.on_measurements_changed(conn, clk, signed_in)
    before = allowance.pooled_minutes(conn, signed_in, week)
    over = conn.execute("SELECT overage_min FROM weeks WHERE week_start = ?", (week,)).fetchone()[0]
    assert over == 40

    outcome = audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    bucket = source(conn, T.UNKNOWN_AUDIOBOOK_KEY)
    assert (bucket.label, bucket.labelled_at) == (T.WORTHWHILE, clock.iso(clk.now()))
    assert labels.resolve(conn, source_id=bucket.id).basis == T.BASIS_SOURCE_LABEL
    meta = json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?", (outcome.import_id,)).fetchone()[0])
    assert "DR5.28" in meta["standing_label"] and meta["new_books"] == [T.UNKNOWN_AUDIOBOOK_KEY]

    after = allowance.pooled_minutes(conn, signed_in, week)
    assert after.worthwhile_min == pytest.approx(before.worthwhile_min + 144.26, abs=0.011)
    assert after.charged_min == before.charged_min and after.unclassified_min == before.unclassified_min
    assert conn.execute("SELECT overage_min FROM weeks WHERE week_start = ?", (week,)).fetchone()[0] == 40
    assert bucket.id not in {r.source_id for r in labels.label_queue(conn, clk, signed_in)}
    split = {s.week_start: s for s in report.label_split(conn, clk, signed_in)}
    assert split[week].worthwhile_min == pytest.approx(144.26, abs=0.011)
    assert split[week].wasteful_min == pytest.approx(400.0)


def source(conn, key: str) -> labels.Source:
    row = conn.execute("SELECT id FROM sources WHERE kind = ? AND source_key = ?",
                       (T.KIND_AUDIOBOOK, key)).fetchone()
    assert row is not None, key
    return labels.get_source(conn, row[0])


def test_his_own_label_on_a_book_wins_over_the_standing_decision(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    days = {"2026-10-05": ms(10), "2026-10-09": ms(50), "2026-10-10": ms(30)}
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
    clk.set("2026-10-10T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=36630)))
    book = source(conn, BW)
    assert book.label == T.WORTHWHILE
    labels.set_source_label(conn, clk, book.id, T.WASTEFUL)
    clk.set("2026-10-11T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=35000)))
    assert labels.get_source(conn, book.id).label == T.WASTEFUL
    week = week_of("2026-10-09")
    pooled = allowance.pooled_minutes(conn, signed_in, week)
    assert pooled.wasteful_min == pytest.approx(80.0)                   # both days were this book
    assert source(conn, T.UNKNOWN_AUDIOBOOK_KEY).label == T.WORTHWHILE
    assert pooled.worthwhile_min == pytest.approx(10.0)                  # the 5th: before the first read
    assert pooled.charged_min == pytest.approx(80.0)


# --- the sign-in file -------------------------------------------------------------

TOKEN = "Atna|SECRET-ACCESS-TOKEN-zzzz"


def fake_library_module(days, books, *, refresh: bool, fail: Exception | None = None, warn: bool = False):
    """A stand-in for the `audible` package: an Authenticator that loads and saves JSON, and a Client."""
    mod = types.ModuleType("audible")

    class Authenticator:
        def __init__(self, data):
            self.data = data

        @classmethod
        def from_file(cls, path):
            if warn:
                warnings.warn("Using legacy crypto libraries", UserWarning, stacklevel=2)
            return cls(json.loads(pathlib.Path(path).read_text()))

        def to_dict(self):
            return dict(self.data)

        def to_file(self, filename, encryption=False, set_default=True):
            assert encryption is False and set_default is False
            pathlib.Path(filename).write_text(json.dumps(self.data, indent=4))

    class Client:
        def __init__(self, auth, country_code):
            assert country_code == "uk"
            self.auth, self.inner = auth, FakeClient(days, books)
            self.closed = False

        def get(self, path, **params):
            if warn:
                warnings.warn("SHA-1 is deprecated", DeprecationWarning, stacklevel=2)
                warnings.warn("Using legacy crypto libraries", UserWarning, stacklevel=2)
            if refresh:
                self.auth.data["access_token"] = TOKEN
                self.auth.data["expires"] = 1_900_000_000.0
            logging.getLogger("audible.client").info("request with %s", self.auth.data.get("access_token"))
            if fail is not None:
                raise fail
            return self.inner.get(path, **params)

        def close(self):
            self.closed = True

    mod.Authenticator, mod.Client = Authenticator, Client
    return mod


def test_a_refreshed_sign_in_is_written_back_at_0600_and_never_anywhere_else(conn, signed_in, monkeypatch,
                                                                             caplog):
    caplog.set_level(logging.DEBUG)
    auth = signed_in.audible_auth
    auth.write_text(json.dumps({"adp_token": "x", "access_token": "old", "expires": 1.0}))
    auth.chmod(0o600)
    monkeypatch.setitem(sys.modules, "audible", fake_library_module(REAL_DAYS, library(), refresh=True))
    outcome = audible.sync(conn, clock.FrozenClock("2026-10-09T07:30:00+00:00"), signed_in)
    assert outcome.status == "ok"
    saved = json.loads(auth.read_text())
    assert saved["access_token"] == TOKEN and saved["adp_token"] == "x"
    assert stat.S_IMODE(os.stat(auth).st_mode) == 0o600
    assert not auth.with_name(auth.name + ".tmp").exists()

    # Nowhere else: not in a log line, an import row, the evidence or a figure's detail.
    assert TOKEN not in caplog.text                    # the library's own INFO line is quietened too
    assert all(TOKEN not in " ".join(str(v) for v in tuple(r)) for r in conn.execute("SELECT * FROM imports"))
    assert all(TOKEN not in r["detail"] for r in conn.execute("SELECT detail FROM measurements"))
    raw = pathlib.Path(signed_in.raw_dir) / "audible"
    assert all(TOKEN.encode() not in f.read_bytes() and b"adp_token" not in f.read_bytes() for f in raw.iterdir())
    assert all(stat.S_IMODE(os.stat(f).st_mode) == 0o600 for f in raw.iterdir())


def test_a_refresh_is_saved_even_when_the_read_then_fails(conn, signed_in, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    auth = signed_in.audible_auth
    auth.write_text(json.dumps({"adp_token": "x", "access_token": "old"}))
    auth.chmod(0o600)
    monkeypatch.setitem(sys.modules, "audible",
                        fake_library_module({}, None, refresh=True, fail=NetworkError(TOKEN)))
    outcome = audible.sync(conn, clock.FrozenClock("2026-10-09T07:30:00+00:00"), signed_in)
    assert outcome.status == "failed" and TOKEN not in outcome.error
    assert json.loads(auth.read_text())["access_token"] == TOKEN
    assert stat.S_IMODE(os.stat(auth).st_mode) == 0o600
    assert not auth.with_name(auth.name + ".tmp").exists()
    assert all(TOKEN not in " ".join(str(v) for v in tuple(r)) for r in conn.execute("SELECT * FROM imports"))


def test_an_unchanged_sign_in_is_left_alone(conn, signed_in, monkeypatch):
    auth = signed_in.audible_auth
    before = auth.read_bytes()
    os.utime(auth, (1_000_000_000, 1_000_000_000))
    monkeypatch.setitem(sys.modules, "audible", fake_library_module(REAL_DAYS, library(), refresh=False))
    assert audible.sync(conn, clock.FrozenClock("2026-10-09T07:30:00+00:00"), signed_in).status == "ok"
    assert auth.read_bytes() == before and os.stat(auth).st_mtime == 1_000_000_000


def test_the_librarys_warnings_never_reach_the_log(conn, signed_in, monkeypatch, recwarn):
    monkeypatch.setitem(sys.modules, "audible",
                        fake_library_module(REAL_DAYS, library(), refresh=False, warn=True))
    assert audible.sync(conn, clock.FrozenClock("2026-10-09T07:30:00+00:00"), signed_in).status == "ok"
    assert [w for w in recwarn if issubclass(w.category, (UserWarning, DeprecationWarning))] == []


def test_the_real_client_is_never_reached_by_a_test(conn, signed_in):
    with pytest.raises(pytest.fail.Exception, match="real Audible client"):
        audible.sync(conn, clock.FrozenClock("2026-10-09T07:30:00+00:00"), signed_in)


# --- evidence and reparse -------------------------------------------------------------

def test_the_fixture_shapes_are_read_as_the_real_ones(conn, signed_in):
    stats = json.loads((FIXTURES / "stats.json").read_text())
    lib = json.loads((FIXTURES / "library.json").read_text())
    books, truncated = audible.reduce_library(lib)
    assert not truncated and [b.asin for b in books] == ["B0TEST0001", "B0TEST0002", "B0TEST0003"]
    assert books[1] == audible.Book("B0TEST0002", "Test Book Two", 30000, 0.0, True, "2026-09-20T10:00:00.000Z",
                                    30000)

    class Fixture(FakeClient):
        def get(self, path, **params):
            self.calls.append((path, params))
            return copy.deepcopy(stats if path == audible.STATS_PATH else lib)

    outcome = audible.sync(conn, clock.FrozenClock("2026-09-23T07:30:00+00:00"), signed_in, client=Fixture({}))
    assert outcome.status == "ok"
    assert day_totals(conn) == {"2026-09-14": 50.0, "2026-09-17": 25.0, "2026-09-22": 10.0}
    data = evidence.raw_file(signed_in, conn.execute("SELECT raw_path FROM imports WHERE id = ?",
                                                     (outcome.import_id,)).fetchone()[0]).read_bytes()
    doc = json.loads(data)
    assert doc["stats"][0]["response"] == stats                         # stored verbatim
    assert set(doc["library"][0]) == {"asin", "title", "remaining_s", "percent", "finished", "finished_at",
                                      "runtime_s"}                     # reduced: nothing else kept
    with pytest.raises(audible.AudibleError):
        audible.parse(b"{not json")
    with pytest.raises(audible.AudibleError):                          # no readable date: the shape changed
        audible.parse(json.dumps(doc | {"stats": [{"response": {"aggregated_daily_listening_stats": [
            {"aggregated_sum": 1, "interval_identifier": "14/09/2026", "unit": "Milliseconds"}]}}]}).encode())
    odd = audible.parse(json.dumps(doc | {"stats": [{"response": {"aggregated_daily_listening_stats": [
        {"aggregated_sum": -1, "interval_identifier": "2026-09-14", "unit": "Milliseconds"},
        {"aggregated_sum": 60000, "interval_identifier": "2026-09-15", "unit": "Milliseconds"}]}}]}).encode())
    assert odd.days == {date(2026, 9, 15): 60000.0}                     # an impossible figure costs its own day
    assert odd.unusable == {date(2026, 9, 14): audible.UNUSABLE_NUMBER}


def test_reparse_rebuilds_from_the_stored_read_and_leaves_newer_days_alone(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    days = REAL_DAYS | {"2026-10-09": ms(50)}
    first = audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
    clk.set("2026-10-10T07:30:00+00:00")
    later = REAL_DAYS | {"2026-10-09": ms(50), "2026-10-05": ms(90)}   # a late offline sync raised the 5th
    second = audible.sync(conn, clk, signed_in, client=FakeClient(later, library(bw=36630)))
    expected = rows(conn)
    assert day_totals(conn)["2026-10-05"] == 90.0

    conn.execute("DELETE FROM measurements WHERE provenance = 'audible_stats' AND period_start >= '2026-10-02'")
    conn.commit()
    clk.set("2026-10-12T12:00:00+00:00")
    outcome = reparse.reparse_import(conn, clk, signed_in, second.import_id)
    assert outcome.status == "ok" and rows(conn) == expected
    meta = json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?", (outcome.import_id,)).fetchone()[0])
    assert meta["read_at"] == "2026-10-10T07:30:00+00:00" and meta["reparse_of"] == second.import_id

    again = reparse.reparse_import(conn, clk, signed_in, first.import_id)   # the older read
    assert again.status == "ok"
    assert day_totals(conn)["2026-10-05"] == 90.0                      # the newer total stands
    assert rows(conn) == expected

    conn.execute("DELETE FROM measurements WHERE provenance = 'audible_stats' AND period_start = '2026-10-09'")
    conn.commit()
    twice = reparse.reparse_import(conn, clk, signed_in, second.import_id)  # the same read again
    assert twice.status == "ok" and rows(conn) == expected             # its own re-statement is not newer
    with pytest.raises(ValueError):
        audible.reparse(conn, clk, signed_in, 999)


# --- the dashboard and the words ------------------------------------------------------

def _client(settings, clk):
    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    app.config["TESTING"] = True
    return app.test_client()


def _flat(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _coverage(html: str) -> str:
    m = re.search(r'<section id="coverage">(.*?)</section>', html, re.S)
    assert m
    return m.group(1)


def test_the_pages_show_audiobooks_with_their_own_badge(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    days = REAL_DAYS | {"2026-10-09": ms(50)}
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
    clk.set("2026-10-10T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=36630)))
    client = _client(signed_in, clk)
    for path in ("/", "/records", "/records?group=audiobooks", "/labels", "/labels?kind=audiobook",
                 "/labels/queue", "/records?week=2026-09-20"):
        assert client.get(path).status_code == 200, path
    records = client.get("/records?group=audiobooks").get_data(as_text=True)
    assert 'class="prov prov-audiblestats"' in records and "split estimated" in records
    assert "Test Book One" in records and audible.BOOK_NOT_KNOWN in records
    totals = client.get("/records?week=2026-09-20").get_data(as_text=True)
    assert 'class="prov prov-audiblestats"' in totals and "Audiobooks" in totals
    lab = client.get("/labels?kind=audiobook").get_data(as_text=True)
    assert "Test Book One" in lab and audible.BOOK_NOT_KNOWN in lab
    assert 'value="worthwhile"' in lab
    overview = client.get("/").get_data(as_text=True)
    assert "Audiobooks" not in _flat(_coverage(overview))               # this week's read is in


def test_the_coverage_list_names_audiobooks_only_when_signed_in_and_owed(conn, settings, signed_in):
    clk = clock.FrozenClock("2026-10-19T07:30:00+00:00")                # Mon 19 Oct, nothing ever read
    owed = _client(signed_in, clk).get("/?week=2026-10-11").get_data(as_text=True)
    assert "Audiobooks" in _flat(_coverage(owed))
    client_html = _client(settings, clk).get("/?week=2026-10-11").get_data(as_text=True)
    assert "Audiobooks" not in _flat(_coverage(client_html))


def test_the_first_read_dates_a_summary_but_a_routine_morning_read_does_not(conn, signed_in):
    """Audible is one of the daily reads (DR5.26 rule 4): only catching up counts as new data."""
    week = T.measurement_week(date(2026, 9, 27))
    written = "2026-10-05T07:30:11+00:00"                              # Monday's summary of 27 Sep - 3 Oct
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    assert not dashboard_app._new_data_since(conn, written, week, TZ)
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    assert dashboard_app._new_data_since(conn, written, week, TZ)     # it fills in what the summary described

    clk.set("2026-10-12T07:30:00+00:00")                                # Monday: the job reads, then writes
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    later_week, later_written = T.measurement_week(date(2026, 10, 4)), "2026-10-12T07:30:11+00:00"
    clk.set("2026-10-13T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    assert not dashboard_app._new_data_since(conn, later_written, later_week, TZ)


def test_new_words_are_plain_and_never_name_the_tool_itself(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient({}, stats_error=Unauthorized()))
    words = [
        *report.GROUP_BLIND_SPOTS[T.GROUP_AUDIOBOOKS], report.AUDIBLE_SIGNIN_ACTION, report.AUDIBLE_NOT_SET_UP,
        checklist.AUDIBLE_SIGNIN_LINE, audible.NO_LIBRARY_WARNING, audible.TRUNCATED_WARNING,
        audible.unusable_warning(date(2026, 10, 8), audible.UNUSABLE_OVER_A_DAY),
        T.covered_elsewhere(T.KIND_WEB_DOMAIN, "audible.co.uk"),
        audible.BOOK_NOT_KNOWN, audible.STANDING_LABEL_NOTE, T.PROVENANCE_TEXT[T.PROV_AUDIBLE_STATS],
        T.covered_elsewhere(T.KIND_IPHONE_APP, "audible"),
        *(audible.failure_message(k, "X") for k in ("sign_in", "unreachable", "unexpected")),
        *(s for s in report.blind_spots(conn, clk, signed_in) if "udible" in s or "udiobook" in s),
        *(r.detail + r.action_text for r in report.needs_attention(conn, clk, signed_in)
          if r.group == T.GROUP_AUDIOBOOKS),
        *(" ".join(r.limits) + r.measures + r.tool for r in report.provenance_map(conn, clk, signed_in)
          if r.group == T.GROUP_AUDIOBOOKS),
    ]
    # The sign-in command has to name the folder; the words around it may not name the tool.
    assert len(words) > 15 and not [w for w in words if "dr-brainrot" in w.replace(audible.SIGN_IN_COMMAND, "")]
    assert audible.SIGN_IN_COMMAND in report.AUDIBLE_SIGNIN_ACTION


# ==========================================================================
# The morning job (commit: "Run the Audible read every morning, last")
# ==========================================================================

def test_the_morning_job_reads_audible_last(conn, signed_in, monkeypatch):
    assert jobs.SYNC_STEPS[-1] == "audible"
    monkeypatch.setitem(sys.modules, "audible", fake_library_module(REAL_DAYS, library(), refresh=False))
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    results = jobs.run_sync(conn, clk, signed_in)
    assert list(results) == list(jobs.SYNC_STEPS)
    assert results["audible"].ok
    imp = conn.execute("SELECT id FROM imports WHERE kind = 'audible'").fetchone()[0]
    assert results["audible"].summary == f"import {imp} ok, 10 book-day figure(s)"
    assert jobs.run_sync(conn, clk, signed_in)["audible"].summary == f"unchanged since import {imp}"


def test_signed_out_the_step_succeeds_and_says_why_nothing_was_read(conn, settings):
    results = jobs.run_sync(conn, clock.FrozenClock("2026-10-09T07:30:00+00:00"), settings)
    assert results["audible"].ok
    assert results["audible"].summary == "not set up (no Audible sign-in): audiobooks not measured"
    assert conn.execute("SELECT COUNT(*) FROM imports WHERE kind = 'audible'").fetchone()[0] == 0


def test_a_failed_read_fails_only_its_own_step_with_plain_words(conn, signed_in, monkeypatch):
    monkeypatch.setitem(sys.modules, "audible",
                        fake_library_module({}, None, refresh=False, fail=NetworkError(TOKEN)))
    results = jobs.run_sync(conn, clock.FrozenClock("2026-10-09T07:30:00+00:00"), signed_in)
    assert not results["audible"].ok
    assert results["audible"].error == ("RuntimeError: audible read failed: Audible couldn't be reached "
                                        "(NetworkError). The next morning's read tries again.")
    assert TOKEN not in results["audible"].error
    assert conn.execute("SELECT status FROM imports WHERE kind = 'audible'").fetchone()[0] == "failed"


# ==========================================================================
# What review found (each test is one finding, reproduced)
# ==========================================================================

def test_one_impossible_day_costs_only_that_day_and_every_later_read_carries_on(conn, signed_in):
    """8 Oct over 24 hours used to fail every read from 9 to 20 Oct, while promising the next would work."""
    days = REAL_DAYS | {"2026-10-08": audible.MAX_DAY_MS + 1, "2026-10-12": ms(15)}
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    warning = audible.unusable_warning(date(2026, 10, 8), audible.UNUSABLE_OVER_A_DAY)
    for n in range(12):                                                 # every morning, 9 to 20 Oct
        today = date(2026, 10, 9) + timedelta(days=n)
        clk.set(f"{today.isoformat()}T07:30:00+00:00")
        outcome = audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
        has_the_8th = date.fromisoformat(outcome.period_start) <= date(2026, 10, 8)
        assert outcome.status == ("partial" if has_the_8th else "ok"), today
        assert outcome.warnings == ((warning,) if has_the_8th else ()), today
        assert audible.covered_through(conn) == today - timedelta(days=1)          # never stuck on the 7th
        if n:
            assert outcome.period_start == (today - timedelta(days=audible.LOOKBACK_DAYS)).isoformat()
        assert report.audible_failure(conn) is None
    assert [s for s in report.blind_spots(conn, clk, signed_in) if "Audible" in s] == []

    [eighth] = rows(conn, "2026-10-08")                                 # unmeasured, never zero
    assert (eighth["minutes"], eighth["source_key"]) == (None, T.UNKNOWN_AUDIOBOOK_KEY)
    assert eighth["detail"]["unreadable_reason"] == audible.UNUSABLE_OVER_A_DAY
    assert conn.execute("SELECT status FROM measurements WHERE provenance = 'audible_stats' "
                        "AND period_start = '2026-10-08'").fetchone()[0] == "unreadable"
    assert day_totals(conn)["2026-10-12"] == 15.0                       # the days after it are measured
    week = audiobook_total(conn, clk, signed_in, "2026-10-04")
    assert week.minutes == pytest.approx(83.48) and week.unmeasured_items == 1
    pooled = allowance.pooled_minutes(conn, signed_in, week_of("2026-10-04"))
    assert pooled.unmeasured_items == 1 and pooled.worthwhile_min == pytest.approx(83.48)


def test_a_figure_in_another_unit_or_listed_twice_costs_only_its_own_day(conn, signed_in):
    def spoil(out):
        entries = out["aggregated_daily_listening_stats"]
        for entry in list(entries):
            if entry["interval_identifier"] == "2026-10-02":
                entry["unit"] = "Seconds"
            elif entry["interval_identifier"] == "2026-10-03":
                entries.append(dict(entry))                             # the same total twice: harmless
            elif entry["interval_identifier"] == "2026-10-05":
                entries.append(dict(entry, aggregated_sum=entry["aggregated_sum"] + 1000))   # which one?
        return out

    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    outcome = audible.sync(conn, clk, signed_in, client=Doctored(REAL_DAYS, library(), spoil=spoil))
    assert outcome.status == "partial"
    assert outcome.warnings == (audible.unusable_warning(date(2026, 10, 2), audible.UNUSABLE_UNIT),
                                audible.unusable_warning(date(2026, 10, 5), audible.UNUSABLE_TWICE))
    assert all("unmeasured, not zero" in w for w in outcome.warnings)
    unmeasured = {r["period_start"]: r["detail"]["unreadable_reason"] for r in rows(conn) if r["minutes"] is None}
    assert unmeasured == {"2026-10-02": audible.UNUSABLE_UNIT, "2026-10-05": audible.UNUSABLE_TWICE}
    totals = day_totals(conn)
    assert "2026-10-02" not in totals and "2026-10-05" not in totals
    assert totals["2026-10-03"] == 30.4 and totals["2026-09-14"] == 56.53
    stored = conn.execute("SELECT warnings FROM imports WHERE id = ?", (outcome.import_id,)).fetchone()[0]
    assert json.loads(stored) == list(outcome.warnings)

    clk.set("2026-10-10T07:30:00+00:00")                                # the next answer is clean
    assert audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library())).status == "ok"
    assert not [r for r in rows(conn) if r["minutes"] is None]          # asked again, and measured
    assert day_totals(conn)["2026-10-05"] == 83.48


def test_a_reparse_is_not_a_read_and_never_hides_a_refused_sign_in(conn, signed_in):
    clk = clock.FrozenClock("2026-10-08T07:30:00+00:00")
    first = audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    clk.set("2026-10-09T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient({}, stats_error=Unauthorized()))
    assert report.audible_needs_signin(conn, signed_in)

    clk.set("2026-10-09T10:00:00+00:00")                                # 12:00 local: a reparse
    assert reparse.reparse_import(conn, clk, signed_in, first.import_id).status == "ok"
    failure = report.audible_failure(conn)
    assert (failure.kind, failure.at) == ("sign_in", "2026-10-09T07:30:00+00:00")
    assert report.audible_needs_signin(conn, signed_in)
    spots = [s for s in report.blind_spots(conn, clk, signed_in) if "Audible" in s]
    assert len(spots) == 1 and audible.SIGN_IN_COMMAND in spots[0]
    state, action, _detail, last = report._audible_state(conn, clk, signed_in)
    assert state == report.STATE_ATTENTION and audible.SIGN_IN_COMMAND in action
    assert last == "2026-10-08T07:30:00+00:00"                          # the last read that reached Audible
    clk.set("2026-10-10T07:00:00+00:00")                                # the next morning, still refused
    needs = checklist.week_needs(conn, clk, signed_in, T.measurement_week(date(2026, 10, 4)))
    assert needs.audible_signin

    clk.set("2026-10-10T07:30:00+00:00")                                # signed in again, read
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    clk.set("2026-10-10T10:00:00+00:00")
    reparse.reparse_import(conn, clk, signed_in, first.import_id)
    assert report._audible_state(conn, clk, signed_in)[2] == "Read on Sat 10 Oct 09:30."   # not 12:00


def test_a_finished_week_is_incomplete_until_a_read_reaches_its_saturday(conn, signed_in):
    clk = clock.FrozenClock("2026-10-08T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))     # through Wed 7 Oct
    assert audiobook_total(conn, clk, signed_in, "2026-10-04").import_status == "ok"   # still running
    for day in ("2026-10-09", "2026-10-10", "2026-10-11"):
        clk.set(f"{day}T07:30:00+00:00")
        assert audible.sync(conn, clk, signed_in, client=FakeClient({}, stats_error=NetworkError())).status == "failed"

    week = audiobook_total(conn, clk, signed_in, "2026-10-04")         # Sun 11 Oct: 4 to 10 Oct is over
    assert week.minutes == pytest.approx(83.48)
    assert (week.import_status, week.import_id) == (report.IMPORT_INCOMPLETE, None)   # 8 to 10 Oct unread
    assert not report.everything_in([week])
    html = _client(signed_in, clk).get("/?week=2026-10-04").get_data(as_text=True)
    coverage = _flat(_coverage(html))
    assert "Audiobooks" in coverage and "incomplete" in coverage

    clk.set("2026-10-12T07:30:00+00:00")                                # back: the read reaches Saturday
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    assert audiobook_total(conn, clk, signed_in, "2026-10-04").import_status == "ok"

    clk.set("2026-10-14T07:30:00+00:00")                                # read through Tue 13 Oct, then not
    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    clk.set("2026-10-18T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient({}, stats_error=NetworkError()))
    quiet = audiobook_total(conn, clk, signed_in, "2026-10-11")        # no listening in the days read
    assert (quiet.minutes, quiet.import_status) == (0.0, report.IMPORT_INCOMPLETE)


def _section(html: str, name: str) -> str:
    m = re.search(rf'<section id="{name}">(.*?)</section>', html, re.S)
    assert m, name
    return _flat(m.group(1))


def test_signed_out_the_records_page_owes_nothing_and_shows_no_zero(conn, settings, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    _screentime_row(conn, clk, settings, "Instagram", 30.0, start="2026-10-04", end="2026-10-10")
    out = _client(settings, clk).get("/records").get_data(as_text=True)
    for name in ("imports", "gated", "cost-per-keeper", "weekly-totals"):
        text = _section(out, name)
        assert "Audible" not in text and "Audiobooks" not in text, name

    signed = _client(signed_in, clk).get("/records").get_data(as_text=True)     # signed in, nothing read yet
    assert "Audible" in _section(signed, "imports")                     # honest: none received yet
    assert "Audiobooks" not in _section(signed, "gated")                # but never "nothing consumed"
    assert "Audiobooks" not in _section(signed, "cost-per-keeper")      # nor a 0-minute price

    audible.sync(conn, clk, signed_in, client=FakeClient(REAL_DAYS, library()))
    read = _client(signed_in, clk).get("/records").get_data(as_text=True)
    assert "Audiobooks" in _section(read, "cost-per-keeper")
    assert "Audiobooks" in _section(read, "gated")
    after = _client(settings, clk).get("/records").get_data(as_text=True)          # signed out, figures kept
    assert "Audiobooks" in _section(after, "cost-per-keeper")


def test_a_book_not_known_day_carries_no_split_chip(conn, signed_in):
    clk = clock.FrozenClock("2026-10-09T07:30:00+00:00")
    days = REAL_DAYS | {"2026-10-09": ms(50)}
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
    clk.set("2026-10-10T07:30:00+00:00")
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library(bw=36630)))
    html = _client(signed_in, clk).get("/records?group=audiobooks").get_data(as_text=True)
    records = re.findall(r'<tr class="record[^"]*">(.*?)</tr>', html, re.S)
    unknown = [r for r in records if audible.BOOK_NOT_KNOWN in r]
    known = [r for r in records if "Test Book One" in r]
    assert len(unknown) == 10 and len(known) == 1                      # the backfill, and the 9th
    assert not [r for r in unknown if "split estimated" in r]           # nothing was divided
    assert "split estimated" in known[0]


def test_audibles_web_player_and_its_app_on_the_mac_are_never_charged_twice(conn, clk, settings):
    reason = "Audible's own listening stats (account-level)"
    for kind, key in ((T.KIND_WEB_DOMAIN, "audible.co.uk"), (T.KIND_WEB_DOMAIN, "audible.com"),
                      (T.KIND_DESKTOP_APP, "com.audible.iphone")):
        assert T.covered_elsewhere(*labels.normalise_key(kind, key)) == reason
    assert labels.normalise_key(T.KIND_WEB_DOMAIN, "www.audible.co.uk") == (T.KIND_WEB_DOMAIN, "audible.co.uk")

    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="test")
    site = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "www.audible.co.uk", "audible.co.uk")
    evidence.upsert_measurement(conn, rec, source_id=site.id, device=T.DEVICE_DESKTOP, provenance=T.PROV_EXPORT,
                                period_start="2026-10-06", period_end="2026-10-06", minutes=40.0)
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-10-04", period_end="2026-10-08")
    pooled = allowance.pooled_minutes(conn, settings, week_of("2026-10-06"))
    assert pooled.charged_min == 0.0 and pooled.unclassified_min == 0.0      # unlabelled, still never charged
    assert pooled.covered_excluded_min == pytest.approx(40.0)              # and still visible
    assert site.id not in {r.source_id for r in labels.label_queue(conn, clk, settings)}


def test_a_book_relabelled_wasteful_counts_in_this_weeks_pace(conn, settings, signed_in):
    clk = clock.FrozenClock("2026-10-14T07:30:00+00:00")                # Wed: Sun to Tue against last week's
    before = pace.pace(conn, clk, signed_in)
    assert "audiobook listening, a day wasn't read" in before.left_out  # signed in, not read yet: named
    assert not [x for x in pace.pace(conn, clk, settings).left_out if "audiobook" in x]   # signed out: silent

    days = {"2026-10-05": ms(60), "2026-10-12": ms(30)}
    audible.sync(conn, clk, signed_in, client=FakeClient(days, library()))
    p = pace.pace(conn, clk, signed_in)
    assert "audiobook listening" in p.counted
    assert (p.this_week_min, p.last_week_min) == (0.0, 0.0)             # worthwhile: never brain rot
    labels.set_source_label(conn, clk, source(conn, T.UNKNOWN_AUDIOBOOK_KEY).id, T.WASTEFUL)
    p = pace.pace(conn, clk, signed_in)
    assert (p.this_week_min, p.last_week_min) == (pytest.approx(30.0), pytest.approx(60.0))
    assert "audiobook listening" in p.basis


def test_the_nightly_refresh_names_the_audible_read_and_the_restart_includes_the_bot():
    root = pathlib.Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    nightly = next(ln for ln in readme.splitlines() if ln.startswith("| `com.jacob.dr-brainrot.nightly`"))
    assert "Audible" in nightly
    assert "Audible" in (root / "launchd" / "com.jacob.dr-brainrot.nightly.plist").read_text()
    for agent in ("com.jacob.dr-brainrot.dashboard", "com.jacob.dr-brainrot.bot"):     # both always-on processes
        assert f"launchctl kickstart -k gui/$(id -u)/{agent}" in readme
