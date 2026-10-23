"""Runtime-adjustable plant-screening thresholds.

The admin endpoints (`app/api/v1/admin.py`) write one row into
`screening_thresholds`; the classifier grades every uploaded photo against
that row until it changes again. Why a database row and not an environment
variable:

* Changing an env var means a deploy — minutes, and a blast radius of the
  whole service — where an admin turning a knob wants seconds.
* Cloud Run runs several instances. A process-local override would only
  change whichever instance happened to serve the PATCH.

Every instance refreshes its local override from the row on the upload path,
at most once per `CACHE_SECONDS`, so a change made anywhere reaches every
instance within that window. When the row does not exist the configured
defaults (`PLANT_REJECT_BELOW` / `PLANT_ACCEPT_AT`) govern, which keeps a
bare checkout and a fresh database behaving exactly as before.

The refresh path is deliberately non-raising: the config store is a
convenience on the upload hot path, and a database that cannot answer must
not take submissions down with it. A failed refresh keeps the thresholds the
process already holds and tries again after the throttle window.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.ml.classifier import (
    clear_runtime_thresholds,
    set_runtime_thresholds,
)
from app.models.screening_thresholds import ScreeningThresholds

logger = get_logger(__name__)

#: How often one instance re-reads the row. Bounds both the staleness after a
#: change and the extra queries the upload path pays; 30 s keeps a burst of
#: uploads to one cheap query per half minute per instance.
CACHE_SECONDS = 30.0

_last_refresh = 0.0


def reset_refresh_throttle() -> None:
    """Forget when this process last read the row. For tests."""
    global _last_refresh
    _last_refresh = 0.0


class ScreeningConfigService:
    """Read and write the runtime screening thresholds."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self) -> ScreeningThresholds | None:
        """The stored row, or `None` when no admin has ever set one."""
        result = await self.db.execute(
            select(ScreeningThresholds).where(ScreeningThresholds.id == 1)
        )
        return result.scalars().first()

    async def update(
        self, reject_below: float, accept_at: float, updated_by: str
    ) -> ScreeningThresholds:
        """Write the pair and apply it to this process immediately.

        `updated_at` is set here rather than left to the server default, so
        the returned row carries it without another round trip. The caller's
        session still owns the commit; other instances converge on the row
        through `refresh`.
        """
        if not 0.0 <= reject_below < accept_at <= 1.0:
            raise ValueError(
                f"Thresholds must satisfy 0 <= reject_below ({reject_below}) "
                f"< accept_at ({accept_at}) <= 1."
            )

        row = await self.get()
        if row is None:
            row = ScreeningThresholds(
                id=1,
                reject_below=reject_below,
                accept_at=accept_at,
                updated_by=updated_by,
                updated_at=datetime.now(UTC),
            )
            self.db.add(row)
        else:
            row.reject_below = reject_below
            row.accept_at = accept_at
            row.updated_by = updated_by
            row.updated_at = datetime.now(UTC)

        await self.db.flush()
        set_runtime_thresholds(reject_below, accept_at)
        return row

    async def refresh(self) -> None:
        """Bring this process's override up to date, throttled.

        Called on the upload path before any photo is scored. Throttled so a
        burst of uploads costs one query per `CACHE_SECONDS`, and non-raising
        so a config store that cannot answer never blocks an upload.
        """
        global _last_refresh
        now = time.monotonic()
        if now - _last_refresh < CACHE_SECONDS:
            return
        # Record the attempt before the query: a failure also throttles, so a
        # sick database is not hammered once per photo.
        _last_refresh = now
        try:
            row = await self.get()
        except Exception as exc:
            logger.debug(
                "screening_thresholds_refresh_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if row is None:
            clear_runtime_thresholds()
        else:
            set_runtime_thresholds(row.reject_below, row.accept_at)
