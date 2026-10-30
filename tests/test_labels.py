"""labels.py: resolution precedence, batch selection, listing (CONTRACTS Part II §5)."""
from __future__ import annotations

import itertools

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
    return clock.FrozenClock("2026-09-16T10:00:00+00:00")


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    db.init_db(c)
    db.migrate(c)
    yield c
    c.close()


REF = "spotify:4rOoJ6Egrf8K2IrywzwOMk"


# --- resolution: every combination ------------------------------------------

OVERRIDES = [None, T.WASTEFUL, T.WORTHWHILE]
SOURCE_STATES = ["no_source", "unlabelled", T.WASTEFUL, T.WORTHWHILE]
REFS = [REF, None]


@pytest.mark.parametrize("override,source_state,item_ref", list(itertools.product(OVERRIDES, SOURCE_STATES, REFS)))
def test_resolution_precedence_exhaustive(conn, clk, override, source_state, item_ref):
    source_id = None
    if source_state != "no_source":
        source_id = labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Synthetic Show").id
        if source_state != "unlabelled":
            labels.set_source_label(conn, clk, source_id, source_state)
    if override is not None:
        labels.set_item_override(conn, clk, REF, override)

    got = labels.resolve(conn, source_id=source_id, item_ref=item_ref)

    if override is not None and item_ref is not None:
        expected = labels.ResolvedLabel(override, T.BASIS_ITEM_OVERRIDE)
    elif source_state in (T.WASTEFUL, T.WORTHWHILE):
        expected = labels.ResolvedLabel(source_state, T.BASIS_SOURCE_LABEL)
    else:
        expected = labels.ResolvedLabel(T.WASTEFUL, T.BASIS_DEFAULT)
    assert got == expected


def test_override_for_another_item_does_not_leak(conn, clk):
    src = labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Synthetic Show")
    labels.set_item_override(conn, clk, "spotify:other", T.WORTHWHILE)
    assert labels.resolve(conn, source_id=src.id, item_ref=REF).basis == T.BASIS_DEFAULT


def test_last_override_wins(conn, clk):
    labels.set_item_override(conn, clk, REF, T.WORTHWHILE)
    labels.set_item_override(conn, clk, REF, T.WASTEFUL)
    assert labels.item_override(conn, REF) == T.WASTEFUL


def test_unknown_source_id_raises_rather_than_defaulting(conn):
    with pytest.raises(labels.LabelError):
        labels.resolve(conn, source_id=999_999)
    with pytest.raises(labels.LabelError):
        labels.set_source_label(conn, clock.FrozenClock("2026-09-16T10:00:00+00:00"), 999_999, T.WASTEFUL)


def test_override_short_circuits_before_the_source_lookup(conn, clk):
    # Documented Phase 1 behaviour: an explicit item override is final.
    labels.set_item_override(conn, clk, REF, T.WORTHWHILE)
    assert labels.resolve(conn, source_id=999_999, item_ref=REF).label == T.WORTHWHILE


@pytest.mark.parametrize("surface,raw", [
    (T.KIND_IPHONE_APP, "X"), (T.KIND_IPHONE_APP, "twitter"), (T.KIND_WEB_DOMAIN, "https://mobile.twitter.com/x"),
    (T.KIND_WEB_DOMAIN, "www.x.com"), (T.KIND_X, "anything"),
])
def test_x_folds_to_the_seeded_wasteful_source(conn, clk, surface, raw):
    src = labels.ensure_source(conn, clk, surface, raw)
    assert (src.kind, src.source_key, src.label) == (T.KIND_X, "x", T.WASTEFUL)
    assert labels.resolve(conn, source_id=src.id) == labels.ResolvedLabel(T.WASTEFUL, T.BASIS_SOURCE_LABEL)


def _corrupt(conn, sql, params):
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(sql, params)
    conn.execute("PRAGMA ignore_check_constraints = OFF")


def test_invalid_source_label_in_db_raises(conn, clk):
    src = labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Synthetic Show")
    _corrupt(conn, "UPDATE sources SET label = 'neutral' WHERE id = ?", (src.id,))
    with pytest.raises(labels.LabelError):
        labels.resolve(conn, source_id=src.id)


def test_invalid_override_in_db_raises(conn, clk):
    _corrupt(conn, "INSERT INTO item_labels VALUES (?, 'meh', '2026-09-16T10:00:00+00:00', 'capture')", (REF,))
    with pytest.raises(labels.LabelError):
        labels.resolve(conn, source_id=None, item_ref=REF)


@pytest.mark.parametrize("bad", ["neutral", "Wasteful", "", None])
def test_writes_reject_invalid_labels(conn, clk, bad):
    src = labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Synthetic Show")
    with pytest.raises(labels.LabelError):
        labels.set_source_label(conn, clk, src.id, bad)
    with pytest.raises(labels.LabelError):
        labels.set_item_override(conn, clk, REF, bad)


def test_set_source_label_commits_and_stamps(conn, clk):
    src = labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Synthetic Show")
    conn.commit()
    clk.advance(hours=1)
    out = labels.set_source_label(conn, clk, src.id, T.WORTHWHILE)
    assert (out.label, out.labelled_at) == (T.WORTHWHILE, "2026-09-16T11:00:00+00:00")
    assert not conn.in_transaction


# --- sources_needing_label --------------------------------------------------

def _measure(conn, clk, settings, source_id, provenance=T.PROV_EXPORT, day="2026-09-13"):
    rec = evidence.begin_import(conn, clk, kind="coldturkey", parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=source_id, device="desktop", provenance=provenance,
                                period_start=day, period_end=day, minutes=1.0)
    evidence.finish_import(conn, clk, settings, rec, period_start=day, period_end=day)


def _capture(conn, cid, source_id):
    conn.execute(
        "INSERT INTO vault_captures VALUES (?, '2026-09-15T08:00:00+00:00', 'article', 'web', ?, NULL, 't', "
        "'01-Inbox/x.md', ?, '2026-09-15T08:00:00+00:00')", (cid, source_id, "0" * 64))
    conn.commit()


def _listen(conn, spotify_id, show, platform="spotify", source_key=None):
    conn.execute("INSERT INTO episodes (spotify_id, title, show, description, release_date, duration_ms, platform, "
                 "source_key) VALUES (?, 't', ?, '', '2026-09-01', 60000, ?, ?)",
                 (spotify_id, show, platform, source_key))
    conn.execute("INSERT INTO listening (spotify_id, polled_at, position_ms, delta_ms, fully_played) "
                 "VALUES (?, '2026-09-15T08:00:00+00:00', 0, 0, 0)", (spotify_id,))
    conn.commit()


def _src(conn, clk, kind, key, at):
    clk.set(at)
    s = labels.ensure_source(conn, clk, kind, key)
    conn.commit()
    return s


def test_needing_label_requires_evidence_of_each_kind(conn, clk, settings):
    measured = _src(conn, clk, T.KIND_WEB_DOMAIN, "measured.example", "2026-09-10T00:00:00+00:00")
    captured = _src(conn, clk, T.KIND_WEB_DOMAIN, "captured.example", "2026-09-11T00:00:00+00:00")
    podcast = _src(conn, clk, T.KIND_PODCAST_SHOW, "Synthetic  SHOW", "2026-09-12T00:00:00+00:00")
    channel = _src(conn, clk, T.KIND_YOUTUBE_CHANNEL, "UCsyntheticChannel0000001", "2026-09-12T01:00:00+00:00")
    _src(conn, clk, T.KIND_WEB_DOMAIN, "never-observed.example", "2026-09-09T00:00:00+00:00")
    _src(conn, clk, T.KIND_PODCAST_SHOW, "Queued Not Listened", "2026-09-09T00:00:00+00:00")
    conn.execute("INSERT INTO episodes (spotify_id, title, show, description, release_date, duration_ms) "
                 "VALUES ('ep-no-listen', 't', 'Queued Not Listened', '', '2026-09-01', 1)")
    clk.set("2026-09-16T10:00:00+00:00")
    _measure(conn, clk, settings, measured.id)
    _capture(conn, "a" * 32, captured.id)
    _listen(conn, "ep1", "synthetic show")
    _listen(conn, "youtube:vid1", "Channel Title", platform="youtube", source_key="UCsyntheticChannel0000001")

    got = labels.sources_needing_label(conn, clk, settings)
    assert [s.id for s in got] == [measured.id, captured.id, podcast.id, channel.id]


def test_needing_label_excludes_labelled_and_covered(conn, clk, settings):
    covered = _src(conn, clk, T.KIND_IPHONE_APP, "YouTube", "2026-09-10T00:00:00+00:00")
    labelled = _src(conn, clk, T.KIND_IPHONE_APP, "Maps", "2026-09-10T00:00:00+00:00")
    x = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "X")
    open_ = _src(conn, clk, T.KIND_IPHONE_APP, "Notes", "2026-09-11T00:00:00+00:00")
    clk.set("2026-09-16T10:00:00+00:00")
    for s in (covered, labelled, x, open_):
        _measure(conn, clk, settings, s.id)
    labels.set_source_label(conn, clk, labelled.id, T.WORTHWHILE)
    assert [s.id for s in labels.sources_needing_label(conn, clk, settings)] == [open_.id]


def test_needing_label_cooldown_is_seven_days(conn, clk, settings):
    s = _src(conn, clk, T.KIND_IPHONE_APP, "Notes", "2026-09-01T00:00:00+00:00")
    _measure(conn, clk, settings, s.id)
    clk.set("2026-09-16T10:00:00+00:00")
    labels.mark_prompted(conn, clk, [s.id])
    assert labels.get_source(conn, s.id).prompted_at == "2026-09-16T10:00:00+00:00"
    assert labels.sources_needing_label(conn, clk, settings) == []
    clk.set("2026-09-23T09:59:59+00:00")
    assert labels.sources_needing_label(conn, clk, settings) == []
    clk.set("2026-09-23T10:00:00+00:00")
    assert [x.id for x in labels.sources_needing_label(conn, clk, settings)] == [s.id]


def test_needing_label_orders_oldest_first_and_limits(conn, clk, settings):
    made = []
    for i, at in enumerate(["2026-09-05", "2026-09-01", "2026-09-03", "2026-09-01", "2026-09-02"]):
        made.append(_src(conn, clk, T.KIND_IPHONE_APP, f"App {i}", f"{at}T00:00:00+00:00"))
    clk.set("2026-09-16T10:00:00+00:00")
    for s in made:
        _measure(conn, clk, settings, s.id)
    expected = [made[1].id, made[3].id, made[4].id, made[2].id, made[0].id]   # ties by id
    assert [s.id for s in labels.sources_needing_label(conn, clk, settings)] == expected
    assert [s.id for s in labels.sources_needing_label(conn, clk, settings, limit=2)] == expected[:2]
    assert labels.sources_needing_label(conn, clk, settings, limit=0) == []


def test_limit_applies_after_filtering(conn, clk, settings):
    covered = _src(conn, clk, T.KIND_IPHONE_APP, "YouTube", "2026-09-01T00:00:00+00:00")
    later = _src(conn, clk, T.KIND_IPHONE_APP, "Notes", "2026-09-02T00:00:00+00:00")
    clk.set("2026-09-16T10:00:00+00:00")
    _measure(conn, clk, settings, covered.id)
    _measure(conn, clk, settings, later.id)
    assert [s.id for s in labels.sources_needing_label(conn, clk, settings, limit=1)] == [later.id]


def test_mark_prompted_empty_is_noop(conn, clk):
    labels.mark_prompted(conn, clk, [])


# --- list_sources -----------------------------------------------------------

def test_list_sources_filters(conn, clk):
    a = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "beta")
    b = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "Alpha")
    c = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "example.org")
    conn.commit()
    labels.set_source_label(conn, clk, b.id, T.WORTHWHILE)
    x_id = labels.ensure_source(conn, clk, T.KIND_X, "x").id

    assert [s.id for s in labels.list_sources(conn, kind=T.KIND_IPHONE_APP)] == [b.id, a.id]
    assert {s.id for s in labels.list_sources(conn, label="unlabelled")} == {a.id, c.id}
    assert [s.id for s in labels.list_sources(conn, label=T.WORTHWHILE)] == [b.id]
    assert [s.id for s in labels.list_sources(conn, label=T.WASTEFUL)] == [x_id]
    assert len(labels.list_sources(conn)) == 4
    assert labels.list_sources(conn, kind=T.KIND_WEB_DOMAIN, label=T.WORTHWHILE) == []
    with pytest.raises(labels.LabelError):
        labels.list_sources(conn, label="neutral")
    with pytest.raises(labels.LabelError):
        labels.list_sources(conn, kind="tiktok")


def test_unknown_youtube_channel_is_never_offered_for_labelling(conn, clk, settings):
    import evidence
    unknown = labels.ensure_source(conn, clk, "youtube_channel", "unknown", "Unknown channel")
    rec = evidence.begin_import(conn, clk, kind="youtube_history", parser_version="t")
    evidence.upsert_measurement(conn, rec, source_id=unknown.id, device="account",
                                provenance="page_extraction", period_start="2026-09-14",
                                period_end="2026-09-14", minutes=None, status="duration_missing",
                                item_key="vid")
    conn.commit()
    assert unknown.id not in [s.id for s in labels.sources_needing_label(conn, clk, settings)]


# --- tool: not content, a source label only (DECISIONS DR5.5) -------------------

@pytest.mark.parametrize("override,item_ref", list(itertools.product(OVERRIDES, REFS)))
def test_resolution_precedence_with_a_tool_source(conn, clk, override, item_ref):
    """An item override beats tool (a captured item is content); tool beats the default."""
    source_id = labels.ensure_source(conn, clk, T.KIND_PODCAST_SHOW, "Synthetic Show").id
    labels.set_source_label(conn, clk, source_id, T.TOOL)
    if override is not None:
        labels.set_item_override(conn, clk, REF, override)

    got = labels.resolve(conn, source_id=source_id, item_ref=item_ref)

    if override is not None and item_ref is not None:
        assert got == labels.ResolvedLabel(override, T.BASIS_ITEM_OVERRIDE)
    else:
        assert got == labels.ResolvedLabel(T.TOOL, T.BASIS_SOURCE_LABEL)


def test_tool_is_a_source_label_and_never_an_item_override(conn, clk):
    assert T.SOURCE_LABELS == (T.WASTEFUL, T.WORTHWHILE, T.TOOL)
    assert T.LABELS == (T.WASTEFUL, T.WORTHWHILE)
    src = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "amazon.pl")
    clk.set("2026-09-17T10:00:00+00:00")
    got = labels.set_source_label(conn, clk, src.id, T.TOOL)
    assert (got.label, got.labelled_at) == (T.TOOL, "2026-09-17T10:00:00+00:00")
    with pytest.raises(labels.LabelError):
        labels.set_item_override(conn, clk, REF, T.TOOL)
    assert labels.item_override(conn, REF) is None
    # There is no #tool token: it stays in the note as ordinary text, and no override is taken.
    assert labels.parse_override_token("bought a charger #tool") == ("bought a charger #tool", None)


def test_a_tool_source_counts_as_labelled_and_is_never_asked_about(conn, clk, settings):
    tool = _src(conn, clk, T.KIND_WEB_DOMAIN, "allegro.pl", "2026-09-10T00:00:00+00:00")
    open_ = _src(conn, clk, T.KIND_WEB_DOMAIN, "reddit.com", "2026-09-11T00:00:00+00:00")
    clk.set("2026-09-16T10:00:00+00:00")
    for s in (tool, open_):
        _measure(conn, clk, settings, s.id)
    labels.set_source_label(conn, clk, tool.id, T.TOOL)
    assert [s.id for s in labels.sources_needing_label(conn, clk, settings)] == [open_.id]


def test_list_sources_filters_tool(conn, clk):
    a = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "amazon.pl")
    b = labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "reddit.com")
    conn.commit()
    labels.set_source_label(conn, clk, a.id, T.TOOL)
    assert [s.id for s in labels.list_sources(conn, label=T.TOOL)] == [a.id]
    assert {s.id for s in labels.list_sources(conn, label="unlabelled")} == {b.id}
