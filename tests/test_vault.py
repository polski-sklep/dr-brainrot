"""sources/vault.py: outbox ingestion, capture-ID scan, outcomes (CONTRACTS Part II §6.4).

Every vault here is synthetic and built in tmp_path. Nothing from the real
vault is read, committed or printed.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil

import pytest

import clock
import config
import db
import dr_brainrot_types as T
import labels
from sources import vault

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vault"
OBSIDIAN_INGEST_VECTORS = pathlib.Path("/Users/Jacob/Projects/obsidian-ingest/tests/body_hash_vectors.json")

ID_X = "a" * 32
ID_ARTICLE = "b" * 32
ID_YT = "c" * 32
ID_TEXT = "d" * 32
ID_DELETED = "e" * 32
ID_TRASHED = "f" * 32
ID_DUP = "1" * 32
ID_DUP_INBOX = "2" * 32


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "vault").mkdir()
    return config.test_settings(tz="Europe/Warsaw", raw_dir=tmp_path / "raw", vault_path=tmp_path / "vault",
                                capture_outbox=tmp_path / "outbox.jsonl")


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


def note(cid: str | None, body: str, **fm) -> str:
    lines = ["---", "type: reference"]
    lines += [f"{k}: {v}" for k, v in fm.items()]
    if cid is not None:
        lines.append(f"{vault.FRONTMATTER_KEY}: {cid}")
    lines.append("---")
    return "\n".join(lines) + f"\n\n{body}\n"


def put(settings, rel: str, text: str) -> pathlib.Path:
    path = pathlib.Path(settings.vault_path) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def line(cid: str, *, capture_type="x-post", body="Body text.", rel_path=None, url="https://x.com/a/status/1",
         title="Synthetic title", **extra) -> dict:
    obj = {"v": 1, "capture_id": cid, "captured_at": "2026-09-15T08:00:00+00:00", "capture_type": capture_type,
           "url": url, "title": title, "author": None, "rel_path": rel_path or f"01-Inbox/Read/{cid[:4]}.md",
           "body_sha256": hashlib.sha256(body.strip().encode()).hexdigest()}
    obj.update(extra)
    return obj


def write_outbox(settings, *items) -> None:
    with open(settings.capture_outbox, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write((item if isinstance(item, str) else json.dumps(item)) + "\n")


def append_outbox(settings, *items) -> None:
    with open(settings.capture_outbox, "a", encoding="utf-8") as fh:
        for item in items:
            fh.write((item if isinstance(item, str) else json.dumps(item)) + "\n")


# --- body hash: shared vector ----------------------------------------------

def _vectors(path: pathlib.Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _vectors(FIXTURES / "body_hash_vectors.json"), ids=lambda c: c["name"])
def test_body_hash_matches_shared_vector(case):
    assert hashlib.sha256(case["expected_body"].encode("utf-8")).hexdigest() == case["sha256"]
    assert vault.body_sha256(case["note_text"]) == case["sha256"]


def test_frontmatter_only_edit_vector_shares_the_hash():
    cases = {c["name"]: c for c in _vectors(FIXTURES / "body_hash_vectors.json")}
    assert cases["standard_capture"]["sha256"] == cases["frontmatter_only_edit"]["sha256"]


def test_obsidian_ingest_carries_an_identical_vector_copy():
    if not OBSIDIAN_INGEST_VECTORS.exists():
        pytest.skip("obsidian-ingest checkout (with its body-hash vector copy) not present")
    assert OBSIDIAN_INGEST_VECTORS.read_bytes() == (FIXTURES / "body_hash_vectors.json").read_bytes()


# --- frontmatter reader -----------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    (f"---\ndrbr_capture_id: {ID_X}\n---\nbody", ID_X),
    (f"---\ntitle: t\ndrbr_capture_id: \"{ID_X.upper()}\"\n---\n", ID_X),
    (f"---\ndrbr_capture_id: '{ID_X}'\n---\n", ID_X),
    (f"---\r\ndrbr_capture_id: {ID_X}   # set by bot\r\n---\r\n", ID_X),
    (f"---\ntags:\n  - x\n  drbr_capture_id: {ID_X}\n---\n", None),        # indented: not top-level
    (f"no frontmatter\ndrbr_capture_id: {ID_X}\n", None),
    (f"---\na: 1\n---\n---\ndrbr_capture_id: {ID_X}\n---\n", None),          # only the first block
    (f"---\nnot_drbr_capture_id: {ID_X}\n---\n", None),
    ("---\ndrbr_capture_id: nope\n---\n", "nope"),                           # malformed, reported by scan
])
def test_frontmatter_capture_id(text, expected):
    assert vault.frontmatter_capture_id(text) == expected


@pytest.mark.parametrize("capture_type,group", [
    ("x-post", "x"), ("x-article", "x"), ("youtube", "youtube"), ("article", "web"),
    ("text", "other"), ("curriculum", "other"), ("tweet", "other"), ("", "other"),
])
def test_keeper_group_for(capture_type, group):
    assert vault.keeper_group_for(capture_type) == group


# --- outbox -----------------------------------------------------------------

def test_missing_outbox_is_nothing_captured(conn, clk, settings):
    assert vault.read_outbox(conn, clk, settings) == vault.OutboxResult(0, [])


def test_outbox_is_idempotent_and_reports_malformed_lines(conn, clk, settings):
    write_outbox(
        settings,
        line(ID_X),
        "{not json",
        "",
        line(ID_ARTICLE, capture_type="article", url="https://www.Example.org/post?id=1"),
        json.dumps({**line(ID_YT), "capture_id": "short"}),
        json.dumps({**line(ID_YT), "captured_at": "2026-09-15T08:00:00"}),
        json.dumps({**line(ID_YT), "rel_path": "/abs/path.md"}),
        json.dumps({**line(ID_YT), "v": 2}),
        "[1, 2]",
        line(ID_YT, capture_type="youtube", url="https://youtu.be/abc"),
    )
    first = vault.read_outbox(conn, clk, settings)
    assert first.new == 3
    assert [m.split(":")[0] for m in first.malformed] == ["line 2", "line 5", "line 6", "line 7", "line 8", "line 9"]
    assert all("x.com" not in m and "Synthetic" not in m for m in first.malformed)   # reasons, not content

    again = vault.read_outbox(conn, clk, settings)
    assert again.new == 0 and len(again.malformed) == 6
    assert conn.execute("SELECT COUNT(*) FROM vault_captures").fetchone()[0] == 3

    append_outbox(settings, line(ID_TEXT, capture_type="text", url=None))
    assert vault.read_outbox(conn, clk, settings).new == 1


def test_outbox_attribution_uses_explicit_data_only(conn, clk, settings):
    write_outbox(
        settings,
        line(ID_X, capture_type="x-article"),
        line(ID_ARTICLE, capture_type="article", url="https://www.Example.org/post?id=1"),
        line(ID_YT, capture_type="youtube", url="https://www.youtube.com/watch?v=abc"),
        line(ID_TEXT, capture_type="text", url=None),
        line(ID_DELETED, capture_type="article", url=None),
    )
    vault.read_outbox(conn, clk, settings)
    rows = {r["capture_id"]: r for r in conn.execute("SELECT * FROM vault_captures")}
    x_source = labels.ensure_source(conn, clk, T.KIND_IPHONE_APP, "X")
    assert (rows[ID_X]["keeper_group"], rows[ID_X]["source_id"]) == ("x", x_source.id)
    art = labels.get_source(conn, rows[ID_ARTICLE]["source_id"])
    assert (rows[ID_ARTICLE]["keeper_group"], art.kind, art.source_key) == ("web", T.KIND_WEB_DOMAIN, "example.org")
    assert (rows[ID_YT]["keeper_group"], rows[ID_YT]["source_id"]) == ("youtube", None)
    assert (rows[ID_TEXT]["keeper_group"], rows[ID_TEXT]["source_id"]) == ("other", None)
    assert rows[ID_DELETED]["source_id"] is None
    assert rows[ID_X]["captured_at"] == "2026-09-15T08:00:00+00:00"
    assert rows[ID_X]["logged_at"] == "2026-09-16T10:00:00+00:00"


def test_outbox_conflicting_duplicate_keeps_first_and_reports(conn, clk, settings):
    write_outbox(settings, line(ID_X, title="first"), line(ID_X, title="first"), line(ID_X, title="second"))
    result = vault.read_outbox(conn, clk, settings)
    assert result.new == 1
    assert len(result.malformed) == 1 and result.malformed[0].startswith("line 3")
    assert conn.execute("SELECT title FROM vault_captures").fetchone()[0] == "first"


def test_outbox_zulu_timestamp_normalised(conn, clk, settings):
    write_outbox(settings, line(ID_X, captured_at="2026-09-15T08:00:00Z"))
    vault.read_outbox(conn, clk, settings)
    assert conn.execute("SELECT captured_at FROM vault_captures").fetchone()[0] == "2026-09-15T08:00:00+00:00"


# --- scan -------------------------------------------------------------------

BODY = "Captured body."


@pytest.fixture
def synthetic_vault(conn, clk, settings):
    """A small vault exercising every outcome. Returns nothing; state is on disk and in the DB."""
    put(settings, ".obsidian/workspace.md", note(ID_TEXT, BODY))          # ignored config dir
    put(settings, ".git/stray.md", note(ID_TEXT, BODY))                   # ignored
    put(settings, "02-Ideas/untracked.md", note(None, "old note"))        # no id: outside measurement
    # Still in the inbox (a subfolder counts as inbox).
    put(settings, "01-Inbox/Read/YoutubeTranscripts/video.md", note(ID_YT, BODY, format="youtube"))
    # Renamed and moved out of the inbox, frontmatter edited only.
    put(settings, "02-Ideas/Deep/A better name.md",
        note(ID_X, BODY, format="x-post", read_status="read", tags="[idea]"))
    # Moved and its body developed.
    put(settings, "03-Projects/article.md", note(ID_ARTICLE, BODY + "\n\nMy own thoughts added."))
    # Deleted to Obsidian's trash only.
    put(settings, ".trash/gone.md", note(ID_TRASHED, BODY))
    # Duplicated: one copy in the inbox, one kept elsewhere.
    put(settings, "01-Inbox/Read/dup.md", note(ID_DUP, BODY))
    put(settings, "04-Archive/dup copy.md", note(ID_DUP, BODY))
    # Duplicated but both copies still in the inbox.
    put(settings, "01-Inbox/a.md", note(ID_DUP_INBOX, BODY))
    put(settings, "01-Inbox/Read/b.md", note(ID_DUP_INBOX, BODY))
    # Carries an id the outbox never logged.
    put(settings, "02-Ideas/unlogged.md", note("9" * 32, BODY))
    # Malformed id.
    put(settings, "02-Ideas/bad.md", note("zzz", BODY))
    write_outbox(
        settings,
        line(ID_X, body=BODY, rel_path="01-Inbox/Read/Original name.md"),
        line(ID_ARTICLE, capture_type="article", body=BODY, url="https://example.org/a"),
        line(ID_YT, capture_type="youtube", body=BODY),
        line(ID_DELETED, body=BODY),
        line(ID_TRASHED, body=BODY),
        line(ID_DUP, body=BODY),
        line(ID_DUP_INBOX, body=BODY),
        "garbage line",
    )
    assert vault.read_outbox(conn, clk, settings).new == 7


def test_scan_outcomes(conn, clk, settings, synthetic_vault):
    outcome = vault.scan(conn, clk, settings)
    assert outcome.status == "partial" and outcome.measurements_written == 0
    got = vault.latest_outcomes(conn)
    at = "2026-09-16T10:00:00+00:00"
    assert got[ID_YT] == vault.VaultOutcome("unprocessed", "01-Inbox/Read/YoutubeTranscripts/video.md", False, 1, at)
    assert got[ID_X] == vault.VaultOutcome("kept", "02-Ideas/Deep/A better name.md", False, 1, at)
    assert got[ID_ARTICLE] == vault.VaultOutcome("kept", "03-Projects/article.md", True, 1, at)
    assert got[ID_DELETED] == vault.VaultOutcome("excluded", None, None, 0, at)
    assert got[ID_TRASHED] == vault.VaultOutcome("excluded", None, None, 0, at)
    assert got[ID_DUP] == vault.VaultOutcome("kept", "04-Archive/dup copy.md", False, 2, at)
    assert got[ID_DUP_INBOX] == vault.VaultOutcome("unprocessed", "01-Inbox/Read/b.md", False, 2, at)
    assert set(got) == {ID_X, ID_ARTICLE, ID_YT, ID_DELETED, ID_TRASHED, ID_DUP, ID_DUP_INBOX}

    joined = "\n".join(outcome.warnings)
    assert ID_DUP in joined and ID_DUP_INBOX in joined
    assert "1 note(s) carry a capture id the outbox never logged" in joined
    assert "02-Ideas/bad.md" in joined

    row = conn.execute("SELECT kind, raw_path, status, meta, period_start FROM imports WHERE id = ?",
                       (outcome.import_id,)).fetchone()
    meta = json.loads(row["meta"])
    assert (row["kind"], row["raw_path"], row["period_start"]) == ("vault_scan", None, None)
    assert meta == {"files_scanned": 10, "captures": 7, "unprocessed": 2, "kept": 3, "excluded": 2,
                    "edited": 1, "duplicate_ids": 2, "unlogged_ids": 1, "malformed_ids": 1}
    # A vault scan is not "first valid data" for the diagnostic clock.
    assert conn.execute("SELECT value FROM meta WHERE key = 'diagnostic_start'").fetchone() is None
    # Paths are always stored relative to the vault.
    for (p,) in conn.execute("SELECT rel_path FROM vault_observations WHERE rel_path IS NOT NULL"):
        assert not p.startswith("/") and str(settings.vault_path) not in p


def test_rename_and_move_keeps_identity_across_scans(conn, clk, settings):
    write_outbox(settings, line(ID_X, body=BODY))
    vault.read_outbox(conn, clk, settings)
    path = put(settings, "01-Inbox/Read/Original.md", note(ID_X, BODY))
    vault.scan(conn, clk, settings)
    assert vault.latest_outcomes(conn)[ID_X].outcome == "unprocessed"

    clk.advance(days=7)
    target = pathlib.Path(settings.vault_path) / "05-Notes/Renamed entirely.md"
    target.parent.mkdir(parents=True)
    shutil.move(path, target)
    vault.scan(conn, clk, settings)
    latest = vault.latest_outcomes(conn)[ID_X]
    assert (latest.outcome, latest.rel_path, latest.edited, latest.scanned_at) == \
        ("kept", "05-Notes/Renamed entirely.md", False, "2026-09-23T10:00:00+00:00")
    # History is appended, not overwritten.
    assert [r[0] for r in conn.execute(
        "SELECT outcome FROM vault_observations WHERE capture_id = ? ORDER BY id", (ID_X,))] == \
        ["unprocessed", "kept"]


def test_deletion_after_keeping_becomes_excluded(conn, clk, settings):
    (pathlib.Path(settings.vault_path) / "01-Inbox").mkdir()   # a real vault has its inbox (Phase 4: scan now requires it)
    write_outbox(settings, line(ID_X, body=BODY))
    vault.read_outbox(conn, clk, settings)
    path = put(settings, "02-Ideas/n.md", note(ID_X, BODY))
    vault.scan(conn, clk, settings)
    path.unlink()
    clk.advance(days=1)
    vault.scan(conn, clk, settings)
    assert vault.latest_outcomes(conn)[ID_X].outcome == "excluded"


def test_latest_outcomes_uses_newest_even_at_the_same_instant(conn, clk, settings):
    write_outbox(settings, line(ID_X, body=BODY))
    vault.read_outbox(conn, clk, settings)
    path = put(settings, "01-Inbox/n.md", note(ID_X, BODY))
    vault.scan(conn, clk, settings)
    path.rename(pathlib.Path(settings.vault_path) / "n.md")      # vault root is outside the inbox
    vault.scan(conn, clk, settings)
    assert vault.latest_outcomes(conn)[ID_X].outcome == "kept"


def test_edited_is_body_only(conn, clk, settings):
    (pathlib.Path(settings.vault_path) / "01-Inbox").mkdir()   # a real vault has its inbox (Phase 4: scan now requires it)
    write_outbox(settings, line(ID_X, body=BODY), line(ID_ARTICLE, body=BODY))
    vault.read_outbox(conn, clk, settings)
    put(settings, "02-Ideas/fm.md", note(ID_X, BODY, read_status="done", rating=5) + "\n\n\n")
    put(settings, "02-Ideas/body.md", note(ID_ARTICLE, BODY + " (edited)"))
    vault.scan(conn, clk, settings)
    got = vault.latest_outcomes(conn)
    assert got[ID_X].edited is False
    assert got[ID_ARTICLE].edited is True


def test_custom_inbox_setting_and_trailing_slash(conn, clk, settings):
    s = config.test_settings(**{**settings.__dict__, "vault_inbox": "Inbox/"})
    write_outbox(s, line(ID_X, body=BODY), line(ID_ARTICLE, body=BODY))
    vault.read_outbox(conn, clk, s)
    put(s, "Inbox/deep/sub/n.md", note(ID_X, BODY))
    put(s, "Inbox-old/n.md", note(ID_ARTICLE, BODY))          # a sibling prefix is not the inbox
    vault.scan(conn, clk, s)
    got = vault.latest_outcomes(conn)
    assert (got[ID_X].outcome, got[ID_ARTICLE].outcome) == ("unprocessed", "kept")


def test_missing_vault_fails_loudly_instead_of_excluding_everything(conn, clk, settings, tmp_path):
    write_outbox(settings, line(ID_X, body=BODY))
    vault.read_outbox(conn, clk, settings)
    s = config.test_settings(**{**settings.__dict__, "vault_path": tmp_path / "unmounted"})
    outcome = vault.scan(conn, clk, s)
    assert outcome.status == "failed" and "not a readable folder" in outcome.error
    assert conn.execute("SELECT COUNT(*) FROM vault_observations").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM imports WHERE id = ?", (outcome.import_id,)).fetchone()[0] == "failed"

    unset = config.test_settings(**{**settings.__dict__, "vault_path": None})
    assert vault.scan(conn, clk, unset).status == "failed"


def test_missing_inbox_folder_is_a_warning(conn, clk, settings):
    outcome = vault.scan(conn, clk, settings)
    assert outcome.status == "partial"
    assert any("inbox folder" in w for w in outcome.warnings)


def test_missing_inbox_with_captures_fails_rather_than_marking_everything_kept(conn, clk, settings):
    """Phase 4 regression: without the inbox every capture read as 'kept'."""
    write_outbox(settings, line(ID_X, body=BODY))
    vault.read_outbox(conn, clk, settings)
    put(settings, "Somewhere/n.md", note(ID_X, BODY))
    outcome = vault.scan(conn, clk, settings)
    assert outcome.status == "failed" and "inbox folder" in outcome.error
    assert conn.execute("SELECT COUNT(*) FROM vault_observations").fetchone()[0] == 0
    assert vault.latest_outcomes(conn) == {}


def test_inbox_setting_in_the_wrong_case_still_finds_the_inbox(conn, clk, settings):
    """Phase 4 regression: on a case-insensitive disk '01-inbox' passed is_dir()
    but matched no note path, so an inbox note was recorded as kept."""
    s = config.test_settings(**{**settings.__dict__, "vault_inbox": "01-inbox/read"})
    write_outbox(s, line(ID_X, body=BODY), line(ID_ARTICLE, body=BODY))
    vault.read_outbox(conn, clk, s)
    put(s, "01-Inbox/Read/n.md", note(ID_X, BODY))
    put(s, "02-Ideas/k.md", note(ID_ARTICLE, BODY))
    outcome = vault.scan(conn, clk, s)
    assert outcome.status == "ok", outcome
    got = vault.latest_outcomes(conn)
    assert (got[ID_X].outcome, got[ID_ARTICLE].outcome) == ("unprocessed", "kept")


def test_clean_scan_is_ok(conn, clk, settings):
    write_outbox(settings, line(ID_X, body=BODY))
    vault.read_outbox(conn, clk, settings)
    put(settings, "01-Inbox/Read/n.md", note(ID_X, BODY))
    outcome = vault.scan(conn, clk, settings)
    assert (outcome.status, outcome.warnings) == ("ok", ())


def test_non_utf8_note_is_warned_not_fatal(conn, clk, settings):
    write_outbox(settings, line(ID_X, body=BODY))
    vault.read_outbox(conn, clk, settings)
    put(settings, "01-Inbox/Read/n.md", note(ID_X, BODY))
    p = pathlib.Path(settings.vault_path) / "02-Ideas/latin1.md"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"caf\xe9")
    outcome = vault.scan(conn, clk, settings)
    assert outcome.status == "partial"
    assert vault.latest_outcomes(conn)[ID_X].outcome == "unprocessed"
