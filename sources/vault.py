"""Obsidian vault: capture log ingestion and the capture-ID scan.

CONTRACTS.md Part II §6.4. Measures one thing: did a captured note ever leave
the inbox. It produces **counts**, never minutes — there is no resume position
for a markdown note, so nothing here estimates reading time (DR1.18).

Two inputs, deliberately decoupled (DR1.15):

* **The outbox** — an append-only JSONL file obsidian-ingest writes *before*
  it writes each note (DR1.16). `read_outbox` copies new lines into
  `vault_captures`. It is the denominator: only notes the log heard of are
  tracked, so everything already in the vault stays outside the measurement.
* **The vault itself** — `scan` walks the notes, reads the one frontmatter key
  `drbr_capture_id`, and records where each capture currently lives.

Identity is the capture id and nothing else. Paths are recorded for display
and never matched on, because renames and moves happen precisely to the notes
that mattered enough to develop; path tracking would lose exactly those.

Outcomes are exactly three, decided only from where files physically are:

* ``unprocessed`` — every copy is under ``settings.vault_inbox``;
* ``kept``        — at least one copy is anywhere else in the vault;
* ``excluded``    — no file carries the id (deleted, or only in ``.trash/``).
  A deleted note is indistinguishable from one that never existed, so it
  leaves the denominator instead of counting as a failure.

``edited`` is a secondary flag: the body (frontmatter excluded) no longer
hashes to what was captured. Folder is the signal; edit is colour.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import clock
import drbr_types as T
import evidence
import labels
from clock import Clock
from config import Settings

PARSER_VERSION = "vault-1"
FRONTMATTER_KEY = "drbr_capture_id"
OUTBOX_VERSION = 1

#: Directories never searched for kept/unprocessed copies. `.trash/` is
#: Obsidian's soft delete: a note only there has been deleted by the user.
SKIP_DIRS = frozenset({".obsidian", ".git", ".trash"})

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_LINE_RE = re.compile(rf"^{FRONTMATTER_KEY}[ \t]*:(.*)$")


# --- the note format (shared with obsidian-ingest) --------------------------

def _split_frontmatter(note_text: str) -> tuple[list[str] | None, str]:
    """-> (frontmatter lines or None, body text).

    A frontmatter block exists only if the very first line is ``---`` and a
    later line is ``---`` (trailing whitespace and a UTF-8 BOM tolerated).
    Anything else — no opening fence, or an unclosed one — means the whole
    text is body. This exact rule is duplicated in obsidian-ingest's
    ``vault_write.body_sha256`` and pinned in both repos by the same test
    vector (tests/fixtures/vault/body_hash_vectors.json); change both or
    neither, or every capture would read as edited.
    """
    text = note_text[1:] if note_text.startswith("﻿") else note_text
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:])
    return None, text


def body_sha256(note_text: str) -> str:
    """SHA-256 of the note body: text after the first frontmatter block, stripped.

    Frontmatter is excluded so Obsidian property edits (``read_status``,
    tags) do not count as editing the note; mtimes are not used at all
    because moves, syncs and git checkouts change them (DR1.17).
    """
    _, body = _split_frontmatter(note_text)
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def frontmatter_capture_id(note_text: str) -> str | None:
    """The ``drbr_capture_id`` value in the first frontmatter block, or None.

    A deliberately tiny reader for one top-level scalar key rather than a
    YAML dependency: the scan must not fail on the rest of a user's
    frontmatter, which it never needs to understand. Quotes and a trailing
    `` # comment`` are tolerated; the value is lower-cased. A present but
    malformed value is returned as-is so the caller can warn about it.
    """
    fm, _ = _split_frontmatter(note_text)
    if fm is None:
        return None
    for line in fm:
        m = _KEY_LINE_RE.match(line.rstrip("\r"))
        if not m:
            continue
        value = m.group(1).strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1].strip()
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        return value.lower()
    return None


# --- grouping ---------------------------------------------------------------

_KEEPER_GROUPS = {
    "x-post": T.GROUP_X,
    "x-article": T.GROUP_X,
    "youtube": T.GROUP_YOUTUBE,
    "article": T.GROUP_WEB,
}


def keeper_group_for(capture_type: str) -> str:
    """The cost-per-keeper row a capture counts towards. Unknown types -> 'other'.

    Mapped from obsidian-ingest's own recorded type, verbatim, not from the
    note's content or folder, so a capture's group never depends on judgement.
    """
    return _KEEPER_GROUPS.get(capture_type, T.GROUP_OTHER)


def _source_for(conn: sqlite3.Connection, clk: Clock, capture_type: str, url: str | None) -> int | None:
    """Attribute a capture to a labelled source only from explicit data.

    X captures -> the one seeded X source. Articles -> the URL's host as a
    web domain. YouTube captures carry no channel id in the outbox line, and
    guessing one from a title would be inference, so they get no source.
    """
    group = keeper_group_for(capture_type)
    if group == T.GROUP_X:
        return labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, "x.com").id
    if group == T.GROUP_WEB and url:
        host = (urlsplit(url).hostname or "").strip()
        if host:
            return labels.ensure_source(conn, clk, T.KIND_WEB_DOMAIN, host, display_name=host).id
    return None


# --- outbox -----------------------------------------------------------------

@dataclass(frozen=True)
class OutboxResult:
    new: int
    malformed: list[str]


class _BadLine(ValueError):
    pass


def _validate_line(obj: object) -> dict:
    """Check one outbox object against the §6.4 shape. Raises _BadLine."""
    if not isinstance(obj, dict):
        raise _BadLine("not a JSON object")
    if obj.get("v") != OUTBOX_VERSION:
        raise _BadLine(f"unsupported version {obj.get('v')!r}")
    cid = obj.get("capture_id")
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        raise _BadLine("capture_id is not 32 lowercase hex characters")
    captured_at = obj.get("captured_at")
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        raise _BadLine("captured_at is not an ISO timestamp") from None
    if dt.tzinfo is None:
        raise _BadLine("captured_at has no timezone")
    for key in ("capture_type", "rel_path"):
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            raise _BadLine(f"{key} missing or empty")
    if not isinstance(obj.get("title"), str):
        raise _BadLine("title missing")
    for key in ("url", "author"):
        if obj.get(key) is not None and not isinstance(obj[key], str):
            raise _BadLine(f"{key} must be a string or null")
    rel = obj["rel_path"]
    if rel.startswith("/") or pathlib.PurePosixPath(rel).is_absolute() or ".." in pathlib.PurePosixPath(rel).parts:
        raise _BadLine("rel_path is not relative to the vault")
    sha = obj.get("body_sha256")
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise _BadLine("body_sha256 is not a hex SHA-256")
    return {**obj, "captured_at": clock.iso(dt)}


def read_outbox(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> OutboxResult:
    """Copy new outbox lines into `vault_captures`. Idempotent by capture_id.

    The whole file is re-read every time: it is small, append-only, and
    re-reading makes the function trivially safe to run as often as the job
    likes. A malformed line is reported (by line number and reason, never by
    content) and skipped; one bad line never hides the rest. A second line
    with a known capture_id but different data is reported too — the first
    record stands, because it was written before the note was.
    A missing outbox file means nothing has been captured yet.
    """
    path = settings.capture_outbox
    if path is None or not pathlib.Path(path).exists():
        return OutboxResult(0, [])
    malformed: list[str] = []
    new = 0
    now = clock.iso(clk.now())
    with open(path, "rb") as fh:
        raw_lines = fh.read().split(b"\n")
    for n, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            obj = _validate_line(json.loads(raw.decode("utf-8")))
        except UnicodeDecodeError:
            malformed.append(f"line {n}: not UTF-8")
            continue
        except json.JSONDecodeError:
            malformed.append(f"line {n}: not valid JSON")
            continue
        except _BadLine as exc:
            malformed.append(f"line {n}: {exc}")
            continue
        existing = conn.execute(
            "SELECT captured_at, capture_type, url, title, initial_rel_path, body_sha256 "
            "FROM vault_captures WHERE capture_id = ?", (obj["capture_id"],)).fetchone()
        if existing is not None:
            if tuple(existing) != (obj["captured_at"], obj["capture_type"], obj.get("url"),
                                   obj["title"], obj["rel_path"], obj["body_sha256"]):
                malformed.append(f"line {n}: capture_id already logged with different data; first record kept")
            continue
        source_id = _source_for(conn, clk, obj["capture_type"], obj.get("url"))
        conn.execute(
            "INSERT INTO vault_captures (capture_id, captured_at, capture_type, keeper_group, source_id, "
            "url, title, initial_rel_path, body_sha256, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (obj["capture_id"], obj["captured_at"], obj["capture_type"], keeper_group_for(obj["capture_type"]),
             source_id, obj.get("url"), obj["title"], obj["rel_path"], obj["body_sha256"], now),
        )
        new += 1
    conn.commit()
    return OutboxResult(new, malformed)


# --- scan -------------------------------------------------------------------

@dataclass(frozen=True)
class VaultOutcome:
    outcome: str               # 'unprocessed' | 'kept' | 'excluded'
    rel_path: str | None       # None when excluded
    edited: bool | None        # None when excluded
    copies: int
    scanned_at: str


def _in_inbox(rel_path: str, inbox: str) -> bool:
    inbox = inbox.strip("/")
    return rel_path == inbox or rel_path.startswith(inbox + "/")


def _resolve_inbox(root: pathlib.Path, inbox: str) -> str | None:
    """The inbox folder's relative path as it is spelled on disk, or None if absent.

    Outcomes compare note paths (as `os.walk` spells them) against the inbox
    prefix. On macOS the file system is case-insensitive, so a setting of
    `01-inbox` passes an `is_dir()` check for `01-Inbox` while matching no
    note path at all — which would silently turn every capture into `kept`.
    Each segment is therefore resolved to its real spelling: an exact name
    first, else the one entry that matches case-insensitively.
    """
    current = root
    parts: list[str] = []
    for segment in [p for p in inbox.strip("/").split("/") if p]:
        try:
            names = [e.name for e in os.scandir(current) if e.is_dir()]
        except OSError:
            return None
        if segment in names:
            real = segment
        else:
            matches = [n for n in names if n.casefold() == segment.casefold()]
            if len(matches) != 1:
                return None
            real = matches[0]
        parts.append(real)
        current = current / real
    return "/".join(parts) if parts else None


def _walk_notes(root: pathlib.Path, warnings: list[str]):
    """Yield (relative posix path, text) for every note outside SKIP_DIRS."""

    def on_error(exc: OSError) -> None:
        warnings.append(f"could not list a folder: {exc.strerror or exc}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            full = pathlib.Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            try:
                data = full.read_bytes()
            except OSError as exc:
                warnings.append(f"could not read {rel}: {exc.strerror or exc}")
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                warnings.append(f"{rel} is not valid UTF-8; read with replacement characters")
                text = data.decode("utf-8", errors="replace")
            yield rel, text


def scan(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> evidence.Outcome:
    """Record one observation per logged capture: where its note is now.

    A missing or unreadable vault root **fails the import** rather than
    marking every capture excluded — an unmounted disk must never look like
    the user deleted everything. Notes carrying an id the outbox never logged
    are counted and warned about (the log failed for them) but not tracked.
    """
    rec = evidence.begin_import(conn, clk, kind="vault_scan", parser_version=PARSER_VERSION)
    root = pathlib.Path(settings.vault_path) if settings.vault_path is not None else None
    if root is None or not root.is_dir():
        error = "vault path is not configured" if root is None else "vault path is not a readable folder"
        evidence.fail_import(conn, rec, error)
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error=error)
    captures = {r["capture_id"]: r["body_sha256"]
                for r in conn.execute("SELECT capture_id, body_sha256 FROM vault_captures")}
    inbox = _resolve_inbox(root, settings.vault_inbox)
    if inbox is None:
        error = f"inbox folder {settings.vault_inbox!r} does not exist in the vault"
        if captures:
            # Without the inbox, "outside the inbox" is everywhere: every
            # capture would be recorded as kept and flow into cost per keeper.
            # Same reasoning as a missing vault root (DR2.6): fail loudly.
            evidence.fail_import(conn, rec, error + "; outcomes cannot be decided")
            return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error=error + "; outcomes cannot be decided")
        rec.warnings.append(error)
        inbox = settings.vault_inbox.strip("/")
    found: dict[str, list[tuple[str, str]]] = {}      # id -> [(rel_path, current body hash)]
    files = unknown = bad_ids = 0
    try:
        for rel, text in _walk_notes(root, rec.warnings):
            files += 1
            cid = frontmatter_capture_id(text)
            if cid is None:
                continue
            if not _ID_RE.match(cid):
                bad_ids += 1
                rec.warnings.append(f"{rel}: {FRONTMATTER_KEY} is not a 32-character hex id")
                continue
            if cid not in captures:
                unknown += 1
                continue
            found.setdefault(cid, []).append((rel, body_sha256(text)))
    except OSError as exc:  # pragma: no cover — os.walk reports via onerror
        evidence.fail_import(conn, rec, f"vault walk failed: {exc}")
        return evidence.Outcome(rec.id, T.IMPORT_FAILED, 0, error=f"vault walk failed: {exc}")

    if unknown:
        rec.warnings.append(
            f"{unknown} note(s) carry a capture id the outbox never logged; they are not tracked")

    scanned_at = clock.iso(clk.now())
    counts = {"unprocessed": 0, "kept": 0, "excluded": 0}
    edited_total = duplicates = 0
    for cid, captured_hash in captures.items():
        copies = sorted(found.get(cid, []))
        if not copies:
            outcome, rel_path, edited = "excluded", None, None
        else:
            outside = [c for c in copies if not _in_inbox(c[0], inbox)]
            outcome = "kept" if outside else "unprocessed"
            counted = outside or copies
            rel_path = counted[0][0]
            edited = any(h != captured_hash for _, h in counted)
            edited_total += int(edited)
            if len(copies) > 1:
                duplicates += 1
                rec.warnings.append(f"capture {cid} is carried by {len(copies)} files; outcome '{outcome}'")
        counts[outcome] += 1
        conn.execute(
            "INSERT INTO vault_observations (import_id, capture_id, scanned_at, outcome, rel_path, edited, copies) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rec.id, cid, scanned_at, outcome, rel_path, None if edited is None else int(edited), len(copies)),
        )

    rec.meta.update({
        "files_scanned": files, "captures": len(captures), **counts, "edited": edited_total,
        "duplicate_ids": duplicates, "unlogged_ids": unknown, "malformed_ids": bad_ids,
    })
    status = evidence.finish_import(conn, clk, settings, rec, period_start=None, period_end=None)
    return evidence.Outcome(rec.id, status, 0, tuple(rec.warnings))


def latest_outcomes(conn: sqlite3.Connection) -> dict[str, VaultOutcome]:
    """The current outcome per capture: its observation from the newest scan.

    Older observations are kept as history; only the newest one is current.
    Captures never scanned yet are absent rather than guessed.
    """
    rows = conn.execute(
        """
        SELECT o.capture_id, o.outcome, o.rel_path, o.edited, o.copies, o.scanned_at
        FROM vault_observations o
        WHERE o.id = (SELECT o2.id FROM vault_observations o2 WHERE o2.capture_id = o.capture_id
                      ORDER BY o2.scanned_at DESC, o2.id DESC LIMIT 1)
        """
    ).fetchall()
    return {
        r["capture_id"]: VaultOutcome(r["outcome"], r["rel_path"],
                                      None if r["edited"] is None else bool(r["edited"]),
                                      r["copies"], r["scanned_at"])
        for r in rows
    }
