# Database schema (supplied separately)

The SQL files that create the database (tables, constraints, and the plant
library data) are handed over separately, not kept in this repository.

For the Docker Compose stack, copy them here, named so they sort in the order
they must run (`01_schema.sql`, `02_data.sql`, ...). PostgreSQL runs every
`*.sql` file in this folder once, when its data volume is first created, and
stops at the first error. To run them again, remove the volume:
`docker compose down -v` (this deletes the database).

For any other database, run them by hand before the first deploy; see
DEPLOY.md, "Database".

After the schema is in place, `python -m app.db.migrate` applies the
backend's own changes in `../sql/` on top of it. Those scripts assume the
tables already exist.
