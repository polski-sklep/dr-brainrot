"""Optional local check: the parser against the real saved history page.

Skips when `/Users/Jacob/Projects/dr-brainrot/inputs/` is absent (it is gitignored and
personal). Asserts only counts and shapes; no assertion message or output
includes a title, channel or video id (CONTRACTS §12).
"""
from __future__ import annotations

import pathlib
from datetime import date, timedelta

import pytest

from sources import youtube

REAL = pathlib.Path("/Users/Jacob/Projects/dr-brainrot/inputs/Watch history - YouTube.html")
# Saved on Wednesday 2026-09-16, Europe/Warsaw.
CAPTURED_AT = "2026-09-16T10:00:00+00:00"


@pytest.fixture(scope="module")
def parsed():
    if not REAL.exists():
        pytest.skip("real YouTube history page not present")
    data = youtube.extract_initial_data(REAL.read_text(encoding="utf-8"))
    return youtube.parse_pages([data], captured_at=CAPTURED_AT, tz="Europe/Warsaw")


def test_real_page_parses_without_warnings(parsed):
    n_entries, n_warnings = len(parsed.entries), len(parsed.warnings)
    assert n_entries > 0
    assert n_warnings == 0


def test_real_page_dates_are_recent_and_newest_first(parsed):
    days = [date.fromisoformat(e.watch_date) for e in parsed.entries]
    capture = date(2026, 9, 16)
    out_of_range = sum(not (capture - timedelta(days=60) <= d <= capture) for d in days)
    misordered = sum(a < b for a, b in zip(days, days[1:]))
    assert out_of_range == 0
    assert misordered == 0


def test_real_page_has_shorts_videos_and_readable_progress(parsed):
    shorts = [e for e in parsed.entries if e.is_short]
    videos = [e for e in parsed.entries if not e.is_short]
    assert len(shorts) > 0 and len(videos) > 0
    assert sum(e.percent is not None for e in shorts) == 0
    with_progress = [e.percent for e in videos if e.percent is not None]
    missing = len(videos) - len(with_progress)
    assert missing <= 3
    assert sum(not 0 <= p <= 100 for p in with_progress) == 0
