"""Source labels, per-item overrides, and the one label resolver.

CONTRACTS.md Part II §5. Phase 1 wrote the core (normalisation, writes, the
resolver); Phase 2 added batch selection for the bot's label prompts and a
listing for the dashboard's label manager, without changing any signature.

The governing principle: **the user labels, the system counts.** Nothing in
this module may derive a label from behaviour, duration, folder, or any other
indirect signal. A label is read from explicit stored state or it is the
documented default.

**Content only (DECISIONS DR5.5).** DRBR measures brain-rot *content*, not time
in general. wasteful/worthwhile are value judgements about content. A source
that is not content at all (a shop, email, banking, a todo app, the weather,
DRBR's own dashboard) is labelled `tool`: out of scope. `resolve` returns
`tool` for it and every consumer keeps those minutes out of wasteful,
worthwhile, the allowance and cost per keeper, while still showing them
separately. `tool` is a *source* label only: per-item overrides stay binary,
because a captured queue item is content by definition, and an override on it
still wins over a tool source.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

import clock
import drbr_types as T
from clock import Clock
from config import Settings


class LabelError(ValueError):
    """Bad label value, unknown source, or malformed key."""


@dataclass(frozen=True)
class Source:
    id: int
    kind: str
    source_key: str
    display_name: str
    label: str | None          # None = unlabelled
    labelled_at: str | None
    first_seen_at: str
    prompted_at: str | None


@dataclass(frozen=True)
class ResolvedLabel:
    label: str                 # 'wasteful' or 'worthwhile'; 'tool' only from a source label (DR5.5)
    basis: str                 # T.BASIS_*


# --- normalisation ----------------------------------------------------------

_X_APP_NAMES = {"x", "twitter"}
_X_DOMAINS = {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}
#: X/Twitter clients for the Mac, by bundle id, casefolded (Qbserve, DR5.3).
_X_BUNDLES = {"com.twitter.twitter-mac", "maccatalyst.com.atebits.tweetie2", "com.atebits.tweetie2"}


def normalise_key(kind: str, raw: str) -> tuple[str, str]:
    """-> (kind, key). The only place source identity is canonicalised.

    X is folded into one source whichever surface reports it (iPhone app 'X',
    desktop 'x.com'/'twitter.com', a Mac X client's bundle id), because the
    brief labels X once. A desktop app's key is its bundle id, casefolded.
    """
    if kind not in T.SOURCE_KINDS:
        raise LabelError(f"unknown source kind {kind!r}")
    value = (raw or "").strip()
    if not value:
        raise LabelError(f"empty source key for {kind}")
    if kind == T.KIND_IPHONE_APP:
        key = re.sub(r"\s+", " ", value).casefold()
        if key in _X_APP_NAMES:
            return T.KIND_X, "x"
        return kind, key
    if kind == T.KIND_WEB_DOMAIN:
        key = value.casefold()
        key = re.sub(r"^[a-z]+://", "", key).split("/")[0]
        if key.startswith("www."):
            key = key[4:]
        if key in _X_DOMAINS:
            return T.KIND_X, "x"
        return kind, key
    if kind == T.KIND_DESKTOP_APP:
        key = value.casefold()
        if key in _X_BUNDLES:
            return T.KIND_X, "x"
        return kind, key
    if kind == T.KIND_PODCAST_SHOW:
        return kind, re.sub(r"\s+", " ", value).casefold()
    if kind == T.KIND_YOUTUBE_CHANNEL:
        return kind, value            # channel ids (UC...) are case-sensitive
    return T.KIND_X, "x"


# --- sources ----------------------------------------------------------------

def _to_source(row: sqlite3.Row) -> Source:
    return Source(row["id"], row["kind"], row["source_key"], row["display_name"], row["label"],
                  row["labelled_at"], row["first_seen_at"], row["prompted_at"])


def ensure_source(conn: sqlite3.Connection, clk: Clock, kind: str, raw_key: str,
                  display_name: str | None = None) -> Source:
    """Get or create a source, normalised. Does not commit (callers batch)."""
    kind, key = normalise_key(kind, raw_key)
    conn.execute(
        "INSERT OR IGNORE INTO sources (kind, source_key, display_name, first_seen_at) VALUES (?, ?, ?, ?)",
        (kind, key, (display_name or raw_key).strip() or key, clock.iso(clk.now())),
    )
    row = conn.execute("SELECT * FROM sources WHERE kind = ? AND source_key = ?", (kind, key)).fetchone()
    return _to_source(row)


def get_source(conn: sqlite3.Connection, source_id: int) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return _to_source(row) if row else None


def is_covered_elsewhere(source: Source) -> str | None:
    """Reason text if this source's minutes are measured by a better source."""
    return T.COVERED_ELSEWHERE.get((source.kind, source.source_key))


# --- explicit label writes -------------------------------------------------

def set_source_label(conn: sqlite3.Connection, clk: Clock, source_id: int, label: str) -> Source:
    """The user labelled a source: wasteful, worthwhile or tool. Commits."""
    _check_source_label(label)
    cur = conn.execute("UPDATE sources SET label = ?, labelled_at = ? WHERE id = ?",
                       (label, clock.iso(clk.now()), source_id))
    if cur.rowcount != 1:
        raise LabelError(f"no source with id {source_id}")
    conn.commit()
    return get_source(conn, source_id)  # type: ignore[return-value]


def set_item_override(conn: sqlite3.Connection, clk: Clock, item_ref: str, label: str) -> None:
    """Per-item override written at capture. Last explicit statement wins. Does not commit."""
    _check_label(label)
    conn.execute(
        "INSERT INTO item_labels (item_ref, label, set_at, origin) VALUES (?, ?, ?, 'capture') "
        "ON CONFLICT(item_ref) DO UPDATE SET label = excluded.label, set_at = excluded.set_at",
        (item_ref, label, clock.iso(clk.now())),
    )


_OVERRIDE_RE = re.compile(r"(?<!\S)#(worthwhile|worth|wasteful|waste)(?!\S)", re.IGNORECASE)


def parse_override_token(text: str) -> tuple[str, str | None]:
    """Split an explicit label token off a why-note. -> (note, label or None).

    '#worthwhile' / '#worth' / '#wasteful' / '#waste', as its own word,
    anywhere in the note. Typing it is the override, in the same message as the
    reason, so it is one decision rather than two. Two conflicting tokens are
    not guessed between: the note is returned untouched with no override.
    """
    found = {m.group(1).lower() for m in _OVERRIDE_RE.finditer(text or "")}
    labels_found = {T.WORTHWHILE if f.startswith("worth") else T.WASTEFUL for f in found}
    if len(labels_found) != 1:
        return (text or "").strip(), None
    note = _OVERRIDE_RE.sub("", text)
    note = re.sub(r"[ \t]{2,}", " ", note).strip()
    return note, labels_found.pop()


# --- resolution -------------------------------------------------------------

def _check_label(label: str) -> str:
    """A value label: what an item override may be."""
    if label not in T.LABELS:
        raise LabelError(f"label must be one of {T.LABELS}, got {label!r}")
    return label


def _check_source_label(label: str) -> str:
    """What a source may be labelled: a value label, or tool (not content)."""
    if label not in T.SOURCE_LABELS:
        raise LabelError(f"source label must be one of {T.SOURCE_LABELS}, got {label!r}")
    return label


def item_override(conn: sqlite3.Connection, item_ref: str) -> str | None:
    row = conn.execute("SELECT label FROM item_labels WHERE item_ref = ?", (item_ref,)).fetchone()
    return row["label"] if row else None


def resolve(conn: sqlite3.Connection, *, source_id: int | None, item_ref: str | None = None) -> ResolvedLabel:
    """THE resolver. Every gate, allowance and report call goes through here.

    Precedence, fixed:
      1. per-item override (set at capture)          -> basis item_override
      2. the source's explicit label                  -> basis source_label
         (wasteful, worthwhile, or tool: not content, out of scope)
      3. unlabelled (or no source known)              -> wasteful, default_unlabelled

    An override beats a tool source: a captured item is content by definition.
    """
    if item_ref is not None:
        override = item_override(conn, item_ref)
        if override is not None:
            return ResolvedLabel(_check_label(override), T.BASIS_ITEM_OVERRIDE)
    if source_id is not None:
        row = conn.execute("SELECT label FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise LabelError(f"no source with id {source_id}")
        if row["label"] is not None:
            return ResolvedLabel(_check_source_label(row["label"]), T.BASIS_SOURCE_LABEL)
    return ResolvedLabel(T.DEFAULT_LABEL, T.BASIS_DEFAULT)


# --- prompting and listing ---------------------------------------------------

#: A source the user was asked about is not asked about again for this long.
#: Ignoring a prompt is allowed; being nagged daily about it is not.
PROMPT_COOLDOWN = timedelta(days=7)

UNLABELLED = "unlabelled"


def _sources_with_evidence(conn: sqlite3.Connection) -> set[int]:
    """Ids of sources something was actually observed for.

    Three kinds of evidence, all explicit rows: a measurement, a vault
    capture attributed to the source, or a listening row for a queue episode
    whose show/channel is the source. The last is matched in Python because
    podcast keys are normalised by `normalise_key` (casefold), which SQL
    cannot reproduce exactly.
    """
    ids = {r[0] for r in conn.execute("SELECT DISTINCT source_id FROM measurements")}
    ids |= {r[0] for r in conn.execute(
        "SELECT DISTINCT source_id FROM vault_captures WHERE source_id IS NOT NULL")}
    keys: set[tuple[str, str]] = set()
    for row in conn.execute(
        "SELECT DISTINCT e.platform, e.show, e.source_key FROM episodes e "
        "WHERE EXISTS (SELECT 1 FROM listening l WHERE l.spotify_id = e.spotify_id)"
    ):
        try:
            if row["platform"] == "youtube":
                if row["source_key"]:
                    keys.add(normalise_key(T.KIND_YOUTUBE_CHANNEL, row["source_key"]))
            elif row["show"]:
                keys.add(normalise_key(T.KIND_PODCAST_SHOW, row["show"]))
        except LabelError:
            continue
    if keys:
        for row in conn.execute("SELECT id, kind, source_key FROM sources"):
            if (row["kind"], row["source_key"]) in keys:
                ids.add(row["id"])
    return ids


def sources_needing_label(conn: sqlite3.Connection, clk: Clock, settings: Settings, *,
                          limit: int = 8) -> list[Source]:
    """The next sources to ask the user about, oldest first.

    Only sources that are unlabelled (tool counts as labelled), not covered by a better measurement
    (asking to label "YouTube" the app would be a decision that changes
    nothing), actually observed at least once, and not prompted within
    `PROMPT_COOLDOWN`. Selection is a filter over stored facts; nothing about
    how much or how long a source was used affects whether or when it is asked.
    `settings` is part of the fixed contract signature and currently unused.
    """
    if limit <= 0:
        return []
    cutoff = clock.iso(clk.now() - PROMPT_COOLDOWN)
    evidenced = _sources_with_evidence(conn)
    rows = conn.execute(
        "SELECT * FROM sources WHERE label IS NULL AND (prompted_at IS NULL OR prompted_at <= ?) "
        "ORDER BY first_seen_at ASC, id ASC",
        (cutoff,),
    ).fetchall()
    out: list[Source] = []
    for row in rows:
        source = _to_source(row)
        if source.id not in evidenced or is_covered_elsewhere(source):
            continue
        if source.kind == T.KIND_YOUTUBE_CHANNEL and source.source_key == T.UNKNOWN_YOUTUBE_CHANNEL_KEY:
            continue
        out.append(source)
        if len(out) >= limit:
            break
    return out


def mark_prompted(conn: sqlite3.Connection, clk: Clock, source_ids) -> None:
    """Stamp sources as just asked about. Commits: the message has already gone."""
    ids = list(source_ids)
    if not ids:
        return
    now = clock.iso(clk.now())
    conn.executemany("UPDATE sources SET prompted_at = ? WHERE id = ?", [(now, i) for i in ids])
    conn.commit()


def list_sources(conn: sqlite3.Connection, *, kind: str | None = None,
                 label: str | None = None) -> list[Source]:
    """Sources for the label manager, optionally filtered.

    `label` is 'wasteful', 'worthwhile', 'tool', 'unlabelled' (stored NULL),
    or None for all. Unlabelled is kept distinct from wasteful here even though it
    resolves to wasteful: "never decided" and "decided" are different facts.
    """
    clauses, params = [], []
    if kind is not None:
        if kind not in T.SOURCE_KINDS:
            raise LabelError(f"unknown source kind {kind!r}")
        clauses.append("kind = ?")
        params.append(kind)
    if label == UNLABELLED:
        clauses.append("label IS NULL")
    elif label is not None:
        _check_source_label(label)
        clauses.append("label = ?")
        params.append(label)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM sources {where} ORDER BY kind, display_name COLLATE NOCASE, id", params
    ).fetchall()
    return [_to_source(r) for r in rows]
