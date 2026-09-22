"""The routes the UX overhaul added: sync, the label queue, bulk labelling, corrections.

Route and data behaviour only. How any of it *looks* is the design pass's
business and is asserted in `tests/test_dashboard.py`; nothing here reads
markup beyond the ids and values a form needs to work, so the two can move
independently.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import allowance  # noqa: E402
import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import drbr_types as T  # noqa: E402
import labels  # noqa: E402
from dashboard import app as dashboard_app  # noqa: E402
from tests.test_allowance import label_source, make_settings, measure, open_db  # noqa: E402

WED = "2026-09-16T10:00:00+00:00"


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, db_path=tmp_path / "drbr.db",
                         weekly_allowance_min=360, go_live="2026-09-13")


@pytest.fixture
def clk():
    return clock.FrozenClock(WED)


@pytest.fixture
def conn(settings):
    c = open_db(settings)
    yield c
    c.close()


@pytest.fixture
def client(settings, clk, conn):
    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    return app.test_client()


def post(client, path, **form):
    """Same-origin POST: the app refuses cross-site and same-site writes."""
    return client.post(path, data=form, headers={"Sec-Fetch-Site": "same-origin"})


# --- the week default -------------------------------------------------------

def test_week_defaults_to_the_current_week_and_clamps_below_the_diagnostic(settings, clk):
    """A week before go-live holds no diagnostic data, so it is never opened."""
    choice = dashboard_app.choose_week(clk, settings, None, "2026-09-13")
    assert choice.sunday == "2026-09-13" and choice.is_current

    clamped = dashboard_app.choose_week(clk, settings, "2026-08-30", "2026-09-13")
    assert clamped.sunday == "2026-09-13"
    assert clamped.prev_sunday is None          # nothing before the diagnostic to link to

    clk.set("2026-09-30T10:00:00+00:00")
    later = dashboard_app.choose_week(clk, settings, None, "2026-09-13")
    assert later.sunday == "2026-09-27" and later.prev_sunday == "2026-09-20"


def test_a_future_week_is_clamped_to_the_current_one(settings, clk):
    assert dashboard_app.choose_week(clk, settings, "2026-12-01", "2026-09-13").sunday == "2026-09-13"


def test_bad_week_is_still_refused(client):
    assert client.get("/?week=last-tuesday").status_code == 400


# --- label queue and bulk labelling -----------------------------------------

def _unlabelled(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Facebook", day="2026-09-14",
            minutes=150.0, device=T.DEVICE_IPHONE, provenance=T.PROV_SCREENSHOT)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-14", minutes=20.0)
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCsmall", day="2026-09-14", minutes=5.0)


def test_queue_lists_the_biggest_decisions_first(client, conn, clk, settings):
    _unlabelled(conn, clk, settings)
    html = client.get("/labels/queue").get_data(as_text=True)
    seen, order = set(), []
    for name in re.findall(r"Facebook|reddit\.com|UCsmall", html):   # names repeat (label + cell)
        if name not in seen:
            seen.add(name)
            order.append(name)
    assert order == ["Facebook", "reddit.com", "UCsmall"]             # 150, 20, 5 minutes
    assert "150 min" in html


def test_queue_is_empty_when_everything_is_labelled(client, conn, clk, settings):
    _unlabelled(conn, clk, settings)
    ids = [r.source_id for r in labels.label_queue(conn, clk, settings)]
    labels.bulk_set_label(conn, clk, ids, T.TOOL, settings=settings)
    html = client.get("/labels/queue").get_data(as_text=True)
    assert "every source with measured minutes is labelled" in html


def test_bulk_labels_a_selection_and_re_records_overage(client, conn, clk, settings):
    _unlabelled(conn, clk, settings)
    week = db.current_week(conn, clk, settings).week_start
    before = allowance.pooled_minutes(conn, settings, week).wasteful_min
    assert before == pytest.approx(175.0)          # unlabelled counts as wasteful

    ids = [r.source_id for r in labels.label_queue(conn, clk, settings) if r.display_name != "reddit.com"]
    resp = post(client, "/labels/bulk", label=T.TOOL, source_id=[str(i) for i in ids])
    assert resp.status_code == 303 and "msg=labelled" in resp.headers["Location"]

    after = allowance.pooled_minutes(conn, settings, week)
    assert after.wasteful_min == pytest.approx(20.0)      # only reddit.com is still content
    assert after.tool_min == pytest.approx(155.0)
    assert conn.execute("SELECT overage_min FROM weeks WHERE week_start = ?", (week,)).fetchone()[0] == 0


def test_bulk_refuses_a_bad_label_or_an_empty_selection(client, conn, clk, settings):
    _unlabelled(conn, clk, settings)
    ids = [str(r.source_id) for r in labels.label_queue(conn, clk, settings)]
    assert post(client, "/labels/bulk", label="neutral", source_id=ids).status_code == 400
    assert post(client, "/labels/bulk", label=T.TOOL).status_code == 400


def test_bulk_post_is_refused_cross_site(client, conn, clk, settings):
    _unlabelled(conn, clk, settings)
    ids = [str(r.source_id) for r in labels.label_queue(conn, clk, settings)]
    resp = client.post("/labels/bulk", data={"label": T.TOOL, "source_id": ids},
                       headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


# --- correcting a reading OCR could not settle ------------------------------

def _unreadable_facebook(conn, clk, settings):
    """The real case: OCR read Facebook as both 2h 27m and 2h 37m, so neither was stored."""
    import evidence

    source = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "Facebook")
    conn.commit()
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="test-1")
    evidence.upsert_measurement(
        conn, rec, source_id=source.id, device=T.DEVICE_IPHONE, provenance=T.PROV_SCREENSHOT,
        period_start="2026-09-14", period_end="2026-09-14", minutes=None, status="unreadable",
        detail={"raw": ["Facebook | 2h 27m", "Facebook | 2h 37m"], "readings": [147.0, 157.0],
                "unreadable_reason": "two readings"},
    )
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-14", period_end="2026-09-14")
    return conn.execute("SELECT id FROM measurements WHERE status = 'unreadable'").fetchone()[0]


def test_a_person_can_settle_a_reading_and_the_week_follows(client, conn, clk, settings):
    mid = _unreadable_facebook(conn, clk, settings)
    week = db.current_week(conn, clk, settings).week_start
    assert allowance.pooled_minutes(conn, settings, week).wasteful_min == 0

    resp = post(client, f"/measurements/{mid}/fix", minutes="147")
    assert resp.status_code == 303 and "msg=fixed" in resp.headers["Location"]

    row = conn.execute("SELECT minutes, status, detail FROM measurements WHERE id = ?", (mid,)).fetchone()
    assert row["minutes"] == pytest.approx(147.0) and row["status"] == "ok"
    assert "2h 27m" in row["detail"]                       # the original reading is kept
    assert allowance.pooled_minutes(conn, settings, week).wasteful_min == pytest.approx(147.0)


@pytest.mark.parametrize("value", ["-5", "2000", "", "lots"])
def test_a_refused_correction_changes_nothing(client, conn, clk, settings, value):
    mid = _unreadable_facebook(conn, clk, settings)
    assert post(client, f"/measurements/{mid}/fix", minutes=value).status_code == 400
    assert conn.execute("SELECT status FROM measurements WHERE id = ?", (mid,)).fetchone()[0] == "unreadable"


# --- sync now ---------------------------------------------------------------

def test_sync_now_runs_the_fetching_steps_and_reports_back(client, settings, monkeypatch):
    calls = {}

    def fake_run_sync(conn, clk, settings, bot=None):
        calls["ran"] = True
        import jobs

        return {"vault_scan": jobs.StepResult("vault_scan", True, "import 1 ok"),
                "qbserve": jobs.StepResult("qbserve", True, "13 app-day figures")}

    monkeypatch.setattr(dashboard_app.jobs, "run_sync", fake_run_sync)
    resp = post(client, "/sync")
    assert calls["ran"] and resp.status_code == 303 and "msg=synced" in resp.headers["Location"]


def test_sync_now_says_so_when_a_step_fails(client, settings, monkeypatch):
    def failing(conn, clk, settings, bot=None):
        import jobs

        return {"qbserve": jobs.StepResult("qbserve", False, "", error="database not found")}

    monkeypatch.setattr(dashboard_app.jobs, "run_sync", failing)
    resp = post(client, "/sync")
    assert "msg=sync-failed" in resp.headers["Location"]


def test_flash_codes_are_whitelisted(client):
    class Args:
        @staticmethod
        def getlist(_name):
            return ["synced", "not-a-real-code"]

    assert dashboard_app.flash_from(Args()) == [dashboard_app.FLASH_TEXT["synced"]]


def test_a_refused_write_explains_itself(client, conn, clk, settings):
    """A bare 403 page is unactionable; say what happened and what to do."""
    _unlabelled(conn, clk, settings)
    ids = [str(r.source_id) for r in labels.label_queue(conn, clk, settings)]
    resp = client.post("/labels/bulk", data={"label": T.TOOL, "source_id": ids},
                       headers={"Sec-Fetch-Site": "same-site"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 403
    assert "did not come from this dashboard" in body and "127.0.0.1:8750" in body
    # and nothing was written
    assert all(s.label is None for s in labels.list_sources(conn, label=labels.UNLABELLED))


def test_a_refused_htmx_toggle_answers_with_a_row_not_a_page(client, conn, clk, settings):
    """htmx swaps the response into one <tr>; a whole document there is garbage."""
    src = label_source(conn, clk, T.KIND_WEB_DOMAIN, "reddit.com", None)
    resp = client.post(f"/labels/{src.id}", data={"label": T.TOOL},
                       headers={"Sec-Fetch-Site": "same-site", "HX-Request": "true"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 403
    assert body.lstrip().startswith("<tr") and "<!doctype" not in body.lower()
    assert "did not come from this dashboard" in body
    assert labels.get_source(conn, src.id).label is None        # nothing written
