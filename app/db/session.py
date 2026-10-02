"""Async database session management."""

import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

logger = logging.getLogger(__name__)


def _clean_url(raw: str) -> str:
    """Strip BOM and surrounding whitespace from a DATABASE_URL secret.

    PowerShell on Windows often writes UTF-8 BOM (or trailing \r\n) when
    creating Secret Manager values, which makes SQLAlchemy's URL parser
    fail with "Could not parse SQLAlchemy URL from given URL string".
    Be defensive so a slightly-mangled secret still boots.
    """
    if not raw:
        return raw
    # UTF-8 BOM (read as one char), UTF-16 BOMs as raw bytes, plus whitespace.
    return raw.lstrip("﻿").lstrip("ï»¿").strip()


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
    url = _clean_url(settings.DATABASE_URL)
    # Log the scheme and host so deploy issues are easy to diagnose without
    # leaking the password.
    try:
        from sqlalchemy.engine.url import make_url
        parsed = make_url(url)
        logger.info(
            "DB engine init: dialect=%s host=%s port=%s db=%s",
            parsed.get_backend_name(),
            parsed.host,
            parsed.port,
            parsed.database,
        )
    except Exception as exc:
        logger.error(
            "DATABASE_URL appears malformed (length=%d, starts_with=%r): %s",
            len(url),
            url[:20] if url else "",
            exc,
        )
        raise

    connect_args: dict[str, object] = {}
    if _is_pooler(url):
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
        connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid4()}__"

    return create_async_engine(
        url,
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


async def ping() -> None:
    """Round-trip one trivial query, raising if the database cannot answer."""
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield an async database session for FastAPI dependency injection."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
