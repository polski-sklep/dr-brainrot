"""sources/youtube.py — Agent A. CONTRACTS.md Part II §6.1.

No network: the Data API client runs on `httpx.MockTransport`, everything else
on `FakeYouTubeApi`. Page data comes from the sanitised fixtures in
tests/fixtures/youtube/, derived from the real saved history page.

Capture instant used throughout: Wednesday 2026-09-16 12:00 Europe/Warsaw.
Fixture sections and the days they resolve to:
    Today     2026-09-16  Shorts fxVideo0001, fxVideo0002
    Yesterday 2026-09-15  Short fxVideo0003; videos 0004 (45%), 0005 (100%)
    Monday    2026-09-14  0006 (100%, collaboration), 0007 (10%)
    Friday    2026-09-11  0008 (67%), 0009 (LIVE, no progress bar)
    Thursday  2026-09-10  Shorts 0010, 0011
    Sep 9     2026-09-09  Short 0012; video 0013 (92%)
  continuation page:
    Aug 25    2026-08-25  0014 (100%), 0015 (100%)
    Aug 24    2026-08-24  0015 again (100%), 0016 (30%)
"""
from __future__ import annotations

import copy
import json
import logging
import pathlib
from datetime import date

import httpx
import pytest

import clock
import config
import db
import dr_brainrot_types as T
import evidence
from dr_brainrot_types import VideoInfo, VideoLookupError
from sources import youtube
from sources.youtube import DataApiClient, FakeYouTubeApi, YouTubeParseError

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "youtube"
CAPTURED_AT = "2026-09-16T10:00:00+00:00"
TZ = "Europe/Warsaw"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def initial():
    return load("history_initial.json")


@pytest.fixture
def continuation():
    return load("history_continuation.json")


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw")


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-16T10:05:00+00:00")


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


CHANNEL_A = "UCapiChannelAAAAAAAAAAAA"
CHANNEL_B = "UCapiChannelBBBBBBBBBBBB"

DURATIONS = {
    "fxVideo0001": 45, "fxVideo0002": 180, "fxVideo0003": 60,
    "fxVideo0004": 600, "fxVideo0005": 1200, "fxVideo0006": 3723, "fxVideo0007": 600,
    "fxVideo0008": 900, "fxVideo0009": None, "fxVideo0010": 60, "fxVideo0011": 60,
    "fxVideo0012": 60, "fxVideo0013": 300, "fxVideo0014": 600, "fxVideo0015": 600,
    "fxVideo0016": 600,
}


def api_videos(exclude=()):
    out = {}
    for vid, dur in DURATIONS.items():
        if vid in exclude:
            continue
        n = int(vid[-4:])
        out[vid] = VideoInfo(vid, f"API title {n}", CHANNEL_A if n % 2 else CHANNEL_B,
                             "Channel A" if n % 2 else "Channel B", dur)
    return out


@pytest.fixture
def api():
    return FakeYouTubeApi(api_videos())


def payload(pages, *, captured_at=CAPTURED_AT, cursor=None, cursor_reached=False, **extra) -> bytes:
    body = {"schema": 1, "captured_at": captured_at, "extension_version": "0.1.0",
            "cursor": cursor, "cursor_reached": cursor_reached, "pages": pages}
    body.update(extra)
    return json.dumps(body).encode()


def rows(conn):
    return {r["item_key"]: r for r in conn.execute("SELECT * FROM measurements ORDER BY id")}


def sections_of(page):
    return [s for s in youtube._sections(page)]


# ---------------------------------------------------------------------------
# Date headers
# ---------------------------------------------------------------------------

WED = date(2026, 9, 16)


@pytest.mark.parametrize("text,expected", [
    ("Today", date(2026, 9, 16)),
    ("today", date(2026, 9, 16)),
    ("Yesterday", date(2026, 9, 15)),
    ("Monday", date(2026, 9, 14)),
    ("Sunday", date(2026, 9, 13)),
    ("Thursday", date(2026, 9, 10)),
    ("Wednesday", date(2026, 9, 9)),         # never today: seven days back
    ("Sep 9", date(2026, 9, 9)),
    ("Sep 16", date(2026, 9, 16)),           # the capture day itself is "not after"
    ("Sep 17", date(2025, 9, 17)),           # would be in the future -> last year
    ("Dec 31", date(2025, 12, 31)),
    ("September 9", date(2026, 9, 9)),
    ("Sept 9", date(2026, 9, 9)),
    ("Aug 30, 2025", date(2025, 8, 30)),
    ("  Aug   30,  2025 ", date(2025, 8, 30)),
    ("Feb 29", date(2024, 2, 29)),           # leap day walks back to a leap year
])
def test_resolve_date_header(text, expected):
    assert youtube.resolve_date_header(text, WED) == expected


def test_weekday_across_month_and_year_boundary():
    assert youtube.resolve_date_header("Friday", date(2027, 1, 2)) == date(2027, 1, 1)
    assert youtube.resolve_date_header("Thursday", date(2027, 1, 2)) == date(2026, 12, 31)
    assert youtube.resolve_date_header("Dec 31", date(2027, 1, 2)) == date(2026, 12, 31)


@pytest.mark.parametrize("text", ["Mañana", "Last week", "", "Sep 32", "Feb 30, 2025", "Foo 3", "9 Sep"])
def test_unknown_or_impossible_header_raises(text):
    with pytest.raises(YouTubeParseError):
        youtube.resolve_date_header(text, WED)


def test_headers_resolve_in_settings_tz_across_warsaw_midnight(initial):
    # 22:30 UTC on the 15th is 00:30 on the 16th in Warsaw (CEST, UTC+2).
    after = youtube.parse_pages([initial], captured_at="2026-09-15T22:30:00+00:00", tz=TZ)
    before = youtube.parse_pages([initial], captured_at="2026-09-15T21:59:59+00:00", tz=TZ)
    assert after.entries[0].watch_date == "2026-09-16"
    assert before.entries[0].watch_date == "2026-09-15"
    # And the same instant read in UTC would still be the 15th.
    assert youtube.parse_pages([initial], captured_at="2026-09-15T22:30:00+00:00",
                               tz="UTC").entries[0].watch_date == "2026-09-15"


# ---------------------------------------------------------------------------
# Page parsing
# ---------------------------------------------------------------------------

def test_parse_initial_page(initial):
    parsed = youtube.parse_pages([initial], captured_at=CAPTURED_AT, tz=TZ)
    assert parsed.warnings == []
    got = [(e.video_id, e.watch_date, e.is_short, e.percent) for e in parsed.entries]
    assert got == [
        ("fxVideo0001", "2026-09-16", True, None),
        ("fxVideo0002", "2026-09-16", True, None),
        ("fxVideo0003", "2026-09-15", True, None),
        ("fxVideo0004", "2026-09-15", False, 45.0),
        ("fxVideo0005", "2026-09-15", False, 100.0),
        ("fxVideo0006", "2026-09-14", False, 100.0),
        ("fxVideo0007", "2026-09-14", False, 10.0),
        ("fxVideo0008", "2026-09-11", False, 67.0),
        ("fxVideo0009", "2026-09-11", False, None),
        ("fxVideo0010", "2026-09-10", True, None),
        ("fxVideo0011", "2026-09-10", True, None),
        ("fxVideo0012", "2026-09-09", True, None),
        ("fxVideo0013", "2026-09-09", False, 92.0),
    ]
    assert [e.order for e in parsed.entries] == list(range(13))


def test_regular_video_page_metadata(initial):
    e = {x.video_id: x for x in youtube.parse_pages([initial], captured_at=CAPTURED_AT, tz=TZ).entries}
    assert e["fxVideo0004"].title == "Fixture video 4"
    assert e["fxVideo0004"].channel_id == "UCfixtureChannel00000001"
    assert e["fxVideo0004"].channel_name == "Fixture channel 4"
    assert e["fxVideo0004"].resume_s == 245
    # A collaboration renders an avatar stack naming two channels: no single page channel.
    assert e["fxVideo0006"].channel_id is None
    # Shorts carry no channel on the page.
    assert e["fxVideo0001"].channel_id is None and e["fxVideo0001"].title == "Fixture short 1"


def test_live_item_without_progress_bar_is_none_not_zero(initial):
    e = {x.video_id: x for x in youtube.parse_pages([initial], captured_at=CAPTURED_AT, tz=TZ).entries}
    assert e["fxVideo0009"].percent is None
    assert e["fxVideo0009"].live_badge is True


def test_continuation_envelope_parses_with_the_same_code(initial, continuation):
    parsed = youtube.parse_pages([initial, continuation], captured_at=CAPTURED_AT, tz=TZ)
    tail = [(e.video_id, e.watch_date, e.percent) for e in parsed.entries[13:]]
    assert tail == [
        ("fxVideo0014", "2026-08-25", 100.0),
        ("fxVideo0015", "2026-08-25", 100.0),
        ("fxVideo0015", "2026-08-24", 100.0),
        ("fxVideo0016", "2026-08-24", 30.0),
    ]
    assert [e.order for e in parsed.entries] == list(range(17))


def test_no_pages_is_a_parse_error():
    with pytest.raises(YouTubeParseError, match="no pages"):
        youtube.parse_pages([], captured_at=CAPTURED_AT, tz=TZ)


def test_page_without_sections_is_a_parse_error(initial):
    broken = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": []}}}
    with pytest.raises(YouTubeParseError, match="no itemSectionRenderer"):
        youtube.parse_pages([initial, broken], captured_at=CAPTURED_AT, tz=TZ)


def test_renamed_section_renderer_is_a_parse_error(initial):
    text = json.dumps(initial).replace("itemSectionRenderer", "historyDaySectionRenderer")
    with pytest.raises(YouTubeParseError):
        youtube.parse_pages([json.loads(text)], captured_at=CAPTURED_AT, tz=TZ)


def test_unknown_header_fails_the_whole_parse(initial):
    sections_of(initial)[2]["header"]["itemSectionHeaderRenderer"]["title"] = {"simpleText": "Lunes"}
    with pytest.raises(YouTubeParseError, match="Lunes"):
        youtube.parse_pages([initial], captured_at=CAPTURED_AT, tz=TZ)


def test_missing_header_fails(initial):
    del sections_of(initial)[1]["header"]
    with pytest.raises(YouTubeParseError, match="no date header"):
        youtube.parse_pages([initial], captured_at=CAPTURED_AT, tz=TZ)


def test_explicit_year_header(initial):
    sections_of(initial)[5]["header"]["itemSectionHeaderRenderer"]["title"] = {"simpleText": "Sep 9, 2025"}
    parsed = youtube.parse_pages([initial], captured_at=CAPTURED_AT, tz=TZ)
    assert parsed.entries[-1].watch_date == "2025-09-09"


def test_unrecognised_items_are_warnings_not_silent_drops(initial):
    sec = sections_of(initial)[1]
    sec["contents"].append({"videoRenderer": {"videoId": "legacy00001"}})
    playlist = copy.deepcopy(sec["contents"][1])
    playlist["lockupViewModel"]["contentType"] = "LOCKUP_CONTENT_TYPE_PLAYLIST"
    sec["contents"].append(playlist)
    parsed = youtube.parse_pages([initial], captured_at=CAPTURED_AT, tz=TZ)
    assert len(parsed.entries) == 13
    assert any("videoRenderer" in w for w in parsed.warnings)
    assert any("LOCKUP_CONTENT_TYPE_PLAYLIST" in w for w in parsed.warnings)


def test_extract_initial_data_from_html(initial):
    html = ('<html><script nonce="x">var ytInitialData = ' + json.dumps(initial)
            + ';</script><script>var other = 1;</script></html>')
    assert youtube.extract_initial_data(html) == initial
    with pytest.raises(YouTubeParseError):
        youtube.extract_initial_data("<html>nothing</html>")


# ---------------------------------------------------------------------------
# Data API client
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,seconds", [
    ("PT1H2M3S", 3723), ("PT45S", 45), ("PT3M", 180), ("PT2H", 7200), ("P1DT2H", 93600),
    ("P1W", 604800), ("PT1M0S", 60),
    ("P0D", None), ("PT0S", None), ("", None), ("PT", None), ("P", None), ("garbage", None),
    (None, None), (123, None), ("PT1H2M3", None),
])
def test_parse_iso8601_duration(raw, seconds):
    assert youtube.parse_iso8601_duration(raw) == seconds


def video_json(vid, duration="PT10M", channel="UCchan0000000000000000001", title="T"):
    item = {"kind": "youtube#video", "id": vid,
            "snippet": {"title": title, "channelId": channel, "channelTitle": "Chan"}}
    if duration is not None:
        item["contentDetails"] = {"duration": duration}
    else:
        item["contentDetails"] = {}
    return item


def client_with(handler, **settings_overrides):
    s = config.test_settings(youtube_api_key="SECRET-KEY-123", **settings_overrides)
    return DataApiClient(s, transport=httpx.MockTransport(handler))


def test_client_batches_50_ids_and_keeps_key_out_of_the_url():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        ids = request.url.params["id"].split(",")
        return httpx.Response(200, json={"items": [video_json(i, "PT1H2M3S") for i in ids if i != "gone0000003"]})

    ids = [f"vid{n:08d}" for n in range(120)] + ["gone0000003"]
    with client_with(handler) as client:
        got = client.videos(ids + ids[:5])      # duplicates asked once
    assert [len(r.url.params["id"].split(",")) for r in seen] == [50, 50, 21]
    for r in seen:
        assert r.url.host == "www.googleapis.com" and r.url.path == "/youtube/v3/videos"
        assert r.url.params["part"] == "contentDetails,snippet"
        assert r.headers["X-Goog-Api-Key"] == "SECRET-KEY-123"
        assert "SECRET-KEY-123" not in str(r.url)
    assert len(got) == 120 and "gone0000003" not in got
    assert got["vid00000007"] == VideoInfo("vid00000007", "T", "UCchan0000000000000000001", "Chan", 3723)


def test_client_missing_or_live_duration_is_none():
    def handler(request):
        return httpx.Response(200, json={"items": [video_json("live0000001", "P0D"),
                                                   video_json("nodur000001", None)]})

    with client_with(handler) as client:
        got = client.videos(["live0000001", "nodur000001"])
    assert got["live0000001"].duration_s is None
    assert got["nodur000001"].duration_s is None


def test_client_video_info():
    def handler(request):
        return httpx.Response(200, json={"items": [video_json("one00000001", "PT30S")]})

    with client_with(handler) as client:
        assert client.video_info("one00000001").duration_s == 30
        assert client.video_info("missing0001") is None


@pytest.mark.parametrize("response,match", [
    (httpx.Response(403, json={"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}), "quota"),
    (httpx.Response(400, json={"error": {"code": 400, "errors": [{"reason": "keyInvalid"}]}}), "keyInvalid"),
    (httpx.Response(403, text="<html>forbidden</html>"), "HTTP 403"),
    (httpx.Response(500, text="oops"), "HTTP 500"),
    (httpx.Response(200, text="not json"), "non-JSON"),
    (httpx.Response(200, json={"kind": "youtube#videoListResponse"}), "items"),
])
def test_client_failures_raise_video_lookup_error_without_the_key(response, match):
    with client_with(lambda request: response) as client:
        with pytest.raises(VideoLookupError, match=match) as exc:
            client.videos(["abc00000001"])
    assert "SECRET-KEY-123" not in str(exc.value)


def test_client_transport_error_is_video_lookup_error(caplog):
    def handler(request):
        raise httpx.ConnectError("connection refused for key SECRET-KEY-123", request=request)

    caplog.set_level(logging.DEBUG)
    with client_with(handler) as client:
        with pytest.raises(VideoLookupError) as exc:
            client.videos(["abc00000001"])
    assert "SECRET-KEY-123" not in str(exc.value)
    assert exc.value.__cause__ is None
    assert "SECRET-KEY-123" not in caplog.text


def test_client_never_logs_the_key(caplog):
    caplog.set_level(logging.DEBUG)
    with client_with(lambda r: httpx.Response(200, json={"items": [video_json("abc00000001")]})) as client:
        client.videos(["abc00000001"])
    assert "SECRET-KEY-123" not in caplog.text


def test_client_without_key_refuses():
    s = config.test_settings(youtube_api_key="")
    client = DataApiClient(s, transport=httpx.MockTransport(lambda r: pytest.fail("no request expected")))
    with pytest.raises(VideoLookupError, match="not configured"):
        client.videos(["abc00000001"])
    assert client.videos([]) == {}


def test_fake_api_surface():
    fake = FakeYouTubeApi([VideoInfo("a0000000001", "t", "UCx", "X", 10)])
    assert fake.video_info("a0000000001").duration_s == 10
    assert fake.video_info("b0000000001") is None
    fake.error = VideoLookupError("quota")
    with pytest.raises(VideoLookupError):
        fake.videos(["a0000000001"])
    assert fake.calls == [["a0000000001"], ["b0000000001"], ["a0000000001"]]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def test_ingest_writes_one_fact_per_video(conn, clk, settings, api, initial, continuation):
    out = youtube.ingest(conn, clk, settings, payload([initial, continuation]), api)

    assert out.status == "partial"                   # the LIVE item has no progress
    assert out.error is None and out.duplicate_of is None
    assert out.measurements_written == 16            # 17 appearances, 0015 listed twice
    assert (out.period_start, out.period_end) == ("2026-08-24", "2026-09-16")
    m = rows(conn)
    assert set(m) == set(DURATIONS)

    expected_minutes = {
        # Partial videos with a saved resume position use it (DR2.1): 245 s, 377 s,
        # and 0013's 527 s clamped to its 300 s duration. 0007 shows the 10% floor
        # with no resume position, so the percentage is all there is.
        "fxVideo0004": 245 / 60, "fxVideo0005": 20.0, "fxVideo0006": 62.05, "fxVideo0007": 1.0,
        "fxVideo0008": 377 / 60, "fxVideo0013": 5.0, "fxVideo0014": 10.0, "fxVideo0015": 10.0,
        "fxVideo0016": 3.0,
        # Shorts: assumed full, true API duration.
        "fxVideo0001": 0.75, "fxVideo0002": 3.0, "fxVideo0003": 1.0, "fxVideo0010": 1.0,
        "fxVideo0011": 1.0, "fxVideo0012": 1.0,
    }
    for vid, minutes in expected_minutes.items():
        assert m[vid]["status"] == "ok", vid
        assert m[vid]["minutes"] == pytest.approx(minutes), vid
    for r in m.values():
        assert r["device"] == "account" and r["provenance"] == "page_extraction"
        assert r["period_start"] == r["period_end"]
        assert r["import_id"] == out.import_id

    imp = conn.execute("SELECT * FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    assert imp["kind"] == "youtube_history" and imp["parser_version"] == youtube.PARSER_VERSION
    assert imp["raw_path"].startswith("youtube_history/") and imp["raw_path"].endswith(".json")
    assert evidence.raw_file(settings, imp["raw_path"]).exists()


def test_sources_are_keyed_by_api_channel_id(conn, clk, settings, api, initial):
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    srcs = {r["source_key"]: r for r in conn.execute("SELECT * FROM sources WHERE kind = 'youtube_channel'")}
    assert set(srcs) == {CHANNEL_A, CHANNEL_B}
    assert srcs[CHANNEL_A]["display_name"] == "Channel A" and srcs[CHANNEL_A]["label"] is None
    r = rows(conn)["fxVideo0004"]
    assert r["source_id"] == srcs[CHANNEL_B]["id"]
    assert r["item_title"] == "API title 4"


def test_partially_watched_detail(conn, clk, settings, api, initial):
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    r = rows(conn)["fxVideo0004"]
    assert r["period_start"] == "2026-09-15"
    assert json.loads(r["detail"]) == {
        "percent": 45.0, "duration_s": 600, "is_short": False, "assumed_full": False,
        "page_channel_id": "UCfixtureChannel00000001", "resume_s": 245, "live_badge": False,
        "also_listed_on": [], "page_order": 3, "minutes_basis": "resume_position",
    }


def test_fully_watched_video(conn, clk, settings, api, initial):
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    r = rows(conn)["fxVideo0005"]
    assert r["minutes"] == pytest.approx(20.0)
    assert json.loads(r["detail"])["percent"] == 100.0


def test_shorts_are_assumed_full_at_api_duration(conn, clk, settings, initial):
    fake = FakeYouTubeApi(api_videos())
    fake.add(VideoInfo("fxVideo0002", "long short", CHANNEL_A, "Channel A", 179))
    youtube.ingest(conn, clk, settings, payload([initial]), fake)
    r = rows(conn)["fxVideo0002"]
    assert r["status"] == "ok" and r["minutes"] == pytest.approx(179 / 60)
    d = json.loads(r["detail"])
    assert d["is_short"] is True and d["assumed_full"] is True and d["percent"] is None
    assert d["duration_s"] == 179


def test_short_without_api_duration_is_duration_missing_not_average(conn, clk, settings, initial):
    fake = FakeYouTubeApi(api_videos())
    fake.add(VideoInfo("fxVideo0001", "s", CHANNEL_A, "Channel A", None))
    out = youtube.ingest(conn, clk, settings, payload([initial]), fake)
    r = rows(conn)["fxVideo0001"]
    assert r["status"] == "duration_missing" and r["minutes"] is None
    assert any("fxVideo0001" in w for w in out.warnings)


def test_missing_progress_is_recorded_null_with_warning(conn, clk, settings, api, initial):
    out = youtube.ingest(conn, clk, settings, payload([initial]), api)
    r = rows(conn)["fxVideo0009"]
    assert r["status"] == "progress_missing"
    assert r["minutes"] is None
    assert out.status == "partial"
    assert any("fxVideo0009" in w and "no progress" in w and "LIVE" in w for w in out.warnings)


def test_missing_progress_on_ordinary_video(conn, clk, settings, api, initial):
    lockup = sections_of(initial)[1]["contents"][1]["lockupViewModel"]
    overlays = lockup["contentImage"]["thumbnailViewModel"]["overlays"]
    for o in overlays:
        o.get("thumbnailBottomOverlayViewModel", {}).pop("progressBar", None)
    out = youtube.ingest(conn, clk, settings, payload([initial]), api)
    r = rows(conn)["fxVideo0004"]
    assert (r["status"], r["minutes"]) == ("progress_missing", None)
    assert out.status == "partial" and any("fxVideo0004" in w for w in out.warnings)


def test_duration_missing_from_api(conn, clk, settings, api, initial):
    api.add(VideoInfo("fxVideo0008", "t", CHANNEL_A, "Channel A", None))
    out = youtube.ingest(conn, clk, settings, payload([initial]), api)
    r = rows(conn)["fxVideo0008"]
    assert (r["status"], r["minutes"]) == ("duration_missing", None)
    assert json.loads(r["detail"])["percent"] == 67.0
    assert any("fxVideo0008" in w for w in out.warnings)


def test_video_absent_from_api_falls_back_to_page_channel(conn, clk, settings, initial):
    fake = FakeYouTubeApi(api_videos(exclude={"fxVideo0004", "fxVideo0001"}))
    out = youtube.ingest(conn, clk, settings, payload([initial]), fake)
    m = rows(conn)
    assert (m["fxVideo0004"]["status"], m["fxVideo0004"]["minutes"]) == ("duration_missing", None)
    src = conn.execute("SELECT * FROM sources WHERE id = ?", (m["fxVideo0004"]["source_id"],)).fetchone()
    assert (src["source_key"], src["display_name"]) == ("UCfixtureChannel00000001", "Fixture channel 4")
    assert m["fxVideo0004"]["item_title"] == "Fixture video 4"
    # A Short has no page channel: an explicit unknown source, never a guessed one.
    src = conn.execute("SELECT * FROM sources WHERE id = ?", (m["fxVideo0001"]["source_id"],)).fetchone()
    assert src["source_key"] == youtube.UNKNOWN_CHANNEL_KEY
    assert out.status == "partial"


def test_out_of_range_percent_is_unreadable(conn, clk, settings, api, initial):
    bar = next(youtube._find(sections_of(initial)[1], "thumbnailOverlayProgressBarViewModel"))
    bar["startPercent"] = 140
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    r = rows(conn)["fxVideo0004"]
    assert (r["status"], r["minutes"]) == ("unreadable", None)


def test_repeat_listings_measure_the_shared_position_once(conn, clk, settings, api, initial, continuation):
    youtube.ingest(conn, clk, settings, payload([initial, continuation]), api)
    r = conn.execute("SELECT * FROM measurements WHERE item_key = 'fxVideo0015'").fetchall()
    assert len(r) == 1
    assert r[0]["period_start"] == "2026-08-25"               # newest listing
    assert json.loads(r[0]["detail"])["also_listed_on"] == ["2026-08-24"]


def test_short_listed_on_two_days_is_two_viewings(conn, clk, settings, api, initial):
    today = sections_of(initial)[0]["contents"][0]["reelShelfRenderer"]["items"]
    sep9 = sections_of(initial)[5]["contents"][0]["reelShelfRenderer"]["items"]
    sep9.append(copy.deepcopy(today[0]))                      # fxVideo0001 also on Sep 9
    today.append(copy.deepcopy(today[0]))                     # and twice on Today
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    got = conn.execute("SELECT period_start FROM measurements WHERE item_key = 'fxVideo0001' "
                       "ORDER BY period_start").fetchall()
    assert [g["period_start"] for g in got] == ["2026-09-09", "2026-09-16"]


def test_no_time_of_day_or_sittings_are_recorded(conn, clk, settings, api, initial):
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    for r in rows(conn).values():
        assert len(r["period_start"]) == 10
        d = json.loads(r["detail"])
        assert not {"time", "watched_at", "sittings", "sessions"} & set(d)


# --- structural failures ----------------------------------------------------

def test_zero_sections_fails_loudly_and_keeps_raw(conn, clk, settings, api):
    data = payload([{"contents": {"twoColumnBrowseResultsRenderer": {"tabs": []}}}])
    out = youtube.ingest(conn, clk, settings, data, api)
    assert out.status == "failed" and out.measurements_written == 0
    assert "no itemSectionRenderer" in out.error
    imp = conn.execute("SELECT * FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    assert imp["status"] == "failed" and "no itemSectionRenderer" in imp["error"]
    assert evidence.raw_file(settings, imp["raw_path"]).read_bytes() == data
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
    assert api.calls == []


def test_missing_initial_data_fails(conn, clk, settings, api):
    out = youtube.ingest(conn, clk, settings, payload([]), api)
    assert out.status == "failed" and "no pages" in out.error


def test_sections_without_videos_fail_rather_than_report_an_empty_week(conn, clk, settings, api, initial):
    for s in sections_of(initial):
        s["contents"] = []
    out = youtube.ingest(conn, clk, settings, payload([initial]), api)
    assert out.status == "failed" and "no readable videos" in out.error


@pytest.mark.parametrize("data,match", [
    (b"not json", "not UTF-8 JSON"),
    (b"\xff\xfe", "not UTF-8 JSON"),
    (b"[]", "not a JSON object"),
    (json.dumps({"schema": 2, "captured_at": CAPTURED_AT, "pages": []}).encode(), "schema"),
    (json.dumps({"schema": 1, "pages": []}).encode(), "captured_at"),
    (json.dumps({"schema": 1, "captured_at": "yesterday", "pages": []}).encode(), "captured_at"),
    (json.dumps({"schema": 1, "captured_at": CAPTURED_AT}).encode(), "pages"),
])
def test_malformed_payloads_fail(conn, clk, settings, api, data, match):
    out = youtube.ingest(conn, clk, settings, data, api)
    assert out.status == "failed" and match in out.error


def test_unknown_header_fails_import_and_writes_nothing(conn, clk, settings, api, initial):
    sections_of(initial)[3]["header"]["itemSectionHeaderRenderer"]["title"] = {"simpleText": "Vendredi"}
    out = youtube.ingest(conn, clk, settings, payload([initial]), api)
    assert out.status == "failed" and "Vendredi" in out.error
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE kind = 'youtube_channel'").fetchone()[0] == 0
    assert youtube.current_cursor(conn) is None


# --- cursor -------------------------------------------------------------------

def test_first_import_sets_cursor_to_newest_video(conn, clk, settings, api, initial):
    assert youtube.current_cursor(conn) is None
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    assert youtube.current_cursor(conn) == "fxVideo0001"
    assert youtube.current_cursor_date(conn) == "2026-09-16"


def test_cursor_video_and_older_are_skipped(conn, clk, settings, api, initial, continuation):
    evidence.set_meta(conn, youtube.CURSOR_KEY, "fxVideo0008")
    evidence.set_meta(conn, youtube.CURSOR_DATE_KEY, "2026-09-11")
    out = youtube.ingest(conn, clk, settings, payload([initial, continuation], cursor="fxVideo0008",
                                                      cursor_reached=True), api)
    assert out.status == "ok" and out.warnings == ()
    assert set(rows(conn)) == {f"fxVideo{n:04d}" for n in range(1, 8)}
    assert (out.period_start, out.period_end) == ("2026-09-14", "2026-09-16")
    assert api.calls == [[f"fxVideo{n:04d}" for n in range(1, 8)]]
    assert youtube.current_cursor(conn) == "fxVideo0001"


def test_rewatched_cursor_video_at_the_top_is_not_the_stopping_point(conn, clk, settings, api, initial,
                                                                    continuation):
    evidence.set_meta(conn, youtube.CURSOR_KEY, "fxVideo0013")
    evidence.set_meta(conn, youtube.CURSOR_DATE_KEY, "2026-09-09")
    rewatch = copy.deepcopy(sections_of(initial)[5]["contents"][1])     # fxVideo0013 lockup
    sections_of(initial)[0]["contents"].append(rewatch)                  # ...listed again Today
    out = youtube.ingest(conn, clk, settings, payload([initial, continuation]), api)
    m = rows(conn)
    assert "fxVideo0012" in m                                            # listed above the real cursor
    assert not {"fxVideo0014", "fxVideo0015", "fxVideo0016"} & set(m)    # older than it
    assert m["fxVideo0013"]["period_start"] == "2026-09-16"
    assert all("fxVideo0009" in w for w in out.warnings)                 # only the LIVE item


def test_cursor_not_reached_is_a_warning(conn, clk, settings, api, initial):
    evidence.set_meta(conn, youtube.CURSOR_KEY, "olderVideo1")
    evidence.set_meta(conn, youtube.CURSOR_DATE_KEY, "2026-09-01")
    out = youtube.ingest(conn, clk, settings, payload([initial], cursor="olderVideo1", cursor_reached=False), api)
    assert out.status == "partial"
    assert any("not reached" in w and "page cap" in w for w in out.warnings)
    assert youtube.current_cursor(conn) == "fxVideo0001"


def test_nothing_new_since_cursor_is_ok_with_zero_facts(conn, clk, settings, api, initial):
    evidence.set_meta(conn, youtube.CURSOR_KEY, "fxVideo0001")
    evidence.set_meta(conn, youtube.CURSOR_DATE_KEY, "2026-09-16")
    out = youtube.ingest(conn, clk, settings, payload([initial], cursor_reached=True), api)
    assert (out.status, out.measurements_written, out.period_start) == ("ok", 0, None)
    assert api.calls == []
    assert youtube.current_cursor(conn) == "fxVideo0001"


def test_failed_import_leaves_cursor(conn, clk, settings, initial):
    evidence.set_meta(conn, youtube.CURSOR_KEY, "fxVideo0013")
    evidence.set_meta(conn, youtube.CURSOR_DATE_KEY, "2026-09-09")
    out = youtube.ingest(conn, clk, settings, payload([initial]), FakeYouTubeApi(error=VideoLookupError("quota")))
    assert out.status == "failed"
    assert youtube.current_cursor(conn) == "fxVideo0013"
    assert youtube.current_cursor_date(conn) == "2026-09-09"


def test_partial_import_advances_cursor(conn, clk, settings, api, initial):
    out = youtube.ingest(conn, clk, settings, payload([initial]), api)
    assert out.status == "partial"
    assert youtube.current_cursor(conn) == "fxVideo0001"


def test_cursor_never_moves_backwards(conn, clk, settings, api, initial):
    evidence.set_meta(conn, youtube.CURSOR_KEY, "futureVid01")
    evidence.set_meta(conn, youtube.CURSOR_DATE_KEY, "2026-09-20")
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    assert youtube.current_cursor(conn) == "futureVid01"


# --- duplicates and failure recovery -----------------------------------------

def test_same_bytes_twice_is_a_duplicate(conn, clk, settings, api, initial):
    data = payload([initial])
    first = youtube.ingest(conn, clk, settings, data, api)
    n_imports = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    n_rows = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    again = youtube.ingest(conn, clk, settings, data, api)
    assert again.duplicate_of == first.import_id
    assert again.import_id == first.import_id and again.measurements_written == 0
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == n_imports
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == n_rows
    assert len(api.calls) == 1


def test_api_failure_fails_import_keeps_raw_and_reparse_recovers(conn, clk, settings, initial):
    data = payload([initial])
    down = FakeYouTubeApi(error=VideoLookupError("YouTube Data API quota exhausted (HTTP 403: quotaExceeded)"))
    failed = youtube.ingest(conn, clk, settings, data, down)
    assert failed.status == "failed" and "quota" in failed.error
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE kind = 'youtube_channel'").fetchone()[0] == 0
    imp = conn.execute("SELECT * FROM imports WHERE id = ?", (failed.import_id,)).fetchone()
    assert evidence.raw_file(settings, imp["raw_path"]).read_bytes() == data
    assert youtube.current_cursor(conn) is None

    # The same bytes sent again are not a duplicate of a *failed* import.
    retry_bytes_outcome = youtube.ingest(conn, clk, settings, data, down)
    assert retry_bytes_outcome.duplicate_of is None and retry_bytes_outcome.status == "failed"

    clk.advance(days=1)
    ok = youtube.reparse(conn, clk, settings, failed.import_id, FakeYouTubeApi(api_videos()))
    assert ok.status == "partial" and ok.import_id not in (failed.import_id, retry_bytes_outcome.import_id)
    assert ok.measurements_written == 13
    assert youtube.current_cursor(conn) == "fxVideo0001"
    new = conn.execute("SELECT * FROM imports WHERE id = ?", (ok.import_id,)).fetchone()
    assert new["raw_path"] == imp["raw_path"] and new["received_at"] == "2026-09-17T10:05:00+00:00"


def test_reparse_writes_a_new_import_and_replaces_facts(conn, clk, settings, api, initial):
    first = youtube.ingest(conn, clk, settings, payload([initial]), api)
    api.add(VideoInfo("fxVideo0004", "retitled", CHANNEL_B, "Channel B", 1200))
    second = youtube.reparse(conn, clk, settings, first.import_id, api)
    assert second.import_id != first.import_id and second.duplicate_of is None
    r = rows(conn)["fxVideo0004"]
    # Minutes come from the saved 245 s position, which a longer API duration doesn't change.
    assert r["import_id"] == second.import_id and r["minutes"] == pytest.approx(245 / 60)
    assert r["item_title"] == "retitled"
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 13
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 2


def test_reparse_of_old_import_uses_its_cursor_and_does_not_rewind(conn, clk, settings, api, initial, continuation):
    evidence.set_meta(conn, youtube.CURSOR_KEY, "fxVideo0014")
    evidence.set_meta(conn, youtube.CURSOR_DATE_KEY, "2026-08-25")
    first = youtube.ingest(conn, clk, settings, payload([initial, continuation]), api)
    assert first.measurements_written == 13
    # A later sync moves the cursor on.
    evidence.set_meta(conn, youtube.CURSOR_KEY, "laterVideo1")
    evidence.set_meta(conn, youtube.CURSOR_DATE_KEY, "2026-09-23")
    again = youtube.reparse(conn, clk, settings, first.import_id, api)
    assert again.measurements_written == 13                  # original cursor, not the current one
    assert youtube.current_cursor(conn) == "laterVideo1"


def test_reparse_unknown_import(conn, clk, settings, api):
    out = youtube.reparse(conn, clk, settings, 999, api)
    assert out.status == "failed" and out.import_id is None and "999" in out.error


def test_diagnostic_start_fixed_by_first_youtube_import(conn, clk, settings, api, initial):
    youtube.ingest(conn, clk, settings, payload([initial]), api)
    assert evidence.get_meta(conn, evidence.DIAGNOSTIC_START_KEY) == "2026-09-09"


def test_resume_position_beats_the_ten_percent_display_floor(conn, clk, settings, api, initial):
    """A barely-started video renders a 10% bar; the saved position is the measurement."""
    page = json.loads(json.dumps(initial))
    for obj in _walk(page):
        if isinstance(obj, dict) and obj.get("contentId") == "fxVideo0004":
            for bar in _walk(obj):
                if isinstance(bar, dict) and "thumbnailOverlayProgressBarViewModel" in bar:
                    bar["thumbnailOverlayProgressBarViewModel"]["startPercent"] = 10
            for cmd in _walk(obj):
                if isinstance(cmd, dict) and "startTimeSeconds" in cmd:
                    cmd["startTimeSeconds"] = 57
    youtube.ingest(conn, clk, settings, payload([page]), api)
    r = rows(conn)["fxVideo0004"]
    assert r["minutes"] == pytest.approx(57 / 60)          # not 10% of 600 s = 1.0
    assert json.loads(r["detail"])["minutes_basis"] == "resume_position"


def test_history_before_go_live_is_not_measured(conn, clk, api, initial, tmp_path):
    live = config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw2", go_live="2026-09-13")
    out = youtube.ingest(conn, clk, live, payload([initial]), api)
    assert out.period_start >= "2026-09-13"
    assert all(r["period_start"] >= "2026-09-13" for r in rows(conn).values())
    meta = json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?", (out.import_id,)).fetchone()[0])
    assert meta["skipped_before_go_live"] > 0
    assert evidence.get_meta(conn, "diagnostic_start") == out.period_start


def _walk(obj):
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def test_unstorable_page_value_fails_the_import_instead_of_crashing(conn, clk, settings, api, initial):
    """Phase 4 regression: a negative resume position made minutes negative;
    evidence.upsert_measurement raised EvidenceError, which escaped ingest,
    left the import 'did not finish' and answered the extension with HTTP 500."""
    for we in youtube._find(sections_of(initial)[1], "watchEndpoint"):
        if isinstance(we, dict) and we.get("videoId") == "fxVideo0004":
            we["startTimeSeconds"] = -30
    out = youtube.ingest(conn, clk, settings, payload([initial]), api)
    assert out.status == "failed" and "negative minutes" in (out.error or "")
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
    row = conn.execute("SELECT status, error FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    assert row["status"] == "failed" and "negative minutes" in row["error"]
    assert youtube.current_cursor(conn) is None
