"""The tool label end to end (DECISIONS DR5.5): a week of brain-rot content plus errands.

One week through the real entry points: Cold Turkey statistics ingested by the
source module, a source labelled tool from the Telegram button, the allowance
alert from poll, Sunday's rollover, then the report and the dashboard HTML.

The promise under test is the user's: DRBR is for brain-rot content, not for
time spent on Amazon. 300 counted content minutes against a 360-minute
allowance is not over, however long the shopping took, and the 200 tool
minutes are still shown rather than silently dropped.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import clock  # noqa: E402
import db  # noqa: E402
import drbr_types as T  # noqa: E402
import labels  # noqa: E402
import poll  # noqa: E402
import report  # noqa: E402
from dashboard import app as dashboard_app  # noqa: E402
from sources import coldturkey, coldturkey_local  # noqa: E402
from tests.test_allowance import FakeBot, make_settings, open_db  # noqa: E402

OWNER = 1
WEEK = "2026-09-13T16:00:00+00:00"           # allowance week opening Sun 13 Sep 18:00 Warsaw


def test_300_wasteful_content_minutes_and_200_tool_minutes_is_not_over_360(tmp_path):
    settings = make_settings(tmp_path, db_path=tmp_path / "drbr.db", weekly_allowance_min=360,
                             telegram_user_id=OWNER)
    clk = clock.FrozenClock("2026-09-14T08:00:00+00:00")
    conn = open_db(settings)
    assert db.current_week(conn, clk, settings).week_start == WEEK

    # Cold Turkey's statistics for Mon and Tue: 300 min of reddit, 200 min of shopping.
    clk.set("2026-09-17T09:00:00+00:00")
    doc = coldturkey_local.export_document({
        (date(2026, 9, 14), "reddit.com"): 150.0, (date(2026, 9, 15), "reddit.com"): 150.0,
        (date(2026, 9, 14), "amazon.pl"): 120.0, (date(2026, 9, 15), "amazon.pl"): 80.0,
    })
    outcome = coldturkey.ingest(conn, clk, settings, doc, origin=coldturkey.ORIGIN_LOCAL_DB)
    assert outcome.status == "ok"

    # The user labels amazon.pl in the dashboard: Tool (not content). Telegram
    # only ever said that something was waiting (DR5.6).
    amazon = next(s for s in labels.list_sources(conn, kind=T.KIND_WEB_DOMAIN) if s.source_key == "amazon.pl")
    queue = labels.label_queue(conn, clk, settings)
    assert [r.display_name for r in queue] == ["reddit.com", "amazon.pl"]      # biggest decision first
    assert labels.bulk_set_label(conn, clk, [amazon.id], T.TOOL, settings=settings) == 1
    assert labels.get_source(conn, amazon.id).label == T.TOOL
    reddit = next(s for s in labels.list_sources(conn, kind=T.KIND_WEB_DOMAIN)
                  if s.source_key == "reddit.com")
    assert labels.bulk_set_label(conn, clk, [reddit.id], T.WASTEFUL, settings=settings) == 1

    # Alerts read wasteful content only: 300 of 360 is approaching, never exceeded.
    bot = FakeBot()
    poll.check_alerts(conn, clk, settings, bot)
    assert bot.texts == ["300 of 360 counted content minutes used this week across sources. 60 min left."]

    # Sunday: the week closes with no overage recorded.
    clk.set("2026-09-20T16:30:00+00:00")
    closed, _opened = db.roll_over_week(conn, clk, settings)
    assert closed.week_start == WEEK and closed.overage_min == 0

    (split,) = [s for s in report.label_split(conn, clk, settings) if s.week_start == WEEK]
    assert (split.wasteful_min, split.worthwhile_min, split.tool_min, split.overage_min) == \
        (pytest.approx(300), 0.0, pytest.approx(200), 0)

    client = dashboard_app.create_app(settings, clk, lambda: db.connect(settings)).test_client()
    html = client.get("/?week=2026-09-13").get_data(as_text=True)
    split_html = re.search(r'<section id="label-split">(.*?)</section>', html, re.S).group(1)
    week_row = next(r for r in re.findall(r"<tr>.*?</tr>", split_html.split("</svg>")[1], re.S)
                    if "2026-09-13" in r)
    cells = [" ".join(re.sub(r"<[^>]+>", " ", c).split())
             for c in re.findall(r'<td class="num[^"]*">(.*?)</td>', week_row, re.S)]
    assert cells[0] == "300 min" and cells[4] == "200 min"   # wasteful … tool
    totals_html = re.search(r'<section id="weekly-totals">(.*?)</section>', html, re.S).group(1)
    assert re.search(r'class="num tool-min">\s*200 min', totals_html)
    allowance_html = re.search(r'<section id="allowance">(.*?)</section>', html, re.S).group(1)
    assert "No week in the diagnostic window has gone over." in allowance_html
    conn.close()
