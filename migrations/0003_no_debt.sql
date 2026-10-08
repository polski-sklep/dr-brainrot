-- No debt carry-over (DECISIONS DR5.2), schema_version 2 -> 3.
--
-- The user's rule: going over the allowance is RECORDED, never carried into the
-- next week. `weeks.overage_min` records how far a week's pooled wasteful
-- consumption went past that week's allowance (NULL until computed; the
-- current week's figure is so far). `weeks.debt_min` stays for backward
-- compatibility with the queue's schema but is zeroed here and never written again:
-- every week gets its full allowance.
ALTER TABLE weeks ADD COLUMN overage_min INTEGER CHECK (overage_min IS NULL OR overage_min >= 0);
UPDATE weeks SET debt_min = 0;
