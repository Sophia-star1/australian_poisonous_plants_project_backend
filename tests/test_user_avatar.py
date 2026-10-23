"""Profile pictures: what is stored, and the endpoints that set and clear it."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from PIL import Image

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.main import app
from app.ml import classifier
from app.ml.classifier import ImageTooLargeError, UndecodableImageError
from app.models.user import User
from app.schemas.user import UserOut
from app.services.avatar_service import AVATAR_SIZE, normalise_avatar
from app.services.storage_service import StorageService, StoredFile

PROJECT = "https://proj.supabase.co"
AVATARS = f"{PROJECT}/storage/v1/object/public/avatars"

_GPS_IFD = 0x8825
_ORIENTATION = 0x0112


def _image_bytes(
    size: tuple[int, int] = (800, 600),
    mode: str = "RGB",
    fmt: str = "JPEG",
    colour: Any = (30, 140, 60),
    exif: Image.Exif | None = None,
) -> bytes:
    buf = io.BytesIO()
    image = Image.new(mode, size, colour)
    kwargs = {"exif": exif} if exif is not None else {}
    image.save(buf, fmt, **kwargs)
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# ── what is stored ───────────────────────────────────────────────────────────


def test_any_shape_becomes_a_square_jpeg() -> None:
    out = _open(normalise_avatar(_image_bytes((1200, 500))))
    assert (out.format, out.size, out.mode) == ("JPEG", (AVATAR_SIZE, AVATAR_SIZE), "RGB")


def test_a_small_picture_is_scaled_up_to_the_same_size() -> None:
    assert _open(normalise_avatar(_image_bytes((64, 64), fmt="PNG"))).size == (512, 512)


def test_the_gps_position_and_other_metadata_are_dropped() -> None:
    """A phone photo can carry where it was taken; an avatar is public."""
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # camera make
    exif.get_ifd(_GPS_IFD).update({1: "S", 2: (35.0, 17.0, 0.0)})
    source = _image_bytes(exif=exif)
    assert _GPS_IFD in _open(source).getexif(), "the fixture must really carry GPS"

    out = _open(normalise_avatar(source))
    assert dict(out.getexif()) == {}
    assert "icc_profile" not in out.info


def test_the_exif_orientation_is_applied_before_cropping() -> None:
    """A portrait photo is stored sideways with a rotation flag."""
    # Left half red, right half blue; orientation 6 = rotate 90 degrees clockwise
    # to display, so the red half should end up on top.
    image = Image.new("RGB", (400, 200), (0, 0, 255))
    image.paste((255, 0, 0), (0, 0, 200, 200))
    exif = Image.Exif()
    exif[_ORIENTATION] = 6
    buf = io.BytesIO()
    image.save(buf, "JPEG", exif=exif)

    out = _open(normalise_avatar(buf.getvalue())).convert("RGB")
    top = out.getpixel((256, 5))
    bottom = out.getpixel((256, 506))
    assert top[0] > 200 and top[2] < 60, f"top should be red, got {top}"
    assert bottom[2] > 200 and bottom[0] < 60, f"bottom should be blue, got {bottom}"


def test_transparency_is_flattened_onto_white_not_black() -> None:
    out = _open(normalise_avatar(_image_bytes((300, 300), "RGBA", "PNG", (0, 0, 0, 0))))
    assert all(channel > 245 for channel in out.convert("RGB").getpixel((10, 10)))


def test_bytes_that_are_not_an_image_are_refused() -> None:
    with pytest.raises(UndecodableImageError):
        normalise_avatar(b"definitely not an image")


def test_an_image_over_the_decode_budget_still_makes_an_avatar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same budget as screening. Over it, libvips decodes straight to a smaller
    size, so a large PNG — refused before — becomes a normal avatar."""
    monkeypatch.setattr(classifier, "MAX_DECODE_PIXELS", 10_000)
    monkeypatch.setattr(classifier, "SHRINK_TARGET_PIXELS", 2_500)
    avatar = Image.open(io.BytesIO(normalise_avatar(_image_bytes((400, 400), fmt="PNG"))))
    assert avatar.size == (AVATAR_SIZE, AVATAR_SIZE)


def test_transparency_is_flattened_on_the_large_image_path_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(classifier, "MAX_DECODE_PIXELS", 10_000)
    clear = _image_bytes((400, 400), mode="RGBA", fmt="PNG", colour=(0, 0, 0, 0))
    avatar = Image.open(io.BytesIO(normalise_avatar(clear)))
    assert avatar.getpixel((5, 5)) >= (250, 250, 250), "transparent areas turned black"


def test_beyond_the_source_limit_an_avatar_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small file can declare a huge image; past this limit it is not decoded at all."""
    monkeypatch.setattr(classifier, "MAX_DECODE_PIXELS", 10_000)
    monkeypatch.setattr(classifier, "MAX_SOURCE_PIXELS", 100_000)
    with pytest.raises(ImageTooLargeError):
        normalise_avatar(_image_bytes((400, 400), fmt="PNG"))


def test_the_url_is_part_of_the_public_user() -> None:
    user = User(id=1, email="a@example.com", is_active=True, role="user")
    assert UserOut.model_validate(user).avatar_url is None
    user.avatar_url = f"{AVATARS}/users/1/x.jpg"
    assert UserOut.model_validate(user).avatar_url == f"{AVATARS}/users/1/x.jpg"


# ── the endpoints ────────────────────────────────────────────────────────────


class _Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.commit_error: Exception | None = None

    async def flush(self) -> None:
        self.events.append("flush")

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.events.append("commit")

    async def rollback(self) -> None:
        return None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A signed-in user, storage recorded, and the order things happened in."""
    events: list[str] = []
    user = User(id=7, email="ada@example.com", full_name="Ada", is_active=True, role="user")
    session = _Session(events)
    state: dict[str, Any] = {"user": user, "session": session, "events": events, "uploads": []}

    async def _upload_bytes(
        self: Any, data: bytes, key: str, bucket: str, content_type: str
    ) -> StoredFile:
        events.append(f"upload {bucket}/{key}")
        state["uploads"].append({"data": data, "key": key, "type": content_type})
        return StoredFile(key=key, url=f"{PROJECT}/storage/v1/object/public/{bucket}/{key}")

    async def _delete_file(self: Any, key: str, bucket: str | None = None) -> bool:
        events.append(f"delete {bucket}/{key}")
        return True

    monkeypatch.setattr(settings, "SUPABASE_URL", PROJECT)
    monkeypatch.setattr(StorageService, "upload_bytes", _upload_bytes)
    monkeypatch.setattr(StorageService, "delete_file", _delete_file)

    async def _db() -> AsyncIterator[_Session]:
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_active_user] = lambda: user
    yield state
    app.dependency_overrides.clear()


async def _put(client: AsyncClient, data: bytes, content_type: str = "image/png") -> Any:
    return await client.put(
        "/api/v1/users/me/avatar", files={"file": ("me.png", data, content_type)}
    )


@pytest.mark.asyncio
async def test_you_must_be_signed_in(async_client: AsyncClient) -> None:
    response = await _put(async_client, _image_bytes(fmt="PNG"))
    assert response.status_code == 401
    assert (await async_client.delete("/api/v1/users/me/avatar")).status_code == 401


@pytest.mark.asyncio
async def test_uploading_stores_a_jpeg_and_returns_its_url(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    response = await _put(async_client, _image_bytes((900, 400), fmt="PNG"))

    assert response.status_code == 200, response.text
    (upload,) = world["uploads"]
    assert upload["type"] == "image/jpeg"
    assert upload["key"].startswith("users/7/") and upload["key"].endswith(".jpg")
    assert _open(upload["data"]).size == (512, 512)
    url = response.json()["data"]["avatar_url"]
    assert url == f"{AVATARS}/{upload['key']}"
    assert world["user"].avatar_url == url
    assert world["events"][-1] == "commit", "nothing to delete the first time"


@pytest.mark.asyncio
async def test_each_upload_gets_a_new_url(async_client: AsyncClient, world: dict[str, Any]) -> None:
    """A reused URL would keep showing the cached old picture."""
    first = (await _put(async_client, _image_bytes(fmt="PNG"))).json()["data"]["avatar_url"]
    second = (await _put(async_client, _image_bytes(fmt="PNG"))).json()["data"]["avatar_url"]
    assert first != second


@pytest.mark.asyncio
async def test_the_old_picture_is_deleted_only_after_the_commit(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    world["user"].avatar_url = f"{AVATARS}/users/7/old.jpg"

    await _put(async_client, _image_bytes(fmt="PNG"))

    events = world["events"]
    assert events.index("commit") < events.index("delete avatars/users/7/old.jpg")


@pytest.mark.asyncio
async def test_an_avatar_outside_our_bucket_is_never_deleted(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    world["user"].avatar_url = "https://images.example.com/someone-elses.jpg"
    await _put(async_client, _image_bytes(fmt="PNG"))
    assert not any(e.startswith("delete") for e in world["events"])


@pytest.mark.asyncio
async def test_a_failed_commit_takes_the_new_upload_back(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    world["user"].avatar_url = f"{AVATARS}/users/7/old.jpg"
    world["session"].commit_error = RuntimeError("connection was closed")

    with pytest.raises(RuntimeError, match="connection was closed"):
        await _put(async_client, _image_bytes(fmt="PNG"))

    new_key = world["uploads"][0]["key"]
    deletes = [e for e in world["events"] if e.startswith("delete")]
    assert deletes == [f"delete avatars/{new_key}"], "the new one goes, the old one stays"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "content_type", "message"),
    [
        (b"%PDF-1.7", "application/pdf", "Unsupported type"),
        (b"not an image at all", "image/jpeg", "not a readable image"),
    ],
    ids=["wrong type", "not an image"],
)
async def test_these_uploads_are_refused_and_store_nothing(
    async_client: AsyncClient,
    world: dict[str, Any],
    data: bytes,
    content_type: str,
    message: str,
) -> None:
    response = await _put(async_client, data, content_type)
    assert response.status_code == 422
    assert message in response.json()["message"]
    assert world["uploads"] == [] and world["user"].avatar_url is None


@pytest.mark.asyncio
async def test_a_file_over_the_size_limit_is_refused(
    async_client: AsyncClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    response = await _put(async_client, _image_bytes((600, 600), fmt="PNG", colour=None))
    assert response.status_code == 422
    assert "upload limit" in response.json()["message"]
    assert world["uploads"] == []


@pytest.mark.asyncio
async def test_removing_clears_the_url_then_deletes_the_file(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    world["user"].avatar_url = f"{AVATARS}/users/7/old.jpg"

    response = await async_client.delete("/api/v1/users/me/avatar")

    assert response.status_code == 200
    assert response.json()["data"]["avatar_url"] is None
    assert world["events"][-2:] == ["commit", "delete avatars/users/7/old.jpg"]


@pytest.mark.asyncio
async def test_removing_when_there_is_none_is_fine(
    async_client: AsyncClient, world: dict[str, Any]
) -> None:
    response = await async_client.delete("/api/v1/users/me/avatar")
    assert response.status_code == 200
    assert not any(e.startswith("delete") for e in world["events"])
