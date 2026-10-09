"""Admin correction and removal of published plants.

Until these existed nothing could change or remove a plant once an approval
had published it: three test approvals put "Test222", "Test333" and
"TestImage" into the public library, and the only way out was SQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.main import app
from app.models.plant import Plant, PlantImage, unknown_defaults
from app.models.user import User
from app.services.storage_service import StorageService

PROJECT = "https://proj.supabase.co"
PUBLIC = f"{PROJECT}/storage/v1/object/public"


def _plant(plant_id: int = 7) -> Plant:
    plant = Plant(id=plant_id, botanical_name="Abrus precatorius", **unknown_defaults())
    plant.habitat = "Riverbanks"
    plant.toxicity_by_species = {**plant.toxicity_by_species, "humans": "Toxic"}
    urls = [
        f"{PUBLIC}/plant-images-v2/002_Abrus.jpg",
        f"{PUBLIC}/plant-images-v2/plants/7/abc.jpg",
        f"{PUBLIC}/submission-images/submissions/x/1.jpg",
        "https://images.ala.org.au/image/abc.jpg",
    ]
    plant.images = [
        PlantImage(id=i, image_url=url, is_primary=(i == 0)) for i, url in enumerate(urls)
    ]
    return plant


class _Session:
    def __init__(self, library: dict[int, Plant], events: list[str]) -> None:
        self.library = library
        self.events = events
        self.duplicate_of: int | None = None
        self.lookups: list[Any] = []

    async def get(self, model: type, primary_key: Any) -> Any:
        return self.library.get(primary_key) if model is Plant else None

    async def scalar(self, statement: Any) -> Any:
        self.lookups.append(statement)
        return self.duplicate_of

    async def delete(self, obj: Any) -> None:
        self.events.append(f"delete row {obj.id}")

    async def flush(self) -> None:
        self.events.append("flush")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        return None


def _user(role: str) -> User:
    user = User()
    user.id = 1
    user.email = f"{role}@example.com"
    user.full_name = role
    user.is_active = True
    user.role = role
    return user


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """One plant in the library, an admin signed in, storage recorded."""
    events: list[str] = []
    session = _Session({7: _plant()}, events)
    state: dict[str, Any] = {"session": session, "events": events, "fail": set()}

    async def _delete_file(self: Any, key: str, bucket: str | None = None) -> bool:
        events.append(f"delete file {bucket}/{key}")
        return key not in state["fail"]

    monkeypatch.setattr(settings, "SUPABASE_URL", PROJECT)
    monkeypatch.setattr(StorageService, "delete_file", _delete_file)

    async def _db() -> AsyncIterator[_Session]:
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_active_user] = lambda: _user("admin")
    yield state
    app.dependency_overrides.clear()


# ── who may ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_only_admins_may_change_the_library(
    async_client: AsyncClient, world: dict[str, Any], method: str
) -> None:
    app.dependency_overrides[get_current_active_user] = lambda: _user("user")
    kwargs = {"json": {"habitat": "x"}} if method == "patch" else {}

    response = await getattr(async_client, method)("/api/v1/plants/7", **kwargs)

    assert response.status_code == 403
    assert world["events"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_an_unknown_plant_is_404(
    async_client: AsyncClient, world: dict[str, Any], method: str
) -> None:
    kwargs = {"json": {"habitat": "x"}} if method == "patch" else {}
    response = await getattr(async_client, method)("/api/v1/plants/999", **kwargs)
    assert response.status_code == 404


# ── editing ──────────────────────────────────────────────────────────────────


async def _patch(client: AsyncClient, body: dict[str, Any]) -> Any:
    return await client.patch("/api/v1/plants/7", json=body)


@pytest.mark.asyncio
async def test_only_the_fields_sent_change(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    response = await _patch(async_client, {"family_name": "Fabaceae"})

    assert response.status_code == 200, response.text
    plant = world["session"].library[7]
    assert plant.family_name == "Fabaceae"
    assert plant.habitat == "Riverbanks", "an omitted field is left alone"
    assert response.json()["data"]["family_name"] == "Fabaceae"
    assert world["events"][-1] == "commit"


@pytest.mark.asyncio
async def test_a_blank_field_goes_back_to_unknown(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    await _patch(async_client, {"habitat": "   ", "common_names": [], "health_effects": None})

    plant = world["session"].library[7]
    assert (plant.habitat, plant.common_names, plant.health_effects) == (
        "Unknown",
        ["Unknown"],
        "Unknown",
    )


@pytest.mark.asyncio
async def test_objects_are_merged_key_by_key(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    """Correcting one species must not wipe the other ten."""
    await _patch(async_client, {"toxicity_by_species": {"dogs": " toxic "}})

    species = world["session"].library[7].toxicity_by_species
    assert species["dogs"] == "Toxic"
    assert species["humans"] == "Toxic", "untouched key kept"
    assert set(species) == set(unknown_defaults()["toxicity_by_species"])


@pytest.mark.asyncio
async def test_a_blank_key_resets_just_that_key(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    await _patch(async_client, {"toxicity_by_species": {"humans": ""}})
    assert world["session"].library[7].toxicity_by_species["humans"] == "Unknown"


@pytest.mark.asyncio
async def test_an_extra_morphology_key_can_be_added_and_removed(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    await _patch(async_client, {"morphological_attributes": {"rhizome": "thick"}})
    assert world["session"].library[7].morphological_attributes["rhizome"] == "thick"

    await _patch(async_client, {"morphological_attributes": {"rhizome": ""}})
    assert "rhizome" not in world["session"].library[7].morphological_attributes


@pytest.mark.asyncio
async def test_renaming_to_a_name_already_taken_is_a_409(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    session = world["session"]
    session.duplicate_of = 3

    response = await _patch(async_client, {"botanical_name": "Nerium oleander"})

    assert response.status_code == 409
    assert "already exists (id 3)" in response.json()["message"]
    assert session.library[7].botanical_name == "Abrus precatorius"
    assert "commit" not in world["events"]
    # The plant being renamed is not its own duplicate.
    (lookup,) = session.lookups
    assert "plants_v2.id !=" in str(lookup.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_a_rename_to_a_free_name_goes_through(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    """The fix for a typo'd approval such as "Test222"."""
    response = await _patch(async_client, {"botanical_name": "Abrus  precatorius L."})
    assert response.status_code == 200
    assert world["session"].library[7].botanical_name == "Abrus  precatorius L."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"botanical_name": None},
        {"botanical_name": "   "},
        {},
        {"toxic_to_humans": True},
    ],
    ids=["null name", "blank name", "nothing to change", "unknown field"],
)
async def test_these_edits_are_refused(
    async_client: AsyncClient, world: dict[str, Any], body: dict[str, Any]
) -> None:
    response = await _patch(async_client, body)
    assert response.status_code == 422
    assert world["events"] == []


# ── deleting ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_removes_the_rows_then_the_files(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    response = await async_client.delete("/api/v1/plants/7")

    assert response.status_code == 200, response.text
    events = world["events"]
    assert events[:3] == ["delete row 7", "flush", "commit"], "rows committed before files"
    assert events[3:] == [
        "delete file plant-images-v2/002_Abrus.jpg",
        "delete file plant-images-v2/plants/7/abc.jpg",
    ]
    data = response.json()["data"]
    assert data["id"] == 7
    assert data["photos_deleted"] == 2
    # Another bucket and another host are reported, never deleted.
    assert data["photos_not_deleted"] == [
        f"{PUBLIC}/submission-images/submissions/x/1.jpg",
        "https://images.ala.org.au/image/abc.jpg",
    ]


@pytest.mark.asyncio
async def test_a_file_that_will_not_delete_is_reported_not_raised(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    world["fail"].add("plants/7/abc.jpg")

    response = await async_client.delete("/api/v1/plants/7")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["photos_deleted"] == 1
    assert f"{PUBLIC}/plant-images-v2/plants/7/abc.jpg" in data["photos_not_deleted"]


# ── the storage guard deletion relies on ─────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "key"),
    [
        (f"{PUBLIC}/plant-images-v2/plants/7/abc.jpg", "plants/7/abc.jpg"),
        (f"{PUBLIC}/submission-images/plants/7/abc.jpg", None),
        (f"{PUBLIC}/plant-images-v2-old/abc.jpg", None),
        ("https://other.supabase.co/storage/v1/object/public/plant-images-v2/a.jpg", None),
        ("plants/7/abc.jpg", None),
        (f"{PUBLIC}/plant-images-v2/", None),
    ],
)
def test_key_in_bucket_only_answers_for_that_bucket_here(
    monkeypatch: pytest.MonkeyPatch, url: str, key: str | None
) -> None:
    monkeypatch.setattr(settings, "SUPABASE_URL", PROJECT)
    assert StorageService().key_in_bucket(url, "plant-images-v2") == key
