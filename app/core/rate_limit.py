"""Fixed-window rate limiting for credential endpoints.

Two backends behind one interface:

* Redis, when ``REDIS_URL`` is configured. Counters are shared across every
  process, which is what actually matters on Cloud Run — the service scales to
  10 instances, so a per-process limiter lets an attacker multiply their
  budget by the instance count.
* In-memory, otherwise. Correct for local development and a single worker,
  and deliberately still better than nothing in production, but the limit is
  per-process. The startup log says which one is active so this is never a
  silent surprise.

The window is fixed rather than sliding: it costs one counter per key instead
of a sorted set, and the worst case (a burst straddling the boundary allows
2x the limit) is acceptable for slowing down credential stuffing.
"""

import time
from dataclasses import dataclass, field

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RateLimitResult:
    """Outcome of a single rate limit check."""

    allowed: bool
    remaining: int
    retry_after: int


class InMemoryRateLimiter:
    """Per-process fixed-window counter. Not shared between workers."""

    def __init__(self) -> None:
        self._hits: dict[str, tuple[int, float]] = {}

    def _purge(self, now: float) -> None:
        """Drop expired windows so the dict cannot grow without bound."""
        expired = [key for key, (_, reset) in self._hits.items() if reset <= now]
        for key in expired:
            del self._hits[key]

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Count one attempt against `key` and report whether it is allowed."""
        now = time.monotonic()
        self._purge(now)

        count, reset = self._hits.get(key, (0, now + window_seconds))
        if reset <= now:
            count, reset = 0, now + window_seconds

        count += 1
        self._hits[key] = (count, reset)

        retry_after = max(1, int(reset - now))
        return RateLimitResult(
            allowed=count <= limit,
            remaining=max(0, limit - count),
            retry_after=retry_after,
        )

    async def reset(self, key: str) -> None:
        """Clear the counter, e.g. after a successful login."""
        self._hits.pop(key, None)


class RedisRateLimiter:
    """Fixed-window counter shared across every process via Redis."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # imported lazily: optional dependency

        self._redis = redis.from_url(url, decode_responses=True)

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Count one attempt against `key` and report whether it is allowed."""
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = await pipe.execute()

        if ttl < 0:  # first hit in this window, or a key with no expiry
            await self._redis.expire(key, window_seconds)
            ttl = window_seconds

        return RateLimitResult(
            allowed=int(count) <= limit,
            remaining=max(0, limit - int(count)),
            retry_after=max(1, int(ttl)),
        )

    async def reset(self, key: str) -> None:
        """Clear the counter, e.g. after a successful login."""
        await self._redis.delete(key)


@dataclass
class _LimiterHolder:
    """Lazily built singleton so importing this module has no side effects."""

    instance: InMemoryRateLimiter | RedisRateLimiter | None = field(default=None)


_holder = _LimiterHolder()


def get_rate_limiter() -> InMemoryRateLimiter | RedisRateLimiter:
    """Return the process-wide limiter, building it on first use."""
    if _holder.instance is None:
        if settings.redis_enabled:
            _holder.instance = RedisRateLimiter(settings.REDIS_URL)
            logger.info("rate_limiter_backend", backend="redis")
        else:
            _holder.instance = InMemoryRateLimiter()
            logger.warning(
                "rate_limiter_backend",
                backend="in-memory",
                detail="Counters are per-process; set REDIS_URL to share them across instances.",
            )
    return _holder.instance


def reset_rate_limiter() -> None:
    """Drop the singleton. Test helper — not used at runtime."""
    _holder.instance = None
