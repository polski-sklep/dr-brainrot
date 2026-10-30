-- dr-brainrot migration: schema_version 1 -> 2. Applied by db.migrate() exactly once.
--
-- Additive only. No existing queue row is rewritten; no queue column is dropped or
-- retyped. Every new column on an existing table has a default that reproduces
-- pre-dr-brainrot behaviour for the rows already there.
--
-- Conventions (CONTRACTS.md Part II §1):
--   * instants are ISO-8601 UTC strings (clock.iso), as in the queue;
--   * calendar days are local dates 'YYYY-MM-DD' in DR_BRAINROT_TZ;
--   * JSON columns hold text produced by json.dumps(..., sort_keys=True).

-- ---------------------------------------------------------------------------
-- Existing queue tables, extended
-- ---------------------------------------------------------------------------

-- The queue can now hold YouTube videos as well as Spotify episodes. The
-- column name `spotify_id` is kept (renaming would break every queue query);
-- for a YouTube row it holds 'youtube:<videoId>'. Spotify rows keep their bare
-- 22-char ids and default to platform 'spotify'.
ALTER TABLE episodes ADD COLUMN platform TEXT NOT NULL DEFAULT 'spotify'
    CHECK (platform IN ('spotify', 'youtube'));

-- The labelled source this row belongs to. NULL for Spotify (the source is the
-- normalised show name, which the queue already stores in `show`); the YouTube
-- channel id for YouTube rows.
ALTER TABLE episodes ADD COLUMN source_key TEXT;

-- ---------------------------------------------------------------------------
-- Sources and labels
-- ---------------------------------------------------------------------------

-- A thing that can carry a label: a podcast show, a YouTube channel, an iPhone
-- app, a desktop web domain, or X. label NULL means unlabelled, which RESOLVES
-- to wasteful (labels.resolve) but is stored as NULL so "never decided" stays
-- distinguishable from "decided wasteful".
CREATE TABLE IF NOT EXISTS sources (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN
                     ('podcast_show', 'youtube_channel', 'iphone_app', 'web_domain', 'x')),
    source_key     TEXT NOT NULL,          -- normalised by labels.normalise_key
    display_name   TEXT NOT NULL,
    label          TEXT CHECK (label IN ('wasteful', 'worthwhile')),
    labelled_at    TEXT,
    first_seen_at  TEXT NOT NULL,
    prompted_at    TEXT,                   -- last time the bot asked for a label
    UNIQUE (kind, source_key)
);

-- X is the one label fixed by the brief rather than bootstrapped from data.
INSERT OR IGNORE INTO sources (kind, source_key, display_name, label, labelled_at, first_seen_at)
VALUES ('x', 'x', 'X', 'wasteful', '1970-01-01T00:00:00+00:00', '1970-01-01T00:00:00+00:00');

-- Per-item override, set at capture in the same message as the why-note.
-- item_ref is 'spotify:<episodeId>' or 'youtube:<videoId>'.
CREATE TABLE IF NOT EXISTS item_labels (
    item_ref       TEXT PRIMARY KEY,
    label          TEXT NOT NULL CHECK (label IN ('wasteful', 'worthwhile')),
    set_at         TEXT NOT NULL,
    origin         TEXT NOT NULL CHECK (origin IN ('capture'))
);

-- ---------------------------------------------------------------------------
-- Raw evidence and measurements
-- ---------------------------------------------------------------------------

-- One row per ingestion attempt, successful or not. raw_path points into
-- data/raw/ so any parse can be re-run later against the original evidence.
CREATE TABLE IF NOT EXISTS imports (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN
                     ('youtube_history', 'screentime', 'coldturkey', 'vault_scan')),
    received_at    TEXT NOT NULL,
    raw_path       TEXT,                   -- relative to settings.raw_dir; NULL for vault_scan
    raw_sha256     TEXT,
    parser_version TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'failed')),
    period_start   TEXT,                   -- local date, inclusive
    period_end     TEXT,                   -- local date, inclusive
    warnings       TEXT NOT NULL DEFAULT '[]',
    error          TEXT,
    meta           TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_imports_kind ON imports(kind, received_at);
CREATE INDEX IF NOT EXISTS idx_imports_sha  ON imports(raw_sha256);

-- Minutes from every non-Spotify source. Spotify stays in the queue's `listening`.
--
-- minutes is NULL whenever the value could not be measured (status != 'ok').
-- It is never defaulted to 0: a zero is a measurement, a NULL is an absence.
CREATE TABLE IF NOT EXISTS measurements (
    id             INTEGER PRIMARY KEY,
    import_id      INTEGER NOT NULL REFERENCES imports(id),
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    device         TEXT NOT NULL CHECK (device IN ('iphone', 'desktop', 'account')),
    provenance     TEXT NOT NULL CHECK (provenance IN
                     ('page_extraction', 'screenshot_ocr', 'manual_export')),
    item_key       TEXT NOT NULL DEFAULT '',   -- YouTube video id; '' for aggregates
    item_title     TEXT,
    period_start   TEXT NOT NULL,              -- local date, inclusive
    period_end     TEXT NOT NULL,              -- local date, inclusive
    minutes        REAL CHECK (minutes IS NULL OR minutes >= 0),
    status         TEXT NOT NULL DEFAULT 'ok' CHECK (status IN
                     ('ok', 'progress_missing', 'duration_missing', 'unreadable')),
    detail         TEXT NOT NULL DEFAULT '{}',
    CHECK ((status = 'ok') = (minutes IS NOT NULL))
);

-- The natural key. A re-import of the same fact replaces the older row (the
-- older import's raw evidence is kept in `imports`).
CREATE UNIQUE INDEX IF NOT EXISTS uq_measurements_fact
    ON measurements(provenance, source_id, item_key, period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_measurements_period ON measurements(period_start);

-- Category totals from Screen Time. Kept apart from `measurements` because a
-- category is not a labelled source; it is reported, never gated.
CREATE TABLE IF NOT EXISTS category_measurements (
    id             INTEGER PRIMARY KEY,
    import_id      INTEGER NOT NULL REFERENCES imports(id),
    category       TEXT NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    minutes        REAL NOT NULL CHECK (minutes >= 0),
    UNIQUE (category, period_start, period_end)
);

-- ---------------------------------------------------------------------------
-- Obsidian vault
-- ---------------------------------------------------------------------------

-- Written from the obsidian-ingest outbox. Identity is capture_id, never path.
CREATE TABLE IF NOT EXISTS vault_captures (
    capture_id       TEXT PRIMARY KEY,
    captured_at      TEXT NOT NULL,
    capture_type     TEXT NOT NULL,          -- obsidian-ingest input type, verbatim
    keeper_group     TEXT NOT NULL CHECK (keeper_group IN
                       ('x', 'youtube', 'podcasts', 'web', 'other')),
    source_id        INTEGER REFERENCES sources(id),
    url              TEXT,
    title            TEXT,
    initial_rel_path TEXT NOT NULL,
    body_sha256      TEXT NOT NULL,
    logged_at        TEXT NOT NULL
);

-- One row per capture per scan. The latest scan is the current outcome.
CREATE TABLE IF NOT EXISTS vault_observations (
    id             INTEGER PRIMARY KEY,
    import_id      INTEGER NOT NULL REFERENCES imports(id),
    capture_id     TEXT NOT NULL REFERENCES vault_captures(capture_id),
    scanned_at     TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('unprocessed', 'kept', 'excluded')),
    rel_path       TEXT,                    -- NULL when excluded
    edited         INTEGER CHECK (edited IN (0, 1)),   -- NULL when excluded
    copies         INTEGER NOT NULL DEFAULT 1 CHECK (copies >= 0),
    UNIQUE (import_id, capture_id)
);

CREATE INDEX IF NOT EXISTS idx_vault_obs_capture ON vault_observations(capture_id, scanned_at);

-- ---------------------------------------------------------------------------
-- Prompt bookkeeping (same idea as the queue's alerts_sent: dedup state, not input)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prompts_sent (
    kind           TEXT NOT NULL CHECK (kind IN
                     ('screentime', 'coldturkey', 'youtube', 'labels', 'diagnostic_end')),
    period_start   TEXT NOT NULL,           -- local date of the week the prompt is about
    sent_on        TEXT NOT NULL,           -- local date sent; one per kind per week per day
    sent_at        TEXT NOT NULL,
    PRIMARY KEY (kind, period_start, sent_on)
);
