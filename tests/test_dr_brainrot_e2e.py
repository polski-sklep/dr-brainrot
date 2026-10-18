"""dr-brainrot end to end: one diagnostic fortnight through every real entry point.

Phase 4, independent verification (dr-brainrot-claude-code-prompt.md, "Integration
test"). Written by the verifier, not by any agent that built the code.

Why one long scenario rather than many small tests: the defects this guards
against live *between* modules — a label set in Telegram that the dashboard
never sees, a late import that never reaches the recorded overage, a source identifier
the vault and Cold Turkey spell differently, a provenance that is flattened
on the way to the page. Unit tests already pin each module alone. Here the
whole chain runs once, in order, on a frozen clock, and each numbered block
asserts one of the brief's promises at the point in the story where it
becomes observable. Numbers in the comments match the Phase 4 checklist:

  [1] capture creation              [10] vault outcome: unprocessed
  [2] source-label inheritance      [11] vault outcome: kept
  [3] per-item override             [12] vault exclusion (deleted, trashed)
  [4] unlabelled defaults wasteful  [13] move/rename keeps identity
  [5] worthwhile gate: lock, cost 0 [14] report generation
  [6] wasteful allowance rules      [15] provenance to report and HTML
  [7] pooled spending across sources[16] duplicate ingestion, every source
  [8] week rollover                 [17] parser failure, every source
  [9] overage recorded, never carried (DR5.2), incl. late re-record

Everything external is faked at its boundary and nothing else: FakeSpotify
and an httpx.MockTransport SpotifyClient, FakeYouTubeApi behind the real
ingest blueprint, recorded Vision observations behind the real Screen Time
handler, the sanitised Cold Turkey export behind the real document handler,
and obsidian-ingest's own `vault_write.write_note` writing into a temporary
vault (emulated with the identical line format only if that checkout is
absent). The database starts as a queue version-1 file with queue rows in it and
is migrated, as the live one was.

Timeline (Europe/Warsaw, CEST = UTC+2; go-live Sunday 13 Sep 2026):

  Sun 13 15:00Z  migrate the pre-dr-brainrot queue database
  Sun 13 16:00Z  triage: the queue's week closes, the go-live allowance week opens
  Mon 14 06:00Z  four captures in Telegram; a show labelled worthwhile
  Tue 15 12:00Z  obsidian-ingest files five notes
  Wed 16 05:00Z  a worthwhile promotion is refused: still locked
  Wed 16 07:00Z  poll lifts locks and reads positions; promotions
  Thu 17 07:00Z  poll again; Spotify duplicate and scope failure
  Sun 20 07:05Z  YouTube sync through the extension API; channel labelled
  Sun 20 16:00Z  triage: the go-live week closes
  Mon 21 07:30Z  Screen Time screenshots, a label, the Cold Turkey export
  Mon 21 09:00Z  daily job (vault misconfigured, then correct)
  Mon 21 12:00Z  notes moved, renamed, edited, deleted, trashed
  Tue 22 09:00Z  daily job rescans; reports and dashboard read back
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import logging
import pathlib
import re
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import allowance  # noqa: E402
import bot  # noqa: E402
import clock  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import dr_brainrot_types as T  # noqa: E402
import evidence  # noqa: E402
import handlers_imports as hi  # noqa: E402
import handlers_labels  # noqa: E402
import jobs  # noqa: E402
import labels  # noqa: E402
import poll  # noqa: E402
import report  # noqa: E402
import spotify  # noqa: E402
import triage  # noqa: E402
from dashboard import app as dashboard_app  # noqa: E402
from sources import screentime, vault, youtube  # noqa: E402
from sources.ocr_vision import TextObservation  # noqa: E402
from spotify import Episode, FakeSpotify  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
OBSIDIAN_INGEST = pathlib.Path("/Users/Jacob/Projects/obsidian-ingest/vault_write.py")

MIN = 60_000
OWNER = 1
TOKEN = "e2e-ingest-token"

# Spotify episodes (22-character ids, as the queue requires).
LEGACY = "LegacyEpisode000000001"
DROPPED = "DroppedEpisode00000001"
W = "WastefulEpisode0000001"       # "Drama Hour", unlabelled
G = "WorthwhileEpisode00001"       # "Deep Science", labelled worthwhile at the source
M = "MarathonEpisode0000001"       # "Drama Hour", too long for what is left

# YouTube, from the sanitised history fixture (tests/test_youtube.py's API table).
CHANNEL_A = "UCapiChannelAAAAAAAAAAAA"      # odd fixture ids; labelled worthwhile on the dashboard
CHANNEL_B = "UCapiChannelBBBBBBBBBBBB"      # even fixture ids; never labelled
CAPTURED_VIDEO = "fxVideo0004"              # channel B, captured with #worthwhile
YT_DURATIONS = {
    "fxVideo0001": 45, "fxVideo0002": 180, "fxVideo0003": 60,
    "fxVideo0004": 600, "fxVideo0005": 1200, "fxVideo0006": 3723, "fxVideo0007": 600,
    "fxVideo0008": 900, "fxVideo0009": None, "fxVideo0010": 60, "fxVideo0011": 60,
    "fxVideo0012": 60, "fxVideo0013": 300, "fxVideo0014": 600, "fxVideo0015": 600,
    "fxVideo0016": 600,
}

LEGACY_WEEK = "2026-09-06T16:00:00+00:00"   # queue week opened before dr-brainrot existed
OLDER_WEEK = "2026-08-30T16:00:00+00:00"
GO_LIVE_WEEK = "2026-09-13T16:00:00+00:00"  # Sun 13 Sep 18:00 Warsaw
NEXT_WEEK = "2026-09-20T16:00:00+00:00"

QUEUE_TABLES = {
    "episodes": "spotify_id, title, show, description, release_date, duration_ms",
    "queue": "id, spotify_id, why_note, captured_at, deadline, state, cycles_seen, still_holds, promoted_at, resolved_at",
    "listening": "id, spotify_id, polled_at, position_ms, delta_ms, fully_played",
    "weeks": "week_start, allowance_min, debt_min, promoted_min, listened_min",
    "alerts_sent": "week_start, kind, sent_at",
    "deadline_notices": "queue_id, sent_at",
}


# ==========================================================================
# Boundary doubles (Telegram objects only; everything behind them is real)
# ==========================================================================

class User:
    def __init__(self, uid=OWNER):
        self.id = uid


class Chat:
    def __init__(self, cid=OWNER):
        self.id = cid


class TgFile:
    def __init__(self, data: bytes):
        self.data = data

    async def download_as_bytearray(self):
        return bytearray(self.data)


class FileRef:
    def __init__(self, data: bytes, file_name=None, mime_type=None):
        self.data, self.file_name, self.mime_type = data, file_name, mime_type

    async def get_file(self):
        return TgFile(self.data)


class Message:
    def __init__(self, text=None, *, photo=None, document=None, date=None, media_group_id=None):
        self.text = text
        self.photo = photo or []
        self.document = document
        self.date = date
        self.media_group_id = media_group_id
        self.chat = Chat()
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class Query:
    def __init__(self, data):
        self.data = data
        self.from_user = User()
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[str] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class Update:
    def __init__(self, *, message=None, query=None):
        self.effective_user = User()
        self.message = message
        self.effective_message = message
        self.callback_query = query


class Context:
    def __init__(self, bot_data):
        self.bot_data = bot_data
        self.user_data: dict = {}
        self.chat_data: dict = {}


class Gate:
    """The image batcher's quiet period, released by the test instead of a clock."""

    def __init__(self):
        self.event: asyncio.Event | None = None

    async def sleep(self, _delay):
        if self.event is None:
            self.event = asyncio.Event()
        await self.event.wait()

    def open(self):
        if self.event is None:
            self.event = asyncio.Event()
        self.event.set()


class SyncBot:
    """What jobs.py/triage.py/poll.py send through: records instead of calling Telegram."""

    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


def run(coro):
    return asyncio.run(coro)


def flat(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def section(html: str, section_id: str) -> str:
    m = re.search(rf'<section id="{section_id}">(.*?)</section>', html, re.S)
    assert m, f"section {section_id} missing"
    return m.group(1)


def table_rows(html: str) -> list[str]:
    return re.findall(r"<tr[^>]*>.*?</tr>", html, re.S)


def one_row(rows: list[str], *needles: str) -> str:
    found = [r for r in rows if all(n in r for n in needles)]
    assert len(found) == 1, (needles, len(found))
    return found[0]


def count(conn, table: str, where: str = "1 = 1", params=()) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


# ==========================================================================
# Evidence builders
# ==========================================================================

def episode(sid, title, show, minutes) -> Episode:
    return Episode(sid, title, show, None, "2026-09-01", minutes * MIN, 0, False)


def youtube_api() -> youtube.FakeYouTubeApi:
    videos = {}
    for vid, dur in YT_DURATIONS.items():
        n = int(vid[-4:])
        videos[vid] = T.VideoInfo(vid, f"API title {n}", CHANNEL_A if n % 2 else CHANNEL_B,
                                  "Channel A" if n % 2 else "Channel B", dur)
    return youtube.FakeYouTubeApi(videos)


def youtube_payload(pages, captured_at) -> bytes:
    return json.dumps({"schema": 1, "captured_at": captured_at, "extension_version": "0.1.0",
                       "cursor": None, "cursor_reached": False, "pages": pages}).encode()


def screentime_pages() -> tuple[list[list[TextObservation]], dict]:
    doc = json.loads((FIXTURES / "screentime" / "last_week.json").read_text())
    return [[TextObservation.from_dict(o) for o in p] for p in doc["pages"]], doc["region_recovery"]


def recorded_ocr(pages):
    """Recorded Vision output per screenshot, keyed by the image bytes' index."""
    def ocr(data: bytes):
        return pages[int(data.decode().split("-")[1])]
    return ocr


def recorded_region(recovery):
    obs = TextObservation.from_dict(recovery["observation"])

    def region(data: bytes, x, y, w, h):
        page = int(data.decode().split("-")[1])
        return [obs] if page == recovery["page"] and y <= obs.y <= y + h else []
    return region


def coldturkey_export() -> bytes:
    """The sanitised real export plus twitter.com (folds into X) and youtube.com (covered elsewhere)."""
    series = json.loads((FIXTURES / "coldturkey" / "website_statistics.json").read_text())
    days = [point[0] for point in series[0]["data"]]           # 13..19 Sep at UTC midnight
    series.append({"label": "twitter.com", "data": [[d, 5.0 if i == 2 else 0] for i, d in enumerate(days)]})
    series.append({"label": "youtube.com", "data": [[d, 40.0 if i == 1 else 0] for i, d in enumerate(days)]})
    return json.dumps(series).encode()


def load_vault_writer(monkeypatch, vault_root: pathlib.Path, outbox: pathlib.Path, clk):
    """obsidian-ingest's real `write_note`, pointed at the temporary vault and frozen clock.

    Falls back to an emulation of the exact outbox/frontmatter contract
    (CONTRACTS.md Part II §6.4) only when that checkout is not on this machine.
    """
    monkeypatch.setenv("DR_BRAINROT_CAPTURE_OUTBOX", str(outbox))

    class FrozenDateTime:
        @staticmethod
        def now(tz=None):
            return clk.now()

    if OBSIDIAN_INGEST.exists():
        spec = importlib.util.spec_from_file_location("obsidian_ingest_vault_write", OBSIDIAN_INGEST)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "VAULT_PATH", str(vault_root))
        monkeypatch.setattr(module, "datetime", FrozenDateTime)

        def write(title, body, folder, fm, capture_type):
            path, log_error = module.write_note(title, body, folder, fm, capture_type)
            assert log_error is None
            return pathlib.Path(path)
        return write, True

    def emulated(title, body, folder, fm, capture_type):  # pragma: no cover — only without the checkout
        capture_id = uuid.uuid4().hex
        fm = {**fm, "drbr_capture_id": capture_id}
        text = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\n\n" + body.rstrip() + "\n"
        path = vault_root / folder / f"{title}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"v": 1, "capture_id": capture_id, "captured_at": clock.iso(clk.now()),
                "capture_type": capture_type, "url": fm.get("url") or None, "title": title,
                "author": fm.get("author") or None, "rel_path": path.relative_to(vault_root).as_posix(),
                "body_sha256": vault.body_sha256(text)}
        with open(outbox, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        path.write_text(text, encoding="utf-8")
        return path
    return emulated, False


def build_pop_v1(path: pathlib.Path) -> dict[str, list[tuple]]:
    """A representative pre-dr-brainrot queue database: schema.sql only, with the queue's own rows."""
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA_PATH.read_text())
    raw.executemany("INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?)", [
        (LEGACY, "Old Talk", "Legacy Show", "desc", "2026-08-01", 60 * MIN),
        (DROPPED, "Skipped", "Legacy Show", None, None, 30 * MIN),
    ])
    raw.executemany("INSERT INTO queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (1, LEGACY, "why legacy", "2026-08-28T08:00:00+00:00", None, "played", 1, 1,
         "2026-08-30T17:00:00+00:00", "2026-09-08T09:00:00+00:00"),
        (2, DROPPED, "maybe later", "2026-08-29T08:00:00+00:00", "2026-09-30T16:00:00+00:00", "dropped", 0,
         None, None, "2026-08-29T20:00:00+00:00"),
    ])
    raw.executemany("INSERT INTO listening VALUES (?, ?, ?, ?, ?, ?)", [
        (1, LEGACY, "2026-09-07T09:00:00+00:00", 10 * MIN, 10 * MIN, 0),
        (2, LEGACY, "2026-09-08T09:00:00+00:00", 60 * MIN, 50 * MIN, 1),
    ])
    raw.executemany("INSERT INTO weeks VALUES (?, ?, ?, ?, ?)", [
        (OLDER_WEEK, 180, 0, 60, 0),
        (LEGACY_WEEK, 180, 0, 0, 60),
    ])
    raw.execute("INSERT INTO alerts_sent VALUES (?, 'approaching', ?)", (OLDER_WEEK, "2026-09-01T09:17:00+00:00"))
    raw.execute("INSERT INTO deadline_notices VALUES (2, '2026-08-29T09:17:00+00:00')")
    raw.execute("INSERT INTO meta VALUES ('last_triage_week', ?)", (LEGACY_WEEK,))
    raw.commit()
    snapshot = {t: raw.execute(f"SELECT {cols} FROM {t} ORDER BY 1").fetchall() for t, cols in QUEUE_TABLES.items()}
    assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "1"
    raw.close()
    return snapshot


# ==========================================================================
# The scenario
# ==========================================================================

def test_dr_brainrot_end_to_end(tmp_path, monkeypatch, caplog):
    vault_root = tmp_path / "vault"
    (vault_root / "01-Inbox" / "Read").mkdir(parents=True)
    outbox = tmp_path / "data" / "vault_captures.jsonl"
    outbox.parent.mkdir()
    settings = config.test_settings(
        tz="Europe/Warsaw", weekly_allowance_min=1000, telegram_user_id=OWNER,
        db_path=tmp_path / "data" / "dr-brainrot.db", raw_dir=tmp_path / "data" / "raw",
        vault_path=vault_root, vault_inbox="01-Inbox", capture_outbox=outbox,
        go_live="2026-09-13", diagnostic_weeks=8, ingest_token=TOKEN,
    )
    clk = clock.FrozenClock("2026-09-13T15:00:00+00:00")
    tg = SyncBot()

    # ------------------------------------------------------------------
    # Migration of a representative queue database: nothing lost
    # ------------------------------------------------------------------
    before = build_pop_v1(settings.db_path)
    conn = db.connect(settings)
    db.init_db(conn)
    db.migrate(conn)
    db.init_db(conn)
    db.migrate(conn)                                   # a restart re-runs both; still the current version, nothing doubled
    assert evidence.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    # Migration 0003 adds `weeks.overage_min` (NULL until computed) and zeroes debt_min;
    # these queue weeks already had debt 0, so the queue columns below are byte-identical.
    assert [tuple(r) for r in conn.execute("SELECT week_start, overage_min FROM weeks ORDER BY 1")] == \
        [(OLDER_WEEK, None), (LEGACY_WEEK, None)]
    for table, cols in QUEUE_TABLES.items():
        assert [tuple(r) for r in conn.execute(f"SELECT {cols} FROM {table} ORDER BY 1")] == \
            [tuple(r) for r in before[table]], table
    assert {tuple(r) for r in conn.execute("SELECT platform, source_key FROM episodes")} == {("spotify", None)}
    assert evidence.get_meta(conn, "last_triage_week") == LEGACY_WEEK
    x_source = conn.execute("SELECT * FROM sources WHERE kind = 'x'").fetchone()
    assert (x_source["source_key"], x_source["label"]) == ("x", "wasteful")      # the one pre-seeded label

    fake_spotify = FakeSpotify({
        W: episode(W, "Episode 12: The Feud", "Drama Hour", 90),
        G: episode(G, "Quantum Gravity, Slowly", "Deep Science", 1500),
        M: episode(M, "The Whole Saga", "Drama Hour", 2000),
    })
    yt_api = youtube_api()
    ctx = Context({"settings": settings, "conn": conn, "clk": clk, "spotify": fake_spotify, "youtube": yt_api})

    def say(text: str) -> list[str]:
        msg = Message(text)
        run(bot.on_message(Update(message=msg), ctx))
        return msg.replies

    def press(data: str) -> list[tuple[str | None, bool]]:
        q = Query(data)
        run(bot.on_callback(Update(query=q), ctx))
        return q.answers

    def label_in_dashboard(kind: str, key: str, label: str) -> int:
        """The only way to label since DR5.6: the dashboard's bulk write."""
        source_id = conn.execute("SELECT id FROM sources WHERE kind = ? AND source_key = ?", (kind, key)).fetchone()[0]
        return labels.bulk_set_label(conn, clk, [source_id], label, settings=settings)

    def overage(week_start: str) -> int | None:
        return conn.execute("SELECT overage_min FROM weeks WHERE week_start = ?", (week_start,)).fetchone()[0]

    def debt(week_start: str) -> int:
        return conn.execute("SELECT debt_min FROM weeks WHERE week_start = ?", (week_start,)).fetchone()[0]

    def queue_row(sid: str):
        return conn.execute("SELECT * FROM queue WHERE spotify_id = ? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()

    # ------------------------------------------------------------------
    # Sun 13 16:00Z — the queue's triage closes the pre-dr-brainrot week and opens go-live's
    # ------------------------------------------------------------------
    clk.set(GO_LIVE_WEEK)
    sent_before = len(tg.sent)
    triage.run_triage(conn, clk, settings, fake_spotify, tg)
    go_live = db._to_week(conn.execute("SELECT * FROM weeks WHERE week_start = ?", (GO_LIVE_WEEK,)).fetchone())
    assert (go_live.allowance_min, go_live.debt_min, go_live.promoted_min) == (1000, 0, 0)    # [8]
    assert len(tg.sent) == sent_before                        # an empty queue posts nothing (DR5.24)

    # ------------------------------------------------------------------
    # Mon 14 06:00Z — captures through the bot
    # ------------------------------------------------------------------
    clk.set("2026-09-14T06:00:00+00:00")
    assert "Why?" in say(f"https://open.spotify.com/episode/{W}")[0]
    assert count(conn, "queue") == 2                           # [1] nothing is written before the why-note
    reply_w = say("drama is my comfort food")[0]
    assert "Label: wasteful (source not labelled yet, so wasteful by default)" in reply_w      # [4]

    say(f"https://open.spotify.com/episode/{G}")
    say("the physics episode everyone cites")
    say(f"https://youtu.be/{CAPTURED_VIDEO}")
    reply_yt = say("#worthwhile for the talk prep")[0]
    assert "Label: worthwhile (your tag in this note)" in reply_yt and "Costs 0 min" in reply_yt   # [3]
    assert "Locked until" in reply_yt                                                           # [5]
    say(f"https://open.spotify.com/episode/{M}")
    say("all six parts, one sitting")

    # [1] capture creation
    for sid, note in ((W, "drama is my comfort food"), (G, "the physics episode everyone cites"),
                      (f"youtube:{CAPTURED_VIDEO}", "for the talk prep"), (M, "all six parts, one sitting")):
        row = queue_row(sid)
        assert (row["state"], row["why_note"], row["captured_at"]) == ("locked", note, "2026-09-14T06:00:00+00:00")
    yt_episode = conn.execute("SELECT * FROM episodes WHERE spotify_id = ?", (f"youtube:{CAPTURED_VIDEO}",)).fetchone()
    assert (yt_episode["platform"], yt_episode["source_key"], yt_episode["duration_ms"]) == ("youtube", CHANNEL_B, 600_000)
    assert [tuple(r) for r in conn.execute("SELECT item_ref, label, origin FROM item_labels")] == [
        (f"youtube:{CAPTURED_VIDEO}", "worthwhile", "capture")]
    assert {(r["kind"], r["source_key"], r["label"]) for r in conn.execute(
        "SELECT kind, source_key, label FROM sources WHERE kind != 'x'")} == {
        ("podcast_show", "drama hour", None), ("podcast_show", "deep science", None),
        ("youtube_channel", CHANNEL_B, None)}          # identity only: capturing never labels

    # [2] the user labels the show; the item inherits it
    assert label_in_dashboard("podcast_show", "deep science", T.WORTHWHILE) == 1
    g_item = db.get_item(conn, queue_row(G)["id"])
    assert allowance.item_label(conn, g_item) == labels.ResolvedLabel("worthwhile", T.BASIS_SOURCE_LABEL)
    w_item = db.get_item(conn, queue_row(W)["id"])
    assert allowance.item_label(conn, w_item) == labels.ResolvedLabel("wasteful", T.BASIS_DEFAULT)       # [4]
    yt_item = db.get_item(conn, queue_row(f"youtube:{CAPTURED_VIDEO}")["id"])
    assert allowance.item_label(conn, yt_item).basis == T.BASIS_ITEM_OVERRIDE                             # [3]

    # ------------------------------------------------------------------
    # Tue 15 12:00Z — obsidian-ingest files five notes
    # ------------------------------------------------------------------
    clk.set("2026-09-15T12:00:00+00:00")
    write_note, real_writer = load_vault_writer(monkeypatch, vault_root, outbox, clk)
    notes = {
        "stays": write_note("A thread on sleep", "Tweet body about sleep.", "01-Inbox/Read/X Bookmarks",
                            {"type": "reference", "format": "x-post", "url": "https://x.com/a/status/1", "source_type": "x-post", "source_url": "https://x.com/a/status/1",
                             "author": "someone"}, "x-post"),
        "developed": write_note("Long article on attention", "The long-form X article body.", "01-Inbox/Read",
                                {"type": "reference", "format": "x-article", "url": "https://x.com/b/status/2", "source_type": "x-article", "source_url": "https://x.com/b/status/2"},
                                "x-article"),
        "deleted": write_note("Talk transcript", "Transcript text.", "01-Inbox/YoutubeTranscripts",
                              {"type": "reference", "format": "youtube", "source_type": "youtube",
                               "url": f"https://www.youtube.com/watch?v={CAPTURED_VIDEO}", "source_url": f"https://www.youtube.com/watch?v={CAPTURED_VIDEO}"}, "youtube"),
        "archived": write_note("Blog post", "An article body.", "01-Inbox/Read",
                               {"type": "reference", "format": "article", "url": "https://www.example.org/post", "source_type": "article", "source_url": "https://www.example.org/post"},
                               "article"),
        "trashed": write_note("Another thread", "Tweet body two.", "01-Inbox/Read/X Bookmarks",
                              {"type": "reference", "format": "x-post", "url": "https://x.com/c/status/3", "source_type": "x-post", "source_url": "https://x.com/c/status/3"},
                              "x-post"),
    }
    outbox_lines = [json.loads(line) for line in outbox.read_text().splitlines()]
    ids = {name: re.search(r"(?m)^drbr_capture_id: ([0-9a-f]{32})$", path.read_text()).group(1)
           for name, path in notes.items()}
    assert [line["capture_id"] for line in outbox_lines] == list(ids.values())
    assert all(line["captured_at"] == "2026-09-15T12:00:00+00:00" for line in outbox_lines)

    # ------------------------------------------------------------------
    # Wed 16 05:00Z — 47 hours in: worthwhile is still locked
    # ------------------------------------------------------------------
    clk.set("2026-09-16T05:00:00+00:00")
    assert press(f"promote:{yt_item.id}") == [(bot.ALREADY_RESOLVED, False)]
    with pytest.raises(db.IllegalTransition):
        db.promote(conn, clk, settings, g_item.id)
    assert queue_row(G)["state"] == "locked" and queue_row(f"youtube:{CAPTURED_VIDEO}")["state"] == "locked"   # [5]
    assert queue_row(G)["promoted_at"] is None
    assert db.current_week(conn, clk, settings).promoted_min == 0

    # ------------------------------------------------------------------
    # Wed 16 07:00Z — poll lifts the locks; impulse listening before promotion
    # ------------------------------------------------------------------
    clk.set("2026-09-16T07:00:00+00:00")
    fake_spotify.set_position(W, 30 * MIN)             # heard before it was ever promoted: ungated
    fake_spotify.set_position(M, 20 * MIN)             # never promoted at all: ungated
    poll.run_poll(conn, clk, settings, fake_spotify, tg)
    assert {queue_row(s)["state"] for s in (W, G, M, f"youtube:{CAPTURED_VIDEO}")} == {"queued"}
    assert not any("youtube:" in sid for _, ids_ in fake_spotify.calls if _ == "get_episodes" for sid in ids_)

    clk.set("2026-09-16T07:05:00+00:00")
    assert press(f"promote:{yt_item.id}") == [("Promoted.", False)]                 # [5] worthwhile: cost 0
    assert press(f"promote:{g_item.id}") == [("Promoted.", False)]                  # [5] 1500 min, over the allowance, still free
    assert db.current_week(conn, clk, settings).promoted_min == 0
    assert press(f"promote:{w_item.id}") == [("Promoted.", False)]                  # [6] wasteful pays the remaining 60
    assert db.current_week(conn, clk, settings).promoted_min == 60
    m_item = db.get_item(conn, queue_row(M)["id"])
    refused = press(f"promote:{m_item.id}")                                         # [6] 1980 > 940 left
    assert refused == [("That would put you 1040 min over. 940 min left this week.", True)]
    assert queue_row(M)["state"] == "queued" and queue_row(M)["promoted_at"] is None
    assert db.current_week(conn, clk, settings).promoted_min == 60

    # ------------------------------------------------------------------
    # Thu 17 07:00Z — listening after promotion
    # ------------------------------------------------------------------
    clk.set("2026-09-17T07:00:00+00:00")
    fake_spotify.set_position(W, 80 * MIN)             # +50, gated, wasteful
    fake_spotify.set_position(G, 100 * MIN)            # +100, gated, worthwhile
    poll.run_poll(conn, clk, settings, fake_spotify, tg)
    listening_rows = count(conn, "listening")
    poll.run_poll(conn, clk, settings, fake_spotify, tg)                            # [16] Spotify: same positions again
    assert count(conn, "listening") == listening_rows
    assert not any("(wasteful only)" in t for t in tg.texts())       # 100 wasteful of 1000: no alert; worthwhile never counts
    pooled = allowance.pooled_minutes(conn, settings, GO_LIVE_WEEK)
    assert pooled.charged_min == pytest.approx(100)                                # 30 + 50 + 20
    assert pooled.worthwhile_min == pytest.approx(100)                              # counted, never charged

    # [17] Spotify: a token that lost its scope must fail loudly, never read as zero. The poll's
    #      positions step fails (logged, exit 1) and the rest of the poll still runs (DR5.25).
    def no_resume_point(request: httpx.Request) -> httpx.Response:
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "token_type": "Bearer", "expires_in": 3600,
                                             "scope": "user-read-playback-position"})
        return httpx.Response(200, json={"episodes": [
            {"id": sid, "type": "episode", "name": "x", "show": {"name": "Drama Hour"}, "duration_ms": 1}
            for sid in db.polling_targets(conn)]})
    scoped_out = spotify.SpotifyClient(settings, transport=httpx.MockTransport(no_resume_point))
    fake_spotify.set_position(W, 85 * MIN)
    with caplog.at_level(logging.ERROR, logger="dr-brainrot.poll"):
        assert poll.run_poll(conn, clk, settings, scoped_out, tg) == ["positions"]
    assert any(isinstance(r.exc_info[1], spotify.SpotifyError) and "resume_point" in str(r.exc_info[1])
               for r in caplog.records if r.exc_info)
    scoped_out.close()
    assert count(conn, "listening") == listening_rows

    # ------------------------------------------------------------------
    # Sun 20 07:05Z — YouTube history through the extension's API
    # ------------------------------------------------------------------
    monkeypatch.setattr(dashboard_app, "DataApiClient", lambda _settings: youtube_api())
    app = dashboard_app.create_app(settings, clk, lambda: db.connect(settings))
    app.config["TESTING"] = True
    web = app.test_client()
    headers = {"X-Dr-Brainrot-Token": TOKEN, "Content-Type": "application/json"}
    clk.set("2026-09-20T07:05:00+00:00")
    initial = json.loads((FIXTURES / "youtube" / "history_initial.json").read_text())
    body = youtube_payload([initial], "2026-09-20T07:00:00+00:00")

    assert web.post("/api/youtube/history", data=body,
                    headers={"X-Dr-Brainrot-Token": "wrong"}).status_code == 401                # [17] refused, nothing recorded
    assert count(conn, "imports", "kind = 'youtube_history'") == 0
    resp = web.post("/api/youtube/history", data=body, headers=headers)
    yt_outcome = resp.get_json()
    assert resp.status_code == 200 and yt_outcome["status"] == "partial"
    assert yt_outcome["measurements_written"] == 11
    assert yt_outcome["period_start"] == "2026-09-14"                                  # Sep 9 is before go-live
    yt_import = yt_outcome["import_id"]
    dup = web.post("/api/youtube/history", data=body, headers=headers)                 # [16]
    assert dup.status_code == 200 and dup.get_json()["duplicate_of"] == yt_import
    assert count(conn, "measurements", "import_id != ?", (yt_import,)) == 0
    broken = youtube_payload([{"contents": {"twoColumnBrowseResultsRenderer": {}}}], "2026-09-20T07:02:00+00:00")
    bad = web.post("/api/youtube/history", data=broken, headers=headers)               # [17]
    assert bad.status_code == 422 and bad.get_json()["status"] == "failed"
    assert "no itemSectionRenderer" in bad.get_json()["error"]
    assert web.get("/api/youtube/cursor", headers=headers).get_json() == {
        "cursor": "fxVideo0001", "cursor_date": "2026-09-20"}                            # the failure moved nothing
    assert evidence.get_meta(conn, "diagnostic_start") == "2026-09-14"

    yt_rows = {r["item_key"]: r for r in conn.execute(
        "SELECT m.*, s.source_key FROM measurements m JOIN sources s ON s.id = m.source_id "
        "WHERE m.provenance = 'page_extraction'")}
    shorts = [r for r in yt_rows.values() if json.loads(r["detail"])["is_short"]]
    assert len(shorts) == 5 and all(r["status"] == "ok" and json.loads(r["detail"])["assumed_full"] for r in shorts)
    assert (yt_rows["fxVideo0009"]["status"], yt_rows["fxVideo0009"]["minutes"]) == ("progress_missing", None)
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE kind = 'youtube_channel' AND source_key = ?",
                        (CHANNEL_B,)).fetchone()[0] == 1          # capture path and history path agree on the key

    # [2] channel A labelled on the dashboard: its videos inherit it
    channel_a = conn.execute("SELECT id FROM sources WHERE kind = 'youtube_channel' AND source_key = ?",
                             (CHANNEL_A,)).fetchone()[0]
    clk.set("2026-09-20T07:10:00+00:00")
    assert web.post(f"/labels/{channel_a}", data={"label": "worthwhile"}).status_code == 303
    by_item = {r.item: r for r in report.records(conn, clk, settings, group=T.GROUP_YOUTUBE)}
    assert (by_item["API title 5"].label, by_item["API title 5"].label_basis) == ("worthwhile", T.BASIS_SOURCE_LABEL)
    assert (by_item["API title 6"].label, by_item["API title 6"].label_basis) == ("wasteful", T.BASIS_DEFAULT)
    assert (by_item["API title 4"].label, by_item["API title 4"].label_basis) == ("worthwhile", T.BASIS_ITEM_OVERRIDE)

    # ------------------------------------------------------------------
    # Sun 20 16:00Z — triage closes the go-live week
    # ------------------------------------------------------------------
    clk.set(NEXT_WEEK)
    sent_before = len(tg.sent)
    triage.run_triage(conn, clk, settings, fake_spotify, tg)
    # [8] rollover: promotions lapse, evidence of the choice survives, a fresh week opens
    for sid in (W, G, f"youtube:{CAPTURED_VIDEO}"):
        row = queue_row(sid)
        assert row["state"] == "queued" and row["promoted_at"] == "2026-09-16T07:05:00+00:00"
    nxt = db._to_week(conn.execute("SELECT * FROM weeks WHERE week_start = ?", (NEXT_WEEK,)).fetchone())
    assert (nxt.allowance_min, nxt.promoted_min) == (1000, 0)
    # [9] overage from pooled wasteful: Spotify 100 + YouTube channel B 69.33 = 169 < 1000
    assert overage(GO_LIVE_WEEK) == 0
    assert nxt.debt_min == 0 and nxt.effective_allowance_min == 1000
    posts = [m["text"] for m in tg.sent[sent_before:]]
    assert any("Quantum Gravity" in p and "Label: worthwhile (source label) · costs 0 min — worthwhile" in p
               for p in posts)
    assert any("API title 4" in p and "Label: worthwhile (your tag at capture)" in p for p in posts)
    assert "Last week spent 169 counted content minutes across sources" in posts[-1]
    assert "Week of Sun 20 Sep 18:00" in posts[-1]            # displayed in Warsaw time, not Lisbon
    assert "went" not in posts[-1] and "Debt" not in posts[-1]

    # ------------------------------------------------------------------
    # Mon 21 07:30Z — Screen Time screenshots through the photo handler
    # ------------------------------------------------------------------
    pages, recovery = screentime_pages()
    received = datetime(2026, 9, 21, 7, 30, tzinfo=timezone.utc)

    def send_screenshots(images: list[bytes], ingest) -> Message:
        gate = Gate()
        ctx.bot_data[hi.BATCHER_KEY] = hi.ImageBatcher(delay=4.0, sleep=gate.sleep)
        ctx.bot_data["screentime_ingest"] = ingest
        handler = bot.authorised_only(hi.on_image)
        msgs = [Message(photo=[FileRef(b"thumb"), FileRef(data)], date=received, media_group_id="album")
                for data in images]

        async def scenario():
            for m in msgs:
                await handler(Update(message=m), ctx)
            gate.open()
            await ctx.bot_data[hi.BATCHER_KEY].drain()
        run(scenario())
        return msgs[-1]

    def st_ingest(ocr, region):
        return lambda c, k, s, images, *, received_at: screentime.ingest_batch(
            c, k, s, images, received_at=received_at, ocr=ocr, region_ocr=region)

    clk.set("2026-09-21T07:30:00+00:00")
    shots = [f"screenshot-{i}".encode() for i in range(10)]
    last = send_screenshots(shots, st_ingest(recorded_ocr(pages), recorded_region(recovery)))
    assert "Screen Time (10 screenshots): 39 app figures recorded for 2026-09-13 – 2026-09-19 (complete)." \
        == last.replies[0]
    # [9] late import: the closed week's minutes changed, so its recorded overage did too
    assert overage(GO_LIVE_WEEK) == 81               # 169.33 + 911.97 = 1081 -> 81 over 1000
    assert debt(NEXT_WEEK) == 0                      # and nothing is carried

    again = send_screenshots(shots, st_ingest(recorded_ocr(pages), recorded_region(recovery)))      # [16]
    assert "already imported" in again.replies[0]
    junk_pages = [[TextObservation("Hello there", 1.0, 0.2, 0.5, 0.3, 0.02)]] * 2
    junk = send_screenshots([b"screenshot-0", b"screenshot-1"],
                            st_ingest(recorded_ocr(junk_pages), lambda *a, **k: []))         # [17]
    assert junk.replies[0].startswith("Screen Time (2 screenshots): import failed, nothing was recorded.")
    assert "don't look like Screen Time" in junk.replies[0]
    assert count(conn, "measurements", "provenance = 'screenshot_ocr'") == 39
    assert count(conn, "imports", "kind = 'screentime' AND status = 'failed'") == 1
    assert overage(GO_LIVE_WEEK) == 81
    assert debt(NEXT_WEEK) == 0

    # A label set in the dashboard re-records the overage at once.
    clk.set("2026-09-21T07:40:00+00:00")
    label_in_dashboard("iphone_app", "chatterbox", T.WORTHWHILE)
    assert overage(GO_LIVE_WEEK) == 43               # 38 minutes moved to worthwhile
    assert debt(NEXT_WEEK) == 0

    # ------------------------------------------------------------------
    # Mon 21 08:00Z — Cold Turkey export through the document handler
    # ------------------------------------------------------------------
    def send_document(data: bytes, name: str) -> Message:
        msg = Message(document=FileRef(data, name, "application/json"), date=received)
        run(bot.authorised_only(hi.on_json_document)(Update(message=msg), ctx))
        return msg

    clk.set("2026-09-21T08:00:00+00:00")
    ctx.bot_data.pop("coldturkey_ingest", None)
    export = coldturkey_export()
    ct_msg = send_document(export, "Cold Turkey Blocker Website Statistics.json")
    assert ct_msg.replies[0] == "Cold Turkey: 49 domain-day figures recorded for 2026-09-13 – 2026-09-19 (complete)."
    assert overage(GO_LIVE_WEEK) == 135              # + 92.07 wasteful; youtube.com's 40 excluded as covered
    assert debt(NEXT_WEEK) == 0
    assert "already imported" in send_document(export, "again.json").replies[0]                       # [16]
    blocked = send_document((FIXTURES / "coldturkey" / "websites_blocked.json").read_bytes(), "blocked.json")
    assert "import failed, nothing was recorded" in blocked.replies[0] and "counts blocks" in blocked.replies[0]  # [17]
    assert count(conn, "measurements", "provenance = 'manual_export'") == 49

    # One X, however it is reported: iPhone app, x.com, twitter.com, and the vault.
    x_rows = conn.execute("SELECT DISTINCT m.source_id, m.item_key, m.provenance FROM measurements m "
                          "JOIN sources s ON s.id = m.source_id WHERE s.kind = 'x'").fetchall()
    assert {r["source_id"] for r in x_rows} == {x_source["id"]}
    assert {(r["provenance"], r["item_key"]) for r in x_rows} == {
        ("screenshot_ocr", ""), ("manual_export", "x.com"), ("manual_export", "twitter.com")}

    # ------------------------------------------------------------------
    # Mon 21 09:00Z — the daily job, first with the vault unmounted
    # ------------------------------------------------------------------
    clk.set("2026-09-21T09:00:00+00:00")
    with open(outbox, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    unmounted = dataclasses.replace(settings, vault_path=tmp_path / "not-mounted")
    assert jobs.run_daily(conn, clk, unmounted, tg) == ["vault_scan"]                                 # [17] loud
    failed_scan = conn.execute("SELECT * FROM imports WHERE kind = 'vault_scan' ORDER BY id DESC").fetchone()
    assert failed_scan["status"] == "failed" and "not a readable folder" in failed_scan["error"]
    assert count(conn, "vault_observations") == 0                    # never "everything was deleted"
    assert count(conn, "vault_captures") == 5                        # the outbox was still read; the bad line skipped

    # The first week of data is over and no job ran on Sunday, so Monday sends
    # the check-in once. Every source arrived, so it asks only for labels — with
    # a link, no buttons, and nothing to do in the chat (DR5.6, DR5.24).
    checkins = [t for t in tg.texts() if "check-in" in t.lower()]
    assert len(checkins) == 1
    (checkin,) = checkins
    assert checkin.startswith("Check-in for last week, Sun 13 – Sat 19 Sep.\n\n1. Label new sources. ")
    assert "/labels/queue" in checkin and "d:lbl:" not in checkin and "\n\n2." not in checkin
    assert checkin.endswith("Already in: Screen Time, YouTube, Cold Turkey.")     # every source covered
    assert not any(m.get("reply_markup") for m in tg.sent if "check-in" in m["text"].lower())

    prompts_before = len(tg.sent)
    assert jobs.run_daily(conn, clk, settings, tg) == []
    assert len(tg.sent) == prompts_before                             # once, not once per run
    first_scan = vault.latest_outcomes(conn)
    assert {name: first_scan[cid].outcome for name, cid in ids.items()} == dict.fromkeys(ids, "unprocessed")
    assert vault.read_outbox(conn, clk, settings).new == 0                                           # [16] vault

    # ------------------------------------------------------------------
    # Mon 21 12:00Z — the user tidies the vault
    # ------------------------------------------------------------------
    clk.set("2026-09-21T12:00:00+00:00")
    developed = vault_root / "10-Notes" / "Renamed idea.md"
    developed.parent.mkdir()
    shutil.move(notes["developed"], developed)
    developed.write_text(developed.read_text() + "\nMy own synthesis, added later.\n")
    archived = vault_root / "20-Archive" / "post.md"
    archived.parent.mkdir()
    shutil.move(notes["archived"], archived)
    notes["deleted"].unlink()
    (vault_root / ".trash").mkdir()
    shutil.move(notes["trashed"], vault_root / ".trash" / "Another thread.md")

    # ------------------------------------------------------------------
    # Tue 22 09:00Z — the daily job rescans
    # ------------------------------------------------------------------
    clk.set("2026-09-22T09:00:00+00:00")
    assert jobs.run_daily(conn, clk, settings, tg) == []
    now = vault.latest_outcomes(conn)
    assert now[ids["stays"]].outcome == "unprocessed"                                                 # [10]
    kept = now[ids["developed"]]
    assert (kept.outcome, kept.rel_path, kept.edited) == ("kept", "10-Notes/Renamed idea.md", True)  # [11] [13]
    assert [r[0] for r in conn.execute("SELECT outcome FROM vault_observations WHERE capture_id = ? ORDER BY id",
                                       (ids["developed"],))] == ["unprocessed", "kept"]              # [13] same identity
    assert conn.execute("SELECT initial_rel_path FROM vault_captures WHERE capture_id = ?",
                        (ids["developed"],)).fetchone()[0] == "01-Inbox/Read/Long article on attention.md"
    assert (now[ids["archived"]].outcome, now[ids["archived"]].edited) == ("kept", False)             # moved, untouched
    assert (now[ids["deleted"]].outcome, now[ids["trashed"]].outcome) == ("excluded", "excluded")    # [12]
    assert vault.scan(conn, clk, settings).status == "ok"                                             # [16] rescan
    assert {k: v.outcome for k, v in vault.latest_outcomes(conn).items()} == {k: v.outcome for k, v in now.items()}

    # Identifier agreement across sources: the vault's article and Cold Turkey's domain are one source.
    article_source = conn.execute("SELECT source_id FROM vault_captures WHERE capture_id = ?",
                                  (ids["archived"],)).fetchone()[0]
    ct_example = conn.execute("SELECT DISTINCT m.source_id FROM measurements m JOIN sources s ON s.id = m.source_id "
                              "WHERE s.kind = 'web_domain' AND s.source_key = 'example.org'").fetchall()
    assert [r[0] for r in ct_example] == [article_source]
    assert conn.execute("SELECT source_id FROM vault_captures WHERE capture_id = ?",
                        (ids["stays"],)).fetchone()[0] == x_source["id"]

    # ------------------------------------------------------------------
    # Tue 22 10:00Z — reports [14] with provenance [15]
    # ------------------------------------------------------------------
    clk.set("2026-09-22T10:00:00+00:00")
    diag = report.diagnostic_banner(conn, clk, settings)
    assert (diag.start, diag.end, diag.week_number, diag.days_remaining, diag.ended) == (
        "2026-09-14", "2026-11-08", 2, 47, False)

    totals = {(t.group, t.provenance, t.device): t for t in report.weekly_totals(conn, clk, settings, GO_LIVE_WEEK)}
    assert set(totals) == {
        (T.GROUP_PODCASTS, T.PROV_API, T.DEVICE_ACCOUNT),
        (T.GROUP_YOUTUBE, T.PROV_PAGE, T.DEVICE_ACCOUNT),
        (T.GROUP_X, T.PROV_SCREENSHOT, T.DEVICE_IPHONE),
        (T.GROUP_X, T.PROV_EXPORT, T.DEVICE_DESKTOP),
        (T.GROUP_IPHONE, T.PROV_SCREENSHOT, T.DEVICE_IPHONE),
        (T.GROUP_DESKTOP, T.PROV_EXPORT, T.DEVICE_DESKTOP),
    }
    pods = totals[(T.GROUP_PODCASTS, T.PROV_API, T.DEVICE_ACCOUNT)]
    assert (pods.minutes, pods.import_status) == (pytest.approx(200), None)
    yt = totals[(T.GROUP_YOUTUBE, T.PROV_PAGE, T.DEVICE_ACCOUNT)]
    assert (yt.minutes, yt.unmeasured_items, yt.import_status, yt.import_id) == (
        pytest.approx(96.4166667), 1, "partial", yt_import)
    iphone = totals[(T.GROUP_IPHONE, T.PROV_SCREENSHOT, T.DEVICE_IPHONE)]
    assert (iphone.minutes, iphone.import_status) == (pytest.approx(786.9666667), "ok")
    assert totals[(T.GROUP_X, T.PROV_SCREENSHOT, T.DEVICE_IPHONE)].minutes == pytest.approx(125)
    assert totals[(T.GROUP_X, T.PROV_EXPORT, T.DEVICE_DESKTOP)].minutes == pytest.approx(16.72)
    desktop = totals[(T.GROUP_DESKTOP, T.PROV_EXPORT, T.DEVICE_DESKTOP)]
    assert (desktop.minutes, desktop.covered_excluded_min) == (pytest.approx(75.35), pytest.approx(40))
    assert desktop.covered_reasons == (("YouTube history (account-level)", pytest.approx(40)),)

    # [7] one pooled allowance: every source's wasteful minutes in one figure
    pooled = allowance.pooled_minutes(conn, settings, GO_LIVE_WEEK)
    assert {g: round(v.charged_min, 2) for g, v in pooled.by_group.items()} == {
        T.GROUP_PODCASTS: 100.0, T.GROUP_YOUTUBE: 69.33, T.GROUP_X: 141.72,
        T.GROUP_IPHONE: 748.97, T.GROUP_DESKTOP: 75.35}
    assert pooled.charged_min == pytest.approx(1135.37)
    assert pooled.worthwhile_min == pytest.approx(100 + 27.0833333 + 38)
    assert pooled.covered_excluded_min == pytest.approx(40)
    assert pooled.screenshot_ocr_min == pytest.approx(911.9666667)

    split = {w.week_start: w for w in report.label_split(conn, clk, settings)}
    assert list(split) == [GO_LIVE_WEEK, NEXT_WEEK]            # weeks before the diagnostic are not bars
    assert split[GO_LIVE_WEEK].charged_min == pytest.approx(1135.37)

    status = report.allowance_status(conn, clk, settings)
    # Last week's 135 over is recorded on that week; this week keeps its full 1000.
    assert (status.week_start, status.overage_min, status.effective_min) == (NEXT_WEEK, 0, 1000)
    assert status.spent_wasteful_min == pytest.approx(3.0)        # the channel-B Short watched on Sunday 20th
    assert status.remaining_min == 997
    assert (split[GO_LIVE_WEEK].allowance_min, split[GO_LIVE_WEEK].overage_min) == (1000, 135)

    gated = {r.group: r for r in report.gated_vs_ungated(conn, clk, settings, GO_LIVE_WEEK)}
    # "Drama Hour" is deliberately never labelled in this walkthrough, so its 50
    # ungated minutes are undecided rather than wasteful: reflex is a subset of
    # wasteful, and neither claims a judgement nobody made (DR5.11).
    assert (gated[T.GROUP_PODCASTS].gated_min, gated[T.GROUP_PODCASTS].ungated_min,
            gated[T.GROUP_PODCASTS].wasteful_ungated_min) == (pytest.approx(150), pytest.approx(50), 0.0)
    assert gated[T.GROUP_YOUTUBE].gated_min == pytest.approx(4.0833333)        # the promoted, captured video
    # Channel A and Channel B are unlabelled here too, so the same rule applies:
    # ungated, but nothing is called wasteful on the person's behalf (DR5.11).
    assert gated[T.GROUP_YOUTUBE].wasteful_ungated_min == 0.0
    assert gated[T.GROUP_YOUTUBE].ungated_min == pytest.approx(92.3333333)
    assert gated[T.GROUP_IPHONE].gated_min == 0 and gated[T.GROUP_IPHONE].provenances == (T.PROV_SCREENSHOT,)
    assert gated[T.GROUP_X].provenances == (T.PROV_SCREENSHOT, T.PROV_EXPORT)
    reflex = sum(r.wasteful_ungated_min for r in gated.values())
    # Reflex is the ungated share of *labelled* wasteful minutes. In this
    # walkthrough only X is labelled wasteful, so the figure is its 141.72 and
    # not the 1,085 that counting every undecided source used to produce
    # (DR5.11). The undecided minutes are still carried, as unclassified.
    assert reflex == pytest.approx(141.72)
    assert allowance.pooled_minutes(conn, settings, GO_LIVE_WEEK).unclassified_min > 900

    keepers = report.cost_per_keeper(conn, clk, settings)
    assert keepers[0].group == T.GROUP_X
    assert (keepers[0].minutes, keepers[0].keepers, keepers[0].minutes_per_keeper) == (
        pytest.approx(133.1), 1, pytest.approx(133.1))           # Sep 13 is before the window; Sep 14 on counts
    assert keepers[0].provenances == (T.PROV_SCREENSHOT, T.PROV_EXPORT, T.PROV_VAULT)
    by_group = {k.group: k for k in keepers}
    assert (by_group[T.GROUP_YOUTUBE].keepers, by_group[T.GROUP_YOUTUBE].minutes_per_keeper) == (0, None)
    assert (by_group[T.GROUP_WEB].minutes, by_group[T.GROUP_WEB].keepers) == (None, 1)
    assert [k.minutes_per_keeper is None for k in keepers] == sorted(k.minutes_per_keeper is None for k in keepers)

    funnels = report.funnels(conn, clk, settings)
    assert (funnels.vault.captured, funnels.vault.unprocessed, funnels.vault.kept,
            funnels.vault.excluded, funnels.vault.edited_kept) == (3, 1, 2, 2, 1)   # [12] excluded outside
    assert (funnels.queue.captured, funnels.queue.survived_lock, funnels.queue.promoted, funnels.queue.played) == (4, 4, 3, 3)

    spots = report.blind_spots(conn, clk, settings)
    assert "1 YouTube video(s) with no readable progress are left out of minutes (not counted as zero)." in spots
    assert not any("has not been received" in s for s in spots)

    ios_records = report.records(conn, clk, settings, group=T.GROUP_IPHONE)
    assert ios_records and {r.provenance for r in ios_records} == {T.PROV_SCREENSHOT}
    chatter = next(r for r in ios_records if r.source.casefold() == "chatterbox")
    assert (chatter.label, chatter.label_basis, chatter.import_id) == ("worthwhile", T.BASIS_SOURCE_LABEL,
                                                                        iphone.import_id)

    # ------------------------------------------------------------------
    # [15] the same provenance reaches the HTML
    # ------------------------------------------------------------------
    overview = web.get("/?week=2026-09-13").get_data(as_text=True)
    assert "Diagnostic day 9 of 56" in flat(overview)
    # [15] is about provenance reaching the HTML, and since DR5.12 that HTML is
    # the detail page: weekly-totals, reflex, cost-per-keeper and funnels all
    # moved off the Overview together.
    html = web.get("/records?week=2026-09-13").get_data(as_text=True)
    week_rows = table_rows(section(html, "weekly-totals"))
    iphone_row = one_row(week_rows, "iPhone apps", "prov-screenshot")
    assert "lower confidence" in iphone_row and "787 min" in flat(iphone_row)
    assert "prov-api" in one_row(week_rows, "Podcasts")
    yt_row = one_row(week_rows, "YouTube", "prov-page")
    assert "lower confidence" not in yt_row and f"#import-{yt_import}" in yt_row and "1 unmeasured item" in flat(yt_row)
    desk_row = flat(one_row(week_rows, "Desktop web", "prov-export"))
    assert "40 min: already measured by YouTube history (account-level)" in desk_row
    reflex_html = section(html, "reflex")
    assert "142 min" in flat(reflex_html) and "prov-screenshot" in reflex_html
    x_keeper = one_row(table_rows(section(html, "cost-per-keeper")), ">X<")
    assert "133 min per keeper" in flat(x_keeper) and "prov-screenshot" in x_keeper and "lower confidence" in x_keeper
    vault_funnel = flat(section(html, "funnels").split('<div id="vault-funnel">')[1])
    assert "Kept 2" in vault_funnel and "outside the denominator: 2" in vault_funnel
    assert not re.search(r"\d\s*min\b", vault_funnel)                           # captures are never minutes

    records_html = web.get("/records?group=iphone_apps").get_data(as_text=True)
    # Scoped to the records table itself: the diagnostic detail below it (DR5.12)
    # is deliberately unfiltered, so its rows are not part of this count.
    records_table = records_html.split('<table id="records">')[1].split("</table>")[0]
    record_rows = table_rows(records_table)[1:]              # 39 minus X, which is its own group
    assert len(record_rows) == 38 and all("prov-screenshot" in r and ("lower confidence" in r or "unmeasured" in r)
                                          for r in record_rows)
    labels_html = web.get("/labels?kind=youtube_channel").get_data(as_text=True)
    assert "worthwhile" in flat(one_row(table_rows(labels_html), CHANNEL_A))

    # The queue's pre-dr-brainrot history is still exactly what it was.
    for table in ("episodes", "listening", "alerts_sent", "deadline_notices"):
        rows_now = [tuple(r) for r in conn.execute(
            f"SELECT {QUEUE_TABLES[table]} FROM {table} ORDER BY 1")][:len(before[table])]
        assert rows_now == [tuple(r) for r in before[table]], table
    assert [tuple(r) for r in conn.execute(
        f"SELECT {QUEUE_TABLES['weeks']} FROM weeks WHERE week_start IN (?, ?) ORDER BY 1",
        (OLDER_WEEK, LEGACY_WEEK))] == [tuple(r) for r in before["weeks"]]
    # The only dr-brainrot addition to those rows: an overage recorded against their own 180 (neither went over).
    assert [tuple(r) for r in conn.execute(
        "SELECT week_start, overage_min FROM weeks WHERE week_start IN (?, ?) ORDER BY 1",
        (OLDER_WEEK, LEGACY_WEEK))] == [(OLDER_WEEK, 0), (LEGACY_WEEK, 0)]
    assert [tuple(r) for r in conn.execute("SELECT * FROM queue WHERE id IN (1, 2) ORDER BY id")] == \
        [tuple(r) for r in before["queue"]]
    assert real_writer or not OBSIDIAN_INGEST.exists()
    conn.close()
