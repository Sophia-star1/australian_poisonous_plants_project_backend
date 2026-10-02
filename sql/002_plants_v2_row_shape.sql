-- Keep every `plants_v2` row in the shape the API can serve.
--
-- Why
-- ---
-- The read endpoints load rows in bulk, so one malformed row does not fail on
-- its own — it takes the whole request down with it:
--
--   * a jsonb column holding anything but an object (`'"Unknown"'`, `'[]'`)
--     makes `jsonb_each_text` raise "cannot call jsonb_each_text on a
--     non-object", and the species / toxin-profile filters call it on every
--     row: every search returns 500, whatever was searched for
--   * the same row fails `dict[str, Any]` validation in `PlantSummary`, so
--     GET /plants/ — the browse list — returns 500 as well
--   * a NULL inside a text array (`ARRAY['Jequirity', NULL]`) fails
--     `list[str]` validation the same way; NOT NULL on the column does not
--     look inside it
--   * a blank `botanical_name` publishes an entry nobody can find or name
--
-- The API never writes any of these — `_create_plant` fills every column with
-- a well-formed value. The risk is the Supabase table editor and SQL editor,
-- and bulk imports like the one that created this table. These constraints
-- make the database refuse such a row at the moment it is written, with the
-- constraint's name in the error, instead of the app failing for every user
-- until someone finds the row.
--
-- Safe to run on the current data: verified against the live table that all
-- 100 rows already satisfy every check (all three jsonb columns are objects,
-- no array holds a NULL, no name is blank). Each constraint is checked first
-- and reports the offending ids rather than failing with a bare violation.
-- Re-runnable: an existing constraint is left alone.
--
-- Run once against the project database, either in the Supabase SQL editor or
-- with psql. Note `psql` does not accept the app's `DATABASE_URL` as-is: drop
-- the `+asyncpg` from its scheme first.
--
--   psql "postgresql://…" -f sql/002_plants_v2_row_shape.sql
--
-- Like 001, this lives in `sql/` because the project has no migration history
-- yet. Fold both into the first real migration when that is set up.

DO $$
DECLARE
    checks text[][] := ARRAY[
        ['plants_v2_botanical_name_not_blank',
         $c$btrim(botanical_name) <> ''$c$],
        ['plants_v2_toxicity_by_species_is_object',
         $c$jsonb_typeof(toxicity_by_species) = 'object'$c$],
        ['plants_v2_toxin_profile_is_object',
         $c$jsonb_typeof(toxin_profile) = 'object'$c$],
        ['plants_v2_morphological_attributes_is_object',
         $c$jsonb_typeof(morphological_attributes) = 'object'$c$],
        ['plants_v2_text_arrays_have_no_nulls',
         $c$array_position(common_names, NULL) IS NULL
            AND array_position(toxic_parts, NULL) IS NULL
            AND array_position(distribution, NULL) IS NULL$c$]
    ];
    i int;
    name text;
    expr text;
    offenders bigint[];
BEGIN
    FOR i IN 1 .. array_length(checks, 1) LOOP
        name := checks[i][1];
        expr := checks[i][2];

        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.plants_v2'::regclass AND conname = name
        ) THEN
            RAISE NOTICE '% already exists, skipped', name;
            CONTINUE;
        END IF;

        EXECUTE format(
            'SELECT array_agg(id ORDER BY id) FROM public.plants_v2 WHERE NOT (%s)', expr
        ) INTO offenders;
        IF offenders IS NOT NULL THEN
            RAISE EXCEPTION '% would reject existing plants_v2 rows %; fix them first',
                name, offenders;
        END IF;

        EXECUTE format('ALTER TABLE public.plants_v2 ADD CONSTRAINT %I CHECK (%s)', name, expr);
        RAISE NOTICE '% added', name;
    END LOOP;
END $$;
