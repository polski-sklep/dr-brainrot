-- Audiobook listening from Audible (DECISIONS DR5.28), schema_version 7 -> 8.
--
-- Audible's own daily listening totals are read once a day, and each day is
-- split across the books whose place moved. Each book is a source of its own,
-- labelled worthwhile when first seen: the user's standing decision that
-- audiobooks are content but not brain rot.
--
-- SQLite cannot alter a CHECK constraint, so the three tables whose CHECKs gain
-- a value are rebuilt with the procedure of 0004, 0005 and 0007: a `_new` table
-- with the widened CHECK and otherwise identical columns, constraints and
-- indexes; copy every row, ids included; drop; rename; recreate indexes.
--
-- Widened, nothing else changed:
--   imports.kind             + 'audible'
--   sources.kind             + 'audiobook'
--   measurements.provenance  + 'audible_stats'
-- No row is changed and nothing is labelled here: books are labelled by the
-- reader when it first sees them.
--
-- db.migrate runs this with PRAGMA foreign_keys=OFF (set before BEGIN) and
-- checks PRAGMA foreign_key_check before committing. measurements.import_id,
-- measurements.source_id, vault_captures.source_id, vault_observations.import_id
-- and category_measurements.import_id reference these tables by name and
-- resolve to the rebuilt ones.

-- ---------------------------------------------------------------------------
-- imports
-- ---------------------------------------------------------------------------
CREATE TABLE imports_new (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN
                     ('youtube_history', 'screentime', 'coldturkey', 'vault_scan', 'qbserve',
                      'brave_sync', 'audible')),
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
                     ('podcast_show', 'youtube_channel', 'iphone_app', 'web_domain', 'x', 'desktop_app',
                      'audiobook')),
    source_key     TEXT NOT NULL,          -- normalised by labels.normalise_key
    display_name   TEXT NOT NULL,
    label          TEXT CHECK (label IN ('wasteful', 'worthwhile', 'tool')),
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
                     ('page_extraction', 'screenshot_ocr', 'manual_export', 'app_tracker',
                      'browser_sync', 'audible_stats')),
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
