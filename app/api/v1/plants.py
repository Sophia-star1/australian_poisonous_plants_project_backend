"""Plant endpoints: browse, detail, and search.

Replaces the mobile app's direct Supabase calls
(`from('plants').select(...)`) with a real REST surface:

* `GET  /plants/`         — full browse list (ordered by botanical name).
* `GET  /plants/search`   — ranked search (debounced from the client).
* `GET  /plants/{id}`     — full detail row, images joined in.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.session import get_db
from app.schemas.common import ApiResponse
from app.schemas.plant import PlantDetail, PlantSummary
from app.services.plant_service import SEARCH_DEFAULT_LIMIT, PlantService

router = APIRouter()


@router.get("/", response_model=ApiResponse[list[PlantSummary]])
async def list_plants(
    limit: int | None = Query(
        default=None, ge=1, le=500, description="Optional row cap (default: all)."
    ),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[PlantSummary]]:
    """Return every plant, ordered by botanical name.

    Drives the `PlantsScreen` browse list.
    """
    rows = await PlantService(db).list_plants(limit=limit)
    return ApiResponse.success(
        data=[PlantSummary.model_validate(row) for row in rows]
    )


@router.get("/search", response_model=ApiResponse[list[PlantSummary]])
async def search_plants(
    q: str = Query("", description="Free-text query string."),
    limit: int = Query(
        SEARCH_DEFAULT_LIMIT, ge=1, le=100, description="Max rows returned."
    ),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[PlantSummary]]:
    """Ranked search over botanical name, common names, and family.

    Empty / whitespace-only queries return `[]` so the mobile hook can
    clear results without an extra round trip.
    """
    rows = await PlantService(db).search_plants(q, limit=limit)
    return ApiResponse.success(
        data=[PlantSummary.model_validate(row) for row in rows]
    )


@router.get("/{plant_id}", response_model=ApiResponse[PlantDetail])
async def get_plant(
    plant_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[PlantDetail]:
    """Full plant detail (with images) for `PlantDetailScreen`."""
    row = await PlantService(db).get_plant(plant_id)
    if row is None:
        raise NotFoundError(f"Plant {plant_id} not found.")
    return ApiResponse.success(data=PlantDetail.model_validate(row))
