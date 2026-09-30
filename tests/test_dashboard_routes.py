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
import insights  # noqa: E402
import labels  # noqa: E402
import report  # noqa: E402
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
    before = allowance.pooled_minutes(conn, settings, week).charged_min
    assert before == pytest.approx(175.0)          # undecided still counts against the week

    ids = [r.source_id for r in labels.label_queue(conn, clk, settings) if r.display_name != "reddit.com"]
    resp = post(client, "/labels/bulk", label=T.TOOL, source_id=[str(i) for i in ids])
    assert resp.status_code == 303 and "msg=labelled" in resp.headers["Location"]

    after = allowance.pooled_minutes(conn, settings, week)
    # Only reddit.com is still content, and it is still undecided: charged to the
    # week, but never called wasteful on the person's behalf (DR5.11).
    assert (after.wasteful_min, after.unclassified_min) == (0.0, pytest.approx(20.0))
    assert after.charged_min == pytest.approx(20.0)
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
    assert allowance.pooled_minutes(conn, settings, week).charged_min == 0

    resp = post(client, f"/measurements/{mid}/fix", minutes="147")
    assert resp.status_code == 303 and "msg=fixed" in resp.headers["Location"]

    row = conn.execute("SELECT minutes, status, detail FROM measurements WHERE id = ?", (mid,)).fetchone()
    assert row["minutes"] == pytest.approx(147.0) and row["status"] == "ok"
    assert "2h 27m" in row["detail"]                       # the original reading is kept
    assert allowance.pooled_minutes(conn, settings, week).charged_min == pytest.approx(147.0)


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


# --- the weekly summary panel (DR5.8) ---------------------------------------

def _complete_weeks(conn, clk, settings):
    """Two finished weeks of figures, and a clock in the week after them, so
    `write_summary` has something to summarise and actually calls the client."""
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Facebook", day="2026-09-16",
            minutes=200.0, device=T.DEVICE_IPHONE, provenance=T.PROV_SCREENSHOT)
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Facebook", day="2026-09-23",
            minutes=100.0, device=T.DEVICE_IPHONE, provenance=T.PROV_SCREENSHOT)
    clk.set("2026-09-30T10:00:00+00:00")


def test_the_overview_carries_the_insight_context_key(client, conn, clk, settings, monkeypatch):
    """The design pass reads `insight`; it is None until something is written."""
    seen = {}

    real = dashboard_app.render_template

    def capture(name, **ctx):
        if name == "overview.html":
            seen.update(ctx)
        return real(name, **ctx)

    monkeypatch.setattr(dashboard_app, "render_template", capture)
    assert client.get("/").status_code == 200
    assert "insight" in seen and seen["insight"] is None

    _complete_weeks(conn, clk, settings)
    insights.write_summary(conn, clk, settings, client=_FakeInsightClient("Steady."))
    seen.clear()
    assert client.get("/").status_code == 200
    insight = seen["insight"]
    assert insight.summary == "Steady."
    assert insight.model == "claude-opus-5" and insight.fallback_reason is None


class _FakeInsightClient:
    """The same shape `tests/test_insights.py` injects. Never goes out."""

    def __init__(self, text="A summary.", *, raises=None):
        self.calls, self.raises, self.text = [], raises, text
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer.raises is not None:
                    raise outer.raises
                return outer._response()

        self.messages = _Messages()

    def _response(self):
        block = type("B", (), {"type": "text", "text": self.text})()
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()
        return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": usage})()


def test_refresh_insight_rewrites_this_weeks_summary(client, conn, clk, settings, monkeypatch):
    _complete_weeks(conn, clk, settings)
    fake = _FakeInsightClient("Rewritten.")
    real_write = insights.write_summary        # bound before the patch, so no recursion

    def with_fake_client(c, k, s, *, client=None, **kw):
        return real_write(c, k, s, client=fake, **kw)

    monkeypatch.setattr(dashboard_app.insights, "write_summary", with_fake_client)

    resp = post(client, "/insights/refresh")
    assert resp.status_code == 303 and "msg=insight-written" in resp.headers["Location"]
    assert dashboard_app.FLASH_TEXT["insight-written"][0] == "ok"
    assert insights.latest(conn, clk, settings).summary == "Rewritten."

    # Forced: a second press writes again rather than returning the first row.
    assert post(client, "/insights/refresh").status_code == 303
    assert len(fake.calls) == 2


def test_refresh_insight_says_so_when_it_falls_back(client, conn, clk, settings):
    """No API key in test settings: the row is failed and the flash warns."""
    resp = post(client, "/insights/refresh")
    assert resp.status_code == 303 and "msg=insight-fallback" in resp.headers["Location"]
    assert dashboard_app.FLASH_TEXT["insight-fallback"][0] == "warn"
    assert conn.execute("SELECT status FROM insights").fetchone()["status"] == "failed"
    shown = insights.latest(conn, clk, settings)
    assert shown.model is None and shown.fallback_reason


def test_refresh_insight_refuses_a_write_from_anywhere_else(client, conn):
    resp = client.post("/insights/refresh", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 0


# --- insight staleness ------------------------------------------------------

def _insight(week_start):
    import insights

    return insights.Insight(summary="s", created_at="2026-09-21T10:00:00+00:00", model="claude-opus-5",
                            week_start=week_start, fallback_reason=None, based_on_through="2026-09-19")


def test_an_insight_is_stale_only_once_a_later_week_has_completed(settings):
    """Per week, not per import: daily re-reads must not make every summary look old."""
    tz = settings.tz
    # Written during the week opening Sun 20 Sep, describing the week ending Sat 19 Sep.
    written_on_the_21st = _insight("2026-09-20T16:00:00+00:00")
    assert dashboard_app.insight_is_stale(written_on_the_21st, "2026-09-13", tz) is False
    assert dashboard_app.insight_is_stale(None, "2026-09-27", tz) is False


def test_staleness_follows_the_week_described_not_the_week_written_in(settings):
    """The bug this guards: week_start is the *generation* week, a week later than the subject.

    Testing generation fires a week late, so a superseded summary reads as
    current for the seven days the flag exists to cover.
    """
    tz = settings.tz
    i = _insight("2026-09-20T16:00:00+00:00")        # describes 13-19 Sep (based_on_through)
    assert i.based_on_through == "2026-09-19"
    # Sun 20 Sep: the week of 13-19 is the newest complete one. Still current.
    assert dashboard_app.insight_is_stale(i, "2026-09-13", tz) is False
    # Sun 27 Sep: the week of 20-26 completed with no summary of its own. Stale.
    assert dashboard_app.insight_is_stale(i, "2026-09-20", tz) is True
    assert dashboard_app.insight_is_stale(i, "2026-09-27", tz) is True


def test_staleness_needs_no_based_on_through_to_be_safe(settings):
    import insights

    none_yet = insights.Insight(summary="s", created_at="2026-09-21T10:00:00+00:00", model=None,
                                week_start="2026-09-20T16:00:00+00:00",
                                fallback_reason="too little data", based_on_through=None)
    assert dashboard_app.insight_is_stale(none_yet, "2026-09-27", settings.tz) is False


def test_overview_passes_the_staleness_flag(client, conn, clk, settings):
    import insights

    week = db.current_week(conn, clk, settings).week_start
    with client.application.test_request_context():
        pass
    conn.execute("INSERT INTO insights (created_at, model, week_start, summary, payload, status) "
                 "VALUES (?, 'claude-opus-5', ?, 'text', '{}', 'ok')",
                 ("2026-09-14T10:00:00+00:00", "2026-09-06T16:00:00+00:00"))   # an older week
    conn.commit()
    assert insights.latest(conn, clk, settings) is not None
    html = client.get("/").get_data(as_text=True)
    assert html.count("<!doctype") == 1        # renders; the panel itself is the design session's


# ==========================================================================
# split_bars: the chart has to carry the whole week
# ==========================================================================

def _split(**kw):
    base = dict(week_start="2026-09-13T16:00:00+00:00", wasteful_min=0.0, worthwhile_min=0.0)
    return report.WeekSplit(**{**base, **kw})


def test_bars_reproduce_the_week_including_undecided_time():
    """A bar drawn from labelled minutes alone would omit most of the week.

    The regression this guards: `wasteful_min` became labelled-only in DR5.11,
    and geometry built from `wasteful + worthwhile` then left undecided time
    out of the picture entirely — the chart answering "is it going down" would
    have halved for a reason that was not behaviour.
    """
    heavy = _split(wasteful_min=205.0, unclassified_min=290.0, worthwhile_min=72.0)
    light = _split(week_start="2026-09-20T16:00:00+00:00", wasteful_min=18.0,
                   unclassified_min=50.0, worthwhile_min=1.2)
    chart = dashboard_app.split_bars([heavy, light], light.week_start, "Europe/Warsaw")

    tallest, shorter = chart["bars"]
    # The tallest bar fills the plot, and each bar's three bands sum to its week.
    for bar, s in ((tallest, heavy), (shorter, light)):
        drawn = bar["waste_h"] + bar["undec_h"] + bar["worth_h"]
        week = s.charged_min + s.worthwhile_min
        assert drawn == pytest.approx(week * tallest_scale(chart, heavy))
        assert bar["undec_h"] > 0                        # undecided is visible, not dropped
    # Stacked bottom-up in a fixed order, with no gap between the bands.
    assert tallest["undec_y"] == pytest.approx(tallest["waste_y"] - tallest["undec_h"])
    assert tallest["worth_y"] == pytest.approx(tallest["undec_y"] - tallest["worth_h"])


def tallest_scale(chart, heavy) -> float:
    """Pixels per minute, read back from the bar that sets the peak."""
    return chart["bars"][0]["waste_h"] / heavy.wasteful_min


def test_bars_are_empty_rather_than_infinite_when_a_week_has_nothing():
    chart = dashboard_app.split_bars([_split()], "2026-09-13T16:00:00+00:00", "Europe/Warsaw")
    assert chart["empty"] is True
    assert chart["bars"][0]["undec_h"] == 0.0


# --- staleness by relabelling (DR5.18) ----------------------------------------
# A summary written 2026-09-21T10:00Z describing the week ending 2026-09-19.

def _relabel_at(conn, clk, kind, key, when, label=T.TOOL):
    clk.set(when)
    label_source(conn, clk, kind, key, label)


def test_relabelling_a_source_the_summary_counted_makes_it_stale(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-15",
            minutes=40.0)
    insight = _insight("2026-09-20T16:00:00+00:00")
    assert dashboard_app.insight_staleness(conn, insight, "2026-09-13", settings.tz) is None

    _relabel_at(conn, clk, T.KIND_WEB_DOMAIN, "reddit.com", "2026-09-21T13:00:00+00:00")
    assert dashboard_app.insight_staleness(conn, insight, "2026-09-13", settings.tz) \
        == dashboard_app.STALE_RELABELLED
    # The old flag keeps its one meaning, so its sentence stays true.
    assert dashboard_app.insight_is_stale(insight, "2026-09-13", settings.tz) is False


def test_labelling_a_source_first_seen_after_the_summary_does_not(conn, clk, settings):
    """Only labels that feed the summarised figures count."""
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="new.example", day="2026-09-22",
            minutes=40.0)
    _relabel_at(conn, clk, T.KIND_WEB_DOMAIN, "new.example", "2026-09-23T13:00:00+00:00")
    insight = _insight("2026-09-20T16:00:00+00:00")
    assert dashboard_app.insight_staleness(conn, insight, "2026-09-13", settings.tz) is None


def test_a_label_set_before_the_summary_was_written_does_not(conn, clk, settings):
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-15",
            minutes=40.0)
    _relabel_at(conn, clk, T.KIND_WEB_DOMAIN, "reddit.com", "2026-09-20T09:00:00+00:00")
    insight = _insight("2026-09-20T16:00:00+00:00")
    assert dashboard_app.insight_staleness(conn, insight, "2026-09-13", settings.tz) is None


def test_a_podcast_show_label_counts_though_it_has_no_measurements(conn, clk, settings):
    """Podcast minutes come from listening, not from measurements."""
    labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Some Show", "Some Show")
    conn.commit()
    _relabel_at(conn, clk, T.KIND_PODCAST_SHOW, "Some Show", "2026-09-21T13:00:00+00:00")
    insight = _insight("2026-09-20T16:00:00+00:00")
    assert dashboard_app.insight_staleness(conn, insight, "2026-09-13", settings.tz) \
        == dashboard_app.STALE_RELABELLED


def test_a_newer_week_outranks_a_relabel(conn, clk, settings):
    """Both can be true; the bigger one is named. A superseded week is the more urgent news."""
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-15",
            minutes=40.0)
    _relabel_at(conn, clk, T.KIND_WEB_DOMAIN, "reddit.com", "2026-09-21T13:00:00+00:00")
    insight = _insight("2026-09-20T16:00:00+00:00")
    assert dashboard_app.insight_staleness(conn, insight, "2026-09-20", settings.tz) \
        == dashboard_app.STALE_NEWER_WEEK


def test_no_summary_is_never_stale(conn, settings):
    assert dashboard_app.insight_staleness(conn, None, "2026-09-20", settings.tz) is None
