"""Phase 1 shared modules: dr_brainrot_types day/week semantics, evidence, label resolver core."""
from __future__ import annotations

import json
from datetime import date

import pytest

import clock
import config
import db
import dr_brainrot_types as T
import evidence
import labels


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw")


@pytest.fixture
def clk():
    return clock.FrozenClock("2026-09-16T10:00:00+00:00")   # Wednesday


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


# --- weeks ------------------------------------------------------------------

@pytest.mark.parametrize("day,sunday", [
    (date(2026, 9, 13), date(2026, 9, 13)),   # Sunday itself
    (date(2026, 9, 16), date(2026, 9, 13)),   # Wednesday
    (date(2026, 9, 19), date(2026, 9, 13)),   # Saturday
    (date(2026, 9, 20), date(2026, 9, 20)),   # next Sunday
])
def test_week_sunday(day, sunday):
    assert T.week_sunday(day) == sunday


def test_allowance_week_matches_pop_boundary(settings):
    for d in T.measurement_week(date(2026, 9, 13)).days():
        start = T.allowance_week_start_for_day(d, settings.tz)
        assert start == clock.iso(clock.week_start_for(clock.parse("2026-09-13T16:00:00+00:00"), settings.tz))
    assert T.measurement_week_for_allowance_week(start, settings.tz).start == "2026-09-13"


def test_allowance_week_across_dst_end(settings):
    # Europe/Warsaw leaves DST on 2026-10-25 (a Sunday): 18:00 local is 17:00 UTC.
    assert T.allowance_week_start_for_day(date(2026, 10, 27), settings.tz) == "2026-10-25T17:00:00+00:00"
    assert T.allowance_week_start_for_day(date(2026, 10, 24), settings.tz) == "2026-10-18T16:00:00+00:00"


def test_local_date_uses_configured_zone(settings):
    assert T.local_date("2026-09-12T22:30:00+00:00", settings.tz) == date(2026, 9, 13)


def test_item_ref_shape():
    assert T.item_ref("youtube", "fxVideoAAAA") == "youtube:fxVideoAAAA"
    with pytest.raises(ValueError):
        T.item_ref("tiktok", "x")
    with pytest.raises(ValueError):
        T.item_ref("spotify", "a:b")


# --- evidence ---------------------------------------------------------------

def test_store_raw_is_content_addressed_and_idempotent(settings):
    rel, sha = evidence.store_raw(settings, "coldturkey", b"[1]", "json")
    rel2, sha2 = evidence.store_raw(settings, "coldturkey", b"[1]", ".json")
    assert (rel, sha) == (rel2, sha2)
    assert evidence.raw_file(settings, rel).read_bytes() == b"[1]"
    assert oct(evidence.raw_file(settings, rel).stat().st_mode & 0o777) == "0o600"


def _source(conn, clk):
    s = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "https://www.Google.com/search")
    conn.commit()
    return s


def test_import_starts_failed_and_finishes_ok(conn, clk, settings):
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t1")
    assert conn.execute("SELECT status FROM imports WHERE id=?", (rec.id,)).fetchone()[0] == "failed"
    src = _source(conn, clk)
    evidence.upsert_measurement(conn, rec, source_id=src.id, device="desktop", provenance=T.PROV_EXPORT,
                                period_start="2026-09-13", period_end="2026-09-13", minutes=11.83)
    assert evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-13",
                                  period_end="2026-09-19") == "ok"
    assert evidence.get_meta(conn, "diagnostic_start") == "2026-09-13"


def test_warnings_make_partial(conn, clk, settings):
    rec = evidence.begin_import(conn, clk, kind="screentime", parser_version="t1")
    rec.warnings.append("row 3 unreadable")
    assert evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-06",
                                  period_end="2026-09-12") == "partial"
    row = conn.execute("SELECT warnings FROM imports WHERE id=?", (rec.id,)).fetchone()
    assert json.loads(row[0]) == ["row 3 unreadable"]


def test_fail_import_rolls_back_measurements_and_records_error(conn, clk):
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t1")
    src = _source(conn, clk)
    evidence.upsert_measurement(conn, rec, source_id=src.id, device="desktop", provenance=T.PROV_EXPORT,
                                period_start="2026-09-13", period_end="2026-09-13", minutes=5)
    evidence.fail_import(conn, rec, "boom")
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
    assert tuple(conn.execute("SELECT status, error FROM imports WHERE id=?", (rec.id,)).fetchone()) == \
        ("failed", "boom")


@pytest.mark.parametrize("minutes,status", [(0, "unreadable"), (None, "ok"), (3.0, "progress_missing")])
def test_status_and_minutes_must_agree(conn, clk, minutes, status):
    rec = evidence.begin_import(conn, clk, kind="youtube_history", parser_version="t1")
    src = _source(conn, clk)
    with pytest.raises(evidence.EvidenceError):
        evidence.upsert_measurement(conn, rec, source_id=src.id, device="account", provenance=T.PROV_PAGE,
                                    period_start="2026-09-13", period_end="2026-09-13",
                                    minutes=minutes, status=status)


def test_reimport_replaces_fact_on_natural_key(conn, clk, settings):
    src = _source(conn, clk)
    for mins in (5.0, 7.5):
        rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t1")
        evidence.upsert_measurement(conn, rec, source_id=src.id, device="desktop", provenance=T.PROV_EXPORT,
                                    period_start="2026-09-13", period_end="2026-09-13", minutes=mins)
        evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-13", period_end="2026-09-13")
    assert [tuple(r) for r in conn.execute("SELECT minutes, import_id FROM measurements")] == [(7.5, 2)]


def test_diagnostic_start_never_moves(conn):
    assert evidence.ensure_diagnostic_start(conn, "2026-09-13") == "2026-09-13"
    assert evidence.ensure_diagnostic_start(conn, "2026-09-06") == "2026-09-13"


def test_diagnostic_countdown(conn, clk, settings):
    assert evidence.diagnostic(conn, clk, settings).started is False
    evidence.ensure_diagnostic_start(conn, "2026-09-13")
    d = evidence.diagnostic(conn, clk, settings)
    assert (d.end, d.days_remaining, d.week_number, d.ended) == ("2026-11-07", 52, 1, False)
    clk.set("2026-11-08T10:00:00+00:00")
    d = evidence.diagnostic(conn, clk, settings)
    assert (d.days_remaining, d.week_number, d.ended) == (0, 8, True)


# --- labels core ------------------------------------------------------------

@pytest.mark.parametrize("kind,raw,expected", [
    (T.KIND_IPHONE_APP, "X", (T.KIND_X, "x")),
    (T.KIND_WEB_DOMAIN, "https://twitter.com/home", (T.KIND_X, "x")),
    (T.KIND_WEB_DOMAIN, "www.YouTube.com", (T.KIND_WEB_DOMAIN, "youtube.com")),
    (T.KIND_IPHONE_APP, "Google  Maps", (T.KIND_IPHONE_APP, "google maps")),
    (T.KIND_YOUTUBE_CHANNEL, "UCfixtureChannel00000099", (T.KIND_YOUTUBE_CHANNEL, "UCfixtureChannel00000099")),
])
def test_normalise_key(kind, raw, expected):
    assert labels.normalise_key(kind, raw) == expected


def test_resolution_precedence(conn, clk):
    src = labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Show One")
    ref = "spotify:4rOoJ6Egrf8K2IrywzwOMk"
    assert labels.resolve(conn, source_id=src.id, item_ref=ref) == labels.ResolvedLabel("wasteful", T.BASIS_DEFAULT)
    conn.execute("UPDATE sources SET label='worthwhile' WHERE id=?", (src.id,))
    assert labels.resolve(conn, source_id=src.id, item_ref=ref).basis == T.BASIS_SOURCE_LABEL
    conn.execute("INSERT INTO item_labels VALUES (?, 'wasteful', '2026-09-16T10:00:00+00:00', 'capture')", (ref,))
    assert labels.resolve(conn, source_id=src.id, item_ref=ref) == \
        labels.ResolvedLabel("wasteful", T.BASIS_ITEM_OVERRIDE)
    assert labels.resolve(conn, source_id=None) == labels.ResolvedLabel("wasteful", T.BASIS_DEFAULT)


def test_x_surfaces_share_one_seeded_wasteful_source(conn, clk):
    a = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "X")
    b = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "x.com")
    assert a.id == b.id and a.label == "wasteful"


def test_covered_elsewhere(conn, clk):
    yt = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "YouTube")
    assert labels.is_covered_elsewhere(yt)
    # Brave is covered too since DR5.13 (the sites inside it are measured
    # instead), so the counterexample is an app nothing else sees.
    assert labels.is_covered_elsewhere(labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "Brave"))
    assert labels.is_covered_elsewhere(
        labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "Instagram")) is None


@pytest.mark.parametrize("text,expected", [
    ("looked good #worthwhile", ("looked good", "worthwhile")),
    ("#waste but funny", ("but funny", "wasteful")),
    ("for the trip #WORTH by friday", ("for the trip by friday", "worthwhile")),
    ("no token here", ("no token here", None)),
    ("#worth and #waste", ("#worth and #waste", None)),
    ("hash#worthwhile inside a word", ("hash#worthwhile inside a word", None)),
])
def test_parse_override_token(text, expected):
    assert labels.parse_override_token(text) == expected


def test_set_labels(conn, clk):
    src = labels.ensure_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, "UCabc", "Chan")
    assert labels.set_source_label(conn, clk, src.id, "worthwhile").label == "worthwhile"
    labels.set_item_override(conn, clk, "youtube:vid1", "wasteful")
    labels.set_item_override(conn, clk, "youtube:vid1", "worthwhile")
    assert labels.item_override(conn, "youtube:vid1") == "worthwhile"
    with pytest.raises(labels.LabelError):
        labels.set_source_label(conn, clk, 9999, "wasteful")
    with pytest.raises(labels.LabelError):
        labels.set_item_override(conn, clk, "youtube:vid1", "neutral")
