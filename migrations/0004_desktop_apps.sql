-- Desktop apps from Qbserve (DECISIONS DR5.3), schema_version 3 -> 4.
--
-- SQLite cannot alter a CHECK constraint, so the four tables whose CHECKs gain
-- a value are rebuilt with SQLite's documented procedure: create `x_new` with
-- the widened CHECK and otherwise identical columns and constraints, copy every
-- row (ids included), drop the old table, rename, recreate its indexes.
--
-- Widened, nothing else changed:
--   sources.kind            + 'desktop_app'
--   measurements.provenance + 'app_tracker'
--   imports.kind            + 'qbserve'
--   prompts_sent.kind       + 'qbserve_stale'
--
-- db.migrate runs this with PRAGMA foreign_keys=OFF (set before BEGIN, since it
-- cannot change inside a transaction), so dropping a referenced table neither
-- cascades nor fails, and runs PRAGMA foreign_key_check before committing.
-- References to these tables from other tables (vault_captures.source_id,
-- vault_observations.import_id) are by name and resolve to the rebuilt tables.

-- ---------------------------------------------------------------------------
-- imports
-- ---------------------------------------------------------------------------
CREATE TABLE imports_new (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN
                     ('youtube_history', 'screentime', 'coldturkey', 'vault_scan', 'qbserve')),
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
INSERT INTO imports_new (id, kind, received_at, raw_path, raw_sha256, parser_version, status,
                         period_start, period_end, warnings, error, meta)
    SELECT id, kind, received_at, raw_path, raw_sha256, parser_version, status,
           period_start, period_end, warnings, error, meta FROM imports;
DROP TABLE imports;
ALTER TABLE imports_new RENAME TO imports;
CREATE INDEX IF NOT EXISTS idx_imports_kind ON imports(kind, received_at);
CREATE INDEX IF NOT EXISTS idx_imports_sha  ON imports(raw_sha256);

-- ---------------------------------------------------------------------------
-- sources
-- ---------------------------------------------------------------------------
CREATE TABLE sources_new (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN
                     ('podcast_show', 'youtube_channel', 'iphone_app', 'web_domain', 'x', 'desktop_app')),
    source_key     TEXT NOT NULL,          -- normalised by labels.normalise_key
    display_name   TEXT NOT NULL,
    label          TEXT CHECK (label IN ('wasteful', 'worthwhile')),
    labelled_at    TEXT,
    first_seen_at  TEXT NOT NULL,
    prompted_at    TEXT,                   -- last time the bot asked for a label
    UNIQUE (kind, source_key)
);
INSERT INTO sources_new (id, kind, source_key, display_name, label, labelled_at, first_seen_at, prompted_at)
    SELECT id, kind, source_key, display_name, label, labelled_at, first_seen_at, prompted_at FROM sources;
DROP TABLE sources;
ALTER TABLE sources_new RENAME TO sources;

-- ---------------------------------------------------------------------------
-- measurements
-- ---------------------------------------------------------------------------
CREATE TABLE measurements_new (
    id             INTEGER PRIMARY KEY,
    import_id      INTEGER NOT NULL REFERENCES imports(id),
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    device         TEXT NOT NULL CHECK (device IN ('iphone', 'desktop', 'account')),
    provenance     TEXT NOT NULL CHECK (provenance IN
                     ('page_extraction', 'screenshot_ocr', 'manual_export', 'app_tracker')),
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
INSERT INTO measurements_new (id, import_id, source_id, device, provenance, item_key, item_title,
                              period_start, period_end, minutes, status, detail)
    SELECT id, import_id, source_id, device, provenance, item_key, item_title,
           period_start, period_end, minutes, status, detail FROM measurements;
DROP TABLE measurements;
ALTER TABLE measurements_new RENAME TO measurements;
CREATE UNIQUE INDEX IF NOT EXISTS uq_measurements_fact
    ON measurements(provenance, source_id, item_key, period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_measurements_period ON measurements(period_start);

-- ---------------------------------------------------------------------------
-- prompts_sent
-- ---------------------------------------------------------------------------
CREATE TABLE prompts_sent_new (
    kind           TEXT NOT NULL CHECK (kind IN
                     ('screentime', 'coldturkey', 'youtube', 'labels', 'diagnostic_end', 'qbserve_stale')),
    period_start   TEXT NOT NULL,           -- local date of the week the prompt is about
    sent_on        TEXT NOT NULL,           -- local date sent; one per kind per week per day
    sent_at        TEXT NOT NULL,
    PRIMARY KEY (kind, period_start, sent_on)
);
INSERT INTO prompts_sent_new (kind, period_start, sent_on, sent_at)
    SELECT kind, period_start, sent_on, sent_at FROM prompts_sent;
DROP TABLE prompts_sent;
ALTER TABLE prompts_sent_new RENAME TO prompts_sent;
