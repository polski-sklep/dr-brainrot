"""Phase 4 regression tests for defects found by the independent verifier.

Each test names the defect it pins (REVIEW.md Part II). They reuse the
fixture helpers of test_allowance.py / test_report.py so evidence is written
exactly as source modules write it.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import allowance  # noqa: E402
import clock  # noqa: E402
import db  # noqa: E402
import drbr_types as T  # noqa: E402
import evidence  # noqa: E402
import report  # noqa: E402
from dashboard import app as dashboard_app  # noqa: E402
from sources import screentime  # noqa: E402
from tests.test_allowance import (  # noqa: E402
    label_source,
    make_settings,
    measure,
    open_db,
)

TZ = "Europe/Warsaw"
PRE_WEEK = "2026-09-06T16:00:00+00:00"       # allowance week opening Sun 6 Sep 18:00 Warsaw
GO_LIVE_WEEK = "2026-09-13T16:00:00+00:00"   # Sun 13 Sep 18:00 Warsaw


def flat(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def section(html: str, section_id: str) -> str:
    m = re.search(rf'<section id="{section_id}">(.*?)</section>', html, re.S)
    assert m, f"section {section_id} missing"
    return m.group(1)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, db_path=tmp_path / "drbr.db", go_live="2026-09-13")


@pytest.fixture
def conn(settings):
    c = open_db(settings)
    yield c
    c.close()


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-16T10:00:00+00:00")


# --------------------------------------------------------------------------
# P4-02: evidence from before go-live charged the allowance
# --------------------------------------------------------------------------

def test_pre_go_live_evidence_is_shown_but_never_charged(conn, clk, settings):
    clk.set("2026-09-09T10:00:00+00:00")
    db.current_week(conn, clk, settings)                  # the POP week before go-live exists
    clk.set("2026-09-14T10:00:00+00:00")
    db.roll_over_week(conn, clk, settings)                # go-live week opens with no debt
    assert db.current_week(conn, clk, settings).debt_min == 0

    # A first Cold Turkey export reaching back before go-live: 400 wasteful minutes.
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-10", minutes=400.0)

    pooled = allowance.pooled_minutes(conn, settings, PRE_WEEK)
    assert pooled.wasteful_min == 0.0
    assert db.current_week(conn, clk, settings).debt_min == 0     # recompute ran; still no debt
    assert all(s.ungated_min == 0 for s in allowance.gated_split(conn, settings, PRE_WEEK).values())
    # The evidence is not hidden: the week's totals still show it.
    totals = {(t.group, t.provenance): t for t in report.weekly_totals(conn, clk, settings, PRE_WEEK)}
    assert totals[(T.GROUP_DESKTOP, T.PROV_EXPORT)].minutes == pytest.approx(400)

    # From go-live on, the same source charges as before.
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-13", minutes=50.0)
    assert allowance.pooled_minutes(conn, settings, GO_LIVE_WEEK).charged_min == pytest.approx(50)


# --------------------------------------------------------------------------
# P4-03 / gap (c): a week's status names the import covering it
# --------------------------------------------------------------------------

def test_weekly_total_reports_the_covering_import_not_the_latest(conn, clk, settings):
    first = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t")
    first.warnings.append("week-13 warning")
    evidence.finish_import(conn, clk, settings, first, period_start="2026-09-13", period_end="2026-09-19")
    later = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t")
    evidence.finish_import(conn, clk, settings, later, period_start="2026-09-20", period_end="2026-09-26")

    row = next(t for t in report.weekly_totals(conn, clk, settings, GO_LIVE_WEEK) if t.provenance == T.PROV_EXPORT)
    assert (row.import_status, row.import_id) == ("partial", first.id)

    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    html = app.test_client().get("/records?week=2026-09-13").get_data(as_text=True)
    totals = section(html, "weekly-totals")
    assert f'href="#import-{first.id}"' in totals
    assert f'id="import-{first.id}"' in totals and "week-13 warning" in flat(totals)
    assert f'id="import-{later.id}"' not in totals


# --------------------------------------------------------------------------
# P4-04 / gap (d): covered-elsewhere reason lost for a week straddling the start
# --------------------------------------------------------------------------

def test_covered_reason_survives_a_week_straddling_the_diagnostic_start(conn, clk, tmp_path):
    s = make_settings(tmp_path, db_path=tmp_path / "drbr.db")          # no go-live: start is the data
    evidence.ensure_diagnostic_start(conn, "2026-09-16")                # a Wednesday
    measure(conn, clk, s, kind=T.KIND_WEB_DOMAIN, key="youtube.com", day="2026-09-14", minutes=25.0)

    row = next(t for t in report.weekly_totals(conn, clk, s, GO_LIVE_WEEK) if t.provenance == T.PROV_EXPORT)
    assert row.covered_excluded_min == pytest.approx(25)
    assert row.covered_reasons == (("YouTube history (account-level)", pytest.approx(25)),)

    app = dashboard_app.create_app(s, clk, lambda: db.connect(s))
    html = app.test_client().get("/records?week=2026-09-13").get_data(as_text=True)
    assert "already measured by YouTube history (account-level)" in flat(section(html, "weekly-totals"))


# --------------------------------------------------------------------------
# P4-01 / gap (a): the reflex figure reaches the dashboard
# --------------------------------------------------------------------------

def test_reflex_figure_is_exposed_and_shown_prominently(conn, clk, settings):
    label_source(conn, clk, T.KIND_IPHONE_APP, "Calm", T.WORTHWHILE)
    label_source(conn, clk, T.KIND_IPHONE_APP, "Instagram", T.WASTEFUL)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, "x.com", T.WASTEFUL)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-13",
            period_end="2026-09-19", minutes=70.0)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Calm", day="2026-09-13",
            period_end="2026-09-19", minutes=30.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="x.com", day="2026-09-14", minutes=15.0)

    rows = {r.group: r for r in report.gated_vs_ungated(conn, clk, settings, GO_LIVE_WEEK)}
    assert rows[T.GROUP_IPHONE].ungated_min == pytest.approx(100)
    assert rows[T.GROUP_IPHONE].wasteful_ungated_min == pytest.approx(70)
    assert rows[T.GROUP_IPHONE].provenances == (T.PROV_SCREENSHOT,)
    assert rows[T.GROUP_X].wasteful_ungated_min == pytest.approx(15)

    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    # The contract is that the figure is prominent, not that it has its own
    # section: since DR5.12 the total is on the Overview and the per-group
    # working moved to Records. Both are asserted so neither can quietly go.
    overview = app.test_client().get("/?week=2026-09-13").get_data(as_text=True)
    assert "85 min" in flat(section(overview, "week-summary"))
    assert flat(overview).index("85 min") < flat(overview).index("What it was")

    detail = app.test_client().get("/records?week=2026-09-13").get_data(as_text=True)
    reflex = section(detail, "reflex")
    assert "85 min" in flat(reflex) and "prov-screenshot" in reflex and "lower confidence" in reflex
    assert "70 min" in flat(section(detail, "gated"))


# --------------------------------------------------------------------------
# gap (b): report.latest_imports
# --------------------------------------------------------------------------

def test_latest_imports_reads_every_kind_verbatim_including_failures(conn, clk, settings):
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="st-x")
    evidence.fail_import(conn, rec, "week header unreadable")
    got = {i.kind: i for i in report.latest_imports(conn, clk, settings)}
    assert set(got) == set(T.IMPORT_KINDS) - {"audible"}        # signed out of Audible: nothing owed (DR5.28)
    assert (got["screentime"].id, got["screentime"].status, got["screentime"].error) == (
        rec.id, "failed", "week header unreadable")
    assert got["coldturkey"].id is None


# --------------------------------------------------------------------------
# P4-09: aggregates that stack OCR on API minutes say how much is OCR
# --------------------------------------------------------------------------

def test_split_and_allowance_carry_their_screenshot_share(conn, clk, settings):
    clk.set("2026-09-16T10:00:00+00:00")
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Instagram", day="2026-09-13",
            period_end="2026-09-19", minutes=70.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-14", minutes=20.0)
    label_source(conn, clk, T.KIND_IPHONE_APP, "Instagram", T.WASTEFUL)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, "reddit.com", T.WASTEFUL)
    split = [w for w in report.label_split(conn, clk, settings) if w.week_start == GO_LIVE_WEEK][0]
    assert (split.wasteful_min, split.screenshot_ocr_min) == (pytest.approx(90), pytest.approx(70))
    status = report.allowance_status(conn, clk, settings)
    assert status.spent_screenshot_ocr_min == pytest.approx(70)


# --------------------------------------------------------------------------
# P4-06: Apple Vision is unreachable from every test module
# --------------------------------------------------------------------------

def test_vision_is_blocked_suite_wide(conn, clk, settings):
    out = screentime.ingest_batch(conn, clk, settings, [(b"not-an-image", "a.png")],
                                  received_at="2026-09-16T10:00:00+00:00")
    assert out.status == "failed" and "reached Apple Vision" in (out.error or "")


# --------------------------------------------------------------------------
# Audit check (no defect): day -> allowance week agrees with POP's instant
# boundary for every day across both Warsaw DST changes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first,last", [("2026-10-18", "2026-11-01"), ("2027-03-21", "2027-04-04")])
def test_every_day_maps_to_the_pop_week_opening_on_its_sunday_across_dst(first, last):
    from datetime import date, datetime, time, timedelta
    from zoneinfo import ZoneInfo

    day, end = date.fromisoformat(first), date.fromisoformat(last)
    while day <= end:
        start = T.allowance_week_start_for_day(day, TZ)
        sunday = T.week_sunday(day)
        evening = datetime.combine(sunday, time(18, 30), tzinfo=ZoneInfo(TZ))
        assert start == clock.iso(clock.week_start_for(evening, TZ)), day
        assert clock.parse(start).astimezone(ZoneInfo(TZ)).strftime("%a %H:%M") == "Sun 18:00", day
        assert day in T.measurement_week_for_allowance_week(start, TZ).days(), day
        nxt = clock.next_week_start(clock.parse(start), TZ)
        assert nxt.astimezone(ZoneInfo(TZ)).strftime("%a %H:%M") == "Sun 18:00", day
        day += timedelta(days=1)
