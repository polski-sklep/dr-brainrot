"""Screen Time: duration parsing, the OCR-page parser, and the ingest (CONTRACTS Part II §6.2).

Apple Vision is never called here. Every test feeds recorded, sanitised
observations from `tests/fixtures/screentime/` through fake `ocr` callables,
and an autouse guard makes any accidental real OCR call fail loudly.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import replace

import pytest

import clock
import config
import db
import evidence
from sources import ocr_vision, screentime as st
from sources.ocr_vision import TextObservation as TO

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "screentime"
TZ = "Europe/Warsaw"
RECEIVED = "2026-09-16T10:11:00+00:00"          # Wednesday; last week = Sun 6 .. Sat 12 Sep

#: The real Vision entry points, kept before the autouse guard replaces them, for the local-only check.
REAL_RECOGNIZE, REAL_RECOGNIZE_REGION = ocr_vision.recognize, ocr_vision.recognize_region


def load(name: str) -> tuple[list[list[TO]], dict]:
    doc = json.loads((FIXTURES / name).read_text())
    return [[TO.from_dict(o) for o in page] for page in doc["pages"]], doc["region_recovery"]


def images(n: int = 10) -> list[tuple[bytes, str]]:
    return [(f"screenshot-{i}".encode(), f"IMG_{i}.PNG") for i in range(n)]


def fake_ocr(pages):
    def ocr(data: bytes):
        return pages[int(data.decode().split("-")[1])]
    return ocr


def fake_region(recovery):
    """Returns the recorded crop read for the unnamed row, for every crop tried over it."""
    obs = TO.from_dict(recovery["observation"])

    def region(data: bytes, x, y, w, h):
        page = int(data.decode().split("-")[1])
        if page == recovery["page"] and y <= obs.y <= y + h:
            return [obs]
        return []
    return region


def no_region(data, x, y, w, h):
    return []


def parse(pages, received_at=RECEIVED):
    return st.parse_observations(pages, received_at=received_at, tz=TZ)


def apps_by_name(result):
    return {a.name: a for a in result.app_rows}


def edit(pages, old: str, new: str, page: int | None = None):
    out = []
    for i, p in enumerate(pages):
        out.append([replace(o, text=new) if o.text == old and (page is None or i == page) else o for o in p])
    return out


@pytest.fixture(autouse=True)
def vision_is_off_limits(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a unit test reached Apple Vision; inject a fake ocr instead")
    monkeypatch.setattr(ocr_vision, "recognize", boom)
    monkeypatch.setattr(ocr_vision, "recognize_region", boom)


@pytest.fixture
def settings(tmp_path):
    return config.test_settings(tz=TZ, raw_dir=tmp_path / "raw")


@pytest.fixture
def clk():
    return clock.FrozenClock(RECEIVED)


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


# --- parse_duration ----------------------------------------------------------------

@pytest.mark.parametrize("text,minutes", [
    ("1h 17m", 77), ("23m", 23), ("53s", 53 / 60), ("2h", 120), ("1h 2m", 62), ("1h2m", 62),
    ("• 11m", 11), ("• 17ml", 17), ("Im", 1), ("4S", 4 / 60), ("15 min", 15), ("1 hr", 60),
    ("0m", 0), ("1h 0m 5s", 60 + 5 / 60), ("1O m", 10),
])
def test_parse_duration_reads_screen_time_formats_and_observed_ocr_quirks(text, minutes):
    assert st.parse_duration(text) == pytest.approx(minutes)


@pytest.mark.parametrize("text", [
    "", "66", "262", "• 31", "022", "Sunday: 90", "1h 75m", "90s", "15 min >", "Brave", "38% from last week",
    "12:11", "h", "m", "minutes",
])
def test_parse_duration_rejects_counts_and_non_durations(text):
    assert st.parse_duration(text) is None


# --- parse_observations -------------------------------------------------------------

def test_last_week_parse_reads_header_and_the_whole_most_used_list():
    pages, _ = load("last_week.json")
    r = parse(pages)
    assert r.device_name == "Test Phone"
    assert r.week_label == "last week"
    assert (r.period_start, r.period_end) == ("2026-09-06", "2026-09-12")
    assert r.is_full_past_week
    apps = apps_by_name(r)
    assert len(apps) == 38
    assert apps["Lionfish"].minutes == 148
    assert apps["Roadie"].minutes == 141
    assert apps["Snapshots"].minutes == 28                   # '• 28ml'
    assert apps["Bankly"].minutes == 1                       # 'Im'
    assert apps["Ringer"].minutes == pytest.approx(4 / 60)    # '4S'
    assert apps["lnkpad"].raw == ("lnkpad | 18m",)            # a misread name is kept verbatim, not corrected
    assert r.meta["most_used_complete"] is True


def test_overlapping_screenshots_are_deduplicated_by_app_name():
    pages, _ = load("last_week.json")
    r = parse(pages)
    lionfish = apps_by_name(r)["Lionfish"]
    assert lionfish.pages == (1, 2)
    assert [a.name for a in r.app_rows].count("Lionfish") == 1


def test_count_sections_never_become_minutes():
    pages, _ = load("last_week.json")
    r = parse(pages)
    apps = apps_by_name(r)
    assert "Callbox" not in apps            # only listed under Notifications
    assert "Total Pickups" not in apps
    # Lionfish is also listed under First Used After Pickup and Notifications with counts.
    assert apps["Lionfish"].raw == ("Lionfish | 2h 28m",)


def test_summary_categories_and_total():
    pages, _ = load("last_week.json")
    r = parse(pages)
    assert r.categories == [("Entertainment", 262), ("Creativity", 156), ("Education", 148)]
    assert r.total_minutes == 554
    assert r.daily_average_minutes == 356


def test_unnamed_row_is_a_warning_not_a_guess():
    pages, _ = load("last_week.json")
    r = parse(pages)
    assert any("no readable app name" in w and "'2h 5m'" in w for w in r.warnings)
    assert r.meta["unnamed_rows"] == 1
    assert st.find_unnamed_rows(pages)[0][0] == 1


def test_region_recovery_names_the_row_when_crops_agree():
    pages, recovery = load("last_week.json")
    recovered, notes = st.recover_unnamed_rows(pages, [b for b, _ in images()], fake_region(recovery))
    r = parse(recovered)
    x = apps_by_name(r)["X"]
    assert (x.minutes, x.recovered, x.confidence) == (125, True, 0.3)
    assert not any("no readable app name" in w for w in r.warnings)
    assert notes == []


def test_region_recovery_disagreement_leaves_the_row_unnamed():
    pages, _ = load("last_week.json")
    calls = []

    def disagree(data, x, y, w, h):
        calls.append(y)
        return [TO("X" if len(calls) == 1 else "Xa", 0.3, 0.19, 0.406, 0.03, 0.018, "region")]

    recovered, _ = st.recover_unnamed_rows(pages, [b for b, _ in images()], disagree)
    assert len(calls) == len(st.RECOVERY_CROPS)
    assert "X" not in apps_by_name(parse(recovered))


def test_conflicting_durations_for_one_app_are_unreadable_with_a_warning():
    pages, _ = load("last_week.json")
    pages = edit(pages, "2h 28m", "2h 29m", page=1)          # Lionfish on screenshot 2 only
    r = parse(pages)
    lionfish = apps_by_name(r)["Lionfish"]
    assert (lionfish.minutes, lionfish.status) == (None, "unreadable")
    assert any("Lionfish" in w and "different durations" in w for w in r.warnings)
    assert "Lionfish" not in dict(r.apps)


def test_name_without_duration_is_unreadable_not_zero():
    pages, _ = load("last_week.json")
    pages = [[o for o in p if not (i == 2 and o.text == "22m")] for i, p in enumerate(pages)]  # Postbag's value
    pages = edit(pages, "• 22m", "• ??", page=2)
    r = parse(pages)
    postbag = apps_by_name(r)["Postbag"]
    assert (postbag.minutes, postbag.status) == (None, "unreadable")
    assert any("Postbag" in w and "no readable duration" in w for w in r.warnings)


def test_this_week_is_not_a_full_past_week():
    pages, _ = load("this_week.json")
    r = parse(pages)
    assert r.week_label == "this week"
    assert not r.is_full_past_week
    assert r.period_start is None
    assert any("This Week" in w and "Last Week" in w for w in r.warnings)


def test_all_devices_view_is_refused():
    pages, _ = load("all_devices.json")
    r = parse(pages)
    assert not r.is_full_past_week
    assert any("All Devices" in w for w in r.warnings)


def test_explicit_date_range_header():
    pages, _ = load("this_week.json")
    past = parse(edit(pages, "This Week", "Sep 6 – 12"))
    assert past.is_full_past_week and (past.period_start, past.period_end) == ("2026-09-06", "2026-09-12")
    dm = parse(edit(pages, "This Week", "30 Aug – 5 Sep"))
    assert dm.is_full_past_week and (dm.period_start, dm.period_end) == ("2026-08-30", "2026-09-05")
    current = parse(edit(pages, "This Week", "Sep 13 – 19"))
    assert not current.is_full_past_week
    assert any("isn't over yet" in w for w in current.warnings)
    not_a_week = parse(edit(pages, "This Week", "Sep 7 – 13"))
    assert not not_a_week.is_full_past_week


def test_last_week_received_on_sunday_is_the_week_that_just_ended():
    pages, _ = load("last_week.json")
    r = parse(pages, received_at="2026-09-13T08:00:00+00:00")      # Sunday
    assert (r.period_start, r.period_end) == ("2026-09-06", "2026-09-12")


def test_mixed_week_labels_write_nothing():
    pages, _ = load("last_week.json")
    r = parse(edit(pages, "Last Week", "This Week", page=4))
    assert not r.is_full_past_week
    assert any("different weeks" in w for w in r.warnings)


def test_most_used_list_not_expanded_is_a_warning():
    pages, _ = load("last_week.json")
    r = parse(pages[:2])            # stops at "Show More" while apps of 38 min are still showing
    assert r.meta["most_used_complete"] is False
    assert any("Show More" in w for w in r.warnings)


def test_a_list_cut_off_among_apps_of_seconds_is_not_a_warning():
    """DR5.30: the list already reached 13-second apps, so whatever "Show More" hides is
    smaller still. The flag stays honest; only the request to resend goes."""
    pages, _ = load("last_week.json")
    r = parse(pages[:4])
    assert r.meta["most_used_complete"] is False
    assert not any("Show More" in w for w in r.warnings)


def test_images_that_are_not_screen_time_raise():
    with pytest.raises(st.ScreenTimeParseError):
        parse([[TO("Hello there", 1.0, 0.2, 0.5, 0.3, 0.02)], []])
    with pytest.raises(st.ScreenTimeParseError):
        parse([])


# --- ingest ------------------------------------------------------------------------------

def measurement_rows(conn):
    return conn.execute(
        "SELECT m.*, s.kind, s.source_key FROM measurements m JOIN sources s ON s.id = m.source_id ORDER BY m.id"
    ).fetchall()


def ingest(conn, clk, settings, fixture="last_week.json", *, region=True, **kw):
    pages, recovery = load(fixture)
    return st.ingest_batch(conn, clk, settings, images(), received_at=kw.pop("received_at", RECEIVED),
                           ocr=kw.pop("ocr", fake_ocr(pages)),
                           region_ocr=fake_region(recovery) if region else no_region, **kw)


def test_last_week_ingest_writes_measurements_for_that_sunday_to_saturday(conn, clk, settings):
    out = ingest(conn, clk, settings)
    assert out.status == "ok"
    assert out.measurements_written == 39
    assert (out.period_start, out.period_end) == ("2026-09-06", "2026-09-12")
    rows = measurement_rows(conn)
    assert len(rows) == 39
    assert {(r["device"], r["provenance"], r["period_start"], r["period_end"]) for r in rows} == {
        ("iphone", "screenshot_ocr", "2026-09-06", "2026-09-12")}
    x = [r for r in rows if r["kind"] == "x"]
    assert len(x) == 1 and x[0]["minutes"] == 125 and x[0]["source_key"] == "x"   # X folds into the X source
    detail = json.loads(x[0]["detail"])
    assert detail["name_reread_from_crop"] is True and detail["confidence"] == 0.3
    lionfish = next(r for r in rows if r["source_key"] == "lionfish")
    assert json.loads(lionfish["detail"])["raw"] == ["Lionfish | 2h 28m"]
    assert evidence.get_meta(conn, "diagnostic_start") == "2026-09-06"


def test_category_totals_are_written_in_the_same_import(conn, clk, settings):
    out = ingest(conn, clk, settings)
    cats = conn.execute("SELECT category, minutes, import_id, period_start FROM category_measurements "
                        "ORDER BY category").fetchall()
    assert [(c["category"], c["minutes"]) for c in cats] == [("Creativity", 156), ("Education", 148),
                                                             ("Entertainment", 262)]
    assert {c["import_id"] for c in cats} == {out.import_id}
    meta = json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?", (out.import_id,)).fetchone()["meta"])
    assert meta["total_minutes"] == 554 and meta["categories_written"] == 3


def test_unreadable_row_makes_the_import_partial(conn, clk, settings):
    out = ingest(conn, clk, settings, region=False)
    assert out.status == "partial"
    assert out.measurements_written == 38
    assert any("no readable app name" in w for w in out.warnings)
    assert not [r for r in measurement_rows(conn) if r["kind"] == "x"]


def test_conflict_is_stored_as_unreadable_null(conn, clk, settings):
    pages, _ = load("last_week.json")
    out = ingest(conn, clk, settings, ocr=fake_ocr(edit(pages, "2h 28m", "2h 29m", page=1)))
    assert out.status == "partial"
    row = next(r for r in measurement_rows(conn) if r["source_key"] == "lionfish")
    assert (row["minutes"], row["status"]) == (None, "unreadable")


def test_this_week_ingest_stores_evidence_but_writes_nothing(conn, clk, settings):
    out = ingest(conn, clk, settings, "this_week.json")
    assert (out.status, out.measurements_written, out.period_start) == ("partial", 0, None)
    assert any("Last Week" in w for w in out.warnings)
    assert measurement_rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM category_measurements").fetchone()[0] == 0
    row = conn.execute("SELECT * FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    assert evidence.raw_file(settings, row["raw_path"]).exists()
    assert evidence.get_meta(conn, "diagnostic_start") is None      # a partial week does not start the clock


def test_full_week_with_no_readable_most_used_is_evidence_only_not_a_zero_week(conn, clk, settings):
    """Phase 4 regression: a Last Week capture whose Most Used list is absent
    used to record the week's period with zero app rows, so the week counted
    as covered and the dashboard showed iPhone apps as a received 0 min."""
    pages, _ = load("last_week.json")
    no_most_used = [[o for o in page if o.y < 0.2 or st._key(o.text) in ("total screen time", "daily average")
                     or st.parse_duration(o.text) is None or o.x > 0.5] for page in pages]
    no_most_used = [[o for o in page if st._key(o.text) != "most used"] for page in no_most_used]
    parsed = parse(no_most_used)
    assert parsed.is_full_past_week and not parsed.app_rows
    out = ingest(conn, clk, settings, ocr=fake_ocr(no_most_used))
    assert (out.status, out.measurements_written, out.period_start) == ("partial", 0, None)
    assert any("evidence only" in w for w in out.warnings)
    assert measurement_rows(conn) == []
    row = conn.execute("SELECT period_start FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    assert row["period_start"] is None                      # does not cover the week
    assert evidence.get_meta(conn, "diagnostic_start") is None


def test_reparse_that_reads_no_app_rows_does_not_wipe_the_earlier_facts(conn, clk, settings):
    """Phase 4: an evidence-only re-read is decided before the reparse deletes anything."""
    first = ingest(conn, clk, settings)
    assert first.measurements_written == 39
    pages, _ = load("last_week.json")
    no_most_used = [[o for o in page if o.y < 0.2 or st._key(o.text) in ("total screen time", "daily average")
                     or st.parse_duration(o.text) is None or o.x > 0.5] for page in pages]
    no_most_used = [[o for o in page if st._key(o.text) != "most used"] for page in no_most_used]
    again = st.reparse(conn, clk, settings, first.import_id, ocr=fake_ocr(no_most_used), region_ocr=no_region)
    assert (again.status, again.measurements_written) == ("partial", 0)
    assert len(measurement_rows(conn)) == 39
    assert {r["import_id"] for r in measurement_rows(conn)} == {first.import_id}


def test_all_devices_ingest_writes_nothing(conn, clk, settings):
    out = ingest(conn, clk, settings, "all_devices.json")
    assert (out.status, out.measurements_written) == ("partial", 0)
    assert measurement_rows(conn) == []


def test_duplicate_batch_returns_the_earlier_import(conn, clk, settings):
    first = ingest(conn, clk, settings)
    again = ingest(conn, clk, settings)
    assert again.duplicate_of == first.import_id and again.measurements_written == 0
    later_same_week = ingest(conn, clk, settings, received_at="2026-09-17T09:00:00+00:00")
    assert later_same_week.duplicate_of == first.import_id
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


def test_manifest_is_stored_as_raw_evidence(conn, clk, settings):
    out = ingest(conn, clk, settings)
    row = conn.execute("SELECT * FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    manifest = json.loads(evidence.raw_file(settings, row["raw_path"]).read_bytes())
    assert manifest["received_at"] == RECEIVED
    assert len(manifest["images"]) == 10
    assert evidence.raw_file(settings, manifest["images"][0]["path"]).read_bytes() == b"screenshot-0"
    assert row["raw_sha256"] == evidence.sha256_bytes(evidence.raw_file(settings, row["raw_path"]).read_bytes())


def test_reparse_with_different_ocr_writes_a_new_import_replacing_facts(conn, clk, settings):
    first = ingest(conn, clk, settings)
    pages, recovery = load("last_week.json")
    fewer = [[o for o in p if o.text not in ("Roadie",)] for p in pages]     # the new read misses one app
    fewer = edit(fewer, "2h 28m", "2h 27m")                                   # and reads Lionfish differently
    out = st.reparse(conn, clk, settings, first.import_id, ocr=fake_ocr(fewer), region_ocr=fake_region(recovery))
    assert out.import_id != first.import_id and out.status in ("ok", "partial")
    rows = measurement_rows(conn)
    assert {r["import_id"] for r in rows} == {out.import_id}
    assert "roadie" not in {r["source_key"] for r in rows}
    assert next(r for r in rows if r["source_key"] == "lionfish")["minutes"] == 147
    meta = json.loads(conn.execute("SELECT meta FROM imports WHERE id = ?", (out.import_id,)).fetchone()["meta"])
    assert meta["reparse_of"] == first.import_id


def test_ocr_failure_is_a_failed_import_with_raw_kept(conn, clk, settings):
    def broken(data):
        raise RuntimeError("Vision exploded")
    out = ingest(conn, clk, settings, ocr=broken)
    assert out.status == "failed" and "text recognition failed" in out.error
    row = conn.execute("SELECT * FROM imports WHERE id = ?", (out.import_id,)).fetchone()
    assert row["status"] == "failed"
    manifest = json.loads(evidence.raw_file(settings, row["raw_path"]).read_bytes())
    assert all(evidence.raw_file(settings, i["path"]).exists() for i in manifest["images"])
    assert measurement_rows(conn) == []
    # A failed import is not a duplicate: the same images can be tried again, and re-parsed.
    retry = st.reparse(conn, clk, settings, out.import_id, ocr=fake_ocr(load("last_week.json")[0]),
                       region_ocr=no_region)
    assert retry.status == "partial" and retry.measurements_written == 38


def test_non_screen_time_images_fail(conn, clk, settings):
    out = st.ingest_batch(conn, clk, settings, [(b"cat-0", "cat.jpg")], received_at=RECEIVED,
                          ocr=lambda b: [TO("A cat", 1.0, 0.3, 0.5, 0.2, 0.02)], region_ocr=no_region)
    assert out.status == "failed" and "don't look like Screen Time" in out.error


def test_no_images_is_a_recorded_failure(conn, clk, settings):
    out = st.ingest_batch(conn, clk, settings, [], received_at=RECEIVED, ocr=no_region, region_ocr=no_region)
    assert out.status == "failed" and out.import_id is not None


# --- optional local check against the real screenshots ---------------------------------

REAL_INPUTS = pathlib.Path("/Users/Jacob/Projects/dr-brainrot/inputs")


def test_real_screenshots_local_only():
    """Runs real Vision on inputs/ only when DR_BRAINROT_REAL_OCR=1 and the files exist. Prints nothing personal."""
    import os

    files = sorted(REAL_INPUTS.glob("IMG_52*.PNG"))
    if os.environ.get("DR_BRAINROT_REAL_OCR") != "1" or len(files) != 10:
        pytest.skip("real OCR check is local-only (set DR_BRAINROT_REAL_OCR=1 with inputs/ present)")
    data = [f.read_bytes() for f in files]
    pages = [REAL_RECOGNIZE(b) for b in data]
    pages, _ = st.recover_unnamed_rows(pages, data, REAL_RECOGNIZE_REGION)
    r = st.parse_observations(pages, received_at="2026-09-16T10:11:00+00:00", tz=TZ)
    assert r.week_label == "this week" and not r.is_full_past_week
    assert r.device_name
    assert len(r.app_rows) >= 35
    assert r.meta["unnamed_rows"] == 0 and "X" in r.meta["recovered_names"]
    assert len(r.categories) == 3 and r.total_minutes is not None
