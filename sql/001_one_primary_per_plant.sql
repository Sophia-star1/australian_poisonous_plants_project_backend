-- One primary photo per plant, enforced by the database.
--
-- Why this is not just an application concern
-- -------------------------------------------
-- Clients read the cover photo either as `plant_images[0]` — which the
-- `(is_primary DESC, id)` ordering on `Plant.images` now makes deterministic —
-- or by looking for `is_primary`. The second reading has no defined answer if a
-- plant has two, and nothing was stopping that: the only indexes on
-- `plant_images_v2` are the primary key, a unique `(plant_id, image_url)` and a
-- plain `plant_id`.
--
-- The application does maintain the invariant today (the first uploaded photo
-- becomes primary, and promotion carries the flag across unchanged), so this
-- index is not fixing a live defect. It is making the invariant true rather
-- than merely intended — one careless UPDATE in the Supabase SQL editor is
-- enough to break it, and the failure is silent: the cover photo simply becomes
-- whichever row the planner returns first.
--
-- A partial unique index rather than a constraint, because the rule applies
-- only to rows where `is_primary` is true; a plant may have any number of
-- non-primary photos.
--
-- Safe to run on the current data: verified that no plant has more than one
-- primary, so there is nothing to reconcile first. Re-runnable thanks to
-- IF NOT EXISTS.
--
-- Run once against the project database:
--   psql "$DATABASE_URL" -f sql/001_one_primary_per_plant.sql
-- or paste it into the Supabase SQL editor.
--
-- This file exists because the project has no migration history yet: Alembic is
-- configured but `alembic/versions/*.py` is gitignored and the database has no
-- `alembic_version` table, so there is nowhere for a revision to live. Fold
-- this into the first real migration when that is set up.

-- Check before creating, so a pre-existing violation reports itself rather than
-- surfacing as an opaque index-build failure.
DO $$
DECLARE
    offenders int;
BEGIN
    SELECT count(*) INTO offenders
    FROM (
        SELECT plant_id
        FROM public.plant_images_v2
        WHERE is_primary
        GROUP BY plant_id
        HAVING count(*) > 1
    ) t;

    IF offenders > 0 THEN
        RAISE EXCEPTION
            '% plant(s) already have more than one primary photo; resolve those before adding the index',
            offenders;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS plant_images_v2_one_primary_per_plant
    ON public.plant_images_v2 (plant_id)
    WHERE is_primary;
