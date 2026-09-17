"""Tests for bot.py — Phase 2, Agent C.

Two hard rules govern this file:

* **No live Telegram API call, ever.** Handlers are driven directly with fake
  Update/Context objects. The only thing that touches `telegram.ext` is the
  `build_app` test, and `Application.builder().build()` opens no socket.
* **No import of the real db.py or spotify.py.** Both are being written
  concurrently. Everything they provide is faked here against the
  CONTRACTS.md signatures.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
from dataclasses import dataclass, replace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import clock  # noqa: E402
import config  # noqa: E402

AUTHORISED = 1
INTRUDER = 999

LINK = "https://open.spotify.com/episode/4rOoJ6Egrf8K2IrywzwOMk?si=abc123"
URI = "spotify:episode:4rOoJ6Egrf8K2IrywzwOMk"
EPISODE_ID = "4rOoJ6Egrf8K2IrywzwOMk"

NOW = "2026-08-30T09:00:00+00:00"  # a Sunday


# ==========================================================================
# Doubles for spotify.py (CONTRACTS.md §3)
# ==========================================================================

class InvalidEpisodeRef(ValueError):
    """Message is user-facing, as in the real module."""


class SpotifyError(Exception):
    pass


@dataclass(frozen=True)
class FakeEpisode:
    spotify_id: str
    title: str
    show: str
    description: str | None
    release_date: str | None
    duration_ms: int
    resume_position_ms: int = 0
    fully_played: bool = False


EPISODE = FakeEpisode(
    spotify_id=EPISODE_ID,
    title="The Long One",
    show="Deep Dive FM",
    description="A description.",
    release_date="2026-08-01",
    duration_ms=107 * 60_000,  # 1h 47m
)

OTHER_ID = "1aBcDeFgHiJkLmNoPqRsTu"
OTHER_EPISODE = replace(EPISODE, spotify_id=OTHER_ID, title="The Short One", duration_ms=43 * 60_000)


class FakeSpotify:
    """Same surface as SpotifyClient, plus the module-level ref parser."""

    def __init__(self, episodes: dict[str, FakeEpisode], *, fail: Exception | None = None):
        self.episodes = episodes
        self.fail = fail
        self.fetched: list[str] = []

    @staticmethod
    def parse_episode_ref(text: str) -> str:
        text = (text or "").strip()
        if text.startswith("spotify:"):
            kind, _, rest = text[len("spotify:"):].partition(":")
            if kind != "episode":
                raise InvalidEpisodeRef(f"That is a Spotify {kind}, not an episode.")
            if not bot.re.fullmatch(r"[A-Za-z0-9]{22}", rest):
                raise InvalidEpisodeRef("That does not look like a Spotify episode id.")
            return rest
        if "open.spotify.com" in text:
            path = text.split("open.spotify.com", 1)[1].split("?", 1)[0]
            parts = [p for p in path.split("/") if p and not p.startswith("intl-")]
            if len(parts) != 2 or parts[0] != "episode":
                kind = parts[0] if parts else "link"
                raise InvalidEpisodeRef(f"That is a Spotify {kind}, not an episode.")
            if not bot.re.fullmatch(r"[A-Za-z0-9]{22}", parts[1]):
                raise InvalidEpisodeRef("That does not look like a Spotify episode id.")
            return parts[1]
        raise InvalidEpisodeRef("Send me a Spotify episode link.")

    def get_episode(self, spotify_id: str) -> FakeEpisode:
        if self.fail is not None:
            raise self.fail
        self.fetched.append(spotify_id)
        return self.episodes[spotify_id]


# ==========================================================================
# Doubles for db.py (CONTRACTS.md §2)
# ==========================================================================

class IllegalTransition(Exception):
    pass


class AllowanceExceeded(Exception):
    def __init__(self, message: str, overage_min: int, remaining_min: int, cost_min: int):
        super().__init__(message)
        self.overage_min = overage_min
        self.remaining_min = remaining_min
        self.cost_min = cost_min


@dataclass(frozen=True)
class FakeItem:
    id: int
    spotify_id: str
    title: str
    show: str
    duration_ms: int
    why_note: str
    captured_at: str
    deadline: str | None
    state: str
    cycles_seen: int = 0
    still_holds: int | None = None
    promoted_at: str | None = None
    resolved_at: str | None = None


class FakeDB:
    """Records every write. `calls` is the assertion surface for "nothing
    was created"."""

    IllegalTransition = IllegalTransition
    AllowanceExceeded = AllowanceExceeded

    def __init__(self, *, items: dict[int, FakeItem] | None = None, promote_raises: Exception | None = None):
        self.calls: list[tuple] = []
        self.items = items or {}
        self.promote_raises = promote_raises
        self._next_id = max(self.items, default=0) + 1
        self.triage: list[FakeItem] = []

    # --- capture ---
    def upsert_episode(self, conn, spotify_id, title, show, description, release_date, duration_ms):
        self.calls.append(("upsert_episode", spotify_id))

    def capture(self, conn, clk, settings, *, spotify_id, why_note, deadline=None):
        if not (why_note or "").strip():
            raise ValueError("why_note is required")
        self.calls.append(("capture", spotify_id, why_note, deadline))
        ep = EPISODE if spotify_id == EPISODE_ID else OTHER_EPISODE
        item = FakeItem(
            id=self._next_id, spotify_id=spotify_id, title=ep.title, show=ep.show,
            duration_ms=ep.duration_ms, why_note=why_note, captured_at=clock.iso(clk.now()),
            deadline=deadline, state="locked",
        )
        self._next_id += 1
        self.items[item.id] = item
        return item

    def record_listening(self, conn, clk, spotify_id, position_ms, delta_ms, fully_played):
        """The capture path seeds a zero-delta baseline when the episode is
        already part heard, so the first poll does not credit listening that
        happened before the item existed."""
        self.calls.append(("record_listening", spotify_id, position_ms, delta_ms))

    # --- reading ---
    def get_item(self, conn, item_id):
        item = self.items.get(item_id)
        # Soft-deleted rows are invisible to every default query.
        if item is None or item.state in ("dropped", "expired"):
            return None
        return item

    def triage_items(self, conn, clk, settings):
        return list(self.triage)

    # --- state ---
    def set_state(self, conn, clk, item_id, new_state):
        self.calls.append(("set_state", item_id, new_state))
        self.items[item_id] = replace(self.items[item_id], state=new_state)
        return self.items[item_id]

    def record_still_holds(self, conn, item_id, answer):
        self.calls.append(("record_still_holds", item_id, answer))
        self.items[item_id] = replace(self.items[item_id], still_holds=int(answer))
        return self.items[item_id]

    def promote(self, conn, clk, settings, item_id):
        if self.promote_raises is not None:
            raise self.promote_raises
        self.calls.append(("promote", item_id))
        self.items[item_id] = replace(self.items[item_id], state="promoted")
        return self.items[item_id]

    # --- convenience ---
    def writes(self, name):
        return [c for c in self.calls if c[0] == name]


# ==========================================================================
# Fake Update / Context
# ==========================================================================

class FakeUser:
    def __init__(self, uid: int):
        self.id = uid


class FakeMessage:
    def __init__(self, text: str | None = None):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return FakeMessage(text)


class FakeCallbackQuery:
    def __init__(self, data: str, uid: int = AUTHORISED):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage("rendered item")
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))


class FakeUpdate:
    def __init__(self, *, text=None, uid=AUTHORISED, callback=None):
        self.effective_user = FakeUser(uid) if callback is None else callback.from_user
        self.message = FakeMessage(text) if callback is None else None
        self.effective_message = self.message
        self.callback_query = callback


class FakeContext:
    def __init__(self, bot_data: dict):
        self.bot_data = bot_data
        self.user_data: dict = {}
        self.chat_data: dict = {}


def run(coro):
    return asyncio.run(coro)


# ==========================================================================
# Fixtures
# ==========================================================================

@pytest.fixture
def settings():
    return config.test_settings(telegram_user_id=AUTHORISED)


@pytest.fixture
def clk():
    return clock.FrozenClock(NOW)


@pytest.fixture
def spotify():
    return FakeSpotify({EPISODE_ID: EPISODE, OTHER_ID: OTHER_EPISODE})


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def label_conn():
    """DRBR: a real, empty, migrated schema for the label tables only.

    Queue writes still go to FakeDB. The capture path now resolves the item's
    label and records its source (`labels`, `allowance`), which needs the
    `sources`/`item_labels` tables; before DRBR this fixture passed `object()`.
    Built from the SQL files directly, so this module still imports no db.py.
    """
    import sqlite3

    root = pathlib.Path(__file__).resolve().parents[1]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((root / "schema.sql").read_text())
    for migration in sorted((root / "migrations").glob("*.sql")):
        conn.executescript(migration.read_text())
    yield conn
    conn.close()


@pytest.fixture
def ctx(settings, clk, spotify, fake_db, label_conn):
    return FakeContext({
        "settings": settings, "conn": label_conn, "clk": clk,
        "spotify": spotify, "db": fake_db,
    })


# ==========================================================================
# 1. Authorisation — silence, absolute
# ==========================================================================

def test_unauthorised_message_gets_zero_replies(ctx, fake_db, spotify):
    update = FakeUpdate(text=LINK, uid=INTRUDER)
    run(bot.on_message(update, ctx))
    assert update.message.replies == []
    assert fake_db.calls == []
    assert spotify.fetched == []
    assert ctx.user_data == {}


@pytest.mark.parametrize("handler", [bot.cmd_start, bot.cmd_queue, bot.cmd_stats])
def test_unauthorised_commands_get_zero_replies(ctx, handler, fake_db):
    update = FakeUpdate(text="/start", uid=INTRUDER)
    run(handler(update, ctx))
    assert update.message.replies == []
    assert fake_db.calls == []


def test_unauthorised_callback_gets_zero_answers(ctx, fake_db):
    fake_db.items[7] = _item(7, "queued")
    query = FakeCallbackQuery("promote:7", uid=INTRUDER)
    run(bot.on_callback(FakeUpdate(callback=query), ctx))
    assert query.answers == []
    assert fake_db.calls == []


def test_authorised_only_is_a_decorator_and_passes_the_owner_through(ctx):
    seen = []

    @bot.authorised_only
    async def handler(update, context):
        seen.append(update.effective_user.id)

    run(handler(FakeUpdate(text="x", uid=AUTHORISED), ctx))
    run(handler(FakeUpdate(text="x", uid=INTRUDER), ctx))
    assert seen == [AUTHORISED]


# ==========================================================================
# 2. Capture — nothing is written until the why-note arrives
# ==========================================================================

@pytest.mark.parametrize("bad", [
    "https://open.spotify.com/track/4rOoJ6Egrf8K2IrywzwOMk",
    "spotify:show:4rOoJ6Egrf8K2IrywzwOMk",
    "https://open.spotify.com/episode/tooshort",
    "just some chat",
])
def test_malformed_link_is_rejected_clearly_and_creates_nothing(ctx, fake_db, bad):
    update = FakeUpdate(text=bad)
    run(bot.on_message(update, ctx))
    assert len(update.message.replies) == 1
    assert update.message.replies[0].strip()          # a clear message, not silence
    assert fake_db.calls == []                        # nothing created
    assert bot.PENDING_KEY not in ctx.user_data


def test_link_asks_why_and_writes_nothing(ctx, fake_db):
    update = FakeUpdate(text=LINK)
    run(bot.on_message(update, ctx))

    reply = update.message.replies[0]
    assert "Why?" in reply
    assert "The Long One" in reply and "Deep Dive FM" in reply and "1h 47m" in reply
    assert ctx.user_data[bot.PENDING_KEY]["spotify_id"] == EPISODE_ID
    assert fake_db.calls == []                        # THE point: no row yet


def test_abandoning_mid_flow_creates_nothing(ctx, fake_db):
    run(bot.on_message(FakeUpdate(text=LINK), ctx))
    # ... user never replies. Session ends here.
    assert fake_db.calls == []
    assert fake_db.items == {}


def test_full_capture_writes_only_after_the_note(ctx, fake_db, clk):
    run(bot.on_message(FakeUpdate(text=LINK), ctx))
    assert fake_db.calls == []

    note = FakeUpdate(text="Guest builds the exact thing I'm stuck on")
    run(bot.on_message(note, ctx))

    assert [c[0] for c in fake_db.calls] == ["upsert_episode", "capture"]
    _, sid, why, deadline = fake_db.writes("capture")[0]
    assert sid == EPISODE_ID
    assert why == "Guest builds the exact thing I'm stuck on"
    assert deadline is None
    assert bot.PENDING_KEY not in ctx.user_data       # conversation closed


def test_confirmation_shows_title_show_and_duration(ctx):
    run(bot.on_message(FakeUpdate(text=LINK), ctx))
    note = FakeUpdate(text="worth it")
    run(bot.on_message(note, ctx))

    reply = note.message.replies[0]
    assert "The Long One" in reply
    assert "Deep Dive FM" in reply
    assert "1h 47m" in reply                          # the time cost, at capture
    assert "worth it" in reply
    assert "Locked until" in reply


def test_second_link_replaces_the_pending_capture(ctx, fake_db):
    run(bot.on_message(FakeUpdate(text=LINK), ctx))
    run(bot.on_message(FakeUpdate(text=f"spotify:episode:{OTHER_ID}"), ctx))
    assert ctx.user_data[bot.PENDING_KEY]["spotify_id"] == OTHER_ID
    assert fake_db.calls == []

    run(bot.on_message(FakeUpdate(text="the short one instead"), ctx))
    assert fake_db.writes("capture")[0][1] == OTHER_ID
    assert len(fake_db.writes("capture")) == 1        # not queued behind


def test_bad_link_mid_flow_is_rejected_not_taken_as_the_note(ctx, fake_db):
    run(bot.on_message(FakeUpdate(text=LINK), ctx))
    bad = FakeUpdate(text="https://open.spotify.com/track/4rOoJ6Egrf8K2IrywzwOMk")
    run(bot.on_message(bad, ctx))
    assert "track" in bad.message.replies[0]
    assert fake_db.calls == []
    assert ctx.user_data[bot.PENDING_KEY]["spotify_id"] == EPISODE_ID


def test_a_bare_deadline_is_not_a_note(ctx, fake_db):
    run(bot.on_message(FakeUpdate(text=LINK), ctx))
    note = FakeUpdate(text="by friday")
    run(bot.on_message(note, ctx))
    assert fake_db.calls == []                        # still nothing
    assert "reason" in note.message.replies[0].lower()
    assert bot.PENDING_KEY in ctx.user_data           # still pending


def test_spotify_failure_creates_nothing(settings, clk, fake_db):
    ctx = FakeContext({
        "settings": settings, "conn": object(), "clk": clk,
        "spotify": FakeSpotify({}, fail=SpotifyError("boom")), "db": fake_db,
    })
    update = FakeUpdate(text=LINK)
    run(bot.on_message(update, ctx))
    assert len(update.message.replies) == 1
    assert fake_db.calls == []
    assert bot.PENDING_KEY not in ctx.user_data


def test_capture_carries_the_deadline_through(ctx, fake_db):
    run(bot.on_message(FakeUpdate(text=LINK), ctx))
    run(bot.on_message(FakeUpdate(text="prep for the interview by 3 sep"), ctx))
    _, _, why, deadline = fake_db.writes("capture")[0]
    assert why == "prep for the interview"
    assert deadline is not None and deadline.startswith("2026-09-03T17:00")


# ==========================================================================
# 3. Deadline parsing — the negatives matter most
# ==========================================================================

@pytest.mark.parametrize("text", [
    "by the way this looked good",
    "recommended by a friend",
    "looked good",
    "the interview before the funding round",
    "by",
    "before bed",
    "by popular demand",
    "read by may",                       # bare month: not a date form
    "by 2026-13-40",                     # not a real date
    "finish by friday afternoon",        # trailing junk after the day
    "by 3",                              # a day with no month
])
def test_unrecognised_trailers_never_eat_the_users_words(clk, text):
    note, deadline = bot.parse_deadline(text, clk, "Europe/Lisbon")
    assert deadline is None
    assert note == text.strip()


@pytest.mark.parametrize("trailer,expect_date", [
    ("by friday", "2026-09-04"),         # NOW is Sunday 30 Aug 2026
    ("before friday", "2026-09-04"),
    ("by sunday", "2026-08-30"),         # today, 18:00 local still ahead
    ("by tomorrow", "2026-08-31"),
    ("by today", "2026-08-30"),
    ("by 3 sep", "2026-09-03"),
    ("by 3rd sep", "2026-09-03"),
    ("by sep 3", "2026-09-03"),
    ("by 3 september", "2026-09-03"),
    ("by 2026-09-03", "2026-09-03"),
    ("by 1 feb", "2027-02-01"),          # rolls into next year
])
def test_recognised_deadline_forms(clk, trailer, expect_date):
    note, deadline = bot.parse_deadline(f"listen to this {trailer}", clk, "Europe/Lisbon")
    assert note == "listen to this"
    assert deadline is not None
    local = clock.parse(deadline).astimezone(__import__("zoneinfo").ZoneInfo("Europe/Lisbon"))
    assert local.strftime("%Y-%m-%d") == expect_date
    assert (local.hour, local.minute) == (18, 0)      # 18:00 local on the day


def test_deadline_anchors_on_the_last_by(clk):
    note, deadline = bot.parse_deadline("recommended by a friend by friday", clk, "Europe/Lisbon")
    assert note == "recommended by a friend"
    assert deadline is not None


def test_deadline_is_stored_as_utc_iso(clk):
    _, deadline = bot.parse_deadline("x by 2026-09-03", clk, "Europe/Lisbon")
    assert deadline.endswith("+00:00")
    assert clock.parse(deadline) is not None


def test_weekday_today_after_six_rolls_forward():
    late = clock.FrozenClock("2026-08-30T19:00:00+00:00")  # Sunday, 20:00 Lisbon
    _, deadline = bot.parse_deadline("x by sunday", late, "Europe/Lisbon")
    local = clock.parse(deadline).astimezone(__import__("zoneinfo").ZoneInfo("Europe/Lisbon"))
    assert local.strftime("%Y-%m-%d") == "2026-09-06"


def test_note_with_no_trailer_is_returned_untouched(clk):
    note, deadline = bot.parse_deadline("  spaced out  ", clk, "Europe/Lisbon")
    assert (note, deadline) == ("spaced out", None)


# ==========================================================================
# 4. Inline buttons
# ==========================================================================

def _item(item_id: int, state: str, **kw) -> FakeItem:
    base = dict(
        id=item_id, spotify_id=EPISODE_ID, title=EPISODE.title, show=EPISODE.show,
        duration_ms=EPISODE.duration_ms, why_note="looked good",
        captured_at=NOW, deadline=None, state=state,
    )
    base.update(kw)
    return FakeItem(**base)


def _press(ctx, data, uid=AUTHORISED):
    query = FakeCallbackQuery(data, uid=uid)
    run(bot.on_callback(FakeUpdate(callback=query), ctx))
    return query


def test_promote_button_promotes(ctx, fake_db):
    fake_db.items[3] = _item(3, "queued")
    query = _press(ctx, "promote:3")
    assert fake_db.writes("promote") == [("promote", 3)]
    assert fake_db.items[3].state == "promoted"
    assert query.answers[0][0] == "Promoted."


def test_gate_refusal_does_not_promote_and_reports_the_overage(settings, clk, spotify):
    fake_db = FakeDB(promote_raises=AllowanceExceeded("nope", overage_min=25, remaining_min=40, cost_min=65))
    fake_db.items[3] = _item(3, "queued")
    ctx = FakeContext({"settings": settings, "conn": object(), "clk": clk, "spotify": spotify, "db": fake_db})

    query = _press(ctx, "promote:3")

    assert fake_db.writes("promote") == []            # refused, no state change
    assert fake_db.items[3].state == "queued"
    text, alert = query.answers[0]
    assert text == "That would put you 25 min over. 40 min left this week."
    assert alert is True


def test_drop_button_soft_deletes(ctx, fake_db):
    fake_db.items[4] = _item(4, "queued")
    query = _press(ctx, "drop:4")
    assert fake_db.writes("set_state") == [("set_state", 4, "dropped")]
    assert query.answers[0][0] == "Dropped."


@pytest.mark.parametrize("action,expected", [("holds_yes", True), ("holds_no", False)])
def test_still_holds_is_recorded_independently(ctx, fake_db, action, expected):
    fake_db.items[5] = _item(5, "queued")
    query = _press(ctx, f"{action}:5")

    assert fake_db.writes("record_still_holds") == [("record_still_holds", 5, expected)]
    # never inferred from, and never inferring, promote/drop
    assert fake_db.writes("promote") == []
    assert fake_db.writes("set_state") == []
    assert fake_db.items[5].state == "queued"
    assert query.answers[0][0].startswith("Noted")


def test_still_holds_no_does_not_drop(ctx, fake_db):
    fake_db.items[5] = _item(5, "queued")
    _press(ctx, "holds_no:5")
    assert fake_db.items[5].state == "queued"
    assert fake_db.items[5].still_holds == 0


def test_promote_then_still_holds_are_both_recorded(ctx, fake_db):
    fake_db.items[6] = _item(6, "queued")
    _press(ctx, "promote:6")
    _press(ctx, "holds_yes:6")
    assert fake_db.writes("promote") == [("promote", 6)]
    assert fake_db.writes("record_still_holds") == [("record_still_holds", 6, True)]


@pytest.mark.parametrize("state", ["dropped", "expired", "played"])
@pytest.mark.parametrize("action", ["promote", "drop", "holds_yes", "holds_no"])
def test_callback_on_a_terminal_item_answers_already_resolved(ctx, fake_db, state, action):
    fake_db.items[8] = _item(8, state)
    query = _press(ctx, f"{action}:8")
    assert query.answers == [(bot.ALREADY_RESOLVED, False)]
    assert fake_db.calls == []                        # and raises nothing


def test_callback_on_an_unknown_item_answers_already_resolved(ctx, fake_db):
    query = _press(ctx, "promote:404")
    assert query.answers == [(bot.ALREADY_RESOLVED, False)]
    assert fake_db.calls == []


def test_double_promote_does_not_call_promote_twice(ctx, fake_db):
    fake_db.items[9] = _item(9, "queued")
    _press(ctx, "promote:9")
    query = _press(ctx, "promote:9")
    assert len(fake_db.writes("promote")) == 1
    assert query.answers[0][0] == "Already promoted."


@pytest.mark.parametrize("data", ["", "nonsense", "promote:", "explode:3", "promote:abc"])
def test_malformed_callback_data_is_answered_not_raised(ctx, fake_db, data):
    query = _press(ctx, data)
    assert query.answers == [("Unrecognised button.", False)]
    assert fake_db.calls == []


# ==========================================================================
# 5. Commands
# ==========================================================================

def test_start_explains_in_one_message(ctx):
    update = FakeUpdate(text="/start")
    run(bot.cmd_start(update, ctx))
    assert len(update.message.replies) == 1
    assert "link" in update.message.replies[0].lower()


def test_queue_lists_unlocked_items_with_note_and_duration(ctx, fake_db):
    fake_db.triage = [_item(1, "queued"), _item(2, "queued", why_note="for the flight")]
    update = FakeUpdate(text="/queue")
    run(bot.cmd_queue(update, ctx))
    reply = update.message.replies[0]
    assert "1h 47m" in reply
    assert "looked good" in reply and "for the flight" in reply


def test_queue_when_empty(ctx):
    update = FakeUpdate(text="/queue")
    run(bot.cmd_queue(update, ctx))
    assert "Nothing unlocked" in update.message.replies[0]


def test_stats_delegates_to_the_injected_renderer(ctx):
    seen = []

    def render_stats(conn, clk, settings):
        seen.append((conn, clk, settings))
        return "captured 10 · survived 6 · promoted 3"

    ctx.bot_data["render_stats"] = render_stats
    update = FakeUpdate(text="/stats")
    run(bot.cmd_stats(update, ctx))

    assert len(seen) == 1
    assert update.message.replies == ["captured 10 · survived 6 · promoted 3"]


def test_stats_degrades_gracefully_when_triage_is_absent(ctx, monkeypatch):
    # Phase 3 has not landed yet: bot.py must not hard-fail.
    monkeypatch.setitem(sys.modules, "triage", None)
    update = FakeUpdate(text="/stats")
    run(bot.cmd_stats(update, ctx))
    assert len(update.message.replies) == 1
    assert "not wired up" in update.message.replies[0]


# ==========================================================================
# 6. Wiring — offline only
# ==========================================================================

def test_build_app_registers_handlers_without_touching_the_network(settings, clk, spotify):
    conn = object()
    app = bot.build_app(settings, conn, clk, spotify)

    assert app.bot_data["settings"] is settings
    assert app.bot_data["conn"] is conn
    assert app.bot_data["clk"] is clk
    assert app.bot_data["spotify"] is spotify

    registered = [h for group in app.handlers.values() for h in group]
    pop_handlers = [h for h in registered if getattr(h.callback, "__module__", "") == "bot"]
    assert len(pop_handlers) == 5
    callbacks = {h.callback for h in registered}
    assert {bot.cmd_start, bot.cmd_queue, bot.cmd_stats, bot.on_callback, bot.on_message} <= callbacks


# ==========================================================================
# 9. The error handler
#
# Without one, PTB logs the traceback and drops the update. From the user's
# side the bot just ignores them — indistinguishable from the deliberate
# silence an unauthorised sender gets. That ambiguity is what this closes.
# ==========================================================================

def _boom(message="kaboom"):
    try:
        raise RuntimeError(message)
    except RuntimeError as e:
        return e


def test_error_handler_tells_the_authorised_user_it_failed(ctx):
    update = FakeUpdate(text="anything")
    ctx.error = _boom()
    run(bot.on_error(update, ctx))
    assert update.message.replies == [bot.GENERIC_FAILURE]


def test_error_handler_answers_a_callback_with_an_alert(ctx):
    query = FakeCallbackQuery("promote:7")
    ctx.error = _boom()
    run(bot.on_error(FakeUpdate(callback=query), ctx))
    assert query.answers == [(bot.GENERIC_FAILURE, True)]


def test_error_handler_stays_silent_for_an_unauthorised_user(ctx):
    """The silent-rejection rule survives even when their update is what crashed."""
    update = FakeUpdate(text="anything", uid=INTRUDER)
    ctx.error = _boom()
    run(bot.on_error(update, ctx))
    assert update.message.replies == []


def test_error_handler_stays_silent_for_an_unauthorised_callback(ctx):
    query = FakeCallbackQuery("promote:7", uid=INTRUDER)
    ctx.error = _boom()
    run(bot.on_error(FakeUpdate(callback=query), ctx))
    assert query.answers == []


def test_the_user_never_sees_the_exception_text(ctx):
    """Exception messages can carry request URLs, and those carry the token."""
    update = FakeUpdate(text="anything")
    ctx.error = _boom("https://api.telegram.org/bot123:SECRET/getUpdates failed")
    run(bot.on_error(update, ctx))
    assert update.message.replies == [bot.GENERIC_FAILURE]
    assert "SECRET" not in update.message.replies[0]
    assert "api.telegram.org" not in update.message.replies[0]


def test_the_logged_traceback_has_the_token_scrubbed(ctx, settings, caplog):
    """D5.5 closed this on the happy path; the error path must not reopen it."""
    token = settings.telegram_bot_token
    ctx.error = _boom(f"POST https://api.telegram.org/bot{token}/getUpdates blew up")
    with caplog.at_level(logging.ERROR, logger="pop.bot"):
        run(bot.on_error(FakeUpdate(text="x"), ctx))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in logged
    assert "<TOKEN>" in logged


def test_error_handler_does_not_raise_when_replying_also_fails(ctx):
    """A failure inside the error handler would be unhandleable."""
    update = FakeUpdate(text="anything")

    async def explode(*a, **k):
        raise ConnectionError("telegram unreachable")

    update.message.reply_text = explode
    ctx.error = _boom()
    run(bot.on_error(update, ctx))  # must simply return


def test_error_handler_survives_an_update_that_is_not_an_update(ctx):
    """PTB passes `object()` when the failure had no update attached."""
    ctx.error = _boom()
    run(bot.on_error(object(), ctx))


def test_error_handler_survives_no_exception_attached(ctx):
    ctx.error = None
    run(bot.on_error(FakeUpdate(text="x"), ctx))


def test_build_app_registers_the_error_handler(settings, clk, spotify):
    app = bot.build_app(settings, object(), clk, spotify)
    assert bot.on_error in app.error_handlers


# ==========================================================================
# 10. DRBR — YouTube capture and the label override (Part II §7)
#
# These drive the REAL db.py against a migrated in-memory database: db.py
# exists now, and the properties under test (nothing written until the note,
# the override stored against the right item, the label read back) live in
# the database, not in a double. The YouTube client is always a fake.
# ==========================================================================

import drbr_types as T  # noqa: E402

VIDEO_ID = "dQw4w9WgXcQ"
CHANNEL_ID = "UCuAXFkgsw1L7xaCfnd5JJOw"
VIDEO = T.VideoInfo(VIDEO_ID, "A Talk Worth Hearing", CHANNEL_ID, "Talks Channel", 25 * 60)
LIVE = T.VideoInfo("LiveStream1", "Live now", CHANNEL_ID, "Talks Channel", None)


class FakeYouTube:
    """`video_info(video_id) -> VideoInfo | None`, raising VideoLookupError on `fail`."""

    def __init__(self, videos: dict | None = None, *, fail: Exception | None = None):
        self.videos = videos if videos is not None else {VIDEO_ID: VIDEO, LIVE.video_id: LIVE}
        self.fail = fail
        self.asked: list[str] = []

    def video_info(self, video_id: str):
        self.asked.append(video_id)
        if self.fail is not None:
            raise self.fail
        return self.videos.get(video_id)


@pytest.fixture
def real_conn(settings):
    import db  # noqa: PLC0415

    conn = db.connect(settings)
    db.init_db(conn)
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def youtube():
    return FakeYouTube()


@pytest.fixture
def real_ctx(settings, clk, spotify, real_conn, youtube):
    return FakeContext({
        "settings": settings, "conn": real_conn, "clk": clk,
        "spotify": spotify, "youtube": youtube,
    })


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.mark.parametrize("text", [
    f"https://www.youtube.com/watch?v={VIDEO_ID}",
    f"https://youtube.com/watch?feature=shared&v={VIDEO_ID}&t=42",
    f"https://m.youtube.com/watch?v={VIDEO_ID}",
    f"https://youtu.be/{VIDEO_ID}?si=abc",
    f"https://www.youtube.com/shorts/{VIDEO_ID}",
    f"worth a look https://youtu.be/{VIDEO_ID} later",
])
def test_youtube_links_parse_to_the_strict_video_id(text):
    assert bot.parse_youtube_ref(text) == VIDEO_ID


@pytest.mark.parametrize("text", [
    "https://www.youtube.com/watch?v=tooShort1",                 # 9 chars
    f"https://www.youtube.com/watch?v={VIDEO_ID}X",               # 12 chars
    "https://www.youtube.com/@SomeChannel",
    "https://www.youtube.com/playlist?list=PLabcdefghijk",
    "https://youtu.be/",
])
def test_non_video_youtube_links_do_not_parse(text):
    assert bot.parse_youtube_ref(text) is None


def test_youtube_capture_happy_path_writes_only_after_the_note(real_ctx, real_conn, youtube):
    link = FakeUpdate(text=f"https://youtu.be/{VIDEO_ID}")
    run(bot.on_message(link, real_ctx))

    reply = link.message.replies[0]
    assert "A Talk Worth Hearing" in reply and "Talks Channel" in reply and "25m" in reply
    assert "Why?" in reply
    assert youtube.asked == [VIDEO_ID]
    assert real_ctx.user_data[bot.PENDING_KEY]["spotify_id"] == f"youtube:{VIDEO_ID}"
    assert _count(real_conn, "episodes") == 0 and _count(real_conn, "queue") == 0

    note = FakeUpdate(text="the speaker answers my exact question")
    run(bot.on_message(note, real_ctx))

    ep = real_conn.execute("SELECT * FROM episodes").fetchone()
    assert (ep["spotify_id"], ep["platform"], ep["source_key"], ep["show"], ep["duration_ms"]) == (
        f"youtube:{VIDEO_ID}", "youtube", CHANNEL_ID, "Talks Channel", 25 * 60_000)
    q = real_conn.execute("SELECT * FROM queue").fetchone()
    assert (q["state"], q["why_note"]) == ("locked", "the speaker answers my exact question")

    confirmation = note.message.replies[0]
    assert "Label: wasteful (source not labelled yet, so wasteful by default)" in confirmation
    assert "Locked until" in confirmation
    source = real_conn.execute("SELECT * FROM sources WHERE kind = 'youtube_channel'").fetchone()
    assert (source["source_key"], source["label"]) == (CHANNEL_ID, None)   # identity, not a label
    assert _count(real_conn, "item_labels") == 0
    assert _count(real_conn, "listening") == 0                              # no Spotify baseline
    assert bot.PENDING_KEY not in real_ctx.user_data


@pytest.mark.parametrize("fake,expected", [
    (FakeYouTube(), bot.YOUTUBE_NO_DURATION),
    (FakeYouTube(fail=T.VideoLookupError("quota")), bot.YOUTUBE_UNAVAILABLE),
])
def test_youtube_capture_refused_without_a_known_duration(settings, clk, spotify, real_conn, fake, expected):
    ctx = FakeContext({"settings": settings, "conn": real_conn, "clk": clk, "spotify": spotify,
                       "youtube": fake})
    update = FakeUpdate(text="https://www.youtube.com/watch?v=LiveStream1")
    run(bot.on_message(update, ctx))
    assert update.message.replies == [expected]
    assert bot.PENDING_KEY not in ctx.user_data
    assert _count(real_conn, "episodes") == 0 and _count(real_conn, "queue") == 0


def test_a_refused_youtube_link_mid_flow_keeps_the_pending_capture(real_ctx, real_conn):
    run(bot.on_message(FakeUpdate(text=LINK), real_ctx))
    bad = FakeUpdate(text="https://youtu.be/LiveStream1")
    run(bot.on_message(bad, real_ctx))
    assert bad.message.replies == [bot.YOUTUBE_NO_DURATION]              # not taken as the note
    assert real_ctx.user_data[bot.PENDING_KEY]["spotify_id"] == EPISODE_ID
    assert _count(real_conn, "queue") == 0


@pytest.mark.parametrize("bot_data_youtube,text,expected", [
    (FakeYouTube(videos={}), f"https://youtu.be/{VIDEO_ID}", bot.YOUTUBE_NO_SUCH_VIDEO),
    (None, f"https://youtu.be/{VIDEO_ID}", bot.YOUTUBE_NOT_CONFIGURED),
    (FakeYouTube(), "https://www.youtube.com/@SomeChannel", bot.YOUTUBE_NOT_A_VIDEO),
])
def test_other_youtube_failures_reply_and_write_nothing(settings, clk, spotify, real_conn,
                                                        bot_data_youtube, text, expected):
    data = {"settings": settings, "conn": real_conn, "clk": clk, "spotify": spotify}
    if bot_data_youtube is not None:
        data["youtube"] = bot_data_youtube
    ctx = FakeContext(data)
    update = FakeUpdate(text=text)
    run(bot.on_message(update, ctx))
    assert update.message.replies == [expected]
    assert _count(real_conn, "episodes") == 0
    assert bot.PENDING_KEY not in ctx.user_data


def test_override_token_is_stored_and_confirmed(real_ctx, real_conn):
    run(bot.on_message(FakeUpdate(text=f"https://youtu.be/{VIDEO_ID}"), real_ctx))
    note = FakeUpdate(text="#worthwhile for the talk prep")
    run(bot.on_message(note, real_ctx))

    q = real_conn.execute("SELECT why_note FROM queue").fetchone()
    assert q["why_note"] == "for the talk prep"                       # the tag is not the reason
    row = real_conn.execute("SELECT * FROM item_labels").fetchone()
    assert (row["item_ref"], row["label"], row["origin"]) == (f"youtube:{VIDEO_ID}", "worthwhile", "capture")
    reply = note.message.replies[0]
    assert "Label: worthwhile (your tag in this note)" in reply
    assert "Costs 0 min" in reply
    assert "Locked until" in reply                                    # worthwhile is still locked


@pytest.mark.parametrize("text", [
    "prep for the interview #worth by friday",
    "prep for the interview by friday #worth",
])
def test_deadline_and_token_together(real_ctx, real_conn, text):
    run(bot.on_message(FakeUpdate(text=f"https://youtu.be/{VIDEO_ID}"), real_ctx))
    run(bot.on_message(FakeUpdate(text=text), real_ctx))
    q = real_conn.execute("SELECT why_note, deadline FROM queue").fetchone()
    assert q["why_note"] == "prep for the interview"
    assert q["deadline"].startswith("2026-09-04T17:00")               # Friday 18:00 Lisbon
    assert real_conn.execute("SELECT label FROM item_labels").fetchone()["label"] == "worthwhile"


def test_a_bare_token_is_not_a_reason(real_ctx, real_conn):
    run(bot.on_message(FakeUpdate(text=f"https://youtu.be/{VIDEO_ID}"), real_ctx))
    note = FakeUpdate(text="#worthwhile")
    run(bot.on_message(note, real_ctx))
    assert "reason" in note.message.replies[0].lower()
    assert _count(real_conn, "queue") == 0 and _count(real_conn, "item_labels") == 0
    assert bot.PENDING_KEY in real_ctx.user_data


def test_confirmation_names_a_source_label(real_ctx, real_conn, clk):
    import labels  # noqa: PLC0415

    source = labels.ensure_source(real_conn, clk, T.KIND_YOUTUBE_CHANNEL, CHANNEL_ID, "Talks Channel")
    labels.set_source_label(real_conn, clk, source.id, T.WORTHWHILE)
    run(bot.on_message(FakeUpdate(text=f"https://youtu.be/{VIDEO_ID}"), real_ctx))
    note = FakeUpdate(text="good channel")
    run(bot.on_message(note, real_ctx))
    assert "Label: worthwhile (the source's label)" in note.message.replies[0]


def test_confirmation_for_a_tool_source_says_it_is_not_counted(real_ctx, real_conn, clk):
    import labels  # noqa: PLC0415

    source = labels.ensure_source(real_conn, clk, T.KIND_YOUTUBE_CHANNEL, CHANNEL_ID, "Talks Channel")
    labels.set_source_label(real_conn, clk, source.id, T.TOOL)
    run(bot.on_message(FakeUpdate(text=f"https://youtu.be/{VIDEO_ID}"), real_ctx))
    note = FakeUpdate(text="how to fill in the tax form")
    run(bot.on_message(note, real_ctx))
    reply = note.message.replies[0]
    assert "Label: tool (the source's label)" in reply
    assert "labelled tool (not content)" in reply and "#wasteful or #worthwhile" in reply


def test_build_app_parks_an_injected_youtube_client(settings, clk, spotify):
    yt = FakeYouTube()
    assert bot.build_app(settings, object(), clk, spotify, youtube=yt).bot_data["youtube"] is yt
    assert "youtube" not in bot.build_app(settings, object(), clk, spotify).bot_data


def test_spotify_capture_takes_the_override_too(real_ctx, real_conn):
    run(bot.on_message(FakeUpdate(text=LINK), real_ctx))
    note = FakeUpdate(text="guest is the expert #wasteful")
    run(bot.on_message(note, real_ctx))
    row = real_conn.execute("SELECT * FROM item_labels").fetchone()
    assert (row["item_ref"], row["label"]) == (f"spotify:{EPISODE_ID}", "wasteful")
    assert "Label: wasteful (your tag in this note)" in note.message.replies[0]
    ep = real_conn.execute("SELECT platform, source_key FROM episodes").fetchone()
    assert (ep["platform"], ep["source_key"]) == ("spotify", None)
    src = real_conn.execute("SELECT kind, source_key FROM sources WHERE kind = 'podcast_show'").fetchone()
    assert (src["kind"], src["source_key"]) == ("podcast_show", "deep dive fm")


def test_recapture_without_a_tag_says_the_override_came_from_an_earlier_capture(real_ctx, real_conn):
    """Phase 4 regression: overrides are per item, so a second capture of the
    same video without a tag still resolves to the old override, and the
    confirmation used to claim it came from "your tag in this note"."""
    run(bot.on_message(FakeUpdate(text=f"https://youtu.be/{VIDEO_ID}"), real_ctx))
    run(bot.on_message(FakeUpdate(text="#worthwhile for the talk prep"), real_ctx))
    run(bot.on_message(FakeUpdate(text=f"https://youtu.be/{VIDEO_ID}"), real_ctx))
    again = FakeUpdate(text="still want this one")
    run(bot.on_message(again, real_ctx))
    reply = again.message.replies[0]
    assert "Label: worthwhile (your tag from an earlier capture of this item)" in reply
    assert "your tag in this note" not in reply
