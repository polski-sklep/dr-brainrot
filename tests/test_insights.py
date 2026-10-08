"""The weekly Overview summary (DR5.8): payload, privacy, fallback, storage, migration.

Nothing here reaches the network. `conftest.py` blocks it for the whole suite;
every test that exercises `write_summary` either injects a fake client or runs
the no-key path, so a regression that constructed a real client would fail at
the socket rather than spend money.

The privacy test is the important one. It seeds the database with source names
nobody would write by accident and asserts that not one of them survives into
the payload that would be sent.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import clock  # noqa: E402
import db  # noqa: E402
import evidence  # noqa: E402
import labels  # noqa: E402
import dr_brainrot_types as T  # noqa: E402
import insights  # noqa: E402
import jobs  # noqa: E402
from tests.test_allowance import label_source, make_settings, measure, open_db  # noqa: E402

#: A Wednesday inside the week opening Sunday 2026-09-27. The two complete
#: weeks before it are 09-13..09-19 and 09-20..09-26.
WED = "2026-09-30T10:00:00+00:00"


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, db_path=tmp_path / "dr-brainrot.db", weekly_allowance_min=360,
                         go_live="2026-09-13")


@pytest.fixture
def clk():
    return clock.FrozenClock(WED)


@pytest.fixture
def conn(settings):
    c = open_db(settings)
    yield c
    c.close()


# --- the distinctive names the payload must never carry ---------------------
#: Chosen so a substring match cannot be a false positive: none of these
#: appears anywhere in dr-brainrot's own vocabulary.
SECRET_NAMES = {
    "app": "Zornblatt Timewaster",           # iPhone app
    "domain": "kwarglepuff-news.example",    # web domain
    "channel_title": "Grimbledorf Explains", # YouTube channel display name
    "channel_key": "UCzzQUUXWOBBLEFISHzz0001",
    "show": "The Pendlewhack Hour",          # podcast show
    "mac_app": "com.snorgleflex.chatter",    # Mac app bundle id
    "x_key": "x",
    "video_id": "BLORPvid001",
    "video_title": "Why Blorptangle Matters",
}


def _measure_app(conn, clk, settings, *, key, day, minutes, display):
    """A Qbserve app-day figure. `tests.test_allowance.measure` has no
    `app_tracker` kind, and widening a helper four other modules import is not
    this feature's business — so the two evidence calls are made directly."""
    source = labels.ensure_source(conn, clk, T.KIND_DESKTOP_APP, key, display)
    conn.commit()
    rec = evidence.begin_import(conn, clk, kind="qbserve", parser_version="test-1")
    evidence.upsert_measurement(conn, rec, source_id=source.id, device=T.DEVICE_DESKTOP,
                                provenance=T.PROV_APP_TRACKER, period_start=day, period_end=day,
                                minutes=minutes, status="ok")
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=day)


def _week(conn, clk, settings, *, day: str, minutes: dict[str, float]):
    """One measurement per source for a local day, all with distinctive names."""
    measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key=SECRET_NAMES["app"], day=day,
            minutes=minutes["iphone"], device=T.DEVICE_IPHONE, provenance=T.PROV_SCREENSHOT,
            display=SECRET_NAMES["app"], period_end=day)
    measure(conn, clk, settings, kind=T.KIND_WEB_DOMAIN, key=SECRET_NAMES["domain"], day=day,
            minutes=minutes["web"], display=SECRET_NAMES["domain"])
    measure(conn, clk, settings, kind=T.KIND_YOUTUBE_CHANNEL, key=SECRET_NAMES["channel_key"],
            day=day, minutes=minutes["youtube"], display=SECRET_NAMES["channel_title"],
            item_key=SECRET_NAMES["video_id"], item_title=SECRET_NAMES["video_title"])
    _measure_app(conn, clk, settings, key=SECRET_NAMES["mac_app"], day=day,
                 minutes=minutes["desktop_apps"], display=SECRET_NAMES["mac_app"])


@pytest.fixture
def two_weeks(conn, clk, settings):
    """Two complete weeks of figures: the second much lighter than the first."""
    _week(conn, clk, settings, day="2026-09-16",
          minutes={"iphone": 200.0, "web": 60.0, "youtube": 40.0, "desktop_apps": 7.0})
    _week(conn, clk, settings, day="2026-09-23",
          minutes={"iphone": 100.0, "web": 40.0, "youtube": 20.0, "desktop_apps": 30.0})
    label_source(conn, clk, T.KIND_YOUTUBE_CHANNEL, SECRET_NAMES["channel_key"], T.WORTHWHILE)
    label_source(conn, clk, T.KIND_DESKTOP_APP, SECRET_NAMES["mac_app"], T.TOOL)
    # The iPhone app and the web domain are what these tests mean by wasteful,
    # so they are labelled. An unlabelled source is undecided, not wasteful
    # (DR5.11), and would leave every trend sentence describing nothing.
    label_source(conn, clk, T.KIND_IPHONE_APP, SECRET_NAMES["app"], T.WASTEFUL)
    label_source(conn, clk, T.KIND_WEB_DOMAIN, SECRET_NAMES["domain"], T.WASTEFUL)
    return conn


# --------------------------------------------------------------------------
# build_payload
# --------------------------------------------------------------------------

def test_payload_shape_and_the_weeks_it_covers(two_weeks, clk, settings):
    p = insights.build_payload(two_weeks, clk, settings)

    assert p["schema"] == insights.PAYLOAD_SCHEMA
    assert p["timezone"] == settings.tz
    assert p["generated_for_week_start"] == clock.iso(clock.week_start_for(clk.now(), settings.tz))
    assert set(p) >= {"weeks", "cost_per_keeper", "funnels", "diagnostic", "based_on_through",
                      "complete_weeks", "weekly_allowance_min"}

    # The current (unfinished) week is never summarised: only complete weeks.
    assert p["complete_weeks"] == 2 and len(p["weeks"]) == 2
    assert [w["days"] for w in p["weeks"]] == [
        {"from": "2026-09-13", "to": "2026-09-19"},
        {"from": "2026-09-20", "to": "2026-09-26"},
    ]
    assert p["based_on_through"] == "2026-09-26"
    assert all(w["week_start"] != p["generated_for_week_start"] for w in p["weeks"])

    first, second = p["weeks"]
    assert first["change_from_previous_week"] is None          # nothing before it in the window
    assert first["wasteful_min"] == 260.0                      # 200 iPhone + 60 web
    assert first["worthwhile_min"] == 40.0                     # the labelled channel
    assert first["tool_min"] == 7.0                            # the tool-labelled Mac app
    assert second["wasteful_min"] == 140.0
    change = second["change_from_previous_week"]
    assert change["wasteful_min"] == -120.0 and change["wasteful_pct"] == pytest.approx(-46.2)

    # Per group, in REPORT_GROUPS order, with the gated split carried through.
    assert [g["group"] for g in first["by_group"]] == list(T.REPORT_GROUPS)
    iphone = next(g for g in first["by_group"] if g["group"] == T.GROUP_IPHONE)
    assert iphone["wasteful_min"] == 200.0 and iphone["wasteful_ungated_min"] == 200.0
    assert first["reflex_wasteful_min"] == 260.0
    assert first["reflex_share_of_wasteful"] == 1.0            # nothing passed through the queue

    assert first["allowance_min"] == 360 and first["overage_min"] == 0
    assert {"queue", "vault"} == set(p["funnels"])
    assert all(set(k) == {"group", "minutes", "keepers", "minutes_per_keeper"}
               for k in p["cost_per_keeper"])


def test_payload_is_deterministic_and_json_serialisable(two_weeks, clk, settings):
    a = insights.build_payload(two_weeks, clk, settings)
    b = insights.build_payload(two_weeks, clk, settings)
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_payload_names_the_weeks_whose_evidence_never_arrived(conn, clk, settings):
    """A gap is stated per week, by group, never absorbed into a zero."""
    _week(conn, clk, settings, day="2026-09-16",
          minutes={"iphone": 10.0, "web": 10.0, "youtube": 10.0, "desktop_apps": 1.0})
    p = insights.build_payload(conn, clk, settings)
    gaps = " ".join(g for w in p["weeks"] for g in w["coverage_gaps"])
    # Cold Turkey never arrived for either week; the week with no evidence at
    # all names every weekly import.
    assert "Desktop web (Cold Turkey) was not received" in gaps
    assert "2026-09-20 to 2026-09-26" in gaps
    # Qbserve is unconfigured here, so desktop apps are unmeasured by design,
    # not an import the user owes.
    assert "Qbserve" not in gaps


def test_an_empty_database_builds_a_payload_of_zeros_and_gaps(conn, clk, settings):
    """`go_live` is set, so the weeks exist and are honestly empty — every
    weekly import is named as not received rather than the week reading as a
    quiet zero."""
    p = insights.build_payload(conn, clk, settings)
    json.dumps(p)
    assert p["complete_weeks"] == 2 and p["based_on_through"] == "2026-09-26"
    assert all(w["wasteful_min"] == 0.0 for w in p["weeks"])
    assert all(len(w["coverage_gaps"]) == 3 for w in p["weeks"])
    assert "No wasteful content was measured" in insights.fallback_summary(p)


def test_before_go_live_there_is_no_complete_week_at_all(conn, clk, tmp_path):
    """Nothing to compare, and the fallback says exactly that."""
    future = make_settings(tmp_path, db_path=tmp_path / "dr-brainrot.db", go_live="2026-12-06")
    p = insights.build_payload(conn, clk, future)
    assert p["weeks"] == [] and p["complete_weeks"] == 0 and p["based_on_through"] is None
    assert "No week of figures is complete yet" in insights.fallback_summary(p)


# --------------------------------------------------------------------------
# Privacy — the constraint the whole feature stands on
# --------------------------------------------------------------------------

def test_the_payload_carries_no_source_name_title_or_path(two_weeks, conn, clk, settings):
    """Aggregates only leave this Mac (DR5.8). Group names, minutes, counts — nothing else."""
    # A vault capture with a note title and a path, and a queue item with a
    # podcast show and an episode title: every shape of personal name there is.
    db.upsert_episode(conn, "7aBcDeFgHiJkLmNoPqRsTu", SECRET_NAMES["video_title"],
                      SECRET_NAMES["show"], None, None, 3_600_000)
    db.capture(conn, clk, settings, spotify_id="7aBcDeFgHiJkLmNoPqRsTu", why_note="because")
    rec = evidence.begin_import(conn, clk, kind="vault_scan", parser_version="test-1")
    conn.execute(
        "INSERT INTO vault_captures (capture_id, captured_at, capture_type, keeper_group, url, "
        "title, initial_rel_path, body_sha256, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("cap-blorp-1", "2026-09-16T10:00:00+00:00", "url", T.GROUP_WEB,
         f"https://{SECRET_NAMES['domain']}/thing", "Note: Flimbertrout Considered",
         "01-Inbox/Flimbertrout Considered.md", "abc123", "2026-09-16T10:00:00+00:00"))
    conn.execute(
        "INSERT INTO vault_observations (import_id, capture_id, scanned_at, outcome, rel_path, "
        "edited) VALUES (?, ?, ?, ?, ?, ?)",
        (rec.id, "cap-blorp-1", "2026-09-17T10:00:00+00:00", "kept",
         "02-Notes/Flimbertrout Considered.md", 1))
    evidence.finish_import(conn, clk, settings, rec, period_start="2026-09-16",
                           period_end="2026-09-17")
    conn.commit()

    payload = insights.build_payload(conn, clk, settings)
    sent = insights.user_prompt(payload)

    for what, name in SECRET_NAMES.items():
        if what == "x_key":            # "x" is a report group name, not a source name
            continue
        assert name not in sent, f"{what} leaked into the payload: {name!r}"
    for leaked in ("Flimbertrout", "Note:", "https://", "/01-Inbox", "cap-blorp-1",
                   "7aBcDeFgHiJkLmNoPqRsTu", ".md", str(settings.db_path)):
        assert leaked not in sent, f"{leaked!r} leaked into the payload"

    # What it *may* say: group names and the standing vocabulary.
    assert '"group": "iphone_apps"' in sent and '"group": "desktop_web"' in sent
    # Group keys are the fixed REPORT_GROUPS constants, never a source key that
    # happened to end up in `by_group`.
    groups = {g["group"] for w in payload["weeks"] for g in w["by_group"]}
    assert groups == set(T.REPORT_GROUPS)
    assert {k["group"] for k in payload["cost_per_keeper"]} <= set(T.REPORT_GROUPS) | {
        T.GROUP_WEB, T.GROUP_OTHER}


def _strings(value, path="$"):
    """Every string leaf in the payload, with the path it sits at."""
    if isinstance(value, dict):
        for k, v in value.items():
            assert isinstance(k, str)
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


def test_every_free_text_string_in_the_payload_is_built_from_constants(
        two_weeks, conn, clk, settings):
    """The coverage sentences are the only prose the payload carries, and they
    are assembled from `report.GROUP_TEXT` and `report.EXPECTED_WEEKLY` — never
    from a source, a title or a stored import warning."""
    import re

    import report

    import progress

    payload = insights.build_payload(conn, clk, settings)
    # DR5.17's comparison adds exactly two vocabularies, both fixed in code:
    # the direction words, and the channel names — never a source.
    allowed_plain = (set(T.REPORT_GROUPS) | {settings.tz, insights.PAYLOAD_SCHEMA}
                     | {None, "from", "to"} | {"down", "up", "level"}
                     | set(progress.CHANNEL_NAMES.values()))
    gap_names = {name for _k, _g, _p, _d, name in report.EXPECTED_WEEKLY}

    for path, text in _strings(payload):
        if text in allowed_plain:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):                       # a local date
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", text):  # an instant
            continue
        if path.endswith("]") and "coverage_gaps" in path:
            group, name = re.fullmatch(
                r"(.+?) \((.+?)\) was not received for the week "
                r"\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}\.", text).groups()
            assert group in report.GROUP_TEXT.values() and name in gap_names
            continue
        raise AssertionError(f"unexpected free text at {path}: {text!r}")


def test_a_stored_import_warning_never_reaches_the_payload(two_weeks, conn, clk, settings):
    """Import warnings quote real rows ("Facebook | 2h 27m"); none is copied."""
    conn.execute("UPDATE imports SET warnings = ?, error = ? WHERE id = (SELECT MAX(id) FROM imports)",
                 (json.dumps([f"could not read '{SECRET_NAMES['app']}' on row 3"]),
                  f"parse failed near {SECRET_NAMES['domain']}"))
    conn.commit()
    sent = insights.user_prompt(insights.build_payload(conn, clk, settings))
    assert SECRET_NAMES["app"] not in sent and SECRET_NAMES["domain"] not in sent
    assert "could not read" not in sent and "parse failed" not in sent


def test_the_stored_payload_is_the_one_that_was_sent(two_weeks, conn, clk, settings):
    fake = FakeClient("Wasteful content fell.")
    insights.write_summary(conn, clk, settings, client=fake)
    stored = json.loads(conn.execute("SELECT payload FROM insights").fetchone()["payload"])
    assert stored == insights.build_payload(conn, clk, settings)
    assert json.dumps(stored, indent=2, sort_keys=True) in fake.calls[0]["messages"][0]["content"]


# --------------------------------------------------------------------------
# fallback_summary
# --------------------------------------------------------------------------

def _payload(weeks):
    return {"schema": insights.PAYLOAD_SCHEMA, "weeks": weeks, "complete_weeks": len(weeks)}


def _w(start, to, wasteful, *, worthwhile=0.0, reflex_share=None, overage=0, allowance=360, gaps=()):
    return {"week_start": start, "days": {"from": start, "to": to}, "wasteful_min": wasteful,
            "worthwhile_min": worthwhile, "reflex_share_of_wasteful": reflex_share,
            "overage_min": overage, "allowance_min": allowance, "coverage_gaps": list(gaps)}


def test_fallback_says_when_wasteful_content_fell():
    text = insights.fallback_summary(_payload([
        _w("2026-09-13", "2026-09-19", 307.0),
        _w("2026-09-20", "2026-09-26", 190.0, reflex_share=0.9),
    ]))
    assert "Wasteful content fell 38% week on week, from 307 min to 190 min." in text
    assert "90% of wasteful content bypassed the queue." in text
    assert "2 complete weeks" in text


def test_fallback_says_when_it_rose_and_when_the_allowance_was_passed():
    text = insights.fallback_summary(_payload([
        _w("2026-09-13", "2026-09-19", 200.0),
        _w("2026-09-20", "2026-09-26", 400.0, overage=40, allowance=360),
    ]))
    assert "rose 100% week on week, from 200 min to 400 min" in text
    assert "went 40 min past the 360 min allowance" in text


def test_fallback_says_flat_rather_than_inventing_a_direction():
    text = insights.fallback_summary(_payload([
        _w("2026-09-13", "2026-09-19", 300.0),
        _w("2026-09-20", "2026-09-26", 306.0),
    ]))
    assert "held steady week on week, 300 min to 306 min." in text
    assert "fell" not in text and "rose" not in text


def test_fallback_is_honest_about_too_little_data():
    one = insights.fallback_summary(_payload([_w("2026-09-13", "2026-09-19", 100.0)]))
    assert "no week-on-week trend yet" in one and "One complete week so far" in one
    assert "fell" not in one and "rose" not in one

    none = insights.fallback_summary(_payload([]))
    assert "No week of figures is complete yet" in none and "nothing to compare" in none


def test_fallback_says_a_week_is_incomplete_rather_than_reading_a_gap_as_zero():
    text = insights.fallback_summary(_payload([
        _w("2026-09-13", "2026-09-19", 300.0),
        _w("2026-09-20", "2026-09-26", 20.0, gaps=["Desktop web (Cold Turkey) was not received."]),
    ]))
    assert "evidence is incomplete" in text and "a floor, not a total" in text


def test_fallback_on_real_figures_invents_nothing(two_weeks, clk, settings):
    payload = insights.build_payload(two_weeks, clk, settings)
    text = insights.fallback_summary(payload)
    assert "260 min" in text and "140 min" in text and "fell 46%" in text


# --------------------------------------------------------------------------
# write_summary, with a fake client
# --------------------------------------------------------------------------

class FakeBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class FakeUsage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class FakeResponse:
    def __init__(self, text, *, stop_reason="end_turn", usage=(1234, 88)):
        self.content = [FakeBlock(text)] if text is not None else []
        self.stop_reason = stop_reason
        self.usage = FakeUsage(*usage)


class FakeMessages:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if self._owner.raises is not None:
            raise self._owner.raises
        return self._owner.response


class FakeClient:
    """Stands in for `anthropic.Anthropic`. Records the request; never goes out."""

    def __init__(self, text="A summary.", *, stop_reason="end_turn", raises=None, usage=(1234, 88)):
        self.calls: list[dict] = []
        self.raises = raises
        self.response = FakeResponse(text, stop_reason=stop_reason, usage=usage)
        self.messages = FakeMessages(self)


def test_write_summary_stores_the_model_answer_and_the_token_counts(two_weeks, conn, clk, settings):
    fake = FakeClient("Wasteful content is falling; reflex consumption is not.")
    result = insights.write_summary(conn, clk, settings, client=fake)

    assert result.fallback_reason is None            # `model is None` IS the fallback
    assert result.summary == "Wasteful content is falling; reflex consumption is not."
    assert result.model == insights.MODEL == "claude-opus-5"
    assert result.week_start == clock.iso(clock.week_start_for(clk.now(), settings.tz))
    assert result.based_on_through == "2026-09-26"

    row = conn.execute("SELECT * FROM insights").fetchone()
    assert (row["status"], row["error"]) == ("ok", None)
    assert (row["input_tokens"], row["output_tokens"]) == (1234, 88)
    assert row["model"] == "claude-opus-5"


def test_created_at_is_when_the_figures_were_read_so_data_landing_mid_call_makes_it_stale(
        two_weeks, conn, clk, settings):
    """The request takes seconds (5 Oct: 09:30:06 to 09:30:11). A Screen Time
    import that starts during it is not in the summary, and used to be stamped
    before the summary's created_at, so the chip never said so (DR5.26)."""
    from dashboard import app as dashboard_app

    start = clk.now()

    class Racing(FakeClient):
        def __init__(self):
            super().__init__("A summary.")
            self.messages = self

        def create(self, **kwargs):
            clk.set(clock.iso(start + timedelta(seconds=3)))          # the import begins mid-call
            measure(conn, clk, settings, kind=T.KIND_IPHONE_APP, key="Late Arrival App", day="2026-09-20",
                    period_end="2026-09-26", minutes=500.0)
            clk.set(clock.iso(start + timedelta(seconds=6)))          # then the model answers
            return self.response

    insight = insights.write_summary(conn, clk, settings, client=Racing())
    assert insight.created_at == clock.iso(start)
    stored = json.loads(conn.execute("SELECT payload FROM insights").fetchone()["payload"])
    assert stored["weeks"][-1] != insights.build_payload(conn, clk, settings)["weeks"][-1]   # not in it
    assert dashboard_app.insight_staleness(conn, insight, "2026-09-20", settings.tz) == \
        dashboard_app.STALE_NEW_DATA


def test_the_prompt_forbids_inventing_numbers_and_judging(two_weeks, conn, clk, settings):
    fake = FakeClient()
    insights.write_summary(conn, clk, settings, client=fake)
    (call,) = fake.calls

    assert call["model"] == "claude-opus-5" and call["max_tokens"] == insights.MAX_TOKENS <= 400
    system = call["system"]
    # No invention.
    assert "Every number you use must come from the JSON payload" in system
    assert "do not state any number that is not in the payload" in system
    # No judgement, no relabelling, no advice — advice is computed locally, with
    # names, and shown beside the paragraph (DR5.17).
    assert "the system counts and the person labels" in system
    assert "Do not say whether any of this content was worth the time" in system
    assert "do not suggest that anything should be labelled" in system
    assert "Give no advice and no recommendations" in system
    # An improvement may be stated — as a fact, not a compliment.
    assert "When the like-for-like comparison shows a fall, say so plainly" in system
    assert "no cheerleading" in system and "no moralising" in system
    # And the raw weekly totals are never passed off as a change in behaviour.
    assert "Never present the difference between the weeks' raw totals" in system
    # ...nor the per-group figures, which carry the same distortion.
    assert "do not set one week's group figures beside the other's" in system
    # Incomplete weeks are stated.
    assert "coverage_gaps" in system and "rather than treating the gap as zero" in system
    # Shape.
    assert "3 to 5 short sentences" in system and "No headings" in system

    (message,) = call["messages"]
    assert message["role"] == "user"
    assert message["content"].startswith(insights.USER_PROMPT_HEADER)


def test_a_summary_is_written_once_a_week_unless_forced(two_weeks, conn, clk, settings):
    first = insights.write_summary(conn, clk, settings, client=FakeClient("First."))

    again = FakeClient("Second.")
    skipped = insights.write_summary(conn, clk, settings, client=again)
    assert again.calls == [] and skipped.summary == "First." == first.summary
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 1

    forced = FakeClient("Second.")
    result = insights.write_summary(conn, clk, settings, client=forced, force=True)
    assert len(forced.calls) == 1 and result.summary == "Second."
    assert conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 2
    assert insights.latest(conn, clk, settings).summary == "Second."

    # A new week asks again without being forced.
    clk.set("2026-10-07T10:00:00+00:00")
    next_week = FakeClient("Third.")
    assert insights.write_summary(conn, clk, settings, client=next_week).summary == "Third."
    assert len(next_week.calls) == 1


def test_a_fallback_week_is_retried_by_the_next_run(two_weeks, conn, clk, settings):
    """A passing failure must not freeze the whole week on dr-brainrot's own sentences."""
    failed = insights.write_summary(
        conn, clk, settings, client=FakeClient(raises=RuntimeError("connection reset by peer")))
    assert failed.model is None

    # Same week, no force: the failure is retried rather than returned again.
    retry = FakeClient("Written on the second attempt.")
    result = insights.write_summary(conn, clk, settings, client=retry)
    assert len(retry.calls) == 1
    assert result.model == insights.MODEL
    assert result.summary == "Written on the second attempt."
    assert insights.latest(conn, clk, settings).summary == "Written on the second attempt."

    # And once it has succeeded the week is closed again.
    after = FakeClient("Third.")
    assert insights.write_summary(conn, clk, settings, client=after).summary == (
        "Written on the second attempt.")
    assert after.calls == []

def test_an_api_failure_is_stored_and_the_fallback_is_returned(two_weeks, conn, clk, settings):
    fake = FakeClient(raises=RuntimeError("connection reset by peer"))
    result = insights.write_summary(conn, clk, settings, client=fake)

    assert result.model is None                      # the one fallback signal
    assert result.fallback_reason.startswith("API call failed: ")
    assert "connection reset by peer" in result.fallback_reason    # verbatim, not a code
    assert result.summary == insights.fallback_summary(
        insights.build_payload(conn, clk, settings))
    assert "fell 46%" in result.summary

    row = conn.execute("SELECT * FROM insights").fetchone()
    assert row["status"] == "failed"                 # the column stays, for querying
    assert row["model"] is None
    assert "connection reset by peer" in row["error"] and "RuntimeError" in row["error"]
    assert row["input_tokens"] is None and row["output_tokens"] is None
    # The payload is still stored, so what the summary is based on is checkable.
    assert json.loads(row["payload"])["complete_weeks"] == 2


@pytest.mark.parametrize("kwargs, why", [
    ({"stop_reason": "refusal"}, "declined"),
    ({"stop_reason": "max_tokens"}, "cut off"),
    ({"text": "   "}, "no text"),
    ({"text": None}, "no text"),
])
def test_an_unusable_answer_is_a_failure_not_a_summary(two_weeks, conn, clk, settings, kwargs, why):
    result = insights.write_summary(conn, clk, settings, client=FakeClient(**kwargs))
    assert result.model is None and why in result.fallback_reason
    assert result.fallback_reason.startswith("API call failed: ")
    assert why in conn.execute("SELECT error FROM insights").fetchone()["error"]


def test_without_an_api_key_nothing_is_called_and_no_ok_row_is_written(two_weeks, conn, clk, settings):
    assert settings.anthropic_api_key == ""
    result = insights.write_summary(conn, clk, settings)     # no client, no key

    assert result.model is None
    assert result.fallback_reason == insights.REASON_NO_KEY
    assert "No ANTHROPIC_API_KEY is set" in result.fallback_reason
    assert result.summary == insights.fallback_summary(insights.build_payload(conn, clk, settings))
    assert conn.execute("SELECT COUNT(*) FROM insights WHERE status = 'ok'").fetchone()[0] == 0
    row = conn.execute("SELECT * FROM insights").fetchone()
    assert row["model"] is None and row["error"] == insights.REASON_NO_KEY


def test_the_api_key_never_reaches_the_row_or_the_error(two_weeks, conn, clk, tmp_path, caplog):
    keyed = make_settings(tmp_path, db_path=tmp_path / "dr-brainrot.db", weekly_allowance_min=360,
                          go_live="2026-09-13", anthropic_api_key="sk-ant-NOTAREALKEY-0123456789")
    fake = FakeClient(raises=RuntimeError("bad x-api-key: sk-ant-NOTAREALKEY-0123456789"))
    with caplog.at_level("WARNING"):
        insights.write_summary(conn, clk, keyed, client=fake)

    row = conn.execute("SELECT * FROM insights").fetchone()
    assert "sk-ant-NOTAREALKEY-0123456789" not in (row["error"] or "")
    assert "***" in row["error"]
    for field in ("payload", "summary", "model"):
        assert "sk-ant-" not in (row[field] or "")
    assert "sk-ant-NOTAREALKEY-0123456789" not in caplog.text


def test_write_summary_changes_no_figure_and_no_label(two_weeks, conn, clk, settings):
    def snapshot():
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
                for t in ("sources", "measurements", "weeks", "item_labels", "imports", "queue")}

    before = snapshot()
    insights.write_summary(conn, clk, settings, client=FakeClient("A summary."))
    assert snapshot() == before


# --------------------------------------------------------------------------
# latest
# --------------------------------------------------------------------------

def test_latest_is_none_until_something_is_written(conn, clk, settings):
    assert insights.latest(conn, clk, settings) is None


def test_latest_returns_the_newest_row_and_marks_a_failed_one_as_fallback(
        two_weeks, conn, clk, settings):
    insights.write_summary(conn, clk, settings, client=FakeClient("Model wrote this."))
    good = insights.latest(conn, clk, settings)
    assert good.summary == "Model wrote this."
    assert good.model == "claude-opus-5" and good.fallback_reason is None

    insights.write_summary(conn, clk, settings, force=True,
                           client=FakeClient(raises=RuntimeError("boom")))
    bad = insights.latest(conn, clk, settings)
    assert bad.model is None and "boom" in bad.fallback_reason
    assert "fell 46%" in bad.summary and bad.based_on_through == "2026-09-26"


# --------------------------------------------------------------------------
# The daily job
# --------------------------------------------------------------------------

def test_the_daily_job_writes_a_summary_and_a_failure_never_fails_the_run(
        two_weeks, conn, clk, settings, monkeypatch):
    """`insights` is a step of run_daily, not of run_sync, and never fails it."""
    monkeypatch.setattr(jobs, "run_sync", lambda *a, **k: {})
    monkeypatch.setattr(jobs.checklist, "send_due", lambda *a, **k: [])
    monkeypatch.setattr(jobs, "send_end_notice", lambda *a, **k: "skipped")

    boom = RuntimeError("the provider is down")
    monkeypatch.setattr(insights, "write_summary",
                        lambda *a, **k: (_ for _ in ()).throw(boom))
    assert jobs.run_daily(conn, clk, settings, bot=None) == []      # not a failed step

    monkeypatch.undo()
    monkeypatch.setattr(jobs, "run_sync", lambda *a, **k: {})
    monkeypatch.setattr(jobs.checklist, "send_due", lambda *a, **k: [])
    monkeypatch.setattr(jobs, "send_end_notice", lambda *a, **k: "skipped")
    assert jobs.run_daily(conn, clk, settings, bot=None) == []
    row = conn.execute("SELECT * FROM insights").fetchone()
    assert row is not None and row["status"] == "failed"           # no key in test settings


def test_run_sync_never_writes_a_summary(two_weeks, conn, clk, settings, monkeypatch):
    called = []
    monkeypatch.setattr(insights, "write_summary", lambda *a, **k: called.append(1))
    jobs.run_sync(conn, clk, settings)
    assert called == []
    assert "insights" not in jobs.SYNC_STEPS


# --------------------------------------------------------------------------
# Migration 0006
# --------------------------------------------------------------------------

def test_schema_version_is_eight_and_the_insights_table_exists(conn):
    assert db.SCHEMA_VERSION == 8
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "8"
    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(insights)")}
    assert set(cols) == {"id", "created_at", "model", "week_start", "summary", "payload",
                         "status", "error", "input_tokens", "output_tokens"}
    assert cols["input_tokens"] == "INTEGER" and cols["summary"] == "TEXT"


def test_status_is_constrained_to_ok_and_failed(conn):
    conn.execute("INSERT INTO insights (created_at, week_start, status) VALUES ('t', 'w', 'ok')")
    conn.execute("INSERT INTO insights (created_at, week_start, status) VALUES ('t', 'w', 'failed')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO insights (created_at, week_start, status) "
                     "VALUES ('t', 'w', 'pending')")


# --------------------------------------------------------------------------
# check_env — shape only, never a live call
# --------------------------------------------------------------------------

def _check_anthropic(env, capsys):
    import check_env

    check_env.failures.clear()
    check_env.check_anthropic(env)
    out = capsys.readouterr().out
    failed = list(check_env.failures)
    check_env.failures.clear()
    return out, failed


def test_check_env_anthropic_step_is_shape_only_and_optional(capsys):
    out, failed = _check_anthropic({}, capsys)
    assert not failed and "[10/" in out
    assert "optional" in out and "computed sentences" in out

    out, failed = _check_anthropic({"ANTHROPIC_API_KEY": "not-a-key"}, capsys)
    assert failed and "does not match expected shape" in out

    key = "sk-ant-api03-" + "x" * 40
    out, failed = _check_anthropic({"ANTHROPIC_API_KEY": key}, capsys)
    assert not failed and "OK" in out
    assert key not in out and out.count("…") == 1            # masked, never printed


# --------------------------------------------------------------------------
# The three fields the dashboard branches on
# --------------------------------------------------------------------------

def test_model_is_none_exactly_when_the_summary_is_dr_brainrots_own(two_weeks, conn, clk, settings):
    """One fact, one field: there is no second flag that could disagree."""
    written = insights.write_summary(conn, clk, settings, client=FakeClient("Model text."))
    assert written.model == insights.MODEL and written.fallback_reason is None

    fell_back = insights.write_summary(conn, clk, settings, force=True,
                                       client=FakeClient(raises=RuntimeError("nope")))
    assert fell_back.model is None and fell_back.fallback_reason is not None
    assert not hasattr(fell_back, "is_fallback") and not hasattr(fell_back, "status")


def test_the_fallback_reason_is_the_real_reason_not_a_code(two_weeks, conn, clk, tmp_path):
    no_key = insights.write_summary(conn, clk, make_settings(
        tmp_path, db_path=tmp_path / "dr-brainrot.db", go_live="2026-09-13"))
    assert no_key.fallback_reason == insights.REASON_NO_KEY
    assert "ANTHROPIC_API_KEY" in no_key.fallback_reason

    failed = insights.write_summary(conn, clk, make_settings(
        tmp_path, db_path=tmp_path / "dr-brainrot.db", go_live="2026-09-13"), force=True,
        client=FakeClient(raises=RuntimeError("502 Bad Gateway from upstream")))
    assert failed.fallback_reason == "API call failed: RuntimeError: 502 Bad Gateway from upstream"

    # No code ever reaches the reason the dashboard prints.
    for reason in (no_key.fallback_reason, failed.fallback_reason):
        assert reason not in ("ok", "failed") and len(reason.split()) > 3


def test_too_little_data_is_a_reason_and_costs_no_api_call(conn, clk, tmp_path):
    """Nothing complete to summarise: nothing is sent, nothing is spent."""
    future = make_settings(tmp_path, db_path=tmp_path / "dr-brainrot.db", go_live="2026-12-06")
    fake = FakeClient("should never be asked for")
    result = insights.write_summary(conn, clk, future, client=fake)
    assert fake.calls == []
    assert result.model is None and result.fallback_reason == insights.REASON_TOO_LITTLE
    assert "Too little data" in result.fallback_reason
    assert "No week of figures is complete yet" in result.summary
    assert result.based_on_through is None


def test_based_on_through_is_a_local_date_comparable_to_a_measurement_week(
        two_weeks, conn, clk, settings):
    """A local YYYY-MM-DD in settings.tz, not an instant and not from week_start."""
    import re

    import report

    result = insights.write_summary(conn, clk, settings, client=FakeClient("x"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", result.based_on_through)
    assert "T" not in result.based_on_through and "+00:00" not in result.based_on_through

    # It IS the Saturday of the last complete measurement week...
    current = clock.week_start_for(clk.now(), settings.tz)
    last_complete = T.measurement_week_for_allowance_week(
        clock.iso(clock.week_start_for(current - timedelta(seconds=1), settings.tz)), settings.tz)
    assert result.based_on_through == last_complete.end == "2026-09-26"
    # ...and compares directly with the window start and with today.
    assert result.based_on_through >= report.window_start(conn, clk, settings)
    assert result.based_on_through < T.iso_date(T.local_date(clk.now(), settings.tz))
    # ...and is not the week it was generated in.
    assert not result.week_start.startswith(result.based_on_through)


def test_a_known_api_failure_reads_as_a_sentence_not_a_400_body():
    """The panel prints this to a person; the raw body goes to the log instead."""
    raw = ("BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': "
           "'invalid_request_error', 'message': 'Your credit balance is too low to access the "
           "Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, "
           "'request_id': 'req_011CfJJqRgp7xSYfqp6xy9rX'}")
    said = insights.explain_failure(raw)
    assert "credit" in said and "request_id" not in said and "{" not in said
    assert said.endswith("from its own figures.")


def test_an_unknown_api_failure_keeps_the_real_error_but_bounded():
    assert insights.explain_failure("Boom: something new") == "API call failed: Boom: something new"
    long = "X" * 400
    assert len(insights.explain_failure(long)) < 220 and insights.explain_failure(long).endswith("…")
