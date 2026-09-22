-- The weekly insights summary (DECISIONS DR5.8), schema_version 5 -> 6.
--
-- One row per generated summary. Additive only: a new table, no existing
-- table is touched and nothing is rebuilt, so this step needs none of the
-- foreign-key gymnastics 0004 and 0005 needed.
--
-- What each column holds:
--   created_at     when the row was written (clock.iso shape, UTC)
--   model          the model id that produced `summary`; NULL when none did
--   week_start     the allowance week the summary was generated IN, so the
--                  "once a week" rule is a lookup rather than a date sum
--   summary        the text shown on the Overview. For a failed row this is
--                  the locally computed fallback, so the panel is readable
--                  even when the payload cannot be re-read.
--   payload        the exact aggregate-only JSON that was sent (DR5.8), kept
--                  so the summary can always be checked against what it was
--                  based on, and so `latest` can recompute the fallback.
--   status         'ok'     — a model wrote `summary`
--                  'failed' — no key, an API error, or an unusable answer;
--                             `summary` is then the computed fallback
--   error          why it failed, secrets scrubbed. NULL when status is 'ok'.
--   input_tokens / output_tokens  what the call cost, NULL when none was made
--
-- The payload never contains a source name, an item title, a note title or a
-- path: only report group names, minutes by label, and counts.

CREATE TABLE IF NOT EXISTS insights (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    model         TEXT,
    week_start    TEXT NOT NULL,
    summary       TEXT,
    payload       TEXT,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER
);

CREATE INDEX IF NOT EXISTS idx_insights_week ON insights (week_start, id);
