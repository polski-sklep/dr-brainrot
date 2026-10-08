-- The "tool" source label (DECISIONS DR5.5), schema_version 4 -> 5.
--
-- dr-brainrot measures brain-rot content only. A source the user only goes to to get
-- a task done (shopping, email, banking, a todo app) is not content, so it is
-- labelled `tool`: out of scope, never counted as wasteful or worthwhile, and
-- shown separately so the label cannot become a hiding place.
--
-- SQLite cannot alter a CHECK constraint, so `sources` is rebuilt with the same
-- procedure as 0004: create `sources_new` with the widened CHECK and otherwise
-- identical columns and constraints, copy every row (ids included), drop the
-- old table, rename. `sources` has no indexes of its own besides the implicit
-- UNIQUE (kind, source_key), which the new definition carries.
--
-- Widened, nothing else changed:
--   sources.label  + 'tool'
-- `item_labels.label` stays ('wasteful', 'worthwhile'): a queue item is content
-- by definition, so a per-item override is always a value judgement.
--
-- db.migrate runs this with PRAGMA foreign_keys=OFF (set before BEGIN), so
-- dropping `sources` neither cascades nor fails, and runs PRAGMA
-- foreign_key_check before committing. measurements.source_id and
-- vault_captures.source_id reference `sources` by name and resolve to the
-- rebuilt table.

CREATE TABLE sources_new (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN
                     ('podcast_show', 'youtube_channel', 'iphone_app', 'web_domain', 'x', 'desktop_app')),
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

-- The one automatic label (user-approved, DR5.5): dr-brainrot's own dashboard host is
-- the measuring instrument, not content. Cold Turkey records the time spent
-- reading the dashboard in a browser as the web domain 127.0.0.1 (or
-- localhost). Nothing else is labelled here; the user labels.
-- labelled_at is the moment this migration ran, in the same UTC ISO-8601 shape
-- clock.iso writes ('YYYY-MM-DDTHH:MM:SS+00:00').
UPDATE sources
   SET label = 'tool',
       labelled_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
 WHERE kind = 'web_domain' AND source_key IN ('127.0.0.1', 'localhost');
