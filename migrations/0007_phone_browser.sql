-- The phone browser's own history (DECISIONS DR5.13), schema_version 6 -> 7.
--
-- Screen Time reports a phone browser as one opaque block: "Brave, 163 min",
-- with no way to see whether that was Reddit or a bank. Brave Sync carries the
-- phone's visits to this Mac, where they can be read locally, so the block can
-- be broken into the hostnames behind it.
--
-- Sync carries *that* a page was visited, never for how long: every synced row
-- arrives with visit_duration = 0. So the minutes still come from Screen Time
-- and are apportioned across the hostnames by the gap between consecutive
-- visits. That is an estimate, and `browser_sync` exists as its own provenance
-- so the page can say so rather than passing it off as a measurement.
--
-- SQLite cannot alter a CHECK constraint, so both tables are rebuilt with the
-- same procedure as 0004 and 0005: a `_new` table with the widened CHECK and
-- otherwise identical columns, constraints and indexes; copy every row, ids
-- included; drop; rename.
--
-- Widened, nothing else changed:
--   measurements.provenance  + 'browser_sync'
--   imports.kind             + 'brave_sync'
--
-- db.migrate runs this with PRAGMA foreign_keys=OFF (set before BEGIN) and
-- checks PRAGMA foreign_key_check before committing. measurements.import_id
-- references `imports` by name and resolves to the rebuilt table.

-- ---------------------------------------------------------------------------
-- imports
-- ---------------------------------------------------------------------------
CREATE TABLE imports_new (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN
                     ('youtube_history', 'screentime', 'coldturkey', 'vault_scan', 'qbserve',
                      'brave_sync')),
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
-- measurements
-- ---------------------------------------------------------------------------
CREATE TABLE measurements_new (
    id             INTEGER PRIMARY KEY,
    import_id      INTEGER NOT NULL REFERENCES imports(id),
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    device         TEXT NOT NULL CHECK (device IN ('iphone', 'desktop', 'account')),
    provenance     TEXT NOT NULL CHECK (provenance IN
                     ('page_extraction', 'screenshot_ocr', 'manual_export', 'app_tracker',
                      'browser_sync')),
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
