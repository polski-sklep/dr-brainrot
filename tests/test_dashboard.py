"""Tests for the DRBR dashboard (dashboard/app.py). Phase 3, integration.

Flask test client against a migrated sqlite file per test, frozen clocks, no
network (conftest blocks it). Evidence is written the way source modules
write it, reusing the helpers from test_allowance.py / test_report.py.

Beyond "every page renders", these tests pin the honesty rules the brief sets
for the UI: provenance tiers look different and a screenshot figure says it is
lower confidence; unmeasured is a count, never "0 min"; covered-elsewhere
minutes are shown as excluded; vault figures are never minutes; and the
countdown is on every page in each of its three states.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import drbr_types as T  # noqa: E402
import evidence  # noqa: E402
import labels  # noqa: E402
from dashboard import app as dashboard_app  # noqa: E402
from tests.test_allowance import (  # noqa: E402
    WED,
    label_source,
    make_settings,
    measure,
    open_db,
)
from tests.test_report import seed_week, vault_capture  # noqa: E402

PAGES = ("/", "/labels", "/records")
WEEK_SUNDAY = "2026-09-06"          # the measurement week of WEEK / WED
CSS = pathlib.Path(dashboard_app.__file__).parent / "static" / "dashboard.css"


# ==========================================================================
# Fixtures and helpers
# ==========================================================================

@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, db_path=tmp_path / "drbr.db")


@pytest.fixture
def conn(settings):
    c = open_db(settings)
    yield c
    c.close()


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-16T10:00:00+00:00")


@pytest.fixture
def client(settings, clk, conn):
    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    app.config["TESTING"] = True
    return app.test_client()


def text(resp) -> str:
    return resp.get_data(as_text=True)


def flat(html: str) -> str:
    """Tags stripped, whitespace collapsed: what a reader sees."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def section(html: str, section_id: str) -> str:
    m = re.search(rf'<section id="{section_id}">(.*?)</section>', html, re.S)
    assert m, f"section {section_id} missing"
    return m.group(1)


def body_rows(html: str) -> list[str]:
    tbody = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    assert tbody
    return re.findall(r"<tr[^>]*>.*?</tr>", tbody.group(1), re.S)


def row_for(rows: list[str], *needles: str) -> str:
    matches = [r for r in rows if all(n in r for n in needles)]
    assert len(matches) == 1, f"expected one row with {needles}, got {len(matches)}"
    return matches[0]


def populate(conn, clk, settings):
    """Spotify, YouTube, Screen Time and Cold Turkey measurements plus vault captures."""
    seed_week(conn, clk, settings)
    vault_capture(conn, clk, group=T.GROUP_X, outcome="kept", captured_at="2026-09-08T10:00:00+00:00")
    vault_capture(conn, clk, group=T.GROUP_X, outcome="unprocessed", captured_at="2026-09-08T11:00:00+00:00")
    vault_capture(conn, clk, group=T.GROUP_YOUTUBE, outcome="excluded", captured_at="2026-09-08T12:00:00+00:00")
    clk.set("2026-09-16T10:00:00+00:00")


# ==========================================================================
# Rendering
# ==========================================================================

@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_for_an_empty_database(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert 'id="countdown"' in text(resp)


@pytest.mark.parametrize("path", PAGES + (f"/?week={WEEK_SUNDAY}", "/records?group=youtube"))
def test_every_page_renders_for_a_populated_database(client, conn, clk, settings, path):
    populate(conn, clk, settings)
    resp = client.get(path)
    assert resp.status_code == 200
    html = text(resp)
    if path.startswith("/records"):
        assert "Good video" in html
    if path == f"/?week={WEEK_SUNDAY}":
        for sid in ("weekly-totals", "label-split", "allowance", "gated", "cost-per-keeper",
                    "funnels", "blind-spots", "imports"):
            assert f'<section id="{sid}">' in html
        assert "<svg" in html and "bar-wasteful" in html


def test_overview_defaults_to_the_current_week_and_links_the_previous_one(client, clk):
    """DR5.7: the page opens on the week the allowance section describes.

    Defaulting to the last complete week put two different weeks on one screen
    with nothing saying so, and in week 1 that week was outside the diagnostic.
    """
    clk.set("2026-09-16T10:00:00+00:00")        # Wednesday of the week opening Sun 13 Sep
    html = text(client.get("/"))
    weeknav = flat(section(html, "weekly-totals"))
    assert "Week Sun 2026-09-13 – Sat 2026-09-19" in weeknav and "in progress" in weeknav

    older = flat(section(text(client.get("/?week=2026-09-08")), "weekly-totals"))
    assert "Week Sun 2026-09-06 – Sat 2026-09-12" in older


def test_bad_week_parameter_is_refused(client):
    assert client.get("/?week=last-tuesday").status_code == 400


def test_htmx_is_pinned_with_an_integrity_hash(client):
    html = text(client.get("/"))
    assert f'src="{dashboard_app.HTMX_URL}"' in html
    assert re.search(r"htmx\.org@2\.\d+\.\d+/dist/htmx\.min\.js", dashboard_app.HTMX_URL)
    assert f'integrity="{dashboard_app.HTMX_SRI}"' in html and dashboard_app.HTMX_SRI.startswith("sha384-")


# ==========================================================================
# Countdown
# ==========================================================================

@pytest.mark.parametrize("path", PAGES)
def test_countdown_not_started(client, path):
    header = flat(text(client.get(path)).split("</header>")[0])
    assert "Diagnostic not started yet." in header
    assert "first valid data" in header


def test_countdown_not_started_before_go_live_says_why(tmp_path, clk):
    settings = make_settings(tmp_path, db_path=tmp_path / "g.db", go_live="2026-09-20")
    open_db(settings).close()
    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    header = flat(text(app.test_client().get("/")).split("</header>")[0])
    assert "Diagnostic not started yet." in header and "goes live on 2026-09-20" in header


@pytest.mark.parametrize("path", PAGES)
def test_countdown_running(client, conn, clk, path):
    evidence.ensure_diagnostic_start(conn, "2026-09-07")
    clk.set("2026-09-16T10:00:00+00:00")        # local day 10; last day 2026-11-01
    header = flat(text(client.get(path)).split("</header>")[0])
    assert "Diagnostic day 10 of 56 · 46 days left · ends 2026-11-01 · week 2 of 8" in header


@pytest.mark.parametrize("path", PAGES)
def test_countdown_ended(client, conn, clk, path):
    evidence.ensure_diagnostic_start(conn, "2026-09-07")
    clk.set("2026-11-02T10:00:00+00:00")
    header = text(client.get(path)).split("</header>")[0]
    assert "countdown-ended" in header
    assert "Diagnostic ended — name the decision or shut it down" in flat(header)
    assert "days left" not in header


# ==========================================================================
# Weekly totals: provenance, unmeasured, covered elsewhere, import status
# ==========================================================================

def test_provenance_badges_are_distinct_per_tier_and_screenshots_are_marked(client, conn, clk, settings):
    populate(conn, clk, settings)
    rows = body_rows(section(text(client.get(f"/?week={WEEK_SUNDAY}")), "weekly-totals"))

    tiers = {T.PROV_API: "prov-api", T.PROV_PAGE: "prov-page",
             T.PROV_SCREENSHOT: "prov-screenshot", T.PROV_EXPORT: "prov-export"}
    for css in tiers.values():
        assert any(f'class="prov {css}"' in r for r in rows), css
    # Each badge's text differs, and each tier has its own, different CSS rule.
    assert len({dashboard_app.PROVENANCE_BADGES[p][0] for p in tiers}) == 4
    css_text = CSS.read_text()
    rules = {c: re.search(rf"\.{c}\s*\{{([^}}]*)\}}", css_text).group(1).strip() for c in tiers.values()}
    assert len(set(rules.values())) == 4

    iphone = row_for(rows, "iPhone apps", "prov-screenshot")
    assert 'class="lowconf"' in iphone and "lower confidence" in iphone
    for other in ("prov-api", "prov-page", "prov-export"):
        for r in rows:
            if other in r:
                assert "lowconf" not in r


def test_unmeasured_items_are_a_count_never_zero_minutes(client, conn, clk, settings):
    clk.set(WED)
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key="UCbad", day="2026-09-08",
            minutes=None, status="progress_missing", item_key="badvideo001")
    clk.set("2026-09-16T10:00:00+00:00")
    rows = body_rows(section(text(client.get(f"/?week={WEEK_SUNDAY}")), "weekly-totals"))
    yt = flat(row_for(rows, "YouTube", "prov-page"))
    assert "1 unmeasured item" in yt and "not counted as zero" in yt
    assert "nothing measured" in yt
    assert not re.search(r"(?<![\d.])0(\.0)? min", yt)


def test_missing_weekly_evidence_is_not_received_not_zero(client):
    rows = body_rows(section(text(client.get(f"/?week={WEEK_SUNDAY}")), "weekly-totals"))
    screentime = flat(row_for(rows, "iPhone apps", "prov-screenshot"))
    assert "not received" in screentime and "missing" in screentime
    assert "0 min" not in screentime


def test_covered_elsewhere_is_shown_as_excluded_with_its_reason(client, conn, clk, settings):
    populate(conn, clk, settings)
    rows = body_rows(section(text(client.get(f"/?week={WEEK_SUNDAY}")), "weekly-totals"))
    iphone = flat(row_for(rows, "iPhone apps", "prov-screenshot"))
    assert "33 min excluded" in iphone
    assert "YouTube history (account-level)" in iphone
    assert "90 min" in iphone                     # Instagram is counted; the YouTube app is not


def test_import_status_incomplete_and_partial_with_warnings(client, conn, clk, settings):
    populate(conn, clk, settings)
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="test-1")
    rec.warnings.append("skipped 3 days on or after the export date")
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-06", period_end="2026-09-12")
    html = text(client.get(f"/?week={WEEK_SUNDAY}"))
    rows = body_rows(section(html, "weekly-totals"))
    assert "incomplete" in flat(row_for(rows, "YouTube", "prov-page"))
    assert "partial" in flat(row_for(rows, "Desktop web", "prov-export"))
    imports = flat(section(html, "imports"))
    assert "partial" in imports and "skipped 3 days on or after the export date" in imports


def test_failed_import_is_visible(client, conn, clk):
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="test-1")
    evidence.fail_import(conn, rec, "could not read the week header")
    imports = flat(section(text(client.get("/")), "imports"))
    assert "failed" in imports and "could not read the week header" in imports


# ==========================================================================
# Cost per keeper, funnels, allowance, gated
# ==========================================================================

def test_cost_per_keeper_says_no_keepers_yet(client, conn, clk, settings):
    seed_week(conn, clk, settings)
    clk.set("2026-09-16T10:00:00+00:00")
    rows = body_rows(section(text(client.get("/")), "cost-per-keeper"))
    podcasts = flat(row_for(rows, "Podcasts"))
    assert "no keepers yet" in podcasts
    assert "prov-api" in row_for(rows, "Podcasts")
    assert "invisible" in podcasts                # the group's blind spot


def test_cost_per_keeper_prices_a_group_with_keepers(client, conn, clk, settings):
    seed_week(conn, clk, settings)
    vault_capture(conn, clk, group=T.GROUP_YOUTUBE, outcome="kept")
    clk.set("2026-09-16T10:00:00+00:00")
    rows = body_rows(section(text(client.get("/")), "cost-per-keeper"))
    assert "40 min per keeper" in flat(rows[0]) and "YouTube" in rows[0]
    assert "prov-vault" in rows[0]


def test_vault_numbers_are_never_minutes(client, conn, clk, settings):
    populate(conn, clk, settings)
    funnels = section(text(client.get("/")), "funnels")
    vault = funnels.split('<div id="vault-funnel">')[1]
    assert not re.search(r"\d\s*min\b", flat(funnels)), flat(funnels)
    v = flat(vault)
    assert "Kept 1" in v and "Excluded (vanished or in the trash), outside the denominator: 1" in v
    assert "Captured (unprocessed + kept) 2" in v


def test_allowance_status_is_plain_numbers(client, conn, clk, settings):
    populate(conn, clk, settings)
    allowance_html = flat(section(text(client.get("/")), "allowance"))
    for label in ("Spent", "Remaining", "Over the allowance so far", "Allowance",
                  "Weeks over the allowance"):
        assert label in allowance_html
    assert "debt" not in allowance_html.lower() and "effective" not in allowance_html.lower()
    assert "target" not in allowance_html.lower() and "score" not in allowance_html.lower()
    assert "No week in the diagnostic window has gone over." in allowance_html   # 132 of 180


def test_weeks_over_the_allowance_table_lists_each_weeks_own_overage(client, conn, clk, settings):
    populate(conn, clk, settings)                                   # week of 6 Sep: 132 wasteful
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-09",
            minutes=118.0)                                          # 250 against 180
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-14",
            minutes=200.0)                                          # current week: 20 over so far
    html = section(text(client.get("/")), "allowance")
    table = html.split("Weeks over the allowance", 1)[1]
    rows = body_rows(table)
    assert len(rows) == 2
    closed = flat(row_for(rows, "2026-09-06"))
    assert "180 min" in closed and closed.endswith("70 min")
    current = flat(row_for(rows, "2026-09-13"))
    assert "(in progress)" in current and current.endswith("20 min")   # not reduced by last week's 70
    status = flat(html.split("Weeks over the allowance", 1)[0])
    assert "Over the allowance so far 20 min" in status


def test_gated_vs_ungated_shows_the_reflex_share(client, conn, clk, settings):
    populate(conn, clk, settings)
    rows = body_rows(section(text(client.get(f"/?week={WEEK_SUNDAY}")), "gated"))
    iphone = row_for(rows, "iPhone apps")
    assert '<td class="reflex">100% ungated</td>' in iphone
    assert "nothing consumed" in flat(row_for(rows, "Desktop web"))      # None share, not 0%


# ==========================================================================
# Labels
# ==========================================================================

def test_labels_page_lists_sources_with_unlabelled_stated_and_x_toggleable(client, conn, clk):
    label_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, "UCplain", None, "Plain Channel")
    html = text(client.get("/labels"))
    rows = body_rows(html)
    plain = row_for(rows, "Plain Channel")
    assert "unlabelled → counts as wasteful" in plain and "<form" in plain
    x_row = row_for(rows, "<td>X</td>")
    assert "<form" in x_row and "wasteful" in x_row


def test_labels_page_filters_by_kind_and_label(client, conn, clk):
    label_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, "UCgood", T.WORTHWHILE, "Good Channel")
    label_source(conn, clk, T.KIND_IPHONE_APP, "Instagram", None, "Instagram")
    by_kind = text(client.get("/labels?kind=youtube_channel"))
    assert "Good Channel" in by_kind and "Instagram" not in by_kind
    unlabelled = text(client.get("/labels?label=unlabelled"))
    assert "Instagram" in unlabelled and "Good Channel" not in unlabelled
    assert client.get("/labels?kind=nonsense").status_code == 400


def _overage_scenario(conn, clk, settings):
    """A closed week with 500 wasteful minutes: 140 over, recorded on it, nothing carried."""
    clk.set("2026-09-14T10:00:00+00:00")
    db.current_week(conn, clk, settings)
    src = measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="news.example", day="2026-09-15",
                  minutes=500.0)
    clk.set("2026-09-20T16:30:00+00:00")
    closed, opened = db.roll_over_week(conn, clk, settings)
    assert closed.overage_min == 140
    assert opened.debt_min == 0
    return src, closed, opened


def _week_figures(conn, week_start):
    row = conn.execute("SELECT overage_min, debt_min FROM weeks WHERE week_start = ?", (week_start,)).fetchone()
    return row["overage_min"], row["debt_min"]


@pytest.fixture
def overage_settings(tmp_path):
    return make_settings(tmp_path, db_path=tmp_path / "overage.db", weekly_allowance_min=360)


def test_label_toggle_via_htmx_returns_the_row_and_re_records_overage(overage_settings, clk):
    conn = open_db(overage_settings)
    src, closed, opened = _overage_scenario(conn, clk, overage_settings)
    client = dashboard_app.create_app(overage_settings, clk, lambda: db.connect(overage_settings)).test_client()

    resp = client.post(f"/labels/{src.id}", data={"label": T.WORTHWHILE}, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    fragment = text(resp)
    assert fragment.lstrip().startswith(f'<tr id="source-{src.id}"')
    assert "<html" not in fragment and "<strong>worthwhile</strong>" in fragment

    assert labels.get_source(conn, src.id).label == T.WORTHWHILE
    assert _week_figures(conn, closed.week_start) == (0, 0)      # now worthwhile: never over
    assert _week_figures(conn, opened.week_start) == (0, 0)
    conn.close()


def test_label_toggle_via_plain_post_redirects_and_re_records_overage(overage_settings, clk):
    conn = open_db(overage_settings)
    src, closed, opened = _overage_scenario(conn, clk, overage_settings)
    labels.set_source_label(conn, clk, src.id, T.WORTHWHILE)
    conn.execute("UPDATE weeks SET overage_min = 0 WHERE week_start = ?", (closed.week_start,))
    conn.commit()
    client = dashboard_app.create_app(overage_settings, clk, lambda: db.connect(overage_settings)).test_client()

    resp = client.post(f"/labels/{src.id}", data={"label": T.WASTEFUL, "f_kind": "web_domain"})
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith(f"/labels?kind=web_domain#source-{src.id}")
    assert labels.get_source(conn, src.id).label == T.WASTEFUL
    assert _week_figures(conn, closed.week_start) == (140, 0)    # re-recorded on the closed week
    assert _week_figures(conn, opened.week_start) == (0, 0)      # and never carried
    conn.close()


def test_labels_page_says_a_relabel_re_records_overage(client):
    page = flat(text(client.get("/labels")))
    assert "re-records each week's overage" in page
    assert "debt" not in page.lower()


def test_unknown_youtube_channel_is_shown_but_not_toggleable(client, conn, clk):
    unknown = label_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, T.UNKNOWN_YOUTUBE_CHANNEL_KEY, None,
                           "Unknown channel")
    row = row_for(body_rows(text(client.get("/labels"))), "Unknown channel")
    assert "<form" not in row and "Not toggleable" in row
    resp = client.post(f"/labels/{unknown.id}", data={"label": T.WORTHWHILE})
    assert resp.status_code == 400
    assert labels.get_source(conn, unknown.id).label is None


def test_label_toggle_refuses_bad_input_and_cross_site_posts(client, conn, clk):
    src = label_source(conn, clk, T.KIND_IPHONE_APP, "Instagram", None, "Instagram")
    assert client.post(f"/labels/{src.id}", data={"label": "great"}).status_code == 400
    assert client.post("/labels/999999", data={"label": T.WASTEFUL}).status_code == 404
    resp = client.post(f"/labels/{src.id}", data={"label": T.WORTHWHILE},
                       headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert labels.get_source(conn, src.id).label is None


# ==========================================================================
# Records
# ==========================================================================

def test_records_rows_show_provenance_status_and_import(client, conn, clk, settings):
    populate(conn, clk, settings)
    rows = body_rows(text(client.get("/records")))
    bad = row_for(rows, "badvideo001")
    assert "progress_missing" in bad and "unmeasured" in bad and "prov-page" in bad
    assert re.search(r"#\d+", bad)
    pod = row_for(rows, "Pod", "prov-api")
    assert "API poll" in pod
    covered = row_for(rows, "prov-screenshot", "already measured by")
    assert "lower confidence" in covered


def test_records_filters(client, conn, clk, settings):
    populate(conn, clk, settings)
    yt = text(client.get("/records?group=youtube"))
    assert "Good video" in yt and "Instagram" not in yt and "prov-export" not in yt

    worth = body_rows(text(client.get("/records?label=worthwhile")))
    assert len(worth) == 1 and "Good video" in worth[0]

    dated = body_rows(text(client.get("/records?start=2026-09-09&end=2026-09-09")))
    assert any("2026-09-09" in r and "prov-export" in r for r in dated)   # Cold Turkey X, that day
    assert not any("Good video" in r for r in dated)                      # YouTube, 2026-09-07
    assert all("2026-09-07" not in r and "2026-09-08" not in r for r in dated)

    bad = client.get("/records?start=yesterday")
    assert bad.status_code == 400 and "Start must be a date" in text(bad)


# ==========================================================================
# Wiring
# ==========================================================================

def test_ingest_blueprint_is_mounted_behind_the_token(client, settings):
    assert client.get("/api/youtube/cursor").status_code == 401
    assert client.post("/api/youtube/history", data=b"{}").status_code == 401
    ok = client.get("/api/youtube/cursor", headers={"X-DRBR-Token": settings.ingest_token})
    assert ok.status_code == 200 and ok.get_json() == {"cursor": None, "cursor_date": None}


def test_body_limit_is_at_least_25_mb(settings, clk):
    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    assert app.config["MAX_CONTENT_LENGTH"] >= 25 * 1024 * 1024


def test_each_request_gets_its_own_connection_closed_on_teardown(settings, clk, conn):
    opened = []

    def get_conn():
        c = db.connect(settings)
        opened.append(c)
        return c

    client = dashboard_app.create_app(settings, clk, get_conn).test_client()
    client.get("/")
    client.get("/labels")
    assert len(opened) == 2
    for c in opened:
        with pytest.raises(Exception):
            c.execute("SELECT 1")


def test_main_binds_loopback_only(monkeypatch, tmp_path):
    settings = make_settings(tmp_path, db_path=tmp_path / "main.db", dashboard_port=8799)
    monkeypatch.setattr(config, "load", lambda *a, **k: settings)
    calls = []
    import flask

    monkeypatch.setattr(flask.Flask, "run", lambda self, *a, **k: calls.append((a, k)))
    dashboard_app.main()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert not args
    assert kwargs == {"host": "127.0.0.1", "port": 8799, "threaded": True, "debug": False,
                      "use_reloader": False}
    conn = db.connect(settings)
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() is not None
    conn.close()


# ==========================================================================
# Tool: not content (DR5.5)
# ==========================================================================

def test_labels_page_states_the_content_rule_and_offers_tool(client, conn, clk):
    src = label_source(conn, clk, T.KIND_WEB_DOMAIN, "amazon.pl", None, "amazon.pl")
    html = text(client.get("/labels"))
    page = " ".join(re.sub(r"<[^>]+>", "", html).split())                 # inline emphasis removed in place
    assert ("DRBR measures brain-rot content only. Label each source by asking: is this content I consume? "
            "If yes, is it brain rot (wasteful) or not (worthwhile)? If I only go there to get a task done — "
            "buy, pay, book, log in, organise, check the weather — it's a tool and isn't counted.") in page
    assert '<option value="tool"' in html                                   # filter option
    row = row_for(body_rows(html), "amazon.pl")
    assert 'name="label" value="tool"' in row and 'hx-post' in row


def test_tool_toggle_via_htmx_returns_the_row_and_re_records_overage(overage_settings, clk):
    conn = open_db(overage_settings)
    src, closed, opened = _overage_scenario(conn, clk, overage_settings)
    client = dashboard_app.create_app(overage_settings, clk, lambda: db.connect(overage_settings)).test_client()

    resp = client.post(f"/labels/{src.id}", data={"label": T.TOOL}, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    fragment = text(resp)
    assert fragment.lstrip().startswith(f'<tr id="source-{src.id}"')
    assert "tool → not content, not counted" in fragment
    assert re.search(r'value="tool"\s+disabled', fragment)
    assert labels.get_source(conn, src.id).label == T.TOOL
    assert _week_figures(conn, closed.week_start) == (0, 0)             # tool: never over
    conn.close()


def test_tool_toggle_via_plain_post_redirects_and_filters(overage_settings, clk):
    conn = open_db(overage_settings)
    src, closed, _ = _overage_scenario(conn, clk, overage_settings)
    client = dashboard_app.create_app(overage_settings, clk, lambda: db.connect(overage_settings)).test_client()

    resp = client.post(f"/labels/{src.id}", data={"label": T.TOOL, "f_label": "tool"})
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith(f"/labels?label=tool#source-{src.id}")
    assert labels.get_source(conn, src.id).label == T.TOOL
    assert _week_figures(conn, closed.week_start) == (0, 0)
    listed = text(client.get("/labels?label=tool"))
    assert "news.example" in listed
    assert "news.example" not in text(client.get("/labels?label=unlabelled"))
    # Cross-site posts are refused for tool exactly as for the other labels.
    other = label_source(conn, clk, T.KIND_IPHONE_APP, "Mail", None, "Mail")
    assert client.post(f"/labels/{other.id}", data={"label": T.TOOL},
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert labels.get_source(conn, other.id).label is None
    conn.close()


def test_overview_states_scope_and_shows_tool_minutes_outside_the_bars(client, conn, clk, settings):
    clk.set(WED)
    db.current_week(conn, clk, settings)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, "amazon.pl", T.TOOL)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="amazon.pl", day="2026-09-08", minutes=200.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-08", minutes=30.0)
    clk.set("2026-09-16T10:00:00+00:00")

    # The page now opens on the current week (DR5.7), so ask for the week the
    # minutes above are in; the point of this test is tool minutes, not the default.
    html = text(client.get("/?week=2026-09-08"))
    assert "Brain-rot content only: tools are excluded" in flat(html)

    totals = section(html, "weekly-totals")
    assert "Tool (not content, excluded)" in totals
    desktop = row_for(body_rows(totals), "Desktop web")
    assert "30 min" in desktop and "200 min" in desktop

    split = section(html, "label-split")
    week_row_html = row_for(body_rows(split.split("</svg>")[1]), WEEK_SUNDAY)
    cells = re.findall(r'<td class="num[^"]*">(.*?)</td>', week_row_html, re.S)
    assert flat(cells[0]) == "30 min" and flat(cells[1]) == "0 min" and flat(cells[3]) == "200 min"
    assert "Week of 2026-09-06: wasteful 30 min, worthwhile 0 min" in split       # the bar holds content only

    keepers = row_for(body_rows(section(html, "cost-per-keeper")), "<td>Desktop web</td>")
    assert "30 min" in keepers and "200 min" not in keepers
    assert "sources you mark as tool" in section(html, "blind-spots")


def test_records_page_filters_tool(client, conn, clk, settings):
    clk.set(WED)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, "amazon.pl", T.TOOL)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="amazon.pl", day="2026-09-08", minutes=5.0)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key="reddit.com", day="2026-09-08", minutes=5.0)
    clk.set("2026-09-16T10:00:00+00:00")
    resp = client.get("/records?label=tool")
    assert resp.status_code == 200
    assert "amazon.pl" in text(resp) and "reddit.com" not in text(resp)
