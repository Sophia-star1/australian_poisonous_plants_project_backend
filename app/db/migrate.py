"""Apply the SQL in sql/ and make sure the public storage buckets exist.

Run before every deploy by cloudbuild.yaml, with the image just built:

    python -m app.db.migrate

and by hand against any database the same way, with DATABASE_URL set.

Each `sql/*.sql` file runs once, in filename order, recorded by name in
`schema_migrations`. A file runs in its own transaction together with its
record, so it either applies and is recorded or neither happens. The scripts
are written to be re-runnable anyway, which is what lets the ones applied by
hand before this existed simply run again, change nothing, and be recorded.

It connects through the app's own engine (`get_engine`), so the URL is
cleaned and the pooler settings applied exactly as for the running service,
which is known to connect, rather than by a second interpretation of
DATABASE_URL that could disagree with it.

Two deploys running at once are serialised by a transaction-level advisory
lock. A session-level lock would not do: through Supabase's transaction
pooler, consecutive statements may reach different server sessions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.logging import get_logger
from app.db.session import get_engine
from app.services.storage_service import StorageService

logger = get_logger(__name__)

#: sql/ beside app/, in the repository and in the image alike.
SQL_DIR = Path(__file__).resolve().parents[2] / "sql"

#: Arbitrary, fixed: every migration run takes the same advisory lock.
MIGRATION_LOCK = 52_710_009

#: Buckets the code expects to exist and serve public URLs: all three, so a
#: fresh Supabase project needs none created by hand. With S3 they are key
#: prefixes and the check only confirms the one physical bucket exists.
PUBLIC_BUCKETS = (
    settings.SUPABASE_SUBMISSION_BUCKET,
    settings.SUPABASE_PLANT_BUCKET,
    settings.SUPABASE_AVATAR_BUCKET,
)

_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def apply_migrations(conn: Any, sql_dir: Path = SQL_DIR) -> list[str]:
    """Run every not-yet-recorded script in `sql_dir`; returns their names."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", MIGRATION_LOCK)
        await conn.execute(_LEDGER)

    applied: list[str] = []
    for script in sorted(sql_dir.glob("*.sql")):
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", MIGRATION_LOCK)
            if await conn.fetchval(
                "SELECT 1 FROM schema_migrations WHERE filename = $1", script.name
            ):
                continue
            await conn.execute(script.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", script.name)
        logger.info("migration_applied", filename=script.name)
        applied.append(script.name)
    return applied


async def main() -> int:
    engine = get_engine()
    try:
        async with engine.connect() as connection:
            # The asyncpg connection underneath: scripts with DO blocks and
            # several statements need its simple-query execute, which the
            # SQLAlchemy layer does not offer.
            raw = await connection.get_raw_connection()
            applied = await apply_migrations(raw.driver_connection)
    finally:
        await engine.dispose()
    logger.info("migrations_done", applied=applied or "none new")

    storage = StorageService()
    for bucket in PUBLIC_BUCKETS:
        outcome = await storage.ensure_public_bucket(bucket)
        logger.info("storage_bucket_checked", bucket=bucket, outcome=outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
