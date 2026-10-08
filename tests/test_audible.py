"""Audiobooks: Audible's own daily listening totals, split across books (DR5.28).

His decision, 8 Oct 2026: "Yes, I want it; albeit not considered as brainrot".
So audiobooks are measured, each book is a source labelled worthwhile as his
standing decision, and none of it is ever charged as brain rot.

No test reaches the real Audible client or the network: `conftest.py` puts a
stub in `sys.modules["audible"]` that fails on any use, and every read here is
given a fake client. No test reads the real sign-in or store file.
"""
from __future__ import annotations

import pathlib
import re
from datetime import date

import pytest

import allowance
import clock
import config
import db
import drbr_types as T
import evidence
import labels
import report
from dashboard import app as dashboard_app

TZ = "Europe/Warsaw"
CSS = pathlib.Path(dashboard_app.__file__).parent / "static" / "dashboard.css"


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw", go_live="2026-09-13",
                                weekly_allowance_min=360, db_path=tmp_path / "drbr.db")


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
