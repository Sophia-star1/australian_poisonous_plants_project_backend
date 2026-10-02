"""Revocation list for issued JWTs.

JWTs are self-contained, so "log out" cannot mean anything on its own — the
server has to remember that a particular token is no longer welcome. Each
token carries a `jti` (JWT ID); logging out records that id until the token
would have expired anyway, which bounds the storage without needing a sweeper.

Backends mirror `core.rate_limit`:

* Redis when ``REDIS_URL`` is set. Required for this to be meaningful in
  production: with several Cloud Run instances, a per-process list only
  revokes the token on whichever instance happened to serve the logout.
* In-memory otherwise, which is correct for local development and a single
  worker. The active backend is logged at startup.

Entries are keyed by `jti` rather than the raw token so nothing that could be
replayed is ever written to storage or a log.
"""

import time
from dataclasses import dataclass, field

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "revoked:"


class InMemoryDenylist:
    """Per-process revocation list. Not shared between workers."""

    def __init__(self) -> None:
        self._revoked: dict[str, float] = {}

    def _purge(self, now: float) -> None:
        """Forget entries whose token has expired on its own."""
        expired = [jti for jti, expires_at in self._revoked.items() if expires_at <= now]
        for jti in expired:
            del self._revoked[jti]

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        """Mark `jti` as revoked for the remainder of its lifetime."""
        now = time.time()
        self._purge(now)
        self._revoked[jti] = now + max(ttl_seconds, 1)

    async def is_revoked(self, jti: str) -> bool:
        """Report whether `jti` has been revoked and has not yet expired."""
        expires_at = self._revoked.get(jti)
        if expires_at is None:
            return False
        if expires_at <= time.time():
            del self._revoked[jti]
            return False
        return True

    async def consume(self, jti: str, ttl_seconds: int) -> bool:
        """Revoke `jti` and report whether this call was the one that did.

        Check and set happen with no `await` between them, so on one event
        loop two refreshes racing with the same token cannot both win.
        """
        if await self.is_revoked(jti):
            return False
        await self.revoke(jti, ttl_seconds)
        return True


class RedisDenylist:
    """Revocation list shared across processes; entries expire via Redis TTL."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # imported lazily: optional dependency

        self._redis = redis.from_url(url, decode_responses=True)

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        """Mark `jti` as revoked for the remainder of its lifetime."""
        await self._redis.set(f"{_KEY_PREFIX}{jti}", "1", ex=max(ttl_seconds, 1))

    async def is_revoked(self, jti: str) -> bool:
        """Report whether `jti` has been revoked and has not yet expired."""
        return bool(await self._redis.exists(f"{_KEY_PREFIX}{jti}"))

    async def consume(self, jti: str, ttl_seconds: int) -> bool:
        """Revoke `jti` and report whether this call was the one that did.

        `SET NX` is a single atomic command, so across every instance exactly
        one caller sees True for a given token.
        """
        created = await self._redis.set(f"{_KEY_PREFIX}{jti}", "1", ex=max(ttl_seconds, 1), nx=True)
        return bool(created)


@dataclass
class _DenylistHolder:
    """Lazily built singleton so importing this module has no side effects."""

    instance: InMemoryDenylist | RedisDenylist | None = field(default=None)


_holder = _DenylistHolder()


def get_denylist() -> InMemoryDenylist | RedisDenylist:
    """Return the process-wide denylist, building it on first use."""
    if _holder.instance is None:
        if settings.redis_enabled:
            _holder.instance = RedisDenylist(settings.REDIS_URL)
            logger.info("token_denylist_backend", backend="redis")
        else:
            _holder.instance = InMemoryDenylist()
            logger.warning(
                "token_denylist_backend",
                backend="in-memory",
                detail="Revocations are per-process; set REDIS_URL to share them across instances.",
            )
    return _holder.instance


def reset_denylist() -> None:
    """Drop the singleton. Test helper — not used at runtime."""
    _holder.instance = None


def seconds_until(expiry_timestamp: float | None) -> int:
    """Seconds from now until `expiry_timestamp`, floored at one second.

    Used to size the denylist entry: there is no point remembering a token
    for longer than it would have been accepted anyway.
    """
    if expiry_timestamp is None:
        return int(settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400)
    return max(1, int(expiry_timestamp - time.time()))
