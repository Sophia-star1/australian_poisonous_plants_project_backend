-- The admin-adjustable screening thresholds: a single row, id = 1.
--
-- Why this table exists
-- ---------------------
-- The plant classifier grades every uploaded photo against two numbers: below
-- `reject_below` the upload is refused, at or above `accept_at` it passes
-- cleanly, and between them it is accepted but flagged for the reviewer.
-- Those numbers were compile-time settings (`PLANT_REJECT_BELOW` /
-- `PLANT_ACCEPT_AT`), so tuning them meant a redeploy. The admin endpoints
-- (`GET/PATCH /api/v1/admin/screening`) write this row instead; see
-- `app/services/screening_config_service.py` for how instances converge on it.
--
-- The single-row shape is enforced by the primary-key check, so two admins
-- editing at once merge into one config rather than creating two. The check
-- constraints mirror the classifier's own validation: an ordered pair inside
-- [0, 1], because a reversed pair would reject everything it also accepts.
--
-- Absence of the row means "nobody has overridden anything": the service
-- falls back to the configured defaults, so a fresh database behaves exactly
-- like the code did before this table existed.
--
-- Applied by `app/db/migrate.py` on deploy, like every other sql/ script.
-- Re-runnable: CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS screening_thresholds (
    id           smallint PRIMARY KEY CHECK (id = 1),
    reject_below double precision NOT NULL,
    accept_at    double precision NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    CHECK (reject_below >= 0 AND reject_below < accept_at AND accept_at <= 1)
);
