"""Fixed-window rate limiting for the credential and upload endpoints.

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

from fastapi import Request

from app.config import settings
from app.core.exceptions import TooManyRequestsError
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

        self._redis = redis.Redis.from_url(url, decode_responses=True)

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


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    Cloud Run terminates TLS at its front end, so `request.client.host` is the
    load balancer. The first entry of X-Forwarded-For is the original caller.
    It is spoofable in general, which is why login is also limited per
    account and uploads per signed-in user, alongside the per-IP limits — an
    attacker can rotate one or the other, but not both at once.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(
    scope: str,
    identifiers: list[str],
    limit: int,
    window_seconds: int | None = None,
    message: str = "Too many attempts. Please wait before trying again.",
) -> None:
    """Raise 429 if any identifier has exhausted its budget for this window.

    `window_seconds` defaults to RATE_LIMIT_WINDOW_SECONDS, the credential
    endpoints' window.
    """
    if not settings.RATE_LIMIT_ENABLED:
        return
    limiter = get_rate_limiter()
    window = window_seconds or settings.RATE_LIMIT_WINDOW_SECONDS
    for identifier in identifiers:
        result = await limiter.hit(f"rl:{scope}:{identifier}", limit, window)
        if not result.allowed:
            raise TooManyRequestsError(message, retry_after=result.retry_after)
