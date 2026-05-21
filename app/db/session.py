"""Async database session management."""

from collections.abc import AsyncIterator
from functools import lru_cache
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


def _is_pooler(url: str) -> bool:
    """Detect Supabase's transaction pooler from the host string."""
    return "pooler.supabase.com" in url


@lru_cache(maxsize=1)
def get_engine():
    """Create the async engine lazily on first use.

    Supabase's transaction pooler (port 6543) is pgbouncer in
    transaction-pool mode, which does not support server-side prepared
    statements. We disable asyncpg's cache and give every prepared
    statement a unique name when we detect the pooler.
    """
    connect_args: dict[str, object] = {}
    if _is_pooler(settings.DATABASE_URL):
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
        connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid4()}__"

    return create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        connect_args=connect_args,
    )


@lru_cache(maxsize=1)
def get_session_factory():
    """Create the session factory lazily on first use."""
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield an async database session for FastAPI dependency injection."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
