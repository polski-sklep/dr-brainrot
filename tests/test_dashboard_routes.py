"""The routes the UX overhaul added: sync, the label queue, bulk labelling, corrections.

Route and data behaviour only. How any of it *looks* is the design pass's
business and is asserted in `tests/test_dashboard.py`; nothing here reads
markup beyond the ids and values a form needs to work, so the two can move
independently.
"""
from __future__ import annotations

import math
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

    def fake_run_sync(conn, clk, settings):
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


# ==========================================================================
# The brain against your peak week: geometry and wording (rot_view)
# ==========================================================================

import progress  # noqa: E402

W13, W20, W27 = ("2026-09-13T16:00:00+00:00", "2026-09-20T16:00:00+00:00",
                 "2026-09-27T16:00:00+00:00")


def _link(sunday, before, after, *, undecided=0.0, channels=(), sources=0, unreadable=0,
          start=W20):
    saturday = {"2026-09-20": "2026-09-26", "2026-09-27": "2026-10-03"}[sunday]
    return progress.Link(start, sunday, saturday, before, after, undecided, channels, sources,
                         unreadable)


def _chain(ratio, steps, *, state="drawn", week=("2026-09-20", "2026-09-26"), start=W20,
           unreadable=0):
    return progress.PeakChain(state, start, week[0], week[1], ratio, W13, "2026-09-13",
                              "2026-09-19", tuple(steps), (W13, start), None, unreadable)


class _Eye:                                       # report.EyeRow, as far as rot_view reads it
    def __init__(self, day):
        self.day = day


@pytest.mark.parametrize("ratio,step", [
    (None, None), (0, 0), (1e-9, 1), (0.05, 1), (0.14, 1), (0.15, 2), (0.6, 6), (0.61, 6),
    (0.67, 7), (0.7, 7), (0.5445, 5), (0.85, 9), (0.901, 9), (0.94, 9), (0.95, 10), (1.0, 10),
    (1.2, 10),
])
def test_the_brain_is_drawn_at_the_nearest_tenth_and_never_clean_with_rot_in_it(ratio, step):
    assert dashboard_app.drawn_step(ratio) == step


def test_todays_step_holds_under_every_disputed_reading_but_settling_facebook():
    """0.670 today; 0.687 with X's desktop half kept; 0.539 / 0.523 once Facebook is settled."""
    assert {dashboard_app.drawn_step(r) for r in (0.670, 0.687)} == {7}
    assert {dashboard_app.drawn_step(r) for r in (0.539, 0.523)} == {5}


def test_the_first_step_is_the_icon():
    assert [(c.cx, c.cy, c.r) for c in dashboard_app.STEP_COLONIES[1]] == [
        (8.2, 9.1, 1.15), (15.4, 12.4, 1.05), (10.9, 14.9, 0.9)]
    first = dashboard_app.specimen(0.1)
    assert len(first.mould) == 3 and first.bloom == () and first.spores == ()
    assert all(m["young"] and m["tier"] == 0 for m in first.mould)   # the icon's own flat speck


def test_a_heavier_step_contains_every_colony_of_a_lighter_one():
    for lighter, heavier in zip(dashboard_app.STEP_COLONIES, dashboard_app.STEP_COLONIES[1:]):
        big = {c.seat: c.r for c in heavier}
        assert all(c.seat in big and big[c.seat] >= c.r for c in lighter)
    assert len(dashboard_app.STEP_COLONIES[10]) > len(dashboard_app.STEP_COLONIES[9])


def _outline_polygon(steps=24):
    nums = [float(n) for n in re.findall(r"-?\d*\.?\d+", dashboard_app.BRAIN_OUTLINE)]
    x, y, pts, i = nums[0], nums[1], [], 2
    while i + 6 <= len(nums):
        c1x, c1y, c2x, c2y, ex, ey = nums[i:i + 6]
        for k in range(1, steps + 1):
            t = k / steps
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
            pts.append((a * x + b * c1x + c * c2x + d * ex, a * y + b * c1y + c * c2y + d * ey))
        x, y, i = ex, ey, i + 6
    return pts


def test_every_seat_sits_well_inside_the_brain():
    poly = _outline_polygon()

    def inside(px, py):
        hit, j = False, len(poly) - 1
        for k, (xi, yi) in enumerate(poly):
            xj, yj = poly[j]
            if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                hit = not hit
            j = k
        return hit

    def edge(px, py):
        return min(((px - x) ** 2 + (py - y) ** 2) ** 0.5 for x, y in poly)

    for cx, cy, *_ in dashboard_app.SEATS:
        assert inside(cx, cy) and edge(cx, cy) >= 1.1, (cx, cy)


def _pairs(d):
    nums = [float(n) for n in re.findall(r"-?\d*\.?\d+", re.sub(r"A[^A-Za-z]*", "", d))]
    return list(zip(nums[::2], nums[1::2]))


def test_the_large_brain_is_the_mark_drawn_in_full():
    """The same outline on every week, blank included, and its deepest grooves
    are the mark's own midline and four folds. BRAIN_DEEP is a frozen copy:
    this checks every fold's ends against BRAIN_FOLDS, so a moved fold fails
    here, but a change to a fold's curve must be copied by hand."""
    weeks = [dashboard_app.specimen(r, 0.1, "a") for r in (None, 0.0, 0.1, 0.67, 1.0)]
    assert len({(s.outline, s.viewbox, s.deep, s.sulci, s.fine, s.crest) for s in weeks}) == 1
    assert weeks[0].outline == dashboard_app.BRAIN_OUTLINE
    deep = _pairs(dashboard_app.BRAIN_DEEP)
    ends = [(12, 4.6), (12, 17.9)]                                 # the midline
    for m in re.finditer(r"M([\d.]+) ([\d.]+)c([^M]*)", dashboard_app.BRAIN_FOLDS):
        x, y = float(m.group(1)), float(m.group(2))
        dx, dy = [float(n) for n in re.findall(r"-?\d*\.?\d+", m.group(3))][-2:]
        ends += [(x, y), (x + dx, y + dy)]
    assert len(ends) == 10
    for ex, ey in ends:
        assert min(((ex - x) ** 2 + (ey - y) ** 2) ** 0.5 for x, y in deep) < 0.35, (ex, ey)


def test_every_groove_is_closed_and_inside_the_brain():
    poly = _outline_polygon()

    def near_or_inside(px, py):
        hit, j = False, len(poly) - 1
        for k, (xi, yi) in enumerate(poly):
            xj, yj = poly[j]
            if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                hit = not hit
            j = k
        return hit or min(((px - x) ** 2 + (py - y) ** 2) ** 0.5 for x, y in poly) < 0.3

    for d in (dashboard_app.BRAIN_DEEP, dashboard_app.BRAIN_SULCI, dashboard_app.BRAIN_FINE,
              dashboard_app.BRAIN_CREST):
        assert d.count("M") == d.count("Z") and "nan" not in d
        assert all(near_or_inside(x, y) for x, y in _pairs(d))


def test_the_same_week_always_draws_the_same_brain():
    assert dashboard_app.specimen(0.67, 0.04, "a") == dashboard_app.specimen(0.67, 0.04, "a")
    assert dashboard_app.rot_view(None, []) == dashboard_app.rot_view(None, [])


def test_undecided_time_is_drawn_apart_and_only_when_a_patch_fits():
    assert dashboard_app.specimen(0.67, 0.0).undecided == ()
    everything = dashboard_app.specimen(0.67, 1.0)
    assert everything.mould == () and len(everything.undecided) == 18
    assert dashboard_app.specimen(0.1, 0.01).undecided == ()        # too little to draw


def test_nothing_measured_is_a_blank_brain_never_a_clean_one():
    view = dashboard_app.rot_view(None, [])
    assert view["state"] == "not_measured" and view["specimen"].step is None
    assert view["specimen"].mould == () and "nothing drawn" in view["alt"]


def test_one_step_prints_both_like_for_like_figures():
    split = _split(week_start=W20, wasteful_min=324.8, unclassified_min=4.8)
    peak = _split(week_start=W13, wasteful_min=210.4, unclassified_min=6.5)
    view = dashboard_app.rot_view(
        _chain(118.58 / 177.07, [_link("2026-09-20", 177.07, 118.58, undecided=4.74,
                                       channels=("the phone browser by site",), sources=3,
                                       unreadable=1)], unreadable=1),
        [peak, split], eye=[_Eye("2026-09-14")])
    assert view["single"] and (view["before_min"], view["after_min"]) == (177.07, 118.58)
    assert view["pct"] == "67%" and view["specimen"].step == 7
    assert view["counted_min"] == 118.58 and view["week_charged_min"] == pytest.approx(329.6)
    assert view["peak_not_tallest"] and view["settle_can_move"]
    assert view["set_aside"] == "the phone browser by site and 3 sources"
    assert "119 min" in view["alt"] and "177 min" in view["alt"] and "not measured" not in view["alt"]


def test_several_steps_never_set_minutes_from_different_bases_side_by_side():
    view = dashboard_app.rot_view(_chain(0.5445, [
        _link("2026-09-20", 177.07, 118.58), _link("2026-09-27", 207.82, 168.97, start=W27)],
        week=("2026-09-27", "2026-10-03"), start=W27), [])
    assert not view["single"] and view["before_min"] is None and view["after_min"] is None
    assert [s["sunday"] for s in view["steps"]] == ["2026-09-27", "2026-09-20"]
    assert [s["change"] for s in view["steps"]] == ["down 19%", "down 33%"]
    assert view["specimen"].step == 5 and "about 54%" in view["alt"]


def test_a_new_peak_is_drawn_full_and_the_rest_said_in_words():
    view = dashboard_app.rot_view(_chain(1.2, [_link("2026-09-20", 100.0, 120.0)]), [])
    assert view["is_new_peak"] and view["over_pct"] == "20%" and view["pct"] == "100%"
    assert view["specimen"].step == 10
    assert "new peak week" in view["alt"] and "not measured" not in view["alt"]


def test_the_overview_carries_the_brain_last(client, conn, clk, settings, monkeypatch):
    seen = []
    real = dashboard_app.render_template

    def capture(name, **ctx):
        if name == "overview.html":
            seen.append(list(ctx))
        return real(name, **ctx)

    monkeypatch.setattr(dashboard_app, "render_template", capture)
    assert client.get("/").status_code == 200
    assert seen[0][-1] == "rot" and seen[0][-2] == "insight_stale_reason"


def test_settling_is_only_offered_when_there_is_a_reading_to_settle():
    chain = _chain(0.67, [_link("2026-09-20", 177.07, 118.58, unreadable=1)], unreadable=1)
    assert not dashboard_app.rot_view(chain, [])["settle_can_move"]       # no "Readings to settle"
    assert dashboard_app.rot_view(chain, [], eye=[_Eye("2026-09-21")])["settle_can_move"]
    assert not dashboard_app.rot_view(chain, [], eye=[_Eye("2026-09-29")])["settle_can_move"]


def test_set_aside_counts_channels_and_sources_apart():
    assert dashboard_app.set_aside_phrase((), 0) == ""
    assert dashboard_app.set_aside_phrase(("the phone browser by site",), 0) == "the phone browser by site"
    assert dashboard_app.set_aside_phrase(("a", "b"), 11) == "2 channels and 11 sources"
    assert dashboard_app.set_aside_phrase((), 1) == "1 source"


def test_a_rise_the_panel_calls_level_is_never_a_new_peak():
    view = dashboard_app.rot_view(_chain(251 / 250, [_link("2026-09-20", 250.0, 251.0)]), [])
    assert not view["is_new_peak"] and view["above_peak"] and view["pct"] == "100%"
    assert view["over_pct"] is None and "new peak" not in view["alt"]


def test_a_sliver_of_brain_rot_never_reads_as_zero():
    view = dashboard_app.rot_view(_chain(1 / 250, [_link("2026-09-20", 250.0, 1.0)]), [])
    assert view["specimen"].step == 1 and view["pct"] == "under 1%"
    assert "0%" not in view["alt"]
    assert dashboard_app._undecided_min(0.03) == "Under 0.1 min"      # it opens a sentence


def test_the_tallest_bar_is_the_one_the_chart_draws():
    peak = _split(week_start=W13, wasteful_min=200.0, worthwhile_min=150.0)       # bar 350
    shown = _split(week_start=W20, wasteful_min=250.0)                            # bar 250
    chain = _chain(0.8, [_link("2026-09-20", 200.0, 160.0)])
    assert not dashboard_app.rot_view(chain, [peak, shown])["peak_not_tallest"]
    peak = _split(week_start=W13, wasteful_min=200.0)                             # bar 200
    assert dashboard_app.rot_view(chain, [peak, shown])["peak_not_tallest"]


def test_the_alt_gives_undecided_time_in_minutes():
    view = dashboard_app.rot_view(
        _chain(0.67, [_link("2026-09-20", 177.07, 118.58, undecided=40.0)]), [])
    assert view["undecided_drawn"] and "40 min of the week's 119 min is undecided time" in view["alt"]
    assert "patches are" not in view["alt"]


def test_the_first_step_draws_the_marks_own_flat_specks():
    first = dashboard_app.specimen(0.1)
    assert all(m["plain"] for m in first.mould) and first.bloom == ()
    assert not any(m["plain"] for m in dashboard_app.specimen(0.2).mould)


def test_a_clean_week_before_and_two_clean_weeks_say_so():
    nothing = progress.PeakChain("nothing_before", W20, "2026-09-20", "2026-09-26", None, None,
                                 None, None, (_link("2026-09-20", 0.0, 30.0),), (), None)
    view = dashboard_app.rot_view(nothing, [])
    assert view["specimen"].step is None and "had no brain rot" in view["alt"]
    clean = progress.PeakChain("nothing_before", W20, "2026-09-20", "2026-09-26", None, None,
                               None, None, (_link("2026-09-20", 0.0, 0.0),), (), None)
    view = dashboard_app.rot_view(clean, [])
    assert view["specimen"].step == 0 and view["clean_both"]


def test_the_progress_panel_phrase_opens_its_sentence_capitalised():
    p = progress.Progress(W20, "2026-09-20", "2026-09-26", W13, "down", "Down.", 177.0, 119.0,
                          ("x",), (), (), ("the phone browser by site",), 1, 2)
    assert dashboard_app.progress_set_aside(p) == "The phone browser by site and 3 sources"


def test_two_quiet_weeks_with_something_left_out_are_not_called_clean():
    chain = progress.PeakChain("nothing_before", W20, "2026-09-20", "2026-09-26", None, None,
                               None, None, (_link("2026-09-20", 0.0, 0.0, sources=1),), (), None)
    view = dashboard_app.rot_view(chain, [])
    assert not view["clean_both"] and view["specimen"].step is None


def test_a_week_just_above_the_peak_is_the_benchmark_it_is_about_to_become():
    shown = _split(week_start=W20, wasteful_min=262.0)
    peak = _split(week_start=W13, wasteful_min=250.0)
    view = dashboard_app.rot_view(_chain(262 / 250, [_link("2026-09-20", 250.0, 262.0)]),
                                  [peak, shown])
    assert view["above_peak"] and not view["is_new_peak"]
    assert not view["peak_not_tallest"]         # the shown week is the tallest, and the benchmark


@pytest.mark.parametrize("kind,key,url", [
    (T.KIND_WEB_DOMAIN, "reddit.com", "https://reddit.com"),
    (T.KIND_WEB_DOMAIN, "Artlectics.com", "https://artlectics.com"),
    (T.KIND_WEB_DOMAIN, "m.youtube.com", "https://m.youtube.com"),
    (T.KIND_WEB_DOMAIN, "127.0.0.1", None),                       # nothing to open from here
    (T.KIND_WEB_DOMAIN, "localhost", None),
    (T.KIND_WEB_DOMAIN, "fcoeoabgfenejglbffodgkkbkcdhcgfn", None),  # a browser extension
    (T.KIND_WEB_DOMAIN, "file:", None),
    (T.KIND_WEB_DOMAIN, "javascript:alert(1)", None),
    (T.KIND_WEB_DOMAIN, 'evil.com/"><script>', None),
    (T.KIND_YOUTUBE_CHANNEL, "UC0Cyf-sOPE0AGY41I6andkQ",
     "https://www.youtube.com/channel/UC0Cyf-sOPE0AGY41I6andkQ"),
    (T.KIND_YOUTUBE_CHANNEL, "UCgood", None),                      # not a channel id
    (T.KIND_X, "x", "https://x.com"),
    (T.KIND_IPHONE_APP, "facebook", None),
    (T.KIND_DESKTOP_APP, "com.apple.notes", None),
    (T.KIND_PODCAST_SHOW, "triggernometry", None),
    (None, None, None),
])
def test_a_source_links_to_its_own_page_only_when_it_has_one(kind, key, url):
    assert dashboard_app.source_url(kind, key) == url


# --- mushrooms: from 85% of the peak week the mould fruits (DR5.22) -----------

@pytest.mark.parametrize("ratio,pct", [
    (None, None), (0.0, None), (0.84, None), (0.8449, None), (0.845, 85), (0.85, 85),
    (0.86, 86), (0.29 * 3, 87), (1.0, 100), (1.05, 105), (1.1, 110), (1.12, 110), (3.0, 110),
])
def test_mushrooms_come_up_at_85_and_are_full_grown_at_the_new_peak_line(ratio, pct):
    """By the whole point, half-points up, float noise guarded (0.29 * 3 is
    0.8699999999999999), and held at the page's own new-peak line."""
    assert dashboard_app.fruit_pct(ratio) == pct
    assert dashboard_app.FRUIT_FULL == 100 + progress.LEVEL_PCT


def test_no_mushroom_below_85_and_the_first_at_85():
    A = dashboard_app
    for r in (None, 0.0, 0.1, 0.5, 0.84, 0.8449):
        assert A.specimen(r).shrooms == () and A.specimen(r).fruit is None
    first = A.specimen(0.845)
    assert len(first.shrooms) == 1 and first.fruit == 85
    assert first.shrooms[0].seat == 0                  # on the mark's own biggest speck


def test_every_point_past_85_makes_every_mushroom_a_little_bigger():
    A = dashboard_app
    states = A.FRUIT_STATES
    assert len(states) == A.FRUIT_FULL - A.FRUIT_FROM + 1           # one per whole point
    for now, nxt in zip(states, states[1:]):
        before, after = {s.born: s.w for s in now}, {s.born: s.w for s in nxt}
        assert set(before) <= set(after)                            # none ever goes back down
        assert all(after[b] > w for b, w in before.items())         # every one grows, every point
        # Those already up gain at least this much cap between them at every point
        # (about 1px at 320px), arrivals aside, so every point draws a new picture.
        assert sum(after[b] - w for b, w in before.items()) >= 0.065
        assert {s.cap for s in now} != {s.cap for s in nxt}
    # Past the new-peak line they hold, whatever the ratio.
    assert A.specimen(1.12).shrooms == A.specimen(3.0).shrooms == A.specimen(1.1).shrooms
    assert [len(s) for s in states][::5] == [1, 3, 4, 6, 7, 7]       # 85, 90, 95, 100, 105, 110


def test_the_mushrooms_are_precomputed_and_a_render_only_selects():
    A = dashboard_app
    assert all(A._shrooms_at(p) == A.FRUIT_STATES[p - A.FRUIT_FROM]
               for p in range(A.FRUIT_FROM, A.FRUIT_FULL + 1))
    assert A.specimen(0.93, 0.0, "a").shrooms is A.FRUIT_STATES[93 - A.FRUIT_FROM]
    assert A.specimen(1.3, 0.0, "b").shrooms is A.FRUIT_STATES[-1]
    assert A.specimen(0.97, 0.2, "a") == A.specimen(0.97, 0.2, "a")


def test_the_mushrooms_stay_little():
    """Full grown, the biggest cap is under a seventh of the brain's width (17.1),
    and all of them together, caps and stems, cover under 8% of its area
    (measured on their control polygons, which only ever overstate it)."""
    full = dashboard_app.FRUIT_STATES[-1]
    assert len(full) == len(dashboard_app.SHROOMS) <= 8
    assert max(s.w for s in full) <= 2.45

    def area(pts):
        return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2

    covered = sum(area(_pairs(s.cap)) + area(_pairs(s.stem)) for s in full)
    assert covered < 0.08 * area(_outline_polygon())
    first = dashboard_app.FRUIT_STATES[0][0]
    assert 1.2 <= first.w <= 1.5                     # little, but a mushroom at 240px


def test_every_mushroom_stands_well_inside_the_brain_rooted_in_its_colony():
    poly = _outline_polygon()

    def inside(px, py):
        hit, j = False, len(poly) - 1
        for k, (xi, yi) in enumerate(poly):
            xj, yj = poly[j]
            if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                hit = not hit
            j = k
        return hit and min(((px - x) ** 2 + (py - y) ** 2) ** 0.5 for x, y in poly) >= 0.5

    for state in (dashboard_app.FRUIT_STATES[0], dashboard_app.FRUIT_STATES[-1]):
        for s in state:
            x, y, turn = float(s.x), float(s.y), math.radians(float(s.tilt))
            c, n = math.cos(turn), math.sin(turn)
            for d in (s.cap, s.stem, s.mound):
                for px, py in _pairs(d):              # every point, tilted about the foot
                    qx, qy = x + (px - x) * c - (py - y) * n, y + (px - x) * n + (py - y) * c
                    assert inside(qx, qy), (s.born, qx, qy)
    for born, seat, x, y, *_ in dashboard_app.SHROOMS:
        # Its foot is in its colony from the point it comes up (half a point under it).
        step = dashboard_app.drawn_step(born / 100 - .005)
        host = {c.seat: c for c in dashboard_app.STEP_COLONIES[step]}[seat]
        assert ((x - host.cx) ** 2 + (y - host.cy) ** 2) ** 0.5 < host.r, (born, step)


def test_undecided_time_never_fruits():
    A = dashboard_app
    for share in (0.0, 0.1, 0.3, 0.45, 0.6, 0.9, 1.0):
        for ratio in (0.85, 0.93, 1.0, 1.2):
            sp = A.specimen(ratio, share, "a")
            drawn = {m["i"] for m in sp.mould}
            assert {s.seat for s in sp.shrooms} <= drawn
            assert sorted(s.i for s in sp.shrooms) == list(range(len(sp.shrooms)))
            assert (sp.fruit is None) == (sp.shrooms == ())
    assert A.specimen(1.0, 1.0).shrooms == () and A.specimen(1.0, 1.0).fruit is None


def test_the_printed_percent_and_the_mushrooms_count_the_same_point():
    """The page prints whole points half up, as the mushrooms count them, so a
    week at 84.5% says 85% beside its first mushroom. The step lines keep _pct."""
    A = dashboard_app
    assert A._share(0.845) == "85%" and A.fruit_pct(0.845) == 85
    for k in range(8000, 10001):
        r = k / 10000
        assert A._share(r) == f"{A.whole_pct(r)}%"
        assert (A.fruit_pct(r) is not None) == (int(A._share(r)[:-1]) >= 85)
    view = A.rot_view(_chain(0.845, [_link("2026-09-20", 200.0, 169.0)]), [])
    assert view["pct"] == "85%" and view["specimen"].shrooms


def test_the_page_says_so_only_when_mushrooms_are_drawn():
    under = dashboard_app.rot_view(_chain(0.84, [_link("2026-09-20", 100.0, 84.0)]), [])
    assert "mushroom" not in under["alt"] and under["fruit_from"] is None
    view = dashboard_app.rot_view(_chain(0.9, [_link("2026-09-20", 100.0, 90.0)]), [])
    assert view["specimen"].fruit == 90 and view["fruit_from"] == 85
    assert ("Three little mushrooms sprout from the mould, as they do from 85% of your peak "
            "week, a little bigger with each point.") in view["alt"]
    one = dashboard_app.rot_view(_chain(0.85, [_link("2026-09-20", 100.0, 85.0)]), [])
    assert "One little mushroom sprouts from the mould, as it does from 85%" in one["alt"]
    peak = dashboard_app.rot_view(_chain(1.2, [_link("2026-09-20", 100.0, 120.0)]), [])
    assert peak["specimen"].step == 10 and peak["pct"] == "100%"
    assert "Seven mushrooms sprout from the mould, fully grown." in peak["alt"]
    # Said after the mould and before undecided time, counting only those drawn.
    und = dashboard_app.rot_view(
        _chain(1.0, [_link("2026-09-20", 100.0, 100.0, undecided=60.0)]), [])
    n = len(und["specimen"].shrooms)
    assert 0 < n < 6 and f"{dashboard_app._COUNT[n]} little mushroom" in und["alt"]
    assert und["alt"].index("mushroom") < und["alt"].index("undecided time")


def test_full_grown_mushrooms_mean_a_new_peak_never_a_level_week():
    """100 to 112 min is 12% up but under the 15-minute level rule: the page says
    "too little to call a new peak", so the toadstools stop a point short."""
    level = dashboard_app.rot_view(_chain(1.12, [_link("2026-09-20", 100.0, 112.0)]), [])
    assert level["above_peak"] and not level["is_new_peak"]
    assert level["specimen"].fruit == dashboard_app.FRUIT_FULL - 1 and "fully grown" not in level["alt"]
    peak = dashboard_app.rot_view(_chain(1.2, [_link("2026-09-20", 100.0, 120.0)]), [])
    assert peak["is_new_peak"] and peak["specimen"].fruit == dashboard_app.FRUIT_FULL
