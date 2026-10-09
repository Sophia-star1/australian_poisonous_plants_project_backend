"""Plant endpoints: browse, detail, and search.

Replaces the mobile app's direct Supabase calls
(`from('plants').select(...)`) with a real REST surface:

* `GET  /plants/`         — full browse list (ordered by botanical name).
* `GET  /plants/count`    — how many plants there are.
* `GET  /plants/search`   — ranked search (debounced from the client).
* `GET  /plants/{id}`     — full detail row, images joined in.

And for admins:

* `PATCH  /plants/{id}`   — correct an entry.
* `DELETE /plants/{id}`   — remove an entry, its image rows and its photos.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, ConflictError, NotFoundError
from app.db.session import get_db
from app.dependencies import require_admin
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.plant import (
    SEARCHABLE_FIELDS,
    PlantCount,
    PlantDeleted,
    PlantDetail,
    PlantSummary,
)
from app.schemas.submission import PlantUpdate
from app.services.plant_service import SEARCH_DEFAULT_LIMIT, DuplicatePlantError, PlantService

router = APIRouter()


@router.get("/", response_model=ApiResponse[list[PlantSummary]])
async def list_plants(
    limit: int | None = Query(
        default=None, ge=1, le=500, description="Optional row cap (default: all)."
    ),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[PlantSummary]]:
    """Every plant, ordered by botanical name. Public.

    Returns summaries — name, family, common names and a headline toxicity —
    not the full record. Use `GET /plants/{id}` for that.

    `limit` caps the rows returned, up to 500; omit it for all of them.
    """
    rows = await PlantService(db).list_plants(limit=limit)
    return ApiResponse.success(
        data=[PlantSummary.model_validate(row) for row in rows]
    )


# Declared ahead of `/{plant_id}`, which would otherwise take "count" as an id
# and answer 422 because it is not an integer.
@router.get("/count", response_model=ApiResponse[PlantCount])
async def count_plants(db: AsyncSession = Depends(get_db)) -> ApiResponse[PlantCount]:
    """How many plants the library holds. Public.

    The same plants `GET /plants/` lists, counted by the database, so a client
    that only needs the number does not have to download every summary.
    """
    total = await PlantService(db).count_plants()
    return ApiResponse.success(data=PlantCount(total=total))


@router.get("/search", response_model=ApiResponse[list[PlantSummary]])
async def search_plants(
    q: str = Query("", description="Free-text query string."),
    limit: int = Query(
        SEARCH_DEFAULT_LIMIT, ge=1, le=100, description="Max rows returned."
    ),
    field: str | None = Query(
        None,
        description="Comma-separated field names to search. Leave empty to search all fields. "
        "Valid: botanical_name, family_name, common_names, origin_status, lethality, "
        "toxic_parts, distribution, health_effects, toxicity_by_species, toxin_profile.",
    ),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[PlantSummary]]:
    """Search plants, best matches first. Public.

    Case-insensitive substring matching across botanical and common names,
    family, origin, lethality, toxic parts, distribution, health effects, the
    toxicity profile and the toxin profile. `%` and `_` are matched literally,
    not as wildcards.

    Searching a species name — `?q=dogs` — returns the plants recorded as
    toxic to that species, not every plant that has an entry for it. Searching
    the toxin profile matches its values, so a chemical class or a named toxin
    finds the plant while a key name like `primary_class` does not.

    `field` narrows the search to a comma-separated subset, e.g.
    `?q=acacia&field=botanical_name,family_name`. Valid names are
    botanical_name, family_name, common_names, origin_status, lethality,
    toxic_parts, distribution, health_effects, toxicity_by_species and
    toxin_profile. Habitat is excluded — matching it returned more noise than
    results.

    An empty or whitespace-only `q` returns `[]` rather than everything, so a
    client clearing its search box needs no special case. `limit` caps results
    at 100.
    """
    fields: list[str] | None = None
    if field is not None:
        fields = [f.strip() for f in field.split(",") if f.strip()]
        invalid = [f for f in fields if f not in SEARCHABLE_FIELDS]
        if invalid:
            raise AppException(
                status_code=422,
                detail=f"Invalid search field(s): {', '.join(invalid)}. "
                f"Valid: {', '.join(sorted(SEARCHABLE_FIELDS))}.",
            )

    rows = await PlantService(db).search_plants(q, limit=limit, fields=fields)
    return ApiResponse.success(
        data=[PlantSummary.model_validate(row) for row in rows]
    )


@router.get("/{plant_id}", response_model=ApiResponse[PlantDetail])
async def get_plant(
    plant_id: int,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[PlantDetail]:
    """One plant in full, with its photographs. Public.

    Everything the summary list omits: distribution, habitat, seasonality, the
    toxin profile and morphology.

    Fields absent from the data are the string `"Unknown"` rather than null —
    that is how the dataset records a gap, so treat it as "not recorded" when
    displaying.

    Errors:

    * `404` — no plant with that id.
    """
    row = await PlantService(db).get_plant(plant_id)
    if row is None:
        raise NotFoundError(f"Plant {plant_id} not found.")
    return ApiResponse.success(data=PlantDetail.model_validate(row))


@router.patch("/{plant_id}", response_model=ApiResponse[PlantDetail])
async def update_plant(
    plant_id: int,
    payload: PlantUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ApiResponse[PlantDetail]:
    """Admin: correct a plant entry. Send only the fields that change.

    The same fields as the `plant` an approval takes. A field left out is
    unchanged. A field sent blank or `null` goes back to `"Unknown"`, the
    dataset's "not recorded" — except `botanical_name`, which can be changed
    but not cleared. `toxicity_by_species`, `toxin_profile` and
    `morphological_attributes` are merged key by key, so
    `{"toxicity_by_species": {"dogs": "Toxic"}}` changes dogs and leaves the
    other species as they were.

    Photographs are not edited here. Returns the entry as `GET` would.

    Errors:

    * `404` — no plant with that id.
    * `409` — renaming to a botanical name another plant already has.
    * `422` — nothing to change, an unknown field, or a cleared name.
    """
    service = PlantService(db)
    try:
        plant = await service.update_plant(plant_id, payload)
    except DuplicatePlantError as exc:
        raise ConflictError(str(exc)) from exc
    if plant is None:
        raise NotFoundError(f"Plant {plant_id} not found.")

    out = PlantDetail.model_validate(plant)
    # Committed before answering, so a failed commit is an error the admin
    # sees rather than a 200 for a change that was never written.
    await db.commit()
    return ApiResponse.success(data=out, message="Plant updated.")


@router.delete("/{plant_id}", response_model=ApiResponse[PlantDeleted])
async def delete_plant(
    plant_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ApiResponse[PlantDeleted]:
    """Admin: delete a plant entry for good.

    Removes the entry, its image rows, and its photographs in this project's
    plant bucket. There is no undo. Images hosted anywhere else are left in
    place and listed in `photos_not_deleted`.

    A submission approved into this plant keeps its status and its original
    photos; only the published entry goes.

    Errors:

    * `404` — no plant with that id.
    """
    service = PlantService(db)
    urls = await service.delete_plant(plant_id)
    if urls is None:
        raise NotFoundError(f"Plant {plant_id} not found.")

    # Rows first, files second: deleting the photos before the commit would,
    # on a failed commit, leave an entry whose photos are gone.
    await db.commit()
    kept = await service.delete_photo_files(urls)
    return ApiResponse.success(
        data=PlantDeleted(
            id=plant_id,
            photos_deleted=len(urls) - len(kept),
            photos_not_deleted=kept,
        ),
        message="Plant deleted.",
    )
